#!/usr/bin/env bash
# Launch the team version-control dashboard:
#   Hot issues · Human roadblocks · Master DAG · Lane ownership (assign lanes to
#   people + their working branch) · Codebase (graphify) preview.
# Safe to run more than once — if the dashboard is already up it just opens a
# fresh browser tab instead of failing to bind the port.
cd "$(dirname "$0")/.." || exit 1
if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
exec "$PY" work/tools/dashboard.py "$@"
