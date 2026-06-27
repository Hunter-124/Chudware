# Phase 1 Review — core-sim-ecad

Track: **core-sim-ecad** (FEA/SPICE/LBM, ECAD port, parts catalog)
Branch: `master`
Reviewer date: 2026-06-27 (parallel full-codebase review, pass 2)

Scope reviewed: `core/sim/**`, `core/ecad/**`, `core/parts/**` + their tests, with
parity reference to `engine/sim/**` and `engine/ecad/**`.

## Summary

The track remains in strong shape. The SPICE simulator (op/dc/tran/ac/tf/four/noise/step/
temp/mc, nonlinear D/Q/M + Newton, controlled E/G/F/H, mutual L K, behavioral B,
OPAMP/T subckts), the LBM D3Q19 BGK wind-tunnel core, the FEA static/modal/fatigue solvers,
the ECAD session (sch netlist+ERC, board DRC+ratsnest+zonefill, autoroute, sync, fab export,
container I/O), and the parts catalog are faithful ports with explicit parity comments
throughout.

**One real parity bug was found and fixed** in `ecad_simulate`: the AC-convenience path
treated `"ac": null` on a source as “already has AC stimulus”, so it skipped injecting the
unit AC drive that Python adds when no source carries a truthy `params.ac`. Two stale header
comments were also corrected (schema.h ERC/DRC “DEFERRED”; prior pass already fixed spice.h /
netlist.cpp / session.h sim-table docs).

All 36 C++ tests pass (`ctest --test-dir core/build-occt -C Release`), including
`sim`, `modal`, `spice_netlist`, `parts`, `fatigue`, and `ops`.

No CMakeLists or build config was touched. No commits were made.

## Fixed

### Parity bug — `ecad_simulate` AC convenience (`core/ecad/session.cpp`)

Python (`engine/ecad/rpc.py::_ecad_simulate`) checks `d["params"].get("ac")` — a null/missing
`ac` is falsy and triggers the unit-AC injection on the first `V` source. C++ used
`params.contains("ac")`, which is true even when `ac` is JSON null, so a schematic whose
inline sim carried `"ac": null` would run AC with no excitation and silently diverge.

**Fix:** require `contains("ac") && !ac.is_null()` before treating a device as AC-stimulated.

### Stale header comment (`core/ecad/schema.h`)

Header claimed “Connectivity / ERC / DRC are separate passes (DEFERRED to later waves)”.
Those passes are live in `netlist.cpp`, `erc.cpp`, `drc.cpp`, `zonefill.cpp`, and are wired
through `session.cpp::compile_field`. Updated to describe the real split (validate here;
connectivity in sibling modules + compile_field).

### Tests (`core/tests/test_spice_netlist.cpp`)

Added `parse_netlist: X OPAMP subckt expands with Dclamp model` — verifies built-in subckt
expansion and `Dclamp` model merge (the path that only runs when X-instances are present).

## Flagged

Design smells / parity gaps / cross-track concerns — NOT fixed (out of lane or risky):

- **`core/sim/spice.cpp` (~2300 lines)** — single TU for the entire MNA simulator. Hard to
  navigate; high regression risk. Suggested split: `spice_setup.cpp`, `spice_stamps.cpp`,
  `spice_solve.cpp` + thin dispatcher. Pure refactor; no public-API change.
- **`core/sim/spice.cpp` `_SafeExpr` behavioral evaluator** — user-authored `V=`/`I=` expressions
  with manual Jacobian. Security-/correctness-sensitive; needs dedicated differential tests
  (divide-by-zero, nested `I(`, `limit()`). Flag — large + sensitive.
- **`core/sim/lbm.cpp`** — manual float32 pairwise reduction order for numpy bit-parity.
  Fragile under `-ffast-math` or compiler reordering. Add a prominent comment/`static_assert`
  guard at the TU top. Flag — parity-sensitive, not a bug.
- **`core/sim/numpy_rng.{h,cpp}`** — vendored numpy SeedSequence + PCG64 + ziggurat. Keep BSD
  notice prominent; pin the numpy version the tables were harvested from. Flag — license drift.
- **`core/ecad/session.cpp`** — global `g_sessions` + owner-tracked `g_mtx` for Windows SEH
  crash recovery. Duplicates `core/doc/session.cpp`. Factor into shared `core/util/crash_lock.h`.
  Cross-track — flag, don't fix.
- **`core/ecad/container.cpp` `ecad_container_open`** — manual `mz_zip_reader_end` on every
  error path. Wrap `mz_zip_archive` in RAII. Flag — mechanical C-style resource.
- **`core/parts/generate.cpp` `parse_leading_double`** — `strtod` with null endptr; non-numeric
  `dowel_pin` size silently becomes 0.0 then fails `d <= 0.0` with a generic “unknown size”.
  Tighten error message. Flag — minor UX.
- **`core/sim/flow_run.cpp:200`** — progress JSON uses truncate-in-place (Windows `rename`
  won't replace). Torn reads possible; comment accepts this. Flag — product decision.
- **ECAD RPC parity gaps vs `engine/ecad/rpc.py`** — C++ session exposes the core doc lifecycle
  and compute paths but lacks several Python RPC handlers:
  - `ecad_resolve_pick` — pick disambiguation
  - `ecad_to_3d` — board→STEP (lives in `engine/ecad/threed.py`; host may still route to Python)
  - `ecad_design` — LLM orchestrator entry (intentionally agent-side)
  - `engine/ecad/autosave.py` — no C++ autosave module
  - `ecad_export` kinds `spice`, `netlist`, `kicad` — C++ only supports `gerber|drill|bom|cpl`
    (returns in-memory `files` map vs Python's path-based write); `write_spice` not ported to C++
  - `ecad_route_diff_pair` — C++ session adds this; Python worker has no matching RPC (host-only)
- **No dedicated ECAD unit tests in `core/tests/`** — netlist/ERC/DRC/autoroute/session are
  gated only by `tools/cpp_ecad_parity.py` harness, not doctest. Suggested: add
  `test_ecad_netlist.cpp` / `test_ecad_ops.cpp` with a few closed-form sch/pcb fixtures.
- **Cross-track:** `host/src/core/sim_client.cpp` — audit dispatch against full `ecad_*` API in
  `session.h` (especially `ecad_simulate`, `ecad_route_diff_pair`, `ecad_fill_zones`). Host track.

## Tests

| Test | Scope | Result |
|------|-------|--------|
| `sim` | FEA static + LBM ref_length | PASS |
| `modal` | Modal solver | PASS |
| `spice_netlist` | parse_netlist + netlist_to_spice_circuit | PASS (4 cases) |
| `parts` | Catalog + build() dims | PASS |
| `fatigue` | Fatigue solver | PASS |
| `ops` | Geom boolean ops (not ECAD) | PASS |

Full suite: **36/36 PASS** (~13 s).

## Files touched

Code:
- `core/ecad/session.cpp` — AC null parity fix
- `core/ecad/schema.h` — stale DEFERRED comment
- `core/tests/test_spice_netlist.cpp` — OPAMP subckt expansion test

Docs:
- `docs/review/core-sim-ecad.md` (this file)

No CMakeLists, build config, or non-track source files were modified.
