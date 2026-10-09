"""backend/api/gpu.py — gpu routes (Phase 3.4)."""
from flask import Blueprint, request, jsonify, Response, send_file, send_from_directory, after_this_request, g, abort
import time

from backend.db.repos import samples as samples_repo

bp = Blueprint('gpu', __name__)


@bp.route("/api/sessions")
def api_sessions():
    import app as _app
    """GPU activity sessions over the range — contiguous GPU-busy periods rebuilt
    from the power/util history. Plus the live training processes detected on the
    hub right now (_app.LATEST['training']). Powers the Experiments tab."""
    s = _app.get_settings()
    try:
        price = float(s.get("kwh_price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    rng = request.args.get("range", "7d")
    span = _app.RANGES.get(rng, 604800)
    now = int(time.time())
    with _app.LOCK:
        since = (samples_repo.min_ts(conn=_app.DB) or now) if span is None else now - span
        rows = samples_repo.sessions_since(since, conn=_app.DB)
    sessions = _app._gpu_sessions(rows, _app.INTERVAL, price=price)[:50]
    tot_energy = round(sum(x["energy_kwh"] for x in sessions), 3)
    return jsonify({
        "range": rng, "currency": s.get("currency") or "$",
        "price": price, "kwh_price": price,
        "active_pct": _app._ACTIVE_UTIL,
        "sessions": sessions,
        "totals": {"count": len(sessions), "energy_kwh": tot_energy,
                   "cost": round(tot_energy * price, 2),
                   "active_hours": round(sum(x["duration"] for x in sessions) / 3600.0, 1)},
        "training": _app.LATEST.get("training") or [],
        "devtools": _app.LATEST.get("devtools") or [],
    })


@bp.route("/api/models")
def api_models():
    import app as _app
    """The Model Registry: the full inventory of models available across the
    fleet -- this host's ollama on-disk catalogue (GET /api/tags, size/quant/param
    detail) merged with every other recognised AI server's model list on this
    host (#219: vLLM, llama.cpp, LM Studio, ComfyUI, InvokeAI, ...) PLUS every
    registered remote host's own catalog (Ollama slice so far -- probe.py polls
    each remote's ollama the same way, over SSH, no direct network reachability
    to the remote's port required). Cross-referenced with what's loaded right
    now so the UI can flag resident models + their live VRAM, grouped by host
    then provider.

    Always 200, graceful-degrade, never 500, no secret leak (we echo a `reachable`
    bool, never the URL/creds). Ollama half cached ~45s so a busy tab can't hammer
    it; the rest rides the existing sampler's cached model_catalog (no extra I/O)
    plus whatever's already sitting in HOST_DATA from the normal host-poll cycle
    (also no extra I/O -- this route never triggers a fresh remote poll itself).
    /api/tags is polled outside any held _app.LOCK."""
    ollama_models, reachable = _app._model_registry()
    catalog = list(_app.LATEST.get("model_catalog") or [])
    with _app.HOST_DATA_LOCK:
        remote_items = list(_app.HOST_DATA.items())
    for _name, entry in remote_items:
        remote_catalog = (entry.get("data") or {}).get("model_catalog")
        if remote_catalog:
            # Key every remote entry by its REGISTERED fleet name — that is what the
            # host selector sends and what the UI filters on. The probe's own
            # socket.gethostname() may differ (registered "Work" vs "DESKTOP-…"),
            # which silently hid that host's models.
            for m in remote_catalog:
                if isinstance(m, dict):
                    catalog.append(dict(m, host=_name))
                elif isinstance(m, (list, tuple)):
                    # An older probe.ps1 double-nested the array ([[...]], #292);
                    # never let one malformed remote 500 the whole registry.
                    catalog.extend(dict(x, host=_name) for x in m if isinstance(x, dict))
    models = _app._merge_registry(ollama_models, catalog)
    return jsonify({
        "enabled": _app.COPILOT_ENABLED,
        "ollama_reachable": reachable,
        "models": models,
        "totals": _app._registry_totals(models),
        "providers": sorted({m["provider"] for m in models}),
    })


@bp.route("/api/ai/now")
def api_ai_now():
    import app as _app
    """Light live snapshot for the AI Models tab's fast poll: loaded models with
    the VRAM/RAM/ctx state (throttled on-demand ollama re-probe, ~3s TTL — see
    ai_models_now) plus the sampler's latest meta/serving/callers. No DB, no
    _app.LOCK, so it's safe to poll every few seconds."""
    models, at = _app.ai_models_now()
    return jsonify({"ts": _app.LATEST.get("ts"), "probed_at": int(at), "models": models,
                    "model_meta": _app.LATEST.get("model_meta") or {},
                    "serving": _app.LATEST.get("serving") or [],
                    "callers": _app.LATEST.get("callers") or []})


