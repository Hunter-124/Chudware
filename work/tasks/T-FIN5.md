---
id: T-FIN5
title: Firmware workspace GUI (ui/imgui) - project browser, build console, serial monitor, emulation trace
workstream: FIN
owner: unassigned
status: todo
branch: task/T-FIN5-fw-gui-workspace
depends_on: [T-FIN1, T-FIN4]
lane: app-gui
kind: new
priority: 3
---

## Context

The firmware pillar is MCP-only today — no desktop surface at all. Add a firmware
workspace to the imgui GUI (ui/imgui is the sole GUI target per T-G24): project list +
sources panel, build button + parsed-warnings console, emulation run panel (pick platform +
scenario, run, live UART console), and a GPIO/waveform trace viewer reading the T-FIN1
trace artifact. Flash/debug buttons appear only when fw_status says the backend exists.
Progressive disclosure: workspace offered when a firmware project or ECAD doc is open,
consistent with the Simulate-tab disclosure rules (T-FIN24).

## Files / areas touched

- ui/imgui/ — firmware workspace panel (follow existing workspace-tab patterns)
- services/worker — fw_* endpoint routing for the GUI worker (mirror MCP handlers)
- headless scenario shot proving: project visible, build console output, emulation trace rendered

## Definition of done

- Create/build/emulate a project entirely from the GUI against the live engine
- Serial monitor streams UART from emulation; trace viewer shows GPIO timeline
- Honest gating: missing renode/toolchain shows the fw_status guidance, not a dead button
- Light+dark verified; scenario shot wired for regression
