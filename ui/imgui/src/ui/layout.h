// AI-CAD2 dock layout — default dock arrangement + glass dock chrome (S1).
//
// buildDefaultLayout() owns the dock-slot names + split ratios that "Reset layout" will rebuild
// (host parity: host/src/ui/layout.cpp). drawDockSplitters() draws 1px palette.line splitters
// over transparent ImGui docking chrome (GLASS_DESIGN_SPEC §6 S1, §7 G4).
#pragma once

struct ImVec2;
typedef unsigned int ImGuiID;

namespace app { struct AppState; }

// Global on purpose — matches host forward-decl pattern for reset-layout wiring in G2.
void buildDefaultLayout(ImGuiID root, const ImVec2& size);

namespace ui {

// Left task-pane tab indices — numerically aligned with host AppState::LeftPaneTab (LP_*).
enum {
    kLeftPaneExplorer        = 0,
    kLeftPaneFeatureManager  = 1,
    kLeftPanePropertyManager = 2,
    kLeftPaneConfigurations  = 3,
    kLeftPaneSimulation      = 4,
};

// Call every frame immediately after DockSpace() inside the dock host window.
void drawDockSplitters(ImGuiID dockRootId);

// ---- T-G23 overlay band (chrome parity with host/src/ui/layout.{h,cpp}) ------------------------
// The screen-space rect between the ribbon (bottom edge of the chrome) and the status bar — the
// region the full-width viewport surface fills and the pinned glass columns overlay. application.cpp
// publishes it once per frame (setOverlayBand) before the columns draw; the pinned columns
// (drawLeftPane / drawRightPane) read it via overlayBand() to anchor their geometry.
struct OverlayBand {
    float minX = 0.f, minY = 0.f, maxX = 0.f, maxY = 0.f;
    bool  valid = false;
};
void setOverlayBand(float minX, float minY, float maxX, float maxY);
const OverlayBand& overlayBand();

// Pinned-column inner edges — viewport HUD dodges under the columns. slot 0 = left FM column's
// RIGHT edge, slot 1 = right Copilot column's LEFT edge. Returns a negative value when the column
// didn't draw this/last frame (frame-stamped).
void publishOverlayColumnEdge(int slot, float edgeX);
float overlayColumnEdge(int slot);

// Floating command dialogs (Evaluate / assembly / Cut List …) lost their docked rightBottom slot
// with the overlay layout; anchor the NEXT window's top-right corner just left of where the Copilot
// column pins, inset in the overlay band (ImGuiCond_FirstUseEver). Call immediately before Begin().
void setNextCommandDialogPlacement();

// Pinned full-height rounded glass FeatureManager column over the viewport's LEFT edge — hosts the
// vertical icon rail (Model Tree / Bodies / Files / Configurations) + the active tab body + the
// PropertyManager form (drawInspector) with a collapse-to-pill affordance. Replaces the old docked
// "Browser###left" window. Call every frame.
void drawLeftPane(app::AppState& s);

// Pinned full-height rounded glass Copilot column over the viewport's RIGHT edge — hosts the agent
// timeline (drawAgentPanel) with a collapse-to-pill affordance. Replaces the old docked
// "Assistant###agent" window. Call every frame.
void drawRightPane(app::AppState& s);

}  // namespace ui
