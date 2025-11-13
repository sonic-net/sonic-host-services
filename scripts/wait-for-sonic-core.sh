set -euo pipefail

log() { echo "[wait-for-sonic-core] $*"; }

# Hard dep we expect to be up before we start: swss
if systemctl is-active --quiet swss.service; then
  log "Service swss.service is active"
else
  log "Waiting for swss.service to become active…"
  systemctl --no-pager --full status swss.service || true
  exit 0  # let systemd retry; ExecStartPre must be quick
fi

# Hard dep we expect to be up before we start: gnmi
if systemctl is-active --quiet gnmi.service; then
  log "Service gnmi.service is active"
else
  log "Waiting for gnmi.service to become active…"
  systemctl --no-pager --full status gnmi.service || true
  exit 0  # let systemd retry; ExecStartPre must be quick
fi

# pmon is advisory: proceed even if it's not active yet
if systemctl is-active --quiet pmon.service; then
  log "Service pmon.service is active"
else
  log "pmon.service not active yet (advisory)"
fi

# Wait for CHASSIS_MODULE_TABLE to exist (best-effort, bounded time)
DEFAULT_MAX_WAIT_SECONDS=60
MAX_WAIT=${WAIT_CORE_MAX_SECONDS:-$DEFAULT_MAX_WAIT_SECONDS}
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
