---
id: T-DP6
title: Advisory expansion — every checker becomes a diagnostic advisor with a targeted fix + tool
workstream: DP
owner: sonnet-rust1
status: doing
branch: task/T-DP6-diagnostic-advisors
depends_on: [T-DP1]
lane: ai-copilot
kind: overhaul
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

Doc §3.2 — "advise, don't guess-loop". Turn reactive checkers (interference, interface,
DFM, wall-thickness, sim precheck) into ADVISORS that emit a diagnosed, targeted fix + the tool
to apply it — e.g. interference → "boss $3 collides with cut $7 by 2.1mm along +Z; reduce boss
height ≤X or move cut to Y" + the edit tool. Advisors fire BEFORE an op (inject the right
calculator/data/context) and AFTER (diagnose, target). Interface components carry extra context
+ checkers, audited on failure. Every acceptance criterion maps to a concrete check invocation.

## Definition of done

- [ ] ≥4 checkers upgraded to diagnostic advisors emitting {diagnosis, targeted_fix, fix_tool}.
- [ ] Interference advisor names the colliding features + magnitude + axis + suggested fix (test
      against a real overlapping-solid doc).
- [ ] Pre-op advisory injection wired via aicad.trigger; post-op diagnosis feeds the fixer loop
      (bounded, not endless).
- [ ] gate.sh PASS; graphify update.
