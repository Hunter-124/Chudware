---
id: T-DP2
title: feature_architect role + functional-decomposition helpers + requirement-coverage checker
workstream: DP
owner: unassigned
status: todo
branch: task/T-DP2-feature-architect
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

Stage 1 (doc §2). New role services/agent/roles/feature_architect.yaml with a rich system
prompt: enumerate every FUNCTION the part must perform (mount/mate/seal/bear-load/clear/fasten/
locate/dissipate) BEFORE geometry. Output `feature[]` = {id, function, driving_requirement,
interfaces_with, criticality} written to the ledger.
- Helpers: part-class functional checklists (bracket/housing/shaft/plate/enclosure/fitting…);
  requirement-coverage checker (every requirement → ≥1 feature; every feature → a requirement or
  recorded assumption).
- Completeness-critic council hook ("what function did we miss?").

## Definition of done

- [ ] feature_architect role yaml + prompt; handoff schema validated.
- [ ] Functional checklists for ≥6 part classes; coverage checker with tests.
- [ ] Ledger `feature` events written + read by the Stage-1 gate.
- [ ] Gym: a C1-class task produces a coherent feature list (real run, honest result).
- [ ] gate.sh PASS; graphify update.
