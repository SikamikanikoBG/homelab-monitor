"""Live agent events, riding the dashboard's existing /api/stream.

A browser tab keeps exactly one stream open; the agent's frames travel on it as
`event: agent` (run status changes, steps/tool chips, approvals, task changes,
and text deltas for attended chat runs). The bus is a small in-memory ring
with a global sequence number; publishing bumps `REV` and wakes the stream's
condition variable. Nothing here is persisted — the database is the truth,
and a client that reconnects simply refetches the run.

When the agent is off nothing is ever published, so /api/stream stays
byte-identical to what it was.
"""
import json
import threading
import time

_RING = 2000
_lock = threading.Lock()
_events = []          # [(seq, payload_dict)]
_state = {"seq": 0}
_deltas = {}          # run_id -> [text, first_at]
_DELTA_EVERY = 0.1


def rev():
    return _state["seq"]


def _wake():
    from backend.agent import ports
    try:
        cond = ports.live_cond()
    except Exception as e:                  # app not importable (unit tests of this module)
        print("agent events: no live condition (%s)" % e, flush=True)
        return
    with cond:
        cond.notify_all()


def publish(event_type, **data):
    payload = dict(data, type=event_type, ts=time.time())
    with _lock:
        _state["seq"] += 1
        _events.append((_state["seq"], payload))
        if len(_events) > _RING:
            del _events[:len(_events) - _RING]
    _wake()


def text_delta(run_id, text):
    """Buffer streamed text and publish it at most every 100 ms."""
    flush = None
    with _lock:
        buf = _deltas.setdefault(run_id, ["", time.monotonic()])
        buf[0] += text
        if time.monotonic() - buf[1] >= _DELTA_EVERY:
            flush, _deltas[run_id] = buf[0], ["", time.monotonic()]
    if flush:
        publish("text", run_id=run_id, text=flush)


def flush_text(run_id):
    with _lock:
        buf = _deltas.pop(run_id, None)
    if buf and buf[0]:
        publish("text", run_id=run_id, text=buf[0])


def since(seq):
    """[(seq, payload)] published after `seq` (oldest first)."""
    with _lock:
        return [(s, p) for s, p in _events if s > seq]


def frames_since(seq):
    """SSE text for every event after `seq`, and the new last seq."""
    out, last = [], seq
    for s, p in since(seq):
        out.append("event: agent\nid: a%d\ndata: %s\n\n" % (s, json.dumps(p, ensure_ascii=False)))
        last = s
    return "".join(out), last


def reset():
    with _lock:
        _events.clear()
        _deltas.clear()
