---
id: T-M6
title: CAM overhaul: pocket entry fix + arc-true stock simulation
workstream: M
owner: opus-iron
status: review

status: done
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

- [x] gen_pocket2d non-helix entry: rapid_to_clearance + rapid_xy(entry) before plunge_to
      (+ regression test)
- [x] MoveKind::Arc swept along the arc parameterization (center+radius already on Move),
      not the chord (+ convex-surface gouge test that chord-sweep would miss)
- [x] TurnRough sim test: removed_volume_mm3 > 0 and machined_fraction > 0.3
- [x] core build + ctest green; `graphify update .`

## DONE (opus-iron, 2026-07-02)

Commit `8d9958a` on `task/T-M6-cam-overhaul`. Scope: op_milling.cpp + stock_sim.cpp + test_cam.cpp.

**D3 (pocket air-plunge).** `gen_pocket2d`'s non-helix branch now calls `pb.rapid_xy(entry)`
(which retracts to clearance internally) before `pb.plunge_to(z)`, so the tool is over the
hole before it plunges instead of diving from the previous loop/level's end XY. The helix
branch already positioned itself via `rapid_xy` — this brings the straight-plunge path to parity.

**D8 (arc chord approximation).** The stock-sim move loop now sweeps `MoveKind::Arc` along the
true arc (center+radius carried on the `Move`), sampling ~2 points per grid cell, instead of a
single `prev→to` chord. A chord bows inside a convex arc and under-removes near the mid-bulge,
which false-negatives the gouge check on curved passes. Refactored the per-segment dexel sweep
into a reused `sweep_seg` lambda; linear moves call it once, arcs once per sub-chord.

**G4 (turning sim + coverage).** Added an axisymmetric turning-sim branch: a top-down Z-dexel
cannot represent diameter reduction (removal is radial; the dexel's height axis is the spindle
axis). For `MachineKind::Lathe` + cylindrical stock we model a radius profile R(z), lower it as
each turning pass cuts, and integrate removed volume `π(R0²−R(z)²)dz`. Milling paths are
untouched (branch is gated on Lathe+cylinder).

**Tests (append-only, +3 cases).** D3: every `Plunge` is immediately preceded by a `Rapid` at
its own XY. D8: the arc mid-bulge (7.07,7.07 — 2.93 mm off the chord, beyond the 1 mm tool) is
cut to depth while a point outside the arc stays at the stock top. G4: rough a Ø24 bar toward
Ø12, assert `removed>0`, `machined_fraction>0.3` (~0.72), `remaining<stock`.

**Verified.** `test_cam` 54/54 (3498 assertions); full core ctest 18/18 green (no-OCCT build).
`graphify update .` run.

**Handoff note for T-M3 (branches off this branch, merge order M6 → M3):** the per-segment dexel
sweep is now the `sweep_seg` lambda in `simulate_setup`; the holder-collision check can reuse it
with a Z-offset extent. Turning takes an early-return branch before the milling dexel is built.

## Driver notes (Fable)

- Audit gives file:line for both defects — surgical fixes, no refactors.
- You own stock_sim.cpp for this wave; T-M3 branches off you (merge order M6 → M3).
  test_cam.cpp additions append-only (shared with M1/M2).

## Progress log
- 2026-07-02 22:22 UTC [opus-iron] REVIEW: task/T-M6-cam-overhaul pushed (commit 8d9958a). D3: gen_pocket2d non-helix entry now rapid_xy(entry) before plunge (no more air-plunge from stale XY). D8: stock_sim sweeps arcs along the true arc (center+radius), not the chord — fixes convex-surface gouge false-negatives; per-segment sweep refactored into a sweep_seg lambda. G4: added axisymmetric turning-sim branch (radius-profile R(z) integral) since the top-down dexel can't model diameter reduction; gated on Lathe+cylinder so milling is untouched. +3 tests: pocket-entry rapid-before-plunge, arc mid-bulge cut vs chord, TurnRough removed>0 & machined_fraction>0.3 (~0.72). Verified: test_cam 54/54 (3498 asserts), core ctest 18/18 green. HANDOFF for T-M3 (branches off M6): reuse sweep_seg for holder collision with a Z-offset extent; turning early-returns before the milling dexel. Ready for human merge.
