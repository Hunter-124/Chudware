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
- **H3 drawing — DONE (17a7844).** ~1,560 LOC, workspace-gated in viewport.cpp (Drafting);
  shot renders full A3 sheet + 4 projected views. Surfaces cleanly (content-swap inside the
  Viewport window). Flagged a probable engine doc-id bug (scenario doc2 empty).
- **H2 ECAD — DONE (565eb96).** ~2,573 LOC (5 files incl. new ecad_palette seam), central-node
  dock ("ECAD"). Same headless surfacing gap as CAM (see below). Build green, shot exit 0.
- **H4 sim — DONE (dbd79fa).** ~2,255 LOC, workspace-gated overlay in viewport.cpp (Simulate);
  shot renders Simulate ribbon + "Simulation" column + 3D model — surfaces cleanly. Also wired
  a pre-existing FEA/CFD colormap-to-renderer gap. fea solve ~150-165s → use >120s shot timeout.
- **T-G13 shot fix merged in** (44fc56f) — T-G8 depends on it for headless shot verification.

### OPEN central follow-up — CAM/ECAD headless surfacing (application.cpp/layout.cpp)

CAM and ECAD are separate dock tabs sharing the central node with Viewport; the node's
`ImGuiDockNodeFlags_NoTabBar` does NOT honor `SetNextWindowFocus()` reliably headless, so the
cam/ecad shots show the Model viewport (ribbon/workspace bar are correct). Drawing + sim do NOT
have this problem because they content-swap/overlay INSIDE the Viewport window (workspace-gated)
rather than being separate dock tabs. FIX OPTIONS: (a) set the dock node's SelectedTabId via
`ImGui::DockBuilderGetNode(...)->SelectedTabId = ImGui::GetID("CAM"/"ECAD")` when that
workspace/mode is active; or (b) restructure CAM/ECAD to render inside the Viewport window like
drawing/sim. Bounded, high-value for Erik's walkthrough — do before G4 sign-off.

### Panel-dispatch wiring pattern (for H2–H5)

Central-node panels (CAM/ECAD/Viewport) are drawn as `Begin("<DockName>")/draw<Panel>(s)/End()`
blocks in `application.cpp::frame()`; the dock window must be pre-registered in `layout.cpp`.
Workspace-surfaced panels (drawing→Drafting, sim→Simulate, sketch→Sketch, ecad→Schematic/Board)
key off `st.workspace`, so their scenarios already switch workspace and DON'T hit H1's surfacing
gap. To avoid concurrent-edit conflicts, the LEAD wires application.cpp dispatch centrally;
subagents add only their `ui/imgui/src/ui/<node>/**` files. `ui/imgui/CMakeLists.txt:78`
GLOB_RECURSEs `src/*.cpp` — no CMakeLists edit needed for new files.

### Remaining work (H1-H4 DONE)

1. **H5 sketch (completion)** — host `sketch_mode.cpp` is 8,314 LOC; split into
   `ui/imgui/src/ui/sketch/{sketch_mode,tools,constraints,overlay,edit}.*` (the split IS the
   modularity payoff — do NOT port as one file). Uses F1 camera API. `ui/imgui/src/ui/sketch/`
   already exists from earlier waves — build on it. Verify `AI_CAD_SHOT=sketch` + `AI_CAD_SHOT=asm`.
2. **Diff harnesses** (integrator infra, `tools/`): run the SAME job on host AI-CAD + new AI-CAD2
   and byte/JSON-diff artifacts — G-code across 8 post dialects, netlist JSON + ERC, DXF/PDF
   export, study JSON. Since the compute is engine-side and RPCs were kept byte-identical, these
   should diff clean; the harness proves it.
3. **CAM/ECAD surfacing fix** (see central follow-up above).
4. **G4 gate**: `bash scripts/gate.sh` green + all domain diffs + `AI_CAD_SHOT=asm`.

Verify offscreen via `xvfb-run -a`, worker present (`AI_CAD_PYTHON=$(command -v python3)`);
flow needs ~240s, fea >120s. Prebuilt libaicad: `/home/hunter/Repositories/CAD/core/build-occt-linux/bin/libaicad.so`.
