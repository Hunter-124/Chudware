---
id: T-RF16
title: RF stream-Z CFD/LBM sim_run_flow + sim_cancel cutover to core/ (node R4b)
workstream: RF
owner: unassigned
status: todo
branch: task/T-RF16-cfd-cpp
depends_on: [T-RF3]
lane: simulation
kind: overhaul
priority: 3
---

## Context

FEA already routes C++ by default. Probe-verified 2026-07-22 (contracts/web_parity.md):
`sim_run_flow` (LBM path) is ALREADY libaicad-dispatched — this node shrinks to
`sim_cancel` (unrouted everywhere today) + verifying the OpenFOAM solver path. Scope is
much smaller than originally estimated.

## Scope

Port the LBM flow solver to core/sim (CUDA optional — match existing core/sim patterns;
a CPU port with equal results is acceptable if CUDA is out of scope), reach parity on
the simsuite flow cases, flip routing for sim_run_flow + sim_cancel, delete the Python
LBM module in the same commit. Thermal-transient tests that ride along must stay green.

## Definition of done

- [ ] sim_run_flow served by libaicad; simsuite flow parity green.
- [ ] Python LBM deleted same commit; routing flipped (integrator merge).
- [ ] gate.sh green; REFACTOR_LOG handoff; web_parity inventory shrinks.
