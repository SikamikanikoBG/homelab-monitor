# Configuration

Almost nothing needs to be configured to get started. Two layers exist for
when you do want to tune things:

1. **Environment variables** for sample cadence, retention, paths.
2. **The Settings tab in the UI** for alerts (saved into SQLite, no env vars
   or config files).

## Environment variables

Set these under `environment:` in `docker-compose.yml`. All optional.

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `9800` | Dashboard listens on `0.0.0.0:$PORT`. With host networking, this is the LAN port too. |
| `SAMPLE_INTERVAL` | `10` | Seconds between collector cycles (also the multi-host probe cadence). |
| `RETENTION_DAYS` | `180` | How long SQLite history is kept. Downsampled on read, so longer ranges stay cheap. |
| `PRESSURE_FREE_MB` | `2048` | Free VRAM below this counts as "pressure" for the insights / alerts. |
| `HOST_ROOT` | `/rootfs` | Where host `/` is bind-mounted into the container (for disk usage). |
| `DOCKER_SOCK` | `/var/run/docker.sock` | Path to the Docker socket inside the container. |
| `DB_PATH` | `/data/gpu.db` | SQLite history file. Default lives under the `./data` bind mount. |
| `WATCH_CONTAINERS` | *(empty)* | Comma-separated container names to always scan for OOM events, even if not GPU-attributed. |
| `WATCH_SERVICES` | *(empty)* | Comma-separated systemd units to always surface in the Services tab. |
| `CHECK_UPDATES` | `true` | Whether to poll GitHub releases for "update available" banner. |
| `ALLOW_SELF_UPDATE` | *(off)* | Opt-in. Adds an **Update now** button to the update modal that pulls the new image, recreates this container, and restarts it (rolling back automatically if the new version fails its health-check). Off by default — see note below. |
| `SELF_UPDATE_HELPER_IMAGE` | `docker:cli` | Image used for the short-lived detached helper that runs `docker compose` to recreate the container during a self-update. Override only if `docker:cli` isn't reachable in your registry. |
| `MONITOR_IMAGE` | *(unset)* | Used **internally** by the self-update flow to pin an exact image — the versioned `:x.y.z` tag for the upgrade, the previous image ref/digest for a rollback. You normally never set this by hand; left unset, the shipped compose file falls back to the usual `sikamikaniko123/homelab-monitor:latest`. |
| `SSH_DIR` | `/data/.ssh` | Where the multi-host SSH keypair lives. Persists across rebuilds. |

### One-click self-update (`ALLOW_SELF_UPDATE`)

This is the first and only action in the monitor that **writes** — everything
else is read-only. It is therefore **off by default** and must be opted into
using the bundled override file:

```bash
docker compose -f docker-compose.yml -f docker-compose.self-update.yml up -d
```

`docker-compose.self-update.yml` sets `ALLOW_SELF_UPDATE: "1"` and upgrades the
docker socket from `:ro` to read-write (needed to create the update helper). The
main `docker-compose.yml` keeps `:ro` so plain monitoring deployments are
unaffected.

When enabled and a newer release exists, the update modal shows an **Update now**
button. On click (after a confirm) it pulls the new image, launches a detached
`docker:cli` helper that recreates this container via your compose file, and the
helper health-checks the result: if the new version doesn't report itself healthy
within ~60s it rolls back to the image that was running. The dashboard streams the
log live and reloads itself once the new version is up.

Requirements / caveats:

- The docker socket must be mounted **read-write** (not `:ro`) — handled
  automatically by the override file.
- The container must have been started with **docker compose** (the helper reads
  the compose project labels to know what to recreate). A plain `docker run`
  deploy is refused with a clear message — use the manual command instead.
- The container **restarts**, so the dashboard is briefly unavailable.
- For the upgrade to pull the **exact** target version (and for a rollback to
  restore the **exact** previous image), your compose file's image line must use
  the `image: ${MONITOR_IMAGE:-sikamikaniko123/homelab-monitor:latest}` form —
  which the shipped `docker-compose.yml` now does. The helper sets `MONITOR_IMAGE`
  to the immutable `:x.y.z` tag for the pull/up and to the previous image
  ref/digest on rollback; with a hardcoded `:latest` image line, pinning and
  rollback would silently degrade to re-pulling `:latest`.

## Alerts (configured in the UI)

Open the **Alerts** tab and fill in either or both:

- **Discord webhook URL** (works with any Discord channel webhook)
- **ntfy.sh topic** (use the public server or self-hosted)

Then set:

- **Minimum severity** — `warning` or `critical only`
- **Disk alert threshold** — fires when any real filesystem crosses this %

Alerts are **edge-triggered**: one ping per state change, not a flood. Each
alert key is remembered until the underlying condition recovers, then the
next failure re-fires exactly once.

## Triggers

Built-in triggers (no config needed beyond enabling alerts):

- Container goes unhealthy / exits non-zero / is dead
- systemd unit fails
- GPU VRAM pressure (free below `PRESSURE_FREE_MB`)
- GPU OOM events scraped from container logs
- Disk usage crossing the threshold above

Add your own by extending `dispatch_alert` in `app.py`.

## Compose excerpt

A trimmed `docker-compose.yml` for the curious — see the real one in
the [repo](https://github.com/SikamikanikoBG/homelab-monitor/blob/main/docker-compose.yml).

```yaml
services:
  homelab-monitor:
    # ${MONITOR_IMAGE:-…} lets the self-update flow pin/rollback an exact image
    image: ${MONITOR_IMAGE:-sikamikaniko123/homelab-monitor:latest}
    container_name: homelab-monitor
    restart: unless-stopped
    network_mode: host          # for direct LAN access + model-server APIs
    pid: host                   # to map GPU PIDs → containers
    environment:
      PORT: "9800"
      SAMPLE_INTERVAL: "10"
      RETENTION_DAYS: "180"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /:/rootfs:ro
      - ./data:/data
      - /run/dbus/system_bus_socket:/run/dbus/system_bus_socket:ro
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```
