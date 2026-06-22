// core/agent/config.cpp — see config.h. Mirrors agent/config.py resolution.
#include "agent/config.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cstdlib>
#include <fstream>
#include <sstream>

#include "agent/paths.h"

namespace aicad {
namespace agent {

const char* kDefaultLocalModel = "gemma-4-e4b";
const char* kDefaultLocalBaseUrl = "http://localhost:1234/v1";
const char* kOfflineSetting = "offline_mode";

// load_config — mirror agent/config.py load_config(): read paths_config()
// (%APPDATA%/AI-CAD/config.json) and parse it. Returns {} on a missing file or any
// parse error (matching the Python try/except that swallows failures).
json load_config() {
    std::error_code ec;
    auto p = paths_config();
    if (!std::filesystem::exists(p, ec)) return json::object();
    try {
        std::ifstream fh(p, std::ios::binary);
        if (!fh) return json::object();
        std::ostringstream ss;
        ss << fh.rdbuf();
        json j = json::parse(ss.str());
        return j.is_object() ? j : json::object();
    } catch (...) {
        return json::object();
    }
}

namespace {

const std::array<const char*, 5> kLoopbackHosts = {
    "localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"};

std::string lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

// Replicate urlparse(url).hostname:
//  - netloc = substring after "scheme://" up to the next '/', '?' or '#'.
//    (Python's urlsplit treats "//" as the netloc marker after the scheme.)
//  - within netloc: drop userinfo (everything up to and incl. the last '@'),
//    then if it starts with '[' the host is up to the matching ']' (brackets
//    stripped), else up to the first ':'.
//  - hostname is then lowercased by urlparse. Returns empty string for "no host".
std::string urlparse_hostname(const std::string& url) {
    if (url.empty()) return "";
    // Find "://" or a leading "//". Python: scheme is [a-z][a-z0-9+.-]* then ':'.
    std::string rest;
    size_t scheme_sep = url.find("://");
    if (scheme_sep != std::string::npos) {
        rest = url.substr(scheme_sep + 3);
    } else if (url.rfind("//", 0) == 0) {
        rest = url.substr(2);
    } else {
        // No netloc marker -> urlsplit puts everything in path; hostname is None.
        return "";
    }
    // netloc ends at first '/', '?' or '#'.
    size_t end = rest.find_first_of("/?#");
    std::string netloc = (end == std::string::npos) ? rest : rest.substr(0, end);
    // Drop userinfo.
    size_t at = netloc.rfind('@');
    if (at != std::string::npos) netloc = netloc.substr(at + 1);
    // IPv6 literal in brackets.
    std::string host;
    if (!netloc.empty() && netloc[0] == '[') {
        size_t close = netloc.find(']');
        if (close != std::string::npos)
            host = netloc.substr(1, close - 1);
        else
            host = netloc.substr(1);
    } else {
        size_t colon = netloc.find(':');
        host = (colon == std::string::npos) ? netloc : netloc.substr(0, colon);
    }
    return lower(host);
}

bool in_loopback(const std::string& host) {
    for (const char* h : kLoopbackHosts)
        if (host == h) return true;
    return false;
}

// env override -> fall back to real getenv. Empty optional means "use getenv".
std::optional<std::string> env_get(const json& env, const char* key) {
    if (env.contains(key) && env[key].is_string())
        return env[key].get<std::string>();
    const char* v = std::getenv(key);
    if (v) return std::string(v);
    return std::nullopt;
}

// cfg.get(key) as a string, with optional nested default. Returns nullopt if
// absent/null. Mirrors Python truthiness via `or` chains at the call sites.
std::optional<std::string> cfg_str(const json& cfg, const char* key) {
    if (cfg.contains(key) && cfg[key].is_string()) {
        std::string s = cfg[key].get<std::string>();
        return s;  // empty string preserved; caller applies Python `or` (falsy).
    }
    return std::nullopt;
}

// Python `a or b`: a if truthy (non-empty string / present) else b.
std::string py_or(const std::optional<std::string>& a, const std::string& b) {
    if (a && !a->empty()) return *a;
    return b;
}

}  // namespace

bool offline_enabled(const json& cfg) {
    if (!cfg.is_object() || !cfg.contains(kOfflineSetting)) return false;
    const json& v = cfg[kOfflineSetting];
    if (v.is_boolean()) return v.get<bool>();
    if (v.is_number()) return v.get<double>() != 0.0;
    if (v.is_string()) return !v.get<std::string>().empty();
    if (v.is_null()) return false;
    if (v.is_array()) return !v.empty();
    if (v.is_object()) return !v.empty();
    return false;
}

bool host_is_loopback(const std::string& base_url) {
    if (base_url.empty()) return false;
    return in_loopback(urlparse_hostname(base_url));
}

bool provider_offline_safe(const json& provider) {
    if (provider.contains("offline_safe") && provider["offline_safe"].is_boolean() &&
        provider["offline_safe"].get<bool>())
        return true;
    std::string name;
    if (provider.contains("name") && provider["name"].is_string())
        name = provider["name"].get<std::string>();
    if (name == "mock" || name == "mock-roles" || name == "base") return true;
    if (name == "anthropic") return false;
    if (name == "gemini") return false;  // always cloud (generativelanguage API)
    std::optional<std::string> base_url;
    if (provider.contains("base_url") && provider["base_url"].is_string())
        base_url = provider["base_url"].get<std::string>();
    if (name == "openai")
        return base_url ? host_is_loopback(*base_url) : false;
    if (name == "llamacpp")
        return host_is_loopback(py_or(base_url, kDefaultLocalBaseUrl));
    // Unknown provider: loopback base_url allowed; None/cloud refused.
    return base_url ? host_is_loopback(*base_url) : false;
}

json resolve_provider(const std::optional<std::string>& name_arg, const json& cfg,
                      const json& env) {
    bool offline = offline_enabled(cfg);
    // name = (name_arg or env AI_CAD_PROVIDER or cfg.provider or "").lower()
    std::optional<std::string> name_opt = name_arg;
    if (!name_opt || name_opt->empty()) {
        auto ev = env_get(env, "AI_CAD_PROVIDER");
        if (ev && !ev->empty()) name_opt = ev;
    }
    if (!name_opt || name_opt->empty()) {
        auto pv = cfg_str(cfg, "provider");
        if (pv && !pv->empty()) name_opt = pv;
    }
    std::string name = lower(name_opt.value_or(""));

    auto refusal = [](const std::string& msg) {
        return json{{"error", msg}};
    };

    // Offline refusals BEFORE constructing anything (security-critical path).
    if (offline && name == "gemini")
        return refusal(
            std::string("offline mode is enabled (config '") + kOfflineSetting +
            "': true): the Google Gemini cloud provider is refused. Use a local "
            "(loopback) model server instead.");
    if (offline && (name == "anthropic" || name == "openai") && !name.empty()) {
        if (name == "anthropic")
            return refusal(
                std::string("offline mode is enabled (config '") + kOfflineSetting +
                "': true): the Anthropic cloud provider is refused. Use a local "
                "(loopback) model server instead.");
        // openai: only allowed if a loopback base_url is configured.
        std::optional<std::string> oai_base;
        if (cfg.contains("openai") && cfg["openai"].is_object() &&
            cfg["openai"].contains("base_url") && cfg["openai"]["base_url"].is_string())
            oai_base = cfg["openai"]["base_url"].get<std::string>();
        if (!(oai_base && host_is_loopback(*oai_base)))
            return refusal(
                std::string("offline mode is enabled (config '") + kOfflineSetting +
                "': true): the OpenAI cloud provider is refused. Configure a local "
                "(loopback) base_url, or disable offline mode in Settings.");
    }
    if (offline && (name == "llamacpp" || name == "llama.cpp" || name == "llama-cpp")) {
        json lcfg = (cfg.contains("llamacpp") && cfg["llamacpp"].is_object())
                        ? cfg["llamacpp"]
                        : json::object();
        std::string host = "127.0.0.1";
        if (lcfg.contains("host") && lcfg["host"].is_string() &&
            !lcfg["host"].get<std::string>().empty())
            host = lcfg["host"].get<std::string>();
        long port = 1234;
        if (lcfg.contains("port")) {
            if (lcfg["port"].is_number()) {
                long p = lcfg["port"].get<long>();
                if (p) port = p;
            } else if (lcfg["port"].is_string() && !lcfg["port"].get<std::string>().empty()) {
                port = std::atol(lcfg["port"].get<std::string>().c_str());
            }
        }
        std::string base_url;
        if (lcfg.contains("base_url") && lcfg["base_url"].is_string() &&
            !lcfg["base_url"].get<std::string>().empty())
            base_url = lcfg["base_url"].get<std::string>();
        else
            base_url = "http://" + host + ":" + std::to_string(port) + "/v1";
        if (!host_is_loopback(base_url))
            return refusal(
                std::string("offline mode is enabled (config '") + kOfflineSetting +
                "': true): the managed llama.cpp endpoint '" + base_url +
                "' is not loopback, so it is refused before probing the server.");
    }

    // Resolve descriptors (no network client construction).
    auto oai_cfg = [&](const char* key) -> std::optional<std::string> {
        if (cfg.contains("openai") && cfg["openai"].is_object() &&
            cfg["openai"].contains(key) && cfg["openai"][key].is_string())
            return cfg["openai"][key].get<std::string>();
        return std::nullopt;
    };
    bool oai_vision = true;
    if (cfg.contains("openai") && cfg["openai"].is_object() &&
        cfg["openai"].contains("vision") && cfg["openai"]["vision"].is_boolean())
        oai_vision = cfg["openai"]["vision"].get<bool>();

    json desc;
    if (name.empty()) {
        // openai-local default descriptor.
        desc = json{
            {"name", "openai"},
            {"model", py_or(cfg_str(cfg, "model"), kDefaultLocalModel)},
            {"base_url", py_or(oai_cfg("base_url"), kDefaultLocalBaseUrl)},
            {"supports_vision", oai_vision},
            {"api_key", py_or(py_or(cfg_str(cfg, "api_key"),
                                    env_get(env, "OPENAI_API_KEY").value_or("")),
                              "lm-studio")},
        };
    } else if (name == "mock") {
        desc = json{{"name", "mock"}, {"model", nullptr}, {"base_url", nullptr},
                    {"supports_vision", false}, {"api_key", nullptr}};
    } else if (name == "anthropic") {
        desc = json{
            {"name", "anthropic"},
            {"model", py_or(cfg_str(cfg, "model"), "claude-opus-4-8")},
            {"base_url", nullptr},
            {"supports_vision", true},
            {"api_key", py_or(cfg_str(cfg, "api_key"),
                              env_get(env, "ANTHROPIC_API_KEY").value_or(""))},
        };
        if (desc["api_key"].is_string() && desc["api_key"].get<std::string>().empty())
            desc["api_key"] = nullptr;  // key may be None when unset (mirrors Python).
    } else if (name == "gemini") {
        // Google Gemini (cloud). Key from cfg.api_key or GEMINI_API_KEY / GOOGLE_API_KEY env.
        std::string key = py_or(cfg_str(cfg, "api_key"), env_get(env, "GEMINI_API_KEY").value_or(""));
        if (key.empty()) key = env_get(env, "GOOGLE_API_KEY").value_or("");
        desc = json{
            {"name", "gemini"},
            {"model", py_or(cfg_str(cfg, "model"), "gemini-2.5-flash")},
            {"base_url", nullptr},
            {"supports_vision", true},
            {"api_key", key},
        };
        if (desc["api_key"].is_string() && desc["api_key"].get<std::string>().empty())
            desc["api_key"] = nullptr;
    } else if (name == "openai") {
        std::optional<std::string> base = oai_cfg("base_url");
        desc = json{
            {"name", "openai"},
            {"model", py_or(cfg_str(cfg, "model"), "gpt-4o")},
            {"base_url", base ? json(*base) : json(nullptr)},
            {"supports_vision", oai_vision},
            {"api_key", py_or(cfg_str(cfg, "api_key"),
                              env_get(env, "OPENAI_API_KEY").value_or(""))},
        };
        if (desc["api_key"].is_string() && desc["api_key"].get<std::string>().empty())
            desc["api_key"] = nullptr;
    } else if (name == "llamacpp" || name == "llama.cpp" || name == "llama-cpp") {
        desc = json{
            {"name", "llamacpp"},
            {"model", py_or(cfg_str(cfg, "model"), kDefaultLocalModel)},
            {"base_url", kDefaultLocalBaseUrl},
            {"supports_vision", false},
            {"api_key", nullptr},
        };
    } else {
        return refusal(std::string("Unknown provider: '") + name + "'");
    }

    // assert_offline_safe AFTER resolution (catches the empty-name local default
    // + edge cases). Returns an error field instead of throwing.
    if (offline && !provider_offline_safe(desc)) {
        std::string n = desc.contains("name") && desc["name"].is_string()
                            ? desc["name"].get<std::string>() : "?";
        std::string base = "(cloud endpoint)";
        if (desc.contains("base_url") && desc["base_url"].is_string() &&
            !desc["base_url"].get<std::string>().empty())
            base = desc["base_url"].get<std::string>();
        return refusal(
            std::string("offline mode is enabled (config '") + kOfflineSetting +
            "': true): the resolved provider '" + n + "' -> " + base +
            " is NOT local-only, so the run is refused. Switch to a local "
            "(loopback) model server or disable offline mode in Settings.");
    }
    // Reasoning effort (Off/Low/Medium/High) flows to the provider as a hint; the
    // provider layer maps it per-backend (gemini thinkingConfig, anthropic effort).
    if (auto eff = cfg_str(cfg, "reasoning_effort"); eff && !eff->empty())
        desc["reasoning_effort"] = *eff;

    desc["offline_safe"] = provider_offline_safe(desc);
    return desc;
}

}  // namespace agent
}  // namespace aicad
