---
id: T-BL55
title: 3D sketcher P5 — advanced constraints + solver robustness
workstream: backlog
owner: unassigned
status: backlog
branch: task/T-BL55-sketch3d-advanced
depends_on: [T-BL53, T-BL54]
lane: modeling-core
kind: new
priority: 5
deferred: yes
---

## Context

Phase 5 of the 3D sketcher plan (`docs/SKETCH3D_PLAN.md`, T-BL45). PARKED until greenlight.
Final hardening phase; depends on P3 (T-BL53) and P4 (T-BL54).

## Scope

- Advanced constraints: `horizontal3d`, `vertical3d`, `symmetric3d`, `tangent3d` (3D tangent is
  the hardest — may stay partial with documented deferrals, mirroring the 2D conic-family policy).
- Warm-start / drag hardening for large 3D sketches.
- Optional: evaluate Ceres Solver (BSD-3, OK-to-bundle, no license debt) as a robustness fallback
  for hard 3D NLLS systems where analytic Jacobians prove fragile (own scoped decision — analytic
  stays the baseline per T-BL45 §3.3).

## Clean-room protocol (binding, T-BL45 §1). No GPL solver source.

## Acceptance

- Advanced constraints solve to correct DOF; deferrals documented.
- Drag warm-start stable on a large (50+ entity) 3D sketch.
- Robustness suite green; ctest + `scripts/gate.sh` green; `graphify update .`.
