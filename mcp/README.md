# HomeLab Monitor — MCP server

A small [Model Context Protocol](https://modelcontextprotocol.io) server that lets
Claude (or any MCP client) **connect to a running HomeLab Monitor and explore the
whole homelab** — hosts, containers, systemd services, GPU, AI model servers,
alerts and host posture.

It's a thin, well-described wrapper over the monitor's existing **read-only** HTTP
endpoints. No collectors are touched and **nothing is mutated** — see the guardrails
below.

> **It ships inside the monitor image.** Since v0.14.0 the server runs in the same
> container as the dashboard, on `MCP_PORT` (default `9810`). You usually don't run
> anything from this folder — just connect:
> `claude mcp add --transport http homelab http://YOUR-HUB:9810/mcp`.
> The files here are the source (and let you run it standalone for dev).

## Tools

Everything the dashboard shows is reachable. Start with `list_hosts` → `get_host` →
`get_snapshot`, then drill in with the detail tools.

| Tool | What it answers | Wraps |
|------|-----------------|-------|
| `list_hosts()` | What's in the fleet and is it healthy? | `GET /api/fleet` |
| `get_host(name)` | One host's System / Network / Security inventory (`"local"` = the hub) | `GET /api/host_data/<name>` |
| `get_snapshot()` | Live GPU / host / Docker / systemd overview + diagnostics | `GET /api/health` |
| `get_containers()` | Full Docker list: state, health, ports, RAM/VRAM, image disk, uptime | `GET /api/health` |
| `get_services()` | Full systemd list: active/sub state, ports, RAM, admin/watched flags | `GET /api/health` |
| `get_memory(range="6h")` | Per-service & per-process RAM breakdown (the memory treemap) | `GET /api/data` |
| `get_gpu(range="6h")` | GPU util / VRAM / power / temp, per-model VRAM, caller attribution | `GET /api/data` |
| `get_ai_models(range="6h")` | Which models are loaded, VRAM, and *who is driving them* | `GET /api/data` |
| `get_installed_models()` | Every model available on the hub, by provider — not just loaded | `GET /api/models` |
| `get_history(range="6h")` | Charted time-series (GPU + host) for trends | `GET /api/data` |
| `get_events(range="6h")` / `get_alerts(...)` | Recent OOM kills / threshold crossings + insights | `GET /api/data` |
| `scan_disk(path="/", rescan=False)` | WizTree-style nested folder-size treemap | `GET /api/disk_scan` |

## Resources

| Resource | Content |
|----------|---------|
| `homelab://metrics` | Prometheus exposition text (`GET /metrics`) |
| `homelab://health` | Liveness + running version (`GET /healthz`) |
| `homelab://changelog` | The bundled CHANGELOG, for version context |

## Configure

| Env | Default | Meaning |
|-----|---------|---------|
| `HOMELAB_MONITOR_URL` | `http://localhost:9800` | Base URL of the monitor to read |
| `HOMELAB_HTTP_TIMEOUT` | `10` | Per-request timeout (seconds) |
| `MCP_TRANSPORT` | `stdio` | `stdio`, or `http` (streamable-http) for the sidecar |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `9810` | Bind address for the `http` transport |

## Run it

### A. Built-in (recommended)

The monitor image already runs this server on `MCP_PORT` (default `9810`). Just
connect:

```bash
claude mcp add --transport http homelab http://YOUR-HUB:9810/mcp
```

### B. Local Python (dev, stdio)

Run it from a checkout against any monitor:

```bash
pip install -r requirements.txt   # Python 3.10+
HOMELAB_MONITOR_URL=http://YOUR-HUB:9800 python server.py
```

### C. Docker stdio against a remote monitor (advanced)

The same image can be driven over stdio:

```bash
claude mcp add homelab -- docker run -i --rm \
  -e HOMELAB_MONITOR_URL=http://YOUR-HUB:9800 -e MCP_TRANSPORT=stdio \
  sikamikaniko123/homelab-monitor python /app/mcp_server.py
```

## Guardrails

This server is **read-only**. There are no write tools. Any future write capability
(run a probe, restart a container, apply an OS update) must be opt-in, clearly
labelled destructive, and gated behind explicit config — same philosophy as the
rest of the project (issue #70).

## Tests

`homelab_client.py` is pure stdlib and unit-tested against a stub monitor, so the
endpoint-wrapping/trimming logic runs on any Python 3.8+:

```bash
python mcp/tests/test_client.py
```
