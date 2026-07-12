"""
backend/collectors — background worker functions extracted from app.py (Phase 3.2).

Each function uses a lazy ``import app as _app`` so that module-level globals
(_app.LATEST, _app.HEALTH, DB, _app.LOCK, …) are resolved at call time, avoiding circular
imports. Thread-start lines remain in app.py and resolve via re-exports.

Phase 4.3 — one thread per worker, watchdog detects stalls
===========================================================
Each worker already runs in its own daemon thread (started in app.py), so a stalled
worker cannot block any of the others.  This module adds:

  * ``_heartbeat(name, interval)`` — called by each worker at the start of every
    cycle; records a monotonic timestamp in ``_HEARTBEATS``.
  * ``watchdog()`` — a daemon thread that wakes every ``check_interval`` seconds and
    logs any worker whose last heartbeat is older than 2× its declared sleep interval.

Thread-safety: ``_HEARTBEAT_LOCK`` guards ``_HEARTBEATS``.  It is never held while
calling into app code, so there is no lock-ordering hazard with app.py's LOCK or
_NOTIFIER_LOCK.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from backend.db.repos.samples import rollup_now as _rollup_now
from backend.db.repos.samples import rollup_net_now as _rollup_net_now
from backend._heartbeat import heartbeat as _heartbeat, get_heartbeats as _get_heartbeats
from backend.probes import _match_probe, _match_probe_key, probe_models

# Re-export so callers can do `from backend.collectors import _HEARTBEATS, _HEARTBEAT_LOCK`
# (used by tests that import the heartbeat module directly for isolation assertions).
from backend._heartbeat import _HEARTBEATS, _HEARTBEAT_LOCK  # noqa: F401


def watchdog(check_interval: float = 10.0) -> None:  # pragma: no cover
    """Daemon loop: log workers that haven't heartbeated within 2× their interval.

    Runs in its own daemon thread; never raises so it survives indefinitely.
    Exceptions in worker code cause the worker loop to catch-and-continue, so
    the heartbeat simply stops updating — the watchdog detects that gap.
    """
    while True:
        time.sleep(check_interval)
        now = time.monotonic()
        for name, info in _get_heartbeats().items():
            deadline = info["ts"] + info["interval"] * 2
            if now > deadline:
                overdue = now - deadline
                print(
                    f"[watchdog] WARNING: worker '{name}' overdue by "
                    f"{overdue:.1f}s (interval={info['interval']}s)",
                    flush=True,
                )


# ── Background workers ───────────────────────────────────────────────────────

def host_poller():
    import app as _app
    """Loop: probe every registered host whose last Test was healthy. Hosts are
    polled *concurrently* so one slow/timing-out remote can't delay the others and
    age their rows out to a false 'offline' (the flapping bug). A per-host adaptive
    timeout (issue #99) still isolates slow remotes — they self-calibrate to a
    working budget instead of going permanently dark, while fast hosts stay at the
    15s default. Errors are kept on the cache row so the UI can show a last error."""
    # Stagger the first run a touch so we don't fire before the app is fully up.
    time.sleep(2)
    while True:
        _heartbeat("host_poller", _app.INTERVAL)
        try:
            hosts = _app.list_hosts()
            if hosts:
                # Each host gets its own thread for the cycle, so the wall-clock
                # period is the slowest single probe, not the sum of all of them.
                with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as ex:
                    list(ex.map(_app._poll_one_host, hosts))
        except Exception as e:
            print("host_poller error:", e, flush=True)
        time.sleep(_app.INTERVAL)


def uptime_worker():
    import app as _app
    """Dedicated daemon loop: wakes every few seconds, probes due checks. Kept off the
    collector thread so a slow/hanging probe never delays metric sampling. Inert (zero
    outbound) when no checks are configured/enabled."""
    while True:
        _heartbeat("uptime_worker", 5)
        try:
            _app._uptime_tick()
        except Exception as e:
            print("uptime_worker error:", e, flush=True)
        time.sleep(5)

# ── Notifier: Discord webhook + ntfy.sh + Telegram ─────────────────────────
# Edge-triggered: each alert key is remembered in _app._NOTIFIED so a flapping state
# doesn't spam the channel. A key clears when the underlying condition recovers
# (container becomes healthy again, disk drops below threshold, etc.), so the
# next failure re-fires exactly once.


def sample_once():
    import app as _app
    conts = _app.containers()
    nm = {c["id"]: c["name"] for c in conts}

    # ── GPU half ──────────────────────────────────────────────────────────────
    # Isolated in its own try/except so a flaky, missing or slow nvidia-_app.smi can
    # NEVER block the host metrics below. Before this, an exception here aborted
    # the whole sample, freezing CPU/RAM/temperature on every poll (and forever on
    # a GPU-less host). Now a GPU failure just degrades the GPU panel to "absent"
    # while temperature & friends keep refreshing.
    util = mem_used = mem_total = power = temp = 0.0
    gpus = []
    gpu_extra = {}
    procs = {}
    gpu_pids = {}
    gpu_avail = False
    gpu_vendor = None   # "nvidia" | "amd" | "hybrid" — drives the vendor-aware GPU diagnostic
    try:
        # One CSV row per card (issue #95). Parse each field defensively: nvidia-_app.smi
        # emits the literal "[N/A]" / "[Not Supported]" for power.draw/temperature
        # on many consumer/laptop GPUs and inside _app.containers, even with `nounits` —
        # so degrade just the bad field to 0 rather than dropping the whole card.
        # NVIDIA via nvidia-smi. Guarded on its own: a missing nvidia-smi raises
        # FileNotFoundError, and before this that aborted the whole GPU half — so an
        # AMD card on a host without nvidia-smi was never read at all (issue #1).
        nv_gpus = []
        try:
            rows = _app.smi(["--query-gpu=index,name,utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu",
                        "--format=csv,noheader,nounits"]).splitlines()
            for line in rows:
                if not line.strip():
                    continue
                p = [x.strip() for x in line.split(",")]
                if len(p) < 7:
                    continue
                u, mu, mt, pw, tp = (_app._gpu_num(x) for x in p[2:7])
                nv_gpus.append({"idx": int(_app._gpu_num(p[0])), "name": p[1] or f"GPU {p[0]}",
                             "util": u, "mem_used": mu, "mem_total": mt, "power": pw, "temp": tp})
        except Exception:
            nv_gpus = []   # no nvidia-smi / wedged driver — an AMD card may still be present
        # AMD via the amdgpu sysfs back-end — read even when NVIDIA is present, so a
        # hybrid NVIDIA+AMD box shows both vendors (issue #1). Re-index the AMD cards
        # above the NVIDIA range so per-card history (gpu_samples.idx) never collides.
        amd_cards = _app.amd_gpus()
        if amd_cards:
            base = (max(g["idx"] for g in nv_gpus) + 1) if nv_gpus else 0
            for i, g in enumerate(amd_cards):
                g["idx"] = base + i
        gpus = nv_gpus + amd_cards
        if not gpus:
            raise ValueError("no NVIDIA or AMD GPU detected")
        gpu_avail = True
        gpu_vendor = ("hybrid" if (nv_gpus and amd_cards)
                      else "amd" if amd_cards else "nvidia")
        # Aggregate across ALL cards for the single-GPU views: VRAM + power pool,
        # utilisation averages, temperature is the hottest card. NVIDIA and AMD
        # expose identical keys, so this stays vendor-agnostic.
        mem_used  = sum(g["mem_used"] for g in gpus)
        mem_total = sum(g["mem_total"] for g in gpus)
        power     = sum(g["power"] for g in gpus)
        util      = round(sum(g["util"] for g in gpus) / len(gpus))
        temp      = max(g["temp"] for g in gpus)
        # NVIDIA-only enrichment (clocks/throttle) + per-process VRAM attribution
        # (nvidia-smi compute-apps), applied to the NVIDIA cards only — the dicts are
        # the same objects held in `gpus`, so in-place enrichment shows through. AMD
        # cards show the core panel (util/VRAM/temp/power); per-process AMD
        # attribution is a follow-up (issue #1).
        if nv_gpus:
            _app._enrich_gpus(nv_gpus)
            gpu_extra = _app._gpu_extra(nv_gpus)
            for line in _app.smi(["--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"]).splitlines():
                if line.strip():
                    pid, mem = (p.strip() for p in line.split(","))
                    svc = _app.service_for_pid(pid, nm)
                    procs[svc] = procs.get(svc, 0) + _app._gpu_num(mem)
                    try:
                        gpu_pids[int(pid)] = gpu_pids.get(int(pid), 0) + _app._gpu_num(mem)
                    except ValueError:
                        pass
        else:
            gpu_extra = {}
    except Exception as e:
        # Log only on the ok→fail edge so a permanently GPU-less host doesn't spam.
        if _app.LATEST.get("gpu_avail"):
            print("GPU sample failed (continuing without GPU):", e, flush=True)

    # Detect models from EVERY recognised AI server, not just the ones holding the GPU
    # right now — so a server that has unloaded its model (e.g. OLLAMA_KEEP_ALIVE
    # expired) or sits between requests still shows up as Idle instead of vanishing.
    # Probes are independent 2 s-timeout HTTP calls, so run them in parallel.
    ai = [c for c in conts if _match_probe(c)]
    models = []
    model_catalog = []   # {service, provider, model, loaded, vram_mb} — the Installed-models registry (#219)
    if ai:
        with ThreadPoolExecutor(max_workers=min(8, len(ai))) as ex:
            found_lists = list(ex.map(probe_models, ai))
        provider_of = {c["name"]: _match_probe_key(c) for c in ai}
        for ct, found in zip(ai, found_lists):
            svc = ct["name"]
            provider = provider_of.get(svc)
            smem = procs.get(svc)                         # MB this server holds on the GPU now
            api_vram = any(v is not None for _, v in found)
            for mdl, vram in found:
                if vram is not None:                      # server reported its own VRAM (Ollama)
                    vram_val = round(vram)
                elif not api_vram and len(found) == 1 and smem:
                    vram_val = round(smem)                # single model ↔ all the server's VRAM
                else:
                    vram_val = None                        # server up but idle / can't attribute
                models.append((svc, mdl, vram_val))
                model_catalog.append({"service": svc, "provider": provider, "model": mdl,
                                       "loaded": vram_val is not None, "vram_mb": vram_val})

    # Attribute model-server traffic to its callers (who is driving Ollama, etc.).
    edges = _app.sample_callers(conts, {c["name"] for c in ai})

    # Model intelligence: per-model metadata (Ollama /api/show, cached) + live serving
    # telemetry (vLLM/TGI /metrics). Both best-effort — a slow/absent endpoint must
    # never wedge the sample, so each is isolated.
    try:
        model_meta = _app.collect_model_meta(ai, models)
    except Exception as e:
        print(f"collectors/sample_once collect_model_meta error: {e}", flush=True)
        model_meta = {}
    try:
        serving = _app.collect_serving(ai)
    except Exception as e:
        print(f"collectors/sample_once collect_serving error: {e}", flush=True)
        serving = []
    try:
        training = _app.collect_training(gpu_pids)
    except Exception as e:
        print(f"collectors/sample_once collect_training error: {e}", flush=True)
        training = []
    try:
        devtools = _app.collect_devtools(gpu_pids)
    except Exception as e:
        print(f"collectors/sample_once collect_devtools error: {e}", flush=True)
        devtools = []

    host = _app.read_host()
    # Measured CPU/DRAM watts (RAPL) + per-process CPU breakdown — both best-effort.
    # Call _app.collect_top_processes ONCE here (the sampler cadence) and cache it so the
    # Top-processes card + the cost attribution share one delta (_app.health_scan reuses it).
    rapl = {}
    try:
        rapl = _app.read_rapl_power()
    except Exception as e:
        print(f"collectors/sample_once read_rapl_power error: {e}", flush=True)
        rapl = {}
    cpu_power, dram_power = rapl.get("cpu_w"), rapl.get("dram_w")
    try:
        top_cpu = _app.collect_top_processes()
    except Exception as e:
        print(f"collectors/sample_once collect_top_processes error: {e}", flush=True)
        top_cpu = None
    _app.HEALTH["processes"] = top_cpu
    ts = int(time.time())
    if _app._DB_MAINTENANCE:
        return
    from backend.db.repos import system as system_repo
    from backend.db.repos import experiments as exp_repo
    # Read retention before acquiring LOCK — get_settings() also takes LOCK
    # (non-reentrant), so calling it inside the block would deadlock the thread.
    _retention = _app.get_retention_secs() if ts % 360 < _app.INTERVAL else None
    with _app.LOCK:
        # When the GPU is absent/failed, store NULL for the GPU columns (not 0) so
        # history charts skip the gap via AVG() instead of showing a fake 0 dip;
        # the host columns are always real.
        gcols = (util, mem_used, mem_total, power, temp) if gpu_avail else (None,)*5
        _app.DB.executemany(
            "INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp,cpu,ram_used,ram_total,load1,ctemp,cpu_power,dram_power)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(ts, *gcols, host["cpu"], host["ram_used"],
              host["ram_total"], host["load1"], host["ctemp"], cpu_power, dram_power)])
        _app.DB.executemany("INSERT INTO proc VALUES(?,?,?)", [(ts, svc, mem) for svc, mem in procs.items()])
        pp_rows = _app._attribute_power_rows(ts, power, procs, cpu_power, top_cpu)
        if pp_rows:
            _app.DB.executemany("INSERT INTO power_proc(ts,kind,name,watts) VALUES(?,?,?,?)", pp_rows)
        _app.DB.executemany("INSERT INTO models VALUES(?,?,?,?)",
                            [(ts, svc, mdl, vram) for svc, mdl, vram in models if vram is not None])
        _app.DB.executemany("INSERT INTO edges VALUES(?,?,?,?)",
                            [(ts, caller, server, n) for (caller, server), n in edges.items()])
        # Per-GPU history only when there's more than one card (single-GPU rigs are
        # already covered by the aggregate `samples` table) — keeps storage lean.
        if gpu_avail and len(gpus) > 1:
            _app.DB.executemany(
                "INSERT INTO gpu_samples(ts,idx,util,mem_used,mem_total,power,temp) VALUES(?,?,?,?,?,?,?)",
                [(ts, g["idx"], g["util"], g["mem_used"], g["mem_total"], g["power"], g["temp"]) for g in gpus])
        _cur_net_rows = list(_app._net_rows(ts, nm))   # host NICs + per-container talkers (#30)
        _app.DB.executemany("INSERT INTO net_samples(ts,iface,bytes_in,bytes_out) VALUES(?,?,?,?)",
                       _cur_net_rows)
        # Disk I/O moves fast, so sample it on its own tighter cadence (~45s) into
        # a dedicated 7-day ring — dense enough for per-device sparklines + the
        # anomaly baseline without bloating the _app.DB. Sourced from the _app.health_scan
        # snapshot (populated every 15s) so no extra /proc read here.
        if ts % 45 < _app.INTERVAL:
            dio = _app.HEALTH.get("disk_io") or {}
            if dio.get("available"):
                _app.DB.executemany(
                    "INSERT INTO disk_io_samples(ts,device,read_mb_s,write_mb_s,util_pct) VALUES(?,?,?,?,?)",
                    [(ts, it["device"], it.get("read_mb_s"), it.get("write_mb_s"), it.get("util_pct"))
                     for it in (dio.get("items") or [])])
            # Persist a BOUNDED per-process I/O ring: only the top-few writers +
            # top-few readers from the attribution already computed (comm only,
            # never argv). Deduped by pid -> at most ~6 rows/poll, not all ~20
            # candidates. Feeds spike-time attribution; rides this same cadence.
            _pio = (_app.HEALTH.get("processes") or {}).get("io") or {}
            if _pio.get("available"):
                _seen_pids, _pio_rows = set(), []
                for _r in (sorted((_pio.get("writers") or []),
                                  key=lambda r: -(r.get("write_b_s") or 0))[:3]
                           + sorted((_pio.get("readers") or []),
                                    key=lambda r: -(r.get("read_b_s") or 0))[:3]):
                    _p = _r.get("pid")
                    if _p in _seen_pids:
                        continue
                    _seen_pids.add(_p)
                    _pio_rows.append((ts, _p, _r.get("name"),
                                      int(_r.get("read_b_s") or 0), int(_r.get("write_b_s") or 0)))
                if _pio_rows:
                    _app.DB.executemany("INSERT INTO proc_io_samples(ts,pid,comm,read_bps,write_bps) "
                                   "VALUES(?,?,?,?,?)", _pio_rows)
        if _retention is not None:
            for t in ("samples", "proc", "models", "edges", "events", "gpu_samples", "net_samples", "power_proc"):
                _app.DB.executemany(f"DELETE FROM {t} WHERE ts<?", [(ts - _retention,)])
            _app.DB.executemany("DELETE FROM disk_io_samples WHERE ts<?", [(ts - _app._DISK_IO_RETENTION,)])
            _app.DB.executemany("DELETE FROM proc_io_samples WHERE ts<?", [(ts - _app._PROC_IO_RETENTION,)])
        if ts % 60 < _app.INTERVAL:   # stale-run janitor: a crashed/disconnected push run -> killed
            _app.DB.executemany(
                "UPDATE runs SET status='killed', ended_at=COALESCE(ended_at,heartbeat_at,?) "
                "WHERE status='running' AND heartbeat_at IS NOT NULL AND heartbeat_at < ?",
                [(ts, ts - 180)])
        # Phase 1.2a: keep rollup tables current after each raw insert
        _app._rollup_now(_app.DB, ts, *gcols,
                    cpu=host["cpu"], ram_used=host["ram_used"], ram_total=host["ram_total"],
                    load1=host["load1"], ctemp=host["ctemp"],
                    cpu_power=cpu_power, dram_power=dram_power)
        _app._rollup_net_now(_app.DB, ts, _cur_net_rows)
        _app.DB.commit()
    # MLflow pull (network; outside the lock) every ~5 min when configured.
    if _app.get_settings().get("mlflow_uri") and ts % 300 < _app.INTERVAL:
        try:
            _app.sync_mlflow()
        except Exception as e:
            print("mlflow sync error:", e, flush=True)
    _app.LATEST.update(ts=ts, util=util, mem_used=mem_used, mem_total=mem_total, power=power, temp=temp,
                  cpu_power=cpu_power, dram_power=dram_power, rapl=rapl.get("domains"),
                  gpu_avail=gpu_avail, gpu_vendor=gpu_vendor, gpus=gpus, gpu_extra=gpu_extra,
                  procs=sorted(({"service": s, "mem": round(m)} for s, m in procs.items()), key=lambda x: -x["mem"]),
                  models=[{"service": s, "model": m, "vram": v} for s, m, v in models],
                  model_catalog=model_catalog,
                  model_meta=model_meta, serving=serving, training=training, devtools=devtools,
                  callers=sorted(({"caller": c, "server": s, "conns": n} for (c, s), n in edges.items()),
                                 key=lambda x: -x["conns"]), host=host)


def collector():
    import app as _app
    last_oom = last_health = last_notify = last_diskio = 0
    while True:
        _heartbeat("collector", _app.INTERVAL)
        try:
            sample_once()
            now = time.time()
            if now - last_oom > 60:
                _app.oom_scan(); last_oom = now
            if now - last_diskio > 60:
                try: _app.diskio_scan()
                except Exception as e: print("_app.diskio_scan error:", e, flush=True)
                last_diskio = now
            if now - last_health > 15:
                _app.health_scan(); last_health = now
            # Notifier runs *after* the latest health/oom data is in place, so
            # state-change detection sees a consistent snapshot.
            if now - last_notify > 20:
                try: _app.notify_scan()
                except Exception as e: print("_app.notify_scan error:", e, flush=True)
                last_notify = now
        except Exception as e:
            print("collector error:", e, flush=True)
        time.sleep(_app.INTERVAL)

# ── Insights ──────────────────────────────────────────────────────────────


def brief_worker():
    import app as _app
    """Dedicated daemon: every 30s, send the daily brief if it's due. Inert unless
    the brief is enabled with a configured channel."""
    while True:
        _heartbeat("brief_worker", 30)
        try:
            _app._brief_run_once()
        except Exception as e:
            print("brief_worker error:", e, flush=True)
        time.sleep(30)


