// T-DP15: full-viewport workspace shell panels.
// Placeholder shells for AiAgent and PlmManagement workspaces.
// Bodies land in T-DP16 (AiAgent) and the R-stack (PlmManagement).
#include "ui/workspace_shells.h"
#include "ui/agent_dag_panel.h"   // T-DP16: mission-DAG centerpiece
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

void drawAiAgentShell(AppState& s) {
    // T-DP16 (first slice): AI-Agent full-viewport workspace body.
    //
    // The full-viewport takeover itself is inherited from T-DP15: workspaceKind()
    // marks AiAgent as FullViewport, application.cpp suppresses the pinned glass
    // overlay columns and hands this shell the whole content area (the same
    // mechanism that swaps the viewport for the ECAD schematic canvas). Switching
    // out of the workspace returns to Model with the glass copilot overlay intact.
    //
    // This slice embeds the existing mission-DAG panel as the centerpiece. The
    // panel draws its own glass PanelScaffold header ("Agent DAG") + status line
    // and handles the empty state, so we add only a thin workspace-title strip
    // above it (no glass-on-glass wrapping) and hand it the remaining area.
    const float S = theme::S();

    // Thin workspace-title strip — identifies the takeover; consistent glass text.
    ImGui::PushFont(theme::font(theme::Font::H2));
    ImGui::TextUnformatted("AI Agent");
    ImGui::PopFont();
    ImGui::PushFont(theme::font(theme::Font::Body));
    ImGui::PushStyleColor(ImGuiCol_Text, TCOL(txt1));
    ImGui::TextUnformatted("Mission workspace - live design-pipeline DAG");
    ImGui::PopStyleColor();
    ImGui::PopFont();
    ImGui::Dummy(ImVec2(0, 8 * S));

    // Centerpiece: the mission DAG (Full mode = graph + node detail pane).
    ui::drawAgentDagPanel(s, aicad::ui::DagWidgetMode::Full);

    // ---- DEFERRED to follow-up T-DP16 slices (do NOT build here) -------------
    // TODO(T-DP16 slice 2): worker steering rail — pause/resume/kill any agent or
    //   sub-agent, change a role's model/effort live, approve/deny pending
    //   sub-agent spawns (ties to T-DP9 delegation seams).
    // TODO(T-DP16 slice 3): session management — list/switch/fork/resume sessions
    //   (embeds the multi-file view T-DP13).
    // TODO(T-DP16 slice 4): transparency stream embed (T-DP11).
}

void drawPlmShell(AppState& /*s*/) {
    // T-DP15: PLM project-hub full-viewport shell — body in R-stack.
    drawShellPlaceholder("PLM", "Project management hub — body arrives in the R-stack");
}

} // namespace ui
