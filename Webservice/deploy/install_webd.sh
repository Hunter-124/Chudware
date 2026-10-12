#!/usr/bin/env bash
# ===========================================================================
#  install_webd.sh — promote one validated engine + WASM release to webd.
# ---------------------------------------------------------------------------
#  The engine and browser bundle are built/staged as one immutable release,
#  validated before the live `current` symlink moves, and restarted only after
#  authenticated gateway telemetry proves no engine bridges are active.
#  Superseded releases are retained rather than deleted, so rollback.sh always
#  has a validated tree to go back to.
# ===========================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DST="${CHUDWARE_WEB_HOME:-$HOME/chudware-web}"
RELEASES="$DST/releases"
BIN="$DST/bin"
APP="$DST/app"
KEY="chudwareweb"
WORKSPACE_DEFAULT="$HOME/Documents/ME489/Crimson_Crankers_Program/01_Design_Package/CAD_Build"
WORKSPACE_ROOT="${CHUDWARE_WORKSPACE_ROOT:-$WORKSPACE_DEFAULT}"
RESTART=0

case "$#:${1:-}" in
  0:) ;;
  1:--restart) RESTART=1 ;;
  *) echo "usage: $0 [--restart]" >&2; exit 2 ;;
esac

fatal() {
  echo "[webd] ERROR: $*" >&2
  exit 1
}

# Match update.sh's pre-mutation refusal contract. Call only before service
# definitions, the live release, or running services have been changed.
refuse() {
  echo "[webd] REFUSED: $*" >&2
  echo "[webd] live release and running services were not changed" >&2
  exit 4
}

# Retention depth for `releases/`, counting the release this run promotes. Before
# this existed the previous release was deleted the instant a promotion succeeded,
# which meant a bad build reaching production had nothing behind it to restore.
# Validated here rather than at GC time: the GC runs after a ~20 minute build and
# a service restart, and discovering a typo'd retention count there is useless.
KEEP_RELEASES="${CHUDWARE_WEBD_KEEP_RELEASES:-3}"
[[ "$KEEP_RELEASES" =~ ^[0-9]+$ ]] && [ "$KEEP_RELEASES" -ge 1 ] \
  || fatal "CHUDWARE_WEBD_KEEP_RELEASES must be an integer >= 1 (got '$KEEP_RELEASES')"

PYTHON="$ROOT/Webservice/gateway/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3 || true)"
fi
[ -n "$PYTHON" ] && [ -x "$PYTHON" ] || fatal "python3 is required for manifest and health validation"
command -v sha256sum >/dev/null 2>&1 || fatal "sha256sum is required"
command -v curl >/dev/null 2>&1 || fatal "curl is required"
command -v systemctl >/dev/null 2>&1 || fatal "systemctl is required"

mkdir -p "$DST" "$RELEASES"
if [ ! -d "$WORKSPACE_ROOT" ]; then
  mkdir -p "$WORKSPACE_ROOT"
  echo "[webd] created workspace root $WORKSPACE_ROOT"
fi

# DWG import on the website runs inside the shared hostd: it shells out to LibreDWG's
# `dwg2dxf` for the 2D entities (DWG export is a native writer and needs no LibreDWG).
# LibreDWG is GPL-3.0, so it is a server-side subprocess only — never linked into the
# engine and never part of the browser bundle. hostd looks on its own PATH, then in
# <XDG_DATA_HOME|~/.local/share>/Chudware/tools/libredwg/bin (core/doc/dwg.cpp
# libredwg_tool). Provision that managed copy for the environment the hostd unit
# actually runs with; a verified install returns at once with no network access, a
# first install fetches the SHA256-pinned source release and needs a C toolchain.
UNIT_XDG_DATA_HOME="$(systemctl --user show-environment 2>/dev/null |
  sed -n 's/^XDG_DATA_HOME=//p')"
if ! LIBREDWG_LOG="$(env -u APPDATA XDG_DATA_HOME="$UNIT_XDG_DATA_HOME" \
    "$PYTHON" "$ROOT/scripts/provision_libredwg.py" 2>&1)"; then
  printf '%s\n' "$LIBREDWG_LOG" >&2
  refuse "LibreDWG provisioning failed; web DWG import would be unavailable (scripts/provision_libredwg.py)"
fi
echo "[webd] ${LIBREDWG_LOG##*$'\n'}"

# update.sh passes the tree it just built. Direct/sync installs use the same
# authoritative full-engine output; never fall back to a stale slim/dist tree.
SRC="${CHUDWARE_WEBD_ARTIFACT_DIR:-$ROOT/core/build-occt-linux/bin}"
if [ "${SRC#/}" = "$SRC" ]; then
  SRC="$ROOT/$SRC"
fi
if ! SRC="$(cd "$SRC" 2>/dev/null && pwd)"; then
  fatal "engine artifact directory missing: $SRC (run ./build.sh daemon)"
fi
for f in libchudware.so chudware-hostd; do
  [ -f "$SRC/$f" ] || fatal "$SRC/$f missing (hostd and library must come from one fresh tree)"
done
[ -x "$SRC/chudware-hostd" ] || fatal "$SRC/chudware-hostd is not executable"

STAGE_ROOT="$(mktemp -d "$DST/.deploy-staging.XXXXXX")"
cleanup() {
  if [ -n "${STAGE_ROOT:-}" ] && [ -d "$STAGE_ROOT" ]; then
    rm -rf "$STAGE_ROOT"
  fi
}
trap cleanup EXIT

STAGE_BIN="$STAGE_ROOT/bin"
STAGE_APP="$STAGE_ROOT/app"
STAGE_PROMPTS="$STAGE_ROOT/agent/prompts"
mkdir -p "$STAGE_BIN" "$STAGE_APP" "$STAGE_PROMPTS"

# Stage one engine set. Optional $ORIGIN siblings are copied only from this tree.
for f in libchudware.so libngcore.so libnglib.so lib3mf.so chudware-hostd; do
  [ -e "$SRC/$f" ] || continue
  cp -a "$SRC/$f" "$STAGE_BIN/$f"
  echo "[webd] staged $f from $SRC"
done

# Stage the complete learned-mesh-advisor closure beside the engine. The backend
# resolves both ONNX Runtime and polymesh-advisor/ relative to libchudware.so;
# copying only the engine makes the optional advisor look installed while every
# request silently falls back to deterministic meshing.
command -v readelf >/dev/null 2>&1 \
  || fatal "readelf is required to inspect the engine's ONNX Runtime dependency"
if ! ENGINE_DEPS="$(readelf -d "$SRC/libchudware.so" 2>/dev/null |
    sed -n 's/.*(NEEDED).*\[\([^]]*\)\].*/\1/p')"; then
  fatal "could not inspect $SRC/libchudware.so dependencies"
fi
[ -n "$ENGINE_DEPS" ] \
  || fatal "$SRC/libchudware.so has no recorded dependencies and cannot be validated"
ADVISOR_RUNTIME_NAMES="$(printf '%s\n' "$ENGINE_DEPS" |
  sed -n '/libonnxruntime/{s|.*/||;p}' | sort -u)"
ADVISOR_PACKAGE_DIR="polymesh-advisor"
ADVISOR_REQUIRED_FILES=(model.onnx normalization.json clamps.json ood.json)
if [ -n "$ADVISOR_RUNTIME_NAMES" ]; then
  for name in $ADVISOR_RUNTIME_NAMES; do
    runtime=""
    for candidate in "$SRC/$name" "$SRC/libonnxruntime.so"; do
      if [ -f "$candidate" ]; then
        runtime="$candidate"
        break
      fi
    done
    [ -n "$runtime" ] \
      || fatal "$SRC/libchudware.so needs $name but no ONNX Runtime payload is staged"
    cp -L "$runtime" "$STAGE_BIN/$name"
    echo "[webd] staged $name from $runtime"
  done
  mkdir -p "$STAGE_BIN/$ADVISOR_PACKAGE_DIR"
  for f in "${ADVISOR_REQUIRED_FILES[@]}"; do
    [ -f "$SRC/$ADVISOR_PACKAGE_DIR/$f" ] \
      || fatal "$SRC/$ADVISOR_PACKAGE_DIR/$f missing from advisor-enabled engine build"
    cp -L "$SRC/$ADVISOR_PACKAGE_DIR/$f" "$STAGE_BIN/$ADVISOR_PACKAGE_DIR/$f"
  done
  echo "[webd] staged $ADVISOR_PACKAGE_DIR/ (${#ADVISOR_REQUIRED_FILES[@]} advisor artifacts)"
fi

# Prompts are part of the release because hostd resolves them through `current`.
#
# SOURCE is services/agent/prompts; the DESTINATION stays agent/prompts because
# that is the runtime layout chudware-hostd.service bakes in via @PROMPTS@.
# They stopped being the same path twice over: bf4f56c8 moved the tree under
# services/ and left a root `agent` symlink so old paths kept working, then
# d9db7f96 deleted that symlink on 2026-08-10 for Windows portability. This
# line still pointed at the symlink, so every deploy from 2026-08-10 onward
# died here -- which is the other half of why production served a bundle from
# 2026-08-07 for fifteen days.
PROMPTS_SRC="$ROOT/services/agent/prompts"
[ -d "$PROMPTS_SRC" ] || fatal "$PROMPTS_SRC missing (agent roles would be unusable)"
cp -a "$PROMPTS_SRC/." "$STAGE_PROMPTS/"
if ! "$PYTHON" - "$STAGE_PROMPTS" <<'PY'
from pathlib import Path
import sys
raise SystemExit(0 if next(Path(sys.argv[1]).rglob("*.md"), None) else 1)
PY
then
  fatal "$PROMPTS_SRC contains no role prompts"
fi

# Chudcode is a pinned compiled release input. Deployment never installs a source
# checkout or reaches the network: the caller supplies a local binary and the checked-in
# lock verifies both its target and sha256 before it enters the immutable release.
CHUD_INPUT="${CHUDWARE_CHUD_BIN:-}"
[ -n "$CHUD_INPUT" ] || fatal "CHUDWARE_CHUD_BIN is required"
CHUD_LOCK="$ROOT/vendor/chudcode/CHUDCODE_ARTIFACT.lock"
[ -f "$CHUD_LOCK" ] || fatal "$CHUD_LOCK missing"
if ! "$PYTHON" "$ROOT/scripts/pin_chudcode.py" stage \
    --target linux-x86_64 --input "$CHUD_INPUT" --output "$STAGE_BIN/chud"; then
  fatal "Chudcode artifact verification/staging failed"
fi
if ! "$PYTHON" "$ROOT/scripts/pin_chudcode.py" stage-notices \
    --output-dir "$STAGE_ROOT"; then
  fatal "Chudcode notice verification/staging failed"
fi
cp "$CHUD_LOCK" "$STAGE_ROOT/CHUDCODE_ARTIFACT.lock"
echo "[webd] staged verified Chudcode binary and notices"

# Build WASM into the private release stage, never directly over the live app.
EMSDK_DIR="${EMSDK:-$HOME/.emsdk}"
[ -f "$EMSDK_DIR/emsdk_env.sh" ] || fatal "emsdk not found at $EMSDK_DIR; refusing a hostd-only release"
WASM_LOG="${CHUDWARE_WEBD_WASM_LOG:-/tmp/chudware-webd-wasm.log}"
mkdir -p "$(dirname "$WASM_LOG")"
echo "[webd] building WASM into private stage (log: $WASM_LOG)"
if ! CHUDWARE_WEB_APP="$STAGE_APP" EMSDK="$EMSDK_DIR" "$ROOT/build.sh" wasm >"$WASM_LOG" 2>&1; then
  fatal "WASM build/stage failed (see $WASM_LOG)"
fi
for f in index.html Chudware.js Chudware.wasm; do
  [ -s "$STAGE_APP/$f" ] || fatal "staged WASM bundle missing/empty $f"
done
if ! "$PYTHON" - "$STAGE_APP/Chudware.wasm" <<'PY'
from pathlib import Path
import sys
raise SystemExit(0 if Path(sys.argv[1]).read_bytes()[:4] == b"\0asm" else 1)
PY
then
  fatal "staged Chudware.wasm has invalid magic"
fi

# Validate the staged daemon against the staged library before any live path moves.
if ! LD_LIBRARY_PATH="$STAGE_BIN${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
     "$STAGE_BIN/chudware-hostd" --version >/dev/null 2>&1; then
  fatal "staged chudware-hostd cannot load its staged engine set"
fi

# Stage the templated unit and hash every runtime input that requires a coordinated
# hostd/WASM cutover. The manifest is not created until both artifacts validated.
PROMPTS_DST="$DST/current/agent/prompts"
sed -e "s|@BIN@|$BIN|g" -e "s|@PROMPTS@|$PROMPTS_DST|g" -e "s|@REPO@|$ROOT|g" \
  "$ROOT/Webservice/deploy/chudware-hostd.service" > "$STAGE_ROOT/chudware-hostd.service"

HOSTD_SHA="$(sha256sum "$STAGE_BIN/chudware-hostd" | cut -d' ' -f1)"
LIB_SHA="$(sha256sum "$STAGE_BIN/libchudware.so" | cut -d' ' -f1)"
ADVISOR_RUNTIME_SHA="$("$PYTHON" - "$STAGE_BIN" <<'PY'
from hashlib import sha256
from pathlib import Path
import sys
root = Path(sys.argv[1])
files = sorted(
    [path for path in root.glob("libonnxruntime*") if path.is_file()]
    + [path for path in (root / "polymesh-advisor").glob("*") if path.is_file()]
)
h = sha256()
for path in files:
    h.update(path.relative_to(root).as_posix().encode())
    h.update(b"\0")
    h.update(path.read_bytes())
    h.update(b"\0")
print(h.hexdigest())
PY
)"
WASM_SHA="$(sha256sum "$STAGE_APP/Chudware.wasm" | cut -d' ' -f1)"
JS_SHA="$(sha256sum "$STAGE_APP/Chudware.js" | cut -d' ' -f1)"
CHUD_SHA="$(sha256sum "$STAGE_BIN/chud" | cut -d' ' -f1)"
CHUD_LOCK_SHA="$(sha256sum "$STAGE_ROOT/CHUDCODE_ARTIFACT.lock" | cut -d' ' -f1)"
CHUD_NOTICE_SHA="$(sha256sum "$STAGE_ROOT/CHUDCODE-NOTICE" | cut -d' ' -f1)"
CHUD_THIRD_PARTY_NOTICES_SHA="$(sha256sum "$STAGE_ROOT/CHUDCODE-THIRD-PARTY-NOTICES.txt" | cut -d' ' -f1)"
HOSTD_UNIT_SHA="$(sha256sum "$STAGE_ROOT/chudware-hostd.service" | cut -d' ' -f1)"
GATEWAY_UNIT_SHA="$(sha256sum "$ROOT/Webservice/deploy/chudware-gateway.service" | cut -d' ' -f1)"
STAGING_GATEWAY_UNIT_SHA="$(sha256sum "$ROOT/Webservice/deploy/chudware-gateway-staging.service" | cut -d' ' -f1)"
PROMPTS_SHA="$("$PYTHON" - "$STAGE_PROMPTS" <<'PY'
from hashlib import sha256
from pathlib import Path
import sys
root = Path(sys.argv[1])
h = sha256()
for path in sorted(p for p in root.rglob("*") if p.is_file()):
    h.update(path.relative_to(root).as_posix().encode())
    h.update(b"\0")
    h.update(path.read_bytes())
    h.update(b"\0")
print(h.hexdigest())
PY
)"
WASM_TOKEN="$("$PYTHON" - "$STAGE_APP/Chudware.wasm" <<'PY'
from hashlib import sha1
from pathlib import Path
import sys
print(sha1(Path(sys.argv[1]).read_bytes()).hexdigest()[:12])
PY
)"
CHECKOUT_HEAD="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || printf 'unknown')"
JOINT_SHA="$(printf '%s\n' "$HOSTD_SHA" "$LIB_SHA" "$ADVISOR_RUNTIME_SHA" \
  "$WASM_SHA" "$JS_SHA" "$CHUD_SHA" "$CHUD_LOCK_SHA" "$CHUD_NOTICE_SHA" \
  "$CHUD_THIRD_PARTY_NOTICES_SHA" "$HOSTD_UNIT_SHA" "$GATEWAY_UNIT_SHA" \
  "$STAGING_GATEWAY_UNIT_SHA" "$PROMPTS_SHA" \
  | sha256sum | cut -d' ' -f1)"
# One clock read feeds both the release id and the manifest timestamp, so the two
# can never straddle a second boundary and disagree. UTC rather than local time:
# release retention and rollback order trees by their lexicographic directory
# name, and a local-time stamp runs backwards for an hour on every DST fall-back,
# which would let the collector reap the newest release instead of the oldest.
BUILT_AT_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
RELEASE_ID="${BUILT_AT_UTC//[:-]/}-${JOINT_SHA:0:12}-$$"
cat > "$STAGE_ROOT/deploy-manifest" <<EOF
schema=5
release_id=$RELEASE_ID
joint_sha256=$JOINT_SHA
hostd_sha256=$HOSTD_SHA
libchudware_sha256=$LIB_SHA
advisor_runtime_sha256=$ADVISOR_RUNTIME_SHA
wasm_sha256=$WASM_SHA
javascript_sha256=$JS_SHA
chud_sha256=$CHUD_SHA
chudcode_artifact_lock_sha256=$CHUD_LOCK_SHA
chudcode_notice_sha256=$CHUD_NOTICE_SHA
chudcode_third_party_notices_sha256=$CHUD_THIRD_PARTY_NOTICES_SHA
hostd_unit_sha256=$HOSTD_UNIT_SHA
gateway_unit_sha256=$GATEWAY_UNIT_SHA
staging_gateway_unit_sha256=$STAGING_GATEWAY_UNIT_SHA
prompts_sha256=$PROMPTS_SHA
wasm_token=$WASM_TOKEN
checkout_head=$CHECKOUT_HEAD
source=$SRC
built_at_utc=$BUILT_AT_UTC
EOF
echo "[webd] joint release validated (sha ${JOINT_SHA:0:12})"


CURRENT_JOINT=""
if [ -f "$DST/current/deploy-manifest" ]; then
  CURRENT_JOINT="$(sed -n 's/^joint_sha256=//p' "$DST/current/deploy-manifest" | sed -n '1p')"
fi
RUNNING=0
systemctl --user is-active --quiet chudware-hostd.service && RUNNING=1
RUNNING_JOINT="$(cat "$DST/.webd-running-joint" 2>/dev/null || true)"
NEEDS_PROMOTION=0
[ "$CURRENT_JOINT" = "$JOINT_SHA" ] || NEEDS_PROMOTION=1
NEEDS_RESTART=0
if [ "$RESTART" = 1 ] && { [ "$RUNNING" = 0 ] || [ "$RUNNING_JOINT" != "$JOINT_SHA" ]; }; then
  NEEDS_RESTART=1
fi

if [ "$RUNNING" = 1 ] && [ "$RESTART" = 0 ] && [ "$NEEDS_PROMOTION" = 1 ]; then
  refuse "hostd is running; refusing to promote a new joint release without --restart"
fi

# Read systemd-style KEY=value entries without sourcing a secrets file as shell.
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

# A caller can require an idle proof even when hostd itself is unchanged (update.sh
# does this before bouncing the gateway). Ordinary sync only drains for a real
# hostd/WASM change. Missing session/account/telemetry is fail-closed.
REQUIRE_IDLE="${CHUDWARE_WEBD_REQUIRE_IDLE:-0}"
case "$REQUIRE_IDLE" in 0|1) ;; *) fatal "CHUDWARE_WEBD_REQUIRE_IDLE must be 0 or 1" ;; esac
if [ "$RUNNING" = 1 ] && { [ "$NEEDS_RESTART" = 1 ] || [ "$REQUIRE_IDLE" = 1 ]; }; then
  DATA_ROOT="${CHUDWARE_WEB_DATA:-$HOME/chudware-web-data}"
  GATEWAY_ENV="${CHUDWARE_WEB_GATEWAY_ENV:-$DATA_ROOT/gateway.env}"
  [ -r "$GATEWAY_ENV" ] || refuse "gateway env unreadable at $GATEWAY_ENV; cannot authenticate drain health"
  FILE_DATA_ROOT="$(read_env_value "$GATEWAY_ENV" CHUDWARE_WEB_DATA)"
  [ -z "$FILE_DATA_ROOT" ] || DATA_ROOT="$FILE_DATA_ROOT"
  DATA_ROOT="${DATA_ROOT/#\~/$HOME}"
  HEALTH_EMAIL="${CHUDWARE_WEBD_HEALTH_EMAIL:-$(read_env_value "$GATEWAY_ENV" CHUDWARE_WEBD_HEALTH_EMAIL)}"
  if [ -z "$HEALTH_EMAIL" ]; then
    HEALTH_DB_URL="$(read_env_value "$GATEWAY_ENV" CHUDWARE_DB_URL)"
    [ -n "$HEALTH_DB_URL" ] || refuse "CHUDWARE_DB_URL missing from $GATEWAY_ENV; cannot select a loopback identity"
    command -v psql >/dev/null 2>&1 || refuse "psql not found; install postgresql to read the account store"
    if ! HEALTH_EMAIL="$(with_pgpass "$HEALTH_DB_URL" psql --no-password -tAXq -v ON_ERROR_STOP=1 -c \
      "SELECT email FROM accounts WHERE status='active' ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, email LIMIT 1")"; then
      refuse "could not read an active loopback identity from the account store"
    fi
  fi
  [ -n "$HEALTH_EMAIL" ] || refuse "no active account available for authenticated drain health"
  HEALTH_PORT="${CHUDWARE_WEB_GATEWAY_PORT:-8300}"
  [[ "$HEALTH_PORT" =~ ^[0-9]+$ ]] && [ "$HEALTH_PORT" -ge 1 ] && [ "$HEALTH_PORT" -le 65535 ] \
    || refuse "invalid loopback gateway port: $HEALTH_PORT"
  HEALTH_URL="http://127.0.0.1:$HEALTH_PORT"
  MAXW="${CHUDWARE_WEBD_RESTART_MAX_WAIT:-600}"
  [[ "$MAXW" =~ ^[0-9]+$ ]] || refuse "CHUDWARE_WEBD_RESTART_MAX_WAIT must be a non-negative integer"
  HEALTH_DB_URL="${HEALTH_DB_URL:-$(read_env_value "$GATEWAY_ENV" CHUDWARE_DB_URL)}"
  [ -n "$HEALTH_DB_URL" ] || refuse "CHUDWARE_DB_URL missing from $GATEWAY_ENV; cannot mint a drain health session"
  GATEWAY_DIR="$ROOT/Webservice/gateway"
  GATEWAY_PY="$GATEWAY_DIR/.venv/bin/python"
  [ -x "$GATEWAY_PY" ] || refuse "gateway venv python missing at $GATEWAY_PY; cannot mint a drain health session"
  # The session must outlive the whole drain wait, or a long poll would start
  # failing on an expired cookie and look like an unreachable gateway.
  if ! HEALTH_SID="$(cd "$GATEWAY_DIR" && CHUDWARE_DB_URL="$HEALTH_DB_URL" \
    "$GATEWAY_PY" -m authn ops-session --email "$HEALTH_EMAIL" --ttl-seconds "$((MAXW + 60))")"; then
    refuse "could not mint an ops session for $HEALTH_EMAIL; refusing unauthenticated drain"
  fi
  [ -n "$HEALTH_SID" ] || refuse "ops session for $HEALTH_EMAIL was empty; refusing unauthenticated drain"
  waited=0
  while :; do
    if ! active="$(probe_engine_bridges)"; then
      refuse "authenticated $HEALTH_URL/healthz unreachable or missing engine_bridges; refusing restart"
    fi
    if [ "$active" = 0 ]; then
      echo "[webd] authenticated drain reports zero active engine bridges"
      break
    fi
    if [ "$waited" -ge "$MAXW" ]; then
      refuse "$active engine bridge(s) still active after ${MAXW}s; refusing to drop users"
    fi
    echo "[webd] $active engine bridge(s) active — deferring promotion/restart (${waited}s/${MAXW}s)"
    sleep 5
    waited=$((waited + 5))
  done
fi
# Install service definitions before moving `current`, so daemon-reload failure
# cannot leave freshly promoted bits paired with stale service policy.
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
install_unit() {
  local src="$1" dst="$2" tmp
  tmp="$dst.staging.$$"
  cp "$src" "$tmp"
  mv -f "$tmp" "$dst"
}
install_unit "$STAGE_ROOT/chudware-hostd.service" "$UNIT_DIR/chudware-hostd.service"
install_unit "$ROOT/Webservice/deploy/chudware-gateway.service" "$UNIT_DIR/chudware-gateway.service"
install_unit "$ROOT/Webservice/deploy/chudware-gateway-staging.service" "$UNIT_DIR/chudware-gateway-staging.service"
systemctl --user daemon-reload || fatal "systemd user daemon-reload failed"
systemctl --user enable chudware-hostd.service >/dev/null || fatal "could not enable chudware-hostd.service"
echo "[webd] systemd units installed"

LEGACY_BIN=""
LEGACY_APP=""
if [ "$NEEDS_PROMOTION" = 1 ]; then
  [ ! -e "$DST/current" ] || [ -L "$DST/current" ] || fatal "$DST/current exists and is not a symlink"
  if [ -L "$BIN" ] && [ "$(readlink "$BIN")" != "current/bin" ]; then
    fatal "$BIN points somewhere unexpected: $(readlink "$BIN")"
  fi
  if [ -L "$APP" ] && [ "$(readlink "$APP")" != "current/app" ]; then
    fatal "$APP points somewhere unexpected: $(readlink "$APP")"
  fi
  RELEASE_DIR="$RELEASES/$RELEASE_ID"
  # Parity gate before anything live moves. The browser bundle bakes assets/ in at LINK
  # time (the --preload-file flags in host/CMakeLists.txt), so $STAGE_APP can be a
  # perfectly valid, freshly linked WASM bundle carrying stale assets — the hashes above
  # pin the release to itself, never to its sources. That shipped once: 69690e57
  # (2026-08-17) re-rendered every ribbon tool icon, webd kept serving the bundle linked
  # 2026-08-07, and ten days passed before anyone hand-scanned Chudware.data for PNG bytes
  # and found it. Refuse the promotion instead of moving `current` onto a stale release.
  [ -f "$ROOT/scripts/check_web_parity.py" ] \
    || fatal "$ROOT/scripts/check_web_parity.py missing; cannot prove the staged bundle matches its sources"
  "$PYTHON" "$ROOT/scripts/check_web_parity.py" --bundle "$STAGE_APP" \
    || fatal "staged web bundle does not match $ROOT (see [web-parity] output above); refusing to promote a stale release"
  mv "$STAGE_ROOT" "$RELEASE_DIR"
  STAGE_ROOT=""

  ln -s "releases/$RELEASE_ID" "$DST/.current.staging.$$"
  mv -Tf "$DST/.current.staging.$$" "$DST/current"

  if [ ! -L "$BIN" ]; then
    if [ -e "$BIN" ]; then
      LEGACY_BIN="$DST/.legacy-bin-$RELEASE_ID"
      mv "$BIN" "$LEGACY_BIN"
    fi
    ln -s current/bin "$DST/.bin.staging.$$"
    mv -Tf "$DST/.bin.staging.$$" "$BIN"
  fi
  if [ ! -L "$APP" ]; then
    if [ -e "$APP" ]; then
      LEGACY_APP="$DST/.legacy-app-$RELEASE_ID"
      mv "$APP" "$LEGACY_APP"
    fi
    ln -s current/app "$DST/.app.staging.$$"
    mv -Tf "$DST/.app.staging.$$" "$APP"
  fi
  echo "[webd] promoted joint release $RELEASE_ID"
else
  echo "[webd] joint release unchanged (sha ${JOINT_SHA:0:12}); no promotion"
fi

# Stable gateway/desktop discovery files are written atomically after promotion.
RUNDIR="${XDG_RUNTIME_DIR:-/tmp/chudware-$(id -u)}"
SOCK="$RUNDIR/chudware/hostd-$KEY.sock"
cat > "$DST/.webd.env.staging.$$" <<EOF
# written by install_webd.sh $(date -Is) — read by the gateway (config.py)
CHUDWARE_WEBD_SOCKET=$SOCK
CHUDWARE_WORKSPACE_ROOT=$WORKSPACE_ROOT
CHUDWARE_CHUDCODE_BIN=$BIN/chud
EOF
mv -f "$DST/.webd.env.staging.$$" "$DST/webd.env"
cat > "$DST/.chudware-shared.sh.staging.$$" <<EOF
#!/usr/bin/env bash
export CHUDWARE_INSTANCE_KEY=$KEY
export CHUDWARE_HOSTD_BIN="$BIN/chudware-hostd"
export CHUDWARE_CHUDCODE_BIN="$BIN/chud"
export CHUDWARE_ENGINE_DAEMON=1
exec "\${CHUDWARE_BIN:-$ROOT/host/build-linux/bin/chudware}" "\$@"
EOF
chmod +x "$DST/.chudware-shared.sh.staging.$$"
mv -f "$DST/.chudware-shared.sh.staging.$$" "$DST/chudware-shared.sh"

if [ "$RESTART" = 1 ]; then
  if [ "$NEEDS_RESTART" = 1 ]; then
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
    [ "$ready" = 1 ] || fatal "chudware-hostd failed readiness after restart"
    printf '%s\n' "$JOINT_SHA" > "$DST/.webd-running-joint.staging.$$"
    mv -f "$DST/.webd-running-joint.staging.$$" "$DST/.webd-running-joint"
    echo "[webd] restarted and ready (joint sha ${JOINT_SHA:0:12})"
  else
    echo "[webd] running release already matches joint sha ${JOINT_SHA:0:12}; no hostd restart"
  fi
else
  echo "[webd] release installed; start it with: systemctl --user start chudware-hostd.service"
fi

# Old trees are removed only after the requested service operation has completed
# successfully, preserving a rollback artifact on restart/readiness failure.
# LEGACY_BIN/LEGACY_APP are pre-`releases/`-layout leftovers, not release history.
[ -z "$LEGACY_BIN" ] || rm -rf "$LEGACY_BIN"
[ -z "$LEGACY_APP" ] || rm -rf "$LEGACY_APP"

# Retention, not deletion. This block used to `rm -rf` whatever `current` pointed at
# the instant a promotion succeeded, which left production with literally nothing to
# roll back to; now the newest $KEEP_RELEASES trees survive (the release promoted by
# this run counts as the first of them) and rollback.sh picks a target out of them.
#
# Ordering is by release id, which begins with the UTC promotion stamp and therefore
# sorts chronologically as plain text — mtime is not trusted for those, because a
# `cp -a`, a restore, or a parity re-read can touch a directory long after it was
# built. A directory whose name does not carry that stamp (hand-copied, or predating
# the timestamped ids) is slotted in by its mtime rendered into the same shape, so
# the two kinds interleave chronologically instead of one always sorting first.
CURRENT_REAL="$(readlink -f "$DST/current" 2>/dev/null || true)"
kept=0
while IFS=$'\t' read -r _ name; do
  dir="$RELEASES/$name"
  # The `-ef` clause is belt and braces: the live tree is never reaped even if the
  # retention count is wrong or an interrupted promotion left `current` pointing at
  # something that does not sort into the newest $KEEP_RELEASES.
  if [ "$kept" -lt "$KEEP_RELEASES" ] \
     || { [ -n "$CURRENT_REAL" ] && [ "$dir" -ef "$CURRENT_REAL" ]; }; then
    kept=$((kept + 1))
    continue
  fi
  rm -rf "$dir"
  echo "[webd] reaped superseded release $name"
done < <(
  for dir in "$RELEASES"/*; do
    [ -d "$dir" ] || continue
    name="${dir##*/}"
    if [[ "$name" =~ ^[0-9]{8}T[0-9]{6}Z?- ]]; then
      printf '%s\t%s\n' "$name" "$name"
    else
      # An unstattable directory sorts as epoch 0: unknown age is treated as oldest
      # rather than crashing the collector or letting the tree accumulate forever.
      printf '%s\t%s\n' \
        "$(date -u -d "@$(stat -c %Y "$dir" 2>/dev/null || echo 0)" +%Y%m%dT%H%M%SZ)" "$name"
    fi
  done | sort -r
)
echo "[webd] deployment complete (joint sha ${JOINT_SHA:0:12}; $kept release(s) retained)"
