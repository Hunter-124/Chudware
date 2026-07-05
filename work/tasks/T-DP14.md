---
id: T-DP14
title: Design-pipeline ↔ PLM wiring (stage checkpoints + rationale narrative)
workstream: DP
owner: unassigned
status: todo
branch: task/T-DP14-pipeline-plm-wiring
depends_on: [T-DP1, T-R5]
lane: plm-projects
kind: new
priority: 3
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

Doc §3.8. The pipeline runs INSIDE the vault: stage boundaries cut PLM checkpoints; the
design rationale (feature → geometry → linkage → build decisions, straight from the ledger)
becomes the PLM "why" narrative (T-R3 summaries). Multi-part assembly work attributed per
participant. Respects the not-git-shaped, contract-first PLM decisions (see
plm-fine-detail-decisions memory).

## Definition of done

- [ ] Each stage boundary writes a vault checkpoint with the stage's ledger slice as rationale.
- [ ] PLM history shows the design story (feature→geometry→build) in plain language (T-R3).
- [ ] Multi-part attribution correct in the timeline.
- [ ] gate.sh PASS; graphify update.
