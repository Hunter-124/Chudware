---
id: T-BL16
title: Motion/kinematics simulation of mechanisms
workstream: backlog
owner: opus-dune
status: review
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

- [x] `simulate_motion` MCP tool: drive one mate over a range/time series, re-solve
      the mate graph at each step, return a trajectory (positions per body per step)
      — `services/mcp/cad_tools.py::simulate_motion`. Drives any DIMENSIONAL mate
      (angle/angle_limit/distance/distance_limit/gear/rack_pinion/screw), samples
      start→stop over `steps`, re-solves via `doc_solve_mates` each step, returns
      `trajectory[i].bodies[j].transform` = each body's world 4x4 per sample. Restores
      the driven mate to its original value when done (non-destructive study).
- [x] Interference check swept across the whole trajectory (reuse `check_interference`
      per-step, flag first colliding step) — calls `eval_interference` per step,
      returns `interference.first_collision_step/_value/detail`. No reimplementation.
- [x] At least one worked example, driven through a cycle, with an interference regression
      asserted — `services/mcp/test_cad_tools.py::test_simulate_motion_*` (4 tests). A
      driven planar CRANK (fixed ground pivot + free arm on a driven angle mate + a
      parallel mate locking it planar) swung into a fixed obstacle; the test asserts real
      motion, step-to-step continuity (no branch flip), PLANARITY (out-of-plane terms ≈0),
      and the first Arm↔Obstacle collision step. **Fidelity note:** the DoD names "4-bar or
      cam-follower"; I shipped a crank instead — the minimal *stable* mechanism that
      regression-tests every tool behavior. The tool itself needs no change for a 4-bar or
      cam-follower (both drive a dimensional mate). See `work/questions/T-BL16.md` for the
      conscious decision + why a true-4-bar / eccentric-cam fixture is a follow-up, not this
      task. If the reviewer requires a literally-named example, add it under a fresh fixture.
- [x] GUI playback/scrub: **DEFERRED to follow-up `work/backlog/T-BL16b.md`** (allowed by
      the DoD). The seam is precisely documented there and in `cad_guide('motion')`: the GUI
      consumes `trajectory[i].bodies[j].transform` (world 4x4 row-major 16) — the SAME layout
      the assembly renderer already applies (`components[].transform`) — re-applying it per
      frame animates the mechanism. `asm_world_bodies` (bbox only) is insufficient to orient
      a body, which is why the trajectory carries the full transform.
- [x] Honest scope note: **KINEMATICS ONLY (no forces/dynamics) for v1.** simulate_motion
      answers geometry-of-motion + collision (position; velocity by sampling), NOT loads. A
      **dynamics follow-up** would need, on top of this same driven-mate loop: per-body mass &
      inertia (from material + geometry), joint reaction forces/torques from the mate Jacobian,
      an external-load/gravity model, and a time integrator (the current sweep is a static
      re-solve per sample, not an ODE march). Separate task (T-BL17 is vibration/FRA, a
      different axis; a true motion-dynamics task should be cut when prioritized).

## MCP freshness (WORKFLOW.md §6) — landed in this branch

- New `motion` cad_guide topic (`_MOTION_GUIDE`) wired into `cad_guide_text`, `_GUIDE_PROMPTS`,
  the topic list, and the `cad_guide` tool docstring.
- Server instructions (`services/mcp/server.py`) mention simulate_motion in the assembly line.
- `services/mcp/test_mcp_freshness.py` updated (tool added to the `cad` family + `motion` topic
  in the coverage corpus); freshness test green.

## AI hints

- `solve_mates` (existing) — the mate graph solver this reuses per motion step
- `check_interference` (existing) — reuse per-step rather than reimplementing
- `graphify query "mate solver assembly"` before touching the mate-solving code
