// Chudcode worker payload parsing — sanitized provider descriptors, streamed login events,
// per-session stats, and built-in tool state, as served by the persistent Chudcode worker
// (services/agent/worker_rpc.py: chudcode_provider_list / chudcode_auth_state /
// chudcode_login / chudcode_models_list / chudcode_session_stats / chudcode_tool_state).
//
// Deliberately GL/ImGui/AppState-free so host/tests/test_chudcode_descriptors.cpp links the
// REAL implementation rather than a copy of it (same pattern as conv_ids.h). Every parser is
// tolerant of the two field spellings the worker contract allows (e.g. label|name,
// authKinds|methods, loadMode|load) but never invents data: an entry without a usable key
// is rejected, and absent fields stay absent.
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace ui {

inline constexpr char kChudcodeSttStatusMethod[] = "chudcode_stt_status";
inline constexpr char kChudcodeSttStartMethod[] = "chudcode_stt_start";
inline constexpr char kChudcodeSttStopMethod[] = "chudcode_stt_stop";
inline constexpr char kChudcodeSttCancelMethod[] = "chudcode_stt_cancel";
inline constexpr char kChudcodeValidateKeyMethod[] = "chudcode_validate_key";
inline constexpr char kChudcodeSetKeyMethod[] = "chudcode_set_key";

// One sanitized provider descriptor (chudcode_provider_list providers[] entry, and the
// scalar half of a chudcode_auth_state response).
struct ChudcodeProviderDesc {
    std::string id;            // registry id ("anthropic", "kimi-code", ...)
    std::string label;         // display name; defaults to id
    std::string state;         // signed_out|key_set|oauth_connected|error|disabled
    std::string origin;        // runtime|config|oauth|api_key|env|fallback (empty = none)
    std::string envVar;        // when origin == "env"
    std::string error;         // last validate/login error text
    std::string consoleUrl;    // vendor key console deep link
    bool keyless = false;
    bool hasApiKey = false;    // authKinds/methods contains an API-key kind
    bool hasOAuth = false;     // authKinds/methods contains "oauth"
    int modelsAvailable = 0;
};

// Parse one providers[] entry. Returns false (out untouched) when the entry has no id —
// the caller counts those instead of silently shrinking the list. Accepts both the frozen
// spelling (label/state/authKinds) and the Chudcode-native one (name + authenticated/method).
bool parseChudcodeProviderDesc(const nlohmann::json& p, ChudcodeProviderDesc& out);

// Whether a provider state means "requests can succeed" (whitelist — never
// "anything but signed_out"; `error`/`disabled` credentials fail every request).
bool chudcodeProviderUsable(const std::string& state);

// One streamed chudcode_login progress event, normalized. kind is "open_url" when the event
// carries a URL the host should open in the system browser; other kinds pass through
// verbatim (extension_ui_request, progress, ...) with their display text in message.
struct ChudcodeLoginEvent {
    std::string kind;
    std::string url;          // for open_url
    std::string launchUrl;    // optional argv-safe opener target
    std::string message;      // instructions / progress text, when present
    std::string interaction;  // browser|device, when reported
};
// Returns false for frames that carry nothing this UI can use (no kind, no url, no text).
bool parseChudcodeLoginEvent(const nlohmann::json& ev, ChudcodeLoginEvent& out);

// Sanitized response shared by chudcode_stt_status/start/stop/cancel. State uses
// Chudcode's native idle|recording|transcribing vocabulary; progress is -1 when absent.
struct ChudcodeSttResult {
    bool available = false;
    std::string state;
    std::string model;
    int progress = -1;
    std::string error;
    std::string text;
};
bool parseChudcodeSttResult(const nlohmann::json& r, ChudcodeSttResult& out);

// Insert committed/partial speech after the prompt that existed when recording began.
std::string composeChudcodeTranscript(const std::string& base, const std::string& committed,
                                     const std::string& partial);

// Provider-bound acknowledgement checks used by onboarding. A stale or cross-provider
// response cannot validate/save the currently selected provider.
bool chudcodeKeyValidationAccepted(const nlohmann::json& r, const std::string& providerId);
bool chudcodeKeySaveAccepted(const nlohmann::json& r, const std::string& providerId);

// chudcode_session_stats — authoritative per-session accounting from the Chudcode host.
struct ChudcodeSessionModelRow {
    std::string model;
    std::string provider;
    double usd = 0.0;
    long long tokensIn = 0, tokensOut = 0;
    bool priced = false;
};
struct ChudcodeSessionStats {
    bool hasCost = false;      // a dollar figure was reported (0 is a real value)
    double usd = 0.0;
    long long tokensIn = 0, tokensOut = 0;
    long long cacheRead = 0, cacheWrite = 0;
    std::vector<ChudcodeSessionModelRow> models;
};
// Parses {ok:true, ...} into out. Returns false when the response is not an ok object
// (out untouched — the caller keeps its last good data).
bool parseChudcodeSessionStats(const nlohmann::json& r, ChudcodeSessionStats& out);

// chudcode_tool_state — sanitized built-in tool names/load modes.
struct ChudcodeToolRow {
    std::string name;
    std::string loadMode;      // eager|lazy|on_demand|... verbatim; empty = unreported
    std::string description;
    bool builtin = false;
    bool available = true;     // false = the service knows the tool but cannot serve it here
};
// Returns false when the response is not an ok object carrying a tools array.
bool parseChudcodeToolState(const nlohmann::json& r, std::vector<ChudcodeToolRow>& out);

// Which array, if any, of a session_compact / session_shake result should REPLACE the
// visible chat transcript. Returns a pointer into `result` ("messages" first, then the
// legacy bare "kept_messages" tail) ONLY when that array is a nonempty explicit
// replacement payload; nullptr otherwise. Chudcode online compaction rewrites the
// service-side session and legitimately omits both keys — and a present-but-EMPTY array
// is a degenerate payload, not an instruction to wipe the visible chat.
const nlohmann::json* chudcodeCompactReplacement(const nlohmann::json& result);

}  // namespace ui
