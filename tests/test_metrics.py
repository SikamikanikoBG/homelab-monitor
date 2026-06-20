"""Tests for the Prometheus / OpenMetrics /metrics endpoint — the pure-stdlib
extra families (total power, per-disk bytes + fill %, month-cost projection,
anomaly flags) and valid exposition (no duplicate HELP/TYPE, numeric values)."""
import os
import re
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def parse_exposition(text):
    """Minimal exposition parser: returns (help_names, type_names, samples).
    Validates every non-comment line is `name{labels} value` with a numeric
    value (or NaN). Raises AssertionError on a malformed line."""
    helps, types, samples = [], [], {}
    line_re = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)(\s+\S+)?$')
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if line.startswith("# HELP "):
            helps.append(line.split()[2]); continue
        if line.startswith("# TYPE "):
            types.append(line.split()[2]); continue
        if line.startswith("#"):
            continue
        m = line_re.match(line)
        assert m, f"malformed metric line: {line!r}"
        name, _labels, val = m.group(1), m.group(2), m.group(3)
        if val != "NaN":
            float(val)  # raises ValueError if not numeric
        samples.setdefault(name, []).append((line, val))
    return helps, types, samples


class TestMetricsEndpoint(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()
        self._host = app.LATEST.get("host")
        self._power = app.LATEST.get("power")
        self._cpu = app.LATEST.get("cpu_power")
        self._dram = app.LATEST.get("dram_power")

    def tearDown(self):
        app.LATEST["host"] = self._host
        app.LATEST["power"] = self._power
        app.LATEST["cpu_power"] = self._cpu
        app.LATEST["dram_power"] = self._dram

    def test_200_text_and_parses(self):
        r = self.c.get("/metrics")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/plain", r.content_type)
        helps, types, samples = parse_exposition(r.get_data(as_text=True))
        # exposition validity: no duplicate HELP or TYPE for any metric name
        self.assertEqual(len(helps), len(set(helps)), f"duplicate HELP: {helps}")
        self.assertEqual(len(types), len(set(types)), f"duplicate TYPE: {types}")
        # every TYPE'd family has a matching HELP
        for t in types:
            self.assertIn(t, helps, f"TYPE without HELP: {t}")

    def test_expected_metric_names_present(self):
        app.LATEST["power"], app.LATEST["cpu_power"], app.LATEST["dram_power"] = 250.0, 40.0, 5.0
        app.LATEST["host"] = {"disks": [{"mount": "/", "used": 100.0, "total": 500.0, "pct": 20}]}
        body = self.c.get("/metrics").get_data(as_text=True)
        for name in ("homelab_build_info", "homelab_power_total_w",
                     "homelab_disk_used_bytes", "homelab_disk_total_bytes",
                     "homelab_disk_fill_pct", "homelab_anomaly_active"):
            self.assertIn(name, body, f"missing metric: {name}")

    def test_total_power_value(self):
        app.LATEST["power"], app.LATEST["cpu_power"], app.LATEST["dram_power"] = 200.0, 50.0, 10.0
        body = self.c.get("/metrics").get_data(as_text=True)
        _h, _t, samples = parse_exposition(body)
        line, val = samples["homelab_power_total_w"][0]
        self.assertAlmostEqual(float(val), 260.0, places=3)

    def test_disk_bytes_conversion(self):
        app.LATEST["host"] = {"disks": [{"mount": "/data", "used": 1.0, "total": 2.0, "pct": 50}]}
        body = self.c.get("/metrics").get_data(as_text=True)
        _h, _t, samples = parse_exposition(body)
        used = [v for l, v in samples["homelab_disk_used_bytes"] if "/data" in l]
        self.assertTrue(used)
        self.assertAlmostEqual(float(used[0]), 1024 ** 3, delta=1.0)  # 1 GB -> bytes

    def test_anomaly_flag_one_per_series(self):
        body = self.c.get("/metrics").get_data(as_text=True)
        _h, _t, samples = parse_exposition(body)
        flags = samples.get("homelab_anomaly_active", [])
        # one sample per known anomaly series
        self.assertEqual(len(flags), len(app._ANOMALY_SERIES))
        for _l, v in flags:
            self.assertIn(float(v), (0.0, 1.0))

    def test_extra_text_helper_standalone(self):
        # The pure-stdlib block on its own must also be valid exposition.
        helps, types, _s = parse_exposition(app._extra_metrics_text())
        self.assertEqual(len(helps), len(set(helps)))
        self.assertEqual(len(types), len(set(types)))


if __name__ == "__main__":
    unittest.main()
