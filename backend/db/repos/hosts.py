"""backend/db/repos/hosts.py — CRUD helpers for the hosts table (Phase 4.1)."""
import time
from backend.db import connection


def list_all(conn=None) -> list:
    """Return all host rows ordered by added_at."""
    c = conn or connection()
    return c.execute(
        "SELECT name, ssh_target, tags, added_at, last_check_at, last_check_json, "
        "poll_timeout, poll_calibrated_at FROM hosts ORDER BY added_at"
    ).fetchall()


def get(name: str, conn=None):
    """Return one host row (name, ssh_target, tags) or None."""
    c = conn or connection()
    return c.execute(
        "SELECT name, ssh_target, tags FROM hosts WHERE name=?", (name,)
    ).fetchone()


def get_ssh_target(name: str, conn=None):
    """Return ssh_target string for a host, or None."""
    c = conn or connection()
    row = c.execute("SELECT ssh_target FROM hosts WHERE name=?", (name,)).fetchone()
    return row[0] if row else None


def insert(name: str, ssh_target: str, tags: str, added_at: int, conn=None):
    """Insert a new host row. Raises sqlite3.IntegrityError on duplicate name."""
    c = conn or connection()
    c.execute(
        "INSERT INTO hosts(name, ssh_target, tags, added_at) VALUES(?,?,?,?)",
        (name, ssh_target, tags, added_at)
    )
    c.commit()


def rename(old: str, new: str, conn=None, commit=True) -> int:
    """Rename a host; the row keeps its target, tags, poll state and last check.
    Raises sqlite3.IntegrityError when `new` is already taken. Returns rowcount.
    commit=False leaves the caller's transaction open (rename_host commits once)."""
    c = conn or connection()
    cur = c.execute("UPDATE hosts SET name=? WHERE name=?", (new, old))
    if commit:
        c.commit()
    return cur.rowcount


def delete(name: str, conn=None, commit=True) -> int:
    """Delete a host by name. Returns rowcount."""
    c = conn or connection()
    cur = c.execute("DELETE FROM hosts WHERE name=?", (name,))
    if commit:
        c.commit()
    return cur.rowcount


def update(fields_sql: str, params: list, conn=None) -> int:
    """Run a dynamic UPDATE hosts SET <fields_sql> WHERE name=?. Returns rowcount."""
    c = conn or connection()
    cur = c.execute(f"UPDATE hosts SET {fields_sql} WHERE name=?", params)
    c.commit()
    return cur.rowcount


def update_check(name: str, ts: int, result_json: str, conn=None):
    """Persist the last probe result for a host."""
    c = conn or connection()
    c.execute(
        "UPDATE hosts SET last_check_at=?, last_check_json=? WHERE name=?",
        (ts, result_json, name)
    )
    c.commit()


def get_poll_state(name: str, conn=None):
    """Return (poll_timeout, poll_fails) row or None."""
    c = conn or connection()
    return c.execute(
        "SELECT poll_timeout, poll_fails FROM hosts WHERE name=?", (name,)
    ).fetchone()


def save_poll_state(fields_sql: str, params: list, conn=None):
    """Run a dynamic UPDATE hosts SET <fields_sql> WHERE name=? for poll state."""
    c = conn or connection()
    c.execute(f"UPDATE hosts SET {fields_sql} WHERE name=?", params)
    c.commit()
