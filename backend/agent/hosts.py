"""Keep the agent's per-host rows in step with the host registry.

A rename moves the host's mode, action rules and protections to the new name
and refuses its pending approvals (the agent has to propose them again — an
approval is never silently re-targeted). A delete drops the rows, so a host
re-added under the same name lands on the safe default (Passive). Audit rows
keep the old name: history is history.

Installs that never enabled the agent have no agent tables; nothing is created
here for them.
"""
from backend.agent import ports, schema


def _tables_exist():
    with ports._app().LOCK:
        return schema.exists(ports.db_handle())


def renamed(old, new):
    if not _tables_exist():
        return []
    from backend.agent import store
    refused = store.host_renamed(old, new)
    store.admin_log("host", old, {"renamed_to": new, "approvals_refused": refused})
    return refused


def deleted(name):
    if not _tables_exist():
        return []
    from backend.agent import store
    refused = store.host_deleted(name)
    store.admin_log("host", name, {"deleted": True, "approvals_refused": refused})
    return refused
