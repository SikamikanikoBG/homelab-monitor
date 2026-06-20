"""Unit tests for the Forecasts feature (disk-fill ETA + cost-this-month
projection) — pure-Python stats over the SQLite history, no new deps."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class TestLinFit(unittest.TestCase):
    def test_perfect_line(self):
        xs = [0, 1, 2, 3, 4]
        ys = [1, 3, 5, 7, 9]          # y = 2x + 1
        slope, intercept, r2 = app._linfit(xs, ys)
        self.assertAlmostEqual(slope, 2.0, places=6)
        self.assertAlmostEqual(intercept, 1.0, places=6)
        self.assertAlmostEqual(r2, 1.0, places=6)

    def test_too_few_points(self):
        self.assertIsNone(app._linfit([1], [1]))

    def test_zero_x_variance(self):
        self.assertIsNone(app._linfit([5, 5, 5], [1, 2, 3]))

    def test_flat_series_zero_slope(self):
        slope, intercept, _ = app._linfit([0, 1, 2, 3], [10, 10, 10, 10])
        self.assertAlmostEqual(slope, 0.0, places=6)
        self.assertAlmostEqual(intercept, 10.0, places=6)


class TestDiskForecast(unittest.TestCase):
    def setUp(self):
        with app.LOCK:
            app.DB.execute("DELETE FROM disk_samples")
            app.DB.commit()

    def _seed(self, mount, start_used, gb_per_day, total, days=20, step_h=6):
        now = int(time.time())
        with app.LOCK:
            n = int(days * 24 / step_h)
            for i in range(n):
                ts = now - days * 86400 + i * step_h * 3600
                used = start_used + gb_per_day * ((ts - (now - days * 86400)) / 86400.0)
                app.DB.execute("INSERT INTO disk_samples(ts,mount,used,total) VALUES(?,?,?,?)",
                               (ts, mount, round(used, 2), total))
            app.DB.commit()
        return now

    def test_filling_disk_gives_eta(self):
        # +5 GB/day over 10 days -> latest used ~150 GB of 200 GB; ~10 days left.
        self._seed("/data", 100.0, 5.0, 200.0, days=10)
        with app.LOCK:
            res = app._disk_forecasts(app.DB.cursor(), int(time.time()))
        d = next(x for x in res if x["mount"] == "/data")
        self.assertEqual(d["status"], "filling")
        self.assertAlmostEqual(d["gb_per_day"], 5.0, delta=0.5)
        self.assertTrue(5 < d["eta_days"] < 15)
        self.assertIsNotNone(d["eta_ts"])

    def test_stable_disk_no_eta(self):
        self._seed("/flat", 50.0, 0.0, 500.0)
        with app.LOCK:
            res = app._disk_forecasts(app.DB.cursor(), int(time.time()))
        d = next(x for x in res if x["mount"] == "/flat")
        self.assertEqual(d["status"], "stable")
        self.assertIsNone(d["eta_days"])

    def test_insufficient_history_collecting(self):
        now = int(time.time())
        with app.LOCK:
            app.DB.execute("INSERT INTO disk_samples(ts,mount,used,total) VALUES(?,?,?,?)",
                           (now - 100, "/new", 10.0, 100.0))
            app.DB.commit()
            res = app._disk_forecasts(app.DB.cursor(), now)
        d = next(x for x in res if x["mount"] == "/new")
        self.assertEqual(d["status"], "collecting")


class TestAnomalies(unittest.TestCase):
    def setUp(self):
        with app.LOCK:
            app.DB.execute("DELETE FROM samples")
            app.DB.commit()

    def _seed_util(self, baseline_vals, latest, n_extra=0):
        """Seed `samples` with a util series: baseline_vals then `latest` last."""
        now = int(time.time())
        seq = list(baseline_vals) + [latest]
        with app.LOCK:
            for i, v in enumerate(seq):
                ts = now - (len(seq) - i) * app.INTERVAL
                app.DB.execute(
                    "INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp) "
                    "VALUES(?,?,?,?,?,?)", (ts, v, 0, 0, 0, 0))
            app.DB.commit()
        return now

    def test_spike_is_flagged(self):
        # 60 quiet readings around 20% then a 95% spike -> clear anomaly.
        base = [20.0 + (i % 3) for i in range(60)]
        now = self._seed_util(base, 95.0)
        with app.LOCK:
            res = app._zscore_anomalies(app.DB.cursor(), now)
        self.assertEqual(res["status"], "quiet")
        util = [a for a in res["items"] if a["key"] == "gpu_util"]
        self.assertTrue(util, "expected a gpu_util anomaly")
        self.assertEqual(util[0]["direction"], "spike")
        self.assertGreaterEqual(abs(util[0]["z"]), res["threshold"])

    def test_dip_direction(self):
        base = [90.0 + (i % 3) for i in range(60)]
        now = self._seed_util(base, 5.0)
        with app.LOCK:
            res = app._zscore_anomalies(app.DB.cursor(), now)
        util = [a for a in res["items"] if a["key"] == "gpu_util"]
        self.assertTrue(util)
        self.assertEqual(util[0]["direction"], "dip")

    def test_flat_series_no_false_alarm(self):
        # Perfectly flat (zero variance) -> below min stddev -> never flagged.
        now = self._seed_util([42.0] * 60, 42.0)
        with app.LOCK:
            res = app._zscore_anomalies(app.DB.cursor(), now)
        self.assertEqual(res["status"], "quiet")
        self.assertFalse([a for a in res["items"] if a["key"] == "gpu_util"])

    def test_normal_reading_not_flagged(self):
        base = [20.0 + (i % 5) for i in range(60)]
        now = self._seed_util(base, 22.0)        # within the normal band
        with app.LOCK:
            res = app._zscore_anomalies(app.DB.cursor(), now)
        self.assertFalse([a for a in res["items"] if a["key"] == "gpu_util"])

    def test_insufficient_history_collecting(self):
        now = self._seed_util([20.0, 21.0, 22.0], 95.0)   # only 4 points total
        with app.LOCK:
            res = app._zscore_anomalies(app.DB.cursor(), now)
        self.assertEqual(res["status"], "collecting")
        self.assertEqual(res["items"], [])

    def test_endpoint_exposes_anomalies_block(self):
        j = app.app.test_client().get("/api/forecast").get_json()
        self.assertIn("anomalies", j)
        self.assertIn("items", j["anomalies"])
        self.assertIsInstance(j["anomalies"]["items"], list)
        self.assertIn(j["anomalies"]["status"], ("quiet", "collecting"))


class TestForecastEndpoint(unittest.TestCase):
    def test_endpoint_shape_and_no_crash(self):
        j = app.app.test_client().get("/api/forecast").get_json()
        self.assertIn("disk", j)
        self.assertIn("cost_month", j)
        self.assertIsInstance(j["disk"], list)

    def test_cost_projection_disabled_without_price(self):
        app.save_settings({"kwh_price": "", "currency": "$"})
        j = app.app.test_client().get("/api/forecast").get_json()
        self.assertFalse(j["cost_month"]["enabled"])

    def test_cost_projection_enabled_with_price(self):
        # Seed this-month samples and set a price -> projection should be enabled
        # and >= month-to-date.
        now = int(time.time())
        lt = time.localtime(now)
        month_start = int(time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)))
        with app.LOCK:
            app.DB.execute("DELETE FROM samples WHERE ts>=?", (month_start,))
            ts = month_start + 3600
            while ts < now:
                app.DB.execute("INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp) "
                               "VALUES(?,?,?,?,?,?)", (ts, 0, 0, 0, 200, 0))
                ts += app.INTERVAL * 30
            app.DB.commit()
        app.save_settings({"kwh_price": "0.30", "currency": "€", "tariff_mode": "single"})
        cm = app.app.test_client().get("/api/forecast").get_json()["cost_month"]
        self.assertTrue(cm["enabled"])
        self.assertGreaterEqual(cm["projected_month"], cm["month_to_date"])


if __name__ == "__main__":
    unittest.main()
