"""Unit tests for MCP status pill backend (issue #84)."""
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
import mcp_status as ms


class TestMcpStatusFile(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._prev = ms.STATUS_PATH
        ms.STATUS_PATH = os.path.join(self._tmpdir.name, "mcp_status.json")
        ms._IN_FLIGHT = 0

    def tearDown(self):
        ms.STATUS_PATH = self._prev
        ms._IN_FLIGHT = 0
        self._tmpdir.cleanup()

    def test_record_and_read_activity(self):
        ms.record_activity()
        ms.clear_activity()
        raw = ms.read_status()
        self.assertIn("last_activity_ts", raw)
        self.assertEqual(raw["total_requests"], 1)
        self.assertEqual(raw["in_flight"], 0)

    def test_in_flight_tracks_concurrent_calls(self):
        ms.record_activity()
        raw = ms.read_status()
        self.assertEqual(raw["in_flight"], 1)
        ms.clear_activity()
        self.assertEqual(ms.read_status()["in_flight"], 0)

    def test_read_missing_file_returns_empty(self):
        self.assertEqual(ms.read_status(), {})


class TestBuildMcpStatus(unittest.TestCase):
    def test_disabled_returns_off(self):
        with patch.object(app, "_mcp_enabled", return_value=False):
            out = app.build_mcp_status()
        self.assertEqual(out, {"enabled": False, "state": "off"})

    @patch.object(app, "_mcp_probe", return_value=False)
    @patch.object(app, "_mcp_enabled", return_value=True)
    @patch.object(app, "_mcp_port", return_value=9810)
    @patch.object(ms, "read_status", return_value={})
    def test_unreachable_is_down(self, *_mocks):
        out = app.build_mcp_status()
        self.assertEqual(out["state"], "down")
        self.assertFalse(out["up"])

    @patch.object(app, "_mcp_probe", return_value=True)
    @patch.object(app, "_mcp_enabled", return_value=True)
    @patch.object(app, "_mcp_port", return_value=9810)
    @patch.object(ms, "read_status", return_value={"in_flight": 2, "last_activity_ts": time.time()})
    def test_in_flight_is_active(self, *_mocks):
        out = app.build_mcp_status()
        self.assertEqual(out["state"], "active")
        self.assertEqual(out["active_requests"], 2)

    @patch.object(app, "_mcp_probe", return_value=True)
    @patch.object(app, "_mcp_enabled", return_value=True)
    @patch.object(app, "_mcp_port", return_value=9810)
    @patch.object(ms, "read_status", return_value={"last_activity_ts": time.time() - 10})
    def test_recent_activity_is_active(self, *_mocks):
        out = app.build_mcp_status()
        self.assertEqual(out["state"], "active")
        self.assertLessEqual(out["last_activity_age_s"], 45)

    @patch.object(app, "_mcp_probe", return_value=True)
    @patch.object(app, "_mcp_enabled", return_value=True)
    @patch.object(app, "_mcp_port", return_value=9810)
    @patch.object(ms, "read_status", return_value={"last_activity_ts": time.time() - 120})
    def test_stale_activity_is_idle(self, *_mocks):
        out = app.build_mcp_status()
        self.assertEqual(out["state"], "idle")

    @patch.object(app, "_mcp_probe", return_value=True)
    @patch.object(app, "_mcp_enabled", return_value=True)
    @patch.object(app, "_mcp_port", return_value=9810)
    @patch.object(ms, "read_status", return_value={})
    def test_no_activity_up_is_idle(self, *_mocks):
        out = app.build_mcp_status()
        self.assertEqual(out["state"], "idle")

    @patch.object(app, "_mcp_probe", return_value=True)
    @patch.object(app, "_mcp_enabled", return_value=True)
    @patch.object(app, "_mcp_port", return_value=9810)
    @patch.object(ms, "read_status", return_value={})
    def test_missing_status_file_is_idle_not_error(self, *_mocks):
        out = app.build_mcp_status()
        self.assertEqual(out["state"], "idle")
        self.assertTrue(out["up"])


if __name__ == "__main__":
    unittest.main()
