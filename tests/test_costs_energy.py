"""/api/costs/energy — kWh per hour / per day over time — and the Costs page's
Today / 7d / 30d windows, which now count CPU and DRAM next to the GPU."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app

KWH = 3_600_000.0   # watt-seconds in one kWh


def local_midnight(ts, back=0):
    lt = time.localtime(ts)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - back, 0, 0, 0, 0, 0, -1)))


class TestEnergyApi(unittest.TestCase):
    def setUp(self):
        self.now = int(time.time())
        self.hour = (self.now // 3600) * 3600
        with app.LOCK:
            app.DB.execute("DELETE FROM samples_1h")
            app.DB.execute("DELETE FROM host_samples_1h")
            app.DB.commit()
        app.save_settings({"kwh_price": "0.30", "currency": "$", "tariff_mode": "single",
                           "system_idle_watts": ""})
        self.c = app.app.test_client()

    def _hub(self, ts, gpu_kwh=0.0, cpu_kwh=0.0, dram_kwh=0.0):
        with app.LOCK:
            app.DB.execute(
                "INSERT INTO samples_1h(ts,power,cpu_power,dram_power,cnt,wsec,cpu_wsec,dram_wsec) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (ts, 0, 0, 0, 360, gpu_kwh * KWH, cpu_kwh * KWH, dram_kwh * KWH))
            app.DB.commit()

    def test_hours_sum_all_components_and_leave_gaps(self):
        self._hub(self.hour - 7200, gpu_kwh=1.0, cpu_kwh=0.5)
        self._hub(self.hour, gpu_kwh=0.2, dram_kwh=0.05)
        j = self.c.get("/api/costs/energy?by=hour").get_json()
        self.assertEqual(j["by"], "hour")
        self.assertEqual(j["days"], 7)
        pts = j["points"]
        self.assertEqual(len(pts), 3)                   # starts at the first hour with data
        self.assertEqual(pts[0]["ts"], self.hour - 7200)
        self.assertEqual(pts[-1]["ts"], self.hour)
        self.assertAlmostEqual(pts[-1]["kwh"], 0.25, places=4)
        self.assertIsNone(pts[-2]["kwh"])               # no samples that hour: a gap, not 0
        self.assertAlmostEqual(pts[-3]["kwh"], 1.5, places=4)
        self.assertAlmostEqual(pts[-3]["cost"], 0.45, places=4)
        self.assertAlmostEqual(j["total_kwh"], 1.75, places=3)
        self.assertAlmostEqual(j["total_cost"], 1.75 * 0.30, delta=0.006)   # rounded to cents
        self.assertEqual(j["filled"], 2)
        self.assertEqual(j["peak"]["ts"], self.hour - 7200)

    def test_days_bucket_by_local_calendar_day(self):
        yesterday = local_midnight(self.now, 1)
        a = ((yesterday + 3 * 3600) // 3600) * 3600
        self._hub(a, gpu_kwh=1.0)
        self._hub(a + 7200, cpu_kwh=2.0)
        j = self.c.get("/api/costs/energy?by=day").get_json()
        pts = j["points"]
        self.assertEqual(len(pts), 2)
        self.assertEqual(pts[-1]["ts"], local_midnight(self.now))
        self.assertEqual(pts[-2]["ts"], yesterday)
        self.assertAlmostEqual(pts[-2]["kwh"], 3.0, places=4)
        self.assertIsNone(pts[-1]["kwh"])

    def test_days_are_clamped_and_bad_input_falls_back(self):
        self._hub(self.hour - 40 * 86400, gpu_kwh=1.0)  # older than the 31-day cap
        self._hub(self.hour - 30 * 86400, gpu_kwh=1.0)
        j = self.c.get("/api/costs/energy?by=hour&days=999").get_json()
        self.assertEqual(j["days"], 31)
        self.assertEqual(j["filled"], 1)
        self.assertEqual(len(j["points"]), 30 * 24 + 1)
        j = self.c.get("/api/costs/energy?by=week&days=x").get_json()
        self.assertEqual((j["by"], j["days"]), ("hour", 7))
        j = self.c.get("/api/costs/energy?by=day&days=0").get_json()
        self.assertEqual(j["days"], 1)

    def test_window_starts_at_first_reading_and_keeps_later_gaps(self):
        self._hub(self.hour - 5 * 3600, gpu_kwh=1.0)
        self._hub(self.hour - 1 * 3600, gpu_kwh=1.0)
        pts = self.c.get("/api/costs/energy?by=hour").get_json()["points"]
        self.assertEqual([p["kwh"] is None for p in pts], [False, True, True, True, False, True])

    def test_no_history_means_no_points(self):
        j = self.c.get("/api/costs/energy?by=day").get_json()
        self.assertEqual((j["points"], j["filled"], j["peak"]), ([], 0, None))

    def test_remote_host_reads_its_own_history(self):
        with app.LOCK:
            app.DB.execute(
                "INSERT INTO host_samples_1h(ts,host,cnt,gpu_wsec,cpu_wsec,dram_wsec) VALUES(?,?,?,?,?,?)",
                (self.hour, "nas", 360, 0, 0.4 * KWH, 0.1 * KWH))
            app.DB.commit()
        self._hub(self.hour, gpu_kwh=9.0)               # the hub's own draw must not leak in
        j = self.c.get("/api/costs/energy?by=hour&host=nas").get_json()
        self.assertAlmostEqual(j["points"][-1]["kwh"], 0.5, places=4)
        self.assertAlmostEqual(j["total_kwh"], 0.5, places=3)

    def test_energy_without_a_tariff(self):
        app.save_settings({"kwh_price": ""})
        try:
            self._hub(self.hour, gpu_kwh=1.0)
            j = self.c.get("/api/costs/energy").get_json()
            self.assertTrue(j["ok"])
            self.assertFalse(j["enabled"])
            self.assertAlmostEqual(j["points"][-1]["kwh"], 1.0, places=4)
        finally:
            app.save_settings({"kwh_price": "0.30"})


class TestCostWindowsCountTheWholeMachine(unittest.TestCase):
    """Regression: Today / 7d / 30d summed only samples_1h.wsec, which is the
    GPU's energy alone, so a box with RAPL CPU/DRAM readings was under-billed
    in those cards while the range total (and now the energy chart) counted it."""

    def setUp(self):
        with app.LOCK:
            app.DB.execute("DELETE FROM samples_1h")   # the cards read only the rollup
            app.DB.commit()
        app.save_settings({"kwh_price": "0.30", "currency": "$", "tariff_mode": "single",
                           "system_idle_watts": ""})

    def test_today_includes_cpu_and_dram(self):
        hour = (int(time.time()) // 3600) * 3600
        with app.LOCK:
            app.DB.execute(
                "INSERT INTO samples_1h(ts,power,cpu_power,dram_power,cnt,wsec,cpu_wsec,dram_wsec) "
                "VALUES(?,?,?,?,?,?,?,?)", (hour, 0, 0, 0, 360, 0, 1.0 * KWH, 1.0 * KWH))
            app.DB.commit()
        j = app.app.test_client().get("/api/costs?range=1h").get_json()
        cost = j["machines"][0]["cost"]
        self.assertAlmostEqual(cost["today"], 0.60, places=2)   # was 0.00: GPU-only
        self.assertAlmostEqual(cost["d7"], 0.60, places=2)
        self.assertAlmostEqual(cost["d30"], 0.60, places=2)


if __name__ == "__main__":
    unittest.main()
