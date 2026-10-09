"""Unit tests for per-host cost storage + the /api/costs?host= mode: the
host_samples repo (raw + 1h rollup), the poller's write path, the rename
cascade, and the API's energy/cost math for a remote host."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
from backend.db.repos import host_samples as hs_repo


def _clean(*hosts):
    with app.LOCK:
        for h in hosts:
            app.DB.execute("DELETE FROM host_samples WHERE host=?", (h,))
            app.DB.execute("DELETE FROM host_samples_1h WHERE host=?", (h,))
        app.DB.commit()
    with app.HOST_DATA_LOCK:
        for h in hosts:
            app.HOST_DATA.pop(h, None)


class TestHostSamplesRepo(unittest.TestCase):
    def tearDown(self):
        _clean("t1")

    def test_record_writes_raw_and_rollup(self):
        ts = 1_700_000_000
        with app.LOCK:
            hs_repo.record(app.DB, ts, "t1", 10, cpu=10, gpu_power=100, cpu_power=50)
            hs_repo.record(app.DB, ts + 10, "t1", 10, cpu=30, gpu_power=300, cpu_power=70)
            app.DB.commit()
            raw = app.DB.execute(
                "SELECT COUNT(*) FROM host_samples WHERE host='t1'").fetchone()[0]
            row = app.DB.execute(
                "SELECT gpu_power, cpu_power, cpu, cnt FROM host_samples_1h WHERE host='t1'"
            ).fetchone()
        self.assertEqual(raw, 2)
        self.assertAlmostEqual(row[0], 200.0)   # rollup averages the two polls
        self.assertAlmostEqual(row[1], 60.0)
        self.assertAlmostEqual(row[2], 20.0)
        self.assertEqual(row[3], 2)

    def test_absent_sensor_stays_null_not_zero(self):
        ts = 1_700_000_000
        with app.LOCK:
            hs_repo.record(app.DB, ts, "t1", 10, cpu=10)               # no GPU, no RAPL
            hs_repo.record(app.DB, ts + 10, "t1", 10, cpu=20, gpu_power=100)
            app.DB.commit()
            row = app.DB.execute(
                "SELECT gpu_power, cpu_power, cnt FROM host_samples_1h WHERE host='t1'"
            ).fetchone()
        # Energy math reads avg*cnt, so a tick where the sensor didn't report
        # must count as 0 in the mean for the reconstructed sum to be honest:
        # (NULL, 100) over 2 ticks → avg 50, avg*cnt = 100 W-ticks. A sensor
        # that NEVER reports stays NULL, so "absent" is still distinct from
        # "0 W" — that's what `measured` keys off.
        self.assertAlmostEqual(row[0], 50.0)
        self.assertIsNone(row[1])
        self.assertEqual(row[2], 2)


class TestPollerWrite(unittest.TestCase):
    def tearDown(self):
        _clean("t2")

    def test_record_host_sample_extracts_payload(self):
        app._record_host_sample("t2", {
            "cpu": 12.5, "ram_used": 4000, "ram_total": 64000, "load1": 0.5,
            "ctemp": 40, "cpu_power": 55.5,
            "gpu": {"count": 3, "util": 10, "mem_used": 60000, "mem_total": 73728,
                    "power": 400, "temp": 60},
        })
        with app.LOCK:
            row = app.DB.execute(
                "SELECT cpu, gpu_power, gpu_mem_total, cpu_power, dram_power "
                "FROM host_samples WHERE host='t2'").fetchone()
        self.assertEqual(row, (12.5, 400, 73728, 55.5, None))

    def test_gpuless_host_writes_nulls(self):
        app._record_host_sample("t2", {"cpu": 5, "ram_used": 100, "ram_total": 200})
        with app.LOCK:
            row = app.DB.execute(
                "SELECT cpu, gpu_power, cpu_power FROM host_samples WHERE host='t2'"
            ).fetchone()
        self.assertEqual(row, (5, None, None))


class TestRenameFollowsHistory(unittest.TestCase):
    def tearDown(self):
        _clean("t3", "t3new")
        with app.LOCK:
            app.DB.execute("DELETE FROM hosts WHERE name IN ('t3','t3new')")
            app.DB.commit()

    def test_rename_moves_host_samples(self):
        with app.LOCK:
            app.DB.execute("INSERT INTO hosts(name, ssh_target, added_at) VALUES('t3','u@h',0)")
            hs_repo.record(app.DB, 1_700_000_000, "t3", 10, gpu_power=100)
            app.DB.commit()
        app.rename_host("t3", "t3new")
        with app.LOCK:
            hosts = [r[0] for r in app.DB.execute(
                "SELECT DISTINCT host FROM host_samples WHERE host IN ('t3','t3new')")]
            hosts_1h = [r[0] for r in app.DB.execute(
                "SELECT DISTINCT host FROM host_samples_1h WHERE host IN ('t3','t3new')")]
        self.assertEqual(hosts, ["t3new"])
        self.assertEqual(hosts_1h, ["t3new"])


class TestApiCostsHost(unittest.TestCase):
    HOST = "t4"

    def setUp(self):
        self.now = int(time.time())
        _clean(self.HOST)
        with app.LOCK:
            # Six 10s polls of 200 W GPU + 60 W CPU + 8 W DRAM, pre-rolled to 1h
            # the same way the poller's upsert would land them.
            for i in range(6):
                hs_repo.record(app.DB, self.now - 100 + i * 10, self.HOST, 10,
                               cpu=30, gpu_power=200, cpu_power=60, dram_power=8)
            app.DB.commit()
        with app.HOST_DATA_LOCK:
            app.HOST_DATA[self.HOST] = {"data": {"host": {
                "gpu": {"power": 250}, "cpu_power": 61.0}}, "at": self.now}
        app.save_settings({"kwh_price": "0.30", "currency": "$", "tariff_mode": "single",
                           "system_idle_watts": ""})

    def tearDown(self):
        _clean(self.HOST)

    def test_host_mode_prices_host_history(self):
        j = app.app.test_client().get(f"/api/costs?range=1h&host={self.HOST}").get_json()
        self.assertTrue(j["enabled"])
        self.assertEqual(j["host"], self.HOST)
        m = j["machines"][0]
        self.assertEqual(m["name"], self.HOST)
        self.assertEqual(sorted(m["measured"]), ["cpu", "dram", "gpu"])
        # 6 ticks * 268 W * 10s = 4.4667 Wh ≈ 0.004 kWh
        expect_kwh = 6 * (200 + 60 + 8) * 10 / 3_600_000.0
        self.assertAlmostEqual(m["energy_kwh"]["total"], round(expect_kwh, 3), places=3)
        self.assertAlmostEqual(m["cost_range"], round(expect_kwh * 0.30, 2), places=2)
        self.assertEqual(m["now_w"]["gpu"], 250)
        self.assertEqual(m["now_w"]["cpu"], 61)
        self.assertEqual(j["breakdown"], [])          # no remote attribution yet
        self.assertIn("gpu", j["components"])
        self.assertIn("cpu", j["components"])

    def test_gpu_only_host_omits_rapl_components(self):
        _clean(self.HOST)
        with app.LOCK:
            hs_repo.record(app.DB, self.now - 50, self.HOST, 10, cpu=10, gpu_power=300)
            app.DB.commit()
        j = app.app.test_client().get(f"/api/costs?range=1h&host={self.HOST}").get_json()
        m = j["machines"][0]
        self.assertEqual(m["measured"], ["gpu"])
        self.assertNotIn("cpu", j["components"])
        self.assertFalse(j["rapl_available"])

    def test_unmeasurable_host_reports_nothing_honestly(self):
        _clean(self.HOST)
        with app.LOCK:
            hs_repo.record(app.DB, self.now - 50, self.HOST, 10, cpu=10)   # vitals only
            app.DB.commit()
        j = app.app.test_client().get(f"/api/costs?range=1h&host={self.HOST}").get_json()
        m = j["machines"][0]
        self.assertEqual(m["measured"], [])
        self.assertEqual(m["energy_kwh"]["total"], 0)

    def test_local_path_unchanged(self):
        j = app.app.test_client().get("/api/costs?range=1h").get_json()
        self.assertNotIn("host", j)
        self.assertEqual(j["machines"][0]["name"], "local")


if __name__ == "__main__":
    unittest.main()
