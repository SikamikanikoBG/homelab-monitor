"""Unit tests for multi-GPU sampling (issue #95)."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class TestMultiGpu(unittest.TestCase):
    def _sample_with(self, gpu_csv):
        def fake_smi(args):
            a = " ".join(args)
            if "query-gpu" in a:
                return gpu_csv
            return ""   # no compute-apps
        # amd_gpus is mocked empty so the test doesn't pick up the host's real
        # /sys/class/drm AMD card (which would merge in as a hybrid GPU).
        with patch("app.smi", side_effect=fake_smi), \
             patch("app.amd_gpus", return_value=[]), \
             patch("app.containers", return_value=[]), \
             patch("app.sample_callers", return_value={}), \
             patch("app.read_host", return_value={"cpu": 0, "ram_used": 0,
                                                   "ram_total": 0, "load1": 0, "ctemp": 0}):
            app.sample_once()

    def test_two_gpus_parsed_and_aggregated(self):
        self._sample_with("0, NVIDIA GeForce RTX 3090, 50, 8000, 24576, 200, 60\n"
                          "1, NVIDIA GeForce RTX 3090, 10, 2000, 24576, 100, 45")
        gs = app.LATEST["gpus"]
        self.assertEqual([g["idx"] for g in gs], [0, 1])
        self.assertTrue(app.LATEST["gpu_avail"])
        self.assertEqual(app.LATEST["mem_used"], 10000)    # 8000 + 2000 (pool)
        self.assertEqual(app.LATEST["mem_total"], 49152)   # 24576 * 2
        self.assertEqual(app.LATEST["power"], 300)         # 200 + 100
        self.assertEqual(app.LATEST["util"], 30)           # (50 + 10) / 2
        self.assertEqual(app.LATEST["temp"], 60)           # hottest card
        # per-GPU history persisted when there's more than one card
        with app.LOCK:
            n = app.DB.execute("SELECT COUNT(DISTINCT idx) FROM gpu_samples").fetchone()[0]
        self.assertGreaterEqual(n, 2)

    def test_single_gpu_still_works(self):
        self._sample_with("0, NVIDIA GeForce RTX 3090, 25, 4000, 24576, 150, 55")
        self.assertEqual(len(app.LATEST["gpus"]), 1)
        self.assertEqual(app.LATEST["mem_used"], 4000)
        self.assertEqual(app.LATEST["util"], 25)
        self.assertTrue(app.LATEST["gpu_avail"])

    def test_not_supported_fields_degrade(self):
        self._sample_with("0, GPU, [N/A], 1000, 8000, [Not Supported], [Not Supported]")
        g = app.LATEST["gpus"][0]
        self.assertEqual(g["mem_used"], 1000)
        self.assertEqual(g["power"], 0)   # unsupported -> 0, card still present
        self.assertEqual(g["temp"], 0)

    def _sample_hybrid(self, gpu_csv, amd_cards, smi_error=None):
        """Drive sample_once() with a mocked nvidia-smi (or an error) AND a mocked
        amd_gpus() list, to exercise the vendor-merge path (issue #1)."""
        def fake_smi(args):
            if smi_error is not None:
                raise smi_error
            a = " ".join(args)
            return gpu_csv if "query-gpu" in a else ""   # no compute-apps
        with patch("app.smi", side_effect=fake_smi), \
             patch("app.amd_gpus", return_value=amd_cards), \
             patch("app.containers", return_value=[]), \
             patch("app.sample_callers", return_value={}), \
             patch("app.read_host", return_value={"cpu": 0, "ram_used": 0,
                                                  "ram_total": 0, "load1": 0, "ctemp": 0}):
            app.sample_once()

    def test_hybrid_nvidia_and_amd_both_shown(self):
        # One NVIDIA card + one AMD card must BOTH appear (a hybrid box), with the
        # AMD card re-indexed above the NVIDIA range so gpu_samples.idx can't collide.
        amd = [{"idx": 0, "name": "AMD Radeon RX 7900 XTX", "util": 20.0,
                "mem_used": 1024, "mem_total": 8192, "power": 40.0, "temp": 50.0}]
        self._sample_hybrid("0, NVIDIA GeForce RTX 3090, 50, 8000, 24576, 200, 60", amd)
        gs = app.LATEST["gpus"]
        self.assertEqual(len(gs), 2)
        self.assertEqual(app.LATEST["gpu_vendor"], "hybrid")
        self.assertEqual(sorted(g["idx"] for g in gs), [0, 1])   # unique, AMD bumped to 1
        self.assertEqual(app.LATEST["mem_total"], 24576 + 8192)  # pooled across vendors
        self.assertIn("AMD Radeon RX 7900 XTX", {g["name"] for g in gs})

    def test_hybrid_gpu_extra_pools_both_vendors(self):
        # The 'GPU right now' chips must aggregate across vendors: the power chip
        # divides pooled draw by the pooled cap, so an NVIDIA-only aggregate would
        # read >100% of cap as soon as the AMD card draws anything. Here the mocked
        # smi returns no enrichment rows, so the AMD card's cap is the only one —
        # it must survive into the aggregate rather than vanish.
        amd = [{"idx": 0, "name": "AMD Radeon RX 7900 XTX", "util": 20.0,
                "mem_used": 1024, "mem_total": 8192, "power": 40.0, "temp": 50.0,
                "power_limit": 291.0, "clk_sm": 2400, "pstate": "auto"}]

        def fake_smi(args):
            a = " ".join(args)
            if "memory.used" in a:      # the base per-card query
                return "0, NVIDIA GeForce RTX 3090, 50, 8000, 24576, 200, 60"
            if "power.limit" in a:      # the enrichment query: give NVIDIA its own cap
                return "0, 12, 1500, 9000, 350, 70, P2"
            return ""                   # compute-apps: nothing

        with patch("app.smi", side_effect=fake_smi), \
             patch("app.amd_gpus", return_value=amd), \
             patch("app.containers", return_value=[]), \
             patch("app.sample_callers", return_value={}), \
             patch("app.read_host", return_value={"cpu": 0, "ram_used": 0,
                                                  "ram_total": 0, "load1": 0, "ctemp": 0}):
            app.sample_once()
        x = app.LATEST["gpu_extra"]
        self.assertEqual(x["power_limit"], 350 + 291)   # BOTH vendors' caps, summed
        # The representative card (g0) is the NVIDIA one: its P-state must not be
        # replaced by the AMD policy string on a hybrid box.
        self.assertEqual(x["pstate"], "P2")
        # Only NVIDIA measured mem-bandwidth utilisation: its value must come
        # through undiluted, not averaged against the APU's absent counter.
        self.assertEqual(x["mem_util"], 12)

    def test_hybrid_partial_power_cap_hides_the_chip(self):
        # The AMD card has no cap here: publishing the NVIDIA-only cap would let
        # the UI divide BOTH vendors' pooled draw by half a denominator.
        amd = [{"idx": 0, "name": "AMD Strix Halo", "util": 0.0, "mem_used": 24655,
                "mem_total": 126976, "power": 40.0, "temp": 33.0}]

        def fake_smi(args):
            a = " ".join(args)
            if "memory.used" in a:
                return "0, NVIDIA GeForce RTX 3090, 50, 8000, 24576, 200, 60"
            if "power.limit" in a:
                return "0, 12, 1500, 9000, 350, 70, P2"
            return ""

        with patch("app.smi", side_effect=fake_smi), \
             patch("app.amd_gpus", return_value=amd), \
             patch("app.containers", return_value=[]), \
             patch("app.sample_callers", return_value={}), \
             patch("app.read_host", return_value={"cpu": 0, "ram_used": 0,
                                                  "ram_total": 0, "load1": 0, "ctemp": 0}):
            app.sample_once()
        self.assertNotIn("power_limit", app.LATEST["gpu_extra"])

    def test_compute_apps_failure_keeps_chips_and_amd_attribution(self):
        # The compute-apps query is a separate nvidia-smi round-trip: when it dies
        # (timeout, wedged driver) the sample must keep the GPU chips and still run
        # the AMD fdinfo attribution that follows it.
        amd = [{"idx": 0, "name": "AMD Strix Halo", "util": 0.0, "mem_used": 24655,
                "mem_total": 126976, "power": 9.0, "temp": 33.0,
                "clk_sm": 600, "pstate": "auto",
                "unified": True, "pdev": "0000:67:00.0"}]

        def fake_smi(args):
            a = " ".join(args)
            if "memory.used" in a:
                return "0, NVIDIA GeForce RTX 3090, 50, 8000, 24576, 200, 60"
            if "power.limit" in a:
                return "0, 12, 1500, 9000, 350, 70, P2"
            if "compute-apps" in a:
                raise TimeoutError("nvidia-smi hung")
            return ""

        with patch("app.smi", side_effect=fake_smi), \
             patch("app.amd_gpus", return_value=amd), \
             patch("app.amd_fdinfo_procs", return_value={4242: {"0000:67:00.0": {"vram": 0.0, "gtt": 22500.0}}}), \
             patch("app.service_for_pid", return_value="host:llama-server"), \
             patch("app.containers", return_value=[]), \
             patch("app.sample_callers", return_value={}), \
             patch("app.read_host", return_value={"cpu": 0, "ram_used": 0,
                                                  "ram_total": 0, "load1": 0, "ctemp": 0}):
            app.sample_once()
        self.assertEqual(app.LATEST["gpu_extra"].get("clk_sm"), 1500)   # chips survived
        self.assertIn({"service": "host:llama-server", "mem": 22500},
                      app.LATEST["procs"])

    def test_amd_only_gpu_extra_carries_the_enriched_fields(self):
        amd = [{"idx": 0, "name": "AMD Strix Halo", "util": 0.0, "mem_used": 24655,
                "mem_total": 126976, "power": 9.0, "temp": 33.0,
                "clk_sm": 600, "pstate": "auto"}]
        self._sample_hybrid("", amd, smi_error=FileNotFoundError("nvidia-smi"))
        x = app.LATEST["gpu_extra"]
        self.assertEqual(x["clk_sm"], 600)
        self.assertEqual(x["pstate"], "auto")

    def test_amd_only_without_nvidia_smi(self):
        # The real bug: nvidia-smi missing entirely (FileNotFoundError) must NOT hide
        # an AMD card — before the fix the exception aborted the whole GPU half.
        amd = [{"idx": 0, "name": "AMD Radeon RX 7900 XTX", "util": 0.0,
                "mem_used": 512, "mem_total": 8192, "power": 0.0, "temp": 0.0}]
        self._sample_hybrid("", amd, smi_error=FileNotFoundError("nvidia-smi"))
        self.assertTrue(app.LATEST["gpu_avail"])
        self.assertEqual(app.LATEST["gpu_vendor"], "amd")
        self.assertEqual(len(app.LATEST["gpus"]), 1)
        self.assertEqual(app.LATEST["mem_total"], 8192)


if __name__ == "__main__":
    unittest.main()
