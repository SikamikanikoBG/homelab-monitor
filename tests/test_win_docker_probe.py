"""Unit tests for the Windows-host Docker capability check in probe_host().

Regression coverage for a real bug: a Docker Desktop client/engine API-version
mismatch (a genuine, live "daemon is unhealthy" condition) prints an error
message containing the word "error" — which used to get misclassified as
"Docker CLI not on PATH" (status: info, "not installed") instead of "CLI
present, daemon not responding" (status: warn). All SSH calls are mocked —
no real network in CI."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _add_host(name="work", ssh_target="user@1.2.3.4"):
    with app.LOCK:
        app.DB.execute("DELETE FROM hosts WHERE name=?", (name,))
        app.DB.execute("INSERT INTO hosts(name, ssh_target, added_at) VALUES(?,?,0)",
                       (name, ssh_target))
        app.DB.commit()


class TestWindowsDockerProbe(unittest.TestCase):
    def setUp(self):
        _add_host()

    def tearDown(self):
        with app.LOCK:
            app.DB.execute("DELETE FROM hosts WHERE name=?", ("work",))
            app.DB.commit()

    def _run_probe(self, docker_stdout):
        """Drive probe_host() down the Windows branch with a canned response
        for the Docker capability check specifically."""
        with patch.object(app, "_tcp_probe", return_value=(True, None)), \
             patch.object(app, "_ensure_ssh_keypair"), \
             patch.object(app, "_detect_os", return_value={"family": "windows", "label": "Windows 11"}), \
             patch.object(app, "_ssh", return_value=(0, "ok", "", 5)), \
             patch.object(app, "_ssh_with_stdin") as m:
            def side_effect(user, host, port, cmd, stdin_bytes, timeout=60):
                if b"docker" in stdin_bytes:
                    return (0, docker_stdout, "", 5)
                if b"LastBootUpTime" in stdin_bytes:
                    return (0, "up=123", "", 5)
                if b"nvidia-smi" in stdin_bytes:
                    return (0, "missing", "", 5)
                return (0, "", "", 5)
            m.side_effect = side_effect
            return app.probe_host("work")

    def _docker_check(self, result):
        return next(c for c in result["checks"] if c["id"] == "docker")

    def test_daemon_error_is_warn_not_absent(self):
        """The exact real-world failure: CLI present, daemon returns an API
        version-mismatch error containing the word 'error'. Must NOT be
        reported as 'Docker CLI not on PATH'."""
        dk = self._docker_check(self._run_probe(
            "DOCKER_ERR:request returned 500 Internal Server Error for API route "
            "and version http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine/v1.54/version, "
            "check if the server supports the requested API version"))
        self.assertEqual(dk["status"], "warn")
        self.assertIn("daemon didn't respond cleanly", dk["detail"])
        self.assertIn("remedy", dk)

    def test_cli_absent_is_info(self):
        dk = self._docker_check(self._run_probe("DOCKER_ABSENT"))
        self.assertEqual(dk["status"], "info")
        self.assertIn("not found on PATH", dk["detail"])

    def test_cli_ok_reports_server_version(self):
        dk = self._docker_check(self._run_probe("DOCKER_OK:27.3.1"))
        self.assertEqual(dk["status"], "ok")
        self.assertIn("27.3.1", dk["detail"])


if __name__ == "__main__":
    unittest.main()
