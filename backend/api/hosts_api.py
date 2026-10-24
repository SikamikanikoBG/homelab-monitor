"""backend/api/hosts_api.py — hosts_api routes (Phase 3.4)."""
from flask import Blueprint, request, jsonify, Response, send_file, send_from_directory, after_this_request, g, abort
import hashlib
import json
import os
import time
import re
import shlex

bp = Blueprint('hosts_api', __name__)


def _agent_hosts():
    """Agent policy follows a host's rename and is dropped with the host."""
    from backend.agent import hosts
    return hosts


@bp.route("/api/hosts", methods=["GET", "POST"])
def api_hosts():
    import app as _app
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        host, err = _app.add_host((body.get("name") or "").strip(),
                             (body.get("ssh_target") or "").strip(),
                             (body.get("tags") or "").strip())
        if err:
            return jsonify({"ok": False, "error": err}), 400
        return jsonify({"ok": True, "host": host}), 201
    return jsonify({"hosts": _app.list_hosts()})


@bp.route("/api/hosts/<name>", methods=["DELETE", "PATCH"])
def api_hosts_one(name):
    import app as _app
    if request.method == "DELETE":
        ok = _app.delete_host(name)
        if ok:
            _agent_hosts().deleted(name)
        return jsonify({"ok": ok}), (200 if ok else 404)
    body = request.get_json(silent=True) or {}
    new_name = body.get("name").strip() if isinstance(body.get("name"), str) else None
    ssh_target = body.get("ssh_target").strip() if isinstance(body.get("ssh_target"), str) else None
    tags = body.get("tags").strip() if isinstance(body.get("tags"), str) else None
    # Validate the target up front: rename and target-update are two mutations,
    # and a combined request must not commit the rename only to 400 on the
    # target — a non-ok PATCH has to mean "nothing changed".
    if ssh_target is not None and _app._parse_ssh_target(ssh_target) is None:
        return jsonify({"ok": False, "error": "SSH target must look like user@host or user@host:port."}), 400
    host = None
    if new_name and new_name != name:
        host, err = _app.rename_host(name, new_name)
        if err:
            return jsonify({"ok": False, "error": err}), 404 if err.startswith("No host") else 400
        _agent_hosts().renamed(name, host["name"])
        name = host["name"]
    if ssh_target is not None or tags is not None:
        host, err = _app.update_host(name, ssh_target=ssh_target, tags=tags)
        if err:
            return jsonify({"ok": False, "error": err}), 400 if "look like" in err or "Nothing" in err else 404
    if host is None:
        return jsonify({"ok": False, "error": "Nothing to update."}), 400
    return jsonify({"ok": True, "host": host})


@bp.route("/api/lan/scan")
def api_lan_scan():
    import app as _app
    return jsonify(_app.discover_lan())


@bp.route("/api/host_data/<name>")
def api_host_data(name):
    import app as _app
    """Per-host metric snapshot. `local` is the hub itself; remotes are served
    from the in-memory cache populated by host_poller()."""
    if name == "local":
        return jsonify({"name": "local", "host": _app.enrich_os_upgrade(_app._local_now_snapshot()),
                        "at": int(time.time()), "online": True})
    with _app.HOST_DATA_LOCK:
        entry = _app.HOST_DATA.get(name)
    if not entry or "data" not in entry:
        # No successful poll yet (or never registered) — still respond 200 so
        # the UI can render a "waiting" state instead of erroring.
        return jsonify({"name": name, "online": False,
                        "error": (entry or {}).get("error") or "no data yet",
                        "at": (entry or {}).get("at"),
                        "host": None})
    age     = int(time.time()) - int(entry["at"])
    online  = _app._host_is_online(entry)
    return jsonify({"name": name, "host": _app.enrich_os_upgrade(entry["data"].get("host", {})),
                    "at": entry["at"], "online": online,
                    "stale_for": 0 if online else age,
                    "error": entry.get("error")})


@bp.route("/api/host_history")
def api_host_history():
    """CPU / RAM / load history for one host — the System tab's chart feed.

    `host=local` is the hub, served from its own `samples` series via /api/data;
    everything else comes out of `host_samples`, which the poller has been
    filling with each remote's vitals all along. There is deliberately no
    "remote history" endpoint separate from this one: the System tab used to
    chart the hub and then keep charting the hub after you switched machines,
    and the only reason that bug could exist is that nothing here read the
    per-host rows.

    Shaped like the slice of /api/data the chart consumes — {labels, cpu,
    ram_used, ram_total, load1, ctemp} — so the same drawing code serves both.
    """
    import app as _app
    from backend.db.repos import host_samples as hs_repo

    name = request.args.get("host", "local")
    rng = request.args.get("range", "6h")
    span = _app.RANGES.get(rng, 21600)
    now = int(time.time())
    with _app.LOCK:
        if span is None:
            since = hs_repo.min_ts(name, conn=_app.DB) or now
        else:
            since = now - span
        bk = max(_app.INTERVAL, round(max(1, now - since) / _app.MAX_POINTS))
        rows = hs_repo.vitals_series(name, since, bk, conn=_app.DB)

    return jsonify({
        "host": name, "range": rng, "bucket_sec": bk,
        "labels":    [int(r[0]) for r in rows],
        # None, not 0, for a sensor the host never reported: Chart.js leaves a
        # gap where there is no reading, which is the honest drawing. A zeroed
        # point would render as a machine that was genuinely idle.
        "cpu":       [r[1] for r in rows],
        "ram_used":  [r[2] for r in rows],
        "ram_total": [r[3] for r in rows],
        "load1":     [r[4] for r in rows],
        "ctemp":     [r[5] for r in rows],
    })


@bp.route("/api/fleet")
def api_fleet():
    import app as _app
    """Compact summary KPIs for every host in the fleet. Drives the All-hosts
    table. Order: local first, then registered hosts in the order they were
    added.

    The rows are built in app.fleet_payload() because /api/stream pushes the same
    document on its `fleet` event — two builders would drift, and the table would
    then say something different depending on whether it arrived by poll or by
    push."""
    return jsonify(_app.fleet_payload())


@bp.route("/api/hosts/<name>/test", methods=["POST"])
def api_hosts_test(name):
    import app as _app
    result = _app.probe_host(name)
    if result is None:
        return jsonify({"ok": False, "error": "no such host"}), 404
    return jsonify({"ok": True, "result": result})


def host_run_enabled():
    """`/api/hosts/<name>/run` runs any shell command on a host. Nothing in the
    dashboard uses it, so it is off unless ENABLE_HOST_RUN=1 (#354)."""
    return (os.environ.get("ENABLE_HOST_RUN") or "").strip().lower() in ("1", "true", "yes", "on")


@bp.route("/api/hosts/<name>/run", methods=["POST"])
def api_hosts_run(name):
    import app as _app
    """Execute a command on a registered host. Body: {cmd, sudo_password?}.
    The sudo password is processed in-memory: piped via stdin to `sudo -S`
    on the remote, NEVER stored in the _app.DB, NEVER written to logs, NEVER
    in any process's argv. Don't pass arbitrary cmd from untrusted users —
    the API is reachable to anyone on the LAN who can hit the dashboard."""
    if not _app.ENABLE_CONTROLS:
        return jsonify({"ok": False, "error": "Controls are disabled (ENABLE_CONTROLS=0)."}), 403
    if not host_run_enabled():
        return jsonify({"ok": False, "error": "Running arbitrary commands is off by default. "
                        "Set ENABLE_HOST_RUN=1 to turn it on (see website/configuration.md)."}), 403
    body = request.get_json(silent=True) or {}
    cmd = (body.get("cmd") or "").strip()
    sudo_password = body.get("sudo_password") or None
    if not cmd:
        return jsonify({"ok": False, "error": "cmd required"}), 400
    result = _app.run_on_host(name, cmd, sudo_password=sudo_password)
    # Every use is logged — host, caller and exit code, never the command
    # (it may hold secrets).
    from backend.auth import gate
    p = gate.principal()
    print(json.dumps({"event": "host_run", "host": name, "caller": "%s:%s" % (p.get("kind"), p.get("id")),
                      "ip": request.remote_addr, "exit_code": (result or {}).get("exit_code"),
                      "cmd_sha256": hashlib.sha256(cmd.encode("utf-8")).hexdigest()}), flush=True)
    # Drop the password reference ASAP — Python keeps the string object until
    # GC, but at least we don't hold our own reference past this point.
    sudo_password = None
    body = None
    if result is None:
        return jsonify({"ok": False, "error": "no such host"}), 404
    return jsonify(result)


_SERVICE_ACTIONS = ("start", "stop", "restart")
_UNIT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,254}$")
_WIN_SERVICE_PS = {
    "start":   "Start-Service",
    "stop":    "Stop-Service -Force",
    "restart": "Restart-Service -Force",
}


@bp.route("/api/services/<name>/action", methods=["POST"])
def api_service_action(name):
    """Start/stop/restart a service — local systemd unit (D-Bus) or, given
    `host`, a registered remote's systemd unit (SSH + systemctl, same
    sudo-password plumbing as api_hosts_run) or Windows service (SSH +
    PowerShell). Body: {action, host?, sudo_password?}. Gated by ENABLE_CONTROLS."""
    import app as _app
    if not _app.ENABLE_CONTROLS:
        return jsonify({"ok": False, "error": "Service controls are disabled (ENABLE_CONTROLS=0). Unset it, or drop docker-compose.readonly.yml, to enable them (see website/configuration.md)."}), 403
    if not _UNIT_NAME_RE.match(name or ""):
        return jsonify({"ok": False, "error": "invalid unit/service name"}), 400
    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "").strip()
    host = (body.get("host") or "local").strip()
    if action not in _SERVICE_ACTIONS:
        return jsonify({"ok": False, "error": "action must be one of: %s" % ", ".join(_SERVICE_ACTIONS)}), 400

    if host == "local":
        ok, err = _app.systemd_unit_action(name, action)
        return jsonify({"ok": ok} if ok else {"ok": False, "error": err})

    with _app.HOST_DATA_LOCK:
        entry = _app.HOST_DATA.get(host) or {}
    family = (((entry.get("data") or {}).get("host") or {}).get("os") or {}).get("family") or "linux"
    if family == "windows":
        script = "%s -Name %s" % (_WIN_SERVICE_PS[action], _app._ps_single_quote(name))
        result = _app.run_on_host_windows(host, script)
    else:
        sudo_password = body.get("sudo_password") or None
        result = _app.run_on_host(host, "systemctl %s -- %s" % (action, shlex.quote(name)),
                                  sudo_password=sudo_password)
        sudo_password = None
    body = None
    if result is None:
        return jsonify({"ok": False, "error": "no such host"}), 404
    return jsonify(result)


