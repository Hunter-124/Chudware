---
id: T-BL44
title: Sketch — spacebar orients the view normal to the active sketch plane
workstream: backlog
owner: sonnet-harle
status: doing
priority: 2
lane: app-gui
kind: fix
depends_on: [T-BL48]
branch: task/T-BL44-sketch-spacebar-normal-to
---

## Context

Direction from Hunter (2026-07-03). While editing a sketch, pressing the **space bar** should
automatically orient the camera **normal to the sketch plane** ("normal to" view). Small
quick-win, but Hunter wants it in with the sketch-workflow batch.

## Definition of done

- [ ] In sketch edit mode, pressing space orients the view normal to the ACTIVE sketch plane.
- [ ] Smooth camera transition consistent with existing view-orientation animation (not an
      instant jarring snap if the app animates view changes elsewhere).
- [ ] **Resolve any existing spacebar binding:** check what space currently does (globally
      and in sketch mode) — if it's bound elsewhere, scope this to sketch mode so nothing
      else breaks, or reconcile the binding explicitly and note the decision.
- [ ] Verify: enter a sketch on a non-default plane / an angled face, rotate the view, press
      space → view snaps normal-to the sketch plane. Screenshot before/after, light+dark.
- [ ] flock GUI build; scripts/gate.sh + `graphify update .` before review.

## Driver notes

- graphify-first: `graphify query "viewport camera view orientation normal to"` and
  `graphify query "keyboard input handling viewport"`. host/src NOT indexed — targeted reads.
- Two pieces: (1) keyboard handler for space in sketch mode, (2) compute the sketch plane
  normal + call the existing "orient view to a normal/direction" camera routine (the
  view-cube / standard-views code already does normal-to-plane orientation — reuse it).
- Tier: Sonnet, small.
- Related: T-BL41 (sketch dim hide), T-BL42/43 (sketch workflow).

## Progress log
- 2026-07-04 00:24 UTC [opus-hunter] BLOCKED by opus-hunter. Root cause: ui/imgui interactive sketcher entry is not wired. The only active_=true setter (sketch_mode.cpp:351 enterForFeature) is never dispatched — onRibbonCommand/enterForFeature/enterNewOnSelection have ZERO callers in ui/imgui|app (only in deprecated host/, ref wiring host/src/app.cpp:994). s_sketch is a viewport function-local static with no command sink; feature_tree.cpp:315 = void* sketchMode=nullptr. So there is no active-sketch state for Space to orient to; a correct handler is committed on task/T-BL44-sketch-spacebar-normal-to but necessarily no-ops (NOT merged). Full finding + recommendation in work/questions/T-BL44.md. Scoped block: T-BL44/T-BL41/T-BL46 need the active_ overlay → blocked on a "wire ui/imgui sketch entry" prerequisite (untracked; recommend cutting it, Opus-tier). T-BL42 is NOT blocked (feature-tree/doc-state). Handler collapses to trivial once wiring lands.
- 2026-07-04 [fable-helm] UNBLOCKED: T-BL48 wiring at review (task/T-BL48-wire-imgui-sketch-entry). opus-hunter session ended; released to unassigned. Handler already committed on this task's branch — remaining work is rebase onto the T-BL48 branch + behavioral re-verify (s_sketch.cameraLock path) + screenshots.
