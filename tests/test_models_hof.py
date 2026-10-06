"""AI Models Hall of Fame residency accounting.

`query_model_hof` bills every sample the interval it actually covered
(`interval_sec`) instead of multiplying the row count by whatever the current
global interval happens to be. These tests pin that: a leaderboard must not
change when the sampling interval changes, a sampling gap must not be read as
continuous residency, and rows written before the column existed must still
contribute.
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.db.repos import system as system_repo


def _db(with_interval=True):
    db = sqlite3.connect(":memory:")
    cols = "ts INTEGER, service TEXT, model TEXT, vram REAL, ram REAL"
    if with_interval:
        cols += ", interval_sec INTEGER"
    db.execute(f"CREATE TABLE models({cols})")
    db.commit()
    return db


class TestModelHofResidency(unittest.TestCase):

    def test_interval_change_does_not_rewrite_history(self):
        db = _db()
        # Ten samples taken while the collector ran at 60s...
        db.executemany("INSERT INTO models VALUES(?,?,?,?,?,?)",
                       [(t, "ollama", "big:30b", 1000.0, 0.0, 60)
                        for t in range(0, 600, 60)])
        # ...then ten more after an operator shortened the interval to 30s.
        db.executemany("INSERT INTO models VALUES(?,?,?,?,?,?)",
                       [(t, "ollama", "big:30b", 1000.0, 0.0, 30)
                        for t in range(600, 900, 30)])
        db.commit()
        rows = system_repo.query_model_hof(0, 30, conn=db)
        # 10*60 + 10*30 = 900s. The old COUNT(DISTINCT ts)*30 said 600s.
        self.assertEqual(rows[0][2], 900)

    def test_rows_predating_the_column_fall_back_to_the_caller_interval(self):
        # The ALTER migration always adds the column, so pre-existing rows carry
        # NULL rather than the column being absent.
        db = _db()
        db.executemany("INSERT INTO models VALUES(?,?,?,?,?,?)",
                       [(t, "ollama", "legacy:7b", 500.0, 0.0, None) for t in (0, 30, 60)])
        db.commit()
        rows = system_repo.query_model_hof(0, 30, conn=db)
        self.assertEqual(rows[0][2], 90)

    def test_gap_does_not_stretch_residency(self):
        db = _db()
        # Two isolated samples an hour apart are 30s each, not a 3600s residency.
        db.executemany("INSERT INTO models VALUES(?,?,?,?,?,?)",
                       [(t, "ollama", "spiky:8b", 100.0, 0.0, 30) for t in (0, 3600)])
        db.commit()
        rows = system_repo.query_model_hof(0, 30, conn=db)
        self.assertEqual(rows[0][2], 60)

    def test_duplicate_rows_for_one_tick_are_billed_once(self):
        # `models` has no unique constraint and the collector inserts blindly, so
        # a model discovered twice inside a single poll must not double its
        # residency — the SUM would otherwise bill both rows.
        db = _db()
        db.executemany("INSERT INTO models VALUES(?,?,?,?,?,?)",
                       [(0, "ollama", "dup:7b", 100.0, 0.0, 30),
                        (0, "ollama", "dup:7b", 100.0, 0.0, 30),
                        (30, "ollama", "dup:7b", 100.0, 0.0, 30)])
        db.commit()
        rows = system_repo.query_model_hof(0, 30, conn=db)
        self.assertEqual(rows[0][2], 60)

    def test_backfill_interval_columns_covers_models(self):
        # Pre-migration `models` rows must be stamped once at migration time;
        # otherwise they keep falling back to whatever interval happens to be
        # current when the leaderboard is queried.
        from backend.db.repos import schema as schema_repo
        db = _db()
        for ddl in (
            "CREATE TABLE samples(ts INTEGER, interval_sec INTEGER)",
            "CREATE TABLE host_samples(ts INTEGER, interval_sec INTEGER)",
            "CREATE TABLE power_proc(ts INTEGER, interval_sec INTEGER)",
            "CREATE TABLE samples_1h(cnt INTEGER, power REAL, wsec REAL,"
            " cpu_power REAL, cpu_wsec REAL, dram_power REAL, dram_wsec REAL)",
            "CREATE TABLE host_samples_1h(cnt INTEGER, gpu_power REAL, cpu_power REAL,"
            " dram_power REAL, gpu_wsec REAL, cpu_wsec REAL, dram_wsec REAL)",
        ):
            db.execute(ddl)
        db.executemany("INSERT INTO models VALUES(?,?,?,?,?,?)",
                       [(t, "ollama", "legacy:7b", 500.0, 0.0, None) for t in (0, 30)])
        db.commit()
        schema_repo.backfill_interval_columns(db, 45)
        # 2 * 45 (backfilled), not 2 * 30 (the query-time interval).
        self.assertEqual(system_repo.query_model_hof(0, 30, conn=db)[0][2], 90)

    def test_vram_null_rows_are_excluded(self):
        db = _db()
        db.executemany("INSERT INTO models VALUES(?,?,?,?,?,?)",
                       [(0, "ollama", "cpu-only", None, 0.0, 30),
                        (0, "ollama", "gpu", 100.0, 0.0, 30)])
        db.commit()
        rows = system_repo.query_model_hof(0, 30, conn=db)
        self.assertEqual([r[1] for r in rows], ["gpu"])

    def test_peak_and_avg_vram_still_reported(self):
        db = _db()
        db.executemany("INSERT INTO models VALUES(?,?,?,?,?,?)",
                       [(0, "ollama", "m", 100.0, 0.0, 30),
                        (30, "ollama", "m", 300.0, 0.0, 30)])
        db.commit()
        rows = system_repo.query_model_hof(0, 30, conn=db)
        _, _, loaded, peak, avg, first_seen, last_seen = rows[0]
        self.assertEqual((loaded, peak, avg, first_seen, last_seen), (60, 300.0, 200.0, 0, 30))


if __name__ == "__main__":
    unittest.main()
