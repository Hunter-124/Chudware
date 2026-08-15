# build-test — CI gate, CMake, and test-fixture layer

Scope: scripts/ (gate.sh, check.py, feature_surface_audit.py, gui_lint.py, parity_audit.py, scripts/hooks/), CMakeLists.txt, CMakePresets.json, corpora/
Contracts: none (build-test is purely infrastructure; test contracts are implicit in core/CMakeLists.txt enable_testing + ctest and the services pytest sweep)

## Entry points
- scripts/gate.sh — Linux build+test gate: checks, core build, host GUI build, WASM smoke, ctest, service pytest sweep, and host screenshot smoke
- scripts/gate.sh `stage_check()` — runs scripts/check.py
- scripts/gate.sh `stage_core_build()` — configures/builds core/ as libchudware with OCCT
- scripts/gate.sh `stage_ui_abstract()` — despite its legacy function name, builds the sole active GUI from host/
- scripts/gate.sh `stage_ctest()` — runs core ctest
- scripts/gate.sh `stage_pytest()` — sweeps test-bearing services subpackages plus tools and training
- scripts/gate.sh `stage_ui_imgui()` — despite its legacy function name, runs the host GUI screenshot smoke
- CMakePresets.json — supported core presets; the GUI gate configures host/ directly
- corpora/README.md:1–14 — test fixtures layout: sketch/ (solver JSON), render/ (SVG outputs), sw_parity/ (SolidWorks parity JSON), sw_fixtures/ (.SLDPRT imports)

## How it connects
gate.sh orchestrates CI across the active stack: source/docs checks, core and host builds, WASM smoke, ctest, service pytest units, and a headless host screenshot. Dependency-gated stages SKIP honestly when their required toolchain is absent.

## Debugging playbook
- Full gate: `bash scripts/gate.sh` (uses core/build-linux as build dir for stages 2–3)
- Just check.py: `python3 scripts/check.py` (runs gui_lint + feature_surface_audit + parity_audit)
- Just core build: `cmake -S core -B core/build-linux -DCHUDWARE_WITH_OCCT=ON && cmake --build core/build-linux --config Release`
- Just ctest: `ctest --test-dir core/build-linux -C Release --output-on-failure` (requires successful core build)
- Just service pytest: `python3 -X utf8 -m pytest services -q` (requires a provisioned runtime venv)
- Just host GUI: `cmake -S host -B host/build-linux -G Ninja -DCMAKE_BUILD_TYPE=Release && ninja -C host/build-linux Chudware`
- Gotcha: check.py is non-fatal (CHECK_PY_FATAL=0, L31) because feature_surface_audit is missing dwg_*/cam_* handlers and parity_audit has SW_PARITY_SPEC drift (docs/REFACTOR_LOG.md); flip to 1 once gaps close to make check.py hard-gating
- Gotcha: OCCT detection is optional; gate stays green on boxes with no system OCCT (L84–88)
- Gotcha: venv is optional; pytest SKIPs if runtime venv absent (L156–169); check linux-build-port memory for provisioning on Linux

## Graphify starters
- graphify query "gate.sh stages check pytest ctest core build"
- graphify explain "stage_core_build"
- graphify path "check.py" "gate.sh"

## Related tasks/docs
- work/tasks: grep `build|gate|cmake|pytest|test` in work/tasks/*.md (build-test infrastructure lives in task notes, not separate task IDs)
- docs/REFACTOR_LOG.md (pre-existing check.py red reasons, build status)
- AGENTS.md §3 (gate semantics, SKIP vs FAIL rules, swarm coordination)
- core/CMakeLists.txt:224–294 (test target definitions: test_sim, test_modal, test_tet10, test_spice_netlist, test_cam, test_nsolve + doctest header provisioning)
- scripts/hooks/pre-commit (dev hook for pre-commit check.py only, no pytest)
