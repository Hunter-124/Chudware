// core/agent/providers.cpp — C++ port of agent/providers/*.py (see providers.h).
#include "agent/providers.h"

#include <fstream>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

#include "agent/http.h"

namespace aicad {
namespace agent {

const char* kAnthropicDefaultModel = "claude-opus-4-8";
const char* kOpenAIDefaultModel = "gpt-4o";

// --------------------------------------------------------------------------- base.py
json text_part(const std::string& text) { return json{{"type", "text"}, {"text", text}}; }

json image_part(const std::string& path) { return json{{"type", "image"}, {"path", path}}; }

json user_message(const json& parts) { return json{{"role", "user"}, {"content", parts}}; }

// --------------------------------------------------------------------------- helpers
std::string read_file_bytes(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) throw std::runtime_error("cannot open file: " + path);
    std::string data((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    return data;
}

// base64.standard_b64encode — standard alphabet ('+','/'), '=' padding.
std::string base64_encode(const std::string& bytes) {
    static const char tbl[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string out;
    out.reserve(((bytes.size() + 2) / 3) * 4);
    size_t i = 0;
    const size_t n = bytes.size();
    while (i + 3 <= n) {
        unsigned v = (static_cast<unsigned char>(bytes[i]) << 16) |
                     (static_cast<unsigned char>(bytes[i + 1]) << 8) |
                     static_cast<unsigned char>(bytes[i + 2]);
        out.push_back(tbl[(v >> 18) & 0x3F]);
        out.push_back(tbl[(v >> 12) & 0x3F]);
        out.push_back(tbl[(v >> 6) & 0x3F]);
        out.push_back(tbl[v & 0x3F]);
        i += 3;
    }
    const size_t rem = n - i;
    if (rem == 1) {
        unsigned v = static_cast<unsigned char>(bytes[i]) << 16;
        out.push_back(tbl[(v >> 18) & 0x3F]);
        out.push_back(tbl[(v >> 12) & 0x3F]);
        out.push_back('=');
        out.push_back('=');
    } else if (rem == 2) {
        unsigned v = (static_cast<unsigned char>(bytes[i]) << 16) |
                     (static_cast<unsigned char>(bytes[i + 1]) << 8);
        out.push_back(tbl[(v >> 18) & 0x3F]);
        out.push_back(tbl[(v >> 12) & 0x3F]);
        out.push_back(tbl[(v >> 6) & 0x3F]);
        out.push_back('=');
    }
    return out;
}

namespace {
// Read part["type"] / part["text"] / part["path"] as std::string (tolerant).
std::string pstr(const json& part, const char* key) {
    auto it = part.find(key);
    if (it != part.end() && it->is_string()) return it->get<std::string>();
    return std::string();
}
}  // namespace

// --------------------------------------------------------------------------- anthropic
json anthropic_block(const json& part) {
    if (pstr(part, "type") == "text") {
        return json{{"type", "text"}, {"text", part.value("text", std::string())}};
    }
    std::string data = base64_encode(read_file_bytes(pstr(part, "path")));
    return json{
        {"type", "image"},
        {"source",
         {{"type", "base64"}, {"media_type", "image/png"}, {"data", data}}},
    };
}

BuiltRequest anthropic_build_request(const std::string& model, const std::string& system,
                                     const json& messages, int max_tokens) {
    json api_messages = json::array();
    for (const auto& m : messages) {
        json content = json::array();
        for (const auto& p : m.at("content")) content.push_back(anthropic_block(p));
        api_messages.push_back(json{{"role", m.at("role")}, {"content", content}});
    }
    json body = {
        {"model", model},
        {"max_tokens", max_tokens},
        {"system", system},
        {"messages", api_messages},
        {"thinking", {{"type", "adaptive"}}},
        {"output_config", {{"effort", "high"}}},
    };
    BuiltRequest r;
    r.url = "https://api.anthropic.com/v1/messages";
    r.headers = {
        {"x-api-key", ""},  // filled by complete(); builder keeps it pure/inspectable
        {"anthropic-version", "2023-06-01"},
        {"content-type", "application/json"},
    };
    r.body_json = body;
    r.body = body.dump();
    return r;
}

ParsedReply anthropic_parse_response(const json& resp) {
    ParsedReply out;
    auto it = resp.find("content");
    if (it == resp.end() || !it->is_array()) return out;
    for (const auto& b : *it) {
        std::string ty = b.value("type", std::string());
        if (ty == "text" && b.contains("text") && b["text"].is_string()) {
            out.text += b["text"].get<std::string>();
        } else if (ty == "thinking" && b.contains("thinking") && b["thinking"].is_string()) {
            out.thinking += b["thinking"].get<std::string>();
        }
    }
    return out;
}

// --------------------------------------------------------------------------- openai
std::string openai_normalize_base_url(const std::string& base_url) {
    if (base_url.empty()) return base_url;  // Python: `if not base_url: return base_url`
    std::string u = base_url;
    while (!u.empty() && u.back() == '/') u.pop_back();  // rstrip("/")

    // Parse like urllib.parse.urlparse: scheme://netloc/path...  We only need to
    // know whether a scheme + netloc are present and whether the path is "" or "/".
    std::string scheme, netloc, path;
    auto scheme_end = u.find("://");
    std::string rest;
    if (scheme_end != std::string::npos) {
        scheme = u.substr(0, scheme_end);
        rest = u.substr(scheme_end + 3);
    } else {
        rest = u;  // no scheme
    }
    if (!scheme.empty()) {
        // netloc is up to the next '/', '?' or '#'.
        auto slash = rest.find_first_of("/?#");
        if (slash == std::string::npos) {
            netloc = rest;
            path = "";
        } else {
            netloc = rest.substr(0, slash);
            // path is the segment up to a query/fragment.
            std::string after = rest.substr(slash);
            auto qf = after.find_first_of("?#");
            path = (qf == std::string::npos) ? after : after.substr(0, qf);
        }
    }
    // Note: u was already rstripped of "/", so a bare host yields path=="" and a
    // host with a single trailing slash also yields path=="" (the slash is gone);
    // urlparse on the original would give "/" — both are covered by the in-set test.
    bool path_empty_or_slash = (path.empty() || path == "/");
    if (!scheme.empty() && !netloc.empty() && path_empty_or_slash) {
        u += "/v1";
    }
    return u;
}

json openai_content(const json& part) {
    if (pstr(part, "type") == "text") {
        return json{{"type", "text"}, {"text", part.value("text", std::string())}};
    }
    std::string data = base64_encode(read_file_bytes(pstr(part, "path")));
    return json{{"type", "image_url"},
                {"image_url", {{"url", "data:image/png;base64," + data}}}};
}

json openai_message(const std::string& role, const json& parts) {
    bool all_text = true;
    for (const auto& p : parts) {
        if (pstr(p, "type") != "text") {
            all_text = false;
            break;
        }
    }
    if (all_text) {
        std::string joined;
        bool first = true;
        for (const auto& p : parts) {
            if (!first) joined += "\n\n";
            joined += p.value("text", std::string());
            first = false;
        }
        return json{{"role", role}, {"content", joined}};
    }
    json arr = json::array();
    for (const auto& p : parts) arr.push_back(openai_content(p));
    return json{{"role", role}, {"content", arr}};
}

BuiltRequest openai_build_request(const std::string& model, const std::string& system,
                                  const json& messages, int max_tokens,
                                  const std::string& base_url) {
    std::string nbase = openai_normalize_base_url(base_url);

    json msgs = json::array();
    msgs.push_back(json{{"role", "system"}, {"content", system}});
    for (const auto& m : messages) {
        msgs.push_back(openai_message(m.at("role").get<std::string>(), m.at("content")));
    }
    json body = {{"model", model}, {"messages", msgs}, {"max_tokens", max_tokens}};

    std::string root = nbase.empty() ? std::string("https://api.openai.com/v1") : nbase;

    BuiltRequest r;
    r.url = root + "/chat/completions";
    r.headers = {
        {"Authorization", "Bearer "},  // filled by complete()
        {"content-type", "application/json"},
    };
    r.body_json = body;
    r.body = body.dump();
    return r;
}

ParsedReply openai_parse_response(const json& resp) {
    ParsedReply out;  // text default ""
    auto it = resp.find("choices");
    if (it == resp.end() || !it->is_array() || it->empty()) return out;  // no choices -> ""
    const json& msg = (*it)[0].value("message", json::object());
    if (msg.contains("content") && msg["content"].is_string()) {
        out.text = msg["content"].get<std::string>();
    }
    // (content == null) -> "" (Python `... or ""`); covered by the default.
    return out;
}

// --------------------------------------------------------------------------- mock
std::string MockProvider::complete() {
    if (replies_.empty()) return std::string();
    size_t idx = i_ < replies_.size() ? i_ : replies_.size() - 1;
    std::string reply = replies_[idx];
    ++i_;
    return reply;
}

std::string RoleScriptedProvider::role_of(const std::string& system) {
    std::string s;
    s.reserve(system.size());
    for (char c : system) s.push_back(static_cast<char>(std::tolower((unsigned char)c)));

    auto starts = [&](const char* pfx) { return s.rfind(pfx, 0) == 0; };
    if (starts("role:")) {
        // first line after "role:" up to '\n', then strip the value.
        std::string first = s.substr(0, s.find('\n'));
        std::string val = first.substr(first.find(':') + 1);
        // strip() — leading/trailing whitespace.
        size_t a = val.find_first_not_of(" \t\r\n");
        size_t b = val.find_last_not_of(" \t\r\n");
        if (a == std::string::npos) return std::string();
        return val.substr(a, b - a + 1);
    }
    if (starts("you are the intake")) return "intake";
    if (s.find("planning a cad build") != std::string::npos) return "planner";
    if (starts("you are the modeler")) return "modeler";
    if (starts("you are the reviewer")) return "reviewer";
    if (starts("you are the fixer")) return "fixer";
    if (starts("you are the memory")) return "memory";
    return "?";
}

std::string RoleScriptedProvider::complete(const std::string& system) {
    std::string role = role_of(system);
    auto it = by_role_.find(role);
    if (it == by_role_.end() || !it->is_array() || it->empty()) {
        return "MOCK-MISSING-ROLE:" + role;
    }
    json& queue = *it;
    if (queue.size() > 1) {
        std::string reply = queue.front().get<std::string>();
        queue.erase(queue.begin());  // pop(0)
        return reply;
    }
    return queue.front().get<std::string>();
}

// --------------------------------------------------------------------------- complete
CompleteResult complete(const std::string& provider, const json& cfg, const std::string& system,
                        const json& messages, int max_tokens) {
    CompleteResult res;

    if (provider == "mock") {
        std::vector<std::string> replies;
        if (cfg.contains("replies") && cfg["replies"].is_array())
            for (const auto& r : cfg["replies"]) replies.push_back(r.get<std::string>());
        MockProvider mp(std::move(replies));
        res.text = mp.complete();
        return res;
    }

    std::string api_key = cfg.value("api_key", std::string());
    std::string base_url = cfg.value("base_url", std::string());

    if (provider == "anthropic") {
        std::string model = cfg.value("model", std::string(kAnthropicDefaultModel));
        BuiltRequest req = anthropic_build_request(model, system, messages, max_tokens);
        for (auto& h : req.headers)
            if (h.first == "x-api-key") h.second = api_key;
        HttpResponse hr = http_post(req.url, req.headers, req.body, max_tokens > 0 ? 120000 : 120000);
        if (!hr.error.empty()) {
            res.error = hr.error;
            return res;
        }
        if (hr.status < 200 || hr.status >= 300) {
            res.error = "anthropic HTTP " + std::to_string(hr.status) + ": " + hr.body;
            return res;
        }
        ParsedReply pr = anthropic_parse_response(json::parse(hr.body, nullptr, false));
        res.text = pr.text;
        res.thinking = pr.thinking;
        return res;
    }

    if (provider == "openai" || provider == "llamacpp") {
        std::string def = (provider == "llamacpp") ? std::string("local-gguf")
                                                    : std::string(kOpenAIDefaultModel);
        std::string model = cfg.value("model", def);
        BuiltRequest req = openai_build_request(model, system, messages, max_tokens, base_url);
        for (auto& h : req.headers)
            if (h.first == "Authorization")
                h.second = "Bearer " + (api_key.empty() ? std::string("not-needed") : api_key);
        HttpResponse hr = http_post(req.url, req.headers, req.body);
        if (!hr.error.empty()) {
            res.error = hr.error;
            return res;
        }
        if (hr.status < 200 || hr.status >= 300) {
            res.error = "openai HTTP " + std::to_string(hr.status) + ": " + hr.body;
            return res;
        }
        json parsed = json::parse(hr.body, nullptr, false);
        ParsedReply pr = openai_parse_response(parsed);
        if (pr.text.empty()) {
            // Mirror the oracle: no choices -> surface an error (caller decides).
            if (!parsed.contains("choices") || !parsed["choices"].is_array() ||
                parsed["choices"].empty()) {
                res.error = "openai endpoint returned no completion";
                return res;
            }
        }
        res.text = pr.text;
        return res;
    }

    res.error = "unknown provider: " + provider;
    return res;
}

// --------------------------------------------------------------------------- assist provider
AssistCompleteFn make_assist_provider(const json& descriptor) {
    json d = descriptor;
    // {error:"..."} -> signals a provider error (the except branch in the oracle).
    if (d.contains("error") && d["error"].is_string() && !d["error"].get<std::string>().empty()) {
        std::string err = d["error"].get<std::string>();
        return [err](const std::string&, const std::string&, std::string* e) -> std::string {
            if (e) *e = err;
            return std::string();
        };
    }
    std::string name = d.value("name", std::string());
    if (name == "mock" && d.contains("by_role") && d["by_role"].is_object()) {
        auto rp = std::make_shared<RoleScriptedProvider>(d["by_role"]);
        return [rp](const std::string& system, const std::string&, std::string*) -> std::string {
            return rp->complete(system);
        };
    }
    if (name == "mock") {
        std::vector<std::string> replies;
        if (d.contains("replies") && d["replies"].is_array())
            for (const auto& r : d["replies"]) replies.push_back(r.get<std::string>());
        auto mp = std::make_shared<MockProvider>(std::move(replies));
        return [mp](const std::string&, const std::string&, std::string*) -> std::string {
            return mp->complete();
        };
    }
    // Real provider: route a single completion through complete() (live network).
    json cfg = d;
    return [name, cfg](const std::string& system, const std::string& body,
                       std::string* e) -> std::string {
        json messages = json::array({user_message(json::array({text_part(body)}))});
        CompleteResult cr = complete(name, cfg, system, messages, 1024);
        if (!cr.error.empty() && e) *e = cr.error;
        return cr.text;
    };
}

}  // namespace agent
}  // namespace aicad
