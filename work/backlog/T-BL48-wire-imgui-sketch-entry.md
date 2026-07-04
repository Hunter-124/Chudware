---
id: T-BL48
title: Wire ui/imgui interactive sketcher entry — dispatch sketch enter/edit/exit so active sketch editing works (unblocks T-BL41/44/46)
workstream: backlog
owner: opus-tarn
status: review
priority: 2
lane: app-gui
kind: fix
depends_on: []
branch: task/T-BL48-wire-imgui-sketch-entry
---

## Context

Cut 2026-07-04 by fable-helm from opus-hunter's T-BL44 blocking finding (see T-BL44
progress log; full writeup in work/questions/T-BL44.md on the T-BL44 branch):

> ui/imgui interactive sketcher entry is not wired. The only `active_=true` setter
> (`sketch_mode.cpp:351 enterForFeature`) is never dispatched — `onRibbonCommand` /
> `enterForFeature` / `enterNewOnSelection` have ZERO callers in ui/imgui|app (only in
> deprecated host/, ref wiring `host/src/app.cpp:994`). `s_sketch` is a viewport
> function-local static with no command sink; `feature_tree.cpp:315` = `void*
> sketchMode=nullptr`. So there is no active-sketch state.

The whole interactive sketch-workflow batch Hunter prioritized (T-BL41 dimension
auto-hide, T-BL44 spacebar normal-to, T-BL46 drag dead-zone) is blocked on this.
Hunter re-confirmed the sketcher batch as a top priority 2026-07-04.

## Definition of done

- [x] **Entry is dispatched:** the ribbon Sketch command (and the new-sketch-on-selected-
      plane/face flow) actually enters interactive sketch mode in the ui/imgui app —
      `enterForFeature` / `enterNewOnSelection` get real callers, `active_` becomes true.
      Mirror the deprecated reference wiring at `host/src/app.cpp:994` (host/ is
      reference-only — do not resurrect host code paths).
- [x] **Command sink:** `s_sketch` (viewport function-local static) is replaced/exposed as
      a reachable sketch-mode sink so ribbon + feature tree + viewport share one state;
      `feature_tree.cpp:315`'s `void* sketchMode=nullptr` placeholder is wired to it.
- [x] **Edit an existing sketch:** double-click (or context menu) on a sketch row in the
      Feature Manager enters edit mode on that sketch.
- [x] **Exit works:** confirm/cancel leaves sketch mode cleanly, sketch data lands in the
      doc, downstream feature (e.g. extrude) can consume it.
- [x] Real-flow verify in the GUI: new sketch on a datum plane → draw an element → exit →
      extrude it. Screenshots light+dark; headless `AI_CAD_SHOT` scenario covering sketch
      entry if one exists or is cheap to add.
- [x] After landing: post a FOREMAN.md note that T-BL44/T-BL41/T-BL46 are unblocked
      (T-BL44 depends_on is already relinked to this task).
- [x] flock-serialized GUI build; scripts/gate.sh + `graphify update .` before review.

## Driver notes

- **Tier: OPUS** (per the T-BL44 finding's recommendation — this is state-machine/
  dispatch architecture, not a cosmetic fix).
- graphify-first: `graphify query "sketch mode enter active viewport"` — but note
  graphify indexes ui/abstract + core, NOT host/src; targeted reads in host/src (the
  reference wiring) and ui/imgui are expected.
- Known pointers: `ui/imgui/.../sketch_mode.cpp:351` (enterForFeature, sole active_
  setter), `ui/imgui/.../feature_tree.cpp:315` (sketchMode nullptr), viewport `s_sketch`
  static, `host/src/app.cpp:994` (reference dispatch).
- **Coordination:** T-BL42 (feature-sketch absorption) also edits feature_tree.cpp and
  T-BL43 (external refs) touches in-sketch selection — both in flight this session. Keep
  your diff scoped to entry/exit dispatch; post a FOREMAN.md line if you collide.
- A correct spacebar handler for T-BL44 is already committed on
  `task/T-BL44-sketch-spacebar-normal-to` (unmerged) — do NOT duplicate it; just make
  the wiring it needs exist.

## Progress log

- 2026-07-04 opus-tarn: COLLISION found+resolved — duplicate task-id: opus-hunter's
  task/T-BL48-sketch-entry-wiring (renamed from T-BL47, at review) already delivered the
  ribbon-entry half. Per advisor: based THIS branch on hunter's tip (his commits credited
  in-history), added only the superset delta. Merge fate question: work/questions/T-BL48.md
  (recommend: merge this branch, close hunter's as folded-in). FOREMAN coord note posted;
  hunter + helm notified via team_send.
- 2026-07-04 opus-tarn: delta shipped (e584c74e): AppState::pendingSketchEditFid/
  pendingSketchReparentFid; feature_tree.cpp 3 deferred stubs filled (Edit Sketch +
  Edit Sketch Plane/Face ctx-menu, double-click on sketch row) via st.app-> (FmState mirror);
  viewport drain calls enterForFeature/beginPlaneEdit; dead void* sketchMode removed.
- 2026-07-04 opus-tarn: proof scenario (39ed76f9): AI_CAD_SHOT=sketch_dispatch drives the
  REAL path only (never force-sets inSketch). Both themes, xvfb, engine-attached
  (AI_CAD_LIBAICAD -> built libaicad.so; demo-mode runs discarded per helm's ruling):
  entry drain inSketch=1 + overlay PNG (double-gated on inSketch AND active_), exit drain
  inSketch=0, reedit via pendingSketchEditFid inSketch=1 + f1's rect entities visible in
  editor (sketch_dispatch_04_reedit.png), extrude ok=1 (engine feature_status). PNGs
  eyeballed: scratch/sketch_dispatch_shots{,_dark}/.
- 2026-07-04 opus-tarn: PRE-EXISTING quirk noted (NOT this branch): the viewport GL mesh
  shows the startup demo body instead of the freshly-extruded slab in the post-extrude shot
  — reproduced byte-identically with MASTER's binary on the merged `sketch` scenario
  (sketch_03_solid.png). Extrude consumption itself is engine-verified ok=1. Follow-up chip
  suggested separately.
- 2026-07-04 opus-tarn: unblocks T-BL44/T-BL41/T-BL46. API for follow-ups: inside
  viewport.cpp use s_sketch.active()/cameraLock(yaw,pitch) (T-BL44 handler already does);
  outside the viewport read AppState::inSketch (drain keeps it = active_ || planePicking_);
  to ENTER programmatically: dispatch a sketch cmd id or set pendingSketchEditFid.
