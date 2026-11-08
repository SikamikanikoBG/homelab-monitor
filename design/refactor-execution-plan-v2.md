# Refactor — Execution Plan v2

Supersedes `design/rfc-refactor.md` and `design/refactor-execution-plan.md` phases 4.2–4.4
(0–4.1 of that plan are DONE and stay — see "What's already landed" below). Implements the
target architecture at the "Phase 2 · the target — SETTLED" section of the Plan of Record
artifact (https://claude.ai/artifact/XYCyV22R7n7xv6rZDvY8sE).

**Constraint, unchanged:** single container, plain SQLite, zero extra deps, zero behaviour change.

**Hard rule, unchanged:** a PR either *moves* code (zero logic change) or *changes* behavior —
never both. Every move PR's contract is a green, byte-identical snapshot suite.

**Definition of done per phase:** snapshot suite green (and, for Phase 0, the new gates
themselves green) · code review (`/code-review medium`) run and all confirmed/plausible findings
addressed · committed.

## Contributor-disruption constraint

This is a public project — 213 stars, 30 forks, real external contributors (not just solo work).
Checked at plan time: **no open code PRs** (only 4 dependabot bumps), but release cadence is fast
and bursty (4 tags in the 9 days before this check) and at least 3 open issues touch
`app.py`/`backend/` directly — any of them could turn into a PR mid-refactor with no warning.

Rules that follow from this, applied throughout the phases below:

- **Every PR here is small and merges same-day or next-day.** No long-lived `refactor/` branch —
  same discipline as the old plan, for the same reason: a branch that outlives one release cycle
  goes stale against this cadence.
- **Phases 0–3 and most of Phase 4 are low-conflict by construction** — they add new files under
  `core/` and `entrypoints/` rather than rewriting existing ones, so an external PR touching
  `app.py` or an existing `backend/` file rebases cleanly against them without help.
- **Phase 4.3 (draining `app.py`'s route bodies into `entrypoints/http/`) is the one genuinely
  high-conflict step** — it touches every blueprint. Handle it exactly as the old plan's Phase 3
  did successfully: **announce a short window** (a GitHub issue/discussion, "`app.py` route
  extraction happening on `[date]`, please hold or rebase `app.py` PRs for ~2–3 days"), then merge
  the whole drain same-day. Given the release cadence, 2–3 days is the right size — long enough to
  not rush review, short enough that it doesn't stall anyone.
- **Before opening that window**, check `gh pr list --state open` and `gh issue list --search
  "app.py OR backend/"` again — the state checked at plan time (2026-10-01) will be stale by the
  time Phase 4.3 actually starts.

---

## What's already landed (verified at HEAD, not assumed)

| Claim in old handoff | Verified now |
|---|---|
| Snapshot gate fails on missing baseline (not self-baseline) | **True.** `tests/snapshot_helper.py:26-29` — `test_case.fail(...)` on missing path. Bug #0 from the Plan of Record is fixed. |
| `backend/db/repos/` exists, call sites migrated | **True.** 13 repo files under `backend/db/repos/`. |
| `backend/api/` blueprints, `backend/collectors/`, `backend/notify/`, `backend/probes/` extracted | **True**, all present. |
| Extraction made `app.py` thin | **False.** `app.py` is 7,809 LOC, 137 top-level globals, 50 `.LOCK` sites. 14 files under `backend/` still do `import app as _app`. The back-import cycle is exactly what the Plan of Record's "core problem" box describes — this is why Phase 2 below exists. |
| `core/` or `entrypoints/` exist | **False.** Neither directory exists yet. Everything below is new. |
| AST import guard / route inventory / LOC ratchet | **False.** None exist. These are gates, not moved code — they must land first regardless of anything else. |
| `CONTRIBUTING.md` still points at `app.py` | **True.** Line 73: "In `app.py`, add a `collect_<thing>()`...". Must be fixed in Phase 0 or the LOC ratchet is fighting the docs. |
| `test_module_identity.py` pins the `sys.modules.setdefault` hack | **True**, 3 tests, asserting the literal string is present and precedes the first backend import. **No PR in this plan removes that line** — see Phase 4. |
| 344 test bindings to `app.*` | **377** `patch`/`patch.object(app, ...)` sites measured now (grew since the artifacts' count). Budget this explicitly in Phase 2, not as cleanup after. |

---

## Phase 0 — Gates (blocks every structural PR)

No code moves in this phase. These are the instruments that prove later phases didn't change
behaviour, built *before* anything they'd need to catch.

| PR | Scope | Acceptance |
|---|---|---|
| **0.1** | AST import guard: a test that parses `core/` (once it exists — land as a no-op test against an empty/placeholder `core/__init__.py` first, so the mechanism is proven before Phase 2 needs it) and fails if any module under it imports `app`. | Test passes against empty `core/`; mutation-tested — hand-add `import app` to a dummy file under `core/` and confirm it goes red, then revert. |
| **0.2** | Route inventory snapshot: one baseline enumerating all 74 `(rule, endpoint, methods)` triples from `app.url_map`. | New test, new snapshot file; CI gate on it; a renamed/removed endpoint fails it. |
| **0.3** | Fix `CONTRIBUTING.md:73` and the "add an alert channel" line (`:92`) to point at `core/act/` and `core/observe/` (forward-reference the target layout — these lines become correct the moment Phase 2/2.4 land, not before, so mark the section "target state, in progress"). | Docs no longer actively tell contributors to grow `app.py`. |
| **0.4** | `app.py` LOC ratchet: CI step that fails if `app.py` grows versus the committed baseline (store the baseline LOC count in the workflow or a tracked file; ratchet down as later phases shrink it). | A PR that adds lines to `app.py` without shrinking elsewhere fails CI. |

**Gate: no PR past Phase 0 merges until 0.1–0.4 are green.** This mirrors the old plan's Phase 0
gate pattern (and keeps the same spirit) but targets the *new* risks (fake seam, LOC regrowth)
instead of the determinism risk the old Phase 0 already solved.

---

## Phase 1 — `core/db.py` (lowest blast radius)

Not a repeat of the old Phase 2.1–2.3 (those already landed as `backend/db/` + `backend/db/repos/`).
This phase gives `core/` a way to reach the database **without importing `app`**, which nothing
today provides — `backend/db/repos/*` still take a connection handed to them by call sites that
get it from `_app.DB`.

| PR | Scope | Acceptance |
|---|---|---|
| **1.1** | `core/__init__.py`, `core/db.py`: `conn()` (thread-local, wraps the existing `backend/db/__init__.py` factory — do not reimplement WAL/busy_timeout, import and reuse it) and `write()` as a transaction context manager. `core/` may not import `app` (checked by 0.1's now-live guard). | `core/db.py` importable standalone (no `app` in `sys.modules`); existing `backend/db/repos/*` unchanged; snapshots green. |
| **1.2** | `app.py`'s `DB` global becomes a thin call-through to `core.db.conn()` rather than the other way around — i.e. invert today's direction one global at a time, starting with `DB`. `app.DB` stays readable for the 229 existing test references, but it's now backed by `core/`, not the reverse. | `app.DB` identity-preserving for same-thread callers (existing tests pass unmodified); `core/db.py` has zero `app` import. |

This phase is deliberately small and low-risk — it's the "open the door" step, same role as the
old plan's Phase 2, but aimed at the real target instead of a re-export shim.

---

## Phase 2 — `core/state.py` (the one genuinely risky step)

This is where the old plan's three rejected designs (and this plan's own Phase 2.1 equivalent)
broke. The fix: accessor functions / a mutable holder, never `from x import y`.

| PR | Scope | Acceptance |
|---|---|---|
| **2.1** | `core/state.py`: a module-level holder object (not bare module globals) wrapping `LATEST`, `HEALTH`, `AI_SERVERS`, `HOST_DATA`, `LIVE_REV`, `FLEET_REV`. Accessor functions: `live()`, `health()`, `fleet()`, `bump_live()`, `bump_fleet()`. **No re-export of `app.LATEST` etc. — `app.py`'s globals become call-throughs to this module, not the other way around**, mirroring the direction flip in Phase 1. | `core/state.py` has zero `app` import (AST-guarded); unit tests on the holder directly (no Flask/app needed). |
| **2.2** | Migrate `backend/collectors/__init__.py`'s writes (`LATEST.update(...)`, `_app.AI_SERVERS = ...` rebinds, `bump_live()`) to call `core.state.*` accessors instead of mutating `_app.*` directly. This is the step that actually proves rebinding-safety — `AI_SERVERS` is rebound today, not mutated, which is exactly the failure mode that killed the old re-export designs. | Collector tests green; a test that rebinds `AI_SERVERS` via the accessor and confirms both `app.AI_SERVERS` (if still read anywhere) and `core.state.fleet()` agree — no desync window. |
| **2.3** | Migrate `backend/notify/__init__.py`'s reads of `_app.HEALTH`/`_app.LATEST`/`HOST_DATA` to `core.state.*` accessors. Low risk — this is pure read, and bugs #2/#3 already proved `HOST_DATA`-based alerting works without a `core/judge/` layer; this PR just relocates the read, it does not add alert-evaluation logic. | Notify tests green; alert key shapes byte-identical (protects issue #318's open naming-asymmetry question — don't touch key shape in this PR). |
| **2.4** | Budget the test-binding migration explicitly, as its own reviewed PR, not folded into 2.1–2.3: update the ~377 `patch("app.X", ...)` / `patch.object(app, "X", ...)` sites that target `LATEST`/`HEALTH`/`AI_SERVERS`/`HOST_DATA`/`LIVE_REV`/`FLEET_REV` to patch `core.state` instead, so a moved-but-silently-stale mock doesn't ship as a passing-but-meaningless test. | `grep` audit: zero test files patch `app.LATEST`/`app.HEALTH`/`app.AI_SERVERS`/`app.HOST_DATA` directly after this PR; snapshot suite still green (proves the production code path, not just the mock, moved correctly). |

Route 74 endpoints' reads of fleet state (`/api/now`, `/api/stream`, `/metrics`) are **not**
touched in this phase — they keep reading `app.*`, which now call-throughs to `core.state`. HTTP
route migration is Phase 4.

---

## Phase 3 — `core/act/`

The extension point v1 needs. Collapses the `ENABLE_CONTROLS` bare-`if` currently duplicated
across 8 view functions in 4 blueprints into one gate.

| PR | Scope | Acceptance |
|---|---|---|
| **3.1** | `core/act/__init__.py`: pure functions for each management operation (container start/stop/restart, service control, host run) wrapping today's `run_on_host`/`_ssh_with_stdin` primitives (moved from `app.py`, not reimplemented). `ENABLE_CONTROLS` checked **once**, inside `core/act/`, not per-caller. | Route inventory (0.2) stays green — same 74 routes, same methods; action-endpoint snapshots added for the 8 previously-bare-`if` routes if not already covered by 0.2/existing snapshots. |
| **3.2** | Migrate the 8 view functions to call `core.act.*` instead of inlining the check + SSH call. Each blueprint's route body shrinks to parse → call `core.act` → jsonify. | Snapshot suite green; manual grep confirms zero `ENABLE_CONTROLS` checks remain outside `core/act/`. |
| **3.3** | `core/exec/` (or keep SSH/PowerShell execution inside `core/act/` if splitting them out doesn't pay for itself — decide at implementation time, not here): the one place commands run on a remote host. This is where phase-3-of-the-product (v1) auth/audit attaches later, without redesign — don't build the hooks now, just make sure there's one call site to attach them to. | All remote command execution (SSH + PowerShell) traced to a single module; `observe/`'s probe calls and `act/`'s management calls both go through it. |

`core/act/` doubling as the v1 capability registry is a *property* of this design, not a thing
built in this phase — no registry, no `requires=('controls',)` decorator, no audit log here. That's
v1 work per the Plan of Record's explicit correction.

---

## Phase 4 — `core/observe/` + `core/supervisor.py`, then drain `app.py`

| PR | Scope | Acceptance |
|---|---|---|
| **4.1** | `core/observe/__init__.py`: move `backend/collectors/*`'s collection logic (not already covered by 2.2's state-write migration) so it calls `core.state`/`core.db`/`core.act` directly rather than through `_app.*`. | Collector/host_poller/uptime_worker/brief_worker tests green; these modules' `import app as _app` count drops to zero. |
| **4.2** | `core/supervisor.py`: `Worker(name, fn, interval, stop)` wrapping the 6 daemon threads. Supervisor owns **lifecycle only** — start/stop/status — and does **not** call `core/act/`. (No scheduled-remediation capability exists today; don't add one here.) Replace the `if "pytest" not in sys.modules` test-isolation hack with explicit `supervisor.start_all()` called from `app.py`'s composition root, never at import time. | Clean shutdown test: stop mid-write doesn't truncate a transaction; test suite no longer needs the `pytest`-in-`sys.modules` guard for worker startup. |
| **4.3** | Drain `app.py` by domain, one PR per release, each individually revertible: route registration → `entrypoints/http/`; remaining route bodies → thin callers into `core/`. This is where `entrypoints/http/` (renamed from the old plan's "adapters/", per the Plan of Record's naming correction) actually gets created as a directory — not before, since there's nothing to put in it until core/ owns the logic it's forwarding to. | `app.py` LOC ratchet (0.4) trends down each PR; route inventory (0.2) stays byte-identical throughout. |
| **4.4** | `/api/agent/tools`: generate the tool surface from `core/act/`'s functions (introspection, not a new registry — v1 builds the registry proper). This is the seam the MCP process's `HTTP loopback` reads, and the seam `entrypoints/agent/` will call into when v1 ships it — **both deferred structurally, not built now.** | Generated route serves a schema matching `core/act/`'s current function signatures; MCP's existing contract (`smithery.yaml`, OCI label) untouched. |

**What this phase explicitly does not do:** it does not delete
`sys.modules.setdefault("app", sys.modules["__main__"])` or touch `test_module_identity.py`.
`app.py` can shrink to "low hundreds of LOC" (composition root only) while that line and its guard
stay exactly as they are — the alias just has less to alias once `core/` owns the logic. Removing
it is only safe once nothing imports `app` expecting side effects from top-level execution, which
is a later, separately-reviewed step not scheduled here.

---

## Sequencing & dependencies

```
0.1─0.2─0.3─0.4 ─► 1.1─1.2 ─► 2.1─2.2─2.3─2.4 ─► 3.1─3.2─3.3 ─► 4.1─4.2─4.3─4.4
```

Linear, not forked — unlike the old plan's Phase 1/Phase 2 fork (which existed because those were
genuinely independent: rollups vs. db seam). Here, each phase's accessor seam is a hard
prerequisite for the next: `core/act/` (Phase 3) needs `core/state.py` (Phase 2) to report what
it's acting on; `core/observe/` (Phase 4) needs both.

- **Phase 0 must fully land before Phase 1.** The AST guard is worthless if written after the
  thing it's supposed to catch.
- **2.4 (test-binding migration) is not optional cleanup** — do not let it slip to "after Phase 4".
  It's sequenced inside Phase 2 deliberately, so a desync between mocked and real state can't hide
  behind a green suite for multiple phases.
- Every phase ships independently and leaves `next` releasable. If work stops after Phase 2,
  `core/db.py` + `core/state.py` landing cleanly is still a net improvement over today's back-import
  cycle — same "survives contact with real life" test the old plan used.

## Rollback

Same discipline as the old plan: **move PRs are reverted, not forward-fixed; this plan contains
no change PRs** (unlike the old plan's 1.2b/1.3, which legitimately altered output and are already
shipped). If a snapshot or the route-inventory baseline (0.2) goes red on any PR here, that's a
bug in the PR, not a declared behaviour change — fix forward or revert, never re-baseline.

## Track

One GitHub issue per PR, `refactor` label, linked to this doc and to the Plan of Record artifact.
Record `app.py` LOC at each phase boundary against the Phase-0 ratchet baseline, same as the old
plan tracked DB size/latency — so "is this working" has a number, not a feeling.
