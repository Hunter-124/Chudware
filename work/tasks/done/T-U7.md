---
id: T-U7
title: New-doc-kind viewers v1 — piping, hydraulic, structural
workstream: U
owner: sonnet-wrenn
status: done
branch: task/T-U7-ui
day: 7
priority: 4
depends_on: [T-U1]
lane: app-gui
kind: new
---

## Context

Workstreams D/E created doc kinds (piping networks, ISO 1219 hydraulic schematics,
structural frames) that are MCP-only today — invisible in the app. R1 parity demands at
least view+inspect for every doc kind.

## Files / areas touched

- ui/abstract/{piping,hydraulic,structural}_view.h + one viewer panel each (scaffolded):
  2D schematic render (nodes/edges w/ ISO symbols for hydraulic; iso/plan view for
  piping; frame + section labels for structural), selection → inspector showing element
  params, solve-status strip (reuse T-U6 result rendering where shared)
- Read-only v1; edit = "open Copilot" affordance (doc-ops editing is a later wave)

## Definition of done

- [ ] Each doc kind opens in its viewer from the doc tabs; renders a real solved example
      from the merged test fixtures
- [ ] Element selection shows params + solve results; zero recomputation in UI
- [ ] Headless screenshots of all three; ui_style_check green; gate; graphify update .

## Driver notes (Fable)

- One task, three thin viewers — resist depth. SHARE the schematic-canvas code (one
  ui/abstract graph-canvas view-model, three configs). ISO 1219 symbol set: draw the
  ~12 needed primitives w/ ImDrawList, tokens.h colors — no image assets.
- This is deliberately v1 scope-fenced: full editors are future tasks the T-G12 loop
  will prioritize from Erik's feedback.
