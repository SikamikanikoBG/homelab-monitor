"""backend/api/agent_api.py — /api/agent/* (#354).

Unless AGENT_ENABLED=1, every route here is a 404 — the blueprint is
registered, but answers nothing. With the env switch on, `status` explains what
is still missing (a password, the owner's toggle) and everything else needs an
admin password; running anything needs the agent switched on.
"""
from flask import Blueprint, abort, jsonify, request

from backend.agent import boot, ports, state, store

bp = Blueprint("agent_api", __name__)

# Routes that work before the owner has switched the agent on: reading the
# setup state, turning it on, and the settings it needs first (models & keys,
# policy). Everything that starts or continues a run needs it on.
_SETUP_OK = {"agent_api.status", "agent_api.enable"}


@bp.before_request
def _guard():
    if not state.env_allowed():
        abort(404)
    if request.endpoint == "agent_api.status":
        return None
    if not state.password_set():
        return jsonify({"ok": False, "error": "password_required",
                        "message": "Set a dashboard password first (Settings → Security)."}), 403
    return None


@bp.route("/api/agent/status")
def status():
    pw = state.password_set()
    out = {"allowed": True, "version": ports.version(), "password_set": pw, "enabled": False, "paused": False,
           "controls_enabled": ports.controls_enabled(), "pending_approvals": 0, "running": 0,
           "default": {"provider_id": None, "model": None}}
    if pw:
        s = state.get_all()
        out.update(enabled=state.runtime_enabled(), paused=s["paused"] == "1",
                   default={"provider_id": s["default_provider_id"] or None,
                            "model": s["default_model"] or None})
        out.update(store.counts())
    return jsonify(out)


@bp.route("/api/agent/enable", methods=["POST"])
def enable():
    body = request.get_json(silent=True) or {}
    on = bool(body.get("enabled"))
    state.put({"enabled": "1" if on else "0"}, actor=_actor())
    if on:
        boot.start_if_enabled()
    else:
        boot.on_disabled()
    return jsonify({"ok": True, "enabled": state.runtime_enabled()})


def _actor():
    from backend.auth import gate
    p = gate.principal()
    return "%s:%s" % (p.get("kind"), p.get("id"))
