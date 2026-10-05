"""backend/db/repos/system.py — helpers for events, disk_io_samples, and misc queries (Phase 4.1)."""
from backend.db import connection

_TOTAL_W_EXPR = "COALESCE(power,0)+COALESCE(cpu_power,0)+COALESCE(dram_power,0)"


# ── events table ─────────────────────────────────────────────────────────────

def insert_event(ts: int, service: str, kind: str, detail: str, conn=None):
    """Insert or ignore an event row."""
    c = conn or connection()
    c.execute(
        "INSERT OR IGNORE INTO events VALUES(?,?,?,?)",
        (ts, service, kind, detail)
    )
    c.commit()


def insert_events_batch(event_tuples: list, conn=None):
    """Insert multiple (ts, service, kind, detail) event rows in one transaction."""
    c = conn or connection()
    c.executemany("INSERT OR IGNORE INTO events VALUES(?,?,?,?)", event_tuples)
    c.commit()


def query_events_since(since: int, order_desc: bool = False, limit: int = None, conn=None) -> list:
    """Return events since `since`. Optional desc ordering and limit."""
    c = conn or connection()
    order = "DESC" if order_desc else "ASC"
    if limit is not None:
        return c.execute(
            f"SELECT ts, service, kind, detail FROM events WHERE ts>=? ORDER BY ts {order} LIMIT ?",
            (since, limit)
        ).fetchall()
    return c.execute(
        f"SELECT ts, service, kind, detail FROM events WHERE ts>=? ORDER BY ts {order}",
        (since,)
    ).fetchall()


def query_oom_events_since(since: int, conn=None) -> list:
    """Return OOM events since `since` ordered by ts."""
    c = conn or connection()
    return c.execute(
        "SELECT ts, service, detail FROM events WHERE kind='oom' AND ts>=? ORDER BY ts",
        (since,)
    ).fetchall()


# ── disk_io_samples ───────────────────────────────────────────────────────────

def query_disk_io_for_anomaly(since: int, conn=None):
    """Return (device, read_mb_s, write_mb_s) rows for anomaly detection."""
    c = conn or connection()
    return c.execute(
        "SELECT device, read_mb_s, write_mb_s FROM disk_io_samples "
        "WHERE ts>=? ORDER BY ts", (since,)
    ).fetchall()


# ── samples helpers ───────────────────────────────────────────────────────────

def min_ts_samples(conn=None):
    """Return the earliest ts in samples, or None."""
    c = conn or connection()
    return c.execute("SELECT MIN(ts) FROM samples").fetchone()[0]


def min_ts_net_samples(conn=None):
    """Return the earliest ts in net_samples, or None."""
    c = conn or connection()
    return c.execute("SELECT MIN(ts) FROM net_samples").fetchone()[0]


def count_samples_since(since: int, conn=None) -> int:
    """Return count of samples rows >= since."""
    c = conn or connection()
    return c.execute("SELECT COUNT(*) FROM samples WHERE ts>=?", (since,)).fetchone()[0] or 1


def query_samples_bucketed(bk: int, since: int, conn=None) -> list:
    """Return bucketed aggregate rows from samples since `since`."""
    c = conn or connection()
    return c.execute(
        "SELECT (ts/?)*? b,AVG(util),AVG(mem_used),MAX(mem_used),AVG(power),AVG(temp),"
        "AVG(cpu),AVG(ram_used),AVG(ram_total),AVG(load1),AVG(ctemp) "
        "FROM samples WHERE ts>=? GROUP BY b ORDER BY b",
        (bk, bk, since)
    ).fetchall()


def query_proc_bucketed(bk: int, since: int, conn=None) -> list:
    """Return (bucket, service, avg_mem) from proc since `since`.

    Scoped to host='local': `proc` used to be implicitly the hub's own table and
    now carries every host's per-service VRAM, so an unscoped query would stack
    a remote's services onto the hub's chart.
    """
    c = conn or connection()
    return c.execute(
        "SELECT (ts/?)*? b,service,AVG(mem) FROM proc WHERE host='local' AND ts>=? GROUP BY b,service",
        (bk, bk, since)
    ).fetchall()


def query_disk_io_bucketed(bk: int, since: int, conn=None) -> list:
    """Return bucketed disk_io_samples averages since `since`."""
    c = conn or connection()
    return c.execute(
        "SELECT (ts/?)*? b,device,AVG(read_mb_s),AVG(write_mb_s),AVG(util_pct) "
        "FROM disk_io_samples WHERE ts>=? GROUP BY b,device",
        (bk, bk, since)
    ).fetchall()


def query_proc_summary(since: int, conn=None) -> list:
    """Return (service, max_mem, avg_mem, count_distinct_ts) from proc since
    `since`, for the hub's own services (see query_proc_bucketed on scoping)."""
    c = conn or connection()
    return c.execute(
        "SELECT service,MAX(mem),AVG(mem),COUNT(DISTINCT ts) FROM proc "
        "WHERE host='local' AND ts>=? GROUP BY service",
        (since,)
    ).fetchall()


def query_model_summary(since: int, conn=None) -> list:
    """Return (service, model, max_vram, avg_vram, max_ram_spill) from models since
    `since`. max_ram_spill is the worst spill into system RAM (0 = never spilled;
    NULL-ram rows count as 0 — spill is only known for servers that report it)."""
    c = conn or connection()
    return c.execute(
        "SELECT service,model,MAX(vram),AVG(vram),MAX(COALESCE(ram,0)) "
        "FROM models WHERE ts>=? AND vram IS NOT NULL "
        "GROUP BY service,model",
        (since,)
    ).fetchall()


def query_model_runs(since: int, gap: int, conn=None) -> list:
    """Reconstruct load sessions ("runs") per model from the residency samples:
    a run is a contiguous stretch of rows where the model was loaded, split when
    consecutive samples are more than `gap` seconds apart (the model was unloaded
    in between — e.g. ollama keep-alive expired). Returns
    (service, model, runs, runs_spilled, peak_ram) where runs_spilled counts
    sessions that touched system RAM at any point."""
    c = conn or connection()
    return c.execute(
        "WITH r AS ("
        "  SELECT service, model, ts, COALESCE(ram,0) AS ram,"
        "         CASE WHEN ts - LAG(ts) OVER (PARTITION BY service,model ORDER BY ts) > ?"
        "              THEN 1 ELSE 0 END AS brk"
        "  FROM models WHERE ts>=? AND vram IS NOT NULL),"
        " s AS ("
        "  SELECT service, model, ram,"
        "         SUM(brk) OVER (PARTITION BY service,model ORDER BY ts) AS sess"
        "  FROM r),"
        " g AS ("
        "  SELECT service, model, sess, MAX(ram) AS peak_ram"
        "  FROM s GROUP BY service, model, sess)"
        "SELECT service, model, COUNT(*),"
        "       SUM(CASE WHEN peak_ram>0 THEN 1 ELSE 0 END), MAX(peak_ram) "
        "FROM g GROUP BY service, model",
        (gap, since)
    ).fetchall()


def min_ts_models(conn=None):
    """Return the earliest ts in models, or None."""
    c = conn or connection()
    return c.execute("SELECT MIN(ts) FROM models").fetchone()[0]


def query_model_hof(since: int, interval: int, conn=None) -> list:
    """Hall-of-Fame leaderboard rows from models since `since`: one row per
    (service, model), longest-resident first. `models` carries no interval_sec
    column (unlike samples/power_proc), so loaded time is
    COUNT(DISTINCT ts) * `interval` — the same rows × SAMPLE_INTERVAL convention
    the AI Models tab already uses for runs/used-by. A sampling gap therefore
    undercounts rather than stretches a span, the safer bias for a leaderboard.
    Returns (service, model, loaded_for_sec, peak_vram_mb, avg_vram_mb,
    first_seen, last_seen)."""
    c = conn or connection()
    return c.execute(
        "SELECT service,model,COUNT(DISTINCT ts)*? AS loaded_for_sec,"
        "MAX(vram),AVG(vram),MIN(ts),MAX(ts) "
        "FROM models WHERE ts>=? AND vram IS NOT NULL "
        "GROUP BY service,model ORDER BY loaded_for_sec DESC, model ASC",
        (interval, since)
    ).fetchall()


def query_model_callers(since: int, conn=None) -> list:
    """Attribute callers to *models* by time overlap: a caller↔server connection
    sample counts toward a model when that model was resident on the server at
    the same tick. Approximate by design (a server hosting two models at once
    credits both), but with one model resident at a time — the common case on a
    single card — it answers "which app drove this model". Returns
    (service, model, caller, overlap_samples)."""
    c = conn or connection()
    return c.execute(
        "SELECT m.service, m.model, e.caller, COUNT(*) "
        "FROM edges e JOIN models m ON m.ts=e.ts AND m.service=e.server "
        "WHERE e.ts>=? AND m.vram IS NOT NULL "
        "GROUP BY m.service, m.model, e.caller",
        (since,)
    ).fetchall()


def query_edges_summary(since: int, conn=None) -> list:
    """Return (caller, server, sum_conns, count_ts) from edges since `since`."""
    c = conn or connection()
    return c.execute(
        "SELECT caller,server,SUM(conns),COUNT(DISTINCT ts) FROM edges WHERE ts>=? "
        "GROUP BY caller,server",
        (since,)
    ).fetchall()


def query_events_range(since: int, conn=None) -> list:
    """Return (ts, service, kind, detail) events since `since` ordered by ts."""
    c = conn or connection()
    return c.execute(
        "SELECT ts,service,kind,detail FROM events WHERE ts>=? ORDER BY ts", (since,)
    ).fetchall()


def query_proc_at_time(ts: int, exclude_service: str, conn=None):
    """Return (service, mem) from proc at/before ts, excluding one service.
    Hub-scoped (see query_proc_bucketed)."""
    c = conn or connection()
    return c.execute(
        "SELECT service,mem FROM proc WHERE host='local' AND ts<=? AND service!=? "
        "ORDER BY ts DESC,mem DESC LIMIT 1",
        (ts, exclude_service)
    ).fetchone()


def query_net_samples(since: int, conn=None) -> list:
    """Return (ts, iface, bytes_in, bytes_out) from net_samples since `since`."""
    c = conn or connection()
    return c.execute(
        "SELECT ts,iface,bytes_in,bytes_out FROM net_samples WHERE ts>=? ORDER BY iface,ts",
        (since,)
    ).fetchall()


def min_ts_samples_1h(conn=None):
    """Return the earliest ts in samples_1h, or None."""
    c = conn or connection()
    return c.execute("SELECT MIN(ts) FROM samples_1h").fetchone()[0]


def query_samples_for_cost(ts_from: int, ts_to: int, conn=None) -> list:
    """Return (ts, total_w) from samples in [ts_from, ts_to)."""
    c = conn or connection()
    return c.execute(
        f"SELECT ts, {_TOTAL_W_EXPR} w FROM samples WHERE ts>=? AND ts<?",
        (ts_from, ts_to)
    ).fetchall()


def min_ts_power_proc(conn=None):
    """Return the earliest ts in power_proc, or None."""
    c = conn or connection()
    return c.execute("SELECT MIN(ts) FROM power_proc").fetchone()[0]
