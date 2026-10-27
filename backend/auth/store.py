"""SQL for the login: the admin password row, sessions, and api_keys.scope.

Every function takes the connection it should use (the caller holds app.LOCK),
like the repos under backend/db/repos. `ensure()` creates the tables on first
use and is cheap afterwards; the tables are additive, so an older image that
doesn't know them simply ignores them.
"""
import sqlite3
import time

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_admin(
  id INTEGER PRIMARY KEY CHECK(id=1), pw_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS auth_sessions(
  id_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL,
  created_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
  ip TEXT, ua TEXT);
"""
# (table, column) pairs added to existing tables. `scope` defaults to 'push' so
# every key minted before the login existed keeps doing exactly what it did:
# push experiment runs, nothing else.
COLUMNS = (("api_keys", "scope TEXT NOT NULL DEFAULT 'push'"),
           ("api_keys", "last_ip TEXT"))

# id -> the connection itself. Holding the object (not just its id) means a
# closed handle's id can't be reused by a new connection after a restore.
_ready = {}


def ensure(conn):
    if _ready.get(id(conn)) is conn:
        return
    conn.executescript(AUTH_SCHEMA)
    for table, col in COLUMNS:
        try:
            conn.execute("ALTER TABLE %s ADD COLUMN %s" % (table, col))
        except sqlite3.OperationalError:
            pass
    conn.commit()
    _ready[id(conn)] = conn


def forget(conn):
    """Called after a restore swaps the database file under the same handle id."""
    _ready.pop(id(conn), None)


# ── admin password ───────────────────────────────────────────────────────────

def get_hash(conn):
    ensure(conn)
    row = conn.execute("SELECT pw_hash FROM auth_admin WHERE id=1").fetchone()
    return row[0] if row else None


def set_hash(pw_hash, conn):
    ensure(conn)
    now = int(time.time())
    conn.execute("INSERT INTO auth_admin(id,pw_hash,created_at,updated_at) VALUES(1,?,?,?) "
                 "ON CONFLICT(id) DO UPDATE SET pw_hash=excluded.pw_hash, updated_at=excluded.updated_at",
                 (pw_hash, now, now))
    conn.commit()


def clear_hash(conn):
    ensure(conn)
    conn.execute("DELETE FROM auth_admin")
    conn.commit()


# ── sessions ─────────────────────────────────────────────────────────────────

def insert_session(id_hash, csrf, now, expires_at, ip, ua, conn):
    ensure(conn)
    conn.execute("INSERT INTO auth_sessions(id_hash,csrf,created_at,last_seen_at,expires_at,ip,ua) "
                 "VALUES(?,?,?,?,?,?,?)", (id_hash, csrf, now, now, expires_at, ip, ua))
    conn.commit()


def get_session(id_hash, conn):
    ensure(conn)
    return conn.execute("SELECT id_hash,csrf,created_at,last_seen_at,expires_at,ip,ua "
                        "FROM auth_sessions WHERE id_hash=?", (id_hash,)).fetchone()


def touch_session(id_hash, now, conn):
    conn.execute("UPDATE auth_sessions SET last_seen_at=? WHERE id_hash=?", (now, id_hash))
    conn.commit()


def list_sessions(conn):
    ensure(conn)
    return conn.execute("SELECT id_hash,created_at,last_seen_at,expires_at,ip,ua FROM auth_sessions "
                        "ORDER BY last_seen_at DESC").fetchall()


def delete_session(id_hash, conn):
    ensure(conn)
    cur = conn.execute("DELETE FROM auth_sessions WHERE id_hash=?", (id_hash,))
    conn.commit()
    return cur.rowcount


def delete_sessions_except(keep_hash, conn):
    ensure(conn)
    conn.execute("DELETE FROM auth_sessions WHERE id_hash<>?", (keep_hash or "",))
    conn.commit()


def purge_expired_sessions(now, conn):
    ensure(conn)
    conn.execute("DELETE FROM auth_sessions WHERE expires_at<?", (now,))
    conn.commit()


# ── api_keys scope ───────────────────────────────────────────────────────────

def key_by_hash(key_hash, conn):
    ensure(conn)
    return conn.execute("SELECT id, expires_at, scope FROM api_keys WHERE key_hash=?",
                        (key_hash,)).fetchone()


def touch_key(kid, now, conn, ip=None):
    if ip is None:
        conn.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (now, kid))
    else:
        conn.execute("UPDATE api_keys SET last_used_at=?, last_ip=? WHERE id=?", (now, ip, kid))
    conn.commit()


def set_key_scope(kid, scope, conn):
    ensure(conn)
    conn.execute("UPDATE api_keys SET scope=? WHERE id=?", (scope, kid))
    conn.commit()


def key_scopes(conn):
    """{id: (scope, last_ip)} for every key."""
    ensure(conn)
    return {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, scope, last_ip FROM api_keys")}


def revoke_non_push_keys(keep_ids, conn):
    """Delete every read/admin key not in keep_ids. Returns the number removed."""
    ensure(conn)
    keep = set(keep_ids or ())
    ids = [r[0] for r in conn.execute("SELECT id FROM api_keys WHERE scope<>'push'") if r[0] not in keep]
    for kid in ids:
        conn.execute("DELETE FROM api_keys WHERE id=?", (kid,))
    conn.commit()
    return len(ids)


def reset():
    """Forget which connections were prepared (after a restore reopens the DB)."""
    _ready.clear()
