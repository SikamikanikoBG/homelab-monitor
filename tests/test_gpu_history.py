"""Per-card GPU history storage — the table the GPU cockpit reads for every host.

The point of this storage is that the hub and a remote are the SAME shape: the
hub writes itself as host='local', so one reader serves both and the dashboard's
old charts-here / snapshot-there fork has nothing left to stand on. These tests
pin the parts that are easy to get subtly wrong: max-vs-avg in the rollup,
absent-vs-zero for unreported sensors, and host isolation.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_db():
    """A migrated, empty database — schema exactly as a new install gets it."""
    import app
    path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(path)
    app._apply_schema_migrations(conn)
    conn.commit()
    return conn


def _card(idx=0, **kw):
    g = {"idx": idx, "util": 50, "mem_used": 12000, "mem_total": 24576,
         "power": 200, "temp": 70}
    g.update(kw)
    return g


class TestRecord(unittest.TestCase):
    def setUp(self):
        self.conn = _fresh_db()
        from backend.db.repos import gpu_samples
        self.repo = gpu_samples

    def test_hub_and_remote_share_one_table(self):
        self.repo.record(self.conn, 1000, "local", [_card(0)])
        self.repo.record(self.conn, 1000, "vader", [_card(0), _card(1), _card(2)])
        self.assertEqual(self.repo.cards_for("local", conn=self.conn), [0])
        self.assertEqual(self.repo.cards_for("vader", conn=self.conn), [0, 1, 2])

    def test_one_hosts_cards_never_leak_into_another(self):
        self.repo.record(self.conn, 1000, "local", [_card(0, temp=40)])
        self.repo.record(self.conn, 1000, "vader", [_card(0, temp=86)])
        rows = self.repo.series("vader", 0, 3600, conn=self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][6], 86)          # avg temp, vader's card only

    def test_unreported_fan_stays_null_not_zero(self):
        # A passively cooled card reports no fan. Storing 0 would be a claim the
        # fan is stopped, which is what the fan-stall alert fires on.
        self.repo.record(self.conn, 1000, "vader", [_card(0)])          # no fan key
        got = self.conn.execute("SELECT fan FROM gpu_samples").fetchone()[0]
        self.assertIsNone(got)

    def test_unreported_memory_temp_stays_null_not_zero(self):
        # Only some cards have a memory-junction sensor. Storing 0 for the rest
        # would put a 0 °C VRAM reading next to an 80 °C core.
        self.repo.record(self.conn, 1000, "vader", [_card(0)])         # no temp_mem key
        got = self.conn.execute("SELECT temp_mem FROM gpu_samples").fetchone()[0]
        self.assertIsNone(got)

    def test_zero_fan_is_stored_as_zero(self):
        # The other side of the same coin: a card that reports 0% must store 0,
        # so a genuinely stalled fan is distinguishable from an absent sensor.
        self.repo.record(self.conn, 1000, "vader", [_card(0, fan=0)])
        got = self.conn.execute("SELECT fan FROM gpu_samples").fetchone()[0]
        self.assertEqual(got, 0)

    def test_throttle_mask_is_stored_not_the_reason_labels(self):
        self.repo.record(self.conn, 1000, "vader",
                         [_card(0, throttle_mask=0x40, throttle=["HW thermal"], throttled=True)])
        got = self.conn.execute("SELECT throttle FROM gpu_samples").fetchone()[0]
        self.assertEqual(got, 0x40)


class TestRollup(unittest.TestCase):
    def setUp(self):
        self.conn = _fresh_db()
        from backend.db.repos import gpu_samples
        self.repo = gpu_samples

    def test_temp_keeps_both_average_and_peak(self):
        # An hour that averaged 71 C while peaking at 87 C is an hour with a
        # thermal problem. Averaging alone would hide exactly that.
        for ts, temp in ((0, 60), (10, 66), (20, 87)):
            self.repo.record(self.conn, ts, "vader", [_card(0, temp=temp)])
        avg, mx = self.conn.execute(
            "SELECT temp, temp_max FROM gpu_samples_1h WHERE host='vader'").fetchone()
        self.assertAlmostEqual(avg, 71.0, places=1)
        self.assertEqual(mx, 87)

    def test_fan_keeps_both_average_and_peak(self):
        for ts, fan in ((0, 40), (10, 60), (20, 100)):
            self.repo.record(self.conn, ts, "vader", [_card(0, fan=fan)])
        avg, mx = self.conn.execute(
            "SELECT fan, fan_max FROM gpu_samples_1h WHERE host='vader'").fetchone()
        self.assertAlmostEqual(avg, 66.67, places=1)
        self.assertEqual(mx, 100)

    def test_memory_temp_keeps_both_average_and_peak(self):
        # The peak is the whole point for VRAM: memory-junction heat is what
        # trips the sticky slowdown, and an hourly average smooths it away.
        for ts, tm in ((0, 82), (10, 88), (20, 104)):
            self.repo.record(self.conn, ts, "vader", [_card(0, temp_mem=tm)])
        avg, mx = self.conn.execute(
            "SELECT temp_mem, temp_mem_max FROM gpu_samples_1h WHERE host='vader'").fetchone()
        self.assertAlmostEqual(avg, 91.33, places=1)
        self.assertEqual(mx, 104)

    def test_memory_temp_average_ignores_polls_without_the_sensor(self):
        # Same rule as the fan: divide by the polls that actually reported, or a
        # card whose sensor answers intermittently reads far cooler than it is.
        for ts in (0, 10, 20):
            self.repo.record(self.conn, ts, "vader", [_card(0)])                # no sensor
        for ts in (30, 40):
            self.repo.record(self.conn, ts, "vader", [_card(0, temp_mem=90)])
        tm, tm_cnt, cnt = self.conn.execute(
            "SELECT temp_mem, temp_mem_cnt, cnt FROM gpu_samples_1h WHERE host='vader'").fetchone()
        self.assertEqual((cnt, tm_cnt), (5, 2))
        self.assertAlmostEqual(tm, 90.0, places=6)

    def test_memory_temp_max_stays_null_while_nothing_reports_it(self):
        self.repo.record(self.conn, 0,  "vader", [_card(0)])
        self.repo.record(self.conn, 10, "vader", [_card(0)])
        tm, tm_max = self.conn.execute(
            "SELECT temp_mem, temp_mem_max FROM gpu_samples_1h WHERE host='vader'").fetchone()
        self.assertIsNone(tm)
        self.assertIsNone(tm_max)

    def test_throttled_seconds_count_only_the_actionable_kind(self):
        # Three things that are NOT a thermal problem must not accumulate:
        #   0x04 SW_POWER_CAP — a card at a deliberately lowered power limit sits
        #        here by design; counting it would show a healthy box as
        #        permanently throttled and train the user to ignore the light.
        #   0x01 GPU idle     — normal operation.
        #   0x00 nothing.
        # Only the 0x40 HW-thermal sample counts, so one interval = 10 s.
        self.repo.record(self.conn, 0,  "vader", [_card(0, throttle_mask=0x40)], interval=10)
        self.repo.record(self.conn, 10, "vader", [_card(0, throttle_mask=0x04)], interval=10)
        self.repo.record(self.conn, 20, "vader", [_card(0, throttle_mask=0x01)], interval=10)
        self.repo.record(self.conn, 30, "vader", [_card(0, throttle_mask=0)],    interval=10)
        secs = self.conn.execute(
            "SELECT throttle_secs FROM gpu_samples_1h WHERE host='vader'").fetchone()[0]
        self.assertEqual(secs, 10)

    def test_sustained_thermal_throttle_accumulates_across_samples(self):
        for ts in (0, 10, 20):
            self.repo.record(self.conn, ts, "vader", [_card(0, throttle_mask=0x20)], interval=10)
        secs = self.conn.execute(
            "SELECT throttle_secs FROM gpu_samples_1h WHERE host='vader'").fetchone()[0]
        self.assertEqual(secs, 30)

    def test_a_max_stays_null_while_nothing_reports_it(self):
        # The MAX columns must not be poisoned to a sentinel. Two consecutive
        # polls with no fan used to leave fan_max = -1, i.e. a fabricated
        # reading — exactly what this feature refuses to do everywhere else.
        self.repo.record(self.conn, 0,  "vader", [_card(0)])     # no fan
        self.repo.record(self.conn, 10, "vader", [_card(0)])     # no fan
        fan, fan_max = self.conn.execute(
            "SELECT fan, fan_max FROM gpu_samples_1h WHERE host='vader'").fetchone()
        self.assertIsNone(fan)
        self.assertIsNone(fan_max)

    def test_fan_average_ignores_polls_that_reported_no_fan(self):
        # Dividing by the total poll count drags an intermittently-reported fan
        # toward zero — and near-zero is what the fan-stall alert fires on.
        # Three silent polls then two at 80% must average 80, not 32.
        for ts in (0, 10, 20):
            self.repo.record(self.conn, ts, "vader", [_card(0)])          # no fan
        for ts in (30, 40):
            self.repo.record(self.conn, ts, "vader", [_card(0, fan=80)])
        fan, fan_cnt, cnt = self.conn.execute(
            "SELECT fan, fan_cnt, cnt FROM gpu_samples_1h WHERE host='vader'").fetchone()
        self.assertEqual(cnt, 5)
        self.assertEqual(fan_cnt, 2)
        self.assertAlmostEqual(fan, 80.0, places=6)

    def test_each_card_rolls_up_separately(self):
        self.repo.record(self.conn, 0, "vader",
                         [_card(0, temp=80), _card(1, temp=86), _card(2, temp=64)])
        rows = dict((r[0], r[1]) for r in self.conn.execute(
            "SELECT idx, temp_max FROM gpu_samples_1h WHERE host='vader'"))
        self.assertEqual(rows, {0: 80, 1: 86, 2: 64})

    def test_hours_bucket_independently(self):
        self.repo.record(self.conn, 0,    "vader", [_card(0, temp=60)])
        self.repo.record(self.conn, 3700, "vader", [_card(0, temp=90)])
        rows = self.conn.execute(
            "SELECT ts, temp_max FROM gpu_samples_1h WHERE host='vader' ORDER BY ts").fetchall()
        self.assertEqual(rows, [(0, 60), (3600, 90)])


class TestRollupIsActuallyRead(unittest.TestCase):
    """The hourly rollup has to be READ, not just written.

    Raw rows are retention-purged, so a 30-day request answered from the raw
    table alone would silently show the last two days and call it a month.
    """

    def setUp(self):
        self.conn = _fresh_db()
        from backend.db.repos import gpu_samples
        self.repo = gpu_samples

    def _rollup_only(self, host, hours, temp=70, temp_max=None, throttle_secs=0,
                     temp_mem=None, temp_mem_max=None):
        """Write rollup rows with NO raw rows — the state after a purge."""
        for h in range(hours):
            self.conn.execute(
                "INSERT INTO gpu_samples_1h(ts,host,idx,util,mem_used,mem_total,power,"
                "temp,temp_max,fan,fan_max,temp_mem,temp_mem_max,temp_mem_cnt,"
                "throttle_secs,cnt) "
                "VALUES(?,?,0,50,12000,24576,200,?,?,40,60,?,?,?,?,360)",
                (h * 3600, host, temp, temp_max if temp_max is not None else temp,
                 temp_mem, temp_mem_max if temp_mem_max is not None else temp_mem,
                 360 if temp_mem is not None else 0, throttle_secs))
        self.conn.commit()

    def test_a_long_range_reads_the_rollup_when_raw_is_gone(self):
        self._rollup_only("vader", 48)
        rows = self.repo.series("vader", 0, 3600, conn=self.conn)
        self.assertEqual(len(rows), 48)

    def test_a_short_range_still_reads_raw_for_full_resolution(self):
        self.repo.record(self.conn, 1000, "vader", [_card(0)])
        rows = self.repo.series("vader", 0, 60, conn=self.conn)
        self.assertEqual(len(rows), 1)

    def test_peaks_survive_the_downsample(self):
        # The whole reason the rollup keeps MAX columns: an hour that peaked at
        # 91 C must not average away into a comfortable 70.
        self._rollup_only("vader", 4, temp=70, temp_max=91)
        rows = self.repo.series("vader", 0, 3600, conn=self.conn)
        self.assertEqual(max(r[7] for r in rows), 91)

    def test_memory_temp_survives_the_downsample_as_an_hourly_peak(self):
        # Past the raw retention window the memory row must still answer, and it
        # must answer with the hour's PEAK — the reason the rollup keeps a MAX.
        self._rollup_only("vader", 4, temp_mem=84, temp_mem_max=103)
        rows = self.repo.series("vader", 0, 3600, conn=self.conn)
        self.assertEqual(max(r[13] for r in rows), 103)
        self.assertEqual(max(r[12] for r in rows), 84)
        h = self.repo.health("vader", 0, conn=self.conn)
        self.assertEqual(h[0][8], 103)

    def test_memory_temp_is_absent_in_the_rollup_when_no_card_reported_it(self):
        self._rollup_only("vader", 4)                     # no memory sensor
        rows = self.repo.series("vader", 0, 3600, conn=self.conn)
        self.assertTrue(all(r[12] is None and r[13] is None for r in rows))
        self.assertIsNone(self.repo.health("vader", 0, conn=self.conn)[0][8])

    def test_cards_survive_in_the_rollup_after_raw_is_purged(self):
        self._rollup_only("vader", 3)
        self.assertEqual(self.repo.cards_for("vader", conn=self.conn), [0])

    def test_range_all_spans_the_rollup_not_just_raw(self):
        self._rollup_only("vader", 3)                      # starts at ts=0
        self.repo.record(self.conn, 100000, "vader", [_card(0)])
        self.assertEqual(self.repo.min_ts("vader", conn=self.conn), 0)

    def test_health_uses_the_rollup_and_keeps_throttled_seconds_exact(self):
        # throttle_secs is stored AS seconds, so it stays exact across the
        # downsample even though "hot" becomes hour-granular.
        self._rollup_only("vader", 3, temp=70, temp_max=88, throttle_secs=120)
        h = self.repo.health("vader", 0, hot_c=84, interval=10, conn=self.conn)[0]
        self.assertEqual(h[5], 360)          # 3 hours x 120 s, exact
        self.assertEqual(h[3], 88)           # peak temp preserved
        self.assertEqual(h[6], 3 * 3600)     # hot hours, hour-granular


class TestHealthAndSpans(unittest.TestCase):
    def setUp(self):
        self.conn = _fresh_db()
        from backend.db.repos import gpu_samples
        self.repo = gpu_samples

    def test_health_reports_hot_and_throttled_seconds_per_card(self):
        for ts in range(0, 50, 10):
            self.repo.record(self.conn, ts, "vader", [
                _card(0, temp=70),
                _card(1, temp=86, throttle_mask=0x40),
            ])
        by_idx = {r[0]: r for r in self.repo.health("vader", 0, interval=10, conn=self.conn)}
        self.assertEqual(by_idx[0][3], 70)         # peak temp
        self.assertEqual(by_idx[0][5], 0)          # throttled seconds
        self.assertEqual(by_idx[0][6], 0)          # seconds at/above 84 C
        self.assertEqual(by_idx[1][3], 86)
        self.assertEqual(by_idx[1][5], 50)         # 5 samples x 10s
        self.assertEqual(by_idx[1][6], 50)

    def test_power_capped_counts_only_against_a_known_limit(self):
        # A card that doesn't report a power limit must not be counted as capped
        # — the denominator is missing, not zero. 1 of 2 samples => 50%.
        self.repo.record(self.conn, 0, "vader", [_card(0, power=280)])
        self.repo.record(self.conn, 10, "vader", [_card(0, power=280, power_limit=280)])
        capped_pct = self.repo.health("vader", 0, interval=10, conn=self.conn)[0][7]
        self.assertEqual(capped_pct, 50.0)

    def test_throttle_spans_return_only_thermal_and_power_samples(self):
        self.repo.record(self.conn, 0,  "vader", [_card(0, throttle_mask=0x40)])
        self.repo.record(self.conn, 10, "vader", [_card(0, throttle_mask=0x01)])
        self.repo.record(self.conn, 20, "vader", [_card(0, throttle_mask=0)])
        spans = self.repo.throttle_spans("vader", 0, conn=self.conn)
        self.assertEqual([s[0] for s in spans], [0])


class TestVramAttribution(unittest.TestCase):
    def setUp(self):
        self.conn = _fresh_db()
        from backend.db.repos import gpu_samples
        self.repo = gpu_samples

    def test_per_service_vram_is_scoped_to_its_host(self):
        # Every box in a fleet runs a service called "ollama"; the series for one
        # host must not pick up another host's rows.
        self.conn.executemany("INSERT INTO proc(ts,service,mem,host) VALUES(?,?,?,?)", [
            (0, "ollama", 60000, "vader"),
            (0, "ollama", 8000,  "local"),
        ])
        rows = self.repo.vram_by_service("vader", 0, 3600, conn=self.conn)
        self.assertEqual(rows, [(0, "ollama", 60000)])

    def test_legacy_hub_rows_are_readable_as_local(self):
        # Rows written before the host column existed are the hub's own, and the
        # migration's DEFAULT must make them queryable under 'local'.
        self.conn.execute("INSERT INTO proc(ts,service,mem) VALUES(0,'ollama',8000)")
        rows = self.repo.vram_by_service("local", 0, 3600, conn=self.conn)
        self.assertEqual(rows, [(0, "ollama", 8000)])


class TestRenameFollowsGpuHistory(unittest.TestCase):
    def test_renaming_a_host_carries_its_gpu_history(self):
        # Without this the cockpit history is orphaned under the old name and
        # the renamed host shows an empty GPU tab.
        conn = _fresh_db()
        from backend.db.repos import gpu_samples as repo
        repo.record(conn, 1000, "oldname", [_card(0), _card(1)])
        conn.execute("INSERT INTO proc(ts,service,mem,host) VALUES(1000,'ollama',8000,'oldname')")
        repo.rename_host("oldname", "newname", conn=conn)
        self.assertEqual(repo.cards_for("oldname", conn=conn), [])
        self.assertEqual(repo.cards_for("newname", conn=conn), [0, 1])
        self.assertEqual(repo.vram_by_service("newname", 0, 3600, conn=conn),
                         [(0, "ollama", 8000)])
        # The rollup has to follow too, or long-range history splits in two.
        n = conn.execute("SELECT COUNT(*) FROM gpu_samples_1h WHERE host='newname'").fetchone()[0]
        self.assertEqual(n, 2)


class TestLegacyGpuRows(unittest.TestCase):
    def test_pre_migration_card_history_reads_as_the_hub(self):
        # The hub has been storing per-card rows for multi-GPU rigs all along
        # without ever displaying them. After the migration that history belongs
        # to 'local' and the cockpit can chart it immediately.
        conn = _fresh_db()
        from backend.db.repos import gpu_samples
        conn.execute("INSERT INTO gpu_samples(ts,idx,util,mem_used,mem_total,power,temp) "
                     "VALUES(100,0,50,12000,24576,200,70)")
        self.assertEqual(gpu_samples.cards_for("local", conn=conn), [0])
        rows = gpu_samples.series("local", 0, 3600, conn=conn)
        self.assertEqual(rows[0][6], 70)


if __name__ == "__main__":
    unittest.main()
