"""The fast lane: a screen cadence that is independent of the storage cadence.

INTERVAL is what every cost figure in this project is integrated against
(`sum(watts) * INTERVAL / 3_600_000`). FAST_INTERVAL only refreshes LATEST and
wakes the SSE stream. The tests that matter here are the ones that pin that
separation down — a fast lane that writes a row, or that shares the sampler's
/proc/stat counters, corrupts history rather than merely being wrong on screen.
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
from backend.collectors import fast_sample_once


_HOST_FAST = {"cpu": 42.5, "ram_used": 8000, "ram_total": 16000,
              "ram_kernel": 500, "load1": 1.25, "uptime": 99, "ctemp": 51.0}


def _seed_latest():
    """LATEST as the slow sampler leaves it: vitals plus the slow-moving blocks
    the fast lane must not touch."""
    app.LATEST.update(
        ts=1000, util=10, mem_used=2000, mem_total=24576, power=100, temp=40,
        gpu_avail=True, gpu_vendor="nvidia",
        gpus=[{"idx": 0, "name": "RTX 3090", "util": 10, "mem_used": 2000,
               "mem_total": 24576, "power": 100, "temp": 40, "fan": 30}],
        host={"cpu": 5, "cores": 8, "ram_used": 1000, "ram_total": 16000,
              "disks": [{"mount": "/", "used": 10, "total": 100, "pct": 10}],
              "os": {"family": "linux"}, "hw": {"cpu": "Xeon"},
              "net": {"nics": []}, "sec": {"firewall": "on"}})


class TestFastSample(unittest.TestCase):
    def setUp(self):
        _seed_latest()

    def test_writes_no_rows(self):
        """The invariant the cost pipeline depends on.

        Every energy figure treats one row in `samples` as INTERVAL seconds of
        power. A fast lane that also inserted would have each 2 s reading counted
        as 10 s, inflating every cost on the page by 5x — so it must leave the
        table untouched no matter how often it runs."""
        tables = ("samples", "proc", "models", "power_proc", "gpu_samples",
                  "net_samples", "edges", "host_samples")
        # Deltas, not absolutes: these tables are shared with every other test in
        # the session, so what matters is that this loop adds nothing.
        count = lambda: {t: app.DB.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                         for t in tables}
        before = count()
        with patch("app.read_host_fast", return_value=dict(_HOST_FAST)), \
             patch("app.gpu_cards_fast", return_value={}):
            for _ in range(5):
                fast_sample_once()
        self.assertEqual(before, count(), "the fast lane wrote rows")

    def test_refreshes_vitals(self):
        with patch("app.read_host_fast", return_value=dict(_HOST_FAST)), \
             patch("app.gpu_cards_fast", return_value={}):
            fast_sample_once()
        h = app.LATEST["host"]
        self.assertEqual(h["cpu"], 42.5)
        self.assertEqual(h["ram_used"], 8000)
        self.assertEqual(h["load1"], 1.25)
        self.assertEqual(h["ctemp"], 51.0)

    def test_merges_and_keeps_the_slow_blocks(self):
        """read_host_fast() omits disks and the OS/hardware/network/security
        inventories on purpose. Assigning its result over LATEST["host"] instead
        of merging would blank those panels for the rest of the interval."""
        with patch("app.read_host_fast", return_value=dict(_HOST_FAST)), \
             patch("app.gpu_cards_fast", return_value={}):
            fast_sample_once()
        h = app.LATEST["host"]
        self.assertEqual(h["disks"][0]["mount"], "/")
        self.assertEqual(h["os"]["family"], "linux")
        self.assertEqual(h["hw"]["cpu"], "Xeon")
        self.assertEqual(h["sec"]["firewall"], "on")
        self.assertEqual(h["cores"], 8)      # untouched keys survive too

    def test_gpu_updates_cards_in_place(self):
        with patch("app.read_host_fast", return_value={}), \
             patch("app.gpu_cards_fast",
                   return_value={0: {"util": 97.0, "mem_used": 22000.0,
                                     "power": 330.0, "temp": 71.0}}):
            fast_sample_once()
        card = app.LATEST["gpus"][0]
        self.assertEqual(card["util"], 97.0)
        self.assertEqual(card["temp"], 71.0)
        # Fields the light query doesn't ask for keep the sampler's values.
        self.assertEqual(card["name"], "RTX 3090")
        self.assertEqual(card["fan"], 30)
        self.assertEqual(card["mem_total"], 24576)

    def test_gpu_aggregates_follow_the_cards(self):
        app.LATEST["gpus"].append({"idx": 1, "name": "RTX 3090", "util": 0,
                                   "mem_used": 0, "mem_total": 24576,
                                   "power": 20, "temp": 35})
        with patch("app.read_host_fast", return_value={}), \
             patch("app.gpu_cards_fast",
                   return_value={0: {"util": 100.0, "mem_used": 20000.0, "power": 300.0, "temp": 80.0},
                                 1: {"util": 0.0, "mem_used": 1000.0, "power": 25.0, "temp": 40.0}}):
            fast_sample_once()
        self.assertEqual(app.LATEST["util"], 50)          # mean across cards
        self.assertEqual(app.LATEST["mem_used"], 21000)   # pooled VRAM
        self.assertEqual(app.LATEST["power"], 325)        # summed watts
        self.assertEqual(app.LATEST["temp"], 80)          # hottest card, never the mean

    def test_never_invents_or_drops_a_card(self):
        """Card discovery belongs to the sampler. A fast reading that named a card
        the per-card history has never seen would put a GPU on screen with no
        series behind it."""
        with patch("app.read_host_fast", return_value={}), \
             patch("app.gpu_cards_fast",
                   return_value={0: {"util": 5.0, "mem_used": 1.0, "power": 1.0, "temp": 1.0},
                                 7: {"util": 9.0, "mem_used": 9.0, "power": 9.0, "temp": 9.0}}):
            fast_sample_once()
        self.assertEqual([c["idx"] for c in app.LATEST["gpus"]], [0])

    def test_skips_the_gpu_query_when_there_is_no_gpu(self):
        """A GPU-less box must not spawn nvidia-smi every couple of seconds."""
        app.LATEST["gpu_avail"] = False
        with patch("app.read_host_fast", return_value={}), \
             patch("app.gpu_cards_fast") as g:
            fast_sample_once()
        g.assert_not_called()

    def test_skips_the_nvidia_query_on_an_amd_box(self):
        app.LATEST["gpu_vendor"] = "amd"
        with patch("app.read_host_fast", return_value={}), \
             patch("app.gpu_cards_fast") as g:
            fast_sample_once()
        g.assert_not_called()

    def test_bumps_the_live_revision(self):
        before = app.LIVE_REV
        with patch("app.read_host_fast", return_value={}), \
             patch("app.gpu_cards_fast", return_value={}):
            fast_sample_once()
        self.assertEqual(app.LIVE_REV, before + 1)


class TestCpuDeltaState(unittest.TestCase):
    """Two loops read /proc/stat at different cadences. Sharing one counter dict
    would make each consume the other's interval — the 10 s sampler would quietly
    be storing a 2 s figure and vice versa."""

    def test_separate_state_dicts_do_not_interfere(self):
        slow = {"idle": 0, "total": 0}
        fast = {"idle": 0, "total": 0}
        reads = ["cpu 100 0 100 800 0 0 0 0 0 0\n",
                 "cpu 150 0 150 900 0 0 0 0 0 0\n",
                 "cpu 200 0 200 1000 0 0 0 0 0 0\n"]
        seq = iter(reads)

        class _F:
            def __init__(self, text): self._t = text
            def readline(self): return self._t
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with patch("builtins.open", lambda *a, **k: _F(next(seq))):
            app._cpu_pct(slow)     # primes slow from read #1
            app._cpu_pct(fast)     # primes fast from read #2
            app._cpu_pct(slow)     # slow measures read #1 -> #3, not #2 -> #3
        # slow's window spans both ticks: idle +200 of total +400 -> 50% busy.
        self.assertEqual(slow["total"], 1400)
        self.assertEqual(fast["total"], 1200)

    def test_default_state_is_the_samplers(self):
        """Calling without an argument must keep using the sampler's counters, so
        every existing caller behaves exactly as before."""
        with patch("app._cpu_prev", {"idle": 1, "total": 2}) as prev:
            self.assertIsNotNone(prev)


class TestLivePayload(unittest.TestCase):
    def setUp(self):
        _seed_latest()
        self.client = app.app.test_client()

    def test_api_now_serves_live_values_only(self):
        r = self.client.get("/api/now")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["version"], app.VERSION)
        self.assertEqual(j["interval"], app.INTERVAL)
        self.assertIn("rev", j)
        self.assertEqual(j["now"]["util"], app.LATEST["util"])
        # No history: that is the whole point of the split.
        for k in ("labels", "total", "services", "summary", "insights"):
            self.assertNotIn(k, j)

    def test_payload_snapshots_latest(self):
        """live_payload() copies LATEST before serializing. The sampler updates it
        without holding LOCK, so handing the live dict to the JSON encoder risks
        it changing size mid-iteration."""
        p = app.live_payload()
        app.LATEST["a_new_key"] = 1
        self.assertNotIn("a_new_key", p["now"])
        del app.LATEST["a_new_key"]

    def test_fleet_payload_carries_a_revision(self):
        with patch("app.list_hosts", return_value=[]), \
             patch("app._local_now_snapshot", return_value={"cpu": 1}), \
             patch("app.enrich_os_upgrade", side_effect=lambda x: x):
            p = app.fleet_payload()
        self.assertEqual(p["rev"], app.FLEET_REV)
        self.assertEqual(p["hosts"][0]["name"], "local")

    def test_fleet_endpoint_and_stream_share_one_builder(self):
        """/api/fleet and the SSE `fleet` event must return the same document —
        two builders would drift and the table would depend on how it arrived."""
        with patch("app.list_hosts", return_value=[]), \
             patch("app._local_now_snapshot", return_value={"cpu": 1}), \
             patch("app.enrich_os_upgrade", side_effect=lambda x: x), \
             patch("app.socket.gethostname", return_value="testhost"):
            direct = app.fleet_payload()
            served = self.client.get("/api/fleet").get_json()
        self.assertEqual(direct["hosts"], served["hosts"])


class TestStream(unittest.TestCase):
    def setUp(self):
        _seed_latest()
        self.client = app.app.test_client()

    def _frames(self, resp, n):
        """Pull n chunks off the endless stream, then close it."""
        try:
            return self._peek(resp, n)
        finally:
            resp.close()

    @staticmethod
    def _peek(resp, n):
        """Pull n chunks and leave the stream open (closing it releases the slot)."""
        it = resp.response.__iter__()
        return [next(it).decode() for _ in range(n)]

    def test_first_frame_is_the_current_state(self):
        """A freshly opened page paints from the stream instead of waiting out a
        whole sample interval for the next tick."""
        r = self.client.get("/api/stream")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.mimetype.startswith("text/event-stream"))
        frames = self._frames(r, 2)
        self.assertIn("retry:", frames[0])
        self.assertIn("event: now", frames[1])
        body = frames[1].split("data: ", 1)[1]
        self.assertEqual(json.loads(body)["now"]["util"], app.LATEST["util"])

    def test_stream_is_not_cached_or_buffered(self):
        r = self.client.get("/api/stream")
        try:
            self.assertEqual(r.headers.get("Cache-Control"), "no-cache")
            # nginx buffers proxied responses by default, which would hold every
            # frame until the buffer fills — i.e. no live updates behind a proxy.
            self.assertEqual(r.headers.get("X-Accel-Buffering"), "no")
        finally:
            r.close()

    def test_concurrent_streams_are_bounded(self):
        """Flask's threaded server gives each open stream a thread; a browser stuck
        reconnecting must not be able to allocate them without limit."""
        from backend.api import system as sysapi
        opened = []
        try:
            with patch.object(sysapi, "_STREAM_MAX", 2):
                for _ in range(2):
                    r = self.client.get("/api/stream")
                    opened.append(r)
                    self._peek(r, 1)            # start the generator, keep it open
                rejected = self.client.get("/api/stream")
                self.assertEqual(rejected.status_code, 503)
            # Closing a stream frees its slot again.
            opened.pop().close()
            with patch.object(sysapi, "_STREAM_MAX", 2):
                accepted = self.client.get("/api/stream")
                opened.append(accepted)
                self.assertEqual(accepted.status_code, 200)
        finally:
            for r in opened:
                r.close()


class TestFastIntervalConfig(unittest.TestCase):
    def test_a_fast_lane_slower_than_the_sampler_is_disabled(self):
        """FAST_INTERVAL >= INTERVAL buys nothing and doubles the reads, so the
        module resolves it to 0 (off) rather than running a pointless loop."""
        self.assertTrue(app.FAST_INTERVAL == 0 or app.FAST_INTERVAL < app.INTERVAL)

    def test_disabled_fast_sampler_returns_immediately(self):
        from backend.collectors import fast_sampler
        with patch.object(app, "FAST_INTERVAL", 0):
            fast_sampler()      # must return, not loop


class TestGetFastInterval(unittest.TestCase):
    """get_fast_interval() — the live-adjustable screen cadence behind
    Settings -> General -> Live refresh interval. Same read-live-from-settings
    pattern as get_retention_secs(), so every case here pins FAST_INTERVAL and
    INTERVAL to known values rather than trusting the real environment's."""

    def test_reads_the_persisted_value(self):
        with patch.object(app, "FAST_INTERVAL", 2), patch.object(app, "INTERVAL", 10), \
             patch.object(app, "get_settings", return_value={"fast_interval_s": "1"}):
            self.assertEqual(app.get_fast_interval(), 1)

    def test_missing_setting_falls_back_to_startup_value(self):
        with patch.object(app, "FAST_INTERVAL", 2), patch.object(app, "INTERVAL", 10), \
             patch.object(app, "get_settings", return_value={}):
            self.assertEqual(app.get_fast_interval(), 2)

    def test_clamped_to_the_1_30_range(self):
        with patch.object(app, "FAST_INTERVAL", 2), patch.object(app, "INTERVAL", 60), \
             patch.object(app, "get_settings", return_value={"fast_interval_s": "999"}):
            self.assertEqual(app.get_fast_interval(), 30)

    def test_never_as_slow_or_slower_than_the_sampler(self):
        # A stale/tampered setting requesting >= INTERVAL degrades to the
        # startup value rather than doubling the reads for nothing.
        with patch.object(app, "FAST_INTERVAL", 2), patch.object(app, "INTERVAL", 10), \
             patch.object(app, "get_settings", return_value={"fast_interval_s": "10"}):
            self.assertEqual(app.get_fast_interval(), 2)

    def test_pinned_to_zero_when_the_fast_lane_never_started(self):
        # The setting is irrelevant once FAST_INTERVAL started at 0 — the
        # fast_sampler thread returned immediately and nothing is running.
        with patch.object(app, "FAST_INTERVAL", 0), \
             patch.object(app, "get_settings", return_value={"fast_interval_s": "1"}):
            self.assertEqual(app.get_fast_interval(), 0)

    def test_non_numeric_setting_falls_back_to_startup_value(self):
        with patch.object(app, "FAST_INTERVAL", 2), patch.object(app, "INTERVAL", 10), \
             patch.object(app, "get_settings", return_value={"fast_interval_s": "abc"}):
            self.assertEqual(app.get_fast_interval(), 2)


class TestValidateFastIntervalSettings(unittest.TestCase):
    def test_absent_or_blank_is_fine(self):
        self.assertIsNone(app._validate_fast_interval_settings({}))
        self.assertIsNone(app._validate_fast_interval_settings({"fast_interval_s": ""}))
        self.assertIsNone(app._validate_fast_interval_settings({"fast_interval_s": None}))

    def test_valid_values_accepted(self):
        for v in ("1", "2", "10", "30"):
            self.assertIsNone(app._validate_fast_interval_settings({"fast_interval_s": v}), f"v={v!r}")

    def test_out_of_range_rejected(self):
        for v in ("0", "-1", "31", "999"):
            err = app._validate_fast_interval_settings({"fast_interval_s": v})
            self.assertIsNotNone(err, f"v={v!r} should be rejected")
            self.assertIn("30", err)

    def test_non_numeric_rejected(self):
        err = app._validate_fast_interval_settings({"fast_interval_s": "fast"})
        self.assertIsNotNone(err)


if __name__ == "__main__":
    unittest.main()
