---
id: T-E2
title: 1D pipe network solver + pressure drop
workstream: E
owner: opus-dell
status: done
branch: task/T-E2-network-solve
day: 7
depends_on: [T-E1]
lane: simulation
kind: new
---

## Context

Steady-state network solve: EPANET 2.2 via WNTR (MIT/BSD, bundle freely) for water networks; `fluids` (MIT) for component pressure drop (Darcy-Weisbach, Colebrook, fitting K-factors). Pump curves are user-imported CSVs with affinity-law scaling (no free curve DB exists). MCP tools: piping_solve, piping_report.

## Definition of done

- [x] piping doc -> network model -> node pressures + link flows (native fluids+scipy
      nodal solver; WNTR/EPANET NOT used — see Driver notes / Q1)
- [x] Per-segment pressure-drop report with K-factor breakdown
- [x] Pump curve CSV import + affinity scaling
- [x] piping_solve/piping_report MCP tools registered

## AI hints

- WNTR: https://github.com/USEPA/WNTR
- fluids fittings module for K-factors

## Driver notes (opus-dell)

- Backend decision: native `fluids`+numpy/scipy nodal solver (mirrors
  `engine/sim/circuit.py` MNA precedent), **no WNTR/EPANET dep added** — per foreman
  addenda. Rationale + advisor sign-off request logged in `work/questions/T-E2.md` (Q1).
- Solver: `engine/piping/solve.py` (H-equation Newton, Darcy-Weisbach + Colebrook +
  fitting K-factors). Pump-curve CSV import + affinity scaling and the per-segment
  pressure-drop report live in the same module. MCP `piping_solve`/`piping_report` in
  `services/mcp/piping_tools.py`, wired append-only into `services/mcp/server.py`.
