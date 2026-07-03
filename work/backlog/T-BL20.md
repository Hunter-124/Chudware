---
id: T-BL20
title: FEA/CFD V&V cross-validation - MacNeal-Harder/NAFEMS cases + CalculiX second opinion
workstream: backlog
owner: sonnet-doro
status: doing
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

- [ ] FEA static + modal cases execute (not SKIP) in the V&V battery via MCP wiring
- [ ] At least 3 MacNeal-Harder/NAFEMS-style cases with tolerance-tiered error tables
- [ ] CalculiX subprocess stage cross-checks at least the cantilever case; licenses.md
      row if any new dep is added
- [ ] gate.sh stage 4.5 covers the new cases; report lands in reports/vv/

## AI hints

- `work/questions/T-BL01.md` — the original scope question and what was deferred
- `reports/vv/`, gate.sh stage 4.5 — existing battery structure to extend
- Engine doc-load & verify contract: doc_new + eq_set + add_feature; CoreError vs
  feature_status for rebuild failure detection
