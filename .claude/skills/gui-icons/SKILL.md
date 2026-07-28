---
name: gui-icons
description: Chudware icon subsystem specialist — generating icons (procedural PIL icons and screenshot-based icons rendered from scripts that drive the modeling engine), editing the atlases, and wiring icons into the GUI. Use for any task mentioning icons, the icon atlas, ribbon/tree glyphs, icon recipes, contact sheets, or the icon pipeline. Workers dispatched by gui-orchestrator read this file first instead of searching the repo.
---

# GUI Icons Specialist

Scope: everything about how Chudware icons are made, stored, and drawn. You do NOT need to know
anything about the Parts Library, sketcher, or other panels — if your task drifts there, that's
a different specialist's job (`.claude/skills/gui-workspaces/SKILL.md` etc.).

## The two generation paths

**1. Procedural line-art icons** — `assets/make_icons.py` (PIL, deterministic — no timestamps,
so regeneration is diffable). Draws ~90 icons at 8× supersample, LANCZOS downsample, into TWO
theme atlases at TWO sizes:
- Output: `assets/icons/atlas_dark.png`, `assets/icons/atlas_light.png`, `assets/icons/atlas.json`
- Sizes: 32 px (ribbon big buttons) and 20 px (small buttons, tree rows), packed in the same
  atlas, names suffixed `@32`/`@20` in the JSON: `{"cell": {"name@size": {x,y,w,h}}, "atlas_px":[w,h]}`
- Style: 2 px-equivalent stroke, rounded joins, flat fills. Dark atlas: light strokes `#E5EAF1`
  + teal accent `#2DD4BF`. Light atlas: slate strokes `#3A4350` + accent `#1A8C7F`.
- **Both atlases MUST share one pixel layout** — theme switch swaps texture, UVs stay constant.
- Helpers: `assets/pack_icons.py` (packing), `assets/test_icons.py`, `assets/test_pack_icons.py`.

**2. Screenshot-based icons from modeling scripts** — `scripts/icon_pipeline.py` (T-G15/T-BL34).
A recipe (name, camera, build steps) drives the REAL engine headlessly: the GUI runs with
`CHUDWARE_SHOT=iconpipe` + `CHUDWARE_ICON_RECIPES=<json>`, builds each demo part (newPart +
applyOps), renders through the transparent thumbnail path (no chrome, no gizmo, alpha=0) to
`scratch/icon_render/<name>.png`, then fits the render into that icon's @32/@20 cells in BOTH
atlases — all other cells preserved byte-for-byte.
- Run: `python3 scripts/icon_pipeline.py --render`
- Icons carry NO baked background plate (must work on ribbon glass in both themes).
- v2 layers: op-created body colored with app accent teal via `set_appearance`.
- CAM tiles come from real generated G-code: `scripts/cam_toolpath_icons.py`.
- A failed build step yields an empty tile — obvious on the contact sheet
  (`scratch/icon_contact_sheet.png`). Always regenerate the contact sheet for human review.

## The contract (read before adding/renaming anything)

`contracts/icons.md` is the frozen inventory of icon names grouped by ribbon tab
(App/tree, Features, Sketch, Sketch flyouts, Assembly, Evaluate, Simulation, …). Contracts are
append-only: new icons get appended to the inventory, existing names never change or vanish —
`ribbon_def.cpp` and the feature tree reference them by string.

## C++ side — loading and drawing

- `host/src/ui/icons.cpp/.h` — atlas loader: `loadIcons()`, `getIcon(name, size)` →
  `IconUV{texId, uv0, uv1}`. `syncIconTheme()` swaps the bound atlas texture on theme toggle.
- `host/src/ui/icons_sdf.cpp` — SDF rasterization for line-art, crisp at any DPI.
- Consumers reference icons by bare name (no `@32` suffix) — e.g. the `icon` field in
  `RibbonCmd` rows in `host/src/ui/ribbon_def.cpp`; size is picked by context.
- Deployed copy the app actually loads at runtime: `host/build-linux/bin/assets/icons/`
  (the build copies it; if your new icon doesn't show up, check the deployed copy is fresh).

## Adding a new icon, end to end

1. Append the name to `contracts/icons.md` inventory.
2. Procedural: add the draw function to `assets/make_icons.py`, regenerate.
   Screenshot-based: add a recipe and run `scripts/icon_pipeline.py --render`.
3. Verify `assets/icons/atlas.json` now has `name@32` and `name@20`, both atlas PNGs updated,
   identical layout.
4. Reference it by name wherever needed (ribbon row, tree, etc.).
5. Lint: `python3 scripts/gui_lint.py --rule icons` (checks every icon name used in C++ exists
   in the atlas). Then `./build.sh` from repo root must compile.

## Verification handoff

Don't screenshot-judge your own work beyond the contact sheet mechanics. Return to the
orchestrator: files changed, and a one-line user check such as "toggle Settings > theme —
new <name> icon renders crisp on ribbon (32px) and in the feature tree (20px) in both themes."
