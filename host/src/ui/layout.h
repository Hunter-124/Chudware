// AI-CAD dock layout + SolidWorks-faithful tabbed left pane (Wave 1C + 1D).
//
// 1C: owns buildDefaultLayout() — the dock-slot names + split ratios that "Reset layout" rebuilds.
//     Moved verbatim out of title_bar.cpp (it was never a title-bar concern). The function stays in
//     the GLOBAL namespace because app_state.h forward-declares it there and app.cpp calls it
//     unqualified — keeping it global avoids touching either site.
//
// 1D: ui::drawLeftPane() is the SolidWorks left task pane: a vertical icon-tab strip (Explorer /
//     FeatureManager / PropertyManager / Configurations) docked into the single "LeftPane" window,
//     with one tab body shown at a time. It routes to the existing panel bodies (feature tree, PM
//     forms, configurations) so no panel logic is duplicated.
#pragma once

#include <functional>
#include <string>

struct AppState;

// 1C: build the default dock arrangement (Reset layout rebuilds exactly this). GLOBAL on purpose —
// see app_state.h's forward declaration and the unqualified call in app.cpp. The body lives in
// layout.cpp now (was title_bar.cpp).
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
};

// Draw the tabbed SolidWorks left task pane into the "LeftPane" dock window. Owns the vertical
// icon-tab strip + the active tab's body, and the PM auto-switch (open-edge -> PropertyManager tab,
// close-edge -> FeatureManager tab). editCallback is forwarded to the FeatureManager body (a tree
// "Edit" reopens a PM form). Call every frame in place of the old drawFeatureTree/PM block.
void drawLeftPane(AppState& st,
                  const std::function<void(const std::string& featureId)>& editCallback = {});

// Configurations tab body (no Begin/End). The engine has no per-part config schema concept beyond
// the implicit default, so this renders the active "Default [Active]" config + a short note. Reuses
// nothing from ConfigPanel (that dockable window stays for the right task pane).
void drawConfigurationsBody(AppState& st);

}  // namespace ui
