---
description: Show the task board, who's online, and open issues (team situational awareness)
---
Current team state:

!`python3 work/tools/work.py board 2>/dev/null || py -3 work/tools/work.py board 2>/dev/null || python work/tools/work.py board`

!`python3 work/tools/teamsync.py team 2>/dev/null || py -3 work/tools/teamsync.py team 2>/dev/null || python work/tools/teamsync.py team`

Summarize for the user: what's in flight and by whom, what's ready to claim, which review branches await human merge, who's online, and any open issues that overlap current work.

For human-attention items (review queue, blocked tasks, open questions): `python3 work/tools/work.py roadblocks` (or `work_roadblocks` MCP tool) — check this first when coming online.
For per-lane ship-readiness rollup: `python3 work/tools/work.py lanes` (or `work_lanes` MCP tool). Lanes are user-facing feature areas; deferred tasks (parked ML/SaaS plans) are excluded from active counts. $ARGUMENTS
