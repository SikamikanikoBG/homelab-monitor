# Changelog

All notable changes to **HomeLab Monitor** are documented here. The format is
loosely based on [Keep a Changelog](https://keepachangelog.com/), and the
project follows semantic-ish versioning. Each entry links to its full GitHub
release notes.

## [0.14.4](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.14.4) — 2026-06-10
- **Fix:** Services tab now refreshes immediately when you switch hosts. The host-switch path in `refreshCurrentTab()` was missing a `services` branch, so Services only re-rendered on tab-change instead of right away. Closes #82. Thanks @mohd-ibadullah for the first-time contribution.

## [0.14.3](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.14.3) — 2026-06-09
- **Fix:** the built-in MCP server over HTTP rejected remote clients with **421
  Misdirected Request**. The MCP SDK's DNS-rebinding guard only trusts a
  `localhost` Host header, so the documented `claude mcp add --transport http
  homelab http://YOUR-HUB:9810/mcp` failed whenever the hub was reached by name or
  IP. The HTTP/SSE transport now disables that localhost-only check by default
  (safe — every tool is read-only), and a new `MCP_ALLOWED_HOSTS` (plus optional
  `MCP_ALLOWED_ORIGINS`) env lets you lock it back down to specific hosts. stdio
  is unaffected.

## [0.14.2](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.14.2) — 2026-06-09
- **Fix:** correct the `io.modelcontextprotocol.server.name` image label to match the
  publisher namespace casing (`io.github.SikamikanikoBG/homelab-monitor`) so the
  image passes the official MCP Registry's OCI ownership check. `server.json` trimmed
  to a ≤100-char description and the placeholder remote dropped (OCI/stdio entry is
  the canonical one for a self-hosted server). Discoverability only — no runtime changes.

## [0.14.1](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.14.1) — 2026-06-09
- **MCP registry packaging** — the image is now publishable to the official
  [MCP Registry](https://registry.modelcontextprotocol.io): added the
  `io.modelcontextprotocol.server.name` image label and a root `server.json`
  (OCI + streamable-HTTP entries). Also added a `smithery.yaml` so the server can
  be catalogued on [Smithery](https://smithery.ai), and a 400×400 logo. No runtime
  changes — discoverability only.

## [0.14.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.14.0) — 2026-06-09
- **Built-in MCP server** — a first-class [Model Context Protocol](https://modelcontextprotocol.io)
  server now ships **inside the same image**, so Claude (or any MCP client) can
  connect to the monitor and explore the whole homelab as named, **read-only**
  tools — no extra container. Served on `MCP_PORT` (default **9810**) alongside the
  dashboard; connect with `claude mcp add --transport http homelab http://YOUR-HUB:9810/mcp`.
  Full dashboard parity: `list_hosts`, `get_host`, `get_snapshot`, `get_containers`,
  `get_services`, `get_memory` (per-service/per-process RAM), `get_gpu` (util/VRAM/
  power + caller attribution), `get_ai_models`, `get_history` (charted series),
  `get_events`/`get_alerts`, and `scan_disk` (WizTree-style folder treemap) — plus
  `metrics`, `health` and `changelog` resources. Read-only by design: no write tools.
  Opt out with `ENABLE_MCP=0`. Docs:
  [MCP server](https://sikamikanikobg.github.io/homelab-monitor/mcp/). Closes #70.

## [0.13.1](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.13.1) — 2026-06-07
- **Mobile-friendly dashboard** — the dense tables (Containers, Services, AI
  Models, Network) no longer push the whole page sideways on a phone; each one
  now scrolls within its card. Verified no horizontal overflow down to 360px.
- **Smarter OS-upgrade hint** — a host is now offered the **next reachable**
  release instead of the newest one, so e.g. an Ubuntu 22.04 LTS box is pointed
  at 24.04 rather than a newer LTS it can't `do-release-upgrade` to directly.
- **Fix:** an empty amber banner could show on every tab even when setup was
  healthy (the diagnostics banner's `display` overrode its hidden state).
- Demo video now embedded from **YouTube** on the README and landing page.

## [0.13.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.13.0) — 2026-06-06
- **Windows host monitoring** — register and monitor a **Windows** machine over
  SSH, agentless, via a built-in **PowerShell probe** (no install). Same fleet
  row + System / Network / Services tabs; per-service RAM; GPU via `nvidia-smi`.
- **Hosts onboarding redesigned** — clear three-step flow, theme-safe in light &
  dark, with a **per-OS command chooser** (Linux / Windows user / Windows admin).
- **Containers: RAM vs VRAM** in separate columns, with RAM as the real resident
  set (page cache excluded) and a **table total**.
- **Memory map** (System tab) — interactive treemap of RAM by **container** and
  **systemd service**; works on Docker-less hosts too.
- **New Disks tab** — **WizTree-style** nested folder treemaps; scan a disk,
  drill into folders. On-demand and cached.
- UI refresh — prominent GitHub star/repo/issue cluster, cohesive version &
  update chrome, faster new-release checks.

## [0.12.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.12.0) — 2026-06-02
- **Container disk that's actually right** — the Containers tab now counts each
  container's true footprint (writable layer **plus** its volumes & bind mounts;
  mounts shared between containers are excluded), so heavy containers like Ollama
  and Immich show their real GB instead of a near-empty writable layer.
- **AI Models** now recognises **WhisperX / whisper-asr-webservice** and a dozen
  more servers (SGLang, Triton, Wyoming voice, OpenLLM, LiteLLM, GPUStack,
  Cortex/Jan, …).
- Time-range picker now shows on **every** tab.
- Sidebar brand no longer truncates.

## [0.11.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.11.0) — 2026-05-31
- **AI Models: detect-all + caller attribution** — every recognised model server
  is listed (and stays listed as **Idle** when its model unloads), with a
  **"Driven by"** breakdown showing which services are calling each server.

## [0.10.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.10.0) — 2026-05-31
- **System / Network / Security tabs** — host inventory (OS, kernel, arch, init
  system, hardware), per-host network interfaces & listening sockets, and a
  read-only security posture check (firewall, SSH hardening, SELinux/AppArmor,
  fail2ban, reboot-pending, auto-updates).

## [0.9.1](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.9.1) — 2026-05-31
- Accurate remote CPU temperatures.

## [0.9.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.9.0) — 2026-05-30
- **Container & Service vital signs** — per-container and per-systemd-unit health,
  uptime, memory and ports.

## [0.8.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.8.0) — 2026-05-30
- **Multi-machine monitoring** — register other boxes over SSH from the Hosts tab
  and see every host's vitals side-by-side. No agents, just SSH + Python 3.

## [0.7.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.7.0) — 2026-05-29
- **Self-healthcheck** — `/healthz` liveness endpoint + Docker `HEALTHCHECK`.

## [0.6.3](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.6.3) — 2026-05-29
- Rendered release notes in the in-app update modal.

## [0.6.2](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.6.2) — 2026-05-29
- Faster recovery from negative update-check results.

## [0.6.1](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.6.1) — 2026-05-29
- Branded GitHub star button + contribution nudge.

## [0.6.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.6.0) — 2026-05-29
- In-UI update notification + favicon.

## [0.5.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.5.0) — 2026-05-28
- **Alerting (Discord + ntfy.sh)** — edge-triggered push notifications, configured
  entirely from the Alerts tab.

## [0.4.1](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.4.1) — 2026-05-26
- Accurate Prometheus label series.

## [0.4.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.4.0) — 2026-05-26
- **Prometheus `/metrics` endpoint** — standard scrape endpoint reading from the
  in-memory snapshot (no extra polling).

## [0.2.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.2.0) — 2026-05-25
- Local-AI model drill-down, contention intelligence & host health.

---

_Older tags exist in the repository history. For the authoritative, complete
notes of any release see the
[GitHub releases page](https://github.com/SikamikanikoBG/homelab-monitor/releases)._
