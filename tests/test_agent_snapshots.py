"""Snapshot baselines for every new JSON endpoint (#354, T20).

Kept in their own file so the existing 72 baselines in test_snapshots.py stay
byte-identical — that file proves a default install answers exactly as before.
Time is frozen, ids are a counter, the fleet is one fake host, and a provider
is seeded with a key that must never appear in any baseline.
"""
import itertools
import json
import os
import unittest
from unittest.mock import patch

from agent_fakes import AgentOn, FakeFleet, ScriptedAdapter, app
from snapshot_helper import FROZEN_TS, assert_snapshot, frozen_time
from backend.agent import providers, store

KEY = "sk-SNAPSHOT-SECRET-0001"


class TestAgentSnapshots(unittest.TestCase):
    def setUp(self):
        counter = itertools.count(1)
        self.ids = patch.object(store, "new_id", lambda: "id%04d" % next(counter))
        self.ids.start()
        self.frozen = frozen_time()
        self.frozen.__enter__()
        # Session tokens are random; a fixed one keeps the audit's actor id stable.
        self.tok = patch("backend.auth.sessions.secrets.token_urlsafe", lambda n=32: "fixed-token-%d" % n)
        self.tok.start()
        self.health = patch.object(app, "HEALTH", {"docker": {"available": True, "containers": [{"name": "hub-web"}]},
                                                   "systemd": {"available": False}, "at": FROZEN_TS})
        self.health.start()
        self.servers = patch.object(app, "AI_SERVERS", [])
        self.servers.start()
        self.on = AgentOn().start()
        self.fleet = FakeFleet({"lab1": {"family": "linux", "containers": ["web"], "services": ["nginx.service"]}}).start()

    def tearDown(self):
        self.fleet.stop()
        self.on.stop()
        self.servers.stop()
        self.health.stop()
        self.tok.stop()
        self.frozen.__exit__()
        self.ids.stop()

    def snap(self, name, path, method="GET", body=None):
        r = getattr(self.on, method.lower())(path, body) if method != "GET" else self.on.get(path)
        text = r.get_data(as_text=True)
        self.assertNotIn(KEY, text, name)
        data = {"status": r.status_code, "body": r.get_json(silent=True)}
        assert_snapshot(self, name, data)
        return data

    def seed(self):
        pid = self.on.post("/api/agent/providers", {"preset": "openai_compatible", "base_url": "http://model:8000/v1",
                                                    "api_key": KEY, "label": "model box"}).get_json()["provider"]["id"]
        store.model_upsert(pid, "m1", tools="yes", multi_step="yes", context_window=32768, probe_detail="ok",
                           probed_at=FROZEN_TS)
        self.on.put("/api/agent/settings", {"default_provider_id": pid, "default_model": "m1", "tz": "UTC"})
        self.on.put("/api/agent/policy/hosts/lab1", {"mode": "active"})
        self.on.put("/api/agent/policy/hosts/lab1/actions/container.restart", {"rule": "ask"})
        self.on.put("/api/agent/policy/protected", {"host": "lab1", "kind": "container", "name": "web", "reason": "VPN"})
        self.on.put("/api/agent/policy/protected", {"host": "lab1", "kind": "container", "name": "web", "protected": False})
        ad = ScriptedAdapter([{"tool_calls": [{"id": "call_1", "name": "container__restart",
                                               "arguments": {"host": "lab1", "name": "web", "reason": "it is stuck"}}]}])
        cid = self.on.post("/api/agent/conversations").get_json()["id"]
        with patch.object(providers, "adapter_for", lambda row: ad):
            run = self.on.post("/api/agent/conversations/%s/messages" % cid, {"text": "restart web"}).get_json()["run_id"]
        task = self.on.post("/api/agent/tasks", {"template_id": "docker-cleanup", "hosts": ["lab1"], "tz": "UTC",
                                                 "provider_id": pid, "model": "m1"}).get_json()["task"]["id"]
        return pid, cid, run, task

    def test_status_and_setup(self):
        self.snap("agent_status", "/api/agent/status")
        self.snap("agent_settings", "/api/agent/settings")
        self.snap("agent_presets", "/api/agent/presets")
        self.snap("agent_tools", "/api/agent/tools")
        self.snap("agent_templates", "/api/agent/templates")
        self.snap("agent_policy_empty", "/api/agent/policy")
        self.snap("agent_cron_preview", "/api/agent/cron/preview", "POST", {"cron": "0 3 * * 0", "tz": "UTC"})
        self.snap("agent_cron_preview_bad", "/api/agent/cron/preview", "POST", {"cron": "0 25 * * *", "tz": "UTC"})

    def test_seeded_endpoints(self):
        pid, cid, run, task = self.seed()
        self.snap("agent_providers", "/api/agent/providers")
        self.snap("agent_models", "/api/agent/models?discover=0")
        self.snap("agent_policy", "/api/agent/policy")
        self.snap("agent_conversations", "/api/agent/conversations")
        self.snap("agent_conversation_detail", "/api/agent/conversations/" + cid)
        self.snap("agent_runs", "/api/agent/runs")
        self.snap("agent_run_detail", "/api/agent/runs/" + run)
        self.snap("agent_approvals", "/api/agent/approvals?status=pending")
        self.snap("agent_tasks", "/api/agent/tasks")
        self.snap("agent_task_fires", "/api/agent/tasks/%s/fires" % task)
        r = self.on.get("/api/agent/audit/export")
        export = json.loads(r.get_data(as_text=True))
        export["exported_at"] = "<frozen>"
        assert_snapshot(self, "agent_audit_export_json", export)
        self.snap("agent_status_seeded", "/api/agent/status")

    def test_errors(self):
        self.snap("agent_provider_bad_url", "/api/agent/providers", "POST", {"preset": "vllm", "base_url": "ftp://x"})
        self.snap("agent_task_invalid", "/api/agent/tasks", "POST", {"name": "", "prompt": "", "cron": "x", "mode": "y"})
        self.snap("agent_rule_unlock_needs_ack", "/api/agent/policy/hosts/lab1/actions/container.stop", "PUT",
                  {"rule": "allow", "unattended_unlocked": True})
        self.snap("agent_approval_unknown", "/api/agent/approvals/nope/decision", "POST", {"decision": "approve"})


class TestAuthSnapshots(unittest.TestCase):
    def setUp(self):
        from auth_helpers import clear_login
        clear_login()
        self.frozen = frozen_time()
        self.frozen.__enter__()

    def tearDown(self):
        from auth_helpers import clear_login
        self.frozen.__exit__()
        clear_login()

    def test_auth_endpoints(self):
        c = app.app.test_client()
        assert_snapshot(self, "auth_status_no_password", c.get("/api/auth/status").get_json())
        r = c.post("/api/auth/setup", json={"password": "short"})
        assert_snapshot(self, "auth_setup_weak", {"status": r.status_code, "body": r.get_json()})
        r = c.post("/api/auth/setup", json={"password": "a long enough password"})
        body = r.get_json()
        body["csrf"] = "<random>"
        assert_snapshot(self, "auth_setup_ok", {"status": r.status_code, "body": body})
        st = c.get("/api/auth/status").get_json()
        st["csrf"] = "<random>"
        assert_snapshot(self, "auth_status_logged_in", st)
        anon = app.app.test_client()
        r = anon.get("/api/fleet")
        assert_snapshot(self, "auth_required_401", {"status": r.status_code, "body": r.get_json()})
        r = anon.post("/api/auth/login", json={"password": "wrong"})
        assert_snapshot(self, "auth_login_bad", {"status": r.status_code, "body": r.get_json()})
        r = c.post("/api/settings", json={})
        assert_snapshot(self, "auth_csrf_403", {"status": r.status_code, "body": r.get_json()})

    def test_host_run_disabled(self):
        os.environ.pop("ENABLE_HOST_RUN", None)
        r = app.app.test_client().post("/api/hosts/x/run", json={"cmd": "id"})
        assert_snapshot(self, "api_hosts_run_disabled", {"status": r.status_code, "body": r.get_json()})


if __name__ == "__main__":
    unittest.main()
