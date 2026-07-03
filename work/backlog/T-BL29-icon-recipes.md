---
id: T-BL29
title: Screenshot-icon recipes — remaining groups (revolve/sweep/loft, Pattern, Multi-body, Curves, Sheet Metal Create, Appearance Display, Manufacture, appr_glass/appr_paint)
workstream: backlog
owner: sonnet-lumen
status: doing
priority: 3
lane: app-gui
kind: fix
depends_on: [T-G15]
branch: task/T-BL29-icon-recipes
---

## Context

T-G15 built the complete screenshot-icon pipeline (`scripts/icon_pipeline.py`) and landed
the first tranche of photographic icons in-context (verified dark+light). The pipeline is
fully re-runnable with one command:

```
python3 scripts/icon_pipeline.py --render
```

That command reads the RECIPES table in `scripts/icon_pipeline.py`, builds demo parts via
the real engine, captures clean GL-viewport thumbnails (no gizmo/labels) via
`AI_CAD_SHOT=iconpipe`, composites them into BOTH theme atlases (byte-preserving all other
cells), marks photographic tiles `raster_only` in `atlas.json` (so they never get
SDF-flattened), and writes a contact sheet.

**First tranche already live (do NOT redo):**
- Features Dress-up: `fillet`, `chamfer`, `shell`
- Features 3D Body: `extrude_boss`
- Appearance Material: `appr_metal` (Aluminum), `appr_plastic`, `appr_default` — real
  preset renders

**This task covers the remaining groups** exactly as enumerated in T-G15's HANDOFF 2 DoD
checkbox. No C++ is expected — the pipeline infrastructure is already in place; work is
recipe-writing + eyeballing + any scenario-side extensions called out below.

### Shared visual contract (must be respected — decided T-G15/T-G16)

- Transparent background, 4dp corner radius — one asset, both themes.
- 32px logical cell, ~2px transparent inset, iso-ish camera, neutral studio lighting.
- Modified/relevant faces emphasized with the **selection-highlight color** — NOTE: as of
  T-G15 HANDOFF 2 the `renderThumbnail` path colors per-body but has NO per-face overlay.
  **Before bulk-generating Dress-up-style groups beyond the current tranche, design the
  per-face emphasis path** (options: per-face sub-mesh color override in the thumb pass, or
  reuse the id-pass selection overlay). This design call gates all groups where face
  emphasis is meaningful (Dress-up extras, Pattern result faces, etc.).
- Photographic tiles enter the atlas as `raster_only` — never SDF.
- Headless runner: `xvfb-run -a python3 scripts/icon_pipeline.py --render`
  (`--ui-screenshot` on the host binary; `--shot` is AI-CAD2 only and silently falls
  through to interactive).

## Definition of done

### Features — 3D Body (remaining)
- [ ] `revolve_boss` recipe: sketch profile + revolve op, emphasize revolved face
- [ ] `sweep_boss` recipe: path sketch + profile sketch + sweep op
- [ ] `loft_boss` recipe: two section sketches + loft op
- [ ] Any additional 3D Body cells in atlas.json covered

### Features — Pattern
- [ ] Op schemas verified against `services/agent/prompts/shared/cheatsheets/` +
      `core/doc/validate.cpp` before writing recipes
- [ ] `pattern_linear` recipe
- [ ] `pattern_circular` recipe
- [ ] `pattern_mirror` recipe (if separate atlas cell)
- [ ] All generated Pattern icons eyeballed — no black/empty tiles

### Features — Multi-body
- [ ] Boolean ops need a **second positioned body** — check box feature position params
      before writing recipes (T-G15 HANDOFF 2 explicit trap)
- [ ] `bool_union` / `bool_cut` / `bool_intersect` (or equivalent atlas cell names)
      recipes written with two-body setup
- [ ] All generated Multi-body icons eyeballed

### Features — Curves
- [ ] Curve op schemas verified (cheatsheets + validate.cpp)
- [ ] Recipes for all Curves-group atlas cells
- [ ] All generated Curves icons eyeballed

### Sheet Metal — Create
- [ ] Sheet Metal tab renders EMPTY headless without doc context (T-G15 HANDOFF 2 noted:
      contextual gating). **Resolve: either add a doc/workspace-mode hook or verify the
      `AI_CAD_SHOT=iconpipe` path can construct a sheet-metal doc** before writing recipes
- [ ] Recipes for Sheet Metal Create group cells
- [ ] All generated Sheet Metal Create icons eyeballed

### Appearance — Display
- [ ] `display_edges`, `display_hlr`, `view_cube` (and any other Display-group cells)
      recipes or verify these are line-art glyphs (not photographic) and confirm they now
      render correctly post SDF-fix from T-G15 (kFar=1<<12)
- [ ] If photographic: recipe + eyeball; if line-art: in-context tab capture confirms no
      regression

### Appearance Material — remaining presets
- [ ] `appr_glass` recipe: appearance-display scene needed — part rendered with glass
      preset showing transparency/refraction in the GL viewport
- [ ] `appr_paint` recipe: appearance-display scene needed — part rendered with paint
      preset

  **Note:** Both presets require the Appearance/Display GL state to be active in the
  viewport during capture. T-G15 HANDOFF 2 flags this as needing "appearance-display
  scenes" — likely a new `AI_CAD_SHOT=iconpipe` recipe variant or a new scenario hook that
  applies the appearance preset before thumbnail capture.

### Manufacture — 2.5D, Holes, 3D, Turning, Cutting
- [ ] **CAM-simulator capture source required** — `cam_simulate` output, NOT the modeling
      viewport (T-G15 DoD explicit). This is new scenario work; budget accordingly.
- [ ] Design the capture path: `cam_simulate` -> screenshot -> crop/composite per the
      visual contract (transparent bg, 4dp radius, ~80% fill)
- [ ] Recipes for all five Manufacture sub-groups
- [ ] All generated Manufacture icons eyeballed

### Wrap-up
- [ ] `python3 scripts/icon_pipeline.py --render` succeeds end-to-end with full recipe set
- [ ] Contact sheet committed to `work/questions/` for Hunter's round-2 review (all groups)
- [ ] `AI_CAD_SHOT=ribbon + AI_CAD_RIBBON_TAB=<tab>` in-context verification for each
      newly covered ribbon tab, dark AND light — look at every PNG (PNG-eyeball trap)
- [ ] `graphify update .` run after any source changes

## Driver notes

**Pipeline entry point:** `scripts/icon_pipeline.py` — add entries to the `RECIPES` dict
(name must be an existing `atlas.json` cell). Run with `xvfb-run -a python3
scripts/icon_pipeline.py --render`. The contact sheet and injected atlases land in
`host/build-linux/bin/assets/icons/`.

**Key files (already mapped by T-G15 — skip the graphify pass on these):**
- `scripts/icon_pipeline.py` — RECIPES table + atlas injection + contact sheet
- `host/src/ui/scenarios.cpp` — `runIconPipe` scenario; extend face ref-selectors here
  as new recipes need them (`vertical_edges`, `all_edges`, `top_face` exist; add more as
  needed)
- `host/src/ui/renderer.{h,cpp}` — `renderThumbnail(transparentBg)` + `readThumbnailRGBA`
- `host/assets/atlas.json` — source of truth for cell names (recipes MUST use existing
  names)
- `host/assets/make_icons.py` — only needed if adding NEW cells; existing cells are fine
- `host/src/ui/ribbon_def.cpp` — cmd_id -> icon-name map (for checking what cells exist
  per group)
- `services/agent/prompts/shared/cheatsheets/` — verify op schemas before writing recipes
- `core/doc/validate.cpp` — canonical op parameter validation

**Reference task:** `work/tasks/T-G15.md` — full pipeline design, shared visual contract,
HANDOFF 2 section with traps and per-group notes.

**Open design questions before bulk generation:**
1. Per-face emphasis overlay in `renderThumbnail` (see contract note above) — decide
   direction before Dress-up-style groups beyond current tranche.
2. Sheet Metal tab contextual gating — needs a workspace-mode hook or doc context.
3. Manufacture: `cam_simulate` capture path design — no precedent in current pipeline.
4. appr_glass/appr_paint: appearance-display GL state during capture — may need a new
   recipe variant or scenario hook.

## Progress log
