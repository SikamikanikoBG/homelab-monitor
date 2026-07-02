"""Unit tests for the Disk I/O feature (issue #196, done our way):
  • collect_disk_io() delta math, warm-up, device filtering, util% + latency
    (including the div-by-zero -> None guard)
  • the disk_io_samples migration is additive + idempotent
  • history insert/prune shape + the /api/diskio/history endpoint
  • per-device z-score anomaly firing on a synthetic spike, merged into the
    shared anomaly bundle
  • the Copilot 'disk_io' ask topic is detected and pulls the live snapshot +
    busiest-processes context
No secrets or raw host paths are leaked in any output asserted here.
"""
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _dline(name, reads, s_read, ms_read, writes, s_write, ms_write, ms_io):
    """One /proc/diskstats line with our exact column layout (14 tokens):
    major minor name  reads rmerged s_read ms_read  writes wmerged s_write ms_write  inflight ms_io weighted
    """
    return (f"8 0 {name} {reads} 0 {s_read} {ms_read} "
            f"{writes} 0 {s_write} {ms_write} 0 {ms_io} 0")


class TestCollectDiskIo(unittest.TestCase):
    def setUp(self):
        app._disk_io_prev = {}

    def _poll(self, lines, now):
        blob = "\n".join(lines) + "\n"
        with patch("app.os.path.exists", return_value=True), \
             patch("app.time.time", return_value=now), \
             patch("builtins.open", unittest.mock.mock_open(read_data=blob)):
            return app.collect_disk_io()

    def test_warmup_first_poll_empty(self):
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

    def test_no_proc_diskstats_unavailable(self):
        with patch("app.os.path.exists", return_value=False):
            r = app.collect_disk_io()
        self.assertFalse(r["available"])
        self.assertFalse(r["warming_up"])
        self.assertEqual(r["items"], [])
        # no raw host path leaked into the payload beyond the generic reason
        self.assertNotIn("/rootfs", str(r))


class TestMigrationIdempotent(unittest.TestCase):
    def test_schema_reapply_is_noop(self):
        # additive + idempotent: re-running the whole schema must not raise and the
        # disk_io_samples table (+ its index) must exist with the expected columns.
        with app.LOCK:
            app.DB.executescript(app._DB_SCHEMA)
            app.DB.executescript(app._DB_SCHEMA)
            cols = [r[1] for r in app.DB.execute("PRAGMA table_info(disk_io_samples)").fetchall()]
        self.assertEqual(cols, ["ts", "device", "read_mb_s", "write_mb_s", "util_pct"])


def _clear_diskio():
    with app.LOCK:
        app.DB.execute("DELETE FROM disk_io_samples")
        app.DB.commit()


class TestHistoryEndpoint(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()
        _clear_diskio()

    def tearDown(self):
        _clear_diskio()

    def _seed(self, device, n=40, base=5.0, step=45):
        now = int(time.time())
        with app.LOCK:
            for i in range(n):
                ts = now - (n - i) * step
                app.DB.execute(
                    "INSERT INTO disk_io_samples(ts,device,read_mb_s,write_mb_s,util_pct) "
                    "VALUES(?,?,?,?,?)", (ts, device, base, base / 2, base))
            app.DB.commit()
        return now

    def test_history_series_shape(self):
        self._seed("sda", n=10)
        j = self.c.get("/api/diskio/history?window=3600").get_json()
        self.assertIn("devices", j)
        self.assertEqual(len(j["devices"]), 1)
        d = j["devices"][0]
        self.assertEqual(d["device"], "sda")
        for k in ("ts", "read_mb_s", "write_mb_s", "util_pct"):
            self.assertEqual(len(d[k]), 10)

    def test_history_window_prunes_old(self):
        now = int(time.time())
        with app.LOCK:
            app.DB.execute("INSERT INTO disk_io_samples VALUES(?,?,?,?,?)",
                           (now - 100000, "sda", 1.0, 1.0, 1.0))  # outside 1h window
            app.DB.execute("INSERT INTO disk_io_samples VALUES(?,?,?,?,?)",
                           (now - 60, "sda", 2.0, 2.0, 2.0))       # inside
            app.DB.commit()
        j = self.c.get("/api/diskio/history?window=3600").get_json()
        self.assertEqual(len(j["devices"][0]["ts"]), 1)


class TestDiskIoAnomaly(unittest.TestCase):
    def setUp(self):
        _clear_diskio()

    def tearDown(self):
        _clear_diskio()

    def test_spike_fires_and_merges_into_bundle(self):
        now = int(time.time())
        with app.LOCK:
            # a long calm baseline (~5 MB/s total) then a sharp final spike
            for i in range(50):
                ts = now - (51 - i) * 45
                app.DB.execute(
                    "INSERT INTO disk_io_samples(ts,device,read_mb_s,write_mb_s,util_pct) "
                    "VALUES(?,?,?,?,?)", (ts, "sda", 3.0 + (i % 2) * 0.2, 1.0, 5.0))
            # the latest point: a huge spike
            app.DB.execute(
                "INSERT INTO disk_io_samples(ts,device,read_mb_s,write_mb_s,util_pct) "
                "VALUES(?,?,?,?,?)", (now, "sda", 400.0, 300.0, 99.0))
            app.DB.commit()
            bundle = app._zscore_anomalies(app.DB.cursor(), now)
        keys = [it["key"] for it in bundle["items"]]
        self.assertIn("disk_io:sda", keys)
        it = next(i for i in bundle["items"] if i["key"] == "disk_io:sda")
        self.assertEqual(it["direction"], "spike")
        self.assertEqual(it["device"], "sda")
        self.assertEqual(it["unit"], "MB/s")

    def test_flat_series_does_not_fire(self):
        now = int(time.time())
        with app.LOCK:
            for i in range(60):
                ts = now - (60 - i) * 45
                app.DB.execute(
                    "INSERT INTO disk_io_samples(ts,device,read_mb_s,write_mb_s,util_pct) "
                    "VALUES(?,?,?,?,?)", (ts, "sdb", 2.0, 1.0, 3.0))
            app.DB.commit()
            bundle = app._zscore_anomalies(app.DB.cursor(), now)
        keys = [it["key"] for it in bundle["items"]]
        self.assertNotIn("disk_io:sdb", keys)


class TestCopilotDiskIoTopic(unittest.TestCase):
    def test_topic_detected(self):
        self.assertIn("disk_io", app._ask_detect_topics("why is disk i/o so high right now"))
        self.assertIn("disk_io", app._ask_detect_topics("what is writing heavily to sda"))

    def test_topic_facts_pull_snapshot_and_procs(self):
        snap = {"available": True,
                "summary": {"total_read_mb_s": 12.3, "total_write_mb_s": 4.5},
                "items": [{"device": "sda", "read_mb_s": 12.3, "write_mb_s": 4.5,
                           "util_pct": 88.0, "read_lat_ms": 5.0, "write_lat_ms": None}]}
        procs = {"by_cpu": [{"name": "postgres", "cpu_pct": 42.0, "mem_mb": 1024}]}
        with patch.dict(app.HEALTH, {"disk_io": snap, "processes": procs}):
            lines, srcs = app._ask_topic_facts({"disk_io"}, "disk io?", {}, int(time.time()))
        self.assertIn("disk_io", srcs)
        joined = " ".join(lines)
        self.assertIn("sda", joined)
        self.assertIn("88.0% util", joined)
        self.assertIn("postgres", joined)

    def test_explain_context_enriches_disk_io_key(self):
        snap = {"available": True, "summary": {"total_read_mb_s": 9, "total_write_mb_s": 1},
                "items": [{"device": "nvme0n1", "read_mb_s": 9, "write_mb_s": 1,
                           "util_pct": 70, "read_lat_ms": 2.0, "write_lat_ms": 1.0}]}
        with patch.dict(app.HEALTH, {"disk_io": snap, "processes": {"by_cpu": []}}):
            ctx = app._explain_context({"key": "disk_io:nvme0n1", "direction": "spike",
                                        "value": 10.0, "baseline": 2.0, "z": 5.0, "unit": "MB/s"})
        self.assertEqual(ctx["disk_io"]["device"], "nvme0n1")
        self.assertIn("nvme0n1", ctx["label"])


if __name__ == "__main__":
    unittest.main()
