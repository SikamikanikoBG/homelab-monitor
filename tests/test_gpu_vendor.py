"""Cross-vendor GPU `vendor` badge — backend inference + additive-payload tests.

Verifies the Linux GPU paths (app.sample_once via nvidia-smi, app.read_amd_gpus
via sysfs, and probe.read_gpu) now emit a `vendor` slug identical to the Windows
probe's VendorOf, so the dashboard reads ONE field regardless of host OS. Also
guards back-compat: every pre-existing GPU key must still be present, with
`vendor` added on top (never renamed/dropped).

Owned by this fire only — a new file so it never collides with the tests/ +
conftest work happening concurrently."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
import probe


class TestVendorInference(unittest.TestCase):
    """The pure name→slug mappers in both app.py and probe.py must agree and
    classify representative marketing names correctly, with a neutral fallback."""

    CASES = [
        ("NVIDIA GeForce RTX 3090", "nvidia"),
        ("NVIDIA GeForce GTX 1080 Ti", "nvidia"),
        ("Tesla V100-SXM2-16GB", "nvidia"),
        ("Quadro RTX 8000", "nvidia"),
        ("AMD Radeon RX 7900 XTX", "amd"),
        ("AMD Instinct MI300X", "amd"),
        ("Radeon Pro W6800", "amd"),
        ("Intel Arc A770", "intel"),
        ("Intel(R) UHD Graphics 770", "intel"),
        ("Intel Iris Xe Graphics", "intel"),
        ("Some Weird Accelerator 9000", "unknown"),
        ("", "unknown"),
        (None, "unknown"),
    ]

    def test_app_gpu_vendor(self):
        for name, want in self.CASES:
            self.assertEqual(app._gpu_vendor(name), want, name)

    def test_probe_gpu_vendor(self):
        for name, want in self.CASES:
            self.assertEqual(probe._gpu_vendor(name), want, name)

    def test_app_and_probe_agree(self):
        # Byte-identical semantics keep the UI's single-field contract honest.
        for name, _ in self.CASES:
            self.assertEqual(app._gpu_vendor(name), probe._gpu_vendor(name), name)


class TestNvidiaSampleVendor(unittest.TestCase):
    """app.sample_once (nvidia-smi path) tags each card with vendor='nvidia'
    while keeping every pre-existing per-card key intact (additive)."""

    def _sample_with(self, gpu_csv):
        def fake_smi(args):
            return gpu_csv if "query-gpu" in " ".join(args) else ""
        with patch("app.smi", side_effect=fake_smi), \
             patch("app.containers", return_value=[]), \
             patch("app.sample_callers", return_value={}), \
             patch("app.read_amd_gpus", return_value=[]), \
             patch("app.read_host", return_value={"cpu": 0, "ram_used": 0,
                                                  "ram_total": 0, "load1": 0, "ctemp": 0}):
            app.sample_once()

    def test_rtx_3090_reports_nvidia(self):
        self._sample_with("0, NVIDIA GeForce RTX 3090, 25, 4000, 24576, 150, 55")
        g = app.LATEST["gpus"][0]
        self.assertEqual(g["vendor"], "nvidia")

    def test_backcompat_all_prior_keys_present(self):
        self._sample_with("0, NVIDIA GeForce RTX 3090, 25, 4000, 24576, 150, 55")
        g = app.LATEST["gpus"][0]
        for k in ("idx", "name", "util", "mem_used", "mem_total", "power", "temp"):
            self.assertIn(k, g, k)   # nothing renamed/dropped
        self.assertEqual(g["idx"], 0)
        self.assertEqual(g["mem_used"], 4000)
        self.assertEqual(g["mem_total"], 24576)
        self.assertEqual(g["util"], 25)


class TestAmdSampleVendor(unittest.TestCase):
    """The sysfs AMD reader keeps emitting vendor='amd' with the full key set."""

    def _card(self, root):
        dev = os.path.join(root, "class", "drm", "card0", "device")
        os.makedirs(dev, exist_ok=True)
        for fn, val in (("vendor", "0x1002\n"), ("gpu_busy_percent", "42\n"),
                        ("mem_info_vram_used", str(8 * 1024 * 1024 * 1024) + "\n"),
                        ("mem_info_vram_total", str(24 * 1024 * 1024 * 1024) + "\n"),
                        ("product_name", "AMD Radeon RX 7900 XTX\n")):
            with open(os.path.join(dev, fn), "w") as f:
                f.write(val)

    def test_amd_card_reports_amd_and_keys(self):
        with tempfile.TemporaryDirectory() as root:
            self._card(root)
            glob_pat = os.path.join(root, "class", "drm", "card*", "device")
            with patch.object(app, "AMD_DRM_GLOB", glob_pat):
                cards = app.read_amd_gpus()
            self.assertEqual(len(cards), 1)
            g = cards[0]
            self.assertEqual(g["vendor"], "amd")
            for k in ("idx", "name", "util", "mem_used", "mem_total", "power", "temp"):
                self.assertIn(k, g, k)


class TestProbeReadGpuVendor(unittest.TestCase):
    """probe.read_gpu emits vendor on both the NVIDIA and AMD branches, additive
    to the existing {count,name,mem_used,mem_total,util,temp} contract."""

    def test_nvidia_branch(self):
        class R:
            returncode = 0
            stdout = b"4000, 24576, 25, 55, NVIDIA GeForce RTX 3090\n"
        with patch("probe.subprocess.run", return_value=R()):
            out = probe.read_gpu()
        g = out["gpu"]
        self.assertEqual(g["vendor"], "nvidia")
        for k in ("count", "name", "mem_used", "mem_total", "util", "temp"):
            self.assertIn(k, g, k)

    def test_amd_branch(self):
        def boom(*a, **k):
            raise FileNotFoundError("no nvidia-smi")
        amd = [{"name": "AMD Radeon RX 7900 XTX", "util": 30, "mem_used": 8000,
                "mem_total": 24576, "temp": 60, "power": 200}]
        with patch("probe.subprocess.run", side_effect=boom), \
             patch("probe.read_amd_gpus", return_value=amd):
            out = probe.read_gpu()
        g = out["gpu"]
        self.assertEqual(g["vendor"], "amd")
        for k in ("count", "name", "mem_used", "mem_total", "util", "temp"):
            self.assertIn(k, g, k)


if __name__ == "__main__":
    unittest.main()
