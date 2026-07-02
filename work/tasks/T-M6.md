---
id: T-M6
title: CAM overhaul: pocket entry fix + arc-true stock simulation
workstream: M
owner: unassigned
status: todo
branch: task/T-M6-cam-overhaul
day: 1
priority: 2
depends_on: []
---

## Context

CAM overhaul wave 1 (start immediately, parallel with T-M1). Fixes: pocket non-helix entry
plunges without a prior rapid_xy (air-plunge from wrong position — audit D3); stock sim
chord-approximates arcs (false-negative gouge checks on convex surfaces — D8); turning sim
has zero coverage (G4). Audit: work/audits/CAM_AUDIT.md §3.1-3.3, §T-M6.

## Files / areas touched

- core/cam/op_milling.cpp (~L147-153), core/cam/stock_sim.cpp (~L205-212),
  core/tests/test_cam.cpp

## Definition of done

- [ ] gen_pocket2d non-helix entry: rapid_to_clearance + rapid_xy(entry) before plunge_to
      (+ regression test)
- [ ] MoveKind::Arc swept along the arc parameterization (center+radius already on Move),
      not the chord (+ convex-surface gouge test that chord-sweep would miss)
- [ ] TurnRough sim test: removed_volume_mm3 > 0 and machined_fraction > 0.3
- [ ] core build + ctest green; `graphify update .`

## Driver notes (Fable)

- Audit gives file:line for both defects — surgical fixes, no refactors.
- You own stock_sim.cpp for this wave; T-M3 branches off you (merge order M6 → M3).
  test_cam.cpp additions append-only (shared with M1/M2).
