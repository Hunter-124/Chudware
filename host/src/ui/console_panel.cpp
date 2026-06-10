// AI-CAD Console panel — the runtime/agent log, color-coded by [tag].
#include "imgui.h"

#include "console_panel.h"

#include "../core/app_state.h"

namespace ui {

void drawConsolePanel(AppState& st) {
    // ---------- Console ----------
    if (st.showConsole) {
    if (ImGui::Begin("Console"))
    {
        ImGui::PushFont(st.fonts.mono);
        ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0.07f, 0.08f, 0.10f, 1));
        ImGui::BeginChild("##log", ImVec2(0, 0), ImGuiChildFlags_None);
        for (auto& line : st.log) {
            ImVec4 col(0.78f, 0.82f, 0.88f, 1);
            if (line.rfind("[err]", 0) == 0 || line.rfind("[!]", 0) == 0)
                col = ImVec4(0.96f, 0.55f, 0.5f, 1);
            else if (line.rfind("[cad]", 0) == 0)
                col = ImVec4(0.95f, 0.76f, 0.42f, 1);  // build issue, not an app error
            else if (line.rfind("[ok]", 0) == 0)
                col = ImVec4(ui::palette.accent[0], ui::palette.accent[1], ui::palette.accent[2], 1);
            else if (line.rfind("[..]", 0) == 0)
                col = ImVec4(0.62f, 0.66f, 0.72f, 1);
            ImGui::TextColored(col, "%s", line.c_str());
        }
        if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 4) ImGui::SetScrollHereY(1.0f);
        ImGui::EndChild();
        ImGui::PopStyleColor();
        ImGui::PopFont();
    }
    ImGui::End();
    }
}

}  // namespace ui
