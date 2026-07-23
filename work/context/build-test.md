# build-test — CI gate, CMake, and test-fixture layer

Scope: scripts/ (gate.sh, check.py, feature_surface_audit.py, gui_lint.py, parity_audit.py, scripts/hooks/), CMakeLists.txt, CMakePresets.json, corpora/
Contracts: none (build-test is purely infrastructure; test contracts are implicit in core/CMakeLists.txt enable_testing + ctest, engine/doc pytest, and ui/abstract ctest)

## Entry points
- scripts/gate.sh:1 — Linux build+test gate, orchestrates 6 sequential stages (check, core build, ui/abstract, ctest, pytest, ui/imgui smoke)
- scripts/gate.sh:39 `stage_check()` — runs scripts/check.py (gui_lint icons/theme, feature_surface_audit ribbon→handlers, parity_audit spec drift)
- scripts/gate.sh:72 `stage_core_build()` — cmake configure/build core/ as libchudware.so with -DCHUDWARE_WITH_OCCT=ON; detects OCCT via find_occt_config() (L58)
- scripts/gate.sh:129 `stage_ctest()` — runs `ctest --test-dir core/build-linux` (constraint solver, geometry, sim tests; see core/CMakeLists.txt:224–294)
- scripts/gate.sh:154 `stage_pytest()` — runs `pytest engine/doc agent -q` if venv/pytest is available; SKIP if not provisioned
- scripts/gate.sh:183 `stage_ui_imgui()` — cmake preset linux-app, build, smoke test via `--shot` (headless render) + optional `--selftest`
- CMakePresets.json:6,18 — linux-core preset (OCCT ON, Ninja) and linux-app preset (ImGui GUI, ui/abstract ON)
- corpora/README.md:1–14 — test fixtures layout: sketch/ (solver JSON), render/ (SVG outputs), sw_parity/ (SolidWorks parity JSON), sw_fixtures/ (.SLDPRT imports)

## How it connects
gate.sh orchestrates Linux CI across the entire stack: `stage_check` (stdlib-only source/docs gates, ~1s), `stage_core_build` (libchudware with OCCT geometry kernel), `stage_ctest` (doctest-based C++ tests), `stage_pytest` (Python oracle + agent tests), `stage_ui_imgui` (ImGui desktop app build + headless render smoke test). Each stage can SKIP if dependencies are absent (OCCT, cmake, venv) without failing the gate — only hard failures (code errors, missing binaries) fail. corpora/ feeds all solver/render/parity tests: fixture JSON files in sketch/, render outputs validate against expected SVG, sw_parity/ cross-checks Python sketch solver vs SolidWorks, sw_fixtures/ tests SLDPRT import.

## Debugging playbook
- Full gate: `bash scripts/gate.sh` (uses core/build-linux as build dir for stages 2–3)
- Just check.py: `python3 scripts/check.py` (runs gui_lint + feature_surface_audit + parity_audit)
- Just core build: `cmake -S core -B core/build-linux -DCHUDWARE_WITH_OCCT=ON && cmake --build core/build-linux --config Release`
- Just ctest: `ctest --test-dir core/build-linux -C Release --output-on-failure` (requires successful core build)
- Just pytest: `python3 -X utf8 -m pytest engine/doc agent -q` (requires venv at runtime/venv/bin/python or .venv/bin/python)
- Just ui/imgui: `cmake --preset linux-app && cmake --build build/linux-app --config Release`
- Gotcha: check.py is non-fatal (CHECK_PY_FATAL=0, L31) because feature_surface_audit is missing dwg_*/cam_* handlers and parity_audit has SW_PARITY_SPEC drift (docs/REFACTOR_LOG.md); flip to 1 once gaps close to make check.py hard-gating
- Gotcha: OCCT detection is optional; gate stays green on boxes with no system OCCT (L84–88)
- Gotcha: venv is optional; pytest SKIPs if runtime venv absent (L156–169); check linux-build-port memory for provisioning on Linux

## Graphify starters
- graphify query "gate.sh stages check pytest ctest core build"
- graphify explain "stage_core_build"
- graphify path "check.py" "gate.sh"

## Related tasks/docs
- work/tasks: grep `build|gate|cmake|pytest|test` in work/tasks/*.md (build-test infrastructure lives in task notes, not separate task IDs)
- docs/TESTING_STANDARDS.md (standards workflow, pre-commit hook install)
- docs/REFACTOR_LOG.md (pre-existing check.py red reasons, build status)
- AGENTS.md §3 (gate semantics, SKIP vs FAIL rules, swarm coordination)
- core/CMakeLists.txt:224–294 (test target definitions: test_sim, test_modal, test_tet10, test_spice_netlist, test_cam, test_nsolve + doctest header provisioning)
- scripts/hooks/pre-commit (dev hook for pre-commit check.py only, no pytest)
