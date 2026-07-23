#!/usr/bin/env bash
# Admin rebuild — wasm / image / engine targets. Serialized by deploy.lock.
set -euo pipefail

LOGDIR="${CHUDWARE_WEB_DATA:-$HOME/chudware-web-data}/logs"
mkdir -p "$LOGDIR"

REPO="${CHUDWARE_OMP_DIR:-$HOME/Repositories/CAD}"
cd "$REPO"

STATUS="$LOGDIR/deploy-status.json"
LOCK="$LOGDIR/deploy.lock"
LOGFILE="$LOGDIR/admin-rebuild.log"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "busy"
  exit 3
fi

# Truncate + tee all subsequent output into the admin rebuild log.
exec > >(tee "$LOGFILE") 2>&1

STARTED_AT="$(date +%s)"
printf '{"running":true,"kind":"rebuild","started_at":%s,"last":null}\n' "$STARTED_AT" >"$STATUS"

_rc=0
_cleanup() {
  _rc=$?
  FINISHED_AT="$(date +%s)"
  printf '{"running":false,"kind":null,"started_at":null,"last":{"kind":"rebuild","rc":%s,"finished_at":%s}}\n' \
    "$_rc" "$FINISHED_AT" >"$STATUS"
}
trap _cleanup EXIT

if [[ $# -eq 0 ]]; then
  set -- wasm
fi

echo "=== admin-rebuild start $(date -Iseconds) repo=$REPO targets=$* ==="

for target in "$@"; do
  echo
  echo "=== [$(date -Iseconds)] target: $target ==="
  case "$target" in
    wasm)
      if [[ -f "$HOME/.emsdk/emsdk_env.sh" ]]; then
        set +u
        # shellcheck disable=SC1091
        source "$HOME/.emsdk/emsdk_env.sh"
        set -u
      fi
      ./build.sh wasm
      ;;
    image)
      Webservice/image/stage.sh
      podman build -t localhost/chudware-session:latest -f Webservice/image/Containerfile Webservice/image
      ;;
    engine)
      ./build.sh core
      ;;
    *)
      echo "ERROR: unknown target '$target' (expected wasm|image|engine)" >&2
      exit 2
      ;;
  esac
  echo "=== [$(date -Iseconds)] target $target done ==="
done

echo
echo "=== admin-rebuild finished ok $(date -Iseconds) ==="
exit 0
