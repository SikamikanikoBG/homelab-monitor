"""SQLite backup and restore helpers for HomeLab Monitor (issue #86)."""

import os
import sqlite3
import time

SQLITE_MAGIC = b"SQLite format 3\x00"
MAX_BACKUP_BYTES = 512 * 1024 * 1024  # 512 MB upload/download cap

REQUIRED_TABLES = (
    "samples", "proc", "models", "edges", "events", "settings", "hosts",
)


def backup_filename(when=None):
    """Return a download filename like homelab-backup-20260610-153045.db."""
    when = when or time.gmtime()
    return time.strftime("homelab-backup-%Y%m%d-%H%M%S.db", when)


def sql_literal_path(path):
    """Escape a filesystem path for use inside a SQL string literal."""
    return path.replace("'", "''")


def remove_wal_sidecars(db_path):
    """Drop WAL/SHM files left over from the previous database file."""
    for suffix in ("-wal", "-shm"):
        try:
            os.unlink(db_path + suffix)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def validate_backup(path, max_bytes=MAX_BACKUP_BYTES):
    """Return (True, None) if path is a valid HomeLab Monitor SQLite backup."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return False, "Could not read the uploaded file."

    if size < 100:
        return False, "Backup file is empty."
    if size > max_bytes:
        return False, "Backup file exceeds maximum size (%d MB)." % (max_bytes // (1024 * 1024))

    try:
        with open(path, "rb") as fh:
            if fh.read(16) != SQLITE_MAGIC:
                return False, "File is not a valid SQLite database."
    except OSError:
        return False, "Could not read the uploaded file."

    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    except sqlite3.Error as exc:
        return False, "Could not open backup: %s" % exc

    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for name in REQUIRED_TABLES:
            if name not in tables:
                return False, "Not a HomeLab Monitor backup (missing table: %s)." % name
        check = conn.execute("PRAGMA integrity_check").fetchone()
        if not check or check[0] != "ok":
            return False, "Database failed integrity check — file may be corrupt."
    except sqlite3.Error as exc:
        return False, "Could not validate backup: %s" % exc
    finally:
        conn.close()

    return True, None


def vacuum_into(connection, dest_path):
    """Write a consistent snapshot of connection's DB to dest_path."""
    safe = sql_literal_path(dest_path)
    connection.execute("VACUUM INTO '%s'" % safe)
