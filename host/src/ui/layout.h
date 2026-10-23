// Chudware overlay layout + the pinned FeatureManager glass column.
//
// Overlay redesign: the dockspace hosts ONLY the central document windows (Viewport / ECAD,
// tabbed) so the 3D surface runs edge to edge between the ribbon and the status bar. Manufacture
// renders geometric previews through the native viewport and keeps Explorer, Job Manager, and NC
// Output in the pinned left column. The old SolidWorks docked-columns arrangement (left task pane /
// right task pane / dialogs slot) is GONE — buildDefaultLayout() builds a single splitless central
// node, while FeatureManager and Copilot render as pinned glass columns over its left and right
// edges.
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

// Left-pane tab indices match AppState::LeftPaneTab (LP_*) values 0..4.
// Retired rail tab 5 redirects to Explorer; Graph's jump-only tab retains 6.
enum {
    kLeftPaneExplorer        = 0,
    kLeftPaneFeatureManager  = 1,
    kLeftPanePropertyManager = 2,
    kLeftPaneConfigurations  = 3,
    kLeftPaneSimulation      = 4,
};
constexpr int kLeftPaneGraph = 6;

// ---- overlay band ------------------------------------------------------------------------------
// The screen-space rect between the ribbon (bottom edge of the chrome) and the status bar — the
// region the full-width viewport surface fills and the pinned glass columns overlay. app.cpp
// publishes it once per frame (setOverlayBand, right where dockTopY/statusH are computed) before
// the panels draw; consumers (drawLeftPane here, the Copilot column in ui/copilot/) read it
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
// draw. Slots: 0 = FeatureManager column (drawLeftPane), 1 = Copilot column (ui/copilot/).
// Slot 0 restores BOTH left-column widths: the shared FeatureManager column and the Simulate
// workspace's own Simulation Manager column (default metrics::simManagerPaneW), even if the
// latter is not on screen when Reset is chosen.
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

// Publish/read the actual rendered height (screen pixels) of a pinned overlay column this frame.
// drawLeftPane calls publishOverlayColumnHeight(0, heightPx) with the true column height each
// frame it draws; viewport_panel.cpp reads overlayColumnHeight(0) to decide the triad anchor:
// when the column is short/collapsed and does not reach down to the triad row, the triad anchors
// at the far-left of the viewport; when the column is tall enough to reach it, it anchors to the
// right of the column's edge (the existing hudLeft behaviour). Returns -1 when not published.
void publishOverlayColumnHeight(int slot, float heightPx);
float overlayColumnHeight(int slot);

// ---- doc-tab band bottom edge --------------------------------------------------------------------
// The doc-tab strip draws inside the central document window, so its glass band actually ends a few
// px BELOW band.minY + docTabStripHeight (the central window's content origin sits under the node
// top). drawDocTabs publishes the band's real bottom (screen y) every frame it draws; the pinned
// columns anchor below max(nominal strip height, this edge) so the band never overlaps their top
// corners. Returns a negative value when the strip didn't draw this/last frame.
void publishDocTabBandBottom(float y);
float docTabBandBottom();

// ---- pinned-column resize boundary ---------------------------------------------------------------
// ONE resize affordance for both pinned columns (FeatureManager's RIGHT edge, Copilot's LEFT edge):
// a hairline boundary that brightens on hover, an always-faint grip pill at its vertical middle, a
// soft accent glow along the edge on hover/drag (fading out top and bottom), the EW resize cursor,
// and double-click to reset. Call inside the column window right after Begin(). The hit strip
// covers the window-padding gutter along that edge and is an overlap-permissive item, so any widget
// later submitted over it (and any child window) keeps its hover and clicks.
// `currentWidth` is the column's width this frame; `interactive` false (fixed-width columns) paints
// nothing and never hit-tests.
struct ColumnResizeResult {
    float width = 0.f;             // currentWidth, or drag-start width +/- drag distance (UNclamped)
    bool  active = false;          // a drag is in progress
    bool  resetRequested = false;  // double-clicked: caller restores its default width
};
ColumnResizeResult columnResizeEdge(const char* strId, bool rightEdge, float currentWidth,
                                    bool interactive = true);

// Draw the tabbed SolidWorks-style left manager as a pinned full-height rounded glass column over
// the viewport's left edge (~12dp margins, below the doc-tab band). Owns the horizontal icon-tab
// row + the active tab's body, the PM auto-switch (open-edge -> PropertyManager tab, close-edge
// -> FeatureManager tab), the vertical collapse-to-header-strip affordance (T-G21: the column
// rolls UP to its top tab row + collapse chevron instead of shrinking to a small pill; see
// ui::PanelCollapseAnim in panel_collapse.h — shared with the Copilot column), and the right-edge
// resize boundary (columnResizeEdge). editCallback is forwarded to the FeatureManager body (a tree
// "Edit" reopens a PM form). Call every frame in place of the old drawFeatureTree/PM block. In
// Simulate the column is a separate window ("SimulationManager##LeftPane") with its own remembered
// width and the wider metrics::simManagerPaneW default; every other workspace shares
// "FeatureManager##LeftPane".
void drawLeftPane(AppState& st,
                  const std::function<void(const std::string& featureId)>& editCallback = {});

// Configurations tab body (no Begin/End). The engine has no per-part config schema concept beyond
// the implicit default, so this renders the active "Default [Active]" config + a short note. Reuses
// nothing from ConfigPanel (that dockable window stays for the right task pane).
void drawConfigurationsBody(AppState& st);

// Floating command dialogs keep user geometry; callers provide metric-scaled pixel sizes.
// Call immediately before Begin() with its exact window name. Constraints and recovery clamp
// remembered geometry to the current work/overlay band (also after viewport or DPI changes).
// Published column edges guide only initial placement; they never prevent dragging/resizing.
void setNextCommandDialogPlacement(const char* windowName, ImVec2 preferredSizePx,
                                   ImVec2 minimumSizePx);

// Evaluate-tab ribbon "dead space" (screen px) to the RIGHT of the last Evaluate group. The
// Zebra / Symmetry inspector windows dock INTO this strip while the Evaluate tab is active
// (they must pin inside the ribbon bar, not float over the viewport). The ribbon
// publishes it every frame it draws the Evaluate tab; evalRibbonDock().valid is false otherwise
// (frame-stamped, like the overlay-column edges) so a tab switch drops the dock next frame.
struct EvalRibbonDock { float minX = 0, minY = 0, maxX = 0, maxY = 0; bool valid = false; };
void setEvalRibbonDock(float minX, float minY, float maxX, float maxY);
EvalRibbonDock evalRibbonDock();

}  // namespace ui
