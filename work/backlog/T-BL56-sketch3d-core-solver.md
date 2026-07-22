---
id: T-BL56
title: 3D sketcher P1 — sketch3d data model + core Newton solver skeleton
workstream: backlog
owner: unassigned
status: backlog
branch: task/T-BL56-sketch3d-core
depends_on: [T-BL45]
lane: modeling-core
kind: new
priority: 4
deferred: yes
---

## Context

Phase 1 of the 3D sketcher build plan frozen in `docs/SKETCH3D_PLAN.md` (T-BL45). PARKED until
Hunter greenlights the build. **Do not start without a greenlight** (`work_set` to unpark) — and
first confirm DECISION-NEEDED-A (parity oracle) and DECISION-NEEDED-B (build-own vs subprocess)
in that doc are resolved.

## Scope

- New feature type `type:"sketch3d"` (plane-free, entities in world `[x,y,z]`, NO `refs.plane`).
- Entities: `point3d`, `line3d`. Constraints: `coincident3d`, `fix3d`, `distance3d`.
- New solver module `core/sketch3d/` — Newton loop over Eigen with analytic Jacobians, DOF
  analysis (`under`/`full`/`over`), mirroring `core/sketch/sketch.{h,cpp}` in 3-space.
- Schema addition to `contracts/doc_schema.md` (§3-3D) — append-only, dedicated commit.
- Per DECISION-NEEDED-A: if A1, write `engine/doc/sketch3d_solver.py` reference first + a
  differential harness (C++ matches Python).

## Clean-room protocol (binding, from T-BL45 §1)

Do NOT read SolveSpace / Frontier / any GPL solver source. Derive residuals/Jacobians from first
principles + our existing 2D solver + public literature only.

## Acceptance

- A 3D sketch of points+lines with distance/coincident/fix solves to `full` DOF.
- ctest green (differential parity vs Python oracle if A1 chosen).
- Document round-trips save/open; opens without solving (last-solved coords persisted).
- `bash scripts/gate.sh` green; `graphify update .` run.
