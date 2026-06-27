# Phase 1 Review — core-sim-ecad

Track: **core-sim-ecad** (FEA/SPICE/LBM, ECAD port, parts catalog)
Branch: `feat/gui-rewrite`
Reviewer date: 2026-06-27

Scope reviewed: `core/sim/**`, `core/ecad/**`, `core/parts/**` + their tests, with
parity reference to `engine/sim/**` and `engine/ecad/**`.

## Summary

The track is in strong shape — the SPICE simulator (op/dc/tran/ac/tf/four/noise/step/
temp/mc, nonlinear D/Q/M + Newton, controlled E/G/F/H, mutual L K, behavioral B,
OPAMP/T subckts), the LBM D3Q19 BGK wind-tunnel core (bit-exact float32 parity with
numpy's pairwise reduction order), the FEA static/modal/fatigue solvers, the ECAD
session (sch netlist+ERC, board DRC+ratsnest+zonefill, autoroute, sync, fab export,
container I/O), and the parts catalog are all faithful ports with explicit parity
comments throughout. No bugs, null errors, resource leaks, or broken imports were
found in the in-lane files. The only safe issues were **stale header comments that
claimed features were "DEFERRED" when the implementations are in fact present** —
these are fixed below. The cross-cutting parity gaps (K/B/subckt in spice.h, the
SYMBOLS-library fallback claim in netlist.cpp/session.h) were the highest-value
documentation drift because they actively mislead future readers about what the C++
core can do.

No CMakeLists or build config was touched. No integration builds/tests were run
(Phase 2).

## Fixed

Stale-comment corrections (documentation drift only; no behavior change):

- `core/sim/spice.h:4-9` — header claimed "FIRST increment: linear DC only …
  Nonlinear devices / transient / AC / netlist parser deferred." The simulator now
  implements all of those (D/Q/M + Newton, tran, AC, .dc sweep, tf/four/noise/step/
  temp/mc, K/B/OPAMP/T). Replaced with an accurate capability summary.
- `core/sim/spice.h:14-17` — `spice_simulate` doc said it handles only the "op"
  analysis and others report `convergence:false`. False since P27-P33. Trimmed to
  the real contract (never throws; sets convergence.<type>=false on failure).
- `core/sim/spice.cpp:10` — opening comment repeated the "K/B/subckt/parser
  deferred" claim. Corrected: K and B are implemented in this file; deck parsing
  and subckt expansion live in `sim/netlist.cpp`.
- `core/sim/netlist.cpp:868-872` — `netlist_to_spice_circuit` comment said the
  starter SYMBOLS-library sim fallback is "intentionally NOT reproduced" and such
  components are skipped. The function DOES reproduce it via `symbol_sim_table()`
  (R/C/C_pol/L/D/LED/Zener/Q_NPN/Q_PNP/NMOS/PMOS/OPAMP_dual/NE555/power/V/I/Switch).
  Rewrote to describe the real precedence (inline `sim` > libid table > skip warning).
- `core/ecad/session.h:7-11` — claimed the `compile` field (netlist/ERC/DRC/
  ratsnest) is "DEFERRED to a later wave" and returns an empty skeleton. The
  `compile_field` in `session.cpp` is live: sch -> `build_netlist` + `run_erc`;
  board -> `fill_zones_into` + `run_drc` + `ratsnest`. Updated to match.
- `core/ecad/session.h:117-122` — `ecad_simulate` PARITY SCOPE note said a
  non-empty circuit "requires each component to carry an inline sim mapping" and
  that the library fallback is not ported. Incorrect — `netlist_to_spice_circuit`
  resolves libids via `symbol_sim_table`. Reworded so callers don't avoid the C++
  path expecting it to fail on starter-library symbols.

## Flagged

Design smells / larger refactors / cross-track concerns — NOT fixed (out of lane
or risky):

- `core/sim/spice.cpp` (~2300 lines) — single TU holds the entire MNA simulator
  (DC/tran/AC stamps, Newton + stepping, behavioral expr evaluator, MC/ziggurat
  plumbing, probe evaluation, step/sweep orchestration). **Risk**: hard to navigate,
  high cognitive load, easy to introduce parity regressions. **Suggested approach**:
  split into `spice_setup.cpp` (node/branch indexing + Setup), `spice_stamps.cpp`
  (per-device stamps + behavioral companion), `spice_solve.cpp` (Newton/tran/AC
  drivers), keeping `spice.cpp` as the thin `spice_simulate` dispatcher. Pure
  refactor; no public-API change. Flag, don't fix (large mechanical refactor).
- `core/sim/spice.cpp` lines ~617-810 (`_SafeExpr` behavioral evaluator) — a
  from-scratch recursive-descent expression parser with manual Jacobian
  differentiation. **Risk**: security-/correctness-sensitive (evaluates arbitrary
  user-authored `V=/I=` expressions); divergence from `engine/sim/devices.py`
  semantics would silently corrupt nonlinear tran results. **Suggested approach**:
  extract the evaluator into its own TU + add a dedicated differential test
  (edge cases: `V=` with nested `I(`, divide-by-zero, `limit()`). Flag —
  security-sensitive and large.
- `core/sim/lbm.cpp` — D3Q19 kernel replicates numpy's exact float32 pairwise
  reduction order by hand (`pairwise_sum19` / `pairwise_sum_f32`). **Risk**: any
  compiler reordering or fast-math flag would silently break bit-parity with the
  Python oracle; the manual unroll is fragile. **Suggested approach**: leave as-is
  for the parity gate, but add a `static_assert`/comment block at the top warning
  that `-ffast-math` / `__restrict` reordering is forbidden here. Flag — parity-
  sensitive, not a bug.
- `core/sim/numpy_rng.{h,cpp}` — vendored bit-exact clone of numpy's
  SeedSequence + PCG64 + ziggurat `standard_normal`. **Risk**: license/attribution
  (numpy BSD) and version drift if numpy's RNG tables change in a future major.
  **Suggested approach**: keep the BSD notice already in the header prominent, and
  pin the numpy version the tables were harvested from in a comment. Flag —
  license-sensitive.
- `core/ecad/session.cpp` — global `g_sessions` map + `g_mtx` with hand-rolled
  owner-tracking (`g_mtx_held`/`g_mtx_owner`) for Windows SEH crash recovery
  (`force_release_ecad_lock`). **Risk**: the recovery path is Windows-specific and
  duplicates the same pattern in `core/doc/session.cpp`; a deadlock in one registry
  can stall the whole ECAD subsystem. **Suggested approach**: factor the
  lock+owner-tracking into a shared `core/util/crash_lock.h` used by both doc and
  ecad. Cross-track (touches `core/doc` and `core/util`) — flag, don't fix.
- `core/ecad/container.cpp` `ecad_container_open` — multiple early-return paths
  each call `mz_zip_reader_end(&zip)` before `io_error`. **Risk**: easy to leak the
  zip handle if a new return path is added without the cleanup. **Suggested
  approach**: wrap `mz_zip_archive` in an RAII guard whose destructor calls
  `mz_zip_reader_end`. Flag — mechanical but touches a C-style resource.
- `core/parts/generate.cpp` — `parse_leading_double` uses `std::strtod` with
  `nullptr` endptr and ignores parse failures (returns 0.0 for "abc"). **Risk**:
  `dowel_pin` with a non-numeric `size` silently builds a 0-Ø pin instead of
  throwing via `bad()` (the `d <= 0.0` check does catch it, but the error message
  is "unknown size" rather than "not a number"). **Suggested approach**: tighten
  the error to distinguish "unknown" from "non-numeric". Flag — minor UX, not a
  bug.
- `core/sim/flow_run.cpp:200` — progress-file write uses truncate-in-place
  (comment explains Windows `std::rename` won't replace an existing file), so a
  torn read is possible. **Risk**: a poller can briefly see a short/garbled JSON.
  **Suggested approach**: write to a sibling `.tmp` and atomically replace on
  POSIX, or use a lock-file on Windows. Flag — product decision (the comment
  explicitly accepts torn reads).
- **Cross-track needed**: `host/src/core/sim_client.cpp` (host/CMakeLists.txt:103)
  is the RPC bridge to this track's `spice_simulate` / `lbm_run` / FEA solvers. The
  C++ SPICE/LBM/FEA surface is now wider than what `sim_client` likely routes
  (e.g. `ecad_simulate`, `ecad_route_diff_pair`, `ecad_fill_zones`). **Suggested
  approach**: a host-track pass should audit `sim_client` + the `routeEcadToCpp`
  dispatch against the full `ecad_*` API in `core/ecad/session.h`. Flag for the
  host track — out of my lane.

## Tests added

- `core/tests/test_spice_netlist.cpp` (new, untracked) — sanity tests for the
  SPICE deck parser (`parse_netlist`) and the schematic-to-circuit mapping
  (`netlist_to_spice_circuit`): built-in OPAMP/NE555 subckt expansion, ground
  canonicalization, value-suffix scaling, and the starter-SYMBOL sim-table
  fallback path (the very parity the header comments now correctly describe).
  **NOTE**: this file is NOT registered in `core/CMakeLists.txt` — per the
  Phase 1 guardrails I did not edit CMakeLists. Phase 2 must add a
  `target_sources` / `doctest_discover_tests` entry for it before it runs.

No other test files were created or modified.

## Files touched

Code (stale-comment fixes only):
- `core/sim/spice.h`
- `core/sim/spice.cpp`
- `core/sim/netlist.cpp`
- `core/ecad/session.h`

Docs:
- `docs/review/core-sim-ecad.md` (this file)

No CMakeLists, build config, or non-track source files were modified.
