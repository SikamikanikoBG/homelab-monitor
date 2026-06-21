"""Unit tests for the busy-vs-quiet cost/energy heatmap: /api/cost/heatmap.

Covers grid shape (7×24), local day/hour bucketing correctness, tariff-aware
cost-rate math matching the shared cost helper, busy/quiet rollup extremes,
thin-history -> clean 'not ready' (no crash), cost-disabled -> power-only path,
and always-200.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _wipe():
    with app.LOCK:
        app.DB.execute("DELETE FROM samples")
        app.DB.commit()


def _insert(ts, power, cpu=0, dram=0):
    with app.LOCK:
        app.DB.execute(
            "INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp,cpu,"
            "ram_used,ram_total,load1,ctemp,cpu_power,dram_power) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, 50, 8000, 24000, power, 60, 30, 1000, 2000, 1.0, 50, cpu, dram))
        app.DB.commit()


def _ts_for(wday, hour):
    """A unix ts in the current week whose LOCAL weekday==wday and hour==hour."""
    now = int(time.time())
    base = time.localtime(now)
    # step back to this week's Monday local-midnight
    monday = now - base.tm_wday * 86400 - base.tm_hour * 3600 - base.tm_min * 60 - base.tm_sec
    # mktime round-trips local time correctly across DST
    lt = time.localtime(monday + wday * 86400)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 30, 0, 0, 0, -1)))


class TestHeatmapShapeAndBucketing(unittest.TestCase):
    def setUp(self):
        _wipe()
        app.save_settings({"kwh_price": "0.30", "currency": "$", "tariff_mode": "single",
                           "kwh_price_night": "", "system_idle_watts": ""})

    def test_grid_shape_always_7x24(self):
        j = app.app.test_client().get("/api/cost/heatmap").get_json()
        self.assertTrue(j["ok"])
        self.assertEqual((j["rows"], j["cols"]), (7, 24))
        self.assertEqual(len(j["avg_w"]), 7)
        self.assertTrue(all(len(r) == 24 for r in j["avg_w"]))
        self.assertEqual(len(j["cost_h"]), 7)
        self.assertEqual(len(j["samples"]), 7)

    def test_buckets_into_correct_local_day_hour(self):
        # Tue (wday=1) 14:00 cell gets 100W, Thu (wday=3) 03:00 gets 300W
        _insert(_ts_for(1, 14), 100)
        _insert(_ts_for(3, 3), 300)
        j = app.app.test_client().get("/api/cost/heatmap").get_json()
        self.assertEqual(j["avg_w"][1][14], 100)
        self.assertEqual(j["avg_w"][3][3], 300)
        self.assertEqual(j["samples"][1][14], 1)
        # untouched cell stays empty
        self.assertIsNone(j["avg_w"][0][0])

    def test_total_w_includes_cpu_and_dram(self):
        _insert(_ts_for(2, 10), 100, cpu=40, dram=10)   # total 150
        j = app.app.test_client().get("/api/cost/heatmap").get_json()
        self.assertEqual(j["avg_w"][2][10], 150)

    def test_cell_is_mean_of_its_ticks(self):
        t = _ts_for(4, 9)
        _insert(t, 100); _insert(t + 10, 200); _insert(t + 20, 300)
        j = app.app.test_client().get("/api/cost/heatmap").get_json()
        self.assertEqual(j["avg_w"][4][9], 200)
        self.assertEqual(j["samples"][4][9], 3)


class TestHeatmapCostMath(unittest.TestCase):
    def setUp(self):
        _wipe()

    def test_single_tariff_cost_rate(self):
        app.save_settings({"kwh_price": "0.40", "currency": "$", "tariff_mode": "single",
                           "kwh_price_night": ""})
        _insert(_ts_for(0, 12), 500)        # 500 W = 0.5 kW
        j = app.app.test_client().get("/api/cost/heatmap").get_json()
        self.assertTrue(j["enabled"])
        # €/h = 0.5 kW * 0.40 €/kWh = 0.20
        self.assertAlmostEqual(j["cost_h"][0][12], 0.20, places=4)

    def test_dual_tariff_uses_night_price_overnight(self):
        app.save_settings({"kwh_price": "0.40", "kwh_price_night": "0.10",
                           "tariff_mode": "dual", "night_start": "22:00",
                           "night_end": "06:00", "currency": "$"})
        _insert(_ts_for(0, 3), 1000)        # 03:00 -> night band, 1 kW
        _insert(_ts_for(0, 14), 1000)       # 14:00 -> day band, 1 kW
        j = app.app.test_client().get("/api/cost/heatmap").get_json()
        self.assertAlmostEqual(j["cost_h"][0][3], 0.10, places=4)   # night price
        self.assertAlmostEqual(j["cost_h"][0][14], 0.40, places=4)  # day price


class TestHeatmapRollups(unittest.TestCase):
    def setUp(self):
        _wipe()
        app.save_settings({"kwh_price": "0.30", "currency": "$", "tariff_mode": "single",
                           "kwh_price_night": ""})

    def test_busiest_and_quietest(self):
        _insert(_ts_for(2, 16), 800)        # busiest
        _insert(_ts_for(6, 4), 50)          # quietest
        _insert(_ts_for(3, 10), 300)
        j = app.app.test_client().get("/api/cost/heatmap").get_json()
        self.assertEqual((j["busiest"]["day"], j["busiest"]["hour"]), (2, 16))
        self.assertEqual(j["busiest"]["avg_w"], 800)
        self.assertEqual((j["quietest"]["day"], j["quietest"]["hour"]), (6, 4))
        self.assertEqual(j["quietest"]["avg_w"], 50)

    def test_busy_quiet_bands(self):
        # 8 populated cells: 100..800 step 100; quartile = 2 cells each end
        for i, w in enumerate(range(100, 900, 100)):
            _insert(_ts_for(i % 7, i), w)
        j = app.app.test_client().get("/api/cost/heatmap").get_json()
        self.assertIsNotNone(j["bands"])
        self.assertGreater(j["bands"]["busy"]["avg_w"], j["bands"]["quiet"]["avg_w"])
        # busy band = top quartile (700,800) -> 750; quiet = (100,200) -> 150
        self.assertEqual(j["bands"]["busy"]["avg_w"], 750)
        self.assertEqual(j["bands"]["quiet"]["avg_w"], 150)


class TestHeatmapDegrade(unittest.TestCase):
    def test_thin_history_not_ready_no_crash(self):
        _wipe()
        app.save_settings({"kwh_price": "0.30", "currency": "$", "tariff_mode": "single"})
        _insert(_ts_for(0, 0), 100)         # a single tick
        r = app.app.test_client().get("/api/cost/heatmap")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertFalse(j["ready"])
        self.assertIsNone(j["bands"])

    def test_empty_history_always_200(self):
        _wipe()
        app.save_settings({"kwh_price": "0.30", "currency": "$", "tariff_mode": "single"})
        r = app.app.test_client().get("/api/cost/heatmap")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertFalse(j["ready"])
        self.assertEqual(j["total_ticks"], 0)
        self.assertIsNone(j["busiest"])

    def test_cost_disabled_power_only_path(self):
        _wipe()
        app.save_settings({"kwh_price": "", "currency": "$", "tariff_mode": "single"})
        _insert(_ts_for(1, 11), 250)
        r = app.app.test_client().get("/api/cost/heatmap")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertFalse(j["enabled"])
        self.assertEqual(j["avg_w"][1][11], 250)     # power still present
        self.assertEqual(j["cost_h"][1][11], 0.0)    # cost rate is zero (no price)

    def test_days_window_capped(self):
        _wipe()
        app.save_settings({"kwh_price": "0.30"})
        j = app.app.test_client().get("/api/cost/heatmap?days=9999").get_json()
        self.assertLessEqual(j["days"], 365)
        j2 = app.app.test_client().get("/api/cost/heatmap?days=bad").get_json()
        self.assertEqual(j2["days"], 30)            # default on garbage


if __name__ == "__main__":
    unittest.main()
