"""Prometheus series for the agent. Registered only when the agent starts, so
an install that never enables it keeps a byte-identical /metrics page. No host
names in labels (they'd multiply the series)."""
import threading

_lock = threading.Lock()
_m = {}


def register():
    with _lock:
        if _m:
            return True
        try:
            from prometheus_client import Counter, Gauge, Histogram
        except ImportError:
            return False
        try:
            _m["runs"] = Counter("homelab_agent_runs", "Agent runs finished", ["trigger", "status"])
            _m["run_seconds"] = Histogram("homelab_agent_run_seconds", "Agent run duration",
                                          buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800))
            _m["actions"] = Counter("homelab_agent_actions", "Agent action decisions and outcomes",
                                    ["action", "decision", "outcome"])
            _m["tokens"] = Counter("homelab_agent_tokens", "Model tokens", ["direction"])
            _m["cost"] = Counter("homelab_agent_cost_micros", "Model cost in millionths of the Costs currency")
            _m["provider_errors"] = Counter("homelab_agent_provider_errors", "Model provider errors", ["kind"])
            _m["fires"] = Counter("homelab_agent_task_fires", "Scheduled task slots", ["outcome"])
            _m["pending"] = Gauge("homelab_agent_pending_approvals", "Approvals waiting for a decision")
        except ValueError:                          # already registered (re-import); keep what exists
            pass
        return True


def _get(name):
    with _lock:
        return _m.get(name)


def run_finished(run):
    c, h = _get("runs"), _get("run_seconds")
    if c and run:
        c.labels(trigger=run.get("trigger") or "", status=run.get("status") or "").inc()
    if h and run and run.get("started_at") and run.get("ended_at"):
        h.observe(max(0, run["ended_at"] - run["started_at"]))
    pending()


def action(action_id, decision, outcome):
    c = _get("actions")
    if c:
        c.labels(action=action_id, decision=decision, outcome=outcome or "").inc()


def tokens(provider_id, prompt, completion, cost):
    c = _get("tokens")
    if c:
        c.labels(direction="in").inc(prompt or 0)
        c.labels(direction="out").inc(completion or 0)
    k = _get("cost")
    if k and cost:
        k.inc(cost)


def provider_error(provider_id, kind):
    c = _get("provider_errors")
    if c:
        c.labels(kind=kind).inc()


def fire(outcome):
    c = _get("fires")
    if c:
        c.labels(outcome=outcome).inc()


def pending():
    g = _get("pending")
    if g:
        from backend.agent import store
        try:
            g.set(store.counts()["pending_approvals"])
        except Exception as e:
            print("agent metrics: %s" % e, flush=True)
