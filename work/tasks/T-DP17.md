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
