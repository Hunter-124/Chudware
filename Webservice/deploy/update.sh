#!/usr/bin/env bash
# ===========================================================================
#  update.sh — one-shot pull → rebuild → promote → redeploy for this host.
# ---------------------------------------------------------------------------
#  Keeps the desktop GUI + billet-web stack current with origin/master.
#
#  Usage:
#    bash Webservice/deploy/update.sh              # pull + host GUI + engine + webd + gateway
#    bash Webservice/deploy/update.sh --pull-only  # git pull only
#    bash Webservice/deploy/update.sh --no-pull    # rebuild/redeploy current tree
#    bash Webservice/deploy/update.sh --host-only  # desktop Billet binary only
#    bash Webservice/deploy/update.sh --web-only   # engine → install_webd + gateway
#    bash Webservice/deploy/update.sh --image      # also rebuild session container
#    bash Webservice/deploy/update.sh --wasm       # also rebuild wasm target
#
#  Env:
#    BILLET_OMP_DIR / repo root detection (this script's ../../)
#    BILLET_WEB_DATA   default ~/billet-web-data
#    BILLET_UPDATE_REMOTE default origin
#    BILLET_UPDATE_BRANCH default master
# ===========================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO="${BILLET_OMP_DIR:-$REPO}"
cd "$REPO"

REMOTE="${BILLET_UPDATE_REMOTE:-origin}"
BRANCH="${BILLET_UPDATE_BRANCH:-master}"
LOGDIR="${BILLET_WEB_DATA:-$HOME/billet-web-data}/logs"
mkdir -p "$LOGDIR"
LOGFILE="$LOGDIR/update.log"
LOCK="$LOGDIR/deploy.lock"
STATUS="$LOGDIR/deploy-status.json"

DO_PULL=1
DO_HOST=1
DO_ENGINE=1
DO_WEBD=1
DO_GATEWAY=1
DO_IMAGE=0
DO_WASM=0

for arg in "$@"; do
  case "$arg" in
    --pull-only) DO_HOST=0; DO_ENGINE=0; DO_WEBD=0; DO_GATEWAY=0; DO_IMAGE=0; DO_WASM=0 ;;
    --no-pull)   DO_PULL=0 ;;
    --host-only) DO_ENGINE=0; DO_WEBD=0; DO_GATEWAY=0; DO_IMAGE=0; DO_WASM=0 ;;
    --web-only)  DO_HOST=0; DO_IMAGE=0; DO_WASM=0 ;;
    --image)     DO_IMAGE=1 ;;
    --wasm)      DO_WASM=1 ;;
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

echo "=== billet update start $(date -Iseconds) repo=$REPO ==="
echo "    pull=$DO_PULL host=$DO_HOST engine=$DO_ENGINE webd=$DO_WEBD gateway=$DO_GATEWAY image=$DO_IMAGE wasm=$DO_WASM"

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
    git log --oneline "$BEFORE..$AFTER" | head -30
  fi
fi

if [[ "$DO_HOST" = 1 ]]; then
  echo
  echo "=== [$(date -Iseconds)] ./build.sh (host GUI) ==="
  ./build.sh
fi

if [[ "$DO_ENGINE" = 1 ]]; then
  echo
  echo "=== [$(date -Iseconds)] ./build.sh core (engine / hostd) ==="
  ./build.sh core
fi

if [[ "$DO_WASM" = 1 ]]; then
  echo
  echo "=== [$(date -Iseconds)] ./build.sh wasm ==="
  if [[ -f "$HOME/.emsdk/emsdk_env.sh" ]]; then
    set +u
    # shellcheck disable=SC1091
    source "$HOME/.emsdk/emsdk_env.sh"
    set -u
  fi
  ./build.sh wasm
fi

if [[ "$DO_IMAGE" = 1 ]]; then
  echo
  echo "=== [$(date -Iseconds)] session image ==="
  Webservice/image/stage.sh
  podman build -t localhost/billet-session:latest -f Webservice/image/Containerfile Webservice/image
fi

if [[ "$DO_WEBD" = 1 ]]; then
  echo
  echo "=== [$(date -Iseconds)] install_webd.sh --restart ==="
  bash Webservice/deploy/install_webd.sh --restart
fi

if [[ "$DO_GATEWAY" = 1 ]]; then
  echo
  echo "=== [$(date -Iseconds)] restart billet-gateway ==="
  if systemctl --user cat billet-gateway.service >/dev/null 2>&1; then
    systemctl --user restart billet-gateway.service
    systemctl --user is-active billet-gateway.service || true
    curl -fsS --max-time 5 http://127.0.0.1:8300/healthz || true
    echo
  else
    echo "    billet-gateway.service not installed — skip (run setup.sh once)"
  fi
fi

echo
echo "=== billet update finished ok $(date -Iseconds) ==="
echo "Desktop: host/build-linux/bin/Billet"
echo "Shared:  ~/billet-web/billet-shared.sh  (AICAD_INSTANCE_KEY=billetweb)"
exit 0
