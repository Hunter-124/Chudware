// Settings + About modals — ported from host/src/ui/settings_modal.cpp.
#include "ui/settings_modal.h"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "imgui.h"

#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/domain_tokens.h"
#include "cpp_core.h"
#include "bootstrap.h"
#include "ui/first_run.h"
#include <platform/paths.h>

namespace ui {
namespace {

AppSettings s_settings;
SettingsUiState s_ui;
static std::vector<char> s_spBuf(16 * 1024, 0);
static bool s_settingsLoaded = false;

static void copyStr(char* dst, size_t n, const std::string& s) {
    std::strncpy(dst, s.c_str(), n - 1);
    dst[n - 1] = '\0';
}

static bool primaryButton(const char* label, const ImVec2& size) {
    const auto& p = theme::pal();
    ImGui::PushStyleColor(ImGuiCol_Button, theme::vec(p.accent));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, theme::roleVec(p.accent, 1.15f));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, theme::vec(p.accentDim));
    ImGui::PushStyleColor(ImGuiCol_Text, theme::panelview::accentButtonInk());
    bool r = ImGui::Button(label, size);
    ImGui::PopStyleColor(4);
    return r;
}

static void dimText(const char* t) {
    ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
    ImGui::TextWrapped("%s", t);
    ImGui::PopStyleColor();
}

static void helpMarker(const char* desc) {
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
    ImGui::TextUnformatted("(?)");
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) {
        ImGui::BeginTooltip();
        ImGui::PushTextWrapPos(ImGui::GetFontSize() * 28.f);
        ImGui::TextUnformatted(desc);
        ImGui::PopTextWrapPos();
        ImGui::EndTooltip();
    }
}

static void applyThemeNow(app::AppState& st, bool light) {
    theme::setDark(!light);
    st.dark = !light;
    s_settings.theme = light ? "light" : "dark";
}

static bool ipsUnits() { return s_settings.units == Units::IPS; }

struct LocalPreset {
    std::string id, label, note, baseUrl;
    bool vision = false;
};

static const char* providerLabel(int provider) {
    switch (provider) {
        case 0: return "Anthropic (Claude)";
        case 1: return "OpenAI";
        case 2: return "Local (OpenAI-compatible)";
        case 3: return "llama.cpp (managed local)";
        case 4: return "Google Gemini";
        case 5: return "Cloud API (Qwen / DeepSeek / OpenAI-compatible)";
        default: return "?";
    }
}

static const std::vector<LocalPreset>& localPresets() {
    static const std::vector<LocalPreset> k = {
        {"qwen2.5-coder-7b-instruct", "Qwen2.5-Coder 7B (ops/code)", "Code model for CAD ops.",
         "http://localhost:1234/v1", false},
        {"qwen2.5-vl-7b-instruct", "Qwen2.5-VL 7B (vision review)", "Vision judge on viewport PNGs.",
         "http://localhost:1234/v1", true},
    };
    return k;
}

static const std::vector<LocalPreset>& cloudPresets() {
    static const std::vector<LocalPreset> k = {
        {"qwen/qwen3-coder:free", "OpenRouter · Qwen3-Coder (free)", "OpenRouter free tier.",
         "https://openrouter.ai/api/v1", false},
        {"qwen3-vl-plus", "Alibaba Qwen3-VL-Plus (vision)", "DashScope vision model.",
         "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", true},
    };
    return k;
}

static const char* cloudServiceKey(const std::string& baseUrl) {
    if (baseUrl.find("openrouter") != std::string::npos) return "openrouter";
    if (baseUrl.find("deepseek") != std::string::npos) return "deepseek";
    if (baseUrl.find("dashscope") != std::string::npos) return "alibaba";
    return "cloud";
}

static char* providerKeyBuf(AppSettings& s, int provider) {
    switch (provider) {
        case 0: return s.anthropicKey;
        case 1: return s.openaiKey;
        case 2: return s.apiKey;
        case 4: return s.geminiKey;
        case 5: return s.cloudKey;
        default: return s.apiKey;
    }
}

static void drawProviderModelPicker(app::AppState& st, bool busy) {
    (void)busy;
    AppSettings& s = s_settings;
    const float sc = theme::S();
    const char* providers[] = {
        "Anthropic (Claude)", "OpenAI", "Local (OpenAI-compatible)",
        "llama.cpp (managed local)", "Google Gemini",
        "Cloud API (Qwen / DeepSeek / OpenAI-compatible)",
    };
    ImGui::TextUnformatted("AI provider");
    ImGui::SetNextItemWidth(-1);
    if (ImGui::Combo("##prov", &s.provider, providers, 6)) { /* changed */ }

    ImGui::Dummy(ImVec2(0, 4 * sc));
    ImGui::TextUnformatted("Model id");
    ImGui::SetNextItemWidth(-1);
    ImGui::InputText("##model", s.model, sizeof(s.model));

    const bool cloud = (s.provider == 0 || s.provider == 1 || s.provider == 4 || s.provider == 5);
    ImGui::Dummy(ImVec2(0, 4 * sc));
    if (s.provider == 5) {
        ImGui::TextUnformatted("Endpoint (base URL)");
        ImGui::SetNextItemWidth(-1);
        ImGui::InputText("##cloudbase", s.baseUrl, sizeof(s.baseUrl));
    } else if (!cloud) {
        ImGui::TextUnformatted("Base URL");
        ImGui::SetNextItemWidth(-1);
        ImGui::InputText("##baseurl", s.baseUrl, sizeof(s.baseUrl));
    }
    if (cloud || s.provider == 2) {
        ImGui::TextUnformatted(s.provider == 2 ? "API key (optional)" : "API key");
        char* key = providerKeyBuf(s, s.provider);
        ImGui::SetNextItemWidth(-1);
        ImGui::InputText("##provkey", key, 256, ImGuiInputTextFlags_Password);
    }
    ImGui::Checkbox("Model supports vision (send screenshots)", &s.vision);
    ImGui::Checkbox("Offline mode (local endpoints only)", &s.offlineMode);
    if (!st.workerStarted)
        dimText("Worker not ready — start the runtime to probe models.");
}

struct PromptRecord {
    std::string role, builtinPath, overridePath;
    bool overridden = false;
    std::vector<char> buf{std::vector<char>(16 * 1024, 0)};
    int builtinLen = 0;
};
static std::vector<PromptRecord> s_promptRecs;
static bool s_promptsLoaded = false;
static int  s_selectedRole  = 0;

static void loadPrompts(app::AppState& st) {
    nlohmann::json result;
    std::string err;
    if (!st.worker.call("get_prompts_v2", nlohmann::json::object(), result, err, 20000)) {
        st.status = "[err] get_prompts_v2: " + err;
        return;
    }
    auto str = [](const nlohmann::json& j, const char* k) -> std::string {
        auto it = j.find(k);
        return (it != j.end() && it->is_string()) ? it->get<std::string>() : std::string();
    };
    const nlohmann::json* recs = &result;
    if (result.is_object() && result.contains("prompts") && result["prompts"].is_array())
        recs = &result["prompts"];
    s_promptRecs.clear();
    if (!recs->is_array()) { s_promptsLoaded = true; return; }
    for (auto& r : *recs) {
        if (!r.is_object()) continue;
        PromptRecord pr;
        pr.role         = str(r, "role");
        pr.builtinPath  = str(r, "path");
        pr.overridePath = str(r, "override_path");
        pr.overridden   = r.value("overridden", false);
        std::string text = str(r, "text");
        pr.builtinLen = (int)text.size();
        std::fill(pr.buf.begin(), pr.buf.end(), 0);
        std::strncpy(pr.buf.data(), text.c_str(), pr.buf.size() - 1);
        s_promptRecs.push_back(std::move(pr));
    }
    s_promptsLoaded = true;
}

static void savePromptOverride(PromptRecord& pr) {
    std::ofstream f(pr.overridePath, std::ios::out | std::ios::trunc);
    if (f.is_open()) {
        f << pr.buf.data();
        pr.overridden = true;
    }
}

static void resetPromptOverride(PromptRecord& pr, app::AppState& st) {
    std::remove(pr.overridePath.c_str());
    pr.overridden = false;
    s_promptsLoaded = false;
    loadPrompts(st);
}

}  // namespace

AppSettings& appSettings() {
    if (!s_settingsLoaded) loadAppSettings();
    return s_settings;
}

SettingsUiState& settingsUi() { return s_ui; }

void loadAppSettings() {
    s_settingsLoaded = true;
    std::ifstream f(platform::configPath());
    if (!f) return;
    try {
        nlohmann::json j;
        f >> j;
        std::string prov = j.value("provider", "gemini");
        auto oai = j.value("openai", nlohmann::json::object());
        std::string base = oai.value("base_url", "");
        bool hasBase = !base.empty();
        auto isLoopback = [](const std::string& u) {
            return u.find("localhost") != std::string::npos ||
                   u.find("127.0.0.1") != std::string::npos;
        };
        if (prov == "gemini") s_settings.provider = 4;
        else if (prov == "anthropic") s_settings.provider = 0;
        else if (prov == "llamacpp") s_settings.provider = 3;
        else if (hasBase) s_settings.provider = isLoopback(base) ? 2 : 5;
        else s_settings.provider = 1;
        copyStr(s_settings.model, sizeof(s_settings.model), j.value("model", std::string()));
        copyStr(s_settings.apiKey, sizeof(s_settings.apiKey), j.value("api_key", std::string()));
        auto keys = j.value("keys", nlohmann::json::object());
        copyStr(s_settings.geminiKey, sizeof(s_settings.geminiKey), keys.value("gemini", std::string()));
        copyStr(s_settings.anthropicKey, sizeof(s_settings.anthropicKey), keys.value("anthropic", std::string()));
        copyStr(s_settings.openaiKey, sizeof(s_settings.openaiKey), keys.value("openai", std::string()));
        copyStr(s_settings.cloudKey, sizeof(s_settings.cloudKey), keys.value("cloud_compat", std::string()));
        copyStr(s_settings.reasoningEffort, sizeof(s_settings.reasoningEffort),
                j.value("reasoning_effort", std::string("high")));
        if (hasBase) std::strncpy(s_settings.baseUrl, base.c_str(), sizeof(s_settings.baseUrl) - 1);
        s_settings.offlineMode = j.value("offline_mode", false);
        s_settings.planMode = j.value("plan_mode", true);
        s_settings.maxIters = j.value("max_iters", 0);
        s_settings.systemPrompt = j.value("system_prompt", "");
        s_settings.theme = j.value("theme", "dark");
        s_settings.units = (j.value("units", std::string("mmgs")) == "ips") ? Units::IPS : Units::MMGS;
        s_settings.workspaceDir = j.value("workspace_dir", std::string());
        s_settings.recentFiles.clear();
        if (auto it = j.find("recent_files"); it != j.end() && it->is_array())
            for (const auto& e : *it)
                if (e.is_string()) s_settings.recentFiles.push_back(e.get<std::string>());
        auto exp = j.value("experimental", nlohmann::json::object());
        if (auto vr = exp.value("vision_review", nlohmann::json::object()); !vr.empty()) {
            s_settings.expVisionReview = vr.value("enabled", false);
            copyStr(s_settings.expVisionModel, sizeof(s_settings.expVisionModel),
                    vr.value("model", std::string("qwen2.5-vl-7b-instruct")));
            copyStr(s_settings.expVisionBaseUrl, sizeof(s_settings.expVisionBaseUrl),
                    vr.value("base_url", std::string("http://localhost:1234/v1")));
            copyStr(s_settings.expVisionKey, sizeof(s_settings.expVisionKey),
                    vr.value("api_key", std::string()));
        }
        s_settings.expCadSequenceMode = exp.value("cad_sequence_mode", false);
        std::fill(s_spBuf.begin(), s_spBuf.end(), 0);
        std::strncpy(s_spBuf.data(), s_settings.systemPrompt.c_str(), s_spBuf.size() - 1);
    } catch (...) {}
}

void saveAppSettings(const AppSettings& s) {
    nlohmann::json j;
    if (s.provider == 4) {
        j["provider"] = "gemini";
        j["model"] = s.model[0] ? s.model : "gemini-2.5-flash";
        j["api_key"] = s.geminiKey;
    } else if (s.provider == 0) {
        j["provider"] = "anthropic";
        j["model"] = s.model[0] ? s.model : "claude-opus-4-8";
        j["api_key"] = s.anthropicKey;
    } else if (s.provider == 1) {
        j["provider"] = "openai";
        j["model"] = s.model[0] ? s.model : "gpt-4o";
        j["api_key"] = s.openaiKey;
    } else if (s.provider == 2) {
        j["provider"] = "openai";
        j["model"] = s.model;
        j["api_key"] = s.apiKey[0] ? s.apiKey : "lm-studio";
        j["openai"] = {{"base_url", s.baseUrl}, {"vision", s.vision}};
    } else if (s.provider == 5) {
        j["provider"] = "openai";
        j["model"] = s.model[0] ? s.model : "qwen-max";
        j["api_key"] = s.cloudKey;
        j["openai"] = {{"base_url", s.baseUrl}, {"vision", s.vision}};
    } else {
        j["provider"] = "llamacpp";
        j["model"] = "local-gguf";
        j["llamacpp"] = {{"base_url", s.baseUrl}, {"model_path", s.model}, {"vision", s.vision}};
    }
    j["keys"] = {{"gemini", s.geminiKey}, {"anthropic", s.anthropicKey},
                 {"openai", s.openaiKey}, {"cloud_compat", s.cloudKey}};
    if (s.reasoningEffort[0]) j["reasoning_effort"] = s.reasoningEffort;
    j["offline_mode"] = s.offlineMode;
    j["plan_mode"] = s.planMode;
    if (s.maxIters > 0) j["max_iters"] = s.maxIters;
    if (!s.systemPrompt.empty()) j["system_prompt"] = s.systemPrompt;
    j["theme"] = s.theme;
    j["units"] = (s.units == Units::IPS) ? "ips" : "mmgs";
    if (!s.workspaceDir.empty()) j["workspace_dir"] = s.workspaceDir;
    j["recent_files"] = s.recentFiles;
    if (s.expVisionReview || s.expCadSequenceMode) {
        nlohmann::json exp = nlohmann::json::object();
        if (s.expVisionReview) {
            exp["vision_review"] = {
                {"enabled", true},
                {"model", s.expVisionModel},
                {"base_url", s.expVisionBaseUrl},
                {"api_key", s.expVisionKey},
            };
        }
        if (s.expCadSequenceMode) exp["cad_sequence_mode"] = true;
        j["experimental"] = exp;
    }
    try {
        std::filesystem::create_directories(platform::appdataRoot());
        std::ofstream out(platform::configPath(), std::ios::out | std::ios::trunc);
        if (out) out << j.dump(2);
    } catch (...) {}
}

void drawSettingsModal(app::AppState& st, bool busy) {
    AppSettings& settings = appSettings();

    static bool s_settingsOpen = false;
    if (s_ui.openSettings) {
        s_settingsOpen = true;
        s_ui.openSettings = false;
    }
    if (s_ui.openSettingsTab >= 0) s_settingsOpen = true;

    const float sc = theme::S();
    ImGuiViewport* vp = ImGui::GetMainViewport();
    const float margin = theme::U(3.5f);
    const float maxW = std::max(360.f * sc, vp->WorkSize.x - margin * 2.f);
    const float maxH = std::max(420.f * sc, vp->WorkSize.y - margin * 2.f);
    const float winW = std::min(680.f * sc, maxW);
    const float winH = std::min(720.f * sc, maxH);

    if (s_settingsOpen) {
        ImGui::SetNextWindowSize(ImVec2(winW, winH), ImGuiCond_Appearing);
        ImGui::SetNextWindowPos(vp->GetCenter(), ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
        theme::pushGlassWindowStyle();
        if (ImGui::Begin("##settings_win", &s_settingsOpen,
                         ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                         ImGuiWindowFlags_NoDocking)) {
            ImDrawList* wdl = ImGui::GetWindowDrawList();
            ImVec2 wp = ImGui::GetWindowPos();
            ImVec2 ws = ImGui::GetWindowSize();
            theme::glassPanel(wdl, ImRect(wp, ImVec2(wp.x + ws.x, wp.y + ws.y)),
                              theme::MaterialLevel::Thick, theme::ctx().m.roundingLg * sc);

            // Self-drawn header (replaces the transparent ImGui title bar)
            {
                ImGui::PushFont(theme::font(theme::Font::H2));
                ImGui::TextUnformatted("Settings");
                ImGui::PopFont();
                const float btnW = ImGui::GetFrameHeight();
                ImGui::SameLine();
                ImGui::SetCursorPosX(ImGui::GetWindowWidth() - btnW -
                                     ImGui::GetStyle().WindowPadding.x);
                if (ImGui::Button("\xC3\x97##settings_close", ImVec2(btnW, btnW)))
                    s_settingsOpen = false;
                if (ImGui::IsKeyPressed(ImGuiKey_Escape))
                    s_settingsOpen = false;
            }
            ImGui::Spacing();

            bool jumpToAgents = false;
            if (!s_ui.pendingPromptRole.empty() && st.workerStarted) {
                if (!s_promptsLoaded) loadPrompts(st);
                for (int i = 0; i < (int)s_promptRecs.size(); ++i) {
                    if (s_promptRecs[i].role == s_ui.pendingPromptRole) {
                        s_selectedRole = i;
                        break;
                    }
                }
                s_ui.pendingPromptRole.clear();
                jumpToAgents = true;
            }
            int jumpToTab = -1;
            if (s_ui.openSettingsTab >= 0) {
                jumpToTab = s_ui.openSettingsTab;
                s_ui.openSettingsTab = -1;
            }

            ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
            const float footerH = ImGui::GetFrameHeightWithSpacing() + 20.f * sc;
            ImGui::BeginChild("##settabsarea",
                              ImVec2(0, std::max(120.f * sc,
                                                 ImGui::GetContentRegionAvail().y - footerH)));
            if (ImGui::BeginTabBar("##settabs")) {
                if (ImGui::BeginTabItem("General", nullptr,
                                        jumpToTab == 0 ? ImGuiTabItemFlags_SetSelected
                                                       : ImGuiTabItemFlags_None)) {
                    ImGui::Dummy(ImVec2(0, 2 * sc));
                    ImGui::SeparatorText("Document");
                    ImGui::TextUnformatted("Units");
                    helpMarker("MMGS or IPS display/input units. Stored in config.json.");
                    int unitSel = ipsUnits() ? 1 : 0;
                    bool unitsChanged = false;
                    unitsChanged |= ImGui::RadioButton("MMGS (millimeter, gram, second)", &unitSel, 0);
                    unitsChanged |= ImGui::RadioButton("IPS (inch, pound, second)", &unitSel, 1);
                    if (unitsChanged) {
                        settings.units = (unitSel == 1) ? Units::IPS : Units::MMGS;
                        st.units = ipsUnits() ? "in" : "mm";
                        saveAppSettings(settings);
                    }
                    dimText("Changes how lengths display/parse everywhere (saved immediately).");

                    ImGui::Dummy(ImVec2(0, 8 * sc));
                    ImGui::SeparatorText("Display");
                    bool isLight = !st.dark;
                    if (ImGui::Checkbox("Light theme (SolidWorks-style)", &isLight))
                        applyThemeNow(st, isLight);
                    ImGui::SameLine();
                    dimText("(saved immediately)");

                    ImGui::Dummy(ImVec2(0, 8 * sc));
                    ImGui::SeparatorText("Engine");
                    auto flagOn = [](const char* name, bool dflt) {
                        const char* f = std::getenv(name);
                        if (f && f[0]) return f[0] != '0';
                        return dflt;
                    };
                    if (core::cpp_core().available()) {
                        ImGui::TextUnformatted("Active engine: C++ core (libaicad) + Python worker");
                        dimText(("C++ core loaded (ABI " + core::cpp_core().abiVersion() + ")").c_str());
                        auto routeRow = [&](const char* label, bool on, const char* env) {
                            ImGui::BulletText("%s: %s", label, on ? "C++ core" : "Python worker");
                            ImGui::SameLine();
                            dimText(("(" + std::string(env) + ")").c_str());
                        };
                        const bool docsOn = flagOn("AI_CAD_CPP_DOCS", true);
                        routeRow("Documents",  docsOn, "AI_CAD_CPP_DOCS");
                        routeRow("Simulation", flagOn("AI_CAD_CPP_SIM", true), "AI_CAD_CPP_SIM");
                        routeRow("AI agent",   flagOn("AI_CAD_CPP_AGENT", docsOn), "AI_CAD_CPP_AGENT");
                        routeRow("ECAD",       flagOn("AI_CAD_CPP_ECAD", false), "AI_CAD_CPP_ECAD");
                    } else {
                        ImGui::TextUnformatted("Active engine: Python worker (subprocess)");
                        dimText("C++ core: not loaded — set AI_CAD_LIBAICAD or stage libaicad next to the exe.");
                    }

                    ImGui::Dummy(ImVec2(0, 8 * sc));
                    ImGui::SeparatorText("Performance");
                    dimText("Sampling profiler — live flame graph of modeling time.");
                    if (ImGui::Button("Open Profiler", ImVec2(160.f * sc, 30.f * sc)))
                        s_ui.showProfilerWindow = true;
                    ImGui::Dummy(ImVec2(0, 6 * sc));
                    dimText(("Settings stored in " + platform::appdataDisplayLabel()).c_str());
                    ImGui::EndTabItem();
                }
                if (ImGui::BeginTabItem("AI", nullptr,
                                        jumpToTab == 1 ? ImGuiTabItemFlags_SetSelected
                                                       : ImGuiTabItemFlags_None)) {
                    ImGui::Dummy(ImVec2(0, 2 * sc));
                    ImGui::SeparatorText("Provider & model");
                    dimText(("Used by Design, Edit, and Help. Stored in " +
                             platform::appdataDisplayLabel()).c_str());
                    ImGui::Dummy(ImVec2(0, 4 * sc));
                    drawProviderModelPicker(st, busy);
                    ImGui::EndTabItem();
                }
                if (ImGui::BeginTabItem("Prompts", nullptr,
                                        (jumpToAgents || jumpToTab == 2) ? ImGuiTabItemFlags_SetSelected
                                                                         : ImGuiTabItemFlags_None)) {
                    ImGui::Dummy(ImVec2(0, 2 * sc));
                    bool canFetch = st.workerStarted && !busy;
                    if (!s_promptsLoaded) {
                        if (canFetch) loadPrompts(st);
                        else dimText("Worker not ready — start the runtime first.");
                    }
                    if (s_promptsLoaded && !s_promptRecs.empty()) {
                        float listW = 100.f * sc;
                        float avail = ImGui::GetContentRegionAvail().y;
                        ImGui::PushStyleColor(ImGuiCol_ChildBg, TVEC(bg2));
                        ImGui::BeginChild("##rolelist", ImVec2(listW, avail - 38.f * sc),
                                          ImGuiChildFlags_Border);
                        for (int i = 0; i < (int)s_promptRecs.size(); ++i) {
                            auto& pr = s_promptRecs[i];
                            std::string lbl = pr.role;
                            if (pr.overridden) lbl += " *";
                            if (ImGui::Selectable(lbl.c_str(), i == s_selectedRole))
                                s_selectedRole = i;
                        }
                        ImGui::EndChild();
                        ImGui::PopStyleColor();
                        ImGui::SameLine();
                        ImGui::BeginChild("##roleeditor", ImVec2(0, avail - 38.f * sc), ImGuiChildFlags_None);
                        if (s_selectedRole < (int)s_promptRecs.size()) {
                            auto& pr = s_promptRecs[s_selectedRole];
                            if (pr.overridden) {
                                ImGui::TextColored(TVEC(warn), "[overridden]");
                                ImGui::SameLine();
                            }
                            ImGui::PushFont(theme::font(theme::Font::H2));
                            ImGui::TextUnformatted(pr.role.c_str());
                            ImGui::PopFont();
                            ImGui::PushFont(theme::font(theme::Font::Mono));
                            float edH = ImGui::GetContentRegionAvail().y - 38.f * sc;
                            if (edH < 100.f * sc) edH = 100.f * sc;
                            ImGui::InputTextMultiline("##prompted", pr.buf.data(), pr.buf.size(),
                                                      ImVec2(-1, edH));
                            ImGui::PopFont();
                            if (!canFetch) ImGui::BeginDisabled();
                            if (primaryButton("Save", ImVec2(80.f * sc, 28.f * sc)))
                                savePromptOverride(pr);
                            if (!canFetch) ImGui::EndDisabled();
                            ImGui::SameLine();
                            if (!canFetch) ImGui::BeginDisabled();
                            if (ImGui::Button("Reset to built-in", ImVec2(140.f * sc, 28.f * sc)))
                                resetPromptOverride(pr, st);
                            if (!canFetch) ImGui::EndDisabled();
                        }
                        ImGui::EndChild();
                    }
                    ImGui::EndTabItem();
                }
                if (ImGui::BeginTabItem("Advanced", nullptr,
                                        (s_ui.wantSettings || jumpToTab == 3)
                                            ? ImGuiTabItemFlags_SetSelected
                                            : ImGuiTabItemFlags_None)) {
                    ImGui::Dummy(ImVec2(0, 2 * sc));
                    ImGui::SeparatorText("Run behavior");
                    ImGui::Checkbox("Plan before building (recommended)", &settings.planMode);
                    helpMarker("Plan mode drafts a build plan before writing CAD code.");
                    ImGui::Dummy(ImVec2(0, 4 * sc));
                    ImGui::SeparatorText("Iteration budget");
                    ImGui::TextUnformatted("Refine-pass cap per request");
                    ImGui::SetNextItemWidth(140.f * sc);
                    ImGui::InputInt("##miters", &settings.maxIters);
                    if (settings.maxIters < 0) settings.maxIters = 0;
                    if (settings.maxIters > 64) settings.maxIters = 64;
                    ImGui::SameLine();
                    dimText("0 = automatic");
                    ImGui::Dummy(ImVec2(0, 6 * sc));
                    ImGui::SeparatorText("Prompt override");
                    ImGui::TextUnformatted("System prompt override");
                    dimText("Leave empty to use the built-in prompt.");
                    ImGui::PushFont(theme::font(theme::Font::Mono));
                    ImGui::InputTextMultiline("##sysprompt", s_spBuf.data(), s_spBuf.size(),
                                              ImVec2(-1, ImGui::GetTextLineHeight() * 10));
                    ImGui::PopFont();
                    bool canFetch = st.workerStarted && !busy;
                    if (!canFetch) ImGui::BeginDisabled();
                    if (ImGui::Button("Load built-in prompt")) {
                        nlohmann::json pr;
                        std::string perr;
                        if (st.worker.call("get_prompts", nlohmann::json::object(), pr, perr, 20000)) {
                            std::fill(s_spBuf.begin(), s_spBuf.end(), 0);
                            std::strncpy(s_spBuf.data(), pr.value("system_prompt", "").c_str(),
                                         s_spBuf.size() - 1);
                        }
                    }
                    if (!canFetch) ImGui::EndDisabled();
                    ImGui::SameLine();
                    if (ImGui::Button("Clear (use built-in)"))
                        std::fill(s_spBuf.begin(), s_spBuf.end(), 0);

                    ImGui::Dummy(ImVec2(0, 8 * sc));
                    ImGui::SeparatorText("Privacy");
                    {
                        int consent = ui::currentConsentIndex();
                        bool changed = false;
                        for (int i = 0; i < 3; ++i)
                            changed |= ImGui::RadioButton(ui::consentLabel(i), &consent, i);
                        if (changed) ui::setConsentIndex(consent);
                    }

                    ImGui::Dummy(ImVec2(0, 8 * sc));
                    ImGui::SeparatorText("Runtime");
                    const bool ready = runtimeReady();
                    ImGui::TextColored(ready ? TVEC(good) : TVEC(warn),
                                       "%s", ready ? "Runtime ready" : "Runtime not installed");
                    const bool repairing = ui::runtimeRepairInProgress();
                    if (repairing) ImGui::BeginDisabled();
                    if (ImGui::Button(ready ? "Repair Runtime" : "Set Up Runtime"))
                        ui::triggerRuntimeRepair(true);
                    if (repairing) { ImGui::EndDisabled(); ImGui::SameLine(); ImGui::TextDisabled("working..."); }

                    ImGui::EndTabItem();
                }
                if (ImGui::BeginTabItem("Experimental", nullptr,
                                        jumpToTab == 4 ? ImGuiTabItemFlags_SetSelected
                                                       : ImGuiTabItemFlags_None)) {
                    ImGui::Dummy(ImVec2(0, 2 * sc));
                    dimText("Opt-in features from model research. Default-off and additive.");
                    ImGui::Dummy(ImVec2(0, 4 * sc));
                    ImGui::SeparatorText("Model routing");
                    ImGui::TextUnformatted("Command model (sends & parses CAD ops)");
                    {
                        char cm[600];
                        std::snprintf(cm, sizeof(cm), "   %s  ·  %s",
                                      providerLabel(settings.provider),
                                      settings.model[0] ? settings.model : "(default)");
                        ImGui::TextColored(TVEC(accent), "%s", cm);
                    }
                    ImGui::Dummy(ImVec2(0, 6 * sc));
                    ImGui::Checkbox("Route vision & spatial reasoning through a separate model",
                                    &settings.expVisionReview);
                    if (settings.expVisionReview) {
                        std::vector<const char*> vlabels;
                        std::vector<const LocalPreset*> vps;
                        vlabels.push_back("(custom / type below)");
                        vps.push_back(nullptr);
                        for (const auto& p : cloudPresets())
                            if (p.vision) { vlabels.push_back(p.label.c_str()); vps.push_back(&p); }
                        for (const auto& p : localPresets())
                            if (p.vision) { vlabels.push_back(p.label.c_str()); vps.push_back(&p); }
                        int vsel = 0;
                        for (int i = 1; i < (int)vps.size(); ++i)
                            if (vps[i] && vps[i]->id == settings.expVisionModel) vsel = i;
                        ImGui::SetNextItemWidth(-1);
                        if (ImGui::Combo("##expvispreset", &vsel, vlabels.data(), (int)vlabels.size()) &&
                            vsel > 0 && vps[vsel]) {
                            const LocalPreset* p = vps[vsel];
                            std::strncpy(settings.expVisionModel, p->id.c_str(),
                                         sizeof(settings.expVisionModel) - 1);
                            std::strncpy(settings.expVisionBaseUrl, p->baseUrl.c_str(),
                                         sizeof(settings.expVisionBaseUrl) - 1);
                            bool isCloud = std::strcmp(cloudServiceKey(p->baseUrl), "cloud") != 0;
                            std::strncpy(settings.expVisionKey,
                                         isCloud && settings.cloudKey[0] ? settings.cloudKey : "",
                                         sizeof(settings.expVisionKey) - 1);
                        }
                        ImGui::TextUnformatted("Model id");
                        ImGui::SetNextItemWidth(-1);
                        ImGui::InputText("##expvismodel", settings.expVisionModel,
                                         sizeof(settings.expVisionModel));
                        ImGui::TextUnformatted("Endpoint (base URL)");
                        ImGui::SetNextItemWidth(-1);
                        ImGui::InputText("##expvisbase", settings.expVisionBaseUrl,
                                         sizeof(settings.expVisionBaseUrl));
                        ImGui::TextUnformatted("API key (blank for local)");
                        ImGui::SetNextItemWidth(-1);
                        ImGui::InputText("##expviskey", settings.expVisionKey,
                                         sizeof(settings.expVisionKey),
                                         ImGuiInputTextFlags_Password);
                    }
                    ImGui::Dummy(ImVec2(0, 8 * sc));
                    ImGui::SeparatorText("Parametric sequence mode (experimental)");
                    ImGui::Checkbox("Prefer an editable feature sequence over a monolithic solid",
                                    &settings.expCadSequenceMode);
                    dimText("Keeps generated parts editable as a real feature tree.");
                    ImGui::EndTabItem();
                }
                ImGui::EndTabBar();
            }
            ImGui::EndChild();
            ImGui::PopStyleColor();

            if (primaryButton("Save", ImVec2(120.f * sc, 34.f * sc))) {
                settings.systemPrompt = s_spBuf.data();
                saveAppSettings(settings);
                st.status = "[ok] saved settings";
                s_settingsOpen = false;
            }
            ImGui::SameLine();
            if (ImGui::Button("Cancel", ImVec2(100.f * sc, 34.f * sc))) {
                std::fill(s_spBuf.begin(), s_spBuf.end(), 0);
                std::strncpy(s_spBuf.data(), settings.systemPrompt.c_str(), s_spBuf.size() - 1);
                s_settingsOpen = false;
            }
        }
        ImGui::End();
        theme::popGlassWindowStyle();
    }

    if (s_ui.showAbout) {
        ImGui::OpenPopup("About ShopCraft");
        s_ui.showAbout = false;
    }
    ImGui::SetNextWindowSize(ImVec2(380.f * sc, 0), ImGuiCond_Appearing);
    if (ImGui::BeginPopupModal("About ShopCraft", nullptr, ImGuiWindowFlags_NoResize)) {
        ImGui::PushFont(theme::font(theme::Font::H1));
        ImGui::TextColored(TVEC(txt0), "ShopCraft");
        ImGui::PopFont();
        dimText("Parametric CAD, driven by AI.");
        ImGui::Dummy(ImVec2(0, 4 * sc));
        ImGui::TextUnformatted("build123d + OpenCASCADE kernel");
        ImGui::TextUnformatted("GLFW / Dear ImGui / OpenGL");
        ImGui::Dummy(ImVec2(0, 8 * sc));
        if (primaryButton("Close", ImVec2(110.f * sc, 32.f * sc))) ImGui::CloseCurrentPopup();
        ImGui::EndPopup();
    }
}

}  // namespace ui
