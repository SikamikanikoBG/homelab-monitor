"""A down model server logs once, not on every probe (#371)."""
import contextlib
import io
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: F401  (backend.probes resolves helpers through it)
from backend import probes

REFUSED = ConnectionRefusedError(111, "Connection refused")


class TestProbeLogState(unittest.TestCase):
    def setUp(self):
        getattr(probes, "_PROBE_DOWN", set()).clear()

    def lines(self, fn, n=5):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for _ in range(n):
                fn()
        return out.getvalue().splitlines()

    def test_down_wyoming_logs_one_line(self):
        with patch("backend.probes.socket.create_connection", side_effect=REFUSED):
            got = self.lines(lambda: probes.probe_wyoming("10.0.0.9"))
        self.assertEqual(len(got), 1, got)
        self.assertIn("10.0.0.9", got[0])

    def test_two_down_hosts_log_one_line_each(self):
        with patch("backend.probes.socket.create_connection", side_effect=REFUSED):
            got = self.lines(lambda: (probes.probe_wyoming("10.0.0.9"), probes.probe_wyoming("10.0.0.8")))
        self.assertEqual(len(got), 2, got)

    def test_down_custom_server_logs_again_only_after_a_recovery(self):
        desc = {"name": "lm", "host": "10.0.0.9", "port": 1234, "provider": "vllm"}
        probe = lambda: probes.probe_custom_server(desc)
        with patch("backend.probes._probe_for_provider", side_effect=REFUSED):
            self.assertEqual(len(self.lines(probe)), 1)
        with patch("backend.probes._probe_for_provider", return_value=[("m", None)]):
            got = self.lines(probe)
            self.assertEqual(len(got), 1, got)
            self.assertIn("answering again", got[0])
        with patch("backend.probes._probe_for_provider", side_effect=REFUSED):
            self.assertEqual(len(self.lines(probe)), 1)

    def test_healthy_server_logs_nothing(self):
        desc = {"name": "lm", "host": "10.0.0.9", "port": 1234, "provider": "vllm"}
        with patch("backend.probes._probe_for_provider", return_value=[("m", None)]):
            self.assertEqual(self.lines(lambda: probes.probe_custom_server(desc)), [])

    def test_down_container_server_logs_one_line(self):
        def refuse(ip):
            raise REFUSED
        with patch("backend.probes._match_probe", return_value=refuse):
            got = self.lines(lambda: probes.probe_models({"name": "ollama", "ip": "172.17.0.2"}))
        self.assertEqual(len(got), 1, got)


if __name__ == "__main__":
    unittest.main()
