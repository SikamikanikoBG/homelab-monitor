"""The read tools, declared once (#328).

These are exactly the 20 functions the MCP server exposes from
`homelab_client` — same names, same parameters (a test parses mcp/server.py
and checks) — plus an optional `host` on get_containers / get_services for the
in-app agent, which works across the fleet.

`homelab_client` stays where MCP needs it (mcp/ in the repo, /app in the
image); `client()` finds it in either place. The agent calls it in-process:
its transport is a Flask test client marked as the internal read-only caller,
so the agent and MCP share every line of fetching and shaping.
"""
import importlib.util
import os
import sys

from backend.tools import ToolSpec

_RANGE = {"type": "string", "enum": ["1h", "6h", "24h", "7d", "30d"], "default": "6h",
          "description": "Time window."}
_HOST = {"type": "string", "maxLength": 128,
         "description": "Host name from list_hosts, or \"local\" for the hub itself."}


def client():
    """The homelab_client module, imported once from wherever it ships."""
    mod = sys.modules.get("homelab_client")
    if mod is not None:
        return mod
    try:
        import homelab_client as mod          # the image: /app/homelab_client.py
        return mod
    except ImportError:
        pass
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(here, "mcp", "homelab_client.py")
    spec = importlib.util.spec_from_file_location("homelab_client", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["homelab_client"] = mod
    spec.loader.exec_module(mod)
    return mod


def _obj(props=None, required=()):
    return {"type": "object", "additionalProperties": False, "properties": props or {},
            "required": list(required)}


def _call(fname):
    def handler(**kw):
        return getattr(client(), fname)(**kw)
    handler.__name__ = fname
    return handler


def _containers(host="local"):
    hc = client()
    if host in (None, "", "local"):
        return hc.get_containers()
    d = hc.get_host(host)
    docker = ((d.get("host") or {}).get("docker") or {})
    return {"host": host, "online": d.get("online"), "available": docker.get("available"),
            "containers": docker.get("containers") or [], "error": d.get("error")}


def _services(host="local"):
    hc = client()
    if host in (None, "", "local"):
        return hc.get_services()
    d = hc.get_host(host)
    sd = ((d.get("host") or {}).get("systemd") or {})
    return {"host": host, "online": d.get("online"), "available": sd.get("available"),
            "services": sd.get("services") or [], "error": d.get("error")}


def _scan_disk(path="/", rescan=False):
    return client().scan_disk(path=path, rescan=rescan, max_wait=20)


READ_TOOLS = [
    ToolSpec("list_hosts", "Start here. Every host: online, CPU, RAM, disks, containers; plus `attention`, "
             "the active events the dashboard's Overview shows. One call covers the fleet.",
             _obj(), _call("list_hosts"), host_scope="fleet"),
    ToolSpec("get_host", "Full System/Network/Security inventory of one host, including its Docker "
             "containers and systemd services. Large: use it when one host needs a closer look.",
             _obj({"name": _HOST}, ["name"]), _call("get_host"), host_scope="arg", host_arg="name"),
    ToolSpec("get_snapshot", "The hub's live overview: GPU, host, problem containers and failed services.",
             _obj(), _call("get_snapshot"), host_scope="hub"),
    ToolSpec("get_containers", "Every Docker container with state, health, RAM, VRAM and disk. "
             "`host` picks the machine (default: the hub).",
             _obj({"host": dict(_HOST, default="local")}), _containers,
             host_scope="host_opt", host_arg="host", agent_only_params=("host",)),
    ToolSpec("get_services", "Every systemd unit with state, ports and RAM. `host` picks the machine "
             "(default: the hub).",
             _obj({"host": dict(_HOST, default="local")}), _services,
             host_scope="host_opt", host_arg="host", agent_only_params=("host",)),
    ToolSpec("get_memory", "RAM breakdown per service and process on the hub over a time window.",
             _obj({"range": _RANGE}), _call("get_memory"), host_scope="hub"),
    ToolSpec("get_gpu", "GPU utilisation, VRAM per model and who drives it, on the hub.",
             _obj({"range": _RANGE}), _call("get_gpu"), host_scope="hub"),
    ToolSpec("get_history", "Charted time series of the hub (GPU, CPU, RAM, load, temperatures).",
             _obj({"range": _RANGE}), _call("get_history"), host_scope="hub"),
    ToolSpec("scan_disk", "Folder-size treemap of a path on the hub (largest folders first). Slow and deep: "
             "only to find what fills a disk the overview showed nearly full, or when asked.",
             _obj({"path": {"type": "string", "maxLength": 512, "default": "/"},
                   "rescan": {"type": "boolean", "default": False}}), _scan_disk, host_scope="hub"),
    ToolSpec("get_ai_models", "Loaded model servers, their VRAM and callers.",
             _obj({"range": _RANGE}), _call("get_ai_models"), host_scope="fleet"),
    ToolSpec("get_installed_models", "Every AI model installed across the fleet, by host and provider, "
             "with size and whether it is loaded.",
             _obj(), _call("get_installed_models"), host_scope="fleet"),
    ToolSpec("get_costs", "What each machine cost in power over a window; `host` narrows to one.",
             _obj({"range": dict(_RANGE, default="7d"), "host": {"type": "string", "maxLength": 128, "default": ""}}),
             _call("get_costs"), host_scope="fleet"),
    ToolSpec("get_entity_cost", "Power cost of one process, container, service or model.",
             _obj({"name": {"type": "string", "maxLength": 200},
                   "kind": {"type": "string", "maxLength": 40, "default": ""},
                   "range": dict(_RANGE, default="7d")}, ["name"]),
             _call("get_entity_cost"), host_scope="fleet"),
    ToolSpec("get_experiments", "Tracked training/eval runs and the GPU energy over each run.",
             _obj({"range": dict(_RANGE, default="7d"), "status": {"type": "string", "maxLength": 20, "default": ""}}),
             _call("get_experiments"), host_scope="fleet"),
    ToolSpec("get_experiment", "One tracked run with its metrics.",
             _obj({"run_id": {"type": "string", "maxLength": 128}}, ["run_id"]),
             _call("get_experiment"), host_scope="fleet"),
    ToolSpec("get_benchmarks", "Stored LLM benchmarks: tokens/sec, what fits in VRAM, best context size.",
             _obj({"range": dict(_RANGE, default="30d"), "model": {"type": "string", "maxLength": 200, "default": ""}}),
             _call("get_benchmarks"), host_scope="fleet"),
    ToolSpec("get_benchmark", "One stored benchmark in full.",
             _obj({"run_id": {"type": "string", "maxLength": 128}}, ["run_id"]),
             _call("get_benchmark"), host_scope="fleet"),
    ToolSpec("get_events", "OOM kills and other events over a window.",
             _obj({"range": _RANGE}), _call("get_events"), host_scope="fleet"),
    ToolSpec("get_uptime", "Uptime checks with their state, response times and certificate expiry.",
             _obj({"range": dict(_RANGE, default="24h")}), _call("get_uptime"), host_scope="fleet"),
    ToolSpec("get_alerts", "Threshold crossings and alerts over a window.",
             _obj({"range": _RANGE}), _call("get_alerts"), host_scope="fleet"),
]

BY_NAME = {t.name: t for t in READ_TOOLS}
