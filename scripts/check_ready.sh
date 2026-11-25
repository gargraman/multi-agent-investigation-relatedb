#!/usr/bin/env bash
# AC1 check (spec §17.3): every core container is healthy (its healthcheck hits /readyz) and the
# orchestrator's /api/v1/system/health, reached through the frontend's same-origin proxy, is OK.
#
# Usage: scripts/check_ready.sh   (after `make up`; NEXUS_UI_PORT defaults to 3000)
set -euo pipefail
cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f infrastructure/docker/compose.yaml --env-file .env)
fail=0
for svc in context-service sigint-agent orchestrator frontend; do
  cid=$("${COMPOSE[@]}" ps -q "$svc")
  state=$( [ -n "$cid" ] && docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" || echo missing)
  printf '  %-16s %s\n' "$svc" "$state"
  case "$state" in healthy|running) ;; *) fail=1 ;; esac
done
port="${NEXUS_UI_PORT:-3000}"
body=$(curl -fsS "http://127.0.0.1:${port}/api/v1/system/health" || true)
overall=$(printf '%s' "$body" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("status") or d.get("overall") or "?")' 2>/dev/null || echo unreachable)
printf '  %-16s %s\n' "system/health" "$overall"
[ "$overall" = "OK" ] || fail=1
[ "$fail" = 0 ] && echo "AC1: PASS" || { echo "AC1: FAIL" >&2; exit 1; }
