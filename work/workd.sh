#!/usr/bin/env bash
# Team sync daemon: continuously pulls remote task/issue/presence state, pushes
# your heartbeat, and prints a change digest each cycle. Leave it running in a
# terminal while you (and your Claude sessions) work.
# Usage: work/workd.sh <agent-name> [interval-seconds]
if [ -z "$1" ]; then echo "usage: work/workd.sh <agent-name> [interval-seconds]"; exit 2; fi
cd "$(dirname "$0")/.." || exit 1
exec python3 work/tools/teamsync.py daemon --agent "$1" --interval "${2:-60}"
