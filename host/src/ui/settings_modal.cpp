// AI-CAD Settings modal (AI provider + Agent + Agents-prompt-editor tabs) and the About modal.
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "imgui.h"

#include "settings_modal.h"

#include "../core/app_state.h"
#include "../renderer.h"

// ---- C22b prompt editor state ----
// Per-role editor buffer and metadata, lazily loaded from get_prompts_v2.
namespace {
struct PromptRecord {
    std::string role;
    std::string builtinPath;     // abs path of the built-in .md
    std::string overridePath;    // abs path where we write the override
    bool overridden = false;
    // 16 KB buffer (matches the card spec)
    std::vector<char> buf{std::vector<char>(16 * 1024, 0)};
    int builtinLen = 0;  // length of the built-in text (for diff indicator)
};
static std::vector<PromptRecord> s_promptRecs;
static bool s_promptsLoaded = false;
static int  s_selectedRole  = 0;

static void loadPrompts(AppState& st) {
    nlohmann::json result;
    std::string err;
    if (!st.worker.call("get_prompts_v2", nlohmann::json::object(), result, err, 20000)) {
        st.addLog("[err] get_prompts_v2: " + err);
        return;
    }
    auto& recs = result.value("prompts", nlohmann::json::array());
    s_promptRecs.clear();
    for (auto& r : recs) {
        PromptRecord pr;
        pr.role         = r.value("role", "");
        pr.builtinPath  = r.value("path", "");
        pr.overridePath = r.value("override_path", "");
        pr.overridden   = r.value("overridden", false);
        std::string text = r.value("text", "");
        pr.builtinLen = (int)text.size();
        // fill buf with the resolved text (override or built-in)
        std::fill(pr.buf.begin(), pr.buf.end(), 0);
        std::strncpy(pr.buf.data(), text.c_str(), pr.buf.size() - 1);
        s_promptRecs.push_back(std::move(pr));
    }
    s_promptsLoaded = true;
}

static void savePromptOverride(PromptRecord& pr) {
    // Write the buffer contents to the override file
    std::ofstream f(pr.overridePath, std::ios::out | std::ios::trunc);
    if (f.is_open()) {
        f << pr.buf.data();
        pr.overridden = true;
    }
}

static void resetPromptOverride(PromptRecord& pr, AppState& st) {
    // Delete the override file
    std::remove(pr.overridePath.c_str());
    pr.overridden = false;
    // Reload built-in text from the worker
    s_promptsLoaded = false;
    loadPrompts(st);
}
}  // anonymous namespace

namespace ui {

void drawSettingsModal(AppState& st, bool busy) {
    Settings& settings = st.settings;

    // ---------- Settings modal ----------
    if (st.openSettings) {
        ImGui::OpenPopup("Settings");
        st.openSettings = false;
    }
    float sc = ui::uiScale;
    ImGui::SetNextWindowSize(ImVec2(580.0f * sc, 640.0f * sc), ImGuiCond_Appearing);
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

        // C22b: if a pending prompt role was requested, load prompts and jump to Agents tab.
        bool jumpToAgents = false;
        if (!st.pendingPromptRole.empty() && st.workerStarted) {
            if (!s_promptsLoaded) loadPrompts(st);
            // Select the matching role in the list
            for (int i = 0; i < (int)s_promptRecs.size(); ++i) {
                if (s_promptRecs[i].role == st.pendingPromptRole) {
                    s_selectedRole = i;
                    break;
                }
            }
            st.pendingPromptRole.clear();
            jumpToAgents = true;
        }

        // tab content scrolls in its own region; Save/Cancel stay pinned at the bottom
        ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
        ImGui::BeginChild("##settabsarea",
                          ImVec2(0, ImGui::GetContentRegionAvail().y - 52.0f * sc));
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
                // --- C35: local-only / offline mode (ITAR compliance) --- begin marked block ---
                ImGui::Dummy(ImVec2(0, 6));
                ImGui::Checkbox("Offline mode (local models only — no network egress)",
                                &settings.offlineMode);
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip(
                        "When enabled, the AI refuses any provider whose endpoint is not the "
                        "local machine (localhost / 127.0.0.1 / ::1). Cloud providers (Anthropic, "
                        "OpenAI) are blocked outright, so no technical data ever leaves this "
                        "computer. Enforced in the agent before every run.");
                // --- end C35 marked block ---
                ImGui::EndTabItem();
            }
            // --- C22b: Agents tab (role prompt editor) --- begin marked block ---
            if (ImGui::BeginTabItem("Agents", nullptr,
                                    jumpToAgents ? ImGuiTabItemFlags_SetSelected
                                                 : ImGuiTabItemFlags_None)) {
                ImGui::Dummy(ImVec2(0, 2));
                bool canFetch = st.workerStarted && !busy;
                if (!s_promptsLoaded) {
                    if (canFetch) {
                        loadPrompts(st);
                    } else {
                        dimText("Worker not ready — start the runtime first.");
                    }
                }
                if (s_promptsLoaded && !s_promptRecs.empty()) {
                    // Left column: role list
                    float listW = 100.0f * sc;
                    float avail = ImGui::GetContentRegionAvail().y;
                    ImGui::PushStyleColor(ImGuiCol_ChildBg,
                        ImVec4(ui::palette.panelBg[0], ui::palette.panelBg[1],
                               ui::palette.panelBg[2], 1));
                    ImGui::BeginChild("##rolelist", ImVec2(listW, avail - 38.0f * sc),
                                      ImGuiChildFlags_Border);
                    for (int i = 0; i < (int)s_promptRecs.size(); ++i) {
                        bool sel = (i == s_selectedRole);
                        auto& pr = s_promptRecs[i];
                        std::string lbl = pr.role;
                        if (pr.overridden) lbl += " *";
                        if (ImGui::Selectable(lbl.c_str(), sel))
                            s_selectedRole = i;
                    }
                    ImGui::EndChild();
                    ImGui::PopStyleColor();

                    ImGui::SameLine();

                    // Right column: editor + diff indicator
                    ImGui::BeginChild("##roleeditor", ImVec2(0, avail - 38.0f * sc), ImGuiChildFlags_None);
                    if (s_selectedRole < (int)s_promptRecs.size()) {
                        auto& pr = s_promptRecs[s_selectedRole];
                        // Overridden badge
                        if (pr.overridden) {
                            ImGui::PushStyleColor(ImGuiCol_Text,
                                ImVec4(ui::palette.statusWarn[0], ui::palette.statusWarn[1],
                                       ui::palette.statusWarn[2], 1));
                            ImGui::TextUnformatted("[overridden]");
                            ImGui::PopStyleColor();
                            ImGui::SameLine();
                        }
                        ImGui::PushFont(st.fonts.medium);
                        ImGui::TextUnformatted(pr.role.c_str());
                        ImGui::PopFont();
                        // Diff indicator (just char count difference — not a full diff)
                        int curLen = (int)std::strlen(pr.buf.data());
                        int delta  = curLen - pr.builtinLen;
                        if (delta != 0) {
                            ImGui::SameLine();
                            dimText(delta > 0 ? "(differs from built-in: longer)"
                                              : "(differs from built-in: shorter)");
                        }
                        ImGui::PushFont(st.fonts.mono);
                        float edH = ImGui::GetContentRegionAvail().y - 38;
                        if (edH < 100) edH = 100;
                        ImGui::InputTextMultiline("##prompted", pr.buf.data(), pr.buf.size(),
                                                  ImVec2(-1, edH));
                        ImGui::PopFont();
                        // Save / Reset
                        if (!canFetch) ImGui::BeginDisabled();
                        if (primaryButton("Save", ImVec2(80.0f * sc, 28.0f * sc))) {
                            savePromptOverride(pr);
                            st.addLog("[ok] prompt saved: " + pr.role);
                        }
                        if (!canFetch) ImGui::EndDisabled();
                        ImGui::SameLine();
                        if (!canFetch) ImGui::BeginDisabled();
                        if (ImGui::Button("Reset to built-in", ImVec2(140.0f * sc, 28.0f * sc))) {
                            resetPromptOverride(pr, st);
                            st.addLog("[ok] prompt reset: " + pr.role);
                        }
                        if (!canFetch) ImGui::EndDisabled();
                        if (ImGui::IsItemHovered())
                            ImGui::SetTooltip("Delete the override and restore the shipped prompt");
                    }
                    ImGui::EndChild();
                }
                ImGui::EndTabItem();
            }
            // --- end C22b marked block ---

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
        if (primaryButton("Save", ImVec2(120.0f * sc, 34.0f * sc))) {
            settings.systemPrompt = st.spBuf.data();
            saveSettings(settings);
            st.addLog("[ok] saved settings");
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel", ImVec2(100.0f * sc, 34.0f * sc))) {
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
    ImGui::SetNextWindowSize(ImVec2(380.0f * sc, 0), ImGuiCond_Appearing);
    if (ImGui::BeginPopupModal("About AI-CAD", nullptr, ImGuiWindowFlags_NoResize)) {
        const Tex& wm = (ui::activeTheme == ui::Theme::Light && st.wordmarkDark.id)
                            ? st.wordmarkDark : st.wordmark;
        if (wm.id) {
            ImGui::Image((ImTextureID)(intptr_t)wm.id,
                         ImVec2(190.0f * sc, 190.0f * sc * (float)wm.h / wm.w));
        } else {
            sectionHeader(st.fonts.h1, "AI-CAD");
        }
        dimText("Parametric CAD, driven by AI.");
        ImGui::Dummy(ImVec2(0, 4));
        ImGui::TextUnformatted("build123d + OpenCASCADE kernel");
        ImGui::TextUnformatted("GLFW / Dear ImGui / OpenGL");
        ImGui::Dummy(ImVec2(0, 8));
        if (primaryButton("Close", ImVec2(110.0f * sc, 32.0f * sc))) ImGui::CloseCurrentPopup();
        ImGui::EndPopup();
    }
}

}  // namespace ui
