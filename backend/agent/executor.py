"""Runs one catalogue action. The single exec path for the agent.

Order, every time: policy again (it may have changed since the proposal —
EC-8) → the per-host lock (two runs never prune or restart on one host at once
— EC-11) → pre-checks (exists, not loaded, path allowed, passwordless sudo) →
audit "intent" written BEFORE anything runs (so a crash mid-action is still on
record — EC-25) → the command through ports → audit "result". Non-idempotent
actions are never retried; a host that times out is a failed result (EC-9).
"""
import json
import threading
import time

from backend.agent import audit, catalog, policy, ports, store
from backend.agent.catalog import checks

LOCK_WAIT_S = 120
_locks = {}
_locks_guard = threading.Lock()
_holder = {}


def _host_lock(host):
    with _locks_guard:
        return _locks.setdefault(host, threading.Lock())


def _trim(s, n=4000):
    s = s or ""
    return s if len(s) <= n else s[:n] + "\n[… %d more characters]" % (len(s) - n)


def _ollama_base(host):
    for s in ports.ai_servers():
        if (s.get("provider") or "").lower() == "ollama":
            return "http://%s:%s" % (s.get("ip") or "127.0.0.1", s.get("port") or 11434)
    return None


def _run_command(host, cmd, timeout_s):
    via = cmd["via"]
    if via == "ssh":
        r = ports.run_on_host(host, cmd["cmd"])
    elif via == "ps":
        r = ports.run_on_host_windows(host, cmd["script"])
    elif via == "docker_api":
        try:
            code, raw = ports.docker_req(cmd["method"], cmd["path"], body=cmd.get("body"), timeout=timeout_s)
        except Exception as e:
            print("agent executor: docker socket error: %s" % e, flush=True)
            return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "Docker socket: %s" % e}
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
        return {"ok": 200 <= code < 300 or code == 304, "exit_code": code, "stdout": text if code < 400 else "",
                "stderr": text if code >= 400 else ""}
    elif via == "systemd":
        ok, err = ports.systemd_unit_action(cmd["unit"], cmd["action"])
        return {"ok": bool(ok), "exit_code": 0 if ok else 1, "stdout": "", "stderr": err or ""}
    elif via == "ollama_api":
        from backend.agent.providers import http
        base = _ollama_base(host)
        if not base:
            return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "No Ollama server found on the hub."}
        import urllib.request
        data = json.dumps(cmd["body"]).encode() if cmd.get("body") is not None else None
        req = urllib.request.Request(base + cmd["path"], data=data, method=cmd["method"],
                                     headers={"Content-Type": "application/json"})
        try:
            with http._opener.open(req, timeout=timeout_s) as resp:
                return {"ok": True, "exit_code": resp.status, "stdout": resp.read(65536).decode("utf-8", "replace"),
                        "stderr": ""}
        except Exception as e:
            print("agent executor: ollama api error: %s" % e, flush=True)
            return {"ok": False, "exit_code": -1, "stdout": "", "stderr": str(e)[:300]}
    else:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "unknown executor"}
    if r is None:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "no such host"}
    return r


def execute(run_id, host, action_id, params, *, attended, run_mode="act", task_actions=None, approval_id=None,
            actor=None, model_base_url=None, step_kind_prefix="action"):
    """Returns {ok, refused?, reason?, message, stdout, stderr, exit_code, ms}."""
    a = catalog.get(action_id)
    d = policy.decide(host, action_id, params, attended=attended, run_mode=run_mode, task_actions=task_actions,
                      approved=bool(approval_id), model_base_url=model_base_url)
    kind, target = catalog.target_of(a or {}, params or {}) if a else ("", "")
    if d.verdict != "allow":
        why = None
        if d.reason == "protected":
            why = policy.protected_reason(host, kind, target, model_base_url)
        msg = policy.hint(d.reason, host, action_id, params, why)
        store.step_append(run_id, "policy", tool=action_id, host=host, target=target, args_json=params,
                          decision=d.verdict, decision_reason=d.reason, approval_id=approval_id, actor=actor,
                          result_excerpt=msg)
        audit.action_line("refused", run_id, host, action_id, params, decision=d.reason, approval_id=approval_id,
                          actor=actor)
        return {"ok": False, "refused": True, "verdict": d.verdict, "reason": d.reason, "message": msg}

    lock = _host_lock(host)
    if not lock.acquire(timeout=LOCK_WAIT_S):
        busy = _holder.get(host) or {}
        msg = "%s is busy with %s from another run; not retried." % (host, busy.get("action", "an action"))
        store.step_append(run_id, "note", tool=action_id, host=host, target=target, result_excerpt=msg, ok=0)
        return {"ok": False, "refused": True, "reason": "host_busy", "message": msg}
    try:
        _holder[host] = {"action": action_id, "run_id": run_id}
        pre = checks.run(a.get("pre"), host, params)
        if not pre and a.get("sudo") and host != "local" and policy.os_family(host) != "windows":
            pre = checks.sudo_nopasswd(host, params)
            if pre == "needs_sudo":
                user = None
                for h in ports.registered_hosts():
                    if h["name"] == host:
                        user = (h.get("ssh_target") or "").split("@")[0] or None
                pre = ("%s needs passwordless sudo for exactly this command. Add this line with `visudo -f "
                       "/etc/sudoers.d/homelab-agent` on %s: %s" % (host, host, catalog.sudoers_line(a, user, params)))
        if pre:
            store.step_append(run_id, "policy", tool=action_id, host=host, target=target, args_json=params,
                              decision="deny", decision_reason="precheck", approval_id=approval_id, actor=actor,
                              result_excerpt=pre)
            audit.action_line("refused", run_id, host, action_id, params, decision="precheck", detail=pre,
                              approval_id=approval_id, actor=actor)
            return {"ok": False, "refused": True, "reason": "precheck", "message": pre}
        cmd = catalog.render(a, catalog.target_key(host, policy.os_family(host)), params)
        store.step_append(run_id, "%s_intent" % step_kind_prefix, tool=action_id, host=host, target=target,
                          args_json=params, decision="allow", decision_reason=d.reason, approval_id=approval_id,
                          actor=actor)
        audit.action_line("intent", run_id, host, action_id, params, decision=d.reason, approval_id=approval_id,
                          actor=actor)
        t0 = time.monotonic()
        r = _run_command(host, cmd, a.get("timeout_s", 90))
        ms = int((time.monotonic() - t0) * 1000)
        out = {"ok": bool(r.get("ok")), "exit_code": r.get("exit_code"), "stdout": _trim(r.get("stdout")),
               "stderr": _trim(r.get("stderr"), 1500), "ms": ms}
        excerpt = (out["stdout"] or out["stderr"] or ("ok" if out["ok"] else "failed"))[:2000]
        store.step_append(run_id, "%s_result" % step_kind_prefix, tool=action_id, host=host, target=target,
                          args_json=params, ok=out["ok"], result_bytes=len(r.get("stdout") or "") +
                          len(r.get("stderr") or ""), result_excerpt=excerpt, approval_id=approval_id,
                          actor=actor, duration_ms=ms)
        audit.action_line("result", run_id, host, action_id, params, ok=out["ok"], approval_id=approval_id,
                          actor=actor, detail=None if out["ok"] else out["stderr"])
        out["message"] = ("Done." if out["ok"] else "Failed (exit %s)." % out["exit_code"])
        return out
    finally:
        _holder.pop(host, None)
        lock.release()


def probe(run_id, host, action_id, params, actor=None):
    """Read-only probes go through the same path (policy says allow for them on
    any host that isn't Off), recorded as probe steps."""
    return execute(run_id, host, action_id, params, attended=True, actor=actor, step_kind_prefix="probe")
