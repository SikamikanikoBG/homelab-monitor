FROM python:3.12-slim
WORKDIR /app

# flask = web layer; jeepney = pure-Python D-Bus client used to read systemd
# (no native libs, keeps the image slim). curl only needed to vendor Chart.js below.
RUN pip install --no-cache-dir flask==3.0.3 jeepney==0.8.0 prometheus_client==0.20.0 \
 && apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Vendor Chart.js so the dashboard works fully offline / on a LAN with no internet.
RUN mkdir -p /app/static \
 && curl -fsSL https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js \
      -o /app/static/chart.min.js

COPY app.py /app/app.py
COPY static/dashboard.html /app/static/dashboard.html

ENV PORT=8099
EXPOSE 8099
CMD ["python", "app.py"]
