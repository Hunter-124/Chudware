---
id: T-DP17
title: Interactive agent DAG / flowchart widget — clickable nodes to sub-agent detail
workstream: DP
owner: sonnet-mira1
status: review
branch: task/T-DP17-agent-dag-widget
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

Doc §9.3. A clickable node-graph widget (new file) of the running workflow: mission phases +
the build DAG (aicad.buildgraph/1) + the delegation tree (aicad.delegation/1 — who spawned
whom). Click a node → detail: sub-agent role, model, current tool call, ledger slice, token
spend, status (queued/running/blocked/done/failed) with live status colouring. Embeds compact
in the overlay copilot and large in the AI-Agent workspace (T-DP16). Reuses aicad.mission event
streams + the delegation tree. Self-contained widget — can start now. ui/imgui only; glass
design; dark-mode contrast; ui_style_check clean; EYEBALL shots.

## Definition of done

- [ ] Widget renders a live DAG from mission + buildgraph + delegation data.
- [ ] Node click opens detail (role/model/tool/ledger-slice/tokens/status); live colouring.
- [ ] Compact + full render modes both shot-verified, EYEBALLED.
- [ ] ui_style_check clean; gate.sh PASS; graphify update.

## Progress log
- 2026-07-05 21:13 UTC [sonnet-mira1] agent_dag_panel shipped: view model (agent_dag_view.h), panel .cpp/.h, wired into application.cpp + scenarios. Two shots: agent_dag (full+detail, pre-selected node n_geom) and agent_dag_compact (graph-only). Build clean, ui_style_check 0 findings. Branch pushed; gate.sh running.
- 2026-07-05 21:31 UTC [sonnet-mira1] T-DP17 ready for review. Branch: task/T-DP17-agent-dag-widget, tip b21ab4e7.

Shipped:
- ui/abstract/agent_dag_view.h — pure view-model (DagWidgetNode, DagWidgetEdge, AgentDagModel, DagWidgetMode, status/kind enums)
- ui/imgui/src/ui/agent_dag_panel.h/.cpp — full widget (~640 lines): JSONL event projection, Kahn topo-sort layered layout, ImDrawList bezier-edge canvas, glass detail pane, T-U1 scaffold integration
- application.cpp — one Begin/End window block wiring the panel
- scenarios.cpp — fixture with 6 nodes across 5 layers (mission + 3 delegation sub-agents + verify + gate) seeding both shots

Shot scenarios (HEADLESS, eyeballed):
- AI_CAD_SHOT=agent_dag (full mode, 1600x1000): floating Agent DAG window, all 6 nodes visible, bezier edges, detail pane for geometry_designer (role/model/tool/ledger/tokens), status colors: teal=done, teal-highlight=running, amber=blocked, red=failed
- AI_CAD_SHOT=agent_dag_compact (compact mode, 1600x1000): canvas-only embedded panel, all 6 nodes including red failed VFY and amber blocked GATE visible at depth 4, no scaffold header

Fixes from advisor review (commit b21ab4e7):
- agentDagShotForceMode() seam: compact branch now has real execution coverage
- kLayerGapY 80->50: depth-4 nodes no longer clip
- ASCII glyphs: "..." and "*" replace UTF-8 ellipsis/bullet (no tofu)
- Mission name ASCII (colon not em-dash)

ui_style_check: 0 findings. Build: 0 new warnings. gate.sh running.
