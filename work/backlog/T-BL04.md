---
id: T-BL04
title: Fatigue + thermal + buckling + contact FEA (CalculiX)
workstream: backlog
owner: opus-fell
status: doing
branch: task/T-BL04-calculix-fea
depends_on: []
lane: simulation
kind: new
priority: 4
---

## Context

S-N curve fatigue post-processing on existing static FEA results; CalculiX (GPL,
subprocess-only per CONTRIBUTING.md license rules) for everything PyNite (T-D2)
doesn't cover.

**Expanded 2026-07-03 (Erik-directed):** since CalculiX is being integrated anyway
for thermal, bundle in the other analysis types it natively supports rather than
three separate solver-integration tasks:

1. Fatigue life estimate (S-N curve post-processing on existing `run_fea_static`
   results — no new solver needed, pure post-processing)
2. Thermal steady-state solve via CalculiX subprocess
3. Linear buckling (eigenvalue) via CalculiX — critical for slender members/thin
   panels where static stress alone doesn't catch the failure mode
4. Nonlinear/contact FEA (large deflection, contact between bodies) via CalculiX —
   needed for snap-fits, press-fits, anything where parts touch under load

Expose all four as MCP tools and DAG verify-node end conditions, same pattern as
`run_fea_static`/`run_fea_modal`.

## Definition of done

- [x] Fatigue life estimate tool on static results — `run_fea_fatigue` routes to the
      existing C++ `sim_run_fatigue` solver (reuses the static stress field + all 5
      mean-stress theories; no duplicate solver) then applies the Marin/S-N life layer
      mirroring `host/src/ui/sim_manager.cpp` so MCP and GUI never diverge.
- [x] Thermal steady-state solve via CalculiX subprocess — `run_fea_thermal`
      (`*HEAT TRANSFER, STEADY STATE` box-proxy) via the reused `fea_advanced.run_ccx`.
- [x] Linear buckling (eigenvalue) solve via CalculiX — `run_fea_buckling` (`*BUCKLE`).
- [x] Nonlinear/contact FEA (large deflection + contact) via CalculiX — `run_fea_contact`
      (`*STATIC, NLGEOM` + `*CONTACT PAIR`, two-block proxy).
- [x] All four callable as MCP tools (registered in `services/mcp/cad_tools.py`, cad
      family + `cad_guide('simulation')` + freshness list) and DAG end conditions
      (`services/dag/verify.py` DISPATCH_TABLE + `_build_tool_kwargs`, fail-closed).
- [x] Hand-calc-validated per solver: `scripts/vv_benchmark.py` FAMILY 10 — FAT1
      Basquin life vs Shigley Eq 6-14..16 (7.263e4 cyc, PRECISE, PASS), FAT2 runout
      (EXACT), FAT3 Marin ka vs Shigley Eq 6-19 (PRECISE); TH1 1-D conduction vs
      Incropera, BK1 Euler vs Timoshenko & Gere, CT1 Newton-III conservation (all
      ENGINEERING, ccx). ccx cases SKIP honestly (§0 shape) when the binary is absent;
      generators/parsers covered by 13 pure-Python unit tests + TestSmoke cases.

## Implementation notes

- **Reuse (no second CalculiX integration):** `services/mcp/fea_advanced.py`
  generalises the T-BL20 `bench_calculix` seam into a reusable `run_ccx()` subprocess
  runner + clean-room `.inp` generators + `.dat` parsers. **sonnet-wick (T-BL17,
  frequency response) can import `fea_advanced.run_ccx` for a `*FREQUENCY` / `*STEADY
  STATE DYNAMICS` fallback** rather than building a rival ccx runner.
- **Fatigue solver already existed in C++** (`core/sim/fea_fatigue.cpp`,
  `sim_run_fatigue`, GUI-wired) — the tool routes to it, matching the
  `run_fea_static→sim_run_static` pattern, instead of a Python reimplementation.
- **Netgen-gated backend:** on Linux (no Netgen) `sim_run_fatigue`/`sim_run_static`
  are compiled out (`#ifdef AICAD_WITH_NETGEN`), so fatigue degrades `{skipped:true}`
  exactly like `run_fea_static` does today; ccx is not installed here either, so the
  three ccx tools also degrade honestly. No fabricated results anywhere.
- **License:** CalculiX row in `contracts/licenses.md` expanded (T-BL20 V&V →
  T-BL04 tools); GPL-2.0 subprocess-only, PATH-discovered (`AI_CAD_CCX` override),
  never linked/bundled, clean-room `.inp` (no CalculiX example text copied).
