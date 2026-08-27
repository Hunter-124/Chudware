---
id: T-FIN13
title: PDN v1 - DC IR-drop solve on copper with current-density and I2R heat map
workstream: FIN
owner: fable-fin
status: done
branch: task/T-FIN13-ecad-pdn-irdrop
depends_on: [T-FIN8]
lane: ecad-firmware
kind: new
priority: 2
---

## Context

Resistive DC solve of power-net copper (pours/traces/vias meshed to a 2.5D resistor
network) with component current draws from T-FIN8 metadata as sinks and regulator outputs
as sources. Report per-component voltage drop vs tolerance, current-density hotspots, and
export the per-element I2R dissipation map — the trace-heating input for the thermal
handoff (T-FIN16). Pure numpy/scipy sparse solve; no external dep.

## Files / areas touched

- engine ecad pdn.py — copper extraction -> resistor mesh -> sparse solve
- services/mcp/ecad_tools.py — ecad_run_pdn tool
- aicad.boardphys/1 sidecar extension: i2r_map
- tests: analytic strip/plane cases + seeded undersized-trace fixture

## Definition of done

- Analytic validation: uniform strip and annular via cases within 5% of closed form
- Undersized 3A trace fixture flags drop + density; clean board passes
- I2R map emitted in the boardphys sidecar for thermal consumption
- Runtime acceptable on the demo board (<10s)
