"""backend/api/costs.py — costs routes (Phase 3.4)."""
from flask import Blueprint, request, jsonify, Response, send_file, send_from_directory, after_this_request, g, abort
import time

from backend.db.repos import costs as costs_repo

bp = Blueprint('costs', __name__)


@bp.route("/api/cost")
def api_cost():
    import app as _app
    """Power → kWh → money (#25), now tariff-aware. Integrates the GPU `power`
    samples we already collect. Energy comes from samples_1h.wsec, a watt-second
    accumulator each rollup upsert grows using the interval that was ACTUALLY
    active when that sample was written (backend/db/repos/samples.py:rollup_now),
    so energy(kWh) = SUM(wsec) / 3_600_000 stays correct however many times
    SAMPLE_INTERVAL changes afterward -- no per-row multiply by a global here.

    Single mode (default): cost = energy * kwh_price — byte-for-byte the original
    behaviour. Dual mode: each sample is billed at the night price inside the
    (possibly midnight-wrapping) night window and the day price otherwise, split
    per window. A blank night price silently degrades to single, so a user who
    doesn't know their rates keeps the simple average. The card stays hidden until
    a day price is set (`enabled`)."""
    s = _app.get_settings()
    def fnum(key):
        v = (s.get(key) or "").strip()
        if v == "":
            return None
        try:
            return float(v)
        except ValueError:
            return None

    day_price   = fnum("kwh_price") or 0.0
    night_price = fnum("kwh_price_night")
    mode = "dual" if (s.get("tariff_mode") == "dual" and night_price is not None
                      and day_price > 0) else "single"
    currency = s.get("currency") or "$"
    is_night = _app._make_is_night(s.get("night_start", "22:00"), s.get("night_end", "06:00"))

    rng = request.args.get("range", "7d")
    span = _app.RANGES.get(rng, 604800)
    now = int(time.time())

    with _app.LOCK:
        def avg_w(since):
            return round(costs_repo.avg_power_since(since, conn=_app.DB) or 0)
        def total_kwh(since):
            tot = costs_repo.sum_wsec_since(since, conn=_app.DB) or 0
            return tot / 3_600_000.0
        def split_kwh(since):
            """One pass over (ts,wsec,cnt) >= since -> (day_kwh, night_kwh)."""
            day_wsec = night_wsec = 0.0
            for ts, wsec, c in costs_repo.samples_1h_wsec_cnt_since(since, conn=_app.DB):
                if is_night(ts):
                    night_wsec += (wsec or 0)
                else:
                    day_wsec += (wsec or 0)
            return day_wsec / 3_600_000.0, night_wsec / 3_600_000.0

        lt = time.localtime(now)
        midnight = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        wins = {"today": midnight, "d7": now - 604800, "d30": now - 2592000}
        kwh, cost, split = {}, {}, {}
        for w, since in wins.items():
            if mode == "dual":
                dk, nk = split_kwh(since)
            else:
                dk, nk = total_kwh(since), 0.0       # single: one SUM, no per-row loop
            dc, nc = dk * day_price, nk * (night_price or 0.0)
            kwh[w]  = round(dk + nk, 3)
            cost[w] = round(dc + nc, 2)
            split[w] = {"day_kwh": round(dk, 3), "night_kwh": round(nk, 3),
                        "day_cost": round(dc, 2), "night_cost": round(nc, 2)}

        # Cumulative-cost series across the selected range (mirrors api_data buckets).
        since = (costs_repo.min_ts_samples_1h(conn=_app.DB) or now) if span is None else now - span
        bk = max(_app.INTERVAL, round(max(1, now - since) / _app.MAX_POINTS))
        labels, cost_cum, running = [], [], 0.0
        if mode == "dual":                            # stream + classify per bucket (one pass)
            acc = {}
            for ts, wsec, c in costs_repo.samples_1h_wsec_cnt_since_ordered(since, conn=_app.DB):
                b = (ts // bk) * bk
                price = night_price if is_night(ts) else day_price
                acc[b] = acc.get(b, 0.0) + (wsec or 0) / 3_600_000.0 * price
            for b in sorted(acc):
                running += acc[b]
                labels.append(int(b)); cost_cum.append(round(running, 4))
        else:                                         # single: cheap SQL-bucketed path
            rows = costs_repo.samples_1h_bucketed_wsec(since, bk, conn=_app.DB)
            for b, wsec in rows:
                running += (wsec or 0) / 3_600_000.0 * day_price
                labels.append(int(b)); cost_cum.append(round(running, 4))

    return jsonify({
        "enabled": day_price > 0, "kwh_price": day_price, "currency": currency,
        "range": rng, "bucket_sec": bk,
        "current_w": round(_app.LATEST.get("power") or 0),
        "avg_24h_w": avg_w(now - 86400), "avg_7d_w": avg_w(now - 604800),
        "kwh": kwh, "cost": cost, "split": split,
        "tariff": {"mode": mode, "price_day": day_price, "price_night": night_price,
                   "night_start": s.get("night_start", "22:00"),
                   "night_end": s.get("night_end", "06:00")},
        "series": {"labels": labels, "cost_cum": cost_cum},
    })


def _host_machine(_app, ctx, host, since, now):
    """One remote host's machine dict over [since, now]: energy per component,
    tariff-aware cost windows, live wattage from the poller snapshot, and which
    sources are actually measured. No bucketed series — the single-host route
    adds those when it needs a chart. Caller must NOT hold LOCK.

    Energy comes from host_samples_1h's gpu_wsec/cpu_wsec/dram_wsec -- watt-second
    accumulators baked in at write time with THAT poll's own interval (see
    host_samples.record) -- so no interval multiplier is needed here at all."""
    from backend.db.repos import host_samples as hs_repo
    comp_kwh = {"gpu": 0.0, "cpu": 0.0, "dram": 0.0}
    have = {"gpu": False, "cpu": False, "dram": False}
    cost_range = 0.0
    with _app.LOCK:
        for ts, p, cp, dp, cnt_ in hs_repo.full_since(host, since, conn=_app.DB):
            if p is not None:  have["gpu"] = True
            if cp is not None: have["cpu"] = True
            if dp is not None: have["dram"] = True
        for ts, gws, cws, dws, cnt_ in hs_repo.wsec_full_since(host, since, conn=_app.DB):
            price = _app._price_at(ctx, ts)
            comp_kwh["gpu"] += (gws or 0) / 3_600_000.0
            comp_kwh["cpu"] += (cws or 0) / 3_600_000.0
            comp_kwh["dram"] += (dws or 0) / 3_600_000.0
            cost_range += ((gws or 0) + (cws or 0) + (dws or 0)) / 3_600_000.0 * price
        def win_cost(start):
            tot = 0.0
            for ts, wsec in hs_repo.total_wsec_since(host, start, conn=_app.DB):
                tot += (wsec or 0) / 3_600_000.0 * _app._price_at(ctx, ts)
            return round(tot, 2)
        lt = time.localtime(now)
        midnight = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        cost_win = {"today": win_cost(midnight), "d7": win_cost(now - 604800), "d30": win_cost(now - 2592000)}
    # Live wattage from the poller's latest snapshot (same source the GPU tab uses).
    with _app.HOST_DATA_LOCK:
        entry = _app.HOST_DATA.get(host) or {}
    hostd = ((entry.get("data") or {}).get("host") or {})
    now_gpu = round((hostd.get("gpu") or {}).get("power") or 0) if hostd.get("gpu") else None
    now_cpu = round(hostd.get("cpu_power")) if hostd.get("cpu_power") is not None else None
    now_dram = round(hostd.get("dram_power")) if hostd.get("dram_power") is not None else None
    measured = [k for k in ("gpu", "cpu", "dram") if have[k]]
    machine = {"name": host,
               "now_w": {"gpu": now_gpu, "cpu": now_cpu, "dram": now_dram,
                         "total": (now_gpu or 0) + (now_cpu or 0) + (now_dram or 0)},
               "energy_kwh": {k: round(v, 3) for k, v in comp_kwh.items() if have[k]},
               "cost": cost_win, "cost_range": round(cost_range, 2),
               "measured": measured, "estimated": []}
    machine["energy_kwh"]["total"] = round(sum(machine["energy_kwh"].values()), 3)
    return machine


def _api_costs_host(_app, host):
    """Per-host flavour of /api/costs — same response shape, integrated over
    host_samples_1h (written by the host poller) instead of the hub's rollups.
    What's measurable on a remote is whatever its probe ships: GPU power via
    nvidia-smi, CPU/DRAM via RAPL when the SSH user can read the counters. No
    per-entity breakdown yet — that needs remote per-process attribution, so
    `breakdown` is [] and the UI says so instead of guessing."""
    from backend.db.repos import host_samples as hs_repo
    ctx = _app._cost_ctx()
    rng = request.args.get("range", "7d")
    span = _app.RANGES.get(rng, 604800)
    now = int(time.time())
    with _app.LOCK:
        since = (hs_repo.min_ts_1h(host, conn=_app.DB) or now) if span is None else now - span
        bk = max(_app.INTERVAL, round(max(1, now - since) / _app.MAX_POINTS))
        comp = hs_repo.comp_bucketed(host, since, bk, conn=_app.DB)
    machine = _host_machine(_app, ctx, host, since, now)
    have_gpu, have_cpu, have_dram = ("gpu" in machine["measured"], "cpu" in machine["measured"],
                                    "dram" in machine["measured"])
    labels = [int(r[0]) for r in comp]
    series = {"labels": labels}
    if have_gpu:  series["gpu"]  = [round(r[1] or 0) for r in comp]
    if have_cpu:  series["cpu"]  = [round(r[2] or 0) for r in comp]
    if have_dram: series["dram"] = [round(r[3] or 0) for r in comp]
    return jsonify({
        "enabled": ctx["day"] > 0, "range": rng, "bucket_sec": bk, "currency": ctx["currency"],
        "host": host, "rapl_available": have_cpu,
        "tariff": {"mode": ctx["mode"], "price_day": ctx["day"], "price_night": ctx["night"],
                   "night_start": ctx["night_start"], "night_end": ctx["night_end"]},
        "machines": [machine], "components": series, "breakdown": [],
    })


@bp.route("/api/costs")
def api_costs():
    import app as _app
    """Richer power+cost view for the Costs page: per-machine totals, a stacked
    component breakdown (GPU measured, CPU/DRAM measured via RAPL, optional operator
    'other' baseline) and a ranked per-process/service/model breakdown — all over a
    selectable range and tariff-aware. /api/cost (GPU-only) is left untouched.
    With ?host=<name> the same shape is served for a registered remote,
    integrated over its own host_samples history. With ?host=all the hub's
    machines list additionally carries one entry per registered host that has
    measurable power — the fleet burn view; components/breakdown stay the
    hub's own."""
    host = (request.args.get("host") or "").strip()
    if host and host not in ("local", "all"):
        return _api_costs_host(_app, host)
    ctx = _app._cost_ctx()
    cur = ctx["currency"]
    rng = request.args.get("range", "7d")
    span = _app.RANGES.get(rng, 604800)
    now = int(time.time())
    with _app.LOCK:
        since = (costs_repo.min_ts_samples_1h(conn=_app.DB) or now) if span is None else now - span
        bk = max(_app.INTERVAL, round(max(1, now - since) / _app.MAX_POINTS))
        comp = costs_repo.samples_1h_comp_bucketed(since, bk, conn=_app.DB)
        # component energy + cost over the range (tariff-aware, one streaming pass).
        # wsec/cpu_wsec/dram_wsec are each column's own accumulator -- already
        # interval-correct -- so all three components are history-proof here, no
        # AVG*cnt*INTERVAL rescale needed for any of them (matches host_samples_1h's
        # three-component shape).
        comp_kwh = {"gpu": 0.0, "cpu": 0.0, "dram": 0.0}
        cost_range = 0.0
        for ts, p, cp, dp, cnt_, wsec, cpu_wsec, dram_wsec in costs_repo.samples_1h_full_since(since, conn=_app.DB):
            price = _app._price_at(ctx, ts)
            gpu_kwh = (wsec or 0) / 3_600_000.0
            cpu_kwh = (cpu_wsec or 0) / 3_600_000.0
            dram_kwh = (dram_wsec or 0) / 3_600_000.0
            comp_kwh["gpu"] += gpu_kwh
            comp_kwh["cpu"] += cpu_kwh
            comp_kwh["dram"] += dram_kwh
            cost_range += (gpu_kwh + cpu_kwh + dram_kwh) * price
        # today/d7/d30 total-cost windows (machine total watts, tariff-aware).
        # wsec pools the WHOLE machine's draw (GPU+CPU+DRAM, see rollup_now), so
        # this window total is fully history-proof, unlike comp_kwh's cpu/dram legs.
        def win_cost(start):
            tot = 0.0
            for ts, wsec in costs_repo.samples_1h_total_wsec_since(start, conn=_app.DB):
                tot += (wsec or 0) / 3_600_000.0 * _app._price_at(ctx, ts)
            return round(tot, 2)
        lt = time.localtime(now)
        midnight = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        cost_win = {"today": win_cost(midnight), "d7": win_cost(now - 604800), "d30": win_cost(now - 2592000)}
        # ranked per-entity breakdown from power_proc (tariff-aware day/night split).
        # Each row now carries its OWN interval_sec, so accumulate wsec directly
        # instead of raw watts needing a later global multiply.
        acc = {}
        for ts, kind, name, watts, interval_sec in costs_repo.power_proc_since(since, conn=_app.DB):
            a = acc.setdefault((kind, name), [0.0, 0.0])
            wsec = watts * (interval_sec or _app.INTERVAL)
            if ctx["mode"] == "dual" and ctx["is_night"](ts):
                a[1] += wsec
            else:
                a[0] += wsec
    hours = max(1e-9, (now - since) / 3600.0)
    idle_w = ctx["idle_w"]
    labels = [int(r[0]) for r in comp]
    series = {"labels": labels,
              "gpu":  [round(r[1] or 0) for r in comp],
              "cpu":  [round(r[2] or 0) if r[2] is not None else 0 for r in comp],
              "dram": [round(r[3] or 0) if r[3] is not None else 0 for r in comp]}
    have_cpu = any(r[2] is not None for r in comp)
    have_dram = any(r[3] is not None for r in comp)
    if not have_cpu: series.pop("cpu")
    if not have_dram: series.pop("dram")
    if idle_w:
        series["other"] = [round(idle_w)] * len(labels)
    breakdown = []
    for (kind, name), (day_wsec, night_wsec) in acc.items():
        day_kwh, night_kwh = day_wsec / 3_600_000.0, night_wsec / 3_600_000.0
        energy = day_kwh + night_kwh
        cost = day_kwh * ctx["day"] + night_kwh * (ctx["night"] if ctx["night"] is not None else ctx["day"])
        # avg_w over the whole range: total watt-seconds back out over wall-clock
        # seconds in [since, now] -- interval-correct, no per-tick averaging needed.
        breakdown.append({"kind": kind, "name": name, "energy_kwh": round(energy, 4),
                          "cost": round(cost, 4),
                          "avg_w": round((day_wsec + night_wsec) / 3600.0 / max(1e-9, hours))})
    breakdown.sort(key=lambda x: -x["energy_kwh"])
    now_gpu = round(_app.LATEST.get("power") or 0)
    now_cpu = round(_app.LATEST.get("cpu_power") or 0) if _app.LATEST.get("cpu_power") is not None else None
    now_dram = round(_app.LATEST.get("dram_power") or 0) if _app.LATEST.get("dram_power") is not None else None
    now_total = now_gpu + (now_cpu or 0) + (now_dram or 0) + (round(idle_w) if idle_w else 0)
    measured = ["gpu"] + (["cpu"] if have_cpu else []) + (["dram"] if have_dram else [])
    machine = {"name": "local",
               "now_w": {"gpu": now_gpu, "cpu": now_cpu, "dram": now_dram, "total": now_total},
               "energy_kwh": {k: round(v, 3) for k, v in comp_kwh.items() if (k != "dram" or have_dram)},
               "cost": cost_win, "cost_range": round(cost_range, 2),
               "measured": measured, "estimated": (["other"] if idle_w else [])}
    machine["energy_kwh"]["total"] = round(sum(machine["energy_kwh"][k] for k in machine["energy_kwh"] if k != "total"), 3)
    machines = [machine]
    if host == "all":
        # Fleet mode: one machine per registered host with measurable power,
        # each integrated over its own host_samples history. Hosts with no
        # watt source are skipped — the fleet total only sums real numbers.
        for h in _app.list_hosts():
            try:
                hm = _host_machine(_app, ctx, h["name"], since, now)
                if hm["measured"]:
                    machines.append(hm)
            except Exception as e:
                print(f"fleet cost skip ({h.get('name')}):", e, flush=True)
    return jsonify({
        "enabled": ctx["day"] > 0, "range": rng, "bucket_sec": bk, "currency": cur,
        "rapl_available": have_cpu,
        "tariff": {"mode": ctx["mode"], "price_day": ctx["day"], "price_night": ctx["night"],
                   "night_start": ctx["night_start"], "night_end": ctx["night_end"]},
        "machines": machines, "components": series, "breakdown": breakdown[:40],
    })


@bp.route("/api/cost/heatmap")
def api_cost_heatmap():
    import app as _app
    """Busy-vs-quiet rhythm of the lab as a 7×24 grid (local day-of-week × hour).

    Each historical `samples` row is the machine's total draw (GPU+CPU+DRAM) for
    one _app.INTERVAL tick. We bucket every tick by its LOCAL weekday/hour and average
    the watts in each cell, then derive a cost-rate (€/h) for that cell at the
    tariff's price for that hour band — reusing the same `_app._cost_ctx`/`_app._price_at`
    machinery as the Costs page, so the €/kWh math never diverges. Sparse cells
    carry their own sample count so the UI can be honest about coverage.

    Pure-Python aggregation, read outside any held lock, always 200. When cost is
    disabled (no tariff) we still return the power grid and `enabled:false` so the
    card can render watts and prompt for a price.
    """
    ctx = _app._cost_ctx()
    cur = ctx["currency"]
    # window: last N days, sane default 30, capped at a year so a huge _app.DB can't stall
    try:
        days = int(request.args.get("days", "30"))
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(days, 365))
    now = int(time.time())
    since = now - days * 86400

    # 7×24 accumulators: summed watts and tick count per local day/hour cell
    sum_w = [[0.0] * 24 for _ in range(7)]
    cnt   = [[0]   * 24 for _ in range(7)]
    span_min = span_max = None
    host = (request.args.get("host") or "").strip()
    try:
        with _app.LOCK:
            if host and host != "local":
                from backend.db.repos import host_samples as hs_repo
                rows = hs_repo.heatmap(host, since, conn=_app.DB)
            else:
                rows = costs_repo.samples_1h_heatmap(since, conn=_app.DB)
        # aggregate OUTSIDE the lock — pure Python, no _app.DB calls below
        for ts, w, row_cnt in rows:
            lt = time.localtime(ts)
            # Python weekday(): Mon=0..Sun=6 — matches our locale day labels
            d = lt.tm_wday
            h = lt.tm_hour
            sum_w[d][h] += (w or 0) * (row_cnt or 1)
            cnt[d][h] += row_cnt or 1
            if span_min is None or ts < span_min:
                span_min = ts
            if span_max is None or ts > span_max:
                span_max = ts
    except Exception:
        rows = []

    # build the grids; price each cell at the tariff for a representative ts in it
    rep_ts = span_max or now
    rep_lt = time.localtime(rep_ts)
    # anchor to local midnight of the most recent observed day so is_night() lands
    # in the right band per (day,hour); the date component is irrelevant to the band
    anchor = int(time.mktime((rep_lt.tm_year, rep_lt.tm_mon, rep_lt.tm_mday,
                              0, 0, 0, 0, 0, -1)))
    avg_w  = [[None] * 24 for _ in range(7)]
    cost_h = [[None] * 24 for _ in range(7)]   # cost per HOUR at this cell's mean draw
    max_w = max_cost = 0.0
    busiest = quietest = None                  # by avg watts
    total_ticks = 0
    for d in range(7):
        for h in range(24):
            n = cnt[d][h]
            total_ticks += n
            if n == 0:
                continue
            aw = sum_w[d][h] / n
            avg_w[d][h] = round(aw)
            # price for this hour band: a ts at hour h on the anchor day
            cell_ts = anchor + h * 3600
            price = _app._price_at(ctx, cell_ts)
            ch = aw / 1000.0 * price           # W -> kW * €/kWh = €/h
            cost_h[d][h] = round(ch, 4)
            max_w = max(max_w, aw)
            max_cost = max(max_cost, ch)
            if busiest is None or aw > busiest["avg_w"]:
                busiest = {"day": d, "hour": h, "avg_w": round(aw),
                           "cost_h": round(ch, 4), "samples": n}
            if quietest is None or aw < quietest["avg_w"]:
                quietest = {"day": d, "hour": h, "avg_w": round(aw),
                            "cost_h": round(ch, 4), "samples": n}

    # busy vs quiet bands: top/bottom quartile of populated cells by avg watts
    populated = [(avg_w[d][h], cost_h[d][h])
                 for d in range(7) for h in range(24) if avg_w[d][h] is not None]
    bands = None
    if len(populated) >= 4:
        ordered = sorted(populated, key=lambda x: x[0])
        q = max(1, len(ordered) // 4)
        quiet_band = ordered[:q]
        busy_band = ordered[-q:]
        def band_stats(b):
            return {"avg_w": round(sum(x[0] for x in b) / len(b)),
                    "avg_cost_h": round(sum((x[1] or 0) for x in b) / len(b), 4),
                    "cells": len(b)}
        bands = {"busy": band_stats(busy_band), "quiet": band_stats(quiet_band)}

    # per-day rollups (busiest / quietest day by mean watts across populated hours)
    day_avg = [None] * 7
    for d in range(7):
        vals = [avg_w[d][h] for h in range(24) if avg_w[d][h] is not None]
        if vals:
            day_avg[d] = round(sum(vals) / len(vals))

    coverage = round(total_ticks / max(1, days * 24 * 3600 / _app.INTERVAL), 4)
    # "ready" once we have at least a day's worth of ticks spread across cells
    populated_cells = len(populated)
    ready = total_ticks >= (86400 / _app.INTERVAL) and populated_cells >= 6

    return jsonify({
        "ok": True,
        "enabled": ctx["day"] > 0,
        "currency": cur,
        "days": days,
        "interval_sec": _app.INTERVAL,
        "ready": ready,
        "rows": 7, "cols": 24,
        "avg_w": avg_w,
        "cost_h": cost_h,
        "samples": cnt,
        "day_avg_w": day_avg,
        "max_w": round(max_w),
        "max_cost_h": round(max_cost, 4),
        "busiest": busiest,
        "quietest": quietest,
        "bands": bands,
        "total_ticks": total_ticks,
        "populated_cells": populated_cells,
        "coverage": coverage,
        "span": {"min": span_min, "max": span_max},
        "tariff": {"mode": ctx["mode"], "price_day": ctx["day"], "price_night": ctx["night"],
                   "night_start": ctx["night_start"], "night_end": ctx["night_end"]},
    })


@bp.route("/api/costs/entity")
def api_costs_entity():
    import app as _app
    """Per-entity drilldown: a power + cumulative-cost time-series for one
    process/service/model over the range, plus what resources it used."""
    ctx = _app._cost_ctx()
    name = request.args.get("name", "")
    kind = request.args.get("kind", "")
    rng = request.args.get("range", "7d")
    span = _app.RANGES.get(rng, 604800)
    now = int(time.time())
    with _app.LOCK:
        since = (costs_repo.min_ts_power_proc(conn=_app.DB) or now) if span is None else now - span
        bk = max(_app.INTERVAL, round(max(1, now - since) / _app.MAX_POINTS))
        rows = costs_repo.power_proc_entity(name, since, bk, kind=kind, conn=_app.DB)
        # cumulative tariff-aware cost needs per-bucket price; classify by bucket start ts
        vram_peak = None
        if kind != "cpu":
            vram_peak = costs_repo.max_vram_for_service(name, since, conn=_app.DB)
    labels, watts, cost_cum, running, energy = [], [], [], 0.0, 0.0
    peak = 0.0
    # power_proc_entity now returns (bucket, wsec_sum, max_watts, avg_watts): the
    # wsec sum already bakes in each row's own interval_sec, so energy for the
    # bucket is a straight SUM(watts*interval_sec)/3.6e6 -- no bk/_app.INTERVAL
    # rescale needed (that rescale used to assume every row in a bucket was
    # sampled at the CURRENT global interval, which is exactly this bug).
    for b, wsec, maxw, avgw in rows:
        labels.append(int(b)); watts.append(round(avgw or 0))
        peak = max(peak, maxw or 0)
        e = (wsec or 0) / 3_600_000.0
        energy += e
        running += e * _app._price_at(ctx, int(b))
        cost_cum.append(round(running, 4))
    return jsonify({
        "name": name, "kind": kind, "range": rng, "bucket_sec": bk, "currency": ctx["currency"],
        "energy_kwh": round(energy, 4), "cost": round(running, 2),
        "avg_w": round(sum(watts) / len(watts)) if watts else 0, "peak_w": round(peak),
        "series": {"labels": labels, "watts": watts, "cost_cum": cost_cum},
        "resources": {"gpu_vram_peak_mb": round(vram_peak) if vram_peak else None},
    })


