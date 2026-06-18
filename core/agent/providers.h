// core/agent/providers.h — C++ port of agent/providers/*.py (the LLM provider layer).
//
// This is the first increment (P51) of the agent/LLM port toward zero-Python. It
// mirrors, byte-for-byte, the wire-format construction + response parsing of the
// Python provider adapters:
//   base.py             — provider-neutral message/part builders.
//   anthropic_provider  — Claude messages.create body + content blocks + parse.
//   openai_provider     — OpenAI-compatible chat.completions body + parse, base-url
//                         normalization, text-join-vs-array message branch.
//   llamacpp_provider   — == OpenAIProvider with a loopback base_url (the process-
//                         management half stays a host concern, out of scope here).
//   mock_provider       — MockProvider + RoleScriptedProvider (no network).
//
// The request BUILDERS are kept pure (no network) so they can be golden-tested
// offline against the Python oracle (tools/cpp_agent_parity.py).
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace aicad {
namespace agent {

using nlohmann::json;

// --- base.py message/part builders ------------------------------------------
json text_part(const std::string& text);
json image_part(const std::string& path);
json user_message(const json& parts);       // base.user(parts)
json assistant_message(const json& parts);  // base.assistant(parts)

// --- helpers ----------------------------------------------------------------
// base64.standard_b64encode(...).decode() — standard alphabet, "=" padding.
std::string base64_encode(const std::string& bytes);
// Read a file's raw bytes; throws std::runtime_error if it cannot be opened.
std::string read_file_bytes(const std::string& path);

// --- a built request (pure; ready for http_post) ----------------------------
struct BuiltRequest {
    std::string url;
    std::vector<std::pair<std::string, std::string>> headers;
    std::string body;  // serialized JSON
    json body_json;    // same body as parsed JSON (for golden tests / inspection)
};

// --- anthropic_provider.py --------------------------------------------------
extern const char* kAnthropicDefaultModel;  // "claude-opus-4-8"
// _block(part): text -> {type,text}; image -> {type,source:{type,media_type,data}}.
json anthropic_block(const json& part);
BuiltRequest anthropic_build_request(const std::string& model, const std::string& system,
                                     const json& messages, int max_tokens);
struct ParsedReply {
    std::string text;
    std::string thinking;
};
ParsedReply anthropic_parse_response(const json& resp);

// --- openai_provider.py -----------------------------------------------------
extern const char* kOpenAIDefaultModel;  // "gpt-4o"
// _normalize_base_url: strip trailing "/"; append "/v1" if scheme+netloc and path in ("","/").
// Returns the input unchanged when empty (mirrors the Python `if not base_url: return base_url`).
std::string openai_normalize_base_url(const std::string& base_url);
// _content(part): text -> {type,text}; image -> {type:image_url,image_url:{url:data-uri}}.
json openai_content(const json& part);
// _message(role, parts): text-only -> {role, content:"\n\n".join}; else array-of-parts.
json openai_message(const std::string& role, const json& parts);
BuiltRequest openai_build_request(const std::string& model, const std::string& system,
                                  const json& messages, int max_tokens,
                                  const std::string& base_url);
ParsedReply openai_parse_response(const json& resp);

// --- mock_provider.py -------------------------------------------------------
// MockProvider: scripted replies in order; the last repeats once exhausted.
class MockProvider {
public:
    explicit MockProvider(std::vector<std::string> replies)
        : replies_(std::move(replies)), i_(0) {}
    std::string complete();  // ignores system/messages — deterministic script.

private:
    std::vector<std::string> replies_;
    size_t i_;
};

// RoleScriptedProvider: returns replies keyed by orchestrator role, sniffed from
// the system prompt's opening line. role_of() is the public, static sniffer.
class RoleScriptedProvider {
public:
    explicit RoleScriptedProvider(json by_role) : by_role_(std::move(by_role)) {}
    static std::string role_of(const std::string& system);
    std::string complete(const std::string& system);

private:
    json by_role_;  // {role: [reply, ...]}
};

// --- assist provider (ask.py / pm_assist.py single-call helper) -------------
// Build a single-call completion fn from a mock descriptor for the in-app assists.
// Supports {name:"mock", replies:[...]} (flat MockProvider, last repeats),
// {name:"mock", by_role:{role:[...]}} (RoleScriptedProvider, routed on the system
// prompt's ROLE: line), and {error:"..."} (signals a provider error via *err,
// like the Python except branch). Any other descriptor routes through complete().
// The returned fn ignores `err` for the ask path; pm_assist reads it.
#include <functional>
using AssistCompleteFn =
    std::function<std::string(const std::string& system, const std::string& body, std::string* err)>;
AssistCompleteFn make_assist_provider(const json& descriptor);

// --- high-level dispatch ----------------------------------------------------
// complete(): pick anthropic vs openai/llamacpp, build the request, http_post,
// parse. provider is "anthropic"|"openai"|"llamacpp"|"mock". cfg may carry
// {api_key, model, base_url}. On a transport/HTTP error, .error is set.
struct CompleteResult {
    std::string text;
    std::string thinking;
    std::string error;
};
CompleteResult complete(const std::string& provider, const json& cfg, const std::string& system,
                        const json& messages, int max_tokens = 8000);

}  // namespace agent
}  // namespace aicad
