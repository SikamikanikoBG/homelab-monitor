"""The fleet 'online' flag must not flap. A healthy host that misses one slow or
timed-out poll cycle (or that legitimately needs a long probe budget) should stay
'online' until a *sustained* gap of missed samples — never blink offline between
refreshes. Regression guard for the overview summary flapping bug."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class TestHostOnlineWindow(unittest.TestCase):
    def test_window_is_generous_for_fast_host(self):
        # A fast host (default budget) still gets several missed cycles of grace.
        with patch.object(app, "_host_poll_state", return_value=(app.HOST_POLL_TIMEOUT, 0)):
            w = app._host_online_window("fast")
        self.assertGreaterEqual(w, app.INTERVAL * 6)

    def test_window_scales_with_learned_timeout(self):
        # A host that learned a long budget gets a proportionally longer window,
        # so its own slow probe cycles never read as offline.
        with patch.object(app, "_host_poll_state", return_value=(90, 0)):
            w = app._host_online_window("slow")
        self.assertGreaterEqual(w, (90 + app.INTERVAL) * 2)


class TestHostIsOnline(unittest.TestCase):
    def _entry(self, age_sec, window=None, data=True):
        e = {"at": int(app.time.time()) - age_sec}
        if data:
            e["data"] = {"host": {}}
        if window is not None:
            e["window"] = window
        return e

    def test_no_entry_is_offline(self):
        self.assertFalse(app._host_is_online(None))
        self.assertFalse(app._host_is_online({}))

    def test_entry_without_data_is_offline(self):
        self.assertFalse(app._host_is_online(self._entry(1, data=False)))

    def test_fresh_sample_is_online(self):
        self.assertTrue(app._host_is_online(self._entry(2, window=60)))

    def test_one_missed_cycle_stays_online(self):
        # A single slow/timed-out cycle (~INTERVAL*2 old) must NOT flip to offline —
        # this is exactly the flap the old INTERVAL*3 (30s) cutoff caused.
        self.assertTrue(app._host_is_online(self._entry(app.INTERVAL * 3 + 1, window=60)))

    def test_sustained_gap_is_offline(self):
        self.assertFalse(app._host_is_online(self._entry(120, window=60)))

    def test_missing_window_falls_back_to_generous_default(self):
        # Old cache rows written before this field existed still get hysteresis.
        e = self._entry(app.INTERVAL * 3 + 1)
        e.pop("window", None)
        self.assertTrue(app._host_is_online(e))


if __name__ == "__main__":
    unittest.main()
