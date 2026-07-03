#!/usr/bin/env python3
"""HomeLab Monitor — MCP server (issue #70).

A thin, well-described Model Context Protocol wrapper over the monitor's existing
**read-only** HTTP endpoints, so Claude (or any MCP-capable client) can connect to
the monitor and explore the whole homelab: hosts, containers, systemd services,
GPU, AI model servers, alerts and host posture.

All the actual HTTP + payload-trimming lives in `homelab_client.py` (pure stdlib,
no `mcp` dependency, unit-tested on its own). This file just registers each of
those functions as an MCP tool/resource and picks a transport.

Transports (env `MCP_TRANSPORT`):
  * `stdio` (default)            — for `claude mcp add` / Claude Desktop.
  * `http` / `streamable-http`   — for the optional docker-compose sidecar; listens
                                    on `MCP_HOST`:`MCP_PORT` (default 0.0.0.0:9810).

Config:
  * `HOMELAB_MONITOR_URL`  base URL of the monitor   (default http://localhost:9800)
  * `HOMELAB_HTTP_TIMEOUT` per-request timeout, sec  (default 10)
  * `MCP_ALLOWED_HOSTS`    comma-separated Host allow-list for the HTTP transport
                           (e.g. "YOUR-HUB:9810"; `host:*` wildcards ok). Unset →
                           the SDK's localhost-only DNS-rebinding check is turned
                           OFF so the server is reachable across the homelab by
                           name/IP, matching the documented `claude mcp add …
                           http://YOUR-HUB:9810/mcp`. Set it to lock back down.
  * `MCP_ALLOWED_ORIGINS`  comma-separated Origin allow-list (only consulted when
                           `MCP_ALLOWED_HOSTS` is set).

Guardrails: **read-only**. There are no write tools. Any future write capability
(run a probe, restart a container, apply an OS update) must be opt-in, clearly
labelled destructive, and gated behind explicit config — per issue #70.
"""

import functools
import json
import os
import sys

# Make `import homelab_client` / `mcp_status` work regardless of the caller's cwd.
_here = os.path.dirname(os.path.abspath(__file__))
for _p in (_here, os.path.dirname(_here)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import homelab_client as hc  # noqa: E402
import mcp_status as ms  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402

INSTRUCTIONS = (
    "Read-only access to a HomeLab Monitor instance — everything the dashboard shows "
    "is reachable here. Orientation: `list_hosts` (fleet) → `get_host` (one machine's "
    "full System/Network/Security inventory) → `get_snapshot` (live overview). Detail "
    "tools: `get_containers` and `get_services` (full Docker/systemd lists), "
    "`get_memory` (per-service/per-process RAM breakdown), `get_gpu` (utilisation, "
    "per-model VRAM, and who's driving it), `get_ai_models` (loaded models), "
    "`get_installed_models` (every model available, by provider — not just loaded), "
    "`get_history` (charted time-series), `get_costs`/`get_entity_cost` (power "
    "turned into money, per machine and per process/container/service/model), "
    "`get_experiments`/`get_experiment` (tracked runs priced by the GPU energy they "
    "burned), `get_events`/`get_alerts` (OOM kills / threshold crossings), and "
    "`scan_disk(path)` (WizTree-style folder treemap). "
    "Resources expose Prometheus `/metrics`, `/healthz` and the CHANGELOG. This "
    "server never mutates the fleet."
)

mcp = FastMCP("homelab-monitor", instructions=INSTRUCTIONS)


def _track(fn):
    """Bump the shared MCP activity file on every tool/resource invocation."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        ms.record_activity()
        try:
            return fn(*args, **kwargs)
        finally:
            ms.clear_activity()
    return wrapper


# ── tools (read-only) ─────────────────────────────────────────────────────────

@mcp.tool()
@_track
def list_hosts() -> dict:
    """List every host in the fleet with headline vitals (hub listed first).

    Returns the roster: name, online status, OS, CPU/RAM load, fullest disk and any
    OS-upgrade/reboot hint per host. Start here, then drill in with `get_host`.
    """
    return hc.list_hosts()


@mcp.tool()
@_track
def get_host(name: str) -> dict:
    """Full System / Network / Security inventory for a single host.

    `name` is the host's registered name, or "local" for the hub itself.
    """
    return hc.get_host(name)


@mcp.tool()
@_track
def get_snapshot() -> dict:
    """Current live vitals across the hub: GPU, host RAM/CPU, Docker and systemd
    health summaries (with any problem containers / failed units), pending OS
    updates and whether a monitor update is available. DB-free and cheap.
    """
    return hc.get_snapshot()


@mcp.tool()
@_track
def get_containers() -> dict:
    """Full Docker container list (Containers tab): per container name, image, state,
    health, exposed ports, RAM (`mem_bytes`), VRAM (`vram_bytes`), image disk and
    uptime — plus the summary counts.
    """
    return hc.get_containers()


@mcp.tool()
@_track
def get_services() -> dict:
    """Full systemd unit list (Services tab): per unit active/sub state, description,
    listening ports, RAM, uptime, admin/watched flags and status verdict — plus the
    summary counts.
    """
    return hc.get_services()


@mcp.tool()
@_track
def get_memory(range: str = "6h") -> dict:
    """RAM breakdown behind the memory treemap: per-service peak/avg/present RAM over
    `range`, the current RAM per process right now, kernel (non-reclaimable) memory,
    and totals. Answers "what is eating RAM on this hub?".
    """
    return hc.get_memory(range)


@mcp.tool()
@_track
def get_gpu(range: str = "6h") -> dict:
    """GPU detail: current utilisation, VRAM used/total, power and temperature, plus
    per-model VRAM use and the caller→server attribution over `range`. Answers "why
    is the GPU pinned, and which service is calling the model server?".
    """
    return hc.get_gpu(range)


@mcp.tool()
@_track
def get_history(range: str = "6h") -> dict:
    """Charted time-series the dashboard graphs over `range`: timestamps + aligned
    arrays for GPU util/VRAM/power/temp and host CPU/RAM/load/temp. Use for trends.
    """
    return hc.get_history(range)


@mcp.tool()
@_track
def scan_disk(path: str = "/", rescan: bool = False) -> dict:
    """WizTree-style nested folder-size treemap for a host path (Disks tab). Wraps the
    monitor's on-demand scan and polls until done. `path` is an absolute host path
    (e.g. "/", "/var"); set `rescan=True` to force a fresh scan. Returns total/free
    bytes and a nested `entries` tree of {name, path, bytes, children}.
    """
    return hc.scan_disk(path, rescan)


@mcp.tool()
@_track
def get_ai_models(range: str = "6h") -> dict:
    """AI model servers: which models are loaded, their VRAM use, and who is driving
    them (caller→server connection-seconds attribution over `range`, e.g. "6h",
    "24h", "7d"). Answers "why is the GPU pinned, and which service is calling it?".
    """
    return hc.get_ai_models(range)


@mcp.tool()
@_track
def get_installed_models() -> dict:
    """Installed-models registry (#219): every AI model available on the hub —
    grouped by provider (ollama, vllm, llama.cpp, LM Studio, ComfyUI, …) — not just
    what's currently loaded. Ollama entries carry on-disk size/quant/param detail;
    other providers carry name/loaded/vram. Answers "what can I run, and where?"
    without SSHing in to run `ollama list` or poke a server's API by hand.
    """
    return hc.get_installed_models()


@mcp.tool()
@_track
def get_costs(range: str = "7d") -> dict:
    """Power-cost summary for the hub (Costs tab): what the machine drew and what it
    cost over `range`, with the live `tariff`, the `machine` totals (now/energy/cost
    for today, 7d, 30d and the range) and a ranked `breakdown` of which processes,
    containers, services and models cost the most. Answers "what did my homelab cost,
    and what's the biggest line item?".
    """
    return hc.get_costs(range)


@mcp.tool()
@_track
def get_entity_cost(name: str, kind: str = "", range: str = "7d") -> dict:
    """Cost drill-down for one process/container/service/model by `name` (use a
    `kind`/`name` pair from `get_costs`' breakdown; `kind` is optional). Returns its
    `energy_kwh`, `cost`, `avg_w`/`peak_w` and resource use over `range`. Answers
    "what did *this* model or container cost me?".
    """
    return hc.get_entity_cost(name, kind, range)


@mcp.tool()
@_track
def get_experiments(range: str = "7d", status: str = "") -> dict:
    """Tracked training/eval runs (Experiments tab), each priced with the real GPU
    energy it burned. Optionally filter by `status` (running/finished/failed/killed).
    Each row carries its params, latest metrics (loss/accuracy…, plus throughput as
    `tokens_per_sec` when an LLM training script logged it), duration and cost.
    Answers "which runs ran, how did they do, what did each one cost, and how many
    tokens/sec were they pushing?".
    """
    return hc.get_experiments(range, status)


@mcp.tool()
@_track
def get_experiment(run_id: str) -> dict:
    """Full detail for one tracked run by `run_id` (from `get_experiments`): its
    logged-metric series (the loss curve, and a tokens/sec curve too if logged),
    the GPU power/util time-series over the run, and the priced energy it burned.
    An unknown id returns an HTTP 404 error.
    """
    return hc.get_experiment(run_id)


@mcp.tool()
@_track
def get_events(range: str = "6h") -> dict:
    """Recent edge-triggered events over `range` (OOM kills, threshold crossings),
    each with a blame line where one can be attributed, plus derived insights.
    """
    return hc.get_events(range)


@mcp.tool()
@_track
def get_alerts(range: str = "6h") -> dict:
    """Alias for `get_events` — the monitor's alerts are its edge-triggered events."""
    return hc.get_alerts(range)


# ── resources ────────────────────────────────────────────────────────────────

@mcp.resource("homelab://metrics", mime_type="text/plain")
@_track
def metrics_resource() -> str:
    """Prometheus exposition text from the monitor's /metrics endpoint."""
    return hc.get_metrics()


@mcp.resource("homelab://health", mime_type="application/json")
@_track
def health_resource() -> str:
    """Monitor liveness + running version (from /healthz)."""
    return json.dumps(hc.get_version(), indent=2)


def _changelog_path():
    """CHANGELOG.md location: explicit env, else bundled next to this file / in /app."""
    candidates = [
        os.environ.get("HOMELAB_CHANGELOG_PATH"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "CHANGELOG.md"),
        "/app/CHANGELOG.md",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


@mcp.resource("homelab://changelog", mime_type="text/markdown")
@_track
def changelog_resource() -> str:
    """The monitor's CHANGELOG, bundled into the image, for version context."""
    path = _changelog_path()
    if not path:
        return "# CHANGELOG unavailable\nNo CHANGELOG.md was bundled with this MCP server."
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


# ── entrypoint ───────────────────────────────────────────────────────────────

def _transport_security():
    """DNS-rebinding protection for the HTTP/SSE transport.

    The MCP SDK ships a Host/Origin allow-list that, by default, only accepts
    `localhost`/`127.0.0.1`. That's the right default for a stdio server a browser
    might reach, but this transport deliberately binds 0.0.0.0 so the homelab can
    reach it — under that default it returns **421 Misdirected Request** to anyone
    connecting by hostname or IP, breaking the documented `claude mcp add …
    http://YOUR-HUB:9810/mcp`.

    So: if `MCP_ALLOWED_HOSTS` is set we honour it (exact or `host:*` patterns,
    plus matching `MCP_ALLOWED_ORIGINS`); otherwise we turn the check off, which
    is safe here because every tool is read-only.
    """
    hosts = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if not hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    origins = [o.strip() for o in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def main():
    transport = (os.environ.get("MCP_TRANSPORT") or "stdio").strip().lower()
    if transport in ("http", "streamable-http", "streamable_http"):
        mcp.settings.host = os.environ.get("MCP_HOST", "0.0.0.0")
        mcp.settings.port = int(os.environ.get("MCP_PORT", "9810"))
        mcp.settings.transport_security = _transport_security()
        print("homelab-monitor MCP on http://%s:%s/mcp -> %s"
              % (mcp.settings.host, mcp.settings.port, hc.base_url()), file=sys.stderr)
        mcp.run(transport="streamable-http")
    elif transport == "sse":
        mcp.settings.host = os.environ.get("MCP_HOST", "0.0.0.0")
        mcp.settings.port = int(os.environ.get("MCP_PORT", "9810"))
        mcp.settings.transport_security = _transport_security()
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
