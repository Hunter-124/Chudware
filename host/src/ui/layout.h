// AI-CAD overlay layout + the pinned FeatureManager glass column.
//
// Overlay redesign: the dockspace hosts ONLY the central document windows (Viewport / ECAD / CAM,
// tabbed) so the 3D surface runs edge to edge between the ribbon and the status bar. The old
// SolidWorks docked-columns arrangement (left task pane / right task pane / dialogs slot) is GONE —
// buildDefaultLayout() now builds a single splitless central node, and the FeatureManager renders
// as a pinned full-height rounded glass column OVER the viewport's left edge (drawLeftPane). The
// Copilot column (legacy_chat.cpp) mirrors the same pattern on the right edge.
//
// buildDefaultLayout stays in the GLOBAL namespace because app_state.h forward-declares it there
// and app.cpp calls it unqualified — keeping it global avoids touching either site.
#pragma once

#include <functional>
#include <string>

struct AppState;

// Build the default dock arrangement (Reset layout rebuilds exactly this): one central node, no
// splits. GLOBAL on purpose — see app_state.h's forward declaration and the unqualified call in
// app.cpp.
struct ImVec2;
typedef unsigned int ImGuiID;
void buildDefaultLayout(ImGuiID root, const ImVec2& size);

namespace ui {

// Wave 1D: left-pane tab indices. These MUST match AppState::LeftPaneTab (LP_*) values 0..3; the
// brief specifies the kLeftPane* spelling for this module, and the two enums are kept numerically
// identical (Explorer=0, FeatureManager=1, PropertyManager=2, Configurations=3).
enum {
    kLeftPaneExplorer        = 0,
    kLeftPaneFeatureManager  = 1,
    kLeftPanePropertyManager = 2,
    kLeftPaneConfigurations  = 3,
    kLeftPaneSimulation      = 4,
};

// ---- overlay band ------------------------------------------------------------------------------
// The screen-space rect between the ribbon (bottom edge of the chrome) and the status bar — the
// region the full-width viewport surface fills and the pinned glass columns overlay. app.cpp
// publishes it once per frame (setOverlayBand, right where dockTopY/statusH are computed) before
// the panels draw; consumers (drawLeftPane here, the Copilot column in legacy_chat.cpp) read it
// via overlayBand() to anchor their pinned geometry.
struct OverlayBand {
    float minX = 0.f, minY = 0.f, maxX = 0.f, maxY = 0.f;
    bool  valid = false;
};
void setOverlayBand(float minX, float minY, float maxX, float maxY);
const OverlayBand& overlayBand();

// "Reset layout" (title_bar.cpp -> st.resetLayout -> app.cpp rebuild branch) must also restore the
// pinned columns' default width/collapse state, or the menu item breaks its promise. app.cpp calls
// resetOverlayColumns() in the rebuild branch; each column consumes its one-shot flag on its next
// draw. Slots: 0 = FeatureManager column (drawLeftPane), 1 = Copilot column (legacy_chat.cpp).
void resetOverlayColumns();
bool takeOverlayColumnReset(int slot);

// ---- pinned-column occlusion edges ---------------------------------------------------------------
// Viewport HUD elements (orientation triad, model-info pill, result legend) must not draw UNDER the
// pinned glass columns. Each column publishes its inner edge every frame it draws (slot 0 =
// FeatureManager column's RIGHT edge, slot 1 = Copilot column's LEFT edge); overlayColumnEdge()
// returns that edge, or a negative value when the column didn't draw this/last frame (hidden or
// collapsed to the small pill — HUD may then use the full viewport width).
void publishOverlayColumnEdge(int slot, float edgeX);
float overlayColumnEdge(int slot);

// ---- doc-tab band bottom edge --------------------------------------------------------------------
// The doc-tab strip draws inside the central document window, so its glass band actually ends a few
// px BELOW band.minY + docTabStripHeight (the central window's content origin sits under the node
// top). drawDocTabs publishes the band's real bottom (screen y) every frame it draws; the pinned
// columns anchor below max(nominal strip height, this edge) so the band never overlaps their top
// corners. Returns a negative value when the strip didn't draw this/last frame.
void publishDocTabBandBottom(float y);
float docTabBandBottom();

// Draw the tabbed SolidWorks-style left manager as a pinned full-height rounded glass column over
// the viewport's left edge (~12dp margins, below the doc-tab band). Owns the horizontal icon-tab
// row + the active tab's body, the PM auto-switch (open-edge -> PropertyManager tab, close-edge
// -> FeatureManager tab), the vertical collapse-to-header-strip affordance (T-G21: the column
// rolls UP to its top tab row + collapse chevron instead of shrinking to a small pill; see
// ui::PanelCollapseAnim in panel_collapse.h — shared with the Copilot column), and the invisible
// right-edge resize. editCallback is forwarded to the FeatureManager body (a tree "Edit" reopens a
// PM form). Call every frame in place of the old drawFeatureTree/PM block.
void drawLeftPane(AppState& st,
                  const std::function<void(const std::string& featureId)>& editCallback = {});

// Configurations tab body (no Begin/End). The engine has no per-part config schema concept beyond
// the implicit default, so this renders the active "Default [Active]" config + a short note. Reuses
// nothing from ConfigPanel (that dockable window stays for the right task pane).
void drawConfigurationsBody(AppState& st);

// Default first-open placement for the floating command dialogs (Evaluate / assembly / Cut List /
// Design Table windows): with the docked rightBottom slot gone they open as free floaters, so this
// anchors the NEXT window's top-right corner just left of where the Copilot column pins, 12dp
// inside the overlay band (ImGuiCond_FirstUseEver — a user's later placement persists in the ini).
// Call immediately before ImGui::Begin(); no-op fallback (ImGui default cascade) pre-band-publish.
void setNextCommandDialogPlacement();

}  // namespace ui
