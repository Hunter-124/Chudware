---
id: T-RF20
title: RF stream-Z MCP + DAG thin-client flip — no engine imports in Python (node R4f)
workstream: RF
owner: unassigned
status: todo
branch: task/T-RF20-thin-client-flip
depends_on: [T-RF19, T-RF4, T-RF9]
lane: platform-infra
kind: overhaul
priority: 3
---

## Context

Endgame of zero-Python: services/mcp and services/dag must call hostd/libaicad only.
Python agent_turn.py consumers (services/dag/node_runner.py, services/plm/assist.py +
ai_vc.py, subagent_rpc.py, mission_dispatch.py, reviewer_exec.py) either call the C++
agent over RPC or are deleted with their feature. Accepted fallback per the old plan:
thin Python clients over libaicad = still "zero Python ENGINE".

## Scope

Flip services/mcp + services/dag to daemon/libaicad calls only; remove every
engine_oracle import from the Python tree (grep proof in gate); move/delete the
agent_turn consumers. Advisor/ledger/mission tooling stays Python but engine-free.
MCP freshness rule applies to any tool-surface change.

## Definition of done

- [ ] `grep -r "engine_oracle\|from engine\|import engine" services/` returns nothing
      live; gate lint enforces it.
- [ ] MCP server + DAG runner work against hostd with no Python engine import.
- [ ] gate.sh green; REFACTOR_LOG handoff.
