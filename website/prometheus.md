# Prometheus & Grafana

HomeLab Monitor exposes a standard Prometheus scrape endpoint at `/metrics` (port 9800
by default). It reads exclusively from the in-memory snapshot that the background
collector keeps fresh — **no extra polling, no double-sampling**.

## Metrics exposed

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `homelab_gpu_vram_used_mb` | gauge | `gpu` | GPU VRAM currently used (MB) |
| `homelab_gpu_vram_total_mb` | gauge | `gpu` | GPU VRAM total capacity (MB) |
| `homelab_gpu_util_pct` | gauge | `gpu` | GPU utilisation (%) |
| `homelab_gpu_temp_c` | gauge | `gpu` | GPU temperature (°C) |
| `homelab_gpu_power_w` | gauge | `gpu` | GPU power draw (W) |
| `homelab_gpu_info` | gauge | `host`, `gpu`, `name`, `vendor` | GPU identity; value is always `1`, the card's `host`/`gpu`/`name`/`vendor` live in the labels (info-series pattern) |
| `homelab_host_cpu_pct` | gauge | — | Host CPU usage (%) |
| `homelab_host_mem_used_pct` | gauge | — | Host memory used (%) |
| `homelab_host_disk_used_pct` | gauge | `mountpoint` | Disk used per mount (%) |
| `homelab_container_state` | gauge | `name`, `state` | `1` = the container is in this state |
| `homelab_systemd_unit_state` | gauge | `unit`, `state` | `1` = the unit is in this state (e.g. `active`) |
| `homelab_model_loaded_vram_mb` | gauge | `server`, `model` | VRAM used by a loaded model (MB) |
| `homelab_build_info` | gauge | `version` | Always `1`; app version in the label |
| `homelab_power_total_w` | gauge | — | Total machine power draw (GPU + CPU + DRAM, W) |
| `homelab_disk_used_bytes` | gauge | `mountpoint` | Filesystem used space (bytes) |
| `homelab_disk_total_bytes` | gauge | `mountpoint` | Filesystem total space (bytes) |
| `homelab_disk_fill_pct` | gauge | `mountpoint` | Filesystem fill (%) |
| `homelab_cost_month_to_date` | gauge | `currency` | Energy cost so far this month — emitted only when an energy price is configured |
| `homelab_cost_month_projected` | gauge | `currency` | Projected full-month energy cost — emitted only when an energy price is configured |
| `homelab_anomaly_active` | gauge | `series`, `direction` | `1` = the series is flagged anomalous right now, `0` = normal |
| `homelab_llm_tokens_per_second` | gauge | `model` | LLM generation throughput, latest real measurement (tokens/s) — appears once a copilot generation has run |
| `homelab_llm_ttft_ms` | gauge | `model` | LLM time-to-first-token, latest measurement (ms) — appears once a copilot generation has run |
| `homelab_llm_resident_models` | gauge | — | Models currently loaded in ollama (count) — emitted when ollama is reachable |
| `homelab_uptime_up` | gauge | `check` | Uptime check current state (`1` = up, `0` = down; `unknown` omitted) |
| `homelab_uptime_latency_ms` | gauge | `check` | Uptime check last measured latency (ms) |
| `homelab_uptime_uptime_ratio` | gauge | `check` | Uptime check uptime fraction over the window (0..1) |
| `homelab_uptime_checks_total` | gauge | — | Configured uptime checks (count) |
| `homelab_uptime_checks_down` | gauge | — | Uptime checks currently down (count) |
| `homelab_uptime_cert_days_remaining` | gauge | `check` | TLS certificate days remaining per cert check (negative = expired) |
| `homelab_uptime_cert_not_after_seconds` | gauge | `check` | TLS certificate expiry as a POSIX timestamp (seconds) per cert check |

The GPU/host/container/model families come from the `prometheus_client` library;
the total-power, GPU-info, per-disk-bytes, month-cost, anomaly-flag, LLM and uptime
families are built with pure-stdlib string formatting, so the endpoint stays rich even
on a build without `prometheus_client` installed. The `homelab_uptime_*` families
appear only when at least one external uptime check is configured, and the two
`homelab_cost_month_*` families only when an energy price is set — a design choice so
a `0` never masquerades as a real reading.

!!! note "Where's disk I/O?"
    Per-device disk I/O (read/write MB/s, busy %) is sampled and charted **inside the
    dashboard's own history views**, but it is not currently published on `/metrics`.
    Only the metrics listed above are exported — everything you scrape here exists.

## The `homelab_gpu_info` info-series and the vendor join

`homelab_gpu_info` is a classic Prometheus *info* series: its value is always `1` and
all the useful data (`host`, `gpu`, `name`, `vendor`) lives in the labels. The numeric
GPU gauges (`homelab_gpu_util_pct`, `homelab_gpu_power_w`, …) keep their lean
`{gpu="gpu0"}` label set untouched, so existing recording rules that key on `gpu` are
unaffected. To *decorate* those numeric series with the card's real name and vendor,
join against the info-series with `group_left`:

```promql
# GPU utilisation, labelled by the real card name + vendor
homelab_gpu_util_pct * on(gpu) group_left(vendor, name) homelab_gpu_info
```

Example live series:

```text
homelab_gpu_info{host="ArDi",gpu="gpu0",name="NVIDIA GeForce RTX 3090",vendor="nvidia"} 1
```

The same pattern works for power, temperature and VRAM — swap the left-hand metric.

## Quick verification

```bash
curl http://<your-host>:9800/metrics
```

## Sample scrape config

```yaml
# prometheus.yml
scrape_configs:
  - job_name: 'homelab_monitor'
    scrape_interval: 15s
    static_configs:
      - targets: ['<your-host>:9800']
```

## Import the Grafana dashboard

A ready-to-import dashboard is checked in at
[`docs/grafana/homelab_prometheus_dashboard.json`](https://github.com/SikamikanikoBG/homelab-monitor/blob/main/docs/grafana/homelab_prometheus_dashboard.json).

In Grafana: **Dashboards → Import → Upload JSON file**, then pick your Prometheus
datasource when prompted (the dashboard ships a `datasource` template variable of type
`prometheus`, plus `gpu` and `mountpoint` variables so multi-GPU / multi-mount fleets
filter cleanly). Panels cover:

- **GPU** — utilisation, VRAM used %, temperature and power, with a vendor/model-labelled
  utilisation chart driven by the `homelab_gpu_info` join above.
- **Host & disk** — CPU %, RAM %, per-mount fill %, used/total bytes and total machine power.
- **Containers & units** — running-container count plus `homelab_container_state` /
  `homelab_systemd_unit_state` tables.
- **LLM / AI** — resident model count, tokens/s and TTFT, loaded-model VRAM and live
  anomaly flags.
- **Cost & uptime** — month-to-date and projected energy cost, uptime checks up/down,
  per-check latency and uptime ratio, and TLS certificate days-remaining.

## Alerting & recording rules

A ready-to-load Prometheus rule pack ships alongside the dashboard at
[`docs/grafana/alerts.rules.yml`](https://github.com/SikamikanikoBG/homelab-monitor/blob/main/docs/grafana/alerts.rules.yml).
Every expression is keyed on a metric this exporter **actually** publishes (see the
catalogue above) — nothing aspirational — so the rules light up the moment the
matching series appear.

Point Prometheus at it via `rule_files:`:

```yaml
# prometheus.yml
rule_files:
  - alerts.rules.yml     # path relative to your prometheus.yml (or an absolute path)

scrape_configs:
  - job_name: 'homelab_monitor'   # the HomelabExporterDown alert matches this job name
    scrape_interval: 15s
    static_configs:
      - targets: ['<your-host>:9800']
```

Reload with `curl -X POST http://<prometheus>:9090/-/reload` (or SIGHUP), then check
**Status → Rules** in the Prometheus UI. Validate locally first with
`promtool check rules docs/grafana/alerts.rules.yml` if you have the Prometheus
toolkit installed.

### Recording rules

| Recorded series | Expression | Use |
|---|---|---|
| `homelab:gpu_vram_fill:ratio` | `homelab_gpu_vram_used_mb / homelab_gpu_vram_total_mb` | VRAM fill fraction (0..1) per GPU — the same expression the VRAM alert watches |
| `homelab:gpu_util_pct:by_vendor` | `homelab_gpu_util_pct * on(gpu) group_left(vendor, name) homelab_gpu_info` | GPU utilisation decorated with the card's real `vendor` + `name` via the info-series join |

### Alerts

| Alert | Expr (summary) | Severity | Meaning |
|---|---|---|---|
| `HomelabGpuTempHigh` | `homelab_gpu_temp_c > 85` for 5m | warning | GPU running hot |
| `HomelabGpuTempCritical` | `homelab_gpu_temp_c > 92` for 2m | critical | Thermal-throttle / damage risk |
| `HomelabGpuVramNearFull` | `homelab_gpu_vram_used_mb / homelab_gpu_vram_total_mb > 0.95` for 10m | warning | VRAM OOM / model-eviction risk |
| `HomelabPowerDrawHigh` | `homelab_power_total_w > 500` for 15m | warning | Sustained high total draw (PSU / cost) |
| `HomelabDiskFillHigh` | `homelab_disk_fill_pct > 90` for 15m | warning | Filesystem over 90% |
| `HomelabDiskFillCritical` | `homelab_disk_fill_pct > 97` for 5m | critical | Filesystem almost full |
| `HomelabHostCpuSaturated` | `homelab_host_cpu_pct > 90` for 15m | warning | Host CPU saturated |
| `HomelabHostMemoryHigh` | `homelab_host_mem_used_pct > 90` for 15m | warning | Host memory pressure / OOM risk |
| `HomelabContainerNotRunning` | `homelab_container_state{state=~"exited|dead|restarting"} == 1` for 5m | warning | A tracked container left the running state |
| `HomelabSystemdUnitFailed` | `homelab_systemd_unit_state{state="failed"}` for 5m | warning | A systemd unit entered the failed state |
| `HomelabUptimeCheckDown` | `homelab_uptime_up == 0` for 2m | critical | A monitored endpoint is down |
| `HomelabTlsCertExpiringSoon` | `homelab_uptime_cert_days_remaining < 14` for 1h | warning | TLS cert expiring within 14 days |
| `HomelabTlsCertExpired` | `homelab_uptime_cert_days_remaining < 0` for 5m | critical | TLS cert already expired |
| `HomelabAnomalyActive` | `homelab_anomaly_active > 0` for 5m | warning | Built-in z-score detector flagged a series |
| `HomelabMonthlyCostOverBudget` | `homelab_cost_month_projected > 50` for 1h | warning | Projected month cost over a template budget |
| `HomelabExporterDown` | `up{job="homelab_monitor"} == 0` for 2m | critical | Prometheus can't scrape the exporter |

A few expressions encode this exporter's exact value semantics, worth knowing before
you tune them:

- **`homelab_container_state`** carries a value of `1` for a container's *current*
  state only, so the alert matches the trouble states (`exited|dead|restarting`)
  directly rather than testing an (absent) `running` series.
- **`homelab_systemd_unit_state`** stores active(`1`)/not-active(`0`) in the *value*
  and the literal state in the `state` label — so a failed unit reads as
  `{state="failed"} 0`. The alert fires on the mere presence of that series
  (Prometheus alerts fire per matching element regardless of the sample value).
- The **`homelab_cost_month_*`** and **`homelab_uptime_*`** (incl. TLS cert) families
  are emitted only when a price / uptime checks are configured; their rules stay
  dormant until the series exist.

!!! tip "Thresholds are examples — tune them"
    Every threshold and `for:` window above is a sensible **starting point**, not a
    universal truth. Adjust them to your hardware, workload and budget (e.g. raise the
    `homelab_power_total_w` and `homelab_cost_month_projected` limits for a busy
    training rig), and rename the `homelab_monitor` job matcher in `HomelabExporterDown`
    if your scrape `job_name` differs.

The same series drive the ready-to-import Grafana dashboard at
[`docs/grafana/homelab_prometheus_dashboard.json`](https://github.com/SikamikanikoBG/homelab-monitor/blob/main/docs/grafana/homelab_prometheus_dashboard.json)
(see [Import the Grafana dashboard](#import-the-grafana-dashboard) above), so alerts and
panels stay in lock-step.

!!! note
    Prometheus is entirely optional — the built-in dashboard is fully self-contained
    without it. The endpoint is there for folks who already run a Prometheus/Grafana stack.
