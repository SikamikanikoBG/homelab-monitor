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
  • #219: PROBES provider-key lookup + merging the ollama disk registry with the
    fleet's multi-provider model_catalog (vLLM, llama.cpp, …) into one list
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

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


class TestMatchProbeKey(unittest.TestCase):
    def test_matches_by_image_or_name(self):
        self.assertEqual(app._match_probe_key({"image": "ollama/ollama", "name": "ollama"}), "ollama")
        self.assertEqual(app._match_probe_key({"image": "vllm/vllm-openai:latest", "name": "vllm-server"}), "vllm")
        self.assertEqual(app._match_probe_key({"image": "ghcr.io/lm-studio/server", "name": "lmstudio"}),
                          "lmstudio")

    def test_no_match_returns_none(self):
        self.assertIsNone(app._match_probe_key({"image": "nginx:latest", "name": "reverse-proxy"}))

    def test_key_matches_fn_from_match_probe(self):
        # _match_probe and _match_probe_key must agree on which PROBES row wins.
        ct = {"image": "vllm/vllm-openai:latest", "name": "vllm-server"}
        key = app._match_probe_key(ct)
        fn = app._match_probe(ct)
        expected_fn = dict(app.PROBES)[key]
        self.assertIs(fn, expected_fn)


class TestMergeRegistry(unittest.TestCase):
    def test_ollama_entries_tagged_and_kept(self):
        ollama = app._parse_model_registry(SAMPLE_TAGS, [])
        out = app._merge_registry(ollama, [])
        self.assertEqual(len(out), 3)
        self.assertTrue(all(m["provider"] == "ollama" and m["host"] == "local" for m in out))

    def test_vllm_catalog_entry_merged_in(self):
        catalog = [{"host": "gpu-node-01",
                    "service": "vllm-server",
                    "provider": "vllm",
                    "model": "mistral-7b-instruct",
                    "loaded": True,
                    "vram_mb": 5200}]
        out = app._merge_registry([], catalog)
        self.assertEqual(len(out), 1)
        m = out[0]
        self.assertEqual(m["name"], "mistral-7b-instruct")
        self.assertEqual(m["provider"], "vllm")
        self.assertEqual(m["host"], "gpu-node-01")
        self.assertTrue(m["loaded"])
        self.assertEqual(m["vram_mb"], 5200)
        self.assertIsNone(m["size_bytes"])   # vLLM's /v1/models has no on-disk size

    def test_catalog_ollama_entries_deduped_against_disk_registry(self):
        ollama = app._parse_model_registry(SAMPLE_TAGS, [])
        # The PROBES ollama probe would ALSO report gemma3:1b via /api/tags — the
        # richer disk registry entry must win, not a duplicate lightweight one.
        catalog = [{"service": "ollama", "provider": "ollama", "model": "gemma3:1b",
                    "loaded": False, "vram_mb": None}]
        out = app._merge_registry(ollama, catalog)
        self.assertEqual(len(out), 3)   # still just the 3 ollama disk entries

    def test_remote_ollama_entries_pass_through_with_detail(self):
        # THE #236 regression: every provider=='ollama' catalog entry was
        # dropped, which blinded the fleet registry to remote hosts' ollama —
        # the exact hosts the fleet slice exists for. Remote entries (host set,
        # not the hub) must survive, with their registry detail carried.
        catalog = [{"host": "vader", "service": "ollama", "provider": "ollama",
                    "model": "glm-air:80k", "loaded": True, "vram_mb": 62461,
                    "size_bytes": 65495378161, "family": "glm4moe",
                    "param_size": "110.5B", "quant": "Q3_K_M",
                    "modified": "2026-07-20T10:00:00Z"}]
        out = app._merge_registry([], catalog)
        self.assertEqual(len(out), 1)
        m = out[0]
        self.assertEqual((m["host"], m["provider"]), ("vader", "ollama"))
        self.assertTrue(m["loaded"])
        self.assertEqual(m["vram_mb"], 62461)
        self.assertEqual(m["param_size"], "110.5B")
        self.assertEqual(m["size_gb"], round(65495378161 / 1073741824, 2))

    def test_hub_hostname_ollama_entries_still_deduped(self):
        # The hub's own probe stamps its real hostname — those are covered by
        # the richer disk registry and must still be dropped like host-less ones.
        with mock.patch.object(app.socket, "gethostname", return_value="hubbox"):
            out = app._merge_registry([], [{"host": "hubbox", "provider": "ollama",
                                            "model": "gemma3:1b", "loaded": False}])
        self.assertEqual(out, [])

    def test_catalog_entries_missing_model_name_skipped(self):
        catalog = [{"service": "x", "provider": "vllm", "model": None}]
        self.assertEqual(app._merge_registry([], catalog), [])

    def test_same_model_same_provider_different_hosts_kept(self):
        catalog = [
            {"host": "gpu-node-01", "service": "a", "provider": "vllm",
             "model": "llama3", "loaded": True, "vram_mb": 1000},
            {"host": "gpu-node-02", "service": "b", "provider": "vllm",
             "model": "llama3", "loaded": False, "vram_mb": None},
        ]
        out = app._merge_registry([], catalog)
        self.assertEqual(len(out), 2)
        self.assertEqual(
            {m["host"] for m in out},
            {"gpu-node-01", "gpu-node-02"}
        )

    def test_same_name_two_providers_both_kept(self):
        catalog = [{"service": "a", "provider": "vllm", "model": "llama3", "loaded": True, "vram_mb": 1},
                   {"service": "b", "provider": "llama.cpp", "model": "llama3", "loaded": False, "vram_mb": None}]
        out = app._merge_registry([], catalog)
        self.assertEqual({m["provider"] for m in out}, {"vllm", "llama.cpp"})

    def test_totals_across_providers(self):
        ollama = app._parse_model_registry(SAMPLE_TAGS, [])
        catalog = [{"service": "vllm-server", "provider": "vllm", "model": "mistral-7b-instruct",
                    "loaded": True, "vram_mb": 5200}]
        out = app._merge_registry(ollama, catalog)
        t = app._registry_totals(out)
        self.assertEqual(t["count"], 4)
        self.assertEqual(t["loaded"], 1)   # only mistral (vllm) — ollama fixture has no resident set


class TestEndpoint(unittest.TestCase):
    def setUp(self):
        self._url = app.COPILOT_OLLAMA_URL
        self._en = app.COPILOT_ENABLED
        self._cache = app._REGISTRY_CACHE
        self._catalog = app.LATEST.get("model_catalog")
        app._REGISTRY_CACHE = None
        app.LATEST["model_catalog"] = []
        self.c = app.app.test_client()

    def tearDown(self):
        app.COPILOT_OLLAMA_URL = self._url
        app.COPILOT_ENABLED = self._en
        app._REGISTRY_CACHE = self._cache
        app.LATEST["model_catalog"] = self._catalog

    def test_always_200_unreachable(self):
        app.COPILOT_ENABLED = True
        app.COPILOT_OLLAMA_URL = "http://127.0.0.1:1"   # dead port
        r = self.c.get("/api/models")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertFalse(j["ollama_reachable"])
        self.assertEqual(j["models"], [])
        self.assertEqual(j["totals"]["count"], 0)
        self.assertEqual(j["providers"], [])
        self.assertIn("enabled", j)

    def test_merges_catalog_from_other_providers(self):
        app.COPILOT_ENABLED = True
        app.COPILOT_OLLAMA_URL = "http://127.0.0.1:1"   # dead port — no ollama entries
        app.LATEST["model_catalog"] = [
            {"service": "vllm-server", "provider": "vllm", "model": "mistral-7b-instruct",
             "loaded": True, "vram_mb": 5200},
        ]
        j = self.c.get("/api/models").get_json()
        self.assertEqual(j["providers"], ["vllm"])
        self.assertEqual(len(j["models"]), 1)
        self.assertEqual(j["models"][0]["provider"], "vllm")

    def test_remote_catalog_is_keyed_by_the_registered_host_name(self):
        """A remote's probe reports its own socket.gethostname(), which need not
        match the name the host is registered under. The dashboard filters by the
        REGISTERED name, so /api/models must speak that name — otherwise the host's
        models are silently invisible on its own AI Models tab."""
        app.COPILOT_ENABLED = True
        app.COPILOT_OLLAMA_URL = "http://127.0.0.1:1"   # dead port — no hub entries
        with app.HOST_DATA_LOCK:
            app.HOST_DATA["Work"] = {"data": {"model_catalog": [
                {"host": "DESKTOP-ABC", "service": "ollama", "provider": "ollama",
                 "model": "qwen3:8b", "loaded": True, "vram_mb": 5200},
            ]}}
        try:
            j = self.c.get("/api/models").get_json()
        finally:
            with app.HOST_DATA_LOCK:
                app.HOST_DATA.pop("Work", None)
        self.assertEqual(len(j["models"]), 1)
        self.assertEqual(j["models"][0]["host"], "Work")          # registered name wins
        self.assertEqual(j["models"][0]["name"], "qwen3:8b")

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


class TestResolveFleetHost(unittest.TestCase):
    """A custom server's fleet_host → the fleet name its models are stamped with.
    The per-host AI Models tab groups by fleet name, so this is what makes a
    hub-probed vLLM show up under 'vader' instead of the hub's hostname."""
    @classmethod
    def setUpClass(cls):
        from backend import collectors
        # A plain module function — call it unbound, `self.resolve(...)` would
        # hand `self` in as `stored`.
        cls.resolve = staticmethod(collectors._resolve_fleet_host)

    def test_blank_and_local_are_the_hub(self):
        for stored in (None, "", "local"):
            self.assertEqual(self.resolve(stored, {"local", "vader"}), "local")

    def test_known_host_is_itself(self):
        self.assertEqual(self.resolve("vader", {"local", "vader", "cloudy"}), "vader")

    def test_removed_host_degrades_to_hub(self):
        # 'vader' was deleted after the server was registered — degrade to the
        # hub (always visible) rather than vanish into a name no tab has.
        self.assertEqual(self.resolve("vader", {"local", "cloudy"}), "local")

    def test_case_and_whitespace_are_not_clever(self):
        # A fleet name is exact-match; we don't normalise, so a typo stays a
        # "removed" host and degrades to the hub.
        self.assertEqual(self.resolve("Vader", {"local", "vader"}), "local")


if __name__ == "__main__":
    unittest.main()
