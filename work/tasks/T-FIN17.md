---
id: T-FIN17
title: Heatsink selection loop - parts-library candidates solved to a Tj target
workstream: FIN
owner: fable-fin
status: done
branch: task/T-FIN17-heatsink-selection-loop
depends_on: [T-FIN16]
lane: simulation
kind: new
priority: 2
---

## Context

Close the 'pick a heatsink' loop Hunter asked for: given a hot component from
T-FIN16 and a Tj/case target, pull candidate heatsinks (parts library heatsink category +
parametric generator for plain finned sinks), mount candidate on the component (simple
bonded contact + TIM resistance), re-run the thermal study per candidate (optionally with
forced-air h presets), and return a ranked table (achieved Tj, mass, cost when known)
with the winning study kept. Sequential re-solves are fine; cache the board mesh.

## Files / areas touched

- services/mcp/ecad_thermal.py — heatsink_select tool + parametric finned-sink generator
- parts library: heatsink taxonomy entries (thermal resistance metadata where sourced)
- tests: known hot regulator picks a sink that meets target; impossible target reported honestly

## Definition of done

- Demo: 3W regulator at 85C limit -> ranked candidates, winner meets target in re-solve
- Parametric sink generator produces valid geometry across its parameter range
- Result envelope conforms to simhandoff verify shape; documented in cad_guide('simulation')
