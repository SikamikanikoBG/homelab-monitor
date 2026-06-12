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
import os, re, glob, time, json, socket, sqlite3, threading, subprocess, http.client, urllib.parse, urllib.request, ipaddress, shlex, struct, shutil, tempfile
try:
    import fcntl                       # Linux-only; used for per-iface IPv4 (SIOCGIFADDR)
except ImportError:
    fcntl = None
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, Response, send_file, after_this_request
import db_backup
try:
    from prometheus_client import (Gauge, generate_latest, CONTENT_TYPE_LATEST,
                                   REGISTRY, CollectorRegistry)
    _PROM_OK = True
except ImportError:
    _PROM_OK = False

VERSION      = "0.14.4"
DB_PATH      = os.environ.get("DB_PATH", "/data/gpu.db")
MCP_IDLE_SEC = 45   # seconds without MCP activity before the pill shows idle
INTERVAL     = int(os.environ.get("SAMPLE_INTERVAL", "10"))
RETENTION    = int(os.environ.get("RETENTION_DAYS", "180")) * 86400
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
CREATE INDEX IF NOT EXISTS idx_proc_ts   ON proc(ts);
CREATE INDEX IF NOT EXISTS idx_models_ts ON models(ts);
CREATE INDEX IF NOT EXISTS idx_edges_ts  ON edges(ts);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_event ON events(ts, service, kind);
"""
_SAMPLE_MIGRATIONS = ("cpu REAL", "ram_used REAL", "ram_total REAL", "load1 REAL", "ctemp REAL")

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
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def _apply_schema_migrations(conn):
    conn.executescript(_DB_SCHEMA)
    for col in _SAMPLE_MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE samples ADD COLUMN {col}")
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
          "procs": [], "models": [], "callers": [], "host": {}, "gpu_avail": None}
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
    # close() must run even when connect/request/read raise (dockerd slow,
    # restarting, unreachable) — otherwise the manually-created AF_UNIX socket fd
    # leaks on every failed call and the process eventually hits its fd limit.
    c = http.client.HTTPConnection("localhost", timeout=4)
    c.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        c.sock.settimeout(4); c.sock.connect(DOCKER_SOCK)
        c.request("GET", path)
        return c.getresponse().read()
    finally:
        c.close()

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

def health_scan():
    HEALTH["docker"]  = collect_docker()
    HEALTH["systemd"] = collect_systemd()
    HEALTH["update"]  = collect_update()
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
HOST_POLL_TIMEOUT = 15

def probe_host_metrics(user, host, port, family="linux"):
    """Run the right probe on the remote via SSH stdin, return (data, error).
    Windows hosts get the PowerShell probe; everything else gets probe.py. Both
    emit the same JSON shape, so the caller and the UI don't branch on OS."""
    if family == "windows":
        if not _PROBE_PS_SCRIPT:
            return None, "probe.ps1 not packaged in this image"
        rc, out, err, _ = _ssh_with_stdin(user, host, port, _WIN_PS_CMD,
                                          _PROBE_PS_SCRIPT, timeout=HOST_POLL_TIMEOUT)
    else:
        if not _PROBE_SCRIPT:
            return None, "probe.py not packaged in this image"
        rc, out, err, _ = _ssh_with_stdin(user, host, port, "python3 -",
                                          _PROBE_SCRIPT, timeout=HOST_POLL_TIMEOUT)
    if rc != 0:
        return None, _clean_ssh_err(err, out, rc)
    # lstrip a UTF-8 BOM: PowerShell over SSH can prepend one, which str.strip()
    # won't remove and which would otherwise make json.loads choke on char 0.
    out = (out or "").lstrip("﻿").strip()
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
                fam = ((check.get("os") or {}).get("family")) or "linux"
                data, err = probe_host_metrics(u, host, port, fam)
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
    "alert_min_level":     "warning",  # "warning" or "critical"
    "disk_alert_pct":      "90",       # disk usage % that trips an alert
}
SETTING_SECRETS = {"discord_webhook_url", "telegram_token"}   # never round-tripped to the UI in full

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

# ── Notifier: Discord webhook + ntfy.sh + Telegram ─────────────────────────
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

def _tg_escape(text):
    """Escape Telegram legacy-Markdown metacharacters in user-supplied text."""
    return (text or "").replace("\\", "\\\\").replace("_", "\\_").replace("*", "\\*") \
                       .replace("`", "\\`").replace("[", "\\[")

def _post_to_telegram(token, chat_id, level, title, body):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    text = (f"*{_tg_escape(title)}*\n\n{_tg_escape(body)}\n\n"
            f"_HomeLab Monitor · {level}_")
    return _post_json(url, {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})

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
    if s.get("telegram_token") and s.get("telegram_chat_id"):
        try: _post_to_telegram(s["telegram_token"], s["telegram_chat_id"],
                               level, title, detail); out.append(("telegram", True, None))
        except Exception as e: out.append(("telegram", False, str(e)))
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
    if not (s.get("discord_webhook_url") or s.get("ntfy_topic")
            or (s.get("telegram_token") and s.get("telegram_chat_id"))):
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
    procs = {}
    gpu_avail = False
    try:
        g = smi(["--query-gpu=utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu",
                 "--format=csv,noheader,nounits"]).splitlines()[0].split(",")
        # Parse each field defensively: nvidia-smi emits the literal "[N/A]" /
        # "[Not Supported]" for power.draw/temperature.gpu on many consumer/laptop
        # GPUs and inside containers, even with `nounits`. The old all-or-nothing
        # float() unpack raised ValueError on that, which the except below caught
        # and reported the (present, working) GPU as ABSENT. Degrade just the bad
        # field to 0 instead.
        util, mem_used, mem_total, power, temp = (_gpu_num(x) for x in g)
        for line in smi(["--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"]).splitlines():
            if line.strip():
                pid, mem = (p.strip() for p in line.split(","))
                svc = service_for_pid(pid, nm)
                procs[svc] = procs.get(svc, 0) + _gpu_num(mem)
        gpu_avail = True
    except Exception as e:
        # Log only on the ok→fail edge so a permanently GPU-less host doesn't spam.
        if LATEST.get("gpu_avail"):
            print("GPU sample failed (continuing without GPU):", e, flush=True)

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

    host = read_host()
    ts = int(time.time())
    if _DB_MAINTENANCE:
        return
    with LOCK:
        # When the GPU is absent/failed, store NULL for the GPU columns (not 0) so
        # history charts skip the gap via AVG() instead of showing a fake 0 dip;
        # the host columns are always real.
        gcols = (util, mem_used, mem_total, power, temp) if gpu_avail else (None,)*5
        DB.execute("INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp,cpu,ram_used,ram_total,load1,ctemp)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (ts, *gcols, host["cpu"], host["ram_used"],
                    host["ram_total"], host["load1"], host["ctemp"]))
        for svc, mem in procs.items():
            DB.execute("INSERT INTO proc VALUES(?,?,?)", (ts, svc, mem))
        for svc, mdl, vram in models:
            if vram is not None:          # persist only VRAM-bearing rows; idle catalogue
                DB.execute("INSERT INTO models VALUES(?,?,?,?)", (ts, svc, mdl, vram))  # lives in LATEST only
        for (caller, server), n in edges.items():
            DB.execute("INSERT INTO edges VALUES(?,?,?,?)", (ts, caller, server, n))
        if ts % 360 < INTERVAL:
            for t in ("samples", "proc", "models", "edges", "events"):
                DB.execute(f"DELETE FROM {t} WHERE ts<?", (ts - RETENTION,))
        DB.commit()
    LATEST.update(ts=ts, util=util, mem_used=mem_used, mem_total=mem_total, power=power, temp=temp,
                  gpu_avail=gpu_avail,
                  procs=sorted(({"service": s, "mem": round(m)} for s, m in procs.items()), key=lambda x: -x["mem"]),
                  models=[{"service": s, "model": m, "vram": v} for s, m, v in models],
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

@app.route("/api/health")
def api_health():
    """Current state of the status monitors (Docker + systemd) plus a light GPU/host
    snapshot. Cheap and DB-free, so the dashboard can poll it often."""
    gpu_avail = LATEST.get("gpu_avail")
    now = {"gpu": {"util": LATEST["util"], "mem_used": LATEST["mem_used"],
                   "mem_total": (LATEST["mem_total"] or 24576) if gpu_avail else 0,
                   "power": LATEST["power"], "temp": LATEST["temp"],
                   "available": bool(gpu_avail)},
           "host": enrich_os_upgrade(LATEST["host"])}
    docker  = HEALTH["docker"]  or {"available": False, "reason": "warming up…",
                                    "containers": [], "summary": {"total": 0, "running": 0, "problems": 0}}
    systemd = HEALTH["systemd"] or {"available": False, "reason": "warming up…",
                                    "services": [], "summary": {}}
    update  = HEALTH["update"]  or {"available": False, "current": VERSION}
    return jsonify({"version": VERSION, "updated": HEALTH["at"], "now": now,
                    "docker": docker, "systemd": systemd, "update": update,
                    "os_updates": os_updates_summary(),
                    "diagnostics": local_diagnostics(),
                    "mcp": {"enabled": _mcp_enabled(), "port": _mcp_port()},
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
        r = subprocess.run(["du", "-b", "--max-depth=2", "--one-file-system", real],
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
        save_settings(updates)
    return jsonify({"version": VERSION, "settings": _public_settings()})

@app.route("/api/notify/test", methods=["POST"])
def api_notify_test():
    """Send a one-shot test alert using the currently saved settings."""
    s = get_settings()
    if not (s.get("discord_webhook_url") or s.get("ntfy_topic")
            or (s.get("telegram_token") and s.get("telegram_chat_id"))):
        return jsonify({"ok": False, "results": [],
                        "reason": "No Discord webhook, ntfy topic, or Telegram bot configured."}), 400
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
