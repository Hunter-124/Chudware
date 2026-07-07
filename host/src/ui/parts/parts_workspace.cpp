#include "parts_workspace.h"

#include "core/app_state.h"
#include "imgui.h"

namespace ui {

// Placeholder body — replaced by the parts-port agent with the real catalog browser,
// vendor login pane, and download queue (ported from the retired ui/imgui tree).
void drawPartsLibraryWorkspace(AppState&) {
    ImGui::Dummy(ImVec2(0, 8));
    ImGui::TextDisabled("Parts Library workspace is being brought online.");
}

}  // namespace ui
