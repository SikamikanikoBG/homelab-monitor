"""Unit tests for the power & cost estimator endpoint (issue #25)."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class TestCost(unittest.TestCase):
    def _seed_hour_at(self, watts):
        """Fill the last hour with `watts` power samples, one per INTERVAL."""
        now = int(time.time())
        with app.LOCK:
            app.DB.execute("DELETE FROM samples")
            app.DB.execute("DELETE FROM samples_1h")
            n = 3600 // app.INTERVAL
            for i in range(n):
                ts = now - 3600 + i * app.INTERVAL
                app.DB.execute("INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp,interval_sec) "
                               "VALUES(?,?,?,?,?,?,?)", (ts, 0, 0, 0, watts, 0, app.INTERVAL))
            app.DB.commit()
            app.DB.executescript("""
                INSERT OR IGNORE INTO samples_1h(ts,util,mem_used,mem_total,power,temp,
                    cpu,ram_used,ram_total,load1,ctemp,cpu_power,dram_power,cnt,wsec)
                SELECT (ts/3600)*3600, AVG(util), AVG(mem_used), AVG(mem_total), AVG(power), AVG(temp),
                    AVG(cpu), AVG(ram_used), AVG(ram_total), AVG(load1), AVG(ctemp),
                    AVG(cpu_power), AVG(dram_power), COUNT(*), SUM(COALESCE(power,0)*interval_sec)
                FROM samples GROUP BY (ts/3600)*3600;
            """)
            app.DB.commit()

    def test_disabled_without_price(self):
        app.save_settings({"kwh_price": "", "currency": "$"})
        j = app.app.test_client().get("/api/cost?range=30d").get_json()
        self.assertFalse(j["enabled"])

    def test_250w_for_an_hour_at_0_30(self):
        # 250 W for 1 h = 0.25 kWh -> 0.075 at 0.30/kWh (issue acceptance criterion).
        self._seed_hour_at(250)
        app.save_settings({"kwh_price": "0.30", "currency": "€", "tariff_mode": "single"})
        j = app.app.test_client().get("/api/cost?range=30d").get_json()
        self.assertTrue(j["enabled"])
        self.assertEqual(j["currency"], "€")
        self.assertEqual(j["avg_24h_w"], 250)
        self.assertAlmostEqual(j["kwh"]["d30"], 0.25, delta=0.01)
        self.assertAlmostEqual(j["cost"]["d30"], 0.075, delta=0.01)
        self.assertTrue(len(j["series"]["cost_cum"]) > 0)
        # cumulative series is monotonic non-decreasing and ends near the total
        cc = j["series"]["cost_cum"]
        self.assertTrue(all(cc[i] <= cc[i + 1] + 1e-9 for i in range(len(cc) - 1)))


class TestNightWindow(unittest.TestCase):
    def test_wrapping_window(self):
        is_night = app._make_is_night("22:00", "06:00")
        lt = time.localtime()
        ts = lambda h: int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, 0, 0, 0, 0, -1)))
        self.assertTrue(is_night(ts(2)))     # 02:00 is night
        self.assertTrue(is_night(ts(23)))    # 23:00 is night
        self.assertFalse(is_night(ts(14)))   # 14:00 is day
        self.assertFalse(is_night(ts(6)))    # 06:00 boundary is day (end exclusive)

    def test_same_day_window(self):
        is_night = app._make_is_night("01:00", "05:00")
        lt = time.localtime()
        ts = lambda h: int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, 0, 0, 0, 0, -1)))
        self.assertTrue(is_night(ts(3)))
        self.assertFalse(is_night(ts(22)))

    def test_empty_window_is_all_day(self):
        is_night = app._make_is_night("06:00", "06:00")
        self.assertFalse(is_night(int(time.time())))

    def test_junk_falls_back_to_defaults(self):
        is_night = app._make_is_night("nonsense", "")
        lt = time.localtime()
        ts = lambda h: int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, 0, 0, 0, 0, -1)))
        self.assertTrue(is_night(ts(2)))     # defaults to 22:00–06:00
        self.assertFalse(is_night(ts(14)))


class TestDualTariff(unittest.TestCase):
    def _seed_hour(self, hour, watts):
        """Fill one full local-clock hour (hour:00–hour:59) with `watts` samples."""
        lt = time.localtime()
        base = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 0, 0, 0, 0, -1)))
        with app.LOCK:
            for i in range(3600 // app.INTERVAL):
                ts = base + i * app.INTERVAL
                app.DB.execute("INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp,interval_sec) "
                               "VALUES(?,?,?,?,?,?,?)", (ts, 0, 0, 0, watts, 0, app.INTERVAL))
            app.DB.commit()
            app.DB.executescript("""
                INSERT OR IGNORE INTO samples_1h(ts,util,mem_used,mem_total,power,temp,
                    cpu,ram_used,ram_total,load1,ctemp,cpu_power,dram_power,cnt,wsec)
                SELECT (ts/3600)*3600, AVG(util), AVG(mem_used), AVG(mem_total), AVG(power), AVG(temp),
                    AVG(cpu), AVG(ram_used), AVG(ram_total), AVG(load1), AVG(ctemp),
                    AVG(cpu_power), AVG(dram_power), COUNT(*), SUM(COALESCE(power,0)*interval_sec)
                FROM samples GROUP BY (ts/3600)*3600;
            """)
            app.DB.commit()

    def setUp(self):
        with app.LOCK:
            app.DB.execute("DELETE FROM samples")
            app.DB.execute("DELETE FROM samples_1h")
            app.DB.commit()

    def tearDown(self):
        # Settings persist in a shared table — restore single mode so we don't
        # leak dual config into other tests / later runs.
        app.save_settings({"tariff_mode": "single", "kwh_price_night": "",
                           "night_start": "22:00", "night_end": "06:00"})

    def test_split_day_and_night(self):
        self._seed_hour(2, 250)    # 02:00–03:00 night (default window 22:00–06:00) -> 0.25 kWh
        self._seed_hour(14, 100)   # 14:00–15:00 day -> 0.10 kWh
        app.save_settings({"kwh_price": "0.30", "kwh_price_night": "0.10",
                           "currency": "€", "tariff_mode": "dual",
                           "night_start": "22:00", "night_end": "06:00"})
        j = app.app.test_client().get("/api/cost?range=30d").get_json()
        self.assertEqual(j["tariff"]["mode"], "dual")
        self.assertAlmostEqual(j["split"]["d30"]["night_kwh"], 0.25, delta=0.01)
        self.assertAlmostEqual(j["split"]["d30"]["day_kwh"], 0.10, delta=0.01)
        # total kwh = day + night; total cost = day*0.30 + night*0.10 = 0.03 + 0.025 = 0.055
        self.assertAlmostEqual(j["kwh"]["d30"], 0.35, delta=0.01)
        self.assertAlmostEqual(j["cost"]["d30"], 0.055, delta=0.005)
        # day/night component costs add up to the window total
        s = j["split"]["d30"]
        self.assertAlmostEqual(s["day_cost"] + s["night_cost"], j["cost"]["d30"], delta=0.011)

    def test_blank_night_price_degrades_to_single(self):
        self._seed_hour(2, 200)    # 0.20 kWh, all night by clock — but single mode ignores window
        app.save_settings({"kwh_price": "0.40", "kwh_price_night": "",
                           "currency": "$", "tariff_mode": "dual"})
        j = app.app.test_client().get("/api/cost?range=30d").get_json()
        self.assertEqual(j["tariff"]["mode"], "single")   # blank night => single
        self.assertEqual(j["split"]["d30"]["night_kwh"], 0.0)
        self.assertAlmostEqual(j["cost"]["d30"], round(0.20 * 0.40, 2), delta=0.005)


if __name__ == "__main__":
    unittest.main()
