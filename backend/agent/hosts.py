"""Keep the agent's per-host rows in step with the host registry.

app.rename_host / app.delete_host call these inside their own transaction (on
their connection, under their lock), so a host can't be renamed while its
agent policy stays behind — the old rows would otherwise sit there and be
inherited by the next host that takes the name (alpha finding #4).

A rename moves the host's mode, action rules and protections to the new name
and refuses its pending approvals (the agent has to propose them again — an
approval is never silently re-targeted); a chat run parked on one of them is
cancelled so its conversation isn't stuck (#8). A delete drops the rows, so a
host re-added under the same name lands on the safe default (Passive). Audit
rows keep the old name: history is history.

Installs that never enabled the agent have no agent tables; nothing is created
for them.
"""
from backend.agent import ports, schema


def renamed(old, new, conn):
    if not schema.exists(conn):
        return []
    from backend.agent import store
    refused = store.host_renamed(old, new, conn=conn)
    store.admin_log("host", old, {"renamed_to": new, "approvals_refused": refused}, conn=conn)
    return refused


def deleted(name, conn):
    if not schema.exists(conn):
        return []
    from backend.agent import store
    refused = store.host_deleted(name, conn=conn)
    store.admin_log("host", name, {"deleted": True, "approvals_refused": refused}, conn=conn)
    return refused


def prune_orphans():
    """Drop policy rows for hosts that no longer exist (left by older builds or
    a failed rename). Run at boot; returns how many rows went."""
    a = ports._app()
    with a.LOCK:
        if not schema.exists(a.DB):
            return 0
    from backend.agent import store
    return store.prune_policy_orphans(ports.host_names())
