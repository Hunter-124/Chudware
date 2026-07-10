---
id: T-FIN22
title: Hydraulic<->piping<->FEA couplings - hose runs solve + actuator loads to FEA
workstream: FIN
owner: fable-fin
status: done
branch: task/T-FIN22-hydraulic-piping-fea
depends_on: [T-FIN15]
lane: simulation
kind: new
priority: 4
---

## Context

Two cheap, high-value couplings on the simhandoff seam: (a) hydraulic circuits
(ISO 1219 model) hand their hose/tube runs to the piping 1D solver for real pressure-drop
per line at the circuit operating point, replacing per-hose estimates — results flow back
into hydraulic sizing margins; (b) hydraulic_fea_handoff takes cylinder/actuator mount
loads at max pressure and generates static FEA load cases on the mounting geometry
(companion of T-FIN20's pattern).

## Files / areas touched

- services/mcp/hydraulic_tools.py — piping handoff + fea handoff tools
- services/mcp/piping_tools.py — accept externally-supplied network from hydraulic docs
- tests: circuit fixture where hose losses change pump margin; mount-load FEA case

## Definition of done

- Hydraulic circuit line losses solved via piping engine with per-line results
- Cylinder mount load case lands in FEA with correct force magnitude/direction
- Both conform to simhandoff/1 with verify steps where a cross-check exists
