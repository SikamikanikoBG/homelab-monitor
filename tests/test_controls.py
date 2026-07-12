"""Tests for the opt-in container/service controls (start/stop/restart).

The reviewer hammers each safety property — this touches the real host, so the
gating is load-bearing:

  • OFF by default: with ENABLE_CONTROLS false, EVERY control endpoint returns a
    clean 403 and never touches docker or D-Bus (no side-effect, no traceback).
  • Action enum validated: anything but start|stop|restart → 400.
  • Target validation: only a container/unit the monitor already enumerates is
    ever acted on. Unknown / injection-y / free-form names → 404, and no
    docker/D-Bus call is ever made for them.
  • No shell / no name-kill: docker goes over the Engine API socket with a
    resolved 12-hex id; systemd goes over D-Bus with a resolved unit name. No
    subprocess, no pkill/killall.
  • Public surface exposes NO control capability (/api/status, /status).
  • Idempotent + honest errors: docker/D-Bus down or target vanished → clean
    JSON error (never a 500).

No real docker or systemd is touched — both paths are mocked.
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


FAKE_CONTAINERS = [
    {"id": "abc123def456", "name": "grafana", "image": "grafana/grafana", "ip": None, "ports": []},
    {"id": "0011223344ff", "name": "ollama", "image": "ollama/ollama", "ip": None, "ports": []},
]

FAKE_SYSTEMD = {
    "available": True,
    "services": [
        {"name": "netdata.service", "active": "active", "sub": "running", "status": "ok"},
        {"name": "pihole-FTL.service", "active": "inactive", "sub": "dead", "status": "warn"},
    ],
    "summary": {},
}


def _client():
    return app.app.test_client()


class TestDisabledByDefault(unittest.TestCase):
    """With the flag OFF (the default, and how the live arena runs) nothing acts."""

    def test_container_action_403_when_disabled(self):
        with patch.object(app, "ENABLE_CONTROLS", False), \
             patch("app._resolve_container") as rc, \
             patch("app._docker_control") as dc:
            r = _client().post("/api/containers/grafana/action", json={"action": "restart"})
            self.assertEqual(r.status_code, 403)
            self.assertFalse(r.get_json()["ok"])
            # Hard gate: NOT even the target resolver runs, let alone docker.
            rc.assert_not_called()
            dc.assert_not_called()

    def test_service_action_403_when_disabled(self):
        with patch.object(app, "ENABLE_CONTROLS", False), \
             patch("app._resolve_unit") as ru, \
             patch("app._systemd_control") as sc:
            r = _client().post("/api/services/netdata/action", json={"action": "stop"})
            self.assertEqual(r.status_code, 403)
            ru.assert_not_called()
            sc.assert_not_called()

    def test_health_reports_disabled(self):
        with patch.object(app, "ENABLE_CONTROLS", False):
            self.assertFalse(app._controls_state()["enabled"])


class TestActionEnum(unittest.TestCase):
    def test_bad_action_400_container(self):
        with patch.object(app, "ENABLE_CONTROLS", True), \
             patch("app._resolve_container") as rc, \
             patch("app._docker_control") as dc:
            for bad in ("kill", "pause", "delete", "rm -rf", "", "START;stop"):
                r = _client().post("/api/containers/grafana/action", json={"action": bad})
                self.assertEqual(r.status_code, 400, bad)
            rc.assert_not_called()
            dc.assert_not_called()

    def test_bad_action_400_service(self):
        with patch.object(app, "ENABLE_CONTROLS", True), \
             patch("app._systemd_control") as sc:
            r = _client().post("/api/services/netdata/action", json={"action": "reload"})
            self.assertEqual(r.status_code, 400)
            sc.assert_not_called()

    def test_valid_actions_are_exactly_three(self):
        self.assertEqual(tuple(app.CONTROL_ACTIONS), ("start", "stop", "restart"))


class TestContainerTargetValidation(unittest.TestCase):
    def test_unknown_container_404_no_docker_call(self):
        with patch.object(app, "ENABLE_CONTROLS", True), \
             patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app._docker_req") as req:
            # Unknown / injection-y / free-form names. (A short hex PREFIX of a
            # tracked id would legitimately resolve, so it isn't tested here — the
            # gate is "matches a tracked container", not "is a full name".)
            for bad in ("nope", "../etc", "grafana;rm", "zzzz", "a b"):
                r = _client().post("/api/containers/%s/action" % bad, json={"action": "restart"})
                self.assertEqual(r.status_code, 404, bad)
            # No unresolved name ever reached the docker socket.
            req.assert_not_called()

    def test_known_container_start_ok(self):
        with patch.object(app, "ENABLE_CONTROLS", True), \
             patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app._docker_req", return_value=(204, b"")) as req:
            r = _client().post("/api/containers/grafana/action", json={"action": "start"})
            self.assertEqual(r.status_code, 200)
            j = r.get_json()
            self.assertTrue(j["ok"])
            self.assertEqual(j["action"], "start")
            # The RESOLVED id (not the raw name) is what hits the socket, via POST.
            method, path = req.call_args[0][0], req.call_args[0][1]
            self.assertEqual(method, "POST")
            self.assertIn("abc123def456", path)
            self.assertTrue(path.endswith("/start"))

    def test_already_stopped_is_idempotent_304(self):
        with patch.object(app, "ENABLE_CONTROLS", True), \
             patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app._docker_req", return_value=(304, b"")):
            r = _client().post("/api/containers/grafana/action", json={"action": "stop"})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()["ok"])

    def test_docker_unavailable_clean_error_not_500(self):
        with patch.object(app, "ENABLE_CONTROLS", True), \
             patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app._docker_req", side_effect=OSError("socket gone")):
            r = _client().post("/api/containers/grafana/action", json={"action": "restart"})
            self.assertEqual(r.status_code, 502)
            self.assertFalse(r.get_json()["ok"])
            # honest, generic error — no traceback / socket path leaked
            self.assertNotIn("socket gone", r.get_json()["error"])


class TestServiceTargetValidation(unittest.TestCase):
    def test_resolve_unit_known_and_bare(self):
        app.HEALTH["systemd"] = FAKE_SYSTEMD
        self.assertEqual(app._resolve_unit("netdata"), "netdata.service")
        self.assertEqual(app._resolve_unit("netdata.service"), "netdata.service")

    def test_resolve_unit_rejects_unknown_and_injection(self):
        app.HEALTH["systemd"] = FAKE_SYSTEMD
        for bad in ("sshd", "netdata; rm", "../foo", "netdata.service && reboot", ""):
            self.assertIsNone(app._resolve_unit(bad), bad)

    def test_unknown_service_404_no_dbus(self):
        app.HEALTH["systemd"] = FAKE_SYSTEMD
        with patch.object(app, "ENABLE_CONTROLS", True), \
             patch("app._systemd_control") as sc:
            r = _client().post("/api/services/sshd/action", json={"action": "restart"})
            self.assertEqual(r.status_code, 404)
            sc.assert_not_called()

    def test_known_service_restart_ok(self):
        app.HEALTH["systemd"] = FAKE_SYSTEMD
        with patch.object(app, "ENABLE_CONTROLS", True), \
             patch("app._systemd_control", return_value=(True, None)) as sc:
            r = _client().post("/api/services/netdata/action", json={"action": "restart"})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()["ok"])
            # The RESOLVED full unit name is what's handed to the control fn.
            self.assertEqual(sc.call_args[0][0], "netdata.service")
            self.assertEqual(sc.call_args[0][1], "restart")

    def test_systemd_error_clean_not_500(self):
        app.HEALTH["systemd"] = FAKE_SYSTEMD
        with patch.object(app, "ENABLE_CONTROLS", True), \
             patch("app._systemd_control", return_value=(False, "systemd control unavailable")):
            r = _client().post("/api/services/netdata/action", json={"action": "stop"})
            self.assertEqual(r.status_code, 502)
            self.assertFalse(r.get_json()["ok"])


class TestNoNameKillNoShell(unittest.TestCase):
    """The control paths must NEVER shell out or kill by name (Claude Code + other
    services run on this host by name). Assert no subprocess is invoked."""

    def test_container_action_never_subprocesses(self):
        with patch.object(app, "ENABLE_CONTROLS", True), \
             patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app._docker_req", return_value=(204, b"")), \
             patch("subprocess.run") as sr, patch("subprocess.Popen") as sp, \
             patch("os.system") as osys:
            _client().post("/api/containers/grafana/action", json={"action": "stop"})
            sr.assert_not_called()
            sp.assert_not_called()
            osys.assert_not_called()

    def test_service_action_never_subprocesses(self):
        app.HEALTH["systemd"] = FAKE_SYSTEMD
        with patch.object(app, "ENABLE_CONTROLS", True), \
             patch("app._systemd_control", return_value=(True, None)), \
             patch("subprocess.run") as sr, patch("subprocess.Popen") as sp, \
             patch("os.system") as osys:
            _client().post("/api/services/netdata/action", json={"action": "restart"})
            sr.assert_not_called()
            sp.assert_not_called()
            osys.assert_not_called()


class TestPublicSurfaceNoControls(unittest.TestCase):
    """The public /status surface must expose NO control capability."""

    def test_public_status_has_no_controls_field(self):
        with patch.object(app, "ENABLE_CONTROLS", True):
            r = _client().get("/api/status")
            # 200 (page on) or 404 (page off) — either way, no control leakage.
            if r.status_code == 200:
                body = r.get_json()
                self.assertNotIn("controls", body)
                self.assertNotIn("ENABLE_CONTROLS", r.get_data(as_text=True))

    def test_no_control_route_reachable_via_get(self):
        # The action endpoints are POST-only; a GET must not mutate.
        with patch.object(app, "ENABLE_CONTROLS", True), \
             patch("app._docker_req") as req, patch("app._systemd_control") as sc:
            self.assertEqual(_client().get("/api/containers/grafana/action").status_code, 405)
            self.assertEqual(_client().get("/api/services/netdata/action").status_code, 405)
            req.assert_not_called()
            sc.assert_not_called()


class TestHostRunGatedByControls(unittest.TestCase):
    """/api/hosts/<name>/run executes an arbitrary command on a registered host
    over SSH — the most powerful host-mutating surface we expose. It MUST be
    fail-closed behind ENABLE_CONTROLS, exactly like the container/service routes:
    with the flag off (the default, how the live arena runs) it returns a clean
    403 and NEVER reaches run_on_host / SSH."""

    def test_host_run_403_when_disabled(self):
        with patch.object(app, "ENABLE_CONTROLS", False), \
             patch("app.run_on_host") as roh:
            r = _client().post("/api/hosts/anyhost/run", json={"cmd": "reboot"})
            self.assertEqual(r.status_code, 403)
            self.assertFalse(r.get_json()["ok"])
            # Hard gate: the SSH executor is never even reached.
            roh.assert_not_called()

    def test_host_run_reaches_executor_when_enabled(self):
        with patch.object(app, "ENABLE_CONTROLS", True), \
             patch("app.run_on_host", return_value={"ok": True, "exit_code": 0,
                    "stdout": "", "stderr": "", "ms": 1}) as roh:
            r = _client().post("/api/hosts/anyhost/run", json={"cmd": "uptime"})
            self.assertEqual(r.status_code, 200)
            roh.assert_called_once()


class TestHealthExposesFlag(unittest.TestCase):
    def test_health_payload_carries_controls_block(self):
        with patch.object(app, "ENABLE_CONTROLS", False):
            r = _client().get("/api/health")
            self.assertEqual(r.status_code, 200)
            ctl = r.get_json().get("controls")
            self.assertIsNotNone(ctl)
            self.assertIs(ctl["enabled"], False)
            self.assertEqual(set(ctl["actions"]), {"start", "stop", "restart"})


if __name__ == "__main__":
    unittest.main()
