// AI-CAD Settings modal (AI provider + Agent + Agents-prompt-editor tabs) and the About modal.
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "imgui.h"

#include "settings_modal.h"

#include "../core/app_state.h"
#include "../core/cpp_core.h"
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

// A dimmed "(?)" marker that reveals a one-line explanation on hover — for discoverability of the
// less-obvious settings (vision / plan mode / max iters / units). Drawn inline (SameLine first).
static void helpMarker(const char* desc) {
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Text,
                          ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                                 ui::palette.textDim[2], 1));
    ImGui::TextUnformatted("(?)");
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) {
        ImGui::BeginTooltip();
        ImGui::PushTextWrapPos(ImGui::GetFontSize() * 28.0f);
        ImGui::TextUnformatted(desc);
        ImGui::PopTextWrapPos();
        ImGui::EndTooltip();
    }
}

static void copySettingText(char* dst, size_t dstSize, const char* src) {
    if (!dst || dstSize == 0) return;
    std::fill(dst, dst + dstSize, 0);
    std::strncpy(dst, src ? src : "", dstSize - 1);
}

// Apply a dark/light theme immediately (settings.theme persists on Save) and sync the viewport
// background. Shared by the General-tab theme control. MUST pass st.scale: applyTheme re-seeds all
// style-vars to base and only multiplies them by the scale argument (ScaleAllSizes) — passing 1.0f
// on a HiDPI display collapses every padding/rounding to 100% while the pre-scaled fonts stay large,
// and clobbers ui::uiScale, desyncing every custom-drawn widget. (Was hardcoded 1.0f — bug.)
static void applyThemeNow(AppState& st, bool light) {
    ui::Theme next = light ? ui::Theme::Light : ui::Theme::Dark;
    ui::applyTheme(next, st.scale);
    st.settings.theme = light ? "light" : "dark";
    if (st.renderer)
        st.renderer->setBackground(
            ui::palette.viewportTop[0], ui::palette.viewportTop[1], ui::palette.viewportTop[2],
            ui::palette.viewportBottom[0], ui::palette.viewportBottom[1],
            ui::palette.viewportBottom[2]);
}
}  // anonymous namespace

namespace ui {

void drawSettingsModal(AppState& st, bool busy) {
    Settings& settings = st.settings;

    // ---------- Settings window ----------
    static bool s_settingsOpen = false;
    if (st.openSettings) {
        s_settingsOpen = true;
        st.openSettings = false;
    }
    float sc = ui::uiScale;
    ImGuiViewport* vp = ImGui::GetMainViewport();
    const float margin = 28.0f * sc;
    const float maxW = std::max(360.0f * sc, vp->WorkSize.x - margin * 2.0f);
    const float maxH = std::max(420.0f * sc, vp->WorkSize.y - margin * 2.0f);
    const float winW = std::min(680.0f * sc, maxW);
    const float winH = std::min(720.0f * sc, maxH);
    if (s_settingsOpen) {
        ImGui::SetNextWindowSize(ImVec2(winW, winH), ImGuiCond_Appearing);
        ImGui::SetNextWindowPos(vp->GetCenter(), ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
        if (ImGui::Begin("Settings", &s_settingsOpen,
                         ImGuiWindowFlags_NoResize | ImGuiWindowFlags_NoDocking)) {
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

        // Tab content scrolls in its own region; Save/Cancel stay pinned at the bottom.
        ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
        const float footerH = ImGui::GetFrameHeightWithSpacing() + 20.0f * sc;
        ImGui::BeginChild("##settabsarea",
                          ImVec2(0, std::max(120.0f * sc,
                                             ImGui::GetContentRegionAvail().y - footerH)));
        if (ImGui::BeginTabBar("##settabs")) {
            // ---- General tab: Document (units) + Display (theme) ----
            if (ImGui::BeginTabItem("General")) {
                ImGui::Dummy(ImVec2(0, 2 * sc));

                ImGui::SeparatorText("Document");
                ImGui::TextUnformatted("Units");
                helpMarker("The unit system for dimension display and input. The model is always "
                           "stored in canonical millimeters; this only changes how lengths are "
                           "shown and how a bare typed number is read. Angles stay in degrees in "
                           "both systems. You can also click the MMGS/IPS badge in the status bar "
                           "to switch.");
                int unitSel = st.ips() ? 1 : 0;
                bool unitsChanged = false;
                unitsChanged |= ImGui::RadioButton("MMGS (millimeter, gram, second)", &unitSel, 0);
                unitsChanged |= ImGui::RadioButton("IPS (inch, pound, second)", &unitSel, 1);
                if (unitsChanged) {
                    settings.units = (unitSel == 1) ? Units::IPS : Units::MMGS;
                    // Saved immediately so the choice survives a Cancel (it is a display/document
                    // preference, like theme — and the status-bar toggle persists the same way).
                    saveSettings(settings);
                    st.addLog(std::string("[units] set to ") + (st.ips() ? "IPS" : "MMGS"));
                }
                dimText("Changes how lengths display/parse everywhere (saved immediately).");

                ImGui::Dummy(ImVec2(0, 8 * sc));
                ImGui::SeparatorText("Display");
                bool isLight = (ui::activeTheme == ui::Theme::Light);
                if (ImGui::Checkbox("Light theme (SolidWorks-style)", &isLight))
                    applyThemeNow(st, isLight);
                ImGui::SameLine();
                dimText("(saved immediately)");
                helpMarker("Switches the whole UI and viewport between the dark and light "
                           "(SolidWorks-style) palettes. Persists across restarts.");

                ImGui::Dummy(ImVec2(0, 8 * sc));
                ImGui::SeparatorText("Engine");
                // Reflect the REAL strangler-seam state (see worker_client.cpp): once the C++
                // core (libaicad.dll) loads, doc ops route to it by default (AI_CAD_CPP_DOCS,
                // default ON); sim/agent/ecad are explicit opt-ins. These mirror the flag reads
                // in worker_client.cpp — keep the defaults in sync if they change there.
                auto flagOn = [](const char* name, bool dflt) {
                    const char* f = std::getenv(name);
                    if (f && f[0]) return f[0] != '0';
                    return dflt;
                };
                if (core::cpp_core().available()) {
                    ImGui::TextUnformatted("Active engine: C++ core (libaicad.dll) + Python worker");
                    helpMarker("The in-process C++ core is loaded. New part documents run on it; "
                               "assemblies and legacy/code-exec operations still use the Python "
                               "worker. Per-subsystem routing is shown below and is controlled by "
                               "the AI_CAD_CPP_* environment variables (set a flag to 1 to route "
                               "that subsystem to C++, 0 to force Python).");
                    dimText(("C++ core: libaicad.dll loaded (ABI " +
                             core::cpp_core().abiVersion() + ")").c_str());
                    auto routeRow = [&](const char* label, bool on, const char* env) {
                        ImGui::BulletText("%s: %s", label, on ? "C++ core" : "Python worker");
                        ImGui::SameLine();
                        dimText(("(" + std::string(env) + ")").c_str());
                    };
                    const bool docsOn = flagOn("AI_CAD_CPP_DOCS", false);
                    routeRow("Documents",  docsOn,                            "AI_CAD_CPP_DOCS");
                    routeRow("Simulation", flagOn("AI_CAD_CPP_SIM",   true),  "AI_CAD_CPP_SIM");
                    // AI agent FOLLOWS the doc engine by default (must run on the same engine
                    // as the doc it edits) — see worker_client.cpp cppAgentFlag.
                    routeRow("AI agent",   flagOn("AI_CAD_CPP_AGENT", docsOn), "AI_CAD_CPP_AGENT");
                    routeRow("ECAD",       flagOn("AI_CAD_CPP_ECAD",  false), "AI_CAD_CPP_ECAD");
                } else {
                    ImGui::TextUnformatted("Active engine: Python worker (subprocess)");
                    helpMarker("The C++ core (libaicad.dll) is NOT loaded, so every operation uses "
                               "the Python worker. To load it, set the AI_CAD_LIBAICAD environment "
                               "variable to the built DLL (core/build-occt/bin/Release/libaicad.dll) "
                               "or stage that DLL next to AI-CAD.exe, then restart. Once loaded, new "
                               "part documents route to the C++ core automatically.");
                    dimText("C++ core: not loaded");
                }

                ImGui::Dummy(ImVec2(0, 6 * sc));
                dimText("All settings here are stored in %APPDATA%\\AI-CAD\\config.json.");
                ImGui::EndTabItem();
            }
            if (ImGui::BeginTabItem("AI")) {
                ImGui::Dummy(ImVec2(0, 2 * sc));
                ImGui::SeparatorText("Provider & model");
                dimText("Used by Design, Edit, and Help. Stored in "
                        "%APPDATA%\\AI-CAD\\config.json.");
                ImGui::Dummy(ImVec2(0, 4 * sc));
                const char* provs[] = {"Anthropic (Claude)", "OpenAI",
                                       "Local (OpenAI-compatible)", "llama.cpp (managed local)"};
                ImGui::SetNextItemWidth(-1);
                int prevProvider = settings.provider;
                if (ImGui::Combo("##prov", &settings.provider, provs, 4) &&
                    settings.provider != prevProvider) {
                    if (settings.provider == 2) {
                        copySettingText(settings.baseUrl, sizeof(settings.baseUrl),
                                        "http://localhost:1234/v1");
                        if (std::strstr(settings.model, ".gguf"))
                            copySettingText(settings.model, sizeof(settings.model), "gemma-4-e4b");
                        settings.vision = true;
                    } else if (settings.provider == 3) {
                        copySettingText(settings.baseUrl, sizeof(settings.baseUrl),
                                        "http://127.0.0.1:1234/v1");
                        if (!std::strstr(settings.model, ".gguf")) {
                            copySettingText(settings.model, sizeof(settings.model),
                                            "%APPDATA%\\AI-CAD\\models\\model.gguf");
                        }
                        settings.vision = false;
                    }
                }
                ImGui::Dummy(ImVec2(0, 4 * sc));
                ImGui::TextUnformatted(settings.provider == 3 ? "GGUF model path" : "Model");
                ImGui::SetNextItemWidth(-1);
                ImGui::InputText("##model", settings.model, sizeof(settings.model));
                if (settings.provider == 0 || settings.provider == 1) {
                    ImGui::Dummy(ImVec2(0, 8 * sc));
                    ImGui::SeparatorText("Credentials");
                    ImGui::TextUnformatted("API key");
                    ImGui::SetNextItemWidth(-1);
                    ImGui::InputText("##key", settings.apiKey, sizeof(settings.apiKey),
                                     ImGuiInputTextFlags_Password);
                } else if (settings.provider == 2) {
                    ImGui::Dummy(ImVec2(0, 8 * sc));
                    ImGui::SeparatorText("Connection");
                    ImGui::TextUnformatted("Base URL");
                    ImGui::SetNextItemWidth(-1);
                    ImGui::InputText("##base", settings.baseUrl, sizeof(settings.baseUrl));
                    ImGui::Dummy(ImVec2(0, 4));
                    ImGui::SeparatorText("Credentials");
                    ImGui::TextUnformatted("API key (optional)");
                    ImGui::SetNextItemWidth(-1);
                    ImGui::InputText("##lkey", settings.apiKey, sizeof(settings.apiKey),
                                     ImGuiInputTextFlags_Password);
                    ImGui::Dummy(ImVec2(0, 8));
                    ImGui::SeparatorText("Capabilities");
                    ImGui::Checkbox("Model supports vision (send screenshots)",
                                    &settings.vision);
                    helpMarker("Let the agent SEE its own renders: each iteration's contact-sheet "
                               "screenshot is sent back to the model so it can visually check the "
                               "part and self-correct. Only enable for multimodal local models — "
                               "a text-only model will error on image input.");
                } else {
                    ImGui::TextUnformatted("Base URL");
                    ImGui::SetNextItemWidth(-1);
                    ImGui::InputText("##llamabase", settings.baseUrl, sizeof(settings.baseUrl));
                    ImGui::Checkbox("Model supports vision (send screenshots)",
                                    &settings.vision);
                    helpMarker("Managed llama.cpp launches a bundled or AppData-installed "
                               "llama-server.exe on demand and talks to its local /v1 chat API. "
                               "Put llama-server.exe under %APPDATA%\\AI-CAD\\llamacpp and a GGUF "
                               "model under %APPDATA%\\AI-CAD\\models, or package those folders "
                               "with the installer.");
                }
                // --- C35: local-only / offline mode (ITAR compliance) --- begin marked block ---
                ImGui::Dummy(ImVec2(0, 8 * sc));
                ImGui::SeparatorText("Privacy");
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
            if (ImGui::BeginTabItem("Prompts", nullptr,
                                    jumpToAgents ? ImGuiTabItemFlags_SetSelected
                                                 : ImGuiTabItemFlags_None)) {
                ImGui::Dummy(ImVec2(0, 2 * sc));
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
                        float edH = ImGui::GetContentRegionAvail().y - 38.0f * sc;
                        if (edH < 100.0f * sc) edH = 100.0f * sc;
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

            if (ImGui::BeginTabItem("Advanced", nullptr,
                                    st.wantSettings ? ImGuiTabItemFlags_SetSelected
                                                    : ImGuiTabItemFlags_None)) {
                ImGui::Dummy(ImVec2(0, 2 * sc));
                ImGui::SeparatorText("Run behavior");
                ImGui::Checkbox("Plan before building (recommended)", &settings.planMode);
                helpMarker("Plan mode: before writing any CAD code the agent drafts a build plan "
                           "(the parts, their concrete dimensions and the feature list) and shows "
                           "it in the conversation, then codes against that plan. Off = it goes "
                           "straight to code.");
                dimText("The agent first drafts a build plan - the parts, concrete "
                        "dimensions and features - shows it in the conversation, then "
                        "writes the CAD code against it.");
                ImGui::Dummy(ImVec2(0, 4 * sc));
                ImGui::SeparatorText("Iteration budget");
                ImGui::TextUnformatted("Refine-pass cap per request");
                helpMarker("Max iters: how many times the agent may re-build and refine a part in "
                           "one request before it must stop. 0 = automatic — it keeps going until "
                           "the render matches the request (or stabilizes), then stops on its own. "
                           "A small cap (e.g. 3) bounds cost/time on hard parts.");
                ImGui::SetNextItemWidth(140.0f * sc);
                ImGui::InputInt("##miters", &settings.maxIters);
                if (settings.maxIters < 0) settings.maxIters = 0;
                if (settings.maxIters > 64) settings.maxIters = 64;
                ImGui::SameLine();
                dimText("0 = automatic (stops once the render matches)");
                ImGui::Dummy(ImVec2(0, 6 * sc));
                ImGui::SeparatorText("Prompt override");
                ImGui::Dummy(ImVec2(0, 2 * sc));
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
            s_settingsOpen = false;
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel", ImVec2(100.0f * sc, 34.0f * sc))) {
            std::fill(st.spBuf.begin(), st.spBuf.end(), 0);  // discard edits
            std::strncpy(st.spBuf.data(), settings.systemPrompt.c_str(), st.spBuf.size() - 1);
            s_settingsOpen = false;
        }
        }
        ImGui::End();
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
        ImGui::Dummy(ImVec2(0, 4 * sc));
        ImGui::TextUnformatted("build123d + OpenCASCADE kernel");
        ImGui::TextUnformatted("GLFW / Dear ImGui / OpenGL");
        ImGui::Dummy(ImVec2(0, 8 * sc));
        if (primaryButton("Close", ImVec2(110.0f * sc, 32.0f * sc))) ImGui::CloseCurrentPopup();
        ImGui::EndPopup();
    }
}

}  // namespace ui
