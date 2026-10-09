"""backend/api/integrations.py — integrations routes (Phase 3.4)."""
from flask import Blueprint, request, jsonify, Response, send_file, send_from_directory, after_this_request, g, abort
import logging
import shlex

from backend.db.repos import notify as notify_repo
from backend.notify import dispatch_alert, _dispatch_to_channels

_log = logging.getLogger(__name__)

bp = Blueprint('integrations', __name__)


def _remote_log_stream(host, name, tail):
    """One shot of a remote container's logs, framed as SSE so the browser's
    log drawer reads a remote host exactly like the hub.

    `docker logs --follow` over SSH would hold an ssh process (and a worker
    thread) open per viewer for as long as the drawer is open, so a remote
    host is polled by the client instead — it re-requests every few seconds.
    The stream says so in its last event rather than leaving the drawer
    looking live when it is not."""
    import app as _app
    cmd = "docker logs --tail %d --timestamps %s 2>&1" % (tail, shlex.quote(name))
    res = _app.run_on_host(host, cmd)
    if res is None:
        yield "event: error\ndata: no such host\n\n"
        return
    if not res.get("ok") and not (res.get("stdout") or "").strip():
        err = (res.get("stderr") or "").strip() or "docker logs failed on %s" % host
        yield "event: error\ndata: %s\n\n" % err.replace("\n", " ")[:300]
        return
    for line in (res.get("stdout") or "").splitlines():
        yield "data: %s\n\n" % line
    yield "event: end\ndata: snapshot\n\n"


@bp.route("/api/containers/<name>/logs")
def api_container_logs(name):
    import app as _app
    """Last `tail` log lines for a container; with follow=1, streams new lines as
    SSE. Read-only — `docker logs` needs no extra socket permissions.
    With `host=<fleet name>`, reads that host's container over SSH instead
    (a snapshot per request — see _remote_log_stream)."""
    if not _app._CT_NAME_RE.match(name or ""):
        return jsonify({"error": "invalid container name"}), 400
    try:
        tail = max(1, min(2000, int(request.args.get("tail", 200))))
    except (TypeError, ValueError):
        tail = 200
    follow = request.args.get("follow") == "1"
    host = (request.args.get("host") or "local").strip()
    if host and host != "local":
        return Response(_remote_log_stream(host, name, tail),
                        mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    return Response(_app._docker_log_stream(name, tail, follow),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


_CONTAINER_ACTIONS = ("start", "stop", "restart")
_RESTART_POLICIES = ("no", "on-failure", "unless-stopped", "always")


def _docker_err_message(raw):
    """Docker's error body is `{"message": "..."}` on a well-formed failure;
    fall back to the raw bytes for anything else (a proxy timeout, empty body)."""
    try:
        import json
        msg = (json.loads(raw) or {}).get("message")
        if msg:
            return msg[:300]
    except (ValueError, KeyError, TypeError):
        pass
    try:
        return raw.decode("utf-8", "replace")[:300] if isinstance(raw, bytes) else str(raw)[:300]
    except (AttributeError, UnicodeDecodeError):
        return "unknown Docker error"


def _remote_docker(host, args):
    """Run one `docker <args>` on a registered host over SSH and return the
    endpoint's own {ok, error} shape. Every value interpolated into the command
    is shell-quoted by the caller, and the container name is validated against
    _CT_NAME_RE before it gets here."""
    import app as _app
    res = _app.run_on_host(host, "docker " + args)
    if res is None:
        return jsonify({"ok": False, "error": "no such host"}), 404
    if res.get("ok"):
        return jsonify({"ok": True})
    err = ((res.get("stderr") or "") + " " + (res.get("stdout") or "")).strip()
    if "no such container" in err.lower():
        return jsonify({"ok": False, "error": "No such container on %s." % host}), 404
    if "permission denied" in err.lower() or "docker daemon" in err.lower():
        err += ("  (the SSH user on %s needs to be in the docker group — the same "
                "access the host probe already uses to list containers)" % host)
    return jsonify({"ok": False, "error": err[:300] or "docker failed on %s" % host}), 400


@bp.route("/api/containers/<name>/action", methods=["POST"])
def api_container_action(name):
    """Start/stop/restart a container — on the hub through the Docker socket,
    or on a registered host with `host` in the body (SSH + `docker`, the same
    plumbing the remote service controls use). Gated by ENABLE_CONTROLS."""
    import urllib.parse
    import app as _app
    if not _app.ENABLE_CONTROLS:
        return jsonify({"ok": False, "error": "Container controls are disabled (ENABLE_CONTROLS=0). Unset it, or drop docker-compose.readonly.yml, to enable them (see website/configuration.md)."}), 403
    if not _app._CT_NAME_RE.match(name or ""):
        return jsonify({"ok": False, "error": "invalid container name"}), 400
    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "").strip()
    host = (body.get("host") or "local").strip()
    if action not in _CONTAINER_ACTIONS:
        return jsonify({"ok": False, "error": "action must be one of: %s" % ", ".join(_CONTAINER_ACTIONS)}), 400
    if host and host != "local":
        return _remote_docker(host, "%s -- %s" % (action, shlex.quote(name)))
    try:
        code, raw = _app._docker_req("POST", "/containers/%s/%s" % (urllib.parse.quote(name), action))
    except Exception as e:
        _log.warning("Docker socket error for container %s/%s: %s", name, action, e)
        return jsonify({"ok": False, "error": "Could not reach the Docker socket: %s" % e}), 500
    # No cache to invalidate: collect_docker() re-lists containers live on every
    # call (the state field is never stale) — the Containers tab picks this up
    # on its next poll, same as everything else on the dashboard.
    if code in (204, 304):
        return jsonify({"ok": True})
    if code == 404:
        return jsonify({"ok": False, "error": "No such container."}), 404
    return jsonify({"ok": False, "error": _docker_err_message(raw)}), 400


@bp.route("/api/containers/<name>/restart-policy", methods=["POST"])
def api_container_restart_policy(name):
    """Change a container's restart policy — hub via the Docker socket, a
    registered host via `host` in the body (SSH + `docker update`)."""
    import urllib.parse
    import app as _app
    if not _app.ENABLE_CONTROLS:
        return jsonify({"ok": False, "error": "Container controls are disabled (ENABLE_CONTROLS=0). Unset it, or drop docker-compose.readonly.yml, to enable them (see website/configuration.md)."}), 403
    if not _app._CT_NAME_RE.match(name or ""):
        return jsonify({"ok": False, "error": "invalid container name"}), 400
    body = request.get_json(silent=True) or {}
    policy = (body.get("policy") or "").strip()
    host = (body.get("host") or "local").strip()
    if policy not in _RESTART_POLICIES:
        return jsonify({"ok": False, "error": "policy must be one of: %s" % ", ".join(_RESTART_POLICIES)}), 400
    if host and host != "local":
        return _remote_docker(host, "update --restart=%s -- %s" % (shlex.quote(policy), shlex.quote(name)))
    try:
        code, raw = _app._docker_req("POST", "/containers/%s/update" % urllib.parse.quote(name),
                                      body={"RestartPolicy": {"Name": policy}})
    except Exception as e:
        _log.warning("Docker socket error for container %s restart-policy: %s", name, e)
        return jsonify({"ok": False, "error": "Could not reach the Docker socket: %s" % e}), 500
    _app._docker_policy["at"] = 0
    if code == 200:
        return jsonify({"ok": True})
    if code == 404:
        return jsonify({"ok": False, "error": "No such container."}), 404
    return jsonify({"ok": False, "error": _docker_err_message(raw)}), 400


@bp.route("/api/notify/test", methods=["POST"])
def api_notify_test():
    import app as _app
    """Send a one-shot test alert using the currently saved settings."""
    s = _app.get_settings()
    if not (s.get("discord_webhook_url") or s.get("ntfy_topic")
            or (s.get("telegram_token") and s.get("telegram_chat_id"))
            or (s.get("email_host") and s.get("email_from") and s.get("email_to"))
            or s.get("slack_webhook_url")
            or s.get("webhook_url")):
        return jsonify({"ok": False, "results": [],
                        "reason": "No Discord webhook, ntfy topic, Telegram bot, email, Slack webhook, or generic webhook configured."}), 400
    results = dispatch_alert(s, "info",
                             "✅ HomeLab Monitor — test alert",
                             "If you see this, alerts are wired up correctly.")
    return jsonify({"ok": all(ok for _, ok, _ in results),
                    "results": [{"channel": c, "ok": ok, "error": err} for c, ok, err in results]})


@bp.route("/api/notify/rules", methods=["GET", "POST"])
def api_notify_rules():
    import app as _app
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        action = body.get("action", "add")
        if action == "add":
            with _app.LOCK:
                notify_repo.insert_rule(
                    body.get("match_kind", "container"), body.get("match_pattern", "*"),
                    body.get("channel", "all"), body.get("min_level", "warning"),
                    1 if body.get("enabled", True) else 0, conn=_app.DB)
        elif action == "update":
            rule_id = body.get("id")
            if not rule_id:
                return jsonify({"ok": False, "error": "id required"}), 400
            with _app.LOCK:
                notify_repo.update_rule(
                    rule_id, body.get("match_kind"), body.get("match_pattern"),
                    body.get("channel"), body.get("min_level"),
                    1 if body.get("enabled", True) else 0, conn=_app.DB)
        elif action == "delete":
            rule_id = body.get("id")
            if not rule_id:
                return jsonify({"ok": False, "error": "id required"}), 400
            with _app.LOCK:
                notify_repo.delete_rule(rule_id, conn=_app.DB)
        else:
            return jsonify({"ok": False, "error": f"unknown action: {action}"}), 400
        return jsonify({"ok": True, "rules": _app.get_notification_rules()})
    return jsonify({"rules": _app.get_notification_rules()})


@bp.route("/api/notify/rules/test", methods=["POST"])
def api_notify_rules_test():
    import app as _app
    """Test a notification rule by sending a sample alert that would match it."""
    body = request.get_json(silent=True) or {}
    s = _app.get_settings()
    if not (s.get("discord_webhook_url") or s.get("ntfy_topic")
            or (s.get("telegram_token") and s.get("telegram_chat_id"))):
        return jsonify({"ok": False, "error": "No notification channels configured."}), 400
    test_rule = {
        "match_kind": body.get("match_kind", "container"),
        "match_pattern": body.get("match_pattern", "*"),
        "channel": body.get("channel", "all"),
        "min_level": body.get("min_level", "warning"),
        "enabled": True,
    }
    test_key = f"{test_rule['match_kind']}:test-rule"
    channels = _app._apply_rules(test_key, body.get("level", "warning"), [test_rule])
    if channels is None:
        return jsonify({"ok": False, "error": "Rule would not match — check kind and pattern."}), 400
    _dispatch_to_channels(s, body.get("level", "warning"),
                          "🔔 HomeLab Monitor — rule test",
                          f"Test of rule: {test_rule['match_kind']} / {test_rule['match_pattern']} → {test_rule['channel']} @ {test_rule['min_level']}",
                          channels)
    return jsonify({"ok": True, "channels": list(channels)})


