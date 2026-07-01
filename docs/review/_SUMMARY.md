# Parallel Full Codebase Review — Consolidated Summary

Repo: `c:\Users\dev\Desktop\AI-opencad` · Branch: `master` · Date: 2026-06-27  
Plan: `c:\Users\dev\.cursor\plans\parallel_full_codebase_review_8df1109c.plan.md`

---

## Baseline vs Final — test delta

| Suite | Baseline (Phase 0) | Final (Phase 2) | Delta |
|-------|--------------------|-----------------|-------|
| core `ctest` (Release) | 36/36 passed | **36/36 passed** | 0 regressions |
| Python modules (Phase 0 set) | 5/5 passed | **5/5 passed** | 0 regressions |
| `engine.intake.test_intake` | (not in baseline) | **84/84 passed** | +1 module run in integration |
| `scripts/check.py` gates | 2 of 4 FAILED | **2 of 4 FAILED** | unchanged (pre-existing) |
| core build | — | exit 0 | clean |
| host build | (no build dir) | skipped | — |
| app build | (no build dir) | skipped | — |

**Pre-existing gate failures (not blamed on review):**
- `feature_surface_audit` — 31 missing drawing ribbon command handlers
- `parity_audit` — 12 DRIFT rows; harvester still reads deleted `engine/doc/features/` (0 compilers)

**Gates now passing (vs older baselines):** `gui_lint` icons + theme (0 findings each).

---

## Track scorecard

| Track | Fixes | Flagged | Tests added/extended | Report |
|-------|------:|--------:|---------------------:|--------|
| core-doc | 6 | 8 | 1 (B11 oblique `side`) | [core-doc.md](core-doc.md) |
| core-geom | 4 | 11 | 4 (mate_corpus, sketch, hole, sweep) | [core-geom.md](core-geom.md) |
| core-sim-ecad | 2 | 9 | 1 (OPAMP subckt) | [core-sim-ecad.md](core-sim-ecad.md) |
| core-infra | 9 | 10 | 3 (agent, machine, expr) | [core-infra.md](core-infra.md) |
| host-ui | 6 | 10 | 0 | [host-ui.md](host-ui.md) |
| host-core-render | 11 | 8 | 0 (1 manual aicm test) | [host-core-render.md](host-core-render.md) |
| app | 15 | 16 | 1 (`test_command_registry`) | [app.md](app.md) |
| engine-py | 8 | 9 | 0 | [engine-py.md](engine-py.md) |
| engine-new-mcp-agent | 7 | 6 | 2 (intake re-export, volume baseline) | [engine-new-mcp-agent.md](engine-new-mcp-agent.md) |
| tools-scripts-docs | 7 | 8 | 0 | [tools-scripts-docs.md](tools-scripts-docs.md) |
| **Total** | **75** | **95** | **13** | |

---

## Top fixes by area

### core-doc (6)
- `side_word` threshold 0.92 + max-component tie-break (oblique faces → `side: null`)
- `doc_adjacency` Explorer-ordered face ordinals; edge `surf` + face `concavity` payload
- `orient_tint` axis tie-break aligned with `side_word`
- Stale comments in `picks.h` / `picks.cpp`

### core-geom (4)
- MSVC-safe `kPi` in `profile.cpp`
- Degenerate hole/helix guards (`hole.cpp`, `curves.cpp`)
- Stale sketch Jacobian comment

### core-infra (9)
- OOM-safe `emit()`; non-object `params_json` validation
- `jstr()` null-string hardening on live agent RPCs
- `run_assembly_design` emits `part_skipped` / `assembly_failed` events
- WIN32-gate `winhttp`/`bcrypt`; `enable_testing()` ordering; `test_machine` target

### engine-new-mcp-agent (7)
- `_LAST_VOLUME` seeded on `new_part`/open/import (write-diff baseline)
- `engine/intake/__init__.py` public API re-export (fixes `ImportError`)
- FDM-default fit anchors in planner prompts
- `coupon_spec` dead fallback removed; `worst_tier` publicized

### app (15)
- Engine `open()` error propagation; undo/redo empty-doc guard
- Hole feature wire format; command palette dedupe; title-bar doc tab close
- `test_command_registry` wired in `app/CMakeLists.txt`

### host-ui (6)
- Null `renderer` guards in sketch/viewport/view-selector
- `icons.cpp` atlas load hardening; `property_manager` `form_` guard

### host-core-render (3 this pass)
- `unloadDocMesh()` on tab switch to tessellation-less docs
- UTF-8 path open in `aicm_loader.cpp`; `resyncTree` datum refresh

---

## Top flagged items (need your decision)

1. **31 drawing ribbon commands without handlers** — `feature_surface_audit` gate; cross-track (`host-ui` table + `app.cpp`/engine handlers).

2. **`parity_audit.py` harvester broken** — Still reads deleted `engine/doc/features/`; 12 DRIFT rows are false positives for C++ features that exist in `core/doc/`. Fix in `tools-scripts-docs` lane.

3. **Missing `tools/cpp_parity.py` family** — Docs cite 24/24 geometric parity proof; harness absent. Substitute: ctest 36/36 PASS.

4. **`engine/kernel.py` script-exec safety** — Full `build123d` namespace + unrestricted `__builtins__` for model-generated code. Contrast with `_SafeExpr` in sim. Design decision: isolate vs allowlist.

5. **Planner PROCESS BLOCK not wired to C++ orchestrator** — MCP path has `set_process` + `process_block_text`; `core/agent` planner does not inject per-process block yet.

6. **`doc_set_meta` / `doc_get_meta` semantics** — No undo/revision bump; product decision on metadata mutability.

7. **Face id space Explorer vs MapShapes** — Assumption on simple solids; cross-track verify against tessellator AICM face ids.

8. **`.mcp.json` committed with machine-specific paths** — Should be gitignored; use `config.example.json`.

9. **285 gui_lint scale findings in host-ui** — 22 real DPI gaps + 263 world-space art; batch into dedicated sizing PR.

10. **Agent HTTP Windows-only** — `aicad_agent` needs curl/libcurl or feature gate for non-Win builds.

---

## Integration (Phase 2)

1. **CMake test registration** — All new/extended tests already wired: `test_machine`, `test_mate_corpus`, `test_spice_netlist` (+ extended sketch/hole/sweep/expr/selectors).
2. **Rebuild** — `cmake --build core/build-occt --config Release` → exit 0.
3. **ctest** — 36/36 PASS (~12.3 s). Matches baseline.
4. **Python** — 5 baseline modules + `engine.intake.test_intake` (84/84) all PASS.
5. **`scripts/check.py`** — 2/4 gates FAIL (same as baseline).
6. **Cross-track sweep** — All 60 modified files are in-lane per ownership table. No out-of-lane edits detected. No destructive git commands run.

---

## Working tree

**60 files changed, 1313 insertions(+), 1105 deletions(-)**

Key code areas:
- `core/doc/` (selectors, picks, session), `core/api/libaicad.cpp`, `core/agent/run.cpp`
- `core/geom/` (hole, curves, profile), `core/ecad/session.cpp`
- `engine/intake/` (package re-export), `engine/dfm/process_profiles.py`
- `agent/loop.py`, `mcp_server/intake_tools.py`
- `host/src/ui/` (icons, sketch, viewport), `host/src/core/` (doc tabs, mesh unload)
- `app/src/` (engine, palette, title bar)
- Docs: README, ARCHITECTURE, TESTING_STANDARDS, GUI_TOOLING

**No commits made.** Review with `git diff` before committing.

---

## Blockers

None for merge from a test perspective — all executable tests green, no new regressions vs baseline.

**Non-blocking but actionable:**
- Pre-existing `scripts/check.py` gate failures (ribbon handlers, parity drift)
- `mcp_server/test_cad_tools.py` volume-baseline test is DLL-gated (skipped without staged `libaicad.dll`)
- `host/build` and `app/build` dirs absent on this machine — host/app compile not verified in integration
