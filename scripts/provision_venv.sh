#!/usr/bin/env bash
# Provision the source checkout's Python runtime without touching engine builds.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT/.venv-mcp}"
CHUDCODE_RPC_SRC="${CHUDCODE_RPC_SRC-/home/hunter/Repositories/omp/python/chudcode-rpc}"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    python3 -m venv "$VENV_DIR"
fi
PYTHON="$VENV_DIR/bin/python"
if [[ ! -f "$VENV_DIR/.chudware-pip-upgraded" ]]; then
    "$PYTHON" -m pip install --upgrade pip
    touch "$VENV_DIR/.chudware-pip-upgraded"
fi

if [[ -d "$CHUDCODE_RPC_SRC" ]]; then
    # The private package is not on PyPI. Supply its wheel to the requirements
    # resolver first, keeping requirements.txt (including its pin) authoritative.
    WHEELS="$(mktemp -d)"
    trap 'rm -rf -- "$WHEELS"' EXIT
    "$PYTHON" -m pip wheel --no-deps --wheel-dir "$WHEELS" "$CHUDCODE_RPC_SRC"
    "$PYTHON" -m pip install --find-links "$WHEELS" -r "$ROOT/requirements.txt"
    "$PYTHON" -m pip install --editable "$CHUDCODE_RPC_SRC"
else
    "$PYTHON" -m pip install -r "$ROOT/requirements.txt"
fi
"$PYTHON" "$ROOT/scripts/mcp_runtime_audit.py" --deps-only
