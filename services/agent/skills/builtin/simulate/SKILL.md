---
name: simulate
description: Run FEA/CFD/motion studies with honest setup checks — fixtures+loads, mesh sanity, hand-calc anchors, and narrated postcheck before claiming results.
inject: auto
triggers:
  keywords: [fea, cfd, simulate, simulation, stress, modal, thermal, wind tunnel, drag, load case, will it hold, will this hold, strong enough, hold up under, factor of safety, safety factor, deflection, buckling, von mises, yield strength]
  tools: [run_fea_static, run_fea_modal, run_cfd_flow, simulate_motion]
  phases: [sim]
---

# Simulate

## Static FEA sequence

1. Geometry ready and verified (`describe_part` / assembly seated with `solve_mates`).
2. Material: `set_material` or study material block with real E, nu, yield.
3. Study setup: at least one fixture + one load. Name faces in plain words.
4. Optional precheck / mesh hint from sim advisor tools when available.
5. `run_fea_static(...)` — read max stress/displacement; compare to yield and any hand-calc anchor.
6. Narrate limits: mesh coarseness, BC idealizations, linear assumptions.

## CFD / flow

1. Watertight outer shape; units mm, flow conditions explicit (m/s, fluid).
2. `run_cfd_flow(...)` with stated velocity/AoA when relevant.
3. Report drag/lift with the solver identity; do not invent wind-tunnel precision.

## Motion

1. Assembly mates solved.
2. `simulate_motion(mate_id, start, stop)` — sweep interference across the range.
3. Fail if clash appears at any step.

## Rules

- Verify-before-done: no "looks fine" without the solver payload + a sanity check.
- Missing fixture or load is a blocker — fix setup, do not run blind.
- Cross-sim handoff formats: `cad_guide('simulation')`.

## Deeper docs

- `cad_guide('simulation')`, `cad_guide('motion')`
