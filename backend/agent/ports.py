"""The agent's only door into app.py.

Every other module under backend/agent/ is plain domain code: it never imports
`app` (a test walks the package and checks). Whatever the agent needs from the
running dashboard — the database handle, the SSH runner, the Docker socket,
host probe data, settings, notifications — comes through a function here, with
a lazy `import app` inside it. When the refactor's `core/act/` lands, this file
is re-pointed and nothing else changes.
"""
import contextlib
import functools
import os
import threading


def _app():
    import app as _a
    return _a


@contextlib.contextmanager
def db():
    """The dashboard's own connection under its lock, with the agent tables
    created on first use. Hold it for SQL only — never across a model call or
    an SSH command."""
    from backend.agent import schema
    a = _app()
    with a.LOCK:
        schema.ensure(a.DB)
        yield a.DB


def db_handle():
    return _app().DB


# ── switches ─────────────────────────────────────────────────────────────────

def controls_enabled():
    return bool(_app().ENABLE_CONTROLS)


def version():
    return _app().VERSION


# ── remote execution (catalogue-rendered commands only; see executor.py) ────

def run_on_host(host, cmd):
    return _app().run_on_host(host, cmd)


def run_on_host_windows(host, script):
    return _app().run_on_host_windows(host, script)


def ps_quote(s):
    return _app()._ps_single_quote(s)


def docker_req(method, path, body=None, timeout=30):
    return _app()._docker_req(method, path, body=body, timeout=timeout)


def systemd_unit_action(unit, action):
    return _app().systemd_unit_action(unit, action)


def self_container_name():
    return _app()._self_container_name()


# ── what the hub knows about hosts and models ────────────────────────────────

def registered_hosts():
    """[{name, ssh_target}] — the hub itself is 'local' and not in this list."""
    return [{"name": h.get("name"), "ssh_target": h.get("ssh_target")} for h in (_app().list_hosts() or [])]


def host_names():
    return ["local"] + [h["name"] for h in registered_hosts()]


def host_block(host):
    """The probe's `host` block for a host (os, disks, …), {} when unknown."""
    a = _app()
    if host == "local":
        return (a.LATEST or {}).get("host") or {}
    with a.HOST_DATA_LOCK:
        entry = a.HOST_DATA.get(host) or {}
    return (entry.get("data") or {}).get("host") or {}


def host_data(host):
    """The whole cached probe payload for a remote host, {} for local/unknown."""
    a = _app()
    if host == "local":
        return dict(a.LATEST or {})
    with a.HOST_DATA_LOCK:
        entry = a.HOST_DATA.get(host) or {}
    return dict(entry.get("data") or {})


def host_online(host):
    a = _app()
    if host == "local":
        return True
    with a.HOST_DATA_LOCK:
        entry = a.HOST_DATA.get(host)
    return bool(entry) and bool(a._host_is_online(entry))


def host_reach(host):
    """What the hub knows about reaching a host: {"state": online|offline|unknown,
    "since": last time it answered (epoch) or None, "error": last poll error}.
    "unknown" only while a host hasn't been polled yet since the hub started."""
    a = _app()
    if host == "local":
        return {"state": "online", "since": None, "error": None}
    with a.HOST_DATA_LOCK:
        entry = dict(a.HOST_DATA.get(host) or {})
    if entry and a._host_is_online(entry):
        return {"state": "online", "since": entry.get("at"), "error": None}
    check = next((h.get("last_check") or {} for h in (a.list_hosts() or []) if h.get("name") == host), {})
    skipped = (check.get("summary") or {}).get("overall") not in (None, "ok", "warn")   # the poller won't try it
    if entry.get("data") or entry.get("error") or skipped:
        return {"state": "offline", "since": entry.get("at") or _last_sample(host),
                "error": entry.get("error") or (check.get("summary") or {}).get("detail")}
    return {"state": "unknown", "since": None, "error": None}


def _last_sample(host):
    """The newest stored sample of a host (survives hub restarts), or None."""
    try:
        with db() as c:
            row = c.execute("SELECT MAX(ts) FROM host_samples WHERE host=?", (host,)).fetchone()
            ts = row[0] if row else None
            if not ts:
                row = c.execute("SELECT MAX(ts) FROM host_samples_1h WHERE host=?", (host,)).fetchone()
                ts = row[0] if row else None
        return int(ts) if ts else None
    except Exception as e:                      # a missing table on an odd DB: "since" is just unknown
        print("agent: last sample of %s unreadable: %s" % (host, e), flush=True)
        return None


def host_health(host):
    """Docker/systemd/GPU data for capability checks, from the same caches the
    dashboard shows. Remote: the probe payload; local: HEALTH + LATEST."""
    a = _app()
    if host == "local":
        h = dict(a.HEALTH or {})
        h["gpus"] = (a.LATEST or {}).get("gpus") or []
        h["models"] = (a.LATEST or {}).get("models") or []
        h["model_catalog"] = (a.LATEST or {}).get("model_catalog") or []
        return h
    return host_data(host)


def ai_servers():
    return [dict(s) for s in (_app().AI_SERVERS or [])]


def loaded_models():
    """[{service, model, vram, host}] for every model loaded anywhere the hub sees."""
    a = _app()
    rows = [dict(m, host=m.get("host") or "local") for m in ((a.LATEST or {}).get("models") or [])]
    with a.HOST_DATA_LOCK:
        items = list(a.HOST_DATA.items())
    for name, entry in items:
        for m in ((entry.get("data") or {}).get("models") or []):
            if isinstance(m, dict):
                rows.append(dict(m, host=name))
    return rows


def custom_ai_servers():
    import json
    raw = (_app().get_settings().get("custom_ai_servers") or "").strip()
    try:
        v = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [x for x in v if isinstance(x, dict)]


def remote_model_catalogs():
    """{host: [catalog rows]} from every remote's last probe."""
    a = _app()
    with a.HOST_DATA_LOCK:
        items = list(a.HOST_DATA.items())
    out = {}
    for name, entry in items:
        cat = (entry.get("data") or {}).get("model_catalog") or []
        out[name] = [m for m in cat if isinstance(m, dict)]
    return out


@functools.lru_cache(maxsize=256)
def _resolve(name):
    """A name's IPv4, once per process (a slow resolver must not slow every run)."""
    import ipaddress
    import socket
    try:
        ipaddress.ip_address(name)
        return name
    except ValueError:
        pass
    try:
        return socket.gethostbyname(name)
    except OSError:
        return None


def host_for_address(addr):
    """The fleet host that owns an address (its ssh target, or any IPv4 its
    probe reports), "local" for the hub's loopback, else None. Used to say
    where a model server runs instead of assuming the hub (UAT-1)."""
    addr = (addr or "").strip().lower().strip("[]")
    if not addr:
        return None
    if addr in ("localhost", "127.0.0.1", "::1"):
        return "local"
    want = {addr, _resolve(addr)} - {None}
    for name in host_names():
        known = set()
        target = ssh_target_host(name) if name != "local" else None
        if target:
            known |= {target.lower(), _resolve(target)} - {None}
        net = host_block(name).get("net") or {}
        if net.get("primary_ip"):
            known.add(net["primary_ip"])
        for i in net.get("ifaces") or []:
            known |= {str(ip).split("/")[0] for ip in i.get("ipv4") or []}
        if want & known:
            return name
    return None


def ssh_target_host(host):
    """The address part of a registered host's ssh target, for reachability
    checks of model servers discovered on it."""
    a = _app()
    for h in registered_hosts():
        if h["name"] == host:
            parsed = a._parse_ssh_target(h.get("ssh_target") or "")
            return parsed[1] if parsed else None
    return None


# ── settings, notifications, retention ───────────────────────────────────────

def dashboard_settings():
    return _app().get_settings()


def dispatch_alert(level, title, detail):
    from backend.notify import dispatch_alert as _dispatch
    return _dispatch(dashboard_settings(), level, title, detail)


def dispatch_to_channels(level, title, detail, channels):
    from backend.notify import _dispatch_to_channels
    return _dispatch_to_channels(dashboard_settings(), level, title, detail, channels)


# What each alert channel needs before it can deliver — the same settings
# backend/notify checks before sending.
_CHANNEL_NEEDS = {"discord": ("discord_webhook_url",), "ntfy": ("ntfy_topic",),
                  "telegram": ("telegram_token", "telegram_chat_id"),
                  "email": ("email_host", "email_from", "email_to"),
                  "slack": ("slack_webhook_url",), "webhook": ("webhook_url",)}


def channels_configured():
    s = dashboard_settings()
    return sorted(c for c, keys in _CHANNEL_NEEDS.items() if all(s.get(k) for k in keys))


def retention_secs():
    return _app().get_retention_secs()


def hub_tz():
    return os.environ.get("TZ") or "UTC"


# ── live push over the existing /api/stream ──────────────────────────────────

def live_cond():
    return _app().LIVE_COND


# ── in-process read transport (agent read tools, no network hop) ────────────

_client_lock = threading.Lock()
_client = {}


def internal_get(path):
    """GET a dashboard route in-process, as the internal read-only caller.
    Returns (status, body_text)."""
    from backend.auth.gate import INTERNAL_ENV_KEY
    a = _app()
    c = a.app.test_client()
    r = c.get(path, environ_base={INTERNAL_ENV_KEY: True, "REMOTE_ADDR": "127.0.0.1"})
    return r.status_code, r.get_data(as_text=True)
