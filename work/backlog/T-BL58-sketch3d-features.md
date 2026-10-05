---
id: T-BL58
title: 3D sketcher P3 — feature integration (sweep path, loft guide-curves)
workstream: backlog
owner: unassigned
status: backlog
branch: task/T-BL58-sketch3d-features
depends_on: [T-BL57]
lane: modeling-core
kind: new
priority: 4
deferred: yes
---

## Context

Phase 3 of the 3D sketcher plan (`docs/plans/SKETCH3D_PLAN.md`, T-BL45). PARKED until greenlight.
Depends on P2 (T-BL57). This is the highest-value slice: 3D sketch paths driving sweeps.

## Scope

- Verify a `sketch3d` edge body is consumed as a `sweep` `refs.path` through the EXISTING
  `sel(edge, region-chain)` grammar (per T-BL45 §4.1 the sweep compiler
  `core/geom/features.cpp::sweep_wire_solid` already accepts an arbitrary spine wire — precedent
  is the `helix` feature; expect little/no compiler change, mostly wiring + tests).
- Loft guide-curves + 3D section placement (may need a small loft-compiler extension —
  contract-first if so).

## Acceptance

- A 3D sketch path drives a sweep end-to-end via engine/MCP (e.g. a pipe around an out-of-plane
  bend).
- A loft with a 3D guide curve builds.
- `verify_battery` PASS on the resulting doc.
- ctest + `scripts/gate.sh` green; `graphify update .`.
