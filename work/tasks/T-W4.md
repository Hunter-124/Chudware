---
id: T-W4
title: Engineering calculator registry - bolted joints, threads, press fits, beams, walls + geometry auto-prefill
workstream: W
owner: unassigned
status: todo
branch: task/T-W4-calculators
depends_on: [T-W3]
lane: ai-copilot
kind: new
priority: 1
---

## Context

Pillar 20 (docs/ADVISOR_ARCHITECTURE.md §2.5). A keyed registry of closed-form
engineering calculators the advisory engine can invoke with inputs pre-filled from
live geometry, so models receive computed numbers ("thread engagement needed: 9 mm in
6061 for M6 class 8.8") instead of deriving them. All results use aicad.calc/1
(contracts/advisor.md §7) including `inputs.prefilled` provenance.

## Files / areas touched

- `engine_oracle/research/calcs/` — new package: `bolted_joint.py`, `thread_engagement.py`,
  `press_fit.py`, `beam.py`, `wall_thickness.py`, `snap_fit.py`, `registry.py`
- `services/mcp/research_tools.py` — route `research_eng_calc(calc_id, inputs)` through
  the registry; list_calcs discovery
- `services/agent/advisor/prefill.py` — new: map calc input slots to doc queries
  (doc_eval_measure, doc_bbox, feature params) — standalone module, no advisory-engine
  dependency (T-W5 consumes it later)
- tests: `engine_oracle/research/calcs/test_calcs.py`

## Steps

1. Read contracts/advisor.md §7; T-W3's fits.py for envelope precedent.
2. Registry: `register(calc_id, fn, input_schema, prefill_hints)`; each calc declares
   typed inputs (family: length/force/torque/material_ref/...) so prefill and unit
   normalization (interaction contract) are automatic.
3. Calculators (standard closed forms; cite Shigley/Roark per function):
   - bolted_joint: preload from torque (T=K·F·d), stress area check vs proof load,
     recommended torque for grade/size
   - thread_engagement: min engagement length internal/external material pairing
     (uses materials.json yield values)
   - press_fit: interference pressure (thick-wall Lamé), assembly force, stress check
     vs yield (pairs with T-W3 fit bands)
   - beam: cantilever + simply-supported point/UDL deflection & max stress for
     rect/round/tube sections (this is also the sim cross-check primitive for T-W9)
   - wall_thickness: min wall by process (machining chatter rule-of-thumb, 3dp min
     wall from DFM profile, injection nominal wall by material)
   - snap_fit: cantilever snap deflection/strain vs allowable for material
4. `prefill.py`: given a calc_id + doc context, resolve hinted inputs (e.g. press_fit
   d from a named cylindrical face via doc_eval_measure; beam span from bbox) and
   record `inputs.prefilled` entries. Unresolvable slots stay for the model/user.
5. Tests: each calc against a hand-checked reference case (document the reference in
   the test); registry discovery; prefill against a fixture doc.
6. MCP freshness: research section of server instructions lists calc ids; `graphify update .`.

## Definition of done

- [ ] ≥6 calculators registered, each with a cited reference case test
- [ ] aicad.calc/1 envelopes with equation + inputs.prefilled provenance
- [ ] research_eng_calc routes calc_ids + discovery works
- [ ] prefill resolves geometry-derived inputs on a fixture doc
- [ ] Tests pass; server instructions updated

## AI hints

- `graphify query "research_eng_calc"` to find the current eng_calc seam.
- materials.json fields: E_mpa, nu, density_g_cm3, yield_mpa, ultimate_mpa,
  endurance_mpa (engine_oracle/sim/materials.json).
- Keep each calc pure + stdlib/numpy only; no LLM anywhere in this task.

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
