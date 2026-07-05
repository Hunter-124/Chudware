---
id: T-DP10
title: Auto-mode workflow router + gym-based tuning loop
workstream: DP
owner: unassigned
status: todo
branch: task/T-DP10-automode-tuning
depends_on: [T-DP2, T-DP3, T-DP4, T-DP8]
lane: ai-copilot
kind: new
priority: 2
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

Doc §3.6. Auto-mode (default): a workflow router classifies the task and lets the model
self-select how much pipeline to use; LOCAL MODELS DEFAULT TO FULL SCAFFOLD. Advanced mode
exposes every knob. The tuning loop: the gym bench harness (tools/flywheel/bench/gym/) runs
example tasks across workflow configurations and LEARNS which scaffold level is best per
task-class, feeding the router's classifier — measured, never guessed.

## Definition of done

- [ ] Workflow router classifies a task → scaffold level; local defaults to full.
- [ ] Gym runs ≥2 scaffold configs over the C-suite and records per-class win rates (real runs,
      honest numbers).
- [ ] Router consumes the tuning output; a task-class picks its measured-best config (test).
- [ ] gate.sh PASS; graphify update.
