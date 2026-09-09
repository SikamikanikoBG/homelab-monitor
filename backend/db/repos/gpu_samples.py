"""backend/db/repos/gpu_samples.py — per-card GPU history for the whole fleet.

One raw row per card per poll plus an hourly rollup, keyed (ts, host, idx). The
hub stores its own cards as host='local', so a remote and the hub are the same
shape and the GPU cockpit reads both through one function. That is the point of
this module: the dashboard used to fork into a local renderer with charts and a
remote renderer with a snapshot, and the fork existed only because the storage
did.

Rollup rule: rates (util, power, VRAM) are averaged, but temperature and fan
also keep a MAX, and throttling keeps a duration. An hour that averaged 71 °C
while peaking at 87 °C is an hour with a thermal problem; averaging alone would
smooth away precisely the event worth alerting on.
"""
from backend.db import connection

# Raw per-sample columns, in the order record() writes them, paired with the key
# to read from a card dict. The pairing is explicit because one of them differs:
# the DB column `throttle` stores the numeric bitmask, which the card carries as
# `throttle_mask` — its `throttle` key is the list of human-readable reasons.
_COLS = ("util", "mem_used", "mem_total", "power", "temp", "fan",
         "mem_util", "clk_sm", "clk_mem", "power_limit", "temp_mem", "throttle")
_SRC_KEY = {"throttle": "throttle_mask"}

# Which throttle bits mean "something is wrong" as opposed to "doing what it was
# configured to do". SW/HW thermal and the generic HW slowdown are in; SW_POWER_CAP
# (0x04) is deliberately OUT.
#
# That exclusion is the difference between a useful signal and a permanent red
# light: a box whose cards run at a deliberately lowered power limit sits at its
# cap essentially all the time by design, so counting power-cap as throttling
# would report it as continuously throttled and train the user to ignore the
# indicator. Power-capping is still surfaced — health() derives it from power vs
# power_limit and the cockpit shows it as its own calm state.
# Kept in sync with app._THERMAL_BITS.
_THERMAL_BITS = 0x0000000000000068


def record(conn, ts: int, host: str, cards, interval: int = 10):
    """Store one poll's worth of cards for `host` and fold them into the rollup.

    `cards` is the probe/collector per-card list. conn is required — the caller
    holds app.LOCK. A metric the card didn't report stays NULL rather than 0, so
    "no fan sensor" and "fan stopped" remain distinguishable all the way down to
    storage; AVG() then skips the gap instead of charting a fake zero.

    `interval` is the poll period in seconds, used to turn "this sample was
    throttling" into seconds-per-hour in the rollup.
    """
    if not cards:
        return
    raw, roll = [], []
    for g in cards:
        idx = g.get("idx")
        if idx is None:
            continue
        vals = tuple(g.get(_SRC_KEY.get(c, c)) for c in _COLS)
        raw.append((ts, host, idx) + vals)
        thr = g.get("throttle_mask") or 0
        # Only thermal/power-brake bits count as throttled time. Idle and
        # app-clock bits are normal operation and would otherwise report a
        # perfectly healthy idle card as throttling all night.
        secs = interval if (thr & _THERMAL_BITS) else 0
        fan = g.get("fan")
        tmem = g.get("temp_mem")
        roll.append(((ts // 3600) * 3600, host, idx,
                     g.get("util"), g.get("mem_used"), g.get("mem_total"), g.get("power"),
                     g.get("temp"), g.get("temp"), fan, fan,
                     1 if fan is not None else 0,
                     tmem, tmem, 1 if tmem is not None else 0, secs))
    if not raw:
        return
    conn.executemany(
        f"INSERT INTO gpu_samples(ts,host,idx,{','.join(_COLS)}) "
        f"VALUES(?,?,?{',?' * len(_COLS)})", raw)
    # Averages are recomputed incrementally from the running count, the same way
    # host_samples_1h does it; MAX columns take the greater of stored and new.
    # Two things this UPSERT has to get right, both of them the "absent is not
    # zero" rule surviving into the rollup:
    #
    # * A MAX must stay NULL while nothing has reported. `MAX(COALESCE(x,-273),
    #   COALESCE(excluded.x,-273))` poisons the column to -273 the moment two
    #   polls without the metric land in the same hour — a fabricated reading,
    #   which is exactly what this feature refuses to do everywhere else.
    # * An average must divide by the number of polls that actually REPORTED the
    #   metric, not by the total poll count. Dividing `fan` by `cnt` drags an
    #   intermittently-reported fan toward zero — and a fan reading near zero is
    #   the thing the fan-stall alert fires on.
    conn.executemany(
        "INSERT INTO gpu_samples_1h(ts,host,idx,util,mem_used,mem_total,power,"
        "temp,temp_max,fan,fan_max,fan_cnt,temp_mem,temp_mem_max,temp_mem_cnt,"
        "throttle_secs,cnt) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1) "
        "ON CONFLICT(ts,host,idx) DO UPDATE SET "
        "  util=CASE WHEN excluded.util IS NOT NULL THEN (COALESCE(util,0)*cnt+excluded.util)/(cnt+1) ELSE util END,"
        "  mem_used=CASE WHEN excluded.mem_used IS NOT NULL THEN (COALESCE(mem_used,0)*cnt+excluded.mem_used)/(cnt+1) ELSE mem_used END,"
        "  mem_total=COALESCE(excluded.mem_total, mem_total),"
        "  power=CASE WHEN excluded.power IS NOT NULL THEN (COALESCE(power,0)*cnt+excluded.power)/(cnt+1) ELSE power END,"
        "  temp=CASE WHEN excluded.temp IS NOT NULL THEN (COALESCE(temp,0)*cnt+excluded.temp)/(cnt+1) ELSE temp END,"
        "  temp_max=CASE WHEN temp_max IS NULL THEN excluded.temp_max "
        "                WHEN excluded.temp_max IS NULL THEN temp_max "
        "                ELSE MAX(temp_max, excluded.temp_max) END,"
        # Averaged over fan_cnt — the polls that reported a fan — not over cnt.
        "  fan=CASE WHEN excluded.fan IS NULL THEN fan "
        "           WHEN fan IS NULL THEN excluded.fan "
        "           ELSE (fan*fan_cnt+excluded.fan)/(fan_cnt+1) END,"
        "  fan_max=CASE WHEN fan_max IS NULL THEN excluded.fan_max "
        "               WHEN excluded.fan_max IS NULL THEN fan_max "
        "               ELSE MAX(fan_max, excluded.fan_max) END,"
        "  fan_cnt=fan_cnt+excluded.fan_cnt,"
        # Memory-junction temp: averaged over temp_mem_cnt for the same reason
        # the fan is, and its own MAX kept so an hour that spiked to 104 °C
        # can't average down into a comfortable 82.
        "  temp_mem=CASE WHEN excluded.temp_mem IS NULL THEN temp_mem "
        "                WHEN temp_mem IS NULL THEN excluded.temp_mem "
        "                ELSE (temp_mem*temp_mem_cnt+excluded.temp_mem)/(temp_mem_cnt+1) END,"
        "  temp_mem_max=CASE WHEN temp_mem_max IS NULL THEN excluded.temp_mem_max "
        "                    WHEN excluded.temp_mem_max IS NULL THEN temp_mem_max "
        "                    ELSE MAX(temp_mem_max, excluded.temp_mem_max) END,"
        "  temp_mem_cnt=temp_mem_cnt+excluded.temp_mem_cnt,"
        "  throttle_secs=throttle_secs+excluded.throttle_secs,"
        "  cnt=cnt+1",
        roll)


def cards_for(host: str, conn=None) -> list:
    """The card indexes this host has ever reported, ascending.

    Unions raw and rollup: raw rows are retention-purged, so a card the host
    stopped reporting months ago survives only in the rollup, and dropping it
    would make its history unreachable.
    """
    c = conn or connection()
    return [r[0] for r in c.execute(
        "SELECT idx FROM gpu_samples WHERE host=? "
        "UNION SELECT idx FROM gpu_samples_1h WHERE host=? ORDER BY idx",
        (host, host)).fetchall()]


def series(host: str, since: int, bucket: int, conn=None) -> list:
    """Bucketed per-card series since `since`.

    Returns (bucket_ts, idx, util, mem_used, mem_total, power, temp, temp_max,
    fan, fan_max, mem_util, clk_sm, temp_mem, temp_mem_max, throttle_any)
    ordered by time then card.

    Reads whichever table can actually answer. Raw rows are retention-purged
    (48 h by default), so a 7d/30d/all request answered from `gpu_samples` alone
    would silently show only the last two days and call it a month. Once the
    bucket is an hour or more the hourly rollup has the same resolution anyway,
    so anything at or above that switches to `gpu_samples_1h` — which is exactly
    what that table exists for, and it keeps its own MAX columns so peaks
    survive the downsample.
    """
    c = conn or connection()
    # Use the rollup when raw can't answer for this window. The bucket size is
    # not the trigger — a purged raw ring is — but at hourly buckets and above
    # the rollup has the same resolution anyway.
    if _use_rollup(host, since, c):
        return c.execute(
            "SELECT (ts/?)*? b, idx, AVG(util), AVG(mem_used), MAX(mem_total), AVG(power), "
            "       AVG(temp), MAX(temp_max), AVG(fan), MAX(fan_max), NULL, NULL, "
            "       AVG(temp_mem), MAX(temp_mem_max), "
            "       CASE WHEN SUM(throttle_secs) > 0 THEN ? ELSE 0 END "
            "FROM gpu_samples_1h WHERE host=? AND ts>=? GROUP BY b, idx ORDER BY b, idx",
            (bucket, bucket, _THERMAL_BITS, host, since)).fetchall()
    return c.execute(
        "SELECT (ts/?)*? b, idx, AVG(util), AVG(mem_used), MAX(mem_total), AVG(power), "
        "       AVG(temp), MAX(temp), AVG(fan), MAX(fan), AVG(mem_util), AVG(clk_sm), "
        "       AVG(temp_mem), MAX(temp_mem), "
        "       MAX(COALESCE(throttle,0)) "
        "FROM gpu_samples WHERE host=? AND ts>=? GROUP BY b, idx ORDER BY b, idx",
        (bucket, bucket, host, since)).fetchall()


def health(host: str, since: int, hot_c: int = 84, interval: int = 10, conn=None) -> list:
    """Per-card health over the window, for the card-health table.

    (idx, window_sec, avg_temp, peak_temp, peak_fan, throttled_sec, hot_sec,
     capped_pct, peak_temp_mem) — seconds, not sample counts, so the caller
     doesn't have to know which table answered.

    `peak_temp_mem` is the memory-junction peak, NULL for a card whose driver
    never reported one. It is the range's worst moment, not its average, because
    memory heat is judged by how high it ever got.

    `hot_c` is passed in rather than fixed so the "hot" column counts against
    the same threshold the alerts use, including a per-host override. A table
    that says 0 minutes hot while the notifier is paging about heat would be
    worse than no table.

    Beyond the raw retention window this reads the hourly rollup. Throttled
    seconds stay exact there (the rollup accumulates them as seconds), peaks
    stay exact (it keeps MAX columns), but "hot" becomes hour-granular: an hour
    whose peak crossed the threshold counts as a hot hour. That is an
    over-estimate for a brief spike, and the caller flags the window as
    downsampled so the number is never presented as minute-accurate.
    """
    c = conn or connection()
    if interval <= 0:
        interval = 10
    if _use_rollup(host, since, c):
        rows = c.execute(
            "SELECT idx, SUM(cnt), AVG(temp), MAX(temp_max), MAX(fan_max), "
            "       SUM(throttle_secs), "
            "       SUM(CASE WHEN temp_max >= ? THEN 3600 ELSE 0 END), "
            "       0, MAX(temp_mem_max) "
            "FROM gpu_samples_1h WHERE host=? AND ts>=? GROUP BY idx ORDER BY idx",
            (hot_c, host, since)).fetchall()
        # cnt is a sample count; turn it into the seconds the window covers.
        return [(r[0], (r[1] or 0) * interval, r[2], r[3], r[4],
                 r[5] or 0, r[6] or 0, 0.0, r[8]) for r in rows]
    rows = c.execute(
        "SELECT idx, COUNT(*), AVG(temp), MAX(temp), MAX(fan), "
        "       SUM(CASE WHEN COALESCE(throttle,0) & ? THEN 1 ELSE 0 END), "
        "       SUM(CASE WHEN temp >= ? THEN 1 ELSE 0 END), "
        "       SUM(CASE WHEN power_limit > 0 AND power >= power_limit * 0.98 THEN 1 ELSE 0 END), "
        "       MAX(temp_mem) "
        "FROM gpu_samples WHERE host=? AND ts>=? GROUP BY idx ORDER BY idx",
        (_THERMAL_BITS, hot_c, host, since)).fetchall()
    return [(r[0], (r[1] or 0) * interval, r[2], r[3], r[4],
             (r[5] or 0) * interval, (r[6] or 0) * interval,
             round((r[7] or 0) * 100.0 / r[1], 1) if r[1] else 0.0,
             r[8]) for r in rows]


def _use_rollup(host: str, since: int, conn) -> bool:
    """True when the raw ring can no longer answer for `since` but the rollup can.

    The test is "does the rollup hold an EARLIER HOUR than the oldest raw sample"
    — i.e. has retention actually purged raw rows the rollup still remembers.
    Not simply "does the window start before the oldest raw sample": that is also
    true for a host that only started collecting five minutes ago, where raw is
    the complete and correct answer. (And every rollup row is bucketed down to
    its hour, so its timestamp is always <= the raw sample that produced it —
    which is why the comparison is hour-to-hour.)
    """
    raw = conn.execute("SELECT MIN(ts) FROM gpu_samples WHERE host=?", (host,)).fetchone()[0]
    roll = conn.execute("SELECT MIN(ts) FROM gpu_samples_1h WHERE host=?", (host,)).fetchone()[0]
    if roll is None:
        return False                      # nothing rolled up; raw is all there is
    if raw is None:
        return True                       # raw fully purged (or never written)
    return since < raw and roll < (raw // 3600) * 3600


def throttle_spans(host: str, since: int, conn=None) -> list:
    """Raw (ts, idx) samples where the card reported a thermal/power throttle.

    The caller stitches consecutive samples into spans — it knows the poll
    interval and therefore what "consecutive" means; SQL here would have to
    guess. Bounded by the raw retention window like everything else.
    """
    c = conn or connection()
    return c.execute(
        "SELECT ts, idx, throttle FROM gpu_samples "
        "WHERE host=? AND ts>=? AND COALESCE(throttle,0) & ? ORDER BY ts",
        (host, since, _THERMAL_BITS)).fetchall()


def latest(host: str, conn=None) -> list:
    """The most recent stored sample per card — used when a host has history but
    is currently offline, so the cockpit can show its last known state rather
    than an empty tab."""
    c = conn or connection()
    return c.execute(
        "SELECT g.* FROM gpu_samples g JOIN "
        "  (SELECT idx, MAX(ts) mts FROM gpu_samples WHERE host=? GROUP BY idx) m "
        "  ON g.idx=m.idx AND g.ts=m.mts WHERE g.host=? ORDER BY g.idx",
        (host, host)).fetchall()


def last_seen(host: str, conn=None) -> dict:
    """{card idx: last sample ts} for a host.

    Distinguishes "this card stopped reporting a minute ago" (an incident) from
    "this card was removed from the machine last month" (history). Without it,
    every retired GPU would raise a permanent critical alert.
    """
    c = conn or connection()
    return {r[0]: r[1] for r in c.execute(
        "SELECT idx, MAX(ts) FROM gpu_samples WHERE host=? GROUP BY idx", (host,)).fetchall()}


def min_ts(host: str, conn=None):
    """Earliest sample for a host, or None when it has no history yet.

    Considers the rollup too — it is what "range = all" should actually span
    once raw rows past the retention window have been purged.
    """
    c = conn or connection()
    raw = c.execute("SELECT MIN(ts) FROM gpu_samples WHERE host=?", (host,)).fetchone()[0]
    roll = c.execute("SELECT MIN(ts) FROM gpu_samples_1h WHERE host=?", (host,)).fetchone()[0]
    vals = [v for v in (raw, roll) if v is not None]
    return min(vals) if vals else None


def vram_by_service(host: str, since: int, bucket: int, conn=None) -> list:
    """Bucketed per-service VRAM since `since`: (bucket_ts, service, avg_mem).

    Reads `proc`, which the hub has always written for itself and remotes now
    write alongside it under their own host name.
    """
    c = conn or connection()
    return c.execute(
        "SELECT (ts/?)*? b, service, AVG(mem) FROM proc "
        "WHERE host=? AND ts>=? GROUP BY b, service ORDER BY b",
        (bucket, bucket, host, since)).fetchall()


def service_totals(host: str, since: int, conn=None) -> list:
    """(service, peak_mem, avg_mem, samples_present) since `since`."""
    c = conn or connection()
    return c.execute(
        "SELECT service, MAX(mem), AVG(mem), COUNT(DISTINCT ts) FROM proc "
        "WHERE host=? AND ts>=? GROUP BY service ORDER BY MAX(mem) DESC",
        (host, since)).fetchall()


def distinct_sample_times(host: str, since: int, conn=None) -> int:
    """How many distinct polls `host` recorded since `since` — the denominator
    for "% of time" columns, so a service present in every sample reads 100%
    regardless of how many cards it spanned."""
    c = conn or connection()
    return c.execute("SELECT COUNT(DISTINCT ts) FROM proc WHERE host=? AND ts>=?",
                     (host, since)).fetchone()[0] or 0


def rename_host(old: str, new: str, conn=None):
    """Follow a host rename so its GPU history doesn't split in two."""
    c = conn or connection()
    for tbl in ("gpu_samples", "gpu_samples_1h", "proc"):
        c.execute(f"UPDATE {tbl} SET host=? WHERE host=?", (new, old))
