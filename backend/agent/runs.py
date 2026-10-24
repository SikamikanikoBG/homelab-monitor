"""The run queue: a small worker pool with chat ahead of schedules.

AGENT_MAX_RUNS workers (default 2) take jobs from a priority queue — chat and
approval resumes first, scheduled runs after — so a burst of automations can't
saturate one local GPU or the dashboard's own threads, and chat stays
responsive (EC-33). A run waiting for approval holds no thread (it is parked in
the database). Stop sets the run's cancel event and closes the model's socket;
an action already running over SSH is allowed to finish and is recorded
(killing ssh mid-`docker` leaves worse state — and the remote command keeps
running anyway).

Tests switch to inline mode: a submitted job runs in the calling thread.
"""
import itertools
import queue
import threading

from backend.agent import events, state, store

PRIO = {"run": 0, "resume": 0, "approval": 0, "scheduled": 1}


class Control:
    def __init__(self):
        self.cancel = threading.Event()
        self.adapter = None

    def stop(self):
        self.cancel.set()
        a = self.adapter
        if a is not None:
            try:
                a.abort()
            except Exception as e:                  # aborting a finished stream can raise; stopping anyway
                print("agent: abort error: %s" % e, flush=True)


class Manager:
    def __init__(self):
        self.q = queue.PriorityQueue()
        self.seq = itertools.count()
        self.controls = {}
        self.lock = threading.Lock()
        self.workers = []
        self.stopping = threading.Event()
        self.inline = False

    def control(self, run_id):
        with self.lock:
            return self.controls.setdefault(run_id, Control())

    def release(self, run_id):
        with self.lock:
            self.controls.pop(run_id, None)

    def start(self):
        with self.lock:
            alive = [w for w in self.workers if w.is_alive()]
            self.workers = alive
            self.stopping.clear()
            for i in range(len(alive), state.max_runs()):
                t = threading.Thread(target=self._work, name="agent-worker-%d" % i, daemon=True)
                t.start()
                self.workers.append(t)

    def stop(self):
        self.stopping.set()
        for _ in self.workers:
            self.q.put((99, next(self.seq), ("stop", None)))

    def submit(self, kind, ref, scheduled=False):
        prio = PRIO["scheduled"] if scheduled else PRIO.get(kind, 0)
        if self.inline:
            self._dispatch(kind, ref)
            return
        self.q.put((prio, next(self.seq), (kind, ref)))

    def _work(self):
        while not self.stopping.is_set():
            _, _, (kind, ref) = self.q.get()
            if kind == "stop":
                return
            try:
                self._dispatch(kind, ref)
            except Exception as e:                  # never let one bad job kill a worker
                print("agent worker: %s %s failed: %r" % (kind, ref, e), flush=True)
                if kind in ("run", "resume", "scheduled"):
                    from backend.agent import loop
                    loop.finish(ref, "failed", "internal error: %s" % str(e)[:200])

    def _dispatch(self, kind, ref):
        from backend.agent import approvals, loop
        if kind in ("run", "scheduled"):
            loop.run(ref)
        elif kind == "resume":
            loop.run(ref, resume=True)
        elif kind == "approval":
            approvals.run_approval_job(ref)

    def cancel(self, run_id, reason="stopped by the user"):
        r = store.run_get(run_id)
        if not r:
            return None
        if r["status"] in ("queued", "waiting_approval"):
            if store.run_set_status_if(run_id, ("queued", "waiting_approval"), "cancelled", reason):
                if r["status"] == "waiting_approval":
                    store.approvals_expire_for_run(run_id, "run_cancelled")
                events.publish("run", run_id=run_id, status="cancelled")
                return "cancelled"
        with self.lock:
            ctl = self.controls.get(run_id)
        if ctl:
            ctl.stop()
            return "stopping"
        return store.run_get(run_id)["status"]

    def cancel_all(self, reason, scheduled_only=False):
        n = 0
        for r in store.runs_with_status(("queued", "running", "waiting_approval")):
            if scheduled_only and r["trigger"] not in ("schedule",):
                continue
            if self.cancel(r["id"], reason) in ("cancelled", "stopping"):
                n += 1
        return n


_mgr = {"m": None}


def manager():
    if _mgr["m"] is None:
        _mgr["m"] = Manager()
    return _mgr["m"]


def use_inline(on):
    manager().inline = bool(on)


def reset():
    m = _mgr["m"]
    if m is not None:
        m.stop()
    _mgr["m"] = None
