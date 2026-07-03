---
id: T-BL20
title: FEA/CFD V&V cross-validation - MacNeal-Harder/NAFEMS cases + CalculiX second opinion
workstream: backlog
owner: sonnet-doro
status: review
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

## Progress log
- 2026-07-03 22:35 UTC [sonnet-doro] (foreman liveness probe) No branch pushed ~2h after claim. If you are alive: push your worktree branch NOW (even WIP) — work only counts on the remote. If nothing lands by the next foreman sweep the claim gets released per the dead-driver pattern.
- 2026-07-03 22:38 UTC [sonnet-doro] Landed on task/T-BL20-fea-vv-crossval (pushed). 5 new executed MacNeal-Harder-style FEA/modal cases (all PRECISE, <0.5% err vs live OCCT build) + CalculiX second-opinion stage (ccx absent in this sandbox -> graceful SKIP, .inp generator smoke-tested). Found + filed a real engine bug in the pressure-load path (core/sim/fea_run.cpp) as issue #9, marked xfail not hidden. Also filed issue #10 for pre-existing mcp_server test failures found during gate verification (confirmed unrelated to this branch). Did not re-run gate.sh's cold OCCT core-build stage in this fresh worktree (no C++ touched, no build cache, ccache disabled -- would run past one foreground turn); check.py + full vv_benchmark.py pytest suite verified directly instead. See HANDOFF section in work/backlog/T-BL20.md for detail.
