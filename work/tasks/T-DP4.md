---
id: T-DP4
title: linkage_planner role + reference-integrity / datum / interface-consistency checkers
workstream: DP
owner: sonnet-jade1
status: doing
branch: task/T-DP4-linkage-planner
depends_on: [T-DP1]
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

Stage 3 (doc §2). New role services/agent/roles/linkage_planner.yaml: decide how geometry
links — datum scheme, reference chains, shared interfaces, parametric relationships,
sketch-on-face vs sketch-on-plane. Output `linkage` = {datum_tree, reference_edges,
shared_interfaces} to the ledger (the skeleton the buildgraph orders).
- Helpers: reference-integrity checker (no dangling refs, no cycles), datum-completeness,
  interface-consistency (two features sharing a bore agree on Ø/tolerance).
- Regen-stability council hook ("robust to later edits?").

## Definition of done

- [ ] linkage_planner role + prompt; linkage schema validated.
- [ ] Three checkers with tests (dangling ref, cycle, interface mismatch each caught).
- [ ] Ledger `linkage` events read by the Stage-3 gate + consumed by buildgraph (Stage 4).
- [ ] gate.sh PASS; graphify update.

## Progress log
- 2026-07-06 00:08 UTC [sonnet-jade1] Role, checkers, tests committed + pushed. gate.sh running (at pytest stage). 32 new tests + 57 roles/routing all green. Setting review when gate returns.
