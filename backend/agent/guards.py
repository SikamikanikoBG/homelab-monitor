"""Budgets and loop guards, ported from Jarvis2 (engine/loop.py, supervision.py).

Small local models loop, repeat the same failing call, or answer with nothing.
These guards end a run with a plain reason instead of letting it spin (EC-3),
and keep the prompt inside the model's window (EC-4, EC-24). All of them look
at structure — call names, argument hashes, result hashes, sizes, counts —
never at the wording of what the model or a tool said.
"""
import hashlib
import json
import time
from dataclasses import dataclass, field

CHARS_PER_TOKEN = 3.2
SAME_FAILURE_LIMIT = 2
ERROR_STREAK_LIMIT = 3
REPEAT_POINTER_MIN_CHARS = 600
RESULT_CAP_CHARS = 12000
AGED_VIEW_CHARS = 600
COMPRESS_TO = 0.5


def args_hash(name, args):
    raw = json.dumps([name, args], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def result_hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:24]


@dataclass
class StepRecord:
    tool: str
    args_hash: str
    result_hash: str
    ok: bool
    summary: str
    kind: str = "read"


@dataclass
class Watch:
    budget: dict
    started: float = field(default_factory=time.monotonic)
    steps: list = field(default_factory=list)
    model_calls: int = 0
    tokens: int = 0
    error_streak: int = 0
    empty_replies: int = 0

    def record(self, rec):
        """The error streak counts the SAME failing call in a row (alpha #10):
        unrelated failures — one host offline, one check missing — are normal
        in a productive run and must not end it."""
        prev = self.steps[-1] if self.steps else None
        self.steps.append(rec)
        if rec.ok:
            self.error_streak = 0
        elif prev is not None and not prev.ok and prev.args_hash == rec.args_hash:
            self.error_streak += 1
        else:
            self.error_streak = 1


def budget_exceeded(w):
    """A plain reason when a budget is spent, else None."""
    b = w.budget or {}
    if b.get("max_steps") and w.model_calls >= int(b["max_steps"]):
        return "it used all %d steps it was allowed" % int(b["max_steps"])
    if b.get("max_seconds") and time.monotonic() - w.started >= float(b["max_seconds"]):
        return "it ran out of time (%d s)" % int(b["max_seconds"])
    if b.get("max_tokens") and w.tokens >= int(b["max_tokens"]):
        return "it used its token budget (%d)" % int(b["max_tokens"])
    return None


def error_streak_reason(w):
    if w.error_streak >= ERROR_STREAK_LIMIT:
        last = [s for s in w.steps if not s.ok][-1:]
        return "%d tool calls in a row failed (last: %s)" % (w.error_streak, last[0].summary[:120] if last else "")
    return None


def repeated_failure(w, name, args):
    digest = args_hash(name, args)
    fails = [s for s in w.steps if s.args_hash == digest and not s.ok]
    if len(fails) < SAME_FAILURE_LIMIT:
        return None
    return ("not run: this exact %s call already failed %d times the same way, so it won't be retried. "
            "Last error: %s. Try something else, or tell the user what you found and what is blocked."
            % (name, len(fails), fails[-1].summary[:200]))


def already_in_context(w, name, args, text):
    """A pointer instead of a second copy when a read call returns exactly what
    the same call returned earlier in this run."""
    if len(text or "") < REPEAT_POINTER_MIN_CHARS:
        return None
    a, r = args_hash(name, args), result_hash(text)
    for n, s in enumerate(w.steps, 1):
        if s.args_hash == a and s.result_hash == r:
            return ("[identical to the result of tool call %d (%s, same arguments) — nothing changed; that "
                    "result is already above.]" % (n, name))
    return None


def cap_result(text, cap=RESULT_CAP_CHARS):
    if len(text) <= cap:
        return text
    return (text[:cap] + "\n[truncated: %d more characters. Ask about one host, a narrower range or a "
            "specific item to see the rest.]" % (len(text) - cap))


def _size(m):
    return len(m.get("content") or "") + len(json.dumps(m.get("tool_calls") or [], default=str))


def aged(m):
    c = m.get("content") or ""
    if len(c) <= AGED_VIEW_CHARS:
        return m
    return dict(m, content=c[:AGED_VIEW_CHARS] + "\n[older result shortened to save room: %d characters left out]"
                % (len(c) - AGED_VIEW_CHARS))


def compress_old_tool_results(messages, budget_chars):
    """Shrink the OLDEST tool results to a head view once the run's results go
    over budget — never the ones the model is answering right now — and only
    down to half the budget so it doesn't trigger again next step."""
    last_assistant = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1)
    if last_assistant < 0:
        return messages
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    total = sum(len(messages[i].get("content") or "") for i in tool_idx)
    if total <= budget_chars:
        return messages
    target = int(budget_chars * COMPRESS_TO)
    out = list(messages)
    for i in tool_idx:
        if i >= last_assistant:
            break
        shrunk = aged(out[i])
        if shrunk is out[i]:
            continue
        total -= len(out[i].get("content") or "") - len(shrunk.get("content") or "")
        out[i] = shrunk
        if total <= target:
            break
    return out


def fit_to_window(messages, limit_chars, fresh_from=0):
    """Cut the newest tool results (index >= fresh_from), largest first and only
    by the overflow, so the prompt fits the window."""
    total = sum(_size(m) for m in messages)
    overflow = total - limit_chars
    if overflow <= 0:
        return messages
    out = list(messages)
    fresh = sorted((i for i, m in enumerate(out) if m.get("role") == "tool" and i >= fresh_from),
                   key=lambda i: -len(out[i].get("content") or ""))
    for i in fresh:
        if overflow <= 0:
            break
        c = out[i].get("content") or ""
        room = len(c) - AGED_VIEW_CHARS
        if room <= 0:
            continue
        cut = min(room, overflow)
        keep = len(c) - cut
        out[i] = dict(out[i], content=c[:keep] + "\n[cut to fit the model's window: %d characters left out]" % cut)
        overflow -= cut
    return out


def total_chars(system, messages, tools):
    return len(system or "") + sum(_size(m) for m in messages) + len(json.dumps(tools or []))


def window_chars(context_window, reserve_tokens):
    return int(max(1024, (context_window or 8192) - reserve_tokens) * CHARS_PER_TOKEN * 0.9)
