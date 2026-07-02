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
