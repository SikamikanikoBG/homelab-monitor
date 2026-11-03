"""The audit trail's second copy, and its export.

Every action intent and result is printed to stdout as one JSON line, so
`docker logs homelab-monitor` keeps a copy outside the database (a restore or
a hand-edited DB can't rewrite what was already logged). Secrets never get
here: actions take no secret parameters, and provider keys never enter a run.

Export (US-11): runs and their steps as JSON or CSV, filtered like the Activity
list. The export is admin-only (it lists hosts, arguments and excerpts).
"""
import csv
import io
import json
import time

from backend.agent import store


def _line(rec):
    rec = dict(rec, event="agent_audit", ts=int(time.time()))
    print(json.dumps(rec, sort_keys=True, ensure_ascii=False), flush=True)


def action_line(phase, run_id, host, action, params, decision=None, ok=None, approval_id=None, actor=None,
                detail=None):
    _line({"phase": phase, "run_id": run_id, "host": host, "action": action, "params": params,
           "decision": decision, "ok": ok, "approval_id": approval_id, "actor": actor,
           "detail": (detail or "")[:300] or None})


def approval_line(approval_id, status, actor):
    _line({"phase": "approval", "approval_id": approval_id, "status": status, "actor": actor})


EXPORT_STEP_COLS = ("run_id", "seq", "ts", "kind", "tool", "host", "target", "args_json", "ok", "result_bytes",
                    "result_excerpt", "decision", "decision_reason", "approval_id", "actor", "duration_ms",
                    "tokens_in", "tokens_out")
EXPORT_RUN_COLS = ("id", "trigger", "task_id", "attended", "mode", "provider_id", "model", "status",
                   "status_reason", "steps", "prompt_tokens", "completion_tokens", "cost_micros", "actor",
                   "created_at", "started_at", "ended_at")


def export(fmt="json", **filters):
    runs = store.run_list(limit=int(filters.pop("limit", 5000)), **filters)
    steps = store.steps_for_export(run_ids=[r["id"] for r in runs])
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(("record",) + EXPORT_RUN_COLS + EXPORT_STEP_COLS)
        by_run = {}
        for s in steps:
            by_run.setdefault(s["run_id"], []).append(s)
        for r in runs:
            w.writerow(("run",) + tuple(r.get(c) for c in EXPORT_RUN_COLS) + ("",) * len(EXPORT_STEP_COLS))
            for s in by_run.get(r["id"], []):
                w.writerow(("step",) + ("",) * len(EXPORT_RUN_COLS) + tuple(s.get(c) for c in EXPORT_STEP_COLS))
        return buf.getvalue()
    return json.dumps({"exported_at": int(time.time()),
                       "runs": [{c: r.get(c) for c in EXPORT_RUN_COLS} for r in runs],
                       "steps": [{c: s.get(c) for c in EXPORT_STEP_COLS} for s in steps]},
                      indent=1, ensure_ascii=False)
