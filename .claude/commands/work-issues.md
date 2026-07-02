---
description: Open GitHub issues with assignees — the anti-overlap fix registry
---
!`gh issue list --state open --limit 100 2>/dev/null || echo "gh CLI unavailable — install GitHub CLI and run: gh auth login"`

These are the known issues and who is on them (https://github.com/Hunter-124/CAD/issues). Rules to apply and remind the user of:
- Before fixing ANY bug: check this list; if it's assigned to someone else, coordinate (team_send / issue comment) instead of duplicating the fix.
- Before starting a fix: claim it — `gh issue edit <n> --add-assignee @me --add-label in-progress` (or the issue_claim MCP tool).
- Found a bug you're not fixing now? File it — `gh issue create` (or issue_create) with repro + suspected files.

$ARGUMENTS
