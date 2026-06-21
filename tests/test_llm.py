"""Unit tests for the LLM-engine throughput surface (the AI Lab Cockpit).

Covers the honest-capture math + parsing + endpoint shape with no ollama in CI:
  • timing-field parse → tok/s + TTFT from a sample ollama generate response
  • side-channel capture updates the latest-measurement state, without touching
    the copilot's returned text
  • /api/ps parse → resident-model list incl. GPU/CPU split + keep-alive
  • /api/llm shape: always 200, graceful enabled:false when unreachable, the
    no-recent-generation null state, and no secret (URL) leak.
"""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


# A realistic non-streaming ollama /api/generate response (durations in ns).
SAMPLE_GEN = {
    "model": "gemma3:1b",
    "response": "ok",
    "done": True,
    "load_duration": 1_200_000_000,        # 1.2 s
    "prompt_eval_count": 40,
    "prompt_eval_duration": 200_000_000,   # 0.2 s → 200 tok/s
    "eval_count": 120,
    "eval_duration": 2_000_000_000,        # 2.0 s → 60 tok/s
}


class TestThroughputMath(unittest.TestCase):
    def test_tps_and_ttft_from_sample(self):
        m = app._llm_metrics_from_response(SAMPLE_GEN, "gemma3:1b")
        self.assertIsNotNone(m)
        self.assertEqual(m["tps"], 60.0)                 # 120 / 2.0 s
        self.assertEqual(m["prompt_tps"], 200.0)         # 40 / 0.2 s
        # TTFT = load_duration + prompt_eval_duration = 1.4 s = 1400 ms
        self.assertEqual(m["ttft_ms"], 1400.0)
        self.assertEqual(m["eval_count"], 120)
        self.assertEqual(m["prompt_eval_count"], 40)
        self.assertEqual(m["model"], "gemma3:1b")
        self.assertIsInstance(m["ts"], int)

    def test_no_timing_returns_none(self):
        # An empty/error response has no eval_count → no fabricated number.
        self.assertIsNone(app._llm_metrics_from_response({"response": ""}))
        self.assertIsNone(app._llm_metrics_from_response({"eval_count": 0,
                                                          "eval_duration": 0}))
        self.assertIsNone(app._llm_metrics_from_response(None))

    def test_partial_timing_degrades(self):
        # Missing prompt-eval still yields gen tok/s; prompt_tps falls to None.
        d = {"eval_count": 50, "eval_duration": 1_000_000_000}
        m = app._llm_metrics_from_response(d, "x")
        self.assertEqual(m["tps"], 50.0)
        self.assertIsNone(m["prompt_tps"])


class TestCapture(unittest.TestCase):
    def setUp(self):
        self._saved = app._LLM_LAST

    def tearDown(self):
        app._LLM_LAST = self._saved

    def test_capture_updates_latest(self):
        app._LLM_LAST = None
        app._capture_llm_metrics(SAMPLE_GEN)
        self.assertIsNotNone(app._LLM_LAST)
        self.assertEqual(app._LLM_LAST["tps"], 60.0)

    def test_capture_ignores_untimed_response(self):
        app._LLM_LAST = None
        app._capture_llm_metrics({"response": "hello"})  # no timing fields
        self.assertIsNone(app._LLM_LAST)

    def test_capture_never_raises(self):
        app._capture_llm_metrics("not a dict")
        app._capture_llm_metrics(None)


class TestResidentParse(unittest.TestCase):
    def test_parse_gpu_cpu_split_and_keepalive(self):
        from datetime import datetime, timezone
        now = time.time()
        exp = datetime.fromtimestamp(now + 300, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000000000Z")
        ps = {"models": [
            {"name": "gemma3:1b", "size": 2_000_000_000,
             "size_vram": 2_000_000_000, "expires_at": exp},
            {"name": "llama3:8b", "size": 8_000_000_000,
             "size_vram": 4_000_000_000},   # half offloaded to CPU
        ]}
        res = app._parse_resident_models(ps, now=now)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["name"], "gemma3:1b")
        self.assertEqual(res[0]["gpu_fraction"], 1.0)            # fully on GPU
        self.assertAlmostEqual(res[0]["keep_alive_sec"], 300, delta=3)
        self.assertEqual(res[1]["gpu_fraction"], 0.5)            # 50/50 split
        self.assertIsNone(res[1]["keep_alive_sec"])             # no expires_at

    def test_parse_empty_and_garbage(self):
        self.assertEqual(app._parse_resident_models({}), [])
        self.assertEqual(app._parse_resident_models(None), [])
        self.assertEqual(app._parse_resident_models({"models": [{}]}), [])


class TestEndpoint(unittest.TestCase):
    def setUp(self):
        self._url = app.COPILOT_OLLAMA_URL
        self._en = app.COPILOT_ENABLED
        self._last = app._LLM_LAST
        self.c = app.app.test_client()

    def tearDown(self):
        app.COPILOT_OLLAMA_URL = self._url
        app.COPILOT_ENABLED = self._en
        app._LLM_LAST = self._last

    def test_shape_and_always_200_unreachable(self):
        app.COPILOT_ENABLED = True
        app.COPILOT_OLLAMA_URL = "http://127.0.0.1:1"   # dead port
        app._LLM_LAST = None
        r = self.c.get("/api/llm")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertFalse(j["ollama_reachable"])
        self.assertEqual(j["resident"], [])
        self.assertIsNone(j["last"])                     # no-recent-generation
        self.assertIn("model", j)
        self.assertIn("enabled", j)

    def test_disabled_returns_enabled_false(self):
        app.COPILOT_ENABLED = False
        r = self.c.get("/api/llm")
        j = r.get_json()
        self.assertFalse(j["enabled"])
        self.assertEqual(j["resident"], [])

    def test_last_populated_with_age(self):
        app.COPILOT_ENABLED = True
        app.COPILOT_OLLAMA_URL = "http://127.0.0.1:1"
        app._LLM_LAST = dict(app._llm_metrics_from_response(SAMPLE_GEN, "gemma3:1b"))
        app._LLM_LAST["ts"] = int(time.time()) - 5
        j = self.c.get("/api/llm").get_json()
        self.assertIsNotNone(j["last"])
        self.assertEqual(j["last"]["tps"], 60.0)
        self.assertGreaterEqual(j["last"]["age_sec"], 4)
        self.assertNotIn("ts", j["last"])                # internal ts not echoed

    def test_no_secret_leak(self):
        # The configured URL (which could carry creds) must never appear.
        app.COPILOT_ENABLED = True
        app.COPILOT_OLLAMA_URL = "http://user:secret@127.0.0.1:1"
        body = self.c.get("/api/llm").get_data(as_text=True)
        self.assertNotIn("secret", body)
        self.assertNotIn("127.0.0.1:1", body)

    def test_history_field_present_and_empty_when_no_samples(self):
        app.COPILOT_ENABLED = True
        app.COPILOT_OLLAMA_URL = "http://127.0.0.1:1"
        with app.LOCK:
            app.DB.execute("DELETE FROM llm_samples")
            app.DB.commit()
        j = self.c.get("/api/llm").get_json()
        self.assertIn("history", j)
        self.assertEqual(j["history"], [])


def _clear_llm_samples():
    with app.LOCK:
        app.DB.execute("DELETE FROM llm_samples")
        app.DB.commit()


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self._saved = app._LLM_LAST
        _clear_llm_samples()

    def tearDown(self):
        app._LLM_LAST = self._saved
        _clear_llm_samples()

    def _count(self):
        with app.LOCK:
            return app.DB.execute("SELECT COUNT(*) FROM llm_samples").fetchone()[0]

    def test_capture_inserts_a_row(self):
        app._capture_llm_metrics(SAMPLE_GEN)
        self.assertEqual(self._count(), 1)
        hist = app._llm_history()
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["tps"], 60.0)
        self.assertEqual(hist[0]["ttft_ms"], 1400.0)

    def test_untimed_response_inserts_nothing(self):
        app._capture_llm_metrics({"response": "hi"})  # no timing → no row
        self.assertEqual(self._count(), 0)

    def test_retention_caps_rows(self):
        # Push well past the ring cap; rows should be trimmed to the cap.
        cap = app._LLM_SAMPLE_CAP
        now = int(time.time())
        with app.LOCK:
            app.DB.executemany(
                "INSERT INTO llm_samples(ts,model,tps,ttft_ms,prompt_tps,eval_count) "
                "VALUES(?,?,?,?,?,?)",
                [(now - (cap + 50 - i), "m", float(i), 100.0, None, i)
                 for i in range(cap + 50)])
            app.DB.commit()
        # A fresh capture triggers the trim.
        app._capture_llm_metrics(SAMPLE_GEN)
        self.assertLessEqual(self._count(), cap)

    def test_retention_drops_old_rows(self):
        now = int(time.time())
        with app.LOCK:
            app.DB.execute(
                "INSERT INTO llm_samples(ts,model,tps,ttft_ms,prompt_tps,eval_count) "
                "VALUES(?,?,?,?,?,?)",
                (now - app.RETENTION - 86400, "old", 1.0, 1.0, None, 1))
            app.DB.commit()
        app._capture_llm_metrics(SAMPLE_GEN)  # triggers retention DELETE
        with app.LOCK:
            old = app.DB.execute(
                "SELECT COUNT(*) FROM llm_samples WHERE ts < ?",
                (now - app.RETENTION,)).fetchone()[0]
        self.assertEqual(old, 0)

    def test_history_oldest_first(self):
        now = int(time.time())
        with app.LOCK:
            for i, t in enumerate((now - 30, now - 20, now - 10)):
                app.DB.execute(
                    "INSERT INTO llm_samples(ts,model,tps,ttft_ms,prompt_tps,eval_count) "
                    "VALUES(?,?,?,?,?,?)", (t, "m", float(i), 50.0, None, i))
            app.DB.commit()
        hist = app._llm_history()
        ts = [h["ts"] for h in hist]
        self.assertEqual(ts, sorted(ts))  # oldest → newest

    def test_persist_db_error_does_not_propagate(self):
        # A broken DB write must never break the copilot path: capture still
        # updates _LLM_LAST and never raises.
        app._LLM_LAST = None
        real = app.DB
        try:
            class _Boom:
                def execute(self, *a, **k):
                    raise RuntimeError("db down")
            app.DB = _Boom()
            app._capture_llm_metrics(SAMPLE_GEN)   # must not raise
        finally:
            app.DB = real
        self.assertIsNotNone(app._LLM_LAST)
        self.assertEqual(app._LLM_LAST["tps"], 60.0)


class TestPromExport(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()
        self._last = app._LLM_LAST
        self._url = app.COPILOT_OLLAMA_URL
        self._en = app.COPILOT_ENABLED

    def tearDown(self):
        app._LLM_LAST = self._last
        app.COPILOT_OLLAMA_URL = self._url
        app.COPILOT_ENABLED = self._en

    def test_gauges_emitted_when_measurement_exists(self):
        app._LLM_LAST = dict(app._llm_metrics_from_response(SAMPLE_GEN, "gemma3:1b"))
        body = self.c.get("/metrics").get_data(as_text=True)
        self.assertIn("homelab_llm_tokens_per_second", body)
        self.assertIn("homelab_llm_ttft_ms", body)
        self.assertIn('model="gemma3:1b"', body)
        # latest tok/s value appears on the gauge line
        line = [l for l in body.splitlines()
                if l.startswith("homelab_llm_tokens_per_second{")][0]
        self.assertIn("60.0", line)

    def test_no_tps_gauge_when_no_measurement(self):
        app._LLM_LAST = None
        # unreachable ollama → resident gauge also absent (not a fake 0)
        app.COPILOT_ENABLED = True
        app.COPILOT_OLLAMA_URL = "http://127.0.0.1:1"
        body = self.c.get("/metrics").get_data(as_text=True)
        self.assertNotIn("homelab_llm_tokens_per_second", body)
        self.assertNotIn("homelab_llm_ttft_ms", body)

    def test_metrics_still_parses_with_llm_gauges(self):
        from test_metrics import parse_exposition
        app._LLM_LAST = dict(app._llm_metrics_from_response(SAMPLE_GEN, "gemma3:1b"))
        helps, types, _ = parse_exposition(
            self.c.get("/metrics").get_data(as_text=True))
        self.assertEqual(len(helps), len(set(helps)))
        self.assertEqual(len(types), len(set(types)))
        self.assertIn("homelab_llm_tokens_per_second", types)


if __name__ == "__main__":
    unittest.main()
