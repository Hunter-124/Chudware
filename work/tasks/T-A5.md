---
id: T-A5
title: Integration day - run demo mission end-to-end
workstream: A
owner: sonnet-vale
status: review
branch: task/T-A5-integration-demo
day: 5
priority: 4
depends_on: [T-A4, T-INT]
---

## Context

Day 5 is integration: run the sprint demo mission ("battery-powered soil-moisture sensor on a cedar post") through the full DAG orchestrator, touching every pillar. Person A owns the orchestrator seams — fixing node routing, gate behavior, and verify-node wiring as issues surface. The demo must complete end-to-end with at least one human gate (before "ordering"), one firmware verify node (Renode assert), one structural check, and a priced BOM.

## Files / areas touched

- `services/dag/` — bug fixes throughout
- `services/agent/prompts/dag_planner.md` — tune the demo mission graph
- `contracts/dag_schema.md` — read-only reference; any needed changes require workstream consensus
- Demo run artifacts written to `%APPDATA%/AI-CAD/runs/demo_<timestamp>/`

## Steps

1. Run `graphify query "DAG orchestrator integration demo"` to get a map of all touched modules.
2. Generate the demo mission graph using `dag_plan` MCP tool with the prompt: "battery-powered soil-moisture sensor on a cedar post — full design to fabrication."
3. Execute with `dag_run`; monitor with `dag_status`; fix any node failures (check logs in checkpoint DB).
4. Verify all pillar verify-nodes fire: interference check, ERC/DRC, firmware Renode assert, D/C ratio, budget vs. target.
5. Approve the pre-order gate via `dag_approve`.
6. Record the run (screen capture or log export); save artifacts to `docs/sessions/demo_sprint_day5/`.
7. Write a two-paragraph debrief in `docs/sessions/demo_sprint_day5/DEBRIEF.md`: what worked, what was stubbed, what goes to backlog.

## Definition of done

- [ ] Demo mission runs end-to-end without manual exception handling
- [ ] At least one gate node fires and resumes correctly
- [ ] All five pillar verify-nodes produce a result (pass or documented stub)
- [ ] Run artifacts saved under `docs/sessions/demo_sprint_day5/`
- [ ] DEBRIEF.md written with honest account of what was stubbed

## AI hints

- `graphify query "demo mission soil moisture sensor"` — check if any prior demo fixtures exist
- `graphify query "dag_run dag_status MCP"` — verify tool registration from T-A4
- Relevant files: `services/dag/executor.py`, `services/dag/gate.py`, `services/dag/verify.py`, `services/agent/prompts/dag_planner.md`
- If a pillar node is not ready, stub it: return a hardcoded pass result and add a TODO comment — do not block the demo

## Progress log
- 2026-07-03 00:49 UTC [sonnet-vale] REVIEW check-in: branch task/T-A5-integration-demo pushed. Capstone demo complete — thin-slice (2-node design+gate) proves genuine PAUSE+RESUME; full mission (12-node) fires all 5 verify pillars fail-closed, gate pauses and resumes, report completes. Artifacts in docs/sessions/demo_sprint_day5/. Status: review.
