"""/api/hosts/<name>/run runs any shell command, and nothing in the dashboard
uses it — so it is off unless ENABLE_HOST_RUN=1 (#354), and every use is
logged without the command text. The existing container/service control
endpoints are unchanged by the login when no password is set, and need a
session + CSRF (or an admin key) once one is."""
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from auth_helpers import app, clear_login, login, mint_key


class TestHostRunOptIn(unittest.TestCase):
    def setUp(self):
        clear_login()
        self.c = app.app.test_client()

    def tearDown(self):
        clear_login()

    def test_off_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENABLE_HOST_RUN", None)
            with patch("app.run_on_host") as run:
                r = self.c.post("/api/hosts/x/run", json={"cmd": "reboot"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("ENABLE_HOST_RUN", r.get_json()["error"])
        run.assert_not_called()

    def test_opt_in_runs_and_logs_without_the_command(self):
        out = io.StringIO()
        with patch.dict(os.environ, {"ENABLE_HOST_RUN": "1"}), \
                patch("app.run_on_host", return_value={"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}), \
                redirect_stdout(out):
            r = self.c.post("/api/hosts/lab1/run", json={"cmd": "echo topsecret-value"})
        self.assertEqual(r.status_code, 200)
        line = [ln for ln in out.getvalue().splitlines() if '"host_run"' in ln][0]
        rec = json.loads(line)
        self.assertEqual(rec["host"], "lab1")
        self.assertEqual(rec["exit_code"], 0)
        self.assertNotIn("topsecret", line)

    def test_controls_still_gate_it(self):
        with patch.dict(os.environ, {"ENABLE_HOST_RUN": "1"}), patch.object(app, "ENABLE_CONTROLS", False):
            self.assertEqual(self.c.post("/api/hosts/x/run", json={"cmd": "id"}).status_code, 403)


class TestControlEndpointsWithLogin(unittest.TestCase):
    """Regression: the manual control buttons behave as before without a
    password, and need the login once there is one."""

    def setUp(self):
        clear_login()
        self.c = app.app.test_client()

    def tearDown(self):
        clear_login()
        with app.LOCK:
            app.DB.execute("DELETE FROM api_keys")
            app.DB.commit()

    def _calls(self, client, headers=None):
        h = headers or {}
        with patch("app._docker_req", return_value=(204, b"")), \
                patch("app.systemd_unit_action", return_value=(True, None)):
            return [client.post("/api/containers/web/action", json={"action": "restart"}, headers=h).status_code,
                    client.post("/api/containers/web/restart-policy", json={"policy": "always"},
                                headers=h).status_code,
                    client.post("/api/services/nginx.service/action", json={"action": "restart"},
                                headers=h).status_code]

    def test_without_password_unchanged(self):
        codes = self._calls(self.c)
        self.assertEqual(codes[0], 200)
        self.assertEqual(codes[2], 200)

    def test_with_password_need_session_and_csrf(self):
        csrf = login(self.c)
        anon = app.app.test_client()
        self.assertEqual(self._calls(anon), [401, 401, 401])
        self.assertEqual(self._calls(self.c), [403, 403, 403])
        codes = self._calls(self.c, {"X-CSRF-Token": csrf})
        self.assertEqual(codes[0], 200)
        self.assertEqual(codes[2], 200)
        _, key = mint_key("admin")
        codes = self._calls(anon, {"Authorization": "Bearer " + key})
        self.assertEqual(codes[0], 200)
        _, rkey = mint_key("read")
        self.assertEqual(self._calls(anon, {"Authorization": "Bearer " + rkey}), [403, 403, 403])


if __name__ == "__main__":
    unittest.main()
