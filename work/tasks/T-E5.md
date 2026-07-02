---
id: T-E5
title: Oil-hydraulic circuit design (ISO 1219)
workstream: E
owner: unassigned
status: todo
branch: task/T-E5-hydraulic-power
day: 9
depends_on: [T-E3]
---

## Context

Hydraulic power systems: pumps, cylinders, valves, accumulators. Schematic layer clones the ECAD pattern (symbols + nets = hoses). Simulation via OpenHydraulics Modelica library (BSD-3) exported as FMUs and run through fmpy (BSD) subprocess — avoids the GPL OpenModelica runtime. Component sizing feeds the sourcing BOM (Workstream B).

## Definition of done

- [ ] Hydraulic schematic doc kind with ISO 1219 symbol set
- [ ] Cylinder/pump sizing calcs (force, flow, pressure) as tools
- [ ] One OpenHydraulics FMU circuit simulated via fmpy
- [ ] Components appear in bom_rollup with sourcing fields

## AI hints

- OpenHydraulics: https://github.com/modelica-3rdparty/OpenHydraulics
- fmpy for FMU execution; ECAD schematic engine as the pattern: engine_oracle/ecad/
