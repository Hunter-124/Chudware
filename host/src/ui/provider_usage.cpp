// provider_usage.cpp — see provider_usage.h. Extracted from settings_modal.cpp's file-statics so
// the Copilot composer and the Settings Accounts section share ONE poll and ONE parsed model.
#include "provider_usage.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <ctime>
#include <nlohmann/json.hpp>

#include "core/app_state.h"
#include "model_registry.h"
#include "ui_theme.h"

namespace ui {
namespace {

struct UsageState {
    std::vector<UsageAccount> rows;
    std::map<std::string, UsageLoginProgress> login;
    double lastAccountsPoll = -1e9;
    double lastUsagePoll    = -1e9;
    bool   accountsInFlight = false;
    bool   usageInFlight    = false;
    bool   rpcMissing       = false;   // unknown_method → stop polling, degrade quietly
    std::string lastError;
};
UsageState g;

// 5-minute usage refresh per contracts/providers.md §4.5.
constexpr double kUsageTtlSec = 5.0 * 60.0;

std::string jstr(const nlohmann::json& obj, const char* key) {
    auto it = obj.find(key);
    return it != obj.end() && it->is_string() ? it->get<std::string>() : std::string();
}
// Present-but-null / wrong-type fields must not throw under WASM (exceptions disabled there).
bool jbool(const nlohmann::json& obj, const char* key, bool dflt) {
    auto it = obj.find(key);
    if (it == obj.end() || it->is_null()) return dflt;
    return it->is_boolean() ? it->get<bool>() : dflt;
}
const nlohmann::json* jarr(const nlohmann::json& obj, const char* key) {
    auto it = obj.find(key);
    return (it != obj.end() && it->is_array()) ? &(*it) : nullptr;
}
nlohmann::json jobj(const nlohmann::json& obj, const char* key) {
    auto it = obj.find(key);
    return (it != obj.end() && it->is_object()) ? *it : nlohmann::json::object();
}

void parseUsageWindows(const nlohmann::json& src, UsageAccount& row) {
    row.usage.clear();
    row.hasUsage = false;
    const nlohmann::json* arr = jarr(src, "usage");
    if (!arr) return;
    row.hasUsage = true;
    for (const auto& u : *arr) {
        if (!u.is_object()) continue;
        UsageWindow w;
        w.window = jstr(u, "window");
        w.label  = jstr(u, "label");
        if (w.label.empty()) w.label = w.window.empty() ? "Usage" : w.window;
        auto uf = u.find("used_fraction");
        if (uf != u.end() && uf->is_number()) {
            w.usedFraction = (float)std::clamp(uf->get<double>(), 0.0, 1.0);
            w.hasFraction  = true;
        }
        w.resetsAt = jstr(u, "resets_at");
        w.status   = jstr(u, "status");
        if (w.status.empty()) w.status = "unknown";
        row.usage.push_back(std::move(w));
    }
}

// Identity fields from an accounts/status row. `label` holds an email or human label only —
// never the raw account id, which the display path shortens itself.
void parseAccountIdentity(const nlohmann::json& a, UsageAccount& row) {
    row.displayName = jstr(a, "display_name");
    row.plan        = jstr(a, "plan");
    row.label       = jstr(a, "email");
    if (row.label.empty()) row.label = jstr(a, "label");
    if (!row.label.empty() && row.label == row.accountId) row.label.clear();
    if (!row.displayName.empty() && row.displayName == row.accountId) row.displayName.clear();
    parseUsageWindows(a, row);
}

// Merge subscription_usage rows into the account list by (provider, account_id). The accounts
// poll stays the source of truth for membership.
void mergeUsageRows(const nlohmann::json& rows) {
    if (!rows.is_array()) return;
    for (const auto& r : rows) {
        if (!r.is_object()) continue;
        const std::string prov = jstr(r, "provider");
        const std::string aid  = jstr(r, "account_id");
        for (auto& row : g.rows) {
            if (row.provider != prov) continue;
            if (!aid.empty() && row.accountId != aid) continue;
            const std::string dn = jstr(r, "display_name");
            if (!dn.empty() && dn != row.accountId) row.displayName = dn;
            const std::string em = jstr(r, "email");
            if (!em.empty() && em != row.accountId) {
                row.label = em;
            } else {
                const std::string lb = jstr(r, "label");
                if (!lb.empty() && lb != row.accountId && row.label.empty()) row.label = lb;
            }
            const std::string pl = jstr(r, "plan");
            if (!pl.empty()) row.plan = pl;
            parseUsageWindows(r, row);
            if (aid.empty()) break;   // no account_id: first matching provider only
        }
    }
}

}  // namespace

// ---- polling -----------------------------------------------------------------------------------

void providerUsagePollUsage(AppState& st, bool force) {
    if (g.rpcMissing) return;
    const double now = ImGui::GetTime();
    if (!force && (now - g.lastUsagePoll) < kUsageTtlSec) return;
    if (g.usageInFlight) return;
    g.lastUsagePoll = now;
    g.usageInFlight = true;
    st.rpc.call("subscription_usage", nlohmann::json::object(), [](const core::RpcResult& r) {
        g.usageInFlight = false;
        if (!r.ok) {
            // unknown_method / worker down — keep the last usage; latch missing once so we stop
            // hammering and consumers stay on their "unavailable" path.
            const std::string& err = r.err;
            if (err.find("unknown_method") != std::string::npos ||
                err.find("unknown method") != std::string::npos ||
                err.find("not found") != std::string::npos) {
                g.rpcMissing = true;
            }
            return;
        }
        if (!r.result.is_object()) return;
        if (!jbool(r.result, "ok", true)) return;   // soft failure (429 / outage): keep prior
        if (const auto* rows = jarr(r.result, "rows")) mergeUsageRows(*rows);
    });
}

void providerUsagePollAccounts(AppState& st, double ttlSec, bool force) {
    const double now = ImGui::GetTime();
    if (!force && (now - g.lastAccountsPoll) < ttlSec) return;
    if (g.accountsInFlight) return;
    g.lastAccountsPoll = now;
    g.accountsInFlight = true;
    st.rpc.call("subscription_accounts", nlohmann::json::object(), [](const core::RpcResult& r) {
        g.accountsInFlight = false;
        if (!r.ok || !r.result.is_object()) {
            if (!r.ok && g.lastError.empty())
                g.lastError = r.err.empty() ? "subscription_accounts unavailable" : r.err;
            return;
        }
        if (!jbool(r.result, "ok", true)) {
            const std::string reason = jstr(r.result, "reason");
            g.lastError = reason.empty() ? "accounts failed" : reason;
            return;
        }
        g.lastError.clear();
        // Preserve prior usage while the membership list is rebuilt: the usage poll runs on its
        // own slower TTL and would otherwise blink the rings off on every accounts refresh.
        std::map<std::string, UsageAccount> prevByKey;
        for (const auto& prev : g.rows) prevByKey[prev.provider + "\n" + prev.accountId] = prev;
        g.rows.clear();
        const auto* arr = jarr(r.result, "accounts");
        if (!arr) return;
        for (const auto& a : *arr) {
            if (!a.is_object()) continue;
            UsageAccount row;
            row.provider  = jstr(a, "provider");
            row.accountId = jstr(a, "account_id");
            row.active    = jbool(a, "active", false);
            row.connected = jbool(a, "connected", true);
            parseAccountIdentity(a, row);
            if (!row.hasUsage) {
                auto it = prevByKey.find(row.provider + "\n" + row.accountId);
                if (it != prevByKey.end() && it->second.hasUsage) {
                    row.hasUsage = true;
                    row.usage    = it->second.usage;
                    if (row.displayName.empty()) row.displayName = it->second.displayName;
                    if (row.plan.empty())        row.plan        = it->second.plan;
                }
            }
            g.rows.push_back(std::move(row));
        }
    });
    // Status carries live login progress (device code, verification URL, stage).
    st.rpc.call("subscription_status", nlohmann::json::object(), [](const core::RpcResult& r) {
        if (!r.ok || !r.result.is_object()) return;
        nlohmann::json providers = jobj(r.result, "providers");
        if (!providers.is_object()) return;
        for (auto it = providers.begin(); it != providers.end(); ++it) {
            if (!it.value().is_object()) continue;
            UsageLoginProgress& p = g.login[it.key()];
            const auto& x = it.value();
            const bool wasInProgress = p.inProgress;
            p.inProgress    = jbool(x, "login_in_progress", false);
            p.browserOpened = jbool(x, "browser_opened", false);
            p.userCode      = jstr(x, "user_code");
            p.verifyUrl     = jstr(x, "verification_url");
            if (p.verifyUrl.empty()) p.verifyUrl = jstr(x, "verification_uri");
            // Prefer the complete device URL (code embedded). A bare /oauth2/device without
            // user_code bounces signed-out browsers to the generic login instead of approve.
            if (!p.userCode.empty() &&
                (p.verifyUrl.empty() || p.verifyUrl.find("user_code=") == std::string::npos)) {
                const std::string base =
                    p.verifyUrl.empty() ? "https://accounts.x.ai/oauth2/device" : p.verifyUrl;
                const char sep = (base.find('?') == std::string::npos) ? '?' : '&';
                p.verifyUrl = base + sep + "user_code=" + p.userCode;
            }
            p.stage     = jstr(x, "login_stage");
            p.lastError = jstr(x, "last_error");
            // The start RPC returns immediately; status is the completion edge.
            if (wasInProgress && !p.inProgress) providerUsageInvalidate();
        }
    });
}

void providerUsageInvalidate() {
    g.lastAccountsPoll = -1e9;
    g.lastUsagePoll    = -1e9;
}

// ---- reading -----------------------------------------------------------------------------------

const std::vector<UsageAccount>& providerUsageAccounts() { return g.rows; }
std::map<std::string, UsageLoginProgress>& providerUsageLogin() { return g.login; }
const std::string& providerUsageError() { return g.lastError; }
void providerUsageSetError(std::string err) { g.lastError = std::move(err); }
bool providerUsageRpcMissing() { return g.rpcMissing; }

const char* subscriptionProviderForCad(int cadProvider) {
    switch (cadProvider) {
        case PROV_ANTHROPIC: return "claude";
        case PROV_XAI:       return "xai";
        case PROV_CURSOR:    return "cursor";
        case PROV_KIMISUB:   return "kimi";
        default: return nullptr;   // key-based or local: no subscription windows exist
    }
}

const std::vector<UsageWindow>* activeAccountUsage(const std::string& subProviderId) {
    if (subProviderId.empty()) return nullptr;
    const std::vector<UsageWindow>* fallback = nullptr;
    for (const auto& row : g.rows) {
        if (row.provider != subProviderId) continue;
        if (!row.hasUsage || row.usage.empty()) continue;
        if (row.active) return &row.usage;
        if (!fallback && row.connected) fallback = &row.usage;
    }
    return fallback;
}

// ---- shared presentation -------------------------------------------------------------------

const float* usageStatusColor(const std::string& status, float frac) {
    if (status == "blocked") return ui::palette.statusErr;
    if (status == "warn")    return ui::palette.statusWarn;
    if (status == "ok")      return ui::palette.statusOk;
    // unknown / empty: derive from the fraction (same thresholds as the context meter).
    if (frac >= 0.95f) return ui::palette.statusErr;
    if (frac >= 0.75f) return ui::palette.statusWarn;
    return ui::palette.statusOk;
}

std::string usageResetCountdown(const std::string& iso) {
    if (iso.empty()) return {};
    int y = 0, mo = 0, d = 0, h = 0, mi = 0, s = 0;
    if (std::sscanf(iso.c_str(), "%d-%d-%dT%d:%d:%d", &y, &mo, &d, &h, &mi, &s) < 6) return {};
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
    long long delta = (long long)(target - std::time(nullptr));
    if (delta <= 0) return "resets soon";
    const long long days = delta / 86400;
    delta %= 86400;
    const long long hours = delta / 3600;
    delta %= 3600;
    const long long mins = delta / 60;
    char buf[64];
    if (days > 0)       std::snprintf(buf, sizeof(buf), "resets in %lldd %lldh", days, hours);
    else if (hours > 0) std::snprintf(buf, sizeof(buf), "resets in %lldh %lldm", hours, mins);
    else                std::snprintf(buf, sizeof(buf), "resets in %lldm", mins);
    return buf;
}

std::string usageWindowShortLabel(const UsageWindow& w) {
    // "5h" -> "5H", "7d" -> "7D". Providers that send a prose label ("Weekly limit") fall back
    // to its first token, uppercased and clipped, so a ring caption never wraps.
    const std::string& src = !w.window.empty() ? w.window : w.label;
    std::string out;
    for (char c : src) {
        if (out.size() >= 3) break;
        if (c == ' ' || c == '-' || c == '_') break;
        out.push_back((char)std::toupper((unsigned char)c));
    }
    return out.empty() ? std::string("USE") : out;
}

}  // namespace ui
