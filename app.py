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
import os, re, glob, time, json, socket, sqlite3, threading, subprocess, http.client
from flask import Flask, request, jsonify, Response
try:
    from prometheus_client import (Gauge, generate_latest, CONTENT_TYPE_LATEST,
                                   REGISTRY, CollectorRegistry)
    _PROM_OK = True
except ImportError:
    _PROM_OK = False

VERSION      = "0.3.1"
DB_PATH      = os.environ.get("DB_PATH", "/data/gpu.db")
INTERVAL     = int(os.environ.get("SAMPLE_INTERVAL", "10"))
RETENTION    = int(os.environ.get("RETENTION_DAYS", "180")) * 86400
DOCKER_SOCK  = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
HOST_ROOT    = os.environ.get("HOST_ROOT", "/rootfs")          # host / mounted read-only (optional)
PORT         = int(os.environ.get("PORT", "9800"))
PRESSURE_MB  = int(os.environ.get("PRESSURE_FREE_MB", "2048"))
MAX_POINTS   = 360
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
HEALTH = {"docker": None, "systemd": None, "at": 0}
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

def collect_docker():
    try:
        raw = json.loads(_docker("/containers/json?all=1"))
    except Exception as e:
        return {"available": False, "reason": f"Docker API unreachable: {e}",
                "containers": [], "summary": {"total": 0, "running": 0, "problems": 0}}
    items = []
    for ct in raw:
        level, label = _docker_status(ct.get("State"), ct.get("Status"))
        items.append({"name": (ct.get("Names") or ["/?"])[0].lstrip("/"),
                      "image": ct.get("Image", ""), "state": (ct.get("State") or "").lower(),
                      "status_text": ct.get("Status", ""), "status": level, "label": label})
    rank = {"crit": 0, "warn": 1, "ok": 2, "info": 3}
    items.sort(key=lambda c: (rank.get(c["status"], 9), c["name"].lower()))
    return {"available": True, "containers": items,
            "summary": {"total": len(items),
                        "running": sum(1 for c in items if c["state"] == "running"),
                        "problems": sum(1 for c in items if c["status"] in ("crit", "warn"))}}

def _svc_status(active):
    return {"failed": "crit", "active": "ok",
            "activating": "warn", "deactivating": "warn"}.get(active, "info")

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
    except Exception as e:
        return {"available": False, "reason": f"systemd query failed: {e}", "services": [], "summary": {}}
    finally:
        conn.close()

    admin = {os.path.basename(p) for p, _state in files
             if p.startswith(SYSTEMD_ADMIN_DIR) and p.endswith(".service")}
    services, running, failed = [], 0, 0
    for name, desc, _load, active, sub, *_ in units:
        if not name.endswith(".service"):
            continue
        running += active == "active"
        failed  += active == "failed"
        services.append({"name": name, "desc": desc, "active": active, "sub": sub,
                         "admin": name in admin,
                         "watched": name in WATCH_SERVICES or name[:-8] in WATCH_SERVICES,
                         "status": _svc_status(active)})
    # Default view = the units you actually care about: ones you deployed, ones
    # you asked to watch, and anything currently failing.
    shown = [s for s in services if s["admin"] or s["watched"] or s["status"] == "crit"]
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

def health_scan():
    HEALTH["docker"]  = collect_docker()
    HEALTH["systemd"] = collect_systemd()
    HEALTH["at"]      = int(time.time())

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
    last_oom = last_health = 0
    while True:
        try:
            sample_once()
            now = time.time()
            if now - last_oom > 60:
                oom_scan(); last_oom = now
            if now - last_health > 15:
                health_scan(); last_health = now
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
    return jsonify({"version": VERSION, "updated": HEALTH["at"], "now": now,
                    "docker": docker, "systemd": systemd,
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


@app.route("/")
def index():
    return app.send_static_file("dashboard.html")

threading.Thread(target=collector, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
