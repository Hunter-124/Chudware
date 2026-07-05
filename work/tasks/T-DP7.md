---
id: T-DP7
title: Sketch-completion + GSL-status-to-text + critical-dimension scheme helpers
workstream: DP
owner: sonnet-brook1
status: doing
branch: task/T-DP7-sketch-gsl-helpers
depends_on: []
lane: modeling-core
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

Doc §3.5 — the execution rung lives or dies on sketch quality. Independent of the contract,
can start now.
- Harden auto-coincident touching endpoints for closed-path detection (sketch.auto_coincident).
- Auto-constrain toward DOF 0; critical-dimension scheme chosen WITH manufacturing method +
  material in mind (machined bore vs printed hole → different schemes).
- GSL status→text: a rich text read of sketch/model state (closure, remaining DOF in plain
  words, ordered relations-then-dimensions schedule) so a no-vision local model can SEE its work
  and what's left. This is the feedback channel that makes the loop converge.

## Definition of done

- [ ] Closed-path auto-coincident: touching endpoints forming a loop become coincident (test).
- [ ] Auto-constrain drives a representative underconstrained sketch to DOF 0 (test).
- [ ] Critical-dim scheme differs by process/material (test).
- [ ] GSL text read reports closure + remaining DOF in plain words + a relations-then-dimensions
      schedule (test against ≥3 sketch states).
- [ ] gate.sh PASS; graphify update.
