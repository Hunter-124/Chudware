# Phase 1 Review — core-geom track

Branch: `master` (review snapshot)  
Scope: `core/geom/**`, `core/sketch/**`, `core/solver/**`, `core/mate/**`, `core/mesh/**`, matching `core/tests/*`

## Fixed

| # | File | Issue | Change |
|---|------|-------|--------|
| 1 | `core/geom/profile.cpp` | Arc builder used `M_PI`, which MSVC omits unless `_USE_MATH_DEFINES` is set before `<cmath>` (latent portability bug; `plane.cpp` and `hole.cpp` already avoid this). | Added local `kPi` constant and use it for degree→radian conversion in the arc edge builder. |
| 2 | `core/sketch/sketch.cpp` | Stale `BUG-1:` comment on a fixed numerical guard in circle–circle tangent Jacobian (reads like an open defect). | Renamed comment to “Numerical guard:” — behavior unchanged. |
| 3 | `core/geom/hole.cpp` | `make_hole_tool` accepted `diameter ≤ 0` or `depth ≤ 0`, passing invalid args to `BRepPrimAPI_MakeCylinder` instead of the null-shape failure contract used elsewhere in geom. | Early return `TopoDS_Shape()` when shaft diameter or depth is not positive. |
| 4 | `core/geom/curves.cpp` | `make_helix` divided by `ndy` (zero pitch) and `cos(cone_angle)` (e.g. 90° taper), producing non-finite arc length / invalid OCCT wire. | Early return empty wire for non-positive height/radius, zero pitch, or `|cos(cone_angle)| < 1e-12`. |

**Fix count: 4**

## Flagged

| # | Area | Severity | Notes |
|---|------|----------|-------|
| 1 | `core/mesh/` | Ownership gap | Directory does not exist. Tet/Netgen meshing lives in `core/sim/mesher.cpp`; `core/tests/test_netgen_mesh.cpp` is sim-track, not core-geom. |
| 2 | `core/geom/airfoil.cpp` | Test gap | NACA 4/5-digit and `.dat` import have no C++ unit tests in this track. |
| 3 | `core/sketch/sketch.cpp` | Design / parity | Line–line tangent intentionally deferred (ill-defined). Documented in `sketch.h`; no C++ impl. |
| 4 | `core/geom/profile.cpp` | Product gap | Region nesting uses one-level even–odd (outer + direct holes only). Islands inside holes (depth ≥ 2) are not emitted as faces. |
| 5 | `core/geom/weldment.cpp` | Product gap | `weld_end_cap_plate` returns error for overhang end caps (“not supported in C++ port”). |
| 6 | `core/solver/nsolve.cpp` | Performance | Every LM step uses Jacobi SVD minimum-norm solve on `JᵀJ` (intentional Python parity). Acceptable for sketch/mate sizes (n ≲ 100) but not for larger systems. |
| 7 | `core/mate/mate.cpp` | Performance | `classify_mates` stacks Jacobians and runs SVD rank per mate — O(m²) SVD work for long mate lists. |
| 8 | `core/sketch/sketch.cpp` | Numerical | Conic/spline `point_on` and tangent paths use numeric scan + finite-difference Jacobians; step sizes are pinned for Python parity but remain sensitive at tight tolerances. |
| 9 | Mate corpus harness | Cross-track | `tools/mate_diff.py` referenced in comments but not present in repo snapshot; parity validation depends on Phase 2 / external harness. |
| 10 | Sketch corpus | Test gap | No `core/sketch/corpus/` fixtures; sketch parity relies on inline tests + Python differential harness. |
| 11 | `core/mate/mate.cpp` | API surface | `MateSolveResult` / `solve_mates_json` omit `budget_exceeded` (nsolve supports it; mate JSON bridge does not expose it). Low impact unless drag UX needs the flag. |

**Flag count: 11**

## Tests added

| File | Purpose | CMakeLists |
|------|---------|------------|
| `core/tests/test_mate_corpus.cpp` | Loads all 32 `core/mate/corpus/*.json` fixtures through `solve_mates_json`; schema smoke + spot checks (peg-in-hole DOF, chain convergence, over-defined/redundant, suppressed). | Wired (`mate_corpus` target in `core/CMakeLists.txt`). |
| `core/tests/test_sketch.cpp` | Circle–circle tangent with coincident initial centers (internal branch + Jacobian fallback). | Already in `test_sketch` target. |
| `core/tests/test_hole.cpp` | `make_hole_tool` returns null shape for non-positive diameter/depth. | Already in `test_hole` target. |
| `core/tests/test_sweep.cpp` | `make_helix` returns null wire for zero pitch, non-positive height/radius, and 90° cone angle. | Already in `test_sweep` target. |

## Files touched

- `core/geom/profile.cpp` — MSVC-safe π constant for arc angles
- `core/geom/hole.cpp` — reject non-positive shaft dims
- `core/geom/curves.cpp` — helix degenerate-parameter guards
- `core/sketch/sketch.cpp` — stale comment cleanup (circle–circle tangent guard)
- `core/tests/test_sketch.cpp` — coincident-center tangent regression test
- `core/tests/test_mate_corpus.cpp` — mate corpus smoke suite
- `core/tests/test_hole.cpp` — hole tool degenerate-dim test
- `core/tests/test_sweep.cpp` — helix degenerate-parameter test
- `docs/review/core-geom.md` — this report

## Review summary

Reviewed ~110 source files across geom (35), sketch (3), solver (2), mate (3 + 32 corpus JSON), and 12 matching test files. No resource leaks, broken imports, or missing declared implementations found in owned paths. OCCT null-shape fallbacks and Eigen division guards are consistently applied; two new early-return guards close latent divide-by-zero / invalid-primitive paths in `make_helix` and `make_hole_tool`. Solver and mate/sketch ports mirror the Python oracle structure; primary residual risks are numeric projection Jacobians, single-level profile nesting, and SVD cost at scale — flagged, not changed.

### Test results (scoped)

```
ctest -C Release -R "nsolve|sketch|mate|plane|profile_arc|geom_aicm|ops|sweep|hole|mate_corpus"
→ 10/10 PASSED (after rebuild of hole + sweep targets)
```

**Totals: 4 fixes, 11 flags, 4 test additions (all wired), 9 files touched**
