---
id: T-DP9
title: Hierarchical delegation (overseer-discretion recursion) + budget meter + kill-switch
workstream: DP
owner: unassigned
status: todo
branch: task/T-DP9-hierarchical-delegation
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

Doc §3.4. Extend aicad.delegation/1: an overseer/lead spawns lesser sub-agents; sub-agents
may spawn their own with approval from the overseer that called them, RECURSIVELY — Hunter:
overseer discretion, NO hard depth cap. Rails are a human-visible budget meter + a kill-switch
(runaway protection) + per-role model routing. Sub-agent model preferences are read from config
(GUI setting lands in T-DP12).

## Definition of done

- [ ] Recursive spawn with per-level overseer approval; no hard cap (config soft-limit only).
- [ ] Budget meter tracks tokens/agents across the whole tree; kill-switch halts cleanly (test).
- [ ] Per-role model routing honored; sub-agent model prefs read from config.
- [ ] gate.sh PASS; graphify update.
