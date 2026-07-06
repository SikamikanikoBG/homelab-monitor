"""Unit tests for the container/service controls (ENABLE_CONTROLS — on by
default, gated off with ENABLE_CONTROLS=0 or docker-compose.readonly.yml).

Docker (_docker_req) and systemd (systemd_unit_action) are stubbed — these
tests never touch a real socket. SSH-backed remote actions stub run_on_host /
run_on_host_windows directly, same pattern as test_win_docker_probe.py.
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _add_host(name, ssh_target="user@1.2.3.4"):
    with app.LOCK:
        app.DB.execute("DELETE FROM hosts WHERE name=?", (name,))
        app.DB.execute("INSERT INTO hosts(name, ssh_target, added_at) VALUES(?,?,0)",
                       (name, ssh_target))
        app.DB.commit()


def _remove_host(name):
    with app.LOCK:
        app.DB.execute("DELETE FROM hosts WHERE name=?", (name,))
        app.DB.commit()


class ControlsBase(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self._p_flag = patch.object(app, "ENABLE_CONTROLS", True)
        self._p_flag.start()
        # Caches persist across tests in the same process — start clean.
        app._ct_cache.update(list=[], at=0)
        app._docker_enrich.update(data={}, at=0)
        app._docker_policy.update(data={}, at=0)

    def tearDown(self):
        patch.stopall()


class TestContainerActionGating(ControlsBase):
    def test_disabled_returns_403(self):
        with patch.object(app, "ENABLE_CONTROLS", False):
            rv = self.client.post("/api/containers/web/action", json={"action": "restart"})
        self.assertEqual(rv.status_code, 403)
        self.assertFalse(rv.get_json()["ok"])

    def test_invalid_name_returns_400(self):
        rv = self.client.post("/api/containers/bad;rm/action", json={"action": "restart"})
        self.assertEqual(rv.status_code, 400)

    def test_invalid_action_returns_400(self):
        rv = self.client.post("/api/containers/web/action", json={"action": "delete"})
        self.assertEqual(rv.status_code, 400)
        self.assertIn("action must be one of", rv.get_json()["error"])

    def test_invalid_policy_returns_400(self):
        rv = self.client.post("/api/containers/web/restart-policy", json={"policy": "sometimes"})
        self.assertEqual(rv.status_code, 400)


class TestContainerActionExecution(ControlsBase):
    def test_start_success(self):
        with patch.object(app, "_docker_req", return_value=(204, b"")) as m:
            rv = self.client.post("/api/containers/web/action", json={"action": "start"})
        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.get_json()["ok"])
        method, path = m.call_args[0][:2]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/containers/web/start")

    def test_no_such_container_404(self):
        with patch.object(app, "_docker_req", return_value=(404, b'{"message":"No such container: web"}')):
            rv = self.client.post("/api/containers/web/action", json={"action": "stop"})
        self.assertEqual(rv.status_code, 404)
        self.assertFalse(rv.get_json()["ok"])

    def test_docker_error_message_surfaces(self):
        with patch.object(app, "_docker_req",
                          return_value=(409, b'{"message":"container already stopped"}')):
            rv = self.client.post("/api/containers/web/action", json={"action": "stop"})
        self.assertEqual(rv.status_code, 400)
        self.assertIn("already stopped", rv.get_json()["error"])

    def test_socket_unreachable_returns_500(self):
        with patch.object(app, "_docker_req", side_effect=OSError("no such file or directory")):
            rv = self.client.post("/api/containers/web/action", json={"action": "restart"})
        self.assertEqual(rv.status_code, 500)
        self.assertIn("Docker socket", rv.get_json()["error"])

    def test_restart_policy_success(self):
        with patch.object(app, "_docker_req", return_value=(200, b"{}")) as m:
            rv = self.client.post("/api/containers/web/restart-policy", json={"policy": "unless-stopped"})
        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.get_json()["ok"])
        _, path, kwargs = m.call_args[0][0], m.call_args[0][1], m.call_args[1]
        self.assertEqual(path, "/containers/web/update")
        self.assertEqual(kwargs["body"], {"RestartPolicy": {"Name": "unless-stopped"}})


class TestDockerCollectEnrichment(ControlsBase):
    def _raw_list(self):
        return json.dumps([{
            "Id": "a" * 64, "Names": ["/web"], "Image": "nginx", "State": "running",
            "Status": "Up 2 hours", "Ports": [], "NetworkSettings": {"Networks": {}},
        }]).encode()

    def test_restart_policy_enriched_when_controls_enabled(self):
        def stub(method, path, body=None, timeout=8):
            if path == "/containers/json?all=1":
                return (200, self._raw_list())
            if path.endswith("/json"):
                return (200, json.dumps({"HostConfig": {"RestartPolicy": {"Name": "always", "MaximumRetryCount": 0}}}).encode())
            return (200, b"{}")
        with patch.object(app, "_docker_req", side_effect=stub):
            data = app.collect_docker()
        self.assertEqual(data["containers"][0]["restart_policy"], {"name": "always", "max_retry": 0})

    def test_restart_policy_absent_when_controls_disabled(self):
        def stub(method, path, body=None, timeout=8):
            if path == "/containers/json?all=1":
                return (200, self._raw_list())
            self.fail("inspect should not be called when ENABLE_CONTROLS is off")
        with patch.object(app, "ENABLE_CONTROLS", False), \
             patch.object(app, "_docker_req", side_effect=stub):
            data = app.collect_docker()
        self.assertIsNone(data["containers"][0]["restart_policy"])

    def test_is_self_flag(self):
        def stub(method, path, body=None, timeout=8):
            if path == "/containers/json?all=1":
                return (200, self._raw_list())
            return (200, b"{}")   # stats / inspect calls — content doesn't matter here
        with patch.dict(os.environ, {"HOSTNAME": "a" * 12}), \
             patch.object(app, "_docker_req", side_effect=stub):
            data = app.collect_docker()
        self.assertTrue(data["containers"][0]["is_self"])


class TestServiceActionGating(ControlsBase):
    def test_disabled_returns_403(self):
        with patch.object(app, "ENABLE_CONTROLS", False):
            rv = self.client.post("/api/services/sshd.service/action", json={"action": "restart"})
        self.assertEqual(rv.status_code, 403)

    def test_invalid_unit_name_returns_400(self):
        rv = self.client.post("/api/services/bad;rm/action", json={"action": "restart"})
        self.assertEqual(rv.status_code, 400)

    def test_invalid_action_returns_400(self):
        rv = self.client.post("/api/services/sshd.service/action", json={"action": "reload"})
        self.assertEqual(rv.status_code, 400)


class TestServiceActionLocal(ControlsBase):
    def test_local_success(self):
        with patch.object(app, "systemd_unit_action", return_value=(True, None)) as m:
            rv = self.client.post("/api/services/sshd.service/action", json={"action": "restart"})
        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.get_json()["ok"])
        m.assert_called_once_with("sshd.service", "restart")

    def test_local_failure_surfaces_dbus_error(self):
        with patch.object(app, "systemd_unit_action", return_value=(False, "Unit not found.")):
            rv = self.client.post("/api/services/bogus.service/action", json={"action": "start"})
        body = rv.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "Unit not found.")

    def test_default_host_is_local(self):
        # No "host" key in the body at all — should still hit the local path.
        with patch.object(app, "systemd_unit_action", return_value=(True, None)) as m:
            rv = self.client.post("/api/services/sshd.service/action", json={"action": "restart"})
        self.assertEqual(rv.status_code, 200)
        m.assert_called_once()


class TestServiceActionRemote(ControlsBase):
    def setUp(self):
        super().setUp()
        _add_host("linuxbox")
        _add_host("winbox")
        with app.HOST_DATA_LOCK:
            app.HOST_DATA.pop("linuxbox", None)
            app.HOST_DATA.pop("winbox", None)

    def tearDown(self):
        _remove_host("linuxbox")
        _remove_host("winbox")
        with app.HOST_DATA_LOCK:
            app.HOST_DATA.pop("linuxbox", None)
            app.HOST_DATA.pop("winbox", None)
        super().tearDown()

    def test_remote_linux_uses_systemctl_over_ssh(self):
        app.HOST_DATA["linuxbox"] = {"data": {"host": {"os": {"family": "linux"}}}, "at": 0}
        with patch.object(app, "run_on_host",
                          return_value={"ok": True, "exit_code": 0, "stdout": "", "stderr": "", "ms": 12}) as m:
            rv = self.client.post("/api/services/sshd.service/action",
                                  json={"action": "restart", "host": "linuxbox", "sudo_password": "hunter2"})
        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.get_json()["ok"])
        args, kwargs = m.call_args
        self.assertEqual(args[0], "linuxbox")
        self.assertIn("systemctl restart", args[1])
        self.assertIn("sshd.service", args[1])
        self.assertEqual(kwargs["sudo_password"], "hunter2")

    def test_remote_linux_defaults_when_os_unknown(self):
        # No poll data yet for this host — must not crash, defaults to linux/systemctl.
        with patch.object(app, "run_on_host",
                          return_value={"ok": True, "exit_code": 0, "stdout": "", "stderr": "", "ms": 5}) as m:
            rv = self.client.post("/api/services/sshd.service/action",
                                  json={"action": "start", "host": "linuxbox"})
        self.assertEqual(rv.status_code, 200)
        m.assert_called_once()

    def test_remote_windows_uses_powershell(self):
        app.HOST_DATA["winbox"] = {"data": {"host": {"os": {"family": "windows"}}}, "at": 0}
        with patch.object(app, "run_on_host_windows",
                          return_value={"ok": True, "exit_code": 0, "stdout": "", "stderr": "", "ms": 40}) as m:
            rv = self.client.post("/api/services/wuauserv/action",
                                  json={"action": "stop", "host": "winbox"})
        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.get_json()["ok"])
        args = m.call_args[0]
        self.assertEqual(args[0], "winbox")
        self.assertIn("Stop-Service", args[1])
        self.assertIn("wuauserv", args[1])

    def test_ps_single_quote_doubles_embedded_quotes(self):
        """PowerShell's single-quoted-string escape is doubling the quote
        ('' inside '...'), not backslash-escaping — verify _ps_single_quote
        does exactly that, since a wrong escape would let a service name like
        "bad'; Remove-Item ..." terminate the string early and run as a
        separate statement."""
        self.assertEqual(app._ps_single_quote("it's"), "'it''s'")
        self.assertEqual(app._ps_single_quote("bad'; Remove-Item C:\\ -Force"),
                         "'bad''; Remove-Item C:\\ -Force'")

    def test_unknown_host_returns_404(self):
        rv = self.client.post("/api/services/sshd.service/action",
                              json={"action": "restart", "host": "does-not-exist"})
        self.assertEqual(rv.status_code, 404)


if __name__ == "__main__":
    unittest.main()
