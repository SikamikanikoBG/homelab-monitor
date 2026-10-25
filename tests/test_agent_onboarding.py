"""The UAT-1 redesign's server side: setup state, permission presets, the setup
wizard's finish step, model recommendation, the Overview card, and the chat
extras (conversation title, follow-up questions)."""
import os
import time
import unittest
from unittest.mock import patch

from agent_fakes import AgentOn, FakeFleet, ScriptedAdapter, app, clear_login, wipe_agent_tables
from backend.agent import approvals, extras, onboarding, policy, providers, state, store
from backend.agent.providers.base import ModelInfo
from test_agent_loop import LoopCase, call


class NoPasswordCase(unittest.TestCase):
    def setUp(self):
        wipe_agent_tables()
        clear_login()
        self.env = patch.dict(os.environ, {"AGENT_ENABLED": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        wipe_agent_tables()

    def test_setup_state_is_open_and_says_password_first(self):
        c = app.app.test_client()
        with patch.object(onboarding, "found_count", return_value=2):
            j = c.get("/api/agent/setup?found=1").get_json()
        self.assertEqual((j["password_set"], j["next_step"], j["done"], j["found"]), (False, "password", False, 2))
        self.assertEqual(c.get("/api/agent/overview").status_code, 403)          # data needs the password

    def test_off_without_the_env_switch(self):
        self.env.stop()
        try:
            self.assertEqual(app.app.test_client().get("/api/agent/setup").status_code, 404)
        finally:
            self.env.start()


class PresetCase(unittest.TestCase):
    def setUp(self):
        self.on = AgentOn().start()
        self.fleet = FakeFleet({"lab1": {"family": "linux", "containers": ["web"]}}).start()

    def tearDown(self):
        self.fleet.stop()
        self.on.stop()

    def test_presets_set_every_host_and_new_hosts_follow(self):
        self.assertEqual(policy.mode_for("lab1"), "passive")                    # no preset: as before
        self.assertEqual(policy.rule_for("lab1", "container.restart")[0], "deny")
        self.assertEqual(self.on.put("/api/agent/policy/preset", {"preset": "ask"}).status_code, 200)
        self.assertEqual(policy.mode_for("lab1"), "active")
        self.assertEqual(policy.rule_for("lab1", "container.restart")[0], "ask")
        self.assertEqual(policy.mode_for("a-host-added-later"), "active")
        self.on.put("/api/agent/policy/preset", {"preset": "small"})
        low = [a for a in policy.catalog.actions() if a["risk"] == "low" and not a.get("sudo")]
        rest = [a for a in policy.catalog.actions() if a["risk"] in ("medium", "high") or a.get("sudo")]
        self.assertTrue(low and rest)
        self.assertTrue(all(policy.rule_for("lab1", a["id"])[0] == "allow" for a in low))
        self.assertTrue(all(policy.rule_for("lab1", a["id"])[0] == "ask" for a in rest if a["risk"] != "none"))
        self.on.put("/api/agent/policy/preset", {"preset": "look"})
        self.assertEqual(policy.mode_for("lab1"), "passive")
        self.assertEqual(self.on.put("/api/agent/policy/preset", {"preset": "everything"}).status_code, 400)

    def test_an_explicit_edit_wins_over_the_preset(self):
        self.on.put("/api/agent/policy/preset", {"preset": "small"})
        self.on.put("/api/agent/policy/hosts/lab1", {"mode": "passive"})
        self.on.put("/api/agent/policy/hosts/local/actions/container.restart", {"rule": "deny"})
        self.assertEqual(policy.mode_for("lab1"), "passive")
        self.assertEqual(policy.rule_for("local", "container.restart")[0], "deny")
        j = self.on.get("/api/agent/policy").get_json()
        self.assertEqual(j["preset"], "small")
        self.assertEqual(j["effective_rules"]["local"]["container.restart"]["rule"], "deny")

    def test_small_stuff_unattended_still_only_low_risk_under_the_limit(self):
        self.on.put("/api/agent/policy/preset", {"preset": "small"})
        low = next(a["id"] for a in policy.catalog.actions() if a["risk"] == "low" and not a.get("sudo")
                   and a.get("idempotent") and policy.catalog.supported(a, "lab1", "linux") and not a.get("params", {}).get("required"))
        d = policy.decide("lab1", low, {}, attended=False)
        self.assertIn(d.verdict, ("allow", "deny"))                       # deny only for a missing capability
        med = next(a["id"] for a in policy.catalog.actions() if a["risk"] == "medium")
        self.assertNotEqual(policy.decide("lab1", med, {"name": "web"}, attended=False).verdict, "allow")


class FinishCase(LoopCase):
    def test_finish_refuses_an_untested_model_and_unknown_routines(self):
        store.model_upsert(self.pid, "m2", tools="unknown")
        r = self.on.post("/api/agent/setup/finish", {"provider_id": self.pid, "model": "m2", "preset": "ask"})
        self.assertEqual((r.status_code, r.get_json()["error"]), (400, "model_not_ready"))
        r = self.on.post("/api/agent/setup/finish", {"provider_id": self.pid, "model": "m1", "templates": ["nope"]})
        self.assertEqual(r.get_json()["error"], "bad_template")

    def test_finish_sets_model_preset_routines_and_switches_on(self):
        state.put({"enabled": "0"})
        r = self.on.post("/api/agent/setup/finish", {"provider_id": self.pid, "model": "m1", "preset": "ask",
                                                     "templates": ["disk-space-watch", "docker-cleanup"]})
        j = r.get_json()
        self.assertEqual(r.status_code, 200, j)
        self.assertTrue(j["state"]["done"])
        self.assertEqual(policy.preset(), "ask")
        self.assertEqual((state.get("default_provider_id"), state.get("default_model")), (self.pid, "m1"))
        names = sorted(t["template_id"] for t in store.task_list())
        self.assertEqual(names, ["disk-space-watch", "docker-cleanup"])
        # again: nothing doubles
        j = self.on.post("/api/agent/setup/finish", {"templates": ["disk-space-watch"]}).get_json()
        self.assertEqual(j["skipped"][0]["why"], "already active")
        self.assertEqual(len(store.task_list()), 2)

    def test_starter_routines_are_flagged_in_the_template_data(self):
        starters = [t["id"] for t in self.on.get("/api/agent/templates").get_json()["templates"] if t["starter"]]
        self.assertEqual(sorted(starters), ["container-health", "disk-space-watch", "docker-cleanup"])

    def test_dismiss_is_remembered_on_the_server(self):
        self.assertFalse(self.on.get("/api/agent/setup").get_json()["dismissed"])
        self.on.post("/api/agent/setup/dismiss", {"dismissed": True})
        self.assertTrue(self.on.get("/api/agent/setup").get_json()["dismissed"])


class ModelChoiceCase(LoopCase):
    def test_recommendation_comes_from_probe_data(self):
        cloud = self.on.post("/api/agent/providers", {"preset": "openai", "api_key": "sk-test-123456"}).get_json()["provider"]["id"]
        store.model_upsert(cloud, "big-cloud", tools="yes", multi_step="yes", context_window=200000)
        store.model_upsert(self.pid, "m1", tools="yes", multi_step="yes", context_window=32768)
        store.model_upsert(self.pid, "m-big", tools="yes", multi_step="no", context_window=131072)
        store.model_upsert(self.pid, "embed", tools="no", context_window=999999)
        ms = self.on.get("/api/agent/setup/models").get_json()["models"]
        rec = [m["model"] for m in ms if m["recommended"]]
        self.assertEqual(rec, ["m-big"])                     # local first, then the largest window
        self.assertEqual(ms[0]["model"], "m-big")
        self.assertFalse(next(m for m in ms if m["model"] == "embed")["recommended"])

    def test_setup_discovery_adds_reachable_servers_and_lists_their_models(self):
        found = [{"host": "vader", "server": "vLLM", "provider_key": "vllm", "preset": "vllm",
                  "base_url": "http://10.0.0.9:8000/v1", "reachable": True, "models": ["qwen"], "provider_id": None},
                 {"host": "ardi", "server": "ollama", "provider_key": "ollama", "preset": "ollama",
                  "base_url": "http://10.0.0.8:11434", "reachable": False, "models": [], "provider_id": None}]

        class Lister:
            def list_models(self):
                return [ModelInfo("qwen", 131072)]

        with patch("backend.agent.providers.discovery.discover", return_value=found), \
                patch.object(providers, "adapter_for", lambda row: Lister()):
            j = self.on.post("/api/agent/setup/models").get_json()
        self.assertEqual(len(j["added"]), 1)
        q = next(m for m in j["models"] if m["model"] == "qwen")
        self.assertEqual((q["context_window"], q["tools"]), (131072, "unknown"))


class OverviewCase(LoopCase):
    def test_waiting_and_lately_from_the_runs_own_records(self):
        self.activate("lab1", container__restart="ask")
        t = self.on.post("/api/agent/tasks", {"name": "Stale containers", "prompt": "check", "hosts": ["lab1"],
                                              "cron": "0 3 * * *", "mode": "propose", "actions": ["container.restart"],
                                              "provider_id": self.pid, "model": "m1"}).get_json()["task"]
        self.script({"tool_calls": [call("container__restart", host="lab1", name="web")]},
                    {"text": "**Docker cleanup** on lab1 freed *12.4 GB*. Nothing else needed attention.\n\n| a | b |"})
        self.on.post("/api/agent/tasks/%s/run" % t["id"])
        j = self.on.get("/api/agent/overview").get_json()
        self.assertEqual(j["pending_count"], 1)
        w = j["waiting"][0]
        self.assertEqual((w["host"], w["task"], w["params"]), ("lab1", "Stale containers", {"name": "web"}))
        self.assertEqual(j["lately"][0]["line"], "Docker cleanup on lab1 freed 12.4 GB.")
        self.assertEqual(j["model"]["model"], "m1")

    def test_one_line(self):
        self.assertEqual(onboarding.one_line("| h |\n|---|\n## Result\nAll fine. More text here."), "Result")
        self.assertEqual(onboarding.one_line("Everything is healthy here today. The fullest is vader."),
                         "Everything is healthy here today.")
        long = onboarding.one_line("word " * 100)
        self.assertLessEqual(len(long), onboarding.LINE_MAX)
        self.assertTrue(long.endswith("…"))


class ExtrasCase(LoopCase):
    def test_fallback_title_never_shows_data_or_directives(self):
        self.assertEqual(extras.fallback_title('propose a restart. #stub [{"tool_calls": []}]'), "propose a restart")
        self.assertEqual(extras.fallback_title('{"a": 1}'), "")
        self.assertEqual(extras.fallback_title("how are the disks\nsecond line"), "how are the disks")
        self.assertLessEqual(len(extras.fallback_title("disk " * 40)), extras.TITLE_MAX + 1)

    def test_parse_reads_the_object_never_prose(self):
        self.assertIsNone(extras.parse("Title: Disks. You could ask: what fills /data?"))
        got = extras.parse('Sure: {"title": "Disk space across the fleet", "followups": ["What fills ardi /data?", '
                           '"What can I clean up?", "third"]}')
        self.assertEqual(got["title"], "Disk space across the fleet")
        self.assertEqual(got["followups"], ["What fills ardi /data?", "What can I clean up?"])
        self.assertIsNone(extras.parse('{"title": "' + "x" * 200 + '", "followups": []}')["title"])

    def test_after_the_first_answer_the_chat_gets_a_title_and_follow_ups(self):
        state.put({"chat_extras": "1"})
        self.script({"text": "Disks are fine."},
                    {"text": '{"title": "Disk space check", "followups": ["What fills /data?"]}'},
                    {"text": "Still fine."}, {"text": "not json at all"})
        rid = self.ask('how are the disks #stub [{"text": "x"}]')
        conv = self.on.get("/api/agent/conversations/" + self.cid).get_json()
        self.assertEqual(conv["conversation"]["title"], "Disk space check")
        self.assertEqual(self.run_detail(rid)["run"]["followups"], ["What fills /data?"])
        self.assertTrue(self.adapter.requests[1].no_tools is False and self.adapter.requests[1].tools == [])
        rid2 = self.ask("and now?")
        conv = self.on.get("/api/agent/conversations/" + self.cid).get_json()
        self.assertEqual(conv["conversation"]["title"], "Disk space check")       # only the first answer names it
        self.assertEqual(self.run_detail(rid2)["run"]["followups"], [])           # prose: nothing shown
        self.assertEqual(self.run_detail(rid)["run"]["status"], "done")
        self.assertEqual(self.run_detail(rid2)["run"]["status"], "done")

    def test_title_before_the_model_names_it(self):
        self.script({"text": "ok"})
        self.ask('restart web #stub [{"tool_calls": []}]')
        self.assertEqual(self.on.get("/api/agent/conversations/" + self.cid).get_json()["conversation"]["title"],
                         "restart web")


if __name__ == "__main__":
    unittest.main()
