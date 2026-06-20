# Journal — MCP server for HomeLab Monitor (issue #70)

**Date:** 2026-06-09
**Branch:** `next` (per Arsen: "develop, test, commit, push to next branch")
**Issue:** [#70](https://github.com/SikamikanikoBG/homelab-monitor/issues/70) — first-class
MCP server so Claude / any MCP client can connect to the monitor and explore the
ecosystem. Read-only to start; writes are explicitly out of scope.

## Goal
A thin, well-described MCP server that wraps the monitor's existing read-only HTTP
endpoints with LLM-friendly tool/resource semantics. No collector changes.

## API surface it wraps (verified in app.py)
- `GET /api/fleet`        → roster + per-host headline vitals  → `list_hosts`
- `GET /api/host_data/<n>`→ one host's System/Network/Security → `get_host`
- `GET /api/health`       → live vitals (gpu/host/docker/systemd/overview, DB-free) → `get_snapshot`
- `GET /api/data`         → models + caller attribution + events/insights → `get_ai_models`, `get_events`
- `GET /metrics`          → Prometheus text → resource `homelab://metrics`
- `GET /healthz`          → version/liveness (resource `homelab://health`)
- `CHANGELOG.md` (bundled)→ resource `homelab://changelog` for version context

## Design
- `mcp/homelab_client.py` — pure stdlib (urllib) HTTP client + trimming logic. No
  `mcp` dependency, so it imports/tests on this box's Python 3.8.
- `mcp/server.py` — thin FastMCP wrapper (needs py3.10+, runs in the 3.12 image).
  Supports `stdio` (default, for `claude mcp add`) and `streamable-http`
  (`MCP_TRANSPORT=http`, for the optional docker-compose sidecar).
- Config via env: `HOMELAB_MONITOR_URL` (default http://localhost:9800),
  `MCP_TRANSPORT`, `MCP_HOST`, `MCP_PORT`, `HOMELAB_HTTP_TIMEOUT`.
- Guardrails: **read-only**. No write tools. Documented as such.

## Constraints / testing
- Local box: Python **3.8** only, **no Docker** → can't run the MCP SDK or build the
  image here. Strategy:
  - Unit-test `homelab_client.py` against a stdlib stub HTTP server (py3.8) — covers
    all endpoint-wrapping + trimming (the real substance).
  - `py_compile` `server.py` for syntax (kept 3.8-parseable).
  - Image builds on python:3.12-slim; integration on ardi is a follow-up.

## Steps
- [x] write mcp/homelab_client.py
- [x] write mcp/server.py (FastMCP, stdio + http)
- [x] requirements.txt + Dockerfile + mcp/README.md
- [x] optional sidecar service in docker-compose.yml (opt-in profile, port 9810)
- [x] website/mcp.md + nav, README MCP section, CHANGELOG Unreleased entry
- [x] .dockerignore: `!CHANGELOG.md` so the sidecar can bundle it
- [x] tests: stub-server unit tests (py3.8) + py_compile server.py
- [x] commit + push to next

## Results / verification
- **Unit tests** (`mcp/tests/test_client.py`, py3.8 stub monitor): all checks pass.
- **Live client check** against the real monitor on ardi:9800 (py3.11) — every
  field assumption confirmed against real payloads (4-host fleet, GPU, 39 containers,
  93 services, 10 loaded models). Caught + fixed two filter bugs in the process:
  - a completed oneshot unit (`active:"inactive"`) was wrongly listed as failed →
    now only `active=="failed"` or `status=="bad"` count as problems.
  - container "problem" now means non-running **or** explicitly `unhealthy` (not a
    transient/empty health). Also strip a trailing space on the OS pretty name.
- **Full MCP stdio handshake** on ardi (real `mcp` SDK + live monitor):
  - TOOLS: get_ai_models, get_alerts, get_events, get_host, get_snapshot, list_hosts
  - RESOURCES: homelab://changelog, homelab://health, homelab://metrics
  - `get_snapshot` / `get_ai_models` returned real data; changelog resource read OK.
  - → "ALL INTEGRATION CHECKS OK".
- Image build / compose deploy on ardi: left as a follow-up (per dev workflow,
  WIP goes to dev; this PR is the feature on `next`).

## Follow-up: full dashboard parity (Arsen: "every detail in the UI on the MCP")
Expanded from 6 → **12 tools** so all UI tabs are reachable:
- `get_containers` / `get_services` — full Docker/systemd lists (not just problems).
- `get_memory` — per-service + per-process RAM breakdown (the memory treemap).
- `get_gpu` — util/VRAM/power/temp + per-model VRAM + caller attribution.
- `get_history` — charted GPU+host time-series.
- `scan_disk(path)` — WizTree folder treemap (wraps async `/api/disk_scan`, polls).
- `get_snapshot` now also carries `diagnostics`.
Bug caught against live data: `get_memory` first read `/api/data`'s `mem_total` /
`now.mem_used`, which are **GPU VRAM**, not system RAM → fixed to read host RAM from
`now.host` (verified 128803/41007 MB real). Re-ran unit suite (green), live client
check, and the full MCP stdio handshake — all 12 tools list and return real data:
containers (39), services, memory, gpu, history (361 pts), scan_disk (tree) →
"ALL INTEGRATION CHECKS OK". Docs (README/website/mcp README/CHANGELOG) updated.

## Follow-up 2: single image + v0.14.0 release (Arsen: "1 docker for homelab AND mcp")
Refactored from a separate sidecar to **one container** running both:
- `launch.py` — process supervisor: starts Flask (critical; its death exits the
  container) + the MCP HTTP server (best-effort, respawned with backoff). `ENABLE_MCP=0`
  to opt out.
- root `Dockerfile` — adds `mcp>=1.9.0`, copies `mcp/server.py`→`/app/mcp_server.py`,
  `homelab_client.py`, `CHANGELOG.md`, `launch.py`; EXPOSE 9810; CMD launch.py.
- `docker-compose.yml` — dropped the `--profile mcp` sidecar service; main service now
  carries `MCP_PORT`/`ENABLE_MCP`. Removed `mcp/Dockerfile` (one image).
- **UI:** new "AI agent (MCP)" card in the Setup tab (no robot icon, per Arsen) showing
  enabled/disabled + endpoint + copy-paste connect command; `/api/health` now reports
  `mcp:{enabled,port}`.
- **Release:** VERSION 0.13.1→**0.14.0**; CHANGELOG/README/website/mcp README updated.
  Tag `v0.14.0` triggers release.yml → multi-arch image to Docker Hub. Deploy on ardi
  prod via `docker compose pull && up -d`; remove the temp `homelab-monitor-mcp`
  sidecar (frees :9810 for the single container).

## Follow-up 3: README pitch, landing-page focus + MCP SEO (consulted Jarvis)
- README: expanded MCP section with an agentic-era hook + 4 example prompts (PR #73).
- Landing page: "Now readable by your AI agent, too" band + HomeLab Monitor <-> MCP <->
  Claude/ChatGPT diagram (website/assets/mcp-agents.svg, real brand glyphs; Claude clay
  #D97757, ChatGPT green #10A37F); caption keeps it honest (read-only = question/answer,
  not write). MCP feature card + band CSS. (PR #74)
- SEO: MCP-forward site_description; per-page meta descriptions (verified live); MCP
  page title; repo description rewritten for MCP; topics +mcp/model-context-protocol/
  mcp-server/ai-agents/claude/agentic, -nvidia-smi/flask/python/systemd/
  container-monitoring/vllm (stayed 20). Docs deploy + Hub README sync green.

## Follow-up 4: Ollama in diagram + README embed + hero refresh (Jarvis)
- MCP diagram: added 3rd logo Ollama (Simple Icons), under role header "AI agents &
  MCP clients", labelled "local agents" — Jarvis honesty fix (Ollama = model backend,
  not an MCP client itself). Caption updated for all three.
- README: embedded the diagram (docs/mcp-agents.svg, copy of website/assets one).
- Website hero: dropped stale "Multi-machine since 0.8"; now leads multi-machine over
  SSH + built-in read-only MCP server. (PR #75 → main; docs deploy green.)
- Verified live: hero text present, "since 0.8" gone, SVG has Claude/ChatGPT/Ollama.
