FROM python:3.12-slim
WORKDIR /app

# flask = web layer; jeepney = pure-Python D-Bus client used to read systemd
# (no native libs, keeps the image slim). curl only needed to vendor Chart.js below.
# openssh-client provides ssh + ssh-keygen for the multi-host registry probes.
RUN pip install --no-cache-dir flask==3.0.3 jeepney==0.8.0 prometheus_client==0.20.0 \
 && apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates openssh-client \
 && rm -rf /var/lib/apt/lists/*

# Vendor Chart.js so the dashboard works fully offline / on a LAN with no internet.
RUN mkdir -p /app/static \
 && curl -fsSL https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js \
      -o /app/static/chart.min.js

COPY app.py /app/app.py
COPY probe.py /app/probe.py
COPY static/dashboard.html /app/static/dashboard.html
COPY static/favicon.svg    /app/static/favicon.svg

ENV PORT=8099
EXPOSE 8099

# Self-healthcheck so the container reports its own status to Docker (and to
# our own Containers tab, which reads the same Docker API). /healthz is a
# locks-free 200 that returns the running version — never blocks on the
# collector. start-period covers the initial Flask boot.
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-9800}/healthz" || exit 1

CMD ["python", "app.py"]
