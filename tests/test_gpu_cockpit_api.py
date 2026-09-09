"""The GPU cockpit API — /api/gpu/history and /api/gpu/attribution.

The contract these pin down is "one endpoint serves every host identically".
A remote with three cards and the hub with one must come back in the same shape,
because the dashboard renders both through a single code path; the moment the
payloads differ, the old charts-here / snapshot-there fork grows back.
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _client():
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


def _wipe():
    import app
    for t in ("gpu_samples", "gpu_samples_1h", "proc"):
        app.DB.execute(f"DELETE FROM {t}")
    app.DB.commit()


def _card(idx=0, **kw):
    g = {"idx": idx, "name": "NVIDIA GeForce RTX 3090", "vendor": "nvidia",
         "util": 50, "mem_used": 12000, "mem_total": 24576, "power": 200, "temp": 70}
    g.update(kw)
    return g


def _seed(host, cards_at):
    """cards_at: [(ts, [card, ...]), ...] written straight through the repo."""
    import app
    from backend.db.repos import gpu_samples as repo
    for ts, cards in cards_at:
        repo.record(app.DB, ts, host, cards, interval=10)
    app.DB.commit()


class TestHistoryShape(unittest.TestCase):
    def setUp(self):
        self.c = _client()
        _wipe()

    def test_three_card_remote_returns_one_entry_per_card(self):
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0, temp=80), _card(1, temp=86), _card(2, temp=64, util=0)])
                        for t in range(300, 0, -10)])
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        self.assertEqual(d["host"], "vader")
        self.assertEqual([c["idx"] for c in d["cards"]], [0, 1, 2])
        self.assertTrue(d["has_gpu"])
        self.assertTrue(d["has_history"])

    def test_single_card_hub_has_the_same_shape_as_a_remote(self):
        now = int(time.time())
        _seed("local", [(now - t, [_card(0)]) for t in range(300, 0, -10)])
        _seed("vader", [(now - t, [_card(0), _card(1)]) for t in range(300, 0, -10)])
        a = self.c.get("/api/gpu/history?host=local&range=1h").get_json()
        b = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        self.assertEqual(sorted(a.keys()), sorted(b.keys()))
        self.assertEqual(sorted(a["cards"][0].keys()), sorted(b["cards"][0].keys()))
        self.assertEqual(sorted(a["cards"][0]["series"].keys()),
                         sorted(b["cards"][0]["series"].keys()))

    def test_series_length_matches_labels_for_every_card(self):
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0), _card(1)]) for t in range(600, 0, -10)])
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        n = len(d["labels"])
        for card in d["cards"]:
            for metric, vals in card["series"].items():
                self.assertEqual(len(vals), n, f"{metric} on card {card['idx']}")
        for metric, vals in d["combined"].items():
            self.assertEqual(len(vals), n, metric)

    def test_host_with_no_gpu_is_distinguishable_from_one_with_no_history_yet(self):
        d = self.c.get("/api/gpu/history?host=nosuchhost&range=1h").get_json()
        self.assertFalse(d["has_gpu"])
        self.assertFalse(d["has_history"])
        self.assertEqual(d["cards"], [])


def _live(host, cards):
    """Pretend `host` is online and currently reporting `cards`."""
    import app
    return mock.patch.dict(
        app.HOST_DATA,
        {host: {"data": {"host": {"gpus": cards}}, "at": int(time.time()), "fails": 0}},
        clear=True)


class TestMissingCards(unittest.TestCase):
    """A card missing from the live snapshot has THREE different causes, and
    conflating any two of them produces a false alarm."""

    def setUp(self):
        self.c = _client()
        _wipe()

    def test_a_card_removed_long_ago_is_retired_not_an_incident(self):
        # Found on the live hub: it still had history for a GPU physically taken
        # out of the machine weeks earlier. Reporting that as "fell off the bus"
        # would leave a permanent false critical after any hardware change.
        old = int(time.time()) - 7 * 86400
        _seed("vader", [(old + t, [_card(0), _card(1)]) for t in range(0, 300, 10)])
        with _live("vader", [_card(0)]):     # host IS reporting, card 1 is not
            d = self.c.get("/api/gpu/history?host=vader&range=all").get_json()
        by_idx = {c["idx"]: c["status"] for c in d["cards"]}
        self.assertEqual(by_idx[1], "retired")

    def test_a_card_that_stopped_reporting_just_now_is_gone(self):
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0), _card(1)]) for t in range(60, 0, -10)])
        with _live("vader", [_card(0)]):     # card 1 vanished from a live list
            d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        by_idx = {c["idx"]: c["status"] for c in d["cards"]}
        self.assertEqual(by_idx[1], "gone")

    def test_no_live_snapshot_at_all_is_stale_not_a_fleet_of_dead_cards(self):
        # Found by rendering the real page right after a container restart:
        # HOST_DATA is empty until the first successful poll, so EVERY card read
        # as "fell off the bus" and the tab lit up with three criticals. A card
        # missing from a live list that doesn't exist yet is not an incident.
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0), _card(1), _card(2)]) for t in range(60, 0, -10)])
        import app
        with mock.patch.dict(app.HOST_DATA, {}, clear=True):
            d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        self.assertTrue(all(c["status"] == "stale" for c in d["cards"]),
                        [c["status"] for c in d["cards"]])

    def test_last_seen_is_reported_so_the_ui_can_say_when(self):
        now = int(time.time())
        _seed("vader", [(now - 100, [_card(0)])])
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        self.assertEqual(d["cards"][0]["last_seen"], now - 100)


class TestSupportsMap(unittest.TestCase):
    def setUp(self):
        self.c = _client()
        _wipe()

    def test_unreported_fan_is_advertised_unsupported_not_zero(self):
        # The regression this guards: serialising fan as 0 for a passively cooled
        # card would draw a confident flat line at zero and, worse, look exactly
        # like a stalled fan.
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0)]) for t in range(300, 0, -10)])   # no fan key
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        card = d["cards"][0]
        self.assertFalse(card["supports"]["fan"])
        self.assertTrue(all(v is None for v in card["series"]["fan"]))

    def test_reported_fan_is_supported(self):
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0, fan=62)]) for t in range(300, 0, -10)])
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        self.assertTrue(d["cards"][0]["supports"]["fan"])

    def test_unreported_memory_temp_is_advertised_unsupported(self):
        # Plain-GDDR6 and older cards have no memory-junction sensor at all. The
        # row has to say "not reported" rather than draw a cool flat zero next to
        # a core temp of 80 °C.
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0)]) for t in range(300, 0, -10)])
        card = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()["cards"][0]
        self.assertFalse(card["supports"]["temp_mem"])
        self.assertTrue(all(v is None for v in card["series"]["temp_mem"]))

    def test_reported_memory_temp_is_supported_and_charted(self):
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0, temp=70, temp_mem=96)])
                        for t in range(300, 0, -10)])
        card = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()["cards"][0]
        self.assertTrue(card["supports"]["temp_mem"])
        self.assertEqual(card["series"]["temp_mem"][-1], 96)
        # The two sensors stay separate series: a memory die at 96 must not be
        # able to move the core reading, or vice versa.
        self.assertEqual(card["series"]["temp"][-1], 70)

    def test_memory_temp_peak_lands_in_card_health(self):
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0, temp_mem=84 if t > 100 else 102)])
                        for t in range(300, 0, -10)])
        h = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()["cards"][0]["health"]
        self.assertEqual(h["peak_temp_mem"], 102)

    def test_health_memory_peak_is_absent_not_zero_without_a_sensor(self):
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0)]) for t in range(300, 0, -10)])
        h = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()["cards"][0]["health"]
        self.assertIsNone(h["peak_temp_mem"])

    def test_a_stalled_fan_reports_zero_and_stays_supported(self):
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0, fan=0)]) for t in range(300, 0, -10)])
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        card = d["cards"][0]
        self.assertTrue(card["supports"]["fan"])
        self.assertEqual(card["series"]["fan"][-1], 0)


class TestCombined(unittest.TestCase):
    def setUp(self):
        self.c = _client()
        _wipe()

    def test_combined_temperature_is_the_hottest_card_not_the_mean(self):
        # Averaging temperature across cards hides the one card that is cooking,
        # which is the entire question the combined panel answers.
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0, temp=60), _card(1, temp=86), _card(2, temp=64)])
                        for t in range(300, 0, -10)])
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        self.assertEqual(max(v for v in d["combined"]["temp_max"] if v is not None), 86)

    def test_combined_vram_and_power_are_sums(self):
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0, mem_used=10000, power=100),
                                   _card(1, mem_used=20000, power=200)])
                        for t in range(300, 0, -10)])
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        self.assertEqual(d["combined"]["vram"][-1], 30000)
        self.assertEqual(d["combined"]["power"][-1], 300)


class TestThrottleSpans(unittest.TestCase):
    def setUp(self):
        self.c = _client()
        _wipe()

    def test_consecutive_throttle_samples_merge_into_one_span(self):
        now = (int(time.time()) // 10) * 10
        _seed("vader", [(now - t, [_card(0, temp=87, throttle_mask=0x40)])
                        for t in range(300, 0, -10)])
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        spans = d["cards"][0]["throttle_spans"]
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["reasons"], ["HW thermal"])
        self.assertGreaterEqual(spans[0]["end"] - spans[0]["start"], 280)

    def test_a_gap_starts_a_new_span(self):
        now = (int(time.time()) // 10) * 10
        pts = ([(now - t, [_card(0, throttle_mask=0x40)]) for t in (600, 590, 580)]
               + [(now - t, [_card(0, throttle_mask=0)]) for t in (570, 560, 550)]
               + [(now - t, [_card(0, throttle_mask=0x40)]) for t in (300, 290)])
        _seed("vader", pts)
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        self.assertEqual(len(d["cards"][0]["throttle_spans"]), 2)

    def test_power_cap_alone_is_not_a_throttle_span(self):
        # A deliberately power-limited box sits at its cap by design; showing it
        # as a permanent throttle band would make the indicator meaningless.
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0, throttle_mask=0x04)]) for t in range(300, 0, -10)])
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        self.assertEqual(d["cards"][0]["throttle_spans"], [])


class TestHealth(unittest.TestCase):
    def setUp(self):
        self.c = _client()
        _wipe()

    def test_health_reports_seconds_not_raw_sample_counts(self):
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0, temp=87, throttle_mask=0x40)])
                        for t in range(300, 0, -10)])   # 30 samples @ 10s
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        h = d["cards"][0]["health"]
        self.assertEqual(h["throttled_sec"], 300)
        self.assertEqual(h["hot_sec"], 300)
        self.assertEqual(h["peak_temp"], 87)


class TestThresholdIsShared(unittest.TestCase):
    """The number the dashboard draws and the number that pages you must match.

    A tab that shows a red "HOT" pill at one temperature while the notifier
    fires at another teaches the user to trust neither.
    """

    def setUp(self):
        self.c = _client()
        _wipe()

    def tearDown(self):
        import app
        app.save_settings({"gpu_temp_alert_c": "84", "gpu_temp_overrides": ""})

    def test_hot_c_follows_the_configured_setting(self):
        import app
        app.save_settings({"gpu_temp_alert_c": "90"})
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        self.assertEqual(d["hot_c"], 90)

    def test_hot_c_follows_a_per_host_override(self):
        import app
        app.save_settings({"gpu_temp_alert_c": "84", "gpu_temp_overrides": '{"vader": 88}'})
        a = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        b = self.c.get("/api/gpu/history?host=local&range=1h").get_json()
        self.assertEqual((a["hot_c"], b["hot_c"]), (88, 84))

    def test_the_hot_column_counts_against_that_same_threshold(self):
        import app
        app.save_settings({"gpu_temp_alert_c": "90"})
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0, temp=86)]) for t in range(300, 0, -10)])
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        # 86 C is hot under an 84 C threshold but not under 90 C.
        self.assertEqual(d["cards"][0]["health"]["hot_sec"], 0)


class TestAttribution(unittest.TestCase):
    def setUp(self):
        self.c = _client()
        _wipe()

    def _seed_procs(self, host, rows):
        import app
        app.DB.executemany("INSERT INTO proc(ts,service,mem,host) VALUES(?,?,?,?)", rows)
        app.DB.commit()

    def test_power_split_is_flagged_as_an_estimate(self):
        # Load-bearing: GPUs never meter power per process. If this flag ever
        # goes away the UI silently starts presenting a model as a measurement.
        d = self.c.get("/api/gpu/attribution?host=vader&range=1h").get_json()
        self.assertIs(d["estimated"], True)

    def test_vram_is_split_per_service_and_scoped_to_the_host(self):
        now = int(time.time())
        self._seed_procs("vader", [(now - t, "ollama", 60000, "vader") for t in range(300, 0, -10)]
                         + [(now - t, "whisper", 400, "vader") for t in range(300, 0, -10)]
                         + [(now - t, "ollama", 8000, "local") for t in range(300, 0, -10)])
        d = self.c.get("/api/gpu/attribution?host=vader&range=1h").get_json()
        by_name = {s["service"]: s for s in d["services"]}
        self.assertEqual(sorted(by_name), ["ollama", "whisper"])
        self.assertEqual(by_name["ollama"]["peak_mb"], 60000)

    def test_idle_floor_is_reported_separately_from_services(self):
        # An idle 3090 burns ~100 W just being powered on. Charging that baseline
        # to whichever service holds VRAM would materially overstate its cost.
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0, power=300, mem_used=20000)]) for t in range(300, 100, -10)]
              + [(now - t, [_card(0, power=100, mem_used=20000)]) for t in range(100, 0, -10)])
        self._seed_procs("vader", [(now - t, "ollama", 20000, "vader") for t in range(300, 0, -10)])
        d = self.c.get("/api/gpu/attribution?host=vader&range=1h").get_json()
        self.assertGreater(d["idle_floor_w"], 0)
        self.assertIn("idle", d)

    def test_no_gpu_processes_yields_empty_services_not_zeros(self):
        d = self.c.get("/api/gpu/attribution?host=quiet&range=1h").get_json()
        self.assertEqual(d["services"], [])


class TestHubEnrichmentIsAbsentNotZero(unittest.TestCase):
    """The hub's own cards must follow the same rule the probe does.

    _enrich_gpus() used _gpu_num, which coerces '[N/A]' to 0.0 — so an
    unsupported clock or power cap on the HUB's GPU advertised itself as
    supported and then drew a confident flat line at zero.
    """

    def test_unsupported_fields_stay_absent_on_the_hub(self):
        import app
        cards = [{"idx": 0, "name": "Quadro P2000"}]
        rows = "0, [N/A], 1500, [N/A], [Not Supported], [N/A], P8\n"
        with mock.patch.object(app, "smi",
                               side_effect=lambda a: rows if "utilization.memory" in a[0] else ""):
            app._enrich_gpus(cards)
        c = cards[0]
        self.assertNotIn("mem_util", c)
        self.assertNotIn("clk_mem", c)
        self.assertNotIn("power_limit", c)
        self.assertNotIn("temp_mem", c)
        self.assertEqual(c["clk_sm"], 1500)      # the one it did report
        self.assertEqual(c["pstate"], "P8")


if __name__ == "__main__":
    unittest.main()
