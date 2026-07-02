---
id: T-M2
title: CAM overhaul: true adaptive clearing (trochoidal arc-blend linking)
workstream: M
owner: unassigned
status: todo
branch: task/T-M2-cam-overhaul
day: 2
depends_on: []
---

## Context

CAM overhaul wave 1. `Adaptive2D`/`AdaptiveClear3D` are concentric-offset clearing
mislabeled as adaptive — engagement spikes at corners defeat the point of the strategy.
Audit: work/audits/CAM_AUDIT.md §3.2, §T-M2.

## Files / areas touched

- core/cam/op_adaptive.cpp, core/cam/op_3d.cpp, core/cam/geometry.h (arc helper),
  core/tests/test_cam.cpp

## Definition of done

- [ ] Ring transitions linked with tangent arc moves (radius = engage*0.5), not direct feed_to
- [ ] Engagement-angle check emits a warning when any segment exceeds op.stepover*1.5
- [ ] Naming honest: concentric vs adaptive distinguished internally
- [ ] Test: corner-heavy pocket shows bounded engagement (assert no over-engagement warning
      with arc-blend ON, warning present with it OFF)
- [ ] core build + ctest green; `graphify update .`

## Driver notes (Fable)

- Audit §T-M2 has the algorithm sketch; the arc helper can be a trivial 2D circular arc.
- Do NOT touch stock_sim.cpp (T-M6/M3 own it) or post.cpp (T-M1). test_cam.cpp additions
  append-only.
