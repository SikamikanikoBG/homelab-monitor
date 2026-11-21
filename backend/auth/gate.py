"""The one `before_request` that turns the optional admin password into a login.

No password set (every install until the owner sets one): the gate returns at
its first line, so requests are handled exactly as they were before it existed.

With a password, every route needs a caller unless it is marked
`@open_access` (health check, metrics, public status page, login). Callers are:

- a **session** cookie (the browser). Mutations also need the session's CSRF
  token in `X-CSRF-Token` and, when the browser sends one, a same-origin
  `Origin`/`Referer`.
- a **bearer key** from Integrations → API keys, by scope: `push` only reaches
  the experiment push routes, `read` only GETs that aren't `@sensitive`,
  `admin` reaches everything (no ambient credential, so no CSRF).
- the **internal** caller: the agent's in-process read tools (a WSGI environ
  key no remote client can set) and the MCP process on loopback with the
  per-container token. Both are read-only and refused on `@sensitive` routes.
"""
import hmac
import os
import urllib.parse

from flask import current_app, g, jsonify, redirect, request

from backend.auth import marks, sessions

INTERNAL_ENV_KEY = "hlm.internal"
_SAFE = ("GET", "HEAD", "OPTIONS")
_LOOPBACK = ("127.0.0.1", "::1", "localhost")


def install(app):
    app.before_request(gate)
    app.after_request(frame_headers)


def frame_headers(resp):
    """With a login on, nothing may frame the dashboard (clickjacking a logged-in
    session). Without one, responses stay exactly as they were (alpha #28)."""
    if sessions.password_set():
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'self'")
    return resp


def internal_token():
    return os.environ.get("HLM_INTERNAL_TOKEN") or ""


def _bearer():
    auth = request.headers.get("Authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return request.headers.get("X-API-Key", "").strip()


def _denied(status, error, message):
    return jsonify({"ok": False, "error": error, "message": message}), status


def _unauthenticated():
    wants_page = (request.method == "GET" and not request.path.startswith("/api/")
                  and "text/html" in (request.headers.get("Accept") or ""))
    if wants_page:
        nxt = request.full_path if request.query_string else request.path
        return redirect("/login?next=" + urllib.parse.quote(nxt, safe="/"))
    return _denied(401, "auth_required", "Please log in to the dashboard.")


def _origin_ok():
    """Same-origin check for cookie-authenticated mutations. Browsers send
    Origin on every cross-site POST; absent both headers we rely on the CSRF
    token alone (some privacy setups strip Referer)."""
    src = request.headers.get("Origin") or request.headers.get("Referer") or ""
    if not src:
        return True
    netloc = urllib.parse.urlsplit(src).netloc.lower()
    if netloc == (request.host or "").lower():
        return True
    trusted = [t.strip().lower() for t in os.environ.get("TRUSTED_ORIGINS", "").split(",") if t.strip()]
    return any(netloc == urllib.parse.urlsplit(t).netloc or netloc == t for t in trusted)


def _read_only(view, principal):
    if request.method not in _SAFE:
        return _denied(403, "read_only", "This token can only read.")
    if marks.is_sensitive(view):
        return _denied(403, "sensitive", "This token can't read this route; log in instead.")
    g.hlm_principal = principal
    return None


def gate():
    if not sessions.password_set():
        return None
    view = current_app.view_functions.get(request.endpoint)
    if request.endpoint is None or request.endpoint == "static" or marks.is_open(view):
        return None

    if request.environ.get(INTERNAL_ENV_KEY):
        return _read_only(view, {"kind": "internal", "id": "agent"})

    presented = _bearer()
    tok = internal_token()
    if presented and tok and hmac.compare_digest(presented, tok):
        if (request.remote_addr or "") not in _LOOPBACK:
            return _denied(401, "auth_required", "The internal token only works from this container.")
        return _read_only(view, {"kind": "internal", "id": "mcp"})

    row = sessions.lookup(request.cookies.get(sessions.COOKIE))
    if row:
        g.hlm_principal = {"kind": "session", "id": row[0][:12], "csrf": row[1], "id_hash": row[0]}
        if request.method in _SAFE:
            return None
        if not _origin_ok():
            return _denied(403, "bad_origin", "This request came from another site.")
        sent = request.headers.get("X-CSRF-Token") or ""
        if not sent or not hmac.compare_digest(sent, row[1]):
            return _denied(403, "csrf", "Missing or stale security token — reload the page.")
        return None

    if presented:
        from backend.auth import key_row, store
        krow = key_row(presented)
        if krow:
            kid, scope = krow
            _record_ip(kid)
            principal = {"kind": "bearer", "id": kid[:12], "scope": scope}
            if scope == "admin":
                g.hlm_principal = principal
                return None
            if scope == "read":
                return _read_only(view, principal)
            if marks.is_push_ok(view):          # 'push': experiment routes only
                g.hlm_principal = principal
                return None
            return _denied(403, "scope", "This key can only push experiment runs.")
    return _unauthenticated()


def _record_ip(kid):
    import time
    import app as _app
    from backend.auth import store
    try:
        with _app.LOCK:
            store.touch_key(kid, int(time.time()), _app.DB, ip=(request.remote_addr or "")[:64])
    except Exception as e:                     # bookkeeping only; never block the request
        print("auth: could not record key ip: %s" % e, flush=True)


def principal():
    """Who is calling, for audit lines: {'kind', 'id'} or a 'local' stand-in."""
    return getattr(g, "hlm_principal", None) or {"kind": "open", "id": "lan"}
