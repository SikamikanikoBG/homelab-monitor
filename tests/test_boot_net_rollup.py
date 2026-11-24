"""Regression test (#378): the boot-time rollup must not scan all of net_samples.

net_samples holds one row per interface per sample, so on a large database the
old full-table GROUP BY took over a minute on every boot. The backfill now reads
only the hours missing from net_samples_1h, and must still fill those.
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app

HOUR = 3600
BASE = 1_700_000_000 // HOUR * HOUR


class TestNetRollupBackfill(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        app._apply_schema_migrations(self.db)

    def tearDown(self):
        self.db.close()

    def _raw(self, hour, values):
        self.db.executemany(
            "INSERT INTO net_samples(ts,iface,bytes_in,bytes_out) VALUES(?,?,?,?)",
            [(BASE + hour * HOUR + i * 10, "eth0", v, v * 2) for i, v in enumerate(values)])
        self.db.commit()

    def _rollup(self):
        return self.db.execute(
            "SELECT (ts-?)/3600, bytes_in, bytes_out, cnt FROM net_samples_1h ORDER BY ts",
            (BASE,)).fetchall()

    def test_raw_only_database_is_rolled_up(self):
        self._raw(0, [100, 200])
        self._raw(1, [300])
        self._raw(5, [10, 20, 30])          # hours 2-4 have no samples at all
        app._backfill_rollups(self.db)
        self.assertEqual(self._rollup(), [(0, 150.0, 300.0, 2), (1, 300.0, 600.0, 1),
                                          (5, 20.0, 40.0, 3)])

    def test_gap_is_filled_and_existing_hours_are_untouched(self):
        for h in range(3):
            self._raw(h, [100, 200])
        for h in (0, 2):                    # hour 1 is missing from the rollup
            self.db.execute("INSERT INTO net_samples_1h VALUES(?,?,?,?)",
                            (BASE + h * HOUR, 7, 8, 9))
        app._backfill_rollups(self.db)
        self.assertEqual(self._rollup(), [(0, 7.0, 8.0, 9), (1, 150.0, 300.0, 2),
                                          (2, 7.0, 8.0, 9)])

    def test_empty_table_is_fine(self):
        app._backfill_rollups(self.db)
        self.assertEqual(self._rollup(), [])

    def test_a_filled_rollup_is_not_paid_for_by_scanning_the_raw_rows(self):
        for h in range(20):
            self._raw(h, range(360))        # 7200 raw rows, every hour present
        app._backfill_rollups(self.db)
        ticks = []
        self.db.set_progress_handler(lambda: ticks.append(1) or 0, 100)
        app._backfill_rollups(self.db)
        self.db.set_progress_handler(None, 0)
        # One VM step per raw row would already be 72 ticks; a full scan with the
        # aggregates is several times that.
        self.assertLess(len(ticks), 40)


if __name__ == "__main__":
    unittest.main()
