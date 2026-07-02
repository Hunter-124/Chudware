// AI-CAD Console panel — runtime/agent log, color-coded by [tag].
#include "console_panel.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "imgui.h"
#include "imgui_internal.h"

#include <string>
#include <vector>

namespace ui {

namespace {
std::vector<std::string> gLog;
}  // namespace

void appendConsoleLog(const std::string& line) {
    gLog.push_back(line);
    if (gLog.size() > 4000) gLog.erase(gLog.begin(), gLog.begin() + 500);
}

void clearConsoleLog() { gLog.clear(); }

void drawConsoleBody(app::AppState& /*st*/) {
    const float S = theme::S();
    // Console tab shares the Copilot pane's single glass surface (contract §6/A1, id
    // "agent_glass" — painted once by agent_panel.cpp's drawAgentPanel; adaptivePanel is
    // idempotent per frame, so this refetch is a free cache hit).
    ImVec2 wmin = ImGui::GetWindowPos();
    ImVec2 wmax(wmin.x + ImGui::GetWindowSize().x, wmin.y + ImGui::GetWindowSize().y);
    const theme::AdaptiveSurface& surf =
        theme::adaptivePanel(theme::panelId("agent_glass"), ImRect(wmin, wmax));
    ImGui::PushFont(theme::font(theme::Font::Mono));
    theme::pushGlassChildStyle(6.f * S);
    ImGui::BeginChild("##log", ImVec2(0, 0), ImGuiChildFlags_Borders);
    for (const auto& line : gLog) {
        ImVec4 col = surf.textPrimary;
        if (line.rfind("[err]", 0) == 0 || line.rfind("[!]", 0) == 0)
            col = TVEC(bad);
        else if (line.rfind("[cad]", 0) == 0)
            col = TVEC(warn);
        else if (line.rfind("[ok]", 0) == 0)
            col = TVEC(good);
        else if (line.rfind("[..]", 0) == 0)
            col = TVEC(txt2);
        ImGui::TextColored(col, "%s", line.c_str());
    }
    if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 4.f)
        ImGui::SetScrollHereY(1.f);
    ImGui::EndChild();
    theme::popGlassChildStyle();
    ImGui::PopFont();
}

}  // namespace ui
