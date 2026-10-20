#!/usr/bin/env bash
# ===========================================================================
# rollback.sh restores only an independently authorized native/cell payload.
# It NEVER restores the canonical gateway checkout, Python dependencies or DB
# schema/data. Shared local/native daemons are never stopped or restarted here.
# Automatic mode refuses unknown/incompatible gateway/schema recovery (exit 5).
# ===========================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENVIRONMENT="${CHUDWARE_DEPLOY_ENV:-prod}"
case "$ENVIRONMENT" in
  prod)
    [[ "$(id -un)" != chudware-staging ]] || { echo "REFUSED: staging identity cannot roll back production" >&2; exit 2; }
    DST="${CHUDWARE_WEB_HOME:-$HOME/chudware-web}"
    KEY="chudwareweb"
    HOSTD_UNIT="chudware-hostd.service"
    DATA_DEFAULT="$HOME/chudware-web-data"
    GATEWAY_PORT=8300
    GATEWAY_UNIT="chudware-gateway.service"
    ;;
  staging)
    [[ "$(id -un)" = chudware-staging && "$HOME" = /var/lib/chudware-staging ]] \
      || { echo "REFUSED: staging requires its dedicated account and home" >&2; exit 2; }
    DST="${CHUDWARE_WEB_HOME:-$HOME/chudware-web-staging}"
    KEY="chudwareweb-staging"
    HOSTD_UNIT="chudware-hostd-staging.service"
    DATA_DEFAULT="$HOME/chudware-web-staging"
    GATEWAY_PORT=8310
    GATEWAY_UNIT="chudware-gateway-staging.service"
    ;;
  *) echo "REFUSED: CHUDWARE_DEPLOY_ENV must be prod or staging" >&2; exit 2 ;;
esac
export CHUDWARE_DEPLOY_ENV="$ENVIRONMENT"
RELEASES="$DST/releases"
BIN="$DST/bin"
APP="$DST/app"
UNIT_DIR="$HOME/.config/systemd/user"
MUTATED=0

fatal() {
  echo "[rollback] ERROR: $*" >&2
  if [[ "$MUTATED" = 0 ]]; then exit 4; else exit 1; fi
}

# Exit 2 is "there is nothing here to do" — bad arguments, no second release yet,
# the requested target is already live. It is deliberately distinct from exit 1
# so a CD workflow can tell "rollback could not run" from "rollback broke".
refuse() {
  echo "[rollback] $*" >&2
  exit 2
}

usage() {
  echo "usage: $0 --list" >&2
  echo "       $0 [--to <release-id>] [--automatic]" >&2
  exit 2
}

LIST=0
AUTOMATIC=0
TARGET_ID=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --list) LIST=1 ;;
    --automatic) AUTOMATIC=1 ;;
    --to)
      shift
      [ "$#" -gt 0 ] || usage
      TARGET_ID="$1"
      ;;
    --to=*) TARGET_ID="${1#--to=}" ;;
    *) usage ;;
  esac
  shift
done
if [ "$LIST" = 1 ] && { [ "$AUTOMATIC" = 1 ] || [ -n "$TARGET_ID" ]; }; then
  usage
fi
case "$TARGET_ID" in
  "") ;;
  */*|.|..) refuse "invalid release id '$TARGET_ID'; pass the directory name shown by --list" ;;
esac
if [[ "$AUTOMATIC" = 1 ]]; then
  if [[ -e "${CHUDWARE_WEB_DATA:-$DATA_DEFAULT}/logs/deploy-recovery-required" ]]; then
    echo "[rollback] OPERATOR RECOVERY REQUIRED: gateway checkout/dependencies/database schema may have changed. Native rollback cannot recover them; preserve customer data and use the independently drilled manual recovery plan." >&2
  else
    # Schema-7 runtime authorization does not authenticate checkout_head metadata
    # or current DB schema compatibility. Absence of a mutation marker is not proof.
    echo "[rollback] OPERATOR RECOVERY REQUIRED: gateway source/dependency/schema compatibility is UNKNOWN; the retained authority covers native/cell payload bytes only. No automatic full rollback or blind database restore is safe." >&2
  fi
  exit 5
fi

# A missing or half-built web root is an operator/host mistake, not a rollback
# failure, and must not read as a stack of shell errors from later commands.
[ -d "$DST" ] || refuse "no webd root at $DST (set CHUDWARE_WEB_HOME if it lives elsewhere)"
[ -d "$RELEASES" ] || refuse "no release store at $RELEASES; nothing has ever been promoted here"

# --------------------------------------------------------------------------
# Release inventory
# --------------------------------------------------------------------------
# Ordering rule mirrored from install_webd.sh's retention collector and must be
# kept in step with it: a release id begins with its own UTC promotion stamp
# (%Y%m%dT%H%M%SZ) so the directory name sorts chronologically as plain text,
# and only a directory that does not carry that stamp falls back to its mtime,
# rendered into the same shape so both kinds interleave correctly. Retention and
# rollback disagreeing about which release is "newest" would let the collector
# reap exactly the tree an operator is about to roll back onto.
list_releases_newest_first() {
  local dir name
  for dir in "$RELEASES"/*; do
    [ -d "$dir" ] || continue
    name="${dir##*/}"
    if [[ "$name" =~ ^[0-9]{8}T[0-9]{6}Z?- ]]; then
      printf '%s\t%s\n' "$name" "$name"
    else
      printf '%s\t%s\n' \
        "$(date -u -d "@$(stat -c %Y "$dir" 2>/dev/null || echo 0)" +%Y%m%dT%H%M%SZ)" "$name"
    fi
  done | sort -r
}

# The manifest is a flat key=value file, read the same way install_webd.sh reads
# joint_sha256 out of it. Absent keys yield an empty string rather than failing:
# a release predating a given key is still a legitimate rollback target.
manifest_value() {
  local file="$1" key="$2"
  [ -f "$file" ] || return 0
  sed -n "s/^$key=//p" "$file" | sed -n '1p'
}

CURRENT_REAL="$(readlink -f "$DST/current" 2>/dev/null || true)"
CURRENT_ID=""
if [ -n "$CURRENT_REAL" ]; then
  case "$CURRENT_REAL" in
    "$RELEASES"/*) CURRENT_ID="${CURRENT_REAL##*/}" ;;
  esac
fi

if [ "$LIST" = 1 ]; then
  found=0
  echo "[rollback] retained releases in $RELEASES (newest first, * = live):"
  while IFS=$'\t' read -r _ name; do
    found=1
    manifest="$RELEASES/$name/deploy-manifest"
    joint="$(manifest_value "$manifest" joint_sha256)"
    rev="$(manifest_value "$manifest" checkout_head)"
    built="$(manifest_value "$manifest" built_at_utc)"
    joint="${joint:0:12}"
    rev="${rev:0:12}"
    marker=" "
    [ "$name" = "$CURRENT_ID" ] && marker="*"
    # A release with no manifest (hand-copied, or predating a key) still lists as a
    # selectable target; the columns read "-" rather than going blank and looking
    # like a parse failure.
    printf '%s %-38s  joint %-12s  git %-12s  built %s\n' \
      "$marker" "$name" "${joint:--}" "${rev:--}" "${built:--}"
  done < <(list_releases_newest_first)
  [ "$found" = 1 ] || echo "[rollback] (none)"
  exit 0
fi

# --------------------------------------------------------------------------
# Host tooling (not release-specific — a missing python3 is an operator fault
# and no amount of trying older releases fixes it)
# --------------------------------------------------------------------------
PYTHON="$ROOT/Webservice/gateway/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3 || true)"
fi
[ -n "$PYTHON" ] && [ -x "$PYTHON" ] || fatal "python3 is required for artifact and drain validation"
command -v sha256sum >/dev/null 2>&1 || fatal "sha256sum is required"

# --------------------------------------------------------------------------
# Prove a candidate is still a complete, loadable release before anything moves
# --------------------------------------------------------------------------
# Returns 0 and sets TARGET_DIR/TARGET_JOINT, or prints why the
# candidate is unusable and returns 1. It is a PREDICATE, not a fatal path, so
# automatic selection can walk past a corrupt tree: on 2026-08-31 the newest
# retained release on livingroom-pc had an empty bin/chud, so every rollback
# attempt died on that one directory and reported "ROLLBACK ITSELF FAILED ...
# Production needs hands-on recovery now" while older, perfectly restorable
# releases sat untried next to it. A safety net that gives up on the first torn
# tree is decoration.
reject_candidate() {
  echo "[rollback] candidate $1 unusable: $2" >&2
  return 1
}

SNAPSHOT_DIR=""
SNAPSHOT_PUBLISHED=0
cleanup_snapshot() {
  if [ "$SNAPSHOT_PUBLISHED" = 0 ] && [ -n "$SNAPSHOT_DIR" ]; then
    rm -rf -- "$SNAPSHOT_DIR"
  fi
}
rollback_exit() {
  local rc=$?
  trap - EXIT
  cleanup_snapshot
  if [[ "$rc" != 0 && "$rc" != 2 && "$rc" != 5 ]]; then
    if [[ "${DRAIN_ACQUIRED:-0}" = 1 ]]; then deployment_drain_abort || true; fi
    if [[ "$MUTATED" = 0 ]]; then rc=4; else rc=1; fi
  fi
  exit "$rc"
}
trap rollback_exit EXIT


validate_release() {
  local id="$1"
  cleanup_snapshot
  SNAPSHOT_DIR=""
  local dir="$RELEASES/$id"
  local snapshot manifest joint f
  if ! snapshot="$(env -i PATH=/usr/bin:/bin /usr/bin/python3 -I -S \
      "$ROOT/Webservice/deploy/deployment_release.py" snapshot "$dir" \
      --parent "$RELEASES" --environment "$ENVIRONMENT")"; then
    reject_candidate "$id" "runtime evidence or independent operator authorization failed"
    return 1
  fi
  SNAPSHOT_DIR="$snapshot"
  dir="$snapshot"
  manifest="$dir/deploy-manifest"
  joint="$(manifest_value "$manifest" joint_sha256)"
  for f in bin/chudware-hostd bin/libchudware.so bin/chud CHUDCODE_ARTIFACT.lock \
    CHUDCODE-NOTICE CHUDCODE-THIRD-PARTY-NOTICES.txt \
    app/index.html app/Chudware.js app/Chudware.wasm; do
    [ -s "$dir/$f" ] || reject_candidate "$id" "$dir/$f missing or empty" || return 1
  done
  [ -x "$dir/bin/chudware-hostd" ] && [ -x "$dir/bin/chud" ] \
    || reject_candidate "$id" "authorized runtime binary is not executable" || return 1
  # A retained release needs its own target, not every platform's download.
  # Validate notices independently; the binary check below requires the Linux pin.
  "$PYTHON" "$ROOT/scripts/pin_chudcode.py" \
    --lock "$dir/CHUDCODE_ARTIFACT.lock" verify-notices --source-dir "$dir" >/dev/null 2>&1 \
    || reject_candidate "$id" "Chudcode notices do not satisfy their artifact lock" || return 1
  "$PYTHON" "$ROOT/scripts/pin_chudcode.py" \
    --lock "$dir/CHUDCODE_ARTIFACT.lock" verify \
    --target linux-x86_64 --input "$dir/bin/chud" >/dev/null 2>&1 \
    || reject_candidate "$id" "Chudcode binary does not satisfy its artifact lock" || return 1
  # Same pre-flight install_webd.sh runs against its stage: a release whose daemon
  # can no longer load its own library (a half-deleted tree, a bad restore) must be
  # rejected while `current` still points at something that works.
  env -i PATH=/usr/bin:/bin HOME="$DST" LD_LIBRARY_PATH="$dir/bin" \
    timeout 10s "$dir/bin/chudware-hostd" --version >/dev/null 2>&1 \
    || reject_candidate "$id" "chudware-hostd cannot load its own engine set" || return 1

  TARGET_DIR="$dir"
  TARGET_JOINT="$joint"
  return 0
}

# --------------------------------------------------------------------------
# Target selection
# --------------------------------------------------------------------------
TARGET_DIR=""
TARGET_JOINT=""
if [ -n "$TARGET_ID" ]; then
  # An operator named this release. Do not silently restore a different one:
  # report exactly why the requested tree is unusable and stop.
  [ -d "$RELEASES/$TARGET_ID" ] || refuse "no retained release '$TARGET_ID'; run $0 --list"
  [ "$TARGET_ID" != "$CURRENT_ID" ] || refuse "release $TARGET_ID is already live; nothing to roll back"
  validate_release "$TARGET_ID" \
    || fatal "release $TARGET_ID is not restorable (see the candidate line above); run $0 --list and pick another"
else
  skipped=0
  while IFS=$'\t' read -r _ name; do
    [ "$name" != "$CURRENT_ID" ] || continue
    if validate_release "$name"; then
      TARGET_ID="$name"
      break
    fi
    skipped=$((skipped + 1))
  done < <(list_releases_newest_first)
  # Not a failure: retention only starts accumulating history from the first
  # promotion that runs with CHUDWARE_WEBD_KEEP_RELEASES, so a freshly deployed
  # host legitimately has nothing behind its live release.
  [ -n "$TARGET_ID" ] || refuse \
    "no restorable release to roll back to: $RELEASES holds no retained release other than the live one that passes validation (${skipped} candidate(s) rejected above)"
  [ "$skipped" -eq 0 ] \
    || echo "[rollback] selected $TARGET_ID after skipping ${skipped} unusable candidate(s)"
fi

[ ! -e "$DST/current" ] || [ -L "$DST/current" ] || fatal "$DST/current exists and is not a symlink"
if [ -L "$BIN" ] && [ "$(readlink "$BIN")" != "current/bin" ]; then
  fatal "$BIN points somewhere unexpected: $(readlink "$BIN")"
fi
if [ -L "$APP" ] && [ "$(readlink "$APP")" != "current/app" ]; then
  fatal "$APP points somewhere unexpected: $(readlink "$APP")"
fi

echo "[rollback] restoring $TARGET_ID (joint sha ${TARGET_JOINT:0:12}) over ${CURRENT_ID:-<none>}"
echo "[rollback] payload only: gateway checkout/dependencies and database schema/data are unchanged"

# --------------------------------------------------------------------------
# Operator maintenance — shared with every deployment consumer
# --------------------------------------------------------------------------
# Protected configuration and authenticated cell drain are mandatory. A failed
# gateway probe is not permission to interrupt a user's sessions.
# The private CLI speaks only authenticated loopback operator routes.
command -v systemctl >/dev/null 2>&1 || fatal "systemctl is required"

DATA_ROOT="${CHUDWARE_WEB_DATA:-$DATA_DEFAULT}"
GATEWAY_ENV="${CHUDWARE_WEB_GATEWAY_ENV:-$DATA_ROOT/gateway.env}"
GATEWAY_PY="${CHUDWARE_GATEWAY_PYTHON:-$ROOT/Webservice/gateway/.venv/bin/python}"
source "$ROOT/Webservice/deploy/deployment_drain.sh" || fatal "private maintenance channel is invalid"
[[ -x "$GATEWAY_PY" ]] || fatal "gateway runtime is unavailable"
"$GATEWAY_PY" "$ROOT/Webservice/deploy/gateway_preflight.py" validate \
  --env-file "$GATEWAY_ENV" --environment "$ENVIRONMENT" \
  || fatal "protected gateway configuration is not release-ready"
deployment_drain_acquire \
  || fatal "operator maintenance could not prove all work idle and every cell inactive; no cutover"

# --------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------
# Restore only native/cell payload paths. Captured shared/gateway unit definitions
# are authenticated inputs, not authority to replace those services.
# The installed gateway restarts under the private maintenance lease.
install_unit() {
  local src="$1" dst="$2" tmp
  tmp="$(mktemp "$UNIT_DIR/.rollback-unit.XXXXXXXX")"
  cp "$src" "$tmp"
  mv -f "$tmp" "$dst"
}
MUTATED=1
mkdir -p "$UNIT_DIR"
# Cell/egress templates are part of the authorized runtime tree (cell_runtime_sha256).
for unit in "$TARGET_DIR"/cell/units/*; do
  name="${unit##*/}"
  [[ -f "$unit" && ! -L "$unit" && "$name" =~ ^chudware-(cell|egress-broker)@\.(service|socket)$ ]] \
    || fatal "unexpected cell unit in authorized release: $name"
  install_unit "$unit" "$UNIT_DIR/$name"
done
systemctl --user daemon-reload || fatal "systemd user daemon-reload failed"

# Symlink swap mirrored from install_webd.sh: stage the new link under a pid-unique
# name and mv -Tf it over `current`, so readers never observe a missing symlink.
ln -s "releases/${TARGET_DIR##*/}" "$DST/.current.staging.$$"
mv -Tf "$DST/.current.staging.$$" "$DST/current"
SNAPSHOT_PUBLISHED=1
echo "[rollback] current -> releases/${TARGET_DIR##*/} (authorized snapshot of $TARGET_ID)"
printf '%s\n' "$TARGET_JOINT" > "$DST/.cell-payload-installed-joint.staging.$$"
mv -f "$DST/.cell-payload-installed-joint.staging.$$" "$DST/.cell-payload-installed-joint"

echo "[rollback] cell payload restored: $TARGET_ID (joint sha ${TARGET_JOINT:0:12})"
echo "[rollback] shared daemon and gateway source/dependencies/schema were not changed"
systemctl --user restart "$GATEWAY_UNIT" || fatal "gateway restart failed; maintenance retained"
systemctl --user is-active --quiet "$GATEWAY_UNIT" || fatal "gateway is not active; maintenance retained"
deployment_drain_complete || fatal "qualified rollback health/release failed; maintenance retained"
