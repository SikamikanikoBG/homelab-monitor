"""Named pre-checks the catalogue refers to by id (`"pre": [...]`).

Each returns None when the action may go ahead, or a plain sentence saying why
not. They read the same cached data the dashboard shows; none of them runs a
command on the host except `sudo_nopasswd`.
"""
import json

from backend.agent import ports


def _containers(host):
    if host == "local":
        return ((ports.host_health("local").get("docker") or {}).get("containers")) or None
    return ((ports.host_block(host).get("docker") or {}).get("containers")) or None


def _services(host):
    if host == "local":
        return ((ports.host_health("local").get("systemd") or {}).get("services")) or None
    blk = ports.host_block(host)
    return ((blk.get("systemd") or {}).get("services")) or blk.get("services") or None


def _models(host):
    return [m for m in ports.loaded_models() if m.get("host") == host]


def _installed(host):
    rows = ports.host_health(host).get("model_catalog") or []
    return [m for m in rows if isinstance(m, dict)]


def container_exists(host, params, **_):
    rows = _containers(host)
    if rows is None:
        return "I can't see the containers on %s right now (offline or not polled yet)." % host
    if not any(c.get("name") == params.get("name") for c in rows):
        return "There is no container called %s on %s." % (params.get("name"), host)
    return None


def service_exists(host, params, **_):
    rows = _services(host)
    if rows is None:
        return None          # services list not collected for this host; the command itself will report
    if not any(s.get("name") == params.get("unit") for s in rows):
        return "There is no service called %s on %s." % (params.get("unit"), host)
    return None


def model_not_loaded(host, params, **_):
    for m in _models(host):
        if m.get("model") == params.get("model") and m.get("vram") is not None:
            return "%s is loaded right now on %s; unload it first." % (params.get("model"), host)
    return None


def model_installed(host, params, **_):
    rows = _installed(host)
    if rows and not any((m.get("name") or m.get("model")) == params.get("model") for m in rows):
        return "%s isn't installed on %s." % (params.get("model"), host)
    return None


def model_digest(host, params):
    """What an approval to delete a model is bound to: its digest when the
    catalogue reports one, else size + modified time."""
    for m in _installed(host):
        if (m.get("name") or m.get("model")) == params.get("model"):
            return {k: m.get(k) for k in ("digest", "size", "modified_at", "modified") if m.get(k) is not None}
    return {}


def path_allowed(host, params, **_):
    from backend.agent import state
    try:
        allowed = json.loads(state.get("watched_paths") or "{}").get(host) or []
    except ValueError:
        allowed = []
    path = (params.get("path") or "").rstrip("/") or "/"
    if ".." in path.split("/"):
        return "Paths with .. aren't allowed."
    for p in allowed:
        p = p.rstrip("/") or "/"
        if path == p or path.startswith(p + "/"):
            return None
    return ("%s isn't in the watched paths for %s. Add it under Agent policy → Watched paths first."
            % (params.get("path"), host))


def sudo_nopasswd(host, params, **_):
    r = ports.run_on_host(host, "sudo -n true")
    if r is None:
        return "No such host."
    return None if r.get("ok") else "needs_sudo"


CHECKS = {"container_exists": container_exists, "service_exists": service_exists,
          "model_not_loaded": model_not_loaded, "model_installed": model_installed,
          "path_allowed": path_allowed}


def run(names, host, params):
    for n in names or []:
        msg = CHECKS[n](host, params)
        if msg:
            return msg
    return None
