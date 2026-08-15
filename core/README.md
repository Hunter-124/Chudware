# `core/` — the pure-C++ engine (libchudware)

The pure-C++ engine (`libchudware`) that replaced the former Python engine.
It is linked directly into the C++ host, so document, modeling, solver, and
simulation operations run in process rather than through the retired Python
stdio worker.

## Layout

```
core/
  solver/   nsolve.{h,cpp}   damped-LM constraint solver core
  sketch/   sketch.{h,cpp}   entity parameterization + constraints
  tests/    test_nsolve.cpp  solver-core unit tests (doctest)
            test_sketch.cpp  sketch-solver unit tests
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

## Solver status

The solver and sketch subsystem is implemented in C++ and covered by the
`test_nsolve` and `test_sketch` suites:
- nsolve numeric core: residual/Jacobian registry, damped Levenberg-Marquardt,
  null-space-projected constrained drag, QR-rank DOF / redundant / conflicting
  diagnostics (chronological attribution).
- sketch entities: point, line, circle, arc, ellipse, ellipse_arc, parabola,
  conic, slot, arc_slot, spline, style_spline, text (allocation + handle
  resolution + extraction; conic-family derived-point handles carry their
  analytic / numeric chain-rule Jacobians).
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

Additional sketch coverage continues to land directly in the C++ solver and its
unit and corpus fixtures.

## Methodology

Every analytic Jacobian is covered by the C++ tests and corpus fixtures. When
adding a new constraint:
1. implement the residual and analytic Jacobian in `core/sketch/`,
2. add a fixture to `corpora/sketch/`,
3. add or update the corresponding C++ solver test.
