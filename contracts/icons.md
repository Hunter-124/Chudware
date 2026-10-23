# Contract: Icon Pipeline — v1

> ~90 programmatically generated icons (PIL, like the proven `assets/make_logo.py`),
> rendered into TWO atlases (light + dark theme) at TWO sizes. No hand-drawn assets.

## Generation

- Script: `assets/make_icons.py` → `host/assets/icons/atlas_dark.png`,
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
- **Sketch tab (relations management)**: `display_relations, fully_define`
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

## Manufacture toolpath tiles (append-only)

The Manufacture ribbon op buttons get dedicated cells (previously they borrowed Features
glyphs). Per a 2026-07-04 design decision the shipped art is a **photographic render of
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
---

## 2026-08-17 — Full 3D-render coverage

**25 new cells (appended LAST in `CONTRACT_NAMES`, so every pre-existing cell
keeps its packed UVs).** Each ships an engine-rendered tile; the `make_icons.py`
drawers added alongside them are FALLBACK glyphs only, seen if a render is
missing.

**Sketch / model.**

- `rotate_ent` — rotate selected sketch entities about a pivot.
- `stock` — the raw rectangular billet a part starts from.

**Workspace tabs.**

- `ws_model` — Model workspace: the solid part itself.
- `ws_drawing` — Drawing workspace: sheet with a projected view and a dimension.
- `ws_electronics` — Electronics workspace: board with a chip and traces.
- `ws_simulate` — Simulate workspace: a loaded, meshed body.

**ECAD (board editor).**

- `ecad_pcb` — open/edit the PCB layout.
- `ecad_board3d` — 3D board preview with placed components.
- `ecad_footprint` — footprint (land-pattern) editor.
- `ecad_route` — route a track manually.
- `ecad_via` — place a via through the stack-up.
- `ecad_zone` — define a copper zone (hatched pour region).
- `ecad_autoroute` — route the remaining nets automatically.
- `ecad_fill` — flood/refill zones with copper, honouring keep-outs.
- `ecad_gerber` — export Gerber layer set.
- `ecad_drill` — export the drill (hole) file.
- `ecad_drc` — run design-rule check.
- `ecad_update` — update the board from the schematic.

**Schematic.**

- `sch_new` — new schematic sheet.
- `sch_symbol` — place a component symbol.
- `sch_wire` — draw a wire/net with junctions.
- `sch_power` — place a power or ground port.
- `sch_label` — attach a net label.
- `sch_erc` — run electrical-rule check.
- `sch_simulate` — simulate the circuit (waveform).

**Render coverage.** Every ribbon tool icon across Sketch, Evaluate, Simulation,
Assembly, the Features leftovers (`boundary`, `wrap`, `ref_*`), the workspace
tabs, ECAD and Schematic now ships an engine-rendered tile produced by
`scripts/icon_pipeline.py` recipes (`CHUDWARE_SHOT=iconpipe`, real modelling
engine, no hand-painted pixels). All of those names are listed in
`RENDERED_RECIPE_NAMES`, so a full `python assets/make_icons.py` glyph regen
pastes the rendered pixels back instead of clobbering them; the procedural
drawers remain as the never-blank fallback.

**Colour language** (supersedes the old flat-teal look).

- Base / stock / source / ghost / cut bodies: steel grey (`0.66, 0.69, 0.73`,
  metallic 0.85, roughness 0.38).
- The tool's ACTION — newly created material, the result or the copy — carries
  the teal accent. Domain materials keep their own colour: PCB green, copper,
  black plastic, white sheet, red-hot / blue-cold for thermal.
- Each tile carries at most one (rarely two) annotation cue — arrow, dimension
  arrow, dash, arc, ring, dots or a corner badge — pointing in the tool's
  direction of action, overlaid after render and bolded so it reads at 32 px.

## Texture-size budget — `ATLAS_MAX_PX`

The packed atlas MUST stay within **`ATLAS_MAX_PX` (2048)** on **both** sides.
GLES3 and WebGL2 only *guarantee* a 2048 px `MAX_TEXTURE_SIZE`; a desktop GPU
that happily takes more tells you nothing about the browser. A 56x9418
single-column atlas made `glTexImage2D` fail outright, and because the loader
then had no texture, **every icon in the browser drew as an opaque black
square** — a total-loss failure with no partial symptom to notice early.

Growth is therefore packed in columns, not one endless strip: `make_icons.py`
fills a column to the budget before starting the next. When a committed atlas
outgrows the budget, `python3 assets/make_icons.py --relayout` re-packs it
**pixel-for-pixel** (same cells, new grid, UVs recomputed in `atlas.json`) —
no glyph is redrawn, so nothing regresses visually.
`assets/test_icons.py::test_fits_in_a_texture` is the guard and asserts on the
committed `atlas_px`. Raise `ATLAS_MAX_PX` only after checking the real
`GL_MAX_TEXTURE_SIZE` of every target, WebGL2 included.

## Desktop/web parity — the atlas is baked into the web bundle

The two targets load the **same bytes by different mechanisms**, and this is
the trap:

- **Desktop** reads `assets/icons/` off disk at startup. Regenerate the atlas
  and the next launch picks it up — free, immediate, no build step.
- **Web** has no filesystem. `host/CMakeLists.txt` `--preload-file`s
  `assets/icons/atlas.json`, `atlas_dark.png` and `atlas_light.png` (alongside
  the fonts and logos) and Emscripten bakes them into `Chudware.data` at
  **LINK** time. The bundle is a derived artifact: it freezes whatever the
  assets were when it was linked.

**Therefore any atlas regeneration requires relinking the web bundle
(`./build.sh wasm`) and redeploying it.** An atlas change that is not followed
by a relink reaches desktop for free and web *never* — and both targets keep
working, so nothing fails, nothing warns, and the divergence is invisible.
Note also that a preloaded asset is not a tracked build input, so editing one
does not by itself make ninja relink.

This is not hypothetical. Commit **69690e57 (2026-08-17)** replaced every
ribbon tool icon with an engine-rendered 3D tile. Desktop showed the new tiles
the same day; production served a bundle linked **2026-08-07** until
2026-08-22 — **ten days of stale icons**, found only by hand-scanning
`Chudware.data` for PNG bytes. Nothing else could have caught it: no CI job
built WASM, and no check compared a bundle against its sources.

`scripts/check_web_parity.py` is now that check. It reads **Emscripten's own
file table** out of `Chudware.js` and compares the bytes actually embedded in
the pack against the files in the repo, so it measures the shipped artifact
rather than trusting a build log. It verifies a staged directory (`--bundle`)
or what a server really serves (`--url`, via HTTP Range requests for just the
bytes it needs). Its expected-asset list is derived from the `--preload-file`
flags in `host/CMakeLists.txt`, so **new preloaded assets join the gate
automatically** — nothing to remember when adding one. Exit 0 = parity,
1 = divergence, 2 = cannot evaluate (no bundle to inspect: an explicit SKIP,
never a silent pass).

Corollary for this contract: an atlas relayout (above) rewrites both PNGs and
`atlas.json`, so it is an atlas regeneration like any other and carries the
same relink obligation.

## 2026-08-22 append — two-role accent (PRIMARY / SECONDARY)

The single teal accent is retired. Every icon surface now speaks a two-role
vocabulary, and the roles are semantic, not decorative:

- **PRIMARY — `#F28216` orange, `(0.949, 0.510, 0.086)`.** *What the operation
  produced.* The new boss, the swept solid, the filleted edge, the drilled hole.
  One tile has exactly one primary subject; if two things are orange, the tile is
  not saying anything.
- **SECONDARY — `#0078D7` blue, `(0.000, 0.471, 0.843)`.** *The direction or
  annotation cue.* Extrude/sweep/pattern arrows, mirror planes, rotation axes.
  Never the result. The split is what lets a 32 px tile be read at a glance:
  orange answers "what do I get", blue answers "which way".

Both live in `scripts/icon_recipes/helpers.py` as `PRIMARY_RGB` / `SECONDARY_RGB`,
applied by the `accent()` and `accent2()` recipe steps. `ACCENT_RGB` remains as an
alias of `PRIMARY_RGB` so pre-existing recipes keep compiling. The line-art palette
in `assets/make_icons.py` (`DARK`/`LIGHT`) carries the same two roles, with the
light sheet using a deepened `#C25E05` — `#F28216` measures only 2.4:1 against
`#F3F5F7`, which a 2 px hairline cannot survive at @20.

### `atlas.json` gains `accent_tinted` + `accent_ref`

Engine-rendered tiles have the accent **baked into their pixels**, so a user-chosen
accent cannot reach them by tinting at draw time the way line-art SDF glyphs are
tinted. `scripts/icon_pipeline.py --render` therefore records what it baked:

```json
"accent_tinted": ["extrude_boss", "..."],
"accent_ref": {"primary": [0.949, 0.51, 0.086],
               "secondary": [0.0, 0.471, 0.843]}
```

`accent_tinted` is derived from the recipe steps, not a hand-kept list: a tile is in
it iff its recipe calls `accent()`/`accent2()`, so one that stops using them drops
out on the next render. `accent_ref` is the hue pair those tiles were baked WITH.
Everything not listed keeps the color it is *depicting* — a brass appearance swatch
or a CAM toolpath tile must never rotate.
`host/src/ui/icons.cpp::recolorIconAtlas()` maps those reference hues to the user's
colors across those cells only, always recomputing from a pristine copy of the
decoded atlas so repeated recolors never compound. `ui::setAccentColors()`
(`host/src/ui_theme.cpp`) is the single entry point: it writes the accent/accent2
palette families and calls the retint, and the chosen pair persists as
`appearance.accent.primary` / `appearance.accent.secondary`.

**Both keys are pipeline-owned and load-bearing.** `assets/make_icons.py` rewrites
`atlas.json` wholesale, so it carries them forward explicitly via
`PIPELINE_OWNED_ATLAS_KEYS` in both the full regen and `--relayout`. Losing them
does not fail, warn or log: `recolorIconAtlas` finds no cells, returns, and the
user's accent silently stops reaching the 3D tiles — the same shape as the
`raster_only` bug that made icons "break every rebuild". A new pipeline-owned key
must be added to that tuple in the same commit that starts writing it.

## 2026-10-05 append — Simulation GUI overhaul

27 procedural line-art glyphs, appended LAST in `make_icons.py` `CONTRACT_NAMES`.
Simulate tabs/commands had been borrowing unrelated glyphs: Topology and EM used
`study_static`, Heat Loads used `load_pressure`, Flow/EM boundaries used `fixture`,
Piping used `sweep`, and Hydraulics sizing used library-category glyphs. Added
through the byte-preserving `icon_pipeline.ensure_cells()` path, so all 554 prior
cells are pixel-identical (atlas re-laid 280x1904 -> 336x1734). They are glyphs
only, so they are NOT in `RENDERED_RECIPE_NAMES`/`raster_only`. Roles: `p.stroke`
is the structure, primary/accent is what the command acts on, and secondary/accent2
is the direction or extent cue (load, flow, size).

**Study kinds (tab + New Study).**

- `study_topology` — topology optimisation: accent truss load path inside a dashed design space, load arrow from above.
- `study_em` — electromagnetics: antenna mast radiating wavefronts.
- `study_buckling` — buckling: column on a fixed base under axial load, bowed mode shape in accent.
- `study_contact` — contact: roller pressed onto a block, accent contact patch.
- `study_vibration` — vibration / frequency response: spring-mass on a base with motion marks.
- `study_thermal_fea` — thermal FEA (conduction in a solid): meshed block heated from its base, heat rising off the top.
- `study_thermal_cfd` — thermal CFD (conjugate heat transfer): streamlines over a hot plate. Distinct from `study_thermal` (plain thermometer, kept unchanged).
- `study_piping` — piping: pipe run through a 90° elbow, flanged ends.
- `study_hydraulics` — hydraulics: ISO pump feeding a double-acting cylinder.

**Setup / boundary commands.**

- `load_heat` — heat load: wavy heat-flux arrows entering a face (replaces `load_pressure` on Heat Loads).
- `fluid` — fluid properties: droplet (replaces `material` on Flow → Fluid).
- `flow_bc` — flow boundaries: channel with an inlet arrow in and an outlet arrow out (replaces `fixture`).
- `em_port` — EM excitation port: coax end with an incident wave.
- `em_boundary` — EM boundary (radiation/PEC box): dashed accent enclosure around the body.
- `freq_range` — frequency range: chirp between two band markers (replaces `settings`).

**Piping / Hydraulics commands.** `new_*` reuse the tab's study motif plus the accent plus, as one pattern for "New <X>".

- `new_network` — New Network: `study_piping` elbow, reduced, with a plus (replaces `new_doc`).
- `new_circuit` — New Circuit: `study_hydraulics` pump + cylinder, reduced, with a plus (replaces `new_doc`).
- `route_pipe` — Route Pipe: laid pipe with its dashed centreline routed on to a waypoint (replaces `sweep`).
- `size_actuator` — Size Cylinder/actuator: cylinder under a double-headed extent arrow.
- `size_pump` — Size Pump: ISO pump symbol under the extent arrow.
- `size_motor` — Size Motor: ISO hydraulic-motor symbol (triangle pointing in) under the extent arrow.
- `pump_drive` — Pump Drive: electric motor (finned housing, foot, accent shaft, lightning tick).
- `vessel` — Vessel: vertical pressure vessel with dished heads, nozzle, legs (replaces `revolve_boss`).
- `duct` — Duct Sizing: rectangular duct section with airflow arrow (replaces `flow_arrow`).
- `spec_check` — Spec/code check: standard's page with an accent check badge (distinct from `check`).
- `schematic` — schematic view: sheet with a pump wired to a valve block.
- `plots_view` — plots: axes with two response curves (distinct from the `results` bar chart).

## 2026-10-05 append — Motion playback transport

6 procedural glyphs for the Motion playback bar, appended LAST in `CONTRACT_NAMES`
via `icon_pipeline.ensure_cells()`; all 608 prior cells pixel-identical (atlas
re-laid 336x1734 -> 336x1768). Glyph-only (not in `raster_only`). Roles: primary
accent fill for triangles/pause bars, `p.stroke` outline and end-stop bars.

- `media_first` — go to first frame: |◀.
- `media_prev` — step back one frame: ◀|.
- `media_play` — play: ▶.
- `media_pause` — pause: ‖.
- `media_next` — step forward one frame: |▶.
- `media_last` — go to last frame: ▶|.
