---
id: T-BL34
title: Icon pipeline v2 — color the preview-render models + annotate operation intent over the tiles
workstream: backlog
owner: sonnet-ivo
status: review
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

- [x] Preview-render models colored to emphasize the operation's affected geometry —
      PER-BODY v1 (foreman-approved): recipes color the op-created body with the app
      accent teal (#2DD4BF) via set_appearance (extrude/revolve/sweep/loft/helix whole
      body; linear/circular pattern seed+copies via merge:False second body). No C++
      renderer change needed for v1.
- [ ] TRUE per-face emphasis (fillet/chamfer/shell edges+faces, boolean result faces)
      — needs renderer work: the cheapest identified path is the per-vertex scalar +
      colormap LUT route in renderThumbnail (host/src/renderer.h L73/L221-230) or an
      Overlays::drawFaceWash pass inside the thumbnail FBO. Follow-up task; dress-up
      tiles ship with ring/arrow annotation instead for now. Mirror also reverted to
      annotation-only (merge:False broke the mirrored copy — see progress log).
- [x] Operation-intent annotation overlay composited onto each tile in the PYTHON
      pipeline post-render (ANNOTATIONS table in scripts/icon_pipeline.py): direction
      arrows (extrude/sweep/loft/shell/pattern), rotation arcs (revolve/circular/helix),
      mirror-plane dash, boolean +/-/n badges, dress-up spotlight rings, flat-pattern
      double arrow. appr_* tiles deliberately unannotated (they ARE the preset).
- [x] **Author the Manufacture toolpath icons from scratch** (Hunter directive) — DONE
      for ALL 19 op buttons across all five sub-groups. `scripts/cam_toolpath_icons.py`
      drives the REAL chain per tile (`cam_new → cam_apply_ops(add_tool/add_setup/
      add_operation) → cam_generate → cam_simulate`) via the libchudware core_bridge (no
      GUI, no demo mode) and renders the returned Move list over the stock silhouette
      using the in-app backplot palette (rapid/lead/cut/plunge/arc from ui_theme.cpp).
      2.5D: face/contour/pocket/adaptive/slot/chamfer/engrave; Holes: drill(peck)/
      thread_mill(helix); 3D: parallel/waterline/scallop/3D-pocket over real meshes;
      Turning: rough(staircase)/finish(profile)/face/part-off; Cutting: laser cut2d/
      probe. Every chain generated real cutting moves and simulated ok=True — 19/19,
      nothing faked, no stylized glyphs. Stock-vs-cut + entry are annotated by the
      backplot colors themselves (rapid red / lead green / plunge cyan / cut blue).
      CAM bug found by the smoke check and FILED: issue #13 (lathe stock sim
      under-reports removed volume for turning ops; does not affect toolpath output).
      New atlas cells `cam_*` (19) appended byte-preservingly (ensure_cells) +
      registered in make_icons.py CONTRACT_NAMES with line-art fallback drawers +
      contracts/icons.md (append-only); host/src/ui/ribbon_def.cpp Manufacture op
      buttons repointed from borrowed Features glyphs to the new cells.
- [x] Applied across the icon groups: 3D Body (6), Dress-up (3), Pattern (3),
      Multi-body (3), Curves (1), Sheet Metal (2), Appearance (3, color untouched by
      design), Manufacture (19). 41 tiles injected total; T-BL29's group list fully
      covered. appr_glass/appr_paint remain unwired-to-ribbon (T-BL29 Q4) — unchanged.
- [x] Atlases regenerated via the pipeline only (`xvfb-run -a python3
      scripts/icon_pipeline.py --render`, idempotent, exit 0, zero failed steps).
      Contact sheets scratch/icon_contact_sheet_{dark,light}.png (each tile at review
      size + true 32px) for Hunter's review; every tile eyeballed by the driver,
      including in-context CHUDWARE_SHOT=ribbon manufacture-tab captures dark+light
      (scratch/ribbon_manufacture_{dark,light}.png) proving the C++ loader reads the
      extended atlas.
- [x] scripts/gate.sh + `graphify update .` before review (see progress log).

## Continuation guarantee (Hunter directive)

Whatever icon work has been done, the overlooked/missed tiles MUST remain trackable —
T-BL29 owns the remaining-group recipe coverage and MUST NOT be closed with silent gaps
(any group it can't complete gets documented as a checkbox left open + reason, not
dropped). T-BL34 (this task) owns the coloring+annotation layer on top. Between the two,
every ribbon icon group has a home. If a NEW gap surfaces, add it here or to T-BL29 — do
not let it fall off the board.

## Driver notes

- Pipeline: `scripts/icon_pipeline.py` RECIPES + `CHUDWARE_SHOT=iconpipe`;
  `host/src/renderer.{h,cpp}` renderThumbnail; `host/src/ui/scenarios.cpp` runIconPipe
  (face ref-selectors live here). See work/tasks/done/T-G15.md HANDOFF 2 for the per-face
  overlay design constraints already mapped.
- Annotation compositing is a post-render step (overlay onto the RGBA tile before atlas
  injection) — keep it in the Python pipeline, not the C++ renderer, where practical.

## Open follow-ups (tracked, not silently dropped)

- [ ] TRUE per-face emphasis in renderThumbnail (see DoD item above) — renderer work.
- [ ] Issue #13: lathe stock-sim removed-volume under-report (CAM core bug, filed).
- [ ] Turning tiles are legible but sparse at 20px (few moves by nature); revisit if
      Hunter wants denser demo profiles.

## Progress log

- 2026-07-04 [fable-helm] GREENLIT by Hunter (live session): "make sure that eventually we
  get around to generating the rest of the gui icons too, and make the manufacturing ones
  showcase the generated tool-paths that the tools are used for instead of just screenshots
  of the types of shapes they're supposed to be used on." The "do NOT start until Hunter
  greenlights" park is lifted; the Manufacture-toolpath DoD item is the binding
  interpretation of his directive. Dependency T-BL29 is done. The driver-notes paths
  remain current because `host/` is the sole GUI tree.
### sonnet-ivo — 2026-07-04 (v2 delivered, status: review)

**What shipped (branch task/T-BL34-icon-pipeline-v2-color-annotate):**
- `scripts/cam_toolpath_icons.py` (NEW): real-CAM-chain tile renderer, 19/19 Manufacture
  op tiles from genuine cam_generate output, backplot-palette classified moves over stock,
  doubles as CAM smoke test (found + filed issue #13, lathe sim volume under-report).
- `scripts/icon_pipeline.py`: accent-teal per-body emphasis in recipes; ANNOTATIONS
  overlay table + Python compositor (arrows/arcs/dashes/badges/rings); ensure_cells()
  byte-preserving atlas extension; dual light/dark contact sheets with true-32px preview;
  CAM tiles integrated into --render.
- `assets/make_icons.py`: 19 cam_* CONTRACT_NAMES + line-art fallback drawers
  (coverage check green) so full atlas regen keeps the cells.
- `contracts/icons.md`: Manufacture toolpath tile section appended (append-only).
- `host/src/ui/ribbon_def.cpp`: 19 Manufacture op buttons repointed to dedicated cam_*
  cells (they previously borrowed Features glyphs — a toolpath render in those cells
  would have clobbered fillet/shell/etc.).

**Verification:** pipeline exit 0 / 159 ok / 0 failed steps; contact sheets dark+light
eyeballed tile-by-tile; CHUDWARE_SHOT=ribbon manufacture tab captured dark+light headless
(xvfb, real engine libchudware staged — no demo mode) and eyeballed: new 20px toolpath
icons render in-context from the extended atlas. gate.sh + graphify update run before
review. Known cosmetic: the first-run Chudware info toast overlays the Job group in
headless ribbon shots (pre-existing, affects captures of every tab).

**Design decisions:** per-body v1 emphasis (foreman-approved) with per-face left as
tracked follow-up; mirror kept annotation-only after merge:False broke the mirrored
copy AND the seed straddled the y=0 mirror plane (geometry shifted so seed+copy both
show; dash follows the actual plane trace); annotation kept to one cue per tile,
teal + dark keyline so one asset reads on both themes.
- 2026-08-27 [cleanup audit] Remaining DoD item: true per-face emphasis for
  fillet/chamfer/shell and boolean-result faces in `renderThumbnail`; the shipped
  per-body coloring and annotation layer do not satisfy that deferred renderer work.
