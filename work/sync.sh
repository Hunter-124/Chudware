#!/usr/bin/env bash
# One-shot team sync: refresh remote board/issues/presence caches, print digest.
# Usage: work/sync.sh [agent-name]
cd "$(dirname "$0")/.." || exit 1
if [ -n "$1" ]; then
  exec python3 work/tools/teamsync.py sync --agent "$1"
else
  exec python3 work/tools/teamsync.py sync
fi
