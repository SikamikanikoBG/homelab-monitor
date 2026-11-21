"""Forgot the dashboard password?

    docker exec homelab-monitor python -m backend.auth.cli reset-password

Clears the admin password and every session, turns the AI agent off and pauses
its automations (the agent may only run behind a login). Works straight on the
SQLite file, so the dashboard keeps running; reload the page afterwards.
"""
import os
import sqlite3
import sys


def reset_password(db_path):
    conn = sqlite3.connect(db_path)
    try:
        done = []
        for sql, what in (("DELETE FROM auth_admin", "password cleared"),
                          ("DELETE FROM auth_sessions", "sessions revoked")):
            try:
                conn.execute(sql)
                done.append(what)
            except sqlite3.OperationalError:
                pass                               # tables don't exist: no login was ever set
        try:
            conn.executemany("INSERT INTO agent_settings(key,value) VALUES(?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             [("enabled", "0"), ("paused", "1")])
            done.append("agent turned off and automations paused")
        except sqlite3.OperationalError:
            pass
        conn.commit()
    finally:
        conn.close()
    return done


def main(argv):
    if len(argv) < 2 or argv[1] != "reset-password":
        print(__doc__.strip())
        return 2
    db = os.environ.get("DB_PATH", "/data/gpu.db")
    done = reset_password(db)
    print("; ".join(done) if done else "No login was set — nothing to reset.")
    print("The dashboard picks this up within a minute (or restart the container).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
