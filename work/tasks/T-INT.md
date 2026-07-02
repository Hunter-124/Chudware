---
id: T-INT
title: ABSORBED into T-A5 - day-5 integration demo (capstone consolidated, Erik 2026-07-03)
workstream: integration
owner: Bruh
status: backlog
branch: task/T-INT-integration-demo
day: 5
depends_on: [T-A4, T-B4, T-C4, T-D4]
---

## Context

The sprint goal: one prompt — "battery-powered soil-moisture sensor on a cedar post" — runs through the DAG orchestrator and touches every pillar. Mechanical enclosure and PCB use existing ECAD assets. The integration task is the final wiring and coordination layer: ensure all pillar outputs flow correctly through the DAG, all verify nodes fire, the human gate before "ordering" works, and the run is recorded. Person A owns this coordination; all workstreams converge here.

## Files / areas touched

- `services/dag/` — orchestrator (Workstream A)
- `engine_oracle/bom.py`, `engine_oracle/cost_model.py` — sourcing/cost (Workstream B)
- `engine_oracle/firmware/` — firmware (Workstream C)
- `engine_oracle/structural/` — structural (Workstream D)
- `docs/sessions/demo_sprint_day5/` — run artifacts and debrief

## Steps

1. Confirm all day-4 tasks (T-A4, T-B4, T-C4, T-D4) are in `done` or `review` status.
2. Generate the demo mission graph via `dag_plan` tool with the full prompt.
3. Verify the graph includes: `plan` → `design` (existing ECAD) → `firmware` → `source` → `structural` → `verify` (interference + ERC/DRC + fw_sim_run + D/C ratio + budget) → `gate` (human approval) → `report`.
4. Run `dag_run` and monitor with `dag_status`; triage any failures (stub rather than block the demo).
5. Approve the pre-order gate via `dag_approve`.
6. Collect outputs: priced BOM, firmware log, structural check, code compliance citations, cost estimate.
7. Record the run (terminal log or screen capture); save to `docs/sessions/demo_sprint_day5/`.
8. Write `docs/sessions/demo_sprint_day5/DEBRIEF.md`: demo outcome, what was stubbed, top 3 backlog items surfaced.

## Definition of done

- [ ] Demo mission runs end-to-end with `dag_run` without crashing
- [ ] All five pillar verify nodes fire (even if some are stubs)
- [ ] Gate node fires and resumes correctly after `dag_approve`
- [ ] Priced BOM with at least one supplier-resolved part in the output
- [ ] Structural D/C ratio returned for the cedar post
- [ ] DEBRIEF.md written and committed

## AI hints

- `graphify query "DAG orchestrator integration all pillars"` — confirm all services are wired
- `graphify query "demo sprint sensor mission"` — check for any demo fixture or seed data
- Relevant files: `services/dag/executor.py`, `services/dag/gate.py`, `services/dag/verify.py`, `services/agent/prompts/dag_planner.md`
- Run order in shell: `python -c "from services.dag import executor; executor.run_from_prompt('battery-powered soil-moisture sensor on a cedar post')"`
