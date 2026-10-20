#!/usr/bin/env bash
# Provision only reviewed offline wheels; never build/install a private checkout.
set -euo pipefail
BASE_PYTHON="${CHUDWARE_BASE_PYTHON:-${CHUDWARE_CI_BASE_PYTHON:-${PYTHON:-}}}"
for name in "${!PYTHON@}" "${!LD_@}" "${!DYLD_@}"; do
    [ -z "$name" ] || unset "$name"
done
unset __PYVENV_LAUNCHER__ VIRTUAL_ENV
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT/.venv-mcp}"
CHUDWARE_WHEELHOUSE="${CHUDWARE_WHEELHOUSE:-$ROOT/wheelhouse}"
case "$BASE_PYTHON" in
    /*) ;;
    *) echo "error: select an independently authenticated absolute base Python via CHUDWARE_BASE_PYTHON (or explicit PYTHON)" >&2; exit 1 ;;
esac
"$BASE_PYTHON" -I -S -c 'import sys; sys.path.insert(0, sys.argv[1]); from services.installer.provision import main; sys.exit(main(sys.argv[2:]))' \
    "$ROOT" --base-python "$BASE_PYTHON" --venv-dir "$VENV_DIR" --wheelhouse "$CHUDWARE_WHEELHOUSE" "$@"
"$VENV_DIR/bin/python" -I -c 'import sys,runpy;sys.path.insert(0,sys.argv[1]);sys.argv=[sys.argv[2],"--deps-only"];runpy.run_path(sys.argv[0],run_name="__main__")' \
    "$ROOT" "$ROOT/scripts/mcp_runtime_audit.py"
