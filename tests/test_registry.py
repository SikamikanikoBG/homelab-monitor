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


class TestUsageRollup(unittest.TestCase):
    """Per-model usage rollup from llm_samples, joined onto the registry."""

    def setUp(self):
        self.now = int(time.time())
        with app.LOCK:
            app.DB.execute("DELETE FROM llm_samples")
            # gemma3:1b — 3 runs in window (tps 10, 20, 30), newest = 30
            for i, tps in enumerate([10.0, 20.0, 30.0]):
                app.DB.execute(
                    "INSERT INTO llm_samples(ts,model,tps,ttft_ms,prompt_tps,eval_count)"
                    " VALUES(?,?,?,?,?,?)",
                    (self.now - 100 + i, "gemma3:1b", tps, 50.0 + i, 5.0, 40))
            # llama3:8b recorded WITHOUT a tag → must normalize to :latest? No —
            # registry has 'llama3:8b'. Record matching exact tag, 1 run.
            app.DB.execute(
                "INSERT INTO llm_samples(ts,model,tps,ttft_ms,prompt_tps,eval_count)"
                " VALUES(?,?,?,?,?,?)",
                (self.now - 10, "llama3:8b", 42.0, 80.0, 9.0, 100))
            # An OLD row outside the 30d window — must be excluded.
            app.DB.execute(
                "INSERT INTO llm_samples(ts,model,tps,ttft_ms,prompt_tps,eval_count)"
                " VALUES(?,?,?,?,?,?)",
                (self.now - app._LLM_USAGE_WINDOW - 1000, "gemma3:1b", 999.0,
                 1.0, 1.0, 1))
            app.DB.commit()

    def tearDown(self):
        with app.LOCK:
            app.DB.execute("DELETE FROM llm_samples")
            app.DB.commit()

    def test_groups_by_model(self):
        u = app._llm_usage_by_model(now=self.now)
        g = u["gemma3:1b"]
        self.assertEqual(g["runs"], 3)               # old row excluded by window
        self.assertEqual(g["last_used"], self.now - 98)  # newest in-window ts
        self.assertEqual(g["avg_tps"], 20.0)         # (10+20+30)/3
        self.assertEqual(g["last_tps"], 30.0)        # tps of the newest row
        self.assertEqual(u["llama3:8b"]["runs"], 1)

    def test_bounded_window_excludes_old(self):
        u = app._llm_usage_by_model(now=self.now)
        # If the old row leaked in, runs would be 4 / avg would skew to ~265.
        self.assertEqual(u["gemma3:1b"]["runs"], 3)
        self.assertLess(u["gemma3:1b"]["avg_tps"], 100)

    def test_normalize_join_latest_suffix(self):
        # A bare name on either side folds to ':latest' so the join lands.
        self.assertEqual(app._normalize_model_name("gemma3"), "gemma3:latest")
        self.assertEqual(app._normalize_model_name("gemma3:1b"), "gemma3:1b")
        self.assertEqual(app._normalize_model_name(None), "")
        with app.LOCK:
            app.DB.execute(
                "INSERT INTO llm_samples(ts,model,tps,ttft_ms,prompt_tps,eval_count)"
                " VALUES(?,?,?,?,?,?)",
                (self.now - 5, "weird", 7.0, 12.0, 2.0, 9))  # bare → weird:latest
            app.DB.commit()
        u = app._llm_usage_by_model(now=self.now)
        self.assertIn("weird:latest", u)
        # registry entry 'weird:latest' must pick this up
        models = app._parse_model_registry(SAMPLE_TAGS, [])
        app._apply_usage_to_models(models, u)
        w = [m for m in models if m["name"] == "weird:latest"][0]
        self.assertEqual(w["runs"], 1)
        self.assertEqual(w["last_tps"], 7.0)

    def test_apply_join_lands_and_never_used(self):
        models = app._parse_model_registry(SAMPLE_TAGS, [])
        app._apply_usage_to_models(models, app._llm_usage_by_model(now=self.now))
        by = {m["name"]: m for m in models}
        self.assertEqual(by["gemma3:1b"]["runs"], 3)
        self.assertEqual(by["gemma3:1b"]["avg_tps"], 20.0)
        # weird:latest has NO samples → clean never-used shape, not missing/NaN
        self.assertEqual(by["weird:latest"]["runs"], 0)
        self.assertIsNone(by["weird:latest"]["last_used"])
        self.assertIsNone(by["weird:latest"]["avg_tps"])
        self.assertIsNone(by["weird:latest"]["last_tps"])

    def test_no_samples_dict_empty(self):
        with app.LOCK:
            app.DB.execute("DELETE FROM llm_samples")
            app.DB.commit()
        self.assertEqual(app._llm_usage_by_model(now=self.now), {})
        models = app._parse_model_registry(SAMPLE_TAGS, [])
        app._apply_usage_to_models(models, {})
        for m in models:
            self.assertEqual(m["runs"], 0)
            self.assertIsNone(m["last_used"])

    def test_endpoint_shape_includes_usage(self):
        # /api/models entries must carry the new usage fields, served via cache.
        real = app._fetch_model_registry
        en = app.COPILOT_ENABLED
        cache = app._REGISTRY_CACHE
        app.COPILOT_ENABLED = True
        app._REGISTRY_CACHE = None

        def fake_fetch():
            models = app._parse_model_registry(SAMPLE_TAGS, [])
            app._apply_usage_to_models(models, app._llm_usage_by_model(now=self.now))
            return models, True

        app._fetch_model_registry = fake_fetch
        try:
            j = app.app.test_client().get("/api/models").get_json()
        finally:
            app._fetch_model_registry = real
            app.COPILOT_ENABLED = en
            app._REGISTRY_CACHE = cache
        by = {m["name"]: m for m in j["models"]}
        self.assertIn("runs", by["gemma3:1b"])
        self.assertIn("last_used", by["gemma3:1b"])
        self.assertIn("avg_tps", by["gemma3:1b"])
        self.assertEqual(by["gemma3:1b"]["runs"], 3)


if __name__ == "__main__":
    unittest.main()
