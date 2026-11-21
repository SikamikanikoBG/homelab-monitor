# 04 — Plan: AI agents ("Ask & Automate") — synthesis of the panel

Sources: `04-panel/architect.md` (target design, API contracts §10.5, answers §"Answers to the
challenge"), `04-panel/developer.md` (file list, T0–T23, tests, alpha sandbox, answers A1–A6),
`04-panel/devils-advocate.md` (objections, 18 hard requirements, rebuttal). Where this file and the
panel docs differ, **this file wins**.

## Chosen approach (and why)

One feature, built in new packages and wired into the existing app with ≤ 10 lines in `app.py`:

- **`backend/auth/`**: opt-in dashboard auth (#327). No password set means behaviour is identical
  to today, and the agent cannot be enabled. With a password: one `before_request` gate,
  server-side sessions (scrypt), CSRF via one global fetch wrapper (`static/csrf.js`; the 35 existing
  POST sites are untouched), and bearer keys from `api_keys` with a new `scope` (push/read/admin).
  The MCP process gets an internal GET-only token. Open paths: `/healthz`, `/metrics` (flag),
  `/public*`, `/login`, static. **`/api/hosts/<name>/run` becomes opt-in (`ENABLE_HOST_RUN=0`)**,
  and every use is logged. `esc()` quote-escaping fix ships first, as its own PR.
- **`backend/tools/`**: one read-tool registry shared by MCP and the agent (#328). `homelab_client`
  moves here (with an `mcp/` shim) and gets an injectable transport. The agent calls in-process via
  `app.test_client()` plus a WSGI flag, with no token and no network hop. Results are wrapped as
  untrusted data and paginated.
- **`backend/agent/`**: providers, action catalogue (JSON data), policy engine, loop, store,
  scheduler, templates, executor, boot. `ports.py` is the only module that touches `app`, so it can be
  re-pointed to `core/act/` when the refactor lands. The agent becomes the refactor's first client
  instead of its rival.
- **Providers (#329)**, sync and on urllib:
  - OpenAI-compatible adapter. Presets cover OpenAI, Groq, Mistral, DeepSeek, xAI, OpenRouter,
    Together, vLLM, llama.cpp, LM Studio, LocalAI, Azure OpenAI, and Gemini (labelled experimental).
  - Ollama native adapter (it gives us `num_ctx`).
  - Anthropic Messages adapter.
  - Bedrock is deferred to v1.1 (stdlib SigV4 is feasible; in the meantime it works through an
    OpenAI-compatible gateway).
  - Discovery from the installed-models registry, with a two-step tool probe (badges: tool calls /
    multi-step).
- **Policy**: one pure function, `decide()`, checked when an action is proposed and again when it
  executes.
  - Inputs, in this order: `ENABLE_CONTROLS` → protected target → host mode Off/Passive/Active →
    action rule Deny/Ask/Allow → risk × attended/unattended → per-target rate limit.
  - Defaults: new hosts start Passive; every rule is Deny until ticked.
- **Loop**: a sync port of the Jarvis2 `AgentLoop`.
  - Budgets: steps / tokens / seconds.
  - Guards: repeated-failure refusal, context fitting and ageing, dedupe within a run.
  - Runs waiting for approval are parked durably in the DB (no thread held), and resume after
    restarts.
  - A worker pool (default 2) runs chat ahead of schedules.
  - Live events ride the existing `/api/stream` as `event: agent` frames, so each tab keeps one
    connection. Text deltas are never persisted.
- **Scheduler**:
  - A stdlib 5-field cron parser with `zoneinfo`; each slot fires once.
  - Catch-up `skip` by default (`once` per task); DST gap/overlap fires once.
  - GPU courtesy for local models: defer up to 30 min, then skip.
  - Cloud schedules: a 1 h minimum interval and a daily spend cap.
  - Reports go out through `dispatch_alert` plus in-app.
- **Audit**:
  - `agent_runs/steps/approvals`; `agent_steps` is append-only, enforced by triggers.
  - Every action intent and result is also written as one JSON line to container stdout
    (`docker logs` = the copy outside the database).
  - Export as JSON/CSV.
  - Retention follows `RETENTION_DAYS`.
- **Secrets**:
  - Provider keys sit in a DB column and are never returned to the browser.
  - `key_ref: env:/file:` is supported.
  - **`/api/backup` scrubs every secret column**, including today's webhook/SMTP secrets. After a
    restore, keys must be re-entered.
- **UI** (Gate A: option C + floating widget):
  - `static/agent.js` + `agent.css` (`?v=` cache-bust), plus ~120 lines in `dashboard.html`.
  - An Agent cockpit tab: Chat 3-col / Automations / Activity / Agent policy / Models & keys.
  - A floating Ask widget on every tab, including Overview, hidden in kiosk mode.
  - Model output is rendered as text. Links are allowed only as internal `#tab?host=` deep links.

Rejected (one line each):
- **Writable MCP and letting Claude Code be the agent:** it doesn't reach users without an MCP client,
  and gives no schedules and no policy UI.
- **Calling existing HTTP mutation routes from the agent:** they're unauthenticated and the agent
  could reach `/run`. Executors call `run_on_host*` / `_docker_req` / `systemd_unit_action` directly
  through `ports.py`.
- **Hash-chained audit:** a false "verified" badge after a restore.
- **Hand-rolled key sealing:** crypto risk for little gain over scrubbing backups.
- **A separate SSE endpoint per tab:** the browser's 6-connection limit.
- **Event.wait for approvals:** it freezes workers.
- **Seven native adapters:** three code paths plus presets cover the same providers.

## Action catalogue v1 (data, `backend/agent/catalog/actions.json`)

| Action | Risk | Unattended Allow? | Notes |
|---|---|---|---|
| `container.start` / `container.restart` | low | yes (idempotent, not protected, rate-limited) | local Docker API or SSH `docker` |
| `container.stop` | medium | no (approval) | |
| `container.set_restart_policy` | low | yes | |
| `service.restart` / `service.start` | medium | no | systemd / Windows; root → capped at Ask |
| `docker.prune_dangling_images` / `docker.prune_build_cache` | low | yes | never `-a` |
| `docker.prune_stopped_containers` | medium | no | |
| `journal.vacuum` (days 1–365) | medium | no | Linux only |
| `ollama.unload_model` | low | yes | |
| `ollama.remove_model` | high | **never** (approval only, bound to digest, refused if loaded) | |
| `files.list_older_than` (read probe, allowlisted paths) | none | — | feeds data-cleanup reports |

**Out of v1:** `host.reboot`, volume removal, file deletion (v1.0.x, approval-only), any free shell.

## Templates v1

Build 1: **Docker cleanup** (auto-prunes dangling images and build cache; proposes stopped-container
prune), **Model housekeeping / "model sorting"** (inventory across hosts: duplicates, unused N days,
doesn't fit VRAM from benchmarks; unloads idle models on its own; one approval card per removal with
GB freed), **Disk space watch** (report).

Build 2: System maintenance, Data & log cleanup, GPU health check, Update report, Cost report,
Stale containers, Backup age. Every template is JSON: prompt + tool subset + default schedule/mode +
`applies_when`. An activated task keeps a copy of the template it came from.

## Work breakdown (ordered; details in developer.md §2 + A3)

**Build 1: full scope, narrowest options (for UAT)**
1. T0 prep: ardi test script, a full-pytest CI job on PRs, baseline.
2. T0b: `esc()` quote-escaping fix + attribute sweep + JS test (own PR).
3. T1–T3 auth: tables, passwords, sessions, gate, CSRF wrapper, scoped keys and minting rules,
   `ENABLE_HOST_RUN=0`, backup scrub, MCP internal token, login page, Security card, reset CLI,
   gate sweep over `app.url_map`.
4. T4 agent skeleton (disabled → 404, no threads) + `ports.py` + schema/boot.
5. T5 tool registry + in-process transport + MCP parity test.
6. T6–T8 providers: OpenAI-compatible (+ presets incl. Azure and Gemini-experimental), Ollama,
   Anthropic; discovery + two-step probe; provider CRUD with write-only keys.
7. T9–T10 catalogue + executors + policy (+ protected targets, rate limit, `agent_protected` table).
8. T11 loop + guards + store + boot recovery; stdout audit.
9. T12–T13 chat API, `/api/stream` agent frames, durable approvals (exactly-once, re-check,
   dedupe, expiry), 409 on self-update/restore while running/executing.
10. T14–T16 UI: cockpit + chat + rail, floating widget, policy tab, Models & keys.
11. T17–T18 cron + scheduler + 3 templates + Automations tab (3 modes, Run now, pause all).
12. T19 Activity + run detail + export. T20 snapshots. T22 Playwright smoke + alpha sandbox.

**Build 2:** the remaining 7 templates, `journal.vacuum`/Windows services polish, GPU courtesy +
cloud caps, metrics, Bedrock (if wanted).
**Build 3:** tuning against the real model, zh-CN, docs/website/threat model (#338), CHANGELOG.

Effort (developer's honest estimate for a human dev): ~258 h total; Build 1 ~150 h.

## Test plan → stories (developer.md §3 + A6)

| Story / EC | Tests |
|---|---|
| US-1, EC-1/2 | discovery + probe vs stub server (tool-capable, not capable, multi-step) |
| US-2, EC-12/13 | adapter fixtures per provider (text, tool deltas, malformed args, 401/429/5xx/timeout); secret-never-returned sweep |
| US-3 | auth API tests; gate sweep over every route (no password = unchanged; password = 401/403 without creds/CSRF); key-minting rules; backup scrub scan; `/run` off |
| US-4/5, EC-8/15 | `decide()` exhaustive matrix (mode × rule × risk × attended × controls × protected) as a reviewed snapshot; rename/delete hooks |
| US-6/7, EC-3/4/5/21/24 | ScriptedAdapter loop tests (guards, budgets, dedupe, context fitting); injection suite (container named "ignore previous instructions…" can at most produce a proposal) |
| US-8/9/10, EC-10/11/19 | cron DST table (Europe/Sofia 2026-10-25), once-per-slot, catch-up, overlap lock; approvals 20-thread exactly-once; expiry; dedupe |
| US-11, EC-20 | audit append-only triggers, stdout lines, export, retention, secret scan of DB after a run |
| US-12 | stop mid-run, pause all |
| EC-14 | Windows rendering/OS support tests |
| EC-16 | agent disabled: all `/api/agent/*` 404, no threads, `/api/stream` byte-identical |
| EC-17/18 | Playwright at 390 px and desktop, dark/light; i18n sync |
| EC-22 | precheck: model loaded → refuse |
| Regression | existing control endpoints + snapshots byte-identical; `esc()` JS test; agent.js globals test |

Plus the alpha (stage 7): a throw-away hub on ardi :9831 with no host Docker socket; two
sshd + docker-in-docker target boxes (one container named with an injection payload); a stub model
and the real `qwen3.8-27b` on vader (one run at a time).

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Auth retrofit breaks existing installs/scripts/MCP/Prometheus | Opt-in; no password = no change; gate sweep test; MCP internal token; `/metrics` open flag |
| Prompt injection → actions | Server-side `decide()` twice; unattended only low-risk + idempotent + not protected + rate-limited; tool results wrapped as data; text-only rendering |
| Small local models fail multi-step | Two-step probe badge; budgets/guards end runs with a clear reason; tuning via data/settings, never keyword filters |
| Cloud spend from schedules | Daily cap, 1 h minimum interval, cost shown per run |
| Agent harms its own lifeline | Protected targets from data (hub container, model server, SSH unit) + user marks |
| Conflict with refactor / contributors | New packages only, `ports.py` seam, ≤ 10 lines in app.py, small PRs |
| `dashboard.html` growth | UI lives in `agent.js`; ~120 lines in dashboard.html |
| Concurrent edit of dashboard.html by the #33 session | Coordinated; rebase before touching nav |

## Gate B — owner decisions (2026-10-24)

- Plan **approved as is** (narrow catalogue: no reboot / volume / file deletion in v1; model removal
  approval-only).
- Delivery: build on `feat/ai-agents` → alpha on a throw-away instance → **PR into `next`** → rebuild
  the ardi :9801 dev instance from `next` for UAT.
- Roadmap: **skip aligning with pehota; build all now.** Full scope = Builds 1 + 2 + 3 in this run
  (all 10 templates, Anthropic + Gemini-experimental, GPU courtesy/cloud caps, metrics, export, docs).
  No comments on #331/#342 from us. Bedrock stays deferred.

## Unresolved / for the owner (pre-Gate B notes, kept for the record)

1. **Dates vs. scope.** The full scope doesn't fit the Oct 25 auth gate or the Nov 8 freeze in #342.
   The panel recommends: ship auth + read-only chat + Models & keys for v1.0, with actions, approvals
   and automations on `next` behind the flag for v1.0.x. **Your call, posted on #342.**
   This build proceeds either way, since the agent ships disabled.
2. **Q3 narrowed for high risk.** The only high-risk action in v1 is model removal, and it is
   approval-only with no unattended unlock. Reboot, volume removal and file deletion are out of v1.
   The medium-risk unlock stays (per target, logged).
3. **pehota sign-off.** The plan overrides #331 condition 2 and #342's scope. The panel wants his
   sign-off on #331/#341 and on the `ports.py` seam before the auth PR merges.
4. Off doesn't bind MCP: MCP stays read-only across the whole fleet; the policy page says so.
5. Bedrock in v1.1 (or via a gateway).

## Definition of Done

- [ ] every US-1…US-12 acceptance criterion passes, verified through the real UI and API on the alpha instance
- [ ] every EC-1…EC-24 (and DA additions EC-25…EC-40) handled or explicitly deferred with a reason
- [ ] new unit/API/JS/Playwright tests for the feature; regression tests for touched behaviour (control endpoints, backup, keys, `esc`, `/api/stream`)
- [ ] full test suite green on Linux (ardi) and in CI (new full-pytest job on PRs)
- [ ] snapshot baselines for every new endpoint, read before committing; existing snapshots byte-identical
- [ ] agent disabled by default: zero routes/threads/frames; read-only installs unchanged (test)
- [ ] no new pip deps; `app.py` grows ≤ 10 lines; no keyword lists / hardcoded special cases
- [ ] backup contains no secrets (test); `/api/hosts/<name>/run` off by default
- [ ] deployed via the project's flow (PR → `next` → CI → image) and to the ardi :9801 dev instance, healthy after deploy
- [ ] alpha test passed with no open critical/high defects
- [ ] docs (website agent/auth/mcp pages, README env table, compose comments) + CHANGELOG entry
- [ ] #331 updated to the owner's per-box decision; #327/#328/#329/#330 linked/closed as delivered
