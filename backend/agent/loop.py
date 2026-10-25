"""The agent loop: a sync port of Jarvis2's AgentLoop.

  build the prompt (policy-aware system prompt + conversation history)
  repeat:  budgets → fit the window → ask the model → persist its message
           → no tool calls: done
           → each tool call: read / probe / action (policy decides) → result
  park on an attended Ask (no thread is held); resume when someone decides.

Every model turn and tool call is a step in the audit; text deltas only ride
the live stream and are never stored. A run that can't go on ends with a plain
reason (budget, repeated failures, provider error, stopped).
"""
import json
import time

from backend.agent import (approvals, catalog, events, executor, guards, metrics, policy, ports, prompts,
                           providers, state, store, toolbox)
from backend.agent.providers.base import ChatRequest, ProviderError


class Parked(Exception):
    pass


def finish(run_id, status, reason=None, note=None):
    if not store.run_set_status_if(run_id, ("queued", "running", "waiting_approval"), status, reason):
        return False
    if note:
        store.step_append(run_id, "note", result_excerpt=note[:2000])
    run = store.run_get(run_id)
    events.publish("run", run_id=run_id, conversation_id=run["conversation_id"], status=status, reason=reason)
    metrics.run_finished(run)
    if run.get("task_id"):
        from backend.agent import scheduler
        scheduler.deliver(run)
    return True


def _say(run, text):
    """An assistant line the user sees in the chat (stop reasons, refusals)."""
    store.message_add(run["conversation_id"], "assistant", text, run_id=run["id"])
    events.publish("message", run_id=run["id"], conversation_id=run["conversation_id"])


def _history(run):
    """The conversation as adapter messages. Tool results of earlier runs are
    kept to a short head (their full text stays in the audit); the previous
    run's keep a longer one, so a follow-up ("summarise", "and lab2?") can
    answer from what was just collected instead of collecting it again (UAT-1)."""
    out = []
    msgs = store.message_list(run["conversation_id"])
    earlier = [m["run_id"] for m in msgs if m["run_id"] and m["run_id"] != run["id"]]
    prev = earlier[-1] if earlier else None
    for m in msgs:
        if m["role"] == "tool":
            msg = {"role": "tool", "content": m["content"] or "", "tool_call_id": m["tool_call_id"],
                   "name": m["tool_name"]}
            if m["run_id"] == run["id"]:
                out.append(msg)
            else:
                out.append(guards.aged(msg, guards.PREV_RUN_VIEW_CHARS if m["run_id"] == prev else guards.AGED_VIEW_CHARS))
        elif m["role"] == "assistant":
            out.append({"role": "assistant", "content": m["content"], "tool_calls": m["tool_calls"] or None,
                        "provider_meta": m["provider_meta"]})
        else:
            out.append({"role": "user", "content": m["content"] or ""})
    return out


def _pending_calls(messages):
    last = None
    for m in reversed(messages):
        if m["role"] == "assistant":
            last = m
            break
    if not last or not last.get("tool_calls"):
        return []
    answered = {m.get("tool_call_id") for m in messages if m["role"] == "tool"}
    return [c for c in last["tool_calls"] if c["id"] not in answered]


class _Ctx:
    def __init__(self, run):
        self.run = run
        self.task = store.task_get(run["task_id"]) if run.get("task_id") else None
        self.provider = store.provider_get(run["provider_id"]) if run.get("provider_id") else None
        self.model = store.model_get(run["provider_id"], run["model"]) if self.provider else None
        hosts = (self.task or {}).get("hosts") or None
        self.scope_args = dict(hosts=hosts, run_mode=run["mode"],
                               task_actions=(self.task or {}).get("actions") if self.task else None,
                               task_tools=(self.task or {}).get("tools") if self.task else None)
        budget = state.get_json("budget_chat" if run["trigger"] == "chat" else "budget_scheduled")
        budget.update((self.task or {}).get("budget") or {})
        self.watch = guards.Watch(budget)
        self.attended = bool(run["attended"])
        self.actor = run.get("actor") or ("scheduler" if run.get("task_id") else "chat")
        self.base_url = (self.provider or {}).get("base_url")
        self.cloud = bool(self.provider) and not providers.is_local(self.provider)

    def scope(self):
        return toolbox.Scope(**self.scope_args)


def run(run_id, resume=False):
    r = store.run_get(run_id)
    if not r:
        return
    if not state.runtime_enabled():
        # Re-checked at every start: the password may have been removed since.
        finish(run_id, "refused", "auth_required", "The agent is switched off or the dashboard has no password.")
        return
    if resume:
        if not store.run_set_status_if(run_id, ("waiting_approval",), "running"):
            return
    elif not store.run_set_status_if(run_id, ("queued",), "running"):
        return
    if not r.get("started_at"):
        store.run_update(run_id, started_at=store.now())
    from backend.agent import runs
    ctl = runs.manager().control(run_id)
    events.publish("run", run_id=run_id, conversation_id=r["conversation_id"], status="running")
    try:
        _run(store.run_get(run_id), ctl, resume)
    except Parked:
        pass
    except ProviderError as e:
        _provider_failed(r, e)
    finally:
        runs.manager().release(run_id)
        events.flush_text(run_id)


def _provider_failed(r, e):
    if e.kind == "cancelled":
        _say(r, "Stopped.")
        finish(r["id"], "cancelled", "stopped by the user")
        return
    if e.kind == "auth" and r.get("provider_id"):
        store.provider_tasks_warn(r["provider_id"], "key_rejected")
    metrics.provider_error(r.get("provider_id"), e.kind)
    _say(r, "I couldn't reach the model: %s." % e.human())
    finish(r["id"], "failed", "provider_%s" % e.kind, e.human())


def _run(r, ctl, resume):
    ctx = _Ctx(r)
    if not ctx.provider or not r.get("model"):
        _say(r, "No model is set up for the agent yet. Add one under Agent → Models & keys.")
        finish(r["id"], "failed", "no_model")
        return
    if not ctx.model or ctx.model.get("tools") not in ("yes", "no"):
        # Never tested (or the test couldn't reach it): test now, two small calls,
        # before letting it loose without tools (alpha #14).
        from backend.agent.providers import probe as probe_mod
        res = probe_mod.tool_probe(providers.adapter_for(ctx.provider), r["model"], cancel=ctl.cancel)
        store.model_upsert(ctx.provider["id"], r["model"], tools=res["tools"], multi_step=res["multi_step"],
                           probe_detail=res["detail"], probed_at=store.now())
        ctx.model = store.model_get(ctx.provider["id"], r["model"])
        if res["tools"] == "unknown":
            _say(r, "I couldn't test %s: %s" % (r["model"], res["detail"]))
            finish(r["id"], "failed", "model_untestable")
            return
    if ctx.model and ctx.model.get("tools") == "no":
        _say(r, "%s can't call tools, so it can't look at your lab or act. Pick a model marked "
                "\"tool calls: works\" under Agent → Models & keys." % r["model"])
        finish(r["id"], "refused", "model_without_tools")
        return
    adapter = providers.adapter_for(ctx.provider)
    ctl.adapter = adapter
    if resume:
        _resolve_pending(r, ctx, ctl)
    while True:
        if ctl.cancel.is_set():
            _say(r, "Stopped.")
            finish(r["id"], "cancelled", "stopped by the user")
            return
        if ctx.task and r["trigger"] == "schedule" and state.paused():
            finish(r["id"], "cancelled", "automations paused")
            return
        why = guards.budget_exceeded(ctx.watch) or _cost_cap(ctx)
        if why:
            _end_with_answer(r, ctx, ctl, adapter, "budget_exhausted", why, why)
            return
        req, kinds, err = _request(r, ctx)
        if err:
            _say(r, err[1])
            finish(r["id"], "failed", err[0])
            return
        if ctx.task and r["trigger"] == "schedule":
            from backend.agent import scheduler
            scheduler.gpu_courtesy_wait(ctx, ctl)
        t0 = time.monotonic()
        res = _call_model(r, ctx, ctl, adapter, req)
        store.message_add(r["conversation_id"], "assistant", res.text or None, run_id=r["id"],
                          tool_calls=res.tool_calls or None, provider_meta=res.provider_meta)
        store.step_append(r["id"], "model", tool=r["model"], tokens_in=res.usage.prompt,
                          tokens_out=res.usage.completion, duration_ms=int((time.monotonic() - t0) * 1000),
                          result_excerpt=(res.text or "")[:500] or None,
                          args_json={"tool_calls": [c["name"] for c in res.tool_calls]} if res.tool_calls else None)
        events.publish("message", run_id=r["id"], conversation_id=r["conversation_id"])
        if not res.tool_calls:
            if (res.text or "").strip():
                finish(r["id"], "done", "answered")
                if ctx.attended and r["trigger"] == "chat":
                    from backend.agent import extras
                    extras.after_answer(r, ctx, adapter, ctl)
                return
            ctx.watch.empty_replies += 1
            if ctx.watch.empty_replies >= 2:
                _say(r, "The model returned an empty answer twice, so I stopped.")
                finish(r["id"], "failed", "empty_replies", "gave up because the model returned nothing twice")
                return
            store.message_add(r["conversation_id"], "user", "(Your last reply was empty. Answer the question, "
                              "or call a tool.)", run_id=r["id"])
            continue
        for n, call in enumerate(res.tool_calls):
            _handle(r, ctx, ctl, kinds, call)
            streak = guards.error_streak_reason(ctx.watch)
            if streak:
                for rest in res.tool_calls[n + 1:]:       # every call gets an answer, or the history is invalid
                    _tool_message(r, rest, "not run: the run stopped (%s)." % streak, False, "error")
                _end_with_answer(r, ctx, ctl, adapter, "failed", "error_streak", streak)
                return


def _request(r, ctx, extra_user=None, no_tools=False):
    """(ChatRequest, kinds, None) or (None, None, (reason, text)) when it can't fit."""
    scope = ctx.scope()
    tools, kinds = toolbox.specs(scope)
    system = prompts.build(hosts=scope.hosts, modes=scope.modes, rules=scope.rules, run_mode=r["mode"],
                           attended=ctx.attended, context=r.get("context"),
                           task_prompt=(ctx.task or {}).get("prompt"), tz_name=state.tz(),
                           controls=scope.controls)
    msgs = _history(r)
    if extra_user:
        msgs.append({"role": "user", "content": extra_user})
    window = (ctx.model or {}).get("context_window") or 8192
    max_out = int(state.get("max_output_tokens") or 2048)
    limit = guards.window_chars(window, max_out)
    fixed = len(system) + len(json.dumps(tools))
    if fixed >= limit:
        return None, None, ("context_too_small", "The model's context window (%d tokens) is too small for the "
                            "agent's instructions and tools. Pick a model with a larger window." % window)
    msgs = guards.compress_old_tool_results(msgs, (limit - fixed) // 2)
    msgs = guards.fit_to_window(msgs, limit - fixed)
    if guards.total_chars(system, msgs, tools) > limit * 1.05:
        return None, None, ("context_full", "This conversation no longer fits the model's window (%d tokens). "
                            "Start a new chat." % window)
    return ChatRequest(model=r["model"], system=system, messages=msgs, tools=tools, max_tokens=max_out,
                       num_ctx=window if ctx.provider["adapter"] == "ollama" else None, no_tools=no_tools), kinds, None


def _call_model(r, ctx, ctl, adapter, req):
    on_delta = (lambda d: events.text_delta(r["id"], d)) if ctx.attended else None
    with providers.semaphore(ctx.provider):
        res = adapter.chat(req, on_delta=on_delta, cancel=ctl.cancel)
    events.flush_text(r["id"])
    ctx.watch.model_calls += 1
    ctx.watch.add_usage(res.usage.prompt, res.usage.completion)
    cost = _cost(ctx, res.usage)
    store.run_add_usage(r["id"], res.usage.prompt, res.usage.completion, cost)
    metrics.tokens(r.get("provider_id"), res.usage.prompt, res.usage.completion, cost)
    return res


WRAP_UP = ("[The run is over: %s. You get no more tool calls. Answer my last question now, from what is already in "
           "this conversation: the findings first, with numbers, then one short line on what you didn't get to "
           "check. Answer in the language I wrote in. Don't describe your steps and don't promise to continue.]")


def _wrap_up(r, ctx, ctl, adapter, why):
    """One tool-less call that turns what the run gathered into an answer, so a
    budget or a failing tool never leaves the user with nothing (UAT-1; the
    port of Jarvis2's _finish/_wrap_up). True when an answer was saved."""
    if adapter is None:
        return False
    try:
        req, _, err = _request(r, ctx, extra_user=WRAP_UP % why, no_tools=True)
        if err:
            return False
        t0 = time.monotonic()
        res = _call_model(r, ctx, ctl, adapter, req)
    except ProviderError as e:
        if e.kind == "cancelled":
            raise
        return False
    text = (res.text or "").strip()
    store.step_append(r["id"], "model", tool=r["model"], tokens_in=res.usage.prompt,
                      tokens_out=res.usage.completion, duration_ms=int((time.monotonic() - t0) * 1000),
                      result_excerpt=text[:500] or None, args_json={"wrap_up": why})
    if not text:
        return False
    store.message_add(r["conversation_id"], "assistant", text, run_id=r["id"])   # its tool calls, if any, are dropped
    events.publish("message", run_id=r["id"], conversation_id=r["conversation_id"])
    return True


def _end_with_answer(r, ctx, ctl, adapter, status, reason, why):
    """End a run that hit a budget or a wall — with an answer from what it has."""
    if not _wrap_up(r, ctx, ctl, adapter, why):
        last = next((m["content"] for m in reversed(store.message_list(r["conversation_id"]))
                     if m["run_id"] == r["id"] and m["role"] == "assistant" and (m["content"] or "").strip()), None)
        _say(r, "I stopped because %s, and couldn't write an answer from what I had." % why +
             ("\n\nLast progress: " + last.strip()[:600] if last else " Ask a narrower question to try again."))
    finish(r["id"], status, reason, "stopped because " + why)


def _cost(ctx, usage):
    m = ctx.model or {}
    return int(round(usage.prompt * float(m.get("price_in") or 0) + usage.completion * float(m.get("price_out") or 0)))


def _cost_cap(ctx):
    if not ctx.cloud:
        return None
    r = store.run_get(ctx.run["id"])
    if r["cost_micros"] >= state.get_float("cost_cap_run") * 1e6:
        return "it reached the cost cap for one run"
    day = store.now() - 86400
    if store.cost_since(day) >= state.get_float("cost_cap_day") * 1e6:
        return "today's cloud spend cap is reached"
    if ctx.task and store.cost_since(day, task_id=ctx.task["id"]) >= state.get_float("cost_cap_task_day") * 1e6:
        return "this automation's daily spend cap is reached"
    return None


def _tool_message(r, call, text, ok, kind, host=None, extra=None):
    store.message_add(r["conversation_id"], "tool", text, run_id=r["id"], tool_call_id=call["id"],
                      tool_name=call["name"])
    if kind in ("read", "error"):
        store.step_append(r["id"], "tool" if kind == "read" else "note", tool=call["name"], host=host,
                          args_json=call.get("arguments"), args_hash=guards.args_hash(call["name"], call.get("arguments")),
                          ok=ok, result_bytes=len(text), result_excerpt=text[:1500],
                          result_hash=guards.result_hash(text))
    events.publish("step", run_id=r["id"], tool=call["name"], host=host, ok=ok, kind=kind, **(extra or {}))


def _handle(r, ctx, ctl, kinds, call, approval=None):
    name, args = call["name"], call.get("arguments") or {}
    w = ctx.watch
    ah = guards.args_hash(name, args)
    if "__raw__" in args:
        text = "error: the arguments weren't valid JSON (%s). Call the tool again with a JSON object." % \
               str(args["__raw__"])[:200]
        w.record(guards.StepRecord(name, ah, guards.result_hash(text), False, text))
        _tool_message(r, call, text, False, "error")
        return
    refusal = guards.repeated_failure(w, name, args)
    if refusal:
        w.record(guards.StepRecord(name, ah, guards.result_hash(refusal), False, refusal))
        _tool_message(r, call, refusal, False, "error")
        return
    kind = kinds.get(name)
    if kind is None and catalog.from_tool_name(name):
        # A catalogue action that isn't offered here (host Passive, rule Deny,
        # controls off…): let the policy say exactly which setting would allow it.
        kind = ("action", catalog.from_tool_name(name))
    if kind is None:
        text = "error: there is no tool called %s here." % name[:80]
        w.record(guards.StepRecord(name, ah, guards.result_hash(text), False, text))
        _tool_message(r, call, text, False, "error")
        return
    what, ref = kind
    scope = ctx.scope()
    if what == "read":
        ok, text = toolbox.call_read(scope, ref, args)
        host = args.get("host") or args.get("name") if ref in ("get_host", "get_containers", "get_services") else None
        pointer = guards.already_in_context(w, name, args, text) if ok else None
        w.record(guards.StepRecord(name, ah, guards.result_hash(text), ok, text[:200]))
        _tool_message(r, call, pointer or text, ok, "read", host=host)
        return
    host = args.get("host")
    params = {k: v for k, v in args.items() if k not in ("host", "reason")}
    if what == "probe":
        res = executor.probe(r["id"], host, ref, params, actor=ctx.actor)
        text = _result_text(ref, host, res)
        w.record(guards.StepRecord(name, ah, guards.result_hash(text), bool(res.get("ok")), text[:200], "probe"))
        _tool_message(r, call, text, bool(res.get("ok")), "probe", host=host)
        return
    _action(r, ctx, call, ref, host, params, args.get("reason"), approval)


def _result_text(action_id, host, res):
    if res.get("refused"):
        return "not allowed: " + res.get("message", "")
    body = {"action": action_id, "host": host, "ok": bool(res.get("ok")), "exit_code": res.get("exit_code"),
            "meaning": res.get("meaning"), "output": res.get("stdout"), "error": res.get("stderr") or None}
    return toolbox.wrap(action_id, body)


def _already_done(run_id, action_id, host, params):
    for s in store.step_list(run_id):
        if s["kind"] == "action_result" and s["ok"] and s["tool"] == action_id and s["host"] == host \
                and (s["args"] or {}) == params:
            return s["seq"]
    return None


def _action(r, ctx, call, action_id, host, params, reason, approval):
    w = ctx.watch
    name = call["name"]
    ah = guards.args_hash(name, call.get("arguments"))
    seq = _already_done(r["id"], action_id, host, params)
    if seq and not approval:
        text = ("not run again: the same %s on %s already succeeded in step %d of this run. If it really has to "
                "run again, tell the user and let them ask." % (action_id, host, seq))
        w.record(guards.StepRecord(name, ah, guards.result_hash(text), True, text, "action"))
        _tool_message(r, call, text, True, "action", host=host)
        return
    if approval is None:
        d = policy.decide(host, action_id, params, attended=ctx.attended, run_mode=r["mode"],
                          task_actions=ctx.scope_args["task_actions"], model_base_url=ctx.base_url)
        metrics.action(action_id, d.verdict, None)
        if d.verdict == "deny":
            kind, target = catalog.target_of(catalog.get(action_id) or {}, params) if catalog.get(action_id) else ("", "")
            why = policy.protected_reason(host, kind, target, ctx.base_url) if d.reason == "protected" else None
            msg = policy.hint(d.reason, host, action_id, params, why)
            store.step_append(r["id"], "policy", tool=action_id, host=host, target=target, args_json=params,
                              decision="deny", decision_reason=d.reason, actor=ctx.actor, result_excerpt=msg)
            text = "not allowed: " + msg
            w.record(guards.StepRecord(name, ah, guards.result_hash(text), False, text, "action"))
            _tool_message(r, call, text, False, "action", host=host, extra={"decision": "deny"})
            return
        if d.verdict == "ask":
            aid, new = approvals.propose(r, call["id"], action_id, host, params, reason)
            store.step_append(r["id"], "approval", tool=action_id, host=host,
                              target=catalog.target_of(catalog.get(action_id), params)[1], args_json=params,
                              decision="ask", decision_reason=d.reason, approval_id=aid, actor=ctx.actor,
                              result_excerpt=reason)
            if ctx.attended:
                store.run_set_status_if(r["id"], ("running",), "waiting_approval", d.reason)
                events.publish("run", run_id=r["id"], conversation_id=r["conversation_id"],
                               status="waiting_approval", approval_id=aid)
                raise Parked()
            text = ("proposed for approval (#%s%s): the owner will approve or reject it later; nothing ran now."
                    % (aid[:8], "" if new else ", already pending from an earlier run"))
            w.record(guards.StepRecord(name, ah, guards.result_hash(text), True, text, "action"))
            _tool_message(r, call, text, True, "action", host=host, extra={"decision": "ask", "approval_id": aid})
            return
        res = executor.execute(r["id"], host, action_id, params, attended=ctx.attended, run_mode=r["mode"],
                               task_actions=ctx.scope_args["task_actions"], actor=ctx.actor,
                               model_base_url=ctx.base_url)
    else:
        res = approvals.execute_approved(approval, r, model_base_url=ctx.base_url)
    metrics.action(action_id, "allow", "ok" if res.get("ok") else "failed")
    text = _result_text(action_id, host, res)
    w.record(guards.StepRecord(name, ah, guards.result_hash(text), bool(res.get("ok")) or bool(res.get("refused")),
                               text[:200], "action"))
    _tool_message(r, call, text, bool(res.get("ok")), "action", host=host)


def _resolve_pending(r, ctx, ctl):
    """After an approval decision: answer the parked call, then any calls the
    model made in the same turn after it."""
    msgs = store.message_list(r["conversation_id"])
    pending = _pending_calls(msgs)
    scope = ctx.scope()
    _, kinds = toolbox.specs(scope)
    for call in pending:
        appr = None
        for a in store.approval_list(run_id=r["id"]):
            if a.get("call_id") == call["id"]:
                appr = a
                break
        if appr is None:
            _handle(r, ctx, ctl, kinds, call)
            continue
        if appr["status"] == "approved":
            kinds.setdefault(call["name"], ("action", appr["action"]))
            _action(r, ctx, call, appr["action"], appr["host"], appr["params"], None, appr)
        else:
            what = {"rejected": "The owner rejected this. Nothing was run; don't try it another way.",
                    "expired": "Nobody approved this in time. Nothing was run."}.get(
                appr["status"], "This wasn't approved (%s). Nothing was run." % appr["status"])
            _tool_message(r, call, what, False, "action", host=appr["host"], extra={"decision": appr["status"]})
