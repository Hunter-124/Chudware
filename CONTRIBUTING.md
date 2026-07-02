# Contributing to AI-CAD

Four people + AI agents, trunk-based, fast cycles. Read this before touching anything.

## Prerequisites

- Visual Studio 2022 (MSVC) + CMake for C++ work
- Python 3.11 for engine/agent/MCP work
- The repo knowledge graph: run `graphify update .` after any code change (graphify-out/ is
  already populated)

---

## Workflow

### 1. Pick a task

```bash
python3 work/tools/work.py ready          # list unblocked tasks
python3 work/tools/work.py ready --owner <you>  # filter to yours
```

Tasks are YAML-front-matter files in `work/tasks/`. Each one lists its `file_list` (the exact
files it touches), `blocked_by` deps, and `status`.

### 2. Claim it

Edit the task file directly: set `owner: <you>` and `status: doing`. Commit and push that
single change straight to `master`. This is intentionally tiny so claims never conflict — the
task file is the mutex.

```bash
git add work/tasks/<id>-<slug>.md
git commit -m "claim task/<id>: <slug>"
git push origin master
```

### 3. Branch and build

```bash
git checkout -b task/<id>-<slug>
```

Do the work. Small commits. If you discover you need something outside your task's `file_list`,
log it as a new task file instead of scope-creeping — this keeps parallel work non-overlapping.

### 4. Gate before merging

```bash
bash scripts/gate.sh    # check.py + core build/ctest + ui/abstract tests
```

Do not merge a branch that fails the gate.

### 5. Merge and close

PR or direct merge back to `master`. In the merge commit (or the PR description), update the
task file: set `status: done`. Branches are short-lived — days, not weeks.

```bash
git checkout master
git merge --no-ff task/<id>-<slug>
git push origin master
git branch -d task/<id>-<slug>
```

### 6. Update the graph

```bash
graphify update .
```

Run this after any code change. AST-only, no API cost.

---

## Rules

**Contracts first.** Any new module ships a `contracts/*.md` the same day the module is
created. This is what makes the C++ cutover mechanical later. No module without a contract.

**Scoped write-sets.** Each task's `file_list` defines what it may touch. If two tasks run in
parallel, their file lists must be disjoint. If you need to extend a shared/append-only file
(`contracts/*`, top-level CMake, `core/api/libaicad.h`, `AGENTS.md`), do it in a dedicated
commit and coordinate with the rest of the team.

**Never weaken a parity assertion.** The C++ core is developed against the Python engine as a
differential oracle. Match Python unless Python is provably buggy.

**Log discovered work, don't absorb it.** When you find something that needs doing that is
outside your task scope, create a new task file and stop. Scope creep creates overlapping
write-sets and merge conflicts.

**One gate, no exceptions.** `bash scripts/gate.sh` must be green before any merge to master.

---

## License guardrails

The product is commercial closed-source. License discipline is non-negotiable. The full table
is in [docs/SPRINT_5DAY_PLAN.md](docs/SPRINT_5DAY_PLAN.md) (License guardrails section) — read
it. The short version:

| OK to bundle | Subprocess only (must offer source in installer) | Never |
|---|---|---|
| LangGraph, PyNite, Renode, probe-rs, ngspice, Verilator, openNURBS, ezdxf, IfcOpenShell (LGPL, dynamic link) | CalculiX, OpenFOAM, GHDL, Icarus, esptool, Arduino CLI, LibreDWG | AGPL anything, GPL linking, GrabCAD scraping, retailer scraping, bundling IBC/NDS/Eurocode text |

When adding a new dependency: check SPDX identifier, add it to `contracts/licenses.md`, and
confirm whether it goes in the bundle or subprocess column. If uncertain, ask before merging.

For format reverse-engineering work: interoperability RE is protected under DMCA §1201(f) and
EU Software Directive Art. 6 **only if clean-room** — the person documenting the format and the
person writing the parser must never share competitor source, SDKs, or EULA-bound tooling. See
[docs/PLATFORM_ADR.md](docs/PLATFORM_ADR.md) Decision 5 for the full guardrail.

---

## AI-tooling

AI agents (Claude Code, the in-app agent) are first-class contributors. Same rules apply.

**Start every task with a graph query:**
```bash
graphify query "<what you are about to build>"
graphify path "<ModuleA>" "<ModuleB>"   # understand cross-file relationships
graphify explain "<concept>"            # focused concept lookup
```

This returns a scoped subgraph — much faster and more precise than grep or file browsing for
orientation in a codebase this size.

**Use the in-repo MCP server** to drive design work from Claude Code:
```powershell
mcp_setup.bat    # registers the server with Claude Code, Cursor, VS Code, opencode
```

**Work order format for agents:** paste the task file (YAML front matter + body) into the agent
as the work order. The `file_list`, `context`, and `end_condition` fields tell the agent exactly
what it may touch and what done looks like.

**After code changes:** run `graphify update .` so the next agent session starts with an
accurate graph.

---

## What good task files look like

```yaml
---
id: "0007"
slug: "dag-node-runner"
title: "DAG node runner — wrap agent loop per node"
status: ready          # ready | doing | done | blocked
owner: null
blocked_by: ["0005"]   # IDs of tasks that must be done first
file_list:
  - services/dag/node_runner.py
  - services/dag/context_pack.py
  - contracts/dag_schema.md
end_condition: "node runner executes a single `design` node end-to-end in a pytest"
---

Body: what, why, any design notes.
```

Keep `file_list` tight. If you need a file not listed, either add it to the file list before
starting (and verify no other in-flight task claims it) or log a follow-on task.

---

## Getting help

- Architecture: [ARCHITECTURE.md](ARCHITECTURE.md) + [AGENTS.md](AGENTS.md)
- Platform direction: [docs/PLATFORM_ADR.md](docs/PLATFORM_ADR.md)
- Sprint tasks: [docs/SPRINT_5DAY_PLAN.md](docs/SPRINT_5DAY_PLAN.md)
- Frozen interface contracts: [contracts/](contracts/)
- Task system: [work/README.md](work/README.md) (built in parallel — may not exist yet on your checkout)
