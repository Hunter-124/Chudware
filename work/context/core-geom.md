# core/geom — geometry kernel, sketcher, constraint solver

Scope: core/geom/**, core/sketch/**, core/solver/**
Contracts: contracts/doc_schema.md (entities/constraints §3), contracts/feature_params.md (B-rep feature vocabulary)
Canonical guidance: .claude/skills/chudware-dev/SKILL.md (geometry history and build/resource safety).

## Entry points
- `core/solver/nsolve.h` `System` — numeric residual-based solver (Levenberg-Marquardt, Eigen, analytic Jacobians).
- `core/sketch/sketch.h` `solve_sketch` — sketch entities/constraints → solver variables, solution and warm state.
- `core/sketch/sketch.h` `drag` / `DragContext` — constrained interactive drag with a 10 ms solve budget.
- `core/sketch/sketch.h` `measure_constraint` — solved dimensional values.
- core/geom/aicm.h — ASCII-Compressed-Index-Mesh tessellation format (STEP+ container read/write)
- core/geom/features.h — B-rep feature compilation (extrude/revolve/sweep/loft/rib primitives)
- core/geom/dress.h — dress-up features (fillet/chamfer/shell/draft); calls OCCT's ShapeUpgrade

## How it connects
`core/solver` knows no geometry; `core/sketch` wraps it for 2D constraints; `core/geom` supplies OCCT B-rep primitives. `core/doc/session.cpp` and `sketch_geom.cpp` place sketches and compile document features. `core/geom/plane.h` lifts the sketch plane to 3D.

## Debugging playbook
- Integrator build: `./build.sh core` (configured consumed trees; see build-test.md).
- Tests: `ctest --test-dir core/build-linux -R "sketch|nsolve|polygon|drag|plane|profile" --output-on-failure` (doctest suite under core/tests/test_*.cpp)
- Gotcha: sketch.h solve_sketch is 2D-only; face placement (sketch-on-face) is in core/doc/session.cpp / sketch_geom.cpp
- Residual blocks use analytic derivatives; preserve numerical regression assertions rather than weakening them to hide mismatches.
- Reuse `DragContext` only for the exact sketch/environment; reset it after any document change. Cached drag skips per-frame diagnostics, so retain drag-start statuses and compute full diagnostics on commit.

## Graphify starters
- graphify query "sketch_solver constraint solver system nsolve entities"
- graphify explain "SolveState"
- graphify path "sketch.h" "nsolve.h"

## Related tasks/docs
- work/tasks: grep `sketch|solver|geom|polygon` (polygon entity T-G1; constraint parity T-C1..C5)
- docs/review/GEOMETRY_LLM_REPRESENTATION_REVIEW.md (GSL `describe_sketch` built on solver output)
- contracts/doc_schema.md §3 (entity/constraint schema + coverage list)
- contracts/feature_params.md (B-rep feature vocabulary — primitives, dress-up, patterns, multi-body)
