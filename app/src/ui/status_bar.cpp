#include "ui/panels.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "imgui.h"
#include <cstdio>

namespace ui {

using namespace app;

void drawStatusBar(AppState& s, float height) {
    const float S = theme::S();
    ImGui::PushStyleColor(ImGuiCol_ChildBg, TVEC(bg0));
    ImGui::BeginChild("##status", ImVec2(0, height), false,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 wp = ImGui::GetWindowPos(), wsz = ImGui::GetWindowSize();
    dl->AddLine(wp, ImVec2(wp.x+wsz.x, wp.y), TCOL(line), 1.f);

    float cy = (height - ImGui::GetTextLineHeight())*0.5f;
    ImGui::SetCursorPos(ImVec2(12*S, cy));
    // status dot
    ImVec2 dp = ImGui::GetCursorScreenPos();
    dl->AddCircleFilled(ImVec2(dp.x+4*S, dp.y+ImGui::GetTextLineHeight()*0.5f), 4*S,
                        s.agent.running ? TCOL(warn) : TCOL(good));
    ImGui::SetCursorPosX(28*S);
    ImGui::TextColored(TVEC(txt1), "%s", s.status.c_str());

    // right cluster
    char right[128];
    snprintf(right, sizeof(right), "%d selected   |   units: %s", s.selCount, s.units.c_str());
    ImVec2 ts = ImGui::CalcTextSize(right);
    ImGui::SameLine();
    ImGui::SetCursorPosX(wsz.x - ts.x - 14*S);
    ImGui::TextColored(TVEC(txt2), "%s", right);

    ImGui::EndChild();
    ImGui::PopStyleColor();
}

} // namespace ui
