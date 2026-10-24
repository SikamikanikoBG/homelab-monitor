"""Model servers the hub already knows about, offered as one-click providers (US-1).

Sources, all in-process data the monitor already collects:
- the hub's recognised AI servers (containers on this box),
- the user's custom AI servers (Settings → General),
- each remote host's model catalogue (Ollama on that host).

A discovered server maps to a preset through the preset's `discovery` list
(data). Remote catalogues are collected over SSH, so the hub may not be able to
reach the port itself: every candidate gets a 2-second reachability check, and
unreachable ones are listed as "seen, but not reachable from the hub".
"""
import concurrent.futures

from backend.agent import ports, providers, store
from backend.agent.providers import http


def _preset_for(provider_key):
    key = (provider_key or "").strip().lower()
    for p in providers.presets():
        if key in (p.get("discovery") or []):
            return p
    return None


def _base(p, host, port):
    return "http://%s:%s%s" % (host, port or p.get("default_port"), p.get("path") or "")


def candidates():
    seen, out = set(), []

    def add(host, server, provider_key, addr, port):
        p = _preset_for(provider_key)
        if not p or not addr:
            return
        base = _base(p, addr, port)
        if base in seen:
            return
        seen.add(base)
        out.append({"host": host, "server": server, "provider_key": provider_key, "preset": p["id"],
                    "base_url": base})

    for s in ports.ai_servers():
        add(s.get("host") or "local", s.get("name"), s.get("provider"), s.get("ip") or "127.0.0.1", s.get("port"))
    for s in ports.custom_ai_servers():
        add(s.get("fleet_host") or s.get("runs_on") or "local", s.get("name"), s.get("provider"), s.get("host"),
            s.get("port"))
    for host, catalog in ports.remote_model_catalogs().items():
        addr = ports.ssh_target_host(host)
        for key in sorted({(m.get("provider") or "") for m in catalog}):
            add(host, key, key, addr, None)
    return out


def _reachable(c):
    p = providers.preset(c["preset"]) or {}
    url = c["base_url"].rstrip("/") + ("/api/tags" if p.get("adapter") == "ollama" else "/models")
    try:
        data = http.get_json(url, timeout=2)
    except Exception:
        return False, []
    if p.get("adapter") == "ollama":
        models = [m.get("name") for m in (data.get("models") or []) if m.get("name")]
    else:
        models = [m.get("id") for m in (data.get("data") or []) if isinstance(m, dict) and m.get("id")]
    return True, models[:50]


def discover():
    cands = candidates()
    known = {r["base_url"].rstrip("/"): r["id"] for r in store.provider_list()}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_reachable, cands))
    for c, (ok, models) in zip(cands, results):
        c["reachable"] = ok
        c["models"] = models
        c["provider_id"] = known.get(c["base_url"].rstrip("/"))
    return cands
