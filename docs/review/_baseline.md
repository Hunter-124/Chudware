# Phase 0 Baseline — Parallel Full Codebase Review

Captured: 2026-06-27  
Branch: `feat/gui-rewrite` (up to date with `origin/feat/gui-rewrite`)

## Git snapshot (pre-review)

### Modified (unstaged)
- `core/api/libaicad.cpp`
- `core/doc/picks.cpp`
- `core/doc/selectors.cpp`
- `core/doc/selectors.h`
- `core/doc/session.cpp`
- `core/doc/session.h`
- `core/tests/test_selectors.cpp`
- `docs/contracts/selectors.md`
- `mcp_server/cad_tools.py`
- `mcp_server/server.py`

### Untracked
- `docs/GEOMETRY_LLM_REPRESENTATION_REVIEW.md`
- `docs/INTAKE_MEASUREMENT_DFM_PLAN.md`
- `engine/dfm/` (entire directory)
- `engine/intake/` (entire directory)
- `mcp_server/gsl_tools.py`

### Diff stat (10 files, +393 / -16)
```
 core/api/libaicad.cpp         target/core/doc/picks.cpp
 core/doc/selectors.cpp
 core/doc/selectors.h
 core/doc/session.cpp
 core/doc/session.h
 core/tests/test_selectors.cpp
 docs/contracts/selectors.md
 mcp_server/cad_tools.py
 mcp_server/server.py
 10 files changed, 393 insertions(+), 16 deletions(-)
```

## Python tests

Venv: `%APPDATA%\AI-CAD\runtime\venv\Scripts\python.exe` (present)  
Note: `pytest` module not installed; ran via `python -m <module>`.

| Module | Result |
|--------|--------|
| `engine.ecad.test_ops` | PASS |
| `engine.sim.test_spice` | PASS |
| `engine.dfm.test_process_profiles` | PASS (42/42) |
| `mcp_server.test_roundtrip` | PASS |
| `agent.test_loop_mock` | PASS |

## scripts/check.py

Exit code: **1** (3 of 3 gates FAILED — pre-existing)

| Gate | Result | Notes |
|------|--------|-------|
| `gui_lint` | FAIL | 295 findings (22 scale, 273 scale-ws) — raw px literals not multiplied by uiScale |
| `feature_surface_audit` | FAIL | 31 missing visible ribbon command handlers (drawing/view layout/annotation) |
| `parity_audit` | FAIL | 12 DRIFT rows (done/beyond claims missing compiler artifacts); 57% total parity; WARNING: 0 feature compilers harvested from `engine/doc/features` |

## C++ tests (ctest)

Build dir: `core/build-occt` (exists)

```
ctest --test-dir core/build-occt -C Release --output-on-failure
```

**Result: 33/33 PASSED, 0 failed** (Total time ~13s)

Tests: sim, modal, tet10, nsolve, prof, sketch, drag, plane, expr, agent, aicm, parts, occt_smoke, fatigue, netgen_mesh, overlay_surface, geom_aicm, profile_extrude, ops, sweep, profile_arc, import_roundtrip, export_assembly, document, studies, drawing_gdt, pattern, mirror, hole, selectors, picks, probe, mate

## Known issues to not blame subagents for

1. All three `scripts/check.py` gates fail (gui scale lint, ribbon handlers, parity drift).
2. `pytest` not in managed venv (tests run via `python -m` instead).
3. Pre-existing uncommitted WIP on selectors/session/picks, MCP cad_tools, new engine/dfm and engine/intake dirs.
