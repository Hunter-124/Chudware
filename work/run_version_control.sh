#!/usr/bin/env bash
# Launch the team-progress dashboard (Human Roadblocks / DAG / Graphify Preview / Verify).
cd "$(dirname "$0")/.." || exit 1
exec python3 work/tools/dashboard.py
