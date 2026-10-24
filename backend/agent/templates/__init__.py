"""Automation templates: one JSON file each, reviewed like code.

A template = a prompt + the read tools and probes it may use + the catalogue
actions it may propose + `requires` (host capabilities it needs, which filter
the hosts it is offered for) + a default schedule, mode and budget. Activating
one copies all of that into the task, so the audit says what actually ran, and
a newer template version shows as "update available" instead of changing a
task silently.
"""
import json
import os

from backend.agent import catalog, cron, policy, ports, store
from backend.tools.read import BY_NAME as READ

_HERE = os.path.dirname(os.path.abspath(__file__))
_cache = {}


def all_templates():
    if not _cache:
        for fn in sorted(os.listdir(_HERE)):
            if fn.endswith(".json"):
                with open(os.path.join(_HERE, fn), encoding="utf-8") as f:
                    t = json.load(f)
                _cache[t["id"]] = t
    return list(_cache.values())


def get(tid):
    all_templates()
    return _cache.get(tid)


def problems(t):
    """Everything wrong with a template definition (used by a test over all files)."""
    out = []
    for name in t.get("tools") or []:
        if name not in READ:
            out.append("unknown read tool %s" % name)
    for pid in t.get("probes") or []:
        a = catalog.get(pid)
        if not a or not catalog.is_probe(a):
            out.append("unknown probe %s" % pid)
    for aid in t.get("actions") or []:
        a = catalog.get(aid)
        if not a or catalog.is_probe(a):
            out.append("unknown action %s" % aid)
    try:
        cron.Cron(t.get("default_cron"))
    except cron.CronError as e:
        out.append(str(e))
    if t.get("default_mode") not in ("report", "propose", "act"):
        out.append("bad default_mode")
    for k in ("id", "version", "name", "desc", "prompt"):
        if not t.get(k):
            out.append("missing " + k)
    return out


def eligible_hosts(t):
    req = set(t.get("requires") or [])
    modes = store.host_modes()
    out = []
    for h in ports.host_names():
        if modes.get(h, policy.DEFAULT_MODE) == "off":
            continue
        if req <= policy.capabilities(h):
            out.append(h)
    return out


def task_from(t, overrides):
    """A new task's fields from a template plus the Activate dialog's choices."""
    return {
        "name": overrides.get("name") or t["name"],
        "template_id": t["id"], "template_version": t["version"],
        "prompt": overrides.get("prompt") or t["prompt"],
        "tools": list(t.get("tools") or []) + list(t.get("probes") or []),
        "actions": list(t.get("actions") or []),
        "hosts": overrides.get("hosts") or [],
        "cron": overrides.get("cron") or t["default_cron"],
        "mode": overrides.get("mode") or t["default_mode"],
        "budget": dict(t.get("budget") or {}, **(overrides.get("budget") or {})),
    }


def public(t, with_hosts=True):
    acts = [{"id": a, "risk": (catalog.get(a) or {}).get("risk"), "title": (catalog.get(a) or {}).get("title")}
            for a in t.get("actions") or []]
    out = {"id": t["id"], "version": t["version"], "name": t["name"], "desc": t["desc"], "prompt": t["prompt"],
           "tools": t.get("tools") or [], "probes": t.get("probes") or [], "actions": acts,
           "requires": t.get("requires") or [], "default_cron": t["default_cron"],
           "default_mode": t["default_mode"], "budget": t.get("budget") or {}, "est_tokens": t.get("est_tokens"),
           "runs_alone": [a["id"] for a in acts if a["risk"] == "low"],
           "waits_for_approval": [a["id"] for a in acts if a["risk"] != "low"]}
    if with_hosts:
        out["eligible_hosts"] = eligible_hosts(t)
    return out
