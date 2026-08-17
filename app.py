#!/usr/bin/env python3
"""Home-lab monitor — GPU & local-AI first, whole-host at a glance.

- Attributes GPU VRAM to whatever container/process is using it (fully dynamic).
- Drills down to *which model* is loaded on recognised servers (Ollama, vLLM,
  HF TGI, llama.cpp, Stable Diffusion, ComfyUI).
- Detects VRAM pressure + scans GPU containers' logs for OOM events and
  correlates who-lost-to-whom, then turns it into plain recommendations.
- Reads host CPU / RAM / load / temperature / disk so you can see the whole
  box is healthy from one page, remotely.
- Reports the health of every Docker container and of systemd services
  (including the units you deploy yourself), so the page covers more than the GPU.
- SQLite history, downsampled on read so any range stays fast & readable.

Adding a new monitor (it's meant to be easy):
  1. Write a `collect_<thing>()` that returns a small dict, e.g.
     {"available": bool, "summary": {...}, "items": [...]}.
  2. Populate it from `health_scan()` so the background thread keeps it fresh.
  3. Expose it via `/api/health` and add a matching tab/panel in dashboard.html.
"""
import os, re, sys, glob, time, json, socket, sqlite3, threading, subprocess, smtplib, http.client, urllib.parse, urllib.request, urllib.error, ipaddress, shlex, struct, shutil, tempfile, secrets, hmac, uuid, hashlib, email.message, fnmatch, errno
from functools import wraps

# ── One module, one copy ──────────────────────────────────────────────────────
# The container entrypoint starts this file as a script (`python /app/app.py`),
# which makes it the module "__main__". Everything under backend/ then reaches
# back for globals with a lazy `import app as _app` — and because "app" was not
# in sys.modules, that import used to execute this file a SECOND time, in full,
# as a separate module object.
#
# Two copies of app.py in one process means: two SQLite connections, two LOCKs
# that serialise nothing against each other, two LATEST dicts (the blueprints
# read one, half the samplers write the other), and — because the worker threads
# start at module level — two collectors, two host pollers, two notifiers. Every
# remote was SSH-probed twice per interval, and the append-only tables collected
# duplicate rows: 268 duplicate (ts, service) groups in `proc` and 10,663 in
# `net_samples` in a single hour on the release build.
#
# Registering this module under "app" before any backend import runs makes that
# lazy import resolve to the module already executing.
if __name__ == "__main__":
    sys.modules.setdefault("app", sys.modules["__main__"])
try:
    import fcntl                       # Linux-only; used for per-iface IPv4 (SIOCGIFADDR)
except ImportError:
    fcntl = None
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, Response, send_file, send_from_directory, after_this_request, g, abort
import db_backup
try:
    from prometheus_client import (Gauge, generate_latest, CONTENT_TYPE_LATEST,
                                   REGISTRY, CollectorRegistry)
    _PROM_OK = True
except ImportError:
    _PROM_OK = False

VERSION      = "0.32.0"
DB_PATH      = os.environ.get("DB_PATH", "/data/gpu.db")
MCP_IDLE_SEC = 45   # seconds without MCP activity before the pill shows idle
INTERVAL     = int(os.environ.get("SAMPLE_INTERVAL", "10"))
# ── The fast lane ────────────────────────────────────────────────────────────
# INTERVAL is the *storage* cadence, and it must stay that way: every energy and
# cost figure in this project is `sum(watts) * INTERVAL / 3_600_000`, so a sample
# that lands in the database at any other spacing is silently mispriced.
#
# FAST_INTERVAL is the *screen* cadence. A separate loop re-reads only the cheap
# values — /proc for CPU/RAM/load/temperature, one nvidia-smi query for the cards
# already discovered — refreshes them in LATEST and wakes the SSE stream. It never
# writes a row, so history stays exactly as dense as it was, cost stays exact, and
# the expensive half of a sample (Docker, model-server probes, caller attribution,
# per-process VRAM) keeps running once per INTERVAL as before.
#
# 0 disables the fast lane entirely and the dashboard falls back to polling.
FAST_INTERVAL = max(0, int(os.environ.get("FAST_INTERVAL", "2") or 0))
if FAST_INTERVAL >= INTERVAL:
    # A fast lane no faster than the sampler buys nothing and doubles the reads.
    FAST_INTERVAL = 0
_RETENTION_DAYS_DEFAULT = int(os.environ.get("RETENTION_DAYS", "180"))
RETENTION    = _RETENTION_DAYS_DEFAULT * 86400
_DISK_IO_RETENTION = 7 * 86400   # per-device disk-I/O history: dense, 7-day ring
_PROC_IO_RETENTION = 72 * 3600   # per-process I/O ring: short, spike-attribution only
DOCKER_SOCK  = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
HOST_ROOT    = os.environ.get("HOST_ROOT", "/rootfs")          # host / mounted read-only (optional)
PORT         = int(os.environ.get("PORT", "9800"))
PRESSURE_MB  = int(os.environ.get("PRESSURE_FREE_MB", "2048"))
CHECK_UPDATES = os.environ.get("CHECK_UPDATES", "true").strip().lower() not in ("0", "false", "no", "off")
# Per-host "a newer OS release exists" check. The probe reads pending *package*
# updates offline; detecting a whole new distro release needs the network, so the
# hub does it centrally (endoflife.date). Air-gapped users can switch off just
# this network lookup and keep the offline package counts.
CHECK_OS_UPDATES = os.environ.get("CHECK_OS_UPDATES", "true").strip().lower() not in ("0", "false", "no", "off")
UPDATE_REPO   = os.environ.get("UPDATE_REPO", "SikamikanikoBG/homelab-monitor")
# One-click self-update button. ON by default (recreates this very container
# via a detached docker:cli helper and restarts the app) — set
# ALLOW_SELF_UPDATE=0 to turn it off. Needs the docker socket mounted
# read-write, which the shipped docker-compose.yml now does by default too.
# See start_self_update() and website/configuration.md.
ALLOW_SELF_UPDATE = os.environ.get("ALLOW_SELF_UPDATE", "1").strip().lower() not in ("0", "false", "no", "off")
SELF_UPDATE_HELPER_IMAGE = os.environ.get("SELF_UPDATE_HELPER_IMAGE", "docker:cli")
# Container/service controls (start/stop/restart, restart policy). ON by
# default alongside self-update — set ENABLE_CONTROLS=0 to turn it off (see
# docker-compose.readonly.yml for restoring the old fully-read-only posture,
# sockets included). Local container/service control needs the docker socket
# and the systemd D-Bus socket mounted read-write, which the shipped
# docker-compose.yml now does by default. Gates every mutating route in this
# section; read-only collection (collect_docker/collect_systemd) is unaffected.
# See website/configuration.md.
ENABLE_CONTROLS = os.environ.get("ENABLE_CONTROLS", "1").strip().lower() not in ("0", "false", "no", "off")
# Split cache: once we know there's an update, the answer won't change for hours
# so we can cache it long. But "no update found" / network errors should expire
# sooner — otherwise a release published right after deploy stays invisible for
# the full 6h, and a transient GitHub blip sticks for the same window.
UPDATE_TTL_POSITIVE = 6 * 3600
UPDATE_TTL_NEGATIVE = 10 * 60   # re-check for a new release every 10 min (was 30)
MAX_POINTS   = 360
# Multi-machine monitoring (Issue #35, slice 1: registry + probe). The hub's own
# SSH key lives under SSH_DIR — it's inside /data so it persists across rebuilds
# the same way the SQLite history does. Capability probes run via the system
# `ssh` client (installed in the Dockerfile).
SSH_DIR         = os.environ.get("SSH_DIR", "/data/.ssh")
SSH_KEY         = os.path.join(SSH_DIR, "id_ed25519")
SSH_KNOWN_HOSTS = os.path.join(SSH_DIR, "known_hosts")
SSH_CONNECT_TIMEOUT = 5
HEX64        = re.compile(r"[0-9a-f]{64}")
OOM_RE       = re.compile(r"(out of memory|cuda error: out of memory|failed to allocate|bfcarena|"
                          r"cudamalloc|outofmemory|cublas_status_alloc_failed|cuda_error_out_of_memory)", re.I)
REAL_FS      = {"ext4", "ext3", "xfs", "btrfs", "zfs", "vfat"}

app = Flask(__name__, static_url_path="/static", static_folder="static")

# Phase 3.4: register API blueprints (in original @app.route declaration order)
from backend.api.system import bp as _system_bp
from backend.api.gpu import bp as _gpu_bp
from backend.api.gpu_cockpit import bp as _gpu_cockpit_bp
from backend.api.costs import bp as _costs_bp
from backend.api.experiments import bp as _experiments_bp
from backend.api.uptime_api import bp as _uptime_api_bp
from backend.api.hosts_api import bp as _hosts_api_bp
from backend.api.integrations import bp as _integrations_bp
from backend.api.benchmarks import bp as _benchmarks_bp
app.register_blueprint(_system_bp)
app.register_blueprint(_gpu_bp)
app.register_blueprint(_gpu_cockpit_bp)
app.register_blueprint(_costs_bp)
app.register_blueprint(_experiments_bp)
app.register_blueprint(_uptime_api_bp)
app.register_blueprint(_hosts_api_bp)
app.register_blueprint(_integrations_bp)
app.register_blueprint(_benchmarks_bp)

# ── Prometheus gauges (defined once at module level) ──────────────────────────
_GAUGES: dict = {}
def _make_gauge(name, doc, labels=None):
    """Create a Gauge once; return the cached instance on re-import (safe for multi-import)."""
    if name in _GAUGES:
        return _GAUGES[name]
    try:
        g = Gauge(name, doc, labels or [])
    except (ValueError, AttributeError):
        # ValueError  — already in the global REGISTRY (Flask debug-reloader or double-import).
        # AttributeError — prometheus_client internals renamed (version mismatch).
        # Recover by scanning the registry; _names_to_collectors is private so we guard
        # the whole block and raise loudly if we still can't find it — better than
        # caching None and getting AttributeError later on .set() / .clear().
        try:
            from prometheus_client import REGISTRY
            g = next((c for c in REGISTRY._names_to_collectors.values()
                      if getattr(c, "_name", None) == name), None)
        except Exception:
            g = None
        if g is None:
            raise RuntimeError(
                f"prometheus_client: could not recover gauge {name!r} from REGISTRY "
                "after duplicate-registration — check for double-import or version mismatch"
            )
    _GAUGES[name] = g
    return g

if _PROM_OK:
    _G = {
        "gpu_vram_used":     _make_gauge("homelab_gpu_vram_used_mb",    "GPU VRAM used (MB)",                ["gpu"]),
        "gpu_vram_total":    _make_gauge("homelab_gpu_vram_total_mb",   "GPU VRAM total (MB)",               ["gpu"]),
        "gpu_util":          _make_gauge("homelab_gpu_util_pct",        "GPU utilisation (%)",               ["gpu"]),
        "gpu_temp":          _make_gauge("homelab_gpu_temp_c",          "GPU temperature (°C)",              ["gpu"]),
        "gpu_power":         _make_gauge("homelab_gpu_power_w",         "GPU power draw (W)",                ["gpu"]),
        "host_cpu":          _make_gauge("homelab_host_cpu_pct",        "Host CPU usage (%)"),
        "host_cpu_power":    _make_gauge("homelab_host_cpu_power_w",    "Host CPU package power, RAPL (W)"),
        "host_dram_power":   _make_gauge("homelab_host_dram_power_w",   "Host DRAM power, RAPL (W)"),
        "host_mem_used":     _make_gauge("homelab_host_mem_used_pct",   "Host memory used (%)"),
        "host_disk_used":    _make_gauge("homelab_host_disk_used_pct",  "Host disk used (%)",                ["mountpoint"]),
        "container_state":   _make_gauge("homelab_container_state",     "Container state (1=running)",       ["name", "state"]),
        "systemd_unit":      _make_gauge("homelab_systemd_unit_state",  "Systemd unit state (1=active)",     ["unit",  "state"]),
        "model_vram":        _make_gauge("homelab_model_loaded_vram_mb","Model VRAM loaded (MB)",             ["server", "model"]),
        "model_ram":         _make_gauge("homelab_model_ram_spill_mb", "Model spill into system RAM (MB)",   ["server", "model"]),
        "models_installed":  _make_gauge("homelab_models_installed_total","AI models detected per provider (#219: loaded + idle catalogue)", ["provider"]),
    }
LOCK = threading.Lock()
_DB_MAINTENANCE = False   # True during backup/restore — collector skips DB writes
_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples(ts INTEGER PRIMARY KEY, util REAL, mem_used REAL, mem_total REAL, power REAL, temp REAL);
-- Per-card GPU history for EVERY host, hub included (the hub stores itself as
-- host='local'). One table rather than a hub table plus a remote one: the GPU
-- cockpit renders local and remote through the same reader, and a second table
-- would let the two drift. Pre-existing rows predate the host column and are
-- migrated to 'local', which is exactly what they were.
CREATE TABLE IF NOT EXISTS gpu_samples(ts INTEGER, idx INTEGER, util REAL, mem_used REAL, mem_total REAL, power REAL, temp REAL);
-- Hourly per-card rollup. Rates are averaged, but temp/fan also keep a MAX:
-- an hour that averaged 71 °C while peaking at 87 °C is an hour with a thermal
-- problem, and an average would hide exactly the event worth seeing.
-- throttle_secs counts the seconds the card reported a thermal/power throttle.
CREATE TABLE IF NOT EXISTS gpu_samples_1h(
  ts INTEGER NOT NULL, host TEXT NOT NULL DEFAULT 'local', idx INTEGER NOT NULL,
  util REAL, mem_used REAL, mem_total REAL, power REAL,
  temp REAL, temp_max REAL, fan REAL, fan_max REAL,
  -- fan_cnt: how many of this hour's polls actually REPORTED a fan speed. The
  -- fan average divides by this, not by cnt — otherwise a card that reports a
  -- fan only intermittently has its average dragged toward zero, and near-zero
  -- is precisely what the fan-stall alert fires on.
  fan_cnt INTEGER DEFAULT 0,
  throttle_secs INTEGER DEFAULT 0, cnt INTEGER DEFAULT 1,
  PRIMARY KEY(ts, host, idx));
CREATE INDEX IF NOT EXISTS idx_gpu_1h_host_ts ON gpu_samples_1h(host, ts);
CREATE TABLE IF NOT EXISTS net_samples(ts INTEGER, iface TEXT, bytes_in INTEGER, bytes_out INTEGER);
CREATE TABLE IF NOT EXISTS proc(ts INTEGER, service TEXT, mem REAL);
CREATE TABLE IF NOT EXISTS models(ts INTEGER, service TEXT, model TEXT, vram REAL, ram REAL);
CREATE INDEX IF NOT EXISTS idx_models_ts ON models(ts);
CREATE TABLE IF NOT EXISTS edges(ts INTEGER, caller TEXT, server TEXT, conns INTEGER);
CREATE INDEX IF NOT EXISTS idx_edges_ts ON edges(ts);
CREATE TABLE IF NOT EXISTS events(ts INTEGER, service TEXT, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS disk_io_samples(ts INTEGER NOT NULL, device TEXT NOT NULL, read_mb_s REAL, write_mb_s REAL, util_pct REAL);
CREATE INDEX IF NOT EXISTS idx_diskio_ts ON disk_io_samples(device, ts);
CREATE TABLE IF NOT EXISTS proc_io_samples(ts INTEGER NOT NULL, pid INTEGER, comm TEXT, read_bps INTEGER, write_bps INTEGER);
CREATE INDEX IF NOT EXISTS idx_procio_ts ON proc_io_samples(ts);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS hosts(
  name TEXT PRIMARY KEY,
  ssh_target TEXT NOT NULL,
  tags TEXT DEFAULT '',
  added_at INTEGER NOT NULL,
  last_check_at INTEGER,
  last_check_json TEXT
);
CREATE TABLE IF NOT EXISTS power_proc(ts INTEGER NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, watts REAL NOT NULL);
-- External uptime checks (HTTP/TCP endpoint monitors): the user's OWN configured HTTP/TCP
-- endpoint monitors. `target` is a URL (http) or host:port (tcp) — treated like
-- webhook_url (may carry credentials): persisted for the user, never logged.
-- These are PRIVATE (LAN dashboard + authed API only); they never reach /status.
-- alerts_enabled/fail_threshold/latency_warn_ms drive the per-check smart alerting.
CREATE TABLE IF NOT EXISTS uptime_checks(
  id TEXT PRIMARY KEY, label TEXT NOT NULL, type TEXT NOT NULL DEFAULT 'http',
  target TEXT NOT NULL, interval_sec INTEGER NOT NULL DEFAULT 60,
  timeout_sec INTEGER NOT NULL DEFAULT 10, expected_status INTEGER,
  alerts_enabled INTEGER NOT NULL DEFAULT 1, fail_threshold INTEGER NOT NULL DEFAULT 2,
  latency_warn_ms INTEGER,
  enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS uptime_results(
  check_id TEXT NOT NULL, ts INTEGER NOT NULL, up INTEGER NOT NULL,
  latency_ms REAL, code INTEGER, err TEXT);
CREATE INDEX IF NOT EXISTS idx_uptime_results ON uptime_results(check_id, ts);
CREATE INDEX IF NOT EXISTS idx_gpus_ts   ON gpu_samples(ts);
CREATE INDEX IF NOT EXISTS idx_net_ts    ON net_samples(ts);
CREATE INDEX IF NOT EXISTS idx_proc_ts   ON proc(ts);
CREATE INDEX IF NOT EXISTS idx_models_ts ON models(ts);
CREATE INDEX IF NOT EXISTS idx_edges_ts  ON edges(ts);
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL,
  started_at INTEGER NOT NULL, ended_at INTEGER, host TEXT, params TEXT, tags TEXT,
  notes TEXT, heartbeat_at INTEGER, ext_id TEXT, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS run_metrics(run_id TEXT NOT NULL, ts INTEGER NOT NULL,
  step INTEGER DEFAULT 0, key TEXT NOT NULL, value REAL NOT NULL);
CREATE TABLE IF NOT EXISTS api_keys(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE, prefix TEXT NOT NULL,
  created_at INTEGER NOT NULL, expires_at INTEGER, last_used_at INTEGER);
CREATE TABLE IF NOT EXISTS notification_rules(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  match_kind TEXT NOT NULL DEFAULT 'container',
  match_pattern TEXT NOT NULL DEFAULT '*',
  channel TEXT NOT NULL DEFAULT 'all',
  min_level TEXT NOT NULL DEFAULT 'warning',
  enabled INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_powerproc_ts   ON power_proc(ts);
CREATE INDEX IF NOT EXISTS idx_powerproc_name ON power_proc(name, ts);
CREATE INDEX IF NOT EXISTS idx_runs_started   ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runmetrics_rid ON run_metrics(run_id, key, ts);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_runs_ext ON runs(source, ext_id);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_event ON events(ts, service, kind);
-- Maintenance windows: silence alerts for selected checks/services during planned work.
-- kind matches the alert key prefix (container, systemd, uptime, disk, gpu, host).
-- pattern is fnmatch-style (* = all). recurrence: null=one-off, 'daily', 'weekly'.
CREATE TABLE IF NOT EXISTS maintenance_windows(
  id TEXT PRIMARY KEY, label TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT '*', pattern TEXT NOT NULL DEFAULT '*',
  start_ts INTEGER NOT NULL, end_ts INTEGER NOT NULL,
  recurrence TEXT, note TEXT, created_at INTEGER NOT NULL);
-- Phase 1.2a: per-minute and per-hour rollup tables (additive; raw tables unchanged)
CREATE TABLE IF NOT EXISTS samples_1m (
  ts        INTEGER PRIMARY KEY,
  util      REAL,
  mem_used  REAL,
  mem_total REAL,
  power     REAL,
  temp      REAL,
  cnt       INTEGER DEFAULT 1,
  cpu       REAL,
  ram_used  REAL,
  ram_total REAL,
  load1     REAL,
  ctemp     REAL,
  cpu_power REAL,
  dram_power REAL
);
CREATE TABLE IF NOT EXISTS samples_1h (
  ts        INTEGER PRIMARY KEY,
  util      REAL,
  mem_used  REAL,
  mem_total REAL,
  power     REAL,
  temp      REAL,
  cnt       INTEGER DEFAULT 1,
  cpu       REAL,
  ram_used  REAL,
  ram_total REAL,
  load1     REAL,
  ctemp     REAL,
  cpu_power REAL,
  dram_power REAL
);
CREATE TABLE IF NOT EXISTS net_samples_1m (
  ts        INTEGER PRIMARY KEY,
  bytes_in  REAL,
  bytes_out REAL,
  cnt       INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS net_samples_1h (
  ts        INTEGER PRIMARY KEY,
  bytes_in  REAL,
  bytes_out REAL,
  cnt       INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_samples_1m_ts     ON samples_1m(ts);
CREATE INDEX IF NOT EXISTS idx_samples_1h_ts     ON samples_1h(ts);
CREATE INDEX IF NOT EXISTS idx_net_samples_1m_ts ON net_samples_1m(ts);
CREATE INDEX IF NOT EXISTS idx_net_samples_1h_ts ON net_samples_1h(ts);
-- Per-host time-series (multi-host slice): one raw row per successful host poll
-- plus an hourly rollup keyed (ts, host) — the same raw/1h split the hub uses
-- for its own samples. Raw rows are retention-purged; the 1h rollup is kept
-- (like samples_1h) and is what the Costs integration reads.
CREATE TABLE IF NOT EXISTS host_samples(
  ts INTEGER NOT NULL, host TEXT NOT NULL,
  cpu REAL, ram_used REAL, ram_total REAL, load1 REAL, ctemp REAL,
  gpu_util REAL, gpu_mem_used REAL, gpu_mem_total REAL,
  gpu_power REAL, cpu_power REAL, dram_power REAL, gpu_temp REAL,
  PRIMARY KEY(ts, host)
);
CREATE INDEX IF NOT EXISTS idx_host_samples_host_ts ON host_samples(host, ts);
CREATE TABLE IF NOT EXISTS host_samples_1h(
  ts INTEGER NOT NULL, host TEXT NOT NULL,
  cpu REAL, ram_used REAL, ram_total REAL, load1 REAL, ctemp REAL,
  gpu_util REAL, gpu_mem_used REAL, gpu_mem_total REAL,
  gpu_power REAL, cpu_power REAL, dram_power REAL, gpu_temp REAL,
  cnt INTEGER DEFAULT 1,
  PRIMARY KEY(ts, host)
);
CREATE INDEX IF NOT EXISTS idx_host_samples_1h_host_ts ON host_samples_1h(host, ts);
CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at INTEGER NOT NULL
);
-- LLM Benchmark Lab (active, opt-in): one bench_runs row per (model, execution),
-- one bench_points row per (context-size / gpu-layers) measurement. Stored so a
-- benchmark need not be re-run often; a rerun simply inserts a fresh run (history).
CREATE TABLE IF NOT EXISTS bench_runs(
  id TEXT PRIMARY KEY, host TEXT, endpoint TEXT, model TEXT NOT NULL,
  family TEXT, param_size TEXT, quant TEXT, size_bytes INTEGER,
  status TEXT NOT NULL, config TEXT, summary TEXT, gpu TEXT, error TEXT,
  created_at INTEGER NOT NULL, started_at INTEGER, ended_at INTEGER,
  energy_kwh REAL, cost REAL, avg_w REAL);
CREATE TABLE IF NOT EXISTS bench_points(
  run_id TEXT NOT NULL, ctx INTEGER, num_gpu INTEGER,
  gen_tps REAL, prompt_tps REAL, load_ms REAL, ttft_ms REAL, total_ms REAL,
  eval_count INTEGER, prompt_eval_count INTEGER,
  vram_mb REAL, ram_offload_mb REAL, total_size_mb REAL, gpu_fraction REAL,
  fit TEXT, gpus TEXT, ok INTEGER, err TEXT);
CREATE INDEX IF NOT EXISTS idx_bench_points_run ON bench_points(run_id);
CREATE INDEX IF NOT EXISTS idx_bench_runs_model ON bench_runs(model, created_at);
"""
# cpu_power/dram_power: measured CPU package / DRAM watts via RAPL (#costs). NULL when unavailable.
_SAMPLE_MIGRATIONS = ("cpu REAL", "ram_used REAL", "ram_total REAL", "load1 REAL", "ctemp REAL",
                      "cpu_power REAL", "dram_power REAL")
# Per-host adaptive poll-timeout state (issue #99); added to the hosts table.
_HOST_MIGRATIONS = ("poll_timeout INTEGER", "poll_fails INTEGER DEFAULT 0", "poll_calibrated_at INTEGER")
# Which API key pushed a run (for per-key attribution); added to the runs table.
_RUNS_MIGRATIONS = ("key_id TEXT",)
_UPTIME_MIGRATIONS = ("cert_days_remaining INTEGER", "cert_expires_at INTEGER")
# Per-check opt-in to the public status page (off by default).
_UPTIME_CHECK_MIGRATIONS = ("public INTEGER NOT NULL DEFAULT 0",)
# RAM-spill split for loaded models (ollama size - size_vram). NULL = unknown (non-ollama).
_MODELS_MIGRATIONS = ("ram REAL",)
# Per-card GPU history for every host (the GPU cockpit). `host` turns what was a
# hub-only table into a fleet-wide one; existing rows are the hub's, so they
# default to 'local' and stay valid. The rest is the telemetry the cockpit charts:
# fan speed, memory-bandwidth utilisation, clocks, the power cap the draw is
# measured against, memory-junction temp and the raw throttle bitmask (kept as a
# mask, not as labels, so history can tell thermal throttling from power-capping
# without re-parsing strings).
_GPU_SAMPLE_MIGRATIONS = ("host TEXT NOT NULL DEFAULT 'local'", "fan REAL", "mem_util REAL",
                          "clk_sm REAL", "clk_mem REAL", "power_limit REAL",
                          "temp_mem REAL", "throttle INTEGER")
# Same story for per-service VRAM: `proc` was implicitly the hub's own, so its
# rows are 'local' and remotes now write alongside them.
_PROC_MIGRATIONS = ("host TEXT NOT NULL DEFAULT 'local'",)
# GPU temperature per host: host_samples has always pooled GPU util/VRAM/power
# but silently dropped temperature, so a remote's thermal history simply didn't
# exist. Both the raw table and the rollup gain it.
_COLUMN_MIGRATIONS = (("host_samples", "gpu_temp REAL"),
                      ("host_samples_1h", "gpu_temp REAL"),
                      ("gpu_samples_1h", "fan_cnt INTEGER DEFAULT 0"))
# Indexes that cover columns added by the migrations above. They cannot live in
# _DB_SCHEMA: executescript runs BEFORE the ALTERs, so on an existing database
# the column wouldn't exist yet and the whole script would fail.
_POST_MIGRATION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_gpus_host_ts ON gpu_samples(host, ts)",
    "CREATE INDEX IF NOT EXISTS idx_proc_host_ts ON proc(host, ts)",
)

def _data_dir():
    return os.path.dirname(os.path.abspath(DB_PATH)) or "."

def _data_dir_writable():
    d = _data_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return False
    return os.access(d, os.W_OK)

from backend.db.repos.schema import (
    open_db_connection as _open_db_connection,
    apply_schema_migrations as _apply_schema_migrations_impl,
    record_baseline_if_needed as _record_baseline_if_needed,
)

_EDGE_STATE_MIGRATION = """
CREATE TABLE IF NOT EXISTS notified_keys (
    key TEXT PRIMARY KEY,
    armed_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS uptime_down_since (
    check_id TEXT PRIMARY KEY,
    since_ts INTEGER NOT NULL
);
"""

def _apply_schema_migrations(conn):
    _apply_schema_migrations_impl(conn, _DB_SCHEMA,
                                  _SAMPLE_MIGRATIONS, _HOST_MIGRATIONS,
                                  _RUNS_MIGRATIONS, _UPTIME_MIGRATIONS,
                                  _UPTIME_CHECK_MIGRATIONS,
                                  models_migrations=_MODELS_MIGRATIONS,
                                  gpu_sample_migrations=_GPU_SAMPLE_MIGRATIONS,
                                  proc_migrations=_PROC_MIGRATIONS,
                                  column_migrations=_COLUMN_MIGRATIONS,
                                  post_migration_indexes=_POST_MIGRATION_INDEXES)
    conn.executescript(_EDGE_STATE_MIGRATION)

def _backfill_rollups(conn):
    """Populate rollup tables from existing raw data (idempotent: INSERT OR IGNORE)."""
    conn.executescript("""
        INSERT OR IGNORE INTO samples_1m(ts,util,mem_used,mem_total,power,temp,cnt,cpu,ram_used,ram_total,load1,ctemp,cpu_power,dram_power)
        SELECT (ts/60)*60, AVG(util), AVG(mem_used), AVG(mem_total), AVG(power), AVG(temp), COUNT(*),
               AVG(cpu), AVG(ram_used), AVG(ram_total), AVG(load1), AVG(ctemp), AVG(cpu_power), AVG(dram_power)
        FROM samples GROUP BY (ts/60)*60;

        INSERT OR IGNORE INTO samples_1h(ts,util,mem_used,mem_total,power,temp,cnt,cpu,ram_used,ram_total,load1,ctemp,cpu_power,dram_power)
        SELECT (ts/3600)*3600, AVG(util), AVG(mem_used), AVG(mem_total), AVG(power), AVG(temp), COUNT(*),
               AVG(cpu), AVG(ram_used), AVG(ram_total), AVG(load1), AVG(ctemp), AVG(cpu_power), AVG(dram_power)
        FROM samples GROUP BY (ts/3600)*3600;

        INSERT OR IGNORE INTO net_samples_1m(ts,bytes_in,bytes_out,cnt)
        SELECT (ts/60)*60, AVG(bytes_in), AVG(bytes_out), COUNT(*)
        FROM net_samples GROUP BY (ts/60)*60;

        INSERT OR IGNORE INTO net_samples_1h(ts,bytes_in,bytes_out,cnt)
        SELECT (ts/3600)*3600, AVG(bytes_in), AVG(bytes_out), COUNT(*)
        FROM net_samples GROUP BY (ts/3600)*3600;
    """)
    conn.commit()

def reopen_db():
    """Close and reopen the global DB handle after a restore (same path, new file)."""
    global DB, DB_EPHEMERAL
    try:
        DB.close()
    except Exception:
        pass
    os.makedirs(_data_dir(), exist_ok=True)
    DB = _open_db_connection(DB_PATH)
    _apply_schema_migrations(DB)
    _backfill_rollups(DB)
    DB_EPHEMERAL = False

# Open the history DB, but never let a missing/unwritable /data mount kill the
# whole container at import — a newbie who forgets the `./data:/data` mount should
# still get a running dashboard (with a diagnostics warning), not a crash loop.
# Fall back to an in-memory DB so the app boots; history just doesn't persist.
DB_EPHEMERAL = False
try:
    DB = _open_db_connection(DB_PATH)
except sqlite3.OperationalError as e:
    print(f"WARNING: cannot open DB at {DB_PATH} ({e}); "
          f"falling back to in-memory (history will not persist). "
          f"Mount a writable ./data:/data to keep history.", flush=True)
    DB = _open_db_connection(":memory:")
    DB_EPHEMERAL = True
_apply_schema_migrations(DB)
_backfill_rollups(DB)

LATEST = {"ts": 0, "util": 0, "mem_used": 0, "mem_total": 24576, "power": 0, "temp": 0,
          "cpu_power": None, "dram_power": None,
          "procs": [], "models": [], "callers": [], "host": {}, "gpu_avail": None, "gpu_vendor": None, "gpus": [], "gpu_extra": {},
          "model_meta": {}, "serving": [], "training": [], "devtools": [], "model_catalog": []}
# ── Live revision: the "something changed" signal the SSE stream rides on ────
# Bumped by every producer that refreshes LATEST (the sampler, the fast lane) and
# by the fleet poller. Readers wait on the condition rather than polling it, so an
# idle stream costs one blocked thread and zero CPU, and a new sample reaches the
# browser in the time it takes to serialize it.
#
# Lock order: LIVE_COND is a leaf. Never acquire LOCK or HOST_DATA_LOCK while
# holding it — bump_live() touches nothing else for exactly that reason.
LIVE_REV  = 0
FLEET_REV = 0
LIVE_COND = threading.Condition()

def bump_live():
    """Publish 'LATEST changed' to every waiting SSE stream."""
    global LIVE_REV
    with LIVE_COND:
        LIVE_REV += 1
        LIVE_COND.notify_all()

def bump_fleet():
    """Publish 'a remote host's data changed'. Separate from bump_live() so the
    stream can send the fleet table only when the fleet actually moved, instead
    of re-shipping every host on every 2 s local tick."""
    global FLEET_REV
    with LIVE_COND:
        FLEET_REV += 1
        LIVE_COND.notify_all()

def fleet_payload():
    """Compact per-host summary rows for the All-hosts table: local first, then
    registered hosts in the order they were added. Served by /api/fleet and
    pushed on the SSE `fleet` event — one builder, so poll and push can't drift."""
    rows = [{"name": "local", "label": socket.gethostname() + " (this hub)",
             "ssh_target": None, "host": enrich_os_upgrade(_local_now_snapshot()),
             "at": int(time.time()), "online": True, "is_local": True,
             "last_check": {"summary": {"overall": "ok"}}}]
    hosts = list_hosts()
    with HOST_DATA_LOCK:
        for h in hosts:
            entry = HOST_DATA.get(h["name"]) or {}
            data  = entry.get("data") or {}
            rows.append({
                "name": h["name"],
                "label": h["name"],
                "ssh_target": h["ssh_target"],
                "host": enrich_os_upgrade(data.get("host")) if data else None,
                "at": entry.get("at"),
                "online": _host_is_online(entry),
                "is_local": False,
                "last_check": h.get("last_check"),
                "error": entry.get("error"),
            })
    return {"hosts": rows, "interval": INTERVAL, "rev": FLEET_REV}

def live_payload():
    """The small live-values document: what /api/data returns as `now`, and
    nothing else. No database work, no LOCK — this is the endpoint that has to
    stay cheap enough to serve every couple of seconds.

    LATEST is copied before serializing: the sampler updates it without holding
    LOCK (it always has), so iterating the live dict could otherwise trip over a
    key being added mid-serialization."""
    now = live_now()
    return {"version": VERSION, "rev": LIVE_REV, "interval": INTERVAL,
            "fast_interval": FAST_INTERVAL,
            "mem_total": now.get("mem_total") or 24576, "now": now}

def live_now():
    """LATEST as clients see it — the one place the hub's live block is prepared.

    /api/data and /api/live (and the SSE `now` event behind it) both ship this
    dict, and they used to reach for LATEST independently. Anything derived for
    one of them was then silently missing from the other, which is exactly how
    the AI Models tab ended up with a blank compute column while the GPU tab had
    the numbers.

    Derived here rather than at sample time so it can never go stale, and so a
    spec table that gains a card starts answering for it on the next request
    instead of the next poll.
    """
    from backend import gpuspec
    now = dict(LATEST)
    gpuspec.attach(now.get("gpus"))
    return now

# Where the recognised AI servers live ({name, ip, provider}) — kept OUTSIDE
# LATEST on purpose: LATEST is served wholesale as /api/data "now", and internal
# container IPs don't belong in a browser payload. Refreshed each sample.
AI_SERVERS = []
# Current state of the "status" monitors (Docker + systemd). The background
# collector refreshes these; /api/health just serves the cached snapshot.
HEALTH = {"docker": None, "systemd": None, "update": None, "processes": None, "at": 0}
WATCH_SERVICES = [s.strip() for s in os.environ.get("WATCH_SERVICES", "").split(",") if s.strip()]
SYSTEMD_ADMIN_DIR = "/etc/systemd/system/"   # units here are admin/user-authored (vs vendor)
_ct_cache = {"list": [], "at": 0}
_scan_since = {}
_cpu_prev = {"idle": 0, "total": 0}
# The fast lane's own /proc/stat counters — see _cpu_pct() on why these can't be
# shared with the sampler's.
_cpu_prev_fast = {"idle": 0, "total": 0}

# ── Docker API over the unix socket ────────────────────────────────────────────
def _docker_req(method, path, body=None, timeout=8):
    """Talk to the Docker Engine API over its AF_UNIX socket with an arbitrary
    method + optional JSON body. Returns (status_code, raw_bytes). Used for the
    self-update flow, which has to POST (create/start the helper container) —
    _docker() below is the GET-only convenience wrapper everything else uses.
    close() runs even on error so the hand-made socket fd never leaks."""
    c = http.client.HTTPConnection("localhost", timeout=timeout)
    c.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        c.sock.settimeout(timeout); c.sock.connect(DOCKER_SOCK)
        headers, payload = {}, None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=payload, headers=headers)
        resp = c.getresponse()
        return resp.status, resp.read()
    finally:
        c.close()

def _docker(path):
    # GET-only wrapper kept for the many read-only callers below; returns the raw
    # body (they json.loads it). Shares the socket pattern via _docker_req.
    return _docker_req("GET", path, timeout=4)[1]

def containers():
    if time.time() - _ct_cache["at"] < 30 and _ct_cache["list"]:
        return _ct_cache["list"]
    out = []
    try:
        for ct in json.loads(_docker("/containers/json")):
            nets = (ct.get("NetworkSettings") or {}).get("Networks") or {}
            ip = next((n.get("IPAddress") for n in nets.values() if n.get("IPAddress")), None)
            ports = set()
            for pm in (ct.get("Ports") or []):
                if pm.get("PrivatePort"): ports.add(pm["PrivatePort"])
                if pm.get("PublicPort"):  ports.add(pm["PublicPort"])
            out.append({"id": ct["Id"][:12], "name": (ct.get("Names") or ["/?"])[0].lstrip("/"),
                        "image": ct.get("Image", ""), "ip": ip, "ports": sorted(ports)})
        _ct_cache.update(list=out, at=time.time())
    except Exception as e:
        print("docker list error:", e, flush=True)
    return _ct_cache["list"]

def logs_since(cid, since):
    try:
        raw = _docker(f"/containers/{cid}/logs?stdout=1&stderr=1&timestamps=1&tail=400&since={since}")
    except Exception:
        return ""
    if not raw:
        return ""
    if raw[0] in (0, 1, 2):
        out, i, n = [], 0, len(raw)
        while i + 8 <= n:
            size = int.from_bytes(raw[i + 4:i + 8], "big"); i += 8
            out.append(raw[i:i + size]); i += size
        raw = b"".join(out)
    return raw.decode("utf-8", "replace")

# Phase 3.1: model-server probes moved to backend/probes/ — re-exported for backward compat
from backend.probes import (
    _http_json, _openai_models,
    probe_ollama, probe_tgi, probe_koboldcpp, probe_invokeai, probe_a1111,
    probe_whisper_asr, probe_triton, probe_wyoming, probe_comfy,
    PROBES, _match_probe, _match_probe_key, CATALOG_MAX, probe_models,
)

# ── Model intelligence: per-model metadata + live serving telemetry ───────────
# Two passive, no-dep enrichments that make the AI Models tab authoritative:
#   1. Ollama /api/show — param size, quantization, context length, capabilities
#      (immutable per tag, so cached; only fetched for currently-loaded models).
#   2. vLLM / TGI /metrics — running/waiting requests, KV-cache fill, tokens/sec
#      (Prometheus text, parsed with stdlib regex; tok/s from a counter delta).
def _http_post_json(ip, port, path, payload, timeout=2):
    c = http.client.HTTPConnection(ip, port, timeout=timeout)
    try:
        body = json.dumps(payload).encode()
        c.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        r = c.getresponse(); data = r.read(); status = r.status
    finally:
        c.close()
    return json.loads(data) if status < 400 else None

def _http_text(ip, port, path, timeout=2):
    c = http.client.HTTPConnection(ip, port, timeout=timeout)
    try:
        c.request("GET", path); r = c.getresponse(); data = r.read(); status = r.status
    finally:
        c.close()
    return data.decode("utf-8", "replace") if status < 400 else None

_OLLAMA_META = {}   # model name -> {param_size, quant, ctx, caps, weights_mb}; immutable per tag, cached

# Per-IP cache of ollama's on-disk model sizes (/api/tags `size`). The GGUF file
# is what gets mapped into VRAM/RAM, so it ≈ the *weights* part of a loaded
# model's residency; total resident − weights = context/KV cache + compute
# buffers — the split that explains WHY a model spills into system RAM.
_OLLAMA_TAGS = {}            # ip -> {"at": ts, "sizes": {model_name: bytes}}
_OLLAMA_TAGS_TTL = 600       # steady-state refetch cadence
_OLLAMA_TAGS_MIN_GAP = 60    # floor between refetches when a loaded model is unknown

def _ollama_weights_mb(ip, model):
    """Weights footprint (MB) for an ollama model from the cached /api/tags size.
    Refetches at most once per _OLLAMA_TAGS_MIN_GAP when an unknown loaded model
    appears (fresh pull), else every _OLLAMA_TAGS_TTL. Never raises; a failed
    fetch keeps the previous sizes and backs off."""
    now = time.time()
    c = _OLLAMA_TAGS.get(ip)
    if (not c or now - c["at"] > _OLLAMA_TAGS_TTL
            or (model not in c["sizes"] and now - c["at"] > _OLLAMA_TAGS_MIN_GAP)):
        sizes = dict((c or {}).get("sizes") or {})
        try:
            d = _http_json(ip, 11434, "/api/tags", timeout=3) or {}
            sizes = {m["name"]: m["size"] for m in d.get("models", [])
                     if m.get("name") and m.get("size")}
        except Exception:
            pass                                   # keep old sizes, back off via "at"
        c = _OLLAMA_TAGS[ip] = {"at": now, "sizes": sizes}
    b = c["sizes"].get(model)
    return round(b / 1048576) if b else None

def _ollama_meta(ip, model):
    """POST /api/show once per model and cache. Passive — triggers no inference."""
    if model in _OLLAMA_META:
        return _OLLAMA_META[model]
    meta = {}
    try:
        d = _http_post_json(ip, 11434, "/api/show", {"model": model}) or {}
        det = d.get("details") or {}
        meta["param_size"] = det.get("parameter_size") or ""
        meta["quant"] = det.get("quantization_level") or ""
        ctx = ""
        for k, v in (d.get("model_info") or {}).items():
            if k.endswith(".context_length"):
                ctx = int(v) if str(v).isdigit() else v; break
        meta["ctx"] = ctx
        meta["caps"] = [c for c in (d.get("capabilities") or []) if c != "completion"]
    except Exception:
        meta = {}
    if any(meta.get(k) for k in ("param_size", "quant", "ctx", "caps")):
        _OLLAMA_META[model] = meta
    return meta

def collect_model_meta(ai, models):
    """{model_name: meta} for Ollama models. Cached metadata is returned for free;
    a fresh /api/show is only paid for a currently-loaded model we haven't seen."""
    ip_of = {ct["name"]: (ct.get("ip") or "127.0.0.1") for ct in ai}
    is_ollama = {ct["name"] for ct in ai
                 if "ollama" in (ct.get("name", "") + " " + ct.get("image", "")).lower()}
    out = {}
    for svc, mdl, vram, *_ in models:               # rows are (svc, mdl, vram, ram[, ctx])
        if svc not in is_ollama or not mdl:
            continue
        if mdl in _OLLAMA_META:
            out[mdl] = _OLLAMA_META[mdl]
        elif vram is not None:                      # only pay /api/show for loaded models
            meta = _ollama_meta(ip_of.get(svc, "127.0.0.1"), mdl)
            if meta:
                out[mdl] = meta
        # Weights split (cheap cached /api/tags): attach for loaded models still
        # missing it. Mutating the returned dict also fills the _OLLAMA_META cache
        # entry, so a transient tags failure heals on a later sample.
        m = out.get(mdl)
        if m is not None and vram is not None and "weights_mb" not in m:
            w = _ollama_weights_mb(ip_of.get(svc, "127.0.0.1"), mdl)
            if w:
                m["weights_mb"] = w
    return out

_PROM_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([0-9.eEnaN+-]+)\s*$")

def _parse_prom(text):
    """Prometheus exposition text -> {metric_name: value summed across label sets}.
    Comments and non-finite values are skipped."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] == "#":
            continue
        m = _PROM_RE.match(line)
        if not m:
            continue
        try:
            v = float(m.group(3))
        except ValueError:
            continue
        if v != v or v in (float("inf"), float("-inf")):   # NaN / inf
            continue
        out[m.group(1)] = out.get(m.group(1), 0.0) + v
    return out

def _first_metric(metrics, *names):
    for n in names:
        if n in metrics:
            return metrics[n]
    return None

def _serving_extract(metrics):
    """Pull the serving KPIs out of a parsed /metrics dict (vLLM + TGI aliases).
    Returns raw fields incl. the generation-tokens counter for rate calc upstream."""
    out = {}
    running = _first_metric(metrics, "vllm:num_requests_running", "tgi_batch_current_size")
    waiting = _first_metric(metrics, "vllm:num_requests_waiting", "tgi_queue_size")
    kv = _first_metric(metrics, "vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc")
    gen = _first_metric(metrics, "vllm:generation_tokens_total", "tgi_request_generated_tokens_sum")
    ts_sum = _first_metric(metrics, "vllm:time_to_first_token_seconds_sum")
    ts_cnt = _first_metric(metrics, "vllm:time_to_first_token_seconds_count")
    if running is not None: out["running"] = int(running)
    if waiting is not None: out["waiting"] = int(waiting)
    if kv is not None:      out["kv_cache_pct"] = round(kv * 100, 1) if kv <= 1.5 else round(kv, 1)
    if gen is not None:     out["gen_tokens_total"] = gen
    if ts_sum is not None and ts_cnt:
        out["ttft_avg_s"] = round(ts_sum / ts_cnt, 3)
    return out

_METRICS_HINTS = ("vllm", "text-generation-inference", "tgi", "sglang", "aphrodite",
                  "lorax", "mistral", "text-embeddings-inference", "infinity")
_SERVE_PREV = {}    # svc -> (gen_tokens_total, ts), for tokens/sec from the counter delta

def collect_serving(ai):
    """Scrape /metrics from Prometheus-exposing inference servers and return a list of
    live serving stats (running/waiting/KV-cache/tok-s/TTFT). Bounded: only hint-matched
    containers, a few candidate ports at a short timeout, first hit wins."""
    out, nowt = [], time.time()
    for ct in ai:
        name = ct.get("name", "")
        if not any(h in (name + " " + ct.get("image", "")).lower() for h in _METRICS_HINTS):
            continue
        ip = ct.get("ip") or "127.0.0.1"
        ports = []
        for p in (list(ct.get("ports") or []) + [8000]):
            if p and p not in ports:
                ports.append(p)
        text = None
        for p in ports[:4]:
            try:
                t = _http_text(ip, p, "/metrics", timeout=1.0)
            except Exception:
                t = None
            if t and ("vllm:" in t or "tgi_" in t):
                text = t; break
        if not text:
            continue
        st = _serving_extract(_parse_prom(text))
        gen = st.pop("gen_tokens_total", None)
        if gen is not None:
            prev = _SERVE_PREV.get(name)
            if prev and nowt > prev[1]:
                rate = (gen - prev[0]) / (nowt - prev[1])
                if rate >= 0:
                    st["tok_per_s"] = round(rate, 1)
            _SERVE_PREV[name] = (gen, nowt)
        if st:
            st["service"] = name
            out.append(st)
    return out

# ── AI-tab fast path: throttled on-demand re-probe of ollama's /api/ps ────────
# The sampler runs every INTERVAL (10s default) and the dashboard's global poll
# every 15s, so the AI tab could lag ~25s behind a load/unload. /api/ai/now
# serves a fresher view: re-probe just the ollama servers (one cheap HTTP call
# each, 2s timeout) at most every _AI_NOW_TTL seconds, merged over LATEST for
# everything else. No DB, no LOCK; network I/O happens outside any held lock.
_AI_NOW_LOCK = threading.Lock()
_AI_NOW_CACHE = {"at": 0.0, "models": None}
_AI_NOW_TTL = float(os.environ.get("AI_NOW_TTL", "3"))

def ai_models_now():
    """Return (models, probed_at) — LATEST.models with the ollama entries
    replaced by a just-probed view when the cache is stale. Shape matches
    LATEST['models'] rows exactly ({service, model, vram, ram, ctx_now})."""
    now = time.time()
    with _AI_NOW_LOCK:
        if _AI_NOW_CACHE["models"] is not None and now - _AI_NOW_CACHE["at"] < _AI_NOW_TTL:
            return list(_AI_NOW_CACHE["models"]), _AI_NOW_CACHE["at"]
    base = list(LATEST.get("models") or [])
    fresh = {}                                      # svc -> replacement rows
    for srv in list(AI_SERVERS):
        if srv.get("provider") != "ollama":
            continue
        try:
            rows = probe_ollama(srv.get("ip") or "127.0.0.1")
        except Exception:
            continue                                # keep the sampler's view for this svc
        if not rows:                                # unreachable/empty → don't blank the tab
            continue
        out = []
        for r in rows:                              # loaded rows are 4-wide, idle fallback 2-wide
            name, vram, ram, ctx = (tuple(r) + (None, None))[:4]
            loaded = vram is not None
            out.append({"service": srv["name"], "model": name,
                        "vram": round(vram) if loaded else None,
                        "ram": (round(ram) if ram else 0) if loaded else None,
                        "ctx_now": ctx if loaded else None})
        fresh[srv["name"]] = out
    models = [m for m in base if m.get("service") not in fresh]
    for out in fresh.values():
        models.extend(out)
    with _AI_NOW_LOCK:
        _AI_NOW_CACHE.update(at=now, models=models)
    return list(models), now

# ── Host metrics (read from /proc, /sys, statvfs — host values via shared kernel)
def _cpu_pct(state=None):
    """CPU busy % since this caller's previous reading.

    `state` carries the previous counters, and every concurrent caller MUST bring
    its own. /proc/stat is cumulative, so the figure is entirely a function of the
    delta between two reads: two loops sharing one state dict each consume the
    other's interval and both report a window neither of them meant to measure —
    the 10 s sampler would silently be storing a 2 s figure. The fast lane keeps
    _cpu_prev_fast for exactly this reason."""
    if state is None:
        state = _cpu_prev
    parts = list(map(int, open("/proc/stat").readline().split()[1:]))
    idle, total = parts[3] + parts[4], sum(parts)
    di, dt = idle - state["idle"], total - state["total"]
    state.update(idle=idle, total=total)
    return round(100 * (dt - di) / dt, 1) if dt > 0 and state["total"] else 0.0

def read_disks():
    # Read the *host* mount table (PID 1 lives in the host mount namespace), then
    # statvfs each real filesystem via the read-only host-root bind mount.
    base = HOST_ROOT if os.path.isdir(HOST_ROOT) else "/"
    mounts = "/proc/1/mounts" if os.path.exists("/proc/1/mounts") else "/proc/mounts"
    out, seen = [], set()
    try:
        lines = open(mounts).read().splitlines()
    except Exception:
        lines = []
    for ln in lines or ["/dev/root / ext4"]:
        f = ln.split()
        if len(f) < 3:
            continue
        dev, mp, fs = f[0], f[1].replace("\\040", " "), f[2]
        # De-dupe by MOUNTPOINT (like probe.py), not device: btrfs subvolumes and
        # ZFS datasets share one device (or a non-/dev/ pool name like `tank/data`),
        # so de-duping by device hid every subvolume after the first and the old
        # `/dev/` requirement dropped ZFS entirely. REAL_FS already gates the type.
        if fs not in REAL_FS or mp in seen:
            continue
        seen.add(mp)
        path = (base.rstrip("/") + mp) if base != "/" else mp
        try:
            st = os.statvfs(path)
            total = st.f_blocks * st.f_frsize
            if total == 0:
                continue
            used = total - st.f_bavail * st.f_frsize
            out.append({"mount": mp, "used": round(used / 1073741824, 1),
                        "total": round(total / 1073741824, 1), "pct": round(100 * used / total)})
        except Exception:
            pass
    return sorted(out, key=lambda d: -d["pct"])[:6]

_disk_prev = {}

# /proc/diskstats layout (Linux kernel Documentation/admin-guide/iostats.rst):
# each line is  <major> <minor> <name> f1 f2 f3 …  where the fields after the
# device name are 1-based columns. Mapping to our zero-based `parts` index:
#   parts[3]  = f1  reads completed
#   parts[5]  = f3  sectors read           (×512 B)
#   parts[6]  = f4  ms spent reading
#   parts[7]  = f5  writes completed
#   parts[9]  = f7  sectors written        (×512 B)
#   parts[10] = f8  ms spent writing
#   parts[12] = f10 ms spent doing I/O     (drives utilisation%)
_SECTOR_BYTES = 512

# Partition-name matcher for the summary rollup. A partition is a whole-disk name
# plus a trailing partition suffix, per the kernel's block-device naming:
#   • classic disks:   sdaN / vdaN / hdaN / xvdaN   (letters + trailing digits)
#   • nvme / mmc / md: nvme0n1pN / mmcblk0pN / md0pN (a digit, then 'p', digits)
# Whole disks (sda, nvme0n1, md0) do NOT match. md*/dm* aggregates are excluded
# separately by name prefix so their stacked members aren't double-counted.
_DISK_PART_RE = re.compile(r"^(?:sd|vd|hd|xvd)[a-z]+\d+$|^.+\dp\d+$")

def _is_physical_disk(dev):
    """True only for physical whole-disks — the set the SUMMARY should sum so that
    RAID/dm aggregates and partitions (which restate the same bytes) aren't
    triple-counted. Per-device rows keep every device; only the rollup uses this."""
    if dev.startswith("md") or dev.startswith("dm"):
        return False                       # RAID/device-mapper aggregate
    if _DISK_PART_RE.match(dev):
        return False                       # a partition of some whole disk
    return True

def collect_disk_io():
    """Per-device throughput (MB/s), utilisation (%) and avg op-latency (ms) from
    /proc/diskstats. First poll is a warm-up (no deltas yet). Never raises."""
    path = os.path.join(HOST_ROOT, "proc/diskstats") if os.path.exists(os.path.join(HOST_ROOT, "proc/diskstats")) else "/proc/diskstats"
    if not os.path.exists(path):
        return {"available": False, "warming_up": False, "reason": "no /proc/diskstats",
                "summary": {"total_read_mb_s": 0.0, "total_write_mb_s": 0.0}, "items": []}
    now = time.time()
    out = []
    try:
        with open(path, "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 14: continue
                dev = parts[2]
                if dev.startswith("loop") or dev.startswith("ram") or dev.startswith("sr"): continue
                try:
                    reads    = int(parts[3])
                    s_read   = int(parts[5])
                    ms_read  = int(parts[6])
                    writes   = int(parts[7])
                    s_write  = int(parts[9])
                    ms_write = int(parts[10])
                    ms_io    = int(parts[12])
                except (ValueError, IndexError):
                    continue
                prev = _disk_prev.get(dev)
                _disk_prev[dev] = (s_read, s_write, reads, writes, ms_read, ms_write, ms_io, now)
                if not prev:
                    continue                       # first poll for this device: warm up
                dt = now - prev[7]
                if dt <= 0:
                    continue
                rmb = ((s_read - prev[0]) * _SECTOR_BYTES) / 1048576.0 / dt
                wmb = ((s_write - prev[1]) * _SECTOR_BYTES) / 1048576.0 / dt
                d_reads, d_writes = reads - prev[2], writes - prev[3]
                d_msread, d_mswrite, d_msio = ms_read - prev[4], ms_write - prev[5], ms_io - prev[6]
                # utilisation: fraction of wall time the device had I/O in flight
                util = max(0.0, min(100.0, (d_msio / (dt * 1000.0)) * 100.0))
                # avg latency ms/op; guard the counter-wraps and div-by-zero -> None
                r_lat = round(d_msread / d_reads, 2) if d_reads > 0 and d_msread >= 0 else None
                w_lat = round(d_mswrite / d_writes, 2) if d_writes > 0 and d_mswrite >= 0 else None
                out.append({
                    "device": dev,
                    "read_mb_s":  round(max(0.0, rmb), 1),
                    "write_mb_s": round(max(0.0, wmb), 1),
                    "util_pct":   round(util, 1),
                    "read_lat_ms":  r_lat,
                    "write_lat_ms": w_lat,
                })
    except Exception:
        pass
    out.sort(key=lambda x: -(x["read_mb_s"] + x["write_mb_s"]))
    # Summary totals sum PHYSICAL whole-disks only: md/dm RAID aggregates and
    # partitions restate the same bytes as their spindles, so including them
    # overstates the headline KPI ~3-4x on stacked (md-RAID) hosts.
    phys = [x for x in out if _is_physical_disk(x["device"])]
    return {
        "available": bool(out),
        "warming_up": not out,
        "summary": {
            "total_read_mb_s":  round(sum(x["read_mb_s"]  for x in phys), 1),
            "total_write_mb_s": round(sum(x["write_mb_s"] for x in phys), 1),
        },
        "items": out,
        "at": int(now),
    }

# hwmon drivers / thermal-zone types that expose the real CPU die/core sensors.
_CPU_HWMON = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal")
_CPU_ZONE  = ("x86_pkg_temp", "cpu_thermal", "cpu-thermal")

def _cpu_temp_c():
    """CPU temperature in °C matching `sensors`' CPU cores. The old code took the
    max of every thermal zone, which on many boards grabs a chipset/PCH/NVMe or a
    mis-calibrated package sensor 10-20 °C above the cores (dashboard showed 51 °C
    while `sensors` showed Core N at 37 °C). Prefer the coretemp/k10temp hwmon and
    report the hottest *core*; then a CPU-typed thermal zone; then, as a last
    resort, the old hottest-plausible-zone so exotic/ARM boards still report.
    Mirrors probe.py's _cpu_temp_c so local and remote agree."""
    best = None
    try:
        for hw in glob.glob("/sys/class/hwmon/hwmon*"):
            try:
                name = open(hw + "/name").read().strip()
            except Exception:
                continue
            if name not in _CPU_HWMON:
                continue
            cores, allt = [], []
            for inp in glob.glob(hw + "/temp*_input"):
                try:
                    t = int(open(inp).read().strip()) / 1000.0
                except Exception:
                    continue
                if not (0 < t < 130):
                    continue
                allt.append(t)
                try:
                    lbl = open(inp[:-6] + "_label").read().strip().lower()
                except Exception:
                    lbl = ""
                if lbl.startswith("core"):          # Intel "Core N" — exclude Package
                    cores.append(t)
            pick = max(cores) if cores else (max(allt) if allt else None)
            if pick is not None and (best is None or pick > best):
                best = pick
        if best is not None:
            return round(best, 1)
    except Exception:
        pass
    try:
        for z in glob.glob("/sys/class/thermal/thermal_zone*"):
            try:
                ztype = open(z + "/type").read().strip().lower()
            except Exception:
                continue
            if ztype in _CPU_ZONE or "cpu" in ztype:
                try:
                    t = int(open(z + "/temp").read().strip()) / 1000.0
                except Exception:
                    continue
                if 0 < t < 130 and (best is None or t > best):
                    best = t
        if best is not None:
            return round(best, 1)
    except Exception:
        pass
    try:
        for z in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            try:
                t = int(open(z).read().strip()) / 1000.0
                if 10 < t < 130 and (best is None or t > best):
                    best = t
            except Exception:
                continue
    except Exception:
        pass
    return round(best, 1) if best is not None else None

# ── Network I/O sampling (issue #30) ──────────────────────────────────────────
def _read_net_dev(path):
    """Parse a /proc/net/dev file → {iface: (rx_bytes, tx_bytes)}, excluding lo
    and virtual veth/bridge churn we don't want as 'host' NICs. Cumulative byte
    counters; rates are derived on read so a missed sample never invents traffic."""
    out = {}
    try:
        with open(path) as f:
            for line in f.read().splitlines()[2:]:   # skip the two header rows
                if ":" not in line:
                    continue
                iface, rest = line.split(":", 1)
                iface = iface.strip()
                cols = rest.split()
                if iface == "lo" or len(cols) < 9:
                    continue
                out[iface] = (int(cols[0]), int(cols[8]))   # rx_bytes, tx_bytes
    except Exception:
        pass
    return out

# Per-container veth pairs and the dynamic docker-network bridges are internal
# plumbing — on a busy host there are dozens, and their traffic is already shown
# per-container in the Top-talkers table. Keep the host throughput view to real
# uplinks (eth*, en*, wl*, bond*, docker0, tailscale*, wg* …).
_HOST_NIC_SKIP = re.compile(r"^(veth|br-)")

def _net_rows(ts, nm):
    """Rows for net_samples this tick: host NICs from /proc/net/dev plus one row
    per container (its netns totals, read via a representative PID) tagged '@name'."""
    rows = []
    for iface, (rx, tx) in _read_net_dev("/proc/net/dev").items():
        if _HOST_NIC_SKIP.match(iface):
            continue
        rows.append((ts, iface, rx, tx))
    try:
        for name, pid in container_pids(nm).items():
            dev = _read_net_dev(f"/proc/{pid}/net/dev")
            if not dev:
                continue
            rx = sum(v[0] for v in dev.values()); tx = sum(v[1] for v in dev.values())
            rows.append((ts, "@" + name, rx, tx))
    except Exception:
        pass
    return rows

def read_host():
    h = {"cores": os.cpu_count() or 1}
    try: h["cpu"] = _cpu_pct()
    except Exception: h["cpu"] = 0
    try:
        mi = {}
        for ln in open("/proc/meminfo"):
            mi[ln.split(":")[0]] = int(ln.split()[1])
        h["ram_total"] = round(mi["MemTotal"] / 1024)
        h["ram_used"] = round((mi["MemTotal"] - mi.get("MemAvailable", mi.get("MemFree", 0))) / 1024)
        # Non-reclaimable kernel memory (slab/page-tables/stacks): part of "used" RAM
        # but attributed to no container or service, so the treemap can split it out of
        # "Host & other". SReclaimable is excluded (it counts as available, not used).
        h["ram_kernel"] = round((mi.get("SUnreclaim", 0) + mi.get("KernelStack", 0)
                                 + mi.get("PageTables", 0)) / 1024)
    except Exception:
        h["ram_total"] = h["ram_used"] = h["ram_kernel"] = 0
    try: h["load1"] = float(open("/proc/loadavg").read().split()[0])
    except Exception: h["load1"] = 0
    try: h["uptime"] = int(float(open("/proc/uptime").read().split()[0]))
    except Exception: h["uptime"] = 0
    h["ctemp"] = _cpu_temp_c()
    h["disks"] = read_disks()
    # Slow-changing context (OS / hardware / network / security) for the System,
    # Network and Security tabs. Cached so it isn't recomputed on every 10 s sample.
    h["os"]  = _cached_inv("os",  300, _local_os)
    h["hw"]  = _cached_inv("hw",  300, _local_hw)
    h["net"] = _cached_inv("net",  60, _local_net)
    h["sec"] = _cached_inv("sec", 300, _local_sec)
    return h

def read_host_fast():
    """The moving half of read_host(): four /proc reads and the temperature.

    Deliberately does NOT call read_disks() (a statvfs per mounted filesystem) or
    any of the _cached_inv inventories (OS/hardware/network/security) — those
    change on the scale of minutes to reboots, and re-reading them every couple of
    seconds is the entire cost this fast lane exists to avoid. The sampler keeps
    refreshing them at INTERVAL; the fast lane merges over the top and leaves them
    in place."""
    h = {}
    try: h["cpu"] = _cpu_pct(_cpu_prev_fast)
    except Exception: pass
    try:
        mi = {}
        for ln in open("/proc/meminfo"):
            mi[ln.split(":")[0]] = int(ln.split()[1])
        h["ram_total"] = round(mi["MemTotal"] / 1024)
        h["ram_used"] = round((mi["MemTotal"] - mi.get("MemAvailable", mi.get("MemFree", 0))) / 1024)
        h["ram_kernel"] = round((mi.get("SUnreclaim", 0) + mi.get("KernelStack", 0)
                                 + mi.get("PageTables", 0)) / 1024)
    except Exception:
        pass
    try: h["load1"] = float(open("/proc/loadavg").read().split()[0])
    except Exception: pass
    try: h["uptime"] = int(float(open("/proc/uptime").read().split()[0]))
    except Exception: pass
    try: h["ctemp"] = _cpu_temp_c()
    except Exception: pass
    return h

def gpu_cards_fast():
    """Refresh util/VRAM/power/temp/fan for the cards the sampler already found.

    One nvidia-smi query, and only the volatile fields — no compute-app
    attribution, no clock/throttle enrichment, no card discovery. Matching cards
    by index and updating in place is the point: appearing and disappearing cards
    are the sampler's business, so the fast lane can never invent a card, renumber
    the AMD block, or disagree with the per-card history in the database.

    Returns {idx: {field: value}} for the caller to merge. {} on any failure — a
    wedged driver degrades the refresh rate of the GPU chips, nothing else."""
    out = {}
    try:
        rows = smi(["--query-gpu=index,utilization.gpu,memory.used,power.draw,temperature.gpu",
                    "--format=csv,noheader,nounits"]).splitlines()
    except Exception:
        return {}
    for line in rows:
        if not line.strip():
            continue
        p = [x.strip() for x in line.split(",")]
        if len(p) < 5:
            continue
        try:
            idx = int(_gpu_num(p[0]))
        except (TypeError, ValueError):
            continue
        out[idx] = {"util": _gpu_num(p[1]), "mem_used": _gpu_num(p[2]),
                    "power": _gpu_num(p[3]), "temp": _gpu_num(p[4])}
    return out

# ── System / Hardware / Network / Security inventory (local hub) ──────────────
# Mirror of probe.py's read_os/hw/net/sec for the box the hub itself runs on.
# The container shares the host PID + network namespaces (pid:host,
# network_mode:host in docker-compose), so /proc, /proc/net and /sys/class/net
# are already the host's. Only the container's own /etc, /run, /var differ —
# those are read through the read-only host-root bind mount (HOST_ROOT) via _hp().
# The slim image has no ip/ss/ufw, so we read kernel files directly and fall back
# to config files; anything undeterminable is omitted/None and the UI shows a
# neutral placeholder. Everything is best-effort and never raises out.

_HR = HOST_ROOT if os.path.isdir(HOST_ROOT) else "/"
def _hp(p):
    """Map an absolute host path through the host-root bind mount (or pass through)."""
    return (_HR.rstrip("/") + p) if _HR != "/" else p

def _rt(path):
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return None

def _rt_dt(*paths):
    for p in paths:
        try:
            v = open(p, "rb").read().decode("utf-8", "replace").strip("\x00").strip()
            if v:
                return v
        except Exception:
            continue
    return None

# ── CPU package power via RAPL (Intel/AMD powercap) ───────────────────────────
# Cumulative energy counters under /sys/class/powercap/intel-rapl turn into watts
# via per-interval deltas. AMD (Zen/EPYC) registers under the SAME intel-rapl path.
# Best-effort: a missing tree / permission-denied energy_uj / frozen counter degrades
# that domain to "unavailable" (None) rather than raising or inventing a zero.
# MEASURED, package only (cores+uncore+iGPU+memctl) — NOT wall power.
RAPL_ROOT = os.environ.get("RAPL_ROOT", "/sys/class/powercap")
_RAPL_PREV = {}   # domain-path -> (energy_uj, monotonic_ts)

def _rapl_read_uj(path):
    """(energy_uj, max_range_uj) for a powercap domain dir, or None on any failure."""
    try:
        with open(os.path.join(path, "energy_uj")) as f:
            e = int(f.read().strip())
        with open(os.path.join(path, "max_energy_range_uj")) as f:
            m = int(f.read().strip())
        return e, m
    except (OSError, ValueError):
        return None

def _rapl_domains():
    """Discover powercap domains -> {path: name}. package-*/psys are top-level;
    core/dram/uncore are nested intel-rapl:*:* dirs. The mmio mirror is skipped."""
    out = {}
    try:
        for top in sorted(glob.glob(os.path.join(RAPL_ROOT, "intel-rapl:*"))):
            if os.path.basename(top).startswith("intel-rapl-mmio"):
                continue
            nm = _rt(os.path.join(top, "name"))
            if nm:
                out[top] = nm.strip()
            for sub in sorted(glob.glob(os.path.join(top, "intel-rapl:*:*"))):
                snm = _rt(os.path.join(sub, "name"))
                if snm:
                    out[sub] = snm.strip()
    except Exception:
        pass
    return out

def read_rapl_power():
    """Per-interval RAPL watts, or {} when unavailable. Keys: cpu_power (psys if present
    else sum of package-* domains), dram_power (sum of dram sub-domains), domains{name:w}.
    First call after start seeds state and returns {} (no prior delta)."""
    domains = _rapl_domains()
    if not domains:
        return {}
    now = time.monotonic()
    per = {}
    for path, name in domains.items():
        rd = _rapl_read_uj(path)
        if rd is None:
            continue
        e, mrange = rd
        prev = _RAPL_PREV.get(path)
        _RAPL_PREV[path] = (e, now)
        if not prev:
            continue
        e0, t0 = prev
        dt = now - t0
        if dt <= 0:
            continue
        de = e - e0
        if de < 0:                      # uint wraparound: add one modulus
            de += mrange
        per[name] = max(0.0, de / 1e6 / dt)
    if not per:
        return {}
    psys = per.get("psys")
    pkgs = [w for n, w in per.items() if n.startswith("package")]
    cpu_w = psys if psys is not None else (sum(pkgs) if pkgs else None)
    drams = [w for n, w in per.items() if n == "dram"]
    dram_power = round(sum(drams), 1) if drams else None
    return {"cpu_power": (round(cpu_w, 1) if cpu_w is not None else None),
            "dram_power": dram_power, "domains": {n: round(w, 1) for n, w in per.items()}}

_POWER_PROC_TOPN  = 8
_POWER_PROC_MIN_W = 0.5

def _attribute_power_rows(ts, gpu_power, procs_vram, cpu_power, top_cpu):
    """Build (ts, kind, name, watts) rows for power_proc: GPU watts split across
    services by VRAM share, CPU package watts split across commands by CPU-time
    share (each entity's watts sums into the measured machine total)."""
    rows = []
    vtot = sum(procs_vram.values())
    if gpu_power and vtot > 0:
        for svc, mb in procs_vram.items():
            w = gpu_power * (mb / vtot)
            if w >= _POWER_PROC_MIN_W:
                rows.append((ts, "gpu", svc, round(w, 2)))
    if cpu_power and top_cpu:
        ncpu = top_cpu.get("ncpu") or 1
        ranked = sorted(top_cpu.get("by_cpu", []), key=lambda r: -r.get("cpu_pct", 0))[:_POWER_PROC_TOPN]
        for r in ranked:
            frac = (r.get("cpu_pct", 0) / 100.0) / ncpu
            w = cpu_power * frac
            if w >= _POWER_PROC_MIN_W:
                rows.append((ts, "cpu", r["name"], round(w, 2)))
    return rows

_INV_CACHE, _INV_LOCK = {}, threading.Lock()
def _cached_inv(key, ttl, fn):
    now = time.time()
    with _INV_LOCK:
        ent = _INV_CACHE.get(key)
        if ent and now - ent[0] < ttl:
            return ent[1]
    try:
        val = fn()
    except Exception as e:
        print(f"inventory {key} error:", e, flush=True)
        val = {}
    with _INV_LOCK:
        _INV_CACHE[key] = (now, val)
    return val

def _os_family(osid, id_like="", uname=""):
    """Normalize an os-release ID into a family. Shared with _detect_os()."""
    osid, id_like, uname = (osid or "").lower(), (id_like or "").lower(), (uname or "").lower()
    if uname == "darwin":                                                         return "macos"
    if uname == "windows" or osid == "windows":                                   return "windows"
    if osid == "alpine":                                                          return "alpine"
    if osid in ("opensuse-leap", "opensuse-tumbleweed", "sles", "sled") or "suse" in id_like: return "suse"
    if osid in ("debian", "ubuntu", "raspbian", "pop", "linuxmint") or "debian" in id_like:   return "debian"
    if osid in ("fedora", "rhel", "centos", "rocky", "almalinux") or "rhel" in id_like or "fedora" in id_like: return "rhel"
    if osid in ("arch", "manjaro", "endeavouros") or "arch" in id_like:           return "arch"
    return "linux"

def _local_virt():
    """Detect the *host's* hypervisor from DMI (the hub's own /.dockerenv would
    only describe its container, not the box, so we don't use it here)."""
    blob = ((_rt("/sys/class/dmi/id/product_name") or "") + " " +
            (_rt("/sys/class/dmi/id/sys_vendor") or "")).lower()
    for key, name in (("kvm", "kvm"), ("virtualbox", "virtualbox"), ("vmware", "vmware"),
                      ("qemu", "qemu"), ("xen", "xen"), ("microsoft", "hyper-v")):
        if key in blob:
            return name
    if " hypervisor" in (_rt("/proc/cpuinfo") or ""):
        return "vm"
    return "bare-metal" if blob.strip() else None

def _local_os():
    info = {}
    try:
        u = os.uname(); info["kernel"], info["arch"] = u.release, u.machine
    except Exception:
        pass
    rel = {}
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        txt = _rt(_hp(path))
        if txt:
            for line in txt.splitlines():
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("="); rel[k.strip()] = v.strip().strip('"').strip("'")
            if rel:
                break
    osid = (rel.get("ID") or "").lower()
    info["id"] = osid or None
    info["pretty"] = rel.get("PRETTY_NAME") or rel.get("NAME") or None
    info["version_id"] = rel.get("VERSION_ID") or None
    info["family"] = _os_family(osid, rel.get("ID_LIKE"))
    info["hostname"] = socket.gethostname()
    init = None
    if os.path.isdir(_hp("/run/systemd/system")) or os.path.isdir("/run/systemd/system"):
        init = "systemd"
    else:
        comm = (_rt("/proc/1/comm") or "").strip()        # host PID 1 (pid:host)
        init = "systemd" if comm == "systemd" else ("openrc" if os.path.exists("/run/openrc") else comm or None)
    info["init"] = init
    info["virt"] = _local_virt()
    try:
        info["fqdn"] = socket.getfqdn()
    except Exception:
        pass
    for line in (_rt("/proc/stat") or "").splitlines():
        if line.startswith("btime"):
            try:
                info["boot_time"] = int(line.split()[1])
            except (ValueError, IndexError):
                pass
            break
    pretty = info.get("pretty") or osid or info.get("kernel") or "unknown"
    info["label"] = pretty + (" · " + init if init else "")
    return {k: v for k, v in info.items() if v is not None}

def _count_physical_cores(cpuinfo):
    pairs, cur = set(), None
    for line in cpuinfo.splitlines():
        k, _, v = line.partition(":"); k, v = k.strip().lower(), v.strip()
        if k == "physical id":
            cur = v
        elif k == "core id":
            pairs.add((cur, v))
    return len(pairs)

def _local_hw():
    hw = {}
    ci = _rt("/proc/cpuinfo") or ""
    mname = arm = vendor = None
    phys = set()
    for line in ci.splitlines():
        k, _, v = line.partition(":"); k, v = k.strip().lower(), v.strip()
        if not v:
            continue
        if k == "model name" and not mname:        mname = v       # full CPU string
        elif k == "vendor_id" and not vendor:      vendor = v
        elif k == "physical id":                   phys.add(v)
        elif k == "hardware" and not arm:          arm = v         # ARM SoC name
    # Never use the numeric x86 "model :" field; fall back arm-field → device tree.
    model = mname or arm or _rt_dt("/proc/device-tree/model", "/sys/firmware/devicetree/base/model")
    threads = os.cpu_count() or 1
    if model:  hw["cpu_model"] = model
    if vendor: hw["cpu_vendor"] = vendor
    hw["sockets"], hw["cores"], hw["threads"] = len(phys) or 1, _count_physical_cores(ci) or threads, threads
    khz = _rt("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    try:
        hw["cpu_mhz_max"] = round(int(khz.strip()) / 1000)
    except (AttributeError, ValueError):
        m = re.search(r"cpu MHz\s*:\s*([\d.]+)", ci)
        if m:
            hw["cpu_mhz_max"] = round(float(m.group(1)))
    try:
        mi = {}
        for line in (_rt("/proc/meminfo") or "").splitlines():
            k, _, v = line.partition(":")
            if v:
                mi[k.strip()] = int(v.split()[0])
        if mi.get("MemTotal"):  hw["ram_total"]  = mi["MemTotal"]  // 1024
        if mi.get("SwapTotal"): hw["swap_total"] = mi["SwapTotal"] // 1024
    except Exception:
        pass
    machine = (_rt("/sys/class/dmi/id/product_name") or "").strip()
    if machine.lower() in ("", "to be filled by o.e.m.", "system product name", "default string"):
        machine = _rt_dt("/proc/device-tree/model", "/sys/firmware/devicetree/base/model") or ""
    if machine:
        hw["machine"] = machine
    # GPU name isn't kept on LATEST (only util/mem), so ask nvidia-smi once (cached).
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                            "--format=csv,noheader,nounits"], capture_output=True, timeout=3)
        if r.returncode == 0:
            line = r.stdout.decode("utf-8", "replace").splitlines()[0]
            nm, _, mt = line.partition(",")
            if nm.strip():
                hw["gpu_name"] = nm.strip()
            try:
                hw["gpu_mem_total"] = int(mt.strip())
            except ValueError:
                pass
    except Exception:
        pass
    return hw

def _iface_type(name, d):
    if name == "lo":                                          return "loopback"
    if os.path.isdir(d + "/wireless"):                        return "wifi"
    if name.startswith("wg"):                                 return "wireguard"
    if name.startswith(("tun", "tap")):                       return "tunnel"
    if name.startswith(("docker", "br-", "veth", "virbr")):   return "virtual"
    if name.startswith("bond"):                               return "bond"
    if os.path.exists(d + "/device"):                         return "ethernet"
    return "other"

def _ifaddr_v4(name):
    if not fcntl:
        return None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        res = fcntl.ioctl(s.fileno(), 0x8915,                  # SIOCGIFADDR
                          struct.pack("256s", name[:15].encode()))
        s.close()
        return socket.inet_ntoa(res[20:24])
    except Exception:
        return None

def _sys_ifaces():
    out = []
    base = "/sys/class/net"
    try:
        names = sorted(os.listdir(base))
    except Exception:
        return out
    for n in names:
        # Skip container plumbing (veth pairs, Docker per-network br-<hex> bridges)
        # — pure noise that dominates the list on any Docker host.
        if n.startswith("veth") or re.match(r"br-[0-9a-f]{8,}$", n):
            continue
        d = base + "/" + n
        rd = lambda f: (_rt(d + "/" + f) or "").strip() or None
        iface = {"name": n, "ipv4": [], "ipv6": [], "type": _iface_type(n, d)}
        mac = rd("address")
        if mac and mac != "00:00:00:00:00:00":
            iface["mac"] = mac
        st = rd("operstate")
        if st:
            iface["state"] = st
        try:
            iface["mtu"] = int(rd("mtu"))
        except (TypeError, ValueError):
            pass
        try:
            sp = int(rd("speed"))
            if sp > 0:
                iface["speed_mbps"] = sp
        except (TypeError, ValueError):
            pass
        for stat, key in (("statistics/rx_bytes", "rx_bytes"), ("statistics/tx_bytes", "tx_bytes")):
            try:
                iface[key] = int(rd(stat))
            except (TypeError, ValueError):
                pass
        ip = _ifaddr_v4(n)
        if ip:
            iface["ipv4"].append(ip)
        out.append(iface)
    return out

def _route_default():
    for line in (_rt("/proc/net/route") or "").splitlines()[1:]:
        p = line.split()
        if len(p) >= 3 and p[1] == "00000000":
            try:
                return p[0], ".".join(str(int(p[2][i:i + 2], 16)) for i in (6, 4, 2, 0))
            except ValueError:
                return p[0], None
    return None, None

def _primary_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]; s.close()
        return ip
    except Exception:
        return None

def _resolv():
    ns, search = [], []
    for line in (_rt(_hp("/etc/resolv.conf")) or "").splitlines():
        line = line.strip()
        if line.startswith("nameserver"):
            parts = line.split()
            if len(parts) > 1:
                ns.append(parts[1])
        elif line.startswith(("search", "domain")):
            search += line.split()[1:]
    return ns, search

def _hex_to_ip(h, fam):
    try:
        if fam == 4:
            return socket.inet_ntoa(bytes(int(h[i:i + 2], 16) for i in (6, 4, 2, 0)))
        raw = bytes(int(h[i:i + 2], 16) for i in range(0, 32, 2))
        return socket.inet_ntop(socket.AF_INET6, b"".join(raw[i:i + 4][::-1] for i in (0, 4, 8, 12)))
    except Exception:
        return h

def _proc_listen():
    """Listening sockets from /proc/net/{tcp,udp}[6] (no ss in the slim image).
    Flags `exposed` for all-zero bind addresses (0.0.0.0 / ::)."""
    out, seen = [], set()
    for path, proto, fam, want in (("/proc/net/tcp", "tcp", 4, "0A"), ("/proc/net/tcp6", "tcp", 6, "0A"),
                                   ("/proc/net/udp", "udp", 4, "07"), ("/proc/net/udp6", "udp", 6, "07")):
        txt = _rt(path)
        if not txt:
            continue
        for line in txt.splitlines()[1:]:
            cols = line.split()
            if len(cols) < 4 or cols[3] != want or ":" not in cols[1]:
                continue
            hexip, hexport = cols[1].rsplit(":", 1)
            try:
                port = int(hexport, 16)
            except ValueError:
                continue
            allzero = set(hexip) <= {"0"}
            addr = (("0.0.0.0" if fam == 4 else "::") if allzero else _hex_to_ip(hexip, fam))
            key = (proto, addr, port)
            if key in seen:
                continue
            seen.add(key)
            out.append({"proto": proto, "addr": addr, "port": port, "exposed": allzero, "proc": None})
    out.sort(key=lambda s: (not s["exposed"], s["port"]))
    return out

def _established_count():
    n = 0
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        txt = _rt(path)
        if not txt:
            continue
        for line in txt.splitlines()[1:]:
            cols = line.split()
            if len(cols) > 3 and cols[3] == "01":
                n += 1
    return n

def _local_net():
    net = {}
    ifaces = _sys_ifaces()
    route_if, gw = _route_default()
    if gw:
        net["gateway"] = gw
    pip = _primary_ip()
    if pip:
        net["primary_ip"] = pip
    primary = route_if or next((i["name"] for i in ifaces if pip and pip in i["ipv4"]), None)
    if primary:
        net["primary_iface"] = primary
    ns, search = _resolv()
    if ns:
        net["dns"] = ns
    if search:
        net["search"] = search
    try:
        net["fqdn"] = socket.getfqdn()
    except Exception:
        pass
    net["ifaces"] = ifaces
    net["listen"] = _proc_listen()
    net["established_count"] = _established_count()
    return net

def _local_firewall():
    conf = _rt(_hp("/etc/ufw/ufw.conf"))
    if conf is not None:
        return {"backend": "ufw", "active": "ENABLED=yes" in conf.replace(" ", "")}
    if os.path.isdir(_hp("/etc/firewalld")):
        return {"backend": "firewalld", "active": None}
    if os.path.exists(_hp("/etc/nftables.conf")):
        return {"backend": "nftables", "active": None}
    return {"backend": None, "active": None}

def _ssh_from_file():
    cfg = {}
    for line in (_rt(_hp("/etc/ssh/sshd_config")) or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            parts = line.split(None, 1)
            if len(parts) == 2:
                cfg.setdefault(parts[0].lower(), parts[1].strip())
    if not cfg:
        return None
    out = {}
    if "permitrootlogin" in cfg:
        out["permit_root"] = cfg["permitrootlogin"].split()[0]
    if "passwordauthentication" in cfg:
        out["password_auth"] = cfg["passwordauthentication"].split()[0]
    try:
        out["port"] = int(cfg["port"].split()[0])
    except (KeyError, ValueError):
        pass
    return out or None

def _selinux_local():
    if os.path.exists("/sys/fs/selinux/enforce"):
        v = (_rt("/sys/fs/selinux/enforce") or "").strip()
        return "enforcing" if v == "1" else "permissive" if v == "0" else None
    return "disabled"

def _apparmor_local():
    v = (_rt("/sys/module/apparmor/parameters/enabled") or "").strip()
    if v:
        return "enabled" if v in ("Y", "y") else "disabled"
    return "enabled" if os.path.isdir("/sys/kernel/security/apparmor") else "disabled"

def _fail2ban_local():
    installed = (os.path.isdir(_hp("/etc/fail2ban"))
                 or os.path.exists(_hp("/lib/systemd/system/fail2ban.service"))
                 or os.path.exists(_hp("/etc/systemd/system/fail2ban.service")))
    return {"installed": bool(installed), "active": None}

def _reboot_local():
    return (os.path.exists(_hp("/var/run/reboot-required"))
            or os.path.exists(_hp("/run/reboot-required")))

def _auto_updates_local():
    txt = _rt(_hp("/etc/apt/apt.conf.d/20auto-upgrades"))
    if txt:
        m = re.search(r'Unattended-Upgrade"\s+"(\d+)"', txt)
        if m:
            return m.group(1) != "0"
    return None

def _updates_local():
    """Pending updates for the hub's own host. We only trust the pre-rendered
    update-notifier file (read through HOST_ROOT) — running a package manager
    here would query the *container*, not the host, so on non-apt hosts (or
    without the file) we return None and the UI shows 'needs elevated read'. The
    hub's network-side new-release check still works from os.version_id."""
    txt = _rt(_hp("/var/lib/update-notifier/updates-available"))
    if not txt:
        return None
    out = {"count": None, "security": None, "kernel": None,
           "source": "apt", "checked": "cached"}
    # Same line-by-line parse as the probe: the security line gives the security
    # count, the first other count-bearing line gives the total (wording varies).
    for line in txt.splitlines():
        m = re.search(r"(\d+)", line)
        if not m:
            continue
        n, low = int(m.group(1)), line.lower()
        if "securit" in low:
            out["security"] = n
        elif out["count"] is None and ("update" in low or "package" in low or "can be" in low):
            out["count"] = n
    return out

def _local_sec():
    return {
        "firewall":        _local_firewall(),
        "ssh":             _ssh_from_file(),
        "selinux":         _selinux_local(),
        "apparmor":        _apparmor_local(),
        "fail2ban":        _fail2ban_local(),
        "reboot_required": _reboot_local(),
        "auto_updates":    _auto_updates_local(),
        "updates":         _updates_local(),
    }

# ── Health monitors: Docker containers + systemd services ──────────────────────
# These describe *current state* (is it up? healthy? failed?) rather than a time
# series, so they live behind /api/health and are refreshed by health_scan().
_DOCKER_HEALTH = re.compile(r"\((healthy|unhealthy|health: starting)\)")
# "Up 14 days, 2 hours" / "Up About a minute" / "Up 12 seconds (healthy)" — Docker
# already builds a human Status string, so we parse it instead of issuing N more
# inspect calls just for StartedAt.
_DOCKER_UP_DUR = re.compile(r"^Up\s+(.+?)(?:\s*\(.*\))?$", re.I)
_DUR_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800, "month": 2592000, "year": 31536000}
# Heavy enrichment (per-container `stats`) is too costly to hit on every 10 s
# health_scan, so we keep a separate 30 s cache for it.
_DOCKER_ENRICH_TTL = 30
_docker_enrich = {"data": {}, "at": 0}
# Real disk footprint (writable layer + volumes + bind mounts) needs a `du` over
# host paths via HOST_ROOT — far heavier than the rest, and disk barely moves
# minute-to-minute, so it gets a much longer TTL and runs in its own thread so it
# never stalls the health-scan loop. A cold `du` of a huge media library on a
# spinning disk can take minutes; once the kernel has cached the metadata the
# same scan is seconds, so we allow a generous per-mount timeout and keep the
# last known value when a scan overruns it.
_DOCKER_DISK_TTL   = 1800
_DOCKER_POLICY_TTL = 1800  # restart policies change rarely; no need to inspect every 30 s
_docker_disk = {"data": {}, "at": 0, "busy": False}

def _docker_status(state, status):
    """Map a Docker container's state/status string to a (level, label) pair."""
    st, s = (state or "").lower(), (status or "").lower()
    m = _DOCKER_HEALTH.search(s)
    health = m.group(1) if m else None
    if st == "running":
        if health == "unhealthy":        return "crit", "unhealthy"
        if health == "health: starting": return "warn", "starting"
        return "ok", "healthy" if health == "healthy" else "running"
    if st == "restarting":               return "warn", "restarting"
    if st == "paused":                   return "warn", "paused"
    if st == "exited":
        code = re.search(r"exited \((\d+)\)", s)
        if code and code.group(1) != "0": return "crit", f"exited ({code.group(1)})"
        return "info", "stopped"
    if st == "dead":                     return "crit", "dead"
    if st == "created":                  return "info", "created"
    return "info", st or "unknown"

def _parse_docker_uptime(status_text):
    """Turn Docker's 'Up 14 days, 2 hours' status text into a uptime in seconds.

    Returns 0 for stopped/restarting/dead/etc. — anything that isn't 'Up …'."""
    if not status_text:
        return 0
    m = _DOCKER_UP_DUR.match(status_text.strip())
    if not m:
        return 0
    total = 0
    for n, unit in re.findall(r"(\d+|a|an|about)\s*(second|minute|hour|day|week|month|year)s?", m.group(1).lower()):
        try:
            total += (1 if n in ("a", "an", "about") else int(n)) * _DUR_UNITS[unit]
        except (KeyError, ValueError):
            continue
    return total

def _container_published_ports(ct):
    """Unique published host-side ports for a container (de-duped across v4/v6)."""
    seen = []
    for p in ct.get("Ports") or []:
        port = p.get("PublicPort")
        if isinstance(port, int) and port not in seen:
            seen.append(port)
    return sorted(seen)

def _container_stats(cid):
    """One-shot resident-RAM snapshot for a running container: the working set,
    excluding page cache, exactly like `docker stats`. This is system RAM only —
    GPU VRAM is attributed separately (nvidia-smi compute-apps). Bytes or None."""
    try:
        d = json.loads(_docker(f"/containers/{cid}/stats?stream=false&one-shot=true"))
    except Exception:
        return None
    ms = d.get("memory_stats") or {}
    st = ms.get("stats") or {}
    # Real resident RAM = anonymous memory (+ shared memory) the container holds.
    # NOT `usage - inactive_file` (the docker-stats formula): that still includes
    # ACTIVE file cache, which for AI containers that mmap multi-GB model files is
    # enormous and fully reclaimable. It inflated every row and made the
    # per-container sum exceed the host's *used* RAM — impossible for real RAM,
    # since the host (MemTotal - MemAvailable) counts that cache as available.
    # anon (+ shmem) is what the container actually occupies and sums sensibly.
    if "anon" in st:                                   # cgroup v2
        return (st.get("anon") or 0) + (st.get("shmem") or 0)
    if "rss" in st:                                    # cgroup v1
        return (st.get("rss") or 0) + (st.get("rss_huge") or 0)
    usage = ms.get("usage")                            # last resort: docker-stats math
    if usage is None:
        return None
    cache = st.get("inactive_file", st.get("cache", 0)) or 0
    return max(0, usage - cache)

def _refresh_docker_enrich(running_ids):
    """Per-running-container memory snapshot (parallel). Cached for
    _DOCKER_ENRICH_TTL seconds. Disk is measured separately (see
    _refresh_docker_disk) because a `du` over volumes is far heavier."""
    out = {}
    if running_ids:
        with ThreadPoolExecutor(max_workers=min(8, len(running_ids))) as ex:
            for cid, mem in zip(running_ids, ex.map(_container_stats, running_ids)):
                out.setdefault(cid, {})["mem_bytes"] = mem
    return out

# Restart policy isn't in the /containers/json list payload — only a full
# inspect has it. That's an extra round-trip per container, so it's only ever
# fetched when ENABLE_CONTROLS is on (nothing in the read-only path pays for it).
_docker_policy = {"data": {}, "at": 0}

def _container_restart_policy(cid):
    try:
        d = json.loads(_docker(f"/containers/{cid}/json"))
    except Exception:
        return None
    rp = (d.get("HostConfig") or {}).get("RestartPolicy") or {}
    return {"name": rp.get("Name") or "no", "max_retry": rp.get("MaximumRetryCount") or 0}

def _refresh_docker_policies(ids):
    """Restart policy for every known container (stopped ones too — you may
    want to fix a policy before ever starting it), parallel like _refresh_docker_enrich."""
    out = {}
    if ids:
        with ThreadPoolExecutor(max_workers=min(8, len(ids))) as ex:
            for cid, rp in zip(ids, ex.map(_container_restart_policy, ids)):
                if rp is not None:
                    out[cid] = rp
    return out

def _dir_size(host_path, timeout=120):
    """Apparent size (bytes) of a host path, read through the read-only HOST_ROOT
    bind mount — i.e. `du -sb` as the host would see it. Returns None when the
    path is unreachable or `du` overruns `timeout` (a cold scan of a huge media
    library on spinning rust can run for minutes; the caller keeps the previous
    value on a None so the figure degrades to "stale" rather than "wrong")."""
    base = HOST_ROOT.rstrip("/") if os.path.isdir(HOST_ROOT) else ""
    path = (base + host_path) if base else host_path
    try:
        r = subprocess.run(["du", "-sb", "--", path],
                           capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    try:
        return int((r.stdout or "").split("\t", 1)[0].strip())
    except ValueError:
        return None   # du errored (missing path / no access): stdout has no count

def _container_disk(ct, prev=None, shared=frozenset()):
    """A container's real on-disk footprint: writable layer (SizeRw) + every
    read-write volume and bind mount it owns. Read-only mounts (configs, the
    docker socket, our own /rootfs) aren't this container's data, so they're
    skipped, and so are sources in `shared` — data reachable read-write from more
    than one container (e.g. a common /srv/share, or a directory another container
    covers by mounting its parent) belongs to none of them, and counting it under
    each both double-counts and makes unrelated rows show the same huge number.
    Falls back toward SizeRw alone when host paths can't be measured —
    e.g. HOST_ROOT not mounted — which is the old behaviour.

    `prev` is this container's last computed total: if a mount's `du` overruns
    its timeout we keep the larger of (partial new total, prev) so a transient
    slow scan never makes the number shrink or blink to zero."""
    total = ct.get("SizeRw") or 0
    incomplete = False
    for m in (ct.get("Mounts") or []):
        if not m.get("RW") or m.get("Type") not in ("volume", "bind"):
            continue
        src = _norm_src(m.get("Source"))
        if not src or src in shared:
            continue
        sz = _dir_size(src)
        if sz is None:
            incomplete = True
        else:
            total += sz
    if incomplete and prev is not None:
        return max(total, prev)
    return total

def _norm_src(src):
    """A mount source as a normalised host path, or None when it isn't one.

    Podman reports pseudo-sources (`devpts`, `tmpfs`) for some mounts and
    `_container_disk` has always skipped anything not absolute; the sharing check
    has to apply the same rule and the same normalisation, or the two disagree
    about what a source is and the `src in shared` skip silently misses."""
    if not src or not src.startswith("/"):
        return None
    p = os.path.normpath(src)
    return p[1:] if p.startswith("//") else p   # POSIX keeps a leading "//" verbatim

def _covers(parent, child):
    """True when `parent` is a *strict* ancestor directory of `child`.

    Both arguments must already be normalised (see `_norm_src`). Compared per path
    component, so `/srv` doesn't "contain" `/srvfoo`, and `/` is an ancestor of
    every other absolute path. Equality is deliberately False: the caller treats
    "someone mounts the same source" and "someone mounts a directory above mine"
    as two different cases.

    Purely lexical, so it can't see through symlinks: if `/srv/models` is a link
    to `/mnt/disk/models` and another container mounts `/mnt/disk`, the sharing
    goes unnoticed. Resolving that would mean `realpath` against the host rootfs,
    which isn't always mounted — the pre-existing behaviour, left alone."""
    if parent == child:
        return False
    return True if parent == "/" else child.startswith(parent + "/")

_shared_note = {"seen": frozenset()}

def _shared_mount_sources(sized):
    """Sources (volume/bind) that shouldn't be attributed to any single container.

    Two ways a source qualifies:

    - **Another container mounts the same source.** The original rule, unchanged.
    - **Another container mounts a directory above it.** New, and the reason this
      function was touched: a container mounting `/srv/models` and another mounting
      `/` (what toolbox and distrobox do, at `/run/host`) write the same bytes, but
      no two sources match as strings. The whole tree used to be billed to the one
      container that named it — on the host that prompted this, a container stopped
      for two weeks with a 40.9 kB writable layer was charged 1.25 TB.

    The check is deliberately **one-directional**: mounting a parent does not make
    you lose it because someone else mounted a subdirectory of yours. A container
    with 500 GB under `/srv/media` keeps being billed for it when another mounts
    only `/srv/media/photos` — the sub-tree stops being double-counted, and the
    exclusive part doesn't vanish from the report. Nesting between two mounts of
    the *same* container is not sharing either."""
    owners = {}                                    # source -> set of container indexes
    for i, ct in enumerate(sized):
        for m in (ct.get("Mounts") or []):
            if m.get("RW") and m.get("Type") in ("volume", "bind"):
                src = _norm_src(m.get("Source"))
                if src:
                    owners.setdefault(src, set()).add(i)   # mounted twice still counts once
    shared, covered = set(), set()
    for src, mine in owners.items():
        if len(mine) > 1:
            shared.add(src)
            continue
        for other, theirs in owners.items():
            if _covers(other, src) and theirs - mine:
                shared.add(src)
                covered.add(src)
                break
    # A parent mount can silently empty the disk column for a whole host (one
    # container with `/` read-write covers everything), so say it once when the
    # set changes rather than let the numbers disappear without explanation.
    if covered != _shared_note["seen"]:
        _shared_note["seen"] = frozenset(covered)
        if covered:
            names = sorted(covered)
            head = ", ".join(names[:3]) + (f" (+{len(names) - 3} more)" if len(names) > 3 else "")
            print(f"NOTE: not billing {head} to any container — another container mounts a "
                  f"parent directory read-write, so that data isn't exclusively theirs",
                  flush=True)
    return frozenset(shared)

def _refresh_docker_disk():
    """Measure every container's footprint (running or stopped — stopped
    containers' volumes still occupy disk) and publish results incrementally, so
    fast volume scans (e.g. Ollama) appear right away instead of waiting on one
    container's slow cold `du` (e.g. a 300 GB photo library). One `?size=1` call
    yields SizeRw + the Mounts list per container; the `du`s run in parallel and
    each writes into the live dict as it lands. Seeded from the previous results
    so a periodic rescan never blanks the table."""
    try:
        sized = json.loads(_docker("/containers/json?all=1&size=1"))
    except Exception:
        return
    prev = dict(_docker_disk.get("data") or {})
    fresh = dict(prev)                 # readers keep seeing old values until each is refreshed
    _docker_disk["data"] = fresh
    if not sized:
        fresh.clear()
        return
    shared = _shared_mount_sources(sized)
    def measure(ct):
        cid = ct["Id"][:12]
        fresh[cid] = _container_disk(ct, prev.get(cid), shared)
    with ThreadPoolExecutor(max_workers=min(8, len(sized))) as ex:
        list(ex.map(measure, sized))
    live = {ct["Id"][:12] for ct in sized}
    for cid in [k for k in fresh if k not in live]:
        del fresh[cid]                 # drop containers that no longer exist

def _maybe_refresh_docker_disk():
    """Kick a disk refresh in the background when the cache is stale. Never
    blocks the caller (health_scan): a slow `du` would otherwise hold up Docker +
    systemd + update collection for everyone."""
    if _docker_disk["busy"] or time.time() - _docker_disk["at"] <= _DOCKER_DISK_TTL:
        return
    _docker_disk["busy"] = True
    def run():
        try:
            _refresh_docker_disk()        # publishes into _docker_disk["data"] as it goes
            _docker_disk["at"] = time.time()
        finally:
            _docker_disk["busy"] = False
    threading.Thread(target=run, daemon=True).start()

def collect_docker():
    try:
        raw = json.loads(_docker("/containers/json?all=1"))
    except Exception as e:
        return {"available": False, "reason": f"Docker API unreachable: {e}",
                "containers": [], "summary": {"total": 0, "running": 0, "problems": 0}}
    items = []
    for ct in raw:
        level, label = _docker_status(ct.get("State"), ct.get("Status"))
        state = (ct.get("State") or "").lower()
        status_text = ct.get("Status", "")
        items.append({"id": ct["Id"][:12],
                      "name": (ct.get("Names") or ["/?"])[0].lstrip("/"),
                      "image": ct.get("Image", ""), "state": state,
                      "status_text": status_text, "status": level, "label": label,
                      "ports": _container_published_ports(ct),
                      "uptime_s": _parse_docker_uptime(status_text) if state == "running" else 0})
    # Merge cached enrichment (memory + disk). Refresh on a slower cadence than
    # the basic state pass — see _DOCKER_ENRICH_TTL.
    if time.time() - _docker_enrich["at"] > _DOCKER_ENRICH_TTL:
        running_ids = [c["id"] for c in items if c["state"] == "running"]
        _docker_enrich["data"] = _refresh_docker_enrich(running_ids)
        _docker_enrich["at"] = time.time()
    _maybe_refresh_docker_disk()   # background; first pass leaves disk_bytes None until it lands
    # Restart policy only matters to the (opt-in) controls UI — skip the extra
    # inspect round-trip entirely when controls are off.
    if ENABLE_CONTROLS and time.time() - _docker_policy["at"] > _DOCKER_POLICY_TTL:
        _docker_policy["data"] = _refresh_docker_policies([c["id"] for c in items])
        _docker_policy["at"] = time.time()
    # Per-container GPU VRAM, attributed by the GPU sampler (nvidia-smi
    # compute-apps → /proc/<pid>/cgroup → container name). procs is in MB and
    # keyed by service name (== container name for container-owned PIDs); host /
    # unattributed PIDs use "host:"/"pid:" keys that never match a container.
    vram_mb = {p.get("service"): p.get("mem") for p in (LATEST.get("procs") or [])}
    # Prefer /proc/self/cgroup (reliable even when docker-compose overrides hostname:).
    # cgroups v1 encodes the full 64-char container ID in the cgroup path; v2 doesn't,
    # so fall back to the HOSTNAME env var (still the container short-ID by default).
    self_id = ""
    try:
        with open("/proc/self/cgroup") as _f:
            for _ln in _f:
                _part = _ln.strip().split("/")[-1]
                if len(_part) == 64 and all(c in "0123456789abcdef" for c in _part):
                    self_id = _part[:12]
                    break
    except OSError:
        pass
    if not self_id:
        self_id = (os.environ.get("HOSTNAME") or "")[:12]
    for c in items:
        e = _docker_enrich["data"].get(c["id"]) or {}
        c["mem_bytes"]  = e.get("mem_bytes")
        vmb = vram_mb.get(c["name"])
        c["vram_bytes"] = round(vmb * 1048576) if vmb else None
        c["disk_bytes"] = _docker_disk["data"].get(c["id"])
        c["restart_policy"] = _docker_policy["data"].get(c["id"]) if ENABLE_CONTROLS else None
        c["is_self"] = bool(self_id) and c["id"] == self_id
    rank = {"crit": 0, "warn": 1, "ok": 2, "info": 3}
    items.sort(key=lambda c: (rank.get(c["status"], 9), c["name"].lower()))
    return {"available": True, "containers": items,
            "summary": {"total": len(items),
                        "running": sum(1 for c in items if c["state"] == "running"),
                        "problems": sum(1 for c in items if c["status"] in ("crit", "warn"))}}

def _svc_status(active):
    return {"failed": "crit", "active": "ok",
            "activating": "warn", "deactivating": "warn"}.get(active, "info")

# MemoryCurrent on a unit with no accounting comes back as the sentinel 2^64-1.
_DBUS_U64_MAX = 0xFFFFFFFFFFFFFFFF
_SOCK_INODE_RE = re.compile(r"^socket:\[(\d+)\]$")

def _listen_inode_to_port():
    """Map socket inodes of LISTEN TCP/TCP6 sockets to their local port.

    /proc/net/tcp[6] columns: sl local rem st ... inode. `local` is
    "<hex-ip>:<hex-port>"; `st` is `0A` for LISTEN. We use /proc directly
    instead of `ss -p` because the container's default cap set excludes
    CAP_NET_ADMIN — `ss` can't attribute PIDs to sockets without it. With
    pid:host + network_mode:host (per docker-compose) the inodes here are
    the real host's."""
    inodes = {}
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as f:
                next(f, None)  # header
                for ln in f:
                    cols = ln.split()
                    if len(cols) < 10 or cols[3] != "0A":
                        continue
                    local = cols[1]
                    if ":" not in local:
                        continue
                    try:
                        inodes[int(cols[9])] = int(local.rsplit(":", 1)[1], 16)
                    except ValueError:
                        continue
        except Exception:
            continue
    return inodes

def _cgroup_pids(control_group):
    """Return all PIDs in a systemd service's cgroup.

    Instead of reading /sys/fs/cgroup/.../cgroup.procs (which is unavailable
    inside Docker's private cgroup namespace), we scan /proc/<pid>/cgroup for
    every running process and match the service's ControlGroup string. Works
    on bare-metal and in Docker, and avoids cgroup v1/v2 path differences.

    Falls back to an empty list if the cgroup path is empty or no PIDs match.
    """
    if not control_group:
        return []
    # Normalize: ControlGroup may or may not have a leading slash; strip it
    # so we can match against the proc entries consistently.
    cg = control_group.lstrip("/")
    pids = []
    try:
        for pid_dir in os.listdir("/proc"):
            if not pid_dir.isdigit():
                continue
            try:
                with open(f"/proc/{pid_dir}/cgroup") as f:
                    for ln in f:
                        if cg in ln.strip():
                            pids.append(int(pid_dir))
                            break
            except (FileNotFoundError, PermissionError, OSError):
                continue
    except Exception:
        return []
    return pids

def _ports_for_pid(pid, inode_to_port):
    """LISTEN ports owned by `pid` by walking /proc/<pid>/fd/* for socket:[N]
    symlinks and joining inodes to ports. Empty list on any access failure —
    services that fork (Type=forking) may own LISTEN sockets in a child PID
    rather than MainPID, so an empty result here is silent degradation."""
    if not pid or not inode_to_port:
        return []
    out = set()
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            m = _SOCK_INODE_RE.match(target)
            if not m:
                continue
            port = inode_to_port.get(int(m.group(1)))
            if port is not None:
                out.add(port)
    except (FileNotFoundError, PermissionError):
        return []
    return sorted(out)

def _proc_rss_bytes(pid):
    """VmRSS in bytes for a single PID. Memory fallback used when systemd's
    MemoryCurrent is unset (DefaultMemoryAccounting=no — the default on most
    distros). Returns None on any failure."""
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/status") as f:
            for ln in f:
                if ln.startswith("VmRSS:"):
                    return int(ln.split()[1]) * 1024
    except (FileNotFoundError, PermissionError, ValueError):
        return None
    return None

def _dbus_get_all(conn, addr_cls, new_method_call_fn, path, interface):
    """Wrap Properties.GetAll for a single unit object path. Returns a flat
    {name: value} dict (variants unwrapped) or {} on any failure."""
    props_addr = addr_cls(path, bus_name="org.freedesktop.systemd1",
                          interface="org.freedesktop.DBus.Properties")
    try:
        body = conn.send_and_get_reply(new_method_call_fn(props_addr, "GetAll", "s", (interface,))).body
    except Exception:
        return {}
    # jeepney decodes `v` (variant) entries as (signature, value) tuples; be
    # defensive in case the library version returns the value directly.
    out = {}
    for k, v in (body[0] or {}).items():
        out[k] = v[1] if isinstance(v, tuple) and len(v) == 2 else v
    return out

def collect_systemd():
    """Read systemd *system* units over the host D-Bus socket (pure-Python jeepney).

    Highlights admin/user-authored units (those under /etc/systemd/system) plus
    anything that has failed. Degrades gracefully when the socket isn't mounted,
    so the rest of the dashboard keeps working out of the box."""
    try:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection
    except Exception:
        return {"available": False, "reason": "jeepney not installed in the image.",
                "services": [], "summary": {}}
    try:
        conn = open_dbus_connection(bus="SYSTEM")
    except Exception:
        return {"available": False,
                "reason": "Host D-Bus socket not reachable — mount /run/dbus/system_bus_socket "
                          "(see docker-compose.yml) to enable systemd monitoring.",
                "services": [], "summary": {}}
    try:
        mgr = DBusAddress("/org/freedesktop/systemd1", bus_name="org.freedesktop.systemd1",
                          interface="org.freedesktop.systemd1.Manager")
        units = conn.send_and_get_reply(new_method_call(mgr, "ListUnits")).body[0]
        try:
            files = conn.send_and_get_reply(new_method_call(mgr, "ListUnitFiles")).body[0]
        except Exception:
            files = []
        admin = {os.path.basename(p) for p, _state in files
                 if p.startswith(SYSTEMD_ADMIN_DIR) and p.endswith(".service")}
        services, running, failed = [], 0, 0
        for name, desc, _load, active, sub, _follow, obj_path, *_rest in units:
            if not name.endswith(".service"):
                continue
            running += active == "active"
            failed  += active == "failed"
            services.append({"name": name, "desc": desc, "active": active, "sub": sub,
                             "admin": name in admin,
                             "watched": name in WATCH_SERVICES or name[:-8] in WATCH_SERVICES,
                             "status": _svc_status(active),
                             "_obj_path": obj_path})
        # Default view = the units you actually care about: ones you deployed, ones
        # you asked to watch, and anything currently failing. Enrichment (mem,
        # uptime, ports, exit code) is only done for these — touching every unit
        # would mean ~hundreds of D-Bus round-trips on a busy host.
        shown = [s for s in services if s["admin"] or s["watched"] or s["status"] == "crit"]
        inode_to_port = _listen_inode_to_port()
        now_us = time.time() * 1_000_000
        for s in shown:
            svc_props = _dbus_get_all(conn, DBusAddress, new_method_call,
                                      s["_obj_path"], "org.freedesktop.systemd1.Service")
            unit_props = _dbus_get_all(conn, DBusAddress, new_method_call,
                                       s["_obj_path"], "org.freedesktop.systemd1.Unit")
            pid = int(svc_props.get("MainPID") or 0)
            mem = svc_props.get("MemoryCurrent")
            if isinstance(mem, int) and 0 < mem < _DBUS_U64_MAX:
                s["mem_bytes"] = int(mem)
            else:
                # MemoryAccounting off (the default on most distros) — read the
                # main process's RSS as a coarse fallback. Underestimates for
                # multi-process services but better than showing nothing.
                s["mem_bytes"] = _proc_rss_bytes(pid)
            enter_us = unit_props.get("ActiveEnterTimestamp") or 0
            s["uptime_s"] = max(0, int((now_us - enter_us) / 1_000_000)) if enter_us else 0
            if s["status"] == "crit":
                ex = svc_props.get("ExecMainStatus")
                if ex is not None:
                    s["exit_status"] = int(ex)
            s["ports"] = _ports_for_pid(pid, inode_to_port)
            if not s["ports"]:
                # MainPID owns no listening sockets — walk the service's full
                # cgroup to cover child/forking processes (e.g. Pi-hole FTL,
                # dnsmasq).  Union ports across all children since a forking
                # service can listen on several ports across children.
                # Falls back gracefully if cgroup is unreadable.
                cgroup = svc_props.get("ControlGroup")
                for cpid in _cgroup_pids(cgroup):
                    if cpid == pid:
                        continue
                    child_ports = _ports_for_pid(cpid, inode_to_port)
                    if child_ports:
                        s["ports"].extend(child_ports)
                        s["ports"] = sorted(set(s["ports"]))
            s.pop("_obj_path", None)
    except Exception as e:
        return {"available": False, "reason": f"systemd query failed: {e}", "services": [], "summary": {}}
    finally:
        conn.close()

    rank = {"crit": 0, "warn": 1, "ok": 2, "info": 3}
    shown.sort(key=lambda s: (rank.get(s["status"], 9), s["name"].lower()))
    return {"available": True, "services": shown,
            "summary": {"loaded": len(services), "running": running,
                        "failed": failed, "admin": len(admin)}}

_SYSTEMD_UNIT_METHODS = {"start": "StartUnit", "stop": "StopUnit", "restart": "RestartUnit"}

def systemd_unit_action(unit, action):
    """Start/stop/restart a *local* systemd unit over the same D-Bus socket
    collect_systemd() reads from — a fresh short-lived connection per call.
    Returns (ok, error). The container runs as root (no USER in the Dockerfile),
    so this either works outright (root is implicitly privileged on the system
    bus, no polkit prompt) or fails with the D-Bus/systemd's own error text —
    there's no "are we allowed" pre-check worth doing, the attempt IS the check."""
    try:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection
    except Exception:
        return False, "jeepney not installed in the image."
    try:
        conn = open_dbus_connection(bus="SYSTEM")
    except Exception as e:
        return False, f"Host D-Bus socket not reachable: {e}"
    try:
        mgr = DBusAddress("/org/freedesktop/systemd1", bus_name="org.freedesktop.systemd1",
                          interface="org.freedesktop.systemd1.Manager")
        conn.send_and_get_reply(new_method_call(mgr, _SYSTEMD_UNIT_METHODS[action], "ss", (unit, "replace")))
        return True, None
    except Exception as e:
        return False, str(e)
    finally:
        conn.close()

def build_overview(now, docker, systemd):
    """One status card per subsystem for the Overview tab. New monitors append here."""
    cards = []
    g = now.get("gpu") or {}
    # Only show the GPU card when a GPU is actually present — otherwise a GPU-less
    # host would show a misleading frozen "0% VRAM · 0°C" tile.
    if g.get("available"):
        tot = g.get("mem_total") or 1
        used, used_pct = g.get("mem_used", 0), round((g.get("mem_used", 0) / tot) * 100)
        cards.append({"key": "gpu", "label": "GPU",
                      "status": "crit" if (tot - used) < PRESSURE_MB else "warn" if used_pct >= 85 else "ok",
                      "metric": f"{used_pct}% VRAM",
                      "detail": f"{round(g.get('util', 0))}% util · {round(g.get('temp', 0))}°C"})
    h = now.get("host") or {}
    ram_pct = round(h["ram_used"] / h["ram_total"] * 100) if h.get("ram_total") else 0
    worst_disk = (h.get("disks") or [{}])[0].get("pct", 0)
    cards.append({"key": "host", "label": "Host",
                  "status": "crit" if worst_disk >= 90 or ram_pct >= 90 else
                            "warn" if worst_disk >= 80 or ram_pct >= 80 else "ok",
                  "metric": f"{round(h.get('cpu', 0))}% CPU",
                  "detail": f"{ram_pct}% RAM · {worst_disk}% disk"})
    if docker.get("available"):
        s = docker["summary"]
        crit = any(c["status"] == "crit" for c in docker["containers"])
        cards.append({"key": "containers", "label": "Containers",
                      "status": "crit" if crit else "warn" if s["problems"] else "ok",
                      "metric": f"{s['running']}/{s['total']} up",
                      "detail": f"{s['problems']} need attention" if s["problems"] else "all healthy"})
    else:
        cards.append({"key": "containers", "label": "Containers", "status": "info",
                      "metric": "—", "detail": "unavailable"})
    if systemd.get("available"):
        s = systemd["summary"]
        cards.append({"key": "services", "label": "Services",
                      "status": "crit" if s.get("failed") else "ok",
                      "metric": f"{s.get('running', 0)} running",
                      "detail": f"{s.get('failed', 0)} failed" if s.get("failed") else "none failed"})
    else:
        cards.append({"key": "services", "label": "Services", "status": "info",
                      "metric": "—", "detail": "unavailable"})
    return cards

# ── Update check (GitHub releases) ────────────────────────────────────────────
# Hits the public releases endpoint, caches for UPDATE_TTL, compares against
# VERSION. Surfaces via /api/health → dashboard badge. Off when CHECK_UPDATES
# is false; degrades silently to "not available" on network/rate-limit errors
# so the UI never lights up red because GitHub blinked.
_UPDATE_CACHE = {"at": 0, "data": None}
_UPDATE_LOCK  = threading.Lock()

def _render_markdown(md_text):
    """Render Markdown to HTML via GitHub's /markdown endpoint so the update modal
    can show release notes as proper prose (headings, lists, code, tables) rather
    than raw `##` markup. GitHub sanitises the output server-side, so it's safe to
    set as innerHTML. Returns None on failure — caller falls back to plain text."""
    if not md_text:
        return None
    try:
        body = json.dumps({"text": md_text, "mode": "gfm", "context": UPDATE_REPO}).encode("utf-8")
        req = urllib.request.Request(
            "https://api.github.com/markdown", data=body,
            headers={"Content-Type": "application/json",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": f"homelab-monitor/{VERSION}"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.read().decode("utf-8")
    except Exception:
        return None

def _parse_semver(v):
    """Tolerant semver parser: 'v0.5.0' / '0.5.0-beta' → (0, 5, 0). Missing
    components → 0. Anything unparseable falls back to 0 so a malformed tag
    just looks like an older version, never crashes the comparison."""
    s = (v or "").lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    out = []
    for p in s.split("."):
        try: out.append(int(p))
        except ValueError: out.append(0)
    return tuple(out) or (0,)

def collect_update():
    if not CHECK_UPDATES:
        return {"available": False, "current": VERSION, "disabled": True}
    now = int(time.time())
    with _UPDATE_LOCK:
        cached = _UPDATE_CACHE["data"]
        if cached:
            ttl = UPDATE_TTL_POSITIVE if cached.get("available") else UPDATE_TTL_NEGATIVE
            if (now - _UPDATE_CACHE["at"]) < ttl:
                return cached
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": f"homelab-monitor/{VERSION}"})
        with urllib.request.urlopen(req, timeout=8) as r:
            payload = json.loads(r.read())
    except Exception as e:
        # Network down, rate-limited, no releases yet — degrade silently. We
        # still cache the negative result so we don't hammer GitHub on retry.
        data = {"available": False, "current": VERSION, "error": str(e)[:200]}
        with _UPDATE_LOCK:
            _UPDATE_CACHE.update({"at": now, "data": data})
        return data
    latest = (payload.get("tag_name") or "").lstrip("vV")
    data = {
        "available":    bool(latest) and _parse_semver(latest) > _parse_semver(VERSION),
        "current":      VERSION,
        "latest":       latest or None,
        "release_url":  payload.get("html_url"),
        "release_name": payload.get("name") or (latest and f"v{latest}") or None,
        "release_notes": payload.get("body") or "",
        "published_at": payload.get("published_at"),
        "checked_at":   now,
    }
    # Only render markdown when there's actually an update — the rendered HTML
    # is only ever shown inside the "update available" modal, so rendering on
    # the up-to-date path would waste a GitHub API call every cycle.
    if data["available"]:
        html = _render_markdown(data["release_notes"])
        if html: data["release_notes_html"] = html
    with _UPDATE_LOCK:
        _UPDATE_CACHE.update({"at": now, "data": data})
    return data

# ── Opt-in one-click self-update ──────────────────────────────────────────────
# A container can't `docker compose up -d` its own container in-process — the
# process dies mid-recreate. So we delegate the recreate to a DETACHED docker:cli
# helper that runs the project's compose file and reports progress through the
# shared ./data bind mount (update_state.json + update.log). The app, once it
# restarts on the new image, just reads those files back for the status endpoint.
SELF_UPDATE_IMAGE = "sikamikaniko123/homelab-monitor"   # the image this app ships as
_SELF_UPDATE_STALE_SEC = 15 * 60   # a non-terminal job older than this is abandoned
_SELF_UPDATE_DONE = ("done", "failed", "rolled_back")

def _update_state_path():
    return os.path.join(_data_dir(), "update_state.json")

def _update_log_path():
    return os.path.join(_data_dir(), "update.log")

def _read_update_state():
    try:
        with open(_update_state_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _tail_lines(path, n=200):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except Exception:
        return ""

def _self_container_name():
    return os.environ.get("HOSTNAME") or "homelab-monitor"

def _self_update_script(working_dir, project, config_files, service,
                        target, target_image, previous_image, port):
    """Build the shell the detached docker:cli helper runs. It writes
    update_state.json atomically (temp + mv) and appends human lines to
    update.log at each step, then health-gates the new container and rolls back
    to previous_image on failure. All label-derived values are shell-quoted.

    The forward pull/up pins MONITOR_IMAGE=<target_image> (the immutable
    :x.y.z tag) so compose pulls that exact version instead of the moving
    :latest; rollback pins MONITOR_IMAGE=<previous_image> (the prior digest)
    so `up` genuinely restores the image we were running before."""
    q = shlex.quote
    fflags = " ".join("-f " + q(c) for c in config_files if c)
    compose = "docker compose %s -p %s" % (fflags, q(project))
    svc = q(service) if service else ""
    state_json = q(os.path.join(working_dir, "data", "update_state.json"))
    log = q(os.path.join(working_dir, "data", "update.log"))
    # 127.0.0.1 (not "localhost"): the helper's BusyBox wget resolves "localhost"
    # to IPv6 ::1 first, but the monitor's Flask binds IPv4 0.0.0.0 only — so a
    # "localhost" gate gets connection-refused on [::1] and the update would
    # always roll back. Force IPv4 to actually reach the monitor on the host.
    health = "http://127.0.0.1:%d/api/health" % port
    # MONITOR_IMAGE=<ref> prefix feeds compose's ${MONITOR_IMAGE:-…}
    # interpolation. Forward = the versioned target tag; rollback = the
    # exact previous image ref/digest. Shell-quoted so digests are safe.
    fwd_env  = "MONITOR_IMAGE=%s " % q(target_image)
    prev_env = "MONITOR_IMAGE=%s " % q(previous_image)
    # set_state writes {state, target, previous_image, ...} merging optional
    # error/new_image; mv makes the replace atomic-ish for the polling reader.
    return r'''
set -u
WD=%(wd)s
STATE=%(state)s
LOG=%(log)s
TARGET=%(target)s
PREV=%(prev)s
log(){ printf '%%s %%s\n' "$(date -u +%%H:%%M:%%S)" "$1" >> "$LOG" 2>/dev/null; }
set_state(){ # $1=state  $2=extra-json (optional, no leading comma)
  extra=""; [ -n "${2:-}" ] && extra=",$2"
  printf '{"state":"%%s","target":"%%s","previous_image":"%%s","updated_at":%%s%%s}' \
    "$1" "$TARGET" "$PREV" "$(date +%%s)" "$extra" > "$STATE.tmp" 2>/dev/null \
    && mv "$STATE.tmp" "$STATE" 2>/dev/null; }
cd "$WD" || { log "cannot cd to $WD"; set_state failed '"error":"cd failed"'; exit 1; }

log "pulling new image (%(svc)s)…"
if ! %(fwd_env)s%(compose)s pull %(svc)s >> "$LOG" 2>&1; then
  log "pull failed"; set_state failed '"error":"docker compose pull failed"'; exit 1
fi

log "recreating container on the new image…"
set_state restarting
if ! %(fwd_env)s%(compose)s up -d --no-build %(svc)s >> "$LOG" 2>&1; then
  log "up failed — rolling back to $PREV"
  %(prev_env)s%(compose)s up -d --no-build %(svc)s >> "$LOG" 2>&1
  set_state rolled_back '"error":"recreate failed; previous image restored"'; exit 1
fi

log "waiting for the new version to come up…"
ok=0
i=0
while [ $i -lt 30 ]; do
  cur=$(wget -qO- %(health)s 2>/dev/null | tr -d ' \n' | sed -n 's/.*"update":{[^}]*"current":"\([^"]*\)".*/\1/p')
  if [ -n "$cur" ] && [ "$cur" = "$TARGET" ]; then ok=1; break; fi
  i=$((i+1)); sleep 2
done

if [ $ok -eq 1 ]; then
  log "healthy on v$TARGET — update complete"
  set_state done '"new_image":"%(img)s:'"$TARGET"'"'
else
  log "health-gate failed (never reported v$TARGET) — rolling back to $PREV"
  %(prev_env)s%(compose)s up -d --no-build %(svc)s >> "$LOG" 2>&1
  set_state rolled_back '"error":"new version failed health-check; previous version restored"'
fi
''' % {"wd": q(working_dir), "state": state_json, "log": log,
       "target": q(target), "prev": q(previous_image),
       "compose": compose, "svc": svc, "health": health,
       "fwd_env": fwd_env, "prev_env": prev_env,
       "img": SELF_UPDATE_IMAGE}

def start_self_update(force=False):
    """Kick off the detached self-update helper. Returns (status_code, dict).
    Degrades to error dicts (never raises) so the route can jsonify them."""
    if not ALLOW_SELF_UPDATE:
        return 400, {"ok": False, "error": "Self-update is disabled. Set ALLOW_SELF_UPDATE=1 to enable it."}

    upd = collect_update()
    if not force and not upd.get("available"):
        return 400, {"ok": False, "error": "No update available."}
    target = (upd.get("latest") or "").lstrip("vV")
    if not target:
        return 400, {"ok": False, "error": "Could not determine the target version to update to."}

    # Singleton guard: a fresh non-terminal job blocks a second run.
    st = _read_update_state()
    if st and st.get("state") not in _SELF_UPDATE_DONE:
        age = time.time() - (st.get("updated_at") or st.get("started_at") or 0)
        if age < _SELF_UPDATE_STALE_SEC:
            return 409, {"ok": False, "error": "An update is already in progress.", "state": st}

    # Self-inspect to learn our image + compose labels.
    name = _self_container_name()
    try:
        code, raw = _docker_req("GET", "/containers/%s/json" % urllib.parse.quote(name))
    except Exception as e:
        return 400, {"ok": False, "error": "Could not reach the Docker socket: %s" % e}
    if code != 200:
        return 400, {"ok": False, "error": "Could not inspect own container '%s' (HTTP %s). Is the docker socket mounted read-write?" % (name, code)}
    try:
        info = json.loads(raw)
    except Exception:
        return 400, {"ok": False, "error": "Unexpected response inspecting own container."}

    cfg = info.get("Config") or {}
    labels = cfg.get("Labels") or {}
    # The repo-qualified ref we were deployed from. Prefer Config.Image (the
    # "repo:tag" / "repo@digest" the container was created with) over the
    # top-level Image, which on modern Docker is the bare local image ID
    # ("sha256:<id>") — that has no repo to derive the versioned target tag from,
    # so it would yield a bogus target like "sha256:0.16.0".
    previous_image = cfg.get("Image") or info.get("Image") or ""   # the running image ref/digest
    project    = labels.get("com.docker.compose.project")
    cfg_files  = labels.get("com.docker.compose.project.config_files") or ""
    working_dir = labels.get("com.docker.compose.project.working_dir")
    service    = labels.get("com.docker.compose.service")
    config_files = [c.strip() for c in cfg_files.split(",") if c.strip()]

    # PREFLIGHT: refuse on a plain `docker run` deploy — without compose labels we
    # have nothing to recreate from, so don't half-run.
    if not (project and config_files and working_dir):
        return 400, {"ok": False, "error": (
            "This container wasn't started with docker compose (no compose labels found), "
            "so the one-click update can't recreate it. Use the manual command instead.")}

    # Derive the immutable target image ref from the image we're currently
    # running: strip the tag/digest off the running ref to get the repo, then
    # re-attach the versioned tag. Pulling :x.y.z (not the moving :latest) is
    # what closes the "pull races a freshly-pushed :latest" window. A digest ref
    # (repo@sha256:…) splits on '@'; a tag ref (repo:tag) splits on the last ':'
    # that isn't part of a registry host:port (i.e. after the last '/').
    repo = previous_image
    if "@" in repo:
        repo = repo.split("@", 1)[0]
    else:
        last_seg = repo.rsplit("/", 1)[-1]
        if ":" in last_seg:
            repo = repo.rsplit(":", 1)[0]
    target_image = "%s:%s" % (repo, target) if repo else "%s:%s" % (SELF_UPDATE_IMAGE, target)

    started_at = int(time.time())
    state = {"state": "starting", "target": target, "target_image": target_image,
             "previous_image": previous_image,
             "started_at": started_at, "updated_at": started_at, "log": "update.log"}
    try:
        os.makedirs(_data_dir(), exist_ok=True)
        with open(_update_state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f)
        open(_update_log_path(), "w").close()   # truncate
    except Exception as e:
        return 400, {"ok": False, "error": "Cannot write to the data directory: %s" % e}

    script = _self_update_script(working_dir, project, config_files, service,
                                 target, target_image, previous_image, PORT)
    create_body = {
        "Image": SELF_UPDATE_HELPER_IMAGE,
        "Cmd": ["sh", "-lc", script],
        "HostConfig": {
            "AutoRemove": True,
            # Host networking so the helper's health-gate can reach the monitor at
            # the host's localhost:9800. The monitor runs with network_mode: host,
            # so on the default bridge the helper's "localhost" would be itself and
            # the gate could never confirm the new version → it'd always roll back.
            "NetworkMode": "host",
            "Binds": [
                "%s:/var/run/docker.sock" % DOCKER_SOCK,
                # Mount the compose project dir at the same path inside the helper
                # so `cd <working_dir>` works and ./data is the shared mount where
                # update.log / update_state.json land (the app reads them back).
                "%s:%s" % (working_dir, working_dir),
            ],
        },
    }
    # Ensure the helper image is present. On a fresh host docker:cli isn't pulled
    # yet, so the create below would fail with "No such image". Split the ref into
    # repo + tag the same way as the target image above: a digest splits on '@';
    # a tag is the ':' in the *last* path segment (a ':' before the last '/' is a
    # registry host:port, not a tag). Default tag is "latest" when none is given.
    h_ref = SELF_UPDATE_HELPER_IMAGE
    if "@" in h_ref:
        h_img, h_tag = h_ref.split("@", 1)            # repo@sha256:… → pull by digest
    else:
        last_seg = h_ref.rsplit("/", 1)[-1]
        if ":" in last_seg:
            h_img, h_tag = h_ref.rsplit(":", 1)
        else:
            h_img, h_tag = h_ref, "latest"
    try:
        # The pull API streams a chunked JSON body and only finishes once the pull
        # is done; _docker_req reads the body to completion, so create can't race
        # it. It's a fast no-op when the image is already up to date.
        code, raw = _docker_req("POST", "/images/create?fromImage=%s&tag=%s" % (
            urllib.parse.quote(h_img), urllib.parse.quote(h_tag)),
            timeout=600)
        body_txt = raw.decode("utf-8", "replace")
        if code not in (200, 201) or '"errorDetail"' in body_txt or '"error"' in body_txt:
            return 400, {"ok": False, "error": "Could not pull the update helper image '%s:%s': %s" % (
                h_img, h_tag, (body_txt[:200] if code not in (200, 201) else body_txt[-200:]))}
    except Exception as e:
        return 400, {"ok": False, "error": "Could not pull the update helper image '%s:%s': %s" % (h_img, h_tag, e)}

    try:
        code, raw = _docker_req("POST", "/containers/create", body=create_body)
        if code not in (200, 201):
            return 400, {"ok": False, "error": "Could not create the update helper (HTTP %s): %s" % (code, raw[:200].decode("utf-8", "replace"))}
        cid = (json.loads(raw) or {}).get("Id")
        code, raw = _docker_req("POST", "/containers/%s/start" % cid)
        if code not in (204, 304):
            return 400, {"ok": False, "error": "Could not start the update helper (HTTP %s)." % code}
    except Exception as e:
        return 400, {"ok": False, "error": "Failed to launch the update helper: %s" % e}

    return 202, {"ok": True, "state": state}

# ── Newer-OS-release check (endoflife.date) ───────────────────────────────────
# The probe reports each host's pending *package* updates offline. Detecting that
# a whole new distro release exists needs to know the latest upstream version, so
# the hub fetches it once per distro from endoflife.date and caches it (same split
# TTL as the app-update check). os_upgrade_for() then compares a host's
# version_id against the newest cycle — pure cache read, no network at serve time.
# Degrades silently on any error so an offline hub never lights up the UI red.

# os-release ID → endoflife.date product slug. Rolling distros (arch, gentoo,
# nixos, tumbleweed, void) have no fixed release to be "behind" → intentionally
# absent so they're skipped.
# Verified against endoflife.date's /api/<slug>.json (slugs are not always the
# obvious os-release ID — Leap is "opensuse", Rocky is "rocky-linux").
_EOL_SLUG = {
    "ubuntu": "ubuntu", "pop": "ubuntu", "neon": "ubuntu", "elementary": "ubuntu",
    "debian": "debian", "raspbian": "debian",
    "linuxmint": "linuxmint",
    "opensuse-leap": "opensuse", "opensuse": "opensuse", "sles": "sles",
    "fedora": "fedora",
    "almalinux": "almalinux", "rocky": "rocky-linux", "rhel": "rhel",
    "centos": "centos",
    "amzn": "amazon-linux", "alpine": "alpine",
}
_DISTRO_NAME = {
    "ubuntu": "Ubuntu", "debian": "Debian", "linuxmint": "Linux Mint",
    "opensuse": "openSUSE Leap", "sles": "SLES", "fedora": "Fedora",
    "almalinux": "AlmaLinux", "rocky-linux": "Rocky Linux", "rhel": "RHEL",
    "centos": "CentOS", "amazon-linux": "Amazon Linux", "alpine": "Alpine",
}
_OS_REL_CACHE = {}        # slug -> {"at": ts, "cycles": [{cycle,lts,eol,releaseDate}, ...] | None}
_OS_REL_LOCK  = threading.Lock()

def _fetch_eol_cycles(slug):
    """Fetch release cycles for one distro from endoflife.date, newest-first.
    Returns a list of dicts {cycle, lts, eol, releaseDate} (we keep lts/eol/
    releaseDate so os_upgrade_for can avoid recommending an unreleased, EOL, or
    interim release), or None on error. Tolerates the endoflife v1 {result:[...]}
    envelope and error bodies so a future API shape change can't crash us."""
    try:
        req = urllib.request.Request(
            f"https://endoflife.date/api/{slug}.json",
            headers={"Accept": "application/json",
                     "User-Agent": f"homelab-monitor/{VERSION}"})
        with urllib.request.urlopen(req, timeout=8) as r:
            payload = json.loads(r.read())
        if isinstance(payload, dict):
            payload = payload.get("result") or []
        if not isinstance(payload, list):
            return None
        out = [{"cycle": str(c.get("cycle")), "lts": c.get("lts"),
                "eol": c.get("eol"), "releaseDate": c.get("releaseDate")}
               for c in payload if isinstance(c, dict) and c.get("cycle") is not None]
        return out or None
    except Exception:
        return None

def collect_os_releases():
    """Refresh the endoflife cache for every distro present in the fleet. Cheap:
    only fetches slugs whose cache entry is stale, and only ones we actually have
    a host for. Called from health_scan(); never raises out."""
    if not CHECK_OS_UPDATES:
        return
    slugs = set()
    for _name, host in _iter_fleet_hosts():
        osid = ((host or {}).get("os") or {}).get("id")
        slug = _EOL_SLUG.get((osid or "").lower())
        if slug:
            slugs.add(slug)
    now = int(time.time())
    for slug in slugs:
        with _OS_REL_LOCK:
            ent = _OS_REL_CACHE.get(slug)
            if ent:
                ttl = UPDATE_TTL_POSITIVE if ent.get("cycles") else UPDATE_TTL_NEGATIVE
                if (now - ent["at"]) < ttl:
                    continue
        cycles = _fetch_eol_cycles(slug)
        with _OS_REL_LOCK:
            _OS_REL_CACHE[slug] = {"at": now, "cycles": cycles}

def os_upgrade_for(os_id, version_id):
    """Compare a host's distro version against the newest known cycle (cache only,
    no network). Returns {"new_release": {current, candidate, label}} when a newer
    release exists, else None.

    endoflife.date returns cycles newest-first, so we locate the host's own cycle
    and flag it behind only when newer cycles sit before it (list order, not a
    numeric compare — openSUSE Leap renumbered 42.x → 15.x). The candidate is the
    *nearest* qualifying newer cycle (smallest jump), NOT the newest one: distro
    upgrade tooling only moves one release/LTS at a time, so a 22.04-LTS host's
    actionable target is 24.04, not whatever the latest LTS happens to be. Each
    candidate must be already released, still supported, and — if the host is on
    an LTS — itself LTS. That keeps us from telling a 24.04-LTS host to 'upgrade'
    to a short-lived interim (or an unreleased) release, and from telling a
    22.04-LTS host to jump to a newer LTS it can't reach directly."""
    if not CHECK_OS_UPDATES or not os_id or not version_id:
        return None
    slug = _EOL_SLUG.get(os_id.lower())
    if not slug:
        return None
    with _OS_REL_LOCK:
        ent = _OS_REL_CACHE.get(slug)
    cycles = (ent or {}).get("cycles")
    if not cycles:
        return None
    try:
        strs = [c["cycle"] for c in cycles]
        idx = None
        if version_id in strs:
            idx = strs.index(version_id)
        else:                                   # host "12.5" → match cycle "12"
            major = version_id.split(".")[0]
            if major in strs:
                idx = strs.index(major)
        if idx is None or idx == 0:             # not found, or already newest
            return None
        host_is_lts = bool(cycles[idx].get("lts"))
        today = time.strftime("%Y-%m-%d")       # ISO dates compare lexicographically
        def released(c):
            rd = c.get("releaseDate")
            return (not rd) or str(rd) <= today
        def supported(c):
            eol = c.get("eol")
            if eol is True:  return False
            if eol in (False, None): return True
            return str(eol) > today
        candidate = None
        # Walk strictly-newer entries nearest-first (reversed: newest-first list →
        # closest-to-host first) so we recommend the next reachable release, not the
        # newest. The filters still skip any non-qualifying step in between.
        for c in reversed(cycles[:idx]):
            if released(c) and supported(c) and (not host_is_lts or c.get("lts")):
                candidate = c["cycle"]
                break
        if not candidate:
            return None
        name = _DISTRO_NAME.get(slug, slug)
        return {"new_release": {"current": version_id, "candidate": candidate,
                                "label": f"{name} {candidate}"}}
    except Exception:
        return None

def enrich_os_upgrade(host):
    """Return a shallow copy of `host` with os_upgrade set/cleared from the cached
    endoflife data. NON-mutating on purpose: callers hand us the live shared
    HOST_DATA/LATEST host dicts from request threads, and mutating those in place
    (while the poller swaps them and another request serializes them) risks a
    'dictionary changed size during iteration' RuntimeError. Only the top-level
    os_upgrade key differs, so a shallow copy is enough."""
    if host is None:
        return host
    os_ = host.get("os") or {}
    up = os_upgrade_for(os_.get("id"), os_.get("version_id"))
    out = dict(host)
    if up:
        out["os_upgrade"] = up
    else:
        out.pop("os_upgrade", None)
    return out

def _iter_fleet_hosts():
    """Yield (name, host_block) for the hub itself plus every remote with data.
    Used by both the endoflife refresh and the /api/health fleet update summary."""
    yield "local", _local_now_snapshot()
    with HOST_DATA_LOCK:
        items = list(HOST_DATA.items())
    for name, entry in items:
        host = (entry.get("data") or {}).get("host")
        if host:
            yield name, host

def os_updates_summary():
    """Fleet-wide roll-up that drives the header badge: how many hosts are behind,
    total pending updates, security count, and which hosts have a newer release.

    `hosts` carries the per-host breakdown (package count, security count, kernel
    flag, new-release label) so the modal can render a proper per-machine table
    instead of pointing the user back at each host's Security tab."""
    hosts_behind = total = security = 0
    new_release, hosts = [], []
    for name, host in _iter_fleet_hosts():
        upd = ((host or {}).get("sec") or {}).get("updates") or {}
        cnt = upd.get("count") or 0
        sec = upd.get("security") or 0
        kernel = bool(upd.get("kernel"))
        rel = (enrich_os_upgrade(host) or {}).get("os_upgrade", {}).get("new_release")
        total += cnt
        security += sec
        if cnt > 0 or rel:
            hosts_behind += 1
            hosts.append({"host": name, "count": cnt, "security": sec,
                          "kernel": kernel,
                          "release": rel.get("label") if rel else None})
        if rel:
            new_release.append({"host": name, "label": rel.get("label")})
    return {"hosts_behind": hosts_behind, "total_updates": total,
            "security": security, "new_release": new_release, "hosts": hosts}

# ── Local capability diagnostics ──────────────────────────────────────────────
# A "which requirements are met?" checklist for the hub's own host, in the SAME
# {checks:[{id,label,status,detail,remedy?}], summary} shape the remote probe_host()
# already produces — so the dashboard renders both with one code path. The point:
# a deploy that's missing an optional mount (or has no GPU) should STILL run and
# clearly say what's degraded and how to fix it, instead of failing cryptically.

def _diag(checks, cid, label, status, detail, remedy=None):
    item = {"id": cid, "label": label, "status": status, "detail": detail}
    if remedy:
        item["remedy"] = remedy
    checks.append(item)

def local_diagnostics():
    checks = []
    # GPU (optional) — info, not a failure: the monitor is useful without one.
    # Vendor-aware: NVIDIA is read via nvidia-smi, AMD via the amdgpu sysfs interface
    # (no ROCm). We keep the diagnostic id "nvidia" (stable i18n/DOM key) but label
    # and remediate for whichever vendor is actually present, so an AMD user isn't
    # told to install the NVIDIA runtime.
    vram = round(LATEST.get("mem_total") or 0)
    if LATEST.get("gpu_avail"):
        label, src = {
            "amd":    ("GPU (AMD)", "amdgpu sysfs"),
            "hybrid": ("GPU (NVIDIA + AMD)", "nvidia-smi + amdgpu sysfs"),
        }.get(LATEST.get("gpu_vendor"), ("GPU (NVIDIA)", "nvidia-smi"))
        _diag(checks, "nvidia", label, "ok", f"{src} OK · {vram} MB VRAM")
    else:
        _diag(checks, "nvidia", "GPU", "info",
              "no GPU detected — GPU panels are hidden (everything else works)",
              {"where": "on the host, if it has a GPU. AMD (amdgpu) is picked up "
                        "automatically from the kernel's sysfs — no ROCm, no extra "
                        "config; if a Radeon still isn't seen, check the sysfs nodes "
                        "below exist (older kernels/APUs may not expose them). NVIDIA "
                        "needs nvidia-smi injected: the env vars below only take effect "
                        "when nvidia is Docker's DEFAULT runtime, and the recreate step "
                        "is what was missing if a previous attempt 'did nothing'.",
               "cmd": "# AMD — confirm the kernel exposes the card (no install needed):\n"
                      "for c in /sys/class/drm/card*/device; do echo \"$c:\"; "
                      "cat $c/vendor $c/mem_info_vram_total $c/gpu_busy_percent 2>&1; done\n"
                      "# NVIDIA — make nvidia Docker's default runtime, then RECREATE:\n"
                      "sudo nvidia-ctk runtime configure --runtime=docker --set-as-default\n"
                      "sudo systemctl restart docker\n"
                      "docker compose up -d --force-recreate   # recreate — restart keeps the old runtime\n"
                      "# don't want nvidia as the global default? skip the three lines above and instead run:\n"
                      "#   docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d"})
    # Docker socket — powers Containers + Services + model APIs.
    try:
        json.loads(_docker("/version"))
        _diag(checks, "docker", "Docker socket", "ok", f"reachable at {DOCKER_SOCK}")
    except Exception:
        _diag(checks, "docker", "Docker socket", "warn",
              "not reachable — Containers & model panels are limited",
              {"where": "in docker-compose.yml volumes:",
               "cmd": "- /var/run/docker.sock:/var/run/docker.sock:ro"})
    # Host root — disk usage for the real host rather than the container.
    if os.path.isdir(HOST_ROOT):
        _diag(checks, "host_root", "Host filesystem", "ok", f"mounted at {HOST_ROOT}")
    else:
        _diag(checks, "host_root", "Host filesystem", "warn",
              "host root not mounted — disk stats show the container only",
              {"where": "in docker-compose.yml volumes:",
               "cmd": "- /:/rootfs:ro"})
    # systemd D-Bus — optional Services panel.
    bus = (os.environ.get("DBUS_SYSTEM_BUS_ADDRESS", "").split("unix:path=")[-1]
           or "/run/dbus/system_bus_socket")
    if os.path.exists(bus):
        _diag(checks, "dbus", "systemd D-Bus", "ok", "socket present — Services panel enabled")
    else:
        _diag(checks, "dbus", "systemd D-Bus", "info",
              "socket not mounted — the Services (systemd) panel is unavailable",
              {"where": "in docker-compose.yml volumes:",
               "cmd": "- /run/dbus/system_bus_socket:/run/dbus/system_bus_socket:ro"})
    # History persistence — in-memory fallback when /data isn't writable.
    if DB_EPHEMERAL:
        _diag(checks, "data", "History storage", "warn",
              "running in-memory — history resets on restart",
              {"where": "in docker-compose.yml volumes:",
               "cmd": "- ./data:/data"})
    else:
        _diag(checks, "data", "History storage", "ok", f"persisting to {DB_PATH}")
    # Host metrics — /proc + thermal sensors (needs pid:host and the rootfs mount).
    if os.path.exists("/proc/stat") and glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        _diag(checks, "host_metrics", "Host metrics", "ok", "CPU / RAM / temperature readable")
    elif os.path.exists("/proc/stat"):
        _diag(checks, "host_metrics", "Host metrics", "info",
              "CPU / RAM readable; no thermal sensors exposed on this host")
    else:
        _diag(checks, "host_metrics", "Host metrics", "warn", "/proc not readable — host metrics limited")
    return {"checks": checks, "summary": _summarize(checks)}

# ── Top processes (issue #32): a psutil-free /proc reader for the Host tab's
# mini-htop. Aggregates by command so N workers of one exe collapse into a single
# row, and derives CPU% from the jiffy delta between two health scans (~15s
# apart) against /proc/stat's total. The first scan after boot has no delta, so
# CPU% reads 0 until the next cycle. Builds on the /proc parsing seeded in #34
# (thanks @samkelomncwabe63-svg).
_PROC_PREV = {"total": None, "pids": {}}
_PROC_PAGE_KB = (os.sysconf("SC_PAGE_SIZE") // 1024) if hasattr(os, "sysconf") else 4

def _total_cpu_jiffies():
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]   # cpu  user nice system idle iowait …
    return sum(int(x) for x in parts)

# ── Per-process disk I/O attribution ("who is actually driving that spike") ────
# Sample ONLY the bounded top-N candidate set collect_top_processes already
# computes (by CPU delta + by RAM), one small read each per poll — never a full
# /proc scan. Deltas across polls give per-process read_B/s + write_B/s; prev
# state is keyed by pid and guarded by the process start-time so a recycled pid
# can't inherit a stale counter. /proc/<pid>/io needs matching privilege:
# unreadable (PermissionError/missing) -> degrade silently to no attribution.
_PROC_IO_PREV = {}   # pid(str) -> (starttime:int, read_bytes:int, write_bytes:int, ts:float)

def _fmt_bps(b):
    """Human byte-rate: MB/s, KB/s or B/s. Never raises."""
    try:
        b = float(b or 0)
    except (TypeError, ValueError):
        return "0 B/s"
    if b >= 1048576:
        return "%.1f MB/s" % (b / 1048576.0)
    if b >= 1024:
        return "%.0f KB/s" % (b / 1024.0)
    return "%d B/s" % int(b)

def _read_proc_io(pid):
    """Return (read_bytes, write_bytes) from /proc/<pid>/io, or None when it can't
    be read (no privilege / pid gone / field absent). Never raises."""
    try:
        with open("/proc/%s/io" % pid) as f:
            data = f.read()
    except (OSError, ValueError):
        return None
    rb = wb = None
    for line in data.splitlines():
        if line.startswith("read_bytes:"):
            try: rb = int(line.split(":", 1)[1])
            except (ValueError, IndexError): pass
        elif line.startswith("write_bytes:"):
            try: wb = int(line.split(":", 1)[1])
            except (ValueError, IndexError): pass
    if rb is None or wb is None:
        return None
    return (rb, wb)

def collect_proc_disk_io(candidates, now=None):
    """Per-process disk read/write throughput for a BOUNDED candidate set.
    `candidates`: iterable of (pid_str, comm, starttime) — the monitor's existing
    top-by-CPU / top-by-RAM pids only, so cost stays O(top-N) reads, not O(all
    pids). Reads /proc/<pid>/io for each, computes B/s from the delta vs the
    previous poll, guarding pid reuse (start-time mismatch -> reset) and counter
    resets/negatives (-> drop the sample). Returns {"available": False} when
    /proc/<pid>/io is unreadable for EVERY candidate (no privilege / non-Linux)
    so the feature is simply absent; otherwise the top writer/reader plus short
    leader lists. Never surfaces cmdline/argv — only the process comm."""
    if now is None:
        now = time.time()
    rows, seen, any_readable = [], set(), False
    for pid, comm, starttime in candidates:
        pid = str(pid)
        seen.add(pid)
        io = _read_proc_io(pid)
        if io is None:
            continue
        any_readable = True
        rb, wb = io
        prev = _PROC_IO_PREV.get(pid)
        _PROC_IO_PREV[pid] = (starttime, rb, wb, now)
        if not prev:
            continue                                   # first poll for this pid: warm up
        p_start, p_rb, p_wb, p_ts = prev
        if p_start != starttime:
            continue                                   # pid recycled -> drop stale delta
        dt = now - p_ts
        if dt <= 0:
            continue
        d_rb, d_wb = rb - p_rb, wb - p_wb
        if d_rb < 0 or d_wb < 0:
            continue                                   # counter reset/wrap -> skip
        rows.append({"name": comm, "pid": int(pid),
                     "read_b_s":  int(d_rb / dt),
                     "write_b_s": int(d_wb / dt)})
    # Prune prev state to the current candidate set so it can't grow unbounded.
    for dead in [p for p in _PROC_IO_PREV if p not in seen]:
        _PROC_IO_PREV.pop(dead, None)
    if not any_readable:
        return {"available": False}                    # no /proc/<pid>/io access at all
    writers = sorted((r for r in rows if r["write_b_s"] > 0), key=lambda r: -r["write_b_s"])
    readers = sorted((r for r in rows if r["read_b_s"]  > 0), key=lambda r: -r["read_b_s"])
    return {
        "available": True,
        "top_writer": writers[0] if writers else None,
        "top_reader": readers[0] if readers else None,
        "writers": writers[:5],
        "readers": readers[:5],
    }

def collect_top_processes(top_n=10):
    """Top-N processes by CPU% and by RAM, aggregated by command. Reads /proc
    directly (no psutil); returns None where /proc isn't available (e.g. a
    Windows hub) so the card simply hides rather than erroring."""
    try:
        total = _total_cpu_jiffies()
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except Exception:
        return None
    prev_total = _PROC_PREV["total"]
    prev_pids  = _PROC_PREV["pids"]
    cur_pids, agg = {}, {}
    # Per-pid meta kept only long enough to pick the bounded I/O-attribution set
    # (top-N by CPU delta + top-N by RAM) — we do NOT read /proc/<pid>/io for all.
    pid_meta = []   # (pid, comm, starttime, rss_kb, dcpu)
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
            rp = stat.rfind(")")                 # comm can hold spaces/parens
            comm = stat[stat.find("(") + 1:rp]
            rest = stat[rp + 2:].split()         # fields from 'state' onward
            jiff = int(rest[11]) + int(rest[12]) # utime + stime
            starttime = int(rest[19])            # field 22: start-time (pid-reuse guard)
            with open(f"/proc/{pid}/statm") as f:
                rss_kb = int(f.read().split()[1]) * _PROC_PAGE_KB
        except (OSError, ValueError, IndexError):
            continue
        cur_pids[pid] = jiff
        dpid = (jiff - prev_pids[pid]) if pid in prev_pids else 0
        pid_meta.append((pid, comm, starttime, rss_kb, dpid if dpid > 0 else 0))
        a = agg.setdefault(comm, {"mem_kb": 0, "dcpu": 0, "count": 0})
        a["mem_kb"] += rss_kb
        a["count"]  += 1
        if dpid > 0:
            a["dcpu"] += dpid
    _PROC_PREV["total"] = total
    _PROC_PREV["pids"]  = cur_pids
    ncpu = os.cpu_count() or 1
    span = (total - prev_total) if prev_total else 0
    rows = []
    for comm, a in agg.items():
        cpu = (100.0 * a["dcpu"] / span * ncpu) if span > 0 else 0.0
        rows.append({"name": comm, "cpu_pct": round(cpu, 1),
                     "mem_mb": round(a["mem_kb"] / 1024), "count": a["count"]})
    # Bounded candidate set for per-process I/O attribution: union of the top-N by
    # CPU delta and top-N by RAM. Reuses the selection this function already runs
    # (never a full-/proc io scan) and keeps the heavy-hitters that plausibly drive
    # a disk spike in view across polls so their deltas accumulate.
    cand = {}   # pid -> (pid, comm, starttime)
    for m in sorted(pid_meta, key=lambda r: -r[4])[:top_n]:
        cand[m[0]] = (m[0], m[1], m[2])
    for m in sorted(pid_meta, key=lambda r: -r[3])[:top_n]:
        cand[m[0]] = (m[0], m[1], m[2])
    try:
        proc_io = collect_proc_disk_io(list(cand.values()))
    except Exception:
        proc_io = {"available": False}
    # Tie-break on the other metric — see probe.read_cpu_and_procs(), which
    # sorts identically. Idle commands all sit at 0.0% and would otherwise fall
    # back to /proc order (lowest pid first, i.e. kernel threads).
    return {"by_cpu": sorted(rows, key=lambda r: (-r["cpu_pct"], -r["mem_mb"]))[:top_n],
            "by_mem": sorted(rows, key=lambda r: (-r["mem_mb"], -r["cpu_pct"]))[:top_n],
            "ncpu": ncpu, "io": proc_io}

# ── Experiments: training-run detection + GPU activity sessions ────────────────
# Auto-recognise training / fine-tuning jobs from /proc cmdline, and reconstruct
# "GPU activity sessions" from the power/util history we already store — so the
# Experiments tab works with zero config and no agent inside the job.
TRAIN_LAUNCHERS = ("torchrun", "deepspeed", "torch.distributed.run",
                   "torch.distributed.launch", "torch.distributed.elastic", "accelerate")
_TRAIN_SCRIPT_RE = re.compile(r"(train|finetune|fine[_-]?tune|sft|dpo|grpo|ppo|pretrain|lora|qlora)", re.I)
_ML_FRAMEWORK_RE = re.compile(r"(pytorch_lightning|lightning\.|transformers\.trainer|"
                              r"axolotl|unsloth|trl|llama[_-]?factory|nanogpt|megatron)", re.I)

def _classify_training(argv):
    """argv: cmdline tokens. Return a short label if this looks like a training /
    fine-tuning job, else None. Conservative — must not flag plain inference."""
    if not argv:
        return None
    low = " ".join(argv).lower()
    for l in TRAIN_LAUNCHERS:
        if l in low:
            return l.split(".")[-1]
    for a in argv:
        if a.endswith(".py") and _TRAIN_SCRIPT_RE.search(os.path.basename(a)):
            return os.path.basename(a)
    if "python" in low and _ML_FRAMEWORK_RE.search(low):
        return "training"
    return None

_TRAIN_SEEN = {}   # pid -> first-seen ts (resets on restart; gives a live elapsed clock)

def collect_training(gpu_pids, now=None):
    """Scan /proc for training-like processes; annotate each with the VRAM its PID
    holds on the GPU (from nvidia-smi compute-apps) and how long we've seen it.
    `gpu_pids`: {int pid -> MB}. Returns [] where /proc is unavailable."""
    now = now or int(time.time())
    out, alive = [], set()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except Exception:
        return out
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read()
        except Exception:
            continue
        if not raw:
            continue
        argv = [t for t in raw.decode("utf-8", "replace").split("\x00") if t]
        label = _classify_training(argv)
        if not label:
            continue
        alive.add(pid)
        first = _TRAIN_SEEN.setdefault(pid, now)
        vram = gpu_pids.get(int(pid), 0)
        out.append({"pid": int(pid), "label": label, "cmd": " ".join(argv)[:240],
                    "elapsed": max(0, now - first), "vram": round(vram), "on_gpu": bool(vram)})
    for dead in [p for p in _TRAIN_SEEN if p not in alive]:
        _TRAIN_SEEN.pop(dead, None)
    return sorted(out, key=lambda r: -r["elapsed"])

# Data-science tooling that runs as a local web app — discovered from /proc cmdline
# so the dashboard links straight to your notebooks & experiment trackers, and can
# flag a notebook kernel quietly squatting on VRAM (the "my GPU is full but nothing
# is running" culprit). (kind, label, default_port, cmdline regex).
_DEVTOOLS = [
    ("jupyter",     "Jupyter",     8888, re.compile(r"jupyter[-_ ]?(lab|notebook|server)|jupyterlab", re.I)),
    ("tensorboard", "TensorBoard", 6006, re.compile(r"tensorboard", re.I)),
    ("mlflow",      "MLflow",      5000, re.compile(r"\bmlflow\b", re.I)),
    ("wandb",       "W&B",         8080, re.compile(r"\bwandb\b", re.I)),
    ("streamlit",   "Streamlit",   8501, re.compile(r"\bstreamlit\b", re.I)),
    ("ray",         "Ray",         8265, re.compile(r"raylet|ray\.dashboard|ray start", re.I)),
]
_PORT_RE = re.compile(r"^--?port(?:=(\d{2,5}))?$")

def _extract_port(argv):
    """Pull an explicit --port N / --port=N from a cmdline, else None."""
    for i, a in enumerate(argv):
        m = _PORT_RE.match(a)
        if m:
            if m.group(1):
                return int(m.group(1))
            if i + 1 < len(argv) and argv[i + 1].isdigit():
                return int(argv[i + 1])
    return None

def collect_devtools(gpu_pids):
    """Discover running DS/AI tools (Jupyter, TensorBoard, MLflow, W&B, …) from /proc
    and tag any that are holding GPU VRAM. Returns [] where /proc is unavailable."""
    out, seen = [], set()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except Exception:
        return out
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read()
        except Exception:
            continue
        if not raw:
            continue
        argv = [t for t in raw.decode("utf-8", "replace").split("\x00") if t]
        low = " ".join(argv).lower()
        for kind, label, dport, rx in _DEVTOOLS:
            if not rx.search(low):
                continue
            port = _extract_port(argv) or dport
            key = (kind, port)
            if key in seen:
                break
            seen.add(key)
            vram = gpu_pids.get(int(pid), 0)
            out.append({"kind": kind, "label": label, "port": port, "pid": int(pid),
                        "vram": round(vram), "idle_vram": bool(vram)})
            break
    return sorted(out, key=lambda r: r["label"])

_ACTIVE_UTIL = 20    # GPU util % at/above which a sample counts as a "busy" session sample

def _gpu_sessions(rows, interval, active_util=_ACTIVE_UTIL, max_gap=3, min_len=2, price=0.0):
    """Reconstruct contiguous GPU-busy sessions from sample rows. `rows`: ascending
    (ts, util, power, mem_used). A session is a run of samples with util>=active_util,
    tolerating up to `max_gap` idle samples. Returns sessions newest-first with
    duration, peak util/VRAM, average power, energy (kWh) and money."""
    kwh_per = interval / 3_600_000.0
    sessions, cur, gap = [], None, 0

    def close():
        nonlocal cur
        if cur and cur["n"] >= min_len:
            energy = cur["sum_power"] * kwh_per
            sessions.append({
                "start": cur["start"], "end": cur["end"],
                "duration": cur["end"] - cur["start"] + interval,
                "peak_util": round(cur["peak_util"]), "peak_vram": round(cur["peak_vram"]),
                "avg_power": round(cur["sum_power"] / cur["n"]),
                "energy_kwh": round(energy, 4), "cost": round(energy * price, 4)})
        cur = None

    for ts, util, power, mem in rows:
        if (util or 0) >= active_util:
            if cur is None:
                cur = {"start": ts, "end": ts, "peak_util": 0, "peak_vram": 0, "sum_power": 0.0, "n": 0}
            cur["end"] = ts
            cur["peak_util"] = max(cur["peak_util"], util or 0)
            cur["peak_vram"] = max(cur["peak_vram"], mem or 0)
            cur["sum_power"] += (power or 0)
            cur["n"] += 1
            gap = 0
        elif cur is not None:
            gap += 1
            if gap > max_gap:
                close(); gap = 0
    close()
    return sorted(sessions, key=lambda s: -s["start"])

def health_scan():
    HEALTH["docker"]  = collect_docker()
    HEALTH["systemd"] = collect_systemd()
    HEALTH["update"]  = collect_update()
    HEALTH["disk_io"] = collect_disk_io()
    # Top-processes is refreshed by sample_once (10s cadence) and cached on
    # HEALTH["processes"] — calling it here too would double-step the _PROC_PREV
    # jiffy deltas across two cadences and corrupt both. Reuse the cached value.
    collect_os_releases()
    HEALTH["at"]      = int(time.time())

# ── Settings (UI-managed; persisted in SQLite, no env required) ───────────────
# Everything the notifier needs is configured from the dashboard's Settings tab
# and stored in the `settings` table, so the container is plug-and-play and the
# operator never has to edit docker-compose.yml just to enable alerts.
# ── Hosts registry + SSH capability probe (Issue #35, slice 1) ────────────────
# A monitored host is just a (name, ssh_target) pair persisted in SQLite. The
# hub's own ed25519 keypair is generated on first boot under SSH_DIR (inside
# /data so it survives rebuilds). "Test connection" runs a real per-capability
# checklist via ssh and returns each result with an inline remedy when the user
# can fix it on the remote.

_HOST_NAME_RE  = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$", re.I)
_SSH_TARGET_RE = re.compile(r"^(?P<user>[a-z0-9_.-]+)@(?P<host>[a-z0-9._-]+)(?::(?P<port>\d{1,5}))?$", re.I)

def _ensure_ssh_keypair():
    """Generate hub ed25519 keypair on first boot; idempotent across restarts."""
    try:
        os.makedirs(SSH_DIR, mode=0o700, exist_ok=True)
    except Exception as e:
        print("ssh keydir error:", e, flush=True)
        return
    if os.path.exists(SSH_KEY):
        return
    try:
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-q", "-N", "",
                        "-C", "homelab-monitor@hub", "-f", SSH_KEY],
                       check=True, timeout=10)
        os.chmod(SSH_KEY, 0o600)
        if os.path.exists(SSH_KEY + ".pub"):
            os.chmod(SSH_KEY + ".pub", 0o644)
    except Exception as e:
        print("ssh-keygen failed:", e, flush=True)

def get_hub_pubkey():
    pub = SSH_KEY + ".pub"
    if not os.path.exists(pub):
        _ensure_ssh_keypair()
    try:
        with open(pub) as f:
            return f.read().strip()
    except Exception as e:
        return f"# pubkey unavailable: {e}"

def _parse_ssh_target(t):
    m = _SSH_TARGET_RE.match((t or "").strip())
    if not m:
        return None
    g = m.groupdict()
    port = int(g["port"]) if g["port"] else 22
    if not (1 <= port <= 65535):
        return None
    # Defence-in-depth: a user/host beginning with '-' could be read as an ssh
    # option (e.g. "-oProxyCommand=…") if it ever reached argv unguarded. The
    # ssh calls below also pass the destination after "--", so this is the belt
    # to that suspenders.
    if g["user"].startswith("-") or g["host"].startswith("-"):
        return None
    return g["user"], g["host"], port

def list_hosts():
    from backend.db.repos import hosts as _hosts_repo
    with LOCK:
        rows = _hosts_repo.list_all(conn=DB)
    out = []
    for name, target, tags, added, checked, blob, ptimeout, pcal in rows:
        try:
            check = json.loads(blob) if blob else None
        except Exception:
            check = None
        out.append({"name": name, "ssh_target": target,
                    "tags": [t for t in (tags or "").split(",") if t],
                    "added_at": added, "last_check_at": checked, "last_check": check,
                    # Adaptive poll budget (#99): None until a host is calibrated.
                    "poll_timeout": int(ptimeout) if ptimeout else None,
                    "poll_calibrated_at": pcal})
    return out

def add_host(name, ssh_target, tags=""):
    from backend.db.repos import hosts as _hosts_repo
    if not _HOST_NAME_RE.match(name or ""):
        return None, "Name must be 1–31 chars: letters, digits, '_' or '-', starting with a letter or digit."
    if _parse_ssh_target(ssh_target) is None:
        return None, "SSH target must look like user@host or user@host:port."
    with LOCK:
        try:
            _hosts_repo.insert(name, ssh_target.strip(), (tags or "").strip(), int(time.time()), conn=DB)
        except sqlite3.IntegrityError:
            return None, f"A host named '{name}' already exists."
    return {"name": name, "ssh_target": ssh_target.strip()}, None

def delete_host(name):
    from backend.db.repos import hosts as _hosts_repo
    with LOCK:
        rowcount = _hosts_repo.delete(name, conn=DB)
    return rowcount > 0

def rename_host(old, new):
    """Rename a registered host. The hosts row moves as-is (SSH target, tags,
    poll calibration, last check), the in-memory poll cache is re-keyed so the
    UI doesn't fall back to "no data yet" for a poll interval, and experiment /
    benchmark history recorded under the old name follows so per-host filters
    don't silently split. Returns (host_dict, error_or_None)."""
    from backend.db.repos import hosts as _hosts_repo
    new = (new or "").strip()
    if not _HOST_NAME_RE.match(new):
        return None, "Name must be 1–31 chars: letters, digits, '_' or '-', starting with a letter or digit."
    if new.lower() == "local":
        return None, "'local' is reserved for the hub itself."
    if new == old:
        return None, "Nothing to update."
    with LOCK:
        try:
            rowcount = _hosts_repo.rename(old, new, conn=DB)
        except sqlite3.IntegrityError:
            return None, f"A host named '{new}' already exists."
        if rowcount == 0:
            return None, f"No host named '{old}'."
        DB.execute("UPDATE runs SET host=? WHERE host=?", (new, old))
        DB.execute("UPDATE bench_runs SET host=? WHERE host=?", (new, old))
        from backend.db.repos import host_samples as _hs_repo
        _hs_repo.rename_host(old, new, conn=DB)
        # Per-card GPU history and per-service VRAM are keyed by host name too;
        # without this a rename orphans the whole cockpit history under the old
        # name while the renamed host shows an empty GPU tab.
        from backend.db.repos import gpu_samples as _gs_repo
        _gs_repo.rename_host(old, new, conn=DB)
        DB.commit()
    with HOST_DATA_LOCK:
        if old in HOST_DATA:
            HOST_DATA[new] = HOST_DATA.pop(old)
    with LOCK:
        row = _hosts_repo.get(new, conn=DB)
    return {"name": row[0], "ssh_target": row[1], "tags": row[2]}, None

def update_host(name, ssh_target=None, tags=None):
    """Patch an existing host. Returns (host_dict, error_or_None). The cached
    last-check result is cleared because the old probe no longer applies to the
    new target."""
    from backend.db.repos import hosts as _hosts_repo
    fields, params = [], []
    if ssh_target is not None:
        if _parse_ssh_target(ssh_target) is None:
            return None, "SSH target must look like user@host or user@host:port."
        fields.append("ssh_target=?"); params.append(ssh_target.strip())
    if tags is not None:
        fields.append("tags=?"); params.append((tags or "").strip())
    if not fields:
        return None, "Nothing to update."
    # Clear last-check whenever the target changes.
    if ssh_target is not None:
        fields += ["last_check_at=NULL", "last_check_json=NULL"]
    params.append(name)
    with LOCK:
        rowcount = _hosts_repo.update(','.join(fields), params, conn=DB)
    if rowcount == 0:
        return None, f"No host named '{name}'."
    with LOCK:
        row = _hosts_repo.get(name, conn=DB)
    return {"name": row[0], "ssh_target": row[1], "tags": row[2]}, None

# ── LAN discovery (suggest hosts instead of asking the user to type) ───────────
# Three signals merged: kernel ARP cache (free, sees recently-active hosts),
# TCP-22 sweep across each local /24 (catches anything ARP missed), and reverse
# DNS for friendly names. Container runs with `network_mode: host` so this sees
# the host's network exactly. Results cached 30s to keep UI snappy.

_LAN_CACHE = {"at": 0, "data": None}
_LAN_LOCK  = threading.Lock()
_LAN_TTL   = 30

def _local_subnets():
    """Yield (IPv4Network, iface) pairs for the host's small local subnets.
    Reads /proc/net/route directly so we don't need iproute2 in the image."""
    out = []
    try:
        with open("/proc/net/route") as f:
            next(f, None)  # header
            for line in f:
                parts = line.split()
                if len(parts) < 8:
                    continue
                iface, dest_hex, _, _, _, _, _, mask_hex = parts[:8]
                if dest_hex == "00000000":  # default route
                    continue
                # Skip Docker / VPN bridges — we don't want to ping every
                # container, and we'd just rediscover containers we already
                # know about.
                if iface.startswith(("docker", "br-", "veth", "tun", "tap")):
                    continue
                try:
                    dest = ".".join(str(int(dest_hex[i:i+2], 16)) for i in (6, 4, 2, 0))
                    m_bytes = bytes(int(mask_hex[i:i+2], 16) for i in (6, 4, 2, 0))
                    prefix  = bin(int.from_bytes(m_bytes, "big")).count("1")
                except Exception:
                    continue
                if dest.startswith(("127.", "169.254.")):
                    continue
                if prefix < 22:   # too many hosts (>1k) — skip ping-sweep
                    continue
                try:
                    net = ipaddress.IPv4Network(f"{dest}/{prefix}", strict=False)
                except Exception:
                    continue
                if net.num_addresses > 1024:
                    continue
                out.append((net, iface))
    except Exception as e:
        print("local-subnets read error:", e, flush=True)
    return out

def discover_lan(port=22, timeout=0.4, max_workers=64):
    """Discover LAN hosts likely worth registering. Returns a dict with
    `hosts` (sorted by IP) and `scanned_at`. Each host has: ip, hostname?,
    iface, source (arp|scan), ssh_open (bool)."""
    with _LAN_LOCK:
        now = time.time()
        if _LAN_CACHE["data"] is not None and now - _LAN_CACHE["at"] < _LAN_TTL:
            return _LAN_CACHE["data"]

    candidates = {}

    # 1) ARP cache — free, sees hosts the kernel has talked to recently.
    try:
        with open("/proc/net/arp") as f:
            next(f, None)
            for line in f:
                parts = line.split()
                if len(parts) >= 6:
                    ip, _, _, mac, _, iface = parts[:6]
                    if mac == "00:00:00:00:00:00":
                        continue
                    if iface.startswith(("docker", "br-", "veth", "tun", "tap")):
                        continue
                    candidates.setdefault(ip, {"ip": ip, "source": "arp", "iface": iface})
    except Exception:
        pass

    # 2) Sweep small local subnets we haven't already covered via ARP.
    own_ips = set()
    try:
        for ai in socket.getaddrinfo(socket.gethostname(), None):
            if ai[0] == socket.AF_INET:
                own_ips.add(ai[4][0])
    except Exception:
        pass
    for net, iface in _local_subnets():
        for ip_obj in net.hosts():
            ip = str(ip_obj)
            if ip in candidates or ip in own_ips:
                continue
            candidates[ip] = {"ip": ip, "source": "scan", "iface": iface}

    # 3) Parallel TCP-22 probe + reverse DNS.
    def probe(item):
        ip = item["ip"]
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                item["ssh_open"] = True
        except Exception:
            item["ssh_open"] = False
        try:
            item["hostname"] = socket.gethostbyaddr(ip)[0]
        except Exception:
            pass
        return item

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(probe, list(candidates.values())))

    # Keep ARP-seen hosts (interesting context) and anything reachable on :22.
    keep = [r for r in results if r.get("ssh_open") or r.get("source") == "arp"]
    try:
        keep.sort(key=lambda r: tuple(int(o) for o in r["ip"].split(".")))
    except Exception:
        keep.sort(key=lambda r: r["ip"])

    out = {"hosts": keep, "scanned_at": int(time.time())}
    with _LAN_LOCK:
        _LAN_CACHE["data"] = out
        _LAN_CACHE["at"]   = time.time()
    return out

def _record_check(name, result):
    from backend.db.repos import hosts as _hosts_repo
    with LOCK:
        _hosts_repo.update_check(name, int(time.time()), json.dumps(result), conn=DB)

# ── Per-host metric polling (Issue #35 slice 2) ───────────────────────────────
# Every INTERVAL seconds the background poller pipes probe.py into
# `ssh user@host python3 -` and parses the JSON it prints back. The script is
# loaded once at boot from disk; the remote needs no install. Results are
# cached in memory keyed by host name; the UI's All-hosts table reads from
# this cache.

_PROBE_PATH = os.path.join(os.path.dirname(__file__), "probe.py")
try:
    with open(_PROBE_PATH, "rb") as _f:
        _PROBE_SCRIPT = _f.read()
except Exception as e:
    print("probe.py missing:", e, flush=True)
    _PROBE_SCRIPT = b""

# Windows hosts can't run probe.py (no /proc, no python3 by default). We pipe a
# PowerShell probe instead — always present on Windows, nothing to install — that
# emits the exact same `host` JSON contract. Loaded once at boot like probe.py.
_PROBE_PS_PATH = os.path.join(os.path.dirname(__file__), "probe.ps1")
try:
    with open(_PROBE_PS_PATH, "rb") as _f:
        _PROBE_PS_SCRIPT = _f.read()
except Exception as e:
    print("probe.ps1 missing:", e, flush=True)
    _PROBE_PS_SCRIPT = b""

# How we run a stdin-piped PowerShell script on a Windows remote, regardless of
# whether its OpenSSH default shell is cmd.exe or PowerShell. `-Command -` reads
# the whole script from stdin (an EncodedCommand would blow past cmd's 8 KB argv
# limit for a probe this size), so we feed it the same way probe.py is fed.
_WIN_PS_CMD = "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command -"

HOST_DATA = {}          # name -> {"data": {...}, "at": int, "error": str?}
HOST_DATA_LOCK = threading.Lock()
HOST_POLL_TIMEOUT = 15           # default per-host probe budget (seconds)
# Adaptive per-host poll timeout (issue #99): a host that keeps timing out at its
# current budget is recalibrated once with a high ceiling to learn how long its
# probe actually needs, then that learned budget is persisted and reused. Fast
# hosts never leave the 15s default.
POLL_TIMEOUT_TRIPWIRE   = 2      # consecutive timeouts before we recalibrate
POLL_CALIBRATION_CEILING = 120   # safety ceiling for the learning probe (seconds)
POLL_TIMEOUT_MAX        = 90      # cap on any learned budget
POLL_TIMEOUT_HEADROOM   = 1.5     # learned = measured * headroom (+ a few seconds)

# Phase 3.1: moved to backend/probes/ — re-exported for backward compat
from backend.probes import probe_host_metrics
def _local_now_snapshot():
    """Build a 'host' block for the hub itself, matching the probe shape so
    the All-hosts table and the per-host Host tab can render local and remote
    with the exact same code. We pull from LATEST / HEALTH which the existing
    collector already keeps fresh."""
    H = (LATEST or {}).get("host") or {}
    out = {
        "cpu":       H.get("cpu"),
        "cores":     H.get("cores"),
        "ram_used":  H.get("ram_used"),
        "ram_total": H.get("ram_total"),
        "ram_kernel": H.get("ram_kernel"),
        "load1":     H.get("load1"),
        "uptime":    H.get("uptime"),
        "ctemp":     H.get("ctemp"),
        "disks":     H.get("disks") or [],
        "hostname":  socket.gethostname(),
    }
    # System / Network / Security inventory — pass through so the per-host tabs
    # and the fleet row for `local` render from the same sub-objects as remotes.
    for k in ("os", "hw", "net", "sec"):
        if H.get(k):
            out[k] = H[k]
    # RAPL power — top-level in LATEST (not inside host), so pull explicitly.
    # Mirrors the shape probe.py emits for remotes (cpu_power/dram_power in host).
    for k in ("cpu_power", "dram_power"):
        v = (LATEST or {}).get(k)
        if v is not None:
            out[k] = v
    # GPU summary — the existing collector already keeps these on LATEST's top
    # level. Re-use them so the All-hosts row for `local` matches the shape
    # probe.py emits for remotes.
    # Gate on gpu_avail (not mem_total) so the GPU block isn't emitted during
    # warm-up — LATEST['mem_total'] defaults to 24576, which otherwise made the
    # fleet row show a phantom "0% / 24 GB VRAM" before the first sample on a
    # GPU-less hub. Mirrors api_health, which already keys off gpu_avail.
    if (LATEST or {}).get("gpu_avail"):
        out["gpu"] = {
            "mem_used":  (LATEST or {}).get("mem_used", 0),
            "mem_total": (LATEST or {}).get("mem_total") or 0,
            "util":      (LATEST or {}).get("util", 0),
            "temp":      (LATEST or {}).get("temp", 0),
        }
    return out

# ── Adaptive per-host poll timeout (issue #99) ────────────────────────────────
def _host_poll_state(name):
    """(timeout, fails) for a host — timeout falls back to the global default."""
    from backend.db.repos import hosts as _hosts_repo
    try:
        with LOCK:
            row = _hosts_repo.get_poll_state(name, conn=DB)
    except Exception:
        return HOST_POLL_TIMEOUT, 0
    t = row[0] if row and row[0] else None
    return (int(t) if t else HOST_POLL_TIMEOUT), (int(row[1]) if row and row[1] else 0)

def _host_poll_save(name, timeout=None, fails=None, calibrated=False):
    from backend.db.repos import hosts as _hosts_repo
    sets, params = [], []
    if timeout is not None:   sets.append("poll_timeout=?");       params.append(int(timeout))
    if fails is not None:      sets.append("poll_fails=?");          params.append(int(fails))
    if calibrated:             sets.append("poll_calibrated_at=?");  params.append(int(time.time()))
    if not sets:
        return
    params.append(name)
    try:
        with LOCK:
            _hosts_repo.save_poll_state(','.join(sets), params, conn=DB)
    except Exception as e:
        print("host poll-state save error:", e, flush=True)

def _learned_timeout(measured_ms):
    """Turn a measured probe time into a budget: headroom over the real cost,
    floored at the default and capped at the ceiling."""
    secs = (measured_ms or 0) / 1000.0
    return max(HOST_POLL_TIMEOUT, min(POLL_TIMEOUT_MAX, int(secs * POLL_TIMEOUT_HEADROOM) + 3))

def _poll_and_adapt(name, u, host, port, fam):
    """Probe one host at its learned budget. On a *timeout* (not other errors),
    count it; once a host trips the wire, recalibrate once with a high ceiling to
    learn the budget it actually needs, persist it, and use that probe's data.
    Returns (data, error). Pure of the cache so it's unit-testable."""
    timeout, fails = _host_poll_state(name)
    data, err, _ms, timed_out = probe_host_metrics(u, host, port, fam, timeout=timeout)
    if data:
        if fails:
            _host_poll_save(name, fails=0)
        return data, None
    if not timed_out:
        return None, err                      # genuine error — don't touch tuning
    fails += 1
    _host_poll_save(name, fails=fails)
    if fails < POLL_TIMEOUT_TRIPWIRE:
        return None, err
    # Recalibrate: one probe with the safety ceiling to learn the real cost.
    cdata, cerr, cms, _ct = probe_host_metrics(u, host, port, fam, timeout=POLL_CALIBRATION_CEILING)
    if cdata:
        learned = _learned_timeout(cms)
        _host_poll_save(name, timeout=learned, fails=0, calibrated=True)
        print(f"host '{name}': calibrated poll timeout to {learned}s "
              f"(probe took {round((cms or 0)/1000,1)}s)", flush=True)
        return cdata, None
    return None, cerr or err                  # too slow even at the ceiling / down

def _host_online_window(name):
    """How stale a host's last good sample may be before the UI calls it offline.
    Generous and timeout-aware: a host that legitimately needs a long probe budget
    (or that misses one slow cycle) must not flap to 'offline' between refreshes.
    The flip only happens after a *sustained* gap — at least a few missed samples,
    or twice the host's own learned probe budget, whichever is larger."""
    timeout, _ = _host_poll_state(name)
    return max(INTERVAL * 6, (timeout + INTERVAL) * 2)

def _host_is_online(entry):
    """Single source of truth for the fleet 'online' flag (used by both /api/fleet
    and /api/host_data). True while we hold data whose last *successful* poll is
    inside the host's staleness window. Hysteresis lives here, not in the caller,
    so the table and the per-host view never disagree."""
    if not entry or "data" not in entry or not entry.get("at"):
        return False
    window = entry.get("window") or (INTERVAL * 6)
    return (int(time.time()) - int(entry["at"])) < window

def _poll_one_host(h):
    """Probe one eligible host and fold the result into HOST_DATA. Contains its own
    exceptions so a single bad host can never sink the whole cycle. A *failed* poll
    keeps the last good data and timestamp untouched — only the error is recorded —
    so the online flag rides on last-success, not last-attempt."""
    try:
        check = h.get("last_check") or {}
        if (check.get("summary") or {}).get("overall") not in ("ok", "warn"):
            return
        parsed = _parse_ssh_target(h["ssh_target"])
        if not parsed:
            return
        u, host, port = parsed
        fam = ((check.get("os") or {}).get("family")) or "linux"
        data, err = _poll_and_adapt(h["name"], u, host, port, fam)
        window = _host_online_window(h["name"])
        with HOST_DATA_LOCK:
            entry = HOST_DATA.get(h["name"], {})
            entry["window"] = window
            if data:
                entry["data"]   = data
                entry["at"]     = int(time.time())
                entry["error"]  = None
                entry["fails"]  = 0
            else:
                entry["error"]    = err or "unknown error"
                entry["error_at"] = int(time.time())
                entry["fails"]    = int(entry.get("fails", 0)) + 1
            HOST_DATA[h["name"]] = entry
        # Push the fleet the moment a poll lands. A remote's cadence is still the
        # poller's — its data comes over SSH and making that faster would cost
        # real network — but the browser no longer waits out its own poll on top
        # of it, which was up to 15 s of pure queueing latency per host.
        bump_fleet()
        if data:
            _record_host_sample(h["name"], data.get("host") or {})
    except Exception as e:
        print(f"host poll error ({h.get('name')}):", e, flush=True)

def fleet_gpu_cards():
    """[(host, cards, online)] for every host that reports GPUs right now.

    One accessor so the alert scan doesn't need to know that the hub keeps its
    cards in LATEST while remotes keep theirs in HOST_DATA. Offline hosts are
    included with online=False so the caller can decide — a stale reading must
    not be alerted on as if it were current."""
    out = [("local", list(LATEST.get("gpus") or []), True)]
    with HOST_DATA_LOCK:
        items = list(HOST_DATA.items())
    for name, entry in items:
        if "data" not in entry:
            continue
        cards = ((entry["data"].get("host") or {}).get("gpus")) or []
        if cards:
            out.append((name, list(cards), _host_is_online(entry)))
    return out

def _record_host_sample(name, hostd):
    """Persist one poll's vitals into host_samples(+_1h) — the storage that the
    per-host Costs view (and future per-host history charts) integrate over.
    Absent sensors stay NULL (no GPU ≠ zero watts). Runs on the poller's worker
    threads, so the write takes LOCK on its own — never nest under
    HOST_DATA_LOCK. A storage failure must not break the poll itself.

    Also stores the host's per-card GPU history and per-service VRAM into the
    same tables the hub uses for itself (keyed by host name), which is what lets
    the GPU cockpit render a remote and the hub through one code path instead of
    a charts-here / snapshot-there fork."""
    from backend.db.repos import host_samples as _hs_repo
    from backend.db.repos import gpu_samples as _gs_repo
    gpu = hostd.get("gpu") or {}
    cards = hostd.get("gpus") or []
    ts = int(time.time())
    try:
        with LOCK:
            _hs_repo.record(
                DB, ts, name,
                cpu=hostd.get("cpu"), ram_used=hostd.get("ram_used"),
                ram_total=hostd.get("ram_total"), load1=hostd.get("load1"),
                ctemp=hostd.get("ctemp"),
                gpu_util=gpu.get("util"), gpu_mem_used=gpu.get("mem_used"),
                gpu_mem_total=gpu.get("mem_total"), gpu_power=gpu.get("power"),
                gpu_temp=gpu.get("temp"),
                cpu_power=hostd.get("cpu_power"), dram_power=hostd.get("dram_power"))
            if cards:
                _gs_repo.record(DB, ts, name, cards, interval=INTERVAL)
            rows = _host_vram_rows(ts, name, hostd)
            if rows:
                DB.executemany("INSERT INTO proc(ts,service,mem,host) VALUES(?,?,?,?)", rows)
            DB.commit()
    except Exception as e:
        print(f"host sample store error ({name}):", e, flush=True)

def _host_vram_rows(ts, name, hostd):
    """One (ts, service, mem, host) row per service holding VRAM on `name`.

    Prefers the container attribution the probe already computes (it maps each
    compute pid through /proc/<pid>/cgroup, so `ollama` reads as the container
    name a human recognises) and falls back to the bare process name for VRAM
    held outside a container. Returns [] when the host reports no GPU processes
    — better an honest gap in the chart than a row of zeros."""
    procs = hostd.get("gpu_procs") or []
    if not procs:
        return []
    by_svc, claimed = {}, 0
    for c in ((hostd.get("docker") or {}).get("containers") or []):
        mb = c.get("vram_mb") or 0
        if mb > 0 and c.get("name"):
            by_svc[c["name"]] = by_svc.get(c["name"], 0) + mb
            claimed += mb
    # Whatever the containers didn't account for is running on the host itself;
    # attribute it by process name so it is visible rather than silently lost.
    total = sum((p.get("mem") or 0) for p in procs)
    if total - claimed > 0:
        for p in sorted(procs, key=lambda x: -(x.get("mem") or 0)):
            if claimed >= total:
                break
            take = min(p.get("mem") or 0, total - claimed)
            if take > 0:
                nm = "host:" + str(p.get("name") or "?")
                by_svc[nm] = by_svc.get(nm, 0) + take
                claimed += take
    return [(ts, svc, mem, name) for svc, mem in by_svc.items()]

# Phase 3.2: moved to backend/collectors/ — re-exported for backward compat
from backend.collectors import host_poller

_SSH_BASE_ARGS = [
    "-i", SSH_KEY,
    "-o", "BatchMode=yes",
    "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", f"UserKnownHostsFile={SSH_KNOWN_HOSTS}",
    "-o", "PasswordAuthentication=no",
    "-o", "PubkeyAuthentication=yes",
]

def _ssh(user, host, port, cmd, timeout=8):
    """Run `cmd` on the remote via ssh. Pass the whole command as a single
    argument so ssh hands it to the remote login shell intact — earlier
    versions wrapped with `sh -c` which got mangled when ssh joined argv with
    spaces (`sh -c echo ok` runs echo with $0=ok and produces no output)."""
    t0 = time.time()
    try:
        p = subprocess.run([
            "ssh", *_SSH_BASE_ARGS, "-p", str(port), "--", f"{user}@{host}", cmd,
        ], capture_output=True, timeout=timeout)
        ms = int((time.time() - t0) * 1000)
        return (p.returncode,
                p.stdout.decode("utf-8", "replace").strip(),
                p.stderr.decode("utf-8", "replace").strip(),
                ms)
    except subprocess.TimeoutExpired:
        ms = int((time.time() - t0) * 1000)
        return 124, "", f"ssh timed out after {timeout}s", ms
    except FileNotFoundError:
        return 127, "", "ssh client not found in container (install openssh-client)", 0

def _ssh_with_stdin(user, host, port, cmd, stdin_bytes, timeout=60):
    """Like _ssh but feeds bytes to the remote command's stdin. Used to pipe a
    sudo password into `sudo -S` without ever putting it in argv on either end."""
    t0 = time.time()
    try:
        p = subprocess.run([
            "ssh", *_SSH_BASE_ARGS, "-p", str(port), "--", f"{user}@{host}", cmd,
        ], input=stdin_bytes, capture_output=True, timeout=timeout)
        ms = int((time.time() - t0) * 1000)
        return (p.returncode,
                p.stdout.decode("utf-8", "replace"),
                p.stderr.decode("utf-8", "replace"),
                ms)
    except subprocess.TimeoutExpired:
        ms = int((time.time() - t0) * 1000)
        return 124, "", f"ssh timed out after {timeout}s", ms
    except FileNotFoundError:
        return 127, "", "ssh client not found in container (install openssh-client)", 0

# ── OS detection + OS-aware remedies (so the "paste this on the remote" hints
#    match what actually works on the remote's distro). Runs as part of
#    probe_host(), cached on the host row alongside the capability checks. ─────

_OS_DETECT_SCRIPT = (
    'echo "UNAME=$(uname -s 2>/dev/null)"; '
    'echo "ARCH=$(uname -m 2>/dev/null)"; '
    'if [ -r /etc/os-release ]; then . /etc/os-release; '
    '  echo "ID=$ID"; echo "ID_LIKE=${ID_LIKE:-}"; '
    '  echo "VERSION_ID=${VERSION_ID:-}"; echo "PRETTY_NAME=${PRETTY_NAME:-}"; '
    'fi; '
    'command -v systemctl >/dev/null 2>&1 && echo "INIT=systemd"; '
    'command -v rc-service >/dev/null 2>&1 && echo "INIT=openrc"; '
    ':'   # always exit 0 — the last `&&` would otherwise return non-zero on
          # hosts without rc-service, making us drop the perfectly-good
          # discovery output above.
)

# Windows can't run the POSIX snippet above: a Windows OpenSSH host runs it under
# cmd.exe (which never expands `$(…)`) or PowerShell (where `uname` doesn't
# exist), so neither yields a usable UNAME/ID. When that happens we re-probe with
# this tiny PowerShell script piped over stdin — it answers on any Windows shell —
# and emit the same KEY=VALUE lines the parser already understands.
_WIN_DETECT_PS = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$o=Get-CimInstance Win32_OperatingSystem;"
    "'UNAME=Windows';'ID=windows';"
    "\"PRETTY_NAME=$($o.Caption)\";"
    "\"VERSION_ID=$($o.Version)\";"
    "\"ARCH=$($o.OSArchitecture)\";"
    "'INIT=windows-services'"
).encode("utf-8")

def _detect_os(user, host, port):
    """Run a tiny discovery script via SSH. Returns a normalized dict. We
    ignore rc here — we only care about whatever lines did land on stdout."""
    _, out, _, _ = _ssh(user, host, port, _OS_DETECT_SCRIPT, timeout=10)
    info = {}
    for line in (out or "").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            info[k.strip()] = v.strip().strip('"').strip("'")
    uname = (info.get("UNAME") or "").lower()
    # Windows fallback: no usable POSIX marker came back → ask PowerShell directly.
    if uname not in ("linux", "darwin") and not info.get("ID"):
        _, wout, _, _ = _ssh_with_stdin(user, host, port, _WIN_PS_CMD,
                                        _WIN_DETECT_PS, timeout=10)
        winfo = {}
        for line in (wout or "").splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                winfo[k.strip()] = v.strip().strip('"').strip("'")
        if (winfo.get("UNAME") or "").lower() == "windows":
            info, uname = winfo, "windows"
    # Family normalization for branching remedies (shared with the local inventory).
    family = _os_family(info.get("ID"), info.get("ID_LIKE"), uname)
    # _os_family() collapses unknown to "linux"; preserve a non-linux uname (e.g.
    # an exotic kernel) the way the old logic did.
    if family == "linux" and uname not in ("linux", ""):
        family = uname
    info["family"] = family
    # Pretty short label for the UI badge. The "· windows-services" init suffix
    # is noise on Windows, so the label there is just the product name.
    pretty = info.get("PRETTY_NAME") or info.get("ID") or info.get("UNAME") or "unknown"
    init   = info.get("INIT")
    info["label"] = pretty if family == "windows" else (f"{pretty}" + (f" · {init}" if init else ""))
    return info

def _remedy_docker_windows_daemon():
    """Docker CLI is present on a Windows host but the daemon didn't answer
    cleanly — usually Docker Desktop's engine needs a restart (e.g. after an
    auto-update bumps the CLI's API version ahead of the running engine)."""
    return {"where": "on the remote (Windows)",
            "cmd": "# Docker Desktop's engine isn't answering API calls cleanly.\n"
                   "# Restart it from the system tray (Docker Desktop icon > Restart),\n"
                   "# or from an elevated PowerShell:\n"
                   "Restart-Service com.docker.service -Force\n"
                   "# Then confirm `docker ps` works locally before you re-test here."}

def _remedy_docker_group(user, os_info):
    """Per-OS instructions for joining the docker group / fixing socket perms."""
    fam = (os_info.get("family") or "linux")
    if fam == "macos":
        return {"where": "on the remote (macOS — limited)",
                "cmd": "# macOS doesn't expose the Docker socket the same way as Linux.\n"
                       "# Docker Desktop runs inside a VM; SSH-driven container monitoring\n"
                       "# isn't supported yet. The other panels (host CPU/RAM via /proc) also\n"
                       "# won't work on macOS — full macOS support is a future enhancement."}
    if fam == "alpine":
        return {"where": "on the remote (Alpine)",
                "cmd": f"sudo addgroup {user} docker\n"
                       f"# Log out and back in (or just reconnect over SSH) to pick up the new group."}
    if fam in ("debian", "rhel", "suse", "arch", "linux"):
        return {"where": "on the remote",
                "cmd": f"sudo usermod -aG docker {user}\n"
                       f"# Then log out and back in — the next SSH session inherits the new group."}
    return {"where": "on the remote",
            "cmd": f"# Could not detect the OS family; this is the generic Linux fix:\n"
                   f"sudo usermod -aG docker {user}"}

def _remedy_pubkey(user):
    # This fires at the "SSH reachable" step — BEFORE we can detect the remote OS
    # (auth has to succeed first). So we can't pick the right command for the user;
    # instead we offer per-OS variants and let them choose + copy. `cmd` stays the
    # Linux form so the "Run on remote" button (only shown once SSH works) keeps
    # working on a reachable Linux host.
    key = get_hub_pubkey()
    return {"where": "on the remote — pick your OS, then Copy",
            "cmd": f"mkdir -p ~/.ssh && chmod 700 ~/.ssh\n"
                   f"echo '{key}' >> ~/.ssh/authorized_keys\n"
                   f"chmod 600 ~/.ssh/authorized_keys",
            "variants": [
                {"os": "linux", "label": "Linux / macOS",
                 "cmd": f"mkdir -p ~/.ssh && chmod 700 ~/.ssh\n"
                        f"echo '{key}' >> ~/.ssh/authorized_keys\n"
                        f"chmod 600 ~/.ssh/authorized_keys"},
                {"os": "windows", "label": "Windows (standard user)",
                 "cmd": f"# PowerShell, for a non-admin account:\n"
                        f"New-Item -ItemType Directory -Force $env:USERPROFILE\\.ssh | Out-Null\n"
                        f"Add-Content $env:USERPROFILE\\.ssh\\authorized_keys '{key}'"},
                {"os": "windows-admin", "label": "Windows (admin user)",
                 "cmd": f"# PowerShell (elevated). Admin accounts use a shared keyfile with a\n"
                        f"# strict ACL, or OpenSSH ignores it:\n"
                        f"Add-Content $env:ProgramData\\ssh\\administrators_authorized_keys '{key}'\n"
                        f"icacls $env:ProgramData\\ssh\\administrators_authorized_keys /inheritance:r /grant Administrators:F /grant SYSTEM:F"},
            ]}

def _remedy_sshd_check():
    """Multi-OS 'is sshd up / port open' remedy. Shown on the connect-failure
    paths, which happen BEFORE OS detection — so we offer Linux and Windows and
    let the user pick + copy the one that matches their remote."""
    return {"where": "on the remote — pick your OS",
            "cmd": "# sshd may not be running, or the port is firewalled.\n"
                   "sudo systemctl status sshd\n"
                   "sudo ufw status                 # ufw\n"
                   "sudo firewall-cmd --list-ports  # firewalld",
            "variants": [
                {"os": "linux", "label": "Linux",
                 "cmd": "# Is sshd running, and is port 22 open?\n"
                        "sudo systemctl status sshd\n"
                        "sudo ufw status                 # ufw\n"
                        "sudo firewall-cmd --list-ports  # firewalld"},
                {"os": "windows", "label": "Windows",
                 "cmd": "# PowerShell (elevated): is OpenSSH Server up and port 22 allowed?\n"
                        "Get-Service sshd\n"
                        "Get-NetFirewallRule -DisplayName '*OpenSSH*' | Format-Table Name,Enabled,Profile\n"
                        "# Install + enable it if missing:\n"
                        "Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0\n"
                        "Set-Service sshd -StartupType Automatic; Start-Service sshd"},
            ]}

def _remedy_sshd_down(os_info):
    fam = (os_info or {}).get("family") or ""
    if fam == "alpine":
        return {"where": "on the remote",
                "cmd": "# Check that sshd is running:\nsudo rc-service sshd status"}
    # OS unknown (the usual case here — this fires on a connect timeout, before
    # detection) → offer both Linux and Windows.
    return _remedy_sshd_check()

def _clean_ssh_err(err, out, rc):
    """Build a human summary of an SSH failure. Skips `Warning:` chatter (host
    key added, deprecated alg, etc.) — those aren't the failure cause; the real
    error is usually a line below them."""
    lines = []
    for l in (err or "").splitlines():
        s = l.strip()
        if not s: continue
        if s.lower().startswith("warning:"): continue
        if "Pseudo-terminal will not be allocated" in s: continue
        lines.append(s)
    if lines:
        return " · ".join(lines)[:300]
    if rc == 124:
        return "ssh timed out"
    if (out or "").strip():
        return (out or "").splitlines()[0][:240]
    return "no response (rc=%d)" % rc

def _tcp_probe(host, port, timeout=2.0):
    """Quick TCP-connect probe. Lets us distinguish 'host unreachable / port
    closed' (no point retrying SSH) from 'reached, but auth failed'."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except Exception as e:
        return False, str(e)

def _summarize(checks):
    by = {"ok": 0, "warn": 0, "fail": 0, "info": 0}
    for c in checks:
        by[c["status"]] = by.get(c["status"], 0) + 1
    if by["fail"]:   overall = "fail"
    elif by["warn"]: overall = "warn"
    else:            overall = "ok"
    return {"overall": overall, **by}

# Phase 3.1: moved to backend/probes/ — re-exported for backward compat
from backend.probes import probe_host
def run_on_host(name, cmd, sudo_password=None):
    """Execute `cmd` on a registered host. If `sudo_password` is provided, the
    whole command is wrapped in `sudo -S -p '' bash -c <cmd>` and the password
    is piped via stdin to sudo on the remote — it never appears in argv on
    either the local or remote side, and we never log it. Returns:
    {ok, exit_code, stdout, stderr, ms}."""
    from backend.db.repos import hosts as _hosts_repo
    with LOCK:
        ssh_target = _hosts_repo.get_ssh_target(name, conn=DB)
    if not ssh_target:
        return None
    parsed = _parse_ssh_target(ssh_target)
    if not parsed:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "Bad SSH target.", "ms": 0}
    user, host, port = parsed
    if sudo_password:
        # Wrap once with sudo -S so even compound commands (`a && b`) run as
        # root in a single authentication. Nested `sudo` inside is harmless —
        # root running sudo doesn't reprompt.
        wrapped = f"sudo -S -p '' bash -c {shlex.quote(cmd)}"
        rc, out, err, ms = _ssh_with_stdin(user, host, port, wrapped,
                                           (sudo_password + "\n").encode("utf-8"),
                                           timeout=90)
    else:
        rc, out, err, ms = _ssh(user, host, port, cmd, timeout=90)
    # Strip the leading "Password:" prompt or similar that some sudos emit when
    # `-p ''` isn't fully honored. Be conservative.
    if err:
        err = "\n".join(ln for ln in err.splitlines()
                        if "incorrect password" not in ln.lower() or True)
    return {"ok": rc == 0, "exit_code": rc, "stdout": out, "stderr": err, "ms": ms}

def _ps_single_quote(s):
    """Escape a value for embedding in a single-quoted PowerShell string —
    doubling an embedded `'` is the whole rule (single-quoted PS strings don't
    interpret $ or backticks), same idea as shlex.quote for POSIX shells."""
    return "'" + (s or "").replace("'", "''") + "'"

def run_on_host_windows(name, ps_script):
    """Execute a PowerShell script on a registered Windows host, piped over SSH
    stdin exactly like the probe.ps1 fetch (_WIN_PS_CMD) — just a different
    script. There's no sudo/elevation concept here: whether the command
    succeeds depends on which authorized_keys file got the hub's pubkey during
    onboarding (a plain user's vs. administrators_authorized_keys) — that's a
    Windows OpenSSH behaviour we don't control, so an elevation failure just
    surfaces as the remote's own 'Access is denied' in stderr rather than
    something we predict up front. Returns {ok, exit_code, stdout, stderr, ms}."""
    with LOCK:
        row = DB.execute("SELECT ssh_target FROM hosts WHERE name=?", (name,)).fetchone()
    if not row:
        return None
    parsed = _parse_ssh_target(row[0])
    if not parsed:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "Bad SSH target.", "ms": 0}
    user, host, port = parsed
    rc, out, err, ms = _ssh_with_stdin(user, host, port, _WIN_PS_CMD,
                                       ps_script.encode("utf-8"), timeout=30)
    return {"ok": rc == 0, "exit_code": rc, "stdout": out, "stderr": err, "ms": ms}

# Generate the hub keypair eagerly so /api/hub/pubkey is instant on first hit.
_ensure_ssh_keypair()

SETTING_DEFAULTS = {
    "retention_days":     str(_RETENTION_DAYS_DEFAULT),  # history retention in days
    "alerts_enabled":     "0",       # "0" / "1"
    "discord_webhook_url": "",
    "ntfy_topic":          "",
    "ntfy_server":         "https://ntfy.sh",
    "telegram_token":      "",
    "telegram_chat_id":    "",
    "email_host":          "",
    "email_port":          "587",
    "email_use_tls":       "1",
    "email_username":      "",
    "email_password":      "",
    "email_from":          "",
    "email_to":            "",
    "slack_webhook_url":   "",
    "webhook_url":         "",
    "alert_min_level":     "warning",  # "warning" or "critical"
    "disk_alert_pct":      "90",       # disk usage % that trips an alert
    # ── GPU thermal / throttle alerting (the GPU cockpit) ─────────────────────
    # Every threshold is paired with a SUSTAIN window, because the whole
    # difference between a useful GPU alert and a noisy one is duration: a card
    # touching 85 °C for two seconds mid-batch is not an incident, and a tool
    # that pages for it gets muted within a week.
    "gpu_temp_alert_c":      "84",     # per-card temperature that trips an alert
    "gpu_temp_sustain_s":    "180",    # ...only after this many seconds above it
    "gpu_throttle_sustain_s": "120",   # thermal throttling sustained this long
    "gpu_vram_alert_pct":    "95",     # per-card VRAM %
    "gpu_vram_sustain_s":    "300",
    "gpu_fanstall_alerts":   "1",      # fan reading 0% on a warm card that HAS a fan
    "gpu_missing_alerts":    "1",      # a card that was reporting stops reporting
    "gpu_idle_watts":        "",       # blank = off; W drawn while idle before warning
    "gpu_idle_sustain_s":    "1800",
    # Per-host temperature overrides as JSON {"host": celsius}. Needed, not
    # optional: a box whose cards run at a deliberately lowered power limit
    # lives in the low-to-mid 80s by design, and a single global threshold
    # would cry wolf there while being too slack for a well-cooled machine.
    "gpu_temp_overrides":    "",
    "kwh_price":           "",         # electricity price per kWh (day/peak in dual mode); empty hides the cost card (#25)
    "currency":            "$",        # symbol shown next to costs
    # ── dual (day/night) tariff — revamp ──────────────────────────────────────
    "tariff_mode":         "single",   # "single" | "dual"  (default = original flat behaviour)
    "kwh_price_night":     "",         # night price per kWh; blank => silently behaves as single
    "night_start":         "22:00",    # local time-of-day "HH:MM"; window may wrap midnight
    "night_end":           "06:00",    # local time-of-day "HH:MM"
    "country":             "",         # ISO-3166 alpha-2 — UI prefill memo only; backend never resolves it
    "system_idle_watts":   "",         # optional operator baseline (mainboard/fans/PSU/disks); blank => "other" omitted, never a guessed wall figure
    # ── Integrations / experiment-tracking API (push/pull) ────────────────────
    "api_key":             "",         # Bearer/X-API-Key for run ingest; empty => not generated (ingest fail-closed)
    "mlflow_uri":          "",         # MLflow tracking server base (blank = off)
    "mlflow_token":        "",         # optional bearer for a secured MLflow
    # ── Daily brief (#170) — opt-in once-a-day HTML health digest ──────────────
    "brief_enabled":       "0",        # "0" / "1"
    "brief_time":          "08:00",    # local time-of-day "HH:MM" to send
    "brief_channel":       "",         # one of: email|discord|telegram|ntfy|slack|webhook (must be configured)
    "brief_theme":         "dark",     # "dark" | "light" — palette for the HTML brief
    # ── Public status page (#217 follow-up) — Settings-driven toggle so it
    # doesn't require an env-var restart to turn on/off ─────────────────────
    "public_status_enabled": "0",     # "0" / "1"
    # ── Display preferences ────────────────────────────────────────────────
    "reduced_motion":       "0",      # "0" (default, matches prior behaviour) / "1" — disables gauge animations
}
SETTING_SECRETS = {"discord_webhook_url", "telegram_token", "email_password", "slack_webhook_url", "webhook_url", "api_key", "mlflow_token"}   # never round-tripped to the UI in full

def get_settings():
    """Return the full settings dict (defaults + persisted overrides)."""
    from backend.db.repos import settings as _settings_repo
    out = dict(SETTING_DEFAULTS)
    try:
        with LOCK:
            rows = _settings_repo.get_all(conn=DB)
        for k, v in rows:
            if k in SETTING_DEFAULTS:
                out[k] = v
    except Exception as e:
        print("settings read error:", e, flush=True)
    return out

# Settings that hold a URL the server itself will later POST to (the alert
# webhooks). The dashboard API is intentionally unauthenticated on a trusted
# LAN, so validating the scheme/host on save keeps someone who can reach the
# dashboard from pointing these at a non-HTTP scheme or an empty host — a small
# SSRF-surface tightening, not a change to the trusted-LAN model.
_URL_SETTING_KEYS = {"discord_webhook_url", "ntfy_server", "slack_webhook_url", "webhook_url"}

def _validate_url_settings(updates):
    """Return an error string if any URL-valued setting is malformed, else None.
    An empty value is allowed (it clears the setting)."""
    for key in _URL_SETTING_KEYS:
        if key not in updates:
            continue
        val = (updates[key] or "").strip()
        if not val:
            continue
        try:
            u = urllib.parse.urlparse(val)
        except Exception:
            return f"{key} is not a valid URL."
        if u.scheme not in ("http", "https") or not u.netloc:
            return f"{key} must be an http(s) URL with a host."
    return None

def _validate_email_settings(updates):
    """Return an error string for malformed email alert fields, else None."""
    effective = {**get_settings(), **updates}
    host = (effective.get("email_host") or "").strip()
    from_addr = (effective.get("email_from") or "").strip()
    to_addr = (effective.get("email_to") or "").strip()
    port = (effective.get("email_port") or "587").strip()
    user = (effective.get("email_username") or "").strip()
    pwd  = (effective.get("email_password") or "").strip()
    # If nothing is provided, allow it (email alerts stay off).
    if not any((host, from_addr, to_addr, user, pwd)):
        return None
    if not (host and from_addr and to_addr):
        return "Email alerts require host, from, and to addresses."
    try:
        port_num = int(port)
        if port_num <= 0:
            return "Email port must be a positive integer."
    except ValueError:
        return "Email port must be a number."
    for label, addr in (("From address", from_addr), ("To address", to_addr)):
        if "@" not in addr or addr.startswith("@") or addr.endswith("@"):
            return f"{label} must include '@'."
    return None

_BRIEF_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# Channels the daily brief can target (also used by the worker/test route below).
# Defined here, next to the validator that enum-checks it, so the dependency is local.
_BRIEF_CHANNELS = ("email", "discord", "telegram", "ntfy", "slack", "webhook")

def _validate_brief_settings(updates):
    """Reject malformed daily-brief fields. Enum-validating brief_channel/theme here
    means an arbitrary value can never reach the settings store (or the dashboard
    that renders it into an <option>), closing the stored-XSS surface at the source."""
    if "brief_channel" in updates:
        v = (updates["brief_channel"] or "").strip()
        if v and v not in _BRIEF_CHANNELS:
            return "Unknown daily-brief channel."
    if "brief_theme" in updates and (updates["brief_theme"] or "").strip() not in ("dark", "light"):
        return "Daily-brief theme must be 'dark' or 'light'."
    if "brief_time" in updates and not _BRIEF_TIME_RE.match((updates["brief_time"] or "").strip()):
        return "Daily-brief time must be HH:MM (24-hour)."
    return None

def save_settings(updates):
    """Persist any subset of known setting keys. Unknown keys are ignored."""
    safe = [(k, "" if v is None else str(v)) for k, v in updates.items() if k in SETTING_DEFAULTS]
    if not safe:
        return
    with LOCK:
        DB.executemany("INSERT INTO settings(key,value) VALUES(?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", safe)
        DB.commit()

def get_retention_secs():
    """Return the effective retention window in seconds, read live from settings."""
    try:
        days = int(get_settings().get("retention_days") or _RETENTION_DAYS_DEFAULT)
        days = max(1, min(days, 3650))
    except (ValueError, TypeError):
        days = _RETENTION_DAYS_DEFAULT
    return days * 86400

def _validate_retention_settings(updates):
    """Return an error string if retention_days is invalid, else None."""
    if "retention_days" not in updates:
        return None
    val = (updates["retention_days"] or "").strip()
    if not val:
        return None
    try:
        days = int(val)
    except ValueError:
        return "Retention days must be a whole number."
    if days < 1 or days > 3650:
        return "Retention days must be between 1 and 3650."
    return None

def _validate_gpu_alert_settings(updates):
    """Reject malformed GPU alert thresholds before they reach the store.

    The per-host override field takes JSON typed by a human, so it is validated
    at the door rather than being tolerated at read time: a silently-ignored
    malformed override means the user believes a host has a raised threshold
    when it does not, and finds out via a 3am page.
    """
    if "gpu_temp_overrides" in updates:
        raw = (updates["gpu_temp_overrides"] or "").strip()
        if raw:
            try:
                over = json.loads(raw)
            except ValueError:
                return "Per-host GPU temperature overrides must be JSON, e.g. {\"vader\": 88}."
            if not isinstance(over, dict):
                return "Per-host GPU temperature overrides must be a JSON object of host → °C."
            for host, val in over.items():
                if not isinstance(host, str) or not host:
                    return "Each GPU temperature override needs a host name."
                try:
                    c = int(val)
                except (TypeError, ValueError):
                    return f"GPU temperature override for '{host}' must be a whole number of °C."
                if not (50 <= c <= 110):
                    return f"GPU temperature override for '{host}' must be between 50 and 110 °C."
    for key, lo, hi, what in (("gpu_temp_alert_c", 50, 110, "GPU temperature alert"),
                              ("gpu_vram_alert_pct", 50, 100, "GPU VRAM alert"),
                              ("gpu_temp_sustain_s", 0, 3600, "GPU temperature sustain"),
                              ("gpu_throttle_sustain_s", 0, 3600, "GPU throttle sustain"),
                              ("gpu_vram_sustain_s", 0, 3600, "GPU VRAM sustain"),
                              ("gpu_idle_sustain_s", 0, 86400, "GPU idle sustain")):
        if key not in updates:
            continue
        val = (updates[key] or "").strip()
        if not val:
            continue
        try:
            n = int(val)
        except ValueError:
            return f"{what} must be a whole number."
        if not (lo <= n <= hi):
            return f"{what} must be between {lo} and {hi}."
    return None

# ── Uptime checks: HTTP/TCP endpoint monitors ──────────────────────────────
# User-defined HTTP/TCP endpoint monitors, probed from inside the container on a
# DEDICATED worker thread (never the metrics sampler). Each check carries its own
# smart-alerting config (down/recovery/slow) which notify_scan() acts on, reusing
# the same edge-triggered notifier as every other alert. Targets stay private to
# this dashboard — they never reach the public /status payload.
_UPTIME_MIN_INTERVAL = 20      # floor on per-check cadence (don't hammer targets)
_UPTIME_MAX_INTERVAL = 86400
_UPTIME_MAX_TIMEOUT  = 30      # hard cap so a probe can't pin a worker forever
_UPTIME_MAX_REDIRECTS = 3
_UPTIME_RESULT_CAP   = 5000    # per-check ring-buffer of results
_UPTIME_STRIP_CELLS  = 40      # heartbeat cells shown on a card
_UPTIME_FAIL_MAX     = 10      # cap on the confirm-after threshold
_UPTIME_UA = "HomeLab-Monitor uptime check"
_uptime_due = {}               # check_id -> next monotonic due time (scheduler state)
_uptime_down_since = {}        # check_id -> wall-clock ts the current DOWN streak began
from backend.db.repos import edge_state as _edge_state_repo
_uptime_down_since.update({row[0]: row[1] for row in _edge_state_repo.load_down_since(conn=DB)})

def _uptime_row_to_dict(r):
    cols = ("id", "label", "type", "target", "interval_sec", "timeout_sec",
            "expected_status", "alerts_enabled", "fail_threshold", "latency_warn_ms",
            "enabled", "created_at", "public")
    d = dict(zip(cols, r))
    d["enabled"] = bool(d["enabled"])
    d["alerts_enabled"] = bool(d["alerts_enabled"])
    d["public"] = bool(d.get("public"))
    return d

_CRED_RE = re.compile(r"(://)[^/\s:@]+:[^/\s@]+@")

def _redact_target(s):
    """Strip any `scheme://user:pass@` credentials from a string so a check target
    is safe to log/echo in an error. Storing the full target (with creds) is fine —
    like webhook_url — but we never want it in a log line or surfaced error."""
    return _CRED_RE.sub(r"\1***:***@", s or "")

def list_uptime_checks():
    from backend.db.repos import uptime as _uptime_repo
    with LOCK:
        rows = _uptime_repo.list_checks_full(conn=DB)
    return [_uptime_row_to_dict(r) for r in rows]

def _parse_host_port(target):
    """Parse 'host:port' (the tcp check target). Returns (host, port) or (None, None).
    Accepts a leading tcp:// scheme and bracketed IPv6 literals."""
    t = (target or "").strip()
    if "://" in t:
        u = urllib.parse.urlsplit(t)
        host, port = u.hostname, u.port
        if host and port:
            return host, port
        return None, None
    if t.startswith("[") and "]" in t:          # [ipv6]:port
        host, _, rest = t[1:].partition("]")
        if not rest.startswith(":"):
            return None, None
        portstr = rest[1:]
    else:
        host, sep, portstr = t.rpartition(":")
        if not sep:
            return None, None
    host = host.strip()
    if not host:
        return None, None
    try:
        port = int(portstr)
    except (TypeError, ValueError):
        return None, None
    if not (1 <= port <= 65535):
        return None, None
    return host, port

def _validate_uptime_check(body):
    """Return (clean_dict, None) or (None, error_string). Rejects garbage targets,
    bad URL schemes, unparseable host:port, and out-of-range interval/timeout."""
    label = (body.get("label") or "").strip()
    if not label:
        return None, "A label is required."
    if len(label) > 120:
        return None, "Label is too long (max 120 characters)."
    ctype = (body.get("type") or "http").strip().lower()
    if ctype not in ("http", "tcp"):
        return None, "Type must be 'http' or 'tcp'."
    target = (body.get("target") or "").strip()
    if not target:
        return None, "A target is required."
    if len(target) > 2048:
        return None, "Target is too long."
    expected = None
    if ctype == "http":
        u = urllib.parse.urlsplit(target)
        if u.scheme not in ("http", "https"):
            return None, "HTTP checks need an http:// or https:// URL."
        if not u.hostname:
            return None, "HTTP check URL is missing a host."
        es = body.get("expected_status")
        if es not in (None, ""):
            try:
                expected = int(es)
            except (TypeError, ValueError):
                return None, "Expected status must be a number (e.g. 200)."
            if not (100 <= expected <= 599):
                return None, "Expected status must be a valid HTTP status code (100-599)."
    else:  # tcp
        host, port = _parse_host_port(target)
        if host is None:
            return None, "TCP checks need a host:port target (e.g. db.lan:5432)."
    try:
        interval = int(body.get("interval_sec", 60))
    except (TypeError, ValueError):
        return None, "Interval must be a whole number of seconds."
    if interval < _UPTIME_MIN_INTERVAL:
        return None, f"Interval must be at least {_UPTIME_MIN_INTERVAL} seconds."
    if interval > _UPTIME_MAX_INTERVAL:
        return None, f"Interval must be at most {_UPTIME_MAX_INTERVAL} seconds."
    try:
        timeout = int(body.get("timeout_sec", 10))
    except (TypeError, ValueError):
        return None, "Timeout must be a whole number of seconds."
    if timeout < 1:
        return None, "Timeout must be at least 1 second."
    if timeout > _UPTIME_MAX_TIMEOUT:
        return None, f"Timeout must be at most {_UPTIME_MAX_TIMEOUT} seconds."
    # ── smart-alert config ────────────────────────────────────────────────
    try:
        fail_threshold = int(body.get("fail_threshold", 2))
    except (TypeError, ValueError):
        return None, "Confirm-after must be a whole number of checks."
    fail_threshold = max(1, min(_UPTIME_FAIL_MAX, fail_threshold))
    latency_warn = body.get("latency_warn_ms")
    if latency_warn in (None, ""):
        latency_warn = None
    else:
        try:
            latency_warn = int(latency_warn)
        except (TypeError, ValueError):
            return None, "Latency warning threshold must be a number of milliseconds."
        if latency_warn < 1:
            return None, "Latency warning threshold must be at least 1 ms."
        if latency_warn > 600000:
            return None, "Latency warning threshold is too large."
    return {"label": label, "type": ctype, "target": target,
            "interval_sec": interval, "timeout_sec": timeout,
            "expected_status": expected,
            "alerts_enabled": 1 if body.get("alerts_enabled", True) else 0,
            "fail_threshold": fail_threshold, "latency_warn_ms": latency_warn,
            "enabled": 1 if body.get("enabled", True) else 0,
            "public": 1 if body.get("public") else 0}, None

def create_uptime_check(body):
    from backend.db.repos import uptime as _uptime_repo
    clean, err = _validate_uptime_check(body)
    if err:
        return None, err
    cid = uuid.uuid4().hex
    with LOCK:
        _uptime_repo.insert_check_full(
            cid, clean["label"], clean["type"], clean["target"], clean["interval_sec"],
            clean["timeout_sec"], clean["expected_status"], clean["alerts_enabled"],
            clean["fail_threshold"], clean["latency_warn_ms"], clean["enabled"], int(time.time()),
            clean["public"], conn=DB)
    _uptime_due.pop(cid, None)   # probe promptly on next scheduler pass
    return cid, None

def update_uptime_check(cid, body):
    from backend.db.repos import uptime as _uptime_repo
    with LOCK:
        exists = _uptime_repo.check_exists(cid, conn=DB)
    if not exists:
        return False, "not found"
    if not body:
        return False, "empty update"
    # Quick toggles without full revalidation: enabled (pause/resume) and/or public
    # (show on the public status page). Accepts either alone or both together.
    if body and set(body.keys()) <= {"enabled", "public"}:
        sets, vals = [], []
        if "enabled" in body:
            sets.append("enabled=?"); vals.append(1 if body.get("enabled") else 0)
        if "public" in body:
            sets.append("public=?"); vals.append(1 if body.get("public") else 0)
        with LOCK:
            _uptime_repo.update_check_fields(','.join(sets), vals, cid, conn=DB)
        return True, None
    clean, err = _validate_uptime_check(body)
    if err:
        return False, err
    with LOCK:
        _uptime_repo.update_check_full(
            cid, clean["label"], clean["type"], clean["target"], clean["interval_sec"],
            clean["timeout_sec"], clean["expected_status"], clean["alerts_enabled"],
            clean["fail_threshold"], clean["latency_warn_ms"], clean["enabled"], clean["public"],
            conn=DB)
    _uptime_due.pop(cid, None)   # re-probe with new config promptly
    return True, None

def delete_uptime_check(cid):
    from backend.db.repos import uptime as _uptime_repo
    with LOCK:
        rowcount = _uptime_repo.delete_check_and_results(cid, conn=DB)
    _uptime_due.pop(cid, None)
    _uptime_down_since.pop(cid, None)
    from backend.db.repos import edge_state as _edge_state_repo
    import app as _app
    _edge_state_repo.clear_down_since(cid, conn=_app.DB)
    return rowcount > 0

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface redirects as HTTPError so probe_http counts/bounds them itself."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def _http_probe_once(target, timeout, method):
    """One bounded HTTP request, manually following a few redirects (so we never
    auto-follow into a different scheme/host without counting it). Returns the final
    status code or raises. Uses stdlib urllib only."""
    url = target
    last_code = 0
    for _ in range(_UPTIME_MAX_REDIRECTS + 1):
        req = urllib.request.Request(url, method=method, headers={"User-Agent": _UPTIME_UA})
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status
        except urllib.error.HTTPError as he:
            if he.code in (301, 302, 303, 307, 308):
                loc = he.headers.get("Location")
                if not loc:
                    return he.code
                url = urllib.parse.urljoin(url, loc)
                last_code = he.code
                he.close()
                continue
            raise
    return last_code


def _tls_cert_days(host, port, timeout):
    """Return (days_remaining, expires_at_ts) or (None, None)."""
    import ssl, datetime, socket

    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with ctx.wrap_socket(
            socket.create_connection((host, port), timeout=timeout),
            server_hostname=host,
        ) as ssock:
            cert = ssock.getpeercert()
            not_after = cert.get("notAfter", "") if cert else ""
            if not not_after:
                return None, None

            exp = datetime.datetime.strptime(
                not_after,
                "%b %d %H:%M:%S %Y %Z"
            ).replace(tzinfo=datetime.timezone.utc)

            delta = exp - datetime.datetime.now(datetime.timezone.utc)
            return max(0, delta.days), int(exp.timestamp())

    except Exception:
        return None, None


# Phase 3.1: moved to backend/probes/ — re-exported for backward compat
from backend.probes import probe_http, probe_tcp
def run_uptime_check(check):
    ctype = check["type"]
    timeout = min(int(check.get("timeout_sec") or 10), _UPTIME_MAX_TIMEOUT)

    if ctype == "tcp":
        up, latency, code, err = probe_tcp(check["target"], timeout)
    else:
        up, latency, code, err = probe_http(check["target"], timeout, check.get("expected_status"))

    ts = int(time.time())

    cert_days = None
    cert_expires_at = None

    if ctype != "tcp" and check["target"].lower().startswith("https://"):
        import urllib.parse
        parsed = urllib.parse.urlparse(check["target"])
        host = parsed.hostname
        port = parsed.port or 443

        cert_days, cert_expires_at = _tls_cert_days(host, port, min(timeout, 10))

    if _DB_MAINTENANCE:
        return {"ts": ts, "up": up, "latency_ms": latency, "code": code, "err": err}

    try:
        from backend.db.repos import uptime as _uptime_repo
        with LOCK:
            _uptime_repo.insert_result_and_trim(
                check["id"], ts, 1 if up else 0, latency, cert_days, _UPTIME_RESULT_CAP,
                code=code, err=err, cert_expires_at=cert_expires_at,
                conn=DB)
    except Exception as e:
        print("run_uptime_check DB error:", e, flush=True)

    return {"ts": ts, "up": up, "latency_ms": latency, "code": code, "err": err}
def _uptime_state(check_id, now, window=86400, window2=604800):
    """Read-only summary for one check: current state (up/down/unknown), last latency,
    uptime% over `window` (24h) and `window2` (7d), last_checked, last_err, and a
    coarse heartbeat strip. Caller must NOT hold LOCK (this takes it briefly)."""
    from backend.db.repos import uptime as _uptime_repo
    since, since2 = now - window, now - window2
    with LOCK:
        rows = _uptime_repo.results_since_full(check_id, since, conn=DB)
        agg2 = _uptime_repo.results_window_agg(check_id, since2, conn=DB)
        last = _uptime_repo.results_last_one(check_id, conn=DB)
    total = len(rows)
    up_n = sum(1 for r in rows if r[1])
    uptime = round(100.0 * up_n / total, 2) if total else None
    tot2 = (agg2[0] or 0) if agg2 else 0
    uptime7 = round(100.0 * (agg2[1] or 0) / tot2, 2) if tot2 else None
    state = "unknown"
    last_latency = last_checked = last_err = last_code = None
    if last:
        state = "up" if last[1] else "down"
        last_checked, last_latency, last_code, last_err = last[0], last[2], last[3], last[4]
    # Heartbeat strip: most recent up-to-N results, oldest→newest, carrying {up, t}.
    strip = [{"up": bool(r[1]), "t": r[0]} for r in rows[-_UPTIME_STRIP_CELLS:]]
    # Surface the most recent cert expiry data (index 5, 6 from the last row).
    cert_days = last[5] if last and len(last) > 5 else None
    cert_expires_at = last[6] if last and len(last) > 6 else None
    cert_status = None
    if cert_days is not None:
        if cert_days <= 7:
            cert_status = "red"
        elif cert_days <= 21:
            cert_status = "amber"
        else:
            cert_status = "ok"
    return {"state": state, "uptime": uptime, "uptime7": uptime7, "window_total": total,
            "last_latency_ms": last_latency, "last_checked": last_checked,
            "last_code": last_code, "last_err": last_err, "strip": strip,
            "cert_days_remaining": cert_days, "cert_expires_at": cert_expires_at,
            "cert_status": cert_status}

def uptime_overview(window=86400):
    """All checks + their current state. The user-facing private payload."""
    now = int(time.time())
    out = []
    for c in list_uptime_checks():
        state = _uptime_state(c["id"], now, window)
        in_maint = _in_maintenance("uptime", c["id"]) or _in_maintenance("uptime", c.get("label", ""))
        out.append({**c, **state, "in_maintenance": in_maint})
    return {"checks": out, "now": now, "window": window,
            "min_interval": _UPTIME_MIN_INTERVAL, "max_timeout": _UPTIME_MAX_TIMEOUT}

def uptime_summary():
    """Compact rollup for the Overview cockpit: how many ENABLED checks exist and
    how many are up/down, plus the worst current check. None of it sensitive — it's
    counts + a label — so it can ride the always-visible fleet rollup bar."""
    checks = [c for c in uptime_overview().get("checks", []) if c.get("enabled")]
    up = sum(1 for c in checks if c.get("state") == "up")
    down = sum(1 for c in checks if c.get("state") == "down")
    worst = next((c["label"] for c in checks if c.get("state") == "down"), None)
    return {"total": len(checks), "up": up, "down": down,
            "unknown": len(checks) - up - down, "worst_down": worst}

def uptime_insights():
    """Insight-feed rows for the cockpit: currently-DOWN checks (critical) and
    up-but-slow checks (warning). Uptime rides the SAME Insight Feed as disk/RAM/
    containers instead of a bespoke tile — that's how `next`'s overview surfaces
    everything, so nothing is duplicated. Caller must NOT hold LOCK."""
    out = []
    for c in uptime_overview().get("checks", []):
        if not c.get("enabled"):
            continue
        if c.get("state") == "down":
            out.append({"level": "critical", "title": f"{c['label']} is down",
                        "detail": f"{_redact_target(c['target'])} — {_uptime_down_reason(c)}.",
                        "goto": "uptime"})
        elif c.get("state") == "up":
            lw, lat = c.get("latency_warn_ms"), c.get("last_latency_ms")
            if lw and lat is not None and lat > lw:
                out.append({"level": "warning", "title": f"{c['label']} is slow",
                            "detail": f"{round(lat)} ms (warns above {lw} ms).",
                            "goto": "uptime"})
    return out

def _uptime_tick(now=None):
    """One scheduler pass: probe every ENABLED check whose interval is due. Each probe
    is bounded by its own timeout, so a hanging endpoint can't stall the rest — and
    this runs on a DEDICATED thread, never the metrics sampler. Returns the ids probed."""
    now = time.monotonic() if now is None else now
    probed = []
    for c in list_uptime_checks():
        if not c["enabled"]:
            _uptime_due.pop(c["id"], None)
            continue
        if now < _uptime_due.get(c["id"], 0):
            continue
        try:
            run_uptime_check(c)
        except Exception as e:
            print("uptime check error:", e, flush=True)
        _uptime_due[c["id"]] = now + max(_UPTIME_MIN_INTERVAL, int(c["interval_sec"]))
        probed.append(c["id"])
    return probed

# Phase 3.2: moved to backend/collectors/ — re-exported for backward compat
from backend.collectors import uptime_worker

_NOTIFIED = {}            # key -> 1, "armed" alerts pending recovery
from backend.db.repos import edge_state as _edge_state_repo
_NOTIFIED.update({row[0]: 1 for row in _edge_state_repo.load_notified_keys(conn=DB)})
_NOTIFIER_LOCK = threading.Lock()
LEVELS  = {"info": 0, "warning": 1, "critical": 2}
_COLORS = {"info": 0x58A6FF, "warning": 0xD29922, "critical": 0xF85149}
_NTFY_P = {"info": 3, "warning": 4, "critical": 5}
_NTFY_T = {"info": "information_source", "warning": "warning", "critical": "rotating_light"}

# Discord's API sits behind Cloudflare, which rejects the default
# "Python-urllib/x.y" agent with 403 (error code 1010). A real User-Agent is
# also mandated by Discord's API rules, so every outbound POST carries one.
NOTIFY_USER_AGENT = f"homelab-monitor/{VERSION} (+https://github.com/SikamikanikoBG/homelab-monitor)"

# Phase 3.3: moved to backend/notify/ — re-exported for backward compat
from backend.notify import _post_json

# Phase 3.3: moved to backend/notify/ — re-exported for backward compat
from backend.notify import _post_text

def _post_multipart(url, payload_json, filename, file_bytes,
                    file_ctype="text/html; charset=utf-8", timeout=10):
    """POST multipart/form-data: one JSON `payload_json` part + one file part. Stdlib
    only (no `requests`) so the daily brief can attach its HTML to a Discord webhook
    upload. Returns (status, body)."""
    boundary = "----homelab-" + os.urandom(8).hex()
    head = (f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="payload_json"\r\n'
            "Content-Type: application/json\r\n\r\n"
            f"{payload_json}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"\r\n'
            f"Content-Type: {file_ctype}\r\n\r\n").encode("utf-8")
    body = head + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": NOTIFY_USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()

def send_discord(webhook, level, title, detail):
    payload = {"embeds": [{"title": title, "description": detail,
                           "color": _COLORS.get(level, _COLORS["info"]),
                           "footer": {"text": f"HomeLab Monitor · {level}"},
                           "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())}]}
    return _post_json(webhook, payload)

def send_discord_brief(webhook, level, title, description, html, filename):
    """Daily brief → Discord: a clean, severity-coloured embed (no emoji) carrying the
    summary, plus the full HTML brief attached as a downloadable file. Discord shows
    the attachment as a file card the reader opens in a browser (it does not render
    HTML inline). If the multipart upload is rejected (size/permission/network), fall
    back to a plain embed so the brief still arrives — just without the attachment."""
    embed = {"title": (title or "")[:256], "description": (description or "")[:4000],
             "color": _COLORS.get(level, _COLORS["info"]),
             "footer": {"text": f"HomeLab Monitor v{VERSION}"},
             "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())}
    payload = json.dumps({"embeds": [embed],
                          "attachments": [{"id": 0, "filename": filename}]})
    try:
        return _post_multipart(webhook, payload, filename, html.encode("utf-8"))
    except Exception as e:
        print("brief discord attachment failed, sending summary only:", e, flush=True)
        return send_discord(webhook, level, title, description)

def send_ntfy(server, topic, level, title, detail):
    server = (server or "https://ntfy.sh").rstrip("/")
    url    = f"{server}/{urllib.parse.quote(topic, safe='')}"
    hdr    = {"Content-Type": "text/plain; charset=utf-8",
              "Title":    title.encode("ascii", "replace").decode("ascii"),
              "Priority": str(_NTFY_P.get(level, 3)),
              "Tags":     _NTFY_T.get(level, "information_source")}
    return _post_text(url, detail, hdr)

# Phase 3.3: moved to backend/notify/ — re-exported for backward compat
from backend.notify import _tg_escape

def _post_to_telegram(token, chat_id, level, title, body):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    text = (f"*{_tg_escape(title)}*\n\n{_tg_escape(body)}\n\n"
            f"_HomeLab Monitor · {level}_")
    return _post_json(url, {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})

def _smtp_send(msg, host, port, use_tls, username, password):
    """Connect, optionally STARTTLS + authenticate, send, and always close. Shared by
    the alert and daily-brief email paths so any fix (timeouts, TLS, error handling)
    lives in one place. The connection is built inside the try with a None-guarded
    quit() so a constructor failure can never reach an unbound name."""
    port = int(port)
    ctx = None
    try:
        ctx = (smtplib.SMTP_SSL(host, port, timeout=10) if (use_tls and port == 465)
               else smtplib.SMTP(host, port, timeout=10))
        if use_tls and port != 465:
            ctx.starttls()
        if username and password:
            ctx.login(username, password)
        ctx.send_message(msg)
    finally:
        if ctx is not None:
            ctx.quit()

def _send_email(host, port, use_tls, username, password, from_addr, to_addr, level, title, detail):
    """Send alert via SMTP. Raises on error."""
    msg = email.message.EmailMessage()
    msg["Subject"] = title
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(f"{detail}\n\nHomeLab Monitor · {level}")
    _smtp_send(msg, host, port, use_tls, username, password)

def send_slack(webhook, level, title, detail):
    payload = {"text": f"[{level}] {title}\n\n{detail}"}
    return _post_json(webhook, payload)

def send_webhook(url, level, title, detail, host):
    payload = {"level": level, "title": title, "detail": detail, "host": host}
    return _post_json(url, payload)

# Phase 3.3: moved to backend/notify/ — re-exported for backward compat
from backend.notify import _alert_host_label

# Phase 3.3: moved to backend/notify/ — re-exported for backward compat
from backend.notify import dispatch_alert

# Phase 3.3: moved to backend/notify/ — re-exported for backward compat
from backend.notify import _dispatch_to_channels

def _match_kind(key):
    prefix = key.split(":")[0] if ":" in key else key
    if prefix in ("container", "systemd", "uptime"):
        return prefix
    if prefix in ("gpu", "oom"):
        return "gpu"
    if prefix == "disk":
        return "host"
    return "host"

# Phase 3.3: moved to backend/notify/ — re-exported for backward compat
from backend.notify import _alert_name

def _apply_rules(key, level, rules):
    """Return set of channels if rules match, or None for default behavior."""
    if not rules:
        return None
    kind = _match_kind(key)
    name = _alert_name(key)
    channels = set()
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        if rule["match_kind"] != kind:
            continue
        if not fnmatch.fnmatch(name, rule["match_pattern"]):
            continue
        if LEVELS.get(level, 0) < LEVELS.get(rule.get("min_level", "warning"), 0):
            continue
        if rule["channel"] == "all":
            channels.update(["discord", "ntfy", "telegram", "email", "slack", "webhook"])
        else:
            channels.add(rule["channel"])
    return channels if channels else None

def get_notification_rules():
    from backend.db.repos import notify as _notify_repo
    with LOCK:
        rows = _notify_repo.list_rules(conn=DB)
    return [{"id": r[0], "match_kind": r[1], "match_pattern": r[2], "channel": r[3], "min_level": r[4], "enabled": bool(r[5])} for r in rows]

def _emit(s, key, level, title, detail, rules=None):
    """Fire an alert once per edge. Skips below the configured min level."""
    if LEVELS.get(level, 0) < LEVELS.get(s.get("alert_min_level", "warning"), 1):
        return
    # Suppress alerts during active maintenance windows.
    # key format: "kind:name" or "kind:subkind:name"
    _parts = key.split(":", 1)
    if len(_parts) == 2 and _in_maintenance(_parts[0], _parts[1]):
        return
    from backend.db.repos import edge_state as _edge_state_repo
    import app as _app
    with _NOTIFIER_LOCK:
        if _NOTIFIED.get(key):
            return
        _NOTIFIED[key] = 1
    with LOCK:
        try:
            _edge_state_repo.arm_key(key, int(time.time()), conn=_app.DB)
        except Exception as e:
            print(f"edge_state arm_key error: {e}", flush=True)
    channels = _apply_rules(key, level, rules)
    if channels is not None:
        _dispatch_to_channels(s, level, title, detail, channels)
    else:
        for ch, ok, err in dispatch_alert(s, level, title, detail):
            if not ok:
                print(f"notifier {ch} error:", err, flush=True)

def _clear(key):
    from backend.db.repos import edge_state as _edge_state_repo
    import app as _app
    with _NOTIFIER_LOCK:
        was_armed = key in _NOTIFIED
        _NOTIFIED.pop(key, None)
    if was_armed:
        with LOCK:
            try:
                _edge_state_repo.disarm_key(key, conn=_app.DB)
            except Exception as e:
                print(f"edge_state disarm_key error: {e}", flush=True)

def _in_maintenance(kind, name):
    """Return True if kind:name is currently covered by an active maintenance window."""
    from backend.db.repos import notify as _notify_repo
    now = int(time.time())
    with LOCK:
        rows = _notify_repo.get_active_windows(conn=DB)
    for row_kind, pattern, start_ts, end_ts, recurrence in rows:
        # kind must match or be wildcard
        if row_kind != "*" and row_kind != kind:
            continue
        # pattern must match name
        if not fnmatch.fnmatch(name, pattern):
            continue
        if recurrence is None:
            if start_ts <= now <= end_ts:
                return True
        elif recurrence == "daily":
            window_len = end_ts - start_ts
            elapsed = (now - start_ts) % 86400
            if 0 <= elapsed <= window_len:
                return True
        elif recurrence == "weekly":
            window_len = end_ts - start_ts
            elapsed = (now - start_ts) % (86400 * 7)
            if 0 <= elapsed <= window_len:
                return True
    return False

def list_maintenance_windows():
    from backend.db.repos import notify as _notify_repo
    with LOCK:
        rows = _notify_repo.list_windows(conn=DB)
    return [{"id": r[0], "label": r[1], "kind": r[2], "pattern": r[3],
             "start_ts": r[4], "end_ts": r[5], "recurrence": r[6],
             "note": r[7], "created_at": r[8]} for r in rows]

def create_maintenance_window(label, kind, pattern, start_ts, end_ts, recurrence=None, note=None):
    from backend.db.repos import notify as _notify_repo
    wid = uuid.uuid4().hex
    with LOCK:
        _notify_repo.insert_window(
            wid, label, kind or "*", pattern or "*", int(start_ts), int(end_ts),
            recurrence or None, note or None, int(time.time()), conn=DB)
    return wid

def delete_maintenance_window(wid):
    from backend.db.repos import notify as _notify_repo
    with LOCK:
        _notify_repo.delete_window(wid, conn=DB)

# Phase 3.3: moved to backend/notify/ — re-exported for backward compat
from backend.notify import notify_scan

def _uptime_down_reason(st):
    """Short, credential-safe reason for a DOWN check, taken from its last result."""
    if st.get("last_err"):
        return _redact_target(str(st["last_err"]))[:160]
    if st.get("last_code"):
        return f"HTTP {st['last_code']}"
    return "no response"

def _uptime_confirmed_down(cid, threshold):
    """True once the most recent `threshold` results are ALL failures (and we have at
    least that many). This is the anti-flap gate — one dropped packet never pages."""
    from backend.db.repos import uptime as _uptime_repo
    with LOCK:
        rows = _uptime_repo.results_last_n(cid, threshold, conn=DB)
    return len(rows) >= threshold and all(r[0] == 0 for r in rows)

def _uptime_streak_start(cid, now):
    """Wall-clock ts the current DOWN streak began (walk recent results back while
    they're failures), so a recovery message can quote the real downtime."""
    from backend.db.repos import uptime as _uptime_repo
    with LOCK:
        rows = _uptime_repo.results_last_500(cid, conn=DB)
    start = None
    for ts, up in rows:
        if up == 0:
            start = ts
        else:
            break
    return start if start is not None else now

def _fmt_dur(secs):
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    m = secs // 60
    if m < 60:
        return f"{m}m {secs % 60}s"
    h = m // 60
    if h < 24:
        return f"{h}h {m % 60}m"
    d = h // 24
    return f"{d}d {h % 24}h"

def notify_uptime(s):
    """Per-check smart alerting, reusing the edge-triggered notifier:
      • DOWN      — fired once after `fail_threshold` consecutive failures (anti-flap).
      • RECOVERED — fired once when it comes back, quoting the downtime duration.
      • SLOW      — optional warning when an up endpoint exceeds latency_warn_ms.
    Honours per-check alerts_enabled plus the global min-level/channel gating in _emit.
    Routing rules (match_kind='uptime') are respected, so uptime alerts can be
    steered to specific channels or muted like any other alert."""
    now = int(time.time())
    rules = get_notification_rules()
    for c in list_uptime_checks():
        cid = c["id"]
        down_key, slow_key, rec_key = f"uptime:down:{cid}", f"uptime:slow:{cid}", f"uptime:rec:{cid}"
        if not c["enabled"] or not c["alerts_enabled"]:
            _clear(down_key); _clear(slow_key); _uptime_down_since.pop(cid, None)
            from backend.db.repos import edge_state as _edge_state_repo
            import app as _app
            _edge_state_repo.clear_down_since(cid, conn=_app.DB)
            continue
        # Skip alerting entirely while this check is in a maintenance window.
        if _in_maintenance("uptime", cid) or _in_maintenance("uptime", c.get("label", "")):
            _clear(down_key); _clear(slow_key); _clear(cert_key if "cert_key" in dir() else f"uptime:cert:{cid}")
            _uptime_down_since.pop(cid, None)
            from backend.db.repos import edge_state as _edge_state_repo
            import app as _app
            _edge_state_repo.clear_down_since(cid, conn=_app.DB)
            continue
        thr = max(1, int(c.get("fail_threshold") or 2))
        st = _uptime_state(cid, now)
        tgt = c["target"] if c["type"] == "http" else f"TCP {c['target']}"
        if _uptime_confirmed_down(cid, thr):
            with _NOTIFIER_LOCK:
                first = down_key not in _NOTIFIED
            if first:
                _uptime_down_since[cid] = _uptime_streak_start(cid, now)
                from backend.db.repos import edge_state as _edge_state_repo
                import app as _app
                _edge_state_repo.set_down_since(cid, _uptime_down_since[cid], conn=_app.DB)
            _clear(rec_key)   # re-arm recovery so the eventual comeback fires once
            _emit(s, down_key, "critical", f"🔴 {c['label']} is DOWN",
                  f"{tgt} — {_uptime_down_reason(st)}", rules=rules)
            _clear(slow_key)
        elif st["state"] == "up":
            with _NOTIFIER_LOCK:
                was_down = down_key in _NOTIFIED
            if was_down:
                since = _uptime_down_since.pop(cid, None)
                from backend.db.repos import edge_state as _edge_state_repo
                import app as _app
                _edge_state_repo.clear_down_since(cid, conn=_app.DB)
                dur = _fmt_dur(now - since) if since else "?"
                _clear(down_key)
                # Recovery is good news → emitted at "warning" so it survives the
                # default min-level (a recovery the user never sees is worse than a
                # slightly louder one). It clears the moment the check drops again.
                _emit(s, rec_key, "warning", f"🟢 {c['label']} recovered",
                      f"{tgt} — back up after {dur} down.", rules=rules)
            else:
                _clear(rec_key)
            lw, lat = c.get("latency_warn_ms"), st.get("last_latency_ms")
            if lw and lat is not None and lat > lw:
                _emit(s, slow_key, "warning", f"🐢 {c['label']} is slow",
                      f"{tgt} — {round(lat)} ms (warns above {lw} ms).", rules=rules)
            else:
                _clear(slow_key)
            cert_key = f"uptime:cert:{cid}"
            cert_days = st.get("cert_days_remaining")
            if cert_days is not None:
                if cert_days <= 7:
                    _emit(s, cert_key, "critical", f"🔒 {c['label']} TLS cert expiring",
                          f"{tgt} — cert expires in {cert_days}d.", rules=rules)
                elif cert_days <= 21:
                    _emit(s, cert_key, "warning", f"🔒 {c['label']} TLS cert expiring soon",
                          f"{tgt} — cert expires in {cert_days}d.", rules=rules)
                else:
                    _clear(cert_key)
            else:
                _clear(cert_key)

# ── Sampling ────────────────────────────────────────────────────────────────
def smi(args):
    # 3 s timeout (matches probe.py and the local-hw nvidia-smi call). A wedged
    # driver (GPU off the bus / Xid) makes nvidia-smi hang; the GPU half of
    # sample_once is non-fatal, so a short timeout degrades the GPU panel quickly
    # instead of stalling host metrics (CPU/RAM/temp) for the full window.
    return subprocess.run(["nvidia-smi", *args], capture_output=True, text=True, timeout=3).stdout.strip()

def _gpu_num(x):
    """Tolerant float for nvidia-smi CSV fields: '[N/A]' / '[Not Supported]' /
    blank → 0.0 instead of raising, so one unreadable field (power/temp) never
    aborts the whole GPU sample and hides a present GPU."""
    try:
        return float((x or "").strip())
    except ValueError:
        return 0.0

def _smi_uuid_idx():
    """GPU UUID → card index. `--query-compute-apps` identifies a process's card
    by UUID and never by index, so this map is the only way to answer "which card
    is this process on". {} when unavailable — attribution then stays pooled, as
    it was before per-card support. Mirrors probe._nvidia_uuid_idx."""
    out = {}
    try:
        for line in smi(["--query-gpu=index,uuid", "--format=csv,noheader,nounits"]).splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) >= 2 and p[1]:
                out[p[1]] = int(_gpu_num(p[0]))
    except Exception:
        pass
    return out

def _gpu_opt(x):
    """Like _gpu_num, but an unsupported field stays None instead of becoming 0.

    Use this for any metric where "the card can't tell us" and "the measured
    value is zero" are different claims a human would act on differently — fan
    speed being the sharp case: 0% means a stalled fan, absent means a passively
    cooled card. The cockpit renders the two differently and never alerts on the
    second. Mirrors probe._smi_opt."""
    s = (x or "").strip()
    if not s or s.startswith("["):
        return None
    try:
        return float(s)
    except ValueError:
        return None

# ── AMD GPU back-end (issue #1) ───────────────────────────────────────────────
# NVIDIA goes through nvidia-smi (above); AMD has no universally-present CLI, so we
# read the kernel's amdgpu sysfs directly — available on any host with the in-tree
# `amdgpu` driver, ROCm NOT required. The hub reads the host's sysfs through the
# read-only HOST_ROOT mount via _hp(); the remote Linux probe (probe.py) reads /sys
# in place. Strictly additive: consulted only when nvidia-smi reports no card, so
# NVIDIA and GPU-less hosts behave exactly as before.
def _amd_read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except OSError as e:
        # amdgpu's gpu_busy_percent intermittently returns EBUSY ("Device or
        # resource busy") — a known driver quirk; one retry usually clears it.
        # Any other OS error means the node is genuinely absent → treat as None.
        if getattr(e, "errno", None) == errno.EBUSY:
            try:
                with open(path) as f:
                    return int(f.read().strip())
            except (OSError, ValueError):
                return None
        return None
    except ValueError:
        return None

def _amd_hwmon(dev, fname):
    """Read a hwmon scalar (e.g. temp1_input in millidegrees C, power1_average in
    microwatts) from the card's hwmon node. None if absent/unreadable."""
    try:
        for h in sorted(os.listdir(os.path.join(dev, "hwmon"))):
            v = _amd_read_int(os.path.join(dev, "hwmon", h, fname))
            if v is not None:
                return v
    except OSError:
        pass
    return None

_PCI_IDS_PATHS = ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids")
_AMD_PCI_NAMES = None      # device-id -> pci.ids device name for vendor 0x1002, lazy

def _amd_pci_names():
    """The pci.ids device table for vendor 0x1002, parsed once and cached.

    The slim image ships no pci.ids, but the host's copy is visible through the
    HOST_ROOT bind mount (Fedora/Arch keep it in /usr/share/hwdata, Debian/Ubuntu
    in /usr/share/misc). {} when neither file is readable — callers fall back."""
    global _AMD_PCI_NAMES
    if _AMD_PCI_NAMES is not None:
        return _AMD_PCI_NAMES
    complete_empty = False
    for p in _PCI_IDS_PATHS:
        names = {}
        try:
            with open(_hp(p), encoding="utf-8", errors="replace") as f:
                in_amd = False
                for line in f:
                    if line.startswith("#") or not line.strip():
                        continue
                    if not line.startswith("\t"):          # vendor row
                        in_amd = line[:4].lower() == "1002"
                    elif in_amd and not line.startswith("\t\t"):   # device row
                        did, _, name = line.strip().partition("  ")
                        if re.fullmatch(r"[0-9a-fA-F]{4}", did) and name:
                            names[did.lower()] = name.strip()
                    elif in_amd is False and names:
                        break                              # left the AMD block
        except OSError:
            continue        # unreadable, or died mid-file — discard the partial parse
        if names:
            _AMD_PCI_NAMES = names
            return names
        complete_empty = True
    # Cache decides by what happened: a file we consumed to the end but that had
    # no AMD block won't grow one — cache the empty table rather than re-parsing
    # the whole file per card per tick. No file readable at all stays uncached:
    # a bind mount added later (or a package install) should be picked up, and
    # the retry is just two failed opens.
    if complete_empty:
        _AMD_PCI_NAMES = {}
    return {}

def _amd_pci_name(dev):
    """Card name from pci.ids for kernels that expose no product_name (every APU,
    e.g. Strix Halo). None when the device id is unknown — the caller keeps its
    'AMD GPU <n>' fallback.

    pci.ids writes AMD entries as 'Codename [Retail Name / Retail Name / …]'. The
    bracket is used when it names exactly one retail product; when it lists several
    variants sharing the silicon (Strix Halo covers 8050S and 8060S) we can't tell
    which one this host has, so the codename is the honest label."""
    try:
        with open(os.path.join(dev, "device")) as f:
            did = f.read().strip().lower()
    except OSError:
        return None
    raw = _amd_pci_names().get(did[2:] if did.startswith("0x") else did)
    if not raw:
        return None
    m = re.fullmatch(r"(.+?)\s*\[(.+)\]", raw)
    if m:
        raw = m.group(1).strip() if "/" in m.group(2) else m.group(2).strip()
    return raw if raw.upper().startswith("AMD") else "AMD " + raw

def amd_gpus(drm_root=None):
    """Per-card AMD snapshot from amdgpu sysfs, matching the dict shape sample_once()
    builds for NVIDIA cards (idx/name/util/mem_used/mem_total/power/temp; MB, %, W,
    °C). Empty list when no AMD GPU is present. `drm_root` is injectable for tests;
    in production it points at the host's /sys/class/drm through HOST_ROOT."""
    if drm_root is None:
        drm_root = _hp("/sys/class/drm")
    try:
        entries = sorted(os.listdir(drm_root))
    except OSError:
        return []
    gpus = []
    for nm in entries:
        m = re.fullmatch(r"card(\d+)", nm or "")
        if not m:
            continue
        dev = os.path.join(drm_root, nm, "device")
        try:
            with open(os.path.join(dev, "vendor")) as f:
                if f.read().strip().lower() != "0x1002":   # 0x1002 = AMD/ATI
                    continue
        except OSError:
            continue
        vram_total = _amd_read_int(os.path.join(dev, "mem_info_vram_total"))  # bytes
        vram_used  = _amd_read_int(os.path.join(dev, "mem_info_vram_used"))   # bytes
        # APU / unified-memory iGPU (e.g. Ryzen AI Max / Strix Halo): the dedicated
        # VRAM is a tiny BIOS carve-out (<= ~1 GiB) while the real working set — where
        # models actually load — lives in GTT (system RAM mapped to the GPU, up to
        # nearly all system RAM). Reporting the 512 MB carve-out makes the dashboard
        # read "29% full / VRAM ran low" on an idle 128 GB box. When this looks like an
        # APU (tiny VRAM + large GTT), report GTT instead so residency + pressure
        # reflect reality. Discrete cards (large VRAM) are unaffected.
        gtt_total = _amd_read_int(os.path.join(dev, "mem_info_gtt_total"))    # bytes
        gtt_used  = _amd_read_int(os.path.join(dev, "mem_info_gtt_used"))     # bytes
        unified = bool(vram_total and gtt_total and vram_total <= (1 << 30))  # VRAM <= 1 GiB -> iGPU
        if unified:
            total, used = gtt_total, (gtt_used or 0)
        else:
            total, used = vram_total, vram_used
        busy   = _amd_read_int(os.path.join(dev, "gpu_busy_percent"))      # %
        temp_m = _amd_hwmon(dev, "temp1_input")     # millidegrees C
        powr_u = _amd_hwmon(dev, "power1_average")  # microwatts
        name = None
        try:
            with open(os.path.join(dev, "product_name")) as f:   # newer kernels only
                name = f.read().strip() or None
        except OSError:
            pass
        if not name:
            name = _amd_pci_name(dev)     # pci.ids via HOST_ROOT; None when unknown
        # PCI address of the card (cardN/device is a symlink into /sys/devices/pci…):
        # lets per-process fdinfo attribution match its drm-pdev to *this* card, so a
        # hybrid APU + discrete-AMD host counts GTT only for the APU. None when the
        # link doesn't resolve to a BDF (test fixtures, exotic buses) — attribution
        # then falls back to the any-card-unified heuristic.
        pdev = os.path.basename(os.path.realpath(dev))
        if not re.fullmatch(r"[0-9a-fA-F]{4,}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", pdev):
            pdev = None
        g = {
            "idx": int(m.group(1)),
            "name": name or "AMD GPU %s" % m.group(1),
            "util": float(busy) if busy is not None else 0.0,
            "mem_used":  round(used / 1048576.0) if used is not None else 0.0,
            "mem_total": round(total / 1048576.0) if total is not None else 0.0,
            "power": round(powr_u / 1e6, 1) if powr_u is not None else 0.0,
            "temp":  round(temp_m / 1000.0, 1) if temp_m is not None else 0.0,
            "unified": unified,   # APU/GTT mode — per-process attribution counts GTT too
            "pdev": pdev,
        }
        # Fan: pwm1 is the duty cycle 0-255, fan1_input the tachometer in RPM.
        # Both stay absent on a card with no fan node (passively cooled, or a
        # laptop where the EC owns the fan) rather than reading as a stalled fan.
        pwm = _amd_hwmon(dev, "pwm1")
        if pwm is not None:
            g["fan"] = round(pwm * 100.0 / 255.0)
        rpm = _amd_hwmon(dev, "fan1_input")
        if rpm is not None:
            g["fan_rpm"] = rpm
        _amd_enrich_card(g, dev)   # clocks/perf level/cap — here, while dev is known
        gpus.append(g)
    return gpus

_PP_DPM_CUR = re.compile(r":\s*(\d+)\s*[Mm][Hh]z\s*\*")   # the row the driver stars

def _amd_dpm_mhz(dev, fname):
    """Current clock in MHz from a pp_dpm_* table ('1: 1100Mhz *' — the starred row
    is the active level). None when the file is absent or nothing is starred."""
    try:
        with open(os.path.join(dev, fname)) as f:
            for line in f:
                m = _PP_DPM_CUR.search(line)
                if m:
                    return int(m.group(1))
    except OSError:
        pass
    return None

def _amd_hwmon_labeled(dev, prefix, label):
    """Value of the hwmon channel whose <prefix>N_label matches `label` (e.g. the
    temp channel labelled 'mem'), or None. Channel numbers are not stable across
    cards/drivers, so matching by label is the only reliable way."""
    try:
        for h in sorted(os.listdir(os.path.join(dev, "hwmon"))):
            hdir = os.path.join(dev, "hwmon", h)
            try:
                entries = sorted(os.listdir(hdir))
            except OSError:
                continue
            for e in entries:
                if e.startswith(prefix) and e.endswith("_label"):
                    try:
                        with open(os.path.join(hdir, e)) as f:
                            if f.read().strip() != label:
                                continue
                    except OSError:
                        continue
                    v = _amd_read_int(os.path.join(hdir, e[:-6] + "_input"))
                    if v is not None:      # unreadable input: keep scanning — another
                        return v           # hwmon dir may carry the same label
    except OSError:
        pass
    return None

def _amd_hwmon_has(dev, fname):
    """Whether any of the card's hwmon dirs contains `fname`."""
    try:
        for h in sorted(os.listdir(os.path.join(dev, "hwmon"))):
            if os.path.exists(os.path.join(dev, "hwmon", h, fname)):
                return True
    except OSError:
        pass
    return False

def _amd_enrich_card(g, dev):
    """Best-effort AMD counterpart of _enrich_gpus: attach mem_util/clk_sm/clk_mem/
    power_limit/pstate/temp_mem to one card dict, from amdgpu sysfs instead of
    nvidia-smi. Mutates in place; an absent node leaves its field unset, so the
    same UI chips that hide on an NVIDIA '[N/A]' stay hidden here too.

    Runs inside amd_gpus' per-card loop rather than as a separate collector pass:
    the collector re-indexes AMD cards above the NVIDIA range (gpu_samples.idx must
    not collide), so after that point idx no longer names the sysfs cardN and the
    dev path here is the only reliable handle. Unlike the NVIDIA enrichment (extra
    nvidia-smi round-trips, kept out of the base query), these are a handful of
    sysfs reads on the card we're already visiting.

    What each card offers varies: discrete cards have mem_busy_percent, a power cap
    and a 'mem' temp channel; APUs (Strix Halo) have none of those but do publish
    the pp_dpm_* clock tables and the hwmon sclk frequency. Everything is optional
    and read independently."""
    clk = _amd_dpm_mhz(dev, "pp_dpm_sclk")
    if clk is None:
        # hwmon fallback, by label like everything else here: freq1 is usually
        # sclk but nothing guarantees the ordering. A bare freq1_input is trusted
        # only when the channel is unlabelled (very old kernels).
        hz = _amd_hwmon_labeled(dev, "freq", "sclk")
        if hz is None and not _amd_hwmon_has(dev, "freq1_label"):
            hz = _amd_hwmon(dev, "freq1_input")        # Hz
        clk = round(hz / 1e6) if hz else None
    if clk is not None:
        g["clk_sm"] = clk
    mclk = _amd_dpm_mhz(dev, "pp_dpm_mclk")
    if mclk is not None:
        g["clk_mem"] = mclk
    mbusy = _amd_read_int(os.path.join(dev, "mem_busy_percent"))
    if mbusy is not None:
        g["mem_util"] = mbusy
    cap = _amd_hwmon(dev, "power1_cap")                # microwatts
    if cap:
        g["power_limit"] = round(cap / 1e6, 1)
    lvl = _rt(os.path.join(dev, "power_dpm_force_performance_level"))
    if lvl and lvl.strip():
        g["pstate"] = lvl.strip()                      # auto / low / high / manual
    tmem = _amd_hwmon_labeled(dev, "temp", "mem")      # millidegrees C
    if tmem:
        g["temp_mem"] = round(tmem / 1000.0, 1)

_DRM_UNITS = {"": 1, "KiB": 1024, "MiB": 1048576, "GiB": 1073741824, "TiB": 1099511627776}
_DRM_MAJOR = 226        # /dev/dri/* — char-major-226, Documentation/admin-guide/devices.txt

def _fdinfo_bytes(val):
    """DRM fdinfo memory value ('326612 KiB', '4 MiB') → bytes. 0 when the field is
    absent or malformed.

    A bare number is BYTES, not KiB: the kernel's fdinfo formatter only scales up
    while the value divides evenly by 1024, so anything unaligned prints raw — which
    is also why a zero shows up as a plain '0' and never '0 KiB'. An unrecognised
    unit yields 0 rather than a figure that could be off by a factor of 1024."""
    parts = (val or "").split()
    try:
        n = int(parts[0])
    except (IndexError, ValueError):
        return 0
    return n * _DRM_UNITS.get(parts[1] if len(parts) > 1 else "", 0)

def _fdinfo_region_bytes(f, region):
    """Bytes one DRM client holds privately in <region> ('vram' or 'gtt').

    drm-resident-* is the standardised key; kernels predating that standardisation
    (6.1–6.14, including the 6.6 and 6.12 LTS lines) publish the same figure as
    drm-memory-*, and drm-total-* is its printed alias — so both are fallbacks.
    Without them attribution silently reports nothing on those kernels.

    Selection is by key PRESENCE, not truthiness: drm-resident-* can legitimately
    say 0 (an evicted allocation) while drm-total-* stays nonzero, and falling
    through on the zero would attribute non-resident memory as current residency.

    drm-shared-* is subtracted: a buffer shared between two clients (a dma-buf
    between an app and a compositor, say) is counted in the residency of BOTH, and
    their client-ids differ so dedup can't catch it. Crediting it whole to each
    would let per-service totals exceed the card's capacity; the shared remainder
    stays in the chart's system/other bucket instead of being counted twice."""
    resident = 0
    for prefix in ("drm-resident-", "drm-memory-", "drm-total-"):
        val = f.get(prefix + region)
        if val is not None:
            resident = _fdinfo_bytes(val)
            break
    return max(0, resident - _fdinfo_bytes(f.get("drm-shared-" + region)))

def _drm_fd_rdev(proc_root, pid, fd):
    """The fd's device number, or None when the stat isn't possible.

    Identity is the device number, not the path: a container may map the render node
    anywhere (--device=/dev/dri/renderD128:/dev/gpu0), and a path test would drop it
    along with all of that container's attribution. None means "read fdinfo anyway"
    — a process in another user namespace (rootless podman) denies the stat while
    still allowing fdinfo, and that container is often the one holding all the
    VRAM."""
    try:
        st = os.stat(os.path.join(proc_root, pid, "fd", fd))
    except OSError:
        return None
    return st.st_rdev                              # 0 for anything but a device

def amd_fdinfo_procs(proc_root="/proc"):
    """Per-PID AMD GPU memory from DRM fdinfo — the amdgpu counterpart of
    `nvidia-smi --query-compute-apps`, which AMD has no equivalent of without ROCm
    tools (and this project's AMD back-end is deliberately sysfs-only, issue #1).
    Any process holding the GPU has /proc/<pid>/fd/N → a char-major-226 device and
    a matching fdinfo file with `drm-driver: amdgpu` plus per-region residency keys
    (drm-resident-*, or drm-memory-*/drm-total-* on older kernels; pre-5.19 kernels
    have none and yield {}). Returns {pid: {pdev: {"vram": MB, "gtt": MB}}} — split
    per PCI device (pdev may be None on kernels that omit drm-pdev) so the caller
    can apply the APU-vs-discrete GTT policy per card, not host-wide.

    Clients are counted once per (device, client-id) GLOBALLY, not per pid: dup()'d
    fds, threads and forked children all republish the same DRM client, and a
    supervisor keeping an inherited fd open would otherwise double the worker's
    buffers across two services. Pids scan lowest-first so such a client is always
    credited to the same process — with readdir order the owner would flip between
    samples and the per-service history would sawtooth. Client-ids are recycled, so
    in the rare case where one is freed and reissued mid-scan we drop the second
    sighting — one sample's worth of memory, self-correcting on the next.

    Reads the hub's own /proc: the hub runs in the host PID namespace, so host pids
    and their fds are visible (same access service_for_pid relies on)."""
    out = {}
    try:
        pids = [p for p in os.listdir(proc_root) if p.isdigit()]
    except OSError:
        return {}
    seen = set()       # (device, client-id) already counted, across ALL pids
    for pid in sorted(pids, key=int):
        fddir = os.path.join(proc_root, pid, "fd")
        try:
            fds = os.listdir(fddir)
        except OSError:
            continue   # process vanished, or fd table not readable
        devs = {}      # pdev -> [vram bytes, gtt bytes]
        for fd in fds:
            rdev = _drm_fd_rdev(proc_root, pid, fd)
            if rdev is not None and os.major(rdev) != _DRM_MAJOR:
                continue                       # cheap reject; None = read fdinfo anyway
            try:
                with open(os.path.join(proc_root, pid, "fdinfo", fd)) as f:
                    txt = f.read(8192)     # the drm-* block sits well inside this
            except OSError:
                continue
            if "drm-driver" not in txt:
                continue
            fields = {}
            for line in txt.splitlines():
                k, sep, val = line.partition(":")
                if sep and k.startswith("drm-"):
                    fields[k.strip()] = val.strip()
            client = fields.get("drm-client-id")
            pdev = fields.get("drm-pdev")
            # Dedup device identity: pdev when the kernel names it; else the fd's
            # device number (client-ids are per-device counters, so two pdev-less
            # cards can carry the same id — collapsing them would drop one); else
            # the pid, which narrows dedup to upstream's per-pid semantics rather
            # than ever dropping a client.
            dev_id = pdev if pdev is not None else (rdev if rdev is not None else pid)
            if (fields.get("drm-driver") != "amdgpu" or client is None
                    or (dev_id, client) in seen):
                continue
            seen.add((dev_id, client))
            vb = _fdinfo_region_bytes(fields, "vram")
            gb = _fdinfo_region_bytes(fields, "gtt")
            if vb or gb:
                d = devs.setdefault(pdev, [0, 0])
                d[0] += vb
                d[1] += gb
        if devs:
            out[int(pid)] = {pdev: {"vram": v / 1048576.0, "gtt": g / 1048576.0}
                             for pdev, (v, g) in devs.items()}
    return out

def _amd_attrib_mb(devs, amd_cards):
    """MB one pid holds across AMD cards, given its per-pdev fdinfo split. VRAM
    always counts; GTT counts only on unified-memory (APU) devices — matched by
    pdev so a discrete AMD card living next to an APU doesn't get its staging
    buffers in GTT misread as VRAM. A pdev we can't match to a known card (kernel
    omits drm-pdev, or sysfs didn't yield a BDF) falls back to the host-wide
    any-card-unified heuristic."""
    known   = {g["pdev"] for g in amd_cards if g.get("pdev")}
    uni_set = {g["pdev"] for g in amd_cards if g.get("pdev") and g.get("unified")}
    any_uni = any(g.get("unified") for g in amd_cards)
    mb = 0.0
    for pdev, m in devs.items():
        uni = (pdev in uni_set) if (pdev and pdev in known) else any_uni
        mb += m["vram"] + (m["gtt"] if uni else 0.0)
    return mb

# Extra per-card telemetry the AI/DS crowd actually debugs with: memory-bandwidth
# utilisation (mem-bound vs compute-bound), core/memory clocks, power limit (for
# headroom), performance state, memory-junction temp, and the *throttle reasons*
# that explain a card quietly running below its rated clocks. All best-effort —
# many consumer GPUs report "[N/A]" for some of these and very old drivers may not
# know a field name at all — so enrichment runs in its own guard and never blocks
# the core GPU sample. `clocks_throttle_reasons.active` is a hex bitmask; the
# field prefix was renamed `clocks_event_reasons` in newer drivers, so we try both.
_THROTTLE_BITS = [
    (0x0000000000000004, "Power cap"),    # SW_POWER_CAP — hitting the power limit
    (0x0000000000000008, "HW slowdown"),  # HW_SLOWDOWN (thermal/power/other, generic)
    (0x0000000000000020, "SW thermal"),   # SW_THERMAL_SLOWDOWN
    (0x0000000000000040, "HW thermal"),   # HW_THERMAL_SLOWDOWN — too hot
    (0x0000000000000080, "Power brake"),  # HW_POWER_BRAKE_SLOWDOWN — external power brake
]

# The subset of _THROTTLE_BITS that means "too hot" rather than "at the power
# cap". A card pinned at its power limit is working as configured (vader's 3090s
# run that way by design); a card slowing down thermally is a problem. The alert
# rules treat the two differently, so the distinction lives here, once.
_THERMAL_BITS = 0x0000000000000068   # SW thermal | HW thermal | HW slowdown

def _decode_throttle(hexstr):
    """nvidia-smi throttle bitmask → list of *meaningful* reasons. Idle / app-clocks
    / sync-boost / display bits are normal and intentionally ignored."""
    return _decode_throttle_mask(hexstr)[1]

def _decode_throttle_mask(hexstr):
    """As _decode_throttle, but returns (mask, reasons) — the raw mask is what
    gets stored per sample, so history can distinguish thermal throttling from
    power-capping without re-deriving it from the label strings."""
    try:
        mask = int(str(hexstr).strip(), 16)
    except (TypeError, ValueError):
        return 0, []
    return mask, [label for bit, label in _THROTTLE_BITS if mask & bit]

def _enrich_gpus(gpus):
    """Best-effort: attach mem_util/clocks/power_limit/pstate/temp_mem and throttle
    reasons to each card dict in `gpus` (matched by index). Mutates in place."""
    by_idx = {g["idx"]: g for g in gpus}
    try:
        rows = smi(["--query-gpu=index,utilization.memory,clocks.current.sm,clocks.current.memory,"
                    "power.limit,temperature.memory,pstate", "--format=csv,noheader,nounits"]).splitlines()
        for line in rows:
            p = [x.strip() for x in line.split(",")]
            if len(p) < 7:
                continue
            g = by_idx.get(int(_gpu_num(p[0])))
            if not g:
                continue
            # '[N/A]' must leave mem_util ABSENT, not 0: _gpu_extra averages only
            # the cards that measured, and the UI hides the chip on absence — a
            # coerced 0 would read as a confident "0% mem-bandwidth".
            # Every one of these stays ABSENT when the card doesn't report it,
            # never 0. _gpu_num would coerce '[N/A]' to 0.0, and the cockpit
            # derives its per-card `supports` map from presence — a coerced zero
            # advertises the metric as supported and then draws a confident flat
            # line at zero. The remote probe's _nvidia_enrich already does this;
            # the hub's own cards were the inconsistent half.
            for key, raw in (("mem_util", p[1]), ("clk_sm", p[2]), ("clk_mem", p[3]),
                             ("power_limit", p[4]), ("temp_mem", p[5])):
                v = _gpu_opt(raw)
                if v is not None:
                    g[key] = v
            if p[6] and not p[6].startswith("["):
                g["pstate"] = p[6]
    except Exception:
        pass
    for field in ("clocks_throttle_reasons.active", "clocks_event_reasons.active"):
        try:
            rows = smi(["--query-gpu=index," + field, "--format=csv,noheader,nounits"]).splitlines()
        except Exception:
            continue
        ok = False
        for line in rows:
            p = [x.strip() for x in line.split(",")]
            if len(p) < 2 or not p[1] or p[1].startswith("["):
                continue
            g = by_idx.get(int(_gpu_num(p[0])))
            if g is None:
                continue
            mask, reasons = _decode_throttle_mask(p[1])
            g["throttle_mask"] = mask
            g["throttle"] = reasons
            g["throttled"] = bool(reasons)
            ok = True
        if ok:
            break

def _gpu_extra(gpus):
    """Aggregate the enriched per-card telemetry into one representative dict for the
    always-visible 'GPU right now' panel (single-GPU rigs never see the per-card cards)."""
    if not gpus:
        return {}
    g0 = gpus[0]
    out = {
        "clk_sm":    round(g0.get("clk_sm", 0)),
        "clk_mem":   round(g0.get("clk_mem", 0)),
        "pstate":    g0.get("pstate", ""),
        "temp_mem":  round(max((g.get("temp_mem", 0) for g in gpus), default=0)),
        "throttled": any(g.get("throttled") for g in gpus),
        "throttle":  sorted({r for g in gpus for r in g.get("throttle", [])}),
    }
    # The power chip divides the POOLED draw by this cap, so the cap is published
    # only when every card contributed one: with a card of unknown cap (AMD APUs
    # have no power1_cap, NVIDIA can say '[N/A]') the ratio would exceed 100%
    # merely because the denominator is missing a card the numerator includes.
    if all(g.get("power_limit", 0) > 0 for g in gpus):
        out["power_limit"] = round(sum(g["power_limit"] for g in gpus))
    # mem-bandwidth utilisation averaged over the cards that actually measured it:
    # cards without the counter (AMD APUs have no mem_busy_percent, NVIDIA can say
    # '[N/A]') must neither surface a fabricated 0% chip nor dilute a measured
    # value — 0 measured and 0 unknown are different claims.
    measured = [g["mem_util"] for g in gpus if "mem_util" in g]
    if measured:
        out["mem_util"] = round(sum(measured) / len(measured))
    # Fan follows the same "only the cards that measured it" rule: a passively
    # cooled card in the box must not pull the average toward a speed nothing
    # reported. fan_max is what the cockpit shows — "is there headroom left?" is
    # answered by the busiest fan, not by the mean.
    fans = [g["fan"] for g in gpus if g.get("fan") is not None]
    if fans:
        out["fan"] = round(sum(fans) / len(fans))
        out["fan_max"] = round(max(fans))
    return out

def service_for_pid(pid, nm):
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            h = HEX64.search(f.read())
        if h and nm.get(h.group(0)[:12]):
            return nm[h.group(0)[:12]]
        with open(f"/proc/{pid}/comm") as f:
            return "host:" + f.read().strip()
    except Exception:
        return f"pid:{pid}"

# ── Caller attribution ────────────────────────────────────────────────────────
# "Which service is driving Ollama?" Ollama's API never reveals its callers, so we
# observe it from the outside: for each container we read its OWN ESTABLISHED sockets
# (/proc/<pid>/net/tcp[6] — visible because the hub runs as root in the host PID ns)
# and match the REMOTE port against the ports a model server listens on. A caller
# reaching a server via host.docker.internal collapses onto the gateway IP, so the
# port — not the IP — is what identifies the server. Sampled every tick: long-lived
# LLM streams are caught reliably, sub-second calls (e.g. embeddings) are approximate.
_cpid_cache = {"at": 0, "map": {}}
def container_pids(nm):
    """Map container-name → one representative host PID (any pid shares the netns),
    by scanning /proc once. Cached briefly; the PID is only used to read net/tcp."""
    if time.time() - _cpid_cache["at"] < 25 and _cpid_cache["map"]:
        return _cpid_cache["map"]
    out = {}
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cgroup") as f:
                    h = HEX64.search(f.read())
            except Exception:
                continue
            if h:
                name = nm.get(h.group(0)[:12])
                if name and name not in out:
                    out[name] = pid
    except Exception:
        pass
    _cpid_cache.update(at=time.time(), map=out)
    return out

def _est_remote_ports(pid):
    """Remote ports (int) of a pid's ESTABLISHED TCP connections (state 01)."""
    ports = []
    for fn in (f"/proc/{pid}/net/tcp", f"/proc/{pid}/net/tcp6"):
        try:
            with open(fn) as f:
                next(f, None)
                for line in f:
                    p = line.split()
                    if len(p) > 3 and p[3] == "01":
                        try:
                            ports.append(int(p[2].split(":")[1], 16))
                        except Exception:
                            pass
        except Exception:
            pass
    return ports

def sample_callers(conts, ai_names):
    """{(caller, server): conn_count} for this instant. The hub's own containers are
    excluded so their probe traffic doesn't masquerade as real model usage."""
    targets = {}                                    # remote port → server name
    for c in conts:
        if c["name"] in ai_names:
            for p in (c.get("ports") or []):
                targets.setdefault(p, c["name"])
    if not targets:
        return {}
    nm = {c["id"]: c["name"] for c in conts}
    selfish = {c["name"] for c in conts
               if "homelab-monitor" in c["image"].lower() or "gpu-monitor" in c["image"].lower()}
    edges = {}
    for name, pid in container_pids(nm).items():
        if name in selfish:
            continue
        for port in _est_remote_ports(pid):
            srv = targets.get(port)
            if srv and srv != name:
                edges[(name, srv)] = edges.get((name, srv), 0) + 1
    return edges

# Phase 3.2: moved to backend/collectors/ — re-exported for backward compat
from backend.collectors import sample_once

_DISK_IO_MIN_DEV = 15.0    # MB/s: below this, a device's wobble isn't worth flagging
_DISKIO_ANOM_WINDOW  = 6 * 3600   # trailing baseline window (~6h at the ~45s disk-io cadence)
_DISKIO_ANOM_MIN_PTS = 30         # need a real baseline before scoring anything
_DISKIO_ANOM_Z       = 3.0        # |z| at/above this is "look here", not noise
_diskio_anom_active = set()       # devices currently flagged — edge-triggered so a
                                   # persistent spike logs one event, not one per scan
_diskio_anom_latest = {}          # device -> anomaly item, for the live /api/health badge

def _disk_io_anomaly_items(cur, now):
    """Per-device z-score flags on total (read+write) disk throughput: score the
    latest reading against a trailing baseline (mean/stddev of the window
    excluding the latest point), same maths style as the rest of the monitor's
    threshold checks. Returns a list of {device, value, baseline, z, direction,
    magnitude} dicts — only devices that actually fired. Never raises."""
    from backend.db.repos import system as _sys_repo2
    since = now - _DISKIO_ANOM_WINDOW
    by_dev = {}
    try:
        for dev, r, w in _sys_repo2.query_disk_io_for_anomaly(since, conn=cur):
            by_dev.setdefault(dev, []).append((r or 0.0) + (w or 0.0))
    except Exception:
        return []
    out = []
    for dev, vals in by_dev.items():
        if len(vals) < _DISKIO_ANOM_MIN_PTS:
            continue
        latest = vals[-1]
        base = vals[:-1]
        n = len(base)
        mean = sum(base) / n
        sd = (sum((v - mean) ** 2 for v in base) / n) ** 0.5
        dev_amt = latest - mean
        if abs(dev_amt) < _DISK_IO_MIN_DEV or sd <= 0:
            continue
        z = dev_amt / sd
        if abs(z) < _DISKIO_ANOM_Z:
            continue
        out.append({
            "device": dev, "value": round(latest, 1), "baseline": round(mean, 1),
            "z": round(z, 1), "direction": "spike" if z > 0 else "dip",
            "magnitude": round(abs(latest - mean), 1),
        })
    return out

def diskio_scan():
    """Edge-triggered disk-I/O anomaly check: a device logs ONE event on the
    ok->anomaly transition (not once per scan while it stays flagged), and drops
    out of the active set once it's back to baseline — mirrors the existing
    'log only on the state edge' convention used elsewhere so a persistently busy
    disk can't spam the Insight Feed. Writes into the same `events` table as OOM
    (kind='diskio_spike'), so it rides the existing events->insights plumbing for
    free — no new alert/incident system needed."""
    from backend.db.repos import system as _sys_repo
    if _DB_MAINTENANCE:
        return
    now = int(time.time())
    with LOCK:
        items = _disk_io_anomaly_items(DB.cursor(), now)
        firing = {it["device"]: it for it in items}
        newly = [it for dev, it in firing.items() if dev not in _diskio_anom_active]
        if newly:
            batch = [
                (now, it["device"], "diskio_spike",
                 (f"{it['direction']} to {it['value']} MB/s (baseline ~{it['baseline']} MB/s, "
                  f"{it['z']:+.1f}σ)")[:300])
                for it in newly
            ]
            _sys_repo.insert_events_batch(batch, conn=DB)
        _diskio_anom_active.clear()
        _diskio_anom_active.update(firing.keys())
    _diskio_anom_latest.clear()
    _diskio_anom_latest.update(firing)

def oom_scan():
    targets = ({p["service"] for p in LATEST["procs"]} |
               {x for x in os.environ.get("WATCH_CONTAINERS", "").split(",") if x})
    by_name = {c["name"]: c for c in containers()}
    for svc in targets:
        ct = by_name.get(svc)
        if not ct:
            continue
        for line in logs_since(ct["id"], _scan_since.get(svc, int(time.time()) - 3600)).splitlines():
            if not OOM_RE.search(line):
                continue
            m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", line)
            try:
                ets = int(time.mktime(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))) if m else int(time.time())
            except Exception:
                ets = int(time.time())
            if _DB_MAINTENANCE:
                continue
            from backend.db.repos import system as _sys_repo
            with LOCK:
                _sys_repo.insert_event(ets, svc, "oom", line.strip()[:300], conn=DB)
        _scan_since[svc] = int(time.time())

# Phase 3.2: moved to backend/collectors/ — re-exported for backward compat
from backend.collectors import _rollup_now

# Phase 3.2: moved to backend/collectors/ — re-exported for backward compat
from backend.collectors import _rollup_net_now

# Phase 3.2: moved to backend/collectors/ — re-exported for backward compat
from backend.collectors import collector

def build_insights(total, services, mem_total, events, host):
    ins, mem = [], total["mem"]
    if not mem:
        return [{"level": "info", "title": "Warming up", "detail": "Collecting the first samples…"}]
    peak = max(mem); free_min = mem_total - peak; pct = round(peak / mem_total * 100); pk = mem.index(peak)
    holders = sorted(((s, v[pk]) for s, v in services.items() if v[pk] > 0), key=lambda x: -x[1])
    dom, co = (holders[0] if holders else None), [h for h in holders[1:]]
    if events:
        ins.append({"level": "critical", "title": f"{len(events)} GPU out-of-memory event(s)",
                    "detail": (events[-1].get("blame", "") or "") +
                              " Free up VRAM headroom (smaller model, shorter keep-alive, or stagger heavy jobs)."})
    if free_min < PRESSURE_MB:
        d = f"GPU VRAM peaked at {pct}% — only {round(free_min)} MB free."
        if dom:
            d += f" {dom[0]} held {round(dom[1])} MB" + (f", alongside {', '.join(c[0] for c in co)}" if co else "") + "."
        ins.append({"level": "warning", "title": "GPU VRAM ran low", "detail": d})
    elif pct < 60:
        ins.append({"level": "ok", "title": "GPU has plenty of headroom",
                    "detail": f"VRAM peaked at {pct}% ({round(free_min)} MB free at the tightest)."})
    if dom and "ollama" in dom[0].lower() and dom[1] > mem_total * 0.5:
        ins.append({"level": "info", "title": "Ollama is the heavyweight",
                    "detail": f"{dom[0]} peaked at {round(dom[1])} MB. A shorter OLLAMA_KEEP_ALIVE or smaller default "
                              "model frees VRAM for other services between requests."})
    # host-level
    if host.get("disks"):
        worst = host["disks"][0]
        if worst["pct"] >= 90:
            ins.append({"level": "critical", "title": "Disk nearly full",
                        "detail": f"{worst['mount']} is {worst['pct']}% full ({worst['used']}/{worst['total']} GB)."})
    if host.get("ram_total") and host["ram_used"] / host["ram_total"] > 0.9:
        ins.append({"level": "warning", "title": "RAM pressure",
                    "detail": f"{round(100*host['ram_used']/host['ram_total'])}% of RAM in use."})
    if host.get("load1") and host.get("cores") and host["load1"] > host["cores"] * 1.5:
        ins.append({"level": "warning", "title": "High CPU load",
                    "detail": f"Load {host['load1']} on {host['cores']} cores."})
    return ins

# ── API ──────────────────────────────────────────────────────────────────
RANGES = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000, "all": None}


def _hhmm_to_min(s, default):
    """'HH:MM' -> minutes since midnight [0,1440). Falls back to `default` on junk."""
    try:
        h, m = str(s).split(":")
        v = int(h) * 60 + int(m)
        return v if 0 <= v < 1440 else default
    except Exception:
        return default

def _make_is_night(night_start, night_end):
    """Predicate is_night(ts) in the SERVER's local time. Handles a window that
    wraps midnight (start > end) and the degenerate start == end (no night)."""
    ns = _hhmm_to_min(night_start, 22 * 60)
    ne = _hhmm_to_min(night_end,    6 * 60)
    if ns == ne:
        return lambda ts: False                       # empty window => everything is day
    if ns < ne:                                        # same-day window, e.g. 01:00–05:00
        def is_night(ts):
            lt = time.localtime(ts)
            mins = lt.tm_hour * 60 + lt.tm_min
            return ns <= mins < ne
    else:                                              # wraps midnight, e.g. 22:00–06:00
        def is_night(ts):
            lt = time.localtime(ts)
            mins = lt.tm_hour * 60 + lt.tm_min
            return mins >= ns or mins < ne
    return is_night


_TOTAL_W_EXPR = "COALESCE(power,0)+COALESCE(cpu_power,0)+COALESCE(dram_power,0)"

def _cost_ctx():
    """Shared tariff context for the costs endpoints. Returns a dict with the
    day/night prices, mode, currency and an is_night(ts) predicate."""
    s = get_settings()
    def fnum(key):
        v = (s.get(key) or "").strip()
        try:
            return float(v) if v != "" else None
        except ValueError:
            return None
    day = fnum("kwh_price") or 0.0
    night = fnum("kwh_price_night")
    mode = "dual" if (s.get("tariff_mode") == "dual" and night is not None and day > 0) else "single"
    return {"day": day, "night": night, "mode": mode, "currency": s.get("currency") or "$",
            "is_night": _make_is_night(s.get("night_start", "22:00"), s.get("night_end", "06:00")),
            "night_start": s.get("night_start", "22:00"), "night_end": s.get("night_end", "06:00"),
            "idle_w": fnum("system_idle_watts")}

def _price_at(ctx, ts):
    return ctx["night"] if (ctx["mode"] == "dual" and ctx["is_night"](ts)) else ctx["day"]


MAX_RUN_FIELD, MAX_RUN_JSON, MAX_METRICS_REQ = 4096, 64 * 1024, 1000
RUN_SOURCES = {"jupyter", "colab", "kaggle", "mlflow", "api", "cli"}
RUN_STATUS  = {"running", "finished", "failed", "killed"}

# Multiple named API keys, each optionally with an expiry, stored HASHED (sha256 —
# the plaintext is shown once at creation and never persisted). Runs are attributed
# to the key that pushed them, so you can track per-key usage and revoke individually.
def _hash_key(k):
    return hashlib.sha256(k.encode("utf-8")).hexdigest()

def _create_api_key(name, expires_in_days=None):
    """Mint a key: persist its hash + metadata, return (id, plaintext). The plaintext
    is the only time it's available."""
    from backend.db.repos import auth as _auth_repo
    key = "hlm_" + secrets.token_urlsafe(32)
    kid, now = uuid.uuid4().hex, int(time.time())
    exp = (now + int(expires_in_days) * 86400) if expires_in_days else None
    with LOCK:
        _auth_repo.insert(kid, (name or "key")[:128], _hash_key(key), key[:12], now, exp, None, conn=DB)
    return kid, key

def _gen_api_key(name="default", expires_in_days=None):
    """Back-compat shim (used by tests): mint a key and return the plaintext."""
    return _create_api_key(name, expires_in_days)[1]

# Phase 3.4: moved to backend/auth — re-exported for backward compat
from backend.auth import _key_lookup, _presented_key, require_api_key

def _clip(v, n):
    return ("" if v is None else str(v))[:n]

def _json_field(v, n):
    if v is None:
        return None
    txt = v if isinstance(v, str) else json.dumps(v, separators=(",", ":"))
    if len(txt.encode("utf-8")) > n:
        raise ValueError("payload too large")
    return txt

def _safe_json(txt):
    try:
        return json.loads(txt) if txt else None
    except Exception:
        return None

def _run_cost_window(cur, started, ended, ctx):
    """Integrate samples.power over [started, ended] -> (energy_kwh, cost, avg_w,
    peak_util), priced exactly like the cost card (dual-tariff aware)."""
    from backend.db.repos import experiments as _exp_repo
    end = ended or int(time.time())
    kwh_per = INTERVAL / 3_600_000.0
    e_kwh = cost = sum_p = 0.0
    n = 0
    peak_u = 0.0
    for ts, util, power in _exp_repo.get_run_cost_samples(started, end, conn=cur):
        p = power or 0.0
        sum_p += p; n += 1; peak_u = max(peak_u, util or 0)
        e_kwh += p * kwh_per
        cost += p * kwh_per * (_price_at(ctx, ts) or 0.0)
    return round(e_kwh, 4), round(cost, 4), (round(sum_p / n) if n else 0), round(peak_u)


# ── Local-LLM model registry (ollama) ─────────────────────────────────────────
# Read-only inventory of the models pulled to this host's ollama. Self-contained:
# a small /api/ps (resident) + /api/tags (on-disk) poller, cached briefly so a
# chatty UI can't hammer ollama. No generation, no GPU spin, no secret leak.
COPILOT_OLLAMA_URL = os.environ.get("COPILOT_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
COPILOT_ENABLED    = os.environ.get("COPILOT_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
_REGISTRY_LOCK = threading.Lock()
_REGISTRY_CACHE = None            # {"ts": int, "models": [...], "reachable": bool}
_REGISTRY_TTL = float(os.environ.get("COPILOT_REGISTRY_TTL", "45"))  # seconds


def _llm_resident_models():
    """Poll ollama GET /api/ps for currently-LOADED models. Read-only, short
    timeout, stdlib only. Returns (list, reachable). Each entry:
    {name, size_mb, vram_mb, gpu_fraction, keep_alive_sec}. gpu_fraction is the
    share of the model resident in VRAM (size_vram/size) — surfaces GPU vs CPU
    offload. keep_alive_sec is seconds until expires_at (keep-alive countdown).

    MUST be called OUTSIDE any held LOCK (it does network I/O)."""
    if not COPILOT_ENABLED:
        return [], False
    url = COPILOT_OLLAMA_URL + "/api/ps"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return [], False
    return _parse_resident_models(data), True


def _parse_resident_models(data, now=None):
    """Parse an ollama /api/ps payload into the resident-model list. Pure →
    unit-testable. Tolerates missing/odd fields."""
    now = now or time.time()
    out = []
    for m in (data or {}).get("models", []) if isinstance(data, dict) else []:
        name = m.get("name") or m.get("model")
        if not name:
            continue
        size = m.get("size") or 0
        vram = m.get("size_vram") or 0
        frac = round(vram / size, 3) if size > 0 else None
        keep = None
        exp = m.get("expires_at")
        if exp:
            try:
                # ollama emits RFC3339, e.g. 2026-06-21T12:00:00.123456789Z or +TZ
                from datetime import datetime
                s = exp.strip()
                # python's fromisoformat handles offsets; trim ns to µs and Z
                s = re.sub(r"(\.\d{6})\d+", r"\1", s).replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                keep = int(dt.timestamp() - now)
            except Exception:
                keep = None
        out.append({
            "name": name,
            "size_mb": round(size / 1048576) if size else None,
            "vram_mb": round(vram / 1048576) if vram else None,
            "gpu_fraction": frac,
            "keep_alive_sec": keep,
        })
    return out


def _parse_model_registry(tags, resident=None):
    """Parse an ollama /api/tags payload into the installed-model inventory. Pure
    → unit-testable. Cross-references the resident set (by model name) to flag
    which entries are loaded right now and surface their live VRAM. Tolerates
    missing/odd fields; never raises.

    Each entry: {name, size_bytes, size_gb, family, param_size, quant, modified,
    loaded(bool), vram_mb}."""
    res_by_name = {}
    for m in (resident or []):
        nm = m.get("name")
        if nm:
            res_by_name[nm] = m
    out = []
    for m in (tags or {}).get("models", []) if isinstance(tags, dict) else []:
        name = m.get("name") or m.get("model")
        if not name:
            continue
        size = m.get("size") or 0
        det = m.get("details") or {}
        r = res_by_name.get(name)
        out.append({
            "name": name,
            "size_bytes": size,
            "size_gb": round(size / 1073741824, 2) if size else 0.0,
            "family": det.get("family") or None,
            "param_size": det.get("parameter_size") or None,
            "quant": det.get("quantization_level") or None,
            "modified": m.get("modified_at") or m.get("modified") or None,
            "loaded": r is not None,
            "vram_mb": (r or {}).get("vram_mb"),
        })
    # Largest on disk first — the inventory's natural "what's eating my disk" sort.
    out.sort(key=lambda x: x["size_bytes"] or 0, reverse=True)
    return out


def _registry_totals(models):
    """Header summary for the registry: count, total disk bytes/GB, loaded count.
    Pure."""
    total = sum(m.get("size_bytes") or 0 for m in models)
    return {
        "count": len(models),
        "loaded": sum(1 for m in models if m.get("loaded")),
        "total_bytes": total,
        "total_gb": round(total / 1073741824, 2) if total else 0.0,
    }


def _fetch_model_registry():
    """Poll ollama GET /api/tags for the on-disk catalogue, cross-referenced with
    the resident set. Returns (models, reachable). Read-only, short timeout,
    stdlib only. MUST be called OUTSIDE any held LOCK (network I/O)."""
    if not COPILOT_ENABLED:
        return [], False
    url = COPILOT_OLLAMA_URL + "/api/tags"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            tags = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return [], False
    # Resident cross-ref is best-effort: if /api/ps fails we still return the
    # on-disk list (just without loaded flags), never an error.
    resident, _ = _llm_resident_models()
    return _parse_model_registry(tags, resident), True


def _model_registry(now=None):
    """Cached registry accessor. Serves a fresh fetch at most once per _REGISTRY_TTL
    so a chatty UI can't hammer ollama. Returns (models, reachable). Network I/O
    happens OUTSIDE the cache lock.

    Double-checked locking: after a cache-miss triggers a fetch, we re-check the
    cache under the lock again before writing. Without this, a burst of concurrent
    callers that all observe a stale cache at the same time each independently
    fetch in parallel (N simultaneous HTTP round-trips to ollama), defeating the
    TTL rate-limit on every cache expiry under load. With it, only the first
    caller past the gate fetches; everyone else who raced in behind it gets the
    winner's fresh result instead of triggering their own redundant fetch."""
    now = now or time.time()
    with _REGISTRY_LOCK:
        c = _REGISTRY_CACHE
        if c and (now - c["ts"]) < _REGISTRY_TTL:
            return list(c["models"]), c["reachable"]
    models, reachable = _fetch_model_registry()
    with _REGISTRY_LOCK:
        c = _REGISTRY_CACHE
        # Someone else may have already refreshed the cache while we were
        # fetching (network I/O happens outside the lock, so this window is
        # real). If so, defer to their result instead of overwriting it with
        # our possibly-slightly-older one.
        if c and (now - c["ts"]) < _REGISTRY_TTL:
            return list(c["models"]), c["reachable"]
        globals()["_REGISTRY_CACHE"] = {
            "ts": now, "models": models, "reachable": reachable}
    return list(models), reachable


def _merge_registry(ollama_models, catalog):
    """Combine the rich ollama on-disk registry (size/quant/param/modified) with the
    provider-tagged catalog built each sample cycle from EVERY recognised AI server
    (#219 — vLLM, llama.cpp, LM Studio, ComfyUI, InvokeAI, …). Pure → unit-testable.

    Ollama entries already carry the full registry detail and are tagged
    provider='ollama'/host='local' as-is. Catalog entries whose provider is
    'ollama' are dropped here (the ollama registry is the authoritative, richer
    source for that provider); every other provider becomes a lightweight entry
    (name/provider/loaded/vram — no on-disk size, most catalogue APIs don't expose
    one). Entries missing a model name are skipped."""
    out = [dict(m, provider="ollama", host="local") for m in ollama_models]
    seen = {(m["name"], m["provider"], "local") for m in out}
    hub = socket.gethostname()
    for c in catalog or []:
        name = c.get("model")
        provider = c.get("provider") or "other"
        chost = c.get("host") or "local"
        # The hub's OWN ollama is covered by the richer disk registry above —
        # drop only those duplicates. A REMOTE host's ollama models arrive
        # through this catalog and MUST pass through: dropping every
        # provider=='ollama' entry (as this did originally) silently blinded
        # the fleet registry to exactly the hosts #236 set out to cover.
        if not name or (provider == "ollama" and chost in ("local", hub)):
            continue
        key = (name, provider, chost)
        if key in seen:
            continue
        seen.add(key)
        size = c.get("size_bytes")
        out.append({
            "name": name, "provider": provider, "host": chost,
            "size_bytes": size,
            "size_gb": round(size / 1073741824, 2) if size else None,
            "family": c.get("family"), "param_size": c.get("param_size"),
            "quant": c.get("quant"), "modified": c.get("modified"),
            "loaded": bool(c.get("loaded")), "vram_mb": c.get("vram_mb"),
        })
    return out


_MLF_STATUS = {"RUNNING": "running", "FINISHED": "finished", "FAILED": "failed",
               "KILLED": "killed", "SCHEDULED": "running"}

def _mlf(method, path, payload=None, params=None, timeout=15):
    base = (get_settings().get("mlflow_uri") or "").rstrip("/")
    if not base:
        return None
    url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    hdr = {"Content-Type": "application/json"}
    tok = get_settings().get("mlflow_token")
    if tok:
        hdr["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return json.loads(body) if body else {}

def _ms_to_s(v):
    return int(v) // 1000 if v else None

def sync_mlflow():
    """Pull experiments/runs/metrics from the configured MLflow server into
    runs/run_metrics as source='mlflow'. Idempotent via uniq(source,ext_id)."""
    if not (get_settings().get("mlflow_uri") or "").strip():
        return 0
    exp_ids, tok = [], None
    while True:
        body = _mlf("POST", "/api/2.0/mlflow/experiments/search",
                    {"max_results": 1000, **({"page_token": tok} if tok else {})}) or {}
        exp_ids += [e["experiment_id"] for e in body.get("experiments", [])]
        tok = body.get("next_page_token")
        if not tok:
            break
    if not exp_ids:
        return 0
    now, synced, tok = int(time.time()), 0, None
    while True:
        body = _mlf("POST", "/api/2.0/mlflow/runs/search",
                    {"experiment_ids": exp_ids, "max_results": 1000,
                     **({"page_token": tok} if tok else {})}) or {}
        for run in body.get("runs", []):
            info, data = run.get("info", {}), run.get("data", {})
            ext = info.get("run_id") or info.get("run_uuid")
            if not ext:
                continue
            tagd = {t["key"]: t["value"] for t in data.get("tags", [])}
            name = info.get("run_name") or tagd.get("mlflow.runName") or ext[:8]
            started = _ms_to_s(info.get("start_time")) or now
            ended = _ms_to_s(info.get("end_time"))
            status = _MLF_STATUS.get(info.get("status"), "running")
            params = {p["key"]: p["value"] for p in data.get("params", [])}
            tags = {k: v for k, v in tagd.items() if not k.startswith("mlflow.")}
            from backend.db.repos import experiments as _exp_repo
            with LOCK:
                rid = _exp_repo.get_mlflow_run_id(ext, conn=DB) or uuid.uuid4().hex
                _exp_repo.upsert_mlflow_run(
                    rid, name, status, started, ended, ext,
                    json.dumps(params, separators=(",", ":")),
                    json.dumps(tags, separators=(",", ":")), now, conn=DB)
                _exp_repo.delete_run_metrics(rid, conn=DB)
            for m in data.get("metrics", []):
                hist = _mlf("GET", "/api/2.0/mlflow/metrics/get-history",
                            params={"run_id": ext, "metric_key": m["key"]}) or {}
                rows = [(rid, _ms_to_s(h.get("timestamp")) or started, int(h.get("step") or 0),
                         m["key"], float(h["value"])) for h in hist.get("metrics", [])]
                if rows:
                    with LOCK:
                        DB.executemany("INSERT INTO run_metrics(run_id,ts,step,key,value) VALUES(?,?,?,?,?)", rows)
                        DB.commit()
            synced += 1
        tok = body.get("next_page_token")
        if not tok:
            break
    return synced


_CT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")

def _docker_log_stream(name, tail, follow, timeout=20):
    """Yield Server-Sent Events of a container's logs over the Docker socket.
    Demuxes Docker's 8-byte stream framing (skipped for TTY containers); for
    follow=1 it keeps the connection open and emits a heartbeat every ~20s so a
    closed browser EventSource is noticed and the socket torn down cleanly.
    Speaks HTTP/1.0 straight over the socket: a 1.0 response is streamed raw
    (no chunked transfer-encoding), so a quiet-log heartbeat timeout simply
    recv()s again. The previous http.client version could not resume its chunked
    decoder after a timeout — the stdlib marks the socket file timed-out and
    every later read raises "cannot read from timed out object" — so the stream
    died with a traceback whenever a followed container went quiet."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    text = ""
    def lines_from(s):
        nonlocal text
        text += s
        out = []
        while "\n" in text:
            line, text = text.split("\n", 1)
            out.append(line)
        return out
    try:
        sock.connect(DOCKER_SOCK)
        if follow:
            sock.settimeout(timeout)
        path = (f"/containers/{name}/logs?stdout=1&stderr=1&timestamps=0"
                f"&tail={tail}&follow={'1' if follow else '0'}")
        sock.sendall(f"GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 65536:
            data = sock.recv(4096)
            if not data:
                break
            head += data
        head, _, chunk = head.partition(b"\r\n\r\n")
        st = head.split(b"\r\n", 1)[0].split(None, 2)   # e.g. b'HTTP/1.0 404 No such container'
        code = int(st[1]) if len(st) > 1 and st[1].isdigit() else 0
        if code != 200:
            reason = st[2].decode("utf-8", "replace") if len(st) > 2 else "bad response from docker"
            yield f"event: srverror\ndata: {code} {reason}\n\n"
            return
        framed, buf = None, b""
        while True:
            if chunk:
                if framed is None:
                    framed = chunk[0] in (0, 1, 2)     # 8-byte header stream vs raw TTY
                if framed:
                    buf += chunk
                    while len(buf) >= 8:
                        size = int.from_bytes(buf[4:8], "big")
                        if len(buf) < 8 + size:
                            break
                        payload, buf = buf[8:8 + size], buf[8 + size:]
                        for line in lines_from(payload.decode("utf-8", "replace")):
                            yield f"data: {line}\n\n"
                else:
                    for line in lines_from(chunk.decode("utf-8", "replace")):
                        yield f"data: {line}\n\n"
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                chunk = b""
                yield ": keep-alive\n\n"          # heartbeat → Flask sees a gone client
                continue
            if not chunk:
                break
        if text:
            yield f"data: {text}\n\n"
        yield "event: end\ndata: done\n\n"
    except OSError as e:
        # Docker socket unreachable / connection reset mid-stream: tell the log
        # drawer instead of tracebacking through werkzeug into our own logs.
        yield f"event: srverror\ndata: {e}\n\n"
    finally:
        try: sock.close()
        except Exception: pass


_LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")


def _mcp_enabled():
    return os.environ.get("ENABLE_MCP", "1").strip().lower() not in ("0", "false", "no")

def _mcp_port():
    try:
        return int(os.environ.get("MCP_PORT", "9810") or 9810)
    except ValueError:
        return 9810

def _mcp_probe(port, timeout=0.5):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False

def build_mcp_status():
    """MCP liveness + recent-activity signal for the dashboard status pill."""
    if not _mcp_enabled():
        return {"enabled": False, "state": "off"}
    import mcp_status as ms
    port = _mcp_port()
    up = _mcp_probe(port)
    raw = ms.read_status()
    in_flight = int(raw.get("in_flight") or 0)
    last_ts = raw.get("last_activity_ts")
    age_s = None
    if last_ts is not None:
        try:
            age_s = max(0, int(time.time() - float(last_ts)))
        except (TypeError, ValueError):
            age_s = None
    if not up:
        state = "down"
    elif in_flight > 0 or (age_s is not None and age_s <= MCP_IDLE_SEC):
        state = "active"
    else:
        state = "idle"
    out = {"enabled": True, "up": up, "state": state, "port": port,
           "active_requests": in_flight}
    if last_ts is not None:
        out["last_activity_ts"] = last_ts
    if age_s is not None:
        out["last_activity_age_s"] = age_s
    return out


def _public_settings():
    """Same as get_settings(), but redacts secrets and reports their presence."""
    s = get_settings()
    out = {k: v for k, v in s.items() if k not in SETTING_SECRETS}
    for k in SETTING_SECRETS:
        out[k + "_set"] = bool(s.get(k))
    return out


_DISK_SCAN, _DISK_SCAN_LOCK = {}, threading.Lock()
_DISK_SCAN_TTL = 900   # reuse a completed scan for 15 min
_DISK_SCAN_TIMEOUT = 600
# Two levels at once (folder + its sub-folders) so the treemap can nest. du
# recurses fully whatever --max-depth says, so asking for depth 2 costs the same
# as depth 1 and only prints more. --one-file-system keeps a scan of / from
# wandering into every bind mount and network share on the box.
_DU_ARGS = ["-b", "--max-depth=2", "--one-file-system"]

def _disk_scan_key(host, path):
    """Cache key. Keyed by host as well as path, or a scan of /var on one machine
    would be served as a scan of /var on the next one you clicked."""
    return ("local" if not host or host == "local" else host, path)

def _safe_scan_path(path):
    """Normalise a client-supplied absolute path, or None if it isn't one we are
    willing to hand to a remote shell. Absolute, no '..' traversal, and no shell
    metacharacters or control bytes — the value is shell-quoted at the call site
    too, so this is the belt to that suspenders."""
    if not path or not isinstance(path, str) or not path.startswith("/"):
        return None
    if "\x00" in path or any(ord(c) < 32 for c in path):
        return None
    norm = os.path.normpath(path)
    if ".." in norm.split("/"):
        return None
    return norm

def _parse_du(stdout, root, hostp=lambda p: p):
    """Turn `du -b --max-depth=2` output into the nested {total, entries} the
    treemap draws. Shared by the local and remote scanners so the two can't drift
    into rendering the same filesystem differently."""
    sizes = {}
    for ln in (stdout or "").splitlines():
        parts = ln.split("\t", 1)
        if len(parts) != 2:
            continue
        try:
            sizes[os.path.normpath(parts[1])] = int(parts[0])
        except ValueError:
            continue
    root = os.path.normpath(root)
    total = sizes.get(root, 0)
    by_parent = {}
    for p, b in sizes.items():
        if p == root:
            continue
        by_parent.setdefault(os.path.dirname(p), []).append((p, b))
    TOP_N, KID_N = 32, 26
    def build(p, b, depth):
        node = {"name": os.path.basename(p) or hostp(p), "path": hostp(p), "bytes": b}
        kids = sorted(by_parent.get(p, []), key=lambda x: -x[1])
        if kids and depth < 2:
            shown = kids[:KID_N]
            cnodes = [build(cp, cb, depth + 1) for cp, cb in shown]
            rest = b - sum(cb for _, cb in shown)
            if rest > max(10 * 1024 * 1024, int(b * 0.02)):
                cnodes.append({"name": "(other)", "path": None, "bytes": rest})
            node["children"] = cnodes
        return node
    tops = sorted(by_parent.get(root, []), key=lambda x: -x[1])[:TOP_N]
    return total, [build(p, b, 1) for p, b in tops]

def _disk_scan_store(key, **fields):
    with _DISK_SCAN_LOCK:
        _DISK_SCAN[key] = {"at": int(time.time()), **fields}

def _safe_host_dir(path):
    """Map a requested absolute HOST path to its location under HOST_ROOT, only
    if it's a real directory. Blocks '..' traversal."""
    if not path or not path.startswith("/"):
        return None
    norm = os.path.normpath(path)
    if ".." in norm.split("/"):
        return None
    base = HOST_ROOT.rstrip("/") if os.path.isdir(HOST_ROOT) else ""
    real = (base + norm) if base else norm
    return real if os.path.isdir(real) else None

def _disk_scan_worker(path, real, key=None):
    key = key or _disk_scan_key("local", path)
    base = HOST_ROOT.rstrip("/") if os.path.isdir(HOST_ROOT) else ""
    def hostp(p):
        return (p[len(base):] or "/") if base and p.startswith(base) else p
    try:
        # `--` ends option parsing so a path can never be read as a du flag.
        # `real` always starts with "/" already, so this is belt-and-suspenders.
        r = subprocess.run(["du", *_DU_ARGS, "--", real],
                           capture_output=True, text=True, timeout=_DISK_SCAN_TIMEOUT)
        total, entries = _parse_du(r.stdout, real, hostp)
        free = None
        try:
            s = os.statvfs(real); free = s.f_bavail * s.f_frsize
        except OSError as e:
            print(f"disk scan statvfs({real}) failed: {e}", flush=True)
        _disk_scan_store(key, state="done", total=total, entries=entries,
                         free=free, error=None)
    except subprocess.TimeoutExpired:
        _disk_scan_store(key, state="error", error="scan timed out — folder too large")
    except Exception as e:
        _disk_scan_store(key, state="error", error=str(e)[:200])


# Remotes are scanned the same way the rest of this project reads them: one ssh
# call, no agent, nothing installed or left behind. `du` and `df` are in coreutils
# on every Linux box the probe already supports.
def _remote_scan_cmd(path):
    q = shlex.quote(path)
    # One round trip for both answers, with a sentinel between them so a `df`
    # that fails can't be misread as du output (or vice versa).
    return (f"du {' '.join(_DU_ARGS)} -- {q} 2>/dev/null; "
            f"echo '---HLM-DF---'; "
            f"df -P -B1 -- {q} 2>/dev/null | tail -1")

def _parse_remote_free(block):
    """Available bytes out of a `df -P -B1` line. POSIX -P guarantees one line
    per filesystem with Available in the 4th column, which is what stops a long
    device name from wrapping and shifting every field."""
    for ln in (block or "").splitlines():
        cols = ln.split()
        if len(cols) >= 4:
            try:
                return int(cols[3])
            except ValueError:
                continue
    return None

def _disk_scan_worker_remote(key, name, path):
    """Scan `path` on a registered remote over the existing SSH channel."""
    try:
        h = next((x for x in list_hosts() if x["name"] == name), None)
        if not h:
            _disk_scan_store(key, state="error", error=f"unknown host: {name}")
            return
        parsed = _parse_ssh_target(h.get("ssh_target"))
        if not parsed:
            _disk_scan_store(key, state="error", error="host has no usable SSH target")
            return
        u, host, port = parsed
        rc, out, err, _ms = _ssh(u, host, port, _remote_scan_cmd(path),
                                 timeout=_DISK_SCAN_TIMEOUT)
        if rc != 0 and not out:
            # du exits non-zero on any unreadable subdirectory, so a non-zero rc
            # with output is a partial scan worth showing — only an empty one is
            # a real failure.
            _disk_scan_store(key, state="error",
                             error=(err or f"ssh exited {rc}")[:200])
            return
        du_block, _, df_block = (out or "").partition("---HLM-DF---")
        total, entries = _parse_du(du_block, path)
        if not entries and not total:
            # An empty result is ambiguous — an unreadable directory and an empty
            # one look identical here — so say so rather than drawing a blank
            # treemap that looks like a finished scan of nothing.
            _disk_scan_store(key, state="error",
                             error=f"no readable directories under {path} "
                                   f"(the SSH user may not have permission)")
            return
        _disk_scan_store(key, state="done", total=total, entries=entries,
                         free=_parse_remote_free(df_block), error=None)
    except Exception as e:
        _disk_scan_store(key, state="error", error=str(e)[:200])


_BRIEF_PALETTE = {
    "dark":  {"bg": "#0d1117", "card": "#161b22", "bd": "#30363d", "sub": "#21262d",
              "tx": "#e6edf3", "mut": "#8b949e", "ok": "#3fb950", "warn": "#d29922",
              "crit": "#f85149", "accent": "#d29922", "inset": "#0d1117"},
    "light": {"bg": "#f6f8fa", "card": "#ffffff", "bd": "#d0d7de", "sub": "#eaeef2",
              "tx": "#1f2328", "mut": "#636c76", "ok": "#1a7f37", "warn": "#9a6700",
              "crit": "#cf222e", "accent": "#9a6700", "inset": "#f6f8fa"},
}

def _he(s):
    """Minimal HTML escape (no new import; the brief never embeds attributes)."""
    return str("" if s is None else s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _brief_channel_ready(s, ch):
    """True when channel `ch` has enough config saved to actually deliver."""
    if ch == "email":    return bool(s.get("email_host") and s.get("email_from") and s.get("email_to"))
    if ch == "discord":  return bool(s.get("discord_webhook_url"))
    if ch == "telegram": return bool(s.get("telegram_token") and s.get("telegram_chat_id"))
    if ch == "ntfy":     return bool(s.get("ntfy_topic"))
    if ch == "slack":    return bool(s.get("slack_webhook_url"))
    if ch == "webhook":  return bool(s.get("webhook_url"))
    return False

def _brief_yesterday_cost():
    """(cost, kwh, currency) for the previous local calendar day, tariff-aware.
    (None, None, currency) when cost tracking is off or there were no samples."""
    ctx = _cost_ctx()
    cur = ctx.get("currency") or "$"
    if not ctx.get("day"):                       # no price configured → cost card hidden
        return None, None, cur
    now = int(time.time())
    lt = time.localtime(now)
    today0 = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    y0 = today0 - 86400
    kwh_per = INTERVAL / 3_600_000.0
    cost = kwh = 0.0
    try:
        from backend.db.repos import system as _sys_repo
        with LOCK:
            for ts, w in _sys_repo.query_samples_for_cost(y0, today0, conn=DB):
                cost += (w or 0) * kwh_per * _price_at(ctx, ts)
                kwh  += (w or 0) * kwh_per
    except Exception:
        return None, None, cur
    if kwh <= 0:
        return None, None, cur
    return round(cost, 2), round(kwh, 2), cur

def _brief_fleet():
    """Hub + registered hosts with a LIVE up/down flag. The hub is always up (we're
    the one rendering). A registered host's online flag comes from the SAME source the
    dashboard fleet table uses — _host_is_online() over the live HOST_DATA poll cache —
    NOT the stored manual-Test result. The old code read last_check.overall, a snapshot
    that never goes stale when a host later drops, so an offline host kept counting as
    up (the "5/5 while one is offline" bug). CPU/RAM detail comes from the live poll;
    a down host shows its last poll error instead."""
    def _cpu_ram(h):
        cpu = round(h.get("cpu", 0) or 0)
        ram_pct = round(h["ram_used"] / h["ram_total"] * 100) if h.get("ram_total") else 0
        return f"{cpu}% CPU · {ram_pct}% RAM"
    out = []
    host = LATEST.get("host") or {}
    out.append({"name": host.get("hostname") or "this host", "up": True, "hub": True,
                "detail": _cpu_ram(host)})
    with HOST_DATA_LOCK:
        for h in list_hosts():
            entry = HOST_DATA.get(h["name"]) or {}
            up = _host_is_online(entry)
            if up:
                detail = _cpu_ram((entry.get("data") or {}).get("host") or {})
            else:
                err = (entry.get("error") or "").strip().splitlines()
                detail = (err[0][:80] if err else "offline — no recent poll")
            out.append({"name": h.get("name", "?"), "up": up, "hub": False, "detail": detail})
    return {"hosts": out, "up": sum(1 for x in out if x["up"]), "total": len(out)}

def _brief_checks():
    """Uptime rollup: counts, any down, and certs expiring within 21 days. Derived
    from a SINGLE uptime_overview() read so the counts and the per-check lists can't
    diverge across two snapshots. The cert list stays empty until per-check TLS
    expiry (#163) lands — the section just hides it."""
    checks = [c for c in uptime_overview().get("checks", []) if c.get("enabled")]
    up = sum(1 for c in checks if c.get("state") == "up")
    down = sum(1 for c in checks if c.get("state") == "down")
    summ = {"total": len(checks), "up": up, "down": down,
            "unknown": len(checks) - up - down,
            "worst_down": next((c.get("label") for c in checks if c.get("state") == "down"), None)}
    downs = [c.get("label", "?") for c in checks if c.get("state") == "down"]
    certs = []
    for c in checks:
        cd = c.get("cert_days_remaining")
        if isinstance(cd, (int, float)) and cd <= 21:
            certs.append((c.get("label", "?"), int(cd)))
    return {"summary": summ, "downs": downs, "certs": certs, "enabled": summ["total"] > 0}

def _brief_events(window=86400, limit=8):
    """Recent rows from the events log (OOM/down/recovery, etc.) over the window."""
    from backend.db.repos import system as _sys_repo
    now = int(time.time())
    try:
        with LOCK:
            rows = _sys_repo.query_events_since(now - window, order_desc=True, limit=limit, conn=DB)
    except Exception:
        rows = []
    return [{"ts": r[0], "service": r[1], "kind": r[2], "detail": r[3]} for r in rows]

def _brief_ai_cost():
    models = [{"model": m.get("model", "?"), "service": m.get("service", "?"), "vram": m.get("vram")}
              for m in (LATEST.get("models") or [])]
    cost, kwh, cur = _brief_yesterday_cost()
    gpus = LATEST.get("gpus") or []
    gpu_name = (gpus[0].get("name") if gpus else None) or ((LATEST.get("host") or {}).get("hw") or {}).get("gpu_name")
    return {"available": bool(LATEST.get("gpu_avail")), "gpu_name": gpu_name,
            "temp": LATEST.get("temp"), "util": LATEST.get("util"),
            "mem_used": LATEST.get("mem_used"), "mem_total": LATEST.get("mem_total"),
            "models": models, "cost": cost, "kwh": kwh, "currency": cur}

def _brief_capacity():
    """Disks at/above 80% (trending full) + an idle-VRAM squatter when the GPU sits idle."""
    out = []
    host = LATEST.get("host") or {}
    for d in (host.get("disks") or []):
        if (d.get("pct") or 0) >= 80:
            out.append(("💾", f"{d.get('mount', '?')} at {round(d.get('pct', 0))}%"))
    if (LATEST.get("util") or 0) < 5:
        for m in (LATEST.get("models") or []):
            if (m.get("vram") or 0) >= 256:
                out.append(("🅿️", f"{m.get('service', '?')} holding {round(m['vram'])} MB while the GPU is idle"))
                break
    return out

def _brief_assemble():
    """Gather every section's data + derive the headline / action list. One dict."""
    gpu_avail = LATEST.get("gpu_avail")
    now = {"gpu": {"util": LATEST.get("util"), "mem_used": LATEST.get("mem_used"),
                   "mem_total": (LATEST.get("mem_total") or 24576) if gpu_avail else 0,
                   "power": LATEST.get("power"), "temp": LATEST.get("temp"),
                   "available": bool(gpu_avail)},
           "host": LATEST.get("host") or {}}
    docker  = HEALTH.get("docker")  or {"available": False, "summary": {"total": 0, "running": 0, "problems": 0}, "containers": []}
    systemd = HEALTH.get("systemd") or {"available": False, "summary": {}, "services": []}
    overview = build_overview(now, docker, systemd)
    fleet    = _brief_fleet()
    checks   = _brief_checks()
    events   = _brief_events()
    ai       = _brief_ai_cost()
    capacity = _brief_capacity()

    actions = []
    for x in fleet["hosts"]:
        if not x["up"]:
            actions.append((2, f"Host {x['name']} is offline"))
    for lbl in checks["downs"]:
        actions.append((2, f"Check “{lbl}” is down"))
    for lbl, d in checks["certs"]:
        actions.append((1 if d > 7 else 2, f"TLS cert {lbl} expires in {d} day{'s' if d != 1 else ''}"))
    # Name the actual problem containers / units instead of a bare "N failed" — the
    # whole point of an action line is to say which thing needs you.
    for ct in (docker.get("containers") or []):
        if ct.get("status") in ("crit", "warn"):
            st = (ct.get("status_text") or ct.get("label") or "unhealthy").strip()
            actions.append((2 if ct["status"] == "crit" else 1,
                            f"Container {ct.get('name', '?')} — {st}"))
    for sv in (systemd.get("services") or []):
        if sv.get("status") == "crit":
            ex = sv.get("exit_status")
            tail = f" (exit {ex})" if isinstance(ex, int) else ""
            actions.append((2, f"systemd unit {sv.get('name', '?')} failed{tail}"))
    # Remaining overview cards (GPU/host/…) — containers & services are already
    # itemised by name above, so skip their roll-up cards to avoid "1 failed" dupes.
    for c in overview:
        if c.get("key") in ("containers", "services"):
            continue
        if c["status"] in ("crit", "warn"):
            actions.append((2 if c["status"] == "crit" else 1, f"{c['label']}: {c['detail']}"))
    crit = any(p == 2 for p, _ in actions)
    actions_text = [t for _, t in sorted(actions, key=lambda a: -a[0])]

    return {"overview": overview, "fleet": fleet, "checks": checks, "events": events,
            "ai": ai, "capacity": capacity, "actions": actions_text,
            "issues": len(actions_text), "crit": crit, "now": int(time.time())}

def render_brief(theme="dark"):
    """Return (html, summary, subject, level). `html` is a self-contained inline-styled
    body used for BOTH the email and the Discord HTML attachment; `summary` is the
    compact, emoji-free text for chat channels; `subject` is the title; `level` is the
    severity ("critical"/"warning"/"info") so chat channels colour the message by how
    bad it is instead of always showing info-blue. No emoji anywhere: status is carried
    by colour (CSS dots, the embed stripe) and quiet uppercase labels."""
    P = _BRIEF_PALETTE["light"] if theme == "light" else _BRIEF_PALETTE["dark"]
    d = _brief_assemble()
    hub = _alert_host_label() or "homelab"
    when = time.strftime("%a %d %b", time.localtime(d["now"]))
    issues, crit = d["issues"], d["crit"]
    fleet, checks, ai = d["fleet"], d["checks"], d["ai"]
    level = "critical" if crit else ("warning" if issues else "info")

    if issues == 0:
        head_text, head_col = "All systems healthy", P["ok"]
        head_sub = "nothing needs you today"
    else:
        head_text = f"{issues} thing{'' if issues == 1 else 's'} {'needs' if issues == 1 else 'need'} you"
        head_col = P["crit"] if crit else P["warn"]
        head_sub = "otherwise the lab is healthy"

    cost_kpi = f"{ai['cost']}" if ai["cost"] is not None else "—"
    cur = ai["currency"]
    kpis = [(f"{fleet['up']}/{fleet['total']}", "Hosts up", P["ok"] if fleet["up"] == fleet["total"] else P["warn"]),
            ((f"{checks['summary']['up']}/{checks['summary']['total']}" if checks["enabled"] else "—"),
             "Checks up", P["ok"] if checks["enabled"] and not checks["downs"] else (P["warn"] if checks["downs"] else P["mut"])),
            (str(issues), "To look at", head_col if issues else P["ok"]),
            (cost_kpi, f"{cur} ydy", P["tx"])]

    # ── HTML assembly (table layout, inline styles only) ──────────────────────
    def head(t):
        return (f'<tr><td style="padding:18px 24px 6px"><div style="font-size:12px;text-transform:uppercase;'
                f'letter-spacing:.06em;color:{P["accent"]};font-weight:600">{t}</div></td></tr>')
    def rows_table(lines):
        body = "".join(f'<tr><td style="padding:6px 0;border-bottom:1px solid {P["sub"]};'
                       f'font-size:14px;color:{P["tx"]}">{ln}</td></tr>' for ln in lines)
        return (f'<tr><td style="padding:0 24px 6px"><table role="presentation" width="100%" '
                f'cellpadding="0" cellspacing="0">{body}</table></td></tr>')
    def dot(col):
        """A small coloured status bullet — the no-emoji replacement for 🟢/🔴/🟠."""
        return f'<span style="color:{col};font-size:11px;vertical-align:middle">&#9679;</span>'

    parts = []
    parts.append(f'<tr><td style="padding:20px 24px;border-bottom:1px solid {P["bd"]}">'
                 f'<table role="presentation" width="100%"><tr>'
                 f'<td style="font-size:18px;font-weight:700;color:{P["tx"]}">Daily brief</td>'
                 f'<td align="right" style="font-size:13px;color:{P["mut"]}">{when} · {_he(hub)}</td>'
                 f'</tr></table></td></tr>')
    # KPI strip
    tiles = "".join(
        f'<td width="25%" style="padding:11px 6px;text-align:center;background:{P["inset"]};'
        f'border:1px solid {P["sub"]};border-radius:8px">'
        f'<div style="font-size:20px;font-weight:700;color:{col}">{_he(val)}</div>'
        f'<div style="font-size:10px;color:{P["mut"]};text-transform:uppercase;letter-spacing:.05em">{_he(lbl)}</div></td>'
        for val, lbl, col in kpis)
    parts.append(f'<tr><td style="padding:16px 24px 2px"><table role="presentation" width="100%" '
                 f'cellpadding="0" cellspacing="6"><tr>{tiles}</tr></table></td></tr>')
    # headline — severity shown by the coloured left border + tint, not an emoji
    parts.append(f'<tr><td style="padding:18px 24px"><table role="presentation" width="100%" '
                 f'style="background:{head_col}1a;border:1px solid {head_col};'
                 f'border-left:4px solid {head_col};border-radius:10px">'
                 f'<tr><td style="padding:14px 16px;font-size:16px;color:{P["tx"]}">'
                 f'<b>{_he(head_text)}</b> '
                 f'&nbsp;<span style="color:{P["mut"]}">· {head_sub}</span></td></tr></table></td></tr>')
    # action needed
    if d["actions"]:
        parts.append(head("Action needed"))
        parts.append(rows_table([_he(a) for a in d["actions"]]))
    # fleet
    parts.append(head("Fleet"))
    fleet_lines = []
    for x in fleet["hosts"]:
        extra = f' <span style="color:{P["mut"]}">· {_he(x["detail"])}</span>' if x["detail"] else ""
        tag = " (hub)" if x["hub"] else ""
        fleet_lines.append(f'{dot(P["ok"] if x["up"] else P["crit"])} {_he(x["name"])}{tag}{extra}')
    parts.append(rows_table(fleet_lines))
    # checks
    if checks["enabled"]:
        parts.append(head("Checks"))
        sm = checks["summary"]
        clines = [f'{dot(P["ok"] if not checks["downs"] else P["crit"])} {sm["up"]}/{sm["total"]} checks responding']
        for lbl in checks["downs"]:
            clines.append(f'{dot(P["crit"])} {_he(lbl)} is down')
        for lbl, dd in checks["certs"]:
            clines.append(f'{dot(P["warn"])} cert {_he(lbl)} expires in {dd} day{"s" if dd != 1 else ""}')
        parts.append(rows_table(clines))
    # alerts (events) last 24h
    if d["events"]:
        parts.append(head("Alerts · last 24h"))
        elines = []
        for e in d["events"]:
            t = time.strftime("%H:%M", time.localtime(e["ts"]))
            who = e.get("service") or e.get("kind") or "event"
            elines.append(f'{_he(t)} — {_he(who)}: {_he(e.get("detail") or e.get("kind") or "")}')
        parts.append(rows_table(elines))
    # ai & cost
    parts.append(head("AI &amp; cost"))
    ailines = []
    if ai["available"]:
        gname = ai["gpu_name"] or "GPU"
        t = f' · {round(ai["temp"])}°C' if ai.get("temp") is not None else ""
        ailines.append(f'{_he(gname)}{t} · {len(ai["models"])} model{"s" if len(ai["models"]) != 1 else ""} loaded')
    else:
        ailines.append("No GPU detected on the hub")
    if ai["cost"] is not None:
        ailines.append(f'Yesterday\'s energy: <b>{ai["cost"]} {_he(cur)}</b> · {ai["kwh"]} kWh')
    if ai["models"]:
        names = ", ".join(_he(m["model"]) for m in ai["models"][:4])
        ailines.append(f'<span style="color:{P["mut"]}">models: {names}</span>')
    parts.append(rows_table(ailines))
    # capacity nudges
    if d["capacity"]:
        parts.append(head("Capacity nudges"))
        parts.append(rows_table([_he(txt) for _ic, txt in d["capacity"]]))
    # footer — no CTA link: a brief has no absolute dashboard URL to point at, and
    # a fragment-only href="#" is stripped/dead in Gmail/Outlook/Apple Mail. The
    # footer line tells the reader where the brief is configured instead.
    parts.append(f'<tr><td style="padding:14px 24px;border-top:1px solid {P["bd"]};font-size:12px;color:{P["mut"]}">'
                 f'HomeLab Monitor v{VERSION} · daily brief from {_he(hub)} · configure in Settings → Alerts</td></tr>')

    html = (f'<!DOCTYPE html><html><body style="margin:0;background:{P["bg"]};padding:24px 0;'
            f'font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">'
            f'<table role="presentation" width="600" align="center" cellpadding="0" cellspacing="0" '
            f'style="width:600px;max-width:92%;margin:0 auto;background:{P["card"]};'
            f'border:1px solid {P["bd"]};border-radius:12px;overflow:hidden">'
            f'{"".join(parts)}</table></body></html>')

    # ── compact, emoji-free text summary for chat channels ────────────────────
    # Does NOT repeat the headline: the channel title (subject) already carries it,
    # so the body leads with the sub-line, then the named actions, then a stat strip.
    slines = [head_sub]
    if d["actions"]:
        slines += ["", "ACTION NEEDED"]
        slines += [f'• {a}' for a in d["actions"][:6]]
    stat = f'Hosts {fleet["up"]}/{fleet["total"]}'
    if checks["enabled"]:
        stat += f' · Checks {checks["summary"]["up"]}/{checks["summary"]["total"]}'
    if d["events"]:
        stat += f' · {len(d["events"])} event{"s" if len(d["events"]) != 1 else ""}/24h'
    if ai["cost"] is not None:
        stat += f' · {ai["cost"]} {cur} ydy'
    slines += ["", stat]
    summary = "\n".join(slines)

    subject = f"[{hub}] Daily brief — {head_text}"
    return html, summary, subject, level

def _send_brief_email(s, subject, html, text):
    """Send the brief as a multipart email: plain-text fallback + HTML body."""
    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s["email_from"]
    msg["To"] = s["email_to"]
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    _smtp_send(msg, s["email_host"], s.get("email_port", "587") or 587,
               s.get("email_use_tls", "1") == "1",
               s.get("email_username"), s.get("email_password"))

def send_brief(s, channel):
    """Render and deliver the brief to one channel. Raises on delivery failure.
    The severity `level` (not a hardcoded "info") drives each channel's colour/priority,
    and Discord gets the full HTML attached, not just the text summary."""
    html, summary, subject, level = render_brief(s.get("brief_theme", "dark"))
    fname = f"brief-{time.strftime('%Y-%m-%d')}.html"
    if channel == "email":
        _send_brief_email(s, subject, html, summary)
    elif channel == "discord":
        send_discord_brief(s.get("discord_webhook_url"), level, subject, summary, html, fname)
    elif channel == "telegram":
        _post_to_telegram(s.get("telegram_token"), s.get("telegram_chat_id"), level, subject, summary)
    elif channel == "ntfy":
        send_ntfy(s.get("ntfy_server") or "https://ntfy.sh", s.get("ntfy_topic"), level, subject, summary)
    elif channel == "slack":
        send_slack(s.get("slack_webhook_url"), level, subject, summary)
    elif channel == "webhook":
        send_webhook(s.get("webhook_url"), level, subject, summary, _alert_host_label() or "")
    else:
        raise ValueError(f"unknown brief channel: {channel}")

_BRIEF_LAST_SENT = {"date": None}

def _brief_due(now=None):
    """Return (settings, channel, date_str) when a brief should fire right now, else None.
    Fires once per local day at the configured HH:MM, only if enabled and the chosen
    channel is configured."""
    s = get_settings()
    if s.get("brief_enabled") != "1":
        return None
    ch = s.get("brief_channel") or ""
    if not ch or not _brief_channel_ready(s, ch):
        return None
    lt = time.localtime(now or time.time())
    if time.strftime("%H:%M", lt) != (s.get("brief_time") or "08:00"):
        return None
    today = time.strftime("%Y-%m-%d", lt)
    if _BRIEF_LAST_SENT["date"] == today:
        return None
    return s, ch, today

def _brief_run_once(now=None):
    """One scheduler pass. Returns True if a brief was due (and attempted). The day
    is claimed *before* the send so a transient failure can't trigger a same-minute
    retry / duplicate delivery — a daily digest prefers a single best-effort send
    over risking two."""
    due = _brief_due(now)
    if not due:
        return False
    s, ch, today = due
    _BRIEF_LAST_SENT["date"] = today
    try:
        send_brief(s, ch)
        print(f"daily brief sent to {ch}", flush=True)
    except Exception as e:
        print("brief send error:", e, flush=True)
    return True

# Phase 3.2: moved to backend/collectors/ — re-exported for backward compat
from backend.collectors import brief_worker
from backend.collectors import watchdog as _collector_watchdog
from backend.collectors import fast_sampler, fast_sample_once


if "pytest" not in sys.modules:
    threading.Thread(target=collector, daemon=True).start()
    threading.Thread(target=fast_sampler, daemon=True).start()
    threading.Thread(target=host_poller, daemon=True).start()
    threading.Thread(target=uptime_worker, daemon=True).start()
    threading.Thread(target=brief_worker, daemon=True).start()
    threading.Thread(target=_collector_watchdog, daemon=True).start()


import os as _os

# ── Public status page: monitors (uptime checks marked public) ────────────────
# A check appears on the public page only when it is BOTH public=1 and enabled.
# The index carries a sanitized summary; each service has a fuller read-only
# detail (uptime windows, daily history, response series, incidents, cert). We
# never surface the raw target with credentials — only the host (+ port for tcp).

def _public_monitor_host(check):
    """A display target safe for a public page: the host (and port for tcp),
    never the path/query or any user:pass credentials."""
    t = check.get("target") or ""
    try:
        if check.get("type") == "tcp":
            host, port = _parse_host_port(t)
            return f"{host}:{port}" if host else ""
        u = urllib.parse.urlsplit(t)
        host = u.hostname or ""
        return f"{host}:{u.port}" if u.port else host
    except Exception:
        return ""

def _public_monitor(check, now):
    """One public component for the status page — a single 90-day query yields
    everything the Statuspage-style row needs: current state, 24h/7d/90d uptime,
    a day-by-day bar, the recent heartbeat, cert status, and recent incidents.
    No raw target (host only)."""
    from backend.db.repos import uptime as _uptime_repo
    cid = check["id"]
    with LOCK:
        rows = _uptime_repo.results_since_full(cid, now - 7776000, conn=DB)  # 90 days
    def win(w):
        sub = [r for r in rows if r[0] >= now - w]
        return round(100.0 * sum(1 for r in sub if r[1]) / len(sub), 2) if sub else None
    last = rows[-1] if rows else None
    state = "unknown" if not last else ("up" if last[1] else "down")
    cert_days = last[5] if (last and len(last) > 5) else None
    cert_status = None
    if cert_days is not None:
        cert_status = "red" if cert_days <= 7 else "amber" if cert_days <= 21 else "ok"
    in_maint = _in_maintenance("uptime", cid) or _in_maintenance("uptime", check.get("label", ""))
    return {
        "id": cid, "label": check["label"], "type": check["type"],
        "host": _public_monitor_host(check), "state": state,
        "in_maintenance": in_maint,
        "last_latency_ms": (last[2] if last else None),
        "last_checked": (last[0] if last else None),
        "uptime": win(86400), "uptime7": win(604800), "uptime90": win(7776000),
        "up_since": _uptime_up_since(rows),
        "daily": _uptime_daily(rows),
        "strip": [{"up": bool(r[1]), "t": r[0]} for r in rows[-_UPTIME_STRIP_CELLS:]],
        "incidents": _uptime_incidents(rows, cap=5),
        "cert_days_remaining": cert_days, "cert_status": cert_status,
    }

def _public_monitors(now):
    """Every public+enabled check as a status-page component, in display order."""
    return [_public_monitor(c, now) for c in list_uptime_checks()
            if c.get("public") and c.get("enabled")]

def _public_monitors_summary(monitors):
    return {"total": len(monitors),
            "up": sum(1 for m in monitors if m["state"] == "up"),
            "down": sum(1 for m in monitors if m["state"] == "down")}

def _public_incident_feed(monitors, now, days=14, cap=25):
    """Recent incidents across all public components, tagged with the service
    name, most-recent first — drives the page's 'Past incidents' timeline."""
    cutoff = now - days * 86400
    feed = []
    for m in monitors:
        for inc in m.get("incidents", []):
            if inc["start"] >= cutoff or (inc.get("end") and inc["end"] >= cutoff):
                feed.append({"service": m["label"], **inc})
    feed.sort(key=lambda i: i["start"], reverse=True)
    return feed[:cap]

def _uptime_window_pct(check_id, now, window):
    from backend.db.repos import uptime as _uptime_repo
    with LOCK:
        agg = _uptime_repo.results_window_agg(check_id, now - window, conn=DB)
    tot = (agg[0] or 0) if agg else 0
    return round(100.0 * (agg[1] or 0) / tot, 2) if tot else None

def _uptime_up_since(rows):
    """From ascending [(ts,up,…)] rows, the ts the current contiguous up-run began
    (None if currently down/unknown or there's no data)."""
    if not rows or not rows[-1][1]:
        return None
    since = rows[-1][0]
    for r in reversed(rows):
        if not r[1]:
            break
        since = r[0]
    return since

def _uptime_incidents(rows, cap=20):
    """Reconstruct down-periods from ascending result rows. Each incident:
    {start, end|None, duration_s|None, err}. Most-recent first, capped."""
    incidents, cur = [], None
    for r in rows:
        ts, up, err = r[0], r[1], (r[4] if len(r) > 4 else None)
        if not up and cur is None:
            cur = {"start": ts, "end": None, "duration_s": None, "err": err or None}
        elif up and cur is not None:
            cur["end"] = ts; cur["duration_s"] = ts - cur["start"]
            incidents.append(cur); cur = None
    if cur is not None:                      # still down at the end of the window
        incidents.append(cur)
    incidents.reverse()
    return incidents[:cap]

def _uptime_response_series(rows, max_points=120):
    """Downsample up-result latencies to ~max_points {t, ms} for a sparkline."""
    pts = [(r[0], r[2]) for r in rows if r[1] and r[2] is not None]
    if len(pts) > max_points:
        step = len(pts) / max_points
        pts = [pts[int(i * step)] for i in range(max_points)]
    return [{"t": t, "ms": round(ms)} for t, ms in pts]

def _uptime_daily(rows, days=90):
    """Bucket ascending result rows into per-day {date, up_pct, state} cells
    (oldest→newest), only for days that actually have samples (≤ days)."""
    DAY = 86400
    buckets = {}
    for r in rows:
        d = r[0] - (r[0] % DAY)
        b = buckets.setdefault(d, [0, 0])
        b[0] += 1; b[1] += 1 if r[1] else 0
    out = []
    for d in sorted(buckets)[-days:]:
        tot, upn = buckets[d]
        pct = round(100.0 * upn / tot, 2) if tot else None
        state = "up" if pct == 100 else ("down" if (pct is not None and pct < 100) else "unknown")
        out.append({"date": d, "up_pct": pct, "state": state})
    return out

def _public_status_detail(cid, now):
    """Full read-only detail for one public service, or None if the check isn't
    public+enabled. Windowed over the retained samples (up to 90 days)."""
    check = next((c for c in list_uptime_checks() if c["id"] == cid), None)
    if not check or not (check.get("public") and check.get("enabled")):
        return None
    s = _uptime_state(check["id"], now)
    from backend.db.repos import uptime as _uptime_repo
    with LOCK:
        rows90 = _uptime_repo.results_since_full(check["id"], now - 7776000, conn=DB)  # 90 days
    rows24 = [r for r in rows90 if r[0] >= now - 86400]
    return {
        "id": check["id"], "label": check["label"], "type": check["type"],
        "host": _public_monitor_host(check),
        "state": s["state"], "last_latency_ms": s["last_latency_ms"],
        "last_checked": s["last_checked"], "interval_sec": check["interval_sec"],
        "up_since": _uptime_up_since(rows90),
        "uptime": {
            "24h": _uptime_window_pct(check["id"], now, 86400),
            "7d":  _uptime_window_pct(check["id"], now, 604800),
            "30d": _uptime_window_pct(check["id"], now, 2592000),
            "90d": _uptime_window_pct(check["id"], now, 7776000),
        },
        "daily": _uptime_daily(rows90),
        "response_series": _uptime_response_series(rows24),
        "incidents": _uptime_incidents(rows90),
        "cert_days_remaining": s["cert_days_remaining"],
        "cert_expires_at": s["cert_expires_at"], "cert_status": s["cert_status"],
    }

def _public_overall_status(cards, monitors):
    """ok only when every overview card is ok and no public monitor is down;
    crit if a monitor is down; maintenance if nothing is down but something
    is covered by an active maintenance window; warn otherwise."""
    if any(m["state"] == "down" for m in monitors):
        return "crit"
    if all(c.get("status") == "ok" for c in cards):
        if any(m.get("in_maintenance") for m in monitors):
            return "maintenance"
        return "ok"
    return "warn"

def _public_status_enabled():
    """On if either the PUBLIC_STATUS env var or the Settings toggle is set --
    env var kept for backward compatibility, Settings toggle needs no restart."""
    return bool(_os.environ.get("PUBLIC_STATUS")) or get_settings().get("public_status_enabled") == "1"


if __name__ == "__main__":
    print(
        f"\n  HomeLab Monitor v{VERSION}\n"
        f"      Open  ->  http://localhost:{PORT}   (or http://<this-host-ip>:{PORT} over your LAN/VPN)\n"
        f"      Like it? A star on GitHub helps other home-labbers find it:\n"
        f"      https://github.com/SikamikanikoBG/homelab-monitor\n",
        flush=True,
    )
    app.run(host="0.0.0.0", port=PORT, threaded=True)
