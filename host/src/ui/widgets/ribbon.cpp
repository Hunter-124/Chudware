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

static const char* kRibbonTabs[] = {"Features", "Sketch", "Evaluate", "Simulation", "Assembly"};
static constexpr int kRibbonTabCount = 5;

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
    {"sk_ellipse_big", "sk_ellipse", "Ellipse", "ellipse"},
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
static constexpr float kBigIconSz   = 32.0f;
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
static float ribbon_bigBtnW(const char* label, bool hasDrop) {
    float s = ui::uiScale;
    std::string l1, l2;
    float lblW = ribbon_wrapLabel(label, l1, l2) + kBigLblPad * 2.0f;
    float base = kBigBtnW;
    if (lblW > base) base = lblW;
    if (base > kBigBtnMaxW) base = kBigBtnMaxW;
    return base * s + (hasDrop ? kDropArrowW * s : 0.0f);
}

// Width of a small button in screen pixels.
// Mirrors the hit/draw geometry in ribbon_smBtn (icon + 4px gap + label + trailing pad).
static float ribbon_smBtnW(const char* label) {
    float s = ui::uiScale;
    return kSmIconSz * s + 4.0f * s + ImGui::CalcTextSize(label).x + 14.0f * s;
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
            bigColW += ribbon_bigBtnW(cmd->label, drop) + 2.0f * s;
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
    };
    for (const auto& t : tips)
        if (std::strcmp(t.id, cmd_id) == 0) return t.tip;
    return nullptr;
}

static void ribbon_tooltip(const char* title, const char* cmd_id, bool enabled) {
    auto col = [](const float f[4]) { return ImVec4(f[0], f[1], f[2], f[3]); };
    ImGui::BeginTooltip();
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
    ImGui::EndTooltip();
}

// Draw a SW big button. Returns true if main area clicked.
static bool ribbon_bigBtn(const char* cmd_id, const char* iconName,
                           const char* label, bool enabled, bool hasDrop) {
    float s   = ui::uiScale;
    float bw  = ribbon_bigBtnW(label, hasDrop);
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

    if (hasDrop) {
        ImGui::SameLine(0, 0);
        dropClicked = ImGui::InvisibleButton("##drop", ImVec2(kArrowW * s, bh));
        bool dropHov = ImGui::IsItemHovered();

        ImDrawList* dl = ImGui::GetWindowDrawList();
        if ((mainHov || dropHov) && enabled) {
            ImU32 hBg = ui::Palette::toU32(ui::palette.buttonHovered);
            dl->AddRectFilled(p0, ImVec2(p0.x + bw, p0.y + bh), hBg, 3.0f);
        }
        {
            float lx = p0.x + mainW;
            dl->AddLine(ImVec2(lx, p0.y + 4), ImVec2(lx, p0.y + bh - 4),
                        ui::Palette::toU32(ui::palette.border), 1.0f);
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
            dl->AddRectFilled(p0, ImVec2(p0.x + bw, p0.y + bh), hBg, 3.0f);
        }
    }

    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar(2);

    // Draw icon + label
    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        float iconSz = snap(kBigIconSz * s);
        float cx     = snap(p0.x + mainW * 0.5f);
        float iy     = snap(p0.y + 4.0f * s);

        ui::IconUV ic = ui::getIcon(iconName, 32);
        ImU32 tint = enabled ? IM_COL32(255,255,255,255) : IM_COL32(128,128,128,160);
        dl->AddImage(ic.texId,
                     ImVec2(snap(cx - iconSz * 0.5f), iy),
                     ImVec2(snap(cx - iconSz * 0.5f) + iconSz, iy + iconSz),
                     ic.uv0, ic.uv1, tint);

        std::string l1, l2;
        ribbon_wrapLabel(label, l1, l2);
        ImU32 tCol = enabled ? ui::Palette::toU32(ui::palette.text)
                             : ui::Palette::toU32(ui::palette.textDisabled);
        float lineH = ImGui::GetTextLineHeight();
        float ty    = iy + iconSz + 2.0f * s;
        dl->PushClipRect(ImVec2(p0.x, p0.y), ImVec2(p0.x + mainW, p0.y + bh), true);
        {
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
                // No last-used — open flyout so the user can pick a variant.
                s_flyoutOpenReq = cmd_id;   // deferred (ID-scope fix)
            }
        } else {
            ribbon_fire(cmd_id);
        }
    }

    ImGui::PopID();
    return clicked && enabled;
}

// Draw a big button degraded to small-row layout (icon + side label, one row height).
static bool ribbon_bigBtnAsSmall(const char* cmd_id, const char* iconName,
                                  const char* label, bool enabled, bool hasDrop) {
    float s      = ui::uiScale;
    float rowH   = snap((kBodyH - kCaptionH) * s / 3.0f);  // fit ~3 per body zone
    rowH = ImMax(rowH, kSmRowH * s);
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

    if (hasDrop) {
        ImGui::SameLine(0, 0);
        dropClicked = ImGui::InvisibleButton("##mdr", ImVec2(kDropArrowW * s, rowH));
        bool dropHov = ImGui::IsItemHovered();
        ImDrawList* dl = ImGui::GetWindowDrawList();
        if ((mainHov || dropHov) && enabled)
            dl->AddRectFilled(p0, ImVec2(p0.x + totalW, p0.y + rowH),
                              ui::Palette::toU32(ui::palette.buttonHovered), 3.0f);
        {
            float lx = p0.x + mainW;
            dl->AddLine(ImVec2(lx, p0.y + 2), ImVec2(lx, p0.y + rowH - 2),
                        ui::Palette::toU32(ui::palette.border), 1.0f);
        }
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
                              ui::Palette::toU32(ui::palette.buttonHovered), 3.0f);
        }
    }

    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar();

    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        float ix = snap(p0.x + 2.0f * s);
        float iy = snap(p0.y + (rowH - iconSz) * 0.5f);
        ui::IconUV ic = ui::getIcon(iconName, 20);
        ImU32 tint = enabled ? IM_COL32(255,255,255,255) : IM_COL32(128,128,128,160);
        dl->AddImage(ic.texId, ImVec2(ix, iy), ImVec2(ix + iconSz, iy + iconSz),
                     ic.uv0, ic.uv1, tint);
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
                s_flyoutOpenReq = cmd_id;   // deferred (ID-scope fix)
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
                                  ui::Palette::toU32(ui::palette.buttonHovered), 3.0f);

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
    float totalW = iconSz + ImGui::CalcTextSize(label).x + 10.0f * s;

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

    if (hov && enabled) {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        dl->AddRectFilled(p0, ImVec2(p0.x + totalW, p0.y + rowH),
                          ui::Palette::toU32(ui::palette.buttonHovered), 3.0f);
    }

    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        float snappedIconSz = snap(iconSz);
        float ix  = snap(p0.x);
        float iy  = snap(p0.y + (rowH - snappedIconSz) * 0.5f);
        ui::IconUV ic = ui::getIcon(iconName, 20);
        ImU32 tint = enabled ? IM_COL32(255,255,255,255) : IM_COL32(128,128,128,160);
        dl->AddImage(ic.texId, ImVec2(ix, iy), ImVec2(ix + snappedIconSz, iy + snappedIconSz),
                     ic.uv0, ic.uv1, tint);
        float tx  = snap(p0.x + snappedIconSz + 4.0f * s);
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
                          ui::Palette::toU32(ui::palette.buttonHovered), 3.0f);
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

float drawRibbon(int& activeTab, bool visible) {
    if (!visible) return 0.0f;
    ribbon_seedDefaults();

    float s      = ui::uiScale;
    float tabH   = kTabH  * s;
    float bodyH2 = kBodyH * s;
    float totalH = tabH + bodyH2 + 8.0f * s;

    ImGuiViewport* vp = ImGui::GetMainViewport();
    float titleBarH   = ImGui::GetFrameHeight();

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
    float tabTopPad = 3.0f * s;
    float tabBtnH   = tabH - tabTopPad;
    float tabX = wPos.x + 6.0f * s;
    float tabY = wPos.y + tabTopPad;

    for (int i = 0; i < kRibbonTabCount; ++i) {
        const char* tabLabel = kRibbonTabs[i];
        ImVec2 tsize  = ImGui::CalcTextSize(tabLabel);
        float  tw     = tsize.x + 22.0f * s;
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
            dl->AddRectFilled(tp0, tp1, bg, 3.0f,
                isAct ? ImDrawFlags_RoundCornersTop : 0);
        }
        if (isAct) {
            dl->AddLine(ImVec2(tp0.x + 2, tp1.y - 1),
                        ImVec2(tp1.x - 2, tp1.y - 1),
                        Palette::toU32(palette.accent), 2.0f);
        }
        ImVec4 lc = (isAct || tabHov)
            ? ImVec4(palette.text[0],    palette.text[1],    palette.text[2],    palette.text[3])
            : ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], palette.textDim[3]);
        dl->AddText(nullptr, 0.0f,
                    ImVec2(tp0.x + (tw - tsize.x) * 0.5f,
                           tp0.y + (tabBtnH - tsize.y) * 0.5f),
                    ImGui::ColorConvertFloat4ToU32(lc), tabLabel);

        tabX += tw + 2.0f * s;
    }

    dl->AddLine(ImVec2(wPos.x,              wPos.y + tabH),
                ImVec2(wPos.x + vp->Size.x, wPos.y + tabH),
                Palette::toU32(palette.border), 1.0f);

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
        RibbonGroup& grp = ribbon_findOrAddGroup(groups, c.group);
        if (c.sub_of)  grp.subCmds.push_back(&c);
        else if (c.big) grp.bigCmds.push_back(&c);
        else if (!smallIsHidden(c)) grp.smCmds.push_back(&c);
    }

    // --- C25-W3G: priority collapse assignment ---
    // Available body width = viewport width minus chevron reserve (20px scaled)
    float availW    = vp->Size.x - 8.0f * s;
    float chevronW  = 22.0f * s;  // reserved for overflow '»' button

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
                bx += ribbon_bigBtnW(cmd->label, drop) + 2.0f * s;
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
                    sy += kSmRowH * s;
                }
                sx += col.width;
            }
        }

        // --- Group separator + label ---
        {
            ImVec2 cwinPos = ImGui::GetWindowPos();
            float  sepX    = snap(cwinPos.x + curX + grpW - kGroupPadX * s * 0.5f);
            ImU32  sepCol  = Palette::toU32(palette.border);
            dl->AddLine(ImVec2(sepX, cwinPos.y + 4),
                        ImVec2(sepX, cwinPos.y + bodyH2 - 4), sepCol, 1.0f);

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
                                           ui::Palette::toU32(ui::palette.buttonHovered), 3.0f);
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
                                           ui::Palette::toU32(ui::palette.buttonHovered), 3.0f);
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
