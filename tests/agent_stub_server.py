#!/usr/bin/env python3
"""A deterministic model server for tests and the alpha sandbox. Test-only code:
it is never imported by the product.

Speaks three wire formats on one port:
  OpenAI-compatible  GET /v1/models, POST /v1/chat/completions (SSE)
  Ollama             GET /api/tags, POST /api/show, POST /api/chat (NDJSON)
  Anthropic          GET /v1/models, POST /v1/messages (SSE)
Prefix any path with /err/<status> to get that HTTP error instead, or with
/slow/<seconds> to delay the first byte.

What it answers (`decide()` below):
- a scripted list of turns, when the server was started with --script or a
  test called `set_script()` (consumed in order, then the default applies);
- a user message starting with "#stub " followed by JSON — that JSON (a turn or
  a list of turns) is played for this conversation, so a browser test can drive
  an exact scenario through the real UI;
- the tool-capability probe (`report_number`): n=7, then n=12;
- otherwise: call `list_hosts` once, then answer with a short text that
  mentions how many tool results it saw.

A turn is {"text": str, "tool_calls": [{"name": str, "arguments": dict}],
"usage": {"prompt": int, "completion": int}}.

    python tests/agent_stub_server.py --host 0.0.0.0 --port 18099 [--script file.json]
"""
import argparse
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = ["stub-agent", "stub-notools"]
_lock = threading.Lock()
_script = []
REQUESTS = []          # every chat request body, for assertions


def set_script(turns):
    with _lock:
        _script[:] = list(turns or [])
        REQUESTS.clear()


def _last_user(messages):
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
            return c or ""
    return ""


def _stub_turns(messages):
    """Turns requested by a '#stub ' user message, and how many assistant turns
    followed it so far."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, list):
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                continue
            c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
        if isinstance(c, str) and c.startswith("#stub "):
            turns = json.loads(c[6:])
            turns = turns if isinstance(turns, list) else [turns]
            done = sum(1 for m2 in messages[i + 1:] if m2.get("role") == "assistant")
            return turns, done
        return None, 0
    return None, 0


def decide(model, messages, tool_names):
    with _lock:
        if _script:
            return _script.pop(0)
    if model == "stub-notools":
        return {"text": "I can only talk, I can't use tools."}
    turns, done = _stub_turns(messages)
    if turns is not None:
        return turns[done] if done < len(turns) else {"text": "Done."}
    if "report_number" in tool_names:
        tool_msgs = [m for m in messages if m.get("role") == "tool"
                     or (isinstance(m.get("content"), list) and any(
                         isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"]))]
        return {"tool_calls": [{"name": "report_number", "arguments": {"n": 12 if tool_msgs else 7}}]}
    tool_results = sum(1 for m in messages if m.get("role") == "tool")
    since_user = 0
    for m in reversed(messages):
        if m.get("role") == "user" and not isinstance(m.get("content"), list):
            break
        since_user += 1
    if since_user == 0 and "list_hosts" in tool_names:
        return {"tool_calls": [{"name": "list_hosts", "arguments": {}}]}
    return {"text": "Stub answer: I looked at %d tool result(s). Everything you asked about is listed above."
                    % tool_results}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _prefix(self):
        path = self.path
        if path.startswith("/err/"):
            code = int(path.split("/")[2])
            self._json(code, {"error": {"message": "stub error %d" % code}})
            return None
        if path.startswith("/slow/"):
            parts = path.split("/")
            time.sleep(float(parts[2]))
            path = "/" + "/".join(parts[3:])
        return path

    def do_GET(self):
        path = self._prefix()
        if path is None:
            return
        if path.endswith("/models"):
            return self._json(200, {"data": [{"id": m, "max_model_len": 32768} for m in MODELS]})
        if path.endswith("/api/tags"):
            return self._json(200, {"models": [{"name": m} for m in MODELS]})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self._prefix()
        if path is None:
            return
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        with _lock:
            REQUESTS.append({"path": path, "headers": dict(self.headers), "body": body})
        if path.endswith("/api/show"):
            return self._json(200, {"model_info": {"stub.context_length": 32768},
                                    "capabilities": ["completion"] if body.get("model") == "stub-notools"
                                    else ["completion", "tools"]})
        messages = body.get("messages") or []
        tools = body.get("tools") or []
        names = [(t.get("function") or t).get("name") for t in tools]
        turn = decide(body.get("model"), messages, names)
        if path.endswith("/chat/completions"):
            return self._openai(turn)
        if path.endswith("/api/chat"):
            return self._ollama(turn)
        if path.endswith("/messages"):
            return self._anthropic(turn)
        return self._json(404, {"error": "not found"})

    def _start_stream(self, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Connection", "close")
        self.end_headers()

    def _w(self, s):
        try:
            self.wfile.write(s.encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionError):
            pass                                   # the client hung up (a Stop test); nothing to do

    def _openai(self, turn):
        self._start_stream("text/event-stream")
        text = turn.get("text") or ""
        for i in range(0, len(text), 12):
            self._w("data: %s\n\n" % json.dumps({"choices": [{"delta": {"content": text[i:i + 12]}}]}))
        for i, c in enumerate(turn.get("tool_calls") or []):
            args = c.get("arguments")
            raw = args if isinstance(args, str) else json.dumps(args or {})
            cid = c.get("id") or "call_" + uuid.uuid4().hex[:8]
            self._w("data: %s\n\n" % json.dumps({"choices": [{"delta": {"tool_calls": [
                {"index": i, "id": cid, "type": "function", "function": {"name": c["name"], "arguments": raw[:5]}}]}}]}))
            self._w("data: %s\n\n" % json.dumps({"choices": [{"delta": {"tool_calls": [
                {"index": i, "function": {"arguments": raw[5:]}}]}}]}))
        u = turn.get("usage") or {"prompt": 100, "completion": 20}
        self._w("data: %s\n\n" % json.dumps({"choices": [{"delta": {}, "finish_reason":
                                                           "tool_calls" if turn.get("tool_calls") else "stop"}]}))
        self._w("data: %s\n\n" % json.dumps({"choices": [], "usage": {"prompt_tokens": u["prompt"],
                                                                     "completion_tokens": u["completion"]}}))
        self._w("data: [DONE]\n\n")

    def _ollama(self, turn):
        self._start_stream("application/x-ndjson")
        text = turn.get("text") or ""
        for i in range(0, len(text), 12):
            self._w(json.dumps({"message": {"role": "assistant", "content": text[i:i + 12]}, "done": False}) + "\n")
        if turn.get("tool_calls"):
            self._w(json.dumps({"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": c["name"], "arguments": c.get("arguments") or {}}}
                for c in turn["tool_calls"]]}, "done": False}) + "\n")
        u = turn.get("usage") or {"prompt": 100, "completion": 20}
        self._w(json.dumps({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
                            "prompt_eval_count": u["prompt"], "eval_count": u["completion"]}) + "\n")

    def _anthropic(self, turn):
        self._start_stream("text/event-stream")
        u = turn.get("usage") or {"prompt": 100, "completion": 20}
        self._w("event: message_start\ndata: %s\n\n" % json.dumps(
            {"type": "message_start", "message": {"usage": {"input_tokens": u["prompt"]}}}))
        idx = 0
        if turn.get("text"):
            self._w("data: %s\n\n" % json.dumps({"type": "content_block_start", "index": 0,
                                                 "content_block": {"type": "text", "text": ""}}))
            self._w("data: %s\n\n" % json.dumps({"type": "content_block_delta", "index": 0,
                                                 "delta": {"type": "text_delta", "text": turn["text"]}}))
            idx = 1
        for c in turn.get("tool_calls") or []:
            raw = json.dumps(c.get("arguments") or {})
            self._w("data: %s\n\n" % json.dumps({"type": "content_block_start", "index": idx, "content_block":
                                                 {"type": "tool_use", "id": "toolu_%d" % idx, "name": c["name"]}}))
            for part in (raw[:4], raw[4:]):
                self._w("data: %s\n\n" % json.dumps({"type": "content_block_delta", "index": idx,
                                                     "delta": {"type": "input_json_delta", "partial_json": part}}))
            idx += 1
        self._w("data: %s\n\n" % json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                                             "usage": {"output_tokens": u["completion"]}}))
        self._w("data: %s\n\n" % json.dumps({"type": "message_stop"}))


def start(host="127.0.0.1", port=0):
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://%s:%d" % (host, srv.server_address[1])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18099)
    ap.add_argument("--script")
    a = ap.parse_args()
    if a.script:
        with open(a.script, encoding="utf-8") as f:
            set_script(json.load(f))
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print("stub model server on http://%s:%d (models: %s)" % (a.host, a.port, ", ".join(MODELS)), flush=True)
    srv.serve_forever()
