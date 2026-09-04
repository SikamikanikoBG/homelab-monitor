"""backend/notify — alert dispatch and notification scan (Phase 3.3)."""
import json
import logging
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


# ── Per-card GPU alerting ─────────────────────────────────────────────────────
# What makes these "smart" rather than noisy, in one place:
#
#   1. SUSTAINED, not instantaneous. Every threshold has a duration. A card
#      touching 85 °C for two seconds mid-batch is not an incident, and a tool
#      that pages for it gets muted inside a week — after which it protects
#      nothing at all.
#   2. HYSTERESIS on clear. A condition clears a few degrees below where it
#      fires, so a card hovering exactly on the line doesn't flap between armed
#      and cleared every scan.
#   3. PER CARD, PER HOST keys. GPU 1 alerting must not suppress GPU 0, and two
#      machines are two incidents.
#   4. The body names the CAUSE. "GPU hot" is a fact; "GPU hot, fan already at
#      100%, ollama holds 21.6 GB on this card" is something a human can act on.
#   5. Power-capping is never an alert. A card at a deliberately lowered power
#      limit is doing exactly what it was configured to do.
#
# _GPU_SINCE tracks when each condition first became true. In memory only: after
# a restart a condition simply re-arms from now, which is the conservative
# direction — it can delay an alert, never invent one.
_GPU_SINCE = {}


def _sustained(key, active, need_s, now):
    """True once `active` has held continuously for `need_s`.

    Returns False (and forgets the key) the moment the condition lapses, so the
    clock always measures one unbroken run rather than a total.
    """
    if not active:
        _GPU_SINCE.pop(key, None)
        return False
    since = _GPU_SINCE.setdefault(key, now)
    return (now - since) >= need_s


def _gpu_setting_int(s, key, default):
    try:
        v = (s.get(key) or "").strip()
        return int(v) if v else default
    except (TypeError, ValueError):
        return default


def _gpu_temp_threshold(s, host):
    """The temperature threshold for `host` — its override, else the global."""
    base = _gpu_setting_int(s, "gpu_temp_alert_c", 84)
    raw = (s.get("gpu_temp_overrides") or "").strip()
    if not raw:
        return base
    try:
        over = json.loads(raw)
        if isinstance(over, dict) and host in over:
            return int(over[host])
    except (ValueError, TypeError):
        pass
    return base


def _gpu_card_driver(host, idx):
    """(service, MB) holding the most VRAM on this card, or None.

    This is what turns "GPU 1 is hot" into "GPU 1 is hot and ollama is what's
    on it" — the difference between an alert you read and one you act on.
    """
    try:
        from backend.api.gpu_cockpit import _live_services
        best = None
        for svc in _live_services(host):
            mb = (svc.get("by_card") or {}).get(str(idx))
            if mb and (best is None or mb > best[1]):
                best = (svc["service"], mb)
        return best
    except Exception:
        logging.debug("gpu fan/service lookup failed", exc_info=True)
        return None


def _fan_note(card):
    """'fan already at 100% — no cooling headroom' vs 'fan at 62%'.

    Worth its own helper because it is the single most useful sentence in a
    thermal alert: it says whether there is anything left to try.
    """
    fan = card.get("fan")
    if fan is None:
        return ""
    if fan >= 95:
        return " Fan is already at 100% — there is no cooling headroom left."
    return f" Fan is at {round(fan)}%."


def notify_gpu_cards(s, rules):
    """Per-card thermal / throttle / fan / VRAM alerting for every host."""
    import app as _app
    now = int(time.time())
    seen_keys = set()
    for host, cards, online in _app.fleet_gpu_cards():
        # A stale snapshot from an offline host says nothing about the card's
        # temperature now. Alerting on it would report a machine that may well
        # be powered off.
        if not online:
            continue
        temp_c = _gpu_temp_threshold(s, host)
        hyst = max(2, temp_c // 20)          # clear a few degrees below the trip
        for g in cards:
            idx = g.get("idx")
            if idx is None:
                continue
            label = f"{host}:GPU {idx}" if host != "local" else f"GPU {idx}"
            name = g.get("name") or "GPU"
            temp = g.get("temp")
            mask = g.get("throttle_mask") or 0

            # ── thermal throttling ──────────────────────────────────────────
            key = f"gpu:throttle:{host}:{idx}"
            seen_keys.add(key)
            thr_active = bool(mask & _app._THERMAL_BITS)
            need = _gpu_setting_int(s, "gpu_throttle_sustain_s", 120)
            if _sustained(key, thr_active, need, now):
                who = _gpu_card_driver(host, idx)
                reasons = ", ".join(g.get("throttle") or []) or "thermal"
                detail = (f"{name} on {host} is running below its rated clocks "
                          f"({reasons}), sustained for over {need // 60} min. "
                          f"Temperature {round(temp or 0)} °C.")
                if who:
                    detail += f" {who[0]} holds {round(who[1])} MB on this card."
                detail += _fan_note(g)
                _app._emit(s, key, "critical",
                           f"🔴 {label} is thermally throttling", detail, rules=rules)
            elif not thr_active:
                _app._clear(key)

            # ── running hot (not throttling yet) ────────────────────────────
            key = f"gpu:temp:{host}:{idx}"
            seen_keys.add(key)
            need = _gpu_setting_int(s, "gpu_temp_sustain_s", 180)
            hot = temp is not None and temp >= temp_c
            if _sustained(key, hot, need, now):
                who = _gpu_card_driver(host, idx)
                detail = (f"{name} on {host} has been at or above {temp_c} °C for "
                          f"over {need // 60} min (now {round(temp)} °C).")
                if who:
                    detail += f" {who[0]} holds {round(who[1])} MB on this card."
                detail += _fan_note(g)
                _app._emit(s, key, "critical", f"🔴 {label} is running hot", detail, rules=rules)
            elif temp is not None and temp < (temp_c - hyst):
                # Hysteresis: clear only once it has genuinely come down, not the
                # instant it dips a tenth of a degree under the trip point.
                _app._clear(key)

            # ── fan stall ───────────────────────────────────────────────────
            # Two guards, both learned the hard way:
            #
            # `fan is not None` — a passively cooled datacentre card reports no
            # fan at all, and treating absent as 0% would page the user about
            # hardware that has no fan to stall.
            #
            # The temperature bar is derived from the alert threshold, not a
            # fixed 50 °C. Modern cards have zero-RPM idle modes and legitimately
            # sit in the 50s with their fans completely stopped — a real fleet
            # box was doing exactly that at 53 °C during testing. By the time a
            # card is within 10 °C of its alert threshold, every zero-RPM design
            # has long since spun up, so 0% there genuinely means broken.
            key = f"gpu:fanstall:{host}:{idx}"
            seen_keys.add(key)
            fan = g.get("fan")
            stall_at = max(60, temp_c - 10)
            if s.get("gpu_fanstall_alerts", "1") == "1" and fan is not None:
                stalled = fan == 0 and (temp or 0) >= stall_at
                if _sustained(key, stalled, 120, now):
                    _app._emit(s, key, "critical", f"🔴 {label} fan has stopped",
                               f"{name} on {host} reports a fan speed of 0% while the card "
                               f"is at {round(temp or 0)} °C (at or above {stall_at} °C, where "
                               f"any zero-RPM idle mode would have spun up). A seized fan or "
                               f"an unplugged header will cook a GPU under load.", rules=rules)
                elif not stalled:
                    _app._clear(key)

            # ── per-card VRAM pressure ──────────────────────────────────────
            key = f"gpu:vram:{host}:{idx}"
            seen_keys.add(key)
            mt, mu = g.get("mem_total") or 0, g.get("mem_used") or 0
            pct = (mu / mt * 100) if mt else 0
            lim = _gpu_setting_int(s, "gpu_vram_alert_pct", 95)
            need = _gpu_setting_int(s, "gpu_vram_sustain_s", 300)
            if _sustained(key, mt and pct >= lim, need, now):
                who = _gpu_card_driver(host, idx)
                detail = (f"{name} on {host} is {round(pct)}% full "
                          f"({round(mu)} of {round(mt)} MB).")
                if who:
                    detail += f" Largest consumer: {who[0]} at {round(who[1])} MB."
                _app._emit(s, key, "warning", f"🟠 {label} VRAM is nearly full", detail, rules=rules)
            elif not mt or pct < (lim - 3):
                _app._clear(key)

            # ── idle but drawing power (opt-in, off by default) ─────────────
            idle_w = _gpu_setting_int(s, "gpu_idle_watts", 0)
            if idle_w:
                key = f"gpu:idlewatts:{host}:{idx}"
                seen_keys.add(key)
                wasteful = (g.get("util") or 0) < 5 and (g.get("power") or 0) >= idle_w
                need = _gpu_setting_int(s, "gpu_idle_sustain_s", 1800)
                if _sustained(key, wasteful, need, now):
                    _app._emit(s, key, "warning", f"🟠 {label} is idle but drawing {round(g.get('power') or 0)} W",
                               f"{name} on {host} has been idle for over {need // 60} min while "
                               f"drawing at least {idle_w} W.", rules=rules)
                elif not wasteful:
                    _app._clear(key)

    # ── a card that was reporting has stopped ───────────────────────────────
    if s.get("gpu_missing_alerts", "1") == "1":
        try:
            _notify_gpu_missing(s, rules, now)
        except Exception as e:
            print("notify gpu missing error:", e, flush=True)


# {host: {card idx: last ts it was seen}}. Remembers what each host was
# reporting, so a card disappearing is detectable at all. Only hosts observed in
# THIS process count: after a restart the baseline is whatever is present then,
# which can delay detection but can never invent a card that was never there.
_GPU_ROSTER = {}

# How long a card may stay missing before it is treated as deliberately removed
# rather than failed. Past this the alert clears and the roster forgets it —
# otherwise pulling a GPU out of a machine would leave a critical alert armed
# forever, which is the same false-alarm the cockpit's "retired" state avoids.
_GPU_RETIRE_S = 3600


def _notify_gpu_missing(s, rules, now):
    import app as _app
    for host, cards, online in _app.fleet_gpu_cards():
        if not online:
            continue          # an offline host is a host alert, not a card alert
        present = {g["idx"] for g in cards if g.get("idx") is not None}
        roster = _GPU_ROSTER.setdefault(host, {})
        for idx in present:
            key = f"gpu:missing:{host}:{idx}"
            if idx in roster and roster[idx] < now:
                _app._clear(key)                  # it's back
                _GPU_SINCE.pop(key, None)
            roster[idx] = now
        for idx in sorted(set(roster) - present):
            key = f"gpu:missing:{host}:{idx}"
            gone_for = now - roster[idx]
            if gone_for > _GPU_RETIRE_S:
                # Deliberately removed, not failed. Stop expecting it.
                _app._clear(key)
                _GPU_SINCE.pop(key, None)
                roster.pop(idx, None)
                continue
            # Sustained here too: one poll where nvidia-smi timed out under load
            # is not a missing card.
            if _sustained(key, True, 120, now):
                label = f"{host}:GPU {idx}" if host != "local" else f"GPU {idx}"
                _app._emit(s, key, "critical", f"🔴 {label} has stopped reporting",
                           f"{host} was reporting GPU {idx} until {gone_for // 60} min ago and no "
                           f"longer does. A card that falls off the bus usually means a driver "
                           f"crash (Xid) or a power/riser fault. If you removed it deliberately, "
                           f"this clears itself after an hour.", rules=rules)


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

    # ── Per-card GPU health across the whole fleet ───────────────────────────
    try:
        notify_gpu_cards(s, rules)
    except Exception as e:
        print("notify_scan gpu error:", e, flush=True)

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


