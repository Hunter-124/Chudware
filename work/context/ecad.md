# ECAD (schematic/PCB/sim/Gerber)

Scope: services/ecad/, services/ecad_design/, core/ecad/
Contracts: contracts/ecad/ (ecad_ops.md, ecad_rpc.md, fab_outputs.md, library.md, netlist.md, pcb_schema.md, sch_schema.md, sim.md), contracts/worker_rpc.md
Canonical guidance: .claude/skills/chudware-dev/SKILL.md; GUI changes use gui-orchestrator and gui-workspaces.

## Entry points
- `services/ecad/session.py` — service API forwarding to `services/mcp/ecad_common.py`'s RPC-backed `EcadSession`; the C++ session owns document state.
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
MCP tools and the design orchestrator use the `ecad_common` facades → `core_bridge.call` → hostd/libchudware → `core/ecad/session.cpp`. Python keeps orchestration, metadata, libraries and physics handoffs without a native counterpart; it is not a second board kernel.

## Debugging playbook
- Integrator Python checks: `<venv>/python -X utf8 -m pytest services/ecad -q`, then `<venv>/python -X utf8 -m pytest services/mcp/test_ecad_tools.py services/mcp/test_ecad_hardening.py -q`.
- Native ECAD changes: `./build.sh core`, then `ctest --test-dir core/build-linux -R ecad --output-on-failure` (see build-test.md; use a private hostd for MCP tests).
- Frozen schemas and revision checks remain authoritative; never replace a user's live board/session as a side effect of a tool call.

## Graphify starters
- graphify query "services/ecad core/ecad session drc zonefill gerber"
- graphify explain "EcadSession"
- graphify path "ecad_tools.py" "session.py"

## Related tasks/docs
- contracts/ecad/*.md (frozen RPC/schema contracts — read before changing wire shapes)
- docs/review/GEOMETRY_LLM_REPRESENTATION_REVIEW.md (GSL layer touches ECAD description)

## Physics handoffs
- `aicad.boardphys/1` from `ecad_to_3d` carries component power/thermal/package/rail metadata.
- Pre-fab tools: `ecad_run_hostcheck`, `ecad_run_sicheck`, `ecad_fpga_check`, `ecad_run_pdn`, stackup advice and optional ngspice.
- Physics tools: `ecad_thermal_handoff`, `ecad_verify_thermal`, `heatsink_select`, `ecad_airflow_handoff`, `ecad_vibe_check`, `power_budget`.
- `scripts/demo_sensor_node.py` and `docs/reference/FIN25_SENSOR_NODE_GOLDEN_DEMO.md` cover the cross-domain workflow; report missing external solver capabilities honestly.
