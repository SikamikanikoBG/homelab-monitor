"""backend/notify — alert dispatch and notification scan (Phase 3.3)."""
import json
import re
import socket
import time
import urllib.error
import urllib.request


def _post_json(url, payload, timeout=5):
    import app as _app
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": _app.NOTIFY_USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def _post_text(url, text, headers=None, timeout=5):
    import app as _app
    hdr = dict(headers or {"Content-Type": "text/plain"})
    hdr.setdefault("User-Agent", _app.NOTIFY_USER_AGENT)
    req = urllib.request.Request(url, data=text.encode("utf-8"), headers=hdr)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def _tg_escape(text):
    import app as _app
    """Escape Telegram legacy-Markdown metacharacters in user-supplied text."""
    return (text or "").replace("\\", "\\\\").replace("_", "\\_").replace("*", "\\*") \
                       .replace("`", "\\`").replace("[", "\\[")


def _alert_host_label():
    import app as _app
    """Machine name to stamp on every alert so a notification says *where* the
    problem is. Alerts are raised from the hub's own docker/systemd/disk/GPU
    snapshots, so this is the hub host: prefer the probe-reported hostname (the
    same name the dashboard's host tab shows), fall back to the OS hostname, and
    finally to "" so a label-less environment degrades to the old behaviour."""
    try:
        name = ((_app.LATEST or {}).get("host") or {}).get("hostname")
        if name:
            return str(name).strip()
    except Exception as e:
        print(f"notify/_alert_host_label hostname lookup error: {e}", flush=True)
    try:
        return socket.gethostname()
    except Exception as e:
        print(f"notify/_alert_host_label gethostname error: {e}", flush=True)
        return ""


def dispatch_alert(s, level, title, detail, host=None):
    import app as _app
    """Send to whichever channels are configured. Returns list of (channel, ok, err).

    `title` is prefixed with the machine name (`[host] …`) so every channel —
    Discord, ntfy, Telegram, email, Slack and generic webhook alike — names
    which machine the alert is about.
    Pass host="" to opt out (e.g. a generic message that isn't host-specific)."""
    if host is None:
        host = _app._alert_host_label()
    if host:
        title = f"[{host}] {title}"
    out = []
    if s.get("discord_webhook_url"):
        try: _app.send_discord(s["discord_webhook_url"], level, title, detail); out.append(("discord", True, None))
        except Exception as e: out.append(("discord", False, str(e)))
    if s.get("ntfy_topic"):
        try: _app.send_ntfy(s.get("ntfy_server") or "https://ntfy.sh",
                       s["ntfy_topic"], level, title, detail); out.append(("ntfy", True, None))
        except Exception as e: out.append(("ntfy", False, str(e)))
    if s.get("telegram_token") and s.get("telegram_chat_id"):
        try: _app._post_to_telegram(s["telegram_token"], s["telegram_chat_id"],
                               level, title, detail); out.append(("telegram", True, None))
        except Exception as e: out.append(("telegram", False, str(e)))
    # Email via SMTP
    if s.get("email_host") and s.get("email_from") and s.get("email_to"):
        try:
            _app._send_email(s["email_host"], s.get("email_port", "587"),
                        s.get("email_use_tls", "1") == "1",
                        s.get("email_username", ""), s.get("email_password", ""),
                        s["email_from"], s["email_to"],
                        level, title, detail)
            out.append(("email", True, None))
        except Exception as e:
            out.append(("email", False, str(e)))
    # Slack incoming webhook
    if s.get("slack_webhook_url"):
        try: _app.send_slack(s["slack_webhook_url"], level, title, detail); out.append(("slack", True, None))
        except Exception as e: out.append(("slack", False, str(e)))
    # Generic webhook
    if s.get("webhook_url"):
        try: _app.send_webhook(s["webhook_url"], level, title, detail, host or ""); out.append(("webhook", True, None))
        except Exception as e: out.append(("webhook", False, str(e)))
    return out


def _dispatch_to_channels(s, level, title, detail, channels):
    import app as _app
    """Dispatch to a specific set of channels only."""
    if "discord" in channels and s.get("discord_webhook_url"):
        try: _app.send_discord(s["discord_webhook_url"], level, title, detail)
        except Exception as e: print("notifier discord error:", e, flush=True)
    if "ntfy" in channels and s.get("ntfy_topic"):
        try: _app.send_ntfy(s.get("ntfy_server") or "https://ntfy.sh",
                       s["ntfy_topic"], level, title, detail)
        except Exception as e: print("notifier ntfy error:", e, flush=True)
    if "telegram" in channels and s.get("telegram_token") and s.get("telegram_chat_id"):
        try: _app._post_to_telegram(s["telegram_token"], s["telegram_chat_id"],
                               level, title, detail)
        except Exception as e: print("notifier telegram error:", e, flush=True)
    if "email" in channels and s.get("email_host") and s.get("email_from") and s.get("email_to"):
        try: _app._send_email(s["email_host"], s.get("email_port", "587"),
                         s.get("email_use_tls", "1") == "1",
                         s.get("email_username", ""), s.get("email_password", ""),
                         s["email_from"], s["email_to"], level, title, detail)
        except Exception as e: print("notifier email error:", e, flush=True)
    if "slack" in channels and s.get("slack_webhook_url"):
        try: _app.send_slack(s["slack_webhook_url"], level, title, detail)
        except Exception as e: print("notifier slack error:", e, flush=True)
    if "webhook" in channels and s.get("webhook_url"):
        try: _app.send_webhook(s["webhook_url"], level, title, detail, _app._alert_host_label() or "")
        except Exception as e: print("notifier webhook error:", e, flush=True)


def _alert_name(key):
    import app as _app
    parts = key.split(":")
    return ":".join(parts[1:]) if len(parts) > 1 else key


def notify_scan():
    import app as _app
    s = _app.get_settings()
    if s.get("alerts_enabled") != "1":
        return
    if not (s.get("discord_webhook_url") or s.get("ntfy_topic")
            or (s.get("telegram_token") and s.get("telegram_chat_id"))
            or (s.get("email_host") and s.get("email_from") and s.get("email_to"))
            or s.get("slack_webhook_url")
            or s.get("webhook_url")):
        return
    rules = _app.get_notification_rules()

    # ── Docker containers: edge-trigger on crit/warn, clear on ok ─────────────
    docker = _app.HEALTH.get("docker") or {}
    if docker.get("available"):
        for ct in docker.get("containers", []):
            name = ct.get("name", "?")
            key  = f"container:{name}"
            st   = ct.get("status")
            if st == "crit":
                _app._emit(s, key, "critical", f"🔴 Container {name} {ct.get('label','')}".strip(),
                      f"{name}: {ct.get('status_text','')}", rules=rules)
            elif st == "warn":
                _app._emit(s, key, "warning", f"🟠 Container {name} {ct.get('label','')}".strip(),
                      f"{name}: {ct.get('status_text','')}", rules=rules)
            elif st == "ok":
                _app._clear(key)

    # ── systemd units: edge-trigger on failed ─────────────────────────────────
    systemd = _app.HEALTH.get("systemd") or {}
    if systemd.get("available"):
        for svc in systemd.get("services", []):
            name = svc.get("name", "?")
            key  = f"systemd:{name}"
            if svc.get("status") == "crit":
                _app._emit(s, key, "critical", f"🔴 systemd unit failed: {name}",
                      f"{name} — {svc.get('desc','')} (active={svc.get('active')}, sub={svc.get('sub')})",
                      rules=rules)
            elif svc.get("status") == "ok":
                _app._clear(key)

    # ── GPU VRAM pressure ────────────────────────────────────────────────────
    mem_total = _app.LATEST.get("mem_total") or 0
    mem_used  = _app.LATEST.get("mem_used")  or 0
    if mem_total:
        free = mem_total - mem_used
        key  = "gpu:vram_pressure"
        if free < _app.PRESSURE_MB:
            _app._emit(s, key, "warning", "🟠 GPU VRAM pressure",
                  f"Only {round(free)} MB free of {round(mem_total)} MB "
                  f"({round(100*mem_used/mem_total)}% used).", rules=rules)
        else:
            _app._clear(key)

    # ── Disks crossing the configured threshold ───────────────────────────────
    try: disk_thr = int(s.get("disk_alert_pct") or 90)
    except ValueError: disk_thr = 90
    host = _app.LATEST.get("host") or {}
    seen_disks = set()
    for dk in (host.get("disks") or []):
        mp   = dk.get("mount", "?")
        seen_disks.add(mp)
        key  = f"disk:{mp}"
        pct  = dk.get("pct", 0)
        if pct >= disk_thr:
            level = "critical" if pct >= 95 else "warning"
            _app._emit(s, key, level, f"{'🔴' if level=='critical' else '🟠'} Disk {mp} at {pct}%",
                  f"{mp}: {dk.get('used',0)} GB / {dk.get('total',0)} GB used ({pct}%).",
                  rules=rules)
        else:
            _app._clear(key)

    # ── GPU OOM events from the _app.DB (each event_ts notified at most once) ─────
    try:
        cutoff = int(time.time()) - 3600
        from backend.db.repos import system as system_repo
        with _app.LOCK:
            rows = system_repo.query_oom_events_since(cutoff, conn=_app.DB)
        for ets, svc, detail in rows:
            key = f"oom:{svc}:{ets}"
            with _app._NOTIFIER_LOCK:
                already = key in _app._NOTIFIED
                if not already:
                    _app._NOTIFIED[key] = 1
            if already:
                continue
            if _app.LEVELS["critical"] < _app.LEVELS.get(s.get("alert_min_level", "warning"), 1):
                continue
            channels = _app._apply_rules(key, "critical", rules)
            if channels is not None:
                _dispatch_to_channels(s, "critical", f"🔴 GPU OOM in {svc}", (detail or "")[:1500], channels)
            else:
                for ch, ok, err in dispatch_alert(
                        s, "critical", f"🔴 GPU OOM in {svc}", (detail or "")[:1500]):
                    if not ok: print(f"notifier {ch} error:", err, flush=True)
    except Exception as e:
        print("notify_scan oom error:", e, flush=True)

    # ── Uptime checks: per-check smart alerting (down / recovery / slow) ──────
    try:
        _app.notify_uptime(s)
    except Exception as e:
        print("notify_scan uptime error:", e, flush=True)


