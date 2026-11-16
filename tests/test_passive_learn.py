import json
import tempfile
import unittest

from hisense_local import learn as learnlib
from hisense_local import session as sessionlib
from hisense_local import web
from hisense_local import wire
from hisense_local.passive_learn import READ_ONLY, Labeler


def frame(seq):
    body = json.dumps({"id": 201, "seq": seq, "com": "AAAA", "cmdversion": -1}).encode()
    return (b"HS" + body + b"ab01" * 16 + b"\r\n").hex()


class LabelerTest(unittest.TestCase):
    # specs covering every kind the real catalog uses, read with the real field_value
    SPECS = {
        "temperature": 19,                                       # plain byte
        "Selected_program_ID": {"at": 18, "map": {"27": 44, "33": 52}},  # index map
        "Prewash": [22, 16, 16],                                 # flag bit of byte 22
        "machine_status": 3,                                     # READ-ONLY, must be ignored
        "minutes_left": 29,                                      # READ-ONLY, must be ignored
    }

    def status(self, **over):
        v = [0] * 32
        v[18], v[19], v[22], v[3], v[29] = 27, 4, 0, 32, 90
        for key, idx in {"prog": 18, "temp": 19, "prewash": 22, "status": 3, "min": 29}.items():
            if key in over:
                v[idx] = over[key]
        return v

    def labeler(self):
        return Labeler(self.SPECS, wire.field_value)

    def test_names_a_plain_byte_change_ignoring_side_effects(self):
        lab = self.labeler()
        lab.on_status(self.status())
        lab.on_command(1)
        lab.on_status(self.status(temp=6, status=67, min=80))  # temp + read-only side effects
        self.assertEqual(lab.labels, {"1": "temperature=6"})

    def test_names_an_index_mapped_program(self):
        lab = self.labeler()
        lab.on_status(self.status())
        lab.on_command(2)
        lab.on_status(self.status(prog=33, status=67))
        self.assertEqual(lab.labels["2"], "Selected_program_ID=52")

    def test_names_a_flag_option(self):
        lab = self.labeler()
        lab.on_status(self.status())
        lab.on_command(3)
        lab.on_status(self.status(prewash=15))  # (15+1)&16 == 16 -> flag on
        self.assertEqual(lab.labels["3"], "Prewash=1")

    def test_read_only_only_change_is_not_named(self):
        lab = self.labeler()
        lab.on_status(self.status())
        lab.on_command(4)
        lab.on_status(self.status(status=64, min=70))
        self.assertNotIn("4", lab.labels)
        self.assertIn("4", lab.unresolved)

    def test_two_settable_changes_are_ambiguous(self):
        lab = self.labeler()
        lab.on_status(self.status())
        lab.on_command(5)
        lab.on_status(self.status(prog=33, temp=9))
        self.assertNotIn("5", lab.labels)
        self.assertIn("several properties", lab.unresolved["5"])

    def test_read_only_specs_dropped_at_construction(self):
        lab = self.labeler()
        self.assertEqual(set(lab.specs), {"temperature", "Selected_program_ID", "Prewash"})
        self.assertTrue(READ_ONLY & {"machine_status", "minutes_left"})

    def test_command_before_baseline_is_not_left_pending(self):
        lab = self.labeler()
        lab.on_command(9)
        lab.on_status(self.status())
        self.assertEqual(lab.labels, {})
        self.assertIsNone(lab.pending)
        self.assertIn("9", lab.unresolved)


class RelabelTest(unittest.TestCase):
    def test_relabel_keeps_only_matching_201_frames(self):
        record = {"id": "s", "wid": "w", "recorded_at": 1.0,
                  "login": {"101": "aa", "102": "bb", "103": "cc"},
                  "frames": {"unlabelled_10": frame(10), "unlabelled_11": frame(11),
                             "unlabelled_12": frame(999)}}  # 12's seq mismatches -> dropped
        labels = {"10": "temperature=6", "11": "Selected_program_ID=52", "12": "x=1"}
        out = learnlib.relabel(record, labels)
        self.assertEqual(set(out["frames"]), {"temperature=6", "Selected_program_ID=52"})
        self.assertEqual(out["state"], "ready")
        self.assertEqual(len(out["login"]), 3)

    def test_should_promote_only_a_strictly_richer_session(self):
        self.assertTrue(learnlib.should_promote(5, 3))
        self.assertFalse(learnlib.should_promote(3, 3))
        self.assertFalse(learnlib.should_promote(2, 4))


class LearnPromotionTest(unittest.TestCase):
    def test_richer_session_is_promoted_thinner_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            data = sessionlib.DataDir(d)
            specs = {"Selected_program_ID": {"at": 0, "map": {"27": 44, "33": 52}},
                     "temperature": 1}
            learn = learnlib.Learn(data, specs)
            learn.on_status([27, 4])
            learn.on_cmd(100); learn.on_status([33, 4])   # program -> 52
            learn.on_cmd(101); learn.on_status([33, 6])   # temperature 4->6
            rec = {"id": "a1b2c3d4e5", "wid": "w", "recorded_at": 1.0,
                   "login": {"101": "aa", "102": "bb", "103": "cc"},
                   "frames": {"unlabelled_100": frame(100), "unlabelled_101": frame(101)}}
            learn.on_close(rec)
            active = data.load_active("")
            self.assertIsNotNone(active)
            self.assertEqual(set(active.labels), {"Selected_program_ID=52", "temperature=6"})

            # a later, thinner session must not replace it
            learn2 = learnlib.Learn(data, specs)
            learn2.on_status([27, 4])
            learn2.on_cmd(200); learn2.on_status([27, 7])
            rec2 = {"id": "ffffffffff", "wid": "w", "recorded_at": 2.0,
                    "login": {"101": "aa", "102": "bb", "103": "cc"},
                    "frames": {"unlabelled_200": frame(200)}}
            learn2.on_close(rec2)
            self.assertEqual(data.active_id(), "a1b2c3d4e5")


class WebStatusTest(unittest.TestCase):
    def test_build_status_shapes_each_mode(self):
        s = web.build_status({"frames": 12, "session": "a1b2"}, washer_online=True, learn_on=True)
        self.assertEqual((s["mode"], s["commands"], s["online"]), ("learn", 12, True))
        self.assertIn("Learning", s["headline"])

        s = web.build_status({"frames": 12}, washer_online=False, learn_on=True)
        self.assertIn("Waiting", s["headline"])

        s = web.build_status({"frames": 8}, washer_online=True, learn_on=False)
        self.assertEqual((s["mode"], s["headline"]), ("local", "Local control active"))

        s = web.build_status({"frames": 0}, washer_online=False, learn_on=False)
        self.assertEqual(s["mode"], "empty")

        s = web.build_status({}, washer_online=False, learn_on=False)
        self.assertEqual((s["commands"], s["mode"]), (0, "empty"))


if __name__ == "__main__":
    unittest.main()
