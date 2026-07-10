---
id: T-FIN19
title: Board/enclosure airflow handoff - thermal to conjugate-HT CFD
workstream: FIN
owner: fable-fin
status: doing
branch: task/T-FIN19-board-airflow-handoff
depends_on: [T-FIN16, T-SIM5]
lane: simulation
kind: new
priority: 3
---

## Context

When convection presets aren't enough (enclosures, fans), hand the board thermal
problem to CFD: build the air domain (enclosure box or ambient), run OpenFOAM conjugate
heat transfer (buoyantSimpleFoam path from run_cfd_accurate — requires T-SIM5's live
OpenFOAM integration) with board+components as the solid region and component powers as
volumetric sources, and return component temperatures + a comparison against the T-FIN16
preset-h solve (the verify step: was the film-coefficient assumption sane?). LBM fallback
gives qualitative flow only — labeled advisory, never a temperature verdict.

## Files / areas touched

- services/mcp/ecad_thermal.py — ecad_airflow_handoff conforming to simhandoff/1
- run_cfd_accurate CHT case generation for board solids
- verify: preset-h vs CHT component temps deviation table
- test: fixture with OpenFOAM present (skip cleanly when absent)

## Definition of done

- With OpenFOAM installed: demo board in an enclosure solves CHT and reports component temps
- Verify table quantifies preset-h error; guidance emitted when preset was optimistic
- Honest gating without OpenFOAM (advisory LBM flow render only)
