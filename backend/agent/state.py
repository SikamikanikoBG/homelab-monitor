"""Is the agent on, and its settings.

Three switches, all must be on before anything runs:
1. `AGENT_ENABLED=1` in the environment (the hard switch; unset = every
   /api/agent/* route is a 404, no threads, no UI — the default);
2. an admin password (the agent never runs on an open dashboard);
3. the owner's "Enable agent" toggle in the Agent tab (`enabled` below).

Settings live in `agent_settings` (key/value), never in the dashboard's own
`settings` table, which is round-tripped to the browser wholesale.
"""
import json
import os
import time

from backend.agent import ports

DEFAULTS = {
    "enabled": "0",
    "paused": "0",
    "default_provider_id": "",
    "default_model": "",
    "tz": "",
    "approval_ttl_h": "24",
    "chat_card_ttl_min": "30",
    "budget_chat": json.dumps({"max_steps": 12, "max_seconds": 300, "max_tokens": 120000}),
    "budget_scheduled": json.dumps({"max_steps": 25, "max_seconds": 600, "max_tokens": 200000}),
    # Cloud spend caps, in the Costs tab's currency. Local models cost 0.
    "cost_cap_run": "0.50",
    "cost_cap_task_day": "1.00",
    "cost_cap_day": "2.00",
    # Unattended Allow turns into Ask after this many runs of one action on one
    # target in 24 h.
    "rate_limit_per_day": "3",
    # GPU courtesy for scheduled runs on a local model.
    "gpu_busy_util_pct": "85",
    "gpu_min_free_vram_mb": "1024",
    "gpu_defer_max_min": "30",
    "cloud_min_interval_s": "3600",
    "max_output_tokens": "2048",
    # {host: ["/var/log/myapp", ...]} — the only paths the file probes may look at.
    "watched_paths": "{}",
}


def env_allowed():
    return (os.environ.get("AGENT_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")


def password_set():
    from backend.auth import sessions
    return sessions.password_set()


def get_all():
    out = dict(DEFAULTS)
    with ports.db() as conn:
        for k, v in conn.execute("SELECT key, value FROM agent_settings"):
            if k in DEFAULTS:
                out[k] = v
    return out


def get(key):
    return get_all().get(key, DEFAULTS.get(key))


def get_json(key):
    try:
        return json.loads(get(key) or "{}")
    except ValueError:
        return json.loads(DEFAULTS[key])


def get_float(key):
    try:
        return float(get(key))
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


def put(updates, actor=None):
    clean = {k: (v if isinstance(v, str) else json.dumps(v)) for k, v in updates.items() if k in DEFAULTS}
    if not clean:
        return {}
    with ports.db() as conn:
        conn.executemany("INSERT INTO agent_settings(key,value) VALUES(?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", list(clean.items()))
        conn.execute("INSERT INTO agent_admin_log(ts,actor,kind,subject,detail_json) VALUES(?,?,?,?,?)",
                     (int(time.time()), actor, "settings", ",".join(sorted(clean)),
                      json.dumps(clean, sort_keys=True)))
        conn.commit()
    return clean


def runtime_enabled():
    """Env allows it, a password exists, and the owner switched it on."""
    if not env_allowed() or not password_set():
        return False
    return get("enabled") == "1"


def paused():
    return get("paused") == "1"


def tz():
    return get("tz") or ports.hub_tz()


def max_runs():
    try:
        return max(1, int(os.environ.get("AGENT_MAX_RUNS", "2")))
    except ValueError:
        return 2
