"""Read/write helpers for the samples and samples_1h tables."""
from backend.db import connection


def latest_n(n: int, conn=None) -> list:
    """Return the last n rows from samples (ts DESC)."""
    c = conn or connection()
    return c.execute(
        "SELECT ts,util,mem_used,mem_total,power,temp FROM samples ORDER BY ts DESC LIMIT ?",
        (n,)
    ).fetchall()


def since(ts: int, table: str = "samples", conn=None) -> list:
    """Return all rows from `table` where ts >= ts, ordered ascending."""
    c = conn or connection()
    return c.execute(
        f"SELECT * FROM {table} WHERE ts>=? ORDER BY ts", (ts,)
    ).fetchall()


def insert(ts, util, mem_used, mem_total, power, temp, interval_sec, conn=None):
    """Insert one sample row. interval_sec: SAMPLE_INTERVAL active at write time,
    stored so a later interval change can't reprice this row's historical energy."""
    c = conn or connection()
    c.execute(
        "INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp,interval_sec) VALUES(?,?,?,?,?,?,?)",
        (ts, util, mem_used, mem_total, power, temp, interval_sec)
    )
    c.commit()


def min_ts(conn=None):
    """Return the earliest ts in samples, or None."""
    c = conn or connection()
    return c.execute("SELECT MIN(ts) FROM samples").fetchone()[0]


def rollup_now(conn, ts, util, mem_used, mem_total, power, temp, interval_sec,
               cpu=None, ram_used=None, ram_total=None, load1=None, ctemp=None,
               cpu_power=None, dram_power=None):
    """Upsert the current hour rollup bucket for samples.
    conn is required (called from within app LOCK).

    interval_sec is required: every caller must pass the SAMPLE_INTERVAL that
    was actually active for THIS sample, so wsec/cpu_wsec/dram_wsec (running
    watt-second SUMs) accumulate the true energy contributed by each row
    regardless of any later interval change -- unlike the AVG columns below,
    which stay correct only when re-multiplied by whatever interval is current
    at READ time."""
    h = (ts // 3600) * 3600
    for bucket, tbl in ((h, "samples_1h"),):
        conn.execute(f"""
            INSERT INTO {tbl}(ts,util,mem_used,mem_total,power,temp,cnt,
                cpu,ram_used,ram_total,load1,ctemp,cpu_power,dram_power,wsec,cpu_wsec,dram_wsec)
            VALUES(?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ts) DO UPDATE SET
              util=CASE WHEN excluded.util IS NOT NULL THEN (COALESCE(util,0)*cnt+excluded.util)/(cnt+1) ELSE util END,
              mem_used=CASE WHEN excluded.mem_used IS NOT NULL THEN (COALESCE(mem_used,0)*cnt+excluded.mem_used)/(cnt+1) ELSE mem_used END,
              mem_total=CASE WHEN excluded.mem_total IS NOT NULL THEN (COALESCE(mem_total,0)*cnt+excluded.mem_total)/(cnt+1) ELSE mem_total END,
              power=CASE WHEN excluded.power IS NOT NULL THEN (COALESCE(power,0)*cnt+excluded.power)/(cnt+1) ELSE power END,
              temp=CASE WHEN excluded.temp IS NOT NULL THEN (COALESCE(temp,0)*cnt+excluded.temp)/(cnt+1) ELSE temp END,
              cpu=CASE WHEN excluded.cpu IS NOT NULL THEN (COALESCE(cpu,0)*cnt+excluded.cpu)/(cnt+1) ELSE cpu END,
              ram_used=CASE WHEN excluded.ram_used IS NOT NULL THEN (COALESCE(ram_used,0)*cnt+excluded.ram_used)/(cnt+1) ELSE ram_used END,
              ram_total=CASE WHEN excluded.ram_total IS NOT NULL THEN (COALESCE(ram_total,0)*cnt+excluded.ram_total)/(cnt+1) ELSE ram_total END,
              load1=CASE WHEN excluded.load1 IS NOT NULL THEN (COALESCE(load1,0)*cnt+excluded.load1)/(cnt+1) ELSE load1 END,
              ctemp=CASE WHEN excluded.ctemp IS NOT NULL THEN (COALESCE(ctemp,0)*cnt+excluded.ctemp)/(cnt+1) ELSE ctemp END,
              cpu_power=CASE WHEN excluded.cpu_power IS NOT NULL THEN (COALESCE(cpu_power,0)*cnt+excluded.cpu_power)/(cnt+1) ELSE cpu_power END,
              dram_power=CASE WHEN excluded.dram_power IS NOT NULL THEN (COALESCE(dram_power,0)*cnt+excluded.dram_power)/(cnt+1) ELSE dram_power END,
              wsec=COALESCE(wsec,0)+excluded.wsec,
              cpu_wsec=COALESCE(cpu_wsec,0)+excluded.cpu_wsec,
              dram_wsec=COALESCE(dram_wsec,0)+excluded.dram_wsec,
              cnt=cnt+1
        """, (bucket, util, mem_used, mem_total, power, temp,
              cpu, ram_used, ram_total, load1, ctemp, cpu_power, dram_power,
              (power or 0) * interval_sec,
              (cpu_power or 0) * interval_sec,
              (dram_power or 0) * interval_sec))


def rollup_net_now(conn, ts, net_rows):
    """Upsert the current hour rollup bucket for net_samples.
    conn is required (called from within app LOCK)."""
    if not net_rows:
        return
    h = (ts // 3600) * 3600
    total_in  = sum(r[2] or 0 for r in net_rows)
    total_out = sum(r[3] or 0 for r in net_rows)
    for bucket, tbl in ((h, "net_samples_1h"),):
        conn.execute(f"""
            INSERT INTO {tbl}(ts,bytes_in,bytes_out,cnt)
            VALUES(?,?,?,1)
            ON CONFLICT(ts) DO UPDATE SET
              bytes_in=(bytes_in*cnt+excluded.bytes_in)/(cnt+1),
              bytes_out=(bytes_out*cnt+excluded.bytes_out)/(cnt+1),
              cnt=cnt+1
        """, (bucket, total_in, total_out))


def sessions_since(since: int, conn=None) -> list:
    """Return (ts, util, power, mem_used, interval_sec) for GPU session computation
    since `since`. interval_sec lets the caller price each sample at the cadence
    it was actually collected at, not the cadence active right now."""
    c = conn or connection()
    return c.execute(
        "SELECT ts,util,power,mem_used,interval_sec FROM samples WHERE ts>=? ORDER BY ts", (since,)
    ).fetchall()
