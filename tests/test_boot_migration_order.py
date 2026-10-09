"""Regression test: boot-time migration order must not leave freshly-rolled-up
samples_1h rows with NULL wsec.

Bug: _backfill_interval_columns ran BEFORE _backfill_rollups at both boot call
sites (module-level boot and reopen_db()). _backfill_rollups's
`INSERT OR IGNORE INTO samples_1h(...)` does not set wsec/cpu_wsec/dram_wsec,
so any samples_1h row it inserts fresh (covering an hour that has raw
`samples` rows but no samples_1h row yet -- e.g. a restored raw-only DB, or
any DB with rollup coverage gaps) ends up with wsec NULL. Every cost read
path treats NULL wsec as 0, so that hour's energy/cost silently reads as
zero until the next restart.

Fix: run _backfill_rollups first, then _backfill_interval_columns -- its
`WHERE wsec IS NULL` UPDATE then catches and backfills whatever
_backfill_rollups just inserted.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _clean():
    with app.LOCK:
        for tbl in ("samples", "samples_1h"):
            app.DB.execute(f"DELETE FROM {tbl}")
        app.DB.commit()


class TestBootMigrationOrderBackfillsFreshRollups(unittest.TestCase):
    """Seed raw `samples` rows for an hour with NO corresponding samples_1h
    row, then run the boot-migration path exactly as app.py does (schema
    migrations already applied at import time), and assert the resulting
    samples_1h row's wsec is not NULL and reflects the backfilled energy."""

    INTERVAL = 10
    WATTS = 200

    def setUp(self):
        _clean()
        self.orig_interval = app.INTERVAL
        app.INTERVAL = self.INTERVAL

    def tearDown(self):
        app.INTERVAL = self.orig_interval
        _clean()

    def test_fresh_rollup_row_gets_wsec_backfilled(self):
        anchor = (int(time.time()) // 3600) * 3600
        n = 3600 // self.INTERVAL
        with app.LOCK:
            for i in range(n):
                ts = anchor + i * self.INTERVAL
                app.DB.execute(
                    "INSERT INTO samples(ts,util,mem_used,mem_total,power,temp,interval_sec) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (ts, 50, 1000, 2000, self.WATTS, 40, self.INTERVAL),
                )
            app.DB.commit()
            # Confirm the precondition: raw samples exist, no samples_1h row yet.
            row = app.DB.execute(
                "SELECT wsec FROM samples_1h WHERE ts=?", (anchor,)
            ).fetchone()
            self.assertIsNone(row)

        # Exercise the boot-migration path in the FIXED order: rollups first,
        # then interval-column backfill (mirrors app.py's boot call sites).
        app._backfill_rollups(app.DB)
        app._backfill_interval_columns(app.DB, app.INTERVAL)

        with app.LOCK:
            row = app.DB.execute(
                "SELECT wsec, cnt FROM samples_1h WHERE ts=?", (anchor,)
            ).fetchone()
        self.assertIsNotNone(row, "rollup row should have been inserted")
        wsec, cnt = row
        self.assertIsNotNone(wsec, "wsec must not be NULL after boot migration")
        expected = self.WATTS * cnt * self.INTERVAL
        self.assertAlmostEqual(wsec, expected, places=3)
        self.assertGreater(wsec, 0)


if __name__ == "__main__":
    unittest.main()
