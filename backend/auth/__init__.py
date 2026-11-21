"""backend/auth — API keys (Phase 3.4) and the opt-in dashboard login (#327, #354).

The API-key helpers below are what they always were: `require_api_key` guards
the experiment push routes, `_key_lookup` / `_presented_key` are re-exported by
app.py. The login itself lives in the sibling modules (passwords, store,
sessions, gate) and stays dormant until an admin password is set — with no
password every request is handled exactly as before.
"""
import time
from functools import wraps
from flask import request, jsonify, g

from backend.auth.marks import push_ok


def _key_lookup(presented):
    """Return the key id for a valid, unexpired key, else None."""
    row = key_row(presented)
    return row[0] if row else None


def key_row(presented):
    """(id, scope) for a valid, unexpired key, else None. Bumps last_used_at."""
    import app as _app
    from backend.auth import store
    if not presented:
        return None
    now = int(time.time())
    with _app.LOCK:
        row = store.key_by_hash(_app._hash_key(presented), conn=_app.DB)
        if not row:
            return None
        kid, exp, scope = row
        if exp and exp < now:
            return None
        store.touch_key(kid, now, conn=_app.DB)
    return kid, scope or "push"


def _presented_key():
    auth = request.headers.get("Authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return request.headers.get("X-API-Key", "").strip()


def require_api_key(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        row = key_row(_presented_key())
        # A read-only key is for dashboards and MCP clients; it never pushes.
        if not row or row[1] == "read":
            return jsonify({"ok": False, "error": "missing, invalid, or expired API key"}), 401
        g.api_key_id = row[0]
        return fn(*a, **kw)
    return push_ok(wrapper)
