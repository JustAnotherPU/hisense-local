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
