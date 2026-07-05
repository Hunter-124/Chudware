---
id: T-DP16
title: AI-Agent workspace — full-screen comprehensive copilot (DAG + worker steering + session mgmt)
workstream: DP
owner: unassigned
status: todo
branch: task/T-DP16-ai-agent-workspace
depends_on: [T-DP15, T-DP17]
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

Doc §9.2. The full-viewport AI-Agent workspace (like ECAD's takeover). The glass copilot
OVERLAY stays exactly as-is in modeling (same styling) — this is the comprehensive surface you
switch TO. Contains: the interactive DAG centerpiece (embed T-DP17), worker steering
(pause/resume/redirect/kill any agent/sub-agent; change a role's model/effort live;
approve/deny pending sub-agent spawns — ties to T-DP9 delegation), session management (list/
switch/fork/resume sessions; embed the multi-file view T-DP13), and the transparency stream
(embed T-DP11). Switching in replaces the viewport; switching out returns to the model with the
overlay intact. ui/imgui only; glass design; shot-verified, EYEBALL every PNG.

## Definition of done

- [ ] AI-Agent workspace renders the DAG centerpiece + steering rail + session list.
- [ ] Worker steering actions wired to the delegation/mission control seams (pause/resume/kill,
      model/effort change, spawn approve/deny).
- [ ] Session management: switch/fork/resume works against real sessions.
- [ ] Overlay copilot unchanged in modeling; comprehensive view only in this workspace.
- [ ] Shot scenarios (populated mission, empty state), EYEBALLED. ui_style_check clean; gate.sh PASS.
