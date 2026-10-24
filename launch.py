#!/usr/bin/env python3
"""Container entrypoint — run the monitor (Flask) and the MCP server together.

One image, one container: the dashboard on ``$PORT`` (default 9800) and the MCP
server on ``$MCP_PORT`` (default 9810). The monitor is the critical process — if it
exits, the container exits so Docker's restart policy kicks in. The MCP server is
best-effort: if it crashes it's respawned (with backoff) without taking the
dashboard down. Set ``ENABLE_MCP=0`` to run the dashboard alone.
"""

import os
import secrets
import signal
import subprocess
import sys
import time

PY = sys.executable
_procs = {}
_shutting = False


def _start_monitor():
    return subprocess.Popen([PY, "/app/app.py"])


# A per-container secret the dashboard accepts, read-only and from loopback
# only, so the built-in MCP server keeps working once a login is set (#354).
# Generated fresh on every start; it never leaves the container.
os.environ.setdefault("HLM_INTERNAL_TOKEN", secrets.token_urlsafe(32))


def _start_mcp():
    env = os.environ.copy()
    # The MCP server reads the monitor over HTTP — in-container that's localhost.
    env.setdefault("HOMELAB_MONITOR_URL", "http://localhost:%s" % os.environ.get("PORT", "9800"))
    env.setdefault("MCP_TRANSPORT", "http")
    env.setdefault("MCP_HOST", "0.0.0.0")
    env.setdefault("MCP_PORT", "9810")
    return subprocess.Popen([PY, "/app/mcp_server.py"], env=env)


def _shutdown(signum=None, frame=None):
    global _shutting
    _shutting = True
    for p in _procs.values():
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
    deadline = time.time() + 8
    while time.time() < deadline:
        if all(p is None or p.poll() is not None for p in _procs.values()):
            break
        time.sleep(0.2)
    for p in _procs.values():
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass


def main():
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    _procs["monitor"] = _start_monitor()
    enable_mcp = os.environ.get("ENABLE_MCP", "1").strip().lower() not in ("0", "false", "no")
    if enable_mcp:
        _procs["mcp"] = _start_mcp()
    else:
        print("ENABLE_MCP=0 — MCP server not started", flush=True)

    mcp_fails = 0
    while not _shutting:
        rc = _procs["monitor"].poll()
        if rc is not None:
            print("monitor process exited rc=%s — stopping container" % rc, flush=True)
            _shutdown()
            sys.exit(rc or 1)
        if enable_mcp:
            m = _procs.get("mcp")
            if m is None or m.poll() is not None:
                if m is not None:
                    print("MCP server exited rc=%s — respawning" % m.poll(), flush=True)
                mcp_fails += 1
                if mcp_fails <= 20:
                    time.sleep(min(30, 2 * mcp_fails))
                    if not _shutting:
                        _procs["mcp"] = _start_mcp()
                else:
                    print("MCP server failed repeatedly — leaving the dashboard running without it",
                          flush=True)
                    enable_mcp = False
        time.sleep(2)


if __name__ == "__main__":
    main()
