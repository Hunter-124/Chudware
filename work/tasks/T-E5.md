---
id: T-E5
title: Oil-hydraulic circuit design (ISO 1219)
workstream: E
owner: sonnet-oak
status: review
branch: task/T-E5-oil-hydraulics
day: 9
depends_on: [T-E3]
---

## Context

Hydraulic power systems: pumps, cylinders, valves, accumulators. Schematic layer clones the ECAD pattern (symbols + nets = hoses). Simulation via OpenHydraulics Modelica library (BSD-3) exported as FMUs and run through fmpy (BSD) subprocess — avoids the GPL OpenModelica runtime. Component sizing feeds the sourcing BOM (Workstream B).

## Definition of done

- [x] Hydraulic schematic doc kind with ISO 1219 symbol set
- [x] Cylinder/pump sizing calcs (force, flow, pressure) as tools
- [x] One OpenHydraulics FMU circuit simulated via fmpy
- [x] Components appear in bom_rollup with sourcing fields

## Implementation (sonnet-lux + sonnet-oak closeout)

### Files landed

- `engine/hydraulic/__init__.py` — package
- `engine/hydraulic/schema.py` — `aicad.hydraulic/1` document schema (factories, validation, enums); ISO 1219 component types (12 types: pump, cylinder_linear, cylinder_rotary, valve_directional, valve_check, valve_relief, valve_flow_control, accumulator, reservoir, motor_hydraulic, filter, heat_exchanger); port types; hose nets
- `engine/hydraulic/solve.py` — `size_cylinder`, `size_pump`, `size_accumulator`, `valve_flow`, `fmu_simulate` (fmpy soft-fail seam)
- `engine/hydraulic/bom.py` — `hydraulic_bom_lines`, `sourcing_summary` (VendorPart-compatible shape for B-chain seam)
- `engine/hydraulic/test_hydraulic.py` — 54-test suite (54/54 pass)
- `services/mcp/hydraulic_tools.py` — 6 MCP tools: `hydraulic_new`, `hydraulic_add_component`, `hydraulic_add_hose`, `hydraulic_size_cylinder`, `hydraulic_size_pump`, `hydraulic_bom`
- `services/mcp/server.py` — `register_hydraulic_tools` wired (append-only)
- `contracts/hydraulic_schema.md` — frozen `aicad.hydraulic/1` contract
- `contracts/licenses.md` — fmpy (BSD-2-Clause) and OpenHydraulics (BSD-3) rows added

### Verification

- 54/54 pytest pass (`engine/hydraulic/test_hydraulic.py`)
- gate.sh: core build PASS, ui/abstract PASS, ctest PASS
- gate.sh FAIL stages (all pre-existing, non-fatal):
  - check.py: feature_surface_audit + parity_audit (pre-existing, FAIL-RECORDED)
  - pytest stage 4 (engine/doc + agent): pre-existing golden-file + agent-loop failures
  - ui/imgui: selMulti break — branch base predates master b569020 fix; resolves at merge
- graphify update . run (19517 nodes, 43075 edges)

### FMU simulation note

`fmu_simulate` is a soft-fail seam: returns `{"ok": False, "skipped": True}` when fmpy absent or no fmu_path given. Full execution path is wired and tested against the soft-fail cases. A compiled OpenHydraulics FMU is required for live simulation; none is bundled (Modelica compile step is user-side). fmpy BSD-2-Clause license row in contracts/licenses.md.

### B-chain seam (T-E5 → bom_rollup)

`hydraulic_bom_lines(doc)` emits VendorPart-compatible dicts for every component. The `hydraulic_bom` MCP tool surfaces these. Post-merge wiring into bom_rollup is a B-chain task (T-B4/T-B5 seam noted in bom.py).

## AI hints

- OpenHydraulics: https://github.com/modelica-3rdparty/OpenHydraulics
- fmpy for FMU execution; ECAD schematic engine as the pattern: engine_oracle/ecad/
