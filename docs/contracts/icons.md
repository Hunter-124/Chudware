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

Adding an icon = add a draw function + name here; atlas layout is computed, never hand-packed.
