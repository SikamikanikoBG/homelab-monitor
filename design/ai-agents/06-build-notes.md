# 06 — Build notes (#354, branch `feat/ai-agents`, PR #355)

What was built differs from `04-plan.md` + panel only where noted below. Each
deviation picks the option closest to the plan's intent.

## Deviations and decisions made while building

1. **Agent DB access uses the dashboard's own handle (`app.DB` + `app.LOCK`)
   through `ports.db()`, not a thread-local WAL connection.** A restore swaps the
   file and only `app.DB` is reopened (`reopen_db`); thread-local handles would
   keep reading the old, unlinked file. Tests also run on the in-memory fallback.
   The lock is held only for SQL, never across a model call or SSH.
2. **`homelab_client` stays in `mcp/`** (loaded by path from `backend/tools/read.py`),
   and `mcp/server.py` keeps its `@mcp.tool()` declarations. Moving them would
   break the MCP contract freeze (#337) and `tests/test_mcp_tool_docs.py`. One
   registry still exists (`backend/tools/read.py`) and a parity test checks names
   and parameters against `mcp/server.py`. The agent adds an optional `host` to
   `get_containers`/`get_services` (declared as agent-only).
3. **Three switches**: `AGENT_ENABLED=1` (env; unset = every `/api/agent/*` is 404,
   no threads, no tables, no UI) + a password + the runtime Enable toggle.
4. **No `/api/agent/events` SSE endpoint**: agent frames ride `/api/stream`
   (architect answer 6). Polling every 1.5 s is the fallback.
5. **Action dedupe inside a run has no "unless the model explains why" escape**
   (EC-21): a model that always fills `reason` would defeat it. A repeat returns
   "already done in step N".
6. **Audit**: append-only via UPDATE/DELETE triggers (DELETE only below the
   retention cutoff) + stdout JSON lines. No hash chain (architect answer 3).
7. **`/api/hosts/<name>/run` off by default**: the two existing snapshot tests
   for it now run with `ENABLE_HOST_RUN=1` patched (their baselines are
   unchanged, byte for byte); a new baseline covers the default 403.
8. **`esc()`**: fixed to escape `"` and `'` (and `mdMini`'s local helper). The other
   raw `${…}` attribute interpolations in dashboard.html carry developer
   constants (i18n strings, numbers, ids) and were left as they are; agent.js
   builds no HTML from template literals (JS test).
9. **Gemini** goes through its OpenAI-compatible endpoint, labelled
   experimental, with a schema down-converter and opaque provider fields echoed
   back. No native adapter (the plan allowed skipping it). **Bedrock deferred**
   (Gate B), reachable through an OpenAI-compatible gateway.
10. **No shipped price table** (prices change too often): the cloud spend caps
    count only models the owner gave a price. Tokens and step/time budgets apply
    regardless.
11. **GPU courtesy** checks utilisation and free VRAM of the GPU host behind a
    local model endpoint before a scheduled run's first model call; "prefer an
    already-loaded model" is not implemented.
12. **Host rename/delete hooks** sit in `backend/api/hosts_api.py` (the only caller
    of `rename_host`/`delete_host`), so `app.py` grows by **2 lines** in total.
13. **Protected targets from data**: the hub's container (`_self_container_name`),
    whatever serves the run's model (provider host matched to the hub's AI servers
    or a registered host's loaded models), the unit listening on the host's SSH
    port. Docker/D-Bus daemons aren't auto-protected (no data names them); the
    policy page invites users to mark VPN/DNS/proxy themselves.
14. **Approval notifications** go out once per new approval (not grouped per run).
15. **Prompt tuning against qwen3.8-27b (T23)**: the system prompt lists the
    catalogue ids and says how an action gets enabled; a call to a catalogue action
    that isn't offered now goes through policy so the reply names the exact
    setting. No output filters, no keyword lists.
16. **Flaky test found by the new full-pytest CI job**: `test_db_factory` hit
    "database is locked" when two threads switched a new file to WAL; fixed in
    `open_db_connection` (retry the switch).
17. **zh-CN**: all 308 new strings translated (coverage stays 90.8%).

## Edge cases — status

| EC | Status |
|---|---|
| 1 no model | Chat empty state + `no_model` 400 with a link to Models & keys |
| 2 no tool calls | Two-step probe; runs on a `tools=no` model are refused with a message |
| 3 loops / malformed JSON | budgets, repeated-failure refusal, error streak, empty-reply nudge (tests) |
| 4 huge results | 12k-char cap with a "narrow it" note; window fitting |
| 5 injection | tool results wrapped as untrusted data; policy twice; unattended medium/high only propose (test + live alpha container) |
| 6 unicode / RTL | round-trip test; qwen answered in Bulgarian on the alpha |
| 7 double approve | conditional UPDATE; 20-thread test → one execution |
| 8 policy changed before execution | re-check at execution (test) |
| 9 host offline / timeout | failed result, never retried (test) |
| 10 restart mid-run / missed slots | boot recovery → interrupted; UNIQUE(task, slot); catch-up skip/once (tests) |
| 11 overlap | per-host lock (test) + `skipped_overlap` |
| 12 429/5xx/timeout | retry before first byte only, classified errors (tests) |
| 13 revoked key | `key_rejected` warning on tasks (test) |
| 14 Windows | OS family per host, PowerShell rendering, unsupported actions greyed (tests) |
| 15 ENABLE_CONTROLS=0 | every action Deny, probes still allowed, banner (tests) |
| 16 agent disabled | 404s, no threads/tables/frames (test_agent_disabled) |
| 17 phone / themes | Playwright at 390 px, dark + light screenshots |
| 18 i18n | all strings via keys; test + i18n-sync --check |
| 19 DST | cron table tests (Europe/Sofia); live DST tasks on the alpha for 2026-10-25 |
| 20 audit growth | retention prune keeps pending; excerpts ≤ 2–4 KB |
| 21 same action twice | dedupe (test) |
| 22 loaded model | `model_not_loaded` pre-check (test) |
| 23 empty fleet | templates eligible on the hub alone (test) |
| 24 long conversation | older runs' tool results aged (test) |
| 25 hub = itself | own container protected; intent logged before running |
| 26 model host | its serving container/service protected for the run using it (test) |
| 27 self-update mid-run | 409 while running/executing; `force` cancels; approvals carry catalogue version |
| 28 Windows sudo/timeouts | no sudo on Windows; **deferred**: `run_on_host_windows`'s 30 s timeout can report a slow Restart-Service as failed (existing helper, unchanged) |
| 29 offline host | approval on an offline host fails, no retry |
| 30 rename/delete | policy follows rename; pending approvals refused (test) |
| 31 same name on many hosts | every card and tool call carries the host |
| 32 multi-tab | runs in workers, streamed from the store; closing a tab never stops a run |
| 33 thread pressure | worker pool (`AGENT_MAX_RUNS`, default 2), chat first |
| 34 restore while enabled | 409 while busy; after restore pending approvals expire and it is logged |
| 35 two hubs, one fleet | **documented only** (website/agents.md); no cross-instance lock in v1 |
| 36 agent disabled after use | pending approvals expire, queued/running runs cancel, scheduler stops, audit and keys stay (test) |
| 37 prose refusal | counts as done, not a failure (test) |
| 38 clock skew | hub time only |
| 39 50 hosts × actions on a phone | host picker + one host's action table at a time, horizontal scroll |
| 40 kiosk with login | kiosk keeps a 30-day session cookie; no token in URLs; Ask hidden in kiosk |

## Not done here (by instruction or outside the builder's remit)

- Merge to `next`, the `:next` image and the :9801 dev deploy — the orchestrator does these after the alpha test.
- Updating #331 / linking #327–#330 — instructions said not to comment on #331/#342.
- Co-maintainer sign-off on the `ports.py` seam (DA hard requirement 17).
