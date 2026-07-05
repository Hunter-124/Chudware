---
id: T-SIM2
title: Simulation results field visualization - mesh/nodal-field export + color-mapped contour overlay in the viewport
workstream: SIM
owner: opus-vosk
status: doing
branch: task/T-SIM2-contour-results
depends_on: []
lane: simulation
kind: overhaul
priority: 1
---

## Context

Hunter directive (overnight 2026-07-05): simulation UI overhaul. The T-V1 audit + scope
pass found the #2 gap: study results are shown ONLY as scalar rows + narration text in the
T-U6 studies panel (ui/imgui/src/ui/sim_panel.cpp, 601 LOC). There is no spatial
visualization — users cannot see WHERE stress concentrates or displacement peaks. Every
real CAD package renders a color-mapped contour over the deformed/undeformed mesh.

Current plumbing:
- FEA solvers (core/sim/fea_static/modal/fatigue) compute nodal fields internally but the
  RPC results only surface scalars (max_stress_mpa, max_disp_mm, factor_of_safety).
- ui/abstract/sim_view.h (294 LOC) is the view model — no mesh/field type exists yet.
- sim_manager.cpp handles run dispatch via AsyncRpc.

## Definition of done

- [ ] Backend: result mesh export — surface triangles + nodal scalar field(s)
      (von Mises stress, displacement magnitude, temperature when T-SIM1 lands — design
      the field slot generically, keyed by field name) attached to study results, behind
      a size-bounded RPC (decimate/skin the tet10 volume to surface tris; document the
      size cap). Do NOT bloat describe_results narration — separate structured payload.
- [ ] ui/abstract/sim_view.h: SimResultsMesh model (vertices, tris, per-node scalars,
      field name, min/max range).
- [ ] Viewport overlay in ui/imgui: color-mapped rendering of the result mesh over the
      part (jet/viridis-style ramp — use theme-appropriate ramp, dynamic color respected),
      with a legend bar (min/max + units) in the sim panel. Toggle per study row
      ("Show contour"). Deformed-shape scale factor optional but nice.
- [ ] Works for static (stress + displacement) and modal (mode shape displacement) today;
      field-generic so thermal drops in later.
- [ ] Headless verify: AI_CAD_SHOT scenario capturing a contour-rendered study on a demo
      part (bracket under load), light + dark, eyeballed and described in the work note.
- [ ] scripts/gate.sh green (issue #16 known failures excepted); `graphify update .` run.

## Driver notes

- Python test venv: `~/.local/share/AI-CAD/mcp-venv/bin/python`.
- graphify rule: `graphify query "<question>"` before raw-file reading.
- Branch task/T-SIM2-contour-results, worktree ../CAD-T-SIM2. Stop at status: review.
- ADDITIVE SEAMS ONLY vs T-SIM1 (runs in parallel): do not modify fea_thermal files;
  keep the field-slot design generic so thermal plugs in without rework.
- Design language: pinned glass columns over full-width viewport (locked intent) — the
  legend/toggles live in the sim panel column, the contour lives in the viewport.

## Progress log
- 2026-07-05 04:28 UTC [opus-vosk] INVESTIGATION (opus-vosk): T-SIM2's premise (no spatial FEA visualization) points at the wrong panel. The full color-mapped contour pipeline ALREADY EXISTS and is live in the Simulate workspace: backend core/sim/overlay.cpp static_overlay/modal_overlay/fatigue_overlay skin the tet-volume boundary to surface tris + map nodal scalars (von_mises/disp/fos/principals) + displacement into an AICM (SCLR/DISP chunks); ui/imgui viewport.cpp viewportLoadResultAicm -> C19 colormap LUT + deformed shape; legend.cpp draws the jet min/max/units bar; sim_manager.cpp drawResultControls has a 'Show colormap' toggle + field combo (thermal-ready kFlowFields incl temperature) + deform slider. Built in commits 845b9217 (P55b FEA overlay) + 7f3516bd/7aee4805 (T-G8/G9 SimManager port). Genuine remaining gaps I will close: (1) no documented size cap/decimation in overlay.cpp [DoD-explicit]; (2) AI_CAD_SHOT=fea uses a box not a bracket + no light/dark pair; (3) 'Show colormap' checkbox uses a per-frame local bool (state not reflected). NOTE: T-SIM3 concurrently edits sim_manager.cpp -> my only edits there are the contour toggle fix + legend, additive seams. Question for lead surfaced separately: is the floating sim_panel.cpp studies list intended to also carry contour, or is the Simulate workspace the primary contour UI?
