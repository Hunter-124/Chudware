# work-system — git-tracked task coordination

Scope: work/** (work/tools/work.py, work/tools/teamsync.py, work/tools/team_mcp.py, work/tools/setup_team_tools.py, work/tasks/, work/backlog/, work/context/, work/prompts/, work/questions/, work/DAG.md, work/README.md)
Contracts: None — this is developer-team infrastructure for coordinating 4 people + AI agents in parallel, entirely separate from the product's own MCP server (services/mcp/ — see work/context/services-mcp.md).

## Entry points
- work/tools/work.py:103 `cmd_validate` — schema + dependency-cycle check (CI-able)
- work/tools/work.py:113 `cmd_ready` — list todo tasks whose dependencies are all done
- work/tools/work.py:133 `cmd_board` — full status board grouped by workstream
- work/tools/work.py:149 `cmd_dag` — regenerate work/DAG.md (mermaid graph)
- work/tools/work.py:227 `cmd_claim` — atomically claim next unclaimed task (locks via work/.claim.lock)
- work/tools/work.py:297 `cmd_note` — append timestamped progress note to your claimed task
- work/tools/work.py:328 `cmd_status` — set task status (doing/review/blocked; never "done" via CLI)
- work/tools/work.py:354 `cmd_release` — give a claimed task back to the pool
- work/tools/work.py:374 `cmd_prompt` — emit self-contained agent work order from task file
- work/tools/work.py `cmd_relink` — edit depends_on/workstream, cycle-validated (the only way to rewrite task topology; never edit files directly)
- work/tools/work.py `cmd_board` also takes `--workstream/--owner/--status` filters and `--json` for structured output
- work/tools/_teamlib.py — shared helpers: per-machine `whoami` identity (work/sync/.identity) and graphify staleness check (manifest.json mtime diff)
- work/tools/team_mcp.py — unified `claim`/`release`/`set_status`/`note` tools take a `kind: "task"|"issue"` param (collapses the old task-claim/issue-claim duplication); legacy `work_claim`/`work_note`/`work_status`/`work_release`/`issue_claim`/`issue_comment` still work as thin aliases. Presence now auto-heartbeats on every tool call (no daemon required).
- work/tools/dashboard.py — team-progress GUI (Human Roadblocks / DAG / Graphify Preview / Verify tabs), launched via work/run_version_control.sh|.bat
- work/tools/teamsync.py:1-50 — sync daemon (presence heartbeats, inter-worker messages, GitHub issue cache on work-sync branch)
- work/tools/team_mcp.py:1-50 — MCP stdio server exposing work/, presence, messaging, context packs, GitHub issues
- work/tools/setup_team_tools.py — registers team_mcp.py with Claude Code + Claude Desktop

## How it connects
work.py is the source of truth: task front matter committed straight to master under the claim lock (work/.claim.lock, gitignored). No branches for claiming. teamsync.py mirrors presence/messages on a side work-sync branch (worktree at .worksync/, gitignored local state); team_mcp.py exposes both plus GitHub issues as MCP tools for Claude Code CLI/Desktop. Read work/README.md (layout + task lifecycle: todo → doing → review → done) and work/prompts/autopilot.md (agent coexistence rules, priority policy: humans first, no out-of-day-order claims) in full for the human-facing protocol.

## Debugging playbook
- `python3 work/tools/work.py validate` — catch schema/cycle errors before committing
- `python3 work/tools/work.py board` — full task status (worklist inversion: what blocks whom)
- `python3 work/tools/work.py ready [--owner NAME]` — what can start now
- `python3 work/tools/teamsync.py team` — who's online / task holdings
- Gotcha: work/.claim.lock is filesystem-based (gitignored) — stale after 120s; remove by hand if hung
- Gotcha: work/sync/ and .worksync/ are gitignored local state; never commit them
- Gotcha: claiming always commits straight to master, never a branch — this is atomic and race-safe
- Gotcha: status can NEVER be set to "done" via CLI (human merge-time action only); guarded in work.py:338

## Graphify starters
- Note: graphify does not index work/ — use direct file reads (work/README.md, work/tools/*.py) instead.

## Related tasks/docs
- work/README.md — task lifecycle, layout, adding tasks, CLI reference, owners table
- work/prompts/autopilot.md — agent loop, coexistence rules, escalation protocol, GUI workstream extras
- work/prompts/driver.md — template for cmd_prompt work orders (worktree setup, verification, escalation)
- work/DAG.md — generated mermaid dependency graph (regenerate with `python3 work/tools/work.py dag`)
- work/context/services-mcp.md — the OTHER, product MCP server (don't confuse the two)
