# work-system — git-tracked personal task board

Scope: work/** (work/tools/work.py, work/tasks/, work/backlog/, work/context/, work/prompts/, work/questions/, work/DAG.md, work/README.md)
Contracts: None — this is solo dev-time infrastructure, entirely separate from the product's own MCP server (services/mcp/ — see work/context/services-mcp.md).

## Entry points
- work/tools/work.py:103 `cmd_validate` — schema + dependency-cycle check (CI-able)
- work/tools/work.py:113 `cmd_ready` — list todo tasks whose dependencies are all done
- work/tools/work.py:133 `cmd_board` — full status board grouped by workstream
- work/tools/work.py:149 `cmd_dag` — regenerate work/DAG.md (mermaid graph)
- work/tools/work.py:227 `cmd_claim` — mark a task in-progress (locks via work/.claim.lock; vestigial from the multi-agent era, harmless solo)
- work/tools/work.py:297 `cmd_note` — append timestamped progress note to a task
- work/tools/work.py:328 `cmd_status` — set task status (doing/review/blocked)
- work/tools/work.py:354 `cmd_release` — put a task back to todo
- work/tools/work.py:374 `cmd_prompt` — emit a self-contained work order from a task file
- work/tools/work.py `cmd_relink` — edit depends_on/workstream, cycle-validated (the only way to rewrite task topology; never edit files directly)
- work/tools/work.py `cmd_board` also takes `--workstream/--owner/--status` filters and `--json` for structured output

## How it connects
work.py is the source of truth: task front matter committed straight to the working
branch. Read work/README.md (layout + task lifecycle: todo → doing → review → done)
for the full protocol. This used to also drive a multi-agent team-coordination layer
(teamsync.py, team_mcp.py, dashboard.py, autopilot.md) — that layer was removed
2026-07-08 when the project went back to solo dev; work.py itself is unaffected.

## Debugging playbook
- `python3 work/tools/work.py validate` — catch schema/cycle errors before committing
- `python3 work/tools/work.py board` — full task status (worklist inversion: what blocks whom)
- `python3 work/tools/work.py ready` — what can start now
- Gotcha: work/.claim.lock is filesystem-based (gitignored) — stale after 120s; remove by hand if hung
- Gotcha: status can NEVER be set to "done" via CLI (mark it by hand when actually shipped)

## Graphify starters
- Note: graphify does not index work/ — use direct file reads (work/README.md, work/tools/*.py) instead.

## Related tasks/docs
- work/README.md — task lifecycle, layout, adding tasks, CLI reference
- work/prompts/driver.md — template for cmd_prompt work orders
- work/DAG.md — generated mermaid dependency graph (regenerate with `python3 work/tools/work.py dag`)
- work/context/services-mcp.md — the OTHER, product MCP server (don't confuse the two)
