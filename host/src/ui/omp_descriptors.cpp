// omp_descriptors.cpp — see omp_descriptors.h. GL/ImGui/AppState-free on purpose.
#include "omp_descriptors.h"

#include <algorithm>

namespace ui {

namespace {

std::string jstr(const nlohmann::json& o, const char* key) {
    if (!o.is_object()) return {};
    const auto it = o.find(key);
    return (it != o.end() && it->is_string()) ? it->get<std::string>() : std::string();
}

std::string jstrAny(const nlohmann::json& o, std::initializer_list<const char*> keys) {
    for (const char* k : keys) {
        std::string v = jstr(o, k);
        if (!v.empty()) return v;
    }
    return {};
}

bool jbool(const nlohmann::json& o, const char* key, bool fallback) {
    if (!o.is_object()) return fallback;
    const auto it = o.find(key);
    return (it != o.end() && it->is_boolean()) ? it->get<bool>() : fallback;
}

int jint(const nlohmann::json& o, const char* key, int fallback) {
    if (!o.is_object()) return fallback;
    const auto it = o.find(key);
    return (it != o.end() && it->is_number()) ? (int)it->get<double>() : fallback;
}

long long jll(const nlohmann::json& o, std::initializer_list<const char*> keys) {
    if (!o.is_object()) return 0;
    for (const char* k : keys) {
        const auto it = o.find(k);
        if (it != o.end() && it->is_number()) return (long long)it->get<double>();
    }
    return 0;
}

// One auth-kind spelling -> the two capability bits.
void applyAuthKind(const std::string& kind, OmpProviderDesc& d) {
    if (kind == "api_key" || kind == "api" || kind == "key") d.hasApiKey = true;
    if (kind == "oauth") d.hasOAuth = true;
}

}  // namespace

bool parseOmpProviderDesc(const nlohmann::json& p, OmpProviderDesc& out) {
    if (!p.is_object()) return false;
    OmpProviderDesc d;
    d.id = jstrAny(p, {"id", "providerId"});
    if (d.id.empty()) return false;
    d.label = jstrAny(p, {"label", "name"});
    if (d.label.empty()) d.label = d.id;

    d.state = jstr(p, "state");
    if (d.state.empty()) {
        // Omp-native spelling: authenticated/connected bool + method/kind string.
        const bool authed = jbool(p, "authenticated", false) || jbool(p, "connected", false);
        if (authed) {
            const std::string method = jstrAny(p, {"method", "kind", "authMethod"});
            d.state = method == "oauth" ? "oauth_connected" :
                      (method == "api_key" || method == "api" || method == "key")
                          ? "key_set" : "configured";
        } else {
            d.state = "signed_out";
        }
    }
    d.origin = jstr(p, "origin");
    d.envVar = jstrAny(p, {"envVar", "env"});
    d.error = jstr(p, "error");
    d.consoleUrl = jstrAny(p, {"consoleUrl", "console_url"});
    d.keyless = jbool(p, "keyless", false);
    d.availabilityKnown = p.contains("available") && p["available"].is_boolean();
    d.available = d.availabilityKnown && p["available"].get<bool>();
    d.modelsAvailable = jint(p, "modelsAvailable", 0);

    bool sawKinds = false;
    for (const char* arrKey : {"authKinds", "methods", "auth"}) {
        const auto it = p.find(arrKey);
        if (it == p.end() || !it->is_array()) continue;
        sawKinds = true;
        for (const auto& k : *it)
            if (k.is_string()) applyAuthKind(k.get<std::string>(), d);
        break;
    }
    if (!sawKinds) {
        // A scalar method string still names ONE capability; a keyless provider needs none.
        const std::string method = jstrAny(p, {"method", "authMethod"});
        if (!method.empty()) applyAuthKind(method, d);
    }
    out = std::move(d);
    return true;
}

bool ompProviderUsable(const std::string& state) {
    return state == "key_set" || state == "oauth_connected" || state == "configured";
}

bool parseOmpLoginEvent(const nlohmann::json& ev, OmpLoginEvent& out) {
    if (!ev.is_object()) return false;
    // Some transports nest the payload under "data"; unwrap exactly one level.
    const nlohmann::json* body = &ev;
    {
        const auto it = ev.find("data");
        if (it != ev.end() && it->is_object() &&
            (it->contains("url") || it->contains("kind") || it->contains("type")))
            body = &*it;
    }
    OmpLoginEvent e;
    e.kind = jstrAny(*body, {"kind", "type", "method", "event"});
    e.url = jstrAny(*body, {"url", "authUrl", "verificationUrl"});
    e.launchUrl = jstrAny(*body, {"launchUrl", "launch_url"});
    e.message = jstrAny(*body, {"message", "instructions", "text", "progress"});
    e.interaction = jstr(*body, "interaction");
    // The worker tags browser hand-offs kind="open_url"; a frame that carries a URL under
    // any spelling is one even when the tag is missing or spelled extension_ui_request.
    if (!e.url.empty() || !e.launchUrl.empty()) e.kind = "open_url";
    if (e.kind.empty() && e.message.empty()) return false;
    out = std::move(e);
    return true;
}



bool ompKeySaveAccepted(const nlohmann::json& r, const std::string& providerId) {
    if (!r.is_object() || !jbool(r, "ok", false)) return false;
    const auto provider = r.find("provider");
    return provider != r.end() && provider->is_object() &&
           jstr(*provider, "providerId") == providerId &&
           jbool(*provider, "authenticated", false) && jbool(*provider, "hasApiKey", false);
}

bool parseOmpSessionStats(const nlohmann::json& r, OmpSessionStats& out) {
    if (!r.is_object()) return false;
    {
        const auto ok = r.find("ok");
        if (ok != r.end() && ok->is_boolean() && !ok->get<bool>()) return false;
    }
    // Fields may sit on the result itself or under a "stats" object.
    const nlohmann::json* body = &r;
    {
        const auto it = r.find("stats");
        if (it != r.end() && it->is_object()) body = &*it;
    }
    OmpSessionStats s;
    for (const char* costKey : {"cost", "usd", "totalUsd", "total_usd"}) {
        const auto it = body->find(costKey);
        if (it != body->end() && it->is_number()) {
            s.usd = it->get<double>();
            s.hasCost = true;
            break;
        }
    }
    {
        const auto tok = body->find("tokens");
        const nlohmann::json& t = (tok != body->end() && tok->is_object())
                                      ? *tok
                                      : *body;   // flat spelling: input/output on the body
        s.tokensIn = jll(t, {"input", "tokensIn", "input_tokens"});
        s.tokensOut = jll(t, {"output", "tokensOut", "output_tokens"});
        s.cacheRead = jll(t, {"cacheRead", "cache_read"});
        s.cacheWrite = jll(t, {"cacheWrite", "cache_write"});
    }
    for (const char* modelsKey : {"models", "perModel", "per_model"}) {
        const auto it = body->find(modelsKey);
        if (it == body->end() || !it->is_array()) continue;
        for (const auto& row : *it) {
            if (!row.is_object()) continue;
            OmpSessionModelRow m;
            m.model = jstrAny(row, {"model", "id"});
            if (m.model.empty()) continue;
            m.provider = jstr(row, "provider");
            const auto usd = row.find("usd");
            if (usd != row.end() && usd->is_number()) m.usd = usd->get<double>();
            const auto tok = row.find("tokens");
            const nlohmann::json& t = (tok != row.end() && tok->is_object()) ? *tok : row;
            m.tokensIn = jll(t, {"input", "tokensIn"});
            m.tokensOut = jll(t, {"output", "tokensOut"});
            m.priced = jbool(row, "priced", usd != row.end() && usd->is_number() &&
                                                usd->get<double>() > 0.0);
            s.models.push_back(std::move(m));
        }
        break;
    }
    out = std::move(s);
    return true;
}


bool parseOmpToolState(const nlohmann::json& result, std::vector<OmpToolRow>& out) {
    if (!result.is_object() || !jbool(result, "ok", false)) return false;
    const auto tools = result.find("tools");
    if (tools == result.end() || !tools->is_array()) return false;
    std::vector<OmpToolRow> rows;
    rows.reserve(tools->size());
    for (const auto& item : *tools) {
        if (!item.is_object()) continue;
        OmpToolRow row;
        row.name = jstr(item, "name");
        if (row.name.empty()) continue;
        row.description = jstr(item, "description");
        row.availabilityKnown = item.contains("available") && item["available"].is_boolean();
        row.available = row.availabilityKnown && item["available"].get<bool>();
        row.builtin = jbool(item, "builtin", false);
        rows.push_back(std::move(row));
    }
    out = std::move(rows);
    return true;
}

const nlohmann::json* ompCompactReplacement(const nlohmann::json& result) {
    if (!result.is_object()) return nullptr;
    for (const char* key : {"messages", "kept_messages"}) {
        const auto it = result.find(key);
        if (it != result.end() && it->is_array() && !it->empty()) return &*it;
    }
    return nullptr;
}

}  // namespace ui
