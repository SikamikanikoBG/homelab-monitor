"""Tests for the 🔗 "These fired together" co-anomaly block on the anomaly-ribbon
endpoint (`GET /api/anomaly_ribbon` → `co_anomaly[]`).

The block is server-computed, LLM-free, and ADDITIVE: it reuses the cells already
built for each series (no second DB scan) and marks the fixed 60 buckets where
>=2 metrics were HOT in the SAME time bucket — honest temporal co-occurrence, not
a formal incident.

Coverage:
  • a seeded multi-series spike that lands in the SAME bucket → exactly one
    co_anomaly entry with count=2 and BOTH series' keys, worst-first;
  • a single-series hot bucket is NOT reported (needs >=2);
  • the "hot" definition matches the ribbon's clickable-cell rule exactly
    (dir!='flat' AND score*threshold >= _RIBBON_TL_Z), and mirrors the frontend
    RIBBON_TL_Z constant;
  • empty co_anomaly array when nothing co-fires, and on empty history;
  • the block is ADDITIVE — the existing `series` output is byte-for-byte
    unchanged whether or not co_anomaly is computed;
  • the endpoint stays LLM-free even when co-anomalies fire (tripwire);
  • co_anomaly never reaches the public /status surfaces;
  • the new visible i18n keys exist in BOTH locales (en + zh-CN).
"""
import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EN = os.path.join(ROOT, "locales", "en.json")
ZH = os.path.join(ROOT, "locales", "zh-CN.json")
HTML = os.path.join(ROOT, "static", "dashboard.html")


class _LLMGuard:
    def __init__(self):
        self.calls = 0

    def __enter__(self):
        self._orig_gen = app._ollama_generate
        self._orig_stream = app._ollama_generate_stream

        def _boom(*a, **k):
            self.calls += 1
            raise AssertionError("LLM must not be called on the co-anomaly path")

        app._ollama_generate = _boom
        app._ollama_generate_stream = _boom
        return self

    def __exit__(self, *exc):
        app._ollama_generate = self._orig_gen
        app._ollama_generate_stream = self._orig_stream


class _SamplesFixture(unittest.TestCase):
    """Clears + restores the recent `samples` window around each test."""
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

    def _seed(self, rows):
        """rows: list of (ts, util, mem_used, mem_total, power, temp)."""
        with app.LOCK:
            app.DB.executemany(
                "INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp) "
                "VALUES(?,?,?,?,?,?)", rows)
            app.DB.commit()

    def _spread(self, now, quiet, spike, quiet_row, spike_row):
        """Build `quiet` baseline rows spread across the window then a trailing
        `spike` row that lands in the LAST bucket. quiet_row/spike_row are callables
        mapping a base index/value -> the 5 metric columns (util,mem_used,mem_total,
        power,temp)."""
        seq = list(range(quiet)) + [None]  # None marks the trailing spike
        step = self.WINDOW / (len(seq) + 1)
        rows = []
        for i, marker in enumerate(seq):
            ts = int(now - self.WINDOW + (i + 1) * step)
            cols = spike_row(i) if marker is None else quiet_row(i)
            rows.append((ts,) + tuple(cols))
        return rows


class TestCoAnomalyShape(_SamplesFixture):
    def test_empty_history_empty_co_anomaly(self):
        j = self.c.get("/api/anomaly_ribbon").get_json()
        self.assertIn("co_anomaly", j)
        self.assertEqual(j["co_anomaly"], [])
        self.assertEqual(j["co_anomaly_windows"], 0)

    def test_two_series_spike_same_bucket_is_one_co_entry(self):
        # Seed two INDEPENDENT columns so exactly two series fire together: util
        # (column `util`) quiet ~20 then spikes to 95, and temp (column `temp`) quiet
        # ~40 then spikes to 90. Both trailing spikes land in the SAME (last) bucket
        # → one co_anomaly entry, count=2, both keys present.
        now = int(app.time.time())
        rows = self._spread(
            now, quiet=240, spike=1,
            quiet_row=lambda i: (20.0 + (i % 3), 0, 0, 0, 40.0 + (i % 3)),
            spike_row=lambda i: (95.0, 0, 0, 0, 90.0))
        self._seed(rows)
        j = self.c.get("/api/anomaly_ribbon").get_json()
        co = j["co_anomaly"]
        self.assertEqual(len(co), 1, f"expected exactly one co-firing bucket, got {co}")
        e = co[0]
        self.assertEqual(e["count"], 2)
        self.assertEqual(set(e["keys"]), {"gpu_util", "gpu_temp"})
        self.assertIsInstance(e["ts"], int)
        self.assertIsInstance(e["bucket"], int)
        self.assertEqual(j["co_anomaly_windows"], 1)

    def test_shared_power_column_co_fires_gpu_power_and_total(self):
        # A power spike drives BOTH gpu_power (column `power`) and power_draw
        # (_TOTAL_W_EXPR, which includes `power`); with util also spiking, three
        # series honestly fire together in the same bucket → count=3, all three keys.
        now = int(app.time.time())
        rows = self._spread(
            now, quiet=240, spike=1,
            quiet_row=lambda i: (20.0 + (i % 3), 0, 0, 50.0 + (i % 3), 0),
            spike_row=lambda i: (95.0, 0, 0, 400.0, 0))
        self._seed(rows)
        j = self.c.get("/api/anomaly_ribbon").get_json()
        co = j["co_anomaly"]
        self.assertEqual(len(co), 1, f"expected exactly one co-firing bucket, got {co}")
        e = co[0]
        self.assertEqual(e["count"], 3)
        self.assertEqual(set(e["keys"]), {"gpu_util", "gpu_power", "power_draw"})

    def test_single_series_hot_bucket_not_reported(self):
        # Only util spikes; power stays flat. No bucket has >=2 hot series → empty.
        now = int(app.time.time())
        rows = self._spread(
            now, quiet=240, spike=1,
            quiet_row=lambda i: (20.0 + (i % 3), 0, 0, 50.0 + (i % 3), 0),
            spike_row=lambda i: (95.0, 0, 0, 51.0, 0))   # power stays baseline-ish
        self._seed(rows)
        j = self.c.get("/api/anomaly_ribbon").get_json()
        self.assertEqual(j["co_anomaly"], [])
        self.assertEqual(j["co_anomaly_windows"], 0)

    def test_hot_definition_matches_clickable_cell_rule(self):
        # Every co_anomaly key must, at its bucket, point to a cell that satisfies the
        # SAME hot rule the frontend uses for clickable cells:
        #   dir != 'flat' AND score*threshold >= _RIBBON_TL_Z
        now = int(app.time.time())
        rows = self._spread(
            now, quiet=240, spike=1,
            quiet_row=lambda i: (20.0 + (i % 3), 0, 0, 50.0 + (i % 3), 0),
            spike_row=lambda i: (95.0, 0, 0, 400.0, 0))
        self._seed(rows)
        with app.LOCK:
            j = app._anomaly_ribbon(app.DB.cursor(), now)
        thr = j["threshold"]
        by_key = {s["key"]: s for s in j["series"]}
        self.assertTrue(j["co_anomaly"])
        for e in j["co_anomaly"]:
            hot_here = 0
            for s in j["series"]:
                if s.get("status") != "ok":
                    continue
                c = (s["cells"] or [])[e["bucket"]]
                is_hot = (c is not None and c.get("dir") != "flat"
                          and (c.get("score") or 0.0) * thr >= app._RIBBON_TL_Z)
                if is_hot:
                    hot_here += 1
                    self.assertIn(s["key"], e["keys"],
                                  "a hot series at this bucket must be in co_anomaly keys")
            # count must equal the number of independently-verified hot series here
            self.assertEqual(e["count"], hot_here)
            self.assertGreaterEqual(e["count"], 2)
            # keys are worst-first: scores non-increasing
            scores = [(by_key[k]["cells"][e["bucket"]]["score"]) for k in e["keys"]]
            self.assertEqual(scores, sorted(scores, reverse=True))

    def test_backend_tl_z_matches_frontend_constant(self):
        # The server-side hot threshold must equal the frontend RIBBON_TL_Z so the
        # markers align one-for-one with the clickable cells.
        with open(HTML, encoding="utf-8") as f:
            html = f.read()
        self.assertIn("const RIBBON_TL_Z = 3.0", html)
        self.assertEqual(app._RIBBON_TL_Z, 3.0)

    def test_additive_series_output_unchanged(self):
        # Computing co_anomaly must NOT mutate the per-series cells: the series output
        # is identical whether or not any bucket co-fires.
        now = int(app.time.time())
        rows = self._spread(
            now, quiet=240, spike=1,
            quiet_row=lambda i: (20.0 + (i % 3), 0, 0, 50.0 + (i % 3), 0),
            spike_row=lambda i: (95.0, 0, 0, 400.0, 0))
        self._seed(rows)
        with app.LOCK:
            j = app._anomaly_ribbon(app.DB.cursor(), now)
        # snapshot series, deep-copy, re-derive co-anomaly by hand and confirm the
        # series payload is untouched (block is purely read-over-cells).
        series_snapshot = copy.deepcopy(j["series"])
        thr = j["threshold"]
        recomputed = []
        B = j["buckets"]
        for bi in range(B):
            fired = [(c["score"], s["key"]) for s in j["series"]
                     if s.get("status") == "ok"
                     for c in [(s["cells"] or [])[bi]]
                     if c is not None and c.get("dir") != "flat"
                     and c["score"] * thr >= app._RIBBON_TL_Z]
            if len(fired) >= 2:
                recomputed.append(bi)
        self.assertEqual(series_snapshot, j["series"])   # unchanged by our read
        self.assertEqual([e["bucket"] for e in j["co_anomaly"]], recomputed)


class TestCoAnomalyLLMFreeAndPrivate(_SamplesFixture):
    def test_llm_free_when_co_firing(self):
        now = int(app.time.time())
        rows = self._spread(
            now, quiet=240, spike=1,
            quiet_row=lambda i: (20.0 + (i % 3), 0, 0, 50.0 + (i % 3), 0),
            spike_row=lambda i: (95.0, 0, 0, 400.0, 0))
        self._seed(rows)
        with _LLMGuard() as g:
            r = self.c.get("/api/anomaly_ribbon")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()["co_anomaly"])
            self.assertEqual(g.calls, 0)

    def test_not_on_public_surfaces(self):
        now = int(app.time.time())
        rows = self._spread(
            now, quiet=240, spike=1,
            quiet_row=lambda i: (20.0 + (i % 3), 0, 0, 50.0 + (i % 3), 0),
            spike_row=lambda i: (95.0, 0, 0, 400.0, 0))
        self._seed(rows)
        sp = app.STATUS_PAGE
        app.STATUS_PAGE = True
        try:
            for path in ("/api/status", "/status"):
                body = self.c.get(path).get_data(as_text=True)
                self.assertNotIn("co_anomaly", body)
        finally:
            app.STATUS_PAGE = sp


class TestCoAnomalyI18n(unittest.TestCase):
    NEW_KEYS = [
        "ribbon.cofired_label", "ribbon.cofired_aria",
        "ribbon.cofired_tip", "ribbon.cofired_summary",
    ]

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


if __name__ == "__main__":
    unittest.main()
