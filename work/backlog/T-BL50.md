---
id: T-BL50
title: 3D sketcher P2 — 3D curves + relational constraints + edge-body emit
workstream: backlog
owner: unassigned
status: backlog
branch: task/T-BL50-sketch3d-curves
depends_on: [T-BL49]
lane: modeling-core
kind: new
priority: 4
deferred: yes
---

## Context

Phase 2 of the 3D sketcher plan (`docs/SKETCH3D_PLAN.md`, T-BL45). PARKED until greenlight.
Depends on P1 (T-BL49).

## Scope

- Entities: `arc3d`, `spline3d`, `polyline3d`.
- Constraints: `parallel3d`, `perpendicular3d`, `angle3d`, `point_on3d`, `on_plane` (the bridge
  that reuses 2D planar relation semantics inside a chosen datum plane).
- Emit a `TopoDS_Wire` / edge body from solved 3D entities, reusing the edge primitives
  `core/geom/profile.cpp::build_edge` already uses (line→`BRepBuilderAPI_MakeEdge`,
  arc→`GC_MakeArcOfCircle`, spline→`GeomAPI_Interpolate`) but built directly in 3D.

## Clean-room protocol (binding, T-BL45 §1). No GPL solver source.

## Acceptance

- 3D arcs/splines solve and emit valid OCCT edges.
- `on_plane` reproduces the corresponding planar (2D) constraint result.
- Emitted edge body is selectable via a `kind:"edge"` selector.
- ctest + `scripts/gate.sh` green; `graphify update .`.
