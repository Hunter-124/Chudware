// Chudware Settings modal (Appearance / General / AI / Providers / Benchmarks /
// Prompts / Agent / Experimental tabs) and the About modal.
#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <fstream>
#include <map>
#include <set>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "imgui.h"

#include "settings_modal.h"
#include "config_panel.h"
#include "layout.h"  // drawConfigurationsBody fallback

#include "../core/app_state.h"
#include "file_dialog.h"
#include "../core/fs_backend.h"
#include "../core/cpp_core.h"
#include "../renderer.h"
#include "copilot/copilot.h"  // ui::drawAiSettingsSection (glass-grouped AI tab sections)
#include "ai/ai_prefs.h"  // ui::aiUiVerbosity/aiFollowActiveSession/aiActiveWorkPreviews
#include "model_registry.h"  // ui::localPresets (experimental vision-review model presets)
#include "leaderboard_ui.h"  // aicad.leaderboard/1 compact table (Settings > Benchmarks)
#include "oauth_capture.h"  // in-app xAI multi-account CEF OAuth (Settings → Accounts)
#include "web_capabilities.h"  // ui::webGapReason (T-RF12: sign-in is desktop-only on web)
#include "../core/async_rpc.h"  // core::RpcResult for skills/triggers RPCs
#include "widgets/interwebz.h"  // Skills/Context two-column editors


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

static std::string s_promptsError;
static void loadPrompts(AppState& st) {
    // Prefer list_editable_prompts (YAML mission roles + core .md, with overrides).
    // Fall back to get_prompts_v2 (C++ core kRoles catalog) when the worker is old.
    nlohmann::json result;
    std::string err;
    s_promptsError.clear();
    bool ok = st.worker.call("list_editable_prompts", nlohmann::json::object(), result, err, 20000);
    if (!ok) {
        err.clear();
        ok = st.worker.call("get_prompts_v2", nlohmann::json::object(), result, err, 20000);
        if (!ok) {
            s_promptsError = err.empty() ? "prompt catalog unavailable" : err;
            st.addLog("[err] prompts catalog: " + s_promptsError);
            s_promptsLoaded = false;
            return;
        }
    }
    // Bare array or {"prompts":[...]} or error object
    auto str = [](const nlohmann::json& j, const char* k) -> std::string {
        auto it = j.find(k);
        return (it != j.end() && it->is_string()) ? it->get<std::string>() : std::string();
    };
    if (result.is_object() && result.value("ok", true) == false) {
        s_promptsError = str(result, "error");
        if (s_promptsError.empty()) s_promptsError = "prompt catalog error";
        s_promptsLoaded = false;
        return;
    }
    const nlohmann::json* recs = &result;
    if (result.is_object() && result.contains("prompts") && result["prompts"].is_array())
        recs = &result["prompts"];
    s_promptRecs.clear();
    if (!recs->is_array()) { s_promptsLoaded = true; return; }
    for (auto& r : *recs) {
        if (!r.is_object()) continue;
        PromptRecord pr;
        pr.role         = str(r, "role");
        if (pr.role.empty()) continue;
        pr.builtinPath  = str(r, "path");
        pr.overridePath = str(r, "override_path");
        pr.overridden   = r.value("overridden", false);
        std::string text = str(r, "text");
        pr.builtinLen = (int)text.size();
        std::fill(pr.buf.begin(), pr.buf.end(), 0);
        std::strncpy(pr.buf.data(), text.c_str(), pr.buf.size() - 1);
        s_promptRecs.push_back(std::move(pr));
    }
    // Stable alpha order for power-user scanning
    std::sort(s_promptRecs.begin(), s_promptRecs.end(),
              [](const PromptRecord& a, const PromptRecord& b) { return a.role < b.role; });
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

// Sticky Settings tabs: tab bar stays put; only the body child scrolls.
static void settingsTabBodyBegin(const char* id, bool scroll = true) {
    const float sc = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10.0f * sc, 8.0f * sc));
    ImGuiWindowFlags flags = scroll ? ImGuiWindowFlags_None
                                    : (ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    ImGui::BeginChild(id, ImVec2(0, 0), ImGuiChildFlags_None, flags);
}
static void settingsTabBodyEnd() {
    ImGui::EndChild();
    ImGui::PopStyleVar(1);  // WindowPadding
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
// ---- Subscription accounts (Settings → Providers → Accounts) ----------------
// Consumes subscription_accounts / subscription_switch / subscription_login /
// subscription_logout / subscription_usage. Polls subscription_status for
// device-code UX fields. Usage is optional per contracts/providers.md §4.5 —
// absent fields degrade to a dim "usage unavailable" row.
struct SubUsageWindow {
    std::string window;       // e.g. "5h", "7d"
    std::string label;        // display label
    float usedFraction = 0.f; // 0..1
    bool hasFraction = false; // null means status-only; never render it as 0%
    std::string resetsAt;     // iso8601
    std::string status;       // ok|warn|blocked|unknown
};
struct SubAccountRow {
    std::string provider;
    std::string accountId;
    std::string label;       // email / label fallback
    std::string displayName; // optional §4.5
    std::string plan;        // optional §4.5 plan chip
    bool active = false;
    bool connected = false;
    bool hasUsage = false;   // true when usage array present (may be empty)
    std::vector<SubUsageWindow> usage;
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
    double lastAccountsPoll = -1e9;
    double lastUsagePoll = -1e9;
    bool accountsPollInFlight = false;
    bool usagePollInFlight = false;
    bool usageRpcMissing = false;  // unknown_method → stop polling, dim degrade
    int  lastDrawnFrame = -100;    // edge-detect section open for forced usage poll
    std::string lastError;
};
static SubAccountsUi s_subAccts;
// CEF add-account disabled — device/browser Python flow (xai_sub_provider)
// handles multi-account storage+rotation. Leave oauth_capture compiled but dormant.
static constexpr bool kXaiUseCef = false;

static const char* kSubProviders[] = {"xai", "claude", "codex", "cursor", "kimi"};
static const char* kSubProviderLabels[] = {
    "xAI Grok", "Claude", "ChatGPT / Codex", "Cursor", "Kimi Code"};
// 5-minute usage poll while the Accounts section is visible (§4.5).
static constexpr double kUsagePollIntervalSec = 5.0 * 60.0;
static constexpr double kAccountsPollIntervalSec = 1.5;

static std::string optionalJsonString(const nlohmann::json& obj, const char* key) {
    auto it = obj.find(key);
    return it != obj.end() && it->is_string() ? it->get<std::string>() : std::string();
}
// Present-but-null / wrong-type fields must not throw under WASM (exception catching
// disabled → page abort). Default when key missing, null, or not the requested type.
static bool optionalJsonBool(const nlohmann::json& obj, const char* key, bool dflt = false) {
    auto it = obj.find(key);
    if (it == obj.end() || it->is_null()) return dflt;
    if (it->is_boolean()) return it->get<bool>();
    return dflt;
}
static int optionalJsonInt(const nlohmann::json& obj, const char* key, int dflt = 0) {
    auto it = obj.find(key);
    if (it == obj.end() || it->is_null()) return dflt;
    if (it->is_number_integer()) return it->get<int>();
    if (it->is_number()) return (int)it->get<double>();
    return dflt;
}
static nlohmann::json optionalJsonObj(const nlohmann::json& obj, const char* key) {
    auto it = obj.find(key);
    if (it == obj.end() || !it->is_object()) return nlohmann::json::object();
    return *it;
}
static const nlohmann::json* optionalJsonArr(const nlohmann::json& obj, const char* key) {
    auto it = obj.find(key);
    if (it == obj.end() || !it->is_array()) return nullptr;
    return &(*it);
}


// Parse optional usage array from an account / usage-row object.
static void parseUsageWindows(const nlohmann::json& src, SubAccountRow& row) {
    row.usage.clear();
    row.hasUsage = false;
    auto it = src.find("usage");
    if (it == src.end() || !it->is_array()) return;
    row.hasUsage = true;
    for (const auto& u : *it) {
        if (!u.is_object()) continue;
        SubUsageWindow w;
        w.window = optionalJsonString(u, "window");
        w.label  = optionalJsonString(u, "label");
        if (w.label.empty()) w.label = w.window.empty() ? "Usage" : w.window;
        if (u.contains("used_fraction") && u["used_fraction"].is_number()) {
            w.usedFraction = (float)std::clamp(u["used_fraction"].get<double>(), 0.0, 1.0);
            w.hasFraction = true;
        }
        w.resetsAt = optionalJsonString(u, "resets_at");
        w.status   = optionalJsonString(u, "status");
        if (w.status.empty()) w.status = "unknown";
        row.usage.push_back(std::move(w));
    }
}

// Fill optional identity fields from an accounts/status row (absent = unknown).
// label holds email or a human label only — never the raw account_id (display shortens that).
static void parseAccountIdentity(const nlohmann::json& a, SubAccountRow& row) {
    row.displayName = optionalJsonString(a, "display_name");
    row.plan        = optionalJsonString(a, "plan");
    // Prefer explicit email, then label. Account id is a last-resort display path only.
    row.label = optionalJsonString(a, "email");
    if (row.label.empty()) row.label = optionalJsonString(a, "label");
    // If backend stuffed the raw account_id into label/display_name, clear so the
    // display path can short-id it instead of showing a full UUID as the title.
    if (!row.label.empty() && row.label == row.accountId) row.label.clear();
    if (!row.displayName.empty() && row.displayName == row.accountId) row.displayName.clear();
    parseUsageWindows(a, row);
}

// Shorten a long account id / UUID for the primary label (never a full 36-char UUID).
static std::string shortAccountId(const std::string& id) {
    if (id.empty()) return "(account)";
    if (id.size() <= 10) return id;
    return id.substr(0, 8) + "\xe2\x80\xa6";
}

// Primary row label: display_name → email/label → shortened account id.
static std::string accountPrimaryLabel(const SubAccountRow& row) {
    if (!row.displayName.empty()) return row.displayName;
    if (!row.label.empty()) return row.label;
    return shortAccountId(row.accountId);
}

// Merge usage rows from subscription_usage into existing account rows by
// (provider, account_id). Accounts poll is the source of truth for membership.
static void mergeUsageRows(const nlohmann::json& rows) {
    if (!rows.is_array()) return;
    for (const auto& r : rows) {
        if (!r.is_object()) continue;
        const std::string prov = optionalJsonString(r, "provider");
        const std::string aid  = optionalJsonString(r, "account_id");
        for (auto& row : s_subAccts.rows) {
            if (row.provider != prov) continue;
            if (!aid.empty() && row.accountId != aid) continue;
            if (r.contains("display_name") && r["display_name"].is_string()) {
                const std::string dn = r["display_name"].get<std::string>();
                if (!dn.empty() && dn != row.accountId) row.displayName = dn;
            }
            if (r.contains("email") && r["email"].is_string()) {
                const std::string em = r["email"].get<std::string>();
                if (!em.empty() && em != row.accountId) row.label = em;
            } else if (r.contains("label") && r["label"].is_string()) {
                const std::string lb = r["label"].get<std::string>();
                if (!lb.empty() && lb != row.accountId && row.label.empty())
                    row.label = lb;
            }
            if (r.contains("plan") && r["plan"].is_string()) {
                const std::string pl = r["plan"].get<std::string>();
                if (!pl.empty()) row.plan = pl;
            }
            parseUsageWindows(r, row);
            if (aid.empty()) break;  // no account_id: first matching provider only
        }
    }
}

// Human countdown from an ISO-8601 timestamp ("resets in 2h 14m"). Empty on parse fail.
static std::string formatResetCountdown(const std::string& iso) {
    if (iso.empty()) return {};
    int y = 0, mo = 0, d = 0, h = 0, mi = 0, s = 0;
    if (std::sscanf(iso.c_str(), "%d-%d-%dT%d:%d:%d", &y, &mo, &d, &h, &mi, &s) < 6)
        return {};
    std::tm tm{};
    tm.tm_year = y - 1900;
    tm.tm_mon  = mo - 1;
    tm.tm_mday = d;
    tm.tm_hour = h;
    tm.tm_min  = mi;
    tm.tm_sec  = s;
#if defined(_WIN32)
    const time_t target = _mkgmtime(&tm);
#else
    const time_t target = timegm(&tm);
#endif
    if (target == (time_t)-1) return {};
    const time_t now = std::time(nullptr);
    long long delta = (long long)(target - now);
    if (delta <= 0) return "resets soon";
    const long long days = delta / 86400;
    delta %= 86400;
    const long long hours = delta / 3600;
    delta %= 3600;
    const long long mins = delta / 60;
    char buf[64];
    if (days > 0)
        std::snprintf(buf, sizeof(buf), "resets in %lldd %lldh", days, hours);
    else if (hours > 0)
        std::snprintf(buf, sizeof(buf), "resets in %lldh %lldm", hours, mins);
    else
        std::snprintf(buf, sizeof(buf), "resets in %lldm", mins);
    return buf;
}

static const float* usageStatusColor(const std::string& status, float frac) {
    if (status == "blocked") return ui::palette.statusErr;
    if (status == "warn")    return ui::palette.statusWarn;
    if (status == "ok")      return ui::palette.statusOk;
    // unknown / empty: derive from fraction thresholds (matches context meter).
    if (frac >= 0.95f) return ui::palette.statusErr;
    if (frac >= 0.75f) return ui::palette.statusWarn;
    return ui::palette.statusOk;
}

// Small plan pill (chipBg fill + accent border/text). No-op when plan empty.
static void drawPlanChip(const std::string& plan, float sc) {
    if (plan.empty()) return;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 ts = ImGui::CalcTextSize(plan.c_str());
    const ImVec2 p  = ImGui::GetCursorScreenPos();
    const float  h  = ts.y + 4.0f * sc;
    const float  wd = ts.x + 12.0f * sc;
    const auto& pal = ui::palette;
    dl->AddRectFilled(p, ImVec2(p.x + wd, p.y + h),
                      ui::Palette::toU32(pal.chipBg), h * 0.5f);
    dl->AddRect(p, ImVec2(p.x + wd, p.y + h),
                ImGui::ColorConvertFloat4ToU32(
                    ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.55f)),
                h * 0.5f, 0, 1.0f * sc);
    dl->AddText(ImVec2(p.x + 6.0f * sc, p.y + 2.0f * sc),
                ui::Palette::toU32(pal.accent), plan.c_str());
    ImGui::Dummy(ImVec2(wd, h));
}

// One usage window: label + fraction bar + reset countdown, colored by status.
static void drawUsageWindow(const SubUsageWindow& w, float sc) {
    if (!w.hasFraction) {
        const char* detail = "usage unavailable";
        if (w.status == "needs_login") detail = "sign in required";
        else if (w.status == "blocked") detail = "browser verification required";
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
        ImGui::Text("%s \xe2\x80\x94 %s", w.label.c_str(), detail);
        ImGui::PopStyleColor();
        return;
    }
    const auto& pal = ui::palette;
    const float* role = usageStatusColor(w.status, w.usedFraction);
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(role[0], role[1], role[2], 1.0f));
    ImGui::TextUnformatted(w.label.c_str());
    ImGui::PopStyleColor();

    const std::string countdown = formatResetCountdown(w.resetsAt);
    char pctBuf[32];
    std::snprintf(pctBuf, sizeof(pctBuf), "%.0f%%", w.usedFraction * 100.0f);

    // Bar + percent + countdown on one row.
    const float barH = 6.0f * sc;
    const float barW = ImGui::GetContentRegionAvail().x;
    char right[96];
    if (!countdown.empty())
        std::snprintf(right, sizeof(right), "%s  %s", pctBuf, countdown.c_str());
    else
        std::snprintf(right, sizeof(right), "%s", pctBuf);
    const float rightW = ImGui::CalcTextSize(right).x + 8.0f * sc;
    const float trackW = std::max(40.0f * sc, barW - rightW);

    ImVec2 p = ImGui::GetCursorScreenPos();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float by0 = p.y + (ImGui::GetTextLineHeight() - barH) * 0.5f;
    const float by1 = by0 + barH;
    dl->AddRectFilled(ImVec2(p.x, by0), ImVec2(p.x + trackW, by1),
                      ui::Palette::toU32(pal.accentSoft), 2.0f * sc);
    if (w.usedFraction > 0.f) {
        dl->AddRectFilled(ImVec2(p.x, by0),
                          ImVec2(p.x + trackW * w.usedFraction, by1),
                          ImGui::ColorConvertFloat4ToU32(
                              ImVec4(role[0], role[1], role[2], 0.95f)),
                          2.0f * sc);
    }
    ImGui::Dummy(ImVec2(trackW, ImGui::GetTextLineHeight()));
    ImGui::SameLine(0, 6.0f * sc);
    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
    ImGui::TextUnformatted(right);
    ImGui::PopStyleColor();
}

static void pollSubscriptionUsage(AppState& st, bool force) {
    if (s_subAccts.usageRpcMissing) return;
    const double now = ImGui::GetTime();
    if (!force && (now - s_subAccts.lastUsagePoll) < kUsagePollIntervalSec) return;
    if (s_subAccts.usagePollInFlight) return;
    s_subAccts.lastUsagePoll = now;
    s_subAccts.usagePollInFlight = true;
    st.rpc.call("subscription_usage", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_subAccts.usagePollInFlight = false;
        if (!r.ok) {
            // unknown_method / worker down — keep last usage; mark missing once so we
            // stop hammering and the UI stays on the dim "usage unavailable" path.
            const std::string& err = r.err;
            if (err.find("unknown_method") != std::string::npos ||
                err.find("unknown method") != std::string::npos ||
                err.find("not found") != std::string::npos) {
                s_subAccts.usageRpcMissing = true;
            }
            return;
        }
        if (!r.result.is_object()) return;
        if (optionalJsonBool(r.result, "ok", true) == false) {
            // Soft failure (e.g. 429 / provider outage) — leave prior usage alone.
            return;
        }
        if (const auto* rows = optionalJsonArr(r.result, "rows")) mergeUsageRows(*rows);
    });
}

static void pollSubscriptionAccounts(AppState& st, bool force) {
    const double now = ImGui::GetTime();
    if (!force && (now - s_subAccts.lastAccountsPoll) < kAccountsPollIntervalSec) return;
    if (s_subAccts.accountsPollInFlight) return;
    s_subAccts.lastAccountsPoll = now;
    s_subAccts.accountsPollInFlight = true;
    st.rpc.call("subscription_accounts", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_subAccts.accountsPollInFlight = false;
        if (!r.ok || !r.result.is_object()) {
            // RPC missing / worker down — keep last list; surface once.
            if (!r.ok && s_subAccts.lastError.empty())
                s_subAccts.lastError = r.err.empty() ? "subscription_accounts unavailable"
                                                     : r.err;
            return;
        }
        if (optionalJsonBool(r.result, "ok", true) == false) {
            auto reason = optionalJsonString(r.result, "reason");
            s_subAccts.lastError = reason.empty() ? "accounts failed" : reason;
            return;
        }
        s_subAccts.lastError.clear();
        // Preserve prior usage while rebuilding the membership list (usage poll
        // re-merges; accounts rows may already carry optional usage fields).
        std::map<std::string, SubAccountRow> prevByKey;
        for (const auto& prev : s_subAccts.rows) {
            prevByKey[prev.provider + "\n" + prev.accountId] = prev;
        }
        s_subAccts.rows.clear();
        const auto* arr = optionalJsonArr(r.result, "accounts");
        if (!arr) return;
        for (const auto& a : *arr) {
            if (!a.is_object()) continue;
            SubAccountRow row;
            row.provider  = optionalJsonString(a, "provider");
            row.accountId = optionalJsonString(a, "account_id");
            row.active    = optionalJsonBool(a, "active", false);
            row.connected = optionalJsonBool(a, "connected", true);
            parseAccountIdentity(a, row);
            // If this accounts payload lacked usage, keep the last merged usage.
            if (!row.hasUsage) {
                auto it = prevByKey.find(row.provider + "\n" + row.accountId);
                if (it != prevByKey.end() && it->second.hasUsage) {
                    row.hasUsage = true;
                    row.usage = it->second.usage;
                    if (row.displayName.empty()) row.displayName = it->second.displayName;
                    if (row.plan.empty())        row.plan = it->second.plan;
                }
            }
            s_subAccts.rows.push_back(std::move(row));
        }
    });
    // Also poll status for live login progress (device code etc.).
    st.rpc.call("subscription_status", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        if (!r.ok || !r.result.is_object()) return;
        auto providers = optionalJsonObj(r.result, "providers");
        if (!providers.is_object()) return;
        for (auto it = providers.begin(); it != providers.end(); ++it) {
            if (!it.value().is_object()) continue;
            SubLoginProgress& p = s_subAccts.login[it.key()];
            const auto& x = it.value();
            const bool wasInProgress = p.inProgress;
            p.inProgress = optionalJsonBool(x, "login_in_progress", false);
            p.browserOpened = optionalJsonBool(x, "browser_opened", false);
            p.userCode = optionalJsonString(x, "user_code");
            p.verifyUrl = optionalJsonString(x, "verification_url");
            if (p.verifyUrl.empty())
                p.verifyUrl = optionalJsonString(x, "verification_uri");
            // Prefer the complete device URL (code embedded). Bare /oauth2/device
            // without user_code bounces signed-out browsers to the generic X
            // "tweets with Grok" login instead of the approve form.
            if (!p.userCode.empty() &&
                (p.verifyUrl.empty() ||
                 p.verifyUrl.find("user_code=") == std::string::npos)) {
                const std::string base = p.verifyUrl.empty()
                    ? "https://accounts.x.ai/oauth2/device"
                    : p.verifyUrl;
                const char sep = (base.find('?') == std::string::npos) ? '?' : '&';
                p.verifyUrl = base + sep + "user_code=" + p.userCode;
            }
            p.stage = optionalJsonString(x, "login_stage");
            p.lastError = optionalJsonString(x, "last_error");
            if (wasInProgress && !p.inProgress) {
                // The start RPC completes immediately; status is the completion edge.
                // Force both rows and usage now instead of waiting for their normal TTLs.
                s_subAccts.lastAccountsPoll = -1e9;
                s_subAccts.lastUsagePoll = -1e9;
            }
        }
    });
}

static void drawSubscriptionAccounts(AppState& st, float sc) {
    dimText("Named subscription accounts per provider. Sign in adds another account; "
            "the radio selects which credential the harness uses (seamless switch).");

    // Edge-detect section open (frame gap) → force a usage poll; while visible,
    // refresh every 5 min per contracts/providers.md §4.5.
    const int frame = ImGui::GetFrameCount();
    const bool justOpened = (frame - s_subAccts.lastDrawnFrame) > 1;
    s_subAccts.lastDrawnFrame = frame;
    pollSubscriptionAccounts(st, false);
    pollSubscriptionUsage(st, justOpened);

    // In-app xAI OAuth modal (CEF). Draw every frame so the approve page keeps
    // receiving frames/input even if Accounts scrolls out of view.
    ui::oauthCapture().draw(st, sc);

    if (!s_subAccts.lastError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextWrapped("%s", s_subAccts.lastError.c_str());
        ImGui::PopStyleColor();
    }

    for (int pi = 0; pi < 5; ++pi) {
        const char* prov = kSubProviders[pi];
        const char* plabel = kSubProviderLabels[pi];
        ImGui::PushID(prov);
        ImGui::Dummy(ImVec2(0, 6.0f * sc));
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(ui::palette.accent[0], ui::palette.accent[1], ui::palette.accent[2], 1.0f));
        ImGui::TextUnformatted(plabel);
        ImGui::PopStyleColor();

        // Rows for this provider.
        bool any = false;
        for (size_t i = 0; i < s_subAccts.rows.size(); ++i) {
            SubAccountRow& row = s_subAccts.rows[i];
            if (row.provider != prov) continue;
            any = true;
            ImGui::PushID((int)i);

            // ---- identity row: radio | display_name/email | plan chip | Sign out ----
            bool active = row.active;
            // Radio alone (empty label) — text drawn beside so we can chip + sign-out.
            if (ImGui::RadioButton("##active", active)) {
                if (!active) {
                    nlohmann::json p;
                    p["provider"] = prov;
                    p["account_id"] = row.accountId;
                    st.rpc.call("subscription_switch", p, [](const core::RpcResult& r) {
                        const bool rejected = r.result.is_object() &&
                                              r.result.value("ok", true) == false;
                        if (!r.ok || rejected) {
                            auto reason = rejected
                                ? optionalJsonString(r.result, "reason") : r.err;
                            s_subAccts.lastError =
                                reason.empty() ? "account switch failed" : reason;
                        }
                        s_subAccts.lastAccountsPoll = -1e9;  // force refresh/rollback
                        s_subAccts.lastUsagePoll = -1e9;
                    });
                    // Optimistic local flip (seamless — no restart).
                    for (auto& r2 : s_subAccts.rows)
                        if (r2.provider == prov) r2.active = false;
                    s_subAccts.rows[i].active = true;
                }
            }
            ImGui::SameLine(0, 8.0f * sc);

            // Primary: display_name → email/label → shortened account id (never full UUID).
            // Reserve room for plan chip + Sign out so long labels never clip the actions.
            const char* soLabel = "Sign out";
            const float soW = ImGui::CalcTextSize(soLabel).x
                + ImGui::GetStyle().FramePadding.x * 2.0f;
            const float planChipW = row.plan.empty()
                ? 0.0f
                : (ImGui::CalcTextSize(row.plan.c_str()).x + 12.0f * sc);
            const float gap = 8.0f * sc;
            const float rightBlockW = soW
                + (planChipW > 0.0f ? planChipW + gap : 0.0f)
                + gap;
            const float labelMaxW = std::max(64.0f * sc,
                ImGui::GetContentRegionAvail().x - rightBlockW);

            std::string primary = accountPrimaryLabel(row);
            std::string secondary;
            if (!row.displayName.empty() && !row.label.empty()
                && row.label != row.displayName)
                secondary = row.label;
            if (!row.connected) {
                if (!secondary.empty()) secondary += "  (disconnected)";
                else primary += " (disconnected)";
            }

            // Primary label (ellipsized) + plan chip + Sign out on one baseline.
            ImGui::AlignTextToFramePadding();
            {
                const ImVec2 ts = ImGui::CalcTextSize(primary.c_str());
                if (ts.x > labelMaxW) {
                    ImDrawList* dl = ImGui::GetWindowDrawList();
                    const ImVec2 p0 = ImGui::GetCursorScreenPos();
                    const char* ell = "\xe2\x80\xa6";
                    const float ellW = ImGui::CalcTextSize(ell).x;
                    const float clipW = std::max(ellW + 4.0f * sc, labelMaxW);
                    dl->PushClipRect(p0,
                        ImVec2(p0.x + clipW - ellW, p0.y + ts.y + 2.0f * sc), true);
                    dl->AddText(p0, ImGui::GetColorU32(ImGuiCol_Text), primary.c_str());
                    dl->PopClipRect();
                    dl->AddText(ImVec2(p0.x + clipW - ellW, p0.y),
                                ImGui::GetColorU32(ImGuiCol_Text), ell);
                    ImGui::Dummy(ImVec2(labelMaxW,
                                        std::max(ts.y, ImGui::GetTextLineHeight())));
                } else {
                    ImGui::TextUnformatted(primary.c_str());
                }
                if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)) {
                    if (!secondary.empty() && !row.accountId.empty())
                        ImGui::SetTooltip("%s\n%s\n%s", primary.c_str(), secondary.c_str(),
                                          row.accountId.c_str());
                    else if (!row.accountId.empty() && row.accountId != primary)
                        ImGui::SetTooltip("%s\n%s", primary.c_str(), row.accountId.c_str());
                    else if (!secondary.empty())
                        ImGui::SetTooltip("%s\n%s", primary.c_str(), secondary.c_str());
                }
            }

            if (!row.plan.empty()) {
                ImGui::SameLine(0, gap);
                ImGui::AlignTextToFramePadding();
                drawPlanChip(row.plan, sc);
            }

            ImGui::SameLine(0, 0.0f);
            {
                const float avail = ImGui::GetContentRegionAvail().x;
                if (avail > soW + 4.0f * sc)
                    ImGui::SetCursorPosX(ImGui::GetCursorPosX() + avail - soW);
            }
            if (ImGui::SmallButton(soLabel)) {
                nlohmann::json p;
                p["provider"] = prov;
                p["account_id"] = row.accountId;
                st.rpc.call("subscription_logout", p, [](const core::RpcResult& r) {
                    const bool rejected = r.result.is_object() &&
                                          r.result.value("ok", true) == false;
                    if (!r.ok || rejected) {
                        auto reason = rejected
                            ? optionalJsonString(r.result, "reason") : r.err;
                        s_subAccts.lastError =
                            reason.empty() ? "sign out failed" : reason;
                    }
                    s_subAccts.lastAccountsPoll = -1e9;
                    s_subAccts.lastUsagePoll = -1e9;
                });
                s_subAccts.rows.erase(s_subAccts.rows.begin() + (long)i);
                ImGui::PopID();
                break;
            }

            // Optional email secondary line under the primary identity.
            if (!secondary.empty()) {
                ImGui::Indent(ImGui::GetFrameHeight() + gap);
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
                const ImVec2 sts = ImGui::CalcTextSize(secondary.c_str());
                const float secMax = std::max(48.0f * sc, ImGui::GetContentRegionAvail().x);
                if (sts.x > secMax) {
                    ImDrawList* dl = ImGui::GetWindowDrawList();
                    const ImVec2 sp = ImGui::GetCursorScreenPos();
                    const char* ell = "\xe2\x80\xa6";
                    const float ellW = ImGui::CalcTextSize(ell).x;
                    dl->PushClipRect(sp,
                        ImVec2(sp.x + secMax - ellW, sp.y + sts.y + 2.0f * sc), true);
                    dl->AddText(sp, ui::Palette::toU32(ui::palette.textDim), secondary.c_str());
                    dl->PopClipRect();
                    dl->AddText(ImVec2(sp.x + secMax - ellW, sp.y),
                                ui::Palette::toU32(ui::palette.textDim), ell);
                    ImGui::Dummy(ImVec2(secMax, sts.y));
                } else {
                    ImGui::TextUnformatted(secondary.c_str());
                }
                ImGui::PopStyleColor();
                ImGui::Unindent(ImGui::GetFrameHeight() + gap);
            }

            // ---- usage bars (indented under the identity row) ----
            ImGui::Indent(ImGui::GetFrameHeight() + gap);
            ImGui::Dummy(ImVec2(0, 2.0f * sc));
            if (row.hasUsage && !row.usage.empty()) {
                for (size_t wi = 0; wi < row.usage.size(); ++wi) {
                    ImGui::PushID((int)wi);
                    drawUsageWindow(row.usage[wi], sc);
                    ImGui::PopID();
                }
            } else {
                // Degrade: fields absent or RPC not yet registered.
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
                ImGui::TextUnformatted("usage unavailable");
                ImGui::PopStyleColor();
            }
            ImGui::Unindent(ImGui::GetFrameHeight() + gap);

            ImGui::PopID();
        }
        if (!any) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
            ImGui::TextUnformatted("No accounts signed in");
            ImGui::PopStyleColor();
        }

        // Live login progress for this provider (device-code UX).
        // When the in-app CEF modal owns the xAI flow, suppress the inline
        // external-browser progress (modal shows code + page instead).
        SubLoginProgress& prog = s_subAccts.login[prov];
        const bool cefOwnsXai = (std::string(prov) == "xai") && kXaiUseCef && ui::oauthCapture().active();
        if (cefOwnsXai) {
            // Modal owns the flow; Python status still drives prog via the normal
            // poll above. Do not force-poll every frame (TTL + in-flight guard
            // would still thrash accounts RPCs). Closing mid-flow leaves the
            // Python device-code poll running — same as the external-browser path.
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
            ImGui::TextUnformatted("Sign-in open in embedded browser\xe2\x80\xa6");
            ImGui::PopStyleColor();
        } else if (prog.inProgress) {
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
                // Always surface a code-bearing URL for xAI (see pollSubscriptionAccounts).
                std::string fallbackUrl = prog.verifyUrl;
                if (fallbackUrl.empty() && std::string(prov) == "xai") {
                    fallbackUrl = "https://accounts.x.ai/oauth2/device";
                    if (!prog.userCode.empty())
                        fallbackUrl += "?user_code=" + prog.userCode;
                }
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
            // Allow abandoning a stuck device-code wait without restarting the app.
            // Python may keep polling until TTL; GUI just stops showing the busy state.
            char cancelId[64];
            std::snprintf(cancelId, sizeof(cancelId), "Cancel##login_%s", prov);
            if (ImGui::SmallButton(cancelId)) {
                prog.inProgress = false;
                prog.userCode.clear();
                prog.verifyUrl.clear();
                prog.browserOpened = false;
                prog.stage.clear();
                prog.lastError.clear();
                s_subAccts.lastAccountsPoll = -1e9;
            }
            pollSubscriptionAccounts(st, true);
        } else {
            // First account vs multi-account: label makes "already signed in" obvious.
            // T-RF12: on the web (WASM) build the token store + OAuth flows live in the
            // desktop engine worker, so the button renders disabled with the gap reason
            // as its tooltip instead of firing an RPC that can only fail.
            const char* webWhy = ui::webGapReason("account_signin");
            char addId[64];
            std::snprintf(addId, sizeof(addId),
                          any ? "Add another account##%s" : "Sign in##%s", prov);
            if (webWhy) ImGui::BeginDisabled();
            if (ImGui::SmallButton(addId)) {
                const bool isXai = std::string(prov) == "xai";
                // xAI: CEF path is gated by kXaiUseCef (currently off — Python
                // device/browser token store+rotation handles multi-account).
                // When enabled, prefer in-app CEF; fallback is external browser.
                if (isXai && kXaiUseCef && ui::OauthCapture::cefAvailable()) {
                    prog.inProgress = true;
                    prog.lastError.clear();
                    prog.userCode.clear();
                    prog.verifyUrl.clear();
                    prog.browserOpened = false;
                    prog.stage = "start";
                    ui::oauthCapture().beginXai(st);
                } else {
                    prog.inProgress = true;
                    prog.lastError.clear();
                    prog.userCode.clear();
                    prog.verifyUrl.clear();
                    prog.browserOpened = false;
                    prog.stage = "start";
                    nlohmann::json p;
                    p["provider"] = prov;
                    // xAI is device-code only. Codex uses its browser callback flow here;
                    // its headless device flow cannot surface a code through this UI.
                    if (isXai)
                        p["device"] = true;
                    st.rpc.call("subscription_login", p, [prov](const core::RpcResult& r) {
                        SubLoginProgress& pr = s_subAccts.login[prov];
                        if (!r.ok) {
                            pr.inProgress = false;
                            pr.lastError = r.err.empty() ? "login failed" : r.err;
                        } else if (r.result.is_object() && r.result.value("ok", true) == false) {
                            pr.inProgress = false;
                            auto reason = optionalJsonString(r.result, "reason");
                            auto hint = optionalJsonString(r.result, "hint");
                            pr.lastError = reason.empty() ? "login failed" : reason;
                            if (!hint.empty()) pr.lastError += " \xe2\x80\x94 " + hint;
                        }
                        s_subAccts.lastAccountsPoll = -1e9;
                        s_subAccts.lastUsagePoll = -1e9;
                    });
                }
            }
            if (webWhy) ImGui::EndDisabled();
            // Disabled items need AllowWhenDisabled to still show the explaining tooltip.
            if (webWhy
                    ? ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)
                    : ImGui::IsItemHovered()) {
                if (webWhy)
                    ImGui::SetTooltip("%s", webWhy);
                else if (std::string(prov) == "xai" && kXaiUseCef && ui::OauthCapture::cefAvailable())
                    ImGui::SetTooltip(
                        any
                            ? "Add another %s account in the embedded browser "
                              "(session isolated for multi-account)."
                            : "Sign in to %s in the embedded browser.",
                        plabel);
                else
                    ImGui::SetTooltip(
                        any
                            ? "Add another %s subscription account (browser OAuth)."
                            : "Sign in to %s (browser OAuth). Existing accounts stay listed above.",
                        plabel);
            }
        }
        if (!prog.lastError.empty() && !prog.inProgress) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
            ImGui::TextWrapped("%s", prog.lastError.c_str());
            ImGui::PopStyleColor();
        }
        ImGui::PopID();
    }
}

// ---- OMP provider manager (Settings → Providers) — S-UI-PROV ----------------------
// Two-pane provider sign-in/manager per docs/omp-integration/research/design-provider-signin-ui.md
// §3.1, driven by the frozen libchudware C-ABI methods (OMP_INTEGRATION_PLAN.md RPC CONTRACT),
// which proxy the bun bridge auth_*/models_list methods. The UI holds no auth truth — it
// renders the last agent_provider_list/agent_provider_state response and re-polls on open +
// short TTL (mirrors drawSubscriptionAccounts above). Degrade: unknown_method (sibling slice
// mid-flight / older core) flips s_omp.available off permanently and the tab falls back to
// the legacy Provider & Model / Moonshot / Accounts sections.
struct OmpProviderRow {
    std::string id;            // OMP registry id ("anthropic", "kimi-code", "moonshot", ...)
    std::string label;
    std::string state;         // signed_out|key_set|oauth_connected|error|disabled
    std::string origin;        // runtime|config|oauth|api_key|env|fallback (empty = none)
    std::string envVar;        // when origin == "env"
    std::string error;         // last validate/login error text
    std::string consoleUrl;    // vendor key console deep link
    bool keyless = false;
    bool hasApiKey = false;    // authKinds contains "api_key"
    bool hasOAuth = false;     // authKinds contains "oauth"
    int modelsAvailable = 0;
};
struct OmpAccountRow {
    int credentialId = 0;
    std::string accountId;
    std::string email;
    std::string orgName;
};
struct OmpProvDetail {
    bool loaded = false;
    std::string state;
    std::string origin;
    std::string error;
    std::vector<OmpAccountRow> accounts;
    std::vector<SubUsageWindow> usage;
    std::vector<std::string> usageNotes;
};
struct OmpOAuthFlow {
    bool active = false;
    bool pollInFlight = false;
    std::string handle;
    std::string provider;
    std::string status;        // starting|awaiting_auth|connected|error|cancelled
    std::string url;
    std::string launchUrl;
    std::string instructions;
    std::string progress;
    std::string error;
    double lastPoll = -1e9;
    char codeBuf[512] = "";
};
struct OmpProvUi {
    bool available = true;     // false after unknown_method → legacy fallback
    bool listInFlight = false;
    double lastListPoll = -1e9;
    std::vector<OmpProviderRow> rows;
    std::string selectedId;
    std::string lastError;
    OmpProvDetail detail;
    std::string detailFor;
    bool detailInFlight = false;
    double lastDetailPoll = -1e9;
    // API-key flow (per selected provider)
    char keyBuf[256] = "";
    std::string keyFor;
    bool keyBusy = false;
    std::string keyStatus;
    bool keyStatusErr = false;
    OmpOAuthFlow oauth;
    int lastDrawnFrame = -100;
};
static OmpProvUi s_omp;

static bool ompRpcAbsent(const core::RpcResult& r) {
    if (!r.ok) {
        const std::string& e = r.err;
        if (e.find("unknown") != std::string::npos || e.find("not found") != std::string::npos ||
            e.find("not_found") != std::string::npos || e.find("method") != std::string::npos)
            return true;
        return false;
    }
    if (!r.result.is_object()) return false;
    if (r.result.contains("code")) {
        const std::string c = r.result.value("code", std::string());
        if (c == "unknown_method" || c == "method_not_found" || c == "not_implemented")
            return true;
    }
    if (!r.result.value("ok", true)) {
        const std::string reason = r.result.value("reason", std::string());
        if (reason == "unknown_method" || reason == "method_not_found" ||
            reason == "not_implemented")
            return true;
    }
    return false;
}

// Claude stack + Kimi first (design brief), then signed-in providers, then the rest alpha.
static int ompProviderRank(const OmpProviderRow& r) {
    if (r.id == "anthropic") return 0;
    if (r.id == "kimi-code") return 1;
    if (r.id == "moonshot") return 2;
    if (r.state != "signed_out" && !r.state.empty()) return 3;
    return 4;
}

static void pollOmpProviderList(AppState& st, bool force) {
    if (!s_omp.available || s_omp.listInFlight) return;
    const double now = ImGui::GetTime();
    if (!force && (now - s_omp.lastListPoll) < 3.0) return;
    s_omp.lastListPoll = now;
    s_omp.listInFlight = true;
    st.rpc.call("agent_provider_list", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_omp.listInFlight = false;
        if (ompRpcAbsent(r)) {
            s_omp.available = false;   // permanent degrade → legacy sections
            return;
        }
        if (!r.ok || !r.result.is_object()) {
            if (s_omp.lastError.empty())
                s_omp.lastError = r.err.empty() ? "provider list unavailable" : r.err;
            return;
        }
        if (!optionalJsonBool(r.result, "ok", true)) {
            s_omp.lastError = optionalJsonString(r.result, "error");
            if (s_omp.lastError.empty()) s_omp.lastError = "provider list failed";
            return;
        }
        s_omp.lastError.clear();
        const auto* arr = optionalJsonArr(r.result, "providers");
        if (!arr) return;
        s_omp.rows.clear();
        for (const auto& p : *arr) {
            if (!p.is_object()) continue;
            OmpProviderRow row;
            row.id = optionalJsonString(p, "id");
            if (row.id.empty()) continue;
            row.label = optionalJsonString(p, "label");
            if (row.label.empty()) row.label = row.id;
            row.state = optionalJsonString(p, "state");
            if (row.state.empty()) row.state = "signed_out";
            row.origin = optionalJsonString(p, "origin");
            row.envVar = optionalJsonString(p, "envVar");
            row.error = optionalJsonString(p, "error");
            row.consoleUrl = optionalJsonString(p, "consoleUrl");
            row.keyless = optionalJsonBool(p, "keyless", false);
            row.modelsAvailable = optionalJsonInt(p, "modelsAvailable", 0);
            if (const auto* kinds = optionalJsonArr(p, "authKinds")) {
                for (const auto& k : *kinds) {
                    if (!k.is_string()) continue;
                    const std::string ks = k.get<std::string>();
                    if (ks == "api_key") row.hasApiKey = true;
                    if (ks == "oauth") row.hasOAuth = true;
                }
            }
            s_omp.rows.push_back(std::move(row));
        }
        std::stable_sort(s_omp.rows.begin(), s_omp.rows.end(),
                         [](const OmpProviderRow& a, const OmpProviderRow& b) {
            const int ra = ompProviderRank(a), rb = ompProviderRank(b);
            if (ra != rb) return ra < rb;
            return a.label < b.label;
        });
        if (s_omp.selectedId.empty() && !s_omp.rows.empty())
            s_omp.selectedId = s_omp.rows.front().id;
    });
}

static void fetchOmpProviderDetail(AppState& st, const std::string& id, bool force) {
    if (!s_omp.available || id.empty() || s_omp.detailInFlight) return;
    const double now = ImGui::GetTime();
    if (!force && s_omp.detailFor == id && (now - s_omp.lastDetailPoll) < 60.0) return;
    s_omp.lastDetailPoll = now;
    s_omp.detailInFlight = true;
    s_omp.detailFor = id;
    st.rpc.call("agent_provider_state", nlohmann::json{{"provider", id}},
                [id](const core::RpcResult& r) {
        s_omp.detailInFlight = false;
        if (ompRpcAbsent(r)) { s_omp.available = false; return; }
        if (!r.ok || !r.result.is_object() || !optionalJsonBool(r.result, "ok", true))
            return;   // soft fail — keep last detail
        if (s_omp.detailFor != id) return;   // selection moved on
        OmpProvDetail d;
        d.loaded = true;
        d.state = optionalJsonString(r.result, "state");
        d.origin = optionalJsonString(r.result, "origin");
        d.error = optionalJsonString(r.result, "error");
        if (const auto* accts = optionalJsonArr(r.result, "accounts")) {
            for (const auto& a : *accts) {
                if (!a.is_object()) continue;
                OmpAccountRow ac;
                ac.credentialId = optionalJsonInt(a, "credentialId", 0);
                ac.accountId = optionalJsonString(a, "accountId");
                ac.email = optionalJsonString(a, "email");
                ac.orgName = optionalJsonString(a, "orgName");
                d.accounts.push_back(std::move(ac));
            }
        }
        const nlohmann::json usage = optionalJsonObj(r.result, "usage");
        if (usage.is_object() && !usage.empty()) {
            if (const auto* notes = optionalJsonArr(usage, "notes"))
                for (const auto& n : *notes)
                    if (n.is_string()) d.usageNotes.push_back(n.get<std::string>());
            if (const auto* wins = optionalJsonArr(usage, "windows")) {
                for (const auto& w : *wins) {
                    if (!w.is_object()) continue;
                    SubUsageWindow sw;
                    sw.window = optionalJsonString(w, "id");
                    sw.label = optionalJsonString(w, "label");
                    if (sw.label.empty()) sw.label = optionalJsonString(w, "windowLabel");
                    if (sw.label.empty()) sw.label = sw.window;
                    auto it = w.find("usedFraction");
                    if (it != w.end() && it->is_number()) {
                        sw.usedFraction = (float)it->get<double>();
                        sw.hasFraction = true;
                    }
                    sw.resetsAt = optionalJsonString(w, "resetsAt");
                    sw.status = optionalJsonString(w, "status");
                    if (sw.status.empty()) sw.status = "unknown";
                    d.usage.push_back(std::move(sw));
                }
            }
        }
        s_omp.detail = std::move(d);
    }, 15000);   // auth_state fetches usage reports upstream (8s bridge-side cap)
}

static void refreshOmpProvider(AppState& st) {
    s_omp.lastListPoll = -1e9;
    s_omp.lastDetailPoll = -1e9;
    pollOmpProviderList(st, true);
    fetchOmpProviderDetail(st, s_omp.selectedId, true);
}

// Parse one OAuth flow snapshot (shared by begin_oauth and oauth_poll results).
static void ompOAuthApplySnapshot(const nlohmann::json& r) {
    OmpOAuthFlow& f = s_omp.oauth;
    const std::string st_ = optionalJsonString(r, "status");
    if (!st_.empty()) f.status = st_;
    const std::string url = optionalJsonString(r, "url");
    if (!url.empty()) f.url = url;
    const std::string launch = optionalJsonString(r, "launchUrl");
    if (!launch.empty()) f.launchUrl = launch;
    const std::string instr = optionalJsonString(r, "instructions");
    if (!instr.empty()) f.instructions = instr;
    const std::string prog = optionalJsonString(r, "progress");
    if (!prog.empty()) f.progress = prog;
    const std::string err = optionalJsonString(r, "error");
    if (!err.empty()) f.error = err;
}

static void beginOmpOAuth(AppState& st, const std::string& provider) {
    OmpOAuthFlow& f = s_omp.oauth;
    if (f.active) return;
    f = OmpOAuthFlow{};   // reset (keeps codeBuf zeroed)
    f.active = true;
    f.provider = provider;
    f.status = "starting";
    st.rpc.call("agent_provider_begin_oauth", nlohmann::json{{"provider", provider}},
                [&st](const core::RpcResult& r) {
        OmpOAuthFlow& f = s_omp.oauth;
        if (!r.ok || !r.result.is_object() || !optionalJsonBool(r.result, "ok", true)) {
            f.status = "error";
            f.error = !r.ok ? (r.err.empty() ? "sign-in failed to start" : r.err)
                            : optionalJsonString(r.result, "error");
            if (f.error.empty()) f.error = "sign-in failed to start";
            return;
        }
        f.handle = optionalJsonString(r.result, "handle");
        ompOAuthApplySnapshot(r.result);
        if (f.status == "connected") {
            f.active = false;
            st.addLog("[ok] signed in to " + f.provider);
            refreshOmpProvider(st);
        }
    }, 30000);   // bridge waits up to 20s for the first onAuth callback
}

static void pollOmpOAuth(AppState& st) {
    OmpOAuthFlow& f = s_omp.oauth;
    if (!f.active || f.handle.empty() || f.pollInFlight) return;
    if (f.status != "starting" && f.status != "awaiting_auth") return;
    const double now = ImGui::GetTime();
    if ((now - f.lastPoll) < 1.0) return;
    f.lastPoll = now;
    f.pollInFlight = true;
    st.rpc.call("agent_provider_oauth_poll", nlohmann::json{{"handle", f.handle}},
                [&st](const core::RpcResult& r) {
        OmpOAuthFlow& f = s_omp.oauth;
        f.pollInFlight = false;
        if (!r.ok || !r.result.is_object() || !optionalJsonBool(r.result, "ok", true)) {
            // Transient poll failure — keep waiting; the flow itself owns the TTL.
            return;
        }
        ompOAuthApplySnapshot(r.result);
        if (f.status == "connected") {
            f.active = false;
            st.addLog("[ok] signed in to " + f.provider);
            refreshOmpProvider(st);
        } else if (f.status == "error" || f.status == "cancelled") {
            f.active = false;
        }
    }, 10000);
}

static const float* ompStateColor(const std::string& state) {
    if (state == "oauth_connected" || state == "key_set") return ui::palette.statusOk;
    if (state == "error") return ui::palette.statusErr;
    return ui::palette.textDim;   // signed_out / disabled / unknown
}

static const char* ompStateLabel(const OmpProviderRow& r) {
    if (r.state == "oauth_connected") return "OAuth connected";
    if (r.state == "key_set")
        return r.origin == "env" ? "key set (env)" : "API key set";
    if (r.state == "error") return "error";
    if (r.state == "disabled") return "disabled";
    if (r.keyless) return "keyless";
    return "signed out";
}

// Inline status dot + colored label on one line. The bundled UI font has no
// Geometric-Shapes glyphs (U+25CF/U+25CB), so the old literal bullets rendered
// as "?"; draw the dot with the draw-list instead (matches drawStatusDot in
// plm_overview.cpp and the dozens of AddCircleFilled status dots elsewhere).
// filled = solid dot (connected / key set); else a hollow ring (signed out).
static void ompStatusDotText(const char* label, const float col[3], bool filled,
                             const float* labelCol = nullptr) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float sc = ui::uiScale;
    const float fh = ImGui::GetFontSize();
    const float r = fh * 0.30f;
    const ImVec2 p = ImGui::GetCursorScreenPos();
    const ImVec2 c(p.x + r + 1.0f * sc, p.y + fh * 0.5f);
    const ImU32 u = ImGui::ColorConvertFloat4ToU32(ImVec4(col[0], col[1], col[2], 1.0f));
    if (filled) dl->AddCircleFilled(c, r, u, 16);
    else        dl->AddCircle(c, r, u, 16, 1.4f * sc);
    ImGui::Dummy(ImVec2(r * 2.0f + 6.0f * sc, fh));  // reserve the dot's width
    ImGui::SameLine(0, 0);
    const ImU32 tu = labelCol
        ? ImGui::ColorConvertFloat4ToU32(ImVec4(labelCol[0], labelCol[1], labelCol[2], 1.0f))
        : u;
    ImGui::PushStyleColor(ImGuiCol_Text, tu);
    ImGui::TextUnformatted(label);
    ImGui::PopStyleColor();
}

static void drawOmpProviderDetail(AppState& st, float sc) {
    const OmpProviderRow* row = nullptr;
    for (const auto& r : s_omp.rows)
        if (r.id == s_omp.selectedId) { row = &r; break; }
    if (!row) {
        dimText("Select a provider on the left.");
        return;
    }
    const OmpProviderRow& prov = *row;
    const auto& pal = ui::palette;
    ImGui::PushID(prov.id.c_str());

    fetchOmpProviderDetail(st, prov.id, false);
    const bool detailReady = s_omp.detail.loaded && s_omp.detailFor == prov.id;
    const std::string state = detailReady && !s_omp.detail.state.empty()
                                  ? s_omp.detail.state : prov.state;

    // ---- header: label + state + disable toggle (mapped CAD categories only) ----
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1.0f));
    ImGui::TextUnformatted(prov.label.c_str());
    ImGui::PopStyleColor();
    const float* scol = ompStateColor(state);
    ImGui::SameLine(0, 8.0f * sc);
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(scol[0], scol[1], scol[2], 1.0f));
    ImGui::TextUnformatted(ompStateLabel(prov));
    ImGui::PopStyleColor();
    // Disable = CAD's provider-category off switch (composer picker hides the models;
    // credentials stay). Only the bridge providers with a CAD category key get the toggle.
    const char* catKey = nullptr;
    if (prov.id == "anthropic") catKey = "anthropic";
    else if (prov.id == "kimi-code") catKey = "kimi-code";
    else if (prov.id == "moonshot") catKey = "moonshot";
    if (catKey) {
        ImGui::SameLine(0, 16.0f * sc);
        bool disabled = st.settings.disabledProviders.count(catKey) != 0;
        if (ImGui::Checkbox("Disable", &disabled)) {
            if (disabled) st.settings.disabledProviders.insert(catKey);
            else st.settings.disabledProviders.erase(catKey);
            saveSettings(st.settings);
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Hide this provider's models from the copilot picker. "
                              "Credentials are kept — re-enable restores.");
    }

    // ---- Sign-in section ----
    settingsGlassSectionBegin("##omp_signin", "Sign-in");
    if (state == "error") {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.statusErr));
        ImGui::TextWrapped("%s", (detailReady && !s_omp.detail.error.empty()
                                      ? s_omp.detail.error : prov.error).c_str());
        ImGui::PopStyleColor();
        if (ImGui::SmallButton("Retry")) refreshOmpProvider(st);
        ImGui::SameLine(0, 8.0f * sc);
    }
    if (state == "oauth_connected") {
        ompStatusDotText("OAuth connected", pal.statusOk, /*filled=*/true);
        if (detailReady) {
            for (const auto& ac : s_omp.detail.accounts) {
                ImGui::PushID(ac.credentialId);
                std::string who = !ac.email.empty() ? ac.email
                                  : (!ac.accountId.empty() ? shortAccountId(ac.accountId)
                                                           : "(account)");
                ImGui::Bullet();
                ImGui::TextUnformatted(who.c_str());
                if (!ac.orgName.empty()) {
                    ImGui::SameLine(0, 6.0f * sc);
                    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                    ImGui::TextUnformatted(ac.orgName.c_str());
                    ImGui::PopStyleColor();
                }
                ImGui::PopID();
            }
        }
        if (prov.hasOAuth && !s_omp.oauth.active) {
            if (ImGui::SmallButton("Add account")) beginOmpOAuth(st, prov.id);
            ImGui::SameLine(0, 8.0f * sc);
        }
        if (ImGui::SmallButton("Sign out")) {
            st.rpc.call("agent_provider_logout",
                        nlohmann::json{{"provider", prov.id}},
                        [&st, pid = prov.id](const core::RpcResult& r) {
                if (!r.ok || !r.result.is_object() ||
                    !optionalJsonBool(r.result, "ok", true)) {
                    s_omp.lastError = !r.ok ? r.err : optionalJsonString(r.result, "error");
                } else {
                    s_omp.lastError.clear();
                }
                refreshOmpProvider(st);
            });
        }
    } else if (state == "key_set") {
        ompStatusDotText(prov.origin == "env" && !prov.envVar.empty()
                             ? (std::string("key set via ") + prov.envVar).c_str()
                             : "API key set",
                         pal.statusOk, /*filled=*/true);
        if (ImGui::SmallButton("Remove key")) {
            st.rpc.call("agent_provider_remove_key",
                        nlohmann::json{{"provider", prov.id}},
                        [&st](const core::RpcResult& r) {
                if (!r.ok || !r.result.is_object() ||
                    !optionalJsonBool(r.result, "ok", true))
                    s_omp.lastError = !r.ok ? r.err : optionalJsonString(r.result, "error");
                refreshOmpProvider(st);
            });
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Remove the stored key from the auth bridge. "
                              "Paste a new key below to replace it.");
    } else if (prov.keyless) {
        dimText("No credential needed — this provider serves models without a key.");
    } else {
        ompStatusDotText("signed out", pal.textDim, /*filled=*/false);
    }

    // OAuth entry point + in-flight flow.
    OmpOAuthFlow& flow = s_omp.oauth;
    const bool flowHere = flow.active && flow.provider == prov.id;
    if (prov.hasOAuth && state != "oauth_connected" && !flow.active) {
        if (primaryButton("Sign in\xe2\x80\xa6", ImVec2(140.0f * sc, 0))) {
            beginOmpOAuth(st, prov.id);
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Browser OAuth (PKCE loopback or device code, whichever the "
                              "provider supports). A manual code paste fallback is offered "
                              "when the browser can't be opened.");
    }
    if (flowHere || (!flow.active && flow.provider == prov.id &&
                     (flow.status == "error" || flow.status == "cancelled"))) {
        ImGui::Dummy(ImVec2(0, 4.0f * sc));
        if (flow.status == "starting") {
            ImGui::TextUnformatted("Starting sign-in\xe2\x80\xa6");
        } else if (flow.status == "awaiting_auth") {
            if (!flow.progress.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                ImGui::TextWrapped("%s", flow.progress.c_str());
                ImGui::PopStyleColor();
            } else {
                ImGui::TextUnformatted("Waiting for browser approval\xe2\x80\xa6");
            }
            if (!flow.instructions.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                ImGui::TextWrapped("%s", flow.instructions.c_str());
                ImGui::PopStyleColor();
            }
            const std::string& openUrl = !flow.launchUrl.empty() ? flow.launchUrl : flow.url;
            if (!openUrl.empty()) {
                ImGui::TextWrapped("%s", openUrl.c_str());
                if (ImGui::SmallButton("Copy URL")) ImGui::SetClipboardText(openUrl.c_str());
                ImGui::SameLine(0, 6.0f * sc);
                if (ImGui::SmallButton("Open in browser")) {
#if defined(_WIN32)
                    std::system(("start \"\" \"" + openUrl + "\"").c_str());
#elif defined(__APPLE__)
                    std::system(("open '" + openUrl + "'").c_str());
#else
                    std::system(("xdg-open '" + openUrl + "' >/dev/null 2>&1 &").c_str());
#endif
                }
            }
            // Manual fallback: paste the device code / full redirect URL.
            ImGui::SetNextItemWidth(-1);
            ImGui::InputTextWithHint("##omp_oauth_code", "Paste code or redirect URL\xe2\x80\xa6",
                                     flow.codeBuf, sizeof(flow.codeBuf));
            if (ImGui::SmallButton("Submit code") && flow.codeBuf[0]) {
                st.rpc.call("agent_provider_oauth_manual",
                            nlohmann::json{{"handle", flow.handle}, {"code", flow.codeBuf}},
                            [](const core::RpcResult& r) {
                    if (!r.ok || !r.result.is_object() ||
                        !optionalJsonBool(r.result, "ok", true))
                        s_omp.oauth.error = !r.ok ? r.err
                                                  : optionalJsonString(r.result, "error");
                    s_omp.oauth.codeBuf[0] = 0;
                    s_omp.oauth.lastPoll = -1e9;   // pick up the result on the next poll
                });
            }
            ImGui::SameLine(0, 6.0f * sc);
            if (ImGui::SmallButton("Cancel")) {
                st.rpc.call("agent_provider_oauth_cancel",
                            nlohmann::json{{"handle", flow.handle}},
                            [](const core::RpcResult&) {});
                flow.active = false;
                flow.status = "cancelled";
            }
        } else if (flow.status == "error") {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.statusErr));
            ImGui::TextWrapped("%s", flow.error.empty() ? "Sign-in failed" : flow.error.c_str());
            ImGui::PopStyleColor();
            if (prov.hasOAuth && ImGui::SmallButton("Try again")) beginOmpOAuth(st, prov.id);
        } else if (flow.status == "cancelled") {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
            ImGui::TextUnformatted("Sign-in cancelled.");
            ImGui::PopStyleColor();
        }
    }

    // API-key block (inline; validate → set, with an explicit unvalidated save for providers
    // whose wire API has no cheap validation endpoint).
    if (prov.hasApiKey) {
        ImGui::Dummy(ImVec2(0, 4.0f * sc));
        if (s_omp.keyFor != prov.id) {
            s_omp.keyFor = prov.id;
            s_omp.keyBuf[0] = 0;
            s_omp.keyStatus.clear();
        }
        ImGui::TextUnformatted(state == "key_set" ? "Replace API key" : "Add API key");
        ImGui::SetNextItemWidth(-1);
        ImGui::InputTextWithHint("##omp_key", "Paste key\xe2\x80\xa6", s_omp.keyBuf,
                                 sizeof(s_omp.keyBuf), ImGuiInputTextFlags_Password);
        const bool haveInput = s_omp.keyBuf[0] != 0;
        if (s_omp.keyBusy) ImGui::BeginDisabled();
        if (ImGui::SmallButton("Validate \x26 add") && haveInput && !s_omp.keyBusy) {
            s_omp.keyBusy = true;
            s_omp.keyStatus.clear();
            const std::string pid = prov.id;
            const std::string key = s_omp.keyBuf;
            st.rpc.call("agent_provider_validate_key",
                        nlohmann::json{{"provider", pid}, {"apiKey", key}},
                        [&st, pid, key](const core::RpcResult& r) {
                bool ok = r.ok && r.result.is_object() &&
                          optionalJsonBool(r.result, "ok", false);
                if (!ok) {
                    s_omp.keyBusy = false;
                    s_omp.keyStatusErr = true;
                    s_omp.keyStatus = !r.ok ? (r.err.empty() ? "validation failed" : r.err)
                                            : optionalJsonString(r.result, "error");
                    if (s_omp.keyStatus.empty())
                        s_omp.keyStatus = optionalJsonString(r.result, "detail");
                    if (s_omp.keyStatus.empty()) s_omp.keyStatus = "key rejected";
                    return;
                }
                st.rpc.call("agent_provider_set_key",
                            nlohmann::json{{"provider", pid}, {"apiKey", key}},
                            [&st](const core::RpcResult& sr) {
                    s_omp.keyBusy = false;
                    if (!sr.ok || !sr.result.is_object() ||
                        !optionalJsonBool(sr.result, "ok", true)) {
                        s_omp.keyStatusErr = true;
                        s_omp.keyStatus = !sr.ok ? sr.err
                                                 : optionalJsonString(sr.result, "error");
                        return;
                    }
                    s_omp.keyStatusErr = false;
                    s_omp.keyStatus = "key saved";
                    s_omp.keyBuf[0] = 0;
                    refreshOmpProvider(st);
                });
            }, 20000);
        }
        ImGui::SameLine(0, 6.0f * sc);
        if (ImGui::SmallButton("Add unvalidated") && haveInput && !s_omp.keyBusy) {
            s_omp.keyBusy = true;
            s_omp.keyStatus.clear();
            const std::string pid = prov.id;
            const std::string key = s_omp.keyBuf;
            st.rpc.call("agent_provider_set_key",
                        nlohmann::json{{"provider", pid}, {"apiKey", key}},
                        [&st](const core::RpcResult& sr) {
                s_omp.keyBusy = false;
                if (!sr.ok || !sr.result.is_object() ||
                    !optionalJsonBool(sr.result, "ok", true)) {
                    s_omp.keyStatusErr = true;
                    s_omp.keyStatus = !sr.ok ? sr.err
                                             : optionalJsonString(sr.result, "error");
                    return;
                }
                s_omp.keyStatusErr = false;
                s_omp.keyStatus = "key saved (not validated)";
                s_omp.keyBuf[0] = 0;
                refreshOmpProvider(st);
            });
        }
        if (s_omp.keyBusy) ImGui::EndDisabled();
        if (!s_omp.keyStatus.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ui::Palette::toU32(s_omp.keyStatusErr ? pal.statusErr : pal.statusOk));
            ImGui::TextWrapped("%s", s_omp.keyStatus.c_str());
            ImGui::PopStyleColor();
        }
        if (!prov.consoleUrl.empty()) {
            if (ImGui::SmallButton("Get a key")) {
#if defined(_WIN32)
                std::system(("start \"\" \"" + prov.consoleUrl + "\"").c_str());
#elif defined(__APPLE__)
                std::system(("open '" + prov.consoleUrl + "'").c_str());
#else
                std::system(("xdg-open '" + prov.consoleUrl + "' >/dev/null 2>&1 &").c_str());
#endif
            }
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", prov.consoleUrl.c_str());
        }
    }
    settingsGlassSectionEnd();

    // ---- Usage section (OAuth subscription windows + observed-spend notes) ----
    if (detailReady && !s_omp.detail.usage.empty()) {
        settingsGlassSectionBegin("##omp_usage", "Usage");
        for (const auto& w : s_omp.detail.usage) drawUsageWindow(w, sc);
        for (const auto& n : s_omp.detail.usageNotes) dimText(n.c_str());
        settingsGlassSectionEnd();
    }

    // ---- Models section (OMP catalog: capability chips + ctx + $in/$out per Mtok) ----
    {
        int count = 0;
        for (const auto& m : ui::bridgeModelCatalog())
            if (m.provider == prov.id) ++count;
        char modelsTitle[64];
        std::snprintf(modelsTitle, sizeof(modelsTitle), "Models (%d)",
                      count > 0 ? count : prov.modelsAvailable);
        settingsGlassSectionBegin("##omp_models", modelsTitle);
        if (count == 0) {
            dimText(s_omp.available
                        ? "Catalog loading \xe2\x80\x94 or no bundled models for this provider."
                        : "Catalog unavailable.");
        }
        for (const auto& m : ui::bridgeModelCatalog()) {
            if (m.provider != prov.id) continue;
            ImGui::PushID(m.id.c_str());
            bool hidden = st.settings.hiddenModels.count(m.id) != 0;
            bool shown = !hidden;
            if (ImGui::Checkbox("##show", &shown)) {
                if (shown) st.settings.hiddenModels.erase(m.id);
                else st.settings.hiddenModels.insert(m.id);
                saveSettings(st.settings);
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Show/hide in the copilot model picker.");
            ImGui::SameLine(0, 6.0f * sc);
            ImGui::TextUnformatted(m.name.c_str());
            const std::string sub = ui::bridgeModelSublabel(m);
            if (!sub.empty()) {
                const float sw = ImGui::CalcTextSize(sub.c_str()).x;
                const float rx = ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x
                                 - sw;
                ImGui::SameLine(rx > ImGui::GetCursorPosX() ? rx : 0, 6.0f * sc);
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                ImGui::TextUnformatted(sub.c_str());
                ImGui::PopStyleColor();
            }
            const std::string detail = ui::bridgeModelCostDetail(m);
            if (!detail.empty() && ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", detail.c_str());
            ImGui::PopID();
        }
        settingsGlassSectionEnd();
    }
    ImGui::PopID();
}

// Two-pane provider manager: left = provider list with sign-in state, right = detail
// (Sign-in / Usage / Models). Footer = per-session cost strip. Single-column fallback under
// 640*sc: the list collapses to a combo (design §3.1).
static void drawOmpProviderManager(AppState& st) {
    const float sc = ui::uiScale;
    const auto& pal = ui::palette;
    pollOmpProviderList(st, false);
    pollOmpOAuth(st);
    ui::refreshBridgeModelCatalog(st, false);

    if (!s_omp.available) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.statusWarn));
        ImGui::TextWrapped("Provider sign-in bridge unavailable in this build \xe2\x80\x94 "
                           "legacy provider settings below.");
        ImGui::PopStyleColor();
        return;
    }
    if (!s_omp.lastError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.statusWarn));
        ImGui::TextWrapped("%s", s_omp.lastError.c_str());
        ImGui::PopStyleColor();
    }

    const float footerH = ImGui::GetFrameHeightWithSpacing() + 4.0f * sc;
    const float paneH = -footerH;
    const float availW = ImGui::GetContentRegionAvail().x;
    const bool narrow = availW < 640.0f * sc;

    if (narrow) {
        // Collapsed: provider pick as a combo, detail below.
        std::string preview = s_omp.selectedId;
        for (const auto& r : s_omp.rows)
            if (r.id == s_omp.selectedId) { preview = r.label; break; }
        ImGui::SetNextItemWidth(-1);
        if (ImGui::BeginCombo("##omp_prov_pick", preview.c_str())) {
            for (const auto& r : s_omp.rows) {
                ImGui::PushID(r.id.c_str());
                if (ImGui::Selectable(r.label.c_str(), r.id == s_omp.selectedId))
                    s_omp.selectedId = r.id;
                const float* c = ompStateColor(r.state);
                ImGui::SameLine();
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(c[0], c[1], c[2], 1.0f));
                ImGui::TextUnformatted(ompStateLabel(r));
                ImGui::PopStyleColor();
                ImGui::PopID();
            }
            ImGui::EndCombo();
        }
    } else {
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(8.0f * sc, 6.0f * sc));
        ImGui::BeginChild("##omp_prov_list", ImVec2(220.0f * sc, paneH),
                          ImGuiChildFlags_Borders);
        for (const auto& r : s_omp.rows) {
            ImGui::PushID(r.id.c_str());
            const bool sel = (r.id == s_omp.selectedId);
            if (ImGui::Selectable("##row", sel, 0,
                                  ImVec2(0, ImGui::GetTextLineHeight() * 2 + 8.0f * sc))) {
                s_omp.selectedId = r.id;
            }
            ImDrawList* dl = ImGui::GetWindowDrawList();
            const ImVec2 mn = ImGui::GetItemRectMin();
            const float fh = ImGui::GetFontSize();
            dl->AddText(ImVec2(mn.x + 6.0f * sc, mn.y + 4.0f * sc),
                        ui::Palette::toU32(sel ? pal.text : pal.textDim), r.label.c_str());
            const float* c = ompStateColor(r.state);
            const bool bullet = (r.state == "oauth_connected" || r.state == "key_set");
            char statusLine[160];
            std::snprintf(statusLine, sizeof(statusLine), "%s", ompStateLabel(r));
            const float dotR = fh * 0.28f;
            const float sy = mn.y + 4.0f * sc + fh + 2.0f * sc;
            const ImU32 cu = ImGui::ColorConvertFloat4ToU32(ImVec4(c[0], c[1], c[2], 1.0f));
            const ImVec2 dc(mn.x + 6.0f * sc + dotR, sy + fh * 0.5f);
            if (bullet) dl->AddCircleFilled(dc, dotR, cu, 16);
            else        dl->AddCircle(dc, dotR, cu, 16, 1.4f * sc);
            dl->AddText(ImVec2(mn.x + 6.0f * sc + dotR * 2.0f + 5.0f * sc, sy), cu, statusLine);
            ImGui::PopID();
        }
        if (s_omp.rows.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
            ImGui::TextWrapped(s_omp.listInFlight ? "Loading providers\xe2\x80\xa6"
                                                  : "No providers reported by the bridge.");
            ImGui::PopStyleColor();
        }
        ImGui::EndChild();
        ImGui::PopStyleVar();
        ImGui::SameLine(0, 8.0f * sc);
    }

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGui::BeginChild("##omp_prov_detail", ImVec2(0, paneH));
    drawOmpProviderDetail(st, sc);
    ImGui::EndChild();
    ImGui::PopStyleVar();

    // Footer: per-session cost strip (agent_cost_summary; hides itself when unavailable).
    ui::drawSessionCostStrip(st);
}

// ---- Run benchmark + categories (Settings → Benchmarks) ---------------------
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
    std::string activeCategory;  // non-empty when last launch was per-category
};
static BenchUi s_bench;

struct BenchCategoryLast {
    std::string when;
    float passRate = -1.f;  // <0 = absent
    std::string detailPath;
};
struct BenchCategory {
    std::string id;
    std::string label;
    std::string description;
    std::vector<std::string> toolsCovered;
    bool runnable = true;
    bool hasLast = false;
    BenchCategoryLast last;
};
struct BenchCategoriesUi {
    std::vector<BenchCategory> cats;
    bool loaded = false;
    bool rpcMissing = false;
    bool pollInFlight = false;
    double lastPoll = -1e9;
    std::string lastError;
};
static BenchCategoriesUi s_cats;

// ---- Model roles (Settings → AI → Roles) -------------------------------------
// RPC: ai_roles_get/set. Degrade read-only when worker offline.
// when the RPC is absent.
struct RoleAssign {
    bool isAuto = true;
    std::string provider;
    std::string model;
    // Per-role reasoning effort: "auto" | "low" | "medium" | "high" (default auto).
    std::string thinking = "auto";
};
struct ModelRolesUi {
    std::map<std::string, RoleAssign> roles;
    bool rolesLoaded = false;
    bool rolesRpcMissing = false;
    bool rolesPollInFlight = false;
    double lastRolesPoll = -1e9;
    std::string lastError;
    bool rolesSaveInFlight = false;
};
static ModelRolesUi s_roles;

static const char* kRoleKeys[] = {
    "advisor", "task", "planning", "vision", "default", "plm", "redaction",
    "sketch2d", "model3d", "memory", "cam", "sim", "ecad", "drawing", "reveng"};
static const char* kRoleLabels[] = {
    "Advisor", "Task", "Planning", "Vision", "Default", "PLM", "Redaction",
    "Sketch 2D", "Model 3D", "Memory", "CAM", "Simulation", "ECAD", "Drawing",
    "Reverse eng"};
static constexpr int kRoleCount = 15;

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
        // Moonshot first-class wire provider when the cloud base is api.moonshot.ai.
        if (std::strstr(s.baseUrl, "moonshot"))
            return std::string("moonshot:") + model;
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
        // Transition running → done: auto-refresh leaderboard + categories.
        if (was && !s_bench.running) {
            if (lbRefresh) *lbRefresh = ui::leaderboard::load();
            s_cats.lastPoll = -1e9;  // force category re-poll
            s_cats.loaded = false;
        }
        s_bench.wasRunning = s_bench.running;
    });
}

static void pollBenchCategories(AppState& st, bool force) {
    const double now = ImGui::GetTime();
    if (!force && s_cats.loaded && (now - s_cats.lastPoll) < 4.0) return;
    if (s_cats.pollInFlight) return;
    s_cats.lastPoll = now;
    s_cats.pollInFlight = true;
    st.rpc.call("bench_categories", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_cats.pollInFlight = false;
        if (!r.ok) {
            s_cats.rpcMissing = true;
            if (s_cats.lastError.empty())
                s_cats.lastError = r.err.empty() ? "bench_categories unavailable" : r.err;
            s_cats.loaded = true;
            return;
        }
        if (!r.result.is_object()) {
            s_cats.rpcMissing = true;
            s_cats.loaded = true;
            return;
        }
        if (r.result.value("ok", true) == false) {
            s_cats.lastError = r.result.value("reason", std::string("bench_categories failed"));
            s_cats.loaded = true;
            return;
        }
        s_cats.rpcMissing = false;
        s_cats.lastError.clear();
        s_cats.cats.clear();
        auto arr = r.result.value("categories", nlohmann::json::array());
        if (!arr.is_array()) { s_cats.loaded = true; return; }
        for (const auto& c : arr) {
            if (!c.is_object()) continue;
            BenchCategory row;
            row.id = c.value("id", std::string());
            row.label = c.value("label", row.id);
            row.description = c.value("description", std::string());
            row.runnable = c.value("runnable", true);
            auto tools = c.value("tools_covered", nlohmann::json::array());
            if (tools.is_array()) {
                for (const auto& t : tools)
                    if (t.is_string()) row.toolsCovered.push_back(t.get<std::string>());
            }
            if (c.contains("last") && c["last"].is_object() && !c["last"].is_null()) {
                const auto& L = c["last"];
                row.hasLast = true;
                row.last.when = L.value("when", std::string());
                if (L.contains("pass_rate") && L["pass_rate"].is_number())
                    row.last.passRate = (float)L["pass_rate"].get<double>();
                row.last.detailPath = L.value("detail_path", std::string());
            }
            s_cats.cats.push_back(std::move(row));
        }
        s_cats.loaded = true;
    });
}

// Wire provider name used by role/ranking RPCs (matches routing / config conventions).
// For PROV_CLOUDCOMPAT, Moonshot presets map to first-class "moonshot"; others stay "openrouter".
static const char* roleProviderWire(int prov, const std::string& model = {}) {
    if (prov == ui::PROV_CLOUDCOMPAT) {
        // Moonshot is first-class ("moonshot"); other cloud presets stay "openrouter".
        for (const auto& p : ui::cloudPresets()) {
            if (p.id == model &&
                std::strcmp(ui::cloudServiceKey(p.baseUrl), "moonshot") == 0)
                return "moonshot";
        }
        return "openrouter";
    }
    switch (prov) {
        case ui::PROV_ANTHROPIC:   return "anthropic";
        case ui::PROV_OPENAI:      return "openai";
        case ui::PROV_LOCAL:       return "local";
        case ui::PROV_LLAMACPP:    return "llamacpp";
        case ui::PROV_GEMINI:      return "gemini";
        case ui::PROV_XAI:         return "xai";
        case ui::PROV_CURSOR:      return "cursor";
        case ui::PROV_KIMISUB:     return "kimi-sub";
        default: return "local";
    }
}

static int roleProviderFromWire(const std::string& w) {
    if (w == "anthropic") return ui::PROV_ANTHROPIC;
    if (w == "openai") return ui::PROV_OPENAI;
    if (w == "local") return ui::PROV_LOCAL;
    if (w == "llamacpp") return ui::PROV_LLAMACPP;
    if (w == "gemini") return ui::PROV_GEMINI;
    if (w == "openrouter" || w == "cloud" || w == "cloudcompat" || w == "moonshot")
        return ui::PROV_CLOUDCOMPAT;
    if (w == "xai" || w == "xai-sub") return ui::PROV_XAI;
    if (w == "cursor" || w == "cursor-sub") return ui::PROV_CURSOR;
    if (w == "kimi-sub") return ui::PROV_KIMISUB;
    return -1;
}

static bool providerIsLoopback(int prov) {
    return prov == ui::PROV_LOCAL || prov == ui::PROV_LLAMACPP;
}

// Flat picker entry for role model dropdowns.
struct RolePickEntry {
    bool isAuto = false;
    int provider = ui::PROV_LOCAL;
    std::string model;
    std::string label;  // display
};

static std::vector<RolePickEntry> buildRolePickList(AppState& st, bool localOnly) {
    std::vector<RolePickEntry> out;
    out.push_back(RolePickEntry{true, 0, "", "Auto"});
    // Keep the pick list aligned with the composer dropdown: only providers the user can
    // actually run (API key present, subscription signed in, or local endpoint up).
    ui::refreshModelAvailability(st, false);
    auto add = [&](int prov, const std::string& model, const std::string& label) {
        if (localOnly && !providerIsLoopback(prov)) return;
        if (!ui::providerUsable(st, prov)) return;
        RolePickEntry e;
        e.isAuto = false;
        e.provider = prov;
        e.model = model;
        e.label = label;
        out.push_back(std::move(e));
    };
    // Built-in cloud catalog (Gemini / Anthropic / OpenAI / xAI / Cursor / Kimi Code).
    for (const auto& m : ui::builtinModels()) {
        char buf[256];
        std::snprintf(buf, sizeof(buf), "%s \xc2\xb7 %s",
                      ui::providerLabel(m.provider), m.label.c_str());
        add(m.provider, m.id, buf);
    }
    // Cloud presets: OpenRouter/Alibaba/DeepSeek need cloud key (local or host-shared);
    // Moonshot needs moonshot key (local or host-shared).
    for (const auto& p : ui::cloudPresets()) {
        const char* svc = ui::cloudServiceKey(p.baseUrl);
        if (std::strcmp(svc, "moonshot") == 0) {
            if (!ui::moonshotKeyPresent(st)) continue;
        } else if (!ui::cloudCompatKeyPresent(st)) {
            continue;
        }
        add(ui::PROV_CLOUDCOMPAT, p.id, p.label);
    }
    // Local presets + live-probed local models — only when local is actually in play:
    // redaction/local-only picker, active provider is local, a local probe succeeded, or
    // Offline/ITAR mode. Cloud-only users with no local server see no Local rows.
    const bool includeLocal =
        localOnly ||
        st.settings.offlineMode ||
        providerIsLoopback(st.settings.provider) ||
        st.providerModels[ui::PROV_LOCAL].ok ||
        st.providerModels[ui::PROV_LLAMACPP].ok;
    if (includeLocal) {
        for (const auto& p : ui::localPresets()) {
            // Presets still require a usable local endpoint (or offline intent).
            if (!ui::providerUsable(st, ui::PROV_LOCAL) && !st.settings.offlineMode &&
                !providerIsLoopback(st.settings.provider))
                break;
            char buf[256];
            std::snprintf(buf, sizeof(buf), "Local \xc2\xb7 %s", p.label.c_str());
            add(ui::PROV_LOCAL, p.id, buf);
        }
        const ProviderModels& lpm = st.providerModels[ui::PROV_LOCAL];
        if (lpm.ok) {
            for (const auto& id : lpm.models) {
                char buf[256];
                std::snprintf(buf, sizeof(buf), "Local \xc2\xb7 %s", id.c_str());
                add(ui::PROV_LOCAL, id, buf);
            }
        }
        const ProviderModels& lcpp = st.providerModels[ui::PROV_LLAMACPP];
        if (lcpp.ok) {
            for (const auto& id : lcpp.models) {
                char buf[256];
                std::snprintf(buf, sizeof(buf), "llama.cpp \xc2\xb7 %s", id.c_str());
                add(ui::PROV_LLAMACPP, id, buf);
            }
        }
    }
    return out;
}

static void ensureDefaultRoles() {
    for (int i = 0; i < kRoleCount; ++i) {
        if (s_roles.roles.find(kRoleKeys[i]) == s_roles.roles.end()) {
            RoleAssign a;
            a.isAuto = true;
            s_roles.roles[kRoleKeys[i]] = a;
        }
    }
}

static void pollModelRoles(AppState& st, bool force) {
    const double now = ImGui::GetTime();
    if (!force && s_roles.rolesLoaded && (now - s_roles.lastRolesPoll) < 5.0) return;
    if (s_roles.rolesPollInFlight || s_roles.rolesSaveInFlight) return;
    s_roles.lastRolesPoll = now;
    s_roles.rolesPollInFlight = true;
    st.rpc.call("ai_roles_get", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_roles.rolesPollInFlight = false;
        if (!r.ok) {
            s_roles.rolesRpcMissing = true;
            ensureDefaultRoles();
            s_roles.rolesLoaded = true;
            return;
        }
        if (!r.result.is_object()) {
            s_roles.rolesRpcMissing = true;
            ensureDefaultRoles();
            s_roles.rolesLoaded = true;
            return;
        }
        s_roles.rolesRpcMissing = false;
        auto roles = optionalJsonObj(r.result, "roles");
        if (!roles.is_object()) roles = nlohmann::json::object();
        s_roles.roles.clear();
        for (int i = 0; i < kRoleCount; ++i) {
            RoleAssign a;
            a.isAuto = true;
            a.thinking = "auto";
            auto it = roles.find(kRoleKeys[i]);
            if (it != roles.end()) {
                if (it->is_string()) {
                    std::string v = it->get<std::string>();
                    a.isAuto = (v == "auto" || v.empty());
                } else if (it->is_object()) {
                    a.provider = optionalJsonString(*it, "provider");
                    a.model = optionalJsonString(*it, "model");
                    // Model auto when provider+model absent (or explicit mode=auto).
                    const std::string mode = optionalJsonString(*it, "mode");
                    a.isAuto = (mode == "auto") || (a.provider.empty() && a.model.empty());
                    // thinking: auto|low|medium|high (default auto).
                    std::string th = optionalJsonString(*it, "thinking");
                    if (th.empty()) th = "auto";
                    if (th != "low" && th != "medium" && th != "high" && th != "auto")
                        th = "auto";
                    a.thinking = th;
                }
            }
            s_roles.roles[kRoleKeys[i]] = std::move(a);
        }
        s_roles.rolesLoaded = true;
    });
}

static void setRoleAssign(AppState& st, const char* roleKey, const RoleAssign& a) {
    if (s_roles.rolesRpcMissing) return;
    s_roles.roles[roleKey] = a;
    nlohmann::json p;
    nlohmann::json roles = nlohmann::json::object();
    // Always object form so thinking can ride alongside model auto/fixed.
    nlohmann::json entry = nlohmann::json::object();
    if (a.isAuto) {
        entry["mode"] = "auto";
    } else {
        entry["provider"] = a.provider;
        entry["model"] = a.model;
    }
    std::string th = a.thinking.empty() ? "auto" : a.thinking;
    if (th != "low" && th != "medium" && th != "high" && th != "auto") th = "auto";
    entry["thinking"] = th;
    roles[roleKey] = std::move(entry);
    p["roles"] = roles;
    s_roles.rolesSaveInFlight = true;
    st.rpc.call("ai_roles_set", p, [](const core::RpcResult& r) {
        s_roles.rolesSaveInFlight = false;
        if (!r.ok) {
            s_roles.lastError = r.err.empty() ? "ai_roles_set failed" : r.err;
            return;
        }
        // Prefer server echo when present.
        if (r.result.is_object() && r.result.contains("roles") && r.result["roles"].is_object()) {
            const auto& roles = r.result["roles"];
            for (int i = 0; i < kRoleCount; ++i) {
                auto it = roles.find(kRoleKeys[i]);
                if (it == roles.end()) continue;
                RoleAssign a;
                a.thinking = "auto";
                if (it->is_string()) {
                    a.isAuto = (it->get<std::string>() == "auto");
                } else if (it->is_object()) {
                    a.provider = optionalJsonString(*it, "provider");
                    a.model = optionalJsonString(*it, "model");
                    const std::string mode = optionalJsonString(*it, "mode");
                    a.isAuto = (mode == "auto") || (a.provider.empty() && a.model.empty());
                    std::string th = optionalJsonString(*it, "thinking");
                    if (th.empty()) th = "auto";
                    if (th != "low" && th != "medium" && th != "high" && th != "auto")
                        th = "auto";
                    a.thinking = th;
                }
                s_roles.roles[kRoleKeys[i]] = a;
            }
        }
        s_roles.lastRolesPoll = ImGui::GetTime();
    });
}

static void drawPassRateBar(float rate, float width, float height) {
    // Local copy of leaderboard bar so category rows don't need the full Board.
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    ImVec2 p = ImGui::GetCursorScreenPos();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float h = height > 0 ? height : 8.0f * sc;
    const float w = width > 0 ? width : ImGui::GetContentRegionAvail().x;
    const float r = std::max(0.f, std::min(1.f, rate));
    ImU32 bg = ui::Palette::toU32(pal.accentSoft);
    ImU32 fg = r >= 0.5f ? ui::Palette::toU32(pal.statusOk)
             : r >= 0.2f ? ui::Palette::toU32(pal.statusWarn)
                         : ui::Palette::toU32(pal.statusErr);
    dl->AddRectFilled(p, ImVec2(p.x + w, p.y + h), bg, 3.0f * sc);
    if (r > 0.001f)
        dl->AddRectFilled(p, ImVec2(p.x + w * r, p.y + h), fg, 3.0f * sc);
    ImGui::Dummy(ImVec2(w, h));
}

static void drawBenchStatusLine(AppState& st, float sc) {
    if (s_bench.running) {
        static const char* kSpin[] = {".  ", ".. ", "...", " ..", "  .", "   "};
        int frame = (int)(ImGui::GetTime() * 6.0) % 6;
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.accent));
        if (!s_bench.activeCategory.empty())
            ImGui::Text("Running %s (%s) %s", s_bench.stage.c_str(),
                        s_bench.activeCategory.c_str(), kSpin[frame]);
        else
            ImGui::Text("Running %s %s", s_bench.stage.c_str(), kSpin[frame]);
        ImGui::PopStyleColor();
    } else if (s_bench.stage == "done") {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusOk));
        ImGui::TextUnformatted("Done \xe2\x80\x94 leaderboard refreshed");
        ImGui::PopStyleColor();
        if (!s_bench.lastResultPath.empty()) {
            ImGui::SameLine();
            dimText(s_bench.lastResultPath.c_str());
        }
    } else if (s_bench.stage == "error" && !s_bench.lastError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
        ImGui::TextWrapped("%s", s_bench.lastError.c_str());
        ImGui::PopStyleColor();
    } else {
        dimText("Idle \xe2\x80\x94 pick a category or legacy smoke/diag run.");
    }
    (void)st; (void)sc;
}

static void drawBenchCategories(AppState& st, float sc) {
    pollBenchCategories(st, /*force=*/false);

    if (!s_cats.loaded && s_cats.pollInFlight) {
        dimText("Loading categories\xe2\x80\xa6");
        return;
    }
    if (s_cats.rpcMissing || (!s_cats.loaded && !st.workerStarted)) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextWrapped(
            "Benchmark categories unavailable \xe2\x80\x94 start the worker to load "
            "category runs. The legacy runner below still works.");
        ImGui::PopStyleColor();
        if (!s_cats.lastError.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
            ImGui::TextWrapped("%s", s_cats.lastError.c_str());
            ImGui::PopStyleColor();
        }
        return;
    }
    if (s_cats.cats.empty()) {
        dimText("No benchmark categories reported yet.");
        return;
    }

    dimText("One row per tool family. Run queues that suite; history comes from the last cycle.");

    if (ImGui::BeginTable("##bench_cats", 4,
                          ImGuiTableFlags_BordersInnerV | ImGuiTableFlags_RowBg |
                              ImGuiTableFlags_SizingStretchProp |
                              ImGuiTableFlags_NoHostExtendY)) {
        ImGui::TableSetupColumn("Category", ImGuiTableColumnFlags_WidthStretch, 2.0f);
        ImGui::TableSetupColumn("Tools", ImGuiTableColumnFlags_WidthFixed, 56.0f * sc);
        ImGui::TableSetupColumn("Last result", ImGuiTableColumnFlags_WidthStretch, 2.4f);
        ImGui::TableSetupColumn("Run", ImGuiTableColumnFlags_WidthFixed, 64.0f * sc);
        ImGui::TableHeadersRow();

        for (const auto& cat : s_cats.cats) {
            ImGui::TableNextRow();
            ImGui::PushID(cat.id.c_str());

            ImGui::TableNextColumn();
            ImGui::TextUnformatted(cat.label.empty() ? cat.id.c_str() : cat.label.c_str());
            if (!cat.description.empty() && ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", cat.description.c_str());

            ImGui::TableNextColumn();
            {
                char buf[32];
                std::snprintf(buf, sizeof(buf), "%d", (int)cat.toolsCovered.size());
                ImGui::TextUnformatted(buf);
                if (ImGui::IsItemHovered() && !cat.toolsCovered.empty()) {
                    ImGui::BeginTooltip();
                    ImGui::TextUnformatted("Tools covered:");
                    for (const auto& t : cat.toolsCovered)
                        ImGui::BulletText("%s", t.c_str());
                    ImGui::EndTooltip();
                }
            }

            ImGui::TableNextColumn();
            if (!cat.hasLast) {
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
                ImGui::TextUnformatted("\xe2\x80\x94 never run");
                ImGui::PopStyleColor();
            } else {
                // Compact single-line cell: bar + % only. Long when/detail paths go in
                // the tooltip — dimText uses TextWrapped and was stacking one glyph per
                // line after SameLine inside a stretch column.
                const float rowH = ImGui::GetTextLineHeight();
                if (cat.last.passRate >= 0.f) {
                    const float pctW = 44.0f * sc;
                    const float gap = 6.0f * sc;
                    const float barW = std::max(48.0f * sc,
                        ImGui::GetContentRegionAvail().x - pctW - gap);
                    // Vertically center the thin bar with the % text.
                    const float y0 = ImGui::GetCursorPosY();
                    ImGui::SetCursorPosY(y0 + std::max(0.f, (rowH - 8.0f * sc) * 0.5f));
                    drawPassRateBar(cat.last.passRate, barW, 8.0f * sc);
                    ImGui::SameLine(0, gap);
                    ImGui::SetCursorPosY(y0);
                    char pct[32];
                    std::snprintf(pct, sizeof(pct), "%.0f%%", cat.last.passRate * 100.f);
                    ImGui::TextUnformatted(pct);
                } else {
                    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.accent));
                    ImGui::TextUnformatted("ran");
                    ImGui::PopStyleColor();
                }
                // Tooltip on the % (or "ran") label — bar Dummy has no hover id.
                if (ImGui::IsItemHovered()) {
                    ImGui::BeginTooltip();
                    if (cat.last.passRate >= 0.f)
                        ImGui::Text("Pass rate: %.0f%%", cat.last.passRate * 100.f);
                    if (!cat.last.when.empty())
                        ImGui::Text("When: %s", cat.last.when.c_str());
                    if (!cat.last.detailPath.empty())
                        ImGui::Text("Detail: %s", cat.last.detailPath.c_str());
                    if (cat.last.when.empty() && cat.last.detailPath.empty() &&
                        cat.last.passRate < 0.f)
                        ImGui::TextUnformatted("Completed (no detail path).");
                    ImGui::EndTooltip();
                }
            }

            ImGui::TableNextColumn();
            {
                const bool canRun = cat.runnable && !s_bench.running && st.workerStarted;
                if (!canRun) ImGui::BeginDisabled();
                if (ImGui::SmallButton("Run")) {
                    nlohmann::json p;
                    p["category"] = cat.id;
                    s_bench.running = true;
                    s_bench.stage = "starting";
                    s_bench.lastError.clear();
                    s_bench.lastPoll = -1e9;
                    s_bench.activeCategory = cat.id;
                    st.rpc.call("bench_run", p, [](const core::RpcResult& r) {
                        if (!r.ok) {
                            s_bench.running = false;
                            s_bench.stage = "error";
                            s_bench.lastError = r.err.empty() ? "bench_run failed" : r.err;
                        } else if (r.result.is_object() && r.result.value("ok", true) == false) {
                            s_bench.running = false;
                            s_bench.stage = "error";
                            s_bench.lastError = r.result.value("reason",
                                std::string("bench_run failed"));
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
                    else if (!cat.runnable)
                        ImGui::SetTooltip("This category is not runnable.");
                    else
                        ImGui::SetTooltip("Run category '%s' via bench_run.", cat.id.c_str());
                }
            }

            ImGui::PopID();
        }
        ImGui::EndTable();
    }

    ImGui::Dummy(ImVec2(0, 4.0f * sc));
    drawBenchStatusLine(st, sc);
}

static void drawBenchRunner(AppState& st, float sc, ui::leaderboard::Board& lb,
                            double& lbLast) {
    ImGui::Dummy(ImVec2(0, 6.0f * sc));
    ImGui::Separator();
    ImGui::Dummy(ImVec2(0, 4.0f * sc));
    ImGui::TextUnformatted("Legacy quick run");
    dimText("Spawns the gym CLI detached on the worker (diag / C1 smoke). Prefer the "
            "category rows above for full tool-family coverage. On finish rebuilds "
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
        s_bench.activeCategory.clear();
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
    ImGui::SameLine();
    drawBenchStatusLine(st, sc);
    if (s_bench.running)
        lbLast = -1e9;
}

static void drawModelRolesSection(AppState& st, float sc) {
    pollModelRoles(st, /*force=*/false);
    ensureDefaultRoles();

    dimText("Per-role model + thinking effort. Auto picks within enabled "
            "providers. Thinking Auto lets the classifier (Default) or dispatcher (subagents) choose. "
            "Redaction is hard-locked to local (loopback) providers.");

    if (s_roles.rolesRpcMissing) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextWrapped("ai_roles_* RPC not available \xe2\x80\x94 showing defaults (read-only).");
        ImGui::PopStyleColor();
    }
    if (!s_roles.lastError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
        ImGui::TextWrapped("%s", s_roles.lastError.c_str());
        ImGui::PopStyleColor();
    }

    // Build picker once per frame (cheap; catalogs are small).
    const auto allPicks = buildRolePickList(st, /*localOnly=*/false);
    const auto localPicks = buildRolePickList(st, /*localOnly=*/true);

    if (ImGui::BeginTable("##model_roles", 3,
                          ImGuiTableFlags_BordersInnerV | ImGuiTableFlags_RowBg |
                              ImGuiTableFlags_SizingStretchProp)) {
        ImGui::TableSetupColumn("Role", ImGuiTableColumnFlags_WidthStretch, 1.0f);
        ImGui::TableSetupColumn("Model", ImGuiTableColumnFlags_WidthStretch, 2.4f);
        ImGui::TableSetupColumn("Thinking", ImGuiTableColumnFlags_WidthFixed, 110.0f * sc);
        ImGui::TableHeadersRow();

        for (int i = 0; i < kRoleCount; ++i) {
            const char* key = kRoleKeys[i];
            const bool isRedaction = (std::strcmp(key, "redaction") == 0);
            RoleAssign& a = s_roles.roles[key];
            const auto& picks = isRedaction ? localPicks : allPicks;

            ImGui::TableNextRow();
            ImGui::PushID(key);

            ImGui::TableNextColumn();
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted(kRoleLabels[i]);
            // Purpose tooltip on every role (vision has the full vision-review description).
            if (std::strcmp(key, "advisor") == 0) {
                helpMarker("Steering / Q&A advisor that guides agents with short notes without doing the work itself.");
            } else if (std::strcmp(key, "task") == 0) {
                helpMarker("Leaf worker/builder that executes CAD ops, edits, and concrete subagent tasks.");
            } else if (std::strcmp(key, "planning") == 0) {
                helpMarker("Orchestrator / plan-mode model that drafts multi-step build plans and coordinates work.");
            } else if (std::strcmp(key, "vision") == 0) {
                helpMarker("Translates renderings/screenshots for the worker models, checks and verifies their "
                           "work/changes, and guides them through the visual modeling steps \xe2\x80\x94 while "
                           "offloading the bulk of vision work to cheaper models (e.g. Grok) to save premium-vision tokens.");
            } else if (std::strcmp(key, "default") == 0) {
                helpMarker("Main copilot agent loop (agent / swarm / ask modes) when no more specific role applies.");
            } else if (std::strcmp(key, "plm") == 0) {
                helpMarker("Product lifecycle tasks \xe2\x80\x94 releases, document control, versioning, and PLM workflows.");
            } else if (std::strcmp(key, "redaction") == 0) {
                helpMarker("Local-only model that redacts sensitive data before anything leaves the machine.");
            } else if (std::strcmp(key, "sketch2d") == 0) {
                helpMarker("2D specialist: sketches, ECAD drawings, and other planar work. "
                           "Does not build 3D solids — model3d delegates sketch jobs here.");
            } else if (std::strcmp(key, "model3d") == 0) {
                helpMarker("3D feature planner/builder. Plans the solid feature tree, delegates "
                           "profiles to sketch2d, places them on the right plane/face, and "
                           "verifies orientation with vision before committing.");
            } else if (std::strcmp(key, "memory") == 0) {
                helpMarker("Project memory distiller — writes durable decisions/notes to "
                           ".plm/memory.db. Does not design geometry.");
            }
            if (isRedaction) {
                ImGui::SameLine(0.0f, 6.0f * sc);
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
                ImGui::TextUnformatted("[local]");
                ImGui::PopStyleColor();
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip(
                        "Local only \xe2\x80\x94 ITAR redaction stage must not leave the machine.\n"
                        "Hard-locked to loopback providers; cloud picks are refused.");
            }

            ImGui::TableNextColumn();
            // Resolve current selection index.
            int sel = 0;  // Auto
            if (!a.isAuto) {
                const int wantProv = roleProviderFromWire(a.provider);
                for (int pi = 1; pi < (int)picks.size(); ++pi) {
                    if (picks[pi].model == a.model &&
                        (wantProv < 0 || picks[pi].provider == wantProv ||
                         roleProviderWire(picks[pi].provider, picks[pi].model) == a.provider)) {
                        sel = pi;
                        break;
                    }
                }
                // Unknown model: inject a synthetic entry so the combo still shows it.
                if (sel == 0 && !a.model.empty()) {
                    // Keep Auto selected visually but show the current value dimly.
                    // Better: force index 0 label via preview — fall through to custom.
                }
            }

            // Preview label.
            std::string preview;
            if (a.isAuto) {
                preview = "Auto";
            } else if (sel > 0) {
                preview = picks[sel].label;
            } else {
                preview = a.provider + " \xc2\xb7 " + a.model;
                if (preview == " \xc2\xb7 ") preview = a.model.empty() ? "Auto" : a.model;
            }

            const bool ro = s_roles.rolesRpcMissing || s_roles.rolesSaveInFlight;
            if (ro) ImGui::BeginDisabled();
            ImGui::SetNextItemWidth(-1);
            if (ImGui::BeginCombo("##role_pick", preview.c_str(), ImGuiComboFlags_HeightLarge)) {
                // Type-to-filter — list is already filtered to keyed/signed-in providers.
                static char s_roleFilt[64] = "";
                if (ImGui::IsWindowAppearing()) {
                    s_roleFilt[0] = 0;
                    ImGui::SetKeyboardFocusHere();
                }
                ImGui::SetNextItemWidth(-1);
                ImGui::InputTextWithHint("##role_filt", "Type to filter...",
                                         s_roleFilt, sizeof(s_roleFilt));
                auto lc = [](std::string s) {
                    for (char& c : s) c = (char)std::tolower((unsigned char)c);
                    return s;
                };
                const std::string fq = lc(s_roleFilt);
                for (int pi = 0; pi < (int)picks.size(); ++pi) {
                    if (!fq.empty()) {
                        const std::string hay = lc(picks[pi].label + " " + picks[pi].model);
                        if (hay.find(fq) == std::string::npos) continue;
                    }
                    const bool selected = (pi == sel) || (pi == 0 && a.isAuto);
                    if (ImGui::Selectable(picks[pi].label.c_str(), selected)) {
                        RoleAssign next = a;  // preserve thinking
                        if (picks[pi].isAuto) {
                            next.isAuto = true;
                            next.provider.clear();
                            next.model.clear();
                        } else {
                            next.isAuto = false;
                            next.provider = roleProviderWire(picks[pi].provider, picks[pi].model);
                            next.model = picks[pi].model;
                        }
                        // Hard-enforce redaction local-only client-side too.
                        if (isRedaction && !next.isAuto &&
                            !providerIsLoopback(picks[pi].provider)) {
                            s_roles.lastError =
                                "Redaction role refuses non-loopback providers.";
                        } else {
                            setRoleAssign(st, key, next);
                        }
                    }
                    if (selected) ImGui::SetItemDefaultFocus();
                }
                ImGui::EndCombo();
            }
            if (ro) ImGui::EndDisabled();

            // Thinking effort combo (Auto / Low / Medium / High).
            ImGui::TableNextColumn();
            static const char* kThinkLabels[] = {"Auto", "Low", "Medium", "High"};
            static const char* kThinkValues[] = {"auto", "low", "medium", "high"};
            int thinkSel = 0;
            for (int ti = 0; ti < 4; ++ti) {
                if (a.thinking == kThinkValues[ti]) { thinkSel = ti; break; }
            }
            if (ro) ImGui::BeginDisabled();
            ImGui::SetNextItemWidth(-1);
            if (ImGui::BeginCombo("##role_think", kThinkLabels[thinkSel])) {
                for (int ti = 0; ti < 4; ++ti) {
                    const bool selT = (ti == thinkSel);
                    if (ImGui::Selectable(kThinkLabels[ti], selT)) {
                        RoleAssign next = a;
                        next.thinking = kThinkValues[ti];
                        setRoleAssign(st, key, next);
                    }
                    if (selT) ImGui::SetItemDefaultFocus();
                }
                ImGui::EndCombo();
            }
            if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)) {
                if (std::strcmp(key, "default") == 0) {
                    ImGui::SetTooltip(
                        "Auto: chat classifier assigns effort per prompt.\n"
                        "Low/Medium/High: force that level for Default-role calls.");
                } else {
                    ImGui::SetTooltip(
                        "Auto: dispatcher assigns effort by task complexity.\n"
                        "Low/Medium/High: force that level for this role's calls.");
                }
            }
            if (ro) ImGui::EndDisabled();

            // Redaction ITAR note lives on the Role [local] badge tooltip — never
            // TextWrapped inside the fixed-width Thinking column (that blew out the row).

            ImGui::PopID();
        }
        ImGui::EndTable();
    }

}

// Copilot mode → default harness structure + one-line description. Single source of
// truth for Settings → AI → Harness organization (index matches CopilotMode).
struct HarnessModeInfo {
    const char* label;       // Agent | Plan | Swarm | Edit | Ask | Eng. Firm
    const char* structure;   // default execution structure
    const char* description; // one-line what it does
};
static const HarnessModeInfo kHarnessModes[] = {
    {"Agent",    "auto",
     "Solo for simple, plan/subagent for standard, DAG/company for complex."},
    {"Plan",     "plan-only",
     "Drafts a plan only; no mutation until you approve."},
    {"Swarm",    "multi-agent parallel",
     "Forces a multi-agent / corporate mission with parallel specialists."},
    {"Edit",     "scoped single-doc edit",
     "Changes the open part (scoped to selection when set)."},
    {"Ask",      "read-only Q&A",
     "Questions about the part; nothing changes."},
    {"Eng. Firm","multi-agent + contractor firms",
     "Specialist roles + contractor firms bid on tasks under a $ budget."},
};
static constexpr int kHarnessModeCount =
    (int)(sizeof(kHarnessModes) / sizeof(kHarnessModes[0]));

// Settings → AI → Harness: mode grid + full quick-settings mirror (session knobs).
static void drawHarnessOrganizationSection(AppState& st, float sc);
static void drawHarnessSessionKnobs(AppState& st, float sc);
static void drawMemoryLearningSection(AppState& st, float sc);

// ---- Eng. Firm model assignments (config.json eng_firm.models) ----------------
// Top-level roles + 8 disciplines. Auto = omit entry (ambient chat model). Merge-overlay
// load/save never clobbers unrelated keys.
struct EngFirmAssign {
    bool isAuto = true;
    std::string provider;
    std::string model;
    std::string thinking = "medium";  // low|medium|high (auto omits thinking key)
};
struct EngFirmModelsUi {
    std::map<std::string, EngFirmAssign> top;   // mission_planner|advisor|lead|subcontractor
    std::map<std::string, EngFirmAssign> disc;  // 8 discipline keys
    bool loaded = false;
};
static EngFirmModelsUi s_efModels;

static const char* kEfTopKeys[] = {
    "mission_planner", "advisor", "lead", "subcontractor"};
static const char* kEfTopLabels[] = {
    "Mission planner", "Advisor", "Lead (prime contractor)", "Subcontractor (default)"};
static constexpr int kEfTopCount = 4;

static const char* kEfDiscKeys[] = {
    "mechanical", "electrical", "structural", "thermal",
    "manufacturing", "drafting", "sourcing", "generalist"};
static const char* kEfDiscLabels[] = {
    "Mechanical", "Electrical", "Structural", "Thermal",
    "Manufacturing", "Drafting", "Sourcing", "Generalist"};
static constexpr int kEfDiscCount = 8;

static EngFirmAssign parseEngFirmAssign(const nlohmann::json& j) {
    EngFirmAssign a;
    if (!j.is_object()) return a;
    a.provider = j.value("provider", std::string());
    a.model = j.value("model", std::string());
    std::string th = j.value("thinking", std::string());
    if (th != "low" && th != "medium" && th != "high") th = "medium";
    a.thinking = th.empty() ? "medium" : th;
    a.isAuto = a.provider.empty() && a.model.empty();
    return a;
}

static void loadEngFirmModelsUi() {
    s_efModels.top.clear();
    s_efModels.disc.clear();
    for (int i = 0; i < kEfTopCount; ++i) s_efModels.top[kEfTopKeys[i]] = EngFirmAssign{};
    for (int i = 0; i < kEfDiscCount; ++i) s_efModels.disc[kEfDiscKeys[i]] = EngFirmAssign{};
    try {
        std::ifstream in(configPath());
        if (in) {
            nlohmann::json j;
            in >> j;
            if (j.is_object() && j.contains("eng_firm") && j["eng_firm"].is_object()) {
                const auto& ef = j["eng_firm"];
                if (ef.contains("models") && ef["models"].is_object()) {
                    const auto& m = ef["models"];
                    for (int i = 0; i < kEfTopCount; ++i) {
                        if (m.contains(kEfTopKeys[i]))
                            s_efModels.top[kEfTopKeys[i]] = parseEngFirmAssign(m[kEfTopKeys[i]]);
                    }
                    if (m.contains("disciplines") && m["disciplines"].is_object()) {
                        const auto& d = m["disciplines"];
                        for (int i = 0; i < kEfDiscCount; ++i) {
                            if (d.contains(kEfDiscKeys[i]))
                                s_efModels.disc[kEfDiscKeys[i]] =
                                    parseEngFirmAssign(d[kEfDiscKeys[i]]);
                        }
                    }
                }
            }
        }
    } catch (...) {}
    s_efModels.loaded = true;
}

static void saveEngFirmModelsUi() {
    try {
        nlohmann::json j = nlohmann::json::object();
        {
            std::ifstream in(configPath());
            if (in) {
                nlohmann::json old;
                in >> old;
                if (old.is_object()) j = std::move(old);
            }
        }
        if (!j.contains("eng_firm") || !j["eng_firm"].is_object())
            j["eng_firm"] = nlohmann::json::object();
        nlohmann::json models = nlohmann::json::object();
        // Preserve unknown sibling keys under eng_firm.models (and disciplines).
        if (j["eng_firm"].contains("models") && j["eng_firm"]["models"].is_object())
            models = j["eng_firm"]["models"];

        auto writeEntry = [](nlohmann::json& parent, const char* key, const EngFirmAssign& a) {
            if (a.isAuto || a.model.empty()) {
                parent.erase(key);
                return;
            }
            nlohmann::json e = nlohmann::json::object();
            e["provider"] = a.provider;
            e["model"] = a.model;
            if (a.thinking == "low" || a.thinking == "medium" || a.thinking == "high")
                e["thinking"] = a.thinking;
            parent[key] = std::move(e);
        };

        for (int i = 0; i < kEfTopCount; ++i)
            writeEntry(models, kEfTopKeys[i], s_efModels.top[kEfTopKeys[i]]);

        nlohmann::json discs = nlohmann::json::object();
        if (models.contains("disciplines") && models["disciplines"].is_object())
            discs = models["disciplines"];
        for (int i = 0; i < kEfDiscCount; ++i)
            writeEntry(discs, kEfDiscKeys[i], s_efModels.disc[kEfDiscKeys[i]]);
        if (discs.empty())
            models.erase("disciplines");
        else
            models["disciplines"] = std::move(discs);

        if (models.empty())
            j["eng_firm"].erase("models");
        else
            j["eng_firm"]["models"] = std::move(models);
        if (j["eng_firm"].empty())
            j.erase("eng_firm");

        const std::string path = configPath();
        const std::string tmp = path + ".tmp";
        std::ofstream(tmp) << j.dump(2);
        std::rename(tmp.c_str(), path.c_str());
    } catch (...) {}
}

static void drawEngFirmAssignRow(AppState& st, const char* id, const char* label,
                                 EngFirmAssign& a, const std::vector<RolePickEntry>& picks,
                                 float sc) {
    (void)sc;
    ImGui::TableNextRow();
    ImGui::PushID(id);

    ImGui::TableNextColumn();
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(label);

    ImGui::TableNextColumn();
    int sel = 0;
    if (!a.isAuto) {
        const int wantProv = roleProviderFromWire(a.provider);
        for (int pi = 1; pi < (int)picks.size(); ++pi) {
            if (picks[pi].model == a.model &&
                (wantProv < 0 || picks[pi].provider == wantProv ||
                 roleProviderWire(picks[pi].provider, picks[pi].model) == a.provider)) {
                sel = pi;
                break;
            }
        }
    }
    std::string preview;
    if (a.isAuto) {
        preview = "Auto";
    } else if (sel > 0) {
        preview = picks[sel].label;
    } else {
        preview = a.provider + " \xc2\xb7 " + a.model;
        if (preview == " \xc2\xb7 ") preview = a.model.empty() ? "Auto" : a.model;
    }
    ImGui::SetNextItemWidth(-1);
    if (ImGui::BeginCombo("##ef_pick", preview.c_str(), ImGuiComboFlags_HeightLarge)) {
        static char s_efFilt[64] = "";
        if (ImGui::IsWindowAppearing()) {
            s_efFilt[0] = 0;
            ImGui::SetKeyboardFocusHere();
        }
        ImGui::SetNextItemWidth(-1);
        ImGui::InputTextWithHint("##ef_filt", "Type to filter...",
                                 s_efFilt, sizeof(s_efFilt));
        auto lc = [](std::string s) {
            for (char& c : s) c = (char)std::tolower((unsigned char)c);
            return s;
        };
        const std::string fq = lc(s_efFilt);
        for (int pi = 0; pi < (int)picks.size(); ++pi) {
            if (!fq.empty()) {
                const std::string hay = lc(picks[pi].label + " " + picks[pi].model);
                if (hay.find(fq) == std::string::npos) continue;
            }
            const bool selected = (pi == sel) || (pi == 0 && a.isAuto);
            if (ImGui::Selectable(picks[pi].label.c_str(), selected)) {
                if (picks[pi].isAuto) {
                    a.isAuto = true;
                    a.provider.clear();
                    a.model.clear();
                } else {
                    a.isAuto = false;
                    a.provider = roleProviderWire(picks[pi].provider, picks[pi].model);
                    a.model = picks[pi].model;
                    if (a.thinking != "low" && a.thinking != "medium" && a.thinking != "high")
                        a.thinking = "medium";
                }
                saveEngFirmModelsUi();
            }
            if (selected) ImGui::SetItemDefaultFocus();
        }
        ImGui::EndCombo();
    }

    ImGui::TableNextColumn();
    static const char* kThinkLabels[] = {"Low", "Medium", "High"};
    static const char* kThinkValues[] = {"low", "medium", "high"};
    int thinkSel = 1;  // medium default
    for (int ti = 0; ti < 3; ++ti) {
        if (a.thinking == kThinkValues[ti]) { thinkSel = ti; break; }
    }
    if (a.isAuto) ImGui::BeginDisabled();
    ImGui::SetNextItemWidth(-1);
    if (ImGui::BeginCombo("##ef_think", kThinkLabels[thinkSel])) {
        for (int ti = 0; ti < 3; ++ti) {
            const bool selT = (ti == thinkSel);
            if (ImGui::Selectable(kThinkLabels[ti], selT)) {
                a.thinking = kThinkValues[ti];
                saveEngFirmModelsUi();
            }
            if (selT) ImGui::SetItemDefaultFocus();
        }
        ImGui::EndCombo();
    }
    if (a.isAuto) ImGui::EndDisabled();

    ImGui::PopID();
}

static void drawEngFirmModelAssignments(AppState& st, float sc) {
    if (!s_efModels.loaded) loadEngFirmModelsUi();

    dimText("Empty rows inherit the active chat model. Eng. Firm runs resolve: discipline -> "
            "lead/subcontractor -> chat model.");

    const auto picks = buildRolePickList(st, /*localOnly=*/false);

    if (ImGui::BeginTable("##ef_models", 3,
                          ImGuiTableFlags_BordersInnerV | ImGuiTableFlags_RowBg |
                              ImGuiTableFlags_SizingStretchProp)) {
        ImGui::TableSetupColumn("Role", ImGuiTableColumnFlags_WidthStretch, 1.2f);
        ImGui::TableSetupColumn("Model", ImGuiTableColumnFlags_WidthStretch, 2.4f);
        ImGui::TableSetupColumn("Thinking", ImGuiTableColumnFlags_WidthFixed, 110.0f * sc);
        ImGui::TableHeadersRow();

        for (int i = 0; i < kEfTopCount; ++i)
            drawEngFirmAssignRow(st, kEfTopKeys[i], kEfTopLabels[i],
                                s_efModels.top[kEfTopKeys[i]], picks, sc);

        // Lightweight separator label row for disciplines.
        ImGui::TableNextRow();
        ImGui::TableNextColumn();
        ImGui::AlignTextToFramePadding();
        dimText("Disciplines");
        ImGui::TableNextColumn();
        ImGui::TableNextColumn();

        for (int i = 0; i < kEfDiscCount; ++i)
            drawEngFirmAssignRow(st, kEfDiscKeys[i], kEfDiscLabels[i],
                                s_efModels.disc[kEfDiscKeys[i]], picks, sc);

        ImGui::EndTable();
    }
}

static void drawHarnessOrganizationSection(AppState& st, float sc) {
    // Two-column mode chips: active mode accented, others secondary. Saves vertical
    // space vs the old 6-line stack (screenshot ask).
    dimText("Composer mode picks the org structure. Switch mode from the copilot composer.");
    int active = st.settings.copilotMode;
    if (active < 0 || active >= kHarnessModeCount) active = 0;

    ImGui::Dummy(ImVec2(0, 4.0f * sc));
    if (ImGui::BeginTable("##harness_modes", 2,
                          ImGuiTableFlags_SizingStretchSame | ImGuiTableFlags_NoPadOuterX)) {
        for (int i = 0; i < kHarnessModeCount; ++i) {
            const auto& m = kHarnessModes[i];
            ImGui::TableNextColumn();
            char line[192];
            std::snprintf(line, sizeof(line), "%s  \xc2\xb7  %s%s",
                          m.label, m.structure, (i == active) ? "  (active)" : "");
            if (i == active) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(ui::palette.accent[0], ui::palette.accent[1],
                           ui::palette.accent[2], 1.0f));
                ImGui::TextUnformatted(line);
                ImGui::PopStyleColor();
            } else {
                ImGui::PushStyleColor(ImGuiCol_Text, ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled));
                ImGui::TextUnformatted(line);
                ImGui::PopStyleColor();
            }
            if (ImGui::IsItemHovered() && m.description && m.description[0])
                ImGui::SetTooltip("%s", m.description);
        }
        ImGui::EndTable();
    }
}

// Settings → AI → Harness: full mirror of copilot Quick settings + extras that
// only live in the modal (role adoption, company auto-route, Eng. Firm budget,
// privacy). Laid out in a 2-column grid to save vertical space.
static void drawHarnessSessionKnobs(AppState& st, float sc) {
    Settings& settings = st.settings;
    static const char* kPrefLabels[] = {"Off", "Option", "Encourage", "Require"};

    auto prefCombo = [&](const char* label, const char* id, int* pref, const char* help) {
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted(label);
        if (help && help[0]) {
            ImGui::SameLine();
            helpMarker(help);
        }
        ImGui::SetNextItemWidth(-1.0f);
        int v = (*pref < 0 || *pref > 3) ? 0 : *pref;
        if (ImGui::Combo(id, &v, kPrefLabels, 4)) {
            *pref = v;
            if (pref == &settings.planPreference)
                settings.planMode = (v != 0);
        }
    };

    // ---- Preferences (3 combos) + flags in a 2-col grid ----
    if (ImGui::BeginTable("##harness_prefs", 2,
                          ImGuiTableFlags_SizingStretchSame |
                          ImGuiTableFlags_NoPadOuterX)) {
        ImGui::TableNextColumn();
        prefCombo("Plan preference", "##h_plan_pref", &settings.planPreference,
                  "Off = never plan first. Option = model may plan. "
                  "Encourage = plan by default (recommended). "
                  "Require = always draft a plan before building.");
        ImGui::TableNextColumn();
        prefCombo("Subagent preference", "##h_sub_pref", &settings.subagentPreference,
                  "How freely the main agent may spawn subagents. "
                  "Off disables nesting; Require pushes multi-agent splits.");
        ImGui::TableNextColumn();
        prefCombo("Advisor preference", "##h_adv_pref", &settings.advisorPreference,
                  "Off = no advisor. Option = only when the model asks. "
                  "Encourage = stronger guidance tone when proactive fires. "
                  "Require = more frequent checks when proactive is on.");
        ImGui::TableNextColumn();
        {
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted("Role adoption");
            ImGui::SameLine();
            helpMarker(
                "Supervised (default): only the main session may adopt_role "
                "(switch tools+prompt). Subagents may still delegate() children "
                "with other roles, but must request_role_change to adopt themselves. "
                "Free: subagents may adopt_role too.");
            ImGui::SetNextItemWidth(-1.0f);
            int ra = (settings.roleAdoption != 0) ? 1 : 0;
            const char* kAdopt[] = {"Supervised", "Free"};
            if (ImGui::Combo("##h_role_adopt", &ra, kAdopt, 2))
                settings.roleAdoption = ra;
        }
        ImGui::EndTable();
    }

    ImGui::Dummy(ImVec2(0, 6.0f * sc));

    // ---- Checkboxes in 2 columns (mirrors quick-settings flags) ----
    if (ImGui::BeginTable("##harness_flags", 2,
                          ImGuiTableFlags_SizingStretchSame |
                          ImGuiTableFlags_NoPadOuterX)) {
        ImGui::TableNextColumn();
        ImGui::Checkbox("Proactive advisor", &settings.proactiveAdvisor);
        helpMarker("Opt-in mid-run guidance on failure streaks and thrash. "
                   "Cheap deterministic notes by default; at most one short "
                   "advisor-model call on severe streaks (encourage/require). "
                   "Off = no proactive injects (pull request_advice still works).");
        ImGui::TableNextColumn();
        ImGui::Checkbox("Company structure auto-route", &settings.corporateMode);
        helpMarker("When on, complex cross-discipline requests may route as "
                   "company (director + departments). Default off — Agent will "
                   "not auto-invoke company; use Swarm / Eng. Firm modes or "
                   "turn this on when ready.");

        ImGui::TableNextColumn();
        ImGui::Checkbox("Vision (send screenshots)", &settings.vision);
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Attach viewport screenshots so the model can see the part.");
        ImGui::TableNextColumn();
        ImGui::Checkbox("Offline mode (local only)", &settings.offlineMode);
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip(
                "Refuses any cloud provider. Only a local loopback endpoint is allowed.");

        ImGui::TableNextColumn();
        ImGui::Checkbox("Show assistant thoughts", &ui::assistantThoughtsVisible());
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Show the AI's reasoning steps inline in the conversation.");
        ImGui::TableNextColumn();
        ImGui::Checkbox("Project memory (per-folder)", &settings.memoryEnabled);
        helpMarker("Remembers decisions and notes under the PLM project "
                   "(.plm/memory.db). Auto-created; recall is token-budgeted.");
        ImGui::EndTable();
    }

    ImGui::Dummy(ImVec2(0, 6.0f * sc));

    // ---- Sliders / budgets in 2 columns ----
    if (ImGui::BeginTable("##harness_sliders", 2,
                          ImGuiTableFlags_SizingStretchSame |
                          ImGuiTableFlags_NoPadOuterX)) {
        ImGui::TableNextColumn();
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted("Refine-pass cap");
        ImGui::SameLine();
        helpMarker("0 = automatic (stops once the render matches).");
        ImGui::SetNextItemWidth(-1.0f);
        ImGui::SliderInt("##h_miters", &settings.maxIters, 0, 64,
                         settings.maxIters == 0 ? "automatic" : "%d passes");

        ImGui::TableNextColumn();
        {
            static int s_maxDepth = -1;
            if (s_maxDepth < 0) s_maxDepth = ui::loadMaxSubagentDepth();
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted("Max subagent nesting");
            ImGui::SameLine();
            helpMarker("Nesting levels of subagent-spawning-subagent (not concurrency). "
                       "Root chat = depth 0. Saved to config.json agent.max_subagent_depth.");
            ImGui::SetNextItemWidth(-1.0f);
            int depth = s_maxDepth;
            if (ImGui::SliderInt("##h_depth", &depth, 0, 4, "%d")) {
                if (depth < 0) depth = 0;
                if (depth > 4) depth = 4;
                s_maxDepth = depth;
                ui::saveMaxSubagentDepth(depth);
            }
        }

        ImGui::TableNextColumn();
        if (settings.memoryEnabled) {
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted("Memory token budget");
            ImGui::SetNextItemWidth(-1.0f);
            ImGui::SliderInt("##h_membudget", &settings.memoryTokenBudget, 100, 2000,
                             "%d tokens");
        } else {
            ImGui::Dummy(ImVec2(1, ImGui::GetFrameHeight()));
        }

        ImGui::TableNextColumn();
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted("Eng. Firm budget (USD)");
        ImGui::SameLine();
        helpMarker("Used when the composer is in Eng. Firm mode.");
        ImGui::SetNextItemWidth(-1.0f);
        double b = settings.companyBudgetUsd;
        if (ImGui::InputDouble("##h_engfirmbudget", &b, 5.0, 25.0, "$%.2f")) {
            if (b < 0.0) b = 0.0;
            settings.companyBudgetUsd = b;
        }
        ImGui::EndTable();
    }
}

// ---- agent.* memory/learning knobs (config.json) -----------------------------
// Same load-modify-write recipe as ui::loadMaxSubagentDepth/saveMaxSubagentDepth
// (copilot/settings.cpp): read with a default, save via atomic .tmp rename.
static std::string loadAgentConfigString(const char* key, const char* fallback) {
    std::string v = fallback;
    try {
        std::ifstream in(configPath());
        if (in) {
            nlohmann::json j;
            in >> j;
            if (j.is_object()) {
                auto agent = j.value("agent", nlohmann::json::object());
                if (agent.is_object() && agent.contains(key) && agent[key].is_string())
                    v = agent[key].get<std::string>();
            }
        }
    } catch (...) {}
    return v;
}
static bool loadAgentConfigBool(const char* key, bool fallback) {
    bool v = fallback;
    try {
        std::ifstream in(configPath());
        if (in) {
            nlohmann::json j;
            in >> j;
            if (j.is_object()) {
                auto agent = j.value("agent", nlohmann::json::object());
                if (agent.is_object() && agent.contains(key) && agent[key].is_boolean())
                    v = agent[key].get<bool>();
            }
        }
    } catch (...) {}
    return v;
}
static void saveAgentConfigValue(const char* key, const nlohmann::json& value) {
    try {
        nlohmann::json j = nlohmann::json::object();
        {
            std::ifstream in(configPath());
            if (in) {
                nlohmann::json old;
                in >> old;
                if (old.is_object()) j = std::move(old);
            }
        }
        if (!j.contains("agent") || !j["agent"].is_object())
            j["agent"] = nlohmann::json::object();
        j["agent"][key] = value;
        const std::string path = configPath();
        const std::string tmp = path + ".tmp";
        std::ofstream(tmp) << j.dump(2);
        std::rename(tmp.c_str(), path.c_str());
    } catch (...) {}
}

// Memory & Learning + Workspace visibility knobs (Settings > AI > Harness).
// 2-col grid like the session knobs above. Backend/learning/cache knobs persist to
// config.json agent.* via the recipe above; visibility knobs persist via ui/ai/ai_prefs.
static void drawMemoryLearningSection(AppState& st, float sc) {
    Settings& settings = st.settings;

    static bool s_loaded = false;
    static int s_memBackend = 0;      // 0 sqlite | 1 mnemopi  (agent.memory_backend)
    static int s_compBackend = 0;     // 0 builtin | 1 omp     (agent.compaction_backend)
    static bool s_learn = true;       // agent.memory_learn
    static int s_verbosity = 2;       // ui::aiUiVerbosity (0 quiet | 1 normal | 2 full)
    static bool s_follow = true;      // ui::aiFollowActiveSession
    static bool s_previews = true;    // ui::aiActiveWorkPreviews
    static bool s_cacheWarm = false;  // agent.cache_warming
    if (!s_loaded) {
        s_loaded = true;
        s_memBackend = (loadAgentConfigString("memory_backend", "sqlite") == "mnemopi") ? 1 : 0;
        s_compBackend = (loadAgentConfigString("compaction_backend", "builtin") == "omp") ? 1 : 0;
        s_learn = loadAgentConfigBool("memory_learn", true);
        s_verbosity = ui::aiUiVerbosity();
        s_follow = ui::aiFollowActiveSession();
        s_previews = ui::aiActiveWorkPreviews();
        s_cacheWarm = loadAgentConfigBool("cache_warming", false);
    }

    static const char* kMemBackends[] = {"SQLite", "Mnemopi"};
    static const char* kCompBackends[] = {"Builtin", "OMP snapcompact"};
    static const char* kVerbosity[] = {"Quiet", "Normal", "Full"};

    if (ImGui::BeginTable("##memory_learning", 2,
                          ImGuiTableFlags_SizingStretchSame |
                          ImGuiTableFlags_NoPadOuterX)) {
        ImGui::TableNextColumn();
        {
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted("Memory backend");
            ImGui::SameLine();
            helpMarker("Where project memory (decisions, notes, recall) is stored. "
                       "SQLite = local .plm/memory.db (default). Mnemopi = OMP bridge "
                       "memory. Saved to config.json agent.memory_backend.");
            ImGui::SetNextItemWidth(-1.0f);
            if (ImGui::Combo("##mem_backend", &s_memBackend, kMemBackends, 2))
                saveAgentConfigValue("memory_backend", s_memBackend == 1 ? "mnemopi" : "sqlite");
        }
        ImGui::TableNextColumn();
        {
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted("Compaction backend");
            ImGui::SameLine();
            helpMarker("How long transcripts are compacted. Builtin = pure-Python "
                       "snapcompact archive (default). OMP snapcompact = OMP bridge "
                       "compact engine. Saved to config.json agent.compaction_backend.");
            ImGui::SetNextItemWidth(-1.0f);
            if (ImGui::Combo("##comp_backend", &s_compBackend, kCompBackends, 2))
                saveAgentConfigValue("compaction_backend", s_compBackend == 1 ? "omp" : "builtin");
        }

        ImGui::TableNextColumn();
        ImGui::Checkbox("Project memory (per-folder)", &settings.memoryEnabled);
        helpMarker("Remembers decisions and notes under the PLM project "
                   "(.plm/memory.db). Auto-created; recall is token-budgeted.");
        ImGui::TableNextColumn();
        if (ImGui::Checkbox("Remember decisions & notes", &s_learn))
            saveAgentConfigValue("memory_learn", s_learn);
        helpMarker("Learning switch: when on, the agent writes decisions and notes it "
                   "judges worth keeping into project memory (agent.memory_learn, "
                   "default on). Recall still works when off; nothing new is learned.");

        ImGui::TableNextColumn();
        if (settings.memoryEnabled) {
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted("Memory token budget");
            ImGui::SetNextItemWidth(-1.0f);
            ImGui::SliderInt("##mem_budget_ml", &settings.memoryTokenBudget, 100, 2000,
                             "%d tokens");
        } else {
            ImGui::Dummy(ImVec2(1, ImGui::GetFrameHeight()));
        }
        ImGui::TableNextColumn();
        ImGui::Dummy(ImVec2(1, ImGui::GetFrameHeight()));
        ImGui::EndTable();
    }

    // ---- Workspace visibility: how much the AI command-center views show ----
    settingsSectionHeader("Workspace visibility");
    if (ImGui::BeginTable("##workspace_vis", 2,
                          ImGuiTableFlags_SizingStretchSame |
                          ImGuiTableFlags_NoPadOuterX)) {
        ImGui::TableNextColumn();
        {
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted("Verbosity");
            ImGui::SameLine();
            helpMarker("How much the Agent Workflow / Active Work / Session Plan views "
                       "render. Quiet = titles + status. Normal = last op. "
                       "Full = op tickers, counts, token/latency detail (default).");
            ImGui::SetNextItemWidth(-1.0f);
            if (ImGui::Combo("##ws_verbosity", &s_verbosity, kVerbosity, 3))
                ui::setAiUiVerbosity(s_verbosity);
        }
        ImGui::TableNextColumn();
        if (ImGui::Checkbox("Autopopulate (follow active session)", &s_follow))
            ui::setAiFollowActiveSession(s_follow);
        helpMarker("Views follow the ACTIVE chat session / latest run automatically "
                   "instead of manual picking.");

        ImGui::TableNextColumn();
        if (ImGui::Checkbox("Active-Work previews", &s_previews))
            ui::setAiActiveWorkPreviews(s_previews);
        helpMarker("Render iso preview thumbnails for agent-touched documents in the "
                   "Active Work view. Off = lightweight placeholder only.");
        ImGui::TableNextColumn();
        if (ImGui::Checkbox("Provider cache-warming", &s_cacheWarm))
            saveAgentConfigValue("cache_warming", s_cacheWarm);
        ImGui::EndTable();
    }
    dimText("Cache-warming keeps provider prompt caches warm between runs "
            "(agent.cache_warming, default off).");
}

}  // anonymous namespace

namespace {

// ---- Skills editor (Settings → Prompts → Skills; ported from dead overlay) ----
struct OvSkillRow {
    std::string name;
    std::string description;
    std::string source;  // builtin | user | project
    std::string inject;
};
struct OvSkillsUi {
    std::vector<OvSkillRow> skills;
    bool loaded = false;
    bool rpcMissing = false;
    bool listInFlight = false;
    bool bodyInFlight = false;
    bool saveInFlight = false;
    double lastList = -1e9;
    std::string lastError;
    std::string selected;
    std::string bodyName;          // name the editor buffer belongs to
    std::vector<char> body{std::vector<char>(65536, 0)};
    bool bodyDirty = false;
    bool creating = false;
    char newName[96] = {};
};
static OvSkillsUi s_ovSkills;

static const char* kSkillCreatorTemplate =
    "---\n"
    "name: my-skill\n"
    "description: One line describing what this skill teaches.\n"
    "inject: manual\n"
    "triggers:\n"
    "  keywords: []\n"
    "  tools: []\n"
    "  phases: []\n"
    "---\n"
    "\n"
    "# My Skill\n"
    "\n"
    "Teach a tool sequence. Always verify-before-done.\n"
    "\n"
    "## Steps\n"
    "\n"
    "1. ...\n"
    "2. ...\n"
    "\n"
    "## Checklist\n"
    "\n"
    "- [ ] name + description valid\n"
    "- [ ] verify-before-done called out\n";

static bool ovSkillsAbsent(const core::RpcResult& r) {
    if (!r.ok) {
        const std::string& e = r.err;
        if (e.find("unknown") != std::string::npos || e.find("not found") != std::string::npos ||
            e.find("not_found") != std::string::npos || e.find("method") != std::string::npos)
            return true;
        return false;
    }
    if (r.result.is_object()) {
        const std::string c = r.result.value("code", std::string());
        if (c == "unknown_method" || c == "method_not_found" || c == "not_implemented")
            return true;
        if (!r.result.value("ok", true)) {
            const std::string reason = r.result.value("reason", std::string());
            if (reason == "unknown_method" || reason == "method_not_found")
                return true;
        }
    }
    return false;
}
static void ovPollSkills(AppState& st, bool force) {
    if (s_ovSkills.rpcMissing) return;
    const double now = ImGui::GetTime();
    if (!force && s_ovSkills.loaded && (now - s_ovSkills.lastList) < 4.0) return;
    if (s_ovSkills.listInFlight) return;
    s_ovSkills.lastList = now;
    s_ovSkills.listInFlight = true;
    st.rpc.call("skills_list", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_ovSkills.listInFlight = false;
        if (ovSkillsAbsent(r)) {
            s_ovSkills.rpcMissing = true;
            s_ovSkills.loaded = true;
            s_ovSkills.skills.clear();
            return;
        }
        if (!r.ok || !r.result.is_object()) {
            s_ovSkills.lastError = r.err.empty() ? "skills_list failed" : r.err;
            s_ovSkills.loaded = true;
            return;
        }
        s_ovSkills.lastError.clear();
        s_ovSkills.skills.clear();
        auto arr = r.result.value("skills", nlohmann::json::array());
        if (arr.is_array()) {
            for (const auto& e : arr) {
                if (!e.is_object()) continue;
                OvSkillRow row;
                row.name = e.value("name", std::string());
                row.description = e.value("description", std::string());
                row.source = e.value("source", std::string("builtin"));
                row.inject = e.value("inject", std::string("manual"));
                if (!row.name.empty()) s_ovSkills.skills.push_back(std::move(row));
            }
        }
        std::sort(s_ovSkills.skills.begin(), s_ovSkills.skills.end(),
                  [](const OvSkillRow& a, const OvSkillRow& b) {
                      return a.name < b.name;
                  });
        s_ovSkills.loaded = true;
    });
}
static void ovLoadSkillBody(AppState& st, const std::string& name) {
    if (name.empty() || s_ovSkills.bodyInFlight) return;
    s_ovSkills.bodyInFlight = true;
    s_ovSkills.bodyName = name;
    s_ovSkills.bodyDirty = false;
    std::fill(s_ovSkills.body.begin(), s_ovSkills.body.end(), 0);
    st.rpc.call("skills_get", nlohmann::json{{"name", name}},
                [name](const core::RpcResult& r) {
        s_ovSkills.bodyInFlight = false;
        if (ovSkillsAbsent(r)) {
            s_ovSkills.rpcMissing = true;
            return;
        }
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", true)) {
            s_ovSkills.lastError = r.err.empty()
                ? r.result.value("reason", std::string("skills_get failed"))
                : r.err;
            return;
        }
        if (s_ovSkills.selected != name) return;  // selection moved on
        std::string body = r.result.value("body", std::string());
        std::fill(s_ovSkills.body.begin(), s_ovSkills.body.end(), 0);
        if (body.size() >= s_ovSkills.body.size())
            body.resize(s_ovSkills.body.size() - 1);
        std::memcpy(s_ovSkills.body.data(), body.data(), body.size());
        s_ovSkills.bodyName = name;
        s_ovSkills.bodyDirty = false;
        s_ovSkills.lastError.clear();
    });
}
static void ovSaveSkill(AppState& st) {
    if (s_ovSkills.selected.empty() || s_ovSkills.saveInFlight) return;
    s_ovSkills.saveInFlight = true;
    nlohmann::json p;
    p["name"] = s_ovSkills.selected;
    p["body"] = std::string(s_ovSkills.body.data());
    st.rpc.call("skills_save", p, [](const core::RpcResult& r) {
        s_ovSkills.saveInFlight = false;
        if (ovSkillsAbsent(r)) {
            s_ovSkills.rpcMissing = true;
            return;
        }
        if (!r.ok || (r.result.is_object() && !r.result.value("ok", true))) {
            s_ovSkills.lastError = r.err.empty()
                ? r.result.value("reason", std::string("skills_save failed"))
                : r.err;
            return;
        }
        s_ovSkills.bodyDirty = false;
        s_ovSkills.lastError.clear();
        s_ovSkills.loaded = false;  // refresh list (source may flip to user)
        s_ovSkills.lastList = -1e9;
    });
}
static void ovDeleteSkill(AppState& st, const std::string& name) {
    if (name.empty()) return;
    st.rpc.call("skills_delete", nlohmann::json{{"name", name}},
                [name](const core::RpcResult& r) {
        if (!r.ok || (r.result.is_object() && !r.result.value("ok", true))) {
            s_ovSkills.lastError = r.err.empty()
                ? r.result.value("reason", std::string("skills_delete failed"))
                : r.err;
            return;
        }
        if (s_ovSkills.selected == name) {
            s_ovSkills.selected.clear();
            s_ovSkills.bodyName.clear();
            std::fill(s_ovSkills.body.begin(), s_ovSkills.body.end(), 0);
            s_ovSkills.bodyDirty = false;
        }
        s_ovSkills.loaded = false;
        s_ovSkills.lastList = -1e9;
    });
}

// ---- Context injection triggers (Settings → Prompts → Context) ----
struct OvTriggerRow {
    std::string id;
    std::string purpose;
    std::string source;   // builtin | override | user
    std::string pack;
    std::string exemplar;
    bool enabled = true;
    int budgetTokens = 250;
    int cooldownTurns = 8;
    std::vector<std::string> keywords;
    std::vector<std::string> tools;
    std::vector<std::string> events;
};
struct OvTriggersUi {
    std::vector<OvTriggerRow> rows;
    bool loaded = false;
    bool rpcMissing = false;
    bool listInFlight = false;
    bool bodyInFlight = false;
    bool saveInFlight = false;
    double lastList = -1e9;
    std::string lastError;
    std::string selected;
    // Editor buffers for the selected trigger.
    bool enabled = true;
    int budgetTokens = 250;
    int cooldownTurns = 8;
    char purpose[512] = {};
    char packName[96] = {};
    char keywords[2048] = {};   // one regex per line
    char tools[1024] = {};      // comma or newline separated
    char events[1024] = {};
    char exemplar[512] = {};
    std::vector<char> packBody{std::vector<char>(65536, 0)};
    bool bodyDirty = false;   // any editor field dirty
    bool packDirty = false;   // pack body specifically edited (avoid freezing shipped packs)
    nlohmann::json prerunCalcs = nlohmann::json::array();  // preserved from get/list
    bool creating = false;
    char newId[96] = {};
};
static OvTriggersUi s_ovTrig;

static bool ovTriggersAbsent(const core::RpcResult& r) {
    if (!r.ok) {
        const std::string& e = r.err;
        if (e.find("unknown") != std::string::npos || e.find("not found") != std::string::npos ||
            e.find("not_found") != std::string::npos || e.find("method") != std::string::npos)
            return true;
        return false;
    }
    if (r.result.is_object()) {
        const std::string c = r.result.value("code", std::string());
        if (c == "unknown_method" || c == "method_not_found" || c == "not_implemented")
            return true;
        if (!r.result.value("ok", true)) {
            const std::string reason = r.result.value("reason", std::string());
            if (reason == "unknown_method" || reason == "method_not_found")
                return true;
        }
    }
    return false;
}
static std::vector<std::string> ovSplitLinesOrCommas(const char* buf) {
    std::vector<std::string> out;
    if (!buf) return out;
    std::string cur;
    for (const char* p = buf; ; ++p) {
        const char c = *p;
        if (c == '\n' || c == '\r' || c == ',' || c == 0) {
            size_t a = 0, b = cur.size();
            while (a < b && std::isspace((unsigned char)cur[a])) ++a;
            while (b > a && std::isspace((unsigned char)cur[b - 1])) --b;
            if (b > a) out.emplace_back(cur.substr(a, b - a));
            cur.clear();
            if (c == 0) break;
            continue;
        }
        cur.push_back(c);
    }
    return out;
}
static void ovJoinLines(const std::vector<std::string>& xs, char* buf, size_t n) {
    if (!buf || n == 0) return;
    buf[0] = 0;
    std::string s;
    for (size_t i = 0; i < xs.size(); ++i) {
        if (i) s.push_back('\n');
        s += xs[i];
    }
    if (s.size() >= n) s.resize(n - 1);
    std::memcpy(buf, s.data(), s.size());
    buf[s.size()] = 0;
}
static void ovPollTriggers(AppState& st, bool force) {
    if (s_ovTrig.rpcMissing) return;
    const double now = ImGui::GetTime();
    if (!force && s_ovTrig.loaded && (now - s_ovTrig.lastList) < 4.0) return;
    if (s_ovTrig.listInFlight) return;
    s_ovTrig.lastList = now;
    s_ovTrig.listInFlight = true;
    st.rpc.call("triggers_list", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_ovTrig.listInFlight = false;
        if (ovTriggersAbsent(r)) {
            s_ovTrig.rpcMissing = true;
            s_ovTrig.loaded = true;
            s_ovTrig.rows.clear();
            return;
        }
        if (!r.ok || !r.result.is_object()) {
            s_ovTrig.lastError = r.err.empty() ? "triggers_list failed" : r.err;
            s_ovTrig.loaded = true;
            return;
        }
        s_ovTrig.lastError.clear();
        s_ovTrig.rows.clear();
        auto arr = r.result.value("triggers", nlohmann::json::array());
        if (arr.is_array()) {
            for (const auto& e : arr) {
                if (!e.is_object()) continue;
                OvTriggerRow row;
                row.id = e.value("id", std::string());
                row.purpose = e.value("purpose", std::string());
                row.source = e.value("source", std::string("builtin"));
                row.enabled = e.value("enabled", true);
                row.budgetTokens = e.value("budget_tokens", 250);
                row.cooldownTurns = e.value("cooldown_turns", 8);
                auto inject = e.value("inject", nlohmann::json::object());
                if (inject.is_object()) {
                    row.pack = inject.value("context_pack", std::string());
                    row.exemplar = inject.value("exemplar", std::string());
                }
                auto match = e.value("match", nlohmann::json::object());
                if (match.is_object()) {
                    auto kws = match.value("keywords", nlohmann::json::array());
                    if (kws.is_array())
                        for (const auto& k : kws)
                            if (k.is_string()) row.keywords.push_back(k.get<std::string>());
                    auto tools = match.value("tools", nlohmann::json::array());
                    if (tools.is_array())
                        for (const auto& t : tools)
                            if (t.is_string()) row.tools.push_back(t.get<std::string>());
                    auto evs = match.value("events", nlohmann::json::array());
                    if (evs.is_array())
                        for (const auto& ev : evs)
                            if (ev.is_string()) row.events.push_back(ev.get<std::string>());
                }
                if (!row.id.empty()) s_ovTrig.rows.push_back(std::move(row));
            }
        }
        std::sort(s_ovTrig.rows.begin(), s_ovTrig.rows.end(),
                  [](const OvTriggerRow& a, const OvTriggerRow& b) {
                      return a.id < b.id;
                  });
        s_ovTrig.loaded = true;
    });
}
static void ovLoadTriggerBody(AppState& st, const std::string& id) {
    if (id.empty() || s_ovTrig.bodyInFlight) return;
    s_ovTrig.bodyInFlight = true;
    s_ovTrig.bodyDirty = false;
    s_ovTrig.packDirty = false;
    st.rpc.call("triggers_get", nlohmann::json{{"id", id}},
                [id](const core::RpcResult& r) {
        s_ovTrig.bodyInFlight = false;
        if (ovTriggersAbsent(r)) {
            s_ovTrig.rpcMissing = true;
            return;
        }
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", true)) {
            s_ovTrig.lastError = r.err.empty()
                ? r.result.value("reason", std::string("triggers_get failed"))
                : r.err;
            return;
        }
        if (s_ovTrig.selected != id) return;
        auto trig = r.result.value("trigger", nlohmann::json::object());
        auto pack = r.result.value("pack", nlohmann::json::object());
        s_ovTrig.enabled = trig.value("enabled", true);
        s_ovTrig.budgetTokens = trig.value("budget_tokens", 250);
        s_ovTrig.cooldownTurns = trig.value("cooldown_turns", 8);
        std::string purpose = trig.value("purpose", std::string());
        std::memset(s_ovTrig.purpose, 0, sizeof(s_ovTrig.purpose));
        std::strncpy(s_ovTrig.purpose, purpose.c_str(), sizeof(s_ovTrig.purpose) - 1);
        auto inject = trig.value("inject", nlohmann::json::object());
        std::string packName = inject.is_object()
            ? inject.value("context_pack", std::string()) : std::string();
        std::string exemplar = inject.is_object()
            ? inject.value("exemplar", std::string()) : std::string();
        s_ovTrig.prerunCalcs = (inject.is_object() && inject.contains("prerun_calcs")
                                && inject["prerun_calcs"].is_array())
            ? inject["prerun_calcs"] : nlohmann::json::array();
        std::memset(s_ovTrig.packName, 0, sizeof(s_ovTrig.packName));
        std::strncpy(s_ovTrig.packName, packName.c_str(), sizeof(s_ovTrig.packName) - 1);
        std::memset(s_ovTrig.exemplar, 0, sizeof(s_ovTrig.exemplar));
        std::strncpy(s_ovTrig.exemplar, exemplar.c_str(), sizeof(s_ovTrig.exemplar) - 1);
        auto match = trig.value("match", nlohmann::json::object());
        std::vector<std::string> kws, tools, evs;
        if (match.is_object()) {
            auto a = match.value("keywords", nlohmann::json::array());
            if (a.is_array()) for (const auto& k : a)
                if (k.is_string()) kws.push_back(k.get<std::string>());
            a = match.value("tools", nlohmann::json::array());
            if (a.is_array()) for (const auto& t : a)
                if (t.is_string()) tools.push_back(t.get<std::string>());
            a = match.value("events", nlohmann::json::array());
            if (a.is_array()) for (const auto& e : a)
                if (e.is_string()) evs.push_back(e.get<std::string>());
        }
        ovJoinLines(kws, s_ovTrig.keywords, sizeof(s_ovTrig.keywords));
        ovJoinLines(tools, s_ovTrig.tools, sizeof(s_ovTrig.tools));
        ovJoinLines(evs, s_ovTrig.events, sizeof(s_ovTrig.events));
        std::string body = pack.is_object() ? pack.value("body", std::string()) : std::string();
        std::fill(s_ovTrig.packBody.begin(), s_ovTrig.packBody.end(), 0);
        if (body.size() >= s_ovTrig.packBody.size())
            body.resize(s_ovTrig.packBody.size() - 1);
        std::memcpy(s_ovTrig.packBody.data(), body.data(), body.size());
        s_ovTrig.bodyDirty = false;
        s_ovTrig.packDirty = false;
        s_ovTrig.lastError.clear();
    });
}
static void ovSaveTrigger(AppState& st) {
    if (s_ovTrig.selected.empty() || s_ovTrig.saveInFlight) return;
    s_ovTrig.saveInFlight = true;
    nlohmann::json p;
    p["id"] = s_ovTrig.selected;
    p["enabled"] = s_ovTrig.enabled;
    p["purpose"] = std::string(s_ovTrig.purpose);
    p["budget_tokens"] = s_ovTrig.budgetTokens;
    p["cooldown_turns"] = s_ovTrig.cooldownTurns;
    p["match"] = nlohmann::json{
        {"keywords", ovSplitLinesOrCommas(s_ovTrig.keywords)},
        {"tools", ovSplitLinesOrCommas(s_ovTrig.tools)},
        {"events", ovSplitLinesOrCommas(s_ovTrig.events)},
    };
    p["inject"] = nlohmann::json{
        {"context_pack", std::string(s_ovTrig.packName)},
        {"prerun_calcs", s_ovTrig.prerunCalcs.is_array()
            ? s_ovTrig.prerunCalcs : nlohmann::json::array()},
        {"exemplar", std::string(s_ovTrig.exemplar)},
    };
    for (const auto& row : s_ovTrig.rows) {
        if (row.id == s_ovTrig.selected) {
            if (s_ovTrig.packName[0] == 0 && !row.pack.empty())
                p["inject"]["context_pack"] = row.pack;
            break;
        }
    }
    // Only write pack body when the user actually edited it (or is creating).
    // Toggling Enabled alone must not freeze a shipped pack into the user dir.
    if (s_ovTrig.packDirty || s_ovTrig.creating) {
        p["pack_body"] = std::string(s_ovTrig.packBody.data());
        p["pack_name"] = std::string(s_ovTrig.packName);
    }
    st.rpc.call("triggers_save", p, [](const core::RpcResult& r) {
        s_ovTrig.saveInFlight = false;
        if (ovTriggersAbsent(r)) {
            s_ovTrig.rpcMissing = true;
            return;
        }
        if (!r.ok || (r.result.is_object() && !r.result.value("ok", true))) {
            s_ovTrig.lastError = r.err.empty()
                ? r.result.value("reason", std::string("triggers_save failed"))
                : r.err;
            return;
        }
        s_ovTrig.bodyDirty = false;
        s_ovTrig.packDirty = false;
        s_ovTrig.lastError.clear();
        s_ovTrig.loaded = false;
        s_ovTrig.lastList = -1e9;
    });
}
static void ovDeleteTriggerOverride(AppState& st, const std::string& id) {
    if (id.empty()) return;
    st.rpc.call("triggers_delete", nlohmann::json{{"id", id}},
                [id](const core::RpcResult& r) {
        if (!r.ok || (r.result.is_object() && !r.result.value("ok", true))) {
            s_ovTrig.lastError = r.err.empty()
                ? r.result.value("reason", std::string("triggers_delete failed"))
                : r.err;
            return;
        }
        s_ovTrig.lastError.clear();
        s_ovTrig.loaded = false;
        s_ovTrig.lastList = -1e9;
        if (s_ovTrig.selected == id)
            s_ovTrig.bodyDirty = false;
    });
}

static void drawSettingsSkillsTab(AppState& st, float S) {
    ovPollSkills(st, /*force=*/false);

    if (s_ovSkills.rpcMissing) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextWrapped("skills_* RPC not available \xe2\x80\x94 skill editor hidden.");
        ImGui::PopStyleColor();
        return;
    }
    if (!s_ovSkills.lastError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
        ImGui::TextWrapped("%s", s_ovSkills.lastError.c_str());
        ImGui::PopStyleColor();
    }

    // One scrollbar only: size columns to the remaining viewport minus the group title
    // Interwebz paints above the child (title + underline). Parent ##tab_ai must not also scroll.
    const float titleBudget = ImGui::GetTextLineHeight() + 10.0f * S;
    const float colH = ImMax(180.0f * S, ImGui::GetContentRegionAvail().y - titleBudget);
    ui::InterwebzTwoColumnBegin(12.0f);
    if (ui::InterwebzGroupBegin("##set_skill_list", "Catalog", ImVec2(0, colH))) {
        if (ImGui::Button("New skill", ImVec2(-1, 0))) {
            s_ovSkills.creating = true;
            s_ovSkills.selected.clear();
            std::fill(s_ovSkills.body.begin(), s_ovSkills.body.end(), 0);
            std::strncpy(s_ovSkills.body.data(), kSkillCreatorTemplate,
                         s_ovSkills.body.size() - 1);
            s_ovSkills.bodyName.clear();
            s_ovSkills.bodyDirty = true;
            std::memset(s_ovSkills.newName, 0, sizeof(s_ovSkills.newName));
            std::strncpy(s_ovSkills.newName, "my-skill", sizeof(s_ovSkills.newName) - 1);
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Create a user skill from the skill-creator template.");
        ImGui::Dummy(ImVec2(0, 4.0f * S));

        if (!s_ovSkills.loaded && s_ovSkills.listInFlight) {
            ImGui::TextUnformatted("Loading skills\xe2\x80\xa6");
        } else if (s_ovSkills.skills.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                       ui::palette.textDim[2], 1));
            ImGui::TextWrapped("No skills discovered.");
            ImGui::PopStyleColor();
        } else {
            for (size_t i = 0; i < s_ovSkills.skills.size(); ++i) {
                const auto& sk = s_ovSkills.skills[i];
                ImGui::PushID((int)i);
                const bool sel = (!s_ovSkills.creating && s_ovSkills.selected == sk.name);
                if (ImGui::Selectable("##skrow", sel, 0, ImVec2(0, 0))) {
                    s_ovSkills.creating = false;
                    s_ovSkills.selected = sk.name;
                    ovLoadSkillBody(st, sk.name);
                }
                // Paint name + source chip on the same row.
                {
                    ImVec2 rmin = ImGui::GetItemRectMin();
                    ImVec2 rmax = ImGui::GetItemRectMax();
                    // Expand row height for two lines: name + description.
                    (void)rmin; (void)rmax;
                }
                ImGui::SameLine(0, 0);
                ImGui::BeginGroup();
                ImGui::TextUnformatted(sk.name.c_str());
                ImGui::SameLine();
                {
                    const char* chip = sk.source.c_str();
                    ImU32 chipCol = ui::Palette::toU32(ui::palette.textDim);
                    if (sk.source == "user")
                        chipCol = ui::Palette::toU32(ui::palette.accent);
                    else if (sk.source == "project")
                        chipCol = ui::Palette::toU32(ui::palette.statusOk);
                    ImGui::PushStyleColor(ImGuiCol_Text, chipCol);
                    ImGui::Text("[%s]", chip);
                    ImGui::PopStyleColor();
                }
                if (!sk.description.empty()) {
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                               ui::palette.textDim[2], 1));
                    ImGui::TextWrapped("%s", sk.description.c_str());
                    ImGui::PopStyleColor();
                }
                ImGui::EndGroup();
                // Make the group also selectable.
                if (ImGui::IsItemClicked()) {
                    s_ovSkills.creating = false;
                    s_ovSkills.selected = sk.name;
                    ovLoadSkillBody(st, sk.name);
                }
                ImGui::PopID();
                ImGui::Dummy(ImVec2(0, 2.0f * S));
            }
        }
    }
    ui::InterwebzGroupEnd();

    ui::InterwebzTwoColumnNext();
    if (ui::InterwebzGroupBegin("##set_skill_ed", "Editor", ImVec2(0, colH))) {
        if (s_ovSkills.creating) {
            ImGui::TextUnformatted("Name");
            ImGui::SetNextItemWidth(-1);
            ImGui::InputText("##set_newskill", s_ovSkills.newName, sizeof(s_ovSkills.newName));
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                       ui::palette.textDim[2], 1));
            ImGui::TextWrapped("Saved under the user skills dir via skills_save.");
            ImGui::PopStyleColor();
        } else if (s_ovSkills.selected.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                       ui::palette.textDim[2], 1));
            ImGui::TextWrapped("Select a skill to edit, or New skill.");
            ImGui::PopStyleColor();
        } else {
            ui::InterwebzKeyValueRow("Skill", s_ovSkills.selected.c_str());
            // Source chip for selected.
            const char* src = "builtin";
            for (const auto& sk : s_ovSkills.skills)
                if (sk.name == s_ovSkills.selected) { src = sk.source.c_str(); break; }
            ui::InterwebzKeyValueRow("Source", src);
        }

        if (s_ovSkills.creating || !s_ovSkills.selected.empty()) {
            if (s_ovSkills.bodyInFlight && !s_ovSkills.creating) {
                ImGui::TextUnformatted("Loading body\xe2\x80\xa6");
            } else {
                const float edH = ImMax(100.0f * S,
                    ImGui::GetContentRegionAvail().y - 56.0f * S);
                if (st.fonts.mono) ImGui::PushFont(st.fonts.mono);
                if (ImGui::InputTextMultiline("##set_skillbody", s_ovSkills.body.data(),
                                              s_ovSkills.body.size(), ImVec2(-1, edH)))
                    s_ovSkills.bodyDirty = true;
                if (st.fonts.mono) ImGui::PopFont();
            }

            const bool busySave = s_ovSkills.saveInFlight;
            if (busySave) ImGui::BeginDisabled();
            if (ImGui::Button(s_ovSkills.creating ? "Create##set_sksave" : "Save##set_sksave",
                              ImVec2(-1, 0))) {
                if (s_ovSkills.creating) {
                    // Normalize name from field; rewrite frontmatter name if needed.
                    std::string name = s_ovSkills.newName;
                    // strip spaces
                    name.erase(std::remove_if(name.begin(), name.end(),
                        [](unsigned char c) { return std::isspace(c); }), name.end());
                    if (name.empty()) name = "my-skill";
                    s_ovSkills.selected = name;
                    s_ovSkills.creating = false;
                    ovSaveSkill(st);
                } else {
                    ovSaveSkill(st);
                }
            }
            if (busySave) ImGui::EndDisabled();

            // Delete only for user skills.
            if (!s_ovSkills.creating && !s_ovSkills.selected.empty()) {
                bool isUser = false;
                for (const auto& sk : s_ovSkills.skills)
                    if (sk.name == s_ovSkills.selected && sk.source == "user") {
                        isUser = true; break;
                    }
                if (isUser) {
                    ImGui::PushStyleColor(ImGuiCol_Button,
                        ImVec4(ui::palette.statusErr[0], ui::palette.statusErr[1],
                               ui::palette.statusErr[2], 0.55f));
                    if (ImGui::Button("Delete user skill", ImVec2(-1, 0)))
                        ovDeleteSkill(st, s_ovSkills.selected);
                    ImGui::PopStyleColor();
                }
            }
        }
    }
    ui::InterwebzGroupEnd();
    ui::InterwebzTwoColumnEnd();
}

static void drawSettingsContextTab(AppState& st, float S) {
    ovPollTriggers(st, /*force=*/false);

    if (s_ovTrig.rpcMissing) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextWrapped(
            "triggers_* RPC not available \xe2\x80\x94 Context Injection editor hidden.");
        ImGui::PopStyleColor();
        return;
    }
    if (!s_ovTrig.lastError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
        ImGui::TextWrapped("%s", s_ovTrig.lastError.c_str());
        ImGui::PopStyleColor();
    }

    const float titleBudget = ImGui::GetTextLineHeight() + 10.0f * S;
    const float colH = ImMax(180.0f * S, ImGui::GetContentRegionAvail().y - titleBudget);
    ui::InterwebzTwoColumnBegin(10.0f);
    if (ui::InterwebzGroupBegin("##set_trig_list", "Triggers", ImVec2(0, colH))) {
        if (ImGui::Button("New trigger", ImVec2(-1, 0))) {
            s_ovTrig.creating = true;
            s_ovTrig.selected.clear();
            s_ovTrig.enabled = true;
            s_ovTrig.budgetTokens = 250;
            s_ovTrig.cooldownTurns = 8;
            std::memset(s_ovTrig.purpose, 0, sizeof(s_ovTrig.purpose));
            std::strncpy(s_ovTrig.purpose, "What this pack teaches the model.",
                         sizeof(s_ovTrig.purpose) - 1);
            std::memset(s_ovTrig.packName, 0, sizeof(s_ovTrig.packName));
            std::strncpy(s_ovTrig.packName, "my_pack", sizeof(s_ovTrig.packName) - 1);
            std::memset(s_ovTrig.keywords, 0, sizeof(s_ovTrig.keywords));
            std::strncpy(s_ovTrig.keywords, "\\bmy_keyword\\b",
                         sizeof(s_ovTrig.keywords) - 1);
            std::memset(s_ovTrig.tools, 0, sizeof(s_ovTrig.tools));
            std::memset(s_ovTrig.events, 0, sizeof(s_ovTrig.events));
            std::memset(s_ovTrig.exemplar, 0, sizeof(s_ovTrig.exemplar));
            std::fill(s_ovTrig.packBody.begin(), s_ovTrig.packBody.end(), 0);
            std::strncpy(s_ovTrig.packBody.data(),
                         "# My context pack\n\nImportant considerations:\n- ...\n",
                         s_ovTrig.packBody.size() - 1);
            s_ovTrig.prerunCalcs = nlohmann::json::array();
            s_ovTrig.bodyDirty = true;
            s_ovTrig.packDirty = true;
            std::memset(s_ovTrig.newId, 0, sizeof(s_ovTrig.newId));
            std::strncpy(s_ovTrig.newId, "my_trigger", sizeof(s_ovTrig.newId) - 1);
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip(
                "Create a user-only trigger + pack (saved under context_injection/).");
        ImGui::Dummy(ImVec2(0, 4.0f * S));

        if (!s_ovTrig.loaded && s_ovTrig.listInFlight) {
            ImGui::TextUnformatted("Loading triggers\xe2\x80\xa6");
        } else if (s_ovTrig.rows.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                       ui::palette.textDim[2], 1));
            ImGui::TextWrapped("No triggers discovered.");
            ImGui::PopStyleColor();
        } else {
            for (size_t i = 0; i < s_ovTrig.rows.size(); ++i) {
                const auto& tr = s_ovTrig.rows[i];
                ImGui::PushID((int)i);
                const bool sel = (!s_ovTrig.creating && s_ovTrig.selected == tr.id);
                if (ImGui::Selectable("##trigrow", sel, 0, ImVec2(0, 0))) {
                    s_ovTrig.creating = false;
                    s_ovTrig.selected = tr.id;
                    ovLoadTriggerBody(st, tr.id);
                }
                ImGui::SameLine(0, 0);
                ImGui::BeginGroup();
                ompStatusDotText(tr.id.c_str(),
                                 tr.enabled ? ui::palette.statusOk : ui::palette.textDim,
                                 tr.enabled, ui::palette.text);
                ImGui::SameLine();
                {
                    const char* chip = tr.source.c_str();
                    ImU32 chipCol = ui::Palette::toU32(ui::palette.textDim);
                    if (tr.source == "user")
                        chipCol = ui::Palette::toU32(ui::palette.accent);
                    else if (tr.source == "override")
                        chipCol = ui::Palette::toU32(ui::palette.statusOk);
                    ImGui::PushStyleColor(ImGuiCol_Text, chipCol);
                    ImGui::Text("[%s]", chip);
                    ImGui::PopStyleColor();
                }
                if (!tr.purpose.empty()) {
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                               ui::palette.textDim[2], 1));
                    ImGui::TextWrapped("%s", tr.purpose.c_str());
                    ImGui::PopStyleColor();
                } else if (!tr.pack.empty()) {
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                               ui::palette.textDim[2], 1));
                    ImGui::TextWrapped("pack: %s", tr.pack.c_str());
                    ImGui::PopStyleColor();
                }
                ImGui::EndGroup();
                if (ImGui::IsItemClicked()) {
                    s_ovTrig.creating = false;
                    s_ovTrig.selected = tr.id;
                    ovLoadTriggerBody(st, tr.id);
                }
                ImGui::PopID();
                ImGui::Dummy(ImVec2(0, 2.0f * S));
            }
        }
    }
    ui::InterwebzGroupEnd();

    ui::InterwebzTwoColumnNext();
    if (ui::InterwebzGroupBegin("##set_trig_ed", "Editor", ImVec2(0, colH))) {
        if (s_ovTrig.creating) {
            ImGui::TextUnformatted("Id");
            ImGui::SetNextItemWidth(-1);
            ImGui::InputText("##set_newtrig", s_ovTrig.newId, sizeof(s_ovTrig.newId));
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                       ui::palette.textDim[2], 1));
            ImGui::TextWrapped(
                "Letters, digits, _ and - only. Saved as a user trigger.");
            ImGui::PopStyleColor();
        } else if (s_ovTrig.selected.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                       ui::palette.textDim[2], 1));
            ImGui::TextWrapped("Select a trigger to edit, or New trigger.");
            ImGui::PopStyleColor();
        } else {
            ui::InterwebzKeyValueRow("Trigger", s_ovTrig.selected.c_str());
            const char* src = "builtin";
            for (const auto& tr : s_ovTrig.rows)
                if (tr.id == s_ovTrig.selected) { src = tr.source.c_str(); break; }
            ui::InterwebzKeyValueRow("Source", src);
        }

        if (s_ovTrig.creating || !s_ovTrig.selected.empty()) {
            if (s_ovTrig.bodyInFlight && !s_ovTrig.creating) {
                ImGui::TextUnformatted("Loading\xe2\x80\xa6");
            } else {
                if (ui::InterwebzCheckbox("Enabled", &s_ovTrig.enabled))
                    s_ovTrig.bodyDirty = true;
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip(
                        "Off = never inject this pack (user override of shipped).");

                ImGui::TextUnformatted("Purpose");
                ImGui::SetNextItemWidth(-1);
                if (ImGui::InputText("##set_purpose", s_ovTrig.purpose,
                                     sizeof(s_ovTrig.purpose)))
                    s_ovTrig.bodyDirty = true;
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip(
                        "Why this pack exists — what important considerations it "
                        "forces the model to take.");

                ImGui::TextUnformatted("Keywords (regex, one per line)");
                ImGui::SetNextItemWidth(-1);
                if (ImGui::InputTextMultiline("##set_kws", s_ovTrig.keywords,
                        sizeof(s_ovTrig.keywords),
                        ImVec2(-1, 56.0f * S)))
                    s_ovTrig.bodyDirty = true;

                ImGui::TextUnformatted("Tools (names that fire this)");
                ImGui::SetNextItemWidth(-1);
                if (ImGui::InputText("##set_tools", s_ovTrig.tools,
                                     sizeof(s_ovTrig.tools)))
                    s_ovTrig.bodyDirty = true;
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip(
                        "Comma or newline separated tool names, e.g. part_place, fit_calc.");

                ImGui::TextUnformatted("Events");
                ImGui::SetNextItemWidth(-1);
                if (ImGui::InputText("##set_events", s_ovTrig.events,
                                     sizeof(s_ovTrig.events)))
                    s_ovTrig.bodyDirty = true;

                ImGui::TextUnformatted("Context pack name");
                ImGui::SetNextItemWidth(-1);
                if (ImGui::InputText("##set_packname", s_ovTrig.packName,
                                     sizeof(s_ovTrig.packName)))
                    s_ovTrig.bodyDirty = true;

                ImGui::TextUnformatted("Exemplar (short worked example)");
                ImGui::SetNextItemWidth(-1);
                if (ImGui::InputText("##set_exemplar", s_ovTrig.exemplar,
                                     sizeof(s_ovTrig.exemplar)))
                    s_ovTrig.bodyDirty = true;

                ImGui::SetNextItemWidth(ImMax(100.0f * S,
                    ImGui::GetContentRegionAvail().x * 0.48f));
                if (ImGui::SliderInt("Budget tokens", &s_ovTrig.budgetTokens,
                                     50, 2000))
                    s_ovTrig.bodyDirty = true;
                ImGui::SameLine();
                ImGui::SetNextItemWidth(ImMax(80.0f * S,
                    ImGui::GetContentRegionAvail().x * 0.9f));
                if (ImGui::SliderInt("Cooldown", &s_ovTrig.cooldownTurns, 0, 32))
                    s_ovTrig.bodyDirty = true;

                ImGui::TextUnformatted("Pack body (markdown injected into context)");
                const float edH = ImMax(80.0f * S,
                    ImGui::GetContentRegionAvail().y - 72.0f * S);
                if (st.fonts.mono) ImGui::PushFont(st.fonts.mono);
                if (ImGui::InputTextMultiline("##set_packbody",
                        s_ovTrig.packBody.data(), s_ovTrig.packBody.size(),
                        ImVec2(-1, edH))) {
                    s_ovTrig.bodyDirty = true;
                    s_ovTrig.packDirty = true;
                }
                if (st.fonts.mono) ImGui::PopFont();
            }

            const bool busySave = s_ovTrig.saveInFlight;
            if (busySave) ImGui::BeginDisabled();
            if (ImGui::Button(s_ovTrig.creating ? "Create##set_trsave" : "Save##set_trsave",
                              ImVec2(-1, 0))) {
                if (s_ovTrig.creating) {
                    std::string name = s_ovTrig.newId;
                    name.erase(std::remove_if(name.begin(), name.end(),
                        [](unsigned char c) {
                            return !(std::isalnum(c) || c == '_' || c == '-');
                        }), name.end());
                    if (name.empty()) name = "my_trigger";
                    s_ovTrig.selected = name;
                    s_ovTrig.creating = false;
                    ovSaveTrigger(st);
                } else {
                    ovSaveTrigger(st);
                }
            }
            if (busySave) ImGui::EndDisabled();
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip(
                    "Writes a user override (shipped triggers.yaml is never rewritten).");

            if (!s_ovTrig.creating && !s_ovTrig.selected.empty()) {
                bool isOverrideOrUser = false;
                bool isUserOnly = false;
                for (const auto& tr : s_ovTrig.rows) {
                    if (tr.id == s_ovTrig.selected) {
                        isOverrideOrUser = (tr.source == "override" || tr.source == "user");
                        isUserOnly = (tr.source == "user");
                        break;
                    }
                }
                if (isOverrideOrUser) {
                    ImGui::PushStyleColor(ImGuiCol_Button,
                        ImVec4(ui::palette.statusErr[0], ui::palette.statusErr[1],
                               ui::palette.statusErr[2], 0.55f));
                    if (ImGui::Button(
                            isUserOnly ? "Delete user trigger"
                                       : "Reset to shipped",
                            ImVec2(-1, 0)))
                        ovDeleteTriggerOverride(st, s_ovTrig.selected);
                    ImGui::PopStyleColor();
                    if (ImGui::IsItemHovered())
                        ImGui::SetTooltip(
                            isUserOnly
                                ? "Remove this user-only trigger."
                                : "Clear the user override and restore the shipped trigger.");
                }
            }
        }
    }
    ui::InterwebzGroupEnd();
    ui::InterwebzTwoColumnEnd();
}

}  // namespace (skills/context editors)

// ---- kSettingsEntries: unified command search table (command_search.cpp) ----
// Tab indices: 0=Appearance 1=General 2=AI 3=Providers 4=Benchmarks 5=Prompts
// 6=Experimental (Agent tab merged into AI)
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
    { "Configurations",           "General",      1 },
    { "Engine routing",           "General",      1 },
    { "C++ core",                 "General",      1 },
    { "Performance profiler",     "General",      1 },
    { "Profiler",                 "General",      1 },
    // AI (tab 2) — harness (session knobs), roles, tools+maintenance
    { "Session knobs",            "AI",           2 },
    { "Harness",                  "AI",           2 },
    { "Tools",                    "AI",           2 },
    { "Tool families",            "AI",           2 },
    { "Sketch 2D",                "AI",           2 },
    { "Model 3D",                 "AI",           2 },
    { "Offline mode",             "AI",           2 },
    { "Reasoning effort",         "AI",           2 },
    { "Manage models",            "AI",           2 },
    { "Model roles",              "AI",           2 },
    { "Hidden models",            "AI",           2 },
    { "Harness organization",     "AI",           2 },
    { "Copilot mode",             "AI",           2 },
    { "Mode defaults",            "AI",           2 },
    { "Eng. Firm",                "AI",           2 },
    { "Eng. Firm model assignments", "AI",        2 },
    { "Swarm",                    "AI",           2 },
    // Providers (tab 3) — provider/model, keys, Moonshot, accounts
    { "AI provider",              "Providers",    3 },
    { "Provider & Model",         "Providers",    3 },
    { "Model",                    "Providers",    3 },
    { "API key",                  "Providers",    3 },
    { "Gemini key",               "Providers",    3 },
    { "Anthropic key",            "Providers",    3 },
    { "OpenAI key",               "Providers",    3 },
    { "Cloud API key",            "Providers",    3 },
    { "Moonshot",                 "Providers",    3 },
    { "Kimi",                     "Providers",    3 },
    { "Moonshot key",             "Providers",    3 },
    { "Vision",                   "Providers",    3 },
    { "Local endpoint",           "Providers",    3 },
    { "Accounts",                 "Providers",    3 },
    { "Subscription accounts",    "Providers",    3 },
    { "Sign in",                  "Providers",    3 },
    { "Add API key",              "Providers",    3 },
    { "OAuth",                    "Providers",    3 },
    { "Disconnect",               "Providers",    3 },
    { "Usage & cost",             "Providers",    3 },
    { "Session cost",             "Providers",    3 },
    { "Model routing",            "Providers",    3 },
    // Benchmarks (tab 4)
    { "Benchmarks",               "AI",    2 },
    { "Model leaderboard",        "AI",    2 },
    { "Run benchmark",            "AI",    2 },
    { "Benchmark",                "AI",    2 },
    { "Benchmark categories",     "AI",    2 },
    { "Category run",             "AI",    2 },
    // Prompts (tab 5)
    { "Prompts",                  "AI",           2 },
    { "Role prompts",             "AI",           2 },
    { "Agent prompts",            "AI",       2 },
    { "Prompt editor",            "AI",       2 },
    { "System prompt",            "AI",       2 },
    { "System prompt override",   "AI",       2 },
    { "Skills",                   "AI",       2 },
    { "Skill editor",             "AI",       2 },
    { "Context injection",        "AI",       2 },
    { "Triggers",                 "AI",       2 },
    // Run behavior (lives on AI tab; was Agent tab 6)
    { "Plan preference",          "AI",           2 },
    { "Plan before building",     "AI",           2 },
    { "Plan mode",                "AI",           2 },
    { "Subagent preference",      "AI",           2 },
    { "Role adoption",            "AI",           2 },
    { "Advisor preference",       "AI",           2 },
    { "Proactive advisor",        "AI",           2 },
    { "Project memory",           "AI",           2 },
    { "Memory budget",            "AI",           2 },
    { "Refine-pass cap",          "AI",           2 },
    { "Max iters",                "AI",           2 },
    { "Iteration budget",         "AI",           2 },
    { "Eng. Firm budget",         "AI",           2 },
    { "Company budget",           "AI",           2 },
    { "Run behavior",             "AI",           2 },
    { "Maintenance",              "AI",           2 },
    { "Max subagent nesting",     "AI",           2 },
    // Experimental (tab 6 — Agent tab removed)
    { "Vision review model",      "Experimental",  4 },
    { "Model routing",            "Experimental",  4 },
    { "Seek-CAD split",           "Experimental",  4 },
    { "Parametric sequence mode", "Experimental",  4 },
    { "CAD sequence mode",        "Experimental",  4 },
    { "Feature sequence",         "Experimental",  4 },
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
        // Re-read eng_firm.models so the assignments grid matches disk on every open.
        s_efModels.loaded = false;
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

        // Sticky tab bar: fixed header region (tabs) + scrolling body per tab.
        // Outer child is non-scrolling so the tab row never leaves the top; each
        // tab wraps its content in settingsTabBodyBegin/End so only the body scrolls.
        // Save/Cancel stay pinned under the whole tabs area.
        const float footerH = ImGui::GetFrameHeightWithSpacing() + 20.0f * sc;
        pushGlassChildStyle(10.0f);
        ImGui::BeginChild("##settabsarea",
                          ImVec2(0, std::max(120.0f * sc,
                                             ImGui::GetContentRegionAvail().y - footerH)),
                          ImGuiChildFlags_None,
                          ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
        if (ImGui::BeginTabBar("##settabs")) {
            // ---- Appearance tab (FIRST): theme + colored liquid glass (v2.1) ----
            // The canonical theme control lives here (moved from General; the View-menu item
            // stays a synced shortcut). The glass knobs edit Settings::glassCfg per group and
            // live-apply via ui::setGlassConfig + persist via saveSettings on every change —
            // so the persisted "appearance.glass" block is no longer a dead round-trip.
            if (ImGui::BeginTabItem("Appearance", nullptr,
                                    (jumpToTab == 0) ? ImGuiTabItemFlags_SetSelected
                                                     : ImGuiTabItemFlags_None)) {
                settingsTabBodyBegin("##tab_appearance");
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
                settingsTabBodyEnd();
                ImGui::EndTabItem();
            }

            // ---- General tab: Document (units) + System (engine readout, profiler) ----
            if (ImGui::BeginTabItem("General", nullptr,
                                    (jumpToTab == 1) ? ImGuiTabItemFlags_SetSelected
                                                     : ImGuiTabItemFlags_None)) {
                settingsTabBodyBegin("##tab_general");
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

                // Configurations moved here from the left-pane strip (Graph took that slot).
                settingsSectionHeader("Configurations");
                if (st.configPanel) {
                    st.configPanel->drawEmbedded(st);
                } else {
                    ui::drawConfigurationsBody(st);
                }

                // Workspace folder (audit P1): workspace_dir was persisted but had NO UI —
                // users had to hand-edit config.json to re-root the File Explorer.
                settingsSectionHeader("Workspace");
                ImGui::TextUnformatted("Workspace folder");
#ifdef __EMSCRIPTEN__
                // Web: default is the server workspace abs root (fsBackend), not the
                // literal "/workspace" stub from defaultWorkspaceDir().
                {
                    static std::string s_wsDefaultWeb;
                    if (s_wsDefaultWeb.empty()) {
                        s_wsDefaultWeb = chudware::fsBackend().defaultRoot();
                        if (s_wsDefaultWeb.empty() || s_wsDefaultWeb == "/workspace")
                            s_wsDefaultWeb = chudware::fsBackend().defaultRoot();
                    }
                    static const std::string s_wsHelpWeb =
                        "The File Explorer's root folder on the server workspace. "
                        "Leave empty to use the server default. Browse picks a "
                        "subfolder under the workspace.";
                    static const std::string s_wsHintWeb =
                        s_wsDefaultWeb.empty() ? std::string("(server default)")
                                               : ("(default: " + s_wsDefaultWeb + ")");
                    helpMarker(s_wsHelpWeb.c_str());
                    ImGui::SetNextItemWidth(-210.0f * sc);
                    ImGui::InputTextWithHint("##wsdir", s_wsHintWeb.c_str(),
                                             s_wsBuf.data(), s_wsBuf.size());
                    ImGui::SameLine();
                    if (ImGui::Button("Browse\xe2\x80\xa6", ImVec2(90.0f * sc, 0))) {
                        std::string chosen = openFolderDialog();
                        if (!chosen.empty() && chosen != "/workspace") {
                            std::fill(s_wsBuf.begin(), s_wsBuf.end(), 0);
                            std::strncpy(s_wsBuf.data(), chosen.c_str(), s_wsBuf.size() - 1);
                        }
                    }
                    ImGui::SameLine();
                    if (ImGui::Button("Apply", ImVec2(100.0f * sc, 0))) {
                        std::string chosen = s_wsBuf.data();
                        // Never persist the literal '/workspace' stub — resolve real root.
                        if (chosen.empty() || chosen == "/workspace") {
                            chosen.clear();
                            settings.workspaceDir.clear();
                            st.workspaceDir.clear();
                            // ensureWorkspaceDir will re-resolve via fsBackend().defaultRoot().
                        } else {
                            settings.workspaceDir = chosen;
                            st.workspaceDir = chosen;
                        }
                        saveSettings(settings);
                        st.addLog("[ok] workspace folder set: " +
                                  (settings.workspaceDir.empty() ? std::string("(default)")
                                                                 : settings.workspaceDir));
                    }
                    dimText("Root of the File Explorer on the server workspace (saved on Apply).");
                }
#else
                // Truthful default label: defaultWorkspaceDir() applies the alias rule
                // (existing Documents/Chudware wins, else Documents/Chudware).
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
                    // Re-resolve the live root: empty falls back to Documents/Chudware inside
                    // the Explorer's ensureWorkspaceDir.
                    st.workspaceDir = settings.workspaceDir;
                    st.addLog("[ok] workspace folder set: " +
                              (settings.workspaceDir.empty() ? std::string("(default)")
                                                             : settings.workspaceDir));
                }
                dimText("Root of the File Explorer and default save location (saved on Apply).");
#endif

                // Theme moved to the Appearance tab (the canonical control); engine routing +
                // profiler are status/launchers, folded under one System section (audit P2).
                settingsSectionHeader("System");
                // R5a (T-RF21): routing is unconditional — the C++ core IS the engine
                // (the CHUDWARE_CPP_* escape hatches were removed with the Python oracle).
                if (core::cpp_core().available()) {
                    ImGui::TextUnformatted("Active engine: C++ core (libchudware)");
                    helpMarker("The in-process C++ core is the only modeling engine — documents, "
                               "sketching, FEA/CFD, CAM, ECAD, firmware, sourcing and the AI agent "
                               "all run on it. Services-side Python handles orchestration surfaces "
                               "(parts catalog, reverse engineering, vendor sign-in, missions).");
                    dimText(("C++ core: libchudware loaded (ABI " +
                             core::cpp_core().abiVersion() + ")").c_str());
                    ImGui::BulletText("Modeling / documents / sketch: C++ core");
                    ImGui::BulletText("Simulation (FEA + CFD) / CAM / ECAD / firmware: C++ core");
                    ImGui::BulletText("AI agent (copilot runs): C++ core");
                } else {
                    ImGui::TextUnformatted("Active engine: UNAVAILABLE (libchudware not loaded)");
                    helpMarker("The C++ core (libchudware) is NOT loaded, and it is the only engine "
                               "(the Python oracle was removed). Set the CHUDWARE_LIBCHUDWARE "
                               "environment variable to the built library "
                               "(core/build-occt-linux/bin/libchudware.so) or stage it next to the "
                               "executable, then restart.");
                    dimText("C++ core: not loaded");
                }

                ImGui::Dummy(ImVec2(0, 8 * sc));
                dimText("Sampling profiler \xe2\x80\x94 see where modeling time goes (extrude / "
                        "fillet / boolean, into the kernel) as a live flame graph.");
                if (ImGui::Button("Open Profiler", ImVec2(160.0f * sc, 30.0f * sc)))
                    st.showProfilerWindow = true;
                ImGui::Dummy(ImVec2(0, 6 * sc));
                dimText(("All settings here are stored in " + configPath() + ".").c_str());
                settingsTabBodyEnd();
                ImGui::EndTabItem();
            }
            // ---- AI tab: unified hub ----
            // Tab indices: 0=Appearance 1=General 2=AI 3=Providers 4=Experimental
            static int s_aiSubJump = -1;
            if (ImGui::BeginTabItem("AI", nullptr,
                                    (jumpToTab == 2 || jumpToPrompts) ? ImGuiTabItemFlags_SetSelected
                                                                     : ImGuiTabItemFlags_None)) {
                settingsTabBodyBegin("##tab_ai");
                if (jumpToPrompts && s_aiSubJump < 0) s_aiSubJump = 2;  // Prompts sub-tab
                if (jumpToTab == 4 && s_aiSubJump < 0) s_aiSubJump = 5;  // Benchmarks
                if (jumpToTab == 5 && s_aiSubJump < 0) s_aiSubJump = 2;  // Prompts (legacy)

                if (ImGui::BeginTabBar("##ai_sub", ImGuiTabBarFlags_FittingPolicyScroll)) {
                    auto subFlags = [](int idx) {
                        return (s_aiSubJump == idx) ? ImGuiTabItemFlags_SetSelected
                                                    : ImGuiTabItemFlags_None;
                    };

                    // Harness is first: full quick-settings mirror + mode grid.
                    // (Behavior was absorbed here so knobs aren't duplicated.)
                    if (ImGui::BeginTabItem("Harness", nullptr, subFlags(0))) {
                        settingsGlassSectionBegin("##ai_harness_org", "Harness organization");
                        drawHarnessOrganizationSection(st, sc);
                        settingsGlassSectionEnd();

                        settingsGlassSectionBegin("##ai_harness_session", "Session knobs");
                        dimText("Same knobs as Copilot -> Quick settings, plus modal-only "
                                "options (role adoption, company auto-route, Eng. Firm budget).");
                        ImGui::Dummy(ImVec2(0, 4.0f * sc));
                        drawHarnessSessionKnobs(st, sc);
                        settingsGlassSectionEnd();

                        settingsGlassSectionBegin("##ai_memory", "Memory & Learning");
                        drawMemoryLearningSection(st, sc);
                        settingsGlassSectionEnd();

                        settingsGlassSectionBegin("##ai_privacy", "Privacy");
                        ui::drawAiSettingsSection(st, busy, ui::AiSettingsSection::Privacy);
                        settingsGlassSectionEnd();
                        ImGui::EndTabItem();
                    }



                    if (ImGui::BeginTabItem("Roles", nullptr, subFlags(1))) {
                        // Model pickers first (compact table).
                        settingsGlassSectionBegin("##ai_model_roles", "Model roles");
                drawModelRolesSection(st, sc);
                settingsGlassSectionEnd();
                        ImGui::Dummy(ImVec2(0, 8.0f * sc));
                        {
                            static bool s_efOpen = false;
                            ImGui::SetNextItemOpen(s_efOpen, ImGuiCond_Once);
                            if (ImGui::CollapsingHeader("Eng. Firm model assignments")) {
                                s_efOpen = true;
                                settingsGlassSectionBegin("##ai_eng_firm", "Eng. Firm");
                                drawEngFirmModelAssignments(st, sc);
                                settingsGlassSectionEnd();
                            } else {
                                s_efOpen = false;
                            }
                        }
                        ImGui::EndTabItem();
                    }

                    if (ImGui::BeginTabItem("Prompts", nullptr, subFlags(2))) {
                        // Full-height role prompt editor for power users / dev refinement.
                        bool canFetch = st.workerStarted && !busy;
                        if (!s_promptsLoaded && canFetch) loadPrompts(st);

                        ImGui::AlignTextToFramePadding();
                        ImGui::TextUnformatted("Role prompts");
                        if (ImGui::IsItemHovered())
                            ImGui::SetTooltip(
                                "Edit per-role system prompts (pipeline + harness roles). "
                                "Overrides save next to the shipped YAML cards.");
                        ImGui::SameLine();
                        if (!canFetch) ImGui::BeginDisabled();
                        if (ImGui::SmallButton("Refresh##prompts_refresh")) {
                            s_promptsLoaded = false;
                            loadPrompts(st);
                        }
                        if (!canFetch) ImGui::EndDisabled();
                        if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                            ImGui::SetTooltip(canFetch
                                ? "Reload prompt catalog from the worker"
                                : "Start the worker to load role prompts");
                        if (!s_promptsError.empty()) {
                            ImGui::SameLine();
                            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
                            ImGui::TextUnformatted(s_promptsError.c_str());
                            ImGui::PopStyleColor();
                        } else if (!s_promptsLoaded) {
                            ImGui::SameLine();
                            dimText(st.workerStarted
                                ? "Loading\xe2\x80\xa6"
                                : "Worker not ready \xe2\x80\x94 start runtime to edit prompts.");
                        } else {
                            ImGui::SameLine();
                            dimText((std::to_string(s_promptRecs.size()) + " roles").c_str());
                        }

                        // Auto-select first role once the catalog lands.
                        if (s_promptsLoaded && !s_promptRecs.empty() &&
                            (s_selectedRole < 0 ||
                             (s_selectedRole > (int)s_promptRecs.size()))) {
                            s_selectedRole = 0;
                        }

                        const int sysIdx = (int)s_promptRecs.size();
                        const float listW = 168.0f * sc;
                        // Fill remaining viewport — single scrollbar inside list/editor only.
                        const float promptsH = ImMax(220.0f * sc,
                            ImGui::GetContentRegionAvail().y - 4.0f * sc);

                        ImGui::PushStyleColor(ImGuiCol_ChildBg,
                            ImVec4(ui::palette.panelBg[0], ui::palette.panelBg[1],
                                   ui::palette.panelBg[2], 1));
                        ImGui::BeginChild("##rolelist", ImVec2(listW, promptsH),
                                          ImGuiChildFlags_Border);
                        if (s_promptRecs.empty() && s_promptsLoaded) {
                            dimText("No role prompts returned.");
                        }
                        for (int i = 0; i < (int)s_promptRecs.size(); ++i) {
                            bool sel = (i == s_selectedRole);
                            auto& pr = s_promptRecs[i];
                            std::string lbl = pr.role;
                            if (pr.overridden) lbl += " *";
                            if (ImGui::Selectable(lbl.c_str(), sel))
                                s_selectedRole = i;
                            if (ImGui::IsItemHovered())
                                ImGui::SetTooltip("%s%s", pr.role.c_str(),
                                                  pr.overridden ? " (overridden)" : "");
                        }
                        if (!s_promptRecs.empty() || true) ImGui::Separator();
                        {
                            std::string syslbl = "system (override)";
                            if (st.spBuf[0] != 0) syslbl += " *";
                            if (ImGui::Selectable(syslbl.c_str(), s_selectedRole >= sysIdx))
                                s_selectedRole = sysIdx;
                            if (ImGui::IsItemHovered())
                                ImGui::SetTooltip("Override the agent system prompt (advanced)");
                        }
                        ImGui::EndChild();
                        ImGui::PopStyleColor();

                        ImGui::SameLine();

                        ImGui::BeginChild("##roleeditor", ImVec2(0, promptsH),
                                          ImGuiChildFlags_None);
                        if (s_selectedRole >= sysIdx) {
                            ImGui::PushFont(st.fonts.medium);
                            ImGui::TextUnformatted("System prompt override");
                            ImGui::PopFont();
                            dimText("Empty = built-in. Custom text fully replaces it \xe2\x80\x94 "
                                    "keep the result/parts contract. Applied by Save below.");
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
                                    std::strncpy(st.spBuf.data(),
                                                 pr.value("system_prompt", "").c_str(),
                                                 st.spBuf.size() - 1);
                                } else {
                                    st.addLog("[err] get_prompts: " + perr);
                                }
                            }
                            if (!canFetch) ImGui::EndDisabled();
                            if (ImGui::IsItemHovered())
                                ImGui::SetTooltip("Copy the built-in prompt into the editor");
                            ImGui::SameLine();
                            if (ImGui::Button("Clear (use built-in)"))
                                std::fill(st.spBuf.begin(), st.spBuf.end(), 0);
                        } else if (s_selectedRole >= 0 &&
                                   s_selectedRole < (int)s_promptRecs.size()) {
                            auto& pr = s_promptRecs[s_selectedRole];
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
                                ImGui::SetTooltip("Delete override and restore shipped prompt");
                        } else {
                            dimText("Select a role on the left to edit its prompt.");
                        }
                        ImGui::EndChild();
                        ImGui::EndTabItem();
                    }


                    if (ImGui::BeginTabItem("Skills", nullptr, subFlags(3))) {
                        // Fill-height editors: pin parent scroll so only the catalog/editor scroll.
                        ImGui::SetScrollY(0.0f);
                        drawSettingsSkillsTab(st, sc);
                        ImGui::EndTabItem();
                    }

                    if (ImGui::BeginTabItem("Context", nullptr, subFlags(4))) {
                        ImGui::SetScrollY(0.0f);
                        drawSettingsContextTab(st, sc);
                        ImGui::EndTabItem();
                    }

                    if (ImGui::BeginTabItem("Benchmarks", nullptr, subFlags(5))) {
                // Shared leaderboard board (refreshed by runner / refresh button).
                static ui::leaderboard::Board s_lb;
                static bool s_lbLoaded = false;
                static double s_lbLast = -1e9;
                const double now = ImGui::GetTime();
                if (!s_lbLoaded || (now - s_lbLast) > 5.0) {
                    s_lb = ui::leaderboard::load();
                    s_lbLoaded = true;
                    s_lbLast = now;
                }

                settingsGlassSectionBegin("##bench_categories", "Categories");
                drawBenchCategories(st, sc);
                // Keep status polling alive while this tab is open.
                pollBenchStatus(st, s_bench.running, &s_lb);
                settingsGlassSectionEnd();

                settingsGlassSectionBegin("##bench_leaderboard", "Model Leaderboard");
                dimText("Deterministic gym pass rates from benchmarks/leaderboard/leaderboard.json. "
                        "MOCK badge when task_suite ends in -MOCK.");
                if (ImGui::SmallButton("Refresh##settings_lb")) {
                    s_lb = ui::leaderboard::load();
                    s_lbLast = ImGui::GetTime();
                    s_cats.lastPoll = -1e9;
                    s_cats.loaded = false;
                }
                ImGui::Dummy(ImVec2(0, 2 * sc));
                ui::leaderboard::drawCompactTable(s_lb, sc);
                drawBenchRunner(st, sc, s_lb, s_lbLast);
                settingsGlassSectionEnd();

                                        ImGui::EndTabItem();
                    }

                    if (ImGui::BeginTabItem("Tools", nullptr, subFlags(6))) {

                        settingsGlassSectionBegin("##ai_tools_families", "Tool families");
                        dimText("Enable or disable MCP tool families the agent may call. "
                                "Expand a family for per-tool options (timeout, call caps, "
                                "param defaults, prompt notes). Saved under config.json "
                                "agent.tool_families / agent.tool_options.");
                        static std::map<std::string, bool> s_toolFam;
                        static double s_toolFamLast = -1e9;
                        static bool s_toolFamDirty = false;
                        static bool s_toolOptDirty = false;
                        static std::map<std::string, bool> s_toolEnabled;
                        static std::map<std::string, int> s_toolTimeout;
                        static std::map<std::string, int> s_toolMaxCalls;
                        static std::map<std::string, std::string> s_toolDefaults;
                        static std::map<std::string, std::string> s_toolNote;
                        static std::string s_toolOptErr;
                        static const char* kToolFamilies[] = {
                            "cad", "ecad", "cam", "em", "reveng", "plm", "parts",
                            "advisor", "ledger", "intake", "recon", "gsl",
                            "workspace", "research", "sim", "mission"
                        };
                        static constexpr int kToolFamilyCount =
                            (int)(sizeof(kToolFamilies) / sizeof(kToolFamilies[0]));
                        // Static per-family tool lists (mirrors services/mcp test inventory).
                        struct FamTools { const char* fam; const char* const* tools; int n; };
                        static const char* kCadTools[] = {
                            "cad_guide","new_part","add_features","render_part","close_part",
                            "make_drawing","add_drawing_view","add_dimension","add_gdt",
                            "export_drawing","measure_part","check_interference","solve_mates"
                        };
                        static const char* kSimTools[] = {
                            "run_fea_static","run_fea_modal","run_fea_thermal","run_cfd_flow",
                            "run_cfd_accurate","simulate_motion"
                        };
                        static const char* kCamTools[] = {
                            "cam_new_job","cam_set_stock","cam_add_setup","cam_add_tool",
                            "cam_add_operation","cam_generate","cam_simulate","cam_post"
                        };
                        static const char* kEcadTools[] = {
                            "ecad_new_schematic","ecad_new_pcb","ecad_place_symbol","ecad_wire",
                            "ecad_autoroute","ecad_run_erc","ecad_run_drc","board_from_schematic"
                        };
                        static const char* kPartsTools[] = {
                            "part_search","part_get","part_place","part_source",
                            "bom_price","bom_rollup","request_quote"
                        };
                        static const char* kRevengTools[] = {
                            "reverse_engineer","compare_parts","convert_face_to_sketch",
                            "measure_from_photo","start_capture_session"
                        };
                        static const char* kPlmTools[] = {
                            "plm_init","plm_open","plm_checkpoint","plm_release",
                            "plm_history","plm_where_used","plm_rollback","plm_status"
                        };
                        static const char* kResearchTools[] = {
                            "research_web_search","research_fetch_page","research_material_lookup",
                            "research_eng_calc","fit_calc","recommend_fit"
                        };
                        static const char* kMissionTools[] = {
                            "dag_plan","dag_run","dag_status","mission_start","mission_status",
                            "buildgraph_plan","replan_request"
                        };
                        static const char* kGslTools[] = {
                            "describe_part","describe_sketch","sketch_health","render_sketch"
                        };
                        static const char* kAdvisorTools[] = {
                            "advise","stage_set","advisory_dispute"
                        };
                        static const FamTools kFamToolMap[] = {
                            {"cad", kCadTools, (int)(sizeof(kCadTools)/sizeof(kCadTools[0]))},
                            {"sim", kSimTools, (int)(sizeof(kSimTools)/sizeof(kSimTools[0]))},
                            {"cam", kCamTools, (int)(sizeof(kCamTools)/sizeof(kCamTools[0]))},
                            {"ecad", kEcadTools, (int)(sizeof(kEcadTools)/sizeof(kEcadTools[0]))},
                            {"parts", kPartsTools, (int)(sizeof(kPartsTools)/sizeof(kPartsTools[0]))},
                            {"reveng", kRevengTools, (int)(sizeof(kRevengTools)/sizeof(kRevengTools[0]))},
                            {"plm", kPlmTools, (int)(sizeof(kPlmTools)/sizeof(kPlmTools[0]))},
                            {"research", kResearchTools, (int)(sizeof(kResearchTools)/sizeof(kResearchTools[0]))},
                            {"mission", kMissionTools, (int)(sizeof(kMissionTools)/sizeof(kMissionTools[0]))},
                            {"gsl", kGslTools, (int)(sizeof(kGslTools)/sizeof(kGslTools[0]))},
                            {"advisor", kAdvisorTools, (int)(sizeof(kAdvisorTools)/sizeof(kAdvisorTools[0]))},
                        };
                        static constexpr int kFamToolMapN =
                            (int)(sizeof(kFamToolMap)/sizeof(kFamToolMap[0]));
                        const double nowTf = ImGui::GetTime();
                        if (s_toolFam.empty() || (nowTf - s_toolFamLast) > 2.0) {
                            s_toolFamLast = nowTf;
                            try {
                                std::ifstream in(configPath());
                                nlohmann::json j = nlohmann::json::object();
                                if (in) { in >> j; }
                                auto ag = j.value("agent", nlohmann::json::object());
                                auto tf = ag.value("tool_families", nlohmann::json::object());
                                for (int i = 0; i < kToolFamilyCount; ++i) {
                                    const char* k = kToolFamilies[i];
                                    bool on = true;
                                    if (tf.is_object() && tf.contains(k) && tf[k].is_boolean())
                                        on = tf[k].get<bool>();
                                    s_toolFam[k] = on;
                                }
                                auto to = ag.value("tool_options", nlohmann::json::object());
                                if (to.is_object()) {
                                    for (auto it = to.begin(); it != to.end(); ++it) {
                                        const std::string tn = it.key();
                                        const auto& e = it.value();
                                        if (!e.is_object()) continue;
                                        if (e.contains("enabled") && e["enabled"].is_boolean())
                                            s_toolEnabled[tn] = e["enabled"].get<bool>();
                                        else
                                            s_toolEnabled[tn] = true;
                                        if (e.contains("timeout_s") && e["timeout_s"].is_number_integer())
                                            s_toolTimeout[tn] = e["timeout_s"].get<int>();
                                        if (e.contains("max_calls_per_turn") && e["max_calls_per_turn"].is_number_integer())
                                            s_toolMaxCalls[tn] = e["max_calls_per_turn"].get<int>();
                                        if (e.contains("param_defaults"))
                                            s_toolDefaults[tn] = e["param_defaults"].dump(2);
                                        if (e.contains("prompt_note") && e["prompt_note"].is_string())
                                            s_toolNote[tn] = e["prompt_note"].get<std::string>();
                                    }
                                }
                            } catch (...) {
                                for (int i = 0; i < kToolFamilyCount; ++i)
                                    s_toolFam[kToolFamilies[i]] = true;
                            }
                        }
                        if (ImGui::BeginTable("##tool_fams", 3,
                                              ImGuiTableFlags_SizingStretchSame)) {
                            for (int i = 0; i < kToolFamilyCount; ++i) {
                                const char* k = kToolFamilies[i];
                                ImGui::TableNextColumn();
                                bool v = s_toolFam.count(k) ? s_toolFam[k] : true;
                                if (ImGui::Checkbox(k, &v)) {
                                    s_toolFam[k] = v;
                                    s_toolFamDirty = true;
                                }
                            }
                            ImGui::EndTable();
                        }

                        ImGui::Dummy(ImVec2(0, 8.0f * sc));
                        dimText("Per-tool options (expand a family):");
                        for (int fi = 0; fi < kFamToolMapN; ++fi) {
                            const FamTools& ft = kFamToolMap[fi];
                            if (!ImGui::TreeNode(ft.fam)) continue;
                            for (int ti = 0; ti < ft.n; ++ti) {
                                const char* tn = ft.tools[ti];
                                ImGui::PushID(tn);
                                bool en = s_toolEnabled.count(tn) ? s_toolEnabled[tn] : true;
                                if (ImGui::Checkbox("##en", &en)) {
                                    s_toolEnabled[tn] = en;
                                    s_toolOptDirty = true;
                                }
                                ImGui::SameLine();
                                ImGui::TextUnformatted(tn);
                                ImGui::SameLine();
                                if (ImGui::SmallButton("Options"))
                                    ImGui::OpenPopup("tool_opts");
                                if (ImGui::BeginPopup("tool_opts")) {
                                    ImGui::TextUnformatted(tn);
                                    ImGui::Separator();
                                    const float fieldW = 90.0f * sc;   // compact int field; label sits to its right
                                    const float boxW = 320.0f * sc;    // bounds the popup width (was -1 -> ballooned to ~1000px)
                                    int to = s_toolTimeout.count(tn) ? s_toolTimeout[tn] : 0;
                                    int mc = s_toolMaxCalls.count(tn) ? s_toolMaxCalls[tn] : 0;
                                    ImGui::SetNextItemWidth(fieldW);
                                    if (ImGui::InputInt("timeout_s (0=default)", &to)) {
                                        s_toolTimeout[tn] = std::max(0, to);
                                        s_toolOptDirty = true;
                                    }
                                    ImGui::SetNextItemWidth(fieldW);
                                    if (ImGui::InputInt("max_calls_per_turn (0=unlim)", &mc)) {
                                        s_toolMaxCalls[tn] = std::max(0, mc);
                                        s_toolOptDirty = true;
                                    }
                                    static char defBuf[2048];
                                    static char noteBuf[1024];
                                    std::snprintf(defBuf, sizeof(defBuf), "%s",
                                        s_toolDefaults.count(tn) ? s_toolDefaults[tn].c_str() : "{}");
                                    std::snprintf(noteBuf, sizeof(noteBuf), "%s",
                                        s_toolNote.count(tn) ? s_toolNote[tn].c_str() : "");
                                    ImGui::Dummy(ImVec2(0, 2.0f * sc));
                                    ImGui::TextUnformatted("param_defaults JSON");
                                    if (ImGui::InputTextMultiline("##param_defaults",
                                            defBuf, sizeof(defBuf),
                                            ImVec2(boxW, 60 * sc))) {
                                        s_toolDefaults[tn] = defBuf;
                                        s_toolOptDirty = true;
                                    }
                                    ImGui::TextUnformatted("prompt_note");
                                    if (ImGui::InputTextMultiline("##prompt_note",
                                            noteBuf, sizeof(noteBuf),
                                            ImVec2(boxW, 40 * sc))) {
                                        s_toolNote[tn] = noteBuf;
                                        s_toolOptDirty = true;
                                    }
                                    ImGui::EndPopup();
                                }
                                ImGui::PopID();
                            }
                            ImGui::TreePop();
                        }

                        if (s_toolFamDirty || s_toolOptDirty) {
                            ImGui::Dummy(ImVec2(0, 6.0f * sc));
                            if (!s_toolOptErr.empty()) {
                                ImGui::TextColored(ImVec4(1,0.4f,0.3f,1), "%s",
                                                   s_toolOptErr.c_str());
                            }
                            if (ImGui::Button("Save tool families & options",
                                              ImVec2(240.0f * sc, 0))) {
                                try {
                                    nlohmann::json j = nlohmann::json::object();
                                    {
                                        std::ifstream in(configPath());
                                        if (in) {
                                            nlohmann::json old; in >> old;
                                            if (old.is_object()) j = old;
                                        }
                                    }
                                    if (!j.contains("agent") || !j["agent"].is_object())
                                        j["agent"] = nlohmann::json::object();
                                    nlohmann::json tf = nlohmann::json::object();
                                    for (const auto& kv : s_toolFam) tf[kv.first] = kv.second;
                                    j["agent"]["tool_families"] = tf;

                                    nlohmann::json to = nlohmann::json::object();
                                    // Collect all tool names we know about
                                    std::set<std::string> names;
                                    for (const auto& kv : s_toolEnabled) names.insert(kv.first);
                                    for (const auto& kv : s_toolTimeout) names.insert(kv.first);
                                    for (const auto& kv : s_toolMaxCalls) names.insert(kv.first);
                                    for (const auto& kv : s_toolDefaults) names.insert(kv.first);
                                    for (const auto& kv : s_toolNote) names.insert(kv.first);
                                    s_toolOptErr.clear();
                                    for (const auto& tn : names) {
                                        nlohmann::json e = nlohmann::json::object();
                                        bool en = s_toolEnabled.count(tn) ? s_toolEnabled[tn] : true;
                                        if (!en) e["enabled"] = false;
                                        int tmo = s_toolTimeout.count(tn) ? s_toolTimeout[tn] : 0;
                                        if (tmo > 0) e["timeout_s"] = tmo;
                                        int mc = s_toolMaxCalls.count(tn) ? s_toolMaxCalls[tn] : 0;
                                        if (mc > 0) e["max_calls_per_turn"] = mc;
                                        if (s_toolDefaults.count(tn) && !s_toolDefaults[tn].empty()) {
                                            try {
                                                auto parsed = nlohmann::json::parse(s_toolDefaults[tn]);
                                                if (parsed.is_object() && !parsed.empty())
                                                    e["param_defaults"] = parsed;
                                            } catch (...) {
                                                s_toolOptErr = "Invalid JSON in param_defaults for " + tn;
                                                break;
                                            }
                                        }
                                        if (s_toolNote.count(tn) && !s_toolNote[tn].empty())
                                            e["prompt_note"] = s_toolNote[tn];
                                        if (!e.empty()) to[tn] = e;
                                    }
                                    if (s_toolOptErr.empty()) {
                                        j["agent"]["tool_options"] = to;
                                        std::ofstream outf(configPath(), std::ios::trunc);
                                        outf << j.dump(2);
                                        s_toolFamDirty = false;
                                        s_toolOptDirty = false;
                                        s_toolFamLast = -1e9;
                                    }
                                } catch (...) {}
                            }
                            ImGui::SameLine();
                            if (ImGui::Button("Reset all on", ImVec2(120.0f * sc, 0))) {
                                for (int i = 0; i < kToolFamilyCount; ++i)
                                    s_toolFam[kToolFamilies[i]] = true;
                                s_toolEnabled.clear();
                                s_toolTimeout.clear();
                                s_toolMaxCalls.clear();
                                s_toolDefaults.clear();
                                s_toolNote.clear();
                                s_toolFamDirty = true;
                                s_toolOptDirty = true;
                            }
                        }
                        settingsGlassSectionEnd();

                        ImGui::Dummy(ImVec2(0, 10.0f * sc));
                        settingsGlassSectionBegin("##ai_maintenance", "Maintenance");
                        dimText("Repair the local CAD engine runtime, or trim which models appear "
                                "in the composer's model picker.");
                        {
                            const bool canSetup = !st.setupRunning.load() && !st.job.running.load();
                            if (!canSetup) ImGui::BeginDisabled();
                            if (ImGui::Button("Repair / install runtime", ImVec2(220.0f * sc, 0)))
                                st.startSetup(true);
                            if (!canSetup) ImGui::EndDisabled();
                            if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                                ImGui::SetTooltip(
                                    "Rebuild the local Python/CAD runtime (same as "
                                    "Chudware -> Repair / install runtime).");
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

                    ImGui::EndTabBar();
                }
                s_aiSubJump = -1;
                settingsTabBodyEnd();
                ImGui::EndTabItem();
            }

// ---- Providers tab: OMP two-pane provider manager (legacy sections as the degrade path) ----
            if (ImGui::BeginTabItem("Providers", nullptr,
                                    (jumpToTab == 3) ? ImGuiTabItemFlags_SetSelected
                                                     : ImGuiTabItemFlags_None)) {
                settingsTabBodyBegin("##tab_providers");
                dimText("API providers, sign-in, and model routing. Credentials are stored by "
                        "the auth bridge \xe2\x80\x94 never written to config.json.");
                drawOmpProviderManager(st);
                if (!s_omp.available) {
                    // Degrade path: the agent_provider_* C-ABI is absent in this build
                    // (sibling slice mid-flight / older core) — keep the legacy sections so
                    // provider config stays possible. drawOmpProviderManager drew the banner.
                    settingsGlassSectionBegin("##prov_provider", "Provider \x26 Model");
                    ui::drawAiSettingsSection(st, busy, ui::AiSettingsSection::ProviderModel);
                    settingsGlassSectionEnd();

                    settingsGlassSectionBegin("##prov_moonshot", "Moonshot (Kimi)");
                    dimText("Configured but not the active provider. Store a key to park credit; "
                            "pick a Kimi model later when you want to use it.");
                    ImGui::TextUnformatted("API key (platform.moonshot.ai)");
                    ImGui::SetNextItemWidth(-1);
                    if (ImGui::InputText("##settings_moonshot_key", st.settings.moonshotKey,
                                         sizeof(st.settings.moonshotKey),
                                         ImGuiInputTextFlags_Password)) {
                        saveSettings(st.settings);
                    }
                    {
                        const bool have = st.settings.moonshotKey[0] != '\0';
                        ImGui::PushStyleColor(ImGuiCol_Text,
                            have ? ui::Palette::toU32(ui::palette.statusOk)
                                 : ui::Palette::toU32(ui::palette.statusWarn));
                        ImGui::TextUnformatted(have
                            ? "key set \xe2\x80\x94 Kimi models appear in the picker when enabled"
                            : "no key \xe2\x80\x94 paste a Moonshot API key when you have credit");
                        ImGui::PopStyleColor();
                    }
                    settingsGlassSectionEnd();

                    settingsGlassSectionBegin("##prov_accounts", "Accounts");
                    drawSubscriptionAccounts(st, sc);
                    settingsGlassSectionEnd();
                }

                settingsTabBodyEnd();
                ImGui::EndTabItem();
            }

            // --- end C22b marked block ---
            // Agent tab removed — run behavior lives on AI. wantSettings shot jumps to AI.
            static bool s_wantSettingsTabDone = false;
            bool wantSettingsSel = st.wantSettings && !s_wantSettingsTabDone;
            if (wantSettingsSel) s_wantSettingsTabDone = true;
            // Legacy: Agent(6)/Prompts(5)/Benchmarks(4) → AI(2). Experimental is tab 4.
            if (wantSettingsSel && jumpToTab < 0) jumpToTab = 2;
            if (jumpToTab == 4 || jumpToTab == 5 || jumpToTab == 6) {
                if (jumpToTab == 4) s_aiSubJump = 5;  // Benchmarks (after Behavior fold)
                if (jumpToTab == 5) s_aiSubJump = 2;  // Prompts
                jumpToTab = 2;
            }
            if (jumpToTab == 7) jumpToTab = 4;

            // ---- Experimental tab: research-backed opt-in AI features ----
            if (ImGui::BeginTabItem("Experimental", nullptr,
                                    (jumpToTab == 4) ? ImGuiTabItemFlags_SetSelected
                                                     : ImGuiTabItemFlags_None)) {
                settingsTabBodyBegin("##tab_experimental");
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
                settingsTabBodyEnd();
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
        ImGui::OpenPopup("About Chudware");
        st.showAbout = false;
    }
    ImGui::SetNextWindowSize(ImVec2(380.0f * sc, 0), ImGuiCond_Appearing);
    if (ImGui::BeginPopupModal("About Chudware", nullptr, ImGuiWindowFlags_NoResize)) {
        const Tex& wm = (ui::activeTheme == ui::Theme::Light && st.wordmarkDark.id)
                            ? st.wordmarkDark : st.wordmark;
        if (wm.id) {
            ImGui::Image((ImTextureID)(intptr_t)wm.id,
                         ImVec2(190.0f * sc, 190.0f * sc * (float)wm.h / wm.w));
        } else {
            sectionHeader(st.fonts.h1, "Chudware");
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
