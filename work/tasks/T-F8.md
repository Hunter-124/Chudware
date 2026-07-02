---
id: T-F8
title: describe_assembly, describe_board (GSL promotion), describe_project
workstream: F
owner: unassigned
status: todo
branch: task/T-F8-narration-completion
day: 8
depends_on: [T-F7]
---

## Context

Pillar 11 completion on the T-F7 contract: assemblies (mate graph in words, DOF per
component, fit/clearance narration — lift `_format_fit` out of the build loop),
ECAD (promote the bespoke `ecad_status` into the GSL layer), and the DAG run itself
(`describe_project` — nodes done/blocked, budgets, open questions; feeds WS-A
`dag_status` and the mission-control UI T-F17). READ `docs/AI_TOOLING_MASTER_PLAN.md`
§Pillar 11.

## Files / areas touched

- `services/mcp/gsl_tools.py` (or sibling) — the three describers
- `services/agent/` — extract `_format_fit` into the shared layer
- `services/dag/` — read-only join for describe_project (schema from T-A1 if landed;
  otherwise stub against `contracts/dag_schema.md`)

## Definition of done

- [ ] Three describers on the narration contract, cad_guide topics added
- [ ] `_format_fit` has one home; build loop calls the shared one
- [ ] Tests green; `bash scripts/gate.sh`; `graphify update .`

## Follow-ups inherited from T-F7 (Fable, 2026-07-02)

- Extend `_persist_study` to store the max-von-Mises node/face location so
  `describe_results` can say WHERE the max stress is (T-F7 Q2; currently narrated as
  explicitly-unavailable per the contract's honesty rule).
- Expose per-op stepdown/stepover through a read RPC (cam_extract or cam_tree) so
  `describe_toolpaths` can render those lines unconditionally (T-F7 Q3).
- `contracts/narration.md` now codifies the REAL RPC shapes (verified against C++
  sources by T-F7) — trust it over older notes; cam_simulate emits
  `results[].{machined_fraction, gouges, rapid_collisions, cycle_time_s}`.
