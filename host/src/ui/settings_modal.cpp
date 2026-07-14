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
#include "legacy_chat.h"  // ui::drawAiSettingsSection (glass-grouped AI tab sections)
#include "model_registry.h"  // ui::localPresets (experimental vision-review model presets)
#include "leaderboard_ui.h"  // aicad.leaderboard/1 compact table (Settings > AI)
#include <map>


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

// Shared Settings section chrome: accent title + even vertical rhythm across every tab.
// Prefer this over raw ImGui::SeparatorText so Appearance/General/AI/… share one look.
static void settingsSectionHeader(const char* title, bool first = false) {
    const float sc = ui::uiScale;
    ImGui::Dummy(ImVec2(0, first ? 2.0f * sc : 10.0f * sc));
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(ui::palette.accent[0], ui::palette.accent[1], ui::palette.accent[2], 1.0f));
    ImGui::TextUnformatted(title);
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, 2.0f * sc));
}

// Frosted glass card around a group of related fields. Auto-height; id must be unique per tab.
// Keeps label+widget pairs inside one child so scrolling cannot detach a caption from its field.
static void settingsGlassSectionBegin(const char* id, const char* title) {
    const float sc = ui::uiScale;
    settingsSectionHeader(title);
    ui::pushGlassChildStyle(10.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(14.0f * sc, 10.0f * sc));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(8.0f * sc, 6.0f * sc));
    ImGui::BeginChild(id, ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_Borders);
}

static void settingsGlassSectionEnd() {
    ImGui::EndChild();
    ImGui::PopStyleVar(2);  // ItemSpacing, WindowPadding
    ui::popGlassChildStyle();
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
// ---- Subscription accounts (Settings → AI → Accounts) ----------------------
// Consumes subscription_accounts / subscription_switch / subscription_login /
// subscription_logout. Polls subscription_status for device-code UX fields.
struct SubAccountRow {
    std::string provider;
    std::string accountId;
    std::string label;
    bool active = false;
    bool connected = false;
};
struct SubLoginProgress {
    bool inProgress = false;
    bool browserOpened = false;
    std::string userCode;
    std::string verifyUrl;
    std::string stage;
    std::string lastError;
};
struct SubAccountsUi {
    std::vector<SubAccountRow> rows;
    std::map<std::string, SubLoginProgress> login;  // by provider friendly id
    double lastPoll = -1e9;
    bool pollInFlight = false;
    std::string lastError;
};
static SubAccountsUi s_subAccts;

static const char* kSubProviders[] = {"xai", "claude", "codex", "cursor"};
static const char* kSubProviderLabels[] = {
    "xAI Grok", "Claude", "ChatGPT / Codex", "Cursor"};

static void pollSubscriptionAccounts(AppState& st, bool force) {
    const double now = ImGui::GetTime();
    if (!force && (now - s_subAccts.lastPoll) < 1.5) return;
    if (s_subAccts.pollInFlight) return;
    s_subAccts.lastPoll = now;
    s_subAccts.pollInFlight = true;
    st.rpc.call("subscription_accounts", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_subAccts.pollInFlight = false;
        if (!r.ok || !r.result.is_object()) {
            // RPC missing / worker down — keep last list; surface once.
            if (!r.ok && s_subAccts.lastError.empty())
                s_subAccts.lastError = r.err.empty() ? "subscription_accounts unavailable"
                                                     : r.err;
            return;
        }
        if (r.result.value("ok", true) == false) {
            s_subAccts.lastError = r.result.value("reason", std::string("accounts failed"));
            return;
        }
        s_subAccts.lastError.clear();
        s_subAccts.rows.clear();
        auto arr = r.result.value("accounts", nlohmann::json::array());
        if (!arr.is_array()) return;
        for (const auto& a : arr) {
            if (!a.is_object()) continue;
            SubAccountRow row;
            row.provider  = a.value("provider", std::string());
            row.accountId = a.value("account_id", std::string());
            row.label     = a.value("label", std::string());
            if (row.label.empty()) row.label = a.value("email", std::string());
            if (row.label.empty()) row.label = row.accountId.empty() ? "(account)" : row.accountId;
            row.active    = a.value("active", false);
            row.connected = a.value("connected", true);
            s_subAccts.rows.push_back(std::move(row));
        }
    });
    // Also poll status for live login progress (device code etc.).
    st.rpc.call("subscription_status", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        if (!r.ok || !r.result.is_object()) return;
        auto providers = r.result.value("providers", nlohmann::json::object());
        if (!providers.is_object()) return;
        for (auto it = providers.begin(); it != providers.end(); ++it) {
            if (!it.value().is_object()) continue;
            SubLoginProgress& p = s_subAccts.login[it.key()];
            const auto& x = it.value();
            p.inProgress = x.value("login_in_progress", false);
            p.browserOpened = x.value("browser_opened", false);
            p.userCode = x.value("user_code", std::string());
            p.verifyUrl = x.value("verification_url", std::string());
            if (p.verifyUrl.empty())
                p.verifyUrl = x.value("verification_uri", std::string());
            p.stage = x.value("login_stage", std::string());
            p.lastError = x.value("last_error", std::string());
        }
    });
}

static void drawSubscriptionAccounts(AppState& st, float sc) {
    dimText("Named subscription accounts per provider. Sign in adds another account; "
            "Set active selects which credential the harness uses.");
    pollSubscriptionAccounts(st, false);

    if (!s_subAccts.lastError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextWrapped("%s", s_subAccts.lastError.c_str());
        ImGui::PopStyleColor();
    }

    for (int pi = 0; pi < 4; ++pi) {
        const char* prov = kSubProviders[pi];
        const char* plabel = kSubProviderLabels[pi];
        ImGui::PushID(prov);
        ImGui::Dummy(ImVec2(0, 4.0f * sc));
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(ui::palette.accent[0], ui::palette.accent[1], ui::palette.accent[2], 1.0f));
        ImGui::TextUnformatted(plabel);
        ImGui::PopStyleColor();

        // Rows for this provider.
        bool any = false;
        for (size_t i = 0; i < s_subAccts.rows.size(); ++i) {
            const SubAccountRow& row = s_subAccts.rows[i];
            if (row.provider != prov) continue;
            any = true;
            ImGui::PushID((int)i);
            // Radio for active account.
            bool active = row.active;
            char radioLbl[256];
            std::snprintf(radioLbl, sizeof(radioLbl), "%s%s",
                          row.label.c_str(),
                          row.connected ? "" : " (disconnected)");
            if (ImGui::RadioButton(radioLbl, active)) {
                if (!active) {
                    nlohmann::json p;
                    p["provider"] = prov;
                    p["account_id"] = row.accountId;
                    st.rpc.call("subscription_switch", p, [](const core::RpcResult&) {
                        s_subAccts.lastPoll = -1e9;  // force refresh
                    });
                    // Optimistic local flip.
                    for (auto& r2 : s_subAccts.rows)
                        if (r2.provider == prov) r2.active = false;
                    s_subAccts.rows[i].active = true;
                }
            }
            ImGui::SameLine();
            if (ImGui::SmallButton("Sign out")) {
                nlohmann::json p;
                p["provider"] = prov;
                p["account_id"] = row.accountId;
                st.rpc.call("subscription_logout", p, [](const core::RpcResult&) {
                    s_subAccts.lastPoll = -1e9;
                });
                // Optimistic remove.
                s_subAccts.rows.erase(s_subAccts.rows.begin() + (long)i);
                ImGui::PopID();
                break;
            }
            ImGui::PopID();
        }
        if (!any) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
            ImGui::TextUnformatted("No accounts signed in");
            ImGui::PopStyleColor();
        }

        // Live login progress for this provider (device-code UX).
        SubLoginProgress& prog = s_subAccts.login[prov];
        if (prog.inProgress) {
            if (prog.userCode.empty() && prog.verifyUrl.empty()) {
                ImGui::TextUnformatted("Opening browser\xe2\x80\xa6");
            } else {
                if (prog.browserOpened)
                    ImGui::TextUnformatted("Browser opened \xe2\x80\x94 approve access, then wait\xe2\x80\xa6");
                else
                    ImGui::TextUnformatted("Could not auto-open browser \xe2\x80\x94 open the link below:");
                if (!prog.userCode.empty()) {
                    ImGui::Text("Code: %s", prog.userCode.c_str());
                    ImGui::SameLine();
                    char copyId[64];
                    std::snprintf(copyId, sizeof(copyId), "Copy code##%s", prov);
                    if (ImGui::SmallButton(copyId))
                        ImGui::SetClipboardText(prog.userCode.c_str());
                }
                const std::string fallbackUrl =
                    prog.verifyUrl.empty()
                        ? (std::string(prov) == "xai" ? "https://accounts.x.ai/activate"
                                                      : prog.verifyUrl)
                        : prog.verifyUrl;
                if (!fallbackUrl.empty()) {
                    ImGui::TextWrapped("%s", fallbackUrl.c_str());
                    char openId[64];
                    std::snprintf(openId, sizeof(openId), "Open in browser##%s", prov);
                    if (ImGui::SmallButton(openId)) {
#if defined(_WIN32)
                        std::system(("start \"\" \"" + fallbackUrl + "\"").c_str());
#elif defined(__APPLE__)
                        std::system(("open '" + fallbackUrl + "'").c_str());
#else
                        std::system(("xdg-open '" + fallbackUrl + "' >/dev/null 2>&1 &").c_str());
#endif
                    }
                }
            }
            pollSubscriptionAccounts(st, true);
        } else {
            char signId[64];
            std::snprintf(signId, sizeof(signId), "Sign in##%s", prov);
            if (ImGui::SmallButton(signId)) {
                prog.inProgress = true;
                prog.lastError.clear();
                prog.userCode.clear();
                prog.verifyUrl.clear();
                prog.browserOpened = false;
                prog.stage = "start";
                nlohmann::json p;
                p["provider"] = prov;
                // xAI is device-code only; others use browser OAuth.
                if (std::string(prov) == "xai" || std::string(prov) == "codex")
                    p["device"] = true;
                st.rpc.call("subscription_login", p, [prov](const core::RpcResult& r) {
                    SubLoginProgress& pr = s_subAccts.login[prov];
                    if (!r.ok) {
                        pr.inProgress = false;
                        pr.lastError = r.err.empty() ? "login failed" : r.err;
                    } else if (r.result.is_object() && r.result.value("ok", true) == false) {
                        pr.inProgress = false;
                        auto reason = r.result.value("reason", std::string());
                        auto hint = r.result.value("hint", std::string());
                        pr.lastError = reason.empty() ? "login failed" : reason;
                        if (!hint.empty()) pr.lastError += " — " + hint;
                    }
                    s_subAccts.lastPoll = -1e9;
                });
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Add another %s subscription account (browser OAuth).", plabel);
        }
        if (!prog.lastError.empty() && !prog.inProgress) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
            ImGui::TextWrapped("%s", prog.lastError.c_str());
            ImGui::PopStyleColor();
        }
        ImGui::PopID();
    }
}

// ---- Run benchmark (Settings → AI → Model Leaderboard) ---------------------
struct BenchUi {
    int source = 1;   // 0=current copilot, 1=xAI grok-4.5, 2=local
    int mode = 0;     // 0=diag, 1=smoke
    bool running = false;
    bool pollInFlight = false;
    double lastPoll = -1e9;
    std::string stage = "idle";
    std::string lastError;
    std::string lastResultPath;
    bool wasRunning = false;
};
static BenchUi s_bench;

static std::string benchTierForSource(const Settings& s, int source) {
    if (source == 1) return "xai:grok-4-5";
    if (source == 2) return "local-24k";
    // Current copilot model → gym tier string.
    const int p = s.provider;
    const char* model = s.model[0] ? s.model : "";
    if (p == ui::PROV_XAI) {
        std::string m = model[0] ? model : "grok-4-5";
        // Gym tiers use hyphens (grok-4-5); UI may show grok-4.5.
        for (char& c : m) if (c == '.') c = '-';
        return std::string("xai:") + m;
    }
    if (p == ui::PROV_LOCAL || p == ui::PROV_LLAMACPP) return "local-24k";
    if (p == ui::PROV_CLOUDCOMPAT && model[0]) {
        // OpenRouter path when base looks like openrouter.
        if (std::strstr(s.baseUrl, "openrouter") || std::strstr(model, "/"))
            return std::string("openrouter:") + model;
    }
    if (p == ui::PROV_ANTHROPIC && model[0])
        return std::string("openrouter:anthropic/") + model;
    if (p == ui::PROV_OPENAI && model[0])
        return std::string("openrouter:openai/") + model;
    if (p == ui::PROV_GEMINI && model[0])
        return std::string("openrouter:google/") + model;
    return "local-24k";
}

static bool benchSourceIsPaid(const Settings& s, int source) {
    if (source == 1) return true;   // xAI grok
    if (source == 2) return false;  // local
    // Current copilot: paid if cloud.
    return ui::providerIsCloud(s.provider);
}

static void pollBenchStatus(AppState& st, bool force, ui::leaderboard::Board* lbRefresh) {
    const double now = ImGui::GetTime();
    if (!force && (now - s_bench.lastPoll) < 1.0) return;
    if (s_bench.pollInFlight) return;
    s_bench.lastPoll = now;
    s_bench.pollInFlight = true;
    st.rpc.call("bench_status", nlohmann::json::object(),
                [lbRefresh](const core::RpcResult& r) {
        s_bench.pollInFlight = false;
        if (!r.ok || !r.result.is_object()) return;
        const bool was = s_bench.running;
        s_bench.running = r.result.value("running", false);
        s_bench.stage = r.result.value("stage", std::string("idle"));
        s_bench.lastError = r.result.value("last_error", std::string());
        if (s_bench.lastError == "null") s_bench.lastError.clear();
        // nlohmann may return empty for null; also accept null type.
        if (r.result.contains("last_error") && r.result["last_error"].is_null())
            s_bench.lastError.clear();
        s_bench.lastResultPath = r.result.value("last_result_path", std::string());
        if (r.result.contains("last_result_path") && r.result["last_result_path"].is_null())
            s_bench.lastResultPath.clear();
        // Transition running → done: auto-refresh leaderboard table.
        if (was && !s_bench.running && lbRefresh) {
            *lbRefresh = ui::leaderboard::load();
        }
        s_bench.wasRunning = s_bench.running;
    });
}

static void drawBenchRunner(AppState& st, float sc, ui::leaderboard::Board& lb,
                            double& lbLast) {
    ImGui::Dummy(ImVec2(0, 6.0f * sc));
    ImGui::Separator();
    ImGui::Dummy(ImVec2(0, 4.0f * sc));
    ImGui::TextUnformatted("Run benchmark");
    dimText("Spawns the gym CLI detached on the worker. On finish rebuilds "
            "benchmarks/leaderboard/leaderboard.json and refreshes the table.");

    static const char* kSources[] = {
        "Current copilot model",
        "xAI grok-4.5",
        "Local models (local-24k)",
    };
    static const char* kModes[] = {
        "Quick diag",
        "C1 smoke",
    };
    ImGui::SetNextItemWidth(220.0f * sc);
    ImGui::Combo("##bench_src", &s_bench.source, kSources, 3);
    ImGui::SameLine();
    ImGui::SetNextItemWidth(140.0f * sc);
    ImGui::Combo("##bench_mode", &s_bench.mode, kModes, 2);

    if (benchSourceIsPaid(st.settings, s_bench.source)) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextWrapped("Uses your API credits / subscription for the selected model.");
        ImGui::PopStyleColor();
    }

    const bool canRun = !s_bench.running && st.workerStarted;
    if (!canRun) ImGui::BeginDisabled();
    if (ImGui::Button("Run benchmark", ImVec2(160.0f * sc, 0))) {
        const std::string tier = benchTierForSource(st.settings, s_bench.source);
        const char* mode = (s_bench.mode == 0) ? "diag" : "smoke";
        nlohmann::json p;
        p["mode"] = mode;
        p["tiers"] = nlohmann::json::array({tier});
        s_bench.running = true;
        s_bench.stage = "starting";
        s_bench.lastError.clear();
        s_bench.lastPoll = -1e9;
        st.rpc.call("bench_run", p, [](const core::RpcResult& r) {
            if (!r.ok) {
                s_bench.running = false;
                s_bench.stage = "error";
                s_bench.lastError = r.err.empty() ? "bench_run failed" : r.err;
            } else if (r.result.is_object() && r.result.value("ok", true) == false) {
                s_bench.running = false;
                s_bench.stage = "error";
                s_bench.lastError = r.result.value("reason", std::string("bench_run failed"));
            }
            s_bench.lastPoll = -1e9;
        });
    }
    if (!canRun) ImGui::EndDisabled();
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)) {
        if (s_bench.running)
            ImGui::SetTooltip("A benchmark is already running.");
        else if (!st.workerStarted)
            ImGui::SetTooltip("Worker not ready yet.");
        else
            ImGui::SetTooltip("Start a detached gym CLI run for the selected model.");
    }

    // Status line + spinner while running / after finish.
    pollBenchStatus(st, s_bench.running, &lb);
    if (s_bench.running) {
        // Lightweight spinner via rotating dots (no GL dependency).
        static const char* kSpin[] = {".  ", ".. ", "...", " ..", "  .", "   "};
        int frame = (int)(ImGui::GetTime() * 6.0) % 6;
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.accent));
        ImGui::Text("Running %s %s", s_bench.stage.c_str(), kSpin[frame]);
        ImGui::PopStyleColor();
        // Force table reload timestamp so the next idle poll reloads after done.
        lbLast = -1e9;
    } else if (s_bench.stage == "done") {
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusOk));
        ImGui::TextUnformatted("Done \xe2\x80\x94 leaderboard refreshed");
        ImGui::PopStyleColor();
    } else if (s_bench.stage == "error" && !s_bench.lastError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
        ImGui::TextWrapped("%s", s_bench.lastError.c_str());
        ImGui::PopStyleColor();
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
    { "Accounts",                 "AI",           2 },
    { "Subscription accounts",    "AI",           2 },
    { "Sign in",                  "AI",           2 },
    { "Model leaderboard",        "AI",           2 },
    { "Run benchmark",            "AI",           2 },
    { "Benchmark",                "AI",           2 },
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
    const float winW = std::min(760.0f * sc, maxW);
    const float winH = std::min(760.0f * sc, maxH);
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
        pushGlassChildStyle(10.0f);
        ImGui::BeginChild("##settabsarea",
                          ImVec2(0, std::max(120.0f * sc,
                                             ImGui::GetContentRegionAvail().y - footerH)));
        if (ImGui::BeginTabBar("##settabs")) {
            // ---- Appearance tab (FIRST): theme + colored liquid glass (v2.1) ----
            // The canonical theme control lives here (moved from General; the View-menu item
            // stays a synced shortcut). The glass knobs edit Settings::glassCfg per group and
            // live-apply via ui::setGlassConfig + persist via saveSettings on every change —
            // so the persisted "appearance.glass" block is no longer a dead round-trip.
            if (ImGui::BeginTabItem("Appearance", nullptr,
                                    (jumpToTab == 0) ? ImGuiTabItemFlags_SetSelected
                                                     : ImGuiTabItemFlags_None)) {
                settingsSectionHeader("Theme", /*first=*/true);
                bool isLight = (ui::activeTheme == ui::Theme::Light);
                if (ImGui::Checkbox("Light theme (SolidWorks-style)", &isLight))
                    applyThemeNow(st, isLight);
                dimText("Switches the whole UI between the dark and light palettes. "
                        "Saved immediately.");

                settingsSectionHeader("Liquid Glass");
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
                settingsSectionHeader("Preview");
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
                ImGui::EndTabItem();
            }

            // ---- General tab: Document (units) + System (engine readout, profiler) ----
            if (ImGui::BeginTabItem("General", nullptr,
                                    (jumpToTab == 1) ? ImGuiTabItemFlags_SetSelected
                                                     : ImGuiTabItemFlags_None)) {
                settingsSectionHeader("Document", /*first=*/true);
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
                settingsSectionHeader("Workspace");
                ImGui::TextUnformatted("Workspace folder");
                // Truthful default label: defaultWorkspaceDir() applies the alias rule
                // (existing Documents/AI-CAD wins, else Documents/Billet).
                static const std::string s_wsDefault = defaultWorkspaceDir();
                static const std::string s_wsHelp =
                    "The File Explorer's root folder and the default save location. "
                    "Leave empty to use " + s_wsDefault + ". Applied and saved with the "
                    "Apply button.";
                static const std::string s_wsHint = "(default: " + s_wsDefault + ")";
                helpMarker(s_wsHelp.c_str());
                ImGui::SetNextItemWidth(-110.0f * sc);
                ImGui::InputTextWithHint("##wsdir", s_wsHint.c_str(),
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
                settingsSectionHeader("System");
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
                ImGui::EndTabItem();
            }
            if (ImGui::BeginTabItem("AI", nullptr,
                                    (jumpToTab == 2) ? ImGuiTabItemFlags_SetSelected
                                                     : ImGuiTabItemFlags_None)) {
                dimText(("Used by Design, Edit, and Help. Stored in " + configPath() + ".")
                            .c_str());

                // Glass-grouped sections — each child owns its label+widget pairs so a scroll
                // boundary can never orphan a caption (the unlabeled "0" max-tokens bug).
                settingsGlassSectionBegin("##ai_provider", "Provider & Model");
                ui::drawAiSettingsSection(st, busy, ui::AiSettingsSection::ProviderModel);
                settingsGlassSectionEnd();

                settingsGlassSectionBegin("##ai_accounts", "Accounts");
                drawSubscriptionAccounts(st, sc);
                settingsGlassSectionEnd();

                settingsGlassSectionBegin("##ai_generation", "Generation");
                ui::drawAiSettingsSection(st, busy, ui::AiSettingsSection::Generation);
                settingsGlassSectionEnd();

                settingsGlassSectionBegin("##ai_privacy", "Privacy");
                ui::drawAiSettingsSection(st, busy, ui::AiSettingsSection::Privacy);
                settingsGlassSectionEnd();

                settingsGlassSectionBegin("##ai_leaderboard", "Model Leaderboard");
                dimText("Deterministic gym pass rates from benchmarks/leaderboard/leaderboard.json. "
                        "MOCK badge when task_suite ends in -MOCK.");
                {
                    static ui::leaderboard::Board s_lb;
                    static bool s_lbLoaded = false;
                    static double s_lbLast = -1e9;
                    const double now = ImGui::GetTime();
                    if (!s_lbLoaded || (now - s_lbLast) > 5.0) {
                        s_lb = ui::leaderboard::load();
                        s_lbLoaded = true;
                        s_lbLast = now;
                    }
                    if (ImGui::SmallButton("Refresh##settings_lb")) {
                        s_lb = ui::leaderboard::load();
                        s_lbLast = ImGui::GetTime();
                    }
                    ImGui::Dummy(ImVec2(0, 2 * sc));
                    ui::leaderboard::drawCompactTable(s_lb, sc);
                    drawBenchRunner(st, sc, s_lb, s_lbLast);
                }
                settingsGlassSectionEnd();

                settingsGlassSectionBegin("##ai_maintenance", "Maintenance");
                dimText("Repair the local CAD engine runtime, or trim which models appear in the "
                        "composer's model picker.");
                {
                    const bool canSetup = !st.setupRunning.load() && !st.job.running.load();
                    if (!canSetup) ImGui::BeginDisabled();
                    if (ImGui::Button("Repair / install runtime", ImVec2(220.0f * sc, 0)))
                        st.startSetup(true);
                    if (!canSetup) ImGui::EndDisabled();
                    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                        ImGui::SetTooltip("Rebuild the local Python/CAD runtime (same as "
                                          "Billet \xe2\x86\x92 Repair / install runtime).");
                    if (st.setupRunning.load()) {
                        ImGui::SameLine();
                        dimText("Running\xe2\x80\xa6");
                    }
                }
                ImGui::Dummy(ImVec2(0, 4 * sc));
                if (ImGui::CollapsingHeader("Manage models (chat picker)"))
                    ui::drawAiSettingsSection(st, busy, ui::AiSettingsSection::ManageModels);
                settingsGlassSectionEnd();

                ImGui::EndTabItem();
            }
            // --- C22b: Prompts tab (role prompt editor + system-prompt override) ---
            if (ImGui::BeginTabItem("Prompts", nullptr,
                                    (jumpToPrompts || jumpToTab == 3) ? ImGuiTabItemFlags_SetSelected
                                                                      : ImGuiTabItemFlags_None)) {
                settingsSectionHeader("Role prompts", /*first=*/true);
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
                ImGui::EndTabItem();
            }
            // --- end C22b marked block ---

            // ---- Agent tab: run behavior + iteration budget (the old Advanced tab minus the
            // system-prompt editor, which moved to Prompts where it belongs — audit P1) ----
            // AI_CAD_SHOT=settings selects this tab ONE-SHOT: consuming st.wantSettings directly
            // every frame kept the other tabs unclickable for the whole session (audit P2).
            static bool s_wantSettingsTabDone = false;
            bool wantSettingsSel = st.wantSettings && !s_wantSettingsTabDone;
            if (wantSettingsSel) s_wantSettingsTabDone = true;
            if (ImGui::BeginTabItem("Agent", nullptr,
                                    (wantSettingsSel || jumpToTab == 4) ? ImGuiTabItemFlags_SetSelected
                                                                        : ImGuiTabItemFlags_None)) {
                settingsSectionHeader("Run behavior", /*first=*/true);
                ImGui::Checkbox("Plan before building (recommended)", &settings.planMode);
                dimText("The agent first drafts a build plan - the parts, concrete "
                        "dimensions and features - shows it in the conversation, then "
                        "writes the CAD code against it.");
                settingsSectionHeader("Iteration budget");
                {
                    const float labelW = 200.0f * sc;
                    ImGui::AlignTextToFramePadding();
                    ImGui::TextUnformatted("Refine-pass cap per request");
                    ImGui::SameLine(labelW);
                    ImGui::SetNextItemWidth(160.0f * sc);
                    ImGui::SliderInt("##miters", &settings.maxIters, 0, 64,
                                     settings.maxIters == 0 ? "automatic" : "%d passes");
                }
                dimText("0 = automatic (stops once the render matches)");
                ImGui::EndTabItem();
            }

            // ---- Experimental tab: research-backed opt-in AI features ----
            if (ImGui::BeginTabItem("Experimental", nullptr,
                                    (jumpToTab == 5) ? ImGuiTabItemFlags_SetSelected
                                                     : ImGuiTabItemFlags_None)) {
                dimText("Opt-in, default-off features from the model/provider research. All run "
                        "fully local (ITAR-safe) and are additive — off = the standard copilot.");

                // --- Model routing: command model vs. vision/spatial model ---
                settingsSectionHeader("Model routing", /*first=*/true);

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
                settingsSectionHeader("Parametric sequence mode (experimental)");
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
                ImGui::EndTabItem();
            }
            ImGui::EndTabBar();
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
        ImGui::OpenPopup("About Billet");
        st.showAbout = false;
    }
    ImGui::SetNextWindowSize(ImVec2(380.0f * sc, 0), ImGuiCond_Appearing);
    if (ImGui::BeginPopupModal("About Billet", nullptr, ImGuiWindowFlags_NoResize)) {
        const Tex& wm = (ui::activeTheme == ui::Theme::Light && st.wordmarkDark.id)
                            ? st.wordmarkDark : st.wordmark;
        if (wm.id) {
            ImGui::Image((ImTextureID)(intptr_t)wm.id,
                         ImVec2(190.0f * sc, 190.0f * sc * (float)wm.h / wm.w));
        } else {
            sectionHeader(st.fonts.h1, "Billet");
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
