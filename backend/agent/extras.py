"""After a chat answer: a short title for the conversation and 0–2 follow-up
questions (UAT-1).

One small tool-less call, made after the answer is already on screen. The model
returns a JSON object; the fields are read from it as data, never picked out
of prose. If the reply isn't that object, nothing is shown — no guessing. A
conversation's first title is its first question, cut to a line, until the
model's title arrives (or if it never does).
"""
import json
import re

from backend.agent import events, state, store
from backend.agent.providers.base import ChatRequest, ProviderError

TITLE_MAX = 60
FOLLOWUP_MAX = 90

SYSTEM = (
    "You label one exchange between a home-lab owner and the assistant that watches their machines. "
    "Reply with one JSON object and nothing else: "
    '{"title": "...", "followups": ["...", "..."]}. '
    "title: 2 to 6 words naming what was asked about, in the language of the question, no quotes, no full stop. "
    "followups: 0 to 2 short questions the owner is likely to ask next, in the language of the question, each "
    "one the assistant could answer from the lab's monitoring data or its read-only checks. Use [] when no "
    "follow-up is useful.")


def _json_values(text):
    """(start, end) of every JSON object or array that parses inside text."""
    dec = json.JSONDecoder()
    out, i = [], 0
    while i < len(text):
        if text[i] in "[{":
            try:
                _, end = dec.raw_decode(text, i)
            except ValueError:
                i += 1
                continue
            out.append((i, end))
            i = end
        else:
            i += 1
    return out


def fallback_title(text):
    """The first question as a title: its first line without embedded JSON data,
    without trailing #tags, cut at a word to fit."""
    line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    for start, end in reversed(_json_values(line)):
        line = line[:start] + line[end:]
    words = line.split()
    while words and words[-1].startswith("#"):
        words.pop()
    line = " ".join(words).strip(" .,:;-")
    if len(line) > TITLE_MAX:
        line = line[:TITLE_MAX].rsplit(" ", 1)[0].rstrip(" .,:;-") + "…"
    return line


def parse(text):
    """{"title": str|None, "followups": [str]} from the model's reply, or None."""
    obj = None
    for start, end in _json_values(text or ""):
        try:
            cand = json.loads(text[start:end])
        except ValueError:
            continue
        if isinstance(cand, dict):
            obj = cand
            break
    if obj is None:
        return None
    title = obj.get("title")
    title = re.sub(r"\s+", " ", title).strip(" \"'.") if isinstance(title, str) else ""
    if not title or len(title) > TITLE_MAX or _json_values(title):
        title = None
    ups = []
    for q in obj.get("followups") or []:
        if isinstance(q, str):
            q = re.sub(r"\s+", " ", q).strip()
            if 3 <= len(q) <= FOLLOWUP_MAX and not _json_values(q) and q not in ups:
                ups.append(q)
    return {"title": title, "followups": ups[:2]}


def after_answer(run, ctx, adapter, ctl):
    """Best effort: a failure here never touches the answer or the run's status."""
    if state.get("chat_extras") != "1" or adapter is None:
        return
    msgs = store.message_list(run["conversation_id"])
    mine = [m for m in msgs if m["run_id"] == run["id"]]
    question = next((m["content"] for m in mine if m["role"] == "user" and m["content"]), "")
    answer = next((m["content"] for m in reversed(mine) if m["role"] == "assistant" and m["content"]), "")
    if not question or not answer:
        return
    first = len({m["run_id"] for m in msgs if m["run_id"]}) == 1
    req = ChatRequest(model=run["model"], system=SYSTEM, tools=[], max_tokens=200, temperature=0.2,
                      messages=[{"role": "user", "content": "Question:\n%s\n\nAnswer:\n%s" % (
                          question[:2000], answer[:4000])}])
    try:
        res = adapter.chat(req, cancel=ctl.cancel)
    except ProviderError as e:
        print("agent: no title/follow-ups for %s: %s" % (run["id"], e.human()), flush=True)
        return
    m = (ctx.model or {}) if ctx else {}
    cost = int(round(res.usage.prompt * float(m.get("price_in") or 0) + res.usage.completion * float(m.get("price_out") or 0)))
    store.run_add_usage(run["id"], res.usage.prompt, res.usage.completion, cost)
    got = parse(res.text)
    if got is None:
        return
    store.step_append(run["id"], "meta", args_json={"title": got["title"] if first else None,
                                                    "followups": got["followups"]},
                      tokens_in=res.usage.prompt, tokens_out=res.usage.completion)
    if first and got["title"]:
        store.conversation_update(run["conversation_id"], title=got["title"])
    events.publish("message", run_id=run["id"], conversation_id=run["conversation_id"])


def followups(run_id):
    for s in reversed(store.step_list(run_id)):
        if s["kind"] == "meta":
            return list((s["args"] or {}).get("followups") or [])
    return []
