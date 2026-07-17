// AI-CAD Settings modal (Appearance / General / AI / Benchmarks / Prompts / Agent /
// Experimental tabs) and the About modal.
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <fstream>
#include <map>
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
#include "leaderboard_ui.h"  // aicad.leaderboard/1 compact table (Settings > Benchmarks)
#include "oauth_capture.h"  // in-app xAI multi-account CEF OAuth (Settings → Accounts)


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

// Sticky Settings tabs: tab bar stays put; only the body child scrolls.
static void settingsTabBodyBegin(const char* id) {
    const float sc = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10.0f * sc, 8.0f * sc));
    ImGui::BeginChild(id, ImVec2(0, 0), ImGuiChildFlags_None);
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
// ---- Subscription accounts (Settings → AI → Accounts) ----------------------
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

static const char* kSubProviders[] = {"xai", "claude", "codex", "cursor"};
static const char* kSubProviderLabels[] = {
    "xAI Grok", "Claude", "ChatGPT / Codex", "Cursor"};
// 5-minute usage poll while the Accounts section is visible (§4.5).
static constexpr double kUsagePollIntervalSec = 5.0 * 60.0;
static constexpr double kAccountsPollIntervalSec = 1.5;

static std::string optionalJsonString(const nlohmann::json& obj, const char* key) {
    auto it = obj.find(key);
    return it != obj.end() && it->is_string() ? it->get<std::string>() : std::string();
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
        if (r.result.value("ok", true) == false) {
            // Soft failure (e.g. 429 / provider outage) — leave prior usage alone.
            return;
        }
        mergeUsageRows(r.result.value("rows", nlohmann::json::array()));
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
        if (r.result.value("ok", true) == false) {
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
        auto arr = r.result.value("accounts", nlohmann::json::array());
        if (!arr.is_array()) return;
        for (const auto& a : arr) {
            if (!a.is_object()) continue;
            SubAccountRow row;
            row.provider  = optionalJsonString(a, "provider");
            row.accountId = optionalJsonString(a, "account_id");
            row.active    = a.value("active", false);
            row.connected = a.value("connected", true);
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
        auto providers = r.result.value("providers", nlohmann::json::object());
        if (!providers.is_object()) return;
        for (auto it = providers.begin(); it != providers.end(); ++it) {
            if (!it.value().is_object()) continue;
            SubLoginProgress& p = s_subAccts.login[it.key()];
            const auto& x = it.value();
            const bool wasInProgress = p.inProgress;
            p.inProgress = x.value("login_in_progress", false);
            p.browserOpened = x.value("browser_opened", false);
            p.userCode = optionalJsonString(x, "user_code");
            p.verifyUrl = optionalJsonString(x, "verification_url");
            if (p.verifyUrl.empty())
                p.verifyUrl = optionalJsonString(x, "verification_uri");
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

    for (int pi = 0; pi < 4; ++pi) {
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
                const std::string fallbackUrl =
                    prog.verifyUrl.empty()
                        ? (std::string(prov) == "xai" ? "https://accounts.x.ai/oauth2/device"
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
            char addId[64];
            std::snprintf(addId, sizeof(addId), "Add account##%s", prov);
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
            if (ImGui::IsItemHovered()) {
                if (std::string(prov) == "xai" && kXaiUseCef && ui::OauthCapture::cefAvailable())
                    ImGui::SetTooltip(
                        "Add another %s account in the embedded browser "
                        "(session isolated for multi-account).", plabel);
                else
                    ImGui::SetTooltip(
                        "Add another %s subscription account (browser OAuth).", plabel);
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

// ---- Model roles + rankings (Settings → AI) ---------------------------------
// RPC: ai_roles_get/set, model_rankings_get/set (ModelRolesAuto). Degrade read-only
// when the RPC is absent.
struct RoleAssign {
    bool isAuto = true;
    std::string provider;
    std::string model;
    // Per-role reasoning effort: "auto" | "low" | "medium" | "high" (default auto).
    std::string thinking = "auto";
};
struct ModelRankingRow {
    std::string modelId;
    std::string provider;
    int rank = 3;              // 1–5
    std::string source;        // shipped | user | measured
    std::string notes;
};
struct ModelRolesUi {
    std::map<std::string, RoleAssign> roles;
    std::vector<ModelRankingRow> rankings;
    bool rolesLoaded = false;
    bool rankingsLoaded = false;
    bool rolesRpcMissing = false;
    bool rankingsRpcMissing = false;
    bool rolesPollInFlight = false;
    bool rankingsPollInFlight = false;
    double lastRolesPoll = -1e9;
    double lastRankingsPoll = -1e9;
    std::string lastError;
    bool rolesSaveInFlight = false;
    bool rankingsSaveInFlight = false;
};
static ModelRolesUi s_roles;

static const char* kRoleKeys[] = {
    "advisor", "task", "planning", "default", "plm", "redaction"};
static const char* kRoleLabels[] = {
    "Advisor", "Task", "Planning", "Default", "PLM", "Redaction"};
static constexpr int kRoleCount = 6;

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
static const char* roleProviderWire(int prov) {
    switch (prov) {
        case ui::PROV_ANTHROPIC:   return "anthropic";
        case ui::PROV_OPENAI:      return "openai";
        case ui::PROV_LOCAL:       return "local";
        case ui::PROV_LLAMACPP:    return "llamacpp";
        case ui::PROV_GEMINI:      return "gemini";
        case ui::PROV_CLOUDCOMPAT: return "openrouter";
        case ui::PROV_XAI:         return "xai";
        case ui::PROV_CURSOR:      return "cursor";
        default: return "local";
    }
}

static int roleProviderFromWire(const std::string& w) {
    if (w == "anthropic") return ui::PROV_ANTHROPIC;
    if (w == "openai") return ui::PROV_OPENAI;
    if (w == "local") return ui::PROV_LOCAL;
    if (w == "llamacpp") return ui::PROV_LLAMACPP;
    if (w == "gemini") return ui::PROV_GEMINI;
    if (w == "openrouter" || w == "cloud" || w == "cloudcompat") return ui::PROV_CLOUDCOMPAT;
    if (w == "xai" || w == "xai-sub") return ui::PROV_XAI;
    if (w == "cursor" || w == "cursor-sub") return ui::PROV_CURSOR;
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
    auto add = [&](int prov, const std::string& model, const std::string& label) {
        if (localOnly && !providerIsLoopback(prov)) return;
        RolePickEntry e;
        e.isAuto = false;
        e.provider = prov;
        e.model = model;
        e.label = label;
        out.push_back(std::move(e));
    };
    // Built-in cloud catalog.
    for (const auto& m : ui::builtinModels()) {
        char buf[256];
        std::snprintf(buf, sizeof(buf), "%s \xc2\xb7 %s",
                      ui::providerLabel(m.provider), m.label.c_str());
        add(m.provider, m.id, buf);
    }
    // Cloud OpenRouter / DashScope / DeepSeek presets.
    for (const auto& p : ui::cloudPresets()) {
        char buf[256];
        std::snprintf(buf, sizeof(buf), "OpenRouter / Cloud \xc2\xb7 %s", p.label.c_str());
        add(ui::PROV_CLOUDCOMPAT, p.id, buf);
    }
    // Local presets + live-probed local models.
    for (const auto& p : ui::localPresets()) {
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
        auto roles = r.result.value("roles", nlohmann::json::object());
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
                    a.provider = it->value("provider", std::string());
                    a.model = it->value("model", std::string());
                    // Model auto when provider+model absent (or explicit mode=auto).
                    const std::string mode = it->value("mode", std::string());
                    a.isAuto = (mode == "auto") || (a.provider.empty() && a.model.empty());
                    // thinking: auto|low|medium|high (default auto).
                    std::string th = it->value("thinking", std::string("auto"));
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

static void pollModelRankings(AppState& st, bool force) {
    const double now = ImGui::GetTime();
    if (!force && s_roles.rankingsLoaded && (now - s_roles.lastRankingsPoll) < 5.0) return;
    if (s_roles.rankingsPollInFlight || s_roles.rankingsSaveInFlight) return;
    s_roles.lastRankingsPoll = now;
    s_roles.rankingsPollInFlight = true;
    st.rpc.call("model_rankings_get", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_roles.rankingsPollInFlight = false;
        if (!r.ok || !r.result.is_object()) {
            s_roles.rankingsRpcMissing = true;
            // Seed a read-only fallback from the builtin catalog so the table isn't empty.
            if (s_roles.rankings.empty()) {
                for (const auto& m : ui::builtinModels()) {
                    ModelRankingRow row;
                    row.modelId = m.id;
                    row.provider = roleProviderWire(m.provider);
                    row.rank = 3;
                    row.source = "shipped";
                    s_roles.rankings.push_back(std::move(row));
                }
            }
            s_roles.rankingsLoaded = true;
            return;
        }
        s_roles.rankingsRpcMissing = false;
        s_roles.rankings.clear();
        auto ranks = r.result.value("rankings", nlohmann::json::object());
        if (ranks.is_object()) {
            for (auto it = ranks.begin(); it != ranks.end(); ++it) {
                if (!it.value().is_object()) continue;
                ModelRankingRow row;
                row.modelId = it.key();
                row.rank = it.value().value("rank", 3);
                if (row.rank < 1) row.rank = 1;
                if (row.rank > 5) row.rank = 5;
                row.source = it.value().value("source", std::string("shipped"));
                row.notes = it.value().value("notes", std::string());
                row.provider = it.value().value("provider", std::string());
                s_roles.rankings.push_back(std::move(row));
            }
        }
        std::sort(s_roles.rankings.begin(), s_roles.rankings.end(),
                  [](const ModelRankingRow& a, const ModelRankingRow& b) {
                      if (a.rank != b.rank) return a.rank > b.rank;
                      return a.modelId < b.modelId;
                  });
        s_roles.rankingsLoaded = true;
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
                    a.provider = it->value("provider", std::string());
                    a.model = it->value("model", std::string());
                    const std::string mode = it->value("mode", std::string());
                    a.isAuto = (mode == "auto") || (a.provider.empty() && a.model.empty());
                    std::string th = it->value("thinking", std::string("auto"));
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

static void setModelRank(AppState& st, const std::string& modelId, int rank) {
    if (s_roles.rankingsRpcMissing) return;
    if (rank < 1) rank = 1;
    if (rank > 5) rank = 5;
    for (auto& row : s_roles.rankings) {
        if (row.modelId == modelId) {
            row.rank = rank;
            row.source = "user";
            break;
        }
    }
    nlohmann::json p;
    p["rankings"] = nlohmann::json::object();
    p["rankings"][modelId] = nlohmann::json{{"rank", rank}};
    s_roles.rankingsSaveInFlight = true;
    st.rpc.call("model_rankings_set", p, [](const core::RpcResult& r) {
        s_roles.rankingsSaveInFlight = false;
        if (!r.ok) {
            s_roles.lastError = r.err.empty() ? "model_rankings_set failed" : r.err;
            return;
        }
        s_roles.lastRankingsPoll = -1e9;  // refresh
        s_roles.rankingsLoaded = false;
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
    dimText("One row per tool family. Run queues the gym/suite for that category; status "
            "polls via bench_status. History comes from the last cycle's detail_path.");

    if (!s_cats.loaded && s_cats.pollInFlight) {
        dimText("Loading categories\xe2\x80\xa6");
        return;
    }
    if (s_cats.rpcMissing || (!s_cats.loaded && !st.workerStarted)) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
        ImGui::TextWrapped("Categories unavailable (worker not ready or bench_categories RPC "
                           "not registered yet). The legacy runner below still works.");
        ImGui::PopStyleColor();
        if (!s_cats.lastError.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
            ImGui::TextWrapped("%s", s_cats.lastError.c_str());
            ImGui::PopStyleColor();
        }
        return;
    }
    if (s_cats.cats.empty()) {
        dimText("No categories reported.");
        return;
    }

    if (ImGui::BeginTable("##bench_cats", 4,
                          ImGuiTableFlags_BordersInnerV | ImGuiTableFlags_RowBg |
                              ImGuiTableFlags_SizingStretchProp)) {
        ImGui::TableSetupColumn("Category", ImGuiTableColumnFlags_WidthStretch, 2.4f);
        ImGui::TableSetupColumn("Tools", ImGuiTableColumnFlags_WidthStretch, 0.7f);
        ImGui::TableSetupColumn("Last result", ImGuiTableColumnFlags_WidthStretch, 2.2f);
        ImGui::TableSetupColumn("Run", ImGuiTableColumnFlags_WidthStretch, 0.8f);
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
                if (cat.last.passRate >= 0.f) {
                    const float pctW = 40.0f * sc;
                    const float barW = std::max(40.0f * sc,
                        ImGui::GetContentRegionAvail().x - pctW - 8.0f * sc);
                    drawPassRateBar(cat.last.passRate, barW, 8.0f * sc);
                    ImGui::SameLine(0, 6.0f * sc);
                    char pct[32];
                    std::snprintf(pct, sizeof(pct), "%.0f%%", cat.last.passRate * 100.f);
                    ImGui::TextUnformatted(pct);
                } else {
                    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.accent));
                    ImGui::TextUnformatted("ran");
                    ImGui::PopStyleColor();
                }
                if (!cat.last.when.empty()) {
                    ImGui::SameLine(0, 8.0f * sc);
                    dimText(cat.last.when.c_str());
                }
                if (!cat.last.detailPath.empty() && ImGui::IsItemHovered())
                    ImGui::SetTooltip("detail: %s", cat.last.detailPath.c_str());
                // History: show detail_path as a collapsible one-liner.
                if (!cat.last.detailPath.empty()) {
                    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
                    ImGui::TextWrapped("%s", cat.last.detailPath.c_str());
                    ImGui::PopStyleColor();
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
    pollModelRankings(st, /*force=*/false);
    ensureDefaultRoles();

    dimText("Per-role model + thinking effort. Auto model picks from rankings within enabled "
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
            if (isRedaction) {
                ImGui::SameLine();
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
                ImGui::TextUnformatted("[local]");
                ImGui::PopStyleColor();
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip("Redaction must use a loopback (local) provider. "
                                      "Cloud providers are refused.");
            }

            ImGui::TableNextColumn();
            // Resolve current selection index.
            int sel = 0;  // Auto
            if (!a.isAuto) {
                const int wantProv = roleProviderFromWire(a.provider);
                for (int pi = 1; pi < (int)picks.size(); ++pi) {
                    if (picks[pi].model == a.model &&
                        (wantProv < 0 || picks[pi].provider == wantProv ||
                         roleProviderWire(picks[pi].provider) == a.provider)) {
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
            if (ImGui::BeginCombo("##role_pick", preview.c_str())) {
                for (int pi = 0; pi < (int)picks.size(); ++pi) {
                    const bool selected = (pi == sel) || (pi == 0 && a.isAuto);
                    if (ImGui::Selectable(picks[pi].label.c_str(), selected)) {
                        RoleAssign next = a;  // preserve thinking
                        if (picks[pi].isAuto) {
                            next.isAuto = true;
                            next.provider.clear();
                            next.model.clear();
                        } else {
                            next.isAuto = false;
                            next.provider = roleProviderWire(picks[pi].provider);
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

            if (isRedaction) {
                dimText("Local only \xe2\x80\x94 ITAR redaction stage must not leave the machine.");
            }

            ImGui::PopID();
        }
        ImGui::EndTable();
    }

    ImGui::Dummy(ImVec2(0, 8.0f * sc));
    ImGui::TextUnformatted("Model rankings");
    dimText("1-5 stars. User overrides persist via model_rankings_set; shipped / "
            "measured badges come from the rankings file and live bench cycles.");

    if (s_roles.rankingsRpcMissing) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextWrapped("model_rankings_* RPC not available \xe2\x80\x94 table is read-only.");
        ImGui::PopStyleColor();
    }

    if (!s_roles.rankingsLoaded && s_roles.rankingsPollInFlight) {
        dimText("Loading rankings\xe2\x80\xa6");
        return;
    }
    if (s_roles.rankings.empty()) {
        dimText("No rankings yet.");
        return;
    }

    if (ImGui::BeginTable("##model_ranks", 4,
                          ImGuiTableFlags_BordersInnerV | ImGuiTableFlags_RowBg |
                              ImGuiTableFlags_SizingStretchProp |
                              ImGuiTableFlags_ScrollY,
                          ImVec2(0, 220.0f * sc))) {
        ImGui::TableSetupColumn("Model", ImGuiTableColumnFlags_WidthStretch, 2.4f);
        ImGui::TableSetupColumn("Provider", ImGuiTableColumnFlags_WidthStretch, 1.0f);
        ImGui::TableSetupColumn("Rank", ImGuiTableColumnFlags_WidthStretch, 1.6f);
        ImGui::TableSetupColumn("Source", ImGuiTableColumnFlags_WidthStretch, 0.9f);
        ImGui::TableHeadersRow();

        for (size_t i = 0; i < s_roles.rankings.size(); ++i) {
            auto& row = s_roles.rankings[i];
            ImGui::TableNextRow();
            ImGui::PushID((int)i);

            ImGui::TableNextColumn();
            ImGui::TextUnformatted(row.modelId.c_str());
            if (!row.notes.empty() && ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", row.notes.c_str());

            ImGui::TableNextColumn();
            ImGui::TextUnformatted(row.provider.empty() ? "\xe2\x80\x94" : row.provider.c_str());

            ImGui::TableNextColumn();
            {
                // Star buttons 1–5.
                const bool ro = s_roles.rankingsRpcMissing || s_roles.rankingsSaveInFlight;
                if (ro) ImGui::BeginDisabled();
                for (int s = 1; s <= 5; ++s) {
                    if (s > 1) ImGui::SameLine(0, 2.0f * sc);
                    const bool on = s <= row.rank;
                    if (on)
                        ImGui::PushStyleColor(ImGuiCol_Text,
                            ui::Palette::toU32(ui::palette.accent));
                    else
                        ImGui::PushStyleColor(ImGuiCol_Text,
                            ui::Palette::toU32(ui::palette.textDim));
                    char starId[32];
                    std::snprintf(starId, sizeof(starId), "%s##s%d",
                                  on ? "\xe2\x98\x85" : "\xe2\x98\x86", s);  // ★ / ☆
                    if (ImGui::SmallButton(starId))
                        setModelRank(st, row.modelId, s);
                    ImGui::PopStyleColor();
                }
                if (ro) ImGui::EndDisabled();
            }

            ImGui::TableNextColumn();
            {
                const char* src = row.source.c_str();
                ImU32 col = ui::Palette::toU32(ui::palette.textDim);
                if (row.source == "user")
                    col = ui::Palette::toU32(ui::palette.accent);
                else if (row.source == "measured")
                    col = ui::Palette::toU32(ui::palette.statusOk);
                else if (row.source == "shipped")
                    col = ui::Palette::toU32(ui::palette.textDim);
                ImGui::PushStyleColor(ImGuiCol_Text, col);
                ImGui::TextUnformatted(src[0] ? src : "shipped");
                ImGui::PopStyleColor();
            }

            ImGui::PopID();
        }
        ImGui::EndTable();
    }
}

}  // anonymous namespace

// ---- kSettingsEntries: unified command search table (command_search.cpp) ----
// Tab indices: 0=Appearance 1=General 2=AI 3=Benchmarks 4=Prompts 5=Agent 6=Experimental
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
    { "Model roles",              "AI",           2 },
    { "Model rankings",           "AI",           2 },
    { "Hidden models",            "AI",           2 },
    // Benchmarks (tab 3)
    { "Benchmarks",               "Benchmarks",   3 },
    { "Model leaderboard",        "Benchmarks",   3 },
    { "Run benchmark",            "Benchmarks",   3 },
    { "Benchmark",                "Benchmarks",   3 },
    { "Benchmark categories",     "Benchmarks",   3 },
    { "Category run",             "Benchmarks",   3 },
    // Prompts (tab 4)
    { "Role prompts",             "Prompts",      4 },
    { "Agent prompts",            "Prompts",      4 },
    { "Prompt editor",            "Prompts",      4 },
    { "System prompt",            "Prompts",      4 },
    { "System prompt override",   "Prompts",      4 },
    // Agent (tab 5)
    { "Plan before building",     "Agent",        5 },
    { "Plan mode",                "Agent",        5 },
    { "Refine-pass cap",          "Agent",        5 },
    { "Max iters",                "Agent",        5 },
    { "Iteration budget",         "Agent",        5 },
    // Experimental (tab 6)
    { "Vision review model",      "Experimental", 6 },
    { "Model routing",            "Experimental", 6 },
    { "Seek-CAD split",           "Experimental", 6 },
    { "Parametric sequence mode", "Experimental", 6 },
    { "CAD sequence mode",        "Experimental", 6 },
    { "Feature sequence",         "Experimental", 6 },
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
                settingsTabBodyEnd();
                ImGui::EndTabItem();
            }
            if (ImGui::BeginTabItem("AI", nullptr,
                                    (jumpToTab == 2) ? ImGuiTabItemFlags_SetSelected
                                                     : ImGuiTabItemFlags_None)) {
                settingsTabBodyBegin("##tab_ai");
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

                settingsGlassSectionBegin("##ai_benchmarks_link", "Benchmarks");
                dimText("Leaderboard, category runs, and live status live on the Benchmarks tab.");
                if (ImGui::SmallButton("Open Benchmarks tab")) {
                    // Re-arm openSettingsTab so the next frame selects Benchmarks (index 3).
                    st.openSettingsTab = 3;
                }
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip("Jump to Settings \xe2\x86\x92 Benchmarks");
                settingsGlassSectionEnd();

                settingsGlassSectionBegin("##ai_model_roles", "Model roles");
                drawModelRolesSection(st, sc);
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

                settingsTabBodyEnd();
                ImGui::EndTabItem();
            }
            // ---- Benchmarks tab: categories + leaderboard + legacy runner ----
            if (ImGui::BeginTabItem("Benchmarks", nullptr,
                                    (jumpToTab == 3) ? ImGuiTabItemFlags_SetSelected
                                                     : ImGuiTabItemFlags_None)) {
                settingsTabBodyBegin("##tab_benchmarks");
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

                settingsTabBodyEnd();
                ImGui::EndTabItem();
            }

            // --- C22b: Prompts tab (role prompt editor + system-prompt override) ---
            if (ImGui::BeginTabItem("Prompts", nullptr,
                                    (jumpToPrompts || jumpToTab == 4) ? ImGuiTabItemFlags_SetSelected
                                                                      : ImGuiTabItemFlags_None)) {
                settingsTabBodyBegin("##tab_prompts");
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
                settingsTabBodyEnd();
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
                                    (wantSettingsSel || jumpToTab == 5) ? ImGuiTabItemFlags_SetSelected
                                                                        : ImGuiTabItemFlags_None)) {
                settingsTabBodyBegin("##tab_agent");
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
                settingsSectionHeader("Harness organization");
                dimText("The composer mode picks the org structure: Agent auto-routes; "
                        "Plan drafts first; Swarm forces a multi-agent mission; Eng. Firm runs "
                        "the heaviest firm structure (specialist roles + contractor firms that "
                        "bid on tasks in predicted tokens against a $ budget); Ask is read-only.");
                {
                    const float labelW = 200.0f * sc;
                    ImGui::AlignTextToFramePadding();
                    ImGui::TextUnformatted("Eng. Firm budget (USD)");
                    ImGui::SameLine(labelW);
                    ImGui::SetNextItemWidth(160.0f * sc);
                    double b = settings.companyBudgetUsd;
                    if (ImGui::InputDouble("##engfirmbudget", &b, 5.0, 25.0, "$%.2f")) {
                        if (b < 0.0) b = 0.0;
                        settings.companyBudgetUsd = b;
                    }
                }
                dimText("Max spend the firm must complete the job within; contractor firms "
                        "bid in predicted tokens (converted to $) under this ceiling.");
                settingsTabBodyEnd();
                ImGui::EndTabItem();
            }

            // ---- Experimental tab: research-backed opt-in AI features ----
            if (ImGui::BeginTabItem("Experimental", nullptr,
                                    (jumpToTab == 6) ? ImGuiTabItemFlags_SetSelected
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
