// core/agent/config.cpp — see config.h. Mirrors services/agent/config.py resolution.
#include "agent/config.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cstdlib>
#include <exception>
#include <fstream>
#include <sstream>
#include <utility>
#include <vector>

#include "agent/paths.h"
#include "agent/auth.h"  // in-process subscription token refresh

namespace chudware {
namespace agent {

const char* kDefaultLocalModel = "gemma-4-e4b";
const char* kDefaultLocalBaseUrl = "http://localhost:1234/v1";
const char* kOfflineSetting = "offline_mode";

namespace {
// Read the ACTIVE account's access_token from config.subscription_auth[provider_id].
// Token REFRESH lives in agent/auth.cpp: resolve_provider refreshes a stale
// credential in-process via auth::ensure_fresh_for_run and reloads the store before
// this reader runs, so the token returned here is fresh at run start. xAI rotates
// refresh tokens on every exchange — refresh writes back atomically under the
// shared config.json.lock advisory lock that Python also honors.
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

// load_config — mirror services/agent/config.py load_config(): read paths_config()
// (%APPDATA%/Chudware/config.json) and parse it. Returns {} on a missing file or any
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

// WS3: parse cfg.ai_roles.redaction / cfg.redaction into {provider, model?, base_url?}.
// "auto"/absent/malformed -> null. "provider:model" string shorthand supported (mirrors
// redaction.py resolve_redaction_provider's config fallback block). The loopback-only
// hard rule is enforced at USE time (providers.cpp), not here.
json redaction_role_from(const json& cfg) {
    if (!cfg.is_object()) return json(nullptr);
    json assignment = json(nullptr);
    if (cfg.contains("ai_roles") && cfg["ai_roles"].is_object() &&
        cfg["ai_roles"].contains("redaction"))
        assignment = cfg["ai_roles"]["redaction"];
    else if (cfg.contains("redaction"))
        assignment = cfg["redaction"];
    if (assignment.is_null()) return json(nullptr);
    if (assignment.is_string()) {
        std::string s = assignment.get<std::string>();
        if (s.empty() || s == "auto") return json(nullptr);
        size_t colon = s.find(':');
        if (colon != std::string::npos)
            return json{{"provider", s.substr(0, colon)}, {"model", s.substr(colon + 1)}};
        return json{{"provider", s}};
    }
    if (!assignment.is_object()) return json(nullptr);
    json out = json::object();
    if (assignment.contains("provider") && assignment["provider"].is_string())
        out["provider"] = assignment["provider"];
    if (assignment.contains("model") && assignment["model"].is_string())
        out["model"] = assignment["model"];
    if (assignment.contains("base_url") && assignment["base_url"].is_string())
        out["base_url"] = assignment["base_url"];
    if (!out.contains("provider") || out["provider"].get<std::string>().empty())
        return json(nullptr);
    return out;
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

std::string compliance_mode(const json& cfg) {
    // Mirrors redaction.py normalize_compliance_mode + compliance_mode: trim, lowercase,
    // '-' -> '_', alias localonly/local -> local_only; unknown/missing -> "off".
    if (!cfg.is_object() || !cfg.contains("compliance_mode")) return "off";
    const json& v = cfg["compliance_mode"];
    if (!v.is_string()) return "off";
    std::string s = lower(v.get<std::string>());
    while (!s.empty() && (s.front() == ' ' || s.front() == '\t')) s.erase(s.begin());
    while (!s.empty() && (s.back() == ' ' || s.back() == '\t')) s.pop_back();
    std::replace(s.begin(), s.end(), '-', '_');
    if (s == "localonly" || s == "local_only" || s == "local") return "local_only";
    if (s == "off" || s == "redact") return s;
    return "off";
}

bool local_only_enabled(const json& cfg) {
    // offline_mode remains the legacy hard switch; compliance_mode=local_only is the named
    // WS3 equivalent. redact does NOT imply local-only (it wraps cloud traffic instead).
    return offline_enabled(cfg) || compliance_mode(cfg) == "local_only";
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
    // FreeLLMAPI proxies to real cloud providers over the internet even though its default
    // endpoint is a LOCAL router (http://localhost:3001/v1) — never treat it as loopback-safe
    // regardless of the configured base_url.
    if (name == "freellmapi") return false;
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

json resolve_provider(const std::optional<std::string>& name_arg, json cfg,
                      const json& env) {
    // Legacy offline_mode AND compliance_mode=local_only both refuse non-loopback
    // providers (mirrors config.py: offline = offline_enabled or compliance==local_only).
    bool offline = local_only_enabled(cfg);
    // name = (name_arg or env CHUDWARE_PROVIDER or cfg.provider or "").lower()
    std::optional<std::string> name_opt = name_arg;
    if (!name_opt || name_opt->empty()) {
        auto ev = env_get(env, "CHUDWARE_PROVIDER");
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
                    name == "moonshot" || name == "freellmapi"))
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
    // Subscription aliases not recognized by the native transport must not reach
    // token refresh before the unknown-provider error in offline mode.
    const bool subscription_name = auth::is_subscription_name(name);
    if (offline && subscription_name)
        return refusal("The provider '" + name +
                       "' cannot be used while offline mode is enabled. Use a local model "
                       "server or turn offline mode off in Settings.");

    // Refresh a stale subscription OAuth token IN-PROCESS (ported from the
    // Python subscription_ensure_fresh RPC) and re-read the store so the
    // subscription branches below resolve the rotated access token. Runs after the
    // offline refusals (no network in offline mode) and no-ops for non-subscription
    // names, fresh tokens, and absent credentials. A hard auth failure surfaces as
    // a refusal — the same provider_auth gate jobs.cpp used to apply via RPC.
    if (subscription_name) {
        std::string auth_err = auth::ensure_fresh_for_run(name, cfg);
        if (!auth_err.empty()) return refusal(auth_err);
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
    } else if (name == "freellmapi") {
        // FreeLLMAPI (github.com/tashfeenahmed/freellmapi) — a self-hosted OpenAI-compatible
        // router that stacks free tiers from ~28 cloud providers behind one endpoint + one
        // "unified" key. Own config namespace ("freellmapi", not "openai") so it never shares
        // state with a genuine local OpenAI-compatible server, and so provider_offline_safe's
        // hard "always cloud" rule above can key off the NAME alone. Default endpoint is the
        // desktop app / `install.sh` default (http://localhost:3001/v1); "auto" lets the
        // router's own model picker choose the best available free model per request.
        std::optional<std::string> fla_base;
        if (cfg.contains("freellmapi") && cfg["freellmapi"].is_object() &&
            cfg["freellmapi"].contains("base_url") && cfg["freellmapi"]["base_url"].is_string())
            fla_base = cfg["freellmapi"]["base_url"].get<std::string>();
        bool fla_vision = true;
        if (cfg.contains("freellmapi") && cfg["freellmapi"].is_object() &&
            cfg["freellmapi"].contains("vision") && cfg["freellmapi"]["vision"].is_boolean())
            fla_vision = cfg["freellmapi"]["vision"].get<bool>();
        std::optional<std::string> fla_key;
        if (cfg.contains("freellmapi") && cfg["freellmapi"].is_object() &&
            cfg["freellmapi"].contains("api_key") && cfg["freellmapi"]["api_key"].is_string())
            fla_key = cfg["freellmapi"]["api_key"].get<std::string>();
        if (!fla_key || fla_key->empty()) fla_key = env_get(env, "FREELLMAPI_API_KEY");
        desc = json{
            {"name", "freellmapi"},
            {"model", py_or(cfg_str(cfg, "model"), std::string("auto"))},
            {"base_url", py_or(fla_base, std::string("http://localhost:3001/v1"))},
            {"supports_vision", fla_vision},
            {"api_key", fla_key.value_or("")},
        };
    } else if (name == "mock") {
        desc = json{{"name", "mock"}, {"model", nullptr}, {"base_url", nullptr},
                    {"supports_vision", false}, {"api_key", nullptr}};
    } else if (name == "anthropic" || name == "anthropic-sub") {
        // Credential precedence, highest first: an explicit config.json/env API key, then the
        // active Claude subscription OAuth token from config.json's subscription_auth store
        // (already refreshed in-process by auth::ensure_fresh_for_run above).
        // "anthropic-sub" is subscription-only: cfg.api_key is shared across providers and can
        // still hold a key left over from a provider switch, so it is never read for that id.
        const bool sub_only = (name == "anthropic-sub");
        std::string key;
        bool oauth = sub_only;
        if (!sub_only) {
            key = py_or(cfg_str(cfg, "api_key"),
                        env_get(env, "ANTHROPIC_API_KEY").value_or(""));
            if (key.empty() && cfg.contains("keys") && cfg["keys"].is_object() &&
                cfg["keys"].contains("anthropic") && cfg["keys"]["anthropic"].is_string())
                key = cfg["keys"]["anthropic"].get<std::string>();
            if (key.rfind("sk-ant-oat", 0) == 0) oauth = true;
        }
        if (key.empty()) {
            key = subscription_access_token(cfg, "anthropic-sub");
            if (!key.empty()) oauth = true;
        }
        if (key.empty())
            return refusal("Claude is not configured. Sign in or add an Anthropic API key in Settings.");
        // Global cfg.model may be a Grok id when the active provider is xAI — never send
        // a non-Claude model id to the Anthropic Messages API.
        std::string model = py_or(cfg_str(cfg, "model"), "claude-opus-5-5");
        {
            std::string ml = lower(model);
            if (ml.find("claude") == std::string::npos && ml.find("opus") == std::string::npos &&
                ml.find("sonnet") == std::string::npos && ml.find("haiku") == std::string::npos)
                model = "claude-opus-5-5";
        }
        desc = json{
            {"name", oauth ? "anthropic-sub" : "anthropic"},
            {"model", model},
            {"base_url", nullptr},
            {"supports_vision", true},
            {"api_key", key},
        };
        if (oauth) desc["auth_mode"] = "oauth";
    } else if (name == "gemini") {
        // Google Gemini (cloud). Key from cfg.api_key or GEMINI_API_KEY / GOOGLE_API_KEY env.
        std::string key = py_or(cfg_str(cfg, "api_key"), env_get(env, "GEMINI_API_KEY").value_or(""));
        if (key.empty()) key = env_get(env, "GOOGLE_API_KEY").value_or("");
        desc = json{
            {"name", "gemini"},
            {"model", py_or(cfg_str(cfg, "model"), "gemini-3.5-flash")},
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
            {"model", py_or(cfg_str(cfg, "model"), "gpt-6-sol")},
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
        // A stale OAuth token was already refreshed in-process above (auth.cpp).
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
            // next signed-in xai-sub account (read-only at request time; token
            // refresh itself already happened in auth::ensure_fresh_for_run).
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
        return refusal(
            "Cursor has no native local completions transport. Use "
            "agent_admin_turn (agent service) for online providers.");
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
    // WS3 compliance_mode: thread the normalized mode (and, in redact mode, any configured
    // redaction-role assignment) into the descriptor so providers.cpp::complete() can gate
    // the outbound ITAR scrub without re-reading config.json. "off" is the no-op default.
    desc["compliance_mode"] = compliance_mode(cfg);
    if (compliance_mode(cfg) == "redact") {
        json role = redaction_role_from(cfg);
        if (!role.is_null()) desc["redaction_role"] = role;
        // RedactingProvider forces supports_vision off in redact mode so the run loop never
        // attaches screenshots that complete() would then have to drop (cloud only; loopback
        // providers are never wrapped and keep their vision capability).
        if (desc["offline_safe"].get<bool>() == false) desc["supports_vision"] = false;
    }
    return desc;
}

}  // namespace agent
}  // namespace chudware
