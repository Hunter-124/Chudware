---
id: T-BL46
title: Sketch — larger drag dead zone / element-drag priority so repositioning/resizing elements doesn't instantly pan the view
workstream: backlog
owner: unassigned
status: todo
priority: 2
lane: app-gui
kind: fix
depends_on: []
branch: task/T-BL46-sketch-drag-deadzone
---

## Context

Direction from Hunter (2026-07-03). When dragging sketch elements to reposition or resize
them, the **view instantly starts panning/rotating away** instead of moving the element.
Hunter's suggested fix: a **larger dead zone** before view-drag engages, so small drags on
an element manipulate the element rather than the camera.

## Root-cause note (diagnose before fixing)

Hunter's proposed fix is a bigger dead zone, and that may be exactly right. BUT the deeper
cause may be **hit-test priority**: if a draggable sketch element is under the cursor at
mouse-down, the drag should be captured for the ELEMENT, and view-navigation shouldn't
compete at all. A dead zone alone leaves the bug latent (a fast/large drag still steals to
the view). Diagnose which it is and fix the real cause — the outcome below is the bar, not a
specific mechanism.

## Definition of done

- [ ] **Outcome:** dragging a sketch element (reposition OR resize via a handle/corner) moves
      the element and does NOT pan/rotate the view — for slow AND fast drags.
- [ ] Implement the correct fix: (a) a larger drag dead zone before view-navigation engages,
      and/or (b) element-under-cursor at mouse-down captures the drag so view-nav doesn't
      compete. Prefer (b) if the root cause is hit-test priority; (a) is a reasonable
      complement for near-misses.
- [ ] **Don't break view navigation:** dragging on EMPTY sketch space (no element under
      cursor) still pans/rotates normally, and normal view nav must not feel sluggish or
      dead-zoned into unresponsiveness.
- [ ] Applies in sketch edit mode. Confirm it doesn't regress element selection (a click
      without drag still selects).
- [ ] Verify via real interaction (this is behavioral — a still screenshot won't prove it;
      use xdotool drag simulation under xvfb or document a manual test): grab an element and
      drag slow + fast → element moves, view still; drag empty space → view navigates; drag a
      rectangle corner → resizes without view jump.
- [ ] flock GUI build; scripts/gate.sh + `graphify update .` before review.

## Driver notes

- graphify-first: `graphify query "viewport drag camera pan sketch element"` and
  `graphify query "mouse drag threshold sketch"`. host/src NOT indexed — targeted reads.
- The arbitration between element-drag and camera-drag at mouse-down is the crux. Find where
  a drag is routed to the camera vs an element; check if element hit-testing runs first and
  whether it captures the drag. A drag-threshold (dead zone) constant likely exists — but
  verify a threshold bump actually fixes it vs. a capture-priority change.
- Tier: Sonnet (input/interaction handling).
- Related: sketch cluster T-BL41/42/43/44/46.

## Progress log
