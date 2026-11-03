"""A small 5-field cron, stdlib only (no croniter).

Fields: minute hour day-of-month month day-of-week, with `*`, lists, ranges,
steps (`*/15`, `1-5/2`), month and day names, and `@hourly @daily @weekly
@monthly @yearly`. Vixie semantics: when both day fields are restricted, a day
matches if either does.

Slots are generated in **local wall time** of the task's timezone and converted
to UTC (DST, EC-19):
- spring forward (02:30 doesn't exist): the slot fires once, at the first
  minute after the gap;
- fall back (03:30 happens twice): a daily/hourly-minute slot fires once, on
  its first occurrence — the second has the same wall time, which is never
  "after" the last slot taken.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
           "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                      "nov", "dec"), 1)}
DAYS = {d: i for i, d in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}
FIELDS = (("minute", 0, 59, {}), ("hour", 0, 23, {}), ("day of month", 1, 31, {}), ("month", 1, 12, MONTHS),
          ("day of week", 0, 7, DAYS))
SEARCH_DAYS = 4 * 366


class CronError(ValueError):
    def __init__(self, field, message):
        self.field = field
        super().__init__("%s: %s" % (field, message))


def _num(tok, names, field, lo, hi):
    t = tok.lower()
    if t in names:
        return names[t]
    if not t.isdigit():
        raise CronError(field, "%r is not a number" % tok)
    v = int(t)
    if not lo <= v <= hi:
        raise CronError(field, "%d is outside %d-%d" % (v, lo, hi))
    return v


def _field(expr, field, lo, hi, names):
    out = set()
    for part in expr.split(","):
        if not part:
            raise CronError(field, "empty list item")
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) == 0:
                raise CronError(field, "bad step %r" % s)
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = _num(x, names, field, lo, hi), _num(y, names, field, lo, hi)
            if a > b:
                raise CronError(field, "range %s goes backwards" % part)
        else:
            a = b = _num(part, names, field, lo, hi)
            if step > 1:
                b = hi
        out.update(range(a, b + 1, step))
    return out


class Cron:
    def __init__(self, expr):
        raw = (expr or "").strip()
        expr = ALIASES.get(raw.lower(), raw)
        parts = expr.split()
        if len(parts) != 5:
            raise CronError("expression", "needs 5 fields (minute hour day month weekday)")
        sets = [_field(p, f, lo, hi, n) for p, (f, lo, hi, n) in zip(parts, FIELDS)]
        self.minutes, self.hours, self.dom, self.months, dow = sets
        self.dow = {d % 7 for d in dow}
        self.dom_star = parts[2] == "*"
        self.dow_star = parts[4] == "*"
        self.expr = expr

    def day_matches(self, d):
        dow = (d.weekday() + 1) % 7
        if self.dom_star and self.dow_star:
            return True
        if self.dom_star:
            return dow in self.dow
        if self.dow_star:
            return d.day in self.dom
        return d.day in self.dom or dow in self.dow

    def min_interval_s(self):
        """Smallest gap between two slots in a day (for the cloud 1 h minimum)."""
        times = sorted(h * 60 + m for h in self.hours for m in self.minutes)
        if len(times) < 2:
            return 86400
        gaps = [b - a for a, b in zip(times, times[1:])] + [times[0] + 1440 - times[-1]]
        return min(gaps) * 60


def zone(name):
    name = name or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        if name.upper() in ("UTC", "ETC/UTC", "Z"):
            return timezone.utc            # a system without tz data still knows UTC
        raise CronError("timezone", "unknown timezone %r" % name)


def _local_slots(c, start_local):
    """Wall-clock slots (naive datetimes) at or after start_local, in order."""
    day = start_local.date()
    for _ in range(SEARCH_DAYS):
        if day.month in c.months and c.day_matches(day):
            for h in sorted(c.hours):
                for m in sorted(c.minutes):
                    t = datetime(day.year, day.month, day.day, h, m)
                    if t >= start_local:
                        yield t
        day += timedelta(days=1)


def _to_utc(naive, tz):
    """Local wall time → UTC epoch. Nonexistent times (spring gap) move to the
    first valid minute after the gap; ambiguous ones take the first occurrence."""
    aware = naive.replace(tzinfo=tz, fold=0)
    back = aware.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
    if back != naive:                   # inside a gap: round-trip lands elsewhere
        t = naive
        while True:
            t += timedelta(minutes=1)
            a = t.replace(tzinfo=tz, fold=0)
            if a.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) == t:
                return int(a.timestamp())
    return int(aware.timestamp())


def next_after(expr, tz_name, after_utc, last_slot_local=None):
    """(utc_epoch, local_wall_iso) of the next slot strictly after `after_utc`
    and strictly after `last_slot_local` (wall time) when given. None if none."""
    c = expr if isinstance(expr, Cron) else Cron(expr)
    tz = zone(tz_name)
    after_local = datetime.fromtimestamp(after_utc, tz).replace(tzinfo=None, second=0, microsecond=0)
    start = after_local + timedelta(minutes=1)
    if last_slot_local:
        last = datetime.fromisoformat(last_slot_local)
        start = max(start, last + timedelta(minutes=1))
    seen_utc = set()
    for slot in _local_slots(c, start):
        utc = _to_utc(slot, tz)
        if utc <= after_utc or utc in seen_utc:
            continue
        seen_utc.add(utc)
        return utc, slot.isoformat(timespec="minutes")
    return None


def preview(expr, tz_name, after_utc, n=5):
    out, t, last = [], after_utc, None
    for _ in range(n):
        nxt = next_after(expr, tz_name, t, last)
        if not nxt:
            break
        out.append(nxt)
        t, last = nxt[0], nxt[1]
    return out
