// Chudware ribbon command table — C14.
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
    // Icon column audited against FM_REFRESH_MANIFEST in make_icons.py (68 glyphs).
    // Every icon string below must exist in host/assets/icons/atlas.json.
    // =========================================================================

    // --- Group: Sketch (entry point for the features workflow) ---
    { "sketch_new",        "New Sketch",     "sketch",          "Features", "Sketch",          true,  nullptr,          false },

    // --- Group: 3D Body ---
    { "extrude_boss",      "Extrude",        "extrude_boss",    "Features", "3D Body",         true,  nullptr,          false },
    // Extrude Cut folded UNDER extrude_boss as a split-button variant (parity with revolve_cut).
    // Default face = Extrude (Boss); the flyout injects the boss row so users can switch back.
    { "extrude_cut",       "Extrude Cut",    "extrude_cut",     "Features", "3D Body",         false, "extrude_boss",   false },
    { "revolve_boss",      "Revolve",        "revolve_boss",    "Features", "3D Body",         true,  nullptr,          false },
    { "revolve_cut",       "Revolve Cut",    "revolve_cut",     "Features", "3D Body",         false, "revolve_boss",   false },
    { "sweep_boss",        "Sweep",          "sweep",           "Features", "3D Body",         true,  nullptr,          false },
    { "sweep_cut",         "Sweep Cut",      "sweep_cut",       "Features", "3D Body",         false, "sweep_boss",     false },
    { "loft_boss",         "Loft",           "loft",            "Features", "3D Body",         true,  nullptr,          false },
    { "loft_cut",          "Loft Cut",       "loft_cut",        "Features", "3D Body",         false, "loft_boss",      false },

    // --- Group: Dress-up ---
    // Fillet is the split-button parent; Chamfer folded under it (default face = Fillet).
    { "fillet",            "Fillet",         "fillet",          "Features", "Dress-up",        true,  nullptr,          false },
    { "chamfer",           "Chamfer",        "chamfer",         "Features", "Dress-up",        false, "fillet",         false },
    { "shell",             "Shell",          "shell",           "Features", "Dress-up",        true,  nullptr,          false },
    // Draft is the split-button parent; Rib folded under it (default face = Draft).
    { "draft",             "Draft",          "draft",           "Features", "Dress-up",        true,  nullptr,          false },
    { "rib",               "Rib",            "rib",             "Features", "Dress-up",        false, "draft",          false },

    // --- Group: Hole ---
    { "hole_wizard",       "Hole Wizard",    "hole_wizard",     "Features", "Hole",            true,  nullptr,          false },
    // Thread Wizard — external/internal ISO/Unified threads on a picked cylindrical face (opens the
    // "thread" PM form). Icon "thread" is added by the icon agent; falls back to a text glyph meanwhile.
    { "thread",            "Thread",         "thread",          "Features", "Hole",            true,  nullptr,          false },

    // --- Group: Pattern/Mirror ---
    { "linear_pattern",   "Lin. Pattern",   "linear_pattern",  "Features", "Pattern",         true,  nullptr,          false },
    { "circular_pattern", "Circ. Pattern",  "circular_pattern","Features", "Pattern",         true,  nullptr,          false },
    { "mirror_feature",   "Mirror",         "mirror",          "Features", "Pattern",         true,  nullptr,          false },

    // --- Group: Reference Geometry ---
    // Helix lives LEFT of Ref. Plane (ITEM 10a ribbon layout reorg).
    { "curve_helix",       "Helix",          "curve_helix",     "Features", "Reference",       true,  nullptr,          false },
    { "ref_plane",         "Ref. Plane",     "ref_plane",       "Features", "Reference",       true,  nullptr,          false },
    { "ref_axis",          "Ref. Axis",      "ref_axis",        "Features", "Reference",       false, nullptr,          false },
    { "ref_point",         "Ref. Point",     "ref_point",       "Features", "Reference",       false, nullptr,          false },
    { "ref_csys",          "Coord. Sys.",    "ref_csys",        "Features", "Reference",       false, nullptr,          false },

    // --- Group: Multi-body ---
    // Split moved here from Curves (ITEM 10a).
    { "boolean_combine",   "Combine",        "combine",         "Features", "Multi-body",      true,  nullptr,          false },
    { "boolean_subtract",  "Subtract",       "subtract",        "Features", "Multi-body",      false, nullptr,          false },
    { "boolean_intersect", "Intersect",      "intersect",       "Features", "Multi-body",      false, nullptr,          false },
    { "split",             "Split",          "split",           "Features", "Multi-body",      false, nullptr,          false },

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
    // Centerline: "for construction only" line with an exaggerated midpoint marker. No distinct
    // engine centerline entity exists, so this is a construction-line variant (see sketch_mode.cpp
    // SketchTool::Centerline). Positioned right after Point per the ribbon spec.
    { "sk_centerline",     "Centerline",     "construction",    "Sketch",   "Draw",            false, nullptr,          false },
    // Gizmo-driven bulk transforms: in-plane manipulator + relation snapping + viewport HUD.
    // Placed in Draw right under Point/Centerline per the ribbon spec (moved out of Sketch Tools).
    { "sk_move_entities",  "Move Ent.",      "move_ent",        "Sketch",   "Draw",            false, nullptr,          false },
    { "sk_rotate_entities","Rotate Ent.",    "rotate_ent",      "Sketch",   "Draw",            false, nullptr,          false },
    // Ellipse + Text promoted to tall/big tool buttons to match airfoil/spline/slot/polygon.
    { "sk_text",           "Text",           "text_ent",        "Sketch",   "Draw",            true,  nullptr,          false },

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
    // Pseudo-tab: "Sketch3D" — the 3D free-space sketcher's ribbon tool set. NOT one of
    // kRibbonTabs (never shown by normal tab iteration); ribbon.cpp swaps the Sketch tab's
    // command source to these rows while a sketch3d feature is being edited (Hunter red-pen
    // item 1 — 3D tools belong in the ribbon, not the sketch3d_mode.cpp floating overlay).
    // =========================================================================

    { "sketch3d_exit",      "Exit Sketch",   "ok_check",        "Sketch3D", "Sketch",          true,  nullptr,          false },

    { "sk3d_line",           "Line",          "line",           "Sketch3D", "Draw",            true,  nullptr,          false },
    { "sk3d_arc",            "3-Pt Arc",      "arc_3pt",        "Sketch3D", "Draw",            true,  nullptr,          false },
    { "sk3d_spline",         "Spline",        "spline",         "Sketch3D", "Draw",            true,  nullptr,          false },
    { "sk3d_point",          "Point",         "point_ent",      "Sketch3D", "Draw",            true,  nullptr,          false },

    { "sk3d_plane_xy",       "XY Plane",      "ref_plane",       "Sketch3D", "Working Plane",  true,  nullptr,          false },
    { "sk3d_plane_xz",       "XZ Plane",      "ref_plane",       "Sketch3D", "Working Plane",  false, nullptr,          false },
    { "sk3d_plane_yz",       "YZ Plane",      "ref_plane",       "Sketch3D", "Working Plane",  false, nullptr,          false },
    { "sk3d_plane_view",     "View Plane",    "ref_plane",       "Sketch3D", "Working Plane",  false, nullptr,          false },

    { "sk3d_construction",   "Construction",  "construction",    "Sketch3D", "Sketch Tools",   true,  nullptr,          false },
    { "sk3d_relations",      "Relations",     "relations",       "Sketch3D", "Sketch Tools",    true,  nullptr,          false },
    { "sk3d_fix",            "Fix",           "fix",             "Sketch3D", "Sketch Tools",    false, nullptr,          false },
    { "sk3d_delete",         "Delete",        "trim",            "Sketch3D", "Sketch Tools",    false, nullptr,          false },

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
    // Group: Assembly — former standalone Assembly tab tools, merged onto the RIGHT
    // of the Features ribbon (ITEM 10a). Same cmd_ids / icons / big flags; only the
    // tab/group placement changed. The standalone "Assembly" tab is removed from the
    // tab strip in ribbon.cpp (cross-file patch request).
    // =========================================================================

    // --- Group: Assembly (on Features tab) ---
    { "asm_insert",        "Insert Comp.",   "insert_component", "Features", "Assembly",        true,  nullptr,          false },
    { "asm_parts_library", "Std. Parts",     "insert_component", "Features", "Assembly",        true,  nullptr,          false },
    { "asm_move",          "Move Comp.",     "move_component",   "Features", "Assembly",        true,  nullptr,          false },
    { "asm_pattern",       "Comp. Pattern",  "pattern_component","Features", "Assembly",        true,  nullptr,          false },
    { "asm_mate",          "Mate",           "mate",             "Features", "Assembly",        true,  nullptr,          false },
    { "asm_interference",  "Interference",   "interference",     "Features", "Assembly",        true,  nullptr,          false },
    { "asm_bom",           "BOM",            "bom",              "Features", "Assembly",        true,  nullptr,          false },
    { "asm_explode",       "Exploded View",  "exploded_view",    "Features", "Assembly",        true,  nullptr,          false },

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
    // Airfoil calculator — promoted to its own top-level Draw button (big=true, unique "airfoil"
    // icon; removed from the spline flyout so users can reach it without opening a dropdown).
    { "sk_airfoil",          "Airfoil",         "airfoil",          "Sketch", "Draw", true,  nullptr,          false },

    // --- Ellipse flyout (new big-button parent; original sk_ellipse small-btn kept for compat) ---
    // sk_ellipse_big is the split-button parent with sub-variants. W4-K: enabled (stretch=false) so
    // its default face shows the Ellipse icon+label (instead of the greyed generic look) and a plain
    // click opens the flyout. Sub-variants stay greyed (stretch=true, no engine support yet).
    { "sk_ellipse_big",      "Ellipse",         "ellipse",          "Sketch", "Draw", true,  nullptr,          false },
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
    // Icons: dedicated sm_* atlas glyphs (sm_base_flange / sm_edge_flange / sm_hem / sm_jog / sm_flat_pattern).
    // =========================================================================

    // --- Group: Create ---
    { "sm_base_flange",      "Base Flange",     "sm_base_flange",    "Sheet Metal", "Create",      true,  nullptr,          false },
    { "sm_edge_flange",      "Edge Flange",     "sm_edge_flange",    "Sheet Metal", "Create",      true,  nullptr,          false },
    // Hem (fold an edge back on itself) + Jog (two bends -> a parallel offset step). cmd_id ==
    // engine feature type (pmFormForCommand routes straight through). Dedicated sm_hem / sm_jog glyphs.
    { "sm_hem",              "Hem",             "sm_hem",           "Sheet Metal", "Create",      true,  nullptr,          false },
    { "sm_jog",              "Jog",             "sm_jog",           "Sheet Metal", "Create",      true,  nullptr,          false },
    // W16: one-click Flatten — adds the param-less sm_flat_pattern feature (handled in app.cpp).
    { "sm_flat_pattern",     "Flatten",         "sm_flat_pattern",   "Sheet Metal", "Flat Pattern",true,  nullptr,          false },
    // W14: export the flat pattern (outline on layer OUTLINE + bend lines on layer BEND) to DXF.
    // One-click command (no PM form) — handled in app.cpp like Flatten, via export_flat_dxf.
    { "sm_export_flat_dxf",  "Export Flat DXF", "sm_flat_pattern",  "Sheet Metal", "Flat Pattern",true,  nullptr,          false },

    // =========================================================================
    // Tab: Weldments — sweeps a library profile along a path sketch's segments.
    // cmd_id == engine feature type weld_member (pmFormForCommand routes straight through).
    // Dedicated weld_member glyph (an I-beam / tube structural section).
    // =========================================================================

    // --- Group: Structural ---
    { "weld_member",         "Structural Member","weld_member",     "Weldments",   "Structural",  true,  nullptr,          false },

    // --- Group: Dress --- gusset (corner plate) + end cap (closes a tube end). cmd_id == engine
    // feature type (pmFormForCommand routes straight through). Dedicated weld_gusset / weld_end_cap glyphs.
    { "weld_gusset",         "Gusset",          "weld_gusset",      "Weldments",   "Dress",       true,  nullptr,          false },
    { "weld_end_cap",        "End Cap",         "weld_end_cap",     "Weldments",   "Dress",       true,  nullptr,          false },

    // =========================================================================
    // Tab: PCB Design  (Wave N — AI-ECAD board layout: footprints, routing, DRC, outputs)
    // NOTE: the old single "PCB" tab was split into two dedicated tabs — "PCB Design"
    // (this one, board/layout tools) and "Schematic" (symbols/nets/ERC/analog sim, below).
    // Both share the Electronics workspace. The shared entry commands (AI Design / Open /
    // Save) live here on PCB Design to avoid duplicating a cmd_id across two tabs.
    // =========================================================================

    // --- Group: New ---
    { "ecad_ai_design",     "AI Design",      "ai_panel",        "PCB Design", "New",        true,  nullptr, false },
    { "ecad_new_pcb",       "PCB Layout",     "extrude_boss",    "PCB Design", "New",        true,  nullptr, false },
    { "ecad_open",          "Open",           "ref_plane",       "PCB Design", "New",        false, nullptr, false },
    { "ecad_save",          "Save",           "ok_check",        "PCB Design", "New",        false, nullptr, false },

    // --- Group: Board ---
    { "pcb_update_from_sch","Update Board",   "ok_check",        "PCB Design", "Board",      true,  nullptr, false },
    { "pcb_add_footprint",  "Footprint",      "point_ent",       "PCB Design", "Board",      false, nullptr, false },
    { "pcb_add_track",      "Route",          "line",            "PCB Design", "Board",      false, nullptr, false },
    { "pcb_add_via",        "Via",            "circle",          "PCB Design", "Board",      false, nullptr, false },
    { "pcb_add_zone",       "Copper Zone",    "rect_center",     "PCB Design", "Board",      false, nullptr, false },
    { "ecad_autoroute",     "Autoroute",      "sweep",           "PCB Design", "Board",      true,  nullptr, false },
    { "ecad_fill_zones",    "Fill Zones",     "shell",           "PCB Design", "Board",      false, nullptr, false },

    // --- Group: Outputs ---
    { "ecad_to_3d",         "Board to 3D",    "extrude_boss",    "PCB Design", "Outputs",    true,  nullptr, false },
    { "ecad_export_gerber", "Gerbers",        "ref_plane",       "PCB Design", "Outputs",    false, nullptr, false },
    { "ecad_export_drill",  "Drill",          "hole_wizard",     "PCB Design", "Outputs",    false, nullptr, false },
    { "ecad_export_bom",    "BOM",            "bom",             "PCB Design", "Outputs",    false, nullptr, false },

    // --- Group: Checks --- (Run DRC promoted to a big primary button to the right of Outputs,
    // sized like Board to 3D / PCB Layout; the old small Board-group entry + the manager toolbar
    // icon were removed so there is one obvious DRC affordance.)
    { "ecad_run_drc",       "Run DRC",        "interference",    "PCB Design", "Checks",     true,  nullptr, false },

    // =========================================================================
    // Tab: Schematic  (split from the old "PCB" tab — schematic capture + analog sim)
    // Symbols, nets, power, ERC, and the native analog simulator. Backend hooks to
    // port results into the Simulation tab land later; this just organizes the tools.
    // =========================================================================

    // --- Group: New ---
    { "ecad_new_sch",       "New Schematic",  "sketch",          "Schematic", "New",        true,  nullptr, false },

    // --- Group: Schematic ---
    { "sch_add_symbol",     "Place Symbol",   "point_ent",       "Schematic", "Schematic",  true,  nullptr, false },
    { "sch_add_wire",       "Wire",           "line",            "Schematic", "Schematic",  false, nullptr, false },
    { "sch_add_label",      "Net Label",      "text_ent",        "Schematic", "Schematic",  false, nullptr, false },
    { "sch_add_power",      "Power Port",     "construction",    "Schematic", "Schematic",  false, nullptr, false },
    { "sch_annotate",       "Annotate",       "ok_check",        "Schematic", "Schematic",  false, nullptr, false },
    { "ecad_run_erc",       "Run ERC",        "interference",    "Schematic", "Schematic",  false, nullptr, false },

    // --- Group: Analyze (native analog simulator) ---
    { "ecad_simulate",      "Simulate",       "study_flow",      "Schematic", "Analyze",    true,  nullptr, false },

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
    { "surf_planar",        "Planar Surf.",   "ref_plane",       "Surfaces", "Create", true,  nullptr, false },

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
    // Toggles the in-process stack sampler in libchudware (prof_start/prof_stop). See
    // docs/PROFILER.md. Click to start; model; click again to stop + save a report.
    { "prof_toggle",      "Profiler",      "results",       "Evaluate", "Performance",   true,  nullptr, false },

    // =========================================================================
    // Tab: Appearance — per-body color, RealView material + display style.
    // Material presets apply a set_appearance op to the model (body "b1"); "Appearance"
    // opens the full color/material picker; Display sets st.displayStyle. All handled in
    // app.cpp's dispatcher. Material buttons use dedicated color_picker / appr_* icons; the
    // ribbon.cpp swatch overlay draws each preset's actual RGB. Display reuses the display_* glyphs.
    // =========================================================================

    // --- Group: Material ---
    { "appr_color",          "Appearance",      "color_picker",     "Appearance", "Material", true,  nullptr, false },
    { "appr_mat_plastic",    "Plastic",         "appr_plastic",     "Appearance", "Material", true,  nullptr, false },
    { "appr_mat_aluminum",   "Aluminum",        "appr_metal",       "Appearance", "Material", true,  nullptr, false },
    { "appr_mat_steel",      "Steel",           "appr_steel",       "Appearance", "Material", true,  nullptr, false },
    { "appr_mat_brass",      "Brass",           "appr_brass",       "Appearance", "Material", true,  nullptr, false },
    { "appr_mat_dark",       "Dark Metal",      "appr_dark",        "Appearance", "Material", true,  nullptr, false },
    { "appr_clear",          "Clear",           "refresh",          "Appearance", "Material", false, nullptr, false },

    // --- Group: Display --- (sets st.displayStyle; the viewport applies it each frame) ---
    { "display_shaded",       "Shaded",          "display_shaded",       "Appearance", "Display", true,  nullptr, false },
    { "display_shaded_edges", "Shaded Edges",    "display_shaded_edges", "Appearance", "Display", true,  nullptr, false },
    { "display_hlr",          "Hidden Removed",  "display_hlr",          "Appearance", "Display", true,  nullptr, false },
    { "display_hlv",          "Hidden Visible",  "display_hlv",          "Appearance", "Display", false, nullptr, false },
    { "display_wireframe",    "Wireframe",       "display_wireframe",    "Appearance", "Display", true,  nullptr, false },

    // =========================================================================
    // Polygon flyout variants (parent: sk_polygon, big=false). Each arms the
    // Polygon tool with the appropriate polySides_ value (or Star tool for star).
    // =========================================================================
    { "sk_poly_tri",  "Triangle", "poly_triangle", "Sketch", "Draw", false, "sk_polygon", false },
    { "sk_poly_quad", "Square",   "poly_square",   "Sketch", "Draw", false, "sk_polygon", false },
    { "sk_poly_pent", "Pentagon", "poly_pentagon", "Sketch", "Draw", false, "sk_polygon", false },
    { "sk_poly_hex",  "Hexagon",  "polygon",       "Sketch", "Draw", false, "sk_polygon", false },
    { "sk_poly_oct",  "Octagon",  "poly_octagon",  "Sketch", "Draw", false, "sk_polygon", false },
    { "sk_poly_star", "Star",     "poly_star",     "Sketch", "Draw", false, "sk_polygon", false },

    // =========================================================================
    // Tab: Simulation — shared Reference Geometry group (same cmd_ids as Features tab so
    // dispatch, tooltip, and setRibbonActiveCmd all work without change). Appended here so
    // the Simulate workspace's ribbon has access to ref-plane / axis / point / coord sys
    // without leaking 3D modeling tabs into the sim context.
    // =========================================================================

    // --- Group: Reference (Simulation tab copy) ---
    { "ref_plane",         "Ref. Plane",     "ref_plane",       "Simulation", "Reference",      true,  nullptr, false },
    { "ref_axis",          "Ref. Axis",      "ref_axis",        "Simulation", "Reference",      false, nullptr, false },
    { "ref_point",         "Ref. Point",     "ref_point",       "Simulation", "Reference",      false, nullptr, false },
    { "ref_csys",          "Coord. Sys.",    "ref_csys",        "Simulation", "Reference",      false, nullptr, false },

    // =========================================================================
    // Tab: Manufacture — shared Reference Geometry group (same cmd_ids as Features tab).
    // Appended here so the Manufacture workspace has ref-plane / axis without leaking 3D tabs.
    // =========================================================================

    // --- Group: Reference (Manufacture tab copy) ---
    { "ref_plane",         "Ref. Plane",     "ref_plane",       "Manufacture", "Reference",     true,  nullptr, false },
    { "ref_axis",          "Ref. Axis",      "ref_axis",        "Manufacture", "Reference",     false, nullptr, false },
    { "ref_point",         "Ref. Point",     "ref_point",       "Manufacture", "Reference",     false, nullptr, false },
    { "ref_csys",          "Coord. Sys.",    "ref_csys",        "Manufacture", "Reference",     false, nullptr, false },

    // =========================================================================
    // Tab: Manufacture (CAM — toolpaths / post / material-removal sim)
    // =========================================================================
    // --- Group: Job ---
    { "cam_new_job",       "New Job",     "new_doc",         "Manufacture", "Job",       true,  nullptr, false },
    { "cam_extract",       "From Part",   "hole_wizard",     "Manufacture", "Job",       true,  nullptr, false },
    { "cam_add_setup",     "Setup",       "ref_csys",        "Manufacture", "Job",       false, nullptr, false },
    { "cam_set_stock",     "Stock",       "stock",           "Manufacture", "Job",       false, nullptr, false },
    { "cam_add_tool",      "Tool",        "curve_helix",     "Manufacture", "Job",       false, nullptr, false },
    // --- Group: 2.5D Milling ---
    // CAM overhaul: operation FAMILIES fold into split buttons (same pattern as Features▸3D Body,
    // where extrude_cut sits under extrude_boss). The parent stays big=true and keeps its own
    // action on a plain click; each variant keeps big=false and points sub_of at the parent, so it
    // is reached through the parent's flyout instead of eating another slot in the band. This is
    // what stops the Manufacture tab overflowing (and degrading Output) at uiScale >= 1.25.
    // No cmd_id was renamed, added or removed — cam_panel.cpp's camHandleCommand switch is intact.
    { "cam_op_face",       "Face",        "cam_face",        "Manufacture", "2.5D",      true,  nullptr, false },
    { "cam_op_contour",    "Contour",     "cam_contour2d",   "Manufacture", "2.5D",      true,  nullptr, false },
    { "cam_op_pocket",     "Pocket",      "cam_pocket2d",    "Manufacture", "2.5D",      true,  nullptr, false },
    { "cam_op_adaptive",   "Adaptive",    "cam_adaptive2d",  "Manufacture", "2.5D",      false, "cam_op_pocket",       false },
    { "cam_op_slot",       "Slot",        "cam_slot",        "Manufacture", "2.5D",      false, "cam_op_pocket",       false },
    { "cam_op_chamfer",    "Chamfer",     "cam_chamfer2d",   "Manufacture", "2.5D",      false, "cam_op_contour",      false },
    { "cam_op_engrave",    "Engrave",     "cam_engrave",     "Manufacture", "2.5D",      false, "cam_op_contour",      false },
    // --- Group: Holes ---
    { "cam_op_drill",      "Drill",       "cam_drill",       "Manufacture", "Holes",     true,  nullptr, false },
    { "cam_op_thread",     "Thread Mill", "cam_thread_mill", "Manufacture", "Holes",     false, "cam_op_drill",        false },
    // --- Group: 3D Milling ---
    { "cam_op_parallel3d", "Parallel",    "cam_parallel3d",  "Manufacture", "3D",        true,  nullptr, false },
    { "cam_op_waterline3d","Waterline",   "cam_waterline3d", "Manufacture", "3D",        false, "cam_op_parallel3d",   false },
    { "cam_op_scallop3d",  "Scallop",     "cam_scallop3d",   "Manufacture", "3D",        false, "cam_op_parallel3d",   false },
    { "cam_op_pocket3d",   "3D Pocket",   "cam_pocket3d",    "Manufacture", "3D",        false, "cam_op_parallel3d",   false },
    // --- Group: Turning ---
    { "cam_op_turn_rough", "Rough",       "cam_turn_rough",  "Manufacture", "Turning",   true,  nullptr, false },
    { "cam_op_turn_finish","Finish",      "cam_turn_finish", "Manufacture", "Turning",   false, "cam_op_turn_rough",   false },
    { "cam_op_turn_face",  "Face",        "cam_turn_face",   "Manufacture", "Turning",   false, "cam_op_turn_rough",   false },
    { "cam_op_turn_part",  "Part Off",    "cam_turn_part",   "Manufacture", "Turning",   false, "cam_op_turn_rough",   false },
    // --- Group: Cutting ---
    { "cam_op_laser",      "Laser/Plasma","cam_cut2d",       "Manufacture", "Cutting",   true,  nullptr, false },
    { "cam_op_probe",      "Probe",       "cam_probe",       "Manufacture", "Cutting",   false, "cam_op_laser",        false },
    // --- Group: Output ---
    { "cam_generate",      "Generate",    "ok_check",        "Manufacture", "Output",    true,  nullptr, false },
    { "cam_simulate",      "Simulate",    "study_static",    "Manufacture", "Output",    true,  nullptr, false },
    { "cam_post",          "Post G-code", "ai_panel",        "Manufacture", "Output",    true,  nullptr, false },

    // =========================================================================
    // cam-workspace overhaul (append-only): the Manufacture ribbon now carries the FULL CAM
    // command set (the floating CAM panel is a detail editor only). Show in 3D toggles the
    // viewport toolpath backplot; Flavor opens the dynamic post-dialect dropdown (populated
    // from cam_post_dialects by the workspace tree — a static flyout can't list them).
    // =========================================================================
    { "cam_show3d",        "Show in 3D",  "display_shaded",  "Manufacture", "Output",    false, nullptr, false },
    { "cam_flavor",        "Flavor",      "settings",        "Manufacture", "Output",    false, nullptr, false },

    // =========================================================================
    // sim-gui rework (Hunter red-pen): per-simulation-type ribbon tabs, one per study kind,
    // shown ONLY in the Simulate workspace (ribbon.cpp ribbon_tabVisible, tabs 13..19).
    // Each tab carries JUST the tools for its study type — the first button is always
    // "+ New <type> Study", then the type's setup tools, Run/Results, and the shared
    // Reference group. cmd_ids reuse the existing sim_* dispatch (app.cpp routes every
    // "sim_"-prefixed command into SimManager::handleCommand); new sim_* setup cmd_ids
    // below are handled there too. The legacy all-in-one "Simulation" tab rows above stay
    // (append-only) but that tab is retired from the Simulate workspace.
    // =========================================================================

    // --- Tab: Static (linear stress / displacement / FoS) ---
    { "sim_new_static",    "New Study",   "study_static",    "Static",  "Study",     true,  nullptr, false },
    { "sim_material",      "Material",    "material",        "Static",  "Setup",     true,  nullptr, false },
    { "sim_fixture",       "Fixture",     "fixture",         "Static",  "Setup",     true,  nullptr, false },
    { "sim_load_force",    "Force",       "load_force",      "Static",  "Loads",     true,  nullptr, false },
    { "sim_load_pressure", "Pressure",    "load_pressure",   "Static",  "Loads",     true,  nullptr, false },
    // Torque loads: schema-reserved (engine has no torque/moment load op yet) — honest greyed row.
    { "sim_load_torque",   "Torque",      "load_force",      "Static",  "Loads",     false, nullptr, true  },
    { "sim_mesh",          "Mesh",        "mesh",            "Static",  "Mesh",      true,  nullptr, false },
    { "sim_run",           "Run",         "run_study",       "Static",  "Run",       true,  nullptr, false },
    { "sim_results",       "Results",     "results",         "Static",  "Run",       true,  nullptr, false },
    { "ref_plane",         "Ref. Plane",  "ref_plane",       "Static",  "Reference", false, nullptr, false },
    { "ref_axis",          "Ref. Axis",   "ref_axis",        "Static",  "Reference", false, nullptr, false },
    { "ref_point",         "Ref. Point",  "ref_point",       "Static",  "Reference", false, nullptr, false },
    { "ref_csys",          "Coord. Sys.", "ref_csys",        "Static",  "Reference", false, nullptr, false },

    // --- Tab: Modal (natural frequencies / mode shapes; loads not applicable) ---
    { "sim_new_modal",     "New Study",   "study_modal",     "Modal",   "Study",     true,  nullptr, false },
    { "sim_material",      "Material",    "material",        "Modal",   "Setup",     true,  nullptr, false },
    { "sim_fixture",       "Fixture",     "fixture",         "Modal",   "Setup",     true,  nullptr, false },
    { "sim_modal_options", "Frequencies", "study_modal",     "Modal",   "Options",   true,  nullptr, false },
    { "sim_mesh",          "Mesh",        "mesh",            "Modal",   "Mesh",      true,  nullptr, false },
    { "sim_run",           "Run",         "run_study",       "Modal",   "Run",       true,  nullptr, false },
    { "sim_results",       "Results",     "results",         "Modal",   "Run",       true,  nullptr, false },
    { "ref_plane",         "Ref. Plane",  "ref_plane",       "Modal",   "Reference", false, nullptr, false },
    { "ref_axis",          "Ref. Axis",   "ref_axis",        "Modal",   "Reference", false, nullptr, false },
    { "ref_point",         "Ref. Point",  "ref_point",       "Modal",   "Reference", false, nullptr, false },
    { "ref_csys",          "Coord. Sys.", "ref_csys",        "Modal",   "Reference", false, nullptr, false },

    // --- Tab: Flow (external CFD wind/water tunnel) — fluid, flow direction/boundaries, grid ---
    { "sim_new_flow",      "New Study",   "study_flow",      "Flow",    "Study",     true,  nullptr, false },
    { "sim_fluid",         "Fluid",       "material",        "Flow",    "Setup",     true,  nullptr, false },
    { "sim_flow_bc",       "Boundaries",  "fixture",         "Flow",    "Setup",     true,  nullptr, false },
    { "sim_flow_res",      "Resolution",  "mesh",            "Flow",    "Grid",      true,  nullptr, false },
    { "sim_run",           "Run",         "run_study",       "Flow",    "Run",       true,  nullptr, false },
    { "sim_results",       "Results",     "results",         "Flow",    "Run",       true,  nullptr, false },
    { "ref_plane",         "Ref. Plane",  "ref_plane",       "Flow",    "Reference", false, nullptr, false },
    { "ref_axis",          "Ref. Axis",   "ref_axis",        "Flow",    "Reference", false, nullptr, false },
    { "ref_point",         "Ref. Point",  "ref_point",       "Flow",    "Reference", false, nullptr, false },
    { "ref_csys",          "Coord. Sys.", "ref_csys",        "Flow",    "Reference", false, nullptr, false },

    // --- Tab: Thermal (steady-state heat transfer: wall BC / heat source / CHT) ---
    { "sim_new_thermal",   "New Study",   "study_thermal",   "Thermal", "Study",     true,  nullptr, false },
    { "sim_thermal_bc",    "Heat Loads",  "load_pressure",   "Thermal", "Setup",     true,  nullptr, false },
    { "sim_run",           "Run",         "run_study",       "Thermal", "Run",       true,  nullptr, false },
    { "sim_results",       "Results",     "results",         "Thermal", "Run",       true,  nullptr, false },
    { "ref_plane",         "Ref. Plane",  "ref_plane",       "Thermal", "Reference", false, nullptr, false },
    { "ref_axis",          "Ref. Axis",   "ref_axis",        "Thermal", "Reference", false, nullptr, false },
    { "ref_point",         "Ref. Point",  "ref_point",       "Thermal", "Reference", false, nullptr, false },
    { "ref_csys",          "Coord. Sys.", "ref_csys",        "Thermal", "Reference", false, nullptr, false },

    // --- Tab: Fatigue (cyclic life over a static stress state: loading + S-N/Marin) ---
    { "sim_new_fatigue",   "New Study",   "study_fatigue",   "Fatigue", "Study",     true,  nullptr, false },
    { "sim_material",      "Material",    "material",        "Fatigue", "Setup",     true,  nullptr, false },
    { "sim_fatigue_setup", "Loading / S-N","results",        "Fatigue", "Setup",     true,  nullptr, false },
    { "sim_mesh",          "Mesh",        "mesh",            "Fatigue", "Mesh",      true,  nullptr, false },
    { "sim_run",           "Run",         "run_study",       "Fatigue", "Run",       true,  nullptr, false },
    { "sim_results",       "Results",     "results",         "Fatigue", "Run",       true,  nullptr, false },
    { "ref_plane",         "Ref. Plane",  "ref_plane",       "Fatigue", "Reference", false, nullptr, false },
    { "ref_axis",          "Ref. Axis",   "ref_axis",        "Fatigue", "Reference", false, nullptr, false },
    { "ref_point",         "Ref. Point",  "ref_point",       "Fatigue", "Reference", false, nullptr, false },
    { "ref_csys",          "Coord. Sys.", "ref_csys",        "Fatigue", "Reference", false, nullptr, false },

    // --- Tab: Motion (kinematics: drive one assembly mate, play back the trajectory) ---
    { "sim_new_motion",    "New Study",   "mate",            "Motion",  "Study",     true,  nullptr, false },
    { "sim_motion_setup",  "Driver / Time","move_component", "Motion",  "Setup",     true,  nullptr, false },
    { "sim_run",           "Run",         "run_study",       "Motion",  "Run",       true,  nullptr, false },
    { "sim_results",       "Playback",    "results",         "Motion",  "Run",       true,  nullptr, false },
    { "ref_plane",         "Ref. Plane",  "ref_plane",       "Motion",  "Reference", false, nullptr, false },
    { "ref_axis",          "Ref. Axis",   "ref_axis",        "Motion",  "Reference", false, nullptr, false },
    { "ref_point",         "Ref. Point",  "ref_point",       "Motion",  "Reference", false, nullptr, false },
    { "ref_csys",          "Coord. Sys.", "ref_csys",        "Motion",  "Reference", false, nullptr, false },

    // --- Tab: EM (electromagnetics — backed by the em_* worker RPCs, parallel agent).
    // SimManager gates every sim_em_* command on an em_status probe: when the solver backend is
    // not installed the whole set renders disabled with an honest "EM solver not installed"
    // tooltip (ui::setRibbonDisabledPrefix). Study-kind buttons per Hunter's spec: Eigenmode,
    // Frequency Sweep (S-parameters), Time Domain; setup tools: ports / boundaries / freq range.
    { "sim_em_eigen",      "Eigenmode",   "study_modal",     "EM",      "Study",     true,  nullptr, false },
    { "sim_em_sweep",      "Freq Sweep",  "study_flow",      "EM",      "Study",     true,  nullptr, false },
    { "sim_em_time",       "Time Domain", "study_static",    "EM",      "Study",     true,  nullptr, false },
    { "sim_em_ports",      "Ports",       "point_ent",       "EM",      "Setup",     true,  nullptr, false },
    { "sim_em_boundary",   "Boundaries",  "fixture",         "EM",      "Setup",     true,  nullptr, false },
    { "sim_em_range",      "Freq Range",  "settings",        "EM",      "Setup",     false, nullptr, false },
    { "sim_em_run",        "Run",         "run_study",       "EM",      "Run",       true,  nullptr, false },
    { "sim_em_results",    "Results",     "results",         "EM",      "Run",       true,  nullptr, false },
    { "ref_plane",         "Ref. Plane",  "ref_plane",       "EM",      "Reference", false, nullptr, false },
    { "ref_axis",          "Ref. Axis",   "ref_axis",        "EM",      "Reference", false, nullptr, false },
    { "ref_point",         "Ref. Point",  "ref_point",       "EM",      "Reference", false, nullptr, false },
    { "ref_csys",          "Coord. Sys.", "ref_csys",        "EM",      "Reference", false, nullptr, false },

    // --- 3D Sketch (sketch3d features): entry point on Features + Sketch tabs. Creates a
    //     sketch3d feature and enters the free-space sketcher; the in-mode tools (line / 3-pt
    //     arc / spline / point, working-plane presets, constraints) live on the viewport
    //     overlay toolbar (sketch3d_mode.cpp), not the ribbon. Relocated OFF the Features tab
    //     into the Sketch tab's "Sketch" group so it sits right beside the "New Sketch" entry
    //     (the injected kSketchNewOnSketchTab, same group). Command wiring (sketch3d_new)
    //     unchanged — only tab/group/label moved. ---
    { "sketch3d_new",      "New 3D Sketch", "spline",        "Sketch",   "Sketch",   true,  nullptr, false },

    // RE-ANCHOR(reveng): tab "Reverse Eng" ribbon rows — RE-03 appends shell; RE-27 polishes.
    // Session · Sources · Scale · Reconstruct · Model · Deliver groups (contracts/reveng.md).
    { "reveng_new",          "New Session",    "new_part",        "Reverse Eng", "Session",     true,  nullptr, false },
    { "reveng_open",         "Open Session",   "open_part",       "Reverse Eng", "Session",     false, nullptr, false },
    { "reveng_from_library", "From Library",   "insert_component","Reverse Eng", "Session",     false, nullptr, false },
    // Sources group — photos / video / webcam / datasheet ingest lanes (RE-19 / RE-30).
    { "reveng_add_photos",   "Add Photos",     "new_part",        "Reverse Eng", "Sources",     true,  nullptr, false },
    { "reveng_add_video",    "Add Video",      "sweep",           "Reverse Eng", "Sources",     false, nullptr, false },
    { "reveng_webcam",       "Webcam",         "measure",         "Reverse Eng", "Sources",     false, nullptr, false },
    { "reveng_datasheet",    "Datasheet",      "open_part",       "Reverse Eng", "Sources",     false, nullptr, false },
    // Scale group — ArUco mat / two-point pick / known dimension (contracts/reveng.md §3.1).
    { "reveng_scale_aruco",  "ArUco Mat",      "smart_dim",       "Reverse Eng", "Scale",       true,  nullptr, false },
    { "reveng_scale_points", "Two Points",     "eval_measure",    "Reverse Eng", "Scale",       false, nullptr, false },
    { "reveng_scale_known",  "Known Dim",      "measure",         "Reverse Eng", "Scale",       false, nullptr, false },
    // Reconstruct group — sparse → dense → mesh + 3DGS splat view (RE-21 / RE-28 / RE-27).
    { "reveng_reconstruct",  "Reconstruct",    "dwg_rebuild",     "Reverse Eng", "Reconstruct", true,  nullptr, false },
    { "reveng_splat_view",   "Splat View",     "mesh",            "Reverse Eng", "Reconstruct", false, nullptr, false },
    // Model group — fit primitives, AI-driven model, mesh-body fallback (contracts/reveng.md §4).
    { "reveng_fit",          "Fit Primitives", "fillet",          "Reverse Eng", "Model",       true,  nullptr, false },
    { "reveng_ai_model",     "AI Model",       "new_part",        "Reverse Eng", "Model",       false, nullptr, false },
    { "reveng_mesh_body",    "Mesh Body",      "sim_mesh",        "Reverse Eng", "Model",       false, nullptr, false },
    // Deliver group — export bundle / add to Parts Hub / print scale mat.
    { "reveng_export",       "Export",         "dwg_export_pdf",  "Reverse Eng", "Deliver",     true,  nullptr, false },
    { "reveng_to_parts",     "Add to Parts",   "insert_component","Reverse Eng", "Deliver",     false, nullptr, false },
    { "reveng_print_mat",    "Print Scale Mat","make_drawing",    "Reverse Eng", "Deliver",     false, nullptr, false },
};
// clang-format on

const RibbonCmd* getRibbonCmds(int& count) {
    count = (int)(sizeof(kCmds) / sizeof(kCmds[0]));
    return kCmds;
}

}  // namespace ui
