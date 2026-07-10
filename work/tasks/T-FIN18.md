---
id: T-FIN18
title: Transient thermal FEA - capacitance matrix, time integration, duty-cycle loads
workstream: FIN
owner: fable-fin
status: doing
branch: task/T-FIN18-thermal-transient
depends_on: []
lane: simulation
kind: new
priority: 3
---

## Context

fea_thermal is steady-state only (noted deferred in fea_thermal.h). Add transient:
lumped/consistent capacitance matrix (rho*cp), implicit backward-Euler stepping (SparseLU
refactor reuse), time-varying load schedule (per-load duty cycle / piecewise schedule —
the aicad.powerprofile/1 artifact from T-FIN7 must be directly consumable), result frames
at requested times + envelope (max temp over run). Python oracle parity per the sim-stack
convention, V&V against an analytic transient slab case.

## Files / areas touched

- core/sim/fea_thermal.cpp/h — capacitance assembly + stepper
- engine/sim python oracle parity
- services/mcp/cad_tools.py — run_fea_thermal(transient params)
- scripts/vv_benchmark.py — analytic slab-quench case
- core/tests/test_thermal.cpp — transient cases

## Definition of done

- Analytic 1D slab transient matches within V&V tolerance; added to vv_benchmark
- Duty-cycle load (powerprofile artifact) runs and peak temp reported
- Steady-state results unchanged (regression green)
- Deploy note: redeploy dist .so per engine-change convention
