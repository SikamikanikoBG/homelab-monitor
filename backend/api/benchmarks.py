"""backend/api/benchmarks.py — LLM Benchmark Lab routes + single-flight worker.

Unlike the rest of the monitor, benchmarking is an ACTIVE, opt-in operation: it
drives the ollama HTTP API to load models and run generations. Because the GPU
is a single shared resource, only one benchmark job runs at a time (single-flight,
like the disk-scan job). A job may queue several models; each becomes one
bench_runs row, measured in sequence with live progress.

Endpoints:
  GET  /api/bench/targets   models available to benchmark + GPU inventory + status
  GET  /api/bench           stored runs (+ live job status)
  GET  /api/bench/<id>      one run with all measured points
  POST /api/bench           start a job {models:[...], ctx_list?, gen_tokens?, num_gpu?}
  POST /api/bench/cancel     cooperatively cancel the running job
  DELETE /api/bench/<id>    delete a stored run + its points
"""
import json
import logging
import threading
import time
import urllib.request
import uuid

from flask import Blueprint, request, jsonify

from backend import bench
from backend.db.repos import bench as bench_repo

bp = Blueprint('benchmarks', __name__)

# Single-flight job state (mutated only under _JOB_LOCK).
_JOB_LOCK = threading.Lock()
_JOB = {"active": False, "cancel": False, "run_ids": [], "models": [],
        "current_model": None, "current_ctx": None, "done_models": 0,
        "total_models": 0, "started_at": None, "endpoint": None, "host": None,
        "error": None}

_GEN_TIMEOUT = 300     # base: a cold 30B load + generation can take a while
_GEN_TIMEOUT_MAX = 600  # cap so a single stuck generate can't freeze the slot for long
_PS_TIMEOUT = 6


def _gen_timeout(num_ctx):
    """Scale the per-generate timeout with context size: a big KV cache — especially
    one that spills to RAM — loads slowly, so the 128k/256k rungs shouldn't
    spuriously time out. ~+30s per 8k of context, capped."""
    try:
        extra = (int(num_ctx) // 8192) * 30
    except (TypeError, ValueError):
        extra = 0
    return min(_GEN_TIMEOUT_MAX, _GEN_TIMEOUT + extra)

# ── ephemeral GPU-pinned ollama (device selection) ────────────────────────────
# To benchmark specific card(s) — ollama has no per-request device choice — we
# spin up a throwaway ollama container pinned to the chosen GPU(s), reusing the
# existing models volume, on a scratch port; run the sweep against it; tear it
# down. The main ollama is never touched.
import os as _os
_BENCH_OLLAMA_IMAGE = _os.environ.get("BENCH_OLLAMA_IMAGE", "ollama/ollama:latest")
_BENCH_OLLAMA_VOLUME = _os.environ.get("BENCH_OLLAMA_VOLUME", "vol_ollama")
_BENCH_CONTAINER = "hlm-bench-ollama"


def _bench_enabled():
    import app as _app
    if str(__import__("os").environ.get("BENCH_ENABLED", "true")).strip().lower() in ("0", "false", "no", "off"):
        return False
    return _app.COPILOT_ENABLED


def _default_endpoint():
    import app as _app
    return _app.COPILOT_OLLAMA_URL


def _post_json(base_url, path, payload, timeout):
    """POST JSON to an arbitrary ollama base URL (URL, not ip/port). stdlib only."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base_url.rstrip("/") + path, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status >= 400:
            return None
        return json.loads(r.read().decode("utf-8", "replace"))


def _get_json(base_url, path, timeout):
    req = urllib.request.Request(base_url.rstrip("/") + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status >= 400:
                return {}
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        print(f"bench: GET {path} failed: {e}", flush=True)
        return {}


def _smi_snapshot():
    """Raw nvidia-smi CSV for per-card VRAM attribution. '' when no GPU."""
    import app as _app
    try:
        return _app.smi(["--query-gpu=index,name,memory.used,memory.total,power.draw",
                         "--format=csv,noheader,nounits"]) or ""
    except Exception as e:
        print(f"bench: nvidia-smi snapshot failed: {e}", flush=True)
        return ""


def _gpu_inventory():
    return bench.parse_smi_gpus(_smi_snapshot())


def _unload(base_url, model):
    """Ask ollama to release a model now (keep_alive=0). Best-effort."""
    try:
        _post_json(base_url, "/api/generate",
                   {"model": model, "prompt": "", "keep_alive": 0, "stream": False}, timeout=30)
    except Exception as e:
        print(f"bench: unload of {model} failed (harmless): {e}", flush=True)


def _job_snapshot():
    with _JOB_LOCK:
        j = dict(_JOB)
    j.pop("cancel", None)
    return j


def _native_ctx(model, endpoint):
    """Native context length for a model, read from the SAME endpoint the job will
    benchmark (POST /api/show). Best-effort — returns None on any failure, which
    just means the default context ladder is used without the model's own ceiling."""
    try:
        d = _post_json(endpoint, "/api/show", {"model": model}, timeout=8) or {}
        for k, v in (d.get("model_info") or {}).items():
            if k.endswith(".context_length"):
                return int(v) if str(v).isdigit() else (v if isinstance(v, int) else None)
    except Exception as e:
        print(f"bench: native ctx lookup for {model} failed: {e}", flush=True)
    return None


# ── ephemeral GPU-pinned ollama lifecycle ─────────────────────────────────────
def _free_port():
    """An OS-assigned free TCP port. Using a fresh port per job avoids the
    bind-time race where a just-removed container's host port is still in use."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _docker_rm_bench_container():
    """Remove any leftover scratch container, then wait until it's actually gone
    (a removed name lingers briefly, and reusing it would 409 on create)."""
    import app as _app
    try:
        _app._docker_req("DELETE", f"/containers/{_BENCH_CONTAINER}?force=1", timeout=20)
    except Exception as e:
        print(f"bench: cleanup of stale scratch container failed: {e}", flush=True)
    for _ in range(20):
        try:
            code, _raw = _app._docker_req("GET", f"/containers/{_BENCH_CONTAINER}/json", timeout=5)
            if code == 404:
                return
        except Exception:
            logging.debug("poll for scratch container removal failed", exc_info=True)
            return
        time.sleep(0.5)


def _ephemeral_ollama_up(devices, port=None):
    """Create + start an ollama container pinned to `devices` (GPU index strings),
    sharing the models volume, on host networking at a free port. Returns
    (id, base_url). Raises on failure. Caller MUST tear it down."""
    import app as _app
    port = port or _free_port()
    _docker_rm_bench_container()  # clear any stale one first, and wait until gone
    body = {
        "Image": _BENCH_OLLAMA_IMAGE,
        # Loopback only: the monitor reaches it via 127.0.0.1:<port> (host net),
        # so there's no reason to expose an unauthenticated ollama on all host
        # interfaces for the run's duration.
        "Env": [f"OLLAMA_HOST=127.0.0.1:{port}", "OLLAMA_MAX_LOADED_MODELS=1",
                "NVIDIA_DRIVER_CAPABILITIES=compute,utility"],
        "HostConfig": {
            "NetworkMode": "host",   # monitor is host-net too, so reach 127.0.0.1:port
            "Binds": [f"{_BENCH_OLLAMA_VOLUME}:/root/.ollama"],  # same models, read-shared
            "AutoRemove": False,     # we remove explicitly so errors stay inspectable
            "DeviceRequests": [{"Driver": "nvidia",
                                "DeviceIDs": [str(d) for d in devices],
                                "Capabilities": [["gpu"]]}],
        },
    }
    code, raw = _app._docker_req("POST", f"/containers/create?name={_BENCH_CONTAINER}", body=body, timeout=30)
    if code not in (200, 201):
        raise RuntimeError(f"create failed HTTP {code}: {raw[:200].decode('utf-8', 'replace')}")
    cid = (json.loads(raw) or {}).get("Id")
    code, raw = _app._docker_req("POST", f"/containers/{cid}/start", timeout=30)
    if code not in (204, 304):
        _ephemeral_ollama_down(cid)
        raise RuntimeError(f"start failed HTTP {code}: {raw[:200].decode('utf-8', 'replace')}")
    base_url = f"http://127.0.0.1:{port}"
    # Wait until the scratch ollama answers (it mounts existing models, so no pull).
    for _ in range(80):     # ~60s: cold container init on an old card can be slow
        if _get_json(base_url, "/api/version", 3):
            return cid, base_url
        time.sleep(0.75)
    _ephemeral_ollama_down(cid)
    raise RuntimeError("scratch ollama did not become ready in time")


def _ephemeral_ollama_down(cid):
    """Force-remove the scratch container. Never removes the (named) models volume."""
    import app as _app
    try:
        _app._docker_req("DELETE", f"/containers/{cid}?force=1", timeout=30)
    except Exception as e:
        print(f"bench: scratch container teardown failed: {e}", flush=True)


def _device_label(devices, gpus):
    """Human label for the chosen setup, e.g. 'RTX 3090' or 'P2000 + RTX 3090'."""
    if not devices:
        return "default (main ollama)"
    by_idx = {g["idx"]: (g.get("name") or f"GPU {g['idx']}") for g in (gpus or [])}
    names = [by_idx.get(int(d), f"GPU {d}") for d in devices]
    return " + ".join(names)


# ── worker ────────────────────────────────────────────────────────────────────
def _run_job(run_specs, cfg, host, endpoint, devices=None):
    import app as _app
    cid = None
    if devices:
        try:
            cid, endpoint = _ephemeral_ollama_up(devices)
        except Exception as e:
            print(f"bench: could not start GPU-pinned ollama: {e}", flush=True)
            for spec in run_specs:
                with _app.LOCK:
                    bench_repo.finish_run(spec["id"], "error", None, int(time.time()),
                                          None, None, None,
                                          f"could not start GPU-pinned ollama: {e}"[:300], conn=_app.DB)
                with _JOB_LOCK:
                    _JOB["done_models"] += 1
            with _JOB_LOCK:
                _JOB["active"] = False
                _JOB["current_model"] = None
                _JOB["current_ctx"] = None
            return
    try:
        _run_job_inner(run_specs, cfg, host, endpoint)
    finally:
        if cid:
            _ephemeral_ollama_down(cid)
        with _JOB_LOCK:
            _JOB["active"] = False
            _JOB["current_model"] = None
            _JOB["current_ctx"] = None


def _run_job_inner(run_specs, cfg, host, endpoint):
    import app as _app
    ctx_cost = None
    try:
        ctx_cost = _app._cost_ctx()
    except Exception:
        ctx_cost = None

    def gen_fn(model, prompt, num_ctx, num_predict, num_gpu, keep_alive):
        opts = {"num_ctx": int(num_ctx), "num_predict": int(num_predict)}
        if num_gpu is not None:
            opts["num_gpu"] = int(num_gpu)
        payload = {"model": model, "prompt": prompt, "stream": False,
                   "keep_alive": keep_alive, "options": opts}
        return _post_json(endpoint, "/api/generate", payload, _gen_timeout(num_ctx))

    def ps_fn():
        return _get_json(endpoint, "/api/ps", _PS_TIMEOUT)

    def should_cancel():
        with _JOB_LOCK:
            return _JOB["cancel"]

    for spec in run_specs:
        rid = spec["id"]
        model = spec["model"]
        if should_cancel():
            with _app.LOCK:
                bench_repo.set_status(rid, "canceled", conn=_app.DB)
            with _JOB_LOCK:
                _JOB["done_models"] += 1   # keep progress honest through a cancel
            continue
        with _JOB_LOCK:
            _JOB["current_model"] = model
            _JOB["current_ctx"] = None
        started = int(time.time())
        with _app.LOCK:
            bench_repo.update_run("status=?, started_at=?", ["running", started, rid], conn=_app.DB)

        def on_point(pt, _rid=rid):
            with _JOB_LOCK:
                _JOB["current_ctx"] = pt.get("ctx")
            gpus_json = json.dumps(pt.get("gpus") or [], separators=(",", ":"))
            with _app.LOCK:
                bench_repo.insert_point(
                    _rid, pt.get("ctx"), pt.get("num_gpu"), pt.get("gen_tps"),
                    pt.get("prompt_tps"), pt.get("load_ms"), pt.get("ttft_ms"),
                    pt.get("total_ms"), pt.get("eval_count"), pt.get("prompt_eval_count"),
                    pt.get("vram_mb"), pt.get("ram_offload_mb"), pt.get("total_size_mb"),
                    pt.get("gpu_fraction"), pt.get("fit"), gpus_json,
                    pt.get("ok"), pt.get("err"), conn=_app.DB)

        try:
            meta = {"ctx": _native_ctx(model, endpoint)}
            points, summary = bench.run_model_benchmark(
                model, cfg, gen_fn, ps_fn, _smi_snapshot,
                on_point=on_point, should_cancel=should_cancel, meta=meta)
            ended = int(time.time())
            # Enrich summary with a GPU-placement note (multi-card boxes).
            inv = _gpu_inventory()
            landed = points[-1].get("gpus") if points else None
            summary["gpu_advice"] = bench.gpu_advice(landed, inv)
            e_kwh = cost = avg_w = None
            if ctx_cost is not None:
                try:
                    with _app.LOCK:
                        e_kwh, cost, avg_w, _peak = _app._run_cost_window(
                            _app.DB.cursor(), started, ended, ctx_cost)
                except Exception as e:
                    print(f"bench: cost pricing failed for run {rid}: {e}", flush=True)
            status = "canceled" if should_cancel() and not summary.get("ok_points") else "done"
            with _app.LOCK:
                bench_repo.finish_run(rid, status,
                                      json.dumps(summary, separators=(",", ":")),
                                      ended, e_kwh, cost, avg_w, None, conn=_app.DB)
        except Exception as e:
            with _app.LOCK:
                bench_repo.finish_run(rid, "error", None, int(time.time()),
                                      None, None, None, str(e)[:300], conn=_app.DB)
        finally:
            _unload(endpoint, model)
            with _JOB_LOCK:
                _JOB["done_models"] += 1


# ── routes ────────────────────────────────────────────────────────────────────
@bp.route("/api/bench/targets")
def bench_targets():
    import app as _app
    if not _bench_enabled():
        return jsonify({"enabled": False, "reachable": False, "models": [], "gpus": []})
    models, reachable = _app._model_registry()
    # Only benchmarkable providers (ollama) — the on-disk catalogue.
    out = [{"name": m["name"], "family": m.get("family"), "param_size": m.get("param_size"),
            "quant": m.get("quant"), "size_gb": m.get("size_gb"), "loaded": m.get("loaded")}
           for m in models]
    return jsonify({"enabled": True, "reachable": reachable, "endpoint": _default_endpoint(),
                    "models": out, "gpus": _gpu_inventory(),
                    "ctx_ladder": list(bench.DEFAULT_CTX_LADDER),
                    "default_gen_tokens": bench.DEFAULT_GEN_TOKENS,
                    # Device selection spins up a scratch container → needs controls on.
                    "device_select": bool(_app.ENABLE_CONTROLS),
                    "job": _job_snapshot()})


@bp.route("/api/bench")
def bench_list():
    import app as _app
    rng = request.args.get("range", "30d")
    span = _app.RANGES.get(rng, 2592000)
    now = int(time.time())
    since = 0 if span is None else now - span
    model = request.args.get("model")
    ctx = _cost_ctx_safe()
    out = []
    with _app.LOCK:
        rows = bench_repo.list_runs(since, now, model=model or None, conn=_app.DB)
    for r in rows:
        out.append(_run_row_to_dict(r))
    return jsonify({"range": rng, "currency": (ctx or {}).get("currency", "$"),
                    "runs": out, "job": _job_snapshot(), "enabled": _bench_enabled()})


@bp.route("/api/bench/<rid>")
def bench_get(rid):
    import app as _app
    with _app.LOCK:
        r = bench_repo.get_run(rid, conn=_app.DB)
        if not r:
            return jsonify({"error": "unknown benchmark"}), 404
        pts = bench_repo.get_points(rid, conn=_app.DB)
    d = _run_row_to_dict(r)
    d["points"] = [_point_row_to_dict(p) for p in pts]
    return jsonify(d)


@bp.route("/api/bench", methods=["POST"])
def bench_start():
    import app as _app
    if not _bench_enabled():
        return jsonify({"ok": False, "error": "benchmarking is disabled (BENCH_ENABLED/COPILOT_ENABLED)"}), 400
    # Cheap early-out: if a job is already running, reject now — before paying the
    # nvidia-smi inventory below. This is advisory; the atomic check-and-set at the
    # reservation point stays authoritative for single-flight.
    with _JOB_LOCK:
        if _JOB["active"]:
            return jsonify({"ok": False, "error": "a benchmark is already running",
                            "job": {k: v for k, v in _JOB.items() if k != "cancel"}}), 409
    # Validate the request BEFORE touching the single-flight slot, so a bad request
    # never has to roll the slot back.
    body = request.get_json(silent=True) or {}
    models = body.get("models") or []
    if isinstance(models, str):
        models = [models]
    models = [m for m in ({str(x).strip() for x in models}) if m][:bench.MAX_MODELS_PER_JOB]
    if not models:
        return jsonify({"ok": False, "error": "no models selected"}), 400
    # Optional GPU device selection: benchmark specific card(s) via a throwaway
    # pinned ollama container. Empty -> use the main ollama as configured.
    gpus = _gpu_inventory()
    valid_idx = {g["idx"] for g in gpus}
    raw_devices = body.get("devices") or []
    if isinstance(raw_devices, (str, int)):
        raw_devices = [raw_devices]
    devices = sorted({int(d) for d in raw_devices
                      if str(d).lstrip("-").isdigit() and int(d) in valid_idx})
    if devices and not _app.ENABLE_CONTROLS:
        return jsonify({"ok": False, "error": "GPU selection needs controls enabled (ENABLE_CONTROLS=1)"}), 400
    cfg = {
        "ctx_list": body.get("ctx_list") or None,
        "gen_tokens": _clamp_int(body.get("gen_tokens"), bench.DEFAULT_GEN_TOKENS, 16, 1024),
        "prompt_tokens": _clamp_int(body.get("prompt_tokens"), bench.DEFAULT_PROMPT_TOKENS, 32, 4096),
        "num_gpu": _opt_int(body.get("num_gpu")),
        "devices": devices,
        "device_label": _device_label(devices, gpus),
    }
    host = _app._clip(body.get("host") or "local", 64)
    endpoint = _default_endpoint()
    now = int(time.time())
    # Reserve the single-flight slot atomically: check-and-set under ONE lock
    # acquisition, so two concurrent POSTs can't both pass the "is one running?"
    # gate. The GPU is a shared resource — this guarantee is the whole point.
    with _JOB_LOCK:
        if _JOB["active"]:
            return jsonify({"ok": False, "error": "a benchmark is already running",
                            "job": {k: v for k, v in _JOB.items() if k != "cancel"}}), 409
        _JOB.update({"active": True, "cancel": False, "run_ids": [], "models": [],
                     "current_model": None, "current_ctx": None, "done_models": 0,
                     "total_models": len(models), "started_at": now,
                     "endpoint": endpoint, "host": host, "error": None})
    # We now own the slot; any failure below must release it or it wedges "active".
    try:
        reg, _ = _app._model_registry()
        reg_by = {m["name"]: m for m in reg}
        # Reuse the single GPU snapshot taken during validation (an nvidia-smi
        # subprocess) — identical for every model, and calling it again inside
        # _app.LOCK would stall the sampler and every other route.
        gpu_json = json.dumps(gpus, separators=(",", ":"))
        cfg_json = json.dumps(cfg, separators=(",", ":"))
        run_specs = []
        with _app.LOCK:
            for model in models:
                rid = uuid.uuid4().hex
                m = reg_by.get(model, {})
                bench_repo.insert_run(
                    rid, host, endpoint, model, m.get("family"), m.get("param_size"),
                    m.get("quant"), m.get("size_bytes"), "queued",
                    cfg_json, gpu_json, now, None, conn=_app.DB)
                run_specs.append({"id": rid, "model": model})
        with _JOB_LOCK:
            _JOB["run_ids"] = [s["id"] for s in run_specs]
            _JOB["models"] = [s["model"] for s in run_specs]
            _JOB["total_models"] = len(run_specs)
        threading.Thread(target=_run_job, args=(run_specs, cfg, host, endpoint, devices), daemon=True).start()
    except Exception as e:
        with _JOB_LOCK:
            _JOB["active"] = False
        print(f"bench: failed to launch job: {e}", flush=True)
        return jsonify({"ok": False, "error": "failed to launch benchmark"}), 500
    return jsonify({"ok": True, "run_ids": [s["id"] for s in run_specs], "job": _job_snapshot()})


@bp.route("/api/bench/cancel", methods=["POST"])
def bench_cancel():
    with _JOB_LOCK:
        if not _JOB["active"]:
            return jsonify({"ok": False, "error": "no benchmark running"}), 400
        _JOB["cancel"] = True
    return jsonify({"ok": True})


@bp.route("/api/bench/<rid>", methods=["DELETE"])
def bench_delete(rid):
    import app as _app
    with _JOB_LOCK:
        if _JOB["active"] and rid in _JOB["run_ids"]:
            return jsonify({"ok": False, "error": "benchmark is running — cancel first"}), 409
    with _app.LOCK:
        n = bench_repo.delete_run_with_points(rid, conn=_app.DB)
    return (jsonify({"ok": True}) if n else (jsonify({"ok": False, "error": "unknown benchmark"}), 404))


# ── helpers ───────────────────────────────────────────────────────────────────
def _clamp_int(v, default, lo, hi):
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, iv))


def _opt_int(v):
    if v in (None, "", "auto"):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _cost_ctx_safe():
    import app as _app
    try:
        return _app._cost_ctx()
    except Exception as e:
        print(f"bench: cost context unavailable: {e}", flush=True)
        return None


def _run_row_to_dict(r):
    (rid, host, endpoint, model, family, param_size, quant, size_bytes, status,
     config, summary, gpu, error, created_at, started_at, ended_at,
     energy_kwh, cost, avg_w) = r
    return {
        "id": rid, "host": host, "endpoint": endpoint, "model": model,
        "family": family, "param_size": param_size, "quant": quant,
        "size_bytes": size_bytes, "size_gb": round(size_bytes / 1073741824, 2) if size_bytes else None,
        "status": status, "config": _safe(config), "summary": _safe(summary),
        "gpu": _safe(gpu), "error": error, "created_at": created_at,
        "started_at": started_at, "ended_at": ended_at,
        "duration": (ended_at - started_at) if (ended_at and started_at) else None,
        "energy_kwh": energy_kwh, "cost": cost, "avg_w": avg_w,
    }


def _point_row_to_dict(p):
    (ctx, num_gpu, gen_tps, prompt_tps, load_ms, ttft_ms, total_ms, eval_count,
     prompt_eval_count, vram_mb, ram_offload_mb, total_size_mb, gpu_fraction,
     fit, gpus, ok, err) = p
    return {
        "ctx": ctx, "num_gpu": num_gpu, "gen_tps": gen_tps, "prompt_tps": prompt_tps,
        "load_ms": load_ms, "ttft_ms": ttft_ms, "total_ms": total_ms,
        "eval_count": eval_count, "prompt_eval_count": prompt_eval_count,
        "vram_mb": vram_mb, "ram_offload_mb": ram_offload_mb, "total_size_mb": total_size_mb,
        "gpu_fraction": gpu_fraction, "fit": fit, "gpus": _safe(gpus) or [],
        "ok": bool(ok), "err": err,
    }


def _safe(txt):
    # Narrow catch (not broad): our own stored JSON — a malformed blob is absent.
    try:
        return json.loads(txt) if txt else None
    except (ValueError, TypeError):
        return None
