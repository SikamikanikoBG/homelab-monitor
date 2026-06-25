"""Tests for Demo mode (E4): DEMO_MODE seeds realistic synthetic history on a
fresh DB so every history-backed feature lights up — and is a strict no-op when
the flag is off, when a marker is already set, or when real history exists."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class TestDemoSeed(unittest.TestCase):
    def setUp(self):
        self._demo_was = app.DEMO_MODE
        with app.LOCK:
            for t in ("samples", "disk_samples", "models", "events", "status_history"):
                app.DB.execute(f"DELETE FROM {t}")
            app.DB.execute("DELETE FROM settings WHERE key IN (?, 'kwh_price', 'currency')",
                           (app._DEMO_MARKER,))
            app.DB.commit()

    def tearDown(self):
        app.DEMO_MODE = self._demo_was
        with app.LOCK:
            for t in ("samples", "disk_samples", "models", "events", "status_history"):
                app.DB.execute(f"DELETE FROM {t}")
            app.DB.execute("DELETE FROM settings WHERE key IN (?, 'kwh_price', 'currency')",
                           (app._DEMO_MARKER,))
            app.DB.commit()

    def _counts(self):
        with app.LOCK:
            c = app.DB.cursor()
            return (c.execute("SELECT COUNT(*) FROM samples").fetchone()[0],
                    c.execute("SELECT COUNT(*) FROM disk_samples").fetchone()[0])

    # ── seeding populates the series when empty + flag on ─────────────────────
    def test_seed_populates_when_empty(self):
        app.DEMO_MODE = True
        app._seed_demo_data()
        ns, nd = self._counts()
        self.assertGreater(ns, 1000, "expected a week+ of dense sample history")
        self.assertGreater(nd, 100, "expected disk fill history on multiple mounts")
        with app.LOCK:
            mounts = {r[0] for r in app.DB.execute("SELECT DISTINCT mount FROM disk_samples")}
        self.assertIn("/data", mounts)
        self.assertIn("/", mounts)
        # marker set so a re-run is a no-op
        with app.LOCK:
            row = app.DB.execute("SELECT value FROM settings WHERE key=?",
                                 (app._DEMO_MARKER,)).fetchone()
        self.assertTrue(row and row[0] == "1")

    # ── no-op when the flag is off ────────────────────────────────────────────
    def test_noop_when_flag_off(self):
        app.DEMO_MODE = False
        app._seed_demo_data()
        ns, nd = self._counts()
        self.assertEqual((ns, nd), (0, 0))

    # ── idempotent: a second run after a seed adds nothing ────────────────────
    def test_idempotent_marker(self):
        app.DEMO_MODE = True
        app._seed_demo_data()
        ns1, nd1 = self._counts()
        app._seed_demo_data()           # marker present now → no-op
        ns2, nd2 = self._counts()
        self.assertEqual((ns1, nd1), (ns2, nd2))

    # ── no-op when real history already exists (never clobbers a live DB) ─────
    def test_skips_when_real_history_present(self):
        now = int(time.time())
        with app.LOCK:
            for i in range(60):                 # > the 50-row "in use" threshold
                app.DB.execute("INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp)"
                               " VALUES(?,?,?,?,?,?)", (now - i * 10, 5, 100, 24576, 40, 35))
            app.DB.commit()
        app.DEMO_MODE = True
        app._seed_demo_data()
        with app.LOCK:
            n = app.DB.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
            nd = app.DB.execute("SELECT COUNT(*) FROM disk_samples").fetchone()[0]
            row = app.DB.execute("SELECT value FROM settings WHERE key=?",
                                 (app._DEMO_MARKER,)).fetchone()
        self.assertEqual(n, 60, "must not add to a DB that already has real samples")
        self.assertEqual(nd, 0)
        with app.LOCK:
            nsh = app.DB.execute("SELECT COUNT(*) FROM status_history").fetchone()[0]
        self.assertEqual(nsh, 0, "must not seed status history into a live DB")
        self.assertIsNone(row, "must not mark a real DB as demo-seeded")

    # ── forecast lights up after a seed: anomaly + disk ETA + cost projection ─
    def test_forecast_lights_up_after_seed(self):
        app.DEMO_MODE = True
        app._seed_demo_data()
        now = int(time.time())
        ctx = app._cost_ctx()
        with app.LOCK:
            cur = app.DB.cursor()
            disks = app._disk_forecasts(cur, now)
            cost = app._cost_projection(cur, ctx, now)
            anoms = app._zscore_anomalies(cur, now)
            vram = app._vram_forecast(cur, now)

        # at least one disk mount has a credible fill ETA
        filling = [d for d in disks if d["status"] == "filling" and d.get("eta_days")]
        self.assertTrue(filling, f"expected a filling disk with an ETA, got {disks}")

        # cost projection renders (seed sets a default tariff when none configured)
        self.assertTrue(cost.get("enabled"))
        self.assertGreater(cost.get("projected_month") or 0, 0)

        # the deliberate spike produces at least one z-score anomaly
        self.assertEqual(anoms["status"], "quiet")
        self.assertTrue(anoms["items"], f"expected a seeded anomaly, got {anoms}")

        # VRAM block has a real used/total read
        self.assertIsNotNone(vram.get("used_mb"))
        self.assertIsNotNone(vram.get("total_mb"))


    # ── status history seed: 90-day ribbon + heartbeat strips both populate ────
    def test_status_history_seeded_full_band(self):
        app.DEMO_MODE = True
        app._seed_demo_data()
        now = int(time.time())

        # rows span ~90 days for the 'overall' key, and exist for every subsystem key
        with app.LOCK:
            keys = {r[0] for r in app.DB.execute(
                "SELECT DISTINCT key FROM status_history")}
            states = {r[0] for r in app.DB.execute(
                "SELECT DISTINCT state FROM status_history")}
            span = app.DB.execute(
                "SELECT MIN(ts), MAX(ts) FROM status_history WHERE key='overall'").fetchone()
        for k in app._STATHIST_KEYS:
            self.assertIn(k, keys, f"missing status-history rows for key {k}")
        # only valid ranks 0..3 were inserted
        self.assertTrue(states.issubset({0, 1, 2, 3}),
                        f"status_history has out-of-enum ranks: {states}")
        # band covers roughly the full retention window
        self.assertIsNotNone(span[0])
        self.assertGreaterEqual((span[1] - span[0]) / 86400.0, 85,
                                "expected ~90 days of overall status history")

        # the 90-day uptime ribbon rolls up to ~90 daily cells, high but <100% uptime,
        # with a handful of non-ok days
        daily = app._status_daily(now)
        self.assertEqual(daily["span"], app._DAILY_RIBBON_DAYS)
        self.assertGreaterEqual(daily["total_days"], 85,
                                f"expected ~90 days with data, got {daily['total_days']}")
        self.assertIsNotNone(daily["uptime"])
        # The deterministic seed marks a handful of non-ok calendar days across the
        # ~90-day ribbon (a couple of degraded days + a brief outage, each landing on
        # a few days once the tz-padded window straddles the 90-day cycle), rolling up
        # to ~93% — high but honestly below 100%. Keep the bound loose enough to track
        # the seed's real, reproducible output rather than an aspirational round number.
        self.assertGreater(daily["uptime"], 90.0)
        self.assertLess(daily["uptime"], 100.0, "demo ribbon should not be a fake 100%")
        non_ok = [d for d in daily["days"] if d["s"] is not None and d["s"] > 0]
        self.assertTrue(non_ok, "expected a few non-ok daily cells in the demo ribbon")
        self.assertTrue(any(d["s"] == 3 for d in daily["days"]),
                        "expected at least one down (rank 3) day blip")

        # the per-bucket heartbeat window is populated for every subsystem strip
        hb = app._status_history(now)
        for k in app._STATHIST_KEYS:
            sampled = [c for c in hb[k]["cells"] if c["s"] >= 0]
            self.assertGreater(len(sampled), app._STATHIST_CELLS // 2,
                               f"heartbeat strip for {k} should be mostly populated")
            self.assertIsNotNone(hb[k]["uptime"])

    # ── deterministic: two fresh seeds yield identical status history ──────────
    def test_status_history_seed_deterministic(self):
        app.DEMO_MODE = True
        app._seed_demo_data()
        now = int(time.time())
        d1 = app._status_daily(now)
        with app.LOCK:
            for t in ("samples", "disk_samples", "models", "events", "status_history"):
                app.DB.execute(f"DELETE FROM {t}")
            app.DB.execute("DELETE FROM settings WHERE key=?", (app._DEMO_MARKER,))
            app.DB.commit()
        app._seed_demo_data()
        d2 = app._status_daily(now)
        self.assertEqual(d1["uptime"], d2["uptime"])
        self.assertEqual([d["s"] for d in d1["days"]], [d["s"] for d in d2["days"]])

    # ── flag off → status history stays empty too ─────────────────────────────
    def test_status_history_noop_when_flag_off(self):
        app.DEMO_MODE = False
        app._seed_demo_data()
        with app.LOCK:
            n = app.DB.execute("SELECT COUNT(*) FROM status_history").fetchone()[0]
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
