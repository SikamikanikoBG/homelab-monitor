# Install

## Requirements

- **Docker** + **docker compose** (any modern version).
- For the GPU panels: an **NVIDIA GPU** and the
  [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
- **No GPU?** That's fine — the container, service and host panels still work.
  Skip the toolkit; the dashboard auto-hides the GPU sections.

## Option A — pre-built image (recommended)

No clone, no build:

```bash
curl -fsSLO https://raw.githubusercontent.com/SikamikanikoBG/homelab-monitor/main/docker-compose.yml
docker compose pull
docker compose up -d
```

Multi-arch images (`linux/amd64`, `linux/arm64`) are published on every release
to [`sikamikaniko123/homelab-monitor`](https://hub.docker.com/r/sikamikaniko123/homelab-monitor).

Open **`http://<your-host-ip>:9800`** from any device on your LAN or VPN.

??? tip "Upgrade later"
    ```bash
    docker compose pull
    docker compose up -d
    ```
    Your SQLite history at `./data/gpu.db` survives — it's a bind mount.

??? tip "Updating from the dashboard (opt-in)"
    The commands above are the default way to update. If you'd rather press a
    button, use the bundled override file:
    ```bash
    docker compose -f docker-compose.yml -f docker-compose.self-update.yml up -d
    ```
    When a newer release exists, the update modal then shows an **Update now**
    button that pulls the new image, recreates the container, and rolls back
    automatically if the new version fails its health-check. It's off by default
    — see [Configuration → One-click self-update](configuration.md#one-click-self-update-allow_self_update).

## Option B — from source

Handy if you're tweaking the code or contributing:

```bash
git clone https://github.com/SikamikanikoBG/homelab-monitor.git
cd homelab-monitor
docker compose up -d --build
```

Same URL: **`http://<your-host-ip>:9800`**.

## Verify

```bash
curl -s http://localhost:9800/healthz
# {"status":"ok","version":"0.8.0"}
```

## Uninstall

```bash
docker compose down
# To also drop the SQLite history:
rm -rf ./data
```

## Running on Windows (WSL2 — no Docker Desktop required)

The dashboard is a Linux container, but it runs happily on **Windows 10/11** through
**WSL2** — and you don't need the heavyweight Docker Desktop. Install the Docker Engine
straight into a WSL distro instead:

```powershell
# In PowerShell — install WSL2 if you don't have it yet (one-time, reboot if asked):
wsl --install
```

```bash
# Then, inside your WSL (Ubuntu) shell — install Docker Engine + Compose:
curl -fsSL https://get.docker.com | sh

# Enable systemd so dockerd runs as a service (one-time):
printf '[boot]\nsystemd=true\n' | sudo tee /etc/wsl.conf   # then: wsl --shutdown, reopen

curl -fsSLO https://raw.githubusercontent.com/SikamikanikoBG/homelab-monitor/main/docker-compose.yml
docker compose up -d
```

WSL2 forwards the port to Windows automatically, so the dashboard is reachable at
**`http://localhost:9800`** in your Windows browser. To keep Docker data off your `C:`
drive, give the distro its own home on another drive with `wsl --export` / `wsl --import`.

!!! tip "Want to *monitor* a Windows box (not run the hub on it)?"
    You don't need any of the above — just enable OpenSSH Server on the Windows machine
    and add it on the **Hosts** tab. See [Multi-machine → Windows hosts](multi-host.md#windows-hosts).

## Next steps

- Add your other boxes to the cockpit → [**Multi-machine guide**](multi-host.md)
- Tune sample interval, retention, alert thresholds → [**Configuration**](configuration.md)
- See every panel → [**Features tour**](features.md)
