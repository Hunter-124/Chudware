// T-DP15: full-viewport workspace shell panels.
// Placeholder shells for AiAgent and PlmManagement workspaces.
// Bodies land in T-DP16 (AiAgent) and the R-stack (PlmManagement).
#include "ui/workspace_shells.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "imgui.h"

namespace ui {

using app::AppState;

// Draw a centered placeholder inside the currently-active ImGui window.
// Used by both shell panels to signal "coming in T-DP16 / R-stack".
static void drawShellPlaceholder(const char* title, const char* subtitle) {
    const float S = theme::S();
    ImVec2 avail = ImGui::GetContentRegionAvail();
    // Center vertically and horizontally
    ImFont* titleFont = theme::font(theme::Font::H2);
    ImFont* subFont   = theme::font(theme::Font::Body);
    ImVec2 tSz = titleFont->CalcTextSizeA(titleFont->FontSize, FLT_MAX, 0, title);
    ImVec2 sSz = subFont->CalcTextSizeA(subFont->FontSize, FLT_MAX, 0, subtitle);
    float blockH = tSz.y + 8*S + sSz.y;
    float ox = (avail.x - tSz.x) * 0.5f;
    float oy = (avail.y - blockH) * 0.5f;
    if (ox < 0) ox = 0;
    if (oy < 0) oy = 0;
    ImGui::SetCursorPos(ImVec2(ImGui::GetCursorPos().x + ox, ImGui::GetCursorPos().y + oy));
    ImGui::PushFont(titleFont);
    ImGui::TextUnformatted(title);
    ImGui::PopFont();
    ImGui::SetCursorPosX(ImGui::GetCursorPos().x + (avail.x - sSz.x) * 0.5f);
    ImGui::PushFont(subFont);
    ImGui::PushStyleColor(ImGuiCol_Text, TCOL(txt1));
    ImGui::TextUnformatted(subtitle);
    ImGui::PopStyleColor();
    ImGui::PopFont();
}

void drawAiAgentShell(AppState& /*s*/) {
    // T-DP15: AI-Agent full-viewport shell — body in T-DP16.
    drawShellPlaceholder("AI Agent", "Full-screen agent workspace — body arrives in T-DP16");
}

void drawPlmShell(AppState& /*s*/) {
    // T-DP15: PLM project-hub full-viewport shell — body in R-stack.
    drawShellPlaceholder("PLM", "Project management hub — body arrives in the R-stack");
}

} // namespace ui
