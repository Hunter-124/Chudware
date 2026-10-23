#!/usr/bin/env bash
# Create the Python environment the Chudware services run in (default: .venv-mcp).
#
#   scripts/provision_venv.sh [pip arguments...]
#
# Default (development): `python -m venv` plus `pip install -r requirements.txt` from
# PyPI; chudcode-rpc is built from the Chudcode commit requirements.txt pins (needs git).
# Extra arguments go to that pip call, e.g. `-r packaging/requirements-release.txt`
# for a release compiler venv or `pytest` for running the test suites.
#
# Reviewed offline mode: when CHUDWARE_WHEELHOUSE names a reviewed wheelhouse, install
# only the hash-locked wheel set in vendor/python/RUNTIME_WHEELS.lock through
# services/installer/provision.py, which never consults an index. That mode needs an
# absolute base interpreter in CHUDWARE_BASE_PYTHON (or PYTHON) and forwards the extra
# arguments to provision.py instead.
#
# Env: PYTHON (base interpreter, default python3), VENV_DIR (default <repo>/.venv-mcp).
set -euo pipefail
BASE_PYTHON="${CHUDWARE_BASE_PYTHON:-${PYTHON:-python3}}"
for name in "${!PYTHON@}" "${!LD_@}" "${!DYLD_@}"; do
    [ -z "$name" ] || unset "$name"
done
unset __PYVENV_LAUNCHER__ VIRTUAL_ENV
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT/.venv-mcp}"

if [ -n "${CHUDWARE_WHEELHOUSE:-}" ]; then
    case "$BASE_PYTHON" in
        /*) ;;
        *) echo "error: reviewed provisioning needs an independently authenticated absolute base Python in CHUDWARE_BASE_PYTHON (or PYTHON)" >&2; exit 1 ;;
    esac
    "$BASE_PYTHON" -I -S -c 'import sys; sys.path.insert(0, sys.argv[1]); from services.installer.provision import main; sys.exit(main(sys.argv[2:]))' \
        "$ROOT" --base-python "$BASE_PYTHON" --venv-dir "$VENV_DIR" --wheelhouse "$CHUDWARE_WHEELHOUSE" "$@"
else
    command -v "$BASE_PYTHON" >/dev/null 2>&1 || { echo "error: base Python not found: $BASE_PYTHON (set PYTHON)" >&2; exit 1; }
    command -v git >/dev/null 2>&1 || { echo "error: git is required to build chudcode-rpc from the Chudcode repository" >&2; exit 1; }
    [ -x "$VENV_DIR/bin/python" ] || "$BASE_PYTHON" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r "$ROOT/requirements.txt" "$@"
fi

"$VENV_DIR/bin/python" -I -c 'import sys,runpy;sys.path.insert(0,sys.argv[1]);sys.argv=[sys.argv[2],"--deps-only"];runpy.run_path(sys.argv[0],run_name="__main__")' \
    "$ROOT" "$ROOT/scripts/mcp_runtime_audit.py"
