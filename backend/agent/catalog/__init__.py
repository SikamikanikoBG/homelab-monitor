"""The action catalogue: loading, validation and command rendering.

The catalogue is data (actions.json). This module is the only place a command
is ever built for the agent, and it builds it from a declared template plus
validated parameters — never from a string a model or a user supplied. Every
argv element is a literal or exactly `{param}`; an integer or enum parameter may
also sit inside a literal. Parameters are quoted with shlex (POSIX) or as
single-quoted PowerShell strings.
"""
import copy
import json
import os
import re
import shlex
import urllib.parse

from backend.tools import validate

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOKEN = re.compile(r"\{([a-z_]+)\}")
_cat = {}


def _load():
    if not _cat:
        with open(os.path.join(_HERE, "actions.json"), encoding="utf-8") as f:
            data = json.load(f)
        _cat["version"] = data["version"]
        _cat["actions"] = {a["id"]: a for a in data["actions"]}
    return _cat


def version():
    return _load()["version"]


def actions():
    return list(_load()["actions"].values())


def get(action_id):
    return _load()["actions"].get(action_id)


def is_probe(a):
    return a.get("risk") == "none"


def tool_name(action_id):
    """Catalogue ids use dots; tool names for models allow [a-zA-Z0-9_-] only."""
    return action_id.replace(".", "__")


def from_tool_name(name):
    aid = (name or "").replace("__", ".")
    return aid if get(aid) else None


def target_key(host, os_family):
    """Which template applies: the hub itself, or a remote's OS family."""
    if host == "local":
        return "local"
    return "windows" if os_family == "windows" else "linux"


def supported(a, host, os_family):
    return target_key(host, os_family) in a


def target_of(a, params):
    """(kind, name) the action is aimed at, for protection and rate limits."""
    t = a.get("target") or {"kind": "host"}
    if t.get("param"):
        return t["kind"], str((params or {}).get(t["param"]) or "")
    return t.get("kind", "host"), ""


def check_params(a, params):
    errs = validate(a["params"], params if params is not None else {}, "params")
    return errs


def _param_types(a):
    return {k: v for k, v in (a["params"].get("properties") or {}).items()}


def _sub_token(token, params, types, quote):
    names = _TOKEN.findall(token)
    if not names:
        return quote(token)
    if token == "{%s}" % names[0] and len(names) == 1:
        return quote(str(params[names[0]]))
    for n in names:
        t = types.get(n) or {}
        if t.get("type") != "integer" and "enum" not in t:
            raise ValueError("only integer or enum params may sit inside a literal (%s)" % n)
        token = token.replace("{%s}" % n, str(params[n]))
    return quote(token)


def _ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def _ps_literal(s):
    return s if re.fullmatch(r"[A-Za-z0-9_.:=-]+", s) else _ps_quote(s)


def _deep_sub(obj, params):
    if isinstance(obj, str):
        m = re.fullmatch(r"\{([a-z_]+)\}", obj)
        return params[m.group(1)] if m else obj
    if isinstance(obj, dict):
        return {k: _deep_sub(v, params) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_sub(v, params) for v in obj]
    return obj


def render(a, key, params):
    """The command for target `key` ('local'|'linux'|'windows'). Params must
    already be valid (check_params)."""
    spec = copy.deepcopy(a[key])
    types = _param_types(a)
    via = spec["via"]
    if via == "ssh":
        return {"via": "ssh", "cmd": " ".join(_sub_token(t, params, types, shlex.quote) for t in spec["argv"])}
    if via == "ps":
        parts = []
        for t in spec["argv"]:
            if _TOKEN.search(t):
                parts.append(_sub_token(t, params, types, _ps_quote))
            else:
                parts.append(_ps_literal(t))
        return {"via": "ps", "script": " ".join(parts)}
    if via in ("docker_api", "ollama_api"):
        path = spec["path"]
        for n in _TOKEN.findall(path):
            path = path.replace("{%s}" % n, urllib.parse.quote(str(params[n]), safe=""))
        return {"via": via, "method": spec["method"], "path": path, "body": _deep_sub(spec.get("body"), params)}
    if via == "systemd":
        return {"via": "systemd", "unit": params[a["target"]["param"]], "action": spec["action"]}
    raise ValueError("unknown executor %s" % via)


def preview(a, key):
    """What the action runs, with placeholders, for the policy page and cards."""
    spec = a.get(key)
    if not spec:
        return None
    if spec["via"] in ("ssh", "ps"):
        return " ".join(spec["argv"])
    if spec["via"] == "systemd":
        return "systemctl %s <%s> (D-Bus)" % (spec["action"], a["target"]["param"])
    return "%s %s (%s)" % (spec["method"], spec["path"].split("?")[0], "Docker API" if spec["via"] == "docker_api" else "Ollama API")


def sudoers_line(a, user, params):
    """The exact sudoers line that would let this one command run without a
    password: exact arguments, no wildcards."""
    spec = a.get("linux") or {}
    argv = [t for t in spec.get("argv", []) if t not in ("sudo", "-n")]
    types = _param_types(a)
    cmd = " ".join(_sub_token(t, params, types, lambda s: s) for t in argv)
    return "%s ALL=(root) NOPASSWD: %s" % (user or "<ssh-user>", cmd)


def public(a, os_family_by_target=None):
    return {"id": a["id"], "title": a["title"], "risk": a["risk"], "probe": is_probe(a),
            "idempotent": bool(a.get("idempotent")), "sudo": bool(a.get("sudo")),
            "unattended": a.get("unattended", "policy"), "requires": a.get("requires") or [],
            "targets": [k for k in ("local", "linux", "windows") if k in a],
            "params": a["params"], "target": a.get("target"),
            "preview": {k: preview(a, k) for k in ("local", "linux", "windows") if k in a},
            "default_rule": "deny"}
