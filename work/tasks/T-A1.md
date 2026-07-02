---
id: T-A1
title: Freeze dag_schema.md and scaffold services/dag/
workstream: A
owner: Bruh
status: todo
branch: task/T-A1-dag-schema-scaffold
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
- [x] `services/dag/` package importable with `from services.dag import executor`
- [x] `executor.run()` can execute a two-node test graph and write a checkpoint SQLite file
- [x] Smoke-test passes: `python -m pytest services/dag/test_executor.py -q`

## AI hints

- `graphify query "LangGraph agent services"` — check if any LangGraph usage already exists
- `graphify explain "mission.h"` — understand the C++ mission stage machine before mirroring states
- Relevant files: `services/agent/loop.py`, `core/agent/mission.h`
- The node contract's `autonomy` field must match the `pause` verdict being added to `machine.h` on day 3 (T-A3)
