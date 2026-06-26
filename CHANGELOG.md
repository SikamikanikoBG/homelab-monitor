# Changelog

All notable changes to **HomeLab Monitor** are documented here. The format is
loosely based on [Keep a Changelog](https://keepachangelog.com/), and the
project follows semantic-ish versioning. Each entry links to its full GitHub
release notes.

## [0.18.0-ai](CHANGELOG.md) — 2026-06-22 · **The AI-native HomeLab cockpit** _(next_ai preview)_
*A forward-branch identity that turns the monitor into an AI-native cockpit: a local-LLM Lab Copilot, statistical forecasting + correlated incidents, real-time inference telemetry, full alerting + uptime, and Home Assistant / Prometheus / MCP integrations — all still single-image, pure-Python, read-only by default.*

> This is the `next_ai` preview line (versioned `-ai` so it's never mistaken for the stable release). Everything below is shipped and live. The DNA is unchanged: **one container, pure Python + Flask, no heavy deps, read-only by default, every outbound/integration opt-in and OFF by default.**

**🧠 Lab Copilot — local LLM, no cloud**
- **Daily NL digest + ask-box** (`/api/copilot/digest`, `/api/copilot/ask`) — the on-box ollama phrases your real metrics (GPU, biggest model, disk-fill ETA, cost projection, top anomaly) in plain English. Graceful when no LLM is present (never 500s).
- **"Explain this spike"** (`/api/copilot/explain` + SSE `/api/copilot/explain/stream`) — click any anomaly/incident member and the Copilot reads the surrounding samples/models/power to give a 1–2 sentence cause, now typed in live token-by-token as the local LLM generates it (graceful fallback to the non-stream path + deterministic facts when the LLM is down).
- **"What's broken?" one-keystroke triage** (⌘K) — scores every container by badness, opens the worst one's log drawer, and auto-fires the LLM log summary.
- **Container log tail + "Summarize errors"** — Dozzle-style log viewing over the read-only Docker socket plus one-click local-LLM triage of error/warn lines.
- **Scheduled NL digest push** — once a day at a chosen time the digest goes out through your existing alert channel; edge-triggered exactly once/day, fails safe.

**🔮 Forecasting, anomalies & incidents (pure stats, no deps)**
- **Disk-fill ETA per mount**, **cost-this-month projection** (tariff-aware, vs last month), **VRAM-exhaustion ETA + headroom** — R²-gated least-squares (`_linfit`) on real history; `/api/forecast`.
- **Z-score anomaly flags** on GPU util / VRAM / power / temp + total power draw, with per-series floors so idle/noise never false-alarms; anomaly heat-ribbon under the charts.
- **Correlated incidents** — co-firing anomaly series are grouped into one life-cycled Incident (open → cleared) with a detail drawer, lifecycle timeline, per-member σ, a recovery notification, and an MCP `get_incidents` tool.

**⚡ Live LLM inference telemetry**
- **Live tokens/sec · TTFT · resident models** (`/api/llm`) — measured honestly from our own Copilot generations (ollama's ns timing fields), never fabricated; plus a resident-model list from ollama `/api/ps`.
- **LLM throughput history** — a persisted sparkline on the GPU card and `homelab_llm_tokens_per_second` / `homelab_llm_ttft_ms` / `homelab_llm_resident_models` on `/metrics`.

**🔔 Alerting, recovery & uptime**
- **Opt-in alert engine** — SQLite rules (anomaly / disk-ETA / VRAM-ETA / cost-budget / incident / uptime-down) × channels (**Discord / ntfy / Telegram / generic webhook**), with dedupe + cooldown + snooze + ack + history, and one-shot ✅ recovery notifications.
- **Maintenance / alert-silence windows** — one-off or recurring-daily windows that pause outbound notifications (and defer recoveries) without touching the host.
- **External uptime checks (HTTP/TCP)** — user-defined endpoint monitors probed from inside the container: up/down + latency + 24h uptime% + heartbeat strips, on their own daemon thread; with an `uptime_down` alert rule.

**🔌 Integrations**
- **Prometheus / OpenMetrics `/metrics`** — pure-stdlib `homelab_*` block (GPU/power/disk/cost/anomaly/LLM gauges + build_info); works with or without `prometheus_client`. Grafana-ready.
- **Home Assistant / MQTT auto-discovery** — optional, **publish-only** stdlib MQTT 3.1.1 client (never subscribes → no inbound attack surface) that surfaces the lab as native HA sensors under one device. OFF until configured.
- **Read-only MCP server** — the lab is legible to AI agents over one URL, including `get_incidents`. Read-only by design.

**✨ UX & onboarding**
- **Premium Overview hero** (status orb + KPI strip + active-incident chip), **⌘K command palette** (Linear/Raycast-style fast-nav + actions), **busy-vs-quiet cost heatmap**, **time-range control** (1h/6h/24h/7d/30d/All) across all charts.
- **Public read-only `/status` page** with **uptime heartbeat bars** — Uptime-Kuma's shareable "is my lab up?" surface, privacy-first (no names/IPs/secrets).
- **Demo mode (`DEMO_MODE=1`)** — seeds ~7 days of believable synthetic history on a fresh DB so the whole feature set lights up in seconds.
- Dark / light / system, mobile reflow, a11y throughout, **i18n (English + Simplified Chinese)** for every new string.

## [0.17.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.17.0) — 2026-06-18 · **Ask Your Homelab — over MCP**
*Costs and experiments are now MCP tools, so Claude (or any AI) can tell you what last night cost. Bonus: it speaks Chinese now, and updates itself.*

> 0.16 turned HomeLab Monitor into the **AI Lab Cockpit** — GPU, models and the power bill on one page. 0.17 makes that cockpit **conversational and personal**: your AI can now read the costs and the experiment runs straight over MCP, the whole UI can speak another language, and the dashboard can upgrade itself in one click. Still pure Python + Flask, no new dependencies.

**Ask your AI what it costs (MCP)**
- The built-in **read-only MCP server** gains four tools so an agent can finally see the money side of the lab: `get_costs` (machine draw + cost, with a ranked per-process / container / service / model breakdown), `get_entity_cost` (drill into one line item), and `get_experiments` / `get_experiment` (tracked runs priced by the **real GPU energy they burned** — loss curve and watts on the same timeline). **16 tools total**, still **nothing mutated**. Ask *"what did my homelab cost last night, and which model is the most expensive thing on the GPU?"* and let it pick the tools.

**Make it yours — in your language**
- A full **internationalisation framework**: every tab, modal, toast and helper string is translatable, served from `/locales` and shipped in the image. **Simplified Chinese (zh-CN)** is the first complete translation (96% coverage). Closes #148.

**A dashboard that maintains itself**
- **One-click self-update** from the dashboard — opt-in, **off by default** (`ALLOW_SELF_UPDATE`), confirmation-gated, and the monitor's *first and only* write action. It is **not** exposed over MCP. Closes #142.
- **Toast notifications** — every action confirms itself with a quick, dismissible toast instead of a silent state change. Closes #139.

**Brand & community**
- **New logo — radar, not satellite.** A designed radar mark is now *the* brand everywhere — sidebar, header, favicon, README and the docs site — in a transparent variant for the dark UI and a tiled app-icon for the favicon. Radar fits what the tool does: sweep the fleet, surface the blips.
- **Community** — a "Join the Discord" button and a permanent invite in the sidebar, a "Buy me a coffee" support button, and a live Docker-pulls badge in the README. Come help shape the roadmap: <https://discord.gg/tpKWKEdSQN>

**Fixed**
- Network › Throughput legend filter no longer resets on every auto-refresh (#155).
- Clearer **Topology / Machine** card labels on System › Hardware (#154).
- The offline "What's new" changelog now renders italics, blockquotes and the bold subtitle (#173).
- Bulgaria (BG) tariffs switched from BGN to EUR (#145).
- The brand logo no longer 404s in the container — the Dockerfile copies the whole `static/` dir instead of listing files one by one.

The unfinished personalisation items from the "Make it yours" epic (#147) — reorderable tabs (#33), the AI Models Hall of Fame (#23) and per-service notification rules (#24) — carry forward to the next cycle.

**Thanks to**
This release leaned hard on the community — it wouldn't be what it is without them.
- **@pehota** — one-click self-update (off by default), a socket-safety fix, and migrated Bulgaria's tariffs to EUR
- **@krishnapandey1504** — built the unified toast notification system
- **@DevCrox** — wired the dashboard's status messages onto the new toast system
- **@laishettikarthik-tech** — fixed the network throughput legend filter resetting on refresh
- **@koteshyelamati** — clearer Topology / Machine card labels

Five external contributors on one release. 🙏

## [0.16.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.16.0) — 2026-06-14 · **The AI Lab Cockpit**
*Your GPU, your models, and your power bill — finally on the same page.*

> Two milestones land together here: the **deeper-visibility** work, and the big push that turns HomeLab Monitor into the at-a-glance dashboard for anyone training models or serving LLMs at home. Still pure Python + Flask, no new dependencies — it just reads `nvidia-smi`, `/proc`, `/sys`, the Docker socket and your model servers directly.

**See what your GPU is actually doing**
- **GPU truth** — your card says 100% util, but is it throttling, memory-bandwidth-bound, or quietly drooping its clocks? The GPU tab now decodes `nvidia-smi`'s throttle reasons (a red banner the moment it's power-capped or too hot), and adds memory-bandwidth util, core/mem clocks, power-vs-limit headroom and performance state — all from the same call.
- **Multi-GPU** — every card gets its own live panel; the pooled views still sum across them. Closes #95.
- **Model intelligence** — Ollama models show their parameter size, quantisation and context length at a glance, and vLLM/TGI servers get a live serving strip: **tokens/sec**, requests running/queued, KV-cache fill and TTFT, read straight from their `/metrics`.

**Know what it costs**
- **Costs** gets its own page — power becomes money, per machine, then **per component** (GPU measured via `nvidia-smi`, CPU/DRAM via RAPL), then **per process, container or model** you can click to drill into, over any timeframe. Day & night tariffs (UK Economy 7, France Heures Creuses, …) — or just pick your country for a sensible estimate. Honest by design: every watt is measured or a baseline you set, never a guessed wall figure. Closes #25.

**Track your runs**
- **Experiments** — push a run from Jupyter, Colab or Kaggle with a tiny one-file client (or mirror it from **MLflow**), and it comes back **priced with the real GPU energy it burned** — loss curve and power draw on the same timeline. The API keys are yours to create, name, expire and revoke.
- **Notebooks & tools** — auto-discovers Jupyter, TensorBoard, MLflow, W&B, Streamlit and Ray, and flags the idle notebook squatting on your VRAM.

**The rest of the lab, deeper**
- **Network I/O** — host throughput and per-container top talkers. Closes #30.
- **Container logs** — click a container, tail its logs in a side drawer. Closes #28.
- **Top processes** — a mini-htop on the System tab: who's eating CPU and RAM, by command. Closes #32.
- **Adaptive host timeouts** — a slow box that timed out on poll now learns its own sweet-spot instead of being marked down. Closes #99.
- Click the **logo** to jump back to Overview. Closes #98 — thanks @DevCrox for the PR.

**Tidied up**
- A dedicated **AI** zone in the sidebar, so a homelab admin who doesn't care about models isn't put off by "Experiments". The old Alerts tab is now one **Settings** home with **Alerts / Costs / Integrations** sub-tabs.
- A proper visual pass: a real SVG icon set in place of emoji, consistent type/spacing/radius tokens, visible keyboard focus everywhere, and a light theme that finally behaves.

130+ unit tests, shipped one PR at a time through CI → `:next`. Research and design notes live under `design/ai-cockpit/`.

## [0.15.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.15.0) — 2026-06-12
*Polish, safety & sharing.*
- **Snapshot & Share** — grab the current tab as a PNG, or share a generic link + blurb (no host data) to X, Reddit, Hacker News, LinkedIn, Mastodon, Bluesky, Telegram, WhatsApp and more. Closes #31, #87.
- **Service ports** — the Services tab shows each service's listening port as a click-through link, now including forking services (Pi-hole FTL, dnsmasq, libvirtd) whose socket lives in a child process — resolved via the unit's cgroup. Closes #83. Thanks @ravvdevv for the PR, and @vaishnavidesai09 for the scoping.
- **Backup & restore** — one-click SQLite export/import so you never lose your history. Closes #86. Thanks @mohd-ibadullah.
- **MCP status pill** — see at a glance when an AI agent is connected to the built-in MCP server. Closes #84. Thanks @mohd-ibadullah.
- **Telegram alerting** — a third notification channel alongside Discord and ntfy. Closes #27. Thanks @mohd-ibadullah.
- **Desktop polish** — the dashboard fills the width and tints nav vs. main for easier scanning. Closes #85. Thanks @vikasvardhanv.
- **What's new** — a one-time welcome modal after an upgrade, rolling up the changelog for every release since the one you last ran (served offline from this file).

Huge thanks to @mohd-ibadullah, @vikasvardhanv and @ravvdevv for the PRs this cycle — and @vaishnavidesai09 for the scoping on #83.

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
