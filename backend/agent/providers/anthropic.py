"""Anthropic Messages API over SSE.

`system` is top-level, tools are {name, description, input_schema}, tool calls
come back as `tool_use` content blocks whose JSON input arrives in pieces
(`input_json_delta`), and consecutive tool results go back in one user turn of
`tool_result` blocks. `max_tokens` is mandatory.
"""
import json
import time

from backend.agent.providers import http
from backend.agent.providers.base import ChatResult, ModelInfo, ProviderError, Usage, parse_args

API_VERSION = "2023-06-01"


def to_anthropic_messages(messages):
    out = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            block = {"type": "tool_result", "tool_use_id": m.get("tool_call_id") or "",
                     "content": m.get("content") or ""}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                    and all(b.get("type") == "tool_result" for b in out[-1]["content"]):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue
        if role == "assistant":
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for c in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c.get("arguments") or {}})
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            continue
        out.append({"role": "user", "content": m.get("content") or ""})
    return out


class AnthropicAdapter:
    def __init__(self, base_url, api_key=None, preset=None, extra=None, timeout=60):
        self.base = (base_url or "https://api.anthropic.com/v1").rstrip("/")
        self.key = api_key
        self.preset = preset or {}
        self.timeout = timeout
        self.call = None

    def _headers(self):
        h = {"anthropic-version": API_VERSION}
        if self.key:
            h["x-api-key"] = self.key
        return h

    def payload(self, req):
        p = {"model": req.model, "max_tokens": int(req.max_tokens or 1024), "stream": True,
             "temperature": req.temperature, "messages": to_anthropic_messages(req.messages)}
        if req.system:
            p["system"] = req.system
        if req.tools:
            p["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                          for t in req.tools]
        return p

    def abort(self):
        if self.call:
            self.call.abort()

    def chat(self, req, on_delta=None, cancel=None):
        self.call = http.Call(cancel)
        t0 = time.monotonic()
        res = ChatResult()
        blocks = {}
        text = []
        prompt = completion = 0
        for line in http.stream_lines(self.base + "/messages", self.payload(req), self._headers(),
                                      self.call, self.timeout, self.key):
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except ValueError:
                raise ProviderError("protocol", "bad stream chunk")
            t = ev.get("type")
            if t == "error":
                err = ev.get("error") or {}
                kind = "rate_limited" if err.get("type") == "overloaded_error" else "server"
                raise ProviderError(kind, err.get("message") or "")
            if t == "message_start":
                u = (ev.get("message") or {}).get("usage") or {}
                prompt = int(u.get("input_tokens") or 0)
            elif t == "content_block_start":
                cb = ev.get("content_block") or {}
                blocks[ev.get("index", 0)] = {"type": cb.get("type"), "id": cb.get("id"),
                                              "name": cb.get("name"), "json": ""}
            elif t == "content_block_delta":
                d = ev.get("delta") or {}
                if res.ttft_ms is None:
                    res.ttft_ms = int((time.monotonic() - t0) * 1000)
                if d.get("type") == "text_delta":
                    text.append(d.get("text") or "")
                    if on_delta and d.get("text"):
                        on_delta(d["text"])
                elif d.get("type") == "input_json_delta":
                    blocks.setdefault(ev.get("index", 0), {"type": "tool_use", "json": ""})["json"] += \
                        d.get("partial_json") or ""
            elif t == "message_delta":
                res.finish = (ev.get("delta") or {}).get("stop_reason") or res.finish
                completion = int(((ev.get("usage") or {}).get("output_tokens")) or completion)
            elif t == "message_stop":
                break
        for idx in sorted(blocks):
            b = blocks[idx]
            if b.get("type") == "tool_use":
                res.tool_calls.append({"id": b.get("id") or "toolu_%d" % idx, "name": b.get("name") or "",
                                       "arguments": parse_args(b.get("json") or "{}")})
        res.text = "".join(text)
        res.usage = Usage(prompt, completion)
        res.ms = int((time.monotonic() - t0) * 1000)
        return res

    def list_models(self):
        data = http.get_json(self.base + "/models", self._headers(), timeout=10, secret=self.key)
        return [ModelInfo(m["id"], m.get("context_window")) for m in (data.get("data") or []) if m.get("id")]
