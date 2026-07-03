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

CAM and ECAD are separate dock tabs sharing the central node with Viewport; the node hides its
tab bar (`ImGuiDockNodeFlags_NoTabBar`), so the cam/ecad shots show the Model viewport (ribbon/
workspace bar ARE correct). Drawing + sim do NOT have this problem because they content-swap/
overlay INSIDE the Viewport window (workspace-gated) rather than being separate dock tabs.

TRIED + FAILED (opus-sky, both proven not to surface the panel headless — do NOT re-attempt):
(1) per-frame `SetNextWindowFocus()` on the CAM/ECAD window (committed H1/H2 code);
(2) setting the dock node's `SelectedTabId = w->TabId` directly each frame after submitting all
three central windows (via `FindWindowByName`/`w->DockNode`, imgui_internal). Both build + shot
exit 0 but the central area still renders the 3D Viewport — the Viewport window (docked LAST in
layout.cpp:53, and the one doing the GL render under `PassthruCentralNode`) stays the selected
tab regardless. Likely the tab-bar-hidden node's selection is re-derived from focus/GL-passthru,
not `SelectedTabId`.

RECOMMENDED FIX = option (b), the proven pattern: render CAM + ECAD INSIDE the Viewport window
as a workspace/mode-gated content swap (exactly like H3 drawing's `if (workspace==Drafting){
drawDrawingSheet(s); return; }` in viewport.cpp, and H4 sim's `drawEmbedded`). CAM needs a
mode/flag signal (there is no Manufacture Workspace enum — add one, or gate on an st.camActive
flag set by the cam scenario/command). This removes CAM/ECAD from the central dock node entirely.
Bounded, high-value for Erik's walkthrough — do before G4 sign-off. Panels render + execute
correctly TODAY (verified via frame trace); only the headless which-tab-is-forward is affected.

### Panel-dispatch wiring pattern (for H2–H5)

Central-node panels (CAM/ECAD/Viewport) are drawn as `Begin("<DockName>")/draw<Panel>(s)/End()`
blocks in `application.cpp::frame()`; the dock window must be pre-registered in `layout.cpp`.
Workspace-surfaced panels (drawing→Drafting, sim→Simulate, sketch→Sketch, ecad→Schematic/Board)
key off `st.workspace`, so their scenarios already switch workspace and DON'T hit H1's surfacing
gap. To avoid concurrent-edit conflicts, the LEAD wires application.cpp dispatch centrally;
subagents add only their `ui/imgui/src/ui/<node>/**` files. `ui/imgui/CMakeLists.txt:78`
GLOB_RECURSEs `src/*.cpp` — no CMakeLists edit needed for new files.

### Remaining work (H1-H4 DONE)

1. **H5 sketch — ALREADY PORTED (earlier waves; H5 "spans waves 2-4").**
   `ui/imgui/src/ui/sketch/` is already 8,631 LOC across the modular split
   (sketch_mode/tools/constraints/edit/overlay/internal/seam/palette + airfoil) — the split
   payoff is DONE. `AI_CAD_SHOT=sketch` already renders (Sketch ribbon + "Fully Defined" + real
   geometry — verified during T-G13). Remaining H5 DoD is only the **solver op-batch diff vs
   host** (a harness, item 2), NOT more porting. Confirm `AI_CAD_SHOT=asm` too.
2. **Diff harnesses** (integrator infra, `tools/`): run the SAME job on host AI-CAD + new AI-CAD2
   and byte/JSON-diff artifacts — G-code across 8 post dialects, netlist JSON + ERC, DXF/PDF
   export, study JSON. Since the compute is engine-side and RPCs were kept byte-identical, these
   should diff clean; the harness proves it.
3. **CAM/ECAD surfacing fix** (see central follow-up above).
4. **G4 gate**: `bash scripts/gate.sh` green + all domain diffs + `AI_CAD_SHOT=asm`.

Verify offscreen via `xvfb-run -a`, worker present (`AI_CAD_PYTHON=$(command -v python3)`);
flow needs ~240s, fea >120s. Prebuilt libaicad: `/home/hunter/Repositories/CAD/core/build-occt-linux/bin/libaicad.so`.

## Progress log
- 2026-07-02 23:54 UTC [opus-sky] WIP branch pushed. Done: H1 CAM (e158e31), H3 drawing (17a7844) — both build green, shots verified via the merged T-G13 fix. H2 ECAD in progress. Remaining: H4 sim, H5 sketch-split, diff harnesses (G-code/netlist/DXF/study), G4 gate. Orchestration plan + per-node handoff in the task file. Flagged a probable engine doc-id-resolution bug (scenario doc2 stays empty) to Fable.
- 2026-07-03 01:00 UTC [opus-sky] CHECKPOINT (branch pushed @9940535). PORTS DONE: H1 CAM (e158e31), H2 ECAD (565eb96), H3 drawing (17a7844), H4 sim (dbd79fa) — all build green + shot-verified offscreen via the merged T-G13 fix. H5 sketch was ALREADY ported in earlier waves (8.6k LOC modular split present; sketch+asm shots render). So all 5 subsystems' panels exist. Drawing + sim surface perfectly (workspace content-swap); CAM + ECAD render+execute but aren't forward in headless shots — tab-bar-hidden central dock node; I tried SetNextWindowFocus AND SelectedTabId, both fail (documented in task file), fix = restructure CAM/ECAD to content-swap inside Viewport like drawing/sim. REMAINING (fresh-session scope): (1) CAM/ECAD content-swap surfacing, (2) the DoD diff harnesses (G-code 8 posts / netlist+ERC / DXF+PDF / study JSON / sketch solver op-batch — engine RPCs kept byte-identical so should diff clean), (3) G4 gate. Full plan + per-node handoff in work/tasks/T-G8.md. NOT review-ready yet (DoD diffs+gate outstanding); keeping owned. Also flagged a probable engine doc-id-resolution bug (scenario doc2 stays empty) to Fable earlier.
