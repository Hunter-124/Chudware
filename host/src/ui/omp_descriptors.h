// Omp worker payload parsing — sanitized provider descriptors, streamed login events,
// per-session stats, and built-in tool state, as served by the persistent Omp worker
// (services/agent/worker_rpc.py: omp_provider_list / omp_auth_state /
// omp_login / omp_models_list / omp_session_stats).
//
// Deliberately GL/ImGui/AppState-free so host/tests/test_omp_descriptors.cpp links the
// REAL implementation rather than a copy of it (same pattern as conv_ids.h). Every parser is
// tolerant of the two field spellings the worker contract allows (e.g. label|name,
// authKinds|methods, loadMode|load) but never invents data: an entry without a usable key
// is rejected, and absent fields stay absent.
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace ui {

inline constexpr char kOmpSetKeyMethod[] = "omp_set_key";

// One sanitized provider descriptor (omp_provider_list providers[] entry, and the
// scalar half of a omp_auth_state response).
struct OmpProviderDesc {
    std::string id;            // registry id ("anthropic", "kimi-code", ...)
    std::string label;         // display name; defaults to id
    std::string state;         // signed_out|key_set|oauth_connected|error|disabled
    std::string origin;        // runtime|config|oauth|api_key|env|fallback (empty = none)
    std::string envVar;        // when origin == "env"
    std::string error;         // last authentication error text
    std::string consoleUrl;    // vendor key console deep link
    bool keyless = false;
    bool available = false;   // Runtime availability, not credential validation.
    bool availabilityKnown = false;
    bool hasApiKey = false;    // authKinds/methods contains an API-key kind
    bool hasOAuth = false;     // authKinds/methods contains "oauth"
    int modelsAvailable = 0;
};

// A failed per-provider refresh can retain an earlier confirmation, but cannot
// establish availability for an unknown provider. Explicit refusals revoke it.
static inline bool ompProviderAvailableAfterRefresh(const OmpProviderDesc& row,
                                                    bool previouslyAvailable) noexcept {
    if (row.availabilityKnown) return row.available;
    if (row.state == "signed_out" || row.state == "error" || row.state == "disabled")
        return false;
    return previouslyAvailable;
}

// Parse one providers[] entry. Returns false (out untouched) when the entry has no id —
// the caller counts those instead of silently shrinking the list. Accepts both the frozen
// spelling (label/state/authKinds) and the Omp-native one (name + authenticated/method).
bool parseOmpProviderDesc(const nlohmann::json& p, OmpProviderDesc& out);

// Whether the runtime reports configured authentication. This is not a claim that
// credentials are valid or that a model is currently available.
bool ompProviderUsable(const std::string& state);

// One streamed omp_login progress event, normalized. kind is "open_url" when the event
// carries a URL the host should open in the system browser; other kinds pass through
// verbatim (extension_ui_request, progress, ...) with their display text in message.
struct OmpLoginEvent {
    std::string kind;
    std::string url;          // for open_url
    std::string launchUrl;    // optional argv-safe opener target
    std::string message;      // instructions / progress text, when present
    std::string interaction;  // browser|device, when reported
};
// Returns false for frames that carry nothing this UI can use (no kind, no url, no text).
bool parseOmpLoginEvent(const nlohmann::json& ev, OmpLoginEvent& out);


// Provider-bound acknowledgement checks used by onboarding. A stale or cross-provider
// response cannot acknowledge a save for another provider.
bool ompKeySaveAccepted(const nlohmann::json& r, const std::string& providerId);

// omp_session_stats — authoritative per-session accounting from the Omp host.
struct OmpSessionModelRow {
    std::string model;
    std::string provider;
    double usd = 0.0;
    long long tokensIn = 0, tokensOut = 0;
    bool priced = false;
};
struct OmpSessionStats {
    bool hasCost = false;      // a dollar figure was reported (0 is a real value)
    double usd = 0.0;
    long long tokensIn = 0, tokensOut = 0;
    long long cacheRead = 0, cacheWrite = 0;
    std::vector<OmpSessionModelRow> models;
};
// Parses {ok:true, ...} into out. Returns false when the response is not an ok object
// (out untouched — the caller keeps its last good data).
bool parseOmpSessionStats(const nlohmann::json& r, OmpSessionStats& out);
// Runtime-reported tool metadata; absent availability remains unknown.
struct OmpToolRow {
    std::string name;
    std::string description;
    bool available = false;
    bool availabilityKnown = false;
    bool builtin = false;
};
bool parseOmpToolState(const nlohmann::json& result, std::vector<OmpToolRow>& out);


// Which array, if any, of a session_compact / session_shake result should REPLACE the
// visible chat transcript. Returns a pointer into `result` ("messages" first, then the
// legacy bare "kept_messages" tail) ONLY when that array is a nonempty explicit
// replacement payload; nullptr otherwise. Omp online compaction rewrites the
// service-side session and legitimately omits both keys — and a present-but-EMPTY array
// is a degenerate payload, not an instruction to wipe the visible chat.
const nlohmann::json* ompCompactReplacement(const nlohmann::json& result);

}  // namespace ui
