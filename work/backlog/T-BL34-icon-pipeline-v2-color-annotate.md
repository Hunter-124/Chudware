---
id: T-BL34
title: Icon pipeline v2 — color the preview-render models + annotate operation intent over the tiles
workstream: backlog
owner: unassigned
status: todo
priority: 3
lane: app-gui
kind: overhaul
depends_on: [T-BL29]
branch: task/T-BL34-icon-pipeline-v2-color-annotate
---

## Context

Direction from Hunter (2026-07-03): evolve the screenshot-icon pipeline beyond plain
photographic renders. Two enhancements to how the preview tiles communicate the
operation:

1. **Color the models** in the preview screenshots — the demo part rendered for each
   icon should be colored to highlight the operation (e.g. the modified/relevant faces
   or the added/removed body in a distinct color), so the tile reads as "this is what
   the op does" at a glance, not just a grey part.
2. **Annotate / mark over the tiles** to point out the purpose/intention of the
   operation — a lightweight overlay (arrow, highlight, direction glyph, before/after
   cue) marked onto the icon that signals intent (e.g. draft direction, sweep path,
   fillet edge).

This is the design evolution of the "per-face emphasis overlay" open question already
flagged in T-BL29 and the T-G15 handoff (renderThumbnail colors per-body but has no
per-face overlay). T-BL34 turns that from an open question into scoped work.

## Sequencing

- **depends_on T-BL29** — T-BL29 (sonnet-lumen, in flight) writes the remaining recipes
  against the CURRENT pipeline. Let it land first so v2 builds on a complete recipe set
  rather than racing it. T-BL34 then re-generates the tiles with coloring + annotation.
- Do NOT start T-BL34 until Hunter greenlights (he wants to steer the ambitious GUI
  direction himself for this stretch — this task is parked visibly, not auto-dispatched).

## Definition of done (to be refined with Hunter before start)

- [ ] Preview-render models colored to emphasize the operation's affected geometry
      (per-face or per-body color override in the thumbnail render pass — the T-G15
      renderThumbnail path is where this hooks; design the per-face overlay first).
- [ ] Operation-intent annotation overlay composited onto each tile (arrow/highlight/
      direction/before-after cue appropriate to the op family).
- [ ] Applied across the icon groups (coordinate with T-BL29's group list so nothing is
      missed — see "continuation" note below).
- [ ] Atlases regenerated via the pipeline (DERIVED — never hand-edit PNGs). Contact
      sheet for Hunter's review, light+dark, every tile eyeballed.
- [ ] scripts/gate.sh + `graphify update .` before review.

## Continuation guarantee (Hunter directive)

Whatever icon work has been done, the overlooked/missed tiles MUST remain trackable —
T-BL29 owns the remaining-group recipe coverage and MUST NOT be closed with silent gaps
(any group it can't complete gets documented as a checkbox left open + reason, not
dropped). T-BL34 (this task) owns the coloring+annotation layer on top. Between the two,
every ribbon icon group has a home. If a NEW gap surfaces, add it here or to T-BL29 — do
not let it fall off the board.

## Driver notes

- Pipeline: `scripts/icon_pipeline.py` RECIPES + `AI_CAD_SHOT=iconpipe`;
  `host/src/ui/renderer.{h,cpp}` renderThumbnail; `host/src/ui/scenarios.cpp` runIconPipe
  (face ref-selectors live here). See work/tasks/T-G15.md HANDOFF 2 for the per-face
  overlay design constraints already mapped.
- Annotation compositing is a post-render step (overlay onto the RGBA tile before atlas
  injection) — keep it in the Python pipeline, not the C++ renderer, where practical.

## Progress log
