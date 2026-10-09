"""Read helpers for cost/rollup tables."""
from backend.db import connection

_TOTAL_W_EXPR = "COALESCE(power,0)+COALESCE(cpu_power,0)+COALESCE(dram_power,0)"


def power_since(ts: int, table: str = "samples_1h", conn=None) -> list:
    """Return (ts, power) rows from a rollup table since ts."""
    c = conn or connection()
    return c.execute(
        f"SELECT ts, power FROM {table} WHERE ts>=? AND power IS NOT NULL ORDER BY ts",
        (ts,)
    ).fetchall()


def heatmap_since(ts: int, conn=None) -> list:
    """Return (ts, total_watts) rows from samples_1h for heatmap computation."""
    c = conn or connection()
    return c.execute(
        f"SELECT ts, {_TOTAL_W_EXPR} FROM samples_1h WHERE ts>=? ORDER BY ts", (ts,)
    ).fetchall()


def bucketed_power(ts: int, bucket_sec: int, table: str = "samples", conn=None) -> list:
    """Return (bucket, sum_power) rows grouped into bucket_sec intervals since ts."""
    c = conn or connection()
    return c.execute(
        f"SELECT (ts/?)*? b, SUM(power) FROM {table} WHERE ts>=? AND power IS NOT NULL GROUP BY b ORDER BY b",
        (bucket_sec, bucket_sec, ts)
    ).fetchall()


def avg_power_since(ts: int, conn=None):
    """Return AVG(power) from samples since ts."""
    c = conn or connection()
    return c.execute("SELECT AVG(power) FROM samples WHERE ts>=?", (ts,)).fetchone()[0]


def sum_power_cnt_since(ts: int, conn=None):
    """Return SUM(power*cnt) from samples_1h since ts."""
    c = conn or connection()
    return c.execute("SELECT SUM(power*cnt) FROM samples_1h WHERE ts>=?", (ts,)).fetchone()[0]


def sum_wsec_since(ts: int, conn=None):
    """Return SUM(wsec) from samples_1h since ts -- already-interval-correct
    watt-seconds, so the caller need not multiply by any INTERVAL at all."""
    c = conn or connection()
    return c.execute("SELECT SUM(wsec) FROM samples_1h WHERE ts>=?", (ts,)).fetchone()[0]


def samples_1h_power_cnt_since(ts: int, conn=None) -> list:
    """Return (ts, power, cnt) from samples_1h since ts where power is not null."""
    c = conn or connection()
    return c.execute(
        "SELECT ts,power,cnt FROM samples_1h WHERE ts>=? AND power IS NOT NULL", (ts,)
    ).fetchall()


def samples_1h_wsec_cnt_since(ts: int, conn=None) -> list:
    """Return (ts, wsec, cnt) from samples_1h since ts where wsec is not null --
    the wsec counterpart of samples_1h_power_cnt_since, used by the dual-tariff
    day/night split so each row prices its own already-interval-correct energy."""
    c = conn or connection()
    return c.execute(
        "SELECT ts,wsec,cnt FROM samples_1h WHERE ts>=? AND wsec IS NOT NULL", (ts,)
    ).fetchall()


def samples_1h_power_cnt_since_ordered(ts: int, conn=None) -> list:
    """Return (ts, power, cnt) from samples_1h since ts ordered by ts."""
    c = conn or connection()
    return c.execute(
        "SELECT ts,power,cnt FROM samples_1h WHERE ts>=? AND power IS NOT NULL ORDER BY ts", (ts,)
    ).fetchall()


def samples_1h_wsec_cnt_since_ordered(ts: int, conn=None) -> list:
    """Return (ts, wsec, cnt) from samples_1h since ts ordered by ts."""
    c = conn or connection()
    return c.execute(
        "SELECT ts,wsec,cnt FROM samples_1h WHERE ts>=? AND wsec IS NOT NULL ORDER BY ts", (ts,)
    ).fetchall()


def samples_1h_bucketed_power(ts: int, bk: int, conn=None) -> list:
    """Return (bucket, sum_power_cnt) bucketed from samples_1h since ts."""
    c = conn or connection()
    return c.execute(
        "SELECT (ts/?)*? b, SUM(power*cnt) FROM samples_1h WHERE ts>=? GROUP BY b ORDER BY b",
        (bk, bk, ts)
    ).fetchall()


def samples_1h_bucketed_wsec(ts: int, bk: int, conn=None) -> list:
    """Return (bucket, sum_wsec) bucketed from samples_1h since ts."""
    c = conn or connection()
    return c.execute(
        "SELECT (ts/?)*? b, SUM(wsec) FROM samples_1h WHERE ts>=? GROUP BY b ORDER BY b",
        (bk, bk, ts)
    ).fetchall()


def min_ts_samples_1h(conn=None):
    """Return the earliest ts in samples_1h, or None."""
    c = conn or connection()
    return c.execute("SELECT MIN(ts) FROM samples_1h").fetchone()[0]


def samples_1h_comp_bucketed(ts: int, bk: int, conn=None) -> list:
    """Return (bucket, avg_power, avg_cpu_power, avg_dram_power) bucketed from samples_1h."""
    c = conn or connection()
    return c.execute(
        "SELECT (ts/?)*? b, AVG(power), AVG(cpu_power), AVG(dram_power) "
        "FROM samples_1h WHERE ts>=? GROUP BY b ORDER BY b",
        (bk, bk, ts)
    ).fetchall()


def samples_1h_full_since(ts: int, conn=None) -> list:
    """Return (ts, power, cpu_power, dram_power, cnt, wsec, cpu_wsec, dram_wsec)
    from samples_1h since ts.

    wsec/cpu_wsec/dram_wsec are already-interval-correct watt-second sums for
    each of the three power columns, matching host_samples_1h's three-component
    shape -- no per-row AVG*cnt*INTERVAL rescale needed for any of them."""
    c = conn or connection()
    return c.execute(
        "SELECT ts,power,cpu_power,dram_power,cnt,wsec,cpu_wsec,dram_wsec "
        "FROM samples_1h WHERE ts>=?", (ts,)
    ).fetchall()


def samples_1h_total_w_since(ts: int, conn=None) -> list:
    """Return (ts, total_w, cnt) from samples_1h since ts using total_w_expr."""
    c = conn or connection()
    return c.execute(
        f"SELECT ts, {_TOTAL_W_EXPR} w, cnt FROM samples_1h WHERE ts>=?", (ts,)
    ).fetchall()


def samples_1h_total_wsec_since(ts: int, conn=None) -> list:
    """Return (ts, wsec) from samples_1h since ts -- already-interval-correct.
    COALESCE guards rows the backfill hasn't reached yet (should not happen in
    practice; apply_schema_migrations backfills on every boot)."""
    c = conn or connection()
    return c.execute(
        "SELECT ts, COALESCE(wsec,0) FROM samples_1h WHERE ts>=?", (ts,)
    ).fetchall()


def power_proc_since(ts: int, conn=None) -> list:
    """Return (ts, kind, name, watts, interval_sec) from power_proc since ts.

    interval_sec lets the caller price each row at the cadence it was actually
    sampled at, instead of one global multiplier applied after the fact."""
    c = conn or connection()
    return c.execute(
        "SELECT ts,kind,name,watts,interval_sec FROM power_proc WHERE ts>=?", (ts,)
    ).fetchall()


def min_ts_power_proc(conn=None):
    """Return the earliest ts in power_proc, or None."""
    c = conn or connection()
    return c.execute("SELECT MIN(ts) FROM power_proc").fetchone()[0]


def power_proc_entity(name: str, ts: int, bk: int, kind: str = None, conn=None) -> list:
    """Return (bucket, wsec_sum, max_watts, avg_watts) from power_proc for entity
    drilldown. wsec_sum = SUM(watts*interval_sec) per bucket -- each row's own
    interval baked in, so the caller derives kWh without a global multiplier.
    max_watts/avg_watts stay plain W for the peak_w field and the chart series."""
    c = conn or connection()
    q = ("SELECT (ts/?)*? b, SUM(watts*interval_sec), MAX(watts), AVG(watts) "
         "FROM power_proc WHERE name=? AND ts>=?")
    args = [bk, bk, name, ts]
    if kind:
        q += " AND kind=?"; args.append(kind)
    q += " GROUP BY b ORDER BY b"
    return c.execute(q, args).fetchall()


def max_vram_for_service(name: str, ts: int, conn=None):
    """Return MAX(mem) from proc for one of the HUB's services since ts.

    Host-scoped: `proc` now holds every host's per-service VRAM, and this feeds
    the hub's own cost attribution — an unscoped MAX would pick up a same-named
    service on a remote (every box running `ollama` shares the name).
    """
    c = conn or connection()
    return c.execute(
        "SELECT MAX(mem) FROM proc WHERE host='local' AND service=? AND ts>=?", (name, ts)
    ).fetchone()[0]


def samples_1h_heatmap(ts: int, conn=None) -> list:
    """Return (ts, total_w, cnt) from samples_1h since ts for heatmap."""
    c = conn or connection()
    return c.execute(
        f"SELECT ts, {_TOTAL_W_EXPR} w, cnt FROM samples_1h WHERE ts>=? ORDER BY ts", (ts,)
    ).fetchall()
