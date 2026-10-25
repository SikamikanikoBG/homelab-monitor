"""What a run actually did with actions, from its own records (alpha pass 2, N1).

The model's final text is never filtered or rewritten. Next to it, the chat,
the widget, run detail and every delivery message show this factual list,
built only from the run's audit steps and approval rows: the actions it
proposed, ran or was refused, and the approvals still pending. A model that
claims "an action is waiting on you" without having called the action tool is
contradicted by the list right under its answer ("No actions were proposed").
"""
from backend.agent import catalog, store

# what each outcome is called in the plain-text footer (deliveries, logs)
LABELS = {"pending": "Waiting for your approval", "approved": "Approved", "rejected": "Rejected",
          "expired": "Expired, not run", "refused": "Refused", "cancelled": "Cancelled", "ran": "Ran",
          "failed": "Failed"}
NONE = "No actions were proposed in this run."


def _is_action(action_id):
    a = catalog.get(action_id or "")
    return bool(a) and not catalog.is_probe(a)


def facts(run_id):
    """[{outcome, action, title, host, target, approval_id, detail}] in audit order."""
    out, by_approval = [], {}
    for s in store.step_list(run_id):
        if not _is_action(s["tool"]):
            continue
        a = catalog.get(s["tool"])
        f = {"action": s["tool"], "title": a.get("title") or s["tool"], "host": s["host"], "target": s["target"],
             "approval_id": s["approval_id"], "detail": None}
        if s["kind"] == "approval" and s["approval_id"]:
            appr = store.approval_get(s["approval_id"]) or {}
            if s["approval_id"] in by_approval:
                continue
            f.update(outcome=appr.get("status") or "pending", detail=appr.get("result_excerpt"))
            by_approval[s["approval_id"]] = f
        elif s["kind"] == "action_result":
            f.update(outcome="ran" if s["ok"] else "failed", detail=(s["result_excerpt"] or "")[:300] or None)
            if s["approval_id"] in by_approval:               # the approved card ran in this run: one line
                by_approval[s["approval_id"]].update(outcome=f["outcome"], detail=f["detail"])
                continue
        elif s["kind"] == "policy" or (s["kind"] == "note" and s["ok"] == 0):
            f.update(outcome="refused", detail=(s["result_excerpt"] or s["decision_reason"] or "")[:300] or None)
        else:
            continue
        out.append(f)
    return out


def footer(items, link_prefix="/"):
    """The plain-text form, for delivery messages."""
    if not items:
        return NONE
    lines = ["Actions in this run:"]
    for f in items:
        what = "%s: %s on %s%s" % (LABELS.get(f["outcome"], f["outcome"]), f["title"], f["host"] or "?",
                                   (" (%s)" % f["target"]) if f["target"] else "")
        if f["outcome"] == "pending":
            what += " — %s#agent/approvals" % link_prefix
        elif f["detail"] and f["outcome"] in ("refused", "failed"):
            what += " — " + f["detail"][:160]
        lines.append("- " + what)
    return "\n".join(lines)


def work(run):
    """What a run looked at, for the one line over its answer: "Checked 4
    machines · 1 look-up · 6 s". A fleet-wide read counts every host the agent
    can see; others count the host they name (the tool's declared host field)."""
    from backend.agent import policy, ports
    from backend.tools.read import BY_NAME
    hosts, lookups, fleet = set(), 0, False
    for s in store.step_list(run["id"]):
        if s["kind"] == "tool":
            lookups += 1
            spec = BY_NAME.get(s["tool"])
            if spec is None:
                continue
            if spec.host_scope == "fleet":
                fleet = True
            elif spec.host_scope == "hub":
                hosts.add("local")
            else:
                hosts.add((s["args"] or {}).get(spec.host_arg) or "local")
        elif s["kind"] in ("probe_result", "action_result", "policy", "approval"):
            lookups += 1
            if s["host"]:
                hosts.add(s["host"])
    if fleet:
        hosts |= {h for h in ports.host_names() if policy.mode_for(h) != "off"}
    end = run.get("ended_at") or (store.now() if run.get("started_at") else None)
    secs = max(0, end - run["started_at"]) if end and run.get("started_at") else None
    return {"machines": len(hosts), "lookups": lookups, "seconds": secs}
