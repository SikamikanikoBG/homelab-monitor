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


class TestEnergyMigration(unittest.TestCase):
    def test_migration_adds_energy_wh_and_idempotent(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        app._apply_schema_migrations(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(llm_samples)").fetchall()]
        self.assertIn("energy_wh", cols)
        # Re-running must not raise (duplicate-column is swallowed) and not dup.
        app._apply_schema_migrations(conn)
        cols2 = [r[1] for r in conn.execute("PRAGMA table_info(llm_samples)").fetchall()]
        self.assertEqual(cols2.count("energy_wh"), 1)

    def test_existing_columns_unchanged(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        app._apply_schema_migrations(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(llm_samples)").fetchall()]
        for c in ("ts", "model", "tps", "ttft_ms", "prompt_tps", "eval_count"):
            self.assertIn(c, cols)


class TestEnergyPersist(unittest.TestCase):
    def setUp(self):
        self._saved = app._LLM_LAST
        self._power = app.LATEST.get("power")
        _clear_llm_samples()

    def tearDown(self):
        app._LLM_LAST = self._saved
        app.LATEST["power"] = self._power
        _clear_llm_samples()

    def _last_energy(self):
        with app.LOCK:
            return app.DB.execute(
                "SELECT energy_wh FROM llm_samples ORDER BY ts DESC LIMIT 1").fetchone()[0]

    def test_energy_persisted_when_gpu_power_present(self):
        app.LATEST["power"] = 300.0   # 300 W
        app._capture_llm_metrics(SAMPLE_GEN)  # eval_duration 2.0 s
        # 2.0 s × 300 W / 3600 = 0.16667 Wh
        e = self._last_energy()
        self.assertIsNotNone(e)
        self.assertAlmostEqual(e, 2.0 * 300.0 / 3600.0, places=3)

    def test_energy_null_when_no_gpu_power(self):
        app.LATEST["power"] = 0
        app._capture_llm_metrics(SAMPLE_GEN)
        self.assertIsNone(self._last_energy())

    def test_energy_null_when_no_timing(self):
        # _sample_energy_wh on a metrics dict lacking duration → None.
        self.assertIsNone(app._sample_energy_wh({"eval_count": 10}, gpu_w=300))
        self.assertIsNone(app._sample_energy_wh({}, gpu_w=300))

    def test_sample_energy_matches_inference_cost_formula(self):
        m = app._llm_metrics_from_response(SAMPLE_GEN, "gemma3:1b")
        saved = app.LATEST.get("power")
        try:
            app.LATEST["power"] = 250.0
            e = app._sample_energy_wh(m)
            inf = app._inference_cost(m)
        finally:
            app.LATEST["power"] = saved
        self.assertAlmostEqual(e, inf["energy_wh"], places=3)


class TestSavings(unittest.TestCase):
    def setUp(self):
        _clear_llm_samples()
        self._settings_backup = {
            k: app.get_settings().get(k)
            for k in ("cloud_cost_per_1k", "kwh_price", "tariff_mode", "kwh_price_night")
        }

    def tearDown(self):
        _clear_llm_samples()
        app.save_settings(self._settings_backup)

    def _seed(self, rows):
        # rows: list of (eval_count, energy_wh)
        now = int(time.time())
        with app.LOCK:
            for i, (ec, ewh) in enumerate(rows):
                app.DB.execute(
                    "INSERT INTO llm_samples(ts,model,tps,ttft_ms,prompt_tps,eval_count,energy_wh) "
                    "VALUES(?,?,?,?,?,?,?)", (now - i, "m", 50.0, 100.0, None, ec, ewh))
            app.DB.commit()

    def test_none_when_no_samples(self):
        app.save_settings({"cloud_cost_per_1k": "0.15"})
        self.assertIsNone(app._llm_savings())

    def test_tokens_and_cloud_cost(self):
        app.save_settings({"cloud_cost_per_1k": "0.15", "kwh_price": ""})
        self._seed([(1000, 1.0), (1000, 1.0)])  # 2000 tokens
        s = app._llm_savings()
        self.assertEqual(s["tokens"], 2000)
        # 2000/1000 × 0.15 = 0.30
        self.assertAlmostEqual(s["cloud_cost"], 0.30, places=2)
        # No tariff → local cost + saved are null (graceful).
        self.assertIsNone(s["local_cost"])
        self.assertIsNone(s["saved"])

    def test_no_cloud_rate_hides_cloud(self):
        app.save_settings({"cloud_cost_per_1k": "", "kwh_price": ""})
        self._seed([(500, 0.5)])
        s = app._llm_savings()
        self.assertEqual(s["tokens"], 500)
        self.assertIsNone(s["cloud_cost"])
        self.assertIsNone(s["saved"])

    def test_local_cost_and_saved_with_tariff(self):
        # tariff path: cost is disabled live, so prove it here.
        app.save_settings({"cloud_cost_per_1k": "0.15", "kwh_price": "0.30",
                          "tariff_mode": "single"})
        # 10000 tokens, 50 Wh total local energy = 0.05 kWh
        self._seed([(10000, 50.0)])
        s = app._llm_savings()
        self.assertEqual(s["tokens"], 10000)
        # cloud = 10 × 0.15 = 1.50
        self.assertAlmostEqual(s["cloud_cost"], 1.50, places=2)
        # local = 0.05 kWh × 0.30 = 0.015
        self.assertAlmostEqual(s["local_cost"], 0.015, places=3)
        # saved = 1.50 − 0.015 = 1.485 → rounds to 1.49 (cloud rounds to 2dp first)
        self.assertAlmostEqual(s["saved"], round(1.50 - 0.015, 2), places=2)
        self.assertEqual(s["window_days"], 30)

    def test_null_energy_rows_skip_local_cost(self):
        # Pre-migration rows have NULL energy_wh → local cost stays null even with tariff.
        app.save_settings({"cloud_cost_per_1k": "0.15", "kwh_price": "0.30"})
        self._seed([(1000, None), (1000, None)])
        s = app._llm_savings()
        self.assertEqual(s["tokens"], 2000)
        self.assertIsNone(s["local_energy_kwh"])
        self.assertIsNone(s["local_cost"])
        self.assertIsNone(s["saved"])
        self.assertAlmostEqual(s["cloud_cost"], 0.30, places=2)

    def test_savings_in_api_llm(self):
        app.save_settings({"cloud_cost_per_1k": "0.15"})
        self._seed([(1000, 1.0)])
        saved_en, saved_url = app.COPILOT_ENABLED, app.COPILOT_OLLAMA_URL
        try:
            app.COPILOT_ENABLED = True
            app.COPILOT_OLLAMA_URL = "http://127.0.0.1:1"
            j = app.app.test_client().get("/api/llm").get_json()
        finally:
            app.COPILOT_ENABLED, app.COPILOT_OLLAMA_URL = saved_en, saved_url
        self.assertIn("savings", j)
        self.assertIsNotNone(j["savings"])
        self.assertEqual(j["savings"]["tokens"], 1000)


class TestSpend(unittest.TestCase):
    """_llm_spend(now) — today/last7 partitioning across the local-midnight
    boundary, spark7 ordering, graceful nulls, NULL-energy safety, /api/llm."""

    def setUp(self):
        _clear_llm_samples()
        self._settings_backup = {
            k: app.get_settings().get(k)
            for k in ("cloud_cost_per_1k", "kwh_price", "tariff_mode", "kwh_price_night")
        }
        # A fixed reference "now" mid-afternoon so today's local window is wide.
        lt = list(time.localtime())
        lt[3], lt[4], lt[5] = 14, 0, 0  # 14:00:00 local
        self.now = int(time.mktime(time.struct_time(lt)))
        # Local midnight of today's date.
        self.today_mid = time.mktime(time.strptime(
            time.strftime("%Y-%m-%d", time.localtime(self.now)), "%Y-%m-%d"))

    def tearDown(self):
        _clear_llm_samples()
        app.save_settings(self._settings_backup)

    def _seed_at(self, ts, eval_count, energy_wh):
        with app.LOCK:
            app.DB.execute(
                "INSERT INTO llm_samples(ts,model,tps,ttft_ms,prompt_tps,eval_count,energy_wh) "
                "VALUES(?,?,?,?,?,?,?)", (int(ts), "m", 50.0, 100.0, None, eval_count, energy_wh))
            app.DB.commit()

    def test_today_vs_last7_partition_across_midnight(self):
        app.save_settings({"cloud_cost_per_1k": "", "kwh_price": ""})
        # Today: two rows comfortably after local midnight.
        self._seed_at(self.today_mid + 3600, 100, 10.0)
        self._seed_at(self.today_mid + 7200, 200, 20.0)
        # Yesterday: just before today's local midnight (NOT today).
        self._seed_at(self.today_mid - 60, 500, 50.0)
        # 3 days ago: in last7 but not today.
        self._seed_at(self.today_mid - 3 * 86400 + 3600, 400, 40.0)
        sp = app._llm_spend(self.now)
        # today = only the two post-midnight rows
        self.assertEqual(sp["today"]["calls"], 2)
        self.assertEqual(sp["today"]["tokens"], 300)
        self.assertAlmostEqual(sp["today"]["energy_wh"], 30.0, places=2)
        # last7 = today + yesterday + 3-days-ago = 4 rows
        self.assertEqual(sp["last7"]["calls"], 4)
        self.assertEqual(sp["last7"]["tokens"], 1200)
        self.assertAlmostEqual(sp["last7"]["energy_wh"], 120.0, places=2)

    def test_spark7_seven_ordered_buckets(self):
        app.save_settings({"cloud_cost_per_1k": "", "kwh_price": ""})
        self._seed_at(self.today_mid + 3600, 100, 10.0)
        self._seed_at(self.today_mid - 2 * 86400 + 3600, 50, 5.0)
        sp = app._llm_spend(self.now)
        self.assertEqual(len(sp["spark7"]), 7)
        days = [d["d"] for d in sp["spark7"]]
        self.assertEqual(days, sorted(days))  # oldest → newest
        self.assertEqual(days[-1], time.strftime("%Y-%m-%d", time.localtime(self.now)))
        # No tariff → spark is energy_wh
        self.assertEqual(sp["spark_metric"], "energy_wh")
        self.assertAlmostEqual(sp["spark7"][-1]["v"], 10.0, places=2)  # today
        self.assertAlmostEqual(sp["spark7"][4]["v"], 5.0, places=2)    # 2 days ago

    def test_no_tariff_local_null_cloud_present(self):
        app.save_settings({"cloud_cost_per_1k": "0.15", "kwh_price": ""})
        self._seed_at(self.today_mid + 3600, 2000, 30.0)
        sp = app._llm_spend(self.now)
        self.assertIsNone(sp["today"]["local_cost"])
        self.assertAlmostEqual(sp["today"]["cloud_cost"], 0.30, places=2)  # 2000/1000×0.15
        self.assertEqual(sp["spark_metric"], "energy_wh")

    def test_tariff_sets_local_cost_and_cost_spark(self):
        app.save_settings({"cloud_cost_per_1k": "0.15", "kwh_price": "0.30",
                          "tariff_mode": "single"})
        self._seed_at(self.today_mid + 3600, 10000, 50.0)  # 0.05 kWh
        sp = app._llm_spend(self.now)
        self.assertAlmostEqual(sp["today"]["local_cost"], 0.015, places=3)
        self.assertAlmostEqual(sp["today"]["cloud_cost"], 1.50, places=2)
        self.assertEqual(sp["spark_metric"], "cost")
        self.assertAlmostEqual(sp["spark7"][-1]["v"], 0.015, places=3)

    def test_no_cloud_rate_hides_cloud(self):
        app.save_settings({"cloud_cost_per_1k": "", "kwh_price": ""})
        self._seed_at(self.today_mid + 3600, 500, 5.0)
        sp = app._llm_spend(self.now)
        self.assertIsNone(sp["today"]["cloud_cost"])

    def test_null_energy_rows_no_nan(self):
        app.save_settings({"cloud_cost_per_1k": "0.15", "kwh_price": "0.30"})
        self._seed_at(self.today_mid + 3600, 1000, None)
        self._seed_at(self.today_mid + 7200, 1000, None)
        sp = app._llm_spend(self.now)
        self.assertEqual(sp["today"]["tokens"], 2000)
        self.assertEqual(sp["today"]["energy_wh"], 0.0)  # NULL rows skipped, no NaN
        self.assertIsNone(sp["today"]["local_cost"])     # no energy → no local cost
        self.assertAlmostEqual(sp["today"]["cloud_cost"], 0.30, places=2)

    def test_empty_today_zeros_not_crash(self):
        app.save_settings({"cloud_cost_per_1k": "0.15", "kwh_price": ""})
        sp = app._llm_spend(self.now)
        self.assertEqual(sp["today"]["calls"], 0)
        self.assertEqual(sp["today"]["tokens"], 0)
        self.assertEqual(sp["today"]["energy_wh"], 0.0)
        self.assertEqual(len(sp["spark7"]), 7)

    def test_spend_in_api_llm_and_savings_unchanged(self):
        app.save_settings({"cloud_cost_per_1k": "0.15"})
        self._seed_at(self.today_mid + 3600, 1000, 1.0)
        saved_en, saved_url = app.COPILOT_ENABLED, app.COPILOT_OLLAMA_URL
        try:
            app.COPILOT_ENABLED = True
            app.COPILOT_OLLAMA_URL = "http://127.0.0.1:1"
            j = app.app.test_client().get("/api/llm").get_json()
        finally:
            app.COPILOT_ENABLED, app.COPILOT_OLLAMA_URL = saved_en, saved_url
        self.assertIn("spend", j)
        self.assertIn("today", j["spend"])
        self.assertIn("last7", j["spend"])
        self.assertEqual(len(j["spend"]["spark7"]), 7)
        # existing savings block still present + correct
        self.assertIn("savings", j)
        self.assertEqual(j["savings"]["tokens"], 1000)


class TestCloudSetting(unittest.TestCase):
    def test_default_and_round_trip(self):
        self.assertIn("cloud_cost_per_1k", app.SETTING_DEFAULTS)
        self.assertEqual(app.SETTING_DEFAULTS["cloud_cost_per_1k"], "0.15")
        backup = app.get_settings().get("cloud_cost_per_1k")
        try:
            app.save_settings({"cloud_cost_per_1k": "0.42"})
            self.assertEqual(app.get_settings()["cloud_cost_per_1k"], "0.42")
        finally:
            app.save_settings({"cloud_cost_per_1k": backup})


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
