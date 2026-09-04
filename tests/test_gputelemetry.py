"""Unit tests for the enriched GPU telemetry — mem-BW util, clocks, power limit,
performance state, memory temp, and throttle-reason decoding (GPU truth-telling)."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class TestThrottleDecode(unittest.TestCase):
    def test_idle_and_zero_are_not_throttling(self):
        self.assertEqual(app._decode_throttle("0x0000000000000000"), [])
        self.assertEqual(app._decode_throttle("0x0000000000000001"), [])  # GPU_IDLE — normal

    def test_power_cap(self):
        self.assertEqual(app._decode_throttle("0x0000000000000004"), ["Power cap"])

    def test_combined_power_and_hw_thermal(self):
        # 0x44 = 0x40 (HW thermal) | 0x04 (Power cap)
        self.assertEqual(app._decode_throttle("0x0000000000000044"), ["Power cap", "HW thermal"])

    def test_unsupported_value(self):
        self.assertEqual(app._decode_throttle("[N/A]"), [])
        self.assertEqual(app._decode_throttle(None), [])


class TestEnrichGpus(unittest.TestCase):
    def test_attaches_extra_fields_and_throttle(self):
        gpus = [{"idx": 0, "util": 50, "power": 100, "mem_used": 8000, "mem_total": 24000, "temp": 70}]

        def fake_smi(args):
            a = args[0]
            if "utilization.memory" in a:
                return "0, 45, 1800, 9500, 250, 78, P2"
            if "clocks_throttle_reasons.active" in a:
                return "0, 0x0000000000000004"   # power cap
            return ""
        with patch("app.smi", side_effect=fake_smi):
            app._enrich_gpus(gpus)
        g = gpus[0]
        self.assertEqual(g["mem_util"], 45)
        self.assertEqual(g["clk_sm"], 1800)
        self.assertEqual(g["clk_mem"], 9500)
        self.assertEqual(g["power_limit"], 250)
        self.assertEqual(g["temp_mem"], 78)
        self.assertEqual(g["pstate"], "P2")
        self.assertTrue(g["throttled"])
        self.assertIn("Power cap", g["throttle"])

    def test_field_rename_fallback(self):
        # Newer drivers renamed clocks_throttle_reasons -> clocks_event_reasons.
        gpus = [{"idx": 0, "util": 10}]

        def fake_smi(args):
            a = args[0]
            if "utilization.memory" in a:
                return "0, 5, 300, 405, 200, [N/A], P8"
            if "clocks_throttle_reasons.active" in a:
                return ""                          # old field unsupported -> empty
            if "clocks_event_reasons.active" in a:
                return "0, 0x0000000000000040"     # HW thermal via the new field name
            return ""
        with patch("app.smi", side_effect=fake_smi):
            app._enrich_gpus(gpus)
        self.assertEqual(gpus[0]["pstate"], "P8")
        # [N/A] leaves the field ABSENT, not 0. This assertion used to expect 0;
        # that was the hub disagreeing with its own probe, which has always left
        # unsupported fields off. It matters because the GPU cockpit derives a
        # per-card `supports` map from presence — a coerced 0 advertises a metric
        # as supported and then draws a confident flat line at zero.
        self.assertNotIn("temp_mem", gpus[0])
        self.assertEqual(gpus[0]["throttle"], ["HW thermal"])

    def test_enrichment_never_raises_on_bad_smi(self):
        gpus = [{"idx": 0, "util": 1}]
        with patch("app.smi", side_effect=RuntimeError("nvidia-smi blew up")):
            app._enrich_gpus(gpus)                  # must swallow, not propagate
        self.assertNotIn("mem_util", gpus[0])       # nothing attached, but no crash


class TestGpuCardsFast(unittest.TestCase):
    """gpu_cards_fast() — the fast-lane re-read behind the configurable live
    refresh interval. Its own docstring has always promised fan alongside
    util/VRAM/power/temp, but the query never actually asked nvidia-smi for it —
    fan only ever updated on the full 10s sample regardless of how fast the
    fast lane was told to run."""

    def test_includes_fan_when_the_driver_reports_it(self):
        def fake_smi(args):
            a = args[0]
            if "fan.speed" in a:
                return "0, 12, 8000, 250, 65, 70\n1, 0, 500, 30, 40, 55"
            return ""
        with patch("app.smi", side_effect=fake_smi):
            out = app.gpu_cards_fast()
        self.assertEqual(out[0], {"util": 12.0, "mem_used": 8000.0, "power": 250.0,
                                   "temp": 65.0, "fan": 70})
        self.assertEqual(out[1]["fan"], 55)

    def test_falls_back_when_the_driver_rejects_fan_speed(self):
        # nvidia-smi rejects the WHOLE query on an unrecognised field name, so an
        # older driver's fan.speed attempt comes back empty rather than erroring —
        # this must retry without it instead of reporting no cards at all.
        def fake_smi(args):
            a = args[0]
            if "fan.speed" in a:
                return ""
            return "0, 12, 8000, 250, 65"
        with patch("app.smi", side_effect=fake_smi):
            out = app.gpu_cards_fast()
        self.assertEqual(out[0], {"util": 12.0, "mem_used": 8000.0, "power": 250.0, "temp": 65.0})
        self.assertNotIn("fan", out[0])

    def test_fan_absent_not_zero_on_a_passively_cooled_card(self):
        # [N/A] must leave "fan" unset, not coerce to 0 (0 would read as a
        # stalled fan and could trip the fan-stall alert on hardware with none).
        with patch("app.smi", return_value="0, 12, 8000, 250, 65, [N/A]"):
            out = app.gpu_cards_fast()
        self.assertNotIn("fan", out[0])

    def test_smi_failure_degrades_to_empty(self):
        with patch("app.smi", side_effect=RuntimeError("nvidia-smi blew up")):
            self.assertEqual(app.gpu_cards_fast(), {})


class TestGpuExtra(unittest.TestCase):
    def test_aggregate(self):
        gpus = [
            {"idx": 0, "mem_util": 40, "clk_sm": 1800, "clk_mem": 9000, "power_limit": 250,
             "pstate": "P2", "temp_mem": 80, "throttled": True, "throttle": ["Power cap"]},
            {"idx": 1, "mem_util": 60, "power_limit": 250, "temp_mem": 70, "throttle": []},
        ]
        x = app._gpu_extra(gpus)
        self.assertEqual(x["mem_util"], 50)          # avg(40, 60)
        self.assertEqual(x["power_limit"], 500)      # sum
        self.assertEqual(x["temp_mem"], 80)          # max
        self.assertEqual(x["clk_sm"], 1800)          # representative (card 0)
        self.assertTrue(x["throttled"])              # any card
        self.assertEqual(x["throttle"], ["Power cap"])

    def test_empty(self):
        self.assertEqual(app._gpu_extra([]), {})


if __name__ == "__main__":
    unittest.main()
