"""What the agent needs around a self-update or a database restore.

Both are refused with 409 while a run is running or an approved action is
executing — a restart mid-action leaves a host in a state nobody chose. Pending
approvals don't block (they are durable and carry the catalogue version, so an
approval from before an update is re-checked against the new catalogue).
`force` cancels the runs first and goes ahead.

After a restore the agent tables come from the backup: every pending approval
in it is expired (it was decided about in a different world) and the event is
logged to stdout and to the admin log.
"""
import json

from backend.agent import ports, schema


def _has_tables():
    a = ports._app()
    with a.LOCK:
        return schema.exists(a.DB)


def busy():
    """A short reason when the agent is mid-action, else None."""
    if not _has_tables():
        return None
    from backend.agent import store
    running = store.counts()["running"]                    # queued + running, as the status shows (alpha #33)
    executing = store.approvals_executing()
    if not running and not executing:
        return None
    return "%d agent run(s) in progress, %d approved action(s) executing" % (running, executing)


def cancel_all(reason):
    if not _has_tables():
        return 0
    from backend.agent import runs
    return runs.manager().cancel_all(reason)


def refuse_payload(what):
    reason = busy()
    if not reason:
        return None
    return {"ok": False, "error": "agent_busy",
            "message": "The AI agent is in the middle of something (%s). Wait for it, stop it in the "
                       "Agent tab, or send force=true to cancel it and %s anyway." % (reason, what)}


def after_restore():
    """Called once the restored database is open."""
    from backend.auth import sessions, store as auth_store
    auth_store.reset()
    schema.reset()
    sessions.invalidate()
    print(json.dumps({"event": "agent_audit", "kind": "restore",
                      "message": "audit history replaced by restore"}), flush=True)
    if not _has_tables():
        return
    from backend.agent import store
    expired = store.approvals_expire_all("restore")
    store.admin_log("restore", "database", {"message": "audit history replaced by restore",
                                            "approvals_expired": len(expired)})
