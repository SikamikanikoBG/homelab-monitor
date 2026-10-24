"""backend/api/auth_api.py — the optional admin login (#327, #354).

Until a password is set none of this changes anything: /api/auth/status says
`password_set: false` and the gate lets every request through. Setting one (in
Settings → Security, or ADMIN_PASSWORD at boot) turns the login on.
"""
import os
import threading
import time

from flask import Blueprint, jsonify, request, make_response, current_app

from backend.auth import gate, passwords, sessions, store
from backend.auth.marks import open_access, sensitive

bp = Blueprint("auth_api", __name__)

# Per-IP login throttle: after 5 failures each further attempt waits 1, 2, 4 …
# up to 60 s. In memory on purpose — a restart forgiving a few typos is fine,
# and it never touches Prometheus scrapes or the public status page.
_FREE_TRIES = 5
_fails = {}
_fails_lock = threading.Lock()


def _set_cookie(resp, token):
    secure = request.is_secure or os.environ.get("AUTH_COOKIE_SECURE", "") in ("1", "true", "yes")
    resp.set_cookie(sessions.COOKIE, token, max_age=sessions.LIFETIME, httponly=True,
                    samesite="Lax", secure=secure, path="/")
    return resp


def _throttled(ip):
    with _fails_lock:
        n, until = _fails.get(ip, (0, 0))
    wait = until - time.time()
    return wait if wait > 0 else 0


def _record_fail(ip):
    with _fails_lock:
        n, _ = _fails.get(ip, (0, 0))
        n += 1
        delay = 0 if n < _FREE_TRIES else min(60, 2 ** (n - _FREE_TRIES))
        _fails[ip] = (n, time.time() + delay)


def _record_ok(ip):
    with _fails_lock:
        _fails.pop(ip, None)


def _session_hash():
    p = gate.principal()
    return p.get("id_hash") if p.get("kind") == "session" else None


def _agent_enabled():
    try:
        from backend.agent import state
        return state.runtime_enabled()
    except Exception as e:                        # agent package absent or DB not ready
        print("auth: could not read agent state: %s" % e, flush=True)
        return False


@bp.route("/login")
@open_access
def login_page():
    return current_app.send_static_file("login.html")


@bp.route("/api/auth/status")
@open_access
def status():
    """What the login page and the fetch wrapper need. Open, and says nothing
    beyond whether a password exists and whether *this* caller is logged in."""
    if not sessions.password_set():
        return jsonify({"password_set": False, "authenticated": False, "via": None})
    row = sessions.lookup(request.cookies.get(sessions.COOKIE))
    if row:
        return jsonify({"password_set": True, "authenticated": True, "via": "session", "csrf": row[1]})
    return jsonify({"password_set": True, "authenticated": False, "via": None})


@bp.route("/api/auth/check")
def check():
    """For the MCP process: is this bearer valid, and with what scope. The gate
    has already accepted the caller by the time we get here."""
    p = gate.principal()
    if p.get("kind") == "open":
        return jsonify({"ok": True, "scope": "admin", "password_set": False})
    return jsonify({"ok": True, "scope": p.get("scope") or p.get("kind"), "password_set": True})


@bp.route("/api/auth/setup", methods=["POST"])
@open_access
def setup():
    """Set the first password. Same trust as today's open dashboard: whoever can
    reach it on the LAN can set it. Refused once a password exists."""
    if sessions.password_set():
        return jsonify({"ok": False, "error": "already_set",
                        "message": "A password is already set. Log in to change it."}), 409
    body = request.get_json(silent=True) or {}
    pw = body.get("password")
    err = passwords.check_new_password(pw)
    if err:
        return jsonify({"ok": False, "error": "weak_password", "message": err}), 400
    import app as _app
    sessions.set_password(pw)
    # Read/admin keys minted while the dashboard was open aren't trusted to
    # outlive it: they're revoked unless the owner ticked them to keep.
    keep = [k for k in (body.get("keep_keys") or []) if isinstance(k, str)]
    with _app.LOCK:
        removed = store.revoke_non_push_keys(keep, _app.DB)
    token, csrf = sessions.create(request.remote_addr, request.headers.get("User-Agent"))
    print("auth: admin password set from %s" % (request.remote_addr or "?"), flush=True)
    return _set_cookie(jsonify({"ok": True, "csrf": csrf, "revoked_keys": removed}), token)


@bp.route("/api/auth/login", methods=["POST"])
@open_access
def login():
    ip = request.remote_addr or "?"
    if not sessions.password_set():
        return jsonify({"ok": False, "error": "no_password", "message": "No password is set."}), 409
    wait = _throttled(ip)
    if wait:
        return jsonify({"ok": False, "error": "throttled", "retry_after": int(wait) + 1,
                        "message": "Too many attempts — try again in a moment."}), 429
    body = request.get_json(silent=True) or {}
    if not sessions.verify(body.get("password") or ""):
        _record_fail(ip)
        _metric("fail")
        return jsonify({"ok": False, "error": "bad_password", "message": "That password didn't match."}), 401
    _record_ok(ip)
    _metric("ok")
    token, csrf = sessions.create(ip, request.headers.get("User-Agent"))
    return _set_cookie(jsonify({"ok": True, "csrf": csrf}), token)


@bp.route("/api/auth/logout", methods=["POST"])
def logout():
    token = request.cookies.get(sessions.COOKIE)
    if token:
        sessions.revoke(token)
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie(sessions.COOKIE, path="/")
    return resp


@bp.route("/api/auth/password", methods=["POST", "DELETE"])
def password():
    if not sessions.password_set():
        return jsonify({"ok": False, "error": "no_password", "message": "No password is set."}), 409
    body = request.get_json(silent=True) or {}
    if not sessions.verify(body.get("current") or ""):
        return jsonify({"ok": False, "error": "bad_password", "message": "The current password didn't match."}), 403
    if request.method == "DELETE":
        if _agent_enabled():
            return jsonify({"ok": False, "error": "agent_enabled",
                            "message": "Turn the agent off first — it needs the login."}), 409
        sessions.clear_password()
        resp = make_response(jsonify({"ok": True}))
        resp.delete_cookie(sessions.COOKIE, path="/")
        return resp
    err = passwords.check_new_password(body.get("new"))
    if err:
        return jsonify({"ok": False, "error": "weak_password", "message": err}), 400
    sessions.set_password(body["new"])
    sessions.revoke_others(request.cookies.get(sessions.COOKIE))
    return jsonify({"ok": True})


@bp.route("/api/auth/sessions")
@sensitive
def list_sessions():
    import app as _app
    mine = _session_hash()
    with _app.LOCK:
        rows = store.list_sessions(_app.DB)
    return jsonify({"sessions": [
        {"id": h[:16], "created_at": c, "last_seen_at": s, "expires_at": e, "ip": ip, "ua": ua,
         "current": h == mine} for h, c, s, e, ip, ua in rows]})


@bp.route("/api/auth/sessions/<sid>", methods=["DELETE"])
def delete_session(sid):
    import app as _app
    with _app.LOCK:
        rows = store.list_sessions(_app.DB)
    match = [r[0] for r in rows if r[0].startswith(sid) and len(sid) >= 8]
    if len(match) != 1:
        return jsonify({"ok": False, "error": "not_found"}), 404
    sessions.revoke(id_hash=match[0])
    return jsonify({"ok": True})


def _metric(result):
    try:
        from backend.auth import metrics
        metrics.login(result)
    except Exception as e:
        print("auth: metric error: %s" % e, flush=True)
