#!/usr/bin/env bash
# Admin redeploy — restart chudware-gateway (detached; API already delayed launch).
set -euo pipefail

LOGDIR="${CHUDWARE_WEB_DATA:-$HOME/chudware-web-data}/logs"
mkdir -p "$LOGDIR"

REPO="${CHUDWARE_OMP_DIR:-$HOME/Repositories/CAD}"
cd "$REPO"

STATUS="$LOGDIR/deploy-status.json"
LOCK="$LOGDIR/deploy.lock"
LOGFILE="$LOGDIR/admin-redeploy.log"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "busy"
  exit 3
fi

exec > >(tee "$LOGFILE") 2>&1

STARTED_AT="$(date +%s)"
printf '{"running":true,"kind":"redeploy","started_at":%s,"last":null}\n' "$STARTED_AT" >"$STATUS"

_rc=0
_cleanup() {
  _rc=$?
  FINISHED_AT="$(date +%s)"
  printf '{"running":false,"kind":null,"started_at":null,"last":{"kind":"redeploy","rc":%s,"finished_at":%s}}\n' \
    "$_rc" "$FINISHED_AT" >"$STATUS"
}
trap _cleanup EXIT

echo "=== admin-redeploy start $(date -Iseconds) ==="

# Extra beat so any in-flight 202 has fully flushed even if delay=2 was skipped.
sleep 1

echo "[$(date -Iseconds)] systemctl --user restart chudware-gateway"
systemctl --user restart chudware-gateway

echo "[$(date -Iseconds)] systemctl --user is-active chudware-gateway"
systemctl --user is-active chudware-gateway || true

echo "[$(date -Iseconds)] curl healthz"
curl -fsS http://127.0.0.1:8300/healthz || true
echo

echo "=== admin-redeploy finished $(date -Iseconds) ==="
exit 0
