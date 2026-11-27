"""Unit tests for the opt-in one-click self-update (feat/self-update).

Everything that would touch real Docker (_docker_req) is stubbed, so these tests
never create a container. We drive start_self_update() and the two routes through
the Flask test client, overriding ALLOW_SELF_UPDATE / collect_update / _data_dir.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


# A docker inspect payload with the compose labels start_self_update() needs.
def _inspect_ok():
    return (200, json.dumps({
        "Image": "sikamikaniko123/homelab-monitor:0.16.0",
        "Config": {"Labels": {
            "com.docker.compose.project": "homelab",
            "com.docker.compose.project.config_files": "/srv/homelab/docker-compose.yml",
            "com.docker.compose.project.working_dir": "/srv/homelab",
            "com.docker.compose.service": "homelab-monitor",
        }},
    }).encode())


def _docker_stub(method, path, body=None, timeout=8):
    """Stand-in for app._docker_req covering inspect/create/start."""
    if method == "GET" and "/json" in path:
        return _inspect_ok()
    if path.startswith("/images/create"):
        # Auto-pull of the helper image: emulate a successful streamed pull.
        return (200, b'{"status":"Status: Image is up to date"}')
    if path == "/containers/create":
        return (201, b'{"Id":"deadbeef"}')
    if path.endswith("/start"):
        return (204, b"")
    return (404, b"")


class SelfUpdateBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.datadir = self._tmp.name
        # Point the data dir (where state/log live) at a temp dir.
        self._p_dir = patch.object(app, "_data_dir", return_value=self.datadir)
        self._p_dir.start()
        # Default: enabled + an update available, unless a test overrides it.
        self._p_flag = patch.object(app, "ALLOW_SELF_UPDATE", True)
        self._p_flag.start()
        self._p_upd = patch.object(app, "collect_update",
                                   return_value={"available": True, "current": "0.16.0", "latest": "0.17.0"})
        self._p_upd.start()
        self._p_req = patch.object(app, "_docker_req", side_effect=_docker_stub)
        self._p_req.start()

    def tearDown(self):
        patch.stopall()
        self._tmp.cleanup()

    def _state_path(self):
        return os.path.join(self.datadir, "update_state.json")

    def _write_state(self, state, age_sec=0):
        with open(self._state_path(), "w") as f:
            json.dump({"state": state, "target": "0.17.0",
                       "updated_at": int(time.time()) - age_sec}, f)


class TestStartGating(SelfUpdateBase):
    def test_disabled_returns_400(self):
        with patch.object(app, "ALLOW_SELF_UPDATE", False):
            code, payload = app.start_self_update()
        self.assertEqual(code, 400)
        self.assertFalse(payload["ok"])

    def test_no_update_returns_400(self):
        with patch.object(app, "collect_update",
                          return_value={"available": False, "current": "0.17.0"}):
            code, payload = app.start_self_update()
        self.assertEqual(code, 400)
        self.assertFalse(payload["ok"])

    def test_fresh_job_returns_409(self):
        self._write_state("restarting", age_sec=10)
        code, payload = app.start_self_update()
        self.assertEqual(code, 409)
        self.assertFalse(payload["ok"])

    def test_stale_job_is_allowed(self):
        # Older than 15 min → treated as abandoned, a new run is permitted.
        self._write_state("restarting", age_sec=20 * 60)
        code, payload = app.start_self_update()
        self.assertEqual(code, 202)
        self.assertTrue(payload["ok"])

    def test_terminal_job_does_not_block(self):
        self._write_state("done", age_sec=10)
        code, payload = app.start_self_update()
        self.assertEqual(code, 202)
        self.assertTrue(payload["ok"])

    def test_missing_compose_labels_returns_400(self):
        def _no_labels(method, path, body=None, timeout=8):
            if method == "GET" and "/json" in path:
                return (200, json.dumps({"Image": "img", "Config": {"Labels": {}}}).encode())
            return _docker_stub(method, path, body, timeout)
        with patch.object(app, "_docker_req", side_effect=_no_labels):
            code, payload = app.start_self_update()
        self.assertEqual(code, 400)
        self.assertIn("compose", payload["error"].lower())

    def test_success_writes_state_and_truncates_log(self):
        code, payload = app.start_self_update()
        self.assertEqual(code, 202)
        st = json.load(open(self._state_path()))
        self.assertEqual(st["state"], "starting")
        self.assertEqual(st["target"], "0.17.0")
        self.assertTrue(os.path.exists(os.path.join(self.datadir, "update.log")))


class TestRoutes(SelfUpdateBase):
    def setUp(self):
        super().setUp()
        self.client = app.app.test_client()

    def test_post_disabled_400(self):
        with patch.object(app, "ALLOW_SELF_UPDATE", False):
            rv = self.client.post("/api/update/app", json={})
        self.assertEqual(rv.status_code, 400)
        self.assertFalse(rv.get_json()["ok"])

    def test_post_no_update_400(self):
        with patch.object(app, "collect_update",
                          return_value={"available": False, "current": "0.17.0"}):
            rv = self.client.post("/api/update/app", json={})
        self.assertEqual(rv.status_code, 400)

    def test_post_already_running_409(self):
        self._write_state("restarting", age_sec=5)
        rv = self.client.post("/api/update/app", json={})
        self.assertEqual(rv.status_code, 409)

    def test_post_starts_202(self):
        rv = self.client.post("/api/update/app", json={})
        self.assertEqual(rv.status_code, 202)
        self.assertTrue(rv.get_json()["ok"])

    def test_status_idle_when_no_file(self):
        rv = self.client.get("/api/update/app/status")
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.get_json()["state"], "idle")

    def test_status_returns_state_file(self):
        self._write_state("done", age_sec=1)
        with open(os.path.join(self.datadir, "update.log"), "w") as f:
            f.write("line1\nline2\n")
        rv = self.client.get("/api/update/app/status")
        body = rv.get_json()
        self.assertEqual(body["state"], "done")
        self.assertEqual(body["target"], "0.17.0")
        self.assertIn("line2", body["log"])


class TestHealthGate(SelfUpdateBase):
    """The helper's health gate must poll the open /healthz (no login), not
    /api/health (401 when an admin password is set), and read `version`."""

    TARGET = "0.17.0"

    def _script(self):
        return app._self_update_script(
            "/srv/homelab", "homelab", ["/srv/homelab/docker-compose.yml"],
            "homelab-monitor", self.TARGET,
            "sikamikaniko123/homelab-monitor:" + self.TARGET,
            "sikamikaniko123/homelab-monitor@sha256:abc", 5000)

    def _wget_line(self):
        lines = [l for l in self._script().splitlines() if "wget" in l]
        self.assertEqual(len(lines), 1, lines)
        return lines[0]

    def _read_version(self, body):
        """Run the script's real version-reading pipeline with sh, feeding
        `body` in place of the wget call."""
        m = re.search(r"cur=\$\(wget [^|]*\| (.*)\)\s*$", self._wget_line())
        self.assertIsNotNone(m, self._wget_line())
        pipeline = "printf '%s' \"$BODY\" | " + m.group(1)
        out = subprocess.run(["sh", "-c", pipeline], env={"BODY": body, "PATH": os.environ["PATH"]},
                             capture_output=True, text=True, check=True).stdout
        return out.strip()

    def test_polls_healthz_not_api_health(self):
        line = self._wget_line()
        self.assertIn("http://127.0.0.1:5000/healthz", line)
        self.assertNotIn("/api/health", self._script())

    def test_reads_version_from_healthz_body(self):
        body = json.dumps({"status": "ok", "version": self.TARGET})
        self.assertEqual(self._read_version(body), self.TARGET)

    def test_auth_required_body_yields_no_version(self):
        body = json.dumps({"ok": False, "error": "auth_required", "message": "Login required"})
        self.assertEqual(self._read_version(body), "")

    def test_other_version_does_not_match_target(self):
        body = json.dumps({"status": "ok", "version": "0.16.0"})
        got = self._read_version(body)
        self.assertEqual(got, "0.16.0")
        self.assertNotEqual(got, self.TARGET)


if __name__ == "__main__":
    unittest.main()
