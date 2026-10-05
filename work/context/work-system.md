# work-system — git-tracked personal task board

Scope: work/tools/work.py, work/tasks/, work/backlog/, work/context/, work/prompts/, work/questions/, work/DAG.md
Contracts: none; dev-time infrastructure, separate from the product MCP server.
Canonical policy: work/WORKFLOW.md; layout and CLI reference: work/README.md.

## Entry points
- `work/tools/work.py` `load_tasks` — loads live cards and terminal `done/` archives;
  archived dependencies remain available to readiness and lane reports.
- Read-only commands: `cmd_validate` (schema/cycles), `cmd_ready` (unblocked work),
  `cmd_board` (filtered board, `--json`), `cmd_roadblocks`, `cmd_lanes`, `cmd_prompt`.
- `cmd_dag` — regenerates `work/DAG.md` from the cards; generated output belongs to
  the session driver, not parallel workers.
- Metadata/topology commands: `cmd_set`, `cmd_relink`, `cmd_checkdod`. These and the
  legacy note/status/claim/release/lane-assignment commands perform git operations.
  They are not safe leaf-agent helpers: only the integrator may use them when
  authorized, after inspecting their current implementation and affected paths.
- `work/tools/issue_reporter.py` / `work/report.sh` — explicit user-invoked desktop
  reporter; `/work-report` explains prerequisites and the non-GUI CLI alternative.

## How it connects
Cards are the board data; `work.py` reads them to produce views and self-contained
work orders (`work/prompts/driver.md`). `work/WORKFLOW.md` is normative: solo board,
no claim/reservation step and no team-coordination service. Source-file history does
not make an archived card current work.

## Debugging playbook
- `python3 work/tools/work.py board` / `ready` — inspect the live queue.
- `python3 work/tools/work.py roadblocks --json` / `lanes --json` — decision/area views.
- Integrator checks: `python3 work/tools/work.py validate`, then regenerate the DAG
  once after the combined card edits with `python3 work/tools/work.py dag`.
- Preserve terminal cards and archived questions: dependency resolution and provenance
  still use them. Do not relabel historical plans as pending implementation.
- `_lock` uses `work/.claim.lock` and handles expiry itself. Do not remove a lock
  held by another writer or run git-mutating board commands over concurrent edits.
- The status CLI refuses `done`; completing/archiving a card is an integrator action
  supported by actual delivery evidence, not a worker's automated status flip.

## Graphify starters
- `graphify query "work tools task board cmd_ready"` may orient a mapped card/tool.
  If the graph lacks this area, read `work/WORKFLOW.md` and `work/tools/work.py`
  directly; do not infer that a missing graph node means dead infrastructure.

## Related tasks/docs
- work/WORKFLOW.md — task policy and MCP/context freshness requirements
- work/README.md — layout, archive, CLI reference
- work/prompts/driver.md — work-order template
- work/DAG.md — generated dependency graph
- work/context/services-mcp.md — the separate product tool registry
