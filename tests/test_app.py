import json
import os
import tempfile
import types
import unittest
from unittest import mock

import tests  # noqa: F401  (puts addon/ on the path)
from hisense_local import app


class CalibrateOnceTest(unittest.TestCase):
    """calibrate left on must not step the washer again at the next start."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.args = types.SimpleNamespace(data=self.tmp.name, options=os.path.join(self.tmp.name, "o.json"))
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, calibrate):
        with open(self.args.options, "w") as fh:
            json.dump({"calibrate": calibrate}, fh)

        async def cal(args, opts):
            self.calls.append("calibrate")

        async def serve(args, opts):
            self.calls.append("serve")

        with mock.patch.object(app, "calibrate_async", cal), mock.patch.object(app, "run_async", serve):
            app.run(self.args)

    def test_runs_once_then_serves_until_switched_off_and_on(self):
        self.start(True)
        self.start(True)  # e.g. a host reboot with the option still on
        self.start(False)
        self.start(True)
        self.assertEqual(self.calls, ["calibrate", "serve", "serve", "calibrate"])


class PickOffsetsTest(unittest.TestCase):
    def test_profile_only_without_a_calibration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "status_offsets.json")
            got = app.pick_offsets(path, "WF5I1045BWQ")
            self.assertEqual(got["minutes_left"], 29)
            self.assertEqual(got["machine_status"]["at"], 92)
            self.assertEqual(len(got), len(app.catalog.PROFILES["WF5I1045BWQ"]))  # every entry is valid
            self.assertEqual(app.pick_offsets(path, "other"), {})
            with open(path, "w") as fh:
                json.dump({"offsets": {"temperature": 7}}, fh)
            self.assertEqual(app.pick_offsets(path, "WF5I1045BWQ"), {"temperature": 7})


class OpenLearnerTest(unittest.TestCase):
    def test_rebuilds_candidates_only_from_the_matching_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "status_offsets.json")
            with open(path, "w") as fh:
                json.dump({"offsets": {"minutes_left": 2}, "at": 100.0}, fh)
            report = {"started": 100.0, "steps": [], "timeline": [
                {"values": [32, 127]}, {"label": "Actions=2", "result": "ok"}, {"values": [67, 64]},
                {"label": "Actions=1", "result": "ok"}, {"values": [0, 74]}]}
            with open(os.path.join(tmp, "calibration.json"), "w") as fh:
                json.dump(report, fh)
            learner = app.open_learner(path, tmp, {"minutes_left": 2})
            self.assertEqual(learner.remaining()["machine_status"][0]["at"], 0)
            report["started"] = 99.0  # a report from another run says nothing about these offsets
            with open(os.path.join(tmp, "calibration.json"), "w") as fh:
                json.dump(report, fh)
            self.assertIsNone(app.open_learner(path, tmp, {"minutes_left": 2}))
            with open(os.path.join(tmp, "calibration.json"), "w") as fh:
                fh.write("{broken")
            self.assertIsNone(app.open_learner(path, tmp, {}))
