#!/usr/bin/env bash
# ===========================================================================
# update.sh — environment-aware gateway/native deployment for livingroom-pc.
#
# Usage:
#   bash Webservice/deploy/update.sh staging
#   bash Webservice/deploy/update.sh prod --tag v1.2.0
#
# Staging follows origin/master and deploys only the gateway by default.
# Production requires an explicit v* tag and keeps the native release steps.
# Both environments share this implementation so backup, migration, restart,
# and authenticated health behavior cannot drift.
# ===========================================================================
set -euo pipefail

usage() {
  cat <<'EOF'
usage: update.sh <prod|staging> [options]

Options:
  --tag vX.Y.Z     Required for a production pull; forbidden for staging
  --pull-only      Fetch/pull only (no build, migration, or restart)
  --no-pull        Deploy the current checkout (prod HEAD must be a v* tag)
  --gateway-only   Skip desktop, engine, and webd promotion
  --host-only      Build the desktop binary only
  --web-only       Skip the desktop build; deploy engine/webd and gateway
  -h, --help       Show this help

Health-test overrides:
  CHUDWARE_DEPLOY_HEALTH_BASE_URL  defaults to the selected loopback gateway
  CHUDWARE_DEPLOY_HEALTHZ_PATH     defaults to /healthz
  CHUDWARE_DEPLOY_ME_PATH          defaults to /api/me
  CHUDWARE_DEPLOY_HEALTH_ATTEMPTS  defaults to 30
EOF
}

[[ $# -ge 1 ]] || { usage >&2; exit 2; }
ENVIRONMENT="$1"
shift
case "$ENVIRONMENT" in
  prod)
    DATA_ROOT="${CHUDWARE_WEB_DATA:-$HOME/chudware-web-data}"
    GATEWAY_ENV="${CHUDWARE_WEB_GATEWAY_ENV:-$DATA_ROOT/gateway.env}"
    GATEWAY_UNIT="chudware-gateway.service"
    GATEWAY_PORT=8300
    EXPECTED_DB="chudware_prod"
    DO_HOST=1
    DO_ENGINE=1
    DO_WEBD=1
    ;;
  staging)
    DATA_ROOT="${CHUDWARE_WEB_DATA:-$HOME/chudware-web-staging}"
    GATEWAY_ENV="${CHUDWARE_WEB_GATEWAY_ENV:-$DATA_ROOT/gateway.env}"
    GATEWAY_UNIT="chudware-gateway-staging.service"
    GATEWAY_PORT=8310
    EXPECTED_DB="chudware_staging"
    DO_HOST=0
    DO_ENGINE=0
    DO_WEBD=0
    ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    echo "ERROR: environment must be prod or staging" >&2
    usage >&2
    exit 2
    ;;
esac

DO_PULL=1
DO_GATEWAY=1
RELEASE_TAG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)
      [[ $# -ge 2 ]] || { echo "ERROR: --tag requires a value" >&2; exit 2; }
      RELEASE_TAG="$2"
      shift 2
      ;;
    --pull-only)
      DO_HOST=0; DO_ENGINE=0; DO_WEBD=0; DO_GATEWAY=0
      shift
      ;;
    --no-pull)
      DO_PULL=0
      shift
      ;;
    --gateway-only)
      DO_HOST=0; DO_ENGINE=0; DO_WEBD=0
      shift
      ;;
    --host-only)
      DO_ENGINE=0; DO_WEBD=0; DO_GATEWAY=0
      shift
      ;;
    --web-only)
      DO_HOST=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$ENVIRONMENT" = staging && -n "$RELEASE_TAG" ]]; then
  echo "ERROR: staging follows master; --tag is forbidden" >&2
  exit 2
fi
if [[ "$ENVIRONMENT" = prod && "$DO_PULL" = 1 && ! "$RELEASE_TAG" =~ ^v[0-9] ]]; then
  echo "ERROR: production deploy requires an explicit v* tag (use --tag vX.Y.Z)" >&2
  exit 2
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO="${CHUDWARE_OMP_DIR:-$REPO}"
cd "$REPO"
REMOTE="${CHUDWARE_UPDATE_REMOTE:-origin}"
REF="${CHUDWARE_UPDATE_BRANCH:-master}"
[[ "$ENVIRONMENT" = staging ]] || REF="$RELEASE_TAG"

mkdir -p "$DATA_ROOT/logs"
LOGFILE="$DATA_ROOT/logs/update.log"
STATUS="$DATA_ROOT/logs/deploy-status.json"
# One host and one checkout: prod and staging must not pull or migrate in
# parallel even though their logs and databases are separate.
LOCK="${CHUDWARE_DEPLOY_LOCK:-$HOME/chudware-web-data/logs/deploy.lock}"
mkdir -p "$(dirname "$LOCK")"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "busy (another prod/staging deployment holds $LOCK)" >&2
  exit 3
fi

exec > >(tee -a "$LOGFILE") 2>&1
STARTED_AT="$(date +%s)"
printf '{"running":true,"kind":"deploy-%s","started_at":%s,"last":null}\n' \
  "$ENVIRONMENT" "$STARTED_AT" >"$STATUS"
_cleanup() {
  local rc=$?
  local finished_at
  finished_at="$(date +%s)"
  printf '{"running":false,"kind":null,"started_at":null,"last":{"kind":"deploy-%s","rc":%s,"finished_at":%s}}\n' \
    "$ENVIRONMENT" "$rc" "$finished_at" >"$STATUS"
}
trap _cleanup EXIT

env_value() {
  local key="$1"
  python3 - "$GATEWAY_ENV" "$key" <<'PY'
from pathlib import Path
import sys

path, key = Path(sys.argv[1]), sys.argv[2]
for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, value = line.split("=", 1)
    if name.strip() == key:
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        print(value)
        break
PY
}
with_pgpass() {
  local dsn="$1"; shift
  local pgpass rc
  pgpass="$(mktemp)"
  chmod 600 "$pgpass"
  if ! DB_DSN="$dsn" python3 - "$pgpass" <<'PY'
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

dsn = urlparse(os.environ["DB_DSN"])
if dsn.scheme not in {"postgres", "postgresql"}:
    raise SystemExit("CHUDWARE_DB_URL must be a PostgreSQL URL")
host = dsn.hostname or "localhost"
port = dsn.port or 5432
database = unquote(dsn.path.lstrip("/"))
user = unquote(dsn.username or "")
password = unquote(dsn.password or "")
def field(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace(":", "\\:")
Path(sys.argv[1]).write_text(":".join(map(field, (host, port, database, user, password))) + "\n")
PY
  then
    rm -f "$pgpass"
    return 1
  fi
  if PGPASSFILE="$pgpass" "$@" "postgresql://$(DB_DSN="$dsn" python3 - <<'PY'
import os
from urllib.parse import quote, unquote, urlparse
d = urlparse(os.environ["DB_DSN"])
user = quote(unquote(d.username or ""), safe="")
host = d.hostname or "localhost"
port = d.port or 5432
database = quote(unquote(d.path.lstrip("/")), safe="")
print(f"{user + '@' if user else ''}{host}:{port}/{database}")
PY
)"; then rc=0; else rc=$?; fi
  rm -f "$pgpass"
  return "$rc"
}

assert_database_target() {
  local actual
  actual="$(DB_DSN="$DB_URL" python3 - <<'PY'
import os
from urllib.parse import unquote, urlparse

dsn = urlparse(os.environ["DB_DSN"])
print(unquote(dsn.path.lstrip("/")))
PY
)"
  if [[ "$actual" != "$EXPECTED_DB" ]]; then
    echo "ERROR: $ENVIRONMENT gateway targets database '$actual', expected '$EXPECTED_DB'; refusing deployment" >&2
    exit 1
  fi
  echo "    database isolation: $actual"
}

backup_database() {
  command -v pg_dump >/dev/null 2>&1 \
    || { echo "ERROR: pg_dump is required for the pre-deploy rollback point" >&2; exit 1; }
  local backup_dir stamp final tmp
  backup_dir="$DATA_ROOT/backups"
  mkdir -p "$backup_dir"
  chmod 700 "$backup_dir"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  final="$backup_dir/pre-deploy-${ENVIRONMENT}-${stamp}.dump"
  tmp="$final.tmp.$$"
  umask 077
  echo "=== [$(date -Iseconds)] pg_dump rollback point ($EXPECTED_DB) ==="
  if ! with_pgpass "$DB_URL" pg_dump --no-password --format=custom --file="$tmp"; then
    rm -f "$tmp"
    echo "ERROR: pg_dump failed; deployment has not changed the database or service" >&2
    exit 1
  fi
  mv "$tmp" "$final"
  echo "    rollback dump: $final"
}

install_gateway_requirements() {
  local gateway_python="$REPO/Webservice/gateway/.venv/bin/python"
  [[ -x "$gateway_python" ]] \
    || { echo "ERROR: gateway venv missing at $gateway_python" >&2; exit 1; }
  echo "=== [$(date -Iseconds)] install pinned gateway requirements ==="
  "$gateway_python" -m pip install --disable-pip-version-check -q \
    -r "$REPO/Webservice/gateway/requirements.txt"
}

run_migrations() {
  local gateway_python="$REPO/Webservice/gateway/.venv/bin/python"
  [[ -x "$gateway_python" ]] \
    || { echo "ERROR: gateway venv missing at $gateway_python" >&2; exit 1; }
  echo "=== [$(date -Iseconds)] migrate $EXPECTED_DB ==="
  (
    cd "$REPO/Webservice/gateway"
    CHUDWARE_DB_URL="$DB_URL" "$gateway_python" - <<'PY'
import db

try:
    applied = db.migrate()
    print("    applied: " + (", ".join(applied) if applied else "none (schema current)"))
finally:
    db.close_pool()
PY
  )
}

post_deploy_health_assert() {
  local base health_path me_path attempts health_email health_sid
  local health_body health_code me_body me_code attempt
  base="${CHUDWARE_DEPLOY_HEALTH_BASE_URL:-http://127.0.0.1:$GATEWAY_PORT}"
  health_path="${CHUDWARE_DEPLOY_HEALTHZ_PATH:-/healthz}"
  me_path="${CHUDWARE_DEPLOY_ME_PATH:-/api/me}"
  attempts="${CHUDWARE_DEPLOY_HEALTH_ATTEMPTS:-30}"
  [[ "$health_path" = /* && "$me_path" = /* ]] \
    || { echo "ERROR: health paths must begin with /" >&2; return 1; }
  [[ "$attempts" =~ ^[1-9][0-9]*$ ]] \
    || { echo "ERROR: CHUDWARE_DEPLOY_HEALTH_ATTEMPTS must be a positive integer" >&2; return 1; }

  health_email="$(env_value CHUDWARE_DEPLOY_HEALTH_EMAIL)"
  if [[ -z "$health_email" ]]; then
    health_email="$(with_pgpass "$DB_URL" psql --no-password -tAXq -v ON_ERROR_STOP=1 -c \
      "SELECT email FROM accounts WHERE status='active' ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, email LIMIT 1")"
  fi
  [[ -n "$health_email" ]] \
    || { echo "ERROR: no active $ENVIRONMENT account exists for authenticated /api/me health" >&2; return 1; }

  health_sid="$(
    cd "$REPO/Webservice/gateway"
    CHUDWARE_DB_URL="$DB_URL" "$REPO/Webservice/gateway/.venv/bin/python" \
      -c 'import authn, db, sys; rc = authn._cli(sys.argv[1:]); db.close_pool(); raise SystemExit(rc)' \
      ops-session --email "$health_email" --ttl-seconds 300
  )" || {
    echo "ERROR: could not mint $ENVIRONMENT health session for $health_email" >&2
    return 1
  }
  [[ -n "$health_sid" ]] \
    || { echo "ERROR: health session was empty" >&2; return 1; }

  health_body="$(mktemp)"
  me_body="$(mktemp)"
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    health_code="$(curl -sS --max-time 5 -o "$health_body" -w '%{http_code}' "$base$health_path" 2>/dev/null || true)"
    me_code="$(curl -sS --max-time 5 -o "$me_body" -w '%{http_code}' \
      --cookie "chudware_sid=$health_sid" "$base$me_path" 2>/dev/null || true)"
    if [[ "$health_code" = 200 && "$me_code" = 200 ]] \
        && HEALTH_FILE="$health_body" ME_FILE="$me_body" HEALTH_EMAIL="$health_email" python3 - <<'PY'
import json
import os
from pathlib import Path

health = json.loads(Path(os.environ["HEALTH_FILE"]).read_text())
me = json.loads(Path(os.environ["ME_FILE"]).read_text())
if health != {"ok": True} or me.get("email") != os.environ["HEALTH_EMAIL"]:
    raise SystemExit(1)
PY
    then
      rm -f "$health_body" "$me_body"
      echo "    health asserted: $base$health_path=200, authenticated $base$me_path=200"
      return 0
    fi
    sleep 1
  done
  echo "ERROR: post-deploy health assertion failed after $attempts attempts:" >&2
  echo "       $base$health_path HTTP ${health_code:-000}" >&2
  echo "       authenticated $base$me_path HTTP ${me_code:-000}" >&2
  rm -f "$health_body" "$me_body"
  return 1
}

echo "=== chudware $ENVIRONMENT deploy start $(date -Iseconds) repo=$REPO ==="
echo "    pull=$DO_PULL ref=${REF:-current} host=$DO_HOST engine=$DO_ENGINE webd=$DO_WEBD gateway=$DO_GATEWAY"

if [[ "$DO_GATEWAY" = 1 ]]; then
  [[ -r "$GATEWAY_ENV" ]] \
    || { echo "ERROR: gateway environment is unreadable: $GATEWAY_ENV" >&2; exit 1; }
  DB_URL="$(env_value CHUDWARE_DB_URL)"
  [[ -n "$DB_URL" ]] \
    || { echo "ERROR: CHUDWARE_DB_URL is missing from $GATEWAY_ENV" >&2; exit 1; }
  DEV_MODE="$(env_value CHUDWARE_WEB_DEV)"
  case "$DEV_MODE" in
    ""|0|false|False|FALSE|no|No|NO|off|Off|OFF) ;;
    *) echo "ERROR: CHUDWARE_WEB_DEV must be disabled for $ENVIRONMENT deployments" >&2; exit 1 ;;
  esac
  assert_database_target
  backup_database
fi

if [[ "$DO_PULL" = 1 ]]; then
  echo "=== [$(date -Iseconds)] git fetch/pull $REMOTE $REF ==="
  git fetch "$REMOTE" "$REF"
  if [[ -n "$(git status --porcelain)" ]]; then
    echo "ERROR: working tree dirty — commit/stash before update.sh" >&2
    git status -sb
    exit 1
  fi
  BEFORE="$(git rev-parse --short HEAD)"
  git pull --ff-only "$REMOTE" "$REF"
  AFTER="$(git rev-parse --short HEAD)"
  echo "    HEAD $BEFORE -> $AFTER"
  if [[ "$BEFORE" != "$AFTER" ]]; then
    git log --oneline -n 30 "$BEFORE..$AFTER"
  fi
fi

if [[ "$ENVIRONMENT" = prod ]]; then
  HEAD_TAG="$(git describe --tags --exact-match HEAD 2>/dev/null || true)"
  [[ "$HEAD_TAG" =~ ^v[0-9] ]] \
    || { echo "ERROR: production HEAD is not an exact v* release tag" >&2; exit 1; }
  if [[ -n "$RELEASE_TAG" && "$HEAD_TAG" != "$RELEASE_TAG" ]]; then
    echo "ERROR: production HEAD tag $HEAD_TAG does not match requested $RELEASE_TAG" >&2
    exit 1
  fi
  echo "    production release tag: $HEAD_TAG"
fi

if [[ "$DO_HOST" = 1 ]]; then
  echo "=== [$(date -Iseconds)] ./build.sh (host GUI) ==="
  ./build.sh
fi

ENGINE_ARTIFACT_DIR="$REPO/core/build-occt-linux/bin"
if [[ "$DO_ENGINE" = 1 ]]; then
  echo "=== [$(date -Iseconds)] ./build.sh daemon ==="
  ./build.sh daemon
  [[ -x "$ENGINE_ARTIFACT_DIR/chudware-hostd" && -f "$ENGINE_ARTIFACT_DIR/libchudware.so" ]] \
    || { echo "ERROR: fresh hostd/libchudware artifacts are missing" >&2; exit 1; }
fi

if [[ "$DO_WEBD" = 1 ]]; then
  [[ "$DO_ENGINE" = 1 ]] \
    || { echo "ERROR: webd promotion requires the engine build in this update" >&2; exit 1; }
  echo "=== [$(date -Iseconds)] validate/promote joint hostd + WASM release ==="
  CHUDWARE_WEBD_ARTIFACT_DIR="$ENGINE_ARTIFACT_DIR" \
    CHUDWARE_WEBD_REQUIRE_IDLE=1 \
    bash Webservice/deploy/install_webd.sh --restart
fi

if [[ "$DO_GATEWAY" = 1 ]]; then
  install_gateway_requirements
  run_migrations
  echo "=== [$(date -Iseconds)] restart $GATEWAY_UNIT ==="
  systemctl --user cat "$GATEWAY_UNIT" >/dev/null 2>&1 \
    || { echo "ERROR: $GATEWAY_UNIT is not installed" >&2; exit 1; }
  systemctl --user restart "$GATEWAY_UNIT" \
    || { echo "ERROR: failed to restart $GATEWAY_UNIT" >&2; exit 1; }
  systemctl --user is-active --quiet "$GATEWAY_UNIT" \
    || { echo "ERROR: $GATEWAY_UNIT is not active after restart" >&2; exit 1; }
  post_deploy_health_assert
fi

echo "=== chudware $ENVIRONMENT deploy finished ok $(date -Iseconds) ==="
