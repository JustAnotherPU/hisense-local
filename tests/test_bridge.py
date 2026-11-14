import asyncio
import json
import unittest
from types import SimpleNamespace

from hisense_local import bridge as bridgelib
from tests import fakes


class StubClient:
    def __init__(self):
        self.published, self.subscribed = [], []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

    def last(self, topic):
        return next(json.loads(p) for t, p, _ in reversed(self.published) if t == topic)


class StubGateway:
    def __init__(self, session, result="ok"):
        self.session, self.ready, self.result, self.sent = session, True, result, []

    async def send(self, label):
        self.sent.append(label)
        return self.result if label in self.session.labels else "not captured"


def msg(topic, payload, retain=False):
    return SimpleNamespace(topic=topic, payload=payload.encode(), retain=retain)


class BridgeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = StubClient()
        self.gw = StubGateway(fakes.FakeSession().session())
        self.bridge = bridgelib.Bridge(self.client, asyncio.get_running_loop(), self.gw)

    async def deliver(self, message):
        self.bridge.on_message(self.client, None, message)
        for _ in range(5):
            await asyncio.sleep(0)

    def test_discovery_only_recorded_values(self):
        (topic, payload), = self.bridge.discovery()
        self.assertEqual(topic, "homeassistant/device/hisense_local/config")
        cmps = payload["cmps"]
        self.assertEqual(cmps["program"]["options"], ["Eco 40-60", "Quick 15'"])
        self.assertEqual(cmps["temperature"]["options"], ["30 °C"])  # value 1 is never offered
        self.assertEqual(cmps["spin"]["options"], ["800 rpm"])
        self.assertEqual(cmps["delay"]["options"], ["None", "3 h"])
        switches = sorted(k for k, c in cmps.items() if c["p"] == "switch")
        self.assertEqual(switches, ["child_lock", "downlight", "extra_rinse"])
        buttons = sorted(k for k, c in cmps.items() if c["p"] == "button")
        self.assertEqual(buttons, ["pause", "resume", "start", "stop"])
        self.assertEqual(cmps["start"]["cmd_t"], "hisense_local/set/Actions")
        self.assertEqual(cmps["start"]["pl_prs"], "2")
        self.assertEqual(len(cmps["start"]["avty"]), 2)
        self.assertEqual(cmps["mode"]["ent_cat"], "diagnostic")
        self.assertNotIn("minutes_left", cmps)  # no offset found yet, no sensor

    def test_per_entity_discovery(self):
        bridge = bridgelib.Bridge(self.client, None, self.gw, per_entity=True)
        topics = dict(bridge.discovery())
        program = topics["homeassistant/select/hisense_local/program/config"]
        self.assertEqual(program["name"], "Program")
        self.assertEqual(program["dev"]["name"], "Washer")
        self.assertIn("Quick 15'", program["options"])
        self.assertIn("homeassistant/button/hisense_local/start/config", topics)

    def test_on_connect_subscribes_qos0_and_retains_discovery(self):
        self.bridge.loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *a: None)
        self.bridge.on_connect(self.client, None, {}, 0)
        self.assertEqual(self.client.subscribed, [("hisense_local/set/+", 0), ("hisense_local/capture/next", 0)])
        retained = {t for t, _, r in self.client.published if r}
        self.assertIn("homeassistant/device/hisense_local/config", retained)
        self.assertIn("hisense_local/alive", retained)

    async def test_select_routes_to_label_and_reports(self):
        await self.deliver(msg("hisense_local/set/Selected_program_ID", "Quick 15'"))
        self.assertEqual(self.gw.sent, ["Selected_program_ID=11"])
        self.assertEqual(self.client.last("hisense_local/result"),
                         {"label": "Selected_program_ID=11", "result": "ok"})
        self.assertEqual(self.client.last("hisense_local/state")["Selected_program_ID"], "Quick 15'")

    async def test_switch_and_button(self):
        await self.deliver(msg("hisense_local/set/DownLight", "1"))
        await self.deliver(msg("hisense_local/set/Actions", "2"))
        self.assertEqual(self.gw.sent, ["DownLight=1", "Actions=2"])
        self.assertEqual(self.client.last("hisense_local/state")["DownLight"], "ON")

    async def test_retained_command_ignored(self):
        await self.deliver(msg("hisense_local/set/Actions", "2", retain=True))
        await self.deliver(msg("hisense_local/capture/next", "Steam=1", retain=True))
        self.assertEqual(self.gw.sent, [])
        self.assertFalse(any(t == "hisense_local/result" for t, _, _ in self.client.published))

    async def test_refused_change_snaps_back(self):
        await self.deliver(msg("hisense_local/set/DownLight", "0"))
        self.gw.result = "refused err=5"
        await self.deliver(msg("hisense_local/set/DownLight", "1"))
        state = self.client.last("hisense_local/state")
        self.assertEqual(state["DownLight"], "OFF")
        self.assertEqual(state["last_result"], "DownLight=1: refused err=5")

    async def test_state_shows_done_and_off(self):
        self.bridge.offsets = {"minutes_left": 3,
                               "machine_status": {"at": 0, "map": {"64": "running", "127": "ready"}}}
        self.bridge.on_status([64, 0, 0, 5])
        self.assertEqual(self.client.last("hisense_local/state")["machine_status"], "running")
        self.bridge.on_status([64, 0, 0, 0])
        self.assertEqual(self.client.last("hisense_local/state")["machine_status"], "done")
        self.gw.result = "no ack"
        await self.deliver(msg("hisense_local/set/DownLight", "0"))
        self.assertEqual(self.client.last("hisense_local/state")["machine_status"], "off")
        self.bridge.on_status([127, 0, 0, 15])
        self.assertEqual(self.client.last("hisense_local/state")["machine_status"], "ready")
        self.bridge.on_status([71, 0, 0, 19])
        self.assertIsNone(self.client.last("hisense_local/state")["machine_status"])  # unnamed value

    async def test_unrecorded_value_not_sent_to_washer(self):
        await self.deliver(msg("hisense_local/set/temperature", "1"))
        await self.deliver(msg("hisense_local/set/Remote_hold", "7"))
        self.assertEqual(self.client.last("hisense_local/result")["result"], "not offered")
        self.assertEqual(self.gw.sent, [])

    def test_every_catalog_name_is_a_valid_name(self):
        from hisense_local import catalog
        names = set(catalog.SELECTS) | set(catalog.SWITCH_NAMES) | catalog.NOT_EXPOSED | set(catalog.SENSOR_NAMES)
        names |= {p for p, _ in catalog.BUTTONS.values()} | set(catalog.CALIBRATION_PROPS)
        self.assertEqual([n for n in names if not bridgelib.NAME.fullmatch(n)], [])

    def test_label_that_would_break_a_topic_or_template_is_not_a_control(self):
        odd = ["x/#=0", "x/#=1", "a }}{{ states=0", "a }}{{ states=1", "Steam=0", "Steam=1"]
        self.assertEqual(bridgelib.controls(odd), {"Steam": {0, 1}})

    async def test_long_payload_is_cut_before_it_is_echoed(self):
        await self.deliver(msg("hisense_local/set/DownLight", "x" * 100000))
        self.assertLess(len(self.client.published[-1][1]), 1000)
        self.assertEqual(self.gw.sent, [])

    def test_offsets_file_never_stops_startup(self):
        import os
        import tempfile
        from hisense_local import app
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "status_offsets.json")
            self.assertEqual(app.load_offsets(path), {})
            for content in ("{", "[1]", '{"offsets": 5}'):
                with open(path, "w") as fh:
                    fh.write(content)
                with self.assertLogs("app", "ERROR"):
                    self.assertEqual(app.load_offsets(path), {})
            good = {"minutes_left": 5, "DownLight": [8, 8, 8], "Selected_program_ID": {"at": 3, "map": {"26": 42}}}
            bad = {"a": "x", "b": [1, 2], "c": {"at": "3", "map": {}}, "d": -1, "e }}": 1, "f": {"at": 1, "map": {"1": []}}}
            with open(path, "w") as fh:
                json.dump({"offsets": dict(good, **bad)}, fh)
            with self.assertLogs("app", "WARNING"):
                self.assertEqual(app.load_offsets(path), good)

    async def test_reconnect_resets_ack_state_and_status_wins(self):
        await self.deliver(msg("hisense_local/set/DownLight", "1"))
        self.bridge.on_link(False)
        self.assertIsNone(self.client.last("hisense_local/state")["DownLight"])
        self.bridge.offsets = {"Selected_program_ID": 1, "minutes_left": 2}
        self.bridge.on_status([0, 11, 15])
        state = self.client.last("hisense_local/state")
        self.assertEqual(state["Selected_program_ID"], "Quick 15'")
        self.assertEqual(state["minutes_left"], 15)

    async def test_capture_label_and_got(self):
        self.bridge.capture = SimpleNamespace(next_label=None, running=True, armed=lambda: False)
        await self.deliver(msg("hisense_local/capture/next", "Steam=1"))
        self.assertEqual(self.bridge.capture.next_label, "Steam=1")
        self.bridge.capture_got("Steam=1", 7, "abcdef0123456789")
        self.assertEqual(self.client.last("hisense_local/capture/got"),
                         {"label": "Steam=1", "seq": 7, "session": "abcd"})
        self.assertEqual(self.bridge.state()["mode"], "capture running")


if __name__ == "__main__":
    unittest.main()


class LearnTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = StubClient()
        self.gw = StubGateway(fakes.FakeSession().session())

    async def test_learned_fields_are_saved_and_shown(self):
        from hisense_local import calibrate
        saved = []
        learner = calibrate.Learner({"minutes_left": [2, 3]})
        bridge = bridgelib.Bridge(self.client, asyncio.get_running_loop(), self.gw, offsets={"minutes_left": 2},
                                  learner=learner, save_offsets=lambda found, left: saved.append((dict(found), left)))
        for left in (15, 14, 13):
            bridge.on_status([67, 64, 15, left])
        self.assertEqual(bridge.offsets["minutes_left"], 3)
        self.assertEqual(saved, [({"minutes_left": 3}, {})])
        self.assertEqual(self.client.last("hisense_local/state")["minutes_left"], 13)


class ProfileTest(unittest.IsolatedAsyncioTestCase):
    async def test_built_in_profile_offers_the_status_sensors(self):
        from hisense_local import catalog
        bridge = bridgelib.Bridge(StubClient(), asyncio.get_running_loop(),
                                  StubGateway(fakes.FakeSession().session()),
                                  offsets=dict(catalog.PROFILES["WF5I1045BWQ"]))
        cmps = bridge.discovery()[0][1]["cmps"]
        self.assertEqual(cmps["minutes_left"]["p"], "sensor")
        self.assertEqual(cmps["machine_status"]["name"], "State")
