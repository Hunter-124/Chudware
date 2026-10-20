#!/usr/bin/env bash
# ===========================================================================
# update.sh — environment-aware gateway/native deployment for livingroom-pc.
#
# Usage:
#   bash Webservice/deploy/update.sh staging
#   bash Webservice/deploy/update.sh prod --tag v1.2.0
#   bash Webservice/deploy/update.sh prod --continuous
#
# Automatic master activation is suspended pending independent release qualification.
# An explicit operator/authorized manual workflow may select --continuous or a tag;
# neither bypasses protected configuration, exact-ref or rollback-authority checks.
# Retained rollback covers native/cell payloads only, never gateway source or DB.
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
  --first-cutover-ack PATH  Explicit authority-registry record for this exact commit

Health-test overrides:
  CHUDWARE_DEPLOY_HEALTH_BASE_URL  defaults to the selected loopback gateway
  CHUDWARE_DEPLOY_HEALTH_ATTEMPTS  defaults to 30
EOF
}

# Exit codes are a contract with .github/workflows/deploy-prod.yml:
#   0  deployed
#   1  FAILED PART-WAY: production may have changed; classify native vs manual recovery
#   2  usage error
#   3  another deploy holds the host lock
#   4  REFUSED BEFORE LIVE MUTATION: no service/payload/checkout/schema change; no rollback
# Code 4 exists because a precondition refusal used to exit 1, and the workflow
# then ran rollback.sh against a production it had never touched. The rollback
# failed on its own unrelated grounds ("release ... is not restorable") and every
# push for four days reported "ROLLBACK ITSELF FAILED ... Production needs
# hands-on recovery now" when the only real problem was a dirty checkout.
MUTATED="${CHUDWARE_DEPLOY_MUTATED:-0}"
refuse() {
  echo "REFUSED: $1" >&2
  if [[ "$MUTATED" = 0 ]]; then
    echo "         live deployment was not changed by this run." >&2
    exit 4
  fi
  echo "         deployment already changed; operator recovery may be required." >&2
  exit 1
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
    # The private-FD health/release helper uses the selected gateway's loopback port.
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
if [[ "$ENVIRONMENT" = staging ]]; then
  [[ "$(id -un)" = chudware-staging && "$HOME" = /var/lib/chudware-staging ]] \
    || refuse "staging requires the dedicated chudware-staging account and home"
elif [[ "$(id -un)" = chudware-staging ]]; then
  refuse "staging identity cannot deploy production"
fi
export CHUDWARE_DEPLOY_ENV="$ENVIRONMENT"

DO_PULL=1
DO_GATEWAY=1
RELEASE_TAG=""
CONTINUOUS=0
FIRST_CUTOVER_ACK=""
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
    --first-cutover-ack)
      [[ $# -ge 2 ]] || { echo "ERROR: --first-cutover-ack requires a path" >&2; exit 2; }
      FIRST_CUTOVER_ACK="$2"
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
GATEWAY_PY="${CHUDWARE_GATEWAY_PYTHON:-$REPO/Webservice/gateway/.venv/bin/python}"
cd "$REPO" || refuse "deployment checkout is unavailable"
source "$REPO/Webservice/deploy/deployment_drain.sh" || refuse "private maintenance channel is invalid"
REMOTE="${CHUDWARE_UPDATE_REMOTE:-origin}"
REF="${CHUDWARE_UPDATE_BRANCH:-master}"
# A pinned production deploy pulls the tag ref. Staging and a continuous
# production deploy both track the branch, which is the only ref that exists
# for an untagged HEAD.
if [[ "$ENVIRONMENT" = prod && "$CONTINUOUS" = 0 ]]; then
  REF="$RELEASE_TAG"
fi

mkdir -p "$DATA_ROOT/logs" || refuse "deployment log directory is unavailable"
LOGFILE="$DATA_ROOT/logs/update.log"
STATUS="$DATA_ROOT/logs/deploy-status.json"
# One host and one checkout: prod and staging must not pull or migrate in
# parallel even though their logs and databases are separate.
LOCK="${CHUDWARE_DEPLOY_LOCK:-$HOME/chudware-web-data/logs/deploy.lock}"
mkdir -p "$(dirname "$LOCK")" || refuse "deployment lock directory is unavailable"
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
  "$ENVIRONMENT" "$STARTED_AT" >"$STATUS" || refuse "deployment status is not writable"
# What actually went out, resolved once the working tree is final. Declared
# before the EXIT trap so an early failure cannot trip `set -u` inside it.
RELEASE_ID=""
_cleanup() {
  local rc=$?
  trap - EXIT
  if [[ "$rc" != 0 ]]; then
    if [[ "${DRAIN_ACQUIRED:-0}" = 1 ]]; then deployment_drain_abort || true; fi
    if [[ "$MUTATED" = 1 ]]; then rc=1; else rc=4; fi
  fi
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
  exit "$rc"
}
trap _cleanup EXIT

env_value() {
  local key="$1"
  env -i PATH=/usr/bin:/bin /usr/bin/python3 -I -S - \
      "$REPO/Webservice/deploy" "$GATEWAY_ENV" "$ENVIRONMENT" "$key" <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from gateway_preflight import load_deploy_environment
try:
    values = load_deploy_environment(Path(sys.argv[2]), sys.argv[3])
    print(values.get(sys.argv[4], ""), end="")
except (OSError, ValueError):
    raise SystemExit("protected deployment configuration rejected") from None
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
    refuse "database target does not match the selected environment"
  fi
  echo "    database isolation: $actual"
}

backup_database() {
  local gateway_python="${CHUDWARE_GATEWAY_PYTHON:-$REPO/Webservice/gateway/.venv/bin/python}"
  [[ -x "$gateway_python" ]] \
    || refuse "gateway Python is required for the encrypted pre-deploy recovery backup"
  echo "=== [$(date -Iseconds)] encrypted pg_dump manual-recovery backup ($EXPECTED_DB) ==="
  if ! CHUDWARE_DB_URL="$DB_URL" CHUDWARE_DEPLOY_ENV="$ENVIRONMENT" \
      "$gateway_python" "$REPO/Webservice/deploy/backup_state.py" database \
      --environment "$ENVIRONMENT" --data-root "$DATA_ROOT" --destination "$DATA_ROOT/backups"; then
    echo "ERROR: encrypted backup failed; deployment has not changed the database or service" >&2
    refuse "encrypted backup failed before deployment mutation"
  fi
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
    env -i HOME="$HOME" PATH=/usr/bin:/bin "$gateway_python" - \
        "$REPO/Webservice/deploy" "$GATEWAY_ENV" "$ENVIRONMENT" <<'PY'
from pathlib import Path
import os
import sys
sys.path.insert(0, sys.argv[1])
from gateway_preflight import load_deploy_environment
values = load_deploy_environment(Path(sys.argv[2]), sys.argv[3])
selected = {name: value for name, value in values.items() if name.startswith("CHUDWARE_")}
os.environ.update(selected)
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
  # Enable only this environment; deploying staging never stops production timers.
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
    || refuse "encrypted entitlement credential is missing or unreadable"
  local mode
  mode="$(stat -c '%a' "$encrypted")"
  [[ "$mode" = 600 && ! -L "$encrypted" && "$(stat -c '%u' "$encrypted")" = "$(id -u)" ]] \
    || refuse "encrypted entitlement credential must be a protected owner-only regular file"
  if env_value CHUDWARE_ENTITLEMENT_KEY_PATH | grep -q .; then
    echo "ERROR: remove CHUDWARE_ENTITLEMENT_KEY_PATH from $GATEWAY_ENV; the systemd unit owns it" >&2
    refuse "plaintext entitlement key path is forbidden"
  fi
  echo "    entitlement credential: encrypted, mode 600"
}


acquire_gateway_maintenance() {
  deployment_drain_acquire \
    || refuse "operator maintenance could not prove all work idle and every cell inactive; no cutover"
}


echo "=== chudware $ENVIRONMENT deploy start $(date -Iseconds) repo=$REPO ==="
echo "    pull=$DO_PULL ref=${REF:-current} host=$DO_HOST engine=$DO_ENGINE webd=$DO_WEBD gateway=$DO_GATEWAY"

# Everything in this block runs BEFORE the first change to production, so each
# rejection here is a refusal (exit 4), not a part-way failure (exit 1).
if [[ "$DO_GATEWAY" = 1 || "$DO_WEBD" = 1 || "$DO_PULL" = 1 ]]; then
  GATEWAY_PY="${CHUDWARE_GATEWAY_PYTHON:-$REPO/Webservice/gateway/.venv/bin/python}"
  [[ "${CHUDWARE_DEPLOY_HEALTH_BASE_URL:-http://127.0.0.1:$GATEWAY_PORT}" = "http://127.0.0.1:$GATEWAY_PORT" ]] \
    || refuse "authenticated health is restricted to the selected loopback gateway"
  [[ -x "$GATEWAY_PY" ]] || refuse "gateway runtime is unavailable"
  "$GATEWAY_PY" "$REPO/Webservice/deploy/gateway_preflight.py" validate \
    --env-file "$GATEWAY_ENV" --environment "$ENVIRONMENT" \
    || refuse "protected gateway configuration is not release-ready"
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
fi
if [[ "$DO_WEBD" = 1 ]]; then
  [[ "${CHUDWARE_BASE_PYTHON:-}" = /usr/bin/python3 ]] \
    || refuse "authenticated cell worker base runtime is required"
  env -i PATH=/usr/bin:/bin /usr/bin/python3 -I -S \
    "$REPO/Webservice/deploy/deployment_release.py" runtime-preflight \
    --repo "$REPO" --wheelhouse "${CHUDWARE_WHEELHOUSE:-$REPO/wheelhouse}" \
    || refuse "authenticated offline cell worker runtime inputs are unavailable"
fi

# Resolve the exact candidate without changing the canonical gateway checkout.
if [[ "$DO_PULL" = 1 ]]; then
  git fetch "$REMOTE" "$REF"
  TARGET_COMMIT="$(git rev-parse 'FETCH_HEAD^{commit}')"
else
  TARGET_COMMIT="$(git rev-parse HEAD)"
fi
[[ "$TARGET_COMMIT" =~ ^[0-9a-f]{40}$ ]] || refuse "target commit is unresolved"
[[ -z "${CHUDWARE_DEPLOY_TARGET_SHA:-}" || "$TARGET_COMMIT" = "$CHUDWARE_DEPLOY_TARGET_SHA" ]] \
  || refuse "candidate no longer matches the independently gated commit"
[[ -z "$(git status --porcelain)" ]] || refuse "working tree is dirty"
AUTHORITY_ARGS=(promotion-preflight --web-home "${CHUDWARE_WEB_HOME:-$HOME/chudware-web}" \
  --environment "$ENVIRONMENT" --target-commit "$TARGET_COMMIT")
if [[ "$ENVIRONMENT" = staging ]]; then
  AUTHORITY_ARGS=(promotion-preflight --web-home "${CHUDWARE_WEB_HOME:-$HOME/chudware-web-staging}" \
    --environment "$ENVIRONMENT" --target-commit "$TARGET_COMMIT")
fi
[[ -z "$FIRST_CUTOVER_ACK" ]] || AUTHORITY_ARGS+=(--first-cutover-ack "$FIRST_CUTOVER_ACK")
if [[ "$DO_GATEWAY" = 1 || "$DO_WEBD" = 1 || "$DO_HOST" = 1 || "$DO_PULL" = 1 ]]; then
  env -i PATH=/usr/bin:/bin /usr/bin/python3 -I -S \
    "$REPO/Webservice/deploy/deployment_release.py" "${AUTHORITY_ARGS[@]}" \
    || refuse "rollback authority is unavailable for this exact candidate"
fi
if [[ "$DO_GATEWAY" = 1 || "$DO_WEBD" = 1 || "$DO_PULL" = 1 ]]; then
  acquire_gateway_maintenance
fi
if [[ "$DO_GATEWAY" = 1 ]]; then
  backup_database
fi

# This marker is not release authority. It only prevents an automated native
# rollback from pretending it restored canonical gateway files or database state.
mark_gateway_mutation() {
  MUTATED=1
  printf 'manual_gateway_checkout_and_database_schema_recovery_required\n' \
    > "$DATA_ROOT/logs/deploy-recovery-required"
}

if [[ "$DO_PULL" = 1 ]]; then
  echo "=== [$(date -Iseconds)] git fetch/pull $REMOTE $REF ==="
  git fetch "$REMOTE" "$REF"
  if [[ -n "$(git status --porcelain)" ]]; then
    git status -sb >&2
    refuse "working tree dirty at $REPO — commit/stash before update.sh"
  fi
  BEFORE="$(git rev-parse --short HEAD)"
  mark_gateway_mutation
  git merge --ff-only "$TARGET_COMMIT"
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
    CHUDWARE_DEPLOY_MUTATED=1 exec bash "$SELF" "$ENVIRONMENT" "${ORIGINAL_ARGV[@]:1}" --no-pull
  fi
fi

RELEASE_ID="$(git rev-parse HEAD)"
[[ "$RELEASE_ID" = "$TARGET_COMMIT" ]] || refuse "checkout does not match the qualified target"
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
  MUTATED=1
  ./build.sh
fi

ENGINE_ARTIFACT_DIR="$REPO/core/build-occt-linux/bin"
if [[ "$DO_ENGINE" = 1 ]]; then
  if [[ "$DO_WEBD" = 1 ]]; then
    [[ -f "$REPO/core/build-occt-linux/CMakeCache.txt" ]] \
      || { echo "ERROR: webd engine tree is not configured: core/build-occt-linux" >&2; exit 1; }
    MUTATED=1
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
  INSTALL_ARGS=(--restart)
  [[ -z "$FIRST_CUTOVER_ACK" ]] || INSTALL_ARGS+=(--first-cutover-ack "$FIRST_CUTOVER_ACK")
  # The child may fail after changing native policy; conservatively unsafe from
  # this boundary onward, even when the child itself can report a local refusal.
  MUTATED=1
  CHUDWARE_WEBD_ARTIFACT_DIR="$ENGINE_ARTIFACT_DIR" \
    bash Webservice/deploy/install_webd.sh "${INSTALL_ARGS[@]}"
fi

if [[ "$DO_GATEWAY" = 1 ]]; then
  deployment_drain_call check || refuse "maintenance lost authoritative inactive-cell proof"
  mark_gateway_mutation
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
fi
deployment_drain_complete || refuse "qualified cutover health/release failed; maintenance retained"

echo "=== chudware $ENVIRONMENT deploy finished ok $(date -Iseconds) ==="
