---
id: T-DP3
title: geometry_designer role + proactive op-aware helper/calculator registry
workstream: DP
owner: opus-cyan1
status: doing
branch: task/T-DP3-geometry-designer
depends_on: [T-DP1]
lane: ai-copilot
kind: new
priority: 1
---

## Context

Part of the guided design-thinking pipeline — see
[docs/AGENT_DESIGN_PIPELINE.md](../../docs/AGENT_DESIGN_PIPELINE.md) (architecture of record,
Hunter 2026-07-05). MANDATORY: graphify to orient before editing; REUSE existing W-stack
substrate (phases.py, roles/, councils.py, advisor.md, aicad.buildgraph/1, delegation/routing,
aicad.effort/1, aicad.ledger/1, mission workspace) — extend, do not duplicate. Numbers never
invented — every value cites a tool/table/calc. Flexibility rule: never impose a fixed modeling
path; helpers are offered, stages are skippable checkpoints, scaffolding scales by effort/tier.

## Scope

Stage 2 (doc §2) — the HEART of "advise, don't recall". New role
services/agent/roles/geometry_designer.yaml: per feature, decide concrete geometry + critical
dimensions GATED BY material and process. Output `geometry_spec` = {shapes, nominal_dims,
tolerances, datum_refs, provenance} to the ledger.
- Build the PROACTIVE OP-AWARE HELPER REGISTRY: keyed by geometry kind, auto-load the correct
  calculator/data for what the model is about to do — fit_calc/recommend_fit (mating bores),
  research_thread_spec (threads), wall_thickness/process minimums (walls), bend allowance
  (sheet), draft (molded), research_material_lookup / research_standard_section /
  research_web_search (lookups). Wire via aicad.trigger/1.
- Manufacturing/material are INPUTS to dimensioning (machined vs printed → different tolerances/
  minimums pulled from DFM/process data). Every dim carries provenance — never recalled.

## Definition of done

- [ ] geometry_designer role + prompt; geometry_spec schema validated.
- [ ] Helper registry: geometry-kind → helper set, ≥8 kinds, trigger-bound; each helper returns
      real tool output (no fabricated numbers).
- [ ] Material/process gating changes the emitted tolerances (test: same feature, machined vs
      printed → different minimums).
- [ ] Ledger geometry_spec events with provenance; Stage-2 gate reads them.
- [ ] gate.sh PASS; graphify update.

## Progress log
- 2026-07-05 22:06 UTC [opus-cyan1] Registry + role shipped: 10 geometry kinds bound to triggers, resolve_helpers(kind,material,process) returns real calc/profile numbers with provenance; machined-vs-printed gating verified (cnc wall 0.5 vs fdm 1.0, tol 0.05 vs 0.4). Added bend_allowance+draft_check calcs, sheet_bend+molded_draft triggers, geometry_designer.yaml, localized GEOMETRY gate-ctx branch. 28 new tests + 198 affected green. Running gate.
- 2026-07-05 22:23 UTC [opus-cyan1] gate.sh PASS (RESULT: PASS; the recorded services/engine_oracle fails are pre-existing T-V2, none touch my files). Added trigger-bound assertion (29 tests). Confirmed role dispatch is by role id not stage_scope, so geometry_designer at stage_scope=[feature] is collision-free. CAVEAT for merger: role loads+validates and GEOMETRY gate reads the ledger, but phase->role dispatch (running the role during GEOMETRY phase) does not exist in the codebase yet — that's the seam, out of T-DP3 scope, same state as DP2. Setting review.
