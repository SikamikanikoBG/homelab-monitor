"""Which ollama the Benchmark Lab talks to (#350).

The AI-servers connectivity Test probes the ollama the user registered; the lab
used to talk only to COPILOT_OLLAMA_URL (127.0.0.1:11434 by default), so an
ollama on another box passed Test while Benchmarks said "not answering". The lab
now follows the registered ollama unless COPILOT_OLLAMA_URL was set explicitly.
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
from backend.api import benchmarks


def _settings(*servers):
    return {"custom_ai_servers": json.dumps(list(servers))}


OLLAMA_REMOTE = {"name": "desk-ollama", "host": "192.168.0.80", "port": 11434, "provider": "ollama"}
VLLM_REMOTE = {"name": "vader-vllm", "host": "vader", "port": 8010, "provider": "vllm"}


class TestDefaultEndpoint(unittest.TestCase):
    def setUp(self):
        self._url, self._set = app.COPILOT_OLLAMA_URL, app.COPILOT_OLLAMA_URL_SET
        app.COPILOT_OLLAMA_URL = "http://127.0.0.1:11434"
        app.COPILOT_OLLAMA_URL_SET = False

    def tearDown(self):
        app.COPILOT_OLLAMA_URL, app.COPILOT_OLLAMA_URL_SET = self._url, self._set

    def test_registered_ollama_is_used_when_env_not_set(self):
        with mock.patch.object(app, "get_settings", return_value=_settings(VLLM_REMOTE, OLLAMA_REMOTE)):
            self.assertEqual(benchmarks._default_endpoint(), "http://192.168.0.80:11434")

    def test_explicit_env_wins_over_registered(self):
        app.COPILOT_OLLAMA_URL = "http://gpu-box:11434"
        app.COPILOT_OLLAMA_URL_SET = True
        with mock.patch.object(app, "get_settings", return_value=_settings(OLLAMA_REMOTE)):
            self.assertEqual(benchmarks._default_endpoint(), "http://gpu-box:11434")

    def test_no_registered_ollama_falls_back_to_default(self):
        with mock.patch.object(app, "get_settings", return_value=_settings(VLLM_REMOTE)):
            self.assertEqual(benchmarks._default_endpoint(), "http://127.0.0.1:11434")

    def test_malformed_setting_falls_back_to_default(self):
        with mock.patch.object(app, "get_settings", return_value={"custom_ai_servers": "not json"}):
            self.assertEqual(benchmarks._default_endpoint(), "http://127.0.0.1:11434")

    def test_ipv6_host_is_bracketed(self):
        v6 = dict(OLLAMA_REMOTE, host="fd00::80")
        with mock.patch.object(app, "get_settings", return_value=_settings(v6)):
            self.assertEqual(benchmarks._default_endpoint(), "http://[fd00::80]:11434")


class TestTargetsFollowEndpoint(unittest.TestCase):
    def setUp(self):
        self._url, self._set = app.COPILOT_OLLAMA_URL, app.COPILOT_OLLAMA_URL_SET
        self._en = app.COPILOT_ENABLED
        app.COPILOT_OLLAMA_URL = "http://127.0.0.1:11434"
        app.COPILOT_OLLAMA_URL_SET = False
        app.COPILOT_ENABLED = True
        self.c = app.app.test_client()

    def tearDown(self):
        app.COPILOT_OLLAMA_URL, app.COPILOT_OLLAMA_URL_SET = self._url, self._set
        app.COPILOT_ENABLED = self._en

    def test_targets_read_models_from_the_registered_ollama(self):
        seen = []

        def fake_fetch(base=None):
            seen.append(base)
            return [{"name": "nomic-embed-text:latest"}], True

        with mock.patch.object(app, "get_settings", return_value=_settings(OLLAMA_REMOTE)), \
             mock.patch.object(app, "_fetch_model_registry", side_effect=fake_fetch), \
             mock.patch.object(app, "_model_registry", side_effect=AssertionError("localhost registry used")), \
             mock.patch.object(benchmarks, "_gpu_inventory", return_value=[]):
            j = self.c.get("/api/bench/targets").get_json()
        self.assertEqual(seen, ["http://192.168.0.80:11434"])
        self.assertTrue(j["reachable"])
        self.assertEqual(j["endpoint"], "http://192.168.0.80:11434")
        self.assertEqual([m["name"] for m in j["models"]], ["nomic-embed-text:latest"])

    def test_default_endpoint_keeps_using_the_cached_registry(self):
        with mock.patch.object(app, "get_settings", return_value=_settings()), \
             mock.patch.object(app, "_model_registry", return_value=([], False)) as reg, \
             mock.patch.object(benchmarks, "_gpu_inventory", return_value=[]):
            j = self.c.get("/api/bench/targets").get_json()
        reg.assert_called_once()
        self.assertFalse(j["reachable"])
        self.assertEqual(j["endpoint"], "http://127.0.0.1:11434")


if __name__ == "__main__":
    unittest.main()
