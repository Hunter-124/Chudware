---
description: Sync with the team now — refresh remote board/issues/presence and report what changed
---
!`python3 work/tools/teamsync.py sync 2>/dev/null || py -3 work/tools/teamsync.py sync 2>/dev/null || python work/tools/teamsync.py sync`

Relay the digest to the user: new claims, status changes, new issues, messages, and whether the local checkout is behind on task state. If it is behind, suggest `git pull --rebase --autostash` (only when their working tree is in a safe state). $ARGUMENTS
