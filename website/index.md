---
description: A small, friendly self-hosted homelab dashboard — GPU, AI VRAM, Docker, systemd, host health — with a built-in read-only MCP server for Claude and any AI agent.
hide:
  - navigation
  - toc
---

<div class="hl-hero" markdown>

# <span class="hl-title">HomeLab <span class="em">Monitor</span></span>

<p class="hl-tag">
One small container for your home lab — GPU, AI VRAM, Docker, systemd and host
health on a single page, with every machine registered over SSH in one cockpit.
<strong>Now readable by AI agents too</strong>, through a built-in read-only
<strong>MCP server</strong>.
</p>

<div class="hl-cta">
  <a class="md-button md-button--primary" href="install/">Get started</a>
  <a class="md-button" href="https://github.com/SikamikanikoBG/homelab-monitor" target="_blank" rel="noopener">GitHub →</a>
  <a class="md-button" href="https://hub.docker.com/r/sikamikaniko123/homelab-monitor" target="_blank" rel="noopener">Docker Hub →</a>
</div>

<div class="hl-video">
  <iframe src="https://www.youtube-nocookie.com/embed/5uf2rG-RzcU?rel=0"
          title="HomeLab Monitor — dashboard demo"
          loading="lazy"
          allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
          allowfullscreen></iframe>
</div>

<p class="hl-badges">
  <span><span class="hl-dot"></span>One container · <code>docker compose up -d</code></span>
  <span>No Prometheus / Grafana / cloud</span>
  <span>NVIDIA GPU friendly</span>
  <span>Pull <code>sikamikaniko123/homelab-monitor</code></span>
</p>

</div>

<div class="hl-section-title hl-reveal">What it does</div>

<div class="hl-features" markdown>

<div class="hl-feature hl-reveal delay-1" markdown>
### <span class="ico">🪄</span> Plug-and-play
One Docker container. No agents, no Prometheus/Grafana stack, no cloud account.
Sane defaults; everything else is in the Settings tab.
</div>

<div class="hl-feature hl-reveal delay-2" markdown>
### <span class="ico">🎮</span> GPU attribution
Live VRAM, utilisation, power, temperature — plus *which container or process*
is holding the card, mapped automatically via `/proc/<pid>/cgroup` + the Docker API.
</div>

<div class="hl-feature hl-reveal delay-3" markdown>
### <span class="ico">🧠</span> AI model awareness
Detects the major local-AI servers (Ollama, vLLM, TGI, llama.cpp, A1111,
ComfyUI) and reports *which model is loaded* with per-model VRAM.
</div>

<div class="hl-feature hl-reveal delay-4" markdown>
### <span class="ico">📦</span> Containers &amp; services
Health of every Docker container and every systemd service in one glance.
Your own units highlighted, failed ones surfaced first.
</div>

<div class="hl-feature hl-reveal delay-5" markdown>
### <span class="ico">🌐</span> Multi-machine, agentless
Register other boxes over SSH and they appear in the fleet table. Just `python3`
on the remote — nothing to install. [Walkthrough →](multi-host.md)
</div>

<div class="hl-feature hl-reveal delay-6" markdown>
### <span class="ico">🛡️</span> System, Network &amp; Security
Per host: OS, kernel, **architecture**, machine model and CPU/GPU; interfaces,
DNS and listening sockets with exposure flags; and a read-only security posture
check (firewall, SSH hardening, SELinux/AppArmor, fail2ban) — issues first.
</div>

<div class="hl-feature hl-reveal delay-6" markdown>
### <span class="ico">🔔</span> Alerts
Discord webhook or [ntfy.sh](https://ntfy.sh). Edge-triggered: one ping per
state change, never a spam flood. Configured from the UI.
</div>

<div class="hl-feature hl-reveal delay-1" markdown>
### <span class="ico">🤝</span> MCP for AI agents
A built-in, **read-only** [Model Context Protocol](https://modelcontextprotocol.io)
server lets **Claude, ChatGPT or any MCP client** explore your homelab — one line to
connect, nothing it can change. [More →](mcp.md)
</div>

</div>

<div class="hl-section-title hl-reveal">Now readable by your AI agent, too</div>

<div class="hl-mcp hl-reveal" markdown>

<p class="hl-mcp-pitch">
The same host, GPU, container and disk data you read on the dashboard is now exposed
over a built-in, <strong>read-only</strong> MCP server — so you can just <em>ask</em>
your agent instead of digging. One line connects
<strong>Claude</strong>, <strong>ChatGPT</strong> or any MCP client; nothing to write,
nothing it can change.
</p>

<img class="hl-mcp-diagram" src="assets/mcp-agents.svg" alt="HomeLab Monitor connects over MCP to Claude, ChatGPT, or any MCP client; read-only, both directions are question and answer">

<p class="hl-mcp-cap">Connect any MCP client — Claude, ChatGPT, or an agent running on your own local Ollama models — and it reads your homelab's live state. Read-only: both directions are just question and answer.</p>

```bash
# the dashboard is on :9800; the read-only MCP server rides along on :9810
claude mcp add --transport http homelab http://YOUR-HUB:9810/mcp
```

<p class="hl-mcp-prompts">Then just ask — the agent picks the right tools:</p>

- *"My GPU's been pinned for an hour — which model server is loaded, and who's actually calling it?"*
- *"What's eating `/backup`? Give me the biggest folders and flag anything that looks like runaway logs."*
- *"Which host is lowest on RAM right now, and what's the top process holding it?"*
- *"I want to reboot and run an OS upgrade this weekend — which box needs it most, and a safe order given what's running?"*

[Full tool list &amp; setup → MCP docs](mcp.md){ .md-button }

</div>

<div class="hl-section-title hl-reveal">A look around</div>

<div class="hl-shots" markdown>

<a class="hl-reveal delay-1" href="screenshots/overview.png" target="_blank">
  <img src="screenshots/overview.png" alt="Overview / All-hosts table">
  <span class="lbl">Overview — every host at a glance</span>
</a>

<a class="hl-reveal delay-2" href="screenshots/hosts.png" target="_blank">
  <img src="screenshots/hosts.png" alt="Hosts tab with onboarding wizard">
  <span class="lbl">Hosts — onboarding wizard + capability checklist</span>
</a>

<a class="hl-reveal delay-3" href="screenshots/gpu.png" target="_blank">
  <img src="screenshots/gpu.png" alt="GPU tab">
  <span class="lbl">GPU — VRAM attribution by service</span>
</a>

<a class="hl-reveal delay-4" href="screenshots/services.png" target="_blank">
  <img src="screenshots/services.png" alt="Services tab">
  <span class="lbl">Services — systemd, yours highlighted</span>
</a>

<a class="hl-reveal delay-5" href="screenshots/containers.png" target="_blank">
  <img src="screenshots/containers.png" alt="Containers tab">
  <span class="lbl">Containers — every Docker container</span>
</a>

<a class="hl-reveal delay-6" href="screenshots/system.png" target="_blank">
  <img src="screenshots/system.png" alt="System tab">
  <span class="lbl">System — OS, architecture &amp; hardware inventory</span>
</a>

<a class="hl-reveal delay-1" href="screenshots/network.png" target="_blank">
  <img src="screenshots/network.png" alt="Network tab">
  <span class="lbl">Network — interfaces, DNS &amp; listening sockets</span>
</a>

<a class="hl-reveal delay-2" href="screenshots/security.png" target="_blank">
  <img src="screenshots/security.png" alt="Security tab">
  <span class="lbl">Security — posture check, issues surfaced first</span>
</a>

</div>

<div class="hl-section-title hl-reveal">60-second install</div>

<div class="hl-install hl-reveal" markdown>

### Pre-built image, no clone

```bash
curl -fsSLO https://raw.githubusercontent.com/SikamikanikoBG/homelab-monitor/main/docker-compose.yml
docker compose pull
docker compose up -d
```

Then open **`http://<your-host-ip>:9800`** from any browser on your LAN or VPN.

Full install options (NVIDIA Container Toolkit, from-source, upgrade) →
[**Install**](install.md){ .md-button }

</div>
