---
id: T-DP1
title: Freeze design-pipeline contract (aicad.designpipe/1) + wire six stages into the phase machine
workstream: DP
owner: unassigned
status: todo
branch: task/T-DP1-design-pipeline-contract
depends_on: []
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

- `contracts/design_pipeline.md` (aicad.designpipe/1) frozen: stage IO schemas (`feature`,
  `geometry_spec`, `linkage`, `build_node` — see doc §4), ledger event kinds (add the new kinds
  to aicad.ledger/1; keep the provenance rule on `dimension` + `geometry_spec`), stage-gate
  definitions that READ THE LEDGER (not handoff prose — universal W32 rule), effort/auto-mode
  hooks (which stages/councils/helpers each scaffold level enables), advisor trigger bindings
  (aicad.trigger/1: geometry-kind → helper set).
- Wire the six stages into services/agent/pipeline/phases.py as a FLAGGED refinement of the
  existing SPECIFY→ORGANIZE→PLAN→BUILD span; existing pipeline unchanged when flag off.
- Ledger writer/reader helpers for the new kinds + scoped-slice accessors for the
  mission-dispatch layer (T-W30/W32) to hand fresh-context stages.

## Definition of done

- [ ] contracts/design_pipeline.md frozen; referenced from contracts/README.md; manual regen clean.
- [ ] New ledger kinds validated (provenance enforced); round-trip tests.
- [ ] phases.py runs the six-stage refinement behind a flag; current phase tests green when off.
- [ ] Stage gates read the ledger; unit test per gate.
- [ ] gate.sh PASS; graphify update.
