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
        with patch("app.smi", side_effect=fake_smi), \
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
