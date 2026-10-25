"""The second cold-start tester pass (UAT-1, round 2): where a model server
runs, the agent's own model, a truthful health picture in one call, what each
run in Activity was, and the routines' next run on the Overview."""
import json
import unittest
from unittest.mock import patch

from agent_fakes import app
from backend.agent import ports, prompts, state, store, toolbox
from backend.agent.providers import discovery
from test_agent_loop import LoopCase, call


class WhereCase(LoopCase):
    def test_an_address_belongs_to_the_host_that_owns_it(self):
        self.assertEqual(ports.host_for_address("lab1-addr"), "lab1")          # its ssh target
        self.assertEqual(ports.host_for_address("127.0.0.1"), "local")
        self.assertIsNone(ports.host_for_address("100.76.27.18"))
        with app.HOST_DATA_LOCK:
            app.HOST_DATA["nas"]["data"]["host"]["net"] = {"ifaces": [{"name": "eth0", "ipv4": ["10.9.8.7/24"]}]}
        self.assertEqual(ports.host_for_address("10.9.8.7"), "nas")             # an address its probe reports

    def test_a_custom_server_is_not_put_on_the_hub(self):
        servers = [{"name": "vader-vllm", "host": "lab1-addr", "port": 8000, "provider": "vllm"},
                   {"name": "far", "host": "100.76.27.18", "port": 18021, "provider": "vllm"}]
        with patch.object(ports, "custom_ai_servers", return_value=servers), \
                patch.object(ports, "ai_servers", return_value=[]), \
                patch.object(ports, "remote_model_catalogs", return_value={}):
            found = {c["server"]: c["host"] for c in discovery.candidates()}
        self.assertEqual(found, {"vader-vllm": "lab1", "far": None})


class OwnModelCase(LoopCase):
    def test_rows_about_the_agents_own_model_say_so(self):
        data = {"loaded": [{"model": "m1", "vram": 9000}, {"model": "other", "vram": 10}],
                "callers": [{"server": "vllm", "model": "m1"}]}
        got = toolbox.mark_agent_model(data, "m1")
        self.assertTrue(got["loaded"][0]["in_use_by_agent"])
        self.assertNotIn("in_use_by_agent", got["loaded"][1])
        self.assertTrue(got["callers"][0]["in_use_by_agent"])
        self.assertEqual(toolbox.mark_agent_model(data, None), data)

    def test_the_read_tools_and_the_prompt_carry_it(self):
        import dataclasses
        sc = toolbox.Scope(agent_model="m1")
        fake = dataclasses.replace(toolbox.READ["get_ai_models"], handler=lambda **kw: {"loaded": [{"model": "m1"}]})
        with patch.dict(toolbox.READ, {"get_ai_models": fake}):
            ok, text = toolbox.call_read(sc, "get_ai_models", {})
        self.assertTrue(ok)
        self.assertTrue(json.loads(text)["untrusted_data"]["loaded"][0]["in_use_by_agent"])
        p = prompts.build(hosts=["lab1"], modes={}, rules={}, run_mode="act", attended=True,
                          self_model="m1", self_server="vLLM (host lab1)")
        self.assertIn("You are running on the model m1 served by vLLM (host lab1)", p)
        self.assertIn("never suggest unloading", p)

    def test_a_run_knows_its_model_and_where_it_is_served(self):
        self.script({"text": "ok"})
        self.ask()
        req = self.adapter.requests[0]
        self.assertIn("You are running on the model m1", req.system)
        self.assertIn("model-host", req.system)                    # the provider's address: not a fleet host


class HealthCase(LoopCase):
    def test_one_call_gives_the_dashboards_health_picture(self):
        self.activate("lab1")
        ok, text = toolbox.call_read(toolbox.Scope(), "list_hosts", {})
        self.assertTrue(ok, text)
        d = json.loads(text)["untrusted_data"]
        self.assertIn("attention", d)
        self.assertEqual(d["attention_count"], len(d["attention"]))
        lab1 = next(h for h in d["hosts"] if h["name"] == "lab1")
        self.assertEqual(lab1["vitals"]["containers"]["total"], 3)     # FakeFleet: web, worker, the injection name

    def test_the_prompt_takes_the_dashboards_events_as_the_truth(self):
        p = prompts.build(hosts=["lab1"], modes={}, rules={}, run_mode="act", attended=True)
        self.assertIn("the attention list in list_hosts is exactly what the Overview's event feed shows", p)
        self.assertIn("Count containers from each host's containers field", p)


class ActivityCase(LoopCase):
    def test_each_run_is_labelled_by_its_own_question_or_routine(self):
        self.script({"text": "a"}, {"text": "b"}, {"text": "c"})
        self.ask("how are the disks")
        self.ask("and the GPU?")
        t = self.on.post("/api/agent/tasks", {"name": "Morning check", "prompt": "check", "hosts": ["lab1"],
                                              "cron": "0 7 * * *", "mode": "report", "provider_id": self.pid,
                                              "model": "m1"}).get_json()["task"]
        self.on.post("/api/agent/tasks/%s/run" % t["id"])
        labels = sorted(r["label"] for r in self.on.get("/api/agent/runs").get_json()["items"])
        self.assertEqual(labels, ["Morning check", "and the GPU?", "how are the disks"])

    def test_overview_says_when_the_first_routine_runs(self):
        t = self.on.post("/api/agent/tasks", {"name": "Morning check", "prompt": "check", "hosts": ["lab1"],
                                              "cron": "0 7 * * *", "mode": "report", "provider_id": self.pid,
                                              "model": "m1"}).get_json()["task"]
        o = self.on.get("/api/agent/overview").get_json()
        self.assertEqual(o["lately"], [])
        self.assertEqual((o["routines"], o["next"][0]["task"], o["next"][0]["at"]), (1, "Morning check", t["next_fire"]))

    def test_settings_carry_the_currency(self):
        self.assertTrue(self.on.get("/api/agent/settings").get_json()["currency"])


if __name__ == "__main__":
    unittest.main()


class CustomServerPlaceCase(LoopCase):
    def test_the_collector_files_a_custom_server_under_the_host_that_owns_its_address(self):
        from backend import collectors
        hosts = [{"name": "vader", "ssh_target": "arsen@10.0.0.9:22"}, {"name": "nas", "ssh_target": "nas.lan"}]
        self.assertEqual(collectors._host_by_address("10.0.0.9", hosts), "vader")
        self.assertEqual(collectors._host_by_address("nas.lan", hosts), "nas")
        self.assertIsNone(collectors._host_by_address("100.76.27.18", hosts))
        self.assertEqual(collectors._resolve_fleet_host(collectors._host_by_address("10.0.0.9", hosts),
                                                        {"local", "vader", "nas"}), "vader")

    def test_the_agent_sees_a_far_server_at_its_address_not_on_the_hub(self):
        rows = {"loaded": [{"service": "vader-vllm", "host": "local", "model": "qwen"},
                           {"service": "ollama", "host": "local", "model": "llama"}]}
        got = toolbox.place_custom_servers(rows, {"vader-vllm": "100.76.27.18"})
        self.assertEqual((got["loaded"][0]["host"], got["loaded"][0]["not_on_the_hub"]), ("100.76.27.18", True))
        self.assertEqual(got["loaded"][1]["host"], "local")
        servers = [{"name": "vader-vllm", "host": "100.76.27.18", "port": 18021, "provider": "vllm", "fleet_host": ""}]
        with patch.object(ports, "custom_ai_servers", return_value=servers):
            self.assertEqual(toolbox.place_custom_servers(rows)["loaded"][0]["host"], "100.76.27.18")
