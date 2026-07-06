---
id: T-DP15
title: Workspace-switching architecture — full-viewport workspaces + overlay-hosting workspaces
workstream: DP
owner: sonnet-clay1
status: doing
branch: task/T-DP15-workspace-switching
depends_on: []
lane: app-gui
kind: new
priority: 2
---

## Context

Presentation layer of the guided design pipeline — see
[docs/AGENT_DESIGN_PIPELINE.md](../../docs/AGENT_DESIGN_PIPELINE.md) §9 (Hunter directive,
2026-07-05). MANDATORY: graphify to orient before editing; REUSE existing ui/imgui workspace
machinery (workspace_bar.cpp, doc-tabs, the ECAD full-viewport takeover pattern, the glass
copilot overlay column, agent_panel, watch_grid), aicad.mission event streams,
aicad.delegation/1 tree, aicad.buildgraph/1. ui/imgui is the ONLY live GUI (host/ reference-
only). Design language LOCKED: pinned glass columns, no docked layout, dark-mode contrast per
the T-U1 kit, ui_style_check clean. Verify HEADLESS only (xvfb-run AI_CAD_SHOT=...), never
launch headed; EYEBALL every shot PNG. Numbers cite real output.

## Scope

Doc §9.1. A single registry of workspaces in ui/imgui, each declaring its toolbar/menu
context and whether it is FULL-VIEWPORT (owns the screen, like ECAD) or OVERLAY-HOSTING
(viewport + pinned glass copilot column). Switching is first-class in the existing
workspace_bar.cpp. Register: Modeling/Sketch/Assembly/Drawing/Simulation/CAM (overlay-hosting),
ECAD (full-viewport, exists — fit it into the registry), AI-Agent + PLM-management
(full-viewport, NEW — their bodies land in T-DP16 and the R-stack; this task provides the
switch framework + empty shells). Settings STAYS a modal (T-G22 consolidation — do not make it
a workspace). Each workspace restores its toolbar/panels/scene on entry. ui/imgui only; host/
reference-only; glass design; ui_style_check clean.

## Definition of done

- [ ] Workspace registry + switch framework; each entry declares full-viewport vs overlay-hosting.
- [ ] ECAD refit into the registry with no regression; Modeling keeps the glass overlay.
- [ ] AI-Agent + PLM-management registered as full-viewport shells (bodies deferred to DP16/R).
- [ ] Copilot overlay persists in overlay-hosting workspaces, absent in full-viewport ones.
- [ ] Shot scenarios: switch to each workspace kind, EYEBALLED. ui_style_check clean; gate.sh PASS.
