// core/agent/config.h — C++ port of services/agent/config.py (provider resolution +
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

namespace chudware {
namespace agent {

using nlohmann::json;

extern const char* kDefaultLocalModel;     // "gemma-4-e4b"
extern const char* kDefaultLocalBaseUrl;   // "http://localhost:1234/v1"
extern const char* kOfflineSetting;        // "offline_mode"

// Load the ambient user config (paths_config() == %APPDATA%/Chudware/config.json).
// Mirrors services/agent/config.py load_config(): parse the file if it exists, returning {}
// on a missing file or any parse error (never throws). This is the SAME config the
// Python agent reads, so a config-resolved provider matches the Python behavior.
json load_config();

// bool(cfg.get("offline_mode", False)).
bool offline_enabled(const json& cfg);

// compliance_mode: "off" | "redact" | "local_only" (WS3 ITAR outbound redaction).
// Reads the top-level "compliance_mode" config key; normalized case-insensitively
// ("localonly"/"local-only"/"local" -> "local_only"); unknown/missing -> "off".
// Mirrors services/agent/redaction.py normalize_compliance_mode/compliance_mode.
std::string compliance_mode(const json& cfg);

// local_only_enabled: offline_enabled(cfg) || compliance_mode(cfg)=="local_only" — the
// named WS3 equivalent of the legacy hard local-only switch. resolve_provider() refuses
// every non-loopback provider under either (same machinery as offline_mode).
bool local_only_enabled(const json& cfg);

// urlparse(base_url).hostname lowercased ∈ {localhost,127.0.0.1,::1,[::1],0.0.0.0}.
bool host_is_loopback(const std::string& base_url);

// provider_offline_safe(provider): provider carries {name, base_url?, offline_safe?}.
bool provider_offline_safe(const json& provider);

// Resolve to a descriptor. `env` carries explicit overrides for determinism in
// the gate: {CHUDWARE_PROVIDER?, ANTHROPIC_API_KEY?, OPENAI_API_KEY?}. Absent keys
// fall back to the real getenv. Returns a descriptor; on an offline refusal (or
// unknown provider) the returned object carries an "error" string instead.
// `cfg` is taken by value: for subscription providers the resolver may refresh a
// stale OAuth token in-process (agent/auth.cpp) and swap in the reloaded
// config before reading tokens.
json resolve_provider(const std::optional<std::string>& name, json cfg, const json& env);

}  // namespace agent
}  // namespace chudware
