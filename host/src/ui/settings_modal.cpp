// AI-CAD Settings modal (AI provider + Agent tabs) and the About modal.
#include <algorithm>
#include <cstring>
#include <string>

#include <nlohmann/json.hpp>

#include "imgui.h"

#include "settings_modal.h"

#include "../core/app_state.h"
#include "../renderer.h"

namespace ui {

void drawSettingsModal(AppState& st, bool busy) {
    Settings& settings = st.settings;

    // ---------- Settings modal ----------
    if (st.openSettings) {
        ImGui::OpenPopup("Settings");
        st.openSettings = false;
    }
    ImGui::SetNextWindowSize(ImVec2(580, 640), ImGuiCond_Appearing);
    ImGui::SetNextWindowPos(ImGui::GetMainViewport()->GetCenter(), ImGuiCond_Appearing,
                            ImVec2(0.5f, 0.5f));
    if (ImGui::BeginPopupModal("Settings", nullptr, ImGuiWindowFlags_NoResize)) {
        // ---- theme row (before the tab body) ----
        {
            bool isLight = (ui::activeTheme == ui::Theme::Light);
            if (ImGui::Checkbox("Light theme (SolidWorks-style)", &isLight)) {
                ui::Theme next = isLight ? ui::Theme::Light : ui::Theme::Dark;
                ui::applyTheme(next, 1.0f);  // scale unknown here; 1.0 is fine post-init
                st.settings.theme = (next == ui::Theme::Light) ? "light" : "dark";
                // Viewport background sync is handled by View menu or next applyTheme call;
                // do it here too if renderer is accessible.
                if (st.renderer)
                    st.renderer->setBackground(
                        ui::palette.viewportTop[0], ui::palette.viewportTop[1],
                        ui::palette.viewportTop[2], ui::palette.viewportBottom[0],
                        ui::palette.viewportBottom[1], ui::palette.viewportBottom[2]);
            }
            ImGui::SameLine();
            ImGui::PushStyleColor(ImGuiCol_Text,
                                  ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                                         ui::palette.textDim[2], 1));
            ImGui::TextUnformatted("(saved immediately)");
            ImGui::PopStyleColor();
        }
        ImGui::Separator();
        ImGui::Dummy(ImVec2(0, 2));

        // tab content scrolls in its own region; Save/Cancel stay pinned at the bottom
        ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
        ImGui::BeginChild("##settabsarea",
                          ImVec2(0, ImGui::GetContentRegionAvail().y - 52));
        if (ImGui::BeginTabBar("##settabs")) {
            if (ImGui::BeginTabItem("AI provider")) {
                ImGui::Dummy(ImVec2(0, 2));
                dimText("Used by 'Design with AI' and chat. Stored in "
                        "%APPDATA%\\AI-CAD\\config.json.");
                ImGui::Dummy(ImVec2(0, 4));
                const char* provs[] = {"Anthropic (Claude)", "OpenAI",
                                       "Local (OpenAI-compatible)"};
                ImGui::SetNextItemWidth(-1);
                ImGui::Combo("##prov", &settings.provider, provs, 3);
                ImGui::Dummy(ImVec2(0, 4));
                ImGui::TextUnformatted("Model");
                ImGui::SetNextItemWidth(-1);
                ImGui::InputText("##model", settings.model, sizeof(settings.model));
                if (settings.provider != 2) {
                    ImGui::TextUnformatted("API key");
                    ImGui::SetNextItemWidth(-1);
                    ImGui::InputText("##key", settings.apiKey, sizeof(settings.apiKey),
                                     ImGuiInputTextFlags_Password);
                } else {
                    ImGui::TextUnformatted("Base URL");
                    ImGui::SetNextItemWidth(-1);
                    ImGui::InputText("##base", settings.baseUrl, sizeof(settings.baseUrl));
                    ImGui::TextUnformatted("API key (optional)");
                    ImGui::SetNextItemWidth(-1);
                    ImGui::InputText("##lkey", settings.apiKey, sizeof(settings.apiKey),
                                     ImGuiInputTextFlags_Password);
                    ImGui::Checkbox("Model supports vision (send screenshots)",
                                    &settings.vision);
                }
                ImGui::EndTabItem();
            }
            if (ImGui::BeginTabItem("Agent", nullptr,
                                    st.wantSettings ? ImGuiTabItemFlags_SetSelected
                                                    : ImGuiTabItemFlags_None)) {
                ImGui::Dummy(ImVec2(0, 2));
                ImGui::Checkbox("Plan before building (recommended)", &settings.planMode);
                dimText("The agent first drafts a build plan - the parts, concrete "
                        "dimensions and features - shows it in the conversation, then "
                        "writes the CAD code against it.");
                ImGui::Dummy(ImVec2(0, 4));
                ImGui::TextUnformatted("Refine-pass cap per request");
                ImGui::SetNextItemWidth(140);
                ImGui::InputInt("##miters", &settings.maxIters);
                if (settings.maxIters < 0) settings.maxIters = 0;
                if (settings.maxIters > 64) settings.maxIters = 64;
                ImGui::SameLine();
                dimText("0 = automatic (stops once the render matches)");
                ImGui::Dummy(ImVec2(0, 6));
                ImGui::Separator();
                ImGui::Dummy(ImVec2(0, 4));
                ImGui::TextUnformatted("System prompt override");
                dimText("Leave empty to use the built-in prompt (recommended). A custom "
                        "prompt fully replaces it - keep the `result`/`parts` contract "
                        "and the python+json two-block reply format or the agent breaks.");
                ImGui::PushFont(st.fonts.mono);
                ImGui::InputTextMultiline("##sysprompt", st.spBuf.data(), st.spBuf.size(),
                                          ImVec2(-1, ImGui::GetTextLineHeight() * 10));
                ImGui::PopFont();
                bool canFetch = st.workerStarted && !busy;
                if (!canFetch) ImGui::BeginDisabled();
                if (ImGui::Button("Load built-in prompt")) {
                    nlohmann::json pr;
                    std::string perr;
                    if (st.worker.call("get_prompts", nlohmann::json::object(), pr, perr,
                                    20000)) {
                        std::fill(st.spBuf.begin(), st.spBuf.end(), 0);
                        std::strncpy(st.spBuf.data(), pr.value("system_prompt", "").c_str(),
                                     st.spBuf.size() - 1);
                    } else {
                        st.addLog("[err] get_prompts: " + perr);
                    }
                }
                if (!canFetch) ImGui::EndDisabled();
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip("Copy the built-in prompt into the editor as a "
                                      "starting point");
                ImGui::SameLine();
                if (ImGui::Button("Clear (use built-in)"))
                    std::fill(st.spBuf.begin(), st.spBuf.end(), 0);
                ImGui::EndTabItem();
            }
            ImGui::EndTabBar();
        }
        ImGui::EndChild();
        ImGui::PopStyleColor();
        if (primaryButton("Save", ImVec2(120, 34))) {
            settings.systemPrompt = st.spBuf.data();
            saveSettings(settings);
            st.addLog("[ok] saved settings");
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel", ImVec2(100, 34))) {
            std::fill(st.spBuf.begin(), st.spBuf.end(), 0);  // discard edits
            std::strncpy(st.spBuf.data(), settings.systemPrompt.c_str(), st.spBuf.size() - 1);
            ImGui::CloseCurrentPopup();
        }
        ImGui::EndPopup();
    }

    // ---------- About modal ----------
    if (st.showAbout) {
        ImGui::OpenPopup("About AI-CAD");
        st.showAbout = false;
    }
    ImGui::SetNextWindowSize(ImVec2(380, 0), ImGuiCond_Appearing);
    if (ImGui::BeginPopupModal("About AI-CAD", nullptr, ImGuiWindowFlags_NoResize)) {
        if (st.wordmark.id) {
            ImGui::Image((ImTextureID)(intptr_t)st.wordmark.id,
                         ImVec2(190, 190 * (float)st.wordmark.h / st.wordmark.w));
        } else {
            sectionHeader(st.fonts.h1, "AI-CAD");
        }
        dimText("Parametric CAD, driven by AI.");
        ImGui::Dummy(ImVec2(0, 4));
        ImGui::TextUnformatted("build123d + OpenCASCADE kernel");
        ImGui::TextUnformatted("GLFW / Dear ImGui / OpenGL");
        ImGui::Dummy(ImVec2(0, 8));
        if (primaryButton("Close", ImVec2(110, 32))) ImGui::CloseCurrentPopup();
        ImGui::EndPopup();
    }
}

}  // namespace ui
