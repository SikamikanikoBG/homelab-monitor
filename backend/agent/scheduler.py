"""Scheduled automations: one tick thread, each slot fires once.

Every 20 s: expire overdue approvals, prune old audit rows (hourly), and fire
tasks whose next slot has come. A slot is claimed with an INSERT into
agent_task_fires keyed (task, slot) — whoever inserts first fires it, so two
ticks, a restart or a slow tick never fire it twice (EC-10). Slots missed while
the hub was down are skipped by default (`catch_up: skip`), or the latest one
fires once (`once`) — never one per missed slot.

Before firing: a task with a run still going is skipped (`skipped_overlap`,
EC-11), a paused agent skips (`skipped_paused`), a cloud task over its daily
spend cap skips (`skipped_budget`). A scheduled run on a local model waits up
to 30 min for a busy GPU, then skips (`skipped_gpu_busy`).
"""
import json
import threading
import time

from backend.agent import cron, events, metrics, ports, providers, state, store

TICK_S = 20
_thread = {"t": None, "stop": threading.Event(), "last_prune": 0}


class SkipRun(Exception):
    def __init__(self, outcome, reason):
        self.outcome = outcome
        super().__init__(reason)


def start():
    if _thread["t"] and _thread["t"].is_alive():
        return
    _thread["stop"].clear()
    t = threading.Thread(target=_loop, name="agent-scheduler", daemon=True)
    _thread["t"] = t
    t.start()


def stop():
    _thread["stop"].set()


def _loop():
    while not _thread["stop"].wait(TICK_S):
        try:
            tick()
        except Exception as e:                      # one bad tick must not stop the scheduler
            print("agent scheduler tick failed: %r" % e, flush=True)


def compute_next(task, after_utc, last_slot_local=None):
    nxt = cron.next_after(task["cron"], task["tz"], after_utc, last_slot_local)
    return nxt if nxt else (None, None)


def _missed_latest(task, now):
    """The latest slot at or before `now`, walking from next_fire."""
    slot, local = task["next_fire"], task.get("last_slot_local")
    last = (slot, None)
    t = slot - 60
    while True:
        nxt = cron.next_after(task["cron"], task["tz"], t, local)
        if not nxt or nxt[0] > now:
            return last
        last = nxt
        t, local = nxt[0], nxt[1]


def tick(now=None):
    now = int(now or time.time())
    if not state.runtime_enabled():
        return []
    from backend.agent import approvals
    approvals.expire(now)
    if now - _thread["last_prune"] >= 3600:
        _thread["last_prune"] = now
        try:
            store.prune(now - int(ports.retention_secs()))
        except Exception as e:
            print("agent retention failed: %s" % e, flush=True)
    fired = []
    for task in store.task_list():
        if not task["enabled"] or not task.get("next_fire") or task["next_fire"] > now:
            continue
        slot = task["next_fire"]
        late = now - slot
        local = None
        if late > 2 * TICK_S + 60:
            if task.get("catch_up") == "once":
                slot, local = _missed_latest(task, now)
            else:
                _record(task, slot, "skipped_missed")
                _advance(task, now, None)
                continue
        outcome = _try_fire(task, slot, "schedule")
        fired.append((task["id"], slot, outcome))
        _advance(task, max(now, slot), local)
    return fired


def _record(task, slot, outcome):
    if store.fire_claim(task["id"], slot, outcome):
        metrics.fire(outcome)
        events.publish("task", task_id=task["id"], outcome=outcome, scheduled_for=slot)


def _advance(task, after, last_local):
    nxt, local = compute_next(task, after, last_local)
    store.task_update(task["id"], next_fire=nxt, last_slot_local=local or task.get("last_slot_local"))


def _skip_reason(task):
    if state.paused():
        return "skipped_paused"
    if store.task_active_run(task["id"]):
        return "skipped_overlap"
    prov = store.provider_get(task.get("provider_id")) if task.get("provider_id") else None
    if prov and not providers.is_local(prov):
        day = store.now() - 86400
        if store.cost_since(day, task_id=task["id"]) >= state.get_float("cost_cap_task_day") * 1e6 or \
                store.cost_since(day) >= state.get_float("cost_cap_day") * 1e6:
            return "skipped_budget"
    return None


def _try_fire(task, slot, trigger, mode=None):
    if not store.fire_claim(task["id"], slot, "fired"):
        return "already_fired"
    why = _skip_reason(task)
    if why:
        store.fire_set(task["id"], slot, outcome=why)
        metrics.fire(why)
        events.publish("task", task_id=task["id"], outcome=why, scheduled_for=slot)
        return why
    run_id = start_run(task, slot, trigger, mode)
    store.fire_set(task["id"], slot, run_id=run_id)
    metrics.fire("fired")
    return "fired"


def start_run(task, slot, trigger, mode=None):
    cid = store.conversation_create(task["name"], origin="task", task_id=task["id"])
    provider_id = task.get("provider_id") or state.get("default_provider_id") or None
    model = task.get("model") or state.get("default_model") or None
    store.message_add(cid, "user", "Scheduled run of \"%s\"." % task["name"])
    rid = store.run_create(cid, trigger, False, mode or task["mode"], provider_id=provider_id, model=model,
                           task_id=task["id"], scheduled_for=slot, hosts=task.get("hosts"), actor="scheduler")
    events.publish("run", run_id=rid, conversation_id=cid, status="queued", task_id=task["id"])
    from backend.agent import runs
    runs.manager().submit("scheduled", rid, scheduled=True)
    return rid


def run_now(task, dry_run=False):
    """US-9: Run now. A dry run is Report mode, labelled as such."""
    slot = int(time.time())
    while not store.fire_claim(task["id"], slot, "fired"):
        slot += 1
    run_id = start_run(task, slot, "dry_run" if dry_run else "manual", "report" if dry_run else None)
    store.fire_set(task["id"], slot, run_id=run_id)
    return run_id


# ── delivery ─────────────────────────────────────────────────────────────────

def deliver(run):
    task = store.task_get(run["task_id"]) if run.get("task_id") else None
    if not task:
        return
    channels = (task.get("delivery") or {}).get("channels") or []
    out_channels = [c for c in channels if c != "inapp"]
    if not out_channels:
        return
    msgs = [m for m in store.message_list(run["conversation_id"]) if m["role"] == "assistant" and m.get("content")]
    summary = (msgs[-1]["content"] if msgs else run.get("status_reason") or run["status"])[:1500]
    pending = [a for a in store.approval_list(run_id=run["id"]) if a["status"] == "pending"]
    level = "warning" if run["status"] != "done" or pending else "info"
    title = "Agent: %s — %s" % (task["name"], run["status"].replace("_", " "))
    detail = summary + ("\n\n%d action(s) wait for your approval." % len(pending) if pending else "") + \
        "\nDetails: /#agent/run/%s" % run["id"]
    try:
        if "all" in out_channels:
            ports.dispatch_alert(level, title, detail)
        else:
            ports.dispatch_to_channels(level, title, detail, out_channels)
    except Exception as e:
        print("agent: delivery failed: %s" % e, flush=True)


# ── GPU courtesy ─────────────────────────────────────────────────────────────

def _gpu_busy(base_url):
    """True/False when we know the GPU behind a local model endpoint, else None."""
    want = providers.url_host(base_url)
    host = None
    if want in ("localhost", "127.0.0.1") or any((s.get("ip") or "") == want for s in ports.ai_servers()):
        host = "local"
    else:
        for h in ports.registered_hosts():
            if (ports.ssh_target_host(h["name"]) or "").lower() == want:
                host = h["name"]
    if host is None:
        return None
    gpus = (ports.host_data(host).get("gpus") or ports.host_block(host).get("gpus") or [])
    if not gpus:
        return None
    util_max = float(state.get("gpu_busy_util_pct") or 85)
    min_free = float(state.get("gpu_min_free_vram_mb") or 1024)
    for g in gpus:
        util = g.get("util") or 0
        free = (g.get("mem_total") or 0) - (g.get("mem_used") or 0)
        if util < util_max and free >= min_free:
            return False
    return True


def gpu_courtesy_wait(ctx, ctl, sleep_s=60):
    """Before the first model call of a scheduled run on a local model."""
    if ctx.watch.model_calls or not ctx.provider or not providers.is_local(ctx.provider):
        return
    deadline = time.monotonic() + float(state.get("gpu_defer_max_min") or 30) * 60
    while _gpu_busy(ctx.base_url):
        if time.monotonic() >= deadline:
            from backend.agent import loop
            if ctx.run.get("task_id") and ctx.run.get("scheduled_for"):
                store.fire_set(ctx.run["task_id"], ctx.run["scheduled_for"], outcome="skipped_gpu_busy")
            loop.finish(ctx.run["id"], "cancelled", "skipped_gpu_busy",
                        "The GPU stayed busy for %s minutes, so this run was skipped." % state.get("gpu_defer_max_min"))
            raise loop.Parked()
        if ctl.cancel.wait(sleep_s):
            return


# ── validation for the task API ──────────────────────────────────────────────

def validate_task(t):
    """{field: message} for anything wrong with a task definition."""
    errs = {}
    try:
        c = cron.Cron(t.get("cron"))
    except cron.CronError as e:
        errs["cron"] = str(e)
        c = None
    try:
        cron.zone(t.get("tz"))
    except cron.CronError as e:
        errs["tz"] = str(e)
    if t.get("mode") not in ("report", "propose", "act"):
        errs["mode"] = "mode must be report, propose or act"
    if not (t.get("name") or "").strip():
        errs["name"] = "give it a name"
    if not (t.get("prompt") or "").strip():
        errs["prompt"] = "say what it should do"
    if t.get("catch_up", "skip") not in ("skip", "once"):
        errs["catch_up"] = "catch_up must be skip or once"
    hosts = t.get("hosts") or []
    known = set(ports.host_names())
    bad = [h for h in hosts if h not in known]
    if bad:
        errs["hosts"] = "unknown host(s): %s" % ", ".join(bad)
    prov = store.provider_get(t.get("provider_id")) if t.get("provider_id") else None
    if t.get("provider_id") and not prov:
        errs["provider_id"] = "unknown provider"
    if prov and not providers.is_local(prov) and c is not None:
        min_s = int(state.get("cloud_min_interval_s") or 3600)
        if c.min_interval_s() < min_s:
            errs["cron"] = "a cloud model can run at most once every %d minutes" % (min_s // 60)
    from backend.agent import catalog
    for aid in t.get("actions") or []:
        if not catalog.get(aid):
            errs["actions"] = "unknown action %s" % aid
    return errs
