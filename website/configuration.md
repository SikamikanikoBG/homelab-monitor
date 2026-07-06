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
| `ALLOW_SELF_UPDATE` | **on** | Adds an **Update now** button to the update modal that pulls the new image, recreates this container, and restarts it (rolling back automatically if the new version fails its health-check). Set to `0` to turn it off — see note below. |
| `SELF_UPDATE_HELPER_IMAGE` | `docker:cli` | Image used for the short-lived detached helper that runs `docker compose` to recreate the container during a self-update. Override only if `docker:cli` isn't reachable in your registry. |
| `MONITOR_IMAGE` | *(unset)* | Used **internally** by the self-update flow to pin an exact image — the versioned `:x.y.z` tag for the upgrade, the previous image ref/digest for a rollback. You normally never set this by hand; left unset, the shipped compose file falls back to the usual `sikamikaniko123/homelab-monitor:latest`. |
| `SSH_DIR` | `/data/.ssh` | Where the multi-host SSH keypair lives. Persists across rebuilds. |
| `ENABLE_CONTROLS` | **on** | Turns on the action buttons on the Containers and Services tabs (start/stop/restart, restart policy). Set to `0` to turn them off — see note below. |

### Write actions (`ALLOW_SELF_UPDATE`, `ENABLE_CONTROLS`)

Two things in the monitor **write**, not just read: the one-click self-update,
and the Containers/Services tabs' start/stop/restart controls. Both are **on
by default** in the shipped `docker-compose.yml` (which mounts the docker
socket and the systemd D-Bus socket read-write for exactly this reason). If
you'd rather this box stay pure read-only monitoring, bring it up with the
bundled opt-out override instead:

```bash
docker compose -f docker-compose.yml -f docker-compose.readonly.yml up -d
```

`docker-compose.readonly.yml` sets both env vars to `"0"` and puts both
sockets back to `:ro`. You can also flip either one independently by setting
just that env var — the socket permissions only matter for whichever write
path you actually leave enabled.

#### One-click self-update

When a newer release exists, the update modal shows an **Update now**
button. On click (after a confirm) it pulls the new image, launches a detached
`docker:cli` helper that recreates this container via your compose file, and the
helper health-checks the result: if the new version doesn't report itself healthy
within ~60s it rolls back to the image that was running. The dashboard streams the
log live and reloads itself once the new version is up.

Requirements / caveats:

- The docker socket must be mounted **read-write** (not `:ro`) — the default
  as of the shipped `docker-compose.yml`.
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

#### Container & service controls

What's honest about what it can actually do, by host:

- **Local host** (the machine running this container): full control of
  containers (start/stop/restart, restart policy) and systemd services
  (start/stop/restart), once the sockets above are writable.
- **Remote Linux/Unix hosts** (registered on the Hosts tab): service control
  only, over the same SSH connection used to poll them — `systemctl` under the
  hood, with an optional one-time sudo password (same handling as the Hosts
  tab's "Run on remote": piped to `sudo -S` over the encrypted channel, never
  stored or logged). **Container control isn't available for remote hosts** —
  the Containers tab doesn't have a remote container inventory yet (that's a
  separate, bigger piece of work — see [multi-host](multi-host.md)), so there's
  nothing here to control on a remote box today.
- **Remote Windows hosts**: service control over SSH + PowerShell
  (`Start-Service`/`Stop-Service`/`Restart-Service`). Whether it actually
  succeeds depends on which `authorized_keys` file got the hub's key during
  onboarding — a plain user account can't manage services, an admin account
  can. A permission failure shows the Windows error text as-is rather than
  guessing in advance.

Every action button is disabled with a lock icon when `ENABLE_CONTROLS` is off,
and a container/service that isn't controllable for one of the reasons above
simply doesn't pretend otherwise — you'll see the real error (from Docker,
systemd, or the remote shell) rather than a generic failure.

## Alerts (configured in the UI)

Open the **Alerts** tab and fill in any channel:

- **Discord webhook URL** (works with any Discord channel webhook)
- **ntfy.sh topic** (use the public server or self-hosted)
- **Telegram bot** — bot token + chat ID (legacy Markdown)
- **Email (SMTP)** — host, port (default 587), TLS toggle, from/to address, optional username/password. TLS on port 587 uses STARTTLS; port 465 uses implicit TLS.
- **Slack webhook URL** — incoming webhook from any Slack workspace
- **Generic webhook URL** — POSTs JSON `{level, title, detail, host}` to any endpoint (Teams, Gotify, n8n, …)

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
