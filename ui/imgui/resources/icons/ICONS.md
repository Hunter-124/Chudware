# AI-CAD Icon Set — Asset Guide

This is the authoritative list of every icon the new GUI needs. The build renders these into a
multi-size atlas; each `IconId` enum value **must** have a source here or the build fails (no silent
magenta "?" — that bug is designed out).

## How icons are sourced

1. **Base UI + common glyphs → a vetted MIT vector set.** Default plan: **Lucide** (https://lucide.dev,
   ISC license) for chrome/navigation/file/generic actions. Drop the raw `.svg` into
   `base/<name>.svg`. The build rasterizes them.
2. **CAD-operation glyphs → custom.** Extrude/Revolve/Fillet/etc. don't exist in general icon sets.
   These need to be drawn. Until a real glyph exists, a neutral monochrome **placeholder** renders
   (a dashed rounded square with the tool's initial) — usable, never broken.
3. **Filling placeholders.** Three options, in order of preference:
   - **(a) Image-gen.** Use the prompt in each row below. Target: a single-color (currentColor) line
     icon, 2px stroke, 24×24 grid, rounded caps, no fill, no text, transparent background, centered
     with ~2px padding. Export monochrome SVG → `custom/<name>.svg`.
   - **(b) Cropped screenshot.** For a few "show the real result" affordances (e.g. material swatches,
     appearance thumbnails) a cropped PNG of the actual part in-app is fine → `raster/<name>.png`.
   - **(c) Hand-draw** in the draw-list fallback (`icon_draw.cpp`) for ultra-simple shapes.

## File layout
```
app/resources/icons/
  ICONS.md          ← this file
  base/             ← Lucide (or chosen MIT set) SVGs, name == IconId
  custom/           ← custom CAD-op SVGs, name == IconId
  raster/           ← optional PNGs (swatches, thumbnails)
  atlas.gen.json    ← generated at build (do not edit)
```
Naming: file basename == the `IconId` string exactly (snake_case). One source per id. The build
asserts coverage of the `IconId` enum.

---

## A. Chrome / shell  (use Lucide — names map directly)
| IconId | Lucide ref | Use |
|---|---|---|
| `menu` | `menu` | app (☰) menu button |
| `search` | `search` | global search / palette trigger |
| `win_min` | `minus` | titlebar minimize |
| `win_max` | `square` | titlebar maximize |
| `win_restore` | `copy` | titlebar restore |
| `win_close` | `x` | titlebar close |
| `tab_close` | `x` | doc-tab close |
| `tab_add` | `plus` | new doc |
| `settings` | `settings` | settings |
| `theme` | `sun-moon` | theme toggle |
| `panel_left` | `panel-left` | toggle left dock |
| `panel_right` | `panel-right` | toggle agent panel |
| `reset_layout` | `layout-dashboard` | reset layout |
| `ok_check` | `check` | confirm (replaces old `ok_check`) |
| `cancel_x` | `x` | cancel (replaces old `cancel_x`) |
| `chevron_down` | `chevron-down` | flyout / split-button |
| `more` | `more-horizontal` | overflow |
| `undo` | `undo-2` | edit undo |
| `redo` | `redo-2` | edit redo |
| `copy` | `copy` | copy |
| `cut` | `scissors` | cut |
| `paste` | `clipboard` | paste |
| `delete` | `trash-2` | delete |
| `save` | `save` | save |
| `folder` | `folder` | folder / open |
| `file_part` | `box` | part document |
| `file_assembly` | `boxes` | assembly document |
| `file_drawing` | `file-text` | drawing document |
| `file_schematic` | `circuit-board` | schematic document |
| `file_pcb` | `cpu` | PCB document |
| `refresh` | `refresh-cw` | rebuild / refresh |
| `zoom_fit` | `maximize` | fit view |
| `zoom_area` | `scan` | zoom to area |

## B. Activity rail
| IconId | Lucide ref | Use |
|---|---|---|
| `rail_tree` | `list-tree` | model Tree (history) |
| `rail_bodies` | `layers` | bodies / layers |
| `rail_files` | `folder-tree` | files explorer |
| `rail_configs` | `sliders-horizontal` | configurations |
| `rail_search` | `search` | search in document |

## C. Workspaces
| IconId | Ref | Use |
|---|---|---|
| `ws_model` | `box` | Model workspace |
| `ws_sketch` | `pen-tool` | Sketch workspace |
| `ws_sheetmetal` | custom | Sheet Metal (folded-plate glyph) |
| `ws_weldment` | custom | Weldment (I-beam glyph) |
| `ws_surface` | custom | Surface (curved-patch glyph) |
| `ws_assemble` | `boxes` | Assemble |
| `ws_draft` | `ruler` | Drafting |
| `ws_simulate` | `activity` | Simulate |
| `ws_schematic` | `circuit-board` | Schematic |
| `ws_board` | `cpu` | Board |

## D. Verb-group headers
| IconId | Ref | Use |
|---|---|---|
| `grp_create` | `plus-circle` | Create group |
| `grp_modify` | `wand-2` | Modify group |
| `grp_pattern` | `grid-3x3` | Pattern group |
| `grp_construct` | `axis-3d` | Construct (reference geom) |
| `grp_inspect` | `ruler` | Inspect group |
| `grp_output` | `share` | Output group |

## E. CAD operations — CUSTOM glyphs needed (image-gen prompts)
> Style for all: monochrome line icon, 2px stroke, 24×24, rounded caps, no fill/text, transparent bg.

| IconId | Image-gen prompt |
|---|---|
| `extrude` | "a 2D rectangle profile being pushed into a 3D box along an arrow, isometric, line icon" |
| `revolve` | "a profile rotating around a vertical axis forming a solid of revolution, circular arrow, line icon" |
| `sweep` | "a small circle profile swept along a curved path, line icon" |
| `loft` | "two stacked different-shaped profiles blended into a lofted solid, line icon" |
| `rib` | "a thin reinforcing rib between two walls, line icon" |
| `hole` | "a drill bit over a plate with a counterbored hole cross-section, line icon" |
| `coil` | "a helical spring / coil, line icon" |
| `fillet` | "a cube corner edge with one edge rounded, highlighting the rounded edge, line icon" |
| `chamfer` | "a cube corner with one edge cut at 45 degrees (chamfered), line icon" |
| `shell` | "a hollowed-out box showing thin walls (shelled), cutaway, line icon" |
| `draft` | "a tapered/angled face of a block with a draft-angle arrow, line icon" |
| `split` | "a solid body split by a plane into two halves, line icon" |
| `combine` | "two overlapping solids merging (boolean union), line icon" |
| `subtract` | "one solid cutting a cavity out of another (boolean subtract), line icon" |
| `intersect` | "the overlapping intersection volume of two solids, line icon" |
| `pattern_linear` | "a small box repeated in a straight row, line icon" |
| `pattern_circular` | "a small box repeated around a center point in a circle, line icon" |
| `mirror` | "a shape and its mirror reflection across a dashed center line, line icon" |
| `ref_plane` | "a tilted reference plane (parallelogram) in 3D space, line icon" |
| `ref_axis` | "a dashed reference axis line through space with end ticks, line icon" |
| `ref_point` | "a single reference point with small crosshair, line icon" |
| `ref_csys` | "a 3-axis XYZ coordinate triad, line icon" |
| `measure` | "calipers measuring a distance, line icon" |
| `mass_props` | "a cube on a balance scale (mass properties), line icon" |
| `section_props` | "a cross-section cut of a beam with hatching, line icon" |
| `analyze_draft` | "a part shaded by draft-angle color zones, line icon" |
| `analyze_thickness` | "a wall cross-section with a thickness arrow, line icon" |
| `analyze_curvature` | "a curved surface with curvature comb lines, line icon" |
| `check` | "a magnifier over a solid with a check mark (geometry check), line icon" |
| `interference` | "two overlapping parts with the clash volume highlighted, line icon" |

## F. Sketch tools — CUSTOM (image-gen prompts, same style)
| IconId | Prompt |
|---|---|
| `sk_line` | "a straight line segment with two endpoints, line icon" |
| `sk_rect` | "a rectangle drawn from a corner, line icon" |
| `sk_circle` | "a circle with center dot, line icon" |
| `sk_arc` | "a 3-point arc with endpoints, line icon" |
| `sk_polygon` | "a regular hexagon, line icon" |
| `sk_slot` | "a straight slot (stadium/capsule shape), line icon" |
| `sk_spline` | "a smooth spline curve with control points, line icon" |
| `sk_ellipse` | "an ellipse with major/minor axes, line icon" |
| `sk_point` | "a single sketch point (small plus), line icon" |
| `sk_text` | "a capital letter A (sketch text), line icon" |
| `smart_dim` | "a dimension line with arrows and a value box, line icon" |
| `sk_trim` | "a knife trimming a line segment, line icon" |
| `sk_extend` | "a line being extended to a boundary with an arrow, line icon" |
| `sk_offset` | "two parallel offset curves, line icon" |
| `sk_convert` | "an edge being converted into a sketch entity (arrow from solid edge to line), line icon" |
| `sk_construction` | "a dashed construction line, line icon" |
| `relations` | "two entities linked by a constraint symbol (chain link), line icon" |
| `fix` | "a padlock pinning a point (fix constraint), line icon" |

## G. Simulation
| IconId | Ref/custom | Use |
|---|---|---|
| `sim_static` | custom | "a beam bending under a downward arrow load (static study)" |
| `sim_modal` | custom | "a vibrating/oscillating beam with motion lines (modal)" |
| `sim_flow` | custom | "streamlines flowing around an airfoil (flow study)" |
| `sim_thermal` | custom | "a part with a heat gradient and thermometer (thermal)" |
| `sim_fatigue` | custom | "a part with a crack and cyclic-load curve (fatigue)" |
| `material` | `palette` | material assignment |
| `fixture` | custom | "a fixed/grounded support (hatched ground symbol)" |
| `load_force` | custom | "a bold force arrow on a face" |
| `load_pressure` | custom | "many small arrows pressing on a face (pressure)" |
| `mesh` | `grid-3x3` | mesh |
| `run_study` | `play` | run |
| `results` | `bar-chart-3` | results |

## H. Assembly / Drawing / ECAD (mostly custom or Lucide)
| IconId | Ref/custom | Use |
|---|---|---|
| `insert_component` | custom | "a part dropping into an assembly (plus on a box)" |
| `std_parts` | `library` | standard parts library |
| `move_component` | `move-3d` | move component |
| `mate` | custom | "two faces snapping together with alignment marks (mate)" |
| `pattern_component` | `grid-3x3` | component pattern |
| `bom` | `table` | bill of materials |
| `exploded_view` | custom | "an exploded assembly with parts separated along arrows" |
| `dwg_view` | custom | "an orthographic drawing view frame with a part silhouette" |
| `dwg_projected` | custom | "a first-angle projection arrow between two view frames" |
| `dwg_section` | custom | "a section view with cutting-plane line and hatching" |
| `dwg_detail` | custom | "a circled detail callout with magnified view" |
| `gdt` | custom | "a GD&T feature control frame box with symbol" |
| `datum` | custom | "a datum feature symbol (boxed letter with triangle)" |
| `balloon` | custom | "a numbered balloon callout with leader line" |
| `note` | `type` | text note |
| `title_block` | custom | "a drawing title block grid in the corner of a sheet" |
| `sch_symbol` | custom | "a resistor schematic symbol" |
| `sch_wire` | custom | "two schematic pins joined by a wire with a junction dot" |
| `net_label` | `tag` | net label |
| `power_port` | custom | "a VCC power port symbol (line with bar)" |
| `pcb_route` | custom | "a PCB copper trace routed between two pads" |
| `via` | custom | "a PCB via (concentric annular ring with drill)" |
| `copper_zone` | custom | "a hatched copper pour zone outline" |
| `autoroute` | `route` | autoroute |
| `drc` | `shield-check` | design-rule check |
| `gerber` | `file-output` | gerber export |
| `board_3d` | custom | "a populated PCB shown in 3D isometric" |
| `ai_design` | `sparkles` | AI design entry |

## I. Agent panel
| IconId | Ref | Use |
|---|---|---|
| `agent` | `sparkles` | agent avatar / panel icon |
| `thinking` | `brain` | thinking block |
| `action` | `wrench` | tool-call/action block |
| `agent_send` | `arrow-up` | composer send |
| `agent_stop` | `square` | stop run |
| `attach` | `paperclip` | attach image/file |
| `model_pick` | `chevron-down` | model picker |
| `expand` | `chevron-right` | expand action card |
| `success` | `check-circle-2` | action succeeded |
| `failure` | `x-circle` | action failed |
| `running` | `loader` | spinner (animated) |

---

## Generation workflow (for the user / image-gen model)
1. Pick a style lock: monochrome, 2px stroke, 24px grid, rounded caps, no fill, transparent bg,
   `currentColor` (so theme tinting works).
2. For each `custom` row above, generate the SVG from its prompt; save as `custom/<IconId>.svg`.
3. Keep them visually consistent with the Lucide base set (same weight/feel) so the toolbar reads as
   one family.
4. Re-run the build; the atlas regenerates and the coverage assert confirms none are missing.
5. Swatch/thumbnail icons (materials, appearances) may instead be cropped PNGs of real in-app parts
   → `raster/<IconId>.png`.
</content>
