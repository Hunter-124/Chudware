---
id: T-BL20
title: FEA/CFD V&V cross-validation - MacNeal-Harder/NAFEMS cases + CalculiX second opinion
workstream: backlog
owner: sonnet-doro
status: done
branch: task/T-BL20-fea-vv-crossval
depends_on: []
lane: simulation
kind: fix
priority: 3
---

## Context

**Filed 2026-07-03 (Fable advisor, closing T-BL01 Q1).** The T-BL01 V&V harness
validates the analytic solver families (hydraulic, piping, ECAD impedance/circuit,
PyNite frames) but the C++ FEA/CFD backends are documented SKIP — they have analytic
reference values recorded but no executed cross-validation. Core FEA/CFD currently
ships without an independent second opinion; that's a latent correctness risk.

Scope:

1. Wire the C++ `sim_run_static`/modal path into the V&V battery via the full MCP
   server process (the harness already documents why raw core_bridge can't reach it).
2. Add MacNeal-Harder / NAFEMS-style benchmark geometries (clean-room reconstructions,
   never bundling NAFEMS/ASME documents — same fence as T-BL01).
3. `stage_calculix`: run `ccx` as a subprocess (GPL2 → subprocess-only per
   contracts/licenses.md posture) on a minimal cantilever .inp and compare against
   Roark and against our FEA result — the "second structural opinion" from the
   original T-BL01 context.

## Definition of done

- [x] FEA static + modal cases execute (not SKIP) in the V&V battery via MCP wiring
- [x] At least 3 MacNeal-Harder/NAFEMS-style cases with tolerance-tiered error tables
- [x] CalculiX subprocess stage cross-checks at least the cantilever case; licenses.md
      row if any new dep is added
- [x] gate.sh stage 4.5 covers the new cases; report lands in reports/vv/

## AI hints

- `work/questions/T-BL01.md` — the original scope question and what was deferred
- `reports/vv/`, gate.sh stage 4.5 — existing battery structure to extend
- Engine doc-load & verify contract: doc_new + eq_set + add_feature; CoreError vs
  feature_status for rebuild failure detection

## Progress log
- 2026-07-03 22:35 UTC [sonnet-doro] (foreman liveness probe) No branch pushed ~2h after claim. If you are alive: push your worktree branch NOW (even WIP) — work only counts on the remote. If nothing lands by the next foreman sweep the claim gets released per the dead-driver pattern.
- 2026-07-03 22:38 UTC [sonnet-doro] Landed on task/T-BL20-fea-vv-crossval (pushed). 5 new executed MacNeal-Harder-style FEA/modal cases (all PRECISE, <0.5% err vs live OCCT build) + CalculiX second-opinion stage (ccx absent in this sandbox -> graceful SKIP, .inp generator smoke-tested). Found + filed a real engine bug in the pressure-load path (core/sim/fea_run.cpp) as issue #9, marked xfail not hidden. Also filed issue #10 for pre-existing services.mcp test failures found during gate verification (confirmed unrelated to this branch). Did not re-run gate.sh's cold OCCT core-build stage in this fresh worktree (no C++ touched, no build cache, ccache disabled -- would run past one foreground turn); check.py + full vv_benchmark.py pytest suite verified directly instead. See HANDOFF section for detail.
- 2026-07-03 [fable-foreman] REVIEWED+MERGED: LGTM. Reference values independently verified (Rao modal, Roark static); tolerances standard-appropriate; CalculiX subprocess+GPL-isolated+gated; FEA3 honestly recorded FAIL (issue #9), not back-fitted — strongest possible validation-honesty signal. 25 tests, scope tight (no C++). Merged to master.

## HANDOFF (sonnet-doro, 2026-07-03)

Delivered in `scripts/vv_benchmark.py`:

- **MCP wiring**: `_mcp_call()` dispatches through the full in-process FastMCP server
  (`services.mcp.server.mcp.call_tool`, same pattern as `test_roundtrip.py` /
  `test_cad_tools.py`), since raw `core_bridge.call("sim_run_static", ...)` can't do
  the face-query -> mesh-point-set resolution `run_fea_static`/`run_fea_modal` do.
- **5 executed FEA/modal cases** (family `fea-static` + `modal`): FEA1 (cantilever
  point load, square section, PRECISE, 0.39% err), FEA2 (rect section strong-axis,
  PRECISE, 0.36% err), FEA3 (uniform pressure load, ENGINEERING — **xfail**, see
  below), M1 (modal cantilever square, PRECISE, 0.22% err), M2 (modal cantilever
  rect weak-axis, PRECISE, 0.49% err). All calibrated against a live OCCT build
  (`core/build-occt-linux/bin/libchudware.so`) before landing.
- **CalculiX** (family `calculix`): `bench_calculix()` + `_write_calculix_cantilever_inp()`
  generate a clean-room C3D8 hex-mesh `.inp` for the FEA1 geometry, run `ccx` as a
  PATH-discovered subprocess (GPL-2 isolation, `contracts/licenses.md` row added),
  parse the `.dat` tip displacement, and cross-check vs Roark + our own FEA. `ccx`
  is **not installed in this sandbox** so the live numeric check couldn't be
  end-to-end verified here — it gracefully SKIPs (same posture as
  ngspice/Renode/QEMU/probe-rs/esptool elsewhere in this repo). The `.inp` generator
  itself IS verified without the binary (`TestSmoke::test_calculix_inp_generator`).
- **Real bug found**: FEA3 (uniform pressure load) surfaced a genuine defect in
  `core/sim/fea_run.cpp`'s pressure-load branch — the resultant force is not
  normalized against its own computed `loaded_area` (unlike the force branch, which
  is), so it drifts with mesh density (~10-95% error, confirmed via an isolated
  axial-pressure-vs-axial-force diagnostic). Filed as
  **[issue #9](https://github.com/Hunter-124/CAD/issues/9)** with root-cause
  analysis; the test is `@pytest.mark.xfail(strict=False)` referencing it (not
  skipped, not tolerance-loosened — it fails for real, pytest just doesn't red the
  suite for a tracked, understood bug). Removing the xfail decorator once #9 lands
  is the acceptance check for that fix.
- **Robustness**: `_missing_fea_capability()` detects `"unknown method"` responses
  (the plain `core/build-linux` .so has no OCCT geometry/FEA — only
  `build-occt-linux` does, same split `gate.sh`'s `stage_modeling` already
  documents) and downgrades those to SKIP instead of a false FAIL, in both the
  `bench_*()` report path and the pytest `TestFull` methods. Verified against both
  `.so` builds.

**Verification performed:**
- `pytest scripts/vv_benchmark.py::TestSmoke -q` — 8 passed (pure Python, no .so).
- `CHUDWARE_RUN_VV=full CHUDWARE_LIBCHUDWARE=<build-occt-linux>/libchudware.so pytest
  scripts/vv_benchmark.py -q` — 17 passed, 1 skipped (ccx absent), 1 xfailed
  (issue #9), 0 unexpected failures.
- Same full run against the plain `build-linux` .so (no OCCT) — all FEA/modal/ccx
  cases gracefully SKIP instead of false-failing.
- `python scripts/check.py` (gate stage 1) — all 4 gates PASS, unaffected.
- `scripts/gate.sh`'s core CMake/OCCT build (stage 2/2b/3) was **not** re-run from
  cold in this worktree — this diff touches no C++ (only `scripts/vv_benchmark.py`,
  `contracts/licenses.md`, this task file), a fresh worktree has no build cache, and
  gate.sh disables ccache (`CCACHE_DISABLE=1`) — a from-scratch OCCT-linked build
  would run well past what fits in one foreground gate-run turn. The integrator
  merging into a warm main checkout (which already has `core/build-linux` +
  `core/build-occt-linux` built) gets a fast incremental gate run instead.
- While verifying stage 4 (`pytest services.mcp`), found 8 pre-existing test failures
  + 34 ECAD collection errors that reproduce identically on an unmodified checkout
  against either `.so` build — confirmed unrelated to this branch. Filed as
  **[issue #10](https://github.com/Hunter-124/CAD/issues/10)** for visibility;
  gate.sh's own policy records these as non-fatal, so nothing here changes gate
  outcome, but a human should look eventually.

`graphify update .` run after landing. Branch pushed, ready for merge.
