"""Who may do what: one function, evaluated twice.

`decide()` runs when the model proposes an action and again right before it
executes (after an approval too, EC-8), always on the server. Inputs, in order:

  host mode Off            → deny  (even reads)
  bad params / not on host → deny
  probe (risk none)        → allow (read-only, Passive is enough)
  ENABLE_CONTROLS=0        → deny  (global kill switch)
  protected target         → deny  (the hub itself, the model's own server, user marks)
  host mode Passive        → deny
  not in this task's list  → deny
  run mode Report          → deny
  rule Deny                → deny  (every rule is Deny until the owner ticks it)
  approved by a human      → allow
  run mode Propose         → ask
  rule Ask                 → ask
  root (sudo) action       → ask   (never runs without a human in v1)
  attended chat            → allow
  unattended: low + idempotent + under the rate limit → allow; medium only
  when unlocked (and under the limit); high never     → otherwise ask

Unattended can only make things stricter — the opposite of the rule that let
an unattended Jarvis2 schedule reboot two machines on 2026-10-15.
"""
import time
from dataclasses import dataclass

from backend.agent import catalog, ports, state, store

MODES = ("off", "passive", "active")
RULES = ("deny", "ask", "allow")
DEFAULT_MODE = "passive"


@dataclass
class Decision:
    verdict: str          # allow | ask | deny
    reason: str           # machine code (see HINTS)

    def as_dict(self):
        return {"verdict": self.verdict, "reason": self.reason}


def decide_pure(*, risk, idempotent, sudo, unattended_cap, mode, controls, protected, params_ok, supported,
                in_task, run_mode, rule, approved, attended, unlocked, rate_ok):
    """The truth table, with no I/O. Every argument is a plain value."""
    if mode == "off":
        return Decision("deny", "host_off")
    if not params_ok:
        return Decision("deny", "bad_params")
    if not supported:
        return Decision("deny", "unsupported_on_host")
    if risk == "none":
        return Decision("allow", "probe")
    if not controls:
        return Decision("deny", "controls_disabled")
    if protected:
        return Decision("deny", "protected")
    if mode != "active":
        return Decision("deny", "host_passive")
    if not in_task:
        return Decision("deny", "not_in_task")
    if run_mode == "report":
        return Decision("deny", "report_only")
    if rule not in ("ask", "allow"):
        return Decision("deny", "rule_deny")
    if approved:
        return Decision("allow", "approved")
    if run_mode == "propose":
        return Decision("ask", "propose_mode")
    if rule == "ask":
        return Decision("ask", "rule_ask")
    if sudo:
        return Decision("ask", "needs_root")
    if attended:
        return Decision("allow", "rule_allow")
    if unattended_cap == "never" or risk == "high":
        return Decision("ask", "unattended_needs_approval")
    if not rate_ok:
        return Decision("ask", "rate_limited")
    if risk == "low" and idempotent:
        return Decision("allow", "low_unattended")
    if risk == "medium" and unlocked:
        return Decision("allow", "unlocked")
    return Decision("ask", "unattended_needs_approval")


# ── facts about hosts ────────────────────────────────────────────────────────

def os_family(host):
    if host == "local":
        return "linux"
    fam = ((ports.host_block(host).get("os") or {}).get("family") or "").lower()
    return fam or "linux"


def capabilities(host):
    """What a host can do, from the data the monitor already collects."""
    caps = set()
    h = ports.host_health(host)
    blk = ports.host_block(host)
    fam = os_family(host)
    caps.add("windows" if fam == "windows" else "linux")
    docker = h.get("docker") if host == "local" else (blk.get("docker") or h.get("docker"))
    if (docker or {}).get("available"):
        caps.add("docker")
    sysd = h.get("systemd") if host == "local" else (blk.get("systemd") or h.get("systemd"))
    if (sysd or {}).get("available"):
        caps.update(("systemd", "services"))
    if fam == "windows" and (blk.get("services") or (sysd or {}).get("services")):
        caps.add("services")
    providers_seen = {(m.get("provider") or "").lower() for m in (h.get("model_catalog") or [])}
    if host == "local":
        providers_seen |= {(s.get("provider") or "").lower() for s in ports.ai_servers()}
    if "ollama" in providers_seen:
        caps.add("ollama")
    osinfo = blk.get("os") or {}
    ids = " ".join(str(osinfo.get(k) or "") for k in ("id", "id_like")).lower().split()
    if any(i in ("debian", "ubuntu") for i in ids):
        caps.add("apt")
    return caps


def requires_met(a, host):
    return set(a.get("requires") or []) <= capabilities(host)


# ── protected targets ────────────────────────────────────────────────────────

def _model_hosts(model_base_url):
    """(host, server-name) pairs serving the model endpoint this run talks to."""
    from backend.agent import providers
    want = providers.url_host(model_base_url)
    if not want:
        return set()
    out = set()
    for s in ports.ai_servers():
        if (s.get("ip") or "").lower() == want or want in ("localhost", "127.0.0.1"):
            out.add(("local", s.get("name")))
    for h in ports.registered_hosts():
        if (ports.ssh_target_host(h["name"]) or "").lower() == want:
            for m in ports.loaded_models():
                if m.get("host") == h["name"] and m.get("service"):
                    out.add((h["name"], m["service"]))
    return out


def protected_reason(host, kind, name, model_base_url=None):
    """Why a target may not be touched by the agent, or None."""
    if kind in ("container", "service", "model") and name:
        if host == "local" and kind == "container" and name == ports.self_container_name():
            return "this is the monitor's own container"
        for h, server in _model_hosts(model_base_url):
            if h == host and server == name:
                return "it serves the model this run is using"
        if kind == "service":
            target = None
            for r in ports.registered_hosts():
                if r["name"] == host:
                    target = r.get("ssh_target") or ""
            port = int(target.rsplit(":", 1)[1]) if target and ":" in target.split("@")[-1] else 22
            for svc in ((ports.host_block(host).get("systemd") or {}).get("services") or []):
                if svc.get("name") == name and port in (svc.get("ports") or []):
                    return "the hub reaches this host through it (SSH)"
    for p in store.protected_list():
        if p["host"] == host and (p["kind"] == kind or p["kind"] == "*") and p["name"] == name:
            return p.get("reason") or "marked protected by you"
    return None


# ── the policy, with its inputs looked up ────────────────────────────────────

def rule_for(host, action_id, rules=None):
    r = (rules if rules is not None else store.action_rules()).get(host, {}).get(action_id)
    return (r or {}).get("rule", "deny"), bool((r or {}).get("unattended_unlocked"))


def mode_for(host, modes=None):
    return (modes if modes is not None else store.host_modes()).get(host, DEFAULT_MODE)


def decide(host, action_id, params, *, attended, run_mode="act", task_actions=None, approved=False,
           model_base_url=None, now=None):
    a = catalog.get(action_id)
    if a is None:
        return Decision("deny", "unknown_action")
    mode = mode_for(host)
    if host not in ports.host_names():
        return Decision("deny", "unknown_host")
    if mode != "off" and host != "local" and not ports.host_block(host):
        # Before its first poll after a hub restart we don't know the host's OS
        # or tools yet; say so instead of "not available here" (alpha #27).
        return Decision("deny", "host_not_ready")
    errs = catalog.check_params(a, params)
    fam = os_family(host)
    sup = catalog.supported(a, host, fam) and requires_met(a, host)
    kind, name = catalog.target_of(a, params if not errs else {})
    prot = protected_reason(host, kind, name, model_base_url) if a["risk"] != "none" else None
    rule, unlocked = rule_for(host, action_id)
    t = now or int(time.time())
    try:
        limit = int(state.get("rate_limit_per_day"))
    except (TypeError, ValueError):
        limit = 3
    rate_ok = store.action_count(host, action_id, name, t - 86400) < limit
    return decide_pure(risk=a["risk"], idempotent=bool(a.get("idempotent")), sudo=bool(a.get("sudo")),
                       unattended_cap=a.get("unattended", "policy"), mode=mode, controls=ports.controls_enabled(),
                       protected=bool(prot), params_ok=not errs, supported=sup,
                       in_task=task_actions is None or action_id in task_actions, run_mode=run_mode, rule=rule,
                       approved=approved, attended=attended, unlocked=unlocked, rate_ok=rate_ok)


# ── what the owner reads when something is refused (from the reason code) ──

HINTS = {
    "host_off": "{host} is Off for the agent, so it can't even look at it. Set {host} to Passive or Active in Agent policy.",
    "bad_params": "The request had invalid parameters.",
    "unsupported_on_host": "\"{title}\" isn't available on {host} (wrong OS, or the tool it needs isn't there).",
    "unknown_host": "There is no host called {host}.",
    "host_not_ready": "{host} hasn't reported in since the hub started, so I can't check it yet. Try again in a minute.",
    "unknown_action": "That action isn't in the catalogue.",
    "controls_disabled": "Controls are off for the whole dashboard (ENABLE_CONTROLS=0), so every action is refused.",
    "protected": "{host} → {target} is protected for the agent ({why}). Unmark it in Agent policy if you really want this.",
    "host_passive": "{host} is Passive, so the agent can only read it. To allow this, set {host} to Active and \"{title}\" to Ask or Allow in Agent policy.",
    "not_in_task": "This automation isn't set up to run \"{title}\". Edit the automation and tick \"{title}\" under Actions.",
    "report_only": "This run is in Report mode: it only reports. Switch the automation to Propose or Act to get actions.",
    "rule_deny": "\"{title}\" is set to Deny on {host}. Set it to Ask or Allow in Agent policy.",
    "approved": "Approved.",
    "propose_mode": "This automation proposes actions for approval instead of running them.",
    "rule_ask": "\"{title}\" on {host} waits for your approval.",
    "needs_root": "\"{title}\" needs root, so it always waits for your approval.",
    "rule_allow": "Allowed by your policy.",
    "low_unattended": "Low-risk and allowed unattended.",
    "unlocked": "Allowed unattended (you unlocked it for {host}).",
    "rate_limited": "\"{title}\" already ran on this target several times in the last 24 hours, so it waits for approval.",
    "unattended_needs_approval": "Unattended runs never do \"{title}\" on their own; it waits for your approval.",
    "probe": "Read-only check.",
}


def hint(reason, host, action_id, params=None, why=None):
    a = catalog.get(action_id) or {"title": action_id}
    _, target = catalog.target_of(a, params or {}) if catalog.get(action_id) else ("", "")
    return HINTS.get(reason, reason).format(host=host, title=a["title"], target=target or host, why=why or "")
