---
id: T-RF20
title: RF stream-Z MCP + DAG thin-client flip — no engine imports in Python (node R4f)
workstream: RF
owner: unassigned
status: done
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

DECISION POINT (found 2026-07-22): engine_oracle/kernel.py (657 lines) is the legacy
build123d code-executor behind the v1 MCP `run_model`/`run_assembly` tools — executing
AI-written build123d fundamentally needs a Python interpreter, so it cannot be "ported
to C++". Options: (a) delete the legacy run_model surface (the parametric doc engine
superseded it — YAGNI says this), (b) keep a thin MCP-side executor wrapping the
build123d pip package directly (no engine_oracle import). Evaluate live consumers
(MCP tool usage, services/mcp/cad_tools run_model registration, GUI call sites) and
recommend with evidence; deletion is the default if nothing real consumes it.

## Definition of done

- [x] `grep -r "engine_oracle\|from engine\|import engine" services/` returns nothing
      live; gate lint enforces it.
- [x] MCP server + DAG runner work against hostd with no Python engine import.
- [x] gate.sh green; REFACTOR_LOG handoff.

## Direct-import inventory (2026-07-22, integrator recon — the mechanical surface)

Top `from engine.*`/`import engine.*` importers in services/ (count = import sites):
test_ecad_tools 18, ecad_pcb_tools 14, ecad_sim_tools 12, ecad_common 12,
test_ecad_sim_e2e 9, structural_tools 8, test_board_flow 8, gsl_tools 7,
board_flow 7, ascii2d 7, ecad_thermal 6, ecad_sch_tools 6, hydraulic_tools 5,
code_compliance_tools 5, recon_tools 4, piping_tools 4, plus long tail
(test_multiphysics_misc, test_ecad_hardening, test_sw_feature_import,
test_simhandoff, firmware_tools:60 direct provision import, …).
Sub-split R4f per family at dispatch: ecad_*/board_flow/ascii2d, structural+
hydraulic+piping, gsl/recon, firmware, tests. Most flips are mechanical:
replace the direct call with the equivalent worker-RPC method (the method
surface the Z-stream ported), keeping tool signatures identical.
