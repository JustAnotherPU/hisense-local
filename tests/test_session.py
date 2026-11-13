import json
import os
import stat
import tempfile
import unittest

from hisense_local import session as sessionlib
from tests import fakes


def load(path):
    with open(path) as fh:
        return json.load(fh)


def dump(path, data):
    with open(path, "w") as fh:
        json.dump(data, fh)


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = fakes.FakeSession()

    def tearDown(self):
        self.tmp.cleanup()

    def test_import(self):
        paths = self.fake.write_old_format(self.tmp.name, extra_foreign=3)
        sess, report = sessionlib.import_capture_files(paths["login"], paths["commands"], paths["library"])
        self.assertEqual(sess.id, self.fake.sid)
        self.assertEqual(report["imported"], len(self.fake.frames))
        self.assertEqual(report["frames_of_session"], len(self.fake.frames))
        self.assertEqual(report["other_session"], ["Steam=1"])
        self.assertEqual(report["unsupported"], ["PreSoak=1"])
        # START/PAUSE/RESUME/STOP come out under Prop=value names
        for label in ("Actions=2", "Actions=1", "Remote_hold=1", "Remote_hold=0"):
            self.assertEqual(sess.frame(label), self.fake.frames[label])
        self.assertNotIn("START", sess.labels)
        self.assertEqual(sess.login_reply(102), self.fake.login["102"])

    def test_library_of_other_session_refused(self):
        paths = self.fake.write_old_format(self.tmp.name)
        lib = load(paths["library"])
        lib["key"] = "0" * 16
        dump(paths["library"], lib)
        with self.assertRaises(sessionlib.SessionError):
            sessionlib.import_capture_files(paths["login"], paths["commands"], paths["library"])

    def test_pointer_to_wrong_seq_dropped(self):
        paths = self.fake.write_old_format(self.tmp.name)
        items = load(paths["commands"])
        first = next(iter(items["items"].values()))
        first["raw"] = fakes.line({"id": 201, "seq": 1, "com": "x"}).hex()
        dump(paths["commands"], items)
        _, report = sessionlib.import_capture_files(paths["login"], paths["commands"], paths["library"])
        self.assertEqual(len(report["not_a_matching_201"]), 1)


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = sessionlib.DataDir(os.path.join(self.tmp.name, "data"))
        self.fake = fakes.FakeSession()

    def tearDown(self):
        self.tmp.cleanup()

    def test_modes_and_round_trip(self):
        self.data.save(self.fake.session())
        path = self.data.path(self.fake.sid)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.data.root).st_mode), 0o700)
        loaded = sessionlib.Session.load(path)
        self.assertEqual(loaded.frame("Actions=2"), self.fake.frames["Actions=2"])
        self.assertIsNone(loaded.frame("Steam=1"))

    def test_corrupt_file(self):
        path = os.path.join(self.tmp.name, "bad.json")
        for content in ("{", '{"id": "x"}', json.dumps({"id": "a", "wid": "b", "frames": {},
                                                         "login": {"101": "zz"}})):
            with open(path, "w") as fh:
                fh.write(content)
            with self.assertRaises(sessionlib.SessionError) as ctx:
                sessionlib.Session.load(path)
            self.assertIn("unreadable", str(ctx.exception))

    def test_wrong_types_refused(self):
        good = self.fake.session().data
        path = os.path.join(self.tmp.name, "odd.json")
        for change in ({"wid": None}, {"wid": ""}, {"id": 123}, {"id": "../x"}, {"frames": ["a"]},
                       {"frames": {"Actions=2": "zz"}}, {"frames": {"Actions=2": 5}}, {"login": []}):
            dump(path, dict(good, **change))
            with self.assertRaises(sessionlib.SessionError, msg=change):
                sessionlib.Session.load(path)
        dump(path, [1])
        with self.assertRaises(sessionlib.SessionError):
            sessionlib.Session.load(path)
        dump(path, dict(good, frames=dict(good["frames"], Steam=None)))
        self.assertIsNone(sessionlib.Session.load(path).frame("Steam"))

    def test_adopt_import_moves_and_activates(self):
        src = os.path.join(self.tmp.name, "session.json")
        sessionlib.write_private(src, self.fake.session().data)
        self.assertEqual(self.data.adopt_import(src), self.fake.sid)
        self.assertFalse(os.path.exists(src))
        self.assertEqual(self.data.load_active().id, self.fake.sid)
        # a second import does not steal the active pointer
        other = fakes.FakeSession()
        sessionlib.write_private(src, other.session().data)
        self.data.adopt_import(src)
        self.assertEqual(self.data.active_id(), self.fake.sid)
        self.assertEqual(self.data.active_id(other.sid), other.sid)
        self.assertIsNone(self.data.adopt_import(src))

    def test_bad_id_rejected(self):
        with self.assertRaises(sessionlib.SessionError):
            self.data.path("../../etc/passwd")


if __name__ == "__main__":
    unittest.main()


class ActivePrefixTest(unittest.TestCase):
    def test_unique_prefix_resolves_and_ambiguous_does_not(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            data = sessionlib.DataDir(tmp)
            for sid in ("abcd1111", "abcd2222", "ef001234"):
                open(os.path.join(data.sessions, sid + ".json"), "w").close()
            self.assertEqual(data.active_id("EF00"), "ef001234")
            self.assertEqual(data.active_id("abcd"), "abcd")  # two matches: left as given, load fails
            self.assertTrue(data.activate_if_none("ef001234"))
            self.assertFalse(data.activate_if_none("abcd1111"))
            self.assertEqual(data.active_id(), "ef001234")
