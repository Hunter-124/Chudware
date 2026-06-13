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

    // --- Group: Extrude (boss + cut share a split button) ---
    { "extrude_boss",      "Extrude",        "extrude_boss",    "Features", "Extrude",         true,  nullptr,          false },
    { "extrude_cut",       "Extrude Cut",    "extrude_cut",     "Features", "Extrude",         false, "extrude_boss",   false },

    // --- Group: Revolve ---
    { "revolve_boss",      "Revolve",        "revolve_boss",    "Features", "Revolve",         true,  nullptr,          false },
    { "revolve_cut",       "Revolve Cut",    "revolve_cut",     "Features", "Revolve",         false, "revolve_boss",   false },

    // --- Group: Sweep ---
    { "sweep_boss",        "Sweep",          "sweep",           "Features", "Sweep",           true,  nullptr,          false },
    { "sweep_cut",         "Sweep Cut",      "sweep_cut",       "Features", "Sweep",           false, "sweep_boss",     false },

    // --- Group: Loft ---
    { "loft_boss",         "Loft",           "loft",            "Features", "Loft",            true,  nullptr,          false },
    { "loft_cut",          "Loft Cut",       "loft_cut",        "Features", "Loft",            false, "loft_boss",      false },

    // --- Group: Dress-up ---
    { "fillet",            "Fillet",         "fillet",          "Features", "Dress-up",        true,  nullptr,          false },
    { "chamfer",           "Chamfer",        "chamfer",         "Features", "Dress-up",        true,  nullptr,          false },
    { "shell",             "Shell",          "shell",           "Features", "Dress-up",        true,  nullptr,          false },
    { "draft",             "Draft",          "draft",           "Features", "Dress-up",        false, nullptr,          false },
    { "rib",               "Rib",            "rib",             "Features", "Dress-up",        false, nullptr,          true  },

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
    { "split",             "Split",          "split",           "Features", "Curves",          false, nullptr,          true  },

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
    // C42 honesty: tangent arc has no SketchMode handler yet — greyed (Coming soon).
    { "sk_arc_tangent",    "Tang. Arc",      "arc_tangent",     "Sketch",   "Draw",            false, "sk_arc_3pt",     true  },
    { "sk_polygon",        "Polygon",        "polygon",         "Sketch",   "Draw",            true,  nullptr,          false },
    { "sk_slot",           "Slot",           "slot",            "Sketch",   "Draw",            true,  nullptr,          false },
    { "sk_spline",         "Spline",         "spline",          "Sketch",   "Draw",            true,  nullptr,          false },
    { "sk_ellipse",        "Ellipse",        "ellipse",         "Sketch",   "Draw",            false, nullptr,          false },
    { "sk_point",          "Point",          "point_ent",       "Sketch",   "Draw",            false, nullptr,          false },
    { "sk_text",           "Text",           "text_ent",        "Sketch",   "Draw",            false, nullptr,          true  },

    // --- Group: Smart Dimension ---
    { "smart_dim",         "Smart Dim.",     "smart_dim",       "Sketch",   "Dimension",       true,  nullptr,          false },

    // --- Group: Sketch Tools ---
    { "sk_trim",           "Trim",           "trim",            "Sketch",   "Sketch Tools",    true,  nullptr,          false },
    { "sk_extend",         "Extend",         "extend",          "Sketch",   "Sketch Tools",    true,  nullptr,          false },
    { "sk_offset_ent",     "Offset",         "offset_ent",      "Sketch",   "Sketch Tools",    true,  nullptr,          false },
    // C42 honesty: convert/mirror/patterns/move have no SketchMode handler (engine ops still
    // schema-reserved _op_unsupported_m2) — greyed (Coming soon) instead of eating clicks.
    { "sk_convert_ent",    "Convert",        "convert_ent",     "Sketch",   "Sketch Tools",    true,  nullptr,          true  },
    { "sk_construction",   "Construction",   "construction",    "Sketch",   "Sketch Tools",    false, nullptr,          false },
    { "sk_mirror",         "Mirror",         "mirror_ent",      "Sketch",   "Sketch Tools",    false, nullptr,          true  },
    { "sk_lin_pattern",    "Lin. Pattern",   "pattern_lin_ent", "Sketch",   "Sketch Tools",    false, nullptr,          true  },
    { "sk_circ_pattern",   "Circ. Pattern",  "pattern_circ_ent","Sketch",   "Sketch Tools",    false, nullptr,          true  },
    { "sk_move",           "Move",           "move_ent",        "Sketch",   "Sketch Tools",    false, nullptr,          true  },

    // --- Group: Relations ---
    // C42: sk_relations wired (Add Relation popup over the selection). sk_fix greyed — Fix is
    // reachable via that popup (single entity selected); a dedicated handler can enable it later.
    { "sk_relations",      "Relations",      "relations",       "Sketch",   "Relations",       true,  nullptr,          false },
    { "sk_fix",            "Fix",            "fix",             "Sketch",   "Relations",       false, nullptr,          true  },

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
    { "eval_zebra",        "Zebra",          "zebra",           "Evaluate", "Geometry",        false, nullptr,          true  },
    { "eval_symmetry",     "Symmetry",       "symmetry_check",  "Evaluate", "Geometry",        false, nullptr,          false },

    // --- Group: Check ---
    { "eval_check",        "Check",          "check",           "Evaluate", "Geometry Check",  true,  nullptr,          false },
    { "eval_interference", "Interference",   "interference_eval","Evaluate","Geometry Check",  true,  nullptr,          false },
    { "eval_geometry",     "Geometry Anlys.","geometry_analysis","Evaluate","Geometry Check",  false, nullptr,          true  },

    // =========================================================================
    // Tab: Simulation
    // =========================================================================

    // --- Group: Study ---
    { "sim_new_static",    "Static Study",   "study_static",    "Simulation","Study",          true,  nullptr,          false },
    { "sim_new_modal",     "Modal Study",    "study_modal",     "Simulation","Study",          true,  nullptr,          false },
    { "sim_new_flow",      "Flow Study",     "study_flow",      "Simulation","Study",          true,  nullptr,          false },

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
    { "asm_move",          "Move Comp.",     "move_component",   "Assembly", "Components",      true,  nullptr,          false },
    { "asm_pattern",       "Comp. Pattern",  "pattern_component","Assembly", "Components",      true,  nullptr,          false },

    // --- Group: Mate ---
    { "asm_mate",          "Mate",           "mate",             "Assembly", "Mate",            true,  nullptr,          false },

    // --- Group: Evaluate ---
    { "asm_interference",  "Interference",   "interference",     "Assembly", "Evaluate",        true,  nullptr,          false },
    { "asm_bom",           "Bill of Mat.",   "bom",              "Assembly", "Evaluate",        true,  nullptr,          false },

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
    { "sk_spline_style",     "Style Spline",    "spline_style",     "Sketch", "Draw", false, "sk_spline",      true  },
    { "sk_spline_surface",   "Spline on Surf.", "spline_surface",   "Sketch", "Draw", false, "sk_spline",      true  },
    { "sk_spline_equation",  "Eq. Driven Crv.", "spline_equation",  "Sketch", "Draw", false, "sk_spline",      true  },

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

    // =========================================================================
    // Tab: PCB  (Wave N — AI-ECAD electrical design: schematic capture, PCB layout, sim)
    // =========================================================================

    // --- Group: New ---
    { "ecad_new_sch",       "Schematic",      "sketch",          "PCB", "New",        true,  nullptr, false },
    { "ecad_new_pcb",       "PCB Layout",     "extrude_boss",    "PCB", "New",        true,  nullptr, false },
    { "ecad_open",          "Open",           "ref_plane",       "PCB", "New",        false, nullptr, false },

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

    // --- Group: Outputs ---
    { "ecad_to_3d",         "Board to 3D",    "extrude_boss",    "PCB", "Outputs",    true,  nullptr, false },
    { "ecad_export_gerber", "Gerbers",        "ref_plane",       "PCB", "Outputs",    false, nullptr, false },
    { "ecad_export_drill",  "Drill",          "hole_wizard",     "PCB", "Outputs",    false, nullptr, false },
    { "ecad_export_bom",    "BOM",            "bom",             "PCB", "Outputs",    false, nullptr, false },

};
// clang-format on

const RibbonCmd* getRibbonCmds(int& count) {
    count = (int)(sizeof(kCmds) / sizeof(kCmds[0]));
    return kCmds;
}

}  // namespace ui
