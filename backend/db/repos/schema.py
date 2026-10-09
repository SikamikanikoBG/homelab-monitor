"""backend/db/repos/schema.py — DB initialization, migrations, and schema helpers.

Moved from app.py (Phase 4.1) so that app.py has zero .execute() calls in its
schema/migration functions.  All .execute() calls here are in backend/db/ and
are excluded from the Phase 4.1 acceptance criterion.
"""
import hashlib
import sqlite3
import time
import uuid


def open_db_connection(path: str):
    """Open a SQLite connection with WAL mode and busy_timeout set."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def apply_schema_migrations(conn, schema_sql, sample_migrations, host_migrations,
                             runs_migrations, uptime_migrations, uptime_check_migrations,
                             models_migrations=(), gpu_sample_migrations=(),
                             proc_migrations=(), column_migrations=(),
                             post_migration_indexes=()):
    """Run the full schema bootstrap + column-addition migrations on *conn*."""
    conn.executescript(schema_sql)
    for col in sample_migrations:
        try:
            conn.execute(f"ALTER TABLE samples ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in gpu_sample_migrations:
        try:
            conn.execute(f"ALTER TABLE gpu_samples ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in proc_migrations:
        try:
            conn.execute(f"ALTER TABLE proc ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in models_migrations:
        try:
            conn.execute(f"ALTER TABLE models ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in host_migrations:
        try:
            conn.execute(f"ALTER TABLE hosts ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in runs_migrations:
        try:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in uptime_migrations:
        try:
            conn.execute(f"ALTER TABLE uptime_results ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in uptime_check_migrations:
        try:
            conn.execute(f"ALTER TABLE uptime_checks ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    # Generic (table, column-definition) additions, for tables that don't have
    # their own dedicated migration list above.
    for table, col in column_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    # Indexes over columns the ALTERs above just added — they can't ride in
    # schema_sql, which executes before any of them exist.
    for stmt in post_migration_indexes:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    # samples_1m / net_samples_1m were write-only (nothing ever read them) and
    # absent from the retention purge, so they grew forever on existing DBs.
    # CREATE TABLE IF NOT EXISTS no longer creates them, but that alone leaves
    # them orphaned on every pre-existing database — drop them explicitly.
    # DROP returns the pages to SQLite's freelist for reuse by later writes;
    # the file itself does not shrink without a VACUUM, which we don't run
    # because it rewrites the whole database under an exclusive lock.
    for stmt in ("DROP INDEX IF EXISTS idx_samples_1m_ts",
                 "DROP INDEX IF EXISTS idx_net_samples_1m_ts",
                 "DROP TABLE IF EXISTS samples_1m",
                 "DROP TABLE IF EXISTS net_samples_1m"):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    # Migrate legacy single-instance api_key setting -> api_keys table.
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='api_key'").fetchone()
        legacy = (row[0] if row else "") or ""
        if legacy:
            h = hashlib.sha256(legacy.encode("utf-8")).hexdigest()
            if not conn.execute("SELECT 1 FROM api_keys WHERE key_hash=?", (h,)).fetchone():
                conn.execute(
                    "INSERT INTO api_keys(id,name,key_hash,prefix,created_at,expires_at,last_used_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, "default (migrated)", h, legacy[:12], int(time.time()), None, None))
            conn.execute("UPDATE settings SET value='' WHERE key='api_key'")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    record_baseline_if_needed(conn)


def backfill_interval_columns(conn, current_interval):
    """One-time default for rows written before interval_sec/wsec existed.

    Pre-existing rows carry no record of the cadence they were sampled at, so
    the accepted best-effort default is the INTERVAL in effect right now, at
    migration time. Idempotent via the `IS NULL` guards: rows already backfilled
    (or written post-migration, which always set these columns) are untouched.
    """
    conn.execute("UPDATE samples SET interval_sec=? WHERE interval_sec IS NULL", (current_interval,))
    conn.execute("UPDATE host_samples SET interval_sec=? WHERE interval_sec IS NULL", (current_interval,))
    conn.execute("UPDATE power_proc SET interval_sec=? WHERE interval_sec IS NULL", (current_interval,))
    conn.execute(
        "UPDATE samples_1h SET wsec=COALESCE(power,0)*cnt*? WHERE wsec IS NULL",
        (current_interval,))
    conn.execute(
        "UPDATE samples_1h SET "
        "cpu_wsec=COALESCE(cpu_power,0)*cnt*?, "
        "dram_wsec=COALESCE(dram_power,0)*cnt*? "
        "WHERE cpu_wsec IS NULL",
        (current_interval, current_interval))
    conn.execute(
        "UPDATE host_samples_1h SET "
        "gpu_wsec=COALESCE(gpu_power,0)*cnt*?, "
        "cpu_wsec=COALESCE(cpu_power,0)*cnt*?, "
        "dram_wsec=COALESCE(dram_power,0)*cnt*? "
        "WHERE gpu_wsec IS NULL",
        (current_interval, current_interval, current_interval))
    conn.commit()


def record_baseline_if_needed(conn):
    """Stamp migration 0001 on any DB that already has the baseline schema applied."""
    try:
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        if "0001" not in applied:
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,?)",
                ("0001", int(time.time()))
            )
            conn.commit()
    except sqlite3.OperationalError:
        pass
