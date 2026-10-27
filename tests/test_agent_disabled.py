"""EC-16: the agent ships disabled. Without AGENT_ENABLED=1:
- every /api/agent/* route answers 404 (found by walking app.url_map),
- no agent thread starts at boot and no agent table is created by requests,
- /api/stream carries no agent frame even if something is published,
- the dashboard page and existing snapshots are untouched (test_snapshots.py).
With the env switch on but no password, the agent can't be enabled.
"""
import json
import os
import re
import threading
import unittest
from unittest.mock import patch

from auth_helpers import app, clear_login, login
from backend.agent import boot, events, schema, state


def _agent_rules():
    return [r for r in app.app.url_map.iter_rules() if r.rule.startswith("/api/agent")]


def _path(rule):
    return re.sub(r"<(?:[^:<>]+:)?([^<>]+)>", "x", rule.rule)


class TestDisabledByDefault(unittest.TestCase):
    def setUp(self):
        os.environ.pop("AGENT_ENABLED", None)
        clear_login()
        self.c = app.app.test_client()

    def tearDown(self):
        clear_login()

    def test_every_agent_route_is_404(self):
        rules = _agent_rules()
        self.assertGreaterEqual(len(rules), 2)
        for rule in rules:
            for m in rule.methods - {"HEAD", "OPTIONS"}:
                r = self.c.open(_path(rule), method=m, json={})
                self.assertEqual(r.status_code, 404, "%s %s" % (m, rule.rule))

    def test_still_404_with_a_password(self):
        login(self.c)
        self.assertEqual(self.c.get("/api/agent/status").status_code, 404)

    def test_no_threads_and_no_tables(self):
        before = {t.name for t in threading.enumerate()}
        with patch("backend.agent.boot.start_if_enabled") as start:
            from backend import wiring
            wiring.start_workers()
        start.assert_not_called()
        self.assertEqual({t.name for t in threading.enumerate()} - before, set())
        self.assertFalse(boot.started())
        # Requests on a disabled install never create agent tables.
        with app.LOCK:
            app.DB.executescript(";".join("DROP TABLE IF EXISTS %s" % t for t in schema.tables(app.DB)) or "SELECT 1")
            app.DB.commit()
        schema.reset()
        for p in ("/api/agent/status", "/api/fleet", "/"):
            self.c.get(p)
        with app.LOCK:
            self.assertEqual(schema.tables(app.DB), [])

    def test_stream_has_no_agent_frames(self):
        r = self.c.get("/api/stream")
        try:
            it = iter(r.response)
            first = next(it)
            events.publish("run", run_id="x", status="running")
            app.bump_live()
            chunk = next(it)
            text = (first + chunk).decode() if isinstance(first, bytes) else first + chunk
            self.assertNotIn("event: agent", text)
            self.assertIn("event: now", text)
        finally:
            r.close()
            events.reset()

    def test_dashboard_never_fetches_agent_assets_when_404(self):
        html = open(os.path.join(os.path.dirname(app.__file__), "static", "dashboard.html"), encoding="utf-8").read()
        # The loader only injects agent.js after /api/agent/status answered ok.
        i = html.index("/api/agent/status")
        self.assertIn("if(!r.ok) return;", html[i:i + 200])
        self.assertNotIn('src="/static/agent.js', html)


class TestEnvOnNeedsPassword(unittest.TestCase):
    def setUp(self):
        clear_login()
        self.c = app.app.test_client()
        self.env = patch.dict(os.environ, {"AGENT_ENABLED": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        clear_login()

    def test_status_explains_password_needed(self):
        j = self.c.get("/api/agent/status").get_json()
        self.assertTrue(j["allowed"])
        self.assertFalse(j["password_set"])
        self.assertFalse(j["enabled"])

    def test_cannot_enable_without_password(self):
        r = self.c.post("/api/agent/enable", json={"enabled": True})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["error"], "password_required")
        self.assertFalse(state.runtime_enabled())

    def test_stream_carries_agent_frames_when_allowed(self):
        r = self.c.get("/api/stream")
        try:
            it = iter(r.response)
            for _ in range(3):            # retry hint, first `now`, first `fleet`
                next(it)
            events.publish("run", run_id="abc", status="running")
            chunk = next(it)
            text = chunk.decode() if isinstance(chunk, bytes) else chunk
            self.assertIn("event: agent", text)
            data = json.loads(text.split("data: ", 1)[1].split("\n", 1)[0])
            self.assertEqual(data["run_id"], "abc")
        finally:
            r.close()
            events.reset()


if __name__ == "__main__":
    unittest.main()
