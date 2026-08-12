# Contract: Icon Pipeline — v1

> ~90 programmatically generated icons (PIL, like the proven `host/assets/make_logo.py`),
> rendered into TWO atlases (light + dark theme) at TWO sizes. No hand-drawn assets.

## Generation

- Script: `host/assets/make_icons.py` → `host/assets/icons/atlas_dark.png`,
  `atlas_light.png`, `atlas.json`. Deterministic (no timestamps) so regeneration is diffable.
- Each icon drawn at 8× supersample, downsampled LANCZOS. Cell sizes: **32 px** (ribbon big
  buttons) and **20 px** (small buttons, tree rows) — both sizes packed into the same atlas,
  names suffixed `@32` / `@20` in the JSON.
- Style: 2 px-equivalent stroke, rounded joins, flat fills; dark atlas uses light strokes
  (#E5EAF1) + teal accent (#2DD4BF); light atlas uses SolidWorks-ish slate strokes (#3A4350) +
  accent #1A8C7F. Status badges (error/warning) red/amber in both.
- `atlas.json`: `{"cell": {"name@size": {"x":…, "y":…, "w":…, "h":…}}, "atlas_px": [w,h]}`.
- C++ loader: `host/src/ui/icons.cpp` — `Icon(name, size)` → ImTextureID + UVs; theme switch
  swaps the bound atlas, UVs identical (both atlases MUST share one layout).

## Inventory (names)

- **App/tree**: `app, part, assembly, sketch_node, plane, axis, point, csys, origin, equations,
  material, mates_folder, body_folder, history, rollback, error_badge, warning_badge, ok_badge,
  suppressed`
- **Features tab**: `extrude_boss, extrude_cut, revolve_boss, revolve_cut, sweep, sweep_cut,
  loft, loft_cut, boundary, fillet, chamfer, shell, draft, rib, hole_wizard, linear_pattern,
  circular_pattern, mirror, ref_plane, ref_axis, ref_point, ref_csys, curve_helix, combine,
  intersect, subtract, split, wrap`
- **Sketch tab**: `sketch, smart_dim, line, rect_corner, rect_center, circle, arc_3pt,
  arc_center, arc_tangent, polygon, slot, spline, ellipse, point_ent, text_ent, trim, extend,
  offset_ent, convert_ent, mirror_ent, pattern_lin_ent, pattern_circ_ent, move_ent,
  construction, relations, fix`
- **Sketch flyouts** (variant tools, parented under their base tool): `rect_3pt_corner,
  rect_3pt_center, parallelogram, circle_perimeter, slot_ctr_str, slot_arc_3pt, slot_arc_ctr,
  spline_style, spline_surface, spline_equation, ellipse_partial, parabola, conic`
- **Assembly tab**: `insert_component, mate, move_component, rotate_component,
  pattern_component, smart_fasteners, exploded_view, bom, interference`
- **Evaluate tab**: `measure, mass_props, section_props, interference_eval, draft_analysis,
  thickness, curvature, zebra, symmetry_check, check, geometry_analysis, performance`
- **Simulation tab**: `study_static, study_modal, study_flow, fixture, load_force,
  load_pressure, mesh, run_study, results, flow_arrow`
- **Viewport HUD**: `zoom_fit, view_cube, display_shaded, display_edges, display_wire,
  section_view, orientation`
- **General**: `ok_check, cancel_x, settings, save, open, new_doc, undo, redo, theme_toggle,
  ai_panel, activity, prompt_edit`
- **Simulation (extended)**: `study_thermal, study_fatigue`
- **Mesh types**: `mesh_tet, mesh_hex, mesh_auto`
- **Sheet Metal**: `sm_base_flange, sm_edge_flange, sm_hem, sm_jog, sm_flat_pattern`
- **Weldments**: `weld_member, weld_gusset, weld_end_cap`
- **Workspace tabs**: `ws_features, ws_sketch, ws_evaluate, ws_assembly, ws_sheetmetal,
  ws_weldments, ws_surfaces, ws_appearance`
- **Appearance**: `color_picker, appr_metal, appr_plastic, appr_glass, appr_paint, appr_default`

## Sketch-tool glyphs

Sketch-tool icons depict the **result/action** of each tool, not a generic emblem: the produced
geometry is the bold stroke shape and the defining interaction is an accent cue — endpoints &
picks (line, rectangle corners, circle center+rim, arc 3-points), centerlines (center rectangle,
slot, the construction dash-dot line), a dimension line with double arrows + value tag
(`smart_dim`), scissors over a removed red-dashed segment (`trim`), a dashed reach-to-wall
(`extend`), parallel offset curves (`offset_ent`), edge→sketch projection arrows (`convert_ent`),
a mirror axis with a reflected asymmetric shape (`mirror_ent`), seed + copies along a direction or
ring (`pattern_lin_ent` / `pattern_circ_ent`), and a source → dashed-ghost translation
(`move_ent`). Curves (`spline`, `spline_style`, `parabola`, `conic`) are sampled smooth paths
through their control points. All are tuned to read at **20 px and 32 px in both themes**. Glyph
drawings may be revised freely; the **names and atlas layout (UVs) stay stable**, so a redraw
never shifts any other icon's cell.

Adding an icon = add a draw function + name here; atlas layout is computed, never hand-packed.

## Manufacture toolpath tiles (T-BL34, append-only)

The Manufacture ribbon op buttons get dedicated cells (previously they borrowed Features
glyphs). Per Hunter's directive (2026-07-04) the shipped art is a **photographic render of
the real generated toolpath** — `scripts/cam_toolpath_icons.py` drives
`cam_new → add_tool/add_setup/add_operation → cam_generate → cam_simulate` and draws the
returned moves (backplot palette: rapid/lead/cut/plunge/arc) over the stock silhouette.
The render doubles as a CAM-chain smoke test; an op that yields no renderable toolpath is
a CAM bug to file, never a stylized glyph. `make_icons.py` carries line-art fallbacks so a
cell is never blank.

- **2.5D**: `cam_face, cam_contour2d, cam_pocket2d, cam_adaptive2d, cam_slot,
  cam_chamfer2d, cam_engrave`
- **Holes**: `cam_drill, cam_thread_mill`
- **3D**: `cam_parallel3d, cam_waterline3d, cam_scallop3d, cam_pocket3d`
- **Turning**: `cam_turn_rough, cam_turn_finish, cam_turn_face, cam_turn_part`
- **Cutting**: `cam_cut2d, cam_probe`

## Parts Library (GUI V2, append-only)

The Parts Library workspace gets dedicated glyphs: one per taxonomy top-level category
(drawn as the category's signature part — hex bolt, ball bearing, stock shapes, sprocket +
chain, wheel, wrench, end mill, IC chip, hydraulic cylinder, nut + washer) plus panel
chrome for the vendor/browser/filter/health UI. `host/src/ui/parts/parts_widgets.cpp`
`partIcon()` maps taxonomy + legacy domain ids onto these names and falls back to the
in-code `domainGlyph` vector art for unmapped ids, so a row is never icon-less.

- **Categories**: `parts_all, cat_fasteners, cat_bearings, cat_stock_metal, cat_drivetrain,
  cat_wheels, cat_tools, cat_cutting_tools, cat_electronics, cat_hydraulics, cat_hardware`
- **Panel chrome**: `vendor_globe, browser_panel, filter_funnel, health_check`

## Chat context refs (Wave 4, append-only)

Composer copilot toolbar icons for the chat context-reference feature:

- `paperclip` — composer file-attach button (remade crisp diagonal double-wire clip;
  replaces the old inline ImDrawList glyph, now a proper atlas id).
- `context_cube` — composer context-pick button; the Chudware logo cube drawn outline-only
  (transparent background, theme stroke so the SDF tints by icon/text role). Arms the
  viewport/feature-tree context picker (AppState::contextPickActive).

## 2026-07-16 append (batch 9)
- `msg_edit` — General: chat user-bubble edit pencil (procedural, shared draw_pencil).
- `ask` — General/copilot: speech bubble with question mark for Copilot Ask mode
  (read-only Q&A). Procedural line-art; no baked plate.

- `eng_firm` — General/copilot: classical portico (pediment + three columns + base)
  for the Copilot **Eng. Firm** org mode. Procedural line-art (accent pediment,
  stroke columns); no baked plate. @32/@20.

## 2026-07-24 append (workspace rail)

The workspace rail had glyphs for only 8 of its tabs; the app logged
`[icons] unknown icon: 'ws_parts'` / `'ws_manufacture'` on every run and fell
through to procedural `TbIcon` art. Both are line-art in the existing `ws_*`
idiom (2 px stroke, accent highlight, no baked plate), appended LAST in
`make_icons.py` `CONTRACT_NAMES` so every pre-existing cell keeps its UVs.

- `ws_parts` — Parts Library tab: hex bolt + cog. The head is a flat-top hex
  squashed ~15% vertically; a regular point-up hex downsamples to a circle at
  20 px and the glyph then reads as a magnifier.
- `ws_manufacture` — Manufacture tab: an end mill (accent, chamfered tip, two
  flutes) cutting into a neutral stock slab. Flutes are drawn at full alpha
  because the atlas paste premultiplies (RGB*a, a^2) and a translucent stroke
  bakes down into one dark bar.

Neither belongs in `atlas.json` `raster_only` — that list is for photographic
tiles that must never derive an SDF. `icon_pipeline.py` had listed both names
there years before either had a cell; `make_icons.py` now prunes carried-forward
`raster_only` entries whose cell it draws itself.

## 2026-08-12 append (Copilot Artifacts + PLM Graph, Wave 0)

Thirteen new procedural glyphs, appended LAST in `make_icons.py`
`CONTRACT_NAMES` so every pre-existing cell keeps its UVs (verified: 478/478
prior cells byte-identical rects; atlas grew 56x8126 -> 56x8568). Same idiom as
the rest of the inventory: 2 px-equivalent stroke, flat fills, 8x supersample
downsampled LANCZOS, dark `#E5EAF1`/`#2DD4BF` and light `#3A4350`/`#1A8C7F`, no
baked plate. None belongs in `atlas.json` `raster_only`.

**File-kind glyphs.** The Copilot Artifacts panel drew one 2 px grey dot per
row whatever the artifact was; these give it a type read at 20 px. All eight
share the `_artifact_page()` silhouette (portrait sheet, folded top-right
corner) so a mixed list scans as one family, and differ only in the motif inked
into the lower two thirds — the page interior is ~10 px wide at 20 px, so each
motif is one bold shape, never fine detail.

- `file_image` — sun over a two-peak ridge (PNG/JPG/render).
- `file_mesh` — subdivided wireframe triangle, edges only (STL/OBJ/PLY).
- `file_step` — opaque three-face cube (STEP/IGES/SLDPRT). Hand-rolled rather
  than `shaded_prism`: that helper's face alphas (36/54/72) read as a wireframe
  box at any size, i.e. as `file_mesh`. Solid-vs-wireframe is the entire
  distinction between the two, so the faces here are near-opaque and stepped in
  value (235/150/100).
- `file_doc` — accent title rule + two text rules (PDF/MD/TXT).
- `file_data` — table grid with a filled header band (JSON/CSV/XML/YAML). A
  grid, not braces: braces collapse into two blobs on the 20 px downsample.
- `file_code` — bold angle brackets.
- `file_archive` — two stacked crates under one strap. A zipper's teeth mush
  into a grey bar below ~24 px, so the archive motif is one size class coarser.
- `file_generic` — plain folded page, oversized fold, deliberately empty: the
  fallback must not imply a kind it does not know.

**Panel chrome.**

- `artifacts` — stacked sheets with a corner fold, for the Copilot Artifacts
  tab header. **Supersedes that panel's misuse of `team`**, the multi-person
  glyph from the corporate/org iconography, which said "people" on a documents
  panel. `team` keeps its cell and its PLM Project Hub meaning. Only two sheets
  are drawn: at 20 px a third layer lands inside its neighbour's stroke and the
  stack downsamples into a grey block.
- `copy` — already in the inventory since v1 (two offset sheets, the standard
  copy-to-clipboard mark) and reused as-is; no new cell, no redraw.

**PLM Graph toolbar.**

- `orbit_auto` — auto-rotate toggle: a body inside a tilted orbit with a motion
  arrowhead. The orbit is deliberately flatter than the body is wide
  (`ry < r_sphere`) so its near half crosses in FRONT of the body and its far
  half is clipped behind it. A ring merely tangent to the sphere reads as an
  eye, which is already `hide_show_eye`.
- `depth_3d` — depth-cue toggle: three planes receding with perspective, near
  plane wide/accent/opaque. Far-plane alpha bottoms out around 110; below that
  the slate light-theme stroke vanishes on a light panel at 20 px and only two
  planes read.
- `legend` — three colour key dots (accent / warn / stroke), each labelled by a
  short rule of decreasing length.
- `isolate` — "show only this subtree": the kept node filled inside an accent
  focus ring, three neighbours dimmed to outlines on dim links. Neighbour alpha
  is 170, not the ~100 that reads as "dim" at 32 px — below ~150 they downsample
  to nothing at 20 px and the glyph collapses into a plain target mark.
