"""OpenAI-compatible chat completions over SSE (port of Jarvis2's adapter).

One code path for OpenAI, Groq, Mistral, DeepSeek, xAI, OpenRouter, Together,
vLLM, llama.cpp, LM Studio, LocalAI, Azure OpenAI and Gemini's compatible
endpoint — what differs between them is data in presets.json: auth header
style, URL template, whether `stream_options` is understood, extra body
fields, and which JSON-schema keywords the provider refuses.
"""
import json
import re
import time
import uuid

from backend.agent.providers import http
from backend.agent.providers.base import ChatResult, ModelInfo, ProviderError, Usage, parse_args

# vLLM answers 400 when prompt + max_tokens exceeds the context; the message
# quotes the numbers. Parsing a server's protocol message (not user or model
# text): halve max_tokens and retry while it can still fit (Jarvis2 port).
_CONTEXT_400 = re.compile(
    r"maximum context length is (\d+) tokens.*?requested (\d+) output tokens.*?at least (\d+) input tokens", re.S)
_MIN_OUTPUT_TOKENS = 256


def smaller_max_tokens(error, requested):
    m = _CONTEXT_400.search(error or "")
    if not m:
        return None
    context, _, floor = (int(g) for g in m.groups())
    candidate = min(requested // 2, context - floor - 16)
    return candidate if candidate >= _MIN_OUTPUT_TOKENS else None


def strip_schema(schema, keys):
    """Drop schema keywords a provider refuses (Gemini: pattern,
    additionalProperties). The server still validates every argument."""
    if not keys:
        return schema
    if isinstance(schema, dict):
        return {k: strip_schema(v, keys) for k, v in schema.items() if k not in keys}
    if isinstance(schema, list):
        return [strip_schema(v, keys) for v in schema]
    return schema


def to_openai_messages(system, messages):
    out = [{"role": "system", "content": system}] if system else []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            out.append({"role": "tool", "content": m.get("content") or "", "tool_call_id": m.get("tool_call_id") or ""})
            continue
        item = {"role": role, "content": m.get("content")}
        if m.get("tool_calls"):
            calls = []
            meta = (m.get("provider_meta") or {}).get("tool_calls") or {}
            for c in m["tool_calls"]:
                call = {"id": c["id"], "type": "function",
                        "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments") or {})}}
                call.update(meta.get(c["id"]) or {})      # opaque provider fields, echoed back as received
                calls.append(call)
            item["tool_calls"] = calls
            if not item["content"]:
                item["content"] = None
        elif item["content"] is None:
            item["content"] = ""
        out.append(item)
    return out


class OpenAICompatAdapter:
    def __init__(self, base_url, api_key=None, preset=None, extra=None, timeout=180):
        self.base = (base_url or "").rstrip("/")
        self.key = api_key
        self.preset = preset or {}
        self.extra = extra or {}
        self.timeout = timeout
        self.call = None

    def _headers(self):
        style = self.preset.get("auth", "bearer")
        if not self.key or style == "none":
            return {}
        if style == "api-key":
            return {"api-key": self.key}
        return {"Authorization": "Bearer " + self.key}

    def _chat_url(self, model):
        tpl = self.preset.get("url_template")
        if tpl:
            return tpl.format(base=self.base, model=model, deployment=self.extra.get("deployment") or model,
                              api_version=self.extra.get("api_version") or self.preset.get("api_version", ""))
        return self.base + "/chat/completions"

    def payload(self, req):
        p = {"model": req.model, "messages": to_openai_messages(req.system, req.messages), "stream": True,
             "temperature": req.temperature, "max_tokens": req.max_tokens}
        if self.preset.get("stream_usage", True):
            p["stream_options"] = {"include_usage": True}
        if req.tools and not req.no_tools:
            strip = self.preset.get("schema_strip") or []
            p["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                            "parameters": strip_schema(t["parameters"], strip)}}
                          for t in req.tools]
        for k, v in (self.preset.get("extra_body") or {}).items():
            p.setdefault(k, v)
        return p

    def abort(self):
        if self.call:
            self.call.abort()

    def chat(self, req, on_delta=None, cancel=None):
        payload = self.payload(req)
        url = self._chat_url(req.model)
        while True:
            try:
                return self._stream(url, payload, on_delta, cancel)
            except ProviderError as e:
                smaller = smaller_max_tokens(e.message, int(payload.get("max_tokens") or 0)) \
                    if e.kind == "bad_request" else None
                if smaller is None:
                    if e.kind == "bad_request" and _CONTEXT_400.search(e.message or ""):
                        raise ProviderError("context", e.message, status=e.status)
                    raise
                payload = dict(payload, max_tokens=smaller)

    def _stream(self, url, payload, on_delta, cancel):
        self.call = http.Call(cancel)
        t0 = time.monotonic()
        res = ChatResult()
        pending = {}
        meta = {}
        text = []
        got_usage = False
        for line in http.stream_lines(url, payload, self._headers(), self.call, self.timeout, self.key):
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                raise ProviderError("protocol", "bad stream chunk")
            if chunk.get("error"):
                err = chunk["error"]
                raise ProviderError("server", err.get("message") if isinstance(err, dict) else str(err))
            if chunk.get("usage"):
                u = chunk["usage"]
                res.usage = Usage(int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0))
                got_usage = True
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if content:
                    if res.ttft_ms is None:
                        res.ttft_ms = int((time.monotonic() - t0) * 1000)
                    text.append(content)
                    if on_delta:
                        on_delta(content)
                for tc in delta.get("tool_calls") or []:
                    if res.ttft_ms is None:
                        res.ttft_ms = int((time.monotonic() - t0) * 1000)
                    idx = int(tc.get("index", len(pending)))
                    slot = pending.setdefault(idx, {"id": None, "name": "", "args": "", "meta": {}})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]
                    for k, v in tc.items():
                        if k not in ("index", "id", "type", "function"):
                            slot["meta"][k] = v
                if choice.get("finish_reason"):
                    res.finish = choice["finish_reason"]
        res.text = "".join(text)
        for idx in sorted(pending):
            s = pending[idx]
            cid = s["id"] or "call_" + uuid.uuid4().hex[:12]
            res.tool_calls.append({"id": cid, "name": s["name"], "arguments": parse_args(s["args"])})
            if s["meta"]:
                meta[cid] = s["meta"]
        if meta:
            res.provider_meta = {"tool_calls": meta}
        if not got_usage:
            from backend.agent.providers.base import estimate_tokens
            chars = sum(len(json.dumps(m)) for m in payload["messages"])
            res.usage = Usage(estimate_tokens(chars), estimate_tokens(len(res.text)), estimated=True)
        res.ms = int((time.monotonic() - t0) * 1000)
        return res

    def list_models(self):
        url = self.preset.get("models_url_template", "{base}/models").format(
            base=self.base, api_version=self.extra.get("api_version") or self.preset.get("api_version", ""))
        data = http.get_json(url, self._headers(), timeout=10, secret=self.key)
        rows = data.get("data") if isinstance(data, dict) else data
        out = []
        for m in rows or []:
            if not isinstance(m, dict):
                continue
            mid = m.get("id") or m.get("name")
            if not mid:
                continue
            window = m.get("max_model_len") or m.get("context_length") or m.get("context_window")
            out.append(ModelInfo(str(mid).replace("models/", "", 1) if self.preset.get("strip_models_prefix") else str(mid),
                                 int(window) if isinstance(window, (int, float)) else None))
        return out
