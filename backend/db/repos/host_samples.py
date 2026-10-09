"""backend/db/repos/host_samples.py — per-host time-series (multi-host slice).

One raw row per successful host poll plus an hourly rollup keyed (ts, host),
mirroring the hub's own samples/samples_1h split (see samples.rollup_now).
The Costs integration reads only the 1h rollup, exactly like the hub path.
"""
from backend.db import connection

_COLS = ("cpu", "ram_used", "ram_total", "load1", "ctemp",
         "gpu_util", "gpu_mem_used", "gpu_mem_total",
         "gpu_power", "cpu_power", "dram_power", "gpu_temp")

_UPSERT_SET = ",\n".join(
    f"{c}=CASE WHEN excluded.{c} IS NOT NULL "
    f"THEN (COALESCE({c},0)*cnt+excluded.{c})/(cnt+1) ELSE {c} END"
    for c in _COLS
)


def record(conn, ts: int, host: str, interval_sec: int, **fields):
    """Insert one raw poll row and fold it into the hourly rollup. conn is
    required — the caller holds app.LOCK. Unknown fields are ignored; missing
    fields store NULL so absent sensors (no GPU, unreadable RAPL) never read
    as zero watts.

    interval_sec is required: the SAMPLE_INTERVAL active for THIS poll, stored
    on the raw row and folded into three watt-second accumulators on the
    rollup (gpu_wsec/cpu_wsec/dram_wsec) so a later interval change can't
    reprice history (see samples.rollup_now for the same pattern on the hub's
    own samples_1h)."""
    vals = tuple(fields.get(c) for c in _COLS)
    conn.execute(
        f"INSERT OR REPLACE INTO host_samples(ts,host,{','.join(_COLS)},interval_sec) "
        f"VALUES(?,?{',?' * len(_COLS)},?)",
        (ts, host) + vals + (interval_sec,)
    )
    h = (ts // 3600) * 3600
    gpu_wsec = (fields.get("gpu_power") or 0) * interval_sec
    cpu_wsec = (fields.get("cpu_power") or 0) * interval_sec
    dram_wsec = (fields.get("dram_power") or 0) * interval_sec
    conn.execute(
        f"INSERT INTO host_samples_1h(ts,host,{','.join(_COLS)},cnt,gpu_wsec,cpu_wsec,dram_wsec) "
        f"VALUES(?,?{',?' * len(_COLS)},1,?,?,?) "
        f"ON CONFLICT(ts,host) DO UPDATE SET\n{_UPSERT_SET},\n"
        f"gpu_wsec=COALESCE(gpu_wsec,0)+excluded.gpu_wsec,\n"
        f"cpu_wsec=COALESCE(cpu_wsec,0)+excluded.cpu_wsec,\n"
        f"dram_wsec=COALESCE(dram_wsec,0)+excluded.dram_wsec,\n"
        f"cnt=cnt+1",
        (h, host) + vals + (gpu_wsec, cpu_wsec, dram_wsec)
    )


def min_ts_1h(host: str, conn=None):
    """Earliest rollup ts for a host, or None."""
    c = conn or connection()
    return c.execute(
        "SELECT MIN(ts) FROM host_samples_1h WHERE host=?", (host,)
    ).fetchone()[0]


def min_ts(host: str, conn=None):
    """Earliest sample for a host across raw and rollup, or None.

    What `range=all` should actually span: raw rows are retention-purged, so
    asking `host_samples` alone would shrink a host's "all" window to the last
    couple of days the moment the purge runs.
    """
    c = conn or connection()
    raw = c.execute("SELECT MIN(ts) FROM host_samples WHERE host=?", (host,)).fetchone()[0]
    roll = min_ts_1h(host, conn=c)
    vals = [v for v in (raw, roll) if v is not None]
    return min(vals) if vals else None


def _use_rollup(host: str, since: int, conn) -> bool:
    """True when the raw ring can no longer answer for `since` but the rollup can.

    Same rule as gpu_samples._use_rollup, and for the same reason: the test is
    "does the rollup hold an EARLIER HOUR than the oldest raw sample" — i.e. has
    retention actually purged raw rows the rollup still remembers. Not simply
    "does the window start before the oldest raw sample", which is also true for
    a host added twenty minutes ago, where raw is the complete and correct
    answer and switching to the rollup would hand back a four-point chart of a
    machine that has fine-grained data for its whole life. (Rollup rows are
    bucketed down to the hour, so the comparison is hour-to-hour.)
    """
    raw = conn.execute("SELECT MIN(ts) FROM host_samples WHERE host=?", (host,)).fetchone()[0]
    roll = min_ts_1h(host, conn=conn)
    if roll is None:
        return False                      # nothing rolled up; raw is all there is
    if raw is None:
        return True                       # raw fully purged (or never written)
    return since < raw and roll < (raw // 3600) * 3600


def vitals_series(host: str, since: int, bucket: int, conn=None) -> list:
    """Bucketed CPU / RAM / load / temperature for one host since `since`.

    Returns (bucket_ts, avg_cpu, avg_ram_used, max_ram_total, avg_load1,
    avg_ctemp) ordered by time — the per-host counterpart of the hub's own
    `D.total` series, so the System tab's chart has one shape to draw whichever
    machine is selected.

    `ram_total` takes MAX rather than AVG: it is a capacity, not a rate, and
    averaging it across a bucket where one poll missed the value would drag the
    denominator of every RAM percentage down with it.
    """
    c = conn or connection()
    table = "host_samples_1h" if _use_rollup(host, since, c) else "host_samples"
    return c.execute(
        "SELECT (ts/?)*? b, AVG(cpu), AVG(ram_used), MAX(ram_total), AVG(load1), AVG(ctemp) "
        f"FROM {table} WHERE host=? AND ts>=? GROUP BY b ORDER BY b",
        (bucket, bucket, host, since)
    ).fetchall()


def comp_bucketed(host: str, ts: int, bk: int, conn=None) -> list:
    """(bucket, avg_gpu_power, avg_cpu_power, avg_dram_power) since ts."""
    c = conn or connection()
    return c.execute(
        "SELECT (ts/?)*? b, AVG(gpu_power), AVG(cpu_power), AVG(dram_power) "
        "FROM host_samples_1h WHERE host=? AND ts>=? GROUP BY b ORDER BY b",
        (bk, bk, host, ts)
    ).fetchall()


def full_since(host: str, ts: int, conn=None) -> list:
    """(ts, gpu_power, cpu_power, dram_power, cnt) since ts."""
    c = conn or connection()
    return c.execute(
        "SELECT ts,gpu_power,cpu_power,dram_power,cnt "
        "FROM host_samples_1h WHERE host=? AND ts>=?",
        (host, ts)
    ).fetchall()


def wsec_full_since(host: str, ts: int, conn=None) -> list:
    """(ts, gpu_wsec, cpu_wsec, dram_wsec, cnt) since ts.

    Same rows as full_since, but the already-interval-correct watt-second sums
    instead of the AVG(power) that needs multiplying by a (possibly stale)
    global interval."""
    c = conn or connection()
    return c.execute(
        "SELECT ts,gpu_wsec,cpu_wsec,dram_wsec,cnt "
        "FROM host_samples_1h WHERE host=? AND ts>=?",
        (host, ts)
    ).fetchall()


def total_w_since(host: str, ts: int, conn=None) -> list:
    """(ts, total_watts, cnt) since ts — GPU + CPU + DRAM pooled."""
    c = conn or connection()
    return c.execute(
        "SELECT ts, COALESCE(gpu_power,0)+COALESCE(cpu_power,0)+COALESCE(dram_power,0) w, cnt "
        "FROM host_samples_1h WHERE host=? AND ts>=?",
        (host, ts)
    ).fetchall()


def total_wsec_since(host: str, ts: int, conn=None) -> list:
    """(ts, total_wsec) since ts — GPU + CPU + DRAM watt-second sums pooled."""
    c = conn or connection()
    return c.execute(
        "SELECT ts, COALESCE(gpu_wsec,0)+COALESCE(cpu_wsec,0)+COALESCE(dram_wsec,0) wsec "
        "FROM host_samples_1h WHERE host=? AND ts>=?",
        (host, ts)
    ).fetchall()


def heatmap(host: str, ts: int, conn=None) -> list:
    """(ts, total_w, cnt) since ts for the busy-hours heatmap, ordered."""
    c = conn or connection()
    return c.execute(
        "SELECT ts, COALESCE(gpu_power,0)+COALESCE(cpu_power,0)+COALESCE(dram_power,0) w, cnt "
        "FROM host_samples_1h WHERE host=? AND ts>=? ORDER BY ts",
        (host, ts)
    ).fetchall()


def rename_host(old: str, new: str, conn=None):
    """Follow a host rename so its power history doesn't split."""
    c = conn or connection()
    c.execute("UPDATE host_samples SET host=? WHERE host=?", (new, old))
    c.execute("UPDATE host_samples_1h SET host=? WHERE host=?", (new, old))
