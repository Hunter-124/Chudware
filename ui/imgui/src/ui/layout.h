// AI-CAD2 dock layout — default dock arrangement + glass dock chrome (S1).
//
// buildDefaultLayout() owns the dock-slot names + split ratios that "Reset layout" will rebuild
// (host parity: host/src/ui/layout.cpp). drawDockSplitters() draws 1px palette.line splitters
// over transparent ImGui docking chrome (GLASS_DESIGN_SPEC §6 S1, §7 G4).
#pragma once

struct ImVec2;
typedef unsigned int ImGuiID;

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

}  // namespace ui
