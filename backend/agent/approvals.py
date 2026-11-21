"""Approvals: the human in the loop.

- An attended chat run that hits Ask parks: the approval is a row in the
  database, the run's status is `waiting_approval`, and no thread waits for
  it. Approve/Reject queues a resume job (durable across restarts).
- An unattended run parks its proposal the same way, notifies the owner once,
  and carries on; approving later executes the action against the original
  run's audit trail. A pending proposal of the same action/host/params from a
  later night is counted on the existing card instead of a new one.
- Deciding is exactly once: a conditional UPDATE … WHERE status='pending' — two
  tabs, a double click or a replay all collapse to one decision (EC-7).
- Executing re-checks the policy, the catalogue version and whatever the
  approval is bound to (a model's digest) at that moment (EC-8, EC-27).
- Chat cards expire after 30 minutes, unattended proposals after 24 hours.
"""
import json
import time

from backend.agent import audit, catalog, events, ports, state, store
from backend.agent.catalog import checks


def _ttl(attended):
    if attended:
        return int(float(state.get("chat_card_ttl_min") or 30) * 60)
    return int(float(state.get("approval_ttl_h") or 24) * 3600)


def propose(run, call_id, action_id, host, params, rationale):
    a = catalog.get(action_id)
    bind = None
    if a.get("bind") == "model_digest":
        bind = checks.model_digest(host, params)
    aid, new = store.approval_propose(run["id"], run.get("task_id"), call_id, action_id, host, params, a["risk"],
                                      (rationale or "")[:600] or None, bool(run["attended"]), catalog.version(),
                                      _ttl(bool(run["attended"])), bind)
    events.publish("approval", approval_id=aid, run_id=run["id"], status="pending", new=new, **store.counts())
    if new and not run["attended"]:
        _notify(aid, action_id, host, run)
    return aid, new


def _notify(aid, action_id, host, run):
    a = catalog.get(action_id)
    title = "Agent: approval needed — %s on %s" % (a["title"], host)
    detail = "An automation proposed this and is waiting for you. Open /#agent to approve or reject. " \
             "(approval %s)" % aid[:8]
    try:
        task = store.task_get(run["task_id"]) if run.get("task_id") else None
        channels = ((task or {}).get("delivery") or {}).get("channels")
        out = [c for c in (channels or []) if c != "inapp"]
        if channels is None or "all" in out:
            ports.dispatch_alert("warning", title, detail)      # chat run / "all channels"
        elif out:
            ports.dispatch_to_channels("warning", title, detail, out)
    except Exception as e:                       # a broken alert channel must not break the run
        print("agent: approval notification failed: %s" % e, flush=True)


def decide(aid, approve, actor):
    """Returns (http_status, payload)."""
    appr = store.approval_get(aid)
    if not appr:
        return 404, {"ok": False, "error": "not_found"}
    to = "approved" if approve else "rejected"
    if not store.approval_transition(aid, "pending", to, actor=actor):
        cur = store.approval_get(aid)
        if cur["status"] in ("pending", "expired"):       # past its time, swept or not (alpha #17)
            if cur["status"] == "pending":
                expire()
            return 409, {"ok": False, "error": "expired", "status": "expired",
                         "message": "This approval expired; nothing was run."}
        return 409, {"ok": False, "error": "already_decided", "status": cur["status"]}
    audit.approval_line(aid, to, actor)
    events.publish("approval", approval_id=aid, run_id=appr["run_id"], status=to, **store.counts())
    from backend.agent import runs
    run = store.run_get(appr["run_id"])
    if run and run["status"] == "waiting_approval":
        runs.manager().submit("resume", run["id"])
    elif approve:
        runs.manager().submit("approval", aid)
    return 200, {"ok": True, "status": to}


def execute_approved(appr, run, actor_hint=None, model_base_url=None):
    """Run an approved action exactly once. Returns the executor's result dict."""
    from backend.agent import executor
    if not store.approval_transition(appr["id"], "approved", "executing"):
        cur = store.approval_get(appr["id"])
        return {"ok": False, "refused": True, "reason": "already_" + (cur or {}).get("status", "handled"),
                "message": "This approval was already handled (%s)." % (cur or {}).get("status")}
    if appr.get("catalogue_version") != catalog.version():
        msg = "The action catalogue changed since this was proposed; the agent must propose it again."
        store.approval_transition(appr["id"], "executing", "refused", result=msg)
        return {"ok": False, "refused": True, "reason": "catalogue_changed", "message": msg}
    if appr.get("bind"):
        now = checks.model_digest(appr["host"], appr["params"])
        if now != appr["bind"]:
            msg = "%s changed since this was proposed (re-pulled or replaced); nothing was deleted." % \
                  appr["params"].get("model", "The target")
            store.approval_transition(appr["id"], "executing", "refused", result=msg)
            return {"ok": False, "refused": True, "reason": "bound_changed", "message": msg}
    task_actions = None
    if run and run.get("task_id"):
        task = store.task_get(run["task_id"])
        task_actions = (task or {}).get("actions")
    res = executor.execute(appr["run_id"], appr["host"], appr["action"], appr["params"],
                           attended=bool(appr["attended"]), run_mode="act", task_actions=task_actions,
                           approval_id=appr["id"], actor=appr.get("decided_by") or actor_hint,
                           model_base_url=model_base_url)
    final = "done" if res.get("ok") else ("refused" if res.get("refused") else "failed")
    store.approval_transition(appr["id"], "executing", final,
                              result=(res.get("message") or "") + " " + (res.get("stdout") or res.get("stderr") or "")[:500])
    events.publish("approval", approval_id=appr["id"], run_id=appr["run_id"], status=final, **store.counts())
    return res


def run_approval_job(aid):
    """Worker job for an unattended approval approved after its run ended."""
    appr = store.approval_get(aid)
    if not appr or appr["status"] != "approved":
        return
    run = store.run_get(appr["run_id"])
    res = execute_approved(appr, run)
    events.publish("step", run_id=appr["run_id"], kind="action_result", ok=bool(res.get("ok")))


def expire(now=None):
    """Expire overdue approvals; chat runs parked on one are cancelled."""
    rows = store.approvals_expire(now)
    for r in rows:
        audit.approval_line(r["id"], "expired", None)
        if r.get("attended"):
            store.run_set_status_if(r["run_id"], ("waiting_approval",), "cancelled", "approval_timeout")
            events.publish("run", run_id=r["run_id"], status="cancelled")
        events.publish("approval", approval_id=r["id"], run_id=r["run_id"], status="expired", **store.counts())
    return len(rows)


def serialize(a):
    act = catalog.get(a["action"]) or {"title": a["action"], "risk": a.get("risk")}
    from backend.agent import policy
    key = catalog.target_key(a["host"], policy.os_family(a["host"])) if a["host"] in ports.host_names() else "linux"
    return {"id": a["id"], "run_id": a["run_id"], "task_id": a.get("task_id"), "call_id": a.get("call_id"),
            "action": a["action"],
            "title": act["title"], "host": a["host"], "params": a["params"], "risk": a["risk"],
            "rationale": a.get("rationale"), "status": a["status"], "attended": bool(a.get("attended")),
            "seen_count": a.get("seen_count") or 1, "created_at": a["created_at"],
            "last_proposed_at": a.get("last_proposed_at"), "expires_at": a["expires_at"],
            "decided_at": a.get("decided_at"), "decided_by": a.get("decided_by"),
            "result": a.get("result_excerpt"), "command_preview": catalog.preview_with(act, key, a["params"]) if "params" in act else None,
            "host_mode": policy.mode_for(a["host"]), "rule": policy.rule_for(a["host"], a["action"])[0]}
