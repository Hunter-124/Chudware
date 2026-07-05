---
id: T-DP13
title: Multi-file agent view — watch N agents build N parts simultaneously
workstream: DP
owner: sonnet-vale1
status: review
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

## Progress log
- 2026-07-05 20:43 UTC [sonnet-vale1] Starting T-DP13: multi-file agent view. Running graphify to orient on mission workspace + ui/imgui substrate.
- 2026-07-05 21:02 UTC [sonnet-vale1] Implementation complete. Extended DocTab with agentRunning/agentOwner/agentModel/agentSpinT; doc_tabs.cpp draws pulsing accent dot per tab when running. WatchTile gains spinT for live arc spinner in watch_grid_panel.cpp. New fixture r_concurrent_agents (2 docs both building). New AI_CAD_SHOT=agent_multifile: eyeballed PNG shows both tiles with spinning arcs, feature_modeler+assembly_fit_engineer attribution. ui_style_check 0 findings. Committed 832a582b, gate.sh running.
- 2026-07-05 21:13 UTC [sonnet-vale1] gate.sh PASS (exit 0). ui/imgui PASS, check.py PASS, ui_style_check 0 findings, ctest PASS. Pytest FAIL-RECORDED are all pre-existing. Shot agent_multifile eyeballed: Watch Grid shows 2 tiles both building, spinning arcs, feature_modeler+assembly_fit_engineer attribution. Branch pushed: task/T-DP13-multifile-agent-view @ 832a582b. Ready for review.
