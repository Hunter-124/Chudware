---
id: T-FIN20
title: Motion dynamics upgrade + motion->FEA load handoff
workstream: FIN
owner: fable-fin
status: done
branch: task/T-FIN20-motion-dynamics-fea
depends_on: [T-FIN15]
lane: simulation
kind: new
priority: 3
---

## Context

simulate_motion is kinematics-only. Add rigid-body dynamics: mass/inertia from
existing body properties + assigned materials, applied forces/torques/gravity, joint
reaction forces via the mate constraint jacobian, optional simple friction/damping
coefficients. Then the handoff: motion_fea_handoff exports the worst-case (or
user-selected time) joint reactions as load cases on the selected component and runs
run_fea_static — 'is this link strong enough at the worst instant' in one call, conforming
to simhandoff/1.

## Files / areas touched

- core: motion solver dynamics extension (mate solver + mass matrix)
- services/mcp/cad_tools.py — simulate_motion params + motion_fea_handoff
- V&V: pendulum / slider-crank reaction analytic cross-checks
- tests extending test_simulate_motion.cpp

## Definition of done

- Pendulum + crank analytic reaction forces match within tolerance
- Handoff produces a static study with reaction loads placed at joint faces, provenance recorded
- Kinematics-only results regression-identical when dynamics not requested

## Completion notes (2026-07-10, branch task/T-FIN20-motion-dynamics-fea)

- Shipped: core/doc/motion_dynamics.{h,cpp} (inverse dynamics, jacobian wrench bases,
  min-norm LS), doc_simulate_motion `dynamics` param, AssemblyResult.mate_geometry,
  MCP simulate_motion dynamics + motion_fea_handoff (simhandoff/1 motion_to_fea,
  dry_run), cad_guide simulation/motion updates.
- DoD: pendulum + slider-crank analytic reactions PASS (test_motion_dynamics, 191
  assertions); kinematics-only regression-identical (C++ + pytest field-set checks);
  handoff places reaction loads at joint faces with provenance (dry-run pytest PASS;
  live solve path shares run_fea_static's recipe, not exercised without Netgen engine).
- Tests: core ctest 52/52; pytest test_cad_tools -k "simulate_motion or motion_fea" 7
  passed; test_simhandoff 16 passed.
