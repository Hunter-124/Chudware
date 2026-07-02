---
id: T-E2
title: 1D pipe network solver + pressure drop
workstream: E
owner: opus-dell
status: doing
branch: task/T-E2-network-solve
day: 7
depends_on: [T-E1]
---

## Context

Steady-state network solve: EPANET 2.2 via WNTR (MIT/BSD, bundle freely) for water networks; `fluids` (MIT) for component pressure drop (Darcy-Weisbach, Colebrook, fitting K-factors). Pump curves are user-imported CSVs with affinity-law scaling (no free curve DB exists). MCP tools: piping_solve, piping_report.

## Definition of done

- [ ] piping doc -> WNTR/EPANET model -> node pressures + link flows
- [ ] Per-segment pressure-drop report with K-factor breakdown
- [ ] Pump curve CSV import + affinity scaling
- [ ] piping_solve/piping_report MCP tools registered

## AI hints

- WNTR: https://github.com/USEPA/WNTR
- fluids fittings module for K-factors
