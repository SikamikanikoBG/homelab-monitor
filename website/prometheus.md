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

!!! note
    Prometheus is entirely optional — the built-in dashboard is fully self-contained
    without it. The endpoint is there for folks who already run a Prometheus/Grafana stack.
