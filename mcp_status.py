"""Shared MCP activity status — read by Flask, written by the MCP server process.

A tiny JSON file under /data (or MCP_STATUS_PATH) is the IPC between the two
processes launched by launch.py. No background jobs; writers bump on each tool/
resource call, readers load on demand when /api/mcp-status is hit.
"""

import json
import os
import tempfile
import threading
import time

_DEFAULT_DIR = os.path.dirname(os.environ.get("DB_PATH", "/data/gpu.db"))
STATUS_PATH = os.environ.get("MCP_STATUS_PATH") or os.path.join(_DEFAULT_DIR, "mcp_status.json")

_LOCK = threading.Lock()
_IN_FLIGHT = 0


def _read_raw():
    try:
        with open(STATUS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write_raw(data):
    directory = os.path.dirname(STATUS_PATH) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".mcp_status_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, STATUS_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_activity():
    """Mark an MCP tool/resource invocation (thread-safe, in-process in_flight)."""
    global _IN_FLIGHT
    now = time.time()
    with _LOCK:
        _IN_FLIGHT += 1
        data = _read_raw()
        data["last_activity_ts"] = now
        data["total_requests"] = int(data.get("total_requests") or 0) + 1
        data["in_flight"] = _IN_FLIGHT
        _write_raw(data)


def clear_activity():
    """Decrement in-flight counter after a tool/resource finishes."""
    global _IN_FLIGHT
    with _LOCK:
        _IN_FLIGHT = max(0, _IN_FLIGHT - 1)
        data = _read_raw()
        data["in_flight"] = _IN_FLIGHT
        _write_raw(data)


def read_status():
    """Return the persisted status dict (empty when the file is missing)."""
    return _read_raw()
