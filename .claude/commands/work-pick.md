---
description: Pick and claim a task to work on (atomic, race-safe), then execute its work order
---
Ready (unblocked, unclaimed) tasks:

!`python3 work/tools/work.py ready 2>/dev/null || py -3 work/tools/work.py ready 2>/dev/null || python work/tools/work.py ready`

The user wants to start work. Arguments (may contain an owner name and/or a task id): $ARGUMENTS

1. Determine the owner name: from the arguments, or ask the user for their agent/contributor name if not given.
2. Claim: `python3 work/tools/work.py claim --owner <name> [--task <id>]` — this is the ONLY legitimate way to take a task (it locks, pulls, commits the claim to master, and pushes). On LOST-RACE, run it again.
3. Emit the work order with `python3 work/tools/work.py prompt <id>` and follow it exactly (worktree on the printed branch, driver rules, stop at status review).
4. Post progress as you go: `python3 work/tools/work.py note <id> --owner <name> --text "..."` — parallel sessions and humans read these.
5. If the aicad-team MCP tools are available (work_claim, work_note, team_send), prefer them over raw commands.
6. Note: `ready`/`claim` skip tasks with `deferred: yes` (parked ML/SaaS work) and tiebreak fix-before-new within a priority. To steer metadata (lane, kind, priority, deferred flag): `python3 work/tools/work.py set <id> --owner <name> [--lane L] [--kind fix|overhaul|new] [--priority N] [--deferred yes|no]` (or `work_set` MCP tool).
