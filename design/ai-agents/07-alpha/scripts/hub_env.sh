#!/usr/bin/env bash
# Recreate only the alpha hub with extra env overrides (same image, same data).
#   bash hub_env.sh                      -> as redeploy.sh
#   bash hub_env.sh ENABLE_CONTROLS=0    -> kill switch
#   bash hub_env.sh AGENT_ENABLED=0      -> agent disabled
set -euo pipefail
DATA="${ALPHA_DATA:-$HOME/hlm-alpha}"
declare -A E=([PORT]=9800 [AGENT_ENABLED]=1 [ENABLE_CONTROLS]=1 [ALLOW_SELF_UPDATE]=0 [CHECK_UPDATES]=false
  [DB_PATH]=/data/gpu.db [SSH_DIR]=/data/.ssh [TZ]=Europe/Sofia [AGENT_MAX_RUNS]=1 [DOCKER_SOCK]=/var/run/sandbox/docker.sock)
for kv in "$@"; do E[${kv%%=*}]="${kv#*=}"; done
args=(); for k in "${!E[@]}"; do [ "${E[$k]}" = "UNSET" ] || args+=(-e "$k=${E[$k]}"); done
docker rm -f hlm-agents-alpha >/dev/null 2>&1 || true
docker run -d --name hlm-agents-alpha --network hlm-alpha -p 9831:9800 -p 9841:9810 "${args[@]}" \
  -v hlm-alpha-dindsock:/var/run/sandbox -v "$DATA/data":/data hlm-agents:alpha >/dev/null
until curl -fsS localhost:9831/healthz >/dev/null 2>&1; do sleep 1; done
echo "hub up with: $*"
