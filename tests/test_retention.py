"""Retention purge tests (bug #9: run_metrics/runs were never purged) plus a
schema-application check that the write-only samples_1m/net_samples_1m tables
are actually gone (bug #5).

Calls backend.collectors.purge_finished_runs directly — the same function
sample_once() calls — so these assertions exercise the shipped code rather
than a copy of its SQL that could drift out of sync with it.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
from backend.collectors import purge_finished_runs as _run_purge


def _insert_run(conn, run_id, status, started_at, ended_at, heartbeat_at=None):
    conn.execute(
        "INSERT INTO runs(id,name,source,status,started_at,ended_at,heartbeat_at,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (run_id, run_id, "test", status, started_at, ended_at, heartbeat_at, started_at))


def _insert_metric(conn, run_id, ts, key="loss", value=1.0):
    conn.execute("INSERT INTO run_metrics(run_id,ts,step,key,value) VALUES(?,?,?,?,?)",
                 (run_id, ts, 0, key, value))


class TestRunRetentionPurge(unittest.TestCase):
    def tearDown(self):
        with app.LOCK:
            app.DB.execute("DELETE FROM runs")
            app.DB.execute("DELETE FROM run_metrics")
            app.DB.commit()

    def test_old_run_metric_is_deleted(self):
        now = int(time.time())
        retention = 30 * 86400
        cutoff = now - retention
        with app.LOCK:
            _insert_run(app.DB, "r-old", "finished", cutoff - 3600, cutoff - 1800)
            _insert_metric(app.DB, "r-old", cutoff - 1800)
            _run_purge(app.DB, cutoff)
            app.DB.commit()
            rows = app.DB.execute("SELECT 1 FROM run_metrics WHERE run_id=?", ("r-old",)).fetchall()
        self.assertEqual(rows, [])

    def test_recent_run_metric_survives(self):
        now = int(time.time())
        retention = 30 * 86400
        cutoff = now - retention
        with app.LOCK:
            _insert_run(app.DB, "r-recent", "finished", now - 3600, now - 1800)
            _insert_metric(app.DB, "r-recent", now - 1800)
            _run_purge(app.DB, cutoff)
            app.DB.commit()
            rows = app.DB.execute("SELECT 1 FROM run_metrics WHERE run_id=?", ("r-recent",)).fetchall()
        self.assertEqual(len(rows), 1)

    def test_finished_run_older_than_retention_purged_with_its_metrics(self):
        now = int(time.time())
        retention = 30 * 86400
        cutoff = now - retention
        with app.LOCK:
            _insert_run(app.DB, "r-finished-old", "finished", cutoff - 7200, cutoff - 3600)
            _insert_metric(app.DB, "r-finished-old", cutoff - 3600)
            _insert_metric(app.DB, "r-finished-old", cutoff - 5000)
            _run_purge(app.DB, cutoff)
            app.DB.commit()
            run_rows = app.DB.execute("SELECT 1 FROM runs WHERE id=?", ("r-finished-old",)).fetchall()
            metric_rows = app.DB.execute("SELECT 1 FROM run_metrics WHERE run_id=?",
                                         ("r-finished-old",)).fetchall()
        self.assertEqual(run_rows, [])
        self.assertEqual(metric_rows, [])   # no orphaned metrics left behind

    def test_running_status_with_recent_heartbeat_survives_ancient_started_at(self):
        now = int(time.time())
        retention = 30 * 86400
        cutoff = now - retention
        with app.LOCK:
            # started_at is ancient (well past retention) but status is still
            # 'running' with a fresh heartbeat -> must not be purged.
            _insert_run(app.DB, "r-live", "running", cutoff - 999999, None, heartbeat_at=now)
            _insert_metric(app.DB, "r-live", cutoff - 999999)
            _run_purge(app.DB, cutoff)
            app.DB.commit()
            run_rows = app.DB.execute("SELECT 1 FROM runs WHERE id=?", ("r-live",)).fetchall()
        self.assertEqual(len(run_rows), 1)


class TestSamples1mTablesRemoved(unittest.TestCase):
    """bug #5: samples_1m / net_samples_1m are write-only and never read;
    schema application must not (re)create them, and must drop them on
    existing databases that still have them from before the fix."""

    def test_schema_application_leaves_no_1m_tables(self):
        with app.LOCK:
            # Simulate a pre-fix database that still has the orphaned tables,
            # then re-apply migrations the way boot does.
            app.DB.executescript(
                "CREATE TABLE IF NOT EXISTS samples_1m(ts INTEGER PRIMARY KEY);"
                "CREATE TABLE IF NOT EXISTS net_samples_1m(ts INTEGER PRIMARY KEY);"
                "CREATE INDEX IF NOT EXISTS idx_samples_1m_ts ON samples_1m(ts);"
                "CREATE INDEX IF NOT EXISTS idx_net_samples_1m_ts ON net_samples_1m(ts);"
            )
            app.DB.commit()
            app._apply_schema_migrations(app.DB)
            names = {r[0] for r in app.DB.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('samples_1m','net_samples_1m')").fetchall()}
        self.assertEqual(names, set())


if __name__ == "__main__":
    unittest.main()
