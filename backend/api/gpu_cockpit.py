"""backend/api/gpu_cockpit.py — per-card GPU history and attribution, any host.

Two endpoints, both host-parameterised, both serving the hub and a remote from
the same storage:

  /api/gpu/history?host=&range=      per-card series + combined + card health
  /api/gpu/attribution?host=&range=  per-service VRAM over time + power estimate

`host=local` is the hub. There is deliberately no separate "remote" endpoint:
the dashboard used to render charts for the hub and a bare snapshot for everyone
else, and that fork existed only because the storage did. One reader, one shape.

Honesty rules applied throughout:

* A metric no card in the range ever reported is advertised as unsupported in
  `supports`, not sent as a row of zeros. "This driver can't tell us" and "the
  measured value is zero" are different claims and the UI draws them differently.
* Power attributed to a service is explicitly flagged `estimated` — GPUs do not
  meter power per process, so it is apportioned, and the payload says so rather
  than letting a chart imply a measurement.
"""
from flask import Blueprint, request, jsonify
import logging
import time

from backend import gpuspec
from backend.db.repos import gpu_samples as gpu_repo

bp = Blueprint('gpu_cockpit', __name__)

# Fallback only. The real threshold comes from settings via _hot_c() below, so
# the cockpit's "HOT" pill, its red threshold line and the alert that actually
# pages you are all the same number. A dashboard that draws a warning at one
# temperature while the notifier fires at another teaches the user to distrust
# both.
HOT_C = 84


def _hot_c(host):
    """The temperature threshold for `host`: its per-host override, else the
    configured global, else the default. Same resolution order the notifier
    uses — deliberately, so the two can never disagree."""
    import app as _app
    try:
        from backend.notify import _gpu_temp_threshold
        return _gpu_temp_threshold(_app.get_settings(), host)
    except Exception:
        logging.debug("gpu temp threshold lookup failed for host=%s", host, exc_info=True)
        return HOT_C


def _live_services(host):
    """[{service, mem, by_card}] holding VRAM on `host` right now.

    Computed server-side for both the hub and a remote so the "which service is
    on which card" rule lives in exactly one place. Duplicating it in JS is how
    the two drift into disagreeing about the same box.
    """
    import app as _app
    if host == "local":
        return [dict(p) for p in (_app.LATEST.get("procs") or []) if (p.get("mem") or 0) > 0]
    with _app.HOST_DATA_LOCK:
        entry = _app.HOST_DATA.get(host)
    if not entry or "data" not in entry:
        return []
    h = entry["data"].get("host") or {}
    # Same precedence as the poller's stored attribution: container names first
    # (a human recognises "ollama", not "llama-server"), bare process names for
    # whatever VRAM lives outside a container.
    rows = _app._host_vram_rows(0, host, h)
    # Per-card splits, from whichever side actually knows the link. The probe
    # resolves it for containers (pid -> cgroup -> container, pid -> GPU uuid),
    # because nothing downstream can reconstruct it from a container name alone:
    # the container is "ollama" and the process on the card is "llama-server".
    by_name = {}
    for c in ((h.get("docker") or {}).get("containers") or []):
        if c.get("vram_by_card") and c.get("name"):
            by_name[c["name"]] = c["vram_by_card"]
    for p in (h.get("gpu_procs") or []):
        if p.get("by_card"):
            by_name["host:" + str(p.get("name") or "")] = p["by_card"]
    out = []
    for _ts, svc, mem, _host in rows:
        item = {"service": svc, "mem": mem}
        if svc in by_name:
            item["by_card"] = by_name[svc]
        out.append(item)
    out.sort(key=lambda x: -(x["mem"] or 0))
    return out


def _live_cards(host):
    """(cards, aggregate, procs, at, online) as most recently seen for `host`.

    The live snapshot, not history: it carries fields the time-series doesn't
    (card name, vendor, per-process by_card VRAM) and it is what makes the tab
    paint instantly on a host whose history is still filling.

    Compute (TFLOPS) is attached HERE rather than at the probe, so a remote host
    running an older probe still gets its FLOPS figures — everything the lookup
    needs is the card name and clock, both of which every host already reports.
    """
    import app as _app
    if host == "local":
        return (gpuspec.attach(list(_app.LATEST.get("gpus") or [])),
                _app.LATEST.get("gpu_extra") or {},
                list(_app.LATEST.get("procs") or []),
                _app.LATEST.get("ts"), True)
    with _app.HOST_DATA_LOCK:
        entry = _app.HOST_DATA.get(host)
    if not entry or "data" not in entry:
        return [], {}, [], (entry or {}).get("at"), False
    h = (entry["data"].get("host") or {})
    return (gpuspec.attach(list(h.get("gpus") or [])), h.get("gpu") or {},
            list(h.get("gpu_procs") or []), entry.get("at"),
            _app._host_is_online(entry))


def _span_and_bucket(rng, host):
    """(since, bucket_seconds, now) for a range key, sized like /api/data.

    The `range=all` branch touches the shared DB connection, so it takes LOCK
    like every other reader in this codebase — the collector writes on its own
    thread and an unlocked read here would race it.
    """
    import app as _app
    span = _app.RANGES.get(rng, 21600)
    now = int(time.time())
    if span is None:
        with _app.LOCK:
            since = gpu_repo.min_ts(host, conn=_app.DB) or now
    else:
        since = now - span
    bk = max(_app.INTERVAL, round(max(1, now - since) / _app.MAX_POINTS))
    return since, bk, now


def _stitch_spans(rows, interval):
    """Throttle samples → [{idx, start, end, reasons}] merged spans.

    Consecutive samples belong to one span; a gap longer than two poll intervals
    starts a new one. Done here rather than in SQL because "consecutive" depends
    on the poll interval, which the caller knows and the database doesn't.
    """
    import app as _app
    gap = max(interval * 2, 2)
    spans, open_span = [], {}
    for ts, idx, mask in rows:
        cur = open_span.get(idx)
        if cur and ts - cur["end"] <= gap:
            cur["end"] = ts
            cur["mask"] |= (mask or 0)
        else:
            if cur:
                spans.append(cur)
            open_span[idx] = {"idx": idx, "start": ts, "end": ts, "mask": mask or 0}
    spans.extend(open_span.values())
    for s in spans:
        # A span one sample long has zero width; give it the poll interval so it
        # is visible on a chart rather than being a zero-width invisible marker.
        if s["end"] == s["start"]:
            s["end"] = s["start"] + interval
        s["reasons"] = _app._decode_throttle(hex(s.pop("mask")))
    return sorted(spans, key=lambda s: (s["idx"], s["start"]))


def _status_for(live, hot_c, recent=True, have_live=True):
    """The status pill: what a human should conclude about this card at a glance.

    Ordering matters — a card can be hot AND busy, and "hot" is the fact worth
    surfacing. Power-capping is deliberately NOT a warning state: a box running a
    deliberately lowered power limit sits at its cap by design.

    Three different reasons a card can be absent from the live snapshot, and
    conflating any two of them produces a false alarm:

    * `have_live` false — the HOST isn't reporting any cards at all right now
      (offline, or the hub restarted and hasn't polled yet). We don't know
      anything about this card; that is "stale", not an incident. Without this
      the whole tab lights up red for a minute after every restart.
    * `recent` false — the card's last sample is old, so it was removed from the
      machine. History, not a fault.
    * otherwise — the host is reporting cards and this one isn't among them.
      That is a card that fell off the bus, and it is worth waking someone for.
    """
    import app as _app
    if live is None:
        if not have_live:
            return "stale"
        return "gone" if recent else "retired"
    mask = live.get("throttle_mask") or 0
    if mask & _app._THERMAL_BITS:
        return "throttle"
    if (live.get("temp") or 0) >= hot_c:
        return "hot"
    util = live.get("util") or 0
    if util >= 5:
        return "busy"
    return "idle"


@bp.route("/api/gpu/history")
def api_gpu_history():
    """Per-card GPU history for one host — the GPU cockpit's main feed."""
    import app as _app
    host = request.args.get("host", "local")
    rng = request.args.get("range", "6h")
    since, bk, now = _span_and_bucket(rng, host)
    interval = _app.INTERVAL

    hot_c = _hot_c(host)
    live_cards, live_agg, live_procs, at, online = _live_cards(host)
    live_by_idx = {c["idx"]: c for c in live_cards if c.get("idx") is not None}

    with _app.LOCK:
        rows = gpu_repo.series(host, since, bk, conn=_app.DB)
        health_rows = gpu_repo.health(host, since, hot_c=hot_c, interval=interval, conn=_app.DB)
        thr_rows = gpu_repo.throttle_spans(host, since, conn=_app.DB)
        stored_idxs = gpu_repo.cards_for(host, conn=_app.DB)
        last_seen = gpu_repo.last_seen(host, conn=_app.DB)
    # A card counts as "should still be here" if it reported within the last few
    # polls. Generous enough to survive a slow host or one skipped cycle, tight
    # enough that a card pulled from the machine last week doesn't alert forever.
    recent_cutoff = now - max(interval * 6, 120)

    labels = sorted({int(r[0]) for r in rows})
    pos = {b: i for i, b in enumerate(labels)}
    n = len(labels)
    # Every card the host has EVER reported, plus whatever it is reporting right
    # now: a card that has just been added has no history, and a card that fell
    # off the bus has history but no live entry. Both must appear — the second is
    # the more important one, since a vanished card is a hardware incident.
    idxs = sorted(set(stored_idxs) | set(live_by_idx))

    # series[idx][metric] = [...]; None (not 0) for buckets with no sample, so a
    # gap in the history renders as a gap rather than a dive to zero.
    METRICS = ("util", "vram", "vram_total", "power", "temp", "temp_max",
               "fan", "fan_max", "mem_util", "clk_sm")
    series = {i: {m: [None] * n for m in METRICS} for i in idxs}
    for (b, idx, util, mem_used, mem_total, power, temp, temp_max,
         fan, fan_max, mem_util, clk_sm, _thr) in rows:
        i = pos.get(int(b))
        if i is None or idx not in series:
            continue
        s = series[idx]
        s["util"][i] = _r(util)
        s["vram"][i] = _r(mem_used)
        s["vram_total"][i] = _r(mem_total)
        s["power"][i] = _r(power)
        s["temp"][i] = _r(temp)
        s["temp_max"][i] = _r(temp_max)
        s["fan"][i] = _r(fan)
        s["fan_max"][i] = _r(fan_max)
        s["mem_util"][i] = _r(mem_util)
        s["clk_sm"][i] = _r(clk_sm)

    spans_by_idx = {}
    for s in _stitch_spans(thr_rows, interval):
        spans_by_idx.setdefault(s["idx"], []).append(s)

    # Past the raw retention window the numbers come from the hourly rollup, so
    # "hot" is hour-granular there. Flagged rather than quietly presented as if
    # it were minute-accurate.
    downsampled = bool(rows) and bk >= 3600
    health_by_idx = {}
    for (idx, window_sec, avg_t, peak_t, peak_fan, thr_sec, hot_sec, capped_pct) in health_rows:
        health_by_idx[idx] = {
            "idx": idx,
            "avg_temp": _r(avg_t), "peak_temp": _r(peak_t), "peak_fan": _r(peak_fan),
            "throttled_sec": thr_sec,
            "hot_sec": hot_sec,
            "capped_pct": capped_pct,
            "window_sec": window_sec,
            "downsampled": downsampled,
        }

    cards = []
    for idx in idxs:
        live = live_by_idx.get(idx)
        s = series[idx]
        # "Supported" means this card actually reported the metric at least once
        # in the window (or is reporting it live). Anything else is advertised as
        # unsupported so the UI can say "not reported by this driver" instead of
        # drawing a confident flat zero.
        supports = {m: any(v is not None for v in s[m]) for m in ("fan", "mem_util", "clk_sm", "temp")}
        if live:
            for k, m in (("fan", "fan"), ("mem_util", "mem_util"), ("clk_sm", "clk_sm"), ("temp", "temp")):
                if live.get(m) is not None:
                    supports[k] = True
        # Peak FLOP/s for this card, and the clock-scaled series derived from the
        # core clock we were already storing. Two conditions, not one: the card
        # has to be in the spec table AND the driver has to report a clock. An
        # unrecognised card advertises `tflops` unsupported rather than drawing a
        # flat zero line — the same rule every other optional metric follows.
        compute = gpuspec.compute_for((live or {}).get("name"), (live or {}).get("clk_sm"))
        supports["tflops"] = bool(compute) and supports["clk_sm"]
        if compute:
            boost = compute["boost_mhz"]
            s["tflops"] = [None if v is None else round(compute["fp32"] * v / boost, 2)
                           for v in s["clk_sm"]]
        else:
            s["tflops"] = [None] * n
        cards.append({
            "idx": idx,
            "name": (live or {}).get("name") or f"GPU {idx}",
            "vendor": (live or {}).get("vendor"),
            "compute": compute,
            "mem_total": (live or {}).get("mem_total") or _last(s["vram_total"]) or 0,
            "power_limit": (live or {}).get("power_limit") or 0,
            "present": live is not None,
            "last_seen": last_seen.get(idx),
            "status": _status_for(live, hot_c,
                                  recent=(last_seen.get(idx) or 0) >= recent_cutoff,
                                  have_live=bool(live_by_idx)),
            "now": _now_block(live),
            "supports": supports,
            "series": s,
            "throttle_spans": spans_by_idx.get(idx, []),
            "health": health_by_idx.get(idx),
        })

    # Combined: VRAM and power are sums across cards, util is the mean, and
    # temperature is the MAX. Averaging temperature across cards would hide the
    # one card that is cooking — which is the entire question this panel answers.
    combined = {"util": [], "vram": [], "vram_total": [], "power": [], "temp_max": [],
                "fan_max": [], "tflops": []}
    for i in range(n):
        vals = [series[idx] for idx in idxs]
        combined["util"].append(_mean([v["util"][i] for v in vals]))
        combined["vram"].append(_sum([v["vram"][i] for v in vals]))
        combined["vram_total"].append(_sum([v["vram_total"][i] for v in vals]))
        combined["power"].append(_sum([v["power"][i] for v in vals]))
        combined["temp_max"].append(_max([v["temp_max"][i] for v in vals]))
        combined["fan_max"].append(_max([v["fan_max"][i] for v in vals]))
        # Summed like power, not averaged like util: the box's FLOP/s ceiling is
        # what all its cards can do at once. _sum ignores the cards that
        # contributed nothing, so an unrecognised card in the box lowers the
        # figure rather than voiding it — which is why the pooled block below
        # also ships the recognised-card count.
        combined["tflops"].append(_sum_f([v["tflops"][i] for v in vals]))

    capacity = sum(c["mem_total"] for c in cards) or 0
    pooled_limit = (sum(c["power_limit"] for c in cards)
                    if cards and all(c["power_limit"] for c in cards) else 0)
    return jsonify({
        "host": host, "range": rng, "bucket_sec": bk, "interval": interval,
        "labels": labels, "at": at, "online": online,
        "cards": cards, "combined": combined,
        "capacity_mb": capacity, "power_limit": pooled_limit,
        "hot_c": hot_c,
        "now_services": _live_services(host),
        "now_pooled": _pooled_now(live_cards, live_agg),
        # An empty payload has two very different causes and the UI must not
        # conflate them: a host with no GPU at all, versus a GPU host whose
        # history hasn't accumulated yet.
        "has_gpu": bool(cards),
        "has_history": n > 0,
    })


def _pooled_now(cards, agg):
    """The whole box in one block: VRAM and power summed, utilisation averaged,
    temperature and fan taken as the MAX across cards.

    Max, not mean, for temp and fan on purpose — the headline number should be
    the card in trouble. A mean over three cards where one is at 87 °C and two
    idle at 45 °C reads as a comfortable 59 °C and tells the user nothing.
    Optionals are contributed only by the cards that measured them, so a
    passively cooled card can't drag the fan figure toward a value nothing
    reported.
    """
    if not cards:
        return None
    fans = [c["fan"] for c in cards if c.get("fan") is not None]
    caps = [c.get("power_limit") or 0 for c in cards]
    out = {
        "count": len(cards),
        "util": round(sum(c.get("util") or 0 for c in cards) / len(cards)),
        "mem_used": round(sum(c.get("mem_used") or 0 for c in cards)),
        "mem_total": round(sum(c.get("mem_total") or 0 for c in cards)),
        "power": round(sum(c.get("power") or 0 for c in cards)),
        "temp_max": round(max((c.get("temp") or 0) for c in cards)),
        "busy": sum(1 for c in cards if (c.get("util") or 0) >= 5),
    }
    if fans:
        out["fan_avg"] = round(sum(fans) / len(fans))
        out["fan_max"] = max(fans)
        rpms = [c["fan_rpm"] for c in cards if c.get("fan_rpm") is not None]
        if rpms:
            out["fan_rpm_max"] = max(rpms)
    # Only publish a pooled cap when EVERY card contributed one, else the ratio
    # would exceed 100% purely because the denominator is missing a card the
    # numerator includes.
    if all(caps):
        out["power_limit"] = round(sum(caps))
    # The box's arithmetic ceiling. Absent — not zero — when none of the cards
    # are in the spec table, because "we don't know this card" and "this card
    # can do no work" are not the same statement.
    compute = gpuspec.pooled(cards)
    if compute:
        out["compute"] = compute
    names = sorted({c.get("name") or "GPU" for c in cards})
    out["model"] = (f"{len(cards)}× {names[0]}" if len(names) == 1 and len(cards) > 1
                    else " + ".join(names))
    return out


def _now_block(live):
    """The card's live values, with absent metrics left absent."""
    if not live:
        return None
    out = {}
    for k in ("util", "mem_used", "mem_total", "power", "power_limit", "temp",
              "fan", "fan_rpm", "mem_util", "clk_sm", "clk_mem", "temp_mem", "pstate",
              "compute"):
        v = live.get(k)
        if v is not None:
            out[k] = v
    if live.get("throttled"):
        out["throttled"] = True
        out["throttle"] = live.get("throttle") or []
    return out


def _r(v, nd=0):
    if v is None:
        return None
    return round(v, nd) if nd else round(v)


def _mean(vals):
    xs = [v for v in vals if v is not None]
    return round(sum(xs) / len(xs)) if xs else None


def _sum(vals):
    xs = [v for v in vals if v is not None]
    return round(sum(xs)) if xs else None


def _sum_f(vals):
    """_sum for a quantity where whole numbers are too coarse — a P2000 idling
    at 139 MHz is 0.28 TFLOPS, and rounding that to 0 draws a flat line."""
    xs = [v for v in vals if v is not None]
    return round(sum(xs), 2) if xs else None


def _max(vals):
    xs = [v for v in vals if v is not None]
    return round(max(xs)) if xs else None


def _last(vals):
    for v in reversed(vals):
        if v is not None:
            return v
    return None


@bp.route("/api/gpu/attribution")
def api_gpu_attribution():
    """Who is using this host's GPUs: VRAM per service over time, plus a power
    estimate and what that energy cost.

    The power split is an APPORTIONMENT, not a measurement. GPUs meter power per
    card, never per process, so each service is charged a share of the card's
    measured draw above its idle floor, in proportion to the VRAM it holds. The
    response says `estimated: true` and the UI repeats it, because presenting a
    modelled number as a measured one is the kind of quiet lie a monitoring tool
    should never tell.
    """
    import app as _app
    host = request.args.get("host", "local")
    rng = request.args.get("range", "6h")
    since, bk, now = _span_and_bucket(rng, host)

    with _app.LOCK:
        rows = gpu_repo.vram_by_service(host, since, bk, conn=_app.DB)
        totals = gpu_repo.service_totals(host, since, conn=_app.DB)
        ticks = gpu_repo.distinct_sample_times(host, since, conn=_app.DB)
        power_rows = gpu_repo.series(host, since, bk, conn=_app.DB)

    labels = sorted({int(r[0]) for r in rows} | {int(r[0]) for r in power_rows})
    pos = {b: i for i, b in enumerate(labels)}
    n = len(labels)

    services = {}
    for b, svc, mem in rows:
        i = pos.get(int(b))
        if i is not None:
            services.setdefault(svc, [0] * n)[i] = _r(mem)

    # Pooled draw and pooled VRAM per bucket, for the apportionment below.
    pooled_w, pooled_vram = [0.0] * n, [0.0] * n
    for r in power_rows:
        i = pos.get(int(r[0]))
        if i is None:
            continue
        pooled_w[i] += (r[5] or 0)
        pooled_vram[i] += (r[3] or 0)

    # Idle floor: the lowest pooled draw seen in the window. Cards burn a real,
    # substantial baseline just being powered on (an idle 3090 is ~100 W), and
    # charging that to whichever service happens to hold VRAM would materially
    # overstate its cost. It is reported as its own band instead.
    idle_floor = min((w for w in pooled_w if w > 0), default=0.0)

    power_series = {}
    for svc, mem_series in services.items():
        ps = []
        for i in range(n):
            share = (mem_series[i] / pooled_vram[i]) if pooled_vram[i] else 0
            ps.append(round(max(0.0, pooled_w[i] - idle_floor) * share, 1))
        power_series[svc] = ps

    s = _app.get_settings()
    try:
        price = float(s.get("kwh_price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    hours_per_bucket = bk / 3600.0

    out = []
    peak_by_svc = {t[0]: (t[1], t[2], t[3]) for t in totals}
    for svc, mem_series in sorted(services.items(), key=lambda kv: -max(kv[1] or [0])):
        peak, avg, present = peak_by_svc.get(svc, (0, 0, 0))
        kwh = sum(power_series[svc]) * hours_per_bucket / 1000.0
        out.append({
            "service": svc,
            "vram": mem_series,
            "power": power_series[svc],
            "peak_mb": _r(peak), "avg_mb": _r(avg),
            "pct_time": round(present * 100.0 / ticks, 1) if ticks else 0,
            "est_kwh": round(kwh, 3),
            "est_cost": round(kwh * price, 2) if price else None,
        })

    idle_kwh = idle_floor * n * hours_per_bucket / 1000.0
    return jsonify({
        "host": host, "range": rng, "bucket_sec": bk, "labels": labels,
        "services": out,
        "idle_floor_w": round(idle_floor, 1),
        "idle": {"power": [round(idle_floor, 1)] * n,
                 "est_kwh": round(idle_kwh, 3),
                 "est_cost": round(idle_kwh * price, 2) if price else None},
        "currency": s.get("currency") or "$",
        "kwh_price": price,
        # Load-bearing: the UI prints this next to every wattage on this endpoint.
        "estimated": True,
    })
