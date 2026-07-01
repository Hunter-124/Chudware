# Phase 1 Review — tools-scripts-docs track

Branch: `feat/gui-rewrite` · Captured: 2026-06-27  
Track ownership: `scripts/**`, `installer/**`, `tools/**`, `build.bat`, `mcp_setup.bat`,
`config.example.json`, `.mcp.json`, `README.md`, `ARCHITECTURE.md`, `docs/**` (excl. `docs/review/**`, `contracts/**`).

## Fixed

| item | change |
|---|---|
| `docs/TESTING_STANDARDS.md` | Tier-0 table + CI wiring now use `python scripts/check.py`; documented that `--rule` is single-value (no `--rule icons --rule theme`); DoD item 1 references `core/doc/document.cpp` + `core/tests/` instead of deleted `engine/features/` |
| `docs/GUI_TOOLING.md` | Pre-commit/CI section points at `scripts/check.py` + scale ratchet |
| `docs/GUI_SIZING_FIXPLAN.md` | CI wiring note updated to `scripts/check.py` |
| `ARCHITECTURE.md` | Diagram label `HTTP/SSE` → `stdio JSON-RPC` (section header already said `mcp_server/`) |
| `README.md` | `tools/` layout lists real scripts; C++ section points at `ctest` and notes missing `cpp_parity*.py` harnesses |
| `docs/CPP_PERF.md` | Banner: generator absent, numbers are historical snapshot |
| `tools/aicad_drive.py` | Docstring uses `%APPDATA%\AI-CAD\runtime\venv\Scripts\python.exe` instead of machine-specific Claude path |

**Prior session (already in tree):** `scripts/check.py` splits `gui_lint` into separate `--rule icons` and `--rule theme` invocations (fixes the argparse bug where `--rule icons --rule theme` ran only `theme`).

## Flagged

### 1. Missing `tools/cpp_*_parity*` family referenced throughout docs  (FOCUS item)
**Risk:** high — docs cite parity harnesses as proof of the C++ migration strangler
("24/24 geometric parity", "7/7 session parity"), but the harness scripts do not exist in
the tree. A reader/contributor cannot reproduce the claimed parity numbers.

Confirmed missing via glob (`tools/cpp*.py` → 0 files; `tools/*parity*.py` → 0 files):

| referenced tool | referenced in |
|---|---|
| `tools/cpp_parity.py` | `docs/HANDOFF-datum-axis-viewport.md:158`, `docs/CPP_MIGRATION.md:57,98,104,117`, `README.md` status block (still claims 24/24) |
| `tools/cpp_session_parity.py` | `docs/CPP_MIGRATION.md:143` |
| `tools/cpp_perf_bench.py` | `docs/CPP_PERF.md` (banner added; body still references Python engine timings) |
| `tools/cpp_surf_parity.py`, `cpp_validate_parity.py`, `cpp_mesh_parity.py`, `cpp_fea_run_parity.py`, `cpp_sim_parity.py`, `cpp_lbm_parity.py` | `docs/ZERO_PYTHON_CUTOVER.md:116` |
| `tools/cpp_agent_support_parity.py`, `cpp_assist_parity.py`, `cpp_micro_parity.py`, `cpp_orch_*_parity.py` | `docs/CPP_CUTOVER_CHECKLIST.md:96-100` |
| `tools/solver_diff.py`, `tools/mate_diff.py`, `tools/cpp_eval_parity.py` | `docs/CPP_PERF.md:3`, `core/README.md:40`, `corpus/README.md` |
| `tools/render_sketches.py` | `core/docs/dead_code_candidates.md` |

**Current proof substitute:** `ctest --test-dir core/build-occt -C Release` — **36/36 PASS** (baseline).

**Suggested approach:** restore harness scripts from git history, or rewrite `CPP_MIGRATION.md` /
`ZERO_PYTHON_CUTOVER.md` / README status block to cite ctest + `core/tests/` and drop stale
parity counts. → **flag for architecture-doc track.**

### 2. `scripts/parity_audit.py` harvests 0 compilers — gate permanently broken
**File:line:** `scripts/parity_audit.py:45` (`FEATURES_DIR = engine/doc/features`).

**Risk:** high — `engine/doc/features/` no longer exists (Python doc modeling engine deleted;
feature dispatch lives in `core/doc/document.cpp`). The auditor harvests 0 `@register("<type>")`
decorators, so 12 `done`/`beyond` rows report DRIFT (compile_assembly, compile_csys,
compile_sm_*, compile_weld_*, etc.) even though those features exist in C++.

Live run (2026-06-27): **12 DRIFT rows**, WARNING "harvested 0 feature compilers".

**Suggested approach:** rework harvester to read C++ feature types from `core/doc/validate.cpp`
or retire the Python-era gate. Non-trivial design decision → **flag, don't fix** (fix lives in
`scripts/` but needs core track input on evidence sources).

### 3. `scripts/check.py` — 2 of 4 gates fail (pre-existing)
**Live run (2026-06-27):**

| gate | result |
|---|---|
| `gui_lint (icons)` | PASS |
| `gui_lint (theme)` | PASS |
| `feature_surface_audit` | **FAIL** — 31 missing drawing ribbon handlers (View Layout / Annotation) |
| `parity_audit` | **FAIL** — 12 DRIFT (see #2) |

No `.github/workflows/` CI exists; gates are documented for pre-commit / manual use only
(`scripts/hooks/pre-commit` → `scripts/check.py`).

### 4. `README.md` + `ARCHITECTURE.md` — stale "Python worker is production" narrative
**Risk:** medium — README status block (lines 20–26) and architecture diagram still describe
the Python kernel worker as the production document path. `engine/worker.py` comment indicates
the Python doc modeling engine is gone and RPCs run in `libaicad.dll`. README "How it works"
section (lines 30–47) still shows `engine/doc feature compiler` as primary.

**Suggested approach:** architecture-doc reconciliation across README + ARCHITECTURE + CPP_MIGRATION.
Partial fixes applied (# Fixed); full rewrite crosses tracks → **flag.**

### 5. `.mcp.json` committed with machine-specific absolute paths
**File:line:** `.mcp.json:4,8,9` — `C:\Users\dev\...` paths.

**Risk:** medium — regenerated per-machine by `installer/setup_mcp_clients.py`; other contributors
get broken MCP wiring. Should be gitignored; `mcp_setup.bat` is the canonical entry point.

→ **flag for orchestrator** (`.gitignore` change out of lane).

### 6. `mcp_setup.bat` vs `install.ps1` MCP registration coverage
**Risk:** low — `install.ps1:42` calls `register_mcp.py` (Claude Desktop + Claude Code only);
`mcp_setup.bat:78` calls `setup_mcp_clients.py` (5 clients). README presents `mcp_setup.bat`
as canonical for multi-client wiring. Intentional for end-user installer, but undocumented.

### 7. `mcp_setup.bat :try_python` gates on `build123d`
**File:line:** `mcp_setup.bat:102` (`import mcp, build123d`).

**Risk:** low — still needed for legacy `agent_design` path in `engine/worker.py`; misleading
"no Python found" if only build123d is broken. → **flag.**

### 8. No committed CI workflow
**Risk:** low — `docs/TESTING_STANDARDS.md` describes CI wiring but there is no
`.github/workflows/` (or equivalent). Gates rely on manual / pre-commit hook only.

### 9. `scripts/hooks/pre-commit` is `#!/bin/sh` on a Windows-native repo
**Risk:** very low — works under Git for Windows; noted for completeness.

### 10. `config.example.json` — looks current
Provider keys, llamacpp block, experimental vision/CAD-sequence flags align with README and
`docs/EXPERIMENTAL_AI.md` references. No issues found.

### 11. `build.bat` + `installer/build_release.py` — packaging looks sound
`build.bat` handles host compile, dist sync, core DLL staging with fast-fail robocopy on locks.
`build_release.py` stages app packages, optional wheelhouse/python-embed, install scripts.
`--skip-wheels` / `--with-python` / `--zip` flags documented in module docstring.

**Note:** `build_release.py` ignores `test_*` when copying app packages (line 26) — intentional
for release payload size.

## Tests added

None (doc-only fixes in this pass).

## Files touched

- `docs/review/tools-scripts-docs.md` (this report)
- `docs/TESTING_STANDARDS.md`
- `docs/GUI_TOOLING.md`
- `docs/GUI_SIZING_FIXPLAN.md`
- `ARCHITECTURE.md`
- `README.md`
- `docs/CPP_PERF.md`
- `tools/aicad_drive.py`

## Cross-track fixes needed

| track | item |
|---|---|
| **scripts (in-lane)** | Rework `parity_audit.py` harvester for C++ feature surface (#2) |
| **host track** | 31 drawing ribbon commands need handlers (`feature_surface_audit` failures) |
| **architecture docs** | Reconcile README/ARCHITECTURE/CPP_MIGRATION production-path narrative (#1, #4) |
| **orchestrator** | `.mcp.json` gitignore (#5); optional GitHub Actions workflow for `scripts/check.py` (#8) |
