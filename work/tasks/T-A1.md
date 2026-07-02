---
id: T-A1
title: Freeze dag_schema.md and scaffold services/dag/
workstream: A
owner: sonnet-ash
status: done
branch: task/T-A1-dag-schema
day: 1
depends_on: [T-S1]
---

## Context

Day 1 for the DAG orchestrator covers two separable deliverables: (AM) freezing the contract that all workstreams depend on, and (PM) standing up the LangGraph executor scaffold. The contract (`contracts/dag_schema.md`) defines node types, the node execution contract (inputs/tools/end_condition/verify/on_fail/autonomy), checkpoint format, and run states. The scaffold in `services/dag/` wires LangGraph + `langgraph-checkpoint-sqlite` and can load a graph JSON and persist run state to `%APPDATA%/AI-CAD/runs/`.

## Files / areas touched

- `contracts/dag_schema.md` — new frozen contract (all workstreams read this)
- `services/dag/__init__.py`, `services/dag/executor.py`, `services/dag/graph_loader.py` — new module
- `services/mcp/server.py` — eventual registration point for dag MCP tools (day 4)
- `services/agent/` — existing agent loop that node runner will wrap on day 2

## Steps

1. Run `graphify query "mission runner agent loop"` to understand existing `services/agent/` and `core/agent/mission.h` before writing the schema.
2. Draft `contracts/dag_schema.md`: node types (`plan`, `design`, `ecad`, `firmware`, `structural`, `source`, `simulate`, `verify`, `gate`, `report`); node contract fields (inputs, tools, end_condition, verify, on_fail, autonomy); checkpoint format (SQLite via `langgraph-checkpoint-sqlite`); run states (`pending`, `running`, `paused`, `done`, `failed`); autonomy levels (`auto`, `approve`, `manual`).
3. Get review sign-off from B/C/D — this contract is frozen after today.
4. Create `services/dag/` Python package; install `langgraph` and `langgraph-checkpoint-sqlite` into `.venv-mcp`.
5. Implement `graph_loader.py`: load a graph JSON (nodes + edges) into a LangGraph `StateGraph`.
6. Implement `executor.py`: run the loaded graph, persist checkpoints to `%APPDATA%/AI-CAD/runs/<run_id>.db`; expose a minimal `run(graph_json, initial_state)` function.
7. Write a smoke-test: create a two-node graph (plan → report), run it, assert checkpoint file created.

## Definition of done

- [x] `contracts/dag_schema.md` exists, all required fields documented, workstream leads have confirmed no blockers
- [x] `services/dag/` package importable with `from dag import executor` (services/ on sys.path via conftest, matching agent convention)
- [x] `executor.run()` can execute a two-node test graph and write a checkpoint SQLite file
- [x] Smoke-test passes: `python -m pytest services/dag/test_executor.py -q`

## AI hints

- `graphify query "LangGraph agent services"` — check if any LangGraph usage already exists
- `graphify explain "mission.h"` — understand the C++ mission stage machine before mirroring states
- Relevant files: `services/agent/loop.py`, `core/agent/mission.h`
- The node contract's `autonomy` field must match the `pause` verdict being added to `machine.h` on day 3 (T-A3)

## Completion notes (sonnet-ash, 2026-07-02)

**Recovery:** sonnet-dane died pre-first-commit; all salvaged work was reviewed and kept
as-is (quality was sound) with one fix: import paths changed from `from services.dag`
to `from dag` to match the repo convention (services/agent uses `from agent import ...`).
Added `services/dag/conftest.py` to add `services/` to sys.path for pytest.

**What landed:**
- `contracts/dag_schema.md` — 13-section frozen contract `aicad.dag/1`; all required
  fields, 10 node types, 3 autonomy levels, 4 fail actions, 5 run states, 6 node
  states, checkpoint format, event table, 3 reserved seams (T-A3/T-A4/T-F12)
- `contracts/licenses.md` — 5 rows: langgraph 1.2.7, langgraph-checkpoint-sqlite 3.1.0,
  aiosqlite 0.22.1, langchain-core 1.4.8, langsmith 0.9.6 (all MIT)
- `services/dag/` — package with executor.py, graph_loader.py, __init__.py, conftest.py
- `services/dag/test_executor.py` — 17 tests: smoke (checkpoint, events, importable),
  validation suite (schema/node/edge checks), single-node, default_runs_dir

**Test counts:** 17/17 passed (`mcp-venv pytest services/dag/test_executor.py -q`)

**Gate:** core build PASS, ui/abstract PASS, ctest PASS, ui/imgui PASS.
`check.py` fails: feature_surface_audit + parity_audit — **pre-existing** (confirmed
same on master). Gate pytest fails: 5 tests in engine/doc + agent — **pre-existing**
(same 5 on master: test_golden_parse, test_golden_byte_stable, 3 test_loop_mock).
T-A1 changes caused no new failures.

**Downstream notes for T-A2/A3/A4:**
- T-A2: replace `_make_node_handler` stubs in `graph_loader.py` with real dispatch per NodeType
- T-A3: wire `interrupt()` / `pause` verdict for `autonomy: "approve"` nodes (currently pass-through stub with warning)
- T-A4: implement verify-node logic (currently no-op stub)
- T-F12: `dag_replan` seam — executor.run() does not assume immutable node list
