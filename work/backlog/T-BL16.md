---
id: T-BL16
title: Motion/kinematics simulation of mechanisms
workstream: backlog
owner: unassigned
status: todo
branch: task/T-BL16-motion-simulation
depends_on: []
lane: simulation
kind: new
priority: 4
---

## Context

**Cut 2026-07-03 (Erik-directed).** The biggest missing day-to-day mechanical feature
versus SolidWorks Motion / Onshape Assembly Motion: assemblies and mates are already
solved (`solve_mates`), but nothing simulates an assembly actually *moving* — driving
one mate over a range and watching the rest of the mechanism follow, checking
interference across the sweep, and reading back velocity/torque/force at each mate.

Scope for v1: kinematic (position/velocity) simulation of rigid-body mechanisms driven
by an existing mate (revolute/prismatic/cam/gear-ish couplings already in the mate
solver), sampled over a motion range or time series. Full multibody dynamics (forces,
inertia, contact) is a stretch goal, not required for v1 — start with kinematics only
(no forces), which is far cheaper and still covers the common "does this linkage/cam/
gear train clear everything as it moves" question.

## Definition of done

- [ ] `simulate_motion` MCP tool: drive one mate over a range/time series, re-solve
      the mate graph at each step, return a trajectory (positions per body per step)
- [ ] Interference check swept across the whole trajectory (reuse `check_interference`
      per-step, flag first colliding step)
- [ ] At least one worked example: a 4-bar linkage or cam-follower assembly, driven
      through a full cycle, with an interference regression asserted
- [ ] GUI: playback/scrub control in the viewport for a solved trajectory (can be
      deferred to a follow-up UI task if v1 backend ships without it — note the seam)
- [ ] Honest scope note: kinematics only (no forces/dynamics) for v1; document what a
      dynamics follow-up (T-BL17-style) would need

## AI hints

- `solve_mates` (existing) — the mate graph solver this reuses per motion step
- `check_interference` (existing) — reuse per-step rather than reimplementing
- `graphify query "mate solver assembly"` before touching the mate-solving code
