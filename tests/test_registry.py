"""Unit tests for the Model Registry — the on-disk inventory of ollama models.

Covers the pure parse/totals math + endpoint shape with no ollama in CI:
  • /api/tags parse → name/size/family/param_size/quant/modified
  • bytes → GB rounding
  • cross-ref the resident set → loaded flag + live VRAM
  • totals: count, loaded, total bytes/GB
  • largest-first sort
  • /api/models shape: always 200, graceful enabled:false / models:[] when the
    LLM is disabled or ollama is unreachable, never 500
  • no secret (URL) leak
  • the ~45s cache: a second call inside the TTL does not re-fetch
"""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


# A realistic ollama GET /api/tags payload (sizes in bytes).
SAMPLE_TAGS = {
    "models": [
        {
            "name": "gemma3:1b",
            "size": 815_319_791,
            "modified_at": "2026-06-10T12:00:00.000000000Z",
            "details": {"family": "gemma3", "parameter_size": "1B",
                        "quantization_level": "Q4_K_M"},
        },
        {
            "name": "llama3:8b",
            "size": 4_661_211_808,
            "modified_at": "2026-06-18T08:30:00Z",
            "details": {"family": "llama", "parameter_size": "8.0B",
                        "quantization_level": "Q4_0"},
        },
        {
            # No details block, no modified field — must still parse.
            "name": "weird:latest",
            "size": 0,
        },
    ]
}

# Resident set as produced by _parse_resident_models (only llama3 is loaded).
SAMPLE_RESIDENT = [
    {"name": "llama3:8b", "size_mb": 4445, "vram_mb": 4096,
     "gpu_fraction": 1.0, "keep_alive_sec": 240},
]


class TestRegistryParse(unittest.TestCase):
    def test_fields_and_bytes_to_gb(self):
        out = app._parse_model_registry(SAMPLE_TAGS, [])
        self.assertEqual(len(out), 3)
        by = {m["name"]: m for m in out}
        g = by["gemma3:1b"]
        self.assertEqual(g["size_bytes"], 815_319_791)
        self.assertEqual(g["size_gb"], round(815_319_791 / 1073741824, 2))
        self.assertEqual(g["family"], "gemma3")
        self.assertEqual(g["param_size"], "1B")
        self.assertEqual(g["quant"], "Q4_K_M")
        self.assertEqual(g["modified"], "2026-06-10T12:00:00.000000000Z")

    def test_missing_details_degrades_cleanly(self):
        out = app._parse_model_registry(SAMPLE_TAGS, [])
        w = [m for m in out if m["name"] == "weird:latest"][0]
        self.assertIsNone(w["family"])
        self.assertIsNone(w["param_size"])
        self.assertIsNone(w["quant"])
        self.assertIsNone(w["modified"])
        self.assertEqual(w["size_bytes"], 0)
        self.assertEqual(w["size_gb"], 0.0)
        self.assertFalse(w["loaded"])

    def test_largest_first_sort(self):
        out = app._parse_model_registry(SAMPLE_TAGS, [])
        sizes = [m["size_bytes"] for m in out]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual(out[0]["name"], "llama3:8b")  # the biggest

    def test_cross_ref_loaded_flag_and_vram(self):
        out = app._parse_model_registry(SAMPLE_TAGS, SAMPLE_RESIDENT)
        by = {m["name"]: m for m in out}
        self.assertTrue(by["llama3:8b"]["loaded"])
        self.assertEqual(by["llama3:8b"]["vram_mb"], 4096)
        self.assertFalse(by["gemma3:1b"]["loaded"])
        self.assertIsNone(by["gemma3:1b"]["vram_mb"])

    def test_empty_and_garbage(self):
        self.assertEqual(app._parse_model_registry({}, []), [])
        self.assertEqual(app._parse_model_registry(None, None), [])
        self.assertEqual(app._parse_model_registry({"models": [{}]}, []), [])


class TestRegistryTotals(unittest.TestCase):
    def test_totals(self):
        out = app._parse_model_registry(SAMPLE_TAGS, SAMPLE_RESIDENT)
        t = app._registry_totals(out)
        self.assertEqual(t["count"], 3)
        self.assertEqual(t["loaded"], 1)
        self.assertEqual(t["total_bytes"], 815_319_791 + 4_661_211_808 + 0)
        self.assertEqual(t["total_gb"], round(t["total_bytes"] / 1073741824, 2))

    def test_empty_totals(self):
        t = app._registry_totals([])
        self.assertEqual(t, {"count": 0, "loaded": 0,
                             "total_bytes": 0, "total_gb": 0.0})


class TestEndpoint(unittest.TestCase):
    def setUp(self):
        self._url = app.COPILOT_OLLAMA_URL
        self._en = app.COPILOT_ENABLED
        self._cache = app._REGISTRY_CACHE
        app._REGISTRY_CACHE = None
        self.c = app.app.test_client()

    def tearDown(self):
        app.COPILOT_OLLAMA_URL = self._url
        app.COPILOT_ENABLED = self._en
        app._REGISTRY_CACHE = self._cache

    def test_always_200_unreachable(self):
        app.COPILOT_ENABLED = True
        app.COPILOT_OLLAMA_URL = "http://127.0.0.1:1"   # dead port
        r = self.c.get("/api/models")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertFalse(j["ollama_reachable"])
        self.assertEqual(j["models"], [])
        self.assertEqual(j["totals"]["count"], 0)
        self.assertIn("enabled", j)

    def test_disabled_returns_enabled_false_empty(self):
        app.COPILOT_ENABLED = False
        j = self.c.get("/api/models").get_json()
        self.assertFalse(j["enabled"])
        self.assertEqual(j["models"], [])

    def test_no_secret_leak(self):
        app.COPILOT_ENABLED = True
        app.COPILOT_OLLAMA_URL = "http://user:secret@127.0.0.1:1"
        body = self.c.get("/api/models").get_data(as_text=True)
        self.assertNotIn("secret", body)
        self.assertNotIn("127.0.0.1:1", body)


class TestCache(unittest.TestCase):
    def setUp(self):
        self._cache = app._REGISTRY_CACHE
        self._en = app.COPILOT_ENABLED
        app._REGISTRY_CACHE = None
        app.COPILOT_ENABLED = True

    def tearDown(self):
        app._REGISTRY_CACHE = self._cache
        app.COPILOT_ENABLED = self._en

    def test_serves_cache_inside_ttl_without_refetch(self):
        calls = {"n": 0}

        def fake_fetch():
            calls["n"] += 1
            return list(app._parse_model_registry(SAMPLE_TAGS, [])), True

        real = app._fetch_model_registry
        app._fetch_model_registry = fake_fetch
        try:
            m1, r1 = app._model_registry(now=1000.0)
            m2, r2 = app._model_registry(now=1000.0 + app._REGISTRY_TTL - 1)
        finally:
            app._fetch_model_registry = real
        self.assertEqual(calls["n"], 1)          # second call served from cache
        self.assertTrue(r1 and r2)
        self.assertEqual([m["name"] for m in m1], [m["name"] for m in m2])

    def test_refetches_after_ttl(self):
        calls = {"n": 0}

        def fake_fetch():
            calls["n"] += 1
            return [], True

        real = app._fetch_model_registry
        app._fetch_model_registry = fake_fetch
        try:
            app._model_registry(now=2000.0)
            app._model_registry(now=2000.0 + app._REGISTRY_TTL + 1)
        finally:
            app._fetch_model_registry = real
        self.assertEqual(calls["n"], 2)          # stale → re-fetched


if __name__ == "__main__":
    unittest.main()
