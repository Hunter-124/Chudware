// AI-CAD status bar — state + live agent phase (left), provider/model summary (right).
#include <cmath>
#include <string>

#include "imgui.h"

#include "status_bar.h"

#include "../core/app_state.h"

namespace ui {

void drawStatusBar(AppState& st, ImGuiViewport* vp, float statusH, bool busy,
                   ImGuiWindowFlags hostFlags) {
    // ---------- status bar (state + live agent phase left, provider/model right) ----------
    ImGui::SetNextWindowPos(ImVec2(vp->Pos.x, vp->Pos.y + vp->Size.y - statusH));
    ImGui::SetNextWindowSize(ImVec2(vp->Size.x, statusH));
    ImGui::SetNextWindowViewport(vp->ID);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, 0);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(14, 5));
    const auto& pal = ui::palette;
    ImGui::PushStyleColor(ImGuiCol_WindowBg,
        ImVec4(pal.statusBg[0], pal.statusBg[1], pal.statusBg[2], pal.statusBg[3]));
    ImGui::Begin("##StatusBar", nullptr, hostFlags);
    ImVec4 sc = st.statusKind == 2
              ? ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1)
              : st.statusKind == 1
              ? ImVec4(pal.statusWarn[0], pal.statusWarn[1], pal.statusWarn[2], 1)
              : ImVec4(pal.statusOk[0], pal.statusOk[1], pal.statusOk[2], 1);
    ImVec2 dp0 = ImGui::GetCursorScreenPos();
    float dr = ImGui::GetFontSize() * 0.24f;
    if (busy)  // pulse while a job runs
        sc.w = 0.45f + 0.55f * (0.5f + 0.5f * std::sin((float)ImGui::GetTime() * 5.0f));
    ImGui::GetWindowDrawList()->AddCircleFilled(
        ImVec2(dp0.x + dr + 1, dp0.y + ImGui::GetTextLineHeight() * 0.55f), dr,
        ImGui::ColorConvertFloat4ToU32(sc));
    sc.w = 1.0f;
    ImGui::Dummy(ImVec2(dr * 2 + 8, 0));
    ImGui::SameLine();
    ImGui::TextColored(sc, "%s", st.status.c_str());
    if (busy && !st.livePhase.empty()) {
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.consoleDim[0], pal.consoleDim[1], pal.consoleDim[2], 1));
        ImGui::Text("|  %s", st.livePhase.c_str());
        ImGui::PopStyleColor();
    }
    // provider / model summary, right-aligned; click to open Settings
    const char* provName = st.settings.provider == 0 ? "Anthropic"
                         : st.settings.provider == 1 ? "OpenAI" : "Local";
    std::string ptxt = std::string(provName) + "  /  " +
                       (st.settings.model[0] ? st.settings.model : "default") +
                       (st.settings.planMode ? "   [plan mode]" : "");
    float ptw = ImGui::CalcTextSize(ptxt.c_str()).x;
    ImGui::SameLine(ImGui::GetWindowWidth() - ptw - 16);
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(pal.consoleDim[0], pal.consoleDim[1], pal.consoleDim[2], 1));
    ImGui::TextUnformatted(ptxt.c_str());
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) {
        ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        ImGui::SetTooltip("AI provider & agent settings");
    }
    if (ImGui::IsItemClicked()) st.openSettings = true;
    ImGui::End();
    ImGui::PopStyleColor();
    ImGui::PopStyleVar(3);
}

}  // namespace ui
