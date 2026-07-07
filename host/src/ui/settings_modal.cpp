// AI-CAD Settings modal (Appearance / General / AI / Prompts / Agent / Experimental tabs) and
// the About modal.
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
#include "legacy_chat.h"  // ui::drawProviderModelPicker (shared with the inline AI tab)
#include "model_registry.h"  // ui::localPresets (experimental vision-review model presets)

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
    // get_prompts_v2 returns a BARE JSON array of prompt records; tolerate a legacy
    // {"prompts":[...]} envelope too. Calling value("prompts",...) on a bare array would
    // throw type_error.306, and a present-but-null string field would throw 302 — either
    // crashes the Settings > Agents tab. Read every field defensively.
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

// Apply a dark/light theme immediately (settings.theme persists on Save) and sync the viewport
// background. Shared by the General-tab theme control. MUST pass st.scale: applyTheme re-seeds all
// style-vars to base and only multiplies them by the scale argument (ScaleAllSizes) — passing 1.0f
// on a HiDPI display collapses every padding/rounding to 100% while the pre-scaled fonts stay large,
// and clobbers ui::uiScale, desyncing every custom-drawn widget. (Was hardcoded 1.0f — bug.)
static void applyThemeNow(AppState& st, bool light) {
    ui::Theme next = light ? ui::Theme::Light : ui::Theme::Dark;
    ui::applyTheme(next, st.scale);
    st.settings.theme = light ? "light" : "dark";
    // Persist immediately: the checkbox is labeled "saved immediately" and the View-menu copy
    // already saves on toggle — without this, toggle-then-Cancel applied the theme in memory
    // but lost it on restart (audit P1).
    saveSettings(st.settings);
    if (st.renderer)
        st.renderer->setBackground(
            ui::palette.viewportTop[0], ui::palette.viewportTop[1], ui::palette.viewportTop[2],
            ui::palette.viewportBottom[0], ui::palette.viewportBottom[1],
            ui::palette.viewportBottom[2]);
}

// ---- Connected Accounts: subscription-auth logins (Codex / Claude / Cursor, §4.4) ----
// Sign in with the user's own AI subscription so the agent runs on their plan instead of a
// metered API key. The Python worker seam (subscription_status/login/logout) starts the browser
// OAuth flow on a BACKGROUND thread — subscription_login returns immediately with in_progress:true.
// We therefore never block the GL thread: fire the fast RPC, then poll subscription_status on a
// ~2s frame-time backoff (same non-blocking cadence the Parts vendor panel uses) until the state
// flips to connected.
struct SubProvider {
    std::string key;       // "codex" / "claude" / "cursor"
    std::string label;     // e.g. "OpenAI Codex (ChatGPT Plus/Pro)"
    bool        connected = false;
    std::string email;
    bool        loginInProgress = false;
    std::string lastError;
};
static std::vector<SubProvider> s_subProviders;
static bool   s_subLoaded   = false;
static double s_subLastPoll = 0.0;
static bool   s_subCodexDevice = false;  // Codex-only headless device-code flow
static std::string s_subError;           // top-level fetch/RPC error

static void loadSubscriptionStatus(AppState& st) {
    nlohmann::json result;
    std::string err;
    if (!st.worker.call("subscription_status", nlohmann::json::object(), result, err, 8000)) {
        s_subError = err;
        return;
    }
    s_subError.clear();
    s_subLastPoll = ImGui::GetTime();
    s_subLoaded   = true;
    auto str = [](const nlohmann::json& j, const char* k) -> std::string {
        auto it = j.find(k);
        return (it != j.end() && it->is_string()) ? it->get<std::string>() : std::string();
    };
    if (!result.is_object() || !result.value("ok", false)) return;
    auto pit = result.find("providers");
    if (pit == result.end() || !pit->is_object()) return;
    // Fixed display order; skip any provider the backend doesn't report.
    static const char* kOrder[] = {"codex", "claude", "cursor"};
    s_subProviders.clear();
    for (const char* key : kOrder) {
        auto vit = pit->find(key);
        if (vit == pit->end() || !vit->is_object()) continue;
        const nlohmann::json& p = *vit;
        SubProvider sp;
        sp.key             = key;
        sp.label           = str(p, "label");
        if (sp.label.empty()) sp.label = key;
        sp.connected       = p.value("connected", false);
        sp.email           = str(p, "email");
        sp.loginInProgress = p.value("login_in_progress", false);
        sp.lastError       = str(p, "last_error");
        s_subProviders.push_back(std::move(sp));
    }
}

static void subscriptionLogin(AppState& st, const std::string& key, bool device) {
    nlohmann::json params;
    params["provider"] = key;
    params["device"]   = device;
    nlohmann::json result;
    std::string err;
    // Fire-and-forget: returns immediately (in_progress:true); a short timeout never blocks.
    if (!st.worker.call("subscription_login", params, result, err, 8000)) {
        s_subError = "login: " + err;
        return;
    }
    loadSubscriptionStatus(st);  // refresh so the spinner shows on the next frame
}

static void subscriptionLogout(AppState& st, const std::string& key) {
    nlohmann::json params;
    params["provider"] = key;
    nlohmann::json result;
    std::string err;
    if (!st.worker.call("subscription_logout", params, result, err, 8000)) {
        s_subError = "logout: " + err;
        return;
    }
    loadSubscriptionStatus(st);
}

// One row per provider: label + status, a Connect/Disconnect button, an optional Codex device-code
// checkbox, and any last_error in red. Drawn inside the AI tab (see below).
static void drawConnectedAccounts(AppState& st, bool busy) {
    const float sc = st.scale;
    ImGui::Dummy(ImVec2(0, 8 * sc));
    ImGui::SeparatorText("Connected Accounts");
    dimText("Sign in with your own AI subscription so the agent runs on your plan instead of a "
            "metered API key. Opens a browser to complete sign-in \xe2\x80\x94 nothing is blocked "
            "while you authorize.");
    ImGui::Dummy(ImVec2(0, 4 * sc));

    const bool canFetch = st.workerStarted && !busy;
    if (!s_subLoaded && canFetch) loadSubscriptionStatus(st);

    // Non-blocking poll: while any login is mid-flight, re-fetch status every ~2s so the
    // "Waiting for browser…" state flips to Connected on its own.
    bool anyBusy = false;
    for (auto& p : s_subProviders) if (p.loginInProgress) anyBusy = true;
    if (anyBusy && canFetch && ImGui::GetTime() - s_subLastPoll > 2.0)
        loadSubscriptionStatus(st);

    if (!s_subLoaded) {
        dimText("Worker not ready \xe2\x80\x94 connected accounts appear once the runtime starts.");
        return;
    }
    if (!s_subError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.90f, 0.35f, 0.35f, 1));
        ImGui::TextWrapped("Status error: %s", s_subError.c_str());
        ImGui::PopStyleColor();
    }
    if (s_subProviders.empty()) {
        dimText("No subscription providers available.");
        return;
    }

    for (auto& p : s_subProviders) {
        ImGui::PushID(p.key.c_str());
        ImGui::TextUnformatted(p.label.c_str());
        ImGui::SameLine();
        if (p.loginInProgress) {
            ImGui::TextColored(ImVec4(0.45f, 0.75f, 0.95f, 1), "  Waiting for browser\xe2\x80\xa6");
        } else if (p.connected) {
            ImGui::TextColored(ImVec4(0.40f, 0.80f, 0.45f, 1), "  \xe2\x97\x8f Connected");
            if (!p.email.empty()) {
                ImGui::SameLine();
                ImVec4 c = ImGui::GetStyleColorVec4(ImGuiCol_Text);
                c.w *= 0.62f;
                ImGui::PushStyleColor(ImGuiCol_Text, c);
                ImGui::Text("(%s)", p.email.c_str());
                ImGui::PopStyleColor();
            }
        } else {
            ImVec4 c = ImGui::GetStyleColorVec4(ImGuiCol_Text);
            c.w *= 0.62f;
            ImGui::PushStyleColor(ImGuiCol_Text, c);
            ImGui::TextUnformatted("  Not connected");
            ImGui::PopStyleColor();
        }
        // Action button
        if (p.connected) {
            if (ImGui::SmallButton("Disconnect")) subscriptionLogout(st, p.key);
        } else if (!p.loginInProgress) {
            const bool device = (p.key == "codex") ? s_subCodexDevice : false;
            if (ImGui::SmallButton("Connect")) subscriptionLogin(st, p.key, device);
            // Codex-only headless flow for when the OAuth loopback port is busy.
            if (p.key == "codex") {
                ImGui::SameLine();
                ImGui::Checkbox("use device code", &s_subCodexDevice);
                helpMarker("Headless code-based sign-in for when the local OAuth port (1455) is "
                           "busy. Codex only.");
            }
        }
        // Per-provider error, in red.
        if (!p.lastError.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.90f, 0.35f, 0.35f, 1));
            ImGui::TextWrapped("  %s", p.lastError.c_str());
            ImGui::PopStyleColor();
        }
        ImGui::Dummy(ImVec2(0, 4 * sc));
        ImGui::PopID();
    }
}
}  // anonymous namespace

// ---- kSettingsEntries: unified command search table (command_search.cpp) ----
// Tab indices: 0=Appearance  1=General  2=AI  3=Prompts  4=Agent  5=Experimental
// One entry per prominent field/section; labels mirror the modal's visible text.
const SettingsEntry kSettingsEntries[] = {
    // Appearance (tab 0)
    { "Theme",                    "Appearance",   0 },
    { "Light theme",              "Appearance",   0 },
    { "Dark theme",               "Appearance",   0 },
    { "Liquid glass",             "Appearance",   0 },
    { "Glass tint",               "Appearance",   0 },
    { "Tint strength",            "Appearance",   0 },
    { "Glass level",              "Appearance",   0 },
    { "Glass opacity",            "Appearance",   0 },
    { "Corner rounding",          "Appearance",   0 },
    { "Text contrast",            "Appearance",   0 },
    { "Dynamic color",            "Appearance",   0 },
    // General (tab 1)
    { "Units",                    "General",      1 },
    { "MMGS units",               "General",      1 },
    { "IPS units",                "General",      1 },
    { "Workspace folder",         "General",      1 },
    { "Engine routing",           "General",      1 },
    { "C++ core",                 "General",      1 },
    { "Performance profiler",     "General",      1 },
    { "Profiler",                 "General",      1 },
    // AI (tab 2)
    { "AI provider",              "AI",           2 },
    { "Model",                    "AI",           2 },
    { "API key",                  "AI",           2 },
    { "Gemini key",               "AI",           2 },
    { "Anthropic key",            "AI",           2 },
    { "OpenAI key",               "AI",           2 },
    { "Cloud API key",            "AI",           2 },
    { "Vision",                   "AI",           2 },
    { "Offline mode",             "AI",           2 },
    { "Reasoning effort",         "AI",           2 },
    { "Max response tokens",      "AI",           2 },
    { "Model context window",     "AI",           2 },
    { "Local endpoint",           "AI",           2 },
    { "Manage models",            "AI",           2 },
    { "Hidden models",            "AI",           2 },
    // Prompts (tab 3) — role prompts + the system-prompt override (moved from Advanced)
    { "Role prompts",             "Prompts",      3 },
    { "Agent prompts",            "Prompts",      3 },
    { "Prompt editor",            "Prompts",      3 },
    { "System prompt",            "Prompts",      3 },
    { "System prompt override",   "Prompts",      3 },
    // Agent (tab 4) — run behavior (was the Advanced tab)
    { "Plan before building",     "Agent",        4 },
    { "Plan mode",                "Agent",        4 },
    { "Refine-pass cap",          "Agent",        4 },
    { "Max iters",                "Agent",        4 },
    { "Iteration budget",         "Agent",        4 },
    // Experimental (tab 5)
    { "Vision review model",      "Experimental", 5 },
    { "Model routing",            "Experimental", 5 },
    { "Seek-CAD split",           "Experimental", 5 },
    { "Parametric sequence mode", "Experimental", 5 },
    { "CAD sequence mode",        "Experimental", 5 },
    { "Feature sequence",         "Experimental", 5 },
    // Team & Sync (tab 6) — moved out of the Planning page
    { "Team",                     "Team & Sync",  6 },
    { "Team & Sync",              "Team & Sync",  6 },
    { "Sync",                     "Team & Sync",  6 },
    { "Shared folder",            "Team & Sync",  6 },
    { "Collaborate",              "Team & Sync",  6 },
};
const int kSettingsEntryCount =
    (int)(sizeof(kSettingsEntries) / sizeof(kSettingsEntries[0]));


namespace ui {

void drawSettingsModal(AppState& st, bool busy) {
    Settings& settings = st.settings;

    // ---------- Settings window ----------
    static bool s_settingsOpen = false;
    // On-open snapshot for Cancel: the AI-tab picker and the Agent/Experimental checkboxes edit
    // st.settings live, so Cancel restores this copy (real revert semantics — audit P1). The
    // immediate-persist preferences (units / theme / glass) are re-kept on Cancel below.
    static Settings s_snapshot;
    bool opening = false;
    if (st.openSettings) {
        if (!s_settingsOpen) opening = true;
        s_settingsOpen = true;
        st.openSettings = false;
    }
    if (st.openSettingsTab >= 0 && !s_settingsOpen) { opening = true; s_settingsOpen = true; }
    // Workspace-folder edit buffer (General tab), reseeded from Settings on every open.
    static std::vector<char> s_wsBuf(512, 0);
    if (opening) {
        s_snapshot = settings;
        std::fill(s_wsBuf.begin(), s_wsBuf.end(), 0);
        std::strncpy(s_wsBuf.data(), settings.workspaceDir.c_str(), s_wsBuf.size() - 1);
        // Make sure the glass engine reflects the persisted config before the Appearance
        // preview draws (idempotent — app startup wiring may already have pushed it).
        ui::setGlassConfig(settings.glassCfg);
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
        // Opaque title strip — the global applyTheme sets TitleBg* to rgba(0,0,0,0) so floating
        // glass windows render transparently by default.  Override here so the Settings dialog
        // reads as a crisp native chrome dialog in both light and dark themes.
        ImGui::PushStyleColor(ImGuiCol_TitleBg,
            ImVec4(ui::palette.headerBg[0], ui::palette.headerBg[1],
                   ui::palette.headerBg[2], 1.0f));
        ImGui::PushStyleColor(ImGuiCol_TitleBgActive,
            ImVec4(ui::palette.headerBg[0], ui::palette.headerBg[1],
                   ui::palette.headerBg[2], 1.0f));
        ImGui::PushStyleColor(ImGuiCol_TitleBgCollapsed,
            ImVec4(ui::palette.headerBg[0], ui::palette.headerBg[1],
                   ui::palette.headerBg[2], 1.0f));
        bool settingsBegun = ImGui::Begin("Settings", &s_settingsOpen,
                         ImGuiWindowFlags_NoResize | ImGuiWindowFlags_NoDocking);
        ImGui::PopStyleColor(3);  // TitleBg / TitleBgActive / TitleBgCollapsed
        if (settingsBegun) {
        // C22b: if a pending prompt role was requested, load prompts and jump to the Prompts tab.
        bool jumpToPrompts = false;
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
            jumpToPrompts = true;
        }
        // openSettingsTab: jump to a specific tab requested by the command search widget.
        int jumpToTab = -1;
        if (st.openSettingsTab >= 0) {
            jumpToTab          = st.openSettingsTab;
            st.openSettingsTab = -1;
        }

        // Tab content scrolls in its own region (a subtle glass card); Save/Cancel stay pinned
        // at the bottom.
        const float footerH = ImGui::GetFrameHeightWithSpacing() + 20.0f * sc;

        // AI_CAD_SHOT=settings selects the Agent category ONE-SHOT: consuming
        // st.wantSettings every frame would relock the other cells all session.
        static bool s_wantSettingsTabDone = false;
        bool wantSettingsSel = st.wantSettings && !s_wantSettingsTabDone;
        if (wantSettingsSel) s_wantSettingsTabDone = true;

        // ---- Category grid nav (replaces the old horizontal tab bar) — a tidy grid
        //      of cells that scales to more categories for bigger teams/projects.
        //      The grid itself never scrolls; only the content region below does.
        //      Indices stay stable (Experimental=5) so kSettingsEntries + openSettingsTab
        //      keep working; Team & Sync is appended as 6 and just placed before
        //      Experimental in the visual order. ----------------------------------
        struct SetCat { const char* label; int idx; };
        static const SetCat kCats[] = {
            {"Appearance", 0}, {"General", 1}, {"AI", 2}, {"Prompts", 3},
            {"Agent", 4}, {"Team & Sync", 6}, {"Experimental", 5},
        };
        static int s_setCat = 0;
        if (jumpToTab >= 0)  s_setCat = jumpToTab;
        if (jumpToPrompts)   s_setCat = 3;
        if (wantSettingsSel) s_setCat = 4;
        {
            const int   cols = 4;
            const float gg   = 8.0f * sc;
            const float gw   = ImGui::GetContentRegionAvail().x;
            const float cw   = std::floor((gw - gg * (cols - 1)) / cols);
            const float ch   = 34.0f * sc;
            ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(gg, gg));
            ImGui::PushStyleVar(ImGuiStyleVar_SelectableTextAlign, ImVec2(0.5f, 0.5f));
            for (int i = 0; i < (int)IM_ARRAYSIZE(kCats); ++i) {
                if (i % cols) ImGui::SameLine();
                bool selc = (s_setCat == kCats[i].idx);
                if (selc) {
                    ImGui::PushStyleColor(ImGuiCol_Header, ImVec4(ui::palette.accent[0],
                        ui::palette.accent[1], ui::palette.accent[2], 0.55f));
                    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, ImVec4(ui::palette.accent[0],
                        ui::palette.accent[1], ui::palette.accent[2], 0.65f));
                    ImGui::PushStyleColor(ImGuiCol_HeaderActive, ImVec4(ui::palette.accent[0],
                        ui::palette.accent[1], ui::palette.accent[2], 0.75f));
                }
                if (ImGui::Selectable(kCats[i].label, selc, ImGuiSelectableFlags_None,
                                      ImVec2(cw, ch)))
                    s_setCat = kCats[i].idx;
                if (selc) ImGui::PopStyleColor(3);
            }
            ImGui::PopStyleVar(2);
        }
        ImGui::Dummy(ImVec2(0, 8 * sc));

        // Content region for the selected category — the ONLY part that scrolls.
        pushGlassChildStyle(10.0f);
        ImGui::BeginChild("##settabsarea",
                          ImVec2(0, std::max(120.0f * sc,
                                             ImGui::GetContentRegionAvail().y - footerH)));
        {
            // ---- Appearance tab (FIRST): theme + colored liquid glass (v2.1) ----
            // The canonical theme control lives here (moved from General; the View-menu item
            // stays a synced shortcut). The glass knobs edit Settings::glassCfg per group and
            // live-apply via ui::setGlassConfig + persist via saveSettings on every change —
            // so the persisted "appearance.glass" block is no longer a dead round-trip.
            if (s_setCat == 0) {
                ImGui::Dummy(ImVec2(0, 2 * sc));
                ImGui::SeparatorText("Theme");
                bool isLight = (ui::activeTheme == ui::Theme::Light);
                if (ImGui::Checkbox("Light theme (SolidWorks-style)", &isLight))
                    applyThemeNow(st, isLight);
                dimText("Switches the whole UI between the dark and light palettes. "
                        "Saved immediately.");

                ImGui::Dummy(ImVec2(0, 8 * sc));
                ImGui::SeparatorText("Liquid Glass");
                dimText("Per-surface styling for the glass panels floating over the viewport. "
                        "Dynamic color auto-tints each panel for max contrast against the live "
                        "model; pin a tint below to override it. Saved immediately.");
                ImGui::Dummy(ImVec2(0, 4 * sc));

                // Group selector — segmented pills. Global is the inheritance base: the other
                // groups fall back to it for any knob left at its default ("Reset" state).
                static int s_glassGroup = 0;
                static const char* kGroupLabel[(size_t)ui::GlassGroup::Count] = {
                    "Global", "Feature Manager", "Copilot", "Viewport HUD"};
                for (int i = 0; i < (int)ui::GlassGroup::Count; ++i) {
                    if (i) ImGui::SameLine(0.0f, 6.0f * sc);
                    bool selGrp = (s_glassGroup == i);
                    const float* bc = selGrp ? ui::palette.accentSoft : ui::palette.button;
                    ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(bc[0], bc[1], bc[2], bc[3]));
                    if (ImGui::Button(kGroupLabel[i])) s_glassGroup = i;
                    ImGui::PopStyleColor();
                }

                const ui::GlassGroup grp = (ui::GlassGroup)s_glassGroup;
                ui::GlassStyle& g = settings.glassCfg.group[(size_t)grp];
                // Seed every widget from the group's EFFECTIVE (resolved) values so inherited
                // knobs display what actually renders; an edit pins a concrete value.
                const ui::GlassStyleResolved& res = ui::glassStyleResolved(grp);
                bool glassChanged = false;

                // Aligned label column (uiScale-correct).
                const float labelW = 150.0f * sc;
                const float fieldW = 230.0f * sc;
                auto fieldRow = [&](const char* label) {
                    ImGui::AlignTextToFramePadding();
                    ImGui::TextUnformatted(label);
                    ImGui::SameLine(labelW);
                    ImGui::SetNextItemWidth(fieldW);
                };

                ImGui::Dummy(ImVec2(0, 6 * sc));
                // Tint is hue+saturation only (brightness comes from the glass level/opacity),
                // so edit it through an HSV swatch with the value channel pinned at 1.
                fieldRow("Tint color");
                {
                    float rgb[3];
                    ImGui::ColorConvertHSVtoRGB(res.tintHue / 360.0f, res.tintSat, 1.0f,
                                                rgb[0], rgb[1], rgb[2]);
                    if (ImGui::ColorEdit3("##glasstint", rgb, ImGuiColorEditFlags_NoInputs)) {
                        float h, s2, v;
                        ImGui::ColorConvertRGBtoHSV(rgb[0], rgb[1], rgb[2], h, s2, v);
                        g.tintHue = h * 360.0f;
                        g.tintSat = s2;
                        glassChanged = true;
                    }
                    ImGui::SameLine();
                    dimText("(hue + saturation; strength below mixes it in)");
                }
                // Slider VALUES render beside the track (not centered inside it) — the teal grab
                // sat right on top of the in-track text and obscured it (visual-review fix).
                auto sliderValueText = [&](const char* txt) {
                    ImGui::SameLine(0.0f, 8.0f * sc);
                    ImGui::AlignTextToFramePadding();
                    ImGui::TextUnformatted(txt);
                };
                fieldRow("Tint strength");
                {
                    float v = res.tintStrength;
                    if (ImGui::SliderFloat("##glassstrength", &v, 0.0f, 1.0f, "")) {
                        g.tintStrength = v;
                        glassChanged = true;
                    }
                    char vb[32];
                    std::snprintf(vb, sizeof(vb), "%.2f", v);
                    sliderValueText(v <= 0.0f ? "off" : vb);
                }
                fieldRow("Glass level");
                {
                    static const char* kLevels[] = {"Automatic", "Ultra thin", "Thin",
                                                    "Regular", "Thick"};
                    int lv = res.glassLevel + 1;  // -1 (auto) .. 3 -> 0 .. 4
                    if (lv < 0 || lv > 4) lv = 0;
                    if (ImGui::Combo("##glasslevel", &lv, kLevels, 5)) {
                        g.glassLevel = lv - 1;
                        glassChanged = true;
                    }
                }
                fieldRow("Opacity");
                {
                    float v = res.opacityMul;
                    if (ImGui::SliderFloat("##glassopacity", &v, 0.25f, 2.0f, "")) {
                        g.opacityMul = v;
                        glassChanged = true;
                    }
                    char vb[32];
                    std::snprintf(vb, sizeof(vb), "%.2fx", v);
                    sliderValueText(vb);
                }
                fieldRow("Corner rounding");
                {
                    float v = res.rounding < 0.0f ? 12.0f : res.rounding;
                    if (ImGui::SliderFloat("##glassround", &v, 0.0f, 24.0f, "")) {
                        g.rounding = v;
                        glassChanged = true;
                    }
                    char vb[32];
                    std::snprintf(vb, sizeof(vb), "%.0f dp", v);
                    sliderValueText(res.rounding < 0.0f ? "automatic" : vb);
                }
                fieldRow("Text contrast boost");
                {
                    float v = res.textContrastBoost;
                    if (ImGui::SliderFloat("##glassboost", &v, 0.0f, 1.0f, "")) {
                        g.textContrastBoost = v;
                        glassChanged = true;
                    }
                    char vb[32];
                    std::snprintf(vb, sizeof(vb), "%.2f", v);
                    sliderValueText(v <= 0.0f ? "off" : vb);
                }
                ImGui::Dummy(ImVec2(0, 2 * sc));
                bool dyn = res.dynamicColor;
                if (ImGui::Checkbox("Dynamic color \xe2\x80\x94 auto max-contrast vs model",
                                    &dyn)) {
                    g.dynamicColor = dyn ? 1 : 0;
                    glassChanged = true;
                }
                dimText("On (default): each panel samples the live viewport and steers its tint "
                        "for legibility. Off: the pinned tint above is used as-is.");

                ImGui::Dummy(ImVec2(0, 4 * sc));
                if (ImGui::Button("Reset to defaults", ImVec2(150.0f * sc, 28.0f * sc))) {
                    g = ui::GlassStyle();  // all sentinels -> inherit Global / built-in defaults
                    glassChanged = true;
                }
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip("Restore this group to inherited (Global / built-in) values");

                if (glassChanged) {
                    ui::setGlassConfig(settings.glassCfg);  // live-apply (idempotent)
                    saveSettings(settings);                 // glass knobs persist immediately
                }

                // Live preview through the ACTUAL glass pipeline (adaptivePanel + glassPanel):
                // over the viewport it frosts the real backdrop with this group's resolved style;
                // over chrome it falls back to the layered glass. One stable id per group (the
                // adaptive cache requires the same group every frame for a given id).
                ImGui::Dummy(ImVec2(0, 6 * sc));
                ImGui::SeparatorText("Preview");
                {
                    static const char* kPreviewId[(size_t)ui::GlassGroup::Count] = {
                        "settings_glass_preview_global", "settings_glass_preview_fm",
                        "settings_glass_preview_copilot", "settings_glass_preview_hud"};
                    ImVec2 mn = ImGui::GetCursorScreenPos();
                    float pw = std::max(220.0f * sc, ImGui::GetContentRegionAvail().x);
                    float ph = 64.0f * sc;
                    ImRect pr(mn, ImVec2(mn.x + pw, mn.y + ph));
                    const ui::AdaptiveSurface& surf =
                        ui::adaptivePanel(ui::panelId(kPreviewId[s_glassGroup]), pr, grp);
                    ImDrawList* dl = ImGui::GetWindowDrawList();
                    ui::glassPanel(dl, pr, ui::GlassLevel::Regular, 12.0f, surf);
                    ImFont* pf = st.fonts.medium ? st.fonts.medium : ImGui::GetFont();
                    float fs = ImGui::GetFontSize();
                    ui::glassText(dl, pf, fs, ImVec2(mn.x + 14.0f * sc, mn.y + 12.0f * sc),
                                  surf, kGroupLabel[s_glassGroup]);
                    dl->AddText(pf, fs * 0.88f,
                                ImVec2(mn.x + 14.0f * sc, mn.y + 12.0f * sc + fs * 1.35f),
                                ImGui::ColorConvertFloat4ToU32(surf.textSecondary),
                                "Adaptive text over this group's glass");
                    ImGui::Dummy(ImVec2(pw, ph));
                }
            }

            // ---- General category: Document (units) + System (engine readout, profiler) ----
            if (s_setCat == 1) {
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

                // Workspace folder (audit P1): workspace_dir was persisted but had NO UI —
                // users had to hand-edit config.json to re-root the File Explorer.
                ImGui::Dummy(ImVec2(0, 8 * sc));
                ImGui::SeparatorText("Workspace");
                ImGui::TextUnformatted("Workspace folder");
                helpMarker("The File Explorer's root folder and the default save location. "
                           "Leave empty to use Documents/AI-CAD. Applied and saved with the "
                           "Apply button.");
                ImGui::SetNextItemWidth(-110.0f * sc);
                ImGui::InputTextWithHint("##wsdir", "(default: Documents/AI-CAD)",
                                         s_wsBuf.data(), s_wsBuf.size());
                ImGui::SameLine();
                if (ImGui::Button("Apply", ImVec2(100.0f * sc, 0))) {
                    settings.workspaceDir = s_wsBuf.data();
                    saveSettings(settings);
                    // Re-resolve the live root: empty falls back to Documents/AI-CAD inside
                    // the Explorer's ensureWorkspaceDir.
                    st.workspaceDir = settings.workspaceDir;
                    st.addLog("[ok] workspace folder set: " +
                              (settings.workspaceDir.empty() ? std::string("(default)")
                                                             : settings.workspaceDir));
                }
                dimText("Root of the File Explorer and default save location (saved on Apply).");

                // Theme moved to the Appearance tab (the canonical control); engine routing +
                // profiler are status/launchers, folded under one System section (audit P2).
                ImGui::Dummy(ImVec2(0, 8 * sc));
                ImGui::SeparatorText("System");
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
                    const bool docsOn = flagOn("AI_CAD_CPP_DOCS", true);
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

                ImGui::Dummy(ImVec2(0, 8 * sc));
                dimText("Sampling profiler \xe2\x80\x94 see where modeling time goes (extrude / "
                        "fillet / boolean, into the kernel) as a live flame graph.");
                if (ImGui::Button("Open Profiler", ImVec2(160.0f * sc, 30.0f * sc)))
                    st.showProfilerWindow = true;
                ImGui::Dummy(ImVec2(0, 6 * sc));
                dimText(("All settings here are stored in " + configPath() + ".").c_str());
            }
            if (s_setCat == 2) {
                ImGui::Dummy(ImVec2(0, 2 * sc));
                ImGui::SeparatorText("Provider & model");
                dimText(("Used by Design, Edit, and Help. Stored in " + configPath() + ".")
                            .c_str());
                ImGui::Dummy(ImVec2(0, 4 * sc));
                // Shared provider + availability-filtered model picker + reasoning effort +
                // privacy/offline. The inline AI Settings tab renders the SAME widget.
                ui::drawProviderModelPicker(st, busy);
                // Subscription-auth logins (use your own ChatGPT/Claude/Cursor plan).
                drawConnectedAccounts(st, busy);
            }
            // --- C22b: Prompts category (role prompt editor + system-prompt override) ---
            if (s_setCat == 3) {
                ImGui::Dummy(ImVec2(0, 2 * sc));
                bool canFetch = st.workerStarted && !busy;
                if (!s_promptsLoaded && canFetch) loadPrompts(st);
                if (!s_promptsLoaded)
                    dimText("Worker not ready \xe2\x80\x94 role prompts appear once the runtime "
                            "starts.");
                // Left column: role list + the system-prompt override pseudo-entry (moved here
                // from the old Advanced tab — it IS a prompt; audit P1). sysIdx = one past the
                // roles = the override editor.
                const int sysIdx = (int)s_promptRecs.size();
                float listW = 150.0f * sc;
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
                    if (ImGui::IsItemHovered())  // long names truncate in the narrow list
                        ImGui::SetTooltip("%s%s", pr.role.c_str(),
                                          pr.overridden ? " (overridden)" : "");
                }
                if (!s_promptRecs.empty()) ImGui::Separator();
                {
                    std::string syslbl = "system (override)";
                    if (st.spBuf[0] != 0) syslbl += " *";
                    if (ImGui::Selectable(syslbl.c_str(), s_selectedRole >= sysIdx))
                        s_selectedRole = sysIdx;
                    if (ImGui::IsItemHovered())
                        ImGui::SetTooltip("Override the agent's system prompt (advanced)");
                }
                ImGui::EndChild();
                ImGui::PopStyleColor();

                ImGui::SameLine();

                // Right column: editor + diff indicator
                ImGui::BeginChild("##roleeditor", ImVec2(0, avail - 38.0f * sc),
                                  ImGuiChildFlags_None);
                if (s_selectedRole >= sysIdx) {
                    // System prompt override (applied by the footer Save; empty = built-in).
                    ImGui::PushFont(st.fonts.medium);
                    ImGui::TextUnformatted("System prompt override");
                    ImGui::PopFont();
                    dimText("Leave empty to use the built-in prompt (recommended). A custom "
                            "prompt fully replaces it - keep the `result`/`parts` contract "
                            "and the python+json two-block reply format or the agent breaks. "
                            "Applied by the Save button below.");
                    ImGui::PushFont(st.fonts.mono);
                    float edH = ImGui::GetContentRegionAvail().y - 38.0f * sc;
                    if (edH < 100.0f * sc) edH = 100.0f * sc;
                    ImGui::InputTextMultiline("##sysprompt", st.spBuf.data(), st.spBuf.size(),
                                              ImVec2(-1, edH));
                    ImGui::PopFont();
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
                } else if (s_selectedRole < (int)s_promptRecs.size()) {
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
            // --- end C22b marked block ---

            // ---- Agent category: run behavior + iteration budget (the old Advanced tab minus
            // the system-prompt editor, which moved to Prompts where it belongs — audit P1) ----
            if (s_setCat == 4) {
                ImGui::Dummy(ImVec2(0, 2 * sc));
                ImGui::SeparatorText("Run behavior");
                ImGui::Checkbox("Plan before building (recommended)", &settings.planMode);
                dimText("The agent first drafts a build plan - the parts, concrete "
                        "dimensions and features - shows it in the conversation, then "
                        "writes the CAD code against it.");
                ImGui::Dummy(ImVec2(0, 4 * sc));
                ImGui::SeparatorText("Iteration budget");
                ImGui::TextUnformatted("Refine-pass cap per request");
                ImGui::SetNextItemWidth(140.0f * sc);
                ImGui::SliderInt("##miters", &settings.maxIters, 0, 64,
                                 settings.maxIters == 0 ? "automatic" : "%d passes");
                ImGui::SameLine();
                dimText("0 = automatic (stops once the render matches)");
            }

            // ---- Team & Sync category (moved out of the Planning page; placeholder until
            //      shared remotes / locks land) ----------------------------------------
            if (s_setCat == 6) {
                ImGui::Dummy(ImVec2(0, 2 * sc));
                ImGui::SeparatorText("Team & Sync");
                dimText("Share a project and sync changes across a team without manual "
                        "merging. Coming soon.");
                ImGui::Dummy(ImVec2(0, 6 * sc));
                ImGui::BeginDisabled(true);
                ImGui::Button("Connect a shared folder\xe2\x80\xa6",
                              ImVec2(200.0f * sc, 30.0f * sc));
                ImGui::SameLine();
                ImGui::Button("Sync now", ImVec2(120.0f * sc, 30.0f * sc));
                ImGui::EndDisabled();
            }

            // ---- Experimental category: research-backed opt-in AI features ----
            if (s_setCat == 5) {
                ImGui::Dummy(ImVec2(0, 2 * sc));
                dimText("Opt-in, default-off features from the model/provider research. All run "
                        "fully local (ITAR-safe) and are additive — off = the standard copilot.");

                // --- Model routing: command model vs. vision/spatial model ---
                ImGui::Dummy(ImVec2(0, 4 * sc));
                ImGui::SeparatorText("Model routing");

                // Command model = the active main provider/model (emits + parses the CAD ops). It's
                // chosen in the AI tab / composer picker; shown here read-only for clarity.
                ImGui::TextUnformatted("Command model (sends & parses CAD ops)");
                helpMarker("The model that drives the agent loop: understands the request, plans, and "
                           "emits/parses the CAD operations. Pick it in the AI tab or the composer's "
                           "model chip. Shown here so you can see both routes side by side.");
                {
                    char cm[600];
                    std::snprintf(cm, sizeof(cm), "   %s  \xc2\xb7  %s",
                                  ui::providerLabel(settings.provider),
                                  settings.model[0] ? settings.model : "(default)");
                    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(ui::palette.accent[0],
                                          ui::palette.accent[1], ui::palette.accent[2], 1));
                    ImGui::TextUnformatted(cm);
                    ImGui::PopStyleColor();
                }

                ImGui::Dummy(ImVec2(0, 6 * sc));
                ImGui::Checkbox("Route vision & spatial reasoning through a separate model",
                                &settings.expVisionReview);
                helpMarker("When on, a vision model judges the rendered viewport during the "
                           "review/fix stages (spatial awareness), while the command model above "
                           "plans and emits ops — the Seek-CAD split. It can be local (ITAR-safe) or "
                           "a cloud vision model (e.g. Qwen-VL). When off, the command model handles "
                           "vision too (if it supports images). Refused under Offline mode if cloud.");
                dimText("Command model emits ops; this vision model sees the screenshot and drives fixes.");
                if (settings.expVisionReview) {
                    ImGui::Dummy(ImVec2(0, 2 * sc));
                    ImGui::TextUnformatted("Vision / spatial model");
                    // Vision-capable presets: cloud (Qwen-VL via OpenRouter/DashScope) + local.
                    std::vector<const char*> vlabels;
                    std::vector<const ui::LocalPreset*> vps;
                    vlabels.push_back("(custom / type below)");
                    vps.push_back(nullptr);
                    for (const auto& p : ui::cloudPresets()) if (p.vision) { vlabels.push_back(p.label.c_str()); vps.push_back(&p); }
                    for (const auto& p : ui::localPresets()) if (p.vision) { vlabels.push_back(p.label.c_str()); vps.push_back(&p); }
                    int vsel = 0;
                    for (int i = 1; i < (int)vps.size(); ++i)
                        if (vps[i] && vps[i]->id == settings.expVisionModel) vsel = i;
                    ImGui::SetNextItemWidth(-1);
                    if (ImGui::Combo("##expvispreset", &vsel, vlabels.data(), (int)vlabels.size()) &&
                        vsel > 0 && vps[vsel]) {
                        const ui::LocalPreset* p = vps[vsel];
                        std::strncpy(settings.expVisionModel, p->id.c_str(), sizeof(settings.expVisionModel) - 1);
                        std::strncpy(settings.expVisionBaseUrl, p->baseUrl.c_str(), sizeof(settings.expVisionBaseUrl) - 1);
                        // Cloud vision preset => prefill the cloud key; local => clear (optional key).
                        bool isCloud = std::strcmp(ui::cloudServiceKey(p->baseUrl), "cloud") != 0 ||
                                       p->baseUrl.rfind("http://localhost", 0) != 0;
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
                    ImGui::TextUnformatted("API key (blank for a local endpoint)");
                    ImGui::SetNextItemWidth(-1);
                    ImGui::InputText("##expviskey", settings.expVisionKey, sizeof(settings.expVisionKey),
                                     ImGuiInputTextFlags_Password);
                    helpMarker("Local: LM Studio / Ollama / vLLM (loopback, no key needed). Cloud: a "
                               "vision model endpoint + its API key (refused under Offline mode).");
                }

                // --- Parametric sequence mode (DeepCAD / CAD-Coder direction) ---
                ImGui::Dummy(ImVec2(0, 8 * sc));
                ImGui::SeparatorText("Parametric sequence mode (experimental)");
                ImGui::Checkbox("Prefer an editable feature sequence over a monolithic solid",
                                &settings.expCadSequenceMode);
                helpMarker("Nudges the modeler to build the part as an ordered sequence of "
                           "individually editable features (sketch → extrude/revolve/loft/sweep → "
                           "fillet/chamfer/hole/pattern) that reconstructs a clean design history, "
                           "rather than one fused solid — inspired by the DeepCAD / CAD-Coder "
                           "command-sequence research. Prompt-level steering today; a dedicated "
                           "sequence-generation model is future work (see docs/EXPERIMENTAL_AI.md).");
                dimText("Keeps generated parts editable as a real feature tree, not a dead solid.");

                ImGui::Dummy(ImVec2(0, 8 * sc));
                dimText(("Saved to " + configPath() + " under \"experimental\". Open-weight "
                         "model presets for your main provider live under AI → Local.")
                            .c_str());
            }
        }
        ImGui::EndChild();
        popGlassChildStyle();
        if (primaryButton("Save", ImVec2(120.0f * sc, 34.0f * sc))) {
            settings.systemPrompt = st.spBuf.data();
            saveSettings(settings);
            st.addLog("[ok] saved settings");
            s_settingsOpen = false;
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel", ImVec2(100.0f * sc, 34.0f * sc))) {
            // Restore the on-open snapshot so live-edited (but unsaved) fields don't linger as
            // a half-applied in-memory state (audit P1) — EXCEPT the immediate-persist
            // preferences (units / theme / glass / recent files), which already saved to
            // config.json when they changed and must keep their live values.
            Units keepUnits = settings.units;
            std::string keepTheme = settings.theme;
            ui::GlassConfig keepGlass = settings.glassCfg;
            std::string keepWs = settings.workspaceDir;   // persisted by its Apply button
            std::vector<std::string> keepRecent = settings.recentFiles;
            settings = s_snapshot;
            settings.units = keepUnits;
            settings.theme = keepTheme;
            settings.glassCfg = keepGlass;
            settings.workspaceDir = keepWs;
            settings.recentFiles = keepRecent;
            std::fill(st.spBuf.begin(), st.spBuf.end(), 0);  // discard prompt edits
            std::strncpy(st.spBuf.data(), settings.systemPrompt.c_str(), st.spBuf.size() - 1);
            s_settingsOpen = false;
        }
        }
        ImGui::End();
    }

    // ---------- About modal ----------
    if (st.showAbout) {
        ImGui::OpenPopup("About ShopCraft");
        st.showAbout = false;
    }
    ImGui::SetNextWindowSize(ImVec2(380.0f * sc, 0), ImGuiCond_Appearing);
    if (ImGui::BeginPopupModal("About ShopCraft", nullptr, ImGuiWindowFlags_NoResize)) {
        const Tex& wm = (ui::activeTheme == ui::Theme::Light && st.wordmarkDark.id)
                            ? st.wordmarkDark : st.wordmark;
        if (wm.id) {
            ImGui::Image((ImTextureID)(intptr_t)wm.id,
                         ImVec2(190.0f * sc, 190.0f * sc * (float)wm.h / wm.w));
        } else {
            sectionHeader(st.fonts.h1, "ShopCraft");
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
