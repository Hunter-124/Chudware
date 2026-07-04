---
id: T-BL48
title: Wire ui/imgui interactive sketch entry — dispatch ribbon/sketch commands to s_sketch so enterForFeature runs (unblocks T-BL44/41/46)
workstream: backlog
owner: opus-hunter
status: doing
priority: 2
lane: app-gui
kind: fix
depends_on: []
branch: task/T-BL48-sketch-entry-wiring
---

## Context

Found by opus-hunter while doing T-BL44 (2026-07-03). The ui/imgui interactive sketcher's
**editing entry is not wired**: `SketchMode::onRibbonCommand` / `enterForFeature` /
`enterNewOnSelection` have ZERO callers in ui/imgui|app (only in deprecated `host/`; ref
wiring `host/src/app.cpp:994` `sketchMode.onRibbonCommand(st, cmd_id)`). The single
`SketchMode` instance is a viewport function-local static (`ui/imgui/src/ui/viewport.cpp:729`)
with no command sink; `feature_tree.cpp:315` = `void* sketchMode = nullptr`. So `sketch_new`
(app_state.cpp:164) only flips `s.inSketch=true` and `drawOverlay` returns at
`sketch_overlay.cpp:1332` (`if(!active_) return`) — the whole interactive editing overlay +
camera state is inert. See work/questions/T-BL44.md.

This blocks T-BL44 (spacebar normal-to), T-BL41 (dim/relation glyphs), T-BL46 (drag
dead-zone). Cut as their prerequisite (Hunter greenlit 2026-07-03).

## Definition of done

- [ ] Ribbon/command dispatch routes sketch command ids (sketch_new, sk_line, sk_rect_corner,
      sk_circle, sketch_exit, sk_* tools, dims, constraints) to the single `s_sketch` instance
      via `onRibbonCommand(host, cmdId)` — mirror the host/ reference (`host/src/app.cpp:994`).
- [ ] `s_sketch` is reachable from the command path (lift it out of the viewport-frame
      function-local static into an accessor/singleton — e.g. `SketchMode& sketchMode()` like
      `s_viewSelector` — or a pending-command queue drained in the viewport frame where the
      SketchHost is built). Choose the minimal, clean option; document it.
- [ ] Entering a sketch actually sets `active_=true` and computes `camYaw_/camPitch_` +
      `frame_` from the plane (via enterForFeature). PROVE it: enter a sketch on a plane, then
      confirm `active_` is true and the editing overlay renders (not the `!active_` early-return).
- [ ] The plane-pick / selection path works: `sketch_new` with no selection arms plane-pick
      (`planePicking_`), and picking a plane/planar face enters the sketch (createSketchOnPlane
      → enterForFeature). With a pre-selected plane, enters directly.
- [ ] Sketch exit (`sketch_exit`) restores the pre-sketch camera and clears `active_`.
- [ ] Do NOT weaken tests. flock GUI build (linux-app preset). Light+dark screenshots of an
      entered sketch (overlay visible). scripts/gate.sh + `graphify update .` before review.

## Driver notes

- Reference wiring (deprecated but correct): `host/src/app.cpp:994-1027` shows how host routed
  ribbon commands + edit ops (copy/cut/paste/delete/select-all) around `sketchMode`.
- The ui/imgui SketchMode API is complete (`ui/imgui/src/ui/sketch/sketch_mode.{h,cpp}`,
  sketch_overlay/_edit/_tools/_constraints/_seam) — only the DISPATCH is missing.
- Camera: `g_vpState.yaw/pitch` is the live camera (syncCameraToGl each frame). Entry sets
  `st.yaw=camYaw_` on the SketchHost copy — verify that reaches g_vpState (the host is built
  from g_vpState each frame in viewport.cpp; entry may need a write-back so the orientation
  actually takes). This is the same seam T-BL44's handler depends on.
- Once this lands, T-BL44's committed handler (task/T-BL44-...) should just work — re-verify.

## Progress log
