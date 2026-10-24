"""Test doubles for the agent: a scripted model, a fake fleet, and helpers to
switch the agent on and wipe its tables between tests.

ScriptedAdapter implements the adapter interface (`chat`, `list_models`,
`abort`) and plays a list of turns. A turn is a dict
  {"text": str, "tool_calls": [{"name", "arguments"}], "usage": {"prompt", "completion"}}
or a callable `turn(request) -> dict` for reactive scripts. Every request it
receives is kept in `.requests`, so tests can check exactly what the model was
shown.
"""
import os
import threading
import time
import uuid
from unittest.mock import patch

from auth_helpers import app, clear_login, login
from backend.agent import events, schema
from backend.agent.providers.base import ChatResult, ModelInfo, ProviderError, Usage


class ScriptedAdapter:
    def __init__(self, turns, delay_s=0.0, chunk_text=True):
        self.turns = list(turns)
        self.requests = []
        self.delay_s = delay_s
        self.chunk_text = chunk_text
        self.aborted = threading.Event()

    def chat(self, req, on_delta=None, cancel=None):
        self.requests.append(req)
        if self.delay_s:
            end = time.monotonic() + self.delay_s
            while time.monotonic() < end:
                if (cancel and cancel.is_set()) or self.aborted.is_set():
                    raise ProviderError("cancelled")
                time.sleep(0.01)
        if not self.turns:
            turn = {"text": "(script finished)"}
        else:
            turn = self.turns.pop(0)
        if callable(turn):
            turn = turn(req)
        if isinstance(turn, Exception):
            raise turn
        text = turn.get("text") or ""
        if on_delta and text:
            step = 5 if self.chunk_text else len(text)
            for i in range(0, len(text), step):
                on_delta(text[i:i + step])
        calls = [{"id": c.get("id") or "call_" + uuid.uuid4().hex[:8], "name": c["name"],
                  "arguments": c.get("arguments", {})} for c in turn.get("tool_calls") or []]
        u = turn.get("usage") or {"prompt": 100, "completion": 10}
        return ChatResult(text=text, tool_calls=calls, finish="stop", usage=Usage(u["prompt"], u["completion"]),
                          ms=1, ttft_ms=1)

    def list_models(self):
        return [ModelInfo("scripted", 32768)]

    def abort(self):
        self.aborted.set()


def wipe_agent_tables():
    with app.LOCK:
        for t in schema.tables(app.DB):
            app.DB.execute("DROP TABLE IF EXISTS %s" % t)
        app.DB.commit()
    schema.reset()
    events.reset()


class AgentOn:
    """Context: AGENT_ENABLED=1, a password, a logged-in client (returns csrf),
    the agent switched on, inline runs. Use start()/stop() in setUp/tearDown."""

    def __init__(self):
        self.env = patch.dict(os.environ, {"AGENT_ENABLED": "1"})
        self.client = None
        self.csrf = None

    def start(self, enable=True):
        wipe_agent_tables()
        clear_login()
        self.env.start()
        self.client = app.app.test_client()
        self.csrf = login(self.client)
        from backend.agent import runs
        runs.use_inline(True)
        if enable:
            from backend.agent import state
            state.put({"enabled": "1"})
        return self

    def stop(self):
        from backend.agent import runs
        runs.use_inline(False)
        runs.reset()
        self.env.stop()
        clear_login()
        wipe_agent_tables()

    def h(self):
        return {"X-CSRF-Token": self.csrf}

    def post(self, path, body=None):
        return self.client.post(path, json=body or {}, headers=self.h())

    def put(self, path, body=None):
        return self.client.put(path, json=body or {}, headers=self.h())

    def patch(self, path, body=None):
        return self.client.patch(path, json=body or {}, headers=self.h())

    def delete(self, path, body=None):
        return self.client.delete(path, json=body or {}, headers=self.h())

    def get(self, path):
        return self.client.get(path)


class FakeFleet:
    """Registers hosts in the real hosts table and feeds HOST_DATA, so the agent
    sees a fleet without SSH. `runs` records every command the executor sent."""

    def __init__(self, hosts=None):
        self.hosts = hosts or {"lab1": {"family": "linux", "containers": ["web", "worker"], "services": ["nginx.service"]}}
        self.commands = []
        self.patches = []
        self.result = {"ok": True, "exit_code": 0, "stdout": "ok", "stderr": "", "ms": 5}

    def start(self):
        from backend.db.repos import hosts as hosts_repo
        with app.LOCK:
            for name in self.hosts:
                app.DB.execute("DELETE FROM hosts WHERE name=?", (name,))
                app.DB.execute("INSERT INTO hosts(name, ssh_target, tags, added_at) VALUES(?,?,?,?)",
                               (name, "lab@%s-addr:22" % name, "", int(time.time())))
            app.DB.commit()
        with app.HOST_DATA_LOCK:
            for name, h in self.hosts.items():
                app.HOST_DATA[name] = {"at": int(time.time()), "data": {"host": {
                    "os": {"family": h.get("family", "linux"), "id": h.get("os_id", "debian")},
                    "docker": {"available": True, "containers": [{"name": c, "state": "running"}
                                                                 for c in h.get("containers", [])]},
                    "systemd": {"available": True, "services": [{"name": s, "ports": h.get("ports", {}).get(s, [])}
                                                                for s in h.get("services", [])]}},
                    "model_catalog": h.get("catalog", []), "models": h.get("loaded", [])}}

        def run_on_host(host, cmd, sudo_password=None):
            self.commands.append((host, cmd))
            return dict(self.result)

        def run_on_host_windows(host, script):
            self.commands.append((host, "PS> " + script))
            return dict(self.result)
        self.patches = [patch("app.run_on_host", run_on_host), patch("app.run_on_host_windows", run_on_host_windows),
                        patch("app._host_is_online", lambda entry: True)]
        for p in self.patches:
            p.start()
        return self

    def stop(self):
        for p in self.patches:
            p.stop()
        with app.LOCK:
            for name in self.hosts:
                app.DB.execute("DELETE FROM hosts WHERE name=?", (name,))
            app.DB.commit()
        with app.HOST_DATA_LOCK:
            for name in self.hosts:
                app.HOST_DATA.pop(name, None)
