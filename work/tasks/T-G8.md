---
id: T-G8
title: GUI migration wave 4 - hard subsystems (H1-H5, G4 gate)
workstream: G
owner: opus-sky
status: doing
branch: task/T-G8-migration-wave4
day: 9
priority: 2
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

## Orchestration plan + state (opus-sky, 2026-07-02)

Current state at claim: NOTHING is ported yet — `ui/imgui/src/ui/` has no `cam/` or `ecad/`
dir, no `drawing_panel.*`, no `sim_manager.*` (only `sketch/` exists from earlier waves).
No diff harnesses in `tools/`. Host source to port: cam (cam_panel+cam_mirror, 718 LOC),
ecad (panel+canvas+mirror, 2,501), drawing_panel (2,197), sim_manager (4,026), sketch_mode
(8,314 — split into sketch_mode/tools/constraints/overlay/edit). ~18k LOC total.

This is a swarm-integrator task: one Sonnet subagent per node (H1..H5), each porting its
write-set into the new tree following an already-ported panel as the structural template,
building + shot-verifying offscreen. Nodes are largely independent EXCEPT they share
`ui/imgui/CMakeLists.txt` and `build/linux-app` — serialize builds via
`flock /tmp/g8-build-linux.lock`, and integrate CMakeLists edits centrally (lead) to avoid
concurrent-edit conflicts. Recommended order by size/risk: H1 CAM (smallest, proves the
pattern) → H3 drawing → H2 ECAD → H4 sim → H5 sketch split (hardest).

Verification reality: the DoD byte/JSON diffs (G-code across 8 posts, netlist+ERC, DXF/PDF,
study JSON) compare the NEW panel's engine-driven output against the HOST binary's — the CAM
post/ECAD netlist/etc. computation lives in the ENGINE (`cam_*`/`ecad_*`/sim worker RPCs),
not the panel, so a faithful panel port that drives the identical engine calls should diff
clean. Building those diff harnesses (run same job on both binaries, compare artifacts) is
integrator infra — treat as a distinct sub-deliverable. Shot verification (`AI_CAD_SHOT=cam/
ecad/drawing/fea/flow/sketch/asm`) now works headless thanks to T-G13 (issue #7 fix); real
captures need `AI_CAD_PYTHON`/venv provisioned.

### Progress

- **H1 CAM — DONE (commit e158e31).** Ported `ui/imgui/src/ui/cam/{cam_panel,cam_mirror}`
  (718 LOC) following the `preview_pane` body-only convention; RPCs byte-identical to host.
  Dispatch site: `application.cpp` `Begin("CAM")/drawCamManager(s)` after the Viewport block
  (the "CAM" dock window was pre-registered in `layout.cpp:52`). Build green; `AI_CAD_SHOT=cam`
  captures exit 0. GAP: no Manufacture/CAM `Workspace` enum value exists, so the cam scenario
  shows the Model viewport, not the CAM panel — surfacing it needs a CAM workspace (ribbon +
  command_registry, outside cam/**) or a persistent focus mechanism. Backplot-over-viewport
  overlay + agent-v2 camLoadJob auto-load deferred (viewport.cpp / agent drain outside write-set).
- **T-G13 shot fix merged in** (44fc56f) — T-G8 depends on it for headless shot verification.

### Panel-dispatch wiring pattern (for H2–H5)

Central-node panels (CAM/ECAD/Viewport) are drawn as `Begin("<DockName>")/draw<Panel>(s)/End()`
blocks in `application.cpp::frame()`; the dock window must be pre-registered in `layout.cpp`.
Workspace-surfaced panels (drawing→Drafting, sim→Simulate, sketch→Sketch, ecad→Schematic/Board)
key off `st.workspace`, so their scenarios already switch workspace and DON'T hit H1's surfacing
gap. To avoid concurrent-edit conflicts, the LEAD wires application.cpp dispatch centrally;
subagents add only their `ui/imgui/src/ui/<node>/**` files. `ui/imgui/CMakeLists.txt:78`
GLOB_RECURSEs `src/*.cpp` — no CMakeLists edit needed for new files.

### Remaining nodes (recommended order)

H3 drawing (2,197 LOC, surfaces via Drafting ws) → H2 ECAD (2,501, ecad_panel+canvas+mirror,
Schematic/Board ws) → H4 sim (4,026, Simulate ws, needs F4 sim_client) → H5 sketch (8,314 →
split into sketch_mode/tools/constraints/overlay/edit). Then the diff harnesses (G-code 8 posts,
netlist+ERC, DXF/PDF, study JSON) + G4 gate. Verify each offscreen via its `AI_CAD_SHOT` name
(worker present: `AI_CAD_PYTHON=$(command -v python3)`; flow needs ~240s timeout).
