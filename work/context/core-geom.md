# core/geom — geometry kernel, sketcher, constraint solver

Scope: core/geom/**, core/sketch/**, core/solver/**
Contracts: contracts/doc_schema.md (entities/constraints §3), contracts/feature_params.md (B-rep feature vocabulary)

## Entry points
- core/solver/nsolve.h:64 `System` — numeric residual-based constraint solver (Levenberg-Marquardt, Eigen-based, analytic Jacobians)
- core/sketch/sketch.h:66 `solve_sketch()` — parameterizes sketch entities to nsolve variables and solves 2D constraints (C++ port of engine/doc/sketch_solver.py); returns SketchSolution + warm-start state
- core/sketch/sketch.h:92 `drag()` — constrained interactive drag with 10ms wall-clock budget
- core/sketch/sketch.h:75 `measure_constraint()` — read solved values from dimensional constraints (distance/angle/radius)
- core/geom/aicm.h — ASCII-Compressed-Index-Mesh tessellation format (STEP+ container read/write)
- core/geom/features.h — B-rep feature compilation (extrude/revolve/sweep/loft/rib primitives)
- core/geom/dress.h — dress-up features (fillet/chamfer/shell/draft); calls OCCT's ShapeUpgrade

## How it connects
core/solver is a standalone numeric solver (knows nothing about geometry); core/sketch wraps it to solve 2D constraint problems (sketch entities → nsolve variables, constraints → residual blocks). core/geom provides OCCT-backed B-rep primitives for the 3D feature compiler. The feature tree (core/doc/session.cpp) uses core/sketch to resolve sketches on faces, then core/geom to build 3D solids. Sketch-plane lift (2D→3D) is in core/geom/plane.h. Python oracle: engine/doc/{nsolve,sketch_solver}.py (frozen). README reports 24/24 geometric-feature parity.

## Debugging playbook
- Build: `cmake -S core -B core/build-linux -DAICAD_WITH_OCCT=ON && cmake --build core/build-linux`
- Tests: `ctest --test-dir core/build-linux -R "sketch|nsolve|polygon|drag|plane|profile" --output-on-failure` (doctest suite under core/tests/test_*.cpp)
- Gotcha: sketch.h solve_sketch is 2D-only; face placement (sketch-on-face) is in core/doc/session.cpp / sketch_geom.cpp
- Gotcha: nsolve.h has no Jacobian validation — supply analytic derivatives only; finite-diff will silently ignore mismatches
- Gotcha: drag() wall-clock budget is 10 ms; UX sluggishness signals tight Jacobian code or system_rank complexity (e.g. many redundant constraints)

## Graphify starters
- graphify query "sketch_solver constraint solver system nsolve entities"
- graphify explain "SolveState"
- graphify path "sketch.h" "nsolve.h"

## Related tasks/docs
- work/tasks: grep `sketch|solver|geom|polygon` (polygon entity T-G1; constraint parity T-C1..C5)
- docs/GEOMETRY_LLM_REPRESENTATION_REVIEW.md (GSL `describe_sketch` built on solver output)
- contracts/doc_schema.md §3 (entity/constraint schema + coverage list)
- contracts/feature_params.md (B-rep feature vocabulary — primitives, dress-up, patterns, multi-body)
