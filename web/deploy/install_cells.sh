#!/usr/bin/env bash
# ===========================================================================
#  install_cells.sh — install the self-hosted web gateway and its engine cells
# ---------------------------------------------------------------------------
# Stages a cell payload into $CHUDWARE_WEB_HOME/releases/<id>/cell and points
# $CHUDWARE_WEB_HOME/current at it. The payload holds hostd and the engine
# library, the Python worker venv and services, the agent prompts, the cell
# method policy, the verified Chudcode runtime and the cell launchers. The
# script also creates the gateway venv, writes a default cell runtime
# configuration when none exists, and installs the systemd user units. It
# starts nothing. Cells additionally need root-provisioned disk quotas; see
# web/README.md.
#
# Run from any directory after ./build.sh daemon:
#   web/deploy/install_cells.sh
#
# Environment:
#   CHUDWARE_WEB_HOME        install root (default ~/.local/share/chudware-web)
#   CHUDWARE_WORKSPACE_ROOT  operator workspace (default $CHUDWARE_WEB_HOME/workspace)
#   CHUDWARE_ENGINE_DIR      engine build output (default core/build-occt-linux/bin)
#   CHUDWARE_CHUD_BIN        local Chudcode binary to verify and stage. When unset,
#                            the pinned public release is downloaded and verified.
# ===========================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WEB_HOME="${CHUDWARE_WEB_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/chudware-web}"
WORKSPACE="${CHUDWARE_WORKSPACE_ROOT:-$WEB_HOME/workspace}"
ENGINE="${CHUDWARE_ENGINE_DIR:-$ROOT/core/build-occt-linux/bin}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}"
UNIT_DIR="$CONFIG_DIR/systemd/user"
RUNTIME_CONFIG="$CONFIG_DIR/chudware/cell-runtime.json"
PYTHON=/usr/bin/python3

die() { echo "[cells] ERROR: $*" >&2; exit 1; }

[ -x /usr/bin/bwrap ] || die "bubblewrap is required at /usr/bin/bwrap"
[ -x "$PYTHON" ] || die "$PYTHON is required (cells run the system interpreter)"
command -v systemctl >/dev/null 2>&1 || die "systemctl is required"
command -v git >/dev/null 2>&1 || die "git is required to install the pinned worker requirements"
for f in chudware-hostd libchudware.so; do
  [ -f "$ENGINE/$f" ] || die "$ENGINE/$f missing (run ./build.sh daemon)"
done

# Cell launchers refuse any symlinked path component, so record real paths.
mkdir -p "$WEB_HOME/releases" "$WEB_HOME/cells" "$WORKSPACE"
chmod 700 "$WEB_HOME/cells"
WEB_HOME="$(realpath "$WEB_HOME")"
WORKSPACE="$(realpath "$WORKSPACE")"

STAGE="$(mktemp -d "$WEB_HOME/releases/.staging.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
CELL="$STAGE/cell"
mkdir -p "$CELL/bin" "$CELL/agent/prompts" "$CELL/contracts"

# Engine: hostd, the engine library and the optional siblings it loads from
# its own directory (meshing, 3MF, and the learned-mesh advisor runtime).
for f in chudware-hostd libchudware.so libngcore.so libnglib.so lib3mf.so "$ENGINE"/libonnxruntime*; do
  f="$(basename "$f")"
  if [ -e "$ENGINE/$f" ]; then cp -L "$ENGINE/$f" "$CELL/bin/$f"; fi
done
if [ -d "$ENGINE/polymesh-advisor" ]; then cp -rL "$ENGINE/polymesh-advisor" "$CELL/bin/"; fi
LD_LIBRARY_PATH="$CELL/bin" "$CELL/bin/chudware-hostd" --version >/dev/null \
  || die "staged chudware-hostd cannot load its staged engine library"
echo "[cells] staged engine from $ENGINE"

# Chudcode: a pinned binary, verified against vendor/chudcode/CHUDCODE_ARTIFACT.lock.
CHUD="${CHUDWARE_CHUD_BIN:-}"
if [ -z "$CHUD" ]; then
  CHUD="$("$PYTHON" "$ROOT/scripts/pin_chudcode.py" ensure --target linux-x86_64 \
    --cache-dir "$WEB_HOME/chudcode-cache" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["path"])')" \
    || die "could not obtain a verified Chudcode binary"
fi
"$PYTHON" "$ROOT/scripts/pin_chudcode.py" stage --target linux-x86_64 \
  --input "$CHUD" --output "$CELL/bin/chud" >/dev/null || die "Chudcode verification failed"
"$PYTHON" "$ROOT/scripts/pin_chudcode.py" stage-notices --output-dir "$CELL" >/dev/null \
  || die "Chudcode notice verification failed"
cp "$ROOT/vendor/chudcode/CHUDCODE_ARTIFACT.lock" "$CELL/CHUDCODE_ARTIFACT.lock"
echo "[cells] staged verified Chudcode"

# Worker venv. Built beside the payload, then materialized without symlinks
# (the cell rejects links) with script shebangs pointing at /opt/chudware.
BUILD_VENV="$STAGE/.worker-venv"
"$PYTHON" -m venv "$BUILD_VENV"
"$BUILD_VENV/bin/python" -m pip install --quiet --requirement "$ROOT/requirements.txt" \
  || die "worker requirements failed to install"
"$PYTHON" -I -S - "$ROOT" "$BUILD_VENV" "$CELL" <<'PY'
import shutil
import sys
from pathlib import Path

root, built, cell = map(Path, sys.argv[1:])
shutil.copytree(built, cell / "venv", symlinks=False)
for script in (cell / "venv/bin").iterdir():
    if script.is_file():
        data = script.read_bytes()
        first, newline, rest = data.partition(b"\n")
        if first.startswith(b"#!") and str(built).encode() in first:
            script.write_bytes(b"#!/opt/chudware/venv/bin/python" + newline + rest)
ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "test_*", "tests", "build")
shutil.copytree(root / "services", cell / "services", symlinks=False, ignore=ignore)
shutil.copytree(root / "services/agent/prompts", cell / "agent/prompts", dirs_exist_ok=True)
shutil.copy2(root / "contracts/cell_methods.json", cell / "contracts/cell_methods.json")
for source in (root / "web/deploy/cell").iterdir():
    if source.is_file():
        shutil.copy2(source, cell / "bin" / source.name)
PY
rm -rf "$BUILD_VENV"
echo "[cells] staged worker runtime, services, prompts and cell policy"

RELEASE="$(date -u +%Y%m%dT%H%M%SZ)"
mv "$STAGE" "$WEB_HOME/releases/$RELEASE"
trap - EXIT
ln -sfn "releases/$RELEASE" "$WEB_HOME/.current.new"
mv -Tf "$WEB_HOME/.current.new" "$WEB_HOME/current"
echo "[cells] current -> releases/$RELEASE"

# Gateway venv (the unit runs uvicorn from it).
GATEWAY_VENV="$ROOT/web/gateway/.venv"
if [ ! -x "$GATEWAY_VENV/bin/uvicorn" ]; then
  python3 -m venv "$GATEWAY_VENV"
  "$GATEWAY_VENV/bin/python" -m pip install --quiet --requirement "$ROOT/web/gateway/requirements.txt" \
    || die "gateway requirements failed to install"
fi

# Default cell runtime configuration: this home's cells, the operator's
# workspace, the standard quota proof root, and unrestricted public egress.
if [ ! -e "$RUNTIME_CONFIG" ]; then
  mkdir -p "$(dirname "$RUNTIME_CONFIG")"
  chmod 700 "$(dirname "$RUNTIME_CONFIG")"
  (umask 077; "$PYTHON" -c 'import json,sys; print(json.dumps({"schema": "chudware-cell-runtime-v1", "cells_root": sys.argv[1], "allowed_roots": [sys.argv[2]], "quota_proof_root": "/etc/chudware/cell-quota.d"}, indent=2))' \
    "$WEB_HOME/cells" "$WORKSPACE" > "$RUNTIME_CONFIG")
  echo "[cells] wrote $RUNTIME_CONFIG"
fi

mkdir -p "$UNIT_DIR"
for unit in chudware-cell@.socket chudware-cell@.service \
    chudware-egress-broker@.socket chudware-egress-broker@.service chudware-gateway.service; do
  sed -e "s|@WEB_HOME@|$WEB_HOME|g" -e "s|@WORKSPACE@|$WORKSPACE|g" -e "s|@REPO@|$ROOT|g" \
    "$ROOT/web/deploy/$unit" > "$UNIT_DIR/$unit"
done
systemctl --user daemon-reload
echo "[cells] installed systemd user units into $UNIT_DIR"
echo "[cells] next: provision cell quotas (web/README.md), then: systemctl --user enable --now chudware-gateway"
