"""Window retries run until time is up, unless another snipe waits behind."""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from game import csnipe  # noqa: E402


class StopRetryingTest(unittest.TestCase):
    def test_alone_keeps_going_past_the_budget(self):
        self.assertIsNone(csnipe._stop_retrying(
            csnipe.MAX_SEND_ATTEMPTS + 30, 16 * 60 * 1000, False))

    def test_out_of_time_stops(self):
        self.assertEqual(csnipe._stop_retrying(
            2, csnipe.RETRY_MIN_LEAD_MS - 1, False), "out of time")

    def test_budget_applies_when_another_snipe_waits(self):
        self.assertIsNone(csnipe._stop_retrying(
            csnipe.MAX_SEND_ATTEMPTS - 1, 16 * 60 * 1000, True))
        self.assertEqual(csnipe._stop_retrying(
            csnipe.MAX_SEND_ATTEMPTS, 16 * 60 * 1000, True),
            "another snipe is waiting")


class OthersWaitingTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.now = time.time()

    def tearDown(self):
        os.remove(self.path)

    def _write(self, commands):
        with open(self.path, "w") as f:
            json.dump({"commands": commands}, f)

    def test_alone(self):
        self._write([{"id": "a", "status": "running", "claimed_at": self.now}])
        self.assertFalse(csnipe._others_waiting("a", self.path, self.now))

    def test_finished_and_future_snipes_do_not_count(self):
        self._write([
            {"id": "a", "status": "running", "claimed_at": self.now},
            {"id": "b", "status": "done"},
            {"id": "c", "status": "armed", "start_ts": self.now + 600},
            {"id": "d", "status": "running",
             "claimed_at": self.now - csnipe.STALE_RUNNING_SECONDS - 5},
        ])
        self.assertFalse(csnipe._others_waiting("a", self.path, self.now))

    def test_claimed_in_same_batch(self):
        self._write([
            {"id": "a", "status": "running", "claimed_at": self.now},
            {"id": "b", "status": "running", "claimed_at": self.now},
        ])
        self.assertTrue(csnipe._others_waiting("a", self.path, self.now))

    def test_armed_and_due(self):
        self._write([
            {"id": "a", "status": "running", "claimed_at": self.now},
            {"id": "b", "status": "armed", "start_ts": self.now - 1},
        ])
        self.assertTrue(csnipe._others_waiting("a", self.path, self.now))


if __name__ == "__main__":
    unittest.main()
