"""GPU telemetry loss — the case a driver auto-update creates.

The incident this pins down: a package update on a GPU box replaced the NVIDIA
userspace libraries under a still-loaded kernel module, nvidia-smi started
dying with "Driver/library version mismatch", the probe returned an empty card
list, and the dashboard showed the last numbers it had for a day with not a
single red pixel. The owner noticed only because the temperature didn't move
while the cards were visibly working.

Three things have to be true for that to never happen again:

  1. The probe distinguishes "no GPU" from "GPU tool broken" (gpu_error).
  2. Every host-level surface gets a verdict (gpu_telemetry on the fleet row,
     telemetry + a `lost` card status on the cockpit) — the light.
  3. The notifier raises ONE host-level alert when every card vanishes at
     once, instead of silently dropping the host from the scan.

Plus the second bug found on the same day: every host's poll period was
(slowest probe + INTERVAL), so a 40 s Windows probe made a 4 s Linux box
refresh every 50 s. Each host now rides its own cadence.
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import probe


def _res(rc=0, out="", err=""):
    r = mock.Mock()
    r.returncode = rc
    r.stdout = out.encode()
    r.stderr = err.encode()
    return r


MISMATCH = "Failed to initialize NVML: Driver/library version mismatch"


# ── 1. the probe's diagnosis ─────────────────────────────────────────────────

class TestProbeDiagnosis(unittest.TestCase):
    def test_a_box_with_no_nvidia_stack_at_all_is_not_an_error(self):
        with mock.patch("probe._which", return_value=None), \
             mock.patch("probe.os.path.exists", return_value=False), \
             mock.patch("probe.glob.glob", return_value=[]):
            self.assertIsNone(probe._nvidia_failure())

    def test_driver_loaded_but_tool_missing_is_named(self):
        with mock.patch("probe._which", return_value=None), \
             mock.patch("probe.os.path.exists", side_effect=lambda p: p == "/proc/driver/nvidia"):
            msg = probe._nvidia_failure()
        self.assertIn("nvidia-smi is not installed", msg)

    def test_the_tools_own_error_line_is_quoted(self):
        # The exact failure from the incident. nvidia-smi prints it and exits
        # non-zero; the human should see that line, not a paraphrase.
        with mock.patch("probe._which", return_value="/usr/bin/nvidia-smi"), \
             mock.patch("probe.subprocess.run", return_value=_res(rc=255, out=MISMATCH + "\n")):
            msg = probe._nvidia_failure()
        self.assertIn(MISMATCH, msg)

    def test_a_hung_tool_is_reported_as_a_timeout(self):
        import subprocess
        with mock.patch("probe._which", return_value="/usr/bin/nvidia-smi"), \
             mock.patch("probe.subprocess.run", side_effect=subprocess.TimeoutExpired("nvidia-smi", 3)):
            msg = probe._nvidia_failure()
        self.assertIn("timed out", msg)

    def test_a_working_tool_is_no_failure(self):
        with mock.patch("probe._which", return_value="/usr/bin/nvidia-smi"), \
             mock.patch("probe.subprocess.run", return_value=_res(out="GPU 0: NVIDIA GeForce RTX 3090 (UUID: GPU-x)\n")):
            self.assertIsNone(probe._nvidia_failure())

    def test_read_gpu_emits_gpu_error_not_an_empty_dict(self):
        # No cards from either vendor, but the NVIDIA stack is there and broken:
        # the probe must say so rather than "no GPU on this host".
        with mock.patch("probe._nvidia_cards", return_value=[]), \
             mock.patch("probe._amd_gpu_sysfs", return_value=[]), \
             mock.patch("probe._nvidia_failure", return_value="nvidia-smi failed (exit 255): " + MISMATCH):
            out = probe.read_gpu()
        self.assertNotIn("gpus", out)
        self.assertIn(MISMATCH, out["gpu_error"])

    def test_read_gpu_stays_empty_on_a_genuinely_gpu_less_box(self):
        with mock.patch("probe._nvidia_cards", return_value=[]), \
             mock.patch("probe._amd_gpu_sysfs", return_value=[]), \
             mock.patch("probe._nvidia_failure", return_value=None):
            self.assertEqual(probe.read_gpu(), {})


# ── 2. the hub's verdict and the cockpit's `lost` state ──────────────────────

def _card(idx=0, **kw):
    g = {"idx": idx, "name": "NVIDIA GeForce RTX 3090", "vendor": "nvidia",
         "util": 50, "mem_used": 12000, "mem_total": 24576, "power": 200, "temp": 70}
    g.update(kw)
    return g


def _seed(host, cards_at):
    import app
    from backend.db.repos import gpu_samples as repo
    for ts, cards in cards_at:
        repo.record(app.DB, ts, host, cards, interval=10)
    app.DB.commit()


def _hostdata(host, hostd, online=True):
    import app
    return mock.patch.dict(
        app.HOST_DATA,
        {host: {"data": {"host": hostd}, "at": int(time.time()) - (0 if online else 10 ** 6), "fails": 0}},
        clear=True)


class _HubCase(unittest.TestCase):
    def setUp(self):
        import app
        app.app.config["TESTING"] = True
        self.app = app
        self.c = app.app.test_client()
        for t in ("gpu_samples", "gpu_samples_1h"):
            app.DB.execute(f"DELETE FROM {t}")
        app.DB.commit()
        app._GPU_LAST_CARDS.clear()


class TestVerdict(_HubCase):
    def test_cards_present_means_nothing_to_say(self):
        self.app.note_gpu_cards("vader", {"gpus": [_card()]})
        self.assertIsNone(self.app.gpu_telemetry("vader", {"gpus": [_card()]}))

    def test_a_box_that_never_had_a_gpu_is_silent(self):
        self.app.note_gpu_cards("pi", {})
        self.assertIsNone(self.app.gpu_telemetry("pi", {}))

    def test_cards_that_were_there_and_are_not_now_are_lost(self):
        self.app.note_gpu_cards("vader", {"gpus": [_card()]})
        self.app.note_gpu_cards("vader", {})
        v = self.app.gpu_telemetry("vader", {})
        self.assertEqual(v["ok"], False)
        self.assertIsNone(v["error"])
        self.assertGreater(v["lost_since"], 0)

    def test_the_probes_error_is_carried_even_without_a_roster(self):
        v = self.app.gpu_telemetry("vader", {"gpu_error": MISMATCH})
        self.assertEqual(v["error"], MISMATCH)

    def test_a_hub_restart_mid_incident_remembers_the_cards_from_history(self):
        # Nothing in memory yet, but the DB saw three cards ten minutes ago:
        # the first poll after a restart must not conclude "never had a GPU".
        now = int(time.time())
        _seed("vader", [(now - 600, [_card(0), _card(1), _card(2)])])
        self.app.note_gpu_cards("vader", {})
        v = self.app.gpu_telemetry("vader", {})
        self.assertIsNotNone(v)
        self.assertEqual(v["lost_since"], now - 600)

    def test_cards_removed_long_ago_stop_being_lost(self):
        now = int(time.time())
        _seed("old", [(now - 2 * self.app.GPU_LOST_FORGET_S, [_card(0)])])
        self.app.note_gpu_cards("old", {})
        self.assertIsNone(self.app.gpu_telemetry("old", {}))

    def test_the_fleet_row_carries_the_verdict(self):
        import app
        with mock.patch.object(app, "list_hosts", return_value=[{"name": "vader", "ssh_target": "u@h", "last_check": None}]), \
             _hostdata("vader", {"gpu_error": MISMATCH}):
            rows = {r["name"]: r for r in app.fleet_payload()["hosts"]}
        self.assertEqual(rows["vader"]["gpu_telemetry"]["error"], MISMATCH)
        self.assertIn("gpu_telemetry", rows["local"])


class TestCockpitLostState(_HubCase):
    def test_host_answering_without_its_cards_is_lost_not_stale(self):
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0), _card(1)]) for t in range(300, 0, -10)])
        self.app.note_gpu_cards("vader", {"gpus": [_card(0), _card(1)]})
        with _hostdata("vader", {"gpu_error": "nvidia-smi failed (exit 255): " + MISMATCH}):
            d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        self.assertTrue(d["has_gpu"])          # the cards are still drawn…
        self.assertEqual({c["status"] for c in d["cards"]}, {"lost"})   # …but flagged
        self.assertIsNone(d["now_pooled"])
        self.assertTrue(d["telemetry"]["lost"])
        self.assertIn(MISMATCH, d["telemetry"]["error"])

    def test_lost_survives_the_two_minute_recent_window(self):
        # `gone` decays to `retired` after a couple of polls; a broken driver
        # stays broken for hours and the light must stay on the whole time.
        now = int(time.time())
        _seed("vader", [(now - 1800, [_card(0)])])
        self.app.note_gpu_cards("vader", {"gpus": [_card(0)]})
        self.app._GPU_LAST_CARDS["vader"] = now - 1800
        with _hostdata("vader", {}):
            d = self.c.get("/api/gpu/history?host=vader&range=6h").get_json()
        self.assertEqual(d["cards"][0]["status"], "lost")

    def test_an_offline_host_is_still_stale(self):
        # Offline is the host's problem, not the driver's — calm, as before.
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0)]) for t in range(60, 0, -10)])
        self.app.note_gpu_cards("vader", {"gpus": [_card(0)]})
        with _hostdata("vader", {}, online=False):
            d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        self.assertEqual(d["cards"][0]["status"], "stale")
        self.assertFalse(d["telemetry"]["lost"])

    def test_a_healthy_host_has_no_telemetry_block(self):
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0)]) for t in range(60, 0, -10)])
        with _hostdata("vader", {"gpus": [_card(0)]}):
            d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        self.assertIsNone(d["telemetry"])
        self.assertEqual(d["cards"][0]["status"], "busy")

    def test_a_broken_tool_on_a_box_with_no_history_is_named_not_no_gpu(self):
        with _hostdata("newbox", {"gpu_error": MISMATCH}):
            d = self.c.get("/api/gpu/history?host=newbox&range=1h").get_json()
        self.assertFalse(d["has_gpu"])
        self.assertEqual(d["telemetry"]["error"], MISMATCH)


# ── 3. one host-level alert, not three per-card ones (or none) ───────────────

class TestTelemetryAlert(unittest.TestCase):
    def setUp(self):
        import app
        from backend import notify
        self.app, self.notify = app, notify
        notify._GPU_SINCE.clear()
        notify._GPU_ROSTER.clear()
        self.emitted, self.cleared = [], []
        self.settings = {"gpu_temp_alert_c": "84", "gpu_missing_alerts": "1"}
        self.errors = {}

    def run_scan(self, fleet, at):
        with mock.patch.object(self.app, "fleet_gpu_cards", return_value=fleet), \
             mock.patch.object(self.app, "fleet_gpu_errors", return_value=self.errors), \
             mock.patch.object(self.app, "_emit",
                               side_effect=lambda s, k, lvl, t, d, rules=None: self.emitted.append((k, lvl, t, d))), \
             mock.patch.object(self.app, "_clear", side_effect=lambda k: self.cleared.append(k)), \
             mock.patch.object(self.notify.time, "time", return_value=at):
            self.notify.notify_gpu_cards(self.settings, None)

    def sustain(self, fleet, t0, seconds, step=20):
        for t in range(t0, t0 + seconds + 1, step):
            self.run_scan(fleet, t)

    def keys(self):
        return [e[0] for e in self.emitted]

    def test_every_card_vanishing_at_once_raises_one_host_alert(self):
        three = [("vader", [_card(0), _card(1), _card(2)], True)]
        none = [("vader", [], True)]           # what a dead nvidia-smi produces
        self.run_scan(three, 1000)
        self.sustain(none, 1020, 200)
        # (_emit is mocked, so the same key repeats per scan; real _emit is
        # edge-triggered and fires it once.)
        self.assertEqual({k for k in self.keys() if "telemetry" in k}, {"gpu:telemetry:vader"})
        self.assertEqual([k for k in self.keys() if "missing" in k], [])

    def test_the_alert_quotes_the_tools_error_and_the_card_count(self):
        self.errors = {"vader": "nvidia-smi failed (exit 255): " + MISMATCH}
        self.run_scan([("vader", [_card(0), _card(1)], True)], 1000)
        self.sustain([("vader", [], True)], 1020, 200)
        _k, lvl, title, detail = next(e for e in self.emitted if e[0] == "gpu:telemetry:vader")
        self.assertEqual(lvl, "critical")
        self.assertIn("vader", title)
        self.assertIn(MISMATCH, detail)
        self.assertIn("2 cards", detail)

    def test_one_empty_poll_is_not_a_dead_driver(self):
        three = [("vader", [_card(0), _card(1), _card(2)], True)]
        self.run_scan(three, 1000)
        self.run_scan([("vader", [], True)], 1020)     # nvidia-smi timed out once
        self.run_scan(three, 1040)
        self.assertEqual(self.keys(), [])

    def test_a_box_that_never_had_a_gpu_is_silent(self):
        self.sustain([("pi", [], True)], 1000, 600)
        self.assertEqual(self.keys(), [])

    def test_a_broken_tool_alerts_even_with_no_roster(self):
        # Hub restarted while the driver was already broken: no card was ever
        # seen in this process, but the probe says the tool is dead. Still an
        # incident.
        self.errors = {"vader": MISMATCH}
        self.sustain([("vader", [], True)], 1000, 200)
        self.assertIn("gpu:telemetry:vader", self.keys())

    def test_cards_coming_back_clears_the_alert(self):
        two = [("vader", [_card(0), _card(1)], True)]
        self.run_scan(two, 1000)
        self.sustain([("vader", [], True)], 1020, 200)
        self.cleared.clear()
        self.run_scan(two, 1300)
        self.assertIn("gpu:telemetry:vader", self.cleared)

    def test_cards_removed_for_good_stop_alerting_after_an_hour(self):
        self.run_scan([("vader", [_card(0)], True)], 1000)
        self.sustain([("vader", [], True)], 1020, 200)
        self.cleared.clear()
        self.run_scan([("vader", [], True)], 1000 + 3700)
        self.assertIn("gpu:telemetry:vader", self.cleared)
        self.assertEqual(self.notify._GPU_ROSTER.get("vader"), {})

    def test_an_offline_host_never_raises_it(self):
        self.run_scan([("vader", [_card(0)], True)], 1000)
        self.sustain([("vader", [], False)], 1020, 600)
        self.assertEqual(self.keys(), [])

    def test_the_hub_itself_is_covered(self):
        self.run_scan([("local", [_card(0)], True)], 1000)
        self.sustain([("local", [], True)], 1020, 200)
        self.assertIn("gpu:telemetry:local", self.keys())


class TestFleetAccessorIncludesEmptyHosts(unittest.TestCase):
    def test_a_host_with_data_but_no_cards_is_in_the_scan(self):
        # The hole the incident fell through: hosts with an empty card list were
        # dropped from the accessor, so losing ALL cards at once was invisible
        # to the very check meant to catch a card going missing.
        import app
        with _hostdata("vader", {"gpu_error": MISMATCH}):
            fleet = dict((h, (cards, online)) for h, cards, online in app.fleet_gpu_cards())
            errors = app.fleet_gpu_errors()
        self.assertIn("vader", fleet)
        self.assertEqual(fleet["vader"][0], [])
        self.assertEqual(errors.get("vader"), MISMATCH)


# ── the poller: every host on its own clock ──────────────────────────────────

class TestPerHostCadence(unittest.TestCase):
    def test_a_slow_host_does_not_slow_a_fast_one(self):
        import app
        from backend import collectors
        counts = {"fast": 0, "slow": 0}
        lock = threading.Lock()
        # collectors.time IS the time module: patching sleep there patches it
        # everywhere, so the test holds the original to do its own waiting.
        real_sleep = time.sleep

        def fake_poll(h):
            with lock:
                counts[h["name"]] += 1
            if h["name"] == "slow":
                real_sleep(0.5)

        hosts = [{"name": "fast"}, {"name": "slow"}]
        with mock.patch.object(app, "INTERVAL", 0.05), \
             mock.patch.object(app, "list_hosts", return_value=hosts), \
             mock.patch.object(app, "_poll_one_host", side_effect=fake_poll), \
             mock.patch.object(collectors.time, "sleep", side_effect=lambda s: real_sleep(min(s, 0.05))):
            stop = threading.Event()
            t = threading.Thread(target=collectors.host_poller, args=(stop,), daemon=True)
            t.start()
            real_sleep(1.2)
            stop.set()
            t.join(2)
        with lock:
            fast, slow = counts["fast"], counts["slow"]
        # Old shape: both hosts polled once per (0.5 s + interval) ≈ 2 times.
        # New shape: the fast host keeps its own ~50 ms cadence regardless.
        self.assertGreaterEqual(fast, 8, (fast, slow))
        self.assertLessEqual(slow, 4, (fast, slow))


if __name__ == "__main__":
    unittest.main()
