"""Getting from "the agent exists" to a first answer, and what the Overview shows (UAT-1).

The owner's UAT: "password, then a model, then I don't know what", and nothing
on the home page said the agent was there. Everything here is read from the
agent's own data; the page only lays it out.

- setup_state(): which of the three setup steps are done (password, a model
  that can call tools, the agent switched on with a permission preset), so the
  wizard resumes where it stopped and the Overview knows which card to show.
- model_choices(): the models the hub knows, with what their test found and
  one marked recommended (calls tools; local first, then the largest context,
  then multi-step) — from probe data, never from model names.
- overview(): the "Ask your lab" card's right side: approvals waiting, and what
  the automations did lately, one line each from the run's own answer.
"""
import re

from backend.agent import outcome, policy, ports, providers, state, store

OVERNIGHT_HOURS = 36
OVERNIGHT_MAX = 3
LINE_MAX = 140


def model_choices():
    provs = {p["id"]: p for p in store.provider_list()}
    out = []
    for m in store.model_list():
        p = provs.get(m["provider_id"])
        if not p or not p.get("enabled", 1):
            continue
        out.append({"provider_id": p["id"], "provider": p["label"], "preset": p["preset"],
                    "local": providers.is_local(p), "model": m["model"], "context_window": m["context_window"],
                    "tools": m["tools"] or "unknown", "multi_step": m["multi_step"] or "unknown",
                    "probed": bool(m["probed_at"]), "recommended": False})
    usable = [c for c in out if c["tools"] == "yes"]
    if usable:
        best = max(usable, key=lambda c: (c["local"], c["context_window"] or 0, c["multi_step"] == "yes"))
        best["recommended"] = True
    out.sort(key=lambda c: (not c["recommended"], c["tools"] != "yes", not c["local"], -(c["context_window"] or 0),
                            c["model"]))
    return out


def _default_model_ok():
    pid, model = state.get("default_provider_id"), state.get("default_model")
    if not pid or not model or not store.provider_get(pid):
        return False
    m = store.model_get(pid, model)
    return bool(m and m.get("tools") == "yes")


def setup_state():
    """What the Overview and the wizard need. Before a password exists the
    dashboard is open, so this is all the agent says to anyone (no data)."""
    pw = state.password_set()
    out = {"allowed": state.env_allowed(), "password_set": pw, "model_ready": False, "enabled": False,
           "preset": "", "dismissed": False, "done": False, "next_step": "password"}
    if not pw:
        return out
    out.update(model_ready=_default_model_ok(), enabled=state.runtime_enabled(), preset=policy.preset(),
               dismissed=state.get("onboarding_dismissed") == "1")
    out["done"] = out["model_ready"] and out["enabled"]
    out["next_step"] = "model" if not out["model_ready"] else "permissions" if not out["enabled"] else None
    return out


def found_count():
    """How many models the hub can see on the network (the invitation's "2 found")."""
    from backend.agent.providers import discovery
    try:
        return sum(len(c.get("models") or []) for c in discovery.discover() if c.get("reachable"))
    except Exception as e:                       # discovery is a nicety on this card, never a failure
        print("agent: discovery for the setup card failed: %s" % e, flush=True)
        return 0


_MD = re.compile(r"[*_`#>]+")


def one_line(text):
    """The first sentence of an answer, as plain text, for a one-line summary."""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("|") or set(line) <= set("-=|: "):
            continue
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
        line = _MD.sub("", line).strip(" -•")
        if not line:
            continue
        m = re.search(r"(?<=[.!?。！？])\s", line)
        if m and m.start() >= 20:
            line = line[:m.start()]
        return line if len(line) <= LINE_MAX else line[:LINE_MAX - 1].rsplit(" ", 1)[0] + "…"
    return ""


def run_line(run):
    """One line for a finished automation run: its answer's first sentence, else what its records say."""
    msgs = [m for m in store.message_list(run["conversation_id"])
            if m["role"] == "assistant" and m["run_id"] == run["id"] and (m["content"] or "").strip()]
    line = one_line(msgs[-1]["content"]) if msgs else ""
    if not line:
        line = outcome.footer(outcome.facts(run["id"])).splitlines()[-1].lstrip("- ")
    return line


def overview():
    from backend.agent import approvals as approvals_mod
    pid, model = state.get("default_provider_id"), state.get("default_model")
    prov = store.provider_get(pid) if pid else None
    pend = store.approval_list(status="pending", limit=50)
    tasks = {t["id"]: t["name"] for t in store.task_list()}
    waiting = []
    for a in pend[:3]:
        s = approvals_mod.serialize(a)
        waiting.append({"id": s["id"], "action": s["action"], "title": s["title"], "host": s["host"],
                        "params": s["params"], "risk": s["risk"],
                        "created_at": s["created_at"], "task": tasks.get(a.get("task_id")), "run_id": s["run_id"]})
    since = store.now() - OVERNIGHT_HOURS * 3600
    lately = []
    for r in store.run_list(since=since, limit=40):
        if not r.get("task_id") or r["status"] in ("queued", "running"):
            continue
        lately.append({"run_id": r["id"], "task": tasks.get(r["task_id"]) or r.get("title"), "status": r["status"],
                       "at": r["ended_at"] or r["created_at"], "line": run_line(r)})
        if len(lately) >= OVERNIGHT_MAX:
            break
    return {"model": {"provider_id": pid or None, "model": model or None, "provider": (prov or {}).get("label"),
                      "local": bool(prov) and providers.is_local(prov)},
            "pending_count": len(pend), "waiting": waiting, "lately": lately,
            "hosts": len(ports.host_names()), "preset": policy.preset()}
