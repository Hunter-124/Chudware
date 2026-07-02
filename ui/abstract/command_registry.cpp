#include "command_registry.h"
#include <cstring>
#include <unordered_set>
#include <string>

namespace cmd {

using W = Workspace;
using G = Group;
using K = Kind;

// ============================================================================
//  THE TABLE. Authored in display order: groups appear in the order their first
//  command appears here. Every existing engine cmd_id has exactly one home
//  (variants/flyouts collapse the SolidWorks button-sprawl). See DESIGN.md §3.1.
// ============================================================================
static const Command T[] = {
// id                     label              icon              ws            group        kind         parent             keywords
// ---- MODEL ----------------------------------------------------------------
{ "sketch_new",           "New Sketch",      "ws_sketch",      W::Model,    G::Sketch,    K::Top,      nullptr,           "draw 2d profile" },

{ "prim_box",             "Box",             "prim_box",       W::Model,    G::Create,    K::Top,      nullptr,           "primitive block cube cuboid" },
{ "prim_cylinder",        "Cylinder",        "prim_cylinder",  W::Model,    G::Create,    K::Flyout,   "prim_box",        "primitive rod disc" },
{ "prim_sphere",          "Sphere",          "prim_sphere",    W::Model,    G::Create,    K::Flyout,   "prim_box",        "primitive ball" },
{ "prim_cone",            "Cone",            "prim_cone",      W::Model,    G::Create,    K::Flyout,   "prim_box",        "primitive taper frustum" },

{ "extrude_boss",         "Extrude",         "extrude",        W::Model,    G::Create,    K::Top,      nullptr,           "boss pad protrusion" },
{ "extrude_cut",          "Extrude Cut",     "subtract",       W::Model,    G::Create,    K::Variant,  "extrude_boss",    "pocket remove" },
{ "revolve_boss",         "Revolve",         "revolve",        W::Model,    G::Create,    K::Top,      nullptr,           "lathe turn" },
{ "revolve_cut",          "Revolve Cut",     "subtract",       W::Model,    G::Create,    K::Variant,  "revolve_boss",    "groove" },
{ "sweep_boss",           "Sweep",           "sweep",          W::Model,    G::Create,    K::Top,      nullptr,           "profile path" },
{ "sweep_cut",            "Sweep Cut",       "subtract",       W::Model,    G::Create,    K::Variant,  "sweep_boss",      nullptr },
{ "loft_boss",            "Loft",            "loft",           W::Model,    G::Create,    K::Top,      nullptr,           "blend sections" },
{ "loft_cut",             "Loft Cut",        "subtract",       W::Model,    G::Create,    K::Variant,  "loft_boss",       nullptr },
{ "rib",                  "Rib",             "rib",            W::Model,    G::Create,    K::Top,      nullptr,           "web stiffener" },
{ "hole_wizard",          "Hole",            "hole",           W::Model,    G::Create,    K::Top,      nullptr,           "drill bore counterbore tap" },
{ "curve_helix",          "Coil",            "coil",           W::Model,    G::Create,    K::Top,      nullptr,           "helix spring thread" },

{ "fillet",               "Fillet",          "fillet",         W::Model,    G::Modify,    K::Top,      nullptr,           "round edge blend" },
{ "chamfer",              "Chamfer",         "chamfer",        W::Model,    G::Modify,    K::Top,      nullptr,           "bevel edge" },
{ "shell",                "Shell",           "shell",          W::Model,    G::Modify,    K::Top,      nullptr,           "hollow thin wall" },
{ "draft",                "Draft",           "draft",          W::Model,    G::Modify,    K::Top,      nullptr,           "taper mold angle" },
{ "split",                "Split",           "split",          W::Model,    G::Modify,    K::Top,      nullptr,           "divide body" },
{ "boolean_combine",      "Combine",         "combine",        W::Model,    G::Modify,    K::Top,      nullptr,           "boolean union add" },
{ "boolean_subtract",     "Subtract",        "subtract",       W::Model,    G::Modify,    K::Variant,  "boolean_combine", "boolean difference" },
{ "boolean_intersect",    "Intersect",       "intersect",      W::Model,    G::Modify,    K::Variant,  "boolean_combine", "boolean common" },

{ "linear_pattern",       "Rect. Pattern",   "pattern_linear", W::Model,    G::Pattern,   K::Top,      nullptr,           "array grid repeat" },
{ "circular_pattern",     "Circ. Pattern",   "pattern_circular",W::Model,   G::Pattern,   K::Top,      nullptr,           "radial array repeat" },
{ "mirror_feature",       "Mirror",          "mirror",         W::Model,    G::Pattern,   K::Top,      nullptr,           "reflect symmetry" },

{ "ref_plane",            "Plane",           "ref_plane",      W::Model,    G::Construct,  K::Top,     nullptr,           "datum reference" },
{ "ref_axis",             "Axis",            "ref_axis",       W::Model,    G::Construct,  K::Top,     nullptr,           "datum line" },
{ "ref_point",            "Point",           "ref_point",      W::Model,    G::Construct,  K::Top,     nullptr,           "datum vertex" },
{ "ref_csys",             "Coord Sys",       "ref_csys",       W::Model,    G::Construct,  K::Top,     nullptr,           "csys triad origin" },

{ "eval_measure",         "Measure",         "measure",        W::Model,    G::Inspect,   K::Top,      nullptr,           "distance angle dimension" },
{ "eval_mass_props",      "Mass Props",      "mass_props",     W::Model,    G::Inspect,   K::Top,      nullptr,           "volume density center" },
{ "eval_section_props",   "Section Props",   "section_props",  W::Model,    G::Inspect,   K::Flyout,   "eval_mass_props", "moment inertia" },
{ "eval_thickness",       "Thickness",       "analyze_thickness",W::Model,  G::Inspect,   K::Top,      nullptr,           "wall min" },
{ "eval_draft",           "Draft Anlys",     "analyze_draft",  W::Model,    G::Inspect,   K::Top,      nullptr,           "draft angle mold" },
{ "eval_curvature",       "Curvature",       "analyze_curvature",W::Model,  G::Inspect,   K::Flyout,   "eval_draft",      "surface" },
{ "eval_zebra",           "Zebra",           "analyze_curvature",W::Model,  G::Inspect,   K::Flyout,   "eval_draft",      "surface continuity" },
{ "eval_symmetry",        "Symmetry",        "analyze_curvature",W::Model,  G::Inspect,   K::Flyout,   "eval_draft",      "mirror" },
{ "eval_check",           "Check",           "check",          W::Model,    G::Inspect,   K::Top,      nullptr,           "geometry validity" },
{ "eval_interference",    "Interference",    "interference",   W::Model,    G::Inspect,   K::Flyout,   "eval_check",      "clash" },
{ "eval_geometry",        "Geometry Anlys",  "check",          W::Model,    G::Inspect,   K::Flyout,   "eval_check",      nullptr },
{ "eval_feature_overlap",  "Feature Overlap", "interference",   W::Model,    G::Inspect,   K::Flyout,   "eval_check",      "feature overlap boss cut swallowed clash" },
{ "prof_toggle",          "Profiler",        "results",        W::Model,    G::Inspect,   K::Top,      nullptr,           "performance sampling" },

{ "ai_reverse_engineer",  "Reverse Engineer","ai_design",      W::Model,    G::AI,        K::Top,      nullptr,           "reverse engineer import parametric rebuild editable" },
{ "ai_explain_part",      "Explain Part",    "ai_design",      W::Model,    G::AI,        K::Top,      nullptr,           "describe explain geometry text" },

{ "appearance_edit",      "Appearance",      "material",       W::Model,    G::Appearance, K::Top,    nullptr,           "color material finish texture realview render swatch" },
{ "view_style",           "Display",         "section_props",  W::Model,    G::Appearance, K::Top,    nullptr,           "shaded wireframe edges display style hidden" },
{ "view_style_shaded",    "Shaded",          "section_props",  W::Model,    G::Appearance, K::Flyout, "view_style",      nullptr },
{ "view_style_edges",     "Shaded + Edges",  "section_props",  W::Model,    G::Appearance, K::Flyout, "view_style",      nullptr },
{ "view_style_wire",      "Wireframe",       "section_props",  W::Model,    G::Appearance, K::Flyout, "view_style",      nullptr },

{ "make_drawing",         "Make Drawing",    "dwg_view",       W::Model,    G::Output,    K::Top,      nullptr,           "2d sheet draft" },

// ---- SKETCH (contextual) --------------------------------------------------
{ "sk_line",              "Line",            "sk_line",        W::Sketch,   G::Draw,      K::Top,      nullptr,           "segment polyline" },
{ "sk_rect_corner",       "Rectangle",       "sk_rect",        W::Sketch,   G::Draw,      K::Top,      nullptr,           "box" },
{ "sk_rect_center",       "Center Rect",     "sk_rect",        W::Sketch,   G::Draw,      K::Flyout,   "sk_rect_corner",  nullptr },
{ "sk_rect_3pt_corner",   "3-Pt Corner Rect","sk_rect",        W::Sketch,   G::Draw,      K::Flyout,   "sk_rect_corner",  nullptr },
{ "sk_rect_3pt_center",   "3-Pt Center Rect","sk_rect",        W::Sketch,   G::Draw,      K::Flyout,   "sk_rect_corner",  nullptr },
{ "sk_parallelogram",     "Parallelogram",   "sk_rect",        W::Sketch,   G::Draw,      K::Flyout,   "sk_rect_corner",  nullptr },
{ "sk_circle",            "Circle",          "sk_circle",      W::Sketch,   G::Draw,      K::Top,      nullptr,           nullptr },
{ "sk_circle_perimeter",  "Perimeter Circle","sk_circle",      W::Sketch,   G::Draw,      K::Flyout,   "sk_circle",       nullptr },
{ "sk_arc_3pt",           "Arc",             "sk_arc",         W::Sketch,   G::Draw,      K::Top,      nullptr,           "3 point" },
{ "sk_arc_center",        "Center Arc",      "sk_arc",         W::Sketch,   G::Draw,      K::Flyout,   "sk_arc_3pt",      nullptr },
{ "sk_arc_tangent",       "Tangent Arc",     "sk_arc",         W::Sketch,   G::Draw,      K::Flyout,   "sk_arc_3pt",      nullptr },
{ "sk_polygon",           "Polygon",         "sk_polygon",     W::Sketch,   G::Draw,      K::Top,      nullptr,           "hexagon ngon" },
{ "sk_slot",              "Slot",            "sk_slot",        W::Sketch,   G::Draw,      K::Top,      nullptr,           "capsule" },
{ "sk_slot_ctr_str",      "Center Slot",     "sk_slot",        W::Sketch,   G::Draw,      K::Flyout,   "sk_slot",         nullptr },
{ "sk_slot_arc_3pt",      "3-Pt Arc Slot",   "sk_slot",        W::Sketch,   G::Draw,      K::Flyout,   "sk_slot",         nullptr },
{ "sk_slot_arc_ctr",      "Center Arc Slot", "sk_slot",        W::Sketch,   G::Draw,      K::Flyout,   "sk_slot",         nullptr },
{ "sk_spline",            "Spline",          "sk_spline",      W::Sketch,   G::Draw,      K::Top,      nullptr,           "curve bezier" },
{ "sk_spline_style",      "Style Spline",    "sk_spline",      W::Sketch,   G::Draw,      K::Flyout,   "sk_spline",       nullptr },
{ "sk_spline_equation",   "Equation Curve",  "sk_spline",      W::Sketch,   G::Draw,      K::Flyout,   "sk_spline",       nullptr },
{ "sk_airfoil",           "Airfoil",         "sk_spline",      W::Sketch,   G::Draw,      K::Flyout,   "sk_spline",       "naca wing" },
{ "sk_spline_surface",    "Spline on Surface","sk_spline",     W::Sketch,   G::Draw,      K::Flyout,   "sk_spline",       nullptr },
{ "sk_ellipse_big",       "Ellipse",         "sk_ellipse",     W::Sketch,   G::Draw,      K::Top,      nullptr,           "oval conic" },
{ "sk_ellipse",           "Ellipse",         "sk_ellipse",     W::Sketch,   G::Draw,      K::Variant,  "sk_ellipse_big",  nullptr },
{ "sk_ellipse_partial",   "Partial Ellipse", "sk_ellipse",     W::Sketch,   G::Draw,      K::Flyout,   "sk_ellipse_big",  nullptr },
{ "sk_parabola",          "Parabola",        "sk_ellipse",     W::Sketch,   G::Draw,      K::Flyout,   "sk_ellipse_big",  "conic" },
{ "sk_conic",             "Conic",           "sk_ellipse",     W::Sketch,   G::Draw,      K::Flyout,   "sk_ellipse_big",  nullptr },
{ "sk_point",             "Point",           "sk_point",       W::Sketch,   G::Draw,      K::Top,      nullptr,           nullptr },
{ "sk_text",              "Text",            "sk_text",        W::Sketch,   G::Draw,      K::Top,      nullptr,           "label" },

{ "smart_dim",            "Smart Dimension", "smart_dim",      W::Sketch,   G::Dimension, K::Top,      nullptr,           "constrain measure" },

{ "sk_trim",              "Trim",            "sk_trim",        W::Sketch,   G::Modify,    K::Top,      nullptr,           "cut entity" },
{ "sk_extend",            "Extend",          "sk_extend",      W::Sketch,   G::Modify,    K::Top,      nullptr,           nullptr },
{ "sk_offset_ent",        "Offset",          "sk_offset",      W::Sketch,   G::Modify,    K::Top,      nullptr,           "parallel" },
{ "sk_fillet",            "Fillet",          "fillet",         W::Sketch,   G::Modify,    K::Top,      nullptr,           "round corner" },
{ "sk_chamfer",           "Chamfer",         "chamfer",        W::Sketch,   G::Modify,    K::Flyout,   "sk_fillet",       "bevel corner" },
{ "sk_convert_ent",       "Convert",         "sk_convert",     W::Sketch,   G::Modify,    K::Top,      nullptr,           "project edge" },
{ "sk_mirror",            "Mirror",          "mirror",         W::Sketch,   G::Modify,    K::Top,      nullptr,           "reflect" },
{ "sk_lin_pattern",       "Pattern",         "pattern_linear", W::Sketch,   G::Modify,    K::Top,      nullptr,           "array repeat" },
{ "sk_circ_pattern",      "Circular Pattern","pattern_circular",W::Sketch,  G::Modify,    K::Flyout,   "sk_lin_pattern",  nullptr },
{ "sk_move",              "Move / Copy",     "move_component", W::Sketch,   G::Modify,    K::Top,      nullptr,           "translate" },

{ "sk_relations",         "Add Relation",    "relations",      W::Sketch,   G::Relations, K::Top,      nullptr,           "constraint coincident parallel" },
{ "sk_fix",               "Fix",             "fix",            W::Sketch,   G::Relations, K::Flyout,   "sk_relations",    "ground anchor" },
{ "sk_construction",      "Construction",    "sk_construction",W::Sketch,   G::Relations, K::Top,      nullptr,           "reference geometry" },

{ "sketch_exit",          "Exit Sketch",     "ok_check",       W::Sketch,   G::Finish,    K::Top,      nullptr,           "done close" },

// ---- SHEET METAL ----------------------------------------------------------
{ "sm_base_flange",       "Base Flange",     "ws_sheetmetal",  W::SheetMetal,G::Create,   K::Top,      nullptr,           "tab sheet" },
{ "sm_edge_flange",       "Edge Flange",     "shell",          W::SheetMetal,G::Create,   K::Top,      nullptr,           "bend wall" },
{ "sm_hem",               "Hem",             "shell",          W::SheetMetal,G::Create,   K::Top,      nullptr,           "fold edge" },
{ "sm_jog",               "Jog",             "extrude",        W::SheetMetal,G::Create,   K::Top,      nullptr,           "offset step" },
{ "sm_flat_pattern",      "Flatten",         "shell",          W::SheetMetal,G::Modify,   K::Top,      nullptr,           "unfold flat pattern" },
{ "sm_export_flat_dxf",   "Export Flat DXF", "gerber",         W::SheetMetal,G::Output,   K::Top,      nullptr,           "laser cut" },
{ "appearance_edit",      "Appearance",      "material",       W::SheetMetal,G::Appearance, K::Top,    nullptr,           "color material finish texture realview render swatch" },
{ "view_style",           "Display",         "section_props",  W::SheetMetal,G::Appearance, K::Top,    nullptr,           "shaded wireframe edges display style hidden" },

// ---- WELDMENT -------------------------------------------------------------
{ "weld_member",          "Structural Member","ws_weldment",   W::Weldment, G::Create,    K::Top,      nullptr,           "beam profile frame" },
{ "weld_gusset",          "Gusset",          "shell",          W::Weldment, G::Modify,    K::Top,      nullptr,           "corner plate" },
{ "weld_end_cap",         "End Cap",         "fillet",         W::Weldment, G::Modify,    K::Top,      nullptr,           "tube close" },
{ "appearance_edit",      "Appearance",      "material",       W::Weldment, G::Appearance, K::Top,    nullptr,           "color material finish texture realview render swatch" },
{ "view_style",           "Display",         "section_props",  W::Weldment, G::Appearance, K::Top,    nullptr,           "shaded wireframe edges display style hidden" },

// ---- SURFACE --------------------------------------------------------------
{ "surf_extrude",         "Extrude",         "extrude",        W::Surface,  G::Create,    K::Top,      nullptr,           "surface" },
{ "surf_revolve",         "Revolve",         "revolve",        W::Surface,  G::Create,    K::Top,      nullptr,           "surface" },
{ "surf_sweep",           "Sweep",           "sweep",          W::Surface,  G::Create,    K::Top,      nullptr,           "surface" },
{ "surf_loft",            "Loft",            "loft",           W::Surface,  G::Create,    K::Top,      nullptr,           "surface" },
{ "surf_planar",          "Planar",          "ref_plane",      W::Surface,  G::Create,    K::Top,      nullptr,           "fill region" },
{ "surf_offset",          "Offset",          "sk_offset",      W::Surface,  G::Modify,    K::Top,      nullptr,           nullptr },
{ "surf_ruled",           "Ruled",           "shell",          W::Surface,  G::Modify,    K::Top,      nullptr,           nullptr },
{ "surf_boundary",        "Boundary",        "loft",           W::Surface,  G::Modify,    K::Top,      nullptr,           "patch" },
{ "surf_fill",            "Fill",            "loft",           W::Surface,  G::Modify,    K::Flyout,   "surf_boundary",   nullptr },
{ "surf_knit",            "Knit",            "combine",        W::Surface,  G::Modify,    K::Top,      nullptr,           "sew stitch" },
{ "surf_thicken",         "Thicken",         "extrude",        W::Surface,  G::Modify,    K::Top,      nullptr,           "solidify" },
{ "surf_trim",            "Trim",            "sk_trim",        W::Surface,  G::Modify,    K::Top,      nullptr,           nullptr },
{ "surf_untrim",          "Untrim",          "sk_convert",     W::Surface,  G::Modify,    K::Flyout,   "surf_trim",       nullptr },
{ "surf_delete_face",     "Delete Face",     "subtract",       W::Surface,  G::Modify,    K::Top,      nullptr,           nullptr },
{ "surf_replace_face",    "Replace Face",    "draft",          W::Surface,  G::Modify,    K::Flyout,   "surf_delete_face",nullptr },
{ "appearance_edit",      "Appearance",      "material",       W::Surface,  G::Appearance, K::Top,    nullptr,           "color material finish texture realview render swatch" },
{ "view_style",           "Display",         "section_props",  W::Surface,  G::Appearance, K::Top,    nullptr,           "shaded wireframe edges display style hidden" },

// ---- SIMULATE -------------------------------------------------------------
{ "sim_new_static",       "New Study",       "sim_static",     W::Simulate, G::Study,     K::Top,      nullptr,           "fea stress" },
{ "sim_new_modal",        "Modal Study",     "sim_modal",      W::Simulate, G::Study,     K::Flyout,   "sim_new_static",  "frequency vibration" },
{ "sim_new_flow",         "Flow Study",      "sim_flow",       W::Simulate, G::Study,     K::Flyout,   "sim_new_static",  "cfd aero" },
{ "sim_new_thermal",      "Thermal Study",   "sim_thermal",    W::Simulate, G::Study,     K::Flyout,   "sim_new_static",  "heat" },
{ "sim_new_fatigue",      "Fatigue Study",   "sim_fatigue",    W::Simulate, G::Study,     K::Flyout,   "sim_new_static",  "cyclic life" },
{ "sim_material",         "Material",        "material",       W::Simulate, G::Setup,     K::Top,      nullptr,           "assign" },
{ "sim_fixture",          "Fixtures",        "fixture",        W::Simulate, G::Setup,     K::Top,      nullptr,           "restraint support" },
{ "sim_load_force",       "Loads",           "load_force",     W::Simulate, G::Setup,     K::Top,      nullptr,           "force" },
{ "sim_load_pressure",    "Pressure",        "load_pressure",  W::Simulate, G::Setup,     K::Flyout,   "sim_load_force",  nullptr },
{ "sim_mesh",             "Mesh",            "mesh",           W::Simulate, G::Setup,     K::Top,      nullptr,           "discretize" },
{ "sim_run",              "Run",             "run_study",      W::Simulate, G::Run,       K::Top,      nullptr,           "solve" },
{ "sim_results",          "Results",         "results",        W::Simulate, G::Run,       K::Top,      nullptr,           "plot" },

// ---- ASSEMBLE -------------------------------------------------------------
{ "asm_insert",           "Insert Component","insert_component",W::Assemble, G::Insert,    K::Top,      nullptr,           "add part" },
{ "asm_parts_library",    "Standard Parts",  "std_parts",      W::Assemble, G::Insert,    K::Flyout,   "asm_insert",      "fastener toolbox" },
{ "asm_move",             "Move",            "move_component", W::Assemble, G::Position,  K::Top,      nullptr,           "drag rotate" },
{ "asm_mate",             "Mate",            "mate",           W::Assemble, G::Position,  K::Top,      nullptr,           "constrain align" },
{ "asm_pattern",          "Component Pattern","pattern_component",W::Assemble,G::Pattern,  K::Top,      nullptr,           "array repeat" },
{ "asm_interference",     "Interference",    "interference",   W::Assemble, G::Inspect,   K::Top,      nullptr,           "clash" },
{ "asm_bom",              "BOM",             "bom",            W::Assemble, G::Inspect,   K::Top,      nullptr,           "bill materials" },
{ "asm_explode",          "Exploded View",   "exploded_view",  W::Assemble, G::Display,   K::Top,      nullptr,           nullptr },

// ---- DRAFTING -------------------------------------------------------------
{ "dwg_view_model",       "Model View",      "dwg_view",       W::Drafting, G::Views,     K::Top,      nullptr,           nullptr },
{ "dwg_view_proj",        "Projected",       "dwg_projected",  W::Drafting, G::Views,     K::Top,      nullptr,           nullptr },
{ "dwg_view_std3",        "Standard 3",      "dwg_view",       W::Drafting, G::Views,     K::Top,      nullptr,           "three" },
{ "dwg_view_section",     "Section",         "dwg_section",    W::Drafting, G::Views,     K::Top,      nullptr,           "cut" },
{ "dwg_section_half",     "Half Section",    "dwg_section",    W::Drafting, G::Views,     K::Flyout,   "dwg_view_section",nullptr },
{ "dwg_section_reverse",  "Flip Side",       "dwg_section",    W::Drafting, G::Views,     K::Flyout,   "dwg_view_section",nullptr },
{ "dwg_view_detail",      "Detail",          "dwg_detail",     W::Drafting, G::Views,     K::Top,      nullptr,           "zoom" },
{ "dwg_smart_dim",        "Smart Dim",       "smart_dim",      W::Drafting, G::Dimension, K::Top,      nullptr,           nullptr },
{ "dwg_model_items",      "Model Items",     "relations",      W::Drafting, G::Dimension, K::Top,      nullptr,           "import dims" },
{ "dwg_hole_callout",     "Hole Callout",    "hole",           W::Drafting, G::Dimension, K::Top,      nullptr,           nullptr },
{ "dwg_gdt",              "Geo Tol",         "gdt",            W::Drafting, G::Annotate,  K::Top,      nullptr,           "gd&t tolerance" },
{ "dwg_datum",            "Datum",           "datum",          W::Drafting, G::Annotate,  K::Flyout,   "dwg_gdt",         nullptr },
{ "dwg_datum_target",     "Datum Target",    "datum",          W::Drafting, G::Annotate,  K::Flyout,   "dwg_gdt",         nullptr },
{ "dwg_surface_finish",   "Surface Finish",  "gdt",            W::Drafting, G::Annotate,  K::Flyout,   "dwg_gdt",         nullptr },
{ "dwg_weld",             "Weld",            "relations",      W::Drafting, G::Annotate,  K::Flyout,   "dwg_gdt",         nullptr },
{ "dwg_center_mark",      "Center Mark",     "sk_point",       W::Drafting, G::Annotate,  K::Top,      nullptr,           nullptr },
{ "dwg_center_line",      "Center Line",     "sk_construction",W::Drafting,  G::Annotate, K::Flyout,   "dwg_center_mark", nullptr },
{ "dwg_note",             "Note",            "note",           W::Drafting, G::Annotate,  K::Top,      nullptr,           "text" },
{ "dwg_balloon",          "Balloon",         "balloon",        W::Drafting, G::Annotate,  K::Top,      nullptr,           "callout" },
{ "dwg_auto_balloon",     "Auto Balloon",    "balloon",        W::Drafting, G::Annotate,  K::Flyout,   "dwg_balloon",     nullptr },
{ "dwg_title_block",      "Title Block",     "title_block",    W::Drafting, G::Tables,    K::Top,      nullptr,           nullptr },
{ "dwg_bom_table",        "Bill of Materials","bom",           W::Drafting, G::Tables,    K::Top,      nullptr,           "bom" },
{ "dwg_sheet_props",      "Sheet Props",     "settings",       W::Drafting, G::Sheet,     K::Top,      nullptr,           nullptr },
{ "dwg_rebuild",          "Rebuild",         "refresh",        W::Drafting, G::Sheet,     K::Top,      nullptr,           nullptr },
{ "dwg_fit",              "Fit",             "zoom_fit",       W::Drafting, G::Sheet,     K::Top,      nullptr,           nullptr },
{ "dwg_scale_1_1",        "Scale 1:1",       "zoom_fit",       W::Drafting, G::Sheet,     K::Top,      nullptr,           nullptr },
{ "dwg_scale_1_2",        "Scale 1:2",       "zoom_area",      W::Drafting, G::Sheet,     K::Flyout,   "dwg_scale_1_1",   nullptr },
{ "dwg_scale_2_1",        "Scale 2:1",       "zoom_area",      W::Drafting, G::Sheet,     K::Flyout,   "dwg_scale_1_1",   nullptr },
{ "dwg_export_dxf",       "Export DXF",      "gerber",         W::Drafting, G::Output,    K::Top,      nullptr,           nullptr },
{ "dwg_export_pdf",       "Export PDF",      "save",           W::Drafting, G::Output,    K::Top,      nullptr,           nullptr },

// ---- SCHEMATIC (ECAD) -----------------------------------------------------
{ "ecad_new_sch",         "New Schematic",   "ws_schematic",   W::Schematic, G::New,      K::Top,      nullptr,           nullptr },
{ "ecad_open",            "Open",            "folder",         W::Schematic, G::New,      K::Top,      nullptr,           nullptr },
{ "ecad_save",            "Save",            "save",           W::Schematic, G::New,      K::Top,      nullptr,           nullptr },
{ "sch_add_symbol",       "Place Symbol",    "sch_symbol",     W::Schematic, G::Place,    K::Top,      nullptr,           "component part" },
{ "sch_add_wire",         "Wire",            "sch_wire",       W::Schematic, G::Place,    K::Top,      nullptr,           "net connect" },
{ "sch_add_label",        "Net Label",       "net_label",      W::Schematic, G::Place,    K::Top,      nullptr,           nullptr },
{ "sch_add_power",        "Power Port",      "power_port",     W::Schematic, G::Place,    K::Top,      nullptr,           "vcc gnd" },
{ "sch_annotate",         "Annotate",        "ok_check",       W::Schematic, G::Verify,   K::Top,      nullptr,           "reference designators" },
{ "ecad_run_erc",         "Run ERC",         "drc",            W::Schematic, G::Verify,   K::Top,      nullptr,           "electrical rule check" },
{ "ecad_ai_design",       "AI Design",       "ai_design",      W::Schematic, G::AI,       K::Top,      nullptr,           "generate circuit" },

// ---- BOARD (ECAD) ---------------------------------------------------------
{ "ecad_new_pcb",         "New PCB",         "ws_board",       W::Board,    G::New,       K::Top,      nullptr,           "layout" },
{ "pcb_update_from_sch",  "Update from Sch", "refresh",        W::Board,    G::New,       K::Top,      nullptr,           "netlist" },
{ "pcb_add_footprint",    "Footprint",       "sch_symbol",     W::Board,    G::Place,     K::Top,      nullptr,           "land pattern" },
{ "pcb_add_track",        "Route",           "pcb_route",      W::Board,    G::Place,     K::Top,      nullptr,           "trace copper" },
{ "pcb_add_via",          "Via",             "via",            W::Board,    G::Place,     K::Top,      nullptr,           nullptr },
{ "pcb_add_zone",         "Copper Zone",     "copper_zone",    W::Board,    G::Place,     K::Top,      nullptr,           "pour fill plane" },
{ "ecad_autoroute",       "Autoroute",       "autoroute",      W::Board,    G::Auto,      K::Top,      nullptr,           nullptr },
{ "ecad_fill_zones",      "Fill Zones",      "copper_zone",    W::Board,    G::Auto,      K::Top,      nullptr,           "pour" },
{ "ecad_run_drc",         "Run DRC",         "drc",            W::Board,    G::Auto,      K::Top,      nullptr,           "design rule check" },
{ "ecad_simulate",        "Simulate",        "sim_flow",       W::Board,    G::Analyze,   K::Top,      nullptr,           "spice" },
{ "ecad_to_3d",           "Board to 3D",     "board_3d",       W::Board,    G::Output,    K::Top,      nullptr,           "model" },
{ "ecad_export_gerber",   "Gerbers",         "gerber",         W::Board,    G::Output,    K::Top,      nullptr,           "fabrication" },
{ "ecad_export_drill",    "Drill",           "hole",           W::Board,    G::Output,    K::Top,      nullptr,           "nc" },
{ "ecad_export_bom",      "BOM",             "bom",            W::Board,    G::Output,    K::Top,      nullptr,           nullptr },
};

static const int N = (int)(sizeof(T)/sizeof(T[0]));

const std::vector<Command>& all() {
    static std::vector<Command> v(T, T + N);
    return v;
}

const char* workspaceLabel(Workspace w) {
    switch (w) {
        case W::Model: return "Model";          case W::Sketch: return "Sketch";
        case W::SheetMetal: return "Sheet Metal";case W::Weldment: return "Weldment";
        case W::Surface: return "Surface";       case W::Assemble: return "Assemble";
        case W::Drafting: return "Drafting";     case W::Simulate: return "Simulate";
        case W::Schematic: return "Schematic";   case W::Board: return "Board";
        default: return "?";
    }
}
const char* workspaceIcon(Workspace w) {
    switch (w) {
        case W::Model: return "ws_model";        case W::Sketch: return "ws_sketch";
        case W::SheetMetal: return "ws_sheetmetal";case W::Weldment: return "ws_weldment";
        case W::Surface: return "ws_surface";    case W::Assemble: return "ws_assemble";
        case W::Drafting: return "ws_draft";     case W::Simulate: return "ws_simulate";
        case W::Schematic: return "ws_schematic";case W::Board: return "ws_board";
        default: return "ws_model";
    }
}
const char* groupLabel(Group g) {
    switch (g) {
        case G::Sketch: return "Sketch";   case G::Create: return "Create";
        case G::Modify: return "Modify";   case G::Pattern: return "Pattern";
        case G::Construct: return "Construct"; case G::Inspect: return "Inspect";
        case G::Output: return "Output";   case G::Appearance: return "Appearance";
        case G::Draw: return "Draw";
        case G::Dimension: return "Dimension"; case G::Relations: return "Relations";
        case G::Finish: return "Finish";   case G::Study: return "Study";
        case G::Setup: return "Setup";     case G::Run: return "Run";
        case G::Insert: return "Insert";   case G::Position: return "Position";
        case G::Display: return "Display"; case G::Views: return "Views";
        case G::Annotate: return "Annotate"; case G::Tables: return "Tables";
        case G::Sheet: return "Sheet";     case G::New: return "New";
        case G::Verify: return "Verify";   case G::Place: return "Place";
        case G::Auto: return "Auto";       case G::Analyze: return "Analyze";
        case G::AI: return "AI";           default: return "?";
    }
}

std::vector<Workspace> workspacesFor(DocType d) {
    switch (d) {
        case DocType::Part:      return { W::Model, W::SheetMetal, W::Weldment, W::Surface, W::Simulate };
        case DocType::Assembly:  return { W::Assemble, W::Simulate };
        case DocType::Drawing:   return { W::Drafting };
        case DocType::Schematic: return { W::Schematic };
        case DocType::Pcb:       return { W::Board };
    }
    return { W::Model };
}

std::vector<const Command*> topCommands(Workspace w) {
    std::vector<const Command*> v;
    for (const auto& c : all())
        if (c.ws == w && c.kind == Kind::Top) v.push_back(&c);
    return v;
}
std::vector<Group> groupsFor(Workspace w) {
    std::vector<Group> v;
    for (const auto& c : all()) {
        if (c.ws != w || c.kind != Kind::Top) continue;
        bool seen = false;
        for (Group g : v) if (g == c.group) { seen = true; break; }
        if (!seen) v.push_back(c.group);
    }
    return v;
}
std::vector<const Command*> flyoutChildren(const char* parentId) {
    std::vector<const Command*> v;
    if (!parentId) return v;
    for (const auto& c : all())
        if (c.kind == Kind::Flyout && c.parent && std::strcmp(c.parent, parentId) == 0)
            v.push_back(&c);
    return v;
}
const Command* find(const char* id) {
    if (!id) return nullptr;
    for (const auto& c : all()) if (std::strcmp(c.id, id) == 0) return &c;
    return nullptr;
}
std::vector<const Command*> palette() {
    std::vector<const Command*> v;
    std::unordered_set<std::string> seen;
    for (const auto& c : all())
        if (seen.insert(c.id).second) v.push_back(&c);
    return v;
}

} // namespace cmd
