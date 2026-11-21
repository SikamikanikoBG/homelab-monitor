"""Ollama's native /api/chat (NDJSON), ported from Jarvis2.

Native rather than Ollama's OpenAI-compatible endpoint for one reason:
`options.num_ctx`. Ollama's default context silently truncates long prompts;
here it is sized from /api/show, which also reports `capabilities` (a free
hint whether the model can call tools).
"""
import json
import time
import uuid

from backend.agent.providers import http
from backend.agent.providers.base import ChatResult, ModelInfo, ProviderError, Usage, parse_args


def to_ollama_messages(system, messages):
    out = [{"role": "system", "content": system}] if system else []
    for m in messages:
        if m.get("role") == "tool":
            out.append({"role": "tool", "content": m.get("content") or "", "tool_name": m.get("name") or ""})
            continue
        item = {"role": m["role"], "content": m.get("content") or ""}
        if m.get("tool_calls"):
            item["tool_calls"] = [{"function": {"name": c["name"], "arguments": c.get("arguments") or {}}}
                                  for c in m["tool_calls"]]
        out.append(item)
    return out


def parse_tool_calls(raw):
    calls = []
    for item in raw or []:
        fn = (item or {}).get("function") or {}
        calls.append({"id": item.get("id") or "call_" + uuid.uuid4().hex[:12],
                      "name": str(fn.get("name") or ""), "arguments": parse_args(fn.get("arguments"))})
    return calls


class OllamaAdapter:
    def __init__(self, base_url, api_key=None, preset=None, extra=None, timeout=180):
        self.base = (base_url or "").rstrip("/")
        if self.base.endswith("/v1"):
            self.base = self.base[:-3]
        self.preset = preset or {}
        self.timeout = timeout
        self.call = None

    def payload(self, req):
        opts = {"temperature": req.temperature, "num_predict": req.max_tokens}
        if req.num_ctx:
            opts["num_ctx"] = int(req.num_ctx)
        p = {"model": req.model, "messages": to_ollama_messages(req.system, req.messages),
             "stream": True, "options": opts}
        if req.tools and not req.no_tools:
            p["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                            "parameters": t["parameters"]}} for t in req.tools]
        return p

    def abort(self):
        if self.call:
            self.call.abort()

    def chat(self, req, on_delta=None, cancel=None):
        self.call = http.Call(cancel)
        payload = self.payload(req)
        t0 = time.monotonic()
        res = ChatResult()
        text = []
        for line in http.stream_lines(self.base + "/api/chat", payload, {}, self.call, self.timeout):
            try:
                chunk = json.loads(line)
            except ValueError:
                raise ProviderError("protocol", "bad stream chunk")
            if chunk.get("error"):
                raise ProviderError("server", str(chunk["error"]))
            msg = chunk.get("message") or {}
            if msg.get("content"):
                if res.ttft_ms is None:
                    res.ttft_ms = int((time.monotonic() - t0) * 1000)
                text.append(msg["content"])
                if on_delta:
                    on_delta(msg["content"])
            if msg.get("tool_calls"):
                if res.ttft_ms is None:
                    res.ttft_ms = int((time.monotonic() - t0) * 1000)
                res.tool_calls.extend(parse_tool_calls(msg["tool_calls"]))
            if chunk.get("done"):
                res.finish = chunk.get("done_reason")
                res.usage = Usage(int(chunk.get("prompt_eval_count") or 0), int(chunk.get("eval_count") or 0))
        res.text = "".join(text)
        res.ms = int((time.monotonic() - t0) * 1000)
        return res

    def list_models(self):
        data = http.get_json(self.base + "/api/tags", timeout=10)
        return [ModelInfo(m.get("name") or m.get("model")) for m in (data.get("models") or [])
                if m.get("name") or m.get("model")]

    def show(self, model):
        """(context_window, tools_hint) from /api/show; (None, None) if unknown."""
        try:
            d = http.post_json(self.base + "/api/show", {"model": model}, timeout=10)
        except ProviderError:
            return None, None
        window = None
        for k, v in (d.get("model_info") or {}).items():
            if k.endswith(".context_length") and isinstance(v, int):
                window = v
        caps = d.get("capabilities")
        hint = None if caps is None else ("yes" if "tools" in caps else "no")
        return window, hint
