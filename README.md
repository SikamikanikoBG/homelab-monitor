# 🛰️ HomeLab Monitor

[![GitHub stars](https://img.shields.io/github/stars/SikamikanikoBG/homelab-monitor?style=social)](https://github.com/SikamikanikoBG/homelab-monitor/stargazers)
[![GitHub forks](https://img.shields.io/github/forks/SikamikanikoBG/homelab-monitor?style=social)](https://github.com/SikamikanikoBG/homelab-monitor/network/members)
[![Clones (14d)](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FSikamikanikoBG%2Fhomelab-monitor%2Fstats%2Fclones.json&style=social&logo=git&cacheSeconds=300)](https://github.com/SikamikanikoBG/homelab-monitor)
[![Unique cloners (14d)](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FSikamikanikoBG%2Fhomelab-monitor%2Fstats%2Fclones-unique.json&style=social&logo=git&cacheSeconds=300)](https://github.com/SikamikanikoBG/homelab-monitor)

![version](https://img.shields.io/badge/version-0.6.2-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![docker](https://img.shields.io/badge/deploy-docker--compose-2496ED?logo=docker&logoColor=white)
![gpu](https://img.shields.io/badge/GPU-NVIDIA-76B900?logo=nvidia&logoColor=white)
[![last commit](https://img.shields.io/github/last-commit/SikamikanikoBG/homelab-monitor?color=informational)](https://github.com/SikamikanikoBG/homelab-monitor/commits/main)

A small, friendly dashboard for a self-hosted home lab. One container gives you a
single page that answers the everyday questions: **is the GPU busy and which model
is using it, are my containers healthy, are my services running, and is the box
itself OK?** — readable from your phone over the VPN.

It's built to be **plug-and-play**: `docker compose up -d --build`, open the page,
done. No agents, no Prometheus/Grafana stack, no cloud, and no config required to
get started. If you're newer to home labs it should just work; if you're more
advanced, everything is a handful of clearly-commented Python functions you can
extend.

![HomeLab Monitor — a quick tour of the tabs](docs/demo.gif)

## What it shows

The page is organised into tabs so it stays readable as it grows:

- **Overview** — a status card per subsystem (GPU, Host, Containers, Services) plus
  plain-language insights, so one glance tells you whether anything needs attention.
- **GPU** — live VRAM / utilisation / power / temp, *which container or process*
  holds the VRAM (mapped automatically, nothing hardcoded), and a VRAM-by-service
  timeline.
- **AI Models** — for recognised model servers, *which model* is loaded and its VRAM,
  read live from the server's own API.
- **Containers** — health of **every** Docker container: running / stopped /
  restarting, and whether its health-check is passing.
- **Services** — **systemd** service health, with the units *you* deployed
  highlighted and any failed unit surfaced first.
- **Host** — CPU, RAM, load, uptime, temperature and disk usage, with history.

History is stored in SQLite and **downsampled on read**, so a six-month view loads
as quickly and reads as cleanly as the last hour.

## Screenshots

**GPU — VRAM by service over time** (who held the card, and when):

![GPU tab](docs/gpu.png)

**Containers** — health of every container at a glance:

![Containers tab](docs/containers.png)

**Services** — systemd health, with your own units highlighted and failures first:

![Services tab](docs/services.png)

**Host** — CPU, RAM, load, temperature and disk usage:

![Host tab](docs/host.png)

## Quick start

Requirements: Docker, and — for the GPU panels — an NVIDIA GPU with the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
(No GPU? The container, service and host panels still work fine.)

```bash
git clone https://github.com/SikamikanikoBG/homelab-monitor.git
cd homelab-monitor
docker compose up -d --build
```

Open **http://<your-host-ip>:9800** from any machine on your LAN or VPN.

## Supported model servers

| Server | Model name | Per-model VRAM |
|---|---|---|
| **Ollama** | ✅ | ✅ via `/api/ps` (validated) |
| **vLLM** | ✅ via `/v1/models` | — |
| **HF TGI** | ✅ via `/info` | — |
| **llama.cpp** | ✅ via `/v1/models` | — |
| **Automatic1111 (SD)** | ✅ via `/sdapi/v1/options` | — |
| **ComfyUI** | detected | — |

Don't see yours? Adding a probe is a one-liner — append to `PROBES` in `app.py`.

## Configuration

Set these under `environment:` in `docker-compose.yml` (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `SAMPLE_INTERVAL` | `10` | Seconds between samples |
| `RETENTION_DAYS` | `180` | How long history is kept |
| `PRESSURE_FREE_MB` | `2048` | Free VRAM below this counts as "pressure" |
| `PORT` | `9800` | Dashboard port |
| `WATCH_CONTAINERS` | — | Extra containers to scan for OOM (comma-separated) |
| `WATCH_SERVICES` | — | systemd units to always show, even vendor ones (comma-separated) |
| `CHECK_UPDATES` | `true` | Set to `false` to disable the daily GitHub-releases check (no outbound calls) |

History lives in `./data/gpu.db` (a bind mount), so it survives restarts and upgrades.

### Alerts (Discord & ntfy.sh)

The **Alerts** tab in the dashboard configures push notifications — no env
vars, no config files, no restart. Either channel can be used; both are
optional.

- **Discord** — paste a channel webhook URL. Alerts arrive as a coloured embed
  (red = critical, orange = warning).
- **ntfy.sh** — set a topic (and optionally a self-hosted ntfy server). Alerts
  arrive with severity-based priority and tags.

Alerts fire on **state changes** (edge-triggered) so they don't spam: container
unhealthy / exited non-zero / dead, systemd unit failed, GPU **VRAM pressure**,
GPU **OOM** events, and disks crossing the configured threshold (default 90 %).
A *Send test alert* button verifies the wiring end-to-end.

If nothing is configured, the feature is off — no external calls, no errors.

### Enabling the Services (systemd) panel

To read systemd health, the container needs the host's D-Bus system socket. The
provided `docker-compose.yml` already mounts it read-only:

```yaml
volumes:
  - /run/dbus/system_bus_socket:/run/dbus/system_bus_socket:ro
```

If your host keeps it elsewhere, adjust the mount and `DBUS_SYSTEM_BUS_ADDRESS`.
Remove the mount and the Services panel simply shows "unavailable" — everything
else keeps working.

## How it works

```
nvidia-smi ─► per-process VRAM + PID ─► /proc/<pid>/cgroup ─► Docker API ─► container name
model servers ─► their own API (/api/ps, /v1/models, …) ─► which model + VRAM
Docker API ─► every container's state + health-check status
systemd (D-Bus) ─► service state, with your own units highlighted
host /proc, /sys, statvfs ─► CPU / RAM / load / temp / disk
        │
     SQLite ─► downsample-on-read ─► single-page dashboard (Chart.js, vendored)
```

A background thread samples on an interval; the web layer buckets any range down to
~360 points so it stays responsive over months.

## Adding your own monitor

The code is intentionally small and modular so contributions are easy:

1. In `app.py`, write a `collect_<thing>()` that returns
   `{"available": bool, "summary": {...}, "items": [...]}`.
2. Call it from `health_scan()` so the background thread keeps it fresh, and expose
   it via `/api/health`.
3. In `static/dashboard.html`, add one entry to the `TABS` array plus a matching
   `<section>` and a small renderer.

That's the whole pattern — no build step, no framework.

## Security notes

This is a host monitor, so it runs with `pid: host`, `network_mode: host`, a
**read-only** Docker socket (to read container names/health and query model APIs),
a **read-only** mount of `/` (for disk usage), and a **read-only** D-Bus socket (for
systemd state). That's a broad footprint by design — please keep it behind your
LAN/VPN/firewall and **don't expose it to the public internet.**

## Prometheus integration

Homelab Monitor exposes a standard Prometheus scrape endpoint at `/metrics` (port
9800 by default). It reads exclusively from the in-memory snapshot that the background
collector keeps fresh — **no extra polling, no double-sampling**.

### Metrics exposed

| Metric | Labels | Description |
|---|---|---|
| `homelab_gpu_vram_used_mb` | `gpu` | GPU VRAM currently used (MB) |
| `homelab_gpu_vram_total_mb` | `gpu` | GPU VRAM total capacity (MB) |
| `homelab_gpu_util_pct` | `gpu` | GPU utilisation (%) |
| `homelab_gpu_temp_c` | `gpu` | GPU temperature (°C) |
| `homelab_gpu_power_w` | `gpu` | GPU power draw (W) |
| `homelab_host_cpu_pct` | — | Host CPU usage (%) |
| `homelab_host_mem_used_pct` | — | Host memory used (%) |
| `homelab_host_disk_used_pct` | `mountpoint` | Disk used per mount (%) |
| `homelab_container_state` | `name`, `state` | 1 = container is in this state |
| `homelab_systemd_unit_state` | `unit`, `state` | 1 = unit is active, 0 = otherwise |
| `homelab_model_loaded_vram_mb` | `server`, `model` | VRAM used by a loaded model (MB) |

### Quick verification

```bash
curl http://<your-host-ip>:9800/metrics
```

### Sample Prometheus scrape config

```yaml
# prometheus.yml
scrape_configs:
  - job_name: 'homelab_monitor'
    scrape_interval: 15s
    static_configs:
      - targets: ['<your-host-ip>:9800']
```

### Grafana dashboard

A ready-to-import dashboard is at
[`docs/grafana/homelab_prometheus_dashboard.json`](docs/grafana/homelab_prometheus_dashboard.json).
In Grafana: **Dashboards → Import → Upload JSON file**, then select your Prometheus
datasource. The dashboard covers GPU VRAM, utilisation, temperature, host CPU/RAM,
disk usage, and model VRAM in a single view.

## Roadmap


A few things that would be nice to add next (PRs very welcome):

- Per-model VRAM history timeline
- Multi-GPU layouts
- Telegram alerting (Discord + ntfy already supported — see **Alerts** tab)
- `systemctl --user` (per-user) service support
- AMD / Intel GPU back-ends

## ⭐ Support the project

If HomeLab Monitor saves you a browser tab or two, a ⭐ on GitHub genuinely helps
other home-labbers find it. Thank you!

## Contributing

Issues and PRs are very welcome — especially new model-server probes, new monitors,
and GPU back-ends. This is a hobby tool meant to help fellow home-labbers, so be kind.

## License

MIT — see [LICENSE](LICENSE).
