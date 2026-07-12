"""Unit tests for the AMD GPU back-end (issue #1) — the amdgpu sysfs readers used
by the hub's local collector (app.amd_gpus) and the remote Linux probe
(probe._amd_gpu_sysfs). We build a fake /sys/class/drm tree so the parsing is
verified without an AMD GPU present."""
import errno
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
import probe


def _write(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(value))


def _amd_card(drm, idx, *, total, used, busy, temp_mc=None, power_uw=None,
              name=None, vendor="0x1002"):
    """Lay down a single fake card<idx>/device/ node under <drm>."""
    dev = os.path.join(drm, "card%d" % idx, "device")
    _write(os.path.join(dev, "vendor"), vendor + "\n")
    _write(os.path.join(dev, "mem_info_vram_total"), total)
    _write(os.path.join(dev, "mem_info_vram_used"), used)
    _write(os.path.join(dev, "gpu_busy_percent"), busy)
    if temp_mc is not None:
        _write(os.path.join(dev, "hwmon", "hwmon3", "temp1_input"), temp_mc)
    if power_uw is not None:
        _write(os.path.join(dev, "hwmon", "hwmon3", "power1_average"), power_uw)
    if name is not None:
        _write(os.path.join(dev, "product_name"), name)
    return dev


class TestAmdSysfs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.drm = os.path.join(self.tmp, "drm")
        os.makedirs(self.drm)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_app_collector_reads_card(self):
        # 8 GiB total, 1 GiB used, 37% busy, 54.3°C, 42 W.
        _amd_card(self.drm, 0, total=8 * 1024**3, used=1 * 1024**3, busy=37,
                  temp_mc=54300, power_uw=42_000_000, name="AMD Radeon RX 7900 XTX")
        gpus = app.amd_gpus(drm_root=self.drm)
        self.assertEqual(len(gpus), 1)
        g = gpus[0]
        self.assertEqual(g["idx"], 0)
        self.assertEqual(g["name"], "AMD Radeon RX 7900 XTX")
        self.assertEqual(g["util"], 37.0)
        self.assertEqual(g["mem_total"], 8192)
        self.assertEqual(g["mem_used"], 1024)
        self.assertEqual(g["temp"], 54.3)
        self.assertEqual(g["power"], 42.0)

    def test_probe_representative_matches(self):
        _amd_card(self.drm, 0, total=16 * 1024**3, used=2 * 1024**3, busy=10,
                  temp_mc=40000, name="AMD Instinct MI210")
        out = probe._amd_gpu_sysfs(drm_root=self.drm)
        self.assertIn("gpu", out)
        self.assertEqual(out["gpu"]["count"], 1)
        self.assertEqual(out["gpu"]["name"], "AMD Instinct MI210")
        self.assertEqual(out["gpu"]["mem_total"], 16384)
        self.assertEqual(out["gpu"]["mem_used"], 2048)
        self.assertEqual(out["gpu"]["util"], 10)
        self.assertEqual(out["gpu"]["temp"], 40)

    def test_non_amd_vendor_is_skipped(self):
        # An NVIDIA card (0x10de) in the same tree must be ignored by the AMD reader.
        _amd_card(self.drm, 0, total=8 * 1024**3, used=0, busy=0, vendor="0x10de")
        self.assertEqual(app.amd_gpus(drm_root=self.drm), [])
        self.assertEqual(probe._amd_gpu_sysfs(drm_root=self.drm), {})

    def test_missing_optional_fields_degrade_to_zero(self):
        # No hwmon (temp/power) and no product_name → still a valid card, zeros + fallback name.
        _amd_card(self.drm, 1, total=4 * 1024**3, used=512 * 1024**2, busy=0)
        gpus = app.amd_gpus(drm_root=self.drm)
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["name"], "AMD GPU 1")
        self.assertEqual(gpus[0]["temp"], 0)
        self.assertEqual(gpus[0]["power"], 0.0)
        self.assertEqual(gpus[0]["mem_used"], 512)

    def test_no_gpu_returns_empty(self):
        self.assertEqual(app.amd_gpus(drm_root=self.drm), [])
        self.assertEqual(probe._amd_gpu_sysfs(drm_root=self.drm), {})

    def test_unreadable_root_is_safe(self):
        missing = os.path.join(self.tmp, "does-not-exist")
        self.assertEqual(app.amd_gpus(drm_root=missing), [])
        self.assertEqual(probe._amd_gpu_sysfs(drm_root=missing), {})


class TestVendorAwareDiagnostics(unittest.TestCase):
    """The local diagnostics GPU row must speak the right vendor: an AMD host must
    NOT be told to install the NVIDIA runtime (issue #1 follow-up)."""

    def _gpu_check(self):
        for c in app.local_diagnostics()["checks"]:
            if c["id"] == "nvidia":
                return c
        self.fail("no GPU diagnostic row produced")

    def setUp(self):
        self._saved = dict(app.LATEST)

    def tearDown(self):
        app.LATEST.clear()
        app.LATEST.update(self._saved)

    def test_amd_present_is_labelled_amd(self):
        app.LATEST.update(gpu_avail=True, gpu_vendor="amd", mem_total=16384)
        c = self._gpu_check()
        self.assertEqual(c["label"], "GPU (AMD)")
        self.assertEqual(c["status"], "ok")
        self.assertIn("amdgpu", c["detail"])

    def test_nvidia_present_is_labelled_nvidia(self):
        app.LATEST.update(gpu_avail=True, gpu_vendor="nvidia", mem_total=24576)
        c = self._gpu_check()
        self.assertEqual(c["label"], "GPU (NVIDIA)")
        self.assertIn("nvidia-smi", c["detail"])

    def test_no_gpu_remedy_mentions_amd_not_only_nvidia(self):
        app.LATEST.update(gpu_avail=False, gpu_vendor=None)
        c = self._gpu_check()
        self.assertEqual(c["label"], "GPU")
        self.assertEqual(c["status"], "info")
        # The remedy must guide AMD users, not just NVIDIA ones.
        blob = (c.get("remedy") or {}).get("where", "") + (c.get("remedy") or {}).get("cmd", "")
        self.assertIn("amdgpu", blob)
        self.assertIn("mem_info_vram_total", blob)

    def test_hybrid_is_labelled_both_vendors(self):
        app.LATEST.update(gpu_avail=True, gpu_vendor="hybrid", mem_total=32768)
        c = self._gpu_check()
        self.assertEqual(c["label"], "GPU (NVIDIA + AMD)")
        self.assertIn("nvidia-smi + amdgpu sysfs", c["detail"])


class TestEbusyRetry(unittest.TestCase):
    """amdgpu's gpu_busy_percent intermittently returns EBUSY ('Device or resource
    busy'); the reader must retry once rather than silently dropping utilisation."""

    def test_read_int_retries_once_on_ebusy(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        p = os.path.join(d, "gpu_busy_percent")
        with open(p, "w") as f:
            f.write("37")
        real_open, state = open, {"busy": True}
        def flaky(path, *a, **k):
            if isinstance(path, str) and path.endswith("gpu_busy_percent") and state["busy"]:
                state["busy"] = False
                raise OSError(errno.EBUSY, "Device or resource busy")
            return real_open(path, *a, **k)
        with mock.patch("builtins.open", side_effect=flaky):
            self.assertEqual(app._amd_read_int(p), 37)   # retry succeeded

    def test_read_int_gives_up_on_persistent_ebusy(self):
        def always_busy(path, *a, **k):
            raise OSError(errno.EBUSY, "busy")
        with mock.patch("builtins.open", side_effect=always_busy):
            self.assertIsNone(app._amd_read_int("/sys/x/gpu_busy_percent"))

    def test_other_oserror_is_not_retried(self):
        # A genuinely absent node (ENOENT) → None, no retry loop.
        self.assertIsNone(app._amd_read_int("/definitely/not/here"))


if __name__ == "__main__":
    unittest.main()
