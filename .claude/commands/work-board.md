---
description: Show the task board, who's online, and open issues (team situational awareness)
---
Current team state:

!`python3 work/tools/work.py board 2>/dev/null || py -3 work/tools/work.py board 2>/dev/null || python work/tools/work.py board`

!`python3 work/tools/teamsync.py team 2>/dev/null || py -3 work/tools/teamsync.py team 2>/dev/null || python work/tools/teamsync.py team`

Summarize for the user: what's in flight and by whom, what's ready to claim, which review branches await human merge, who's online, and any open issues that overlap current work. $ARGUMENTS
