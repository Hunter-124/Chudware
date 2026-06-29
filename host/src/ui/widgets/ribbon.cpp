// AI-CAD ribbon widget — C14/C25.
// SolidWorks-style: tab strip on top, grouped big/small buttons, group separators,
// dropdown-split arrows, flyout popups with icon+label rows, disabled state,
// priority-collapse overflow reflow (C25-W3G), last-used variant tracking per parent.
// All colors from ui::palette (theme-role only — no hardcoded ImVec4/ImU32 colors).
#include "ribbon.h"

#include "imgui.h"
#include "imgui_internal.h"

#include "../icons.h"
#include "../../ui_theme.h"   // ui::palette, ui::Palette
#include "../ribbon_def.h"    // RibbonCmd, getRibbonCmds
#include "../metrics.h"       // ui::metrics::px/U/stroke/rounding + ribbon tokens
#include "../appearance_common.h"  // ui::kMaterials (material swatch colors)

#include <cmath>
#include <cstdio>
#include <cstring>
#include <functional>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

// ---------------------------------------------------------------------------
// Module-level state
// ---------------------------------------------------------------------------

static std::function<void(const char*)> s_RibbonCb;

// C25: last-used variant per parent button cmd_id.
static std::unordered_map<std::string, std::string> s_LastUsed;

// C25 FIX: flyout open requests must be deferred. The buttons run inside their own PushID
// scope, so calling OpenPopup there hashes the popup id with the button's seed while the
// matching BeginPopup (ribbon_flyoutPopup, outer scope) hashes it differently — the popup
// never appears. Buttons record the request here; ribbon_flyoutPopup performs the OpenPopup
// in its own (matching) scope.
static std::string s_flyoutOpenReq;

static const char* kRibbonTabs[] = {"Features", "Sketch", "Evaluate", "Simulation", "Assembly",
                                    "Sheet Metal", "Weldments", "PCB", "Surfaces",
                                    "View Layout", "Annotation", "Appearance", "Manufacture"};
static constexpr int kRibbonTabCount = 13;  // + W17 contextual "View Layout"/"Annotation" (drawings)
                                            //   + "Appearance" (index 11) + "Manufacture" (CAM, 12)

// W-overhaul: per-workspace tab icon, parallel to kRibbonTabs[]. The eight true workspace tabs use
// the dedicated ws_* atlas glyphs from the icon contract; the four tabs that have no ws_* glyph
// (Simulation, PCB, and the two contextual drawing tabs) reuse the closest existing atlas icon as a
// placeholder until dedicated glyphs ship.
static const char* kTabIcons[] = {
    "ws_features",    // Features
    "ws_sketch",      // Sketch
    "ws_evaluate",    // Evaluate
    "study_static",   // Simulation (placeholder — no ws_simulation glyph in the contract)
    "ws_assembly",    // Assembly
    "ws_sheetmetal",  // Sheet Metal
    "ws_weldments",   // Weldments
    "ai_panel",       // PCB (placeholder — no ws_pcb glyph in the contract)
    "ws_surfaces",    // Surfaces
    "view_selector",  // View Layout (drawing — placeholder)
    "smart_dim",      // Annotation (drawing — placeholder)
    "ws_appearance",  // Appearance
    "hole_wizard",    // Manufacture (CAM — placeholder, machining glyph)
};
static_assert(sizeof(kTabIcons) / sizeof(kTabIcons[0]) == kRibbonTabCount,
              "kTabIcons must stay parallel to kRibbonTabs");

// W17: in a Drawing doc the ribbon swaps to the two contextual drawing tabs ONLY (the
// mechanical/sim/pcb tabs are 3D-doc context and would do nothing on a sheet). Outside a
// drawing the two drawing tabs stay hidden.
static bool ribbon_tabVisible(int tab, bool showSimulationTools, bool showPcbTools,
                              bool showDrawingTools) {
    if (showDrawingTools) return tab == 9 || tab == 10;  // View Layout, Annotation
    if (tab == 9 || tab == 10) return false;
    if (tab == 3) return showSimulationTools;
    if (tab == 7) return showPcbTools;
    return true;
}

// C25c FIX (defect 3): the Draw group gained a big "Ellipse" split button (sk_ellipse_big) as the
// flyout parent, but the original small sk_ellipse row is still in the (append-only) table. Rendering
// both shows a duplicate "Ellipse". We hide the small row whenever its replacement big-button parent
// is present in the same group. {hidden small cmd_id -> replacement parent cmd_id}.
struct HiddenSmall { const char* hiddenId; const char* parentId; };
static const HiddenSmall kHiddenSmallByParent[] = {
    {"sk_ellipse", "sk_ellipse_big"},
};

// C25c FIX (defect 3): a split-button parent whose own cmd_id is not directly functional can map to
// a "default functional command" — the flyout shows it as the first (enabled) row and a plain click
// fires it. sk_ellipse_big -> sk_ellipse so clicking the big Ellipse draws an ellipse immediately.
struct ParentDefault { const char* parentId; const char* defaultCmd; const char* defaultLabel; const char* icon; };
static const ParentDefault kParentDefaults[] = {
    {"sk_ellipse_big", "sk_ellipse",  "Ellipse", "ellipse"},
    {"sk_polygon",     "sk_poly_hex", "Hexagon", "polygon"},
};

static const ParentDefault* ribbon_parentDefault(const char* parentId) {
    for (const auto& pd : kParentDefaults)
        if (std::strcmp(pd.parentId, parentId) == 0) return &pd;
    return nullptr;
}

// C25c FIX (defect 3): seed last-used so a plain click on a default-mapped split button immediately
// fires its functional default (e.g. clicking the big Ellipse draws an ellipse). Runs once.
static void ribbon_seedDefaults() {
    static bool seeded = false;
    if (seeded) return;
    seeded = true;
    for (const auto& pd : kParentDefaults)
        if (s_LastUsed.find(pd.parentId) == s_LastUsed.end())
            s_LastUsed[pd.parentId] = pd.defaultCmd;
}

// Ribbon geometry constants (logical pixels at 1x — always multiply by ui::uiScale)
static constexpr float kTabH        = 28.0f;
static constexpr float kBigIconSz   = 36.0f;
static constexpr float kSmIconSz    = 20.0f;
static constexpr float kBigBtnW     = 52.0f;
static constexpr float kBigBtnMaxW  = 96.0f;
static constexpr float kBigLblPad   = 6.0f;
static constexpr float kGroupPadX   = 9.0f;
static constexpr float kBodyH       = 100.0f;
static constexpr float kCaptionH    = 22.0f;
static constexpr float kSmRowH      = 22.0f;
static constexpr float kArrowW      = 12.0f;
static constexpr float kDropArrowW  = 14.0f;

static const char* ribbon_displayLabel(const char* cmd_id, const char* label) {
    // The Features-tab sketch entry is intentionally icon-only; the tooltip keeps the full name.
    if (cmd_id && std::strcmp(cmd_id, "sketch_new") == 0) return "";
    return label ? label : "";
}

// C25-W3G: collapse levels for priority degradation
enum CollapseLevel {
    COL_BIG   = 0,  // full big-button layout
    COL_SMALL = 1,  // big buttons step down to small (icon + side label)
    COL_LABEL = 2,  // group label trimmed / hidden
    COL_HIDDEN= 3,  // group goes into overflow chevron
};

// C24: split a big-button label into up to two lines.
static float ribbon_wrapLabel(const char* label, std::string& l1, std::string& l2) {
    l1 = label; l2.clear();
    std::string s(label);
    size_t nl = s.find('\n');
    if (nl != std::string::npos) {
        l1 = s.substr(0, nl);
        l2 = s.substr(nl + 1);
    } else {
        size_t sp = s.rfind(' ');
        if (sp != std::string::npos && s.size() > 7) {
            l1 = s.substr(0, sp);
            l2 = s.substr(sp + 1);
        }
    }
    float w1 = ImGui::CalcTextSize(l1.c_str()).x;
    float w2 = l2.empty() ? 0.0f : ImGui::CalcTextSize(l2.c_str()).x;
    return w1 > w2 ? w1 : w2;
}

// Width of a big button in screen pixels.
// §2.1: ribbon_wrapLabel returns a CalcTextSize result (already screen-px / DPI-scaled), so the
// label width is computed entirely in screen px (kBigLblPad scaled once) and clamped against the
// screen-px min/max bounds. Mixing the screen-px text with *s once would double-scale the label.
static float ribbon_bigBtnW(const char* cmd_id, const char* label, bool hasDrop) {
    float s = ui::uiScale;
    std::string l1, l2;
    const char* visibleLabel = ribbon_displayLabel(cmd_id, label);
    float lblWScreen = ribbon_wrapLabel(visibleLabel, l1, l2) + kBigLblPad * 2.0f * s;
    float base = (visibleLabel[0] == '\0' ? 46.0f : kBigBtnW) * s;
    if (lblWScreen > base) base = lblWScreen;
    if (base > kBigBtnMaxW * s) base = kBigBtnMaxW * s;
    return base + (hasDrop ? kDropArrowW * s : 0.0f);
}

// Width of a small button in screen pixels.
// Mirrors the hit/draw geometry in ribbon_smBtn (icon + 4px gap + label + trailing pad).
static float ribbon_smBtnW(const char* label) {
    float s = ui::uiScale;
    return kSmIconSz * s + 4.0f * s + ImGui::CalcTextSize(label).x + 10.0f * s;
}

// C25c FIX (defect 1): a small-button stack flows top->bottom then wraps into a new column.
// Each column's width must be the widest *row in that column*, not the group-wide max, or a
// short column followed by a wide one overlaps the next group. This describes one wrapped column.
struct SmColumn {
    int   first;   // index of first row (into the flat cmd list)
    int   count;   // rows in this column
    float width;   // measured width = max row width in this column (screen px)
};

// Pack `widths[]` (per-row measured widths) into vertical columns of at most `rowsPerCol` each,
// returning the column descriptors. Total width of the stack = sum of column widths.
static std::vector<SmColumn> ribbon_packColumns(const std::vector<float>& widths, int rowsPerCol) {
    std::vector<SmColumn> cols;
    if (rowsPerCol < 1) rowsPerCol = 1;
    for (int i = 0; i < (int)widths.size(); ) {
        SmColumn col; col.first = i; col.count = 0; col.width = 0.0f;
        for (int r = 0; r < rowsPerCol && i < (int)widths.size(); ++r, ++i) {
            col.width = ImMax(col.width, widths[i]);
            col.count++;
        }
        cols.push_back(col);
    }
    return cols;
}

static float ribbon_columnsTotalW(const std::vector<SmColumn>& cols) {
    float w = 0.0f;
    for (const auto& c : cols) w += c.width;
    return w;
}

// How many small rows fit vertically in the body small-zone.
static int ribbon_rowsPerCol() {
    float s = ui::uiScale;
    float smZoneH = (kBodyH - kCaptionH) * s;
    int n = (int)std::floor(smZoneH / (kSmRowH * s));
    return n < 1 ? 1 : n;
}

// Group descriptor built each frame for the active tab
struct RibbonGroup {
    const char*                        label;
    std::vector<const ui::RibbonCmd*>  bigCmds;
    std::vector<const ui::RibbonCmd*>  smCmds;
    std::vector<const ui::RibbonCmd*>  subCmds;
    CollapseLevel                      collapse = COL_BIG;
};

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

// C25: snap to nearest integer pixel for crisp atlas rendering.
static inline float snap(float v) { return std::floor(v + 0.5f); }

static void ribbon_fire(const char* cmd_id) {
    if (s_RibbonCb) s_RibbonCb(cmd_id);
    else { std::printf("[ribbon] %s\n", cmd_id); std::fflush(stdout); }
}

// C40: the currently ACTIVE tool command (accent-filled button). See ribbon.h.
static std::string s_activeCmd;

// True while a sketch is being edited (set per-frame via setRibbonSketchActive). When false the
// Sketch tab's "Exit Sketch" state button is replaced by this "New Sketch" button — the very same
// entry point the Features tab carries — so a sketch can be started directly from the Sketch tab.
// Same group label ("Sketch") so it occupies the Exit-Sketch slot.
static bool s_sketchActive = false;
static const ui::RibbonCmd kSketchNewOnSketchTab =
    { "sketch_new", "New Sketch", "sketch", "Sketch", "Sketch", true, nullptr, false };
static bool ribbon_isActive(const char* cmd_id) {
    return !s_activeCmd.empty() && cmd_id && s_activeCmd == cmd_id;
}

static RibbonGroup& ribbon_findOrAddGroup(std::vector<RibbonGroup>& groups,
                                          const char* gLabel) {
    for (auto& g : groups)
        if (std::strcmp(g.label, gLabel) == 0) return g;
    RibbonGroup ng;
    ng.label = gLabel;
    groups.push_back(ng);
    return groups.back();
}

static bool ribbon_hasSubs(const RibbonGroup& grp, const ui::RibbonCmd* cmd) {
    for (const auto* sc : grp.subCmds)
        if (std::strcmp(sc->sub_of, cmd->cmd_id) == 0) return true;
    return false;
}

// C25c FIX (defect 1): measured width of the small-button zone for a group, packing rows into
// independently-measured columns (each column's width = its widest row, not the group-wide max).
// `extraSmallRows` are big-cmd labels that degrade to small rows (COL_SMALL/COL_LABEL).
static float ribbon_smZoneW(const RibbonGroup& grp,
                            const std::vector<const ui::RibbonCmd*>& extraSmallRows) {
    std::vector<float> widths;
    widths.reserve(grp.smCmds.size() + extraSmallRows.size());
    for (const auto* cmd : extraSmallRows) {
        float w = ribbon_smBtnW(cmd->label);
        if (ribbon_hasSubs(grp, cmd)) w += kDropArrowW * ui::uiScale;
        widths.push_back(w);
    }
    for (const auto* cmd : grp.smCmds)
        widths.push_back(ribbon_smBtnW(cmd->label));
    if (widths.empty()) return 0.0f;
    auto cols = ribbon_packColumns(widths, ribbon_rowsPerCol());
    return ribbon_columnsTotalW(cols);
}

// C25c FIX (defect 2): the group must be at least as wide as its centered caption + padding, so
// captions like "Dimension" never truncate to "Dimensi" (SolidWorks behavior).
static float ribbon_captionMinW(const RibbonGroup& grp) {
    float s = ui::uiScale;
    return ImGui::CalcTextSize(grp.label).x + kGroupPadX * s * 2.0f;
}

// Compute natural width (screen px) of a group at a given collapse level.
static float ribbon_groupNaturalW(const RibbonGroup& grp, CollapseLevel level) {
    float s = ui::uiScale;
    if (level == COL_HIDDEN) return 0.0f;

    float content;
    if (level == COL_BIG) {
        // big-button column: buttons laid out left-to-right
        float bigColW = 0.0f;
        for (const auto* cmd : grp.bigCmds) {
            bool drop = ribbon_hasSubs(grp, cmd);
            bigColW += ribbon_bigBtnW(cmd->cmd_id, cmd->label, drop) + 2.0f * s;
        }
        float smW = ribbon_smZoneW(grp, {});
        content = kGroupPadX * s + bigColW
                  + (smW > 0 ? smW + kGroupPadX * s : 0.0f)
                  + kGroupPadX * s;
    } else {
        // COL_SMALL / COL_LABEL: big cmds degrade into the small-row stack alongside existing smCmds
        float smW = ribbon_smZoneW(grp, grp.bigCmds);
        content = kGroupPadX * s + smW + kGroupPadX * s;
    }

    // Defect 2: never narrower than the caption (unless the label is hidden at COL_LABEL).
    if (level != COL_LABEL)
        content = ImMax(content, ribbon_captionMinW(grp));
    return content;
}

static const char* ribbon_tipFor(const char* cmd_id) {
    struct T { const char* id; const char* tip; };
    static const T tips[] = {
        {"sketch_new",        "Start a 2D sketch on a plane or planar face."},
        {"extrude_boss",      "Add material by extruding a sketch into a solid."},
        {"extrude_cut",       "Remove material by extruding a sketch through the body."},
        {"revolve_boss",      "Add material by revolving a sketch around an axis."},
        {"revolve_cut",       "Remove material by revolving a sketch around an axis."},
        {"sweep_boss",        "Add material by sweeping a profile along a path."},
        {"sweep_cut",         "Remove material by sweeping a profile along a path."},
        {"loft_boss",         "Add material by blending between two or more profiles."},
        {"loft_cut",          "Remove material by blending between two or more profiles."},
        {"fillet",            "Round one or more edges with a constant radius."},
        {"chamfer",           "Bevel one or more edges by a distance or angle."},
        {"shell",             "Hollow the body, leaving walls of a chosen thickness."},
        {"draft",             "Taper faces by an angle for moldability."},
        {"rib",               "Add a thin support rib from an open sketch."},
        {"hole_wizard",       "Create standard holes (counterbore, countersink, tapped)."},
        {"linear_pattern",    "Repeat a feature in one or two linear directions."},
        {"circular_pattern",  "Repeat a feature around an axis."},
        {"mirror_feature",    "Mirror features across a plane or planar face."},
        {"ref_plane",         "Create a reference plane to sketch or build on."},
        {"ref_axis",          "Create a reference axis for patterns and mates."},
        {"ref_point",         "Create a reference point."},
        {"ref_csys",          "Create a coordinate system."},
        {"boolean_combine",   "Combine multiple bodies into one (union)."},
        {"boolean_subtract",  "Subtract one body from another."},
        {"boolean_intersect", "Keep only the overlap of two bodies."},
        {"curve_helix",       "Create a helix or spiral curve."},
        {"split",             "Split a body into multiple bodies."},
        {"sketch_exit",       "Finish editing and exit the active sketch."},
        {"sk_line",           "Draw a line or chain of lines."},
        {"sk_rect_corner",    "Draw a rectangle from corner to corner."},
        {"sk_rect_center",    "Draw a rectangle from its center."},
        {"sk_circle",         "Draw a circle from center and radius."},
        {"sk_arc_3pt",        "Draw an arc through three points."},
        {"sk_arc_center",     "Draw an arc from center, start and end."},
        {"sk_arc_tangent",    "Draw an arc tangent to an existing entity."},
        {"sk_polygon",        "Draw a regular polygon."},
        {"sk_slot",           "Draw a straight or arc slot."},
        {"sk_spline",         "Draw a smooth spline through points."},
        {"sk_ellipse",        "Draw an ellipse."},
        {"sk_point",          "Place a sketch point."},
        {"sk_text",           "Add sketch text."},
        {"smart_dim",         "Add a driving dimension to entities."},
        {"sk_trim",           "Trim entities to their nearest intersection."},
        {"sk_extend",         "Extend an entity to meet another."},
        {"sk_offset_ent",     "Offset entities by a distance."},
        {"sk_convert_ent",    "Convert model edges into sketch entities."},
        {"sk_construction",   "Toggle entities to construction geometry."},
        {"sk_mirror",         "Mirror sketch entities across a centerline."},
        {"sk_lin_pattern",    "Pattern sketch entities linearly."},
        {"sk_circ_pattern",   "Pattern sketch entities circularly."},
        {"sk_move",           "Move or copy sketch entities."},
        {"sk_relations",      "Add geometric relations between entities."},
        {"sk_fix",            "Fix an entity in place."},
        {"sk_rect_3pt_corner",  "Draw a rectangle by three corner points."},
        {"sk_rect_3pt_center",  "Draw a rectangle by center and three points."},
        {"sk_parallelogram",    "Draw a parallelogram by three points."},
        {"sk_circle_perimeter", "Draw a circle defined by three perimeter points."},
        {"sk_slot_ctr_str",     "Draw a straight slot from its centerpoint."},
        {"sk_slot_arc_3pt",     "Draw an arc slot through three points."},
        {"sk_slot_arc_ctr",     "Draw an arc slot from its centerpoint."},
        {"sk_spline_style",     "Draw a style spline with CV control points."},
        {"sk_spline_surface",   "Project a spline onto a 3D surface."},
        {"sk_spline_equation",  "Define a curve by a parametric equation."},
        {"sk_ellipse_big",      "Draw an ellipse or ellipse variant."},
        {"sk_ellipse_partial",  "Draw a partial ellipse arc."},
        {"sk_parabola",         "Draw a parabolic curve."},
        {"sk_conic",            "Draw a conic curve defined by rho value."},
        {"eval_measure",      "Measure distance, angle, radius between entities."},
        {"eval_mass_props",   "Compute mass, volume, center of mass."},
        {"eval_section_props","Compute area and section properties of a face."},
        {"eval_draft",        "Analyze draft angles relative to a pull direction."},
        {"eval_thickness",    "Check wall thickness across the model."},
        {"eval_curvature",    "Display surface curvature."},
        {"eval_zebra",        "Show zebra stripes to inspect surface continuity."},
        {"eval_symmetry",     "Check the model for symmetry."},
        {"eval_check",        "Check geometry for errors and bad faces."},
        {"eval_interference", "Detect interference between bodies."},
        {"eval_geometry",     "Run a general geometry analysis."},
        {"sim_new_static",    "Create a linear static stress study."},
        {"sim_new_modal",     "Create a frequency / modal study."},
        {"sim_new_flow",      "Create a flow simulation study."},
        {"sim_material",      "Assign a material to the study."},
        {"sim_fixture",       "Add a fixture / restraint."},
        {"sim_load_force",    "Apply a force load."},
        {"sim_load_pressure", "Apply a pressure load."},
        {"sim_mesh",          "Generate the simulation mesh."},
        {"sim_run",           "Run the study."},
        {"sim_results",       "View result plots."},
        {"asm_insert",        "Insert a component into the assembly."},
        {"asm_move",          "Move or rotate a component."},
        {"asm_pattern",       "Pattern a component."},
        {"asm_mate",          "Add a mate to constrain components."},
        {"asm_interference",  "Detect interference between components."},
        {"asm_bom",           "Generate a bill of materials."},
        {"asm_explode",       "Create an exploded view."},
        // --- Sketch: corner fillet/chamfer + airfoil generator (flyout/tools) ---
        {"sk_fillet",         "Round a sharp sketch corner between two lines."},
        {"sk_chamfer",        "Bevel a sharp sketch corner between two lines."},
        {"sk_airfoil",        "Generate a NACA airfoil section as chained splines."},
        // --- Sketch: polygon flyout variants ---
        {"sk_poly_tri",  "Draw an equilateral triangle."},
        {"sk_poly_quad", "Draw a square."},
        {"sk_poly_pent", "Draw a regular pentagon."},
        {"sk_poly_hex",  "Draw a regular hexagon."},
        {"sk_poly_oct",  "Draw a regular octagon."},
        {"sk_poly_star", "Draw a star polygon with alternating outer and inner radii."},
        // --- Simulation: extended study types ---
        {"sim_new_thermal",   "Create a steady-state or transient thermal study."},
        {"sim_new_fatigue",   "Estimate fatigue life from a static stress result."},
        // --- Assembly ---
        {"asm_parts_library", "Insert a standard hardware part from the library."},
        // --- Evaluate: drawing entry point + profiler ---
        {"make_drawing",      "Create a drawing sheet with standard views of the part."},
        {"prof_toggle",       "Start or stop the modeling performance profiler."},
        // --- Sheet Metal ---
        {"sm_base_flange",    "Create the first wall of a sheet-metal part."},
        {"sm_edge_flange",    "Add a flange bent from an existing sheet edge."},
        {"sm_hem",            "Fold an edge back on itself to stiffen or finish it."},
        {"sm_jog",            "Add two bends to offset the sheet into a step."},
        {"sm_flat_pattern",   "Unfold the sheet-metal part into its flat pattern."},
        {"sm_export_flat_dxf","Export the flat-pattern outline and bend lines to DXF."},
        // --- Weldments ---
        {"weld_member",       "Sweep a structural profile along the path segments."},
        {"weld_gusset",       "Add a triangular gusset plate to brace a corner."},
        {"weld_end_cap",      "Close the open end of a structural tube."},
        // --- PCB: schematic, board, analysis, outputs ---
        {"ecad_ai_design",    "Generate a schematic and PCB layout from a text prompt."},
        {"ecad_new_sch",      "Start a new schematic sheet."},
        {"ecad_new_pcb",      "Start a new PCB layout."},
        {"ecad_open",         "Open an existing schematic or board."},
        {"ecad_save",         "Save the schematic and board."},
        {"sch_add_symbol",    "Place a component symbol on the schematic."},
        {"sch_add_wire",      "Draw a wire between schematic pins."},
        {"sch_add_label",     "Add a net label to name a connection."},
        {"sch_add_power",     "Place a power or ground port."},
        {"sch_annotate",      "Assign reference designators to the symbols."},
        {"ecad_run_erc",      "Run electrical rule checks on the schematic."},
        {"pcb_update_from_sch","Push schematic changes onto the board."},
        {"pcb_add_footprint", "Place a component footprint on the board."},
        {"pcb_add_track",     "Route a copper track between pads."},
        {"pcb_add_via",       "Place a via connecting copper layers."},
        {"pcb_add_zone",      "Draw a copper pour zone."},
        {"ecad_autoroute",    "Automatically route the board's connections."},
        {"ecad_fill_zones",   "Recompute copper pours around tracks and pads."},
        {"ecad_run_drc",      "Run design rule checks on the board."},
        {"ecad_simulate",     "Run a SPICE simulation of the circuit."},
        {"ecad_to_3d",        "Build a 3D model of the populated board."},
        {"ecad_export_gerber","Export Gerber fabrication files."},
        {"ecad_export_drill", "Export the NC drill file."},
        {"ecad_export_bom",   "Export the board bill of materials."},
        // --- Surfaces ---
        {"surf_extrude",      "Create an open surface by extruding a profile."},
        {"surf_revolve",      "Create a surface by revolving a profile about an axis."},
        {"surf_sweep",        "Create a surface by sweeping a profile along a path."},
        {"surf_loft",         "Create a surface blended between two or more profiles."},
        {"surf_planar",       "Create a planar surface from a closed boundary."},
        {"surf_offset",       "Offset faces or surfaces by a distance."},
        {"surf_ruled",        "Create a ruled surface from an edge."},
        {"surf_boundary",     "Create a boundary surface between edges."},
        {"surf_fill",         "Fill a bounded region with a surface patch."},
        {"surf_knit",         "Knit adjacent surfaces into a single surface."},
        {"surf_thicken",      "Thicken a surface into a solid body."},
        {"surf_trim",         "Trim a surface with a curve or another surface."},
        {"surf_untrim",       "Remove trim boundaries to restore the full surface."},
        {"surf_delete_face",  "Remove faces from a body or surface."},
        {"surf_replace_face", "Replace faces with a target surface."},
        // --- Drawing: View Layout ---
        {"dwg_view_model",    "Place a model view on the sheet."},
        {"dwg_view_proj",     "Project a new view from an existing view."},
        {"dwg_view_std3",     "Place the standard three orthographic views."},
        {"dwg_view_section",  "Create a section view through a cutting line."},
        {"dwg_view_detail",   "Create an enlarged detail view of a region."},
        {"dwg_sheet_props",   "Edit sheet size, scale, and properties."},
        {"dwg_rebuild",       "Rebuild the views to match the model."},
        {"dwg_fit",           "Fit the sheet to the window."},
        {"dwg_export_dxf",    "Export the drawing sheet to DXF."},
        {"dwg_export_pdf",    "Export the drawing sheet to PDF."},
        {"dwg_section_half",  "Create a half-section view."},
        {"dwg_section_reverse","Flip the section cut direction."},
        {"dwg_scale_1_1",     "Set the selected view scale to 1:1."},
        {"dwg_scale_1_2",     "Set the selected view scale to 1:2."},
        {"dwg_scale_2_1",     "Set the selected view scale to 2:1."},
        // --- Drawing: Annotation ---
        {"dwg_smart_dim",     "Add a dimension to drawing entities."},
        {"dwg_model_items",   "Import dimensions and annotations from the model."},
        {"dwg_hole_callout",  "Add a hole callout to a hole."},
        {"dwg_gdt",           "Add a geometric tolerance frame."},
        {"dwg_datum",         "Place a datum feature symbol."},
        {"dwg_datum_target",  "Place a datum target symbol."},
        {"dwg_surface_finish","Add a surface finish symbol."},
        {"dwg_weld",          "Add a welding symbol."},
        {"dwg_center_mark",   "Add a center mark to a circle or arc."},
        {"dwg_center_line",   "Add a center line between two edges."},
        {"dwg_note",          "Add a text note, optionally with a leader."},
        {"dwg_balloon",       "Add a BOM balloon to a component."},
        {"dwg_auto_balloon",  "Auto-place balloons on every component."},
        {"dwg_title_block",   "Edit the sheet's title block fields."},
        {"dwg_bom_table",     "Insert a bill of materials table."},
        // --- Appearance: material / clear + display styles ---
        {"appr_color",        "Open the appearance editor for color and material."},
        {"appr_mat_plastic",  "Apply a matte plastic appearance."},
        {"appr_mat_aluminum", "Apply a brushed aluminum appearance."},
        {"appr_mat_steel",    "Apply a polished steel appearance."},
        {"appr_mat_brass",    "Apply a warm brass appearance."},
        {"appr_mat_dark",     "Apply a dark metal appearance."},
        {"appr_clear",        "Clear the appearance and restore the default look."},
        {"display_shaded",      "Show the model shaded without edges."},
        {"display_shaded_edges","Show the model shaded with edges."},
        {"display_hlr",         "Show edges with hidden lines removed."},
        {"display_hlv",         "Show edges with hidden lines shown dashed."},
        {"display_wireframe",   "Show the model as a wireframe."},
    };
    for (const auto& t : tips)
        if (std::strcmp(t.id, cmd_id) == 0) return t.tip;
    return nullptr;
}

// Look up the atlas icon name registered for a cmd_id (mirrors the ribbon_def table) so the tooltip
// can render the very glyph the button shows. Returns nullptr for an unknown command.
static const char* ribbon_iconFor(const char* cmd_id) {
    if (!cmd_id) return nullptr;
    int n = 0;
    const ui::RibbonCmd* cmds = ui::getRibbonCmds(n);
    for (int i = 0; i < n; ++i)
        if (std::strcmp(cmds[i].cmd_id, cmd_id) == 0) return cmds[i].icon;
    return nullptr;
}

static void ribbon_tooltip(const char* title, const char* cmd_id, bool enabled) {
    auto col = [](const float f[4]) { return ImVec4(f[0], f[1], f[2], f[3]); };
    ui::pushGlassTooltipStyle();
    if (ImGui::BeginTooltip()) {
        ImVec2 wp = ImGui::GetWindowPos();
        ImVec2 ws = ImGui::GetWindowSize();
        ui::glassBackground(ImGui::GetWindowDrawList(), wp, ImVec2(wp.x + ws.x, wp.y + ws.y));
        // Icon mini-preview: the same glyph the button shows, so the tooltip previews what the tool
        // does. Tinted like the button — white when enabled, grey when disabled / "Coming soon".
        const char* iconName = ribbon_iconFor(cmd_id);
        const bool  hasIcon  = iconName && *iconName;
        if (hasIcon) {
            float isz = 36.0f * ui::uiScale;
            ui::IconUV ic = ui::getIcon(iconName, 32);
            ImVec4 tint = enabled ? ImVec4(1, 1, 1, 1) : ImVec4(0.5f, 0.5f, 0.5f, 0.63f);
            ImGui::Image(ic.texId, ImVec2(isz, isz), ic.uv0, ic.uv1, tint);
            ImGui::SameLine();
            ImGui::BeginGroup();
        }
        ImGui::PushTextWrapPos(ImGui::GetFontSize() * 20.0f);
        ImGui::PushStyleColor(ImGuiCol_Text, col(ui::palette.text));
        ImGui::TextUnformatted(title);
        ImGui::PopStyleColor();
        const char* d = ribbon_tipFor(cmd_id);
        if (d) {
            ImGui::PushStyleColor(ImGuiCol_Text, col(ui::palette.textDim));
            ImGui::TextUnformatted(d);
            ImGui::PopStyleColor();
        }
        if (!enabled) {
            ImGui::PushStyleColor(ImGuiCol_Text, col(ui::palette.textDisabled));
            ImGui::TextUnformatted("Coming soon");
            ImGui::PopStyleColor();
        }
        ImGui::PopTextWrapPos();
        if (hasIcon) ImGui::EndGroup();
        ImGui::EndTooltip();
    }
    ui::popGlassTooltipStyle();
}

// The Appearance "Material" buttons each apply one RealView preset color. ribbon_matSwatchColor maps
// an appr_mat_* cmd_id to the RGB of the matching ui::kMaterials entry (single source of truth) so a
// button can paint a swatch of the exact color it applies. Returns nullptr for any other command.
static const float* ribbon_matSwatchColor(const char* cmd_id) {
    if (!cmd_id) return nullptr;
    struct MatBtn { const char* cmd_id; int idx; };
    static const MatBtn kMatBtns[] = {
        {"appr_mat_plastic",  0}, {"appr_mat_aluminum", 1}, {"appr_mat_steel", 2},
        {"appr_mat_brass",    3}, {"appr_mat_dark",     4},
    };
    for (const auto& m : kMatBtns)
        if (std::strcmp(m.cmd_id, cmd_id) == 0) return ui::kMaterials[m.idx].rgb;
    return nullptr;
}

// Draw a SW big button. Returns true if main area clicked.
static bool ribbon_bigBtn(const char* cmd_id, const char* iconName,
                           const char* label, bool enabled, bool hasDrop) {
    float s   = ui::uiScale;
    float bw  = ribbon_bigBtnW(cmd_id, label, hasDrop);
    float bh  = (kBodyH - kCaptionH) * s;

    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::PushID(cmd_id);

    bool clicked     = false;
    bool dropClicked = false;

    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(0, 0));
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(0, 0));
    ImGui::PushStyleColor(ImGuiCol_Button,        ImVec4(0,0,0,0));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(0,0,0,0));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,  ImVec4(0,0,0,0));

    float mainW = hasDrop ? (bw - kArrowW * s) : bw;
    clicked = ImGui::InvisibleButton("##main", ImVec2(mainW, bh));
    bool mainHov    = ImGui::IsItemHovered();
    bool mainHovTip = ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal);

    // C40: armed-tool highlight (drawn under the hover fill).
    if (ribbon_isActive(cmd_id)) {
        ImDrawList* adl = ImGui::GetWindowDrawList();
        adl->AddRectFilled(p0, ImVec2(p0.x + bw, p0.y + bh),
                           ui::Palette::toU32(ui::palette.accentSoft),
                           ui::metrics::ribbonBtnRound());
        adl->AddRect(p0, ImVec2(p0.x + bw, p0.y + bh),
                     ui::Palette::toU32(ui::palette.accent),
                     ui::metrics::ribbonBtnRound(), 0, ui::metrics::stroke(1.5f));
    }

    if (hasDrop) {
        ImGui::SameLine(0, 0);
        dropClicked = ImGui::InvisibleButton("##drop", ImVec2(kArrowW * s, bh));
        bool dropHov = ImGui::IsItemHovered();

        ImDrawList* dl = ImGui::GetWindowDrawList();
        if ((mainHov || dropHov) && enabled) {
            ImU32 hBg = ui::Palette::toU32(ui::palette.buttonHovered);
            dl->AddRectFilled(p0, ImVec2(p0.x + bw, p0.y + bh), hBg,
                              ui::metrics::ribbonBtnRound());
        }
        {
            float ax = p0.x + mainW + kArrowW * s * 0.5f;
            float ay = p0.y + bh - 10.0f * s;
            ImU32 aCol = enabled ? ui::Palette::toU32(ui::palette.text)
                                 : ui::Palette::toU32(ui::palette.textDisabled);
            float hs = 3.5f * s;
            dl->AddTriangleFilled(ImVec2(ax - hs, ay - hs * 0.5f),
                                  ImVec2(ax + hs, ay - hs * 0.5f),
                                  ImVec2(ax,      ay + hs * 0.5f), aCol);
        }

        if (dropClicked && enabled) s_flyoutOpenReq = cmd_id;   // deferred (ID-scope fix)
    } else {
        if (mainHov && enabled) {
            ImDrawList* dl = ImGui::GetWindowDrawList();
            ImU32 hBg = ui::Palette::toU32(ui::palette.buttonHovered);
            dl->AddRectFilled(p0, ImVec2(p0.x + bw, p0.y + bh), hBg,
                              ui::metrics::ribbonBtnRound());
        }
    }

    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar(2);

    // Draw icon + label
    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        float iconSz = snap(kBigIconSz * s);
        float cx     = snap(p0.x + mainW * 0.5f);
        const char* visibleLabel = ribbon_displayLabel(cmd_id, label);
        float iy     = snap(p0.y + 4.0f * s);
        if (visibleLabel[0] == '\0')
            iy = snap(p0.y + (bh - iconSz) * 0.5f - 1.0f * s);

        ui::IconUV ic = ui::getIcon(iconName, 32);
        ImU32 tint = enabled ? IM_COL32(255,255,255,255) : IM_COL32(128,128,128,160);
        dl->AddImage(ic.texId,
                     ImVec2(snap(cx - iconSz * 0.5f), iy),
                     ImVec2(snap(cx - iconSz * 0.5f) + iconSz, iy + iconSz),
                     ic.uv0, ic.uv1, tint);

        // Material color swatch: a 20x4 bar under the icon previewing the exact color this button
        // applies. The label below is nudged down (see ty) so the bar never overlaps the text.
        const float* matRgb = ribbon_matSwatchColor(cmd_id);
        if (matRgb) {
            float swW = 20.0f * s, swH = 4.0f * s;
            float sx0 = snap(cx - swW * 0.5f);
            float sy0 = snap(iy + iconSz + 1.0f * s);
            ImU32 swCol = IM_COL32((int)(matRgb[0] * 255.0f + 0.5f),
                                   (int)(matRgb[1] * 255.0f + 0.5f),
                                   (int)(matRgb[2] * 255.0f + 0.5f), enabled ? 255 : 160);
            dl->AddRectFilled(ImVec2(sx0, sy0), ImVec2(sx0 + swW, sy0 + swH), swCol, 1.5f * s);
            dl->AddRect(ImVec2(sx0, sy0), ImVec2(sx0 + swW, sy0 + swH),
                        ui::Palette::toU32(ui::palette.border), 1.5f * s, 0,
                        ui::metrics::stroke(1.0f));
        }

        std::string l1, l2;
        ribbon_wrapLabel(visibleLabel, l1, l2);
        ImU32 tCol = enabled ? ui::Palette::toU32(ui::palette.text)
                             : ui::Palette::toU32(ui::palette.textDisabled);
        float lineH = ImGui::GetTextLineHeight();
        float ty    = iy + iconSz + (matRgb ? 6.0f * s : 2.0f * s);
        dl->PushClipRect(ImVec2(p0.x, p0.y), ImVec2(p0.x + mainW, p0.y + bh), true);
        if (visibleLabel[0] != '\0') {
            ImVec2 t1 = ImGui::CalcTextSize(l1.c_str());
            dl->AddText(nullptr, 0.0f, ImVec2(cx - t1.x * 0.5f, ty), tCol, l1.c_str());
            if (!l2.empty()) {
                ImVec2 t2 = ImGui::CalcTextSize(l2.c_str());
                dl->AddText(nullptr, 0.0f, ImVec2(cx - t2.x * 0.5f, ty + lineH), tCol, l2.c_str());
            }
        }
        dl->PopClipRect();
    }

    if (mainHovTip) ribbon_tooltip(label, cmd_id, enabled);

    // C25: plain-click on split button fires last-used variant; if none chosen yet and the
    // parent itself has no functional action (hasDrop=true), open the flyout instead.
    if (clicked && enabled) {
        if (hasDrop) {
            auto it = s_LastUsed.find(cmd_id);
            if (it != s_LastUsed.end() && !it->second.empty()) {
                ribbon_fire(it->second.c_str());
            } else {
                // C34: plain click on a split button fires the PARENT tool itself (SolidWorks
                // behavior — the flyout is the arrow's job). Parents with no action of their
                // own are pre-seeded via kParentDefaults, so they never reach this branch.
                ribbon_fire(cmd_id);
            }
        } else {
            ribbon_fire(cmd_id);
        }
    }

    ImGui::PopID();
    return clicked && enabled;
}

// §2.3: the height of a degraded "big-as-small" row (fit ~3 per body zone, floored at the small
// row height). Layout MUST advance sy by this value, not kSmRowH*s, or these taller rows overlap.
static float ribbon_bigBtnAsSmallRowH() {
    float s = ui::uiScale;
    return ImMax(snap((kBodyH - kCaptionH) * s / 3.0f), kSmRowH * s);
}

// Draw a big button degraded to small-row layout (icon + side label, one row height).
static bool ribbon_bigBtnAsSmall(const char* cmd_id, const char* iconName,
                                  const char* label, bool enabled, bool hasDrop) {
    float s      = ui::uiScale;
    float rowH   = ribbon_bigBtnAsSmallRowH();  // fit ~3 per body zone, floored at kSmRowH
    float iconSz = snap(kSmIconSz * s);
    float lblW   = ImGui::CalcTextSize(label).x;
    float totalW = iconSz + 4.0f * s + lblW + (hasDrop ? kDropArrowW * s : 0.0f) + 10.0f * s;

    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::PushID(cmd_id);

    bool clicked     = false;
    bool dropClicked = false;

    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(0, 0));
    ImGui::PushStyleColor(ImGuiCol_Button,        ImVec4(0,0,0,0));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(0,0,0,0));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,  ImVec4(0,0,0,0));

    float mainW = hasDrop ? (totalW - kDropArrowW * s) : totalW;
    clicked = ImGui::InvisibleButton("##msm", ImVec2(mainW, rowH));
    bool mainHov    = ImGui::IsItemHovered();
    bool mainHovTip = ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal);

    // C40: armed-tool highlight (drawn under the hover fill).
    if (ribbon_isActive(cmd_id)) {
        ImDrawList* adl = ImGui::GetWindowDrawList();
        adl->AddRectFilled(p0, ImVec2(p0.x + totalW, p0.y + rowH),
                           ui::Palette::toU32(ui::palette.accentSoft),
                           ui::metrics::ribbonBtnRound());
        adl->AddRect(p0, ImVec2(p0.x + totalW, p0.y + rowH),
                     ui::Palette::toU32(ui::palette.accent),
                     ui::metrics::ribbonBtnRound(), 0, ui::metrics::stroke(1.5f));
    }

    if (hasDrop) {
        ImGui::SameLine(0, 0);
        dropClicked = ImGui::InvisibleButton("##mdr", ImVec2(kDropArrowW * s, rowH));
        bool dropHov = ImGui::IsItemHovered();
        ImDrawList* dl = ImGui::GetWindowDrawList();
        if ((mainHov || dropHov) && enabled)
            dl->AddRectFilled(p0, ImVec2(p0.x + totalW, p0.y + rowH),
                              ui::Palette::toU32(ui::palette.buttonHovered),
                              ui::metrics::ribbonBtnRound());
        {
            float ax = p0.x + mainW + kDropArrowW * s * 0.5f;
            float ay = p0.y + rowH * 0.5f;
            ImU32 aCol = enabled ? ui::Palette::toU32(ui::palette.text)
                                 : ui::Palette::toU32(ui::palette.textDisabled);
            float hs = 3.0f * s;
            dl->AddTriangleFilled(ImVec2(ax - hs, ay - hs * 0.5f),
                                  ImVec2(ax + hs, ay - hs * 0.5f),
                                  ImVec2(ax,      ay + hs * 0.5f), aCol);
        }
        if (dropClicked && enabled) s_flyoutOpenReq = cmd_id;   // deferred (ID-scope fix)
    } else {
        if (mainHov && enabled) {
            ImDrawList* dl = ImGui::GetWindowDrawList();
            dl->AddRectFilled(p0, ImVec2(p0.x + totalW, p0.y + rowH),
                              ui::Palette::toU32(ui::palette.buttonHovered),
                              ui::metrics::ribbonBtnRound());
        }
    }

    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar();

    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const float* matRgb = ribbon_matSwatchColor(cmd_id);
        float ix = snap(p0.x + 2.0f * s + (matRgb ? 6.0f * s : 0.0f));
        float iy = snap(p0.y + (rowH - iconSz) * 0.5f);
        ui::IconUV ic = ui::getIcon(iconName, 20);
        ImU32 tint = enabled ? IM_COL32(255,255,255,255) : IM_COL32(128,128,128,160);
        dl->AddImage(ic.texId, ImVec2(ix, iy), ImVec2(ix + iconSz, iy + iconSz),
                     ic.uv0, ic.uv1, tint);
        if (matRgb) {
            // Vertical color swatch strip to the left of the icon (compact row layout).
            float stW = 4.0f * s, stH = 14.0f * s;
            float stx = snap(p0.x + 2.0f * s);
            float sty = snap(p0.y + (rowH - stH) * 0.5f);
            ImU32 swCol = IM_COL32((int)(matRgb[0] * 255.0f + 0.5f),
                                   (int)(matRgb[1] * 255.0f + 0.5f),
                                   (int)(matRgb[2] * 255.0f + 0.5f), enabled ? 255 : 160);
            dl->AddRectFilled(ImVec2(stx, sty), ImVec2(stx + stW, sty + stH), swCol, 1.0f * s);
            dl->AddRect(ImVec2(stx, sty), ImVec2(stx + stW, sty + stH),
                        ui::Palette::toU32(ui::palette.border), 1.0f * s, 0,
                        ui::metrics::stroke(1.0f));
        }
        float tx  = snap(ix + iconSz + 4.0f * s);
        float ty  = snap(p0.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f);
        ImU32 tCol = enabled ? ui::Palette::toU32(ui::palette.text)
                             : ui::Palette::toU32(ui::palette.textDisabled);
        dl->PushClipRect(ImVec2(tx, p0.y), ImVec2(p0.x + mainW, p0.y + rowH), true);
        dl->AddText(nullptr, 0.0f, ImVec2(tx, ty), tCol, label);
        dl->PopClipRect();
    }

    if (mainHovTip) ribbon_tooltip(label, cmd_id, enabled);

    if (clicked && enabled) {
        if (hasDrop) {
            auto it = s_LastUsed.find(cmd_id);
            if (it != s_LastUsed.end() && !it->second.empty())
                ribbon_fire(it->second.c_str());
            else {
                ribbon_fire(cmd_id);   // C34: plain click fires the parent (see bigBtn)
            }
        } else {
            ribbon_fire(cmd_id);
        }
    }

    ImGui::PopID();
    return clicked && enabled;
}

// C25: flyout popup for a split button.
static const char* ribbon_flyoutPopup(const char* parentId,
                                      const std::vector<const ui::RibbonCmd*>& subs) {
    float s = ui::uiScale;
    const char* selected = nullptr;

    std::string popId = std::string(parentId) + "_drop";
    // Deferred open from the button (see s_flyoutOpenReq) — performed here so OpenPopup and
    // BeginPopup share the same ID scope.
    if (s_flyoutOpenReq == parentId) {
        ImGui::OpenPopup(popId.c_str());
        s_flyoutOpenReq.clear();
    }
    if (ImGui::BeginPopup(popId.c_str(), ImGuiWindowFlags_NoMove)) {
        float rowH   = snap(28.0f * s);
        float iconSz = snap(20.0f * s);
        float padX   = snap(8.0f * s);
        float gapX   = snap(6.0f * s);

        // C25c FIX (defect 3): if this parent maps to a default functional command, render it as the
        // first ENABLED row (e.g. "Ellipse" -> sk_ellipse) so the flyout offers a working primary
        // action. The greyed variant rows follow. We synthesize a RibbonCmd for the default that
        // points at the parent's icon/label and the mapped functional cmd_id.
        const ParentDefault* pd = ribbon_parentDefault(parentId);
        ui::RibbonCmd defaultRow{};
        std::vector<const ui::RibbonCmd*> rows;
        if (pd) {
            defaultRow.cmd_id  = pd->defaultCmd;
            defaultRow.label   = pd->defaultLabel;
            defaultRow.icon    = pd->icon;
            defaultRow.big     = false;
            defaultRow.sub_of  = parentId;
            defaultRow.stretch = false;   // functional
            rows.push_back(&defaultRow);
        }
        for (const auto* sc : subs) rows.push_back(sc);

        float maxLblW = 0.0f;
        for (const auto* sc : rows)
            maxLblW = ImMax(maxLblW, ImGui::CalcTextSize(sc->label).x);
        float popW = padX + iconSz + gapX + maxLblW + padX;

        for (const auto* sc : rows) {
            bool scEnabled = !sc->stretch;
            ImVec2 cp0 = ImGui::GetCursorScreenPos();
            ImGui::PushID(sc->cmd_id);

            ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(0, 0));
            ImGui::PushStyleColor(ImGuiCol_Button,        ImVec4(0,0,0,0));
            ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(0,0,0,0));
            ImGui::PushStyleColor(ImGuiCol_ButtonActive,  ImVec4(0,0,0,0));
            bool rowClicked = ImGui::InvisibleButton("##fr", ImVec2(popW, rowH));
            bool rowHov     = ImGui::IsItemHovered();
            ImGui::PopStyleColor(3);
            ImGui::PopStyleVar();

            ImDrawList* dl = ImGui::GetWindowDrawList();

            if (rowHov && scEnabled)
                dl->AddRectFilled(cp0, ImVec2(cp0.x + popW, cp0.y + rowH),
                                  ui::Palette::toU32(ui::palette.buttonHovered),
                                  ui::metrics::ribbonBtnRound());

            {
                float ix = snap(cp0.x + padX);
                float iy = snap(cp0.y + (rowH - iconSz) * 0.5f);
                ui::IconUV ic = ui::getIcon(sc->icon, 20);
                ImU32 tint = scEnabled ? IM_COL32(255,255,255,255) : IM_COL32(128,128,128,160);
                dl->AddImage(ic.texId, ImVec2(ix, iy),
                             ImVec2(ix + iconSz, iy + iconSz),
                             ic.uv0, ic.uv1, tint);
            }
            {
                float tx = snap(cp0.x + padX + iconSz + gapX);
                float ty = snap(cp0.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f);
                ImU32 tCol = scEnabled ? ui::Palette::toU32(ui::palette.text)
                                       : ui::Palette::toU32(ui::palette.textDisabled);
                dl->AddText(nullptr, 0.0f, ImVec2(tx, ty), tCol, sc->label);
            }

            if (rowHov && !scEnabled) {
                ImGui::BeginTooltip();
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(ui::palette.text[0], ui::palette.text[1],
                           ui::palette.text[2], ui::palette.text[3]));
                ImGui::TextUnformatted(sc->label);
                ImGui::PopStyleColor();
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(ui::palette.textDisabled[0], ui::palette.textDisabled[1],
                           ui::palette.textDisabled[2], ui::palette.textDisabled[3]));
                ImGui::TextUnformatted("Coming soon");
                ImGui::PopStyleColor();
                ImGui::EndTooltip();
            }

            if (rowClicked && scEnabled) {
                selected = sc->cmd_id;
                ImGui::CloseCurrentPopup();
            }

            ImGui::PopID();
        }
        ImGui::EndPopup();
    }
    return selected;
}

// Draw a small button. Returns true if clicked.
static bool ribbon_smBtn(const char* cmd_id, const char* iconName,
                          const char* label, bool enabled) {
    float s      = ui::uiScale;
    float rowH   = kSmRowH * s;
    float iconSz = kSmIconSz * s;
    // §2.2: drive the drawn/hit width from the SAME helper the packer measures with, so measure
    // and draw never disagree (previously the helper returned text+18*s but this drew text+10*s).
    float totalW = ribbon_smBtnW(label);

    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::PushID(cmd_id);

    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(0, 0));
    ImGui::PushStyleColor(ImGuiCol_Button,        ImVec4(0,0,0,0));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(0,0,0,0));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,  ImVec4(0,0,0,0));
    bool clicked = ImGui::InvisibleButton("##sm", ImVec2(totalW, rowH));
    bool hov     = ImGui::IsItemHovered();
    bool hovTip  = ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal);
    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar();

    // C40: armed-tool highlight (under the hover fill).
    if (ribbon_isActive(cmd_id)) {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        dl->AddRectFilled(p0, ImVec2(p0.x + totalW, p0.y + rowH),
                          ui::Palette::toU32(ui::palette.accentSoft),
                          ui::metrics::ribbonBtnRound());
        dl->AddRect(p0, ImVec2(p0.x + totalW, p0.y + rowH),
                    ui::Palette::toU32(ui::palette.accent),
                    ui::metrics::ribbonBtnRound(), 0, ui::metrics::stroke(1.5f));
    }
    if (hov && enabled) {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        dl->AddRectFilled(p0, ImVec2(p0.x + totalW, p0.y + rowH),
                          ui::Palette::toU32(ui::palette.buttonHovered),
                          ui::metrics::ribbonBtnRound());
    }

    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        float snappedIconSz = snap(iconSz);
        const float* matRgb = ribbon_matSwatchColor(cmd_id);
        float ix  = snap(p0.x + (matRgb ? 6.0f * s : 0.0f));
        float iy  = snap(p0.y + (rowH - snappedIconSz) * 0.5f);
        ui::IconUV ic = ui::getIcon(iconName, 20);
        ImU32 tint = enabled ? IM_COL32(255,255,255,255) : IM_COL32(128,128,128,160);
        dl->AddImage(ic.texId, ImVec2(ix, iy), ImVec2(ix + snappedIconSz, iy + snappedIconSz),
                     ic.uv0, ic.uv1, tint);
        if (matRgb) {
            // Vertical color swatch strip to the left of the icon.
            float stW = 4.0f * s, stH = 14.0f * s;
            float stx = snap(p0.x);
            float sty = snap(p0.y + (rowH - stH) * 0.5f);
            ImU32 swCol = IM_COL32((int)(matRgb[0] * 255.0f + 0.5f),
                                   (int)(matRgb[1] * 255.0f + 0.5f),
                                   (int)(matRgb[2] * 255.0f + 0.5f), enabled ? 255 : 160);
            dl->AddRectFilled(ImVec2(stx, sty), ImVec2(stx + stW, sty + stH), swCol, 1.0f * s);
            dl->AddRect(ImVec2(stx, sty), ImVec2(stx + stW, sty + stH),
                        ui::Palette::toU32(ui::palette.border), 1.0f * s, 0,
                        ui::metrics::stroke(1.0f));
        }
        float tx  = snap(ix + snappedIconSz + 4.0f * s);
        float ty  = snap(p0.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f);
        ImU32 tCol = enabled ? ui::Palette::toU32(ui::palette.text)
                             : ui::Palette::toU32(ui::palette.textDisabled);
        dl->AddText(nullptr, 0.0f, ImVec2(tx, ty), tCol, label);
    }

    if (hovTip) ribbon_tooltip(label, cmd_id, enabled);

    if (clicked && enabled) ribbon_fire(cmd_id);
    ImGui::PopID();
    return clicked && enabled;
}

// C25-W3G: draw an overflow chevron button ('»'). Returns true if clicked.
static bool ribbon_overflowChevron(float x, float y, float bh) {
    float s = ui::uiScale;
    float btnW = 20.0f * s;
    ImGui::SetCursorPos(ImVec2(x, y));
    ImGui::PushID("##overflow_chevron");
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(0, 0));
    ImGui::PushStyleColor(ImGuiCol_Button,        ImVec4(0,0,0,0));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(0,0,0,0));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,  ImVec4(0,0,0,0));
    bool clicked = ImGui::InvisibleButton("##chev", ImVec2(btnW, bh));
    bool hov = ImGui::IsItemHovered();
    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar();

    ImVec2 p0 = ImGui::GetItemRectMin();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    if (hov)
        dl->AddRectFilled(p0, ImVec2(p0.x + btnW, p0.y + bh),
                          ui::Palette::toU32(ui::palette.buttonHovered),
                          ui::metrics::ribbonBtnRound());
    ImU32 tCol = ui::Palette::toU32(ui::palette.text);
    float cx = p0.x + btnW * 0.5f;
    float cy = p0.y + bh * 0.5f;
    float fontSize = ImGui::GetFontSize();
    dl->AddText(nullptr, 0.0f, ImVec2(cx - fontSize * 0.3f, cy - fontSize * 0.5f), tCol, "\xc2\xbb");

    if (hov) ImGui::SetTooltip("More commands");
    ImGui::PopID();
    return clicked;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

namespace ui {

void setRibbonCallback(std::function<void(const char* cmd_id)> cb) {
    s_RibbonCb = std::move(cb);
}

float ribbonHeight() {
    float s = ui::uiScale;
    return (kTabH + kBodyH + 8.0f) * s;
}

void setRibbonActiveCmd(const char* cmd_id) {
    s_activeCmd = (cmd_id && *cmd_id) ? cmd_id : "";
}

void setRibbonSketchActive(bool active) {
    s_sketchActive = active;
}

float drawRibbon(int& activeTab, bool visible, bool showSimulationTools, bool showPcbTools,
                 float titleBarH, bool showDrawingTools) {
    if (!visible) return 0.0f;
    ribbon_seedDefaults();
    if (activeTab < 0 || activeTab >= kRibbonTabCount ||
        !ribbon_tabVisible(activeTab, showSimulationTools, showPcbTools, showDrawingTools)) {
        // Land on the first visible tab — in drawing mode tab 0 is hidden, so pick the
        // first drawing tab; on a 3D doc fall back to Features.
        activeTab = 0;
        for (int i = 0; i < kRibbonTabCount; ++i)
            if (ribbon_tabVisible(i, showSimulationTools, showPcbTools, showDrawingTools)) {
                activeTab = i;
                break;
            }
    }

    float s      = ui::uiScale;
    float tabH   = kTabH  * s;
    float bodyH2 = kBodyH * s;
    float totalH = tabH + bodyH2 + 8.0f * s;

    ImGuiViewport* vp = ImGui::GetMainViewport();
    // Use the caller-provided ACTUAL title-bar height. The old self-computed GetFrameHeight() used
    // the default frame padding, but drawTitleBar pushes a taller FramePadding.y, so the ribbon was
    // positioned ~2*(padDelta) px too high and clipped its tab strip up under the menu bar.
    if (titleBarH < 0.0f) titleBarH = ImGui::GetFrameHeight();

    ImGui::SetNextWindowPos(ImVec2(vp->Pos.x, vp->Pos.y + titleBarH));
    ImGui::SetNextWindowSize(ImVec2(vp->Size.x, totalH));
    ImGui::SetNextWindowViewport(vp->ID);

    ImGuiWindowFlags flags =
        ImGuiWindowFlags_NoTitleBar        | ImGuiWindowFlags_NoCollapse |
        ImGuiWindowFlags_NoResize          | ImGuiWindowFlags_NoMove     |
        ImGuiWindowFlags_NoBringToFrontOnFocus | ImGuiWindowFlags_NoNavFocus |
        ImGuiWindowFlags_NoDocking         | ImGuiWindowFlags_NoScrollbar |
        ImGuiWindowFlags_NoScrollWithMouse;

    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding,   0.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,    ImVec2(0, 0));
    ImGui::PushStyleColor(ImGuiCol_WindowBg,
        ImVec4(palette.ribbonBg[0], palette.ribbonBg[1],
               palette.ribbonBg[2], palette.ribbonBg[3]));

    ImGui::Begin("##RibbonBar", nullptr, flags);
    ImGui::PopStyleVar(3);
    ImGui::PopStyleColor();

    ImDrawList* dl   = ImGui::GetWindowDrawList();
    ImVec2      wPos = ImGui::GetWindowPos();

    // --- Tab strip ---
    float tabTopPad = 0.0f;
    float tabBtnH   = tabH;
    float tabX = wPos.x + 6.0f * s;
    float tabY = wPos.y + tabTopPad;

    for (int i = 0; i < kRibbonTabCount; ++i) {
        if (!ribbon_tabVisible(i, showSimulationTools, showPcbTools, showDrawingTools)) continue;
        const char* tabLabel = kRibbonTabs[i];
        ImVec2 tsize  = ImGui::CalcTextSize(tabLabel);
        float  tw     = tsize.x + 22.0f * s + 18.0f * s + 4.0f * s;  // + 14px icon + gap + pad
        bool   isAct  = (activeTab == i);

        ImVec2 tp0(tabX, tabY);
        ImVec2 tp1(tabX + tw, tabY + tabBtnH);

        ImGui::SetCursorScreenPos(tp0);
        ImGui::PushID(i + 1000);
        if (ImGui::InvisibleButton(tabLabel, ImVec2(tw, tabBtnH))) activeTab = i;
        bool tabHov = ImGui::IsItemHovered();
        ImGui::PopID();

        if (isAct || tabHov) {
            ImU32 bg = isAct ? Palette::toU32(palette.ribbonTabActive)
                             : Palette::toU32(palette.buttonHovered);
            dl->AddRectFilled(tp0, tp1, bg, ui::metrics::ribbonBtnRound(),
                isAct ? ImDrawFlags_RoundCornersTop : 0);
        }
        if (isAct) {
            float tabInset = ui::metrics::ribbonSmSplitInset();
            dl->AddLine(ImVec2(tp0.x + tabInset, tp1.y - ui::metrics::px(1.0f)),
                        ImVec2(tp1.x - tabInset, tp1.y - ui::metrics::px(1.0f)),
                        Palette::toU32(palette.accent), ui::metrics::stroke(2.0f));
        }
        ImVec4 lc = (isAct || tabHov)
            ? ImVec4(palette.text[0],    palette.text[1],    palette.text[2],    palette.text[3])
            : ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], palette.textDim[3]);
        const float textY = snap(tp0.y + (tabBtnH - tsize.y) * 0.5f - 1.0f * s);
        // Per-workspace tab icon: a 14x14 glyph snapped to the left of the centered label, tinted to
        // match the label color (bright when active/hovered, dim otherwise).
        const float tIconSz   = 14.0f * s;
        const float tIconGap  = 4.0f * s;
        const float tContentW = tIconSz + tIconGap + tsize.x;
        const float tGroupX   = snap(tp0.x + (tw - tContentW) * 0.5f);
        ui::IconUV  tic       = ui::getIcon(kTabIcons[i], 14);
        const float tIconY    = snap(tp0.y + (tabBtnH - tIconSz) * 0.5f - 1.0f * s);
        ImU32       tIconTint = ImGui::ColorConvertFloat4ToU32(lc);
        dl->AddImage(tic.texId, ImVec2(tGroupX, tIconY),
                     ImVec2(tGroupX + tIconSz, tIconY + tIconSz), tic.uv0, tic.uv1, tIconTint);
        dl->AddText(nullptr, 0.0f,
                    ImVec2(snap(tGroupX + tIconSz + tIconGap), textY),
                    ImGui::ColorConvertFloat4ToU32(lc), tabLabel);

        tabX += tw + 2.0f * s;
    }

    dl->AddLine(ImVec2(wPos.x,              wPos.y + tabH),
                ImVec2(wPos.x + vp->Size.x, wPos.y + tabH),
                Palette::toU32(palette.border), ui::metrics::stroke(1.0f));

    // --- Build group list ---
    int cmdCount = 0;
    const RibbonCmd* cmds = getRibbonCmds(cmdCount);
    const char* actTabName = kRibbonTabs[activeTab];

    std::vector<RibbonGroup> groups;
    groups.reserve(16);

    // First pass: collect the set of replacement-parent cmd_ids present per (tab,group) so we can
    // hide the superseded small rows (defect 3 — duplicate Ellipse) without deleting table rows.
    auto smallIsHidden = [&](const RibbonCmd& c) -> bool {
        for (const auto& hs : kHiddenSmallByParent) {
            if (std::strcmp(hs.hiddenId, c.cmd_id) != 0) continue;
            // hide only if the replacement parent exists in the same tab+group
            for (int j = 0; j < cmdCount; ++j) {
                const RibbonCmd& o = cmds[j];
                if (std::strcmp(o.cmd_id, hs.parentId) == 0 &&
                    std::strcmp(o.tab, c.tab) == 0 &&
                    std::strcmp(o.group, c.group) == 0)
                    return true;
            }
        }
        return false;
    };

    for (int i = 0; i < cmdCount; ++i) {
        const RibbonCmd& c = cmds[i];
        if (std::strcmp(c.tab, actTabName) != 0) continue;
        // Sketch tab, no sketch being edited: present "New Sketch" in the "Exit Sketch" slot so the
        // user can start a sketch from this tab (matches the Features-tab entry point). Once a sketch
        // is open, s_sketchActive is true and the real "Exit Sketch" button shows.
        const RibbonCmd* cp = &c;
        if (!s_sketchActive && std::strcmp(c.cmd_id, "sketch_exit") == 0)
            cp = &kSketchNewOnSketchTab;
        RibbonGroup& grp = ribbon_findOrAddGroup(groups, cp->group);
        if (cp->sub_of)  grp.subCmds.push_back(cp);
        else if (cp->big) grp.bigCmds.push_back(cp);
        else if (!smallIsHidden(*cp)) grp.smCmds.push_back(cp);
    }

    // --- C25-W3G: priority collapse assignment ---
    // Available body width. Reserve the overflow chevron up front so it never lands off-edge once
    // any group has to be hidden.
    float chevronW  = 22.0f * s;  // reserved for overflow '»' button
    float availW    = vp->Size.x - 8.0f * s - chevronW;

    // Compute natural widths at COL_BIG (per-column small-zone measuring — defect 1)
    std::vector<float> natW(groups.size());
    for (int gi = 0; gi < (int)groups.size(); ++gi)
        natW[gi] = ribbon_groupNaturalW(groups[gi], COL_BIG);

    float totalW = 0.0f;
    for (float w : natW) totalW += w;

    // Assign collapse levels: degrade rightmost groups first (SolidWorks priority order).
    // Pass 1: step rightmost groups from BIG -> SMALL
    if (totalW > availW) {
        for (int gi = (int)groups.size() - 1; gi >= 0 && totalW > availW; --gi) {
            float wBig   = ribbon_groupNaturalW(groups[gi], COL_BIG);
            float wSmall = ribbon_groupNaturalW(groups[gi], COL_SMALL);
            if (wSmall < wBig) {
                totalW -= wBig - wSmall;
                groups[gi].collapse = COL_SMALL;
                natW[gi] = wSmall;
            }
        }
    }
    // Pass 2: hide rightmost groups into overflow
    if (totalW > availW) {
        for (int gi = (int)groups.size() - 1; gi >= 0 && totalW > availW; --gi) {
            if (groups[gi].collapse != COL_HIDDEN) {
                totalW -= natW[gi];
                groups[gi].collapse = COL_HIDDEN;
                natW[gi] = 0.0f;
            }
        }
    }

    // Determine if we need the chevron
    bool anyHidden = false;
    for (const auto& g : groups) if (g.collapse == COL_HIDDEN) { anyHidden = true; break; }

    // --- Body child window (no horizontal scroll — reflow handles it) ---
    ImGui::SetCursorScreenPos(ImVec2(wPos.x + 4.0f * s, wPos.y + tabH + 2.0f * s));
    ImGui::BeginChild("##RibbonBody",
                      ImVec2(vp->Size.x - 8.0f * s, bodyH2 + 4.0f * s),
                      false,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);

    float curX = 0.0f;

    for (int gi = 0; gi < (int)groups.size(); ++gi) {
        RibbonGroup& grp = groups[gi];
        if (grp.collapse == COL_HIDDEN) continue;

        bool isSmall = (grp.collapse != COL_BIG);
        float grpW = natW[gi];
        int rowsPerCol = ribbon_rowsPerCol();

        // Resolve the face icon/label of a split (drop) button, honoring last-used variant.
        auto resolveFace = [&](const ui::RibbonCmd* cmd, bool drop,
                               const char*& showIcon, const char*& showLabel) {
            showIcon = cmd->icon; showLabel = cmd->label;
            if (!drop) return;
            auto it = s_LastUsed.find(cmd->cmd_id);
            if (it == s_LastUsed.end() || it->second.empty()) return;
            for (const auto* sc : grp.subCmds)
                if (std::strcmp(sc->cmd_id, it->second.c_str()) == 0) {
                    showIcon = sc->icon; showLabel = sc->label; return;
                }
        };
        // Run a split button's flyout + fire chosen variant.
        auto runFlyout = [&](const ui::RibbonCmd* cmd) {
            std::vector<const ui::RibbonCmd*> mySubs;
            for (const auto* sc : grp.subCmds)
                if (std::strcmp(sc->sub_of, cmd->cmd_id) == 0) mySubs.push_back(sc);
            const char* chosen = ribbon_flyoutPopup(cmd->cmd_id, mySubs);
            if (chosen) { s_LastUsed[cmd->cmd_id] = chosen; ribbon_fire(chosen); }
        };

        if (!isSmall) {
            // --- BIG layout: big buttons left-to-right, then the small zone in measured columns ---
            float bx = curX + kGroupPadX * s;
            for (const auto* cmd : grp.bigCmds) {
                bool drop = ribbon_hasSubs(grp, cmd);
                ImGui::SetCursorPos(ImVec2(bx, 0.0f));
                const char* showIcon; const char* showLabel;
                resolveFace(cmd, drop, showIcon, showLabel);
                ribbon_bigBtn(cmd->cmd_id, showIcon, showLabel, !cmd->stretch, drop);
                if (drop) runFlyout(cmd);
                bx += ribbon_bigBtnW(cmd->cmd_id, cmd->label, drop) + 2.0f * s;
            }

            // small zone: pack into independently-measured columns (defect 1)
            if (!grp.smCmds.empty()) {
                std::vector<float> widths;
                for (const auto* cmd : grp.smCmds) widths.push_back(ribbon_smBtnW(cmd->label));
                auto cols = ribbon_packColumns(widths, rowsPerCol);
                float sx = bx;
                for (const auto& col : cols) {
                    float sy = 0.0f;
                    for (int r = 0; r < col.count; ++r) {
                        const auto* cmd = grp.smCmds[col.first + r];
                        ImGui::SetCursorPos(ImVec2(sx, sy));
                        ribbon_smBtn(cmd->cmd_id, cmd->icon, cmd->label, !cmd->stretch);
                        sy += kSmRowH * s;
                    }
                    sx += col.width;
                }
            }
        } else {
            // --- SMALL (degraded) layout: big cmds degrade to small rows, then small cmds. All
            // flow into one combined column-packed stack (defect 1 — per-column widths). ---
            std::vector<const ui::RibbonCmd*> rows = grp.bigCmds;
            for (const auto* cmd : grp.smCmds) rows.push_back(cmd);

            std::vector<float> widths;
            int nBig = (int)grp.bigCmds.size();
            for (int i = 0; i < (int)rows.size(); ++i) {
                float w = ribbon_smBtnW(rows[i]->label);
                if (i < nBig && ribbon_hasSubs(grp, rows[i])) w += kDropArrowW * s;
                widths.push_back(w);
            }
            auto cols = ribbon_packColumns(widths, rowsPerCol);
            float sx = curX + kGroupPadX * s;
            for (const auto& col : cols) {
                float sy = 0.0f;
                for (int r = 0; r < col.count; ++r) {
                    int idx = col.first + r;
                    const auto* cmd = rows[idx];
                    ImGui::SetCursorPos(ImVec2(sx, sy));
                    if (idx < nBig) {
                        bool drop = ribbon_hasSubs(grp, cmd);
                        const char* showIcon; const char* showLabel;
                        resolveFace(cmd, drop, showIcon, showLabel);
                        ribbon_bigBtnAsSmall(cmd->cmd_id, showIcon, showLabel, !cmd->stretch, drop);
                        if (drop) runFlyout(cmd);
                    } else {
                        ribbon_smBtn(cmd->cmd_id, cmd->icon, cmd->label, !cmd->stretch);
                    }
                    // §2.3: degraded big rows are taller than kSmRowH — advance by the actual height.
                    sy += (idx < nBig) ? ribbon_bigBtnAsSmallRowH() : kSmRowH * s;
                }
                sx += col.width;
            }
        }

        // --- Group separator + label ---
        {
            ImVec2 cwinPos = ImGui::GetWindowPos();
            float  sepX    = snap(cwinPos.x + curX + grpW - kGroupPadX * s * 0.5f);
            ImVec4 sepMain(palette.border[0], palette.border[1], palette.border[2], 0.92f);
            ImVec4 sepHi(palette.accent[0], palette.accent[1], palette.accent[2], 0.24f);
            ImU32  sepCol  = ImGui::ColorConvertFloat4ToU32(sepMain);
            ImU32  sepHiCol = ImGui::ColorConvertFloat4ToU32(sepHi);
            float  sepInset = ui::metrics::ribbonSplitInset();
            dl->AddLine(ImVec2(sepX + ui::metrics::px(1.0f), cwinPos.y + sepInset + ui::metrics::px(4.0f)),
                        ImVec2(sepX + ui::metrics::px(1.0f), cwinPos.y + bodyH2 - sepInset - ui::metrics::px(4.0f)),
                        sepHiCol, ui::metrics::stroke(1.0f));
            dl->AddLine(ImVec2(sepX, cwinPos.y + sepInset),
                        ImVec2(sepX, cwinPos.y + bodyH2 - sepInset), sepCol,
                        ui::metrics::stroke(1.25f));

            // Defect 2: centered caption. grpW already includes a caption-width floor, so labels
            // such as "Dimension" never truncate at COL_BIG/COL_SMALL. The clip rect is a safety
            // net; if it ever clips (only possible in a degraded edge case) a tooltip is offered.
            if (grp.collapse != COL_LABEL) {
                ImVec2 gts = ImGui::CalcTextSize(grp.label);
                float  capL = cwinPos.x + curX + kGroupPadX * s * 0.5f;
                float  capR = cwinPos.x + curX + grpW - kGroupPadX * s * 0.5f;
                float  glx = snap(capL + ((capR - capL) - gts.x) * 0.5f);
                if (glx < capL) glx = snap(capL);
                float  gly = snap(cwinPos.y + bodyH2 - gts.y - 2.0f * s);
                bool clipped = gts.x > (capR - capL);
                dl->PushClipRect(ImVec2(capL, gly), ImVec2(capR, gly + gts.y), true);
                dl->AddText(nullptr, 0.0f, ImVec2(glx, gly),
                            Palette::toU32(palette.textDim), grp.label);
                dl->PopClipRect();
                if (clipped &&
                    ImGui::IsMouseHoveringRect(ImVec2(capL, gly), ImVec2(capR, gly + gts.y)))
                    ImGui::SetTooltip("%s", grp.label);
            }
        }

        curX += grpW;
    }

    // --- C25-W3G: overflow chevron ---
    if (anyHidden) {
        float bh = (kBodyH - kCaptionH) * s;
        if (ribbon_overflowChevron(curX + 2.0f * s, 0.0f, bh))
            ImGui::OpenPopup("##ribbon_overflow");

        if (ImGui::BeginPopup("##ribbon_overflow", ImGuiWindowFlags_NoMove)) {
            // Draw hidden groups as flyout rows using the same flyout style
            for (int gi = 0; gi < (int)groups.size(); ++gi) {
                const RibbonGroup& grp = groups[gi];
                if (grp.collapse != COL_HIDDEN) continue;

                // Section header
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(palette.textDim[0], palette.textDim[1],
                           palette.textDim[2], palette.textDim[3]));
                ImGui::TextUnformatted(grp.label);
                ImGui::PopStyleColor();

                for (const auto* cmd : grp.bigCmds) {
                    bool enabled = !cmd->stretch;
                    ImVec2 cp0 = ImGui::GetCursorScreenPos();
                    float rowH   = snap(28.0f * s);
                    float iconSz = snap(20.0f * s);
                    float padX   = snap(8.0f * s);
                    float gapX   = snap(6.0f * s);
                    float popW   = padX + iconSz + gapX +
                                   ImGui::CalcTextSize(cmd->label).x + padX;

                    ImGui::PushID(cmd->cmd_id);
                    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(0, 0));
                    ImGui::PushStyleColor(ImGuiCol_Button,        ImVec4(0,0,0,0));
                    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(0,0,0,0));
                    ImGui::PushStyleColor(ImGuiCol_ButtonActive,  ImVec4(0,0,0,0));
                    bool rowClicked = ImGui::InvisibleButton("##ovrow", ImVec2(popW, rowH));
                    bool rowHov     = ImGui::IsItemHovered();
                    ImGui::PopStyleColor(3);
                    ImGui::PopStyleVar();

                    ImDrawList* odl = ImGui::GetWindowDrawList();
                    if (rowHov && enabled)
                        odl->AddRectFilled(cp0, ImVec2(cp0.x + popW, cp0.y + rowH),
                                           ui::Palette::toU32(ui::palette.buttonHovered),
                                           ui::metrics::ribbonBtnRound());
                    {
                        float ix = snap(cp0.x + padX);
                        float iy = snap(cp0.y + (rowH - iconSz) * 0.5f);
                        ui::IconUV ic = ui::getIcon(cmd->icon, 20);
                        ImU32 tint = enabled ? IM_COL32(255,255,255,255) : IM_COL32(128,128,128,160);
                        odl->AddImage(ic.texId, ImVec2(ix, iy), ImVec2(ix + iconSz, iy + iconSz),
                                      ic.uv0, ic.uv1, tint);
                    }
                    {
                        float tx = snap(cp0.x + padX + iconSz + gapX);
                        float ty = snap(cp0.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f);
                        ImU32 tCol = enabled ? ui::Palette::toU32(ui::palette.text)
                                             : ui::Palette::toU32(ui::palette.textDisabled);
                        odl->AddText(nullptr, 0.0f, ImVec2(tx, ty), tCol, cmd->label);
                    }
                    if (rowClicked && enabled) {
                        ribbon_fire(cmd->cmd_id);
                        ImGui::CloseCurrentPopup();
                    }
                    ImGui::PopID();
                }
                for (const auto* cmd : grp.smCmds) {
                    bool enabled = !cmd->stretch;
                    ImVec2 cp0 = ImGui::GetCursorScreenPos();
                    float rowH   = snap(28.0f * s);
                    float iconSz = snap(20.0f * s);
                    float padX   = snap(8.0f * s);
                    float gapX   = snap(6.0f * s);
                    float popW   = padX + iconSz + gapX +
                                   ImGui::CalcTextSize(cmd->label).x + padX;

                    ImGui::PushID(cmd->cmd_id);
                    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(0, 0));
                    ImGui::PushStyleColor(ImGuiCol_Button,        ImVec4(0,0,0,0));
                    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(0,0,0,0));
                    ImGui::PushStyleColor(ImGuiCol_ButtonActive,  ImVec4(0,0,0,0));
                    bool rowClicked = ImGui::InvisibleButton("##ovsmrow", ImVec2(popW, rowH));
                    bool rowHov     = ImGui::IsItemHovered();
                    ImGui::PopStyleColor(3);
                    ImGui::PopStyleVar();

                    ImDrawList* odl = ImGui::GetWindowDrawList();
                    if (rowHov && enabled)
                        odl->AddRectFilled(cp0, ImVec2(cp0.x + popW, cp0.y + rowH),
                                           ui::Palette::toU32(ui::palette.buttonHovered),
                                           ui::metrics::ribbonBtnRound());
                    {
                        float ix = snap(cp0.x + padX);
                        float iy = snap(cp0.y + (rowH - iconSz) * 0.5f);
                        ui::IconUV ic = ui::getIcon(cmd->icon, 20);
                        ImU32 tint = enabled ? IM_COL32(255,255,255,255) : IM_COL32(128,128,128,160);
                        odl->AddImage(ic.texId, ImVec2(ix, iy), ImVec2(ix + iconSz, iy + iconSz),
                                      ic.uv0, ic.uv1, tint);
                    }
                    {
                        float tx = snap(cp0.x + padX + iconSz + gapX);
                        float ty = snap(cp0.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f);
                        ImU32 tCol = enabled ? ui::Palette::toU32(ui::palette.text)
                                             : ui::Palette::toU32(ui::palette.textDisabled);
                        odl->AddText(nullptr, 0.0f, ImVec2(tx, ty), tCol, cmd->label);
                    }
                    if (rowClicked && enabled) {
                        ribbon_fire(cmd->cmd_id);
                        ImGui::CloseCurrentPopup();
                    }
                    ImGui::PopID();
                }
                ImGui::Separator();
            }
            ImGui::EndPopup();
        }
    }
    (void)chevronW;

    ImGui::EndChild();
    ImGui::End();

    return totalH;
}

}  // namespace ui
