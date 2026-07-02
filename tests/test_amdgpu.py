"""Unit tests for hardware-agnostic AMD GPU collection via sysfs (amdgpu).

No AMD hardware is required: each test builds a fake /sys/class/drm tree in a
temp dir and points the reader's glob at it, then asserts the parsed util / VRAM
(MB) / temp (°C) / power (W) / power_limit (W) and the degrade/skip behaviour.
Covers both the local collector (app.read_amd_gpus + sample_once merge) and the
remote probe (probe.read_amd_gpus + read_gpu fallback)."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
import probe


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def make_card(root, card, *, vendor="0x1002", busy="42",
              vram_used=8 * 1024 * 1024 * 1024, vram_total=24 * 1024 * 1024 * 1024,
              temp_mc=65000, power_uw=210_000_000, cap_uw=350_000_000,
              device_id="0x744c", product=None, hwmon=True):
    """Build /sys/class/drm/<card>/device/... with the requested attributes.
    Pass a field as None to omit that file (simulating an unreadable attribute)."""
    dev = os.path.join(root, "class", "drm", card, "device")
    if vendor is not None:
        _write(os.path.join(dev, "vendor"), vendor + "\n")
    if busy is not None:
        _write(os.path.join(dev, "gpu_busy_percent"), str(busy) + "\n")
    if vram_used is not None:
        _write(os.path.join(dev, "mem_info_vram_used"), str(vram_used) + "\n")
    if vram_total is not None:
        _write(os.path.join(dev, "mem_info_vram_total"), str(vram_total) + "\n")
    if device_id is not None:
        _write(os.path.join(dev, "device"), device_id + "\n")
    if product is not None:
        _write(os.path.join(dev, "product_name"), product + "\n")
    if hwmon:
        hp = os.path.join(dev, "hwmon", "hwmon3")
        if temp_mc is not None:
            _write(os.path.join(hp, "temp1_input"), str(temp_mc) + "\n")
        if power_uw is not None:
            _write(os.path.join(hp, "power1_average"), str(power_uw) + "\n")
        if cap_uw is not None:
            _write(os.path.join(hp, "power1_cap"), str(cap_uw) + "\n")
    return dev


class TestAmdReaderApp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.glob = os.path.join(self.tmp, "class", "drm", "card*", "device")

    def _read(self):
        with patch.object(app, "AMD_DRM_GLOB", self.glob):
            return app.read_amd_gpus()

    def test_single_card_all_fields(self):
        make_card(self.tmp, "card0")
        gs = self._read()
        self.assertEqual(len(gs), 1)
        g = gs[0]
        self.assertEqual(g["vendor"], "amd")
        self.assertEqual(g["idx"], 0)
        self.assertEqual(g["util"], 42.0)
        self.assertEqual(g["mem_used"], 8192.0)       # 8 GiB → MB
        self.assertEqual(g["mem_total"], 24576.0)     # 24 GiB → MB
        self.assertEqual(g["temp"], 65.0)             # 65000 m°C → °C
        self.assertEqual(g["power"], 210.0)           # 210e6 µW → W
        self.assertEqual(g["power_limit"], 350.0)     # 350e6 µW → W
        self.assertEqual(g["name"], "AMD Radeon RX 7900 XTX")  # device-id hint

    def test_multiple_cards_reindexed(self):
        make_card(self.tmp, "card0", busy="10", device_id="0x744c")
        make_card(self.tmp, "card1", busy="90", device_id="0x73bf")
        gs = self._read()
        self.assertEqual([g["idx"] for g in gs], [0, 1])
        self.assertEqual({g["util"] for g in gs}, {10.0, 90.0})
        self.assertEqual(gs[1]["name"], "AMD Radeon RX 6900 XT")

    def test_non_amd_card_skipped(self):
        make_card(self.tmp, "card0", vendor="0x10de")   # NVIDIA — must be skipped
        make_card(self.tmp, "card1", vendor="0x1002", busy="55")
        gs = self._read()
        self.assertEqual(len(gs), 1)
        self.assertEqual(gs[0]["util"], 55.0)

    def test_product_name_marker_wins_over_id(self):
        make_card(self.tmp, "card0", product="Radeon Pro W7900", device_id="0x744c")
        self.assertEqual(self._read()[0]["name"], "Radeon Pro W7900")

    def test_missing_fields_degrade_to_zero_card_kept(self):
        # No busy%, no hwmon at all, but VRAM present → card still reported.
        make_card(self.tmp, "card0", busy=None, hwmon=False,
                  vram_used=1 * 1024 * 1024 * 1024, vram_total=8 * 1024 * 1024 * 1024)
        gs = self._read()
        self.assertEqual(len(gs), 1)
        g = gs[0]
        self.assertEqual(g["util"], 0.0)
        self.assertEqual(g["temp"], 0.0)
        self.assertEqual(g["power"], 0.0)
        self.assertEqual(g["power_limit"], 0.0)
        self.assertEqual(g["mem_used"], 1024.0)
        self.assertEqual(g["mem_total"], 8192.0)

    def test_partial_hwmon_temp_only(self):
        make_card(self.tmp, "card0", power_uw=None, cap_uw=None, temp_mc=70000)
        g = self._read()[0]
        self.assertEqual(g["temp"], 70.0)
        self.assertEqual(g["power"], 0.0)
        self.assertEqual(g["power_limit"], 0.0)

    def test_no_metric_nodes_card_dropped(self):
        # vendor matches but no util AND no vram → nothing useful → skip.
        make_card(self.tmp, "card0", busy=None, vram_used=None, vram_total=None,
                  hwmon=False)
        self.assertEqual(self._read(), [])

    def test_unknown_device_id_fallback_name(self):
        make_card(self.tmp, "card0", device_id="0x9999", product=None)
        self.assertEqual(self._read()[0]["name"], "AMD GPU (0x9999)")

    def test_no_sysfs_tree_empty(self):
        # glob points at an empty tree → empty list, no crash.
        self.assertEqual(self._read(), [])


class TestAmdMergeSampleOnce(unittest.TestCase):
    """AMD cards flow through sample_once into LATEST exactly like NVIDIA ones."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.glob = os.path.join(self.tmp, "class", "drm", "card*", "device")

    def _sample(self, nvidia_csv=""):
        def fake_smi(args):
            a = " ".join(args)
            if "query-gpu" in a:
                return nvidia_csv
            return ""
        with patch("app.smi", side_effect=fake_smi), \
             patch.object(app, "AMD_DRM_GLOB", self.glob), \
             patch("app.containers", return_value=[]), \
             patch("app.sample_callers", return_value={}), \
             patch("app.read_host", return_value={"cpu": 0, "ram_used": 0,
                                                   "ram_total": 0, "load1": 0, "ctemp": 0}):
            app.sample_once()

    def test_amd_only_populates_latest(self):
        make_card(self.tmp, "card0", busy="60", temp_mc=72000, power_uw=180_000_000,
                  vram_used=4 * 1024 * 1024 * 1024, vram_total=16 * 1024 * 1024 * 1024)
        self._sample(nvidia_csv="")            # nvidia-smi returns nothing
        self.assertTrue(app.LATEST["gpu_avail"])
        gs = app.LATEST["gpus"]
        self.assertEqual(len(gs), 1)
        self.assertEqual(gs[0]["vendor"], "amd")
        self.assertEqual(app.LATEST["util"], 60)
        self.assertEqual(app.LATEST["mem_used"], 4096)
        self.assertEqual(app.LATEST["mem_total"], 16384)
        self.assertEqual(app.LATEST["power"], 180)
        self.assertEqual(app.LATEST["temp"], 72)
        self.assertEqual(app.LATEST["gpu_extra"].get("power_limit"), 350)

    def test_two_amd_cards_aggregate(self):
        make_card(self.tmp, "card0", busy="20", temp_mc=60000, power_uw=100_000_000,
                  vram_used=2 * 1024 * 1024 * 1024, vram_total=8 * 1024 * 1024 * 1024)
        make_card(self.tmp, "card1", busy="80", temp_mc=75000, power_uw=200_000_000,
                  vram_used=6 * 1024 * 1024 * 1024, vram_total=8 * 1024 * 1024 * 1024)
        self._sample(nvidia_csv="")
        self.assertEqual(len(app.LATEST["gpus"]), 2)
        self.assertEqual(app.LATEST["util"], 50)           # (20+80)/2
        self.assertEqual(app.LATEST["mem_used"], 8192)     # 2+6 GiB
        self.assertEqual(app.LATEST["mem_total"], 16384)
        self.assertEqual(app.LATEST["power"], 300)         # 100+200
        self.assertEqual(app.LATEST["temp"], 75)           # hottest

    def test_mixed_nvidia_and_amd(self):
        # NVIDIA present AND an AMD card — both reported, idx space unique.
        make_card(self.tmp, "card0", busy="40", temp_mc=50000, power_uw=120_000_000,
                  vram_used=1 * 1024 * 1024 * 1024, vram_total=8 * 1024 * 1024 * 1024)
        self._sample(nvidia_csv="0, NVIDIA GeForce RTX 3090, 30, 4000, 24576, 250, 65")
        gs = app.LATEST["gpus"]
        self.assertEqual(len(gs), 2)
        self.assertEqual([g["idx"] for g in gs], [0, 1])
        self.assertEqual(gs[0]["name"], "NVIDIA GeForce RTX 3090")
        self.assertEqual(gs[1]["vendor"], "amd")
        # aggregates span both
        self.assertEqual(app.LATEST["mem_total"], 24576 + 8192)
        self.assertEqual(app.LATEST["power"], 250 + 120)
        self.assertEqual(app.LATEST["util"], 35)           # (30+40)/2

    def test_no_gpu_at_all_still_graceful(self):
        # neither NVIDIA nor AMD → existing 'no GPU' state, no crash.
        self._sample(nvidia_csv="")
        self.assertFalse(app.LATEST["gpu_avail"])
        self.assertEqual(app.LATEST["gpus"], [])

    def test_nvidia_unaffected_when_no_amd(self):
        # AMD glob empty → pure NVIDIA path identical to before.
        self._sample(nvidia_csv="0, NVIDIA GeForce RTX 3090, 25, 4000, 24576, 150, 55")
        gs = app.LATEST["gpus"]
        self.assertEqual(len(gs), 1)
        self.assertEqual(gs[0]["vendor"], "nvidia")  # cross-vendor badge: NVIDIA path now tags vendor
        self.assertEqual(app.LATEST["util"], 25)
        self.assertTrue(app.LATEST["gpu_avail"])


class TestAmdReaderProbe(unittest.TestCase):
    """Remote probe reads its own sysfs and falls back to AMD when no NVIDIA."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.glob = os.path.join(self.tmp, "class", "drm", "card*", "device")

    def test_probe_amd_reader(self):
        make_card(self.tmp, "card0", busy="33", temp_mc=68000, power_uw=190_000_000,
                  vram_used=5 * 1024 * 1024 * 1024, vram_total=16 * 1024 * 1024 * 1024,
                  product="AMD Radeon RX 7800 XT")
        with patch.object(probe, "AMD_DRM_GLOB", self.glob):
            gs = probe.read_amd_gpus()
        self.assertEqual(len(gs), 1)
        g = gs[0]
        self.assertEqual(g["util"], 33)
        self.assertEqual(g["mem_used"], 5120)
        self.assertEqual(g["mem_total"], 16384)
        self.assertEqual(g["temp"], 68)
        self.assertEqual(g["power"], 190)
        self.assertEqual(g["name"], "AMD Radeon RX 7800 XT")

    def test_read_gpu_falls_back_to_amd(self):
        make_card(self.tmp, "card0", busy="44", temp_mc=70000,
                  vram_used=2 * 1024 * 1024 * 1024, vram_total=8 * 1024 * 1024 * 1024)
        make_card(self.tmp, "card1", busy="22", temp_mc=60000,
                  vram_used=1 * 1024 * 1024 * 1024, vram_total=8 * 1024 * 1024 * 1024)

        def boom(*a, **k):
            raise FileNotFoundError("nvidia-smi")   # no NVIDIA driver

        with patch.object(probe, "AMD_DRM_GLOB", self.glob), \
             patch("probe.subprocess.run", side_effect=boom):
            out = probe.read_gpu()
        self.assertIn("gpu", out)
        g = out["gpu"]
        self.assertEqual(g["count"], 2)
        self.assertEqual(g["util"], 33)            # (44+22)/2
        self.assertEqual(g["mem_used"], 3072)      # 2+1 GiB
        self.assertEqual(g["mem_total"], 16384)
        self.assertEqual(g["temp"], 70)            # hottest

    def test_read_gpu_nvidia_wins_when_present(self):
        # AMD card exists but nvidia-smi works → NVIDIA result returned (no regression).
        make_card(self.tmp, "card0", busy="50")

        class R:
            returncode = 0
            stdout = b"4000, 24576, 25, 55, NVIDIA GeForce RTX 3090\n"

        with patch.object(probe, "AMD_DRM_GLOB", self.glob), \
             patch("probe.subprocess.run", return_value=R()):
            out = probe.read_gpu()
        self.assertEqual(out["gpu"]["name"], "NVIDIA GeForce RTX 3090")
        self.assertEqual(out["gpu"]["mem_used"], 4000)

    def test_read_gpu_empty_when_neither(self):
        def boom(*a, **k):
            raise FileNotFoundError("nvidia-smi")
        with patch.object(probe, "AMD_DRM_GLOB", self.glob), \
             patch("probe.subprocess.run", side_effect=boom):
            self.assertEqual(probe.read_gpu(), {})


if __name__ == "__main__":
    unittest.main()
