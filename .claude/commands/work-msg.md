---
description: Message the other active workers (or read your inbox)
---
Team presence:

!`python3 work/tools/teamsync.py team 2>/dev/null || py -3 work/tools/teamsync.py team 2>/dev/null || python work/tools/teamsync.py team`

Arguments: $ARGUMENTS

If the arguments contain a message to send: send it with
`python3 work/tools/teamsync.py send --agent <your-agent-name> [--to <recipient>] --text "<message>"`
(ask for the sender name if unknown; omit --to for broadcast). If the arguments say "inbox" or are empty, read messages instead:
`python3 work/tools/teamsync.py inbox --agent <your-agent-name>` and relay them to the user. Prefer the aicad-team MCP tools (team_send / team_inbox) when available.
