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

## Progress log
- 2026-07-06 02:18 UTC [sonnet-clay1] DONE — branch task/T-DP15-workspace-switching pushed, status: review.

What changed:
- WorkspaceKind enum (OverlayHosting/FullViewport) + workspaceKind() + globalWorkspaces() added to ui/abstract/command_registry.{h,cpp}
- AiAgent + PlmManagement added to Workspace enum with labels ("AI Agent", "PLM") and icons ("ai_design", "history")
- workspace_bar.cpp: second chip cluster for global workspaces (doc-type independent), separated by divider
- application.cpp: drawLeftPane/drawRightPane gated on cmd::workspaceKind(s.effectiveWorkspace()) == OverlayHosting
- workspace_shells.{h,cpp}: new placeholder shell panels for AiAgent and PlmManagement full-viewport bodies (deferred to T-DP16/R-stack)
- scenarios.h/cpp: 4 new shot scenarios: ws_overlay_hosting, ws_full_viewport_ecad, ws_ai_agent, ws_plm

Verification:
- All 4 shots eyeballed: glass columns present in ws_overlay_hosting (Model), absent in ws_full_viewport_ecad (Schematic), ws_ai_agent, ws_plm
- ui_style_check.py: 0 findings on all touched files
- gate.sh RESULT: PASS (FAIL-RECORDED items are pre-existing T-V2, ui/imgui build+shot+selftest PASS)
- Build clean in both ui/imgui/build (dev) and build/linux-app (gate preset)
