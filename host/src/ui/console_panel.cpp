// Chudware Console panel — the runtime/agent log, color-coded by [tag].
#include "imgui.h"

#include <algorithm>
#include <cmath>
#include <limits>

#include "console_panel.h"

#include "../core/app_state.h"
#include "../ui_theme.h"

namespace ui {

// Body only (no Begin/End) — rendered inside the consolidated AI panel's Console tab.
void drawConsoleBody(AppState& st) {
        const auto& pal = ui::palette;
        ImGui::PushFont(st.fonts.mono);
        ImGui::PushStyleColor(ImGuiCol_ChildBg,
            ImVec4(pal.codeBg[0], pal.codeBg[1], pal.codeBg[2], pal.codeBg[3]));
        ImGui::BeginChild("##log", ImVec2(0, 0), ImGuiChildFlags_None,
                          ImGuiWindowFlags_NoScrollbar);
        {
            std::lock_guard<std::mutex> lk(st.logMtx);
            // Messages may contain multiple lines. Cache their exact heights on mutation,
            // then let the clipper skip pixel-height units rather than assuming equal rows.
            static AppState* cachedOwner = nullptr;
            static uint64_t cachedGeneration = std::numeric_limits<uint64_t>::max();
            static float cachedFontSize = 0, cachedSpacing = 0;
            static ImFont* cachedFont = nullptr;
            static std::array<float, AppState::kLogCapacity + 1> offsets{};
            const float fontSize = ImGui::GetFontSize();
            const float spacing = ImGui::GetStyle().ItemSpacing.y;
            if (cachedOwner != &st || cachedGeneration != st.logGeneration ||
                cachedFont != ImGui::GetFont() || cachedFontSize != fontSize || cachedSpacing != spacing) {
                offsets[0] = 0;
                for (size_t i = 0; i < st.logSize; ++i) {
                    const size_t slot = (st.logHead + i) % AppState::kLogCapacity;
                    offsets[i + 1] = offsets[i] + fontSize * st.logLineCounts[slot] + spacing;
                }
                cachedOwner = &st;
                cachedGeneration = st.logGeneration;
                cachedFontSize = fontSize;
                cachedSpacing = spacing;
                cachedFont = ImGui::GetFont();
            }
            const float startY = ImGui::GetCursorPosY();
            const float height = offsets[st.logSize];
            if (height > 0) {
                const int units = (int)std::min(double(std::numeric_limits<int>::max()),
                                               std::ceil(double(height)));
                const float unitHeight = height / units;
                ImGuiListClipper clipper;
                clipper.Begin(units, unitHeight);
                size_t nextLine = 0;
                while (clipper.Step()) {
                    const float firstY = clipper.DisplayStart * unitHeight;
                    const float lastY = clipper.DisplayEnd * unitHeight;
                    auto first = std::upper_bound(offsets.begin(),
                        offsets.begin() + st.logSize + 1, firstY);
                    size_t i = first == offsets.begin() ? 0 : size_t(first - offsets.begin() - 1);
                    i = std::max(i, nextLine);
                    for (; i < st.logSize && offsets[i] < lastY; ++i) {
                        const auto& line = st.log[(st.logHead + i) % AppState::kLogCapacity];
                        ImGui::SetCursorPosY(startY + offsets[i]);
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
                    nextLine = i;
                }
            }
        }
        if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 4) ImGui::SetScrollHereY(1.0f);
        // App-wide accent pill bar replaces the default grey one (child carries NoScrollbar);
        // "host_agent_glass" is the AI panel's ONE shared surface — adaptivePanel is idempotent
        // per frame, so this is a cached lookup.
        {
            ImVec2 mn = ImGui::GetWindowPos();
            ImVec2 mx(mn.x + ImGui::GetWindowSize().x, mn.y + ImGui::GetWindowSize().y);
            const ui::AdaptiveSurface& surf = ui::adaptivePanel(
                ui::panelId("host_agent_glass"), ImRect(mn, mx), ui::GlassGroup::Copilot);
            ui::drawWindowPillScrollbarV(surf, ui::kChatScrollbar);
        }
        ImGui::EndChild();
        ImGui::PopStyleColor();
        ImGui::PopFont();
}

}  // namespace ui
