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
import os, re, glob, time, json, socket, sqlite3, threading, subprocess, http.client, urllib.parse, urllib.request, ipaddress, shlex
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, Response
try:
    from prometheus_client import (Gauge, generate_latest, CONTENT_TYPE_LATEST,
                                   REGISTRY, CollectorRegistry)
    _PROM_OK = True
except ImportError:
    _PROM_OK = False

VERSION      = "0.9.0"
DB_PATH      = os.environ.get("DB_PATH", "/data/gpu.db")
INTERVAL     = int(os.environ.get("SAMPLE_INTERVAL", "10"))
RETENTION    = int(os.environ.get("RETENTION_DAYS", "180")) * 86400
DOCKER_SOCK  = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
HOST_ROOT    = os.environ.get("HOST_ROOT", "/rootfs")          # host / mounted read-only (optional)
PORT         = int(os.environ.get("PORT", "9800"))
PRESSURE_MB  = int(os.environ.get("PRESSURE_FREE_MB", "2048"))
CHECK_UPDATES = os.environ.get("CHECK_UPDATES", "true").strip().lower() not in ("0", "false", "no", "off")
UPDATE_REPO   = os.environ.get("UPDATE_REPO", "SikamikanikoBG/homelab-monitor")
# Split cache: once we know there's an update, the answer won't change for hours
# so we can cache it long. But "no update found" / network errors should expire
# sooner — otherwise a release published right after deploy stays invisible for
# the full 6h, and a transient GitHub blip sticks for the same window.
UPDATE_TTL_POSITIVE = 6 * 3600
UPDATE_TTL_NEGATIVE = 30 * 60
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
DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.execute("PRAGMA journal_mode=WAL")
DB.executescript("""
CREATE TABLE IF NOT EXISTS samples(ts INTEGER PRIMARY KEY, util REAL, mem_used REAL, mem_total REAL, power REAL, temp REAL);
CREATE TABLE IF NOT EXISTS proc(ts INTEGER, service TEXT, mem REAL);
CREATE TABLE IF NOT EXISTS models(ts INTEGER, service TEXT, model TEXT, vram REAL);
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
CREATE INDEX IF NOT EXISTS idx_proc_ts   ON proc(ts);
CREATE INDEX IF NOT EXISTS idx_models_ts ON models(ts);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_event ON events(ts, service, kind);
""")
for col in ("cpu REAL", "ram_used REAL", "ram_total REAL", "load1 REAL", "ctemp REAL"):
    try:
        DB.execute(f"ALTER TABLE samples ADD COLUMN {col}")
    except sqlite3.OperationalError:
        pass
DB.commit()

LATEST = {"ts": 0, "util": 0, "mem_used": 0, "mem_total": 24576, "power": 0, "temp": 0,
          "procs": [], "models": [], "host": {}}
# Current state of the "status" monitors (Docker + systemd). The background
# collector refreshes these; /api/health just serves the cached snapshot.
HEALTH = {"docker": None, "systemd": None, "update": None, "at": 0}
WATCH_SERVICES = [s.strip() for s in os.environ.get("WATCH_SERVICES", "").split(",") if s.strip()]
SYSTEMD_ADMIN_DIR = "/etc/systemd/system/"   # units here are admin/user-authored (vs vendor)
_ct_cache = {"list": [], "at": 0}
_scan_since = {}
_cpu_prev = {"idle": 0, "total": 0}

# ── Docker API over the unix socket ────────────────────────────────────────────
def _docker(path):
    c = http.client.HTTPConnection("localhost", timeout=4)
    c.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.sock.settimeout(4); c.sock.connect(DOCKER_SOCK)
    c.request("GET", path); data = c.getresponse().read(); c.close()
    return data

def containers():
    if time.time() - _ct_cache["at"] < 30 and _ct_cache["list"]:
        return _ct_cache["list"]
    out = []
    try:
        for ct in json.loads(_docker("/containers/json")):
            nets = (ct.get("NetworkSettings") or {}).get("Networks") or {}
            ip = next((n.get("IPAddress") for n in nets.values() if n.get("IPAddress")), None)
            out.append({"id": ct["Id"][:12], "name": (ct.get("Names") or ["/?"])[0].lstrip("/"),
                        "image": ct.get("Image", ""), "ip": ip})
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

# ── Model-server probes (agnostic: append to PROBES to support a new server) ───
def _http_json(ip, port, path, timeout=2):
    c = http.client.HTTPConnection(ip, port, timeout=timeout)
    c.request("GET", path); r = c.getresponse(); body = r.read(); c.close()
    return json.loads(body) if r.status < 400 else None

def probe_ollama(ip):
    d = _http_json(ip, 11434, "/api/ps")
    return [(m["name"], (m.get("size_vram") or 0) / 1048576 or None) for m in (d or {}).get("models", [])]
def probe_vllm(ip):
    d = _http_json(ip, 8000, "/v1/models");  return [(m["id"], None) for m in (d or {}).get("data", [])]
def probe_tgi(ip):
    d = _http_json(ip, 80, "/info") or _http_json(ip, 3000, "/info")
    return [(d["model_id"], None)] if d and d.get("model_id") else []
def probe_llamacpp(ip):
    d = _http_json(ip, 8080, "/v1/models"); return [(m["id"], None) for m in (d or {}).get("data", [])]
def probe_a1111(ip):
    d = _http_json(ip, 7860, "/sdapi/v1/options"); m = (d or {}).get("sd_model_checkpoint")
    return [(m, None)] if m else []
def probe_comfy(ip):
    return [("ComfyUI graph", None)] if _http_json(ip, 8188, "/system_stats") is not None else []

PROBES = [("ollama", probe_ollama), ("vllm", probe_vllm),
          ("text-generation-inference", probe_tgi), ("tgi", probe_tgi),
          ("llama.cpp", probe_llamacpp), ("llamacpp", probe_llamacpp), ("ggml", probe_llamacpp),
          ("automatic1111", probe_a1111), ("stable-diffusion-webui", probe_a1111), ("sd-webui", probe_a1111),
          ("comfyui", probe_comfy)]

def probe_models(ct):
    img, name, ip = ct.get("image", "").lower(), ct.get("name", "").lower(), ct.get("ip")
    if not ip:
        return []
    for key, fn in PROBES:
        if key in img or key in name:
            try:
                return [(m, v) for m, v in fn(ip) if m]
            except Exception:
                return []
    return []

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
        if not dev.startswith("/dev/") or fs not in REAL_FS or dev in seen:
            continue
        seen.add(dev)
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
    except Exception:
        h["ram_total"] = h["ram_used"] = 0
    try: h["load1"] = float(open("/proc/loadavg").read().split()[0])
    except Exception: h["load1"] = 0
    try: h["uptime"] = int(float(open("/proc/uptime").read().split()[0]))
    except Exception: h["uptime"] = 0
    t = 0
    for f in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try: t = max(t, int(open(f).read().strip()) / 1000)
        except Exception: pass
    h["ctemp"] = round(t, 1)
    h["disks"] = read_disks()
    return h

# ── Health monitors: Docker containers + systemd services ──────────────────────
# These describe *current state* (is it up? healthy? failed?) rather than a time
# series, so they live behind /api/health and are refreshed by health_scan().
_DOCKER_HEALTH = re.compile(r"\((healthy|unhealthy|health: starting)\)")
# "Up 14 days, 2 hours" / "Up About a minute" / "Up 12 seconds (healthy)" — Docker
# already builds a human Status string, so we parse it instead of issuing N more
# inspect calls just for StartedAt.
_DOCKER_UP_DUR = re.compile(r"^Up\s+(.+?)(?:\s*\(.*\))?$", re.I)
_DUR_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800, "month": 2592000, "year": 31536000}
# Heavy enrichment (per-container `stats` + Docker layer-size scan) is too costly
# to hit on every 10 s health_scan, so we keep a separate 30 s cache for it.
_DOCKER_ENRICH_TTL = 30
_docker_enrich = {"data": {}, "at": 0}

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
    """One-shot memory snapshot for a running container. Returns bytes or None."""
    try:
        d = json.loads(_docker(f"/containers/{cid}/stats?stream=false&one-shot=true"))
    except Exception:
        return None
    mem = (d.get("memory_stats") or {}).get("usage")
    # Docker counts page cache against `usage`; subtract it like `docker stats` does.
    cache = ((d.get("memory_stats") or {}).get("stats") or {}).get("cache", 0)
    return max(0, (mem or 0) - cache) if mem is not None else None

def _refresh_docker_enrich(running_ids):
    """Pull SizeRw (one Docker call with size=1) + memory (per-container, in
    parallel). Cached for _DOCKER_ENRICH_TTL seconds — heavy enough that we
    don't want it on the 10 s health-scan path."""
    out = {}
    try:
        sized = json.loads(_docker("/containers/json?all=1&size=1"))
        for ct in sized:
            out[ct["Id"][:12]] = {"disk_bytes": ct.get("SizeRw") or 0}
    except Exception:
        pass
    if running_ids:
        with ThreadPoolExecutor(max_workers=min(8, len(running_ids))) as ex:
            for cid, mem in zip(running_ids, ex.map(_container_stats, running_ids)):
                out.setdefault(cid, {})["mem_bytes"] = mem
    return out

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
    for c in items:
        e = _docker_enrich["data"].get(c["id"]) or {}
        c["mem_bytes"]  = e.get("mem_bytes")
        c["disk_bytes"] = e.get("disk_bytes")
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

def health_scan():
    HEALTH["docker"]  = collect_docker()
    HEALTH["systemd"] = collect_systemd()
    HEALTH["update"]  = collect_update()
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
    return g["user"], g["host"], port

def list_hosts():
    with LOCK:
        rows = DB.execute("SELECT name, ssh_target, tags, added_at, last_check_at, last_check_json "
                          "FROM hosts ORDER BY added_at").fetchall()
    out = []
    for name, target, tags, added, checked, blob in rows:
        try:
            check = json.loads(blob) if blob else None
        except Exception:
            check = None
        out.append({"name": name, "ssh_target": target,
                    "tags": [t for t in (tags or "").split(",") if t],
                    "added_at": added, "last_check_at": checked, "last_check": check})
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

HOST_DATA = {}          # name -> {"data": {...}, "at": int, "error": str?}
HOST_DATA_LOCK = threading.Lock()
HOST_POLL_TIMEOUT = 15

def probe_host_metrics(user, host, port):
    """Run probe.py on the remote via SSH stdin, return (data, error)."""
    if not _PROBE_SCRIPT:
        return None, "probe.py not packaged in this image"
    rc, out, err, _ = _ssh_with_stdin(user, host, port, "python3 -",
                                      _PROBE_SCRIPT, timeout=HOST_POLL_TIMEOUT)
    if rc != 0:
        return None, _clean_ssh_err(err, out, rc)
    out = (out or "").strip()
    if not out:
        return None, "empty response from probe"
    try:
        return json.loads(out), None
    except Exception as e:
        return None, f"bad JSON from probe: {e}"

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
        "load1":     H.get("load1"),
        "uptime":    H.get("uptime"),
        "ctemp":     H.get("ctemp"),
        "disks":     H.get("disks") or [],
        "hostname":  socket.gethostname(),
    }
    # GPU summary — the existing collector already keeps these on LATEST's top
    # level. Re-use them so the All-hosts row for `local` matches the shape
    # probe.py emits for remotes.
    gpu_total = (LATEST or {}).get("mem_total") or 0
    if gpu_total > 0:
        out["gpu"] = {
            "mem_used":  (LATEST or {}).get("mem_used", 0),
            "mem_total": gpu_total,
            "util":      (LATEST or {}).get("util", 0),
            "temp":      (LATEST or {}).get("temp", 0),
        }
    return out

def host_poller():
    """Loop: probe every registered host whose last Test was healthy. Per-host
    timeouts isolate slow remotes so the loop never stalls. Errors are kept on
    the cache row so the UI can show a 'last error' instead of just going dark."""
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
                data, err = probe_host_metrics(u, host, port)
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
            "ssh", *_SSH_BASE_ARGS, "-p", str(port), f"{user}@{host}", cmd,
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
            "ssh", *_SSH_BASE_ARGS, "-p", str(port), f"{user}@{host}", cmd,
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

def _detect_os(user, host, port):
    """Run a tiny discovery script via SSH. Returns a normalized dict. We
    ignore rc here — we only care about whatever lines did land on stdout."""
    _, out, _, _ = _ssh(user, host, port, _OS_DETECT_SCRIPT, timeout=10)
    info = {}
    for line in (out or "").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            info[k.strip()] = v.strip().strip('"').strip("'")
    # Family normalization for branching remedies
    osid    = (info.get("ID") or "").lower()
    id_like = (info.get("ID_LIKE") or "").lower()
    uname   = (info.get("UNAME") or "").lower()
    if uname == "darwin":
        family = "macos"
    elif osid == "alpine":
        family = "alpine"
    elif osid in ("opensuse-leap", "opensuse-tumbleweed", "sles", "sled") or "suse" in id_like:
        family = "suse"
    elif osid in ("debian", "ubuntu", "raspbian", "pop", "linuxmint") or "debian" in id_like:
        family = "debian"
    elif osid in ("fedora", "rhel", "centos", "rocky", "almalinux") or "rhel" in id_like or "fedora" in id_like:
        family = "rhel"
    elif osid in ("arch", "manjaro", "endeavouros") or "arch" in id_like:
        family = "arch"
    elif uname in ("linux", ""):
        family = "linux"     # generic Linux, init unknown
    else:
        family = uname or "unknown"
    info["family"] = family
    # Pretty short label for the UI badge
    pretty = info.get("PRETTY_NAME") or info.get("ID") or info.get("UNAME") or "unknown"
    init   = info.get("INIT")
    info["label"] = f"{pretty}" + (f" · {init}" if init else "")
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
    return {"where": "on the remote",
            "cmd": f"mkdir -p ~/.ssh && chmod 700 ~/.ssh\n"
                   f"echo '{get_hub_pubkey()}' >> ~/.ssh/authorized_keys\n"
                   f"chmod 600 ~/.ssh/authorized_keys"}

def _remedy_sshd_down(os_info):
    fam = (os_info or {}).get("family") or "linux"
    if fam == "alpine":
        return {"where": "on the remote",
                "cmd": "# Check that sshd is running:\nsudo rc-service sshd status"}
    return {"where": "on the remote",
            "cmd": "# Check that sshd is running and the port is reachable:\nsudo systemctl status sshd"}

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
                "remedy": {"where": "on the remote",
                           "cmd": "# sshd may not be running, or the port is firewalled.\n"
                                  "# On the remote:\nsudo systemctl status sshd\n"
                                  "# Or check the firewall:\nsudo firewall-cmd --list-ports  # firewalld\nsudo ufw status                  # ufw"}}
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
    "alert_min_level":     "warning",  # "warning" or "critical"
    "disk_alert_pct":      "90",       # disk usage % that trips an alert
}
SETTING_SECRETS = {"discord_webhook_url"}   # never round-tripped to the UI in full

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

def save_settings(updates):
    """Persist any subset of known setting keys. Unknown keys are ignored."""
    safe = [(k, "" if v is None else str(v)) for k, v in updates.items() if k in SETTING_DEFAULTS]
    if not safe:
        return
    with LOCK:
        DB.executemany("INSERT INTO settings(key,value) VALUES(?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", safe)
        DB.commit()

# ── Notifier: Discord webhook + ntfy.sh ──────────────────────────────────────
# Edge-triggered: each alert key is remembered in _NOTIFIED so a flapping state
# doesn't spam the channel. A key clears when the underlying condition recovers
# (container becomes healthy again, disk drops below threshold, etc.), so the
# next failure re-fires exactly once.
_NOTIFIED = {}            # key -> 1, "armed" alerts pending recovery
_NOTIFIER_LOCK = threading.Lock()
LEVELS  = {"info": 0, "warning": 1, "critical": 2}
_COLORS = {"info": 0x58A6FF, "warning": 0xD29922, "critical": 0xF85149}
_NTFY_P = {"info": 3, "warning": 4, "critical": 5}
_NTFY_T = {"info": "information_source", "warning": "warning", "critical": "rotating_light"}

def _post_json(url, payload, timeout=5):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()

def _post_text(url, text, headers=None, timeout=5):
    req = urllib.request.Request(url, data=text.encode("utf-8"),
                                 headers=headers or {"Content-Type": "text/plain"})
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

def dispatch_alert(s, level, title, detail):
    """Send to whichever channels are configured. Returns list of (channel, ok, err)."""
    out = []
    if s.get("discord_webhook_url"):
        try: send_discord(s["discord_webhook_url"], level, title, detail); out.append(("discord", True, None))
        except Exception as e: out.append(("discord", False, str(e)))
    if s.get("ntfy_topic"):
        try: send_ntfy(s.get("ntfy_server") or "https://ntfy.sh",
                       s["ntfy_topic"], level, title, detail); out.append(("ntfy", True, None))
        except Exception as e: out.append(("ntfy", False, str(e)))
    return out

def _emit(s, key, level, title, detail):
    """Fire an alert once per edge. Skips below the configured min level."""
    if LEVELS.get(level, 0) < LEVELS.get(s.get("alert_min_level", "warning"), 1):
        return
    with _NOTIFIER_LOCK:
        if _NOTIFIED.get(key):
            return
        _NOTIFIED[key] = 1
    for ch, ok, err in dispatch_alert(s, level, title, detail):
        if not ok:
            print(f"notifier {ch} error:", err, flush=True)

def _clear(key):
    with _NOTIFIER_LOCK:
        _NOTIFIED.pop(key, None)

def notify_scan():
    s = get_settings()
    if s.get("alerts_enabled") != "1":
        return
    if not (s.get("discord_webhook_url") or s.get("ntfy_topic")):
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
            if already:
                continue
            if LEVELS["critical"] < LEVELS.get(s.get("alert_min_level", "warning"), 1):
                continue
            for ch, ok, err in dispatch_alert(
                    s, "critical", f"🔴 GPU OOM in {svc}", (detail or "")[:1500]):
                if not ok: print(f"notifier {ch} error:", err, flush=True)
    except Exception as e:
        print("notify_scan oom error:", e, flush=True)

# ── Sampling ────────────────────────────────────────────────────────────────
def smi(args):
    return subprocess.run(["nvidia-smi", *args], capture_output=True, text=True, timeout=15).stdout.strip()

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

def sample_once():
    g = smi(["--query-gpu=utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu",
             "--format=csv,noheader,nounits"]).splitlines()[0].split(",")
    util, mem_used, mem_total, power, temp = (float(x.strip() or 0) for x in g)
    nm = {c["id"]: c["name"] for c in containers()}
    procs = {}
    for line in smi(["--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"]).splitlines():
        if line.strip():
            pid, mem = (p.strip() for p in line.split(","))
            svc = service_for_pid(pid, nm)
            procs[svc] = procs.get(svc, 0) + float(mem or 0)

    by_name = {c["name"]: c for c in containers()}
    models = []
    for svc, mem in procs.items():
        found = probe_models(by_name.get(svc, {}))
        if len(found) == 1 and found[0][1] is None:
            models.append((svc, found[0][0], round(mem)))
        else:
            for mdl, vram in found:
                models.append((svc, mdl, round(vram) if vram else None))

    host = read_host()
    ts = int(time.time())
    with LOCK:
        DB.execute("INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp,cpu,ram_used,ram_total,load1,ctemp)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (ts, util, mem_used, mem_total, power, temp, host["cpu"], host["ram_used"],
                    host["ram_total"], host["load1"], host["ctemp"]))
        for svc, mem in procs.items():
            DB.execute("INSERT INTO proc VALUES(?,?,?)", (ts, svc, mem))
        for svc, mdl, vram in models:
            DB.execute("INSERT INTO models VALUES(?,?,?,?)", (ts, svc, mdl, vram))
        if ts % 360 < INTERVAL:
            for t in ("samples", "proc", "models", "events"):
                DB.execute(f"DELETE FROM {t} WHERE ts<?", (ts - RETENTION,))
        DB.commit()
    LATEST.update(ts=ts, util=util, mem_used=mem_used, mem_total=mem_total, power=power, temp=temp,
                  procs=sorted(({"service": s, "mem": round(m)} for s, m in procs.items()), key=lambda x: -x["mem"]),
                  models=[{"service": s, "model": m, "vram": v} for s, m, v in models], host=host)

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
                    "events": evs, "insights": insights, "pressure_free_mb": PRESSURE_MB,
                    "mem_total": mem_total, "peak_mem": peak, "now": LATEST})

@app.route("/healthz")
def healthz():
    """Cheap liveness probe for Docker's HEALTHCHECK and any uptime monitor.
    No DB, no locks — just a 200 with the running version so the answer is
    instant and never gets blocked behind a slow collector pass."""
    return jsonify({"status": "ok", "version": VERSION}), 200

@app.route("/favicon.ico")
def favicon():
    """Default-favicon URL — browsers ask for /favicon.ico even when an explicit
    <link rel="icon"> points elsewhere (during early page load, or for tabs that
    open without rendering HTML). Serve the SVG we ship in static/."""
    return app.send_static_file("favicon.svg")

@app.route("/api/health")
def api_health():
    """Current state of the status monitors (Docker + systemd) plus a light GPU/host
    snapshot. Cheap and DB-free, so the dashboard can poll it often."""
    now = {"gpu": {"util": LATEST["util"], "mem_used": LATEST["mem_used"],
                   "mem_total": LATEST["mem_total"] or 24576, "power": LATEST["power"],
                   "temp": LATEST["temp"]},
           "host": LATEST["host"]}
    docker  = HEALTH["docker"]  or {"available": False, "reason": "warming up…",
                                    "containers": [], "summary": {"total": 0, "running": 0, "problems": 0}}
    systemd = HEALTH["systemd"] or {"available": False, "reason": "warming up…",
                                    "services": [], "summary": {}}
    update  = HEALTH["update"]  or {"available": False, "current": VERSION}
    return jsonify({"version": VERSION, "updated": HEALTH["at"], "now": now,
                    "docker": docker, "systemd": systemd, "update": update,
                    "overview": build_overview(now, docker, systemd)})

@app.route("/metrics")
def metrics():
    """Prometheus text-format scrape endpoint.

    Reads exclusively from the in-memory snapshots (LATEST / HEALTH) that the
    background collector keeps fresh.  No new I/O is triggered on each scrape,
    so double-sampling is impossible.
    """
    if not _PROM_OK:
        return Response("# prometheus_client not installed\n", mimetype="text/plain", status=503)

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

    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


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
        return jsonify({"name": "local", "host": _local_now_snapshot(),
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
    return jsonify({"name": name, "host": entry["data"].get("host", {}),
                    "at": entry["at"], "online": age < INTERVAL * 3,
                    "stale_for": age if age >= INTERVAL * 3 else 0,
                    "error": entry.get("error")})

@app.route("/api/fleet")
def api_fleet():
    """Compact summary KPIs for every host in the fleet. Drives the All-hosts
    table. Order: local first, then registered hosts in the order they were
    added."""
    hosts = list_hosts()
    rows  = []

    # Local row
    rows.append({"name": "local", "label": socket.gethostname() + " (this hub)",
                 "ssh_target": None, "host": _local_now_snapshot(),
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
                "host": data.get("host") if data else None,
                "at": at,
                "online": online,
                "is_local": False,
                "last_check": h.get("last_check"),
                "error": entry.get("error"),
            })
    return jsonify({"hosts": rows, "interval": INTERVAL})

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

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        # Empty string clears a setting; missing key leaves it unchanged.
        # Secrets pass through the "_set: false" sentinel from the UI as a way
        # to clear without revealing the current value.
        updates = {k: body[k] for k in body if k in SETTING_DEFAULTS}
        save_settings(updates)
    return jsonify({"version": VERSION, "settings": _public_settings()})

@app.route("/api/notify/test", methods=["POST"])
def api_notify_test():
    """Send a one-shot test alert using the currently saved settings."""
    s = get_settings()
    if not (s.get("discord_webhook_url") or s.get("ntfy_topic")):
        return jsonify({"ok": False, "results": [],
                        "reason": "No Discord webhook or ntfy topic configured."}), 400
    results = dispatch_alert(s, "info",
                             "✅ HomeLab Monitor — test alert",
                             "If you see this, alerts are wired up correctly.")
    return jsonify({"ok": all(ok for _, ok, _ in results),
                    "results": [{"channel": c, "ok": ok, "error": err} for c, ok, err in results]})

@app.route("/")
def index():
    return app.send_static_file("dashboard.html")

threading.Thread(target=collector, daemon=True).start()
threading.Thread(target=host_poller, daemon=True).start()

if __name__ == "__main__":
    print(
        f"\n  HomeLab Monitor v{VERSION}\n"
        f"      Open  ->  http://localhost:{PORT}   (or http://<this-host-ip>:{PORT} over your LAN/VPN)\n"
        f"      Like it? A star on GitHub helps other home-labbers find it:\n"
        f"      https://github.com/SikamikanikoBG/homelab-monitor\n",
        flush=True,
    )
    app.run(host="0.0.0.0", port=PORT, threaded=True)
