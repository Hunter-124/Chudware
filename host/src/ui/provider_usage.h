#pragma once
// -------------------------------------------------------------------------------------------------
// Provider subscription usage — ONE poller, several consumers.
//
// `subscription_accounts` / `subscription_usage` / `subscription_status` used to be polled by
// file-statics inside settings_modal.cpp, so the only place that could show "how much of my
// SuperGrok / Claude Max window is left" was the Settings → Providers → Accounts section. The
// Copilot composer now shows the same windows as small rings next to the context gauge, and a
// second independent poller would double the RPC traffic and drift out of sync. So the model,
// the parsing and the polls live here; every consumer reads the same rows.
// Everything degrades silently: providers without a subscription (API-key, local endpoints)
// map to no subscription id, accounts without a `usage` array report `hasUsage == false`, and
// a worker that doesn't implement `subscription_usage` latches rpcMissing so the polls stop.
// Consumers MUST hide their indicator when there is nothing to show — never render 0%.
//
// There are TWO upstream stores, not one. The `subscription_*` RPCs above read the Python
// worker's account table; the Chudcode service keeps its OWN credential store, and a Claude
// Max / ZenMux / Kimi Code OAuth session signed in through Settings → Providers exists only
// there. Those windows arrive via `chudcode_auth_state` and land in the SAME UsageWindow model
// (see providerUsagePollBridge), so a consumer picks a source and draws identical rings.
// -------------------------------------------------------------------------------------------------
#include <map>
#include <string>
#include <vector>

#include "imgui.h"

struct AppState;

namespace ui {

// One rate-limit window reported by a provider (contracts/providers.md §4.5).
struct UsageWindow {
    std::string window;        // machine id, e.g. "5h", "7d"
    std::string label;         // display label; falls back to `window`
    float       usedFraction = 0.0f;   // 0..1, only meaningful when hasFraction
    bool        hasFraction = false;   // null upstream = status-only; never render it as 0%
    std::string resetsAt;      // ISO-8601 or ms-epoch decimal; usageResetCountdown() reads both
    std::string status;        // ok | warn | blocked | needs_login | unknown
};

// One signed-in subscription account.
struct UsageAccount {
    std::string provider;      // subscription provider id: xai | claude | codex | cursor | kimi
    std::string accountId;
    std::string label;         // email / human label; never the raw account id
    std::string displayName;   // optional
    std::string plan;          // optional plan chip text
    bool        active = false;     // the credential the harness currently uses
    bool        connected = true;
    bool        hasUsage = false;   // a `usage` array was present (may still be empty)
    std::vector<UsageWindow> usage;
};

// Live device-code / browser login progress for one provider.
struct UsageLoginProgress {
    bool        inProgress = false;
    bool        browserOpened = false;
    std::string userCode;
    std::string verifyUrl;
    std::string stage;
    std::string lastError;
};

// ---- polling -----------------------------------------------------------------------------------
// Accounts + status. `ttlSec` lets a visible management UI refresh briskly (1.5s) while a
// background consumer such as the composer asks for a lazy refresh (60s). `force` ignores the TTL.
void providerUsagePollAccounts(AppState& st, double ttlSec, bool force);
// Usage windows. TTL is fixed at 5 min per contracts/providers.md §4.5; `force` bypasses it.
// No-op once the worker has reported the method as unknown.
void providerUsagePollUsage(AppState& st, bool force);
// Rate-limit windows the Chudcode service reports for ONE provider id ("anthropic", "zenmux",
// "kimi-code"). Source RPC is `chudcode_auth_state`, the same call Settings → Providers makes;
// results are cached per provider id. Same 5-min TTL, in-flight guard and unknown-method latch
// as the polls above; a soft failure keeps the last good windows rather than blanking the rings.
void providerUsagePollBridge(AppState& st, const char* bridgeProviderId, bool force);
// Drop every TTL so the next poll call refetches immediately (after sign-in / switch / logout).
void providerUsageInvalidate();

// ---- reading -----------------------------------------------------------------------------------
const std::vector<UsageAccount>&           providerUsageAccounts();
std::map<std::string, UsageLoginProgress>& providerUsageLogin();
const std::string&                         providerUsageError();
void                                       providerUsageSetError(std::string err);
bool                                       providerUsageRpcMissing();

// The subscription provider id backing a CAD ProviderIndex, or nullptr when that provider is
// key-based / local and therefore has no usage windows at all. Callers hide their indicator on
// nullptr — this is the "inapplicable elements are automatically hidden" gate.
const char* subscriptionProviderForCad(int cadProvider);

// Usage windows of the ACTIVE account for a subscription provider (falls back to the first
// connected account that reports usage). nullptr when nothing applicable is signed in.
const std::vector<UsageWindow>* activeAccountUsage(const std::string& subProviderId);

// The omp bridge provider id backing a CAD ProviderIndex, or nullptr when that provider is not
// bridge-routed and so has no sidecar-side windows. Callers then fall back to
// subscriptionProviderForCad() + activeAccountUsage().
const char* bridgeProviderForCad(int cadProvider);

// Bridge-reported windows for a bridge provider id. nullptr until a poll succeeds AND reports at
// least one window — never a pointer to an empty vector, so nullptr always means "show nothing".
const std::vector<UsageWindow>* bridgeProviderUsage(const std::string& bridgeProviderId);

// ---- shared presentation -------------------------------------------------------------------
// Palette role for a window's status; unknown/empty derives from the fraction using the same
// thresholds as the composer's context meter, so bars and rings agree on when amber turns red.
const float* usageStatusColor(const std::string& status, float frac);
// "resets in 2h 14m" from a reset stamp; empty when absent or unparseable. Accepts BOTH wire
// forms in use: ISO-8601 (Python `subscription_usage`) and a decimal count of milliseconds since
// the epoch (Chudcode `chudcode_auth_state`, which sends resetsAt as a NUMBER).
std::string  usageResetCountdown(const std::string& stamp);
// Very short window caption for a compact ring ("5H", "7D") derived from window/label.
std::string  usageWindowShortLabel(const UsageWindow& w);

// Which windows earn a compact ring in the composer prelude row, in draw order.
//
// The row used to take the first `max` fraction-bearing windows in catalog order, which put a
// windowless lifetime "Total quota" ring ("TOT") beside the rolling ones ("5H", "7D") and then
// beside the context gauge: three cryptic 3-char captions and three rings of identical visual
// weight, reading as one undifferentiated cluster.
//
// A rolling window is the ACTIONABLE number — it says when you can work again. A lifetime total
// cannot be acted on, so it is shown only when there is no time-windowed row at all, never as
// filler beside one. Status-only windows (`hasFraction == false`) are never rendered, because a
// missing fraction is not 0%.
//
// Inline + pointer-returning so the composer's measure and draw passes can call it twice per
// frame and be guaranteed the same set; `src` must outlive the result.
inline std::vector<const UsageWindow*> pickUsageRings(const std::vector<UsageWindow>& src,
                                                      std::size_t max) {
    std::vector<const UsageWindow*> out;
    if (max == 0) return out;
    for (const UsageWindow& w : src) {
        if (!w.hasFraction) continue;
        if (w.window.empty()) continue;      // lifetime total: last resort only, below
        out.push_back(&w);
        if (out.size() >= max) return out;
    }
    if (out.empty()) {
        for (const UsageWindow& w : src) {
            if (!w.hasFraction) continue;
            out.push_back(&w);
            break;
        }
    }
    return out;
}

}  // namespace ui
