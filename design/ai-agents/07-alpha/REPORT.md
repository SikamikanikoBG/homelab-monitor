# Alpha test report: #354 AI agents

**Verdict: GO for UAT.** No open critical or high defects.

The tester was a fresh agent that had not seen the code. It worked from the needs doc, the mockup decision, issue #354, the user docs and PROJECT.md.

Target: the throw-away alpha on ardi :9831 (two sshd+dind lab boxes, a container named like a prompt-injection payload). Models: a stub, a slow stub, and vader vLLM qwen3.8-27b. Prod :9800, dev :9801 and the real fleet were not touched.

## Passes
| Pass | Build | Critical | High | Medium | Low | Cosmetic |
|---|---|---|---|---|---|---|
| 1 | 4c87d58 | 0 | 4 | 13 | 16 | 4 |
| 2 | acf10e8 | 0 | 0 | 1 (N1) | 3 | 1 |

Pass 2 verified every pass-1 finding as fixed on the live alpha, except two:
- #25 is won't-fix: it would need filtering of model output, which RULES.md 5 forbids.
- #21 is documented: API-key hashes stay in backups.

The pass-2 items N1–N5 were sent to the builder before the merge. Their status is in FINDINGS.md.

## Verified for real (pass 2 regression sweep)
- **Auth:** the full matrix passes. Covered:
  - no session, and read, push and admin keys;
  - CSRF missing or wrong;
  - a foreign or `null` Origin;
  - cookie flags, the MCP key, and the reset path.
- **Policy, attended:** all 9 Off/Passive/Active × Deny/Ask/Allow cells were checked with `docker inspect`.
- **Policy, unattended:**
  - low+Allow is rate-limited and then becomes an approval;
  - medium risk needs an approval;
  - Propose and Report modes don't act.
- **`ENABLE_CONTROLS=0`:** denies every action, including approvals given before the switch.
- **Approvals:** 12 parallel approves → exactly one execution. A policy change, Deny or protected target before execution → refused and audited. Expiry works.
- **Prompt injection:** the real model treats the injection-named container as data.
- **XSS:** inert, including in the widget.
- **Secrets:** none in any response, backup, export or hub log.
- **Agent disabled:** 404 on every route, no UI, and no console errors.
- **Kiosk:** hides the Ask button.
- **Stop:** cancels in 0.6 s against a 40 s slow model.
- **Real model:** "take care of anything" ends with a full report (about 23 s). It answers Bulgarian in Bulgarian.
- **Screenshots:** desktop and phone, light and dark, every Agent view. No layout breaks.

## Not testable in the sandbox
- EC-14/28: Windows hosts.
- EC-22: Ollama-specific behaviour.
- EC-25/26: hub-on-host and GPU-shared cases.
- EC-27: self-update.
- EC-11: the per-host lock under an action.
- EC-19: the live DST transition on the night of 2026-10-25, with two tasks left scheduled on the alpha.

These, and the deferrals in `06-build-notes.md`, are what to watch during UAT.
