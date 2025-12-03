set -euo pipefail

log() { echo "[wait-for-sonic-core] $*"; }

# Hard dep we expect to be up before we start: swss
if ! systemctl is-active --quiet swss.service; then
  log "Waiting for swss.service to become active…"
  systemctl --no-pager --full status swss.service || true
  exit 0  # let systemd retry; ExecStartPre must be quick
fi

# Hard dep we expect to be up before we start: gnmi
if ! systemctl is-active --quiet gnmi.service; then
  log "Waiting for gnmi.service to become active…"
  systemctl --no-pager --full status gnmi.service || true
  exit 0  # let systemd retry; ExecStartPre must be quick
fi

# pmon is advisory: proceed even if it's not active yet
if ! systemctl is-active --quiet pmon.service; then
  log "pmon.service not active yet (advisory)"
fi

# Wait for CHASSIS_MODULE to exist (best-effort, bounded time)
DEFAULT_MAX_WAIT_SECONDS=60
MAX_WAIT=${WAIT_CORE_MAX_SECONDS:-$DEFAULT_MAX_WAIT_SECONDS}
INTERVAL=2
ELAPSED=0

has_chassis_table() {
  redis-cli -n 4 KEYS 'CHASSIS_MODULE|*' | grep -q .
}

log "Waiting for CHASSIS_MODULE keys…"
while ! has_chassis_table; do
  if (( ELAPSED >= MAX_WAIT )); then
    log "Timed out waiting for CHASSIS_MODULE; proceeding anyway."
    exit 0
  fi
  sleep "$INTERVAL"
  ELAPSED=$((ELAPSED + INTERVAL))
done

log "SONiC core is ready."
exit 0
