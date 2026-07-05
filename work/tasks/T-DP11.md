---
id: T-DP11
title: GUI transparency panel — helpers / advisories / councils / delegation / tool calls
workstream: DP
owner: unassigned
status: todo
branch: task/T-DP11-transparency-panel
depends_on: [T-DP6]
lane: app-gui
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

Doc §3.7. A ui/imgui panel showing every helper fire, advisory, council vote, tool call,
auto-applied fix, and delegation — what fired, why, what it injected, what it changed — so
issues can be troubleshot. Glass-column design language, dark-mode contrast, ui_style_check
clean. Shot-verify each state.

## Definition of done

- [ ] Panel renders the live action stream from ledger `advisory`/`audit`/tool events.
- [ ] Each entry inspectable (inputs/outputs/why); auto-applied fixes flagged distinctly.
- [ ] Shot scenarios for populated + empty states, EYEBALLED.
- [ ] ui_style_check clean; gate.sh PASS; graphify update.
