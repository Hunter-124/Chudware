// core/agent/config.h — C++ port of agent/config.py (provider resolution +
// offline guard, the ITAR-compliance feature).
//
// P52: ports the PURE resolution logic — the host wires the actual network
// clients. get_provider() here returns a RESOLVED DESCRIPTOR
//   {name, model, api_key, base_url, supports_vision, offline_safe}
// and applies the offline guard by returning an `error` field instead of
// throwing (the security-critical refusal path). NO network clients built.
#pragma once

#include <optional>
#include <string>

#include <nlohmann/json.hpp>

namespace aicad {
namespace agent {

using nlohmann::json;

extern const char* kDefaultLocalModel;     // "gemma-4-e4b"
extern const char* kDefaultLocalBaseUrl;   // "http://localhost:1234/v1"
extern const char* kOfflineSetting;        // "offline_mode"

// Load the ambient user config (paths_config() == %APPDATA%/AI-CAD/config.json).
// Mirrors agent/config.py load_config(): parse the file if it exists, returning {}
// on a missing file or any parse error (never throws). This is the SAME config the
// Python agent reads, so a config-resolved provider matches the Python behavior.
json load_config();

// bool(cfg.get("offline_mode", False)).
bool offline_enabled(const json& cfg);

// urlparse(base_url).hostname lowercased ∈ {localhost,127.0.0.1,::1,[::1],0.0.0.0}.
bool host_is_loopback(const std::string& base_url);

// provider_offline_safe(provider): provider carries {name, base_url?, offline_safe?}.
bool provider_offline_safe(const json& provider);

// Resolve to a descriptor. `env` carries explicit overrides for determinism in
// the gate: {AI_CAD_PROVIDER?, ANTHROPIC_API_KEY?, OPENAI_API_KEY?}. Absent keys
// fall back to the real getenv. Returns a descriptor; on an offline refusal (or
// unknown provider) the returned object carries an "error" string instead.
json resolve_provider(const std::optional<std::string>& name, const json& cfg, const json& env);

// --- experimental AI features (config "experimental" block) -----------------
// These are opt-in, default-off knobs the host exposes in Settings -> Experimental. They are
// ADDITIVE: with the block absent the resolved descriptor is byte-identical to the pre-feature
// shape, so the run loop and the Python-parity gate are unchanged.
//
// resolve_review_provider: resolve a SEPARATE provider descriptor for the vision review/fix
// stages from "experimental.vision_review" — a local OpenAI-compatible vision endpoint (e.g. a
// self-hosted Qwen2.5-VL). This is the Seek-CAD-style split: a strong (or cloud) planner emits
// ops while a local vision judge sees the render and drives self-correction, which also makes an
// all-local ITAR-safe review loop possible. Returns a resolved descriptor (same shape as
// resolve_provider, supports_vision forced on), {error:...} on an offline refusal, or null when
// the block is absent/disabled. Inherits the ambient offline_mode guard.
json resolve_review_provider(const json& cfg, const json& env);

// apply_experimental: attach the experimental knobs onto an already-resolved MAIN descriptor so
// the run loop is the single consumer. Adds "review_provider" (when resolve_review_provider yields
// a usable descriptor) and the "cad_sequence_mode" bool (modeler prompt augmentation preferring an
// editable parametric feature sequence over a monolithic solid). No-op when the block is absent.
json apply_experimental(json resolved_main, const json& cfg, const json& env);

// The model's total token context window for a resolved descriptor. Reads the descriptor's
// "context_window" (populated by resolve_provider from the config override or a per-provider
// default), falling back to the per-provider default by name if absent. Local/unknown providers
// default to a conservative 8192 so we never overrun a small local window.
int context_window_for(const json& provider_cfg);

// Coordinate the per-call INPUT token cap with the OUTPUT token reservation so the two, plus a
// safety margin, fit inside the model's context window. `desired_output` is the caller's wanted
// output budget (e.g. call_budget_for). For a large (cloud) window the input cap stays lean
// (kContextTokenCap) and output stays at `desired_output`; for a small LOCAL window BOTH shrink
// so input + output + margin <= window. This is what stops local models from silently running
// out of context mid-chain (the server truncating the prompt and the model emitting garbage).
struct CallBudget {
    int input_cap;  // max input (system + body) tokens to assemble for this call
    int output;     // max output tokens to request from the provider
};
CallBudget fit_call_budget(const json& provider_cfg, int desired_output);

}  // namespace agent
}  // namespace aicad
