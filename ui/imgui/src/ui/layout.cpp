// AI-CAD2 dock layout + pinned glass columns — chrome parity with host/src/ui/layout.cpp (T-G23).
//
// Overlay redesign (host parity): buildDefaultLayout() builds ONE splitless central node so the 3D
// viewport spans the full dockspace edge-to-edge, and drawLeftPane()/drawRightPane() render the
// FeatureManager + Copilot as pinned full-height rounded glass columns floating OVER the viewport's
// left/right edges (no left/right dock splits, no docked "Browser"/"Assistant" windows). This
// replaces the rejected ShopCraft docked "Model Tree"/Browser layout.
#include "ui/layout.h"
#include "ui/panels.h"

#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/tokens.h"
#include "theme/icons.h"

#include "imgui.h"
#include "imgui_internal.h"

#include <algorithm>
#include <cstdio>

// T-G23 overlay layout (host/src/ui/layout.cpp parity): the dockspace hosts ONLY the central
// document window (Viewport / ECAD / CAM, tabbed in ONE splitless node) so the 3D surface runs edge
// to edge between the ribbon and the status bar. The FeatureManager and Copilot are pinned floating
// glass columns drawn OVER the viewport (drawLeftPane / drawRightPane below) — no left/right dock
// splits, no docked "Browser"/"Assistant" windows, no splitter bars.
void buildDefaultLayout(ImGuiID root, const ImVec2& size) {
    ImGui::DockBuilderRemoveNode(root);
    ImGui::DockBuilderAddNode(root, ImGuiDockNodeFlags_DockSpace);
    ImGui::DockBuilderSetNodeSize(root, size);

    // Central node — 3D default-active; ECAD/CAM pre-registered for domain waves, tabbed with the
    // Viewport in the same node (docked before it so 3D stays the default-active tab).
    ImGui::DockBuilderDockWindow("ECAD", root);
    ImGui::DockBuilderDockWindow("CAM", root);
    ImGui::DockBuilderDockWindow("Viewport###viewport", root);

    ImGui::DockBuilderFinish(root);
}

namespace ui {

// ---- overlay band + pinned-column occlusion plumbing (see layout.h) ----------------------------
static OverlayBand g_overlayBand;

void setOverlayBand(float minX, float minY, float maxX, float maxY) {
    g_overlayBand.minX = minX;
    g_overlayBand.minY = minY;
    g_overlayBand.maxX = maxX;
    g_overlayBand.maxY = maxY;
    g_overlayBand.valid = (maxX > minX && maxY > minY);
}
const OverlayBand& overlayBand() { return g_overlayBand; }

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

// Floating command dialogs (Evaluate / assembly / Cut List …) lost their docked rightBottom slot
// with the overlay layout; give them a sane first-open anchor: top-right inside the overlay band,
// left of the Copilot column's default pin. Pivot (1,0) anchors the window's top-RIGHT corner.
void setNextCommandDialogPlacement() {
    const OverlayBand& band = overlayBand();
    if (!band.valid) return;  // pre-publish frame: keep ImGui's default cascade
    const float S = theme::S();
    const float margin = 12.0f * S;
    const float rightColW = 380.0f * (1.0f + (S - 1.0f) * 0.55f);
    const float x = band.maxX - margin - rightColW - margin;
    ImGui::SetNextWindowPos(ImVec2(x, band.minY + margin), ImGuiCond_FirstUseEver, ImVec2(1, 0));
}

namespace {

void drawDockSplittersRec(ImGuiDockNode* node, ImDrawList* dl, ImU32 col, float thickness) {
    if (!node || !node->IsVisible)
        return;
    if (!node->IsSplitNode())
        return;

    ImGuiDockNode* c0 = node->ChildNodes[0];
    ImGuiDockNode* c1 = node->ChildNodes[1];
    if (c0 && c1 && c0->IsVisible && c1->IsVisible) {
        const ImRect r0 = c0->Rect();
        const ImRect r1 = c1->Rect();
        if (node->SplitAxis == ImGuiAxis_X) {
            const float x = (r0.Max.x + r1.Min.x) * 0.5f;
            dl->AddLine(ImVec2(x, node->Pos.y),
                        ImVec2(x, node->Pos.y + node->Size.y), col, thickness);
        } else {
            const float y = (r0.Max.y + r1.Min.y) * 0.5f;
            dl->AddLine(ImVec2(node->Pos.x, y),
                        ImVec2(node->Pos.x + node->Size.x, y), col, thickness);
        }
    }

    drawDockSplittersRec(c0, dl, col, thickness);
    drawDockSplittersRec(c1, dl, col, thickness);
}

}  // namespace

void drawDockSplitters(ImGuiID dockRootId) {
    ImGuiDockNode* root = ImGui::DockBuilderGetNode(dockRootId);
    if (!root)
        return;

    const float S = theme::S();
    const ImU32 col = theme::roleU32A(theme::pal().line, 0.35f);
    const float thickness = std::max(1.f, 1.f * S);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    drawDockSplittersRec(root, dl, col, thickness);
}

// ---- T-G23 pinned glass columns (host/src/ui/layout.cpp drawLeftPane parity) --------------------
// Both columns share one geometry model: anchored to the overlay band application.cpp publishes each
// frame (ribbon bottom -> status-bar top), inset by ~12dp side/bottom margins with a slim top margin
// so all chrome shares its top edge. The whole-column glass slab IS painted (UltraThin) so the pane
// reads as a frosted card over the full-width viewport; the individual bodies (feature_tree /
// file_explorer / property_manager / agent_panel) keep their own row-level glass pills.

namespace {

// A small glass "pill" affordance at the column anchor that re-expands a collapsed column.
// Returns true if clicked. `leftEdge` pins to the band's left (FM) or right (Copilot) side.
bool drawColumnPill(int slot, const OverlayBand& band, bool leftEdge, const char* icon,
                    const char* tip) {
    const float S = theme::S();
    const float margin = 12.0f * S;
    const float topMargin = 4.0f * S;
    const float pill = 40.0f * S;
    const float px = leftEdge ? (band.minX + margin) : (band.maxX - margin - pill);
    const float py = band.minY + topMargin;
    // Publish the pill's inner edge so the HUD / doc-tabs dodge it like the expanded column.
    publishOverlayColumnEdge(slot, leftEdge ? (px + pill) : px);
    ImGui::SetNextWindowPos(ImVec2(px, py), ImGuiCond_Always);
    ImGui::SetNextWindowSize(ImVec2(pill, pill), ImGuiCond_Always);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, pill * 0.5f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    const ImGuiWindowFlags pillFlags = ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
        ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoDocking |
        ImGuiWindowFlags_NoBackground | ImGuiWindowFlags_NoScrollbar |
        ImGuiWindowFlags_NoSavedSettings;
    char wid[32];
    std::snprintf(wid, sizeof(wid), "##ColPill%d", slot);
    bool clicked = false;
    if (ImGui::Begin(wid, nullptr, pillFlags)) {
        const ImVec2 pp = ImGui::GetWindowPos();
        const ImRect pr(pp, ImVec2(pp.x + pill, pp.y + pill));
        char pid[32]; std::snprintf(pid, sizeof(pid), "col_pill_%d", slot);
        const theme::AdaptiveSurface& psurf = theme::adaptivePanel(theme::panelId(pid), pr);
        theme::glassPanel(ImGui::GetWindowDrawList(), pr, theme::MaterialLevel::UltraThin,
                          pill * 0.5f / S, psurf);
        clicked = ImGui::InvisibleButton("##expand", ImVec2(pill, pill));
        const ImU32 halo = theme::viewportBackdrop().valid()
            ? ImGui::ColorConvertFloat4ToU32(ImVec4(psurf.tint.x, psurf.tint.y, psurf.tint.z,
                                                    0x50 / 255.f))
            : 0;
        icons::drawGlass(ImGui::GetWindowDrawList(), icon,
                         ImVec2(pp.x + pill * 0.5f, pp.y + pill * 0.5f), 20.0f * S,
                         ImGui::ColorConvertFloat4ToU32(psurf.textPrimary), halo);
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", tip);
    }
    ImGui::End();
    ImGui::PopStyleVar(3);
    return clicked;
}

// Resolve the anchored band once (falls back to the main viewport work rect pre-publish).
OverlayBand resolvedBand() {
    OverlayBand band = overlayBand();
    if (!band.valid) {
        const ImGuiViewport* mvp = ImGui::GetMainViewport();
        band.minX = mvp->WorkPos.x;
        band.minY = mvp->WorkPos.y;
        band.maxX = mvp->WorkPos.x + mvp->WorkSize.x;
        band.maxY = mvp->WorkPos.y + mvp->WorkSize.y;
        band.valid = true;
    }
    return band;
}

// Draw the little collapse chevron chip at the pane's top corner. Returns true when clicked.
bool drawCollapseChip(const ImRect& paneRect, bool pointLeft, const theme::AdaptiveSurface& surf) {
    const float S = theme::S();
    const float cb = 26.0f * S;
    const float cx = pointLeft ? (paneRect.Max.x - cb - 8.0f * S) : (paneRect.Min.x + 8.0f * S);
    const ImVec2 c0(cx, paneRect.Min.y + 8.0f * S);
    const ImVec2 savedCur = ImGui::GetCursorScreenPos();
    ImGui::SetCursorScreenPos(c0);
    const bool clicked = ImGui::InvisibleButton("##collapse", ImVec2(cb, cb));
    const bool hover = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    theme::glassRowBackground(dl, c0, ImVec2(c0.x + cb, c0.y + cb), 7.0f, false, false,
                              hover ? theme::rowWash::selected : theme::rowWash::idle);
    const ImU32 col = ImGui::ColorConvertFloat4ToU32(hover ? surf.textPrimary : surf.textSecondary);
    const ImVec2 cc(c0.x + cb * 0.5f, c0.y + cb * 0.5f);
    const float ca = 3.5f * S;
    const float dir = pointLeft ? 1.0f : -1.0f;   // "«" collapse-left  or  "»" collapse-right
    for (int i = 0; i < 2; ++i) {
        const float ox = (i == 0 ? -2.5f : 2.5f) * S;
        dl->AddLine(ImVec2(cc.x + ox + dir * ca * 0.5f, cc.y - ca),
                    ImVec2(cc.x + ox - dir * ca * 0.5f, cc.y), col, 1.6f * S);
        dl->AddLine(ImVec2(cc.x + ox - dir * ca * 0.5f, cc.y),
                    ImVec2(cc.x + ox + dir * ca * 0.5f, cc.y + ca), col, 1.6f * S);
    }
    if (hover) ImGui::SetTooltip("Collapse");
    ImGui::SetCursorScreenPos(savedCur);
    return clicked;
}

}  // namespace

void drawLeftPane(app::AppState& s) {
    static bool s_collapsed = false;
    const float S = theme::S();
    const OverlayBand band = resolvedBand();

    // A live command form pops the collapsed pill open so the form is visible (host auto-switch).
    static bool s_wasActive = false;
    const bool pmActive = !s.activeTool.empty();
    if (pmActive && !s_wasActive) s_collapsed = false;
    s_wasActive = pmActive;

    if (s_collapsed) {
        if (drawColumnPill(0, band, /*leftEdge=*/true, "rail_tree", "Feature Manager"))
            s_collapsed = false;
        return;
    }

    const float margin = 12.0f * S;
    const float topMargin = 4.0f * S;
    const float panelSc = 1.0f + (S - 1.0f) * 0.55f;
    const float colX = band.minX + margin;
    const float colY = band.minY + topMargin;
    const float colH = std::max(120.0f * S, band.maxY - margin - colY);
    const float minW = 250.0f * panelSc;
    const float defW = 360.0f * panelSc;
    const float maxW = std::max(minW + 40.0f * S,
                                std::min(560.0f * panelSc, (band.maxX - band.minX) * 0.42f));
    const float bubbleDp = theme::ctx().m.roundingLg;

    ImGui::SetNextWindowPos(ImVec2(colX, colY), ImGuiCond_Always);
    ImGui::SetNextWindowSize(ImVec2(defW, colH), ImGuiCond_FirstUseEver);
    ImGui::SetNextWindowSizeConstraints(ImVec2(minW, colH), ImVec2(maxW, colH));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, bubbleDp * S);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0.0f);
    ImGui::PushStyleColor(ImGuiCol_ResizeGrip, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_ResizeGripHovered, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_ResizeGripActive, ImVec4(0, 0, 0, 0));
    const ImGuiWindowFlags lpFlags = ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoCollapse |
        ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoDocking | ImGuiWindowFlags_NoScrollbar;
    const bool open = ImGui::Begin("FeatureManager##LeftPane", nullptr, lpFlags);
    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar(2);
    if (!open) { ImGui::End(); return; }

    const ImVec2 wp = ImGui::GetWindowPos(), ws = ImGui::GetWindowSize();
    const ImRect paneRect(wp, ImVec2(wp.x + ws.x, wp.y + ws.y));
    publishOverlayColumnEdge(0, paneRect.Max.x);
    const theme::AdaptiveSurface& surf =
        theme::adaptivePanel(theme::panelId("left_pane_glass"), paneRect);
    theme::glassPanel(ImGui::GetWindowDrawList(), paneRect, theme::MaterialLevel::UltraThin,
                      bubbleDp, surf);

    if (drawCollapseChip(paneRect, /*pointLeft=*/true, surf)) { s_collapsed = true; ImGui::End(); return; }

    // Vertical icon-tab rail (Model Tree / Bodies / Files / Configurations) across the column top,
    // reserving the chevron corner; then the active tab body + the PropertyManager form inside.
    const float railW = 48.0f * S;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGui::BeginChild("##lp_rail", ImVec2(railW, colH - 48.0f * S), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoBackground);
    drawActivityRail(s, railW);
    ImGui::EndChild();
    ImGui::PopStyleVar();
    ImGui::SameLine(0, 4.0f * S);

    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("##lp_body", ImVec2(0, 0), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoBackground);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    if (pmActive) drawInspector(s);
    drawLeftDock(s);
    ImGui::PopStyleColor();
    ImGui::EndChild();
    ImGui::PopStyleColor();

    ImGui::End();
}

void drawRightPane(app::AppState& s) {
    static bool s_collapsed = false;
    const float S = theme::S();
    const OverlayBand band = resolvedBand();

    if (s_collapsed) {
        if (drawColumnPill(1, band, /*leftEdge=*/false, "agent", "Copilot"))
            s_collapsed = false;
        return;
    }

    const float margin = 12.0f * S;
    const float topMargin = 4.0f * S;
    const float panelSc = 1.0f + (S - 1.0f) * 0.55f;
    const float colH = std::max(120.0f * S, band.maxY - margin - (band.minY + topMargin));
    const float minW = 270.0f * panelSc;
    const float defW = 380.0f * panelSc;
    const float maxW = std::max(minW + 40.0f * S,
                                std::min(560.0f * panelSc, (band.maxX - band.minX) * 0.42f));
    const float colY = band.minY + topMargin;
    const float bubbleDp = theme::ctx().m.roundingLg;

    // Right-pinned: width is free but the RIGHT edge stays anchored. ImGui resizes from the right
    // edge, so we pin the top-right corner via pivot and let the left edge float; simpler + robust:
    // pin X to (band.maxX - margin - defW) at first-use and keep the right edge via a per-frame X.
    static float s_curW = defW;
    s_curW = std::min(std::max(s_curW, minW), maxW);
    const float colX = band.maxX - margin - s_curW;

    ImGui::SetNextWindowPos(ImVec2(colX, colY), ImGuiCond_Always);
    ImGui::SetNextWindowSize(ImVec2(s_curW, colH), ImGuiCond_Always);
    ImGui::SetNextWindowSizeConstraints(ImVec2(minW, colH), ImVec2(maxW, colH));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, bubbleDp * S);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0.0f);
    const ImGuiWindowFlags rpFlags = ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoCollapse |
        ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoDocking | ImGuiWindowFlags_NoResize |
        ImGuiWindowFlags_NoScrollbar;
    const bool open = ImGui::Begin("Copilot##RightPane", nullptr, rpFlags);
    ImGui::PopStyleVar(2);
    if (!open) { ImGui::End(); return; }

    const ImVec2 wp = ImGui::GetWindowPos(), ws = ImGui::GetWindowSize();
    const ImRect paneRect(wp, ImVec2(wp.x + ws.x, wp.y + ws.y));
    publishOverlayColumnEdge(1, paneRect.Min.x);
    const theme::AdaptiveSurface& surf =
        theme::adaptivePanel(theme::panelId("right_pane_glass"), paneRect);
    theme::glassPanel(ImGui::GetWindowDrawList(), paneRect, theme::MaterialLevel::UltraThin,
                      bubbleDp, surf);

    if (drawCollapseChip(paneRect, /*pointLeft=*/false, surf)) { s_collapsed = true; ImGui::End(); return; }

    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::Dummy(ImVec2(0, 34.0f * S));   // clear the chevron corner row
    ImGui::BeginChild("##rp_body", ImVec2(0, 0), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoBackground);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    drawAgentPanel(s);
    ImGui::PopStyleColor();
    ImGui::EndChild();
    ImGui::PopStyleColor();

    ImGui::End();
}

}  // namespace ui
