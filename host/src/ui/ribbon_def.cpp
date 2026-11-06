// Chudware ribbon command table — C14.
// ONE static table: {cmd_id, label, icon, tab, group, big, sub_of, stretch}.
// Sorted by tab / group / big-first to keep later-card appends clean.
// stretch=true = schema-reserved, rendered disabled in v1.
//
// APPEND-ONLY: later cards add rows here; never reorder or delete existing rows.
// To CHANGE a row (group / label / icon / size / position in its group), append a replacement row
// with the same tab + cmd_id: the LATER row wins and the earlier one is skipped by every renderer
// (ribbonRowActive). To drop a row from view with no replacement, list it in kRetiredRows below.
// Group order within a tab is declared in widgets/ribbon.cpp kTabGroupOrder, never by row order.
#include "ribbon_def.h"
#include "web_capabilities.h"
#include <cstring>
#include <initializer_list>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

namespace ui {

// clang-format off

// Simulate standard (2026-10): the Study group every simulation study tab carries. "New Study" is a
// launcher split button — a plain click always opens the grouped New Study picker (sim_new_picker,
// SimManager); its arrow lists the study types directly, so Buckling / Contact / Vibration /
// Thermal FEA are one click away on every tab. Thermal CFD (sim_new_thermal) needs the OpenFOAM
// backend, so it is offered only by the picker, which shows why it is unavailable. ICON is the
// tab's own study glyph.
#define SIM_NEW_STUDY_ROWS(TAB, ICON) \
    { "sim_new_picker",      "New Study",   ICON,                TAB, "Study", true,  nullptr,          false }, \
    { "sim_new_static",      "Static",      "study_static",      TAB, "Study", false, "sim_new_picker", false }, \
    { "sim_new_modal",       "Modal",       "study_modal",       TAB, "Study", false, "sim_new_picker", false }, \
    { "sim_new_buckling",    "Buckling",    "study_buckling",    TAB, "Study", false, "sim_new_picker", false }, \
    { "sim_new_fatigue",     "Fatigue",     "study_fatigue",     TAB, "Study", false, "sim_new_picker", false }, \
    { "sim_new_contact",     "Contact",     "study_contact",     TAB, "Study", false, "sim_new_picker", false }, \
    { "sim_new_freqresp",    "Vibration",   "study_vibration",   TAB, "Study", false, "sim_new_picker", false }, \
    { "sim_new_thermal_fea", "Thermal FEA", "study_thermal_fea", TAB, "Study", false, "sim_new_picker", false }, \
    { "sim_new_flow",        "Flow",        "study_flow",        TAB, "Study", false, "sim_new_picker", false }, \
    { "sim_new_motion",      "Motion",      "mate",              TAB, "Study", false, "sim_new_picker", false }, \
    { "sim_new_topology",    "Topology",    "study_topology",    TAB, "Study", false, "sim_new_picker", false }

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
    // sk_relations opens the Add Relation popup over the selection (sketch entities plus picked
    // model vertices/edges/faces); sk_fix pins the selection; sk_display_relations opens the
    // Display/Delete Relations list; sk_fully_define runs the engine sketch_fully_define op.
    { "sk_relations",      "Relations",      "relations",       "Sketch",   "Relations",       true,  nullptr,          false },
    { "sk_fix",            "Fix",            "fix",             "Sketch",   "Relations",       false, nullptr,          false },
    { "sk_display_relations","Show Relations","display_relations","Sketch", "Relations",       false, nullptr,          false },
    { "sk_fully_define",   "Fully Define",   "fully_define",    "Sketch",   "Relations",       true,  nullptr,          false },

    // =========================================================================
    // Pseudo-tab: "Sketch3D" — the 3D free-space sketcher's ribbon tool set. NOT one of
    // kRibbonTabs (never shown by normal tab iteration); ribbon.cpp swaps the Sketch tab's
    // command source to these rows while a sketch3d feature is being edited (3D tools
    // belong in the ribbon, not the sketch3d_mode.cpp floating overlay).
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
    { "eval_measure",      "Measure",        "measure",         "Evaluate", "Measure",         true,  nullptr,          false, CommandCapability::Inspect },
    { "eval_mass_props",   "Mass Props.",    "mass_props",      "Evaluate", "Measure",         true,  nullptr,          false, CommandCapability::Inspect },
    { "eval_section_props","Section Props.", "section_props",   "Evaluate", "Measure",         true,  nullptr,          false, CommandCapability::Inspect },

    // --- Group: Geometry Analysis ---
    { "eval_draft",        "Draft Anlys.",   "draft_analysis",  "Evaluate", "Geometry",        true,  nullptr,          false, CommandCapability::Inspect },
    { "eval_thickness",    "Thickness",      "thickness",       "Evaluate", "Geometry",        true,  nullptr,          false, CommandCapability::Inspect },
    { "eval_curvature",    "Curvature",      "curvature",       "Evaluate", "Geometry",        true,  nullptr,          false, CommandCapability::Inspect },
    { "eval_zebra",        "Zebra",          "zebra",           "Evaluate", "Geometry",        false, nullptr,          false, CommandCapability::Inspect },
    { "eval_symmetry",     "Symmetry",       "symmetry_check",  "Evaluate", "Geometry",        false, nullptr,          false, CommandCapability::Inspect },

    // --- Group: Check ---
    { "eval_check",        "Check",          "check",           "Evaluate", "Geometry Check",  true,  nullptr,          false, CommandCapability::Inspect },
    { "eval_geometry",     "Geometry Anlys.","geometry_analysis","Evaluate","Geometry Check",  false, nullptr,          false, CommandCapability::Inspect },
    { "eval_model_history","Model History", "results",          "Evaluate", "Geometry Check",  true,  nullptr,          false, CommandCapability::Inspect },
    { "eval_interference", "Interference",   "interference_eval","Evaluate","Geometry Check",  true,  nullptr,          false, CommandCapability::Inspect },

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
    { "make_drawing",        "Make Drawing",    "file_doc",         "Evaluate", "Drawing",        true,  nullptr,          false },

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
    { "sm_export_flat_dxf",  "Export Flat DXF", "sm_flat_pattern",  "Sheet Metal", "Flat Pattern",true,  nullptr,          false, CommandCapability::Export },

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

    // Electronics is a KiCad project hub; editing is performed in KiCad.
    { "ecad_ai_design",          "AI Design",        "ai_panel",       "PCB Design", "Project", true,  nullptr, false },
    { "ecad_new_project",        "New Project",      "ecad_pcb",       "PCB Design", "Project", true,  nullptr, false },
    { "ecad_open",               "Open",             "open",           "PCB Design", "Project", false, nullptr, false },
    { "ecad_save",               "Save",             "save",           "PCB Design", "Project", false, nullptr, false },
    { "ecad_kicad_open_project", "KiCad",            "open",           "PCB Design", "KiCad",   true,  nullptr, false },
    { "ecad_kicad_open_pcb",     "PCB Editor",       "ecad_pcb",       "PCB Design", "KiCad",   true,  nullptr, false },
    { "pcb_update_from_sch",     "Update Board",     "ecad_update",    "PCB Design", "Board",   true,  nullptr, false },
    { "ecad_autoroute",          "Autoroute",        "ecad_autoroute", "PCB Design", "Board",   true,  nullptr, false },
    { "ecad_fill_zones",         "Fill Zones",       "ecad_fill",      "PCB Design", "Board",   false, nullptr, false },
    { "ecad_to_3d",              "Board to 3D",      "ecad_board3d",   "PCB Design", "Outputs", true,  nullptr, false },
    { "ecad_export_fab",         "Fabrication",      "ecad_gerber",    "PCB Design", "Outputs", true,  nullptr, false, CommandCapability::Export },
    { "ecad_export_step",        "STEP",             "save",           "PCB Design", "Outputs", false, nullptr, false, CommandCapability::Export },
    { "ecad_export_bom",         "BOM",              "bom",            "PCB Design", "Outputs", false, nullptr, false, CommandCapability::Export },
    { "ecad_run_drc",            "Run DRC",          "ecad_drc",       "PCB Design", "Checks",  true,  nullptr, false },

    { "ecad_kicad_open_sch",     "Schematic Editor", "sch_new",       "Schematic", "KiCad",   true,  nullptr, false },
    { "ecad_run_erc",            "Run ERC",          "sch_erc",       "Schematic", "Checks",  true,  nullptr, false },
    { "ecad_simulate",           "Simulate",         "sch_simulate",  "Schematic", "Analyze", true,  nullptr, false },
    { "sch_export_netlist",      "Netlist",          "file_doc",      "Schematic", "Outputs", false, nullptr, false, CommandCapability::Export },
    { "sch_export_pdf",          "PDF",              "file_doc",      "Schematic", "Outputs", false, nullptr, false, CommandCapability::Export },
    { "ecad_export_bom",         "BOM",              "bom",           "Schematic", "Outputs", false, nullptr, false, CommandCapability::Export },
    { "ecad_save",               "Save",             "save",          "Schematic", "Project", false, nullptr, false },
    { "ecad_kicad_open_project", "KiCad",            "open",          "Schematic", "KiCad",   false, nullptr, false },
    { "ecad_open",               "Open",             "open",          "Schematic", "Project", false, nullptr, false },
    { "ecad_new_project",        "New Project",      "ecad_pcb",      "Schematic", "Project", false, nullptr, false },
    { "ecad_ai_design",          "AI Design",        "ai_panel",      "Schematic", "Project", false, nullptr, false },

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
    { "dwg_fit",          "Fit",           "zoom_fit",      "View Layout", "Sheet", false, nullptr, false, CommandCapability::Inspect },
    { "dwg_export_dxf",   "Export DXF",    "file_doc",     "View Layout", "Sheet", false, nullptr, false, CommandCapability::Export },
    { "dwg_export_pdf",   "Export PDF",    "save",          "View Layout", "Sheet", false, nullptr, false, CommandCapability::Export },
    { "dwg_export_svg",   "Export SVG",    "file_doc",      "View Layout", "Sheet", false, nullptr, false, CommandCapability::Export },
    { "dwg_arrange",      "Auto Arrange",  "view_cube",     "View Layout", "Sheet", false, nullptr, false },
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
    // Toggles the in-process stack sampler in libchudware (prof_start/prof_stop).
    // Click to start; model; click again to stop + save a report.
    { "prof_toggle",      "Profiler",      "results",       "Evaluate", "Performance",   true,  nullptr, false, CommandCapability::Inspect },

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
    { "display_shaded",       "Shaded",          "display_shaded",       "Appearance", "Display", true,  nullptr, false, CommandCapability::Inspect },
    { "display_shaded_edges", "Shaded Edges",    "display_shaded_edges", "Appearance", "Display", true,  nullptr, false, CommandCapability::Inspect },
    { "display_hlr",          "Hidden Removed",  "display_hlr",          "Appearance", "Display", true,  nullptr, false, CommandCapability::Inspect },
    { "display_hlv",          "Hidden Visible",  "display_hlv",          "Appearance", "Display", false, nullptr, false, CommandCapability::Inspect },
    { "display_wireframe",    "Wireframe",       "display_wireframe",    "Appearance", "Display", true,  nullptr, false, CommandCapability::Inspect },

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
    // cam-workspace overhaul (append-only): the Manufacture ribbon carries the full CAM
    // command set. Show in 3D toggles the native viewport toolpath overlay; Flavor opens the
    // dynamic post-dialect dropdown populated by the integrated workspace tree.
    // =========================================================================
    { "cam_show3d",        "Show in 3D",  "display_shaded",  "Manufacture", "Output",    false, nullptr, false },
    { "cam_flavor",        "Flavor",      "settings",        "Manufacture", "Output",    false, nullptr, false },

    // =========================================================================
    // sim-gui rework: per-simulation-type ribbon tabs, one per study kind,
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

    // --- Tab: EM (electromagnetics — backed by the em_* worker RPCs).
    // The cached backend capability gates Run; analysis and setup remain editable.
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
    { "reveng_new",          "New Session",    "new_doc",         "Reverse Eng", "Session",     true,  nullptr, false },
    { "reveng_open",         "Open Session",   "open",            "Reverse Eng", "Session",     false, nullptr, false },
    { "reveng_from_library", "From Library",   "insert_component","Reverse Eng", "Session",     false, nullptr, false },
    // Sources group — photos / video / webcam / datasheet ingest lanes (RE-19 / RE-30).
    { "reveng_add_photos",   "Add Photos",     "file_image",      "Reverse Eng", "Sources",     true,  nullptr, false },
    { "reveng_add_video",    "Add Video",      "sweep",           "Reverse Eng", "Sources",     false, nullptr, false },
    { "reveng_webcam",       "Webcam",         "measure",         "Reverse Eng", "Sources",     false, nullptr, false },
    { "reveng_datasheet",    "Datasheet",      "file_doc",        "Reverse Eng", "Sources",     false, nullptr, false },
    // Scale group — ArUco mat / two-point pick / known dimension (contracts/reveng.md §3.1).
    { "reveng_scale_aruco",  "ArUco Mat",      "smart_dim",       "Reverse Eng", "Scale",       true,  nullptr, false },
    { "reveng_scale_points", "Two Points",     "measure",         "Reverse Eng", "Scale",       false, nullptr, false },
    { "reveng_scale_known",  "Known Dim",      "measure",         "Reverse Eng", "Scale",       false, nullptr, false },
    // Reconstruct group — sparse → dense → mesh + 3DGS splat view (RE-21 / RE-28 / RE-27).
    { "reveng_reconstruct",  "Reconstruct",    "refresh",         "Reverse Eng", "Reconstruct", true,  nullptr, false },
    { "reveng_splat_view",   "Splat View",     "mesh",            "Reverse Eng", "Reconstruct", false, nullptr, false },
    // Model group — fit primitives, AI-driven model, mesh-body fallback (contracts/reveng.md §4).
    { "reveng_fit",          "Fit Primitives", "fillet",          "Reverse Eng", "Model",       true,  nullptr, false },
    { "reveng_ai_model",     "AI Model",       "ai_panel",        "Reverse Eng", "Model",       false, nullptr, false },
    { "reveng_mesh_body",    "Mesh Body",      "mesh",            "Reverse Eng", "Model",       false, nullptr, false },
    // Deliver group — export bundle / add to Parts Hub / print scale mat.
    { "reveng_export",       "Export",         "save",            "Reverse Eng", "Deliver",     true,  nullptr, false, CommandCapability::Export },
    { "reveng_to_parts",     "Add to Parts",   "insert_component","Reverse Eng", "Deliver",     false, nullptr, false },
    { "reveng_print_mat",    "Print Scale Mat","file_doc",        "Reverse Eng", "Deliver",     false, nullptr, false },


    // Motion study entry points (2026-09-28): the Motion tab only exists in the Simulate
    // workspace, so an assembly in Model had no visible way into its Motion study. Both rows
    // open the doc's saved Motion study on its playback node (or create one) — SimManager.
    { "sim_motion_study",     "Motion Study",    "mate",           "Features",   "Assembly",  true,  nullptr,     false },
    { "sim_motion_study",     "Motion Study",    "mate",           "Simulation", "Study",     true,  nullptr,     false },

    // --- Tab: Topology (topology optimization study — Simulate workspace only, tab index 21).
    // Every row routes through SimManager::handleCommand (app.cpp sends the whole sim_ prefix
    // there). Material / Fixture / Force / Pressure / Mesh / Run / Results reuse the shared
    // study commands; the sim_topology_* rows open the topology study's own setup nodes:
    // method + physics + objective/constraints (capability-matrix driven), the thermal / flow
    // boundary editor, preserved (keep-solid) and excluded (keep-void) region wells,
    // manufacturing rules, and the retained-run comparison. Result export lives on the result
    // panel's artifact rows (only geometry-validated surfaces are offered), so no ribbon row.
    { "sim_new_topology",           "New Study",   "study_static",  "Topology", "Study",         true,  nullptr, false },
    { "sim_topology_setup",         "Method",      "settings",      "Topology", "Setup",         true,  nullptr, false },
    { "sim_material",               "Material",    "material",      "Topology", "Setup",         true,  nullptr, false },
    { "sim_fixture",                "Fixture",     "fixture",       "Topology", "Boundaries",    true,  nullptr, false },
    { "sim_load_force",             "Force",       "load_force",    "Topology", "Boundaries",    false, nullptr, false },
    { "sim_load_pressure",          "Pressure",    "load_pressure", "Topology", "Boundaries",    false, nullptr, false },
    { "sim_topology_boundaries",    "Heat / Flow", "flow_arrow",    "Topology", "Boundaries",    true,  nullptr, false },
    { "sim_topology_preserve",      "Preserve",    "combine",       "Topology", "Regions",       true,  nullptr, false },
    { "sim_topology_exclude",       "Exclude",     "subtract",      "Topology", "Regions",       true,  nullptr, false },
    { "sim_topology_manufacturing", "Mfg. Rules",  "draft",         "Topology", "Manufacturing", true,  nullptr, false },
    { "sim_mesh",                   "Mesh",        "mesh",          "Topology", "Mesh",          true,  nullptr, false },
    { "sim_run",                    "Run",         "run_study",     "Topology", "Run",           true,  nullptr, false },
    { "sim_results",                "Results",     "results",       "Topology", "Results",       true,  nullptr, false },
    { "sim_topology_compare",       "Compare",     "legend",        "Topology", "Results",       true,  nullptr, false },
    { "ref_plane",                  "Ref. Plane",  "ref_plane",     "Topology", "Reference",     false, nullptr, false },
    { "ref_axis",                   "Ref. Axis",   "ref_axis",      "Topology", "Reference",     false, nullptr, false },
    { "ref_point",                  "Ref. Point",  "ref_point",     "Topology", "Reference",     false, nullptr, false },
    { "ref_csys",                   "Coord. Sys.", "ref_csys",      "Topology", "Reference",     false, nullptr, false },

    // --- Tab: Piping (piping networks, internal-flow CFD, vessel + duct sizing — Simulate workspace
    // only, tab index 22). Every row routes through SimManager::handleCommand (sim_ prefix) to the
    // PipingPanel, which calls the same native RPCs the agent's piping/industrial tools call and
    // saves each result in the document as a study.
    { "sim_piping_new",             "New Network", "new_doc",       "Piping",   "Network",       true,  nullptr, false },
    { "sim_piping_route",           "Route Pipe",  "sweep",         "Piping",   "Network",       true,  nullptr, false },
    { "sim_piping_solve",           "Solve",       "run_study",     "Piping",   "Analysis",      true,  nullptr, false },
    { "sim_piping_spec",            "Spec Check",  "check",         "Piping",   "Analysis",      true,  nullptr, false },
    { "sim_piping_bom",             "BOM",         "bom",           "Piping",   "Analysis",      true,  nullptr, false },
    { "sim_piping_flow",            "Internal CFD","study_flow",    "Piping",   "Flow",          true,  nullptr, false },
    { "sim_piping_vessel",          "Vessel",      "revolve_boss",  "Piping",   "Equipment",     true,  nullptr, false },
    { "sim_piping_duct",            "Duct Sizing", "flow_arrow",    "Piping",   "Equipment",     true,  nullptr, false },

    // --- Tab: Hydraulics (fluid-power circuits: pumps, valves, cylinders, motors — Simulate
    // workspace only, tab index 23). Every row routes through SimManager::handleCommand (sim_
    // prefix) to the HydraulicPanel, which calls the same native RPCs the agent's hydraulic tools
    // call and saves the circuit and its results in the document as a hydraulic_circuit study.
    { "sim_hyd_new",                "New Circuit", "new_doc",          "Hydraulics", "Circuit",   true,  nullptr, false },
    { "sim_hyd_add",                "Add Component","insert_component","Hydraulics", "Circuit",   true,  nullptr, false },
    { "sim_hyd_hose",               "Connect",     "sch_wire",         "Hydraulics", "Circuit",   true,  nullptr, false },
    { "sim_hyd_size_cyl",           "Size Cylinder","cylinder_feature","Hydraulics", "Sizing",    true,  nullptr, false },
    { "sim_hyd_size_pump",          "Size Pump",   "cat_hydraulics",   "Hydraulics", "Sizing",    true,  nullptr, false },
    { "sim_hyd_size_motor",         "Size Motor",  "cat_drivetrain",   "Hydraulics", "Sizing",    true,  nullptr, false },
    { "sim_hyd_drive",              "Pump Drive",  "performance",      "Hydraulics", "Sizing",    true,  nullptr, false },
    { "sim_hyd_simulate",           "Simulate",    "run_study",        "Hydraulics", "Analysis",  true,  nullptr, false },
    { "sim_hyd_bom",                "BOM",         "bom",              "Hydraulics", "Analysis",  true,  nullptr, false },
    { "sim_hyd_piping",             "To Piping",   "sweep",            "Hydraulics", "Analysis",  true,  nullptr, false },

    // =========================================================================
    // Simulate standard (2026-10) — every simulation tab, re-laid out as replacement rows (the
    // later row for a tab + cmd_id wins, see the header). Study tabs follow
    //   Study · Setup · Boundaries · Mesh · Run · Results · Reference
    // (a group is omitted only when it does not apply: Modal has no loads, Motion no mesh), and a
    // command has the same group / label / icon / size on every tab: Fixture, Force and Pressure
    // are big buttons in Boundaries everywhere. Piping / Hydraulics are not studies:
    //   Network | Circuit · Design · Analysis · Results · Reference.
    // Group order is enforced by widgets/ribbon.cpp kTabGroupOrder; the Reference rows above stay.
    // =========================================================================

    // --- Tab: Simulation (legacy all-in-one tab, Model workspace once a sim tool was used) ---
    SIM_NEW_STUDY_ROWS("Simulation", "ws_simulate"),
    { "sim_motion_study",  "Motion Study", "mate",           "Simulation", "Study",      true,  nullptr, false },
    { "sim_material",      "Material",     "material",       "Simulation", "Setup",      true,  nullptr, false },
    { "sim_fixture",       "Fixture",      "fixture",        "Simulation", "Boundaries", true,  nullptr, false },
    { "sim_load_force",    "Force",        "load_force",     "Simulation", "Boundaries", true,  nullptr, false },
    { "sim_load_pressure", "Pressure",     "load_pressure",  "Simulation", "Boundaries", true,  nullptr, false },
    { "sim_mesh",          "Mesh",         "mesh",           "Simulation", "Mesh",       true,  nullptr, false },
    { "sim_run",           "Run",          "run_study",      "Simulation", "Run",        true,  nullptr, false },
    { "sim_results",       "Results",      "results",        "Simulation", "Results",    true,  nullptr, false },
    { "ref_plane",         "Ref. Plane",   "ref_plane",      "Simulation", "Reference",  false, nullptr, false },
    { "ref_axis",          "Ref. Axis",    "ref_axis",       "Simulation", "Reference",  false, nullptr, false },
    { "ref_point",         "Ref. Point",   "ref_point",      "Simulation", "Reference",  false, nullptr, false },
    { "ref_csys",          "Coord. Sys.",  "ref_csys",       "Simulation", "Reference",  false, nullptr, false },

    // --- Tab: Static ---
    SIM_NEW_STUDY_ROWS("Static", "study_static"),
    { "sim_material",      "Material",     "material",       "Static",     "Setup",      true,  nullptr, false },
    { "sim_fixture",       "Fixture",      "fixture",        "Static",     "Boundaries", true,  nullptr, false },
    { "sim_load_force",    "Force",        "load_force",     "Static",     "Boundaries", true,  nullptr, false },
    { "sim_load_pressure", "Pressure",     "load_pressure",  "Static",     "Boundaries", true,  nullptr, false },
    { "sim_mesh",          "Mesh",         "mesh",           "Static",     "Mesh",       true,  nullptr, false },
    { "sim_run",           "Run",          "run_study",      "Static",     "Run",        true,  nullptr, false },
    { "sim_results",       "Results",      "results",        "Static",     "Results",    true,  nullptr, false },

    // --- Tab: Modal (loads do not apply; fixtures constrain rigid-body modes) ---
    SIM_NEW_STUDY_ROWS("Modal", "study_modal"),
    { "sim_material",      "Material",     "material",       "Modal",      "Setup",      true,  nullptr, false },
    { "sim_modal_options", "Frequencies",  "freq_range",     "Modal",      "Setup",      true,  nullptr, false },
    { "sim_fixture",       "Fixture",      "fixture",        "Modal",      "Boundaries", true,  nullptr, false },
    { "sim_mesh",          "Mesh",         "mesh",           "Modal",      "Mesh",       true,  nullptr, false },
    { "sim_run",           "Run",          "run_study",      "Modal",      "Run",        true,  nullptr, false },
    { "sim_results",       "Results",      "results",        "Modal",      "Results",    true,  nullptr, false },

    // --- Tab: Flow (external CFD; the solver grid is the flow study's mesh) ---
    SIM_NEW_STUDY_ROWS("Flow", "study_flow"),
    { "sim_fluid",         "Fluid",        "fluid",          "Flow",       "Setup",      true,  nullptr, false },
    { "sim_flow_bc",       "Flow BCs",     "flow_bc",        "Flow",       "Boundaries", true,  nullptr, false },
    { "sim_flow_res",      "Resolution",   "mesh",           "Flow",       "Mesh",       true,  nullptr, false },
    { "sim_run",           "Run",          "run_study",      "Flow",       "Run",        true,  nullptr, false },
    { "sim_results",       "Results",      "results",        "Flow",       "Results",    true,  nullptr, false },

    // --- Tab: Thermal (thermal FEA: conduction in the solid — fixed-temperature faces are the
    //     study's fixtures, heat loads are flux / convection). Thermal CFD needs the OpenFOAM
    //     backend and stays reachable only through New Study, where the picker gives the reason. ---
    SIM_NEW_STUDY_ROWS("Thermal", "study_thermal_fea"),
    { "sim_material",      "Material",     "material",       "Thermal",    "Setup",      true,  nullptr, false },
    { "sim_fixture",       "Fixture",      "fixture",        "Thermal",    "Boundaries", true,  nullptr, false },
    { "sim_load_heat",     "Heat Load",    "load_heat",      "Thermal",    "Boundaries", true,  nullptr, false },
    { "sim_mesh",          "Mesh",         "mesh",           "Thermal",    "Mesh",       true,  nullptr, false },
    { "sim_run",           "Run",          "run_study",      "Thermal",    "Run",        true,  nullptr, false },
    { "sim_results",       "Results",      "results",        "Thermal",    "Results",    true,  nullptr, false },

    // --- Tab: Fatigue (cyclic life over a static stress state: fixtures + loads + S-N) ---
    SIM_NEW_STUDY_ROWS("Fatigue", "study_fatigue"),
    { "sim_material",      "Material",     "material",       "Fatigue",    "Setup",      true,  nullptr, false },
    { "sim_fatigue_setup", "Loading / S-N","plots_view",     "Fatigue",    "Setup",      true,  nullptr, false },
    { "sim_fixture",       "Fixture",      "fixture",        "Fatigue",    "Boundaries", true,  nullptr, false },
    { "sim_load_force",    "Force",        "load_force",     "Fatigue",    "Boundaries", true,  nullptr, false },
    { "sim_load_pressure", "Pressure",     "load_pressure",  "Fatigue",    "Boundaries", true,  nullptr, false },
    { "sim_mesh",          "Mesh",         "mesh",           "Fatigue",    "Mesh",       true,  nullptr, false },
    { "sim_run",           "Run",          "run_study",      "Fatigue",    "Run",        true,  nullptr, false },
    { "sim_results",       "Results",      "results",        "Fatigue",    "Results",    true,  nullptr, false },

    // --- Tab: Motion (kinematics: no mesh, no loads; Results is the playback node) ---
    SIM_NEW_STUDY_ROWS("Motion", "mate"),
    { "sim_motion_setup",  "Driver / Time","move_component", "Motion",     "Setup",      true,  nullptr, false },
    { "sim_run",           "Run",          "run_study",      "Motion",     "Run",        true,  nullptr, false },
    { "sim_results",       "Results",      "results",        "Motion",     "Results",    true,  nullptr, false },

    // --- Tab: EM. The three analysis kinds open their forms even when Run is unavailable. ---
    SIM_NEW_STUDY_ROWS("EM", "study_em"),
    { "sim_em_eigen",      "Eigenmode",    "study_modal",    "EM",         "Study",      false, nullptr, false },
    { "sim_em_sweep",      "Freq Sweep",   "plots_view",     "EM",         "Study",      false, nullptr, false },
    { "sim_em_time",       "Time Domain",  "activity",       "EM",         "Study",      false, nullptr, false },
    { "sim_em_range",      "Freq Range",   "freq_range",     "EM",         "Setup",      true,  nullptr, false },
    { "sim_em_ports",      "Ports",        "em_port",        "EM",         "Boundaries", true,  nullptr, false },
    { "sim_em_boundary",   "EM BCs",       "em_boundary",    "EM",         "Boundaries", true,  nullptr, false },
    { "sim_em_run",        "Run",          "run_study",      "EM",         "Run",        true,  nullptr, false },
    { "sim_em_results",    "Results",      "results",        "EM",         "Results",    true,  nullptr, false },

    // --- Tab: Topology (regions and manufacturing rules are setup; heat / flow BCs sit with the
    //     structural boundaries) ---
    SIM_NEW_STUDY_ROWS("Topology", "study_topology"),
    { "sim_topology_setup",         "Method",      "settings",      "Topology", "Setup",      true,  nullptr, false },
    { "sim_material",               "Material",    "material",      "Topology", "Setup",      true,  nullptr, false },
    { "sim_topology_preserve",      "Preserve",    "combine",       "Topology", "Setup",      false, nullptr, false },
    { "sim_topology_exclude",       "Exclude",     "subtract",      "Topology", "Setup",      false, nullptr, false },
    { "sim_topology_manufacturing", "Mfg. Rules",  "draft",         "Topology", "Setup",      false, nullptr, false },
    { "sim_fixture",                "Fixture",     "fixture",       "Topology", "Boundaries", true,  nullptr, false },
    { "sim_load_force",             "Force",       "load_force",    "Topology", "Boundaries", true,  nullptr, false },
    { "sim_load_pressure",          "Pressure",    "load_pressure", "Topology", "Boundaries", true,  nullptr, false },
    { "sim_topology_boundaries",    "Heat / Flow", "load_heat",     "Topology", "Boundaries", true,  nullptr, false },
    { "sim_mesh",                   "Mesh",        "mesh",          "Topology", "Mesh",       true,  nullptr, false },
    { "sim_run",                    "Run",         "run_study",     "Topology", "Run",        true,  nullptr, false },
    { "sim_results",                "Results",     "results",       "Topology", "Results",    true,  nullptr, false },
    { "sim_topology_compare",       "Compare",     "plots_view",    "Topology", "Results",    true,  nullptr, false },

    // --- Tab: Piping ---
    { "sim_piping_new",    "New Network",  "new_network",    "Piping",     "Network",    true,  nullptr, false },
    { "sim_piping_route",  "Route Pipe",   "route_pipe",     "Piping",     "Network",    true,  nullptr, false },
    { "sim_piping_draw",   "Draw Pipe",    "line",           "Piping",     "Network",    true,  nullptr, false },
    { "sim_piping_fitting","Fittings",     "insert_component","Piping",    "Network",    true,  nullptr, false },
    { "sim_piping_vessel", "Vessel",       "vessel",         "Piping",     "Design",     true,  nullptr, false },
    { "sim_piping_duct",   "Duct Sizing",  "duct",           "Piping",     "Design",     true,  nullptr, false },
    { "sim_piping_solve",  "Solve",        "run_study",      "Piping",     "Analysis",   true,  nullptr, false },
    { "sim_piping_spec",   "Spec Check",   "spec_check",     "Piping",     "Analysis",   true,  nullptr, false },
    { "sim_piping_flow",   "Internal CFD", "study_flow",     "Piping",     "Analysis",   true,  nullptr, false },
    { "sim_piping_bom",    "BOM",          "bom",            "Piping",     "Results",    true,  nullptr, false },
    { "ref_plane",         "Ref. Plane",   "ref_plane",      "Piping",     "Reference",  false, nullptr, false },
    { "ref_axis",          "Ref. Axis",    "ref_axis",       "Piping",     "Reference",  false, nullptr, false },
    { "ref_point",         "Ref. Point",   "ref_point",      "Piping",     "Reference",  false, nullptr, false },
    { "ref_csys",          "Coord. Sys.",  "ref_csys",       "Piping",     "Reference",  false, nullptr, false },

    // --- Tab: Hydraulics ---
    { "sim_hyd_new",       "New Circuit",  "new_circuit",    "Hydraulics", "Circuit",    true,  nullptr, false },
    { "sim_hyd_add",       "Add Component","insert_component","Hydraulics","Circuit",    true,  nullptr, false },
    { "sim_hyd_hose",      "Connect",      "sch_wire",       "Hydraulics", "Circuit",    true,  nullptr, false },
    { "sim_hyd_size_cyl",  "Size Cylinder","size_actuator",  "Hydraulics", "Design",     true,  nullptr, false },
    { "sim_hyd_size_pump", "Size Pump",    "size_pump",      "Hydraulics", "Design",     true,  nullptr, false },
    { "sim_hyd_size_motor","Size Motor",   "size_motor",     "Hydraulics", "Design",     true,  nullptr, false },
    { "sim_hyd_drive",     "Pump Drive",   "pump_drive",     "Hydraulics", "Design",     true,  nullptr, false },
    { "sim_hyd_simulate",  "Simulate",     "run_study",      "Hydraulics", "Analysis",   true,  nullptr, false },
    { "sim_hyd_view_schematic","Schematic","schematic",      "Hydraulics", "Results",    true,  nullptr, false },
    { "sim_hyd_view_plots","Plots",        "plots_view",     "Hydraulics", "Results",    true,  nullptr, false },
    { "sim_hyd_view_3d",   "3D Layout",    "depth_3d",       "Hydraulics", "Results",    true,  nullptr, false },
    { "sim_hyd_bom",       "BOM",          "bom",            "Hydraulics", "Results",    true,  nullptr, false },
    { "sim_hyd_piping",    "To Piping",    "study_piping",   "Hydraulics", "Results",    true,  nullptr, false },
    { "ref_plane",         "Ref. Plane",   "ref_plane",      "Hydraulics", "Reference",  false, nullptr, false },
    { "ref_axis",          "Ref. Axis",    "ref_axis",       "Hydraulics", "Reference",  false, nullptr, false },
    { "ref_point",         "Ref. Point",   "ref_point",      "Hydraulics", "Reference",  false, nullptr, false },
    { "ref_csys",          "Coord. Sys.",  "ref_csys",       "Hydraulics", "Reference",  false, nullptr, false },

    // =========================================================================
    // Simulate corrective wave 2 — ribbon labels match the study pane's setup sub-chips and form
    // tabs (Modes, Excitation, Contact, Conduction, Loading, Drivers, Settings, Fixed Temp,
    // Boundaries), and every study type sharing a tab gets its own setup command. Rows re-appended
    // only to keep the chip order inside their group (a later row wins, see the header).
    // sim_buckling_options / sim_contact_options / sim_freqresp_options are study-scoped
    // (ribbonStudyGate): shown only while a study of that type is selected.
    // =========================================================================
    // --- Static tab: Buckling and Contact studies are created from it and set up here ---
    { "sim_buckling_options",    "Modes",       "study_buckling",    "Static",   "Setup",      true,  nullptr, false },
    { "sim_contact_options",     "Contact",     "study_contact",     "Static",   "Setup",      true,  nullptr, false },
    { "sim_material",            "Material",    "material",          "Static",   "Setup",      true,  nullptr, false },
    // --- Modal tab: Modal (natural frequencies) and Vibration studies ---
    { "sim_modal_options",       "Modes",       "freq_range",        "Modal",    "Setup",      true,  nullptr, false },
    { "sim_freqresp_options",    "Excitation",  "study_vibration",   "Modal",    "Setup",      true,  nullptr, false },
    { "sim_material",            "Material",    "material",          "Modal",    "Setup",      true,  nullptr, false },
    // --- Flow tab: the boundaries step is "Boundaries" in the study pane ---
    { "sim_flow_bc",             "Boundaries",  "flow_bc",           "Flow",     "Boundaries", true,  nullptr, false },
    // --- Thermal tab (Thermal FEA): Conduction options; fixtures are fixed-temperature faces ---
    { "sim_thermal_fea_options", "Conduction",  "study_thermal_fea", "Thermal",  "Setup",      true,  nullptr, false },
    { "sim_material",            "Material",    "material",          "Thermal",  "Setup",      true,  nullptr, false },
    { "sim_fixture",             "Fixed Temp",  "fixture",           "Thermal",  "Boundaries", true,  nullptr, false },
    { "sim_load_heat",           "Heat Load",   "load_heat",         "Thermal",  "Boundaries", true,  nullptr, false },
    // --- Fatigue tab ---
    { "sim_fatigue_setup",       "Loading",     "plots_view",        "Fatigue",  "Setup",      true,  nullptr, false },
    { "sim_material",            "Material",    "material",          "Fatigue",  "Setup",      true,  nullptr, false },
    // --- Motion tab ---
    { "sim_motion_setup",        "Drivers",     "move_component",    "Motion",   "Setup",      true,  nullptr, false },
    // --- Topology tab ---
    { "sim_topology_setup",      "Settings",    "settings",          "Topology", "Setup",      true,  nullptr, false },
    { "sim_material",            "Material",    "material",          "Topology", "Setup",      true,  nullptr, false },

    // =========================================================================
    // DWG export (2026-10): Drawing > View Layout > Sheet gains Export DWG (the native R2000 DWG
    // writer behind export_drawing format "dwg"). dwg_arrange is re-appended after it (a later row wins, see the
    // header) so the four Export rows stay adjacent and Auto Arrange keeps its place at the end.
    // =========================================================================
    { "dwg_export_dwg",   "Export DWG",    "file_doc",      "View Layout", "Sheet", false, nullptr, false, CommandCapability::Export },
    { "dwg_arrange",      "Auto Arrange",  "view_cube",     "View Layout", "Sheet", false, nullptr, false },

    // The same read-only study is available beside FEA without creating a second command path.
    { "eval_thickness", "Wall Thickness", "thickness", "Static", "Study", true, nullptr, false, CommandCapability::Inspect },
};
// clang-format on
#undef SIM_NEW_STUDY_ROWS

// Rows removed from view with no replacement (append-only: hidden, never deleted).
struct RetiredRow { const char* tab; const char* cmd_id; };
static const RetiredRow kRetiredRows[] = {
    // Static "Torque": the engine has no torque/moment load op and SimManager::handleCommand has
    // no sim_load_torque handler — a permanently disabled control, so it is not shown at all.
    { "Static", "sim_load_torque" },
    // Thermal "Heat Loads" (sim_thermal_bc) edits a thermal CFD study; the Thermal tab is a
    // Thermal FEA tab now (its heat loads are sim_load_heat), so the CFD-only row is not shown.
    { "Thermal", "sim_thermal_bc" },
    // Thermal CFD needs the OpenFOAM backend the desktop flow solver never runs, so its "New Study"
    // rows (Thermal tab, legacy Simulation tab) are not shown; the New Study picker offers it with
    // the reason it is unavailable.
    { "Thermal",    "sim_new_thermal" },
    { "Simulation", "sim_new_thermal" },
};

const RibbonCmd* getRibbonCmds(int& count) {
    count = (int)(sizeof(kCmds) / sizeof(kCmds[0]));
    return kCmds;
}

bool ribbonRowActive(int index) {
    static constexpr int n = (int)(sizeof(kCmds) / sizeof(kCmds[0]));
    // The table is static, so resolve every row once: retired, or superseded by a later row with
    // the same tab + cmd_id.
    static const std::vector<char> active = [] {
        std::vector<char> a((size_t)n, 1);
        for (int i = 0; i < n; ++i) {
            for (const RetiredRow& r : kRetiredRows)
                if (std::strcmp(r.tab, kCmds[i].tab) == 0 &&
                    std::strcmp(r.cmd_id, kCmds[i].cmd_id) == 0)
                    a[(size_t)i] = 0;
            for (int j = i + 1; j < n && a[(size_t)i]; ++j)
                if (std::strcmp(kCmds[j].tab, kCmds[i].tab) == 0 &&
                    std::strcmp(kCmds[j].cmd_id, kCmds[i].cmd_id) == 0)
                    a[(size_t)i] = 0;
        }
        return a;
    }();
    return index >= 0 && index < n && active[(size_t)index];
}

CommandCapability ribbonCommandCapability(const char* cmdId) {
    if (!cmdId || !*cmdId) return CommandCapability::Mutate;
    static const auto capabilities = [] {
        std::unordered_map<std::string_view, CommandCapability> result;
        for (const RibbonCmd& command : kCmds)
            result.emplace(command.cmd_id, command.capability);
        return result;
    }();
    const auto it = capabilities.find(cmdId);
    return it == capabilities.end() ? CommandCapability::Mutate : it->second;
}

// Rows that configure ONE study type (its Study-node options) on a tab shared with other types:
// Buckling and Contact studies use the Static tab, Vibration uses the Modal tab.
struct StudyScopedRow { const char* cmd_id; const char* typeKey; };
static const StudyScopedRow kStudyScopedRows[] = {
    { "sim_buckling_options", "buckling" },
    { "sim_contact_options",  "contact" },
    { "sim_freqresp_options", "freq_response" },
};

// Study commands that act on the SELECTED study (the per-type setup commands, sim_load_heat and the
// sim_topology_* tools pick a study of their own type instead, so they are never gated here).
static const char* const kSelectedStudyCmds[] = {
    "sim_material", "sim_fixture", "sim_load_force", "sim_load_pressure",
    "sim_mesh",     "sim_run",     "sim_results",
};
static bool selectedStudyCommand(const char* cmdId) {
    for (const char* id : kSelectedStudyCmds)
        if (std::strcmp(id, cmdId) == 0) return true;
    return false;
}


static bool cmdIs(const char* cmdId, std::initializer_list<const char*> ids) {
    for (const char* id : ids)
        if (std::strcmp(cmdId, id) == 0) return true;
    return false;
}

RibbonStudyGate ribbonStudyGate(const char* cmdId, const char* studyTypeKey, const char** reason) {
    if (reason) *reason = nullptr;
    if (!cmdId) return RibbonStudyGate::Enabled;
    for (const StudyScopedRow& r : kStudyScopedRows)
        if (std::strcmp(r.cmd_id, cmdId) == 0)
            return studyTypeKey && std::strcmp(studyTypeKey, r.typeKey) == 0
                       ? RibbonStudyGate::Enabled
                       : RibbonStudyGate::Hidden;
    if (!studyTypeKey) return RibbonStudyGate::Enabled;
    if (!selectedStudyCommand(cmdId)) return RibbonStudyGate::Enabled;

    const std::string_view key = studyTypeKey;
    const char* why = nullptr;
    const bool loads = cmdIs(cmdId, {"sim_load_force", "sim_load_pressure"});
    if (key.empty()) {
        why = "Create or select a study first: New Study.";
    } else if (key == "contact") {
        if (loads || cmdIs(cmdId, {"sim_fixture", "sim_mesh"}))
            why = "Contact runs on a two-block proxy sized to the part: it takes no fixtures, "
                  "loads or mesh settings.";
    } else if (key == "modal") {
        if (loads) why = "Modal studies take no loads: they solve the part's natural frequencies.";
    } else if (key == "freq_response") {
        if (loads) why = "Vibration studies are driven by base excitation (Excitation), not by face "
                         "loads.";
    } else if (key == "thermal_fea") {
        if (loads) why = "Thermal FEA studies take heat loads (Heat Load), not forces or pressures.";
    } else if (key == "flow" || key == "thermal") {
        if (cmdIs(cmdId, {"sim_material"}))
            why = "Flow studies use a fluid, not a solid material: set it under Fluid.";
        else if (loads || cmdIs(cmdId, {"sim_fixture"}))
            why = "Flow studies have no structural fixtures or loads: set the flow under Boundaries.";
        else if (cmdIs(cmdId, {"sim_mesh"}))
            why = "The flow grid is set under Resolution.";
    } else if (key == "motion") {
        if (!cmdIs(cmdId, {"sim_run", "sim_results"}))
            why = "Motion studies drive assembly mates: they take no material, fixtures, loads or "
                  "mesh.";
    }
    if (!why) return RibbonStudyGate::Enabled;
    if (reason) *reason = why;
    return RibbonStudyGate::Disabled;
}

static bool startsWith(std::string_view id, std::string_view prefix) {
    return id.size() >= prefix.size() && id.compare(0, prefix.size(), prefix) == 0;
}

static CommandAvailability disabled(const char* reason) {
    return {true, false, reason};
}

// The table is immutable, so resolve duplicated tab rows just once. An id is reserved only
// if every occurrence is reserved; retired reserved commands must not become dispatchable.
// -1 means no ribbon row, 0 supported, 1 reserved.
static int commandReservation(std::string_view id) {
    static const auto reserved = [] {
        std::unordered_map<std::string_view, bool> result;
        for (const auto& row : kCmds) {
            const auto inserted = result.emplace(row.cmd_id, row.stretch);
            if (!inserted.second) inserted.first->second &= row.stretch;
        }
        return result;
    }();
    const auto it = reserved.find(id);
    return it == reserved.end() ? -1 : (it->second ? 1 : 0);
}

CommandAvailability commandAvailability(const char* cmdId, const CommandContext& c) {
    if (!cmdId || !*cmdId) return {};
    const std::string_view id(cmdId);
    const int reservation = commandReservation(id);
    if (reservation == 1) {
        if (id == "sk_spline_surface")
            return disabled("Splines cannot be sketched directly on curved faces yet.");
        if (id == "sim_load_torque")
            return disabled("The structural solver has no torque or moment load.");
        return disabled("Not available in this version.");
    }
#ifdef __EMSCRIPTEN__
    // webGapReason also describes connection-wide CAD readiness. It is a ribbon capability
    // gate, not a blanket prohibition on internal, document-independent navigation.
    if (reservation >= 0)
        if (const char* reason = webGapReason(cmdId)) return disabled(reason);
#endif

    const char* studyReason = nullptr;
    const RibbonStudyGate study = ribbonStudyGate(cmdId, c.selectedStudyType.c_str(), &studyReason);
    if (study == RibbonStudyGate::Hidden) return {false, false, nullptr};
    // Never infer that a study is absent from an uninitialized or stale list. A list being
    // replaced can also invalidate an otherwise selected study before its action executes.
    const bool listDependent = selectedStudyCommand(cmdId) || id == "sim_parameters" ||
        cmdIs(cmdId, {"sim_modal_options", "sim_buckling_options", "sim_contact_options",
                      "sim_freqresp_options", "sim_thermal_fea_options", "sim_load_heat",
                      "sim_thermal_bc", "sim_fatigue_setup", "sim_motion_setup",
                      "sim_fluid", "sim_flow_bc", "sim_flow_res",
                      "sim_topology_setup", "sim_topology_boundaries", "sim_topology_preserve",
                      "sim_topology_exclude", "sim_topology_manufacturing", "sim_topology_compare"});
    if (listDependent) {
        const bool activeMechanical = c.activeMechanicalReady && !c.activeDocId.empty() &&
            (c.activeDocKind == CommandDocKind::Part || c.activeDocKind == CommandDocKind::Assembly);
        if (!activeMechanical) return disabled("Open a part or assembly first.");
        if (c.studyListPending) return disabled("Wait for the study list to finish loading.");
        if (!c.studyFactsCurrent)
            return disabled("Study list is not current. Open Simulation to refresh or retry.");
    }
    if (study == RibbonStudyGate::Disabled) return disabled(studyReason);
    if (id == "sim_parameters" && c.selectedStudyType.empty())
        return disabled("Create or select a study first.");

    // Preserve the existing Simulation model freeze, while leaving library navigation and
    // sketch finish/cancel available. These latter commands are the way out of a live edit.
    if (c.inSimulation &&
        !cmdIs(cmdId, {"asm_parts_library", "sketch_exit", "sketch_cancel",
                       "sketch3d_exit", "sketch3d_cancel"})) {
        const bool allowed = startsWith(id, "sim_") || startsWith(id, "eval_") ||
            startsWith(id, "view_") || startsWith(id, "zoom") || startsWith(id, "cam_") ||
            startsWith(id, "display_") || startsWith(id, "section") ||
            startsWith(id, "rollback") || startsWith(id, "config_") ||
            id == "toggle_units" || id == "make_drawing";
        const bool modeling = startsWith(id, "sketch") || startsWith(id, "sk_") ||
            startsWith(id, "sk3d_") || startsWith(id, "smart_dim") ||
            startsWith(id, "extrude") || startsWith(id, "revolve") ||
            startsWith(id, "fillet") || startsWith(id, "chamfer") ||
            startsWith(id, "shell") || startsWith(id, "draft") || startsWith(id, "hole") ||
            startsWith(id, "sweep") || startsWith(id, "loft") || startsWith(id, "boss") ||
            startsWith(id, "cut") || startsWith(id, "pattern") || startsWith(id, "mirror") ||
            startsWith(id, "rib") || startsWith(id, "dome") || startsWith(id, "wrap") ||
            startsWith(id, "split") || startsWith(id, "combine") || startsWith(id, "scale") ||
            startsWith(id, "move") || startsWith(id, "boolean") || startsWith(id, "helix") ||
            startsWith(id, "ref_") || startsWith(id, "datum") || startsWith(id, "plane") ||
            startsWith(id, "axis") || startsWith(id, "weld") || startsWith(id, "sm_") ||
            startsWith(id, "asm_") || startsWith(id, "edit_") || id == "new_assembly";
        if (modeling && !allowed)
            return disabled("Leave Simulation to edit model geometry.");
    }

    const bool part = c.activeDocKind == CommandDocKind::Part && c.activeMechanicalReady &&
                      !c.activeDocId.empty();
    const bool assembly = c.activeDocKind == CommandDocKind::Assembly && c.activeMechanicalReady &&
                          !c.activeDocId.empty();
    if (id == "eval_model_history" && !part)
        return disabled("Open a part to inspect its model history.");
    if (id == "sketch_new" || id == "sketch3d_new") {
        if (c.sketch2dActive || c.sketch3dActive)
            return disabled("Finish or cancel the current sketch first.");
        if (!part) return disabled("Open a part first.");
    } else if (startsWith(id, "sk3d_") || id == "sketch3d_exit" || id == "sketch3d_cancel") {
        if (!c.sketch3dActive) return disabled("Start a 3D sketch first.");
        if (id == "sk3d_fix" && !c.sketch3dHasFixSelection)
            return disabled("Select sketch entities or points first.");
        if (id == "sk3d_delete" && !c.sketch3dHasSelection)
            return disabled("Select sketch entities first.");
    } else if (startsWith(id, "sk_") || id == "smart_dim" ||
               id == "sketch_exit" || id == "sketch_cancel") {
        const bool finish3d = id == "sketch_exit" && c.sketch3dActive;
        if (!c.sketch2dActive && !finish3d) return disabled("Start a 2D sketch first.");
        if (id == "sk_construction" && !c.sketch2dHasEntitySelection)
            return disabled("Select sketch entities first.");
        if (id == "sk_fix" && !c.sketch2dHasFixSelection)
            return disabled("Select sketch entities or points first.");
    }

    if (startsWith(id, "dwg_")) {
        if (c.activeDocKind != CommandDocKind::Drawing || c.activeDocId.empty())
            return disabled("Open a drawing first.");
        if (startsWith(id, "dwg_scale_") && !c.drawingViewSelected)
            return disabled("Select a drawing view first.");
    }
    if ((cmdIs(cmdId, {"asm_insert", "asm_move", "asm_pattern", "asm_mate", "asm_explode",
                       "asm_interference", "asm_bom", "sim_motion_study", "sim_new_motion"})) &&
        !assembly)
        return disabled("Open an assembly first.");
    if (cmdIs(cmdId, {"sim_new_static", "sim_new_modal", "sim_new_flow", "sim_new_thermal",
                      "sim_new_fatigue", "sim_new_thermal_fea", "sim_new_buckling",
                      "sim_new_contact", "sim_new_freqresp", "sim_piping_new", "sim_hyd_new"}) &&
        !part && !assembly)
        return disabled("Open a part or assembly first.");

    if (id == "sim_new_topology") {
        if (!part) return disabled("Open a part for topology optimization.");
        if (c.modelSolidKnown && !c.modelHasSolid)
            return disabled("Model a solid design domain first.");
        if (c.topologyCapabilityKnown && !c.topologyCapabilityAvailable)
            return disabled("Topology solver capabilities are unavailable.");
    }
    // The picker itself remains browseable (and provides Retry), but each creation action
    // waits for a current list and any preceding creation. Other metadata probes are unrelated.
    if (cmdIs(cmdId, {"sim_new_static", "sim_new_modal", "sim_new_flow", "sim_new_thermal",
                      "sim_new_fatigue", "sim_new_motion", "sim_new_thermal_fea",
                      "sim_new_buckling", "sim_new_contact", "sim_new_freqresp",
                      "sim_new_topology", "sim_motion_study", "sim_piping_new", "sim_hyd_new"})) {
        if (c.studyListPending) return disabled("Wait for the study list to finish loading.");
        if (c.simCreationPending) return disabled("Wait for the current study to finish being created.");
        if (!c.studyFactsCurrent)
            return disabled("Study list is not current. Open Simulation to refresh or retry.");
    }
    if (id == "sim_new_contact" && c.contactCapabilityKnown && !c.contactCapabilityAvailable)
        return disabled("Install CalculiX to create a contact study.");
    if (id == "sim_new_freqresp" && c.vibrationCapabilityKnown && !c.vibrationCapabilityAvailable)
        return disabled("Install the vibration post-processing solver.");
    if (id == "sim_new_thermal" && c.thermalCfdCapabilityKnown && !c.thermalCfdCapabilityAvailable)
        return disabled("Thermal CFD is not supported by the current flow backend; use Thermal FEA.");
    if (id == "sim_em_run") {
        if (!c.emAnalysisOpen) return disabled("Open an EM analysis first.");
        if (!c.emCapabilityKnown)
            return disabled(c.emCapabilityProbeFailed
                                ? "Could not determine EM solver capability; check the connection."
                                : "Waiting for the EM solver capability check.");
        if (!c.emCapabilityAvailable)
            return disabled("Install an electromagnetics solver that supports this analysis.");
    }

    if (id == "sim_modal_options" && !c.hasModalStudy)
        return disabled("Create a modal study first.");
    if (cmdIs(cmdId, {"sim_thermal_fea_options", "sim_load_heat"}) && !c.hasThermalFeaStudy)
        return disabled("Create a Thermal FEA study first.");
    if (id == "sim_thermal_bc" && !c.hasThermalFeaStudy && !c.hasThermalStudy)
        return disabled("Create a thermal study first.");
    if (id == "sim_fatigue_setup" && !c.hasFatigueStudy)
        return disabled("Create a fatigue study first.");
    if (id == "sim_motion_setup" && !c.hasMotionStudy)
        return disabled("Create a motion study first.");
    if (cmdIs(cmdId, {"sim_fluid", "sim_flow_bc", "sim_flow_res"}) &&
        !c.hasFlowStudy && !c.hasThermalStudy)
        return disabled("Create a flow or thermal CFD study first.");
    if (cmdIs(cmdId, {"sim_topology_setup", "sim_topology_boundaries", "sim_topology_preserve",
                      "sim_topology_exclude", "sim_topology_manufacturing", "sim_topology_compare"}) &&
        !c.hasTopologyStudy)
        return disabled("Create a topology study first.");
    if (c.selectedStudyType == "topology" && cmdIs(cmdId, {"sim_load_force", "sim_load_pressure"}) &&
        c.topologyPhysics != "structural" && c.topologyPhysics != "buckling" &&
        c.topologyPhysics != "thermomechanical")
        return disabled(c.topologyPhysics.empty()
                            ? "Choose the topology physics first."
                            : "Use Heat / Flow boundaries for this physics.");

    const bool ecadDoc = (c.activeDocKind == CommandDocKind::Schematic ||
                          c.activeDocKind == CommandDocKind::PCB) && !c.activeDocId.empty();
    if (id == "pcb_update_from_sch") {
        if (!ecadDoc || !c.hasOpenSchematic || !c.hasOpenBoard)
            return disabled("Open a schematic and board from the same project first.");
    } else if (cmdIs(cmdId, {"ecad_run_erc", "ecad_simulate", "sch_export_netlist",
                             "sch_export_pdf", "ecad_kicad_open_sch"})) {
        if (c.activeDocKind != CommandDocKind::Schematic || c.activeDocId.empty())
            return disabled("Open an active schematic first.");
    } else if (cmdIs(cmdId, {"ecad_run_drc", "ecad_autoroute", "ecad_fill_zones",
                             "ecad_to_3d", "ecad_export_fab", "ecad_export_step",
                             "ecad_kicad_open_pcb"})) {
        if (c.activeDocKind != CommandDocKind::PCB || c.activeDocId.empty())
            return disabled("Open an active board first.");
    } else if (cmdIs(cmdId, {"ecad_save", "ecad_save_as", "ecad_reload", "ecad_export_bom", "ecad_kicad_open_project"}) &&
               !ecadDoc) {
        return disabled("Open an active schematic or board first.");
    }
    if (id == "ecad_reload" && !c.ecadHasPath)
        return disabled("Save or open a KiCad file first.");
    if (id == "ecad_export_fab" && !c.ecadHasPath)
        return disabled("Save the board in a KiCad project first.");
    if (id == "ecad_kicad_open_project" && !c.ecadHasProject)
        return disabled("Open or save a KiCad project first.");
    if (cmdIs(cmdId, {"ecad_run_erc", "ecad_run_drc", "ecad_export_bom",
                      "sch_export_netlist", "sch_export_pdf", "ecad_export_fab",
                      "ecad_export_step", "ecad_to_3d", "ecad_kicad_open_sch",
                      "ecad_kicad_open_pcb", "ecad_kicad_open_project"})) {
        if (!c.kicadStatusKnown) return disabled("Checking KiCad installation.");
        if (!c.kicadAvailable) return disabled("Install KiCad 10 to use this action.");
    }
    if ((id == "ecad_kicad_open_sch" && !c.kicadSchematicAvailable) ||
        (id == "ecad_kicad_open_pcb" && !c.kicadPcbAvailable) ||
        (id == "ecad_kicad_open_project" && !c.kicadProjectAvailable))
        return disabled("The KiCad application for this action was not found.");

    const bool camOperation = startsWith(id, "cam_op_");
    const bool camOutput = cmdIs(cmdId, {"cam_generate", "cam_simulate", "cam_post"});
    const bool camStock = id == "cam_set_stock";
    const bool camExtract = id == "cam_extract";
    const bool camFlavor = id == "cam_flavor";
    const bool camMutation = camOperation || camOutput || camStock || camExtract || camFlavor ||
                             id == "cam_new_job" || id == "cam_add_setup" || id == "cam_add_tool";
    if (camMutation && c.camBusy) return disabled("Wait for the current CAM operation to finish.");
    if ((camOutput || camFlavor) && !c.camHasJob) return disabled("Create a CAM job first.");
    if ((camOperation || camStock) && !c.camHasSetup) return disabled("Add a setup first.");
    if ((camExtract || camStock || camOperation) && !c.camHasSource &&
        !(id == "cam_op_face" && c.camHasCachedExtract))
        return disabled("Open the CAM source part first.");

    if (id == "sm_flat_pattern" &&
        (!part || (!c.sheetMetalHasBase && !c.sheetMetalBaseStatusUnknown) ||
         (!c.sheetMetalHasBends && !c.sheetMetalBendStatusUnknown)))
        return disabled("Add a base flange and bend first.");
    if (id == "sm_export_flat_dxf" &&
        (!part || (!c.sheetMetalHasValidFlat && !c.sheetMetalFlatStatusUnknown)))
        return disabled("Create a flat pattern first.");
    return {};
}

}  // namespace ui
