"""The /api/agent/* surface: providers with write-only keys, model discovery and
the tool probe, the policy API, audit export, the agent's lifecycle, and the
guards it adds around self-update, restore and host rename/delete
(US-1/2/4/5/11/12, EC-13/15/20/27/30/34/36).
"""
import io
import json
import os
import sqlite3
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import agent_stub_server as stub
from agent_fakes import AgentOn, FakeFleet, ScriptedAdapter, app
from backend.agent import boot, providers, runs, scheduler, state, store
from backend.agent.providers import http

SECRET = "sk-live-SUPERSECRET-4242"


class ApiCase(unittest.TestCase):
    def setUp(self):
        self.on = AgentOn().start()
        self.fleet = FakeFleet().start()

    def tearDown(self):
        self.fleet.stop()
        self.on.stop()


class TestProviders(ApiCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.url = stub.start()
        cls._b = http.BACKOFF
        http.BACKOFF = (0, 0, 0)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        http.BACKOFF = cls._b

    def test_key_is_write_only_everywhere(self):
        r = self.on.post("/api/agent/providers", {"preset": "openai", "api_key": SECRET, "label": "my openai"})
        p = r.get_json()["provider"]
        self.assertEqual((p["key_set"], p["key_last4"]), (True, "4242"))
        pid = p["id"]
        # walk every GET the agent exposes and the admin log
        paths = ["/api/agent/status", "/api/agent/providers", "/api/agent/models?discover=0", "/api/agent/settings",
                 "/api/agent/policy", "/api/agent/tools", "/api/agent/templates", "/api/agent/tasks",
                 "/api/agent/runs", "/api/agent/approvals", "/api/agent/conversations", "/api/agent/admin-log",
                 "/api/agent/audit/export", "/api/agent/audit/export?format=csv", "/api/agent/presets",
                 "/api/settings", "/api/integration/keys"]
        for path in paths:
            body = self.on.get(path).get_data(as_text=True)
            self.assertNotIn(SECRET, body, path)
        r = self.on.patch("/api/agent/providers/" + pid, {"label": "renamed"})
        self.assertNotIn(SECRET, r.get_data(as_text=True))
        self.assertEqual(store.provider_secret(pid), SECRET)

    def test_changing_the_host_clears_the_key(self):
        pid = self.on.post("/api/agent/providers", {"preset": "openai_compatible", "base_url": "https://a.example/v1",
                                                    "api_key": SECRET}).get_json()["provider"]["id"]
        r = self.on.patch("/api/agent/providers/" + pid, {"base_url": "https://evil.example/v1"})
        self.assertFalse(r.get_json()["provider"]["key_set"])
        self.assertIsNone(store.provider_secret(pid))

    def test_bad_urls_and_refs(self):
        self.assertEqual(self.on.post("/api/agent/providers", {"preset": "vllm", "base_url": "ftp://x"}).status_code, 400)
        self.assertEqual(self.on.post("/api/agent/providers", {"preset": "nope"}).status_code, 400)
        self.assertEqual(self.on.post("/api/agent/providers", {"preset": "openai", "key_ref": "plain"}).status_code, 400)
        r = self.on.post("/api/agent/providers", {"preset": "openai", "key_ref": "env:OPENAI_KEY"})
        self.assertEqual(r.get_json()["provider"]["key_ref"], "env:OPENAI_KEY")

    def test_test_endpoint_401_vs_unreachable(self):
        pid = self.on.post("/api/agent/providers", {"preset": "openai_compatible", "base_url": self.url + "/err/401/v1",
                                                    "api_key": SECRET}).get_json()["provider"]["id"]
        j = self.on.post("/api/agent/providers/%s/test" % pid).get_json()
        self.assertEqual((j["ok"], j["error_kind"]), (False, "auth"))
        self.assertNotIn(SECRET, json.dumps(j))
        pid2 = self.on.post("/api/agent/providers", {"preset": "vllm", "base_url": "http://127.0.0.1:9/v1"}).get_json()["provider"]["id"]
        j = self.on.post("/api/agent/providers/%s/test" % pid2).get_json()
        self.assertEqual(j["error_kind"], "unreachable")

    def test_test_then_probe_sets_badges(self):
        pid = self.on.post("/api/agent/providers", {"preset": "ollama", "base_url": self.url}).get_json()["provider"]["id"]
        j = self.on.post("/api/agent/providers/%s/test" % pid).get_json()
        self.assertTrue(j["ok"])
        ms = {m["model"]: m for m in self.on.get("/api/agent/models?discover=0").get_json()["models"]}
        self.assertEqual(ms["stub-agent"]["context_window"], 32768)
        self.assertEqual(ms["stub-notools"]["tools"], "no")          # from /api/show before any probe
        j = self.on.post("/api/agent/models/probe", {"provider_id": pid, "model": "stub-agent"}).get_json()
        self.assertEqual((j["tools"], j["multi_step"]), ("yes", "yes"))
        j = self.on.post("/api/agent/models/probe", {"provider_id": pid, "model": "stub-notools"}).get_json()
        self.assertEqual(j["tools"], "no")

    def test_price_per_model(self):
        pid = self.on.post("/api/agent/providers", {"preset": "openai", "api_key": SECRET}).get_json()["provider"]["id"]
        r = self.on.put("/api/agent/models", {"provider_id": pid, "model": "gpt-x", "price_in": 2.5, "price_out": 10})
        self.assertEqual(r.get_json()["model"]["price_out"], 10)
        self.assertEqual(self.on.put("/api/agent/models", {"provider_id": pid, "model": "gpt-x",
                                                           "price_in": -1}).status_code, 400)

    def test_discovery_from_hub_ai_servers(self):
        host_port = self.url.split("//")[1]
        ip, port = host_port.split(":")
        with patch.object(app, "AI_SERVERS", [{"name": "ollama", "ip": ip, "port": int(port), "provider": "ollama"},
                                               {"name": "vllm", "ip": "127.0.0.1", "port": 9, "provider": "vllm"}]):
            found = self.on.get("/api/agent/models").get_json()["discovered"]
        by = {d["preset"]: d for d in found}
        self.assertTrue(by["ollama"]["reachable"])
        self.assertIn("stub-agent", by["ollama"]["models"])
        self.assertFalse(by["vllm"]["reachable"])

    def test_first_provider_becomes_default_and_delete_clears_it(self):
        pid = self.on.post("/api/agent/providers", {"preset": "vllm", "base_url": "http://x:1/v1"}).get_json()["provider"]["id"]
        self.assertEqual(state.get("default_provider_id"), pid)
        self.on.delete("/api/agent/providers/" + pid)
        self.assertEqual(state.get("default_provider_id"), "")


class TestPolicyApi(ApiCase):
    def test_get_policy_defaults(self):
        j = self.on.get("/api/agent/policy").get_json()
        hosts = {h["host"]: h for h in j["hosts"]}
        self.assertEqual(hosts["lab1"]["mode"], "passive")
        self.assertEqual(hosts["local"]["mode"], "passive")
        self.assertEqual(j["rules"], {})
        self.assertTrue(all(a["default_rule"] == "deny" for a in j["actions"]))

    def test_mode_and_rule_validation(self):
        self.assertEqual(self.on.put("/api/agent/policy/hosts/lab1", {"mode": "god"}).status_code, 400)
        self.assertEqual(self.on.put("/api/agent/policy/hosts/nope", {"mode": "active"}).status_code, 404)
        self.assertEqual(self.on.put("/api/agent/policy/hosts/lab1/actions/host.reboot", {"rule": "allow"}).status_code, 400)
        self.assertEqual(self.on.put("/api/agent/policy/hosts/lab1/actions/docker.system_df", {"rule": "allow"}).status_code, 400)

    def test_unlock_rules(self):
        base = "/api/agent/policy/hosts/lab1/actions/"
        r = self.on.put(base + "container.stop", {"rule": "allow", "unattended_unlocked": True})
        self.assertEqual(r.get_json()["error"], "acknowledge_required")
        r = self.on.put(base + "container.stop", {"rule": "allow", "unattended_unlocked": True, "acknowledge": True})
        self.assertEqual(r.status_code, 200)
        r = self.on.put(base + "ollama.remove_model", {"rule": "allow", "unattended_unlocked": True, "acknowledge": True})
        self.assertEqual(r.get_json()["error"], "cannot_unlock")
        r = self.on.put(base + "journal.vacuum", {"rule": "allow", "unattended_unlocked": True, "acknowledge": True})
        self.assertEqual(r.get_json()["error"], "cannot_unlock")      # root actions never run unattended
        log = self.on.get("/api/agent/admin-log").get_json()["items"]
        self.assertTrue(any(i["subject"] == "lab1/container.stop" for i in log))

    def test_protected_and_paths(self):
        r = self.on.put("/api/agent/policy/protected", {"host": "lab1", "kind": "container", "name": "web",
                                                         "reason": "VPN"})
        self.assertEqual(r.get_json()["protected"][0]["name"], "web")
        self.assertEqual(self.on.put("/api/agent/policy/paths", {"host": "lab1", "paths": ["/var/../etc"]}).status_code, 400)
        r = self.on.put("/api/agent/policy/paths", {"host": "lab1", "paths": ["/srv/backups"]})
        self.assertEqual(r.get_json()["watched_paths"], {"lab1": ["/srv/backups"]})

    def test_controls_off_shows_in_status_and_policy(self):
        with patch.object(app, "ENABLE_CONTROLS", False):
            self.assertFalse(self.on.get("/api/agent/policy").get_json()["controls_enabled"])
            self.assertFalse(self.on.get("/api/agent/status").get_json()["controls_enabled"])

    def test_host_rename_moves_policy_and_refuses_pending(self):
        self.on.put("/api/agent/policy/hosts/lab1", {"mode": "active"})
        self.on.put("/api/agent/policy/hosts/lab1/actions/container.restart", {"rule": "ask"})
        run = {"id": store.run_create("c", "chat", True, "act"), "task_id": None, "attended": 0}
        from backend.agent import approvals
        aid, _ = approvals.propose(run, "call1", "container.restart", "lab1", {"name": "web"}, "x")
        r = self.on.patch("/api/hosts/lab1", {"name": "lab9"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.fleet.hosts["lab9"] = self.fleet.hosts.pop("lab1")
        self.assertEqual(store.host_modes().get("lab9"), "active")
        self.assertNotIn("lab1", store.host_modes())
        self.assertEqual(store.action_rules()["lab9"]["container.restart"]["rule"], "ask")
        self.assertEqual(store.approval_get(aid)["status"], "refused")
        r = self.on.delete("/api/hosts/lab9")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("lab9", store.host_modes())


class TestLifecycle(ApiCase):
    def test_status_counts(self):
        j = self.on.get("/api/agent/status").get_json()
        self.assertTrue(j["enabled"])
        self.assertEqual(j["pending_approvals"], 0)

    def test_routes_need_the_agent_on_to_run(self):
        self.on.post("/api/agent/enable", {"enabled": False})
        cid = self.on.post("/api/agent/conversations").get_json()["id"]
        r = self.on.post("/api/agent/conversations/%s/messages" % cid, {"text": "hi"})
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "agent_disabled"))
        self.assertEqual(self.on.get("/api/agent/policy").status_code, 200)   # setup still works

    def test_disable_cancels_pending_and_runs(self):
        run = {"id": store.run_create("c", "schedule", False, "act"), "task_id": None, "attended": 0}
        from backend.agent import approvals
        aid, _ = approvals.propose(run, "c1", "container.restart", "lab1", {"name": "web"}, None)
        with patch("backend.agent.scheduler.stop") as stop:
            self.on.post("/api/agent/enable", {"enabled": False})
        stop.assert_called()
        self.assertEqual(store.approval_get(aid)["status"], "expired")
        self.assertEqual(store.run_get(run["id"])["status"], "cancelled")

    def test_boot_recovery(self):
        rid = store.run_create("c", "chat", True, "act", status="running")
        rid2 = store.run_create("c", "chat", True, "act", status="waiting_approval")
        boot.recover()
        self.assertEqual(store.run_get(rid)["status"], "interrupted")
        self.assertEqual(store.run_get(rid2)["status"], "waiting_approval")

    def test_start_registers_metrics_and_threads(self):
        with patch("backend.agent.runs.Manager.start") as rs, patch("backend.agent.scheduler.start") as ss:
            boot._started["on"] = False
            self.assertTrue(boot.start_if_enabled())
        rs.assert_called_once()
        ss.assert_called_once()
        boot._started["on"] = False
        body = app.app.test_client().get("/metrics").get_data(as_text=True)
        self.assertIn("homelab_agent_pending_approvals", body)

    def test_update_and_restore_refused_while_running(self):
        rid = store.run_create("c", "chat", True, "act", status="running")
        r = self.on.post("/api/update/app", {})
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "agent_busy"))
        data = {"backup": (io.BytesIO(b"SQLite format 3\x00" + b"x" * 200), "b.db")}
        with patch("app._data_dir_writable", return_value=True):
            r = self.on.client.post("/api/backup/restore", data=data, headers=self.on.h(),
                                    content_type="multipart/form-data")
        self.assertEqual(r.status_code, 409)
        store.run_update(rid, status="done")
        with patch("app.start_self_update", return_value=(400, {"ok": False})):
            self.assertEqual(self.on.post("/api/update/app", {}).status_code, 400)

    def test_force_update_cancels_runs(self):
        rid = store.run_create("c", "chat", True, "act", status="queued")
        with patch("app.start_self_update", return_value=(202, {"ok": True})):
            r = self.on.post("/api/update/app", {"force": True})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(store.run_get(rid)["status"], "cancelled")


class TestAudit(ApiCase):
    def _seed_run(self):
        r = self.on.post("/api/agent/providers", {"preset": "vllm", "base_url": "http://model:1/v1", "api_key": SECRET})
        pid = r.get_json()["provider"]["id"]
        store.model_upsert(pid, "m", tools="yes", context_window=32768)
        self.on.put("/api/agent/settings", {"default_provider_id": pid, "default_model": "m"})
        self.on.put("/api/agent/policy/hosts/lab1", {"mode": "active"})
        self.on.put("/api/agent/policy/hosts/lab1/actions/container.restart", {"rule": "allow"})
        ad = ScriptedAdapter([{"tool_calls": [{"name": "container__restart",
                                               "arguments": {"host": "lab1", "name": "web"}}]}, {"text": "done"}])
        cid = self.on.post("/api/agent/conversations").get_json()["id"]
        out = io.StringIO()
        with patch.object(providers, "adapter_for", lambda row: ad), redirect_stdout(out):
            rid = self.on.post("/api/agent/conversations/%s/messages" % cid, {"text": "restart web"}).get_json()["run_id"]
        return rid, out.getvalue()

    def test_stdout_lines_and_no_secrets_anywhere(self):
        rid, out = self._seed_run()
        lines = [json.loads(l) for l in out.splitlines() if '"agent_audit"' in l]
        self.assertEqual([l["phase"] for l in lines if l.get("action")], ["intent", "result"])
        self.assertNotIn(SECRET, out)
        with app.LOCK:
            for t in ("agent_steps", "agent_runs", "agent_messages", "agent_approvals", "agent_admin_log"):
                dump = json.dumps(app.DB.execute("SELECT * FROM %s" % t).fetchall())
                self.assertNotIn(SECRET, dump, t)

    def test_export_json_and_csv(self):
        rid, _ = self._seed_run()
        j = json.loads(self.on.get("/api/agent/audit/export").get_data(as_text=True))
        self.assertEqual(j["runs"][0]["id"], rid)
        self.assertIn("action_result", [s["kind"] for s in j["steps"]])
        r = self.on.get("/api/agent/audit/export?format=csv")
        self.assertIn("attachment", r.headers["Content-Disposition"])
        self.assertIn("container.restart", r.get_data(as_text=True))
        j = json.loads(self.on.get("/api/agent/audit/export?host=nas").get_data(as_text=True))
        self.assertEqual(j["runs"], [])

    def test_activity_filters(self):
        rid, _ = self._seed_run()
        self.assertEqual(self.on.get("/api/agent/runs?host=lab1").get_json()["items"][0]["id"], rid)
        self.assertEqual(self.on.get("/api/agent/runs?host=nas").get_json()["items"], [])
        self.assertEqual(self.on.get("/api/agent/runs?action=container.restart").get_json()["items"][0]["id"], rid)
        self.assertEqual(self.on.get("/api/agent/runs?status=failed").get_json()["items"], [])
        d = self.on.get("/api/agent/runs/" + rid).get_json()
        self.assertEqual(d["run"]["title"], "restart web")
        self.assertEqual(self.on.get("/api/agent/runs/%s?after_seq=2" % rid).get_json()["steps"][0]["seq"], 3)

    def test_retention_prunes_old_finished_runs_only(self):
        rid, _ = self._seed_run()
        old = store.run_create("c", "chat", True, "act", status="done")
        waiting = store.run_create("c", "chat", True, "act", status="waiting_approval")
        with app.LOCK:
            app.DB.execute("UPDATE agent_runs SET created_at=? WHERE id IN (?,?)", (1000, old, waiting))
            app.DB.commit()
        n = store.prune(int(time.time()) - 86400)
        self.assertEqual(n, 1)
        self.assertIsNone(store.run_get(old))
        self.assertIsNotNone(store.run_get(waiting))
        self.assertIsNotNone(store.run_get(rid))


if __name__ == "__main__":
    unittest.main()
