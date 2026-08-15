# ECAD (schematic/PCB/sim/Gerber)

Scope: services/ecad/, services/ecad_design/, core/ecad/
Contracts: contracts/ecad/ (ecad_ops.md, ecad_rpc.md, fab_outputs.md, library.md, netlist.md, pcb_schema.md, sch_schema.md, sim.md), contracts/worker_rpc.md

## Entry points
- services/ecad/session.py — EcadSession: open/save/apply_ops/rebuild/undo-redo state holder
- services/ecad/schema.py — board/schematic data model
- services/ecad/drc.py — PCB design-rule checks
- services/ecad/zonefill.py — copper pour fill
- services/ecad/impedance.py — trace impedance checks
- services/ecad/fab/gerber.py, excellon.py, bom.py, placement.py — Gerber/drill/BOM/CPL export
- services/ecad/threed.py — 3D bridge (PCB → STEP part doc)
- services/ecad/library/{kicad_mod.py,kicad_sym.py,native.py,sexpr.py,starter.py} — KiCad-compatible + native symbol/footprint libraries
- services/ecad_design/orchestrator.py — AI design-from-prompt pipeline
- services/mcp/ecad_tools.py and services/mcp/ecad_{sch,pcb,qa,sim}_tools.py — MCP-facing wrappers
- core/ecad/*.cpp — C++ implementations for autoroute, DRC, ERC, netlist, session, zone fill, DFM, impedance, library, recipes, sync, and fabrication formats

## How it connects
The MCP ECAD tools route stateful board operations through the active services and C++ ECAD implementations. `services/ecad/session.py` holds service-side state, `services/ecad_design/orchestrator.py` owns design orchestration, and `core/ecad/session.cpp` is the native document session.

## Debugging playbook
- Python tests: `<venv>/python -X utf8 -m pytest services/ecad services/mcp/test_ecad_tools.py -q`
- MCP-layer tests also include `services/mcp/test_ecad_hardening.py`
- If touching `core/ecad/*.cpp`, build/run the core ctest suite (see build-test.md) and check for a matching parity test.

## Graphify starters
- graphify query "services/ecad core/ecad session drc zonefill gerber"
- graphify explain "EcadSession"
- graphify path "ecad_tools.py" "session.py"

## Related tasks/docs
- contracts/ecad/*.md (frozen RPC/schema contracts — read before changing wire shapes)
- docs/review/GEOMETRY_LLM_REPRESENTATION_REVIEW.md (GSL layer touches ECAD description)
- FIN workstream (2026-07-10): board physics, host-board/SI/FPGA/PDN packs, thermal handoff,
  heatsink loop, and power budget

## FIN verification + physics handoffs (2026-07-10)
- Metadata keystone: `aicad.boardphys/1` sidecar from `ecad_to_3d` (T-FIN8) — power/thermal/
  package/rail blocks on every component body
- Pre-fab packs: `ecad_run_hostcheck` (T-FIN9), `ecad_run_sicheck` (T-FIN10),
  `ecad_fpga_check` + constraints export (T-FIN11), `ecad_run_pdn` IR-drop (T-FIN13),
  stackup advisor (T-FIN12), optional ngspice backend (T-FIN14)
- Physics: `ecad_thermal_handoff` / `ecad_verify_thermal` (T-FIN16), `heatsink_select`
  (T-FIN17, `services/mcp/heatsink.py` + `heatsink_select_impl` for worker seam),
  `ecad_airflow_handoff` (T-FIN19), `ecad_vibe_check` (T-FIN21), `power_budget` (T-FIN23)
- E2E: `scripts/demo_sensor_node.py` (T-FIN25); narrative docs/reference/FIN25_SENSOR_NODE_GOLDEN_DEMO.md
