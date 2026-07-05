---
id: T-DP13
title: Multi-file agent view — watch N agents build N parts simultaneously
workstream: DP
owner: sonnet-vale1
status: doing
branch: task/T-DP13-multifile-agent-view
depends_on: []
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

Doc §3.4. Agents manage multiple open documents (mission workspace, aicad.mission doc
leases) so the user can watch every sub-agent build its part of an assembly at once. GUI: a
multi-doc view with per-doc agent activity, live viewport per part. ui/imgui only; glass design;
shot-verified. Independent — can start now against the existing mission workspace.

## Definition of done

- [ ] Multiple docs open with per-doc agent attribution + live activity indicator.
- [ ] User can switch/tile between parts being built concurrently.
- [ ] Shot scenario: 2+ parts under concurrent agent work, EYEBALLED.
- [ ] ui_style_check clean; gate.sh PASS; graphify update.
