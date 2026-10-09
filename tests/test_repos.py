"""Unit tests for backend/db/repos/ helpers.

Each test creates a fresh in-memory SQLite DB, seeds it, then calls the
repo helper with conn= explicitly — no thread-local factory involved.
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.db.repos import samples, settings, uptime, costs


def _samples_db():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE samples(ts INTEGER PRIMARY KEY, util REAL, "
        "mem_used REAL, mem_total REAL, power REAL, temp REAL, interval_sec INTEGER)"
    )
    db.execute(
        "CREATE TABLE samples_1h(ts INTEGER PRIMARY KEY, util REAL, "
        "mem_used REAL, mem_total REAL, power REAL, temp REAL, cnt INTEGER, wsec REAL)"
    )
    db.commit()
    return db


def _settings_db():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
    db.commit()
    return db


def _uptime_db():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE uptime_checks("
        "id TEXT PRIMARY KEY, label TEXT, type TEXT, target TEXT, "
        "interval_sec INTEGER, timeout_sec INTEGER, created_at INTEGER DEFAULT 0)"
    )
    db.execute(
        "CREATE TABLE uptime_results("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, check_id TEXT, ts INTEGER, "
        "ok INTEGER, latency_ms REAL)"
    )
    db.commit()
    return db


# ── samples ───────────────────────────────────────────────────────────────────

class TestSamplesRepo(unittest.TestCase):
    def setUp(self):
        self.db = _samples_db()

    def tearDown(self):
        self.db.close()

    def test_latest_n_empty(self):
        self.assertEqual(samples.latest_n(10, conn=self.db), [])

    def test_insert_and_latest_n(self):
        samples.insert(1000, 50.0, 4000, 8000, 200.0, 60.0, 10, conn=self.db)
        rows = samples.latest_n(10, conn=self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 1000)

    def test_latest_n_limit(self):
        for i in range(5):
            samples.insert(i * 1000, float(i), 0, 0, 0, 0, 10, conn=self.db)
        rows = samples.latest_n(3, conn=self.db)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][0], 4000)  # DESC order

    def test_since_returns_range(self):
        samples.insert(1000, 50.0, 4000, 8000, 200.0, 60.0, 10, conn=self.db)
        samples.insert(2000, 60.0, 5000, 8000, 250.0, 65.0, 10, conn=self.db)
        rows = samples.since(1500, conn=self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 2000)

    def test_since_inclusive(self):
        samples.insert(1000, 50.0, 0, 0, 0, 0, 10, conn=self.db)
        rows = samples.since(1000, conn=self.db)
        self.assertEqual(len(rows), 1)

    def test_since_empty_when_all_before(self):
        samples.insert(500, 50.0, 0, 0, 0, 0, 10, conn=self.db)
        rows = samples.since(1000, conn=self.db)
        self.assertEqual(rows, [])


# ── settings ──────────────────────────────────────────────────────────────────

class TestSettingsRepo(unittest.TestCase):
    def setUp(self):
        self.db = _settings_db()

    def tearDown(self):
        self.db.close()

    def test_get_missing_returns_default(self):
        self.assertIsNone(settings.get("missing", conn=self.db))
        self.assertEqual(settings.get("missing", "fallback", conn=self.db), "fallback")

    def test_set_and_get(self):
        settings.set("foo", "bar", conn=self.db)
        self.assertEqual(settings.get("foo", conn=self.db), "bar")

    def test_set_upsert(self):
        settings.set("foo", "bar", conn=self.db)
        settings.set("foo", "baz", conn=self.db)
        self.assertEqual(settings.get("foo", conn=self.db), "baz")

    def test_get_all_empty(self):
        self.assertEqual(settings.get_all(conn=self.db), [])

    def test_get_all_returns_all(self):
        settings.set("a", "1", conn=self.db)
        settings.set("b", "2", conn=self.db)
        rows = settings.get_all(conn=self.db)
        self.assertEqual(len(rows), 2)
        keys = {r[0] for r in rows}
        self.assertIn("a", keys)
        self.assertIn("b", keys)


# ── uptime ────────────────────────────────────────────────────────────────────

class TestUptimeRepo(unittest.TestCase):
    def setUp(self):
        self.db = _uptime_db()

    def tearDown(self):
        self.db.close()

    def test_list_checks_empty(self):
        self.assertEqual(uptime.list_checks(conn=self.db), [])

    def test_insert_and_list_checks(self):
        uptime.insert_check("c1", "My Check", "http", "https://example.com", 60, 10, conn=self.db)
        rows = uptime.list_checks(conn=self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "c1")

    def test_get_check_found(self):
        uptime.insert_check("c1", "Label", "http", "https://x.com", 30, 5, conn=self.db)
        row = uptime.get_check("c1", conn=self.db)
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "c1")

    def test_get_check_not_found(self):
        self.assertIsNone(uptime.get_check("nope", conn=self.db))

    def test_delete_check(self):
        uptime.insert_check("c1", "L", "http", "http://x", 60, 10, conn=self.db)
        rowcount = uptime.delete_check("c1", conn=self.db)
        self.assertEqual(rowcount, 1)
        self.assertIsNone(uptime.get_check("c1", conn=self.db))

    def test_delete_check_missing(self):
        self.assertEqual(uptime.delete_check("nope", conn=self.db), 0)

    def test_insert_result_and_query(self):
        uptime.insert_check("c1", "L", "http", "http://x", 60, 10, conn=self.db)
        uptime.insert_result("c1", 1000, 1, 55.0, conn=self.db)
        uptime.insert_result("c1", 2000, 0, 120.0, conn=self.db)
        rows = uptime.results_since("c1", 1500, conn=self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "c1")  # check_id (col 1, after id)
        self.assertEqual(rows[0][2], 2000)  # ts


# ── costs ─────────────────────────────────────────────────────────────────────

class TestCostsRepo(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE samples_1h(ts INTEGER PRIMARY KEY, util REAL, "
            "mem_used REAL, mem_total REAL, power REAL, temp REAL, cnt INTEGER, cpu_power REAL, dram_power REAL)"
        )
        self.db.execute(
            "CREATE TABLE samples(ts INTEGER PRIMARY KEY, util REAL, "
            "mem_used REAL, mem_total REAL, power REAL, temp REAL)"
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _insert_1h(self, ts, power):
        self.db.execute(
            "INSERT INTO samples_1h(ts,power) VALUES(?,?)", (ts, power)
        )
        self.db.commit()

    def test_power_since_empty(self):
        self.assertEqual(costs.power_since(0, conn=self.db), [])

    def test_power_since_filters(self):
        self._insert_1h(500, 100.0)
        self._insert_1h(1500, 200.0)
        rows = costs.power_since(1000, conn=self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 1500)

    def test_power_since_excludes_null(self):
        self.db.execute("INSERT INTO samples_1h(ts,power) VALUES(2000, NULL)")
        self.db.commit()
        rows = costs.power_since(0, conn=self.db)
        self.assertEqual(rows, [])

    def test_heatmap_since(self):
        self._insert_1h(1000, 150.0)
        self._insert_1h(2000, 250.0)
        rows = costs.heatmap_since(1500, conn=self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 2000)
        self.assertAlmostEqual(rows[0][1], 250.0)

    def test_bucketed_power(self):
        for i in range(4):
            self.db.execute(
                "INSERT INTO samples(ts,power) VALUES(?,?)", (i * 100, 50.0)
            )
        self.db.commit()
        rows = costs.bucketed_power(0, 200, table="samples", conn=self.db)
        # ts 0,100 -> bucket 0; ts 200,300 -> bucket 200
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
