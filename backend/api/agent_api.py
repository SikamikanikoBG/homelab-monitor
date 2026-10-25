"""backend/api/agent_api.py — /api/agent/* (#354).

Unless AGENT_ENABLED=1, every route here is a 404: the blueprint is registered
but answers nothing. With the env switch on, `status` explains what is still
missing; everything else needs an admin password; and starting or continuing
work (chat, Run now, approvals) needs the owner's "Enable agent" toggle.
Provider keys are write-only: no response here ever contains one.
"""
import json
import re
import time

from flask import Blueprint, Response, abort, jsonify, request

from backend.agent import (approvals, audit, boot, catalog, cron, onboarding, policy, ports, providers, scheduler,
                           state, store, templates)
from backend.agent.providers import discovery, probe as probe_mod
from backend.agent.providers.base import ProviderError
from backend.auth.marks import sensitive
from backend.tools.read import READ_TOOLS

bp = Blueprint("agent_api", __name__)

# Running or continuing work needs the agent switched on; the rest doesn't
# (it is how the owner sets it up first).
_NEEDS_ON = {"agent_api.post_message", "agent_api.task_run", "agent_api.approval_decision"}
_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]{0,254}$")
_CHANNELS = ("inapp", "all", "discord", "ntfy", "telegram", "email", "slack", "webhook")


@bp.before_request
def _guard():
    if not state.env_allowed():
        abort(404)
    if request.endpoint in ("agent_api.status", "agent_api.setup_get"):
        return None
    from backend.auth import gate
    p = gate.principal()
    if p.get("kind") in ("bearer", "internal") and p.get("scope") != "admin":
        # The agent's conversations and audit are admin data, like its export (alpha #20).
        return _err(403, "scope", "The agent API needs a dashboard login or an admin key.")
    if not state.password_set():
        return _err(403, "password_required", "Set a dashboard password first (Settings → Security).")
    if request.endpoint in _NEEDS_ON and not state.runtime_enabled():
        return _err(409, "agent_disabled", "The agent is switched off. Turn it on in the Agent tab.")
    return None


def _err(code, error, message=None, **extra):
    return jsonify(dict({"ok": False, "error": error, "message": message or error}, **extra)), code


def _body():
    return request.get_json(silent=True) or {}


def _actor():
    from backend.auth import gate
    p = gate.principal()
    return "%s:%s" % (p.get("kind"), p.get("id"))


def _local_iso(ts, tz_name):
    if not ts:
        return None
    from datetime import datetime
    try:
        return datetime.fromtimestamp(ts, cron.zone(tz_name)).isoformat(timespec="minutes")
    except cron.CronError:
        return None


# ── lifecycle & settings ─────────────────────────────────────────────────────

@bp.route("/api/agent/status")
def status():
    pw = state.password_set()
    out = {"allowed": True, "version": ports.version(), "password_set": pw, "enabled": False, "paused": False,
           "controls_enabled": ports.controls_enabled(), "pending_approvals": 0, "running": 0,
           "default": {"provider_id": None, "model": None}, "has_provider": False}
    if pw:
        s = state.get_all()
        out.update(enabled=state.runtime_enabled(), paused=s["paused"] == "1",
                   default={"provider_id": s["default_provider_id"] or None, "model": s["default_model"] or None},
                   has_provider=bool(store.provider_list()))
        out.update(store.counts())
    return jsonify(out)


@bp.route("/api/agent/enable", methods=["POST"])
def enable():
    on = bool(_body().get("enabled"))
    state.put({"enabled": "1" if on else "0"}, actor=_actor())
    store.admin_log("agent_toggle", "enabled" if on else "disabled", {}, actor=_actor())
    if on:
        boot.start_if_enabled()
    else:
        boot.on_disabled()
    return jsonify({"ok": True, "enabled": state.runtime_enabled()})


@bp.route("/api/agent/pause", methods=["POST"])
def pause():
    on = bool(_body().get("paused"))
    state.put({"paused": "1" if on else "0"}, actor=_actor())
    n = 0
    if on:
        from backend.agent import runs
        n = runs.manager().cancel_all("automations paused", scheduled_only=True)
    return jsonify({"ok": True, "paused": on, "cancelled_runs": n})


_SETTING_KEYS = ("default_provider_id", "default_model", "tz", "approval_ttl_h", "chat_card_ttl_min",
                 "budget_chat", "budget_scheduled", "cost_cap_run", "cost_cap_task_day", "cost_cap_day",
                 "rate_limit_per_day", "gpu_busy_util_pct", "gpu_min_free_vram_mb", "gpu_defer_max_min",
                 "cloud_min_interval_s", "max_output_tokens")
_NUMERIC = ("approval_ttl_h", "chat_card_ttl_min", "cost_cap_run", "cost_cap_task_day", "cost_cap_day",
            "rate_limit_per_day", "gpu_busy_util_pct", "gpu_min_free_vram_mb", "gpu_defer_max_min",
            "cloud_min_interval_s", "max_output_tokens")


def _settings_out():
    s = state.get_all()
    out = {k: s[k] for k in _SETTING_KEYS}
    for k in ("budget_chat", "budget_scheduled"):
        out[k] = state.get_json(k)
    out["tz_effective"] = state.tz()
    out["channels_configured"] = ports.channels_configured()
    out["currency"] = (ports.dashboard_settings() or {}).get("currency") or "$"
    return out


# Sensible ranges, so a typo can't turn a budget off or a card into an
# instant expiry (alpha #12).
_BOUNDS = {"approval_ttl_h": (1, 720), "chat_card_ttl_min": (1, 1440), "cost_cap_run": (0, 1000),
           "cost_cap_task_day": (0, 1000), "cost_cap_day": (0, 10000), "rate_limit_per_day": (0, 100),
           "gpu_busy_util_pct": (1, 100), "gpu_min_free_vram_mb": (0, 1048576), "gpu_defer_max_min": (0, 240),
           "cloud_min_interval_s": (600, 604800), "max_output_tokens": (64, 32768)}
_BUDGET_BOUNDS = {"max_steps": (1, 100), "max_seconds": (10, 7200), "max_tokens": (1000, 2000000)}


def _check_budget(cur, new, field):
    """A partial budget is merged into the current one; every value checked."""
    if not isinstance(new, dict):
        return None, "%s must be an object" % field
    out = dict(cur)
    for k, v in new.items():
        if k not in _BUDGET_BOUNDS:
            return None, "%s.%s isn't a budget setting" % (field, k)
        lo, hi = _BUDGET_BOUNDS[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            return None, "%s.%s must be between %s and %s" % (field, k, lo, hi)
        out[k] = int(v)
    return out, None


def _model_refused(pid, model):
    m = store.model_get(pid, model) if pid and model else None
    return bool(m and m.get("tools") == "no")


@bp.route("/api/agent/settings", methods=["GET", "PUT"])
def settings():
    if request.method == "PUT":
        b = _body()
        unknown = sorted(k for k in b if k not in _SETTING_KEYS)
        if unknown:
            return _err(400, "unknown_setting", "Unknown setting(s): %s" % ", ".join(unknown), fields=unknown)
        upd = dict(b)
        if upd.get("tz"):
            try:
                cron.zone(upd["tz"])
            except cron.CronError as e:
                return _err(400, "bad_tz", str(e), field="tz")
        for k, (lo, hi) in _BOUNDS.items():
            if k in upd:
                try:
                    v = float(upd[k])
                except (TypeError, ValueError):
                    v = None
                if v is None or not lo <= v <= hi:
                    return _err(400, "bad_value", "%s must be between %s and %s" % (k, lo, hi), field=k)
                upd[k] = str(upd[k])
        for k in ("budget_chat", "budget_scheduled"):
            if k in upd:
                merged, err = _check_budget(state.get_json(k), upd[k], k)
                if err:
                    return _err(400, "bad_value", err, field=k)
                upd[k] = merged
        for k in ("default_provider_id", "default_model"):
            if k in upd:
                upd[k] = str(upd[k] or "")[:200]
        if upd.get("default_provider_id") and not store.provider_get(upd["default_provider_id"]):
            return _err(400, "unknown_provider", "That provider doesn't exist.", field="default_provider_id")
        pid = upd.get("default_provider_id", state.get("default_provider_id"))
        if upd.get("default_model") and _model_refused(pid, upd["default_model"]):
            return _err(400, "model_without_tools", "That model can't call tools, so the agent can't use it.",
                        field="default_model")
        state.put(upd, actor=_actor())
    return jsonify(_settings_out())


# ── providers & models ───────────────────────────────────────────────────────

@bp.route("/api/agent/presets")
def presets():
    return jsonify({"presets": providers.public_presets()})


@bp.route("/api/agent/providers", methods=["GET", "POST"])
@sensitive
def providers_route():
    if request.method == "GET":
        return jsonify({"providers": [providers.serialize(p) for p in store.provider_list()]})
    b = _body()
    p = providers.preset(b.get("preset"))
    if not p:
        return _err(400, "bad_preset", "Unknown provider type.", field="preset")
    base = (b.get("base_url") or p.get("base_url") or "").strip().rstrip("/")
    err = providers.check_base_url(base)
    if err:
        return _err(400, "bad_url", err, field="base_url")
    key, ref = providers.split_key(b.get("api_key"), b.get("key_ref"))
    err = providers.check_key_ref(ref)
    if err:
        return _err(400, "bad_key_ref", err, field="key_ref")
    extra = {k: str(v)[:200] for k, v in (b.get("extra") or {}).items() if k in (p.get("extra_fields") or [])}
    row = {"id": store.new_id(), "preset": p["id"], "adapter": p["adapter"],
           "label": (b.get("label") or p["label"]).strip()[:80], "base_url": base, "api_key": key,
           "key_ref": ref, "key_last4": key[-4:] if key else None, "extra": extra,
           "source": "discovered" if b.get("source") == "discovered" else "manual"}
    store.provider_insert(row)
    store.admin_log("provider", row["id"], {"added": p["id"], "base_url": base}, actor=_actor())
    if not state.get("default_provider_id"):
        state.put({"default_provider_id": row["id"]}, actor=_actor())
    return jsonify({"ok": True, "provider": providers.serialize(store.provider_get(row["id"]))})


@bp.route("/api/agent/providers/<pid>", methods=["PATCH", "DELETE"])
def provider_one(pid):
    cur = store.provider_get(pid)
    if not cur:
        return _err(404, "not_found")
    if request.method == "DELETE":
        store.provider_delete(pid)
        if state.get("default_provider_id") == pid:
            state.put({"default_provider_id": "", "default_model": ""}, actor=_actor())
        store.admin_log("provider", pid, {"deleted": True}, actor=_actor())
        return jsonify({"ok": True})
    b = _body()
    upd = {}
    if "label" in b:
        upd["label"] = str(b["label"])[:80]
    if "enabled" in b:
        upd["enabled"] = 1 if b["enabled"] else 0
    if "base_url" in b:
        base = (b["base_url"] or "").strip().rstrip("/")
        err = providers.check_base_url(base)
        if err:
            return _err(400, "bad_url", err, field="base_url")
        upd["base_url"] = base
        if providers.url_host(base) != providers.url_host(cur["base_url"]) and "api_key" not in b:
            # A key is never pointed at a different server without being re-entered.
            upd.update(api_key=None, key_last4=None)
    if "api_key" in b and b["api_key"] is None:
        upd.update(api_key=None, key_last4=None)             # explicit null clears the stored key
    elif (b.get("api_key") or "").strip():                   # an empty string changes nothing (alpha #23)
        key, ref = providers.split_key(b["api_key"], None)
        if ref:
            err = providers.check_key_ref(ref)
            if err:
                return _err(400, "bad_key_ref", err, field="api_key")
            upd.update(key_ref=ref, api_key=None, key_last4=None)
        else:
            upd.update(api_key=key, key_last4=key[-4:])
    if "key_ref" in b:
        err = providers.check_key_ref(b["key_ref"])
        if err:
            return _err(400, "bad_key_ref", err, field="key_ref")
        upd["key_ref"] = b["key_ref"] or None
    if "extra" in b:
        p = providers.preset(cur["preset"]) or {}
        upd["extra_json"] = json.dumps({k: str(v)[:200] for k, v in (b["extra"] or {}).items()
                                        if k in (p.get("extra_fields") or [])})
    store.provider_update(pid, upd)
    store.admin_log("provider", pid, {"changed": sorted(upd)}, actor=_actor())
    return jsonify({"ok": True, "provider": providers.serialize(store.provider_get(pid))})


@bp.route("/api/agent/providers/<pid>/test", methods=["POST"])
def provider_test(pid):
    row = store.provider_get(pid)
    if not row:
        return _err(404, "not_found")
    return jsonify(_test_provider(row))


def _test_provider(row):
    """List a provider's models (and their windows) into the store; the test result."""
    pid = row["id"]
    t0 = time.monotonic()
    try:
        ad = providers.adapter_for(row)
        found = ad.list_models()
    except ProviderError as e:
        res = {"ok": False, "error_kind": e.kind, "detail": e.human(),
               "latency_ms": int((time.monotonic() - t0) * 1000), "models": []}
        if e.kind == "auth":
            store.provider_tasks_warn(pid, "key_rejected")
        store.provider_update(pid, {"last_test_at": store.now(), "last_test_json": json.dumps(res)})
        return res
    for m in found[:200]:
        fields = {"context_window": m.context_window} if m.context_window else {}
        store.model_upsert(pid, m.id, **fields)
    if row["adapter"] == "ollama":
        for m in found[:20]:
            window, hint = ad.show(m.id)
            cur = store.model_get(pid, m.id) or {}
            fields = {"context_window": window} if window else {}
            if hint and cur.get("tools") in (None, "unknown"):
                fields["tools"] = hint
            if fields:
                store.model_upsert(pid, m.id, **fields)
    res = {"ok": True, "error_kind": None, "detail": "%d model(s)" % len(found),
           "latency_ms": int((time.monotonic() - t0) * 1000), "models": [m.id for m in found[:200]]}
    store.provider_update(pid, {"last_test_at": store.now(), "last_test_json": json.dumps(dict(res, models=None))})
    store.provider_tasks_warn(pid, None)
    return res


def _model_out(m):
    return {"provider_id": m["provider_id"], "model": m["model"], "context_window": m["context_window"],
            "tools": m["tools"], "multi_step": m["multi_step"], "detail": m["probe_detail"],
            "probed_at": m["probed_at"], "price_in": m["price_in"], "price_out": m["price_out"]}


@bp.route("/api/agent/models", methods=["GET", "PUT"])
def models():
    if request.method == "PUT":
        b = _body()
        model = str(b.get("model") or "")[:200]
        if not store.provider_get(b.get("provider_id")) or not model:
            return _err(400, "bad_model")
        fields = {}
        for k in ("price_in", "price_out", "context_window"):
            if k in b:
                try:
                    v = float(b[k])
                    if v < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    return _err(400, "bad_value", "%s must be a positive number" % k, field=k)
                fields[k] = int(v) if k == "context_window" else v
        store.model_upsert(b["provider_id"], model, **fields)
        return jsonify({"ok": True, "model": _model_out(store.model_get(b["provider_id"], model))})
    found = discovery.discover() if request.args.get("discover", "1") != "0" else []
    return jsonify({"discovered": found, "models": [_model_out(m) for m in store.model_list()]})


@bp.route("/api/agent/models/probe", methods=["POST"])
def model_probe():
    b = _body()
    row = store.provider_get(b.get("provider_id"))
    model = str(b.get("model") or "")[:200]
    if not row or not model:
        return _err(400, "bad_model")
    cur = store.model_get(row["id"], model) or {}
    ad = providers.adapter_for(row)
    with providers.semaphore(row):
        res = probe_mod.tool_probe(ad, model,
                                   num_ctx=cur.get("context_window") if row["adapter"] == "ollama" else None)
    store.model_upsert(row["id"], model, tools=res["tools"], multi_step=res["multi_step"],
                       probe_detail=res["detail"], probed_at=store.now())
    return jsonify(dict(res, context_window=cur.get("context_window")))


# ── setup and the Overview card (UAT-1) ──────────────────────────────────────

_FOUND = {"at": 0, "n": 0}


@bp.route("/api/agent/setup")
def setup_get():
    """Which setup steps are done. Open like /status: before a password exists
    the dashboard is open anyway, and this carries no lab data."""
    out = onboarding.setup_state()
    if not out["done"] and request.args.get("found") == "1":
        if time.time() - _FOUND["at"] > 60:
            _FOUND.update(at=time.time(), n=onboarding.found_count())
        out["found"] = _FOUND["n"]
    return jsonify(out)


@bp.route("/api/agent/setup/dismiss", methods=["POST"])
def setup_dismiss():
    on = _body().get("dismissed", True)
    state.put({"onboarding_dismissed": "1" if on else "0"}, actor=_actor())
    return jsonify(onboarding.setup_state())


def _found_label(preset, c):
    """"vLLM on vader" when the server belongs to a fleet host, else "vLLM at 10.0.0.9"."""
    if c.get("host") and c["host"] != "local":
        return ("%s on %s" % (preset["label"], c["host"]))[:80]
    return ("%s at %s" % (preset["label"], providers.url_host(c["base_url"]) or "?"))[:80]


@bp.route("/api/agent/setup/models", methods=["GET", "POST"])
def setup_models():
    """GET: the models the hub knows, with test results and a recommendation.
    POST: also find model servers on the network and add the reachable ones as
    providers, listing their models (the wizard then tests each model)."""
    if request.method == "POST":
        from backend.agent.providers import discovery
        added = []
        for c in discovery.discover():
            if not c.get("reachable") or c.get("provider_id"):
                continue
            p = providers.preset(c["preset"])
            if not p or providers.check_base_url(c["base_url"]):
                continue
            row = {"id": store.new_id(), "preset": p["id"], "adapter": p["adapter"],
                   "label": _found_label(p, c),
                   "base_url": c["base_url"].rstrip("/"), "api_key": None, "key_ref": None, "key_last4": None,
                   "extra": {}, "source": "discovered"}
            store.provider_insert(row)
            store.admin_log("provider", row["id"], {"added": p["id"], "base_url": row["base_url"], "by": "setup"},
                            actor=_actor())
            _test_provider(store.provider_get(row["id"]))
            added.append(row["id"])
        return jsonify({"added": added, "models": onboarding.model_choices()})
    return jsonify({"models": onboarding.model_choices()})


@bp.route("/api/agent/policy/preset", methods=["PUT"])
def policy_preset():
    name = _body().get("preset")
    if name not in policy.PRESETS:
        return _err(400, "bad_preset", "Pick one of: %s." % ", ".join(policy.PRESETS), field="preset")
    policy.apply_preset(name, actor=_actor())
    return jsonify({"ok": True, "preset": policy.preset()})


@bp.route("/api/agent/setup/finish", methods=["POST"])
def setup_finish():
    """The wizard's last step, all checked here: a model that calls tools, a
    preset, optional starter routines with their defaults; then the agent is on."""
    b = _body()
    pid, model = b.get("provider_id") or state.get("default_provider_id"), b.get("model") or state.get("default_model")
    m = store.model_get(pid, model) if pid and model and store.provider_get(pid) else None
    if not m or m.get("tools") != "yes":
        return _err(400, "model_not_ready", "Pick a model that passed the test (it has to call tools).", field="model")
    name = b.get("preset") or policy.preset() or "ask"
    if name not in policy.PRESETS:
        return _err(400, "bad_preset", "Pick one of: %s." % ", ".join(policy.PRESETS), field="preset")
    tids = [t for t in (b.get("templates") or []) if isinstance(t, str)][:10]
    for tid in tids:
        if not templates.get(tid):
            return _err(400, "bad_template", "Unknown routine: %s" % tid[:60], field="templates")
    state.put({"default_provider_id": pid, "default_model": model}, actor=_actor())
    policy.apply_preset(name, actor=_actor())
    created, skipped = [], []
    for tid in tids:
        tpl = templates.get(tid)
        hosts = templates.eligible_hosts(tpl)
        if any(t.get("template_id") == tid for t in store.task_list()):
            skipped.append({"template_id": tid, "why": "already active"})
            continue
        t, errs = _task_fields({"template_id": tid, "hosts": hosts, "provider_id": pid, "model": model})
        if errs:
            skipped.append({"template_id": tid, "why": "; ".join("%s: %s" % kv for kv in errs.items())})
            continue
        nxt = cron.next_after(t["cron"], t["tz"], int(time.time()))
        t["next_fire"] = nxt[0] if nxt else None
        new = store.task_insert(t)
        store.admin_log("task", new, {"created": tid, "by": "setup", "hosts": t["hosts"]}, actor=_actor())
        created.append(new)
    state.put({"enabled": "1"}, actor=_actor())
    store.admin_log("agent_toggle", "enabled", {"by": "setup"}, actor=_actor())
    boot.start_if_enabled()
    return jsonify({"ok": True, "state": onboarding.setup_state(), "tasks": created, "skipped": skipped})


@bp.route("/api/agent/overview")
def overview():
    return jsonify(onboarding.overview())


# ── tools, catalogue ─────────────────────────────────────────────────────────

@bp.route("/api/agent/tools")
def tools():
    acts = catalog.actions()
    return jsonify({"read": [t.public() for t in READ_TOOLS],
                    "probes": [catalog.public(a) for a in acts if catalog.is_probe(a)],
                    "actions": [catalog.public(a) for a in acts if not catalog.is_probe(a)],
                    "catalogue_version": catalog.version()})


# ── conversations & chat ─────────────────────────────────────────────────────

def _conv_out(c):
    return {"id": c["id"], "title": c["title"], "origin": c["origin"], "task_id": c["task_id"],
            "created_at": c["created_at"], "updated_at": c["updated_at"], "archived": bool(c["archived"]),
            "last_status": c.get("last_status")}


@bp.route("/api/agent/conversations", methods=["GET", "POST"])
def conversations():
    if request.method == "POST":
        cid = store.conversation_create((_body().get("title") or "")[:200] or None)
        return jsonify({"ok": True, "id": cid})
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except ValueError:
        limit = 50
    items = store.conversation_list(origin=request.args.get("origin") or None, limit=limit,
                                    before=request.args.get("cursor") or None)
    nxt = items[-1]["updated_at"] if len(items) == limit else None
    return jsonify({"items": [_conv_out(c) for c in items], "next": nxt})


def _msg_out(m):
    return {"id": m["id"], "role": m["role"], "content": m["content"], "run_id": m["run_id"],
            "tool_calls": [{"id": c["id"], "name": c["name"], "arguments": c.get("arguments")}
                           for c in (m["tool_calls"] or [])] or None,
            "tool_call_id": m["tool_call_id"], "tool_name": m["tool_name"], "created_at": m["created_at"]}


def _run_out(r):
    return {"id": r["id"], "conversation_id": r["conversation_id"], "trigger": r["trigger"],
            "task_id": r["task_id"], "scheduled_for": r["scheduled_for"], "attended": bool(r["attended"]),
            "mode": r["mode"], "provider_id": r["provider_id"], "model": r["model"], "status": r["status"],
            "status_reason": r["status_reason"], "steps": r["steps"], "prompt_tokens": r["prompt_tokens"],
            "completion_tokens": r["completion_tokens"], "cost": round((r["cost_micros"] or 0) / 1e6, 6),
            "context": r.get("context"), "hosts": r.get("hosts"), "actor": r["actor"], "title": r.get("title"),
            "created_at": r["created_at"], "started_at": r["started_at"], "ended_at": r["ended_at"]}


def _run_full(r):
    """A run with the factual list of what it did with actions (alpha pass 2, N1)."""
    from backend.agent import outcome
    out = _run_out(r)
    out["actions"] = outcome.facts(r["id"])
    out["actions_text"] = outcome.footer(out["actions"])
    out["work"] = outcome.work(r)
    from backend.agent import extras
    out["followups"] = extras.followups(r["id"]) if r["status"] == "done" else []
    return out


@bp.route("/api/agent/conversations/<cid>", methods=["GET", "PATCH", "DELETE"])
def conversation_one(cid):
    c = store.conversation_get(cid)
    if not c:
        return _err(404, "not_found")
    if request.method == "DELETE":
        if store.active_run_in_conversation(cid):
            return _err(409, "run_active", "Stop the running request first.")
        store.conversation_delete(cid)
        return jsonify({"ok": True})
    if request.method == "PATCH":
        b = _body()
        store.conversation_update(cid, title=b.get("title"), archived=b.get("archived"))
        c = store.conversation_get(cid)
    runs = [r for r in store.run_list(limit=500) if r["conversation_id"] == cid]
    cards = []
    for r in runs:
        cards += [approvals.serialize(a) for a in store.approval_list(run_id=r["id"])]
    return jsonify({"conversation": _conv_out(c), "messages": [_msg_out(m) for m in store.message_list(cid)],
                    "runs": [_run_full(r) for r in sorted(runs, key=lambda r: (r["created_at"], r["id"]))],
                    "approvals": sorted(cards, key=lambda a: (a["created_at"], a["id"]))})


def _clean_context(ctx):
    if not isinstance(ctx, dict):
        return None
    out = {}
    for k in ("tab", "host"):
        v = ctx.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()[:60]
    return out or None


@bp.route("/api/agent/conversations/<cid>/messages", methods=["POST"])
def post_message(cid):
    if not store.conversation_get(cid):
        return _err(404, "not_found")
    b = _body()
    text = (b.get("text") or "").strip()
    if not text:
        return _err(400, "empty", "Type a question first.")
    if len(text) > 8000:
        return _err(400, "too_long", "That message is too long (8,000 characters max).")
    if store.active_run_in_conversation(cid):
        return _err(409, "run_active", "Wait for the current answer, or stop it.")
    pid = b.get("provider_id") or state.get("default_provider_id") or None
    model = b.get("model") or state.get("default_model") or None
    if not pid or not store.provider_get(pid) or not model:
        return _err(400, "no_model", "Pick a model first under Agent → Models & keys.")
    rid = store.run_create(cid, "chat", True, "act", provider_id=pid, model=model,
                           context=_clean_context(b.get("context")), actor=_actor())
    store.message_add(cid, "user", text, run_id=rid)
    from backend.agent import extras
    store.conversation_touch(cid, title_if_empty=extras.fallback_title(text) or None)
    from backend.agent import events, runs
    events.publish("run", run_id=rid, conversation_id=cid, status="queued")
    runs.manager().submit("run", rid)
    return jsonify({"ok": True, "run_id": rid, "conversation_id": cid}), 202


# ── runs & audit ─────────────────────────────────────────────────────────────

def _run_filters():
    a = request.args
    f = {k: a.get(k) or None for k in ("trigger", "task_id", "host", "action", "status", "q")}
    for keys, col in ((("from", "since"), "since"), (("to", "until"), "until"), (("cursor",), "before")):
        v = next((a.get(k) for k in keys if a.get(k)), None)
        f[col] = int(v) if v and v.isdigit() else None
    return f


@bp.route("/api/agent/runs")
def runs_list():
    try:
        limit = max(1, min(500, int(request.args.get("limit", 50))))
    except ValueError:
        limit = 50
    items = store.run_list(limit=limit, **_run_filters())
    nxt = items[-1]["created_at"] if len(items) == limit else None
    return jsonify({"items": [dict(_run_out(r), label=lbl) for r, lbl in zip(items, _run_labels(items))], "next": nxt})


def _run_labels(items):
    """What a run was, in a line: the routine's name, else the question that started it
    (several runs share a conversation and so its title, UAT-1)."""
    from backend.agent import extras
    qs = store.run_questions([r["id"] for r in items])
    tasks = {t["id"]: t["name"] for t in store.task_list()}
    out = []
    for r in items:
        if r.get("task_id"):
            out.append(tasks.get(r["task_id"]) or r.get("title"))
        else:
            out.append(extras.fallback_title(qs.get(r["id"]) or "") or r.get("title"))
    return out


def _step_out(s):
    return {"seq": s["seq"], "ts": s["ts"], "kind": s["kind"], "tool": s["tool"], "host": s["host"],
            "target": s["target"], "args": s["args"], "ok": None if s["ok"] is None else bool(s["ok"]),
            "result_bytes": s["result_bytes"], "excerpt": s["result_excerpt"], "decision": s["decision"],
            "decision_reason": s["decision_reason"], "approval_id": s["approval_id"], "actor": s["actor"],
            "duration_ms": s["duration_ms"], "tokens_in": s["tokens_in"], "tokens_out": s["tokens_out"]}


@bp.route("/api/agent/runs/<rid>")
def run_one(rid):
    r = store.run_get(rid)
    if not r:
        return _err(404, "not_found")
    after = request.args.get("after_seq") or "0"
    steps = store.step_list(rid, int(after) if after.isdigit() else 0)
    title = (store.conversation_get(r["conversation_id"]) or {}).get("title")
    return jsonify({"run": _run_full(dict(r, title=title)), "steps": [_step_out(s) for s in steps],
                    "approvals": [approvals.serialize(a) for a in store.approval_list(run_id=rid)]})


@bp.route("/api/agent/runs/<rid>/cancel", methods=["POST"])
def run_cancel(rid):
    from backend.agent import runs
    st = runs.manager().cancel(rid)
    if st is None:
        return _err(404, "not_found")
    return jsonify({"ok": True, "status": st})


@bp.route("/api/agent/audit/export")
@sensitive
def audit_export():
    fmt = "csv" if request.args.get("format") == "csv" else "json"
    body = audit.export(fmt, **_run_filters())
    name = "agent-audit-%s.%s" % (time.strftime("%Y%m%d-%H%M%S"), fmt)
    return Response(body, mimetype="text/csv" if fmt == "csv" else "application/json",
                    headers={"Content-Disposition": "attachment; filename=%s" % name})


@bp.route("/api/agent/admin-log")
@sensitive
def admin_log():
    return jsonify({"items": store.admin_log_list()})


# ── approvals ────────────────────────────────────────────────────────────────

@bp.route("/api/agent/approvals")
def approvals_list():
    status_f = request.args.get("status") or None
    return jsonify({"items": [approvals.serialize(a) for a in store.approval_list(status=status_f)]})


@bp.route("/api/agent/approvals/<aid>/decision", methods=["POST"])
def approval_decision(aid):
    d = _body().get("decision")
    if d not in ("approve", "reject"):
        return _err(400, "bad_decision", "decision must be approve or reject")
    code, payload = approvals.decide(aid, d == "approve", _actor())
    if code == 200:
        # Give the execution a moment, so the answer says "done" or "refused"
        # rather than just "approved" (alpha #30). The card updates live anyway.
        deadline = time.monotonic() + 5
        a = store.approval_get(aid)
        while a["status"] in ("approved", "executing") and time.monotonic() < deadline:
            time.sleep(0.2)
            a = store.approval_get(aid)
        payload["status"] = a["status"]
        payload["approval"] = approvals.serialize(a)
    return jsonify(payload), code


# ── policy ───────────────────────────────────────────────────────────────────

@bp.route("/api/agent/policy")
def policy_get():
    modes = store.host_modes()
    hosts = []
    for h in ports.host_names():
        hosts.append({"host": h, "os": policy.os_family(h), "mode": policy.mode_for(h, modes),
                      "capabilities": sorted(policy.capabilities(h)), "online": ports.host_online(h)})
    try:
        watched = json.loads(state.get("watched_paths") or "{}")
    except ValueError:
        watched = {}
    return jsonify({"controls_enabled": ports.controls_enabled(), "hosts": hosts,
                    "actions": [catalog.public(a) for a in catalog.actions()],
                    "rules": store.action_rules(), "preset": policy.preset(),
                    "effective_rules": policy.effective_rules(ports.host_names()),
                    "protected": store.protected_list(), "watched_paths": watched,
                    "catalogue_version": catalog.version(), "default_mode": policy.default_mode()})


@bp.route("/api/agent/policy/hosts/<host>", methods=["PUT"])
def policy_host(host):
    mode = _body().get("mode")
    if host not in ports.host_names():
        return _err(404, "unknown_host")
    if mode not in policy.MODES:
        return _err(400, "bad_mode", "mode must be off, passive or active")
    store.set_host_mode(host, mode)
    store.admin_log("policy", host, {"mode": mode}, actor=_actor())
    return jsonify({"ok": True})


@bp.route("/api/agent/policy/hosts/<host>/actions/<action>", methods=["PUT"])
def policy_rule(host, action):
    b = _body()
    a = catalog.get(action)
    if host not in ports.host_names():
        return _err(404, "unknown_host")
    if not a or catalog.is_probe(a):
        return _err(400, "bad_action", "Unknown action (read-only checks need no rule).")
    rule = b.get("rule")
    if rule not in policy.RULES:
        return _err(400, "bad_rule", "rule must be deny, ask or allow")
    unlock = bool(b.get("unattended_unlocked"))
    if unlock:
        if a["risk"] != "medium" or a.get("sudo") or a.get("unattended") == "never":
            return _err(400, "cannot_unlock", "Only medium-risk actions without root can run unattended.")
        if rule != "allow":
            return _err(400, "cannot_unlock", "Unlocking only makes sense with Allow.")
        if not b.get("acknowledge"):
            return _err(400, "acknowledge_required",
                        "Unlocking lets automations do this with nobody watching. Confirm to go ahead.")
    store.set_action_rule(host, action, rule, unlock)
    store.admin_log("policy", "%s/%s" % (host, action), {"rule": rule, "unattended_unlocked": unlock},
                    actor=_actor())
    return jsonify({"ok": True})


@bp.route("/api/agent/policy/protected", methods=["PUT"])
def policy_protected():
    b = _body()
    host, kind, name = b.get("host"), b.get("kind"), str(b.get("name") or "")[:200]
    if host not in ports.host_names():
        return _err(404, "unknown_host")
    if kind not in ("container", "service", "model") or not name:
        return _err(400, "bad_target")
    on = bool(b.get("protected", True))
    store.set_protected(host, kind, name, on, (b.get("reason") or "")[:200] or None)
    store.admin_log("protected", "%s/%s/%s" % (host, kind, name), {"protected": on}, actor=_actor())
    return jsonify({"ok": True, "protected": store.protected_list()})


@bp.route("/api/agent/policy/paths", methods=["PUT"])
def policy_paths():
    b = _body()
    host = b.get("host")
    if host not in ports.host_names():
        return _err(404, "unknown_host")
    paths = [str(p).strip() for p in (b.get("paths") or []) if str(p).strip()]
    for p in paths:
        if not _PATH_RE.fullmatch(p) or ".." in p.split("/"):
            return _err(400, "bad_path", "%s isn't an absolute path we can use." % p[:80])
    try:
        cur = json.loads(state.get("watched_paths") or "{}")
    except ValueError:
        cur = {}
    cur[host] = paths[:20]
    state.put({"watched_paths": json.dumps(cur, sort_keys=True)}, actor=_actor())
    return jsonify({"ok": True, "watched_paths": cur})


# ── automations ──────────────────────────────────────────────────────────────

@bp.route("/api/agent/templates")
def templates_list():
    return jsonify({"templates": [templates.public(t) for t in templates.all_templates()]})


def _task_out(t):
    last = store.run_list(task_id=t["id"], limit=1)
    tpl = templates.get(t["template_id"]) if t.get("template_id") else None
    return {"id": t["id"], "name": t["name"], "template_id": t["template_id"],
            "template_version": t["template_version"], "prompt": t["prompt"], "tools": t["tools"],
            "actions": t["actions"], "hosts": t["hosts"], "cron": t["cron"], "tz": t["tz"], "mode": t["mode"],
            "provider_id": t["provider_id"], "model": t["model"], "delivery": t["delivery"], "budget": t["budget"],
            "catch_up": t["catch_up"], "enabled": t["enabled"], "next_fire": t["next_fire"],
            "next_fire_local": _local_iso(t["next_fire"], t["tz"]), "warning": t["warning"],
            "last_run": {"id": last[0]["id"], "status": last[0]["status"], "created_at": last[0]["created_at"]}
            if last else None,
            "update_available": bool(tpl and tpl["version"] > (t["template_version"] or 0)),
            "created_at": t["created_at"], "updated_at": t["updated_at"]}


def _task_fields(b, base=None):
    t = dict(base or {})
    if b.get("template_id") and not base:
        tpl = templates.get(b["template_id"])
        if not tpl:
            return None, {"template_id": "unknown template"}
        t.update(templates.task_from(tpl, b))
    for k in ("name", "prompt", "cron", "mode", "provider_id", "model", "catch_up"):
        if k in b and b[k] is not None:
            t[k] = b[k].strip() if isinstance(b[k], str) else b[k]
    for k in ("hosts", "actions", "tools"):
        if k in b and isinstance(b[k], list):
            t[k] = [str(x) for x in b[k]][:50]
    if "delivery" in b:
        ch = [c for c in ((b.get("delivery") or {}).get("channels") or []) if c in _CHANNELS]
        t["delivery"] = {"channels": ch}
    if "budget" in b and isinstance(b["budget"], dict):
        t["budget"] = {k: int(v) for k, v in b["budget"].items()
                       if k in ("max_steps", "max_seconds", "max_tokens") and isinstance(v, (int, float)) and v > 0}
    t["tz"] = b.get("tz") or t.get("tz") or state.tz()
    if not t.get("tools"):
        t["tools"] = [x.name for x in READ_TOOLS] + [a["id"] for a in catalog.actions() if catalog.is_probe(a)]
    t.setdefault("actions", [])
    t.setdefault("delivery", {"channels": ["inapp"]})
    t.setdefault("budget", {})
    t.setdefault("catch_up", "skip")
    t.setdefault("hosts", [])
    return t, scheduler.validate_task(t)


@bp.route("/api/agent/tasks", methods=["GET", "POST"])
def tasks():
    if request.method == "GET":
        return jsonify({"items": [_task_out(t) for t in store.task_list()]})
    t, errs = _task_fields(_body())
    if errs:
        return _err(400, "invalid", "Please fix the highlighted fields.", fields=errs)
    nxt = cron.next_after(t["cron"], t["tz"], int(time.time()))
    t["next_fire"] = nxt[0] if nxt else None
    tid = store.task_insert(t)
    store.admin_log("task", tid, {"created": t.get("template_id") or "custom", "mode": t["mode"],
                                  "hosts": t["hosts"], "cron": t["cron"]}, actor=_actor())
    return jsonify({"ok": True, "task": _task_out(store.task_get(tid))})


@bp.route("/api/agent/tasks/<tid>", methods=["PATCH", "DELETE"])
def task_one(tid):
    cur = store.task_get(tid)
    if not cur:
        return _err(404, "not_found")
    if request.method == "DELETE":
        store.task_delete(tid)
        store.admin_log("task", tid, {"deleted": True}, actor=_actor())
        return jsonify({"ok": True})
    b = _body()
    t, errs = _task_fields(b, base=cur)
    if errs:
        return _err(400, "invalid", "Please fix the highlighted fields.", fields=errs)
    upd = {k: t[k] for k in ("name", "prompt", "cron", "tz", "mode", "provider_id", "model", "catch_up", "hosts",
                             "actions", "tools", "delivery", "budget") if k in t}
    if "enabled" in b:
        upd["enabled"] = bool(b["enabled"])
    if any(k in b for k in ("cron", "tz", "enabled")):
        nxt = cron.next_after(t["cron"], t["tz"], int(time.time()))
        upd["next_fire"] = nxt[0] if nxt else None
        upd["last_slot_local"] = None
    store.task_update(tid, **upd)
    store.admin_log("task", tid, {"changed": sorted(upd)}, actor=_actor())
    return jsonify({"ok": True, "task": _task_out(store.task_get(tid))})


@bp.route("/api/agent/tasks/<tid>/run", methods=["POST"])
def task_run(tid):
    t = store.task_get(tid)
    if not t:
        return _err(404, "not_found")
    if store.task_active_run(tid):
        return _err(409, "run_active", "This automation is already running.")
    rid = scheduler.run_now(t, dry_run=bool(_body().get("dry_run")))
    return jsonify({"ok": True, "run_id": rid}), 202


@bp.route("/api/agent/tasks/<tid>/fires")
def task_fires(tid):
    if not store.task_get(tid):
        return _err(404, "not_found")
    try:
        limit = max(1, min(200, int(request.args.get("limit", 20))))
    except ValueError:
        limit = 20
    return jsonify({"items": store.fire_list(tid, limit)})


@bp.route("/api/agent/cron/preview", methods=["POST"])
def cron_preview():
    b = _body()
    tz = b.get("tz") or state.tz()
    try:
        out = cron.preview(b.get("cron"), tz, int(time.time()))
        gap = cron.Cron(b.get("cron")).min_interval_s()
    except cron.CronError as e:
        return jsonify({"ok": False, "error": str(e), "field": e.field})
    return jsonify({"ok": True, "tz": tz, "next": [loc for _, loc in out], "min_interval_s": gap})
