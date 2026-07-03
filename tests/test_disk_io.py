"""Unit tests for the Disk I/O feature:
  • collect_disk_io() delta math, warm-up, device filtering, util% + latency
    (including the div-by-zero -> None guard) and the physical-disk-only
    summary rollup (RAID/dm/partitions excluded from totals)
  • the disk_io_samples/proc_io_samples schema is additive + idempotent
  • /api/data's per-device disk_io series (same bucketing/labels as everything
    else on that endpoint)
  • per-device z-score anomaly firing, edge-triggered event logging
    (diskio_scan), and that it doesn't get miscounted as a GPU OOM event
  • per-process I/O attribution: _read_proc_io parsing, collect_proc_disk_io
    delta math (B/s), warm-up, counter-reset/negative clamp, pid-reuse guard,
    "all unreadable" -> absent, and that collect_top_processes only reads
    /proc/<pid>/io for its bounded candidate set (never every pid)
  • the attribution rides ONLY the authed /api/health.disk_io payload and
    NEVER appears on the public status surface (privacy)
No secrets or raw host paths are leaked in any output asserted here.
"""
import os
import re
import sys
import time
import unittest
from unittest.mock import patch, mock_open

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app

MB = 1048576


def _dline(name, reads, s_read, ms_read, writes, s_write, ms_write, ms_io):
    """One /proc/diskstats line with the column layout collect_disk_io() reads:
    major minor name  reads rmerged s_read ms_read  writes wmerged s_write ms_write  inflight ms_io weighted
    """
    return (f"8 0 {name} {reads} 0 {s_read} {ms_read} "
            f"{writes} 0 {s_write} {ms_write} 0 {ms_io} 0")


class TestCollectDiskIo(unittest.TestCase):
    def setUp(self):
        app._disk_prev = {}

    def _poll(self, lines, now):
        blob = "\n".join(lines) + "\n"
        with patch("app.os.path.exists", return_value=True), \
             patch("app.time.time", return_value=now), \
             patch("builtins.open", mock_open(read_data=blob)):
            return app.collect_disk_io()

    def test_warmup_first_poll_unavailable(self):
        r = self._poll([_dline("sda", 0, 0, 0, 0, 0, 0, 0)], now=1000.0)
        self.assertFalse(r["available"])
        self.assertTrue(r["warming_up"])
        self.assertEqual(r["items"], [])

    def test_delta_throughput_util_latency(self):
        self._poll([_dline("sda", 0, 0, 0, 0, 0, 0, 0)], now=1000.0)
        # dt=1s. read 2048 sectors -> 1.0 MB/s; write 4096 -> 2.0 MB/s.
        # reads +10 with +50 ms -> 5.0 ms/op read latency.
        # writes +0 with +20 ms -> latency guard returns None.
        # ms_io +500 over 1000 ms wall -> 50% utilisation.
        r = self._poll([_dline("sda", 10, 2048, 50, 0, 4096, 20, 500)], now=1001.0)
        self.assertTrue(r["available"])
        self.assertFalse(r["warming_up"])
        it = r["items"][0]
        self.assertEqual(it["device"], "sda")
        self.assertAlmostEqual(it["read_mb_s"], 1.0, places=1)
        self.assertAlmostEqual(it["write_mb_s"], 2.0, places=1)
        self.assertAlmostEqual(it["util_pct"], 50.0, places=1)
        self.assertAlmostEqual(it["read_lat_ms"], 5.0, places=2)
        self.assertIsNone(it["write_lat_ms"])
        self.assertEqual(r["summary"]["total_read_mb_s"], 1.0)
        self.assertEqual(r["summary"]["total_write_mb_s"], 2.0)

    def test_util_clamped_to_100(self):
        self._poll([_dline("sda", 0, 0, 0, 0, 0, 0, 0)], now=2000.0)
        # ms_io delta 99999 over 1000 ms wall would be ~10000% -> clamped to 100.
        r = self._poll([_dline("sda", 0, 0, 0, 0, 0, 0, 99999)], now=2001.0)
        self.assertEqual(r["items"][0]["util_pct"], 100.0)

    def test_device_filtering(self):
        self._poll([_dline("sda", 0, 0, 0, 0, 0, 0, 0),
                    _dline("loop0", 0, 0, 0, 0, 0, 0, 0),
                    _dline("ram1", 0, 0, 0, 0, 0, 0, 0),
                    _dline("sr0", 0, 0, 0, 0, 0, 0, 0)], now=3000.0)
        r = self._poll([_dline("sda", 0, 2048, 0, 0, 0, 0, 0),
                        _dline("loop0", 0, 999999, 0, 0, 0, 0, 0),
                        _dline("ram1", 0, 999999, 0, 0, 0, 0, 0),
                        _dline("sr0", 0, 999999, 0, 0, 0, 0, 0)], now=3001.0)
        devs = {it["device"] for it in r["items"]}
        self.assertEqual(devs, {"sda"})

    def test_sorted_by_total_desc(self):
        self._poll([_dline("sda", 0, 0, 0, 0, 0, 0, 0),
                    _dline("sdb", 0, 0, 0, 0, 0, 0, 0)], now=4000.0)
        r = self._poll([_dline("sda", 0, 2048, 0, 0, 0, 0, 0),      # 1 MB/s total
                        _dline("sdb", 0, 0, 0, 0, 8192, 0, 0)], now=4001.0)  # 4 MB/s
        self.assertEqual([it["device"] for it in r["items"]], ["sdb", "sda"])

    def test_summary_counts_physical_whole_disks_only(self):
        # Stacked layout: physical spindle sda (+ its partition sda1) assembled into
        # an md-RAID md0 (+ its partition md0p1), and an nvme whole-disk nvme0n1
        # (+ partition nvme0n1p1). The md aggregate and every partition restate the
        # same bytes as their whole-disks, so the SUMMARY must count only sda+nvme0n1.
        devs = ["sda", "sda1", "md0", "md0p1", "nvme0n1", "nvme0n1p1"]
        self._poll([_dline(d, 0, 0, 0, 0, 0, 0, 0) for d in devs], now=5000.0)
        lines = [
            _dline("sda",       0, 0, 0, 0, 2048, 0, 0),
            _dline("sda1",      0, 0, 0, 0, 2048, 0, 0),   # partition (excluded)
            _dline("md0",       0, 0, 0, 0, 4096, 0, 0),   # RAID aggregate (excluded)
            _dline("md0p1",     0, 0, 0, 0, 4096, 0, 0),   # md partition (excluded)
            _dline("nvme0n1",   0, 0, 0, 0, 4096, 0, 0),
            _dline("nvme0n1p1", 0, 0, 0, 0, 4096, 0, 0),   # nvme partition (excluded)
        ]
        r = self._poll(lines, now=5001.0)
        self.assertEqual({it["device"] for it in r["items"]}, set(devs))
        self.assertEqual(r["summary"]["total_write_mb_s"], 3.0)   # 1.0 (sda) + 2.0 (nvme0n1)
        self.assertEqual(r["summary"]["total_read_mb_s"], 0.0)
        self.assertTrue(app._is_physical_disk("sda"))
        self.assertTrue(app._is_physical_disk("nvme0n1"))
        self.assertTrue(app._is_physical_disk("vda"))
        self.assertFalse(app._is_physical_disk("sda1"))
        self.assertFalse(app._is_physical_disk("nvme0n1p1"))
        self.assertFalse(app._is_physical_disk("md0"))
        self.assertFalse(app._is_physical_disk("md0p1"))
        self.assertFalse(app._is_physical_disk("dm-0"))

    def test_no_proc_diskstats_unavailable(self):
        with patch("app.os.path.exists", return_value=False):
            r = app.collect_disk_io()
        self.assertFalse(r["available"])
        self.assertFalse(r["warming_up"])
        self.assertEqual(r["items"], [])
        self.assertNotIn("/rootfs", str(r))


class TestSchemaMigrationIdempotent(unittest.TestCase):
    def test_disk_io_samples_columns(self):
        with app.LOCK:
            app.DB.executescript(app._DB_SCHEMA)
            app.DB.executescript(app._DB_SCHEMA)   # re-run must not raise
            cols = [r[1] for r in app.DB.execute("PRAGMA table_info(disk_io_samples)").fetchall()]
        self.assertEqual(cols, ["ts", "device", "read_mb_s", "write_mb_s", "util_pct"])

    def test_proc_io_samples_columns(self):
        with app.LOCK:
            app.DB.executescript(app._DB_SCHEMA)
            cols = [r[1] for r in app.DB.execute("PRAGMA table_info(proc_io_samples)").fetchall()]
        self.assertEqual(cols, ["ts", "pid", "comm", "read_bps", "write_bps"])


def _clear_diskio():
    with app.LOCK:
        app.DB.execute("DELETE FROM disk_io_samples")
        app.DB.commit()


class TestRetention(unittest.TestCase):
    """Exercises the EXACT retention statements sample_once() runs (same table,
    same _DISK_IO_RETENTION/_PROC_IO_RETENTION constants), so a constant that
    drifts from the claimed 7d/72h windows fails here, not silently in prod."""
    def tearDown(self):
        with app.LOCK:
            app.DB.execute("DELETE FROM disk_io_samples")
            app.DB.execute("DELETE FROM proc_io_samples")
            app.DB.commit()

    def test_disk_io_samples_pruned_past_retention(self):
        now = int(time.time())
        old = now - app._DISK_IO_RETENTION - 3600      # 1h past the 7d window
        fresh = now - app._DISK_IO_RETENTION + 3600     # 1h inside the 7d window
        with app.LOCK:
            app.DB.execute("INSERT INTO disk_io_samples(ts,device,read_mb_s,write_mb_s,util_pct) "
                           "VALUES(?,?,?,?,?)", (old, "sda", 1.0, 1.0, 1.0))
            app.DB.execute("INSERT INTO disk_io_samples(ts,device,read_mb_s,write_mb_s,util_pct) "
                           "VALUES(?,?,?,?,?)", (fresh, "sda", 2.0, 2.0, 2.0))
            app.DB.execute("DELETE FROM disk_io_samples WHERE ts<?", (now - app._DISK_IO_RETENTION,))
            app.DB.commit()
            rows = app.DB.execute("SELECT ts FROM disk_io_samples").fetchall()
        self.assertEqual([r[0] for r in rows], [fresh])

    def test_proc_io_samples_pruned_past_retention(self):
        now = int(time.time())
        old = now - app._PROC_IO_RETENTION - 3600       # 1h past the 72h window
        fresh = now - app._PROC_IO_RETENTION + 3600      # 1h inside the 72h window
        with app.LOCK:
            app.DB.execute("INSERT INTO proc_io_samples(ts,pid,comm,read_bps,write_bps) "
                           "VALUES(?,?,?,?,?)", (old, 1, "stale", 0, 1))
            app.DB.execute("INSERT INTO proc_io_samples(ts,pid,comm,read_bps,write_bps) "
                           "VALUES(?,?,?,?,?)", (fresh, 2, "recent", 0, 1))
            app.DB.execute("DELETE FROM proc_io_samples WHERE ts<?", (now - app._PROC_IO_RETENTION,))
            app.DB.commit()
            comms = {r[0] for r in app.DB.execute("SELECT comm FROM proc_io_samples")}
        self.assertEqual(comms, {"recent"})


class TestApiDataDiskIoSeries(unittest.TestCase):
    """The per-device trend series folded into /api/data (no separate endpoint —
    it rides the same bucketing/labels/range as every other chart on the tab)."""
    def setUp(self):
        self.c = app.app.test_client()
        _clear_diskio()

    def tearDown(self):
        _clear_diskio()

    def _seed(self, device, n=10, base=5.0, step=45):
        now = int(time.time())
        with app.LOCK:
            for i in range(n):
                ts = now - (n - i) * step
                app.DB.execute(
                    "INSERT INTO disk_io_samples(ts,device,read_mb_s,write_mb_s,util_pct) "
                    "VALUES(?,?,?,?,?)", (ts, device, base, base / 2, base))
            app.DB.commit()

    def test_disk_io_series_shape(self):
        self._seed("sda", n=10)
        j = self.c.get("/api/data?range=1h").get_json()
        self.assertIn("disk_io", j)
        self.assertIn("sda", j["disk_io"])
        d = j["disk_io"]["sda"]
        for k in ("read_mb_s", "write_mb_s", "util_pct"):
            self.assertEqual(len(d[k]), len(j["labels"]))

    def test_empty_when_no_samples(self):
        j = self.c.get("/api/data?range=1h").get_json()
        self.assertEqual(j["disk_io"], {})


class TestDiskIoAnomaly(unittest.TestCase):
    def setUp(self):
        _clear_diskio()
        app._diskio_anom_active = set()
        app._diskio_anom_latest = {}

    def tearDown(self):
        _clear_diskio()
        with app.LOCK:
            app.DB.execute("DELETE FROM events WHERE kind='diskio_spike'")
            app.DB.commit()

    def _seed_baseline_then_spike(self, device="sda", n=50):
        now = int(time.time())
        with app.LOCK:
            for i in range(n):
                ts = now - (n + 1 - i) * 45
                app.DB.execute(
                    "INSERT INTO disk_io_samples(ts,device,read_mb_s,write_mb_s,util_pct) "
                    "VALUES(?,?,?,?,?)", (ts, device, 3.0 + (i % 2) * 0.2, 1.0, 5.0))
            app.DB.execute(
                "INSERT INTO disk_io_samples(ts,device,read_mb_s,write_mb_s,util_pct) "
                "VALUES(?,?,?,?,?)", (now, device, 400.0, 300.0, 99.0))
            app.DB.commit()
        return now

    def test_spike_fires(self):
        now = self._seed_baseline_then_spike()
        with app.LOCK:
            items = app._disk_io_anomaly_items(app.DB.cursor(), now)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["device"], "sda")
        self.assertEqual(items[0]["direction"], "spike")

    def test_flat_series_does_not_fire(self):
        now = int(time.time())
        with app.LOCK:
            for i in range(60):
                ts = now - (60 - i) * 45
                app.DB.execute(
                    "INSERT INTO disk_io_samples(ts,device,read_mb_s,write_mb_s,util_pct) "
                    "VALUES(?,?,?,?,?)", (ts, "sdb", 2.0, 1.0, 3.0))
            app.DB.commit()
            items = app._disk_io_anomaly_items(app.DB.cursor(), now)
        self.assertEqual(items, [])

    def test_diskio_scan_logs_one_event_edge_triggered(self):
        self._seed_baseline_then_spike()
        app.diskio_scan()
        with app.LOCK:
            n1 = app.DB.execute("SELECT COUNT(*) FROM events WHERE kind='diskio_spike'").fetchone()[0]
        self.assertEqual(n1, 1)
        self.assertIn("sda", app._diskio_anom_active)
        # Still firing on the next scan (same DB state) — must NOT log a second event.
        app.diskio_scan()
        with app.LOCK:
            n2 = app.DB.execute("SELECT COUNT(*) FROM events WHERE kind='diskio_spike'").fetchone()[0]
        self.assertEqual(n2, 1)

    def test_diskio_scan_updates_latest_for_live_badge(self):
        self._seed_baseline_then_spike()
        app.diskio_scan()
        self.assertIn("sda", app._diskio_anom_latest)
        self.assertEqual(app._diskio_anom_latest["sda"]["direction"], "spike")

    def test_diskio_events_not_miscounted_as_oom(self):
        # A diskio_spike event in the `events` table must never be treated as a
        # GPU-OOM event by /api/data's insight-building (they share the table).
        now = int(time.time())
        with app.LOCK:
            app.DB.execute("INSERT INTO events VALUES(?,?,?,?)", (now, "sda", "diskio_spike", "spike"))
            app.DB.commit()
        j = app.app.test_client().get("/api/data?range=1h").get_json()
        self.assertEqual(j["events"], [])   # events key stays OOM-only
        titles = [i.get("title", "") for i in j["insights"]]
        self.assertFalse(any("out-of-memory" in t.lower() for t in titles))
        self.assertTrue(any("disk i/o spike on sda" in t.lower() for t in titles))


def _io_blob(rb, wb):
    """A realistic /proc/<pid>/io body (the fields we don't use are still present)."""
    return ("rchar: 100\nwchar: 200\nsyscr: 3\nsyscw: 4\n"
            "read_bytes: %d\nwrite_bytes: %d\ncancelled_write_bytes: 0\n" % (rb, wb))


def _fake_io_open(table):
    """side_effect for builtins.open that serves /proc/<pid>/io from `table`
    ({pid_str: (rb, wb)}); a pid absent from the table raises (unreadable)."""
    def _open(path, *a, **k):
        m = re.match(r"/proc/(\d+)/io$", path)
        if m and m.group(1) in table:
            rb, wb = table[m.group(1)]
            return mock_open(read_data=_io_blob(rb, wb))()
        raise FileNotFoundError(path)
    return _open


class TestReadProcIo(unittest.TestCase):
    def test_parses_read_write_bytes(self):
        with patch("builtins.open", side_effect=_fake_io_open({"7": (11, 22)})):
            self.assertEqual(app._read_proc_io("7"), (11, 22))

    def test_missing_file_returns_none(self):
        with patch("builtins.open", side_effect=_fake_io_open({})):
            self.assertIsNone(app._read_proc_io("7"))

    def test_permission_error_returns_none(self):
        with patch("builtins.open", side_effect=PermissionError("EACCES")):
            self.assertIsNone(app._read_proc_io("7"))

    def test_absent_fields_returns_none(self):
        with patch("builtins.open", mock_open(read_data="rchar: 1\nwchar: 2\n")):
            self.assertIsNone(app._read_proc_io("7"))


class TestCollectProcDiskIo(unittest.TestCase):
    def setUp(self):
        app._PROC_IO_PREV = {}

    def _poll(self, table, cands, now):
        with patch("builtins.open", side_effect=_fake_io_open(table)):
            return app.collect_proc_disk_io(cands, now=now)

    def test_warmup_then_delta_bps(self):
        cands = [("10", "postgres", 100)]
        r1 = self._poll({"10": (0, 0)}, cands, now=0.0)
        self.assertTrue(r1["available"])
        self.assertIsNone(r1["top_writer"])
        r2 = self._poll({"10": (5 * MB, 20 * MB)}, cands, now=10.0)
        self.assertEqual(r2["top_writer"]["name"], "postgres")
        self.assertAlmostEqual(r2["top_writer"]["write_b_s"], 2 * MB, delta=1024)
        self.assertAlmostEqual(r2["top_reader"]["read_b_s"], 0.5 * MB, delta=1024)

    def test_counter_reset_is_clamped(self):
        cands = [("10", "x", 100)]
        self._poll({"10": (100 * MB, 100 * MB)}, cands, now=0.0)
        r = self._poll({"10": (1 * MB, 1 * MB)}, cands, now=10.0)
        self.assertIsNone(r["top_writer"])
        self.assertIsNone(r["top_reader"])

    def test_pid_reuse_guard(self):
        self._poll({"10": (0, 0)}, [("10", "old", 100)], now=0.0)
        r = self._poll({"10": (999 * MB, 999 * MB)}, [("10", "new", 555)], now=10.0)
        self.assertIsNone(r["top_writer"])

    def test_all_unreadable_is_absent(self):
        r = self._poll({}, [("10", "x", 100), ("11", "y", 101)], now=5.0)
        self.assertFalse(r["available"])
        self.assertNotIn("top_writer", r)

    def test_prev_state_pruned_to_candidates(self):
        self._poll({"10": (0, 0), "11": (0, 0)},
                   [("10", "a", 1), ("11", "b", 2)], now=0.0)
        self._poll({"10": (0, 0)}, [("10", "a", 1)], now=10.0)
        self.assertIn("10", app._PROC_IO_PREV)
        self.assertNotIn("11", app._PROC_IO_PREV)


def _stat_blob(pid, comm, utime, stime=0, starttime=100):
    # /proc/<pid>/stat: after comm, rest[11]=utime, rest[12]=stime, rest[19]=starttime.
    return ("%s (%s) S 1 1 1 0 -1 0 0 0 0 0 %d %d 0 0 20 0 1 0 %d\n"
            % (pid, comm, utime, stime, starttime))


class TestTopNBounding(unittest.TestCase):
    """collect_top_processes must sample /proc/<pid>/io for the bounded candidate
    set only — never scan every pid."""
    def setUp(self):
        app._PROC_PREV = {"total": None, "pids": {}}
        app._PROC_IO_PREV = {}

    def test_io_read_only_for_candidates(self):
        n = 25
        io_pids = set()

        def make_open(jiff_of):
            def fake_open(path, *a, **k):
                if path == "/proc/stat":
                    return mock_open(read_data="cpu 100000 0 0 0 0 0 0 0\n")()
                m = re.match(r"/proc/(\d+)/stat$", path)
                if m:
                    p = int(m.group(1))
                    return mock_open(read_data=_stat_blob(m.group(1), "proc%02d" % p,
                                                          jiff_of(p), starttime=p * 7))()
                m = re.match(r"/proc/(\d+)/statm$", path)
                if m:
                    return mock_open(read_data="9999 %d 0 0 0 0 0\n" % (int(m.group(1)) * 8))()
                m = re.match(r"/proc/(\d+)/io$", path)
                if m:
                    io_pids.add(m.group(1))
                    return mock_open(read_data=_io_blob(1000, 2000))()
                raise FileNotFoundError(path)
            return fake_open

        pids = [str(p) for p in range(1, n + 1)]
        with patch("app.os.listdir", return_value=pids), \
             patch("app.os.cpu_count", return_value=4):
            with patch("builtins.open", side_effect=make_open(lambda p: p * 10)):
                app.collect_top_processes(top_n=10)
            io_pids.clear()
            with patch("builtins.open", side_effect=make_open(lambda p: p * 11)):
                r = app.collect_top_processes(top_n=10)
        self.assertIn("io", r)
        self.assertLessEqual(len(io_pids), 20)
        self.assertLess(len(io_pids), n)
        self.assertNotIn("1", io_pids)   # lowest cpu AND lowest mem -> never a candidate


class TestApiHealthAttributionAndPrivacy(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()

    def _attr(self):
        return {"available": True,
                "top_writer": {"name": "postgres", "pid": 10, "read_b_s": 0, "write_b_s": 42 * MB},
                "top_reader": {"name": "rsync", "pid": 11, "read_b_s": 9 * MB, "write_b_s": 0},
                "writers": [{"name": "postgres", "pid": 10, "read_b_s": 0, "write_b_s": 42 * MB}],
                "readers": [{"name": "rsync", "pid": 11, "read_b_s": 9 * MB, "write_b_s": 0}]}

    def test_attribution_on_authed_health(self):
        dio = {"available": True, "summary": {"total_read_mb_s": 9, "total_write_mb_s": 42},
               "items": [{"device": "sda", "read_mb_s": 9, "write_mb_s": 42, "util_pct": 80,
                          "read_lat_ms": 1.0, "write_lat_ms": 2.0}]}
        with patch.dict(app.HEALTH, {"disk_io": dio, "processes": {"io": self._attr()}}):
            j = self.c.get("/api/health").get_json()
        self.assertIn("attribution", j["disk_io"])
        self.assertEqual(j["disk_io"]["attribution"]["top_writer"]["name"], "postgres")

    def test_attribution_absent_when_unavailable(self):
        dio = {"available": True, "summary": {"total_read_mb_s": 0, "total_write_mb_s": 0}, "items": []}
        with patch.dict(app.HEALTH, {"disk_io": dio, "processes": {"io": {"available": False}}}):
            j = self.c.get("/api/health").get_json()
        self.assertNotIn("attribution", j["disk_io"])

    def test_attribution_never_on_public_status(self):
        dio = {"available": True, "summary": {"total_read_mb_s": 9, "total_write_mb_s": 42},
               "items": [{"device": "sda", "read_mb_s": 9, "write_mb_s": 42, "util_pct": 80,
                          "read_lat_ms": 1.0, "write_lat_ms": 2.0}]}
        with patch.dict(app.HEALTH, {"disk_io": dio, "processes": {"io": self._attr()}}), \
             patch.dict(os.environ, {"PUBLIC_STATUS": "1"}):
            r = self.c.get("/api/public-status")
        self.assertNotIn(b"attribution", r.data)
        self.assertNotIn(b"postgres", r.data)
        self.assertNotIn(b"top_writer", r.data)


if __name__ == "__main__":
    unittest.main()
