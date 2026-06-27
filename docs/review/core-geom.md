# Phase 1 Review — core-geom track

Branch: `feat/gui-rewrite`  
Scope: `core/geom/**`, `core/sketch/**`, `core/solver/**`, `core/mate/**`, `core/mesh/**`, matching `core/tests/*`

## Fixed

| # | File | Issue | Change |
|---|------|-------|--------|
| 1 | `core/geom/profile.cpp` | Arc builder used `M_PI`, which MSVC omits unless `_USE_MATH_DEFINES` is set before `<cmath>` (latent portability bug; `plane.cpp` and `hole.cpp` already avoid this). | Added local `kPi` constant and use it for degree→radian conversion in the arc edge builder. |
| 2 | `core/sketch/sketch.cpp` | Stale `BUG-1:` comment on a fixed numerical guard in circle–circle tangent Jacobian (reads like an open defect). | Renamed comment to “Numerical guard:” — behavior unchanged. |

**Fix count: 2**

## Flagged

| # | Area | Severity | Notes |
|---|------|----------|-------|
| 1 | `core/mesh/` | Ownership gap | Directory does not exist. Tet/Netgen meshing lives in `core/sim/mesher.cpp`; `core/tests/test_netgen_mesh.cpp` is sim-track, not core-geom. |
| 2 | `core/geom/airfoil.cpp` | Test gap | NACA 4/5-digit and `.dat` import have no C++ unit tests in this track. |
| 3 | `core/sketch/sketch.cpp` | Design / parity | Line–line tangent intentionally deferred (ill-defined). Documented in `sketch.h`; no C++ impl. |
| 4 | `core/geom/weldment.cpp` | Product gap | `weld_end_cap_plate` returns error for overhang end caps (“not supported in C++ port”). |
| 5 | `core/solver/nsolve.cpp` | Performance | Every LM step uses Jacobi SVD minimum-norm solve on `JᵀJ` (intentional Python parity). Acceptable for sketch/mate sizes (n ≲ 100) but not for larger systems. |
| 6 | `core/mate/mate.cpp` | Performance | `classify_mates` stacks Jacobians and runs SVD rank per mate — O(m²) SVD work for long mate lists. |
| 7 | `core/sketch/sketch.cpp` | Numerical | Conic/spline `point_on` and tangent paths use numeric scan + finite-difference Jacobians; step sizes are pinned for Python parity but remain sensitive at tight tolerances. |
| 8 | Mate corpus harness | Cross-track | `tools/mate_diff.py` referenced in comments but not present in repo snapshot; parity validation depends on Phase 2 / external harness. |
| 9 | Sketch corpus | Test gap | No `core/sketch/corpus/` fixtures; sketch parity relies on inline tests + Python differential harness. |
| 10 | `core/mate/mate.cpp` | API surface | `MateSolveResult` has no `budget_exceeded` field (nsolve supports it; mate JSON bridge omits it). Low impact unless drag UX needs the flag. |

**Flag count: 10**

## Tests added

| File | Purpose | CMakeLists |
|------|---------|------------|
| `core/tests/test_mate_corpus.cpp` | Loads all 32 `core/mate/corpus/*.json` fixtures through `solve_mates_json`; schema smoke + spot checks (peg-in-hole DOF, chain convergence, over-defined/redundant, suppressed). | **Not wired** — add beside existing `test_mate` target in `core/CMakeLists.txt`. |
| `core/tests/test_sketch.cpp` | New case: circle–circle tangent with coincident initial centers (internal branch + Jacobian fallback). | Already in `test_sketch` target. |

## Files touched

- `core/geom/profile.cpp` — MSVC-safe π constant for arc angles
- `core/sketch/sketch.cpp` — stale comment cleanup (circle–circle tangent guard)
- `core/tests/test_sketch.cpp` — coincident-center tangent regression test
- `core/tests/test_mate_corpus.cpp` — **new** mate corpus smoke suite
- `docs/review/core-geom.md` — this report

## Review summary

Reviewed ~110 source files across geom (35), sketch (3), solver (2), mate (3 + 32 corpus JSON), and 12 matching test files. No resource leaks, broken imports, or missing declared implementations found in owned paths. Numerical guards (division-by-zero, degenerate Jacobians, OCCT null-shape fallbacks) are consistently applied. Solver and mate/sketch ports mirror the Python oracle structure; primary risk areas are numeric projection Jacobians and SVD cost at scale — flagged, not changed.

**Totals: 2 fixes, 10 flags, 2 test additions (1 needs CMake wiring), 5 files touched**
