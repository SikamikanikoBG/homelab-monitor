---
title: MCP server — connect Claude, ChatGPT & any AI agent to your homelab
description: Connect Claude, ChatGPT or any MCP client to your homelab. HomeLab Monitor ships a read-only Model Context Protocol server with 16 tools — one line to connect.
---

# MCP server — connect Claude, ChatGPT & any AI agent

HomeLab Monitor ships a built-in **[Model Context Protocol (MCP)](https://modelcontextprotocol.io)
server** so an AI agent — Claude, ChatGPT, or any MCP-capable client — can connect to the
monitor and *explore the whole homelab ecosystem* through it: hosts, containers,
systemd services, GPU, AI model servers, alerts and host posture.

It's a thin, well-described wrapper over the monitor's existing **read-only** HTTP
endpoints. No collectors are touched, and **nothing is mutated**.

!!! info "Read-only by design"
    There are no write tools in the MCP server. The dashboard itself has two
    write actions — one-click self-update and the Containers/Services tabs'
    start/stop/restart controls — gated behind `ALLOW_SELF_UPDATE` /
    `ENABLE_CONTROLS` (on by default; set either to `0`, or use
    `docker-compose.readonly.yml`, to turn them off), each asking for
    confirmation before it acts. Neither is exposed as an MCP tool — this
    server stays read-only regardless of what the dashboard itself can do.

## Tools

Everything the dashboard shows is reachable through these tools. The usual path is
`list_hosts` → `get_host` → `get_snapshot`, then a detail tool.

| Tool | What it answers | Wraps |
|------|-----------------|-------|
| `list_hosts()` | What's in the fleet, and is it healthy? | `/api/fleet` |
| `get_host(name)` | One host's System / Network / Security inventory (`"local"` = the hub) | `/api/host_data/<name>` |
| `get_snapshot()` | Live GPU / host / Docker / systemd overview + diagnostics | `/api/health` |
| `get_containers()` | Full Docker list — state, health, ports, RAM/VRAM, image disk, uptime | `/api/health` |
| `get_services()` | Full systemd list — active/sub state, ports, RAM, admin/watched flags | `/api/health` |
| `get_memory(range)` | Per-service & per-process RAM breakdown (the memory treemap) | `/api/data` |
| `get_gpu(range)` | GPU util / VRAM / power / temp, per-model VRAM, caller attribution | `/api/data` |
| `get_ai_models(range)` | Which models are loaded, their VRAM, and *who is driving them* | `/api/data` |
| `get_history(range)` | Charted time-series (GPU + host) for trends | `/api/data` |
| `get_costs(range)` | What the machine drew and cost, + a ranked per-process/container/service/model breakdown | `/api/costs` |
| `get_entity_cost(name, kind, range)` | Cost drill-down for one process/container/service/model | `/api/costs/entity` |
| `get_experiments(range, status)` | Tracked runs, each priced by the real GPU energy it burned | `/api/runs` |
| `get_experiment(run_id)` | One run's loss-curve metrics, GPU power/util series and priced energy | `/api/runs/<id>` |
| `get_events(range)` / `get_alerts(range)` | Recent OOM kills / threshold crossings + insights | `/api/data` |
| `scan_disk(path, rescan)` | WizTree-style nested folder-size treemap | `/api/disk_scan` |

`range` accepts the same windows as the dashboard, e.g. `6h`, `24h`, `7d`.
`scan_disk` takes an absolute host path (e.g. `/`, `/var`) and polls the background
scan until it's done.

## Resources

| Resource | Content |
|----------|---------|
| `homelab://metrics` | Prometheus exposition text (`/metrics`) |
| `homelab://health` | Liveness + running version (`/healthz`) |
| `homelab://changelog` | The bundled CHANGELOG, for version context |

## Configuration

| Env | Default | Meaning |
|-----|---------|---------|
| `HOMELAB_MONITOR_URL` | `http://localhost:9800` | Base URL of the monitor to read |
| `HOMELAB_HTTP_TIMEOUT` | `10` | Per-request timeout (seconds) |
| `MCP_TRANSPORT` | `stdio` | `stdio`, or `http` (streamable-http) for the sidecar |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `9810` | Bind address for the `http` transport |

## Run it

The MCP server is **built into the monitor image** and served on `MCP_PORT`
(default `9810`) alongside the dashboard — there's nothing extra to deploy. Set
`ENABLE_MCP=0` to turn it off.

=== "Connect (built-in, HTTP)"

    ```bash
    # the dashboard is already running on :9800; the MCP server is on :9810
    claude mcp add --transport http homelab http://YOUR-HUB:9810/mcp
    ```

=== "Local Python (dev, stdio)"

    Run the server straight from a checkout against any monitor:

    ```bash
    pip install -r mcp/requirements.txt   # Python 3.10+
    HOMELAB_MONITOR_URL=http://YOUR-HUB:9800 python mcp/server.py
    ```

=== "Docker stdio (advanced)"

    The same image can also be driven over stdio against a remote monitor:

    ```bash
    claude mcp add homelab -- docker run -i --rm \
      -e HOMELAB_MONITOR_URL=http://YOUR-HUB:9800 -e MCP_TRANSPORT=stdio \
      sikamikaniko123/homelab-monitor python /app/mcp_server.py
    ```

## Try it

Once connected, ask your agent natural questions and let it pick the tools:

- *"Which host has a reboot pending **and** an OS upgrade available — what's the safe order to apply it?"*
- *"Why is the GPU pinned right now, and which service is calling the model server?"*
- *"Any OOM kills in the last 24h? What got blamed?"*
- *"What did my homelab cost last night, and which model is the most expensive thing on the GPU?"*
- *"How much energy did my last training run burn, and what did it cost?"*

!!! warning "Keep it on your LAN/VPN"
    Like the dashboard, the MCP server gives broad visibility into your hosts.
    Don't expose either to the public internet.
