---
id: T-DP5
title: Overseer audit-and-reroute loop + user-change → stage mapping
workstream: DP
owner: opus-slate1
status: doing
branch: task/T-DP5-overseer-audit-loop
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

Doc §3.1. Extend the existing overseer role/advisor: after each stage, inspect the plan +
chain-of-thought, self-identify errors, and route a TARGETED correction back to the specific
stage that owns it (never a blind full retry). Map a user-requested change ("thicker wall",
"add a tab") to the exact stage/feature to re-enter so fixes are implemented, not re-derived.
Overseer holds + re-grounds the master intent so fresh-context stages never drift.

## Definition of done

- [ ] Overseer emits `audit` ledger events with {stage, finding, targeted_reentry}.
- [ ] Re-route lands at the correct stage (test: a geometry error routes to geometry_designer,
      not a full restart).
- [ ] User-change mapping: a change request resolves to a stage+feature re-entry (test).
- [ ] Master-intent re-grounding: a drifted stage output is caught (test).
- [ ] gate.sh PASS; graphify update.
