#!/usr/bin/env bash
# Run ON the VM. Update to latest main (default) or a given ref, rebuild, restart, health-check.
# Rollback: sudo /opt/vu-handbook-mcp/deploy/update.sh <previous-sha>   (history: deploy/.deploy-history)
set -euo pipefail
cd /opt/vu-handbook-mcp
REF="${1:-origin/main}"
git rev-parse HEAD >> deploy/.deploy-history
git fetch --depth 50 origin
git checkout --detach "$REF"
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
sleep 8
docker compose -f deploy/docker-compose.yml ps
curl -fsS "https://$(grep ^MCP_PUBLIC_HOST= deploy/.env | cut -d= -f2)/healthz" && echo "  <- healthy at $(git rev-parse --short HEAD)"
