// core/agent/config.cpp — see config.h. Mirrors agent/config.py resolution.
#include "agent/config.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cstring>
#include <cstdlib>
#include <fstream>
#include <sstream>
#include <vector>

#include "agent/paths.h"
#include "agent/state.h"  // kContextTokenCap (the lean input-cap ceiling for big windows)

namespace aicad {
namespace agent {

const char* kDefaultLocalModel = "gemma-4-e4b";
const char* kDefaultLocalBaseUrl = "http://localhost:1234/v1";
const char* kOfflineSetting = "offline_mode";

namespace {
// Per-provider default context window (total tokens) when the user has not set one. Cloud
// models are large; a LOCAL/unknown server is assumed 8192 — a common LM Studio / Ollama
// default — so input+output budgeting never overruns a small window by accident.
int default_context_window(const std::string& name) {
    if (name == "anthropic" || name == "anthropic-sub") return 200000;
    if (name == "gemini") return 1048576;
    if (name == "openai" || name == "xai" || name == "xai-sub") return 128000;
    if (name == "moonshot") return 1048576;  // Kimi K3 1M window (kimi-k3-256k caps via cfg)
    return 8192;  // local / unknown — conservative
}

// Read the ACTIVE account's access_token from config.subscription_auth[provider_id].
// READ-ONLY: never refreshes or writes tokens. xAI rotates refresh tokens on every
// exchange — only the Python xai_sub_provider may refresh+persist. If the access
// token is expired the call will 401; the user re-signs in via Settings.
std::string subscription_access_token(const json& cfg, const char* provider_id) {
    if (!cfg.is_object() || !cfg.contains("subscription_auth") ||
        !cfg["subscription_auth"].is_object())
        return {};
    const json& sa = cfg["subscription_auth"];
    auto it = sa.find(provider_id);
    if (it == sa.end() || !it->is_object()) return {};
    const json& entry = *it;
    // Multi-account shape: {active, accounts:{id:{access_token,...}}}
    if (entry.contains("accounts") && entry["accounts"].is_object()) {
        std::string aid;
        if (entry.contains("active") && entry["active"].is_string())
            aid = entry["active"].get<std::string>();
        if (aid.empty() && !entry["accounts"].empty())
            aid = entry["accounts"].begin().key();
        auto ac = entry["accounts"].find(aid);
        if (ac == entry["accounts"].end() || !ac->is_object()) return {};
        if (ac->contains("access_token") && (*ac)["access_token"].is_string())
            return (*ac)["access_token"].get<std::string>();
        return {};
    }
    // Legacy flat credential.
    if (entry.contains("access_token") && entry["access_token"].is_string())
        return entry["access_token"].get<std::string>();
    return {};
}

// All access tokens for *provider_id*, ACTIVE account first, for 403/tier-gate
// rotation. Read-only (no refresh); complete() tries the next signed-in account when
// one returns a permission-denied (Heavy-only tier gate).
std::vector<std::string> all_subscription_access_tokens(const json& cfg, const char* provider_id) {
    std::vector<std::string> out;
    if (!cfg.is_object() || !cfg.contains("subscription_auth") ||
        !cfg["subscription_auth"].is_object())
        return out;
    const json& sa = cfg["subscription_auth"];
    auto it = sa.find(provider_id);
    if (it == sa.end() || !it->is_object()) return out;
    const json& entry = *it;
    auto add = [&](const json& ac) {
        if (ac.is_object() && ac.contains("access_token") && ac["access_token"].is_string()) {
            std::string t = ac["access_token"].get<std::string>();
            if (!t.empty() && std::find(out.begin(), out.end(), t) == out.end())
                out.push_back(t);
        }
    };
    if (entry.contains("accounts") && entry["accounts"].is_object()) {
        std::string active;
        if (entry.contains("active") && entry["active"].is_string())
            active = entry["active"].get<std::string>();
        auto af = entry["accounts"].find(active);
        if (af != entry["accounts"].end()) add(*af);  // active first
        for (auto& [k, v] : entry["accounts"].items()) add(v);
    } else {
        add(entry);
    }
    return out;
}
}  // namespace

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

std::string consent_tier(const json& cfg) {
    // Hard override: offline/ITAR mode always forces consent off.
    if (offline_enabled(cfg)) return "off";
    if (!cfg.is_object() || !cfg.contains("telemetry_consent")) return "off";
    const json& v = cfg["telemetry_consent"];
    if (!v.is_string()) return "off";
    const std::string s = v.get<std::string>();
    if (s == "meta" || s == "full") return s;
    return "off";  // unknown / explicit "off" / anything else
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
    if (name == "anthropic" || name == "anthropic-sub") return false;
    if (name == "gemini") return false;  // always cloud (generativelanguage API)
    if (name == "xai" || name == "xai-sub" || name == "cursor" || name == "cursor-sub" ||
        name == "moonshot")
        return false;
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
    if (offline && (name == "gemini" || name == "xai" || name == "xai-sub" ||
                    name == "anthropic-sub" || name == "cursor" || name == "cursor-sub" ||
                    name == "moonshot"))
        return refusal(
            std::string("offline mode is enabled (config '") + kOfflineSetting +
            "': true): the cloud provider '" + name +
            "' is refused. Use a local (loopback) model server instead.");
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
        // Prefer a classic API key; fall back to the active Claude subscription OAuth token.
        std::string key = py_or(cfg_str(cfg, "api_key"),
                                env_get(env, "ANTHROPIC_API_KEY").value_or(""));
        if (key.empty() && cfg.contains("keys") && cfg["keys"].is_object() &&
            cfg["keys"].contains("anthropic") && cfg["keys"]["anthropic"].is_string())
            key = cfg["keys"]["anthropic"].get<std::string>();
        bool oauth = false;
        if (key.empty()) {
            key = subscription_access_token(cfg, "anthropic-sub");
            oauth = !key.empty();
        } else if (key.rfind("sk-ant-oat", 0) == 0) {
            oauth = true;
        }
        // Global cfg.model may be a Grok id when the active provider is xAI — never send
        // a non-Claude model id to the Anthropic Messages API.
        std::string model = py_or(cfg_str(cfg, "model"), "claude-opus-4-8");
        {
            std::string ml = lower(model);
            if (ml.find("claude") == std::string::npos && ml.find("opus") == std::string::npos &&
                ml.find("sonnet") == std::string::npos && ml.find("haiku") == std::string::npos)
                model = "claude-opus-4-8";
        }
        desc = json{
            {"name", oauth ? "anthropic-sub" : "anthropic"},
            {"model", model},
            {"base_url", nullptr},
            {"supports_vision", true},
            {"api_key", key},
        };
        if (oauth) desc["auth_mode"] = "oauth";
        if (desc["api_key"].is_string() && desc["api_key"].get<std::string>().empty())
            desc["api_key"] = nullptr;  // key may be None when unset (mirrors Python).
    } else if (name == "anthropic-sub") {
        std::string key = subscription_access_token(cfg, "anthropic-sub");
        if (key.empty())
            return refusal(
                "Claude subscription not signed in. Open Settings → AI → Accounts and "
                "sign in with Claude (Pro/Max).");
        std::string model = py_or(cfg_str(cfg, "model"), "claude-opus-4-8");
        {
            std::string ml = lower(model);
            if (ml.find("claude") == std::string::npos && ml.find("opus") == std::string::npos &&
                ml.find("sonnet") == std::string::npos && ml.find("haiku") == std::string::npos)
                model = "claude-opus-4-8";
        }
        desc = json{
            {"name", "anthropic-sub"},
            {"model", model},
            {"base_url", nullptr},
            {"supports_vision", true},
            {"api_key", key},
            {"auth_mode", "oauth"},
        };
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
    } else if (name == "xai" || name == "xai-sub") {
        // xAI Grok: classic console.x.ai API key OR SuperGrok OAuth access_token.
        // Completions are OpenAI-compatible at https://api.x.ai/v1/chat/completions.
        // C++ NEVER refreshes the OAuth token (xAI rotates RTs; only Python persists).
        std::string key = py_or(cfg_str(cfg, "api_key"), env_get(env, "XAI_API_KEY").value_or(""));
        if (key.empty() && cfg.contains("keys") && cfg["keys"].is_object() &&
            cfg["keys"].contains("xai") && cfg["keys"]["xai"].is_string())
            key = cfg["keys"]["xai"].get<std::string>();
        bool oauth = false;
        if (key.empty() || name == "xai-sub") {
            std::string sub = subscription_access_token(cfg, "xai-sub");
            if (!sub.empty()) {
                key = sub;
                oauth = true;
            }
        }
        if (key.empty()) {
            return refusal(
                "xAI not configured. Sign in with SuperGrok (Settings → AI → Accounts) "
                "or set an API key from console.x.ai.");
        }
        std::string model = py_or(cfg_str(cfg, "model"), "grok-4.5");
        {
            std::string ml = lower(model);
            // Reject Claude/OpenAI/etc. ids left over when the active provider was switched.
            if (ml.find("grok") == std::string::npos && ml.rfind("xai", 0) != 0)
                model = "grok-4.5";
            if (model == "grok-4-5") model = "grok-4.5";
        }
        desc = json{
            {"name", oauth ? "xai-sub" : "xai"},
            {"model", model},
            {"base_url", "https://api.x.ai/v1"},
            {"supports_vision", true},
            {"api_key", key},
        };
        if (oauth) {
            desc["auth_mode"] = "oauth";
            // Multi-account fallback: on a 403 tier-gate, complete() rotates to the
            // next signed-in xai-sub account (read-only; Python owns refresh).
            json fb = json::array();
            for (const auto& t : all_subscription_access_tokens(cfg, "xai-sub"))
                if (t != key) fb.push_back(t);
            if (!fb.empty()) desc["fallback_api_keys"] = fb;
        }
    } else if (name == "moonshot") {
        // Moonshot (Kimi) — OpenAI-compatible chat completions at api.moonshot.ai/v1. Key
        // precedence: cfg.api_key -> keys.moonshot -> MOONSHOT_API_KEY. Kimi K3 / K2.6 are
        // native-vision, so supports_vision follows the openai.vision override (default true).
        std::string key = py_or(cfg_str(cfg, "api_key"),
                                env_get(env, "MOONSHOT_API_KEY").value_or(""));
        if (key.empty() && cfg.contains("keys") && cfg["keys"].is_object() &&
            cfg["keys"].contains("moonshot") && cfg["keys"]["moonshot"].is_string())
            key = cfg["keys"]["moonshot"].get<std::string>();
        if (key.empty())
            return refusal(
                "Moonshot (Kimi) not configured. Add a key from platform.moonshot.ai "
                "(Settings -> AI), stored as keys.moonshot / MOONSHOT_API_KEY.");
        std::string model = py_or(cfg_str(cfg, "model"), std::string("kimi-k3"));
        desc = json{
            {"name", "moonshot"},
            {"model", model},
            {"base_url", py_or(oai_cfg("base_url"), std::string("https://api.moonshot.ai/v1"))},
            {"supports_vision", oai_vision},
            {"api_key", key},
        };
    } else if (name == "cursor" || name == "cursor-sub") {
        // Cursor auth is stored (subscription_auth.cursor) but there is NO REST
        // completions transport in C++ or Python — Cursor's agent backend is
        // Connect-RPC/protobuf over HTTP/2. Route via a different provider.
        return refusal(
            "Cursor has no C++ chat completions transport (its agent API is Connect-RPC/"
            "protobuf, not REST). Cursor Design runs route through the Python worker — "
            "this refusal firing means that routing was bypassed or the worker is down.");
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

    // Optional per-call output-token override (config "max_output_tokens"). 0/absent = the
    // per-provider automatic default in call_budget_for. Raising it is the fix when a reasoning
    // model burns its whole output budget thinking and returns no/truncated JSON ("no JSON ops").
    if (cfg.contains("max_output_tokens") && cfg["max_output_tokens"].is_number_integer()) {
        int mt = cfg["max_output_tokens"].get<int>();
        if (mt > 0) desc["max_tokens"] = mt;
    }

    // Context window (config "context_window") — the model's total token window that input +
    // output must fit within. 0/absent = the per-provider default (cloud large; local/unknown
    // 8192). fit_call_budget() derives the input cap + output reservation from this so a small
    // local window is never overrun (which would corrupt the prompt and break the chain).
    {
        int cw = 0;
        if (cfg.contains("context_window") && cfg["context_window"].is_number_integer())
            cw = cfg["context_window"].get<int>();
        desc["context_window"] =
            cw > 0 ? cw : default_context_window(desc.value("name", std::string()));
    }

    desc["offline_safe"] = provider_offline_safe(desc);
    return desc;
}

json resolve_review_provider(const json& cfg, const json& env) {
    if (!cfg.is_object() || !cfg.contains("experimental") || !cfg["experimental"].is_object())
        return json(nullptr);
    const json& ex = cfg["experimental"];
    if (!ex.contains("vision_review") || !ex["vision_review"].is_object()) return json(nullptr);
    const json& vr = ex["vision_review"];
    bool enabled = vr.contains("enabled") && vr["enabled"].is_boolean() && vr["enabled"].get<bool>();
    if (!enabled) return json(nullptr);

    // The review judge is a LOCAL OpenAI-compatible vision endpoint (the Seek-CAD use case: a
    // self-hosted Qwen2.5-VL). Synthesize the minimal cfg resolve_provider expects and reuse ALL of
    // its offline-safety logic — so an offline run still refuses a non-loopback review endpoint.
    std::string base_url = vr.contains("base_url") && vr["base_url"].is_string() &&
                                   !vr["base_url"].get<std::string>().empty()
                               ? vr["base_url"].get<std::string>()
                               : std::string(kDefaultLocalBaseUrl);
    std::string model = vr.contains("model") && vr["model"].is_string() &&
                                !vr["model"].get<std::string>().empty()
                            ? vr["model"].get<std::string>()
                            : std::string("qwen2.5-vl-7b-instruct");
    std::string api_key = vr.contains("api_key") && vr["api_key"].is_string() &&
                                  !vr["api_key"].get<std::string>().empty()
                              ? vr["api_key"].get<std::string>()
                              : std::string("lm-studio");
    json sub = json{
        {"provider", "openai"},
        {"model", model},
        {"api_key", api_key},
        {"offline_mode", offline_enabled(cfg)},  // inherit the ambient ITAR guard
        {"openai", {{"base_url", base_url}, {"vision", true}}},
    };
    return resolve_provider(std::string("openai"), sub, env);
}

json apply_experimental(json resolved_main, const json& cfg, const json& env) {
    if (!cfg.is_object() || !cfg.contains("experimental") || !cfg["experimental"].is_object())
        return resolved_main;
    const json& ex = cfg["experimental"];

    // A separate vision review/fix judge. On an offline refusal (cloud endpoint while offline) we
    // SKIP it silently: review then falls back to the main provider, which already passed the
    // offline guard, so the run stays ITAR-safe rather than failing.
    json rp = resolve_review_provider(cfg, env);
    if (rp.is_object() && !rp.contains("error")) resolved_main["review_provider"] = rp;

    if (ex.contains("cad_sequence_mode") && ex["cad_sequence_mode"].is_boolean() &&
        ex["cad_sequence_mode"].get<bool>())
        resolved_main["cad_sequence_mode"] = true;
    return resolved_main;
}

int context_window_for(const json& provider_cfg) {
    if (provider_cfg.contains("context_window") &&
        provider_cfg["context_window"].is_number_integer()) {
        int cw = provider_cfg["context_window"].get<int>();
        if (cw > 0) return cw;
    }
    return default_context_window(provider_cfg.value("name", std::string()));
}

CallBudget fit_call_budget(const json& provider_cfg, int desired_output) {
    const int window = context_window_for(provider_cfg);
    const int margin = 768;      // role markers + JSON scaffolding + tokenizer slop
    const int min_input = 1024;  // always leave room for the request + the current step
    int out = desired_output > 0 ? desired_output : 4096;
    // The output reservation must not swallow the whole window — keep min_input for the prompt.
    int out_ceiling = window - min_input - margin;
    if (out_ceiling < 1024) out_ceiling = 1024;  // pathologically tiny window: still attempt
    if (out > out_ceiling) out = out_ceiling;
    int input_cap = window - out - margin;
    if (input_cap < min_input) input_cap = min_input;
    if (input_cap > kContextTokenCap) input_cap = kContextTokenCap;  // big windows: keep prompts lean
    return CallBudget{input_cap, out};
}

PrewalkConfig load_prewalk_config(const json& cfg_in) {
    PrewalkConfig out;
    json cfg = cfg_in.is_null() ? load_config() : cfg_in;
    if (!cfg.is_object() || !cfg.contains("agent") || !cfg["agent"].is_object()) return out;
    const json& agent = cfg["agent"];
    // agent.prewalk bool — accept truthy numbers/strings like offline_enabled.
    if (agent.contains("prewalk")) {
        const json& v = agent["prewalk"];
        if (v.is_boolean())
            out.enabled = v.get<bool>();
        else if (v.is_number())
            out.enabled = v.get<double>() != 0.0;
        else if (v.is_string())
            out.enabled = !v.get<std::string>().empty() && v.get<std::string>() != "0" &&
                          lower(v.get<std::string>()) != "false" &&
                          lower(v.get<std::string>()) != "off";
    }
    if (agent.contains("prewalk_target") && agent["prewalk_target"].is_string()) {
        std::string t = agent["prewalk_target"].get<std::string>();
        // strip
        size_t a = t.find_first_not_of(" \t\r\n");
        size_t b = t.find_last_not_of(" \t\r\n");
        if (a != std::string::npos) out.target = t.substr(a, b - a + 1);
    }
    if (out.target.empty()) out.target = "@smol";
    return out;
}

namespace {

// Strip a leading '@' role-marker and lower-case for alias compare.
std::string role_alias_key(std::string s) {
    size_t a = s.find_first_not_of(" \t\r\n");
    size_t b = s.find_last_not_of(" \t\r\n");
    if (a == std::string::npos) return "";
    s = s.substr(a, b - a + 1);
    if (!s.empty() && s[0] == '@') s = s.substr(1);
    return lower(s);
}

// Split "provider/model" or "provider:model" (first separator only). Empty model ok.
bool split_provider_model(const std::string& target, std::string& provider, std::string& model) {
    size_t slash = target.find('/');
    size_t colon = target.find(':');
    size_t sep = std::string::npos;
    if (slash != std::string::npos && colon != std::string::npos)
        sep = std::min(slash, colon);
    else if (slash != std::string::npos)
        sep = slash;
    else if (colon != std::string::npos)
        sep = colon;
    if (sep == std::string::npos || sep == 0) return false;
    provider = target.substr(0, sep);
    model = (sep + 1 < target.size()) ? target.substr(sep + 1) : std::string();
    // Drop a trailing thinking suffix like ":high" when the provider used '/'
    // (e.g. openai/gpt-4.1-mini:low) — keep model as-is for simple cases.
    return !provider.empty();
}

}  // namespace

json resolve_prewalk_provider(const json& cfg, const json& env) {
    PrewalkConfig pw = load_prewalk_config(cfg);
    if (!pw.enabled) return json(nullptr);

    std::string target = pw.target;
    if (target.empty()) target = "@smol";
    const std::string alias = role_alias_key(target);

    // Try a concrete ai_roles entry first (role → {provider, model}).
    auto from_ai_role = [&](const std::string& role_key) -> json {
        if (!cfg.is_object() || !cfg.contains("ai_roles") || !cfg["ai_roles"].is_object())
            return json(nullptr);
        const json& roles = cfg["ai_roles"];
        if (!roles.contains(role_key)) return json(nullptr);
        const json& entry = roles[role_key];
        if (entry.is_object() && entry.contains("provider") && entry["provider"].is_string() &&
            !entry["provider"].get<std::string>().empty()) {
            std::string pname = entry["provider"].get<std::string>();
            json desc = resolve_provider(pname, cfg, env);
            if (!desc.is_object() || desc.contains("error")) return json(nullptr);
            if (entry.contains("model") && entry["model"].is_string() &&
                !entry["model"].get<std::string>().empty())
                desc["model"] = entry["model"].get<std::string>();
            return desc;
        }
        return json(nullptr);  // "auto" / unknown → caller falls through
    };

    // @smol / smol / empty → ai_roles.smol, then task, then local default when ambient
    // is already a loopback provider (safe no-op otherwise — never invent a broken target).
    auto resolve_smol = [&]() -> json {
        for (const char* key : {"smol", "task", "tiny"}) {
            json d = from_ai_role(key);
            if (!d.is_null()) return d;
        }
        json cfg_use = cfg.is_object() ? cfg : json::object();
        std::string ambient = lower(cfg_str(cfg_use, "provider").value_or(""));
        // Only fall back to a local descriptor when the ambient provider is already local /
        // empty (empty → openai-local default). Cloud ambient + no ai_roles.smol → null
        // (prewalk no-ops rather than routing build stages at a random loopback server).
        std::optional<std::string> name;
        if (ambient.empty() || ambient == "llamacpp" || ambient == "llama.cpp" ||
            ambient == "llama-cpp" || ambient == "openai") {
            if (ambient == "llamacpp" || ambient == "llama.cpp" || ambient == "llama-cpp")
                name = std::string("llamacpp");
            // For openai: only accept if loopback; otherwise null.
            if (ambient == "openai") {
                json probe = resolve_provider(std::string("openai"), cfg_use, env);
                if (!probe.is_object() || probe.contains("error")) return json(nullptr);
                if (!provider_offline_safe(probe) &&
                    !(probe.contains("base_url") && probe["base_url"].is_string() &&
                      host_is_loopback(probe["base_url"].get<std::string>())))
                    return json(nullptr);
                return probe;
            }
            json desc = resolve_provider(name, cfg_use, env);
            if (!desc.is_object() || desc.contains("error")) return json(nullptr);
            // Empty-name default is always loopback (kDefaultLocalBaseUrl).
            return desc;
        }
        return json(nullptr);
    };

    if (alias.empty() || alias == "smol" || alias == "tiny" || alias == "cheap" ||
        alias == "local") {
        return resolve_smol();
    }

    // Bare role name → ai_roles.<role> fixed assignment.
    {
        json d = from_ai_role(alias);
        if (!d.is_null()) return d;
        // auto / missing concrete assignment for a known cheap-ish role → smol fallback
        if (alias == "task" || alias == "default" || alias == "planning") return resolve_smol();
    }

    // provider/model or provider:model pattern.
    std::string prov, model;
    if (split_provider_model(target, prov, model)) {
        if (!prov.empty() && prov[0] == '@') prov = prov.substr(1);
        json desc = resolve_provider(prov, cfg, env);
        if (!desc.is_object() || desc.contains("error")) return json(nullptr);
        if (!model.empty()) {
            // Drop trailing thinking selector if present (openai/gpt-4.1-mini:low).
            static const char* kThink[] = {":minimal", ":low", ":medium", ":high", ":xhigh",
                                           ":max"};
            for (const char* suf : kThink) {
                const size_t n = std::strlen(suf);
                if (model.size() >= n && lower(model.substr(model.size() - n)) == suf) {
                    model = model.substr(0, model.size() - n);
                    break;
                }
            }
            if (!model.empty()) desc["model"] = model;
        }
        return desc;
    }

    // Bare provider name (anthropic, openai, xai, llamacpp, …).
    json desc = resolve_provider(target, cfg, env);
    if (!desc.is_object() || desc.contains("error")) return json(nullptr);
    return desc;
}

namespace {

// Parse one eng_firm.models entry object → EngFirmModelEntry. Wrong types → empty fields.
EngFirmModelEntry parse_eng_firm_entry(const json& j) {
    EngFirmModelEntry e;
    if (!j.is_object()) return e;
    if (j.contains("provider") && j["provider"].is_string())
        e.provider = j["provider"].get<std::string>();
    if (j.contains("model") && j["model"].is_string()) e.model = j["model"].get<std::string>();
    if (j.contains("thinking") && j["thinking"].is_string())
        e.thinking = j["thinking"].get<std::string>();
    return e;
}

}  // namespace

EngFirmModels load_eng_firm_models(const json& cfg_in) {
    EngFirmModels out;
    try {
        json cfg = cfg_in.is_null() ? load_config() : cfg_in;
        if (!cfg.is_object() || !cfg.contains("eng_firm") || !cfg["eng_firm"].is_object())
            return out;
        const json& ef = cfg["eng_firm"];
        if (!ef.contains("models") || !ef["models"].is_object()) return out;
        const json& m = ef["models"];
        if (m.contains("mission_planner")) out.mission_planner = parse_eng_firm_entry(m["mission_planner"]);
        if (m.contains("advisor")) out.advisor = parse_eng_firm_entry(m["advisor"]);
        if (m.contains("lead")) out.lead = parse_eng_firm_entry(m["lead"]);
        if (m.contains("subcontractor"))
            out.subcontractor = parse_eng_firm_entry(m["subcontractor"]);
        if (m.contains("disciplines") && m["disciplines"].is_object()) {
            for (auto it = m["disciplines"].begin(); it != m["disciplines"].end(); ++it) {
                if (!it.key().empty())
                    out.disciplines[lower(it.key())] = parse_eng_firm_entry(it.value());
            }
        }
    } catch (...) {
        // never throw across the config seam
    }
    return out;
}

json resolve_eng_firm_provider(const EngFirmModelEntry& e, const json& cfg, const json& env) {
    try {
        if (e.empty()) return json(nullptr);

        std::string prov = e.provider;
        std::string model = e.model;
        // Strip whitespace on provider.
        {
            size_t a = prov.find_first_not_of(" \t\r\n");
            size_t b = prov.find_last_not_of(" \t\r\n");
            if (a == std::string::npos)
                prov.clear();
            else
                prov = prov.substr(a, b - a + 1);
        }

        // If provider is empty but model looks like "provider/model", split it.
        // Also honor "provider/model" when provider field itself carries the slash form.
        std::string split_p, split_m;
        if (!prov.empty() && split_provider_model(prov, split_p, split_m)) {
            if (!split_p.empty() && split_p[0] == '@') split_p = split_p.substr(1);
            prov = split_p;
            if (model.empty() && !split_m.empty()) model = split_m;
        }

        if (prov.empty()) return json(nullptr);

        json desc = resolve_provider(prov, cfg, env);
        if (!desc.is_object() || desc.contains("error")) return json(nullptr);

        if (!model.empty()) {
            // Drop trailing thinking selector if present (openai/gpt-4.1-mini:low).
            static const char* kThink[] = {":minimal", ":low", ":medium", ":high", ":xhigh",
                                           ":max"};
            for (const char* suf : kThink) {
                const size_t n = std::strlen(suf);
                if (model.size() >= n && lower(model.substr(model.size() - n)) == suf) {
                    model = model.substr(0, model.size() - n);
                    break;
                }
            }
            if (!model.empty()) desc["model"] = model;
        }

        // thinking → reasoning_effort (same descriptor field other knobs use).
        if (!e.thinking.empty()) {
            std::string th = e.thinking;
            size_t a = th.find_first_not_of(" \t\r\n");
            size_t b = th.find_last_not_of(" \t\r\n");
            if (a != std::string::npos) {
                th = th.substr(a, b - a + 1);
                if (!th.empty()) desc["reasoning_effort"] = th;
            }
        }
        return desc;
    } catch (...) {
        return json(nullptr);
    }
}

std::string eng_firm_discipline_key(Discipline d) {
    switch (d) {
        case Discipline::Mechanical: return "mechanical";
        case Discipline::Electrical: return "electrical";
        case Discipline::Structural: return "structural";
        case Discipline::Thermal: return "thermal";
        case Discipline::Manufacturing: return "manufacturing";
        case Discipline::Drafting: return "drafting";
        case Discipline::Sourcing: return "sourcing";
        case Discipline::Generalist: return "generalist";
    }
    return "generalist";
}

}  // namespace agent
}  // namespace aicad
