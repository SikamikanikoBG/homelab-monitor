"""Start and stop the agent's background work.

Nothing here runs unless the agent is fully on (see state.py): a default
install starts no thread, creates no table and registers no metric.

Starting: mark runs the hub was in the middle of as `interrupted` (their
actions are never retried), expire overdue approvals, then start the worker
pool and the scheduler tick. Runs parked on an approval are left alone — they
are durable and resume when someone decides.
"""
import threading

from backend.agent import ports, state

_lock = threading.Lock()
_started = {"on": False}


def recover():
    from backend.agent import store, audit
    n = 0
    for r in store.runs_with_status(("queued", "running")):
        if store.run_set_status_if(r["id"], ("queued", "running"), "interrupted", "hub restarted"):
            store.step_append(r["id"], "note", result_excerpt="The hub restarted during this run; "
                              "anything it was doing was not retried.")
            n += 1
    for a in store.approvals_expire():
        audit.approval_line(a["id"], "expired", None)
    with ports.db() as c:
        # An approval that was executing when the hub stopped is reported, not re-run.
        rows = c.execute("SELECT id FROM agent_approvals WHERE status='executing'").fetchall()
        c.execute("UPDATE agent_approvals SET status='failed', result_excerpt='The hub restarted while this "
                  "action was running; check the host.' WHERE status='executing'")
        c.commit()
    return n + len(rows)


def start_if_enabled():
    if not state.runtime_enabled():
        return False
    with _lock:
        if _started["on"]:
            return True
        _started["on"] = True
    from backend.agent import metrics, runs, scheduler
    recover()
    metrics.register()
    runs.manager().start()
    scheduler.start()
    print("agent: started (%d workers)" % state.max_runs(), flush=True)
    return True


def on_disabled():
    """The owner switched the agent off: pending approvals are cancelled,
    queued and running runs stop, schedules stop. Keys and the audit stay."""
    from backend.agent import store, runs, scheduler
    if not state.env_allowed():
        return
    for a in store.approvals_expire_all("agent_disabled"):
        store.run_set_status_if(a["run_id"], ("waiting_approval",), "cancelled", "agent switched off")
    runs.manager().cancel_all("agent switched off")
    scheduler.stop()
    with _lock:
        _started["on"] = False


def started():
    return _started["on"]
