"""backend/api/system.py — system routes (Phase 3.4)."""
from flask import Blueprint, request, jsonify, Response, send_file, send_from_directory, after_this_request, g, abort
import time
import os
import re
import shutil
import tempfile
import threading

from backend.db.repos import system as system_repo

bp = Blueprint('system', __name__)


@bp.route("/api/data")
def api_data():
    import app as _app
    rng = request.args.get("range", "6h")
    span = _app.RANGES.get(rng, 21600); now = int(time.time())
    with _app.LOCK:
        since = (system_repo.min_ts_samples(conn=_app.DB) or now) if span is None else now - span
        bk = max(_app.INTERVAL, round(max(1, now - since) / _app.MAX_POINTS))
        tot = system_repo.query_samples_bucketed(bk, since, conn=_app.DB)
        labels = [int(r[0]) for r in tot]
        idx = {b: i for i, b in enumerate(labels)}
        total = {"util": [round(r[1] or 0) for r in tot], "mem": [round(r[2] or 0) for r in tot],
                 "mempk": [round(r[3] or 0) for r in tot], "power": [round(r[4] or 0) for r in tot],
                 "temp": [round(r[5] or 0) for r in tot], "cpu": [round(r[6] or 0) for r in tot],
                 "ram_used": [round(r[7] or 0) for r in tot], "ram_total": [round(r[8] or 0) for r in tot],
                 "load1": [round(r[9] or 0, 2) for r in tot], "ctemp": [round(r[10] or 0) for r in tot]}
        services = {}
        for b, svc, mem in system_repo.query_proc_bucketed(bk, since, conn=_app.DB):
            i = idx.get(int(b))
            if i is not None:
                services.setdefault(svc, [0] * len(labels))[i] = round(mem or 0)
        other = [max(0, total["mem"][i] - sum(s[i] for s in services.values())) for i in range(len(labels))]
        # Per-device disk-I/O trend, same bucketing/labels as everything else on this
        # endpoint — feeds the Disk I/O tab's per-device sparklines.
        disk_io = {}
        for b, dev, r, w, u in system_repo.query_disk_io_bucketed(bk, since, conn=_app.DB):
            i = idx.get(int(b))
            if i is None:
                continue
            d = disk_io.setdefault(dev, {"read_mb_s": [0] * len(labels),
                                         "write_mb_s": [0] * len(labels),
                                         "util_pct": [0] * len(labels)})
            d["read_mb_s"][i]  = round(r or 0, 1)
            d["write_mb_s"][i] = round(w or 0, 1)
            d["util_pct"][i]   = round(u or 0, 1)
        ticks = system_repo.count_samples_since(since, conn=_app.DB)
        summary = sorted(({"service": s, "peak": round(pk), "avg": round(av), "present": round(100 * cnt / ticks)}
                          for s, pk, av, cnt in system_repo.query_proc_summary(since, conn=_app.DB)),
                         key=lambda x: -x["peak"])
        model_summary = sorted(({"service": s, "model": m, "peak": round(pk or 0), "avg": round(av or 0)}
                                for s, m, pk, av in system_repo.query_model_summary(since, conn=_app.DB)),
                               key=lambda x: -x["peak"])
        callers = sorted(({"caller": c, "server": s, "seconds": int((tot or 0) * _app.INTERVAL), "samples": n}
                          for c, s, tot, n in system_repo.query_edges_summary(since, conn=_app.DB)),
                         key=lambda x: -x["seconds"])
        evs = [{"ts": t, "service": s, "kind": k, "detail": d}
               for t, s, k, d in system_repo.query_events_range(since, conn=_app.DB)]
        oom_evs = [e for e in evs if e["kind"] == "oom"]
        for e in oom_evs:
            row = system_repo.query_proc_at_time(e["ts"] + _app.INTERVAL, e["service"], conn=_app.DB)
            if row:
                e["blame"] = (f"{e['service']} lost to {row[0]} (holding {round(row[1])} MB) at "
                              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(e['ts']))}.")
        mem_total = _app.LATEST["mem_total"] or 24576
        peak = max(total["mempk"]) if total["mempk"] else 0
        insights = _app.build_insights(total, services, mem_total, oom_evs, _app.LATEST["host"])
        diskio_evs = [e for e in evs if e["kind"] == "diskio_spike"]
        if diskio_evs:
            latest_by_dev = {}
            for e in diskio_evs:
                latest_by_dev[e["service"]] = e   # `service` column holds the device name here
            for dev, e in latest_by_dev.items():
                insights.append({"level": "warning", "title": f"Disk I/O spike on {dev}",
                                 "detail": e["detail"]})
    # Uptime rows ride the same Insight Feed (computed outside _app.LOCK — _app.uptime_overview
    # takes it itself). DOWN/slow endpoints surface on the cockpit with no new tile.
    try:
        insights = insights + _app.uptime_insights()
        up_summary = _app.uptime_summary()
    except Exception as e:
        print("uptime overview error:", e, flush=True)
        up_summary = {"total": 0, "up": 0, "down": 0, "unknown": 0, "worst_down": None}
    return jsonify({"version": _app.VERSION, "range": rng, "bucket_sec": bk, "labels": labels, "total": total,
                    "services": services, "other": other, "summary": summary, "model_summary": model_summary,
                    "callers": callers, "events": oom_evs, "insights": insights, "pressure_free_mb": _app.PRESSURE_MB,
                    "uptime_summary": up_summary, "disk_io": disk_io,
                    "mem_total": mem_total, "peak_mem": peak, "now": _app.LATEST})


@bp.route("/api/network")
def api_network():
    import app as _app
    """Host NIC throughput + per-container top talkers over a range (#30). Rates
    are derived from the cumulative byte counters in net_samples, so a missed
    sample or a counter reset (reboot) never invents a spike."""
    rng = request.args.get("range", "1h")
    span = _app.RANGES.get(rng, 3600)
    now = int(time.time())
    with _app.LOCK:
        since = (system_repo.min_ts_net_samples(conn=_app.DB) or now) if span is None else now - span
        rows = system_repo.query_net_samples(since, conn=_app.DB)
    bk = max(_app.INTERVAL, round(max(1, now - since) / _app.MAX_POINTS))
    series = {}
    for ts, iface, bi, bo in rows:
        series.setdefault(iface, []).append((ts, bi, bo))

    def rate_buckets(samples):
        """Consecutive cumulative samples → {bucket: [sum_rate, count]} for in/out."""
        ain, aout = {}, {}
        for (t0, i0, o0), (t1, i1, o1) in zip(samples, samples[1:]):
            dt = t1 - t0
            di, do = i1 - i0, o1 - o0
            if dt <= 0 or di < 0 or do < 0:        # gap or counter reset → skip
                continue
            b = (t1 // bk) * bk
            s = ain.setdefault(b, [0, 0]);  s[0] += di / dt; s[1] += 1
            s = aout.setdefault(b, [0, 0]); s[0] += do / dt; s[1] += 1
        return ain, aout

    host_ifaces = sorted(i for i in series if not i.startswith("@") and not _app._HOST_NIC_SKIP.match(i))
    labels = sorted({(t // bk) * bk for i in host_ifaces for t, _, _ in series[i]})
    ifaces_out = []
    for iface in host_ifaces:
        ain, aout = rate_buckets(series[iface])
        ins  = [round(ain[b][0] / ain[b][1]) if ain.get(b, [0, 0])[1] else 0 for b in labels]
        outs = [round(aout[b][0] / aout[b][1]) if aout.get(b, [0, 0])[1] else 0 for b in labels]
        if any(ins) or any(outs):
            ifaces_out.append({"iface": iface, "in": ins, "out": outs})

    talkers = []
    for iface, samples in series.items():
        if not iface.startswith("@") or len(samples) < 2:
            continue
        di = max(0, samples[-1][1] - samples[0][1])
        do = max(0, samples[-1][2] - samples[0][2])
        if di + do > 0:
            talkers.append({"name": iface[1:], "bytes_in": di, "bytes_out": do, "total": di + do})
    talkers.sort(key=lambda x: -x["total"])

    cur_in  = sum(s["in"][-1] for s in ifaces_out) if labels else 0
    cur_out = sum(s["out"][-1] for s in ifaces_out) if labels else 0
    return jsonify({"range": rng, "bucket_sec": bk, "labels": labels,
                    "ifaces": ifaces_out, "talkers": talkers[:10],
                    "current": {"in": cur_in, "out": cur_out}})


@bp.route("/healthz")
def healthz():
    import app as _app
    """Cheap liveness probe for Docker's HEALTHCHECK and any uptime monitor.
    No _app.DB, no locks — just a 200 with the running version so the answer is
    instant and never gets blocked behind a slow collector pass."""
    return jsonify({"status": "ok", "version": _app.VERSION}), 200


@bp.route("/api/changelog")
def api_changelog():
    import app as _app
    """Serve the bundled CHANGELOG.md, sliced to a version range, so the dashboard's
    one-time 'what's new' modal can show exactly what shipped — straight from the
    image, no GitHub round-trip (works fully offline). Read-only.
      ?to=<ver>     newest version to include (default: the running _app.VERSION)
      ?since=<ver>  exclusive lower bound — return every section newer than it,
                    up to `to` (the multi-version roll-up). Omit for just `to`."""
    to_v = request.args.get("to") or _app.VERSION
    since_v = request.args.get("since")
    to_t = _app._parse_semver(to_v)
    since_t = _app._parse_semver(since_v) if since_v else None
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(_app.__file__)), "CHANGELOG.md")
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        print(f"api/system api_changelog error reading CHANGELOG.md: {e}", flush=True)
        return jsonify({"current": _app.VERSION, "sections": [], "markdown": ""})
    # Split on "## [x.y.z](url) — date" headers (Keep-a-Changelog style).
    hdr = re.compile(r"^##\s*\[([^\]]+)\]\(([^)]*)\)\s*[—\-–]?\s*(.*)$")
    sections, cur = [], None
    for line in text.splitlines():
        m = hdr.match(line)
        if m:
            if cur:
                sections.append(cur)
            cur = {"version": m.group(1).strip(), "url": m.group(2).strip(),
                   "date": m.group(3).strip(), "lines": [line]}
        elif cur is not None:
            cur["lines"].append(line)
    if cur:
        sections.append(cur)
    picked = []
    for s in sections:                       # file is newest-first
        sv = _app._parse_semver(s["version"])
        if sv > to_t:
            continue
        if since_t is not None:
            if sv > since_t:
                picked.append(s)
        else:
            picked.append(s)                 # no lower bound → just the newest <= to
            break
    md = "\n".join("\n".join(s["lines"]).rstrip() for s in picked)
    return jsonify({"current": _app.VERSION, "to": to_v, "since": since_v,
                    "sections": [{"version": s["version"], "date": s["date"], "url": s["url"]}
                                 for s in picked],
                    "markdown": md})


@bp.route("/favicon.ico")
def favicon():
    import app as _app
    """Default-favicon URL — browsers ask for /favicon.ico even when an explicit
    <link rel="icon"> points elsewhere (during early page load, or for tabs that
    open without rendering HTML). Serve the SVG we ship in static/."""
    return _app.app.send_static_file("favicon.svg")


@bp.route("/locales/<path:fn>")
def locales(fn):
    import app as _app
    """Serve UI translation files (i18n, #148). The dashboard fetches
    /locales/<code>.json for any non-English locale; English is inlined, so it
    needs no fetch. send_from_directory guards against path traversal."""
    if not fn.endswith(".json"):
        return ("Not found", 404)
    try:
        resp = send_from_directory(_app._LOCALES_DIR, fn)
    except Exception as e:
        print(f"api/system api_locale error serving {fn}: {e}", flush=True)
        return ("Not found", 404)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.route("/api/mcp-status")
def api_mcp_status():
    import app as _app
    return jsonify(_app.build_mcp_status())


@bp.route("/api/health")
def api_health():
    import app as _app
    """Current state of the status monitors (Docker + systemd) plus a light GPU/host
    snapshot. Cheap and _app.DB-free, so the dashboard can poll it often."""
    gpu_avail = _app.LATEST.get("gpu_avail")
    now = {"gpu": {"util": _app.LATEST["util"], "mem_used": _app.LATEST["mem_used"],
                   "mem_total": (_app.LATEST["mem_total"] or 24576) if gpu_avail else 0,
                   "power": _app.LATEST["power"], "temp": _app.LATEST["temp"],
                   "available": bool(gpu_avail),
                   "gpus": _app.LATEST.get("gpus") or [],    # per-card detail (issue #95)
                   "extra": _app.LATEST.get("gpu_extra") or {}},  # mem-bw/clocks/throttle (telemetry)
           "host": _app.enrich_os_upgrade(_app.LATEST["host"])}
    docker  = _app.HEALTH["docker"]  or {"available": False, "reason": "warming up…",
                                    "containers": [], "summary": {"total": 0, "running": 0, "problems": 0}}
    systemd = _app.HEALTH["systemd"] or {"available": False, "reason": "warming up…",
                                    "services": [], "summary": {}}
    update  = dict(_app.HEALTH["update"] or {"available": False, "current": _app.VERSION})
    # Let the frontend decide whether to show the one-click "Update now" button.
    # Set here (not baked into the cached _app.collect_update payload) so toggling the
    # env flag takes effect on restart without waiting for the update cache.
    update["self_update_enabled"] = _app.ALLOW_SELF_UPDATE
    # Same "toggle takes effect without waiting for cache" reasoning as above —
    # controls_enabled drives whether the Containers/Services tabs show action
    # buttons at all (see ENABLE_CONTROLS).
    docker = dict(docker); docker["controls_enabled"] = _app.ENABLE_CONTROLS
    systemd = dict(systemd); systemd["controls_enabled"] = _app.ENABLE_CONTROLS
    disk_io = dict(_app.HEALTH.get("disk_io") or {"available": False, "warming_up": True,
                                              "summary": {"total_read_mb_s": 0.0, "total_write_mb_s": 0.0},
                                              "items": []})
    # Per-process I/O attribution (Top writer/reader) — attach ONLY to this authed
    # payload. Carries process comm (never cmdline/argv) and NEVER appears on the
    # public status surface (build_public_status doesn't read processes/disk_io).
    _pio = (_app.HEALTH.get("processes") or {}).get("io")
    if _pio and _pio.get("available"):
        disk_io["attribution"] = _pio
    if _app._diskio_anom_latest:
        disk_io["anomalies"] = dict(_app._diskio_anom_latest)
    return jsonify({"version": _app.VERSION, "updated": _app.HEALTH["at"], "now": now,
                    "docker": docker, "systemd": systemd, "update": update,
                    "processes": _app.HEALTH["processes"],
                    "disk_io": disk_io,
                    "os_updates": _app.os_updates_summary(),
                    "diagnostics": _app.local_diagnostics(),
                    "mcp": {"enabled": _app._mcp_enabled(), "port": _app._mcp_port()},
                    "overview": _app.build_overview(now, docker, systemd)})


@bp.route("/metrics")
def metrics():
    import app as _app
    """Prometheus text-format scrape endpoint.

    Reads exclusively from the in-memory snapshots (_app.LATEST / _app.HEALTH) that the
    background collector keeps fresh.  No new I/O is triggered on each scrape,
    so double-sampling is impossible.
    """
    if not _app._PROM_OK:
        return Response("# prometheus_client not installed\n", mimetype="text/plain", status=503)

    # Clear all multi-label gauges before re-populating so stale series vanish.
    for key in ("gpu_vram_used", "gpu_vram_total", "gpu_util", "gpu_temp", "gpu_power",
                "host_disk_used", "model_vram", "container_state", "systemd_unit",
                "models_installed"):
        _app._G[key].clear()

    # ── GPU ──────────────────────────────────────────────────────────────────
    gpu_label = "gpu0"
    _app._G["gpu_vram_used"].labels(gpu=gpu_label).set(_app.LATEST.get("mem_used", 0))
    _app._G["gpu_vram_total"].labels(gpu=gpu_label).set(_app.LATEST.get("mem_total", 0))
    _app._G["gpu_util"].labels(gpu=gpu_label).set(_app.LATEST.get("util", 0))
    _app._G["gpu_temp"].labels(gpu=gpu_label).set(_app.LATEST.get("temp", 0))
    _app._G["gpu_power"].labels(gpu=gpu_label).set(_app.LATEST.get("power", 0))

    # ── Host ─────────────────────────────────────────────────────────────────
    host = _app.LATEST.get("host") or {}
    _app._G["host_cpu"].set(host.get("cpu", 0))
    ram_total = host.get("ram_total") or 1
    ram_used  = host.get("ram_used", 0)
    _app._G["host_mem_used"].set(round(100 * ram_used / ram_total, 1))
    for disk in (host.get("disks") or []):
        _app._G["host_disk_used"].labels(mountpoint=disk["mount"]).set(disk.get("pct", 0))

    # ── Model VRAM ───────────────────────────────────────────────────────────
    for entry in (_app.LATEST.get("models") or []):
        vram = entry.get("vram")
        if vram is not None:
            _app._G["model_vram"].labels(server=entry.get("service", "?"),
                                    model=entry.get("model", "?")).set(vram)

    # ── Installed-models registry, by provider (#219) — no new I/O: counts the
    # already-sampled model_catalog, same source the /api/models endpoint merges.
    provider_counts = {}
    for entry in (_app.LATEST.get("model_catalog") or []):
        p = entry.get("provider") or "unknown"
        provider_counts[p] = provider_counts.get(p, 0) + 1
    for provider, count in provider_counts.items():
        _app._G["models_installed"].labels(provider=provider).set(count)

    # ── Docker _app.containers ────────────────────────────────────────────────────
    docker = _app.HEALTH.get("docker") or {}
    for ct in (docker.get("containers") or []):
        name  = ct.get("name", "?")
        state = ct.get("state", "unknown")
        _app._G["container_state"].labels(name=name, state=state).set(1)

    # ── Systemd units ────────────────────────────────────────────────────────
    systemd = _app.HEALTH.get("systemd") or {}
    for svc in (systemd.get("services") or []):
        unit   = svc.get("name", "?")
        active = svc.get("active", "unknown")
        _app._G["systemd_unit"].labels(unit=unit, state=active).set(
            1 if active == "active" else 0)

    return Response(_app.generate_latest(), mimetype=_app.CONTENT_TYPE_LATEST)


@bp.route("/api/hub/pubkey")
def api_hub_pubkey():
    import app as _app
    return jsonify({"pubkey": _app.get_hub_pubkey()})


@bp.route("/api/disk_scan")
def api_disk_scan():
    import app as _app
    path = os.path.normpath(request.args.get("path") or "/")
    rescan = request.args.get("rescan") == "1"
    real = _app._safe_host_dir(path)
    if not real:
        return jsonify({"path": path, "state": "error", "error": f"not a readable directory: {path}"})
    with _app._DISK_SCAN_LOCK:
        ent = _app._DISK_SCAN.get(path)
        if ent and not rescan:
            if ent["state"] == "scanning":
                return jsonify({"path": path, "state": "scanning"})
            if ent["state"] == "done" and time.time() - ent["at"] < _app._DISK_SCAN_TTL:
                return jsonify({"path": path, **ent})
            if ent["state"] == "error" and time.time() - ent["at"] < 20:
                return jsonify({"path": path, **ent})
        _app._DISK_SCAN[path] = {"state": "scanning", "at": int(time.time())}
    threading.Thread(target=_app._disk_scan_worker, args=(path, real), daemon=True).start()
    return jsonify({"path": path, "state": "scanning"})


@bp.route("/api/backup")
def api_backup_download():
    import app as _app
    """Stream a consistent SQLite snapshot (VACUUM INTO) of the live database."""
    if _app._DB_MAINTENANCE:
        return jsonify({"ok": False, "error": "Database maintenance in progress."}), 503
    if not _app._data_dir_writable():
        return jsonify({"ok": False, "error": "Cannot write backup — mount a writable /data volume."}), 400
    tmp_path = None
    try:
        with _app.LOCK:
            fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix=".backup_", dir=_app._data_dir())
            os.close(fd)
            _app.db_backup.vacuum_into(_app.DB, tmp_path)
    except Exception as e:
        if tmp_path:
            try: os.unlink(tmp_path)
            except OSError: pass
        return jsonify({"ok": False, "error": "Backup failed: %s" % e}), 500

    @after_this_request
    def _cleanup_snapshot(resp):
        try: os.unlink(tmp_path)
        except OSError: pass
        return resp

    return send_file(tmp_path, mimetype="application/x-sqlite3", as_attachment=True,
                     download_name=_app.db_backup.backup_filename())


@bp.route("/api/backup/restore", methods=["POST"])
def api_backup_restore():
    import app as _app
    """Replace the live database with an uploaded backup snapshot."""
    if _app._DB_MAINTENANCE:
        return jsonify({"ok": False, "error": "Database maintenance already in progress."}), 503
    if not _app._data_dir_writable():
        return jsonify({"ok": False, "error": "Cannot restore — mount a writable /data volume."}), 400
    upload = request.files.get("backup")
    if not upload or not upload.filename:
        return jsonify({"ok": False, "error": "No backup file uploaded."}), 400

    upload_path = None
    try:
        fd, upload_path = tempfile.mkstemp(suffix=".db", prefix=".restore_upload_", dir=_app._data_dir())
        os.close(fd)
        upload.save(upload_path)
        ok, err = _app.db_backup.validate_backup(upload_path)
        if not ok:
            return jsonify({"ok": False, "error": err}), 400

        with _app.LOCK:
            _app._DB_MAINTENANCE = True
            try:
                if os.path.isfile(_app.DB_PATH):
                    shutil.copy2(_app.DB_PATH, "%s.pre-restore-%d.bak" % (_app.DB_PATH, int(time.time())))
                try:
                    _app.DB.close()
                except Exception as e:
                    # close can fail on already-closed conn; restore proceeds regardless
                    print(f"api/system db_restore DB.close() error (non-fatal): {e}", flush=True)
                _app.db_backup.remove_wal_sidecars(_app.DB_PATH)
                os.replace(upload_path, _app.DB_PATH)
                upload_path = None
                _app.db_backup.remove_wal_sidecars(_app.DB_PATH)
                _app.reopen_db()
            except Exception as e:
                try:
                    _app.reopen_db()
                except Exception as re:
                    print(f"api/system db_restore reopen_db error: {re}", flush=True)
                return jsonify({"ok": False, "error": "Restore failed: %s" % e}), 500
            finally:
                _app._DB_MAINTENANCE = False

        return jsonify({"ok": True,
                        "message": "Backup restored. History and settings have been reloaded."})
    finally:
        if upload_path:
            try: os.unlink(upload_path)
            except OSError: pass


@bp.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    import app as _app
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        # Empty string clears a setting; missing key leaves it unchanged.
        # Secrets pass through the "_set: false" sentinel from the UI as a way
        # to clear without revealing the current value.
        updates = {k: body[k] for k in body if k in _app.SETTING_DEFAULTS}
        err = (_app._validate_url_settings(updates) or _app._validate_email_settings(updates)
               or _app._validate_brief_settings(updates) or _app._validate_retention_settings(updates))
        if err:
            return jsonify({"ok": False, "error": err}), 400
        _app.save_settings(updates)
    return jsonify({"version": _app.VERSION, "settings": _app._public_settings()})


@bp.route("/api/update/app", methods=["POST"])
def api_update_app():
    import app as _app
    """Start the opt-in one-click self-update (detached docker:cli helper).
    400 = disabled / no update / not a compose deploy; 409 = already running;
    202 = job started. Gated by _app.ALLOW_SELF_UPDATE — off by default."""
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force"))
    code, payload = _app.start_self_update(force=force)
    return jsonify(payload), code


@bp.route("/api/update/app/status")
def api_update_app_status():
    import app as _app
    """Read back the self-update progress files from the data dir. Works even
    right after the restart — it just reads update_state.json + a tail of
    update.log. No state file yet → idle."""
    st = _app._read_update_state()
    if not st:
        return jsonify({"state": "idle"})
    st = dict(st)
    st["log"] = _app._tail_lines(_app._update_log_path(), 200)
    st["self_update_enabled"] = _app.ALLOW_SELF_UPDATE
    return jsonify(st)


@bp.route("/")
def index():
    import app as _app
    return _app.app.send_static_file("dashboard.html")


