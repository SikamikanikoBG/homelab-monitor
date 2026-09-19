"""A custom AI server registered for a remote fleet host: its model rows carry
that host's name (so the hub's own AI Models panel doesn't list it), and a
server that stops answering is reported as such instead of vanishing.

Drives sample_once() with everything except the custom-server path mocked.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", ":memory:")
import app  # noqa: E402
from backend import collectors  # noqa: E402

CUSTOM = ('[{"name":"vllm-vader","host":"10.0.0.9","port":8010,"provider":"vllm","fleet_host":"vader"},'
          ' {"name":"vllm-hub","host":"127.0.0.1","port":8011,"provider":"vllm"}]')


class TestCustomServerStatus(unittest.TestCase):
    def _sample(self, probe):
        with patch("app.smi", side_effect=RuntimeError("no gpu")), \
             patch("app.amd_gpus", return_value=[]), \
             patch("app.containers", return_value=[]), \
             patch("app.sample_callers", return_value={}), \
             patch("app.collect_serving", return_value=[]), \
             patch("app.collect_model_meta", return_value={}), \
             patch("app.list_hosts", return_value=[{"name": "vader"}]), \
             patch("app.get_settings", return_value={"custom_ai_servers": CUSTOM}), \
             patch("backend.collectors.probe_custom_server", side_effect=probe), \
             patch("app.read_host", return_value={"cpu": 0, "ram_used": 0,
                                                  "ram_total": 0, "load1": 0, "ctemp": 0}):
            collectors.sample_once()

    def test_rows_carry_the_fleet_host_they_were_registered_for(self):
        def probe(desc):
            return [("qwen3-27b", None, None, None)] if desc["name"] == "vllm-vader" else [("phi-4", None, None, None)]
        self._sample(probe)
        by = {m["service"]: m for m in app.LATEST["models"]}
        self.assertEqual(by["vllm-vader"]["host"], "vader")
        self.assertEqual(by["vllm-hub"]["host"], "local")
        # and the fast-path re-probe list knows the same
        hosts = {s["name"]: s["host"] for s in app.AI_SERVERS}
        self.assertEqual(hosts, {"vllm-vader": "vader", "vllm-hub": "local"})
        st = {s["name"]: s for s in app.LATEST["custom_servers"]}
        self.assertTrue(st["vllm-vader"]["reachable"])
        self.assertEqual(st["vllm-vader"]["target"], "10.0.0.9:8010")
        self.assertEqual(st["vllm-vader"]["models"], 1)

    def test_unanswering_server_is_reported_not_dropped(self):
        def probe(desc):
            return [] if desc["name"] == "vllm-vader" else [("phi-4", None, None, None)]
        self._sample(probe)
        self.assertNotIn("vllm-vader", {m["service"] for m in app.LATEST["models"]})
        st = {s["name"]: s for s in app.LATEST["custom_servers"]}
        self.assertFalse(st["vllm-vader"]["reachable"])
        self.assertEqual(st["vllm-vader"]["host"], "vader")
        self.assertTrue(st["vllm-hub"]["reachable"])

    def test_ai_now_keeps_the_host_stamp(self):
        def probe(desc):
            return [("qwen3-27b", None, None, None)]
        self._sample(probe)
        models, _ = app.ai_models_now()
        self.assertEqual({m["service"]: m["host"] for m in models},
                         {"vllm-vader": "vader", "vllm-hub": "local"})


if __name__ == "__main__":
    unittest.main()
