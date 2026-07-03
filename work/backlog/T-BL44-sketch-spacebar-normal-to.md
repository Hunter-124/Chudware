---
id: T-BL44
title: Sketch — spacebar orients the view normal to the active sketch plane
workstream: backlog
owner: unassigned
status: todo
priority: 2
lane: app-gui
kind: fix
depends_on: []
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
