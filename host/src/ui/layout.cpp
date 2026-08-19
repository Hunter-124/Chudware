// Chudware overlay layout + the pinned FeatureManager glass column — see layout.h.
//
// Overlay redesign: buildDefaultLayout() keeps one uninterrupted central Viewport / ECAD node so
// the 3D surface spans the full dockspace. Manufacture previews render natively in that viewport,
// while drawLeftPane() hosts Explorer, Job Manager, NC Output, Devices, Run and Attention as
// integrated views in the pinned full-height glass column over its left edge. The invisible
// right-edge resize and collapse-to-pill affordance remain; command dialogs float above the
// central node.
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
#include "plm_overview.h"       // compact PLM overview tab (left of Explorer on Model)
#include "doc_tabs.h"           // docTabStripHeight (the column never overlaps the doc-tab band)
#include "icons.h"              // getIcon / IconUV
#include "metrics.h"            // dockRightTarget (command-dialog default anchor)
#include "panel_collapse.h"     // ui::PanelCollapseAnim — shared vertical-collapse animation (T-G21)

#include <filesystem>
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>

// Overlay layout: the dockspace hosts ONLY the central document windows, tabbed in one node, so
// the viewport runs edge to edge between the ribbon and the status bar. The FeatureManager and
// Copilot are pinned floating glass columns drawn OVER the viewport (drawLeftPane below /
// drawAIPanel in ui/copilot/panel.cpp) — no left/right dock splits, no splitter bars anywhere.
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


// Floating command dialogs (Evaluate / assembly / Cut List / Design Table…) lost their docked
// rightBottom slot with the overlay layout; give them a sane first-open anchor: top-right inside
// the overlay band, left of the Copilot column's default pin, 12dp margins. Pivot (1,0) anchors
// the window's top-RIGHT corner so the placement works for any dialog width.
void setNextCommandDialogPlacement() {
    const OverlayBand& band = overlayBand();
    if (!band.valid) return;  // pre-publish frame: keep ImGui's default cascade
    const float margin = 12.0f * ui::uiScale;
    const float x = band.maxX - margin - ui::metrics::dockRightTarget() - margin;
    ImGui::SetNextWindowPos(ImVec2(x, band.minY + margin), ImGuiCond_FirstUseEver, ImVec2(1, 0));
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

    // [sim-gui rework, Hunter red-pen] The simulation setup tree exists ONLY in the Simulate
    // workspace. SimManager::handleCommand parks st.leftPaneTab on LP_Simulation; if the user
    // then switches to Model/Drawing (or any non-Simulate workspace) clamp it back to the
    // FeatureManager so no Simulation pane state leaks into the modeling views (the inverse of
    // the Simulate-side takeover below, which ignores the tab strip entirely).
    if (st.activeWorkspace != AppState::Workspace::Simulate &&
        st.leftPaneTab == kLeftPaneSimulation)
        st.leftPaneTab = kLeftPaneFeatureManager;

    // Vertical-collapse state (T-G21): the column rolls UP to its header (tab row + collapse
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
    const float colH = std::max(120.0f * S, band.maxY - margin - colY);

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
    // T-G21 screenshot hook: --ui-screenshot always resets the overlay columns for determinism
    // (io.IniFilename is nulled in shot mode, so DockBuilderGetNode is always null and the reset
    // branch above always fires) — the persisted collapse setting can't survive that on its own.
    // CHUDWARE_FM_COLLAPSED forces the collapsed capture the same way CHUDWARE_COPILOT_NARROW forces
    // a pane width; no effect outside a screenshot run.
    if (std::getenv("CHUDWARE_FM_COLLAPSED")) {
        s_collapsed = true;
        s_anim.snap(true, colH);  // snap immediately — headless captures must not depend on frame count
    }

    // ---- pinned full-height-or-collapsed glass column -------------------------------------------
    // Position is pinned every frame; WIDTH is resized via ImGui's invisible edge hit-zone
    // (ConfigWindowsResizeFromEdges, EW mouse-cursor feedback, no drawn bars) and persisted in the
    // imgui ini like any floating window. HEIGHT animates between the full column height and the
    // header-strip height (T-G21: vertical collapse — rolls UP to the tab row + chevron instead of
    // shrinking to a pill; see ui::PanelCollapseAnim). Default/min widths reuse the HiDPI-damped
    // targets of the old dock split.
    const float panelSc = 1.0f + (S - 1.0f) * 0.55f;
    // Manufacture hosts a SIX-tab icon rail (Explorer / Job Manager / NC Output / Devices / Run /
    // Attention) across the column top — 6 * 30dp + 5 * 6dp = 210dp of rail before the header chip.
    // It gets a wider default AND a hard minimum so the rail can never be squeezed narrower than its
    // own buttons; unlike Electronics the size is NOT forced (FirstUseEver below), so the user keeps
    // the ability to widen it and the min-width constraint is what guarantees the fit.
    const bool mfgWs  = (st.activeWorkspace == AppState::Workspace::Manufacture);
    const float minW = (mfgWs ? 360.0f : 250.0f) * panelSc;
    // Electronics (PCB/Schematic Manager) runs a little wider than the Model FeatureManager — its
    // layer/net rows carry a swatch + label + eye toggle, so the extra width keeps them uncramped
    // (Hunter red-pen). Forced (Always) on that workspace so it reliably applies to the shared,
    // otherwise user-resized column; the doc-tab strip re-anchors to this edge each frame, so a wider
    // Electronics column just pushes its own tabs right — Model stays at its own persisted width.
    const bool ecadWs = (st.activeWorkspace == AppState::Workspace::Electronics);
    const float defW = (ecadWs ? 360.0f : mfgWs ? 380.0f : 320.0f) * panelSc;
    const float maxW = std::max(minW + 40.0f * S,
                                std::min(560.0f * panelSc, (band.maxX - band.minX) * 0.42f));
    const float winH = s_anim.tick(s_collapsed, colH);
    ImGui::SetNextWindowPos(ImVec2(colX, colY), ImGuiCond_Always);
    ImGui::SetNextWindowSize(ImVec2(defW, winH),
                             (wantReset || ecadWs) ? ImGuiCond_Always : ImGuiCond_FirstUseEver);
    ImGui::SetNextWindowSizeConstraints(ImVec2(minW, winH), ImVec2(maxW, winH));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, bubbleDp * S);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0.0f);
    // Kill every drawn resize affordance (corner grips + the edge hover/active flash) — the
    // cursor change is the only feedback. Popped right after Begin (decorations render inside it);
    // in-window Separator() uses ImGuiCol_Separator, which stays untouched.
    ImGui::PushStyleColor(ImGuiCol_ResizeGrip, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_ResizeGripHovered, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_ResizeGripActive, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_SeparatorHovered, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_SeparatorActive, ImVec4(0, 0, 0, 0));
    // Input-regression fix: this column previously carried only NoBackground and was reliably
    // clickable; commit 043e752 added NoBringToFrontOnFocus (to keep it below transient command
    // dialogs) and the whole pane stopped responding to the mouse. FM (left edge) and the floating
    // dialogs (top-right) don't overlap, so dropping the flag restores click routing without
    // re-introducing any layering issue.
    const ImGuiWindowFlags lpFlags = ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoCollapse |
        ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoDocking | ImGuiWindowFlags_NoBackground |
        ImGuiWindowFlags_NoScrollbar;
    const bool lpOpen = ImGui::Begin("FeatureManager##LeftPane", nullptr, lpFlags);
    ImGui::PopStyleColor(5);
    ImGui::PopStyleVar(2);
    if (!lpOpen) { ImGui::End(); return; }

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
    // Hunter hard requirement: no feature/model tree, no Explorer/Configurations tabs.
    // Entire tab strip + normal body is suppressed; only SimManager::drawTreeBody runs,
    // styled to fill the full column width with the same glass row discipline.
    if (st.activeWorkspace == AppState::Workspace::Simulate && st.simManager) {
        const float S = ui::uiScale;
        // Explorer + Simulation Manager two-button rail — same chrome as the Electronics rail so
        // every workspace column shares one navigation pattern (Hunter directive).
        static int s_simTab = 1;   // 0 = Explorer, 1 = Simulation Manager
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
        // same geometry + drawManagerTabButton chrome as the Model FeatureManager rail (Hunter
        // red-pen: those tab buttons were missing here). No Configurations tab. PCB Manager uses the
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
    // the cursor is restored so the flow below is unaffected. T-G21: position and glyph are
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
        { kLeftPanePLM,             "app",         "P", "PLM" },
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
    // T-G19: the tab-strip child now spans ONLY the tab buttons (was the whole row minus the chevron).
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

    // T-G21: the tab row IS the header strip a vertical collapse rolls up to — measure its height
    // (cursor position right after the row, symmetric-padded) so PanelCollapseAnim knows the exact
    // collapsed target. Measured every frame (one-frame lag feeding next frame's window height, same
    // trick the Copilot composer already uses for s_composerH), so it tracks any future header change
    // with no hand-tuned constant to keep in sync.
    s_anim.measureHeader();

    // Below the header: OK/Cancel, the separator, and the active tab body all disappear once the
    // column has animated down near the header height — gates real work (tree/PM/config bodies)
    // rather than just clipping it, so a collapsed column costs nothing per frame.
    if (s_anim.bodyVisible()) {
        // T-G19: relocated OK/Cancel command buttons. Erik round-1 asked for them ABOVE the PM,
        // between the Configurations tab and the collapse chevron. Rendered on the pane window
        // (after the tab-strip child so their hit rect is clean), right-aligned to end just left
        // of the chevron, vertically centered on the chevron. The PropertyManager owns
        // commit/cancel + the form-ready guard.
        if (pmActive && pm) {
            const float chevLeftX  = lpPaneRect.Max.x - chevSz - 8.0f * S;
            const float rowCenterY = lpPaneRect.Min.y + 8.0f * S + chevSz * 0.5f;
            pm->drawChromeButtons(st, chevLeftX - 8.0f * S, rowCenterY);
        } else {
            // Pane title INLINE with the square tab buttons (Hunter): right-aligned in the header
            // row, vertically centered on the button row, NO icon — the highlighted tab button
            // already carries the glyph. Replaces the old centered header chip below the divider.
            const char* paneTitle = "Feature Manager";
            if (st.leftPaneTab == kLeftPanePLM)                  paneTitle = "PLM";
            else if (st.leftPaneTab == kLeftPaneExplorer)        paneTitle = "Explorer";
            else if (st.leftPaneTab == kLeftPaneConfigurations) paneTitle = "Configurations";
            else if (st.leftPaneTab == kLeftPaneGraph)           paneTitle = "Graph";
            ImDrawList* pdl = ImGui::GetWindowDrawList();
            const ImVec2 tsz = ImGui::CalcTextSize(paneTitle);
            const float rightX = lpPaneRect.Max.x - chevSz - 16.0f * S;   // clear of the collapse chevron
            const float tx = rightX - tsz.x;
            const float ty = paneOrigin.y + railRowH * 0.5f - tsz.y * 0.5f;
            pdl->AddText(ImVec2(tx, ty), ui::Palette::toU32(pal.text), paneTitle);
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
            case kLeftPanePLM:
                drawPlmOverviewBody(st);
                break;

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
                    // [sim-gui rework, Hunter red-pen] The simulation study tree does NOT render
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
