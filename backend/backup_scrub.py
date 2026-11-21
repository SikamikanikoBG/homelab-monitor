"""Strip secrets from a database snapshot before it leaves the hub.

`/api/backup` streams a copy of the whole SQLite file, and backups travel
(cloud drives, USB sticks, chat attachments). So the copy — never the live
database — has every secret blanked: the alert channels' webhook URLs, tokens
and passwords, AI provider keys, the admin password hash and all sessions.
After a restore those are simply re-entered; everything else (history, hosts,
policy, schedules, the audit) comes back as it was.

Done with secure_delete on, then VACUUM, so the old values don't linger in
free pages of the file.
"""
import sqlite3

# (table, SQL) pairs; tables that don't exist in this database are skipped.
def _statements(setting_secrets):
    marks = ",".join("?" * len(setting_secrets))
    return [
        ("settings", "UPDATE settings SET value='' WHERE key IN (%s)" % marks, tuple(setting_secrets)),
        ("agent_providers", "UPDATE agent_providers SET api_key=NULL", ()),
        ("auth_admin", "DELETE FROM auth_admin", ()),
        ("auth_sessions", "DELETE FROM auth_sessions", ()),
    ]


def scrub(path, setting_secrets):
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA secure_delete=ON")
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        done = []
        for table, sql, args in _statements(sorted(setting_secrets)):
            if table in have:
                conn.execute(sql, args)
                done.append(table)
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    return done
