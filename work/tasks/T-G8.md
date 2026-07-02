---
id: T-G8
title: GUI migration wave 4 - hard subsystems (H1-H5, G4 gate)
workstream: G
owner: unassigned
status: todo
branch: task/T-G8-migration-wave4
day: 9
depends_on: [T-G7]
---

## Context

Fourth wave of `docs/GUI_MIGRATION_DAG.md`: the domain-heavy subsystems — H1 CAM, H2
ECAD, H3 drawing sheets, H4 simulation, H5 sketch completion — closed by the G4 gate.
These are verified by BYTE/JSON DIFFS against host output (G-code across all 8 post
dialects, netlist+ERC, DXF/PDF export, study JSON), not just screenshots — that's the
anti-spaghetti guarantee that behavior survived the port.

## Files / areas touched

- `ui/imgui/src/ui/cam/**`, `ui/imgui/src/ui/ecad/**`, `ui/imgui/src/ui/drawing_panel.*`,
  `ui/imgui/src/ui/sim_manager.*`, `ui/imgui/src/ui/sketch_*.cpp` per DAG write-sets
- `tools/` diff scripts (integrator)

## Steps

1. Read `docs/GUI_MIGRATION_DAG.md` H1-H5 + G4.
2. Port each subsystem per its node spec; H5 splits host's 9.3k-line `sketch_mode.cpp`
   into `sketch_mode/tools/constraints/overlay/edit` files using F1's camera API — this
   split IS the modularity payoff, don't port it as one file.
3. Build the diff harnesses (G-code, netlist JSON, DXF/PDF, study JSON) and run them
   against the same jobs on both binaries.
4. G4 gate: all domain diffs green + `AI_CAD_SHOT=asm` screenshot.

## Definition of done

- [ ] G-code byte-diff green for all 8 post dialects
- [ ] Netlist JSON + ERC identical; DXF/PDF export byte-diff green; study JSON diff green
- [ ] Sketch scenario + solver op-batch diff vs host green
- [ ] `bash scripts/gate.sh` green; `graphify update .` run

## AI hints

- Port from current host/src: sketch DOF pill is top-center, accept/deny lives in the
  Sketch ribbon tab (`sketch_exit`/`sketch_cancel`), overlays dodge columns via
  `overlayColumnEdge(0/1)`.
- `AI_CAD_SHOT=flow` needs a ~240s timeout.
