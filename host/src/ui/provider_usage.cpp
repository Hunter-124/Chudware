// provider_usage.cpp — see provider_usage.h. Extracted from settings_modal.cpp's file-statics so
// the Copilot composer and the Settings Accounts section share ONE poll and ONE parsed model.
#include "provider_usage.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <nlohmann/json.hpp>

#include "core/app_state.h"
#include "model_registry.h"
#include "ui_theme.h"
#include "web_capabilities.h"  // ui::webBuild — subscription_* is admin-only over the web bridge

namespace ui {
namespace {

// One bridge provider's cached windows. Keyed by bridge provider id because the composer and the
// Settings Providers tab can be looking at different providers, and neither may blank the other.
struct BridgeUsage {
    std::vector<UsageWindow> windows;
    double lastPoll = -1e9;
    bool   inFlight = false;
};

struct UsageState {
    std::vector<UsageAccount> rows;
    std::map<std::string, UsageLoginProgress> login;
    double lastAccountsPoll = -1e9;
    double lastUsagePoll    = -1e9;
    double lastStatusPoll   = -1e9;
    double interactiveSince = -1.0;
    bool   accountsInFlight = false;
    bool   usageInFlight    = false;
    bool   statusInFlight   = false;
    bool   rpcMissing       = false;   // unknown_method → stop polling, degrade quietly
    std::map<std::string, BridgeUsage> bridge;   // Chudcode service windows, by provider id
    bool   bridgeRpcMissing = false;   // chudcode_auth_state absent → stop asking the service
    std::string lastError;
};
UsageState g;

// 5-minute usage refresh per contracts/providers.md §4.5.
constexpr double kUsageTtlSec = 5.0 * 60.0;

bool startupPollReady(const AppState& st) {
    const double now = ImGui::GetTime();
    if (!st.workerStarted || st.workerBootPending || !st.startupPhase.empty()) {
        g.interactiveSince = -1.0;
        return false;
    }
    if (g.interactiveSince < 0.0) g.interactiveSince = now;
    return now - g.interactiveSince >= 1.5;
}

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
// A reset timestamp may be ISO-8601 or a numeric epoch in milliseconds. Keep the wire value
// verbatim; usageResetCountdown() handles both forms.
std::string jstamp(const nlohmann::json& obj, const char* key) {
    auto it = obj.find(key);
    if (it == obj.end()) return {};
    if (it->is_string()) return it->get<std::string>();
    if (it->is_number()) return std::to_string((long long)it->get<double>());
    return {};
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
        w.resetsAt = jstamp(u, "resets_at");
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

// `chudcode_auth_state`'s `usage.windows[]` → the same UsageWindow rows the subscription path
// produces, so ONE renderer serves both sources. The service differs only in spelling:
// camelCase keys, a "<provider>:<window>" id, and a numeric resetsAt.
void parseBridgeWindows(const nlohmann::json& wins, std::vector<UsageWindow>& out) {
    out.clear();
    for (const auto& w : wins) {
        if (!w.is_object()) continue;
        UsageWindow uw;
        // The shared model's `window` is the bare machine id ("5h"), which is what the compact
        // ring caption derives from; the bridge prefixes it with the provider ("anthropic:5h").
        uw.window = jstr(w, "id");
        const std::size_t colon = uw.window.rfind(':');
        if (colon != std::string::npos) uw.window.erase(0, colon + 1);
        // A bare numeric id (kimi-code sends "<provider>:<index>" -> "0", "1") is a row index,
        // not a window — the real window length only survives in the window label ("5h limit"),
        // which is what the compact ring caption derives from.
        const bool numericId = !uw.window.empty() &&
            uw.window.find_first_not_of("0123456789") == std::string::npos;
        if (uw.window.empty() || numericId) {
            const std::string wl = jstr(w, "windowLabel");
            if (!wl.empty()) uw.window = wl;
        }
        if (uw.window.empty()) uw.window = jstr(w, "windowLabel");
        uw.label = jstr(w, "label");
        if (uw.label.empty()) uw.label = jstr(w, "windowLabel");
        if (uw.label.empty()) uw.label = uw.window.empty() ? "Usage" : uw.window;
        auto uf = w.find("usedFraction");
        if (uf != w.end() && uf->is_number()) {
            uw.usedFraction = (float)std::clamp(uf->get<double>(), 0.0, 1.0);
            uw.hasFraction  = true;
        }
        uw.resetsAt = jstamp(w, "resetsAt");
        uw.status   = jstr(w, "status");
        if (uw.status.empty()) uw.status = "unknown";
        out.push_back(std::move(uw));
    }
}

}  // namespace

// ---- polling -----------------------------------------------------------------------------------

// Subscription accounts and usage describe the OPERATOR's subscriptions: on web there is one
// shared engine process and the browser build has no provider credential path, so the web client
// never polls them. The surfaces that consume this model already render their "unavailable" path
// on an empty model.
void providerUsagePollUsage(AppState& st, bool force) {
    if (g.rpcMissing || webBuild()) return;
    if (!force && !startupPollReady(st)) return;
    const double now = ImGui::GetTime();
    if (!force && (now - g.lastUsagePoll) < kUsageTtlSec) return;
    if (g.usageInFlight) return;
    g.lastUsagePoll = now;
    g.usageInFlight = true;
    st.rpc.call("subscription_usage", nlohmann::json::object(), [](const core::RpcResult& r) {
        g.usageInFlight = false;
        g.lastUsagePoll = ImGui::GetTime();
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

void providerUsagePollBridge(AppState& st, const char* bridgeProviderId, bool force) {
    if (g.bridgeRpcMissing || !bridgeProviderId || !*bridgeProviderId) return;
    if (!force && !startupPollReady(st)) return;
    const std::string id = bridgeProviderId;
    BridgeUsage& slot = g.bridge[id];
    const double now = ImGui::GetTime();
    if (!force && (now - slot.lastPoll) < kUsageTtlSec) return;
    if (slot.inFlight) return;
    slot.lastPoll = now;
    slot.inFlight = true;
    st.rpc.call("chudcode_auth_state", nlohmann::json{{"providerId", id}},
                [id](const core::RpcResult& r) {
        BridgeUsage& s = g.bridge[id];   // std::map: the slot taken above is still this one
        s.inFlight = false;
        s.lastPoll = ImGui::GetTime();
        if (!r.ok) {
            // Same latch as subscription_usage: an absent method stops the polling for good,
            // a transport blip just keeps the windows we already have.
            const std::string& err = r.err;
            if (err.find("unknown_method") != std::string::npos ||
                err.find("unknown method") != std::string::npos ||
                err.find("not found") != std::string::npos) {
                g.bridgeRpcMissing = true;
            }
            return;
        }
        if (!r.result.is_object()) return;
        if (!jbool(r.result, "ok", true)) return;   // signed out / upstream 429: keep prior
        const nlohmann::json usage = jobj(r.result, "usage");
        const auto* wins = jarr(usage, "windows");
        if (!wins) return;   // no usage block, or notes only — keep the last good windows
        parseBridgeWindows(*wins, s.windows);
    }, 15000);   // auth_state may fetch the usage report upstream (bounded worker-side)
}

void providerUsagePollAccounts(AppState& st, double ttlSec, bool force) {
    if (webBuild()) return;  // operator-owned; the web client has no provider credential path
    if (!force && !startupPollReady(st)) return;
    providerUsagePollStatus(st, ttlSec, force);
    const double now = ImGui::GetTime();
    if (!force && (now - g.lastAccountsPoll) < ttlSec) return;
    if (g.accountsInFlight) return;
    g.lastAccountsPoll = now;
    g.accountsInFlight = true;
    st.rpc.call("subscription_accounts", nlohmann::json::object(), [](const core::RpcResult& r) {
        g.accountsInFlight = false;
        g.lastAccountsPoll = ImGui::GetTime();
        // hostd's worker_lane_busy (an agent turn holds the agent worker lane) is transient
        // and internal: keep the rings as they are and say nothing — the next poll after the
        // turn succeeds.
        if (core::workerLaneBusy(r)) return;
        if (!r.ok || !r.result.is_object()) {
            if (!r.ok && g.lastError.empty())
                g.lastError = r.err.empty() ? "subscription_accounts unavailable" : r.err;
            return;
        }
        // Any other method error carries no account list either: keep the rows shown.
        if (r.result.contains("code")) {
            const std::string msg = jstr(r.result, "message");
            g.lastError = msg.empty() ? "accounts failed" : msg;
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
}

void providerUsagePollStatus(AppState& st, double ttlSec, bool force) {
    if (webBuild()) return;
    if (!force && !startupPollReady(st)) return;
    const double now = ImGui::GetTime();
    if (g.statusInFlight || (!force && now - g.lastStatusPoll < ttlSec)) return;
    g.lastStatusPoll = now;
    g.statusInFlight = true;
    st.rpc.call("subscription_status", nlohmann::json::object(), [](const core::RpcResult& r) {
        g.statusInFlight = false;
        g.lastStatusPoll = ImGui::GetTime();
        if (!r.ok || !r.result.is_object()) return;
        nlohmann::json providers = jobj(r.result, "providers");
        if (!providers.is_object()) return;
        for (auto it = providers.begin(); it != providers.end(); ++it) {
            if (!it.value().is_object()) continue;
            UsageLoginProgress& p = g.login[it.key()];
            const auto& x = it.value();
            p.connected     = jbool(x, "connected", false);
            p.email         = jstr(x, "email");
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
    g.lastStatusPoll   = -1e9;
    for (auto& kv : g.bridge) kv.second.lastPoll = -1e9;
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

const char* bridgeProviderForCad(int cadProvider) {
    switch (cadProvider) {
        case PROV_ANTHROPIC: return "anthropic";
        case PROV_ZENMUX:    return "zenmux";
        case PROV_KIMISUB:   return "kimi-code";
        default: return nullptr;   // not bridge-routed: the subscription path serves it
    }
}

const std::vector<UsageWindow>* bridgeProviderUsage(const std::string& bridgeProviderId) {
    if (bridgeProviderId.empty()) return nullptr;
    auto it = g.bridge.find(bridgeProviderId);
    if (it == g.bridge.end() || it->second.windows.empty()) return nullptr;
    return &it->second.windows;
}

// ---- shared presentation -------------------------------------------------------------------

const float* usageStatusColor(const std::string& status, float frac) {
    if (status == "blocked") return ui::palette.statusErr;
    if (status == "warn")    return ui::palette.statusWarn;
    if (status == "ok")      return ui::palette.statusOk;
    // unknown / empty: derive from the fraction — the chud-ui-shared-v1 usage thresholds
    // (ui::kUsageWarnFrac / kUsageCritFrac), same ladder as the context meter.
    if (frac >= ui::kUsageCritFrac) return ui::palette.statusErr;
    if (frac >= ui::kUsageWarnFrac) return ui::palette.statusWarn;
    return ui::palette.statusOk;
}

std::string usageResetCountdown(const std::string& stamp) {
    if (stamp.empty()) return {};
    long long delta = 0;
    if (stamp.find_first_not_of("0123456789") == std::string::npos) {
        // Numeric form: milliseconds since the epoch.
        const long long ms = std::strtoll(stamp.c_str(), nullptr, 10);
        if (ms <= 0) return {};
        delta = ms / 1000 - (long long)std::time(nullptr);
    } else {
        int y = 0, mo = 0, d = 0, h = 0, mi = 0, s = 0;
        if (std::sscanf(stamp.c_str(), "%d-%d-%dT%d:%d:%d", &y, &mo, &d, &h, &mi, &s) < 6)
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
        delta = (long long)(target - std::time(nullptr));
    }
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
    // "5h" -> "5H", "7d" -> "7D". Filler words ("usage", "window", "limit") are skipped so a
    // label-derived window ("5h limit") still renders "5H", and a windowless row ("Usage
    // window") defers to the entry label ("Total quota" -> "TOT"). Capped at 3 chars so a
    // ring caption never wraps.
    auto firstRealToken = [](const std::string& src) -> std::string {
        std::string out;
        size_t pos = 0;
        while (pos < src.size() && out.empty()) {
            const size_t end = src.find_first_of(" \t-_", pos);
            const std::string tok = src.substr(pos, end == std::string::npos ? end : end - pos);
            pos = end == std::string::npos ? src.size() : end + 1;
            std::string low = tok;
            for (char& c : low) c = (char)std::tolower((unsigned char)c);
            if (low == "usage" || low == "window" || low == "limit") continue;
            for (char c : tok) {
                if (out.size() >= 3) break;
                out.push_back((char)std::toupper((unsigned char)c));
            }
        }
        return out;
    };
    std::string out = firstRealToken(!w.window.empty() ? w.window : w.label);
    if (out.empty()) out = firstRealToken(w.label);
    return out.empty() ? std::string("USE") : out;
}

}  // namespace ui
