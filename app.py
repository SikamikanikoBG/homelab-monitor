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
import os, re, sys, glob, time, json, ssl, math, socket, calendar, sqlite3, threading, queue, subprocess, http.client, urllib.parse, urllib.request, urllib.error, ipaddress, shlex, struct, shutil, tempfile, secrets, hmac, uuid, hashlib, smtplib, fnmatch, csv, io
import email.message, email.utils
import html as _html
from functools import wraps
try:
    import fcntl                       # Linux-only; used for per-iface IPv4 (SIOCGIFADDR)
except ImportError:
    fcntl = None
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, Response, send_file, send_from_directory, after_this_request, g, stream_with_context
import db_backup
try:
    from prometheus_client import (Gauge, generate_latest, CONTENT_TYPE_LATEST,
                                   REGISTRY, CollectorRegistry)
    _PROM_OK = True
except ImportError:
    _PROM_OK = False

VERSION      = "0.18.0-ai"
DB_PATH      = os.environ.get("DB_PATH", "/data/gpu.db")
MCP_IDLE_SEC = 45   # seconds without MCP activity before the pill shows idle
INTERVAL     = int(os.environ.get("SAMPLE_INTERVAL", "10"))
RETENTION    = int(os.environ.get("RETENTION_DAYS", "180")) * 86400
_DISK_IO_RETENTION = 7 * 86400   # per-device disk-I/O history: dense, 7-day ring
_PROC_IO_RETENTION = 72 * 3600   # per-process I/O ring: short, spike-attribution only
DOCKER_SOCK  = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
HOST_ROOT    = os.environ.get("HOST_ROOT", "/rootfs")          # host / mounted read-only (optional)
PORT         = int(os.environ.get("PORT", "9800"))
PRESSURE_MB  = int(os.environ.get("PRESSURE_FREE_MB", "2048"))
# Demo mode (E4): on a *fresh* DB, seed ~7 days of realistic synthetic history so
# every history-backed feature (disk-fill ETA, cost projection, z-score anomalies,
# VRAM-ETA, Lab Copilot digest) lights up within seconds — for the README CTA and
# the judgment-day demo. OFF by default; idempotent (guarded on a marker + row
# counts) so it never clobbers real data and a real instance is wholly unaffected.
DEMO_MODE     = os.environ.get("DEMO_MODE", "").strip().lower() in ("1", "true", "yes", "on")
# Public read-only status page (E4) — Uptime-Kuma's shareable "is my lab up?"
# surface. Served at GET /status (HTML) backed by GET /api/status (JSON). It is
# strictly read-only and deliberately leaks no topology: only aggregated, public-
# safe health (overall banner, per-subsystem up/down tiles, anonymized counts,
# uptime, GPU busy/idle). ON by default — it exposes nothing sensitive — but can be
# disabled with STATUS_PAGE=0 for operators who want zero unauthenticated surface.
STATUS_PAGE   = os.environ.get("STATUS_PAGE", "1").strip().lower() not in ("0", "false", "no", "off")
CHECK_UPDATES = os.environ.get("CHECK_UPDATES", "true").strip().lower() not in ("0", "false", "no", "off")
# Per-host "a newer OS release exists" check. The probe reads pending *package*
# updates offline; detecting a whole new distro release needs the network, so the
# hub does it centrally (endoflife.date). Air-gapped users can switch off just
# this network lookup and keep the offline package counts.
CHECK_OS_UPDATES = os.environ.get("CHECK_OS_UPDATES", "true").strip().lower() not in ("0", "false", "no", "off")
UPDATE_REPO   = os.environ.get("UPDATE_REPO", "SikamikanikoBG/homelab-monitor")
# Opt-in one-click self-update button. OFF by default: this is the first action
# that *writes* (it recreates this very container via a detached docker:cli helper
# and restarts the app). Needs the docker socket mounted read-write. See
# start_self_update() and website/configuration.md.
ALLOW_SELF_UPDATE = os.environ.get("ALLOW_SELF_UPDATE", "").strip().lower() in ("1", "true", "yes", "on")
SELF_UPDATE_HELPER_IMAGE = os.environ.get("SELF_UPDATE_HELPER_IMAGE", "docker:cli")
# Opt-in container/service controls (start/stop/restart). OFF by default: this is
# the ONLY surface that mutates the host's runtime state (docker start/stop, unit
# start/stop). With it unset/false the control endpoints are hard-disabled — they
# return a clean 403 and never touch docker or D-Bus. The live arena runs OFF.
# Every mutation validates its target against the set the monitor already
# enumerates (no free-form names, no shell) — see api_container_action /
# api_service_action. This monitor can run with host mounts, which is exactly why
# default-OFF + opt-in + target-validation is load-bearing.
ENABLE_CONTROLS = os.environ.get("ENABLE_CONTROLS", "").strip().lower() in ("1", "true", "yes", "on")
CONTROL_ACTIONS = ("start", "stop", "restart")
# Controls audit log: keep at most this many rows (append-only ring). A control
# action is a rare, deliberate event — a few hundred rows is plenty of history
# and keeps the table unconditionally bounded (pruned on every write).
_CONTROL_AUDIT_RETENTION = 500
# Split cache: once we know there's an update, the answer won't change for hours
# so we can cache it long. But "no update found" / network errors should expire
# sooner — otherwise a release published right after deploy stays invisible for
# the full 6h, and a transient GitHub blip sticks for the same window.
UPDATE_TTL_POSITIVE = 6 * 3600
UPDATE_TTL_NEGATIVE = 10 * 60   # re-check for a new release every 10 min (was 30)
MAX_POINTS   = 360
# ── Lab Copilot (E1): local-LLM insight layer over the monitor's own data ─────
# Talks to an ollama-compatible HTTP API already running on the host (host
# networking → 127.0.0.1:11434 by default). All read-only: it summarises metrics
# the app already computes, never mutates anything. Degrades gracefully when the
# LLM is unreachable / no model is pulled — the feature shows a clear "not
# configured" state, never a 500. Endpoint + model + timeout are env-configurable.
COPILOT_OLLAMA_URL = os.environ.get("COPILOT_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
COPILOT_MODEL      = os.environ.get("COPILOT_MODEL", "gemma3:1b")  # small, fast, usually pulled
COPILOT_TIMEOUT    = float(os.environ.get("COPILOT_TIMEOUT", "30"))  # seconds, hard cap per call
COPILOT_ENABLED    = os.environ.get("COPILOT_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
# Proactive Recommendations panel (E1): deterministic detectors over the live
# signals (forecast/anomaly/cost/incident/uptime/OOM) emit a ranked, actionable
# to-do list. Pure rules — the LLM only OPTIONALLY phrases them; the panel always
# works with ollama off. Thresholds are env-tunable with safe defaults.
RECO_DISK_CRIT_DAYS  = float(os.environ.get("RECO_DISK_CRIT_DAYS", "14"))   # disk fills < this → crit
RECO_DISK_WARN_DAYS  = float(os.environ.get("RECO_DISK_WARN_DAYS", "60"))   # disk fills < this → warn
RECO_VRAM_WARN_GB    = float(os.environ.get("RECO_VRAM_WARN_GB", "2"))      # VRAM headroom < this → warn
RECO_VRAM_CRIT_GB    = float(os.environ.get("RECO_VRAM_CRIT_GB", "0.75"))   # VRAM headroom < this → crit
RECO_COST_WARN_PCT   = int(os.environ.get("RECO_COST_WARN_PCT", "25"))      # projected +% vs last month → warn
RECO_COST_CRIT_PCT   = int(os.environ.get("RECO_COST_CRIT_PCT", "50"))      # projected +% vs last month → crit
RECO_OOM_WARN_N      = int(os.environ.get("RECO_OOM_WARN_N", "1"))          # OOM kills in window → warn
RECO_OOM_CRIT_N      = int(os.environ.get("RECO_OOM_CRIT_N", "3"))          # OOM kills in window → crit
RECO_OOM_WINDOW_DAYS = float(os.environ.get("RECO_OOM_WINDOW_DAYS", "7"))   # OOM look-back window
RECO_MAX_ITEMS       = int(os.environ.get("RECO_MAX_ITEMS", "6"))           # cap the list
# Image-update awareness (What's-Up-Docker style). OFF by default (see settings).
# All env-tunable so the unattended checker stays bounded + polite to registries.
IMG_CHECK_INTERVAL_DEFAULT  = int(os.environ.get("IMG_CHECK_INTERVAL_SEC", "21600"))  # ~6h between full re-checks
IMG_CHECK_INTERVAL_MIN      = int(os.environ.get("IMG_CHECK_INTERVAL_MIN_SEC", "3600"))  # never re-check faster than ~1h
IMG_CHECK_MAX_PER_CYCLE     = int(os.environ.get("IMG_CHECK_MAX_PER_CYCLE", "40"))    # cap containers queried per cycle
IMG_CHECK_RATELIMIT_BACKOFF = int(os.environ.get("IMG_CHECK_RATELIMIT_BACKOFF", "3600"))  # back off ~1h on a 429
RECO_IMG_UPDATES_INFO_N     = int(os.environ.get("RECO_IMG_UPDATES_INFO_N", "1"))     # N updates available → info reco
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

# ── Prometheus gauges (defined once at module level) ──────────────────────────
if _PROM_OK:
    _G = {
        "gpu_vram_used":     Gauge("homelab_gpu_vram_used_mb",    "GPU VRAM used (MB)",                ["gpu"]),
        "gpu_vram_total":    Gauge("homelab_gpu_vram_total_mb",   "GPU VRAM total (MB)",               ["gpu"]),
        "gpu_util":          Gauge("homelab_gpu_util_pct",        "GPU utilisation (%)",               ["gpu"]),
        "gpu_temp":          Gauge("homelab_gpu_temp_c",          "GPU temperature (°C)",              ["gpu"]),
        "gpu_power":         Gauge("homelab_gpu_power_w",         "GPU power draw (W)",                ["gpu"]),
        "host_cpu":          Gauge("homelab_host_cpu_pct",        "Host CPU usage (%)"),
        "host_mem_used":     Gauge("homelab_host_mem_used_pct",   "Host memory used (%)"),
        "host_disk_used":    Gauge("homelab_host_disk_used_pct",  "Host disk used (%)",                ["mountpoint"]),
        "container_state":   Gauge("homelab_container_state",     "Container state (1=running)",       ["name", "state"]),
        "systemd_unit":      Gauge("homelab_systemd_unit_state",  "Systemd unit state (1=active)",     ["unit",  "state"]),
        "model_vram":        Gauge("homelab_model_loaded_vram_mb","Model VRAM loaded (MB)",             ["server", "model"]),
    }
LOCK = threading.Lock()
_DB_MAINTENANCE = False   # True during backup/restore — collector skips DB writes
_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples(ts INTEGER PRIMARY KEY, util REAL, mem_used REAL, mem_total REAL, power REAL, temp REAL);
CREATE TABLE IF NOT EXISTS gpu_samples(ts INTEGER, idx INTEGER, util REAL, mem_used REAL, mem_total REAL, power REAL, temp REAL);
CREATE TABLE IF NOT EXISTS net_samples(ts INTEGER, iface TEXT, bytes_in INTEGER, bytes_out INTEGER);
CREATE TABLE IF NOT EXISTS proc(ts INTEGER, service TEXT, mem REAL);
CREATE TABLE IF NOT EXISTS models(ts INTEGER, service TEXT, model TEXT, vram REAL);
CREATE TABLE IF NOT EXISTS edges(ts INTEGER, caller TEXT, server TEXT, conns INTEGER);
CREATE TABLE IF NOT EXISTS events(ts INTEGER, service TEXT, kind TEXT, detail TEXT);
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
CREATE TABLE IF NOT EXISTS disk_samples(ts INTEGER NOT NULL, mount TEXT NOT NULL, used REAL, total REAL);
CREATE INDEX IF NOT EXISTS idx_disk_ts ON disk_samples(mount, ts);
CREATE TABLE IF NOT EXISTS disk_io_samples(ts INTEGER NOT NULL, device TEXT NOT NULL, read_mb_s REAL, write_mb_s REAL, util_pct REAL);
CREATE INDEX IF NOT EXISTS idx_diskio_ts ON disk_io_samples(device, ts);
CREATE TABLE IF NOT EXISTS proc_io_samples(ts INTEGER NOT NULL, pid INTEGER, comm TEXT, read_bps INTEGER, write_bps INTEGER);
CREATE INDEX IF NOT EXISTS idx_procio_ts ON proc_io_samples(ts);
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
CREATE INDEX IF NOT EXISTS idx_powerproc_ts   ON power_proc(ts);
CREATE INDEX IF NOT EXISTS idx_powerproc_name ON power_proc(name, ts);
CREATE INDEX IF NOT EXISTS idx_runs_started   ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runmetrics_rid ON run_metrics(run_id, key, ts);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_runs_ext ON runs(source, ext_id);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_event ON events(ts, service, kind);
CREATE TABLE IF NOT EXISTS alert_rules(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
  ctype TEXT NOT NULL, params TEXT NOT NULL DEFAULT '{}', channel TEXT NOT NULL DEFAULT 'all',
  level TEXT NOT NULL DEFAULT 'warning', cooldown_min INTEGER NOT NULL DEFAULT 60,
  created_at INTEGER NOT NULL, last_fired_at INTEGER, last_state TEXT,
  snoozed_until INTEGER);
CREATE TABLE IF NOT EXISTS alert_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, rule_id TEXT, rule_name TEXT,
  level TEXT, channel TEXT, status TEXT, title TEXT, detail TEXT, acked INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_alerthist_ts ON alert_history(ts);
-- Built-in notifier edge-state (the `_NOTIFIED` dict). Each key (container:NAME,
-- systemd:UNIT, disk:MOUNT, gpu:vram_pressure, oom:SVC:TS) is "armed" while its
-- condition is still true and a notification has already been sent; it clears when
-- the condition recovers. This table survives a restart so an already-fired,
-- still-true condition does NOT spuriously re-fire on the next post-restart scan.
CREATE TABLE IF NOT EXISTS notified_state(key TEXT PRIMARY KEY, notified_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS maintenance_windows(
  id TEXT PRIMARY KEY, label TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  recurring INTEGER NOT NULL DEFAULT 0,
  start_ts INTEGER, end_ts INTEGER, daily_start TEXT, daily_end TEXT,
  created_at INTEGER NOT NULL);
-- Notification routing rules: redirect a firing alert to a specific channel when
-- its entity/subject glob-matches and its level is at/above min_level. Evaluated in
-- `priority` order BEFORE the default fan-out. ZERO rows = byte-identical to the
-- pre-routing behaviour (the alert rule's own channel). Only ever REDIRECTS; never
-- drops (a matched route to an unconfigured channel falls back, never black-holes).
CREATE TABLE IF NOT EXISTS notification_routes(
  id TEXT PRIMARY KEY, label TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  match TEXT NOT NULL DEFAULT '*', min_level TEXT NOT NULL DEFAULT 'info',
  channel TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_routes_order ON notification_routes(priority, created_at);
CREATE TABLE IF NOT EXISTS incidents(
  id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'open', severity TEXT NOT NULL DEFAULT 'warning',
  opened_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, cleared_at INTEGER,
  miss INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS incident_members(
  incident_id TEXT NOT NULL, series TEXT NOT NULL, direction TEXT, peak_z REAL,
  unit TEXT, peak_value REAL, baseline REAL, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
  active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(incident_id, series));
CREATE INDEX IF NOT EXISTS idx_incidents_state ON incidents(state, opened_at);
CREATE INDEX IF NOT EXISTS idx_incidents_opened ON incidents(opened_at);
CREATE TABLE IF NOT EXISTS llm_samples(ts INTEGER NOT NULL, model TEXT, tps REAL,
  ttft_ms REAL, prompt_tps REAL, eval_count INTEGER);
CREATE INDEX IF NOT EXISTS idx_llm_ts ON llm_samples(ts);
-- Public status heartbeat history (E4): per-subsystem-key status rank sampled at a
-- coarse cadence so the /status page can paint Uptime-Kuma-style bars. Aggregated &
-- anonymized ONLY — `key` is a fixed subsystem label (gpu/host/containers/services/
-- overall), `state` is the 0..3 status rank (0 ok → 3 down). NO names/topology.
CREATE TABLE IF NOT EXISTS status_history(ts INTEGER NOT NULL, key TEXT NOT NULL, state INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_stathist_ts ON status_history(ts);
-- External uptime checks (Uptime-Kuma-style): the user's OWN configured HTTP/TCP
-- endpoint monitors. `target` is a URL (http) or host:port (tcp) — treated like
-- webhook_url (may carry credentials): persisted for the user, never logged.
-- These are PRIVATE (LAN dashboard + authed API only); they never reach /status.
CREATE TABLE IF NOT EXISTS uptime_checks(
  id TEXT PRIMARY KEY, label TEXT NOT NULL, type TEXT NOT NULL DEFAULT 'http',
  target TEXT NOT NULL, interval_sec INTEGER NOT NULL DEFAULT 60,
  timeout_sec INTEGER NOT NULL DEFAULT 10, expected_status INTEGER,
  enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL,
  cert_warn_days INTEGER);
CREATE TABLE IF NOT EXISTS uptime_results(
  check_id TEXT NOT NULL, ts INTEGER NOT NULL, up INTEGER NOT NULL,
  latency_ms REAL, code INTEGER, err TEXT, days_to_expiry INTEGER);
CREATE INDEX IF NOT EXISTS idx_uptime_results ON uptime_results(check_id, ts);
-- Controls audit log (accountability for every host mutation): an append-only,
-- bounded record of every EXECUTED control action (start/stop/restart of a
-- container/service) that passed the ENABLE_CONTROLS gate and reached resolution.
-- Both success AND failure are recorded — a rejected action is exactly what you
-- want audited. `target` is the RESOLVED tracked name/id (never raw user input);
-- `detail` is a short generic phrase (NO secrets/paths/tracebacks); `actor` is a
-- coarse, best-effort client identifier (remote address only — no new PII). This
-- table is PRIVATE: the authed LAN dashboard + /api/controls/log only; it NEVER
-- reaches /status, /api/status, or any public feed.
CREATE TABLE IF NOT EXISTS control_audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
  kind TEXT NOT NULL, target TEXT NOT NULL, action TEXT NOT NULL,
  result TEXT NOT NULL, detail TEXT, actor TEXT);
CREATE INDEX IF NOT EXISTS idx_ctrlaudit_ts ON control_audit(ts);
"""
# cpu_power/dram_power: measured CPU package / DRAM watts via RAPL (#costs). NULL when unavailable.
_SAMPLE_MIGRATIONS = ("cpu REAL", "ram_used REAL", "ram_total REAL", "load1 REAL", "ctemp REAL",
                      "cpu_power REAL", "dram_power REAL")
# Per-host adaptive poll-timeout state (issue #99); added to the hosts table.
_HOST_MIGRATIONS = ("poll_timeout INTEGER", "poll_fails INTEGER DEFAULT 0", "poll_calibrated_at INTEGER")
# cert_warn_days: per-check TLS expiry warning window (cert checks only). days_to_expiry:
# the cert's days-to-expiry recorded on each cert probe. Both NULL for http/tcp checks.
# public: per-check opt-in flag for the public /status surface (0=private by
# default; old rows read as 0). A check appears on the public page ONLY when its
# operator explicitly opts it in AND it is enabled — see _public_check_detail.
_UPTIME_CHECKS_MIGRATIONS = ("cert_warn_days INTEGER", "public INTEGER NOT NULL DEFAULT 0")
# cert_extra: JSON blob of the cert probe's public metadata (not_after string,
# subject_cn, issuer_cn, expiring) recorded on each cert probe; NULL for http/tcp
# results and for pre-migration rows. Surfaced (read) by _uptime_state.
_UPTIME_RESULTS_MIGRATIONS = ("days_to_expiry INTEGER", "cert_extra TEXT")
# Which API key pushed a run (for per-key attribution); added to the runs table.
_RUNS_MIGRATIONS = ("key_id TEXT",)
# energy_wh: per-inference GPU energy (Wh) = eval_duration_s × live GPU power W / 3600,
# stamped when a copilot generation is captured (NULL when GPU power/timing absent or
# for pre-migration rows). Powers the "local vs cloud" inference-savings rollup.
_LLM_SAMPLES_MIGRATIONS = ("energy_wh REAL",)
# Persisted Lab-Copilot explanation for a correlated incident (E1). All nullable:
# ai_explanation = the bounded plain-English probable-cause + suggested next step
# (LLM-generated, sanitised); ai_explained_at = unix ts it was generated;
# ai_model = the model that produced it. Written ONLY from explicit user actions
# (Explain/Regenerate, first-open one-shot) or the dedicated auto-explain worker —
# NEVER on the metrics/collector poll path. Incident READS return these cached
# fields with zero LLM calls. Old rows read as NULL (feature simply shows nothing).
# ai_cause_notified_at = unix ts the ONE supplementary "probable cause" notification
# was sent for this incident (the auto-explain worker's at-most-once dedup flag);
# NULL until sent / for old rows. Never involves the LLM — set on notification send.
_INCIDENTS_MIGRATIONS = ("ai_explanation TEXT", "ai_explained_at INTEGER", "ai_model TEXT",
                         "ai_cause_notified_at INTEGER",
                         # ── AI incident postmortem (E1) — the capstone of the
                         # incidents+AI thread. Written ONLY for RESOLVED (cleared)
                         # incidents, from an explicit "Generate postmortem" action or
                         # the dedicated off-poll postmortem worker — NEVER on any poll
                         # path. All nullable; old rows read as NULL (drawer simply
                         # shows the deterministic skeleton / a Generate affordance).
                         # postmortem_json = the persisted structured postmortem blob
                         # (JSON: {probable_cause, impact, recommended_action}); the
                         # deterministic facts (timeline, duration, members) are NEVER
                         # stored here — they are re-derived from the incident row on
                         # every read so they can never drift. postmortem_at = unix ts
                         # it was generated; postmortem_model = the model that produced
                         # it. ONE at-most-once atomic claim via postmortem_at.
                         "postmortem_json TEXT", "postmortem_at INTEGER", "postmortem_model TEXT")

def _data_dir():
    return os.path.dirname(os.path.abspath(DB_PATH)) or "."

def _data_dir_writable():
    d = _data_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return False
    return os.access(d, os.W_OK)

def _open_db_connection(path):
    conn = sqlite3.connect(path, check_same_thread=False)
    # WAL lets the many reader threads (collector, host_poller, uptime_worker,
    # mqtt_worker, image_update_worker + request threads) read without blocking
    # the single writer. Host-mounted-volume safe: the -wal/-shm sidecars live
    # alongside gpu.db in /data and are checkpointed on clean close; a restore
    # path explicitly clears them (db_backup.remove_wal_sidecars).
    conn.execute("PRAGMA journal_mode=WAL")
    # busy_timeout turns a momentarily-held file lock from an instant
    # "database is locked" error into a short wait (up to 5s) for the lock to
    # free — strictly more robust under concurrency, and the fix for the
    # transient test-suite lock flake. Applies to every connection we open
    # (live app.DB and the shared test handle, same code path).
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def _apply_schema_migrations(conn):
    conn.executescript(_DB_SCHEMA)
    for col in _SAMPLE_MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE samples ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in _HOST_MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE hosts ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in _RUNS_MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in _UPTIME_CHECKS_MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE uptime_checks ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in _UPTIME_RESULTS_MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE uptime_results ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in _LLM_SAMPLES_MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE llm_samples ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in _INCIDENTS_MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE incidents ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    # Migrate a legacy single instance-wide api_key (the previous design) into the
    # api_keys table as a named key, then clear the setting — so existing clients
    # keep working under the new multi-key model.
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='api_key'").fetchone()
        legacy = (row[0] if row else "") or ""
        if legacy:
            h = hashlib.sha256(legacy.encode("utf-8")).hexdigest()
            if not conn.execute("SELECT 1 FROM api_keys WHERE key_hash=?", (h,)).fetchone():
                conn.execute("INSERT INTO api_keys(id,name,key_hash,prefix,created_at,expires_at,last_used_at) "
                             "VALUES(?,?,?,?,?,?,?)",
                             (uuid.uuid4().hex, "default (migrated)", h, legacy[:12], int(time.time()), None, None))
            conn.execute("UPDATE settings SET value='' WHERE key='api_key'")
    except sqlite3.OperationalError:
        pass
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

LATEST = {"ts": 0, "util": 0, "mem_used": 0, "mem_total": 24576, "power": 0, "temp": 0,
          "procs": [], "models": [], "callers": [], "host": {}, "gpu_avail": None, "gpus": [], "gpu_extra": {},
          "model_meta": {}, "serving": [], "training": [], "devtools": []}
# Current state of the "status" monitors (Docker + systemd). The background
# collector refreshes these; /api/health just serves the cached snapshot.
HEALTH = {"docker": None, "systemd": None, "update": None, "processes": None, "disk_io": None, "at": 0}
WATCH_SERVICES = [s.strip() for s in os.environ.get("WATCH_SERVICES", "").split(",") if s.strip()]
SYSTEMD_ADMIN_DIR = "/etc/systemd/system/"   # units here are admin/user-authored (vs vendor)
_ct_cache = {"list": [], "at": 0}
_scan_since = {}
_cpu_prev = {"idle": 0, "total": 0}

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

# ── Image-update awareness (What's-Up-Docker style) ────────────────────────────
# AWARENESS ONLY: for each RUNNING container, read its deployed image digest from
# the LOCAL docker socket (already mounted, already read elsewhere) and compare it
# against the upstream registry's CURRENT manifest digest for the same tag. We
# NEVER pull/run/restart/delete — only registry GETs + local inspects. The whole
# subsystem is OFF by default (image_update_check=0 → zero outbound, zero new
# behaviour). When on, a dedicated daemon thread runs on a slow cadence, heavily
# caches results (~the interval), bounds work per cycle, uses short timeouts, and
# catches every exception so a registry hiccup can NEVER affect monitoring.
#
# Status vocabulary (per container):
#   up_to_date       — deployed digest == upstream manifest digest
#   update_available — deployed digest != upstream manifest digest
#   unknown          — digest-pinned / local-only / unresolvable / private /
#                      auth fail / rate-limited / timeout / unreachable

# Manifest Accept set: cover both single-image manifests and multi-arch manifest
# lists / OCI indexes so the Docker-Content-Digest we read matches what `docker
# pull <repo>:<tag>` would resolve to for this host.
_IMG_ACCEPT = ", ".join([
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
])
_IMG_UA = "homelab-monitor-imgcheck/%s" % VERSION

# In-memory result cache (no new table needed — results live ~one interval and a
# restart simply re-checks). Keyed by container id. Guarded by its own lock so we
# NEVER hold the DB LOCK across a network call.
_IMG_LOCK   = threading.Lock()
_IMG_STATE  = {"results": {}, "checked_at": 0, "count": 0, "running": False,
               "rate_limited_until": 0, "last_error": None, "enabled": False}
# A small per-(repo,tag,registry) upstream-digest cache so two containers on the
# same image don't double-query, and re-checks within the interval are free.
_IMG_DIGEST_CACHE = {}   # (registry, repo, tag) -> {"digest": str|None, "at": ts, "status": str}

def _img_settings(settings=None):
    """Resolve the effective image-update-check config from settings. Returns
    (enabled: bool, interval_sec: int). Never raises."""
    try:
        s = settings if settings is not None else get_settings()
        enabled = str(s.get("image_update_check", "0")) == "1"
        interval = int(float(s.get("image_update_interval_sec", IMG_CHECK_INTERVAL_DEFAULT)))
    except Exception:
        return False, IMG_CHECK_INTERVAL_DEFAULT
    interval = max(IMG_CHECK_INTERVAL_MIN, interval)
    return enabled, interval

def _parse_image_ref(image):
    """Parse a docker image reference into (registry, repository, tag, pinned_digest).

    Handles: implicit Docker Hub (nginx, library/nginx, user/repo), explicit
    docker.io, ghcr.io and other v2 registries (host[:port]/path), :tag (default
    latest), and digest-pinned refs (repo@sha256:...). Returns registry==None for
    an unparseable/empty ref. `pinned_digest` is set only for @sha256 refs (those
    are unknown — there's no moving tag to compare)."""
    if not image or not isinstance(image, str):
        return (None, None, None, None)
    ref = image.strip()
    if not ref:
        return (None, None, None, None)
    # Digest-pinned: repo@sha256:... — and possibly repo:tag@sha256:...
    pinned = None
    if "@" in ref:
        ref, _, dig = ref.partition("@")
        pinned = dig.strip() or None
    # Split off the registry host: the first path segment is a host iff it has a
    # '.' or ':' (port) or is exactly 'localhost' — Docker's own rule.
    registry = None
    name = ref
    if "/" in ref:
        head, _, rest = ref.partition("/")
        if head == "localhost" or "." in head or ":" in head:
            registry = head
            name = rest
    # Tag (only after the registry split, so a registry port colon isn't mistaken
    # for a tag separator).
    repo, tag = name, "latest"
    if ":" in name:
        repo, _, tag = name.rpartition(":")
    # Normalise the registry + library/ default for Docker Hub.
    if registry is None or registry in ("docker.io", "index.docker.io", "registry-1.docker.io"):
        registry = "docker.io"
        if "/" not in repo:
            repo = "library/" + repo
    if not repo:
        return (None, None, None, None)
    return (registry, repo, tag, pinned)

def _registry_token(registry, repo, timeout=6):
    """Fetch an anonymous pull bearer token for a v2 registry. Docker Hub and GHCR
    expose a token service; many others advertise theirs via a 401 WWW-Authenticate
    challenge. We try the well-known endpoints first, then fall back to the
    challenge. Returns a token string or None (anonymous/none needed)."""
    urls = []
    if registry == "docker.io":
        urls.append("https://auth.docker.io/token?service=registry.docker.io&scope=repository:%s:pull" % repo)
    elif registry == "ghcr.io":
        urls.append("https://ghcr.io/token?scope=repository:%s:pull" % repo)
    elif registry in ("quay.io",):
        urls.append("https://quay.io/v2/auth?service=quay.io&scope=repository:%s:pull" % repo)
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _IMG_UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read() or b"{}")
            tok = body.get("token") or body.get("access_token")
            if tok:
                return tok
        except Exception:
            continue
    return None

def _registry_base(registry):
    return "registry-1.docker.io" if registry == "docker.io" else registry

def _upstream_manifest_digest(registry, repo, tag, timeout=6):
    """Query the registry v2 manifest endpoint for the CURRENT digest of repo:tag.
    Returns (digest|None, status) where status is one of 'ok', 'rate_limited',
    'auth', 'notfound', 'error'. Never raises. Read-only GET (we request the
    manifest but only need the Docker-Content-Digest header)."""
    host = _registry_base(registry)
    url = "https://%s/v2/%s/manifests/%s" % (host, repo, urllib.parse.quote(tag, safe=""))
    token = _registry_token(registry, repo, timeout=timeout)
    def _do(tok):
        hdr = {"Accept": _IMG_ACCEPT, "User-Agent": _IMG_UA}
        if tok:
            hdr["Authorization"] = "Bearer " + tok
        # HEAD is enough for the digest and avoids pulling the manifest body, but
        # some registries reject HEAD on manifests — we GET if HEAD misbehaves.
        req = urllib.request.Request(url, headers=hdr, method="HEAD")
        return urllib.request.urlopen(req, timeout=timeout)
    try:
        try:
            resp = _do(token)
        except urllib.error.HTTPError as he:
            if he.code in (405, 400):     # HEAD unsupported → retry as GET
                hdr = {"Accept": _IMG_ACCEPT, "User-Agent": _IMG_UA}
                if token:
                    hdr["Authorization"] = "Bearer " + token
                resp = urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=timeout)
            else:
                raise
        with resp:
            dig = resp.headers.get("Docker-Content-Digest")
        if dig:
            return (dig.strip(), "ok")
        return (None, "error")
    except urllib.error.HTTPError as he:
        if he.code == 429:
            return (None, "rate_limited")
        if he.code in (401, 403):
            return (None, "auth")
        if he.code == 404:
            return (None, "notfound")
        return (None, "error")
    except Exception:
        return (None, "error")

def _container_deployed_digest(cid):
    """Read the deployed image's repo-digest (what's actually running) from the
    LOCAL docker socket. Inspect the container for its Image (image id), then
    inspect that image for RepoDigests — the digest the local image was pulled at.
    Returns (digest|None, image_ref). Never raises."""
    try:
        code, raw = _docker_req("GET", "/containers/%s/json" % urllib.parse.quote(cid), timeout=4)
        if code >= 400:
            return (None, None)
        ins = json.loads(raw or b"{}")
    except Exception:
        return (None, None)
    image_ref = (ins.get("Config") or {}).get("Image") or ins.get("Image") or ""
    img_id = ins.get("Image") or ""
    try:
        code, raw = _docker_req("GET", "/images/%s/json" % urllib.parse.quote(img_id), timeout=4)
        if code >= 400:
            return (None, image_ref)
        idata = json.loads(raw or b"{}")
    except Exception:
        return (None, image_ref)
    repo_digests = idata.get("RepoDigests") or []
    digest = None
    # RepoDigests look like "repo@sha256:...". Match the one for our repo if we can,
    # else take the first (single-repo images have exactly one).
    reg, repo, _tag, _pin = _parse_image_ref(image_ref)
    for rd in repo_digests:
        if "@" in rd:
            name_part, _, dg = rd.partition("@")
            r2, repo2, _t2, _p2 = _parse_image_ref(name_part)
            if repo and repo2 == repo:
                digest = dg.strip()
                break
            if digest is None:
                digest = dg.strip()
    return (digest, image_ref)

def _check_one_image(cid, image_ref, interval, force=False):
    """Resolve the update status for a single container. Uses the per-image
    upstream-digest cache so repeated/same-image checks are free within the
    interval. `force` skips the READ side of that cache (always re-queries the
    registry) for an explicit user-triggered check, but still WRITES the result
    back so the background view stays warm. Returns a result dict. Never raises."""
    reg, repo, tag, pinned = _parse_image_ref(image_ref)
    base = {"id": cid, "image": image_ref or "", "registry": reg,
            "repository": repo, "tag": tag, "status": "unknown",
            "current_digest": None, "latest_digest": None, "checked_at": int(time.time())}
    if pinned:
        base["reason"] = "digest-pinned"
        return base
    if not reg or not repo:
        base["reason"] = "unparseable"
        return base
    # Local-only / private registries we can't anonymously reach → unknown, but we
    # still TRY the public ones (docker.io/ghcr.io/quay.io/lscr.io/gcr.io etc.).
    deployed, _ref = _container_deployed_digest(cid)
    base["current_digest"] = deployed
    if not deployed:
        base["reason"] = "no-local-digest"   # built locally / never pushed with a digest
        return base
    # Per-image upstream digest cache.
    ck = (reg, repo, tag)
    now = time.time()
    cached = _IMG_DIGEST_CACHE.get(ck)
    if cached and not force and (now - cached["at"]) < interval:
        latest, status = cached["digest"], cached["status"]
    else:
        latest, status = _upstream_manifest_digest(reg, repo, tag)
        _IMG_DIGEST_CACHE[ck] = {"digest": latest, "status": status, "at": now}
    base["latest_digest"] = latest
    if status == "rate_limited":
        base["status"] = "unknown"; base["reason"] = "rate_limited"; return base
    if status == "auth":
        base["status"] = "unknown"; base["reason"] = "private-or-auth"; return base
    if status == "notfound":
        base["status"] = "unknown"; base["reason"] = "tag-not-found"; return base
    if status != "ok" or not latest:
        base["status"] = "unknown"; base["reason"] = "unreachable"; return base
    base["status"] = "up_to_date" if latest == deployed else "update_available"
    return base

def _image_update_run_cycle():
    """One bounded pass: for each RUNNING container, resolve its update status,
    respecting a per-cycle cap, a rate-limit backoff, and the OFF switch. Publishes
    into _IMG_STATE. Never raises; never holds DB LOCK across network calls."""
    enabled, interval = _img_settings()
    if not enabled:
        with _IMG_LOCK:
            _IMG_STATE["enabled"] = False
        return
    now = time.time()
    with _IMG_LOCK:
        _IMG_STATE["enabled"] = True
        rl_until = _IMG_STATE["rate_limited_until"]
        already = _IMG_STATE["checked_at"]
    if now < rl_until:
        return                                   # backing off a 429
    if already and (now - already) < interval:
        return                                   # cached results still fresh
    # Enumerate RUNNING containers from the local socket (read-only).
    try:
        raw = json.loads(_docker("/containers/json"))   # running only (no ?all=1)
    except Exception as e:
        with _IMG_LOCK:
            _IMG_STATE["last_error"] = "docker: %s" % e
        return
    targets = [(ct["Id"][:12], ct.get("Image", "")) for ct in raw if ct.get("Id")]
    targets = targets[:IMG_CHECK_MAX_PER_CYCLE]      # bound the work
    results, rate_limited = {}, False
    for cid, image_ref in targets:
        try:
            res = _check_one_image(cid, image_ref, interval)
        except Exception as e:
            res = {"id": cid, "image": image_ref or "", "status": "unknown",
                   "reason": "error", "checked_at": int(time.time())}
            print("image check error (%s): %s" % (cid, e), flush=True)
        if res.get("reason") == "rate_limited":
            rate_limited = True
        results[cid] = res
    count = sum(1 for r in results.values() if r.get("status") == "update_available")
    with _IMG_LOCK:
        _IMG_STATE["results"] = results
        _IMG_STATE["checked_at"] = int(time.time())
        _IMG_STATE["count"] = count
        _IMG_STATE["last_error"] = None
        if rate_limited:
            _IMG_STATE["rate_limited_until"] = time.time() + IMG_CHECK_RATELIMIT_BACKOFF

def image_update_worker():
    """Dedicated daemon loop. Inert (zero outbound) while disabled. When enabled,
    runs one bounded, heavily-cached cycle then sleeps; a registry problem is caught
    and can NEVER stall or crash monitoring."""
    while True:
        try:
            _image_update_run_cycle()
        except Exception as e:
            print("image_update_worker error:", e, flush=True)
        time.sleep(60)   # wake often; the cycle itself no-ops until the interval elapses

def image_updates_snapshot():
    """Read-only view of the current image-update results for the API + reco +
    badge. Never raises. When disabled, returns enabled=False with empty results."""
    enabled, interval = _img_settings()
    with _IMG_LOCK:
        results = list(_IMG_STATE["results"].values())
        checked_at = _IMG_STATE["checked_at"]
        count = _IMG_STATE["count"]
        last_error = _IMG_STATE["last_error"]
        rl_until = _IMG_STATE["rate_limited_until"]
    # If disabled, never advertise stale results — including a stale rate-limit
    # backoff left over from a previous enabled run (off => fully inert/clean).
    if not enabled:
        results, count, checked_at, rl_until = [], 0, 0, 0
    by_status = {"up_to_date": 0, "update_available": 0, "unknown": 0}
    for r in results:
        by_status[r.get("status", "unknown")] = by_status.get(r.get("status", "unknown"), 0) + 1
    return {"enabled": enabled, "interval_sec": interval, "checked_at": checked_at,
            "count": count, "by_status": by_status, "results": results,
            "rate_limited": time.time() < rl_until, "last_error": last_error}

def image_update_check_one(cid, cname, image_ref):
    """On-demand image-update re-check for ONE already-resolved container. This is
    an explicit user action (like 'test notification'): it runs even when the
    background poll is OFF, because it's a single bounded outbound query the user
    asked for. AWARENESS ONLY — registry GET/HEAD for the upstream manifest digest
    + a local docker-socket GET for the deployed digest; NO pull/run/restart/exec/
    delete, no host mutation. Bounded (one image, short per-request timeouts via
    the probe), never hangs, never raises. Respects the rate-limit backoff (returns
    rate_limited rather than hammering Docker Hub). The probe runs OUTSIDE the lock;
    the lock is held only briefly to read the backoff and to write the cache.

    Returns {status, deployed_digest, upstream_digest, checked_at, reason?,
    rate_limited}."""
    now = time.time()
    with _IMG_LOCK:
        rl_until = _IMG_STATE["rate_limited_until"]
    if now < rl_until:
        return {"status": "unknown", "reason": "rate_limited", "rate_limited": True,
                "deployed_digest": None, "upstream_digest": None,
                "checked_at": int(now)}
    # force=True → bypass the long interval cache so the user gets a genuinely
    # fresh answer; the probe itself still has short bounded timeouts.
    _, interval = _img_settings()
    try:
        res = _check_one_image(cid, image_ref, interval, force=True)
    except Exception as e:
        print("on-demand image check error (%s): %s" % (cid, e), flush=True)
        res = {"id": cid, "image": image_ref or "", "status": "unknown",
               "reason": "error", "current_digest": None, "latest_digest": None,
               "checked_at": int(time.time())}
    rate_limited = res.get("reason") == "rate_limited"
    # Warm the shared view: write this one result into the background cache so the
    # Containers tab / /api/images/updates reflect the fresh status immediately.
    with _IMG_LOCK:
        _IMG_STATE["results"][cid] = res
        _IMG_STATE["count"] = sum(
            1 for r in _IMG_STATE["results"].values()
            if r.get("status") == "update_available")
        if rate_limited:
            _IMG_STATE["rate_limited_until"] = time.time() + IMG_CHECK_RATELIMIT_BACKOFF
    return {"status": res.get("status", "unknown"),
            "reason": res.get("reason"),
            "deployed_digest": res.get("current_digest"),
            "upstream_digest": res.get("latest_digest"),
            "checked_at": res.get("checked_at", int(time.time())),
            "rate_limited": rate_limited}

# ── Model-server probes (agnostic: append to PROBES to support a new server) ───
def _http_json(ip, port, path, timeout=2):
    # try/finally so a down/slow model server (common: idle servers, wrong port
    # guesses) can't leak the TCP socket fd when request/read raises.
    c = http.client.HTTPConnection(ip, port, timeout=timeout)
    try:
        c.request("GET", path); r = c.getresponse(); body = r.read()
        status = r.status
    finally:
        c.close()
    return json.loads(body) if status < 400 else None

def probe_ollama(ip):
    """Ollama: models loaded *now* (with live VRAM) from /api/ps; if none are loaded,
    fall back to the pulled catalogue (/api/tags) so the server still shows as Idle."""
    ps = _http_json(ip, 11434, "/api/ps")
    loaded = [(m["name"], (m.get("size_vram") or 0) / 1048576 or None)
              for m in (ps or {}).get("models", []) if m.get("name")]
    if loaded:
        return loaded
    tags = _http_json(ip, 11434, "/api/tags")
    return [(m["name"], None) for m in (tags or {}).get("models", []) if m.get("name")]

def _openai_models(*ports):
    """Factory for the OpenAI-compatible `GET /v1/models` shape (`data[].id`), shared by
    vLLM, llama.cpp/llama-server, LocalAI, faster-whisper-server/Speaches, koboldcpp,
    tabbyAPI, text-generation-webui, LM Studio, xinference, … — they differ only by port.
    Tries each candidate port until one answers with a non-empty model list."""
    def fn(ip):
        for p in ports:
            d = _http_json(ip, p, "/v1/models")
            data = (d or {}).get("data")
            if data:
                return [(m.get("id"), None) for m in data if m.get("id")]
        return []
    return fn

def probe_tgi(ip):
    """HF Text-Generation-Inference / Text-Embeddings-Inference: `GET /info` → model_id."""
    d = _http_json(ip, 80, "/info") or _http_json(ip, 3000, "/info") or _http_json(ip, 8080, "/info")
    return [(d["model_id"], None)] if d and d.get("model_id") else []

def probe_koboldcpp(ip):
    d = _http_json(ip, 5001, "/api/v1/model")
    if d and d.get("result"):
        return [(d["result"], None)]
    return _openai_models(5001)(ip)

def probe_invokeai(ip):
    """InvokeAI v4+: `GET /api/v2/models/` → models[].name (its installed catalogue)."""
    d = _http_json(ip, 9090, "/api/v2/models/")
    return [(m.get("name"), None) for m in (d or {}).get("models", []) if m.get("name")]

def probe_a1111(ip):
    """AUTOMATIC1111 / SD.Next / Forge: the currently-loaded checkpoint. Kept to a single
    entry so the server's GPU VRAM (from nvidia-smi) attributes cleanly to it."""
    m = (_http_json(ip, 7860, "/sdapi/v1/options") or {}).get("sd_model_checkpoint")
    return [(m, None)] if m else []

def probe_whisper_asr(ip):
    """ahmetoner/whisper-asr-webservice (Whisper / WhisperX / faster-whisper
    engines). It has no model-list endpoint — just `/asr` — so we confirm it's up
    via `/openapi.json` and show one Idle entry; its GPU VRAM (nvidia-smi)
    attributes to it while it's transcribing. If something else answers here with
    the OpenAI shape, fall back to listing its models so we don't hide it."""
    for port in (9000, 8000):
        info = (_http_json(ip, port, "/openapi.json") or {}).get("info") or {}
        if "whisper" in (info.get("title", "") + " " + info.get("description", "")).lower():
            return [("Whisper ASR webservice", None)]
    return _openai_models(9000, 8000, 8080)(ip)

def probe_triton(ip):
    """NVIDIA Triton Inference Server: `GET /v2` returns server metadata. The live
    model list is a POST (/v2/repository/index), so we show a single Idle entry
    and let nvidia-smi VRAM attribute to it."""
    d = _http_json(ip, 8000, "/v2")
    if d and "triton" in (d.get("name", "") or "").lower():
        return [("Triton Inference Server", None)]
    return []

def probe_wyoming(ip):
    """Wyoming-protocol voice services (Home Assistant: wyoming-faster-whisper /
    -whisper ASR, -piper TTS, -openwakeword). Plain TCP + JSONL, not HTTP: send a
    `describe` event and read the `info` reply for the program/model names."""
    for port in (10300, 10200, 10400, 10500, 10700):
        try:
            with socket.create_connection((ip, port), timeout=2) as sk:
                sk.settimeout(2)
                sk.sendall(b'{"type": "describe"}\n')
                buf = b""
                while b"\n" not in buf and len(buf) < 65536:
                    chunk = sk.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                header, _, rest = buf.partition(b"\n")
                evt = json.loads(header.decode("utf-8", "replace") or "{}")
                if evt.get("type") != "info":
                    continue
                need = evt.get("data_length") or 0
                while len(rest) < need:
                    chunk = sk.recv(4096)
                    if not chunk:
                        break
                    rest += chunk
                data = json.loads(rest[:need].decode("utf-8", "replace")) if need else {}
        except Exception:
            continue
        names = []
        for grp in ("asr", "tts", "wake", "handle", "intent"):
            for prog in (data.get(grp) or []):
                got = [m.get("name") for m in (prog.get("models") or []) if m.get("name")]
                names += got or ([prog["name"]] if prog.get("name") else [])
        return [(n, None) for n in names] or [("Wyoming service", None)]
    return []

def probe_comfy(ip):
    """ComfyUI: list installed checkpoints from /object_info (real model names). It has no
    'currently loaded' concept, so checkpoints show Idle and the server's GPU VRAM
    (nvidia-smi) attributes to the card. Falls back to a sentinel if it's up but bare."""
    if _http_json(ip, 8188, "/system_stats") is None:
        return []
    info = _http_json(ip, 8188, "/object_info/CheckpointLoaderSimple") or {}
    try:
        names = info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
    except Exception:
        names = []
    return [(n, None) for n in names] or [("ComfyUI (no checkpoints)", None)]

# (key-substring → probe). The key is matched against the container's image AND name,
# first match wins, so order specific→generic. Most servers speak the OpenAI
# /v1/models shape and differ only by their default internal port.
PROBES = [
    ("ollama",                     probe_ollama),
    ("vllm",                       _openai_models(8000)),
    ("text-generation-inference",  probe_tgi),
    ("text-embeddings-inference",  probe_tgi),
    ("lorax",                      probe_tgi),
    # ASR / speech — keep the specific whisper-asr-webservice + WhisperX keys ahead
    # of the generic "whisper" so they don't fall through to the OpenAI probe.
    ("whisper-asr-webservice",     probe_whisper_asr),
    ("asr-webservice",             probe_whisper_asr),
    ("whisperx",                   probe_whisper_asr),
    ("whisper-asr",                probe_whisper_asr),
    ("faster-whisper",             _openai_models(8000)),
    ("speaches",                   _openai_models(8000)),
    ("whisper",                    _openai_models(8000, 9000)),
    ("wyoming",                    probe_wyoming),
    ("openedai-speech",            _openai_models(8000)),
    ("localai",                    _openai_models(8080)),
    ("local-ai",                   _openai_models(8080)),
    ("llama.cpp",                  _openai_models(8080, 8000)),
    ("llama-server",               _openai_models(8080, 8000)),
    ("llamacpp",                   _openai_models(8080, 8000)),
    ("ggml",                       _openai_models(8080, 8000)),
    ("koboldcpp",                  probe_koboldcpp),
    ("tabbyapi",                   _openai_models(5000)),
    ("exllama",                    _openai_models(5000)),
    ("text-generation-webui",      _openai_models(5000)),
    ("oobabooga",                  _openai_models(5000)),
    ("lmstudio",                   _openai_models(1234)),
    ("lm-studio",                  _openai_models(1234)),
    ("xinference",                 _openai_models(9997)),
    ("xorbits",                    _openai_models(9997)),
    ("aphrodite",                  _openai_models(2242)),
    ("mistral-rs",                 _openai_models(1234, 8080)),
    ("sglang",                     _openai_models(30000, 8000)),
    ("ramalama",                   _openai_models(8080, 8000)),
    ("nexa",                       _openai_models(8000)),
    ("openllm",                    _openai_models(3000, 8000)),
    ("litellm",                    _openai_models(4000)),
    ("gpustack",                   _openai_models(80, 8080)),
    ("cortex",                     _openai_models(39281, 1337)),
    ("janhq",                      _openai_models(1337)),
    ("triton",                     probe_triton),
    ("infinity",                   _openai_models(7997)),
    ("invokeai",                   probe_invokeai),
    ("invoke-ai",                  probe_invokeai),
    ("automatic1111",              probe_a1111),
    ("stable-diffusion-webui",     probe_a1111),
    ("sd-webui",                   probe_a1111),
    ("sdnext",                     probe_a1111),
    ("comfyui",                    probe_comfy),
]

def _match_probe(ct):
    """Return the probe fn for a container whose image/name matches a known server, else None."""
    img, name = ct.get("image", "").lower(), ct.get("name", "").lower()
    for key, fn in PROBES:
        if key in img or key in name:
            return fn
    return None

CATALOG_MAX = 15   # max idle "available" models listed per server before collapsing to a count

def probe_models(ct):
    fn = _match_probe(ct)
    if not fn:
        return []
    # Host-networked servers have no per-container IP; the hub shares the host net
    # namespace, so localhost reaches them on their published/default port.
    ip = ct.get("ip") or "127.0.0.1"
    try:
        found = [(m, v) for m, v in fn(ip) if m]
    except Exception:
        return []
    loaded = [x for x in found if x[1] is not None]
    idle   = [x for x in found if x[1] is None]
    # Collapse an oversized idle catalogue (faster-whisper, for one, exposes its full
    # upstream registry of 400+ models) into a single summary row so it can't flood
    # the panel. Loaded models and small catalogues (e.g. your pulled Ollama models)
    # are kept verbatim.
    if len(idle) > CATALOG_MAX:
        idle = [(f"{len(idle)} models available", None)]
    return loaded + idle

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

_OLLAMA_META = {}   # model name -> {param_size, quant, ctx, caps}; immutable per tag, cached

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
    for svc, mdl, vram in models:
        if svc not in is_ollama or not mdl:
            continue
        if mdl in _OLLAMA_META:
            out[mdl] = _OLLAMA_META[mdl]
        elif vram is not None:                      # only pay /api/show for loaded models
            meta = _ollama_meta(ip_of.get(svc, "127.0.0.1"), mdl)
            if meta:
                out[mdl] = meta
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

# ── Host metrics (read from /proc, /sys, statvfs — host values via shared kernel)
def _cpu_pct():
    parts = list(map(int, open("/proc/stat").readline().split()[1:]))
    idle, total = parts[3] + parts[4], sum(parts)
    di, dt = idle - _cpu_prev["idle"], total - _cpu_prev["total"]
    _cpu_prev.update(idle=idle, total=total)
    return round(100 * (dt - di) / dt, 1) if dt > 0 and _cpu_prev["total"] else 0.0

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
    """Per-interval RAPL watts, or {} when unavailable. Keys: cpu_w (psys if present
    else sum of package-* domains), dram_w (sum of dram sub-domains), domains{name:w}.
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
    drams = [w for n, w in per.items() if n == "dram" or n.endswith(":dram")]
    dram_w = round(sum(drams), 1) if drams else None
    return {"cpu_w": (round(cpu_w, 1) if cpu_w is not None else None),
            "dram_w": dram_w, "domains": {n: round(w, 1) for n, w in per.items()}}

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
_DOCKER_DISK_TTL = 1800
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
    skipped, and so are sources in `shared` — a bind/volume mounted by more than
    one container (e.g. a common /srv/share) belongs to none of them, and counting
    it under each both double-counts and makes unrelated rows show the same huge
    number. Falls back toward SizeRw alone when host paths can't be measured —
    e.g. HOST_ROOT not mounted — which is the old behaviour.

    `prev` is this container's last computed total: if a mount's `du` overruns
    its timeout we keep the larger of (partial new total, prev) so a transient
    slow scan never makes the number shrink or blink to zero."""
    total = ct.get("SizeRw") or 0
    incomplete = False
    for m in (ct.get("Mounts") or []):
        if not m.get("RW") or m.get("Type") not in ("volume", "bind"):
            continue
        src = m.get("Source")
        if not src or not src.startswith("/") or src in shared:
            continue
        sz = _dir_size(src)
        if sz is None:
            incomplete = True
        else:
            total += sz
    if incomplete and prev is not None:
        return max(total, prev)
    return total

def _shared_mount_sources(sized):
    """Sources (volume/bind) mounted read-write by more than one container — i.e.
    shared infrastructure that shouldn't be attributed to any single container."""
    users = {}
    for ct in sized:
        seen = set()
        for m in (ct.get("Mounts") or []):
            if m.get("RW") and m.get("Type") in ("volume", "bind"):
                src = m.get("Source")
                if src and src not in seen:        # a container mounting it twice still counts once
                    seen.add(src)
                    users[src] = users.get(src, 0) + 1
    return frozenset(src for src, n in users.items() if n > 1)

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
    # Per-container GPU VRAM, attributed by the GPU sampler (nvidia-smi
    # compute-apps → /proc/<pid>/cgroup → container name). procs is in MB and
    # keyed by service name (== container name for container-owned PIDs); host /
    # unattributed PIDs use "host:"/"pid:" keys that never match a container.
    vram_mb = {p.get("service"): p.get("mem") for p in (LATEST.get("procs") or [])}
    for c in items:
        e = _docker_enrich["data"].get(c["id"]) or {}
        c["mem_bytes"]  = e.get("mem_bytes")
        vmb = vram_mb.get(c["name"])
        c["vram_bytes"] = round(vmb * 1048576) if vmb else None
        c["disk_bytes"] = _docker_disk["data"].get(c["id"])
    # Image-update awareness — fold the cached per-container status in (no network
    # here; the background worker owns the registry queries). Off => all absent.
    img_count = 0
    try:
        iu = image_updates_snapshot()
        if iu.get("enabled"):
            by_id = {r.get("id"): r for r in (iu.get("results") or [])}
            for c in items:
                r = by_id.get(c["id"])
                c["image_update"] = (r or {}).get("status")   # up_to_date/update_available/unknown/None
            img_count = int(iu.get("count") or 0)
    except Exception:
        pass
    rank = {"crit": 0, "warn": 1, "ok": 2, "info": 3}
    items.sort(key=lambda c: (rank.get(c["status"], 9), c["name"].lower()))
    return {"available": True, "containers": items,
            "summary": {"total": len(items),
                        "running": sum(1 for c in items if c["state"] == "running"),
                        "updates_available": img_count,
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
    if LATEST.get("gpu_avail"):
        _diag(checks, "nvidia", "NVIDIA GPU", "ok",
              f"nvidia-smi OK · {round(LATEST.get('mem_total') or 0)} MB VRAM")
    else:
        _diag(checks, "nvidia", "NVIDIA GPU", "info",
              "no GPU detected — GPU panels are hidden (everything else works)",
              {"where": "on the host — only if it actually has an NVIDIA GPU",
               "cmd": "sudo nvidia-ctk runtime configure --runtime=docker --set-as-default\n"
                      "sudo systemctl restart docker"})
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

# ── Per-process disk-I/O attribution (the "what was writing heavily" answer) ────
# Real block-layer bytes per process from /proc/<pid>/io (read_bytes/write_bytes —
# the counters the kernel charges to actual device I/O, not page-cache hits). We
# sample ONLY the bounded top-N candidate set the monitor already computes (by CPU
# delta + by RAM), one small read each per poll — never a full /proc scan. Deltas
# across polls give per-process read_B/s + write_B/s; prev state is keyed by pid and
# guarded by the process start-time so a recycled pid can't inherit a stale counter.
# /proc/<pid>/io needs matching privilege: unreadable (PermissionError/missing) →
# degrade silently to no attribution (Linux-only; absent on the dev box / Windows).
_PROC_IO_PREV = {}   # pid(str) -> (starttime:int, read_bytes:int, write_bytes:int, ts:float)

def _fmt_bps(b):
    """Human byte-rate for deterministic Copilot text: MB/s, KB/s or B/s. Never raises."""
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
    top-by-CPU / top-by-RAM pids only, so cost stays O(top-N) reads, not O(all pids).
    Reads /proc/<pid>/io for each, computes B/s from the delta vs the previous poll,
    guarding pid reuse (start-time mismatch → reset) and counter resets/negatives
    (→ drop the sample). Returns {"available": False} when /proc/<pid>/io is
    unreadable for EVERY candidate (no privilege / non-Linux) so the feature is
    simply absent; otherwise the top writer/reader plus short leader lists. Never
    surfaces cmdline/argv — only the process comm (which the caller sanitises)."""
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
            continue                                   # pid recycled → drop stale delta
        dt = now - p_ts
        if dt <= 0:
            continue
        d_rb, d_wb = rb - p_rb, wb - p_wb
        if d_rb < 0 or d_wb < 0:
            continue                                   # counter reset/wrap → skip
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

def _total_cpu_jiffies():
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]   # cpu  user nice system idle iowait …
    return sum(int(x) for x in parts)

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
    # CPU delta and top-N by RAM. This reuses the selection the monitor already runs
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
    return {"by_cpu": sorted(rows, key=lambda r: -r["cpu_pct"])[:top_n],
            "by_mem": sorted(rows, key=lambda r: -r["mem_mb"])[:top_n],
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

# ── Disk I/O throughput / utilisation / latency (issue #196, done our way) ────
# Read-only, pure-stdlib per-device block-device stats from /proc/diskstats. We
# keep the human's compat response shape (available / summary / items with
# read_mb_s + write_mb_s, loop*/ram*/sr* filtered, sorted by total desc) and
# ENRICH each device with utilisation% and average per-op latency, then feed the
# result into history (disk_io_samples), the z-score anomaly bundle and Copilot.
#
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
# These match the human's existing use of parts[5]/parts[9] for the sector
# counters; the extra indices were confirmed against the kernel doc.
_disk_io_prev = {}
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
    path = os.path.join(HOST_ROOT, "proc/diskstats")
    if not os.path.exists(path):
        path = "/proc/diskstats"
    if not os.path.exists(path):
        return {"available": False, "warming_up": False, "reason": "no /proc/diskstats",
                "summary": {"total_read_mb_s": 0.0, "total_write_mb_s": 0.0}, "items": []}
    now = time.time()
    out = []
    try:
        with open(path, "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 14:
                    continue
                dev = parts[2]
                if dev.startswith("loop") or dev.startswith("ram") or dev.startswith("sr"):
                    continue
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
                prev = _disk_io_prev.get(dev)
                _disk_io_prev[dev] = (s_read, s_write, reads, writes,
                                      ms_read, ms_write, ms_io, now)
                if not prev:
                    continue                       # first poll for this device: warm up
                dt = now - prev[7]
                if dt <= 0:
                    continue
                rmb = ((s_read  - prev[0]) * _SECTOR_BYTES) / 1048576.0 / dt
                wmb = ((s_write - prev[1]) * _SECTOR_BYTES) / 1048576.0 / dt
                d_reads   = reads    - prev[2]
                d_writes  = writes   - prev[3]
                d_msread  = ms_read  - prev[4]
                d_mswrite = ms_write - prev[5]
                d_msio    = ms_io    - prev[6]
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
    # overstated the headline KPI ~3-4x on stacked (md-RAID) hosts.
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
    with LOCK:
        rows = DB.execute("SELECT name, ssh_target, tags, added_at, last_check_at, last_check_json, "
                          "poll_timeout, poll_calibrated_at FROM hosts ORDER BY added_at").fetchall()
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
    if not _HOST_NAME_RE.match(name or ""):
        return None, "Name must be 1–31 chars: letters, digits, '_' or '-', starting with a letter or digit."
    if _parse_ssh_target(ssh_target) is None:
        return None, "SSH target must look like user@host or user@host:port."
    with LOCK:
        try:
            DB.execute("INSERT INTO hosts(name, ssh_target, tags, added_at) VALUES(?,?,?,?)",
                       (name, ssh_target.strip(), (tags or "").strip(), int(time.time())))
            DB.commit()
        except sqlite3.IntegrityError:
            return None, f"A host named '{name}' already exists."
    return {"name": name, "ssh_target": ssh_target.strip()}, None

def delete_host(name):
    with LOCK:
        cur = DB.execute("DELETE FROM hosts WHERE name=?", (name,))
        DB.commit()
    return cur.rowcount > 0

def update_host(name, ssh_target=None, tags=None):
    """Patch an existing host. Returns (host_dict, error_or_None). The cached
    last-check result is cleared because the old probe no longer applies to the
    new target."""
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
        cur = DB.execute(f"UPDATE hosts SET {','.join(fields)} WHERE name=?", params)
        DB.commit()
    if cur.rowcount == 0:
        return None, f"No host named '{name}'."
    with LOCK:
        row = DB.execute("SELECT name, ssh_target, tags FROM hosts WHERE name=?",
                         (name,)).fetchone()
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
    with LOCK:
        DB.execute("UPDATE hosts SET last_check_at=?, last_check_json=? WHERE name=?",
                   (int(time.time()), json.dumps(result), name))
        DB.commit()

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

def probe_host_metrics(user, host, port, family="linux", timeout=HOST_POLL_TIMEOUT):
    """Run the right probe on the remote via SSH stdin. Returns
    (data, error, elapsed_ms, timed_out). Windows hosts get the PowerShell probe;
    everything else gets probe.py — both emit the same JSON shape, so the caller
    and the UI don't branch on OS. `timed_out` is True only when ssh hit the
    timeout (rc 124), which is what the adaptive calibration keys off (#99)."""
    if family == "windows":
        if not _PROBE_PS_SCRIPT:
            return None, "probe.ps1 not packaged in this image", 0, False
        rc, out, err, ms = _ssh_with_stdin(user, host, port, _WIN_PS_CMD,
                                           _PROBE_PS_SCRIPT, timeout=timeout)
    else:
        if not _PROBE_SCRIPT:
            return None, "probe.py not packaged in this image", 0, False
        rc, out, err, ms = _ssh_with_stdin(user, host, port, "python3 -",
                                           _PROBE_SCRIPT, timeout=timeout)
    if rc != 0:
        return None, _clean_ssh_err(err, out, rc), ms, rc == 124
    # lstrip a UTF-8 BOM: PowerShell over SSH can prepend one, which str.strip()
    # won't remove and which would otherwise make json.loads choke on char 0.
    out = (out or "").lstrip("﻿").strip()
    if not out:
        return None, "empty response from probe", ms, False
    try:
        return json.loads(out), None, ms, False
    except Exception as e:
        return None, f"bad JSON from probe: {e}", ms, False

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
        # Additive vendor tag for the fleet-row badge — mirrors the probe.py
        # remote shape. Representative = first card's vendor (single-vendor rigs
        # are the common case; mixed rigs surface per-card vendors in the GPU tab).
        _gl = (LATEST or {}).get("gpus") or []
        if _gl and _gl[0].get("vendor"):
            out["gpu"]["vendor"] = _gl[0]["vendor"]
    return out

# ── Adaptive per-host poll timeout (issue #99) ────────────────────────────────
def _host_poll_state(name):
    """(timeout, fails) for a host — timeout falls back to the global default."""
    try:
        with LOCK:
            row = DB.execute("SELECT poll_timeout, poll_fails FROM hosts WHERE name=?", (name,)).fetchone()
    except Exception:
        return HOST_POLL_TIMEOUT, 0
    t = row[0] if row and row[0] else None
    return (int(t) if t else HOST_POLL_TIMEOUT), (int(row[1]) if row and row[1] else 0)

def _host_poll_save(name, timeout=None, fails=None, calibrated=False):
    sets, params = [], []
    if timeout is not None:   sets.append("poll_timeout=?");       params.append(int(timeout))
    if fails is not None:      sets.append("poll_fails=?");          params.append(int(fails))
    if calibrated:             sets.append("poll_calibrated_at=?");  params.append(int(time.time()))
    if not sets:
        return
    params.append(name)
    try:
        with LOCK:
            DB.execute(f"UPDATE hosts SET {','.join(sets)} WHERE name=?", params)
            DB.commit()
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

def host_poller():
    """Loop: probe every registered host whose last Test was healthy. A per-host
    adaptive timeout (issue #99) isolates slow remotes — they self-calibrate to a
    working budget instead of going permanently dark, while fast hosts stay at the
    15s default. Errors are kept on the cache row so the UI can show a last error."""
    # Stagger the first run a touch so we don't fire before the app is fully up.
    time.sleep(2)
    while True:
        try:
            for h in list_hosts():
                check = h.get("last_check") or {}
                if (check.get("summary") or {}).get("overall") not in ("ok", "warn"):
                    continue
                parsed = _parse_ssh_target(h["ssh_target"])
                if not parsed:
                    continue
                u, host, port = parsed
                fam = ((check.get("os") or {}).get("family")) or "linux"
                data, err = _poll_and_adapt(h["name"], u, host, port, fam)
                with HOST_DATA_LOCK:
                    entry = HOST_DATA.get(h["name"], {})
                    if data:
                        entry["data"]  = data
                        entry["at"]    = int(time.time())
                        entry["error"] = None
                    else:
                        entry["error"]      = err or "unknown error"
                        entry["error_at"]   = int(time.time())
                    HOST_DATA[h["name"]] = entry
        except Exception as e:
            print("host_poller error:", e, flush=True)
        time.sleep(INTERVAL)

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

def probe_host(name):
    """Run the capability checklist against a registered host. Each check returns
    {id, label, status: ok|warn|fail|info, detail, remedy?}. The full result is
    cached on the host row so the UI can show the last-known state even when
    the user hasn't re-tested."""
    with LOCK:
        row = DB.execute("SELECT ssh_target FROM hosts WHERE name=?", (name,)).fetchone()
    if not row:
        return None
    parsed = _parse_ssh_target(row[0])
    if not parsed:
        result = {"checks": [{"id": "parse", "label": "SSH target", "status": "fail",
                              "detail": f"could not parse '{row[0]}'"}]}
        result["summary"] = _summarize(result["checks"])
        _record_check(name, result)
        return result
    user, host, port = parsed
    _ensure_ssh_keypair()
    checks = []
    os_info = {}

    # 1) SSH connect.  Pre-probe TCP-22 so we can give a clear "port closed"
    # answer instead of letting ssh time out cryptically.
    tcp_ok, tcp_err = _tcp_probe(host, port)
    if not tcp_ok:
        item = {"id": "connect", "label": "SSH reachable", "status": "fail",
                "detail": f"port {port} not reachable: {tcp_err}",
                "debug": tcp_err,
                "remedy": _remedy_sshd_check()}
        checks.append(item)
        result = {"checks": checks, "summary": _summarize(checks), "os": {}}
        _record_check(name, result)
        return result

    rc, out, err, ms = _ssh(user, host, port, "echo ok", timeout=SSH_CONNECT_TIMEOUT + 2)
    if rc == 0 and out == "ok":
        checks.append({"id": "connect", "label": "SSH reachable",
                       "status": "ok", "detail": f"port {port}, {ms} ms"})
    else:
        msg = _clean_ssh_err(err, out, rc)
        hint = None
        e = (err or "")
        if "Permission denied" in e:
            hint = _remedy_pubkey(user)
        elif "Host key verification failed" in e:
            hint = {"where": "on the hub (this container)",
                    "cmd": f"# Clear the saved host key and re-test:\nrm {SSH_KNOWN_HOSTS}"}
        elif "Could not resolve hostname" in e or "Name or service not known" in e:
            hint = {"where": "on the hub (this container)",
                    "cmd": "# Hostname did not resolve. Try the IP, or check the container's DNS."}
        elif rc == 124:
            hint = _remedy_sshd_down(None)
        item = {"id": "connect", "label": "SSH reachable", "status": "fail",
                "detail": msg, "debug": (err or "")[:2000]}
        if hint: item["remedy"] = hint
        checks.append(item)
        result = {"checks": checks, "summary": _summarize(checks), "os": {}}
        _record_check(name, result)
        return result

    # SSH connected — detect the remote OS so the rest of the checklist can
    # produce remedies that match the actual distro (and so the UI can show an
    # OS badge per host).
    os_info = _detect_os(user, host, port)
    if (os_info.get("family") or "") == "macos":
        # Be honest about limitations: macOS doesn't expose /proc, doesn't have
        # systemd, doesn't expose the Docker socket the same way, and has no
        # nvidia-smi. We still record the OS but mark the rest as info.
        checks.append({"id": "os", "label": "Detected OS", "status": "info",
                       "detail": os_info.get("label") or "macOS"})
        checks.append({"id": "proc",   "label": "/proc readable",   "status": "info",
                       "detail": "Linux-only — macOS doesn't expose /proc"})
        checks.append({"id": "docker", "label": "Docker socket",    "status": "info",
                       "detail": "macOS Docker socket flow not supported in v0"})
        checks.append({"id": "dbus",   "label": "systemd D-Bus",    "status": "info",
                       "detail": "macOS uses launchd, not systemd"})
        checks.append({"id": "nvidia", "label": "nvidia-smi",       "status": "info",
                       "detail": "no NVIDIA driver path on macOS"})
        result = {"checks": checks, "summary": _summarize(checks), "os": os_info}
        _record_check(name, result)
        return result

    if (os_info.get("family") or "") == "windows":
        # Windows host: same checklist, Windows-native checks. All run through
        # PowerShell-over-stdin so cmd.exe quoting can't bite us. The probe is the
        # PowerShell one (probe.ps1); these checks just gate it and drive the UI.
        checks.append({"id": "os", "label": "Detected OS", "status": "ok",
                       "detail": os_info.get("label") or "Windows"})
        # /proc analogue: can we read host state via WMI/CIM?
        _, out, err, _ = _ssh_with_stdin(user, host, port, _WIN_PS_CMD,
            b"\"up=$([int]((Get-Date)-(Get-CimInstance Win32_OperatingSystem).LastBootUpTime).TotalSeconds)\"",
            timeout=10)
        if out and "up=" in out:
            checks.append({"id": "proc", "label": "Host readable (WMI)", "status": "ok",
                           "detail": f"uptime {out.strip().split('up=')[-1][:12]}s"})
        else:
            checks.append({"id": "proc", "label": "Host readable (WMI)", "status": "warn",
                           "detail": (err or "no reply from PowerShell")[:160]})
        # Docker on Windows: only if the CLI is on PATH (Docker Desktop, or a WSL
        # engine exposed to Windows). Honest "info" otherwise.
        _, out, err, _ = _ssh_with_stdin(user, host, port, _WIN_PS_CMD,
            b"try{(docker version --format '{{.Server.Version}}')}catch{''}", timeout=12)
        dv = (out or "").strip().splitlines()[-1].strip() if (out or "").strip() else ""
        if dv and "." in dv and "error" not in dv.lower() and "cannot" not in dv.lower():
            checks.append({"id": "docker", "label": "Docker", "status": "ok", "detail": f"server v{dv}"})
        else:
            checks.append({"id": "docker", "label": "Docker", "status": "info",
                           "detail": "Docker CLI not on PATH — container panel hidden for this host"})
        # systemd D-Bus analogue: Windows services are always queryable.
        checks.append({"id": "dbus", "label": "Windows services", "status": "ok",
                       "detail": "Get-Service available"})
        # nvidia-smi works on Windows too when the driver is installed.
        _, out, _, _ = _ssh_with_stdin(user, host, port, _WIN_PS_CMD,
            b"if(Get-Command nvidia-smi -EA SilentlyContinue){(nvidia-smi --query-gpu=name --format=csv,noheader -i 0)}else{'missing'}",
            timeout=10)
        nv = (out or "").strip().splitlines()[-1].strip() if (out or "").strip() else ""
        if nv and nv != "missing":
            checks.append({"id": "nvidia", "label": "nvidia-smi", "status": "ok", "detail": nv[:120]})
        else:
            checks.append({"id": "nvidia", "label": "nvidia-smi", "status": "info",
                           "detail": "not found — GPU panel will be hidden for this host"})
        result = {"checks": checks, "summary": _summarize(checks), "os": os_info}
        _record_check(name, result)
        return result

    if os_info.get("label"):
        checks.append({"id": "os", "label": "Detected OS", "status": "ok",
                       "detail": os_info["label"]})

    # 2) /proc readable
    rc, out, err, _ = _ssh(user, host, port, "head -n1 /proc/uptime 2>&1")
    if rc == 0 and out:
        try:
            up = float(out.split()[0])
            detail = f"uptime {int(up)}s"
        except Exception:
            detail = out[:60]
        checks.append({"id": "proc", "label": "/proc readable", "status": "ok", "detail": detail})
    else:
        checks.append({"id": "proc", "label": "/proc readable", "status": "warn",
                       "detail": (err or "unexpected reply")[:160]})

    # 3) Docker socket
    rc, out, err, _ = _ssh(user, host, port,
                           "docker version --format '{{.Server.Version}}' 2>&1 || true")
    out_l = (out or "").lower()
    err_l = (err or "").lower()
    if rc == 0 and out and not any(s in out_l for s in
                                   ("permission denied", "cannot connect", "error", "command not found")):
        checks.append({"id": "docker", "label": "Docker socket", "status": "ok",
                       "detail": f"server v{out}"})
    elif "permission denied" in out_l or "permission denied" in err_l:
        checks.append({"id": "docker", "label": "Docker socket", "status": "warn",
                       "detail": f"permission denied — '{user}' not in the docker group",
                       "remedy": _remedy_docker_group(user, os_info)})
    elif "command not found" in out_l or "command not found" in err_l or out_l == "":
        checks.append({"id": "docker", "label": "Docker socket", "status": "info",
                       "detail": "Docker not installed — container panel will be hidden for this host"})
    else:
        checks.append({"id": "docker", "label": "Docker socket", "status": "warn",
                       "detail": (out or err or "unknown error")[:160]})

    # 4) systemd D-Bus
    _, out, _, _ = _ssh(user, host, port,
                        "[ -S /run/dbus/system_bus_socket ] && echo ok || echo missing")
    if out == "ok":
        checks.append({"id": "dbus", "label": "systemd D-Bus", "status": "ok",
                       "detail": "/run/dbus/system_bus_socket present"})
    else:
        checks.append({"id": "dbus", "label": "systemd D-Bus", "status": "info",
                       "detail": "not present — services panel will be hidden for this host"})

    # 5) nvidia-smi
    _, out, _, _ = _ssh(user, host, port,
                        "command -v nvidia-smi >/dev/null && "
                        "nvidia-smi --query-gpu=name --format=csv,noheader -i 0 2>/dev/null | head -1 || echo missing")
    if out and out != "missing":
        checks.append({"id": "nvidia", "label": "nvidia-smi", "status": "ok", "detail": out[:120]})
    else:
        checks.append({"id": "nvidia", "label": "nvidia-smi", "status": "info",
                       "detail": "not found — GPU panel will be hidden for this host"})

    result = {"checks": checks, "summary": _summarize(checks), "os": os_info}
    _record_check(name, result)
    return result

def run_on_host(name, cmd, sudo_password=None):
    """Execute `cmd` on a registered host. If `sudo_password` is provided, the
    whole command is wrapped in `sudo -S -p '' bash -c <cmd>` and the password
    is piped via stdin to sudo on the remote — it never appears in argv on
    either the local or remote side, and we never log it. Returns:
    {ok, exit_code, stdout, stderr, ms}."""
    with LOCK:
        row = DB.execute("SELECT ssh_target FROM hosts WHERE name=?", (name,)).fetchone()
    if not row:
        return None
    parsed = _parse_ssh_target(row[0])
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

# Generate the hub keypair eagerly so /api/hub/pubkey is instant on first hit.
_ensure_ssh_keypair()

SETTING_DEFAULTS = {
    "alerts_enabled":     "0",       # "0" / "1"
    "discord_webhook_url": "",
    "ntfy_topic":          "",
    "ntfy_server":         "https://ntfy.sh",
    "telegram_token":      "",
    "telegram_chat_id":    "",
    "webhook_url":         "",         # generic outbound webhook (POST JSON) for the rule engine
    # ── Slack incoming-webhook channel — OFF by default, inert until set ────────
    "slack_webhook_url":   "",         # SECRET — Slack incoming webhook (POST JSON); blank => off
    # ── Email (SMTP) channel — OFF by default, inert until host+from+to set ─────
    # SEND-ONLY: the monitor connects out to an SMTP relay to deliver alerts. It
    # never receives mail and never mutates the host. Pure-stdlib smtplib/email.
    "smtp_host":           "",         # SMTP server hostname/IP (blank => off, even if other fields set)
    "smtp_port":           "587",      # SMTP TCP port (587 STARTTLS typical, 465 implicit TLS, 25 plain)
    "smtp_user":           "",         # optional login username (not a secret on its own)
    "smtp_pass":           "",         # SECRET — login password; redacted like other secrets
    "smtp_from":           "",         # envelope/From address
    "smtp_to":             "",         # recipient(s), comma-separated
    "smtp_tls":            "1",        # "1" => STARTTLS (default) / "0" => plain
    "alert_min_level":     "warning",  # "warning" or "critical"
    "disk_alert_pct":      "90",       # disk usage % that trips an alert
    # ── SLO / error-budget target for uptime checks (display + digest only) ─────
    # A monthly availability SLO as a PERCENT string (e.g. "99.9"). Drives the
    # error-budget + burn-rate view on the uptime tiles and a digest mention when
    # a check is over budget / burning hot. Empty/garbage falls back to 99.9; a
    # value of "100" means no error budget (any failure shows as over budget).
    "slo_target":          "99.9",
    "kwh_price":           "",         # electricity price per kWh (day/peak in dual mode); empty hides the cost card (#25)
    "currency":            "$",        # symbol shown next to costs
    # ── dual (day/night) tariff — revamp ──────────────────────────────────────
    "tariff_mode":         "single",   # "single" | "dual"  (default = original flat behaviour)
    "kwh_price_night":     "",         # night price per kWh; blank => silently behaves as single
    "night_start":         "22:00",    # local time-of-day "HH:MM"; window may wrap midnight
    "night_end":           "06:00",    # local time-of-day "HH:MM"
    "country":             "",         # ISO-3166 alpha-2 — UI prefill memo only; backend never resolves it
    "system_idle_watts":   "",         # optional operator baseline (mainboard/fans/PSU/disks); blank => "other" omitted, never a guessed wall figure
    # ── Local-vs-cloud inference savings (E1) ──────────────────────────────────
    # The $/1k OUTPUT tokens a comparable hosted API would charge (GPT-4o-mini-tier
    # default). Used ONLY to price the cloud-equivalent of LOCAL generations in the
    # AI Models savings card. Empty/0 => the cloud comparison gracefully hides, just
    # like an empty kwh_price hides the cost card. Never affects the Costs tab.
    "cloud_cost_per_1k":   "0.15",     # $ per 1k output tokens (hosted-API reference rate)
    # ── Integrations / experiment-tracking API (push/pull) ────────────────────
    "api_key":             "",         # Bearer/X-API-Key for run ingest; empty => not generated (ingest fail-closed)
    "mlflow_uri":          "",         # MLflow tracking server base (blank = off)
    "mlflow_token":        "",         # optional bearer for a secured MLflow
    # ── Scheduled Lab Copilot digest (E1) — OFF by default, inert until enabled ─
    # A plain-English daily summary built by the Copilot and PUSHED through the
    # existing alert channels. Reuses the channel dispatch + the digest builder.
    "digest_enabled":      "0",        # "0" / "1" — master switch (off => zero new behaviour)
    "digest_time":         "08:00",    # local time-of-day "HH:MM" to push the daily/weekly digest
    "digest_channel":      "all",      # which configured channel: all/discord/ntfy/telegram/webhook
    "digest_cadence":      "daily",    # "daily" | "weekly" — how often the digest fires
    "digest_weekday":      "0",        # 0=Mon … 6=Sun — the day weekly digests fire (ignored for daily)
    "digest_last_sent":    "",         # internal: "YYYY-MM-DD" of the last send (edge-trigger guard)
    # ── Home Assistant / MQTT auto-discovery (E4) — OFF by default, inert until enabled ─
    # PUBLISH-ONLY: the monitor pushes its key metrics out to an MQTT broker with
    # HA discovery payloads so the lab shows up as native HA sensors. It NEVER
    # subscribes (no inbound command path = no remote-control attack surface) and
    # NEVER mutates the host. Pure-stdlib socket/ssl client. When mqtt_enabled=0
    # or no host: the publisher thread idles — ZERO connections, ZERO new behaviour.
    "mqtt_enabled":        "0",        # "0" / "1" — master switch
    "mqtt_host":           "",         # broker hostname/IP (blank => off, even if enabled)
    "mqtt_port":           "1883",     # broker TCP port (8883 typical for TLS)
    "mqtt_user":           "",         # optional username (not a secret on its own)
    "mqtt_pass":           "",         # SECRET — broker password; redacted like other secrets
    "mqtt_tls":            "0",        # "0" / "1" — wrap the socket in TLS
    "mqtt_prefix":         "homeassistant",  # HA discovery topic prefix
    "mqtt_interval_sec":   "30",       # seconds between state publishes (min ~10)
    # ── Docker image-update awareness (What's-Up-Docker style) — OFF by default ──
    # AWARENESS ONLY: compares each running container's deployed image digest with
    # the upstream registry's current manifest digest. NEVER pulls/runs/restarts/
    # deletes anything. When off (default): zero outbound, zero new behaviour. When
    # on: a slow, heavily-cached, bounded background poller (see IMG_CHECK_* envs).
    "image_update_check":        "0",        # "0" / "1" — master switch
    "image_update_interval_sec": "21600",    # seconds between full re-checks (~6h; min ~1h enforced)
    # ── Auto-explain new incidents (E1) — OFF by default, inert until enabled ────
    # When "1", a newly-OPENED correlated incident gets a Lab-Copilot explanation
    # generated automatically — but ONLY on a dedicated, decoupled worker thread,
    # NEVER on the metrics/collector poll path. Rate-limited, de-duplicated, and a
    # no-op when the local LLM is unreachable. When "0" (default): zero new
    # behaviour — explanations are only ever produced by an explicit user action.
    "incident_auto_explain":     "0",        # "0" / "1" — master switch
    # ── Probable-cause line in incident notifications (E1) — OFF by default ──────
    # When "1", incident alert notifications carry a concise "🧠 Probable cause: …"
    # line built from the CACHED ai_explanation (never generated on the dispatch/poll
    # path), and — when incident_auto_explain is ALSO "1" — the dedicated auto-explain
    # worker sends ONE supplementary "probable cause" notification per fresh incident
    # after it persists an explanation (still-open, dedup'd, suppression-respecting).
    # When "0" (default): notification behaviour is byte-identical to before.
    "notify_ai_cause":           "0",        # "0" / "1" — opt-in
    # ── AI incident postmortem on resolution (E1) — OFF by default ──────────────
    # When "1", a correlated incident that RESOLVES (state→cleared) gets a short
    # structured postmortem auto-generated — but ONLY on the dedicated off-poll
    # postmortem worker (a cheap enqueue AFTER the DB LOCK is released, never inside
    # evaluate_incidents/collect/health_scan/the sample loop), at most once per
    # incident. When "0" (default): zero new auto behaviour — a postmortem is only
    # ever produced by the explicit "Generate postmortem" action in the drawer. Its
    # off-poll discipline mirrors incident_auto_explain exactly.
    "incident_auto_postmortem":  "0",        # "0" / "1" — master switch
}
SETTING_SECRETS = {"discord_webhook_url", "telegram_token", "api_key", "mlflow_token", "webhook_url", "mqtt_pass", "slack_webhook_url", "smtp_pass"}   # never round-tripped to the UI in full (generic webhook URLs embed Slack/n8n/HA secrets)

def get_settings():
    """Return the full settings dict (defaults + persisted overrides)."""
    out = dict(SETTING_DEFAULTS)
    try:
        with LOCK:
            rows = DB.execute("SELECT key, value FROM settings").fetchall()
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
_URL_SETTING_KEYS = {"discord_webhook_url", "ntfy_server", "webhook_url", "slack_webhook_url"}

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

def save_settings(updates):
    """Persist any subset of known setting keys. Unknown keys are ignored."""
    safe = [(k, "" if v is None else str(v)) for k, v in updates.items() if k in SETTING_DEFAULTS]
    if not safe:
        return
    with LOCK:
        DB.executemany("INSERT INTO settings(key,value) VALUES(?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", safe)
        DB.commit()

# ── Notifier: Discord webhook + ntfy.sh + Telegram ─────────────────────────
# Edge-triggered: each alert key is remembered in _NOTIFIED so a flapping state
# doesn't spam the channel. A key clears when the underlying condition recovers
# (container becomes healthy again, disk drops below threshold, etc.), so the
# next failure re-fires exactly once.
_NOTIFIED = {}            # key -> 1, "armed" alerts pending recovery
_NOTIFIER_LOCK = threading.Lock()
# Cap on persisted notifier edge-state rows. The keyspace is dynamic (container /
# systemd / disk names, plus per-event oom:SVC:TS keys), so unlike the rule engine
# it isn't naturally bounded by a fixed table — an armed key clears on recovery, but
# a churn of ephemeral container names or OOM events could accumulate. We prune the
# oldest rows past this cap on restore so the table can never grow without bound.
_NOTIFIED_STATE_CAP = 500
# Prune the notified_state table back to the cap at runtime once every N inserts,
# not on every single insert — the per-event OOM keys (`oom:SVC:TS`) are never
# _clear()ed, so within one long-lived process the table would otherwise grow
# unbounded until the next restart-time prune. An occasional bounded prune keeps
# it from ever materially exceeding the cap while staying cheap on the hot path.
_NOTIFIED_PRUNE_EVERY = 50
_notified_insert_count = 0

def _prune_notified_state():
    """Delete the oldest rows so the table holds at most _NOTIFIED_STATE_CAP rows.
    Cheap no-op when already under the cap. Defensive: a prune failure must NEVER
    break alert evaluation/dispatch. Caller must NOT hold LOCK."""
    try:
        with LOCK:
            over = DB.execute(
                "SELECT COUNT(*) FROM notified_state").fetchone()[0] - _NOTIFIED_STATE_CAP
            if over > 0:
                DB.execute(
                    "DELETE FROM notified_state WHERE key IN "
                    "(SELECT key FROM notified_state ORDER BY notified_at ASC LIMIT ?)",
                    (over,))
                DB.commit()
    except Exception as e:
        print("notified_state prune error:", e, flush=True)

def _persist_notified(key, on):
    """Write-through one notifier edge-state key to SQLite. Defensive: a persistence
    failure must NEVER break alert evaluation/dispatch — the in-memory `_NOTIFIED`
    dict remains the source of truth for the running process; the table is only a
    restart-durable mirror. Caller already holds _NOTIFIER_LOCK."""
    global _notified_insert_count
    try:
        with LOCK:
            if on:
                DB.execute("INSERT INTO notified_state(key,notified_at) VALUES(?,?) "
                           "ON CONFLICT(key) DO NOTHING", (key, int(time.time())))
            else:
                DB.execute("DELETE FROM notified_state WHERE key=?", (key,))
            DB.commit()
        # Occasional runtime prune so per-event keys (OOM) can't grow the table
        # unbounded across a long uptime. Bounded work, off the per-insert path.
        if on:
            _notified_insert_count += 1
            if _notified_insert_count % _NOTIFIED_PRUNE_EVERY == 0:
                _prune_notified_state()
    except Exception as e:
        print("notified_state persist error:", e, flush=True)

def restore_notified_state():
    """On startup, hydrate the in-memory `_NOTIFIED` dict from SQLite so the first
    post-restart notifier scan does NOT treat an already-fired-and-still-true
    condition as a fresh edge (which would re-fire a duplicate notification). Prunes
    the table to the newest _NOTIFIED_STATE_CAP rows first. Defensive: any failure
    just leaves _NOTIFIED empty (the pre-persistence behaviour)."""
    try:
        _prune_notified_state()
        with LOCK:
            rows = DB.execute("SELECT key FROM notified_state").fetchall()
        with _NOTIFIER_LOCK:
            for (k,) in rows:
                _NOTIFIED[k] = 1
        if rows:
            print(f"restored {len(rows)} armed notifier edge-state key(s)", flush=True)
    except Exception as e:
        print("notified_state restore error:", e, flush=True)
LEVELS  = {"info": 0, "warning": 1, "critical": 2}
# Every notification channel the rule engine / digest / test paths may target.
# "all" fans out to every configured channel; the rest are the individual senders.
_NOTIFY_CHANNELS = ("discord", "ntfy", "telegram", "webhook", "slack", "email")
_VALID_CHANNELS  = ("all",) + _NOTIFY_CHANNELS
_COLORS = {"info": 0x58A6FF, "warning": 0xD29922, "critical": 0xF85149}
# Slack attachment bar colour per level (hex strings; Slack wants "#rrggbb").
_SLACK_COLORS = {"info": "#58A6FF", "warning": "#D29922", "critical": "#F85149"}
_NTFY_P = {"info": 3, "warning": 4, "critical": 5}
_NTFY_T = {"info": "information_source", "warning": "warning", "critical": "rotating_light"}

# Some endpoints (Discord behind Cloudflare) 403 a request with no User-Agent.
# Always carry one on outbound notifications; callers may override via their headers.
_NOTIFY_UA = f"homelab-monitor/{VERSION}"

def _post_json(url, payload, timeout=5):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": _NOTIFY_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()

def _post_text(url, text, headers=None, timeout=5):
    hdr = dict(headers or {"Content-Type": "text/plain"})
    hdr.setdefault("User-Agent", _NOTIFY_UA)
    req = urllib.request.Request(url, data=text.encode("utf-8"), headers=hdr)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()

def send_discord(webhook, level, title, detail):
    payload = {"embeds": [{"title": title, "description": detail,
                           "color": _COLORS.get(level, _COLORS["info"]),
                           "footer": {"text": f"HomeLab Monitor · {level}"},
                           "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())}]}
    return _post_json(webhook, payload)

def send_ntfy(server, topic, level, title, detail):
    server = (server or "https://ntfy.sh").rstrip("/")
    url    = f"{server}/{urllib.parse.quote(topic, safe='')}"
    hdr    = {"Content-Type": "text/plain; charset=utf-8",
              "Title":    title.encode("ascii", "replace").decode("ascii"),
              "Priority": str(_NTFY_P.get(level, 3)),
              "Tags":     _NTFY_T.get(level, "information_source")}
    return _post_text(url, detail, hdr)

def _tg_escape(text):
    """Escape Telegram legacy-Markdown metacharacters in user-supplied text."""
    return (text or "").replace("\\", "\\\\").replace("_", "\\_").replace("*", "\\*") \
                       .replace("`", "\\`").replace("[", "\\[")

def _post_to_telegram(token, chat_id, level, title, body):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    text = (f"*{_tg_escape(title)}*\n\n{_tg_escape(body)}\n\n"
            f"_HomeLab Monitor · {level}_")
    return _post_json(url, {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})

def send_webhook(url, level, title, detail):
    """Generic outbound webhook: POST a flat JSON envelope. The receiver decides
    what to do with it (HA automation, n8n, a script, Slack-incoming, etc.)."""
    payload = {"source": "homelab-monitor", "level": level, "title": title,
               "detail": detail, "ts": int(time.time())}
    return _post_json(url, payload)

def send_slack(webhook, level, title, detail):
    """Slack incoming-webhook: a `text` fallback plus one level-coloured attachment
    so the message reads cleanly in the channel. Goes out via the shared _post_json
    (carries the User-Agent). Returns whatever _post_json returns; raises on error."""
    payload = {
        "text": f"{title}\n{detail}",
        "attachments": [{
            "color":    _SLACK_COLORS.get(level, _SLACK_COLORS["info"]),
            "title":    title,
            "text":     detail,
            "footer":   f"HomeLab Monitor · {level}",
            "ts":       int(time.time()),
        }],
    }
    return _post_json(webhook, payload)

def send_email(subject, body, level, *, host, port=587, user="", password="",
               sender="", to="", tls=True, timeout=10, html_body=None):
    """Deliver a plain-text alert via SMTP using stdlib smtplib + EmailMessage.
    STARTTLS when `tls`; login when user+password set. Returns (ok, err) and NEVER
    raises into the alert loop — and the error text never embeds the password.

    When `html_body` is provided the message is multipart/alternative: the plain-text
    `body` is the fallback (set_content) and the HTML is added via add_alternative,
    so HTML-capable clients render the brief while text-only clients fall back. Without
    `html_body` the message is a single-part text/plain (unchanged behaviour)."""
    try:
        recipients = [a.strip() for a in (to or "").split(",") if a.strip()]
        if not (host and sender and recipients):
            return (False, "not configured")
        msg = email.message.EmailMessage()
        msg["Subject"] = subject
        msg["From"]    = sender
        msg["To"]      = ", ".join(recipients)
        msg["X-HomeLab-Level"] = level
        msg.set_content(body)
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        with smtplib.SMTP(host, int(port or 587), timeout=timeout) as srv:
            srv.ehlo()
            if tls:
                srv.starttls()
                srv.ehlo()
            if user and password:
                srv.login(user, password)
            srv.send_message(msg, from_addr=sender, to_addrs=recipients)
        return (True, None)
    except Exception as e:
        # Scrub the configured password out of any exception text, just in case a
        # backend echoed it back (e.g. auth failures that quote the credential).
        err = str(e)
        if password:
            err = err.replace(password, "***")
        return (False, err)

# Which channels a given settings dict has wired up — drives the "channel"
# selector in the rule engine. "all" means "every configured channel".
def _configured_channels(s):
    out = []
    if s.get("discord_webhook_url"): out.append("discord")
    if s.get("ntfy_topic"):          out.append("ntfy")
    if s.get("telegram_token") and s.get("telegram_chat_id"): out.append("telegram")
    if s.get("webhook_url"):         out.append("webhook")
    if s.get("slack_webhook_url"):   out.append("slack")
    if s.get("smtp_host") and s.get("smtp_from") and s.get("smtp_to"): out.append("email")
    return out

def _send_one_channel(s, ch, level, title, detail, html_detail=None):
    """Send to a single named channel. Returns (ok, err). Raises nothing.

    `html_detail` (optional) is an email-client-safe HTML alternative used ONLY by the
    email channel (multipart/alternative); chat channels always get plain-text only so
    they never render raw HTML tags."""
    try:
        if ch == "discord":
            if not s.get("discord_webhook_url"): return (False, "not configured")
            send_discord(s["discord_webhook_url"], level, title, detail)
        elif ch == "ntfy":
            if not s.get("ntfy_topic"): return (False, "not configured")
            send_ntfy(s.get("ntfy_server") or "https://ntfy.sh", s["ntfy_topic"], level, title, detail)
        elif ch == "telegram":
            if not (s.get("telegram_token") and s.get("telegram_chat_id")): return (False, "not configured")
            _post_to_telegram(s["telegram_token"], s["telegram_chat_id"], level, title, detail)
        elif ch == "webhook":
            if not s.get("webhook_url"): return (False, "not configured")
            send_webhook(s["webhook_url"], level, title, detail)
        elif ch == "slack":
            if not s.get("slack_webhook_url"): return (False, "not configured")
            send_slack(s["slack_webhook_url"], level, title, detail)
        elif ch == "email":
            if not (s.get("smtp_host") and s.get("smtp_from") and s.get("smtp_to")):
                return (False, "not configured")
            ok, err = send_email(
                title, detail, level,
                host=s.get("smtp_host"), port=s.get("smtp_port") or 587,
                user=s.get("smtp_user") or "", password=s.get("smtp_pass") or "",
                sender=s.get("smtp_from"), to=s.get("smtp_to"),
                tls=(str(s.get("smtp_tls", "1")) != "0"),
                html_body=html_detail)
            return (ok, err)
        else:
            return (False, f"unknown channel {ch}")
        return (True, None)
    except Exception as e:
        return (False, str(e))

def dispatch_alert(s, level, title, detail, channel="all", html_detail=None):
    """Send to the requested channel(s). channel='all' fans out to every configured
    channel; otherwise just the one named. Returns list of (channel, ok, err).
    A channel that isn't configured is silently skipped under 'all'.

    `html_detail` (optional) is passed to the email channel ONLY (multipart/alternative
    HTML alternative); all other channels receive plain-text `detail` only."""
    if channel and channel != "all":
        hd = html_detail if channel == "email" else None
        ok, err = _send_one_channel(s, channel, level, title, detail, hd)
        return [(channel, ok, err)]
    out = []
    for ch in _configured_channels(s):
        hd = html_detail if ch == "email" else None
        ok, err = _send_one_channel(s, ch, level, title, detail, hd)
        out.append((ch, ok, err))
    return out

def _emit(s, key, level, title, detail):
    """Fire an alert once per edge. Skips below the configured min level."""
    if LEVELS.get(level, 0) < LEVELS.get(s.get("alert_min_level", "warning"), 1):
        return
    with _NOTIFIER_LOCK:
        if _NOTIFIED.get(key):
            return
        _NOTIFIED[key] = 1
        _persist_notified(key, True)
    for ch, ok, err in dispatch_alert(s, level, title, detail):
        if not ok:
            print(f"notifier {ch} error:", err, flush=True)

def _clear(key):
    with _NOTIFIER_LOCK:
        existed = _NOTIFIED.pop(key, None)
        if existed:
            _persist_notified(key, False)

def notify_scan():
    s = get_settings()
    if s.get("alerts_enabled") != "1":
        return
    if not _configured_channels(s):
        return

    # ── Docker containers: edge-trigger on crit/warn, clear on ok ─────────────
    docker = HEALTH.get("docker") or {}
    if docker.get("available"):
        for ct in docker.get("containers", []):
            name = ct.get("name", "?")
            key  = f"container:{name}"
            st   = ct.get("status")
            if st == "crit":
                _emit(s, key, "critical", f"🔴 Container {name} {ct.get('label','')}".strip(),
                      f"{name}: {ct.get('status_text','')}")
            elif st == "warn":
                _emit(s, key, "warning", f"🟠 Container {name} {ct.get('label','')}".strip(),
                      f"{name}: {ct.get('status_text','')}")
            elif st == "ok":
                _clear(key)

    # ── systemd units: edge-trigger on failed ─────────────────────────────────
    systemd = HEALTH.get("systemd") or {}
    if systemd.get("available"):
        for svc in systemd.get("services", []):
            name = svc.get("name", "?")
            key  = f"systemd:{name}"
            if svc.get("status") == "crit":
                _emit(s, key, "critical", f"🔴 systemd unit failed: {name}",
                      f"{name} — {svc.get('desc','')} (active={svc.get('active')}, sub={svc.get('sub')})")
            elif svc.get("status") == "ok":
                _clear(key)

    # ── GPU VRAM pressure ────────────────────────────────────────────────────
    mem_total = LATEST.get("mem_total") or 0
    mem_used  = LATEST.get("mem_used")  or 0
    if mem_total:
        free = mem_total - mem_used
        key  = "gpu:vram_pressure"
        if free < PRESSURE_MB:
            _emit(s, key, "warning", "🟠 GPU VRAM pressure",
                  f"Only {round(free)} MB free of {round(mem_total)} MB "
                  f"({round(100*mem_used/mem_total)}% used).")
        else:
            _clear(key)

    # ── Disks crossing the configured threshold ───────────────────────────────
    try: disk_thr = int(s.get("disk_alert_pct") or 90)
    except ValueError: disk_thr = 90
    host = LATEST.get("host") or {}
    seen_disks = set()
    for dk in (host.get("disks") or []):
        mp   = dk.get("mount", "?")
        seen_disks.add(mp)
        key  = f"disk:{mp}"
        pct  = dk.get("pct", 0)
        if pct >= disk_thr:
            level = "critical" if pct >= 95 else "warning"
            _emit(s, key, level, f"{'🔴' if level=='critical' else '🟠'} Disk {mp} at {pct}%",
                  f"{mp}: {dk.get('used',0)} GB / {dk.get('total',0)} GB used ({pct}%).")
        else:
            _clear(key)

    # ── GPU OOM events from the DB (each event_ts notified at most once) ─────
    try:
        cutoff = int(time.time()) - 3600
        with LOCK:
            rows = DB.execute("SELECT ts, service, detail FROM events "
                              "WHERE kind='oom' AND ts>=? ORDER BY ts", (cutoff,)).fetchall()
        for ets, svc, detail in rows:
            key = f"oom:{svc}:{ets}"
            with _NOTIFIER_LOCK:
                already = key in _NOTIFIED
                if not already:
                    _NOTIFIED[key] = 1
                    _persist_notified(key, True)
            if already:
                continue
            if LEVELS["critical"] < LEVELS.get(s.get("alert_min_level", "warning"), 1):
                continue
            for ch, ok, err in dispatch_alert(
                    s, "critical", f"🔴 GPU OOM in {svc}", (detail or "")[:1500]):
                if not ok: print(f"notifier {ch} error:", err, flush=True)
    except Exception as e:
        print("notify_scan oom error:", e, flush=True)

# ── Opt-in alerting rule engine ───────────────────────────────────────────────
# User-defined rules stored in SQLite (alert_rules). Each rule is a *trigger over a
# signal the app already computes* — an anomaly being active on a series, a disk or
# VRAM fill-ETA dropping below N days, or the projected month cost exceeding a
# budget. The engine is wholly inert until a user (a) enables a rule and (b) has a
# channel configured: zero rules => zero work. Evaluation reuses the forecast/
# anomaly outputs already gathered each notifier pass, so it adds no heavy compute.
#
# Edge-triggered with a per-rule cooldown: a rule fires at most once per
# cooldown_min window while its condition stays true (last_fired_at), and only
# re-arms cleanly once the condition clears (last_state). Snooze suppresses a rule
# until snoozed_until. Every fire (and every test) appends to alert_history (capped
# at the last ~200 rows). This only sends data OUT — it never touches the host.
_RULE_TYPES = {"anomaly", "disk_eta", "vram_eta", "cost_budget", "incident", "uptime_down", "cert_expiry", "slo_burn"}
_ALERT_HISTORY_CAP = 200
# Rule ids that already have a "suppressed (maintenance)" history row for their
# CURRENT contiguous suppressed-fire span. Edge-trigger so a long window with an
# active alarm logs ONE suppressed row, not one per sampler pass (which would
# otherwise evict real alert history). In-process; resets on restart (harmless).
_MAINT_SUPPRESS_LOGGED = set()

def _rule_row_to_dict(r):
    cols = ("id", "name", "enabled", "ctype", "params", "channel", "level",
            "cooldown_min", "created_at", "last_fired_at", "last_state", "snoozed_until")
    d = dict(zip(cols, r))
    d["enabled"] = bool(d["enabled"])
    try: d["params"] = json.loads(d["params"] or "{}")
    except (TypeError, ValueError): d["params"] = {}
    return d

def list_rules():
    with LOCK:
        rows = DB.execute(
            "SELECT id,name,enabled,ctype,params,channel,level,cooldown_min,created_at,"
            "last_fired_at,last_state,snoozed_until FROM alert_rules ORDER BY created_at").fetchall()
    return [_rule_row_to_dict(r) for r in rows]

def _validate_rule(body):
    """Return (clean_dict, None) or (None, error_string)."""
    name = (body.get("name") or "").strip()
    if not name:
        return None, "A rule name is required."
    ctype = (body.get("ctype") or "").strip()
    if ctype not in _RULE_TYPES:
        return None, f"Unknown condition type. Use one of: {', '.join(sorted(_RULE_TYPES))}."
    channel = (body.get("channel") or "all").strip()
    if channel not in _VALID_CHANNELS:
        return None, "Unknown channel."
    level = (body.get("level") or "warning").strip()
    if level not in LEVELS:
        return None, "Level must be info, warning, or critical."
    try:
        cooldown = int(body.get("cooldown_min", 60))
    except (TypeError, ValueError):
        return None, "Cooldown must be a whole number of minutes."
    if cooldown < 0:
        return None, "Cooldown cannot be negative."
    params = body.get("params") or {}
    if not isinstance(params, dict):
        return None, "params must be an object."
    # Per-type param validation (numbers stored as given; coerced at eval time).
    if ctype == "anomaly":
        series = (params.get("series") or "any")
        valid = {"any"} | {k for k, *_ in _ANOMALY_SERIES}
        if series not in valid:
            return None, f"Unknown anomaly series. Use 'any' or one of: {', '.join(sorted(valid - {'any'}))}."
    elif ctype in ("disk_eta", "vram_eta"):
        try: float(params.get("days"))
        except (TypeError, ValueError):
            return None, "A numeric 'days' threshold is required."
    elif ctype == "cost_budget":
        try: float(params.get("budget"))
        except (TypeError, ValueError):
            return None, "A numeric 'budget' is required."
    elif ctype == "incident":
        sev = (params.get("severity") or "warning")
        if sev not in ("warning", "critical"):
            return None, "Incident severity threshold must be 'warning' or 'critical'."
        params = {**params, "severity": sev}
    elif ctype == "uptime_down":
        # Target a specific uptime check by id, or "any"/none to fire when ANY
        # enabled check is down. A given check_id must exist (reject garbage).
        cid = params.get("check_id")
        if cid in (None, "", "any"):
            params = {**params, "check_id": "any"}
        else:
            if not isinstance(cid, str):
                return None, "check_id must be a check id string or 'any'."
            with LOCK:
                row = DB.execute("SELECT 1 FROM uptime_checks WHERE id=?", (cid,)).fetchone()
            if not row:
                return None, "Unknown uptime check. Pick an existing check or 'any'."
            params = {**params, "check_id": cid}
    elif ctype == "cert_expiry":
        # Fires while a TLS-cert check is in its pre-expiry warn window (cert_warn).
        # Target a specific cert check by id, or "any"/none to fire for ANY cert
        # check currently warning. A specific id must exist AND be a cert-type check
        # (an http/tcp check has no expiry to warn on).
        cid = params.get("check_id")
        if cid in (None, "", "any"):
            params = {**params, "check_id": "any"}
        else:
            if not isinstance(cid, str):
                return None, "check_id must be a check id string or 'any'."
            with LOCK:
                row = DB.execute("SELECT type FROM uptime_checks WHERE id=?", (cid,)).fetchone()
            if not row:
                return None, "Unknown uptime check. Pick an existing cert check or 'any'."
            if row[0] != "cert":
                return None, "cert_expiry rules target a TLS-cert check. Pick a cert-type check or 'any'."
            params = {**params, "check_id": cid}
    elif ctype == "slo_burn":
        # Fires while a check is burning its SLO error budget too fast or is over
        # budget. SLO applies to ANY check type (http/tcp/cert). Target a specific
        # check by id, or "any"/none to fire for ANY check breaching.
        #
        # Two policies:
        #   "single"       (default, backward-compat): one numeric burn_threshold
        #                  (default 1.0 = the budget-sustaining rate) trips on burn_1h.
        #   "multi_window" (Google SRE multi-window burn-rate): a FAST tier
        #                  (fast_burn, default 14.4×, 1h window → page) and a SLOW
        #                  tier (slow_burn, default 6×, 6h window → ticket).
        # Garbage/absent thresholds fall back to their defaults; an unknown policy is
        # coerced to "single" so an old/typo'd rule can never go dead.
        cid = params.get("check_id")
        if cid in (None, "", "any"):
            params = {**params, "check_id": "any"}
        else:
            if not isinstance(cid, str):
                return None, "check_id must be a check id string or 'any'."
            with LOCK:
                row = DB.execute("SELECT 1 FROM uptime_checks WHERE id=?", (cid,)).fetchone()
            if not row:
                return None, "Unknown uptime check. Pick an existing check or 'any'."
            params = {**params, "check_id": cid}
        policy = params.get("policy")
        if policy not in ("single", "multi_window"):
            policy = "single"
        if policy == "multi_window":
            try:
                fb = float(params.get("fast_burn"))
                if not (fb > 0):
                    raise ValueError
            except (TypeError, ValueError):
                fb = 14.4
            try:
                sb = float(params.get("slow_burn"))
                if not (sb > 0):
                    raise ValueError
            except (TypeError, ValueError):
                sb = 6.0
            params = {**params, "policy": "multi_window", "fast_burn": fb, "slow_burn": sb}
        else:
            try:
                bt = float(params.get("burn_threshold"))
                if not (bt > 0):
                    raise ValueError
            except (TypeError, ValueError):
                bt = 1.0
            params = {**params, "policy": "single", "burn_threshold": bt}
    return {"name": name, "ctype": ctype, "channel": channel, "level": level,
            "cooldown_min": cooldown, "params": params,
            "enabled": 1 if body.get("enabled") else 0}, None

def create_rule(body):
    clean, err = _validate_rule(body)
    if err:
        return None, err
    rid = uuid.uuid4().hex
    with LOCK:
        DB.execute(
            "INSERT INTO alert_rules(id,name,enabled,ctype,params,channel,level,cooldown_min,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (rid, clean["name"], clean["enabled"], clean["ctype"], json.dumps(clean["params"]),
             clean["channel"], clean["level"], clean["cooldown_min"], int(time.time())))
        DB.commit()
    return rid, None

def update_rule(rid, body):
    with LOCK:
        exists = DB.execute("SELECT 1 FROM alert_rules WHERE id=?", (rid,)).fetchone()
    if not exists:
        return False, "not found"
    if not body:
        return False, "empty update"
    # Allow a quick enable/disable or snooze without full revalidation.
    # Require the key be present so an empty body can't silently disable a rule.
    if "enabled" in body and set(body.keys()) <= {"enabled"}:
        with LOCK:
            DB.execute("UPDATE alert_rules SET enabled=? WHERE id=?", (1 if body.get("enabled") else 0, rid))
            DB.commit()
        return True, None
    if "snooze_min" in body and set(body.keys()) <= {"snooze_min"}:
        try: mins = int(body.get("snooze_min", 0))
        except (TypeError, ValueError): return False, "snooze_min must be a number"
        until = int(time.time()) + mins * 60 if mins > 0 else None
        with LOCK:
            DB.execute("UPDATE alert_rules SET snoozed_until=? WHERE id=?", (until, rid))
            DB.commit()
        return True, None
    clean, err = _validate_rule(body)
    if err:
        return False, err
    with LOCK:
        DB.execute(
            "UPDATE alert_rules SET name=?,enabled=?,ctype=?,params=?,channel=?,level=?,cooldown_min=? WHERE id=?",
            (clean["name"], clean["enabled"], clean["ctype"], json.dumps(clean["params"]),
             clean["channel"], clean["level"], clean["cooldown_min"], rid))
        DB.commit()
    return True, None

def delete_rule(rid):
    with LOCK:
        cur = DB.execute("DELETE FROM alert_rules WHERE id=?", (rid,))
        DB.commit()
        return cur.rowcount > 0

def record_alert(rule_id, rule_name, level, channel, status, title, detail):
    """Append to alert_history and trim to the last _ALERT_HISTORY_CAP rows."""
    with LOCK:
        DB.execute(
            "INSERT INTO alert_history(ts,rule_id,rule_name,level,channel,status,title,detail)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (int(time.time()), rule_id, rule_name, level, channel, status, title, (detail or "")[:500]))
        DB.execute(
            "DELETE FROM alert_history WHERE id NOT IN "
            "(SELECT id FROM alert_history ORDER BY id DESC LIMIT ?)", (_ALERT_HISTORY_CAP,))
        DB.commit()

def list_alert_history(limit=100):
    with LOCK:
        rows = DB.execute(
            "SELECT id,ts,rule_id,rule_name,level,channel,status,title,detail,acked "
            "FROM alert_history ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    cols = ("id", "ts", "rule_id", "rule_name", "level", "channel", "status", "title", "detail", "acked")
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        d["acked"] = bool(d["acked"])
        out.append(d)
    return out

def ack_alert(hid):
    with LOCK:
        cur = DB.execute("UPDATE alert_history SET acked=1 WHERE id=?", (int(hid),))
        DB.commit()
        return cur.rowcount > 0

# ── Maintenance / alert-silence windows ───────────────────────────────────────
# A scheduled window during which OUTBOUND alert notifications are MUTED. The
# headline use case is a nightly docker-pull restart cycle (recurring daily
# HH:MM–HH:MM, overnight-wrap allowed) or one planned maintenance (one-off
# start_ts/end_ts). This ONLY suppresses sends — it never touches the host or
# what's monitored, and it's wholly inert with zero windows configured.
#
# CRITICAL contract for the recovery edge: while a window is active the engine is
# PAUSED — evaluate_rules does NOT dispatch and does NOT mutate the arm/disarm
# last_state. So an alarm that begins+ends entirely inside a window never sends a
# fire (and thus never owes a recovery), and a condition still active when the
# window ENDS fires normally afterward. A recovery is never sent for a fire that
# was suppressed.

def _maint_row_to_dict(r):
    cols = ("id", "label", "enabled", "recurring", "start_ts", "end_ts",
            "daily_start", "daily_end", "created_at")
    d = dict(zip(cols, r))
    d["enabled"] = bool(d["enabled"])
    d["recurring"] = bool(d["recurring"])
    return d

def list_maintenance():
    with LOCK:
        rows = DB.execute(
            "SELECT id,label,enabled,recurring,start_ts,end_ts,daily_start,daily_end,created_at "
            "FROM maintenance_windows ORDER BY created_at").fetchall()
    return [_maint_row_to_dict(r) for r in rows]

def _parse_hhmm(v):
    """Return minutes-since-midnight for a 'HH:MM' string, or None if invalid."""
    if not isinstance(v, str):
        return None
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", v)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return h * 60 + mi

def _validate_maintenance(body):
    """Return (clean_dict, None) or (None, error_string)."""
    label = (body.get("label") or "").strip()
    if not label:
        return None, "A label is required."
    label = label[:120]
    recurring = bool(body.get("recurring"))
    enabled = 1 if body.get("enabled", True) else 0
    if recurring:
        ds, de = body.get("daily_start"), body.get("daily_end")
        sm, em = _parse_hhmm(ds), _parse_hhmm(de)
        if sm is None or em is None:
            return None, "Recurring windows need daily_start and daily_end as HH:MM."
        if sm == em:
            return None, "Daily start and end cannot be the same time."
        # Overnight wrap (e.g. 23:00–01:00) is allowed; equal times rejected above.
        return {"label": label, "enabled": enabled, "recurring": 1,
                "start_ts": None, "end_ts": None,
                "daily_start": f"{sm // 60:02d}:{sm % 60:02d}",
                "daily_end": f"{em // 60:02d}:{em % 60:02d}"}, None
    # One-off
    try:
        start_ts = int(body.get("start_ts"))
        end_ts = int(body.get("end_ts"))
    except (TypeError, ValueError):
        return None, "One-off windows need numeric start_ts and end_ts (epoch seconds)."
    if end_ts <= start_ts:
        return None, "end_ts must be after start_ts."
    return {"label": label, "enabled": enabled, "recurring": 0,
            "start_ts": start_ts, "end_ts": end_ts,
            "daily_start": None, "daily_end": None}, None

def create_maintenance(body):
    clean, err = _validate_maintenance(body)
    if err:
        return None, err
    mid = uuid.uuid4().hex
    with LOCK:
        DB.execute(
            "INSERT INTO maintenance_windows"
            "(id,label,enabled,recurring,start_ts,end_ts,daily_start,daily_end,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (mid, clean["label"], clean["enabled"], clean["recurring"], clean["start_ts"],
             clean["end_ts"], clean["daily_start"], clean["daily_end"], int(time.time())))
        DB.commit()
    return mid, None

def update_maintenance(mid, body):
    with LOCK:
        exists = DB.execute("SELECT 1 FROM maintenance_windows WHERE id=?", (mid,)).fetchone()
    if not exists:
        return False, "not found"
    if not body:
        return False, "empty update"
    # Quick enable/disable toggle without full revalidation.
    if "enabled" in body and set(body.keys()) <= {"enabled"}:
        with LOCK:
            DB.execute("UPDATE maintenance_windows SET enabled=? WHERE id=?",
                       (1 if body.get("enabled") else 0, mid))
            DB.commit()
        return True, None
    clean, err = _validate_maintenance(body)
    if err:
        return False, err
    with LOCK:
        DB.execute(
            "UPDATE maintenance_windows SET label=?,enabled=?,recurring=?,start_ts=?,end_ts=?,"
            "daily_start=?,daily_end=? WHERE id=?",
            (clean["label"], clean["enabled"], clean["recurring"], clean["start_ts"],
             clean["end_ts"], clean["daily_start"], clean["daily_end"], mid))
        DB.commit()
    return True, None

def delete_maintenance(mid):
    with LOCK:
        cur = DB.execute("DELETE FROM maintenance_windows WHERE id=?", (mid,))
        DB.commit()
        return cur.rowcount > 0

def _window_active(w, now):
    """Is this single window active at epoch `now`? Returns (active, ends_at_ts_or_None).
    For recurring windows the second element is the epoch when the current active
    span ends (for the banner countdown); None when not active."""
    if not w["enabled"]:
        return False, None
    if w["recurring"]:
        sm = _parse_hhmm(w["daily_start"])
        em = _parse_hhmm(w["daily_end"])
        if sm is None or em is None:
            return False, None
        lt = time.localtime(now)
        cur_min = lt.tm_hour * 60 + lt.tm_min
        midnight = now - (cur_min * 60 + lt.tm_sec)   # epoch of local 00:00 today
        if sm < em:
            active = sm <= cur_min < em
            ends_at = midnight + em * 60
        else:
            # Overnight wrap: active if at/after start OR before end.
            active = cur_min >= sm or cur_min < em
            # If we're in the post-midnight tail the span ends today, else tomorrow.
            ends_at = midnight + em * 60 + (0 if cur_min < em else 86400)
        return active, (ends_at if active else None)
    # One-off
    if w["start_ts"] is None or w["end_ts"] is None:
        return False, None
    active = w["start_ts"] <= now <= w["end_ts"]
    return active, (w["end_ts"] if active else None)

def _in_maintenance(now=None):
    """True if ANY enabled window is active at `now`. Returns (active, ends_at_ts).
    ends_at_ts is the latest end among active windows (for the 'muted until' banner),
    or None when nothing is active."""
    if now is None:
        now = int(time.time())
    latest_end = None
    active = False
    for w in list_maintenance():
        a, ends = _window_active(w, now)
        if a:
            active = True
            if ends is not None and (latest_end is None or ends > latest_end):
                latest_end = ends
    return active, latest_end

def _next_window_start(w, now):
    """For an ENABLED window, the soonest FUTURE start≥now and its end as epochs,
    or None. One-off: start_ts if > now. Recurring: project the next occurrence's
    local-time start (today's HH:MM if still future, else tomorrow's). Returns
    (start_ts, end_ts) or None. Local-time projection (mktime), never UTC."""
    if not w["enabled"]:
        return None
    if w["recurring"]:
        sm = _parse_hhmm(w["daily_start"])
        em = _parse_hhmm(w["daily_end"])
        if sm is None or em is None:
            return None
        lt = time.localtime(now)
        midnight = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                    0, 0, 0, 0, 0, -1)))  # local 00:00 today
        cur_min = lt.tm_hour * 60 + lt.tm_min
        # Today's start if it hasn't passed yet, else tomorrow's.
        if sm * 60 > now - midnight:
            start = midnight + sm * 60
        else:
            start = midnight + 86400 + sm * 60
        # End is start's HH:MM + duration (wrap adds a day to the end side).
        span = (em - sm) if em > sm else (em + 1440 - sm)
        return start, start + span * 60
    if w["start_ts"] is not None and w["start_ts"] > now:
        return w["start_ts"], w["end_ts"]
    return None

def _maint_status(now=None):
    """Glanceable maintenance summary (pure, read-only).
    {active, ends_at, next_start, next_end, next_label, next_recurring}.
    active/ends_at = is any window active now + latest active end.
    next_* = the soonest FUTURE window start (≥now) across enabled windows."""
    if now is None:
        now = int(time.time())
    active, ends_at = _in_maintenance(now)
    best = None  # (start, end, label, recurring)
    for w in list_maintenance():
        nxt = _next_window_start(w, now)
        if nxt is None:
            continue
        start, end = nxt
        if best is None or start < best[0]:
            best = (start, end, w["label"], bool(w["recurring"]))
    return {
        "active": active,
        "ends_at": ends_at,
        "next_start": best[0] if best else None,
        "next_end": best[1] if best else None,
        "next_label": best[2] if best else None,
        "next_recurring": best[3] if best else None,
    }

# ── Notification routing rules ────────────────────────────────────────────────
# Route a firing alert to a specific channel when its entity/subject glob-matches
# AND its level is at/above the route's min_level. Evaluated in `priority` order
# (then created_at) BEFORE the default fan-out in evaluate_rules.
#
# CRITICAL invariant — ZERO routes (or no match) = byte-identical to the prior
# behaviour: dispatch_alert(..., channel=rule["channel"]). Routing ONLY ever
# REDIRECTS to the union of matching routes' channels; it never silently drops.
# A matched route whose channel is not configured falls back to the rule's own
# channel (recorded), so a misconfigured route can never black-hole an alert.
#
# The matcher is a single case-insensitive fnmatch glob over the alert's derived
# "entity" — see _alert_entity: the rule name plus the per-ctype subject (which
# already carries the container/service/host/series/mount/check label). A recovery
# notice carries the SAME entity as the fire it recovers, so it routes identically.

def _route_row_to_dict(r):
    cols = ("id", "label", "enabled", "match", "min_level", "channel", "priority", "created_at")
    d = dict(zip(cols, r))
    d["enabled"] = bool(d["enabled"])
    return d

def list_routes():
    with LOCK:
        rows = DB.execute(
            "SELECT id,label,enabled,match,min_level,channel,priority,created_at "
            "FROM notification_routes ORDER BY priority, created_at").fetchall()
    return [_route_row_to_dict(r) for r in rows]

def _validate_route(body):
    """Return (clean_dict, None) or (None, error_string)."""
    label = (body.get("label") or "").strip()
    if not label:
        return None, "A label is required."
    label = label[:120]
    match = (body.get("match") or "*").strip()
    if not match:
        return None, "A match pattern is required (use * for any)."
    match = match[:200]
    min_level = (body.get("min_level") or "info").strip()
    if min_level not in LEVELS:
        return None, "min_level must be info, warning, or critical."
    channel = (body.get("channel") or "").strip()
    # A route MUST name a concrete channel — "all" is the default-fan-out behaviour
    # and is meaningless as a redirect target.
    if channel not in _NOTIFY_CHANNELS:
        return None, f"Channel must be one of: {', '.join(_NOTIFY_CHANNELS)}."
    try:
        priority = int(body.get("priority", 0))
    except (TypeError, ValueError):
        return None, "priority must be a whole number."
    return {"label": label, "match": match, "min_level": min_level, "channel": channel,
            "priority": priority, "enabled": 1 if body.get("enabled", True) else 0}, None

def create_route(body):
    clean, err = _validate_route(body)
    if err:
        return None, err
    rid = uuid.uuid4().hex
    with LOCK:
        DB.execute(
            "INSERT INTO notification_routes(id,label,enabled,match,min_level,channel,priority,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (rid, clean["label"], clean["enabled"], clean["match"], clean["min_level"],
             clean["channel"], clean["priority"], int(time.time())))
        DB.commit()
    return rid, None

def update_route(rid, body):
    with LOCK:
        exists = DB.execute("SELECT 1 FROM notification_routes WHERE id=?", (rid,)).fetchone()
    if not exists:
        return False, "not found"
    if not body:
        return False, "empty update"
    # Quick enable/disable toggle without full revalidation.
    if "enabled" in body and set(body.keys()) <= {"enabled"}:
        with LOCK:
            DB.execute("UPDATE notification_routes SET enabled=? WHERE id=?",
                       (1 if body.get("enabled") else 0, rid))
            DB.commit()
        return True, None
    clean, err = _validate_route(body)
    if err:
        return False, err
    with LOCK:
        DB.execute(
            "UPDATE notification_routes SET label=?,enabled=?,match=?,min_level=?,channel=?,priority=? WHERE id=?",
            (clean["label"], clean["enabled"], clean["match"], clean["min_level"],
             clean["channel"], clean["priority"], rid))
        DB.commit()
    return True, None

def delete_route(rid):
    with LOCK:
        cur = DB.execute("DELETE FROM notification_routes WHERE id=?", (rid,))
        DB.commit()
        return cur.rowcount > 0

def _alert_entity(rule, title):
    """The matchable entity/subject for an alert, for notification routing.

    `_eval_rule`'s `title` already embeds the per-ctype subject — the series key
    (anomaly), the mount (disk_eta), 'GPU VRAM' (vram_eta), the projected-cost line
    (cost_budget), 'incident' (incident), or the check label (uptime_down). We pair
    it with the rule name so a glob can target either the user's rule naming or the
    underlying entity. Lowercased for case-insensitive matching. Recovery notices
    pass the SAME (rule, fire-title) so they route to the same channels as the fire."""
    return f"{rule.get('name') or ''} {title or ''}".strip().lower()

# A stable per-ctype subject keyword, so a recovery notice (which has no live fire
# title — the condition has cleared) routes consistently with the fire it recovers.
# These keywords also appear in the fire titles ("Anomaly on …", "Disk … fills",
# "GPU VRAM …", "… incident …", "Uptime …"), so a glob targeting the entity matches
# both fire and recovery for the same rule.
_CTYPE_SUBJECT = {"anomaly": "anomaly", "disk_eta": "disk", "vram_eta": "vram",
                  "cost_budget": "cost", "incident": "incident", "uptime_down": "uptime",
                  "cert_expiry": "cert", "slo_burn": "slo"}

def _recovery_entity(rule):
    """Matchable entity for a recovery notice — rule name + ctype subject keyword,
    so it routes the SAME way as the fire it recovers."""
    return f"{rule.get('name') or ''} {_CTYPE_SUBJECT.get(rule.get('ctype'), '')}".strip().lower()

def _route_channels(level, entity):
    """Channels selected by the enabled routing rules for an alert at `level` whose
    derived `entity` matches. Returns the ORDERED, de-duplicated union of matching
    routes' channels (priority then created_at order). Empty list = NO route matched
    → caller MUST fall back to the alert rule's own channel (unchanged behaviour).
    Pure read; never raises out."""
    try:
        rank = LEVELS.get(level, 0)
        ent = (entity or "").lower()
        chans = []
        for r in list_routes():
            if not r["enabled"]:
                continue
            if rank < LEVELS.get(r["min_level"], 0):
                continue
            if not fnmatch.fnmatch(ent, (r["match"] or "*").lower()):
                continue
            if r["channel"] not in chans:
                chans.append(r["channel"])
        return chans
    except Exception as e:
        print("route selection error:", e, flush=True)
        return []

def dispatch_routed(s, level, title, detail, *, entity, default_channel):
    """Dispatch an alert through notification routing, then fall back to the prior
    behaviour. If ANY enabled route matches (`entity` glob + level≥min_level), send
    to each matched channel — but a matched channel that isn't configured falls back
    to `default_channel` (recorded, never dropped). If NO route matches, behave
    byte-identically to dispatch_alert(s, level, title, detail, channel=default_channel).
    Returns the same [(channel, ok, err), ...] shape as dispatch_alert."""
    chans = _route_channels(level, entity)
    if not chans:
        return dispatch_alert(s, level, title, detail, channel=default_channel)
    configured = set(_configured_channels(s))
    out = []
    fellback = False
    for ch in chans:
        if ch in configured:
            ok, err = _send_one_channel(s, ch, level, title, detail)
            out.append((ch, ok, err))
        elif not fellback:
            # Matched route points at an unconfigured channel: don't black-hole —
            # fall back to the default fan-out exactly once, so the alert still goes
            # out. Recorded under its real channel name(s) by dispatch_alert.
            fellback = True
            out.extend(dispatch_alert(s, level, title, detail, channel=default_channel))
    return out

# ── AI probable-cause enrichment for incident notifications (E1) ──────────────
# The one-liner is built ONLY from the incident's CACHED ai_explanation (the field
# the auto-explain worker / explicit endpoint persists off the poll path). These
# helpers are pure cache reads — they NEVER call the LLM — so the notification /
# dispatch path stays LLM-free. Gated by the opt-in notify_ai_cause (default OFF).
_INCIDENT_CAUSE_MAX = 240   # hard length cap for the cause line body

def _incident_cause_text(inc):
    """Sanitized, bounded probable-cause text from an incident's CACHED ai_explanation
    (already label-only — series keys, no target/creds/raw err). Collapses whitespace,
    re-redacts defensively, hard-caps length. Returns '' when nothing is cached.
    Pure read — NEVER triggers LLM generation."""
    cause = (inc.get("ai_explanation") or "").strip() if isinstance(inc, dict) else ""
    if not cause:
        return ""
    cause = _redact_target(" ".join(cause.split()))
    if len(cause) > _INCIDENT_CAUSE_MAX:
        cause = cause[:_INCIDENT_CAUSE_MAX - 1].rstrip() + "…"
    return cause

def _incident_cause_line(inc, s=None):
    """The "🧠 Probable cause: …" line to append to an incident notification, IFF the
    notify_ai_cause opt-in is ON and a cached explanation exists; else ''. Cache-only,
    never generates. `s` (settings) may be passed to avoid a re-read."""
    s = s if s is not None else get_settings()
    if s.get("notify_ai_cause") != "1":
        return ""
    cause = _incident_cause_text(inc)
    return f"🧠 Probable cause: {cause}" if cause else ""

def _eval_rule(rule, signals):
    """Evaluate one rule against the precomputed signal bundle. Returns
    (fired_bool, title, detail). Pure; no I/O, no recompute."""
    p = rule["params"]
    ct = rule["ctype"]
    if ct == "anomaly":
        want = p.get("series") or "any"
        items = (signals.get("anomalies") or {}).get("items") or []
        hits = [a for a in items if want == "any" or a.get("key") == want]
        if hits:
            a = hits[0]
            return True, f"Anomaly on {a['key']}", (
                f"{a['key']} {a['direction']} to {a['value']}{a['unit']} "
                f"(baseline {a['baseline']}{a['unit']}, z={a['z']}).")
        return False, None, None
    if ct == "disk_eta":
        thr = float(p.get("days"))
        worst = None
        for d in (signals.get("disk") or []):
            eta = d.get("eta_days")
            if eta is not None and eta <= thr and (worst is None or eta < worst[0]):
                worst = (eta, d)
        if worst:
            eta, d = worst
            return True, f"Disk {d['mount']} fills in ~{eta}d", (
                f"{d['mount']} at {d.get('pct')}% and filling "
                f"~{d.get('gb_per_day')} GB/day — projected full in ~{eta} days "
                f"(threshold {thr}d).")
        return False, None, None
    if ct == "vram_eta":
        thr = float(p.get("days"))
        v = signals.get("vram") or {}
        eta_min = v.get("eta_min")
        if eta_min is not None and v.get("status") == "filling":
            eta_days = eta_min / 1440.0
            if eta_days <= thr:
                return True, f"GPU VRAM fills in ~{round(eta_days,2)}d", (
                    f"VRAM at {v.get('pct')}% rising ~{v.get('mb_per_min')} MB/min — "
                    f"projected full in ~{round(eta_days,2)} days (threshold {thr}d).")
        return False, None, None
    if ct == "cost_budget":
        budget = float(p.get("budget"))
        cm = signals.get("cost_month") or {}
        if not cm.get("enabled"):
            return False, None, None
        proj = cm.get("projected_month")
        if proj is not None and proj > budget:
            cur = cm.get("currency", "")
            return True, f"Projected cost {cur}{proj} over budget", (
                f"Projected month energy cost {cur}{proj} exceeds budget "
                f"{cur}{budget} (month-to-date {cur}{cm.get('month_to_date')}).")
        return False, None, None
    if ct == "incident":
        # ONE notification for the whole correlated event. Fires when an incident is
        # OPEN at/above the configured severity threshold (the edge-triggered arm/
        # disarm + cooldown machinery below gives exactly one fire per open→clear
        # cycle, and one recovery on clear), carrying the correlated member series —
        # vs the human side's one-ping-per-series. Reads the already-computed open
        # incidents from the shared signal bundle; no DB recompute here.
        want = (p.get("severity") or "warning")
        ranks = {"warning": 1, "critical": 2}
        thr = ranks.get(want, 1)
        opens = [i for i in (signals.get("incidents") or []) if i.get("state") == "open"]
        hits = [i for i in opens if ranks.get(i.get("severity"), 0) >= thr]
        if hits:
            inc = max(hits, key=lambda i: (ranks.get(i.get("severity"), 0), i.get("opened_at") or 0))
            mem = [m for m in (inc.get("members") or []) if m.get("active")] or (inc.get("members") or [])
            n = len(mem)
            parts = []
            for m in mem[:6]:
                arrow = "▲" if m.get("direction") == "spike" else "▼"
                parts.append(f"{m.get('series')} {arrow}")
            more = f" +{n - 6} more" if n > 6 else ""
            sev = inc.get("severity", "warning")
            title = f"{sev.capitalize()} incident — {n} correlated anomal{'y' if n == 1 else 'ies'}"
            detail = f"{n} correlated anomal{'y' if n == 1 else 'ies'}: " + ", ".join(parts) + more + "."
            # Append the CACHED probable-cause one-liner when opted in (notify_ai_cause).
            # inc came from the signal bundle (list_incidents) — its ai_explanation is a
            # pure cache read; this NEVER calls the LLM on the dispatch path.
            cause = _incident_cause_line(inc)
            if cause:
                detail = f"{detail}\n{cause}"
            return True, title, detail
        return False, None, None
    if ct == "uptime_down":
        # Fires when the targeted uptime check (or, in "any" mode, ANY enabled
        # check) is currently DOWN per uptime_overview's per-check state. A check
        # with NO results yet reads as "unknown" and does NOT fire — we never
        # alarm before the first probe has actually observed the endpoint.
        # Reads the already-computed uptime states from the shared signal bundle;
        # the redaction (_redact_target) already applied upstream covers target/err.
        want = (p.get("check_id") or "any")
        checks = signals.get("uptime") or []
        if want == "any":
            down = [c for c in checks if c.get("enabled") and c.get("state") == "down"]
            if not down:
                return False, None, None
            labels = [str(c.get("label") or c.get("id") or "?") for c in down]
            n = len(labels)
            head = ", ".join(labels[:4]) + (f" +{n - 4} more" if n > 4 else "")
            first = down[0]
            tail = _uptime_down_reason(first)
            title = f"Uptime — {n} check{'s' if n != 1 else ''} DOWN"
            detail = f"{n} uptime check{'s' if n != 1 else ''} down: {head}." + (f" {first.get('label')}: {tail}" if tail else "")
            return True, title, detail
        match = next((c for c in checks if c.get("id") == want), None)
        if not match or match.get("state") != "down":
            return False, None, None
        label = str(match.get("label") or match.get("id") or "check")
        tail = _uptime_down_reason(match)
        title = f"Uptime DOWN — {label}"
        detail = f"Uptime check '{label}' is DOWN." + (f" {tail}" if tail else "")
        return True, title, detail
    if ct == "cert_expiry":
        # Fires while a TLS-cert check is UP but inside its pre-expiry warn window
        # (uptime_overview sets cert_warn=True iff state=='up' and days_to_expiry<=
        # the check's cert_warn_days). We deliberately require state=='up' so we
        # never double-fire with uptime_down once a cert hard-expires / the host
        # goes down — that's uptime_down's territory. Reads the already-computed
        # per-check states from the shared bundle; targets are _redact_target'd
        # upstream and we surface only the label, day counts, and subject CN.
        want = (p.get("check_id") or "any")
        checks = signals.get("uptime") or []
        warning = [c for c in checks
                   if c.get("type") == "cert" and c.get("enabled")
                   and c.get("state") == "up" and c.get("cert_warn")]
        if want != "any":
            warning = [c for c in warning if c.get("id") == want]
        if not warning:
            return False, None, None
        # Soonest-to-expire first, so the headline names the most urgent cert.
        warning.sort(key=lambda c: (c.get("days_to_expiry") if c.get("days_to_expiry") is not None else 1 << 30))
        first = warning[0]
        n = len(warning)
        label = str(first.get("label") or first.get("id") or "cert")
        d = first.get("days_to_expiry")
        dtxt = f"~{d}d" if d is not None else "soon"
        thr = first.get("cert_warn_days")
        if want == "any" and n > 1:
            labels = [str(c.get("label") or c.get("id") or "?") for c in warning]
            head = ", ".join(labels[:4]) + (f" +{n - 4} more" if n > 4 else "")
            title = f"Certs expiring — {n} within warn window"
            detail = (f"{n} TLS certs nearing expiry: {head}. Soonest: '{label}' in {dtxt}"
                      + (f" (warns at {thr}d)" if thr is not None else "") + ".")
        else:
            title = f"Cert expiring — {label} ({dtxt} left)"
            cn = first.get("subject_cn")
            issuer = first.get("issuer_cn")
            # Exact expiry date (UTC, day granularity) from the persisted notAfter —
            # public cert metadata, safe to surface; the raw target stays redacted.
            na_ts = first.get("not_after_ts")
            date_txt = None
            if na_ts is not None:
                try:
                    date_txt = time.strftime("%Y-%m-%d", time.gmtime(na_ts))
                except (ValueError, OverflowError):
                    date_txt = None
            detail = (f"TLS cert for '{label}' expires in {dtxt}"
                      + (f" on {date_txt}" if date_txt else "")
                      + (f" (warns at {thr}d)" if thr is not None else "")
                      + (f"; subject CN {cn}" if cn else "")
                      + (f"; issued by {issuer}" if issuer else "") + ".")
        return True, title, detail
    if ct == "slo_burn":
        # Fires when an ENABLED check is burning its SLO error budget too fast or is
        # already over budget — the actionable teeth on the per-check `slo` sub-object
        # that uptime_overview already computes. The data_sufficient gate is ESSENTIAL:
        # a brand-new check with a handful of samples must NEVER alarm, however bad its
        # raw failure fraction reads. Reads the shared bundle; we surface only the
        # label, budget %, burn rate, target, and observed window — credential-safe
        # (the raw target is _redact_target'd upstream and never touched here).
        # Multi-window (Google SRE) burn-rate policy distinguishes a FAST burn (1h
        # window, page-worthy) from a SLOW burn (6h window, ticket-worthy); the
        # legacy "single" policy keeps one burn_1h threshold. We never change the
        # rule's configured dispatch level — the burn TIER is encoded in the
        # title/detail (over_budget > fast > slow), and worst-first sort ranks them.
        policy = (p or {}).get("policy")
        if policy not in ("single", "multi_window"):
            policy = "single"
        if policy == "multi_window":
            try:
                fast_thr = float((p or {}).get("fast_burn"))
                if not (fast_thr > 0):
                    raise ValueError
            except (TypeError, ValueError):
                fast_thr = 14.4
            try:
                slow_thr = float((p or {}).get("slow_burn"))
                if not (slow_thr > 0):
                    raise ValueError
            except (TypeError, ValueError):
                slow_thr = 6.0
            thr = None
        else:
            try:
                thr = float((p or {}).get("burn_threshold"))
                if not (thr > 0):
                    raise ValueError
            except (TypeError, ValueError):
                thr = 1.0
            fast_thr = slow_thr = None
        want = (p.get("check_id") or "any")
        checks = signals.get("uptime") or []
        def _tier(c):
            """Which burn tier a check trips, or None. Ranks over_budget(3) >
            fast/1h(2) > slow/6h(1) > none(0). Never None/NaN-compares."""
            slo = c.get("slo") or {}
            if not c.get("enabled") or not slo.get("data_sufficient"):
                return 0
            if slo.get("over_budget"):
                return 3
            b1 = slo.get("burn_1h")
            b6 = slo.get("burn_6h")
            if policy == "multi_window":
                if b1 is not None and b1 >= fast_thr:
                    return 2
                if b6 is not None and b6 >= slow_thr:
                    return 1
                return 0
            # single policy: one burn_1h threshold, no tiering
            return 2 if (b1 is not None and b1 >= thr) else 0
        breaching = [c for c in checks if _tier(c) > 0]
        if want != "any":
            breaching = [c for c in breaching if c.get("id") == want]
        if not breaching:
            return False, None, None
        # Worst-first: tier (over_budget > fast > slow), then budget consumed, then burn.
        def _sev(c):
            slo = c.get("slo") or {}
            return (_tier(c),
                    slo.get("budget_consumed_pct") or 0.0,
                    slo.get("burn_1h") or 0.0)
        breaching.sort(key=_sev, reverse=True)
        first = breaching[0]
        n = len(breaching)
        label = str(first.get("label") or first.get("id") or "check")
        fslo = first.get("slo") or {}
        bc = fslo.get("budget_consumed_pct")
        bctxt = f"{_reco_num(bc)}%" if bc is not None else "?"
        b1 = fslo.get("burn_1h")
        b6 = fslo.get("burn_6h")
        tgt = fslo.get("target")
        tgt_txt = f"{_reco_num(round(tgt * 100, 3))}%" if tgt is not None else "?"
        wda = fslo.get("window_days_actual")
        burn_txt = (f"{_reco_num(b1)}×" if b1 is not None else "?")
        tier = _tier(first)
        # Tier prefix on the title — credential-safe (label + numbers only).
        # ONLY the multi_window policy relabels by tier; the legacy "single" policy
        # keeps its byte-for-byte title/detail (over_budget still surfaced via the
        # "— OVER BUDGET." detail suffix exactly as before).
        if policy == "multi_window" and tier == 3:
            prefix = f"Over budget — '{label}'"
        elif policy == "multi_window" and tier == 2:
            prefix = f"⚡ Fast burn (page) — {label}: burn {burn_txt}/1h ≥ {_reco_num(fast_thr)}×"
        elif policy == "multi_window" and tier == 1:
            prefix = f"🐢 Slow burn (ticket) — {label}: {_reco_num(b6)}×/6h ≥ {_reco_num(slow_thr)}×"
        else:
            prefix = f"'{label}'"
        if want == "any" and n > 1:
            labels = [str(c.get("label") or c.get("id") or "?") for c in breaching]
            head = ", ".join(labels[:4]) + (f" +{n - 4} more" if n > 4 else "")
            title = f"SLO burn — {n} checks breaching budget"
            detail = (f"{n} uptime checks over budget / burning fast: {head}. "
                      f"Worst: {prefix} (budget {bctxt} used, burn {burn_txt}/h"
                      + (f", {_reco_num(b6)}×/6h" if b6 is not None else "")
                      + f", SLO target {tgt_txt}"
                      + (f", observed {wda}d" if wda else "") + ").")
        else:
            if policy == "multi_window" and tier == 3:
                title = f"Over budget — {label} (budget {bctxt} used, burn {burn_txt})"
            elif policy == "multi_window" and tier in (2, 1):
                title = prefix
            else:
                title = f"SLO burn — {label} (budget {bctxt} used, burn {burn_txt})"
            detail = (f"Uptime check '{label}' is burning its SLO error budget: "
                      f"budget {bctxt} used, burn {burn_txt} over the last hour"
                      + (f" ({_reco_num(b6)}× over 6h)" if b6 is not None else "")
                      + f", SLO target {tgt_txt}"
                      + (f", observed over {wda}d" if wda else "")
                      + (" — OVER BUDGET." if tier == 3 else
                         (" — FAST BURN (page)." if (tier == 2 and policy == "multi_window") else
                          (" — SLOW BURN (ticket)." if tier == 1 else "."))))
        return True, title, detail
    return False, None, None

def _uptime_down_reason(c):
    """Short, credential-safe reason for an uptime-down notice: last error and/or
    HTTP status code. _uptime_state already stored a _redact_target'd err; we
    redact again defensively before it leaves in a notification."""
    bits = []
    code = c.get("last_code")
    if code is not None:
        bits.append(f"status {code}")
    err = c.get("last_err")
    if err:
        bits.append(_redact_target(str(err))[:200])
    return " — ".join(bits) if bits else ""

def _live_signal_bundle(now=None):
    """Build the read-only signal bundle _eval_rule() consumes, from the CURRENT
    live signals: disk forecasts, month cost projection, z-score anomalies, VRAM
    forecast, correlated incidents and uptime/cert/SLO per-check states. Every entry
    is a PURE read (forecasts + cached overviews) — no dispatch, no cooldown/last-
    fired writes, no rule mutation. Shared by evaluate_rules() (real firing) and the
    side-effect-free 'would it fire now?' preview endpoint."""
    if now is None:
        now = int(time.time())
    ctx = _cost_ctx()
    with LOCK:
        cur = DB.cursor()
        signals = {"disk": _disk_forecasts(cur, now),
                   "cost_month": _cost_projection(cur, ctx, now),
                   "anomalies": _zscore_anomalies(cur, now),
                   "vram": _vram_forecast(cur, now)}
    # list_incidents() and uptime_overview() take LOCK themselves — read them OUTSIDE
    # the block above so we never nest the non-reentrant lock.
    signals["incidents"] = list_incidents()
    signals["uptime"] = uptime_overview().get("checks", [])
    return signals

def evaluate_rules(signals=None):
    """Evaluate every enabled rule and fire those whose condition is true and whose
    cooldown has elapsed. `signals` may be supplied (so the notifier pass shares the
    forecast it already computed); otherwise it's computed here. Returns the number
    of rules that fired (for tests / logging). Never raises out."""
    rules = [r for r in list_rules() if r["enabled"]]
    if not rules:
        return 0
    s = get_settings()
    if not _configured_channels(s):
        return 0
    now = int(time.time())
    # Maintenance / silence windows: while ANY enabled window is active the engine
    # is PAUSED. We still EVALUATE (so suppressed events can be recorded), but we
    # neither dispatch nor mutate the arm/disarm last_state — see _in_maintenance.
    in_maint, _maint_end = _in_maintenance(now)
    if signals is None:
        try:
            signals = _live_signal_bundle(now)
        except Exception as e:
            print("evaluate_rules signal error:", e, flush=True)
            return 0
    # Defense-in-depth: uptime_down AND cert_expiry rules need per-check states. If a
    # caller passed a pre-built bundle (e.g. the collector) that omitted "uptime",
    # back-fill it here so the rule can never silently go dead. uptime_overview() takes
    # LOCK itself, so this runs outside any held lock. Only pays the read when such a
    # rule is actually enabled.
    if "uptime" not in signals and any(r["ctype"] in ("uptime_down", "cert_expiry", "slo_burn") for r in rules):
        try:
            signals["uptime"] = uptime_overview().get("checks", [])
        except Exception as e:
            print("evaluate_rules uptime back-fill error:", e, flush=True)
            signals["uptime"] = []
    fired = 0
    for rule in rules:
        try:
            active, title, detail = _eval_rule(rule, signals)
        except Exception as e:
            print(f"rule {rule['id']} eval error:", e, flush=True)
            continue
        new_state = "active" if active else "clear"
        snoozed = rule.get("snoozed_until") and rule["snoozed_until"] > now
        cooldown_s = rule["cooldown_min"] * 60
        cooled = (not rule.get("last_fired_at")) or (now - rule["last_fired_at"] >= cooldown_s)
        # Incident rules are strictly edge-once: an incident is a single discrete
        # event, so once we've fired for it (last_state=='active') we don't re-fire
        # while it stays open — ONE notification for the whole correlated event,
        # regardless of cooldown. (Threshold rules still re-fire after cooldown.)
        already_armed = rule["ctype"] == "incident" and rule.get("last_state") == "active"
        should_fire = active and not snoozed and cooled and not already_armed
        if not (should_fire and in_maint):
            # Span ended (alarm cleared or window closed) → re-arm one-shot logging.
            _MAINT_SUPPRESS_LOGGED.discard(rule["id"])
        if should_fire and in_maint:
            # Engine paused by a maintenance window: do NOT send and do NOT arm
            # (no last_state/last_fired_at mutation), so an alarm that begins and
            # ends entirely inside the window never sends a fire — and therefore
            # never owes a spurious recovery. Record ONE suppressed row per span
            # (edge-triggered) so a long window can't flood/evict alert history.
            if rule["id"] not in _MAINT_SUPPRESS_LOGGED:
                full_title = f"{rule['name']}: {title}"
                record_alert(rule["id"], rule["name"], rule["level"], rule["channel"],
                             "suppressed", full_title, "suppressed (maintenance)")
                _MAINT_SUPPRESS_LOGGED.add(rule["id"])
            continue
        if should_fire:
            level = rule["level"]
            full_title = f"{rule['name']}: {title}"
            entity = _alert_entity(rule, title)
            for ch, ok, err in dispatch_routed(s, level, full_title, detail,
                                               entity=entity, default_channel=rule["channel"]):
                status = "sent" if ok else "error"
                record_alert(rule["id"], rule["name"], level, ch, status, full_title, detail if ok else (err or ""))
                if not ok:
                    print(f"rule {rule['id']} channel {ch} error:", err, flush=True)
            with LOCK:
                # last_state='active' is also the "armed for recovery" flag: a rule
                # that fired and is still firing owes exactly one recovery notice.
                DB.execute("UPDATE alert_rules SET last_fired_at=?, last_state=? WHERE id=?",
                           (now, "active", rule["id"]))
                DB.commit()
            fired += 1
        elif not active and rule.get("last_state") == "active" and in_maint:
            # Recovery edge reached while PAUSED: leave the rule armed (last_state
            # stays 'active') and send nothing. The fire it recovers from happened
            # before maintenance; we defer the recovery until the window ends, when
            # this branch runs normally (or, if the condition has resolved by then,
            # one recovery is sent the first pass after the window closes).
            continue
        elif not active and rule.get("last_state") == "active":
            # Recovery edge: the signal that fired has returned to normal. Send one
            # ✅ "cleared" notice through the same channel, marked as a recovery (not a
            # new alarm). Edge-triggered: we immediately disarm so it sends only once,
            # and only because the rule had previously fired (last_state=='active').
            r_title = f"✅ {rule['name']}: cleared"
            r_detail = "Condition recovered — the signal returned to normal."
            # Route the recovery the SAME way as the fire it recovers. A recovery is
            # level 'info'; routes whose min_level is warning/critical won't match it,
            # so it falls back to the rule's own channel — i.e. it goes wherever the
            # fire's own channel sent its recovery before. (See _recovery_entity.)
            r_entity = _recovery_entity(rule)
            for ch, ok, err in dispatch_routed(s, "info", r_title, r_detail,
                                               entity=r_entity, default_channel=rule["channel"]):
                status = "recovered" if ok else "error"
                record_alert(rule["id"], rule["name"], "info", ch, status, r_title, r_detail if ok else (err or ""))
                if not ok:
                    print(f"rule {rule['id']} recovery channel {ch} error:", err, flush=True)
            with LOCK:
                DB.execute("UPDATE alert_rules SET last_state=? WHERE id=?", ("clear", rule["id"]))
                DB.commit()
        elif not active and rule.get("last_state") not in (None, "clear"):
            # Active condition that never actually fired (e.g. snoozed/uncooled and the
            # last_state is some non-active marker) returned to normal — just settle the
            # state to 'clear' without a recovery notice (we only recover real fires).
            with LOCK:
                DB.execute("UPDATE alert_rules SET last_state=? WHERE id=?", ("clear", rule["id"]))
                DB.commit()
    return fired

def _preview_rule(clean, signals):
    """Side-effect-free 'would it fire now?' dry-run for a VALIDATED rule spec against
    the CURRENT signal bundle. Returns (would_fire, detail, observed):

      • would_fire — True/False from the SAME pure _eval_rule() the engine uses, or
        None when the ctype can't be honestly judged at an instant right now (e.g.
        cost tracking is off, or an SLO check has no data yet).
      • detail     — a plain-terms one-liner explaining WHY: the observed value(s)
        vs the rule's threshold, whether firing or not.
      • observed   — a small structured dict of the numbers behind that verdict.

    Purity: reads ONLY the passed-in `signals` bundle. No dispatch to any channel,
    no cooldown/last-fired/snooze write, no rule row created/updated, no LLM. The
    caller (the preview endpoint) is an explicit user action, never a poll."""
    ct = clean.get("ctype")
    p = clean.get("params") or {}
    # Authoritative firing verdict + (when firing) the exact production detail string.
    try:
        fired, _title, fdetail = _eval_rule(clean, signals)
    except Exception:
        fired, _title, fdetail = False, None, None

    if ct == "anomaly":
        want = p.get("series") or "any"
        items = (signals.get("anomalies") or {}).get("items") or []
        hits = [a for a in items if want == "any" or a.get("key") == want]
        observed = {"series": want, "active_anomalies": len(items), "matching": len(hits)}
        who = "any series" if want == "any" else want
        if fired:
            return True, fdetail, observed
        return False, (f"No active anomaly on {who} right now "
                       f"({len(items)} anomaly signal(s) active, {len(hits)} matching)."), observed

    if ct == "disk_eta":
        thr = float(p.get("days"))
        filling = [(d.get("mount"), d.get("eta_days")) for d in (signals.get("disk") or [])
                   if d.get("eta_days") is not None]
        observed = {"threshold_days": thr,
                    "disks": [{"mount": m, "eta_days": e} for m, e in filling]}
        if fired:
            return True, fdetail, observed
        if filling:
            m, e = min(filling, key=lambda x: x[1])
            return False, (f"Soonest disk {m} projected full in ~{e}d "
                           f"> threshold {thr}d."), observed
        return False, (f"No disk is currently filling (no positive fill rate) — "
                       f"threshold {thr}d not reached."), observed

    if ct == "vram_eta":
        thr = float(p.get("days"))
        v = signals.get("vram") or {}
        eta_min = v.get("eta_min")
        eta_days = round(eta_min / 1440.0, 2) if eta_min is not None else None
        observed = {"threshold_days": thr, "status": v.get("status"),
                    "eta_days": eta_days, "pct": v.get("pct")}
        if fired:
            return True, fdetail, observed
        if v.get("status") == "filling" and eta_days is not None:
            return False, (f"GPU VRAM filling — projected full in ~{eta_days}d "
                           f"> threshold {thr}d."), observed
        return False, (f"GPU VRAM not on a filling trajectory right now "
                       f"(status {v.get('status') or 'unknown'}) — threshold {thr}d not reached."), observed

    if ct == "cost_budget":
        budget = float(p.get("budget"))
        cm = signals.get("cost_month") or {}
        cur = cm.get("currency", "")
        proj = cm.get("projected_month")
        observed = {"budget": budget, "enabled": bool(cm.get("enabled")),
                    "projected_month": proj, "month_to_date": cm.get("month_to_date"),
                    "currency": cur}
        if not cm.get("enabled"):
            return None, ("Cost tracking is off — can't preview a budget rule instantly. "
                          "Enable energy pricing to evaluate it."), observed
        if fired:
            return True, fdetail, observed
        return False, (f"Projected month cost {cur}{proj} ≤ budget {cur}{budget} "
                       f"(month-to-date {cur}{cm.get('month_to_date')})."), observed

    if ct == "incident":
        want = p.get("severity") or "warning"
        ranks = {"warning": 1, "critical": 2}
        thr = ranks.get(want, 1)
        opens = [i for i in (signals.get("incidents") or []) if i.get("state") == "open"]
        hits = [i for i in opens if ranks.get(i.get("severity"), 0) >= thr]
        observed = {"severity_threshold": want, "open_incidents": len(opens),
                    "at_or_above": len(hits)}
        if fired:
            return True, fdetail, observed
        return False, (f"No open incident at/above {want} right now "
                       f"({len(opens)} open incident(s))."), observed

    if ct == "uptime_down":
        want = p.get("check_id") or "any"
        checks = signals.get("uptime") or []
        if want == "any":
            enabled = [c for c in checks if c.get("enabled")]
            down = [c for c in enabled if c.get("state") == "down"]
            observed = {"target": "any", "enabled_checks": len(enabled), "down": len(down)}
            if fired:
                return True, fdetail, observed
            return False, (f"0 of {len(enabled)} enabled uptime check(s) are down right now."), observed
        match = next((c for c in checks if c.get("id") == want), None)
        st = match.get("state") if match else "unknown"
        label = str((match or {}).get("label") or want)
        observed = {"target": want, "state": st}
        if fired:
            return True, fdetail, observed
        return False, (f"Check '{label}' is {st} (not down)."), observed

    if ct == "cert_expiry":
        want = p.get("check_id") or "any"
        checks = signals.get("uptime") or []
        certs = [c for c in checks if c.get("type") == "cert" and c.get("enabled")]
        if want != "any":
            certs = [c for c in certs if c.get("id") == want]
        warning = [c for c in certs if c.get("state") == "up" and c.get("cert_warn")]
        days = [c.get("days_to_expiry") for c in certs if c.get("days_to_expiry") is not None]
        soonest = min(days) if days else None
        observed = {"target": want, "cert_checks": len(certs),
                    "in_warn_window": len(warning), "soonest_days": soonest}
        if fired:
            return True, fdetail, observed
        if not certs:
            return False, ("No TLS-cert check to preview right now."), observed
        tail = f"; soonest expires in ~{soonest}d" if soonest is not None else ""
        return False, (f"No TLS cert is inside its warn window right now "
                       f"({len(certs)} cert check(s){tail})."), observed

    if ct == "slo_burn":
        want = p.get("check_id") or "any"
        checks = signals.get("uptime") or []
        relevant = [c for c in checks if c.get("enabled") and (want == "any" or c.get("id") == want)]
        policy = p.get("policy") if p.get("policy") in ("single", "multi_window") else "single"
        # Worst-observed 1h burn among checks that have enough SLO data to judge.
        worst = None
        have_data = 0
        for c in relevant:
            slo = c.get("slo") or {}
            if not slo.get("data_sufficient"):
                continue
            have_data += 1
            b1 = slo.get("burn_1h")
            if b1 is None:
                continue
            if worst is None or b1 > worst[1]:
                worst = (c, b1)
        observed = {"policy": policy, "checks_evaluated": len(relevant),
                    "checks_with_data": have_data,
                    "worst_burn_1h": (worst[1] if worst else None)}
        if policy == "multi_window":
            observed["fast_burn"] = p.get("fast_burn")
            observed["slow_burn"] = p.get("slow_burn")
        else:
            observed["burn_threshold"] = p.get("burn_threshold")
        if fired:
            return True, fdetail, observed
        if have_data == 0:
            return None, ("No check has enough SLO history to judge burn yet — "
                          "can't preview this instantly."), observed
        wb = worst[1] if worst else 0
        if policy == "multi_window":
            return False, (f"Worst burn {_reco_num(wb)}×/1h < fast {_reco_num(p.get('fast_burn'))}× "
                           f"(0 checks over budget)."), observed
        return False, (f"Worst burn {_reco_num(wb)}×/1h < threshold {_reco_num(p.get('burn_threshold'))}× "
                       f"(0 checks over budget)."), observed

    # Unknown/unpreviewably ctype — honest null rather than a faked verdict.
    if fired:
        return True, fdetail, {}
    return None, f"Can't preview '{ct}' instantly.", {}

# ── Incidents (correlated-anomaly lifecycle) ──────────────────────────────────
# The z-score detector flags *individual* series (GPU util/VRAM/power/temp + total
# power). In practice a real event — a runaway training job, a thermal throttle, a
# cooling failure — trips several of those at once. The incidents layer groups
# co-firing anomalies into ONE lifecycled object instead of N independent flags:
#
#   • OPEN     — while ≥1 anomaly is active there is exactly one open incident.
#                New correlated series joining the window EXTEND that incident
#                (added as members) rather than spawning a second one.
#   • severity — derived from the worst member's |z| and count: 'critical' for a
#                very strong (|z|≥6) or broad (≥3 series) event, else 'warning'.
#   • CLEAR    — when *all* members have read normal for CLEAR_CONFIRM consecutive
#                evaluation passes (debounce), so a single sample dipping back to
#                baseline doesn't flap the incident closed. cleared_at is stamped
#                and state→'cleared'.
#
# Pure-SQLite, evaluated in the sampler loop from the *already-computed* anomaly
# bundle (no recompute). Wholly additive: it never sends anything and never
# touches the host — recovery NOTIFICATIONS still flow only through opt-in rules.
_INCIDENT_RETENTION   = 100   # keep at most this many incidents (open + cleared)
_INCIDENT_CLEAR_CONFIRM = 3   # consecutive "all normal" passes before clearing

def _incident_severity(members):
    """Derive a severity from the live (active) members of an incident."""
    act = [m for m in members if m.get("active")]
    if not act:
        act = members
    peak = max((abs(m.get("peak_z") or 0) for m in act), default=0)
    if peak >= 6.0 or len(act) >= 3:
        return "critical"
    return "warning"

def _trim_incidents():
    """Drop the oldest CLEARED incidents (and their members) past the retention cap.
    Open incidents are never trimmed."""
    rows = DB.execute("SELECT id FROM incidents ORDER BY opened_at DESC").fetchall()
    if len(rows) <= _INCIDENT_RETENTION:
        return
    drop = [r[0] for r in rows[_INCIDENT_RETENTION:]
            if (DB.execute("SELECT state FROM incidents WHERE id=?", (r[0],)).fetchone() or [""])[0] == "cleared"]
    for iid in drop:
        DB.execute("DELETE FROM incident_members WHERE incident_id=?", (iid,))
        DB.execute("DELETE FROM incidents WHERE id=?", (iid,))

def evaluate_incidents(anomalies, now=None):
    """Fold the current anomaly bundle into the single open incident (opening one if
    needed), or advance the clear-debounce. `anomalies` is the dict returned by
    _zscore_anomalies. Returns the open incident id (or None). Never raises."""
    now = int(now if now is not None else time.time())
    items = (anomalies or {}).get("items") or []
    active = {a["key"]: a for a in items if a.get("key")}
    newly_opened = False          # a fresh incident opened this pass → auto-explain candidate
    newly_cleared = None          # an incident RESOLVED this pass → auto-postmortem candidate
    result = None
    try:
        with LOCK:
            row = DB.execute(
                "SELECT id, severity, opened_at, miss FROM incidents WHERE state='open' "
                "ORDER BY opened_at DESC LIMIT 1").fetchone()
            iid = row[0] if row else None

            if active:
                if iid is None:
                    iid = uuid.uuid4().hex
                    DB.execute("INSERT INTO incidents(id,state,severity,opened_at,updated_at,miss)"
                               " VALUES(?,?,?,?,?,0)", (iid, "open", "warning", now, now))
                    newly_opened = True
                # upsert each active series as a member (extend the incident)
                mrows = DB.execute(
                    "SELECT series, peak_z FROM incident_members WHERE incident_id=?", (iid,)).fetchall()
                known = {s: pz for s, pz in mrows}
                for key, a in active.items():
                    z = abs(a.get("z") or 0)
                    if key in known:
                        peak = max(known[key] or 0, z)
                        DB.execute(
                            "UPDATE incident_members SET active=1, last_seen=?, peak_z=?, "
                            "direction=?, unit=?, peak_value=?, baseline=? "
                            "WHERE incident_id=? AND series=?",
                            (now, peak, a.get("direction"), a.get("unit"), a.get("value"),
                             a.get("baseline"), iid, key))
                    else:
                        DB.execute(
                            "INSERT INTO incident_members(incident_id,series,direction,peak_z,unit,"
                            "peak_value,baseline,first_seen,last_seen,active) VALUES(?,?,?,?,?,?,?,?,?,1)",
                            (iid, key, a.get("direction"), z, a.get("unit"), a.get("value"),
                             a.get("baseline"), now, now))
                # series no longer firing → mark inactive (kept as history on the incident)
                for s in known:
                    if s not in active:
                        DB.execute("UPDATE incident_members SET active=0 WHERE incident_id=? AND series=?",
                                   (iid, s))
                # recompute severity from live members; reset the clear-debounce
                mem = _incident_members(iid)
                DB.execute("UPDATE incidents SET severity=?, updated_at=?, miss=0 WHERE id=?",
                           (_incident_severity(mem), now, iid))
                DB.commit()
                result = iid
            elif iid is None:
                # No anomalies active this pass and nothing open.
                result = None
            else:
                miss = (row[3] or 0) + 1
                if miss >= _INCIDENT_CLEAR_CONFIRM:
                    DB.execute("UPDATE incidents SET state='cleared', cleared_at=?, updated_at=?, miss=? WHERE id=?",
                               (now, now, miss, iid))
                    DB.execute("UPDATE incident_members SET active=0 WHERE incident_id=?", (iid,))
                    newly_cleared = iid       # off-poll auto-postmortem candidate
                    _trim_incidents()
                else:
                    DB.execute("UPDATE incidents SET miss=?, updated_at=? WHERE id=?", (miss, now, iid))
                DB.commit()
                result = None if miss >= _INCIDENT_CLEAR_CONFIRM else iid
        # LOCK released — the auto-explain trigger runs on a DEDICATED worker (it
        # never touches the LLM here). This is the ONLY auto path, and it is off the
        # poll's DB lock: a cheap enqueue that no-ops unless the opt-in is on.
        if newly_opened and result:
            _enqueue_incident_explain(result)
        # An incident RESOLVED this pass → enqueue an off-poll postmortem (opt-in,
        # default OFF; a cheap enqueue AFTER LOCK release; the atomic claim inside
        # get_incident_postmortem makes it at-most-once). NEVER an LLM call here.
        if newly_cleared:
            _enqueue_incident_postmortem(newly_cleared)
        return result
    except Exception as e:
        print("evaluate_incidents error:", e, flush=True)
        try: DB.rollback()
        except Exception: pass
        return None

# ── Auto-explain worker (E1) — dedicated, decoupled off-poll path ─────────────
# When the `incident_auto_explain` opt-in is "1", a NEWLY-OPENED incident gets a
# Copilot explanation generated automatically — but ONLY here, on a single daemon
# worker draining a queue, NEVER inside collect()/health_scan()/evaluate_incidents/
# the sample loop. The collector merely ENQUEUES an id (a cheap put AFTER the DB
# LOCK is released); all LLM work happens on this thread. Rate-limited (a minimum
# gap between generations, one at a time so never more than one in flight) and
# de-duplicated (an id already queued is skipped). Degrades to a no-op when the
# local LLM is unreachable (generate_incident_explanation persists nothing then).
_INCIDENT_EXPLAIN_Q = queue.Queue()
_INCIDENT_EXPLAIN_QUEUED = set()
_INCIDENT_EXPLAIN_QLOCK = threading.Lock()
_INCIDENT_EXPLAIN_MIN_GAP = float(os.environ.get("INCIDENT_EXPLAIN_MIN_GAP", "10"))  # ≥ s between gens
_incident_explain_worker_started = False

def _incident_explain_worker():
    """Drain the auto-explain queue one id at a time, rate-limited. Runs forever on
    a daemon thread; every generation is a graceful no-op when the LLM is down."""
    last = 0.0
    while True:
        iid = _INCIDENT_EXPLAIN_Q.get()
        try:
            with _INCIDENT_EXPLAIN_QLOCK:
                _INCIDENT_EXPLAIN_QUEUED.discard(iid)
            gap = _INCIDENT_EXPLAIN_MIN_GAP - (time.time() - last)
            if gap > 0:
                time.sleep(gap)
            # The opt-in may have flipped OFF since enqueue — re-check before any
            # LLM work. generate_incident_explanation is itself cache-safe (skips
            # the model when an explanation already exists).
            if get_settings().get("incident_auto_explain") == "1":
                generate_incident_explanation(iid, force=False)
                # Off-poll supplementary "probable cause" notification (opt-in,
                # dedup'd, suppression-respecting). Cache read only — no LLM here.
                _maybe_send_incident_cause(iid)
            last = time.time()
        except Exception as e:
            print("incident explain worker error:", e, flush=True)
        finally:
            _INCIDENT_EXPLAIN_Q.task_done()

def _ensure_incident_explain_worker():
    """Lazily start the single worker thread on first real enqueue (so a process
    that never opts in — and the test suite — never spawns it)."""
    global _incident_explain_worker_started
    with _INCIDENT_EXPLAIN_QLOCK:
        if _incident_explain_worker_started:
            return
        threading.Thread(target=_incident_explain_worker, name="incident-explain",
                         daemon=True).start()
        _incident_explain_worker_started = True

def _enqueue_incident_explain(iid):
    """Queue a newly-opened incident for auto-explanation IF the opt-in is on. A
    cheap, DB-LOCK-free trigger called ONLY after evaluate_incidents releases LOCK;
    reads the setting (its own lock), de-dupes, and hands the id to the worker. A
    no-op when the toggle is off (the default). Never raises, never blocks the
    caller on the LLM."""
    try:
        if not iid or get_settings().get("incident_auto_explain") != "1":
            return
        with _INCIDENT_EXPLAIN_QLOCK:
            if iid in _INCIDENT_EXPLAIN_QUEUED:
                return
            _INCIDENT_EXPLAIN_QUEUED.add(iid)
        _ensure_incident_explain_worker()
        _INCIDENT_EXPLAIN_Q.put(iid)
    except Exception as e:
        print("enqueue incident explain error:", e, flush=True)

def _maybe_send_incident_cause(iid):
    """Off-poll SUPPLEMENTARY "probable cause" notification — sent AT MOST ONCE per
    incident by the auto-explain worker AFTER it persists an explanation. Runs ONLY on
    the dedicated worker thread (never on collect()/health_scan()/dispatch), and only
    when EVERY gate passes:
      • both opt-ins ON  — notify_ai_cause=='1' AND incident_auto_explain=='1'
      • notifications usable — ≥1 channel configured (same gate as the rule engine,
        which fires the primary incident notification this supplements)
      • the incident is still OPEN and has a CACHED explanation (pure read — no LLM)
      • severity ≥ alert_min_level
      • NO maintenance / quick-mute window active (same suppression as any alert)
      • not already sent — an ATOMIC claim of ai_cause_notified_at (rowcount guard)
    Reuses dispatch_alert, so a disabled/unconfigured channel is skipped exactly like
    any other notification. Never calls the LLM (reads the cached field only); never
    raises out."""
    try:
        s = get_settings()
        if s.get("notify_ai_cause") != "1" or s.get("incident_auto_explain") != "1":
            return
        if not _configured_channels(s):
            return
        inc = get_incident(iid)          # cached read — zero LLM
        if not inc or inc.get("state") != "open":
            return
        cause = _incident_cause_text(inc)
        if not cause:
            return
        sev = inc.get("severity") or "warning"
        level = "critical" if sev == "critical" else "warning"
        if LEVELS.get(level, 0) < LEVELS.get(s.get("alert_min_level", "warning"), 1):
            return
        now = int(time.time())
        # Maintenance / quick-mute window active → suppress like any other notification.
        in_maint, _end = _in_maintenance(now)
        if in_maint:
            return
        # Atomic at-most-once: CLAIM the send by stamping the dedup flag, but only while
        # still open and not yet sent. rowcount==0 → someone already claimed it (or it
        # closed) → send nothing. Guarantees no duplicate / no notification storm.
        with LOCK:
            cur = DB.execute(
                "UPDATE incidents SET ai_cause_notified_at=? "
                "WHERE id=? AND state='open' AND ai_cause_notified_at IS NULL",
                (now, iid))
            claimed = cur.rowcount
            DB.commit()
        if not claimed:
            return
        title = f"🧠 Probable cause — {sev.capitalize()} incident"
        detail = f"🧠 Probable cause: {cause}"
        for ch, ok, err in dispatch_alert(s, level, title, detail):
            if not ok:
                print(f"incident cause notify {ch} error:", err, flush=True)
    except Exception as e:
        print("incident cause notify error:", e, flush=True)

def _incident_members(iid):
    """Member rows for one incident (caller holds LOCK or accepts a plain read)."""
    rows = DB.execute(
        "SELECT series, direction, peak_z, unit, peak_value, baseline, first_seen, last_seen, active "
        "FROM incident_members WHERE incident_id=? ORDER BY peak_z DESC", (iid,)).fetchall()
    cols = ("series", "direction", "peak_z", "unit", "peak_value", "baseline",
            "first_seen", "last_seen", "active")
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        d["active"] = bool(d["active"])
        out.append(d)
    return out

def list_incidents(limit=50):
    """Recent incidents, open first then most-recent, each with its members. Plain
    reads; never raises out."""
    try:
        with LOCK:
            rows = DB.execute(
                "SELECT id, state, severity, opened_at, updated_at, cleared_at, "
                "ai_explanation, ai_explained_at, ai_model, postmortem_at FROM incidents "
                "ORDER BY (state='open') DESC, opened_at DESC LIMIT ?", (int(limit),)).fetchall()
            cols = ("id", "state", "severity", "opened_at", "updated_at", "cleared_at",
                    "ai_explanation", "ai_explained_at", "ai_model", "postmortem_at")
            out = []
            for r in rows:
                d = dict(zip(cols, r))
                d["has_postmortem"] = bool(d.pop("postmortem_at", None))
                d["members"] = _incident_members(d["id"])
                d["member_count"] = len(d["members"])
                d["active_count"] = sum(1 for m in d["members"] if m["active"])
                out.append(d)
            return out
    except Exception as e:
        print("list_incidents error:", e, flush=True)
        return []

def get_incident(iid):
    """Full detail for ONE incident: all fields + the complete member list + a
    compact derived timeline (opened → each member's first_seen → cleared). Returns
    the dict, or None for an unknown/garbage id. Plain reads; never raises out.
    No topology/secret leak — members are only the monitored telemetry series keys."""
    iid = str(iid or "")
    if not iid:
        return None
    try:
        with LOCK:
            row = DB.execute(
                "SELECT id, state, severity, opened_at, updated_at, cleared_at, miss, "
                "ai_explanation, ai_explained_at, ai_model, "
                "postmortem_json, postmortem_at, postmortem_model "
                "FROM incidents WHERE id=?", (iid,)).fetchone()
            if not row:
                return None
            cols = ("id", "state", "severity", "opened_at", "updated_at", "cleared_at", "miss",
                    "ai_explanation", "ai_explained_at", "ai_model",
                    "postmortem_json", "postmortem_at", "postmortem_model")
            inc = dict(zip(cols, row))
            inc["members"] = _incident_members(iid)
        inc["member_count"] = len(inc["members"])
        inc["active_count"] = sum(1 for m in inc["members"] if m["active"])
        # Compact, cheaply-derived timeline: open, each member's first appearance,
        # and (if cleared) the clear event. Sorted by time, ties broken stably.
        tl = [{"at": inc["opened_at"], "event": "opened",
               "detail": "incident opened", "series": None}]
        for m in sorted(inc["members"], key=lambda x: (x["first_seen"] or 0)):
            arrow = "▲" if m.get("direction") == "spike" else "▼"
            tl.append({"at": m["first_seen"], "event": "member_joined",
                       "series": m["series"],
                       "detail": f"{m['series']} {arrow} (peak σ={round(abs(m.get('peak_z') or 0), 1)})"})
        if inc.get("cleared_at"):
            tl.append({"at": inc["cleared_at"], "event": "cleared",
                       "detail": "all members returned to baseline", "series": None})
        tl.sort(key=lambda e: (e["at"] or 0))
        inc["timeline"] = tl
        # Glanceable, deterministic spike-time disk-I/O attribution for the drawer
        # ("who wrote DURING the spike") — a pure cached ring read, NO LLM, comm
        # only. Omitted when no disk_io member / no history covers the window.
        _sio = _incident_spike_io(inc)
        if _sio:
            inc["spike_io"] = _sio
        # Glanceable flag for the drawer: does a RESOLVED incident already have a
        # persisted postmortem? (a boolean — the prose itself is fetched lazily via
        # the dedicated postmortem endpoint, keeping this poll payload lean + never
        # shipping the LLM prose on the default drawer poll). The raw JSON blob is
        # NOT exposed on this read path (the API boundary strips it).
        inc["has_postmortem"] = bool(inc.get("postmortem_json"))
        return inc
    except Exception as e:
        print("get_incident error:", e, flush=True)
        return None

def incidents_summary():
    """Compact summary for cheap embedding next to `anomalies` on already-polled
    endpoints: open count + the current top (most-severe, newest) open incident."""
    try:
        with LOCK:
            rows = DB.execute(
                "SELECT id, severity, opened_at, updated_at FROM incidents WHERE state='open' "
                "ORDER BY (severity='critical') DESC, opened_at DESC", ()).fetchall()
            if not rows:
                return {"open": 0, "top": None}
            iid, sev, opened, updated = rows[0]
            members = _incident_members(iid)
            return {"open": len(rows), "top": {
                "id": iid, "severity": sev, "opened_at": opened, "updated_at": updated,
                "member_count": len(members), "active_count": sum(1 for m in members if m["active"]),
                "series": [m["series"] for m in members if m["active"]]}}
    except Exception as e:
        print("incidents_summary error:", e, flush=True)
        return {"open": 0, "top": None}

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

def _gpu_vendor(name):
    """Infer a GPU's vendor slug from its marketing name — mirrors the Windows
    probe's VendorOf so the UI reads ONE `vendor` field regardless of host OS.
    Returns exactly one of the fixed slugs {'nvidia','amd','intel','unknown'};
    the UI maps these to fixed CSS classes, so an unexpected/blank name safely
    falls back to the neutral 'unknown' chip (never trusts the raw string)."""
    s = (name or "").lower()
    if re.search(r"nvidia|geforce|quadro|tesla|rtx|gtx", s):
        return "nvidia"
    if re.search(r"radeon|instinct|firepro|vega|\bati\b|\bamd\b", s):
        return "amd"
    if re.search(r"intel|iris|\buhd\b|hd graphics|\barc\b|\bxe\b|igpu", s):
        return "intel"
    return "unknown"

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

def _decode_throttle(hexstr):
    """nvidia-smi throttle bitmask → list of *meaningful* reasons. Idle / app-clocks
    / sync-boost / display bits are normal and intentionally ignored."""
    try:
        mask = int(str(hexstr).strip(), 16)
    except (TypeError, ValueError):
        return []
    return [label for bit, label in _THROTTLE_BITS if mask & bit]

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
            g["mem_util"]    = _gpu_num(p[1])
            g["clk_sm"]      = _gpu_num(p[2])
            g["clk_mem"]     = _gpu_num(p[3])
            g["power_limit"] = _gpu_num(p[4])
            g["temp_mem"]    = _gpu_num(p[5])
            g["pstate"]      = p[6] if (p[6] and not p[6].startswith("[")) else ""
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
            g["throttle"] = _decode_throttle(p[1])
            g["throttled"] = bool(g["throttle"])
            ok = True
        if ok:
            break

def _gpu_extra(gpus):
    """Aggregate the enriched per-card telemetry into one representative dict for the
    always-visible 'GPU right now' panel (single-GPU rigs never see the per-card cards)."""
    if not gpus:
        return {}
    g0 = gpus[0]
    return {
        "mem_util":  round(sum(g.get("mem_util", 0) for g in gpus) / len(gpus)),
        "clk_sm":    round(g0.get("clk_sm", 0)),
        "clk_mem":   round(g0.get("clk_mem", 0)),
        "power_limit": round(sum(g.get("power_limit", 0) for g in gpus)),
        "pstate":    g0.get("pstate", ""),
        "temp_mem":  round(max((g.get("temp_mem", 0) for g in gpus), default=0)),
        "throttled": any(g.get("throttled") for g in gpus),
        "throttle":  sorted({r for g in gpus for r in g.get("throttle", [])}),
    }

# ── AMD GPU (Linux amdgpu via sysfs) ─────────────────────────────────────────
# Pure-stdlib counterpart to the nvidia-smi path: on boxes with no NVIDIA driver
# (or alongside one) we enumerate AMD cards straight from /sys/class/drm and emit
# the SAME per-card dict shape {idx,name,util,mem_used,mem_total,power,temp,...}
# so every downstream consumer (GPU tab, VRAM-ETA, anomalies, gauges, /metrics,
# MQTT, Copilot) lights up unchanged. No ROCm, no extra deps — just file reads,
# each guarded so a missing sysfs attribute degrades that field to None/0 rather
# than dropping the card or crashing the sample. Works the same in a container as
# long as /sys is visible (it is, by default).
AMD_DRM_GLOB = "/sys/class/drm/card*/device"   # patched in tests to a fake tree

def _read_sysfs(path):
    """Stripped contents of a sysfs file, or None if absent/unreadable. Never raises."""
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None

def _sysfs_int(path, scale=1.0):
    """sysfs integer (optionally scaled, e.g. bytes→MB, µW→W, m°C→°C). None on miss."""
    v = _read_sysfs(path)
    if v is None:
        return None
    try:
        return int(float(v) / scale)
    except (TypeError, ValueError):
        return None

# A few well-known AMD device-id → marketing-name hints. Best-effort only: the
# reader never depends on this — an unknown id just falls back to "AMD GPU".
_AMD_NAMES = {
    "0x73bf": "AMD Radeon RX 6900 XT", "0x73a5": "AMD Radeon RX 6950 XT",
    "0x73df": "AMD Radeon RX 6700 XT", "0x744c": "AMD Radeon RX 7900 XTX",
    "0x747e": "AMD Radeon RX 7800 XT", "0x164e": "AMD Raphael (iGPU)",
    "0x15bf": "AMD Phoenix (iGPU)",    "0x1638": "AMD Cezanne (iGPU)",
    "0x740c": "AMD Instinct MI250X",   "0x74a1": "AMD Instinct MI300X",
}

def _amd_card_name(dev):
    """Best-effort marketing name for an AMD card dir. Tries a product marker file,
    then a device-id lookup, then a neutral fallback — never crashes, never None."""
    for marker in ("product_name", "serial_number"):
        v = _read_sysfs(os.path.join(dev, marker))
        if v and not v.lower().startswith("0x"):
            return v
    did = (_read_sysfs(os.path.join(dev, "device")) or "").lower()
    if did in _AMD_NAMES:
        return _AMD_NAMES[did]
    return f"AMD GPU ({did})" if did else "AMD GPU"

def _amd_hwmon(dev):
    """Temp(°C) + power(W) + power_limit(W) from a card's hwmon subdir. Each field is
    independent: a card may expose temp but not power (common on iGPUs). Returns a
    dict with None for anything unreadable so the merge keeps the card regardless."""
    temp = power = plimit = None
    hwmon_root = os.path.join(dev, "hwmon")
    try:
        subdirs = sorted(os.listdir(hwmon_root))
    except Exception:
        subdirs = []
    for sub in subdirs:
        hp = os.path.join(hwmon_root, sub)
        if temp is None:
            # Prefer the 'edge'/first temp sensor; m°C → °C.
            for t in ("temp1_input", "temp2_input", "temp3_input"):
                temp = _sysfs_int(os.path.join(hp, t), scale=1000.0)
                if temp is not None:
                    break
        if power is None:
            for pw in ("power1_average", "power1_input", "power2_average"):
                power = _sysfs_int(os.path.join(hp, pw), scale=1_000_000.0)  # µW → W
                if power is not None:
                    break
        if plimit is None:
            for pc in ("power1_cap", "power1_cap_max", "power2_cap"):
                plimit = _sysfs_int(os.path.join(hp, pc), scale=1_000_000.0)
                if plimit is not None:
                    break
        if temp is not None and power is not None and plimit is not None:
            break
    return {"temp": temp, "power": power, "power_limit": plimit}

def read_amd_gpus():
    """Enumerate AMD (vendor 0x1002) GPUs from /sys/class/drm via pure file reads.
    Returns a list of per-card dicts matching the nvidia-smi shape so the rest of
    the app consumes them unchanged. Empty list if no amdgpu cards / no sysfs.

    Per card: idx,name,vendor='amd',util(%),mem_used(MB),mem_total(MB),power(W),
    temp(°C),power_limit(W). Unreadable numeric fields degrade to 0 (consistent
    with the nvidia path's _gpu_num) so a present card is never dropped."""
    out = []
    try:
        devs = sorted(glob.glob(AMD_DRM_GLOB))
    except Exception:
        return out
    idx = 0
    for dev in devs:
        try:
            vendor = (_read_sysfs(os.path.join(dev, "vendor")) or "").lower()
            if vendor != "0x1002":
                continue   # not AMD (NVIDIA=0x10de, Intel=0x8086) — skip
            # gpu_busy_percent is the amdgpu utilisation counter; absent on some
            # very old kernels / fully virtualised cards → treat as 0, keep card.
            util = _sysfs_int(os.path.join(dev, "gpu_busy_percent"))
            mem_used  = _sysfs_int(os.path.join(dev, "mem_info_vram_used"),  scale=1024 * 1024)
            mem_total = _sysfs_int(os.path.join(dev, "mem_info_vram_total"), scale=1024 * 1024)
            if util is None and mem_total is None:
                # No amdgpu metric nodes at all (e.g. a display-only / vfio-bound
                # card) — nothing useful to report, so skip rather than emit zeros.
                continue
            hw = _amd_hwmon(dev)
            out.append({
                "idx":         idx,
                "name":        _amd_card_name(dev),
                "vendor":      "amd",
                "util":        float(util if util is not None else 0),
                "mem_used":    float(mem_used if mem_used is not None else 0),
                "mem_total":   float(mem_total if mem_total is not None else 0),
                "power":       float(hw["power"] if hw["power"] is not None else 0),
                "temp":        float(hw["temp"] if hw["temp"] is not None else 0),
                "power_limit": float(hw["power_limit"] if hw["power_limit"] is not None else 0),
            })
            idx += 1
        except Exception:
            continue   # one bad card never aborts enumeration of the rest
    return out

def _amd_gpu_extra(gpus):
    """AMD analogue of _gpu_extra: aggregate the per-card power_limit (and whatever
    else AMD exposes) into the single 'GPU right now' panel dict. AMD sysfs doesn't
    surface mem-bw util / clocks / pstate / throttle the way nvidia-smi does, so
    those stay absent (the UI already tolerates missing telemetry fields)."""
    if not gpus:
        return {}
    return {"power_limit": round(sum(g.get("power_limit", 0) for g in gpus)), "vendor": "amd"}

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

def sample_once():
    conts = containers()
    nm = {c["id"]: c["name"] for c in conts}

    # ── GPU half ──────────────────────────────────────────────────────────────
    # Isolated in its own try/except so a flaky, missing or slow nvidia-smi can
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
    try:
        # One CSV row per card (issue #95). Parse each field defensively: nvidia-smi
        # emits the literal "[N/A]" / "[Not Supported]" for power.draw/temperature
        # on many consumer/laptop GPUs and inside containers, even with `nounits` —
        # so degrade just the bad field to 0 rather than dropping the whole card.
        rows = smi(["--query-gpu=index,name,utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu",
                    "--format=csv,noheader,nounits"]).splitlines()
        for line in rows:
            if not line.strip():
                continue
            p = [x.strip() for x in line.split(",")]
            if len(p) < 7:
                continue
            u, mu, mt, pw, tp = (_gpu_num(x) for x in p[2:7])
            gname = p[1] or f"GPU {p[0]}"
            gpus.append({"idx": int(_gpu_num(p[0])), "name": gname,
                         "vendor": _gpu_vendor(gname) if p[1] else "nvidia",
                         "util": u, "mem_used": mu, "mem_total": mt, "power": pw, "temp": tp})
        if not gpus:
            raise ValueError("nvidia-smi returned no GPU rows")
        gpu_avail = True
        # Aggregate across cards for the existing single-GPU views: VRAM + power are
        # the pool, utilisation is averaged, temperature is the hottest card.
        mem_used  = sum(g["mem_used"] for g in gpus)
        mem_total = sum(g["mem_total"] for g in gpus)
        power     = sum(g["power"] for g in gpus)
        util      = round(sum(g["util"] for g in gpus) / len(gpus))
        temp      = max(g["temp"] for g in gpus)
        _enrich_gpus(gpus)                 # mem-bw util, clocks, power limit, throttle reasons (best-effort)
        gpu_extra = _gpu_extra(gpus)
        for line in smi(["--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"]).splitlines():
            if line.strip():
                pid, mem = (p.strip() for p in line.split(","))
                svc = service_for_pid(pid, nm)
                procs[svc] = procs.get(svc, 0) + _gpu_num(mem)
                try:
                    gpu_pids[int(pid)] = gpu_pids.get(int(pid), 0) + _gpu_num(mem)
                except ValueError:
                    pass
    except Exception as e:
        # Log only on the ok→fail edge so a permanently GPU-less host doesn't spam.
        if LATEST.get("gpu_avail"):
            print("GPU sample failed (continuing without GPU):", e, flush=True)

    # ── AMD GPU half ────────────────────────────────────────────────────────────
    # Hardware-agnostic fallback/extension: when nvidia-smi found no cards (no NVIDIA
    # driver, or a pure-AMD/mixed box) enumerate AMD cards from sysfs and APPEND them
    # so they flow through the identical aggregate + per-card pipeline below. Mixed
    # NVIDIA+AMD rigs get both — AMD cards are re-indexed after the NVIDIA ones so the
    # `idx` space stays unique. Per-model VRAM attribution (nvidia-smi compute-apps)
    # stays NVIDIA-only by design; AMD just contributes util/VRAM/power/temp. Fully
    # isolated so a sysfs quirk can never wedge host metrics.
    try:
        amd = read_amd_gpus()
        if amd:
            base = len(gpus)
            for g in amd:
                g["idx"] = base + g["idx"]
                gpus.append(g)
            gpu_avail = True
            # Recompute the aggregates the single-GPU views read, now spanning both
            # vendors: VRAM + power pool, util averaged, temperature = hottest card.
            mem_used  = sum(g["mem_used"] for g in gpus)
            mem_total = sum(g["mem_total"] for g in gpus)
            power     = sum(g["power"] for g in gpus)
            util      = round(sum(g["util"] for g in gpus) / len(gpus))
            temp      = max(g["temp"] for g in gpus)
            if not gpu_extra:                       # NVIDIA enrichment wins if present
                gpu_extra = _amd_gpu_extra(amd)
    except Exception as e:
        if LATEST.get("gpu_avail"):
            print("AMD GPU sample failed (continuing):", e, flush=True)

    # Detect models from EVERY recognised AI server, not just the ones holding the GPU
    # right now — so a server that has unloaded its model (e.g. OLLAMA_KEEP_ALIVE
    # expired) or sits between requests still shows up as Idle instead of vanishing.
    # Probes are independent 2 s-timeout HTTP calls, so run them in parallel.
    ai = [c for c in conts if _match_probe(c)]
    models = []
    if ai:
        with ThreadPoolExecutor(max_workers=min(8, len(ai))) as ex:
            found_lists = list(ex.map(probe_models, ai))
        for ct, found in zip(ai, found_lists):
            svc = ct["name"]
            smem = procs.get(svc)                         # MB this server holds on the GPU now
            api_vram = any(v is not None for _, v in found)
            for mdl, vram in found:
                if vram is not None:                      # server reported its own VRAM (Ollama)
                    models.append((svc, mdl, round(vram)))
                elif not api_vram and len(found) == 1 and smem:
                    models.append((svc, mdl, round(smem)))  # single model ↔ all the server's VRAM
                else:
                    models.append((svc, mdl, None))         # server up but idle / can't attribute

    # Attribute model-server traffic to its callers (who is driving Ollama, etc.).
    edges = sample_callers(conts, {c["name"] for c in ai})

    # Model intelligence: per-model metadata (Ollama /api/show, cached) + live serving
    # telemetry (vLLM/TGI /metrics). Both best-effort — a slow/absent endpoint must
    # never wedge the sample, so each is isolated.
    try:
        model_meta = collect_model_meta(ai, models)
    except Exception:
        model_meta = {}
    try:
        serving = collect_serving(ai)
    except Exception:
        serving = []
    try:
        training = collect_training(gpu_pids)
    except Exception:
        training = []
    try:
        devtools = collect_devtools(gpu_pids)
    except Exception:
        devtools = []

    host = read_host()
    # Measured CPU/DRAM watts (RAPL) + per-process CPU breakdown — both best-effort.
    # Call collect_top_processes ONCE here (the sampler cadence) and cache it so the
    # Top-processes card + the cost attribution share one delta (health_scan reuses it).
    rapl = {}
    try:
        rapl = read_rapl_power()
    except Exception:
        rapl = {}
    cpu_power, dram_power = rapl.get("cpu_w"), rapl.get("dram_w")
    try:
        top_cpu = collect_top_processes()
    except Exception:
        top_cpu = None
    HEALTH["processes"] = top_cpu
    ts = int(time.time())
    if _DB_MAINTENANCE:
        return
    with LOCK:
        # When the GPU is absent/failed, store NULL for the GPU columns (not 0) so
        # history charts skip the gap via AVG() instead of showing a fake 0 dip;
        # the host columns are always real.
        gcols = (util, mem_used, mem_total, power, temp) if gpu_avail else (None,)*5
        DB.execute("INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp,cpu,ram_used,ram_total,load1,ctemp,cpu_power,dram_power)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (ts, *gcols, host["cpu"], host["ram_used"],
                    host["ram_total"], host["load1"], host["ctemp"], cpu_power, dram_power))
        for svc, mem in procs.items():
            DB.execute("INSERT INTO proc VALUES(?,?,?)", (ts, svc, mem))
        pp_rows = _attribute_power_rows(ts, power, procs, cpu_power, top_cpu)
        if pp_rows:
            DB.executemany("INSERT INTO power_proc(ts,kind,name,watts) VALUES(?,?,?,?)", pp_rows)
        for svc, mdl, vram in models:
            if vram is not None:          # persist only VRAM-bearing rows; idle catalogue
                DB.execute("INSERT INTO models VALUES(?,?,?,?)", (ts, svc, mdl, vram))  # lives in LATEST only
        for (caller, server), n in edges.items():
            DB.execute("INSERT INTO edges VALUES(?,?,?,?)", (ts, caller, server, n))
        # Per-GPU history only when there's more than one card (single-GPU rigs are
        # already covered by the aggregate `samples` table) — keeps storage lean.
        if gpu_avail and len(gpus) > 1:
            for g in gpus:
                DB.execute("INSERT INTO gpu_samples(ts,idx,util,mem_used,mem_total,power,temp) "
                           "VALUES(?,?,?,?,?,?,?)",
                           (ts, g["idx"], g["util"], g["mem_used"], g["mem_total"], g["power"], g["temp"]))
        DB.executemany("INSERT INTO net_samples(ts,iface,bytes_in,bytes_out) VALUES(?,?,?,?)",
                       _net_rows(ts, nm))   # host NICs + per-container talkers (#30)
        # Disk usage barely moves, so sample it sparsely (~5 min) — enough history
        # for a fill-rate trend without bloating the DB. Feeds /api/forecast.
        if ts % 300 < INTERVAL:
            for d in (host.get("disks") or []):
                DB.execute("INSERT INTO disk_samples(ts,mount,used,total) VALUES(?,?,?,?)",
                           (ts, d["mount"], d.get("used"), d.get("total")))
            # Coarse public-status heartbeat sample (~5 min) — aggregated up/down
            # per anonymized subsystem key. Cheap; shares this held LOCK.
            sample_status_history(ts)
        # Disk I/O moves fast, so sample it on its own tighter cadence (~45s) into
        # a dedicated 7-day ring — dense enough for per-device sparklines + the
        # z-score anomaly baseline without bloating the DB. Sourced from the
        # health_scan snapshot (populated every 15s) so no extra /proc read here.
        if ts % 45 < INTERVAL:
            dio = HEALTH.get("disk_io") or {}
            if dio.get("available"):
                for it in (dio.get("items") or []):
                    DB.execute("INSERT INTO disk_io_samples(ts,device,read_mb_s,write_mb_s,util_pct) "
                               "VALUES(?,?,?,?,?)",
                               (ts, it["device"], it.get("read_mb_s"),
                                it.get("write_mb_s"), it.get("util_pct")))
            # Persist a BOUNDED per-process I/O ring: only the top-few writers +
            # top-few readers from the attribution we already computed (comm only,
            # never argv). Deduped by pid → at most ~6 rows/poll, not all ~20
            # candidates. Feeds spike-time attribution (_proc_io_at); NEVER the
            # public status surface. Rides the existing ~45s cadence (no new loop).
            _pio = (HEALTH.get("processes") or {}).get("io") or {}
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
                    DB.executemany("INSERT INTO proc_io_samples(ts,pid,comm,read_bps,write_bps) "
                                   "VALUES(?,?,?,?,?)", _pio_rows)
        if ts % 360 < INTERVAL:
            for t in ("samples", "proc", "models", "edges", "events", "gpu_samples", "net_samples", "power_proc", "disk_samples"):
                DB.execute(f"DELETE FROM {t} WHERE ts<?", (ts - RETENTION,))
            DB.execute("DELETE FROM status_history WHERE ts<?", (ts - _STATHIST_RETENTION,))
            DB.execute("DELETE FROM disk_io_samples WHERE ts<?", (ts - _DISK_IO_RETENTION,))
            DB.execute("DELETE FROM proc_io_samples WHERE ts<?", (ts - _PROC_IO_RETENTION,))
        if ts % 60 < INTERVAL:   # stale-run janitor: a crashed/disconnected push run -> killed
            DB.execute("UPDATE runs SET status='killed', ended_at=COALESCE(ended_at,heartbeat_at,?) "
                       "WHERE status='running' AND heartbeat_at IS NOT NULL AND heartbeat_at < ?",
                       (ts, ts - 180))
        DB.commit()
    # MLflow pull (network; outside the lock) every ~5 min when configured.
    if get_settings().get("mlflow_uri") and ts % 300 < INTERVAL:
        try:
            sync_mlflow()
        except Exception as e:
            print("mlflow sync error:", e, flush=True)
    LATEST.update(ts=ts, util=util, mem_used=mem_used, mem_total=mem_total, power=power, temp=temp,
                  cpu_power=cpu_power, dram_power=dram_power, rapl=rapl.get("domains"),
                  gpu_avail=gpu_avail, gpus=gpus, gpu_extra=gpu_extra,
                  procs=sorted(({"service": s, "mem": round(m)} for s, m in procs.items()), key=lambda x: -x["mem"]),
                  models=[{"service": s, "model": m, "vram": v} for s, m, v in models],
                  model_meta=model_meta, serving=serving, training=training, devtools=devtools,
                  callers=sorted(({"caller": c, "server": s, "conns": n} for (c, s), n in edges.items()),
                                 key=lambda x: -x["conns"]), host=host)

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
            with LOCK:
                DB.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?)", (ets, svc, "oom", line.strip()[:300]))
                DB.commit()
        _scan_since[svc] = int(time.time())

def collector():
    last_oom = last_health = last_notify = 0
    while True:
        try:
            sample_once()
            now = time.time()
            if now - last_oom > 60:
                oom_scan(); last_oom = now
            if now - last_health > 15:
                health_scan(); last_health = now
            # Notifier runs *after* the latest health/oom data is in place, so
            # state-change detection sees a consistent snapshot.
            if now - last_notify > 20:
                try: notify_scan()
                except Exception as e: print("notify_scan error:", e, flush=True)
                # Incidents + user-defined rule engine. Both consume the z-score
                # anomaly bundle, so compute it once and share it. The incident layer
                # always runs (it only writes to local SQLite, sends nothing); the
                # rule engine stays inert unless a rule is enabled. The forecast
                # signals (disk/cost/vram) are only needed by rules, so they're
                # computed lazily when at least one rule is enabled.
                try:
                    nowi = int(now)
                    has_rules = any(r["enabled"] for r in list_rules())
                    ctx = _cost_ctx() if has_rules else None
                    with LOCK:
                        cur = DB.cursor()
                        anomalies = _zscore_anomalies(cur, nowi)
                        sig = {"anomalies": anomalies}
                        if has_rules:
                            sig["disk"] = _disk_forecasts(cur, nowi)
                            sig["cost_month"] = _cost_projection(cur, ctx, nowi)
                            sig["vram"] = _vram_forecast(cur, nowi)
                    evaluate_incidents(anomalies, nowi)
                    if has_rules:
                        # Run incidents first (above) so the incident-rule type sees the
                        # just-updated open incident. list_incidents() takes LOCK itself,
                        # so read it here (outside the block that built `sig`).
                        sig["incidents"] = list_incidents()
                        # uptime_down rules read the per-check states from the bundle.
                        # uptime_overview() takes LOCK internally, so read it here —
                        # outside the block above — and never nest the non-reentrant lock.
                        # Without this the collector hands evaluate_rules a bundle with no
                        # "uptime" key, so uptime_down rules would silently never fire.
                        sig["uptime"] = uptime_overview().get("checks", [])
                        evaluate_rules(sig)
                except Exception as e: print("evaluate_rules error:", e, flush=True)
                # Scheduled Lab Copilot digest. Runs OUTSIDE any held lock (it
                # builds context that acquires LOCK itself); inert unless the user
                # enabled it AND configured a channel. Edge-triggered to fire
                # exactly once per day at/after the target local time.
                try: maybe_send_digest(now)
                except Exception as e: print("maybe_send_digest error:", e, flush=True)
                last_notify = now
        except Exception as e:
            print("collector error:", e, flush=True)
        time.sleep(INTERVAL)

# ── Insights ──────────────────────────────────────────────────────────────
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

@app.route("/api/data")
def api_data():
    rng = request.args.get("range", "6h")
    span = RANGES.get(rng, 21600); now = int(time.time())
    with LOCK:
        cur = DB.cursor()
        since = (cur.execute("SELECT MIN(ts) FROM samples").fetchone()[0] or now) if span is None else now - span
        bk = max(INTERVAL, round(max(1, now - since) / MAX_POINTS))
        tot = cur.execute("SELECT (ts/?)*? b,AVG(util),AVG(mem_used),MAX(mem_used),AVG(power),AVG(temp),"
                          "AVG(cpu),AVG(ram_used),AVG(ram_total),AVG(load1),AVG(ctemp) "
                          "FROM samples WHERE ts>=? GROUP BY b ORDER BY b", (bk, bk, since)).fetchall()
        labels = [int(r[0]) for r in tot]
        idx = {b: i for i, b in enumerate(labels)}
        total = {"util": [round(r[1] or 0) for r in tot], "mem": [round(r[2] or 0) for r in tot],
                 "mempk": [round(r[3] or 0) for r in tot], "power": [round(r[4] or 0) for r in tot],
                 "temp": [round(r[5] or 0) for r in tot], "cpu": [round(r[6] or 0) for r in tot],
                 "ram_used": [round(r[7] or 0) for r in tot], "ram_total": [round(r[8] or 0) for r in tot],
                 "load1": [round(r[9] or 0, 2) for r in tot], "ctemp": [round(r[10] or 0) for r in tot]}
        services = {}
        for b, svc, mem in cur.execute("SELECT (ts/?)*? b,service,AVG(mem) FROM proc WHERE ts>=? GROUP BY b,service",
                                       (bk, bk, since)).fetchall():
            i = idx.get(int(b))
            if i is not None:
                services.setdefault(svc, [0] * len(labels))[i] = round(mem or 0)
        other = [max(0, total["mem"][i] - sum(s[i] for s in services.values())) for i in range(len(labels))]
        ticks = cur.execute("SELECT COUNT(*) FROM samples WHERE ts>=?", (since,)).fetchone()[0] or 1
        summary = sorted(({"service": s, "peak": round(pk), "avg": round(av), "present": round(100 * cnt / ticks)}
                          for s, pk, av, cnt in cur.execute(
                              "SELECT service,MAX(mem),AVG(mem),COUNT(DISTINCT ts) FROM proc WHERE ts>=? GROUP BY service",
                              (since,)).fetchall()), key=lambda x: -x["peak"])
        model_summary = sorted(({"service": s, "model": m, "peak": round(pk or 0), "avg": round(av or 0)}
                                for s, m, pk, av in cur.execute(
                                    "SELECT service,model,MAX(vram),AVG(vram) FROM models WHERE ts>=? AND vram IS NOT NULL "
                                    "GROUP BY service,model", (since,)).fetchall()), key=lambda x: -x["peak"])
        # Caller attribution: connection-seconds per (caller → server) over the range.
        # Each sample of `conns` open connections represents INTERVAL seconds of traffic.
        callers = sorted(({"caller": c, "server": s, "seconds": int((tot or 0) * INTERVAL), "samples": n}
                          for c, s, tot, n in cur.execute(
                              "SELECT caller,server,SUM(conns),COUNT(DISTINCT ts) FROM edges WHERE ts>=? "
                              "GROUP BY caller,server", (since,)).fetchall()), key=lambda x: -x["seconds"])
        evs = [{"ts": t, "service": s, "kind": k, "detail": d}
               for t, s, k, d in cur.execute("SELECT ts,service,kind,detail FROM events WHERE ts>=? ORDER BY ts",
                                              (since,)).fetchall()]
        for e in evs:
            row = cur.execute("SELECT service,mem FROM proc WHERE ts<=? AND service!=? ORDER BY ts DESC,mem DESC LIMIT 1",
                              (e["ts"] + INTERVAL, e["service"])).fetchone()
            if row:
                e["blame"] = (f"{e['service']} lost to {row[0]} (holding {round(row[1])} MB) at "
                              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(e['ts']))}.")
        mem_total = LATEST["mem_total"] or 24576
        peak = max(total["mempk"]) if total["mempk"] else 0
        insights = build_insights(total, services, mem_total, evs, LATEST["host"])
    return jsonify({"version": VERSION, "range": rng, "bucket_sec": bk, "labels": labels, "total": total,
                    "services": services, "other": other, "summary": summary, "model_summary": model_summary,
                    "callers": callers, "events": evs, "insights": insights, "pressure_free_mb": PRESSURE_MB,
                    "mem_total": mem_total, "peak_mem": peak, "now": LATEST})

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

@app.route("/api/cost")
def api_cost():
    """Power → kWh → money (#25), now tariff-aware. Integrates the GPU `power`
    samples we already collect; each sample stands for INTERVAL seconds, so
    energy(kWh) = sum(power_W) * INTERVAL / 3_600_000.

    Single mode (default): cost = energy * kwh_price — byte-for-byte the original
    behaviour. Dual mode: each sample is billed at the night price inside the
    (possibly midnight-wrapping) night window and the day price otherwise, split
    per window. A blank night price silently degrades to single, so a user who
    doesn't know their rates keeps the simple average. The card stays hidden until
    a day price is set (`enabled`)."""
    s = get_settings()
    def fnum(key):
        v = (s.get(key) or "").strip()
        if v == "":
            return None
        try:
            return float(v)
        except ValueError:
            return None

    day_price   = fnum("kwh_price") or 0.0
    night_price = fnum("kwh_price_night")
    mode = "dual" if (s.get("tariff_mode") == "dual" and night_price is not None
                      and day_price > 0) else "single"
    currency = s.get("currency") or "$"
    is_night = _make_is_night(s.get("night_start", "22:00"), s.get("night_end", "06:00"))

    rng = request.args.get("range", "7d")
    span = RANGES.get(rng, 604800)
    now = int(time.time())
    kwh_per_wsample = INTERVAL / 3_600_000.0   # one power sample -> kWh

    with LOCK:
        cur = DB.cursor()
        def avg_w(since):
            return round(cur.execute("SELECT AVG(power) FROM samples WHERE ts>=?", (since,)).fetchone()[0] or 0)
        def total_kwh(since):
            tot = cur.execute("SELECT SUM(power) FROM samples WHERE ts>=?", (since,)).fetchone()[0] or 0
            return tot * kwh_per_wsample
        def split_kwh(since):
            """One pass over (ts,power) >= since -> (day_kwh, night_kwh)."""
            day_w = night_w = 0.0
            for ts, p in cur.execute("SELECT ts,power FROM samples WHERE ts>=? AND power IS NOT NULL", (since,)):
                if is_night(ts):
                    night_w += p
                else:
                    day_w += p
            return day_w * kwh_per_wsample, night_w * kwh_per_wsample

        lt = time.localtime(now)
        midnight = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        wins = {"today": midnight, "d7": now - 604800, "d30": now - 2592000}
        kwh, cost, split = {}, {}, {}
        for w, since in wins.items():
            if mode == "dual":
                dk, nk = split_kwh(since)
            else:
                dk, nk = total_kwh(since), 0.0       # single: one SUM, no per-row loop
            dc, nc = dk * day_price, nk * (night_price or 0.0)
            kwh[w]  = round(dk + nk, 3)
            cost[w] = round(dc + nc, 2)
            split[w] = {"day_kwh": round(dk, 3), "night_kwh": round(nk, 3),
                        "day_cost": round(dc, 2), "night_cost": round(nc, 2)}

        # Cumulative-cost series across the selected range (mirrors api_data buckets).
        since = (cur.execute("SELECT MIN(ts) FROM samples").fetchone()[0] or now) if span is None else now - span
        bk = max(INTERVAL, round(max(1, now - since) / MAX_POINTS))
        labels, cost_cum, running = [], [], 0.0
        if mode == "dual":                            # stream + classify per bucket (one pass)
            acc = {}
            for ts, p in cur.execute("SELECT ts,power FROM samples WHERE ts>=? AND power IS NOT NULL ORDER BY ts", (since,)):
                b = (ts // bk) * bk
                price = night_price if is_night(ts) else day_price
                acc[b] = acc.get(b, 0.0) + (p or 0) * kwh_per_wsample * price
            for b in sorted(acc):
                running += acc[b]
                labels.append(int(b)); cost_cum.append(round(running, 4))
        else:                                         # single: cheap SQL-bucketed path (unchanged)
            rows = cur.execute("SELECT (ts/?)*? b, SUM(power) FROM samples WHERE ts>=? GROUP BY b ORDER BY b",
                               (bk, bk, since)).fetchall()
            for b, p in rows:
                running += (p or 0) * kwh_per_wsample * day_price
                labels.append(int(b)); cost_cum.append(round(running, 4))

    return jsonify({
        "enabled": day_price > 0, "kwh_price": day_price, "currency": currency,
        "range": rng, "bucket_sec": bk,
        "current_w": round(LATEST.get("power") or 0),
        "avg_24h_w": avg_w(now - 86400), "avg_7d_w": avg_w(now - 604800),
        "kwh": kwh, "cost": cost, "split": split,
        "tariff": {"mode": mode, "price_day": day_price, "price_night": night_price,
                   "night_start": s.get("night_start", "22:00"),
                   "night_end": s.get("night_end", "06:00")},
        "series": {"labels": labels, "cost_cum": cost_cum},
    })

# ── Costs page: per-machine → per-component → per-process (with drilldown) ─────
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

@app.route("/api/costs")
def api_costs():
    """Richer power+cost view for the Costs page: per-machine totals, a stacked
    component breakdown (GPU measured, CPU/DRAM measured via RAPL, optional operator
    'other' baseline) and a ranked per-process/service/model breakdown — all over a
    selectable range and tariff-aware. /api/cost (GPU-only) is left untouched."""
    ctx = _cost_ctx()
    cur = ctx["currency"]
    rng = request.args.get("range", "7d")
    span = RANGES.get(rng, 604800)
    now = int(time.time())
    kwh_per = INTERVAL / 3_600_000.0
    with LOCK:
        c = DB.cursor()
        since = (c.execute("SELECT MIN(ts) FROM samples").fetchone()[0] or now) if span is None else now - span
        bk = max(INTERVAL, round(max(1, now - since) / MAX_POINTS))
        comp = c.execute(f"SELECT (ts/?)*? b, AVG(power), AVG(cpu_power), AVG(dram_power) "
                         f"FROM samples WHERE ts>=? GROUP BY b ORDER BY b", (bk, bk, since)).fetchall()
        # component energy + cost over the range (tariff-aware, one streaming pass)
        comp_kwh = {"gpu": 0.0, "cpu": 0.0, "dram": 0.0}
        cost_range = 0.0
        nticks = 0
        for ts, p, cp, dp in c.execute("SELECT ts,power,cpu_power,dram_power FROM samples WHERE ts>=?", (since,)):
            nticks += 1
            price = _price_at(ctx, ts)
            tot = (p or 0) + (cp or 0) + (dp or 0)
            comp_kwh["gpu"] += (p or 0) * kwh_per
            comp_kwh["cpu"] += (cp or 0) * kwh_per
            comp_kwh["dram"] += (dp or 0) * kwh_per
            cost_range += tot * kwh_per * price
        # today/d7/d30 total-cost windows (machine total watts, tariff-aware)
        def win_cost(start):
            tot = 0.0
            for ts, w in c.execute(f"SELECT ts, {_TOTAL_W_EXPR} w FROM samples WHERE ts>=?", (start,)):
                tot += (w or 0) * kwh_per * _price_at(ctx, ts)
            return round(tot, 2)
        lt = time.localtime(now)
        midnight = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        cost_win = {"today": win_cost(midnight), "d7": win_cost(now - 604800), "d30": win_cost(now - 2592000)}
        # ranked per-entity breakdown from power_proc (tariff-aware day/night split)
        acc = {}
        for ts, kind, name, watts in c.execute("SELECT ts,kind,name,watts FROM power_proc WHERE ts>=?", (since,)):
            a = acc.setdefault((kind, name), [0.0, 0.0])
            if ctx["mode"] == "dual" and ctx["is_night"](ts):
                a[1] += watts
            else:
                a[0] += watts
    hours = max(1e-9, (now - since) / 3600.0)
    idle_w = ctx["idle_w"]
    labels = [int(r[0]) for r in comp]
    series = {"labels": labels,
              "gpu":  [round(r[1] or 0) for r in comp],
              "cpu":  [round(r[2] or 0) if r[2] is not None else 0 for r in comp],
              "dram": [round(r[3] or 0) if r[3] is not None else 0 for r in comp]}
    have_cpu = any(r[2] is not None for r in comp)
    have_dram = any(r[3] is not None for r in comp)
    if not have_cpu: series.pop("cpu")
    if not have_dram: series.pop("dram")
    if idle_w:
        series["other"] = [round(idle_w)] * len(labels)
    breakdown = []
    for (kind, name), (dayw, nightw) in acc.items():
        energy = (dayw + nightw) * kwh_per
        cost = (dayw * ctx["day"] + nightw * (ctx["night"] if ctx["night"] is not None else ctx["day"])) * kwh_per
        breakdown.append({"kind": kind, "name": name, "energy_kwh": round(energy, 4),
                          "cost": round(cost, 4), "avg_w": round((dayw + nightw) / max(1, nticks))})
    breakdown.sort(key=lambda x: -x["energy_kwh"])
    now_gpu = round(LATEST.get("power") or 0)
    now_cpu = round(LATEST.get("cpu_power") or 0) if LATEST.get("cpu_power") is not None else None
    now_dram = round(LATEST.get("dram_power") or 0) if LATEST.get("dram_power") is not None else None
    now_total = now_gpu + (now_cpu or 0) + (now_dram or 0) + (round(idle_w) if idle_w else 0)
    measured = ["gpu"] + (["cpu"] if have_cpu else []) + (["dram"] if have_dram else [])
    machine = {"name": "local",
               "now_w": {"gpu": now_gpu, "cpu": now_cpu, "dram": now_dram, "total": now_total},
               "energy_kwh": {k: round(v, 3) for k, v in comp_kwh.items() if (k != "dram" or have_dram)},
               "cost": cost_win, "cost_range": round(cost_range, 2),
               "measured": measured, "estimated": (["other"] if idle_w else [])}
    machine["energy_kwh"]["total"] = round(sum(machine["energy_kwh"][k] for k in machine["energy_kwh"] if k != "total"), 3)
    return jsonify({
        "enabled": ctx["day"] > 0, "range": rng, "bucket_sec": bk, "currency": cur,
        "rapl_available": have_cpu,
        "tariff": {"mode": ctx["mode"], "price_day": ctx["day"], "price_night": ctx["night"],
                   "night_start": ctx["night_start"], "night_end": ctx["night_end"]},
        "machines": [machine], "components": series, "breakdown": breakdown[:40],
    })

@app.route("/api/costs/entity")
def api_costs_entity():
    """Per-entity drilldown: a power + cumulative-cost time-series for one
    process/service/model over the range, plus what resources it used."""
    ctx = _cost_ctx()
    name = request.args.get("name", "")
    kind = request.args.get("kind", "")
    rng = request.args.get("range", "7d")
    span = RANGES.get(rng, 604800)
    now = int(time.time())
    kwh_per = INTERVAL / 3_600_000.0
    with LOCK:
        c = DB.cursor()
        since = (c.execute("SELECT MIN(ts) FROM power_proc").fetchone()[0] or now) if span is None else now - span
        bk = max(INTERVAL, round(max(1, now - since) / MAX_POINTS))
        q = "SELECT (ts/?)*? b, AVG(watts), MAX(watts) FROM power_proc WHERE name=? AND ts>=?"
        args = [bk, bk, name, since]
        if kind:
            q += " AND kind=?"; args.append(kind)
        q += " GROUP BY b ORDER BY b"
        rows = c.execute(q, args).fetchall()
        # cumulative tariff-aware cost needs per-bucket price; classify by bucket start ts
        vram_peak = None
        if kind != "cpu":
            vram_peak = c.execute("SELECT MAX(mem) FROM proc WHERE service=? AND ts>=?", (name, since)).fetchone()[0]
    labels, watts, cost_cum, running, energy = [], [], [], 0.0, 0.0
    peak = 0.0
    for b, avgw, maxw in rows:
        labels.append(int(b)); watts.append(round(avgw or 0))
        peak = max(peak, maxw or 0)
        e = (avgw or 0) * kwh_per * (bk / INTERVAL)   # energy this bucket (avg W over bk seconds)
        energy += e
        running += e * _price_at(ctx, int(b))
        cost_cum.append(round(running, 4))
    return jsonify({
        "name": name, "kind": kind, "range": rng, "bucket_sec": bk, "currency": ctx["currency"],
        "energy_kwh": round(energy, 4), "cost": round(running, 2),
        "avg_w": round(sum(watts) / len(watts)) if watts else 0, "peak_w": round(peak),
        "series": {"labels": labels, "watts": watts, "cost_cum": cost_cum},
        "resources": {"gpu_vram_peak_mb": round(vram_peak) if vram_peak else None},
    })

@app.route("/api/cost/heatmap")
def api_cost_heatmap():
    """Busy-vs-quiet rhythm of the lab as a 7×24 grid (local day-of-week × hour).

    Each historical `samples` row is the machine's total draw (GPU+CPU+DRAM) for
    one INTERVAL tick. We bucket every tick by its LOCAL weekday/hour and average
    the watts in each cell, then derive a cost-rate (€/h) for that cell at the
    tariff's price for that hour band — reusing the same `_cost_ctx`/`_price_at`
    machinery as the Costs page, so the €/kWh math never diverges. Sparse cells
    carry their own sample count so the UI can be honest about coverage.

    Pure-Python aggregation, read outside any held lock, always 200. When cost is
    disabled (no tariff) we still return the power grid and `enabled:false` so the
    card can render watts and prompt for a price.
    """
    ctx = _cost_ctx()
    cur = ctx["currency"]
    # window: last N days, sane default 30, capped at a year so a huge DB can't stall
    try:
        days = int(request.args.get("days", "30"))
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(days, 365))
    now = int(time.time())
    since = now - days * 86400
    kwh_per = INTERVAL / 3_600_000.0      # one power sample -> kWh

    # 7×24 accumulators: summed watts and tick count per local day/hour cell
    sum_w = [[0.0] * 24 for _ in range(7)]
    cnt   = [[0]   * 24 for _ in range(7)]
    span_min = span_max = None
    try:
        with LOCK:
            c = DB.cursor()
            rows = c.execute(
                f"SELECT ts, {_TOTAL_W_EXPR} w FROM samples WHERE ts>=? ORDER BY ts",
                (since,)).fetchall()
        # aggregate OUTSIDE the lock — pure Python, no DB calls below
        for ts, w in rows:
            lt = time.localtime(ts)
            # Python weekday(): Mon=0..Sun=6 — matches our locale day labels
            d = lt.tm_wday
            h = lt.tm_hour
            sum_w[d][h] += (w or 0)
            cnt[d][h] += 1
            if span_min is None or ts < span_min:
                span_min = ts
            if span_max is None or ts > span_max:
                span_max = ts
    except Exception:
        rows = []

    # build the grids; price each cell at the tariff for a representative ts in it
    rep_ts = span_max or now
    rep_lt = time.localtime(rep_ts)
    # anchor to local midnight of the most recent observed day so is_night() lands
    # in the right band per (day,hour); the date component is irrelevant to the band
    anchor = int(time.mktime((rep_lt.tm_year, rep_lt.tm_mon, rep_lt.tm_mday,
                              0, 0, 0, 0, 0, -1)))
    avg_w  = [[None] * 24 for _ in range(7)]
    cost_h = [[None] * 24 for _ in range(7)]   # cost per HOUR at this cell's mean draw
    max_w = max_cost = 0.0
    busiest = quietest = None                  # by avg watts
    total_ticks = 0
    for d in range(7):
        for h in range(24):
            n = cnt[d][h]
            total_ticks += n
            if n == 0:
                continue
            aw = sum_w[d][h] / n
            avg_w[d][h] = round(aw)
            # price for this hour band: a ts at hour h on the anchor day
            cell_ts = anchor + h * 3600
            price = _price_at(ctx, cell_ts)
            ch = aw / 1000.0 * price           # W -> kW * €/kWh = €/h
            cost_h[d][h] = round(ch, 4)
            max_w = max(max_w, aw)
            max_cost = max(max_cost, ch)
            if busiest is None or aw > busiest["avg_w"]:
                busiest = {"day": d, "hour": h, "avg_w": round(aw),
                           "cost_h": round(ch, 4), "samples": n}
            if quietest is None or aw < quietest["avg_w"]:
                quietest = {"day": d, "hour": h, "avg_w": round(aw),
                            "cost_h": round(ch, 4), "samples": n}

    # busy vs quiet bands: top/bottom quartile of populated cells by avg watts
    populated = [(avg_w[d][h], cost_h[d][h])
                 for d in range(7) for h in range(24) if avg_w[d][h] is not None]
    bands = None
    if len(populated) >= 4:
        ordered = sorted(populated, key=lambda x: x[0])
        q = max(1, len(ordered) // 4)
        quiet_band = ordered[:q]
        busy_band = ordered[-q:]
        def band_stats(b):
            return {"avg_w": round(sum(x[0] for x in b) / len(b)),
                    "avg_cost_h": round(sum((x[1] or 0) for x in b) / len(b), 4),
                    "cells": len(b)}
        bands = {"busy": band_stats(busy_band), "quiet": band_stats(quiet_band)}

    # per-day rollups (busiest / quietest day by mean watts across populated hours)
    day_avg = [None] * 7
    for d in range(7):
        vals = [avg_w[d][h] for h in range(24) if avg_w[d][h] is not None]
        if vals:
            day_avg[d] = round(sum(vals) / len(vals))

    coverage = round(total_ticks / max(1, days * 24 * 3600 / INTERVAL), 4)
    # "ready" once we have at least a day's worth of ticks spread across cells
    populated_cells = len(populated)
    ready = total_ticks >= (86400 / INTERVAL) and populated_cells >= 6

    return jsonify({
        "ok": True,
        "enabled": ctx["day"] > 0,
        "currency": cur,
        "days": days,
        "interval_sec": INTERVAL,
        "ready": ready,
        "rows": 7, "cols": 24,
        "avg_w": avg_w,
        "cost_h": cost_h,
        "samples": cnt,
        "day_avg_w": day_avg,
        "max_w": round(max_w),
        "max_cost_h": round(max_cost, 4),
        "busiest": busiest,
        "quietest": quietest,
        "bands": bands,
        "total_ticks": total_ticks,
        "populated_cells": populated_cells,
        "coverage": coverage,
        "span": {"min": span_min, "max": span_max},
        "tariff": {"mode": ctx["mode"], "price_day": ctx["day"], "price_night": ctx["night"],
                   "night_start": ctx["night_start"], "night_end": ctx["night_end"]},
    })

# ── Forecasts: pure-Python linear extrapolation over SQLite history (no deps) ──
def _linfit(xs, ys):
    """Ordinary least-squares slope+intercept for y = a*x + b, plus R² goodness.
    Pure stdlib. Returns (slope, intercept, r2) or None when the fit is undefined
    (fewer than 2 points or zero variance in x)."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    sst = sum((y - my) ** 2 for y in ys)
    if sst == 0:
        r2 = 1.0 if slope == 0 else 0.0
    else:
        ssr = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
        r2 = max(0.0, 1.0 - ssr / sst)
    return slope, intercept, r2

def _disk_forecasts(cur, now):
    """Per-mount disk-fill ETA from the disk_samples history. Fits used-GB vs time
    over the recent window; an ETA is reported only when the trend is meaningfully
    positive and the fit is decent, otherwise the mount reads 'stable'/'collecting'.
    """
    WINDOW = 30 * 86400      # look back up to 30 days of disk history
    MIN_POINTS = 4           # need a few points before any line is trustworthy
    MIN_GB_PER_DAY = 0.1     # below this, call it stable (noise, not a trend)
    MIN_R2 = 0.5             # require a halfway-credible linear fit
    since = now - WINDOW
    mounts = [r[0] for r in cur.execute(
        "SELECT DISTINCT mount FROM disk_samples WHERE ts>=? ORDER BY mount", (since,)).fetchall()]
    out = []
    for mp in mounts:
        rows = cur.execute(
            "SELECT ts, used, total FROM disk_samples WHERE mount=? AND ts>=? ORDER BY ts",
            (mp, since)).fetchall()
        rows = [r for r in rows if r[1] is not None and r[2] is not None]
        total = rows[-1][2] if rows else None
        used = rows[-1][1] if rows else None
        pct = round(100 * used / total) if (total and used is not None) else None
        item = {"mount": mp, "used_gb": used, "total_gb": total, "pct": pct,
                "status": "collecting", "gb_per_day": None, "eta_days": None,
                "eta_ts": None, "free_gb": (round(total - used, 1) if (total and used is not None) else None)}
        if len(rows) < MIN_POINTS:
            out.append(item); continue
        xs = [r[0] / 86400.0 for r in rows]    # time in days (keeps slope in GB/day)
        ys = [r[1] for r in rows]
        fit = _linfit(xs, ys)
        if not fit:
            item["status"] = "stable"; item["gb_per_day"] = 0.0; out.append(item); continue
        slope, intercept, r2 = fit
        item["gb_per_day"] = round(slope, 2)
        item["r2"] = round(r2, 2)
        if slope < MIN_GB_PER_DAY or r2 < MIN_R2 or total is None:
            item["status"] = "stable"
        elif used is not None and used >= total:
            item["status"] = "full"; item["eta_days"] = 0; item["eta_ts"] = now
        else:
            days = (total - used) / slope
            if days > 3650:                    # >10y out is effectively "stable"
                item["status"] = "stable"
            else:
                item["status"] = "filling"
                item["eta_days"] = round(days, 1)
                item["eta_ts"] = int(now + days * 86400)
        out.append(item)
    return out

def _vram_forecast(cur, now):
    """GPU VRAM-exhaustion ETA + headroom, from the `samples.mem_used` series.

    Mirrors the disk-fill ETA approach (same _linfit, same R²/slope gating): an
    ETA-to-full is reported only when VRAM is meaningfully trending up under load
    with a credible linear fit; otherwise the card reads 'stable'/'collecting'.

    Also reports *headroom*: total VRAM minus the VRAM currently held by loaded
    models, framed as "X GB free" — using the per-model VRAM the dashboard
    already tracks (LATEST['models']). All values in MB internally; *_gb fields
    are GB (÷1024) for the UI.

    Returns a dict; never raises (callers wrap in try, but this degrades on its
    own to 'collecting'). Units: rate is MB/min so a short window stays legible.
    """
    WINDOW   = 6 * 3600     # last ~6h of GPU history (10s cadence)
    MIN_PTS  = 30           # need a real trend before extrapolating
    MIN_R2   = 0.5          # half-credible linear fit, same bar as disk ETA
    MIN_MB_PER_MIN = 1.0    # below this, call it stable (idle drift / noise)
    out = {"status": "collecting", "total_mb": None, "total_gb": None,
           "used_mb": None, "used_gb": None, "pct": None,
           "models_mb": None, "models_gb": None, "free_mb": None, "free_gb": None,
           "mb_per_min": None, "r2": None, "eta_min": None, "eta_ts": None}

    # Total + currently-loaded headroom from the live snapshot the UI already has.
    total_mb = LATEST.get("mem_total") or None
    models_mb = sum((m.get("vram") or 0) for m in (LATEST.get("models") or []))
    if total_mb:
        out["total_mb"] = round(total_mb)
        out["total_gb"] = round(total_mb / 1024.0, 1)
        out["models_mb"] = round(models_mb)
        out["models_gb"] = round(models_mb / 1024.0, 1)
        free_mb = max(0.0, total_mb - models_mb)
        out["free_mb"] = round(free_mb)
        out["free_gb"] = round(free_mb / 1024.0, 1)

    since = now - WINDOW
    try:
        rows = cur.execute(
            "SELECT ts, mem_used, mem_total FROM samples WHERE ts>=? ORDER BY ts",
            (since,)).fetchall()
    except Exception:
        rows = []
    rows = [r for r in rows if r[1] is not None]
    if rows:
        used = rows[-1][1]
        tot = rows[-1][2] or total_mb
        out["used_mb"] = round(used)
        out["used_gb"] = round(used / 1024.0, 1)
        if tot:
            out["pct"] = round(100 * used / tot)
            if out["total_mb"] is None:            # fall back to series total
                out["total_mb"] = round(tot); out["total_gb"] = round(tot / 1024.0, 1)

    if len(rows) < MIN_PTS:
        return out
    xs = [r[0] / 60.0 for r in rows]               # time in minutes -> slope MB/min
    ys = [r[1] for r in rows]
    fit = _linfit(xs, ys)
    if not fit:
        out["status"] = "stable"; out["mb_per_min"] = 0.0; return out
    slope, _intercept, r2 = fit
    out["mb_per_min"] = round(slope, 2)
    out["r2"] = round(r2, 2)
    tot = out["total_mb"]
    used = out["used_mb"]
    if slope < MIN_MB_PER_MIN or r2 < MIN_R2 or not tot or used is None:
        out["status"] = "stable"
    elif used >= tot:
        out["status"] = "full"; out["eta_min"] = 0; out["eta_ts"] = now
    else:
        mins = (tot - used) / slope
        if mins > 60 * 24 * 365:                   # >1y out is effectively stable
            out["status"] = "stable"
        else:
            out["status"] = "filling"
            out["eta_min"] = round(mins, 1)
            out["eta_ts"] = int(now + mins * 60)
    return out

def _cost_projection(cur, ctx, now):
    """Extrapolate month-to-date machine energy cost to a full-month estimate, with
    a vs-last-month delta when last month's history is available. Tariff-aware."""
    if not ctx["day"] or ctx["day"] <= 0:
        return {"enabled": False}
    kwh_per = INTERVAL / 3_600_000.0
    lt = time.localtime(now)
    month_start = int(time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)))
    # days in the current month (for projecting the full month)
    if lt.tm_mon == 12:
        next_month = int(time.mktime((lt.tm_year + 1, 1, 1, 0, 0, 0, 0, 0, -1)))
    else:
        next_month = int(time.mktime((lt.tm_year, lt.tm_mon + 1, 1, 0, 0, 0, 0, 0, -1)))
    days_in_month = (next_month - month_start) / 86400.0
    elapsed_days = max(1e-6, (now - month_start) / 86400.0)

    def cost_between(start, end):
        tot = 0.0
        for ts, w in cur.execute(f"SELECT ts, {_TOTAL_W_EXPR} w FROM samples WHERE ts>=? AND ts<?", (start, end)):
            tot += (w or 0) * kwh_per * _price_at(ctx, ts)
        return tot

    # Month-to-date total AND the cumulative-by-day trend in ONE pass over the month
    # rows (no second identical SELECT, no new poll). `mtd` accumulates in the SAME
    # per-row order as the old cost_between loop, so its float arithmetic — and thus
    # round(mtd, 2) — is bit-for-bit identical to before; the per-day buckets are a
    # side tally off the same iteration for the hero sparkline. Each cost_cum point is
    # the running spend through the end of that day (≤ 31 points, tiny).
    def cost_month_and_cum(start, end):
        mtd_tot = 0.0
        per_day = {}            # day-index (since month_start) -> that day's cost
        for ts, w in cur.execute(f"SELECT ts, {_TOTAL_W_EXPR} w FROM samples WHERE ts>=? AND ts<?", (start, end)):
            c = (w or 0) * kwh_per * _price_at(ctx, ts)
            mtd_tot += c
            d = int((ts - start) // 86400)
            per_day[d] = per_day.get(d, 0.0) + c
        cum, run = [], 0.0
        for d in range(0, (max(per_day) + 1) if per_day else 0):
            run += per_day.get(d, 0.0)
            cum.append(round(run, 4))
        return mtd_tot, cum

    mtd, cost_cum = cost_month_and_cum(month_start, now)
    have_data = cur.execute("SELECT 1 FROM samples WHERE ts>=? LIMIT 1", (month_start,)).fetchone() is not None
    projected = mtd / elapsed_days * days_in_month if elapsed_days > 0 else 0.0

    # last full month, for a comparison delta
    pm_year, pm_mon = (lt.tm_year - 1, 12) if lt.tm_mon == 1 else (lt.tm_year, lt.tm_mon - 1)
    last_start = int(time.mktime((pm_year, pm_mon, 1, 0, 0, 0, 0, 0, -1)))
    last_cost = cost_between(last_start, month_start)
    have_last = cur.execute("SELECT 1 FROM samples WHERE ts>=? AND ts<? LIMIT 1",
                            (last_start, month_start)).fetchone() is not None
    delta_pct = None
    if have_last and last_cost > 0:
        delta_pct = round(100 * (projected - last_cost) / last_cost)
    return {
        "enabled": True, "currency": ctx["currency"],
        "month_to_date": round(mtd, 2), "projected_month": round(projected, 2),
        "elapsed_days": round(elapsed_days, 1), "days_in_month": round(days_in_month),
        "last_month": round(last_cost, 2) if have_last else None,
        "delta_pct": delta_pct, "collecting": not have_data,
        # cumulative MTD trend (one point per elapsed day) for the hero cost sparkline
        "cost_cum": cost_cum,
    }

# ── Anomaly detection (z-score on the GPU/power history) ──────────────────────
# Robust trailing-window z-score over the per-INTERVAL `samples` series. We flag
# only the latest reading and only when the baseline is trustworthy (enough
# points, non-trivial variance) — so a flat/idle rig never cries wolf.
_ANOMALY_SERIES = (
    # (key, column, unit, min_dev) — min_dev is the smallest absolute deviation
    # (in the series' own units) we treat as meaningful. A reading must be both
    # ≥ Z_THRESH sigma AND at least min_dev away from baseline to flag, so tiny
    # wobbles on an otherwise-flat series never cry wolf even if z is large.
    ("gpu_util",  "util",      "%",  10.0),
    ("gpu_vram",  "mem_used",  "MB", 256.0),
    ("gpu_power", "power",     "W",  20.0),
    ("gpu_temp",  "temp",      "°C", 5.0),
    ("power_draw", _TOTAL_W_EXPR, "W", 20.0),
)

_DISK_IO_MIN_DEV = 15.0    # MB/s: below this, a device's wobble isn't worth flagging

def _disk_io_anomaly_items(cur, now, window, min_pts, z_thresh):
    """Per-device z-score flags on total (read+write) disk throughput, using the
    SAME rolling-baseline maths as `_zscore_anomalies` so I/O spikes flow into the
    anomaly ribbon, incident grouping and the 'anomaly' alert rule for free.
    Returns (items, checked, enough). Never raises."""
    since = now - window
    by_dev = {}
    try:
        for dev, r, w in cur.execute(
                "SELECT device, read_mb_s, write_mb_s FROM disk_io_samples "
                "WHERE ts>=? ORDER BY ts", (since,)):
            by_dev.setdefault(dev, []).append((r or 0.0) + (w or 0.0))
    except Exception:
        return [], 0, False
    items, checked, enough = [], 0, False
    for dev, vals in by_dev.items():
        if len(vals) < min_pts:
            continue
        enough = True
        checked += 1
        latest = vals[-1]
        base = vals[:-1]
        n = len(base)
        mean = sum(base) / n
        sd = (sum((v - mean) ** 2 for v in base) / n) ** 0.5
        dev_amt = latest - mean
        if abs(dev_amt) < _DISK_IO_MIN_DEV or sd <= 0:
            continue
        z = dev_amt / sd
        if abs(z) < z_thresh:
            continue
        items.append({
            "key": "disk_io:" + dev, "device": dev, "unit": "MB/s",
            "value": round(latest, 1), "baseline": round(mean, 1),
            "z": round(z, 1), "stddev": round(sd, 1),
            "direction": "spike" if z > 0 else "dip",
            "magnitude": round(abs(latest - mean), 1),
            "samples": n + 1,
        })
    return items, checked, enough


def _zscore_anomalies(cur, now):
    """Flag the latest reading of each key series when it sits far from a recent
    rolling baseline. Pure stdlib. Window = last ~6h of samples; baseline mean +
    population stddev are computed over the window *excluding* the latest point,
    so the point is scored against its own recent history, not itself.

    Returns a list of anomaly dicts (only series that actually fired) and a
    `checked` count, plus a `status` so the UI can show a clear empty state:
      • 'quiet'      — series checked, nothing anomalous
      • 'collecting' — not enough history on any series yet
    Never raises on a bad/short/flat series — those are simply skipped."""
    WINDOW   = 6 * 3600    # trailing baseline window (~6h at 10s cadence)
    MIN_PTS  = 30          # need a real baseline before scoring anything
    Z_THRESH = 3.0         # |z| at/above this is "look here", not noise
    since = now - WINDOW
    cols = ", ".join(c for _, c, _, _ in _ANOMALY_SERIES)
    try:
        rows = cur.execute(
            f"SELECT {cols} FROM samples WHERE ts>=? ORDER BY ts", (since,)).fetchall()
    except Exception:
        rows = []
    out, checked = [], 0
    enough = False
    for i, (key, _col, unit, min_dev) in enumerate(_ANOMALY_SERIES):
        vals = [r[i] for r in rows if r[i] is not None]
        if len(vals) < MIN_PTS:
            continue
        enough = True
        checked += 1
        latest = vals[-1]
        base = vals[:-1]                       # score the point vs its own history
        n = len(base)
        mean = sum(base) / n
        var = sum((v - mean) ** 2 for v in base) / n   # population variance
        sd = var ** 0.5
        dev = latest - mean
        if abs(dev) < min_dev:                 # too small to matter (flat/idle/noise)
            continue
        if sd <= 0:                            # zero-variance baseline; the min_dev
            continue                           # gate above already proved it's not flat
        z = dev / sd
        if abs(z) < Z_THRESH:
            continue
        out.append({
            "key": key, "unit": unit,
            "value": round(latest, 1), "baseline": round(mean, 1),
            "z": round(z, 1), "stddev": round(sd, 1),
            "direction": "spike" if z > 0 else "dip",
            "magnitude": round(abs(latest - mean), 1),
            "samples": n + 1,
        })
    # Merge per-device disk-I/O flags into the same bundle (same window/threshold),
    # so a storage spike rides the existing anomaly ribbon / incident / rule paths.
    dio_items, dio_checked, dio_enough = _disk_io_anomaly_items(cur, now, WINDOW, MIN_PTS, Z_THRESH)
    out.extend(dio_items)
    checked += dio_checked
    enough = enough or dio_enough
    # sort most-extreme first so the worst offender leads the card
    out.sort(key=lambda a: abs(a["z"]), reverse=True)
    status = "quiet" if enough else "collecting"
    return {"status": status, "checked": checked, "threshold": Z_THRESH,
            "window_h": WINDOW // 3600, "items": out}

# ── Demo mode (E4): seed realistic synthetic history on a fresh DB ─────────────
# When DEMO_MODE is on AND the DB has no recent real history, lay down ~7 days of
# believable per-sample series plus a slice of the previous calendar month, so the
# history-backed features (disk-fill ETA, cost projection, z-score anomalies,
# VRAM-ETA, Copilot digest) all light up within seconds. Idempotent: guarded on a
# settings marker *and* on whether real samples already exist, so it never clobbers
# a live instance and re-running is a no-op. Pure stdlib; writes through the same
# columns the live sampler uses. Never raises — a seed failure must not block boot.
_DEMO_MARKER = "demo_seeded"

def _demo_already_seeded(cur):
    row = cur.execute("SELECT value FROM settings WHERE key=?", (_DEMO_MARKER,)).fetchone()
    return bool(row and (row[0] or "").strip() in ("1", "true", "yes", "on"))

def _demo_has_real_history(cur, now):
    """True when there's already non-trivial sample history (real or a prior seed)
    in the last 24h — the signal that this DB is in use and must not be touched."""
    try:
        n = cur.execute("SELECT COUNT(*) FROM samples WHERE ts>=?", (now - 86400,)).fetchone()[0]
    except Exception:
        n = 0
    return n > 50

def _demo_util_at(lt):
    """A believable GPU-utilisation shape for a given localtime: busy 'work hours'
    (~9-18) on weekdays, calm overnight and lighter on weekends, with mild noise.
    Returns util% in [0,100]; callers derive power/temp/VRAM from it."""
    import math, random
    h = lt.tm_hour + lt.tm_min / 60.0
    weekend = lt.tm_wday >= 5
    # smooth bell centred on ~13:30, widened across the working day
    base = 70.0 * math.exp(-((h - 13.5) ** 2) / (2 * 4.0 ** 2))
    if weekend:
        base *= 0.45
    idle = 6.0                       # there's always a little background load
    jitter = random.uniform(-6, 8)
    return max(0.0, min(100.0, idle + base + jitter))

def _seed_demo_data():
    """Seed synthetic history when DEMO_MODE is on and the DB is fresh. Safe to call
    unconditionally at startup — it self-gates and swallows all errors."""
    if not DEMO_MODE:
        return
    import random
    random.seed(20260715)            # deterministic shape across restarts/tests
    now = int(time.time())
    try:
        with LOCK:
            cur = DB.cursor()
            if _demo_already_seeded(cur) or _demo_has_real_history(cur, now):
                return
            lt0 = time.localtime(now)
            month_start = int(time.mktime((lt0.tm_year, lt0.tm_mon, 1, 0, 0, 0, 0, 0, -1)))
            pm_year, pm_mon = (lt0.tm_year - 1, 12) if lt0.tm_mon == 1 else (lt0.tm_year, lt0.tm_mon - 1)
            last_month_start = int(time.mktime((pm_year, pm_mon, 1, 0, 0, 0, 0, 0, -1)))
            total_vram = 24576.0     # RTX 3090-class card, matches the live arena rig

            def sample_row(ts):
                lt = time.localtime(ts)
                util = _demo_util_at(lt)
                frac = util / 100.0
                # VRAM: a resident model footprint that creeps up slightly under load
                mem_used = 4200.0 + frac * 11000.0 + random.uniform(-300, 300)
                mem_used = max(800.0, min(total_vram - 200, mem_used))
                power = 38.0 + frac * 290.0 + random.uniform(-12, 12)      # idle→~330W
                temp = 34.0 + frac * 38.0 + random.uniform(-2, 2)          # 34→~72°C
                cpu = 8.0 + frac * 45.0 + random.uniform(-4, 6)
                ram_total = 128 * 1024.0
                ram_used = (22 + frac * 40) / 100.0 * ram_total + random.uniform(-1500, 1500)
                load1 = round(0.6 + frac * 9.0 + random.uniform(-0.4, 0.6), 2)
                ctemp = 36.0 + frac * 24.0 + random.uniform(-2, 2)
                cpu_power = 22.0 + frac * 95.0 + random.uniform(-6, 6)     # RAPL package
                dram_power = 5.0 + frac * 9.0 + random.uniform(-1, 1)
                return (ts, round(util), round(mem_used), round(total_vram), round(power, 1),
                        round(temp, 1), round(cpu, 1), round(ram_used), round(ram_total),
                        load1, round(ctemp, 1), round(cpu_power, 1), round(dram_power, 1))

            rows = []
            # Older band: from the 1st of last month → 7 days ago, sparse (5-min) — just
            # enough for the cost projection's vs-last-month delta without bloating.
            recent_start = now - 7 * 86400
            for ts in range(last_month_start, recent_start, 300):
                rows.append(sample_row(ts))
            # Recent band: last 7 days at 60s — dense enough for every rolling window
            # (anomaly/VRAM use the last ~6h; MIN_PTS=30 needs ≥30 points there).
            for ts in range(recent_start, now, 60):
                rows.append(sample_row(ts))

            # Deliberate spikes so the z-score detector has a real target and
            # "Explain this spike" has something to explain. The detector scores the
            # *latest* reading vs its trailing baseline, so the final samples must be
            # the anomaly: a sharp thermal+power+util spike on top of the normal shape.
            if rows:
                for off, mul in ((4, 0.55), (3, 0.8), (2, 0.95), (1, 1.0), (0, 1.0)):
                    idx = len(rows) - 1 - off
                    if idx < 0:
                        continue
                    ts, util, mem_used, mt, power, temp, cpu, ru, rt, load1, ctemp, cpw, dpw = rows[idx]
                    rows[idx] = (ts, min(100, round(util + 60 * mul)), min(int(total_vram - 100), round(mem_used + 6500 * mul)),
                                 mt, round(power + 260 * mul, 1), round(temp + 28 * mul, 1),
                                 min(100.0, round(cpu + 40 * mul, 1)), ru, rt,
                                 round(load1 + 14 * mul, 2), round(ctemp + 18 * mul, 1),
                                 round(cpw + 70 * mul, 1), dpw)

            DB.executemany(
                "INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp,"
                "cpu,ram_used,ram_total,load1,ctemp,cpu_power,dram_power) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

            # Disk history: one mount filling at a credible rate (clears the slope/R²
            # gate → a real "days until full" ETA), one essentially stable. Sampled at
            # ~5 min like the live sampler does.
            disk_rows = []
            for ts in range(recent_start, now, 300):
                d = (ts - recent_start) / 86400.0           # days since window start
                # /data: 1.86 TB total, ~880 GB used and climbing ~14 GB/day → ~weeks left
                data_used = 880.0 + 14.0 * d + random.uniform(-1.5, 1.5)
                disk_rows.append((ts, "/data", round(data_used, 2), 1862.0))
                # /: 468 GB total, flat ~120 GB used (stable, no ETA)
                root_used = 120.0 + random.uniform(-0.8, 0.8)
                disk_rows.append((ts, "/", round(root_used, 2), 468.0))
            DB.executemany("INSERT INTO disk_samples(ts,mount,used,total) VALUES(?,?,?,?)", disk_rows)

            # A couple of model VRAM rows + an OOM event so the model/events panels and
            # the Copilot digest have something concrete to phrase.
            for ts in range(now - 3600, now, 600):
                DB.execute("INSERT INTO models VALUES(?,?,?,?)", (ts, "ollama", "qwen2.5:14b", 9200))
                DB.execute("INSERT INTO models VALUES(?,?,?,?)", (ts, "ollama", "nomic-embed-text", 640))
            DB.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?)",
                       (now - 5 * 86400, "stable-diffusion", "oom",
                        "CUDA error: out of memory (demo) — allocation of 2.10 GiB failed"))

            # Status history: synthesize ~90 days of coarse status samples so the
            # public /status page renders complete out of the box — the 90-day uptime
            # ribbon (_status_daily over the 'overall' key) AND the per-subsystem
            # heartbeat strips (_status_history's last _STATHIST_CELLS buckets) — instead
            # of waiting days for the live sampler to accumulate. Same {ts,key,state}
            # shape, same bucket alignment, same 0..3 rank meaning the readers expect.
            #
            # Realism is DETERMINISTIC (derived from the day index, no RNG) so it's
            # reproducible across restarts/tests: mostly ok (rank 0) with a degraded day
            # (rank 1-2) every ~17 days and a brief down blip (rank 3) every ~40 days →
            # the ribbon shows ~98-99% uptime with a couple of amber/red daily cells, and
            # the recent heartbeat strip is mostly-green with the odd non-green cell.
            #
            # Cadence is mixed to keep row counts sane: HOURLY across the full 90-day
            # ribbon range, then 5-min (live cadence) over the last ~2 days that feed the
            # per-bucket heartbeat window. ~90d×24h + 2d×288 buckets × 5 keys ≈ 14k rows.
            stat_rows = []
            sh_recent_span = 2 * 86400          # last 2 days at live 5-min cadence
            sh_hourly_start = now - _DAILY_RIBBON_DAYS * 86400 - 86400
            sh_recent_start = now - sh_recent_span

            def _demo_day_states(day_idx):
                """Per-subsystem ranks for whole-day baseline 'day_idx' (0 = oldest).
                Deterministic: a degraded day every ~17 days (a couple of subsystems
                blip to warn), a down blip every ~40 days (overall goes down via one
                subsystem). Most days are fully ok (rank 0)."""
                base = {k: 0 for k in _STATHIST_KEYS}
                # Two degraded days + one brief outage across the 90-day window keep the
                # ribbon at a believable ~96-97% with a couple of amber cells + one red.
                if day_idx % 90 == 23:           # single brief outage (down)
                    base["containers"] = 3
                elif day_idx % 90 in (41, 67):   # two degraded days
                    base["services"] = 2
                base["overall"] = max(base[k] for k in _STATHIST_KEYS if k != "overall")
                return base

            def _emit_status_bucket(bucket_ts):
                day_idx = int((bucket_ts - sh_hourly_start) // 86400)
                states = _demo_day_states(day_idx)
                # Within a non-ok day, only a short slice of buckets actually carries the
                # bad rank (a real incident is bounded in time); the rest of the day is
                # ok. The daily rollup takes the worst rank seen, so a single bad bucket
                # still tints the day's cell while keeping the bucket-level uptime high.
                hod = time.localtime(bucket_ts).tm_hour
                incident = 9 <= hod <= 11        # mid-morning incident window
                bucket_states = {}
                for k in _STATHIST_KEYS:
                    if k == "overall":
                        continue
                    r = states[k]
                    if r > 0 and not incident:
                        r = 0                    # incident bounded in time within the day
                    bucket_states[k] = int(r)
                # overall is the worst across subsystems for THIS bucket
                bucket_states["overall"] = max(bucket_states.values()) if bucket_states else 0
                for k in _STATHIST_KEYS:
                    stat_rows.append((bucket_ts, k, bucket_states[k]))

            # Hourly band across the whole ribbon range (up to the dense recent band).
            for ts in range(sh_hourly_start - (sh_hourly_start % _STATHIST_BUCKET),
                            sh_recent_start, 3600):
                _emit_status_bucket(ts - (ts % _STATHIST_BUCKET))
            # Dense band: last ~2 days at the live 5-min bucket cadence so the heartbeat
            # strip's last _STATHIST_CELLS buckets are fully populated.
            for ts in range(sh_recent_start - (sh_recent_start % _STATHIST_BUCKET),
                            now + _STATHIST_BUCKET, _STATHIST_BUCKET):
                _emit_status_bucket(ts - (ts % _STATHIST_BUCKET))
            DB.executemany("INSERT OR REPLACE INTO status_history(ts,key,state) VALUES(?,?,?)",
                           stat_rows)

            # Tariff defaults so the cost projection renders out of the box (only if
            # the operator hasn't set their own price).
            srow = cur.execute("SELECT value FROM settings WHERE key='kwh_price'").fetchone()
            if not (srow and (srow[0] or "").strip()):
                DB.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('kwh_price','0.28')")
            crow = cur.execute("SELECT value FROM settings WHERE key='currency'").fetchone()
            if not (crow and (crow[0] or "").strip()):
                DB.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('currency','$')")

            DB.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?, '1')", (_DEMO_MARKER,))
            DB.commit()
            print(f"DEMO_MODE: seeded {len(rows)} sample rows + {len(disk_rows)} disk rows "
                  f"+ {len(stat_rows)} status-history rows "
                  f"(synthetic history; real instances are unaffected).", flush=True)
    except Exception as e:
        print("DEMO_MODE seed skipped (continuing):", e, flush=True)


# ── Lab Health Score ──────────────────────────────────────────────────────────
# A single deterministic 0-100 headline for the cockpit, synthesized ONLY from
# signals the app already produces (no LLM, no new probing). Fully explainable:
#
#     score = clamp(100 + Σ factor.delta, 0, 100)
#
# The base is 100 — a perfectly quiet, healthy lab. Each category can only DEDUCT
# (never add), and only for BAD DATA ACTUALLY OBSERVED; a missing / still-warming
# signal never penalizes (absent ≠ unhealthy). Per-category deductions are bounded
# by a documented cap so no single category can sink the whole score, and the
# running total is clamped to [0,100]. Because the caps sum to > 100, many
# simultaneous problems drive the score to 0 (clamped) — as they should.
#
# Category caps (max deduction) + rationale — the model's only tunables:
_HS_CAPS = {
    "uptime":    50,   # a monitored check being DOWN is the most serious signal
    "incidents": 30,   # open correlated-anomaly incidents (worse when critical)
    "slo":       20,   # error-budget exhausted / fast burn on a check
    "disk":      15,   # imminent disk-fill ETA
    "vram":      12,   # imminent GPU-VRAM-exhaustion ETA
    "anomalies": 18,   # currently-firing z-score anomalies
    "thermal":   15,   # GPU temperature near the throttle ceiling / throttling
}
# English fallback labels (the client re-localizes via the `key`; `detail`/`meta`
# carry the live numbers so the UI can localize the sentence too).
_HS_LABELS = {
    "uptime":    "Uptime checks",
    "incidents": "Open incidents",
    "slo":       "SLO error budget",
    "disk":      "Disk capacity",
    "vram":      "GPU VRAM capacity",
    "anomalies": "Active anomalies",
    "thermal":   "GPU thermals",
}
# Band thresholds (score >= cut → band), best-first; each maps to a colour tier.
_HS_BANDS = ((90, "excellent", "ok"), (75, "good", "ok"),
             (50, "fair", "warn"), (0, "at_risk", "crit"))
# Throttle reasons that count as a THERMAL health signal. A routine power/util cap
# ("Power cap") or an external power brake is normal, intended operation — a cool,
# idle GPU sitting at its configured power limit is NOT a health problem — so those
# reasons must never deduct under the thermal category. Only explicit thermal
# slowdowns (which _decode_throttle labels exactly) are a genuine thermal signal.
_HS_THERMAL_THROTTLE = ("SW thermal", "HW thermal")

def _hs_band(score):
    for cut, band, tier in _HS_BANDS:
        if score >= cut:
            return band, tier
    return "at_risk", "crit"

def _hs_num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None

def compute_health_score(signals, now=None):
    """Pure, deterministic Lab Health Score over already-computed signals. NO I/O,
    NO LLM — it only does arithmetic on the dict handed in. `signals` keys (all
    optional; any may be absent/None without penalty):
        uptime    : list of check dicts (enabled/state + optional 'slo' sub-dict)
        incidents : {'open': int, 'critical': int}
        anomalies : {'items': [{'key',...}, ...]}
        disk      : list of mount forecast dicts (status/eta_days/mount)
        vram      : {'status', 'eta_min'}
        gpu       : {'temp': float, 'throttled': bool, 'gpus': [{'temp'}, ...]}
    Returns {score, band, tier, factors:[{key,label,delta,detail,meta}], caps,
    generated_at}. Guarantees score == clamp(100 + Σ factor.delta, 0, 100); each
    factor.delta is a negative integer (deductions only). Never raises."""
    signals = signals or {}
    factors = []

    def add(key, delta, detail, meta):
        delta = int(delta)
        if delta < 0:
            factors.append({"key": key, "label": _HS_LABELS.get(key, key),
                            "delta": delta, "detail": detail, "meta": meta})

    # 1) UPTIME — any enabled check currently DOWN (the hardest failure signal).
    try:
        checks = signals.get("uptime") or []
        enabled = [c for c in checks if isinstance(c, dict) and c.get("enabled")]
        down = [c for c in enabled if c.get("state") == "down"]
        if down:
            d = min(_HS_CAPS["uptime"], 20 * len(down))
            add("uptime", -d, f"{len(down)} of {len(enabled)} checks down",
                {"down": len(down), "total": len(enabled)})
    except Exception:
        pass

    # 2) INCIDENTS — open correlated-anomaly incidents, extra weight if critical.
    try:
        inc = signals.get("incidents") or {}
        op = int(inc.get("open") or 0)
        crit = int(inc.get("critical") or 0)
        if op > 0:
            d = min(_HS_CAPS["incidents"], 10 * op + 8 * crit)
            detail = (f"{op} open incident{'s' if op != 1 else ''}"
                      + (f", {crit} critical" if crit else ""))
            add("incidents", -d, detail, {"open": op, "critical": crit})
    except Exception:
        pass

    # 3) SLO — checks whose error budget is exhausted or burning fast. Only count
    #    checks with data_sufficient so a sparse window never invents a penalty.
    try:
        checks = signals.get("uptime") or []
        ob = burn = 0
        for c in checks:
            slo = (c or {}).get("slo") if isinstance(c, dict) else None
            if not (isinstance(slo, dict) and slo.get("data_sufficient")):
                continue
            if slo.get("over_budget"):
                ob += 1
            if slo.get("burning"):
                burn += 1
        if ob or burn:
            d = min(_HS_CAPS["slo"], 8 * ob + 6 * burn)
            bits = []
            if ob:
                bits.append(f"{ob} over budget")
            if burn:
                bits.append(f"{burn} burning fast")
            add("slo", -d, ", ".join(bits), {"over_budget": ob, "burning": burn})
    except Exception:
        pass

    # 4) DISK CAPACITY — soonest imminent disk-fill ETA (proximity → deduction).
    try:
        def _disk_pen(dsk):
            st = dsk.get("status")
            if st == "full":
                return _HS_CAPS["disk"]
            if st != "filling":
                return 0
            eta = _hs_num(dsk.get("eta_days"))
            if eta is None:
                return 0
            if eta <= 1:  return 15
            if eta <= 3:  return 12
            if eta <= 7:  return 7
            if eta <= 30: return 3
            return 0
        worst, worst_pen = None, 0
        for dsk in (signals.get("disk") or []):
            if not isinstance(dsk, dict):
                continue
            p = _disk_pen(dsk)
            if p > worst_pen:
                worst_pen, worst = p, dsk
        if worst_pen > 0:
            eta = _hs_num(worst.get("eta_days"))
            if worst.get("status") == "full":
                detail = f"{worst.get('mount')} full"
            elif eta is not None:
                when = "<1d" if eta < 1 else f"~{round(eta)}d"
                detail = f"{worst.get('mount')} fills in {when}"
            else:
                detail = worst.get("mount")
            add("disk", -min(_HS_CAPS["disk"], worst_pen), detail,
                {"mount": worst.get("mount"), "status": worst.get("status"),
                 "eta_days": eta})
    except Exception:
        pass

    # 5) VRAM CAPACITY — GPU-VRAM-exhaustion ETA proximity.
    try:
        vram = signals.get("vram") or {}
        st = vram.get("status")
        eta = _hs_num(vram.get("eta_min"))
        p = 0
        if st == "full":
            p = _HS_CAPS["vram"]
        elif st == "filling" and eta is not None:
            if eta <= 60:      p = 12
            elif eta <= 360:   p = 9
            elif eta <= 1440:  p = 5
            elif eta <= 10080: p = 2
        if p > 0:
            if st == "full":
                detail = "GPU VRAM full"
            elif eta is not None and eta < 120:
                detail = f"VRAM exhausts in ~{round(eta)}m"
            else:
                detail = f"VRAM exhausts in ~{round((eta or 0) / 60)}h"
            add("vram", -min(_HS_CAPS["vram"], p), detail,
                {"status": st, "eta_min": eta})
    except Exception:
        pass

    # 6) ANOMALIES — currently-firing z-score anomalies (GPU util/vram/power/temp,
    #    total power, disk-I/O). Only active items; a quiet detector never deducts.
    try:
        items = (signals.get("anomalies") or {}).get("items") or []
        items = [i for i in items if isinstance(i, dict)]
        if items:
            d = min(_HS_CAPS["anomalies"], 5 * len(items))
            keys = [i.get("key") for i in items[:4] if i.get("key")]
            detail = f"{len(items)} anomal{'ies' if len(items) != 1 else 'y'} firing"
            add("anomalies", -d, detail, {"count": len(items), "keys": keys})
    except Exception:
        pass

    # 7) THERMAL — GPU temperature near the throttle ceiling, or actively THERMAL-
    #    throttling. A routine power/util cap (e.g. "Power cap" on a cool, idle card
    #    sitting at its configured power limit) is normal operation, NOT a health
    #    problem — so it must never deduct here. When throttle *reasons* are known we
    #    count only the explicitly-thermal ones; if only an opaque bool is available
    #    we fall back to it (a high temp is always caught by the bands regardless).
    try:
        gpu = signals.get("gpu") or {}
        temps = [_hs_num(gpu.get("temp"))]
        for gg in (gpu.get("gpus") or []):
            if isinstance(gg, dict):
                temps.append(_hs_num(gg.get("temp")))
        temps = [t for t in temps if t is not None and t > 0]
        tmax = max(temps) if temps else None
        reasons = gpu.get("throttle")
        if isinstance(reasons, (list, tuple)):
            thermal_throttle = any(r in _HS_THERMAL_THROTTLE for r in reasons)
        else:
            thermal_throttle = bool(gpu.get("throttled"))
        p = 0
        if tmax is not None:
            if tmax >= 90:   p = 15
            elif tmax >= 84: p = 9
            elif tmax >= 79: p = 4
        if thermal_throttle:
            p = max(p, 9)
        if p > 0:
            if tmax is not None and thermal_throttle:
                detail = f"GPU at {round(tmax)}°C, thermal throttling"
            elif tmax is not None:
                detail = f"GPU at {round(tmax)}°C"
            else:
                detail = "GPU thermal throttling"
            add("thermal", -min(_HS_CAPS["thermal"], p), detail,
                {"temp": (round(tmax, 1) if tmax is not None else None),
                 "throttled": thermal_throttle})
    except Exception:
        pass

    total = sum(f["delta"] for f in factors)
    score = max(0, min(100, 100 + total))
    factors.sort(key=lambda f: f["delta"])   # worst (most negative) first
    band, tier = _hs_band(score)
    return {"score": score, "band": band, "tier": tier, "factors": factors,
            "caps": dict(_HS_CAPS),
            "generated_at": int(now if now is not None else time.time())}

def _hs_incident_counts():
    """Open-incident tally for the health score: total open + how many are
    'critical'. One tiny bounded query; degrades to zeros, never raises."""
    try:
        with LOCK:
            rows = DB.execute(
                "SELECT severity FROM incidents WHERE state='open'").fetchall()
        return {"open": len(rows),
                "critical": sum(1 for r in rows if (r[0] or "") == "critical")}
    except Exception:
        return {"open": 0, "critical": 0}

def _hs_gpu_signal():
    """Live GPU thermal signal for the health score, from the cached snapshot the
    UI already has. Empty (→ no thermal penalty) when no GPU is present, so a
    GPU-less host is never dinged for 'missing' thermals."""
    if not LATEST.get("gpu_avail"):
        return {}
    extra = LATEST.get("gpu_extra") or {}
    return {"temp": LATEST.get("temp"),
            "throttled": extra.get("throttled"),
            "throttle": extra.get("throttle"),
            "gpus": LATEST.get("gpus")}

def _gather_health_signals(now):
    """Collect the (already-computed) signals the Lab Health Score reads. Reuses
    the same pure detectors as /api/forecast (disk/VRAM/anomalies) plus the uptime
    checks (with their SLO sub-dict), open-incident counts and the live GPU
    thermal snapshot. Read-only; NO LLM; degrades to empties, never raises."""
    try:
        with LOCK:
            cur = DB.cursor()
            disks = _disk_forecasts(cur, now)
            anomalies = _zscore_anomalies(cur, now)
            vram = _vram_forecast(cur, now)
    except Exception as e:
        print("health signals error:", e, flush=True)
        disks, anomalies, vram = [], {"items": []}, {}
    try:
        checks = uptime_overview().get("checks", []) or []
    except Exception:
        checks = []
    return {"uptime": checks, "incidents": _hs_incident_counts(),
            "anomalies": anomalies, "disk": disks, "vram": vram,
            "gpu": _hs_gpu_signal()}

@app.route("/api/health_score")
def api_health_score():
    """Read-only, deterministic Lab Health Score (0-100) + explainable breakdown.
    Synthesizes the signals the app already produces — uptime/SLO, open incidents,
    active z-score anomalies, disk/VRAM capacity ETAs and GPU thermals — into ONE
    glanceable headline with a worst-first factor list that reconciles exactly to
    the number (score == clamp(100 + Σ delta)). PURE MATH: this endpoint makes ZERO
    LLM calls and never mutates. Authed dashboard surface only — NOT exposed on the
    public /status pages. Always 200; graceful-degrade, never 500."""
    now = int(time.time())
    return jsonify(compute_health_score(_gather_health_signals(now), now))

# ── 🧭 "What to fix first" — an AI-prioritized remediation plan layered on the
# deterministic Lab Health Score. The PRIORITY (worst-first, by |delta|) and the
# DEEP-LINK targets are 100% deterministic — the local LLM may only reword the
# human-readable what/why/next-step prose, never reorder, invent a factor, or
# change a link. Read-only: it SUGGESTS + deep-links to EXISTING surfaces; it
# never persists and never mutates the host. NO LLM on any poll path — enrichment
# is behind the explicit `?llm=1` action only. ────────────────────────────────
#
# Per health-factor key → the concrete recommended action + the deep-link target.
# `tab` is an EXISTING dashboard tab id (see the <section data-tab="…"> set); the
# UI reuses the same showTab()/#hash mechanism the hero chips already use. `anchor`
# is an existing element id to scroll to (optional). We invent NO new routes.
_HS_FIX = {
    "uptime": {
        "tab": "uptime", "anchor": None, "severity": "crit",
        "title": "Bring the down check(s) back up",
        "action": ("Open Uptime, find the check(s) showing DOWN, and restore the "
                   "target service (or pause the check if the endpoint is retired)."),
        "why": ("A monitored check being down is the hardest failure signal — it "
                "usually means users can't reach something the lab is meant to serve."),
    },
    "incidents": {
        "tab": "gpu", "anchor": "inc-card", "severity": "crit",
        "title": "Work the open incident(s)",
        "action": ("Open the Incidents panel, read the correlated anomalies in each "
                   "open incident, and address the underlying cause; it clears itself "
                   "once every member signal settles back to normal."),
        "why": ("Open incidents group anomalies that fired together — they point at a "
                "real, ongoing problem rather than one noisy sample."),
    },
    "slo": {
        "tab": "uptime", "anchor": None, "severity": "warn",
        "title": "Protect the burning error budget",
        "action": ("Open Uptime and review the check(s) over budget or burning fast; "
                   "reduce the failing requests, or widen the objective if the target "
                   "was set too tight."),
        "why": ("An exhausted or fast-burning error budget means reliability is "
                "trending the wrong way and will breach the objective if it continues."),
    },
    "disk": {
        "tab": "disks", "anchor": None, "severity": "warn",
        "title": "Reclaim space before the disk fills",
        "action": ("Open Disks, scan the filling mount to find the biggest folders, "
                   "and clear or relocate what's growing before the ETA lands."),
        "why": ("A full disk stops writes — logs, backups and databases fail once the "
                "mount is out of space."),
    },
    "vram": {
        "tab": "gpu", "anchor": None, "severity": "warn",
        "title": "Free GPU VRAM before it's exhausted",
        "action": ("Open GPU, see which loaded models hold VRAM, and unload the ones "
                   "you don't need so new work still fits."),
        "why": ("When VRAM is exhausted the GPU can't load the next model or batch — "
                "inference stalls or falls back to slow CPU."),
    },
    "anomalies": {
        "tab": "gpu", "anchor": "anom-card", "severity": "warn",
        "title": "Check what's firing anomalies",
        "action": ("Open GPU and read the Anomalies panel — each firing signal names "
                   "the metric (util / VRAM / power / temp) that's off its baseline."),
        "why": ("Active z-score anomalies mean a live metric is well outside its "
                "recent normal — often the first sign of a developing problem."),
    },
    "thermal": {
        "tab": "gpu", "anchor": None, "severity": "warn",
        "title": "Cool the GPU down",
        "action": ("Open GPU, check the temperature and load, and improve airflow or "
                   "cap the power/clocks; a card near its throttle ceiling loses "
                   "performance."),
        "why": ("A GPU running hot enough to (thermal-)throttle runs slower and wears "
                "faster — sustained heat shortens the card's life."),
    },
}
# Shown when the score is excellent / nothing is firing — a calm all-clear, NOT a
# fabricated problem. Deep-links to the Overview so the user lands on the score.
_HS_FIX_ALLCLEAR = {
    "key": "all_clear", "priority": 1, "severity": "ok",
    "title": "All clear — nothing to fix",
    "why": "No signals are currently reducing your lab health.",
    "action": "Keep an eye on the Lab Health Score; it'll flag the first thing that slips.",
    "deep_link": {"tab": "overview", "anchor": "hero-score"},
}

def build_fixplan(health):
    """DETERMINISTIC remediation plan from a computed health-score dict. Takes the
    firing factors worst-first (the score's own `factors` are already sorted by
    delta) and maps each factor `key` → a concrete action + an EXISTING deep-link
    target from `_HS_FIX`. This ordered list is the SOURCE OF TRUTH for priority +
    links; the LLM (if used) may only reword prose against these exact items.

    Returns a list of {key, priority, title, why, action, deep_link, severity,
    detail}. Empty firing set → a single calm all-clear item (never fabricated
    problems). Pure: NO I/O, NO LLM, never raises."""
    factors = (health or {}).get("factors") or []
    factors = [f for f in factors if isinstance(f, dict) and f.get("key") in _HS_FIX]
    if not factors:
        return [dict(_HS_FIX_ALLCLEAR)]
    plan = []
    for i, f in enumerate(factors):
        spec = _HS_FIX[f["key"]]
        plan.append({
            "key": f["key"],
            "priority": i + 1,
            "severity": spec["severity"],
            "title": spec["title"],
            "why": spec["why"],
            "action": spec["action"],
            # `detail` carries the live number the score already computed, so the
            # item is grounded (e.g. "/backup fills in ~3d") without any new I/O.
            "detail": f.get("detail") or "",
            "deep_link": {"tab": spec["tab"], "anchor": spec["anchor"]},
        })
    return plan


def _fixplan_llm_prompt(plan, score, band):
    """Small, secret-free prompt: hand the LLM the ALREADY-COMPUTED deterministic
    plan items (title + why + action + the live detail) and ask it to reword the
    prose per item, plainer/friendlier, KEEPING the numbers and the recommended
    step. We send only the fields the panel already shows — no targets, no host
    internals. Bounded to the plan set. The item numbers are the contract the
    validator maps back against."""
    lines = []
    for i, it in enumerate(plan):
        det = (" (" + it["detail"] + ")") if it.get("detail") else ""
        lines.append("%d. %s%s — why: %s — next: %s"
                     % (i + 1, it["title"], det, it["why"], it["action"]))
    return (
        "You are the Lab Copilot for a self-hosted homelab monitoring dashboard. "
        "The Lab Health Score is %d/100 (%s). Below is the monitor's own "
        "PRIORITIZED fix-it plan, worst problem first, each already derived from "
        "live data. For EACH numbered item rewrite it as plain, friendly English "
        "with three short parts — a one-line title, a 'why it matters' sentence, "
        "and a concrete 'next step' — keeping every number and the recommended "
        "action. Do NOT add, drop, reorder or renumber items; use ONLY the given "
        "facts; invent nothing. Reply STRICTLY as JSON: "
        "{\"items\":[{\"n\":1,\"title\":\"…\",\"why\":\"…\",\"action\":\"…\"}, …]}.\n\n"
        "PLAN:\n" % (int(score), band) + "\n".join(lines))


# JSON-schema so ollama returns structured output we can parse deterministically.
FIXPLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "title": {"type": "string"},
                    "why": {"type": "string"},
                    "action": {"type": "string"},
                },
                "required": ["n"],
            },
        },
    },
    "required": ["items"],
}


def _fixplan_apply_llm(plan, text):
    """Validate + clamp the LLM's JSON rewrite back against the DETERMINISTIC plan
    and merge the reworded prose in place (title_llm/why_llm/action_llm). HOSTILE-
    INPUT SAFE: an item whose `n` doesn't map to a real plan position is DROPPED;
    the LLM can NOT add a factor, reorder priority, or touch the deep-link/severity/
    key — those come only from `build_fixplan`. Only string prose fields are copied,
    length-clamped. Returns the count of items it actually enriched. Never raises;
    on any parse failure the deterministic prose stands untouched."""
    if not text:
        return 0
    try:
        obj = json.loads(text)
    except Exception:
        return 0
    items = obj.get("items") if isinstance(obj, dict) else None
    if not isinstance(items, list):
        return 0
    n = 0
    for raw in items:
        if not isinstance(raw, dict):
            continue
        try:
            pos = int(raw.get("n"))
        except (TypeError, ValueError):
            continue
        # `n` is 1-based and MUST map to an existing deterministic item; anything
        # out of range (a hallucinated / injected extra factor) is dropped.
        if pos < 1 or pos > len(plan):
            continue
        it = plan[pos - 1]
        touched = False
        for src, dst in (("title", "title_llm"), ("why", "why_llm"),
                         ("action", "action_llm")):
            val = raw.get(src)
            if not isinstance(val, str):
                continue
            val = val.strip()
            # Reject prose with no real words — a tiny model sometimes emits a lone
            # glyph / replacement char; that must NOT clobber the good deterministic
            # text (the UI would otherwise show a meaningless '�'). Require at least
            # a few alphanumerics so only genuine rewrites land.
            if sum(c.isalnum() for c in val) < 3:
                continue
            it[dst] = val[:400]
            touched = True
        if touched:
            n += 1
    return n


@app.route("/api/health/fixplan", methods=["GET", "POST"])
def api_health_fixplan():
    """🧭 "What to fix first" — an AI-prioritized remediation plan built on the
    deterministic Lab Health Score.

    Builds the plan DETERMINISTICALLY: the firing health factors, worst-first,
    each mapped to a concrete recommended action + a deep-link into an EXISTING
    dashboard tab. That ordered list is the source of truth for BOTH priority and
    links. The local LLM optionally ENRICHES the prose (title/why/action) — and
    ONLY on an explicit `?llm=1` (or POST {"llm":true}) call, NEVER on any poll
    path — schema-locked and validated back against the deterministic items, so it
    can't invent a factor, reorder priority, or change a link. If it's off /
    unreachable / returns garbage, the deterministic plan stands and `llm_status`
    says why. When healthy (no firing factors) it returns a calm all-clear plan,
    not a fabricated problem.

    Read-only: it SUGGESTS + deep-links only — it NEVER persists and NEVER mutates
    the host. Authed dashboard surface only (not on public /status). Always 200,
    graceful-degrade, never 500."""
    now = int(time.time())
    body = request.get_json(silent=True) or {} if request.method == "POST" else {}
    want_llm = (request.args.get("llm") in ("1", "true", "yes")
                or bool(body.get("llm")))
    # Deterministic first — this is the source of truth. Computed under the same
    # pure detectors the score uses; LOCK is released inside _gather_health_signals
    # BEFORE we ever touch the network below.
    health = compute_health_score(_gather_health_signals(now), now)
    plan = build_fixplan(health)
    all_clear = not any(p.get("key") in _HS_FIX for p in plan)
    out = {"ok": True, "now": now,
           "score": health.get("score"), "band": health.get("band"),
           "tier": health.get("tier"), "all_clear": all_clear,
           "plan": plan, "model": COPILOT_MODEL, "enabled": COPILOT_ENABLED,
           "llm_used": False, "llm_status": "skipped"}
    # LLM enrichment ONLY on the explicit action, and only when there's something to
    # fix — never on the default GET poll, never on the all-clear plan.
    if want_llm and not all_clear:
        if not COPILOT_ENABLED:
            out["llm_status"] = "disabled"
        else:
            text, err = _ollama_generate(
                _fixplan_llm_prompt(plan, health.get("score"), health.get("band")),
                timeout=min(COPILOT_TIMEOUT, 20), fmt=FIXPLAN_SCHEMA)
            if text is not None and _fixplan_apply_llm(plan, text) > 0:
                out["llm_used"] = True
                out["llm_status"] = "ok"
            else:
                out["llm_status"] = err or "bad_response"
    return jsonify(out)

@app.route("/api/forecast")
def api_forecast():
    """Read-only forecasts computed from the history already in SQLite, using pure
    Python statistics (no numpy/pandas/new deps):
      • disk[]      — per-mount fill ETA from a linear fit of used-GB over time
      • cost_month  — month-to-date energy cost extrapolated to a full-month estimate
      • anomalies   — z-score flags on the latest GPU util/VRAM/power/temp + total
                      power draw vs a trailing baseline
      • vram        — GPU VRAM-exhaustion ETA (linear fit of mem_used over time,
                      R²/slope-gated) + live headroom (total − loaded-model VRAM)
    Degrades gracefully: when there isn't enough history yet, items read 'collecting'
    or 'stable' rather than guessing — never a 500."""
    now = int(time.time())
    ctx = _cost_ctx()
    ooms = []
    try:
        with LOCK:
            cur = DB.cursor()
            disks = _disk_forecasts(cur, now)
            cost_month = _cost_projection(cur, ctx, now)
            anomalies = _zscore_anomalies(cur, now)
            vram = _vram_forecast(cur, now)
            # Recurring-OOM signal, read in the SAME LOCK pass (mirrors _reco_signals)
            # so the cheap reco tally below reuses these forecasts without recompute.
            try:
                cutoff = int(now - RECO_OOM_WINDOW_DAYS * 86400)
                rows = cur.execute(
                    "SELECT service, COUNT(*) n, MAX(ts) last FROM events "
                    "WHERE kind='oom' AND ts>=? GROUP BY service ORDER BY n DESC, last DESC",
                    (cutoff,)).fetchall()
                ooms = [{"service": r[0], "count": r[1], "last_ts": r[2]} for r in rows]
            except Exception:
                ooms = []
    except Exception as e:
        print("forecast error:", e, flush=True)
        return jsonify({"now": now, "disk": [], "cost_month": {"enabled": False},
                        "anomalies": {"status": "collecting", "checked": 0, "items": []},
                        "vram": {"status": "collecting"},
                        "error": "forecast_unavailable"})
    # incidents_summary()/uptime_overview() take LOCK themselves — call OUTSIDE the
    # block above so the non-reentrant lock is never nested.
    incidents = incidents_summary()
    try:
        uptime_checks = uptime_overview().get("checks", []) or []
    except Exception as e:
        print("forecast uptime error:", e, flush=True)
        uptime_checks = []
    down = sum(1 for c in uptime_checks
               if c.get("enabled") and c.get("state") == "down")
    # CHEAP, LLM-FREE "needs attention" tally for the cockpit hero rollup + nav
    # badges: reuse the forecasts just computed (no second heavy pass) and run ONLY
    # the deterministic detectors. /api/forecast already rides the 15s poll, so the
    # badge never has to touch the LLM-backed /api/recommendations on a timer.
    reco = _reco_counts({"disk": disks, "vram": vram, "cost_month": cost_month,
                         "anomalies": anomalies, "incidents": incidents,
                         "uptime": uptime_checks, "ooms": ooms})
    # Lab Health Score rides the SAME already-polled forecast pass (no extra timer,
    # no LLM): reuse the disk/VRAM/anomaly/uptime signals just computed + the live
    # GPU thermal snapshot + an open-incident tally. Deterministic math only.
    health = compute_health_score({
        "uptime": uptime_checks, "incidents": _hs_incident_counts(),
        "anomalies": anomalies, "disk": disks, "vram": vram,
        "gpu": _hs_gpu_signal()}, now)
    return jsonify({"now": now, "disk": disks, "cost_month": cost_month,
                    "anomalies": anomalies, "vram": vram,
                    "incidents": incidents,
                    "uptime": {"down": down, "total": len(uptime_checks)},
                    "reco": reco, "health": health,
                    "attention": {"reco": reco, "incidents_open": incidents.get("open", 0),
                                  "uptime_down": down}})

@app.route("/api/diskio/history")
def api_diskio_history():
    """Recent per-device disk-I/O series (read/write MB/s + utilisation%) drawn
    from the disk_io_samples 7-day ring, for the dashboard's per-device
    sparklines. Read-only; degrades to an empty device list, never a 500."""
    now = int(time.time())
    try:
        window = int(request.args.get("window", 3600))
    except (TypeError, ValueError):
        window = 3600
    window = max(300, min(window, _DISK_IO_RETENTION))
    since = now - window
    devices = {}
    try:
        with LOCK:
            rows = DB.cursor().execute(
                "SELECT device, ts, read_mb_s, write_mb_s, util_pct FROM disk_io_samples "
                "WHERE ts>=? ORDER BY device, ts", (since,)).fetchall()
        for dev, ts, r, w, u in rows:
            d = devices.setdefault(dev, {"device": dev, "ts": [], "read_mb_s": [],
                                         "write_mb_s": [], "util_pct": []})
            d["ts"].append(ts)
            d["read_mb_s"].append(r)
            d["write_mb_s"].append(w)
            d["util_pct"].append(u)
    except Exception as e:
        print("diskio history error:", e, flush=True)
        return jsonify({"now": now, "window": window, "devices": []})
    return jsonify({"now": now, "window": window,
                    "devices": sorted(devices.values(), key=lambda d: d["device"])})


@app.route("/api/incidents")
def api_incidents():
    """Read-only correlated-anomaly incidents — open first, then most-recent — each
    with its member series (direction, peak σ, value-vs-baseline) + derived severity
    + open/cleared state + timestamps. Always 200; graceful-degrade, never 500. No
    topology/secret leakage: members are only the monitored telemetry series keys."""
    try: limit = min(_INCIDENT_RETENTION, max(1, int(request.args.get("limit", 50))))
    except (TypeError, ValueError): limit = 50
    return jsonify({"now": int(time.time()),
                    "summary": incidents_summary(),
                    "incidents": list_incidents(limit)})

@app.route("/api/incidents/<iid>")
def api_incident_one(iid):
    """Full detail for ONE correlated-anomaly incident: all fields + the complete
    member list (series, direction, peak σ, value-vs-baseline, first/last seen,
    active) + a compact derived timeline (opened → member joins → cleared). Returns
    200 with the incident, or a clean 404 JSON for an unknown/garbage id — never a
    500/stacktrace. No topology/secret leakage: members are only telemetry keys."""
    inc = get_incident(iid)
    if inc is None:
        return jsonify({"error": "unknown incident"}), 404
    # Keep the drawer poll lean + LLM-prose-free: the raw postmortem JSON blob is
    # never shipped on this read path (only a `has_postmortem` boolean). The prose
    # is fetched lazily via GET /api/incidents/<id>/postmortem.
    inc = {k: v for k, v in inc.items() if k != "postmortem_json"}
    return jsonify({"now": int(time.time()), "incident": inc})

@app.route("/api/incidents/<iid>/explain", methods=["POST"])
def api_incident_explain(iid):
    """Explicit, on-demand incident explanation — the Explain / Regenerate action
    and the drawer's first-open one-shot. This is the ONLY incident endpoint that
    may reach the LLM; the plain reads (/api/incidents and /api/incidents/<id>) are
    always cache-only (zero LLM). Body: {"regenerate": bool}. On a cache hit with
    regenerate falsey, returns the persisted explanation WITHOUT any LLM call.
    Always 200 (except 404 for an unknown id); graceful llm_status when the LLM is
    off. Persists the generated text so it survives reopen and shows in the list."""
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}
    force = bool(isinstance(payload, dict) and payload.get("regenerate"))
    res = generate_incident_explanation(iid, force=force)
    if res.get("llm_status") == "unknown":
        return jsonify({"error": "unknown incident"}), 404
    res.setdefault("id", iid)
    res["now"] = int(time.time())
    res["model"] = COPILOT_MODEL
    res["enabled"] = COPILOT_ENABLED
    return jsonify(res)

@app.route("/api/incidents/<iid>/postmortem", methods=["GET", "POST"])
def api_incident_postmortem(iid):
    """The AI incident postmortem for a RESOLVED incident — the capstone of the
    incidents+AI thread. This + the explain endpoint are the ONLY incident
    endpoints that may reach the LLM; the plain reads (/api/incidents and
    /api/incidents/<id>) stay cache-only.

    • GET (default): returns the PERSISTED postmortem for a resolved incident, plus
      the deterministic facts (timeline/duration/members) — ZERO LLM call. A
      resolved incident with no postmortem yet returns the deterministic skeleton
      with llm_status "ungenerated"; an OPEN incident returns resolved:false.
    • GET ?generate=1 / POST: EXPLICIT generation — assembles grounding from the
      real incident data and asks the local LLM to compose the prose (JSON mode).
      At-most-once via an atomic claim; ?regenerate=1 / {"regenerate":true} forces
      a fresh generation. LLM off/garbage → deterministic skeleton, honest
      llm_status, never a 500.

    Always 200 except a clean 404 for an unknown id. Never mutates the host /
    incident lifecycle / any alert; the ONLY write is the cached postmortem row.
    PRIVATE (authed) — never on the public status/RSS surface."""
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}
    def _flag(name):
        v = request.args.get(name)
        if v is not None:
            return str(v).strip().lower() in ("1", "true", "yes", "on")
        return bool(isinstance(payload, dict) and payload.get(name))
    force = _flag("regenerate")
    generate = force or _flag("generate") or request.method == "POST"
    res = get_incident_postmortem(iid, generate=generate, force=force)
    if res.get("llm_status") == "unknown":
        return jsonify({"error": "unknown incident"}), 404
    res["now"] = int(time.time())
    res["enabled"] = COPILOT_ENABLED
    return jsonify(res)

# ── Lab Copilot (E1): local-LLM insight layer ────────────────────────────────
# Read-only. Assembles a compact, already-computed snapshot of the lab (live GPU
# + RAM, biggest model on the GPU, disk-fill ETA, projected month cost, the top
# anomaly) and asks a local ollama model to phrase it in plain English. Every
# externally-visible failure mode is a graceful state, never a 500.

def _copilot_context(now=None):
    """Assemble a compact dict of the metrics the dashboard already computes,
    suitable both for prompting the LLM and for a no-LLM fallback summary. Pure
    reads of LATEST + the forecast helpers; never raises (returns what it can)."""
    now = now or int(time.time())
    ctx = {"now": now, "gpu": {}, "ram": {}, "models": [], "disk": [],
           "cost_month": {}, "anomalies": [], "services": []}
    try:
        L = LATEST
        ctx["host"] = (L.get("host") or {}).get("hostname") or socket.gethostname()
        ctx["gpu"] = {
            "available": L.get("gpu_avail"),
            "util_pct": round(L.get("util") or 0),
            "vram_used_mb": round(L.get("mem_used") or 0),
            "vram_total_mb": round(L.get("mem_total") or 0),
            "power_w": round(L.get("power") or 0),
            "temp_c": round(L.get("temp") or 0),
        }
        # biggest models currently resident on the GPU (drives the "driven by Z")
        models = []
        for m in (L.get("models") or []):
            try:
                models.append({"service": m.get("service"), "model": m.get("model"),
                               "vram_mb": round(m.get("vram") or 0)})
            except Exception:
                continue
        models.sort(key=lambda x: -(x["vram_mb"] or 0))
        ctx["models"] = models[:3]
    except Exception as e:
        print("copilot ctx (live) error:", e, flush=True)
    try:
        # _cost_ctx() -> get_settings() acquires LOCK itself, so it MUST be called
        # OUTSIDE our `with LOCK:` block (the lock is non-reentrant) — exactly how
        # api_forecast() does it. Calling it inside would deadlock permanently.
        cctx = _cost_ctx()
        with LOCK:
            cur = DB.cursor()
            disks = _disk_forecasts(cur, now)
            ctx["cost_month"] = _cost_projection(cur, cctx, now)
            anoms = _zscore_anomalies(cur, now)
        # only mounts with a real fill ETA are interesting for the digest
        ctx["disk"] = sorted(
            [{"mount": d["mount"], "pct": d.get("pct"), "eta_days": d.get("eta_days"),
              "free_gb": d.get("free_gb"), "status": d.get("status")}
             for d in disks if d.get("status") == "filling" and d.get("eta_days") is not None],
            key=lambda d: (d["eta_days"] if d["eta_days"] is not None else 1e9))[:3]
        ctx["anomalies"] = (anoms.get("items") or [])[:3]
        ctx["anomaly_status"] = anoms.get("status")
    except Exception as e:
        print("copilot ctx (history) error:", e, flush=True)
    return ctx


def _copilot_facts(c):
    """Render the assembled context as terse, deterministic English bullet facts.
    This is both the LLM's grounding material AND the no-LLM fallback summary, so
    the feature is useful even when ollama is down."""
    lines = []
    g = c.get("gpu") or {}
    if g.get("available") is False:
        lines.append("No GPU detected on this host.")
    else:
        vt = g.get("vram_total_mb") or 0
        vpct = round(100 * (g.get("vram_used_mb") or 0) / vt) if vt else None
        lines.append(
            "GPU: {u}% utilisation, {p} W, {t}°C, VRAM {vu} MB of {vt} MB{vp}.".format(
                u=g.get("util_pct", 0), p=g.get("power_w", 0), t=g.get("temp_c", 0),
                vu=g.get("vram_used_mb", 0), vt=vt,
                vp=(" (%d%%)" % vpct) if vpct is not None else ""))
    models = c.get("models") or []
    if models:
        top = models[0]
        lines.append("Biggest model on the GPU: {m} ({mb} MB, served by {s}).".format(
            m=top.get("model") or "?", mb=top.get("vram_mb") or 0,
            s=top.get("service") or "?"))
    for d in (c.get("disk") or []):
        lines.append("Disk {mp} is {pct}% full and, at the current rate, fills in ~{n} days.".format(
            mp=d.get("mount"), pct=d.get("pct"), n=d.get("eta_days")))
    cm = c.get("cost_month") or {}
    if cm.get("enabled"):
        cur = cm.get("currency") or "$"
        lines.append("Energy cost month-to-date {cur}{mtd}; projected full month {cur}{proj}.".format(
            cur=cur, mtd=cm.get("month_to_date"), proj=cm.get("projected_month")))
    anoms = c.get("anomalies") or []
    if anoms:
        a = anoms[0]
        lines.append("Anomaly: {k} {d} — {v}{u} now vs ~{b}{u} baseline ({z}σ).".format(
            k=a.get("key"), d=a.get("direction"), v=a.get("value"), u=a.get("unit"),
            b=a.get("baseline"), z=a.get("z")))
    elif c.get("anomaly_status") == "quiet":
        lines.append("No anomalies: all monitored series are within their normal range.")
    if not lines:
        lines.append("No metrics available yet — the monitor is still collecting data.")
    return lines


def _copilot_digest_prompt(facts):
    return (
        "You are the Lab Copilot for a self-hosted homelab monitoring dashboard. "
        "Write a short, friendly status digest (2-4 sentences, plain English, no "
        "markdown, no bullet points) summarising the lab RIGHT NOW for its owner. "
        "Use ONLY the facts below; do not invent numbers. Be concrete and calm.\n\n"
        "FACTS:\n- " + "\n- ".join(facts) + "\n\nDIGEST:")


def _copilot_ask_prompt(facts, question):
    return (
        "You are the Lab Copilot for a self-hosted homelab monitoring dashboard. "
        "Answer the user's question using ONLY the facts below — these are live "
        "readings from their lab. If the facts don't contain the answer, say so "
        "plainly. Keep it to 1-3 sentences, plain English, no markdown.\n\n"
        "FACTS:\n- " + "\n- ".join(facts) + "\n\n"
        "QUESTION: " + question.strip() + "\nANSWER:")


# ── LLM throughput side-channel ───────────────────────────────────────────────
# Honest tok/s + TTFT: we DON'T fabricate. Real numbers come only from actual
# ollama generation responses (digest / ask / explain), which carry eval_count,
# eval_duration (ns), prompt_eval_*, and load_duration. We snapshot the latest
# measurement into a tiny module-level dict — a pure side-channel that never
# alters the copilot's returned text. Guarded by its own lock (NOT the global
# LOCK) so capture is independent of the metrics-DB discipline. /api/ps is polled
# separately, outside any held lock.
_LLM_LOCK = threading.Lock()
_LLM_LAST = None  # {model, tps, ttft_ms, prompt_tps, eval_count, prompt_eval_count, ts}
# Throughput history is sparse by nature (one row per REAL copilot generation),
# so a small ring is plenty for a sparkline + Prometheus latest. Keep the most
# recent rows AND drop anything older than the global RETENTION window.
_LLM_SAMPLE_CAP = 500


def _llm_metrics_from_response(data, model=None):
    """Derive a throughput measurement from a non-streaming ollama generate
    response. Returns a dict or None when the response lacks the timing fields
    (e.g. an error/empty response). Pure + side-effect-free → unit-testable.

    ollama durations are nanoseconds. tok/s = eval_count / eval_duration(s).
    TTFT (time-to-first-token) ≈ load_duration + prompt_eval_duration: the model
    load + prompt ingestion before the first generated token streams."""
    if not isinstance(data, dict):
        return None
    eval_count = data.get("eval_count")
    eval_dur = data.get("eval_duration")  # ns
    if not eval_count or not eval_dur or eval_dur <= 0:
        return None
    tps = round(eval_count / (eval_dur / 1e9), 2)
    p_count = data.get("prompt_eval_count") or 0
    p_dur = data.get("prompt_eval_duration") or 0  # ns
    prompt_tps = round(p_count / (p_dur / 1e9), 2) if (p_count and p_dur > 0) else None
    load_dur = data.get("load_duration") or 0  # ns
    ttft_ms = round((load_dur + p_dur) / 1e6, 1) if (load_dur or p_dur) else None
    return {
        "model": model or data.get("model") or COPILOT_MODEL,
        "tps": tps,
        "ttft_ms": ttft_ms,
        "prompt_tps": prompt_tps,
        "eval_count": int(eval_count),
        "prompt_eval_count": int(p_count) if p_count else 0,
        # eval_duration (ns) is kept so the per-inference cost/energy chip can
        # turn generation TIME × live GPU watts into Wh/money server-side. It is
        # NOT persisted to llm_samples (that schema is unchanged) — purely a
        # transient field consumed by _inference_cost. None-safe downstream.
        "eval_duration_ns": int(eval_dur),
        "ts": int(time.time()),
    }


def _sample_energy_wh(m, gpu_w=None):
    """This generation's GPU energy in Wh = eval_duration_s × current GPU power W / 3600
    — the SAME formula `_inference_cost` uses, so the savings rollup can never
    contradict the per-inference cost chip. `gpu_w` defaults to the live LATEST
    snapshot's total power (the source the cost attribution reads). Returns None
    when timing or GPU power is unavailable (graceful → old rows / no-GPU stay NULL).
    Pure given gpu_w; takes no LOCK and makes no network call. Never raises."""
    try:
        eval_ns = m.get("eval_duration_ns") if isinstance(m, dict) else None
        if not eval_ns or eval_ns <= 0:
            return None
        if gpu_w is None:
            gpu_w = LATEST.get("power") or 0
        gpu_w = float(gpu_w or 0)
        if gpu_w <= 0:
            return None
        return round((eval_ns / 1e9) * gpu_w / 3600.0, 4)
    except (TypeError, ValueError):
        return None


def _persist_llm_sample(m):
    """Append one real throughput measurement to llm_samples and trim the ring.
    Writes under the global LOCK (the DB-write discipline every other table
    follows) — and is called OUTSIDE _LLM_LOCK so the two locks never nest.
    Never raises; a DB error here must never break the copilot path."""
    try:
        energy_wh = _sample_energy_wh(m)  # NULL when GPU power/timing absent
        with LOCK:
            DB.execute(
                "INSERT INTO llm_samples(ts,model,tps,ttft_ms,prompt_tps,eval_count,energy_wh) "
                "VALUES(?,?,?,?,?,?,?)",
                (m["ts"], m.get("model"), m.get("tps"), m.get("ttft_ms"),
                 m.get("prompt_tps"), m.get("eval_count"), energy_wh))
            # Retention: drop rows past the global window, then cap to the ring
            # size so a chatty copilot can't grow the table without bound.
            DB.execute("DELETE FROM llm_samples WHERE ts < ?", (m["ts"] - RETENTION,))
            DB.execute(
                "DELETE FROM llm_samples WHERE ts < "
                "(SELECT MIN(ts) FROM (SELECT ts FROM llm_samples ORDER BY ts DESC LIMIT ?))",
                (_LLM_SAMPLE_CAP,))
            DB.commit()
    except Exception:
        pass


def _capture_llm_metrics(data):
    """Side-channel: stash the latest throughput measurement if the response has
    usable timing, and persist it for the tok/s trend. Never raises; never
    touches the copilot's text path."""
    try:
        m = _llm_metrics_from_response(data, COPILOT_MODEL)
        if m is None:
            return
        global _LLM_LAST
        with _LLM_LOCK:
            _LLM_LAST = m
        # Persist OUTSIDE _LLM_LOCK (DB write takes the global LOCK; never nest).
        _persist_llm_sample(m)
    except Exception:
        pass


def _inference_cost(metrics, now=None):
    """Turn ONE local generation's timing into a tiny per-inference cost/throughput
    object for the chip under a Copilot answer — server-side, so the electricity
    tariff and live GPU watts never ship to the client.

    `metrics` is a dict from `_llm_metrics_from_response` (carries eval_count, tps,
    ttft_ms and eval_duration_ns). Returns:
        {model, tokens, tps, ttft_ms, energy_wh, cost, currency}
    or None when there is no usable timing (LLM-down / facts-only path → no chip).

    energy_wh = eval_duration_s × current_GPU_power_W / 3600   (an approximation —
    the chip is prefixed "~" in the UI). Current GPU power is read from the SAME
    live snapshot the cost attribution uses (LATEST['power']). If GPU power is
    unavailable, energy_wh/cost are omitted (chip still shows tokens + tps).

    cost = (energy_wh/1000) × tariff_per_kWh, currency from the cost context, ONLY
    when cost is enabled (a positive tariff at `now`). Otherwise cost is None and
    the chip shows no money. Reuses _cost_ctx/_price_at — the one source of truth,
    so it can never contradict the Costs tab. Pure + side-effect-free → testable.
    Reads LATEST (a plain dict snapshot); takes no LOCK and makes no network call."""
    if not isinstance(metrics, dict):
        return None
    tokens = metrics.get("eval_count")
    eval_ns = metrics.get("eval_duration_ns")
    if not tokens or not eval_ns or eval_ns <= 0:
        return None
    out = {
        "model": metrics.get("model") or COPILOT_MODEL,
        "tokens": int(tokens),
        "tps": metrics.get("tps"),
        "ttft_ms": metrics.get("ttft_ms"),
        "energy_wh": None,
        "cost": None,
        "currency": None,
    }
    # Current total GPU power draw (W) from the live snapshot — same source the
    # cost attribution reads. None/0 → energy & cost are simply omitted.
    try:
        gpu_w = float(LATEST.get("power") or 0)
    except (TypeError, ValueError):
        gpu_w = 0.0
    if gpu_w > 0:
        eval_s = eval_ns / 1e9
        energy_wh = eval_s * gpu_w / 3600.0
        out["energy_wh"] = round(energy_wh, 3)
        # Cost only when a tariff is configured (cost-enabled host). Tariff-aware
        # via _price_at — never contradicts the Costs tab.
        try:
            ctx = _cost_ctx()
            ts = int(now if now is not None else time.time())
            price = _price_at(ctx, ts)  # per-kWh
            if price and price > 0:
                cost = (energy_wh / 1000.0) * price
                # Floor so a real (tiny) cost never renders as a bare 0.0000.
                out["cost"] = round(cost, 4) if cost >= 0.00005 else 0.0001
                out["cost_floored"] = cost < 0.00005
                out["currency"] = ctx.get("currency") or "$"
        except Exception:
            out["cost"] = None
            out["currency"] = None
    return out


def _llm_history(limit=60):
    """Recent real throughput measurements, newest-last for charting. Reads
    llm_samples under LOCK. Returns a list of {ts, tps, ttft_ms, prompt_tps}.
    Never raises — an empty list on any error / when no generation has run yet."""
    try:
        n = max(1, min(int(limit), _LLM_SAMPLE_CAP))
    except (TypeError, ValueError):
        n = 60
    try:
        with LOCK:
            rows = DB.execute(
                "SELECT ts, tps, ttft_ms, prompt_tps FROM llm_samples "
                "ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
    except Exception:
        return []
    out = [{"ts": r[0], "tps": r[1], "ttft_ms": r[2], "prompt_tps": r[3]}
           for r in rows]
    out.reverse()  # oldest → newest for a left-to-right sparkline
    return out


# Per-model usage rollup window: how far back llm_samples are aggregated for the
# registry's "which models earn their disk" view. Bounded so the GROUP BY stays
# cheap and the join reflects RECENT usage, not ancient one-offs.
_LLM_USAGE_WINDOW = int(os.environ.get("COPILOT_USAGE_WINDOW_DAYS", "30")) * 86400


def _normalize_model_name(name):
    """Canonical key for joining ollama tag names against llm_samples.model.
    ollama's /api/tags emits fully-qualified tags ('gemma3:1b'); a bare 'gemma3'
    implies ':latest'. We fold a missing tag → ':latest' so the two sides land
    on the same key regardless of which form was recorded. Pure."""
    if not name:
        return ""
    n = str(name).strip()
    return n if ":" in n else n + ":latest"


def _llm_usage_by_model(now=None, window=None):
    """One cheap grouped read over llm_samples → per-model usage rollup, keyed by
    the NORMALIZED model name. Bounded to the last _LLM_USAGE_WINDOW so the scan
    stays small and reflects recent usage. Reads under the global LOCK following
    the existing DB-read pattern; MUST be called OUTSIDE any held LOCK.

    Returns {normalized_name: {last_used, runs, avg_tps, last_tps, avg_ttft_ms}}.
    Never raises — empty dict on any error so the registry join degrades cleanly."""
    now = int(now or time.time())
    win = _LLM_USAGE_WINDOW if window is None else window
    cutoff = now - win
    try:
        with LOCK:
            rows = DB.execute(
                "SELECT model, MAX(ts), COUNT(*), AVG(tps), AVG(ttft_ms) "
                "FROM llm_samples WHERE ts >= ? GROUP BY model",
                (cutoff,)).fetchall()
            # last_tps is the tps of the most-recent row per model; a correlated
            # lookup keeps it in the same bounded scan without a second hot path.
            last_rows = DB.execute(
                "SELECT s.model, s.tps FROM llm_samples s "
                "JOIN (SELECT model, MAX(ts) mts FROM llm_samples "
                "      WHERE ts >= ? GROUP BY model) m "
                "  ON s.model = m.model AND s.ts = m.mts",
                (cutoff,)).fetchall()
    except Exception:
        return {}
    last_tps = {}
    for r in last_rows:
        if r[0] is not None:
            last_tps[_normalize_model_name(r[0])] = r[1]
    out = {}
    for model, max_ts, runs, avg_tps, avg_ttft in rows:
        if model is None:
            continue
        key = _normalize_model_name(model)
        out[key] = {
            "last_used": int(max_ts) if max_ts is not None else None,
            "runs": int(runs or 0),
            "avg_tps": round(avg_tps, 2) if avg_tps is not None else None,
            "last_tps": last_tps.get(key),
            "avg_ttft_ms": round(avg_ttft, 1) if avg_ttft is not None else None,
        }
    return out


def _apply_usage_to_models(models, usage):
    """Attach the usage rollup to each registry entry by normalized name. A model
    with no samples gets a clean never-used shape (runs:0, last_used:null), never
    a missing key or NaN. Pure; mutates+returns the list it was given."""
    for m in models:
        u = usage.get(_normalize_model_name(m.get("name")))
        if u:
            m["last_used"] = u["last_used"]
            m["runs"] = u["runs"]
            m["avg_tps"] = u["avg_tps"]
            m["last_tps"] = u["last_tps"]
            m["avg_ttft_ms"] = u["avg_ttft_ms"]
        else:
            m["last_used"] = None
            m["runs"] = 0
            m["avg_tps"] = None
            m["last_tps"] = None
            m["avg_ttft_ms"] = None
    return models


# Default window for the local-vs-cloud savings rollup: 30d, the demo-headline span
# (matches the registry usage window). Bounded by RETENTION upstream.
_LLM_SAVINGS_WINDOW = int(os.environ.get("COPILOT_SAVINGS_WINDOW_DAYS", "30")) * 86400


def _llm_savings(now=None, window=None):
    """The headline "local vs cloud" inference-savings rollup over a window (30d
    default). One cheap aggregate read over llm_samples under the global LOCK
    (the existing DB-read discipline; MUST be called OUTSIDE any held LOCK).

    Returns a dict:
        {window_days, tokens, local_energy_kwh, local_cost, cloud_cost, saved,
         cloud_per_1k, currency}
    or None when there are NO samples in the window (so the UI hides the card).

    • tokens            = SUM(eval_count)               — local tokens generated
    • local_energy_kwh  = SUM(energy_wh)/1000           — measured GPU energy (NULL rows skip)
    • local_cost        = local_energy_kwh × per-kWh    — null when no tariff (cost disabled)
                          (tariff via _cost_ctx/_price_at — the SAME source of truth as the
                           Costs tab, so this can NEVER contradict it)
    • cloud_cost        = tokens/1000 × cloud_per_1k    — null when the cloud rate is unset/0
    • saved             = cloud_cost − local_cost       — when both present (the delta);
                          the UI uses cloud_cost as the headline number when local_cost is null.

    All money rounded sensibly. Never raises — None on any error / no samples."""
    now = int(now or time.time())
    win = _LLM_SAVINGS_WINDOW if window is None else window
    cutoff = now - win
    try:
        with LOCK:
            row = DB.execute(
                "SELECT COUNT(*), COALESCE(SUM(eval_count),0), SUM(energy_wh) "
                "FROM llm_samples WHERE ts >= ?", (cutoff,)).fetchone()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    tokens = int(row[1] or 0)
    energy_wh = row[2]  # SUM is NULL only when every in-window row is NULL
    # Cloud-equivalent: tokens/1000 × $/1k. Null when the rate is unset/0.
    cloud_per_1k = None
    cloud_cost = None
    try:
        raw = (get_settings().get("cloud_cost_per_1k") or "").strip()
        rate = float(raw) if raw != "" else 0.0
        if rate > 0:
            cloud_per_1k = rate
            cloud_cost = round((tokens / 1000.0) * rate, 2)
    except (TypeError, ValueError):
        cloud_per_1k = None
    # Local energy cost: measured GPU energy × the tariff (same source as Costs tab).
    # Null when no tariff is configured (cost-disabled host) or no priced rows.
    local_energy_kwh = None
    local_cost = None
    if energy_wh is not None and energy_wh > 0:
        local_energy_kwh = round(energy_wh / 1000.0, 5)
        try:
            ctx = _cost_ctx()
            price = _price_at(ctx, now)  # per-kWh at the current band
            if price and price > 0:
                local_cost = round(local_energy_kwh * price, 4)
        except Exception:
            local_cost = None
    saved = None
    if cloud_cost is not None and local_cost is not None:
        saved = round(cloud_cost - local_cost, 2)
    try:
        currency = _cost_ctx().get("currency") or "$"
    except Exception:
        currency = "$"
    return {
        "window_days": int(win // 86400),
        "tokens": tokens,
        "local_energy_kwh": local_energy_kwh,
        "local_cost": local_cost,
        "cloud_cost": cloud_cost,
        "saved": saved,
        "cloud_per_1k": cloud_per_1k,
        "currency": currency,
    }


def _llm_spend(now=None):
    """"Copilot spend today" rollup — TODAY + last-7-local-days local-inference
    energy/cost/cloud-equivalent, plus a per-local-day spark7 series for a tiny
    7-day trend on the AI Models / LLM tab.

    MIRRORS _llm_savings exactly (same tariff source via _cost_ctx/_price_at — the
    ONE source of truth, so it can never contradict the Costs tab or the savings
    KPI; same NULL-safe SUM(energy_wh); same graceful nulls). Pure stats over
    already-stored llm_samples rows — NO new poll, NO ollama call, NO network.

    "today"/days are bucketed by LOCAL calendar day (the SAME local-midnight
    convention as the status daily-ribbon / Costs "today"), via time.localtime.

    Returns:
        {today:{tokens, energy_wh, local_cost, cloud_cost, currency, calls},
         last7:{...same...},
         spark7:[{d, v} ... 7 local days oldest→newest],
         spark_metric: "cost"|"energy_wh"}

    today/last7 are always present (zeros when no inference). Never raises — on
    DB error returns the empty (zeros) shape. local_cost null when no tariff;
    cloud_cost null when no cloud rate; NULL energy_wh rows skipped (no NaN)."""
    now = int(now or time.time())
    # Local midnight today, and the start of the 7-local-day window.
    today_mid = time.mktime(time.strptime(
        time.strftime("%Y-%m-%d", time.localtime(now)), "%Y-%m-%d"))
    # Pull a touch wider than 7 days so an edge sample is captured regardless of
    # tz/DST drift; we re-bucket by exact local day below.
    win_start = int(today_mid - 6 * 86400 - 86400)
    try:
        with LOCK:
            rows = DB.execute(
                "SELECT ts, eval_count, energy_wh FROM llm_samples WHERE ts >= ?",
                (win_start,)).fetchall()
    except Exception:
        rows = []

    # Aggregate per local calendar day: calls, tokens, energy_wh (NULL-safe).
    per_day = {}  # 'YYYY-MM-DD' -> [calls, tokens, energy_wh_or_None]
    for ts, ev, wh in rows:
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        a = per_day.get(day)
        if a is None:
            a = [0, 0, None]
            per_day[day] = a
        a[0] += 1
        a[1] += int(ev or 0)
        if wh is not None:
            a[2] = (a[2] or 0.0) + wh

    # Tariff + cloud rate resolved ONCE, AFTER releasing LOCK (no nesting), the
    # same source as _llm_savings / the Costs tab.
    cloud_rate = 0.0
    try:
        raw = (get_settings().get("cloud_cost_per_1k") or "").strip()
        cloud_rate = float(raw) if raw != "" else 0.0
    except (TypeError, ValueError):
        cloud_rate = 0.0
    try:
        ctx = _cost_ctx()
        price = _price_at(ctx, now)  # per-kWh at the current band
        currency = ctx.get("currency") or "$"
    except Exception:
        price = None
        currency = "$"
    has_tariff = bool(price and price > 0)
    has_cloud = cloud_rate > 0

    def roll(day_list):
        """Aggregate a set of local days into the {tokens,energy_wh,...} shape."""
        calls = tokens = 0
        energy_wh = None
        for day in day_list:
            a = per_day.get(day)
            if not a:
                continue
            calls += a[0]
            tokens += a[1]
            if a[2] is not None:
                energy_wh = (energy_wh or 0.0) + a[2]
        local_cost = None
        if has_tariff and energy_wh is not None and energy_wh > 0:
            local_cost = round((energy_wh / 1000.0) * price, 4)
        cloud_cost = None
        if has_cloud:
            cloud_cost = round((tokens / 1000.0) * cloud_rate, 2)
        return {
            "tokens": tokens,
            "energy_wh": (round(energy_wh, 2) if energy_wh is not None else 0.0),
            "local_cost": local_cost,
            "cloud_cost": cloud_cost,
            "currency": currency,
            "calls": calls,
        }

    # The 7 local days oldest→newest off today's local midnight.
    week_days = [time.strftime("%Y-%m-%d", time.localtime(today_mid - i * 86400))
                 for i in range(6, -1, -1)]
    today_day = week_days[-1]

    # spark7 — the most demo-meaningful series: local_cost per day when a tariff
    # is set, else energy_wh per day. One value per local day, in order.
    spark_metric = "cost" if has_tariff else "energy_wh"
    spark7 = []
    for day in week_days:
        a = per_day.get(day)
        wh = (a[2] if a and a[2] is not None else 0.0)
        if spark_metric == "cost":
            v = round((wh / 1000.0) * price, 4)
        else:
            v = round(wh, 2)
        spark7.append({"d": day, "v": v})

    return {
        "today": roll([today_day]),
        "last7": roll(week_days),
        "spark7": spark7,
        "spark_metric": spark_metric,
    }


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


# ── Model registry (E1): the full on-disk model inventory ─────────────────────
# The resident view (/api/ps, above) shows what's LOADED right now. The registry
# is the superset: everything PULLED to disk (ollama GET /api/tags). Read-only
# metadata — no generation, no GPU spin. Cached briefly so a chatty UI can't hit
# ollama hard. Always-200 / graceful-degrade lives in the endpoint, not here.
_REGISTRY_LOCK = threading.Lock()
_REGISTRY_CACHE = None            # {"ts": int, "models": [...], "reachable": bool}
_REGISTRY_TTL = float(os.environ.get("COPILOT_REGISTRY_TTL", "45"))  # seconds


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
    models = _parse_model_registry(tags, resident)
    # Per-model usage rollup (last_used/runs/avg_tps) joined from llm_samples by
    # normalized name. Computed here so it rides the registry's 45s cache — no
    # separate hot path. Read happens outside the cache lock per the pattern.
    _apply_usage_to_models(models, _llm_usage_by_model())
    return models, True


def _model_registry(now=None):
    """Cached registry accessor. Serves a fresh fetch at most once per _REGISTRY_TTL
    so a chatty UI can't hammer ollama. Returns (models, reachable). Network I/O
    happens OUTSIDE the cache lock."""
    now = now or time.time()
    with _REGISTRY_LOCK:
        c = _REGISTRY_CACHE
        if c and (now - c["ts"]) < _REGISTRY_TTL:
            return list(c["models"]), c["reachable"]
    models, reachable = _fetch_model_registry()
    with _REGISTRY_LOCK:
        globals()["_REGISTRY_CACHE"] = {
            "ts": now, "models": models, "reachable": reachable}
    return list(models), reachable


@app.route("/api/models")
def api_models():
    """The Model Registry: the full inventory of models PULLED to disk on this
    host's ollama (GET /api/tags), cross-referenced with what's loaded right now
    (/api/ps) so the UI can flag resident models + their live VRAM.

    Always 200, graceful-degrade, never 500, no secret leak (we echo a `reachable`
    bool, never the URL/creds). Cached ~45s — this is rarely-changing metadata, so
    a busy tab can't hammer ollama. Read-only: no generation, no GPU spin.
    /api/tags is polled outside any held LOCK."""
    models, reachable = _model_registry()
    return jsonify({
        "enabled": COPILOT_ENABLED,
        "ollama_reachable": reachable,
        "models": models,
        "totals": _registry_totals(models),
    })


def _ollama_generate(prompt, timeout=None, capture=None, fmt=None):
    """Call the local ollama /api/generate (non-streaming). Returns (text, error)
    where exactly one is non-None. Never raises. `error` is a short machine code:
    'disabled' | 'no_model' | 'unreachable' | 'bad_response'.

    `capture`, when a list, has this call's throughput metrics dict (from
    `_llm_metrics_from_response`, or None) appended — lets a caller build the
    per-inference cost chip WITHOUT changing the (text, error) return shape that
    six callers rely on.

    `fmt`, when set, is ollama's structured-output `format` field — pass either
    the string "json" or a JSON-Schema dict; the returned `text` is then the raw
    JSON string the model produced (caller parses it). When fmt is None the
    request body is byte-for-byte identical to before, so the six prose callers
    are unaffected."""
    if not COPILOT_ENABLED:
        return None, "disabled"
    url = COPILOT_OLLAMA_URL + "/api/generate"
    _payload = {
        "model": COPILOT_MODEL, "prompt": prompt, "stream": False,
        "options": {"temperature": 0.2, "num_predict": 220},
    }
    if fmt is not None:
        _payload["format"] = fmt
    body = json.dumps(_payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=(timeout or COPILOT_TIMEOUT)) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        _capture_llm_metrics(data)  # side-channel; never alters text below
        if isinstance(capture, list):
            try:
                capture.append(_llm_metrics_from_response(data, COPILOT_MODEL))
            except Exception:
                capture.append(None)
        txt = (data.get("response") or "").strip()
        if not txt:
            return None, "bad_response"
        return txt, None
    except urllib.error.HTTPError as e:
        # 404 from ollama usually means the model isn't pulled
        if e.code == 404:
            return None, "no_model"
        print("copilot ollama HTTP error:", e.code, flush=True)
        return None, "unreachable"
    except Exception as e:
        print("copilot ollama error:", type(e).__name__, flush=True)
        return None, "unreachable"


def _ollama_generate_stream(prompt, timeout=None):
    """Streaming sibling of `_ollama_generate`: calls ollama /api/generate with
    `stream:true` and yields ('token', text) tuples as tokens arrive, then a
    terminal ('done', {'text','metrics'}) or ('error', code) tuple. Never raises;
    on any failure it yields a single ('error', <code>) and stops. The error
    codes match `_ollama_generate` ('disabled'|'no_model'|'unreachable'|
    'bad_response') so callers can reuse the same status mapping.

    ollama returns one JSON object per line; each carries a `response` chunk and
    a final object has `done:true` plus the timing fields we capture for tok/s."""
    if not COPILOT_ENABLED:
        yield ("error", "disabled")
        return
    url = COPILOT_OLLAMA_URL + "/api/generate"
    body = json.dumps({
        "model": COPILOT_MODEL, "prompt": prompt, "stream": True,
        "options": {"temperature": 0.2, "num_predict": 220},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    emitted_any = False
    full = []
    last_obj = None
    try:
        with urllib.request.urlopen(req, timeout=(timeout or COPILOT_TIMEOUT)) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                last_obj = obj
                chunk = obj.get("response")
                if chunk:
                    emitted_any = True
                    full.append(chunk)
                    yield ("token", chunk)
                if obj.get("done"):
                    break
    except urllib.error.HTTPError as e:
        if e.code == 404:
            yield ("error", "no_model")
        else:
            print("copilot ollama stream HTTP error:", e.code, flush=True)
            yield ("error", "unreachable")
        return
    except Exception as e:
        # Mid-stream failure (timeout, reset, …): surface it so the caller can
        # fall back to the routed facts. We may already have emitted tokens.
        print("copilot ollama stream error:", type(e).__name__, flush=True)
        yield ("error", "unreachable")
        return
    text = "".join(full).strip()
    if not emitted_any or not text:
        yield ("error", "bad_response")
        return
    metrics = None
    try:
        if isinstance(last_obj, dict):
            _capture_llm_metrics(last_obj)  # side-channel; tok/s trend
            metrics = _llm_metrics_from_response(last_obj, COPILOT_MODEL)
    except Exception:
        metrics = None
    yield ("done", {"text": text, "metrics": metrics})


@app.route("/api/copilot/digest")
def api_copilot_digest():
    """Plain-English 'lab right now' digest generated by the local LLM over the
    monitor's own metrics. Always 200: on any LLM problem it returns the
    deterministic fact summary plus a `source`/`llm_status` the UI surfaces as a
    clear graceful state."""
    now = int(time.time())
    ctx = _copilot_context(now)
    facts = _copilot_facts(ctx)
    out = {"now": now, "model": COPILOT_MODEL, "facts": facts,
           "context": ctx, "enabled": COPILOT_ENABLED}
    _cap = []
    text, err = _ollama_generate(_copilot_digest_prompt(facts), capture=_cap)
    if text is not None:
        out.update({"digest": text, "source": "llm", "llm_status": "ok"})
        inf = _inference_cost(_cap[0] if _cap else None, now)
        if inf:
            out["inference"] = inf
    else:
        out.update({"digest": " ".join(facts), "source": "facts", "llm_status": err})
    return jsonify(out)


# Dark variant of the email-safe palette used by _render_digest_html. Same inline-
# only, no-CSS/JS, no-external-asset contract — we just post-process the light HTML's
# named/hex colours to their dark equivalents so the preview can be viewed in a dark
# browser tab. Order matters (longest/most-specific first) so substrings don't clash.
_DIGEST_DARK_SUBS = (
    ("background:#ffffff", "background:#0b0f17"),
    ("background:#f9fafb", "background:#161b26"),
    ("color:#111827", "color:#f3f4f6"),
    ("color:#1f2937", "color:#e5e7eb"),
    ("color:#374151", "color:#cbd5e1"),
    ("color:#4b5563", "color:#9ca3af"),
    ("border-top:1px solid #e5e7eb", "border-top:1px solid #2a3242"),
    ("color:#9ca3af", "color:#7b8494"),
)


def _digest_html_dark(html_doc):
    """Recolour the light, email-safe digest HTML to a dark variant for the in-browser
    preview ONLY (the email channel keeps the light original — never touched). Pure
    string substitution over our own known inline colours; adds no <style>/JS/assets,
    leaks nothing new. Severity tints (#b91c1c/#b45309) stay — they read fine on dark."""
    for light, dark in _DIGEST_DARK_SUBS:
        html_doc = html_doc.replace(light, dark)
    return html_doc


@app.route("/api/copilot/digest/preview")
def api_copilot_digest_preview():
    """In-browser preview of the CURRENT scheduled digest, rendered as a standalone
    HTML page by the SAME builders the email channel uses (_digest_sections +
    _render_digest_html) — one source of truth, so the preview is byte-faithful to
    what would actually be sent. Always 200 text/html, never hangs, never 500, no
    secret (telemetry facts only; the renderer html-escapes every dynamic value).

    This is an EXPLICIT user action (clicking Preview), so a synchronous ollama call
    for the narrative is acceptable — but bounded: if the LLM is disabled/slow/down
    the page still renders the deterministic sections (narrative omitted), exactly as
    the digest already degrades. ollama is NEVER triggered on a poll, only here.

    Query params:
      • theme = light (default) | dark  — accepted for both; unknown values fall back
        to light. dark applies a recolour of the email-safe palette only.
      • narrative = 1 (default) | 0     — narrative=0 skips ollama for an instant,
        purely-deterministic preview.

    LOCK discipline mirrors /api/copilot/digest: _digest_sections / _copilot_context
    acquire LOCK internally; ollama is called with NO lock held."""
    now = int(time.time())
    theme = (request.args.get("theme") or "light").strip().lower()
    if theme != "dark":
        theme = "light"
    want_narrative = (request.args.get("narrative") or "1").strip() != "0"

    # Deterministic sections first (each helper takes LOCK itself, briefly).
    try:
        sections = _digest_sections(now)
    except Exception as e:
        print("digest preview sections error:", e, flush=True)
        sections = []

    # Optional narrative — assembled OUTSIDE any held lock, and only on this explicit
    # request. Any LLM problem (disabled/unreachable/slow/bad) just drops the
    # narrative; the sections stand alone, so the page is always valid.
    narrative = None
    if want_narrative:
        try:
            ctx = _copilot_context(now)
            facts = _copilot_facts(ctx)
            narrative, _err = _ollama_generate(_copilot_digest_prompt(facts))
        except Exception as e:
            print("digest preview narrative error:", e, flush=True)
            narrative = None

    try:
        html_doc = _render_digest_html(narrative, sections, now)
    except Exception as e:
        print("digest preview render error:", e, flush=True)
        html_doc = ("<!DOCTYPE html><html><body><p>HomeLab Monitor — Lab Copilot "
                    "brief: no data to preview yet.</p></body></html>")
    if theme == "dark":
        html_doc = _digest_html_dark(html_doc)

    return Response(html_doc, mimetype="text/html",
                    headers={"Cache-Control": "no-store"})


@app.route("/api/llm")
def api_llm():
    """The AI Lab Cockpit's live LLM-engine surface. Always 200, graceful-degrade,
    never 500, no secret leak (we echo a `reachable` bool + model name, never the
    URL/creds). Honest numbers only:
      • `last` — the most recent REAL throughput measurement captured as a
        side-channel off our existing copilot generations (tok/s + TTFT derived
        from ollama's eval_count/eval_duration/load_duration). null until a
        generation has run; carries `age_sec` so the UI shows freshness.
      • `resident` — models loaded RIGHT NOW from ollama /api/ps (read-only),
        with VRAM, GPU/CPU split, and keep-alive countdown.
    /api/ps is polled here, outside any held LOCK."""
    resident, reachable = _llm_resident_models()
    with _LLM_LOCK:
        last = dict(_LLM_LAST) if _LLM_LAST else None
    if last is not None:
        last["age_sec"] = max(0, int(time.time()) - last.pop("ts"))
    try:
        limit = max(1, min(int(request.args.get("history", 60)), _LLM_SAMPLE_CAP))
    except (TypeError, ValueError):
        limit = 60
    return jsonify({
        "enabled": COPILOT_ENABLED,
        "ollama_reachable": reachable,
        "model": COPILOT_MODEL,
        "last": last,
        "resident": resident,
        # Sparse tok/s trend — one point per real copilot generation. Empty list
        # (honest) until a generation has run; never 500.
        "history": _llm_history(limit),
        # Local-vs-cloud inference savings rollup (30d). null when no samples yet;
        # cloud/local money fields self-hide when the rate/tariff is unset.
        "savings": _llm_savings(),
        # "Copilot spend today" rollup — today + last-7-local-days local energy/
        # cost/cloud-equivalent + a per-local-day spark7 trend. Always present
        # (zeros when no inference today); same tariff source as `savings`.
        "spend": _llm_spend(),
    })


# ── Scheduled digest ──────────────────────────────────────────────────────────
# The headline fusion of three shipped pillars: the Copilot digest builder +
# forecasting context + the alerting channel dispatch. A plain-English daily
# summary, PUSHED on a schedule through the channels the user already configured.
#
# Inert by default: zero new behaviour unless the user (a) flips digest_enabled
# AND (b) has a notification channel configured. A user who does nothing sees
# nothing. Read-only otherwise — it only sends data OUT, never mutates the host.

DIGEST_TITLE = "Lab Copilot — daily digest"
DIGEST_TITLE_WEEKLY = "Lab Copilot — weekly digest"

# Digest v2 — a structured, multi-section brief built entirely from already-computed
# signals (the SAME forecast/reco/incident/uptime/cost accessors the dashboard uses),
# led by the optional ollama narrative. Every section is DETERMINISTIC and stands
# alone: if the LLM is unreachable the brief still sends (just without the narrative
# line), and it never carries secrets — only telemetry facts (no URLs/tokens/settings).


def _digest_sections(now=None):
    """Assemble the deterministic, secret-free sections of the digest from the
    live signal bundle (reused from the recommendations path — no new heavy work).
    Returns a list of (header, [lines]) tuples; empty sections are omitted by the
    caller. Never raises (a failed sub-signal just yields fewer lines)."""
    now = now or int(time.time())
    sections = []
    try:
        sig = _reco_signals(now)
    except Exception as e:
        print("digest signals error:", e, flush=True)
        sig = {}
    try:
        recos = _reco_detect(sig, now)[:RECO_MAX_ITEMS]
    except Exception:
        recos = []

    # ── Needs attention: open recommendations + incidents + down uptime checks ──
    attn = []
    inc = sig.get("incidents") or {}
    open_inc = int(inc.get("open") or 0)
    uptime = [c for c in (sig.get("uptime") or []) if c.get("enabled")]
    down = [c for c in uptime if c.get("state") == "down"]
    crit_n = sum(1 for it in recos if it.get("severity") == "crit")
    warn_n = sum(1 for it in recos if it.get("severity") == "warn")
    if recos:
        attn.append("Recommendations: %d open (%d critical, %d warning)." % (
            len(recos), crit_n, warn_n))
        for it in recos[:3]:
            attn.append("  - [%s] %s" % ((it.get("severity") or "info").upper(),
                                         it.get("title") or "?"))
    if open_inc:
        top = inc.get("top") or {}
        attn.append("Incidents: %d open (top severity %s, %d active series)." % (
            open_inc, top.get("severity") or "?", int(top.get("active_count") or 0)))
    if down:
        names = ", ".join(str(c.get("label") or c.get("id") or "check") for c in down[:3])
        attn.append("Uptime: %d check%s DOWN — %s." % (
            len(down), "" if len(down) == 1 else "s", names))
    if attn:
        sections.append(("Needs attention", attn))

    # ── Capacity: disk-fill / VRAM / cost-projection ETAs (the nudges) ──────────
    cap = []
    fills = sorted(
        [d for d in (sig.get("disk") or [])
         if d.get("status") == "filling" and d.get("eta_days") is not None],
        key=lambda d: d.get("eta_days"))
    for d in fills[:3]:
        cap.append("Disk %s is %s%% full, fills in ~%s days (~%s GB/day)." % (
            d.get("mount") or "?", d.get("pct"), _reco_num(d.get("eta_days")),
            _reco_num(d.get("gb_per_day"))))
    v = sig.get("vram") or {}
    if v.get("status") == "filling" and v.get("eta_min") is not None:
        cap.append("VRAM trending to full in ~%s min (~%s MB/min)." % (
            _reco_num(v.get("eta_min")), _reco_num(v.get("mb_per_min"))))
    elif v.get("free_gb") is not None and v.get("total_gb"):
        cap.append("VRAM headroom %s GB of %s GB total." % (
            _reco_num(v.get("free_gb")), _reco_num(v.get("total_gb"))))
    if cap:
        sections.append(("Capacity", cap))

    # ── Cost: month-to-date + projected + top entity ───────────────────────────
    cost = []
    cm = sig.get("cost_month") or {}
    if cm.get("enabled"):
        cur = cm.get("currency") or "$"
        line = "Energy month-to-date %s%s; projected full month %s%s" % (
            cur, cm.get("month_to_date"), cur, cm.get("projected_month"))
        dp = cm.get("delta_pct")
        if dp is not None:
            line += " (%s%d%% vs last month)" % ("+" if dp >= 0 else "", dp)
        cost.append(line + ".")
        try:
            top_ents = _ask_top_cost_entities(now, n=1)
        except Exception:
            top_ents = []
        if top_ents:
            t = top_ents[0]
            if t.get("priced"):
                cost.append("Top spender (30d): %s '%s' — %s%s." % (
                    t.get("kind"), t.get("name"), t.get("currency") or "$", t.get("cost")))
            else:
                cost.append("Top consumer (30d): %s '%s' — %s kWh." % (
                    t.get("kind"), t.get("name"), t.get("energy_kwh")))
        if cost:
            sections.append(("Cost", cost))

    # ── Fleet / Uptime: up/down checks + uptime % ──────────────────────────────
    fleet = []
    if uptime:
        up = sum(1 for c in uptime if c.get("state") == "up")
        pcts = [c.get("uptime") for c in uptime if c.get("uptime") is not None]
        avg = (sum(pcts) / len(pcts)) if pcts else None
        line = "Uptime checks: %d up, %d down of %d" % (up, len(down), len(uptime))
        if avg is not None:
            line += " (avg %s%% over window)" % _reco_num(round(avg, 1))
        fleet.append(line + ".")
        for c in down[:3]:
            fleet.append("  - %s is down." % str(c.get("label") or c.get("id") or "check"))
        # SLO error-budget callouts: only when notable (over budget OR burning hot)
        # AND backed by enough data — silent otherwise.
        for c in uptime:
            slo = c.get("slo") or {}
            if not slo.get("data_sufficient"):
                continue
            name = str(c.get("label") or c.get("id") or "check")
            if slo.get("over_budget"):
                fleet.append("  ⚠️ %s burned %s%% of its error budget (SLO %s%%)." % (
                    name, _reco_num(slo.get("budget_consumed_pct")),
                    _reco_num(round(slo.get("target", 0) * 100, 3))))
            elif slo.get("burning"):
                fleet.append("  ⚠️ %s is burning error budget fast (%s× over the last hour)." % (
                    name, _reco_num(slo.get("burn_1h"))))
    if fleet:
        sections.append(("Fleet / Uptime", fleet))

    # ── Anomalies: recent z-score flags ────────────────────────────────────────
    anoms_block = sig.get("anomalies") or {}
    items = (anoms_block.get("items") or [])[:3]
    if items:
        an = []
        for a in items:
            an.append("%s %s — %s%s now vs ~%s%s baseline (%sσ)." % (
                a.get("key"), a.get("direction"), a.get("value"), a.get("unit"),
                a.get("baseline"), a.get("unit"), a.get("z")))
        sections.append(("Anomalies", an))
    elif anoms_block.get("status") == "quiet":
        sections.append(("Anomalies", ["No anomalies — all monitored series are within range."]))

    # ── Latest postmortem: cite the most-recent PERSISTED incident postmortem ────
    # Deterministic — reads already-persisted prose only, NEVER a new LLM call.
    # Omitted cleanly when no postmortem has ever been generated.
    try:
        pm = _latest_postmortem_citation()
    except Exception:
        pm = None
    if pm and pm.get("cause"):
        # One combined parenthetical: "<n signals>, <when>" — never two adjacent
        # runs like "(1 signal) (2026-...)". Either part may be absent.
        n = pm.get("signals")
        bits = []
        if isinstance(n, int) and n >= 0:
            bits.append("%d signal%s" % (n, "" if n == 1 else "s"))
        if pm.get("when"):
            bits.append(pm["when"])
        meta = (" (%s)" % ", ".join(bits)) if bits else ""
        sections.append(("Latest postmortem",
                         ["%s%s — %s." % (pm.get("title") or "incident", meta, pm["cause"])]))

    return sections


def _render_digest_body(narrative, sections):
    """Plain-text, scannable layout that reads well across Discord/ntfy/Telegram/
    email/Slack. The narrative (when present) leads; deterministic sections follow
    under simple headers. Never empty when sections exist."""
    parts = []
    if narrative:
        parts.append("Summary")
        parts.append(narrative.strip())
    for header, lines in sections:
        if not lines:
            continue
        parts.append(("\n" if parts else "") + header)
        parts.extend(lines)
    return "\n".join(parts).strip()


# Subtle severity tints for "Needs attention" lines that begin with [CRIT]/[WARN]
# (matches the plain-text bullets emitted by _digest_sections). Email-client-safe
# named/hex colors only — no CSS classes, no external assets.
_DIGEST_HTML_SEV = (
    ("[CRIT]", "#b91c1c"),   # red-700
    ("[WARN]", "#b45309"),   # amber-700
)


def _render_digest_html(narrative, sections, now=None):
    """Render the SAME deterministic `sections` (and optional ollama narrative) as a
    compact, email-client-safe HTML brief — inline styles only, no <style>/CSS/JS,
    no external assets (Gmail/Outlook-safe). Every dynamic value is HTML-escaped so a
    model/check/entity name containing '<' or '&' can't break the markup. Carries
    telemetry facts ONLY (no secrets), identical content to the plain-text body, so
    the two never drift. Never empty when sections exist."""
    esc = _html.escape
    ts = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(now or time.time()))
    blocks = []
    wrap = "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"

    def _line_html(line):
        # Indented sub-bullets ("  - ...") render as nested; severity-tagged lines
        # get a subtle color. Escape first, then wrap.
        raw = line.rstrip()
        stripped = raw.lstrip()
        indented = raw != stripped
        color = None
        for tag, col in _DIGEST_HTML_SEV:
            if tag in stripped:
                color = col
                break
        style = "margin:2px 0;font-size:13px;line-height:1.45;color:%s;" % (color or "#1f2937")
        if indented:
            style += "padding-left:16px;color:%s;" % (color or "#4b5563")
        return '<div style="%s">%s</div>' % (style, esc(stripped))

    # Header
    blocks.append(
        '<div style="font-size:18px;font-weight:600;color:#111827;'
        'margin:0 0 4px;">HomeLab Monitor — Lab Copilot brief</div>')

    # Narrative summary (LLM, optional). Omitted entirely when LLM is down.
    if narrative:
        blocks.append(
            '<div style="margin:10px 0 4px;">'
            '<div style="font-size:13px;font-weight:600;color:#374151;'
            'text-transform:uppercase;letter-spacing:.04em;">Summary</div>'
            '<div style="font-size:13px;line-height:1.5;color:#1f2937;'
            'margin-top:3px;white-space:pre-wrap;">%s</div></div>' % esc(narrative.strip()))

    # Deterministic sections — always render even with LLM down.
    for header, lines in sections:
        if not lines:
            continue
        attn = (header == "Needs attention")
        accent = "#b45309" if attn else "#374151"
        inner = "".join(_line_html(ln) for ln in lines)
        blocks.append(
            '<div style="margin:12px 0 0;padding:8px 10px;background:#f9fafb;'
            'border-left:3px solid %s;border-radius:4px;">'
            '<div style="font-size:13px;font-weight:600;color:%s;'
            'text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px;">%s</div>'
            '%s</div>' % (accent, accent, esc(header), inner))

    body_inner = "".join(blocks)
    footer = (
        '<div style="margin-top:16px;padding-top:8px;border-top:1px solid #e5e7eb;'
        'font-size:11px;color:#9ca3af;">HomeLab Monitor &middot; %s</div>' % esc(ts))

    return (
        '<!DOCTYPE html><html><body style="margin:0;padding:0;background:#ffffff;">'
        '<div style="%smax-width:640px;margin:0 auto;padding:16px;color:#1f2937;">'
        '%s%s</div></body></html>' % (wrap, body_inner, footer))


def build_digest(now=None, with_html=False):
    """Build the digest message (title, body, llm_status[, html]). Composes the
    optional ollama narrative (our differentiator) on top of a structured,
    deterministic brief assembled from the live signals. When the LLM is unreachable
    the brief still sends — just without the narrative line — and is never empty. The
    body carries telemetry facts ONLY (no secrets). Never raises.

    With `with_html=True`, also returns an email-client-safe HTML rendering of the
    SAME sections+narrative as a 4th element (for the email channel only)."""
    now = now or int(time.time())
    s = get_settings()
    weekly = (s.get("digest_cadence") or "daily").strip() == "weekly"
    title = DIGEST_TITLE_WEEKLY if weekly else DIGEST_TITLE
    try:
        ctx = _copilot_context(now)
        facts = _copilot_facts(ctx)
    except Exception as e:
        print("build_digest context error:", e, flush=True)
        facts = ["The monitor could not assemble metrics for this digest."]
    try:
        sections = _digest_sections(now)
    except Exception as e:
        print("build_digest sections error:", e, flush=True)
        sections = []
    narrative, err = _ollama_generate(_copilot_digest_prompt(facts))
    llm_status = "ok" if narrative else (err or "facts")
    body = _render_digest_body(narrative, sections)
    if not body:
        # Last-resort fallback: the terse fact bullets (always non-empty).
        body = "\n".join("- " + f for f in facts)
    if with_html:
        try:
            html_body = _render_digest_html(narrative, sections, now)
        except Exception as e:
            print("build_digest html error:", e, flush=True)
            html_body = None
        return title, body, llm_status, html_body
    return title, body, llm_status


def send_digest(channel=None, s=None, record=True):
    """Send one digest through the requested channel using the existing alert
    dispatch. `channel` defaults to the configured digest_channel. Returns a dict
    {ok, results, llm_status, reason?}. Never raises, never mutates the host.

    MUST be called OUTSIDE any held LOCK: build_digest()->_copilot_context()
    acquires LOCK itself (non-reentrant)."""
    s = s or get_settings()
    channel = (channel or s.get("digest_channel") or "all").strip()
    if channel not in _VALID_CHANNELS:
        return {"ok": False, "results": [], "reason": "Unknown channel."}
    if channel == "all":
        if not _configured_channels(s):
            return {"ok": False, "results": [], "reason": "No channel configured."}
    elif channel not in _configured_channels(s):
        return {"ok": False, "results": [], "reason": "Channel not configured."}
    title, body, llm_status, html_body = build_digest(with_html=True)
    results = dispatch_alert(s, "info", title, body, channel=channel,
                             html_detail=html_body)
    if record:
        for ch, ok, err in results:
            record_alert(None, "scheduled digest", "info", ch,
                         "sent" if ok else "error", title,
                         None if ok else (err or ""))
    return {"ok": all(ok for _, ok, _ in results),
            "results": [{"channel": c, "ok": ok, "error": err} for c, ok, err in results],
            "llm_status": llm_status}


def _digest_due(s, now=None):
    """True iff the digest should fire on this pass: enabled, a channel is
    configured, the local wall-clock has reached digest_time, and we have not
    already sent today's digest. Edge-triggered via digest_last_sent (a date), so
    it fires exactly once on the first pass at/after the target time — robust to
    the loop interval not landing on HH:MM.

    Cadence:
      • daily  — fires once today, at/after digest_time (unchanged behaviour).
      • weekly — fires ONLY on digest_weekday (0=Mon … 6=Sun), once that day at/
        after digest_time. On any other weekday it never fires, and the once-per-
        day latch (digest_last_sent) prevents a second send the same day."""
    if s.get("digest_enabled") != "1":
        return False
    ch = (s.get("digest_channel") or "all").strip()
    if ch == "all":
        if not _configured_channels(s):
            return False
    elif ch not in _configured_channels(s):
        return False
    try:
        hh, mm = (s.get("digest_time") or "08:00").split(":")
        target_min = int(hh) * 60 + int(mm)
    except Exception:
        target_min = 8 * 60
    lt = time.localtime(now or time.time())
    today = time.strftime("%Y-%m-%d", lt)
    if (s.get("digest_last_sent") or "") == today:
        return False                      # already sent today's digest
    if (s.get("digest_cadence") or "daily").strip() == "weekly":
        try:
            want_wday = int(s.get("digest_weekday") or "0") % 7
        except Exception:
            want_wday = 0
        if lt.tm_wday != want_wday:        # wrong weekday — never fire
            return False
    return (lt.tm_hour * 60 + lt.tm_min) >= target_min


def maybe_send_digest(now=None):
    """Scheduler tick — called once per collector pass, OUTSIDE the LOCK. Sends
    today's digest exactly once, on the first pass at/after digest_time. Records
    the send-date BEFORE dispatching so a slow/failing channel can never cause a
    double-send within the same day. Never raises."""
    try:
        s = get_settings()
        if not _digest_due(s, now):
            return False
        today = time.strftime("%Y-%m-%d", time.localtime(now or time.time()))
        # Edge-trigger latch: stamp the date first so concurrent/next passes
        # see "already sent today" even while this send is in flight.
        save_settings({"digest_last_sent": today})
        out = send_digest(s=s)
        if not out.get("ok"):
            print("scheduled digest send issue:", out.get("reason") or "channel error", flush=True)
        return True
    except Exception as e:
        print("maybe_send_digest error:", e, flush=True)
        return False


@app.route("/api/copilot/digest/send", methods=["POST"])
def api_copilot_digest_send():
    """Manually push a digest now through a channel (the UI 'Send test digest'
    button). Always a clean 200/400 — never a 500. Does not touch the daily
    edge-trigger latch, so a manual send never suppresses the scheduled one."""
    body = request.get_json(silent=True) or {}
    channel = (body.get("channel") or "").strip() or None
    out = send_digest(channel=channel)
    if out.get("reason") and not out.get("results"):
        return jsonify({"ok": False, "reason": out["reason"]}), 400
    return jsonify(out)


# ── Copilot ask-box: live-data retrieval / routing ────────────────────────────
# Upgrade the ask-box from "answers off a fixed digest bundle" to "answers off
# the lab's OWN live data, routed to the question". A deterministic, LLM-free
# routing step (cheap, no second round-trip) reads the question, detects (a) any
# KNOWN entity name it mentions (matched against the live container/service/model
# lists) and (b) topic intent via keyword sets, then pulls a COMPACT, relevant
# fact slice from the SAME accessors that power the API/MCP layer. The routed
# facts (clearly labelled) + the question go to the existing _ollama_generate.
# A `sources` list names what data informed the answer (transparency). On no
# match it falls back to the generic digest context (never worse than before);
# on an LLM-down it returns the routed facts themselves as a readable summary.
#
# HARD bounds keep the prompt small for a local model: at most _ASK_MAX_FACTS
# fact lines, each clipped to _ASK_MAX_LINE chars, total clipped to _ASK_MAX_CHARS.
# READ-ONLY: pure telemetry reads, no host mutation, no new external calls. No
# secrets/settings ever enter the fact set — only telemetry. Facts are assembled
# OUTSIDE any held LOCK (the accessors take the non-reentrant LOCK themselves).

_ASK_MAX_FACTS = 14       # cap injected fact lines
_ASK_MAX_LINE  = 240      # cap chars per fact line
_ASK_MAX_CHARS = 2200     # cap total injected fact chars (prompt stays small)

# Topic intent keyword sets. A topic fires when any of its keywords appears as a
# word-ish token in the (lower-cased) question. Multiple topics can fire.
_ASK_TOPICS = {
    "gpu":      ("gpu", "vram", "graphics", "cuda", "nvidia", "temperature", "temp",
                 "hot", "overheat", "overheating", "power", "watt", "watts", "utilisation",
                 "utilization", "util", "healthy", "health"),
    "disk":     ("disk", "disks", "storage", "drive", "mount", "filesystem", "fill",
                 "filling", "full", "space", "free", "/backup", "/data"),
    # Deliberately specific: bare "read"/"write"/"io" were dropped because whole-
    # token matching made unrelated questions ("read the logs") pull disk-I/O
    # facts. Multi-word entries (with a space) are matched as substrings.
    "disk_io":  ("diskio", "i/o", "disk i/o", "disk io", "iops", "throughput",
                 "latency", "seeks", "mb/s", "sda", "nvme", "vda"),
    "cost":     ("cost", "costs", "expensive", "cheap", "cheapest", "price", "pricey",
                 "budget", "spend", "spending", "money", "bill", "energy", "kwh",
                 "electricity", "eur", "euro", "euros", "dollar", "dollars"),
    "memory":   ("memory", "ram", "mem", "oom", "killed", "leak", "leaking", "swap"),
    "uptime":   ("uptime", "down", "downtime", "availability", "available", "offline",
                 "online", "reachable", "outage"),
    "incident": ("incident", "incidents", "anomaly", "anomalies", "anomalous", "spike",
                 "spikes", "alert", "alerts", "weird", "wrong", "unusual"),
    "container": ("container", "containers", "docker", "service", "services", "systemd",
                  "restart", "restarts", "restarting", "crash", "crashing", "unhealthy"),
}
# Cost/expensive intent that should rank entities (top-N by cost).
_ASK_RANK_HINTS = ("expensive", "cheap", "cheapest", "most", "biggest", "highest",
                   "top", "largest", "priciest", "costly")


def _ask_tokens(q):
    """Lower-cased word-ish tokens from the question (letters/digits/_/-/./:/€/$),
    for deterministic keyword + entity matching with sane boundaries."""
    return set(re.findall(r"[a-z0-9_./:€$-]+", (q or "").lower()))


def _ask_detect_topics(q):
    """Return the set of topic keys whose keyword set matches the question.
    Single-token keywords match on word boundaries (the tokenizer); multi-word
    keywords (those containing a space, e.g. "disk io") match as substrings so a
    phrase can trigger without reintroducing over-broad bare tokens."""
    toks = _ask_tokens(q)
    if not toks:
        return set()
    ql = (q or "").lower()
    out = set()
    for topic, kws in _ASK_TOPICS.items():
        for kw in kws:
            if (kw in ql) if " " in kw else (kw in toks):
                out.add(topic)
                break
    return out


def _ask_live_entities():
    """Snapshot the live entity-name → kind map the question can match against:
    container names, systemd service names, and currently-loaded model names.
    Read-only over HEALTH / LATEST. Never raises. Names are returned verbatim
    (the prompt escapes nothing — these are telemetry identifiers, no secrets)."""
    ents = {}   # lower-name -> {"name": original, "kind": "container|service|model"}
    try:
        dock = (HEALTH.get("docker") or {})
        for c in (dock.get("containers") or []):
            nm = (c.get("name") or "").strip()
            if nm:
                ents.setdefault(nm.lower(), {"name": nm, "kind": "container"})
    except Exception:
        pass
    try:
        sysd = (HEALTH.get("systemd") or {})
        for s in (sysd.get("services") or []):
            nm = (s.get("name") or "").strip()
            # systemd unit names carry a .service suffix; index both forms
            if nm:
                ents.setdefault(nm.lower(), {"name": nm, "kind": "service"})
                base = re.sub(r"\.service$", "", nm)
                if base and base.lower() not in ents:
                    ents.setdefault(base.lower(), {"name": nm, "kind": "service"})
    except Exception:
        pass
    try:
        for m in (LATEST.get("models") or []):
            nm = (m.get("model") or "").strip()
            if nm:
                ents.setdefault(nm.lower(), {"name": nm, "kind": "model"})
    except Exception:
        pass
    return ents


def _ask_match_entities(q, ents=None):
    """Detect KNOWN entity names mentioned in the question. Matches against the
    live name list (case-insensitive) with word-ish boundaries to avoid silly
    substring false-hits (e.g. 'cat' must not match 'concatd'). Returns a list of
    {"name","kind"} (de-duplicated, longest names first so 'stable-diffusion'
    wins over a stray 'stable'). Bounded to a handful."""
    if ents is None:
        ents = _ask_live_entities()
    if not ents:
        return []
    ql = " " + (q or "").lower() + " "
    hits = []
    seen = set()
    # longest first: prefer the most specific name when several would match
    for low in sorted(ents.keys(), key=len, reverse=True):
        if len(low) < 2:
            continue
        # word-ish boundary: the name must be flanked by a non [a-z0-9] char.
        pat = r"(?<![a-z0-9])" + re.escape(low) + r"(?![a-z0-9])"
        if re.search(pat, ql):
            e = ents[low]
            key = (e["name"], e["kind"])
            if key not in seen:
                seen.add(key)
                hits.append(e)
        if len(hits) >= 4:
            break
    return hits


def _ask_entity_facts(name, kind, now):
    """Compact fact slice for one named entity: its live health/cpu/mem/restarts
    (container/service), month-to-date energy cost via the per-entity power_proc
    path, plus any recent OOM event or anomaly touching it. Returns (lines, srcs).
    Read-only; assembled OUTSIDE the LOCK except for the bounded DB reads here."""
    lines, srcs = [], []
    cur_kind = kind
    # live health from HEALTH snapshot
    try:
        if kind == "container":
            for c in ((HEALTH.get("docker") or {}).get("containers") or []):
                if (c.get("name") or "").lower() == name.lower():
                    mem = c.get("mem_bytes")
                    vram = c.get("vram_bytes")
                    parts = ["state {}".format(c.get("state") or "?"),
                             c.get("label") or c.get("status") or "?"]
                    if mem:
                        parts.append("RAM {} MB".format(round(mem / 1048576)))
                    if vram:
                        parts.append("VRAM {} MB".format(round(vram / 1048576)))
                    if c.get("uptime_s"):
                        parts.append("up {}h".format(round(c["uptime_s"] / 3600)))
                    lines.append("Container {n}: {p}.".format(n=name, p=", ".join(parts)))
                    srcs.append("container:" + name)
                    break
        elif kind == "service":
            for s in ((HEALTH.get("systemd") or {}).get("services") or []):
                if (s.get("name") or "").lower() == name.lower():
                    parts = ["active {}".format(s.get("active") or s.get("sub") or "?")]
                    if s.get("status"):
                        parts.append(s.get("label") or s["status"])
                    if s.get("mem_bytes"):
                        parts.append("RAM {} MB".format(round(s["mem_bytes"] / 1048576)))
                    if s.get("exit_status") is not None:
                        parts.append("last exit {}".format(s["exit_status"]))
                    lines.append("Service {n}: {p}.".format(n=name, p=", ".join(parts)))
                    srcs.append("service:" + name)
                    break
        elif kind == "model":
            for m in (LATEST.get("models") or []):
                if (m.get("model") or "").lower() == name.lower():
                    lines.append("Model {n}: {v} MB VRAM resident, served by {s}.".format(
                        n=name, v=round(m.get("vram") or 0), s=m.get("service") or "?"))
                    srcs.append("model:" + name)
                    break
    except Exception:
        pass
    # per-entity month-to-date energy cost from power_proc (the get_entity_cost path)
    try:
        ctx = _cost_ctx()
        kwh_per = INTERVAL / 3_600_000.0
        lt = time.localtime(now)
        mstart = int(time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)))
        with LOCK:
            cur = DB.cursor()
            rows = cur.execute(
                "SELECT ts, watts FROM power_proc WHERE name=? AND ts>=?",
                (name, mstart)).fetchall()
        if rows:
            cost = 0.0
            for ts, w in rows:
                cost += (w or 0) * kwh_per * _price_at(ctx, ts)
            if ctx.get("day", 0) > 0 and cost > 0:
                lines.append("{n} cost month-to-date: {c}{v}.".format(
                    n=name, c=ctx["currency"], v=round(cost, 2)))
                if "cost" not in srcs:
                    srcs.append("cost")
    except Exception:
        pass
    # recent OOM event naming this entity
    try:
        with LOCK:
            cur = DB.cursor()
            row = cur.execute(
                "SELECT ts, detail FROM events WHERE service=? AND kind='oom' "
                "AND ts>=? ORDER BY ts DESC LIMIT 1",
                (name, now - 7 * 86400)).fetchone()
        if row:
            lines.append("Recent OOM kill touching {n}: {w}.".format(
                n=name, w=time.strftime("%Y-%m-%d %H:%M", time.localtime(row[0]))))
            if "events" not in srcs:
                srcs.append("events")
    except Exception:
        pass
    if not lines:
        lines.append("No live detail found for '{n}' right now.".format(n=name))
    return lines, srcs


def _ask_topic_facts(topics, q, ctx, now):
    """Compact fact slices for the detected topics, drawn from the same accessors
    powering the API/MCP layer (and the already-assembled generic `ctx`). Returns
    (lines, srcs). 'cost'+a rank hint pulls top-N entities by cost; 'gpu' pulls
    util/temp/power/VRAM + headroom; 'disk' pulls per-mount fill% + ETA; etc."""
    lines, srcs = [], []
    toks = _ask_tokens(q)
    wants_rank = bool(toks & set(_ASK_RANK_HINTS))

    if "gpu" in topics:
        g = ctx.get("gpu") or {}
        if g.get("available") is False:
            lines.append("No GPU detected on this host.")
        else:
            vt = g.get("vram_total_mb") or 0
            vpct = round(100 * (g.get("vram_used_mb") or 0) / vt) if vt else None
            lines.append("GPU now: {u}% util, {p} W, {t}°C, VRAM {vu}/{vt} MB{vp}.".format(
                u=g.get("util_pct", 0), p=g.get("power_w", 0), t=g.get("temp_c", 0),
                vu=g.get("vram_used_mb", 0), vt=vt,
                vp=(" (%d%%)" % vpct) if vpct is not None else ""))
            try:
                with LOCK:
                    vf = _vram_forecast(DB.cursor(), now)
                if vf.get("free_gb") is not None:
                    lines.append("GPU VRAM headroom: {f} GB free of {t} GB; trend {s}.".format(
                        f=vf["free_gb"], t=vf.get("total_gb"), s=vf.get("status")))
            except Exception:
                pass
        srcs.append("gpu")

    if "disk" in topics:
        try:
            with LOCK:
                disks = _disk_forecasts(DB.cursor(), now)
            filling = sorted(
                [d for d in disks if d.get("status") == "filling" and d.get("eta_days") is not None],
                key=lambda d: d["eta_days"])
            for d in filling[:3]:
                lines.append("Disk {m} is {p}% full; fills in ~{n} days ({f} GB free).".format(
                    m=d["mount"], p=d.get("pct"), n=d["eta_days"], f=d.get("free_gb")))
            if not filling:
                worst = sorted([d for d in disks if d.get("pct") is not None],
                               key=lambda d: -d["pct"])[:2]
                for d in worst:
                    lines.append("Disk {m} is {p}% full ({f} GB free); not currently filling.".format(
                        m=d["mount"], p=d.get("pct"), f=d.get("free_gb")))
            srcs.append("disk")
        except Exception:
            pass

    if "disk_io" in topics:
        n_before = len(lines)
        dio = HEALTH.get("disk_io") or {}
        if dio.get("available"):
            s = dio.get("summary") or {}
            items = dio.get("items") or []
            lines.append("Disk I/O now: {r} MB/s read, {w} MB/s write across {n} device(s).".format(
                r=s.get("total_read_mb_s", 0), w=s.get("total_write_mb_s", 0), n=len(items)))
            for it in items[:3]:
                lat = []
                if it.get("read_lat_ms")  is not None: lat.append("{} ms read".format(it["read_lat_ms"]))
                if it.get("write_lat_ms") is not None: lat.append("{} ms write".format(it["write_lat_ms"]))
                lines.append("{d}: {r} MB/s read, {w} MB/s write, {u}% util{l}.".format(
                    d=it.get("device"), r=it.get("read_mb_s"), w=it.get("write_mb_s"),
                    u=it.get("util_pct", 0), l=(" ("+", ".join(lat)+" latency)") if lat else ""))
            # Real per-process attribution when /proc/<pid>/io is readable: the
            # actual block-layer write/read leaders — the precise "what was writing
            # heavily" answer (cached read; no LLM call on this path).
            attr = ((HEALTH.get("processes") or {}).get("io")) or {}
            if attr.get("available"):
                tw, tr = attr.get("top_writer"), attr.get("top_reader")
                if tw:
                    lines.append("Heaviest writer right now: {n} at {r}.".format(
                        n=tw.get("name"), r=_fmt_bps(tw.get("write_b_s"))))
                if tr:
                    lines.append("Heaviest reader right now: {n} at {r}.".format(
                        n=tr.get("name"), r=_fmt_bps(tr.get("read_b_s"))))
            # who else might be driving it — top CPU consumers are the usual suspects
            # for heavy read/write bursts (a rough, honest proxy, not attribution).
            procs = ((HEALTH.get("processes") or {}).get("by_cpu") or [])[:3]
            if procs:
                lines.append("Busiest processes right now: " + ", ".join(
                    "{n} ({c}% CPU)".format(n=p.get("name"), c=p.get("cpu_pct")) for p in procs) + ".")
        elif dio.get("warming_up"):
            lines.append("Disk I/O monitoring is warming up — one more poll and per-device MB/s appear.")
        if len(lines) > n_before:
            srcs.append("disk_io")

    if "cost" in topics:
        n_before = len(lines)
        cm = ctx.get("cost_month") or {}
        if cm.get("enabled"):
            cur = cm.get("currency") or "$"
            lines.append("Energy cost month-to-date {c}{m}; projected month {c}{p}.".format(
                c=cur, m=cm.get("month_to_date"), p=cm.get("projected_month")))
        # top-N entities by cost when the question asks who's most expensive.
        # When no tariff is set we rank by energy (kWh) — an honest proxy so the
        # "most expensive" question still gets a grounded ranking answer.
        if wants_rank:
            try:
                top = _ask_top_cost_entities(now, n=5)
                if top and not top[0].get("priced"):
                    lines.append("No energy tariff is configured, so ranking by ENERGY USE (kWh) over the last 30 days:")
                for e in top:
                    if e.get("priced"):
                        lines.append("{k} {n}: {c}{v} over the last 30 days (~{w} W avg).".format(
                            k=e["kind"], n=e["name"], c=e["currency"], v=e["cost"], w=e["avg_w"]))
                    else:
                        lines.append("{k} {n}: {e} kWh over the last 30 days (~{w} W avg).".format(
                            k=e["kind"], n=e["name"], e=e["energy_kwh"], w=e["avg_w"]))
            except Exception:
                pass
        # only claim "cost" as a source if we actually injected a cost fact —
        # an empty cost topic must not surface a phantom "based on: cost" chip.
        if len(lines) > n_before:
            srcs.append("cost")

    if "memory" in topics:
        try:
            with LOCK:
                cur = DB.cursor()
                rows = cur.execute(
                    "SELECT service, MAX(mem) m FROM proc WHERE ts>=? "
                    "GROUP BY service ORDER BY m DESC LIMIT 3", (now - 600,)).fetchall()
                ooms = cur.execute(
                    "SELECT ts, service FROM events WHERE kind='oom' AND ts>=? "
                    "ORDER BY ts DESC LIMIT 2", (now - 7 * 86400,)).fetchall()
            if rows:
                top = ", ".join("{s} ({m} MB)".format(s=r[0], m=round(r[1] or 0)) for r in rows)
                lines.append("Top memory consumers now: " + top + ".")
            for ts, svc in ooms:
                lines.append("OOM kill: {s} at {w}.".format(
                    s=svc, w=time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))))
            srcs.append("memory")
        except Exception:
            pass

    if "incident" in topics:
        anoms = ctx.get("anomalies") or []
        for a in anoms[:2]:
            lines.append("Anomaly: {k} {d} — {v}{u} vs ~{b}{u} baseline ({z}σ).".format(
                k=a.get("key"), d=a.get("direction"), v=a.get("value"),
                u=a.get("unit"), b=a.get("baseline"), z=a.get("z")))
        if not anoms and ctx.get("anomaly_status") == "quiet":
            lines.append("No anomalies: all monitored series are within normal range.")
        srcs.append("anomalies")

    if "uptime" in topics:
        try:
            dock = (HEALTH.get("docker") or {})
            sysd = (HEALTH.get("systemd") or {})
            dsum = dock.get("summary") or {}
            ssum = sysd.get("summary") or {}
            bits = []
            if dock.get("available"):
                bits.append("{r}/{t} containers running".format(
                    r=dsum.get("running", 0), t=dsum.get("total", 0)))
            if sysd.get("available"):
                bits.append("{r} services running, {f} failed".format(
                    r=ssum.get("running", 0), f=ssum.get("failed", 0)))
            if bits:
                lines.append("Availability: " + "; ".join(bits) + ".")
                srcs.append("uptime")
        except Exception:
            pass

    if "container" in topics:
        try:
            probs = []
            for c in ((HEALTH.get("docker") or {}).get("containers") or []):
                if c.get("status") in ("crit", "warn"):
                    probs.append("{n} ({l})".format(n=c.get("name"), l=c.get("label") or c.get("status")))
            for s in ((HEALTH.get("systemd") or {}).get("services") or []):
                if s.get("status") in ("crit", "warn"):
                    probs.append("{n} ({l})".format(n=s.get("name"), l=s.get("label") or s.get("status")))
            if probs:
                lines.append("Containers/services needing attention: " + ", ".join(probs[:4]) + ".")
            else:
                lines.append("All containers and services are healthy.")
            srcs.append("health")
        except Exception:
            pass

    return lines, srcs


def _ask_top_cost_entities(now, n=5):
    """Top-N power-attributed entities over the last 30 days from power_proc (the
    same table get_costs ranks). When a tariff is configured we rank by money;
    otherwise we rank by ENERGY (kWh) so 'most expensive' still gets an honest
    proxy answer on labs with no price set. Read-only. Returns a list of
    {kind,name,cost,energy_kwh,avg_w,currency,priced}. Never raises (empty on
    any error)."""
    out = []
    try:
        ctx = _cost_ctx()
        priced = ctx.get("day", 0) > 0
        kwh_per = INTERVAL / 3_600_000.0
        since = now - 30 * 86400
        acc = {}   # (kind,name) -> [cost, energy_kwh, watt_sum, cnt]
        with LOCK:
            cur = DB.cursor()
            for ts, kind, name, watts in cur.execute(
                    "SELECT ts,kind,name,watts FROM power_proc WHERE ts>=?", (since,)):
                a = acc.setdefault((kind, name), [0.0, 0.0, 0.0, 0])
                w = watts or 0
                a[0] += w * kwh_per * _price_at(ctx, ts)
                a[1] += w * kwh_per
                a[2] += w
                a[3] += 1
        rank_idx = 0 if priced else 1     # by cost when priced, else by energy
        ranked = sorted(acc.items(), key=lambda kv: -kv[1][rank_idx])[:n]
        for (kind, name), (cost, energy, wsum, cnt) in ranked:
            if (cost if priced else energy) <= 0:
                continue
            out.append({"kind": kind, "name": name, "cost": round(cost, 2),
                        "energy_kwh": round(energy, 3),
                        "avg_w": round(wsum / max(1, cnt)),
                        "currency": ctx["currency"], "priced": priced})
    except Exception:
        return []
    return out


def _ask_route(question, now=None):
    """Deterministic retrieval/routing for the ask-box. Detects entities + topics
    in the question, pulls a compact relevant fact slice from the live accessors,
    and bounds the result. Returns (facts, sources, used) where `used` is a small
    transparency list (e.g. ['container:chroma','cost','anomalies']). On no match,
    returns ([], [], []) so the caller falls back to the generic digest context.
    Pure reads; the heavy `ctx` is built once and shared. Never raises."""
    now = now or int(time.time())
    facts, srcs = [], []
    try:
        ents = _ask_live_entities()
        matched = _ask_match_entities(question, ents)
        topics = _ask_detect_topics(question)
        # generic ctx is reused by several topic slices (gpu/cost/anomalies)
        ctx = _copilot_context(now)
        for e in matched:
            ef, es = _ask_entity_facts(e["name"], e["kind"], now)
            facts.extend(ef)
            srcs.extend(es)
        if topics:
            tf, ts_ = _ask_topic_facts(topics, question, ctx, now)
            facts.extend(tf)
            srcs.extend(ts_)
    except Exception as e:
        print("copilot ask route error:", e, flush=True)
        return [], [], []
    # de-dup sources preserving order
    used, seen = [], set()
    for s in srcs:
        if s and s not in seen:
            seen.add(s)
            used.append(s)
    facts = _ask_bound_facts(facts)
    return facts, used, used


def _ask_bound_facts(facts):
    """Enforce the hard prompt-size bounds: clip each line, cap line count, and
    cap the total character budget so a big lab can't make the prompt huge."""
    out, total = [], 0
    for f in facts:
        if not f:
            continue
        f = f if len(f) <= _ASK_MAX_LINE else (f[:_ASK_MAX_LINE - 1] + "…")
        if len(out) >= _ASK_MAX_FACTS:
            break
        if total + len(f) > _ASK_MAX_CHARS:
            break
        out.append(f)
        total += len(f)
    return out


@app.route("/api/copilot/ask", methods=["POST"])
def api_copilot_ask():
    """Free-text question answered by the local LLM over the lab's OWN live data,
    routed to the question. A deterministic (LLM-free) routing step pulls the
    relevant fact slice from the live accessors; on no match it falls back to the
    generic digest context (never worse than before). Always 200; graceful
    `llm_status` + the routed facts when the LLM can't answer. `sources` names
    what data informed the answer."""
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    question = (payload.get("question") or "").strip()
    now = int(time.time())
    if not question:
        return jsonify({"now": now, "answer": "", "source": "none",
                        "llm_status": "no_question", "model": COPILOT_MODEL,
                        "sources": []})
    if len(question) > 500:
        question = question[:500]
    # Deterministic retrieval: route the question to the relevant live facts.
    routed, sources, used = _ask_route(question, now)
    if routed:
        facts = routed
        routing = "live"
    else:
        # No specific entity/topic detected → generic digest context (fallback).
        facts = _copilot_facts(_copilot_context(now))
        sources = used = ["digest"]
        routing = "generic"
    out = {"now": now, "model": COPILOT_MODEL, "question": question,
           "facts": facts, "sources": used, "routing": routing,
           "enabled": COPILOT_ENABLED}
    _cap = []
    # Structured (JSON-mode) ask: typed {answer,severity,action} that powers the
    # severity chip + suggested-action CTA (same surface as "Explain this spike").
    # Falls back to plain prose internally when the small model ignores the schema,
    # so this is never worse than prose. ONE structured call on the hot path.
    res, err = _ask_structured(facts, question, capture=_cap)
    if res is not None:
        out.update({"answer": res["answer"], "source": "llm", "llm_status": "ok"})
        if res.get("severity"):
            out["severity"] = res["severity"]
        if res.get("action"):
            out["action"] = res["action"]
        inf = _inference_cost(_cap[0] if _cap else None, now)
        if inf:
            out["inference"] = inf
    else:
        # No LLM: hand back the routed facts as a readable summary so the box is
        # still useful (never a dead end), with the same sources.
        out.update({"answer": "", "facts_summary": " ".join(facts),
                    "source": "facts", "llm_status": err})
    return jsonify(out)


def _sse(event, data):
    """Format one Server-Sent Event frame. `data` is JSON-encoded; the SSE
    `event:` name lets the client switch on token vs done vs error."""
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))


@app.route("/api/copilot/ask/stream", methods=["POST"])
def api_copilot_ask_stream():
    """Streaming sibling of /api/copilot/ask (SSE). Runs the SAME deterministic
    routing (`_ask_route` → bounded live facts), then streams the local LLM's
    answer token-by-token. Additive: the non-stream endpoint is the graceful
    fallback the UI drops to on any stream failure.

    SSE events:
      • event: token   data: {"t": "<chunk>"}          (0..N, as they arrive)
      • event: done    data: {"answer","source":"llm","llm_status":"ok",
                              "sources","routing","facts","model"}
      • event: error   data: {"source":"facts","llm_status":<code>,
                              "facts_summary","sources","routing","facts","model"}
    Always one terminal event (done OR error); never hangs, never 500s once the
    stream has begun. Facts only — no settings/URLs/tokens are streamed. The
    heavy routing happens BEFORE the generator body so we never hold any lock
    across the stream."""
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    question = (payload.get("question") or "").strip()
    now = int(time.time())
    if not question:
        # No body to stream — emit a single terminal error frame.
        def _empty():
            yield _sse("error", {"source": "none", "llm_status": "no_question",
                                 "model": COPILOT_MODEL, "sources": [],
                                 "routing": "none", "facts": [], "facts_summary": ""})
        return Response(stream_with_context(_empty()), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    if len(question) > 500:
        question = question[:500]
    # Deterministic retrieval (reused, same as /api/copilot/ask) — done here,
    # OUTSIDE the generator, so no lock is held while tokens stream.
    routed, sources, used = _ask_route(question, now)
    if routed:
        facts = routed
        routing = "live"
    else:
        facts = _copilot_facts(_copilot_context(now))
        used = ["digest"]
        routing = "generic"
    prompt = _copilot_ask_prompt(facts, question)

    def gen():
        acc = []
        try:
            for kind, val in _ollama_generate_stream(prompt):
                if kind == "token":
                    acc.append(val)
                    yield _sse("token", {"t": val})
                elif kind == "done":
                    done = {
                        "answer": val.get("text") or "".join(acc),
                        "source": "llm", "llm_status": "ok",
                        "sources": used, "routing": routing,
                        "facts": facts, "model": COPILOT_MODEL}
                    inf = _inference_cost(val.get("metrics"), now)
                    if inf:
                        done["inference"] = inf
                    yield _sse("done", done)
                    return
                elif kind == "error":
                    # LLM unavailable (at start or mid-stream): hand back the
                    # routed facts as a readable summary, same as the non-stream
                    # path. Never 500 — a terminal error frame, then close.
                    yield _sse("error", {
                        "source": "facts", "llm_status": val,
                        "facts_summary": " ".join(facts),
                        "sources": used, "routing": routing,
                        "facts": facts, "model": COPILOT_MODEL})
                    return
        except Exception as e:
            # Belt-and-suspenders: the generator must never raise out of the
            # stream. Emit a terminal facts frame and close.
            print("copilot ask stream gen error:", type(e).__name__, flush=True)
            try:
                yield _sse("error", {
                    "source": "facts", "llm_status": "unreachable",
                    "facts_summary": " ".join(facts),
                    "sources": used, "routing": routing,
                    "facts": facts, "model": COPILOT_MODEL})
            except Exception:
                pass
            return
        # The stream ended without a done/error terminal (shouldn't happen) —
        # emit a facts terminal so the client never waits forever.
        yield _sse("error", {
            "source": "facts", "llm_status": "bad_response",
            "facts_summary": " ".join(facts),
            "sources": used, "routing": routing,
            "facts": facts, "model": COPILOT_MODEL})

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Copilot: "Explain this spike" ─────────────────────────────────────────────
# Given an anomaly/point (series key + when), assemble a tight, deterministic
# context — the series' own before/after movement plus what was running on the
# GPU at that moment (models / top processes / power-attributed entities) — and
# ask the local LLM for a 1–2 sentence plain-English likely cause. Read-only,
# bounded, always 200 with a graceful state when the LLM is unavailable.

# Map an anomaly series key to its underlying samples column / SQL expression.
_EXPLAIN_COLS = {k: (col, unit) for (k, col, unit, _md) in _ANOMALY_SERIES}


def _explain_window(cur, key, now, ts):
    """Series-local before/after movement around `ts`: the value at the point,
    the mean of the ~10 min before it and the ~5 min after, so the LLM can see
    whether the series jumped and stayed or just blipped. Returns a dict or {}.
    Never raises."""
    spec = _EXPLAIN_COLS.get(key)
    if not spec:
        return {}
    col, unit = spec
    try:
        before = cur.execute(
            f"SELECT AVG({col}) FROM samples WHERE ts>=? AND ts<? AND {col} IS NOT NULL",
            (ts - 600, ts)).fetchone()[0]
        after = cur.execute(
            f"SELECT AVG({col}) FROM samples WHERE ts>? AND ts<=? AND {col} IS NOT NULL",
            (ts, ts + 300)).fetchone()[0]
        at = cur.execute(
            f"SELECT {col} FROM samples WHERE {col} IS NOT NULL ORDER BY ABS(ts-?) LIMIT 1",
            (ts,)).fetchone()
    except Exception:
        return {}
    out = {"unit": unit}
    if at is not None:
        out["at"] = round(at[0], 1)
    if before is not None:
        out["before_10m"] = round(before, 1)
    if after is not None:
        out["after_5m"] = round(after, 1)
    return out


def _explain_running(cur, ts):
    """What was on the GPU around `ts`: biggest resident models, top processes by
    RAM, and the heaviest power-attributed entities — each picked from the
    sample row(s) nearest the anomaly time. Returns a dict of short lists. Never
    raises (any missing table/data is just an empty list)."""
    lo, hi = ts - 60, ts + 60
    models, procs, power = [], [], []
    try:
        for svc, model, vram in cur.execute(
                "SELECT service, model, MAX(vram) v FROM models "
                "WHERE ts>=? AND ts<=? AND vram IS NOT NULL "
                "GROUP BY service, model ORDER BY v DESC LIMIT 3", (lo, hi)):
            models.append({"service": svc, "model": model, "vram_mb": round(vram or 0)})
    except Exception:
        pass
    try:
        for svc, mem in cur.execute(
                "SELECT service, MAX(mem) m FROM proc WHERE ts>=? AND ts<=? "
                "GROUP BY service ORDER BY m DESC LIMIT 3", (lo, hi)):
            procs.append({"service": svc, "mem_mb": round(mem or 0)})
    except Exception:
        pass
    try:
        for kind, name, watts in cur.execute(
                "SELECT kind, name, MAX(watts) w FROM power_proc WHERE ts>=? AND ts<=? "
                "GROUP BY kind, name ORDER BY w DESC LIMIT 3", (lo, hi)):
            power.append({"kind": kind, "name": name, "watts": round(watts or 0, 1)})
    except Exception:
        pass
    return {"models": models, "procs": procs, "power": power}


def _proc_io_at(ts, window=120, limit=3):
    """Spike-time per-process I/O attribution from the persisted `proc_io_samples`
    ring: the heaviest writer(s)/reader(s) whose samples fall in the anomaly window
    [ts-window, ts+window]. Returns {"available": True, "writers": [...],
    "readers": [...]} (each item {"name", "read_b_s"/"write_b_s"} — the SAME shape
    as the live attribution so the explainer consumes both uniformly), or None when
    no history covers the window (caller then falls back to the live leader). Comm
    only, bounded/windowed query, pure DB read — never triggers an LLM, never
    raises. NOT exposed on any public surface."""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return None
    lo, hi = ts - window, ts + window
    writers, readers = [], []
    try:
        with LOCK:
            cur = DB.cursor()
            for comm, w in cur.execute(
                    "SELECT comm, MAX(write_bps) w FROM proc_io_samples "
                    "WHERE ts>=? AND ts<=? AND write_bps>0 "
                    "GROUP BY comm ORDER BY w DESC LIMIT ?", (lo, hi, limit)):
                writers.append({"name": comm, "write_b_s": int(w or 0)})
            for comm, r in cur.execute(
                    "SELECT comm, MAX(read_bps) r FROM proc_io_samples "
                    "WHERE ts>=? AND ts<=? AND read_bps>0 "
                    "GROUP BY comm ORDER BY r DESC LIMIT ?", (lo, hi, limit)):
                readers.append({"name": comm, "read_b_s": int(r or 0)})
    except Exception:
        return None
    if not writers and not readers:
        return None
    return {"available": True, "writers": writers, "readers": readers}


def _incident_spike_io(inc, now=None):
    """Deterministic spike-time disk-I/O attribution for an incident *read* payload
    (the detail drawer's "who was writing DURING the spike" line). If a disk_io
    series is among the incident's members, join the persisted per-process ring at
    the incident's most-recent activity via `_proc_io_at` and surface the heaviest
    writer + heaviest reader (comm only, each pre-formatted). Pure cached DB read —
    NEVER an LLM call, never a poll. Bounded to the top writer + top reader. Returns
    {"available": True, "writer"?: {...}, "reader"?: {...}} or None when there's no
    disk_io member / no history covers the window / the ring is unreadable — the
    caller then simply omits the field (drawer unchanged). comm only; NOT exposed on
    any public surface. Never raises."""
    try:
        members = (inc or {}).get("members") or []
        if not any(str(m.get("series") or "").startswith("disk_io") for m in members):
            return None
        now = now or int(time.time())
        anchor = max([m.get("last_seen") or 0 for m in members]
                     + [inc.get("updated_at") or 0, inc.get("opened_at") or 0, 0]) or now
        anchor = max(min(int(anchor), now), now - 30 * 24 * 3600)
        hist = _proc_io_at(anchor)
        if not (hist and hist.get("available")):
            return None
        out = {"available": True}
        w = hist.get("writers") or []
        r = hist.get("readers") or []
        if w:
            wb = int(w[0].get("write_b_s") or 0)
            out["writer"] = {"name": w[0].get("name"), "write_b_s": wb,
                             "write_h": _fmt_bps(wb)}
        if r:
            rb = int(r[0].get("read_b_s") or 0)
            out["reader"] = {"name": r[0].get("name"), "read_b_s": rb,
                             "read_h": _fmt_bps(rb)}
        if "writer" not in out and "reader" not in out:
            return None
        return out
    except Exception:
        return None


def _explain_context(point, now=None):
    """Assemble the full deterministic context for an 'explain this spike' call
    from a (sanitised) anomaly/point payload. Pure reads; never raises."""
    now = now or int(time.time())
    key = str(point.get("key") or point.get("series") or "").strip()
    try:
        ts = int(point.get("ts") or point.get("timestamp") or now)
    except (TypeError, ValueError):
        ts = now
    # clamp ts to a sane window: not in the future, not absurdly old
    ts = max(min(ts, now), now - 30 * 24 * 3600)
    ctx = {"now": now, "ts": ts, "key": key,
           "label": _EXPLAIN_COLS.get(key, (None, None))[0] or key,
           "unit": point.get("unit") or (_EXPLAIN_COLS.get(key, (None, ""))[1]),
           "direction": point.get("direction"),
           "value": point.get("value"), "baseline": point.get("baseline"),
           "z": point.get("z"), "magnitude": point.get("magnitude")}
    # Disk-I/O anomaly keys ("disk_io:<dev>") aren't a `samples` column, so give
    # the explainer the live per-device snapshot + busiest processes instead of an
    # empty series window — the top-processes context the ask path also uses.
    if key.startswith("disk_io"):
        dev = key.split(":", 1)[1] if ":" in key else point.get("device")
        ctx["label"] = "disk I/O" + (" on " + dev if dev else "")
        ctx["unit"] = ctx["unit"] or "MB/s"
        try:
            dio = HEALTH.get("disk_io") or {}
            snap = next((it for it in (dio.get("items") or []) if it.get("device") == dev), None)
            ctx["disk_io"] = snap
            ctx["disk_io_summary"] = dio.get("summary")
            ctx["busy_procs"] = ((HEALTH.get("processes") or {}).get("by_cpu") or [])[:3]
            # Spike-time attribution: prefer the PERSISTED ring joined to the
            # anomaly window ("who was writing AT the spike") over the live "now"
            # leaders — a disk_io spike explained later can then still name the
            # historical writer. Pure DB read; never triggers an LLM call. Falls
            # back to the live attribution when no history covers the window.
            _hist = _proc_io_at(ts)
            if _hist and _hist.get("available"):
                ctx["io_writers"] = _hist.get("writers") or []
                ctx["io_readers"] = _hist.get("readers") or []
                ctx["io_historical"] = True
            else:
                _attr = ((HEALTH.get("processes") or {}).get("io")) or {}
                if _attr.get("available"):
                    # Real per-process leaders (comm only) — precise I/O attribution
                    # for the explainer; cached read, never triggers an LLM call.
                    ctx["io_writers"] = _attr.get("writers") or []
                    ctx["io_readers"] = _attr.get("readers") or []
        except Exception:
            pass
    try:
        with LOCK:
            cur = DB.cursor()
            ctx["window"] = _explain_window(cur, key, now, ts)
            ctx["running"] = _explain_running(cur, ts)
    except Exception as e:
        print("copilot explain ctx error:", e, flush=True)
        ctx.setdefault("window", {})
        ctx.setdefault("running", {})
    return ctx


# Human-readable series names for the deterministic explain facts (kept in sync
# with the UI's anom.series.* i18n keys; this is the LLM-grounding / fallback).
_EXPLAIN_LABELS = {
    "gpu_util": "GPU utilisation", "gpu_vram": "GPU VRAM used",
    "gpu_power": "GPU power draw", "gpu_temp": "GPU temperature",
    "power_draw": "total system power draw",
}


def _explain_facts(c):
    """Terse, deterministic English describing the spike and its surroundings.
    Doubles as the LLM grounding and the no-LLM fallback summary."""
    lines = []
    key = c.get("key") or ""
    name = _EXPLAIN_LABELS.get(key, key or "a monitored series")
    unit = c.get("unit") or ""
    direction = c.get("direction") or "change"
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(c.get("ts") or 0))
    val, base, z = c.get("value"), c.get("baseline"), c.get("z")
    head = "At {when}, {name} showed a {dir}".format(when=when, name=name, dir=direction)
    if val is not None and base is not None:
        head += ": {v}{u} versus a ~{b}{u} baseline".format(v=val, u=unit, b=base)
    if z is not None:
        head += " ({z}σ)".format(z=("+%s" % z) if (isinstance(z, (int, float)) and z > 0) else z)
    lines.append(head + ".")
    w = c.get("window") or {}
    if w.get("before_10m") is not None and w.get("at") is not None:
        tail = " It then settled to {a}{u} over the next 5 min.".format(
            a=w["after_5m"], u=w.get("unit") or unit) if w.get("after_5m") is not None else ""
        lines.append("The 10 min before it averaged {b}{u}; at the point it read {a}{u}.{t}".format(
            b=w["before_10m"], u=w.get("unit") or unit, a=w["at"], t=tail))
    r = c.get("running") or {}
    if r.get("models"):
        m = r["models"][0]
        lines.append("Largest model on the GPU then: {mo} ({mb} MB, served by {s}).".format(
            mo=m.get("model") or "?", mb=m.get("vram_mb") or 0, s=m.get("service") or "?"))
    if r.get("procs"):
        top = ", ".join("{s} ({m} MB)".format(s=p.get("service"), m=p.get("mem_mb"))
                        for p in r["procs"][:2])
        lines.append("Top processes by memory then: " + top + ".")
    if r.get("power"):
        p = r["power"][0]
        lines.append("Heaviest power-attributed entity then: {n} (~{w} W).".format(
            n=p.get("name"), w=p.get("watts")))
    # Real per-process disk-I/O attribution (comm only) for disk_io spikes — the
    # precise "what was writing/reading heavily" leaders. When it comes from the
    # persisted ring joined to the anomaly window it names who was writing DURING
    # the spike (historical); otherwise it's the live "now" leader.
    _hist = c.get("io_historical")
    _when = "during the spike" if _hist else "then"
    iw = (c.get("io_writers") or [])
    if iw:
        lines.append("Heaviest writer {w}: {n} ({r}).".format(
            w=_when, n=iw[0].get("name"), r=_fmt_bps(iw[0].get("write_b_s"))))
    ir = (c.get("io_readers") or [])
    if ir:
        lines.append("Heaviest reader {w}: {n} ({r}).".format(
            w=_when, n=ir[0].get("name"), r=_fmt_bps(ir[0].get("read_b_s"))))
    if len(lines) == 1:
        lines.append("No surrounding samples or running-process detail is available for that moment.")
    return lines


def _explain_prompt(facts):
    return (
        "You are the Lab Copilot for a self-hosted homelab monitoring dashboard. "
        "A metric just deviated from its normal range. Using ONLY the facts below "
        "(live readings from the lab around that moment — do not invent anything), "
        "explain the most likely cause in 1-2 short sentences of plain English. "
        "No markdown, no bullet points. If the facts don't point to a clear cause, "
        "say it's unclear and name what would help.\n\n"
        "FACTS:\n- " + "\n- ".join(facts) + "\n\nLIKELY CAUSE:")


# JSON-Schema for ollama structured output — a typed explain object. ollama
# accepts a JSON-Schema in the request's `format` field and constrains decoding
# to it (small models like gemma3:1b honour it best-effort, hence the prose
# fallback in `_explain_structured`).
EXPLAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "explanation": {"type": "string"},
        "severity": {"type": "string", "enum": ["info", "warning", "critical"]},
        "action": {"type": "string"},
    },
    "required": ["explanation", "severity"],
}
_EXPLAIN_SEVERITIES = ("info", "warning", "critical")


def _explain_structured_prompt(facts):
    return (
        "You are the Lab Copilot for a self-hosted homelab monitoring dashboard. "
        "A metric just deviated from its normal range. Using ONLY the facts below "
        "(live readings from the lab around that moment — do not invent anything), "
        "return a JSON object with: \"explanation\" (1-2 short plain-English "
        "sentences naming the most likely cause; say it's unclear if the facts "
        "don't point to one), \"severity\" (one of \"info\", \"warning\", "
        "\"critical\" — how concerning this looks), and \"action\" (one short, "
        "concrete next step the operator could take, or \"\" if none). No "
        "markdown.\n\n"
        "FACTS:\n- " + "\n- ".join(facts) + "\n")


def _explain_structured(facts, capture=None):
    """Ask the LLM for a TYPED explain object via ollama JSON mode, parse it
    defensively, and return (dict, error). On success the dict is
    {'explanation','severity','action'} with severity clamped to the allowed
    set and action either a non-empty string or None.

    Graceful degrade: if the LLM is down/unreachable, or returns no text, or the
    text isn't valid JSON, or lacks a usable explanation (small models may ignore
    the schema), we fall back to the EXISTING plain-prose explain so the answer is
    never worse than today. Returns (None, error_code) only when even the prose
    fallback yields nothing (LLM fully down) — the caller then uses the
    deterministic facts. Never raises. `capture`, when a list, receives exactly
    one metrics dict — the one for the call whose text we actually return — so the
    per-inference cost chip stays accurate across the structured→prose fallback."""
    _scap = []
    text, err = _ollama_generate(
        _explain_structured_prompt(facts), capture=_scap, fmt=EXPLAIN_SCHEMA)
    if text is not None:
        try:
            obj = json.loads(text)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            expl = obj.get("explanation")
            if isinstance(expl, str) and expl.strip():
                sev = obj.get("severity")
                if not (isinstance(sev, str) and sev.lower() in _EXPLAIN_SEVERITIES):
                    sev = "info"
                else:
                    sev = sev.lower()
                act = obj.get("action")
                if not (isinstance(act, str) and act.strip()):
                    act = None
                else:
                    act = act.strip()
                if isinstance(capture, list):
                    capture.append(_scap[0] if _scap else None)
                return {"explanation": expl.strip(), "severity": sev,
                        "action": act}, None
        # Got text but it wasn't usable structured JSON → prose fallback below.
    # Either the LLM is down (err set) or it ignored the schema → plain prose.
    _pcap = []
    ptext, perr = _ollama_generate(
        _explain_prompt(facts), capture=_pcap)
    if ptext is not None:
        if isinstance(capture, list):
            capture.append(_pcap[0] if _pcap else None)
        return {"explanation": ptext, "severity": None, "action": None}, None
    return None, (err or perr or "unreachable")


# ── Incident-level Copilot explanation (E1): persisted, proactive ─────────────
# A short plain-English probable-cause + suggested next step for a WHOLE correlated
# incident (vs the per-member "Why?"). Generated ONLY from explicit events (the
# Explain/Regenerate action, a drawer first-open one-shot) or the dedicated
# auto-explain worker — NEVER on the poll path. Once generated it is PERSISTED on
# the incident row, so every incident READ (list/drawer) returns it with zero LLM
# calls. Series keys only — no check targets / URLs / credentials ever enter the
# prompt context or the stored text (these incidents never reach the public /status
# surface, which is built from uptime_results, not the correlated-incidents table).
_INCIDENT_EXPLAIN_MAX = 1000   # hard cap on the persisted explanation length (chars)


def _incident_explain_context(inc, now=None):
    """Deterministic grounding for an incident-level explanation, assembled purely
    from the incident's own members + what was running on the GPU/host around its
    most-recent activity. The DB LOCK is taken for the read then RELEASED here — no
    LLM call happens under lock. Never raises."""
    now = now or int(time.time())
    members = inc.get("members") or []
    # anchor the "what was running" snapshot on the incident's most-recent activity
    anchor = max([m.get("last_seen") or 0 for m in members]
                 + [inc.get("updated_at") or 0, inc.get("opened_at") or 0, 0]) or now
    anchor = max(min(int(anchor), now), now - 30 * 24 * 3600)
    ctx = {"now": now, "id": inc.get("id"), "severity": inc.get("severity"),
           "state": inc.get("state"), "opened_at": inc.get("opened_at"),
           "cleared_at": inc.get("cleared_at"), "member_count": len(members),
           "active_count": sum(1 for m in members if m.get("active")),
           "members": members, "anchor": anchor}
    try:
        with LOCK:
            cur = DB.cursor()
            ctx["running"] = _explain_running(cur, anchor)
    except Exception as e:
        print("incident explain ctx error:", e, flush=True)
        ctx["running"] = {}
    # If a disk_io series is among the members, join the persisted per-process I/O
    # ring at the incident anchor so the explanation can name who was writing AT
    # the spike (historical). Pure DB read; no LLM. Absent history → simply omitted.
    if any(str(m.get("series") or "").startswith("disk_io") for m in members):
        _hist = _proc_io_at(anchor)
        if _hist and _hist.get("available"):
            ctx["io_writers"] = _hist.get("writers") or []
            ctx["io_readers"] = _hist.get("readers") or []
            ctx["io_historical"] = True
    return ctx


def _incident_explain_facts(ctx):
    """Terse, deterministic English describing a whole incident + its surroundings.
    Doubles as the LLM grounding and the no-LLM fallback. Telemetry series keys
    only — never a check target, URL or credential."""
    lines = []
    sev = ctx.get("severity") or "warning"
    state = ctx.get("state") or "open"
    n, a = ctx.get("member_count") or 0, ctx.get("active_count") or 0
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ctx.get("opened_at") or 0))
    lines.append(
        "A {sev} correlated incident is {state}: {n} signal(s) fired together, "
        "opened {when}, {a} still active.".format(sev=sev, state=state, n=n, when=when, a=a))
    for m in (ctx.get("members") or [])[:6]:
        name = _EXPLAIN_LABELS.get(m.get("series") or "", m.get("series") or "a series")
        direction = m.get("direction") or "change"
        u = m.get("unit") or ""
        z = m.get("peak_z")
        seg = "{name}: {dir}".format(name=name, dir=direction)
        if m.get("peak_value") is not None and m.get("baseline") is not None:
            seg += " to {v}{u} (baseline ~{b}{u})".format(v=m.get("peak_value"), u=u, b=m.get("baseline"))
        if isinstance(z, (int, float)):
            seg += " at {z}σ".format(z=round(abs(z), 1))
        seg += "; " + ("still active" if m.get("active") else "settled") + "."
        lines.append(seg)
    r = ctx.get("running") or {}
    if r.get("models"):
        mo = r["models"][0]
        lines.append("Largest model on the GPU then: {mo} ({mb} MB, served by {s}).".format(
            mo=mo.get("model") or "?", mb=mo.get("vram_mb") or 0, s=mo.get("service") or "?"))
    if r.get("procs"):
        top = ", ".join("{s} ({m} MB)".format(s=p.get("service"), m=p.get("mem_mb"))
                        for p in r["procs"][:2])
        lines.append("Top processes by memory then: " + top + ".")
    if r.get("power"):
        p = r["power"][0]
        lines.append("Heaviest power-attributed entity then: {n} (~{w} W).".format(
            n=p.get("name"), w=p.get("watts")))
    # Spike-time per-process disk-I/O attribution (comm only) from the persisted
    # ring, when a disk_io member fired — "who was writing DURING the spike".
    iw = (ctx.get("io_writers") or [])
    if iw:
        lines.append("Heaviest writer during the spike: {n} ({r}).".format(
            n=iw[0].get("name"), r=_fmt_bps(iw[0].get("write_b_s"))))
    ir = (ctx.get("io_readers") or [])
    if ir:
        lines.append("Heaviest reader during the spike: {n} ({r}).".format(
            n=ir[0].get("name"), r=_fmt_bps(ir[0].get("read_b_s"))))
    return lines


def _sanitize_explanation(text):
    """Bound + normalise an LLM explanation before persisting: collapse whitespace,
    hard-cap the length. Rendering is XSS-escaped at the UI; this keeps the stored
    blob small and clean. Returns None for empty/garbage input."""
    if not isinstance(text, str):
        return None
    t = " ".join(text.split())
    if not t:
        return None
    return t[:_INCIDENT_EXPLAIN_MAX]


def _store_incident_explanation(iid, text, model, now):
    """Persist a generated explanation on the incident row (bounded). Returns True
    on write. Never raises."""
    try:
        with LOCK:
            DB.execute("UPDATE incidents SET ai_explanation=?, ai_explained_at=?, ai_model=? WHERE id=?",
                       (text, now, model, iid))
            DB.commit()
        return True
    except Exception as e:
        print("store incident explanation error:", e, flush=True)
        try: DB.rollback()
        except Exception: pass
        return False


def generate_incident_explanation(iid, force=False, now=None):
    """THE ONLY writer of the incident ai_explanation columns. Reachable purely
    from explicit events — the /api/incidents/<id>/explain endpoint (Explain /
    Regenerate / drawer first-open one-shot) and the dedicated auto-explain worker
    — NEVER from collect()/health_scan()/the sample loop.

    • Unknown incident            → {"llm_status":"unknown"}, no write.
    • Cache present and not force  → returns the cache, ZERO LLM call.
    • force OR no cache            → assembles context under LOCK, RELEASES it, then
      calls the model (structured explain, no lock held). On success persists the
      bounded text + model + ts and returns it. LLM down → returns the status and
      leaves any existing cache intact (graceful no-op, nothing persisted).
    Never raises."""
    now = int(now if now is not None else time.time())
    inc = get_incident(iid)          # cached read — zero LLM
    if inc is None:
        return {"id": iid, "llm_status": "unknown", "cached": False, "source": "none"}
    if inc.get("ai_explanation") and not force:
        return {"id": iid, "explanation": inc["ai_explanation"], "cached": True,
                "source": "llm", "llm_status": "ok", "ai_model": inc.get("ai_model"),
                "ai_explained_at": inc.get("ai_explained_at"), "severity": inc.get("severity")}
    ctx = _incident_explain_context(inc, now)     # LOCK taken + released inside
    facts = _incident_explain_facts(ctx)
    _cap = []
    res, err = _explain_structured(facts, capture=_cap)   # NO lock held across this
    if res is None:
        # LLM off/unreachable — graceful no-op: keep any prior cache, report status.
        out = {"id": iid, "cached": False, "source": "facts",
               "llm_status": err or "unreachable", "facts": facts,
               "severity": inc.get("severity")}
        if inc.get("ai_explanation"):
            out.update({"explanation": inc["ai_explanation"], "source": "llm",
                        "ai_model": inc.get("ai_model"),
                        "ai_explained_at": inc.get("ai_explained_at")})
        return out
    # Compose the bounded persisted blob: probable cause + optional next step.
    text = res["explanation"]
    if res.get("action"):
        text = text + " Suggested next step: " + res["action"]
    text = _sanitize_explanation(text)
    _store_incident_explanation(iid, text, COPILOT_MODEL, now)
    out = {"id": iid, "explanation": text, "cached": False, "source": "llm",
           "llm_status": "ok", "ai_model": COPILOT_MODEL, "ai_explained_at": now,
           "severity": res.get("severity") or inc.get("severity"), "action": res.get("action")}
    inf = _inference_cost(_cap[0] if _cap else None, now)
    if inf:
        out["inference"] = inf
    return out


# ── AI incident postmortem (E1) — the capstone of the incidents+AI thread ─────
# When a correlated incident RESOLVES (state→'cleared'), the dashboard can write
# its own short, structured postmortem: a deterministic timeline + duration +
# member list (from the DB — never the LLM) plus LLM-composed prose for probable
# cause / impact / recommended action. Generated ONLY from an explicit user action
# (POST/?generate=1) or the dedicated off-poll postmortem worker — NEVER on any
# poll path. Persisted at-most-once via an atomic claim of postmortem_at, so a
# resolution never triggers a generation storm. Regeneration is explicit only.
# Graceful degrade: LLM off/garbage → no prose, but the deterministic skeleton
# (timeline/duration/members) still renders. Series keys only — NEVER a check
# target / URL / credential — so it stays consistent with the incidents' privacy
# contract (these never reach the public /status surface).
_POSTMORTEM_FIELD_MAX = 600     # hard cap per LLM prose field (chars)

# JSON-Schema for the structured postmortem prose. The deterministic facts
# (timeline/duration/members) come from the DB, NOT the model — the model only
# composes the three prose fields from the grounding facts.
POSTMORTEM_SCHEMA = {
    "type": "object",
    "properties": {
        "probable_cause": {"type": "string"},
        "impact": {"type": "string"},
        "recommended_action": {"type": "string"},
    },
    "required": ["probable_cause", "impact", "recommended_action"],
}


def _incident_postmortem_facts_deterministic(inc):
    """The DETERMINISTIC half of a postmortem, derived purely from the incident row
    — never the LLM. Timeline (opened→member joins→cleared), duration (opened→
    cleared), severity, and the member list. Re-derived on every read so it can
    never drift from the DB. Series keys only. Never raises."""
    opened = inc.get("opened_at") or 0
    cleared = inc.get("cleared_at") or 0
    dur = (cleared - opened) if (cleared and opened and cleared >= opened) else None
    members = []
    for m in (inc.get("members") or []):
        members.append({
            "series": m.get("series"),
            "label": _EXPLAIN_LABELS.get(m.get("series") or "", m.get("series")),
            "direction": m.get("direction"),
            "peak_z": (round(abs(m.get("peak_z") or 0), 1)
                       if m.get("peak_z") is not None else None),
            "peak_value": m.get("peak_value"), "baseline": m.get("baseline"),
            "unit": m.get("unit"),
            "first_seen": m.get("first_seen"), "last_seen": m.get("last_seen"),
        })
    return {
        "id": inc.get("id"), "severity": inc.get("severity"),
        "opened_at": opened or None, "cleared_at": cleared or None,
        "duration_s": dur, "member_count": len(members), "members": members,
        # the DB-derived lifecycle timeline get_incident already computed
        "timeline": inc.get("timeline") or [],
    }


def _incident_postmortem_grounding(inc, det):
    """Terse deterministic English lines grounding the LLM postmortem: what fired,
    for how long, how bad, plus any already-persisted probable-cause and spike-time
    I/O attribution. This is the model's ONLY source material — it must not invent
    anything beyond these facts. Doubles as the no-LLM prose fallback seed. Series
    keys only. Never raises."""
    lines = []
    sev = det.get("severity") or "warning"
    n = det.get("member_count") or 0
    dur = det.get("duration_s")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(det.get("opened_at") or 0))
    durtxt = _human_dur(dur) if dur is not None else "an unknown duration"
    lines.append(
        "A {sev} correlated incident opened {when}, ran for {d}, then all {n} "
        "signal(s) returned to baseline (resolved).".format(
            sev=sev, when=when, d=durtxt, n=n))
    for m in (det.get("members") or [])[:6]:
        name = m.get("label") or m.get("series") or "a series"
        direction = m.get("direction") or "change"
        u = m.get("unit") or ""
        seg = "{name}: {dir}".format(name=name, dir=direction)
        if m.get("peak_value") is not None and m.get("baseline") is not None:
            seg += " to {v}{u} (baseline ~{b}{u})".format(
                v=m.get("peak_value"), u=u, b=m.get("baseline"))
        if m.get("peak_z") is not None:
            seg += " at {z}σ".format(z=m.get("peak_z"))
        seg += "."
        lines.append(seg)
    # Reuse the already-persisted probable-cause (never a fresh LLM call) if present.
    cause = (inc.get("ai_explanation") or "").strip()
    if cause:
        lines.append("Prior probable-cause note: " + cause)
    # Spike-time I/O attribution (pure cached ring read, comm only) if present.
    sio = inc.get("spike_io") or {}
    if sio.get("writer") and sio["writer"].get("name"):
        lines.append("Heaviest writer during the spike: {n} ({r}).".format(
            n=sio["writer"]["name"], r=sio["writer"].get("write_h") or ""))
    if sio.get("reader") and sio["reader"].get("name"):
        lines.append("Heaviest reader during the spike: {n} ({r}).".format(
            n=sio["reader"]["name"], r=sio["reader"].get("read_h") or ""))
    return lines


def _postmortem_prompt(facts):
    return (
        "You are the Lab Copilot for a self-hosted homelab monitoring dashboard. "
        "A correlated monitoring incident has just RESOLVED. Write a short "
        "post-incident review using ONLY the facts below (deterministic readings "
        "from the lab — do NOT invent hostnames, numbers, times or causes not "
        "present in the facts). Return a JSON object with: \"probable_cause\" "
        "(1-2 plain-English sentences naming the most likely cause; say it is "
        "unclear if the facts don't point to one), \"impact\" (1 sentence on what "
        "this likely affected, grounded in the signals that fired), and "
        "\"recommended_action\" (one short, concrete follow-up the operator could "
        "take, or \"\" if none). No markdown.\n\n"
        "FACTS:\n- " + "\n- ".join(facts) + "\n")


def _postmortem_structured(facts, capture=None):
    """Ask the LLM for the typed postmortem prose via ollama JSON mode; parse it
    defensively. Returns (dict, error) where the dict is
    {'probable_cause','impact','recommended_action'} (each a bounded string,
    recommended_action possibly ''). Graceful degrade: LLM down / no text / not
    valid JSON / missing probable_cause → returns (None, error_code); the caller
    then persists nothing and the deterministic skeleton still renders. Never
    raises. `capture` (a list) receives the one metrics dict for cost accounting."""
    _scap = []
    text, err = _ollama_generate(
        _postmortem_prompt(facts), capture=_scap, fmt=POSTMORTEM_SCHEMA)
    if text is None:
        return None, (err or "unreachable")
    try:
        obj = json.loads(text)
    except Exception:
        return None, "bad_response"
    if not isinstance(obj, dict):
        return None, "bad_response"
    cause = obj.get("probable_cause")
    if not (isinstance(cause, str) and cause.strip()):
        return None, "bad_response"
    def _fld(v):
        if not isinstance(v, str):
            return ""
        return " ".join(v.split())[:_POSTMORTEM_FIELD_MAX]
    out = {"probable_cause": _fld(cause),
           "impact": _fld(obj.get("impact")),
           "recommended_action": _fld(obj.get("recommended_action"))}
    if isinstance(capture, list):
        capture.append(_scap[0] if _scap else None)
    return out, None


def _incident_postmortem_payload(inc, prose=None, model=None, at=None,
                                 llm_status="none"):
    """Assemble the full postmortem payload for the drawer: the DETERMINISTIC facts
    (always present) + the LLM prose (when generated/cached). Never raises."""
    det = _incident_postmortem_facts_deterministic(inc)
    pm = {"deterministic": det, "generated": bool(prose),
          "probable_cause": (prose or {}).get("probable_cause") if prose else None,
          "impact": (prose or {}).get("impact") if prose else None,
          "recommended_action": (prose or {}).get("recommended_action") if prose else None,
          "model": model, "generated_at": at, "llm_status": llm_status}
    return pm


def _load_persisted_postmortem(inc):
    """Return the persisted LLM prose dict for an incident (or None), from the
    postmortem_json column. Never raises."""
    raw = inc.get("postmortem_json")
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and obj.get("probable_cause"):
            return obj
    except Exception:
        pass
    return None


def _latest_postmortem_citation():
    """Deterministic citation for the most-recently-RESOLVED incident that has an
    ALREADY-PERSISTED postmortem — for embedding in the digest. Reads only the
    persisted prose (postmortem_json) + the incident row; NEVER generates and NEVER
    calls the LLM. Returns {"id","title","cause","when"} or None (no persisted
    postmortem exists). Series keys only — same privacy contract as the drawer.
    Never raises."""
    try:
        with LOCK:
            row = DB.execute(
                "SELECT id, severity, opened_at, cleared_at, postmortem_json, postmortem_at "
                "FROM incidents WHERE state='cleared' AND postmortem_at IS NOT NULL "
                "ORDER BY COALESCE(cleared_at, opened_at) DESC, postmortem_at DESC "
                "LIMIT 1", ()).fetchone()
    except Exception as e:
        print("latest postmortem citation error:", e, flush=True)
        return None
    if not row:
        return None
    iid, sev, opened, cleared, pm_json, pm_at = row
    prose = _load_persisted_postmortem({"postmortem_json": pm_json})
    if not prose:
        return None
    cause = (prose.get("probable_cause") or "").strip()
    if not cause:
        return None
    # First sentence / clause of the cause, bounded — keep the digest line tight.
    first = cause.split(". ")[0].strip().rstrip(".")
    if len(first) > 200:
        first = first[:197].rstrip() + "…"
    # Deterministic, series-key-only title: "<severity> incident #<id> (<n> signals)".
    try:
        members = _incident_members(iid)
    except Exception:
        members = []
    n = len(members)
    sev_txt = (sev or "warning").capitalize()
    # Title carries NO parenthetical of its own — the digest joins the signal count
    # and timestamp into a SINGLE parenthetical so the line never reads
    # "... (1 signal) (2026-... )". `signals` is surfaced separately for that join.
    title = "%s incident #%s" % (sev_txt, iid)
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(cleared or opened or 0)) \
        if (cleared or opened) else None
    return {"id": iid, "title": title, "cause": first, "when": when, "signals": n}


def get_incident_postmortem(iid, generate=False, force=False, now=None):
    """THE reader/writer of an incident postmortem. Cache-only unless explicitly
    asked to generate.

    • Unknown incident        → {"llm_status":"unknown", "resolved":False, ...}.
    • Open (not resolved)     → {"resolved":False, "postmortem":None} — an open
                                incident has no postmortem yet. NEVER an LLM call.
    • Resolved + cache        → returns the persisted prose + deterministic facts,
                                ZERO LLM call (unless force).
    • Resolved + generate     → assembles grounding under LOCK, RELEASES it, calls
                                the model (JSON mode, no lock held). On success
                                ATOMICALLY claims postmortem_at (at-most-once) and
                                persists the prose. LLM down/garbage → persists
                                nothing; the deterministic skeleton still returns
                                with an honest llm_status. force re-generates.

    Returns {"ok", "incident_id", "resolved", "postmortem":{...}|None, "model",
    "llm_status"}. Never mutates the host / incident lifecycle / any alert. Never
    raises."""
    now = int(now if now is not None else time.time())
    inc = get_incident(iid)             # cached read — zero LLM
    if inc is None:
        return {"ok": False, "incident_id": iid, "resolved": False,
                "postmortem": None, "model": COPILOT_MODEL, "llm_status": "unknown"}
    resolved = inc.get("state") == "cleared"
    if not resolved:
        # An open incident has no postmortem yet — clean null, no LLM.
        return {"ok": True, "incident_id": iid, "resolved": False,
                "postmortem": None, "model": COPILOT_MODEL, "llm_status": "open"}
    cached = _load_persisted_postmortem(inc)
    if cached and not force:
        pm = _incident_postmortem_payload(
            inc, prose=cached, model=inc.get("postmortem_model"),
            at=inc.get("postmortem_at"), llm_status="ok")
        return {"ok": True, "incident_id": iid, "resolved": True,
                "postmortem": pm, "model": inc.get("postmortem_model") or COPILOT_MODEL,
                "llm_status": "ok", "cached": True}
    if not generate:
        # Resolved but not yet generated and not asked to → deterministic skeleton.
        pm = _incident_postmortem_payload(inc, llm_status="ungenerated")
        return {"ok": True, "incident_id": iid, "resolved": True, "postmortem": pm,
                "model": COPILOT_MODEL, "llm_status": "ungenerated", "cached": False}
    # ── explicit generation (LOCK released before the model call) ──────────────
    det = _incident_postmortem_facts_deterministic(inc)
    facts = _incident_postmortem_grounding(inc, det)
    _cap = []
    prose, err = _postmortem_structured(facts, capture=_cap)   # NO lock held here
    if prose is None:
        # LLM off/garbage → persist nothing, still return the deterministic skeleton.
        pm = _incident_postmortem_payload(inc, llm_status=err or "unreachable")
        return {"ok": True, "incident_id": iid, "resolved": True, "postmortem": pm,
                "model": COPILOT_MODEL, "llm_status": err or "unreachable",
                "cached": False}
    blob = json.dumps(prose)
    # Atomic at-most-once claim: on a first generation (not force) only write when
    # nothing is persisted yet — so two concurrent generates never both persist.
    # force overwrites unconditionally (explicit regenerate).
    try:
        with LOCK:
            if force:
                cur = DB.execute(
                    "UPDATE incidents SET postmortem_json=?, postmortem_at=?, "
                    "postmortem_model=? WHERE id=? AND state='cleared'",
                    (blob, now, COPILOT_MODEL, iid))
            else:
                cur = DB.execute(
                    "UPDATE incidents SET postmortem_json=?, postmortem_at=?, "
                    "postmortem_model=? WHERE id=? AND state='cleared' "
                    "AND postmortem_at IS NULL",
                    (blob, now, COPILOT_MODEL, iid))
            claimed = cur.rowcount
            DB.commit()
    except Exception as e:
        print("store incident postmortem error:", e, flush=True)
        try: DB.rollback()
        except Exception: pass
        claimed = 0
    if not claimed and not force:
        # Someone else won the claim — return the now-persisted version (no re-gen).
        inc2 = get_incident(iid)
        cached2 = _load_persisted_postmortem(inc2) if inc2 else None
        pm = _incident_postmortem_payload(
            inc2 or inc, prose=cached2, model=(inc2 or inc).get("postmortem_model"),
            at=(inc2 or inc).get("postmortem_at"), llm_status="ok")
        return {"ok": True, "incident_id": iid, "resolved": True, "postmortem": pm,
                "model": COPILOT_MODEL, "llm_status": "ok", "cached": True}
    pm = _incident_postmortem_payload(inc, prose=prose, model=COPILOT_MODEL,
                                      at=now, llm_status="ok")
    out = {"ok": True, "incident_id": iid, "resolved": True, "postmortem": pm,
           "model": COPILOT_MODEL, "llm_status": "ok", "cached": False}
    inf = _inference_cost(_cap[0] if _cap else None, now)
    if inf:
        out["inference"] = inf
    return out


# ── Off-poll postmortem worker (E1) — dedicated, decoupled ────────────────────
# When `incident_auto_postmortem` is "1", an incident that RESOLVES gets a
# postmortem generated automatically — but ONLY here, on a single daemon worker
# draining a queue, NEVER inside evaluate_incidents/collect/health_scan/the sample
# loop. The collector merely ENQUEUES the id (a cheap put AFTER the DB LOCK is
# released). Rate-limited, one at a time, de-duplicated. The atomic claim in
# get_incident_postmortem guarantees at-most-once even if enqueued twice.
_INCIDENT_PM_Q = queue.Queue()
_INCIDENT_PM_QUEUED = set()
_INCIDENT_PM_QLOCK = threading.Lock()
_INCIDENT_PM_MIN_GAP = float(os.environ.get("INCIDENT_PM_MIN_GAP", "10"))
_incident_pm_worker_started = False


def _incident_postmortem_worker():
    """Drain the postmortem queue one id at a time, rate-limited. Runs forever on a
    daemon thread; every generation is a graceful no-op when the LLM is down."""
    last = 0.0
    while True:
        iid = _INCIDENT_PM_Q.get()
        try:
            with _INCIDENT_PM_QLOCK:
                _INCIDENT_PM_QUEUED.discard(iid)
            gap = _INCIDENT_PM_MIN_GAP - (time.time() - last)
            if gap > 0:
                time.sleep(gap)
            # The opt-in may have flipped OFF since enqueue — re-check before any
            # LLM work. get_incident_postmortem is itself cache-safe (skips the
            # model when a postmortem already exists / the incident isn't resolved).
            if get_settings().get("incident_auto_postmortem") == "1":
                get_incident_postmortem(iid, generate=True, force=False)
            last = time.time()
        except Exception as e:
            print("incident postmortem worker error:", e, flush=True)
        finally:
            _INCIDENT_PM_Q.task_done()


def _ensure_incident_postmortem_worker():
    """Lazily start the single worker thread on first real enqueue (so a process
    that never opts in — and the test suite — never spawns it)."""
    global _incident_pm_worker_started
    with _INCIDENT_PM_QLOCK:
        if _incident_pm_worker_started:
            return
        threading.Thread(target=_incident_postmortem_worker, name="incident-postmortem",
                         daemon=True).start()
        _incident_pm_worker_started = True


def _enqueue_incident_postmortem(iid):
    """Queue a just-RESOLVED incident for auto-postmortem IF the opt-in is on. A
    cheap, DB-LOCK-free trigger called ONLY after evaluate_incidents releases LOCK;
    reads the setting (its own lock), de-dupes, and hands the id to the worker. A
    no-op when the toggle is off (the default). Never raises, never blocks the
    caller on the LLM."""
    try:
        if not iid or get_settings().get("incident_auto_postmortem") != "1":
            return
        with _INCIDENT_PM_QLOCK:
            if iid in _INCIDENT_PM_QUEUED:
                return
            _INCIDENT_PM_QUEUED.add(iid)
        _ensure_incident_postmortem_worker()
        _INCIDENT_PM_Q.put(iid)
    except Exception as e:
        print("enqueue incident postmortem error:", e, flush=True)


# JSON-Schema for the ask-box's structured answer — mirrors EXPLAIN_SCHEMA so the
# ask result can carry the same severity chip + suggested-action CTA as "Explain
# this spike". ollama constrains JSON-mode decoding to this; small models honour it
# best-effort, hence the prose fallback in `_ask_structured`.
ASK_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "severity": {"type": "string", "enum": ["info", "warning", "critical"]},
        "action": {"type": "string"},
    },
    "required": ["answer", "severity"],
}


def _ask_structured_prompt(facts, question):
    return (
        "You are the Lab Copilot for a self-hosted homelab monitoring dashboard. "
        "Answer the user's question using ONLY the facts below — these are live "
        "readings from their lab; do not invent anything. Return a JSON object "
        "with: \"answer\" (1-3 short plain-English sentences answering the "
        "question; if the facts don't contain the answer, say so plainly), "
        "\"severity\" (one of \"info\", \"warning\", \"critical\" — how "
        "concerning the situation is for the operator), and \"action\" (one "
        "short, concrete next step the operator could take, or \"\" if none). "
        "No markdown.\n\n"
        "FACTS:\n- " + "\n- ".join(facts) + "\n\n"
        "QUESTION: " + question.strip() + "\n")


def _ask_structured(facts, question, capture=None):
    """Ask the LLM for a TYPED ask answer via ollama JSON mode, parse it
    defensively, and return (dict, error). Mirrors `_explain_structured`. On
    success the dict is {'answer','severity','action'} with severity clamped to
    the allowed set and action either a non-empty string or None.

    Graceful degrade: if the LLM is down/unreachable, returns no text, the text
    isn't valid JSON, or lacks a usable answer (small models may ignore the
    schema), we fall back to the EXISTING plain-prose ask (`_copilot_ask_prompt`)
    so the answer is never worse than today. Returns (None, error_code) only when
    even the prose fallback yields nothing (LLM fully down) — the caller then uses
    the deterministic routed facts. Never raises. ONE structured call (then at most
    one prose call on fallback), no LOCK held across the call. `capture`, when a
    list, receives exactly one metrics dict — for the call whose text we actually
    return — so the per-inference cost chip stays accurate across the fallback."""
    _scap = []
    text, err = _ollama_generate(
        _ask_structured_prompt(facts, question), capture=_scap, fmt=ASK_SCHEMA)
    if text is not None:
        try:
            obj = json.loads(text)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            ans = obj.get("answer")
            if isinstance(ans, str) and ans.strip():
                sev = obj.get("severity")
                if not (isinstance(sev, str) and sev.lower() in _EXPLAIN_SEVERITIES):
                    sev = "info"
                else:
                    sev = sev.lower()
                act = obj.get("action")
                if not (isinstance(act, str) and act.strip()):
                    act = None
                else:
                    act = act.strip()
                if isinstance(capture, list):
                    capture.append(_scap[0] if _scap else None)
                return {"answer": ans.strip(), "severity": sev,
                        "action": act}, None
        # Got text but it wasn't usable structured JSON → prose fallback below.
    # Either the LLM is down (err set) or it ignored the schema → plain prose.
    _pcap = []
    ptext, perr = _ollama_generate(
        _copilot_ask_prompt(facts, question), capture=_pcap)
    if ptext is not None:
        if isinstance(capture, list):
            capture.append(_pcap[0] if _pcap else None)
        return {"answer": ptext, "severity": None, "action": None}, None
    return None, (err or perr or "unreachable")


@app.route("/api/copilot/explain", methods=["POST"])
def api_copilot_explain():
    """Explain a single anomaly/point in plain English via the local LLM, grounded
    on the series' own movement and what was running at that moment. Always 200;
    graceful `llm_status` (and the deterministic facts) when the LLM is off."""
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    now = int(time.time())
    ctx = _explain_context(payload, now)
    facts = _explain_facts(ctx)
    out = {"now": now, "model": COPILOT_MODEL, "key": ctx.get("key"),
           "facts": facts, "context": ctx, "enabled": COPILOT_ENABLED}
    _cap = []
    # Structured (JSON-mode) explain: typed {explanation,severity,action} that
    # powers the severity chip + action CTA. Falls back to plain prose internally
    # when the small model ignores the schema, so this is never worse than prose.
    res, err = _explain_structured(facts, capture=_cap)
    if res is not None:
        out.update({"explanation": res["explanation"], "source": "llm",
                    "llm_status": "ok"})
        if res.get("severity"):
            out["severity"] = res["severity"]
        if res.get("action"):
            out["action"] = res["action"]
        inf = _inference_cost(_cap[0] if _cap else None, now)
        if inf:
            out["inference"] = inf
    else:
        # No LLM: hand back the deterministic facts as the explanation so the
        # "Why?" action is never a dead end.
        out.update({"explanation": " ".join(facts), "source": "facts", "llm_status": err})
    return jsonify(out)


@app.route("/api/copilot/explain/stream", methods=["POST"])
def api_copilot_explain_stream():
    """Streaming sibling of /api/copilot/explain (SSE). Builds the SAME
    deterministic explain context + facts + prompt as the non-stream endpoint
    (so streamed and non-stream explanations match), then streams the local
    LLM's likely-cause explanation token-by-token. Additive: the non-stream
    endpoint is the graceful fallback the UI drops to on any stream failure.

    SSE events:
      • event: token   data: {"t": "<chunk>"}          (0..N, as they arrive)
      • event: done    data: {"explanation","source":"llm","llm_status":"ok",
                              "now","key","facts","context","model","enabled"}
      • event: error   data: {"source":"facts","llm_status":<code>,
                              "explanation":<facts summary>,"now","key",
                              "facts","context","model","enabled"}
    Always one terminal event (done OR error); never hangs, never 500s once the
    stream has begun. Facts/telemetry only — no settings/URLs/tokens streamed.
    The context assembly happens BEFORE the generator body so no lock is held
    across the stream."""
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    now = int(time.time())
    # Context + facts + prompt assembled here (OUTSIDE the generator), identical
    # to the non-stream endpoint, so no lock is held while tokens stream and the
    # terminal payload matches /api/copilot/explain byte-shape.
    ctx = _explain_context(payload, now)
    facts = _explain_facts(ctx)
    prompt = _explain_prompt(facts)
    base = {"now": now, "model": COPILOT_MODEL, "key": ctx.get("key"),
            "facts": facts, "context": ctx, "enabled": COPILOT_ENABLED}

    def gen():
        acc = []
        try:
            for kind, val in _ollama_generate_stream(prompt):
                if kind == "token":
                    acc.append(val)
                    yield _sse("token", {"t": val})
                elif kind == "done":
                    out = dict(base)
                    out.update({"explanation": val.get("text") or "".join(acc),
                                "source": "llm", "llm_status": "ok"})
                    inf = _inference_cost(val.get("metrics"), now)
                    if inf:
                        out["inference"] = inf
                    yield _sse("done", out)
                    return
                elif kind == "error":
                    # LLM unavailable (start or mid-stream): hand back the
                    # deterministic facts as the explanation, same as the
                    # non-stream path. Never 500 — terminal error, then close.
                    out = dict(base)
                    out.update({"explanation": " ".join(facts),
                                "source": "facts", "llm_status": val})
                    yield _sse("error", out)
                    return
        except Exception as e:
            print("copilot explain stream gen error:", type(e).__name__, flush=True)
            try:
                out = dict(base)
                out.update({"explanation": " ".join(facts),
                            "source": "facts", "llm_status": "unreachable"})
                yield _sse("error", out)
            except Exception:
                pass
            return
        # Stream ended without a terminal frame (shouldn't happen) — emit a facts
        # terminal so the client never waits forever.
        out = dict(base)
        out.update({"explanation": " ".join(facts),
                    "source": "facts", "llm_status": "bad_response"})
        yield _sse("error", out)

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/copilot/digest/stream", methods=["POST"])
def api_copilot_digest_stream():
    """Streaming sibling of /api/copilot/digest (SSE). Builds the SAME
    deterministic context + facts + prompt as the non-stream endpoint (so the
    streamed and non-stream narratives match), AND assembles the deterministic
    digest sections (reused from the scheduled-digest builder, _digest_sections),
    then streams the local LLM's narrative token-by-token. Additive: the
    non-stream endpoint is the graceful fallback the UI drops to on any stream
    failure; the scheduled-delivery path (build_digest/send_digest) is untouched.

    SSE events:
      • event: token   data: {"t": "<chunk>"}          (0..N, as they arrive)
      • event: done    data: {"digest","source":"llm","llm_status":"ok",
                              "sections","now","model","facts","context","enabled"}
      • event: error   data: {"digest":<facts summary>,"source":"facts",
                              "llm_status":<code>,"sections","now","model","facts",
                              "context","enabled"}
    Always one terminal event (done OR error); never hangs, never 500s once the
    stream has begun. The deterministic sections render even with the LLM down
    (the digest invariant: sections stand alone, never empty). Facts/telemetry
    only — no settings/URLs/tokens streamed. Context + sections + prompt are
    assembled BEFORE the generator body so no lock is held across the stream."""
    now = int(time.time())
    # Context + facts + prompt + deterministic sections assembled here (OUTSIDE
    # the generator), identical to the non-stream endpoint, so no lock is held
    # while tokens stream and the terminal payload matches /api/copilot/digest's
    # byte-shape (plus the additive deterministic `sections`).
    ctx = _copilot_context(now)
    facts = _copilot_facts(ctx)
    prompt = _copilot_digest_prompt(facts)
    try:
        sections = _digest_sections(now)
    except Exception as e:
        print("digest stream sections error:", e, flush=True)
        sections = []
    base = {"now": now, "model": COPILOT_MODEL, "facts": facts,
            "context": ctx, "enabled": COPILOT_ENABLED, "sections": sections}

    def gen():
        acc = []
        try:
            for kind, val in _ollama_generate_stream(prompt):
                if kind == "token":
                    acc.append(val)
                    yield _sse("token", {"t": val})
                elif kind == "done":
                    out = dict(base)
                    out.update({"digest": val.get("text") or "".join(acc),
                                "source": "llm", "llm_status": "ok"})
                    inf = _inference_cost(val.get("metrics"), now)
                    if inf:
                        out["inference"] = inf
                    yield _sse("done", out)
                    return
                elif kind == "error":
                    # LLM unavailable (start or mid-stream): hand back the
                    # deterministic fact summary as the digest, with the
                    # standalone sections, same as the non-stream path. Never
                    # 500 — terminal error frame, then close.
                    out = dict(base)
                    out.update({"digest": " ".join(facts),
                                "source": "facts", "llm_status": val})
                    yield _sse("error", out)
                    return
        except Exception as e:
            print("copilot digest stream gen error:", type(e).__name__, flush=True)
            try:
                out = dict(base)
                out.update({"digest": " ".join(facts),
                            "source": "facts", "llm_status": "unreachable"})
                yield _sse("error", out)
            except Exception:
                pass
            return
        # Stream ended without a terminal frame (shouldn't happen) — emit a facts
        # terminal so the client never waits forever.
        out = dict(base)
        out.update({"digest": " ".join(facts),
                    "source": "facts", "llm_status": "bad_response"})
        yield _sse("error", out)

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Proactive Recommendations (E1) ─────────────────────────────────────────────
# The AI Lab Cockpit doesn't just SHOW state — it tells you what to DO. A ranked,
# actionable to-do list derived from the lab's OWN live signals (forecasts,
# anomalies, costs, incidents, uptime, OOM). Distinct from the digest: the digest
# is a narrative summary; recommendations are prioritized, per-signal, each with a
# severity, the signal it's based on, and a concrete suggested action.
#
# HARD CONSTRAINTS: advice only — recommendations NEVER auto-execute anything; no
# host mutation, no buttons that change the host. Read-only over our own data. The
# deterministic detectors are the reliable core; the LLM ONLY optionally phrases a
# one-line framing and never blocks/errors the panel.
_RECO_SEV_RANK = {"crit": 3, "warn": 2, "info": 1}

def _reco_signals(now):
    """Assemble the live signal bundle the detectors scan. Reuses the SAME forecast
    accessors the /api/forecast + Copilot paths use (no new heavy work), with the
    SAME lock discipline: the history-backed helpers run in one LOCK pass; the
    accessors that take LOCK themselves (incidents/uptime) and the OOM read run
    OUTSIDE it so the non-reentrant lock is never nested. Never raises — a failed
    sub-signal degrades to empty/absent, the panel still renders what it can."""
    sig = {"disk": [], "vram": {}, "cost_month": {}, "anomalies": {},
           "incidents": {"open": 0, "top": None}, "uptime": [], "ooms": []}
    try:
        ctx = _cost_ctx()
        with LOCK:
            cur = DB.cursor()
            sig["disk"] = _disk_forecasts(cur, now)
            sig["cost_month"] = _cost_projection(cur, ctx, now)
            sig["anomalies"] = _zscore_anomalies(cur, now)
            sig["vram"] = _vram_forecast(cur, now)
            # Recurring OOM kills, grouped per service over the look-back window.
            try:
                cutoff = int(now - RECO_OOM_WINDOW_DAYS * 86400)
                rows = cur.execute(
                    "SELECT service, COUNT(*) n, MAX(ts) last FROM events "
                    "WHERE kind='oom' AND ts>=? GROUP BY service ORDER BY n DESC, last DESC",
                    (cutoff,)).fetchall()
                sig["ooms"] = [{"service": r[0], "count": r[1], "last_ts": r[2]} for r in rows]
            except Exception:
                sig["ooms"] = []
    except Exception as e:
        print("recommendations signal error:", e, flush=True)
    # incidents_summary() + uptime_overview() take LOCK themselves → call OUTSIDE.
    try:
        sig["incidents"] = incidents_summary()
    except Exception as e:
        print("recommendations incidents error:", e, flush=True)
    try:
        sig["uptime"] = uptime_overview().get("checks", []) or []
    except Exception as e:
        print("recommendations uptime error:", e, flush=True)
    # Image-update awareness — cheap read of the cached checker results (no network
    # here; the background worker does the registry queries). Absent/off => empty.
    try:
        sig["image_updates"] = image_updates_snapshot()
    except Exception as e:
        print("recommendations image-updates error:", e, flush=True)
        sig["image_updates"] = {}
    return sig


def _reco_detect(sig, now=None):
    """Pure-Python detectors: scan the signal bundle, emit a recommendation dict per
    fired condition. Each item: {id, severity, title, detail, action, source, link?,
    ts?}. NO LLM, NO mutation — advice only. Deterministic + unit-testable. Items are
    ranked (severity, then recency/impact) and capped by the caller."""
    now = now or int(time.time())
    items = []

    # 1) Disk fill ETA below threshold ----------------------------------------
    for d in (sig.get("disk") or []):
        if d.get("status") != "filling":
            continue
        eta = d.get("eta_days")
        if eta is None:
            continue
        mp = d.get("mount") or "?"
        if eta < RECO_DISK_CRIT_DAYS:
            sev = "crit"
        elif eta < RECO_DISK_WARN_DAYS:
            sev = "warn"
        else:
            continue
        free = d.get("free_gb")
        free_txt = (" %s GB free" % free) if free is not None else ""
        items.append({
            "id": "disk:" + mp, "severity": sev, "source": "disk",
            "title": "Disk %s fills in ~%sd" % (mp, _reco_num(eta)),
            "detail": "%s is %s%% full%s and trending up at ~%s GB/day." % (
                mp, d.get("pct"), free_txt, _reco_num(d.get("gb_per_day"))),
            "action": "Archive or prune data on %s, or expand the volume." % mp,
            "link": "disks", "ts": d.get("eta_ts"), "impact": -eta,
        })

    # 2) VRAM headroom low / VRAM-ETA short -----------------------------------
    v = sig.get("vram") or {}
    free_gb = v.get("free_gb")
    total_gb = v.get("total_gb")
    if free_gb is not None and total_gb:
        if free_gb < RECO_VRAM_CRIT_GB:
            sev = "crit"
        elif free_gb < RECO_VRAM_WARN_GB:
            sev = "warn"
        else:
            sev = None
        if sev:
            items.append({
                "id": "vram:headroom", "severity": sev, "source": "vram",
                "title": "VRAM headroom %s GB" % _reco_num(free_gb),
                "detail": "Only %s GB free of %s GB total (loaded models hold %s GB)." % (
                    _reco_num(free_gb), _reco_num(total_gb), _reco_num(v.get("models_gb"))),
                "action": "Unload an idle model or cap concurrent models / keep-alive.",
                "link": "models", "impact": -(free_gb),
            })
    # short VRAM-exhaustion ETA (separate, trend-based) — only when filling soon
    if v.get("status") == "filling" and v.get("eta_min") is not None:
        eta_min = v.get("eta_min")
        if eta_min < 120:    # under ~2h to full is worth flagging
            items.append({
                "id": "vram:eta", "severity": "warn", "source": "vram",
                "title": "VRAM trending to full in ~%s min" % _reco_num(eta_min),
                "detail": "VRAM is climbing at ~%s MB/min and would fill the GPU soon." % _reco_num(v.get("mb_per_min")),
                "action": "Stagger heavy jobs or shorten keep-alive before it OOMs.",
                "link": "models", "ts": v.get("eta_ts"), "impact": -(eta_min / 60.0),
            })

    # 3) Cost projection up sharply vs last month -----------------------------
    cm = sig.get("cost_month") or {}
    if cm.get("enabled") and cm.get("delta_pct") is not None and cm.get("last_month"):
        dp = cm.get("delta_pct")
        if dp >= RECO_COST_CRIT_PCT:
            sev = "crit"
        elif dp >= RECO_COST_WARN_PCT:
            sev = "warn"
        else:
            sev = None
        if sev:
            cur_sym = cm.get("currency") or "$"
            items.append({
                "id": "cost:projection", "severity": sev, "source": "cost",
                "title": "Projected spend %s%s (+%s%% vs last month)" % (
                    cur_sym, _reco_num(cm.get("projected_month")), dp),
                "detail": "Month-to-date %s%s; last month was %s%s." % (
                    cur_sym, _reco_num(cm.get("month_to_date")), cur_sym, _reco_num(cm.get("last_month"))),
                "action": "Shift heavy jobs to off-peak hours or trim idle GPU load.",
                "link": "costs", "impact": dp,
            })

    # 4) Active incident / active anomaly -------------------------------------
    inc = sig.get("incidents") or {}
    top = inc.get("top")
    if inc.get("open") and top:
        sev = "crit" if top.get("severity") == "critical" else "warn"
        series = top.get("series") or []
        slabel = ", ".join(str(s) for s in series[:4]) if series else "correlated series"
        items.append({
            "id": "incident:" + str(top.get("id")), "severity": sev, "source": "incident",
            "title": "%s incident active (%s series)" % (
                (top.get("severity") or "warning").capitalize(), top.get("active_count") or top.get("member_count") or 1),
            "detail": "Open incident correlating: %s. Investigate the driving entity." % slabel,
            "action": "Open the incident drawer to see members and likely cause.",
            "link": "incident:" + str(top.get("id")), "ts": top.get("opened_at"),
            "impact": (top.get("active_count") or 1),
        })
    else:
        # No incident, but a standalone active anomaly is still worth surfacing.
        anoms = (sig.get("anomalies") or {}).get("items") or []
        if anoms:
            a = anoms[0]
            items.append({
                "id": "anomaly:" + str(a.get("key")), "severity": "warn", "source": "anomaly",
                "title": "%s anomaly active (%sσ)" % (str(a.get("key")), _reco_num(abs(a.get("z") or 0))),
                "detail": "%s %s — %s%s now vs ~%s%s baseline." % (
                    a.get("key"), a.get("direction"), a.get("value"), a.get("unit"),
                    a.get("baseline"), a.get("unit")),
                "action": "Check what changed on the GPU/host around now.",
                "link": "gpu", "impact": abs(a.get("z") or 0),
            })

    # 5) Recurring OOM kills ---------------------------------------------------
    for o in (sig.get("ooms") or []):
        n = o.get("count") or 0
        if n < RECO_OOM_WARN_N:
            continue
        svc = o.get("service") or "?"
        sev = "crit" if n >= RECO_OOM_CRIT_N else "warn"
        items.append({
            "id": "oom:" + svc, "severity": sev, "source": "oom",
            "title": "%s OOM-killed %d time%s" % (svc, n, "" if n == 1 else "s"),
            "detail": "%s ran out of memory %d time%s in the last %s days." % (
                svc, n, "" if n == 1 else "s", _reco_num(RECO_OOM_WINDOW_DAYS)),
            "action": "Raise its memory limit or reduce its batch/model size.",
            "link": "containers", "ts": o.get("last_ts"), "impact": n,
        })

    # 6) Uptime check down / flapping -----------------------------------------
    for c in (sig.get("uptime") or []):
        if not c.get("enabled"):
            continue
        state = c.get("state")
        label = str(c.get("label") or c.get("id") or "check")
        up_pct = c.get("uptime")
        if state == "down":
            items.append({
                "id": "uptime:" + str(c.get("id")), "severity": "crit", "source": "uptime",
                "title": "%s is DOWN" % label,
                "detail": _reco_uptime_detail(c) or ("Uptime check '%s' is failing." % label),
                "action": "Investigate the endpoint — service, network, or TLS.",
                "link": "uptime", "ts": c.get("last_checked"), "impact": 100,
            })
        elif up_pct is not None and up_pct < 95 and (c.get("window_total") or 0) >= 5:
            # Flapping: not currently down, but a poor recent uptime% over the window.
            items.append({
                "id": "uptime:" + str(c.get("id")), "severity": "warn", "source": "uptime",
                "title": "%s is flapping (%s%% uptime)" % (label, _reco_num(up_pct)),
                "detail": "'%s' has only %s%% uptime over the recent window." % (label, _reco_num(up_pct)),
                "action": "Investigate intermittent failures on this endpoint.",
                "link": "uptime", "ts": c.get("last_checked"), "impact": 100 - up_pct,
            })

    # 7) Container images with newer upstream versions (awareness-only) ---------
    iu = sig.get("image_updates") or {}
    if iu.get("enabled"):
        n_up = int(iu.get("count") or 0)
        if n_up >= RECO_IMG_UPDATES_INFO_N:
            avail = [r for r in (iu.get("results") or []) if r.get("status") == "update_available"]
            names = ", ".join((r.get("image") or "?") for r in avail[:4])
            more = (" +%d more" % (len(avail) - 4)) if len(avail) > 4 else ""
            items.append({
                "id": "images:updates", "severity": "info", "source": "images",
                "title": "%d container image%s have updates" % (n_up, "" if n_up == 1 else "s"),
                "detail": "Newer upstream images for: %s%s." % (names or "container images", more),
                "action": "Review on the Containers tab and update when convenient.",
                "link": "containers", "impact": n_up,
            })

    # Rank: severity desc, then impact desc, then recency (newer ts first).
    items.sort(key=lambda it: (
        _RECO_SEV_RANK.get(it.get("severity"), 0),
        it.get("impact") or 0,
        it.get("ts") or 0,
    ), reverse=True)
    return items


def _reco_counts(sig):
    """CHEAP, LLM-FREE recommendation tally for the cockpit badge/hero rollup.

    Runs ONLY the deterministic detectors (`_reco_detect`) over an already-assembled
    signal bundle and returns compact counts by severity — it NEVER calls
    `_ollama_generate`. This is what the frequently-polled hero/nav badge rides on,
    so the LLM is never hit on a timer. Returns {crit, warn, total}; never raises."""
    try:
        items = _reco_detect(sig)[:RECO_MAX_ITEMS]
    except Exception as e:
        print("reco counts error:", e, flush=True)
        items = []
    crit = sum(1 for it in items if it.get("severity") == "crit")
    warn = sum(1 for it in items if it.get("severity") == "warn")
    return {"crit": crit, "warn": warn, "total": len(items)}


def _reco_num(x):
    """Format a number tersely: drop a trailing .0 so '14.0' reads '14'. Tolerant of
    None/strings (returns them stringified) so a detector line never raises."""
    if x is None:
        return "?"
    try:
        f = float(x)
    except (TypeError, ValueError):
        return str(x)
    return str(int(f)) if f == int(f) else str(round(f, 1))


def _reco_uptime_detail(c):
    """Credential-safe down reason for an uptime recommendation (reuses the same
    redaction the notifier uses). Never leaks the target URL."""
    try:
        bits = []
        code = c.get("last_code")
        if code is not None:
            bits.append("status %s" % code)
        err = c.get("last_err")
        if err:
            bits.append(_redact_target(str(err))[:160])
        return ("Down — " + " — ".join(bits)) if bits else ""
    except Exception:
        return ""


def _reco_llm_prompt(items):
    """Build a SMALL, secret-free prompt asking the LLM for one short 'top priority'
    line over the already-detected recommendation titles. We send ONLY the
    deterministic title/severity text — no URLs, no creds, no raw host data beyond
    what the panel already shows. Bounded to the top few items."""
    lines = []
    for it in items[:RECO_MAX_ITEMS]:
        lines.append("[%s] %s" % ((it.get("severity") or "info").upper(), it.get("title") or ""))
    return (
        "You are the Lab Copilot for a self-hosted homelab monitoring dashboard. "
        "Below is a ranked list of detected recommendations (already computed by the "
        "monitor). In ONE short sentence (plain English, no markdown), tell the owner "
        "what to tackle FIRST and why. Use ONLY these items; invent nothing.\n\n"
        "RECOMMENDATIONS:\n- " + "\n- ".join(lines) + "\n\nTOP PRIORITY:")


@app.route("/api/recommendations")
def api_recommendations():
    """Ranked, actionable to-do list derived from the lab's own live signals —
    deterministic detectors (the reliable core) over forecast/anomaly/cost/incident/
    uptime/OOM. Always 200, graceful-degrade, read-only, advice-only (NEVER mutates
    the host). The local LLM optionally adds a one-line 'top priority' framing; if
    it's off/unreachable the deterministic items render unchanged. Reuses the same
    forecast accessors as /api/forecast (no extra heavy work).

    `?brief=1` (or `?llm=0`) returns the ranked deterministic ITEMS + counts WITHOUT
    ever calling the local LLM — fast, GPU-free, stable. This is what agent clients
    (the MCP `get_recommendations` tool) poll so they never spin up ollama."""
    now = int(time.time())
    try:
        sig = _reco_signals(now)
        items = _reco_detect(sig, now)[:RECO_MAX_ITEMS]
    except Exception as e:
        print("recommendations error:", e, flush=True)
        items = []
    crit = sum(1 for it in items if it.get("severity") == "crit")
    warn = sum(1 for it in items if it.get("severity") == "warn")
    out = {"now": now, "generated_at": now, "items": items,
           "count": len(items), "counts": {"crit": crit, "warn": warn,
                                            "total": len(items)},
           "model": COPILOT_MODEL,
           "enabled": COPILOT_ENABLED, "llm_used": False,
           "priority": None, "llm_status": "skipped"}
    # Deterministic, LLM-free mode: hand back the ranked items + counts, never touch
    # ollama. Keeps agent polling cheap and the GPU idle.
    brief = (request.args.get("brief") in ("1", "true", "yes")
             or request.args.get("llm") in ("0", "false", "no"))
    if brief:
        return jsonify(out)
    # Optional, bounded LLM framing — only when there's something to prioritise.
    # Never blocks or errors the panel: the deterministic items above stand alone.
    if items:
        text, err = _ollama_generate(_reco_llm_prompt(items), timeout=min(COPILOT_TIMEOUT, 12))
        if text is not None:
            out["priority"] = text
            out["llm_used"] = True
            out["llm_status"] = "ok"
        else:
            out["llm_status"] = err
    return jsonify(out)


@app.route("/api/images/updates")
def api_image_updates():
    """Image-update awareness: per-container update status + an updates-available
    count. AWARENESS ONLY — this endpoint (and the whole subsystem) never pulls,
    runs, restarts or deletes anything. Always 200, graceful-degrade, read-only.
    When the check is OFF (default) returns enabled=False with empty results and
    zero outbound has happened. Statuses: up_to_date / update_available / unknown."""
    try:
        snap = image_updates_snapshot()
    except Exception as e:
        print("image updates endpoint error:", e, flush=True)
        snap = {"enabled": False, "count": 0, "results": [],
                "by_status": {"up_to_date": 0, "update_available": 0, "unknown": 0}}
    return jsonify(snap)


@app.route("/api/images/updates/check", methods=["POST"])
def api_image_update_check_one():
    """On-demand 'Check now' for ONE container. Body: {container:"<name-or-id>"}.

    Runs a SINGLE bounded upstream digest re-check for that container's image and
    returns the fresh status instantly — so the user doesn't wait for the ~6h
    background cycle. This is an explicit user action, so it runs even when the
    background image-update poll is OFF.

    AWARENESS ONLY: registry GET/HEAD (manifest digest) + a local docker-socket
    GET (deployed digest). It NEVER pulls/runs/restarts/exec/deletes anything.

    The container is validated against the LIVE container set (the same injection
    gate as the log-tail feature) — unknown/invalid → clean 404, never a 500,
    never a hang. Respects the rate-limit backoff (rate_limited:true instead of
    hammering). Always 200 with a clean shape, or clean 4xx.

    Response: {ok, container, status, deployed_digest, upstream_digest,
    checked_at, reason?, rate_limited}"""
    body = request.get_json(silent=True) or {}
    name = body.get("container")
    if not isinstance(name, str) or not name.strip():
        return jsonify({"ok": False, "error": "container_required"}), 400
    cid, cname = _resolve_container(name.strip())
    if not cid:
        return jsonify({"ok": False, "container": name, "status": "unknown",
                        "error": "no_such_container"}), 404
    # Resolve the deployed image ref from the live set (read-only docker socket).
    image_ref = ""
    try:
        for ct in containers():
            if ct["id"] == cid:
                image_ref = ct.get("image", "") or ""
                break
    except Exception:
        image_ref = ""
    try:
        res = image_update_check_one(cid, cname, image_ref)
    except Exception as e:
        print("image check-now endpoint error:", e, flush=True)
        res = {"status": "unknown", "reason": "error", "deployed_digest": None,
               "upstream_digest": None, "checked_at": int(time.time()),
               "rate_limited": False}
    res["ok"] = res.get("status") != "unknown" or res.get("reason") in (None, "digest-pinned")
    res["container"] = cname
    res["image"] = image_ref
    return jsonify(res)


@app.route("/api/sessions")
def api_sessions():
    """GPU activity sessions over the range — contiguous GPU-busy periods rebuilt
    from the power/util history. Plus the live training processes detected on the
    hub right now (LATEST['training']). Powers the Experiments tab."""
    s = get_settings()
    try:
        price = float(s.get("kwh_price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    rng = request.args.get("range", "7d")
    span = RANGES.get(rng, 604800)
    now = int(time.time())
    with LOCK:
        cur = DB.cursor()
        since = (cur.execute("SELECT MIN(ts) FROM samples").fetchone()[0] or now) if span is None else now - span
        rows = cur.execute("SELECT ts,util,power,mem_used FROM samples WHERE ts>=? ORDER BY ts", (since,)).fetchall()
    sessions = _gpu_sessions(rows, INTERVAL, price=price)[:50]
    tot_energy = round(sum(x["energy_kwh"] for x in sessions), 3)
    return jsonify({
        "range": rng, "currency": s.get("currency") or "$",
        "price": price, "kwh_price": price,
        "active_pct": _ACTIVE_UTIL,
        "sessions": sessions,
        "totals": {"count": len(sessions), "energy_kwh": tot_energy,
                   "cost": round(tot_energy * price, 2),
                   "active_hours": round(sum(x["duration"] for x in sessions) / 3600.0, 1)},
        "training": LATEST.get("training") or [],
        "devtools": LATEST.get("devtools") or [],
    })

# ── Integrations: experiment/run tracking API (push/pull) + MLflow sync ────────
# Pivot from /proc auto-detection to a key-authenticated ingest API a notebook can
# push to (Jupyter/Colab/Kaggle via homelab_run.py), pulled back with the run's real
# GPU energy/cost attached by overlapping its [start,end] window with `samples`.
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
    key = "hlm_" + secrets.token_urlsafe(32)
    kid, now = uuid.uuid4().hex, int(time.time())
    exp = (now + int(expires_in_days) * 86400) if expires_in_days else None
    with LOCK:
        DB.execute("INSERT INTO api_keys(id,name,key_hash,prefix,created_at,expires_at,last_used_at) "
                   "VALUES(?,?,?,?,?,?,?)", (kid, (name or "key")[:128], _hash_key(key), key[:12], now, exp, None))
        DB.commit()
    return kid, key

def _gen_api_key(name="default", expires_in_days=None):
    """Back-compat shim (used by tests): mint a key and return the plaintext."""
    return _create_api_key(name, expires_in_days)[1]

def _key_lookup(presented):
    """Return the id of a live (non-expired) key matching `presented`, else None,
    and stamp its last_used_at. Stored value is a hash, so the lookup never exposes
    a usable secret. Fail-closed: no keys => all ingest rejected."""
    if not presented:
        return None
    now = int(time.time())
    with LOCK:
        row = DB.execute("SELECT id, expires_at FROM api_keys WHERE key_hash=?",
                         (_hash_key(presented),)).fetchone()
        if not row:
            return None
        kid, exp = row
        if exp and exp < now:
            return None
        DB.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (now, kid))
        DB.commit()
    return kid

def _presented_key():
    auth = request.headers.get("Authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return request.headers.get("X-API-Key", "").strip()

def require_api_key(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        kid = _key_lookup(_presented_key())
        if not kid:
            return jsonify({"ok": False, "error": "missing, invalid, or expired API key"}), 401
        g.api_key_id = kid
        return fn(*a, **kw)
    return wrapper

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
    end = ended or int(time.time())
    kwh_per = INTERVAL / 3_600_000.0
    e_kwh = cost = sum_p = 0.0
    n = 0
    peak_u = 0.0
    for ts, util, power in cur.execute(
            "SELECT ts,util,power FROM samples WHERE ts>=? AND ts<=? AND power IS NOT NULL",
            (started, end)):
        p = power or 0.0
        sum_p += p; n += 1; peak_u = max(peak_u, util or 0)
        e_kwh += p * kwh_per
        cost += p * kwh_per * (_price_at(ctx, ts) or 0.0)
    return round(e_kwh, 4), round(cost, 4), (round(sum_p / n) if n else 0), round(peak_u)

@app.route("/api/integration/keys", methods=["GET", "POST"])
def api_keys_route():
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
        kid, key = _create_api_key(_clip(body.get("name") or "key", 128), days)
        return jsonify({"ok": True, "id": kid, "key": key})
    now = int(time.time())
    with LOCK:
        rows = DB.execute("SELECT id,name,prefix,created_at,expires_at,last_used_at "
                          "FROM api_keys ORDER BY created_at DESC").fetchall()
        counts = dict(DB.execute("SELECT key_id, COUNT(*) FROM runs WHERE key_id IS NOT NULL "
                                 "GROUP BY key_id").fetchall())
    keys = [{"id": kid, "name": name, "prefix": prefix, "created_at": created,
             "expires_at": exp, "last_used_at": used, "expired": bool(exp and exp < now),
             "runs": counts.get(kid, 0)}
            for (kid, name, prefix, created, exp, used) in rows]
    return jsonify({"keys": keys, "has_key": bool(keys)})

@app.route("/api/integration/keys/<kid>", methods=["DELETE"])
def api_keys_delete(kid):
    """Revoke (remove) a key. Runs it pushed are kept; they just lose live attribution."""
    with LOCK:
        cur = DB.execute("DELETE FROM api_keys WHERE id=?", (kid,))
        DB.commit()
    return (jsonify({"ok": True}) if cur.rowcount
            else (jsonify({"ok": False, "error": "unknown key"}), 404))

@app.route("/api/runs", methods=["POST"])
@require_api_key
def api_runs_create():
    body = request.get_json(silent=True) or {}
    source = (body.get("source") or "api").lower()
    if source not in RUN_SOURCES:
        source = "api"
    now = int(time.time())
    rid = (body.get("id") or uuid.uuid4().hex)[:64]
    try:
        params = _json_field(body.get("params"), MAX_RUN_JSON)
        tags = _json_field(body.get("tags"), MAX_RUN_JSON)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 413
    with LOCK:
        DB.execute("INSERT INTO runs(id,name,source,status,started_at,ended_at,host,params,tags,notes,"
                   "heartbeat_at,ext_id,created_at,key_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                   "ON CONFLICT(id) DO NOTHING",
                   (rid, _clip(body.get("name") or "run", MAX_RUN_FIELD), source, "running",
                    int(body.get("started_at") or now), None, _clip(body.get("host"), 256),
                    params, tags, _clip(body.get("notes"), MAX_RUN_FIELD), now, None, now,
                    getattr(g, "api_key_id", None)))
        DB.commit()
    return jsonify({"ok": True, "id": rid})

@app.route("/api/runs/<rid>", methods=["PATCH"])
@require_api_key
def api_runs_update(rid):
    body = request.get_json(silent=True) or {}
    sets, args = ["heartbeat_at=?"], [int(time.time())]
    if body.get("status") in RUN_STATUS:
        sets.append("status=?"); args.append(body["status"])
    if body.get("ended_at"):
        sets.append("ended_at=?"); args.append(int(body["ended_at"]))
    for f, n in (("name", MAX_RUN_FIELD), ("notes", MAX_RUN_FIELD)):
        if f in body:
            sets.append(f"{f}=?"); args.append(_clip(body[f], n))
    try:
        for f in ("params", "tags"):
            if f in body:
                sets.append(f"{f}=?"); args.append(_json_field(body[f], MAX_RUN_JSON))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 413
    args.append(rid)
    with LOCK:
        cur = DB.execute(f"UPDATE runs SET {','.join(sets)} WHERE id=?", args)
        DB.commit()
    return (jsonify({"ok": True}) if cur.rowcount else (jsonify({"ok": False, "error": "unknown run"}), 404))

@app.route("/api/runs/<rid>/metrics", methods=["POST"])
@require_api_key
def api_runs_metrics(rid):
    body = request.get_json(silent=True) or {}
    pts = body.get("metrics")
    if pts is None and "key" in body:
        pts = [body]
    pts = pts or []
    if len(pts) > MAX_METRICS_REQ:
        return jsonify({"ok": False, "error": f"max {MAX_METRICS_REQ} points/request"}), 413
    now = int(time.time())
    rows = []
    for p in pts:
        try:
            rows.append((rid, int(p.get("ts") or now), int(p.get("step") or 0),
                         _clip(p["key"], 128), float(p["value"])))
        except (KeyError, TypeError, ValueError):
            continue
    with LOCK:
        if not DB.execute("SELECT 1 FROM runs WHERE id=?", (rid,)).fetchone():
            return jsonify({"ok": False, "error": "unknown run"}), 404
        if rows:
            DB.executemany("INSERT INTO run_metrics(run_id,ts,step,key,value) VALUES(?,?,?,?,?)", rows)
            DB.execute("UPDATE runs SET heartbeat_at=? WHERE id=?", (now, rid))
            DB.commit()
    return jsonify({"ok": True, "logged": len(rows)})

@app.route("/api/runs/<rid>/finish", methods=["POST"])
@require_api_key
def api_runs_finish(rid):
    body = request.get_json(silent=True) or {}
    status = body.get("status") if body.get("status") in RUN_STATUS else "finished"
    if status == "running":
        status = "finished"
    ended = int(body.get("ended_at") or time.time())
    with LOCK:
        cur = DB.execute("UPDATE runs SET status=?, ended_at=?, heartbeat_at=? WHERE id=?",
                         (status, ended, ended, rid))
        DB.commit()
    return (jsonify({"ok": True, "id": rid, "status": status}) if cur.rowcount
            else (jsonify({"ok": False, "error": "unknown run"}), 404))

@app.route("/api/runs/<rid>", methods=["DELETE"])
def api_runs_delete(rid):
    """Remove a run and its logged metrics. A same-origin browser management action
    (like deleting a host or an API key), so it's open on the LAN rather than
    key-gated — the key gates *ingest* (forgery from notebooks), not housekeeping."""
    with LOCK:
        cur = DB.execute("DELETE FROM runs WHERE id=?", (rid,))
        DB.execute("DELETE FROM run_metrics WHERE run_id=?", (rid,))
        DB.commit()
    return (jsonify({"ok": True}) if cur.rowcount
            else (jsonify({"ok": False, "error": "unknown run"}), 404))

@app.route("/api/runs")
def api_runs_list():
    ctx = _cost_ctx()
    rng = request.args.get("range", "7d"); span = RANGES.get(rng, 604800)
    status = request.args.get("status")
    key_filter = request.args.get("key")
    now = int(time.time()); since = 0 if span is None else now - span
    q = ("SELECT id,name,source,status,started_at,ended_at,host,params,tags,notes,key_id "
         "FROM runs WHERE (ended_at IS NULL OR ended_at>=?) AND started_at<=? ")
    args = [since, now]
    if status in RUN_STATUS:
        q += "AND status=? "; args.append(status)
    if key_filter:
        q += "AND key_id=? "; args.append(key_filter)
    q += "ORDER BY started_at DESC LIMIT 500"
    out = []
    with LOCK:
        cur = DB.cursor()
        key_names = dict(cur.execute("SELECT id, name FROM api_keys").fetchall())
        for (rid, name, source, st, started, ended, host, params, tags, notes, key_id) in cur.execute(q, args).fetchall():
            e_kwh, cost, avg_w, peak_u = _run_cost_window(cur, started, ended, ctx)
            kv = {}
            # latest value per key = the last-logged row (max rowid), robust even when
            # several points share a timestamp.
            for k, v in cur.execute(
                    "SELECT key, value FROM run_metrics WHERE run_id=? AND rowid IN "
                    "(SELECT MAX(rowid) FROM run_metrics WHERE run_id=? GROUP BY key)", (rid, rid)):
                kv[k] = v
            out.append({"id": rid, "name": name, "source": source, "status": st,
                        "started_at": started, "ended_at": ended, "duration": (ended or now) - started,
                        "host": host, "params": _safe_json(params), "tags": _safe_json(tags), "notes": notes,
                        "key_id": key_id, "key_name": key_names.get(key_id),
                        "metrics_latest": kv, "energy_kwh": e_kwh, "cost": cost, "avg_w": avg_w, "peak_util": peak_u})
    return jsonify({"range": rng, "currency": ctx["currency"], "tariff_mode": ctx["mode"], "runs": out})

@app.route("/api/runs/<rid>")
def api_runs_get(rid):
    ctx = _cost_ctx()
    now = int(time.time())
    with LOCK:
        cur = DB.cursor()
        r = cur.execute("SELECT id,name,source,status,started_at,ended_at,host,params,tags,notes "
                        "FROM runs WHERE id=?", (rid,)).fetchone()
        if not r:
            return jsonify({"error": "unknown run"}), 404
        (rid, name, source, st, started, ended, host, params, tags, notes) = r
        end = ended or now
        metrics = {}
        for k, ts, step, v in cur.execute(
                "SELECT key,ts,step,value FROM run_metrics WHERE run_id=? ORDER BY key,ts,step", (rid,)):
            d = metrics.setdefault(k, {"steps": [], "ts": [], "values": []})
            d["steps"].append(step); d["ts"].append(ts); d["values"].append(v)
        bk = max(INTERVAL, round(max(1, end - started) / MAX_POINTS))
        labels, power_w, util_pct = [], [], []
        for b, ap, au in cur.execute("SELECT (ts/?)*? b, AVG(power), AVG(util) FROM samples "
                                     "WHERE ts>=? AND ts<=? GROUP BY b ORDER BY b", (bk, bk, started, end)):
            labels.append(int(b)); power_w.append(round(ap or 0)); util_pct.append(round(au or 0))
        e_kwh, cost, avg_w, peak_u = _run_cost_window(cur, started, end, ctx)
    return jsonify({"id": rid, "name": name, "source": source, "status": st,
                    "started_at": started, "ended_at": ended, "duration": end - started, "host": host,
                    "params": _safe_json(params), "tags": _safe_json(tags), "notes": notes, "metrics": metrics,
                    "resource": {"labels": labels, "power_w": power_w, "util_pct": util_pct, "bucket_sec": bk},
                    "energy_kwh": e_kwh, "cost": cost, "avg_w": avg_w, "peak_util": peak_u,
                    "currency": ctx["currency"], "tariff_mode": ctx["mode"]})

# ── MLflow sync (pull) — mirror MLflow runs in as source='mlflow', pure REST ───
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
            with LOCK:
                row = DB.execute("SELECT id FROM runs WHERE source='mlflow' AND ext_id=?", (ext,)).fetchone()
                rid = row[0] if row else uuid.uuid4().hex
                DB.execute("INSERT INTO runs(id,name,source,status,started_at,ended_at,host,params,tags,"
                           "notes,heartbeat_at,ext_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                           "ON CONFLICT(source,ext_id) DO UPDATE SET status=excluded.status, "
                           "ended_at=excluded.ended_at, name=excluded.name, params=excluded.params, "
                           "tags=excluded.tags, heartbeat_at=excluded.heartbeat_at",
                           (rid, name, "mlflow", status, started, ended, "mlflow",
                            json.dumps(params, separators=(",", ":")),
                            json.dumps(tags, separators=(",", ":")), "", now, ext, now))
                DB.execute("DELETE FROM run_metrics WHERE run_id=?", (rid,))
                DB.commit()
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

@app.route("/api/integration/mlflow/sync", methods=["GET", "POST"])
def api_mlflow_sync():
    """GET -> reachability probe (green/red). POST -> sync now."""
    if not (get_settings().get("mlflow_uri") or "").strip():
        return jsonify({"ok": False, "error": "no MLflow URI configured"}), 400
    if request.method == "POST":
        try:
            return jsonify({"ok": True, "synced": sync_mlflow()})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:200]}), 502
    try:
        _mlf("POST", "/api/2.0/mlflow/experiments/search", {"max_results": 1})
        return jsonify({"ok": True, "reachable": True})
    except Exception as e:
        return jsonify({"ok": False, "reachable": False, "error": str(e)[:200]}), 502

# ── Container logs over SSE (issue #28) ───────────────────────────────────────
_CT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")

def _docker_log_stream(name, tail, follow):
    """Yield Server-Sent Events of a container's logs over the Docker socket.
    Demuxes Docker's 8-byte stream framing (skipped for TTY containers); for
    follow=1 it keeps the connection open and emits a heartbeat every ~20s so a
    closed browser EventSource is noticed and the socket torn down cleanly."""
    c = http.client.HTTPConnection("localhost", timeout=None)
    c.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.sock.connect(DOCKER_SOCK)
    if follow:
        c.sock.settimeout(20)
    path = (f"/containers/{name}/logs?stdout=1&stderr=1&timestamps=0"
            f"&tail={tail}&follow={'1' if follow else '0'}")
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
        c.request("GET", path)
        r = c.getresponse()
        if r.status != 200:
            yield f"event: srverror\ndata: {r.status} {r.reason}\n\n"
            return
        framed, buf = None, b""
        while True:
            try:
                chunk = r.read(4096)
            except socket.timeout:
                yield ": keep-alive\n\n"          # heartbeat → Flask sees a gone client
                continue
            if not chunk:
                break
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
        if text:
            yield f"data: {text}\n\n"
        yield "event: end\ndata: done\n\n"
    finally:
        try: c.close()
        except Exception: pass

@app.route("/api/containers/<name>/logs")
def api_container_logs(name):
    """Last `tail` log lines for a container; with follow=1, streams new lines as
    SSE. Read-only — `docker logs` needs no extra socket permissions."""
    if not _CT_NAME_RE.match(name or ""):
        return jsonify({"error": "invalid container name"}), 400
    try:
        tail = max(1, min(2000, int(request.args.get("tail", 200))))
    except (TypeError, ValueError):
        tail = 200
    follow = request.args.get("follow") == "1"
    return Response(_docker_log_stream(name, tail, follow),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


# ── Structured log tail + LLM "Summarize errors" (AI-native log triage) ────────
# A Dozzle-style structured JSON tail of ONE container's logs, plus a one-click
# local-LLM summary of recent error/warn lines. The pair powers the "see the
# logs → ask the AI what's wrong" story in a single container.
#
# SECURITY (the reviewer hammers these):
#   • No command injection. The client-supplied name is RESOLVED against the live
#     container list (_resolve_container) to a known id BEFORE any use, and docker
#     is invoked via an ARGUMENT LIST (subprocess.run([...], shell=False)) — never
#     a shell string. Unknown/injection-y names get a clean 404, no shell-out.
#   • Read-only: `docker logs` only. No start/stop/exec/host-write surface.
#   • Local-only: the summarize path sends log text ONLY to the on-box ollama
#     (reuses _ollama_generate). Nothing is written to disk or logged server-side.
#   • Bounded: lines capped, bytes capped, short docker/ollama timeouts, no LOCK
#     held across the subprocess/ollama call.

_LOG_LINES_DEFAULT = 100
_LOG_LINES_CAP     = 500
_LOG_DOCKER_TIMEOUT = float(os.environ.get("LOG_DOCKER_TIMEOUT", "8"))  # seconds
_LOG_SUMMARY_BYTES_CAP = 16000   # max bytes of log text ever sent to the LLM
_LOG_ERR_RE = re.compile(r"\b(error|err|errno|fatal|fail(?:ed|ure)?|panic|exception|"
                         r"traceback|critical|crit|warn(?:ing)?|denied|refused|timeout|"
                         r"timed out|unable|cannot|could not|segfault|oom|killed)\b", re.I)


def _resolve_container(name):
    """Resolve a client-supplied container name/id to a KNOWN container id+name,
    or (None, None) if it isn't in the live set. This is the injection gate: only
    values that match a real container are ever handed to subprocess. Uses the
    cached `containers()` enumeration (already a read-only Docker-socket call)."""
    if not name or not _CT_NAME_RE.match(name):
        return None, None
    try:
        live = containers()
    except Exception:
        return None, None
    for ct in live:
        if name == ct["name"] or name == ct["id"] or ct["id"].startswith(name):
            return ct["id"], ct["name"]
    return None, None


def _parse_log_line(raw):
    """Split a `docker logs --timestamps` line into {ts?, text}. Docker prefixes
    each line with an RFC3339Nano timestamp + a space. The text is kept verbatim
    (untrusted — the UI escapes it); we only peel the leading timestamp."""
    m = re.match(r"^(\d{4}-\d\d-\d\dT[\d:.]+Z?(?:[+-]\d\d:?\d\d)?)\s(.*)$", raw, re.S)
    if m:
        return {"ts": m.group(1), "text": m.group(2)}
    return {"text": raw}


def _demux_docker_logs(blob):
    """Decode a non-follow `GET /containers/<id>/logs` body into plain text.
    Docker multiplexes stdout/stderr with an 8-byte header per frame
    (stream byte, 3×0, 4-byte big-endian length) UNLESS the container has a TTY,
    where the body is raw. Detect via the first byte (0/1/2 ⇒ framed). Decoded
    tolerantly — log bytes are untrusted (control chars / partial UTF-8)."""
    if not blob:
        return ""
    if blob[0] in (0, 1, 2):  # framed multiplexed stream
        out, i, n = [], 0, len(blob)
        while i + 8 <= n:
            size = int.from_bytes(blob[i + 4:i + 8], "big")
            i += 8
            out.append(blob[i:i + size].decode("utf-8", "replace"))
            i += size
        return "".join(out)
    return blob.decode("utf-8", "replace")


def _lines_from_text(text, lines):
    raw = text.split("\n")
    if raw and raw[-1] == "":
        raw.pop()
    truncated = len(raw) > lines
    if truncated:
        raw = raw[-lines:]
    return [_parse_log_line(ln) for ln in raw], truncated


def _docker_logs_socket(cid, lines):
    """Read the tail via the Docker Engine API over its AF_UNIX socket — the same
    read-only path the rest of the monitor uses (and what the deployed single
    image actually has; the `docker` CLI is not installed in it). The container
    id is already resolved/known (see _resolve_container) and is a 12-hex string,
    so the URL path can't be poisoned. Returns (lines, truncated, error) or
    raises so the caller can fall back to the CLI."""
    path = (f"/containers/{cid}/logs?stdout=1&stderr=1&timestamps=1"
            f"&tail={int(lines)}&follow=0")
    status, blob = _docker_req("GET", path, timeout=_LOG_DOCKER_TIMEOUT)
    if status != 200:
        # 404 = no such container (shouldn't happen post-resolve); else daemon issue.
        return [], False, "unreachable"
    parsed, truncated = _lines_from_text(_demux_docker_logs(blob), lines)
    return parsed, truncated, None


def _docker_logs_cli(cid, lines):
    """Fallback: read via the docker CLI, invoked as an ARGUMENT LIST
    (subprocess.run([...], shell=False)) so no client value is ever parsed by a
    shell. The resolved id is placed after `--` so it can never be read as a flag
    (defence in depth). Returns (lines, truncated, error). Never raises.
    error ∈ None | 'docker_missing' | 'unreachable' | 'timeout'. Output is decoded
    but NEVER logged server-side."""
    docker = shutil.which("docker")
    if not docker:
        return [], False, "docker_missing"
    args = [docker, "logs", "--tail", str(int(lines)), "--timestamps", "--", cid]
    try:
        p = subprocess.run(args, capture_output=True, shell=False,
                           timeout=_LOG_DOCKER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return [], False, "timeout"
    except (OSError, ValueError):
        return [], False, "unreachable"
    if p.returncode != 0:
        # No such container / daemon down → unreachable; never leak stderr.
        return [], False, "unreachable"
    parsed, truncated = _lines_from_text((p.stdout or b"").decode("utf-8", "replace"), lines)
    return parsed, truncated, None


def _docker_logs_tail(cid, lines):
    """Read the last `lines` log lines of a KNOWN, resolved container id. Tries
    the read-only Docker socket first (what the deployed image has), then falls
    back to the docker CLI for hosts that expose it instead. Never raises;
    returns (lines_list, truncated, error_code).

    Injection-safety is guaranteed upstream: `cid` is only ever a value
    _resolve_container matched against the live container set; both backends pass
    it as a structured value (URL path / argv after `--`), never via a shell."""
    try:
        lines_out, truncated, err = _docker_logs_socket(cid, lines)
        if err is None:
            return lines_out, truncated, None
    except Exception:
        pass  # socket missing/denied → try the CLI fallback below
    return _docker_logs_cli(cid, lines)


@app.route("/api/logs/<container>")
def api_logs_tail(container):
    """Structured JSON tail of ONE container's recent logs. Always 200 with a
    clean shape, or 404 for an unknown/invalid container — never 500, never a
    shell-out for an unresolved name. Read-only.

    Response: {ok, container, lines:[{ts?, text}], truncated, error?}"""
    try:
        n = int(request.args.get("lines", _LOG_LINES_DEFAULT))
    except (TypeError, ValueError):
        n = _LOG_LINES_DEFAULT
    n = max(1, min(_LOG_LINES_CAP, n))
    cid, cname = _resolve_container(container)
    if not cid:
        return jsonify({"ok": False, "container": container, "lines": [],
                        "truncated": False, "error": "no_such_container"}), 404
    lines, truncated, err = _docker_logs_tail(cid, n)
    return jsonify({"ok": err is None, "container": cname, "lines": lines,
                    "truncated": truncated, "error": err})


def _error_lines(lines):
    """Filter parsed log lines toward error/warn-looking ones. Falls back to the
    raw tail when nothing matches the heuristic (so summarize is never starved of
    context when logs are noisy-but-relevant)."""
    hits = [l for l in lines if _LOG_ERR_RE.search(l.get("text", ""))]
    return hits


def _log_summary_prompt(cname, snippet):
    return (
        "You are the Lab Copilot for a self-hosted homelab. The following are "
        "recent ERROR and WARNING log lines from the container '" + cname + "'. "
        "In 2-4 sentences of plain English (no markdown, no bullet points), say "
        "what is going wrong and the most likely cause. Be concrete; if the lines "
        "are benign or inconclusive, say so plainly. Do not invent details beyond "
        "the logs.\n\nLOG LINES:\n" + snippet + "\n\nSUMMARY:")


@app.route("/api/logs/<container>/summarize", methods=["POST"])
def api_logs_summarize(container):
    """One-click local-LLM triage of a container's recent error/warn logs.
    Always 200 (or 404 for an unknown container). Graceful-degrades exactly like
    the copilot: clear `llm_status` ('disabled'/'unreachable'/...) or a
    'no_errors' state. The log text is sent ONLY to the on-box ollama via
    _ollama_generate — never to any external service, never written to disk or
    logged server-side."""
    cid, cname = _resolve_container(container)
    if not cid:
        return jsonify({"ok": False, "container": container, "summary": "",
                        "source": "none", "llm_status": "no_such_container"}), 404
    # Pull a generous tail so the error filter has material, then narrow.
    lines, _trunc, err = _docker_logs_tail(cid, _LOG_LINES_CAP)
    out = {"ok": True, "container": cname, "model": COPILOT_MODEL,
           "enabled": COPILOT_ENABLED}
    if err is not None:
        out.update({"summary": "", "source": "none", "llm_status": err,
                    "error_lines": 0})
        return jsonify(out)
    errs = _error_lines(lines)
    out["error_lines"] = len(errs)
    if not errs:
        out.update({"summary": "", "source": "none", "llm_status": "no_errors"})
        return jsonify(out)
    # Keep the most recent error lines, bounded by bytes sent to the LLM.
    snippet, used = "", []
    for l in reversed(errs):
        piece = (l.get("text") or "").strip()
        if not piece:
            continue
        if len(snippet) + len(piece) + 1 > _LOG_SUMMARY_BYTES_CAP:
            break
        used.append(piece)
    used.reverse()
    snippet = "\n".join(used)[:_LOG_SUMMARY_BYTES_CAP]
    text, gerr = _ollama_generate(_log_summary_prompt(cname, snippet))
    if text is not None:
        out.update({"summary": text, "source": "llm", "llm_status": "ok"})
    else:
        out.update({"summary": "", "source": "none", "llm_status": gerr})
    return jsonify(out)


# ── Container / service controls (opt-in, OFF by default) ─────────────────────
# start / stop / restart for Docker containers and systemd units. The ONLY host-
# mutating surface besides self-update, and it is HARD-GATED behind ENABLE_CONTROLS
# (default OFF — the live arena runs OFF, so these are inert there).
#
# SECURITY (the reviewer verifies each — this touches the real host):
#   • OFF by default. With ENABLE_CONTROLS unset/false EVERY endpoint returns a
#     clean 403 and never touches docker or D-Bus. No side-effect, no traceback.
#   • Target validation. The client value is RESOLVED against the set the monitor
#     already enumerates — containers via _resolve_container (live docker list),
#     units via _resolve_unit (HEALTH systemd inventory). Anything not in that set
#     gets a clean 404. No free-form names ever reach docker / systemd.
#   • No shell, no name-kill. Docker uses the Engine API socket (argv-free HTTP
#     path with a validated 12-hex id). systemd uses the D-Bus Manager methods
#     StartUnit / StopUnit / RestartUnit with the resolved unit NAME — never
#     pkill/killall/kill-by-name, never subprocess+shell.
#   • Idempotent + honest errors. docker/D-Bus unavailable, or target vanished →
#     a clean JSON error (no 500, no leaked paths/secrets).

def _controls_state():
    """What the dashboard needs to decide whether to render action buttons.
    docker/systemd capability is reported so the UI can grey out a control the
    host can't actually service even when the flag is on."""
    return {
        "enabled": ENABLE_CONTROLS,
        "actions": list(CONTROL_ACTIONS),
        "docker": bool((HEALTH.get("docker") or {}).get("available")),
        "systemd": bool((HEALTH.get("systemd") or {}).get("available")),
    }


def _resolve_unit(unit):
    """Resolve a client-supplied systemd unit name to a KNOWN unit name from the
    monitor's live inventory, or None. This is the injection/whitelist gate for
    the systemd control path: only a unit the monitor already enumerates is ever
    handed to D-Bus. Accepts the bare name too (`nginx` → `nginx.service`)."""
    if not unit or not isinstance(unit, str):
        return None
    # Cheap syntactic guard first — a systemd unit name is a restricted charset;
    # anything else can't match the inventory anyway and is rejected outright.
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.@:\\-]{0,255}$", unit):
        return None
    want = unit if unit.endswith(".service") else unit + ".service"
    sysd = HEALTH.get("systemd") or {}
    if not sysd.get("available"):
        return None
    for s in sysd.get("services", []):
        if s.get("name") == want:
            return s["name"]
    return None


def _systemd_control(unit, action):
    """Issue StartUnit/StopUnit/RestartUnit over the host system D-Bus (same
    jeepney path collect_systemd() reads through). `unit` is already resolved to a
    KNOWN unit name; `action` is a validated enum. Returns (ok, error) and never
    raises. No subprocess, no shell, no kill-by-name."""
    method = {"start": "StartUnit", "stop": "StopUnit", "restart": "RestartUnit"}.get(action)
    if not method:
        return False, "invalid action"
    try:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection
    except Exception:
        return False, "systemd control unavailable"
    try:
        conn = open_dbus_connection(bus="SYSTEM")
    except Exception:
        return False, "systemd control unavailable"
    try:
        mgr = DBusAddress("/org/freedesktop/systemd1", bus_name="org.freedesktop.systemd1",
                          interface="org.freedesktop.systemd1.Manager")
        # (unit_name: s, mode: s) → job object path. "replace" is systemd's normal
        # interactive mode: supersede a conflicting queued job rather than fail.
        reply = conn.send_and_get_reply(new_method_call(mgr, method, "ss", (unit, "replace")))
        if getattr(reply, "header", None) and getattr(reply.header, "message_type", None) \
                and str(reply.header.message_type).endswith("error"):
            return False, "systemd rejected the request"
        return True, None
    except Exception:
        # Never leak the D-Bus error text (may carry unit paths) — a generic
        # message is enough for the UI and safe to show.
        return False, "systemd rejected the request"
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _docker_control(cid, action):
    """POST /containers/<id>/<action> over the docker socket. `cid` is already a
    resolved, known 12-hex id (see _resolve_container) so the URL path can't be
    poisoned. Returns (ok, error). 204/304 are both success (304 = already in the
    requested state → idempotent). Never raises, never leaks internals."""
    if action not in CONTROL_ACTIONS:
        return False, "invalid action"
    try:
        status, _ = _docker_req("POST", f"/containers/{cid}/{action}", timeout=20)
    except Exception:
        return False, "docker unavailable"
    if status in (204, 304):
        return True, None
    if status == 404:
        return False, "container not found"
    return False, "docker rejected the request"


def _control_action_from_request():
    """Extract + validate the {action} enum from the request body (JSON) or form.
    Returns (action, error_response_tuple_or_None)."""
    body = request.get_json(silent=True) or {}
    action = (body.get("action") or request.form.get("action") or "").strip().lower()
    if action not in CONTROL_ACTIONS:
        return None, (jsonify({"ok": False, "error": "invalid action",
                               "actions": list(CONTROL_ACTIONS)}), 400)
    return action, None


def _audit_actor():
    """A coarse, best-effort client identifier for the audit log. Reuses only
    what Flask already has (the request's remote address) — no new PII collection,
    no tracking. Trims to a bounded length and never raises."""
    try:
        # request.remote_addr is the immediate peer; behind the arena's reverse
        # proxy that's often the proxy itself — coarse by design, that's fine.
        return (getattr(request, "remote_addr", None) or "unknown")[:45]
    except Exception:
        return "unknown"


def _record_control_audit(kind, target, action, ok, detail=""):
    """Append one row to the controls audit log (accountability for host
    mutations), then prune to the retention cap. Called from INSIDE the control
    endpoints AFTER the action resolves — recording BOTH success and failure.

    This is a side-record of an already-authorized action: it is NOT a mutation
    capability of its own, and it must NEVER break the control response. Every
    failure mode is swallowed. `detail` is expected to already be a short generic
    phrase (the same safe string the endpoint returns to the UI) — we still cap
    its length so nothing large or stray lands in the ring."""
    try:
        det = (str(detail) if detail else "")[:120]
        row = (int(time.time()), str(kind)[:16], str(target)[:200],
               str(action)[:16], "ok" if ok else "error", det, _audit_actor())
        with LOCK:
            DB.execute(
                "INSERT INTO control_audit(ts,kind,target,action,result,detail,actor) "
                "VALUES(?,?,?,?,?,?,?)", row)
            # Prune to the retention cap: keep the newest _CONTROL_AUDIT_RETENTION
            # rows, drop everything older. Bounded on every write → never grows.
            DB.execute(
                "DELETE FROM control_audit WHERE id NOT IN "
                "(SELECT id FROM control_audit ORDER BY id DESC LIMIT ?)",
                (_CONTROL_AUDIT_RETENTION,))
            DB.commit()
    except Exception:
        # A logging failure must never fail or 500 the control request.
        pass


# Whitelists for the read-only audit-log filters. Any value NOT in these sets is
# ignored gracefully (treated as "no filter") — never interpolated into SQL.
_AUDIT_FILTER_RESULTS = ("ok", "error")
_AUDIT_FILTER_ACTIONS = ("start", "stop", "restart")
_AUDIT_FILTER_KINDS   = ("container", "service")

def _audit_filters_from_request():
    """Parse the optional read-only filters on /api/controls/log(.csv):
      ?result=ok|error  ?action=start|stop|restart  ?kind=container|service
      ?since=<unix_ts>
    Returns (where_sql, params, echo) where `echo` is the sanitized filter dict
    reflected back to the client. Unknown/invalid values are dropped silently —
    only whitelisted, parametrized clauses ever reach SQL (no injection surface)."""
    where, params, echo = [], [], {}
    result = (request.args.get("result") or "").strip().lower()
    if result in _AUDIT_FILTER_RESULTS:
        where.append("result=?"); params.append(result); echo["result"] = result
    action = (request.args.get("action") or "").strip().lower()
    if action in _AUDIT_FILTER_ACTIONS:
        where.append("action=?"); params.append(action); echo["action"] = action
    kind = (request.args.get("kind") or "").strip().lower()
    if kind in _AUDIT_FILTER_KINDS:
        where.append("kind=?"); params.append(kind); echo["kind"] = kind
    since_raw = request.args.get("since")
    if since_raw is not None:
        try:
            since = int(float(since_raw))
            if since >= 0:
                where.append("ts>=?"); params.append(since); echo["since"] = since
        except (TypeError, ValueError):
            pass
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    return where_sql, params, echo

def _audit_limit_from_request():
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    return max(1, min(limit, _CONTROL_AUDIT_RETENTION))

def _query_control_audit(where_sql, params, limit):
    """Read filtered audit rows newest-first, bounded by `limit`. Read-only; never
    raises out (returns [] on any DB error)."""
    try:
        with LOCK:
            rows = DB.execute(
                "SELECT ts,kind,target,action,result,detail,actor FROM control_audit"
                + where_sql + " ORDER BY id DESC LIMIT ?",
                tuple(params) + (limit,)).fetchall()
        return [{"ts": ts, "kind": kind, "target": target, "action": action,
                 "result": result, "detail": detail or "", "actor": actor or ""}
                for ts, kind, target, action, result, detail, actor in rows]
    except Exception:
        return []

def _csv_neutralize(val):
    """Neutralize CSV/spreadsheet formula injection: any field whose first char is
    one of = + - @ (or a leading tab/CR that some parsers strip to reach them) is
    prefixed with a single quote so Excel/Sheets/LibreOffice treat it as text, not
    a formula. Everything is stringified first; the stdlib csv writer still handles
    quoting/escaping of delimiters and newlines on top of this."""
    s = "" if val is None else str(val)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s

def _control_audit_to_csv(items):
    """Render audit rows to RFC-4180 CSV (stdlib csv module). Every cell passes
    through _csv_neutralize so a crafted target/detail can't inject a spreadsheet
    formula. Same privacy contract as the JSON endpoint (detail is already the
    generic phrase the endpoint returned)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ts", "utc", "kind", "target", "action", "result", "detail", "actor"])
    for it in items:
        ts = it.get("ts")
        try:
            utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts)))
        except Exception:
            utc = ""
        w.writerow([_csv_neutralize(ts), _csv_neutralize(utc),
                    _csv_neutralize(it.get("kind")), _csv_neutralize(it.get("target")),
                    _csv_neutralize(it.get("action")), _csv_neutralize(it.get("result")),
                    _csv_neutralize(it.get("detail")), _csv_neutralize(it.get("actor"))])
    return buf.getvalue()

@app.route("/api/controls/log")
def api_controls_log():
    """Read-only controls audit log — recent host-mutation actions, newest-first.
    PRIVATE (authed LAN dashboard + API only); NEVER exposed on /status or any
    public feed. This endpoint ONLY reads: it grants no new mutation capability.

    Optional read-only filters (any combination; unknown values ignored):
      ?result=ok|error  ?action=start|stop|restart  ?kind=container|service
      ?since=<unix_ts>
    ?limit= is clamped to a sane cap. ?format=csv streams the same (filtered)
    rows as a downloadable, formula-injection-safe CSV (see /api/controls/log.csv).
    No params → byte-for-byte the pre-filter behaviour (back-compat)."""
    limit = _audit_limit_from_request()
    where_sql, params, echo = _audit_filters_from_request()
    items = _query_control_audit(where_sql, params, limit)
    if (request.args.get("format") or "").strip().lower() == "csv":
        return _controls_log_csv_response(items)
    return jsonify({"enabled": ENABLE_CONTROLS, "count": len(items),
                    "filters": echo, "items": items})

def _controls_log_csv_response(items):
    """Build the CSV download Response for the audit log. Static filename (no user
    text in headers → no header/filename injection); content is formula-safe."""
    fn = "controls-audit-log.csv"
    return Response(_control_audit_to_csv(items),
                    content_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % fn})

@app.route("/api/controls/log.csv")
def api_controls_log_csv():
    """CSV export of the (filtered) controls audit log — a portable compliance
    artifact ("every change made to the host"). Same read-only filters, same limit
    cap, and same privacy contract as /api/controls/log; formula-injection-safe."""
    limit = _audit_limit_from_request()
    where_sql, params, _ = _audit_filters_from_request()
    items = _query_control_audit(where_sql, params, limit)
    return _controls_log_csv_response(items)


@app.route("/api/containers/<name>/action", methods=["POST"])
def api_container_action(name):
    """Start/stop/restart a Docker container. Body: {action: start|stop|restart}.
    HARD-GATED behind ENABLE_CONTROLS (403 when off, the default). The target is
    validated against the live container set — unknown/injection-y names → 404."""
    if not ENABLE_CONTROLS:
        return jsonify({"ok": False, "error": "controls disabled",
                        "hint": "Set ENABLE_CONTROLS=1 to enable start/stop/restart."}), 403
    action, err = _control_action_from_request()
    if err:
        return err
    cid, cname = _resolve_container(name)
    if not cid:
        return jsonify({"ok": False, "error": "unknown container"}), 404
    ok, cerr = _docker_control(cid, action)
    # Audit the outcome (success AND failure) — a side-record only, never fatal.
    _record_control_audit("container", cname, action, ok, "" if ok else cerr)
    if not ok:
        return jsonify({"ok": False, "error": cerr, "container": cname, "action": action}), 502
    _ct_cache["at"] = 0   # force the next enumeration to reflect the new state
    return jsonify({"ok": True, "container": cname, "action": action})


@app.route("/api/services/<unit>/action", methods=["POST"])
def api_service_action(unit):
    """Start/stop/restart a systemd unit. Body: {action: start|stop|restart}.
    HARD-GATED behind ENABLE_CONTROLS (403 when off, the default). The target is
    validated against the monitor's live unit inventory — anything else → 404."""
    if not ENABLE_CONTROLS:
        return jsonify({"ok": False, "error": "controls disabled",
                        "hint": "Set ENABLE_CONTROLS=1 to enable start/stop/restart."}), 403
    action, err = _control_action_from_request()
    if err:
        return err
    resolved = _resolve_unit(unit)
    if not resolved:
        return jsonify({"ok": False, "error": "unknown service"}), 404
    ok, serr = _systemd_control(resolved, action)
    # Audit the outcome (success AND failure) — a side-record only, never fatal.
    _record_control_audit("service", resolved, action, ok, "" if ok else serr)
    if not ok:
        return jsonify({"ok": False, "error": serr, "service": resolved, "action": action}), 502
    return jsonify({"ok": True, "service": resolved, "action": action})

@app.route("/api/network")
def api_network():
    """Host NIC throughput + per-container top talkers over a range (#30). Rates
    are derived from the cumulative byte counters in net_samples, so a missed
    sample or a counter reset (reboot) never invents a spike."""
    rng = request.args.get("range", "1h")
    span = RANGES.get(rng, 3600)
    now = int(time.time())
    with LOCK:
        cur = DB.cursor()
        since = (cur.execute("SELECT MIN(ts) FROM net_samples").fetchone()[0] or now) if span is None else now - span
        rows = cur.execute("SELECT ts,iface,bytes_in,bytes_out FROM net_samples WHERE ts>=? ORDER BY iface,ts",
                           (since,)).fetchall()
    bk = max(INTERVAL, round(max(1, now - since) / MAX_POINTS))
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

    host_ifaces = sorted(i for i in series if not i.startswith("@") and not _HOST_NIC_SKIP.match(i))
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

@app.route("/healthz")
def healthz():
    """Cheap liveness probe for Docker's HEALTHCHECK and any uptime monitor.
    No DB, no locks — just a 200 with the running version so the answer is
    instant and never gets blocked behind a slow collector pass."""
    return jsonify({"status": "ok", "version": VERSION}), 200

@app.route("/api/changelog")
def api_changelog():
    """Serve the bundled CHANGELOG.md, sliced to a version range, so the dashboard's
    one-time 'what's new' modal can show exactly what shipped — straight from the
    image, no GitHub round-trip (works fully offline). Read-only.
      ?to=<ver>     newest version to include (default: the running VERSION)
      ?since=<ver>  exclusive lower bound — return every section newer than it,
                    up to `to` (the multi-version roll-up). Omit for just `to`."""
    to_v = request.args.get("to") or VERSION
    since_v = request.args.get("since")
    to_t = _parse_semver(to_v)
    since_t = _parse_semver(since_v) if since_v else None
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "CHANGELOG.md")
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return jsonify({"current": VERSION, "sections": [], "markdown": ""})
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
        sv = _parse_semver(s["version"])
        if sv > to_t:
            continue
        if since_t is not None:
            if sv > since_t:
                picked.append(s)
        else:
            picked.append(s)                 # no lower bound → just the newest <= to
            break
    md = "\n".join("\n".join(s["lines"]).rstrip() for s in picked)
    return jsonify({"current": VERSION, "to": to_v, "since": since_v,
                    "sections": [{"version": s["version"], "date": s["date"], "url": s["url"]}
                                 for s in picked],
                    "markdown": md})

@app.route("/favicon.ico")
def favicon():
    """Default-favicon URL — browsers ask for /favicon.ico even when an explicit
    <link rel="icon"> points elsewhere (during early page load, or for tabs that
    open without rendering HTML). Serve the SVG we ship in static/."""
    return app.send_static_file("favicon.svg")

_LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")

@app.route("/locales/<path:fn>")
def locales(fn):
    """Serve UI translation files (i18n, #148). The dashboard fetches
    /locales/<code>.json for any non-English locale; English is inlined, so it
    needs no fetch. send_from_directory guards against path traversal."""
    if not fn.endswith(".json"):
        return ("Not found", 404)
    try:
        resp = send_from_directory(_LOCALES_DIR, fn)
    except Exception:
        return ("Not found", 404)
    resp.headers["Cache-Control"] = "no-cache"
    return resp

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

@app.route("/api/mcp-status")
def api_mcp_status():
    return jsonify(build_mcp_status())

# ── Public read-only status page (E4) ────────────────────────────────────────
# Uptime-Kuma's signature feature: a no-auth, shareable "is my lab up?" page.
# Privacy is the hard requirement — this surface is unauthenticated, so it must
# leak NOTHING about topology or operations. We therefore build the payload from
# scratch (never pass through the rich /api/health/data dicts) and include ONLY
# aggregated, public-safe signals: an overall banner, per-subsystem up/down
# tiles, anonymized counts (N services monitored, N up), GPU busy/idle, and a
# "last updated" timestamp. Explicitly EXCLUDED: hostnames, IPs, MAC/interfaces,
# disk mountpoints/paths, OS/kernel, container/service names, models, costs,
# webhook/secret/settings, MCP internals, processes, update channel. If a field
# might leak, it isn't here.
_STATUS_RANK = {"crit": 3, "warn": 2, "info": 1, "ok": 0}
_STATUS_BANNER = {3: "down", 2: "degraded", 1: "operational", 0: "operational"}

# Heartbeat history: keys we sample + how many buckets the public bars show. State
# is the 0..3 status rank; -1 means "no data" (no sample fell in that bucket / DB
# was empty). One sample per ~5 min bucket, 30 buckets ≈ last 2.5 h of detail.
_STATHIST_KEYS   = ("overall", "gpu", "host", "containers", "services")
_STATHIST_BUCKET = 300        # seconds per heartbeat cell (~5 min)
_STATHIST_CELLS  = 30         # cells rendered per subsystem strip
# Retention extended 30→90d so the StatusPage.io-style daily ribbon below has up
# to 90 calendar days to roll up. The per-bucket heartbeat (last _STATHIST_CELLS)
# is unaffected — it only reads a recent slice. ~90d × 288 buckets/day × 5 keys
# of {ts,key,state} ints stays tiny.
_STATHIST_RETENTION = 90 * 86400   # keep ~90 days of coarse status samples
_DAILY_RIBBON_DAYS = 90       # calendar days the public uptime ribbon spans

def _status_states():
    """Derive the current per-subsystem status RANK (0 ok .. 3 down) + overall,
    reusing the same Overview logic the public tiles use. Returns a dict of
    {key: rank} for keys in _STATHIST_KEYS. Aggregated only — no names/topology.
    Never raises; warming subsystems simply contribute their 'info' rank."""
    gpu_avail = LATEST.get("gpu_avail")
    snap = {"gpu": {"util": LATEST.get("util") or 0,
                    "mem_used": LATEST.get("mem_used") or 0,
                    "mem_total": (LATEST.get("mem_total") or 24576) if gpu_avail else 0,
                    "temp": LATEST.get("temp") or 0,
                    "available": bool(gpu_avail)},
            "host": LATEST.get("host") or {}}
    docker  = HEALTH["docker"]  or {"available": False, "containers": [],
                                    "summary": {"total": 0, "running": 0, "problems": 0}}
    systemd = HEALTH["systemd"] or {"available": False, "services": [], "summary": {}}
    cards = build_overview(snap, docker, systemd)
    states, worst = {}, 0
    for c in cards:
        key = c.get("key")
        rank = _STATUS_RANK.get(c.get("status") or "info", 0)
        # Mirror the public-tile softening: a few failed systemd units while the
        # bulk still run is "degraded" (rank 2), not a whole-lab "down" (rank 3).
        if key == "services" and rank >= 3:
            up = int((systemd.get("summary") or {}).get("running", 0)) if systemd.get("available") else 0
            if up > 0:
                rank = 2
        if key in _STATHIST_KEYS:
            states[key] = rank
        worst = max(worst, rank)
    states["overall"] = worst
    return states

def sample_status_history(ts):
    """Persist one coarse status sample per ~5 min bucket. Caller MUST hold LOCK
    (it's invoked from inside the sampler's `with LOCK:` block, like the other
    history writes). Stores only {ts, key, rank} — fully aggregated/anonymized."""
    try:
        states = _status_states()
    except Exception:
        return
    bucket = ts - (ts % _STATHIST_BUCKET)
    # One row per (bucket,key): re-running within the same bucket overwrites with
    # the latest state rather than piling up duplicate rows.
    DB.execute("DELETE FROM status_history WHERE ts=? AND key IN (%s)"
               % ",".join("?" * len(states)), (bucket, *states.keys()))
    DB.executemany("INSERT INTO status_history(ts,key,state) VALUES(?,?,?)",
                   [(bucket, k, int(v)) for k, v in states.items()])

def _status_history(now):
    """Build the public heartbeat series: for each subsystem key, the last
    _STATHIST_CELLS buckets (oldest→newest) as {t, state} where state is the 0..3
    rank, or -1 for a bucket with no sample. Plus an uptime% (share of sampled
    buckets that were fully up, i.e. rank 0) and the up/total bucket counts for the
    a11y summary. Read-only; the caller invokes this OUTSIDE any held LOCK."""
    out = {}
    cur_bucket = now - (now % _STATHIST_BUCKET)
    span_start = cur_bucket - (_STATHIST_CELLS - 1) * _STATHIST_BUCKET
    try:
        with LOCK:
            rows = DB.execute(
                "SELECT ts,key,state FROM status_history WHERE ts>=? AND key IN (%s)"
                % ",".join("?" * len(_STATHIST_KEYS)),
                (span_start, *_STATHIST_KEYS)).fetchall()
    except Exception:
        rows = []
    by_key = {}
    for ts, key, state in rows:
        by_key.setdefault(key, {})[ts - (ts % _STATHIST_BUCKET)] = int(state)
    for key in _STATHIST_KEYS:
        buckets = by_key.get(key, {})
        cells, up, total = [], 0, 0
        for i in range(_STATHIST_CELLS):
            bt = span_start + i * _STATHIST_BUCKET
            st = buckets.get(bt, -1)
            cells.append({"t": bt, "s": st})
            if st >= 0:
                total += 1
                if st == 0:
                    up += 1
        out[key] = {"cells": cells, "up": up, "total": total,
                    "uptime": round(100.0 * up / total, 1) if total else None}
    return out

def _status_daily(now):
    """Roll status_history's 'overall' samples up into a per-CALENDAR-DAY ribbon
    (StatusPage.io style). For each of the last _DAILY_RIBBON_DAYS local days emit
    {d: 'YYYY-MM-DD', s: worst-rank-seen-that-day (0..3), up: up-fraction 0..1}.
    Days with no sample get s=-1 / up=None (honest 'no data' — never fake green).

    Returns {"days": [...oldest→newest...], "uptime": <pct over days WITH data>,
    "up_days": int, "total_days": int (days with ≥1 sample), "span": N}.

    Read-only; a single pass over the retained 'overall' rows. The caller invokes
    this OUTSIDE any held LOCK (it takes LOCK briefly itself, like _status_history)."""
    span = _DAILY_RIBBON_DAYS
    # Pull the window a touch wider than span days so a sample right at the edge of
    # the oldest local day is still captured regardless of tz offset.
    span_start_ts = now - span * 86400 - 86400
    try:
        with LOCK:
            rows = DB.execute(
                "SELECT ts,state FROM status_history WHERE ts>=? AND key='overall'",
                (span_start_ts,)).fetchall()
    except Exception:
        rows = []
    # Aggregate per local calendar day: worst rank + (up-bucket count, total count).
    agg = {}   # 'YYYY-MM-DD' -> [worst_rank, up_count, total_count]
    for ts, state in rows:
        st = int(state)
        if st < 0:
            continue
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        a = agg.get(day)
        if a is None:
            agg[day] = [st, 1 if st == 0 else 0, 1]
        else:
            a[0] = max(a[0], st)
            a[2] += 1
            if st == 0:
                a[1] += 1
    # Walk the last `span` local days oldest→newest off today's local midnight.
    today_mid = time.mktime(time.strptime(
        time.strftime("%Y-%m-%d", time.localtime(now)), "%Y-%m-%d"))
    days, up_days, total_days = [], 0, 0
    for i in range(span - 1, -1, -1):
        day = time.strftime("%Y-%m-%d", time.localtime(today_mid - i * 86400))
        a = agg.get(day)
        if a is None:
            days.append({"d": day, "s": -1, "up": None})
        else:
            worst, upc, tot = a
            frac = round(upc / tot, 4) if tot else None
            days.append({"d": day, "s": worst, "up": frac})
            total_days += 1
            if worst == 0:
                up_days += 1
    uptime = round(100.0 * up_days / total_days, 2) if total_days else None
    return {"days": days, "uptime": uptime, "up_days": up_days,
            "total_days": total_days, "span": span}

# ── External uptime checks (HTTP/TCP) ───────────────────────────────────────
# User-defined endpoint monitors (à la Uptime-Kuma). Read-only outbound: an HTTP
# GET/HEAD or a TCP connect to the user's OWN configured target — never mutates the
# host or the target. Each probe is bounded by its own timeout so a slow/hanging
# endpoint can never stall the metrics sampler or other checks. PRIVATE surface:
# targets/labels/errors live on the authed dashboard + API only, never on /status.
_UPTIME_MIN_INTERVAL = 20      # floor on per-check cadence (don't hammer targets)
_UPTIME_MAX_INTERVAL = 86400
_UPTIME_MAX_TIMEOUT  = 30      # hard cap so a probe can't pin a worker forever
_UPTIME_MAX_REDIRECTS = 3
_UPTIME_RESULT_CAP   = 5000    # per-check ring-buffer of results
_UPTIME_UA = "HomeLab-Monitor uptime check"
# ── SLO / error-budget ──────────────────────────────────────────────────────
# The error budget is computed over a 30-day SLO window, but honestly clamped to
# whatever results actually sit in the per-check ring buffer (a fast cadence may
# not cover a full 30 days — see _UPTIME_RESULT_CAP). Burn rate is the recent
# failure fraction over a rolling window divided by the allowed failure fraction,
# so >1 means the budget is being spent faster than the window can sustain.
_SLO_WINDOW_SEC      = 30 * 86400   # nominal 30-day SLO window
_SLO_BURN_FAST_SEC   = 3600         # short burn window (1h)
_SLO_BURN_SLOW_SEC   = 6 * 3600     # medium burn window (6h)
_SLO_MIN_SAMPLES     = 20           # below this, "collecting…" instead of a number
_SLO_MIN_SPAN_FRAC   = 0.02         # need ≥2% of the window spanned to trust the %

def _parse_slo_target(raw):
    """Parse the slo_target setting (a PERCENT string like '99.9') into an SLO
    fraction in (0, 1]. Empty/garbage/out-of-range → default 0.999. A target of
    exactly 100 (% ) is allowed and means 'no error budget' (any failure is over
    budget); handled downstream via allowed_fail == 0."""
    default = 0.999
    if raw is None:
        return default
    s = str(raw).strip().rstrip("%").strip()
    if not s:
        return default
    try:
        pct = float(s)
    except (TypeError, ValueError):
        return default
    if not (0.0 < pct <= 100.0):
        return default
    return pct / 100.0

def _uptime_slo(rows, now, target, window=_SLO_WINDOW_SEC):
    """Pure error-budget + burn-rate stats for ONE check, computed over already-
    stored (ts, up) result rows — NO poll, NO network, NO DB read here (caller
    passes the rows it already fetched). `rows` is an iterable of (ts, up[, ...])
    ascending by ts (extra columns are ignored). `target` is the SLO fraction
    (0<target<=1); `now` is epoch seconds.

    Returns a dict, always JSON-safe (no NaN/inf):
      target            SLO fraction echoed back
      allowed_fail      1 - target (0 when target == 100%)
      total, down       sample counts inside the window
      observed_fail     down/total (0 when total==0)
      budget_consumed_pct  observed_fail/allowed_fail*100, clamped to [0, 999];
                           None when allowed_fail==0 and there are no failures;
                           999 (>100 sentinel "over") when target==100% and any fail
      burn_1h, burn_6h  recent-failure-fraction / allowed_fail over the rolling
                        windows (None when allowed_fail==0 or no samples in window)
      window_days_actual  span (first→last sample) in days, rounded to 0.1
      data_sufficient   False when too few samples / span far short of the window
      over_budget       budget_consumed_pct is not None and > 100
      burning           burn_1h is not None and burn_1h > 1
    """
    samples = []
    for r in rows:
        ts = r[0]
        if ts is None or ts < now - window:
            continue
        samples.append((ts, 1 if r[1] else 0))
    total = len(samples)
    allowed_fail = max(0.0, 1.0 - float(target))
    out = {
        "target": round(float(target), 6),
        "allowed_fail": round(allowed_fail, 6),
        "total": total, "down": 0, "observed_fail": 0.0,
        "budget_consumed_pct": None,
        "burn_1h": None, "burn_6h": None,
        "window_days_actual": 0.0,
        "data_sufficient": False,
        "over_budget": False, "burning": False,
    }
    if total == 0:
        return out
    down = sum(1 for _, up in samples if not up)
    out["down"] = down
    observed_fail = down / total
    out["observed_fail"] = round(observed_fail, 6)
    span = samples[-1][0] - samples[0][0]
    out["window_days_actual"] = round(span / 86400.0, 1)
    out["data_sufficient"] = (total >= _SLO_MIN_SAMPLES and
                              span >= window * _SLO_MIN_SPAN_FRAC)
    # Budget consumed: how much of the allowed failure fraction we've eaten.
    if allowed_fail <= 0.0:               # target == 100% → no budget at all
        out["budget_consumed_pct"] = 999.0 if down else None
    else:
        out["budget_consumed_pct"] = round(min(999.0, observed_fail / allowed_fail * 100.0), 1)
    out["over_budget"] = (out["budget_consumed_pct"] is not None and
                          out["budget_consumed_pct"] > 100.0)
    # Burn rate over rolling sub-windows: recent failure fraction / allowed_fail.
    def _burn(sub):
        recent = [up for ts, up in samples if ts >= now - sub]
        if not recent or allowed_fail <= 0.0:
            return None
        fail_frac = sum(1 for up in recent if not up) / len(recent)
        return round(fail_frac / allowed_fail, 2)
    out["burn_1h"] = _burn(_SLO_BURN_FAST_SEC)
    out["burn_6h"] = _burn(_SLO_BURN_SLOW_SEC)
    out["burning"] = out["burn_1h"] is not None and out["burn_1h"] > 1.0
    return out
_UPTIME_CERT_DEFAULT_PORT = 443
_UPTIME_CERT_DEFAULT_WARN_DAYS = 21    # warn when a cert expires within this window
_UPTIME_CERT_MAX_WARN_DAYS = 365
_uptime_due = {}               # check_id -> next monotonic due time (scheduler state)

def _uptime_row_to_dict(r):
    cols = ("id", "label", "type", "target", "interval_sec", "timeout_sec",
            "expected_status", "enabled", "created_at", "cert_warn_days", "public")
    d = dict(zip(cols, r))
    d["enabled"] = bool(d["enabled"])
    d["public"] = bool(d.get("public"))
    return d

_CRED_RE = re.compile(r"(://)[^/\s:@]+:[^/\s@]+@")

def _redact_target(s):
    """Strip any `scheme://user:pass@` credentials from a string so a check target
    is safe to log/echo in an error. Storing the full target (with creds) is fine —
    like webhook_url — but we never want it in a log line or surfaced error. Works on
    bare URLs AND on error messages that merely embed a URL. host:port targets and
    credential-free URLs pass through unchanged."""
    return _CRED_RE.sub(r"\1***:***@", s or "")

def list_uptime_checks():
    with LOCK:
        rows = DB.execute(
            "SELECT id,label,type,target,interval_sec,timeout_sec,expected_status,enabled,created_at,cert_warn_days,public "
            "FROM uptime_checks ORDER BY created_at").fetchall()
    return [_uptime_row_to_dict(r) for r in rows]

def _validate_uptime_check(body):
    """Return (clean_dict, None) or (None, error_string). Rejects garbage targets,
    bad URL schemes, unparseable host:port, and out-of-range interval/timeout."""
    label = (body.get("label") or "").strip()
    if not label:
        return None, "A label is required."
    if len(label) > 120:
        return None, "Label is too long (max 120 characters)."
    ctype = (body.get("type") or "http").strip().lower()
    if ctype not in ("http", "tcp", "cert"):
        return None, "Type must be 'http', 'tcp', or 'cert'."
    target = (body.get("target") or "").strip()
    if not target:
        return None, "A target is required."
    if len(target) > 2048:
        return None, "Target is too long."
    expected = None
    cert_warn_days = None
    if ctype == "cert":
        host, port = _parse_cert_target(target)
        if host is None:
            return None, "Cert checks need a host, host:port, or https:// URL (e.g. example.com:443)."
        cwd = body.get("cert_warn_days")
        if cwd in (None, ""):
            cert_warn_days = _UPTIME_CERT_DEFAULT_WARN_DAYS
        else:
            try:
                cert_warn_days = int(cwd)
            except (TypeError, ValueError):
                return None, "Cert warning window must be a whole number of days."
            if cert_warn_days < 1:
                return None, "Cert warning window must be at least 1 day."
            if cert_warn_days > _UPTIME_CERT_MAX_WARN_DAYS:
                return None, f"Cert warning window must be at most {_UPTIME_CERT_MAX_WARN_DAYS} days."
    elif ctype == "http":
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
    return {"label": label, "type": ctype, "target": target,
            "interval_sec": interval, "timeout_sec": timeout,
            "expected_status": expected, "cert_warn_days": cert_warn_days,
            "enabled": 1 if body.get("enabled", True) else 0,
            # Opt-in public-status visibility — OFF unless explicitly requested.
            "public": 1 if body.get("public", False) else 0}, None

def _parse_host_port(target):
    """Parse 'host:port' (the tcp check target). Returns (host, port) or (None, None).
    Accepts a leading tcp:// scheme and bracketed IPv6 literals."""
    t = target.strip()
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

def _parse_cert_target(target):
    """Parse a cert-check target into (host, port). Accepts:
      - 'host'              → port defaults to 443
      - 'host:port'
      - 'https://host[:port][/path]' (scheme/path ignored; host[+port] taken)
    Returns (host, port) or (None, None) on garbage. IPv6 literals use [..]:port."""
    t = (target or "").strip()
    if not t:
        return None, None
    if "://" in t:
        u = urllib.parse.urlsplit(t)
        host = u.hostname
        if not host:
            return None, None
        port = u.port or _UPTIME_CERT_DEFAULT_PORT
        if not (1 <= port <= 65535):
            return None, None
        return host, port
    # Bracketed IPv6 literal, optionally with :port.
    if t.startswith("[") and "]" in t:
        host, _, rest = t[1:].partition("]")
        host = host.strip()
        if not host:
            return None, None
        if not rest:
            return host, _UPTIME_CERT_DEFAULT_PORT
        if not rest.startswith(":"):
            return None, None
        try:
            port = int(rest[1:])
        except (TypeError, ValueError):
            return None, None
        if not (1 <= port <= 65535):
            return None, None
        return host, port
    # bare host or host:port (a bare IPv6 with multiple colons is rejected — use [..])
    if t.count(":") == 1:
        host, _, portstr = t.partition(":")
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
    if ":" in t:                 # bare unbracketed IPv6 / multi-colon garbage
        return None, None
    if any(ch.isspace() for ch in t):
        return None, None
    return t, _UPTIME_CERT_DEFAULT_PORT

def create_uptime_check(body):
    clean, err = _validate_uptime_check(body)
    if err:
        return None, err
    cid = uuid.uuid4().hex
    with LOCK:
        DB.execute(
            "INSERT INTO uptime_checks(id,label,type,target,interval_sec,timeout_sec,"
            "expected_status,enabled,created_at,cert_warn_days,public) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (cid, clean["label"], clean["type"], clean["target"], clean["interval_sec"],
             clean["timeout_sec"], clean["expected_status"], clean["enabled"],
             int(time.time()), clean["cert_warn_days"], clean["public"]))
        DB.commit()
    _uptime_due.pop(cid, None)   # probe promptly on next scheduler pass
    return cid, None

def update_uptime_check(cid, body):
    with LOCK:
        exists = DB.execute("SELECT 1 FROM uptime_checks WHERE id=?", (cid,)).fetchone()
    if not exists:
        return False, "not found"
    if not body:
        return False, "empty update"
    # Quick enable/disable and/or public-toggle without full revalidation. A
    # body limited to {enabled, public} (either or both) is a lightweight flag
    # flip — the public flag is opt-in and never changes from a full edit unless
    # explicitly present, so this keeps the toggles independent of the config form.
    if body and set(body.keys()) <= {"enabled", "public"}:
        sets, params = [], []
        if "enabled" in body:
            sets.append("enabled=?"); params.append(1 if body.get("enabled") else 0)
        if "public" in body:
            sets.append("public=?"); params.append(1 if body.get("public") else 0)
        params.append(cid)
        with LOCK:
            DB.execute(f"UPDATE uptime_checks SET {','.join(sets)} WHERE id=?", params)
            DB.commit()
        return True, None
    clean, err = _validate_uptime_check(body)
    if err:
        return False, err
    # A full config edit must NOT silently change public visibility: only flip
    # the public flag when the body explicitly carries it; otherwise preserve the
    # check's current value (opt-in stays sticky across ordinary edits).
    if "public" not in body:
        with LOCK:
            row = DB.execute("SELECT public FROM uptime_checks WHERE id=?", (cid,)).fetchone()
        clean["public"] = int(row[0]) if row and row[0] is not None else 0
    with LOCK:
        DB.execute(
            "UPDATE uptime_checks SET label=?,type=?,target=?,interval_sec=?,timeout_sec=?,"
            "expected_status=?,enabled=?,cert_warn_days=?,public=? WHERE id=?",
            (clean["label"], clean["type"], clean["target"], clean["interval_sec"],
             clean["timeout_sec"], clean["expected_status"], clean["enabled"],
             clean["cert_warn_days"], clean["public"], cid))
        DB.commit()
    _uptime_due.pop(cid, None)   # re-probe with new config promptly
    return True, None

def delete_uptime_check(cid):
    with LOCK:
        cur = DB.execute("DELETE FROM uptime_checks WHERE id=?", (cid,))
        DB.execute("DELETE FROM uptime_results WHERE check_id=?", (cid,))
        DB.commit()
    _uptime_due.pop(cid, None)
    return cur.rowcount > 0

def probe_http(target, timeout, expected=None):
    """GET (HEAD fallback) the URL, following ≤ _UPTIME_MAX_REDIRECTS redirects.
    Returns (up, latency_ms, code, err). up = connected AND status matches expected
    (or any 2xx/3xx if expected unset). Never raises; bounded by `timeout`. The
    error string is redacted of any embedded credentials before it leaves here."""
    start = time.monotonic()
    try:
        try:
            code = _http_probe_once(target, timeout, "GET")
        except urllib.error.HTTPError as he:
            code = he.code            # a 4xx/5xx still answered — that's a real status
        latency = round((time.monotonic() - start) * 1000, 1)
        if expected is not None:
            up = (code == expected)
        else:
            up = (200 <= code < 400)
        return up, latency, code, (None if up else f"HTTP {code}")
    except Exception as e:
        latency = round((time.monotonic() - start) * 1000, 1)
        return False, latency, None, _redact_target(str(e))[:200]

def _http_probe_once(target, timeout, method):
    """One bounded HTTP request following a couple redirects manually (so we never
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

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface redirects as HTTPError so probe_http counts/bounds them itself."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def probe_tcp(target, timeout):
    """socket.create_connection to host:port, bounded by `timeout`. up = connects.
    Returns (up, latency_ms, None, err). Never raises."""
    start = time.monotonic()
    host, port = _parse_host_port(target)
    if host is None:
        return False, None, None, "bad host:port"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            latency = round((time.monotonic() - start) * 1000, 1)
            return True, latency, None, None
    except Exception as e:
        latency = round((time.monotonic() - start) * 1000, 1)
        return False, latency, None, str(e)[:200]

def _parse_cert_not_after(s):
    """Parse the OpenSSL notAfter string (e.g. 'Jun  1 12:00:00 2026 GMT') into a
    POSIX timestamp (UTC). Returns float seconds or None on unparseable input."""
    if not s:
        return None
    try:
        return calendar.timegm(time.strptime(s, "%b %d %H:%M:%S %Y %Z"))
    except (ValueError, OverflowError):
        return None

def _cert_cn(name_tuples):
    """Pull the commonName out of an ssl cert subject/issuer structure
    (a tuple of RDN tuples-of-pairs). Returns the CN string or None."""
    try:
        for rdn in (name_tuples or ()):
            for k, v in rdn:
                if k == "commonName":
                    return v
    except Exception:
        pass
    return None

def probe_cert(target, timeout, warn_days, verify=True):
    """Open a TLS handshake to the cert-check target (SNI = host), read the peer
    certificate, and compute days-to-expiry. Returns
    (up, latency_ms, days_to_expiry, err, extra) where:
      up=True  → cert valid AND > warn_days from expiry
      up=False → cert EXPIRED, expiring within warn_days (warn/degraded), OR the
                 handshake failed (verify error / refused / timeout / no cert).
    `extra` carries {not_after, subject_cn, issuer_cn, expiring} for the result/UI.
    Never raises; strictly bounded by `timeout`. Pure stdlib ssl/socket. The cert
    fetch is read-only — same trust as the existing http/tcp probes."""
    start = time.monotonic()
    host, port = _parse_cert_target(target)
    if host is None:
        return False, None, None, "bad cert target", {}
    extra = {}
    try:
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                cert = ss.getpeercert()
    except Exception as e:
        latency = round((time.monotonic() - start) * 1000, 1)
        return False, latency, None, _redact_target(str(e))[:200], extra
    latency = round((time.monotonic() - start) * 1000, 1)
    if not cert:
        # verify-disabled handshakes return {} — no structured cert to read.
        return False, latency, None, "no certificate presented", extra
    not_after_raw = cert.get("notAfter")
    exp = _parse_cert_not_after(not_after_raw)
    if exp is None:
        return False, latency, None, "could not parse certificate expiry", extra
    days = int(math.floor((exp - time.time()) / 86400.0))
    extra["not_after"] = not_after_raw
    extra["subject_cn"] = _cert_cn(cert.get("subject"))
    extra["issuer_cn"] = _cert_cn(cert.get("issuer"))
    warn = warn_days if warn_days is not None else _UPTIME_CERT_DEFAULT_WARN_DAYS
    if days < 0:
        return False, latency, days, f"certificate expired {abs(days)}d ago", extra
    if days <= warn:
        # Expiring within the warning window: degraded/warn — recorded as up=1 with
        # a day count so the UI + exporter can flag it, while uptime_down stays
        # reserved for hard failures (expired / unreachable), mirroring http/tcp.
        extra["expiring"] = True
        return True, latency, days, f"certificate expires in {days}d", extra
    return True, latency, days, None, extra

def run_uptime_check(check):
    """Execute one check (dict) and persist its result. Returns the result dict.
    The probe itself is bounded by the check's timeout; the DB write is the only
    LOCK held, and it's brief. Called from the dedicated uptime worker thread."""
    ctype = check["type"]
    timeout = min(int(check.get("timeout_sec") or 10), _UPTIME_MAX_TIMEOUT)
    days_to_expiry = None
    cert_extra_json = None
    if ctype == "tcp":
        up, latency, code, err = probe_tcp(check["target"], timeout)
    elif ctype == "cert":
        up, latency, days_to_expiry, err, extra = probe_cert(
            check["target"], timeout, check.get("cert_warn_days"))
        code = None
        # Persist the public cert metadata (issuer/subject CN, exact notAfter) so
        # the UI + cert_expiry alert can show the date — not just a day count.
        # NULL when the probe yielded nothing (handshake failure → extra == {}).
        if extra:
            try:
                cert_extra_json = json.dumps(extra)
            except (TypeError, ValueError):
                cert_extra_json = None
    else:
        up, latency, code, err = probe_http(check["target"], timeout, check.get("expected_status"))
    ts = int(time.time())
    if not _DB_MAINTENANCE:
        try:
            with LOCK:
                DB.execute("INSERT INTO uptime_results(check_id,ts,up,latency_ms,code,err,days_to_expiry,cert_extra) "
                           "VALUES(?,?,?,?,?,?,?,?)",
                           (check["id"], ts, 1 if up else 0, latency, code, err, days_to_expiry, cert_extra_json))
                # Per-check ring buffer: keep only the newest CAP rows. Trim by rowid
                # (monotonic + unique) so it's exact even when many results share a
                # second — a ts-based MIN() would under-trim on timestamp collisions.
                DB.execute(
                    "DELETE FROM uptime_results WHERE check_id=? AND rowid NOT IN "
                    "(SELECT rowid FROM uptime_results WHERE check_id=? "
                    "ORDER BY rowid DESC LIMIT ?)",
                    (check["id"], check["id"], _UPTIME_RESULT_CAP))
                DB.commit()
        except Exception as e:
            print("uptime persist error:", e, flush=True)
    return {"ts": ts, "up": up, "latency_ms": latency, "code": code, "err": err,
            "days_to_expiry": days_to_expiry}

def _uptime_state(check_id, now, window=86400):
    """Read-only summary for one check over `window` seconds: current state
    (up/down/unknown), last latency, uptime%, last_checked, last_err, and a coarse
    heartbeat strip. Caller must NOT hold LOCK (this takes it briefly)."""
    since = now - window
    with LOCK:
        rows = DB.execute(
            "SELECT ts,up,latency_ms,code,err FROM uptime_results WHERE check_id=? AND ts>=? "
            "ORDER BY ts", (check_id, since)).fetchall()
        last = DB.execute(
            "SELECT ts,up,latency_ms,code,err,days_to_expiry,cert_extra FROM uptime_results WHERE check_id=? "
            "ORDER BY ts DESC LIMIT 1", (check_id,)).fetchone()
    total = len(rows)
    up_n = sum(1 for r in rows if r[1])
    uptime = round(100.0 * up_n / total, 2) if total else None
    state = "unknown"
    last_latency = last_checked = last_err = last_code = None
    days_to_expiry = None
    cert_extra = None
    if last:
        state = "up" if last[1] else "down"
        last_checked = last[0]
        last_latency = last[2]
        last_code = last[3]
        last_err = last[4]
        days_to_expiry = last[5]
        cert_extra = last[6]
    # Heartbeat strip: most recent up-to-_STATHIST_CELLS results, oldest→newest,
    # carrying only {up, ts} — same visual language as the /status bars.
    strip = [{"up": bool(r[1]), "t": r[0]} for r in rows[-_STATHIST_CELLS:]]
    out = {"state": state, "uptime": uptime, "window_total": total,
           "last_latency_ms": last_latency, "last_checked": last_checked,
           "last_code": last_code, "last_err": last_err,
           "days_to_expiry": days_to_expiry, "strip": strip}
    # Surface the persisted cert metadata (issuer/subject CN + exact expiry date)
    # for cert checks. Defensive: NULL / corrupt / pre-migration rows → omit the
    # fields entirely so older rows + non-cert checks read cleanly. The raw notAfter
    # is public cert metadata (not a secret); the connection target stays redacted.
    if cert_extra:
        try:
            ce = json.loads(cert_extra)
        except (ValueError, TypeError):
            ce = None
        if isinstance(ce, dict):
            na = ce.get("not_after")
            if na:
                out["not_after"] = na
                na_ts = _parse_cert_not_after(na)
                if na_ts is not None:
                    out["not_after_ts"] = int(na_ts)
            if ce.get("subject_cn"):
                out["subject_cn"] = ce["subject_cn"]
            if ce.get("issuer_cn"):
                out["issuer_cn"] = ce["issuer_cn"]
    return out

def _uptime_slo_for(check_id, now, target):
    """Fetch the SLO-window (ts, up) rows for one check and run _uptime_slo over
    them. Read-only; takes LOCK briefly. Separate from _uptime_state's 1-day read
    because the SLO window (30d) is wider — but still a single bounded query."""
    since = now - _SLO_WINDOW_SEC
    with LOCK:
        rows = DB.execute(
            "SELECT ts,up FROM uptime_results WHERE check_id=? AND ts>=? ORDER BY ts",
            (check_id, since)).fetchall()
    return _uptime_slo(rows, now, target)

def uptime_overview(window=86400):
    """All checks + their current state. The user-facing private payload."""
    now = int(time.time())
    target = _parse_slo_target(get_settings().get("slo_target"))
    out = []
    for c in list_uptime_checks():
        st = _uptime_state(c["id"], now, window)
        rec = {**c, **st}
        # SLO error-budget + burn-rate (private surface only; never on /status).
        rec["slo"] = _uptime_slo_for(c["id"], now, target)
        # Cert checks expose a soft "warn" when up-but-expiring within the window —
        # state stays 'up' (so uptime_down stays reserved for hard failures), but
        # the UI/exporter can flag the imminent expiry. A hard-expired cert reads
        # as state 'down' already and fires uptime_down like any other failure.
        if rec.get("type") == "cert":
            warn = rec.get("cert_warn_days")
            warn = warn if warn is not None else _UPTIME_CERT_DEFAULT_WARN_DAYS
            d = rec.get("days_to_expiry")
            rec["cert_warn"] = bool(rec.get("state") == "up" and d is not None and d <= warn)
        out.append(rec)
    return {"checks": out, "now": now, "window": window,
            "min_interval": _UPTIME_MIN_INTERVAL, "max_timeout": _UPTIME_MAX_TIMEOUT}

def _uptime_tick(now=None):
    """One scheduler pass: probe every ENABLED check whose interval is due. Each
    probe is bounded by its own timeout, so a hanging endpoint can't stall the rest
    — and this runs on a DEDICATED thread, never the metrics sampler. Returns the
    list of check ids probed this pass (handy for tests)."""
    now = time.monotonic() if now is None else now
    probed = []
    for c in list_uptime_checks():
        if not c["enabled"]:
            _uptime_due.pop(c["id"], None)
            continue
        due = _uptime_due.get(c["id"], 0)
        if now < due:
            continue
        try:
            run_uptime_check(c)
        except Exception as e:
            print("uptime check error:", e, flush=True)
        _uptime_due[c["id"]] = now + max(_UPTIME_MIN_INTERVAL, int(c["interval_sec"]))
        probed.append(c["id"])
    return probed

def uptime_worker():
    """Dedicated daemon loop: wakes every few seconds, probes due checks. Kept off
    the collector thread so a slow/hanging probe never delays metric sampling. Inert
    (zero outbound) when no checks are configured/enabled."""
    while True:
        try:
            _uptime_tick()
        except Exception as e:
            print("uptime_worker error:", e, flush=True)
        time.sleep(5)

def build_public_status():
    """Aggregate, privacy-safe health snapshot for the public /status page.
    Read-only; derives from the same cached signals the Overview tab uses but
    emits only non-sensitive, anonymized fields. Never raises (returns a safe
    'unknown' shape if a subsystem is still warming up)."""
    now = int(time.time())
    gpu_avail = LATEST.get("gpu_avail")
    snap = {"gpu": {"util": LATEST.get("util") or 0,
                    "mem_used": LATEST.get("mem_used") or 0,
                    "mem_total": (LATEST.get("mem_total") or 24576) if gpu_avail else 0,
                    "temp": LATEST.get("temp") or 0,
                    "available": bool(gpu_avail)},
            "host": LATEST.get("host") or {}}
    docker  = HEALTH["docker"]  or {"available": False, "containers": [],
                                    "summary": {"total": 0, "running": 0, "problems": 0}}
    systemd = HEALTH["systemd"] or {"available": False, "services": [], "summary": {}}
    cards = build_overview(snap, docker, systemd)   # reuses the same status logic

    # Per-subsystem tiles — keep ONLY the safe label + status + a coarse,
    # anonymized count summary. Drop the human-readable `metric`/`detail` strings
    # from build_overview (they carry CPU%/disk%/RAM% specifics we don't publish).
    tiles, worst = [], 0
    for c in cards:
        key, st = c.get("key"), c.get("status") or "info"
        rank = _STATUS_RANK.get(st, 0)
        tile = {"key": key, "status": st}
        if key == "containers" and docker.get("available"):
            s = docker["summary"]
            tile["up"] = int(s.get("running", 0))
            tile["total"] = int(s.get("total", 0))
            tile["problems"] = int(s.get("problems", 0))
        elif key == "services" and systemd.get("available"):
            s = systemd["summary"]
            tile["up"] = int(s.get("running", 0))
            tile["failed"] = int(s.get("failed", 0))
            # A handful of failed systemd units while the bulk still run is a
            # "degraded" condition, not a whole-lab "down" — only let services
            # paint the public banner red when nothing is actually running.
            if rank >= 3 and tile["up"] > 0:
                rank = 2
        elif key == "gpu":
            tile["busy"] = bool((snap["gpu"]["util"] or 0) >= 5)
        worst = max(worst, rank)
        tiles.append(tile)

    # Headline counts — anonymized aggregates only (no names, no identities).
    # Use the same `summary.running` basis the services tile reports as `up`, so the
    # KPI strip ("N monitored") and the per-subsystem tile agree. The `services`
    # list is a curated subset (admin/port-bearing units), which would undercount
    # the headline vs. the tile and read as nonsensical (e.g. "92 up of 11").
    n_services = int((systemd.get("summary") or {}).get("running", 0)) if systemd.get("available") else 0
    n_containers = int((docker.get("summary") or {}).get("total", 0)) if docker.get("available") else 0
    n_problems = (int((docker.get("summary") or {}).get("problems", 0)) if docker.get("available") else 0) \
                 + (int((systemd.get("summary") or {}).get("failed", 0)) if systemd.get("available") else 0)

    # Is any anomaly currently firing? (boolean only — no series/values exposed.)
    anomaly_active = False
    try:
        with LOCK:
            anom = _zscore_anomalies(DB.cursor(), now)
        anomaly_active = bool(anom.get("items"))
        if anomaly_active and worst < 2:
            worst = 2   # an active anomaly degrades the banner
    except Exception:
        pass

    gpu = snap["gpu"]
    gpu_pub = {"available": gpu["available"]}
    if gpu["available"]:
        gpu_pub["busy"] = bool((gpu["util"] or 0) >= 5)
        gpu_pub["util_pct"] = round(gpu["util"] or 0)

    # Heartbeat history — aggregated up/down per anonymized subsystem key (and an
    # overall series). Attached to each tile + surfaced top-level so the public
    # page can paint Uptime-Kuma-style bars. Read happens here, OUTSIDE any held
    # LOCK. Carries only {bucket-ts, status-rank} — no names/topology/secrets.
    hist = _status_history(now)
    for tile in tiles:
        h = hist.get(tile["key"])
        if h:
            tile["history"] = h

    # 90-day uptime ribbon — per-calendar-day worst overall rank + up-fraction.
    # Aggregated ints/floats + a date string ONLY (no names/topology/secrets).
    daily = _status_daily(now)

    # Opt-in per-component monitors (public+enabled uptime checks only). Each row
    # carries a label + credential-stripped host + derived numbers — NEVER the raw
    # target/err/internals. Empty when nothing is opted in (default state).
    try:
        monitors = _public_monitors(now)
    except Exception as e:
        print("public monitors error:", e, flush=True)
        monitors = []

    return {
        "status": _STATUS_BANNER.get(worst, "operational"),
        "updated": HEALTH["at"] or now,
        "now": now,
        "demo": DEMO_MODE,
        "tiles": tiles,
        "gpu": gpu_pub,
        "anomaly_active": anomaly_active,
        "counts": {"services": n_services, "containers": n_containers,
                   "monitored": n_services + n_containers, "problems": n_problems},
        "history": hist.get("overall"),
        "history_bucket": _STATHIST_BUCKET,
        "daily": daily["days"],
        "uptime_90d": daily["uptime"],
        "uptime_days": daily["total_days"],
        "uptime_span": daily["span"],
        "monitors": monitors,
    }

# ── Per-service PUBLIC status (opt-in, privacy-first) ─────────────────────────
# The highest-risk leak surface: a per-check public detail page. Everything below
# exposes ONLY a label + a credential-stripped host (and non-default port), plus
# derived/aggregate numbers. NEVER the raw target, NEVER a full URL with userinfo/
# path/query, NEVER the raw err string (which can embed internal hostnames/paths
# even post-redaction). Incident reasons are reduced to "Down" + optional HTTP code.
_PUBLIC_DAILY_DAYS    = _DAILY_RIBBON_DAYS   # 90-day per-component daily ribbon
_PUBLIC_LATENCY_PTS   = 120                  # downsample the latency sparkline to ≤N
_PUBLIC_INCIDENTS_MAX = 50                   # cap incidents returned per component

def _public_host(check):
    """Derive a PUBLIC-safe host string from a check's target: host (and :port when
    non-default for the check type), with ANY credentials stripped. Never returns a
    scheme, userinfo, path, or query — only host[:port]. Falls back to '' if the
    target can't be parsed (never leaks the raw target)."""
    ctype = (check.get("type") or "http").strip().lower()
    target = check.get("target") or ""
    try:
        if ctype == "http":
            u = urllib.parse.urlsplit(target)
            host = u.hostname or ""
            if not host:
                return ""
            port = u.port
            default = 443 if u.scheme == "https" else 80
            if port and port != default:
                return f"{host}:{port}"
            return host
        if ctype == "cert":
            host, port = _parse_cert_target(target)
            if not host:
                return ""
            if port and port != _UPTIME_CERT_DEFAULT_PORT:
                return f"{host}:{port}"
            return host
        # tcp — host:port is the whole point; show the port (no default to hide).
        host, port = _parse_host_port(target)
        if not host:
            return ""
        return f"{host}:{port}" if port else host
    except Exception:
        return ""

def _public_daily_cells(rows, now, days=_PUBLIC_DAILY_DAYS):
    """Roll (ts, up) result rows into a per-CALENDAR-DAY ribbon for ONE check:
    [{d:'YYYY-MM-DD', up: up-fraction 0..1 or None, s: 0(ok)/2(some down)/3(all
    down)/-1(no data)} ...] oldest→newest. Mirrors _status_daily's honest 'no
    data' handling (never fakes green)."""
    agg = {}   # day -> [up_count, total]
    for ts, up in rows:
        if ts is None:
            continue
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        a = agg.get(day)
        if a is None:
            agg[day] = [1 if up else 0, 1]
        else:
            a[1] += 1
            if up:
                a[0] += 1
    today_mid = time.mktime(time.strptime(
        time.strftime("%Y-%m-%d", time.localtime(now)), "%Y-%m-%d"))
    cells = []
    for i in range(days - 1, -1, -1):
        day = time.strftime("%Y-%m-%d", time.localtime(today_mid - i * 86400))
        a = agg.get(day)
        if a is None:
            cells.append({"d": day, "up": None, "s": -1})
        else:
            upc, tot = a
            frac = round(upc / tot, 4) if tot else None
            if upc == tot:
                s = 0
            elif upc == 0:
                s = 3
            else:
                s = 2
            cells.append({"d": day, "up": frac, "s": s})
    return cells

def _uptime_pct_window(rows, now, window):
    """up% over the last `window` seconds from ascending (ts, up) rows, or None."""
    since = now - window
    total = up = 0
    for ts, u in rows:
        if ts is None or ts < since:
            continue
        total += 1
        if u:
            up += 1
    return round(100.0 * up / total, 2) if total else None

def _public_incidents(rows, now, cap=_PUBLIC_INCIDENTS_MAX):
    """Reconstruct down-periods from ascending (ts, up, code) rows. Returns the
    most-recent `cap` incidents, newest→oldest, each:
      {start, end|None (None=ongoing), duration_sec|None, code|None, reason}
    PRIVACY: reason is the generic 'Down' (never the raw err); code is the HTTP
    status only when present. No targets, no err strings, no internals."""
    incidents = []
    cur = None   # {start, last_ts, code}
    for r in rows:
        ts, up = r[0], r[1]
        code = r[2] if len(r) > 2 else None
        if ts is None:
            continue
        if not up:
            if cur is None:
                cur = {"start": ts, "last_ts": ts, "code": code}
            else:
                cur["last_ts"] = ts
                if code is not None:
                    cur["code"] = code   # keep the latest seen status code
        else:
            if cur is not None:
                incidents.append({
                    "start": cur["start"], "end": ts,
                    "duration_sec": max(0, ts - cur["start"]),
                    "code": cur["code"], "reason": "Down"})
                cur = None
    if cur is not None:   # still down at the end of the window → ongoing
        incidents.append({
            "start": cur["start"], "end": None,
            "duration_sec": max(0, now - cur["start"]),
            "code": cur["code"], "reason": "Down"})
    incidents.reverse()   # newest first
    return incidents[:cap]

def _sla_window(tu, incidents, now, window):
    """Additive SLA summary for ONE window: {uptime %, downtime_sec, incidents}.
    uptime mirrors _uptime_pct_window (same data source). Downtime is summed
    HONESTLY from the reconstructed down-periods, clipped to the window — no new
    sampling. downtime/incidents follow the 'no data' semantics of the uptime %:
    when there are no samples in the window we report None/0 rather than fake a
    fully-down window from a stale ongoing incident."""
    pct = _uptime_pct_window(tu, now, window)
    since = now - window
    down = 0
    count = 0
    for inc in incidents:
        s = inc["start"]
        e = inc["end"] if inc["end"] is not None else now
        ov = min(e, now) - max(s, since)
        if ov > 0:
            down += ov
            count += 1
    return {"uptime": pct,
            "downtime_sec": (int(down) if pct is not None else None),
            "incidents": count if pct is not None else 0}

def _public_check_detail(cid, now=None):
    """PUBLIC, privacy-safe detail for ONE uptime check, or None when the check is
    not public+enabled (caller 404s). Single bounded 90-day query over the per-check
    result ring. Exposes label + credential-stripped host + derived/aggregate
    numbers ONLY — never the raw target/err/internals (see module note above)."""
    now = int(time.time()) if now is None else int(now)
    with LOCK:
        crow = DB.execute(
            "SELECT id,label,type,target,interval_sec,timeout_sec,expected_status,"
            "enabled,created_at,cert_warn_days,public FROM uptime_checks WHERE id=?",
            (cid,)).fetchone()
    if not crow:
        return None
    check = _uptime_row_to_dict(crow)
    if not (check["public"] and check["enabled"]):
        return None
    since = now - _PUBLIC_DAILY_DAYS * 86400 - 86400
    with LOCK:
        rows = DB.execute(
            "SELECT ts,up,latency_ms,code FROM uptime_results WHERE check_id=? AND ts>=? "
            "ORDER BY ts", (cid, since)).fetchall()
        last = DB.execute(
            "SELECT ts,up,days_to_expiry FROM uptime_results WHERE check_id=? "
            "ORDER BY ts DESC LIMIT 1", (cid,)).fetchone()
    tu = [(r[0], r[1]) for r in rows]                    # (ts, up)
    tuc = [(r[0], r[1], r[3]) for r in rows]             # (ts, up, code)
    state = "unknown"
    cert_days = None
    if last:
        state = "up" if last[1] else "down"
        cert_days = last[2]
    # up_since: ts of the first sample in the current contiguous run of same-state.
    up_since = None
    if last:
        cur_up = bool(last[1])
        for ts, up in reversed(tu):
            if bool(up) == cur_up:
                up_since = ts
            else:
                break
    # Latency series, downsampled to ≤_PUBLIC_LATENCY_PTS (uptime samples only —
    # a 'down' has no meaningful latency). Stride-decimate, keep last point.
    lat = [(r[0], r[2]) for r in rows if r[1] and r[2] is not None]
    if len(lat) > _PUBLIC_LATENCY_PTS:
        # Stride-decimate to exactly _PUBLIC_LATENCY_PTS points, anchoring the last
        # bucket on the final sample so the most recent latency is always shown.
        n = len(lat)
        idx = sorted({min(n - 1, int(round(i * (n - 1) / (_PUBLIC_LATENCY_PTS - 1))))
                      for i in range(_PUBLIC_LATENCY_PTS)})
        lat = [lat[i] for i in idx]
    response_series = [{"t": int(t), "ms": round(ms, 1)} for t, ms in lat]
    out = {
        "id": check["id"],
        "label": check["label"],
        "host": _public_host(check),
        "type": check["type"],
        "state": state,
        "up_since": up_since,
        "now": now,
        "uptime": {
            "24h": _uptime_pct_window(tu, now, 86400),
            "7d":  _uptime_pct_window(tu, now, 7 * 86400),
            "30d": _uptime_pct_window(tu, now, 30 * 86400),
            "90d": _uptime_pct_window(tu, now, 90 * 86400),
        },
        "daily": _public_daily_cells(tu, now),
        "response_series": response_series,
        "incidents": _public_incidents(tuc, now),
        "span": _PUBLIC_DAILY_DAYS,
    }
    # SLA / downtime summary — additive. Uptime mirrors out["uptime"]; downtime is
    # summed from the SAME reconstructed down-periods (uncapped so a flappy service
    # isn't undercounted), clipped to each window. Never introduces new sampling.
    full_inc = _public_incidents(tuc, now, cap=len(tuc) + 1)
    out["sla"] = {
        "24h": _sla_window(tu, full_inc, now, 86400),
        "7d":  _sla_window(tu, full_inc, now, 7 * 86400),
        "30d": _sla_window(tu, full_inc, now, 30 * 86400),
        "90d": _sla_window(tu, full_inc, now, 90 * 86400),
    }
    if check["type"] == "cert" and cert_days is not None:
        out["cert_days"] = int(cert_days)
    return out

def _public_monitors(now):
    """The per-component rows for the public INDEX: public+enabled checks only,
    each {id, label, host, type, state, uptime (current 90d%), daily (90 cells),
    incidents (recent, generic)}. Privacy-identical to _public_check_detail."""
    mons = []
    for c in list_uptime_checks():
        if not (c.get("public") and c.get("enabled")):
            continue
        since = now - _PUBLIC_DAILY_DAYS * 86400 - 86400
        with LOCK:
            rows = DB.execute(
                "SELECT ts,up,code FROM uptime_results WHERE check_id=? AND ts>=? ORDER BY ts",
                (c["id"], since)).fetchall()
        tu = [(r[0], r[1]) for r in rows]
        tuc = [(r[0], r[1], r[2]) for r in rows]
        state = "unknown"
        if tu:
            state = "up" if tu[-1][1] else "down"
        mons.append({
            "id": c["id"],
            "label": c["label"],
            "host": _public_host(c),
            "type": c["type"],
            "state": state,
            "uptime": _uptime_pct_window(tu, now, 90 * 86400),
            "daily": _public_daily_cells(tu, now),
            "incidents": _public_incidents(tuc, now, cap=10),
        })
    return mons

# ── Shareable per-service status: RSS 2.0 incident feed (opt-in, privacy-first) ──
# Same gate + privacy contract as /api/status/<id>: 404 unless the check is
# public AND enabled AND STATUS_PAGE is on. Items carry ONLY a credential-stripped
# host + generic Down/Recovered + optional HTTP code — never the raw target, path,
# query, userinfo, or the raw err string.
def _rss_pubdate(ts):
    """RFC-822 date (GMT) for an epoch second — valid RSS <pubDate>."""
    try:
        return email.utils.formatdate(float(ts), usegmt=True)
    except Exception:
        return email.utils.formatdate(usegmt=True)

def _human_dur(sec):
    """Compact, human downtime string (server-side mirror of the page's durLabel)."""
    sec = max(0, int(sec or 0))
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm" % (sec // 60)
    if sec < 86400:
        return "%dh %dm" % (sec // 3600, (sec % 3600) // 60)
    return "%dd %dh" % (sec // 86400, (sec % 86400) // 3600)

def _feed_items_for_check(check, incidents, host):
    """Generic RSS item dicts from reconstructed incidents for ONE check. Each
    down-period yields a 'Down' item (at its start) and, once recovered, a
    'Recovered' item (at recovery). PRIVACY: label + credential-stripped host +
    generic Down/Recovered + optional HTTP code ONLY — never target/path/err."""
    cid = check["id"]
    label = check.get("label") or ""
    subject = host or label or "Service"
    items = []
    for inc in incidents:
        code = inc.get("code")
        codestr = " (HTTP %d)" % code if isinstance(code, int) else ""
        dur = inc.get("duration_sec")
        items.append({
            "cid": cid, "label": label, "ts": int(inc["start"]),
            "title": "%s — Down%s" % (subject, codestr),
            "guid": "%s:down:%d" % (cid, int(inc["start"])),
            "desc": "%s went down." % subject
                    + (" Returned HTTP %d." % code if isinstance(code, int) else ""),
        })
        if inc.get("end") is not None:
            items.append({
                "cid": cid, "label": label, "ts": int(inc["end"]),
                "title": "%s — Recovered" % subject,
                "guid": "%s:up:%d" % (cid, int(inc["end"])),
                "desc": "%s recovered%s." % (
                    subject, (" after %s" % _human_dur(dur)) if dur else ""),
            })
    return items

def _incidents_for(cid, now):
    """(ts,up,code) rows → generic capped incidents for a check id (feed helper)."""
    since = now - _PUBLIC_DAILY_DAYS * 86400 - 86400
    with LOCK:
        rows = DB.execute(
            "SELECT ts,up,code FROM uptime_results WHERE check_id=? AND ts>=? ORDER BY ts",
            (cid, since)).fetchall()
    tuc = [(r[0], r[1], r[2]) for r in rows]
    return _public_incidents(tuc, now, cap=_PUBLIC_INCIDENTS_MAX)

def _build_status_feed(cid, now=None, base_url=""):
    """RSS 2.0 XML string for one public+enabled check (or, when cid is None, a
    cross-service feed of all public+enabled checks). Returns None when the check
    isn't public+enabled (caller 404s). All interpolated text is XML-escaped."""
    now = int(time.time()) if now is None else int(now)
    esc = lambda s: _html.escape(str(s), quote=True)
    base = (base_url or "").rstrip("/")
    if cid is None:
        title = "Lab status — incidents"
        page_link = base + "/status"
        self_link = base + "/status/feed.xml"
        desc = "Recent incidents across all public services."
        items = []
        for c in list_uptime_checks():
            if not (c.get("public") and c.get("enabled")):
                continue
            items += _feed_items_for_check(c, _incidents_for(c["id"], now), _public_host(c))
    else:
        with LOCK:
            crow = DB.execute(
                "SELECT id,label,type,target,interval_sec,timeout_sec,expected_status,"
                "enabled,created_at,cert_warn_days,public FROM uptime_checks WHERE id=?",
                (cid,)).fetchone()
        if not crow:
            return None
        check = _uptime_row_to_dict(crow)
        if not (check["public"] and check["enabled"]):
            return None
        host = _public_host(check)
        page_link = base + "/status/" + urllib.parse.quote(cid, safe="")
        self_link = page_link + "/feed.xml"
        title = "%s — status" % (check["label"] or host or "Service")
        desc = "Incident history for %s." % (host or check["label"] or "this service")
        items = _feed_items_for_check(check, _incidents_for(cid, now), host)
    items.sort(key=lambda i: i["ts"], reverse=True)
    items = items[:_PUBLIC_INCIDENTS_MAX]
    p = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
         '<channel>',
         '<title>%s</title>' % esc(title),
         '<link>%s</link>' % esc(page_link),
         '<atom:link href="%s" rel="self" type="application/rss+xml"/>' % esc(self_link),
         '<description>%s</description>' % esc(desc),
         '<language>en</language>',
         '<generator>HomeLab Monitor</generator>',
         '<ttl>5</ttl>',
         '<lastBuildDate>%s</lastBuildDate>' % esc(_rss_pubdate(now))]
    for it in items:
        item_link = base + "/status/" + urllib.parse.quote(it["cid"], safe="")
        p += ['<item>',
              '<title>%s</title>' % esc(it["title"]),
              '<link>%s</link>' % esc(item_link),
              '<guid isPermaLink="false">%s</guid>' % esc(it["guid"]),
              '<pubDate>%s</pubDate>' % esc(_rss_pubdate(it["ts"])),
              '<description>%s</description>' % esc(it["desc"]),
              '</item>']
    p.append('</channel></rss>')
    return "\n".join(p)

def _check_is_public(cid):
    """True only when the check exists AND is public AND enabled (feed/discovery
    gate — mirrors _public_check_detail's contract, cheaply)."""
    try:
        with LOCK:
            r = DB.execute("SELECT enabled,public FROM uptime_checks WHERE id=?",
                           (cid,)).fetchone()
        return bool(r and r[0] and r[1])
    except Exception:
        return False

@app.route("/status/feed.xml")
def status_feed_all():
    """Cross-service RSS 2.0 incident feed (public+enabled checks only). Valid
    (possibly empty) feed while STATUS_PAGE is on; 404 when it's off."""
    if not STATUS_PAGE:
        return ("Status page disabled", 404)
    try:
        xml = _build_status_feed(None, base_url=request.host_url)
    except Exception as e:
        print("status feed error:", e, flush=True)
        xml = None
    if xml is None:
        return ("Not found", 404)
    return Response(xml, content_type="application/rss+xml; charset=utf-8")

@app.route("/status/<cid>/feed.xml")
def status_feed_one(cid):
    """Per-service RSS 2.0 incident feed. 404 unless the check is public AND enabled
    (and STATUS_PAGE on). Privacy-identical to /api/status/<id>."""
    if not STATUS_PAGE:
        return ("Status page disabled", 404)
    try:
        xml = _build_status_feed(cid, base_url=request.host_url)
    except Exception as e:
        print("status feed error:", e, flush=True)
        return ("Not found", 404)
    if xml is None:
        return ("Not found", 404)
    return Response(xml, content_type="application/rss+xml; charset=utf-8")

# ── Downloadable per-service SLA / incident report (CSV + JSON export) ──────────
# Same gate + privacy contract as /api/status/<id> and the RSS feed: 404 unless the
# check is public AND enabled AND STATUS_PAGE is on. The artifact carries ONLY the
# display label + credential-stripped host + the SLA windows block + generic
# Down/Recovered incidents (optional HTTP code) — never the raw target, path, query,
# userinfo, or err string. No new sampling: it reuses _public_check_detail.
def _safe_report_id(cid):
    """Header/filename-safe token from a check id: alnum + dash ONLY, capped. Never
    interpolates raw user/target text into the Content-Disposition header (so a weird
    id/name can't inject headers). Falls back to 'service' when nothing survives."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(cid or "")).strip("-")
    return (s[:64].strip("-") or "service")

def _build_status_report(cid, now=None):
    """Self-contained, privacy-safe report dict for one public+enabled check, or None
    (caller 404s). Reuses _public_check_detail — SAME data source and privacy
    contract as /api/status/<id>: label + host-only + sla windows + generic
    incidents. The sla numbers are byte-for-byte the API's sla block."""
    now = int(time.time()) if now is None else int(now)
    detail = _public_check_detail(cid, now)
    if detail is None:
        return None
    incidents = []
    for inc in (detail.get("incidents") or []):
        end = inc.get("end")
        dur = inc.get("duration_sec")
        code = inc.get("code")
        incidents.append({
            "started_at": int(inc["start"]),
            "ended_at": (int(end) if end is not None else None),
            "duration_sec": (int(dur) if dur is not None else None),
            "state": ("Recovered" if end is not None else "Down"),
            "http_code": (int(code) if isinstance(code, int) else None),
        })
    return {
        "id": detail["id"],
        "label": detail["label"],
        "host": detail["host"],
        "type": detail["type"],
        "generated_at": now,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "sla": detail["sla"],
        "incidents": incidents,
    }

def _report_to_csv(rep):
    """Render a report dict to RFC-4180 CSV via the stdlib csv module (\\r\\n rows,
    proper quoting). Two tables — SLA windows then incidents — plus a small metadata
    header. Same privacy contract as the JSON report (no raw target/err)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["service", rep.get("label") or ""])
    w.writerow(["host", rep.get("host") or ""])
    w.writerow(["generated_at", rep.get("generated_at")])
    w.writerow(["generated_at_utc", rep.get("generated_at_utc") or ""])
    w.writerow([])
    w.writerow(["window", "uptime_pct", "downtime_sec", "downtime_human", "incidents"])
    sla = rep.get("sla") or {}
    for key in ("24h", "7d", "30d", "90d"):
        s = sla.get(key) or {}
        up = s.get("uptime")
        dn = s.get("downtime_sec")
        w.writerow([key,
                    ("" if up is None else up),
                    ("" if dn is None else dn),
                    ("" if dn is None else _human_dur(dn)),
                    (s.get("incidents") or 0)])
    w.writerow([])
    w.writerow(["started_at", "ended_at", "duration_sec", "state", "http_code"])
    for inc in (rep.get("incidents") or []):
        w.writerow([inc.get("started_at"),
                    ("" if inc.get("ended_at") is None else inc.get("ended_at")),
                    ("" if inc.get("duration_sec") is None else inc.get("duration_sec")),
                    (inc.get("state") or ""),
                    ("" if inc.get("http_code") is None else inc.get("http_code"))])
    return buf.getvalue()

@app.route("/status/<cid>/report.json")
def status_report_json(cid):
    """Downloadable self-contained JSON SLA/incident report. 404 unless the check is
    public AND enabled (and STATUS_PAGE on). Privacy-identical to /api/status/<id>."""
    if not STATUS_PAGE:
        return ("Status page disabled", 404)
    try:
        rep = _build_status_report(cid)
    except Exception as e:
        print("status report error:", e, flush=True)
        return ("Not found", 404)
    if rep is None:
        return ("Not found", 404)
    fn = _safe_report_id(cid) + "-status-report.json"
    return Response(json.dumps(rep, indent=2, ensure_ascii=False),
                    content_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % fn})

@app.route("/status/<cid>/report.csv")
def status_report_csv(cid):
    """Downloadable CSV SLA/incident report (stdlib csv, RFC-4180). Same data and
    gate as report.json; 404 identically when private/disabled/missing/STATUS_PAGE-off."""
    if not STATUS_PAGE:
        return ("Status page disabled", 404)
    try:
        rep = _build_status_report(cid)
    except Exception as e:
        print("status report error:", e, flush=True)
        return ("Not found", 404)
    if rep is None:
        return ("Not found", 404)
    fn = _safe_report_id(cid) + "-status-report.csv"
    return Response(_report_to_csv(rep),
                    content_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % fn})

@app.route("/api/status")
def api_status():
    """Public read-only JSON behind the /status page. Aggregated, non-sensitive."""
    if not STATUS_PAGE:
        return ("Status page disabled", 404)
    try:
        return jsonify(build_public_status())
    except Exception as e:
        print("status error:", e, flush=True)
        return jsonify({"status": "operational", "updated": int(time.time()),
                        "now": int(time.time()), "demo": DEMO_MODE, "tiles": [],
                        "gpu": {"available": False}, "anomaly_active": False,
                        "counts": {"services": 0, "containers": 0,
                                   "monitored": 0, "problems": 0},
                        "daily": [], "uptime_90d": None,
                        "uptime_days": 0, "uptime_span": _DAILY_RIBBON_DAYS,
                        "monitors": []})

@app.route("/api/status/<cid>")
def api_status_detail(cid):
    """Public per-service detail. 404 unless the check is public AND enabled (and
    the status page is on). Privacy-safe: host-only, generic incidents, no targets
    /err/internals — see _public_check_detail."""
    if not STATUS_PAGE:
        return ("Status page disabled", 404)
    try:
        detail = _public_check_detail(cid)
    except Exception as e:
        print("status detail error:", e, flush=True)
        return ("Not found", 404)
    if detail is None:
        return ("Not found", 404)
    return jsonify(detail)

_STATUS_SHELL = None
def _status_shell():
    """Cached contents of static/status.html (read once) so the per-service page can
    have an RSS autodiscovery <link> injected without a disk read per request."""
    global _STATUS_SHELL
    if _STATUS_SHELL is None:
        with open(os.path.join(app.static_folder, "status.html"), encoding="utf-8") as f:
            _STATUS_SHELL = f.read()
    return _STATUS_SHELL

@app.route("/status")
@app.route("/status/<cid>")
def status_page(cid=None):
    """Unauthenticated, self-contained public status page (HTML). The optional
    /status/<cid> path is the per-service deep link — the same single-page app
    handles routing client-side; the server just serves the shell either way (the
    detail data itself is gated by /api/status/<cid>)."""
    if not STATUS_PAGE:
        return ("Status page disabled", 404)
    # RSS autodiscovery: only inject the per-service <link> when the check is
    # actually public+enabled (so private/unknown ids leak nothing and readers
    # don't advertise a feed that 404s). Server-rendered so non-JS RSS readers see
    # it. Falls back to the plain static shell otherwise.
    if cid and _check_is_public(cid):
        try:
            shell = _status_shell()
            href = "/status/" + urllib.parse.quote(cid, safe="") + "/feed.xml"
            tag = ('<link rel="alternate" type="application/rss+xml" '
                   'title="Incident feed" href="%s">\n'
                   % _html.escape(href, quote=True))
            return Response(shell.replace("</head>", tag + "</head>", 1),
                            content_type="text/html; charset=utf-8")
        except Exception as e:
            print("status shell inject error:", e, flush=True)
    return app.send_static_file("status.html")

@app.route("/api/health")
def api_health():
    """Current state of the status monitors (Docker + systemd) plus a light GPU/host
    snapshot. Cheap and DB-free, so the dashboard can poll it often."""
    gpu_avail = LATEST.get("gpu_avail")
    now = {"gpu": {"util": LATEST["util"], "mem_used": LATEST["mem_used"],
                   "mem_total": (LATEST["mem_total"] or 24576) if gpu_avail else 0,
                   "power": LATEST["power"], "temp": LATEST["temp"],
                   "available": bool(gpu_avail),
                   "gpus": LATEST.get("gpus") or [],    # per-card detail (issue #95)
                   "extra": LATEST.get("gpu_extra") or {}},  # mem-bw/clocks/throttle (telemetry)
           "host": enrich_os_upgrade(LATEST["host"])}
    docker  = HEALTH["docker"]  or {"available": False, "reason": "warming up…",
                                    "containers": [], "summary": {"total": 0, "running": 0, "problems": 0}}
    systemd = HEALTH["systemd"] or {"available": False, "reason": "warming up…",
                                    "services": [], "summary": {}}
    disk_io = dict(HEALTH.get("disk_io") or {"available": False, "warming_up": True,
                                        "summary": {"total_read_mb_s": 0.0, "total_write_mb_s": 0.0},
                                        "items": []})
    # Per-process I/O attribution (Top writer/reader) — attach ONLY to this authed
    # payload. It carries process comm (never cmdline/argv) and NEVER appears on the
    # public /status surface (build_public_status doesn't read processes/disk_io).
    _pio = (HEALTH.get("processes") or {}).get("io")
    if _pio and _pio.get("available"):
        disk_io["attribution"] = _pio
    update  = dict(HEALTH["update"] or {"available": False, "current": VERSION})
    # Let the frontend decide whether to show the one-click "Update now" button.
    # Set here (not baked into the cached collect_update payload) so toggling the
    # env flag takes effect on restart without waiting for the update cache.
    update["self_update_enabled"] = ALLOW_SELF_UPDATE
    return jsonify({"version": VERSION, "updated": HEALTH["at"], "now": now,
                    "demo": DEMO_MODE, "status_page": STATUS_PAGE,
                    "controls": _controls_state(),
                    "docker": docker, "systemd": systemd, "update": update,
                    "disk_io": disk_io,
                    "processes": HEALTH["processes"],
                    "os_updates": os_updates_summary(),
                    "diagnostics": local_diagnostics(),
                    "mcp": {"enabled": _mcp_enabled(), "port": _mcp_port()},
                    "overview": build_overview(now, docker, systemd)})

# ── Pure-stdlib Prometheus/OpenMetrics exposition (no extra dep) ──────────────
# The base GPU/host/container series are exported via prometheus_client gauges
# (when installed). These *extra* series — total power, per-disk bytes + fill %,
# month-cost projection, and per-series anomaly flags — are built as plain text
# with their own distinct metric names, so they never clash with the gauges'
# HELP/TYPE lines and work even when prometheus_client is absent.
_PROM_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")

def _prom_label_val(v):
    """Escape a label value per the exposition format (\\, \", newline)."""
    return (str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n"))

def _prom_num(v):
    """Format a value as a finite float, or 'NaN' — never raises on junk."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "NaN"
    if f != f or f in (float("inf"), float("-inf")):
        return "NaN"
    return repr(f)

def _prom_metric(out, name, mtype, help_text, samples):
    """Append one metric family (HELP + TYPE once, then its samples) to `out`.
    `samples` is an iterable of (labels_dict_or_None, value). Skips empty
    families so we never emit a TYPE with zero samples and a dangling HELP."""
    samples = [s for s in samples if s[1] is not None]
    if not samples:
        return
    out.append(f"# HELP {name} {help_text}")
    out.append(f"# TYPE {name} {mtype}")
    for labels, val in samples:
        if labels:
            lbl = ",".join(f'{k}="{_prom_label_val(v)}"' for k, v in labels.items())
            out.append(f"{name}{{{lbl}}} {_prom_num(val)}")
        else:
            out.append(f"{name} {_prom_num(val)}")

def _extra_metrics_text():
    """Build the extra pure-stdlib metric families as exposition text. Reads the
    live snapshots + the same forecast/cost helpers the dashboard uses; never
    raises (a failing block is simply omitted)."""
    out = []
    now = int(time.time())

    # build_info (version pinned as a label, value 1) — handy for dashboards.
    _prom_metric(out, "homelab_build_info", "gauge",
                 "Build info (value always 1; version in label)",
                 [({"version": VERSION}, 1)])

    # Total machine power draw (GPU + CPU + DRAM watts), from the live snapshot.
    try:
        total_w = (LATEST.get("power") or 0) + (LATEST.get("cpu_power") or 0) + (LATEST.get("dram_power") or 0)
        _prom_metric(out, "homelab_power_total_w", "gauge",
                     "Total machine power draw (GPU+CPU+DRAM, W)", [(None, total_w)])
    except Exception:
        pass

    # Per-GPU vendor info series (value always 1; identity in the labels). Additive
    # and dedicated — the numeric GPU gauges (homelab_gpu_util_pct etc.) keep their
    # existing label set untouched, so dashboards/recording rules that join on
    # gpu="gpu0" are unaffected; this series lets Grafana label a card by vendor
    # via an on(gpu) group_left(vendor, name) join. Label values are exposition-
    # escaped by _prom_label_val. Omitted entirely when no per-card list exists.
    try:
        host_name = ((LATEST.get("host") or {}).get("hostname") or socket.gethostname())
        info_s = []
        for i, g in enumerate(LATEST.get("gpus") or []):
            idx = g.get("idx")
            # Fall back to the enumerate index (not a constant) so cards missing
            # idx can't collide into an identical labelset — a duplicate series
            # would make Prometheus reject the whole scrape.
            info_s.append(({"host": host_name,
                            "gpu": f"gpu{idx if idx is not None else i}",
                            "name": g.get("name") or "",
                            "vendor": (g.get("vendor") or "unknown")}, 1))
        _prom_metric(out, "homelab_gpu_info", "gauge",
                     "GPU identity (value always 1; host/gpu/name/vendor in labels)",
                     info_s)
    except Exception:
        pass

    # Per-disk bytes + fill % from the host snapshot (GB fields -> bytes).
    try:
        used_s, total_s, pct_s = [], [], []
        for d in ((LATEST.get("host") or {}).get("disks") or []):
            mp = d.get("mount")
            if not mp:
                continue
            lbl = {"mountpoint": mp}
            if d.get("used") is not None:
                used_s.append((lbl, float(d["used"]) * (1024 ** 3)))
            if d.get("total") is not None:
                total_s.append((lbl, float(d["total"]) * (1024 ** 3)))
            if d.get("pct") is not None:
                pct_s.append((lbl, d["pct"]))
        _prom_metric(out, "homelab_disk_used_bytes", "gauge",
                     "Filesystem used space (bytes)", used_s)
        _prom_metric(out, "homelab_disk_total_bytes", "gauge",
                     "Filesystem total space (bytes)", total_s)
        _prom_metric(out, "homelab_disk_fill_pct", "gauge",
                     "Filesystem fill (%)", pct_s)
    except Exception:
        pass

    # Month-to-date + projected energy cost (only when a price is set).
    try:
        ctx = _cost_ctx()
        with LOCK:
            cm = _cost_projection(DB.cursor(), ctx, now)
        if cm.get("enabled"):
            cur = {"currency": cm.get("currency", "")}
            _prom_metric(out, "homelab_cost_month_to_date", "gauge",
                         "Energy cost so far this month (currency unit)",
                         [(cur, cm.get("month_to_date"))])
            _prom_metric(out, "homelab_cost_month_projected", "gauge",
                         "Projected full-month energy cost (currency unit)",
                         [(cur, cm.get("projected_month"))])
    except Exception:
        pass

    # Anomaly flags: 1 when a monitored series is currently flagged, else 0 — one
    # sample per known series so the gauge never silently disappears.
    try:
        with LOCK:
            anom = _zscore_anomalies(DB.cursor(), now)
        fired = {it["key"]: it for it in (anom.get("items") or [])}
        flag_s = []
        for key, _col, _unit, _md in _ANOMALY_SERIES:
            it = fired.get(key)
            flag_s.append(({"series": key,
                            "direction": (it["direction"] if it else "none")}, 1 if it else 0))
        _prom_metric(out, "homelab_anomaly_active", "gauge",
                     "Anomaly flag per series (1=flagged now, 0=normal)", flag_s)
    except Exception:
        pass

    # LLM engine: latest REAL throughput measurement (AI Lab Cockpit). Only
    # emitted once a copilot generation has actually run — we never publish a
    # fake 0 that would look like a stalled engine. Labelled by model so multiple
    # models read distinctly. Resident-model count is always-safe (0 = none).
    try:
        with _LLM_LOCK:
            last = dict(_LLM_LAST) if _LLM_LAST else None
        if last:
            lbl = {"model": last.get("model") or COPILOT_MODEL}
            _prom_metric(out, "homelab_llm_tokens_per_second", "gauge",
                         "LLM generation throughput, latest measurement (tokens/s)",
                         [(lbl, last.get("tps"))])
            _prom_metric(out, "homelab_llm_ttft_ms", "gauge",
                         "LLM time-to-first-token, latest measurement (ms)",
                         [(lbl, last.get("ttft_ms"))])
        resident, reachable = _llm_resident_models()
        if reachable:
            _prom_metric(out, "homelab_llm_resident_models", "gauge",
                         "Models currently loaded in ollama (count)",
                         [(None, len(resident))])
    except Exception:
        pass

    # External uptime checks → Grafana. Only emitted when ≥1 check exists (zero
    # checks ⇒ none of these families appear at all). We read uptime_overview()
    # OUTSIDE any held lock (it takes LOCK briefly itself, like every exporter
    # here). The check LABEL is used as the series label — escaped via
    # _prom_label_val so a quote/backslash/newline can't break exposition — and
    # we NEVER put the raw target (which may carry user:pass creds) into a label.
    try:
        checks = (uptime_overview().get("checks") or [])
        if checks:
            up_s, lat_s, ratio_s, cert_s, cert_na_s = [], [], [], [], []
            down_n = 0
            for c in checks:
                lbl = {"check": c.get("label") or c.get("id") or "?"}
                state = c.get("state")
                # Emit up=1/0 ONLY for known states; a flapping 'unknown'
                # (no result yet) is skipped so it never reads as down.
                if state == "up":
                    up_s.append((lbl, 1))
                elif state == "down":
                    up_s.append((lbl, 0))
                    down_n += 1
                lat = c.get("last_latency_ms")
                if lat is not None:
                    lat_s.append((lbl, lat))
                up_pct = c.get("uptime")
                if up_pct is not None:
                    ratio_s.append((lbl, up_pct / 100.0))
                # Cert checks contribute their days-to-expiry (can be negative when
                # expired). Only emitted for cert checks that have actually probed.
                if c.get("type") == "cert" and c.get("days_to_expiry") is not None:
                    cert_s.append((lbl, c.get("days_to_expiry")))
                # Absolute expiry as a POSIX timestamp (from the persisted cert_extra
                # not_after) so Grafana/Alertmanager can alert on the exact date and
                # chart the renewal cliff — not just the relative day count above.
                if c.get("type") == "cert" and c.get("not_after_ts") is not None:
                    cert_na_s.append((lbl, c.get("not_after_ts")))
            _prom_metric(out, "homelab_uptime_up", "gauge",
                         "Uptime check current state (1=up, 0=down; unknown omitted)", up_s)
            _prom_metric(out, "homelab_uptime_latency_ms", "gauge",
                         "Uptime check last measured latency (ms)", lat_s)
            _prom_metric(out, "homelab_uptime_uptime_ratio", "gauge",
                         "Uptime check uptime fraction over the window (0..1)", ratio_s)
            _prom_metric(out, "homelab_uptime_checks_total", "gauge",
                         "Configured uptime checks (count)", [(None, len(checks))])
            _prom_metric(out, "homelab_uptime_checks_down", "gauge",
                         "Uptime checks currently down (count)", [(None, down_n)])
            # TLS cert expiry — days remaining per cert check. Family appears ONLY
            # when ≥1 cert check has probed (zero cert checks ⇒ absent entirely).
            if cert_s:
                _prom_metric(out, "homelab_uptime_cert_days_remaining", "gauge",
                             "TLS certificate days remaining per cert check (negative = expired)", cert_s)
            if cert_na_s:
                _prom_metric(out, "homelab_uptime_cert_not_after_seconds", "gauge",
                             "TLS certificate expiry as a POSIX timestamp (seconds) per cert check", cert_na_s)
    except Exception:
        pass

    return "\n".join(out) + ("\n" if out else "")

# ── Home Assistant / MQTT auto-discovery publisher (E4) ──────────────────────
# A pure-stdlib (socket/ssl/struct) MQTT 3.1.1 PUBLISH-ONLY client. When enabled
# + configured, a dedicated daemon thread publishes retained HA discovery configs
# and then periodic JSON state, so the lab shows up as native Home Assistant
# sensors under one "HomeLab Monitor" device.
#
# ISOLATION MODEL (critical): the publisher runs on its OWN daemon thread, fully
# isolated. Every network op is wrapped — ALL exceptions are caught and turned
# into a recorded last_error + backoff. Metric values are read OUTSIDE any held
# lock (we snapshot them, then send). It never holds LOCK across a socket op.
# A missing / wrong / unreachable broker can therefore NEVER raise into, stall,
# or slow the sampler, the dashboard, or any request — worst case the thread
# sleeps and retries. When disabled / unconfigured it makes ZERO connections.
#
# SAFE-BY-DESIGN: this client only ever sends CONNECT / PUBLISH / PINGREQ /
# DISCONNECT. It NEVER sends SUBSCRIBE and never reads application messages, so
# there is no inbound command path — the broker cannot drive any action here.

# Each metric we expose as an HA sensor: (sensor key, friendly name,
# device_class|None, unit|None, json key in the shared state payload, icon|None).
_MQTT_SENSORS = (
    ("gpu_util",        "GPU Utilisation",   None,          "%",   "gpu_util",        "mdi:expansion-card"),
    ("gpu_vram_used",   "GPU VRAM Used",     "data_size",   "MB",  "gpu_vram_used",   "mdi:memory"),
    ("gpu_power",       "GPU Power",         "power",        "W",   "gpu_power",       "mdi:flash"),
    ("gpu_temp",        "GPU Temperature",   "temperature", "°C",  "gpu_temp",        "mdi:thermometer"),
    ("power_total",     "Total Power",       "power",        "W",   "power_total",     "mdi:flash"),
    ("cost_mtd",        "Energy Cost MTD",   "monetary",     None,  "cost_mtd",        "mdi:cash"),
    ("cost_projected",  "Energy Cost Projected", "monetary", None,  "cost_projected",  "mdi:cash-clock"),
    ("anomaly_active",  "Anomaly Active",    None,           None,  "anomaly_active",  "mdi:alert-decagram"),
    ("uptime_up",       "Uptime Checks Up",  None,           None,  "uptime_up",       "mdi:check-network"),
    ("uptime_total",    "Uptime Checks Total", None,         None,  "uptime_total",    "mdi:format-list-numbered"),
)

def _mqtt_collect_state():
    """Snapshot the SAME already-computed metric values the /metrics endpoint
    exposes, as a flat dict for the JSON state topic. Never raises; a failing
    block simply leaves that key absent. Reads outside any held lock except the
    brief, bounded LOCK the cost/anomaly helpers take internally (same as the
    Prometheus path) — never held across a socket op."""
    now = int(time.time())
    state, attrs = {}, {}
    try:
        state["gpu_util"]      = round(float(LATEST.get("util") or 0), 1)
        state["gpu_vram_used"] = int(LATEST.get("mem_used") or 0)
        state["gpu_vram_total"] = int(LATEST.get("mem_total") or 0)
        state["gpu_power"]     = round(float(LATEST.get("power") or 0), 1)
        state["gpu_temp"]      = round(float(LATEST.get("temp") or 0), 1)
    except Exception:
        pass
    try:
        state["power_total"] = round((LATEST.get("power") or 0)
                                     + (LATEST.get("cpu_power") or 0)
                                     + (LATEST.get("dram_power") or 0), 1)
    except Exception:
        pass
    # Per-disk fill % as a JSON-attributes blob (one sensor would be noisy; the
    # disks ride along as attributes on the state topic for HA templating).
    try:
        disks = {}
        for d in ((LATEST.get("host") or {}).get("disks") or []):
            mp = d.get("mount")
            if mp and d.get("pct") is not None:
                disks[mp] = d["pct"]
        if disks:
            attrs["disk_fill_pct"] = disks
    except Exception:
        pass
    try:
        ctx = _cost_ctx()
        with LOCK:
            cm = _cost_projection(DB.cursor(), ctx, now)
        if cm.get("enabled"):
            state["cost_mtd"]       = cm.get("month_to_date")
            state["cost_projected"] = cm.get("projected_month")
            attrs["currency"]       = cm.get("currency", "")
    except Exception:
        pass
    try:
        with LOCK:
            anom = _zscore_anomalies(DB.cursor(), now)
        items = anom.get("items") or []
        state["anomaly_active"] = "on" if items else "off"
        if items:
            attrs["anomaly_series"] = [it.get("key") for it in items]
    except Exception:
        pass
    try:
        ov = uptime_overview()
        checks = ov.get("checks") or []
        up = sum(1 for c in checks if (c.get("state") or c.get("status")) == "up")
        state["uptime_total"] = len(checks)
        state["uptime_up"]    = up
        # Per-check state, keyed by the SAME stable id used in discovery. Only the
        # label/slug ever rides the payload — never the raw target (creds-safe).
        for c in checks:
            cid = c.get("id") or _mqtt_slug(c.get("label"))
            sid = _mqtt_slug(cid)
            cstate = c.get("state")
            # binary_sensor reads payload_on='up'/payload_off='down'; an unknown
            # (no result yet) maps to 'down' for HA's connectivity class.
            state["uptime_%s_up" % sid] = "up" if cstate == "up" else "down"
            lat = c.get("last_latency_ms")
            if lat is not None:
                state["uptime_%s_latency" % sid] = lat
            # Cert checks also publish days-to-expiry (negative = expired) so HA can
            # alert ahead of a renewal. Only present for cert checks that have probed.
            if c.get("type") == "cert" and c.get("days_to_expiry") is not None:
                state["uptime_%s_cert_days" % sid] = c.get("days_to_expiry")
    except Exception:
        pass
    if attrs:
        state["_attributes"] = attrs
    return state

# ── Minimal MQTT 3.1.1 packet encoders (publish-only) ────────────────────────
def _mqtt_remaining_length(n):
    """Encode an MQTT 'Remaining Length' as a 1–4 byte varint (MQTT 3.1.1 §2.2.3)."""
    if n < 0 or n > 268435455:
        raise ValueError("remaining length out of range")
    out = bytearray()
    while True:
        digit = n % 128
        n //= 128
        if n > 0:
            digit |= 0x80
        out.append(digit)
        if n <= 0:
            break
    return bytes(out)

def _mqtt_str(s):
    """Encode a UTF-8 string with a 2-byte big-endian length prefix (§1.5.3)."""
    b = s.encode("utf-8")
    if len(b) > 0xFFFF:
        raise ValueError("mqtt string too long")
    return struct.pack(">H", len(b)) + b

def _mqtt_connect_packet(client_id, username=None, password=None, keepalive=60):
    """Build a CONNECT packet (protocol level 4 / MQTT 3.1.1, clean session)."""
    var = _mqtt_str("MQTT") + bytes([0x04])     # protocol name + level
    flags = 0x02                                # clean session
    if username:
        flags |= 0x80
        if password is not None:
            flags |= 0x40
    var += bytes([flags]) + struct.pack(">H", int(keepalive))
    payload = _mqtt_str(client_id)
    if username:
        payload += _mqtt_str(username)
        if password is not None:
            payload += _mqtt_str(password)
    body = var + payload
    return bytes([0x10]) + _mqtt_remaining_length(len(body)) + body

def _mqtt_publish_packet(topic, payload, retain=False, qos=0):
    """Build a PUBLISH packet. QoS 0 only (no packet id) — fine for sensor
    state; retain=True is used for discovery configs so HA keeps them."""
    if qos != 0:
        raise ValueError("only QoS 0 supported (publish-only client)")
    header = 0x30 | (0x01 if retain else 0x00)
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    body = _mqtt_str(topic) + payload
    return bytes([header]) + _mqtt_remaining_length(len(body)) + body

def _mqtt_disconnect_packet():
    return bytes([0xE0, 0x00])

def _mqtt_pingreq_packet():
    return bytes([0xC0, 0x00])

def _mqtt_device_block():
    """Shared HA `device` block so all sensors group under one device."""
    return {"identifiers": ["homelab_monitor"], "name": "HomeLab Monitor",
            "manufacturer": "HomeLab Monitor", "model": "GPU/host monitor",
            "sw_version": VERSION}

def _mqtt_slug(s):
    """Slugify text to a safe MQTT topic / HA object_id token: ascii lowercase,
    alnum + underscore only, collapsed. Used only as a fallback friendly token —
    the STABLE id for unique_id/topic is the check's DB id, never the mutable
    label. Empty/garbage input → 'x' so a topic is always well-formed."""
    out = []
    for ch in (s or ""):
        if ch.isalnum() and ord(ch) < 128:
            out.append(ch.lower())
        else:
            out.append("_")
    slug = "".join(out).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug or "x"

def _uptime_state_keys():
    """The flat state-topic JSON keys for the current uptime checks, as
    (binary_key, latency_key, friendly_label, stable_id) tuples — derived from
    the same uptime_overview() the discovery + state share so they stay in sync.
    Read OUTSIDE any held lock. Returns [] on any error or when no checks."""
    try:
        checks = uptime_overview().get("checks") or []
    except Exception:
        return []
    keys = []
    for c in checks:
        cid = c.get("id") or _mqtt_slug(c.get("label"))
        # DB id is hex (uuid4().hex) — already a safe token; slug-guard anyway.
        sid = _mqtt_slug(cid)
        keys.append(("uptime_%s_up" % sid, "uptime_%s_latency" % sid,
                     c.get("label") or sid, sid))
    return keys

def _mqtt_discovery_messages(prefix):
    """Return a list of (topic, payload_json, retain) for the retained HA
    discovery config topics — one per sensor, plus the shared device block."""
    prefix = (prefix or "homeassistant").strip().strip("/") or "homeassistant"
    state_topic = "homelab/monitor/state"
    dev = _mqtt_device_block()
    msgs = []
    for key, name, dclass, unit, jkey, icon in _MQTT_SENSORS:
        cfg = {
            "name": name,
            "unique_id": "homelab_" + key,
            "object_id": "homelab_" + key,
            "state_topic": state_topic,
            "value_template": "{{ value_json.%s }}" % jkey,
            "json_attributes_topic": state_topic,
            "json_attributes_template": "{{ value_json._attributes | default({}) | tojson }}",
            "availability_topic": "homelab/monitor/availability",
            "device": dev,
        }
        if dclass:
            cfg["device_class"] = dclass
        if unit:
            cfg["unit_of_measurement"] = unit
        if icon:
            cfg["icon"] = icon
        topic = "%s/sensor/homelab_%s/config" % (prefix, key)
        msgs.append((topic, json.dumps(cfg, separators=(",", ":")), True))

    # Per uptime check: a connectivity binary_sensor (ON=up/OFF=down) + a latency
    # sensor (ms), grouped under the SAME device. unique_id/topic key off the
    # STABLE check id (not the mutable label); the label is only the friendly
    # `name`. Only present when ≥1 check exists (zero checks ⇒ no extra topics).
    for bkey, lkey, label, sid in _uptime_state_keys():
        bin_cfg = {
            "name": "Uptime: " + label,
            "unique_id": "homelab_" + bkey,
            "object_id": "homelab_" + bkey,
            "state_topic": state_topic,
            "value_template": "{{ value_json.%s }}" % bkey,
            "device_class": "connectivity",
            "payload_on": "up",
            "payload_off": "down",
            "availability_topic": "homelab/monitor/availability",
            "icon": "mdi:check-network",
            "device": dev,
        }
        msgs.append(("%s/binary_sensor/homelab_%s/config" % (prefix, bkey),
                     json.dumps(bin_cfg, separators=(",", ":")), True))
        lat_cfg = {
            "name": "Uptime latency: " + label,
            "unique_id": "homelab_" + lkey,
            "object_id": "homelab_" + lkey,
            "state_topic": state_topic,
            "value_template": "{{ value_json.%s }}" % lkey,
            "unit_of_measurement": "ms",
            "device_class": "duration",
            "icon": "mdi:timer-outline",
            "availability_topic": "homelab/monitor/availability",
            "device": dev,
        }
        msgs.append(("%s/sensor/homelab_%s/config" % (prefix, lkey),
                     json.dumps(lat_cfg, separators=(",", ":")), True))
    return msgs

# Live status surfaced in the UI (no secrets). Updated only by the publisher
# thread + the test endpoint; read under _MQTT_LOCK.
_MQTT_LOCK = threading.Lock()
_MQTT_STATUS = {"connected": False, "last_publish": None, "last_error": None,
                "last_attempt": None, "publishes": 0}

def _mqtt_set_status(**kw):
    with _MQTT_LOCK:
        _MQTT_STATUS.update(kw)

def _mqtt_sanitize_err(exc):
    """Human-safe error text that never echoes broker creds. We control every
    message we raise; for socket/ssl errors we map to a coarse, safe string."""
    if isinstance(exc, socket.timeout):
        return "connection timed out"
    if isinstance(exc, socket.gaierror):
        return "host not found"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
        return "connection reset by broker"
    if isinstance(exc, OSError):
        # errno-based, no host/cred content
        return "network error" + (f" (errno {exc.errno})" if getattr(exc, "errno", None) else "")
    msg = str(exc)
    return msg if msg else exc.__class__.__name__

_MQTT_CONNACK_CODES = {
    0: "accepted", 1: "unacceptable protocol version", 2: "client id rejected",
    3: "broker unavailable", 4: "bad username or password", 5: "not authorised",
}

def _mqtt_open(host, port, tls, timeout=4.0):
    """Open a (optionally TLS) TCP socket to the broker with a short timeout.
    Caller is responsible for closing. Raises on failure (caught upstream)."""
    sock = socket.create_connection((host, int(port)), timeout=timeout)
    sock.settimeout(timeout)
    if tls:
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        sock = ctx.wrap_socket(sock, server_hostname=host)
    return sock

def _mqtt_read_connack(sock):
    """Read a CONNACK (4 bytes: 0x20 0x02 flags rc). Returns the return code.
    Raises on a malformed/short response or a non-zero code."""
    hdr = b""
    while len(hdr) < 4:
        chunk = sock.recv(4 - len(hdr))
        if not chunk:
            raise OSError("broker closed connection before CONNACK")
        hdr += chunk
    if hdr[0] != 0x20 or hdr[1] != 0x02:
        raise OSError("unexpected CONNACK from broker")
    rc = hdr[3]
    if rc != 0:
        raise OSError("broker rejected connection: " + _MQTT_CONNACK_CODES.get(rc, f"code {rc}"))
    return rc

def _mqtt_session_publish(cfg, publish_discovery=True):
    """Open ONE connection, CONNECT, optionally publish retained discovery
    configs, publish the availability=online + current state, DISCONNECT, close.
    Returns the number of messages published. Raises on any failure (callers
    wrap this). NEVER subscribes. cfg is a plain dict (no shared mutable state)."""
    sock = _mqtt_open(cfg["host"], cfg["port"], cfg["tls"])
    published = 0
    try:
        cid = "homelab-monitor-" + uuid.uuid4().hex[:8]
        sock.sendall(_mqtt_connect_packet(cid, cfg.get("user") or None,
                                          cfg.get("pass") if cfg.get("user") else None,
                                          keepalive=60))
        _mqtt_read_connack(sock)
        prefix = cfg.get("prefix") or "homeassistant"
        if publish_discovery:
            for topic, payload, retain in _mqtt_discovery_messages(prefix):
                sock.sendall(_mqtt_publish_packet(topic, payload, retain=retain))
                published += 1
        # availability (retained) + current state
        sock.sendall(_mqtt_publish_packet("homelab/monitor/availability", "online", retain=True))
        published += 1
        st = _mqtt_collect_state()
        sock.sendall(_mqtt_publish_packet("homelab/monitor/state",
                                          json.dumps(st, separators=(",", ":")), retain=True))
        published += 1
        try:
            sock.sendall(_mqtt_disconnect_packet())
        except Exception:
            pass
        return published
    finally:
        try:
            sock.close()
        except Exception:
            pass

def _mqtt_cfg_from_settings(s):
    """Extract a publisher config dict from settings, or None if not
    enabled/configured (so the thread stays inert). Validates the interval."""
    if (s.get("mqtt_enabled") or "0") != "1":
        return None
    host = (s.get("mqtt_host") or "").strip()
    if not host:
        return None
    try:
        port = int(s.get("mqtt_port") or 1883)
    except (TypeError, ValueError):
        port = 1883
    try:
        interval = max(10, int(s.get("mqtt_interval_sec") or 30))
    except (TypeError, ValueError):
        interval = 30
    return {"host": host, "port": port, "tls": (s.get("mqtt_tls") or "0") == "1",
            "user": (s.get("mqtt_user") or "").strip(),
            "pass": s.get("mqtt_pass") or "",
            "prefix": (s.get("mqtt_prefix") or "homeassistant").strip() or "homeassistant",
            "interval": interval}

def mqtt_worker():
    """Dedicated daemon loop. Idles (zero connections) while disabled/unconfigured.
    When enabled: connects, publishes discovery once per (re)connect, then state
    every interval. ALL exceptions are caught, recorded as last_error, and met
    with a capped backoff so a bad broker can never crash or stall anything."""
    backoff = 5
    discovery_sent = False
    while True:
        try:
            cfg = _mqtt_cfg_from_settings(get_settings())
        except Exception:
            cfg = None
        if not cfg:
            # Disabled / unconfigured: stay completely inert.
            with _MQTT_LOCK:
                if _MQTT_STATUS["connected"]:
                    _MQTT_STATUS["connected"] = False
            discovery_sent = False
            time.sleep(5)
            continue
        try:
            _mqtt_set_status(last_attempt=int(time.time()))
            n = _mqtt_session_publish(cfg, publish_discovery=not discovery_sent)
            discovery_sent = True
            backoff = 5
            with _MQTT_LOCK:
                _MQTT_STATUS["connected"] = True
                _MQTT_STATUS["last_publish"] = int(time.time())
                _MQTT_STATUS["last_error"] = None
                _MQTT_STATUS["publishes"] += n
            time.sleep(cfg["interval"])
        except Exception as e:
            discovery_sent = False     # re-send discovery after a reconnect
            with _MQTT_LOCK:
                _MQTT_STATUS["connected"] = False
                _MQTT_STATUS["last_error"] = _mqtt_sanitize_err(e)
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)

@app.route("/metrics")
def metrics():
    """Prometheus text-format scrape endpoint.

    Reads exclusively from the in-memory snapshots (LATEST / HEALTH) that the
    background collector keeps fresh.  No new I/O is triggered on each scrape,
    so double-sampling is impossible. The base GPU/host/container/model series
    come from prometheus_client gauges (when installed); the extra series (total
    power, per-disk bytes + fill %, month-cost projection, anomaly flags) are
    appended as pure-stdlib exposition text with their own distinct names — so
    the endpoint still serves rich metrics even without prometheus_client.
    """
    if not _PROM_OK:
        # No prometheus_client — serve only the pure-stdlib extra families, which
        # are still valid exposition (HELP/TYPE + numeric samples).
        body = _extra_metrics_text() or "# no metrics available yet\n"
        return Response(body, mimetype="text/plain; version=0.0.4; charset=utf-8")

    # Clear all multi-label gauges before re-populating so stale series vanish.
    for key in ("gpu_vram_used", "gpu_vram_total", "gpu_util", "gpu_temp", "gpu_power",
                "host_disk_used", "model_vram", "container_state", "systemd_unit"):
        _G[key].clear()

    # ── GPU ──────────────────────────────────────────────────────────────────
    gpu_label = "gpu0"
    _G["gpu_vram_used"].labels(gpu=gpu_label).set(LATEST.get("mem_used", 0))
    _G["gpu_vram_total"].labels(gpu=gpu_label).set(LATEST.get("mem_total", 0))
    _G["gpu_util"].labels(gpu=gpu_label).set(LATEST.get("util", 0))
    _G["gpu_temp"].labels(gpu=gpu_label).set(LATEST.get("temp", 0))
    _G["gpu_power"].labels(gpu=gpu_label).set(LATEST.get("power", 0))

    # ── Host ─────────────────────────────────────────────────────────────────
    host = LATEST.get("host") or {}
    _G["host_cpu"].set(host.get("cpu", 0))
    ram_total = host.get("ram_total") or 1
    ram_used  = host.get("ram_used", 0)
    _G["host_mem_used"].set(round(100 * ram_used / ram_total, 1))
    for disk in (host.get("disks") or []):
        _G["host_disk_used"].labels(mountpoint=disk["mount"]).set(disk.get("pct", 0))

    # ── Model VRAM ───────────────────────────────────────────────────────────
    for entry in (LATEST.get("models") or []):
        vram = entry.get("vram")
        if vram is not None:
            _G["model_vram"].labels(server=entry.get("service", "?"),
                                    model=entry.get("model", "?")).set(vram)

    # ── Docker containers ────────────────────────────────────────────────────
    docker = HEALTH.get("docker") or {}
    for ct in (docker.get("containers") or []):
        name  = ct.get("name", "?")
        state = ct.get("state", "unknown")
        _G["container_state"].labels(name=name, state=state).set(1)

    # ── Systemd units ────────────────────────────────────────────────────────
    systemd = HEALTH.get("systemd") or {}
    for svc in (systemd.get("services") or []):
        unit   = svc.get("name", "?")
        active = svc.get("active", "unknown")
        _G["systemd_unit"].labels(unit=unit, state=active).set(
            1 if active == "active" else 0)

    # Gauge families (prometheus_client) + the pure-stdlib extra families. Both
    # are valid exposition; their metric names are disjoint so there is no
    # duplicate HELP/TYPE.
    base = generate_latest().decode("utf-8")
    extra = _extra_metrics_text()
    return Response(base + extra, mimetype=CONTENT_TYPE_LATEST)


def _public_settings():
    """Same as get_settings(), but redacts secrets and reports their presence."""
    s = get_settings()
    out = {k: v for k, v in s.items() if k not in SETTING_SECRETS}
    for k in SETTING_SECRETS:
        out[k + "_set"] = bool(s.get(k))
    return out

@app.route("/api/hub/pubkey")
def api_hub_pubkey():
    return jsonify({"pubkey": get_hub_pubkey()})

@app.route("/api/hosts", methods=["GET", "POST"])
def api_hosts():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        host, err = add_host((body.get("name") or "").strip(),
                             (body.get("ssh_target") or "").strip(),
                             (body.get("tags") or "").strip())
        if err:
            return jsonify({"ok": False, "error": err}), 400
        return jsonify({"ok": True, "host": host}), 201
    return jsonify({"hosts": list_hosts()})

@app.route("/api/hosts/<name>", methods=["DELETE", "PATCH"])
def api_hosts_one(name):
    if request.method == "DELETE":
        ok = delete_host(name)
        return jsonify({"ok": ok}), (200 if ok else 404)
    body = request.get_json(silent=True) or {}
    host, err = update_host(
        name,
        ssh_target=(body.get("ssh_target").strip() if isinstance(body.get("ssh_target"), str) else None),
        tags=(body.get("tags").strip() if isinstance(body.get("tags"), str) else None),
    )
    if err:
        return jsonify({"ok": False, "error": err}), 400 if "look like" in err or "Nothing" in err else 404
    return jsonify({"ok": True, "host": host})

@app.route("/api/lan/scan")
def api_lan_scan():
    return jsonify(discover_lan())

@app.route("/api/host_data/<name>")
def api_host_data(name):
    """Per-host metric snapshot. `local` is the hub itself; remotes are served
    from the in-memory cache populated by host_poller()."""
    if name == "local":
        return jsonify({"name": "local", "host": enrich_os_upgrade(_local_now_snapshot()),
                        "at": int(time.time()), "online": True})
    with HOST_DATA_LOCK:
        entry = HOST_DATA.get(name)
    if not entry or "data" not in entry:
        # No successful poll yet (or never registered) — still respond 200 so
        # the UI can render a "waiting" state instead of erroring.
        return jsonify({"name": name, "online": False,
                        "error": (entry or {}).get("error") or "no data yet",
                        "at": (entry or {}).get("at"),
                        "host": None})
    age = int(time.time()) - int(entry["at"])
    return jsonify({"name": name, "host": enrich_os_upgrade(entry["data"].get("host", {})),
                    "at": entry["at"], "online": age < INTERVAL * 3,
                    "stale_for": age if age >= INTERVAL * 3 else 0,
                    "error": entry.get("error")})

# ── On-demand disk-usage scan (WizTree-style folder treemap) ──────────────────
# `du --max-depth=1` of a host path (read through the read-only HOST_ROOT mount),
# run in a background thread so a slow scan of a big disk never blocks a request.
# The UI polls until state=="done". Results cached per path; one filesystem only
# (--one-file-system) so scanning "/" doesn't wander into other mounted disks.
_DISK_SCAN, _DISK_SCAN_LOCK = {}, threading.Lock()
_DISK_SCAN_TTL = 900   # reuse a completed scan for 15 min

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

def _disk_scan_worker(path, real):
    base = HOST_ROOT.rstrip("/") if os.path.isdir(HOST_ROOT) else ""
    def hostp(p):
        return (p[len(base):] or "/") if base and p.startswith(base) else p
    try:
        # --max-depth=2 gives two levels at once (folder + its sub-folders) for a
        # nested treemap. du recurses fully regardless of --max-depth, so this is
        # no costlier than depth 1 — it only prints more.
        # `--` ends option parsing so a path can never be read as a du flag.
        # `real` always starts with "/" already, so this is belt-and-suspenders.
        r = subprocess.run(["du", "-b", "--max-depth=2", "--one-file-system", "--", real],
                           capture_output=True, text=True, timeout=600)
        sizes = {}
        for ln in (r.stdout or "").splitlines():
            parts = ln.split("\t", 1)
            if len(parts) != 2:
                continue
            try:
                sizes[os.path.normpath(parts[1])] = int(parts[0])
            except ValueError:
                continue
        root = os.path.normpath(real)
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
        entries = [build(p, b, 1) for p, b in tops]
        free = None
        try:
            s = os.statvfs(real); free = s.f_bavail * s.f_frsize
        except Exception:
            pass
        with _DISK_SCAN_LOCK:
            _DISK_SCAN[path] = {"state": "done", "at": int(time.time()),
                                "total": total, "entries": entries, "free": free, "error": None}
    except subprocess.TimeoutExpired:
        with _DISK_SCAN_LOCK:
            _DISK_SCAN[path] = {"state": "error", "at": int(time.time()),
                                "error": "scan timed out — folder too large"}
    except Exception as e:
        with _DISK_SCAN_LOCK:
            _DISK_SCAN[path] = {"state": "error", "at": int(time.time()), "error": str(e)[:200]}

@app.route("/api/disk_scan")
def api_disk_scan():
    path = os.path.normpath(request.args.get("path") or "/")
    rescan = request.args.get("rescan") == "1"
    real = _safe_host_dir(path)
    if not real:
        return jsonify({"path": path, "state": "error", "error": f"not a readable directory: {path}"})
    with _DISK_SCAN_LOCK:
        ent = _DISK_SCAN.get(path)
        if ent and not rescan:
            if ent["state"] == "scanning":
                return jsonify({"path": path, "state": "scanning"})
            if ent["state"] == "done" and time.time() - ent["at"] < _DISK_SCAN_TTL:
                return jsonify({"path": path, **ent})
            if ent["state"] == "error" and time.time() - ent["at"] < 20:
                return jsonify({"path": path, **ent})
        _DISK_SCAN[path] = {"state": "scanning", "at": int(time.time())}
    threading.Thread(target=_disk_scan_worker, args=(path, real), daemon=True).start()
    return jsonify({"path": path, "state": "scanning"})

_GPU_VENDOR_ORDER = {"nvidia": 0, "amd": 1, "intel": 2, "unknown": 3}

def _fleet_vendor_summary(rows):
    """Count GPUs by vendor across ONLINE fleet rows, for the All-hosts summary
    chip. Each online host that reports a GPU contributes one to its
    representative vendor's tally (the fleet row carries a single aggregate GPU);
    a GPU with no/blank/unrecognised vendor — e.g. an older payload that predates
    the vendor field — counts as 'unknown'. Offline hosts and GPU-less hosts are
    skipped. Returns an ordered list of {vendor, count}, NVIDIA→AMD→Intel→unknown
    then alpha, or [] when no online host has a GPU."""
    counts = {}
    for r in (rows or []):
        if not r.get("online"):
            continue
        gpu = (r.get("host") or {}).get("gpu")
        if not gpu:
            continue
        v = str(gpu.get("vendor") or "unknown").lower()
        if v not in _GPU_VENDOR_ORDER:
            v = "unknown"
        counts[v] = counts.get(v, 0) + 1
    return [{"vendor": k, "count": counts[k]}
            for k in sorted(counts, key=lambda k: (_GPU_VENDOR_ORDER.get(k, 9), k))]

@app.route("/api/fleet")
def api_fleet():
    """Compact summary KPIs for every host in the fleet. Drives the All-hosts
    table. Order: local first, then registered hosts in the order they were
    added."""
    hosts = list_hosts()
    rows  = []

    # Local row
    rows.append({"name": "local", "label": socket.gethostname() + " (this hub)",
                 "ssh_target": None, "host": enrich_os_upgrade(_local_now_snapshot()),
                 "at": int(time.time()), "online": True, "is_local": True,
                 "last_check": {"summary": {"overall": "ok"}}})

    with HOST_DATA_LOCK:
        for h in hosts:
            entry = HOST_DATA.get(h["name"]) or {}
            data  = entry.get("data") or {}
            at    = entry.get("at")
            online = bool(data) and at and (int(time.time()) - at) < INTERVAL * 3
            rows.append({
                "name": h["name"],
                "label": h["name"],
                "ssh_target": h["ssh_target"],
                "host": enrich_os_upgrade(data.get("host")) if data else None,
                "at": at,
                "online": online,
                "is_local": False,
                "last_check": h.get("last_check"),
                "error": entry.get("error"),
            })
    return jsonify({"hosts": rows, "interval": INTERVAL,
                    "gpu_vendors": _fleet_vendor_summary(rows)})

@app.route("/api/hosts/<name>/test", methods=["POST"])
def api_hosts_test(name):
    result = probe_host(name)
    if result is None:
        return jsonify({"ok": False, "error": "no such host"}), 404
    return jsonify({"ok": True, "result": result})

@app.route("/api/hosts/<name>/run", methods=["POST"])
def api_hosts_run(name):
    """Execute a command on a registered host. Body: {cmd, sudo_password?}.
    The sudo password is processed in-memory: piped via stdin to `sudo -S`
    on the remote, NEVER stored in the DB, NEVER written to logs, NEVER
    in any process's argv. Don't pass arbitrary cmd from untrusted users —
    the API is reachable to anyone on the LAN who can hit the dashboard."""
    body = request.get_json(silent=True) or {}
    cmd = (body.get("cmd") or "").strip()
    sudo_password = body.get("sudo_password") or None
    if not cmd:
        return jsonify({"ok": False, "error": "cmd required"}), 400
    result = run_on_host(name, cmd, sudo_password=sudo_password)
    # Drop the password reference ASAP — Python keeps the string object until
    # GC, but at least we don't hold our own reference past this point.
    sudo_password = None
    body = None
    if result is None:
        return jsonify({"ok": False, "error": "no such host"}), 404
    return jsonify(result)

@app.route("/api/backup")
def api_backup_download():
    """Stream a consistent SQLite snapshot (VACUUM INTO) of the live database."""
    if _DB_MAINTENANCE:
        return jsonify({"ok": False, "error": "Database maintenance in progress."}), 503
    if not _data_dir_writable():
        return jsonify({"ok": False, "error": "Cannot write backup — mount a writable /data volume."}), 400
    tmp_path = None
    try:
        with LOCK:
            fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix=".backup_", dir=_data_dir())
            os.close(fd)
            db_backup.vacuum_into(DB, tmp_path)
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
                     download_name=db_backup.backup_filename())

@app.route("/api/backup/restore", methods=["POST"])
def api_backup_restore():
    """Replace the live database with an uploaded backup snapshot."""
    global _DB_MAINTENANCE
    if _DB_MAINTENANCE:
        return jsonify({"ok": False, "error": "Database maintenance already in progress."}), 503
    if not _data_dir_writable():
        return jsonify({"ok": False, "error": "Cannot restore — mount a writable /data volume."}), 400
    upload = request.files.get("backup")
    if not upload or not upload.filename:
        return jsonify({"ok": False, "error": "No backup file uploaded."}), 400

    upload_path = None
    try:
        fd, upload_path = tempfile.mkstemp(suffix=".db", prefix=".restore_upload_", dir=_data_dir())
        os.close(fd)
        upload.save(upload_path)
        ok, err = db_backup.validate_backup(upload_path)
        if not ok:
            return jsonify({"ok": False, "error": err}), 400

        with LOCK:
            _DB_MAINTENANCE = True
            try:
                if os.path.isfile(DB_PATH):
                    shutil.copy2(DB_PATH, "%s.pre-restore-%d.bak" % (DB_PATH, int(time.time())))
                try:
                    DB.close()
                except Exception:
                    pass
                db_backup.remove_wal_sidecars(DB_PATH)
                os.replace(upload_path, DB_PATH)
                upload_path = None
                db_backup.remove_wal_sidecars(DB_PATH)
                reopen_db()
            except Exception as e:
                try:
                    reopen_db()
                except Exception:
                    pass
                return jsonify({"ok": False, "error": "Restore failed: %s" % e}), 500
            finally:
                _DB_MAINTENANCE = False

        return jsonify({"ok": True,
                        "message": "Backup restored. History and settings have been reloaded."})
    finally:
        if upload_path:
            try: os.unlink(upload_path)
            except OSError: pass

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        # Empty string clears a setting; missing key leaves it unchanged.
        # Secrets pass through the "_set: false" sentinel from the UI as a way
        # to clear without revealing the current value.
        updates = {k: body[k] for k in body if k in SETTING_DEFAULTS}
        err = _validate_url_settings(updates)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        save_settings(updates)
    return jsonify({"version": VERSION, "settings": _public_settings()})

@app.route("/api/mqtt/status")
def api_mqtt_status():
    """Live publisher status (no secrets) for the Settings UI."""
    s = get_settings()
    with _MQTT_LOCK:
        st = dict(_MQTT_STATUS)
    st["enabled"] = (s.get("mqtt_enabled") or "0") == "1"
    st["configured"] = bool((s.get("mqtt_host") or "").strip())
    return jsonify(st)

@app.route("/api/mqtt/test", methods=["POST"])
def api_mqtt_test():
    """Attempt ONE connect + discovery + state publish using the body's settings
    (falling back to the saved ones), WITHOUT enabling the integration. Returns a
    clean ok/error — never a 500, never echoes broker credentials. Body may carry
    a fresh mqtt_pass; the literal 'CLEAR' and the masked placeholder are treated
    as 'use the saved password'."""
    body = request.get_json(silent=True) or {}
    s = get_settings()
    host = (body.get("mqtt_host") if body.get("mqtt_host") is not None else s.get("mqtt_host")) or ""
    host = host.strip()
    if not host:
        return jsonify({"ok": False, "error": "not configured — set a broker host first"}), 200
    pw = body.get("mqtt_pass")
    if pw is None or pw == "" or pw == "CLEAR":
        pw = s.get("mqtt_pass") or ""     # keep the saved one
    cfg = {
        "host": host,
        "port": int(body.get("mqtt_port") or s.get("mqtt_port") or 1883) if str(body.get("mqtt_port") or s.get("mqtt_port") or "1883").isdigit() else 1883,
        "tls": (str(body.get("mqtt_tls") if body.get("mqtt_tls") is not None else s.get("mqtt_tls")) == "1"),
        "user": ((body.get("mqtt_user") if body.get("mqtt_user") is not None else s.get("mqtt_user")) or "").strip(),
        "pass": pw,
        "prefix": ((body.get("mqtt_prefix") if body.get("mqtt_prefix") is not None else s.get("mqtt_prefix")) or "homeassistant").strip() or "homeassistant",
    }
    try:
        n = _mqtt_session_publish(cfg, publish_discovery=True)
        return jsonify({"ok": True, "published": n,
                        "message": f"Connected and published {n} message(s)."}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": _mqtt_sanitize_err(e)}), 200

@app.route("/api/notify/test", methods=["POST"])
def api_notify_test():
    """Send a one-shot test alert using the currently saved settings."""
    s = get_settings()
    if not _configured_channels(s):
        return jsonify({"ok": False, "results": [],
                        "reason": "No notification channel configured."}), 400
    results = dispatch_alert(s, "info",
                             "✅ HomeLab Monitor — test alert",
                             "If you see this, alerts are wired up correctly.")
    return jsonify({"ok": all(ok for _, ok, _ in results),
                    "results": [{"channel": c, "ok": ok, "error": err} for c, ok, err in results]})

@app.route("/api/alerts/channels/test", methods=["POST"])
def api_alerts_channel_test():
    """Send a one-shot test notification to a single named channel (or 'all').
    Validates against the currently-saved channel config; records the attempt in
    alert history. Only sends data out — never touches the host."""
    body = request.get_json(silent=True) or {}
    channel = (body.get("channel") or "all").strip()
    if channel not in _VALID_CHANNELS:
        return jsonify({"ok": False, "error": "Unknown channel."}), 400
    s = get_settings()
    if channel == "all" and not _configured_channels(s):
        return jsonify({"ok": False, "results": [],
                        "reason": "No channel configured."}), 400
    results = dispatch_alert(s, "info",
                             "✅ HomeLab Monitor — test notification",
                             "If you see this, this channel is wired up correctly.",
                             channel=channel)
    for ch, ok, err in results:
        record_alert(None, "channel test", "info", ch, "sent" if ok else "error",
                     "Test notification", None if ok else (err or ""))
    return jsonify({"ok": all(ok for _, ok, _ in results),
                    "results": [{"channel": c, "ok": ok, "error": err} for c, ok, err in results]})

# ── NL alert authoring (E1): "Describe an alert in plain English" ─────────────
# The Lab Copilot DRAFTS a structured alert rule from a plain-English sentence,
# then the human reviews + confirms it in the SAME manual form and saves through
# the EXISTING validated create path. The LLM only ever proposes; it never
# persists, never mutates the host, and can only draft a normal notification rule
# (the engine has no host-mutating rule type). LLM is called EXCLUSIVELY on the
# explicit POST /api/alerts/rules/from_text action below — never on any poll,
# collect, GET, or the rules-list. Schema context is assembled under LOCK and the
# LOCK is released before the ollama HTTP call.
RULE_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "ctype": {"type": "string", "enum": sorted(_RULE_TYPES)},
        "params": {"type": "object"},
        "channel": {"type": "string"},
        "level": {"type": "string", "enum": ["info", "warning", "critical"]},
        "cooldown_min": {"type": "integer"},
        "summary": {"type": "string"},
    },
    "required": ["ctype", "params", "summary"],
}
_RULE_DRAFT_MAX_TEXT = 500      # cap the NL input length fed to the model
_RULE_DRAFT_MAX_COOLDOWN = 10080   # one week, in minutes


def _rule_schema_context():
    """Assemble the REAL rule schema the engine accepts — the enumerated rule
    types, allowed anomaly series, configured channels and existing uptime/cert
    checks — so the model can only draft within what create_rule() validates.

    Reads settings + uptime_checks under LOCK, then returns a plain dict. NO LLM
    call happens here and the LOCK is released before the caller talks to ollama.
    Never raises."""
    try:
        channels = ["all"] + list(_configured_channels(get_settings()))
    except Exception:
        channels = ["all"]
    checks, cert_ids, check_ids = [], set(), set()
    try:
        with LOCK:
            rows = DB.execute("SELECT id,label,type FROM uptime_checks").fetchall()
        for cid, label, ctype in rows:
            checks.append({"id": cid, "label": label, "type": ctype})
            check_ids.add(cid)
            if ctype == "cert":
                cert_ids.add(cid)
    except Exception:
        pass
    return {
        "types": sorted(_RULE_TYPES),
        "channels": channels,
        "series": ["any"] + [k for k, *_ in _ANOMALY_SERIES],
        "levels": list(LEVELS.keys()),
        "checks": checks,
        "check_ids": check_ids,
        "cert_ids": cert_ids,
    }


def _rule_draft_prompt(text, ctx):
    """Ground the model in the exact rule vocabulary this engine supports."""
    series = ", ".join(ctx["series"])
    channels = ", ".join(ctx["channels"])
    checks = ctx["checks"]
    chk = ", ".join("%s (%s, %s)" % (c["id"], c["label"], c["type"]) for c in checks) or "(none configured)"
    return (
        "You are the Lab Copilot for a self-hosted homelab monitoring dashboard. "
        "Turn the user's plain-English request into ONE alert RULE this engine can "
        "run. Alert rules ONLY send a notification when a signal the dashboard "
        "already computes crosses a line — they never change anything on the host. "
        "You MUST pick a ctype from this exact list and only use its params:\n"
        "- anomaly: params {\"series\": one of [" + series + "]} — fires while that "
        "metric is behaving abnormally (statistical anomaly). Use this for "
        "'temperature/utilisation/power/VRAM spike/unusual' requests (map GPU temp "
        "-> gpu_temp, GPU load -> gpu_util, power -> gpu_power, VRAM -> gpu_vram). "
        "This engine has NO fixed-threshold-with-duration rule, so a request like "
        "'temp over 85 for 10 min' becomes an anomaly on gpu_temp.\n"
        "- disk_eta: params {\"days\": number} — fires when a disk is forecast to "
        "fill within N days.\n"
        "- vram_eta: params {\"days\": number} — fires when GPU VRAM is forecast to "
        "fill within N days.\n"
        "- cost_budget: params {\"budget\": number} — fires when the projected "
        "month electricity cost exceeds this budget.\n"
        "- incident: params {\"severity\": \"warning\"|\"critical\"} — fires when a "
        "correlated incident at/above that severity opens.\n"
        "- uptime_down: params {\"check_id\": one of the ids below or \"any\"} — "
        "fires when an uptime check is down.\n"
        "- cert_expiry: params {\"check_id\": a cert check id or \"any\"} — fires "
        "when a TLS certificate is near expiry.\n"
        "- slo_burn: params {\"check_id\": id or \"any\", \"policy\": "
        "\"single\"|\"multi_window\", and for single \"burn_threshold\": number, "
        "for multi_window \"fast_burn\": number and \"slow_burn\": number} — fires "
        "when a check burns its SLO error budget too fast.\n"
        "Existing uptime checks (id, label, type): " + chk + ".\n"
        "Return a JSON object with: \"name\" (a short rule name), \"ctype\" (from "
        "the list), \"params\" (only the keys for that ctype), \"channel\" (one of "
        "[" + channels + "], default \"all\"), \"level\" (\"info\", \"warning\", or "
        "\"critical\"), \"cooldown_min\" (integer minutes, default 60), and "
        "\"summary\" (one plain-English sentence describing what the rule will do). "
        "If unsure of a specific target, use \"any\". No markdown.\n\n"
        "REQUEST: " + text.strip() + "\n")


def _clamp_num(v, lo, hi, default):
    """Return (value, adjusted) — coerce to float, clamp to [lo,hi], or use
    default when unparseable. `adjusted` is True when we changed what the model
    gave us (so the caller can note it as an assumption)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default, True
    c = max(lo, min(hi, f))
    return c, (c != f)


def _coerce_params_for_type(ctype, p, ctx, assumptions):
    """Coerce the model's params into something _validate_rule() will accept for
    this ctype, clamping out-of-range numbers and dropping unknown targets to
    'any'. Appends human-readable notes to `assumptions`."""
    if not isinstance(p, dict):
        p = {}
    if ctype == "anomaly":
        series = p.get("series")
        if not (isinstance(series, str) and series in ctx["series"]):
            if isinstance(series, str) and series.strip():
                assumptions.append("Unknown series '%s' — watching any series instead." % series.strip()[:40])
            series = "any"
        return {"series": series}
    if ctype in ("disk_eta", "vram_eta"):
        days, adj = _clamp_num(p.get("days"), 0.1, 3650.0, 3.0)
        if adj:
            assumptions.append("Days threshold set to %g." % days)
        return {"days": days}
    if ctype == "cost_budget":
        budget, adj = _clamp_num(p.get("budget"), 0.0, 1e9, 50.0)
        if adj:
            assumptions.append("Budget set to %g." % budget)
        return {"budget": budget}
    if ctype == "incident":
        sev = p.get("severity")
        if sev not in ("warning", "critical"):
            if sev:
                assumptions.append("Incident severity defaulted to warning.")
            sev = "warning"
        return {"severity": sev}
    if ctype in ("uptime_down", "cert_expiry"):
        cid = p.get("check_id")
        valid_ids = ctx["cert_ids"] if ctype == "cert_expiry" else ctx["check_ids"]
        if not (isinstance(cid, str) and cid in valid_ids):
            if isinstance(cid, str) and cid.strip() and cid != "any":
                assumptions.append("No matching check '%s' — applying to any check." % cid.strip()[:40])
            cid = "any"
        return {"check_id": cid}
    if ctype == "slo_burn":
        cid = p.get("check_id")
        if not (isinstance(cid, str) and cid in ctx["check_ids"]):
            if isinstance(cid, str) and cid.strip() and cid != "any":
                assumptions.append("No matching check '%s' — applying to any check." % cid.strip()[:40])
            cid = "any"
        policy = p.get("policy")
        if policy not in ("single", "multi_window"):
            policy = "single"
        if policy == "multi_window":
            fb, a1 = _clamp_num(p.get("fast_burn"), 0.1, 1000.0, 14.4)
            sb, a2 = _clamp_num(p.get("slow_burn"), 0.1, 1000.0, 6.0)
            if a1 or a2:
                assumptions.append("Burn rates set to fast %g× / slow %g×." % (fb, sb))
            return {"check_id": cid, "policy": "multi_window", "fast_burn": fb, "slow_burn": sb}
        bt, adj = _clamp_num(p.get("burn_threshold"), 0.1, 1000.0, 1.0)
        if adj:
            assumptions.append("Burn-rate threshold set to %g×." % bt)
        return {"check_id": cid, "policy": "single", "burn_threshold": bt}
    return {}


def _rule_plain_summary(pr):
    """Deterministic one-line description of a proposed rule (server-side fallback
    when the model omits a usable summary). Never raises."""
    ct = pr.get("ctype") or ""
    p = pr.get("params") or {}
    if ct == "anomaly":
        return "Alert when %s is anomalous." % (p.get("series") or "any series")
    if ct == "disk_eta":
        return "Alert when a disk is forecast to fill within %g days." % float(p.get("days", 3))
    if ct == "vram_eta":
        return "Alert when GPU VRAM is forecast to fill within %g days." % float(p.get("days", 3))
    if ct == "cost_budget":
        return "Alert when projected month cost exceeds %g." % float(p.get("budget", 50))
    if ct == "incident":
        return "Alert when a correlated incident at/above %s opens." % (p.get("severity") or "warning")
    if ct == "uptime_down":
        return "Alert when uptime check '%s' goes down." % (p.get("check_id") or "any")
    if ct == "cert_expiry":
        return "Alert when the TLS cert for '%s' is near expiry." % (p.get("check_id") or "any")
    if ct == "slo_burn":
        return "Alert when '%s' burns its SLO error budget too fast." % (p.get("check_id") or "any")
    return "Could not map this request to a supported rule type."


def _coerce_drafted_rule(obj, ctx, text):
    """Turn a raw LLM object into a safe PROPOSED rule + assumptions + type_ok.
    Fills sane defaults, clamps out-of-range values, and flags an unmappable type.
    Pure (no LOCK, no LLM, no DB writes). Never raises."""
    assumptions = []
    if not isinstance(obj, dict):
        obj = {}
    ctype = obj.get("ctype")
    ctype = ctype.strip() if isinstance(ctype, str) else ""
    type_ok = ctype in _RULE_TYPES
    name = obj.get("name")
    name = name.strip() if isinstance(name, str) and name.strip() else ""
    if not name:
        name = (text.strip()[:48] or "New alert")
        assumptions.append("Named the rule from your description.")
    channel = obj.get("channel")
    channel = channel.strip() if isinstance(channel, str) else ""
    if channel not in _VALID_CHANNELS:
        if channel:
            assumptions.append("Unknown channel '%s' — using all configured channels." % channel[:40])
        channel = "all"
    level = obj.get("level")
    level = level.strip().lower() if isinstance(level, str) else ""
    if level not in LEVELS:
        if level:
            assumptions.append("Severity defaulted to warning.")
        level = "warning"
    cd_raw = obj.get("cooldown_min", 60)
    try:
        cd = int(float(cd_raw))
    except (TypeError, ValueError):
        cd, adj = 60, True
    else:
        adj = False
    if cd < 0:
        cd, adj = 0, True
    if cd > _RULE_DRAFT_MAX_COOLDOWN:
        cd, adj = _RULE_DRAFT_MAX_COOLDOWN, True
    if adj:
        assumptions.append("Cooldown set to %d minutes." % cd)
    params = _coerce_params_for_type(ctype, obj.get("params"), ctx, assumptions) if type_ok else {}
    proposal = {"name": name, "ctype": ctype if type_ok else "", "params": params,
                "channel": channel, "level": level, "cooldown_min": cd, "enabled": False}
    return proposal, assumptions, type_ok


def _draft_rule_from_text(text, ctx):
    """The ONE new LLM caller in this feature. Returns
    (proposal|None, assumptions, valid, llm_status, summary). Never persists,
    never raises. `proposal` is a NOT-SAVED candidate rule dict; `valid` means the
    existing _validate_rule() accepts it verbatim (defense in depth)."""
    raw, err = _ollama_generate(_rule_draft_prompt(text, ctx), fmt=RULE_DRAFT_SCHEMA)
    if raw is None:
        return None, [], False, (err or "unreachable"), ""
    try:
        obj = json.loads(raw)
    except Exception:
        obj = None
    llm_summary = ""
    if isinstance(obj, dict):
        s = obj.get("summary")
        if isinstance(s, str) and s.strip():
            llm_summary = s.strip()[:300]
    proposal, assumptions, type_ok = _coerce_drafted_rule(obj if isinstance(obj, dict) else {}, ctx, text)
    valid = False
    if type_ok:
        _clean, verr = _validate_rule({**proposal, "enabled": False})
        if verr:
            assumptions.append("This draft still needs a fix before it can be saved: %s" % verr)
        else:
            valid = True
    else:
        assumptions.insert(0, "Could not map your request to a rule this engine supports — edit it in the form below.")
    summary = llm_summary or _rule_plain_summary(proposal)
    return proposal, assumptions, valid, "ok", summary


@app.route("/api/alerts/rules/from_text", methods=["POST"])
def api_alert_rule_from_text():
    """Draft (NOT save) an alert rule from a plain-English description via the
    local LLM. The ONLY new LLM caller — never on a poll/collect/GET path. Always
    200; returns a PROPOSED rule the UI pre-fills into the manual form, plus the
    assumptions it filled and a `valid` flag. Saving happens only when the user
    confirms, through the existing validated /api/alerts/rules create path."""
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    now = int(time.time())
    ctx = _rule_schema_context()   # assembled under LOCK; released before any LLM call
    schema_out = {"types": ctx["types"], "channels": ctx["channels"],
                  "series": ctx["series"], "levels": ctx["levels"]}
    if not text:
        return jsonify({"ok": True, "valid": False, "enabled": COPILOT_ENABLED,
                        "model": COPILOT_MODEL, "llm_status": "no_text",
                        "proposal": None, "assumptions": [], "summary": "",
                        "schema": schema_out})
    if len(text) > _RULE_DRAFT_MAX_TEXT:
        text = text[:_RULE_DRAFT_MAX_TEXT]
    if not COPILOT_ENABLED:
        return jsonify({"ok": True, "valid": False, "enabled": False,
                        "model": COPILOT_MODEL, "llm_status": "disabled",
                        "proposal": None, "assumptions": [], "summary": "",
                        "schema": schema_out})
    proposal, assumptions, valid, llm_status, summary = _draft_rule_from_text(text, ctx)
    return jsonify({"ok": True, "valid": valid, "enabled": COPILOT_ENABLED,
                    "model": COPILOT_MODEL, "llm_status": llm_status,
                    "proposal": proposal, "assumptions": assumptions,
                    "summary": summary, "schema": schema_out})


@app.route("/api/alerts/rules/preview", methods=["POST"])
def api_alert_rule_preview():
    """Read-only 'would it fire now?' dry-run for an alert-rule spec (the same shape
    /api/alerts/rules accepts). Validates the spec through the exact _validate_rule()
    path, then evaluates its CONDITION against the CURRENT live signal bundle using
    the SAME pure _eval_rule() the engine uses — but with ZERO side effects: NO
    dispatch to any channel, NO cooldown/last-fired/snooze write, NO rule row
    created/updated/deleted, NO LLM. An explicit user action only — never reachable
    from a poll/collect path. Always 200; an invalid spec comes back valid:false with
    a clear message (no 500)."""
    body = request.get_json(silent=True) or {}
    # Validate as a would-be rule. The `enabled` flag is irrelevant to an instant
    # condition check, and a preview must never depend on a name the user hasn't
    # typed yet — so default a placeholder name and force enabled off.
    spec = {**body, "enabled": False}
    if not (spec.get("name") or "").strip():
        spec["name"] = "preview"
    clean, err = _validate_rule(spec)
    if err:
        return jsonify({"ok": True, "valid": False, "would_fire": None,
                        "detail": err, "observed": {}}), 200
    try:
        signals = _live_signal_bundle()
    except Exception as e:
        print("rule preview signal error:", e, flush=True)
        return jsonify({"ok": True, "valid": True, "ctype": clean["ctype"],
                        "would_fire": None,
                        "detail": "Live signals are momentarily unavailable — try again.",
                        "observed": {}}), 200
    try:
        would_fire, detail, observed = _preview_rule(clean, signals)
    except Exception as e:
        print("rule preview eval error:", e, flush=True)
        return jsonify({"ok": True, "valid": True, "ctype": clean["ctype"],
                        "would_fire": None,
                        "detail": "Could not evaluate that rule right now.",
                        "observed": {}}), 200
    return jsonify({"ok": True, "valid": True, "ctype": clean["ctype"],
                    "would_fire": would_fire, "detail": detail,
                    "observed": observed}), 200


# ── Alert Advisor — proactive alert-rule recommendations ─────────────────────
# The Copilot studies the monitor's OWN live/historical signals (the SAME
# read-only bundle _eval_rule/preview consume) and derives VALID alert-rule
# candidates a user should probably create — each with a plain-English rationale
# and a one-click path into the EXISTING validated draft/create form. This is
# strictly advice: the advisor NEVER persists a rule, dispatches, or writes any
# state. The ranking and every rule spec are DETERMINISTIC; the local LLM may
# only ENRICH the rationale prose on the explicit advisor action (never on any
# poll path), and its absence degrades gracefully (deterministic rationale
# stands). Suggestions flow through the human-confirmed create path only.

def _advisor_covered(existing, ctype, params):
    """True if the user already has a rule of this ctype whose target matches, so
    we never suggest a duplicate. Matching is per-ctype: series for anomaly,
    check_id (with 'any' subsuming a specific target) for the uptime family, and
    a bare presence match for the single-target types (disk_eta/vram_eta/
    cost_budget/incident) — one such rule is enough coverage."""
    want_series = (params or {}).get("series")
    want_cid = (params or {}).get("check_id")
    for r in existing:
        if r.get("ctype") != ctype:
            continue
        rp = r.get("params") or {}
        if ctype == "anomaly":
            rs = rp.get("series") or "any"
            if rs == "any" or rs == want_series:
                return True
        elif ctype in ("uptime_down", "cert_expiry", "slo_burn"):
            rc = rp.get("check_id") or "any"
            if rc == "any" or rc == want_cid:
                return True
        else:
            # disk_eta / vram_eta / cost_budget / incident: single-subject types.
            return True
    return False


def _advisor_candidates(signals, existing, now=None):
    """Deterministically derive alert-rule recommendations from the live signal
    bundle. Returns a list of dicts:
      {ctype, spec, severity, rationale, evidence, already_covered}
    where `spec` is a candidate rule in the EXACT shape create_rule/_validate_rule
    accept (name/ctype/channel/level/cooldown_min/params/enabled). PURE: no I/O,
    no LLM, no mutation. Ranked by severity then urgency; caller caps the list.
    `already_covered` candidates are still emitted (marked) so the UI can grey
    them; the caller filters them out of the actionable list."""
    now = now or int(time.time())
    out = []

    def add(ctype, params, level, severity, rationale, evidence, name):
        spec = {"name": name, "ctype": ctype, "channel": "all",
                "level": level, "cooldown_min": 60, "params": params,
                "enabled": False}
        # Defense in depth: every suggested spec MUST pass the real _validate_rule
        # (the exact create path). If a targeted check_id can't validate (e.g. the
        # id isn't a DB-backed uptime_checks row), fall back to "any" so the
        # one-click create never bounces; drop the candidate only if even that
        # fails. The one_click form still lets the user pick a specific check.
        _clean, verr = _validate_rule(spec)
        if verr:
            if ctype in ("uptime_down", "cert_expiry", "slo_burn"):
                params = {**params, "check_id": "any"}
                spec = {**spec, "params": params}
                _clean, verr = _validate_rule(spec)
            if verr:
                print("advisor: dropped invalid candidate %s: %s" % (ctype, verr),
                      flush=True)
                return
        out.append({
            "ctype": ctype, "spec": spec, "severity": severity,
            "rationale": rationale, "evidence": evidence,
            "already_covered": _advisor_covered(existing, ctype, params),
        })

    # 1) Disk fill ETA — suggest a disk_eta rule with a threshold above the ETA. --
    for d in (signals.get("disk") or []):
        if d.get("status") != "filling":
            continue
        eta = d.get("eta_days")
        if eta is None:
            continue
        mount = d.get("mount") or "?"
        # Threshold: round the ETA up to a sensible headroom (14d floor) so the
        # rule warns BEFORE the disk actually fills.
        thr = 14 if eta <= 14 else int(math.ceil(eta / 7.0) * 7)
        sev = "crit" if eta < RECO_DISK_CRIT_DAYS else "warn"
        gbpd = d.get("gb_per_day")
        rationale = ("%s is filling ~%s GB/day (now %s%% full) — I recommend "
                     "alerting when it's projected to fill within %d days "
                     "(currently ~%s days out)." % (
                         mount, _reco_num(gbpd), _reco_num(d.get("pct")), thr,
                         _reco_num(eta)))
        add("disk_eta", {"days": thr}, "warning", sev, rationale,
            {"mount": mount, "eta_days": eta, "gb_per_day": gbpd,
             "pct": d.get("pct")},
            "Disk %s fills within %dd" % (mount, thr))

    # 2) VRAM fill ETA — suggest a vram_eta rule when the GPU VRAM is trending up. -
    v = signals.get("vram") or {}
    if v.get("status") == "filling" and v.get("eta_min") is not None:
        eta_days = v.get("eta_min") / 1440.0
        thr = 3 if eta_days <= 3 else int(math.ceil(eta_days))
        sev = "crit" if eta_days < 1 else "warn"
        rationale = ("GPU VRAM is climbing ~%s MB/min (now %s%% used) — I "
                     "recommend alerting when it's projected to fill within %d "
                     "day(s) (currently ~%s day(s) out)." % (
                         _reco_num(v.get("mb_per_min")), _reco_num(v.get("pct")),
                         thr, _reco_num(round(eta_days, 2))))
        add("vram_eta", {"days": thr}, "warning", sev, rationale,
            {"eta_min": v.get("eta_min"), "mb_per_min": v.get("mb_per_min"),
             "pct": v.get("pct")},
            "GPU VRAM fills within %dd" % thr)

    # 3) Recurring / active anomaly series — suggest an anomaly rule scoped to it. -
    anoms = (signals.get("anomalies") or {}).get("items") or []
    seen_series = set()
    for a in anoms:
        key = a.get("key")
        if not key or key in seen_series:
            continue
        # Only suggest a scoped anomaly rule for series the engine can target.
        valid_series = {k for k, *_ in _ANOMALY_SERIES}
        if key not in valid_series:
            continue
        seen_series.add(key)
        rationale = ("%s is anomalous right now (%s to %s%s vs ~%s%s baseline, "
                     "z=%s) — I recommend an anomaly alert scoped to %s so you're "
                     "paged when it deviates again." % (
                         key, a.get("direction"), a.get("value"), a.get("unit"),
                         a.get("baseline"), a.get("unit"), _reco_num(a.get("z")),
                         key))
        add("anomaly", {"series": key}, "warning", "warn", rationale,
            {"series": key, "z": a.get("z"), "value": a.get("value"),
             "baseline": a.get("baseline"), "direction": a.get("direction")},
            "Anomaly on %s" % key)

    # 4) Open incident — suggest an incident rule at/above the observed severity. --
    opens = [i for i in (signals.get("incidents") or []) if i.get("state") == "open"]
    if opens:
        worst = max(opens, key=lambda i: (1 if i.get("severity") == "critical" else 0,
                                          i.get("opened_at") or 0))
        want = "critical" if worst.get("severity") == "critical" else "warning"
        n = len([m for m in (worst.get("members") or []) if m.get("active")]) or \
            len(worst.get("members") or [])
        sev = "crit" if want == "critical" else "warn"
        rationale = ("A %s incident is open now (%d correlated series) — I "
                     "recommend an incident alert at/above %s severity so one "
                     "notification covers the whole correlated event." % (
                         want, n, want))
        add("incident", {"severity": want}, want, sev, rationale,
            {"severity": want, "member_count": n},
            "%s incident opens" % want.capitalize())

    # 5) Uptime family — per check: down / cert-expiry / SLO burn. ----------------
    checks = signals.get("uptime") or []
    # cert nearing expiry (any cert check currently in its warn window)
    cert_warn = [c for c in checks if c.get("type") == "cert" and c.get("enabled")
                 and c.get("state") == "up" and c.get("cert_warn")]
    if cert_warn:
        cert_warn.sort(key=lambda c: (c.get("days_to_expiry")
                                      if c.get("days_to_expiry") is not None else 1 << 30))
        c = cert_warn[0]
        label = _redact_target(str(c.get("label") or c.get("id") or "cert"))
        d = c.get("days_to_expiry")
        rationale = ("The TLS cert for '%s' expires in ~%s days — I recommend a "
                     "cert-expiry alert so you renew before it lapses." % (
                         label, _reco_num(d)))
        add("cert_expiry", {"check_id": c.get("id")}, "warning", "warn",
            rationale, {"label": label, "days_to_expiry": d},
            "Cert expiring — %s" % label)
    # SLO burn: any enabled check burning budget / over budget with enough data.
    def _slo_breaching(c):
        slo = c.get("slo") or {}
        if not c.get("enabled") or not slo.get("data_sufficient"):
            return False
        if slo.get("over_budget"):
            return True
        b1 = slo.get("burn_1h")
        return b1 is not None and b1 >= 1.0
    slo_hits = [c for c in checks if _slo_breaching(c)]
    if slo_hits:
        slo_hits.sort(key=lambda c: (c.get("slo") or {}).get("burn_1h") or 0.0,
                      reverse=True)
        c = slo_hits[0]
        label = _redact_target(str(c.get("label") or c.get("id") or "check"))
        slo = c.get("slo") or {}
        bc = slo.get("budget_consumed_pct")
        rationale = ("'%s' is burning its SLO error budget (%s%% consumed, "
                     "burn %s×/h) — I recommend a multi-window SLO-burn alert "
                     "(fast page + slow ticket) on it." % (
                         label, _reco_num(bc), _reco_num(slo.get("burn_1h"))))
        add("slo_burn",
            {"check_id": c.get("id"), "policy": "multi_window",
             "fast_burn": 14.4, "slow_burn": 6.0},
            "warning", "warn", rationale,
            {"label": label, "budget_consumed_pct": bc,
             "burn_1h": slo.get("burn_1h")},
            "SLO burn — %s" % label)

    # Rank: severity desc (crit > warn), covered-last, then rationale stable.
    _sev = {"crit": 2, "warn": 1, "info": 0}
    out.sort(key=lambda it: (
        0 if it["already_covered"] else 1,
        _sev.get(it["severity"], 0),
    ), reverse=True)
    return out


def _advisor_llm_prompt(items):
    """Small, secret-free prompt: hand the LLM the ALREADY-COMPUTED deterministic
    rationales and ask for one short sentence per item, plainer/friendlier. We
    send only the ctype + rationale text the panel already shows (targets are
    already _redact_target'd upstream). Bounded to the actionable set."""
    lines = []
    for i, it in enumerate(items):
        lines.append("%d. [%s] %s" % (i + 1, it["ctype"], it["rationale"]))
    return (
        "You are the Lab Copilot for a self-hosted homelab monitoring dashboard. "
        "Below are alert rules the monitor recommends the owner create, each with "
        "a rationale it already computed from live data. For EACH numbered item, "
        "rewrite its rationale as ONE short, friendly plain-English sentence that "
        "keeps every number and the recommended action. Use ONLY the given facts; "
        "invent nothing. Reply as a numbered list matching the input numbers, no "
        "markdown.\n\nRECOMMENDATIONS:\n" + "\n".join(lines) + "\n\nREWRITTEN:")


def _advisor_apply_llm(items, text):
    """Merge the LLM's numbered rewrite back onto the deterministic items in place,
    setting `rationale_llm` per item. Never raises; a line that can't be parsed
    just leaves that item's LLM rationale unset (deterministic prose still shows).
    The deterministic `rationale` is NEVER overwritten — the LLM prose is additive."""
    if not text:
        return
    by_num = {}
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)[.)\]]\s*(.+)", line)
        if m:
            by_num[int(m.group(1))] = m.group(2).strip()
    for i, it in enumerate(items):
        rew = by_num.get(i + 1)
        if rew:
            it["rationale_llm"] = rew[:400]


@app.route("/api/alerts/advisor", methods=["GET", "POST"])
def api_alert_advisor():
    """Proactive alert-rule recommendations from the lab's OWN live signals.

    Builds the SAME read-only signal bundle _eval_rule/preview consume, derives
    VALID candidate rule specs deterministically (disk_eta / vram_eta / anomaly /
    incident / cert_expiry / slo_burn), skips ones the user already covers, and
    returns them with plain-English rationales. Advice-only: it NEVER persists a
    rule, dispatches, or writes state — suggestions flow through the existing
    human-confirmed create path.

    The local LLM optionally ENRICHES the rationale prose, and ONLY on an explicit
    `?llm=1` (or POST {"llm":true}) call — never on any poll/GET-default path. The
    RANKING and rule specs are deterministic with or without the LLM; if it's
    off/unreachable the deterministic rationale stands (llm_status carries why).
    Always 200, graceful-degrade, read-only."""
    now = int(time.time())
    body = request.get_json(silent=True) or {} if request.method == "POST" else {}
    want_llm = (request.args.get("llm") in ("1", "true", "yes")
                or bool(body.get("llm")))
    try:
        signals = _live_signal_bundle(now)
    except Exception as e:
        print("advisor signal error:", e, flush=True)
        signals = {}
    try:
        existing = list_rules()
    except Exception as e:
        print("advisor rules error:", e, flush=True)
        existing = []
    try:
        cands = _advisor_candidates(signals, existing, now)
    except Exception as e:
        print("advisor candidates error:", e, flush=True)
        cands = []
    # Actionable set = not already covered, capped. Covered ones are dropped from
    # the returned list (we never suggest duplicates).
    recos = [c for c in cands if not c["already_covered"]][:RECO_MAX_ITEMS]
    out = {"ok": True, "now": now, "recommendations": recos,
           "count": len(recos), "model": COPILOT_MODEL,
           "enabled": COPILOT_ENABLED, "llm_used": False, "llm_status": "skipped"}
    # LLM enrichment ONLY on the explicit action — never on the default GET poll.
    if want_llm and recos:
        text, err = _ollama_generate(_advisor_llm_prompt(recos),
                                     timeout=min(COPILOT_TIMEOUT, 20))
        if text is not None:
            _advisor_apply_llm(recos, text)
            out["llm_used"] = True
            out["llm_status"] = "ok"
        else:
            out["llm_status"] = err
    return jsonify(out)


@app.route("/api/alerts/rules", methods=["GET", "POST"])
def api_alert_rules():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        rid, err = create_rule(body)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        return jsonify({"ok": True, "id": rid}), 201
    m_active, m_end = _in_maintenance()
    return jsonify({"rules": list_rules(), "channels": _configured_channels(get_settings()),
                    "types": sorted(_RULE_TYPES),
                    "series": ["any"] + [k for k, *_ in _ANOMALY_SERIES],
                    "maintenance_active": m_active, "maintenance_until": m_end})

@app.route("/api/alerts/rules/<rid>", methods=["PATCH", "DELETE"])
def api_alert_rule_one(rid):
    if request.method == "DELETE":
        ok = delete_rule(rid)
        return jsonify({"ok": ok}), (200 if ok else 404)
    body = request.get_json(silent=True) or {}
    ok, err = update_rule(rid, body)
    if not ok:
        code = 404 if err == "not found" else 400
        return jsonify({"ok": False, "error": err}), code
    return jsonify({"ok": True})

@app.route("/api/alerts/maintenance", methods=["GET", "POST"])
def api_maintenance():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        mid, err = create_maintenance(body)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        return jsonify({"ok": True, "id": mid}), 201
    return jsonify({"windows": list_maintenance(), **_maint_status()})

# Marker label for click-created quick-mute one-offs (so "Unmute now" can find them).
_QUICKMUTE_LABEL = "Quick mute"
_QUICKMUTE_MAX_SECONDS = 24 * 3600

@app.route("/api/alerts/maintenance/status")
def api_maintenance_status():
    """Glanceable status for the UI banner (active / next-upcoming). Read-only."""
    return jsonify(_maint_status())

@app.route("/api/alerts/maintenance/quickmute", methods=["POST"])
def api_maintenance_quickmute():
    """Explicit one-click silence: create a one-off window now→now+duration.
    Only mutes OUTBOUND alerts (same contract as any window); off until clicked."""
    body = request.get_json(silent=True) or {}
    secs = body.get("seconds")
    if secs is None and body.get("minutes") is not None:
        try:
            secs = int(float(body.get("minutes")) * 60)
        except (TypeError, ValueError):
            secs = None
    if secs is None:
        secs = 3600
    try:
        secs = int(secs)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid duration."}), 400
    if secs <= 0:
        return jsonify({"ok": False, "error": "Duration must be positive."}), 400
    secs = min(secs, _QUICKMUTE_MAX_SECONDS)  # clamp to ≤24h
    now = int(time.time())
    mins = max(1, round(secs / 60))
    label = f"{_QUICKMUTE_LABEL} ({mins}m)" if mins != 60 else f"{_QUICKMUTE_LABEL} (1h)"
    mid, err = create_maintenance({"label": label, "recurring": False,
                                   "start_ts": now, "end_ts": now + secs})
    if err:
        return jsonify({"ok": False, "error": err}), 400
    win = next((w for w in list_maintenance() if w["id"] == mid), None)
    return jsonify({"ok": True, "id": mid, "window": win, **_maint_status()}), 201

@app.route("/api/alerts/maintenance/unmute", methods=["POST"])
def api_maintenance_unmute():
    """Cancel the active quick-mute(s): delete any currently-active one-off window
    whose label marks it as a quick-mute. Explicit action; touches nothing else."""
    now = int(time.time())
    removed = []
    for w in list_maintenance():
        if (not w["recurring"] and (w["label"] or "").startswith(_QUICKMUTE_LABEL)
                and _window_active(w, now)[0]):
            if delete_maintenance(w["id"]):
                removed.append(w["id"])
    return jsonify({"ok": True, "removed": removed, **_maint_status()})

@app.route("/api/alerts/maintenance/<mid>", methods=["PATCH", "DELETE"])
def api_maintenance_one(mid):
    if request.method == "DELETE":
        ok = delete_maintenance(mid)
        return jsonify({"ok": ok}), (200 if ok else 404)
    body = request.get_json(silent=True) or {}
    ok, err = update_maintenance(mid, body)
    if not ok:
        code = 404 if err == "not found" else 400
        return jsonify({"ok": False, "error": err}), code
    return jsonify({"ok": True})

@app.route("/api/alerts/routes", methods=["GET", "POST"])
def api_alert_routes():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        rid, err = create_route(body)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        return jsonify({"ok": True, "id": rid}), 201
    return jsonify({"routes": list_routes(),
                    "channels": _configured_channels(get_settings())})

@app.route("/api/alerts/routes/<rid>", methods=["PATCH", "DELETE"])
def api_alert_route_one(rid):
    if request.method == "DELETE":
        ok = delete_route(rid)
        return jsonify({"ok": ok}), (200 if ok else 404)
    body = request.get_json(silent=True) or {}
    ok, err = update_route(rid, body)
    if not ok:
        code = 404 if err == "not found" else 400
        return jsonify({"ok": False, "error": err}), code
    return jsonify({"ok": True})

@app.route("/api/alerts/history")
def api_alert_history():
    try: limit = min(200, max(1, int(request.args.get("limit", 100))))
    except (TypeError, ValueError): limit = 100
    return jsonify({"history": list_alert_history(limit)})

@app.route("/api/alerts/history/<int:hid>/ack", methods=["POST"])
def api_alert_ack(hid):
    ok = ack_alert(hid)
    return jsonify({"ok": ok}), (200 if ok else 404)

# ── External uptime checks API (private; never exposed on /status) ────────────
@app.route("/api/uptime", methods=["GET", "POST"])
def api_uptime():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        cid, err = create_uptime_check(body)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        return jsonify({"ok": True, "id": cid}), 201
    try:
        window = min(2592000, max(3600, int(request.args.get("window", 86400))))
    except (TypeError, ValueError):
        window = 86400
    return jsonify(uptime_overview(window))

@app.route("/api/uptime/<cid>", methods=["PATCH", "DELETE"])
def api_uptime_one(cid):
    if request.method == "DELETE":
        ok = delete_uptime_check(cid)
        return jsonify({"ok": ok}), (200 if ok else 404)
    body = request.get_json(silent=True) or {}
    ok, err = update_uptime_check(cid, body)
    if not ok:
        code = 404 if err == "not found" else 400
        return jsonify({"ok": False, "error": err}), code
    return jsonify({"ok": True})

@app.route("/api/update/app", methods=["POST"])
def api_update_app():
    """Start the opt-in one-click self-update (detached docker:cli helper).
    400 = disabled / no update / not a compose deploy; 409 = already running;
    202 = job started. Gated by ALLOW_SELF_UPDATE — off by default."""
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force"))
    code, payload = start_self_update(force=force)
    return jsonify(payload), code

@app.route("/api/update/app/status")
def api_update_app_status():
    """Read back the self-update progress files from the data dir. Works even
    right after the restart — it just reads update_state.json + a tail of
    update.log. No state file yet → idle."""
    st = _read_update_state()
    if not st:
        return jsonify({"state": "idle"})
    st = dict(st)
    st["log"] = _tail_lines(_update_log_path(), 200)
    st["self_update_enabled"] = ALLOW_SELF_UPDATE
    return jsonify(st)

@app.route("/")
def index():
    return app.send_static_file("dashboard.html")

_seed_demo_data()    # no-op unless DEMO_MODE is on and the DB is fresh
# Background loops write live samples into the shared DB. Under pytest this races
# with tests that aggregate the recent window (e.g. the cost heatmap), injecting a
# phantom "now" cell between a test's wipe and its request. Skip them when the test
# runner is in control; production (python app.py / gunicorn) never imports pytest.
if "pytest" not in sys.modules:
    # Hydrate the notifier edge-state from SQLite BEFORE the collector's first
    # notify_scan(), so an already-fired-and-still-true built-in alert (crashed
    # container, full disk, VRAM pressure) does not re-fire a duplicate on restart.
    restore_notified_state()
    threading.Thread(target=collector, daemon=True).start()
    threading.Thread(target=host_poller, daemon=True).start()
    threading.Thread(target=uptime_worker, daemon=True).start()
    threading.Thread(target=mqtt_worker, daemon=True).start()
    threading.Thread(target=image_update_worker, daemon=True).start()

if __name__ == "__main__":
    print(
        f"\n  HomeLab Monitor v{VERSION}\n"
        f"      Open  ->  http://localhost:{PORT}   (or http://<this-host-ip>:{PORT} over your LAN/VPN)\n"
        f"      Like it? A star on GitHub helps other home-labbers find it:\n"
        f"      https://github.com/SikamikanikoBG/homelab-monitor\n",
        flush=True,
    )
    app.run(host="0.0.0.0", port=PORT, threaded=True)
