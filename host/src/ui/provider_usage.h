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
//
// Everything degrades silently: providers without a subscription (API-key, local endpoints)
// map to no subscription id, accounts without a `usage` array report `hasUsage == false`, and
// a worker that doesn't implement `subscription_usage` latches rpcMissing so the polls stop.
// Consumers MUST hide their indicator when there is nothing to show — never render 0%.
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
    std::string resetsAt;      // ISO-8601, may be empty
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
// Drop both TTLs so the next poll call refetches immediately (after sign-in / switch / logout).
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

// ---- shared presentation -------------------------------------------------------------------
// Palette role for a window's status; unknown/empty derives from the fraction using the same
// thresholds as the composer's context meter, so bars and rings agree on when amber turns red.
const float* usageStatusColor(const std::string& status, float frac);
// "resets in 2h 14m" from an ISO-8601 timestamp; empty when unparseable.
std::string  usageResetCountdown(const std::string& iso);
// Very short window caption for a compact ring ("5H", "7D") derived from window/label.
std::string  usageWindowShortLabel(const UsageWindow& w);

}  // namespace ui
