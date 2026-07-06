# How it works

The hub stitches several live sources into one view:

- **`nvidia-smi`** → per-process VRAM + PID → mapped to a container via
  `/proc/<pid>/cgroup` + the Docker API.
- **Model-server APIs** (Ollama `/api/ps`, OpenAI-style `/v1/models`, A1111, TGI, …) →
  which model is loaded and its VRAM. See [Model servers](model-servers.md).
- **`/proc/<pid>/net/tcp`** → each container's own established connections → matched to a
  model-server port for **caller attribution** ("who's driving Ollama").
- **Docker API** → every container's state + health-check status.
- **systemd D-Bus** → service state, with your own units highlighted.
- **Host `/proc`, `/sys`, `statvfs`** → CPU / RAM / load / temperature / disk.

Everything is sampled by a background thread on an interval, persisted to **SQLite**, and
read with downsampling so any range — last hour or last six months — stays fast and
readable. The dashboard is a single page with vendored Chart.js: no build step, no
framework, no cloud round-trips.

The container also exposes a tiny **`GET /healthz`** liveness endpoint (no DB, no locks —
just a `200` with the running version) that the image's `HEALTHCHECK` polls every 30 s.
The Containers tab therefore lists the monitor itself as `(healthy)`, and any container
orchestrator can pick it up the same way.

## Security model

This is a host monitor, so it runs with `pid: host`, `network_mode: host`, a
**read-write** Docker socket, and a **read-write** D-Bus socket (for systemd
state) — read-write by default because the dashboard's write actions
(one-click self-update, and the Containers/Services tabs' start/stop/restart
controls) are on by default too; see
[Configuration → Write actions](configuration.md#write-actions-allow_self_update-enable_controls)
to lock either or both back down. `/` is still mounted **read-only** (for disk
usage). The dashboard has no login — that's a broad footprint by design —
**please keep it behind your LAN/VPN/firewall and don't expose it to the
public internet.**

For the multi-host registry: pubkey auth only (passwords disabled), per-host SSH
timeouts so a slow remote can never block the loop, and the "Run on remote" sudo
password is piped via stdin to the remote `sudo -S` — it never appears in argv on
either side and is never persisted to SQLite or logs.

## Adding your own monitor

The code is intentionally small and modular so contributions are easy:

1. In `app.py`, write a `collect_<thing>()` that returns
   `{"available": bool, "summary": {...}, "items": [...]}`.
2. Call it from `health_scan()` so the background thread keeps it fresh, and expose it
   via `/api/health`.
3. In `static/dashboard.html`, add one entry to the `TABS` array plus a matching
   `<section>` and a small renderer.

That's the whole pattern — no build step, no framework. See [Contributing](contributing.md).
