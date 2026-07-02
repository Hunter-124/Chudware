---
id: T-BL05
title: FPGA/HDL flow
workstream: backlog
owner: unassigned
status: backlog
branch: task/T-BL05-fpga-flow
depends_on: []
---

## Context

Verilator (Artistic-2.0, bundleable) + GHDL/Icarus (GPL, subprocess) with an agent write->simulate->assert loop mirroring the Renode firmware pattern.

## Definition of done

- [ ] hdl_build/hdl_sim MCP tools
- [ ] Agent loop demo: counter + UART peripheral verified in simulation
