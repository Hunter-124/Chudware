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
    { "sk_arc_tangent",    "Tang. Arc",      "arc_tangent",     "Sketch",   "Draw",            false, "sk_arc_3pt",     false },
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
    { "sk_convert_ent",    "Convert",        "convert_ent",     "Sketch",   "Sketch Tools",    true,  nullptr,          false },
    { "sk_construction",   "Construction",   "construction",    "Sketch",   "Sketch Tools",    false, nullptr,          false },
    { "sk_mirror",         "Mirror",         "mirror_ent",      "Sketch",   "Sketch Tools",    false, nullptr,          false },
    { "sk_lin_pattern",    "Lin. Pattern",   "pattern_lin_ent", "Sketch",   "Sketch Tools",    false, nullptr,          false },
    { "sk_circ_pattern",   "Circ. Pattern",  "pattern_circ_ent","Sketch",   "Sketch Tools",    false, nullptr,          false },
    { "sk_move",           "Move",           "move_ent",        "Sketch",   "Sketch Tools",    false, nullptr,          false },

    // --- Group: Relations ---
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

};
// clang-format on

const RibbonCmd* getRibbonCmds(int& count) {
    count = (int)(sizeof(kCmds) / sizeof(kCmds[0]));
    return kCmds;
}

}  // namespace ui
