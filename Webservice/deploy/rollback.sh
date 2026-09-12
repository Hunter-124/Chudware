#!/usr/bin/env bash
# ===========================================================================
#  rollback.sh — restore a previously promoted webd release.
# ---------------------------------------------------------------------------
#  install_webd.sh retains the newest CHUDWARE_WEBD_KEEP_RELEASES trees under
#  ~/chudware-web/releases instead of deleting the one it supersedes; this
#  script moves `current` back onto one of them and restarts hostd through the
#  same drain-then-restart-then-prove-ready sequence a forward promotion uses.
#  The target is not rebuilt; its artifact lock, compiled Chudcode binary, and
#  recorded digests are re-verified before the live paths move.
# ===========================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DST="${CHUDWARE_WEB_HOME:-$HOME/chudware-web}"
RELEASES="$DST/releases"
BIN="$DST/bin"
APP="$DST/app"
KEY="chudwareweb"
UNIT_DIR="$HOME/.config/systemd/user"

fatal() {
  echo "[rollback] ERROR: $*" >&2
  exit 1
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
  echo "       $0 [--to <release-id>] [--force]" >&2
  exit 2
}

LIST=0
FORCE=0
TARGET_ID=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --list) LIST=1 ;;
    --force) FORCE=1 ;;
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
if [ "$LIST" = 1 ] && { [ "$FORCE" = 1 ] || [ -n "$TARGET_ID" ]; }; then
  usage
fi
case "$TARGET_ID" in
  "") ;;
  */*|.|..) refuse "invalid release id '$TARGET_ID'; pass the directory name shown by --list" ;;
esac

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

validate_release() {
  local id="$1"
  local dir="$RELEASES/$id"
  local manifest="$dir/deploy-manifest"
  local joint f
  local chud_sha lock_sha notice_sha third_party_sha

  joint="$(manifest_value "$manifest" joint_sha256)"
  [ -n "$joint" ] \
    || reject_candidate "$id" "$manifest has no joint_sha256; a tree of unknown identity" || return 1
  for f in bin/chudware-hostd bin/libchudware.so bin/chud CHUDCODE_ARTIFACT.lock \
    CHUDCODE-NOTICE CHUDCODE-THIRD-PARTY-NOTICES.txt \
    app/index.html app/Chudware.js app/Chudware.wasm; do
    [ -s "$dir/$f" ] || reject_candidate "$id" "$dir/$f missing or empty" || return 1
  done
  [ -x "$dir/bin/chudware-hostd" ] \
    || reject_candidate "$id" "bin/chudware-hostd is not executable" || return 1
  [ -x "$dir/bin/chud" ] || reject_candidate "$id" "bin/chud is not executable" || return 1
  [ -f "$dir/chudware-hostd.service" ] \
    || reject_candidate "$id" "chudware-hostd.service missing; predates unit capture and cannot be restored coherently" || return 1
  [ -d "$dir/agent/prompts" ] \
    || reject_candidate "$id" "agent/prompts missing; agent roles would be unusable" || return 1

  chud_sha="$(manifest_value "$manifest" chud_sha256)"
  lock_sha="$(manifest_value "$manifest" chudcode_artifact_lock_sha256)"
  notice_sha="$(manifest_value "$manifest" chudcode_notice_sha256)"
  third_party_sha="$(manifest_value "$manifest" chudcode_third_party_notices_sha256)"
  [ -n "$chud_sha" ] && [ -n "$lock_sha" ] \
    && [ -n "$notice_sha" ] && [ -n "$third_party_sha" ] \
    || reject_candidate "$id" "predates Chudcode artifact and notice evidence" || return 1
  [ "$(sha256sum "$dir/bin/chud" | cut -d' ' -f1)" = "$chud_sha" ] \
    || reject_candidate "$id" "Chudcode binary no longer matches its deploy manifest" || return 1
  [ "$(sha256sum "$dir/CHUDCODE_ARTIFACT.lock" | cut -d' ' -f1)" = "$lock_sha" ] \
    || reject_candidate "$id" "Chudcode artifact lock no longer matches its deploy manifest" || return 1
  [ "$(sha256sum "$dir/CHUDCODE-NOTICE" | cut -d' ' -f1)" = "$notice_sha" ] \
    || reject_candidate "$id" "Chudcode notice no longer matches its deploy manifest" || return 1
  [ "$(sha256sum "$dir/CHUDCODE-THIRD-PARTY-NOTICES.txt" | cut -d' ' -f1)" = "$third_party_sha" ] \
    || reject_candidate "$id" "Chudcode third-party notices no longer match their deploy manifest" || return 1
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
  LD_LIBRARY_PATH="$dir/bin${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    "$dir/bin/chudware-hostd" --version >/dev/null 2>&1 \
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

# --------------------------------------------------------------------------
# Drain — mirrored from install_webd.sh and must be kept in step with it
# --------------------------------------------------------------------------
# Everything from here to the end of the drain loop is a faithful copy of the
# authenticated-drain block in install_webd.sh: read_env_value, with_pgpass, the
# account-store fallback that picks a loopback identity, probe_engine_bridges,
# the ops-session mint sized to outlive the wait, and the poll itself. It is
# duplicated rather than shared because these two scripts have no common library
# and a rollback must not depend on the repo checkout still matching the release
# it is restoring; if you change the drain there, change it here.
command -v curl >/dev/null 2>&1 || fatal "curl is required"
command -v systemctl >/dev/null 2>&1 || fatal "systemctl is required"

read_env_value() {
  "$PYTHON" - "$1" "$2" <<'PY'
from pathlib import Path
import shlex
import sys
path, key = Path(sys.argv[1]), sys.argv[2]
value = ""
for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, candidate = line.split("=", 1)
    if name.strip() != key:
        continue
    parts = shlex.split(candidate.strip(), comments=True, posix=True)
    value = " ".join(parts)
print(value, end="")
PY
}
with_pgpass() {
  local dsn="$1"; shift
  local pgpass rc safe_dsn
  pgpass="$(mktemp)"
  chmod 600 "$pgpass"
  safe_dsn="$(DB_DSN="$dsn" "$PYTHON" - "$pgpass" <<'PY'
import os, sys
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
d = urlparse(os.environ["DB_DSN"])
if d.scheme not in {"postgres", "postgresql"}:
    raise SystemExit("database URL must use PostgreSQL")
host, port = d.hostname or "localhost", d.port or 5432
database, user = unquote(d.path.lstrip("/")), unquote(d.username or "")
password = unquote(d.password or "")
esc = lambda value: str(value).replace("\\", "\\\\").replace(":", "\\:")
Path(sys.argv[1]).write_text(":".join(map(esc, (host, port, database, user, password))) + "\n")
print(f"postgresql://{quote(user, safe='') + '@' if user else ''}{host}:{port}/{quote(database, safe='')}")
PY
)" || { rm -f "$pgpass"; return 1; }
  if PGPASSFILE="$pgpass" "$@" "$safe_dsn"; then rc=0; else rc=$?; fi
  rm -f "$pgpass"
  return "$rc"
}

probe_engine_bridges() {
  local body
  if ! body="$(curl -fsS --max-time 3 -b "chudware_sid=$HEALTH_SID" "$HEALTH_URL/healthz")"; then
    return 1
  fi
  HEALTH_JSON="$body" "$PYTHON" -c '
import json, os
try:
    body = json.loads(os.environ["HEALTH_JSON"])
    n = body["engine_bridges"]
    if body.get("ok") is not True or type(n) is not int or n < 0:
        raise ValueError
    print(n)
except Exception:
    raise SystemExit(1)
'
}

RUNNING=0
systemctl --user is-active --quiet chudware-hostd.service && RUNNING=1
MAXW="${CHUDWARE_WEBD_RESTART_MAX_WAIT:-600}"
[[ "$MAXW" =~ ^[0-9]+$ ]] || fatal "CHUDWARE_WEBD_RESTART_MAX_WAIT must be a non-negative integer"

if [ "$RUNNING" = 1 ] && [ "$FORCE" = 0 ]; then
  DATA_ROOT="${CHUDWARE_WEB_DATA:-$HOME/chudware-web-data}"
  GATEWAY_ENV="${CHUDWARE_WEB_GATEWAY_ENV:-$DATA_ROOT/gateway.env}"
  [ -r "$GATEWAY_ENV" ] || fatal "gateway env unreadable at $GATEWAY_ENV; cannot authenticate drain health (use --force to roll back without draining)"
  FILE_DATA_ROOT="$(read_env_value "$GATEWAY_ENV" CHUDWARE_WEB_DATA)"
  [ -z "$FILE_DATA_ROOT" ] || DATA_ROOT="$FILE_DATA_ROOT"
  DATA_ROOT="${DATA_ROOT/#\~/$HOME}"
  HEALTH_EMAIL="${CHUDWARE_WEBD_HEALTH_EMAIL:-$(read_env_value "$GATEWAY_ENV" CHUDWARE_WEBD_HEALTH_EMAIL)}"
  if [ -z "$HEALTH_EMAIL" ]; then
    HEALTH_DB_URL="$(read_env_value "$GATEWAY_ENV" CHUDWARE_DB_URL)"
    [ -n "$HEALTH_DB_URL" ] || fatal "CHUDWARE_DB_URL missing from $GATEWAY_ENV; cannot select a loopback identity"
    command -v psql >/dev/null 2>&1 || fatal "psql not found; install postgresql to read the account store"
    if ! HEALTH_EMAIL="$(with_pgpass "$HEALTH_DB_URL" psql --no-password -tAXq -v ON_ERROR_STOP=1 -c \
      "SELECT email FROM accounts WHERE status='active' ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, email LIMIT 1")"; then
      fatal "could not read an active loopback identity from the account store"
    fi
  fi
  [ -n "$HEALTH_EMAIL" ] || fatal "no active account available for authenticated drain health (use --force to roll back without draining)"
  HEALTH_PORT="${CHUDWARE_WEB_GATEWAY_PORT:-8300}"
  [[ "$HEALTH_PORT" =~ ^[0-9]+$ ]] && [ "$HEALTH_PORT" -ge 1 ] && [ "$HEALTH_PORT" -le 65535 ] \
    || fatal "invalid loopback gateway port: $HEALTH_PORT"
  HEALTH_URL="http://127.0.0.1:$HEALTH_PORT"
  HEALTH_DB_URL="${HEALTH_DB_URL:-$(read_env_value "$GATEWAY_ENV" CHUDWARE_DB_URL)}"
  [ -n "$HEALTH_DB_URL" ] || fatal "CHUDWARE_DB_URL missing from $GATEWAY_ENV; cannot mint a drain health session"
  GATEWAY_DIR="$ROOT/Webservice/gateway"
  GATEWAY_PY="$GATEWAY_DIR/.venv/bin/python"
  [ -x "$GATEWAY_PY" ] || fatal "gateway venv python missing at $GATEWAY_PY; cannot mint a drain health session"
  # The session must outlive the whole drain wait, or a long poll would start
  # failing on an expired cookie and look like an unreachable gateway.
  if ! HEALTH_SID="$(cd "$GATEWAY_DIR" && CHUDWARE_DB_URL="$HEALTH_DB_URL" \
    "$GATEWAY_PY" -m authn ops-session --email "$HEALTH_EMAIL" --ttl-seconds "$((MAXW + 60))")"; then
    fatal "could not mint an ops session for $HEALTH_EMAIL; refusing unauthenticated drain"
  fi
  [ -n "$HEALTH_SID" ] || fatal "ops session for $HEALTH_EMAIL was empty; refusing unauthenticated drain"
  waited=0
  while :; do
    if ! active="$(probe_engine_bridges)"; then
      fatal "authenticated $HEALTH_URL/healthz unreachable or missing engine_bridges; refusing restart (use --force if the gateway is already down)"
    fi
    if [ "$active" = 0 ]; then
      echo "[rollback] authenticated drain reports zero active engine bridges"
      break
    fi
    if [ "$waited" -ge "$MAXW" ]; then
      fatal "$active engine bridge(s) still active after ${MAXW}s; refusing to drop users"
    fi
    echo "[rollback] $active engine bridge(s) active — deferring rollback (${waited}s/${MAXW}s)"
    sleep 5
    waited=$((waited + 5))
  done
elif [ "$RUNNING" = 1 ]; then
  echo "[rollback] --force: skipping the drain wait, active engine bridges will be dropped"
fi

# --------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------
# The release carries the hostd unit it was validated with, and that unit's hash
# is one of the inputs to its joint sha. Restoring the tree without restoring the
# unit would leave the running service under a policy the joint sha does not
# describe. The gateway units are not part of a release (install_webd.sh hashes
# them straight out of the checkout), so they are deliberately left alone here.
install_unit() {
  local src="$1" dst="$2" tmp
  tmp="$dst.staging.$$"
  cp "$src" "$tmp"
  mv -f "$tmp" "$dst"
}
mkdir -p "$UNIT_DIR"
install_unit "$TARGET_DIR/chudware-hostd.service" "$UNIT_DIR/chudware-hostd.service"
systemctl --user daemon-reload || fatal "systemd user daemon-reload failed"

# Symlink swap mirrored from install_webd.sh: stage the new link under a pid-unique
# name and mv -Tf it over `current`, so readers never observe a missing symlink.
ln -s "releases/$TARGET_ID" "$DST/.current.staging.$$"
mv -Tf "$DST/.current.staging.$$" "$DST/current"
echo "[rollback] current -> releases/$TARGET_ID"

if ! systemctl --user restart chudware-hostd.service; then
  fatal "systemctl restart chudware-hostd.service failed"
fi
ready=0
for ((attempt = 1; attempt <= 30; attempt++)); do
  if systemctl --user is-active --quiet chudware-hostd.service \
      && CHUDWARE_INSTANCE_KEY=$KEY "$BIN/chudware-hostd" --status >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
# .webd-running-joint is written only past this point. A file claiming a release
# that is not actually serving is the bookkeeping corruption the joint sha exists
# to prevent: install_webd.sh would then skip the very restart needed to fix it.
[ "$ready" = 1 ] || fatal "chudware-hostd failed readiness after rolling back to $TARGET_ID; current now points at it but the service is down"
printf '%s\n' "$TARGET_JOINT" > "$DST/.webd-running-joint.staging.$$"
mv -f "$DST/.webd-running-joint.staging.$$" "$DST/.webd-running-joint"
echo "[rollback] restored and ready: $TARGET_ID (joint sha ${TARGET_JOINT:0:12})"
