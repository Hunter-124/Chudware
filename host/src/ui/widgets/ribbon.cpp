// Chudware ribbon widget — C14/C25.
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
#include "../layout.h"        // ui::setEvalRibbonDock (Evaluate-tab inspector dock strip)
#include "../web_capabilities.h"  // ui::webGapReason (T-RF12: WASM-build ribbon gating)
#include "kit.h"
#ifdef __EMSCRIPTEN__
#include <emscripten.h>
#endif

#include <algorithm>
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
                                    "Sheet Metal", "Weldments", "PCB Design", "Surfaces",
                                    "View Layout", "Annotation", "Appearance", "Manufacture",
                                    // sim-gui rework: per-simulation-type tabs, shown ONLY in the
                                    // Simulate workspace (indices 13..19 — see ribbon_tabVisible).
                                    "Static", "Modal", "Flow", "Thermal", "Fatigue", "Motion",
                                    "EM",
                                    // ECAD split: the old single "PCB" tab (index 7, now "PCB Design")
                                    // was split — this is the second half. Appended at index 20 to keep
                                    // the sim per-type tab indices 13..19 stable (append-only).
                                    "Schematic",
                                    // Topology optimization study tab (index 21, Simulate workspace
                                    // only — see ribbon_tabVisible). Appended to keep every index stable.
                                    "Topology",
                                    // Piping networks + internal-flow CFD + vessel/duct sizing (index 22,
                                    // Simulate workspace only). Appended to keep every index stable.
                                    "Piping",
                                    // Hydraulic (fluid-power) circuits: components, sizing, transient
                                    // simulation, BOM, piping handoff (index 23, Simulate only).
                                    "Hydraulics"};
static constexpr int kRibbonTabCount = 24;  // + W17 contextual "View Layout"/"Annotation" (drawings)
                                            //   + "Appearance" (index 11) + "Manufacture" (CAM, 12)
                                            //   + sim-gui per-type tabs (13..19, Simulate only)
                                            //   + "Topology" (21, Simulate only)
                                            //   + "Piping" (22, Simulate only)
                                            //   + "Hydraulics" (23, Simulate only)

// W-overhaul: per-workspace tab icon, parallel to kRibbonTabs[]. The eight true workspace tabs use
// the dedicated ws_* atlas glyphs from the icon contract; PCB and the two contextual drawing tabs
// reuse the closest existing atlas icon as a placeholder until dedicated glyphs ship.
static const char* kTabIcons[] = {
    "ws_features",    // Features
    "ws_sketch",      // Sketch
    "ws_evaluate",    // Evaluate
    "ws_simulate",    // Simulation (legacy all-in-one sim tab)
    "ws_assembly",    // Assembly
    "ws_sheetmetal",  // Sheet Metal
    "ws_weldments",   // Weldments
    "ai_panel",       // PCB Design (placeholder — no ws_pcb glyph in the contract)
    "ws_surfaces",    // Surfaces
    "view_selector",  // View Layout (drawing — placeholder)
    "smart_dim",      // Annotation (drawing — placeholder)
    "ws_appearance",  // Appearance
    "hole_wizard",    // Manufacture (CAM — placeholder, machining glyph)
    // Simulate tabs: each wears its own study glyph — the same one its "New Study" button shows.
    // Motion keeps the assembly "mate" glyph (a motion study drives a mate).
    "study_static",      // Static
    "study_modal",       // Modal
    "study_flow",        // Flow
    "study_thermal_fea", // Thermal (thermal FEA)
    "study_fatigue",     // Fatigue
    "mate",              // Motion
    "study_em",          // EM
    "sketch",            // Schematic (index 20 — reuse sketch glyph; no ws_schematic glyph yet)
    "study_topology",    // Topology (index 21)
    "study_piping",      // Piping (index 22)
    "study_hydraulics",  // Hydraulics (index 23)
};
static_assert(sizeof(kTabIcons) / sizeof(kTabIcons[0]) == kRibbonTabCount,
              "kTabIcons must stay parallel to kRibbonTabs");

// Left-to-right DISPLAY order of the tab strip. Tab indices are append-only (AppState::ribbonTab
// and the scenarios address tabs by index), so the strip order is declared here instead: the
// Simulate tabs read structural (Static, Modal, Fatigue) -> thermal / fluids -> motion -> EM ->
// optimization -> industrial (Piping, Hydraulics). Must list every index exactly once.
static constexpr int kTabDisplayOrder[] = {
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12,
    13 /*Static*/, 14 /*Modal*/, 17 /*Fatigue*/, 16 /*Thermal*/, 15 /*Flow*/, 18 /*Motion*/,
    19 /*EM*/, 20 /*Schematic*/, 21 /*Topology*/, 22 /*Piping*/, 23 /*Hydraulics*/,
};
static_assert(sizeof(kTabDisplayOrder) / sizeof(kTabDisplayOrder[0]) == kRibbonTabCount,
              "kTabDisplayOrder must list every ribbon tab");

// Forward-declared here; defined after drawRibbon's local state block (~line 330).
// Default Model (0) — app.cpp sets the live value each frame via setRibbonWorkspace().
static int s_workspace = 0;

// sim-gui: dynamic command gating. Commands whose cmd_id starts with s_disabledPrefix render
// disabled with s_disabledReason in their tooltip (instead of the static reserved-row reason). Used by
// SimManager to grey the EM tools while the em_* solver backend is not installed. A second
// consumer exists: ribbon_dynDisabledReason also falls through to ui::webGapReason(cmd_id),
// which greys web-unsupported commands in the WASM build (T-RF12, data-driven from
// ui/web_capabilities.cpp — no per-command state here).
static std::string s_disabledPrefix;
static std::string s_disabledReason;

// Simulate study context (setRibbonStudyContext): the selected study's type key, refreshed every
// frame by SimManager::tick. It expires one frame after the last refresh, so a stale study never
// gates the ribbon once no CAD document (and so no SimManager) is active.
static std::string s_studyCtx;
static int s_studyCtxFrame = -100;
static const char* ribbon_studyCtx() {
    return ImGui::GetFrameCount() - s_studyCtxFrame <= 1 ? s_studyCtx.c_str() : nullptr;
}

static const char* ribbon_dynDisabledReason(const char* cmd_id) {
    if (!cmd_id) return nullptr;
    if (!s_disabledPrefix.empty() &&
        std::strncmp(cmd_id, s_disabledPrefix.c_str(), s_disabledPrefix.size()) == 0)
        return s_disabledReason.c_str();
    // Simulate: commands that do not apply to the selected study (ribbon_def.cpp ribbonStudyGate).
    const char* why = nullptr;
    if (ui::ribbonStudyGate(cmd_id, ribbon_studyCtx(), &why) == ui::RibbonStudyGate::Disabled)
        return why;
    // T-RF12: web-build capability gaps (ui/web_capabilities.cpp; folds to nullptr on
    // native, so desktop rendering/tooltips are unchanged).
    return ui::webGapReason(cmd_id);
}
// Effective enabled state for a row: the static schema-reserved flag (stretch) AND the dynamic
// gates above. Every button/flyout/overflow row goes through this.
static bool ribbon_cmdEnabled(const ui::RibbonCmd* c) {
    return !c->stretch && !ribbon_dynDisabledReason(c->cmd_id);
}

// Why a schema-reserved (stretch) row is greyed: the concrete missing capability, so a hover tells
// an engineer what is not supported instead of a placeholder. Unlisted rows get a plain statement.
static const char* ribbon_reservedReason(const char* cmd_id) {
    struct R { const char* id; const char* why; };
    static const R kReasons[] = {
        {"sk_spline_surface", "Not available yet: splines cannot be sketched directly on curved "
                              "faces."},
        {"sim_load_torque",   "Not available yet: the structural solver has no torque or moment "
                              "load."},
    };
    if (cmd_id)
        for (const auto& r : kReasons)
            if (std::strcmp(r.id, cmd_id) == 0) return r.why;
    return "Not available in this version.";
}

// W17 + per-workspace: control which tabs appear in the ribbon for the current workspace.
//
// Tab index → name map (matches kRibbonTabs):
//   0=Features, 1=Sketch, 2=Evaluate, 3=Simulation, 4=Assembly, 5=Sheet Metal,
//   6=Weldments, 7=PCB, 8=Surfaces, 9=View Layout, 10=Annotation, 11=Appearance, 12=Manufacture,
//   13..19=Static/Modal/Flow/Thermal/Fatigue/Motion/EM, 20=Schematic, 21=Topology, 22=Piping,
//   23=Hydraulics
//
// Workspace enum (AppState::Workspace): Model=0, Drawing=1, Simulate=2, Electronics=3,
//   Manufacture=4, PLM=5, PartsLibrary=6, AI=7
//
// PLM/PartsLibrary/AI: ribbon collapsed to 0 height upstream — this function is never reached.
// Drawing workspace: contextual tabs only (View Layout 9, Annotation 10, Sketch 1).
//   Within it the bools still control contextual sub-visibility (sketch-vs-sheet).
// Simulate workspace: Simulation(3) + Reference shared group (added as sim-tab rows in ribbon_def).
// Electronics workspace: PCB tab(7) only.
// Manufacture workspace: Manufacture tab(12) only.
// Model workspace (default): Features/Sketch/Evaluate/Assembly/Surfaces/Sheet Metal/Weldments/
//   Appearance — NO sim/cam/ecad unless the legacy bools also say yes.
static bool ribbon_tabVisible(int tab, bool showSimulationTools, bool showPcbTools,
                              bool showDrawingTools) {
    // Drawing workspace (or Drawing doc override): show View Layout, Annotation, Sketch only.
    if (showDrawingTools || s_workspace == 1 /*Drawing*/) {
        return tab == 9 || tab == 10 || tab == 1;
    }

    // Simulate workspace (sim-gui rework): the per-simulation-type tabs ONLY (Static / Modal /
    // Flow / Thermal / Fatigue / Motion / EM, indices 13..19, plus Topology at 21, Piping at 22 and
    // Hydraulics at 23). Each carries only the tools for its study type + the shared Reference group. The legacy
    // all-in-one Simulation tab (3) is retired from this workspace (its rows stay in ribbon_def for
    // append-only compat).
    if (s_workspace == 2 /*Simulate*/) return (tab >= 13 && tab <= 19) || tab == 21 || tab == 22 || tab == 23;

    // Electronics workspace: both ECAD tabs — PCB Design (7) and Schematic (20).
    if (s_workspace == 3 /*Electronics*/) return tab == 7 || tab == 20;

    // Manufacture workspace: Manufacture tab only.
    if (s_workspace == 4 /*Manufacture*/) return tab == 12;

    // Model workspace (0) and fallback: all 3D modeling tabs, no sim/pcb/drawing/manufacture
    // unless the legacy bools override (backwards compat for callers that pass them explicitly).
    if (tab == 9 || tab == 10) return false;   // drawing tabs: 3D docs never
    if (tab == 4) return false;                // Assembly tab retired: tools live on Features>Assembly group
    if (tab == 3) return showSimulationTools;  // Simulation: legacy bool
    if (tab == 7) return showPcbTools;         // PCB Design: legacy bool
    if (tab == 20) return showPcbTools;        // Schematic: same legacy ECAD bool as PCB Design
    if (tab == 12) return false;               // Manufacture: only in Manufacture workspace
    if (tab >= 13) return false;               // sim-gui per-type tabs + Topology + Piping + Hydraulics: Simulate only
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
    // Default rectangle/arc flavor: a plain click on the Rectangle/Arc split button arms the
    // CENTER variant (Ctr. Rect. / Ctr. Arc). The corner/3-pt variants stay in the flyout.
    {"sk_rect_corner", "sk_rect_center", "Ctr. Rect.", "rect_center"},
    {"sk_arc_3pt",     "sk_arc_center",  "Ctr. Arc",   "arc_center"},
};

static const ParentDefault* ribbon_parentDefault(const char* parentId) {
    for (const auto& pd : kParentDefaults)
        if (std::strcmp(pd.parentId, parentId) == 0) return &pd;
    return nullptr;
}

// Launcher split buttons: the face is ONE fixed command — a plain click always fires the parent
// (Simulate "New Study" always opens the grouped study picker) — and the arrow lists direct
// shortcuts. Picking a shortcut fires it without becoming the face (no last-used swap), the flyout
// does not repeat the parent, and the button is sized to its own label only.
static const char* const kLauncherParents[] = {"sim_new_picker"};

static bool ribbon_isLauncher(const char* cmdId) {
    for (const char* id : kLauncherParents)
        if (std::strcmp(id, cmdId) == 0) return true;
    return false;
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
// N1-N5 (Hunter, 2026-07-07): raised from 96 so long variant labels that can't wrap
// (no space — e.g. "Parallelogram") fit inside a split button's face without clipping.
static constexpr float kBigBtnMaxW  = 120.0f;
static constexpr float kBigLblPad   = 6.0f;
static constexpr float kGroupPadX   = 7.0f;   // audit: was 9 — trimmed dead space between groups
static constexpr float kBodyH       = 112.0f;  // taller so split buttons' 2-line captions clear the
                                               // bottom dropdown-arrow strip (was 100 — text clipped)
static constexpr float kCaptionH    = 22.0f;
static constexpr float kSmRowH      = 22.0f;
static constexpr float kDropArrowW  = 14.0f;  // trailing arrow slot — compact rows only
static constexpr float kBottomPad   = 4.0f;   // audit: was 8 — dead band under the body

static const char* ribbon_displayLabel(const char* cmd_id, const char* label) {
    // sketch_new now carries its "New Sketch" text label like every other tool button
    // (both the Features-tab entry and the injected Sketch-tab slot share this cmd_id).
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

// Launcher split buttons ("New Study") keep their caption on ONE line: their arrow strip sits right
// under the caption, and a two-line caption under the 36 px icon left the second line touching the
// caret (and the button too narrow for its group caption). One line still fits kBigBtnMaxW.
static float ribbon_wrapLabelFor(const char* cmd_id, const char* label, std::string& l1,
                                 std::string& l2) {
    if (cmd_id && ribbon_isLauncher(cmd_id)) {
        l1 = label;
        l2.clear();
        return ImGui::CalcTextSize(label).x;
    }
    return ribbon_wrapLabel(label, l1, l2);
}

// C25: snap to nearest integer pixel for crisp atlas rendering.
// Declared early so measure helpers (ribbon_bigBtnW / ribbon_smBtnW) and all button
// draw paths share the same pixel-grid math.
static inline float snap(float v) { return std::floor(v + 0.5f); }

// Width of a big button in screen pixels.
// §2.1: ribbon_wrapLabel returns a CalcTextSize result (already screen-px / DPI-scaled), so the
// label width is computed entirely in screen px (kBigLblPad scaled once) and clamped against the
// screen-px min/max bounds. Mixing the screen-px text with *s once would double-scale the label.
static float ribbon_bigBtnW(const char* cmd_id, const char* label, bool hasDrop) {
    float s = ui::uiScale;
    std::string l1, l2;
    const char* visibleLabel = ribbon_displayLabel(cmd_id, label);
    float lblWScreen = ribbon_wrapLabelFor(cmd_id, visibleLabel, l1, l2) + kBigLblPad * 2.0f * s;
    float base = (visibleLabel[0] == '\0' ? 46.0f : kBigBtnW) * s;
    if (lblWScreen > base) base = lblWScreen;
    if (base > kBigBtnMaxW * s) base = kBigBtnMaxW * s;
    // The dropdown arrow sits in a bottom strip (bottom-right corner), so a split button is exactly
    // as wide as a plain one — never fattened. hasDrop kept for call-site stability.
    (void)hasDrop;
    // Pixel-snap so measure, hit, fill, and content-centering all share one integer width at
    // every uiScale (1.0 / 1.25 / 1.5) — fractional widths were the root of tile misalignment.
    return snap(base);
}

// Width of a small button in screen pixels.
// Mirrors the hit/draw geometry in ribbon_smBtn (icon + 4px gap + label + trailing pad).
static float ribbon_smBtnW(const char* label) {
    float s = ui::uiScale;
    // Snap so column packing, hit rect, fill, and content centering share one integer width.
    return snap(kSmIconSz * s + 4.0f * s + ImGui::CalcTextSize(label).x + 8.0f * s);
}

// C25c FIX (defect 1): a small-button stack flows top->bottom then wraps into a new column.
// Each column's width must be the widest *row in that column*, not the group-wide max, or a
// short column followed by a wide one overlaps the next group. This describes one wrapped column.
struct SmColumn {
    int   first;   // index of first row (into the flat cmd list)
    int   count;   // rows in this column
    float width;   // measured width = max row width in this column (screen px)
};

// Pack `widths[]` (per-row measured widths) into vertical columns of at most `rowsPerCol`
// each, returning the column descriptors. Total width of the stack = sum of column widths.
//
// The packing is BALANCED, not greedy. Filling each column to rowsPerCol before starting the
// next is what produced the ribbon's worst-looking defect: a group of four small buttons came
// out as a full column of three plus a lone button stranded at the top of a second column,
// with two empty cells under it. "3D Body" (Extrude/Revolve/Sweep + Loft) and "Multi-body"
// (Combine/Subtract/Intersect + Split) both read as broken for exactly that reason.
//
// Spreading the same rows over the same number of columns costs nothing -- the column count is
// already ceil(n / rowsPerCol) either way, so this never widens a group -- and it turns 3+1
// into 2+2. Short groups get shorter and wider, which is the horizontal reading Hunter asked
// for, and no column is ever taller than the body zone allows.
//
// Remainder goes to the LEADING columns so heights are non-increasing left to right; a group
// that steps down reads as deliberate where one that steps up reads as a wrap.
static std::vector<SmColumn> ribbon_packColumns(const std::vector<float>& widths, int rowsPerCol) {
    std::vector<SmColumn> cols;
    if (rowsPerCol < 1) rowsPerCol = 1;
    const int n = (int)widths.size();
    if (n <= 0) return cols;

    const int ncols = (n + rowsPerCol - 1) / rowsPerCol;   // same count greedy would use
    const int base  = n / ncols;
    int       extra = n % ncols;                           // first `extra` columns take one more

    for (int i = 0, c = 0; c < ncols; ++c) {
        SmColumn col; col.first = i; col.count = 0; col.width = 0.0f;
        int take = base + (extra > 0 ? 1 : 0);
        if (extra > 0) --extra;
        for (int r = 0; r < take && i < n; ++r, ++i) {
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

// ---------------------------------------------------------------------------
// Shared group geometry (screen px, snapped). Panel, big buttons, small stacks,
// and the caption strip ALL read from these so a tile can never cross the group
// pill at any uiScale. Layout (top → bottom):
//   padY | content zone (big btn / small rows) | caption strip | padY
// ---------------------------------------------------------------------------
static float ribbon_groupPadY() {
    // Even inset between the group pill's outer edge and the button/caption content.
    return snap(2.0f * ui::uiScale);
}
static float ribbon_captionH() {
    return snap(kCaptionH * ui::uiScale);
}
static float ribbon_bodyH() {
    // Full body-cell height (matches the chrome band). Snapped so panel bottom and
    // caption placement share one integer edge.
    return snap(kBodyH * ui::uiScale);
}
static float ribbon_contentH() {
    // Button zone height: body minus caption strip minus even top/bottom pad.
    // Big buttons, small-row packing, and the group pill's inner rect all use this.
    float h = ribbon_bodyH() - ribbon_captionH() - 2.0f * ribbon_groupPadY();
    float minH = snap(kSmRowH * ui::uiScale);
    return h < minH ? minH : snap(h);
}

// How many small rows fit vertically in the body content zone (the fixed row grid — 3 at every
// uiScale, since kBodyH / kCaptionH / kSmRowH all scale together).
static int ribbon_rowsPerCol() {
    float smZoneH = ribbon_contentH();
    float rowH    = snap(kSmRowH * ui::uiScale);
    int n = (int)std::floor(smZoneH / rowH);
    return n < 1 ? 1 : n;
}

// ONE height for every small-row button in the ribbon — plain small commands AND big commands
// degraded to small rows. The content zone is split into ribbon_rowsPerCol() equal cells (floored
// so the stack never overflows the zone), so small stacks read as thirds of the big-button height
// (Office / SolidWorks). Previously plain rows used kSmRowH (22dp) while degraded rows used a
// third of the zone (~28dp), so mixed columns (Features ▸ Reference) never lined up and every
// plain stack was top-packed well short of the big button beside it.
static float ribbon_smRowH() {
    const int rows = ribbon_rowsPerCol();
    float cell = std::floor(ribbon_contentH() / (float)rows);
    return ImMax(cell, snap(kSmRowH * ui::uiScale));
}

// Top edge (window-local px) of grid row `r`. Rows are distributed evenly over the full content
// zone: row 0 flush with the big button's top, the last row flush with its bottom, the (at most
// rows-1 px) floor remainder spread between them. EVERY column of every group uses this same grid,
// so a short column (2 of 3) stays on rows 1-2 and aligns row-for-row with its neighbours.
static float ribbon_smRowY(float contentY, int r) {
    const int   rows  = ribbon_rowsPerCol();
    const float rowH  = ribbon_smRowH();
    const float zoneH = ribbon_contentH();
    if (rows <= 1) return snap(contentY + (zoneH - rowH) * 0.5f);
    const float pitch = rowH + (zoneH - rowH * (float)rows) / (float)(rows - 1);
    return snap(contentY + pitch * (float)r);
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

// Glass v2.1: the ribbon stays a SOLID chrome band (locked design — no frosting over the
// viewport), but it subtly adopts the Global glass group's user tint so the shell follows the
// Appearance config. Quiet by design: 35% of the resolved tintStrength, hue/sat from the config,
// value + alpha of the base palette role preserved. Out of the box (strength 0) this returns the
// palette color unchanged, so the default look is byte-identical.
static ImVec4 ribbon_chromeTint(const float base[4]) {
    const ui::GlassStyleResolved& gs = ui::glassStyleResolved(ui::GlassGroup::Global);
    ImVec4 c(base[0], base[1], base[2], base[3]);
    if (gs.tintStrength <= 0.001f || gs.tintSat <= 0.001f) return c;
    float h, sat, v;
    ImGui::ColorConvertRGBtoHSV(c.x, c.y, c.z, h, sat, v);
    float tr, tg, tb;
    ImGui::ColorConvertHSVtoRGB(gs.tintHue / 360.0f, gs.tintSat, v, tr, tg, tb);
    const float m = ImClamp(gs.tintStrength * 0.35f, 0.0f, 1.0f);
    c.x += (tr - c.x) * m;
    c.y += (tg - c.y) * m;
    c.z += (tb - c.z) * m;
    return c;
}
// Press feedback scrim: the palette's drop-shadow role at a light alpha, so "click-down" darkening
// is the same wash on every ribbon button size.
static ImU32 ribbon_pressScrim() {
    const float* sh = ui::palette.glassShadow;
    return ImGui::ColorConvertFloat4ToU32(ImVec4(sh[0], sh[1], sh[2], 40.0f / 255.0f));
}


// Crisp icon draw, centered at (cx, cy), sizePx square. Line-art glyphs render via the SDF
// re-raster path (sharp at ANY pixel size — the raw atlas blit upscaled 32->36px big-button
// icons and downscaled small rows into mush), tinted with a palette role so both themes read:
// text normally, accent when the tool is armed, textDisabled when greyed. Multi-color raster
// glyphs (badges/swatches) keep the pre-colored atlas pixels — white tint = identity, matching
// the previous draw exactly.
static void ribbon_icon(ImDrawList* dl, const char* name, float cx, float cy, float sizePx,
                        bool enabled, bool active = false) {
    ImU32 col;
    if (ui::hasIconSdf(name)) {
        const float* role = !enabled ? ui::palette.textDisabled
                          : active   ? ui::palette.accent
                                     : ui::palette.text;
        col = ui::Palette::toU32(role);
    } else if (enabled) {
        col = IM_COL32(255, 255, 255, 255);  // gui-lint: allow identity tint, not a color — raster glyph keeps its atlas pixels
    } else {
        // Greyed raster glyph: the palette's disabled ink so it desaturates with the theme.
        const float* dis = ui::palette.textDisabled;
        col = ImGui::ColorConvertFloat4ToU32(ImVec4(dis[0], dis[1], dis[2], 160.0f / 255.0f));
    }
    ui::drawIconGlass(dl, name, ImVec2(cx, cy), sizePx, col, 0);
}

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

// True while a 3D (free-space) sketch is being edited — see ribbon.h. Swaps the Sketch tab's
// command source to the "Sketch3D" pseudo-tab rows in ribbon_def.cpp instead of the 2D tools.
static bool s_sketch3dActive = false;

// s_workspace declared near top of file (before ribbon_tabVisible).
// Per-workspace ribbon filter. Mirrors AppState::Workspace (Model=0..AI=7). Default: Model.
// Workspace-shell sets this each frame via setRibbonWorkspace(); see ribbon.h.
static const ui::RibbonCmd kSketchNewOnSketchTab =
    { "sketch_new", "New Sketch", "sketch", "Sketch", "Sketch", true, nullptr, false };
// While editing a sketch, the New Sketch slot is replaced by a highlighted "Finish Sketch" big
// button (a dedicated render path — NOT wired through the generic split-button/last-used machinery,
// so a plain click always finishes and only the dropdown offers Cancel). cmd_id carries the finish
// command for the active mode; the Cancel flyout row fires the matching *_cancel below.
// Base face uses the real "sketch" pencil glyph; a check badge is drawn procedurally over it (the
// atlas has no dedicated sketch_finish glyph — see ribbon_sketchFinishBadge).
static const ui::RibbonCmd kSketchFinishOnSketchTab =
    { "sketch_exit",   "Finish Sketch", "sketch", "Sketch", "Sketch", true, nullptr, false };
static const ui::RibbonCmd kSketch3dFinishOnSketchTab =
    { "sketch3d_exit", "Finish Sketch", "sketch", "Sketch", "Sketch", true, nullptr, false };
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

// ---------------------------------------------------------------------------
// Per-tab group ORDER (left -> right)
// ---------------------------------------------------------------------------
// Groups are emitted in the order they first appear in ribbon_def.cpp's table, which is
// APPEND-ONLY ("never reorder or delete existing rows") — so a tab's visual priority is welded to
// whatever order its rows happened to be appended in. That matters twice over: the LEFTMOST group
// is where the eye lands, and the RIGHTMOST group is the first one the overflow passes degrade or
// hide.
//
// A tab that needs a different story registers it HERE rather than reordering table rows. Listed
// groups are emitted first, in the listed order; any group of that tab that is not listed keeps
// its table order and follows them. Tabs with no entry are untouched (zero behavior change).
struct TabGroupOrder {
    const char*        tab;
    const char* const* groups;
    int                count;
};
// Manufacture: the machining workflow, left to right. Job -> operation families (2.5D / Holes /
// 3D / Turning / Cutting) -> Output (Generate / Simulate / Post G-code — what the user is actually
// after) -> Reference last, because ref geometry is a helper, not a step toward G-code.
static const char* const kManufactureGroupOrder[] = {
    "Job", "2.5D", "Holes", "3D", "Turning", "Cutting", "Output", "Reference",
};
// Simulate study tabs (standard §7): Study -> Setup -> Boundaries -> Mesh -> Run -> Results ->
// Reference. A tab simply has no group where one does not apply (Modal: no loads, Motion: no mesh).
static const char* const kSimStudyGroupOrder[] = {
    "Study", "Setup", "Boundaries", "Mesh", "Run", "Results", "Reference",
};
// Piping / Hydraulics are design tools, not studies: the network or circuit, its design (sizing),
// analysis, results, then Reference.
static const char* const kPipingGroupOrder[] = {
    "Network", "Design", "Analysis", "Results", "Reference",
};
static const char* const kHydraulicsGroupOrder[] = {
    "Circuit", "Design", "Analysis", "Results", "Reference",
};
#define RIBBON_GROUP_ORDER(TAB, ARR) {TAB, ARR, (int)(sizeof(ARR) / sizeof(ARR[0]))}
static const TabGroupOrder kTabGroupOrder[] = {
    RIBBON_GROUP_ORDER("Manufacture", kManufactureGroupOrder),
    RIBBON_GROUP_ORDER("Simulation",  kSimStudyGroupOrder),
    RIBBON_GROUP_ORDER("Static",      kSimStudyGroupOrder),
    RIBBON_GROUP_ORDER("Modal",       kSimStudyGroupOrder),
    RIBBON_GROUP_ORDER("Flow",        kSimStudyGroupOrder),
    RIBBON_GROUP_ORDER("Thermal",     kSimStudyGroupOrder),
    RIBBON_GROUP_ORDER("Fatigue",     kSimStudyGroupOrder),
    RIBBON_GROUP_ORDER("Motion",      kSimStudyGroupOrder),
    RIBBON_GROUP_ORDER("EM",          kSimStudyGroupOrder),
    RIBBON_GROUP_ORDER("Topology",    kSimStudyGroupOrder),
    RIBBON_GROUP_ORDER("Piping",      kPipingGroupOrder),
    RIBBON_GROUP_ORDER("Hydraulics",  kHydraulicsGroupOrder),
};
#undef RIBBON_GROUP_ORDER

// Reorder `groups` for `tabName` per kTabGroupOrder. Stable, so unlisted groups keep table order.
static void ribbon_orderGroups(std::vector<RibbonGroup>& groups, const char* tabName) {
    const TabGroupOrder* order = nullptr;
    for (const auto& t : kTabGroupOrder)
        if (std::strcmp(t.tab, tabName) == 0) { order = &t; break; }
    if (!order) return;
    auto rank = [order](const RibbonGroup& g) {
        for (int i = 0; i < order->count; ++i)
            if (std::strcmp(order->groups[i], g.label) == 0) return i;
        return order->count;   // unlisted -> after every listed group, in table order
    };
    std::stable_sort(groups.begin(), groups.end(),
                     [&rank](const RibbonGroup& a, const RibbonGroup& b) {
                         return rank(a) < rank(b);
                     });
}

// Prefer stacking `childId` directly under `parentId` in the small-button column packer.
// Pulls the pair to the front of the smCmds list so rowsPerCol packing places child in the
// same column under parent (e.g. Sketch▸Draw: "Rotate Ent." under "Move Ent.").
static void ribbon_preferStackedPair(std::vector<const ui::RibbonCmd*>& cmds,
                                     const char* parentId, const char* childId) {
    int iP = -1, iC = -1;
    for (int i = 0; i < (int)cmds.size(); ++i) {
        if (cmds[i] && cmds[i]->cmd_id && std::strcmp(cmds[i]->cmd_id, parentId) == 0) iP = i;
        if (cmds[i] && cmds[i]->cmd_id && std::strcmp(cmds[i]->cmd_id, childId) == 0) iC = i;
    }
    if (iP < 0 || iC < 0 || iP == iC) return;
    const ui::RibbonCmd* p = cmds[(size_t)iP];
    const ui::RibbonCmd* c = cmds[(size_t)iC];
    if (iC > iP) {
        cmds.erase(cmds.begin() + iC);
        cmds.erase(cmds.begin() + iP);
    } else {
        cmds.erase(cmds.begin() + iP);
        cmds.erase(cmds.begin() + iC);
    }
    cmds.insert(cmds.begin(), c);
    cmds.insert(cmds.begin(), p);
}

static bool ribbon_hasSubs(const RibbonGroup& grp, const ui::RibbonCmd* cmd) {
    for (const auto* sc : grp.subCmds)
        if (std::strcmp(sc->sub_of, cmd->cmd_id) == 0) return true;
    return false;
}

// N1-N5 (Hunter, 2026-07-07): a split/variant big button's FACE label changes with the selected
// variant (resolveFace swaps in the sub's label). Sizing to the CURRENT face label made the button
// reflow every switch — too narrow for long variants (clipping: N1 "3-Pt Arc", N3 cut variants) and
// bleeding into the neighbor when a wide variant like "Parallelogram" drew past the parent-label
// advance (N2). The fix: every split button gets a STABLE width = the widest of {parent label, every
// variant label, injected parent-default label}. Measure and draw both use this so the button never
// reflows and never clips, regardless of the active variant. Plain (non-drop) buttons return their
// own width unchanged.
static float ribbon_bigBtnWMax(const RibbonGroup& grp, const ui::RibbonCmd* cmd, bool drop) {
    float w = ribbon_bigBtnW(cmd->cmd_id, cmd->label, drop);
    if (!drop || ribbon_isLauncher(cmd->cmd_id)) return w;   // launcher face never changes
    // Injected parent-default flyout row (e.g. sk_ellipse_big -> "Ellipse") is also a possible face.
    if (const ParentDefault* pd = ribbon_parentDefault(cmd->cmd_id))
        w = ImMax(w, ribbon_bigBtnW(pd->defaultCmd, pd->defaultLabel, drop));
    for (const auto* sc : grp.subCmds)
        if (std::strcmp(sc->sub_of, cmd->cmd_id) == 0)
            w = ImMax(w, ribbon_bigBtnW(sc->cmd_id, sc->label, drop));
    return w;
}

// N1-N5: the same widest-variant rule for the degraded "big-as-small" row (icon + side label).
// Mirrors ribbon_bigBtnAsSmall's width (ribbon_smBtnW + optional drop-arrow slot) but over the
// widest of {parent, injected default, every variant} label so the packed column reserves enough.
static float ribbon_bigBtnAsSmallWMax(const RibbonGroup& grp, const ui::RibbonCmd* cmd, bool drop) {
    float s = ui::uiScale;
    float w = ribbon_smBtnW(cmd->label);
    if (drop) {
        if (!ribbon_isLauncher(cmd->cmd_id)) {   // launcher face never changes
            if (const ParentDefault* pd = ribbon_parentDefault(cmd->cmd_id))
                w = ImMax(w, ribbon_smBtnW(pd->defaultLabel));
            for (const auto* sc : grp.subCmds)
                if (std::strcmp(sc->sub_of, cmd->cmd_id) == 0)
                    w = ImMax(w, ribbon_smBtnW(sc->label));
        }
        // Snap the trailing arrow slot so mainW + dropW == totalW on the pixel grid.
        w += snap(kDropArrowW * s);
    }
    return w;
}

// C25c FIX (defect 1): measured width of the small-button zone for a group, packing rows into
// independently-measured columns (each column's width = its widest row, not the group-wide max).
// `extraSmallRows` are big-cmd labels that degrade to small rows (COL_SMALL/COL_LABEL).
static float ribbon_smZoneW(const RibbonGroup& grp,
                            const std::vector<const ui::RibbonCmd*>& extraSmallRows) {
    std::vector<float> widths;
    widths.reserve(grp.smCmds.size() + extraSmallRows.size());
    for (const auto* cmd : extraSmallRows) {
        // N1-N5: degraded big rows reserve the widest-variant width so a variant switch never
        // overflows the packed column.
        bool drop = ribbon_hasSubs(grp, cmd);
        widths.push_back(ribbon_bigBtnAsSmallWMax(grp, cmd, drop));
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

// Content extent (screen px) of a group at a given collapse level — the width its BUTTONS occupy,
// padding included, BEFORE the caption-width floor:
//   padX | [big buttons with inter-gaps only] | [inter-zone gap] | [small cols] | padX
// Previously every big button contributed a trailing 2dp gap (including the last), and an
// extra kGroupPadX was reserved between big and small zones that the draw path never spent —
// that piled dead space on the RIGHT of the group pill.
static float ribbon_groupContentW(const RibbonGroup& grp, CollapseLevel level) {
    float s = ui::uiScale;
    if (level == COL_HIDDEN) return 0.0f;

    const float padX = kGroupPadX * s;
    if (level == COL_BIG) {
        // big-button column: buttons laid out left-to-right; gap ONLY between buttons
        float bigColW = 0.0f;
        for (int i = 0; i < (int)grp.bigCmds.size(); ++i) {
            bool drop = ribbon_hasSubs(grp, grp.bigCmds[i]);
            if (i > 0) bigColW += 2.0f * s;
            bigColW += ribbon_bigBtnWMax(grp, grp.bigCmds[i], drop);
        }
        float smW = ribbon_smZoneW(grp, {});
        // Inter-zone gap matches the inter-button gap (2dp), only when both zones are present.
        const float midGap = (bigColW > 0.0f && smW > 0.0f) ? 2.0f * s : 0.0f;
        return padX + bigColW + midGap + smW + padX;
    }
    // COL_SMALL / COL_LABEL: big cmds degrade into the small-row stack alongside existing smCmds
    return padX + ribbon_smZoneW(grp, grp.bigCmds) + padX;
}

// Natural width (screen px) of a group: its content extent, floored at the caption width so
// captions like "Dimension" never truncate. The draw pass centers the content in any slack the
// floor introduces (see `startX` in drawRibbon).
static float ribbon_groupNaturalW(const RibbonGroup& grp, CollapseLevel level) {
    if (level == COL_HIDDEN) return 0.0f;
    float content = ribbon_groupContentW(grp, level);
    // Defect 2: never narrower than the caption (unless the label is hidden at COL_LABEL).
    if (level != COL_LABEL)
        content = ImMax(content, ribbon_captionMinW(grp));
    return content;
}

// The tab drawRibbon is rendering (set per frame) — a command whose label differs per tab gets the
// matching description from kTabTips.
static const char* s_tipTab = "";

static const char* ribbon_tipFor(const char* cmd_id) {
    // Per-tab descriptions, checked first: the same command reads differently on some tabs
    // (Thermal "Fixed Temp" is sim_fixture holding faces at a temperature).
    struct TabTip { const char* tab; const char* id; const char* tip; };
    static const TabTip kTabTips[] = {
        {"Thermal", "sim_fixture", "Hold the selected faces at a fixed temperature."},
    };
    for (const auto& t : kTabTips)
        if (std::strcmp(t.tab, s_tipTab) == 0 && std::strcmp(t.id, cmd_id) == 0) return t.tip;
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
        {"eval_geometry",     "Find small faces, short edges, slivers, gaps and tangency breaks."},
        {"eval_interference", "Detect interference between bodies."},
        // --- Simulate: Study group (New Study launcher + every study type in its flyout) ---
        {"sim_new_picker",    "Create a study: choose its type from the grouped list (structural, "
                              "thermal, fluids, motion, electromagnetics, optimization). The arrow "
                              "creates a study of one type directly."},
        {"sim_new_static",    "Linear static study: stress, displacement and factor of safety under "
                              "fixtures and loads."},
        {"sim_new_modal",     "Modal study: natural frequencies and mode shapes."},
        {"sim_new_buckling",  "Linear buckling study: critical load factors and buckled mode shapes."},
        {"sim_new_fatigue",   "Fatigue study: cycles to failure from a static stress state, an S-N "
                              "curve and Marin factors."},
        {"sim_new_contact",   "Contact study: two blocks pressed together, on a proxy sized to the "
                              "part (CalculiX) — contact pressure and reaction balance."},
        {"sim_new_freqresp",  "Vibration study: steady response to base excitation across a frequency "
                              "band, with modal damping."},
        {"sim_new_thermal_fea", "Thermal FEA study: steady or transient conduction in the solid — "
                                "fixed temperatures, heat flux and convection."},
        {"sim_new_flow",      "Flow study: external flow around the part in a virtual wind or water "
                              "tunnel — drag, lift and surface pressure."},
        {"sim_new_motion",    "Motion study: drive an assembly mate over time and play the motion back "
                              "with interference checks."},
        // --- Simulate: Setup / Boundaries / Mesh / Run / Results (shared by the study tabs) ---
        {"sim_material",      "Assign the study's material: stiffness, strength, density and thermal "
                              "properties."},
        {"sim_fixture",       "Restrain the selected faces (zero displacement)."},
        {"sim_load_force",    "Apply a force to the selected faces."},
        {"sim_load_pressure", "Apply a pressure normal to the selected faces."},
        {"sim_load_torque",   "Apply a torque about an axis to the selected faces."},
        {"sim_load_heat",     "Apply a heat load to the selected faces of a thermal FEA study: heat "
                              "flux or convection to ambient."},
        {"sim_mesh",          "Set the mesh size and generate the study's mesh."},
        {"sim_run",           "Solve the active study. Progress and Cancel show at the foot of the "
                              "study pane."},
        {"sim_results",       "Open the active study's results: key values, plots and the viewport "
                              "field (a motion study plays back here)."},
        {"sim_modal_options", "Set how many natural frequencies and mode shapes to solve."},
        {"sim_buckling_options", "Set how many buckling modes (critical load factors) to solve."},
        {"sim_contact_options", "Set the contact pressure on the proxy blocks."},
        {"sim_freqresp_options", "Set the base-excitation axis and amplitude, the modal damping and "
                                 "the modes in the superposition."},
        {"sim_thermal_fea_options", "Set the solid's conductivity and initial temperature, and the "
                                    "transient time settings."},
        {"sim_fluid",         "Choose the fluid and its density and viscosity."},
        {"sim_flow_bc",       "Set the flow boundaries: flow direction, speed and angle of attack."},
        {"sim_flow_res",      "Set the flow grid resolution and the solver settings."},
        {"sim_fatigue_setup", "Set the cyclic loading, the S-N curve and the Marin surface, size and "
                              "reliability factors."},
        {"sim_motion_setup",  "Set the drivers (which joints move, from where to where), the "
                              "sampling and the interference check."},
        // --- Simulate: EM tab (waveguide electromagnetics, gated on the em_* backend) ---
        {"sim_em_eigen",      "Eigenmode analysis: cutoff frequencies and field patterns of a "
                              "waveguide cross-section's modes."},
        {"sim_em_sweep",      "Frequency sweep: S-parameters of a waveguide section across a "
                              "frequency band."},
        {"sim_em_time",       "Time-domain analysis: a pulse travelling through the waveguide "
                              "section."},
        {"sim_em_range",      "Set the sweep's start and stop frequency and its point count."},
        {"sim_em_ports",      "Excitation ports: matched modal ports on the guide cross-section — set "
                              "the waveguide, length and mode."},
        {"sim_em_boundary",   "Boundaries: the conducting guide walls and port terminations of the "
                              "waveguide section."},
        {"sim_em_run",        "Solve the open electromagnetics analysis."},
        {"sim_em_results",    "Open the electromagnetics results: modes, S-parameters and fields."},
        {"asm_insert",        "Insert a component into the assembly."},
        {"asm_move",          "Move or rotate a component."},
        {"asm_pattern",       "Pattern a component."},
        {"asm_mate",          "Add a mate to constrain components."},
        {"asm_interference",  "Detect interference between components."},
        {"asm_bom",           "Generate a bill of materials."},
        {"asm_explode",       "Create an exploded view."},
        {"sim_motion_study",  "Open this assembly's Motion study and play it in the viewport "
                              "(creates one when the assembly has none)."},
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
        // --- Simulate: topology optimization (Topology tab) ---
        {"sim_new_topology",  "Create a topology optimization study: find the best material layout "
                              "inside the part for a volume budget."},
        {"sim_topology_setup", "Choose the physics, optimization method, objective, volume budget "
                               "and response limits the solver supports."},
        {"sim_topology_boundaries", "Edit the study's boundary faces: thermal temperatures, heat "
                                    "loads and convection, or flow inlets, outlets and walls."},
        {"sim_topology_preserve", "Keep the selected faces solid to a set depth (mounting faces, "
                                  "bosses) — the optimizer never removes this material."},
        {"sim_topology_exclude", "Keep the selected faces or region empty (clearances, keep-outs) "
                                 "— the optimizer never places material there."},
        {"sim_topology_manufacturing", "Set symmetry, extrusion, build direction, overhang and "
                                       "enclosed-void rules for the optimized shape."},
        {"sim_topology_compare", "Compare retained optimization runs: objective, volume, "
                                 "constraints and convergence against a baseline run."},
        // --- Simulation: piping, internal flow, vessels, ducts (Piping tab) ---
        {"sim_piping_new",    "Start a new pipe network saved in this document."},
        {"sim_piping_route",  "Route a pipe between two points; the router steers around the model's "
                              "bodies with the clearance you set and places elbows at every bend."},
        {"sim_piping_solve",  "Solve the network's flows and pressures from supply pressures and demands."},
        {"sim_piping_spec",   "Check every run and flange against ASME B31.3 at the design pressure "
                              "and temperature: wall, MAWP, flange rating, support span."},
        {"sim_piping_bom",    "Line list, material take-off with masses, and shop spools."},
        {"sim_piping_flow",   "3D internal-flow CFD through a fluid volume: pressure drop against the "
                              "Darcy estimate, Reynolds number and regime."},
        {"sim_piping_vessel", "Size a pressure vessel's shell and heads to ASME VIII-1, with "
                              "hydrotest pressure, volume and masses."},
        {"sim_piping_duct",   "Size a duct tree by equal friction and find the fan static pressure."},
        // --- Simulation: hydraulic (fluid-power) circuits (Hydraulics tab) ---
        {"sim_hyd_new",       "Start a new hydraulic circuit saved in this document."},
        {"sim_hyd_add",       "Add an ISO 1219 component: pump, motor, cylinder, directional / relief / "
                              "check / flow-control valve, accumulator, reservoir, filter, cooler."},
        {"sim_hyd_hose",      "Connect component ports with a hose (two ends, or more for a tee) and set "
                              "its length, bore and pressure rating."},
        {"sim_hyd_size_cyl",  "Size a cylinder: forces, areas, flows and stroke time; bore from a force."},
        {"sim_hyd_size_pump", "Size a pump: displacement, hydraulic and shaft power, torque."},
        {"sim_hyd_size_motor","Size a hydraulic motor for a torque and speed, with the catalog displacement."},
        {"sim_hyd_drive",     "Size the electric motor driving a pump: rating, poles, frame and current."},
        {"sim_hyd_simulate",  "Simulate the circuit over time with a valve schedule and actuator loads: "
                              "pressures, strokes, speeds, energy and heat."},
        {"sim_hyd_bom",       "Bill of materials with sourcing for every component and hose."},
        {"sim_hyd_piping",    "Turn the hoses into a solved pipe network and open it in the Piping tab."},
        {"sim_hyd_view_schematic", "Show the circuit's ISO 1219 schematic in the central view."},
        {"sim_hyd_view_plots",     "Show the simulation plots in the central view: pressures, "
                                   "strokes and speeds over time."},
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
        {"dwg_export_dwg",    "Export the drawing sheet to AutoCAD DWG."},
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
        {"dwg_auto_balloon",  "Auto-place numbered BOM balloons on every visible component."},
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
        // --- Manufacture (CAM): job setup, operation families, output ---
        {"cam_new_job",       "Start an empty CAM job for the active part."},
        {"cam_extract",       "Build a CAM job from the part: stock, setup, and candidate features."},
        {"cam_add_setup",     "Add a machining setup — work offset, orientation, and safe heights."},
        {"cam_set_stock",     "Define the raw stock the toolpaths cut away from."},
        {"cam_add_tool",      "Add a cutting tool to the job's tool library."},
        {"cam_op_face",       "Face the top of the stock flat to a target depth."},
        {"cam_op_contour",    "Profile around a contour, one side of the selected geometry."},
        {"cam_op_chamfer",    "Break a sharp edge with a chamfer tool along the contour."},
        {"cam_op_engrave",    "Trace text or curves at a single depth with a V or engraving bit."},
        {"cam_op_pocket",     "Clear a closed pocket down to depth with stepover passes."},
        {"cam_op_adaptive",   "High-efficiency adaptive clearing with constant tool engagement."},
        {"cam_op_slot",       "Cut a slot along its centerline in stepped-down passes."},
        {"cam_op_drill",      "Drill the selected holes with a peck or standard cycle."},
        {"cam_op_thread",     "Mill an internal or external thread with a thread mill."},
        {"cam_op_parallel3d", "3D finish in parallel passes at a chosen stepover and angle."},
        {"cam_op_waterline3d","3D finish in constant-Z levels — best on steep walls."},
        {"cam_op_scallop3d",  "3D finish at constant scallop height over blended surfaces."},
        {"cam_op_pocket3d",   "Rough a 3D cavity level by level down to the surface."},
        {"cam_op_turn_rough", "Rough a turned profile in stepped depth-of-cut passes."},
        {"cam_op_turn_finish","Finish pass along the turned profile to final size."},
        {"cam_op_turn_face",  "Face the end of the bar stock square."},
        {"cam_op_turn_part",  "Part the finished piece off the bar."},
        {"cam_op_laser",      "2D laser, plasma, or waterjet cut along the selected contours."},
        {"cam_op_probe",      "Probe a feature to set the work offset before cutting."},
        {"cam_generate",      "Generate toolpaths for every operation in the job."},
        {"cam_simulate",      "Run the material-removal simulation over the generated toolpaths."},
        {"cam_post",          "Post the toolpaths to G-code for the selected controller dialect."},
        {"cam_show3d",        "Toggle the toolpath backplot in the 3D viewport."},
        {"cam_flavor",        "Choose the post-processor dialect the G-code is written for."},
    };
    for (const auto& t : tips)
        if (std::strcmp(t.id, cmd_id) == 0) return t.tip;
    return nullptr;
}

static bool ribbon_tooltipsEnabled() {
#ifdef __EMSCRIPTEN__
    return ImGui::GetIO().DisplaySize.x > 900.0f;
#else
    return true;
#endif
}

// `iconName` is the glyph the hovered control actually draws (rows of one cmd_id can differ per tab).
static void ribbon_tooltip(const char* title, const char* cmd_id, const char* iconName,
                           bool enabled) {
    auto col = [](const float f[4]) { return ImVec4(f[0], f[1], f[2], f[3]); };
    ui::pushGlassTooltipStyle();
    if (ImGui::BeginTooltip()) {
        ImVec2 wp = ImGui::GetWindowPos();
        ImVec2 ws = ImGui::GetWindowSize();
        ui::glassBackground(ImGui::GetWindowDrawList(), wp, ImVec2(wp.x + ws.x, wp.y + ws.y));
        // Icon mini-preview: the same glyph the button shows, so the tooltip previews what the tool
        // does. Tinted like the button — white when enabled, grey when disabled / reserved.
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
            // Dynamic gate (e.g. "EM solver not installed") beats the static reserved-row reason.
            const char* why = ribbon_dynDisabledReason(cmd_id);
            ImGui::PushStyleColor(ImGuiCol_Text, col(ui::palette.textDisabled));
            ImGui::TextUnformatted(why ? why : ribbon_reservedReason(cmd_id));
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

// Liquid-glass fill + hairline outline for a ribbon button (big or small).
// Called before active so the state layers are: rest/hover (this) < press < active outline.
//
// Layers (all palette roles — theme-safe in light AND dark):
//   1. base fill    = text at low alpha (light in dark themes, dark in light themes) — the
//                     faint tint that signals a pressable zone
//   2. shine        = vertical translucent gradient, glassHighlight -> transparent over the
//                     top ~55% (same material recipe as ui::glassBackground) — "liquid glass"
//   3. outline      = text-role hairline at translucent alpha, soft rounding
//
// `hovered`: brightens all three layers (brighter gradient + outline) — the hover state.
// `inset`:   1px vertical inset for stacked small-button rows so adjacent bottom/top edges
//            don't share a pixel and merge into a solid grid line.
//
// All layers scale down when !enabled so disabled buttons keep a low-contrast ghost.
// Vertical top->bottom gradient with ROUNDED corners covering the full [a,b] rounded rect (so a
// tinted button fill reaches into the rounded corners instead of a square gradient stopping short).
// Mirrors ui::glassCardWash's rounded-fill technique: rounded base in the bottom color, a
// rounded-top strip in the top color, then a straight multicolor band for the interior.
static void ribbon_vGradRounded(ImDrawList* dl, ImVec2 a, ImVec2 b, float rnd, ImU32 top, ImU32 bot) {
    if (b.x <= a.x || b.y <= a.y) return;
    rnd = ImClamp(rnd, 0.0f, ImMin((b.x - a.x), (b.y - a.y)) * 0.5f);
    // Full rounded base in the bottom colour — owns every corner (incl. the top ones).
    dl->AddRectFilled(a, b, bot, rnd);
    // Top rounded strip so the top corners carry the bright colour. Height is at least
    // the corner radius so ImGui's rounded-AA path doesn't collapse into square corner
    // pips (the "small red/orange rectangle at top corners" glitch when height ≈ rnd).
    const float topH = ImMax(rnd, 1.0f);
    dl->AddRectFilled(a, ImVec2(b.x, a.y + topH), top, rnd, ImDrawFlags_RoundCornersTop);
    // Interior vertical fade. AddRectFilledMultiColor is SQUARE-cornered — but between the
    // top strip (height ≥ rnd) and the bottom arc (last rnd px) the rounded rect's sides are
    // STRAIGHT, so the band can span the FULL width with zero corner overflow. The old rnd
    // left/right inset left faint margin strips inside the outline (the "white inner border"
    // artifact) — margins showed only the faint `bot` base while the interior was tinted.
    const float y0 = a.y + topH;
    const float y1 = b.y - rnd;
    if (y1 > y0) {
        dl->AddRectFilledMultiColor(ImVec2(a.x, y0), ImVec2(b.x, y1),
                                    top, top, bot, bot);
    }
}

static void ribbon_btnRest(ImDrawList* dl, ImVec2 p0, ImVec2 p1, float rounding, bool enabled,
                            bool inset = false, bool hovered = false) {
    const float* t = ui::palette.text;
    const float* a = ui::palette.accent;   // colored-glass tint (goes with the app color scheme)
    // Keep a clearly-visible border at rest (not just on hover) so every button reads as a discrete
    // key, and keep the top shine restrained so the gradient never blows out to near-white.
    float lineA    = !enabled ? 0.24f : (hovered ? 0.72f : 0.55f);
    float shineMul = !enabled ? 0.35f : (hovered ? 0.95f : 0.55f);
    ImU32 border = IM_COL32((int)(t[0]*255), (int)(t[1]*255), (int)(t[2]*255),
                             (int)(lineA * 255));
    if (inset) {
        // Scale-aware 1px vertical inset so stacked rows don't share an edge pixel at any DPI.
        // (Was hard-coded 1.0f — under-inset at 125%/150% and left a fractional mismatch vs
        // the hit/content rect which never shrank.)
        const float ins = snap(1.0f * ui::uiScale);
        p0.y += ins;
        p1.y -= ins;
    }
    // Colored gradient background: a faint neutral base plus a top->bottom accent wash so every
    // button reads as tinted "liquid glass" in the app color, brightening on hover.
    float baseA    = !enabled ? 0.04f : (hovered ? 0.10f : 0.06f);
    float tintTopA = !enabled ? 0.05f : (hovered ? 0.18f : 0.12f);
    ImU32 base = IM_COL32((int)(t[0]*255), (int)(t[1]*255), (int)(t[2]*255), (int)(baseA * 255));
    dl->AddRectFilled(p0, p1, base, rounding);
    const float ix = 1.0f * ui::uiScale;
    const float rIn = ImMax(0.0f, rounding - ix);   // inner rounding for the 1px-inset fills
    {
        ImVec2 g0(p0.x + ix, p0.y + ix), g1(p1.x - ix, p1.y - ix);
        ImU32 aTop = IM_COL32((int)(a[0]*255), (int)(a[1]*255), (int)(a[2]*255),
                              (int)(tintTopA * 255));
        ImU32 aBtm = IM_COL32((int)(a[0]*255), (int)(a[1]*255), (int)(a[2]*255),
                              (int)(tintTopA * 0.28f * 255));
        ribbon_vGradRounded(dl, g0, g1, rIn, aTop, aBtm);   // rounded: reaches the corners
    }
    // Vertical shine gradient — lighter top, fading to transparent by ~55% height. Rounded top so
    // it hugs the button's rounded corners instead of poking a square edge past them.
    {
        ImVec2 g0(p0.x + ix, p0.y + ix);
        ImVec2 g1(p1.x - ix, p0.y + (p1.y - p0.y) * 0.55f);
        if (g1.x > g0.x && g1.y > g0.y) {
            float hc[4];
            ui::Palette::withAlpha(ui::palette.glassHighlight, hc, shineMul);
            ImU32 top = ui::Palette::toU32(hc);
            ImU32 btm = top & 0x00FFFFFFu;  // same RGB, alpha=0 (ABGR: bits 24-31 = A)
            ribbon_vGradRounded(dl, g0, g1, rIn, top, btm);
        }
    }
    dl->AddRect(p0, p1, border, rounding, 0, ui::metrics::stroke(1.0f));
}

// Fwd decl: procedural Finish check badge, drawn over the big button icon (defined below).
static void ribbon_sketchFinishBadge(ImDrawList* dl, float cx, float cy, float sz, bool isCheck);

// Bold active outline (deliverable 3): replaces the old accent underline / left-border cues.
// Filled with accentSoft + BOLD accent outline on the full bounding box.
static void ribbon_btnActive(ImDrawList* dl, ImVec2 p0, ImVec2 p1, float rounding) {
    dl->AddRectFilled(p0, p1, ui::Palette::toU32(ui::palette.accentSoft), rounding);
    dl->AddRect(p0, p1, ui::Palette::toU32(ui::palette.accent),
                rounding, 0, ui::metrics::stroke(2.0f));
}

// Draw a SW big button. Returns true if main area clicked.
// wOverride < 0 => size from the passed (face) label as before; >= 0 => draw at this stable width
// (the widest-variant width, so a split button never reflows/clips when the face label changes).
static bool ribbon_bigBtn(const char* cmd_id, const char* iconName,
                           const char* label, bool enabled, bool hasDrop,
                           float wOverride = -1.0f, bool highlight = false,
                           bool finishBadge = false) {
    float s   = ui::uiScale;
    // ONE snapped geometry rect for hit / fill / content. Measure helpers already snap widths;
    // re-snap here so a fractional wOverride (or unsapped caller) cannot desync the three.
    float bw  = snap(wOverride >= 0.0f ? wOverride : ribbon_bigBtnW(cmd_id, label, hasDrop));
    float bh  = ribbon_contentH();
    float rnd = ui::metrics::ribbonBtnRound();

    // Pixel-snap origin so the tile lands on the grid even when the group cursor is fractional
    // (SetCursorPos(bx) at non-integer uiScale). Hit, fill, and content all start from this.
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    p0.x = snap(p0.x);
    p0.y = snap(p0.y);
    ImGui::SetCursorScreenPos(p0);
    ImGui::PushID(cmd_id);

    bool clicked     = false;
    bool dropClicked = false;

    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(0, 0));
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(0, 0));
    ImGui::PushStyleColor(ImGuiCol_Button,        ImVec4(0,0,0,0));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(0,0,0,0));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,  ImVec4(0,0,0,0));

    // Split-button geometry (Hunter, 2026-07-07): the dropdown arrow lives in a short BOTTOM strip,
    // tucked into the bottom-RIGHT corner (under the icon, right side). The button keeps its natural
    // width (never fattened) and the taller body (kBodyH bump) gives the 2-line caption room so it is
    // no longer clipped by the strip.
    // dropH is snapped so mainH + dropH == bh exactly (no fractional seam between hit strips).
    float dropH = hasDrop ? snap(12.0f * s) : 0.0f;
    float mainH = bh - dropH;
    clicked = ImGui::InvisibleButton("##main", ImVec2(bw, mainH));
    bool mainHov    = ImGui::IsItemHovered();
    bool mainHovTip = ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal);
    bool mainPress  = ImGui::IsItemActive();
    bool dropHov    = false;

    if (hasDrop) {
        ImGui::SetCursorScreenPos(ImVec2(p0.x, p0.y + mainH));
        dropClicked = ImGui::InvisibleButton("##drop", ImVec2(bw, dropH));
        dropHov = ImGui::IsItemHovered();
        if (dropClicked && enabled) s_flyoutOpenReq = cmd_id;   // deferred (ID-scope fix)
    }

    ImDrawList* dl = ImGui::GetWindowDrawList();
    // Same snapped p0 + (bw, bh) used for fill, active outline, press, and content clip.
    const ImVec2 p1(p0.x + bw, p0.y + bh);

    // State layering: liquid-glass rest/hover fill → bold active outline → press overlay.
    const bool isActive = ribbon_isActive(cmd_id);
    const bool hovAny   = (mainHov || dropHov) && enabled;
    if (highlight) {
        // Highlighted action (e.g. Finish Sketch): keep the neutral glass fill but wrap it in an
        // accent-GREEN border so it stands out from the tool buttons without flooding the tile green.
        ribbon_btnRest(dl, p0, p1, rnd, enabled, /*inset=*/false, /*hovered=*/hovAny);
        const float* ok = ui::palette.statusOk;
        ImVec4 bc(ok[0], ok[1], ok[2], hovAny ? 1.0f : 0.90f);
        dl->AddRect(p0, p1, ImGui::ColorConvertFloat4ToU32(bc), rnd, 0, 2.0f * s);
    } else {
        ribbon_btnRest(dl, p0, p1, rnd, enabled, /*inset=*/false, /*hovered=*/hovAny);
    }
    if (isActive) {
        // Armed tool: BOLD accent outline around the full bounding box (app-wide rule).
        ribbon_btnActive(dl, p0, p1, rnd);
    }

    if (hasDrop) {
        // Split affordance: a hairline across the TOP of the drop strip, inset from both button
        // edges by the shared split inset, so a split button reads as split before its flyout is
        // ever opened. Color is the border role at reduced alpha — theme role, no literal.
        float dvc[4];
        ui::Palette::withAlpha(ui::palette.border, dvc, enabled ? 0.55f : 0.28f);
        const float dvInset = ui::metrics::ribbonSplitInset();
        dl->AddLine(ImVec2(snap(p0.x + dvInset),      snap(p0.y + mainH)),
                    ImVec2(snap(p0.x + bw - dvInset), snap(p0.y + mainH)),
                    ui::Palette::toU32(dvc), ui::metrics::stroke(1.0f));
        // Dropdown arrow: bottom strip, tucked to the RIGHT (under the icon, right side). Its own
        // strip sits below the caption block, so it never overlaps the text.
        float ax = snap(p0.x + bw - 8.0f * s);
        float ay = snap(p0.y + mainH + dropH * 0.5f);
        ImU32 aCol = enabled ? ui::Palette::toU32(ui::palette.text)
                             : ui::Palette::toU32(ui::palette.textDisabled);
        float hs = 3.5f * s;
        dl->AddTriangleFilled(ImVec2(ax - hs, ay - hs * 0.5f),
                              ImVec2(ax + hs, ay - hs * 0.5f),
                              ImVec2(ax,      ay + hs * 0.5f), aCol);
    }
    // Press feedback: darken the button slightly while the mouse button is held down.
    // Draws over hover/active layers — gives physical "click-down" feel.
    if (mainPress && enabled) {
        ImU32 pressOverlay = ribbon_pressScrim();
        dl->AddRectFilled(p0, p1, pressOverlay, rnd);
    }

    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar(2);

    // Draw icon + label — vertically centered within the CONTENT rect (full tile minus drop strip).
    // Content uses the same p0/bw/mainH as the hit/fill rect so the block cannot drift relative
    // to the background tile (the prior bug: fractional p0 + independent snap on cx/iy only).
    // Block = iconSz + optional swatch bar (4px+gap) + label lines. Centered with ≥3px edge pad.
    {
        float iconSz = snap(kBigIconSz * s);
        // Centered on the FULL button width — the arrow lives in the bottom strip, not a side
        // column, so dropdown and plain buttons center identically.
        float cx     = snap(p0.x + bw * 0.5f);
        const char* visibleLabel = ribbon_displayLabel(cmd_id, label);
        const float* matRgb = ribbon_matSwatchColor(cmd_id);

        std::string l1, l2;
        ribbon_wrapLabelFor(cmd_id, visibleLabel, l1, l2);
        float lineH   = ImGui::GetTextLineHeight();
        int   nLines  = (visibleLabel[0] == '\0') ? 0 : (l2.empty() ? 1 : 2);
        float swatchH = matRgb ? 5.0f * s : 0.0f;  // 4px bar + 1px gap
        float labelH  = nLines > 0 ? (lineH * (float)nLines + 2.0f * s + swatchH) : 0.0f;
        float blockH  = iconSz + labelH;
        // Vertical centering excludes the bottom arrow strip so icon+label never collide with it.
        // mainH is the same snapped value used for the main hit strip.
        float iy;
        if (visibleLabel[0] == '\0') {
            iy = snap(p0.y + (mainH - iconSz) * 0.5f);
        } else {
            iy = snap(p0.y + (mainH - blockH) * 0.5f);
            if (iy < p0.y + 3.0f * s) iy = snap(p0.y + 3.0f * s);
        }

        ribbon_icon(dl, iconName, cx, iy + iconSz * 0.5f, iconSz, enabled, ribbon_isActive(cmd_id));
        (void)finishBadge;  // Finish Sketch reads via its green border alone — no check-badge overlay.

        // Material color swatch: a 20x4 bar under the icon previewing the exact color this button
        // applies. The label below is nudged down (see ty) so the bar never overlaps the text.
        if (matRgb) {
            float swW = snap(20.0f * s), swH = snap(4.0f * s);
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

        ImU32 tCol = enabled ? ui::Palette::toU32(ui::palette.text)
                             : ui::Palette::toU32(ui::palette.textDisabled);
        float ty = snap(iy + iconSz + swatchH + 2.0f * s);
        // Clip to the content rect (same as main hit) so labels never spill into the drop strip.
        dl->PushClipRect(ImVec2(p0.x, p0.y), ImVec2(p0.x + bw, p0.y + mainH), true);
        if (visibleLabel[0] != '\0') {
            ImVec2 t1 = ImGui::CalcTextSize(l1.c_str());
            dl->AddText(nullptr, 0.0f, ImVec2(snap(cx - t1.x * 0.5f), ty), tCol, l1.c_str());
            if (!l2.empty()) {
                ImVec2 t2 = ImGui::CalcTextSize(l2.c_str());
                dl->AddText(nullptr, 0.0f, ImVec2(snap(cx - t2.x * 0.5f), snap(ty + lineH)),
                            tCol, l2.c_str());
            }
        }
        dl->PopClipRect();
    }

    if (mainHovTip && ribbon_tooltipsEnabled()) ribbon_tooltip(label, cmd_id, iconName, enabled);

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

// Draw a big button degraded to small-row layout (icon + side label, one row height).
static bool ribbon_bigBtnAsSmall(const char* cmd_id, const char* iconName,
                                  const char* label, bool enabled, bool hasDrop,
                                  float wOverride = -1.0f) {
    float s      = ui::uiScale;
    float rowH   = ribbon_smRowH();  // shared small-row grid height (same as ribbon_smBtn)
    float iconSz = snap(kSmIconSz * s);
    // §2.2 (same fix as ribbon_smBtn): drive the drawn/hit width from the SAME helper the column
    // packer measures with, so measure and draw never disagree.
    // N1-N5: wOverride (widest-variant width) keeps the row stable when the face label changes.
    // ONE snapped totalW / dropW / mainW for hit, fill, and content (mainW + dropW == totalW).
    float dropW  = hasDrop ? snap(kDropArrowW * s) : 0.0f;
    float totalW = snap(wOverride >= 0.0f ? wOverride
                                          : ribbon_smBtnW(label) + dropW);
    float mainW  = totalW - dropW;

    // Pixel-snap origin so hit/fill/content share one grid-aligned tile.
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    p0.x = snap(p0.x);
    p0.y = snap(p0.y);
    ImGui::SetCursorScreenPos(p0);
    ImGui::PushID(cmd_id);

    bool clicked     = false;
    bool dropClicked = false;

    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(0, 0));
    ImGui::PushStyleColor(ImGuiCol_Button,        ImVec4(0,0,0,0));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(0,0,0,0));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,  ImVec4(0,0,0,0));

    clicked = ImGui::InvisibleButton("##msm", ImVec2(mainW, rowH));
    bool mainHov    = ImGui::IsItemHovered();
    bool mainHovTip = ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal);
    bool mainPress  = ImGui::IsItemActive();
    bool dropHov    = false;

    if (hasDrop) {
        ImGui::SameLine(0, 0);
        dropClicked = ImGui::InvisibleButton("##mdr", ImVec2(dropW, rowH));
        dropHov = ImGui::IsItemHovered();
        if (dropClicked && enabled) s_flyoutOpenReq = cmd_id;   // deferred (ID-scope fix)
    }

    ImDrawList* dl_sm = ImGui::GetWindowDrawList();
    float rnd_sm = ui::metrics::ribbonBtnRound();
    const bool isActiveSm = ribbon_isActive(cmd_id);
    ImVec2 p1_sm(p0.x + totalW, p0.y + rowH);

    // State layering: liquid-glass rest/hover → active (bold outline) → press overlay.
    // inset=true: scale-aware 1px vertical inset so stacked rows don't share an edge pixel.
    // (Compact horizontal row: no room to stack the arrow under the 20px icon, so the arrow
    // keeps its trailing slot here — content is centered inside mainW, not the full totalW.)
    ribbon_btnRest(dl_sm, p0, p1_sm, rnd_sm, enabled, /*inset=*/true,
                   /*hovered=*/(mainHov || dropHov) && enabled);
    if (isActiveSm) ribbon_btnActive(dl_sm, p0, p1_sm, rnd_sm);

    if (hasDrop) {
        float ax = snap(p0.x + mainW + dropW * 0.5f);
        float ay = snap(p0.y + rowH * 0.5f);
        ImU32 aCol = enabled ? ui::Palette::toU32(ui::palette.text)
                             : ui::Palette::toU32(ui::palette.textDisabled);
        float hs = 3.0f * s;
        dl_sm->AddTriangleFilled(ImVec2(ax - hs, ay - hs * 0.5f),
                                 ImVec2(ax + hs, ay - hs * 0.5f),
                                 ImVec2(ax,      ay + hs * 0.5f), aCol);
    }
    if (mainPress && enabled) {
        dl_sm->AddRectFilled(p0, p1_sm, ribbon_pressScrim(), rnd_sm);
    }

    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar();

    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const float* matRgb = ribbon_matSwatchColor(cmd_id);
        // Center the icon+text block horizontally within the main (non-arrow) area.
        // Content rect is [p0.x, p0.x+mainW] × [p0.y, p0.y+rowH] — same as the main hit strip.
        const float matPad   = matRgb ? 6.0f * s : 0.0f;
        const float contentW = matPad + iconSz + 4.0f * s + ImGui::CalcTextSize(label).x;
        const float xoff     = snap(ImMax(ui::metrics::ribbonSmSplitInset(),
                                          (mainW - contentW) * 0.5f));
        float ix = snap(p0.x + xoff + matPad);
        float iy = snap(p0.y + (rowH - iconSz) * 0.5f);
        ribbon_icon(dl, iconName, ix + iconSz * 0.5f, iy + iconSz * 0.5f, iconSz, enabled,
                    ribbon_isActive(cmd_id));
        if (matRgb) {
            // Vertical color swatch strip to the left of the icon (compact row layout).
            float stW = snap(4.0f * s), stH = snap(14.0f * s);
            float stx = snap(p0.x + xoff);
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
        dl->PushClipRect(ImVec2(p0.x, p0.y), ImVec2(p0.x + mainW, p0.y + rowH), true);
        dl->AddText(nullptr, 0.0f, ImVec2(tx, ty), tCol, label);
        dl->PopClipRect();
    }

    if (mainHovTip && ribbon_tooltipsEnabled()) ribbon_tooltip(label, cmd_id, iconName, enabled);

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

        // N4 FIX (Hunter, 2026-07-07): a split button whose PARENT is itself a functional command
        // (revolve_boss/sweep_boss/loft_boss, sk_rect_corner, sk_arc_3pt, ...) had no flyout row for
        // the parent, so once a *_cut / variant was picked (last-used) the user was stuck one-way —
        // there was no way to switch BACK to the boss/base variant. Inject the parent itself as the
        // FIRST row (unless a kParentDefaults row already stands in for the parent's action, or the
        // parent is a pure flyout host with no engine action). Selecting it sets s_LastUsed[parent]=
        // parent; resolveFace then finds no matching sub and falls back to the parent's own face.
        // A launcher's parent is the button face itself, so its flyout lists only the shortcuts.
        const ui::RibbonCmd* parentCmd = nullptr;
        if (!pd && !ribbon_isLauncher(parentId)) {
            int n = 0;
            const ui::RibbonCmd* all = ui::getRibbonCmds(n);
            for (int i = 0; i < n; ++i)
                if (std::strcmp(all[i].cmd_id, parentId) == 0) { parentCmd = &all[i]; break; }
            // Only inject when the parent has its own action (not schema-reserved / stretch).
            if (parentCmd && !parentCmd->stretch) rows.push_back(parentCmd);
        }

        // Dedupe: the injected parent-default row (e.g. sk_polygon -> sk_poly_hex "Hexagon") is often
        // ALSO one of the variant subs. Adding it twice pushed the same cmd_id ID twice — the "2
        // visible items with conflicting ID" warning — and listed "Hexagon" twice. Skip the sub that
        // the default already stands in for.
        for (const auto* sc : subs) {
            if (pd && std::strcmp(sc->cmd_id, pd->defaultCmd) == 0) continue;
            rows.push_back(sc);
        }

        float maxLblW = 0.0f;
        for (const auto* sc : rows)
            maxLblW = ImMax(maxLblW, ImGui::CalcTextSize(sc->label).x);
        float popW = padX + iconSz + gapX + maxLblW + padX;

        for (const auto* sc : rows) {
            bool scEnabled = ribbon_cmdEnabled(sc);
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
                ribbon_icon(dl, sc->icon, ix + iconSz * 0.5f, iy + iconSz * 0.5f, iconSz,
                            scEnabled);
            }
            {
                float tx = snap(cp0.x + padX + iconSz + gapX);
                float ty = snap(cp0.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f);
                ImU32 tCol = scEnabled ? ui::Palette::toU32(ui::palette.text)
                                       : ui::Palette::toU32(ui::palette.textDisabled);
                dl->AddText(nullptr, 0.0f, ImVec2(tx, ty), tCol, sc->label);
            }

            // Same glass tooltip as the buttons: description, and the real disabled reason (e.g. a
            // web-build gap, or what a reserved row lacks).
            if (rowHov && ribbon_tooltipsEnabled())
                ribbon_tooltip(sc->label, sc->cmd_id, sc->icon, scEnabled);

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

// True for the injected "Finish Sketch" big button (2D or 3D). Identity is by pointer: only the two
// kSketch*FinishOnSketchTab singletons take this path (their cmd_id doubles as a real finish
// command, so a cmd_id match alone would also catch the ribbon_def rows).
static bool ribbon_isSketchFinish(const ui::RibbonCmd* cmd) {
    return cmd == &kSketchFinishOnSketchTab || cmd == &kSketch3dFinishOnSketchTab;
}

// Draw a small badge (accent disc + white check ✓, or err disc + white ✗) in the bottom-right of a
// big button's icon — the atlas has no dedicated sketch_finish/sketch_cancel glyph, so Finish/Cancel
// are marked procedurally in the same clean check/✗ style the old commit chips used.
static void ribbon_sketchFinishBadge(ImDrawList* dl, float cx, float cy, float sz, bool isCheck) {
    float s = ui::uiScale;
    float br = sz * 0.34f;                 // badge radius relative to the 32px icon
    float bx = cx + sz * 0.30f;            // tucked bottom-right of the icon block
    float by = cy + sz * 0.30f;
    const float* role = isCheck ? ui::palette.statusOk : ui::palette.statusErr;
    ImVec4 disc(role[0], role[1], role[2], 1.0f);
    dl->AddCircleFilled(ImVec2(bx, by), br, ImGui::ColorConvertFloat4ToU32(disc));
    const ImU32 gl = ImGui::ColorConvertFloat4ToU32(ui::maxContrastTextOn(disc));
    float lw = ImMax(1.6f * s, br * 0.28f);
    if (isCheck) {
        dl->AddLine(ImVec2(bx - br * 0.45f, by + br * 0.02f),
                    ImVec2(bx - br * 0.08f, by + br * 0.42f), gl, lw);
        dl->AddLine(ImVec2(bx - br * 0.08f, by + br * 0.42f),
                    ImVec2(bx + br * 0.50f, by - br * 0.40f), gl, lw);
    } else {
        dl->AddLine(ImVec2(bx - br * 0.42f, by - br * 0.42f),
                    ImVec2(bx + br * 0.42f, by + br * 0.42f), gl, lw);
        dl->AddLine(ImVec2(bx - br * 0.42f, by + br * 0.42f),
                    ImVec2(bx + br * 0.42f, by - br * 0.42f), gl, lw);
    }
}

// Dedicated Cancel-only dropdown for the Finish Sketch button. Deliberately NOT ribbon_flyoutPopup:
// that path injects the parent (Exit Sketch) row and writes s_LastUsed, which would corrupt the
// finish button's face + plain-click action. This one offers a single "Cancel Sketch" item that
// fires the matching *_cancel command and never touches last-used state. Uses the same deferred
// s_flyoutOpenReq open handshake as the other split buttons (ID-scope fix, see comment ~L35).
static void ribbon_sketchFinishFlyout(const ui::RibbonCmd* cmd) {
    float s = ui::uiScale;
    const char* parentId  = cmd->cmd_id;   // sketch_exit / sketch3d_exit
    const bool  is3d      = std::strcmp(parentId, "sketch3d_exit") == 0;
    const char* cancelCmd = is3d ? "sketch3d_cancel" : "sketch_cancel";

    std::string popId = std::string(parentId) + "_drop";
    if (s_flyoutOpenReq == parentId) {
        ImGui::OpenPopup(popId.c_str());
        s_flyoutOpenReq.clear();
    }
    if (ImGui::BeginPopup(popId.c_str(), ImGuiWindowFlags_NoMove)) {
        float rowH   = snap(28.0f * s);
        float iconSz = snap(20.0f * s);
        float padX   = snap(8.0f * s);
        float gapX   = snap(6.0f * s);
        const char* label = "Cancel Sketch";
        float popW = padX + iconSz + gapX + ImGui::CalcTextSize(label).x + padX;

        ImVec2 cp0 = ImGui::GetCursorScreenPos();
        ImGui::PushID("##sk_cancel_row");
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(0, 0));
        ImGui::PushStyleColor(ImGuiCol_Button,        ImVec4(0,0,0,0));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(0,0,0,0));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive,  ImVec4(0,0,0,0));
        bool rowClicked = ImGui::InvisibleButton("##fr", ImVec2(popW, rowH));
        bool rowHov     = ImGui::IsItemHovered();
        ImGui::PopStyleColor(3);
        ImGui::PopStyleVar();

        ImDrawList* dl = ImGui::GetWindowDrawList();
        if (rowHov)
            dl->AddRectFilled(cp0, ImVec2(cp0.x + popW, cp0.y + rowH),
                              ui::Palette::toU32(ui::palette.buttonHovered),
                              ui::metrics::ribbonBtnRound());
        {
            // Base pencil glyph + procedural ✗ badge (no dedicated sketch_cancel atlas glyph).
            float ix = snap(cp0.x + padX);
            float iy = snap(cp0.y + (rowH - iconSz) * 0.5f);
            ribbon_icon(dl, "sketch", ix + iconSz * 0.5f, iy + iconSz * 0.5f, iconSz, true);
            ribbon_sketchFinishBadge(dl, ix + iconSz * 0.5f, iy + iconSz * 0.5f, iconSz,
                                     /*isCheck=*/false);
        }
        {
            float tx = snap(cp0.x + padX + iconSz + gapX);
            float ty = snap(cp0.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f);
            dl->AddText(nullptr, 0.0f, ImVec2(tx, ty),
                        ui::Palette::toU32(ui::palette.text), label);
        }
        if (rowClicked) {
            ribbon_fire(cancelCmd);
            ImGui::CloseCurrentPopup();
        }
        ImGui::PopID();
        ImGui::EndPopup();
    }
}

// Draw a small button. Returns true if clicked.
static bool ribbon_smBtn(const char* cmd_id, const char* iconName,
                          const char* label, bool enabled, float wOverride = -1.0f) {
    float s      = ui::uiScale;
    // ONE snapped geometry for hit / fill / content — same integer totalW and rowH at every scale.
    // rowH is the shared small-row grid height, identical to degraded big rows (ribbon_smRowH).
    float rowH   = ribbon_smRowH();
    float iconSz = snap(kSmIconSz * s);
    // §2.2: drive the drawn/hit width from the SAME helper the packer measures with, so measure
    // and draw never disagree (previously the helper returned text+18*s but this drew text+10*s).
    // wOverride >= 0 draws at the COLUMN width so every small button in a stacked column shares one
    // edge (clean, aligned display) instead of each hugging its own label width.
    float totalW = snap(wOverride >= 0.0f ? wOverride : ribbon_smBtnW(label));

    // Pixel-snap origin so the background tile and content share one grid-aligned rect.
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    p0.x = snap(p0.x);
    p0.y = snap(p0.y);
    ImGui::SetCursorScreenPos(p0);
    ImGui::PushID(cmd_id);

    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(0, 0));
    ImGui::PushStyleColor(ImGuiCol_Button,        ImVec4(0,0,0,0));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(0,0,0,0));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,  ImVec4(0,0,0,0));
    bool clicked = ImGui::InvisibleButton("##sm", ImVec2(totalW, rowH));
    bool hov     = ImGui::IsItemHovered();
    bool hovTip  = ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal);
    bool press   = ImGui::IsItemActive();
    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar();

    // State layering: liquid-glass rest/hover (unified with big buttons) → active (bold
    // outline) → press overlay. inset=true: scale-aware 1px vertical inset so stacked small
    // rows don't share a grid-line edge. Fill uses the same p0/totalW/rowH as the hit rect.
    {
        ImDrawList* dl_s = ImGui::GetWindowDrawList();
        float rnd_s = ui::metrics::ribbonBtnRound();
        ImVec2 p1_s(p0.x + totalW, p0.y + rowH);
        const bool isActiveSm2 = ribbon_isActive(cmd_id);
        ribbon_btnRest(dl_s, p0, p1_s, rnd_s, enabled, /*inset=*/true, /*hovered=*/hov && enabled);
        if (isActiveSm2) {
            ribbon_btnActive(dl_s, p0, p1_s, rnd_s);
        }
        if (press && enabled) {
            dl_s->AddRectFilled(p0, p1_s, ribbon_pressScrim(), rnd_s);
        }
    }

    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const float* matRgb = ribbon_matSwatchColor(cmd_id);
        // Center the icon+text block horizontally within the (column-wide) button — same totalW
        // as hit/fill, so content cannot drift relative to the background tile.
        const float matPad   = matRgb ? 6.0f * s : 0.0f;
        const float contentW = matPad + iconSz + 4.0f * s + ImGui::CalcTextSize(label).x;
        const float xoff     = snap(ImMax(0.0f, (totalW - contentW) * 0.5f));
        float ix  = snap(p0.x + xoff + matPad);
        float iy  = snap(p0.y + (rowH - iconSz) * 0.5f);
        ribbon_icon(dl, iconName, ix + iconSz * 0.5f, iy + iconSz * 0.5f,
                    iconSz, enabled, ribbon_isActive(cmd_id));
        if (matRgb) {
            // Vertical color swatch strip to the left of the icon.
            float stW = snap(4.0f * s), stH = snap(14.0f * s);
            float stx = snap(p0.x + xoff);
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
        dl->AddText(nullptr, 0.0f, ImVec2(tx, ty), tCol, label);
    }

    if (hovTip && ribbon_tooltipsEnabled()) ribbon_tooltip(label, cmd_id, iconName, enabled);

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

void setRibbonActiveCmd(const char* cmd_id) {
    s_activeCmd = (cmd_id && *cmd_id) ? cmd_id : "";
}

void setRibbonSketchActive(bool active) {
    s_sketchActive = active;
}

void setRibbonSketch3DActive(bool active) {
    s_sketch3dActive = active;
}

void setRibbonWorkspace(int workspace) {
    s_workspace = workspace;
}

void setRibbonDisabledPrefix(const char* prefix, const char* reason) {
    s_disabledPrefix = prefix ? prefix : "";
    s_disabledReason = (reason && *reason) ? reason : "Unavailable";
}

void setRibbonStudyContext(const char* studyTypeKey) {
    s_studyCtx = studyTypeKey ? studyTypeKey : "";
    s_studyCtxFrame = ImGui::GetFrameCount();
}

float drawRibbon(int& activeTab, bool visible, bool showSimulationTools, bool showPcbTools,
                 float titleBarH, bool showDrawingTools) {
    if (!visible) return 0.0f;
    // PLM(5), PartsLibrary(6), AI(7): no ribbon band — viewport reclaims the full height.
    if (s_workspace >= 5) return 0.0f;
    ribbon_seedDefaults();
    if (activeTab < 0 || activeTab >= kRibbonTabCount ||
        !ribbon_tabVisible(activeTab, showSimulationTools, showPcbTools, showDrawingTools)) {
        // Land on the first visible tab — in drawing mode tab 0 is hidden, so pick the
        // first drawing tab; on a 3D doc fall back to Features.
        activeTab = 0;
        for (int k = 0; k < kRibbonTabCount; ++k)
            if (const int i = kTabDisplayOrder[k];
                ribbon_tabVisible(i, showSimulationTools, showPcbTools, showDrawingTools)) {
                activeTab = i;
                break;
            }
    }

#ifdef __EMSCRIPTEN__
    const bool mobile = ImGui::GetIO().DisplaySize.x <= 900.0f ||
                        EM_ASM_INT({ return window.matchMedia('(pointer: coarse)').matches; });
#else
    const bool mobile = false;
#endif
    float s      = ui::uiScale;
    float tabH   = mobile ? ui::metrics::px(44.0f) : kTabH * s;
    // Shared snapped body height — panel, buttons, caption, and chrome all use this.
    float bodyH2 = mobile ? ui::metrics::px(44.0f) : ribbon_bodyH();
    float totalH = (mobile ? tabH : tabH + bodyH2) + kBottomPad * s;

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
    // Translucent chrome base so the glassBackground layers below actually read as glass — the
    // tooltip does the same via a transparent PopupBg. Keep the chrome-tint hue (so the band still
    // follows the Global glass tint) but drop its alpha; the glass fill/shine/border supply the
    // surface. See SHARED VISUAL CONTRACT in work/tasks/T-G15.md.
    ImVec4 ribbonBase = ribbon_chromeTint(palette.ribbonBg);
    ribbonBase.w = 0.0f;
    ImGui::PushStyleColor(ImGuiCol_WindowBg, ribbonBase);

    ImGui::Begin("##RibbonBar", nullptr, flags);
    ImGui::PopStyleVar(3);
    ImGui::PopStyleColor();

    ImDrawList* dl   = ImGui::GetWindowDrawList();
    ImVec2      wPos = ImGui::GetWindowPos();

    // Liquid-glass chrome: paint the shared glass material (the SAME glassBackground helper the
    // tooltips use) over the band so the ribbon matches the rest of the app. Full-width flush band
    // -> rounding 0 (rounded corners would gap at the window edges); the glass READ comes from the
    // translucent fill + inner shine + hairline border. See SHARED VISUAL CONTRACT in
    // work/tasks/T-G15.md (opus-gleam, 2026-07-03).
    {
        ImVec2 wSize = ImGui::GetWindowSize();
        ui::glassBackground(dl, wPos, ImVec2(wPos.x + wSize.x, wPos.y + wSize.y), 0.0f);
    }

    // --- Tab strip ---
    float tabTopPad = 0.0f;
    float tabBtnH   = tabH;
    float tabX = wPos.x + 6.0f * s;
    float tabY = wPos.y + tabTopPad;

    if (mobile) {
        ImGui::SetCursorScreenPos(ImVec2(tabX, tabY));
        ImGui::SetNextItemWidth(vp->Size.x * 0.5f - ui::metrics::px(8.0f));
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,
                            ImVec2(ui::metrics::px(10.0f),
                                   (tabH - ImGui::GetFontSize()) * 0.5f));
        if (ImGui::BeginCombo("##ribbon_tabs", kRibbonTabs[activeTab])) {
            for (int k = 0; k < kRibbonTabCount; ++k) {
                const int i = kTabDisplayOrder[k];
                if (!ribbon_tabVisible(i, showSimulationTools, showPcbTools, showDrawingTools)) continue;
                if (ImGui::Selectable(kRibbonTabs[i], activeTab == i)) activeTab = i;
            }
            ImGui::EndCombo();
        }
        ImGui::PopStyleVar();
    } else {
    for (int k = 0; k < kRibbonTabCount; ++k) {
        const int i = kTabDisplayOrder[k];
        if (!ribbon_tabVisible(i, showSimulationTools, showPcbTools, showDrawingTools)) continue;
        const char* tabLabel = kRibbonTabs[i];
        ImVec2 tsize  = ImGui::CalcTextSize(tabLabel);
        // audit: trimmed from 44px of chrome to 38 — icon(14) + gap(4) + 10px pad each side
        float  tw     = tsize.x + 18.0f * s + 20.0f * s;
        bool   isAct  = (activeTab == i);

        ImVec2 tp0(tabX, tabY);
        ImVec2 tp1(tabX + tw, tabY + tabBtnH);

        ImGui::SetCursorScreenPos(tp0);
        ImGui::PushID(i + 1000);
        if (ImGui::InvisibleButton(tabLabel, ImVec2(tw, tabBtnH))) activeTab = i;
        bool tabHov = ImGui::IsItemHovered();
        ImGui::PopID();

        {
            // Colored gradient background on EVERY tab (the same Layers-style wash the rows/buttons
            // use), stronger on the active tab and on hover. The active tab is framed by the glow
            // border (coloredRowWash selected=true) — no separate bold outline box.
            ui::glassRowBackground(dl, tp0, tp1, 4.0f, /*stripe*/false, /*selected*/false,
                                   isAct ? 0.42f : (tabHov ? 0.34f : 0.20f));
            const float strength = isAct ? 0.30f : (tabHov ? 0.20f : 0.12f);
            ui::coloredRowWash(dl, tp0, tp1, 4.0f, palette.accent, strength, /*selected*/isAct);
            if (!isAct) {
                // Resting border on every non-active tab so each tool tab reads as a discrete
                // button even when not selected/highlighted. A thin hairline, clearly lighter and
                // thinner than the active tab's colored glow frame (coloredRowWash selected=true).
                float bc[4];
                Palette::withAlpha(palette.border, bc, tabHov ? 0.60f : 0.42f);
                dl->AddRect(tp0, tp1, Palette::toU32(bc), 4.0f, 0, ui::metrics::stroke(1.0f));
            }
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
        const float tIconY    = snap(tp0.y + (tabBtnH - tIconSz) * 0.5f - 1.0f * s);
        // SDF path renders the 14px glyph crisp (the raster 20px cell downscaled to mush here);
        // raster fallback keeps the previous tinted AddImage behavior byte-for-byte.
        ui::drawIconGlass(dl, kTabIcons[i],
                          ImVec2(tGroupX + tIconSz * 0.5f, tIconY + tIconSz * 0.5f), tIconSz,
                          ImGui::ColorConvertFloat4ToU32(lc), 0);
        dl->AddText(nullptr, 0.0f,
                    ImVec2(snap(tGroupX + tIconSz + tIconGap), textY),
                    ImGui::ColorConvertFloat4ToU32(lc), tabLabel);

        tabX += tw + 2.0f * s;
    }
    }

    dl->AddLine(ImVec2(wPos.x,              wPos.y + tabH),
                ImVec2(wPos.x + vp->Size.x, wPos.y + tabH),
                Palette::toU32(palette.border), ui::metrics::stroke(1.0f));

    // --- Build group list ---
    int cmdCount = 0;
    const RibbonCmd* cmds = getRibbonCmds(cmdCount);
    const char* actTabName = kRibbonTabs[activeTab];
    s_tipTab = actTabName;   // per-tab tooltip overrides (ribbon_tipFor)

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

    // A 3D sketch being edited swaps the WHOLE Sketch-tab command source to the "Sketch3D"
    // pseudo-tab (ribbon_def.cpp) instead of the normal 2D tool rows (Hunter red-pen item 1).
    const bool onSketchTab   = std::strcmp(actTabName, "Sketch") == 0;
    const char* srcTabName   = (onSketchTab && s_sketch3dActive) ? "Sketch3D" : actTabName;

    for (int i = 0; i < cmdCount; ++i) {
        const RibbonCmd& c = cmds[i];
        if (std::strcmp(c.tab, srcTabName) != 0) continue;
        // Superseded (a later row for this tab + cmd_id replaces it) or retired rows never render.
        if (!ribbonRowActive(i)) continue;
        // Simulate: a study type's own setup row on a shared tab (Buckling "Modes" on Static, …)
        // shows only while a study of that type is selected.
        if (ribbonStudyGate(c.cmd_id, ribbon_studyCtx(), nullptr) == RibbonStudyGate::Hidden)
            continue;
        // Sketch tab, no sketch being edited: present "New Sketch" in the "Exit Sketch" slot so the
        // user can start a sketch from this tab (matches the Features-tab entry point). Once a sketch
        // is open, s_sketchActive is true and the real "Exit Sketch" button shows.
        // While editing ANY sketch, the single highlighted "Finish Sketch" button takes the place of
        // BOTH New Sketch and New 3D Sketch — suppress the "New 3D Sketch" entry so only Finish shows.
        if (std::strcmp(c.cmd_id, "sketch3d_new") == 0 && (s_sketchActive || s_sketch3dActive))
            continue;
        const RibbonCmd* cp = &c;
        if (std::strcmp(c.cmd_id, "sketch_exit") == 0) {
            // Editing a 2D sketch: this slot becomes the highlighted "Finish Sketch" big button
            // (dropdown → Cancel Sketch), taking the place + size of New Sketch. Not editing: it is
            // the "New Sketch" entry point so a sketch can be started straight from the Sketch tab.
            cp = s_sketchActive ? &kSketchFinishOnSketchTab : &kSketchNewOnSketchTab;
        }
        // Same treatment for the 3D sketcher's "Exit Sketch" slot while a 3D sketch is being edited.
        if (std::strcmp(c.cmd_id, "sketch3d_exit") == 0) {
            if (s_sketch3dActive) cp = &kSketch3dFinishOnSketchTab;
        }
        RibbonGroup& grp = ribbon_findOrAddGroup(groups, cp->group);
        if (cp->sub_of)  grp.subCmds.push_back(cp);
        else if (cp->big) grp.bigCmds.push_back(cp);
        else if (!smallIsHidden(*cp)) grp.smCmds.push_back(cp);
    }

    // Emit groups in this tab's declared workflow order (kTabGroupOrder) instead of the order
    // their rows happen to sit in the append-only command table. No-op for tabs with no entry.
    ribbon_orderGroups(groups, srcTabName);

    // Sketch▸Draw: stack "Rotate Ent." under "Move Ent." (same column) to save horizontal space.
    for (auto& g : groups) {
        if (g.label && std::strcmp(g.label, "Draw") == 0)
            ribbon_preferStackedPair(g.smCmds, "sk_move_entities", "sk_rotate_entities");
    }

    // --- C25-W3G: priority collapse assignment ---
    // Available body width. Reserve the overflow chevron up front so it never lands off-edge once
    // any group has to be hidden. (The sketch Finish/Cancel controls now live inline in the Sketch
    // group's New-Sketch slot, so no right-edge cluster width is reserved anymore.)
    float chevronW  = 22.0f * s;                // reserved for overflow '»' button
    float bodyW     = (mobile ? vp->Size.x * 0.5f : vp->Size.x) - 8.0f * s;
    float availW    = bodyW - chevronW;

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
    // Pass 1b: give back big layout where it now fits. Pass 1 is greedy right-to-left and never
    // undoes a step, so a group whose small layout saves only a few px (e.g. Hole: two bigs vs one
    // two-row column) gets demoted even when the group degraded after it freed far more than was
    // needed. Restore the CHEAPEST groups first (smallest big-minus-small width, leftmost on ties)
    // so the leftover slack returns as many groups as possible to full big-button layout.
    if (totalW <= availW) {
        std::vector<int> demoted;
        for (int gi = 0; gi < (int)groups.size(); ++gi)
            if (groups[gi].collapse == COL_SMALL) demoted.push_back(gi);
        std::vector<float> bigW(groups.size(), 0.0f);
        for (int gi : demoted) bigW[gi] = ribbon_groupNaturalW(groups[gi], COL_BIG);
        std::stable_sort(demoted.begin(), demoted.end(), [&](int a, int b) {
            return bigW[a] - natW[a] < bigW[b] - natW[b];
        });
        for (int gi : demoted) {
            if (totalW - natW[gi] + bigW[gi] > availW) continue;
            totalW += bigW[gi] - natW[gi];
            groups[gi].collapse = COL_BIG;
            natW[gi] = bigW[gi];
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

    // Hard fit guard. The two passes above balance MEASURED widths against availW; this walks the
    // exact left-to-right run the draw loop will use and hides everything from the first group
    // whose right edge would land past the body child's clip edge. The pill and caption are painted
    // into the PARENT (unclipped) draw list while the buttons live in the clipped ##RibbonBody
    // child, so a group that only half fits would show its chrome over cut-off labels. Anything
    // that does not fit WHOLE belongs to the overflow chevron.
    {
        float run = 0.0f;
        bool  cut = false;
        for (int gi = 0; gi < (int)groups.size(); ++gi) {
            if (!cut && groups[gi].collapse != COL_HIDDEN && run + natW[gi] <= availW) {
                run += natW[gi];
                continue;
            }
            cut = true;
            groups[gi].collapse = COL_HIDDEN;
            natW[gi]            = 0.0f;
        }
    }
    // Phones use the existing command overflow instead of spending the landscape
    // viewport on a full-height ribbon. Every group and split-button variant remains available.
    if (mobile) {
        for (auto& group : groups) group.collapse = COL_HIDDEN;
        std::fill(natW.begin(), natW.end(), 0.0f);
    }

    // Determine if we need the chevron
    bool anyHidden = false;
    for (const auto& g : groups) if (g.collapse == COL_HIDDEN) { anyHidden = true; break; }

    // --- Body child window (no horizontal scroll — reflow handles it) ---
    // TRANSPARENT child bg: the default ChildBg is the OPAQUE panelBg role, which painted a flat
    // solid rectangle over the ribbon's liquid-glass band (glassBackground). That opaque panel only
    // covered the BODY (the tab strip draws outside this child, straight on the glass), so its top
    // edge — a full-width straight line right under the buttons' rounded bottom corners — read as a
    // hard border/step against the buttons' rounded tiles (worst at the bottom-left/right corners,
    // where the panel edge cut across the curve). Dropping the fill lets the same glass show through
    // the whole band, so the body matches the tabs and the buttons sit on one continuous surface.
    //
    // WindowPadding MUST be zero: theme default (~15×13 scaled) would offset SetCursorPos content
    // relative to GetWindowPos-based panel rects, so tiles and the group pill no longer share an
    // origin. Pad is applied explicitly via ribbon_groupPadY() so panel and buttons agree.
    ImGui::SetCursorScreenPos(ImVec2(wPos.x + (mobile ? vp->Size.x * 0.5f : 0.0f) + 4.0f * s,
                                    wPos.y + (mobile ? 0.0f : tabH) + 2.0f * s));
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0.0f, 0.0f));
    ImGui::BeginChild("##RibbonBody",
                      ImVec2(bodyW, bodyH2 + 2.0f * s),
                      false,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    ImGui::PopStyleVar();
    ImGui::PopStyleColor();

    // Clip the group CHROME to the body child's rect. Pill / divider / caption are drawn into the
    // parent ribbon draw list (so they composite BEHIND the buttons, which belong to this child),
    // and that list is NOT clipped by the child — chrome could otherwise paint past the very edge
    // that cuts the button labels. Same rect, same fate: chrome and content appear together or not
    // at all. Popped immediately after the group loop.
    const ImVec2 bodyClipMin = ImGui::GetWindowPos();
    const ImVec2 bodyClipMax(bodyClipMin.x + bodyW, bodyClipMin.y + bodyH2 + 2.0f * s);
    dl->PushClipRect(bodyClipMin, bodyClipMax, true);

    float curX = 0.0f;

    for (int gi = 0; gi < (int)groups.size(); ++gi) {
        RibbonGroup& grp = groups[gi];
        if (grp.collapse == COL_HIDDEN) continue;

        bool isSmall = (grp.collapse != COL_BIG);
        float grpW = natW[gi];
        int rowsPerCol = ribbon_rowsPerCol();
        // Shared vertical metrics for this group: padY + content zone + caption + padY.
        // Buttons start at contentY so they sit fully inside the pill at any uiScale.
        const float padY     = ribbon_groupPadY();
        const float contentH = ribbon_contentH();
        const float contentY = padY;
        // Centering: grpW is floored at the caption width (ribbon_captionMinW), so a group whose
        // caption is wider than its buttons gets a pill wider than its content. Left-aligning the
        // tiles in that pill leaves them visibly off the caption's axis — center the whole content
        // block in the slack instead, so pill, caption and tiles share one center line.
        const float slack    = ImMax(0.0f, grpW - ribbon_groupContentW(grp, grp.collapse));
        const float startX   = curX + kGroupPadX * s + snap(slack * 0.5f);

        // --- Liquid-glass group background (drawn into the ribbon window's draw list) ---
        // The button widgets render into the ##RibbonBody child window, which composites ON TOP of
        // this parent draw list (see the ChildBg note above — the child's default opaque bg used to
        // paint OVER this band), so this frosted+washed pill always sits behind the group's
        // icons/labels regardless of code order. Horizontal extent matches the caption bounds
        // (curX + half a group pad on each side) so adjacent groups don't abut and the divider bar
        // has a gap to sit in. Vertical extent = padY + contentH + captionH + padY so the pill
        // encloses every button tile with even padding (same snapped metrics the buttons use).
        {
            ImVec2 cwp   = ImGui::GetWindowPos();
            float  gpad  = kGroupPadX * s * 0.5f;
            float  rnd   = 6.0f * s;                       // pinned spec corner radius
            // Pill top/bottom: even padY inset from the body cell (matches contentY / caption bottom).
            ImVec2 g0(snap(cwp.x + curX + gpad), snap(cwp.y + padY));
            ImVec2 g1(snap(cwp.x + curX + grpW - gpad), snap(cwp.y + bodyH2 - padY));
            float gtint[3] = { ui::palette.accent[0], ui::palette.accent[1], ui::palette.accent[2] };
            ui::glassRowBackground(dl, g0, g1, rnd, /*stripe*/false, /*selected*/false,
                                   /*washAlpha*/0.30f);
            ui::coloredRowWash(dl, g0, g1, rnd, gtint, /*strength*/0.16f, /*selected*/false);
        }

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
            // A launcher's shortcut fires once; the face stays the launcher (no last-used swap).
            if (chosen) {
                if (!ribbon_isLauncher(cmd->cmd_id)) s_LastUsed[cmd->cmd_id] = chosen;
                ribbon_fire(chosen);
            }
        };

        if (!isSmall) {
            // --- BIG layout: big buttons left-to-right, then the small zone in measured columns ---
            // Inter-button gap ONLY between bigs (not after the last). When a small zone follows,
            // a matching 2dp mid-gap separates the zones — same math as ribbon_groupNaturalW so
            // left/right pill padding stays equal.
            float bx = startX;
            for (int bi = 0; bi < (int)grp.bigCmds.size(); ++bi) {
                const auto* cmd = grp.bigCmds[bi];
                if (bi > 0) bx += 2.0f * s;
                // Finish Sketch: highlighted big button with a dedicated Cancel-only dropdown — NOT
                // routed through the generic split-button last-used machinery (a plain click must
                // always finish; only the flyout cancels). Rendered in the New-Sketch slot.
                if (ribbon_isSketchFinish(cmd)) {
                    ImGui::SetCursorPos(ImVec2(bx, contentY));
                    float wStable = ribbon_bigBtnW(cmd->cmd_id, cmd->label, /*hasDrop=*/true);
                    ribbon_bigBtn(cmd->cmd_id, cmd->icon, cmd->label, true, /*hasDrop=*/true,
                                  wStable, /*highlight=*/true, /*finishBadge=*/true);
                    ribbon_sketchFinishFlyout(cmd);
                    bx += wStable;
                    continue;
                }
                bool drop = ribbon_hasSubs(grp, cmd);
                ImGui::SetCursorPos(ImVec2(bx, contentY));
                const char* showIcon; const char* showLabel;
                resolveFace(cmd, drop, showIcon, showLabel);
                // N1-N5: stable widest-variant width — draw and advance share it so the face label
                // (variant) can change without reflowing or bleeding into the neighbor.
                float wStable = ribbon_bigBtnWMax(grp, cmd, drop);
                ribbon_bigBtn(cmd->cmd_id, showIcon, showLabel, ribbon_cmdEnabled(cmd), drop, wStable);
                if (drop) runFlyout(cmd);
                bx += wStable;
            }

            // small zone: pack into independently-measured columns (defect 1)
            if (!grp.smCmds.empty()) {
                if (!grp.bigCmds.empty()) bx += 2.0f * s;  // mid-gap between big and small zones
                std::vector<float> widths;
                for (const auto* cmd : grp.smCmds) widths.push_back(ribbon_smBtnW(cmd->label));
                auto cols = ribbon_packColumns(widths, rowsPerCol);
                float sx = bx;
                for (const auto& col : cols) {
                    // Every column sits on the shared row grid: a partially-filled column
                    // (2 of 3) occupies rows 1-2 and aligns row-for-row with its neighbours.
                    for (int r = 0; r < col.count; ++r) {
                        const auto* cmd = grp.smCmds[col.first + r];
                        ImGui::SetCursorPos(ImVec2(sx, ribbon_smRowY(contentY, r)));
                        ribbon_smBtn(cmd->cmd_id, cmd->icon, cmd->label, ribbon_cmdEnabled(cmd),
                                     col.width);
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
                if (i < nBig) {
                    // N1-N5: degraded big row reserves its widest-variant width.
                    widths.push_back(ribbon_bigBtnAsSmallWMax(grp, rows[i],
                                                              ribbon_hasSubs(grp, rows[i])));
                } else {
                    widths.push_back(ribbon_smBtnW(rows[i]->label));
                }
            }
            auto cols = ribbon_packColumns(widths, rowsPerCol);
            float sx = startX;
            for (const auto& col : cols) {
                // Same shared row grid as the big-layout small zone: degraded big rows and plain
                // small rows have one height and one pitch, so mixed columns line up.
                for (int r = 0; r < col.count; ++r) {
                    int idx = col.first + r;
                    const auto* cmd = rows[idx];
                    ImGui::SetCursorPos(ImVec2(sx, ribbon_smRowY(contentY, r)));
                    if (idx < nBig) {
                        // Finish Sketch degraded row: keep its Cancel dropdown (dedicated flyout,
                        // not runFlyout — see the big-layout branch above for why).
                        if (ribbon_isSketchFinish(cmd)) {
                            // Draw at the COLUMN width so every row in this stack shares one edge.
                            ribbon_bigBtnAsSmall(cmd->cmd_id, cmd->icon, cmd->label,
                                                 true, /*hasDrop=*/true, col.width);
                            ribbon_sketchFinishFlyout(cmd);
                            continue;
                        }
                        bool drop = ribbon_hasSubs(grp, cmd);
                        const char* showIcon; const char* showLabel;
                        resolveFace(cmd, drop, showIcon, showLabel);
                        // Uniform column width (the packer already reserved the widest variant).
                        ribbon_bigBtnAsSmall(cmd->cmd_id, showIcon, showLabel,
                                             ribbon_cmdEnabled(cmd), drop, col.width);
                        if (drop) runFlyout(cmd);
                    } else {
                        ribbon_smBtn(cmd->cmd_id, cmd->icon, cmd->label, ribbon_cmdEnabled(cmd),
                                     col.width);
                    }
                }
                sx += col.width;
            }
        }

        // --- Group separator + label ---
        {
            ImVec2 cwinPos = ImGui::GetWindowPos();
            // Center the divider in the inter-group GAP, not on the left pill's right edge. The
            // group pill spans [curX + gpad, curX + grpW - gpad] (gpad = kGroupPadX*s*0.5), so the
            // gap between this pill and the next runs [curX+grpW-gpad, curX+grpW+gpad] — its midpoint
            // is curX+grpW. The old sepX (grpW - 0.5*pad) sat exactly on this pill's right edge,
            // jamming the divider against the left group and leaving a lopsided gap to the right one.
            float  sepX    = snap(cwinPos.x + curX + grpW);
            // Two-line highlight+hairline divider, retuned to the title-bar pinned spec (faint accent
            // highlight over a restrained border hairline) so the ribbon dividers read the same weight
            // as the title bar's — not the previous heavier 0.92 main line.
            float  dy0 = cwinPos.y + bodyH2 * 0.14f;
            float  dy1 = cwinPos.y + bodyH2 * 0.86f;
            ImU32  dHi  = (ui::Palette::toU32(ui::palette.accent) & 0x00FFFFFFu) | 0x22000000u; // faint accent highlight
            ImU32  dCol = (ui::Palette::toU32(ui::palette.border) & 0x00FFFFFFu) | 0x66000000u; // main hairline
            dl->AddLine(ImVec2(sepX - 0.5f * s, dy0), ImVec2(sepX - 0.5f * s, dy1), dHi,  ui::metrics::stroke(1.0f));
            dl->AddLine(ImVec2(sepX + 0.5f * s, dy0), ImVec2(sepX + 0.5f * s, dy1), dCol, ui::metrics::stroke(1.0f));

            // Defect 2: centered caption. grpW already includes a caption-width floor, so labels
            // such as "Dimension" never truncate at COL_BIG/COL_SMALL. The clip rect is a safety
            // net; if it ever clips (only possible in a degraded edge case) a tooltip is offered.
            if (grp.collapse != COL_LABEL) {
                ImVec2 gts = ImGui::CalcTextSize(grp.label);
                float  capL = cwinPos.x + curX + kGroupPadX * s * 0.5f;
                float  capR = cwinPos.x + curX + grpW - kGroupPadX * s * 0.5f;
                float  glx = snap(capL + ((capR - capL) - gts.x) * 0.5f);
                if (glx < capL) glx = snap(capL);
                // Caption strip sits under the content zone: contentY + contentH … bodyH2 - padY.
                // Vertically center the label in that strip so it never collides with button tiles.
                // Screen coords: cwinPos.y + relative strip (draw list is the parent ribbon window).
                float  capTop = cwinPos.y + contentY + contentH;
                float  capBot = cwinPos.y + bodyH2 - padY;
                float  gly = snap(capTop + ((capBot - capTop) - gts.y) * 0.5f);
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

    dl->PopClipRect();   // group-chrome clip pushed right after BeginChild

    // Evaluate-tab inspector dock: publish the dead space to the RIGHT of the last group so the
    // Zebra / Symmetry windows pin INSIDE the ribbon strip (Hunter red-pen: dock, don't float).
    // curX is the last group's right edge (child-local); the child spans vp->Size.x - 8*s.
    if (!mobile && std::strcmp(actTabName, "Evaluate") == 0) {
        const ImVec2 cwp = ImGui::GetWindowPos();
        const float gpad = kGroupPadX * s * 0.5f;
        const float dockL = cwp.x + curX + gpad + (anyHidden ? 26.0f * s : 0.0f);
        const float dockT = cwp.y + ribbon_groupPadY();
        const float dockR = cwp.x + (vp->Size.x - 8.0f * s) - 6.0f * s;
        const float dockB = cwp.y + bodyH2 - ribbon_groupPadY();
        ui::setEvalRibbonDock(dockL, dockT, dockR, dockB);
    }

    // --- C25-W3G: overflow chevron ---
    if (anyHidden) {
        if (mobile) {
            ImGui::SetCursorPos(ImVec2(ui::metrics::px(2.0f), 0.0f));
            if (ui::kit::secondaryButton("Commands", true, nullptr,
                                         ImVec2(bodyW - ui::metrics::px(4.0f),
                                                bodyH2 - ui::metrics::px(4.0f))))
                ImGui::OpenPopup("##ribbon_overflow");
            ImGui::SetNextWindowSizeConstraints(ImVec2(0, 0),
                ImVec2(vp->Size.x - ui::metrics::px(12.0f),
                       std::max(ui::metrics::px(44.0f), vp->Size.y - totalH - titleBarH)));
        } else {
            float bh = ribbon_contentH();
            if (ribbon_overflowChevron(curX + 2.0f * s, ribbon_groupPadY(), bh))
                ImGui::OpenPopup("##ribbon_overflow");
        }

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

                // One row renderer for big + small overflowed commands (the two loops were
                // byte-identical apart from the button id, which PushID(cmd_id) scopes anyway).
                // `indent` steps in a folded split-button variant so it reads as belonging to the
                // parent above it — without listing subs here, a group that overflows would make
                // every folded variant unreachable.
                auto overflowRow = [&](const ui::RibbonCmd* cmd, float indent) {
                    bool enabled = ribbon_cmdEnabled(cmd);
                    ImVec2 cp0 = ImGui::GetCursorScreenPos();
                    float rowH   = snap(ui::metrics::px(mobile ? 44.0f : 28.0f));
                    float iconSz = snap(ui::metrics::px(20.0f));
                    float pad    = snap(ui::metrics::U(2));
                    float gapX   = snap(ui::metrics::px(6.0f));
                    float padX   = pad + indent;
                    float popW   = padX + iconSz + gapX +
                                   ImGui::CalcTextSize(cmd->label).x + pad;
                    if (mobile) popW = std::min(popW, vp->Size.x - ui::metrics::px(36.0f));

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
                        ribbon_icon(odl, cmd->icon, ix + iconSz * 0.5f, iy + iconSz * 0.5f,
                                    iconSz, enabled);
                    }
                    {
                        float tx = snap(cp0.x + padX + iconSz + gapX);
                        float ty = snap(cp0.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f);
                        ImU32 tCol = enabled ? ui::Palette::toU32(ui::palette.text)
                                             : ui::Palette::toU32(ui::palette.textDisabled);
                        if (mobile) {
                            const std::string label = ui::kit::elide(cmd->label,
                                std::max(1.0f, popW - padX - iconSz - gapX - pad));
                            odl->AddText(nullptr, 0.0f, ImVec2(tx, ty), tCol, label.c_str());
                        } else {
                            odl->AddText(nullptr, 0.0f, ImVec2(tx, ty), tCol, cmd->label);
                        }
                    }
                    if (rowClicked && enabled) {
                        ribbon_fire(cmd->cmd_id);
                        ImGui::CloseCurrentPopup();
                    }
                    ImGui::PopID();
                };
                for (const auto* cmd : grp.bigCmds) {
                    overflowRow(cmd, 0.0f);
                    for (const auto* sc : grp.subCmds)
                        if (std::strcmp(sc->sub_of, cmd->cmd_id) == 0)
                            overflowRow(sc, ui::metrics::U(3));
                }
                for (const auto* cmd : grp.smCmds) {
                    overflowRow(cmd, 0.0f);
                    if (mobile)
                        for (const auto* sc : grp.subCmds)
                            if (std::strcmp(sc->sub_of, cmd->cmd_id) == 0)
                                overflowRow(sc, ui::metrics::U(3));
                }
                ImGui::Separator();
            }
            ImGui::EndPopup();
        }
    }
    (void)chevronW;

    // Sketch Finish/Cancel now render as a highlighted "Finish Sketch" big button (with a Cancel
    // dropdown) in the New-Sketch slot of the Sketch group — see the bigCmds render loop above. The
    // old right-aligned commit-chip cluster was removed in favor of that native ribbon button.

    ImGui::EndChild();
    ImGui::End();

    return totalH;
}

}  // namespace ui
