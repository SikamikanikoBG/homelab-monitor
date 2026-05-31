# UI migration parity checklist — current `dashboard.html` → rev 6 (Portainer-style)

Contract for the migration: **every row in the "Current" column must still work after migration.**
Strategy: keep all JS renderers + element IDs + API calls; replace only the **shell, CSS, nav, and theming**, and add the **AI Models timeline/expand** + **dark/light/system** + **density**.

Legend — Action: 🟢 keep as-is · 🔵 restyle (same DOM/IDs) · 🟠 restructure (new shell, same renderer) · 🆕 new.

| # | Area | Current dashboard | rev 6 target | Action | Risk / note |
|---|------|-------------------|--------------|--------|-------------|
| 1 | **App shell** | `header` (title/ver/update/star/live) + sticky `.topnav` (hostbar + tab nav + gear) | Left **sidebar** (brand, grouped nav, footer) + content **top bar** | 🟠 | Keep all `<section data-tab>` blocks & inner IDs untouched |
| 2 | **Navigation** | Top tab row + **gear drawer** for settings (`#nav`/`#subnav`, `setSettingsMode`) | Sidebar groups **Monitoring / Settings** (both always visible) | 🟠 | `showTab()` stays the source of truth; drawer logic collapses to a no-op |
| 3 | **Nav attention dots** | `#ndot-<key>` dot on tab when crit/warn (`renderHealth`) | **count badge** on sidebar row (red/amber) | 🔵 | Same data (`HH.overview[].status`), new element |
| 4 | **Host switcher** | `.hostbar` pills + `+ Add host` (`renderHostBar`) | Desktop: pills in top bar · Mobile: scrollable strip | 🟠 | Reuse `renderHostBar`, retarget container; `setHost`/`CURRENT_HOST` unchanged |
| 5 | **Range + auto-refresh** | `.controls` card (1h/6h/24h/7d/30d/all + auto-refresh) | Segmented range in top bar; auto-refresh kept | 🔵 | Keep `#auto`, `.rb[data-r]`, 15 s poll loop |
| 6 | **Theme** | dark only (hardcoded `:root`) | **dark / light / system** + toggle | 🆕 | Tokenize; charts read hardcoded colors → see #20 |
| 7 | **Density** | none | comfortable default + **Compact** toggle | 🆕 | `.dense` class on root |
| 8 | **Overview = Fleet** | `.fleet-tbl` (Host/Status/CPU/RAM/GPU/Load/Uptime/Temp/Disks), row-click → focus host | same table, restyled (badges, tabular nums, toolbar) | 🔵 | `renderFleet()` kept; **must not** become single-host KPIs |
| 9 | **GPU** | now-KPIs + VRAM bar + now table + GPU-services summary + **stacked VRAM chart w/ capacity + pressure bands + OOM ▼** + util/power/temp dual chart | same, restyled | 🔵 | Charts (`buildCharts`) kept verbatim — the AI-eng's must-keep |
| 10 | **AI Models** | loaded-now table (service/model/vram) + range summary (model/server/peak) | **+ expandable rows** (details) **+ per-model VRAM timeline** | 🆕🔵 | Timeline from `D.services[service]`; events from `D.events`; details from `D.summary`/`model_summary` |
| 11 | **Containers** | KPIs + table (name/state badge/image/ports/uptime/mem/disk/status) | table + toolbar + flat badges | 🔵 | `renderHealth` container block kept; restyle `.badge`,`.sdot` |
| 12 | **Services** | KPIs + table (unit/state/ports/uptime/mem/desc), `yours`/`watched` pills, port pills | table + toolbar + badges | 🔵 | `renderServicesBlock` kept |
| 13 | **Host** | KPIs (CPU/RAM/Load/Uptime/temp) + disk bars + CPU/RAM/load chart | KPIs + disk table + chart | 🔵 | `renderHostTab` + local path kept |
| 14 | **Hosts (settings)** | pubkey copy/show-cmd, add form, **Scan LAN**, host rows: Test/Remove/**Edit inline**, capability checklist, remedy **copy / Run-on-remote (sudo panel)**, LAN suggestions | same, restyled in Settings group | 🔵 | Large surface — **do not drop**: `loadHosts`,`testHost`,`scanLAN`,`openRunPanel`,`beginEditHost`,`renderChecks` |
| 15 | **Alerts (settings)** | enable, Discord webhook (secret-masked), ntfy topic/server, min severity, disk %, Save/Test | same, restyled | 🔵 | `loadAlerts`/`saveAlerts`/`testAlerts` kept |
| 16 | **Update modal** | badge → modal w/ release notes (GitHub markdown), copy cmd, link | same, restyled | 🔵 | `openUpdateModal`, `/api/markdown` kept |
| 17 | **GitHub star** | pill + count fetch + pre/post CTA + `hl_starred` localStorage | one place (sidebar footer) + count + CTA | 🟠 | Trim from header to footer; keep star-state logic |
| 18 | **Status semantics** | color + dot; `--accent`==`--warn`==#d29922 | **neutral selection; amber=warn only**; badge = square+word; counts not bare dots | 🔵 | Unanimous fix; colorblind-safe |
| 19 | **Multi-host scope** | local-only notices on gpu/models/containers when remote host selected; host-scoped host/services | unchanged behavior | 🟢 | `renderLocalOnlyNotice` etc. kept |
| 20 | **Charts theming** | Chart.js colors hardcoded (`#21262d` grid, `#8b949e` ticks, `#e6edf3` legend) | read theme tokens so light mode looks right | 🔵 | Low risk if deferred: charts stay dark-tuned until parametrized |
| 21 | **Polling / routing** | 15 s `setInterval`, `#hash` routing, `hashchange` | unchanged | 🟢 | Keep wire-up block |
| 22 | **APIs consumed** | `/api/data /health /fleet /settings /notify/test /hub/pubkey /hosts(.../test/run) /host_data /lan/scan /markdown` + GitHub star | unchanged | 🟢 | **No backend changes** — pure front-end migration |

## AI Models enhancement (#10) — proposed detail
Expandable row per model; expanded panel shows (all from existing data):
- **VRAM timeline** — sparkline of `D.services[service]` over the selected range, with OOM `▼` markers from `D.events`.
- **Server / backend** (ollama, vLLM, TGI, llama.cpp, SD, ComfyUI), **GPU placement** where known.
- **Peak / Avg VRAM** (`D.summary`), **% time resident** (`summary.present`), **current VRAM** (`n.models[].vram`).
- Status badge: Loaded / Idle.

## Out of scope (explicit, so it's not "forgotten")
- No backend/`app.py`/`probe.py` changes. No new API. No multi-GPU backend (mock shows it; real data is single-GPU today — keep current behavior, leave room in layout).
- Chart light-theme polish (#20) may land in a follow-up if time-boxed.
