#!/usr/bin/env bash
set -euo pipefail

log() { echo "[wait-for-sonic-core] $*"; }

# Hard deps we expect to be up before we start
for svc in swss.service pmon.service; do
  if systemctl is-active --quiet "$svc"; then
    log "Service $svc is active"
  else
    log "Waiting for $svc to become active…"
    systemctl is-active -q "$svc" || true
    systemctl --no-pager --full status "$svc" || true
    exit 0  # let systemd retry; ExecStartPre must be quick
  fi
done

# Wait for CHASSIS_MODULE_TABLE to exist (best-effort, bounded time)
MAX_WAIT=${WAIT_CORE_MAX_SECONDS:-60}
INTERVAL=2
ELAPSED=0

has_chassis_table() {
  redis-cli -n 6 KEYS 'CHASSIS_MODULE_TABLE|*' | grep -q .
}

log "Waiting for CHASSIS_MODULE_TABLE keys…"
while ! has_chassis_table; do
  if (( ELAPSED >= MAX_WAIT )); then
    log "Timed out waiting for CHASSIS_MODULE_TABLE; proceeding anyway."
    exit 0
  fi
  sleep "$INTERVAL"
  ELAPSED=$((ELAPSED + INTERVAL))
done

log "CHASSIS_MODULE_TABLE present."
log "SONiC core is ready."
exit 0
