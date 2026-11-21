# Alpha findings: #354 AI agents, pass 1 (2026-10-24)

Tester: fresh agent, user's view only. Alpha: ardi :9831. Scripts are in `scripts/`; evidence is in `shots/`.
Counts: critical 0 · high 4 · medium 13 · low 16 · cosmetic 4.

## High
1. **US-9/US-8: custom automations can never propose or run an action.**
   - Repro: New from a prompt → lab1, mode Propose → Run now; the model calls `container.restart`.
   - Expected: a pending approval. Actual: refused `not_in_task`, with "Edit the automation to add it."
   - Cause: neither the New nor the Edit dialog has an actions field, so the UI saves `actions: []`.
   - Evidence: `ui_newprompt.py`, `shots/a-desk-new-prompt-filled.png`.
2. **US-6/EC-17: phone Chat has no conversation list and no New chat.**
   - At 390 px, `#ag-new` and `.ag-convs` are hidden. The widget has `+` but no history, and "Open in Agent" lands on the same view.
   - Evidence: `shots/t1-phone-light-chat.png`, `ui_phone_conv.py`.
3. **EC-3/US-6: `host.reboot_required` returns `ok:false, exit 1` when no reboot is needed.**
   - A real qwen run ("look at lab1 and lab2 and take care of anything") did 20 good reads, then ended `failed/error_streak` with no answer.
   - Evidence: `t_real.py inject`, `t_probes.py`.
4. **US-4/EC-30: a failed rename leaves a host Active with stale rules.**
   - Repro: lab2 → lab2x (200), lab2x → lab2 (500), set lab2 Passive, lab2 → lab2x (500).
   - Result: the host is renamed anyway; the policy shows lab2x Active plus an orphan rule. Orphans stay in `agent_host_policy` / `agent_action_rules`, so any future host with that name inherits them.
   - Root: `IntegrityError UNIQUE host_samples_1h.ts,host` in `backend/db/repos/host_samples.py:185` (see #18).

## Medium
5. **US-12/EC-36: Stop takes effect only after the in-flight model call returns.**
   - Took 37 s with a 40 s stub, with no "stopping" state in the UI. Agent-off behaves the same way.
   - While a cancelled run drains, `running` reads 2 with `AGENT_MAX_RUNS=1`.
6. **The widget fails silently on send during an active run.** The API returns 409 `run_active`; the UI shows nothing.
7. **US-6: deep link `#containers?host=lab1` doesn't switch host.** Evidence: `shots/d-after-deeplink.png`.
8. **EC-30: a run whose approval a rename invalidated stays `waiting_approval` forever.** Its conversation then answers 409 until Stop.
9. **Probes break on BusyBox/Alpine hosts.**
   - `host.disk_usage` (`df -x`) and `files.*` (`find -printf`) fail with raw usage text.
   - `gpu.kernel_errors_24h` is offered without journalctl (exit 127).
10. **EC-3: the error-streak guard counts failures across different tools and hosts.** It ends productive runs with no answer.
11. **US-11: Activity filters, paging and phone detail.**
    - No task, action or date filters in the UI, and the API ignores `since`, `from` and `q`.
    - Only the newest 50 runs show, with no "load more".
    - On a phone, run detail renders about 8000 px below the tap.
12. **Agent settings accept bad values.**
    - Accepted: `chat_card_ttl_min: 0`, `approval_ttl_h: 0` or `100000`, `max_output_tokens: 1e9`, an unknown `default_provider_id`, unknown keys.
    - A partial `budget_chat` replaces the whole object; a loop then ran 61 steps.
13. **US-8: template cards and the Activate dialog hide the estimated tokens and the default model.** `est_tokens` exists in the API.
14. **EC-2: models that are untested or probed without tool calls are let through.**
    - An untested model chats without tools.
    - A model that probed "no tool calls" can be set as default, and the schedule picker offers it.
15. **US-1: "Found on your lab" lists servers, not models.** No per-model size or badge until Add; then Use is a second click.
16. **Task create accepts things that can't work.**
    - Accepted: `hosts: []`, an Off host, an ineligible template host, an unconfigured delivery channel.
    - No length limits: a 5000-char name and a 200 kB prompt are accepted.
17. **Approving an expired card returns 409 `already_decided` with `status: "pending"`.** Nothing runs, which is correct, but the error is wrong.

## Low
18. Pre-existing core bug: renaming a host back to a name it had before gives a 500 (`host_samples_1h` UNIQUE).
19. With the agent disabled, every page load logs a console 404 for `/api/agent/status`.
20. A read key can read run steps and excerpts, but the audit export of the same data gives 403.
21. The backup still includes `api_keys.key_hash`. This is not documented, and restored keys work again.
22. Over the API, `api_key: "env:NAME"` is stored as a literal key. `key_ref` accepts any `file:` path.
23. PATCH with `api_key: ""` clears the key.
24. zh-CN leaves agent strings in English: activity labels and server-side probe/test texts.
25. When the model refuses without calling the tool, the hint names the wrong setting.
26. An unknown host is reported as "is Off for the agent".
27. For about 20 s after a hub restart, actions are refused as "wrong OS".
28. No X-Frame-Options or CSP (hardening; SameSite=Lax mitigates it).
29. A cron that can never fire (`0 0 31 2 *`) is accepted silently.
30. Approving a card that is then refused at execution returns `200 status: approved`.
31. Approving in the inbox gives no running/done feedback.
32. Activity "When" shows the time but no date.
33. The restore-busy message's run count differs from `status.running`.

## Cosmetic
34. In the phone widget the Stop button overlaps the textarea, and Approve/Reject are 26 px touch targets.
35. In the phone policy table the action columns start off-screen with no scroll hint.
36. Delivery options show raw lowercase channel ids and don't mark unconfigured channels; a provider test error can dump raw HTML.
37. The "Switch the agent on" copy says every host starts Passive, but on re-enable the old policy is kept.

## Passed (verified for real)
- **Auth:** the full matrix passes, including CSRF and Origin checks, cookie flags, throttling, the MCP key, minting rules and the reset CLI.
- **Policy:** all 9 attended cells verified with `docker inspect`. Unattended: low+Allow is rate-limited 3/day, medium needs an unlock, root/high can't be unlocked.
- **Approvals:**
  - `ENABLE_CONTROLS=0` denies everything.
  - 12 parallel approves give one execution.
  - A change in policy, Deny or protected status before approval is refused and audited.
  - Expiry works and dedupe holds.
- **Injection:** the injection-named container produced no proposal with the real model.
- **XSS:** inert.
- **Secrets:** none in any response, backup, export or log.
- **Disabled agent:** 404s everywhere and no UI.
- **Kiosk:** hides the widget.
- **Scheduler:** DST preview, pause and the cloud hourly limit behave correctly.
- **Lifecycle:** hub restart → `interrupted`; offline box → failed with no retry; restore while busy → 409.
- **Provider errors:** classified correctly.
- **Real model:** handles Bulgarian, and asks which host is meant when ambiguous.

## Not tested
- EC-14 and EC-28 (Windows): no Windows box. EC-28 is missing from the user docs.
- EC-22, EC-25 and EC-26: no Ollama or nested hub in the sandbox.
- EC-27: self-update is off on the alpha.
- EC-11: the per-host lock wasn't exercised under an action.
- EC-19: the live DST check runs tonight.
- EC-20, EC-24 and restore with `force` were not exercised.

## Pass 2

| # | Severity | Finding | Status |
|---|---|---|---|
| N1 | medium | The real model's answer claimed "Action waiting on you: restart of worker on lab1" without calling the action tool (no approval, no card). | Fixed in 7a4b07f. The model's text is left as it is. Under every run the chat, widget, run detail and delivery messages show the actions the run really proposed, ran or was refused, built from its audit steps and approvals; when there were none: "No actions were proposed in this run." The system prompt says an action is only pending once its tool returned an approval id. Regression: `tests/test_agent_alpha2.py` (stub claims a pending action, never calls the tool). |
| N2 | low | security.md said read keys read everything, the agent API refuses them. | Fixed in 7a4b07f (`website/security.md`: only `/api/agent/status` answers a read key). |
| N3 | low | Activity filter lists empty for 1–2 s after the tab opens. | Fixed in 7a4b07f: policy and automations load with the tab; the selects stay disabled until their data is there. |
| N4 | low | Use offered for models probed "no tool calls". | Fixed in 7a4b07f: no Use button for them, a "can't be used" pill with a tooltip saying why. |
| N5 | cosmetic | Activity "Since" date box had no visible label. | Fixed in 7a4b07f. |
| #17 | leftover | A card already swept to expired answered `already_decided`. | Fixed in 7a4b07f: it answers `error: "expired"`; a decided card still says `already_decided`. |
