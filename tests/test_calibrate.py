import asyncio
import unittest
import unittest.mock

from hisense_local import calibrate, wire
from tests import fakes

PROGRAM_AT, MINUTES_AT, TOTAL_AT, LIGHT_AT, RINSE_AT = 3, 5, 6, 8, 10
DURATIONS = {9: 239, 11: 15}
LIGHT = {1: 9, 0: 1}  # the drum light is one bit (0x08) of its byte, as in real status frames


class FakeWasherState:
    """Gateway stand-in whose status block reflects the last settings sent."""

    def __init__(self, labels, fail_at=None):
        self.session = fakes.FakeSession(labels).session()
        self.values = [0] * 12
        self.values[PROGRAM_AT], self.values[LIGHT_AT] = 11, LIGHT[1]
        self.values[MINUTES_AT] = self.values[TOTAL_AT] = 15
        self.sent, self.fail_at, self.running = [], fail_at, False

    async def exchange(self, label):
        self.sent.append(label)
        if label == self.fail_at:
            return "washer offline", None
        prop, _, value = label.partition("=")
        value = int(value)
        if prop == "Selected_program_ID":
            self.values[PROGRAM_AT] = value
            self.values[MINUTES_AT] = self.values[TOTAL_AT] = DURATIONS.get(value, 60)
        elif prop == "DownLight":
            self.values[LIGHT_AT] = LIGHT[value]
        elif prop == "Extra_Rinse":
            self.values[RINSE_AT] = value
        elif prop == "Actions":
            self.running = value == 2
        return "ok", list(self.values)

    def latest(self):
        if self.running:
            self.values[MINUTES_AT] -= 1  # only the time-left copy counts down
        return list(self.values)


LABELS = ["Selected_program_ID=9", "Selected_program_ID=11", "Selected_program_ID=8",
          "DownLight=0", "DownLight=1", "Extra_Rinse=0", "Extra_Rinse=1", "Child_lock=0",
          "Child_lock=1", "Actions=2", "Actions=1", "Remote_hold=1", "Remote_hold=0"]


class CalibrateTest(unittest.IsolatedAsyncioTestCase):
    def test_plan_has_no_cycle_commands(self):
        steps = calibrate.plan(LABELS)
        self.assertTrue(all(not s.startswith(("Actions", "Remote_hold")) for s in steps))
        self.assertIn("DownLight=1", steps)

    def test_field_value_reads_bits(self):
        spec = [LIGHT_AT, 0x08, 0x08]
        self.assertEqual(wire.field_value([0] * 8 + [9], spec), 1)
        self.assertEqual(wire.field_value([0] * 8 + [1], spec), 0)
        self.assertIsNone(wire.field_value([1], spec))

    async def test_offsets_found_and_state_restored(self):
        washer = FakeWasherState(LABELS)
        report = await calibrate.run(washer, washer.latest, pause=0, with_cycle=True,
                                     cycle_wait=(0.05, 0.01, 0.01, 0.01))
        offsets = report["offsets"]
        self.assertEqual(offsets["Selected_program_ID"], PROGRAM_AT)
        self.assertEqual(offsets["DownLight"], [LIGHT_AT, 0x08, 0x08])
        self.assertEqual(offsets["Extra_Rinse"], RINSE_AT)
        self.assertEqual(offsets["minutes_left"], MINUTES_AT)  # not the constant total copy
        self.assertNotIn("Child_lock", offsets)  # never moved a byte
        self.assertEqual(washer.sent[-3:], ["Selected_program_ID=11", "Child_lock=0", "DownLight=1"])

    async def test_abort_still_restores(self):
        washer = FakeWasherState(LABELS, fail_at="DownLight=1")
        with self.assertRaises(RuntimeError):
            await calibrate.run(washer, washer.latest, pause=0)
        # light offset unknown at abort time: program and child lock still go back
        self.assertEqual(washer.sent[-2:], ["Selected_program_ID=11", "Child_lock=0"])

    async def test_no_start_when_the_quick_program_is_refused_or_stop_is_missing(self):
        washer = FakeWasherState(LABELS, fail_at=None)
        real = washer.exchange

        async def refuse_quick(label):
            if label == "Selected_program_ID=11" and "Child_lock=0" in washer.sent:
                washer.sent.append(label)
                return "refused err=5", None
            return await real(label)
        washer.exchange = refuse_quick
        await calibrate.run(washer, washer.latest, pause=0, with_cycle=True, cycle_wait=(0.01,) * 4)
        self.assertNotIn("Actions=2", washer.sent)

        washer = FakeWasherState([label for label in LABELS if label != "Actions=1"])
        await calibrate.run(washer, washer.latest, pause=0, with_cycle=True, cycle_wait=(0.01,) * 4)
        self.assertNotIn("Actions=2", washer.sent)

    async def test_unacknowledged_stop_is_sent_again(self):
        washer = FakeWasherState(LABELS)
        real, stops = washer.exchange, []

        async def lose_first_stop(label):
            if label == "Actions=1":
                stops.append(label)
                if len(stops) == 1:
                    return "no ack", None
            return await real(label)
        washer.exchange = lose_first_stop
        report = await calibrate.run(washer, washer.latest, pause=0, with_cycle=True, cycle_wait=(0.01,) * 4)
        self.assertEqual(len(stops), 2)
        self.assertEqual(report["restored"][0], ("Actions=1", "ok"))

    async def test_cancel_during_cycle_stops_drum_first(self):
        washer = FakeWasherState(LABELS)
        task = asyncio.ensure_future(calibrate.run(washer, washer.latest, pause=0, with_cycle=True,
                                                   cycle_wait=(5, 5, 5, 5)))
        while "Actions=2" not in washer.sent:
            await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        tail = washer.sent[washer.sent.index("Actions=2") + 1:]
        self.assertEqual(tail[0], "Actions=1")
        self.assertIn("Child_lock=0", tail)


if __name__ == "__main__":
    unittest.main()


def step(label, values, diff=None):
    return {"label": label, "result": "ok", "values": values, "diff": [[0, 0, 1]] if diff is None else diff}


class DeriveFromRealShapesTest(unittest.TestCase):
    """Shapes seen on a WF5I1045BWQ: program ids above 20 stored as a dial index,
    temperature values the washer clamps, the duration stored in several bytes."""

    def test_program_index_clamped_temperature_and_provisional_time_left(self):
        dial = {9: 9, 11: 11, 42: 26, 57: 38}
        temps = {0: 0, 1: 0, 3: 3, 4: 4, 9: 6, 6: 6}  # 1 does nothing, 90 is clamped to 60
        steps = []
        for prog, minutes in ((9, 239), (11, 15), (42, 58), (57, 49)):
            steps.append(step(f"Selected_program_ID={prog}", [dial[prog], 4, minutes, minutes, 0]))
        last = 4
        for t, b in temps.items():
            steps.append(step(f"temperature={t}", [38, b, 49, 49, 0], [] if b == last else None))
            last = b
        with unittest.mock.patch.dict(calibrate.catalog.KNOWN_DURATIONS, {9: 239, 11: 15, 42: 58, 57: 49}):
            found, ambiguous = calibrate.derive(steps)
        self.assertEqual(found["Selected_program_ID"], {"at": 0, "map": {"26": 42, "38": 57}})
        self.assertEqual(found["temperature"], 1)
        self.assertEqual(found["minutes_left"], 2)  # provisional until a cycle is recorded
        self.assertEqual(ambiguous["minutes_left"], [2, 3])
        self.assertEqual(wire.field_value([38, 6, 49, 49, 0], found["Selected_program_ID"]), 57)
        self.assertEqual(wire.field_value([11, 6, 49, 49, 0], found["Selected_program_ID"]), 11)
        self.assertEqual(wire.field_value([38, 6, 49, 49, 0], found["temperature"]), 6)  # never read as 90

    def test_cycle_too_short_to_tick_keeps_the_provisional_time_left(self):
        steps = [step("Selected_program_ID=9", [9, 239, 239]), step("Selected_program_ID=11", [11, 15, 15])]
        timeline = [{"t": 0, "values": [11, 15, 15]}, {"t": 1, "values": [11, 15, 15]}]
        found, ambiguous = calibrate.derive(steps, timeline)
        self.assertEqual(found["minutes_left"], 1)
        self.assertEqual(ambiguous["minutes_left"], [1, 2])

    def test_clamp_to_a_level_never_sent_finds_nothing(self):
        steps = [step("temperature=0", [0]), step("temperature=4", [4]), step("temperature=9", [6])]
        found, _ = calibrate.derive(steps)
        self.assertNotIn("temperature", found)

    def test_unchanged_flag_step_still_counts(self):
        steps = [step("DownLight=1", [9], []), step("DownLight=0", [1])]
        found, _ = calibrate.derive(steps)
        self.assertEqual(found["DownLight"], [0, 8, 8])
