"""backend/api/experiments.py — experiments routes (Phase 3.4)."""
from flask import Blueprint, request, jsonify, Response, send_file, send_from_directory, after_this_request, g, abort
import time
import uuid

bp = Blueprint('experiments', __name__)

# decorator defined in _app.app.py — must import at module level (used as @require_api_key)
from backend.auth import require_api_key  # no circular import — backend.auth uses lazy _app
from backend.db.repos import auth as auth_repo
from backend.auth import sessions, store as auth_store
from backend.auth.marks import sensitive
from backend.db.repos import experiments as exp_repo

KEY_SCOPES = ("push", "read", "admin")
ADMIN_KEY_DAYS = 90    # an admin key is a root key; it should not live forever by default


@bp.route("/api/integration/keys", methods=["GET", "POST"])
@sensitive
def api_keys_route():
    import app as _app
    """GET -> {keys:[{id,name,prefix,created_at,expires_at,last_used_at,expired,runs}]}
    (never the secret). POST {name, expires_in_days?} -> {id, key} (key revealed once)."""
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        days = body.get("expires_in_days")
        try:
            days = int(days) if days not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            days = None
        if days is not None and days <= 0:
            days = None
        scope = body.get("scope") or "push"
        if scope not in KEY_SCOPES:
            return jsonify({"ok": False, "error": "scope must be one of: %s" % ", ".join(KEY_SCOPES)}), 400
        if scope != "push" and not sessions.password_set():
            # A read or admin key on an open dashboard would outlive the day a
            # password is set; only push keys can be minted before that.
            return jsonify({"ok": False, "error": "password_required",
                            "message": "Set a dashboard password before creating read or admin keys."}), 403
        if scope == "admin" and days is None:
            days = ADMIN_KEY_DAYS
        kid, key = _app._create_api_key(_app._clip(body.get("name") or "key", 128), days)
        if scope != "push":
            with _app.LOCK:
                auth_store.set_key_scope(kid, scope, _app.DB)
        return jsonify({"ok": True, "id": kid, "key": key})
    now = int(time.time())
    with _app.LOCK:
        rows = auth_repo.list_all(conn=_app.DB)
        counts = dict(auth_repo.count_runs_by_key(conn=_app.DB))
    keys = [{"id": kid, "name": name, "prefix": prefix, "created_at": created,
             "expires_at": exp, "last_used_at": used, "expired": bool(exp and exp < now),
             "runs": counts.get(kid, 0)}
            for (kid, name, prefix, created, exp, used) in rows]
    if sessions.password_set():
        # Scopes only exist once there is a login; without one the listing
        # stays exactly what it always was.
        with _app.LOCK:
            extra = auth_store.key_scopes(_app.DB)
        for k in keys:
            k["scope"], k["last_ip"] = extra.get(k["id"], ("push", None))
    return jsonify({"keys": keys, "has_key": bool(keys)})


@bp.route("/api/integration/keys/<kid>", methods=["DELETE"])
def api_keys_delete(kid):
    import app as _app
    """Revoke (remove) a key. Runs it pushed are kept; they just lose live attribution."""
    with _app.LOCK:
        rowcount = auth_repo.delete(kid, conn=_app.DB)
    return (jsonify({"ok": True}) if rowcount
            else (jsonify({"ok": False, "error": "unknown key"}), 404))


@bp.route("/api/runs", methods=["POST"])
@require_api_key
def api_runs_create():
    import app as _app
    body = request.get_json(silent=True) or {}
    source = (body.get("source") or "api").lower()
    if source not in _app.RUN_SOURCES:
        source = "api"
    now = int(time.time())
    rid = (body.get("id") or uuid.uuid4().hex)[:64]
    try:
        params = _app._json_field(body.get("params"), _app.MAX_RUN_JSON)
        tags = _app._json_field(body.get("tags"), _app.MAX_RUN_JSON)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 413
    with _app.LOCK:
        exp_repo.insert_run(
            rid, _app._clip(body.get("name") or "run", _app.MAX_RUN_FIELD), source, "running",
            int(body.get("started_at") or now), None, _app._clip(body.get("host"), 256),
            params, tags, _app._clip(body.get("notes"), _app.MAX_RUN_FIELD), now, None, now,
            getattr(g, "api_key_id", None), conn=_app.DB)
    return jsonify({"ok": True, "id": rid})


@bp.route("/api/runs/<rid>", methods=["PATCH"])
@require_api_key
def api_runs_update(rid):
    import app as _app
    body = request.get_json(silent=True) or {}
    sets, args = ["heartbeat_at=?"], [int(time.time())]
    if body.get("status") in _app.RUN_STATUS:
        sets.append("status=?"); args.append(body["status"])
    if body.get("ended_at"):
        sets.append("ended_at=?"); args.append(int(body["ended_at"]))
    for f, n in (("name", _app.MAX_RUN_FIELD), ("notes", _app.MAX_RUN_FIELD)):
        if f in body:
            sets.append(f"{f}=?"); args.append(_app._clip(body[f], n))
    try:
        for f in ("params", "tags"):
            if f in body:
                sets.append(f"{f}=?"); args.append(_app._json_field(body[f], _app.MAX_RUN_JSON))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 413
    args.append(rid)
    with _app.LOCK:
        rowcount = exp_repo.update_run(','.join(sets), args, conn=_app.DB)
    return (jsonify({"ok": True}) if rowcount else (jsonify({"ok": False, "error": "unknown run"}), 404))


@bp.route("/api/runs/<rid>/metrics", methods=["POST"])
@require_api_key
def api_runs_metrics(rid):
    import app as _app
    body = request.get_json(silent=True) or {}
    pts = body.get("metrics")
    if pts is None and "key" in body:
        pts = [body]
    pts = pts or []
    if len(pts) > _app.MAX_METRICS_REQ:
        return jsonify({"ok": False, "error": f"max {_app.MAX_METRICS_REQ} points/request"}), 413
    now = int(time.time())
    rows = []
    for p in pts:
        try:
            rows.append((rid, int(p.get("ts") or now), int(p.get("step") or 0),
                         _app._clip(p["key"], 128), float(p["value"])))
        except (KeyError, TypeError, ValueError):
            continue
    with _app.LOCK:
        if not exp_repo.exists_run(rid, conn=_app.DB):
            return jsonify({"ok": False, "error": "unknown run"}), 404
        if rows:
            exp_repo.insert_metrics_and_update_heartbeat(rid, rows, now, conn=_app.DB)
    return jsonify({"ok": True, "logged": len(rows)})


@bp.route("/api/runs/<rid>/finish", methods=["POST"])
@require_api_key
def api_runs_finish(rid):
    import app as _app
    body = request.get_json(silent=True) or {}
    status = body.get("status") if body.get("status") in _app.RUN_STATUS else "finished"
    if status == "running":
        status = "finished"
    ended = int(body.get("ended_at") or time.time())
    with _app.LOCK:
        rowcount = exp_repo.update_run_status(rid, status, ended, ended, conn=_app.DB)
    return (jsonify({"ok": True, "id": rid, "status": status}) if rowcount
            else (jsonify({"ok": False, "error": "unknown run"}), 404))


@bp.route("/api/runs/<rid>", methods=["DELETE"])
def api_runs_delete(rid):
    import app as _app
    """Remove a run and its logged metrics. A same-origin browser management action
    (like deleting a host or an API key), so it's open on the LAN rather than
    key-gated — the key gates *ingest* (forgery from notebooks), not housekeeping."""
    with _app.LOCK:
        rowcount = exp_repo.delete_run_with_metrics(rid, conn=_app.DB)
    return (jsonify({"ok": True}) if rowcount
            else (jsonify({"ok": False, "error": "unknown run"}), 404))


@bp.route("/api/runs")
def api_runs_list():
    import app as _app
    ctx = _app._cost_ctx()
    rng = request.args.get("range", "7d"); span = _app.RANGES.get(rng, 604800)
    status = request.args.get("status")
    key_filter = request.args.get("key")
    now = int(time.time()); since = 0 if span is None else now - span
    out = []
    with _app.LOCK:
        key_names = dict(auth_repo.get_names(conn=_app.DB))
        for (rid, name, source, st, started, ended, host, params, tags, notes, key_id) in exp_repo.list_runs(since, now, status=status if status in _app.RUN_STATUS else None, key_id=key_filter or None, conn=_app.DB):
            e_kwh, cost, avg_w, peak_u = _app._run_cost_window(_app.DB.cursor(), started, ended, ctx)
            kv = dict(exp_repo.get_run_metrics_latest(rid, conn=_app.DB))
            out.append({"id": rid, "name": name, "source": source, "status": st,
                        "started_at": started, "ended_at": ended, "duration": (ended or now) - started,
                        "host": host, "params": _app._safe_json(params), "tags": _app._safe_json(tags), "notes": notes,
                        "key_id": key_id, "key_name": key_names.get(key_id),
                        "metrics_latest": kv, "energy_kwh": e_kwh, "cost": cost, "avg_w": avg_w, "peak_util": peak_u})
    return jsonify({"range": rng, "currency": ctx["currency"], "tariff_mode": ctx["mode"], "runs": out})


@bp.route("/api/runs/<rid>")
def api_runs_get(rid):
    import app as _app
    ctx = _app._cost_ctx()
    now = int(time.time())
    with _app.LOCK:
        r = exp_repo.get_run(rid, conn=_app.DB)
        if not r:
            return jsonify({"error": "unknown run"}), 404
        (rid, name, source, st, started, ended, host, params, tags, notes) = r
        end = ended or now
        metrics = {}
        for k, ts, step, v in exp_repo.get_run_metrics(rid, conn=_app.DB):
            d = metrics.setdefault(k, {"steps": [], "ts": [], "values": []})
            d["steps"].append(step); d["ts"].append(ts); d["values"].append(v)
        bk = max(_app.INTERVAL, round(max(1, end - started) / _app.MAX_POINTS))
        labels, power_w, util_pct = [], [], []
        for b, ap, au in exp_repo.get_run_power_buckets(started, end, bk, conn=_app.DB):
            labels.append(int(b)); power_w.append(round(ap or 0)); util_pct.append(round(au or 0))
        e_kwh, cost, avg_w, peak_u = _app._run_cost_window(_app.DB.cursor(), started, end, ctx)
    return jsonify({"id": rid, "name": name, "source": source, "status": st,
                    "started_at": started, "ended_at": ended, "duration": end - started, "host": host,
                    "params": _app._safe_json(params), "tags": _app._safe_json(tags), "notes": notes, "metrics": metrics,
                    "resource": {"labels": labels, "power_w": power_w, "util_pct": util_pct, "bucket_sec": bk},
                    "energy_kwh": e_kwh, "cost": cost, "avg_w": avg_w, "peak_util": peak_u,
                    "currency": ctx["currency"], "tariff_mode": ctx["mode"]})


@bp.route("/api/integration/mlflow/sync", methods=["GET", "POST"])
def api_mlflow_sync():
    import app as _app
    """GET -> reachability probe (green/red). POST -> sync now."""
    if not (_app.get_settings().get("mlflow_uri") or "").strip():
        return jsonify({"ok": False, "error": "no MLflow URI configured"}), 400
    if request.method == "POST":
        try:
            return jsonify({"ok": True, "synced": _app.sync_mlflow()})
        except Exception as e:
            print(f"api/experiments sync_mlflow error: {e}", flush=True)
            return jsonify({"ok": False, "error": str(e)[:200]}), 502
    try:
        _app._mlf("POST", "/api/2.0/mlflow/experiments/search", {"max_results": 1})
        return jsonify({"ok": True, "reachable": True})
    except Exception as e:
        print(f"api/experiments mlflow reachability check error: {e}", flush=True)
        return jsonify({"ok": False, "reachable": False, "error": str(e)[:200]}), 502


