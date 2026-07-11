"""Tests for the controls audit log (accountability for every host mutation).

The controls audit log is the trust story behind our opt-in controls: every
EXECUTED start/stop/restart of a container/service — success AND failure — is
persisted to a bounded, append-only SQLite ring and surfaced read-only via
GET /api/controls/log. The reviewer verifies each property below:

  • An enabled control action writes EXACTLY ONE audit row (success case).
  • A FAILED action is audited too (a rejected action is what you want audited).
  • GET /api/controls/log returns rows newest-first and respects the ?limit= cap.
  • The audit log is PRIVATE — it never appears on /api/status or /status.
  • The table is BOUNDED — retention prune keeps it from growing without limit.
  • A forced audit-write failure NEVER breaks / 500s the control response.
  • detail is generic (no traceback / socket path leaked into the row).

No real docker or systemd is touched — both paths are mocked.
"""
import os
import sys
import unittest
from unittest.mock import patch

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
    ],
    "summary": {},
}


def _client():
    return app.app.test_client()


def _clear_audit():
    with app.LOCK:
        app.DB.execute("DELETE FROM control_audit")
        app.DB.commit()


def _audit_rows():
    with app.LOCK:
        return app.DB.execute(
            "SELECT ts,kind,target,action,result,detail,actor FROM control_audit ORDER BY id"
        ).fetchall()


class TestAuditWrite(unittest.TestCase):
    def setUp(self):
        _clear_audit()

    def test_success_writes_exactly_one_row(self):
        with patch.object(app, "ENABLE_CONTROLS", True), \
             patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app._docker_req", return_value=(204, b"")):
            r = _client().post("/api/containers/grafana/action", json={"action": "start"})
            self.assertEqual(r.status_code, 200)
        rows = _audit_rows()
        self.assertEqual(len(rows), 1)
        ts, kind, target, action, result, detail, actor = rows[0]
        self.assertEqual(kind, "container")
        # RESOLVED tracked name — not raw user input.
        self.assertEqual(target, "grafana")
        self.assertEqual(action, "start")
        self.assertEqual(result, "ok")

    def test_failure_is_audited_too(self):
        with patch.object(app, "ENABLE_CONTROLS", True), \
             patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app._docker_req", side_effect=OSError("socket gone")):
            r = _client().post("/api/containers/grafana/action", json={"action": "restart"})
            self.assertEqual(r.status_code, 502)
        rows = _audit_rows()
        self.assertEqual(len(rows), 1)
        _ts, kind, target, action, result, detail, _actor = rows[0]
        self.assertEqual(result, "error")
        self.assertEqual(action, "restart")
        # detail is generic — the raw exception text must never leak into the row.
        self.assertNotIn("socket gone", detail or "")

    def test_service_action_audited(self):
        app.HEALTH["systemd"] = FAKE_SYSTEMD
        with patch.object(app, "ENABLE_CONTROLS", True), \
             patch("app._systemd_control", return_value=(True, None)):
            r = _client().post("/api/services/netdata/action", json={"action": "stop"})
            self.assertEqual(r.status_code, 200)
        rows = _audit_rows()
        self.assertEqual(len(rows), 1)
        _ts, kind, target, action, result, _d, _a = rows[0]
        self.assertEqual(kind, "service")
        self.assertEqual(target, "netdata.service")   # resolved unit name
        self.assertEqual(result, "ok")

    def test_disabled_and_bad_target_write_nothing(self):
        # Refused-at-gate (controls OFF) and 404 (unknown target) never reach
        # resolution → by design they write NO audit row.
        with patch.object(app, "ENABLE_CONTROLS", False):
            _client().post("/api/containers/grafana/action", json={"action": "start"})
        with patch.object(app, "ENABLE_CONTROLS", True), \
             patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app._docker_req") as req:
            _client().post("/api/containers/nope/action", json={"action": "start"})
            req.assert_not_called()
        self.assertEqual(len(_audit_rows()), 0)


class TestAuditReadEndpoint(unittest.TestCase):
    def setUp(self):
        _clear_audit()

    def test_newest_first_and_limit_cap(self):
        with patch.object(app, "ENABLE_CONTROLS", True), \
             patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app._docker_req", return_value=(204, b"")):
            for act in ("start", "stop", "restart"):
                _client().post("/api/containers/grafana/action", json={"action": act})
        r = _client().get("/api/controls/log")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        acts = [it["action"] for it in j["items"]]
        # newest-first: last executed (restart) comes first.
        self.assertEqual(acts, ["restart", "stop", "start"])
        self.assertEqual(j["count"], 3)
        # ?limit= is honoured and clamped to the retention cap.
        r2 = _client().get("/api/controls/log?limit=1")
        self.assertEqual(len(r2.get_json()["items"]), 1)
        r3 = _client().get("/api/controls/log?limit=999999")
        self.assertLessEqual(len(r3.get_json()["items"]), app._CONTROL_AUDIT_RETENTION)

    def test_get_endpoint_is_read_only(self):
        # A GET must never mutate — POST is not allowed on the log endpoint.
        self.assertEqual(_client().post("/api/controls/log").status_code, 405)


class TestRetentionBound(unittest.TestCase):
    def setUp(self):
        _clear_audit()

    def test_prune_bounds_table(self):
        cap = app._CONTROL_AUDIT_RETENTION
        with patch.object(app, "ENABLE_CONTROLS", True), \
             patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app._docker_req", return_value=(204, b"")):
            for _ in range(cap + 25):
                _client().post("/api/containers/grafana/action", json={"action": "start"})
        self.assertLessEqual(len(_audit_rows()), cap)


class TestAuditNeverBreaksAction(unittest.TestCase):
    def setUp(self):
        _clear_audit()

    def test_internal_db_error_swallowed(self):
        # Force the audit INSERT to raise from inside _record_control_audit (the DB
        # handle blows up) — the try/except must swallow it and the control response
        # stays a clean 200. sqlite3.Connection.execute is read-only, so swap the
        # whole handle for a stub that raises.
        class _BoomDB:
            def execute(self, *a, **k):
                raise Exception("db gone")
            def commit(self):
                pass
        with patch.object(app, "ENABLE_CONTROLS", True), \
             patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app._docker_req", return_value=(204, b"")), \
             patch.object(app, "DB", _BoomDB()):
            r = _client().post("/api/containers/grafana/action", json={"action": "start"})
            # The action still succeeds; the swallowed audit failure never 500s it.
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()["ok"])


class TestAuditNotOnPublicSurface(unittest.TestCase):
    def setUp(self):
        _clear_audit()
        # Seed a row so a leak would actually show up.
        with patch.object(app, "ENABLE_CONTROLS", True), \
             patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app._docker_req", return_value=(204, b"")):
            _client().post("/api/containers/grafana/action", json={"action": "start"})

    def test_api_status_leaks_nothing(self):
        with patch.object(app, "ENABLE_CONTROLS", True):
            r = _client().get("/api/status")
            body = r.get_data(as_text=True)
        self.assertNotIn("control_audit", body)
        self.assertNotIn("controls/log", body)
        # the seeded target name must not appear via the public status surface
        if r.status_code == 200:
            self.assertNotIn("grafana", r.get_json().get("controls", {}) if isinstance(r.get_json(), dict) else {})

    def test_status_page_leaks_nothing(self):
        with patch.object(app, "ENABLE_CONTROLS", True):
            r = _client().get("/status")
            body = r.get_data(as_text=True) if r.status_code == 200 else ""
        self.assertNotIn("control_audit", body)
        self.assertNotIn("/api/controls/log", body)


if __name__ == "__main__":
    unittest.main()
