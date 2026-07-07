#include "plm_workspace.h"

#include "imgui.h"

namespace ui {

// Placeholder body — replaced by the parts-port agent with the real vault status,
// checkpoint list, and history timeline over services/plm.
void drawPlmWorkspace(app::AppState&) {
    ImGui::Dummy(ImVec2(0, 8));
    ImGui::TextDisabled("PLM workspace is being brought online.");
}

}  // namespace ui
