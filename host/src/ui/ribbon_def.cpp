// AI-CAD ribbon command table — C14.
// ONE static table: {cmd_id, label, icon, tab, group, big, sub_of, stretch}.
// Sorted by tab / group / big-first to keep later-card appends clean.
// stretch=true = schema-reserved, rendered disabled in v1.
//
// APPEND-ONLY: later cards add rows here; never reorder or delete existing rows.
#include "ribbon_def.h"

namespace ui {

// clang-format off
static const RibbonCmd kCmds[] = {

    // =========================================================================
    // Tab: Features
    // =========================================================================

    // --- Group: Sketch (entry point for the features workflow) ---
    { "sketch_new",        "New Sketch",     "sketch",          "Features", "Sketch",          true,  nullptr,          false },

    // --- Group: 3D Body ---
    { "extrude_boss",      "Extrude",        "extrude_boss",    "Features", "3D Body",         true,  nullptr,          false },
    { "extrude_cut",       "Extrude Cut",    "extrude_cut",     "Features", "3D Body",         true,  nullptr,          false },
    { "revolve_boss",      "Revolve",        "revolve_boss",    "Features", "3D Body",         true,  nullptr,          false },
    { "revolve_cut",       "Revolve Cut",    "revolve_cut",     "Features", "3D Body",         false, "revolve_boss",   false },
    { "sweep_boss",        "Sweep",          "sweep",           "Features", "3D Body",         true,  nullptr,          false },
    { "sweep_cut",         "Sweep Cut",      "sweep_cut",       "Features", "3D Body",         false, "sweep_boss",     false },
    { "loft_boss",         "Loft",           "loft",            "Features", "3D Body",         true,  nullptr,          false },
    { "loft_cut",          "Loft Cut",       "loft_cut",        "Features", "3D Body",         false, "loft_boss",      false },

    // --- Group: Dress-up ---
    { "fillet",            "Fillet",         "fillet",          "Features", "Dress-up",        true,  nullptr,          false },
    { "chamfer",           "Chamfer",        "chamfer",         "Features", "Dress-up",        true,  nullptr,          false },
    { "shell",             "Shell",          "shell",           "Features", "Dress-up",        true,  nullptr,          false },
    { "draft",             "Draft",          "draft",           "Features", "Dress-up",        false, nullptr,          false },
    { "rib",               "Rib",            "rib",             "Features", "Dress-up",        false, nullptr,          false },

    // --- Group: Hole ---
    { "hole_wizard",       "Hole Wizard",    "hole_wizard",     "Features", "Hole",            true,  nullptr,          false },

    // --- Group: Pattern/Mirror ---
    { "linear_pattern",   "Lin. Pattern",   "linear_pattern",  "Features", "Pattern",         true,  nullptr,          false },
    { "circular_pattern", "Circ. Pattern",  "circular_pattern","Features", "Pattern",         true,  nullptr,          false },
    { "mirror_feature",   "Mirror",         "mirror",          "Features", "Pattern",         true,  nullptr,          false },

    // --- Group: Reference Geometry ---
    { "ref_plane",         "Ref. Plane",     "ref_plane",       "Features", "Reference",       true,  nullptr,          false },
    { "ref_axis",          "Ref. Axis",      "ref_axis",        "Features", "Reference",       false, nullptr,          false },
    { "ref_point",         "Ref. Point",     "ref_point",       "Features", "Reference",       false, nullptr,          false },
    { "ref_csys",          "Coord. Sys.",    "ref_csys",        "Features", "Reference",       false, nullptr,          false },

    // --- Group: Multi-body ---
    { "boolean_combine",   "Combine",        "combine",         "Features", "Multi-body",      true,  nullptr,          false },
    { "boolean_subtract",  "Subtract",       "subtract",        "Features", "Multi-body",      false, nullptr,          false },
    { "boolean_intersect", "Intersect",      "intersect",       "Features", "Multi-body",      false, nullptr,          false },

    // --- Group: Curves ---
    { "curve_helix",       "Helix",          "curve_helix",     "Features", "Curves",          true,  nullptr,          false },
    { "split",             "Split",          "split",           "Features", "Curves",          false, nullptr,          false },

    // =========================================================================
    // Tab: Sketch
    // =========================================================================

    // --- Group: Sketch State ---
    { "sketch_exit",       "Exit Sketch",    "ok_check",        "Sketch",   "Sketch",          true,  nullptr,          false },

    // --- Group: Draw ---
    { "sk_line",           "Line",           "line",            "Sketch",   "Draw",            true,  nullptr,          false },
    { "sk_rect_corner",    "Rectangle",      "rect_corner",     "Sketch",   "Draw",            true,  nullptr,          false },
    { "sk_rect_center",    "Ctr. Rect.",     "rect_center",     "Sketch",   "Draw",            false, "sk_rect_corner", false },
    { "sk_circle",         "Circle",         "circle",          "Sketch",   "Draw",            true,  nullptr,          false },
    { "sk_arc_3pt",        "3-Pt Arc",       "arc_3pt",         "Sketch",   "Draw",            true,  nullptr,          false },
    { "sk_arc_center",     "Ctr. Arc",       "arc_center",      "Sketch",   "Draw",            false, "sk_arc_3pt",     false },
    // C54: tangent arc wired — starts tangent to the prior/selected entity's free endpoint.
    { "sk_arc_tangent",    "Tang. Arc",      "arc_tangent",     "Sketch",   "Draw",            false, "sk_arc_3pt",     false },
    { "sk_polygon",        "Polygon",        "polygon",         "Sketch",   "Draw",            true,  nullptr,          false },
    { "sk_slot",           "Slot",           "slot",            "Sketch",   "Draw",            true,  nullptr,          false },
    { "sk_spline",         "Spline",         "spline",          "Sketch",   "Draw",            true,  nullptr,          false },
    { "sk_ellipse",        "Ellipse",        "ellipse",         "Sketch",   "Draw",            false, nullptr,          false },
    { "sk_point",          "Point",          "point_ent",       "Sketch",   "Draw",            false, nullptr,          false },
    { "sk_text",           "Text",           "text_ent",        "Sketch",   "Draw",            false, nullptr,          false },

    // --- Group: Smart Dimension ---
    { "smart_dim",         "Smart Dim.",     "smart_dim",       "Sketch",   "Dimension",       true,  nullptr,          false },

    // --- Group: Sketch Tools ---
    { "sk_trim",           "Trim",           "trim",            "Sketch",   "Sketch Tools",    true,  nullptr,          false },
    { "sk_extend",         "Extend",         "extend",          "Sketch",   "Sketch Tools",    true,  nullptr,          false },
    { "sk_offset_ent",     "Offset",         "offset_ent",      "Sketch",   "Sketch Tools",    true,  nullptr,          false },
    // C52: line-line corner fillet / chamfer (engine ops sketch_fillet / sketch_chamfer).
    { "sk_fillet",         "Sk. Fillet",     "fillet",          "Sketch",   "Sketch Tools",    false, nullptr,          false },
    { "sk_chamfer",        "Sk. Chamfer",    "chamfer",         "Sketch",   "Sketch Tools",    false, nullptr,          false },
    // C53: convert / mirror / linear+circular patterns wired to real engine ops
    // (sketch_convert / sketch_mirror_entities / sketch_pattern_entities).
    { "sk_convert_ent",    "Convert",        "convert_ent",     "Sketch",   "Sketch Tools",    true,  nullptr,          false },
    { "sk_construction",   "Construction",   "construction",    "Sketch",   "Sketch Tools",    false, nullptr,          false },
    { "sk_mirror",         "Mirror",         "mirror_ent",      "Sketch",   "Sketch Tools",    false, nullptr,          false },
    { "sk_lin_pattern",    "Lin. Pattern",   "pattern_lin_ent", "Sketch",   "Sketch Tools",    false, nullptr,          false },
    { "sk_circ_pattern",   "Circ. Pattern",  "pattern_circ_ent","Sketch",   "Sketch Tools",    false, nullptr,          false },
    // C54: Move/Copy wired to the sketch_move engine op (select entities, from-pt -> to-pt delta).
    { "sk_move",           "Move",           "move_ent",        "Sketch",   "Sketch Tools",    false, nullptr,          false },

    // --- Group: Relations ---
    // C42: sk_relations wired (Add Relation popup over the selection). sk_fix greyed — Fix is
    // reachable via that popup (single entity selected); a dedicated handler can enable it later.
    { "sk_relations",      "Relations",      "relations",       "Sketch",   "Relations",       true,  nullptr,          false },
    { "sk_fix",            "Fix",            "fix",             "Sketch",   "Relations",       false, nullptr,          false },

    // =========================================================================
    // Tab: Evaluate
    // =========================================================================

    // --- Group: Measure ---
    { "eval_measure",      "Measure",        "measure",         "Evaluate", "Measure",         true,  nullptr,          false },
    { "eval_mass_props",   "Mass Props.",    "mass_props",      "Evaluate", "Measure",         true,  nullptr,          false },
    { "eval_section_props","Section Props.", "section_props",   "Evaluate", "Measure",         true,  nullptr,          false },

    // --- Group: Geometry Analysis ---
    { "eval_draft",        "Draft Anlys.",   "draft_analysis",  "Evaluate", "Geometry",        true,  nullptr,          false },
    { "eval_thickness",    "Thickness",      "thickness",       "Evaluate", "Geometry",        true,  nullptr,          false },
    { "eval_curvature",    "Curvature",      "curvature",       "Evaluate", "Geometry",        true,  nullptr,          false },
    { "eval_zebra",        "Zebra",          "zebra",           "Evaluate", "Geometry",        false, nullptr,          false },
    { "eval_symmetry",     "Symmetry",       "symmetry_check",  "Evaluate", "Geometry",        false, nullptr,          false },

    // --- Group: Check ---
    { "eval_check",        "Check",          "check",           "Evaluate", "Geometry Check",  true,  nullptr,          false },
    { "eval_interference", "Interference",   "interference_eval","Evaluate","Geometry Check",  true,  nullptr,          false },
    { "eval_geometry",     "Geometry Anlys.","geometry_analysis","Evaluate","Geometry Check",  false, nullptr,          false },

    // =========================================================================
    // Tab: Simulation
    // =========================================================================

    // --- Group: Study ---
    { "sim_new_static",    "Static Study",   "study_static",    "Simulation","Study",          true,  nullptr,          false },
    { "sim_new_modal",     "Modal Study",    "study_modal",     "Simulation","Study",          true,  nullptr,          false },
    { "sim_new_flow",      "Flow Study",     "study_flow",      "Simulation","Study",          true,  nullptr,          false },
    { "sim_new_thermal",   "Thermal Study",  "study_flow",      "Simulation","Study",          true,  nullptr,          false },
    { "sim_new_fatigue",   "Fatigue Study",  "results",         "Simulation","Study",          true,  nullptr,          false },

    // --- Group: Materials ---
    { "sim_material",      "Material",       "material",        "Simulation","Materials",      true,  nullptr,          false },

    // --- Group: Fixtures ---
    { "sim_fixture",       "Fixture",        "fixture",         "Simulation","Fixtures",       true,  nullptr,          false },

    // --- Group: Loads ---
    { "sim_load_force",    "Force",          "load_force",      "Simulation","Loads",          true,  nullptr,          false },
    { "sim_load_pressure", "Pressure",       "load_pressure",   "Simulation","Loads",          true,  nullptr,          false },

    // --- Group: Mesh ---
    { "sim_mesh",          "Mesh",           "mesh",            "Simulation","Mesh",           true,  nullptr,          false },

    // --- Group: Run ---
    { "sim_run",           "Run",            "run_study",       "Simulation","Run",            true,  nullptr,          false },
    { "sim_results",       "Results",        "results",         "Simulation","Run",            true,  nullptr,          false },

    // =========================================================================
    // Tab: Assembly (C20b)
    // =========================================================================

    // --- Group: Components ---
    { "asm_insert",        "Insert Comp.",   "insert_component", "Assembly", "Components",      true,  nullptr,          false },
    { "asm_parts_library", "Std. Parts",     "insert_component", "Assembly", "Components",      true,  nullptr,          false },
    { "asm_move",          "Move Comp.",     "move_component",   "Assembly", "Components",      true,  nullptr,          false },
    { "asm_pattern",       "Comp. Pattern",  "pattern_component","Assembly", "Components",      true,  nullptr,          false },

    // --- Group: Mate ---
    { "asm_mate",          "Mate",           "mate",             "Assembly", "Mate",            true,  nullptr,          false },

    // --- Group: Evaluate ---
    { "asm_interference",  "Interference",   "interference",     "Assembly", "Evaluate",        true,  nullptr,          false },
    { "asm_bom",           "BOM",            "bom",              "Assembly", "Evaluate",        true,  nullptr,          false },

    // --- Group: Display ---
    { "asm_explode",       "Exploded View",  "exploded_view",    "Assembly", "Display",         true,  nullptr,          false },

    // =========================================================================
    // C25: Sketch flyout variants (SolidWorks parity — append-only)
    // stretch=false  = wired in engine;  stretch=true = Coming Soon (greyed)
    // =========================================================================

    // --- Rectangle flyout (parent: sk_rect_corner) ---
    // sk_rect_center already existed as sub_of sk_rect_corner (see row above)
    // C28-B: 3pt/center rect + parallelogram implemented (map to 4 lines w/ relations) — enabled.
    { "sk_rect_3pt_corner",  "3Pt Cor. Rect",  "rect_3pt_corner",  "Sketch", "Draw", false, "sk_rect_corner", false },
    { "sk_rect_3pt_center",  "3Pt Ctr. Rect",  "rect_3pt_center",  "Sketch", "Draw", false, "sk_rect_corner", false },
    { "sk_parallelogram",    "Parallelogram",   "parallelogram",    "Sketch", "Draw", false, "sk_rect_corner", false },

    // --- Circle flyout (parent: sk_circle) ---
    // C28-B: perimeter circle (circumcircle of 3 picks) implemented — enabled.
    { "sk_circle_perimeter", "Perim. Circle",   "circle_perimeter", "Sketch", "Draw", false, "sk_circle",      false },

    // --- Slot flyout (parent: sk_slot) ---
    // C28-B: centerpoint straight slot implemented. C30-B: arc slots implemented (arc_slot entity,
    // arc-spine capsule) — enabled.
    { "sk_slot_ctr_str",     "Ctr. Str. Slot",  "slot_ctr_str",     "Sketch", "Draw", false, "sk_slot",        false },
    { "sk_slot_arc_3pt",     "3Pt Arc Slot",    "slot_arc_3pt",     "Sketch", "Draw", false, "sk_slot",        false },
    { "sk_slot_arc_ctr",     "Ctr. Arc Slot",   "slot_arc_ctr",     "Sketch", "Draw", false, "sk_slot",        false },

    // --- Spline flyout (parent: sk_spline) ---
    { "sk_spline_style",     "Style Spline",    "spline_style",     "Sketch", "Draw", false, "sk_spline",      false },
    { "sk_spline_surface",   "Spline on Surf.", "spline_surface",   "Sketch", "Draw", false, "sk_spline",      true  },
    { "sk_spline_equation",  "Eq. Driven Crv.", "spline_equation",  "Sketch", "Draw", false, "sk_spline",      false },
    // Airfoil calculator — generates a NACA/.dat section as chained splines (reuses the
    // equation-curve glyph; both are parametric-curve generators).
    { "sk_airfoil",          "Airfoil",         "spline_equation",  "Sketch", "Draw", false, "sk_spline",      false },

    // --- Ellipse flyout (new big-button parent; original sk_ellipse small-btn kept for compat) ---
    // sk_ellipse_big is the split-button parent with sub-variants. W4-K: enabled (stretch=false) so
    // its default face shows the Ellipse icon+label (instead of the greyed generic look) and a plain
    // click opens the flyout. Sub-variants stay greyed (stretch=true, no engine support yet).
    { "sk_ellipse_big",      "Ellipse",         "ellipse",          "Sketch", "Draw", false, nullptr,          false },
    // C28-B: conic family wired to the C28-A engine contract (ellipse_arc/parabola/conic entities) —
    // enabled. (Worker errors at add-time are acceptable until the engine side merges.)
    { "sk_ellipse_partial",  "Partial Ellipse", "ellipse_partial",  "Sketch", "Draw", false, "sk_ellipse_big", false },
    { "sk_parabola",         "Parabola",        "parabola",         "Sketch", "Draw", false, "sk_ellipse_big", false },
    { "sk_conic",            "Conic",           "conic",            "Sketch", "Draw", false, "sk_ellipse_big", false },

    // DRAWINGS module — "Make Drawing" entry point. Lives on the Evaluate tab (the produce-a-
    // deliverable home) in its own group. From an open part it creates a drawing sheet with the
    // standard four views and opens it as a Drawing tab. Reuses the section_props icon as a
    // sheet-ish glyph until a dedicated drawing icon ships.
    { "make_drawing",        "Make Drawing",    "section_props",    "Evaluate", "Drawing",        true,  nullptr,          false },

    // =========================================================================
    // Tab: Sheet Metal — base + edge flange drive the engine sm_* features.
    // cmd_id == engine feature type (pmFormForCommand routes straight through).
    // Icons reuse extrude_boss (base) and shell (edge); ribbon falls back gracefully.
    // =========================================================================

    // --- Group: Create ---
    { "sm_base_flange",      "Base Flange",     "extrude_boss",     "Sheet Metal", "Create",      true,  nullptr,          false },
    { "sm_edge_flange",      "Edge Flange",     "shell",            "Sheet Metal", "Create",      true,  nullptr,          false },
    // Hem (fold an edge back on itself) + Jog (two bends -> a parallel offset step). cmd_id ==
    // engine feature type (pmFormForCommand routes straight through). Icons reuse shell/extrude_boss.
    { "sm_hem",              "Hem",             "shell",            "Sheet Metal", "Create",      true,  nullptr,          false },
    { "sm_jog",              "Jog",             "extrude_boss",     "Sheet Metal", "Create",      true,  nullptr,          false },
    // W16: one-click Flatten — adds the param-less sm_flat_pattern feature (handled in app.cpp).
    { "sm_flat_pattern",     "Flatten",         "shell",            "Sheet Metal", "Flat Pattern",true,  nullptr,          false },
    // W14: export the flat pattern (outline on layer OUTLINE + bend lines on layer BEND) to DXF.
    // One-click command (no PM form) — handled in app.cpp like Flatten, via export_flat_dxf.
    { "sm_export_flat_dxf",  "Export Flat DXF", "section_props",    "Sheet Metal", "Flat Pattern",true,  nullptr,          false },

    // =========================================================================
    // Tab: Weldments — sweeps a library profile along a path sketch's segments.
    // cmd_id == engine feature type weld_member (pmFormForCommand routes straight through).
    // Reuses the sweep icon (a structural member is a profile swept along a path).
    // =========================================================================

    // --- Group: Structural ---
    { "weld_member",         "Structural Member","sweep",           "Weldments",   "Structural",  true,  nullptr,          false },

    // --- Group: Dress --- gusset (corner plate) + end cap (closes a tube end). cmd_id == engine
    // feature type (pmFormForCommand routes straight through). Reuse shell/fillet icons.
    { "weld_gusset",         "Gusset",          "shell",            "Weldments",   "Dress",       true,  nullptr,          false },
    { "weld_end_cap",        "End Cap",         "fillet",           "Weldments",   "Dress",       true,  nullptr,          false },

    // =========================================================================
    // Tab: PCB  (Wave N — AI-ECAD electrical design: schematic capture, PCB layout, sim)
    // =========================================================================

    // --- Group: New ---
    { "ecad_ai_design",     "AI Design",      "ai_panel",        "PCB", "New",        true,  nullptr, false },
    { "ecad_new_sch",       "Schematic",      "sketch",          "PCB", "New",        true,  nullptr, false },
    { "ecad_new_pcb",       "PCB Layout",     "extrude_boss",    "PCB", "New",        true,  nullptr, false },
    { "ecad_open",          "Open",           "ref_plane",       "PCB", "New",        false, nullptr, false },
    { "ecad_save",          "Save",           "ok_check",        "PCB", "New",        false, nullptr, false },

    // --- Group: Schematic ---
    { "sch_add_symbol",     "Place Symbol",   "point_ent",       "PCB", "Schematic",  true,  nullptr, false },
    { "sch_add_wire",       "Wire",           "line",            "PCB", "Schematic",  false, nullptr, false },
    { "sch_add_label",      "Net Label",      "text_ent",        "PCB", "Schematic",  false, nullptr, false },
    { "sch_add_power",      "Power Port",     "construction",    "PCB", "Schematic",  false, nullptr, false },
    { "sch_annotate",       "Annotate",       "ok_check",        "PCB", "Schematic",  false, nullptr, false },
    { "ecad_run_erc",       "Run ERC",        "interference",    "PCB", "Schematic",  false, nullptr, false },

    // --- Group: Board ---
    { "pcb_update_from_sch","Update Board",   "ok_check",        "PCB", "Board",      true,  nullptr, false },
    { "pcb_add_footprint",  "Footprint",      "point_ent",       "PCB", "Board",      false, nullptr, false },
    { "pcb_add_track",      "Route",          "line",            "PCB", "Board",      false, nullptr, false },
    { "pcb_add_via",        "Via",            "circle",          "PCB", "Board",      false, nullptr, false },
    { "pcb_add_zone",       "Copper Zone",    "rect_center",     "PCB", "Board",      false, nullptr, false },
    { "ecad_autoroute",     "Autoroute",      "sweep",           "PCB", "Board",      true,  nullptr, false },
    { "ecad_fill_zones",    "Fill Zones",     "shell",           "PCB", "Board",      false, nullptr, false },
    { "ecad_run_drc",       "Run DRC",        "interference",    "PCB", "Board",      false, nullptr, false },

    // --- Group: Analyze (native simulator) ---
    { "ecad_simulate",      "Simulate",       "study_flow",      "PCB", "Analyze",    true,  nullptr, false },

    // --- Group: Outputs ---
    { "ecad_to_3d",         "Board to 3D",    "extrude_boss",    "PCB", "Outputs",    true,  nullptr, false },
    { "ecad_export_gerber", "Gerbers",        "ref_plane",       "PCB", "Outputs",    false, nullptr, false },
    { "ecad_export_drill",  "Drill",          "hole_wizard",     "PCB", "Outputs",    false, nullptr, false },
    { "ecad_export_bom",    "BOM",            "bom",             "PCB", "Outputs",    false, nullptr, false },

    // =========================================================================
    // Tab: Surfaces — open-surface (kind="surface") body features. cmd_id == the
    // engine feature type, so pmFormForCommand routes each one straight through.
    // Icons reuse existing atlas glyphs (no new icon names — gui_lint --rule icons
    // must stay clean): extrude_boss / revolve_boss / sweep / loft / plane / offset_ent
    // / boundary / combine / trim / convert_ent / subtract / wrap / shell.
    // =========================================================================

    // --- Group: Create --- surfaces grown from sketch profiles / sections.
    { "surf_extrude",       "Extruded Surf.", "extrude_boss",    "Surfaces", "Create", true,  nullptr, false },
    { "surf_revolve",       "Revolved Surf.", "revolve_boss",    "Surfaces", "Create", true,  nullptr, false },
    { "surf_sweep",         "Swept Surf.",    "sweep",           "Surfaces", "Create", true,  nullptr, false },
    { "surf_loft",          "Lofted Surf.",   "loft",            "Surfaces", "Create", true,  nullptr, false },
    { "surf_planar",        "Planar Surf.",   "plane",           "Surfaces", "Create", true,  nullptr, false },

    // --- Group: Curves & Patches --- derived surfaces from faces / edges / curves.
    { "surf_offset",        "Offset Surf.",   "offset_ent",      "Surfaces", "Curves & Patches", true,  nullptr, false },
    { "surf_ruled",         "Ruled Surf.",    "shell",           "Surfaces", "Curves & Patches", true,  nullptr, false },
    { "surf_boundary",      "Boundary Surf.", "boundary",        "Surfaces", "Curves & Patches", true,  nullptr, false },
    { "surf_fill",          "Filled Surf.",   "boundary",        "Surfaces", "Curves & Patches", false, nullptr, false },

    // --- Group: Edit --- knit/thicken/trim and face-level surface edits.
    { "surf_knit",          "Knit Surf.",     "combine",         "Surfaces", "Edit",   true,  nullptr, false },
    { "surf_thicken",       "Thicken",        "extrude_boss",    "Surfaces", "Edit",   true,  nullptr, false },
    { "surf_trim",          "Trim Surf.",     "trim",            "Surfaces", "Edit",   true,  nullptr, false },
    { "surf_untrim",        "Untrim Surf.",   "convert_ent",     "Surfaces", "Edit",   false, nullptr, false },
    { "surf_delete_face",   "Delete Face",    "subtract",        "Surfaces", "Edit",   false, nullptr, false },
    { "surf_replace_face",  "Replace Face",   "wrap",            "Surfaces", "Edit",   false, nullptr, false },

    // =========================================================================
    // W17 DRAWING OVERHAUL — two contextual tabs shown ONLY in a Drawing doc
    // (ribbon_tabVisible swaps to these when activeTabIsDrawing()). cmd_ids are
    // dispatched in app.cpp -> ui::drawingHandleCommand(). Icons reuse existing
    // atlas glyphs (gui_lint --rule icons must stay clean).
    // =========================================================================

    // --- Tab: View Layout ---------------------------------------------------
    { "dwg_view_model",   "Model View",    "part",          "View Layout", "Views", true,  nullptr, false },
    { "dwg_view_proj",    "Projected",     "orientation",   "View Layout", "Views", true,  nullptr, false },
    { "dwg_view_std3",    "Standard 3",    "view_cube",     "View Layout", "Views", true,  nullptr, false },
    { "dwg_view_section", "Section",       "section_view",  "View Layout", "Views", true,  nullptr, false },
    { "dwg_view_detail",  "Detail",        "zoom_area",     "View Layout", "Views", true,  nullptr, false },
    // --- Group: Sheet --- sheet-level + output actions (were inline buttons).
    { "dwg_sheet_props",  "Sheet Props",   "settings",      "View Layout", "Sheet", true,  nullptr, false },
    { "dwg_rebuild",      "Rebuild",       "refresh",       "View Layout", "Sheet", false, nullptr, false },
    { "dwg_fit",          "Fit",           "zoom_fit",      "View Layout", "Sheet", false, nullptr, false },
    { "dwg_export_dxf",   "Export DXF",    "section_props", "View Layout", "Sheet", false, nullptr, false },
    { "dwg_export_pdf",   "Export PDF",    "save",          "View Layout", "Sheet", false, nullptr, false },
    // --- Group: Section --- custom-section controls applied by the Section tool.
    { "dwg_section_half",   "Half Section",  "section_view",  "View Layout", "Section", false, nullptr, false },
    { "dwg_section_reverse","Flip Side",     "orientation",   "View Layout", "Section", false, nullptr, false },
    // --- Group: Scale --- rescale the selected view.
    { "dwg_scale_1_1",    "1:1",           "zoom_fit",      "View Layout", "Scale", false, nullptr, false },
    { "dwg_scale_1_2",    "1:2",           "zoom_area",     "View Layout", "Scale", false, nullptr, false },
    { "dwg_scale_2_1",    "2:1",           "zoom_area",     "View Layout", "Scale", false, nullptr, false },

    // --- Tab: Annotation ----------------------------------------------------
    { "dwg_smart_dim",    "Smart Dim",     "smart_dim",     "Annotation", "Dimensions", true,  nullptr, false },
    { "dwg_model_items",  "Model Items",   "relations",     "Annotation", "Dimensions", true,  nullptr, false },
    { "dwg_hole_callout", "Hole Callout",  "hole_wizard",   "Annotation", "Dimensions", true,  nullptr, false },
    // --- Group: GD&T --- ASME Y14.5 geometric tolerancing.
    { "dwg_gdt",          "Geo Tol",       "smart_dim",     "Annotation", "GD&T", true,  nullptr, false },
    { "dwg_datum",        "Datum",         "point",         "Annotation", "GD&T", false, nullptr, false },
    { "dwg_datum_target", "Datum Tgt",     "point",         "Annotation", "GD&T", false, nullptr, false },
    { "dwg_surface_finish","Surf Finish",  "construction",  "Annotation", "GD&T", false, nullptr, false },
    { "dwg_weld",         "Weld",          "relations",     "Annotation", "GD&T", false, nullptr, false },
    // --- Group: Annotations --- marks, lines, notes, balloons.
    { "dwg_center_mark",  "Center Mark",   "point",         "Annotation", "Annotations", true,  nullptr, false },
    { "dwg_center_line",  "Center Line",   "construction",  "Annotation", "Annotations", true,  nullptr, false },
    { "dwg_note",         "Note",          "text_ent",      "Annotation", "Annotations", true,  nullptr, false },
    { "dwg_balloon",      "Balloon",       "circle",        "Annotation", "Annotations", false, nullptr, false },
    { "dwg_auto_balloon", "Auto Balloon",  "circular_pattern", "Annotation", "Annotations", false, nullptr, false },
    // --- Group: Tables --- title block + BOM.
    { "dwg_title_block",  "Title Block",   "new_doc",       "Annotation", "Tables", true,  nullptr, false },
    { "dwg_bom_table",    "Bill of Mat.",  "bom",           "Annotation", "Tables", true,  nullptr, false },

    // --- Tab: Evaluate, Group: Performance --- sampling profiler for modeling perf.
    // Toggles the in-process stack sampler in libaicad (prof_start/prof_stop). See
    // docs/PROFILER.md. Click to start; model; click again to stop + save a report.
    { "prof_toggle",      "Profiler",      "results",       "Evaluate", "Performance",   true,  nullptr, false },

    // =========================================================================
    // Tab: Appearance — per-body color, RealView material + display style.
    // Material presets apply a set_appearance op to the model (body "b1"); "Appearance"
    // opens the full color/material picker; Display sets st.displayStyle. All handled in
    // app.cpp's dispatcher. Icons reuse existing atlas glyphs (material / refresh /
    // display_*) so gui_lint --rule icons stays clean.
    // =========================================================================

    // --- Group: Material ---
    { "appr_color",          "Appearance",      "material",         "Appearance", "Material", true,  nullptr, false },
    { "appr_mat_plastic",    "Plastic",         "material",         "Appearance", "Material", true,  nullptr, false },
    { "appr_mat_aluminum",   "Aluminum",        "material",         "Appearance", "Material", true,  nullptr, false },
    { "appr_mat_steel",      "Steel",           "material",         "Appearance", "Material", true,  nullptr, false },
    { "appr_mat_brass",      "Brass",           "material",         "Appearance", "Material", true,  nullptr, false },
    { "appr_mat_dark",       "Dark Metal",      "material",         "Appearance", "Material", true,  nullptr, false },
    { "appr_clear",          "Clear",           "refresh",          "Appearance", "Material", false, nullptr, false },

    // --- Group: Display --- (sets st.displayStyle; the viewport applies it each frame) ---
    { "display_shaded",       "Shaded",          "display_shaded",       "Appearance", "Display", true,  nullptr, false },
    { "display_shaded_edges", "Shaded Edges",    "display_shaded_edges", "Appearance", "Display", true,  nullptr, false },
    { "display_hlr",          "Hidden Removed",  "display_hlr",          "Appearance", "Display", true,  nullptr, false },
    { "display_hlv",          "Hidden Visible",  "display_hlv",          "Appearance", "Display", false, nullptr, false },
    { "display_wireframe",    "Wireframe",       "display_wireframe",    "Appearance", "Display", true,  nullptr, false },

};
// clang-format on

const RibbonCmd* getRibbonCmds(int& count) {
    count = (int)(sizeof(kCmds) / sizeof(kCmds[0]));
    return kCmds;
}

}  // namespace ui
