"""Can this model drive the agent? Two small requests answer it (US-1, EC-2).

1. Offer one tool, `report_number(n)`, and ask for n=7. A well-formed call with
   n == 7 means it can call tools ("tools: yes"); a plain-text answer means it
   can't ("tools: no").
2. Answer that call with a tool result asking for n = 7 + 5, and expect a
   second call with n == 12. That shows it reads a tool result and keeps
   going — what a multi-step agent run needs ("multi-step: yes").

Both checks look only at the structured tool call (name and argument), never
at the wording of a reply.
"""
from backend.agent.providers.base import ChatRequest, ProviderError

TOOL = {"name": "report_number", "description": "Report a number to the test harness.",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["n"],
                       "properties": {"n": {"type": "integer"}}}}
SYSTEM = "You are a test harness. Always answer by calling the provided tool."


def _call_n(result):
    for c in result.tool_calls:
        if c.get("name") == TOOL["name"]:
            n = (c.get("arguments") or {}).get("n")
            if isinstance(n, str) and n.strip().lstrip("-").isdigit():
                n = int(n)
            return c, n
    return None, None


def tool_probe(adapter, model, num_ctx=None, cancel=None):
    """{'tools': yes|no|unknown, 'multi_step': yes|no|unknown, 'detail': str, 'ms': int}"""
    msgs = [{"role": "user", "content": "Call report_number with n = 7."}]
    try:
        r1 = adapter.chat(ChatRequest(model=model, system=SYSTEM, messages=msgs, tools=[TOOL], max_tokens=200,
                                      temperature=0, num_ctx=num_ctx), cancel=cancel)
    except ProviderError as e:
        return {"tools": "unknown", "multi_step": "unknown", "detail": e.human(), "ms": 0}
    call, n = _call_n(r1)
    if call is None:
        return {"tools": "no", "multi_step": "no", "ms": r1.ms,
                "detail": "This model answered in text instead of calling the tool, so it can't drive the agent. "
                          "Pick a model with tool calling."}
    if n != 7:
        return {"tools": "no", "multi_step": "no", "ms": r1.ms,
                "detail": "The model called the tool but with the wrong argument (n=%r)." % (n,)}
    msgs += [{"role": "assistant", "content": r1.text or None, "tool_calls": [call],
              "provider_meta": r1.provider_meta},
             {"role": "tool", "tool_call_id": call["id"], "name": TOOL["name"],
              "content": "Recorded 7. Now call report_number again with n equal to 7 + 5."}]
    try:
        r2 = adapter.chat(ChatRequest(model=model, system=SYSTEM, messages=msgs, tools=[TOOL], max_tokens=200,
                                      temperature=0, num_ctx=num_ctx), cancel=cancel)
    except ProviderError as e:
        return {"tools": "yes", "multi_step": "unknown", "detail": "Second step failed: " + e.human(),
                "ms": r1.ms}
    _, n2 = _call_n(r2)
    multi = "yes" if n2 == 12 else "no"
    detail = ("Calls tools and follows up on their results." if multi == "yes" else
              "Calls tools, but didn't follow up on a tool result — fine for questions, weak for multi-step tasks.")
    return {"tools": "yes", "multi_step": multi, "detail": detail, "ms": r1.ms + r2.ms}
