import unittest

import tests  # noqa: F401  (puts addon/ on the path)
from hisense_local import calibrate

CANDIDATES = {"minutes_left": [2, 3],
              "machine_status": [{"at": 0, "map": {"32": "ready", "67": "running", "0": "stopped"}},
                                 {"at": 1, "map": {"127": "ready", "64": "running", "74": "stopped"}}]}


def row(state, other, left, total):
    return [state, other, left, total]


class LearnerTest(unittest.TestCase):
    def test_countdown_settles_time_left_then_state(self):
        learner = calibrate.Learner(CANDIDATES, clock=2)
        self.assertEqual(learner.feed(row(67, 64, 15, 15)), {})
        self.assertEqual(learner.feed(row(67, 64, 15, 14)), {})  # one tick: not enough
        self.assertEqual(learner.feed(row(67, 80, 15, 14)), {})  # the other byte moves, no tick
        got = learner.feed(row(67, 80, 15, 13))
        self.assertEqual(got["minutes_left"], 3)
        self.assertEqual(got["machine_status"]["at"], 0)  # byte 1 no longer reads "running"
        self.assertEqual(learner.remaining(), {})

    def test_identical_copies_settle_on_the_first(self):
        learner = calibrate.Learner(CANDIDATES)
        for left in (15, 14, 13):
            got = learner.feed(row(67, 64, left, left))
        self.assertEqual(got, {"minutes_left": 2})  # both states still read "running"
        self.assertEqual(learner.remaining()["machine_status"], CANDIDATES["machine_status"])

    def test_copies_moving_while_not_running_is_not_a_tick(self):
        learner = calibrate.Learner(CANDIDATES)
        for left in (15, 14, 13, 12):
            self.assertEqual(learner.feed(row(32, 127, left, left)), {})  # dial turned at rest
        self.assertEqual(learner.remaining()["minutes_left"], [2, 3])

    def test_copies_that_disagree_are_not_a_tick(self):
        learner = calibrate.Learner(CANDIDATES)
        learner.feed(row(67, 64, 20, 15))
        self.assertEqual(learner.feed(row(67, 64, 19, 14)), {})
        self.assertEqual(learner.feed(row(67, 64, 18, 13)), {})

    def test_state_picked_at_the_end_of_the_cycle(self):
        learner = calibrate.Learner({"machine_status": CANDIDATES["machine_status"]}, clock=3)
        learner.feed(row(67, 64, 0, 3))
        self.assertEqual(learner.feed(row(67, 64, 0, 2)), {})
        got = learner.feed(row(67, 64, 0, 1))
        self.assertEqual(got, {"machine_status": CANDIDATES["machine_status"][0]})  # 3 names, lowest offset
        self.assertEqual(learner.remaining(), {})

    def test_settled_clock_still_settles_the_state(self):
        learner = calibrate.Learner({"machine_status": CANDIDATES["machine_status"]}, clock=3)
        learner.feed(row(67, 64, 15, 15))
        self.assertEqual(learner.feed(row(67, 90, 15, 14)), {"machine_status": CANDIDATES["machine_status"][0]})

    def test_bad_candidates_are_dropped(self):
        learner = calibrate.Learner({"minutes_left": ["x", -1], "machine_status": [{"at": "1"}, 5]})
        self.assertEqual(learner.remaining(), {})
        self.assertEqual(learner.feed([1, 2, 3]), {})


class StateCandidatesTest(unittest.TestCase):
    def test_from_a_cycle_timeline(self):
        timeline = [{"label": "Actions=2", "result": "ok"}, {"values": [67, 9, 0]}, {"values": [67, 9, 0]},
                    {"label": "Actions=1", "result": "ok"}, {"values": [0, 9, 168]}]
        timeline.insert(0, {"values": [32, 9, 168]})
        hits = calibrate.state_candidates(timeline)
        self.assertEqual(hits[0], {"at": 0, "map": {"0": "stopped", "32": "ready", "67": "running"}})
        self.assertEqual(hits[1], {"at": 2, "map": {"168": "ready", "0": "running"}})  # back to rest
        self.assertEqual(len(hits), 2)  # byte 1 never changed
