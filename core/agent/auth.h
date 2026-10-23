// core/agent/auth.h — subscription token store + pre-run refresh.
//
// C++ port of the Python subscription-auth layer (services/agent/providers/
// sub_auth.py + subscription_login.py ensure_fresh). The C++ agent used to ask
// Python (worker RPC subscription_ensure_fresh) to refresh a stale OAuth token
// before every run; it now refreshes IN-PROCESS against the SAME config.json
// "subscription_auth" store, using the same on-disk shapes, the same
// <config.json>.lock advisory file lock (flock / LockFileEx), and the same
// atomic tmp+replace write-back — so Python (which keeps read/write access to
// the store) and C++ refreshes cannot race. xAI rotates refresh
// tokens on EVERY exchange: refresh re-reads the credential under the lock
// before POSTing and writes the rotated token back atomically in the same
// critical section.
#pragma once

#include <functional>
#include <string>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

#include "agent/http.h"

namespace chudware {
namespace agent {
namespace auth {

using nlohmann::json;

// Contract-shaped result, mirrors subscription_login.ensure_fresh:
//   {ok, refreshed, expires_in, error} — expires_in_s is -1 when unknown.
struct EnsureFreshResult {
    bool ok = false;
    bool refreshed = false;
    long expires_in_s = -1;
    std::string error;  // set when !ok
};

// True when `name` (wire/config provider id: cursor/codex/kimi/xai/claude and
// the *-sub / anthropic aliases) maps to a subscription-auth provider.
bool is_subscription_name(const std::string& name);

// Refresh the active subscription token when expired or expiring within the
// 5-minute ensure-fresh skew (subscription_login._ENSURE_FRESH_SKEW_MS).
// Multi-account: rotates to a live sibling account when the active account's
// refresh is definitively dead (invalid_grant / revoked). Never throws.
EnsureFreshResult ensure_fresh(const std::string& provider);

// Pre-run seam for resolve_provider: when the caller's cfg snapshot carries an
// expired/expiring subscription credential for `name`, run ensure_fresh() and
// replace cfg_inout with the reloaded on-disk config (so the descriptor picks
// up the rotated access token). Returns "" on success / no-op; a non-empty
// string is a hard auth-failure message to surface as a provider refusal
// (mirrors the old jobs.cpp provider_auth pre-run gate).
std::string ensure_fresh_for_run(const std::string& name, json& cfg_inout);

// Load one credential from the store (multi-account normalized; legacy flat
// entries read as account "default"). Empty account_id = the ACTIVE account.
// Returns json() (null) when absent. Read-only.
json load_credential(const std::string& cred_id, const std::string& account_id = "");

// True when the credential is expired within skew_ms (sub_auth.is_expired).
bool is_expired(const json& cred, long long skew_ms = 0);

// Test seam: replace the HTTP POST transport used by token refresh. Passing
// nullptr restores the real http_post. Not thread-safe to SET; install once
// before exercising ensure_fresh.
using HttpPostFn = std::function<HttpResponse(
    const std::string& url,
    const std::vector<std::pair<std::string, std::string>>& headers,
    const std::string& body, int timeout_ms)>;
void set_http_post_for_testing(HttpPostFn fn);

}  // namespace auth
}  // namespace agent
}  // namespace chudware
