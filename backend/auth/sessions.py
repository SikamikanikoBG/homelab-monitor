"""The admin login's state: is a password set, and who is this session.

`password_set()` is asked on every request, so it is one cached boolean; it is
refreshed whenever the password is set, changed or cleared, and after a
restore. Sessions are random 32-byte tokens kept as a sha256 in the database
(the cookie value itself is never stored), with a 60 s in-memory cache so a busy
dashboard doesn't hit SQLite per request, and `last_seen_at` written at most
every 5 minutes.
"""
import hashlib
import os
import secrets
import threading
import time

from backend.auth import passwords, store

COOKIE = "hlm_session"
LIFETIME = 30 * 86400
_TOUCH_EVERY = 300
_CACHE_TTL = 60

_lock = threading.Lock()
_pw_cache = {"known": False, "set": False, "at": 0.0}
# The reset CLI edits the database from another process; re-read now and then
# so a reset takes effect without a restart.
_PW_TTL = 30
_cache = {}            # id_hash -> (row, cached_at)


def _db():
    import app as _app
    return _app.LOCK, _app.DB


def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def invalidate():
    """Drop every cached answer (password changed, sessions revoked, restore)."""
    with _lock:
        _pw_cache["known"] = False
        _cache.clear()


def password_set():
    with _lock:
        if _pw_cache["known"] and time.time() - _pw_cache["at"] < _PW_TTL:
            return _pw_cache["set"]
    lock, db = _db()
    with lock:
        value = bool(store.get_hash(db))
    with _lock:
        _pw_cache.update(known=True, set=value, at=time.time())
    return value


def seed_from_env():
    """ADMIN_PASSWORD / ADMIN_PASSWORD_FILE set a password at boot, only if none
    exists — a headless install can come up protected, and an env var can never
    silently replace a password changed in the UI."""
    pw = os.environ.get("ADMIN_PASSWORD") or ""
    path = os.environ.get("ADMIN_PASSWORD_FILE") or ""
    if not pw and path:
        try:
            with open(path, encoding="utf-8") as f:
                pw = f.read().strip()
        except OSError as e:
            print("auth: cannot read ADMIN_PASSWORD_FILE (%s)" % e, flush=True)
            return False
    if not pw or password_set():
        return False
    if passwords.check_new_password(pw):
        print("auth: ADMIN_PASSWORD is shorter than %d characters — ignored" % passwords.MIN_LENGTH,
              flush=True)
        return False
    set_password(pw)
    print("auth: admin password set from the environment", flush=True)
    return True


def verify(password):
    lock, db = _db()
    with lock:
        stored = store.get_hash(db)
    return passwords.verify_password(password, stored)


def set_password(password):
    lock, db = _db()
    h = passwords.hash_password(password)
    with lock:
        store.set_hash(h, db)
    invalidate()


def clear_password():
    lock, db = _db()
    with lock:
        store.clear_hash(db)
        store.delete_sessions_except(None, db)
    invalidate()


def create(ip=None, ua=None):
    """Start a session. Returns (cookie_token, csrf)."""
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    now = int(time.time())
    lock, db = _db()
    with lock:
        store.insert_session(_hash(token), csrf, now, now + LIFETIME, (ip or "")[:64], (ua or "")[:200], db)
    return token, csrf


def lookup(token):
    """The session row (id_hash, csrf, created, last_seen, expires, ip, ua) or None."""
    if not token:
        return None
    h = _hash(token)
    now = time.time()
    with _lock:
        hit = _cache.get(h)
        if hit and now - hit[1] < _CACHE_TTL:
            row = hit[0]
            return row if row and row[4] > now else None
    lock, db = _db()
    with lock:
        row = store.get_session(h, db)
        if row and row[4] > now and now - row[3] > _TOUCH_EVERY:
            store.touch_session(h, int(now), db)
            row = row[:3] + (int(now),) + row[4:]
    with _lock:
        _cache[h] = (row, now)
    return row if row and row[4] > now else None


def revoke(token=None, id_hash=None):
    lock, db = _db()
    with lock:
        n = store.delete_session(id_hash or _hash(token or ""), db)
    invalidate()
    return n


def revoke_others(keep_token):
    lock, db = _db()
    with lock:
        store.delete_sessions_except(_hash(keep_token) if keep_token else None, db)
    invalidate()


def id_of(token):
    return _hash(token) if token else None
