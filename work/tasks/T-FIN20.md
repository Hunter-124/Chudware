---
id: T-FIN20
title: Motion dynamics upgrade + motion->FEA load handoff
workstream: FIN
owner: fable-fin
status: doing
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
