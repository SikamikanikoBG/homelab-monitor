"""Regression tests for the alpha pass-1 findings (design/ai-agents/07-alpha/FINDINGS.md).
Each test names the finding it pins down."""
import json
import time
import unittest
from unittest.mock import patch

from agent_fakes import AgentOn, FakeFleet, ScriptedAdapter, app
from backend.agent import approvals, hosts as agent_hosts, providers, store


class RenameCase(unittest.TestCase):
    def setUp(self):
        self.on = AgentOn().start()
        self.fleet = FakeFleet({"lab2": {"family": "linux", "containers": ["web"]}}).start()

    def tearDown(self):
        with app.LOCK:
            for h in ("lab2", "lab2x"):
                app.DB.execute("DELETE FROM host_samples_1h WHERE host=?", (h,))
                app.DB.execute("DELETE FROM host_samples WHERE host=?", (h,))
                app.DB.execute("DELETE FROM hosts WHERE name=?", (h,))
            app.DB.commit()
        self.fleet.stop()
        self.on.stop()

    def _sample(self, host, ts):
        with app.LOCK:
            app.DB.execute("INSERT INTO host_samples_1h(ts, host, cpu, cnt) VALUES(?,?,1,1)", (ts, host))
            app.DB.commit()

    def test_18_rename_back_to_a_former_name(self):
        self._sample("lab2", 3600)
        self.assertEqual(self.on.patch("/api/hosts/lab2", {"name": "lab2x"}).status_code, 200)
        self._sample("lab2", 3600)               # the poller wrote under the old name mid-rename
        self._sample("lab2x", 7200)
        r = self.on.patch("/api/hosts/lab2x", {"name": "lab2"})
        self.assertEqual(r.status_code, 200, r.get_json())
        with app.LOCK:
            hosts = [r[0] for r in app.DB.execute("SELECT DISTINCT host FROM host_samples_1h WHERE host IN ('lab2','lab2x')")]
        self.assertEqual(hosts, ["lab2"])

    def test_4_policy_moves_with_the_rename_and_leaves_no_orphan(self):
        self.on.put("/api/agent/policy/hosts/lab2", {"mode": "active"})
        self.on.put("/api/agent/policy/hosts/lab2/actions/container.restart", {"rule": "allow"})
        self.assertEqual(self.on.patch("/api/hosts/lab2", {"name": "lab2x"}).status_code, 200)
        self.assertEqual(store.host_modes(), {"lab2x": "active"})
        self.assertEqual(list(store.action_rules()), ["lab2x"])

    def test_4_failed_rename_changes_nothing(self):
        self.on.put("/api/agent/policy/hosts/lab2", {"mode": "active"})
        quiet = app.app.test_client()
        quiet.set_cookie("hlm_session", self.on.client.get_cookie("hlm_session").value)
        app.app.config["PROPAGATE_EXCEPTIONS"] = False
        try:
            with patch("backend.db.repos.gpu_samples.rename_host", side_effect=RuntimeError("boom")):
                r = quiet.patch("/api/hosts/lab2", json={"name": "lab2x"}, headers=self.on.h())
        finally:
            app.app.config["PROPAGATE_EXCEPTIONS"] = None
        self.assertEqual(r.status_code, 500)
        with app.LOCK:
            self.assertIsNotNone(app.DB.execute("SELECT 1 FROM hosts WHERE name='lab2'").fetchone())
        self.assertEqual(store.host_modes(), {"lab2": "active"})

    def test_4_orphans_cleaned_at_boot(self):
        store.set_host_mode("ghost", "active")
        store.set_action_rule("ghost", "container.restart", "allow", False)
        self.assertEqual(agent_hosts.prune_orphans(), 2)
        self.assertNotIn("ghost", store.host_modes())

    def test_8_rename_cancels_a_chat_run_parked_on_the_host(self):
        rid = store.run_create("c", "chat", True, "act", status="waiting_approval")
        aid, _ = approvals.propose(store.run_get(rid), "c1", "container.restart", "lab2", {"name": "web"}, None)
        self.on.patch("/api/hosts/lab2", {"name": "lab2x"})
        self.assertEqual(store.approval_get(aid)["status"], "refused")
        self.assertEqual(store.run_get(rid)["status"], "cancelled")

    def test_delete_drops_policy_in_the_same_transaction(self):
        self.on.put("/api/agent/policy/hosts/lab2", {"mode": "active"})
        self.assertEqual(self.on.delete("/api/hosts/lab2").status_code, 200)
        self.assertEqual(store.host_modes(), {})


if __name__ == "__main__":
    unittest.main()


from backend.agent import catalog, executor, guards, policy  # noqa: E402


class ProbeCase(unittest.TestCase):
    def setUp(self):
        from agent_fakes import wipe_agent_tables
        wipe_agent_tables()
        self.fleet = FakeFleet({"alp": {"family": "linux", "os_id": "alpine", "containers": ["web"]}}).start()
        with app.HOST_DATA_LOCK:                 # BusyBox box: docker, no systemd
            app.HOST_DATA["alp"]["data"]["host"]["systemd"] = {"available": False}
        self.run = store.run_create(store.conversation_create("t"), "chat", True, "act")

    def tearDown(self):
        self.fleet.stop()

    def test_3_reboot_not_required_is_an_answer_not_a_failure(self):
        self.fleet.result = {"ok": False, "exit_code": 1, "stdout": "", "stderr": ""}
        out = executor.probe(self.run, "alp", "host.reboot_required", {})
        self.assertTrue(out["ok"])
        self.assertEqual(out["meaning"], "No reboot is pending.")
        self.fleet.result = {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}
        self.assertIn("pending", executor.probe(self.run, "alp", "host.reboot_required", {})["meaning"])

    def test_9_missing_command_says_not_available(self):
        self.fleet.result = {"ok": False, "exit_code": 127, "stdout": "", "stderr": "sh: x: not found"}
        out = executor.probe(self.run, "alp", "docker.system_df", {})
        self.assertFalse(out["ok"])
        self.assertIn("Not available on alp", out["meaning"])

    def test_9_probes_use_portable_commands(self):
        r = lambda aid, p: catalog.render(catalog.get(aid), "linux", p)["cmd"]
        self.assertEqual(r("host.disk_usage", {}), "df -hP")
        self.assertEqual(r("files.list_older_than", {"path": "/srv", "days": 30}),
                         "find /srv -xdev -type f -mtime +30 -exec du -k '{}' +")
        self.assertEqual(r("files.changed_within", {"path": "/srv", "days": 2}),
                         "find /srv -maxdepth 2 -type f -mtime -2")
        for a in catalog.actions():
            for tok in (a.get("linux") or {}).get("argv", []):
                self.assertNotIn(tok, ("-printf", "-newermt", "-x"), a["id"])   # GNU-only flags

    def test_9_kernel_errors_need_journalctl(self):
        self.assertNotIn("systemd", policy.capabilities("alp"))
        self.assertEqual(policy.decide("alp", "gpu.kernel_errors_24h", {}, attended=True).reason,
                         "unsupported_on_host")

    def test_27_host_without_data_yet_is_not_ready(self):
        with app.HOST_DATA_LOCK:
            saved = app.HOST_DATA.pop("alp")
        try:
            d = policy.decide("alp", "docker.system_df", {}, attended=True)
            self.assertEqual(d.reason, "host_not_ready")
            self.assertIn("hasn't reported in", policy.hint(d.reason, "alp", "docker.system_df"))
        finally:
            with app.HOST_DATA_LOCK:
                app.HOST_DATA["alp"] = saved


class StreakCase(unittest.TestCase):
    def rec(self, name, ok):
        return guards.StepRecord(name, guards.args_hash(name, {}), "r", ok, "x")

    def test_10_unrelated_failures_dont_end_the_run(self):
        w = guards.Watch({})
        for n in ("a", "b", "c", "d", "e"):
            w.record(self.rec(n, False))
        self.assertIsNone(guards.error_streak_reason(w))

    def test_10_the_same_failing_call_does(self):
        w = guards.Watch({})
        for _ in range(3):
            w.record(self.rec("a", False))
        self.assertIsNotNone(guards.error_streak_reason(w))
        w.record(self.rec("a", True))
        self.assertEqual(w.error_streak, 0)


class UnknownHostCase(unittest.TestCase):
    def setUp(self):
        self.on = AgentOn().start()

    def tearDown(self):
        self.on.stop()

    def test_26_unknown_host_is_not_called_off(self):
        from backend.agent import toolbox
        ok, text = toolbox.call_read(toolbox.Scope(), "get_host", {"name": "nosuch"})
        self.assertFalse(ok)
        self.assertIn("no host called nosuch", text)
        self.assertNotIn("Off", text)


class StopCase(unittest.TestCase):
    """#5: Stop must not wait for the model's first byte."""

    @classmethod
    def setUpClass(cls):
        import agent_stub_server as stub
        cls.srv, cls.url = stub.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_5_stop_during_a_slow_first_byte(self):
        import threading
        from backend.agent.providers.base import ChatRequest, ProviderError
        from backend.agent.providers.openai_compat import OpenAICompatAdapter
        ev = threading.Event()
        threading.Timer(0.3, ev.set).start()
        t0 = time.monotonic()
        with self.assertRaises(ProviderError) as cm:
            OpenAICompatAdapter(self.url + "/slow/5/v1", preset={}).chat(
                ChatRequest(model="stub-agent", system="", messages=[{"role": "user", "content": "x"}]), cancel=ev)
        self.assertEqual(cm.exception.kind, "cancelled")
        self.assertLess(time.monotonic() - t0, 2)

    def test_5_normal_end_does_not_cancel_the_run(self):
        import threading
        import agent_stub_server as stub
        from backend.agent.providers.base import ChatRequest
        from backend.agent.providers.openai_compat import OpenAICompatAdapter
        stub.set_script([{"text": "hello"}])
        ev = threading.Event()
        r = OpenAICompatAdapter(self.url + "/v1", preset={}).chat(
            ChatRequest(model="stub-agent", system="", messages=[{"role": "user", "content": "x"}]), cancel=ev)
        self.assertEqual(r.text, "hello")
        self.assertFalse(ev.is_set())


class ValidationCase(unittest.TestCase):
    def setUp(self):
        self.on = AgentOn().start()
        self.fleet = FakeFleet().start()
        r = self.on.post("/api/agent/providers", {"preset": "vllm", "base_url": "http://m:1/v1"})
        self.pid = r.get_json()["provider"]["id"]
        store.model_upsert(self.pid, "good", tools="yes")
        store.model_upsert(self.pid, "bad", tools="no")

    def tearDown(self):
        self.fleet.stop()
        self.on.stop()

    def test_12_settings_bounds_unknown_keys_and_partial_budgets(self):
        for body in ({"chat_card_ttl_min": 0}, {"approval_ttl_h": 100000}, {"max_output_tokens": 1e9},
                     {"default_provider_id": "nope"}, {"made_up": 1}, {"budget_chat": {"max_steps": 0}},
                     {"budget_chat": {"bogus": 3}}, {"default_model": "bad"}):
            self.assertEqual(self.on.put("/api/agent/settings", body).status_code, 400, body)
        r = self.on.put("/api/agent/settings", {"budget_chat": {"max_steps": 5}})
        self.assertEqual(r.get_json()["budget_chat"], {"max_steps": 5, "max_seconds": 300, "max_tokens": 120000})

    def test_14_untested_model_is_probed_before_it_runs(self):
        store.model_upsert(self.pid, "fresh")
        ad = ScriptedAdapter([{"text": "I only talk."}])
        cid = self.on.post("/api/agent/conversations").get_json()["id"]
        with patch.object(providers, "adapter_for", lambda row: ad):
            rid = self.on.post("/api/agent/conversations/%s/messages" % cid,
                               {"text": "hi", "provider_id": self.pid, "model": "fresh"}).get_json()["run_id"]
        self.assertEqual(store.model_get(self.pid, "fresh")["tools"], "no")
        self.assertEqual(store.run_get(rid)["status"], "refused")

    def test_16_task_create_rejects_what_cant_work(self):
        base = {"name": "t", "prompt": "p", "cron": "0 3 * * *", "mode": "report", "hosts": ["lab1"]}
        cases = [dict(base, hosts=[]), dict(base, name="x" * 200), dict(base, prompt="y" * 9000),
                 dict(base, delivery={"channels": ["discord"]}), dict(base, provider_id=self.pid, model="bad")]
        store.set_host_mode("lab1", "off")
        cases.append(dict(base))
        for body in cases:
            r = self.on.post("/api/agent/tasks", body)
            self.assertEqual(r.status_code, 400, (body.get("hosts"), list(r.get_json().get("fields", {}))))
        store.set_host_mode("lab1", "passive")
        with patch.object(app, "HEALTH", {"docker": {"available": False}}):
            r = self.on.post("/api/agent/tasks", {"template_id": "docker-cleanup", "hosts": ["local"]})
        self.assertIn("hosts", r.get_json()["fields"])

    def test_29_a_cron_that_never_fires_is_refused(self):
        r = self.on.post("/api/agent/tasks", {"name": "t", "prompt": "p", "cron": "0 0 31 2 *", "mode": "report",
                                              "hosts": ["lab1"]})
        self.assertIn("never fires", r.get_json()["fields"]["cron"])

    def test_17_approving_an_expired_card_says_expired(self):
        rid = store.run_create("c", "schedule", False, "act")
        aid, _ = approvals.propose(store.run_get(rid), "c1", "container.restart", "lab1", {"name": "web"}, None)
        with app.LOCK:
            app.DB.execute("UPDATE agent_approvals SET expires_at=1 WHERE id=?", (aid,))
            app.DB.commit()
        r = self.on.post("/api/agent/approvals/%s/decision" % aid, {"decision": "approve"})
        self.assertEqual((r.status_code, r.get_json()["error"], r.get_json()["status"]), (409, "expired", "expired"))

    def test_30_decision_reports_the_execution_outcome(self):
        store.set_host_mode("lab1", "active")
        store.set_action_rule("lab1", "container.restart", "ask", False)
        rid = store.run_create("c", "schedule", False, "act", status="done")
        aid, _ = approvals.propose(store.run_get(rid), "c1", "container.restart", "lab1", {"name": "web"}, None)
        store.set_host_mode("lab1", "passive")          # refused at execution time
        r = self.on.post("/api/agent/approvals/%s/decision" % aid, {"decision": "approve"})
        self.assertEqual(r.get_json()["status"], "refused")

    def test_20_read_keys_cant_read_the_agent(self):
        from auth_helpers import mint_key
        _, key = mint_key("read")
        h = {"Authorization": "Bearer " + key}
        c = app.app.test_client()
        for path in ("/api/agent/runs", "/api/agent/conversations", "/api/agent/audit/export"):
            self.assertEqual(c.get(path, headers=h).status_code, 403, path)
        _, admin = mint_key("admin")
        self.assertEqual(c.get("/api/agent/runs", headers={"Authorization": "Bearer " + admin}).status_code, 200)
        with app.LOCK:
            app.DB.execute("DELETE FROM api_keys")
            app.DB.commit()

    def test_22_23_key_references_and_empty_patch(self):
        r = self.on.post("/api/agent/providers", {"preset": "openai", "api_key": "env:OPENAI_KEY"})
        p = r.get_json()["provider"]
        self.assertEqual((p["key_ref"], p["key_last4"]), ("env:OPENAI_KEY", None))
        self.assertIsNone(store.provider_secret(p["id"]))
        self.assertEqual(self.on.post("/api/agent/providers", {"preset": "openai", "key_ref": "file:/etc/shadow"}).status_code, 400)
        self.assertEqual(self.on.post("/api/agent/providers", {"preset": "openai", "key_ref": "file:/run/secrets/../../etc/x"}).status_code, 400)
        self.assertEqual(self.on.post("/api/agent/providers", {"preset": "openai", "key_ref": "file:/run/secrets/openai"}).status_code, 200)
        pid = self.on.post("/api/agent/providers", {"preset": "openai", "api_key": "sk-real-1234"}).get_json()["provider"]["id"]
        self.on.patch("/api/agent/providers/" + pid, {"api_key": ""})
        self.assertEqual(store.provider_secret(pid), "sk-real-1234")
        self.on.patch("/api/agent/providers/" + pid, {"api_key": None})
        self.assertIsNone(store.provider_secret(pid))

    def test_11_activity_filters(self):
        cid = store.conversation_create("disk report")
        rid = store.run_create(cid, "chat", True, "act", status="done")
        self.assertEqual([r["id"] for r in self.on.get("/api/agent/runs?q=disk").get_json()["items"]], [rid])
        self.assertEqual(self.on.get("/api/agent/runs?q=nothing").get_json()["items"], [])
        self.assertEqual(self.on.get("/api/agent/runs?since=%d" % (time.time() + 100)).get_json()["items"], [])

    def test_33_busy_matches_status(self):
        from backend.agent import maintenance
        store.run_create("c", "chat", True, "act", status="queued")
        self.assertIn("1 agent run(s) in progress", maintenance.busy())

    def test_36_settings_list_configured_channels(self):
        app.save_settings({"ntfy_topic": "lab"})
        try:
            self.assertEqual(self.on.get("/api/agent/settings").get_json()["channels_configured"], ["ntfy"])
        finally:
            app.save_settings({"ntfy_topic": ""})
