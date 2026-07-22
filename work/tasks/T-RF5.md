---
id: T-RF5
title: RF wave-1 single agent entry — kill legacy agent_design path (node R1b)
workstream: RF
owner: unassigned
status: todo
branch: task/T-RF5-single-agent-entry
depends_on: [T-RF4]
lane: ai-copilot
kind: overhaul
priority: 2
---

## Context

Three agent brains exist: C++ core/agent/run.cpp (in-app default), Python
services/agent/agent_turn.py (DAG/PLM/MCP subagents), Python services/agent/loop.py
(3,732 lines — only the legacy agent_design path; worker_client.cpp:187 "NOT routed").
jobs.cpp keeps a legacy launcher (jobs.cpp:357-491) solely for the Cursor transport,
which T-RF4 replaces.

## Scope

Fold agent_design into the v2 run (mode flag) or delete it as a distinct method; remove
the legacy jobs.cpp launcher; delete loop.py + its tests + run_agent.py CLI remnant;
remove the engine_oracle/worker.py agent_design branch. GUI ends with exactly one run
path: agent_run_v2. MCP freshness rule applies.

## Definition of done

- [ ] agent_design gone or fully aliased to agent_run_v2; no jobs.cpp Python launcher.
- [ ] loop.py deleted; pytest engine/doc agent green.
- [ ] Copilot chat e2e verified headless (AI_CAD_SHOT scenario).
- [ ] gate.sh green; REFACTOR_LOG handoff; MCP instructions/cad_guide refreshed.
