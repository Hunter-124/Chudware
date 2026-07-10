---
id: T-FIN16
title: ECAD->thermal FEA handoff - board study with auto heat sources, budget verify
workstream: FIN
owner: unassigned
status: todo
branch: task/T-FIN16-ecad-thermal-handoff
depends_on: [T-FIN8, T-FIN13, T-FIN15]
lane: simulation
kind: new
priority: 2
---

## Context

The marquee integration: one call takes a routed board to a thermal FEA study.
ecad_thermal_handoff builds the 3D board+components solid (T-FIN8), assigns effective
material conductivities (FR4 + copper-fraction-weighted layers — document the
homogenization), applies heat sources from component power metadata (+ optional trace I2R
map from T-FIN13), convection BCs from an environment param (still air / forced / cold
plate presets), runs run_fea_thermal, and maps results back to components (Tj estimate via
theta_jc + case temp). ecad_verify_thermal compares against component temperature limits ->
per-component pass/margin verdict. Conforms to aicad.simhandoff/1.

## Files / areas touched

- services/mcp/ecad_tools.py or new ecad_thermal.py — handoff + verify tools
- engine_oracle/ecad/threed.py — FEA-ready solid variant (merged board stack, component blocks)
- core/sim/fea_thermal — no solver change expected; loads generated from sidecar
- tests: regulator-dissipation demo board, hand-calc cross-check (theta_ja sanity)

## Definition of done

- One call from routed demo board -> solved temperature field with zero manual BC setup
- Per-component Tj/limit table with margins; verify verdict matches hand calc within stated tolerance
- Trace I2R inclusion toggleable and visibly changes hotspots on the PDN fixture
- Homogenization assumptions documented in tool output + cad_guide
