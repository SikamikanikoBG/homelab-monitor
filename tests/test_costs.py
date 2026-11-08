"""Unit tests for the Costs page: RAPL watts, per-process power attribution,
and the /api/costs + /api/costs/entity endpoints."""
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class TestRapl(unittest.TestCase):
    def test_watts_from_delta_and_wraparound(self):
        app._RAPL_PREV.clear()
        # one package domain; second read wraps the uint counter (smaller than prev)
        reads = iter([(900_000_000, 1_000_000_000), (100_000_000, 1_000_000_000)])
        with patch("app._rapl_domains", return_value={"/p": "package-0"}), \
             patch("app._rapl_read_uj", side_effect=lambda p: next(reads)), \
             patch("app.time.monotonic", side_effect=[100.0, 110.0]):   # one call per invocation
            r1 = app.read_rapl_power()       # seeds, no delta yet
            r2 = app.read_rapl_power()
        self.assertEqual(r1, {})
        # de = (1e8 - 9e8) + 1e9 = 2e8 µJ over 10 s = 20 W
        self.assertAlmostEqual(r2["cpu_power"], 20.0, places=1)

    def test_absent_degrades_to_empty(self):
        with patch("app._rapl_domains", return_value={}):
            self.assertEqual(app.read_rapl_power(), {})

    def test_dram_subdomain(self):
        app._RAPL_PREV.clear()
        reads = {"/pkg": iter([(0, 10**12), (650_000_000, 10**12)]),
                 "/dram": iter([(0, 10**12), (130_000_000, 10**12)])}
        with patch("app._rapl_domains", return_value={"/pkg": "package-0", "/dram": "dram"}), \
             patch("app._rapl_read_uj", side_effect=lambda p: next(reads[p])), \
             patch("app.time.monotonic", side_effect=[0.0, 10.0]):   # one call per invocation
            app.read_rapl_power()
            r = app.read_rapl_power()
        self.assertAlmostEqual(r["cpu_power"], 65.0, places=1)    # 650 J / 10 s
        self.assertAlmostEqual(r["dram_power"], 13.0, places=1)   # 130 J / 10 s


class TestAttribution(unittest.TestCase):
    def test_gpu_vram_share_and_cpu_time_share(self):
        rows = app._attribute_power_rows(
            1000, 300.0, {"ollama": 9000, "comfy": 3000}, 60.0,
            {"ncpu": 4, "by_cpu": [{"name": "python", "cpu_pct": 200},
                                   {"name": "idle", "cpu_pct": 0.1}]})
        d = {(k, n): w for (_, k, n, w) in rows}
        self.assertAlmostEqual(d[("gpu", "ollama")], 225.0, places=1)   # 300 * 9000/12000
        self.assertAlmostEqual(d[("gpu", "comfy")], 75.0, places=1)     # 300 * 3000/12000
        self.assertAlmostEqual(d[("cpu", "python")], 30.0, places=1)    # 60 * (200/100)/4
        self.assertNotIn(("cpu", "idle"), d)                            # below 0.5 W floor

    def test_no_cpu_power_means_no_cpu_rows(self):
        rows = app._attribute_power_rows(1000, 100.0, {"x": 1000}, None,
                                         {"ncpu": 1, "by_cpu": [{"name": "p", "cpu_pct": 50}]})
        self.assertTrue(all(k != "cpu" for (_, k, _n, _w) in rows))


class TestApiCosts(unittest.TestCase):
    def setUp(self):
        self.now = int(time.time())
        with app.LOCK:
            app.DB.execute("DELETE FROM samples")
            app.DB.execute("DELETE FROM samples_1h")
            app.DB.execute("DELETE FROM power_proc")
            for i in range(6):
                ts = self.now - 100 + i * 10
                app.DB.execute(
                    "INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp,cpu,ram_used,"
                    "ram_total,load1,ctemp,cpu_power,dram_power,interval_sec) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, 50, 8000, 24000, 200, 60, 30, 1000, 2000, 1.0, 50, 60, 8, 10))
                app.DB.execute("INSERT INTO power_proc(ts,kind,name,watts,interval_sec) VALUES(?,?,?,?,?)",
                                (ts, "gpu", "ollama", 150, 10))
                app.DB.execute("INSERT INTO power_proc(ts,kind,name,watts,interval_sec) VALUES(?,?,?,?,?)",
                                (ts, "cpu", "python", 20, 10))
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
        app.save_settings({"kwh_price": "0.30", "currency": "$", "tariff_mode": "single",
                           "system_idle_watts": ""})

    def test_costs_overview(self):
        j = app.app.test_client().get("/api/costs?range=1h").get_json()
        self.assertTrue(j["enabled"])
        self.assertTrue(j["rapl_available"])
        m = j["machines"][0]
        self.assertEqual(sorted(m["measured"]), ["cpu", "dram", "gpu"])
        self.assertIn("cpu", j["components"])
        self.assertIn("dram", j["components"])
        names = {b["name"]: b for b in j["breakdown"]}
        self.assertIn("ollama", names)
        self.assertIn("python", names)
        self.assertGreater(names["ollama"]["energy_kwh"], 0)
        self.assertGreater(names["ollama"]["cost"], 0)

    def test_other_baseline_opt_in(self):
        app.save_settings({"system_idle_watts": "50"})
        j = app.app.test_client().get("/api/costs?range=1h").get_json()
        self.assertIn("other", j["components"])
        self.assertEqual(j["machines"][0]["estimated"], ["other"])
        app.save_settings({"system_idle_watts": ""})           # restore

    def test_entity_drilldown(self):
        j = app.app.test_client().get("/api/costs/entity?name=ollama&kind=gpu&range=1h").get_json()
        self.assertEqual(j["name"], "ollama")
        self.assertGreater(j["energy_kwh"], 0)
        self.assertGreater(len(j["series"]["watts"]), 0)
        self.assertEqual(len(j["series"]["watts"]), len(j["series"]["cost_cum"]))


if __name__ == "__main__":
    unittest.main()
