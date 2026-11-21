#!/usr/bin/env bash
# Remove everything up.sh created (and only that).
DATA="${ALPHA_DATA:-$HOME/hlm-alpha}"
docker rm -f hlm-agents-alpha hlm-sandbox-local hlm-target1 hlm-target2 hlm-alpha-stub >/dev/null 2>&1 || true
docker volume rm hlm-alpha-dindsock >/dev/null 2>&1 || true
docker network rm hlm-alpha >/dev/null 2>&1 || true
# the hub wrote its data as root; remove it the same way
[ -d "$DATA" ] && docker run --rm -v "$DATA":/d alpine sh -c 'rm -rf /d/data' && rmdir "$DATA" 2>/dev/null
echo "alpha removed"
