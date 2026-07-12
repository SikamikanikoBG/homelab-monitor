"""Tests for the 🌈 anomaly-ribbon timeline endpoint (`/api/anomaly_ribbon`) — the
time-bucketed VISUAL of the same z-score maths the Anomalies card uses.

Coverage:
  • endpoint shape: {ok, now, window_h, buckets, threshold, status, series[...]}
    with a FIXED bucket count and every series carrying exactly that many cells;
  • every scored cell's `score` is in [0, 1] and `dir` ∈ {spike, dip, flat};
  • empty buckets are null (a gap), not a fabricated zero;
  • the ribbon's baseline mirrors `_zscore_anomalies` exactly — on a seeded spike,
    the flagged series' peak bucket agrees with the card (same direction, |z|≥thresh);
  • short / no history degrades to 'collecting' with all-null cells, never 500;
  • the endpoint makes ZERO LLM calls (tripwire monkeypatch);
  • the ribbon is NOT exposed on the public /status surfaces (authed only);
  • the new visible i18n keys exist in BOTH locales (en + zh-CN parity).
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EN = os.path.join(ROOT, "locales", "en.json")
ZH = os.path.join(ROOT, "locales", "zh-CN.json")


class _LLMGuard:
    """Monkeypatch every LLM entry point to raise, proving zero calls on the path."""
    def __init__(self):
        self.calls = 0

    def __enter__(self):
        self._orig_gen = app._ollama_generate
        self._orig_stream = app._ollama_generate_stream

        def _boom(*a, **k):
            self.calls += 1
            raise AssertionError("LLM must not be called on the anomaly-ribbon path")

        app._ollama_generate = _boom
        app._ollama_generate_stream = _boom
        return self

    def __exit__(self, *exc):
        app._ollama_generate = self._orig_gen
        app._ollama_generate_stream = self._orig_stream


class _SamplesFixture(unittest.TestCase):
    """Clears and restores the recent `samples` window around each test."""
    def setUp(self):
        self.c = app.app.test_client()
        self.WINDOW = 6 * 3600
        with app.LOCK:
            self._saved = app.DB.execute(
                "SELECT ts,util,mem_used,mem_total,power,temp FROM samples").fetchall()
            app.DB.execute("DELETE FROM samples")
            app.DB.commit()

    def tearDown(self):
        with app.LOCK:
            app.DB.execute("DELETE FROM samples")
            app.DB.executemany(
                "INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp) "
                "VALUES(?,?,?,?,?,?)", self._saved)
            app.DB.commit()

    def _seed_util(self, baseline_vals, latest, spread_window=True):
        """Seed a util series then a final spike/dip. When `spread_window` (default),
        samples are spread evenly across the full 6h window so distinct buckets are
        populated and the trailing spike lands in the LAST bucket (mirroring a live
        6h history), rather than all crammed into bucket 0."""
        now = int(app.time.time())
        seq = list(baseline_vals) + [latest]
        with app.LOCK:
            if spread_window:
                step = self.WINDOW / (len(seq) + 1)      # spread across the whole 6h
                for i, v in enumerate(seq):
                    ts = int(now - self.WINDOW + (i + 1) * step)
                    app.DB.execute(
                        "INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp) "
                        "VALUES(?,?,?,?,?,?)", (ts, v, 0, 0, 0, 0))
            else:
                for i, v in enumerate(seq):
                    ts = now - (len(seq) - i) * app.INTERVAL
                    app.DB.execute(
                        "INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp) "
                        "VALUES(?,?,?,?,?,?)", (ts, v, 0, 0, 0, 0))
            app.DB.commit()
        return now


class TestShapeAndBounds(_SamplesFixture):
    def test_empty_history_collecting_shape(self):
        with _LLMGuard() as g:
            r = self.c.get("/api/anomaly_ribbon")
            self.assertEqual(r.status_code, 200)
            j = r.get_json()
            self.assertEqual(g.calls, 0)
        self.assertTrue(j["ok"])
        self.assertEqual(j["buckets"], app._RIBBON_BUCKETS)
        self.assertEqual(j["window_h"], 6)
        self.assertEqual(j["threshold"], 3.0)
        self.assertEqual(j["status"], "collecting")
        for s in j["series"]:
            self.assertEqual(s["status"], "collecting")
            self.assertEqual(len(s["cells"]), app._RIBBON_BUCKETS)
            self.assertTrue(all(c is None for c in s["cells"]))

    def test_fixed_bucket_count_and_score_bounds(self):
        base = [20.0 + (i % 3) for i in range(240)]
        self._seed_util(base, 95.0)
        j = self.c.get("/api/anomaly_ribbon").get_json()
        for s in j["series"]:
            self.assertEqual(len(s["cells"]), app._RIBBON_BUCKETS)
            for c in s["cells"]:
                if c is None:
                    continue
                self.assertGreaterEqual(c["score"], 0.0)
                self.assertLessEqual(c["score"], 1.0)
                self.assertIn(c["dir"], ("spike", "dip", "flat"))
                self.assertIsInstance(c["ts"], int)

    def test_empty_buckets_are_null_gaps(self):
        # A gappy series (large stride) must leave many buckets null, not zero-filled.
        now = int(app.time.time())
        with app.LOCK:
            for i in range(60):
                ts = now - self.WINDOW + i * 60      # first 1h only → later buckets empty
                app.DB.execute(
                    "INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp) "
                    "VALUES(?,?,?,?,?,?)", (ts, 20.0 + (i % 4), 0, 0, 0, 0))
            app.DB.commit()
        j = self.c.get("/api/anomaly_ribbon").get_json()
        util = next(s for s in j["series"] if s["key"] == "gpu_util")
        self.assertEqual(util["status"], "ok")
        self.assertTrue(any(c is None for c in util["cells"]),
                        "later empty buckets must be null gaps")


class TestAgreesWithDetector(_SamplesFixture):
    def test_spike_peak_agrees_with_zscore_card(self):
        # 240 quiet readings ~20% then a 95% spike → the detector flags gpu_util spike;
        # the ribbon's peak bucket must be the same series, same direction, score==1.0
        # (|z| ≥ threshold ⇒ min(1,|z|/Z)==1), and share the exact baseline maths.
        base = [20.0 + (i % 3) for i in range(240)]
        now = self._seed_util(base, 95.0)
        with app.LOCK:
            card = app._zscore_anomalies(app.DB.cursor(), now)
            ribbon = app._anomaly_ribbon(app.DB.cursor(), now)
        util_card = [a for a in card["items"] if a["key"] == "gpu_util"]
        self.assertTrue(util_card, "detector should flag gpu_util")
        self.assertEqual(util_card[0]["direction"], "spike")

        util_rib = next(s for s in ribbon["series"] if s["key"] == "gpu_util")
        self.assertEqual(util_rib["status"], "ok")
        scored = [c for c in util_rib["cells"] if c is not None]
        peak = max(scored, key=lambda c: c["score"])
        self.assertEqual(peak["score"], 1.0)          # |z| ≥ Z_THRESH ⇒ clamped to 1
        self.assertEqual(peak["dir"], "spike")

        # Same baseline maths: recompute mean/pop-stddev over the window EXCLUDING the
        # latest point (as both the card and ribbon do) and confirm the spike bucket's
        # z clears the threshold using that baseline.
        vals = base + [95.0]
        b = vals[:-1]
        n = len(b)
        mean = sum(b) / n
        sd = (sum((v - mean) ** 2 for v in b) / n) ** 0.5
        self.assertGreaterEqual(abs((95.0 - mean) / sd), ribbon["threshold"])

    def test_dip_direction_in_ribbon(self):
        base = [90.0 + (i % 3) for i in range(240)]
        self._seed_util(base, 5.0)
        j = self.c.get("/api/anomaly_ribbon").get_json()
        util = next(s for s in j["series"] if s["key"] == "gpu_util")
        peak = max((c for c in util["cells"] if c), key=lambda c: c["score"])
        self.assertEqual(peak["dir"], "dip")

    def test_short_history_series_is_collecting(self):
        # Fewer than MIN_PTS points → that series omits scoring (collecting), no crash.
        self._seed_util([20.0] * 5, 21.0)
        j = self.c.get("/api/anomaly_ribbon").get_json()
        util = next(s for s in j["series"] if s["key"] == "gpu_util")
        self.assertEqual(util["status"], "collecting")
        self.assertTrue(all(c is None for c in util["cells"]))


class TestNoLeakAndLLMFree(_SamplesFixture):
    def test_llm_free_even_when_firing(self):
        base = [20.0 + (i % 3) for i in range(240)]
        self._seed_util(base, 95.0)
        with _LLMGuard() as g:
            r = self.c.get("/api/anomaly_ribbon")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(g.calls, 0)

    def test_not_on_public_surfaces(self):
        base = [20.0 + (i % 3) for i in range(240)]
        self._seed_util(base, 95.0)
        sp = app.STATUS_PAGE
        app.STATUS_PAGE = True
        try:
            for path in ("/api/status", "/status"):
                body = self.c.get(path).get_data(as_text=True)
                self.assertNotIn("anomaly_ribbon", body)
                # the ribbon's private per-series scores must never leak here
                self.assertNotIn("\"buckets\"", body)
        finally:
            app.STATUS_PAGE = sp


class _DiskIoFixture(unittest.TestCase):
    """Clears and restores the recent `disk_io_samples` window around each test."""
    def setUp(self):
        self.c = app.app.test_client()
        self.WINDOW = 6 * 3600
        with app.LOCK:
            self._saved = app.DB.execute(
                "SELECT ts,device,read_mb_s,write_mb_s,util_pct FROM disk_io_samples"
            ).fetchall()
            app.DB.execute("DELETE FROM disk_io_samples")
            app.DB.commit()

    def tearDown(self):
        with app.LOCK:
            app.DB.execute("DELETE FROM disk_io_samples")
            app.DB.executemany(
                "INSERT INTO disk_io_samples(ts,device,read_mb_s,write_mb_s,util_pct) "
                "VALUES(?,?,?,?,?)", self._saved)
            app.DB.commit()

    def _seed_dev(self, device, baseline_total, latest_total, n=240):
        """Seed a device's read+write throughput across the full 6h window: `n`
        baseline points (split read/write) then a final spike/dip. Returns `now`."""
        now = int(app.time.time())
        seq = [baseline_total + (i % 3) for i in range(n)] + [latest_total]
        step = self.WINDOW / (len(seq) + 1)
        with app.LOCK:
            for i, tot in enumerate(seq):
                ts = int(now - self.WINDOW + (i + 1) * step)
                r = tot * 0.6
                w = tot - r
                app.DB.execute(
                    "INSERT INTO disk_io_samples(ts,device,read_mb_s,write_mb_s,util_pct) "
                    "VALUES(?,?,?,?,?)", (ts, device, r, w, 0.0))
            app.DB.commit()
        return now


class TestDiskIoRibbonRows(_DiskIoFixture):
    def test_disk_io_row_appears_after_gpu_power_series(self):
        # Seed one device with enough history → a disk_io:<dev> row must appear,
        # AFTER the fixed GPU/power series (stable order).
        self._seed_dev("sda", 20.0, 300.0)
        j = self.c.get("/api/anomaly_ribbon").get_json()
        keys = [s["key"] for s in j["series"]]
        self.assertIn("disk_io:sda", keys)
        # GPU/power series come first, disk_io rows after.
        gpu_idx = keys.index("gpu_util")
        dio_idx = keys.index("disk_io:sda")
        self.assertGreater(dio_idx, gpu_idx)
        row = next(s for s in j["series"] if s["key"] == "disk_io:sda")
        self.assertEqual(row["unit"], "MB/s")
        self.assertIn("sda", row["label"])
        self.assertEqual(len(row["cells"]), app._RIBBON_BUCKETS)

    def test_disk_io_score_matches_detector_baseline_on_spike(self):
        # The ribbon's disk_io peak bucket must agree with _disk_io_anomaly_items:
        # same baseline maths (mean + pop-stddev over the window EXCLUDING latest),
        # same MIN_DEV gate, spike direction, |z|≥threshold ⇒ clamped score 1.0.
        base_tot, spike_tot, n = 20.0, 400.0, 240
        now = self._seed_dev("nvme0n1", base_tot, spike_tot, n=n)
        with app.LOCK:
            items, checked, enough = app._disk_io_anomaly_items(
                app.DB.cursor(), now, self.WINDOW, 30, 3.0)
            ribbon = app._anomaly_ribbon(app.DB.cursor(), now)
        card = [a for a in items if a["key"] == "disk_io:nvme0n1"]
        self.assertTrue(card, "detector should flag disk_io:nvme0n1")
        self.assertEqual(card[0]["direction"], "spike")

        row = next(s for s in ribbon["series"] if s["key"] == "disk_io:nvme0n1")
        self.assertEqual(row["status"], "ok")
        scored = [c for c in row["cells"] if c is not None]
        peak = max(scored, key=lambda c: c["score"])
        self.assertEqual(peak["dir"], "spike")
        self.assertEqual(peak["score"], 1.0)          # |z| ≥ Z_THRESH ⇒ clamped to 1

        # Recompute the baseline exactly and confirm the spike clears threshold with
        # the min-dev gate the detector uses.
        vals = [base_tot + (i % 3) for i in range(n)] + [spike_tot]
        b = vals[:-1]
        m = sum(b) / len(b)
        sd = (sum((v - m) ** 2 for v in b) / len(b)) ** 0.5
        self.assertGreaterEqual(abs(spike_tot - m), app._DISK_IO_MIN_DEV)
        self.assertGreaterEqual(abs((spike_tot - m) / sd), ribbon["threshold"])

    def test_dip_direction_for_disk_io(self):
        self._seed_dev("sdb", 300.0, 20.0)
        j = self.c.get("/api/anomaly_ribbon").get_json()
        row = next(s for s in j["series"] if s["key"] == "disk_io:sdb")
        peak = max((c for c in row["cells"] if c), key=lambda c: c["score"])
        self.assertEqual(peak["dir"], "dip")

    def test_no_disk_io_history_no_row_no_500(self):
        # With disk_io_samples empty, the endpoint must still 200 and simply carry
        # no disk_io:* rows (the GPU/power rows are unaffected).
        r = self.c.get("/api/anomaly_ribbon")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertFalse(any(s["key"].startswith("disk_io:") for s in j["series"]))

    def test_short_disk_io_history_is_collecting(self):
        # Fewer than MIN_PTS points for a device → a collecting row (all-null cells),
        # never a crash — mirroring the GPU/power collecting semantics.
        self._seed_dev("sdc", 20.0, 21.0, n=5)
        j = self.c.get("/api/anomaly_ribbon").get_json()
        row = next(s for s in j["series"] if s["key"] == "disk_io:sdc")
        self.assertEqual(row["status"], "collecting")
        self.assertTrue(all(c is None for c in row["cells"]))

    def test_disk_io_row_llm_free_and_not_public(self):
        self._seed_dev("sda", 20.0, 400.0)
        with _LLMGuard() as g:
            r = self.c.get("/api/anomaly_ribbon")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(g.calls, 0)
        sp = app.STATUS_PAGE
        app.STATUS_PAGE = True
        try:
            for path in ("/api/status", "/status"):
                body = self.c.get(path).get_data(as_text=True)
                self.assertNotIn("disk_io:", body)
        finally:
            app.STATUS_PAGE = sp


class TestI18nParity(unittest.TestCase):
    NEW_KEYS = [
        "ribbon.timeline_title", "ribbon.timeline_cap",
        "ribbon.no_anomalies", "ribbon.building", "ribbon.peak",
    ]
    DEAD_KEYS = ["anom.quiet", "ribbon.timeline_explain_hint"]

    def setUp(self):
        with open(EN, encoding="utf-8") as f:
            self.en = json.load(f)
        with open(ZH, encoding="utf-8") as f:
            self.zh = json.load(f)

    def test_new_keys_in_both_locales(self):
        for k in self.NEW_KEYS:
            self.assertIn(k, self.en, f"en.json missing {k}")
            self.assertIn(k, self.zh, f"zh-CN.json missing {k}")

    def test_full_key_parity(self):
        en_keys = {k for k in self.en if not k.startswith("_")}
        zh_keys = {k for k in self.zh if not k.startswith("_")}
        self.assertEqual(en_keys - zh_keys, set())
        self.assertEqual(zh_keys - en_keys, set())

    def test_dead_keys_removed_from_both_locales(self):
        # The two reviewer-flagged dead keys must be gone from BOTH locales so parity
        # holds and nothing references a missing/dangling string.
        for k in self.DEAD_KEYS:
            self.assertNotIn(k, self.en, f"en.json still carries dead key {k}")
            self.assertNotIn(k, self.zh, f"zh-CN.json still carries dead key {k}")


class TestNoDeadKeyReferences(unittest.TestCase):
    """The renderer must not look up the removed dead i18n keys (the guarded-
    unreachable 'quiet' branch and the unused explain-hint caption key)."""

    def test_renderer_drops_dead_quiet_and_hint_lookups(self):
        with open(os.path.join(ROOT, "static", "dashboard.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertNotIn("ribbon.timeline_explain_hint", html)
        self.assertNotIn("'flat'?'quiet'", html)
        self.assertNotIn("anom.quiet", html)


if __name__ == "__main__":
    unittest.main()
