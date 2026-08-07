#!/usr/bin/env bash
# ===========================================================================
#  install_webd.sh — promote one validated engine + WASM release to webd.
# ---------------------------------------------------------------------------
#  The engine and browser bundle are built/staged as one immutable release,
#  validated before the live `current` symlink moves, and restarted only after
#  authenticated gateway telemetry proves no engine bridges are active.
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

# Prompts are part of the release because hostd resolves them through `current`.
PROMPTS_SRC="$ROOT/agent/prompts"
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

# Provider/auth RPCs require the vendored Bun workspace. Missing Bun, lockfile,
# bridge source, install failure, or an absent node_modules tree is deployment-fatal.
VENDOR_OMP="$ROOT/vendor/omp"
[ -f "$VENDOR_OMP/package.json" ] || fatal "$VENDOR_OMP/package.json missing"
[ -f "$VENDOR_OMP/bun.lock" ] || fatal "$VENDOR_OMP/bun.lock missing"
[ -f "$VENDOR_OMP/bridge/src/bridge.ts" ] || fatal "$VENDOR_OMP/bridge/src/bridge.ts missing"
BUN="${CHUDWARE_BUN:-$HOME/.bun/bin/bun}"
if [ ! -x "$BUN" ]; then
  BUN="$(command -v bun || true)"
fi
[ -n "$BUN" ] && [ -x "$BUN" ] || fatal "bun not found; provider/auth RPC dependencies cannot be provisioned"
if ! (cd "$VENDOR_OMP" && "$BUN" install --frozen-lockfile); then
  fatal "bun install --frozen-lockfile failed in $VENDOR_OMP"
fi
[ -d "$VENDOR_OMP/node_modules" ] || fatal "$VENDOR_OMP/node_modules missing after bun install"
echo "[webd] provider/auth dependencies provisioned"

# Build WASM into the private release stage, never directly over the live app.
EMSDK_DIR="${EMSDK:-$HOME/.emsdk}"
[ -f "$EMSDK_DIR/emsdk_env.sh" ] || fatal "emsdk not found at $EMSDK_DIR; refusing a hostd-only release"
WASM_LOG="${CHUDWARE_WEBD_WASM_LOG:-/tmp/chudware-webd-sync-wasm.log}"
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
WASM_SHA="$(sha256sum "$STAGE_APP/Chudware.wasm" | cut -d' ' -f1)"
JS_SHA="$(sha256sum "$STAGE_APP/Chudware.js" | cut -d' ' -f1)"
PROVIDER_SHA="$("$PYTHON" - "$VENDOR_OMP" <<'PY'
from hashlib import sha256
from pathlib import Path
import sys
root = Path(sys.argv[1])
inputs = [
    root / "package.json",
    root / "bun.lock",
    root / "bunfig.toml",
    root / "OMP_VENDOR.lock",
    root / "bridge",
    root / "packages",
    root / "patches",
]
files = []
for item in inputs:
    if item.is_file():
        files.append(item)
    elif item.is_dir():
        files.extend(
            path for path in item.rglob("*")
            if path.is_file()
            and not {"node_modules", "dist", "build", ".cache", ".turbo", ".git"}.intersection(path.parts)
        )
h = sha256()
for path in sorted(files):
    h.update(path.relative_to(root).as_posix().encode())
    h.update(b"\0")
    h.update(path.read_bytes())
    h.update(b"\0")
print(h.hexdigest())
PY
)"
HOSTD_UNIT_SHA="$(sha256sum "$STAGE_ROOT/chudware-hostd.service" | cut -d' ' -f1)"
GATEWAY_UNIT_SHA="$(sha256sum "$ROOT/Webservice/deploy/chudware-gateway.service" | cut -d' ' -f1)"
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
JOINT_SHA="$(printf '%s\n' "$HOSTD_SHA" "$LIB_SHA" "$WASM_SHA" "$JS_SHA" \
  "$PROVIDER_SHA" "$HOSTD_UNIT_SHA" "$GATEWAY_UNIT_SHA" "$PROMPTS_SHA" \
  | sha256sum | cut -d' ' -f1)"
cat > "$STAGE_ROOT/deploy-manifest" <<EOF
schema=1
joint_sha256=$JOINT_SHA
hostd_sha256=$HOSTD_SHA
libchudware_sha256=$LIB_SHA
wasm_sha256=$WASM_SHA
javascript_sha256=$JS_SHA
provider_runtime_sha256=$PROVIDER_SHA
hostd_unit_sha256=$HOSTD_UNIT_SHA
gateway_unit_sha256=$GATEWAY_UNIT_SHA
prompts_sha256=$PROMPTS_SHA
wasm_token=$WASM_TOKEN
checkout_head=$CHECKOUT_HEAD
source=$SRC
prepared_at=$(date -Is)
EOF
echo "[webd] joint release validated (sha ${JOINT_SHA:0:12})"

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
install_unit "$ROOT/Webservice/deploy/chudware-webd-sync.path" "$UNIT_DIR/chudware-webd-sync.path"
install_unit "$ROOT/Webservice/deploy/chudware-webd-sync.service" "$UNIT_DIR/chudware-webd-sync.service"
systemctl --user daemon-reload || fatal "systemd user daemon-reload failed"
systemctl --user enable chudware-hostd.service >/dev/null || fatal "could not enable chudware-hostd.service"
systemctl --user enable --now chudware-webd-sync.path >/dev/null || fatal "could not enable/start chudware-webd-sync.path"
echo "[webd] systemd units installed"

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
  fatal "hostd is running; refusing to promote a new joint release without --restart"
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

probe_engine_bridges() {
  local body
  if ! body="$(curl -fsS --max-time 3 --user "$HEALTH_EMAIL:$HEALTH_SECRET" "$HEALTH_URL/healthz")"; then
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
# hostd/WASM change. Missing secret/account/telemetry is fail-closed.
REQUIRE_IDLE="${CHUDWARE_WEBD_REQUIRE_IDLE:-0}"
case "$REQUIRE_IDLE" in 0|1) ;; *) fatal "CHUDWARE_WEBD_REQUIRE_IDLE must be 0 or 1" ;; esac
if [ "$RUNNING" = 1 ] && { [ "$NEEDS_RESTART" = 1 ] || [ "$REQUIRE_IDLE" = 1 ]; }; then
  DATA_ROOT="${CHUDWARE_WEB_DATA:-$HOME/chudware-web-data}"
  GATEWAY_ENV="${CHUDWARE_WEB_GATEWAY_ENV:-$DATA_ROOT/gateway.env}"
  [ -r "$GATEWAY_ENV" ] || fatal "gateway env unreadable at $GATEWAY_ENV; cannot authenticate drain health"
  FILE_DATA_ROOT="$(read_env_value "$GATEWAY_ENV" CHUDWARE_WEB_DATA)"
  [ -z "$FILE_DATA_ROOT" ] || DATA_ROOT="$FILE_DATA_ROOT"
  DATA_ROOT="${DATA_ROOT/#\~/$HOME}"
  HEALTH_SECRET="$(read_env_value "$GATEWAY_ENV" CHUDWARE_WEB_SSO_SHARED_SECRET)"
  [ -n "$HEALTH_SECRET" ] || fatal "CHUDWARE_WEB_SSO_SHARED_SECRET missing; refusing unauthenticated drain"
  HEALTH_EMAIL="${CHUDWARE_WEBD_HEALTH_EMAIL:-$(read_env_value "$GATEWAY_ENV" CHUDWARE_WEBD_HEALTH_EMAIL)}"
  if [ -z "$HEALTH_EMAIL" ]; then
    ACCOUNTS_DB="$DATA_ROOT/accounts.db"
    [ -r "$ACCOUNTS_DB" ] || fatal "accounts database unreadable at $ACCOUNTS_DB; cannot select a loopback identity"
    if ! HEALTH_EMAIL="$("$PYTHON" - "$ACCOUNTS_DB" <<'PY'
import sqlite3
import sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
row = con.execute(
    "SELECT email FROM accounts WHERE status='active' "
    "ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, email LIMIT 1"
).fetchone()
print(row[0] if row else "", end="")
PY
)"; then
      fatal "could not read an active loopback identity from $ACCOUNTS_DB"
    fi
  fi
  [ -n "$HEALTH_EMAIL" ] || fatal "no active account available for authenticated drain health"
  HEALTH_PORT="${CHUDWARE_WEB_GATEWAY_PORT:-8300}"
  [[ "$HEALTH_PORT" =~ ^[0-9]+$ ]] && [ "$HEALTH_PORT" -ge 1 ] && [ "$HEALTH_PORT" -le 65535 ] \
    || fatal "invalid loopback gateway port: $HEALTH_PORT"
  HEALTH_URL="http://127.0.0.1:$HEALTH_PORT"
  MAXW="${CHUDWARE_WEBD_RESTART_MAX_WAIT:-600}"
  [[ "$MAXW" =~ ^[0-9]+$ ]] || fatal "CHUDWARE_WEBD_RESTART_MAX_WAIT must be a non-negative integer"
  waited=0
  while :; do
    if ! active="$(probe_engine_bridges)"; then
      fatal "authenticated $HEALTH_URL/healthz unreachable or missing engine_bridges; refusing restart"
    fi
    if [ "$active" = 0 ]; then
      echo "[webd] authenticated drain reports zero active engine bridges"
      break
    fi
    if [ "$waited" -ge "$MAXW" ]; then
      fatal "$active engine bridge(s) still active after ${MAXW}s; refusing to drop users"
    fi
    echo "[webd] $active engine bridge(s) active — deferring promotion/restart (${waited}s/${MAXW}s)"
    sleep 5
    waited=$((waited + 5))
  done
fi

LEGACY_BIN=""
LEGACY_APP=""
OLD_CURRENT_REAL=""
RELEASE_DIR=""
if [ "$NEEDS_PROMOTION" = 1 ]; then
  [ ! -e "$DST/current" ] || [ -L "$DST/current" ] || fatal "$DST/current exists and is not a symlink"
  if [ -L "$BIN" ] && [ "$(readlink "$BIN")" != "current/bin" ]; then
    fatal "$BIN points somewhere unexpected: $(readlink "$BIN")"
  fi
  if [ -L "$APP" ] && [ "$(readlink "$APP")" != "current/app" ]; then
    fatal "$APP points somewhere unexpected: $(readlink "$APP")"
  fi
  OLD_CURRENT_REAL="$(readlink -f "$DST/current" 2>/dev/null || true)"
  RELEASE_ID="$(date +%Y%m%dT%H%M%S)-${JOINT_SHA:0:12}-$$"
  RELEASE_DIR="$RELEASES/$RELEASE_ID"
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
EOF
mv -f "$DST/.webd.env.staging.$$" "$DST/webd.env"
cat > "$DST/.chudware-shared.sh.staging.$$" <<EOF
#!/usr/bin/env bash
export CHUDWARE_INSTANCE_KEY=$KEY
export CHUDWARE_HOSTD_BIN="$BIN/chudware-hostd"
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

# Old trees/releases are removed only after the requested service operation has
# completed successfully, preserving a rollback artifact on restart/readiness failure.
[ -z "$LEGACY_BIN" ] || rm -rf "$LEGACY_BIN"
[ -z "$LEGACY_APP" ] || rm -rf "$LEGACY_APP"
if [ -n "$OLD_CURRENT_REAL" ] && [ "$OLD_CURRENT_REAL" != "$RELEASE_DIR" ]; then
  case "$OLD_CURRENT_REAL" in
    "$RELEASES"/*) rm -rf "$OLD_CURRENT_REAL" ;;
  esac
fi
echo "[webd] deployment complete (joint sha ${JOINT_SHA:0:12})"
