"""AI-tab fast path: ai_models_now() throttled ollama re-probe + /api/ai/now.

The endpoint exists so the AI Models tab can poll every few seconds without
touching the DB or LOCK; ai_models_now() re-probes ollama at most every
_AI_NOW_TTL seconds and merges over LATEST for everything else.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _reset():
    app._AI_NOW_CACHE.update(at=0.0, models=None)
    app.AI_SERVERS = [{"name": "ollama", "ip": "1.2.3.4", "provider": "ollama"},
                      {"name": "whisperx", "ip": "1.2.3.5", "provider": "whisper"}]
    app.LATEST["models"] = [
        {"service": "ollama", "model": "stale:8b", "vram": 111, "ram": 0, "ctx_now": None},
        {"service": "whisperx", "model": "Whisper ASR webservice", "vram": 588, "ram": None, "ctx_now": None},
    ]


class TestAiModelsNow(unittest.TestCase):
    def setUp(self):
        _reset()

    def tearDown(self):
        app._AI_NOW_CACHE.update(at=0.0, models=None)
        app.AI_SERVERS = []

    def test_fresh_probe_replaces_only_ollama_rows(self):
        with patch("app.probe_ollama",
                   return_value=[("fresh:30b", 15000.0, 5000.0, 65536)]) as po:
            models, _at = app.ai_models_now()
        po.assert_called_once_with("1.2.3.4", 11434)        # non-ollama servers not probed
        by = {m["model"]: m for m in models}
        self.assertNotIn("stale:8b", by)                    # replaced by the live view
        self.assertEqual(by["fresh:30b"],
                         {"service": "ollama", "model": "fresh:30b",
                          "vram": 15000, "ram": 5000, "ctx_now": 65536})
        self.assertEqual(by["Whisper ASR webservice"]["vram"], 588)   # kept from LATEST

    def test_second_call_within_ttl_served_from_cache(self):
        with patch("app.probe_ollama", return_value=[("m", 100.0, 0.0, 4096)]) as po:
            app.ai_models_now()
            app.ai_models_now()
        self.assertEqual(po.call_count, 1)

    def test_probe_failure_keeps_sampler_view(self):
        with patch("app.probe_ollama", side_effect=OSError("down")):
            models, _at = app.ai_models_now()
        self.assertIn("stale:8b", {m["model"] for m in models})

    def test_empty_probe_keeps_sampler_view(self):
        # ollama briefly unreachable → probe returns [] → don't blank the tab.
        with patch("app.probe_ollama", return_value=[]):
            models, _at = app.ai_models_now()
        self.assertIn("stale:8b", {m["model"] for m in models})

    def test_idle_fallback_rows_normalize(self):
        # /api/tags fallback rows are 2-wide (name, None) — must not crash and
        # must come through as idle entries.
        with patch("app.probe_ollama", return_value=[("pulled:8b", None)]):
            models, _at = app.ai_models_now()
        by = {m["model"]: m for m in models}
        self.assertEqual(by["pulled:8b"],
                         {"service": "ollama", "model": "pulled:8b",
                          "vram": None, "ram": None, "ctx_now": None})


class TestApiAiNow(unittest.TestCase):
    def setUp(self):
        _reset()
        app.LATEST["model_meta"] = {"fresh:30b": {"weights_mb": 17700}}

    def tearDown(self):
        app._AI_NOW_CACHE.update(at=0.0, models=None)
        app.AI_SERVERS = []

    def test_endpoint_shape(self):
        client = app.app.test_client()
        with patch("app.probe_ollama", return_value=[("fresh:30b", 15000.0, 5000.0, 65536)]):
            r = client.get("/api/ai/now")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertIn("models", j)
        self.assertIn("model_meta", j)
        self.assertIn("probed_at", j)
        by = {m["model"]: m for m in j["models"]}
        self.assertEqual(by["fresh:30b"]["ctx_now"], 65536)
        self.assertEqual(j["model_meta"]["fresh:30b"]["weights_mb"], 17700)


if __name__ == "__main__":
    unittest.main()
