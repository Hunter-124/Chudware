#!/usr/bin/env bash
# ===========================================================================
# update.sh — environment-aware gateway/native deployment for livingroom-pc.
#
# Usage:
#   bash Webservice/deploy/update.sh staging
#   bash Webservice/deploy/update.sh prod --tag v1.2.0
#   bash Webservice/deploy/update.sh prod --continuous
#
# Staging follows origin/master and deploys only the gateway by default.
# Production accepts exactly one of two release policies:
#   --tag vX.Y.Z  pinned release; HEAD must be that exact v* tag.
#   --continuous  untagged continuous deployment of origin/master HEAD, used by
#                 .github/workflows/deploy-prod.yml so production tracks master
#                 instead of silently serving a weeks-old bundle. The tradeoff
#                 is deliberate and it is a policy change: a v* tag no longer
#                 gates production. What gates it instead is the green master
#                 CI gate upstream, plus every check below (clean tree,
#                 ff-only pull, pg_dump rollback point, database-name and
#                 entitlement asserts, joint-release parity validation and the
#                 two post-deploy health assertions), with rollback.sh
#                 restoring the previous retained joint release on failure.
# The two policies are mutually exclusive, and a bare `prod` with neither flag
# still refuses to deploy an untagged HEAD.
# Both environments share this implementation so backup, migration, restart,
# and authenticated health behavior cannot drift.
# ===========================================================================
set -euo pipefail

usage() {
  cat <<'EOF'
usage: update.sh <prod|staging> [options]

Options:
  --tag vX.Y.Z     Pinned production release; forbidden for staging
  --continuous     Production only: deploy the untagged current master HEAD;
                   rejected for staging and rejected together with --tag
  --pull-only      Fetch/pull only (no build, migration, or restart)
  --no-pull        Deploy the current checkout (prod HEAD must be a v* tag
                   unless --continuous is given)
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

# Exit codes are a contract with .github/workflows/deploy-prod.yml:
#   0  deployed
#   1  FAILED PART-WAY: production may have been mutated -> the caller must roll back
#   2  usage error
#   3  another deploy holds the host lock
#   4  REFUSED BEFORE ANY MUTATION: production is exactly as it was -> do NOT roll back
# Code 4 exists because a precondition refusal used to exit 1, and the workflow
# then ran rollback.sh against a production it had never touched. The rollback
# failed on its own unrelated grounds ("release ... is not restorable") and every
# push for four days reported "ROLLBACK ITSELF FAILED ... Production needs
# hands-on recovery now" when the only real problem was a dirty checkout.
refuse() {
  echo "REFUSED: $1" >&2
  echo "         production was not touched by this run." >&2
  exit 4
}

[[ $# -ge 1 ]] || { usage >&2; exit 2; }
# Snapshot argv and our own bytes before anything consumes or replaces them.
# `git pull` below can rewrite this very file while bash is still reading it;
# see the re-exec guard after the pull for why that matters.
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
SELF_SHA_BEFORE="$(sha256sum "$SELF" | cut -d' ' -f1)"
ORIGINAL_ARGV=("$@")
ENVIRONMENT="$1"
shift
case "$ENVIRONMENT" in
  prod)
    DATA_ROOT="${CHUDWARE_WEB_DATA:-$HOME/chudware-web-data}"
    GATEWAY_ENV="${CHUDWARE_WEB_GATEWAY_ENV:-$DATA_ROOT/gateway.env}"
    GATEWAY_UNIT="chudware-gateway.service"
    # post_deploy_health_assert defaults its base URL to
    # http://127.0.0.1:$GATEWAY_PORT. Under `set -u` an unset GATEWAY_PORT
    # aborts the run at the health assert — i.e. after the pg_dump, both
    # builds, the joint-release promotion and the hostd restart have already
    # landed — leaving a half-reported deploy that looks like a shell bug.
    # The port belongs here with the other per-environment settings.
    GATEWAY_PORT=8300
    RETENTION_UNIT="chudware-security-retention.service"
    RETENTION_TIMER="chudware-security-retention.timer"
    RELEASE_RETENTION_UNIT="chudware-release-retention.service"
    RELEASE_RETENTION_TIMER="chudware-release-retention.timer"
    # Host-side, not gateway-side, but it belongs to this host's disk safety:
    # the CEF metrics spool leaks ~650 MB/day and the disk it fills is the one
    # carrying PostgreSQL, the release store and the tunnel.
    CEF_SPOOL_UNIT="chudware-cef-spool.service"
    CEF_SPOOL_TIMER="chudware-cef-spool.timer"
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
    RETENTION_UNIT="chudware-security-retention-staging.service"
    RETENTION_TIMER="chudware-security-retention-staging.timer"
    RELEASE_RETENTION_UNIT="chudware-release-retention-staging.service"
    RELEASE_RETENTION_TIMER="chudware-release-retention-staging.timer"
    # Same unit both environments: it bounds a per-user cache path, not a
    # per-environment one, so a staging deploy must not install a second copy
    # under a different name that would reap the same directories twice.
    CEF_SPOOL_UNIT="chudware-cef-spool.service"
    CEF_SPOOL_TIMER="chudware-cef-spool.timer"
    # assert_database_target, backup_database and run_migrations all reference
    # EXPECTED_DB unconditionally whenever the gateway is deployed, so leaving
    # it out of this arm would abort every staging run on an unbound variable.
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
CONTINUOUS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)
      [[ $# -ge 2 ]] || { echo "ERROR: --tag requires a value" >&2; exit 2; }
      RELEASE_TAG="$2"
      shift 2
      ;;
    --continuous)
      CONTINUOUS=1
      shift
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
if [[ "$ENVIRONMENT" = staging && "$CONTINUOUS" = 1 ]]; then
  echo "ERROR: staging already follows master; --continuous is meaningless there" >&2
  exit 2
fi
if [[ "$CONTINUOUS" = 1 && -n "$RELEASE_TAG" ]]; then
  echo "ERROR: --continuous and --tag are contradictory release policies; pass exactly one" >&2
  exit 2
fi
if [[ "$ENVIRONMENT" = prod && "$CONTINUOUS" = 0 && "$DO_PULL" = 1 && ! "$RELEASE_TAG" =~ ^v[0-9] ]]; then
  echo "ERROR: production deploy requires --tag vX.Y.Z or --continuous" >&2
  exit 2
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO="${CHUDWARE_REPO_DIR:-$REPO}"
cd "$REPO"
REMOTE="${CHUDWARE_UPDATE_REMOTE:-origin}"
REF="${CHUDWARE_UPDATE_BRANCH:-master}"
# A pinned production deploy pulls the tag ref. Staging and a continuous
# production deploy both track the branch, which is the only ref that exists
# for an untagged HEAD.
if [[ "$ENVIRONMENT" = prod && "$CONTINUOUS" = 0 ]]; then
  REF="$RELEASE_TAG"
fi

mkdir -p "$DATA_ROOT/logs"
LOGFILE="$DATA_ROOT/logs/update.log"
STATUS="$DATA_ROOT/logs/deploy-status.json"
# One host and one checkout: prod and staging must not pull or migrate in
# parallel even though their logs and databases are separate.
LOCK="${CHUDWARE_DEPLOY_LOCK:-$HOME/chudware-web-data/logs/deploy.lock}"
mkdir -p "$(dirname "$LOCK")"
# The re-exec hop after a self-replacing pull INHERITS fd 9 and the flock on it,
# because exec keeps the process and its descriptors. It must not re-open fd 9:
# doing so closes the inherited open file description, and the lock does not
# come back, because the parent's `tee` co-process below forked while fd 9 was
# open and still holds a duplicate of that description. The first attempt at
# this re-exec guard did exactly that and every second hop died with
# "busy (another prod/staging deployment holds ...)" — a deploy that correctly
# detected the stale-script bug and then refused to finish.
if [[ "${CHUDWARE_DEPLOY_REEXECED:-0}" != 1 ]]; then
  exec 9>"$LOCK"
  if ! flock -n 9; then
    echo "busy (another prod/staging deployment holds $LOCK)" >&2
    exit 3
  fi
fi

# `exec 9>&-` inside the process substitution keeps the log co-process from
# holding a duplicate of the lock descriptor for its whole lifetime.
exec > >(exec 9>&-; tee -a "$LOGFILE") 2>&1
STARTED_AT="$(date +%s)"
printf '{"running":true,"kind":"deploy-%s","started_at":%s,"last":null}\n' \
  "$ENVIRONMENT" "$STARTED_AT" >"$STATUS"
# What actually went out, resolved once the working tree is final. Declared
# before the EXIT trap so an early failure cannot trip `set -u` inside it.
RELEASE_ID=""
_cleanup() {
  local rc=$?
  local finished_at release_json
  finished_at="$(date +%s)"
  # The revision rides inside "last": deploy_ops._read_status() drops unknown
  # top-level keys but passes "last" through verbatim, so the admin Deploy card
  # and the CI log agree on which revision this run promoted. The substitution
  # keeps the file parseable even for an exotic refname; no tag or sha we would
  # ever deploy contains a JSON-hostile character.
  release_json=null
  [[ -z "$RELEASE_ID" ]] || release_json="\"${RELEASE_ID//[^A-Za-z0-9._+@\/-]/_}\""
  printf '{"running":false,"kind":null,"started_at":null,"last":{"kind":"deploy-%s","rc":%s,"finished_at":%s,"release":%s}}\n' \
    "$ENVIRONMENT" "$rc" "$finished_at" "$release_json" >"$STATUS"
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
install_current_units() {
  local unit_dir="$HOME/.config/systemd/user"
  mkdir -p "$unit_dir"
  install -m 644 "$REPO/Webservice/deploy/$GATEWAY_UNIT" "$unit_dir/$GATEWAY_UNIT"
  install -m 644 "$REPO/Webservice/deploy/$RETENTION_UNIT" "$unit_dir/$RETENTION_UNIT"
  install -m 644 "$REPO/Webservice/deploy/$RETENTION_TIMER" "$unit_dir/$RETENTION_TIMER"
  install -m 644 "$REPO/Webservice/deploy/$RELEASE_RETENTION_UNIT" "$unit_dir/$RELEASE_RETENTION_UNIT"
  install -m 644 "$REPO/Webservice/deploy/$RELEASE_RETENTION_TIMER" "$unit_dir/$RELEASE_RETENTION_TIMER"
  install -m 644 "$REPO/Webservice/deploy/$CEF_SPOOL_UNIT" "$unit_dir/$CEF_SPOOL_UNIT"
  install -m 644 "$REPO/Webservice/deploy/$CEF_SPOOL_TIMER" "$unit_dir/$CEF_SPOOL_TIMER"
  systemctl --user daemon-reload \
    || { echo "ERROR: systemd user daemon-reload failed" >&2; exit 1; }
}
enable_security_retention() {
  # Retention is operational hygiene, not a gateway availability dependency.
  # Surface failures in this deploy's journal/log but continue the gateway rollout.
  systemctl --user disable --now chudware-security-retention.timer chudware-security-retention-staging.timer >/dev/null 2>&1 || true
  if ! systemctl --user enable --now "$RETENTION_UNIT" "$RETENTION_TIMER"; then
    echo "WARN: failed to enable retention for $ENVIRONMENT; expired security batches will not be purged" >&2
  fi
}
enable_release_retention() {
  # Separate from the security timer above because the failure modes are not
  # comparable. Expired security batches are a database-size problem; superseded
  # release artifacts are a DISK problem — one nightly now writes an AppImage
  # plus a .deb, an .rpm and a .tar.zst, which on a filesystem with under 4 GB
  # free is roughly ten nights before PostgreSQL and the tunnel go down with the
  # gateway. The admin route stays for a manual dry-run-then-confirm, but
  # nothing load-bearing may depend on somebody pressing it.
  #
  # Both environments' timers are disabled first so a prod deploy cannot leave a
  # staging timer running against the staging database, or the reverse.
  systemctl --user disable --now chudware-release-retention.timer chudware-release-retention-staging.timer >/dev/null 2>&1 || true
  if ! systemctl --user enable --now "$RELEASE_RETENTION_UNIT" "$RELEASE_RETENTION_TIMER"; then
    echo "WARN: failed to enable release retention for $ENVIRONMENT; superseded beta artifacts will accumulate until the disk fills" >&2
  fi
}
enable_cef_spool_reaper() {
  # Bounds the Chromium persistent-histogram spool the CEF host leaks into
  # ~/.local/share/chudware/cef_cache_*/{BrowserMetrics,DeferredBrowserMetrics}.
  # Measured 2026-08-27 on this host: 2,790 files / 10.5 GB in 16 days, about
  # 650 MB/day, on the same 119 GB SSD as PostgreSQL and the tunnel.
  #
  # The host binary reaps this at CEF startup, which is necessary but not
  # sufficient here: this daemon can run for weeks without a restart, so the
  # startup reaper may never get a turn, and the host is only redeployed when
  # somebody rebuilds it. Same unit for both environments — it bounds a
  # per-user cache path, not a per-environment one.
  if ! systemctl --user enable --now "$CEF_SPOOL_UNIT" "$CEF_SPOOL_TIMER"; then
    echo "WARN: failed to enable the CEF metrics spool reaper; the browser cache roots will grow at roughly 650 MB/day until the disk fills" >&2
  fi
}
assert_entitlement_credential() {
  local encrypted
  case "$ENVIRONMENT" in
    prod) encrypted="$HOME/.config/credstore.encrypted/chudware-entitlement.cred" ;;
    staging) encrypted="$HOME/.config/credstore.encrypted/chudware-entitlement-staging.cred" ;;
  esac
  [[ -f "$encrypted" && -r "$encrypted" ]] \
    || { echo "ERROR: encrypted entitlement credential is missing: $encrypted" >&2; exit 1; }
  local mode
  mode="$(stat -c '%a' "$encrypted")"
  [[ "$mode" = 600 ]] \
    || { echo "ERROR: encrypted entitlement credential must have mode 600: $encrypted ($mode)" >&2; exit 1; }
  if env_value CHUDWARE_ENTITLEMENT_KEY_PATH | grep -q .; then
    echo "ERROR: remove CHUDWARE_ENTITLEMENT_KEY_PATH from $GATEWAY_ENV; the systemd unit owns it" >&2
    exit 1
  fi
  echo "    entitlement credential: encrypted, mode 600"
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

# Everything in this block runs BEFORE the first change to production, so each
# rejection here is a refusal (exit 4), not a part-way failure (exit 1).
if [[ "$DO_GATEWAY" = 1 ]]; then
  [[ -r "$GATEWAY_ENV" ]] \
    || refuse "gateway environment is unreadable: $GATEWAY_ENV"
  DB_URL="$(env_value CHUDWARE_DB_URL)"
  [[ -n "$DB_URL" ]] \
    || refuse "CHUDWARE_DB_URL is missing from $GATEWAY_ENV"
  DEV_MODE="$(env_value CHUDWARE_WEB_DEV)"
  case "$DEV_MODE" in
    ""|0|false|False|FALSE|no|No|NO|off|Off|OFF) ;;
    *) refuse "CHUDWARE_WEB_DEV must be disabled for $ENVIRONMENT deployments" ;;
  esac
  assert_entitlement_credential
  assert_database_target
  backup_database
fi

if [[ "$DO_PULL" = 1 ]]; then
  echo "=== [$(date -Iseconds)] git fetch/pull $REMOTE $REF ==="
  git fetch "$REMOTE" "$REF"
  if [[ -n "$(git status --porcelain)" ]]; then
    git status -sb >&2
    refuse "working tree dirty at $REPO — commit/stash before update.sh"
  fi
  BEFORE="$(git rev-parse --short HEAD)"
  git pull --ff-only "$REMOTE" "$REF"
  AFTER="$(git rev-parse --short HEAD)"
  echo "    HEAD $BEFORE -> $AFTER"
  if [[ "$BEFORE" != "$AFTER" ]]; then
    git log --oneline -n 30 "$BEFORE..$AFTER"
  fi
fi

# Re-exec when the pull replaced THIS script.
#
# Bash reads a script incrementally rather than slurping it, so a pull that
# rewrites update.sh mid-run leaves the already-parsed definitions in memory
# while later reads come from the new bytes. On 2026-08-27 that produced a
# deploy which reported "finished ok" while silently running the PRE-pull
# copy: install_current_units was still the three-unit version and
# enable_release_retention — a function the new file adds and calls — was never
# invoked, so a newly added systemd timer was not installed and nothing said
# so. A deploy that omits half of what the committed script asks for and still
# exits 0 is the worst failure shape available, so re-exec once, under a guard,
# and say that it happened.
#
# The lock is briefly released across the handoff: exec inherits fd 9, but the
# new process runs its own `exec 9>"$LOCK"` and flock, which closes the
# inherited file description and drops the lock with it. That window is
# acceptable — one host, and the workflow's concurrency group already
# serialises deploys — but do not describe this as lock-preserving.
# CHUDWARE_DEPLOY_REEXECED bounds it to a single hop even if the new copy
# somehow hashes differently again.
if [[ "${CHUDWARE_DEPLOY_REEXECED:-0}" != 1 && -f "$SELF" ]]; then
  SELF_SHA_AFTER="$(sha256sum "$SELF" | cut -d' ' -f1)"
  if [[ "$SELF_SHA_AFTER" != "$SELF_SHA_BEFORE" ]]; then
    echo "=== [$(date -Iseconds)] update.sh changed in this pull; re-executing the new copy ==="
    echo "    ${SELF_SHA_BEFORE:0:12} -> ${SELF_SHA_AFTER:0:12}"
    export CHUDWARE_DEPLOY_REEXECED=1
    # --no-pull: the pull already happened and the tree is at the target ref.
    # Re-pulling would be harmless but re-checking a v* tag against a moving
    # remote is not, and it keeps the second hop's log honest about what it did.
    exec bash "$SELF" "$ENVIRONMENT" "${ORIGINAL_ARGV[@]:1}" --no-pull
  fi
fi

RELEASE_ID="$(git rev-parse HEAD)"
if [[ "$ENVIRONMENT" = prod ]]; then
  if [[ "$CONTINUOUS" = 1 ]]; then
    echo "    UNTAGGED CONTINUOUS DEPLOYMENT: promoting current $REF HEAD to production; no v* release tag gates this run"
    echo "    production commit: $RELEASE_ID $(git log -1 --format=%s HEAD)"
  else
    HEAD_TAG="$(git describe --tags --exact-match HEAD 2>/dev/null || true)"
    [[ "$HEAD_TAG" =~ ^v[0-9] ]] \
      || refuse "production HEAD is not an exact v* release tag"
    if [[ -n "$RELEASE_TAG" && "$HEAD_TAG" != "$RELEASE_TAG" ]]; then
      refuse "production HEAD tag $HEAD_TAG does not match requested $RELEASE_TAG"
    fi
    RELEASE_ID="$HEAD_TAG"
    echo "    production release tag: $HEAD_TAG"
  fi
fi

if [[ "$DO_HOST" = 1 ]]; then
  echo "=== [$(date -Iseconds)] ./build.sh (host GUI) ==="
  ./build.sh
fi

ENGINE_ARTIFACT_DIR="$REPO/core/build-occt-linux/bin"
if [[ "$DO_ENGINE" = 1 ]]; then
  if [[ "$DO_WEBD" = 1 ]]; then
    [[ -f "$REPO/core/build-occt-linux/CMakeCache.txt" ]] \
      || { echo "ERROR: webd engine tree is not configured: core/build-occt-linux" >&2; exit 1; }
    echo "=== [$(date -Iseconds)] configure required webd PolyMesh backend ==="
    cmake -S core -B core/build-occt-linux -DCHUDWARE_WITH_POLYMESH=ON
  fi
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
  install_current_units
  enable_security_retention
  enable_release_retention
  enable_cef_spool_reaper
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
