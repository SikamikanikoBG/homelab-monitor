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
            for t in ("samples", "disk_samples", "models", "events"):
                app.DB.execute(f"DELETE FROM {t}")
            app.DB.execute("DELETE FROM settings WHERE key IN (?, 'kwh_price', 'currency')",
                           (app._DEMO_MARKER,))
            app.DB.commit()

    def tearDown(self):
        app.DEMO_MODE = self._demo_was
        with app.LOCK:
            for t in ("samples", "disk_samples", "models", "events"):
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


if __name__ == "__main__":
    unittest.main()
