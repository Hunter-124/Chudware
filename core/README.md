# `core/` — the pure-C++ engine (libchudware)

The clean-slate C++ rewrite of the Python `engine/`. See
`docs/` plan *Core Rewrite: Python Engine → Pure C++* (and the approved plan in
`~/.claude/plans/`) for the full motivation and wave breakdown.

End state: this becomes a static library linked directly into the C++ host
(single process, no stdio RPC). During migration the legacy Python engine stays
the **oracle** — a differential harness runs both on identical inputs and diffs
the results — so each subsystem is proven at parity before the host switches to
calling `libchudware` in-process.

## Layout

```
core/
  solver/   nsolve.{h,cpp}   damped-LM constraint solver core (port of engine/doc/nsolve.py)
  sketch/   sketch.{h,cpp}   entity parameterization + constraints (port of engine/doc/sketch_solver.py)
  tests/    test_nsolve.cpp  solver-core unit tests (doctest)
            test_sketch.cpp  sketch-solver tests mirroring engine/doc/test_sketch_solver.py
  CMakeLists.txt             standalone build (Eigen + doctest + nlohmann/json via FetchContent)
```

## Build & test (Windows, VS2022)

```
cmake -S core -B core/build -G "Visual Studio 17 2022" -A x64
cmake --build core/build --config Release
ctest --test-dir core/build -C Release            # or run bin\Release\test_*.exe
```

CMake 4.x note: doctest's `cmake_minimum_required(3.0)` is bypassed via
`SOURCE_SUBDIR` (header-only include), so **no** `CMAKE_POLICY_VERSION_MINIMUM`
override is needed — and must not be set (it poisons the cache; wipe `core/build`
if it ever gets in).

## Status (Wave 1a + 1b)

**Done & verified** (`test_nsolve` 6 cases, `test_sketch` 22 cases all green;
`tools/solver_diff.py` 36/36 fixtures at Python↔C++ parity):
- nsolve numeric core: residual/Jacobian registry, damped Levenberg-Marquardt,
  null-space-projected constrained drag, QR-rank DOF / redundant / conflicting
  diagnostics (chronological attribution).
- sketch entities: point, line, circle, arc, ellipse, ellipse_arc, parabola,
  conic, slot, arc_slot, spline, style_spline, text (allocation + handle
  resolution + extraction; conic-family derived-point handles carry their
  analytic / numeric chain-rule Jacobians verbatim from sketch_solver.py).
- sketch constraints: coincident, horizontal, vertical, parallel, perpendicular,
  collinear, coradial, concentric, equal (line/slot length + circle/arc radius),
  midpoint, symmetric, angle, point_on (line/circle/arc), fix (whole-entity +
  sub-handle + conic-family derived-handle pin), tangent (line-circle AND
  circle-circle, internal/external branch frozen as-drawn), distance — point-point
  (aligned + axis h/v directional), point-line, AND the C50 `cond` (min/max rim
  with the radius as a solver variable on either ref), radius, diameter (incl.
  slot/arc_slot width mapping w=2r / w=d); SVD-nullspace status coloring. Polar
  (r, θ) placement is expressed by `angle` + `distance`-to-origin (proven by
  `corpora/sketch/ported_polar_point.json` landing a point at (r·cosθ, r·sinθ)).

**Not yet ported** (next increments, driven to parity by the differential harness):
- Conic/spline *constraints* that need numeric closest-point projection:
  point_on on ellipse_arc/parabola/conic, tangent(line-conic / curve-curve /
  line-spline). The entities themselves and their analytic handle Jacobians are
  ported; only these numeric-Jacobian relations remain.
- Expression dims beyond a bare env-variable lookup (full `equations.evaluate`).
- `drag()` (interactive soft-target path), warm-start signatures, `measure_constraint`.

## Methodology

Every analytic Jacobian here is copied from the matching Python helper so the two
stay bit-comparable under the corpus harness. When porting a new constraint:
1. copy the residual + analytic Jacobian from `engine/doc/sketch_solver.py`,
2. add a fixture to the SW-parity corpus (Wave 1b),
3. confirm the differential harness (Wave 1c) shows Python↔C++ parity within tol.
