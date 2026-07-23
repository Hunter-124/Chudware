# ECAD (schematic/PCB/sim/Gerber)

Scope: engine/ecad/ (= engine_oracle/ecad/, root `engine` symlink), core/ecad/ (C++ port in progress)
Contracts: contracts/ecad/ (ecad_ops.md, ecad_rpc.md, fab_outputs.md, library.md, netlist.md, pcb_schema.md, sch_schema.md, sim.md), contracts/worker_rpc.md

## Entry points
- engine/ecad/session.py — EcadSession: open/save/apply_ops/rebuild/undo-redo state holder
- engine/ecad/rpc.py:46-829 — RPC handlers: `_ecad_new/_ecad_open/_ecad_save/_ecad_apply_ops/_ecad_rebuild/_ecad_tree/_ecad_undo/_ecad_redo/_ecad_resolve_pick/_ecad_describe/_ecad_export/_ecad_to_3d/_ecad_update_from_sch/_ecad_dfm/_ecad_autoroute/_ecad_fill_zones/_ecad_design/_ecad_simulate`
- engine/ecad/orchestrator.py:364 `build_design`, :665 `run_design`, :552 `simulate_design` — AI design-from-prompt pipeline (plan → build → ERC → deterministic-fix loop → sim)
- engine/ecad/schema.py — board/schematic data model, `new_board`
- engine/ecad/netlist.py, engine/ecad/impedance.py — netlist extraction, trace impedance checks
- engine/ecad/drc.py:494 `ratsnest()`, :594 `run_drc()` — PCB design-rule checks + unrouted-net ratsnest
- engine/ecad/erc.py — schematic electrical-rule checks
- engine/ecad/autoroute.py:675 `autoroute()` — grid/A* autorouter incl. diff-pair routing (:1215 `route_diff_pair`)
- engine/ecad/zonefill.py, engine/ecad/dfm.py — copper pour fill, DFM checks
- engine/ecad/fab/gerber.py, excellon.py, bom.py, placement.py — Gerber/drill/BOM/CPL export
- engine/ecad/threed.py — `board_to_step` — 3D bridge (PCB → STEP part doc)
- engine/ecad/library/{kicad_mod.py,kicad_sym.py,native.py,sexpr.py,starter.py} — KiCad-compatible + native symbol/footprint libraries
- engine/ecad/formats.py — .ecad+/.kicad_sch/.kicad_pcb import-export
- mcp_server/ecad_tools.py — MCP-facing wrappers (the `ecad_*` tools in this session's tool list)
- core/ecad/*.cpp — C++ port already exists for autoroute, drc, erc, netlist, session, zonefill, dfm, impedance, library, recipes, sync, fabformats (in-flight cutover; verify current parity before assuming Python is authoritative)

## How it connects
`mcp_server/ecad_tools.py` calls the engine **directly in-process** (not via the worker JSON-RPC socket) per contracts/ecad/ecad_rpc.md:43 — different from the CAD (part) MCP path, which does go through the worker/libchudware routing. `ecad_apply_ops` runs sch→netlist+ERC or pcb→DRC+ratsnest **synchronously** unless it would block too long, in which case `stats.partial=true` and the GUI issues an async `ecad_rebuild`. `_ecad_to_3d` bridges a PCB board into a normal CAD part doc for STEP export (fab_outputs.md §5). README states "Still Python-only: ECAD" but `core/ecad/*.cpp` already mirrors most Python modules 1:1 (autoroute.cpp, drc.cpp, erc.cpp, netlist.cpp, session.cpp, zonefill.cpp) — treat that claim as possibly stale and check `core/tests/` / parity corpus before relying on it.

## Debugging playbook
- Run one Python test module: `<venv>/python -X utf8 -m engine.ecad.test_drc` (house style: plain `main()` + asserts, `if __name__ == "__main__":` at file end — see every `engine/ecad/test_*.py`)
- Other module tests: `test_autoroute.py`, `test_autoroute2.py` (diff-pair/meander cases), `test_erc.py`(not confirmed — check `ls engine/ecad/test_*.py`), `test_dfm.py`, `test_fab.py`, `test_formats.py`, `test_hierarchy.py`, `test_impedance.py`, `test_integration.py`, `test_library.py`, `test_netlist.py`, `test_ops.py`, `test_orchestrator.py`, `test_rpc.py`, `test_sync.py`, `test_threed.py`, `test_zonefill.py`, `test_autosave.py`
- MCP-layer tests: `services/mcp/test_ecad_hardening.py` (timeout/slow-autoroute handling), `mcp_server/test_ecad_tools.py`
- If touching `core/ecad/*.cpp`, build/run the core ctest suite (see build-test.md) and check for a matching parity test before assuming Python-only.

## Graphify starters
- graphify query "engine/ecad rpc orchestrator drc erc autoroute gerber"
- graphify explain "autoroute()"
- graphify path "ecad_tools.py" "EcadSession"

## Related tasks/docs
- contracts/ecad/*.md (frozen RPC/schema contracts — read before changing wire shapes)
- docs/GEOMETRY_LLM_REPRESENTATION_REVIEW.md (GSL layer touches `_ecad_describe`)
- FIN workstream (2026-07-10): boardphys + host-board/SI/FPGA/PDN packs, thermal handoff,
  heatsink loop, power budget — see docs/SIM_FW_FINALIZE_PLAN_2026-07-10.md

## FIN verification + physics handoffs (2026-07-10)
- Metadata keystone: `aicad.boardphys/1` sidecar from `ecad_to_3d` (T-FIN8) — power/thermal/
  package/rail blocks on every component body
- Pre-fab packs: `ecad_run_hostcheck` (T-FIN9), `ecad_run_sicheck` (T-FIN10),
  `ecad_fpga_check` + constraints export (T-FIN11), `ecad_run_pdn` IR-drop (T-FIN13),
  stackup advisor (T-FIN12), optional ngspice backend (T-FIN14)
- Physics: `ecad_thermal_handoff` / `ecad_verify_thermal` (T-FIN16), `heatsink_select`
  (T-FIN17, `services/mcp/heatsink.py` + `heatsink_select_impl` for worker seam),
  `ecad_airflow_handoff` (T-FIN19), `ecad_vibe_check` (T-FIN21), `power_budget` (T-FIN23)
- E2E: `scripts/demo_sensor_node.py` (T-FIN25); narrative docs/FIN25_SENSOR_NODE_GOLDEN_DEMO.md
