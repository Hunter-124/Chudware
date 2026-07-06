---
id: T-SIM2
title: Simulation results field visualization - mesh/nodal-field export + color-mapped contour overlay in the viewport
workstream: SIM
owner: opus-vosk
status: done
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
- 2026-07-05 [opus-sweep] FINDING (flagged by Hunter): work/questions/T-SIM2-shots/contour_light.png
  and contour_dark.png are stale — captured before the day-10..12 GUI polish wave (T-G14/16/21/22/
  23/24/25) and show the old chrome. Rebuilt AI-CAD2 on Windows (T-G25's doc_tabs.h fix was the
  build blocker, now on master) and re-ran `AI_CAD_SHOT=fea_overlay` against the committed
  contracts/fixtures/sim2_cantilever_result.aicm fixture to recapture — but found the FEA legend
  (and separately, the orientation-gizmo cube) currently render HIDDEN behind the ShopCraft Copilot
  panel: both are positioned off the viewport rect's nominal right edge (viewport.cpp's `b.x`),
  which no longer accounts for the Copilot panel's width now that it renders on top. Confirmed via
  a temporary debug probe that legendOn is true every frame — this is a draw-position bug, not a
  state bug. Did NOT commit new screenshots (they'd be missing the legend, a regression vs. the
  current committed ones) and did NOT attempt a fix (viewport-rect/layout code affects several
  other elements — gestures, view-selector, heads-up toolbar — and deserves its own scoped look).
  Repro: build ui/imgui (see docs/BUILD_WINDOWS.md), stage build/windows-occt DLLs next to
  AI-CAD2.exe, run `AI_CAD_SHOT=fea_overlay AI_CAD_REPO=<repo root> AI-CAD2.exe --shot out.png`
  with the Copilot panel open (default state) — legend/gizmo absent; likely present if the panel
  is collapsed. Next step: fix the viewport-rect calc (or give legend/gizmo their own Copilot-aware
  bound) in ui/imgui/src/ui/viewport.cpp, then recapture both this task's contact shots and T-G25's.
- 2026-07-05 [opus-sweep] Per Hunter: removed the stale pair outright (`git rm`) rather than
  leave incorrect-GUI screenshots sitting in the repo — contour_light.png/contour_dark.png no
  longer exist under work/questions/T-SIM2-shots/. This is a narrower, scoped removal (just these
  two files); other tasks' historical per-fix screenshots (T-G19, T-G12, T-BL37, T-BL41, etc.) are
  untouched — they're legitimate point-in-time DoD evidence for already-closed tasks, not ongoing
  documentation, so predating the polish wave doesn't make them wrong. Recapture is still blocked
  on the legend/gizmo-behind-Copilot-panel bug above; do that fix first, then regenerate this
  pair (and re-verify T-G25's own contact shots still look right once legend/gizmo positioning
  changes, since drawOrientationGizmo shares the same viewport-rect calc).
