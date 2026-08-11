#!/usr/bin/env bash
# ===========================================================================
#  update.sh — one-shot pull → rebuild → promote → redeploy for this host.
# ---------------------------------------------------------------------------
#  Keeps the desktop GUI + chudware-web stack current with origin/master.
#
#  Usage:
#    bash Webservice/deploy/update.sh              # pull + desktop + joint hostd/WASM + gateway
#    bash Webservice/deploy/update.sh --pull-only  # git pull only
#    bash Webservice/deploy/update.sh --no-pull    # rebuild/redeploy current tree
#    bash Webservice/deploy/update.sh --host-only  # desktop Chudware binary only
#    bash Webservice/deploy/update.sh --web-only   # joint hostd/WASM release + gateway
#
#  Env:
#    CHUDWARE_OMP_DIR / repo root detection (this script's ../../)
#    CHUDWARE_WEB_DATA   default ~/chudware-web-data
#    CHUDWARE_UPDATE_REMOTE default origin
#    CHUDWARE_UPDATE_BRANCH default master
# ===========================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO="${CHUDWARE_OMP_DIR:-$REPO}"
cd "$REPO"

REMOTE="${CHUDWARE_UPDATE_REMOTE:-origin}"
BRANCH="${CHUDWARE_UPDATE_BRANCH:-master}"
LOGDIR="${CHUDWARE_WEB_DATA:-$HOME/chudware-web-data}/logs"
mkdir -p "$LOGDIR"
LOGFILE="$LOGDIR/update.log"
LOCK="$LOGDIR/deploy.lock"
STATUS="$LOGDIR/deploy-status.json"

DO_PULL=1
DO_HOST=1
DO_ENGINE=1
DO_WEBD=1
DO_GATEWAY=1

for arg in "$@"; do
  case "$arg" in
    --pull-only) DO_HOST=0; DO_ENGINE=0; DO_WEBD=0; DO_GATEWAY=0 ;;
    --no-pull)   DO_PULL=0 ;;
    --host-only) DO_ENGINE=0; DO_WEBD=0; DO_GATEWAY=0 ;;
    --web-only)  DO_HOST=0 ;;
    -h|--help)
      sed -n '2,25p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "busy (another deploy/update holds $LOCK)" >&2
  exit 3
fi

exec > >(tee -a "$LOGFILE") 2>&1

STARTED_AT="$(date +%s)"
printf '{"running":true,"kind":"update","started_at":%s,"last":null}\n' "$STARTED_AT" >"$STATUS"

_rc=0
_cleanup() {
  _rc=$?
  FINISHED_AT="$(date +%s)"
  printf '{"running":false,"kind":null,"started_at":null,"last":{"kind":"update","rc":%s,"finished_at":%s}}\n' \
    "$_rc" "$FINISHED_AT" >"$STATUS"
}
trap _cleanup EXIT

echo "=== chudware update start $(date -Iseconds) repo=$REPO ==="
echo "    pull=$DO_PULL host=$DO_HOST engine=$DO_ENGINE webd=$DO_WEBD gateway=$DO_GATEWAY"

if [[ "$DO_PULL" = 1 ]]; then
  echo
  echo "=== [$(date -Iseconds)] git fetch/pull $REMOTE $BRANCH ==="
  git fetch "$REMOTE" "$BRANCH"
  # Refuse to pull over dirty worktree — operator must stash/commit first.
  if [[ -n "$(git status --porcelain)" ]]; then
    echo "ERROR: working tree dirty — commit/stash before update.sh" >&2
    git status -sb
    exit 1
  fi
  BEFORE="$(git rev-parse --short HEAD)"
  git pull --ff-only "$REMOTE" "$BRANCH"
  AFTER="$(git rev-parse --short HEAD)"
  echo "    HEAD $BEFORE -> $AFTER"
  if [[ "$BEFORE" != "$AFTER" ]]; then
    # -n 30, NOT `| head -30`: under `set -euo pipefail` a truncating head closes the
    # pipe, git log dies of SIGPIPE (141), and the whole deploy aborts right here — after
    # the tree moved but before anything was rebuilt. Silent, and only ever on a big
    # catch-up pull (>30 commits), which is exactly when you need the rebuild most.
    git log --oneline -n 30 "$BEFORE..$AFTER"
  fi
fi

if [[ "$DO_HOST" = 1 ]]; then
  echo
  echo "=== [$(date -Iseconds)] ./build.sh (host GUI) ==="
  ./build.sh
fi

ENGINE_ARTIFACT_DIR="$REPO/core/build-occt-linux/bin"
if [[ "$DO_ENGINE" = 1 ]]; then
  echo
  echo "=== [$(date -Iseconds)] ./build.sh daemon (authoritative full engine / hostd) ==="
  ./build.sh daemon
  [[ -x "$ENGINE_ARTIFACT_DIR/chudware-hostd" ]] \
    || { echo "ERROR: fresh hostd artifact missing at $ENGINE_ARTIFACT_DIR/chudware-hostd" >&2; exit 1; }
  [[ -f "$ENGINE_ARTIFACT_DIR/libchudware.so" ]] \
    || { echo "ERROR: fresh engine artifact missing at $ENGINE_ARTIFACT_DIR/libchudware.so" >&2; exit 1; }
fi

if [[ "$DO_WEBD" = 1 ]]; then
  [[ "$DO_ENGINE" = 1 ]] || { echo "ERROR: webd promotion requires the engine build in this update" >&2; exit 1; }
  echo
  echo "=== [$(date -Iseconds)] validate/promote joint hostd + WASM release ==="
  CHUDWARE_WEBD_ARTIFACT_DIR="$ENGINE_ARTIFACT_DIR" \
    CHUDWARE_WEBD_REQUIRE_IDLE=1 \
    bash Webservice/deploy/install_webd.sh --restart
fi

if [[ "$DO_GATEWAY" = 1 ]]; then
  echo
  echo "=== [$(date -Iseconds)] restart chudware-gateway ==="
  systemctl --user cat chudware-gateway.service >/dev/null 2>&1 \
    || { echo "ERROR: chudware-gateway.service is not installed" >&2; exit 1; }
  systemctl --user restart chudware-gateway.service \
    || { echo "ERROR: failed to restart chudware-gateway.service" >&2; exit 1; }
  systemctl --user is-active --quiet chudware-gateway.service \
    || { echo "ERROR: chudware-gateway.service is not active after restart" >&2; exit 1; }
  gateway_ready=0
  for ((attempt = 1; attempt <= 30; attempt++)); do
    if health_body="$(curl -fsS --max-time 5 http://127.0.0.1:8300/healthz 2>/dev/null)" \
        && HEALTH_JSON="$health_body" python3 -c \
          'import json, os; body=json.loads(os.environ["HEALTH_JSON"]); raise SystemExit(0 if body == {"ok": True} else 1)'; then
      gateway_ready=1
      break
    fi
    sleep 1
  done
  [[ "$gateway_ready" = 1 ]] \
    || { echo "ERROR: chudware-gateway failed anonymous /healthz readiness" >&2; exit 1; }
  echo "    gateway ready: {\"ok\":true}"
fi

echo
echo "=== chudware update finished ok $(date -Iseconds) ==="
echo "Desktop: host/build-linux/bin/chudware"
echo "Shared:  ~/chudware-web/chudware-shared.sh  (CHUDWARE_INSTANCE_KEY=chudwareweb)"
exit 0
