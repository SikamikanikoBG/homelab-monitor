"""GPU `vendor` across the integration surfaces (E5): Prometheus /metrics info
series, the MCP `get_gpu` per-card payload, and the All-hosts fleet vendor
summary. All three are additive/back-compat — nothing pre-existing is renamed
or dropped, and an absent vendor folds to 'unknown'."""
import os
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp"))
import homelab_client as hc


def _parse(text):
    """name -> list of (raw_line, value) for every sample line."""
    samples = {}
    line_re = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)(\s+\S+)?$')
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        m = line_re.match(line)
        assert m, f"malformed metric line: {line!r}"
        name, _lbl, val = m.group(1), m.group(2), m.group(3)
        if val != "NaN":
            float(val)
        samples.setdefault(name, []).append((line, val))
    return samples


class TestGpuInfoMetric(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()
        self._gpus = app.LATEST.get("gpus")
        self._host = app.LATEST.get("host")

    def tearDown(self):
        app.LATEST["gpus"] = self._gpus
        app.LATEST["host"] = self._host

    def test_info_series_emitted_with_vendor(self):
        app.LATEST["host"] = {"hostname": "rig-a"}
        app.LATEST["gpus"] = [
            {"idx": 0, "name": "NVIDIA GeForce RTX 3090", "vendor": "nvidia",
             "util": 50, "mem_used": 100, "mem_total": 24576, "power": 300, "temp": 60},
            {"idx": 1, "name": "AMD Radeon RX 7900 XTX", "vendor": "amd",
             "util": 10, "mem_used": 50, "mem_total": 24576, "power": 90, "temp": 45},
        ]
        body = app._extra_metrics_text()
        samples = _parse(body)
        self.assertIn("homelab_gpu_info", samples)
        lines = [l for l, _v in samples["homelab_gpu_info"]]
        # value is always 1 (info-style gauge)
        for _l, v in samples["homelab_gpu_info"]:
            self.assertEqual(float(v), 1.0)
        joined = "\n".join(lines)
        self.assertIn('vendor="nvidia"', joined)
        self.assertIn('vendor="amd"', joined)
        self.assertIn('name="NVIDIA GeForce RTX 3090"', joined)
        self.assertIn('gpu="gpu0"', joined)
        self.assertIn('gpu="gpu1"', joined)
        self.assertIn('host="rig-a"', joined)

    def test_hostile_name_is_escaped(self):
        app.LATEST["host"] = {"hostname": "h"}
        app.LATEST["gpus"] = [
            {"idx": 0, "name": 'Evil "GPU"\\x\nY', "vendor": "nvidia",
             "util": 0, "mem_used": 0, "mem_total": 0, "power": 0, "temp": 0},
        ]
        body = app._extra_metrics_text()
        # exposition must still parse (escaping kept every line well-formed)
        samples = _parse(body)
        line = samples["homelab_gpu_info"][0][0]
        self.assertIn('\\"', line)   # quote escaped
        self.assertIn("\\\\", line)  # backslash escaped
        self.assertIn("\\n", line)   # newline escaped
        # the raw newline never leaks into the exposition
        self.assertNotIn("\n", line)

    def test_absent_vendor_folds_to_unknown(self):
        app.LATEST["host"] = {"hostname": "h"}
        app.LATEST["gpus"] = [
            {"idx": 0, "name": "Mystery Accelerator",
             "util": 0, "mem_used": 0, "mem_total": 0, "power": 0, "temp": 0},
        ]
        samples = _parse(app._extra_metrics_text())
        self.assertIn('vendor="unknown"', samples["homelab_gpu_info"][0][0])

    def test_no_gpus_omits_info_series(self):
        app.LATEST["gpus"] = []
        self.assertNotIn("homelab_gpu_info", app._extra_metrics_text())

    def test_numeric_gpu_series_unchanged_no_vendor_label(self):
        # Back-compat: the numeric gpu gauges (when prometheus_client is present)
        # keep their existing label set — the vendor label lives ONLY on the
        # dedicated info series, so joins on gpu="gpu0" are unaffected.
        if not app._PROM_OK:
            self.skipTest("prometheus_client not installed; numeric gauges absent")
        app.LATEST["gpus"] = [
            {"idx": 0, "name": "NVIDIA GeForce RTX 3090", "vendor": "nvidia",
             "util": 5, "mem_used": 1, "mem_total": 2, "power": 3, "temp": 4},
        ]
        body = self.c.get("/metrics").get_data(as_text=True)
        samples = _parse(body)
        for name in ("homelab_gpu_util_pct", "homelab_gpu_vram_used_mb",
                     "homelab_gpu_vram_total_mb", "homelab_gpu_temp_c",
                     "homelab_gpu_power_w"):
            self.assertIn(name, samples, f"numeric series missing: {name}")
            for line, _v in samples[name]:
                self.assertNotIn("vendor", line, f"vendor polluted {name}: {line}")


class TestMcpGetGpuVendor(unittest.TestCase):
    def _fake_data(self, gpus):
        return {"range": "6h", "now": {"gpu_avail": True, "util": 42, "mem_used": 100,
                                       "mem_total": 24576, "power": 300, "temp": 60,
                                       "gpus": gpus},
                "model_summary": [], "callers": [], "pressure_free_mb": 1000}

    def test_get_gpu_carries_vendor_additively(self):
        gpus = [
            {"idx": 0, "name": "NVIDIA GeForce RTX 3090", "vendor": "nvidia",
             "util": 42, "mem_used": 100, "mem_total": 24576, "power": 300, "temp": 60},
            {"idx": 1, "name": "AMD Radeon RX 7900 XTX", "vendor": "amd",
             "util": 8, "mem_used": 40, "mem_total": 24576, "power": 90, "temp": 44},
        ]
        with patch.object(hc, "_get", return_value=self._fake_data(gpus)):
            out = hc.get_gpu("6h")
        # existing top-level keys intact
        for k in ("range", "available", "util_pct", "vram_used_mb", "vram_total_mb",
                  "power_w", "temp_c", "pressure_free_mb", "models_vram", "callers"):
            self.assertIn(k, out)
        self.assertEqual(out["util_pct"], 42)
        # additive per-card list with vendor
        self.assertIn("gpus", out)
        self.assertEqual(len(out["gpus"]), 2)
        self.assertEqual(out["gpus"][0]["vendor"], "nvidia")
        self.assertEqual(out["gpus"][1]["vendor"], "amd")
        self.assertEqual(out["gpus"][0]["name"], "NVIDIA GeForce RTX 3090")
        self.assertEqual(out["gpus"][0]["vram_total_mb"], 24576)

    def test_get_gpu_absent_vendor_is_unknown(self):
        gpus = [{"idx": 0, "name": "Mystery", "util": 0, "mem_used": 0,
                 "mem_total": 0, "power": 0, "temp": 0}]
        with patch.object(hc, "_get", return_value=self._fake_data(gpus)):
            out = hc.get_gpu("6h")
        self.assertEqual(out["gpus"][0]["vendor"], "unknown")

    def test_get_gpu_no_cards_empty_list(self):
        with patch.object(hc, "_get", return_value=self._fake_data([])):
            out = hc.get_gpu("6h")
        self.assertEqual(out["gpus"], [])


class TestFleetVendorSummary(unittest.TestCase):
    def _row(self, online=True, vendor="nvidia", gpu=True):
        if not gpu:
            host = {}
        elif vendor is None:
            host = {"gpu": {"util": 0}}  # gpu present, no vendor (older payload)
        else:
            host = {"gpu": {"vendor": vendor}}
        return {"online": online, "host": host}

    def test_counts_by_vendor(self):
        rows = [self._row(vendor="nvidia"), self._row(vendor="nvidia"),
                self._row(vendor="nvidia"), self._row(vendor="amd")]
        out = app._fleet_vendor_summary(rows)
        self.assertEqual(out, [{"vendor": "nvidia", "count": 3},
                               {"vendor": "amd", "count": 1}])

    def test_offline_hosts_skipped(self):
        rows = [self._row(vendor="nvidia"), self._row(vendor="amd", online=False)]
        out = app._fleet_vendor_summary(rows)
        self.assertEqual(out, [{"vendor": "nvidia", "count": 1}])

    def test_gpuless_hosts_skipped(self):
        rows = [self._row(vendor="nvidia"), self._row(gpu=False)]
        out = app._fleet_vendor_summary(rows)
        self.assertEqual(out, [{"vendor": "nvidia", "count": 1}])

    def test_absent_vendor_counts_unknown(self):
        rows = [self._row(vendor=None), self._row(vendor="weird-vendor")]
        out = app._fleet_vendor_summary(rows)
        self.assertEqual(out, [{"vendor": "unknown", "count": 2}])

    def test_ordering_nvidia_amd_intel_unknown(self):
        rows = [self._row(vendor="unknown"), self._row(vendor="intel"),
                self._row(vendor="amd"), self._row(vendor="nvidia")]
        out = app._fleet_vendor_summary(rows)
        self.assertEqual([o["vendor"] for o in out],
                         ["nvidia", "amd", "intel", "unknown"])

    def test_no_gpus_returns_empty(self):
        self.assertEqual(app._fleet_vendor_summary([self._row(gpu=False)]), [])
        self.assertEqual(app._fleet_vendor_summary([]), [])

    def test_api_fleet_includes_vendor_summary(self):
        c = app.app.test_client()
        j = c.get("/api/fleet").get_json()
        self.assertIn("gpu_vendors", j)
        self.assertIsInstance(j["gpu_vendors"], list)


if __name__ == "__main__":
    unittest.main()
