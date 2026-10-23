// Chudware overlay layout + the pinned FeatureManager glass column — see layout.h.
//
// Overlay redesign: buildDefaultLayout() keeps one uninterrupted central Viewport / ECAD node so
// the 3D surface spans the full dockspace. Manufacture previews render natively in that viewport,
// while drawLeftPane() hosts Explorer, Job Manager, NC Output, Devices, Run and Attention as
// integrated views in the pinned full-height glass column over its left edge. The right-edge resize
// boundary (columnResizeEdge, shared with the Copilot column) and collapse affordance remain;
// command dialogs float above the central node.
#include "layout.h"

#include "imgui.h"
#include "imgui_internal.h"

#include "../core/app_state.h"
#include "../ui_theme.h"   // ui::uiScale, ui::palette, liquid glass v2

#include "feature_tree.h"       // drawFeatureTreeBody
#include "file_explorer.h"      // Wave 3: drawFileExplorerBody (Explorer tab)
#include "property_manager.h"   // PropertyManager::drawBody
#include "config_panel.h"       // ConfigPanel::drawEmbedded (live Configurations tab)
#include "plm/graph_tab.h"      // plmGraphFocus / plmGraphAvailable (Graph tab jump)
#include "plm/plm_hub.h"         // g_plmHub, PlmPage
#include "sim_manager.h"        // SimManager::drawTreeBody (live Simulation tab)
#include "cam/cam_panel.h"      // drawCamTreeBody (the Manufacture workspace's left tree)
#include "manufacture/manufacture_panel.h"  // Manufacture Devices/Run/Attention rail bodies + critical count
#include "ecad/ecad_panel.h"    // drawEcadManagerBody (the Electronics workspace's left tree)
#include "doc_tabs.h"           // docTabStripHeight (the column never overlaps the doc-tab band)
#include "icons.h"              // getIcon / IconUV
#include "anim.h"               // ui::anim::toward — resize-boundary hover fade
#include "metrics.h"            // logical dp scaling
#include "panel_collapse.h"     // ui::PanelCollapseAnim — shared vertical-collapse animation
#include "widgets/kit.h"         // ui::kit::elide

#include <filesystem>
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#ifdef __EMSCRIPTEN__
#include <emscripten.h>
#endif

// Overlay layout: the dockspace hosts ONLY the central document windows, tabbed in one node, so
// the viewport runs edge to edge between the ribbon and the status bar. The FeatureManager and
// Copilot are pinned floating glass columns drawn OVER the viewport (drawLeftPane below /
// drawAIPanel in ui/copilot/panel.cpp) — no left/right dock splits; each column's inner edge
// carries the shared columnResizeEdge boundary instead of a splitter bar.
// "Reset layout" rebuilds exactly this (plus resetOverlayColumns for the columns' geometry).
void buildDefaultLayout(ImGuiID root, const ImVec2& size) {
    ImGui::DockBuilderRemoveNode(root);
    ImGui::DockBuilderAddNode(root, ImGuiDockNodeFlags_DockSpace);
    ImGui::DockBuilderSetNodeSize(root, size);

    // ECAD shares the single, uninterrupted central node with the Viewport (tabbed). Dock ECAD
    // FIRST so the Viewport is the default-active tab (3D is the primary view); the ECAD tab pops
    // forward on its own (SetNextWindowFocus) when an ECAD document is loaded. Manufacture keeps
    // this viewport visible and renders toolpaths through its native overlay path; its Job Manager
    // and NC Output views live in the pinned left column rather than a separate dock node.
    ImGui::DockBuilderDockWindow("ECAD", root);
    ImGui::DockBuilderDockWindow("Viewport", root);
    ImGui::DockBuilderDockWindow("Viewport###Viewport", root);

    ImGui::DockBuilderFinish(root);
}

namespace ui {

// ---- overlay band + pinned-column reset plumbing (see layout.h) --------------------------------
static OverlayBand g_overlayBand;
static bool g_overlayColReset[2] = {false, false};

void setOverlayBand(float minX, float minY, float maxX, float maxY) {
    g_overlayBand.minX = minX;
    g_overlayBand.minY = minY;
    g_overlayBand.maxX = maxX;
    g_overlayBand.maxY = maxY;
    g_overlayBand.valid = (maxX > minX && maxY > minY);
}
const OverlayBand& overlayBand() { return g_overlayBand; }


void setNextCommandDialogPlacement(const char* windowName, ImVec2 preferredSizePx,
                                   ImVec2 minimumSizePx) {
    const ImGuiViewport* viewport = ImGui::GetMainViewport();
    ImVec2 lo = viewport->WorkPos;
    ImVec2 hi(lo.x + viewport->WorkSize.x, lo.y + viewport->WorkSize.y);
    const OverlayBand& band = overlayBand();
    if (band.valid) {
        lo.x = std::max(lo.x, band.minX);
        lo.y = std::max(lo.y, band.minY);
        hi.x = std::min(hi.x, band.maxX);
        hi.y = std::min(hi.y, band.maxY);
    }
    if (hi.x <= lo.x || hi.y <= lo.y) return;
    const float marginX = std::min(metrics::px(12), (hi.x - lo.x) * 0.05f);
    const float marginY = std::min(metrics::px(12), (hi.y - lo.y) * 0.05f);
    lo.x += marginX; lo.y += marginY;
    hi.x -= marginX; hi.y -= marginY;
    const ImVec2 maximum(hi.x - lo.x, hi.y - lo.y);
    const ImVec2 minimum(std::min(minimumSizePx.x, maximum.x),
                         std::min(minimumSizePx.y, maximum.y));
    const ImVec2 preferred(std::clamp(preferredSizePx.x, minimum.x, maximum.x),
                           std::clamp(preferredSizePx.y, minimum.y, maximum.y));
    ImGui::SetNextWindowSizeConstraints(minimum, maximum);
    ImGui::SetNextWindowSize(preferred, ImGuiCond_FirstUseEver);

    // Columns are hints, not hard boundaries: on narrow viewports a useful dialog may overlap
    // them. Never reserve space for a hidden Copilot or use its nominal/default width.
    const float leftEdge = overlayColumnEdge(0);
    const float rightEdge = overlayColumnEdge(1);
    const float freeLeft = leftEdge >= lo.x ? std::min(hi.x, leftEdge + marginX) : lo.x;
    const float freeRight = rightEdge >= lo.x ? std::min(hi.x, rightEdge - marginX) : hi.x;
    const float anchorRight = freeRight - freeLeft >= preferred.x ? freeRight : hi.x;
    ImVec2 position(std::clamp(anchorRight - preferred.x, lo.x, hi.x - preferred.x), lo.y);
    ImVec2 size = preferred;
    bool remembered = false;
    if (const ImGuiWindow* window = ImGui::FindWindowByName(windowName)) {
        position = window->Pos;
        size = window->SizeFull;
        remembered = true;
    } else if (const ImGuiWindowSettings* settings =
                   ImGui::FindWindowSettingsByID(ImHashStr(windowName))) {
        const ImVec2 viewportPos = settings->ViewportId
            ? ImVec2((float)settings->ViewportPos.x, (float)settings->ViewportPos.y)
            : viewport->Pos;
        position = ImVec2(settings->Pos.x + viewportPos.x, settings->Pos.y + viewportPos.y);
        size = ImVec2((float)settings->Size.x, (float)settings->Size.y);
        remembered = true;
    }
    const ImVec2 boundedSize(std::clamp(size.x, minimum.x, maximum.x),
                             std::clamp(size.y, minimum.y, maximum.y));
    const ImVec2 boundedPos(std::clamp(position.x, lo.x, hi.x - boundedSize.x),
                            std::clamp(position.y, lo.y, hi.y - boundedSize.y));
    if (remembered && (boundedSize.x != size.x || boundedSize.y != size.y))
        ImGui::SetNextWindowSize(boundedSize);
    if (remembered && (boundedPos.x != position.x || boundedPos.y != position.y))
        ImGui::SetNextWindowPos(boundedPos);
    else
        ImGui::SetNextWindowPos(boundedPos, ImGuiCond_FirstUseEver);
}

void resetOverlayColumns() { g_overlayColReset[0] = g_overlayColReset[1] = true; }

// ---- pinned-column occlusion edges (see layout.h) ----------------------------------------------
// Frame-stamped so a column that stops drawing (hidden / collapsed) stops occluding one frame later.
static float g_overlayColEdge[2]      = {-1.0f, -1.0f};
static int   g_overlayColEdgeFrame[2] = {-1, -1};

void publishOverlayColumnEdge(int slot, float edgeX) {
    if (slot < 0 || slot > 1) return;
    g_overlayColEdge[slot] = edgeX;
    g_overlayColEdgeFrame[slot] = ImGui::GetFrameCount();
}
float overlayColumnEdge(int slot) {
    if (slot < 0 || slot > 1) return -1.0f;
    if (g_overlayColEdgeFrame[slot] < ImGui::GetFrameCount() - 1) return -1.0f;
    return g_overlayColEdge[slot];
}

// ---- Evaluate-tab ribbon inspector dock (see layout.h) -----------------------------------------
// Frame-stamped so the dock invalidates one frame after the ribbon stops publishing it (tab switch).
static EvalRibbonDock g_evalDock;
static int            g_evalDockFrame = -1;

void setEvalRibbonDock(float minX, float minY, float maxX, float maxY) {
    g_evalDock.minX = minX; g_evalDock.minY = minY;
    g_evalDock.maxX = maxX; g_evalDock.maxY = maxY;
    g_evalDock.valid = (maxX > minX && maxY > minY);
    g_evalDockFrame = ImGui::GetFrameCount();
}
EvalRibbonDock evalRibbonDock() {
    if (g_evalDockFrame < ImGui::GetFrameCount() - 1) { EvalRibbonDock d; return d; }
    return g_evalDock;
}

// ---- pinned-column rendered height (see layout.h) -----------------------------------------------
// Mirrors publishOverlayColumnEdge: each column publishes its actual rendered height in pixels
// once per frame so the orientation triad can decide whether the column reaches down to the triad
// row and should push it right, or is short/collapsed so the triad stays at the far-left anchor.
static float g_overlayColHeight[2]      = {-1.0f, -1.0f};
static int   g_overlayColHeightFrame[2] = {-1, -1};

void publishOverlayColumnHeight(int slot, float heightPx) {
    if (slot < 0 || slot > 1) return;
    g_overlayColHeight[slot] = heightPx;
    g_overlayColHeightFrame[slot] = ImGui::GetFrameCount();
}
float overlayColumnHeight(int slot) {
    if (slot < 0 || slot > 1) return -1.0f;
    if (g_overlayColHeightFrame[slot] < ImGui::GetFrameCount() - 1) return -1.0f;
    return g_overlayColHeight[slot];
}

// ---- doc-tab band bottom edge (see layout.h) -----------------------------------------------------
// Frame-stamped like the column edges: a strip that stops drawing stops insetting one frame later.
static float g_docTabBandBottom      = -1.0f;
static int   g_docTabBandBottomFrame = -1;

void publishDocTabBandBottom(float y) {
    g_docTabBandBottom = y;
    g_docTabBandBottomFrame = ImGui::GetFrameCount();
}
float docTabBandBottom() {
    if (g_docTabBandBottomFrame < ImGui::GetFrameCount() - 1) return -1.0f;
    return g_docTabBandBottom;
}

bool takeOverlayColumnReset(int slot) {
    if (slot < 0 || slot > 1 || !g_overlayColReset[slot]) return false;
    g_overlayColReset[slot] = false;
    return true;
}

// ---- pinned-column resize boundary (see layout.h) ----------------------------------------------
// Palette role `a` (alpha * alphaA) blended toward role `b` (alpha * alphaB) by t, as a draw color.
static ImU32 mixRoles(const float a[4], float alphaA, const float b[4], float alphaB, float t) {
    float c[4];
    for (int i = 0; i < 3; ++i) c[i] = a[i] + (b[i] - a[i]) * t;
    c[3] = a[3] * alphaA + (b[3] * alphaB - a[3] * alphaA) * t;
    return ui::Palette::toU32(c);
}

ColumnResizeResult columnResizeEdge(const char* strId, bool rightEdge, float currentWidth,
                                    bool interactive) {
    ColumnResizeResult r;
    r.width = currentWidth;
    if (!interactive) return r;

    ImGuiContext& g = *ImGui::GetCurrentContext();
    ImGuiWindow* win = ImGui::GetCurrentWindow();
    const ImRect winR = win->Rect();
    const ImGuiID id = win->GetID(strId);
    const float edgeX = rightEdge ? winR.Max.x : winR.Min.x;
    // Two grid units inside the edge: the strip lives in the window-padding gutter, so it is
    // comfortably grabbable without reaching into the column's rows.
    const float hitW = metrics::U(2.0f);
    const ImRect hit(rightEdge ? edgeX - hitW : edgeX, winR.Min.y,
                     rightEdge ? edgeX : edgeX + hitW, winR.Max.y);

    // Same mechanism as ImGui's own window resize borders: KeepAliveID + ButtonBehavior, no
    // ItemAdd — the strip is edge chrome, so it neither grows the window's content extent nor
    // registers as overflowing content. AllowOverlap lets any widget submitted later over the
    // strip win hover (child windows win by being separate windows). The window's inner clip stops
    // half a padding short of the edge, so hit-test under the full window rect.
    ImGui::KeepAliveID(id);
    ImGui::PushClipRect(winR.Min, winR.Max, false);
    bool hovered = false, held = false;
    ImGui::ButtonBehavior(hit, id, &hovered, &held,
                          ImGuiButtonFlags_AllowOverlap | ImGuiButtonFlags_NoNavFocus);
    ImGui::PopClipRect();

    // Absolute drag (start width + distance from the press point), so clamping or the column
    // re-anchoring under the cursor can never accumulate drift. One drag at a time app-wide.
    static ImGuiID s_dragId = 0;
    static float s_dragStartW = 0.f;
    if (held && s_dragId != id) { s_dragId = id; s_dragStartW = currentWidth; }
    if (!held && s_dragId == id) s_dragId = 0;
    if (hovered && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) {
        r.resetRequested = true;
        // The second press re-armed a drag; end it so its start width can't override the reset.
        if (held) ImGui::ClearActiveID();
        held = false;
        s_dragId = 0;
    }
    if (held) {
        const float dx = g.IO.MousePos.x - g.IO.MouseClickedPos[ImGuiMouseButton_Left].x;
        r.width = s_dragStartW + (rightEdge ? dx : -dx);
        r.active = true;
    }
    if (hovered || held) ImGui::SetMouseCursor(ImGuiMouseCursor_ResizeEW);
    if (hovered && !held && g.HoveredIdTimer >= ImGui::GetStyle().HoverDelayNormal)
        kit::tooltip("Drag to resize · Double-click to reset width");

    // ---- paint: faint hairline + grip at rest; brighter accent line + soft glow on hover/drag ----
    const float t = anim::toward(id, (hovered || held) ? 1.0f : 0.0f, anim::kFast);
    const auto& pal = ui::palette;
    ImDrawList* dl = win->DrawList;
    const float glowW = metrics::U(2.0f);
    dl->PushClipRect(ImVec2(winR.Min.x - glowW, winR.Min.y), ImVec2(winR.Max.x + glowW, winR.Max.y),
                     false);

    // The line runs between the bubble's corner arcs so it never overshoots the rounded column.
    float y0 = winR.Min.y + win->WindowRounding;
    float y1 = winR.Max.y - win->WindowRounding;
    if (y1 - y0 < metrics::U(4.0f)) {               // collapsed header strip: use the whole edge
        y0 = winR.Min.y + metrics::U(1.0f);
        y1 = winR.Max.y - metrics::U(1.0f);
    }
    const float midY = (winR.Min.y + winR.Max.y) * 0.5f;

    if (t > 0.001f && y1 > y0) {
        // Glow: accent at low alpha peaking at the edge's middle, fading to nothing toward the
        // top/bottom and away from the edge on both sides (bilinear quads).
        float glowC[4];
        ui::Palette::withAlpha(pal.accentSoft, glowC, t);
        const ImU32 peak = ui::Palette::toU32(glowC);
        const ImU32 none = peak & ~IM_COL32_A_MASK;
        const float gm = (y0 + y1) * 0.5f;
        dl->AddRectFilledMultiColor(ImVec2(edgeX - glowW, y0), ImVec2(edgeX, gm), none, none, peak, none);
        dl->AddRectFilledMultiColor(ImVec2(edgeX - glowW, gm), ImVec2(edgeX, y1), none, peak, none, none);
        dl->AddRectFilledMultiColor(ImVec2(edgeX, y0), ImVec2(edgeX + glowW, gm), none, none, none, peak);
        dl->AddRectFilledMultiColor(ImVec2(edgeX, gm), ImVec2(edgeX + glowW, y1), peak, none, none, none);
    }

    if (y1 > y0) {
        // Boundary hairline with short fades at both ends.
        const float lw = metrics::stroke(1.0f + t);
        const float lx0 = edgeX - lw * 0.5f, lx1 = edgeX + lw * 0.5f;
        const ImU32 lineCol = mixRoles(pal.textDim, 0.16f, pal.accent, 0.85f, t);
        const ImU32 lineNone = lineCol & ~IM_COL32_A_MASK;
        const float fadeH = std::min(metrics::U(6.0f), (y1 - y0) * 0.25f);
        dl->AddRectFilledMultiColor(ImVec2(lx0, y0), ImVec2(lx1, y0 + fadeH),
                                    lineNone, lineNone, lineCol, lineCol);
        dl->AddRectFilled(ImVec2(lx0, y0 + fadeH), ImVec2(lx1, y1 - fadeH), lineCol);
        dl->AddRectFilledMultiColor(ImVec2(lx0, y1 - fadeH), ImVec2(lx1, y1),
                                    lineCol, lineCol, lineNone, lineNone);
    }

    // Grip: a short rounded pill centred on the edge, always faintly visible, on a surface-colored
    // halo so it reads over any viewport content; grows a touch and takes the accent on hover.
    {
        const float gw = metrics::px(4.0f);
        const float gh = std::min(metrics::U(7.0f), winR.GetHeight() * 0.5f) * (1.0f + 0.15f * t);
        const ImVec2 g0(edgeX - gw * 0.5f, midY - gh * 0.5f);
        const ImVec2 g1(edgeX + gw * 0.5f, midY + gh * 0.5f);
        const float halo = metrics::stroke(1.0f);
        float haloC[4];
        ui::Palette::withAlpha(pal.glassBg, haloC, 0.55f + 0.35f * t);
        dl->AddRectFilled(ImVec2(g0.x - halo, g0.y - halo), ImVec2(g1.x + halo, g1.y + halo),
                          ui::Palette::toU32(haloC), gw * 0.5f + halo);
        dl->AddRectFilled(g0, g1, mixRoles(pal.textDim, 0.55f, pal.accent, 1.0f, t), gw * 0.5f);
    }
    dl->PopClipRect();
    return r;
}

static ImVec4 roleColor(const float role[4]) {
    return ImVec4(role[0], role[1], role[2], role[3]);
}

static bool drawManagerTabButton(const char* id,
                                 const char* icon,
                                 const char* fallback,
                                 const char* tip,
                                 bool selected,
                                 bool pmAttention,
                                 const ui::AdaptiveSurface& surf,
                                 ImU32 halo,
                                 float btn,
                                 float glyph,
                                 bool useTreeGlyph) {
    const auto& pal = ui::palette;
    const float S = ui::uiScale;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImVec2 p1(p0.x + btn, p0.y + btn);

    ImGui::PushID(id);
    bool clicked = ImGui::InvisibleButton("##lp_tab", ImVec2(btn, btn));
    bool hovered = ImGui::IsItemHovered();

    // Persistent frosted pill on every rail tab, PLUS the Layers-style colored gradient wash the
    // rows/ribbon buttons use — stronger when selected or hovered. Selection is the glow border
    // (coloredRowWash selected=true) — no left vertical accent bar (app-wide rule).
    const float wash = selected ? 0.40f : (hovered ? 0.32f : 0.20f);
    ui::glassRowBackground(dl, p0, p1, 7.0f, false, /*selected*/false, wash);
    const float tintStrength = selected ? 0.30f : (hovered ? 0.20f : 0.12f);
    ui::coloredRowWash(dl, p0, p1, 7.0f, pal.accent, tintStrength, selected);

    // Liquid glass v2: rail-tab glyph tint tracks the pane's adaptive surface (max contrast
    // over the frosted backdrop) instead of the raw palette; PM-attention keeps the semantic
    // accent color. drawIconGlass adds the opposite-tone contrast halo (host contract §H4).
    ImVec4 tint = selected ? surf.textPrimary : surf.textSecondary;
    if (pmAttention && !selected) tint = roleColor(pal.accent);
    IconUV ic = getIcon(icon, 20);
    if (useTreeGlyph) {
        // Feature Manager tab: draw the same purpose-built feature-tree glyph the pane-header chip
        // uses (root node + two branch children) instead of the atlas "history" clock icon.
        ui::drawFeatureTreeGlyph(dl, ImVec2(p0.x + (btn - glyph) * 0.5f, p0.y + (btn - glyph) * 0.5f),
                                 glyph, ImGui::ColorConvertFloat4ToU32(tint));
    } else if (ic.valid) {
        drawIconGlass(dl, icon, ImVec2(p0.x + btn * 0.5f, p0.y + btn * 0.5f), glyph,
                     ImGui::ColorConvertFloat4ToU32(tint), halo);
    } else {
        ImVec2 ts = ImGui::CalcTextSize(fallback);
        dl->AddText(ImVec2(p0.x + (btn - ts.x) * 0.5f, p0.y + (btn - ts.y) * 0.5f),
                    ImGui::ColorConvertFloat4ToU32(tint), fallback);
    }

    if (pmAttention) {
        float r = 3.0f * S;
        dl->AddCircleFilled(ImVec2(p1.x - 6.0f * S, p0.y + 6.0f * S), r,
                            ui::Palette::toU32(pal.statusWarn));
    }

    if (hovered) ImGui::SetTooltip("%s", tip);
    ImGui::PopID();
    return clicked;
}

// ---- Configurations tab body (no Begin/End) -------------------------------
// The part-document engine has no named-configuration schema yet (config_* RPCs exist for the
// dockable ConfigPanel, but parts ship a single implicit configuration). Render the implicit
// "Default [Active]" entry plus a short note pointing at the future design-tables work, matching
// the SolidWorks ConfigurationManager's at-rest appearance for a single-config part.
void drawConfigurationsBody(AppState& st) {
    const auto& pal = ui::palette;
    const ImVec4 accentCol(pal.accent[0], pal.accent[1], pal.accent[2], 1);
    // Liquid glass v2: dim text over this over-viewport pane tracks the adaptive surface
    // (host contract §H4); the surface is a cached idempotent-per-frame lookup (A2) of the SAME
    // "host_left_glass" id drawLeftPane already painted this frame — no signature change needed
    // since this function is a fixed layout.h entry point.
    const ImVec2 cfgWp = ImGui::GetWindowPos(), cfgWs = ImGui::GetWindowSize();
    const ImRect cfgRect(cfgWp, ImVec2(cfgWp.x + cfgWs.x, cfgWp.y + cfgWs.y));
    const ui::AdaptiveSurface& cfgSurf =
        ui::adaptivePanel(ui::panelId("host_left_glass"), cfgRect, ui::GlassGroup::FeatureManager);
    const ImVec4& dimCol = cfgSurf.textSecondary;

    ImGui::PushFont(st.fonts.medium);
    ImGui::TextColored(accentCol, "Configurations");
    ImGui::PopFont();
    ImGui::Spacing();

    // Implicit single configuration: bold accent dot + "[Active]" tag (SolidWorks bolds the active).
    ImGui::PushStyleColor(ImGuiCol_Text, accentCol);
    ImGui::TextUnformatted("*  Default");
    ImGui::PopStyleColor();
    ImGui::SameLine();
    ImGui::TextColored(dimCol, "[Active]");

    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    ImGui::PushTextWrapPos(ImGui::GetContentRegionAvail().x);
    ImGui::TextColored(dimCol,
        "Use Design Table for editable configuration values. The active configuration is "
        "mirrored here so the left manager stays the single place to inspect model state.");
    ImGui::PopTextWrapPos();
}

// ---- the SolidWorks-faithful tabbed left pane -----------------------------

void drawLeftPane(AppState& st,
                  const std::function<void(const std::string& featureId)>& editCallback) {
    PropertyManager* pm = st.propertyManager;

    // [sim-gui rework] The simulation setup tree exists ONLY in the Simulate
    // workspace. SimManager::handleCommand parks st.leftPaneTab on LP_Simulation; if the user
    // then switches to Model/Drawing (or any non-Simulate workspace) clamp it back to the
    // FeatureManager so no Simulation pane state leaks into the modeling views (the inverse of
    // the Simulate-side takeover below, which ignores the tab strip entirely).
    if (st.activeWorkspace != AppState::Workspace::Simulate &&
        st.leftPaneTab == kLeftPaneSimulation)
        st.leftPaneTab = kLeftPaneFeatureManager;

    // Vertical-collapse state: the column rolls UP to its header (tab row + collapse
    // chevron) instead of shrinking to a pill. Persisted in config.json (st.settings.fmCollapsed)
    // so a collapsed column stays collapsed across restarts; seeded once from the loaded setting
    // (loadSettings runs before the first frame, so this static local's initializer reads it).
    static bool s_collapsed = st.settings.fmCollapsed;
    static ui::PanelCollapseAnim s_anim;

    // Auto-switch policy: the PropertyManager form is hosted INSIDE the FeatureManager tab now (the
    // separate "Property Manager" tab was a duplicate and was removed). When a command goes active
    // (inactive -> active) jump to the FeatureManager tab and pop the collapsed pill open so the
    // form is visible — an invisible active command is worse than a yank.
    static bool s_pmWasActive = false;
    const bool pmActive = pm && pm->active();
    if (pmActive && !s_pmWasActive) {
        st.leftPaneTab = kLeftPaneFeatureManager;
        s_collapsed = false;
    }
    s_pmWasActive = pmActive;

    const auto& pal = ui::palette;
    const float S = ui::uiScale;
#ifdef __EMSCRIPTEN__
    const bool mobile = ImGui::GetIO().DisplaySize.x <= 900.0f ||
                        EM_ASM_INT({ return window.matchMedia('(pointer: coarse)').matches; });
#else
    const bool mobile = false;
#endif

    // ---- pinned-column geometry --------------------------------------------------------------
    // Anchored to the overlay band app.cpp publishes each frame (ribbon bottom -> status-bar top),
    // inset by the doc-tab strip on top (the column must never overlap that band) and ~12dp
    // margins. Falls back to the main viewport's work rect for the pre-publish first frame.
    OverlayBand band = overlayBand();
    if (!band.valid) {
        const ImGuiViewport* mvp = ImGui::GetMainViewport();
        band.minX = mvp->WorkPos.x;
        band.minY = mvp->WorkPos.y;
        band.maxX = mvp->WorkPos.x + mvp->WorkSize.x;
        band.maxY = mvp->WorkPos.y + mvp->WorkSize.y;
    }
    const float margin = 12.0f * S;
    // Columns hug closer to the ribbon than the side/bottom margins (moved up per design). The
    // Copilot column + doc-tab strip use the same topMargin so all three share their top edge.
    const float topMargin = 4.0f * S;
    const float colX = band.minX + margin;
    // The doc-tab strip no longer sits ABOVE this column — it floats over the viewport, starting to
    // the RIGHT of the column's right edge (fix 2). So the FeatureManager column can start flush at
    // the top of the viewport band; the horizontal icon rail (fix 1) fills the space the doc tabs
    // used to occupy above it.
    const float colY = band.minY + topMargin;
    const float availableH = std::max(1.0f, band.maxY - margin - colY);
    const float colH = mobile ? availableH : std::max(120.0f * S, availableH);

    // ONE bubble radius shared by the invisible window (clip/interaction shape) and the drawn
    // glass, so the user's per-group rounding knob can't desync the corners: the resolved
    // FeatureManager rounding wins when set, else the 12dp bubble default.
    const ui::GlassStyleResolved& rsFm = ui::glassStyleResolved(ui::GlassGroup::FeatureManager);
    const float bubbleDp = (rsFm.rounding >= 0.f) ? rsFm.rounding : 12.0f;

    const bool wantReset = takeOverlayColumnReset(0);
    if (wantReset && s_collapsed) {
        s_collapsed = false;
        st.settings.fmCollapsed = false;
        saveSettings(st.settings);
    }
    // Workspace-rail columns (Simulate's Simulation Manager, Manufacture, Electronics) draw no
    // collapse chevron: their tree IS the workspace's primary surface. They therefore never inherit
    // the Model FeatureManager's persisted roll-up — a collapsed fm_collapsed used to pin the
    // Simulation Manager column to its 69px header strip, so the whole study list was clipped to a
    // blank rail with no control to expand it. s_collapsed (the Model choice) is kept untouched.
    const bool railWs = (st.activeWorkspace == AppState::Workspace::Simulate && st.simManager) ||
                        st.activeWorkspace == AppState::Workspace::Manufacture ||
                        st.activeWorkspace == AppState::Workspace::Electronics;
    // Screenshot hook: --ui-screenshot always resets the overlay columns for determinism
    // (io.IniFilename is nulled in shot mode, so DockBuilderGetNode is always null and the reset
    // branch above always fires) — the persisted collapse setting can't survive that on its own.
    // CHUDWARE_FM_COLLAPSED forces the collapsed capture the same way CHUDWARE_COPILOT_NARROW forces
    // a pane width; no effect outside a screenshot run.
    const bool forceCollapsed = std::getenv("CHUDWARE_FM_COLLAPSED") != nullptr;
    if (forceCollapsed) s_collapsed = true;
    const bool collapsedHere = s_collapsed && !railWs;
    // Snap (no roll animation) when the effective state flips because the workspace changed, and
    // every frame under the screenshot hook — headless captures must not depend on frame count.
    static int s_lastRailWs = -1;
    if (forceCollapsed || (s_lastRailWs >= 0 && s_lastRailWs != (int)railWs))
        s_anim.snap(collapsedHere, colH);
    s_lastRailWs = (int)railWs;

    // ---- pinned full-height-or-collapsed glass column -------------------------------------------
    // Position is pinned every frame; WIDTH is resized via the shared visible resize boundary on
    // the right edge (columnResizeEdge: hairline + grip + hover glow, EW cursor, double-click resets
    // to defW) and persisted in the imgui ini like any floating window. HEIGHT animates between the
    // full column height and the header-strip height (vertical collapse — rolls UP to the tab
    // row + chevron instead of shrinking to a pill; see ui::PanelCollapseAnim). Default/min widths
    // reuse the HiDPI-damped targets of the old dock split.
    const float panelSc = 1.0f + (S - 1.0f) * 0.55f;
    // Manufacture hosts a SIX-tab icon rail (Explorer / Job Manager / NC Output / Devices / Run /
    // Attention) across the column top — 6 * 30dp + 5 * 6dp = 210dp of rail before the header chip.
    // It gets a wider default AND a hard minimum so the rail can never be squeezed narrower than its
    // own buttons; unlike Electronics the size is NOT forced (FirstUseEver below), so the user keeps
    // the ability to widen it and the min-width constraint is what guarantees the fit.
    const bool mfgWs  = (st.activeWorkspace == AppState::Workspace::Manufacture);
    const float availableW = std::max(1.0f, band.maxX - band.minX - margin * 2.0f);
    const float minW = mobile ? std::min(availableW, 250.0f * panelSc)
                             : (mfgWs ? 360.0f : 250.0f) * panelSc;
    // Electronics (PCB/Schematic Manager) runs a little wider than the Model FeatureManager — its
    // layer/net rows carry a swatch + label + eye toggle, so the extra width keeps them uncramped.
    // Forced (Always) on that workspace so it reliably applies to the shared,
    // otherwise user-resized column; the doc-tab strip re-anchors to this edge each frame, so a wider
    // Electronics column just pushes its own tabs right — Model stays at its own persisted width.
    const bool ecadWs = (st.activeWorkspace == AppState::Workspace::Electronics);
    // Simulate (when its Simulation Manager body draws — the early-return branch below) gets its
    // OWN column window, so its width is remembered separately from the shared FeatureManager
    // column: a wider default (metrics::simManagerPaneW, so sim forms stop wrapping at ~280 px of
    // content) without forcing it (FirstUseEver), the user's resize persisted in the imgui ini under
    // its own name, and the Model/Drawing/Manufacture/Electronics width untouched. Window slot 0 =
    // shared FeatureManager column, 1 = Simulation Manager column. Position, collapse state and the
    // published overlay edge/height are identical for both; only one is submitted per frame.
    const bool simWs = (st.activeWorkspace == AppState::Workspace::Simulate && st.simManager);
    const int widthSlot = simWs ? 1 : 0;
    // "Reset layout" is a one-shot consumed by whichever column window draws this frame; latch it
    // per window so the one NOT on screen also snaps back to its default the next time it draws.
    static bool s_widthResetPending[2] = {false, false};
    if (wantReset) s_widthResetPending[0] = s_widthResetPending[1] = true;
    const bool resetWidth = s_widthResetPending[widthSlot];
    s_widthResetPending[widthSlot] = false;
    // Default capped at this fraction of the overlay band (still >= minW) so the Simulate column
    // never eats the viewport on a narrow window; maxW below stays the hard drag limit.
    constexpr float kSimPaneMaxBandFrac = 0.36f;
    const float defW = mobile ? std::min(availableW, 420.0f * panelSc)
                     : simWs  ? std::max(minW, std::min(ui::metrics::simManagerPaneW(),
                                                        (band.maxX - band.minX) * kSimPaneMaxBandFrac))
                              : (ecadWs ? 360.0f : mfgWs ? 380.0f : 320.0f) * panelSc;
    const float maxW = mobile ? availableW
                             : std::max(minW + 40.0f * S,
                                        std::min(560.0f * panelSc, (band.maxX - band.minX) * 0.42f));
    const float animatedH = s_anim.tick(collapsedHere, colH);
    const float winH = mobile ? std::min(animatedH, colH) : animatedH;
    ImGui::SetNextWindowPos(ImVec2(colX, colY), ImGuiCond_Always);
    ImGui::SetNextWindowSize(ImVec2(defW, winH),
                             (mobile || resetWidth || ecadWs) ? ImGuiCond_Always : ImGuiCond_FirstUseEver);
    ImGui::SetNextWindowSizeConstraints(ImVec2(minW, winH), ImVec2(maxW, winH));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, bubbleDp * S);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0.0f);
    // ImGui's own edge/corner resize is off (NoResize below): columnResizeEdge right after Begin
    // owns the width so this column and the Copilot column share one visible boundary treatment.
    // Input-regression fix: this column previously carried only NoBackground and was reliably
    // clickable; commit 043e752 added NoBringToFrontOnFocus (to keep it below transient command
    // dialogs) and the whole pane stopped responding to the mouse. FM (left edge) and the floating
    // dialogs (top-right) don't overlap, so dropping the flag restores click routing without
    // re-introducing any layering issue.
    // NoFocusOnAppearing: the two column windows (see widthSlot) swap on every Simulate <-> other
    // workspace switch; a reappearing column must not steal keyboard focus or jump above open
    // floating dialogs. Clicking inside still focuses it normally.
    const ImGuiWindowFlags lpFlags = ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoCollapse |
        ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoDocking | ImGuiWindowFlags_NoBackground |
        ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoFocusOnAppearing | ImGuiWindowFlags_NoResize;
    const bool lpOpen = ImGui::Begin(widthSlot == 1 ? "SimulationManager##LeftPane"
                                                    : "FeatureManager##LeftPane",
                                     nullptr, lpFlags);
    ImGui::PopStyleVar(2);
    if (!lpOpen) { ImGui::End(); return; }
    // Dark theme: one theme glass slab under the whole column (no-op in light, where the column
    // stays transparent over the viewport). Painted first so the resize hairline and every row
    // draw above it; its surface matches the theme-toned ink adaptivePanel hands the column.
    {
        const ImVec2 wp = ImGui::GetWindowPos(), ws = ImGui::GetWindowSize();
        ui::pinnedColumnSurface(ImGui::GetWindowDrawList(), ImRect(wp, ImVec2(wp.x + ws.x, wp.y + ws.y)),
                                ui::GlassGroup::FeatureManager);
    }

    // Right-edge resize boundary, submitted before any content so the column body (submitted later,
    // overlap-permissive strip) keeps every click. Electronics forces its width and mobile pins the
    // column, so neither offers the affordance. The new width lands next frame (SetWindowSize ->
    // SizeFull, clamped by the constraints above; ini marked dirty so it persists).
    {
        const float curW = ImGui::GetWindowWidth();
        const ColumnResizeResult rz = columnResizeEdge("##lp_resize", /*rightEdge=*/true, curW,
                                                       !mobile && !ecadWs);
        if (rz.resetRequested)
            ImGui::SetWindowSize(ImVec2(defW, ImGui::GetWindowHeight()));
        else if (rz.active)
            ImGui::SetWindowSize(ImVec2(std::clamp(rz.width, minW, maxW), ImGui::GetWindowHeight()));
    }

    // Liquid glass v2 (host contract §H4): the WHOLE-column glass slab is intentionally NOT painted
    // anymore — the column bounding box is hidden (fully transparent) so the 3D viewport shows
    // straight through, and only the individual rows/tabs/text/icons carry their own glass pills
    // (glassRowBackground / glassRowAtCursor / drawIconGlass). We still resolve the adaptive surface
    // (idempotent per frame — A2) so every embedded body (feature_tree.cpp / file_explorer.cpp / the
    // PM forms) fetches the SAME contrast-matched text color via panelId("host_left_glass").
    const ImVec2 lpWp = ImGui::GetWindowPos();
    const ImVec2 lpWs = ImGui::GetWindowSize();
    const ImRect lpPaneRect(lpWp, ImVec2(lpWp.x + lpWs.x, lpWp.y + lpWs.y));
    // Let the viewport HUD (triad / model-info pill) dodge the pinned column (layout.h).
    publishOverlayColumnEdge(0, lpPaneRect.Max.x);
    // Publish the column's occupied BOTTOM as an absolute screen Y so the triad can decide whether
    // the FM column reaches down to the triad row (content reaches down → anchor right of FM;
    // short/collapsed → anchor at far left, underneath the column). Default = the whole window
    // bottom; the Model pane overrides this below with the real content bottom (header + rows).
    publishOverlayColumnHeight(0, lpWp.y + lpWs.y);
    const ui::AdaptiveSurface& surf =
        ui::adaptivePanel(ui::panelId("host_left_glass"), lpPaneRect, ui::GlassGroup::FeatureManager);
    const bool lpLive = ui::viewportBackdrop().valid();
    const ImU32 lpHalo = lpLive
        ? ImGui::ColorConvertFloat4ToU32(ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0x50 / 255.f))
        : 0;
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);

    // [workspace-tabs seam] Simulate workspace: show ONLY the simulation setup tree.
    // Requirement: no feature/model tree, no Explorer/Configurations tabs.
    // Entire tab strip + normal body is suppressed; only SimManager::drawTreeBody runs,
    // styled to fill the full column width with the same glass row discipline.
    if (st.activeWorkspace == AppState::Workspace::Simulate && st.simManager) {
        const float S = ui::uiScale;
        // Explorer + Simulation Manager two-button rail — same chrome as the Electronics rail so
        // every workspace column shares one navigation pattern.
        static int s_simTab = 1;   // 0 = Explorer, 1 = Simulation Manager
        if (st.revealSimManager) { s_simTab = 1; st.revealSimManager = false; }
        const float ebtn = 30.0f * S, eglyph = 18.0f * S, egap = 6.0f * S, erowPad = 6.0f * S;
        const float erailRowH = erowPad + ebtn + erowPad;
        const ImVec2 ePaneOrigin  = ImGui::GetCursorScreenPos();
        const float  ePaneInnerW  = ImGui::GetContentRegionAvail().x;
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0.0f, 0.0f));
        ImGui::BeginChild("##sim_tabstrip", ImVec2(2.0f * ebtn + egap, erailRowH),
                          ImGuiChildFlags_None,
                          ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoBackground);
        ImGui::PopStyleVar();
        ImGui::SetCursorPosY(erowPad);
        ImGui::SetCursorPosX(0.0f);
        if (drawManagerTabButton("simE", "body_folder", "E", "Explorer", s_simTab == 0, false,
                                 surf, lpHalo, ebtn, eglyph, /*useTreeGlyph=*/false))
            s_simTab = 0;
        ImGui::SameLine(0, egap);
        if (drawManagerTabButton("simM", "history", "S", "Simulation Manager", s_simTab == 1, false,
                                 surf, lpHalo, ebtn, eglyph, /*useTreeGlyph=*/true))
            s_simTab = 1;
        ImGui::EndChild();
        {
            const float railW = 2.0f * ebtn + egap;
            const float hdrX = ePaneOrigin.x + railW + egap;
            const float hdrW = ePaneInnerW - railW - egap;
            const char* hdrTitle = s_simTab == 0 ? "Explorer" : "Simulation Manager";
            const char* hdrIcon  = s_simTab == 0 ? "body_folder" : "ai_panel";
            const ImVec2 saveLocal = ImGui::GetCursorPos();
            ImGui::SetCursorScreenPos(ImVec2(hdrX, ePaneOrigin.y + erowPad + 2.0f * S));
            ui::fmPaneHeaderChip(st, hdrTitle, hdrIcon, hdrX + hdrW * 0.5f);
            ImGui::SetCursorPos(saveLocal);
        }
        {
            ImDrawList* pdl = ImGui::GetWindowDrawList();
            const float sepY = ePaneOrigin.y + erailRowH - 0.5f;
            pdl->AddLine(ImVec2(ePaneOrigin.x, sepY), ImVec2(ePaneOrigin.x + ePaneInnerW, sepY),
                         ui::Palette::toU32(pal.glassBorder), 1.0f * S);
        }
        ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
        const float ebodyPad = 12.0f * S;
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                            ImVec2(ebodyPad, ImGui::GetStyle().WindowPadding.y));
        const float eBodyTopY = ImGui::GetCursorScreenPos().y;
        ImGui::BeginChild("##sim_body", ImVec2(0, -8.0f * S), ImGuiChildFlags_AlwaysUseWindowPadding);
        if (s_simTab == 0) {
            drawFileExplorerBody(st);
        } else {
            // Same body inset as the Explorer tab — the FM convention is the shared ebodyPad
            // child padding; the manager body adds no extra WindowPadding of its own.
            st.simManager->drawTreeBody(st);
        }
        const float eBodyContentH = ImGui::GetCursorPosY();
        ImGui::EndChild();
        ImGui::PopStyleVar();
        ImGui::PopStyleColor();   // ChildBg
        ImGui::PopStyleColor();   // ImGuiCol_Text (surf.textPrimary)
        // Republish this column's ACTUAL content bottom (absolute screen Y) so the orientation triad
        // tucks under the column when the tree/explorer is short, and only dodges right when the
        // content really reaches the triad's row — same behaviour as the Model FeatureManager column.
        publishOverlayColumnHeight(0, eBodyTopY + eBodyContentH);
        ImGui::End();
        return;
    }

    // [workspace-tabs seam] Manufacture keeps the native 3D viewport uninterrupted and integrates
    // its six supporting views here: Explorer, the CAM Job Manager tree (Job / Setups / Operations /
    // Tools / Models), NC Output, and the three operator views that used to float over the viewport
    // as a drawer — Devices, Run and Attention. No separate CAM window, detail dock or floating
    // drawer is used; the only Manufacture chrome outside this column is the Offline/Live mode strip
    // app.cpp reserves under the ribbon.
    if (st.activeWorkspace == AppState::Workspace::Manufacture) {
        const float S = ui::uiScale;
        // Explorer + Job Manager + NC Output + Devices + Run + Attention rail — same chrome as the
        // Electronics/Simulate rails.
        static int s_camTab = 1;   // 0=Explorer 1=Job Manager 2=NC Output 3=Devices 4=Run 5=Attention
        // Headless-shot parity: `CHUDWARE_SHOT=manufacture` is the operator-safety capture, so it
        // opens on Attention the way the retired floating drawer did. Every other shot (notably
        // `cam`, the marketing capture) keeps the Job Manager default. Resolved ONCE — the old
        // per-frame assignment also made the rail unclickable under any shot.
        static bool s_camTabShotInit = false;
        if (st.shot && !s_camTabShotInit) {
            s_camTabShotInit = true;
            const char* shotName = std::getenv("CHUDWARE_SHOT");
            if (shotName && std::strcmp(shotName, "manufacture") == 0) s_camTab = 5;
        }
        const float ebtn = 30.0f * S, eglyph = 18.0f * S, egap = 6.0f * S, erowPad = 6.0f * S;
        const float erailRowH = erowPad + ebtn + erowPad;
        const float erailW = 6.0f * ebtn + 5.0f * egap;
        const ImVec2 ePaneOrigin  = ImGui::GetCursorScreenPos();
        const float  ePaneInnerW  = ImGui::GetContentRegionAvail().x;
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0.0f, 0.0f));
        ImGui::BeginChild("##cam_tabstrip", ImVec2(erailW, erailRowH),
                          ImGuiChildFlags_None,
                          ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoBackground);
        ImGui::PopStyleVar();
        ImGui::SetCursorPosY(erowPad);
        ImGui::SetCursorPosX(0.0f);
        if (drawManagerTabButton("camE", "body_folder", "E", "Explorer", s_camTab == 0, false,
                                 surf, lpHalo, ebtn, eglyph, /*useTreeGlyph=*/false))
            s_camTab = 0;
        ImGui::SameLine(0, egap);
        if (drawManagerTabButton("camM", "history", "J", "Job Manager", s_camTab == 1, false,
                                 surf, lpHalo, ebtn, eglyph, /*useTreeGlyph=*/true))
            s_camTab = 1;
        ImGui::SameLine(0, egap);
        if (drawManagerTabButton("camO", "save", "N", "NC Output", s_camTab == 2, false,
                                 surf, lpHalo, ebtn, eglyph, /*useTreeGlyph=*/false))
            s_camTab = 2;
        ImGui::SameLine(0, egap);
        if (drawManagerTabButton("camD", "ws_manufacture", "D", "Devices", s_camTab == 3, false,
                                 surf, lpHalo, ebtn, eglyph, /*useTreeGlyph=*/false))
            s_camTab = 3;
        ImGui::SameLine(0, egap);
        if (drawManagerTabButton("camR", "run_study", "R", "Run", s_camTab == 4, false,
                                 surf, lpHalo, ebtn, eglyph, /*useTreeGlyph=*/false))
            s_camTab = 4;
        ImGui::SameLine(0, egap);
        // Attention is the one rail tab that carries the attention dot: an unacknowledged critical
        // item must be visible even while the operator is on another tab.
        if (drawManagerTabButton("camA", "warning_badge", "A", "Attention", s_camTab == 5,
                                 ui::manufactureCriticalCount(st) > 0,
                                 surf, lpHalo, ebtn, eglyph, /*useTreeGlyph=*/false))
            s_camTab = 5;
        ImGui::EndChild();
        {
            const float hdrX = ePaneOrigin.x + erailW + egap;
            const float hdrW = ePaneInnerW - erailW - egap;
            const char* hdrTitle = s_camTab == 0 ? "Explorer"
                                  : s_camTab == 1 ? "Job Manager"
                                  : s_camTab == 2 ? "NC Output"
                                  : s_camTab == 3 ? "Devices"
                                  : s_camTab == 4 ? "Run" : "Attention";
            const char* hdrIcon  = s_camTab == 0 ? "body_folder"
                                  : s_camTab == 1 ? "ai_panel"
                                  : s_camTab == 2 ? "save"
                                  : s_camTab == 3 ? "ws_manufacture"
                                  : s_camTab == 4 ? "run_study" : "warning_badge";
            const ImVec2 saveLocal = ImGui::GetCursorPos();
            ImGui::SetCursorScreenPos(ImVec2(hdrX, ePaneOrigin.y + erowPad + 2.0f * S));
            ui::fmPaneHeaderChip(st, hdrTitle, hdrIcon, hdrX + hdrW * 0.5f);
            ImGui::SetCursorPos(saveLocal);
        }
        {
            ImDrawList* pdl = ImGui::GetWindowDrawList();
            const float sepY = ePaneOrigin.y + erailRowH - 0.5f;
            pdl->AddLine(ImVec2(ePaneOrigin.x, sepY), ImVec2(ePaneOrigin.x + ePaneInnerW, sepY),
                         ui::Palette::toU32(pal.glassBorder), 1.0f * S);
        }
        ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
        const float ebodyPad = 12.0f * S;
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                            ImVec2(ebodyPad, ImGui::GetStyle().WindowPadding.y));
        const float eBodyTopY = ImGui::GetCursorScreenPos().y;
        ImGui::BeginChild("##cam_body", ImVec2(0, -8.0f * S), ImGuiChildFlags_AlwaysUseWindowPadding);
        if (s_camTab == 0) {
            drawFileExplorerBody(st);
        } else if (s_camTab == 1) {
            // Every integrated CAM / Manufacture body shares the Explorer tab's child inset and adds
            // no extra WindowPadding or child of its own.
            ui::drawCamTreeBody(st);
        } else if (s_camTab == 2) {
            ui::drawCamOutputBody(st);
        } else if (s_camTab == 3) {
            ui::drawManufactureDevicesBody(st);
        } else if (s_camTab == 4) {
            ui::drawManufactureRunBody(st);
        } else {
            ui::drawManufactureAttentionBody(st);
        }
        const float eBodyContentH = ImGui::GetCursorPosY();
        ImGui::EndChild();
        ImGui::PopStyleVar();
        ImGui::PopStyleColor();   // ChildBg
        ImGui::PopStyleColor();   // ImGuiCol_Text (surf.textPrimary)
        // Republish the column's ACTUAL content bottom so the triad tucks underneath when short
        // (mirrors the Model + Simulate columns).
        publishOverlayColumnHeight(0, eBodyTopY + eBodyContentH);
        ImGui::End();
        return;
    }

    // [workspace-tabs seam] Electronics workspace: the ECAD "PCB Manager" / "Schematic Manager" body
    // (ecad/ecad_panel.cpp::drawEcadManagerBody), same pattern as Simulate/Manufacture above — one
    // workspace-appropriate tree, no Explorer/FeatureManager/Configurations tabs. Hosting it INSIDE
    // this SAME pinned column (rather than a separate embedded child inside the ECAD dock window,
    // which is what app.cpp used to do) is what keeps the column's geometry byte-identical to the
    // Model tab's FeatureManager column — publishOverlayColumnEdge(0, ...) above already ran this
    // frame, so the floating doc-tab strip (doc_tabs.cpp) anchors at the SAME screen X switching
    // between Model and Electronics (previously it fell back to the band's far-left edge, since
    // Electronics never called drawLeftPane and the published edge is frame-stamped stale by one
    // frame — see doc_tabs.cpp's overlayColumnEdge(0) consumer).
    if (st.activeWorkspace == AppState::Workspace::Electronics) {
        // Electronics gets its own icon-tab rail (Explorer + PCB Manager) across the column top —
        // same geometry + drawManagerTabButton chrome as the Model FeatureManager rail (those tab
        // buttons were previously missing here). No Configurations tab. PCB Manager uses the
        // Feature Manager tree glyph (useTreeGlyph=true); Explorer uses the folder icon and hosts the
        // workspace file browser. Selection is a local static (no AppState churn) — a pure UI toggle.
        static int s_ecadTab = 1;   // 0 = Explorer, 1 = PCB Manager
        const float ebtn = 30.0f * S, eglyph = 18.0f * S, egap = 6.0f * S, erowPad = 6.0f * S;
        const float erailRowH = erowPad + ebtn + erowPad;
        const ImVec2 ePaneOrigin = ImGui::GetCursorScreenPos();
        const float  ePaneInnerW = ImGui::GetContentRegionAvail().x;
        const float  ePaneCenterX = ePaneOrigin.x + ePaneInnerW * 0.5f;
        (void)ePaneCenterX;   // header chip now positioned in the rail deadspace (round-5b)

        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0.0f, 0.0f));
        ImGui::BeginChild("##ecad_tabstrip", ImVec2(2.0f * ebtn + egap, erailRowH),
                          ImGuiChildFlags_None,
                          ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoBackground);
        ImGui::PopStyleVar();
        ImGui::SetCursorPosY(erowPad);
        ImGui::SetCursorPosX(0.0f);
        if (drawManagerTabButton("ecadE", "body_folder", "E", "Explorer", s_ecadTab == 0, false,
                                 surf, lpHalo, ebtn, eglyph, /*useTreeGlyph=*/false))
            s_ecadTab = 0;
        ImGui::SameLine(0, egap);
        if (drawManagerTabButton("ecadM", "history", "P", "PCB Manager", s_ecadTab == 1, false,
                                 surf, lpHalo, ebtn, eglyph, /*useTreeGlyph=*/true))
            s_ecadTab = 1;
        ImGui::EndChild();

        // Active-tab header chip drawn in the DEAD BAND to the RIGHT of the two square tab buttons,
        // vertically centered against them — round-5b: kills that empty band and lets the manager
        // body start straight below the rail (its chip used to sit on its own row under here, pushing
        // everything down). Cursor is saved/restored so the hairline + body flow below is unaffected.
        {
            const float railW = 2.0f * ebtn + egap;
            const float hdrX = ePaneOrigin.x + railW + egap;
            const float hdrW = ePaneInnerW - railW - egap;
            const char* hdrTitle = s_ecadTab == 0 ? "Explorer" : ui::ecadManagerTitle();
            const char* hdrIcon  = s_ecadTab == 0 ? "body_folder" : "ai_panel";
            const ImVec2 saveLocal = ImGui::GetCursorPos();
            ImGui::SetCursorScreenPos(ImVec2(hdrX, ePaneOrigin.y + erowPad + 2.0f * S));
            ui::fmPaneHeaderChip(st, hdrTitle, hdrIcon, hdrX + hdrW * 0.5f);
            ImGui::SetCursorPos(saveLocal);
        }

        // Hairline under the rail, spanning the full pane width (matches the Model rail separator).
        {
            ImDrawList* pdl = ImGui::GetWindowDrawList();
            const float sepY = ePaneOrigin.y + erailRowH - 0.5f;
            pdl->AddLine(ImVec2(ePaneOrigin.x, sepY), ImVec2(ePaneOrigin.x + ePaneInnerW, sepY),
                         ui::Palette::toU32(pal.glassBorder), 1.0f * S);
        }

        // Body. Transparent ChildBg so the inner BeginChild regions (layers/nets tree, results strip)
        // show the column glass instead of ImGui's opaque default — that fill was the flat "white bar"
        // behind Layers/Nets. Explorer reuses the workspace file browser under its own header chip;
        // PCB Manager draws its own header chip internally.
        ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
        const float ebodyPad = 12.0f * S;
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                            ImVec2(ebodyPad, ImGui::GetStyle().WindowPadding.y));
        const float eBodyTopY = ImGui::GetCursorScreenPos().y;
        ImGui::BeginChild("##ecad_body", ImVec2(0, -8.0f * S), ImGuiChildFlags_AlwaysUseWindowPadding);
        if (s_ecadTab == 0) {
            // Header chip is drawn up in the rail row (round-5b) — body starts at the file list.
            drawFileExplorerBody(st);
        } else {
            ui::drawEcadManagerBody(st, surf, lpHalo);
        }
        const float eBodyContentH = ImGui::GetCursorPosY();
        ImGui::EndChild();
        ImGui::PopStyleVar();
        ImGui::PopStyleColor();   // ImGuiCol_ChildBg
        ImGui::PopStyleColor();   // ImGuiCol_Text (surf.textPrimary)
        // Republish the column's ACTUAL content bottom so the triad tucks underneath when short
        // (mirrors the Model / Simulate / Manufacture columns).
        publishOverlayColumnHeight(0, eBodyTopY + eBodyContentH);
        ImGui::End();
        return;
    }

    // Collapse chevron — a persistent glass CHIP parked at the column's top-right corner, on the
    // same top row as the horizontal icon rail (fix 1) but reserved out of its width (chevRsv
    // below) so the two never overlap. A resting glass background (not hover-only) makes it read as an
    // obvious button rather than a stray floating mark (fix 4). Drawn before the rail / body lay out;
    // the cursor is restored so the flow below is unaffected. Position and glyph are
    // unchanged from the old collapse-to-pill version — only the toggle now runs both directions
    // (the pill's separate re-expand control is gone; this one chip drives the vertical collapse).
    const float chevSz  = 26.0f * S;                 // square chip, matches the rail icon-button height
    const float chevRsv = chevSz + 8.0f * S;         // width the rail must leave clear at the right
    {
        const float cb = chevSz;
        const ImVec2 c0(lpPaneRect.Max.x - cb - 8.0f * S, lpPaneRect.Min.y + 8.0f * S);
        const ImVec2 savedCur = ImGui::GetCursorScreenPos();
        ImGui::SetCursorScreenPos(c0);
        const bool cClicked = ImGui::InvisibleButton("##lp_collapse", ImVec2(cb, cb));
        const bool cHover = ImGui::IsItemHovered();
        ImDrawList* wdl = ImGui::GetWindowDrawList();
        // Shared collapse-chip recipe (must match Copilot ##cp_collapse):
        // rest wash 0.32 / hover 0.50, textPrimary glyph, 1.85px stroke.
        ui::glassRowBackground(wdl, c0, ImVec2(c0.x + cb, c0.y + cb), 7.0f, false, cHover,
                               cHover ? 0.50f : 0.32f);
        const ImU32 cCol = ImGui::ColorConvertFloat4ToU32(surf.textPrimary);
        const ImVec2 cc(c0.x + cb * 0.5f, c0.y + cb * 0.5f);
        const float ca = 3.5f * S;
        const float stroke = 1.85f * S;
        for (int i = 0; i < 2; ++i) {   // "«" double chevron
            const float ox = (i == 0 ? -2.5f : 2.5f) * S;
            wdl->AddLine(ImVec2(cc.x + ox + ca * 0.5f, cc.y - ca),
                         ImVec2(cc.x + ox - ca * 0.5f, cc.y), cCol, stroke);
            wdl->AddLine(ImVec2(cc.x + ox - ca * 0.5f, cc.y),
                         ImVec2(cc.x + ox + ca * 0.5f, cc.y + ca), cCol, stroke);
        }
        if (cHover) ImGui::SetTooltip(s_collapsed ? "Expand" : "Collapse");
        if (cClicked) {
            s_collapsed = !s_collapsed;
            st.settings.fmCollapsed = s_collapsed;
            saveSettings(st.settings);
        }
        ImGui::SetCursorScreenPos(savedCur);
    }

    // True horizontal center of the WHOLE pane (icon rail + body), captured BEFORE the rail child so
    // the FeatureManager header can be centered across the full panel. Centering only within the body
    // (right of the rail) left the header sitting slightly left of the panel's true center.
    const ImVec2 paneOrigin  = ImGui::GetCursorScreenPos();
    const float  paneInnerW  = ImGui::GetContentRegionAvail().x;
    const float  paneCenterX = paneOrigin.x + paneInnerW * 0.5f;

    // ---- horizontal icon-tab row (fix 1: was a vertical strip down the left edge; now a row across
    // the column's TOP edge, so the tab bodies below get the panel's FULL width — kills the old left
    // gutter dead space). The collapse chevron (fix 4) shares this top row at the far right; the row
    // reserves chevRsv on its right so the two never collide. ----
    struct TabDef { int id; const char* icon; const char* fallback; const char* tip; };
    static const TabDef kTabs[] = {
        { kLeftPaneExplorer,        "body_folder", "E", "Explorer" },
        { kLeftPaneFeatureManager,  "history",     "F", "Feature Manager" },
        { kLeftPaneGraph,           "mesh",        "G", "Graph" },
    };

    const float btn = 30.0f * S;
    const float glyph = 18.0f * S;
    const float gap = 6.0f * S;
    const int   nTabs      = (int)(sizeof(kTabs)/sizeof(kTabs[0]));
    const float rowPad     = 6.0f * S;
    const float railRowH   = rowPad + btn + rowPad;   // full top-row height (icons + top/bottom pad)
    const float paneInnerWFull = ImGui::GetContentRegionAvail().x;
    // The tab-strip child now spans ONLY the tab buttons (was the whole row minus the chevron).
    // This frees the top row's right span — between the last tab (Configurations) and the collapse
    // chevron — for the relocated OK/Cancel command buttons rendered below while a form is active.
    const float tabsW = nTabs * btn + (nTabs - 1) * gap;
    const float railW = std::max(btn, tabsW);

    // Zero the child's WindowPadding so the row background/origin and the buttons share one origin.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0.0f, 0.0f));
    ImGui::BeginChild("##lp_tabstrip", ImVec2(railW, railRowH), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoBackground);
    ImGui::PopStyleVar();
    ImGui::SetCursorPosY(rowPad);
    for (int i = 0; i < nTabs; ++i) {
        const TabDef& t = kTabs[i];
        const bool selected = (st.leftPaneTab == t.id) ||
                              (t.id == kLeftPaneFeatureManager && st.leftPaneTab == kLeftPaneSimulation);
        // PM form now lives in the FeatureManager tab, so the attention dot rides that tab.
        const bool attention = (t.id == kLeftPaneFeatureManager && pmActive);
        char idBuf[16];
        std::snprintf(idBuf, sizeof(idBuf), "%d", t.id);
        if (i == 0) ImGui::SetCursorPosX(0.0f);
        else        ImGui::SameLine(0, gap);
        if (drawManagerTabButton(idBuf, t.icon, t.fallback, t.tip, selected, attention,
                                 surf, lpHalo, btn, glyph,
                                 t.id == kLeftPaneFeatureManager)) {
            if (t.id == kLeftPaneGraph) {
                // Jump to the PLM Graph workspace (full-page). Do not leave the
                // left strip parked on an empty Graph body.
                st.activeWorkspace = AppState::Workspace::PLM;
                ui::g_plmHub.page = ui::PlmPage::Graph;
                std::string focus;
                if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()) {
                    const auto& tab = st.docTabs[st.activeTab];
                    if (!tab.path.empty()) {
                        std::filesystem::path fp(tab.path);
                        focus = fp.stem().string();
                        if (focus.empty()) focus = fp.filename().string();
                    } else if (!tab.title.empty()) {
                        focus = tab.title;
                    }
                }
                if (!focus.empty()) {
                    if (ui::plmGraphAvailable()) ui::plmGraphFocus(focus);
                }
                st.leftPaneTab = kLeftPaneFeatureManager;
            } else {
                st.leftPaneTab = t.id;
                // Opening the Simulation manager reveals + fronts the Simulation ribbon toolbar so the
                // study tools are at hand the moment the user is in the simulation workspace.
                if (t.id == kLeftPaneSimulation) {
                    st.showSimulationTools = true;
                    st.ribbonTab = AppState::RT_Simulation;
                }
            }
        }
    }
    ImGui::EndChild();

    // The tab row IS the header strip a vertical collapse rolls up to — measure its height
    // (cursor position right after the row, symmetric-padded) so PanelCollapseAnim knows the exact
    // collapsed target. Measured every frame (one-frame lag feeding next frame's window height, same
    // trick the Copilot composer already uses for s_composerH), so it tracks any future header change
    // with no hand-tuned constant to keep in sync.
    s_anim.measureHeader();

    // Below the header: OK/Cancel, the separator, and the active tab body all disappear once the
    // column has animated down near the header height — gates real work (tree/PM/config bodies)
    // rather than just clipping it, so a collapsed column costs nothing per frame.
    if (s_anim.bodyVisible()) {
        // Relocated OK/Cancel command buttons. They sit ABOVE the PM,
        // between the Configurations tab and the collapse chevron. Rendered on the pane window
        // (after the tab-strip child so their hit rect is clean), right-aligned to end just left
        // of the chevron, vertically centered on the chevron. The PropertyManager owns
        // commit/cancel + the form-ready guard.
        if (pmActive && pm) {
            const float chevLeftX  = lpPaneRect.Max.x - chevSz - 8.0f * S;
            const float rowCenterY = lpPaneRect.Min.y + 8.0f * S + chevSz * 0.5f;
            pm->drawChromeButtons(st, chevLeftX - 8.0f * S, rowCenterY);
        } else {
            // Pane title INLINE with the square tab buttons: right-aligned in the header
            // row, vertically centered on the button row, NO icon — the highlighted tab button
            // already carries the glyph. Its owned band begins after the last tab and ends before
            // the collapse chip; elide within that measured span rather than painting through
            // either control when the pane is narrow.
            const char* paneTitle = "Feature Manager";
            if (st.leftPaneTab == kLeftPaneExplorer)             paneTitle = "Explorer";
            else if (st.leftPaneTab == kLeftPaneConfigurations) paneTitle = "Configurations";
            else if (st.leftPaneTab == kLeftPaneGraph)           paneTitle = "Graph";
            const float titleLeftX = paneOrigin.x + railW + gap;
            const float collapseLeftX = lpPaneRect.Max.x - chevSz - 8.0f * S;
            const float titleRightX = collapseLeftX - 8.0f * S;
            const float titleBandW = titleRightX - titleLeftX;
            if (titleBandW > 0.0f) {
                const std::string shownTitle = ui::kit::elide(paneTitle, titleBandW);
                if (!shownTitle.empty()) {
                    ImDrawList* pdl = ImGui::GetWindowDrawList();
                    const ImVec2 tsz = ImGui::CalcTextSize(shownTitle.c_str());
                    const float tx = titleRightX - tsz.x;
                    const float ty = paneOrigin.y + railRowH * 0.5f - tsz.y * 0.5f;
                    pdl->AddText(ImVec2(tx, ty), ui::Palette::toU32(pal.text),
                                 shownTitle.c_str());
                }
            }
        }

        // Horizontal separator under the icon row, spanning the full pane width on the pane draw
        // list.
        {
            ImDrawList* pdl = ImGui::GetWindowDrawList();
            const float sepY = paneOrigin.y + railRowH - 0.5f;
            pdl->AddLine(ImVec2(paneOrigin.x, sepY),
                         ImVec2(paneOrigin.x + paneInnerWFull, sepY),
                         ui::Palette::toU32(pal.glassBorder), 1.0f * S);
        }

        // ---- active tab body ----
        // (The pane title now renders inline with the square tab buttons above — see the header
        // row — so there is no separate centered header chip here anymore.)

        // Equal left/right margins via symmetric WindowPadding + a slim scrollbar. When the form
        // overflows, ImGui reserves a vertical scrollbar on the right, which would make the right
        // gap (padding + scrollbar) wider than the left (padding) — exactly the lopsided look on
        // the tall Extrude PropertyManager. We mirror it by indenting the content by the
        // scrollbar width while scrolling, so the gap from the rail equals the gap from the panel
        // edge in BOTH states. s_bodyScrolled samples last frame's scroll state (queried just
        // before EndChild).
        static bool s_bodyScrolled = false;
        const float bodyPad = 12.0f * S;
        const float sbSize  = 8.0f * S;
        // Transparent body — row-level glass pills only; viewport shows through gaps.
        ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
        // Tight top padding: the theme's default WindowPadding.y (~9dp) left a large dead gap between
        // the divider under the square tab buttons and the pane-header title chip. 4dp reads snug.
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(bodyPad, 4.0f * S));
        ImGui::PushStyleVar(ImGuiStyleVar_ScrollbarSize, sbSize);
        // Inset the scroll region above the bubble's rounded bottom edge so the last row (the
        // file strip's "(unsaved)" line / buttons) is never clipped by the corner radius
        // (visual-review fix).
        const float bodyTopScreenY = ImGui::GetCursorScreenPos().y;
        // NoScrollbar: we replace ImGui's default bar with the custom pill-on-track scrollbar
        // (ui::drawWindowPillScrollbarV) drawn just before EndChild, matching the doc-tab file strip.
        ImGui::BeginChild("##lp_body", ImVec2(0, -8.0f * S), ImGuiChildFlags_AlwaysUseWindowPadding,
                          ImGuiWindowFlags_NoScrollbar);
        const bool bodyIndented = s_bodyScrolled;
        if (bodyIndented) ImGui::Indent(sbSize);   // balance the right-side scrollbar gutter
        // Title moved up into the tab-button row; the tree/body starts straight below the top
        // divider under the buttons (no centered header chip, no extra separator here).
        switch (st.leftPaneTab) {

            case kLeftPaneExplorer:
                // Wave 3: the workspace file browser (file_explorer.cpp).
                drawFileExplorerBody(st);
                break;

            case kLeftPaneConfigurations:
                // Configurations moved to Settings → General. Legacy leftPaneTab
                // values (View menu / stale state) redirect there once.
                st.openSettings = true;
                st.openSettingsTab = 1;  // General
                st.leftPaneTab = kLeftPaneFeatureManager;
                break;

            case 5:  // retired PLM rail tab; preserve older in-memory selections
                st.leftPaneTab = kLeftPaneExplorer;
                drawFileExplorerBody(st);
                break;
            case kLeftPaneGraph:
                // Should not stick — click handler jumps to PLM Graph workspace.
                st.leftPaneTab = kLeftPaneFeatureManager;
                break;

            case kLeftPaneSimulation:   // legacy programmatic target — render the same unified body
            case kLeftPaneFeatureManager:
            default:
                // A live command form owns the tab body (the former standalone PropertyManager
                // tab was merged in here as a duplicate). At rest, the feature tree + inline
                // simulation section.
                if (pmActive) {
                    pm->drawBody(st);
                } else {
                    // [sim-gui rework] The simulation study tree does NOT render
                    // here any more: it exists ONLY in the Simulate workspace (the early-return
                    // Simulate branch above owns it). Model/Drawing show the feature tree alone.
                    drawFeatureTreeBody(st, editCallback);
                }
                break;
        }
        // Match drawWindowPillScrollbarV's phantom-overflow tolerance so the gutter indent is only
        // reserved when a real bar actually shows (was: any sub-px overflow reserved the gutter).
        s_bodyScrolled = ImGui::GetScrollMaxY() >
                         (ImGui::GetStyle().WindowPadding.y + 4.0f * S);   // next frame's indent balance
        // Republish the FM column's ACTUAL content bottom (header + rows), overriding the full-window
        // height stamped above — so the orientation triad only dodges right when the tree really
        // reaches its row; otherwise the triad anchors at the far-left, underneath the column.
        const float bodyContentH = ImGui::GetCursorPosY();   // child-local content extent
        if (bodyIndented) ImGui::Unindent(sbSize);
        // Custom pill-on-track scrollbar (replaces the suppressed default bar) — same style as the
        // doc-tab file strip. No-op unless the body overflows.
        ui::drawWindowPillScrollbarV(surf);
        ImGui::EndChild();
        publishOverlayColumnHeight(0, bodyTopScreenY + bodyContentH);   // absolute content bottom Y
        ImGui::PopStyleVar(2);   // WindowPadding + ScrollbarSize pushed before the body child
        ImGui::PopStyleColor();  // ChildBg transparent
    }  // s_anim.bodyVisible()

    ImGui::PopStyleColor();  // surf.textPrimary pushed after Begin (liquid glass v2)
    ImGui::End();
}

}  // namespace ui
