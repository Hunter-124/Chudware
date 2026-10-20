#!/usr/bin/env bash
# ===========================================================================
#  install_webd.sh — promote one validated engine + WASM release to webd.
# ---------------------------------------------------------------------------
# The browser gateway launches workspace cells from the retained payload. The
# optional shared daemon serves local/native clients only; this script never
# enables, stops or restarts it. --restart requests a drained cell-payload cutover.
# ===========================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENVIRONMENT="${CHUDWARE_DEPLOY_ENV:-prod}"
case "$ENVIRONMENT" in
  prod)
    [[ "$(id -un)" != chudware-staging ]] || { echo "REFUSED: staging identity cannot deploy production" >&2; exit 4; }
    DST="${CHUDWARE_WEB_HOME:-$HOME/chudware-web}"
    KEY="chudwareweb"
    HOSTD_UNIT="chudware-hostd.service"
    GATEWAY_UNIT="chudware-gateway.service"
    DATA_DEFAULT="$HOME/chudware-web-data"
    GATEWAY_PORT=8300
    WORKSPACE_DEFAULT="$HOME/Documents/ME489/Crimson_Crankers_Program/01_Design_Package/CAD_Build"
    SERVICE_USER="!chudware-staging"
    ;;
  staging)
    [[ "$(id -un)" = chudware-staging && "$HOME" = /var/lib/chudware-staging ]] \
      || { echo "REFUSED: staging requires the dedicated chudware-staging account and home" >&2; exit 4; }
    DST="${CHUDWARE_WEB_HOME:-$HOME/chudware-web-staging}"
    KEY="chudwareweb-staging"
    HOSTD_UNIT="chudware-hostd-staging.service"
    GATEWAY_UNIT="chudware-gateway-staging.service"
    DATA_DEFAULT="$HOME/chudware-web-staging"
    GATEWAY_PORT=8310
    WORKSPACE_DEFAULT="$DST/workspace"
    SERVICE_USER="chudware-staging"
    ;;
  *) echo "REFUSED: CHUDWARE_DEPLOY_ENV must be prod or staging" >&2; exit 4 ;;
esac
export CHUDWARE_DEPLOY_ENV="$ENVIRONMENT"
RELEASES="$DST/releases"
BIN="$DST/bin"
APP="$DST/app"
WORKSPACE_ROOT="${CHUDWARE_WORKSPACE_ROOT:-$WORKSPACE_DEFAULT}"
# Every live payload/policy install uses the same maintenance cutover.
MUTATED=0
FIRST_CUTOVER_ACK=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --restart) shift ;;
    --first-cutover-ack)
      [[ $# -ge 2 ]] || { echo "missing acknowledgment path" >&2; exit 2; }
      FIRST_CUTOVER_ACK="$2"; shift 2 ;;
    *) echo "usage: $0 [--restart] [--first-cutover-ack PATH]" >&2; exit 2 ;;
  esac
done

fatal() {
  echo "[webd] ERROR: $*" >&2
  if [[ "$MUTATED" = 0 ]]; then exit 4; else exit 1; fi
}

# Match update.sh's pre-mutation refusal contract. Call only before service
# definitions, the live release, or running services have been changed.
refuse() {
  echo "[webd] REFUSED: $*" >&2
  if [[ "$MUTATED" = 0 ]]; then
    echo "[webd] live release and running services were not changed" >&2
    exit 4
  fi
  exit 1
}

preflight_exit() {
  local rc=$?
  trap - EXIT
  if [[ "$rc" != 0 ]]; then
    if [[ "${DRAIN_ACQUIRED:-0}" = 1 ]]; then deployment_drain_abort || true; fi
    if [[ "$MUTATED" = 0 ]]; then rc=4; else rc=1; fi
  fi
  exit "$rc"
}
trap preflight_exit EXIT

# Retention depth for `releases/`, counting the release this run promotes. Before
# this existed the previous release was deleted the instant a promotion succeeded,
# which meant a bad build reaching production had nothing behind it to restore.
# Validated here rather than at GC time: the GC runs after a ~20 minute build and
# a service restart, and discovering a typo'd retention count there is useless.
KEEP_RELEASES="${CHUDWARE_WEBD_KEEP_RELEASES:-3}"
[[ "$KEEP_RELEASES" =~ ^[0-9]+$ ]] && [ "$KEEP_RELEASES" -ge 2 ] \
  || fatal "CHUDWARE_WEBD_KEEP_RELEASES must retain the current and at least one prior payload (integer >= 2)"

PYTHON="$ROOT/Webservice/gateway/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3 || true)"
fi
[ -n "$PYTHON" ] && [ -x "$PYTHON" ] || fatal "python3 is required for manifest and health validation"
command -v sha256sum >/dev/null 2>&1 || fatal "sha256sum is required"
command -v curl >/dev/null 2>&1 || fatal "curl is required"
command -v systemctl >/dev/null 2>&1 || fatal "systemctl is required"

DATA_ROOT="${CHUDWARE_WEB_DATA:-$DATA_DEFAULT}"
GATEWAY_ENV="${CHUDWARE_WEB_GATEWAY_ENV:-$DATA_ROOT/gateway.env}"
GATEWAY_PY="${CHUDWARE_GATEWAY_PYTHON:-$ROOT/Webservice/gateway/.venv/bin/python}"
source "$ROOT/Webservice/deploy/deployment_drain.sh" || refuse "private maintenance channel is invalid"
[[ -x "$GATEWAY_PY" ]] || refuse "gateway runtime is unavailable"
"$GATEWAY_PY" "$ROOT/Webservice/deploy/gateway_preflight.py" validate \
  --env-file "$GATEWAY_ENV" --environment "$ENVIRONMENT" \
  || refuse "protected gateway configuration is not release-ready"
[[ "${CHUDWARE_BASE_PYTHON:-}" = /usr/bin/python3 ]] \
  || refuse "authenticated cell worker base runtime is required"
env -i PATH=/usr/bin:/bin /usr/bin/python3 -I -S \
  "$ROOT/Webservice/deploy/deployment_release.py" runtime-preflight \
  --repo "$ROOT" --wheelhouse "${CHUDWARE_WHEELHOUSE:-$ROOT/wheelhouse}" \
  || refuse "authenticated offline cell worker runtime inputs are unavailable"
TARGET_COMMIT="$(git -C "$ROOT" rev-parse HEAD)"
AUTHORITY_ARGS=(promotion-preflight --web-home "$DST" --environment "$ENVIRONMENT" \
  --target-commit "$TARGET_COMMIT")
[[ -z "$FIRST_CUTOVER_ACK" ]] || AUTHORITY_ARGS+=(--first-cutover-ack "$FIRST_CUTOVER_ACK")
env -i PATH=/usr/bin:/bin /usr/bin/python3 -I -S \
  "$ROOT/Webservice/deploy/deployment_release.py" "${AUTHORITY_ARGS[@]}" \
  || refuse "independent prior-payload authority or explicit first-cutover acknowledgment is required"
deployment_drain_acquire \
  || refuse "operator maintenance could not prove all work idle and every cell inactive; no cutover"
MUTATED=1

mkdir -p "$DST" "$RELEASES"
if [ ! -d "$WORKSPACE_ROOT" ]; then
  mkdir -p "$WORKSPACE_ROOT"
  echo "[webd] created workspace root $WORKSPACE_ROOT"
fi

# Legacy local/native LibreDWG provisioning is not a browser-cell readiness proof:
# the managed HOME copy is outside the retained cell payload. Cell converter
# availability requires independent runtime-image qualification. This step does
# not start, stop or restart the optional local/native daemon.
UNIT_XDG_DATA_HOME="$(systemctl --user show-environment 2>/dev/null |
  sed -n 's/^XDG_DATA_HOME=//p')"
if ! LIBREDWG_LOG="$(env -u APPDATA XDG_DATA_HOME="$UNIT_XDG_DATA_HOME" \
    "$PYTHON" "$ROOT/scripts/provision_libredwg.py" 2>&1)"; then
  printf '%s\n' "$LIBREDWG_LOG" >&2
  refuse "legacy local/native LibreDWG provisioning failed; cell converter availability remains independently unqualified"
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
  local rc=$?
  trap - EXIT
  if [ -n "${STAGE_ROOT:-}" ] && [ -d "$STAGE_ROOT" ]; then
    rm -rf "$STAGE_ROOT"
  fi
  if [[ "$rc" != 0 ]]; then
    if [[ "${DRAIN_ACQUIRED:-0}" = 1 ]]; then deployment_drain_abort || true; fi
    if [[ "$MUTATED" = 0 ]]; then rc=4; else rc=1; fi
  fi
  exit "$rc"
}
trap cleanup EXIT

STAGE_BIN="$STAGE_ROOT/bin"
STAGE_APP="$STAGE_ROOT/app"
STAGE_PROMPTS="$STAGE_ROOT/agent/prompts"
STAGE_CELL="$STAGE_ROOT/cell"
mkdir -p "$STAGE_BIN" "$STAGE_APP" "$STAGE_PROMPTS" "$STAGE_CELL/bin" "$STAGE_CELL/units"

# Cells never borrow the gateway venv or repository at runtime. Authenticate the
# complete offline wheel closure before creating the release-private worker venv.
CELL_BASE="${CHUDWARE_BASE_PYTHON:-}"
[[ "$CELL_BASE" = /usr/bin/python3 ]] \
  || fatal "cell worker runtime requires independently authenticated CHUDWARE_BASE_PYTHON=/usr/bin/python3"
if ! env -i HOME="$HOME" PATH=/usr/bin:/bin "$CELL_BASE" -I -S - \
    "$ROOT" "$STAGE_CELL" "${CHUDWARE_WHEELHOUSE:-$ROOT/wheelhouse}" <<'PY'
import os
from pathlib import Path
import shutil
import sys
root, stage, wheelhouse = map(Path, sys.argv[1:])
sys.path.insert(0, str(root))
from services.installer.provision import ensure_runtime
temporary = stage / ".worker-runtime"
result = ensure_runtime(base_python="/usr/bin/python3", wheelhouse=wheelhouse, venv_dir=temporary)
if result.get("status") != "ready":
    raise RuntimeError("authenticated cell worker runtime provisioning failed: " + str(result))
# Retained runtime trees forbid links. Materialize the proven venv (including
# its base interpreter and lib64 alias) without weakening that policy.
shutil.copytree(temporary, stage / "venv", symlinks=False)
shutil.rmtree(temporary)
for executable in (stage / "venv/bin").iterdir():
    if executable.is_file():
        data = executable.read_bytes()
        if data.startswith(b"#!") and b".worker-runtime/" in data.split(b"\n", 1)[0]:
            _, rest = data.split(b"\n", 1)
            executable.write_bytes(b"#!/opt/chudware/venv/bin/python\n" + rest)
ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "test_*", "tests", "build")
shutil.copytree(root / "services", stage / "services", symlinks=False, ignore=ignore)
if (root / "engine").is_dir():
    shutil.copytree(root / "engine", stage / "engine", symlinks=False, ignore=ignore)
shutil.copy2(root / "vendor/python/RUNTIME_WHEELS.lock", stage / "RUNTIME_WHEELS.lock")
for source in (root / "Webservice/deploy/cell").iterdir():
    if source.is_file() and source.name != "__pycache__":
        shutil.copy2(source, stage / "bin" / source.name)
PY
then
  fatal "cell worker runtime unavailable: reviewed vendor/python/RUNTIME_WHEELS.lock and complete authenticated offline wheelhouse (including chudcode-rpc==0.3.1) are required; no ambient/index fallback"
fi

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
# The cell mounts only its complete runtime subtree, never the release's app or
# host-only unit definitions. Materialize trusted engine/artifact siblings.
cp -aL "$STAGE_BIN/." "$STAGE_CELL/bin/"
mkdir -p "$STAGE_CELL/agent/prompts"
cp -aL "$STAGE_PROMPTS/." "$STAGE_CELL/agent/prompts/"
for identity in CHUDCODE_ARTIFACT.lock CHUDCODE-NOTICE CHUDCODE-THIRD-PARTY-NOTICES.txt; do
  cp "$STAGE_ROOT/$identity" "$STAGE_CELL/$identity"
done

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
  -e "s|@INSTANCE_KEY@|$KEY|g" -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
  "$ROOT/Webservice/deploy/chudware-hostd.service" > "$STAGE_ROOT/chudware-hostd.service"
if [[ "$ENVIRONMENT" = staging ]]; then
  mkdir -p "$HOME/.config" "$HOME/.cache" "$HOME/.local/share"
  cat >> "$STAGE_ROOT/chudware-hostd.service" <<EOF

[Service]
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=$DST $HOME/.config $HOME/.cache $HOME/.local/share
UMask=0077
EOF
fi
cp "$ROOT/Webservice/deploy/chudware-gateway.service" "$STAGE_ROOT/chudware-gateway.service"
cp "$ROOT/Webservice/deploy/chudware-gateway-staging.service" "$STAGE_ROOT/chudware-gateway-staging.service"
for unit in chudware-cell@.socket chudware-cell@.service \
    chudware-egress-broker@.socket chudware-egress-broker@.service; do
  sed -e "s|@WEB_HOME@|$DST|g" "$ROOT/Webservice/deploy/$unit" > "$STAGE_CELL/units/$unit"
done

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
GATEWAY_UNIT_SHA="$(sha256sum "$STAGE_ROOT/chudware-gateway.service" | cut -d' ' -f1)"
STAGING_GATEWAY_UNIT_SHA="$(sha256sum "$STAGE_ROOT/chudware-gateway-staging.service" | cut -d' ' -f1)"
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
CELL_RUNTIME_SHA="$(env -i PATH=/usr/bin:/bin /usr/bin/python3 -I -S - "$STAGE_CELL" <<'PY'
from hashlib import sha256
from pathlib import Path
import sys
root = Path(sys.argv[1])
h = sha256()
for path in sorted(p for p in root.rglob("*") if p.is_file()):
    h.update(path.relative_to(root).as_posix().encode())
    h.update(b"\0")
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
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
  "$STAGING_GATEWAY_UNIT_SHA" "$PROMPTS_SHA" "$CELL_RUNTIME_SHA" \
  | sha256sum | cut -d' ' -f1)"
# One clock read feeds both the release id and the manifest timestamp, so the two
# can never straddle a second boundary and disagree. UTC rather than local time:
# release retention and rollback order trees by their lexicographic directory
# name, and a local-time stamp runs backwards for an hour on every DST fall-back,
# which would let the collector reap the newest release instead of the oldest.
BUILT_AT_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
RELEASE_ID="${BUILT_AT_UTC//[:-]/}-${JOINT_SHA:0:12}-$$"
RELEASE_TREE_SHA="$(env -i PATH=/usr/bin:/bin /usr/bin/python3 -I -S \
  "$ROOT/Webservice/deploy/deployment_release.py" tree-digest "$STAGE_ROOT")"
cat > "$STAGE_ROOT/deploy-manifest" <<EOF
schema=7
release_id=$RELEASE_ID
joint_sha256=$JOINT_SHA
release_tree_sha256=$RELEASE_TREE_SHA
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
cell_runtime_sha256=$CELL_RUNTIME_SHA
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
# The legacy shared daemon is not a browser gateway consumer. Never bounce an
# active user's daemon during a cell-payload update.
NEEDS_PROMOTION=0
[ "$CURRENT_JOINT" = "$JOINT_SHA" ] || NEEDS_PROMOTION=1

# The acquired maintenance lease, not a public snapshot, owns this cutover.
deployment_drain_call check || fatal "maintenance lost authoritative inactive-cell proof"
# Recheck the prior target immediately before any live service policy changes.
env -i PATH=/usr/bin:/bin /usr/bin/python3 -I -S \
  "$ROOT/Webservice/deploy/deployment_release.py" "${AUTHORITY_ARGS[@]}" \
  || refuse "prior-payload authorization changed during staging"
MUTATED=1
# Install cell/gateway policy before moving `current`.
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
install_unit() {
  local src="$1" dst="$2" tmp
  tmp="$dst.staging.$$"
  cp "$src" "$tmp"
  mv -f "$tmp" "$dst"
}
install_unit "$ROOT/Webservice/deploy/$GATEWAY_UNIT" "$UNIT_DIR/$GATEWAY_UNIT"
for unit in chudware-cell@.socket chudware-cell@.service \
    chudware-egress-broker@.socket chudware-egress-broker@.service; do
  install_unit "$STAGE_CELL/units/$unit" "$UNIT_DIR/$unit"
done
systemctl --user daemon-reload || fatal "systemd user daemon-reload failed"
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

# Workspace storage discovery only; browser execution is isolated in cells.
cat > "$DST/.webd.env.staging.$$" <<EOF
CHUDWARE_WORKSPACE_ROOT=$WORKSPACE_ROOT
EOF
mv -f "$DST/.webd.env.staging.$$" "$DST/webd.env"
printf '%s\n' "$JOINT_SHA" > "$DST/.cell-payload-installed-joint.staging.$$"
mv -f "$DST/.cell-payload-installed-joint.staging.$$" "$DST/.cell-payload-installed-joint"

echo "[webd] retained cell payload installed; existing workspace cells keep their private runtime"
echo "[webd] shared local/native daemon was not enabled or restarted"

if [[ "$DRAIN_BORROWED" = 0 ]]; then
  systemctl --user restart "$GATEWAY_UNIT" || fatal "gateway restart failed; maintenance retained"
  systemctl --user is-active --quiet "$GATEWAY_UNIT" || fatal "gateway is not active; maintenance retained"
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
deployment_drain_complete || fatal "qualified cutover health/release failed; maintenance retained"
echo "[webd] deployment complete (joint sha ${JOINT_SHA:0:12}; $kept release(s) retained)"
