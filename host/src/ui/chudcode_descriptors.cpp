// chudcode_descriptors.cpp — see chudcode_descriptors.h. GL/ImGui/AppState-free on purpose.
#include "chudcode_descriptors.h"

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
void applyAuthKind(const std::string& kind, ChudcodeProviderDesc& d) {
    if (kind == "api_key" || kind == "api" || kind == "key") d.hasApiKey = true;
    if (kind == "oauth") d.hasOAuth = true;
}

}  // namespace

bool parseChudcodeProviderDesc(const nlohmann::json& p, ChudcodeProviderDesc& out) {
    if (!p.is_object()) return false;
    ChudcodeProviderDesc d;
    d.id = jstrAny(p, {"id", "providerId"});
    if (d.id.empty()) return false;
    d.label = jstrAny(p, {"label", "name"});
    if (d.label.empty()) d.label = d.id;

    d.state = jstr(p, "state");
    if (d.state.empty()) {
        // Chudcode-native spelling: authenticated/connected bool + method/kind string.
        const bool authed = jbool(p, "authenticated", false) || jbool(p, "connected", false);
        if (authed) {
            const std::string method = jstrAny(p, {"method", "kind", "authMethod"});
            d.state = (method == "oauth") ? "oauth_connected" : "key_set";
        } else {
            d.state = "signed_out";
        }
    }
    d.origin = jstr(p, "origin");
    d.envVar = jstrAny(p, {"envVar", "env"});
    d.error = jstr(p, "error");
    d.consoleUrl = jstrAny(p, {"consoleUrl", "console_url"});
    d.keyless = jbool(p, "keyless", false);
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

bool chudcodeProviderUsable(const std::string& state) {
    return state == "key_set" || state == "oauth_connected";
}

bool parseChudcodeLoginEvent(const nlohmann::json& ev, ChudcodeLoginEvent& out) {
    if (!ev.is_object()) return false;
    // Some transports nest the payload under "data"; unwrap exactly one level.
    const nlohmann::json* body = &ev;
    {
        const auto it = ev.find("data");
        if (it != ev.end() && it->is_object() &&
            (it->contains("url") || it->contains("kind") || it->contains("type")))
            body = &*it;
    }
    ChudcodeLoginEvent e;
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

bool parseChudcodeSttResult(const nlohmann::json& r, ChudcodeSttResult& out) {
    if (!r.is_object() || !jbool(r, "ok", false)) return false;
    ChudcodeSttResult s;
    s.available = jbool(r, "available", false);
    s.state = jstr(r, "state");
    if (s.state != "idle" && s.state != "recording" && s.state != "transcribing")
        return false;
    s.model = jstr(r, "model");
    const auto progress = r.find("progress");
    if (progress != r.end() && progress->is_number())
        s.progress = std::clamp((int)progress->get<double>(), 0, 100);
    s.error = jstrAny(r, {"error", "reason"});
    s.text = jstr(r, "text");
    out = std::move(s);
    return true;
}

std::string composeChudcodeTranscript(const std::string& base, const std::string& committed,
                                     const std::string& partial) {
    std::string out = base;
    const auto append = [&out](const std::string& piece) {
        if (piece.empty()) return;
        if (!out.empty() && out.back() != ' ' && out.back() != '\n') out.push_back(' ');
        out += piece;
    };
    append(committed);
    append(partial);
    return out;
}

bool chudcodeKeyValidationAccepted(const nlohmann::json& r, const std::string& providerId) {
    return r.is_object() && jbool(r, "ok", false) && jbool(r, "valid", false) &&
           jstr(r, "providerId") == providerId;
}

bool chudcodeKeySaveAccepted(const nlohmann::json& r, const std::string& providerId) {
    if (!r.is_object() || !jbool(r, "ok", false)) return false;
    const auto provider = r.find("provider");
    return provider != r.end() && provider->is_object() &&
           jstr(*provider, "providerId") == providerId &&
           jbool(*provider, "authenticated", false) && jbool(*provider, "hasApiKey", false);
}

bool parseChudcodeSessionStats(const nlohmann::json& r, ChudcodeSessionStats& out) {
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
    ChudcodeSessionStats s;
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
            ChudcodeSessionModelRow m;
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

bool parseChudcodeToolState(const nlohmann::json& r, std::vector<ChudcodeToolRow>& out) {
    if (!r.is_object()) return false;
    {
        const auto ok = r.find("ok");
        if (ok != r.end() && ok->is_boolean() && !ok->get<bool>()) return false;
    }
    const auto tools = r.find("tools");
    if (tools == r.end() || !tools->is_array()) return false;
    std::vector<ChudcodeToolRow> rows;
    rows.reserve(tools->size());
    for (const auto& t : *tools) {
        if (!t.is_object()) continue;
        ChudcodeToolRow row;
        row.name = jstrAny(t, {"name", "id"});
        if (row.name.empty()) continue;
        row.loadMode = jstrAny(t, {"loadMode", "load", "load_mode", "mode"});
        row.description = jstr(t, "description");
        row.builtin = jbool(t, "builtin", true);
        row.available = jbool(t, "available", true);
        rows.push_back(std::move(row));
    }
    out = std::move(rows);
    return true;
}

const nlohmann::json* chudcodeCompactReplacement(const nlohmann::json& result) {
    if (!result.is_object()) return nullptr;
    for (const char* key : {"messages", "kept_messages"}) {
        const auto it = result.find(key);
        if (it != result.end() && it->is_array() && !it->empty()) return &*it;
    }
    return nullptr;
}

}  // namespace ui
