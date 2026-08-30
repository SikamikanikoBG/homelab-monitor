"""backend/api/uptime_api.py — uptime_api routes (Phase 3.4)."""
from flask import Blueprint, request, jsonify, Response, send_file, send_from_directory, after_this_request, g, abort
import time

bp = Blueprint('uptime_api', __name__)


@bp.route("/api/uptime", methods=["GET", "POST"])
def api_uptime():
    import app as _app
    """List uptime checks with their live state (GET) or create one (POST).
    GET is always 200 (an empty list before any are added); POST returns a clean
    400 with a human error on invalid input."""
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        cid, err = _app.create_uptime_check(body)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        return jsonify({"ok": True, "id": cid}), 201
    requested_range = request.args.get("range", "24h")
    window = _app.RANGES.get(requested_range, _app.RANGES["24h"])
    # The overview currently requires a finite window; report the fallback for
    # unknown values and the `all` range instead of mislabeling the response.
    if window is None:
        requested_range, window = "24h", _app.RANGES["24h"]
    return jsonify({**_app.uptime_overview(window), "range": requested_range})


@bp.route("/api/uptime/<cid>", methods=["PATCH", "DELETE"])
def api_uptime_one(cid):
    import app as _app
    """Update (full edit or a quick enabled toggle) or delete one check."""
    if request.method == "DELETE":
        return (jsonify({"ok": True}) if _app.delete_uptime_check(cid)
                else (jsonify({"ok": False, "error": "not found"}), 404))
    body = request.get_json(silent=True) or {}
    ok, err = _app.update_uptime_check(cid, body)
    if ok:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": err}), (404 if err == "not found" else 400)


@bp.route("/api/maintenance", methods=["GET", "POST"])
def api_maintenance():
    import app as _app
    """List maintenance windows (GET) or create one (POST)."""
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        label = (body.get("label") or "").strip()
        if not label:
            return jsonify({"ok": False, "error": "label is required"}), 400
        start_ts = body.get("start_ts")
        end_ts   = body.get("end_ts")
        if not start_ts or not end_ts:
            return jsonify({"ok": False, "error": "start_ts and end_ts are required"}), 400
        try:
            start_ts = int(start_ts)
            end_ts = int(end_ts)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "start_ts and end_ts must be numeric"}), 400
        if end_ts <= start_ts:
            return jsonify({"ok": False, "error": "end_ts must be after start_ts"}), 400
        recurrence = body.get("recurrence") or None
        if recurrence and recurrence not in ("daily", "weekly"):
            return jsonify({"ok": False, "error": "recurrence must be daily, weekly, or null"}), 400
        if recurrence == "daily" and (end_ts - start_ts) > 86400:
            return jsonify({"ok": False, "error": "daily recurrence window cannot exceed 24h"}), 400
        if recurrence == "weekly" and (end_ts - start_ts) > 604800:
            return jsonify({"ok": False, "error": "weekly recurrence window cannot exceed 7d"}), 400
        wid = _app.create_maintenance_window(
            label=label,
            kind=body.get("kind") or "*",
            pattern=body.get("pattern") or "*",
            start_ts=start_ts,
            end_ts=end_ts,
            recurrence=recurrence,
            note=body.get("note"),
        )
        return jsonify({"ok": True, "id": wid}), 201
    return jsonify(_app.list_maintenance_windows())


@bp.route("/api/maintenance/<wid>", methods=["DELETE"])
def api_maintenance_one(wid):
    import app as _app
    """Delete a maintenance window."""
    _app.delete_maintenance_window(wid)
    return jsonify({"ok": True})


@bp.route("/api/brief/preview")
def api_brief_preview():
    import app as _app
    """Render the current brief as HTML (the Preview button opens this in a tab)."""
    theme = request.args.get("theme") or _app.get_settings().get("brief_theme", "dark")
    html, _, _, _ = _app.render_brief("light" if theme == "light" else "dark")
    return Response(html, mimetype="text/html; charset=utf-8")


@bp.route("/api/brief/test", methods=["POST"])
def api_brief_test():
    import app as _app
    """Render + deliver a brief now to the chosen channel (Send-test button)."""
    s = _app.get_settings()
    body = request.get_json(silent=True) or {}
    ch = (body.get("channel") or s.get("brief_channel") or "").strip()
    if ch not in _app._BRIEF_CHANNELS:
        return jsonify({"ok": False, "error": "Pick a channel for the daily brief first."}), 400
    if not _app._brief_channel_ready(s, ch):
        return jsonify({"ok": False, "error": f"The {ch} channel isn't configured yet — fill it in above under Alerts."}), 400
    try:
        _app.send_brief(s, ch)
    except Exception as e:
        print(f"api/uptime_api send_brief error: {e}", flush=True)
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True, "channel": ch})


@bp.route("/api/public-status")
def api_public_status():
    import app as _app
    if not _app._public_status_enabled():
        abort(404)
    cfg = _app.get_settings()
    gpu_avail = _app.LATEST.get("gpu_avail")
    now = {"gpu": {"util": _app.LATEST.get("util"), "mem_used": _app.LATEST.get("mem_used"),
                   "mem_total": (_app.LATEST.get("mem_total") or 24576) if gpu_avail else 0,
                   "power": _app.LATEST.get("power"), "temp": _app.LATEST.get("temp"),
                   "available": bool(gpu_avail),
                   "gpus": _app.LATEST.get("gpus") or [],
                   "extra": _app.LATEST.get("gpu_extra") or {}},
           "host": _app.LATEST.get("host") or {}}
    docker  = _app.HEALTH.get("docker")  or {"available": False, "reason": "warming up", "containers": [], "summary": {"total": 0, "running": 0, "problems": 0}}
    systemd = _app.HEALTH.get("systemd") or {"available": False, "reason": "warming up", "services": [], "summary": {}}
    cards = _app.build_overview(now, docker, systemd)
    safe_cards = [{k: v for k, v in c.items() if k in ("key", "label", "status", "metric", "detail")} for c in cards]
    _now = int(time.time())
    monitors = _app._public_monitors(_now)
    return jsonify({
        "lab_name": cfg.get("lab_name", "My HomeLab"),
        "lab_emoji": cfg.get("lab_emoji", "🏠"),
        "overview": safe_cards,
        "monitors": monitors,
        "monitors_summary": _app._public_monitors_summary(monitors),
        "incidents": _app._public_incident_feed(monitors, _now),
        "status": _app._public_overall_status(safe_cards, monitors),
    })


@bp.route("/api/public-status/<cid>")
def api_public_status_one(cid):
    import app as _app
    if not _app._public_status_enabled():
        abort(404)
    detail = _app._public_status_detail(cid, int(time.time()))
    if detail is None:
        abort(404)
    return jsonify(detail)


@bp.route("/public/<cid>")
@bp.route("/public")
def public_status(cid=None):
    import app as _app
    if not _app._public_status_enabled():
        abort(404)
    return send_from_directory("static", "public.html")

