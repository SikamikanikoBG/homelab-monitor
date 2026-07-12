# Changelog

All notable changes to **HomeLab Monitor** are documented here. The format is
loosely based on [Keep a Changelog](https://keepachangelog.com/), and the
project follows semantic-ish versioning. Each entry links to its full GitHub
release notes.

## [Unreleased]

**Fixed**
- **AMD GPUs are now detected reliably — including on hosts without nvidia-smi, and alongside an NVIDIA card.** Two gaps in the amdgpu back-end (#1): the collector called nvidia-smi unguarded, so on a machine without it the GPU read aborted before the AMD sysfs reader ever ran — a pure-AMD box reported "no GPU" even though the reader worked; and detection was NVIDIA-*or*-AMD, hiding the AMD card on hybrid machines. nvidia-smi is now read defensively and both back-ends always run, with their cards merged (AMD re-indexed so per-card history stays distinct) and VRAM/util/temp aggregated across all of them; per-process attribution still applies to the NVIDIA cards only. Utilisation also survives the amdgpu `gpu_busy_percent` EBUSY quirk — the intermittent "Device or resource busy" race — by retrying once instead of reporting 0%.
- **GPU diagnostics are now vendor-aware — an AMD host is no longer told to install the NVIDIA runtime.** The local requirements panel (and the remote-host probe's GPU check) hard-coded an "NVIDIA GPU" row with nvidia-ctk-only remediation, so a machine with a working AMD Radeon — read via the amdgpu sysfs back-end since v0.21.0 — still showed a confusing "no NVIDIA GPU detected" message. The row now reports the real vendor (GPU (AMD) / GPU (NVIDIA)), and the no-GPU remedy explains that AMD is detected automatically from the kernel (no ROCm needed), with a one-liner to confirm the card's sysfs nodes exist. (#1)

## [0.24.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.24.0) — 2026-07-09 · **A restructured engine underneath, and controls on by default**
*The ~7,600-line `app.py` monolith is now a proper `backend/` module tree — behavior unchanged, backed by a 71-snapshot test suite and a CI gate that fails the build on a silent `except: pass`. Separately, container/service start-stop-restart controls (plus self-update) flip from opt-in to on by default — worth a glance at your compose file before you upgrade.*

**Changed**
- **Container/service start-stop-restart controls, and self-update, are now on by default.** Previously both needed explicit opt-in (`ENABLE_CONTROLS=1`, `ALLOW_SELF_UPDATE=1`). The shipped `docker-compose.yml` now mounts the Docker and D-Bus sockets read-write by default so both write paths work out of the box. Want the old fully read-only posture back? Bring the stack up with the new `docker-compose.readonly.yml` override, or set either flag to `0`. Containers: local host only for now. Services: local systemd, remote Linux/Unix (SSH + systemctl), remote Windows (SSH + PowerShell). Buttons stay disabled with an explanation when the underlying sockets aren't present; a real failure (permission denied, non-admin Windows session) surfaces the actual error, never a generic one. (#229)

**Added / Internal**
- **`app.py` extracted into a `backend/` module tree.** All 54 routes, every collector, probe and notify path, and all DB access now live in `backend/api/`, `backend/collectors/`, `backend/probes/`, `backend/notify/` and `backend/db/` (thread-local connections, a versioned migration runner, repo shims instead of raw SQL scattered everywhere) — HTTP responses byte-identical (71-snapshot suite). New rollup tables (`samples_1h`/`net_samples_1h`) make long-range Costs/heatmap queries cheap. Alert edge-state (`_NOTIFIED`, uptime down-since) now persists to the DB, so a restart no longer risks a duplicate or missed alert. Each background worker runs in its own thread with a watchdog that logs a stall instead of hanging silently. _(backend architecture refactor by @pehota, #230)_

## [0.23.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.23.0) — 2026-07-03 · **A real Disk I/O dashboard, AI models across every provider, and alerts that shut up during maintenance**
*The Disk I/O tab goes from a bare read/write table to per-device utilisation, op-latency, 6h trend sparklines, z-score anomaly detection and per-process attribution. The AI Models "Installed models" registry now catalogs vLLM/llama.cpp/LM Studio alongside ollama, grouped by provider. Tokens/sec gets first-class treatment on the Experiments tab and over MCP. Click-to-copy reaches every table in the dashboard, not just the System tab. And maintenance windows let you silence alerts for planned work instead of eating the noise.*

**Added**
- **Installed-models registry now spans providers, not just ollama.** The AI Models tab's "Installed models" panel merges ollama's on-disk catalogue (size, params, quant, last-modified) with every other recognised server's model list — vLLM, llama.cpp, LM Studio, ComfyUI, and the rest of the fleet's `PROBES` table — grouped by a new **Provider** column, with a filter box to search by model or provider. Surfaced via a new MCP tool `get_installed_models()` and a `homelab_models_installed_total{provider=...}` Prometheus gauge, so "what can I run, and where" no longer means SSHing in. Cross-host dedupe stays a follow-up. (#219)
- **Click-to-copy, extended beyond the System tab.** The one-click copy affordance from the System/Hardware cards is now a small reusable `copyable()` chip used across the dashboard: container names (Containers), unit names (Services), interface addresses/MAC (Network), listening-socket bind addresses/ports (Network), and model names (AI Models — both the Loaded view and the Installed-models registry). One delegated click listener now handles every copy button in the document, so new tables get the affordance for free without a per-render wiring call. Normal text selection is unaffected. (#220)
- **Tokens/sec on Experiments, first-class instead of buried — in the UI *and* MCP.** A run's `tokens_per_sec` (however your script named it — `tok_per_sec`/`tokens_sec`/`tok_s` all recognised) now gets its own chip in the Runs table instead of being lost among arbitrary metrics, leads the run-detail chart list, and — mirroring the AI Models tab's "Tokens / Joule" — the run's cost caption now reads out average/peak tok/s and **tokens/kWh**. `get_experiments()`/`get_experiment()` over MCP already passed it through (the run-tracking API stores any metric key you log, no schema change) — now documented explicitly so an agent knows to look for it, with test coverage proving the passthrough. Plus a documented `homelab_run.py` example. (AI Models tab's own tokens/sec, from vLLM/TGI `/metrics`, was already shipped and verified working — Ollama has no passive way to expose it, confirmed via its API design; that stays a known gap, not silently faked.)
- **Disk I/O Throughput, from a bare read/write table to a real per-device dashboard.** Each device now shows utilisation % and average read/write op-latency (extra `/proc/diskstats` fields), a 6h read-vs-write trend sparkline riding the same `/api/data` range everything else uses, a per-device z-score anomaly badge (rolling baseline, edge-triggered into the Insight Feed so a persistent spike logs once, not every scan), and per-process "top writer / top reader" attribution from a bounded `/proc/<pid>/io` sample of the processes already tracked for CPU/RAM (never a full-`/proc` scan). The headline Total read/write KPIs now sum **physical whole-disks only** — RAID/dm aggregates and partitions no longer triple-count the same bytes. Built with the app's own components throughout (`.kpi`/`.dbar`+`sev()`/`.pill`/`.badge`/`.mspark`) — no new visual language. Per-process attribution is authed-only, comm name only, never on the public status page.
- **Maintenance windows — silence alerts for planned work instead of eating the noise.** A new `maintenance_windows` table lets you cover a container, systemd unit, uptime check, disk or GPU (by name/id pattern, or `*` for everything) for a one-off, daily or weekly window; alerts routed through `_emit()` and uptime's down/recovery/cert/slow checks are silenced while a match is active, and the Uptime tab shows a blue **In maintenance** pill instead of a false "down". New `GET/POST /api/maintenance` and `DELETE /api/maintenance/<id>`. _(contributed by @1HazyOne707, #167)_

**Fixed**
- **Copy buttons silently claimed "failed" even when the copy worked.** A duplicate `copyText()` declaration shadowed the original (JS hoists the later one), which meant the standalone copy buttons (Share link, integration key, MCP command, update command, per-OS remediation command) always read back `undefined` from a function that never returned a value — showing "⚠️ Copy failed — select and Ctrl+C" on every successful copy. Consolidated to one `copyText()` that returns whether the copy actually worked. (#220)
- **Daily Brief on chat channels was a starved, misleading blob — rebuilt.** The brief only ever sent its rich card to **email**; Discord/Telegram/Slack/ntfy got a stripped text summary that (a) showed the headline twice, (b) was always coloured info-blue regardless of severity, (c) said "Services: 1 failed" without naming the service, and — worst — (d) **counted an offline host as up** (`5/5` while one machine was down), because it read each host's stale stored *Test* result instead of the live online flag the dashboard uses. Now: the fleet tally comes from the same `_host_is_online()` source as `/api/fleet` (offline hosts are correctly counted and show their last error); chat messages are coloured by real severity; action lines **name** the failing container/unit (e.g. `immich_ml — Exited (137) 2 hours ago`); the headline isn't repeated; and **Discord posts the full HTML brief as an attachment** alongside the clean summary. All "infant-icon" emoji are gone — status is shown with colour (CSS dots, the embed stripe) and quiet uppercase labels. (#170)

## [0.22.1](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.22.1) — 2026-06-27 · **Hero engine GPU gauge tells the truth about a full card**
*A small fix so the Overview's "Lab's Engine" GPU gauge stops reading calm when the card is actually maxed.*

**Fixed**
- **Overview engine GPU gauge now reflects VRAM saturation, not just compute util.** A GPU with models loaded but no inference running (low util, ~full VRAM) was showing a calm low number on the hero engine gauge while the GPU tab and the Overview status tile both correctly flagged it **crit** at ~94% VRAM. The gauge now reads the *binding constraint* — `max(util%, VRAM%)` — so a memory-pinned card lights up to match the rest of the UI, with both VRAM and `% util` shown in the line beneath.

## [0.22.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.22.0) — 2026-06-27 · **A real status page — public lab health, plus a page per service**
*The throwaway `/public` page is reborn in the app's own skin: overall lab health, a live list of the services you watch, and a dedicated status page for each one — uptime over 24h/7d/30d/90d, incident history and response times. Alerts also gain email, Slack and generic-webhook channels, services get brand logos, and the AI Models tab now lists every model pulled to disk.*

**Added**
- **A proper status page — overview + a page per monitored service.** The public page (`/public`) is rebuilt in the app's own design system (it was a bare throwaway before): the lab's system health *plus* a list of the services you monitor on the Uptime tab, each with a live heartbeat, 24h uptime and latency. Click any service for its **own status page** (`/public/<id>`) — current state and how long it's held, uptime over **24h / 7d / 30d / 90d**, a day-by-day history bar, a response-time chart, and a reconstructed list of past incidents (when it went down, for how long, and why), plus TLS-cert days-remaining. Each Uptime check has a new **"public" toggle** (off by default) so you choose exactly what's listed; the whole page stays gated behind `PUBLIC_STATUS`, and only the service name + host are ever exposed — never the raw target or credentials. New read-only endpoints: `GET /api/public-status` (now includes the monitors) and `GET /api/public-status/<id>`. _(public-status groundwork by @siva23367, #197)_
- **Daily Brief — an opt-in once-a-day HTML health digest.** A single scheduled email/Discord/ntfy/Telegram summary of the lab's last 24h — overall health, anything that alerted, and the headline numbers — so you get one calm daily readout instead of watching the dashboard. Off by default; pick the channel and the send-time. (#170, #207)
- Alerts now support **email (SMTP)**, **Slack incoming webhooks**, and a **generic webhook** target alongside Discord, ntfy and Telegram. All three channels configure from the Alerts settings panel and honour the existing minimum severity + disk threshold rules. _(contributed by @Jishnu-Prasad888, #191)_
- **Per-service notification rules — choose which services can page you.** Instead of every check shouting on every channel, each monitored service can opt in/out of alerting independently, so noisy or low-priority services stay quiet. _(contributed by @Mr-Neutr0n, #24/#151)_
- **TLS-certificate expiry tracking on uptime checks.** HTTPS checks now read the certificate's days-remaining and warn **before** it expires (surfaced on the dashboard and the new status pages), so a silent cert lapse doesn't take a service down. _(contributed by @1HazyOne707, #163/#198)_
- **Brand logos for famous services** on the Containers and Services tables. The monitor now shows the recognisable icon (Immich, Plex, Pi-hole, Home Assistant, Postgres, Grafana, n8n, Ollama, and ~60 more) in front of the name, matched from the container image or unit name — faster visual scanning of a busy host. Logos are embedded Simple Icons (MIT), so there are **no runtime external requests**; near-black brands fall back to the theme text colour so they stay visible in dark mode, and unrecognised entries are unchanged.
- **Installed-models registry on the AI Models tab.** A new panel lists every model **pulled to disk** on this host's local LLM (ollama) — name, on-disk size, params · quantization, a **Loaded** badge (with live VRAM) for models resident right now, and last-modified — with an *N models · X GB on disk · M loaded* header. Read-only (`GET /api/models` → ollama `/api/tags`+`/api/ps`, cached ~45s, always-200 graceful-degrade when ollama is off, no secret leak). Loads on tab view + manual refresh.
- **Copy buttons on the System & Hardware cards** so the OS / kernel / CPU / host values are one click to clipboard (with a clean fallback when the clipboard API isn't available). (#208)

**Changed**
- Wired the new Alerts form labels to the locale files so translated dashboards automatically pick up the email/Slack/webhook copy.
- **Overview engine gauges**: the `%` now rides inline on the number's baseline (reads as `67%`) instead of as a detached superscript, and the **GPU gauge shows live power draw (W)** on its own line beneath the VRAM.

**Fixed**
- **GPU went undetected on hosts where nvidia isn't Docker's default runtime** (stock Ubuntu/Debian/Mint, where GPU containers normally opt in per-container with `--gpus all`). The monitor exposed the card only through the `NVIDIA_*` env vars, which the toolkit honours only for the default runtime — so the card worked everywhere else but the dashboard reported "no GPU detected" (#203). Added a **`docker-compose.gpu.yml` override** that requests the nvidia runtime for just this container (no global-default change, still starts on GPU-less hosts), and corrected the Setup-tab remedy + compose comments to include the **`--force-recreate`** step a plain restart was silently skipping.
- **AMD / Intel GPUs now show in the per-host tab** instead of a misleading "no NVIDIA GPU" empty state, and nameless / NVIDIA-without-`smi` cards are handled gracefully. (#206)
- **Overview engine gauges** keep the number + `%` centred in the ring at every size. (#211)

## [0.21.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.21.0) — 2026-06-23 · **See your power bill by the hour — and which GPU is burning it**
*Three new questions answered at a glance: when is your lab most expensive, which GPU is doing the work (whoever made it), and is anything down?*

**Added**
- **Busy-hours cost heatmap on the Costs tab.** A 7×24 grid — local day-of-week × hour — of your lab's typical total draw, **shaded by cost per hour** once a tariff is set (by power otherwise). It turns months of history into one picture of *when* your rig actually costs you money, with the busiest and quietest slots called out and a busy-vs-quiet spread. No setup; it fills in after about a day of samples.
- **Any-vendor GPU support — the panel is no longer NVIDIA-only.** **AMD** GPUs are read on Linux straight from the kernel's `amdgpu` interface — **no ROCm, no vendor tools** — and **AMD and Intel** GPUs (including integrated) are read on Windows hosts, so the card finally shows up with its name, utilisation and VRAM. Strictly additive: NVIDIA and GPU-less hosts behave exactly as before.
- **Built-in uptime monitoring.** Watch any **HTTP endpoint or TCP port** from the same container — know the moment anything stops responding, with a heartbeat strip, **24h/7d uptime %**, latency, and **smart per-check alerts** (anti-flap confirm, recovery quoting the downtime, optional slow-response warning). Nothing else to self-host.

**Polish**
- The busy-hours heatmap uses the dashboard's SVG icon family and a fixed-width day column; accessibility and layout passes throughout.

_Rolls up the work shipped through `next` since 0.17.3 into one release. Still pure Python + Flask — no new runtime dependencies._

## [0.20.1](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.20.1) — 2026-06-23 · _patch_
_Silent patch — polish on the new busy-hours heatmap._

**Fixed**
- **Heatmap icons** now use the dashboard's SVG icon family — a calendar glyph in the heading and trending-up / trending-down / balance icons on the callouts — instead of raw emoji, so the card matches every other panel.
- **Heatmap layout** — the day-of-week label column is pinned to a fixed width (`table-layout:fixed`); on wide screens it could previously stretch to roughly half the card and squash the 24 hour cells.

## [0.20.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.20.0) — 2026-06-23 · **Busy hours — see when your lab burns power**
*A 7×24 day-of-week × hour heatmap on the Costs tab shows the rhythm of your lab at a glance — when it's busy, when it's idle, and (with a tariff set) when it's expensive.*

**Added**
- **Busy-hours heatmap on the Costs tab.** Every history sample is bucketed by its **local weekday and hour** and averaged, giving a 7×24 grid of typical total draw (GPU + CPU + DRAM). Colour scales by **cost-per-hour** once you've set a tariff, or by **power** otherwise — so it's useful even before you add a price. Callouts surface the **busiest** and **quietest** slots and, when priced, the spread between your busiest and quietest quarter of hours. Served from a new pure-Python `/api/cost/heatmap` endpoint (own 30-day window, aggregated outside the DB lock), reusing the same tariff machinery as the rest of the Costs page so the €/kWh maths never diverges. The grid is a real `<table>` with per-cell `aria-label`s, sparse cells are dimmed by sample count (honest about coverage), and it fills in after about a day of history.

## [0.19.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.19.0) — 2026-06-23 · **AMD & Intel GPUs join the party**
*The GPU panel is no longer NVIDIA-only — AMD and Intel cards now show up too, with no vendor tools to install.*

**Added**
- **AMD GPU support on Linux — no ROCm required.** Both the hub's own collector and every monitored Linux host now read AMD cards through the in-kernel `amdgpu` sysfs interface (`gpu_busy_percent`, `mem_info_vram_total`/`used`, and hwmon temperature/power) — so an AMD box shows the GPU panel (name, utilisation, VRAM, temp, power) with zero configuration. (#1)
- **AMD & Intel GPU support on Windows hosts.** The Windows probe now falls back to Windows' built-in GPU performance counters + WMI when `nvidia-smi` is absent, surfacing AMD and Intel GPUs — including **integrated** graphics — with name, utilisation and VRAM.

**Changed**
- The GPU back-end is now vendor-aware: NVIDIA continues through `nvidia-smi`, and the AMD/Intel paths are consulted **only** when `nvidia-smi` reports nothing — so NVIDIA and GPU-less hosts behave exactly as before. Per-card clock/throttle enrichment and per-process VRAM attribution remain NVIDIA-only for now (AMD per-process attribution is tracked as a follow-up).

## [0.18.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.18.0) — 2026-06-22 · **Is my lab up? — Uptime monitoring built in**
*Point it at any URL or port and it watches them for you — heartbeat, uptime %, and alerts that page once (not on every dropped packet) and tell you when it's back.*

**Added**
- **Uptime checks — built right in.** A new **Uptime** tab monitors any **HTTP endpoint or TCP port** (your own services, a NAS, a remote site) from inside the container — a heartbeat strip, **24h + 7d uptime %**, latency and last error per check. Probes run on a dedicated worker (never the metrics sampler), each bounded by its own timeout, so a hanging endpoint can't stall the rest; nothing is probed until you add a check.
- **Smart per-check alerting**, reusing your existing channels (Discord / ntfy / Telegram): a check is only called **DOWN after N consecutive failures** (anti-flap — a single dropped packet won't page you), a **recovery alert quotes the downtime**, and an optional **latency warning** fires when an endpoint is up but slow. On by default per check; honours the global minimum severity.
- **Overview at a glance** — a 🛰 uptime chip on the cockpit's fleet rollup (red the moment a check is down), and down/slow endpoints surface in the Insight Feed.

**Privacy:** check targets/labels/errors stay on the private dashboard + authed API — they never reach the public status payload.

_Rolls up the silent 0.17.1–0.17.3 patches below._

## [0.17.3](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.17.3) — 2026-06-21 · _patch_
_Silent patch — Docker image rebuilt, no release announcement (patches roll up into the next minor)._

**Changed**
- **Overview rebuilt as a 3-column mission-control cockpit.** The fleet rail (every host at a glance), the engine + cost column, and the live insight feed + cost leaderboard sit side by side, with a containers / services / diagnostics strip beneath — a clear glance → scan → drill hierarchy instead of the old stacked layout.
- **The engine shows three gauges — GPU · CPU · RAM — for the focused host**, with a **This host / Whole fleet** toggle that aggregates the whole homelab (average util, summed VRAM / cores / RAM). Each ring turns amber → red as it saturates.
- **Calmer, more deliberate styling** across the Overview: solid panels with soft depth, neutral ink for figures, semantic colour reserved for state, restrained motion.

**Fixed**
- **Accessibility** — every tab now passes a WCAG 2.1 AA contrast scan in both themes (muted captions, warn/ok text on tinted surfaces, in-text link distinction, keyboard-focusable scroll regions, full reduced-motion coverage).
- **Mobile** — fixed a horizontal-overflow bug (the chart canvas could pin the page wider than the viewport); the dashboard is now fully responsive down to 320px, and the GitHub / Discord / social cluster is shown again on small screens.
- Removed the duplicated detailed-fleet table from the Overview (the fleet rail is the canonical view).

## [0.17.2](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.17.2) — 2026-06-21 · _patch_
_Silent patch — Docker image rebuilt, no release announcement (patches roll up into the next minor)._

**Fixed**
- **Alerts now name the machine.** Every notification — Discord, ntfy and Telegram — is prefixed with `[<machine>]`, so when you monitor many hosts you can tell at a glance *which* one a "Container unhealthy" / "Disk at 95%" / "GPU VRAM pressure" alert is about. The **Test** button shows the same label so you can confirm it. No configuration needed — the name comes from the host's reported hostname.

## [0.17.1](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.17.1) — 2026-06-21 · _patch_
_Silent patch — Docker image rebuilt, no release announcement (patches roll up into the next minor)._

**Fixed**
- **Discord notifications no longer fail with 403.** Discord's webhook API sits behind Cloudflare, which rejects the default `Python-urllib` agent (error 1010). Outbound notifier POSTs now send a real `User-Agent`, so the alert-settings **Test** button works.
- **Fleet "online/offline" no longer flaps.** The overview summary occasionally showed healthy hosts as offline, then online again next refresh. Hosts are now polled **concurrently** (one slow remote can't age the others out) and the online flag uses a generous, timeout-aware staleness window — a single slow or missed poll cycle can't flip a host offline.

**Changed**
- **Overview tab reordered** — fleet data table first, then the AI/GPU workload band, then MCP, with **Setup & requirements** moved to the bottom.
- **CI/CD**: the Discord release announcer now skips patch releases, matching the selfh.st gate — both speak only on minor+ versions.

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
