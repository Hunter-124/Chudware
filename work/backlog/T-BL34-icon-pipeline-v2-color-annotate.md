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
- [ ] **Author the Manufacture toolpath icons from scratch** (Hunter directive) — T-BL29
      DEFERRED the entire Manufacture group (no `cam_simulate` capture path existed in the
      pipeline), so this task OWNS it, not just annotation. Build the capture path:
      `cam_new_job → cam_add_tool → cam_add_operation → cam_simulate` → render the real
      toolpath (moves/cutter path over the stock) → composite per the visual contract, for
      all five sub-groups (2.5D, Holes, 3D, Turning, Cutting). The rendered toolpath IS a
      smoke check the CAM op chain works — if an op won't produce a renderable toolpath,
      FILE it as a CAM bug (issue + task), never fake the tile (WORKFLOW.md §6b). Then
      annotate (cut direction, entry, stock-vs-cut). Never a stylized glyph — genuine
      engine output only.
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

## HANDOFF — reserved for mooseinatree (Hunter directive 2026-07-05)

Hunter: "the rest of the icon generation should be delegated to mooseinatree since
that's pretty straightforward." This task is RESERVED for mooseinatree — other agents,
do not claim it.

State you inherit (branch `task/T-BL34-icon-pipeline-v2-color-annotate`):
- sonnet-ivo committed substantial work then went silent 2026-07-04 13:22:
  - 6fc57ace — icon pipeline v2: real CAM toolpath tiles, accent emphasis, intent annotations
  - 09fe65d8 — light+dark contact sheets + in-context ribbon captures "for review"
- Status was never flipped; unknown how much DoD verification actually happened.

Your job (finisher pattern):
1. Check out the branch, merge master in (T-W17 GUI panels landed since the cut).
2. Audit the committed work against the DoD checkboxes above — especially: EVERY tile
   eyeballed (light + dark), Manufacture toolpath tiles are genuine engine output (never
   stylized glyphs — WORKFLOW.md §6b), atlases regenerated via the pipeline (never
   hand-edited PNGs), contact sheet ready for Hunter's review.
3. Fill any gaps (missed groups per the continuation guarantee above), run
   scripts/gate.sh + `graphify update .`, flip to status: review with a work note
   summarizing what you verified vs redid. Never merge to master, never set done.

No rush — proceed when you're online; nothing blocks on this.

## Progress log

- 2026-07-04 [fable-helm] GREENLIT by Hunter (live session): "make sure that eventually we
  get around to generating the rest of the gui icons too, and make the manufacturing ones
  showcase the generated tool-paths that the tools are used for instead of just screenshots
  of the types of shapes they're supposed to be used on." The "do NOT start until Hunter
  greenlights" park is lifted; the Manufacture-toolpath DoD item is the binding
  interpretation of his directive. Dependency T-BL29 is done. NOTE: driver-notes paths
  reference deprecated host/src — the pipeline hooks may have moved to ui/imgui during the
  migration waves; verify where AI_CAD_SHOT=iconpipe lives NOW before editing.
- 2026-07-05 04:12 UTC [sonnet-ivo] Driver sonnet-ivo stalled 2026-07-04 13:22 (contact sheets committed for review on task branch, then silence; status never flipped). Hunter directive (overnight 2026-07-05): remaining icon generation is DELEGATED TO MOOSEINATREE. Claim released; task reserved for mooseinatree — see HANDOFF section in task file.
