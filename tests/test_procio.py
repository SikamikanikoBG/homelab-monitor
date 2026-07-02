"""Unit tests for per-process disk-I/O attribution (the "what was writing
heavily" answer). Covers:
  • _read_proc_io parsing + graceful None on missing/unreadable /proc/<pid>/io
  • collect_proc_disk_io delta math (B/s), warm-up, counter-reset/negative clamp,
    pid-reuse guard (start-time mismatch → drop), and "all unreadable" → absent
  • top-N bounding: collect_top_processes reads /proc/<pid>/io for the bounded
    candidate set only, never every pid
  • the attribution rides ONLY the authed /api/health.disk_io payload, is ABSENT
    when unreadable, and NEVER appears on the public /status payload (privacy)
  • the Copilot disk_io ask topic + explain context include the real per-process
    leaders when available — and neither path makes any LLM call
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
        self.assertTrue(r1["available"])            # readable, but no delta yet
        self.assertIsNone(r1["top_writer"])
        r2 = self._poll({"10": (5 * MB, 20 * MB)}, cands, now=10.0)
        self.assertEqual(r2["top_writer"]["name"], "postgres")
        self.assertAlmostEqual(r2["top_writer"]["write_b_s"], 2 * MB, delta=1024)
        self.assertAlmostEqual(r2["top_reader"]["read_b_s"], 0.5 * MB, delta=1024)

    def test_counter_reset_is_clamped(self):
        cands = [("10", "x", 100)]
        self._poll({"10": (100 * MB, 100 * MB)}, cands, now=0.0)
        # counters went DOWN (process re-exec / kernel reset) → negative delta dropped
        r = self._poll({"10": (1 * MB, 1 * MB)}, cands, now=10.0)
        self.assertIsNone(r["top_writer"])
        self.assertIsNone(r["top_reader"])

    def test_pid_reuse_guard(self):
        # Same pid, but start-time changed → a recycled pid; stale counter dropped.
        self._poll({"10": (0, 0)}, [("10", "old", 100)], now=0.0)
        r = self._poll({"10": (999 * MB, 999 * MB)}, [("10", "new", 555)], now=10.0)
        self.assertIsNone(r["top_writer"])          # not attributed to the new pid

    def test_all_unreadable_is_absent(self):
        r = self._poll({}, [("10", "x", 100), ("11", "y", 101)], now=5.0)
        self.assertFalse(r["available"])
        self.assertNotIn("top_writer", r)

    def test_prev_state_pruned_to_candidates(self):
        self._poll({"10": (0, 0), "11": (0, 0)},
                   [("10", "a", 1), ("11", "b", 2)], now=0.0)
        self._poll({"10": (0, 0)}, [("10", "a", 1)], now=10.0)   # 11 no longer a cand
        self.assertIn("10", app._PROC_IO_PREV)
        self.assertNotIn("11", app._PROC_IO_PREV)               # dead pid pruned


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
        # 25 distinct processes; top_n=10 → at most 20 candidates (by cpu ∪ by mem).
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
            # First poll seeds jiffy prevs; second gives real per-pid CPU deltas
            # (delta = p), so low-pid procs are neither top-CPU nor top-RAM.
            with patch("builtins.open", side_effect=make_open(lambda p: p * 10)):
                app.collect_top_processes(top_n=10)
            io_pids.clear()
            with patch("builtins.open", side_effect=make_open(lambda p: p * 11)):
                r = app.collect_top_processes(top_n=10)
        self.assertIn("io", r)
        self.assertLessEqual(len(io_pids), 20)      # bounded by 2*top_n
        self.assertLess(len(io_pids), n)            # NOT every pid scanned
        # pid "1" is lowest cpu AND lowest mem → never a candidate → never read
        self.assertNotIn("1", io_pids)


class TestApiHealthAndPrivacy(unittest.TestCase):
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
        with patch.dict(app.HEALTH, {"disk_io": dio, "processes": {"io": self._attr()}}):
            pub = app.build_public_status()
        blob = str(pub)
        self.assertNotIn("attribution", blob)
        self.assertNotIn("postgres", blob)          # no per-process leak on public surface
        self.assertNotIn("top_writer", blob)


class TestCopilotEnrichmentNoLLM(unittest.TestCase):
    """The disk_io Copilot context includes the real leaders when available — and
    the read/poll path makes ZERO LLM calls."""
    def _attr(self):
        return {"available": True,
                "top_writer": {"name": "postgres", "pid": 10, "read_b_s": 0, "write_b_s": 42 * MB},
                "top_reader": {"name": "rsync", "pid": 11, "read_b_s": 9 * MB, "write_b_s": 0},
                "writers": [{"name": "postgres", "pid": 10, "read_b_s": 0, "write_b_s": 42 * MB}],
                "readers": [{"name": "rsync", "pid": 11, "read_b_s": 9 * MB, "write_b_s": 0}]}

    def test_ask_topic_facts_include_leaders_no_llm(self):
        snap = {"available": True, "summary": {"total_read_mb_s": 9, "total_write_mb_s": 42},
                "items": [{"device": "sda", "read_mb_s": 9, "write_mb_s": 42, "util_pct": 88,
                           "read_lat_ms": 5.0, "write_lat_ms": None}]}
        procs = {"by_cpu": [{"name": "python", "cpu_pct": 30, "mem_mb": 100}], "io": self._attr()}
        with patch("app._ollama_generate", side_effect=AssertionError("LLM must not run")), \
             patch("app._ollama_generate_stream", side_effect=AssertionError("LLM must not run")), \
             patch.dict(app.HEALTH, {"disk_io": snap, "processes": procs}):
            lines, srcs = app._ask_topic_facts({"disk_io"}, "what is writing heavily?", {}, int(time.time()))
        self.assertIn("disk_io", srcs)
        joined = " ".join(lines)
        self.assertIn("postgres", joined)           # real writer, not just the cpu proxy
        self.assertIn("rsync", joined)
        self.assertIn("MB/s", joined)

    def test_ask_topic_facts_omit_leaders_when_absent(self):
        snap = {"available": True, "summary": {"total_read_mb_s": 1, "total_write_mb_s": 1},
                "items": [{"device": "sda", "read_mb_s": 1, "write_mb_s": 1, "util_pct": 5,
                           "read_lat_ms": None, "write_lat_ms": None}]}
        procs = {"by_cpu": [{"name": "python", "cpu_pct": 30, "mem_mb": 100}],
                 "io": {"available": False}}
        with patch.dict(app.HEALTH, {"disk_io": snap, "processes": procs}):
            lines, _ = app._ask_topic_facts({"disk_io"}, "disk io?", {}, int(time.time()))
        joined = " ".join(lines)
        self.assertNotIn("Heaviest writer", joined)

    def test_explain_context_includes_leaders_no_llm(self):
        snap = {"available": True, "summary": {"total_read_mb_s": 9, "total_write_mb_s": 42},
                "items": [{"device": "sda", "read_mb_s": 9, "write_mb_s": 42, "util_pct": 88,
                           "read_lat_ms": 5.0, "write_lat_ms": 1.0}]}
        with patch("app._ollama_generate", side_effect=AssertionError("LLM must not run")), \
             patch.dict(app.HEALTH, {"disk_io": snap, "processes": {"by_cpu": [], "io": self._attr()}}):
            ctx = app._explain_context({"key": "disk_io:sda", "direction": "spike",
                                        "value": 50.0, "baseline": 5.0, "z": 6.0, "unit": "MB/s"})
            facts = app._explain_facts(ctx)
        self.assertEqual(ctx["io_writers"][0]["name"], "postgres")
        self.assertTrue(any("postgres" in f for f in facts))


if __name__ == "__main__":
    unittest.main()
