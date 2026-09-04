import logging
import socket
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
from backend.probes import (_match_probe, _match_probe_key, probe_models,
                            probe_custom_server, parse_custom_servers)

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


def _resolve_fleet_host(stored, known_hosts):
    """A custom server's fleet_host setting → the fleet name its models are
    stamped with. ""/None/"local" → "local" (the hub). A name that is currently
    a registered fleet host → itself (so the per-host AI Models tab picks it up).
    Anything else — a host that was removed after the server was registered —
    degrades to the hub, where it was always visible, rather than vanishing into
    a name no tab has. Pure → unit-testable."""
    if not stored or stored == "local":
        return "local"
    return stored if stored in known_hosts else "local"

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
    svc_by_card = {}    # service -> {card idx: MB} — per-card VRAM attribution
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
            # fan.speed is asked for in the same pass, but nvidia-smi rejects the
            # WHOLE query on an unrecognised field name — so a driver too old to
            # know it falls back to the exact previous 7-field query rather than
            # reporting no cards at all.
            base_fields = ("index,name,utilization.gpu,memory.used,memory.total,"
                           "power.draw,temperature.gpu")
            rows = _app.smi([f"--query-gpu={base_fields},fan.speed",
                             "--format=csv,noheader,nounits"]).splitlines()
            if not any(line.strip() for line in rows):
                rows = _app.smi([f"--query-gpu={base_fields}",
                                 "--format=csv,noheader,nounits"]).splitlines()
            for line in rows:
                if not line.strip():
                    continue
                p = [x.strip() for x in line.split(",")]
                if len(p) < 7:
                    continue
                u, mu, mt, pw, tp = (_app._gpu_num(x) for x in p[2:7])
                card = {"idx": int(_app._gpu_num(p[0])), "name": p[1] or f"GPU {p[0]}",
                        "util": u, "mem_used": mu, "mem_total": mt, "power": pw, "temp": tp}
                # Absent, not 0: passively-cooled datacentre cards have no fan to
                # report, and a 0 here would trip the fan-stall alert on hardware
                # that has no fan to stall.
                fan = _app._gpu_opt(p[7]) if len(p) > 7 else None
                if fan is not None:
                    card["fan"] = round(fan)   # % of max, integral like the probe's
                nv_gpus.append(card)
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
        # NVIDIA enrichment (clocks/throttle) + per-process VRAM attribution
        # (nvidia-smi compute-apps), applied to the NVIDIA cards only — the dicts are
        # the same objects held in `gpus`, so in-place enrichment shows through.
        if nv_gpus:
            _app._enrich_gpus(nv_gpus)
            try:
                # Best-effort on its own: a timeout or malformed row in this extra
                # query must cost this sample's NVIDIA attribution, not the GPU
                # chips below it nor the AMD fdinfo attribution that follows.
                #
                # gpu_uuid comes along so VRAM can be attributed to a *card*, not
                # just to the pool — "which of the three 3090s is ollama on" is
                # the question the per-card cockpit exists to answer. A driver
                # that rejects the field falls back to the original query and
                # simply reports no per-card split.
                uuid_idx = _app._smi_uuid_idx()
                rows = [l for l in _app.smi(["--query-compute-apps=gpu_uuid,pid,used_memory",
                                             "--format=csv,noheader,nounits"]).splitlines()
                        if l.strip()]
                # CHECK the answer rather than inferring the shape from the fact
                # that rows came back: nvidia-smi UUIDs are always "GPU-…" (or
                # "MIG-…" on a partitioned card), so the leading field identifies
                # itself. Guessing wrong here shifts every column by one. Same
                # verification probe.py does — the two must not drift.
                has_uuid = bool(rows) and all(
                    r.strip().startswith(("GPU-", "MIG-")) for r in rows)
                if not has_uuid:
                    rows = _app.smi(["--query-compute-apps=pid,used_memory",
                                     "--format=csv,noheader,nounits"]).splitlines()
                for line in rows:
                    if not line.strip():
                        continue
                    f = [x.strip() for x in line.split(",")]
                    if len(f) < (3 if has_uuid else 2):
                        continue
                    uuid, (pid, mem) = (f[0], f[1:3]) if has_uuid else (None, f[:2])
                    svc = _app.service_for_pid(pid, nm)
                    mb = _app._gpu_num(mem)
                    procs[svc] = procs.get(svc, 0) + mb
                    idx = uuid_idx.get(uuid) if uuid else None
                    if idx is not None:
                        per = svc_by_card.setdefault(svc, {})
                        per[idx] = per.get(idx, 0) + mb
                    try:
                        gpu_pids[int(pid)] = gpu_pids.get(int(pid), 0) + mb
                    except ValueError:
                        pass
            except Exception:
                logging.debug("per-PID GPU memory aggregation failed", exc_info=True)
                pass
        # Aggregate the 'GPU right now' chips from EVERY card, whatever the vendor:
        # NVIDIA cards were enriched just above, AMD ones arrive already enriched
        # (amd_gpus reads clocks/perf level/cap in its per-card pass). The pooled
        # sums must span vendors — an NVIDIA-only aggregate on a hybrid box
        # compares total draw (both vendors) against the NVIDIA cap alone, and the
        # power chip reads >100% whenever the AMD card draws anything.
        gpu_extra = _app._gpu_extra(gpus)
        # AMD per-process VRAM via DRM fdinfo (kernel 5.19+) — the amdgpu counterpart
        # of --query-compute-apps, feeding the same procs/gpu_pids pipeline so the
        # VRAM-allocation panel, VRAM-by-service chart, container VRAM column and GPU
        # cost attribution all light up on AMD too. On a unified-memory APU the
        # working set lives in GTT (see amd_gpus), so GTT counts there — matched
        # per-device by _amd_attrib_mb, so a discrete AMD card in the same box
        # keeps counting VRAM only, matching what mem_used reports.
        if amd_cards:
            for pid, devs in _app.amd_fdinfo_procs().items():
                mb = _app._amd_attrib_mb(devs, amd_cards)
                if mb < 1:
                    continue   # sub-MB DRM clients (compositors idling etc.) are noise
                svc = _app.service_for_pid(pid, nm)
                procs[svc] = procs.get(svc, 0) + mb
                gpu_pids[pid] = gpu_pids.get(pid, 0) + mb
    except Exception as e:
        # Log only on the ok→fail edge so a permanently GPU-less host doesn't spam.
        if _app.LATEST.get("gpu_avail"):
            print("GPU sample failed (continuing without GPU):", e, flush=True)

    # Detect models from EVERY recognised AI server, not just the ones holding the GPU
    # right now — so a server that has unloaded its model (e.g. OLLAMA_KEEP_ALIVE
    # expired) or sits between requests still shows up as Idle instead of vanishing.
    # Probes are independent 2 s-timeout HTTP calls, so run them in parallel.
    ai = [c for c in conts if _match_probe(c)]
    # Plus the user-registered custom servers: auto-discovery only reaches
    # the hub's containers on standard ports and remotes' localhost ollama, so a
    # vLLM on another box at a non-standard port needs an explicit entry. A
    # malformed setting degrades to "no custom servers", never to a broken sample.
    custom_raw = _app.get_settings().get("custom_ai_servers") or ""
    custom, _c_err = parse_custom_servers(custom_raw)
    if _c_err or custom is None:
        print(f"custom_ai_servers ignored ({_c_err or 'unparseable'}): {str(custom_raw)[:120]!r}", flush=True)
        custom = []
    # Fleet names the per-host AI Models tab filters on: "local" (the hub) plus
    # the registered hosts. Read once per sample, not per row.
    try:
        _known_hosts = {"local"} | {h["name"] for h in _app.list_hosts()}
    except Exception:
        _known_hosts = {"local"}
    for s in custom:
        if any(c["name"] == s["name"] for c in ai):
            continue                                    # container discovery already covers it
        fleet = _resolve_fleet_host(s.get("fleet_host"), _known_hosts)
        if fleet != (s.get("fleet_host") or "local"):
            print(f"custom_ai_servers '{s['name']}': host '{s.get('fleet_host')}' "
                  f"not registered — shown under the hub instead", flush=True)
        ai.append({"name": s["name"], "ip": s["host"], "port": s["port"],
                   "provider": s["provider"], "image": s["provider"],
                   "ports": [s["port"]], "fleet_host": fleet})
    models = []
    model_catalog = []   # {host, service, provider, model, loaded, vram_mb} — the Installed-models registry (#219)
    ai_servers = []      # {name, ip, port, provider} — for the /api/ai/now throttled live re-probe
    # Needed below, ahead of its usual place further down: every non-Ollama probe
    # (vLLM, llama.cpp, TGI, …) always reports vram=None — it has no on-disk/loaded
    # split the way Ollama does, so the *only* other way to attribute VRAM is the
    # hub's own nvidia-smi process list, which can never see a process on a remote
    # box. A custom server registered against a remote fleet_host would then show
    # "not loaded" forever despite visibly serving traffic — its live /metrics
    # telemetry (below) is the one host-independent signal that it's genuinely
    # resident, so it has to be known before "loaded" is decided per model.
    try:
        serving = _app.collect_serving(ai)
    except Exception as e:
        print(f"collectors/sample_once collect_serving error: {e}", flush=True)
        serving = []
    serving_services = {s["service"] for s in serving if s.get("service")}
    if ai:
        def _probe_one(ct):
            # Containers ride the port-guessing PROBES table; custom descriptors
            # carry the user's explicit port, so probe_custom_server owns them.
            return probe_custom_server(ct) if "port" in ct else probe_models(ct)
        with ThreadPoolExecutor(max_workers=min(8, len(ai))) as ex:
            found_lists = list(ex.map(_probe_one, ai))
        provider_of = {c["name"]: (c.get("provider") or _match_probe_key(c)) for c in ai}
        ai_servers = [{"name": c["name"], "ip": c.get("ip") or "127.0.0.1",
                       "port": c.get("port"), "provider": provider_of.get(c["name"])} for c in ai]
        for ct, found in zip(ai, found_lists):
            svc = ct["name"]
            provider = provider_of.get(svc)
            # The per-host AI Models tab groups /api/models by fleet name; the
            # hub's rows must say "local" (its raw hostname matches no pill), and
            # a custom server rides the fleet name it was registered for.
            host_label = ct.get("fleet_host") if "fleet_host" in ct else "local"
            smem = procs.get(svc)                         # MB this server holds on the GPU now
            api_vram = any(v is not None for _, v, _, _ in found)
            for mdl, vram, ram, ctx in found:
                if vram is not None:                      # server reported its own VRAM (Ollama)
                    vram_val = round(vram)
                    ram_val = round(ram) if ram else 0    # spill into system RAM (0 = fully on GPU)
                elif not api_vram and len(found) == 1 and smem:
                    vram_val = round(smem)                # single model ↔ all the server's VRAM
                    ram_val = None                        # attributed from nvidia-smi — spill unknown
                else:
                    vram_val = None                        # server up but idle / can't attribute
                    ram_val = None
                models.append((svc, mdl, vram_val, ram_val,
                               ctx if vram_val is not None else None))
                model_catalog.append({
                    "host": host_label,
                    "service": svc,
                    "provider": provider,
                    "model": mdl,
                    # A model this server's own /metrics confirms is actively serving
                    # is unambiguously loaded, VRAM or no VRAM figure to show for it.
                    "loaded": vram_val is not None or svc in serving_services,
                    "vram_mb": vram_val,
                    "ram_mb": ram_val,
                })

    # Attribute model-server traffic to its callers (who is driving Ollama, etc.).
    edges = _app.sample_callers(conts, {c["name"] for c in ai})

    # Model intelligence: per-model metadata (Ollama /api/show, cached). Live serving
    # telemetry (vLLM/TGI /metrics) was already collected above — collect_serving()
    # tracks each service's token counter between calls to derive tok/s, so calling
    # it a second time here would sample that delta over mere milliseconds instead
    # of a full sample interval and report a garbage rate.
    try:
        model_meta = _app.collect_model_meta(ai, models)
    except Exception as e:
        print(f"collectors/sample_once collect_model_meta error: {e}", flush=True)
        model_meta = {}
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
    cpu_power, dram_power = rapl.get("cpu_power"), rapl.get("dram_power")
    # Surface measured watts on the live snapshot so /metrics can export them.
    # Only overwrite on a real reading — a transient {} from read_rapl_power keeps the last good value.
    if cpu_power is not None:
        _app.LATEST["cpu_power"] = cpu_power
    if dram_power is not None:
        _app.LATEST["dram_power"] = dram_power
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
    from backend.db.repos import gpu_samples as gpu_samples_repo
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
        _app.DB.executemany("INSERT INTO proc(ts,service,mem,host) VALUES(?,?,?,'local')",
                            [(ts, svc, mem) for svc, mem in procs.items()])
        pp_rows = _app._attribute_power_rows(ts, power, procs, cpu_power, top_cpu)
        if pp_rows:
            _app.DB.executemany("INSERT INTO power_proc(ts,kind,name,watts) VALUES(?,?,?,?)", pp_rows)
        _app.DB.executemany("INSERT INTO models(ts,service,model,vram,ram) VALUES(?,?,?,?,?)",
                            [(ts, svc, mdl, vram, ram) for svc, mdl, vram, ram, _ctx in models if vram is not None])
        _app.DB.executemany("INSERT INTO edges VALUES(?,?,?,?)",
                            [(ts, caller, server, n) for (caller, server), n in edges.items()])
        # Per-card history for the hub's own cards, stored under host='local' so
        # the GPU cockpit reads them through exactly the same query it uses for a
        # remote. Written for single-card rigs too, now that the tab charts fan,
        # memory-bandwidth and throttle state per card — none of which the pooled
        # `samples` table carries, so "one card means the aggregate covers it" no
        # longer holds.
        if gpu_avail and gpus:
            gpu_samples_repo.record(_app.DB, ts, "local", gpus, interval=_app.INTERVAL)
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
            for t in ("samples", "proc", "models", "edges", "events", "gpu_samples", "net_samples", "power_proc", "host_samples"):
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
    # Private (not in LATEST → never serialized to clients): where the recognised
    # AI servers live, so the /api/ai/now fast path can re-probe ollama on demand.
    _app.AI_SERVERS = ai_servers
    _app.LATEST.update(ts=ts, util=util, mem_used=mem_used, mem_total=mem_total, power=power, temp=temp,
                  cpu_power=cpu_power, dram_power=dram_power, rapl=rapl.get("domains"),
                  gpu_avail=gpu_avail, gpu_vendor=gpu_vendor, gpus=gpus, gpu_extra=gpu_extra,
                  procs=sorted(({"service": s, "mem": round(m),
                                 **({"by_card": {str(i): round(v) for i, v in sorted(svc_by_card[s].items())}}
                                    if s in svc_by_card else {})}
                                for s, m in procs.items()), key=lambda x: -x["mem"]),
                  models=[{"service": s, "model": m, "vram": v, "ram": r, "ctx_now": c}
                          for s, m, v, r, c in models],
                  model_catalog=model_catalog,
                  model_meta=model_meta, serving=serving, training=training, devtools=devtools,
                  callers=sorted(({"caller": c, "server": s, "conns": n} for (c, s), n in edges.items()),
                                 key=lambda x: -x["conns"]), host=host)
    # Wake the SSE streams: the slow sample carries everything the fast lane
    # can't (containers, models, VRAM attribution, callers), so a browser must
    # not have to wait for the next fast tick to see it.
    _app.bump_live()


def fast_sample_once():
    """Refresh the values a human watches move, and nothing else.

    Writes no database rows on purpose. Everything priced in this project is
    integrated from `samples` at INTERVAL spacing, so an extra row here would be
    counted as a full interval of energy and inflate every cost figure on the
    page — the fast lane exists to make the screen quicker, not the history
    denser."""
    import app as _app
    host = _app.read_host_fast()
    if host:
        # Merge, never replace: read_host_fast() deliberately omits disks and the
        # OS/hardware/network/security inventories, and assigning a fresh dict
        # here would blank those panels between slow samples.
        cur = dict(_app.LATEST.get("host") or {})
        cur.update(host)
        _app.LATEST["host"] = cur
    # Only touch the GPU once the sampler has confirmed there is one, and only for
    # NVIDIA: a GPU-less box must not spawn nvidia-smi every couple of seconds
    # forever, and AMD's sysfs cards are refreshed by the sampler itself.
    if _app.LATEST.get("gpu_avail") and (_app.LATEST.get("gpu_vendor") in ("nvidia", "hybrid")):
        fresh = _app.gpu_cards_fast()
        if fresh:
            cards = _app.LATEST.get("gpus") or []
            for c in cards:
                upd = fresh.get(c.get("idx"))
                if upd:
                    c.update(upd)
            # Re-pool the aggregates from the cards so the headline GPU numbers and
            # the per-card panels can never disagree mid-interval. Same reductions
            # sample_once uses — average utilisation, hottest card, summed watts.
            if cards:
                _app.LATEST["util"] = round(sum(c.get("util") or 0 for c in cards) / len(cards))
                _app.LATEST["mem_used"] = sum(c.get("mem_used") or 0 for c in cards)
                _app.LATEST["power"] = sum(c.get("power") or 0 for c in cards)
                _app.LATEST["temp"] = max(c.get("temp") or 0 for c in cards)
    _app.LATEST["fast_ts"] = int(time.time())
    _app.bump_live()


def fast_sampler():
    import app as _app
    """Loop the cheap re-read at FAST_INTERVAL. Inert when FAST_INTERVAL is 0."""
    if not _app.FAST_INTERVAL:
        return
    # Prime the CPU delta before the first published reading, otherwise that
    # reading is an average stretching back to boot rather than a live figure.
    try:
        _app.read_host_fast()
    except Exception as e:
        print("fast_sampler prime error:", e, flush=True)
    time.sleep(_app.FAST_INTERVAL)
    while True:
        _heartbeat("fast_sampler", _app.FAST_INTERVAL)
        try:
            fast_sample_once()
        except Exception as e:
            print("fast_sampler error:", e, flush=True)
        time.sleep(_app.FAST_INTERVAL)


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


