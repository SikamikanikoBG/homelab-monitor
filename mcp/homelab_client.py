"""Pure-stdlib client + response shaping for the HomeLab Monitor MCP server.

This module deliberately has **no dependency on the `mcp` SDK** (or any third-party
package). It only knows how to:

  1. call the monitor's existing read-only HTTP endpoints, and
  2. trim/relabel their payloads into compact, LLM-friendly shapes.

Keeping it SDK-free means the substance — the endpoint wrapping and trimming — can
be imported and unit-tested on any Python 3.8+ (the `mcp` SDK needs 3.10+ and only
runs inside the shipped 3.12 image). `server.py` is the thin layer that turns each
function here into an MCP tool/resource.

Everything is **read-only**. There is intentionally no function that mutates the
fleet — see issue #70's guardrails.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://localhost:9800"


def base_url():
    """Monitor base URL, e.g. http://ardi:9800. Trailing slash trimmed."""
    return (os.environ.get("HOMELAB_MONITOR_URL") or DEFAULT_URL).rstrip("/")


def _timeout():
    try:
        return float(os.environ.get("HOMELAB_HTTP_TIMEOUT", "10"))
    except ValueError:
        return 10.0


class MonitorError(RuntimeError):
    """Raised when the monitor can't be reached or returns a non-2xx / bad body.

    Carries a short, human-readable message so the agent gets an actionable error
    (wrong URL, monitor down, unknown host) instead of a raw stack trace.
    """


def _get(path):
    """GET a JSON endpoint and return the decoded object."""
    url = base_url() + path
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "homelab-monitor-mcp"})
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise MonitorError("monitor returned HTTP %s for %s" % (e.code, path))
    except urllib.error.URLError as e:
        raise MonitorError("cannot reach monitor at %s (%s)" % (base_url(), e.reason))
    except OSError as e:
        raise MonitorError("cannot reach monitor at %s (%s)" % (base_url(), e))
    try:
        return json.loads(raw)
    except ValueError:
        raise MonitorError("monitor returned a non-JSON body for %s" % path)


def _get_text(path):
    """GET a text endpoint (e.g. /metrics) and return the raw string."""
    url = base_url() + path
    req = urllib.request.Request(url, headers={"User-Agent": "homelab-monitor-mcp"})
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise MonitorError("monitor returned HTTP %s for %s" % (e.code, path))
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise MonitorError("cannot reach monitor at %s (%s)" % (base_url(), reason))


# ── shaping helpers ──────────────────────────────────────────────────────────

def _host_summary(host):
    """One host's *headline* vitals — the compact form used in the fleet roster."""
    if not host:
        return None
    os_info = host.get("os") or {}
    ram_total = host.get("ram_total") or 0
    ram_used = host.get("ram_used") or 0
    os_name = os_info.get("pretty") or os_info.get("name") or os_info.get("id")
    out = {
        "os": os_name.strip() if isinstance(os_name, str) else os_name,
        "kernel": os_info.get("kernel"),
        "arch": os_info.get("arch"),
        "cpu_pct": host.get("cpu"),
        "cores": host.get("cores"),
        "load1": host.get("load1"),
        "ram_used_mb": ram_used,
        "ram_total_mb": ram_total,
        "ram_pct": round(100 * ram_used / ram_total, 1) if ram_total else None,
        "cpu_temp_c": host.get("ctemp"),
        "uptime_sec": host.get("uptime"),
    }
    # Fullest disk only, so the roster stays one line per host.
    disks = host.get("disks") or []
    if disks:
        fullest = max(disks, key=lambda d: d.get("pct") or 0)
        out["fullest_disk"] = {"mount": fullest.get("mount"), "pct": fullest.get("pct")}
    # Surface an OS-upgrade hint when enrich_os_upgrade() attached one.
    if host.get("os_upgrade") or os_info.get("upgrade"):
        out["os_upgrade"] = host.get("os_upgrade") or os_info.get("upgrade")
    if host.get("reboot_required") or host.get("reboot_pending"):
        out["reboot_required"] = True
    return {k: v for k, v in out.items() if v is not None}


# ── tool implementations (each returns plain JSON-able data) ─────────────────

def list_hosts():
    """Fleet roster + headline vitals for every registered host (local hub first)."""
    data = _get("/api/fleet")
    hosts = []
    for row in data.get("hosts", []):
        hosts.append({
            "name": row.get("name"),
            "label": row.get("label"),
            "online": row.get("online"),
            "is_local": row.get("is_local"),
            "ssh_target": row.get("ssh_target"),
            "last_seen_ts": row.get("at"),
            "overall": ((row.get("last_check") or {}).get("summary") or {}).get("overall"),
            "vitals": _host_summary(row.get("host")),
            "error": row.get("error"),
        })
    return {"count": len(hosts), "sample_interval_sec": data.get("interval"), "hosts": hosts}


def get_host(name):
    """Full System / Network / Security inventory for one host.

    `name` is the registered host name, or "local" for the hub itself.
    """
    data = _get("/api/host_data/" + urllib.parse.quote(str(name)))
    host = data.get("host")
    if host is None and not data.get("online", True):
        # Host known but no successful poll yet — return the waiting state verbatim.
        return {"name": data.get("name", name), "online": False,
                "error": data.get("error") or "no data yet", "at": data.get("at")}
    return {
        "name": data.get("name", name),
        "online": data.get("online"),
        "at": data.get("at"),
        "stale_for_sec": data.get("stale_for") or 0,
        "error": data.get("error"),
        "host": host,
    }


def get_snapshot():
    """Live, DB-free vitals: GPU, host, Docker + systemd health, and the overview.

    Mirrors the dashboard's at-a-glance state. Container/service lists are trimmed
    to *problems* plus headline counts so the payload stays small; ask `get_host`
    for full per-host detail.
    """
    h = _get("/api/health")
    docker = h.get("docker") or {}
    systemd = h.get("systemd") or {}
    containers = docker.get("containers") or []
    services = systemd.get("services") or []
    # Genuine problems only: a non-running container, or one Docker reports
    # explicitly unhealthy. (A "starting"/"" health is transient, not a problem.)
    problem_containers = [c for c in containers
                          if c.get("state") not in (None, "running")
                          or c.get("health") == "unhealthy"]
    # systemd "failed" is the real failure state. A completed oneshot is
    # "inactive", not failed — don't surface those as problems (matches the
    # monitor's own `summary.failed` count). `status == "bad"` catches anything
    # the monitor itself flags as bad even if ActiveState differs.
    failed_services = [s for s in services
                       if s.get("active") == "failed" or s.get("status") == "bad"]
    update = h.get("update") or {}
    return {
        "version": h.get("version"),
        "updated_ts": h.get("updated"),
        "overview": h.get("overview"),
        "gpu": (h.get("now") or {}).get("gpu"),
        "host": _host_summary((h.get("now") or {}).get("host")),
        "docker": {
            "available": docker.get("available"),
            "reason": docker.get("reason"),
            "summary": docker.get("summary"),
            "problem_containers": problem_containers,
        },
        "systemd": {
            "available": systemd.get("available"),
            "reason": systemd.get("reason"),
            "summary": systemd.get("summary"),
            "failed_services": failed_services,
        },
        "os_updates": h.get("os_updates"),
        "diagnostics": h.get("diagnostics"),
        "update_available": bool(update.get("available")),
        "current_version": update.get("current") or h.get("version"),
    }


def get_containers():
    """Every Docker container with full detail (the Containers tab).

    Each item: name, id, image, state, status/status_text, health label, exposed
    ports, RAM (`mem_bytes`), VRAM (`vram_bytes`), image disk (`disk_bytes`),
    uptime. Returns the complete list, not just problems.
    """
    h = _get("/api/health")
    d = h.get("docker") or {}
    return {"available": d.get("available"), "reason": d.get("reason"),
            "summary": d.get("summary"), "containers": d.get("containers") or []}


def get_services():
    """Every systemd unit with full detail (the Services tab).

    Each item: name, active/sub state, description, listening ports, RAM
    (`mem_bytes`), uptime, whether it's an admin/vendor unit and whether it's
    explicitly watched, plus the monitor's own status verdict.
    """
    h = _get("/api/health")
    s = h.get("systemd") or {}
    return {"available": s.get("available"), "reason": s.get("reason"),
            "summary": s.get("summary"), "services": s.get("services") or []}


def get_ai_models(range="6h"):
    """Which model servers are loaded, their VRAM, and *who is driving them*.

    `loaded` is the current snapshot of model servers; `vram_summary` is peak/avg
    VRAM per (server, model) over `range`; `callers` is connection-seconds per
    caller→server edge over `range` — the "driven by" attribution.
    """
    data = _get("/api/data?range=" + urllib.parse.quote(str(range)))
    now = data.get("now") or {}
    return {
        "range": data.get("range", range),
        "loaded": now.get("models") or [],
        "vram_summary": data.get("model_summary") or [],
        "callers": data.get("callers") or [],
    }


def get_events(range="6h"):
    """Recent edge-triggered events (OOM kills, threshold crossings) + insights.

    Each event may carry a `blame` line attributing a memory loss to the service
    that grew at the same time. `insights` are the human-readable takeaways the
    dashboard derives from the same window.
    """
    data = _get("/api/data?range=" + urllib.parse.quote(str(range)))
    return {
        "range": data.get("range", range),
        "events": data.get("events") or [],
        "insights": data.get("insights") or [],
    }


# Alias kept because the issue lists both names; alerts == edge-triggered events.
def get_alerts(range="6h"):
    """Alias for get_events — the monitor's alerts *are* its edge-triggered events."""
    return get_events(range)


def get_incidents(limit=20, incident_id=""):
    """Correlated-anomaly **incidents** — the monitor groups co-firing z-score
    anomalies (GPU util/VRAM/power/temp + total power) into ONE lifecycled incident
    instead of N independent flags.

    Without `incident_id`: recent incidents, open-first then most-recent (capped by
    `limit`), each with its `members` (per-series direction, peak σ, value-vs-
    baseline, first/last seen, active), derived `severity` (warning/critical),
    `state` (open/cleared) and timestamps — plus a `summary` (open count + the top
    open incident). With `incident_id`: full detail for that one incident including
    a derived `timeline` (opened → each member joining → cleared); an unknown id
    surfaces as an HTTP 404 error. Read-only; members are only telemetry series
    keys (no topology/secret leak).
    """
    if incident_id:
        d = _get("/api/incidents/" + urllib.parse.quote(str(incident_id)))
        return d.get("incident", d)
    try:
        lim = max(1, int(limit))
    except (TypeError, ValueError):
        lim = 20
    d = _get("/api/incidents?limit=" + str(lim))
    incidents = d.get("incidents") or []
    return {
        "summary": d.get("summary") or {"open": 0, "top": None},
        "count": len(incidents),
        "incidents": incidents,
    }


def get_memory(range="6h"):
    """RAM breakdown — the data behind the System tab's memory treemap.

    `per_service` is peak/avg/present RAM (MB) per container/systemd service over
    `range`; `current_procs` is the current RAM (MB) per service right now;
    `ram_kernel_mb` is non-reclaimable kernel memory (slab/page-tables/stacks).
    Together these explain where used RAM is going.
    """
    data = _get("/api/data?range=" + urllib.parse.quote(str(range)))
    now = data.get("now") or {}
    # NB: /api/data's top-level `mem_total` and `now.mem_used` are GPU *VRAM* (see
    # get_gpu). System RAM lives on the host snapshot — read it from there.
    host = now.get("host") or {}
    return {
        "range": data.get("range", range),
        "ram_total_mb": host.get("ram_total"),
        "ram_used_mb": host.get("ram_used"),
        "ram_kernel_mb": host.get("ram_kernel"),
        "peak_used_mb": data.get("peak_mem"),
        "pressure_free_mb": data.get("pressure_free_mb"),
        "per_service": data.get("summary") or [],
        "current_procs": now.get("procs") or [],
    }


def get_gpu(range="6h"):
    """GPU detail — current utilisation/VRAM/power/temp, per-model VRAM use and the
    caller→server attribution that explains *who is driving the GPU*.

    `models_vram` is peak/avg VRAM (MB) per (server, model) over `range`;
    `callers` is connection-seconds per caller→server edge over the same window.
    """
    data = _get("/api/data?range=" + urllib.parse.quote(str(range)))
    now = data.get("now") or {}
    return {
        "range": data.get("range", range),
        "available": now.get("gpu_avail"),
        "util_pct": now.get("util"),
        "vram_used_mb": now.get("mem_used"),
        "vram_total_mb": now.get("mem_total"),
        "power_w": now.get("power"),
        "temp_c": now.get("temp"),
        "pressure_free_mb": data.get("pressure_free_mb"),
        "models_vram": data.get("model_summary") or [],
        "callers": data.get("callers") or [],
    }


def get_history(range="6h"):
    """Charted time-series the dashboard graphs over `range`.

    `labels` are unix timestamps; `series` holds aligned arrays: GPU `util`/`mem`/
    `power`/`temp`, host `cpu`/`ram_used`/`ram_total`/`load1`/`ctemp`, and `mempk`
    (peak GPU VRAM per bucket). `bucket_sec` is the down-sampling bucket width.
    """
    data = _get("/api/data?range=" + urllib.parse.quote(str(range)))
    return {
        "range": data.get("range", range),
        "bucket_sec": data.get("bucket_sec"),
        "labels": data.get("labels") or [],
        "series": data.get("total") or {},
        "peak_mem_mb": data.get("peak_mem"),
    }


# ── costs & experiments (the AI Lab Cockpit, over MCP) ───────────────────────

def get_costs(range="7d"):
    """Power-cost summary for the hub (the Costs tab): what the machine drew and
    what it cost over `range`, plus the ranked list of which processes, containers,
    services and models cost the most.

    Returns `currency` and `tariff` (flat, or a day/night split), the `machine`
    totals (`now_w` live draw, `energy_kwh`, and `cost` windows for today/7d/30d
    plus `cost_range` for the selected window), and `breakdown` — the top energy
    consumers, each with `kind`, `name`, `energy_kwh`, `cost` and `avg_w`.
    `enabled` is False when no tariff is configured (energy is still reported,
    cost reads 0). Answers "what did my homelab cost, and what's the biggest line
    item?". The per-bucket stacked-area chart is omitted to keep this compact.
    """
    d = _get("/api/costs?range=" + urllib.parse.quote(str(range)))
    machines = d.get("machines") or []
    m = machines[0] if machines else {}
    return {
        "range": d.get("range", range),
        "enabled": d.get("enabled"),
        "currency": d.get("currency"),
        "rapl_available": d.get("rapl_available"),
        "tariff": d.get("tariff"),
        "machine": {
            "now_w": m.get("now_w"),
            "energy_kwh": m.get("energy_kwh"),
            "cost": m.get("cost"),
            "cost_range": m.get("cost_range"),
            "measured": m.get("measured"),
            "estimated": m.get("estimated"),
        },
        "breakdown": d.get("breakdown") or [],
    }


def get_entity_cost(name, kind="", range="7d"):
    """Cost drill-down for one process / container / service / model by `name`
    (the Costs tab's click-through). Pass the `kind`/`name` pair from
    `get_costs`' breakdown; `kind` is optional but disambiguates same-named rows.

    Returns `energy_kwh` and `cost` over `range`, the `avg_w`/`peak_w` it drew,
    and `resources` (e.g. peak GPU VRAM). Answers "what did *this* model/container
    cost me?". The per-bucket cost-curve series is omitted to keep this compact.
    """
    q = "?name=" + urllib.parse.quote(str(name)) + "&range=" + urllib.parse.quote(str(range))
    if kind:
        q += "&kind=" + urllib.parse.quote(str(kind))
    d = _get("/api/costs/entity" + q)
    return {
        "name": d.get("name", name),
        "kind": d.get("kind", kind),
        "range": d.get("range", range),
        "currency": d.get("currency"),
        "energy_kwh": d.get("energy_kwh"),
        "cost": d.get("cost"),
        "avg_w": d.get("avg_w"),
        "peak_w": d.get("peak_w"),
        "resources": d.get("resources"),
    }


def get_experiments(range="7d", status=""):
    """Tracked training/eval runs (the Experiments tab), each priced with the real
    GPU energy it burned. Optionally filter by `status` (running / finished /
    failed / killed).

    Returns one row per run: `id`, `name`, `source` (sdk/mlflow/…), `status`,
    `started_at`/`ended_at`/`duration`, `host`, `params`, `tags`,
    `metrics_latest` (the last value logged per metric — e.g. loss/accuracy), and
    the energy it cost: `energy_kwh`, `cost`, `avg_w`, `peak_util`. Answers "which
    runs ran, how did they do, and what did each one cost?".
    """
    q = "?range=" + urllib.parse.quote(str(range))
    if status:
        q += "&status=" + urllib.parse.quote(str(status))
    d = _get("/api/runs" + q)
    runs = d.get("runs") or []
    return {
        "range": d.get("range", range),
        "currency": d.get("currency"),
        "tariff_mode": d.get("tariff_mode"),
        "count": len(runs),
        "runs": runs,
    }


def get_experiment(run_id):
    """Full detail for one tracked run by `run_id` (from `get_experiments`).

    Returns its logged-metric series (`metrics`: per-key steps/ts/values — the
    loss curve), the GPU `resource` time-series (`power_w`/`util_pct` over the
    run), and the priced energy it burned (`energy_kwh`, `cost`, `avg_w`,
    `peak_util`). An unknown id surfaces as an HTTP 404 error.
    """
    return _get("/api/runs/" + urllib.parse.quote(str(run_id)))


def scan_disk(path="/", rescan=False, max_wait=60):
    """WizTree-style nested folder-size treemap for a host path (the Disks tab).

    Wraps the monitor's on-demand `/api/disk_scan`, which runs in the background;
    this polls until it's `done` (up to `max_wait` seconds) and returns the tree.
    `entries` is a nested list of {name, path, bytes, children}. `path` must be an
    absolute host path (e.g. "/", "/var", "/home"). Set `rescan=True` to force a
    fresh scan instead of reusing a cached one.
    """
    base_q = "?path=" + urllib.parse.quote(str(path))
    query = base_q + ("&rescan=1" if rescan else "")
    waited = 0.0
    while True:
        d = _get("/api/disk_scan" + query)
        state = d.get("state")
        if state != "scanning":
            return {"path": d.get("path", path), "state": state,
                    "total_bytes": d.get("total"), "free_bytes": d.get("free"),
                    "entries": d.get("entries") or [], "error": d.get("error")}
        if waited >= max_wait:
            return {"path": path, "state": "scanning",
                    "note": "still scanning after %ds — call scan_disk again to poll" % int(max_wait)}
        time.sleep(1.5)
        waited += 1.5
        query = base_q  # never re-trigger a rescan on subsequent polls


def get_metrics():
    """Raw Prometheus exposition text from the monitor's /metrics endpoint."""
    return _get_text("/metrics")


def get_version():
    """Liveness + running version from /healthz (cheap, never blocks)."""
    return _get("/healthz")
