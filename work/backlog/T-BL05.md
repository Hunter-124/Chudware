---
id: T-BL05
title: FPGA/HDL flow
workstream: backlog
owner: unassigned
status: backlog
branch: task/T-BL05-fpga-flow
depends_on: []
lane: ecad-firmware
kind: new
priority: 5
---

## Context

Verilator (Artistic-2.0, bundleable) + GHDL/Icarus (GPL, subprocess) with an agent write->simulate->assert loop mirroring the Renode firmware pattern.

## Definition of done

- [ ] hdl_build/hdl_sim MCP tools
- [ ] Agent loop demo: counter + UART peripheral verified in simulation

**PARKED (Erik, 2026-07-03): out of scope for the v1 prototype push — low priority; revisit after the five objectives close.**
