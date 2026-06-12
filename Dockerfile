FROM python:3.12-slim
WORKDIR /app

# flask = web layer; jeepney = pure-Python D-Bus client used to read systemd
# (no native libs, keeps the image slim). curl only needed to vendor Chart.js below.
# openssh-client provides ssh + ssh-keygen for the multi-host registry probes.
# mcp = the Model Context Protocol SDK powering the built-in MCP server (served on
# $MCP_PORT alongside the dashboard); it pulls in starlette/uvicorn for HTTP.
RUN pip install --no-cache-dir flask==3.0.3 jeepney==0.8.0 prometheus_client==0.20.0 "mcp>=1.9.0" \
 && apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates openssh-client \
 && rm -rf /var/lib/apt/lists/*

# Vendor Chart.js + html2canvas so the dashboard works fully offline / on a LAN
# with no internet. html2canvas powers the Snapshot/Share PNG capture (#31/#87).
RUN mkdir -p /app/static \
 && curl -fsSL https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js \
      -o /app/static/chart.min.js \
 && curl -fsSL https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js \
      -o /app/static/html2canvas.min.js

COPY app.py /app/app.py
COPY db_backup.py /app/db_backup.py
COPY mcp_status.py /app/mcp_status.py
COPY probe.py /app/probe.py
# probe.ps1 is the Windows-host probe: the hub pipes it over SSH to Windows
# remotes (PowerShell, no install) and gets back the same JSON probe.py emits.
COPY probe.ps1 /app/probe.ps1
COPY static/dashboard.html /app/static/dashboard.html
COPY static/favicon.svg    /app/static/favicon.svg

# Built-in MCP server (read-only): the FastMCP wrapper + its pure-stdlib client,
# plus the CHANGELOG it serves as a resource, and the process launcher that runs
# the dashboard and the MCP server side-by-side in this one container.
COPY mcp/server.py         /app/mcp_server.py
COPY mcp/homelab_client.py /app/homelab_client.py
COPY CHANGELOG.md          /app/CHANGELOG.md
COPY launch.py             /app/launch.py

# Identifies this image to the official MCP Registry (registry.modelcontextprotocol.io),
# which validates OCI-packaged servers by matching this label to the published name.
LABEL io.modelcontextprotocol.server.name="io.github.SikamikanikoBG/homelab-monitor"

ENV PORT=8099 \
    MCP_PORT=9810
EXPOSE 8099 9810

# Self-healthcheck so the container reports its own status to Docker (and to
# our own Containers tab, which reads the same Docker API). /healthz is a
# locks-free 200 that returns the running version — never blocks on the
# collector. start-period covers the initial Flask boot.
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-9800}/healthz" || exit 1

# Runs the dashboard (Flask) and the MCP server together. ENABLE_MCP=0 to opt out.
CMD ["python", "/app/launch.py"]
