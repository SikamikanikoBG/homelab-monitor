# 🛰️ HomeLab Monitor

[![GitHub stars](https://img.shields.io/github/stars/SikamikanikoBG/homelab-monitor?style=social)](https://github.com/SikamikanikoBG/homelab-monitor/stargazers)
[![GitHub forks](https://img.shields.io/github/forks/SikamikanikoBG/homelab-monitor?style=social)](https://github.com/SikamikanikoBG/homelab-monitor/network/members)
[![Clones (14d)](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FSikamikanikoBG%2Fhomelab-monitor%2Fstats%2Fclones.json&style=social&logo=git&cacheSeconds=300)](https://github.com/SikamikanikoBG/homelab-monitor)
[![Unique cloners (14d)](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FSikamikanikoBG%2Fhomelab-monitor%2Fstats%2Fclones-unique.json&style=social&logo=git&cacheSeconds=300)](https://github.com/SikamikanikoBG/homelab-monitor)

[![website](https://img.shields.io/badge/docs-sikamikanikobg.github.io%2Fhomelab--monitor-d29922?logo=readthedocs&logoColor=white)](https://sikamikanikobg.github.io/homelab-monitor/)
![version](https://img.shields.io/badge/version-0.10.0-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![docker](https://img.shields.io/badge/deploy-docker--compose-2496ED?logo=docker&logoColor=white)
![gpu](https://img.shields.io/badge/GPU-NVIDIA-76B900?logo=nvidia&logoColor=white)
[![last commit](https://img.shields.io/github/last-commit/SikamikanikoBG/homelab-monitor?color=informational)](https://github.com/SikamikanikoBG/homelab-monitor/commits/main)

> 🆕 **v0.11.0** — **AI Models, upgraded**: every recognised model server now shows even while **Idle** (Ollama, vLLM, llama.cpp, LocalAI, faster-whisper, Stable Diffusion, ComfyUI and ~15 more), so nothing flickers away when a model unloads — plus a **"Driven by"** breakdown revealing *which services are calling each server* (who's actually driving Ollama). [Release notes →](https://github.com/SikamikanikoBG/homelab-monitor/releases)
>
> 🛰️ **Source, issues and roadmap live on [GitHub](https://github.com/SikamikanikoBG/homelab-monitor).** If HomeLab Monitor saves you a browser tab, a ⭐ there genuinely helps other home-labbers find it.

A small, friendly dashboard for a self-hosted home lab. One container gives you a
single page that answers the everyday questions: **is the GPU busy and which model
is using it, are my containers healthy, are my services running, and is the box
itself OK?** — readable from your phone over the VPN. Since 0.8, it also covers
**multiple machines**: register your other boxes over SSH and see every host's
vitals side-by-side in one cockpit.

It's built to be **plug-and-play**: `docker compose up -d --build`, open the page,
done. No agents, no Prometheus/Grafana stack, no cloud, and no config required to
get started. If you're newer to home labs it should just work; if you're more
advanced, everything is a handful of clearly-commented Python functions you can
extend.

![HomeLab Monitor — a quick tour of the tabs](docs/demo.gif)

## What it shows

A **host pill bar** at the top lets you switch between the local box and any
remote you've registered. The page is organised into tabs that scope to the
active host:

- **Overview** — every registered host side-by-side: CPU% + cores, RAM % + GB,
  GPU util + VRAM, load, uptime, temp, disks. Click a row to focus that host in
  the other tabs.
- **GPU** — live VRAM / utilisation / power / temp, *which container or process*
  holds the VRAM (mapped automatically, nothing hardcoded), and a VRAM-by-service
  timeline.
- **AI Models** — every recognised model server (Ollama, vLLM, llama.cpp, faster-whisper,
  Stable Diffusion, ComfyUI, …), *which model* is loaded and its VRAM, read live from the
  server's own API. Servers stay listed as **Idle** when their model unloads (nothing
  flickers away), and a **"Driven by"** breakdown shows *which services are calling each
  server* — so you can see what's actually driving Ollama.
- **Containers** — health of **every** Docker container with uptime, memory,
  disk footprint and **clickable port chips** that open `host:port` in a new tab.
- **Services** — **systemd** service health for the active host (local *or*
  remote), with the units *you* deployed highlighted, any failed unit surfaced
  first, plus per-unit uptime, memory, and listen ports.
- **System** — CPU, RAM, load, uptime, temperature and disk usage, **plus** a
  full inventory panel: OS + version, kernel, **architecture**, init system,
  bare-metal/VM detection, machine model, CPU model & topology, and GPU.
  History is local-only for now; live KPIs + inventory work for any host.
- **Network** — per-host interfaces (IPv4/IPv6, MAC, link state, speed, MTU,
  RX/TX), default gateway, DNS resolvers, and a listening-socket table that
  flags which ports are bound to **all interfaces** vs localhost.
- **Security** — a read-only posture check per host: firewall (ufw/firewalld/
  nftables), SSH hardening (root login / password auth), SELinux/AppArmor,
  fail2ban, reboot-pending and auto-updates — **issues surfaced first**, with
  anything that needs root to read marked clearly rather than guessed.
- **Hosts** — a registry and onboarding wizard: paste the hub's public key,
  add a host, run a per-capability **Test connection** (SSH / `/proc` / Docker
  socket / systemd / `nvidia-smi`), and use **▶ Run on remote** to execute the
  fix command on the remote without leaving the dashboard.

History is stored in SQLite and **downsampled on read**, so a six-month view loads
as quickly and reads as cleanly as the last hour.

## Screenshots

**Overview** — every registered host at a glance, polling every 10 s:

![Overview / All hosts](docs/overview.png)

**GPU — VRAM by service over time** (who held the card, and when):

![GPU tab](docs/gpu.png)

**Containers** — health of every container at a glance:

![Containers tab](docs/containers.png)

**Services** — systemd health for any registered host, your own units highlighted and failures first:

![Services tab](docs/services.png)

**System** — KPIs + disks, plus the OS / architecture / hardware inventory for any host:

![System tab](docs/system.png)

**Network** — interfaces, DNS, and listening sockets with exposure flags:

![Network tab](docs/network.png)

**Security** — firewall, SSH hardening, MAC, fail2ban, reboot & updates — issues first:

![Security tab](docs/security.png)

**Hosts** — registry + onboarding wizard with the per-capability checklist:

![Hosts tab](docs/hosts.png)

## Multi-machine monitoring

Since 0.8 the hub watches more than its own box. **Open the Hosts tab**, paste
the hub's auto-generated SSH key onto each remote you want to monitor, and the
hub will start polling it. No agents, no installs, just SSH + Python 3.

![Hub-and-spokes architecture](docs/architecture.svg)

**Adding a host (4 steps, no guessing):**

1. Open **Hosts** → click **🔍 Scan LAN** to suggest reachable boxes (ARP cache
   + TCP-22 sweep), or type a host directly as `user@host[:port]`.
2. Hit **Add host**, then **Test**. The wizard runs a capability checklist:
   - ✅ SSH reachable (port + ms)
   - ✅ Detected OS (e.g. `Ubuntu 22.04.5 LTS · systemd`)
   - ✅ `/proc` readable
   - ⚠️ Docker socket — if `'arsen' not in the docker group`, the exact
     remediation appears inline: `sudo usermod -aG docker arsen`.
   - ✅ systemd D-Bus
   - ℹ️ `nvidia-smi` (or "not found — GPU panel will be hidden")
3. For amber/red rows, click **📋 Copy** or **▶ Run on remote** — if the
   command needs `sudo`, the panel asks for the sudo password with a clear
   *"used once, not stored, never in argv"* note. Output streams back inline.
4. Done — the hub starts polling. Switch to **Overview** to see the new row
   populate within one poll cycle (~10 s).

**Where data comes from.** The hub pipes a small self-contained `probe.py`
through SSH (`ssh user@host python3 -`). Nothing persists on the remote;
the script reads `/proc/*`, `/sys/*` and a handful of config files (for the
OS / hardware / network / security inventory), optionally runs `nvidia-smi`
and `systemctl list-units`, and prints one JSON blob back. It's pure-stdlib
Python 3.6+ and degrades field-by-field, so it runs the same on x86_64, arm64
and i686. The image installs
`openssh-client` and `ssh-keygen` for this — the SSH key lives under
`./data/.ssh/` so it survives rebuilds.

**Security.** Pubkey auth only (passwords disabled). Per-host SSH timeouts so
a slow remote can never block the loop. The "Run on remote" sudo password is
piped via stdin to the remote `sudo -S` — never appears in argv on either side
and is never persisted to SQLite or logs.

**What this slice covers vs. what's coming:** Overview, System, Network,
Security and Services tabs work for any registered host that supports them.
GPU / AI Models / Containers tabs are local-only for now and tell you exactly why ("cloudy has
no NVIDIA GPU", "Docker not installed on cloudy", etc.) using the host's
capability check — per-host versions land in subsequent releases. See
[#35](https://github.com/SikamikanikoBG/homelab-monitor/issues/35) for the
broader design and follow-up slices.

## Quick start
Requirements: Docker, and — for the GPU panels — an NVIDIA GPU with the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
(No GPU? The container, service and host panels still work fine.)

**Option A — pre-built image (recommended).** No clone, no build:

```bash
curl -fsSLO https://raw.githubusercontent.com/SikamikanikoBG/homelab-monitor/main/docker-compose.yml
docker compose pull
docker compose up -d
```

Multi-arch images (`linux/amd64`, `linux/arm64`) are published to
[`sikamikaniko123/homelab-monitor`](https://hub.docker.com/r/sikamikaniko123/homelab-monitor)
on every release. To upgrade later: `docker compose pull && docker compose up -d`.

**Option B — from source** (build it yourself, handy if you're tweaking the code):

```bash
git clone https://github.com/SikamikanikoBG/homelab-monitor.git
cd homelab-monitor
docker compose up -d --build
```

Either way, open **http://<your-host-ip>:9800** from any machine on your LAN or VPN.

## Supported model servers

Recognised even while **Idle**, so the server stays on the dashboard when its model is unloaded. Per-model VRAM comes from the server's API where available, otherwise it's attributed from `nvidia-smi`.

| Server | Model name | Per-model VRAM |
|---|---|---|
| **Ollama** | ✅ loaded + pulled catalogue | ✅ via `/api/ps` (validated) |
| **vLLM** | ✅ via `/v1/models` | attributed |
| **llama.cpp / llama-server** | ✅ via `/v1/models` | attributed |
| **LocalAI** | ✅ via `/v1/models` | attributed |
| **HF TGI / TEI** | ✅ via `/info` | attributed |
| **faster-whisper / Speaches** | ✅ via `/v1/models` | attributed |
| **koboldcpp** | ✅ via `/api/v1/model` | attributed |
| **tabbyAPI · text-generation-webui · LM Studio · xinference · Aphrodite · Infinity** | ✅ via `/v1/models` | attributed |
| **SGLang · OpenLLM · LiteLLM · GPUStack · Cortex / Jan · Ramalama · Nexa · mistral.rs** | ✅ via `/v1/models` | attributed |
| **LoRAX** | ✅ via `/info` | attributed |
| **Whisper ASR webservice / WhisperX** | ✅ up via `/openapi.json` (single entry) | attributed |
| **Wyoming (HA voice: faster-whisper / Piper / openWakeWord)** | ✅ via `describe` over TCP | attributed |
| **OpenedAI-Speech** | ✅ via `/v1/models` | attributed |
| **NVIDIA Triton** | ✅ up via `/v2` (single entry) | attributed |
| **Stable Diffusion (A1111 / Forge / SD.Next)** | ✅ via `/sdapi/v1/options` | attributed |
| **InvokeAI** | ✅ via `/api/v2/models/` | attributed |
| **ComfyUI** | ✅ checkpoints via `/object_info` | attributed |

Don't see yours? Adding a probe is a one-liner — append to `PROBES` in `app.py`. Most servers speak the OpenAI `/v1/models` shape and differ only by port.

### Who's calling? (caller attribution)

Model-server APIs never reveal *who* is calling them. The monitor works it out from the
outside: it samples each container's own established connections and matches the remote
port to a model server, then attributes **connection-time per caller → server** — surfaced
as the **"Driven by"** breakdown on each server card. It's sampled, so long LLM streams are
tracked reliably while sub-second calls (e.g. embeddings) are approximate; the hub's own
probe traffic is excluded.

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

The hub stitches several live sources into one view:

- **`nvidia-smi`** → per-process VRAM + PID → mapped to container via `/proc/<pid>/cgroup` + the Docker API
- **Model-server APIs** (Ollama `/api/ps`, OpenAI-style `/v1/models`, A1111, TGI, …) → which model is loaded and its VRAM
- **`/proc/<pid>/net/tcp`** → each container's own established connections → matched to a model-server port for **caller attribution** ("who's driving Ollama")
- **Docker API** → every container's state + health-check status
- **systemd D-Bus** → service state, with your own units highlighted
- **Host `/proc`, `/sys`, `statvfs`** → CPU / RAM / load / temp / disk

Everything is sampled by a background thread on an interval, persisted to
**SQLite**, and read with downsampling so any range — last hour or last six
months — stays fast and readable. The dashboard is a single page with vendored
Chart.js: no build step, no framework, no cloud round-trips.

The container also exposes a tiny **`GET /healthz`** liveness endpoint (no DB, no
locks — just a 200 with the running version) that the image's `HEALTHCHECK` polls
every 30s. The Containers tab therefore lists the monitor itself as `(healthy)`,
and any container orchestrator can pick it up the same way.

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
