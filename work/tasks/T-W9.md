---
id: T-W9
title: Simulation verification advisor - pre-sim sanity, hand-calc cross-check, margin-policy narration
workstream: W
owner: sonnet-lark
status: done
branch: task/T-W9-sim-advisor
depends_on: [T-W4, T-W5]
lane: simulation
kind: new
priority: 1
---

## Context

Pillar 20 (docs/ADVISOR_ARCHITECTURE.md §2.8). Sim results are only useful to an agent
loop if (a) garbage setups are caught before the solve and (b) results arrive with an
independent sanity anchor and a margin verdict. Pre-sim: BC lint + mesh suggestion +
order-of-magnitude hand-calc (T-W4 beam calc). Post-sim: extend describe_results with
material margin policy and geometry-linked improvement suggestions; misses become
failure cards (T-W8 shapes if merged, else advisory blockers).

## Files / areas touched

- `services/agent/advisor/sim_advisor.py` — new: precheck + postcheck
- FEA/CFD tool seam (`run_fea_static`/`run_fea_modal`/`run_cfd_flow` wrappers in
  services/mcp) — precheck before dispatch, postcheck on results
- `describe_results` — margin policy + suggestions section
- `engine_oracle/sim/margin_policy.json` — new: per-material-class safety factors
  (yield SF, endurance SF for fatigue contexts) with source notes
- tests: `services/agent/advisor/test_sim_advisor.py`

## Steps

1. Read docs §2.8, contract §3; T-W4 beam calc API.
2. Precheck (deterministic): loads/fixtures reference existing faces (stable ids
   resolve); at least one fixture + one load; body not fully rigid-constrained on all
   faces; mesh size suggestion from min feature size (doc queries: min fillet radius,
   min wall via wall_thickness tool if present); expected-stress anchor when geometry
   maps to a beam/plate approximation (bbox aspect + load pattern heuristic — document
   the mapping rules; emit anchor only when the mapping is confident, else "no anchor").
   Output = aicad.advisory/1 items; blockers stop dispatch (config-gated).
3. Postcheck: compare max stress vs material yield × margin policy → verdict
   PASS/WARN/FAIL; compare vs hand-calc anchor (>5x divergence ⇒ WARN "result
   implausible, check BCs"); locate max-stress face by stable id/name and, when it is
   a fillet/sharp edge, emit the Kt-based suggestion pattern from docs §2.8 with the
   owning feature named as candidate_fix ref.
4. Extend describe_results: narrated verdict + margins + anchor comparison +
   suggestions (keep raw scalars; additive only).
5. Wire into T-W5 engine sim stage battery + exit gate (sim stage exits on
   PASS/WARN-acknowledged only).
6. Tests: BC lint fixtures (no fixture, all-fixed, dangling ref); cantilever fixture
   doc where FEA ≈ beam calc within tolerance (use existing sim test fixtures if
   present); margin verdicts across materials; implausible-result WARN.
7. MCP freshness (cad_guide simulation topic) + `graphify update .`.

## Definition of done

- [ ] Precheck catches the BC-lint fixture classes and suggests mesh size
- [ ] Hand-calc anchor emitted for beam-like fixtures; divergence WARN works
- [ ] describe_results narrates margin verdict + geometry-linked suggestions
- [ ] Wired as sim-stage battery + exit gate; tests pass

## AI hints

- `graphify query "fea static run study describe_results"` first; core/sim entry
  points are via worker RPC — do not touch C++ solvers.
- Materials fields for margins: engine_oracle/sim/materials.json.
- Engine doc-load & verify contract memory: use doc_new+eq_set+add_feature to build
  test fixtures headlessly.

## Scope update 2026-07-04 (in-app pivot — Hunter directive)

Priority raised to 1: the in-app copilot harness (services/agent + GUI) is the sole
integration target for this task. Changes vs the original spec:
- SKIP the MCP freshness rule steps (server instructions, cad_guide topics, manual
  regen) — external MCP surface polish is parked in T-W20 (deferred batch task).
- Registering tools with the MCP server is OPTIONAL — do it only when it is one
  decorator on the same function and costs nothing; never let it drive design.
- Acceptance = the capability working through the IN-APP path (services/agent hosts,
  loop.py / dag runner / pipeline), proven by python tests. graphify update . still
  required (that serves our own agents).

## Progress log
- 2026-07-05 02:59 UTC [sonnet-lark] Implementation complete. Branch task/T-W9-sim-advisor pushed.

## What was built

**services/agent/advisor/sim_advisor.py** (new, 420 lines):
- `precheck(study, doc_geom)` — BC lint (no fixture/load → blocker, all-fixed/dangling → action), mesh-size suggestion from min fillet/wall, beam_calc_raw anchor (aspect ≥ 3 → confident; else "no anchor"). Returns aicad.advisory/1-shaped items.
- `postcheck(study, precheck_items, doc_geom)` — margin-policy verdict (PASS/WARN/FAIL per SF), FEA/anchor divergence WARN (>5× ratio, both directions), Kt-pattern suggestion for fillet/sharp-edge max-stress faces.
- `narrate_postcheck(r)` — additive text appended to describe_results output.
- `CHUDWARE_SIM_PRECHECK_BLOCK` env gate (default on) — deliberate §9.3 deviation documented in module docstring.

**engine_oracle/sim/margin_policy.json** (new):
metal_steel SF=1.5/2.0, metal_aluminium SF=1.5/2.5, metal_titanium SF=1.4/2.0, polymer SF=2.0/3.0, general SF=2.0/3.0. Sources: Shigley 10th §6-12.

**services/agent/advisor/stages.py** — added CHECK_SIM_BC/MARGIN/ANCHOR/DIVERGENCE/MESH; sim battery now [sim_bc, sim_results, sim_margin, sim_divergence, sim_mesh, sim_anchor]; exit gate: [sim_bc, sim_results, sim_margin].

**services/agent/advisor/engine.py** — fix providers for sim_bc, sim_margin, sim_divergence registered in _FIX_PROVIDERS.

**services/mcp/gsl_tools.py** — additive postcheck narration appended to fea_static describe_results path; guarded so advisor failure never crashes narration. Existing frozen-string tests unaffected.

**services/agent/advisor/test_sim_advisor.py** — 43 tests, all offline (no FEA solver, no network).

## Test output
43 passed, 0 new regressions
Baseline: 23 failed / 40 errors (all pre-existing issue #16, unchanged)
Suite: 1260 passed, 23 failed, 35 skipped, 40 errors (vs 1217/23/35/40 before)

## Citation honesty
FEA solver does not run headlessly in CI. Divergence tests seed a study dict with max_von_mises_mpa set to 1.5× / 10× beam_calc_raw output for same geometry — proves comparison logic, does NOT verify the FEA solver. Documented in test docstrings.

## DoD checkboxes
- [x] Precheck catches BC-lint fixture classes and suggests mesh size
- [x] Hand-calc anchor emitted for beam-like fixtures; divergence WARN works
- [x] describe_results narrates margin verdict + geometry-linked suggestions
- [x] Wired as sim-stage battery + exit gate; tests pass
