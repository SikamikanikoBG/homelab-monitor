"""Validates the shipped Grafana dashboard artifact.

This test is intentionally self-contained: it only READS the checked-in
dashboard JSON at docs/grafana/homelab_prometheus_dashboard.json. It does not
import app.py or touch the running service. Its job is to guarantee that

  * the file is valid JSON and a plausible Grafana dashboard, and
  * every PromQL `expr` in every panel references ONLY metric names that the
    HomeLab Monitor exporter actually publishes.

The allow-list below is derived by hand from the exporter definitions in
app.py (the `_G` prometheus_client gauges near the top + the pure-stdlib
`homelab_*` families built in the /metrics builder). If a new metric is added
to the exporter and charted here, add it to EXPORTER_METRICS too. If a panel
ever references a metric NOT in this list, the test fails — which is exactly
what stops the dashboard from drifting into aspirational/invented metrics.
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD = os.path.join(HERE, os.pardir, "docs", "grafana",
                         "homelab_prometheus_dashboard.json")

# Every metric family the exporter really publishes (verified against a live
# `curl /metrics` and the definitions in app.py). Keep this in sync with the
# exporter; it is the single source of truth this test enforces.
EXPORTER_METRICS = {
    # prometheus_client gauges (_G)
    "homelab_gpu_vram_used_mb",
    "homelab_gpu_vram_total_mb",
    "homelab_gpu_util_pct",
    "homelab_gpu_temp_c",
    "homelab_gpu_power_w",
    "homelab_host_cpu_pct",
    "homelab_host_mem_used_pct",
    "homelab_host_disk_used_pct",
    "homelab_container_state",
    "homelab_systemd_unit_state",
    "homelab_model_loaded_vram_mb",
    # pure-stdlib families
    "homelab_build_info",
    "homelab_power_total_w",
    "homelab_gpu_info",
    "homelab_disk_used_bytes",
    "homelab_disk_total_bytes",
    "homelab_disk_fill_pct",
    "homelab_cost_month_to_date",
    "homelab_cost_month_projected",
    "homelab_anomaly_active",
    "homelab_llm_tokens_per_second",
    "homelab_llm_ttft_ms",
    "homelab_llm_resident_models",
    "homelab_uptime_up",
    "homelab_uptime_latency_ms",
    "homelab_uptime_uptime_ratio",
    "homelab_uptime_checks_total",
    "homelab_uptime_checks_down",
    "homelab_uptime_cert_days_remaining",
    "homelab_uptime_cert_not_after_seconds",
}

_METRIC_TOKEN = re.compile(r"homelab_[a-z0-9_]+")


def _load():
    with open(DASHBOARD, encoding="utf-8") as fh:
        return json.load(fh)


def _all_targets(dashboard):
    for panel in dashboard.get("panels", []):
        for t in panel.get("targets", []) or []:
            yield panel, t
        # collapsed rows may nest panels
        for sub in panel.get("panels", []) or []:
            for t in sub.get("targets", []) or []:
                yield sub, t


def test_dashboard_is_valid_json_and_shape():
    d = _load()
    assert d.get("title")
    assert d.get("uid")
    assert isinstance(d.get("schemaVersion"), int) and d["schemaVersion"] >= 30
    assert isinstance(d.get("panels"), list) and d["panels"]


def test_has_prometheus_datasource_template_variable():
    d = _load()
    tvars = d.get("templating", {}).get("list", [])
    ds_vars = [v for v in tvars
               if v.get("type") == "datasource" and v.get("query") == "prometheus"]
    assert ds_vars, "expected a templating datasource variable of type prometheus"


def test_every_panel_expr_references_only_real_metrics():
    d = _load()
    seen = set()
    for panel, target in _all_targets(d):
        expr = target.get("expr", "")
        assert expr, f"panel {panel.get('id')} has a target with no expr"
        for tok in _METRIC_TOKEN.findall(expr):
            seen.add(tok)
            assert tok in EXPORTER_METRICS, (
                f"panel {panel.get('id')!r} references unknown metric {tok!r} "
                f"(not published by the exporter)"
            )
    # sanity: the dashboard actually charts a meaningful chunk of the exporter
    assert len(seen) >= 15, f"dashboard only references {len(seen)} metrics"


def test_gpu_info_vendor_join_is_present():
    """The headline feature: label GPU series by vendor/name via an
    on(gpu) group_left(...) join against homelab_gpu_info."""
    d = _load()
    joined = [t.get("expr", "") for _, t in _all_targets(d)
              if "homelab_gpu_info" in t.get("expr", "")]
    assert joined, "expected at least one panel joining against homelab_gpu_info"
    assert any("group_left" in e and "on(gpu)" in e.replace(" ", "")
               for e in joined), \
        "expected an on(gpu) group_left join against homelab_gpu_info"
