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


class TestProcIoRingSchema(unittest.TestCase):
    """The persisted per-process I/O ring: additive/idempotent migration."""
    def test_migration_idempotent(self):
        with app.LOCK:
            app.DB.executescript(app._DB_SCHEMA)
            app.DB.executescript(app._DB_SCHEMA)          # re-run must not raise
            cols = [r[1] for r in app.DB.execute(
                "PRAGMA table_info(proc_io_samples)").fetchall()]
        self.assertEqual(cols, ["ts", "pid", "comm", "read_bps", "write_bps"])


def _seed_pio(rows):
    """rows: iterable of (ts, pid, comm, read_bps, write_bps)."""
    with app.LOCK:
        app.DB.execute("DELETE FROM proc_io_samples")
        app.DB.executemany(
            "INSERT INTO proc_io_samples(ts,pid,comm,read_bps,write_bps) VALUES(?,?,?,?,?)",
            list(rows))
        app.DB.commit()


class TestProcIoAt(unittest.TestCase):
    """_proc_io_at joins the persisted ring to an anomaly window."""
    def test_returns_top_writer_in_window(self):
        t = 1_000_000
        _seed_pio([
            (t,     10, "postgres", 0,      42 * MB),
            (t + 5, 11, "rsync",    9 * MB, 1 * MB),
            (t - 5, 10, "postgres", 0,      40 * MB),
        ])
        r = app._proc_io_at(t, window=120)
        self.assertTrue(r["available"])
        self.assertEqual(r["writers"][0]["name"], "postgres")
        self.assertAlmostEqual(r["writers"][0]["write_b_s"], 42 * MB, delta=1)
        self.assertEqual(r["readers"][0]["name"], "rsync")

    def test_returns_none_when_no_history_in_window(self):
        _seed_pio([(1_000_000, 10, "postgres", 0, 42 * MB)])
        self.assertIsNone(app._proc_io_at(2_000_000, window=120))   # far from any row

    def test_returns_none_on_empty_ring(self):
        _seed_pio([])
        self.assertIsNone(app._proc_io_at(1_000_000))

    def test_bounded_query_limit(self):
        t = 500_000
        _seed_pio([(t, p, "w%02d" % p, 0, (p + 1) * MB) for p in range(10)])
        r = app._proc_io_at(t, window=60, limit=3)
        self.assertLessEqual(len(r["writers"]), 3)


class TestProcIoRingPersistence(unittest.TestCase):
    """The poll-path INSERT persists ONLY the top-few writers/readers (bounded),
    deduped by pid — not all candidates."""
    def _attr_many(self):
        writers = [{"name": "w%02d" % p, "pid": p, "read_b_s": 0,
                    "write_b_s": (100 - p) * MB} for p in range(1, 11)]
        readers = [{"name": "r%02d" % p, "pid": 100 + p, "read_b_s": (100 - p) * MB,
                    "write_b_s": 0} for p in range(1, 11)]
        return {"available": True, "writers": writers, "readers": readers,
                "top_writer": writers[0], "top_reader": readers[0]}

    def _run_persist(self, ts):
        """Exercise ONLY the ring-INSERT snippet with the real DB (mirrors the
        ~45s cadence block: top-3 writers + top-3 readers, deduped by pid)."""
        _pio = self._attr_many()
        seen, out = set(), []
        for _r in (sorted(_pio["writers"], key=lambda r: -(r.get("write_b_s") or 0))[:3]
                   + sorted(_pio["readers"], key=lambda r: -(r.get("read_b_s") or 0))[:3]):
            p = _r.get("pid")
            if p in seen:
                continue
            seen.add(p)
            out.append((ts, p, _r.get("name"),
                        int(_r.get("read_b_s") or 0), int(_r.get("write_b_s") or 0)))
        with app.LOCK:
            app.DB.executemany(
                "INSERT INTO proc_io_samples(ts,pid,comm,read_bps,write_bps) VALUES(?,?,?,?,?)",
                out)
            app.DB.commit()
        return out

    def test_persists_only_top_few(self):
        with app.LOCK:
            app.DB.execute("DELETE FROM proc_io_samples")
            app.DB.commit()
        rows = self._run_persist(1234)
        # top-3 writers + top-3 readers, no pid overlap here → exactly 6 rows.
        self.assertEqual(len(rows), 6)
        with app.LOCK:
            n = app.DB.execute("SELECT COUNT(*) FROM proc_io_samples").fetchone()[0]
            comms = {r[0] for r in app.DB.execute("SELECT comm FROM proc_io_samples")}
        self.assertEqual(n, 6)                     # NOT all 20 candidates
        self.assertIn("w01", comms)                # heaviest writer kept
        self.assertNotIn("w10", comms)             # 10th writer NOT persisted

    def test_prune_enforces_retention(self):
        old = 1000
        fresh = old + app._PROC_IO_RETENTION + 10
        _seed_pio([(old, 1, "stale", 0, MB), (fresh, 2, "fresh", 0, MB)])
        with app.LOCK:
            app.DB.execute("DELETE FROM proc_io_samples WHERE ts<?",
                           (fresh - app._PROC_IO_RETENTION,))
            app.DB.commit()
            comms = {r[0] for r in app.DB.execute("SELECT comm FROM proc_io_samples")}
        self.assertIn("fresh", comms)
        self.assertNotIn("stale", comms)           # beyond 72h retention → pruned


class TestSpikeAttributionJoinNoLLM(unittest.TestCase):
    """The explain context names the HISTORICAL writer from the ring when present,
    falls back to the live leader otherwise — and the join makes ZERO LLM calls."""
    def _live_attr(self):
        return {"available": True,
                "top_writer": {"name": "live_writer", "pid": 9, "read_b_s": 0, "write_b_s": 5 * MB},
                "writers": [{"name": "live_writer", "pid": 9, "read_b_s": 0, "write_b_s": 5 * MB}],
                "readers": []}

    def test_names_historical_writer_over_live(self):
        now = int(time.time())
        ts = now - 3600
        _seed_pio([(ts, 10, "spike_writer", 0, 80 * MB)])
        snap = {"available": True, "summary": {"total_read_mb_s": 9, "total_write_mb_s": 80},
                "items": [{"device": "sda", "read_mb_s": 9, "write_mb_s": 80, "util_pct": 90}]}
        with patch("app._ollama_generate", side_effect=AssertionError("LLM must not run")), \
             patch("app._ollama_generate_stream", side_effect=AssertionError("LLM must not run")), \
             patch.dict(app.HEALTH, {"disk_io": snap, "processes": {"io": self._live_attr()}}):
            ctx = app._explain_context({"key": "disk_io:sda", "ts": ts, "direction": "spike",
                                        "value": 80.0, "baseline": 5.0, "z": 7.0, "unit": "MB/s"})
            facts = app._explain_facts(ctx)
        self.assertTrue(ctx.get("io_historical"))
        self.assertEqual(ctx["io_writers"][0]["name"], "spike_writer")
        self.assertTrue(any("spike_writer" in f and "during the spike" in f for f in facts))
        self.assertFalse(any("live_writer" in f for f in facts))

    def test_falls_back_to_live_when_no_history(self):
        now = int(time.time())
        _seed_pio([])                               # empty ring → no window coverage
        snap = {"available": True, "summary": {"total_read_mb_s": 0, "total_write_mb_s": 5},
                "items": [{"device": "sda", "read_mb_s": 0, "write_mb_s": 5, "util_pct": 50}]}
        with patch("app._ollama_generate", side_effect=AssertionError("LLM must not run")), \
             patch.dict(app.HEALTH, {"disk_io": snap, "processes": {"io": self._live_attr()}}):
            ctx = app._explain_context({"key": "disk_io:sda", "ts": now, "direction": "spike",
                                        "value": 5.0, "baseline": 1.0, "z": 4.0, "unit": "MB/s"})
            facts = app._explain_facts(ctx)
        self.assertFalse(ctx.get("io_historical"))
        self.assertEqual(ctx["io_writers"][0]["name"], "live_writer")
        self.assertTrue(any("live_writer" in f for f in facts))

    def test_proc_io_at_makes_no_llm_call(self):
        t = int(time.time())
        _seed_pio([(t, 10, "postgres", 0, 42 * MB)])
        with patch("app._ollama_generate", side_effect=AssertionError("LLM must not run")), \
             patch("app._ollama_generate_stream", side_effect=AssertionError("LLM must not run")):
            r = app._proc_io_at(t)
        self.assertEqual(r["writers"][0]["name"], "postgres")


class TestIncidentSpikeAttributionNoLLM(unittest.TestCase):
    """An incident whose members include a disk_io series names the historical
    writer from the ring — with ZERO LLM calls in the context/facts path."""
    def test_incident_facts_name_historical_writer(self):
        now = int(time.time())
        anchor = now - 1800
        _seed_pio([(anchor, 10, "backup_job", 0, 66 * MB)])
        inc = {"id": "inc1", "severity": "warning", "state": "open",
               "opened_at": anchor, "updated_at": anchor,
               "members": [{"series": "disk_io:sda", "direction": "spike",
                            "last_seen": anchor, "active": True,
                            "peak_value": 66.0, "baseline": 3.0, "peak_z": 6.0, "unit": "MB/s"}]}
        with patch("app._ollama_generate", side_effect=AssertionError("LLM must not run")), \
             patch("app._ollama_generate_stream", side_effect=AssertionError("LLM must not run")):
            ctx = app._incident_explain_context(inc, now=now)
            facts = app._incident_explain_facts(ctx)
        self.assertTrue(ctx.get("io_historical"))
        self.assertTrue(any("backup_job" in f and "during the spike" in f for f in facts))

    def test_incident_without_diskio_member_has_no_io_attr(self):
        now = int(time.time())
        _seed_pio([(now - 100, 10, "backup_job", 0, 66 * MB)])
        inc = {"id": "inc2", "severity": "warning", "state": "open",
               "opened_at": now, "updated_at": now,
               "members": [{"series": "gpu_util", "direction": "spike",
                            "last_seen": now, "active": True}]}
        ctx = app._incident_explain_context(inc, now=now)
        self.assertNotIn("io_writers", ctx)         # not joined for non-disk_io incidents


class TestProcIoRingPrivacy(unittest.TestCase):
    """The per-process ring + spike attribution must NEVER reach a public surface."""
    def test_ring_absent_from_public_status(self):
        t = int(time.time())
        _seed_pio([(t, 10, "secret_proc", 0, 99 * MB)])
        pub = app.build_public_status()
        blob = str(pub)
        self.assertNotIn("secret_proc", blob)
        self.assertNotIn("proc_io_samples", blob)
        self.assertNotIn("write_bps", blob)

    def test_build_public_status_does_not_read_ring(self):
        # Even with a poisoned ring the public payload stays leak-free.
        t = int(time.time())
        _seed_pio([(t, 1, "leakybench", 12 * MB, 34 * MB)])
        self.assertNotIn("leakybench", str(app.build_public_status()))


def _mk_incident(iid, opened_at, *members, severity="warning", state="open"):
    """Insert one incident + its members directly (no evaluate/enqueue path).
    members: (series, last_seen) tuples."""
    with app.LOCK:
        app.DB.execute("DELETE FROM incidents WHERE id=?", (iid,))
        app.DB.execute("DELETE FROM incident_members WHERE incident_id=?", (iid,))
        app.DB.execute(
            "INSERT INTO incidents(id,state,severity,opened_at,updated_at,miss)"
            " VALUES(?,?,?,?,?,0)", (iid, state, severity, opened_at, opened_at))
        for series, last_seen in members:
            app.DB.execute(
                "INSERT INTO incident_members(incident_id,series,direction,peak_z,unit,"
                "peak_value,baseline,first_seen,last_seen,active) VALUES(?,?,?,?,?,?,?,?,?,1)",
                (iid, series, "spike", 6.0, "MB/s", 80.0, 5.0, opened_at, last_seen))
        app.DB.commit()


class TestIncidentSpikeIoReadPayload(unittest.TestCase):
    """The deterministic `spike_io` field on the incident *read* payload
    (/api/incidents/<id> → get_incident): present when a disk_io member + history
    exist, gracefully omitted otherwise, comm-only, and NEVER an LLM call."""
    def tearDown(self):
        with app.LOCK:
            app.DB.execute("DELETE FROM incidents")
            app.DB.execute("DELETE FROM incident_members")
            app.DB.execute("DELETE FROM proc_io_samples")
            app.DB.commit()

    def test_helper_names_top_writer_and_reader(self):
        now = int(time.time())
        anchor = now - 300
        _seed_pio([(anchor, 10, "backup_job", 0, 66 * MB),
                   (anchor, 11, "rsync", 9 * MB, 0)])
        inc = {"id": "i1", "opened_at": anchor, "updated_at": anchor,
               "members": [{"series": "disk_io:sda", "last_seen": anchor}]}
        s = app._incident_spike_io(inc, now=now)
        self.assertTrue(s["available"])
        self.assertEqual(s["writer"]["name"], "backup_job")
        self.assertIn("MB/s", s["writer"]["write_h"])
        self.assertEqual(s["reader"]["name"], "rsync")

    def test_helper_none_without_diskio_member(self):
        now = int(time.time())
        _seed_pio([(now, 10, "backup_job", 0, 66 * MB)])
        inc = {"id": "i2", "opened_at": now, "updated_at": now,
               "members": [{"series": "gpu_util", "last_seen": now}]}
        self.assertIsNone(app._incident_spike_io(inc, now=now))

    def test_helper_none_when_no_history_covers_window(self):
        now = int(time.time())
        _seed_pio([(now - 99999, 10, "backup_job", 0, 66 * MB)])  # far outside window
        inc = {"id": "i3", "opened_at": now, "updated_at": now,
               "members": [{"series": "disk_io:sda", "last_seen": now}]}
        self.assertIsNone(app._incident_spike_io(inc, now=now))

    def test_helper_none_on_empty_ring(self):
        now = int(time.time())
        _seed_pio([])
        inc = {"id": "i4", "opened_at": now, "updated_at": now,
               "members": [{"series": "disk_io:sda", "last_seen": now}]}
        self.assertIsNone(app._incident_spike_io(inc, now=now))

    def test_get_incident_carries_spike_io(self):
        now = int(time.time())
        _mk_incident("gi1", now, ("disk_io:sda", now))
        _seed_pio([(now, 10, "postgres", 0, 42 * MB)])
        inc = app.get_incident("gi1")
        self.assertIn("spike_io", inc)
        self.assertEqual(inc["spike_io"]["writer"]["name"], "postgres")

    def test_get_incident_omits_spike_io_without_diskio(self):
        now = int(time.time())
        _mk_incident("gi2", now, ("gpu_util", now))
        _seed_pio([(now, 10, "postgres", 0, 42 * MB)])
        self.assertNotIn("spike_io", app.get_incident("gi2"))

    def test_read_path_makes_zero_llm_calls(self):
        now = int(time.time())
        _mk_incident("gi3", now, ("disk_io:sda", now))
        _seed_pio([(now, 10, "postgres", 0, 42 * MB)])
        with patch("app._ollama_generate", side_effect=AssertionError("LLM must not run")), \
             patch("app._ollama_generate_stream", side_effect=AssertionError("LLM must not run")):
            inc = app.get_incident("gi3")
            _ = app.list_incidents()
        self.assertIn("spike_io", inc)

    def test_api_incident_one_carries_spike_io_and_no_llm(self):
        now = int(time.time())
        _mk_incident("gi4", now, ("disk_io:sda", now))
        _seed_pio([(now, 10, "kworker", 0, 55 * MB)])
        app.app.config["TESTING"] = True
        c = app.app.test_client()
        with patch("app._ollama_generate", side_effect=AssertionError("LLM must not run")), \
             patch("app._ollama_generate_stream", side_effect=AssertionError("LLM must not run")):
            r = c.get("/api/incidents/gi4")
        j = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(j["incident"]["spike_io"]["writer"]["name"], "kworker")

    def test_spike_io_comm_absent_from_public_status(self):
        now = int(time.time())
        _mk_incident("gi5", now, ("disk_io:sda", now))
        _seed_pio([(now, 10, "secret_writer", 0, 77 * MB)])
        # the field surfaces on the authed read...
        self.assertEqual(app.get_incident("gi5")["spike_io"]["writer"]["name"], "secret_writer")
        # ...but NEVER on any public surface
        self.assertNotIn("secret_writer", str(app.build_public_status()))


if __name__ == "__main__":
    unittest.main()
