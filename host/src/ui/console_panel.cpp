// AI-CAD Console panel — the runtime/agent log, color-coded by [tag].
#include "imgui.h"

#include "console_panel.h"

#include "../core/app_state.h"

namespace ui {

// Body only (no Begin/End) — used both by the standalone Console window and the consolidated
// AI panel's Console tab.
void drawConsoleBody(AppState& st) {
        const auto& pal = ui::palette;
        ImGui::PushFont(st.fonts.mono);
        ImGui::PushStyleColor(ImGuiCol_ChildBg,
            ImVec4(pal.codeBg[0], pal.codeBg[1], pal.codeBg[2], pal.codeBg[3]));
        ImGui::BeginChild("##log", ImVec2(0, 0), ImGuiChildFlags_None);
        for (auto& line : st.log) {
            ImVec4 col(pal.consoleDefault[0], pal.consoleDefault[1],
                       pal.consoleDefault[2], 1);
            if (line.rfind("[err]", 0) == 0 || line.rfind("[!]", 0) == 0)
                col = ImVec4(pal.consoleErr[0], pal.consoleErr[1], pal.consoleErr[2], 1);
            else if (line.rfind("[cad]", 0) == 0)
                col = ImVec4(pal.consoleWarn[0], pal.consoleWarn[1], pal.consoleWarn[2], 1);
            else if (line.rfind("[ok]", 0) == 0)
                col = ImVec4(pal.consoleOk[0], pal.consoleOk[1], pal.consoleOk[2], 1);
            else if (line.rfind("[..]", 0) == 0)
                col = ImVec4(pal.consoleDim[0], pal.consoleDim[1], pal.consoleDim[2], 1);
            ImGui::TextColored(col, "%s", line.c_str());
        }
        if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 4) ImGui::SetScrollHereY(1.0f);
        ImGui::EndChild();
        ImGui::PopStyleColor();
        ImGui::PopFont();
}

void drawConsolePanel(AppState& st) {
    if (!st.showConsole) return;
    if (ImGui::Begin("Console")) drawConsoleBody(st);
    ImGui::End();
}

}  // namespace ui
