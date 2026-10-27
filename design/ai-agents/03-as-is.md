# 03 — As-is map (next @ c372022, 2026-10-24)

## Shape of the app

- **One container, Flask + stdlib.** `requirements.txt`: flask, Flask-Compress, jeepney, prometheus_client,
  mcp. **Hard rule (CONTRIBUTING + design/ai-cockpit/PLAN.md): no new pip deps**; issue #329 restates
  "plain HTTP, no heavy deps" (note: `requests` is NOT installed — use `urllib.request`/`http.client`).
- `app.py` 7,813 LOC, composition root + many globals (`LOCK` app.py:203, `DB`, `HOST_DATA`, `LATEST`…).
  `backend/` holds blueprints (`backend/api/*.py`), repos (`backend/db/repos/*.py`), collectors,
  notify, probes. 14 backend modules still `import app as _app` (back-import cycle).
- **Refactor in flight:** `design/refactor-execution-plan-v2.md` — Phase 0 gates (AST guard for
  `core/`, route-inventory snapshot, **app.py LOC ratchet**), then `core/db.py`, `core/state.py`,
  `core/act/` (single `ENABLE_CONTROLS` gate + single remote-exec module), `core/supervisor.py`.
  None of `core/` exists yet. Implication: **new agent code goes into new modules (`backend/agent/…`
  or `core/agent/…`), never grows app.py**, and must not import `app` at module top-level where avoidable.
- Frontend: `static/dashboard.html` 11,814 LOC, vanilla JS, no build. `TABS` array at
  dashboard.html:3190 with zones `monitoring | ai | settings`. i18n: `data-i18n` attributes,
  `locales/en.json` + `zh-CN.json`, loader dashboard.html:3052-3170. Theme tokens dashboard.html:17-31.
  All API calls are raw `fetch('/api/…')` (31 POST/DELETE sites, 31 mutation routes server-side).
- Ports: dashboard `PORT` 9800, MCP `MCP_PORT` 9810 (separate process, `mcp/server.py`).

## Things the feature touches or reuses

| Area | Where | Notes |
|---|---|---|
| Read-only tool set | `mcp/homelab_client.py` (556 LOC, pure stdlib, HTTP to the monitor) + `mcp/server.py:88-300` (20 `@mcp.tool()` wrappers) | **Reuse as the agent's read tools** (#328: one implementation, two transports). The functions call the dashboard's own HTTP API → in-process the agent can call them with base_url=loopback. `get_containers`/`get_services` are hub-only today (no `host` arg) — per-host read for remote boxes comes from `/api/host_data` / `get_host`. |
| Hosts registry | table `hosts(name, ssh_target, tags, added_at, last_check_*)` app.py:241; repo `backend/db/repos/hosts.py`; hub = `"local"` | Per-host agent policy needs new storage (new table keyed by host name; must follow rename `hosts.rename` and delete). |
| Remote exec primitives | `_ssh` app.py:4205, `_ssh_with_stdin` app.py:4226, `run_on_host` app.py:4444 (sudo via stdin), `run_on_host_windows` app.py:4482 | Action catalogue executes through these. No free-form command: catalogue builds the command from a fixed template + validated params (`shlex.quote`, `_ps_single_quote`). |
| Existing control endpoints | `POST /api/containers/<name>/action` (start/stop/restart, local via Docker socket, remote via SSH) integrations.py:103; `/restart-policy` :134; `POST /api/services/<name>/action` hosts_api.py:186; **`POST /api/hosts/<name>/run` hosts_api.py:154 = arbitrary shell, unauthenticated, only gated by ENABLE_CONTROLS** | #331 says acting tools call these. Name regexes `_CT_NAME_RE`, `_UNIT_NAME_RE` reusable. The `/run` route is the biggest auth risk. |
| Global kill switch | `ENABLE_CONTROLS` app.py:109 (default **on**), checked in 8 places | Stays the global master switch above the per-box policy. |
| Settings store | `settings(key,value)` table; `get_settings()` app.py:4591, `save_settings()` :4673, `SETTING_SECRETS` :4589 redacted in the UI (:7008) | Provider keys can live here as secrets (never returned), or in a dedicated table. |
| API keys | `api_keys` table app.py:277, `_hash_key` app.py:6529, `backend/auth.py` `require_api_key` (experiments push API) | Pattern for bearer tokens; **no dashboard auth/session exists** (#327 open). |
| Notifications | `backend/notify/__init__.py` `dispatch_alert(s, level, title, detail, host)` :56, `_dispatch_to_channels` :102; Discord/ntfy/Telegram/email/Slack/webhook; rules table `notification_rules` | Approval-needed + run reports go out through `dispatch_alert`. |
| Background workers | `threading.Thread(...)` app.py:7605-7610 guarded by `if "pytest" not in sys.modules`; examples `brief_worker` (daily brief, time-of-day scheduler!), `uptime_worker` in `backend/collectors/` | Agent scheduler = one more daemon thread, same guard; only started when the agent is enabled. `brief_worker` is the closest existing "scheduled job" pattern. |
| Model discovery | AI Models tab: `/api/models`, installed-models registry (`get_installed_models` in homelab_client.py:252), `PROBES` (30+ model servers), custom AI servers (`tests/test_custom_ai_servers.py`) | Source for "pick a local model" (US-1). Has host + server URL + provider kind. |
| Benchmarks | `backend/bench.py`, `/api/benchmarks` | Model housekeeping template uses "fits in VRAM" data. |
| Disk scan | `scan_disk` (local + remote over SSH) app.py:7100/7148 | Disk-space / data-cleanup templates. |
| Retention | `RETENTION_DAYS` + retention job | Audit tables follow it. |
| Schema/migrations | `SCHEMA` string in app.py (CREATE IF NOT EXISTS) + `apply_schema_migrations` `backend/db/repos/schema.py:21` (ALTER ADD COLUMN lists) | New tables via CREATE IF NOT EXISTS in a new repo module; `tests/test_migrations.py`, `test_boot_migration_order.py`. |
| Snapshot tests | `tests/snapshot_helper.py` `assert_snapshot`; new JSON endpoints need deliberate baselines (`UPDATE_SNAPSHOTS=1`) | Every new endpoint → snapshot. |
| JS tests | `tests/js/*.js` lift functions out of dashboard.html and run on node | Pattern for testing widget/cockpit pure functions. |
| Kiosk | `/?display=1` display mode | Floating widget hidden there. |

## Build, test, deploy

- Local tests: `pytest` (69 files). **Baseline on ardi (python:3.12-slim): 1086 passed, 1 skipped,
  1 failed** — `tests/test_probe_gpu.py::TestReadGpuMerge::test_no_gpu_returns_empty` (environment:
  probe finds something on that box; not ours, not fixed). On Windows the suite dies early on a
  tempfile permission error (`test_backup`) → run tests on ardi/Linux or in CI.
- CI `.github/workflows/ci.yml`: on PR to next/main and push to next → `snapshot` job
  (tests/test_snapshots.py twice for determinism), `smoke` job (py_compile, dashboard invariants,
  `tests/js/*.js` on node, docker build, boot + `/healthz`), then on push to next **publish
  `sikamikaniko123/homelab-monitor:next`** (+ `:next-<sha>`). Full pytest runs in `release-validate.yml`.
  → We should add the full pytest (or at least agent tests) to CI on PRs.
- Branching: feature branch → **PR into `next`** (CONTRIBUTING). Release from main by the maintainer.
- Staging/UAT on ardi: **`homelab-monitor-dev` = :9801 (MCP :9811)**, built locally from
  `~/homelab-monitor-dev` (branch next, `docker-compose.dev.yml`, `build: .`, data `./data-dev`) —
  it monitors the **real fleet** (6 hosts: local/ardi, vader, Workoholik, JarvisVM, cloudy, oldie).
  Deploy = `git pull` there + `docker compose -f docker-compose.dev.yml up -d --build`. Prod :9800
  = `sikamikaniko123/homelab-monitor:latest` — untouched.
- Isolated alpha instance: build the branch on ardi as `hlm-agents:alpha`, run with its own data
  volume on a free port (e.g. 9831/9841), **no real fleet registered**; give it throw-away target
  boxes (e.g. an sshd + docker-in-docker container) so actions have something safe to act on.
  Do NOT mount the real Docker socket read-write into the alpha instance (it would act on ardi's real
  containers).
- LLMs available for testing: **vLLM `qwen3.8-27b` on vader `http://100.76.27.18:18021/v1` and
  `:18022/v1`** (OpenAI-compatible, tool calls; shared with Jarvis2 → keep alpha load light);
  `ollama-embed` on ardi:11434 has only `nomic-embed-text` (no chat model). Cloud keys: none
  available to tests → cloud adapters tested with recorded/mocked HTTP.

## Jarvis2 — what the pipeline can port (C:\Projects\Jarvis2, FastAPI/async/pydantic → port to sync stdlib)

| Pattern | Jarvis2 source | Port as |
|---|---|---|
| Run / step model, budgets (`max_steps 25, max_tokens 200k, max_seconds 600`) | `packages/proto/jarvis_proto/runs.py:41,119` | `agent_runs`, `agent_steps` tables + dataclasses |
| Agent loop: stream model → tool calls → dispatch → results → repeat | `packages/core/jarvis_core/engine/loop.py:124` (`AgentLoop`) | `backend/agent/loop.py` (sync, one thread per run) |
| Repeated-failure guard (same call failed N× → refuse) | loop.py:1256 `_repeated_failure` | same logic |
| Context fitting: cut fresh results by overflow; age oldest results | loop.py:1284 `_fit_to_window`, :1309 `_compress_old_tool_results` | same logic, char-based |
| Duplicate-call replay | loop.py:1237 `_already_in_context`, :1343 `_replay` | dedupe read calls; never replay actions |
| Supervision: StepRecord(args_hash, result_hash), budget end with summary, nudge/stop | `engine/supervision.py:30-80` | simplified: budgets + repeat detection (no judge model in v1) |
| Confirmation policy `needs_confirmation(name, destructive, unattended)` | `proto/jarvis_proto/settings.py:463`, loop.py:713/810/821, `run_engine.confirm()` run_engine.py:276 | **policy engine**: host mode × action rule × risk × attended; run waits for approval (chat) or parks it (unattended) |
| Model adapters: OpenAI-compatible (stream, tool-call delta assembly `_finalize_tool_calls`), Ollama native (`_parse_tool_calls`), probe() with models + context window | `core/jarvis_core/models/openai_compat.py:119,155,280`, `ollama.py:60,152`, `base.py` (ModelError retryable, ProbeResult) | `backend/agent/providers/*.py` with urllib; add Anthropic Messages + Gemini native adapters |
| Schedules: cron/at + tz, `compute_next`, catch-up policy, `schedule_fires` UNIQUE(schedule_id, scheduled_for) = fire once per slot, TICK 15 s | `core/jarvis_core/features/schedules.py:1-60` | `agent_tasks` + `agent_task_fires`; croniter is a dep → **write a small stdlib cron parser** (5-field) |
| Lesson from 2026-10-15 incident: unattended runs skipped confirmation and rebooted 2 machines | memory: jarvisvm-reboot-recovery | unattended policy: medium/high never auto-run (owner decision Q3) |

## Related issues

#341 epic, #327 auth (gate 2026-10-25), #328 shared tools, #329 providers, #330 chat + audit,
#331 acting tools (**condition 2 superseded by owner: per-box matrix**), #338 threat model,
#325 snapshot baselines for mutations, #322 persist HEALTH, #337 contract freeze, #342 roadmap.
No open PRs touching agent code.

## Reuse vs. new

**Reuse:** homelab_client read functions (as tools), hosts registry, `run_on_host*` + name regexes,
container/service action code paths, `dispatch_alert`, settings + secret redaction, api_keys hashing,
installed-models registry, worker-thread pattern, snapshot/JS test harnesses, i18n, theme tokens,
Jarvis2 loop/guards/schedule/confirmation designs.

**New:** dashboard auth (password, sessions, bearer tokens, CSRF, fetch wrapper), provider adapters
(OpenAI-compat, Ollama, Anthropic, Gemini, Azure, Bedrock-SigV4?), tool registry shared with MCP,
action catalogue + policy engine, agent loop + run store + audit, approvals, scheduler + cron parser,
template library, cockpit tab + floating widget UI, streaming endpoint (SSE), tests + docs.
