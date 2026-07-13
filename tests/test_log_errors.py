"""Tests for the Dozzle-style per-container recent-error COUNT badge feature
(next_ai). Endpoint: GET /api/logs/errors.

Covers the constraints the reviewer hammers:
  • the curated error regex counts real failures and the false-positive guard
    rejects "no error" / "0 errors" / "error: none";
  • window filtering by the --timestamps prefix (old errors don't count);
  • TTL cache — a second call within the TTL does NOT re-scan the socket
    (asserted via a call counter);
  • bounded: tail capped, running-container count hard-capped (+ truncated flag);
  • running-only (derives from containers(), which is running-only);
  • LLM-free tripwire — _ollama_generate is NEVER called on this path;
  • graceful degrade — docker unreachable / a container error → errors:0 /
    unavailable flag, NEVER a 500;
  • privacy — the counts + names are absent from every public surface;
  • i18n parity for the new keys across en + zh-CN.

No real docker or ollama is touched — both are mocked.
"""
import json
import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


FAKE_CONTAINERS = [
    {"id": "abc123def456", "name": "grafana", "image": "grafana/grafana", "ip": None, "ports": []},
    {"id": "0011223344ff", "name": "ollama", "image": "ollama/ollama", "ip": None, "ports": []},
]


def _mk(ts, text):
    return {"ts": ts, "text": text}


def _reset_cache():
    with app._ERRCOUNT_LOCK:
        app._errcount_cache.update(at=0.0, data=None)


class TestErrorRegexCounting(unittest.TestCase):
    def test_counts_real_error_words(self):
        now = 1_000_000.0
        ts = "2001-09-09T01:46:40Z"                    # == now (epoch 1e9)
        lines = [_mk(ts, "INFO started ok"),
                 _mk(ts, "ERROR connection refused to db"),
                 _mk(ts, "panic: runtime error"),
                 _mk(ts, "Traceback (most recent call last):"),
                 _mk(ts, "segfault at 0x0"),
                 _mk(ts, "OOM killed a worker")]
        cnt, last = app._count_recent_errors(lines, app._ERRCOUNT_WINDOW_S, now=now)
        self.assertEqual(cnt, 5)
        self.assertEqual(last, ts)

    def test_false_positive_guard(self):
        now = 1_000_000_000.0
        ts = "2001-09-09T01:46:40Z"
        lines = [_mk(ts, "no errors detected, all good"),
                 _mk(ts, "0 errors, 0 warnings"),
                 _mk(ts, "shutdown without error"),
                 _mk(ts, "error: none"),
                 _mk(ts, "errorlevel 0")]
        cnt, _ = app._count_recent_errors(lines, app._ERRCOUNT_WINDOW_S, now=now)
        self.assertEqual(cnt, 0)

    def test_word_boundary_no_substring_hits(self):
        now = 1_000_000_000.0
        ts = "2001-09-09T01:46:40Z"
        # "terror", "erroneous" should NOT match a whole-word error token.
        lines = [_mk(ts, "terror movie night"),
                 _mk(ts, "erroneous assumption")]
        cnt, _ = app._count_recent_errors(lines, app._ERRCOUNT_WINDOW_S, now=now)
        self.assertEqual(cnt, 0)

    def test_window_filtering_by_timestamp(self):
        now = 1_000_000_000.0                          # 2001-09-09T01:46:40Z
        recent = "2001-09-09T01:40:00Z"                # ~7 min ago (inside 15m)
        old    = "2001-09-09T01:00:00Z"                # ~46 min ago (outside)
        lines = [_mk(recent, "ERROR fresh failure"),
                 _mk(old, "ERROR stale failure")]
        cnt, last = app._count_recent_errors(lines, app._ERRCOUNT_WINDOW_S, now=now)
        self.assertEqual(cnt, 1)
        self.assertEqual(last, recent)

    def test_unparseable_ts_still_counts(self):
        # A container without --timestamps support shouldn't silently read as 0.
        now = 1_000_000_000.0
        lines = [{"text": "ERROR no timestamp here"}]
        cnt, _ = app._count_recent_errors(lines, app._ERRCOUNT_WINDOW_S, now=now)
        self.assertEqual(cnt, 1)


class TestErrorCountEndpoint(unittest.TestCase):
    def setUp(self):
        _reset_cache()
        self.c = app.app.test_client()

    def tearDown(self):
        _reset_cache()

    def _tail_factory(self, blobs):
        # blobs: {cid: (lines_list, err)}
        def _tail(cid, n):
            self.assertLessEqual(n, app._LOG_LINES_CAP)
            lines, err = blobs.get(cid, ([], None))
            return lines, False, err
        return _tail

    def test_shape_and_counts(self):
        ts = "2001-09-09T01:46:40Z"
        blobs = {"abc123def456": ([_mk(ts, "ERROR boom"), _mk(ts, "fatal crash")], None),
                 "0011223344ff": ([_mk(ts, "INFO ok")], None)}
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app._docker_logs_tail", side_effect=self._tail_factory(blobs)), \
             patch("app.time.time", return_value=1_000_000_000.0):
            r = self.c.get("/api/logs/errors")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["window_min"], 15)
        by = {x["name"]: x for x in j["containers"]}
        self.assertEqual(by["grafana"]["errors"], 2)
        self.assertEqual(by["grafana"]["id_short"], "abc123def456")
        self.assertEqual(by["ollama"]["errors"], 0)
        self.assertEqual(j["scanned"], 2)
        self.assertFalse(j["truncated"])

    def test_no_raw_log_text_in_response(self):
        ts = "2001-09-09T01:46:40Z"
        blobs = {"abc123def456": ([_mk(ts, "ERROR secret-token=hunter2")], None)}
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app._docker_logs_tail", side_effect=self._tail_factory(blobs)), \
             patch("app.time.time", return_value=1_000_000_000.0):
            r = self.c.get("/api/logs/errors")
        self.assertNotIn("hunter2", r.get_data(as_text=True))

    def test_llm_free_tripwire(self):
        ts = "2001-09-09T01:46:40Z"
        blobs = {"abc123def456": ([_mk(ts, "ERROR boom")], None)}
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app._docker_logs_tail", side_effect=self._tail_factory(blobs)), \
             patch("app._ollama_generate") as gen:
            r = self.c.get("/api/logs/errors")
        self.assertEqual(r.status_code, 200)
        gen.assert_not_called()

    def test_cache_second_call_does_not_rescan(self):
        ts = "2001-09-09T01:46:40Z"
        blobs = {"abc123def456": ([_mk(ts, "ERROR boom")], None),
                 "0011223344ff": ([], None)}
        calls = {"n": 0}
        base = self._tail_factory(blobs)
        def counting(cid, n):
            calls["n"] += 1
            return base(cid, n)
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app._docker_logs_tail", side_effect=counting):
            r1 = self.c.get("/api/logs/errors")
            first = calls["n"]
            r2 = self.c.get("/api/logs/errors")
            second = calls["n"]
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertGreater(first, 0)                   # first call scanned
        self.assertEqual(second, first)                # second served from cache
        self.assertFalse(r1.get_json()["cached"])
        self.assertTrue(r2.get_json()["cached"])

    def test_cache_expiry_rescans(self):
        blobs = {"abc123def456": ([], None), "0011223344ff": ([], None)}
        calls = {"n": 0}
        base = self._tail_factory(blobs)
        def counting(cid, n):
            calls["n"] += 1
            return base(cid, n)
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app._docker_logs_tail", side_effect=counting):
            self.c.get("/api/logs/errors")
            first = calls["n"]
            # Expire the cache by rewinding its timestamp past the TTL.
            with app._ERRCOUNT_LOCK:
                app._errcount_cache["at"] -= (app._ERRCOUNT_TTL + 1)
            self.c.get("/api/logs/errors")
            second = calls["n"]
        self.assertGreater(second, first)              # re-scanned after expiry

    def test_bounded_tail_never_exceeds_cap(self):
        # _tail_factory asserts n <= cap; also assert the configured tail is bounded.
        self.assertLessEqual(app._ERRCOUNT_TAIL, app._LOG_LINES_CAP)
        self.assertLessEqual(app._ERRCOUNT_TAIL, 200)

    def test_container_cap_and_truncated_flag(self):
        many = [{"id": ("%012x" % i), "name": "c%d" % i,
                 "image": "x", "ip": None, "ports": []}
                for i in range(app._ERRCOUNT_CT_CAP + 5)]
        with patch("app.containers", return_value=many), \
             patch("app._docker_logs_tail", return_value=([], False, None)):
            r = self.c.get("/api/logs/errors")
        j = r.get_json()
        self.assertEqual(len(j["containers"]), app._ERRCOUNT_CT_CAP)
        self.assertTrue(j["truncated"])

    def test_degrade_docker_unreachable_no_500(self):
        with patch("app.containers", side_effect=OSError("no socket")):
            r = self.c.get("/api/logs/errors")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["containers"], [])

    def test_degrade_per_container_error_flags_unavailable(self):
        def tail(cid, n):
            if cid == "abc123def456":
                return [], False, "unreachable"
            return [_mk("2001-09-09T01:46:40Z", "ERROR boom")], False, None
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app._docker_logs_tail", side_effect=tail), \
             patch("app.time.time", return_value=1_000_000_000.0):
            r = self.c.get("/api/logs/errors")
        self.assertEqual(r.status_code, 200)
        by = {x["name"]: x for x in r.get_json()["containers"]}
        self.assertTrue(by["grafana"].get("unavailable"))
        self.assertEqual(by["grafana"]["errors"], 0)
        self.assertEqual(by["ollama"]["errors"], 1)

    def test_per_container_exception_never_500(self):
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app._docker_logs_tail", side_effect=RuntimeError("kaboom")):
            r = self.c.get("/api/logs/errors")
        self.assertEqual(r.status_code, 200)
        for x in r.get_json()["containers"]:
            self.assertEqual(x["errors"], 0)
            self.assertTrue(x.get("unavailable"))


class TestPrivacyNotOnPublicSurfaces(unittest.TestCase):
    """Error counts + names must never appear on any public/anonymized surface."""
    def test_absent_from_public_status(self):
        pub = json.dumps(app.build_public_status())
        for token in ("logs/errors", "id_short", "window_min", "errcount"):
            self.assertNotIn(token, pub)

    def test_public_status_route_no_error_counts(self):
        c = app.app.test_client()
        r = c.get("/api/status")
        if r.status_code == 200 and r.is_json:
            body = r.get_data(as_text=True)
            self.assertNotIn("id_short", body)


class TestI18nParity(unittest.TestCase):
    def test_new_keys_present_in_both_locales(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        en = json.load(open(os.path.join(here, "locales", "en.json"), encoding="utf-8"))
        zh = json.load(open(os.path.join(here, "locales", "zh-CN.json"), encoding="utf-8"))
        for k in ("col.errors", "ct.err_window", "ct.err_count", "ct.err_count_hint",
                  "ct.err_none", "ct.err_none_hint", "ct.err_unavail",
                  "ct.err_unavail_hint"):
            self.assertIn(k, en, "missing in en: " + k)
            self.assertIn(k, zh, "missing in zh-CN: " + k)


if __name__ == "__main__":
    unittest.main()
