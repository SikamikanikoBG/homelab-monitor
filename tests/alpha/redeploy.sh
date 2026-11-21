#!/usr/bin/env bash
# Rebuild the alpha hub from the checked-out branch and recreate only the hub
# container; data, targets and the stub stay as they are.
#   cd ~/hlm-agents-alpha && git pull && bash tests/alpha/redeploy.sh
set -euo pipefail
DATA="${ALPHA_DATA:-$HOME/hlm-alpha}"
docker build -q -t hlm-agents:alpha . >/dev/null
docker rm -f hlm-agents-alpha >/dev/null 2>&1 || true
docker run -d --name hlm-agents-alpha --network hlm-alpha -p 9831:9800 -p 9841:9810 \
  -e PORT=9800 -e AGENT_ENABLED=1 -e ENABLE_CONTROLS=1 -e ALLOW_SELF_UPDATE=0 -e CHECK_UPDATES=false \
  -e DB_PATH=/data/gpu.db -e SSH_DIR=/data/.ssh -e TZ=Europe/Sofia -e AGENT_MAX_RUNS=1 \
  -e DOCKER_SOCK=/var/run/sandbox/docker.sock -v hlm-alpha-dindsock:/var/run/sandbox \
  -v "$DATA/data":/data hlm-agents:alpha >/dev/null
until curl -fsS localhost:9831/healthz >/dev/null 2>&1; do sleep 1; done
echo "alpha hub redeployed: $(curl -fsS localhost:9831/healthz)"
