---
id: T-DP12
title: GUI sub-agent model-preference settings + effort auto/advanced mode UI
workstream: DP
owner: unassigned
status: todo
branch: task/T-DP12-model-prefs-settings
depends_on: [T-DP9]
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

Doc §3.4/§3.6. In the consolidated Settings modal AI tab: sub-agent model-preference
controls (which model each role/tier uses, feeding routing.yaml), and the effort auto/advanced
mode UI (auto-mode default; advanced exposes stage/council/helper knobs). Builds on T-W28 effort
section. Inline in the popup (settings consolidation rule — no floating windows).

## Definition of done

- [ ] Sub-agent model prefs editable per role/tier, persisted, written through routing.py.
- [ ] Auto/advanced mode toggle; advanced reveals stage/council/helper knobs.
- [ ] Inline in Settings AI tab; shot-verified; ui_style_check clean.
- [ ] gate.sh PASS; graphify update.
