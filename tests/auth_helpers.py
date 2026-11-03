"""Shared helpers for tests that need the optional login switched on.

Every test that sets a password must call `clear_login()` in tearDown: the test
process shares one in-memory database, and a password left behind would put
every later test file behind the login.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402

from backend.auth import sessions, store  # noqa: E402

PASSWORD = "correct horse battery"


def clear_login():
    with app.LOCK:
        store.ensure(app.DB)
        app.DB.execute("DELETE FROM auth_admin")
        app.DB.execute("DELETE FROM auth_sessions")
        app.DB.commit()
    sessions.invalidate()
    from backend.api import auth_api
    auth_api._fails.clear()


def login(client, password=PASSWORD):
    """Set the password if none is set (else log in). Returns the CSRF token."""
    if not sessions.password_set():
        r = client.post("/api/auth/setup", json={"password": password})
    else:
        r = client.post("/api/auth/login", json={"password": password})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["csrf"]


def mint_key(scope="push", days=None):
    """Mint a key straight through the app helper (bypasses the HTTP rule that
    read/admin keys need a password, so tests can build any state)."""
    kid, key = app._create_api_key("test-" + scope, days)
    if scope != "push":
        with app.LOCK:
            store.set_key_scope(kid, scope, app.DB)
    return kid, key
