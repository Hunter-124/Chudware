// core/agent/providers.cpp — C++ port of agent/providers/*.py (see providers.h).
#include "agent/providers.h"

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cctype>
#include <chrono>
#include <fstream>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <unordered_set>
#include <vector>

#include "agent/http.h"
#include "agent/omp_bridge_client.h"

namespace aicad {
namespace agent {
namespace {
// Per-thread sink so a design/micro run can observe every HTTP completion without
// threading RunEvents through complete()'s call graph.
thread_local std::function<void(const json&)> g_provider_call_emitter;
// Per-thread sink for incremental token deltas (streaming completions). kind is
// "text" or "thinking". nullptr = no live streaming (behaviour == pre-streaming).
thread_local std::function<void(const std::string&, const std::string&)> g_provider_delta_emitter;
// Active run cancel flag (shared with HTTP xfer callback via set_http_cancel_flag).
thread_local const std::atomic<bool>* g_provider_cancel = nullptr;

// Incremental Server-Sent-Events accumulator for OpenAI-compatible `stream:true`
// chat completions. feed() is called per received network buffer; it reassembles
// `data:` lines across buffer boundaries, accumulates content + reasoning deltas,
// and forwards each delta to the live emitter as it arrives.
struct SseAccum {
    std::string text;
    std::string thinking;
    std::string carry;
    void reset() { text.clear(); thinking.clear(); carry.clear(); }
    void feed(const char* data, size_t len,
              const std::function<void(const std::string&, const std::string&)>& emit) {
        carry.append(data, len);
        size_t nl;
        while ((nl = carry.find('\n')) != std::string::npos) {
            std::string line = carry.substr(0, nl);
            carry.erase(0, nl + 1);
            if (!line.empty() && line.back() == '\r') line.pop_back();
            if (line.rfind("data:", 0) != 0) continue;  // skip ':' comments / event: / blanks
            std::string payload = line.substr(5);
            const size_t s = payload.find_first_not_of(' ');
            if (s == std::string::npos) continue;
            payload = payload.substr(s);
            if (payload == "[DONE]") continue;
            json j = json::parse(payload, nullptr, false);
            if (j.is_discarded() || !j.contains("choices") || !j["choices"].is_array() ||
                j["choices"].empty())
                continue;
            const json& d = j["choices"][0].value("delta", json::object());
            if (d.contains("content") && d["content"].is_string()) {
                const std::string c = d["content"].get<std::string>();
                if (!c.empty()) { text += c; if (emit) emit(c, "text"); }
            }
            for (const char* k : {"reasoning_content", "reasoning"}) {
                if (d.contains(k) && d[k].is_string()) {
                    const std::string c = d[k].get<std::string>();
                    if (!c.empty()) { thinking += c; if (emit) emit(c, "thinking"); }
                    break;
                }
            }
        }
    }
};

std::string truncate_excerpt(std::string s, size_t n = 200) {
    // Collapse whitespace so a multi-line JSON error body becomes one chat-friendly line.
    for (char& c : s) {
        if (c == '\n' || c == '\r' || c == '\t') c = ' ';
    }
    // Squeeze runs of spaces.
    std::string out;
    out.reserve(s.size());
    bool sp = false;
    for (char c : s) {
        if (c == ' ') {
            if (!sp) out.push_back(c);
            sp = true;
        } else {
            out.push_back(c);
            sp = false;
        }
    }
    while (!out.empty() && out.front() == ' ') out.erase(out.begin());
    while (!out.empty() && out.back() == ' ') out.pop_back();
    if (out.size() > n) out = out.substr(0, n - 3) + "...";
    return out;
}

std::string request_id_from_body(const std::string& body) {
    if (body.empty()) return {};
    json j = json::parse(body, nullptr, false);
    if (!j.is_object()) return {};
    // OpenAI/xAI: top-level "id"; Anthropic: "id" or error.error.request_id.
    if (j.contains("id") && j["id"].is_string()) return j["id"].get<std::string>();
    if (j.contains("request_id") && j["request_id"].is_string())
        return j["request_id"].get<std::string>();
    if (j.contains("error") && j["error"].is_object()) {
        const auto& e = j["error"];
        if (e.contains("request_id") && e["request_id"].is_string())
            return e["request_id"].get<std::string>();
    }
    return {};
}

void emit_provider_call(const CompleteResult& res, const std::string& provider) {
    auto& emit = g_provider_call_emitter;
    if (!emit) return;
    json ev = {
        {"kind", "provider_call"},
        {"provider", provider},
        {"endpoint", res.endpoint},
        {"model", res.model},
        {"http_status", res.http_status},
        {"latency_ms", res.latency_ms},
        {"request_id", res.request_id},
        {"attempt", res.attempt > 0 ? res.attempt : 1},
        {"error_excerpt", res.error_excerpt},
        {"ok", res.error.empty()},
    };
    try {
        emit(ev);
    } catch (...) {
    }
}
}  // namespace

void set_provider_call_emitter(std::function<void(const json&)> emit) {
    g_provider_call_emitter = std::move(emit);
}
std::function<void(const json&)>& provider_call_emitter() { return g_provider_call_emitter; }

void set_provider_delta_emitter(std::function<void(const std::string&, const std::string&)> emit) {
    g_provider_delta_emitter = std::move(emit);
}
std::function<void(const std::string&, const std::string&)>& provider_delta_emitter() {
    return g_provider_delta_emitter;
}

void set_provider_cancel_flag(const std::atomic<bool>* flag) {
    g_provider_cancel = flag;
    set_http_cancel_flag(flag);
}
const std::atomic<bool>* provider_cancel_flag() { return g_provider_cancel; }


const char* kAnthropicDefaultModel = "claude-opus-4-8";
const char* kOpenAIDefaultModel = "gpt-4o";

// --------------------------------------------------------------------------- base.py
json text_part(const std::string& text) { return json{{"type", "text"}, {"text", text}}; }

json image_part(const std::string& path) { return json{{"type", "image"}, {"path", path}}; }

json user_message(const json& parts) { return json{{"role", "user"}, {"content", parts}}; }

// --------------------------------------------------------------------------- helpers
// Read a string field tolerating a present-but-NULL value. nlohmann's value(key,default)
// throws type_error.302 when the key EXISTS but is null (it only returns the default when
// the key is ABSENT). The resolved provider descriptor sets api_key/base_url to null on
// purpose (e.g. gemini has base_url:null; llamacpp/local has api_key:null), so value() here
// would throw -> the run aborts and the host masks it as "unknown method: agent_run_v2".
std::string str_or(const json& cfg, const char* key, const std::string& dflt = std::string()) {
    auto it = cfg.find(key);
    if (it == cfg.end() || !it->is_string()) return dflt;
    return it->get<std::string>();
}

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
        return json{{"type", "text"}, {"text", pstr(part, "text")}};
    }
    std::string data = base64_encode(read_file_bytes(pstr(part, "path")));
    return json{
        {"type", "image"},
        {"source",
         {{"type", "base64"}, {"media_type", "image/png"}, {"data", data}}},
    };
}

namespace {

// Lowercase copy (ASCII).
std::string lower_copy(std::string s) {
    for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

// Strip provider / date / bedrock prefixes so model ids parse cleanly.
std::string normalize_claude_model_id(std::string m) {
    m = lower_copy(std::move(m));
    // Trim.
    auto a = m.find_first_not_of(" \t\r\n");
    auto b = m.find_last_not_of(" \t\r\n");
    if (a == std::string::npos) return "";
    m = m.substr(a, b - a + 1);
    // Keep only the last path segment (provider/model).
    auto slash = m.find_last_of('/');
    if (slash != std::string::npos) m = m.substr(slash + 1);
    // anthropic. / au.anthropic. / eu.anthropic. bedrock-style prefixes.
    const char* prefs[] = {"anthropic.", "au.anthropic.", "eu.anthropic."};
    for (const char* p : prefs) {
        size_t n = std::char_traits<char>::length(p);
        if (m.size() >= n && m.compare(0, n, p) == 0) {
            m = m.substr(n);
            break;
        }
    }
    // Drop trailing -vN:M (bedrock version suffix).
    auto vpos = m.rfind("-v");
    if (vpos != std::string::npos) {
        bool ok = true;
        size_t i = vpos + 2;
        if (i >= m.size() || !std::isdigit(static_cast<unsigned char>(m[i]))) ok = false;
        while (ok && i < m.size() && std::isdigit(static_cast<unsigned char>(m[i]))) ++i;
        if (ok && i < m.size() && m[i] == ':') {
            ++i;
            if (i >= m.size() || !std::isdigit(static_cast<unsigned char>(m[i]))) ok = false;
            while (ok && i < m.size() && std::isdigit(static_cast<unsigned char>(m[i]))) ++i;
            if (ok && i == m.size()) m = m.substr(0, vpos);
        }
    }
    // Drop trailing YYYYMMDD date stamps so they never become "major".
    if (m.size() > 9 && m[m.size() - 9] == '-') {
        bool digits = true;
        for (size_t i = m.size() - 8; i < m.size(); ++i)
            if (!std::isdigit(static_cast<unsigned char>(m[i]))) {
                digits = false;
                break;
            }
        if (digits) m = m.substr(0, m.size() - 9);
    }
    return m;
}

// Parse "kind" + version tuple from a Claude model id.
// kind: opus|sonnet|haiku|fable|mythos|other
// version: e.g. (4,5), (4,8), (5,) — empty when unknown.
struct ClaudeModelId {
    std::string kind;
    int major = -1;
    int minor = -1;  // -1 = absent
};

bool extract_version_after(const std::string& m, size_t pos, int* major, int* minor) {
    // pos points at first digit of major.
    if (pos >= m.size() || !std::isdigit(static_cast<unsigned char>(m[pos]))) return false;
    size_t i = pos;
    int maj = 0;
    while (i < m.size() && std::isdigit(static_cast<unsigned char>(m[i]))) {
        maj = maj * 10 + (m[i] - '0');
        ++i;
    }
    *major = maj;
    *minor = -1;
    if (i < m.size() && (m[i] == '-' || m[i] == '_' || m[i] == '.')) {
        size_t j = i + 1;
        if (j < m.size() && std::isdigit(static_cast<unsigned char>(m[j]))) {
            size_t start = j;
            int minv = 0;
            while (j < m.size() && std::isdigit(static_cast<unsigned char>(m[j]))) {
                minv = minv * 10 + (m[j] - '0');
                ++j;
            }
            // Guard: an 8-digit "minor" is a date that escaped the strip.
            if ((j - start) < 6) *minor = minv;
        }
    }
    return true;
}

ClaudeModelId parse_claude_model(const std::string& model) {
    ClaudeModelId out;
    out.kind = "other";
    std::string m = normalize_claude_model_id(model);
    if (m.empty()) return out;

    const char* kinds[] = {"opus", "sonnet", "haiku", "fable", "mythos"};
    for (const char* k : kinds) {
        if (m.find(k) != std::string::npos) {
            out.kind = k;
            break;
        }
    }

    if (out.kind != "other") {
        // Legacy: claude-3-5-sonnet / claude-3-opus  →  major before kind.
        // Look for "claude[-_](\d+)(?:[-_.](\d+))?[-_]<kind>"
        std::string needle = out.kind;
        size_t kpos = m.find(needle);
        // Prefer a match that has digits just before the kind (legacy shape).
        bool legacy = false;
        if (kpos != std::string::npos && kpos >= 2) {
            // Walk back over separator + digits.
            size_t i = kpos;
            if (m[i - 1] == '-' || m[i - 1] == '_') {
                size_t j = i - 1;
                // optional minor digits
                size_t dig_end = j;
                while (j > 0 && std::isdigit(static_cast<unsigned char>(m[j - 1]))) --j;
                if (j < dig_end) {
                    // have digits; maybe another separator + major
                    size_t minor_start = j;
                    if (j > 0 && (m[j - 1] == '-' || m[j - 1] == '_' || m[j - 1] == '.')) {
                        size_t j2 = j - 1;
                        size_t maj_end = j2;
                        while (j2 > 0 && std::isdigit(static_cast<unsigned char>(m[j2 - 1]))) --j2;
                        if (j2 < maj_end) {
                            // legacy with minor: claude-3-5-sonnet
                            int maj = 0, minv = 0;
                            for (size_t t = j2; t < maj_end; ++t) maj = maj * 10 + (m[t] - '0');
                            for (size_t t = minor_start; t < dig_end; ++t)
                                minv = minv * 10 + (m[t] - '0');
                            // Require "claude" somewhere before.
                            if (m.find("claude") != std::string::npos && (dig_end - minor_start) < 6) {
                                out.major = maj;
                                out.minor = minv;
                                legacy = true;
                            }
                        }
                    }
                    if (!legacy) {
                        // single version number before kind: claude-3-opus
                        int maj = 0;
                        for (size_t t = j; t < dig_end; ++t) maj = maj * 10 + (m[t] - '0');
                        if (m.find("claude") != std::string::npos) {
                            out.major = maj;
                            out.minor = -1;
                            legacy = true;
                        }
                    }
                }
            }
        }
        if (!legacy) {
            // Modern: claude-sonnet-4-5 / claude-sonnet-5 / claude-opus-4-8
            // Find kind then digits after separator.
            size_t p = m.find(out.kind);
            if (p != std::string::npos) {
                size_t after = p + out.kind.size();
                if (after < m.size() && (m[after] == '-' || m[after] == '_')) {
                    int maj = 0, minv = -1;
                    if (extract_version_after(m, after + 1, &maj, &minv)) {
                        out.major = maj;
                        out.minor = minv;
                    }
                }
            }
        }
    }

    if (out.major < 0) {
        // Fallback: first claude[-_]?N[.N]
        size_t p = m.find("claude");
        if (p != std::string::npos) {
            size_t i = p + 6;
            if (i < m.size() && (m[i] == '-' || m[i] == '_')) ++i;
            int maj = 0, minv = -1;
            if (extract_version_after(m, i, &maj, &minv)) {
                out.major = maj;
                out.minor = minv;
            }
        }
    }
    return out;
}

std::string normalize_effort(const std::string& raw) {
    std::string effort = lower_copy(raw.empty() ? "high" : raw);
    // trim
    auto a = effort.find_first_not_of(" \t\r\n");
    auto b = effort.find_last_not_of(" \t\r\n");
    effort = (a == std::string::npos) ? "" : effort.substr(a, b - a + 1);
    if (effort == "med") effort = "medium";
    if (effort == "auto" || effort == "none" || effort == "off" || effort.empty())
        effort = "high";
    if (effort != "low" && effort != "medium" && effort != "high" && effort != "max" &&
        effort != "xhigh")
        effort = "high";
    return effort;
}

int budget_tokens_for(const std::string& effort, int max_tokens) {
    int want = 4096;
    if (effort == "low") want = 1024;
    else if (effort == "medium") want = 4096;
    else if (effort == "high") want = 8192;
    else if (effort == "xhigh") want = 16384;
    else if (effort == "max") want = 24576;
    int cap = std::max(1, max_tokens - 1);
    int budget = std::min(want, cap);
    if (budget < 1024 && cap >= 1024) budget = 1024;
    return std::max(1, budget);
}

}  // namespace

std::string anthropic_thinking_mode_for_model(const std::string& model) {
    ClaudeModelId id = parse_claude_model(model);
    if (id.kind == "fable" || id.kind == "mythos") return "adaptive";
    if (id.major < 0) {
        // Unknown Claude-ish id: prefer adaptive only for clearly modern names.
        std::string mid = lower_copy(model);
        const char* adaptive_hints[] = {"opus-4-6", "opus-4-7", "opus-4-8", "sonnet-4-6",
                                        "sonnet-4-7", "sonnet-5", "opus-5", "haiku-4-6"};
        for (const char* h : adaptive_hints)
            if (mid.find(h) != std::string::npos) return "adaptive";
        if (mid.find("claude") != std::string::npos) return "budget";
        return "none";
    }
    int major = id.major;
    int minor = id.minor < 0 ? 0 : id.minor;
    // Gen 5 (sonnet-5 / opus-5): adaptive.
    if (major >= 5) return "adaptive";
    // 4.6+: adaptive for all kinds.
    if (major == 4 && minor >= 6) return "adaptive";
    // Opus 4.5: budget_tokens + output_config.effort.
    if (id.kind == "opus" && major == 4 && minor >= 5) return "budget_effort";
    // Sonnet/Haiku 4.5 and older: budget_tokens only (effort 400s).
    if (major >= 3) return "budget";
    return "none";
}

BuiltRequest anthropic_build_request(const std::string& model, const std::string& system,
                                     const json& messages, int max_tokens,
                                     const std::string& reasoning_effort) {
    json api_messages = json::array();
    for (const auto& m : messages) {
        json content = json::array();
        for (const auto& p : m.at("content")) content.push_back(anthropic_block(p));
        api_messages.push_back(json{{"role", m.at("role")}, {"content", content}});
    }

    const std::string effort = normalize_effort(reasoning_effort);
    const std::string mode = anthropic_thinking_mode_for_model(model);
    int effective_max = max_tokens;
    bool send_effort = false;
    std::string wire_effort = effort;

    json body = {
        {"model", model},
        {"max_tokens", effective_max},
        {"system", system},
        {"messages", api_messages},
    };

    if (mode == "adaptive") {
        // Adaptive models reject temperature/top_p/budget_tokens; effort rides on output_config.
        // Map our "xhigh" through as-is (API accepts it).
        // MUST be an object — json{{"type","adaptive"}} is an array under nlohmann.
        body["thinking"] = json::object({{"type", "adaptive"}});
        body["output_config"] = json::object({{"effort", wire_effort}});
        send_effort = true;
    } else if (mode == "budget" || mode == "budget_effort") {
        int budget = budget_tokens_for(effort, max_tokens);
        // If the caller's max_tokens is too tight for a real budget, lift it so the
        // request is legal (budget_tokens must be < max_tokens).
        if (budget >= effective_max) effective_max = budget + 1024;
        body["max_tokens"] = effective_max;
        body["thinking"] = json::object({{"type", "enabled"}, {"budget_tokens", budget}});
        if (mode == "budget_effort") {
            // Opus 4.5: effort alongside budget_tokens. xhigh → max on the wire.
            if (wire_effort == "xhigh") wire_effort = "max";
            body["output_config"] = json::object({{"effort", wire_effort}});
            send_effort = true;
        }
    }
    // mode == "none": no thinking / effort fields.

    BuiltRequest r;
    r.url = "https://api.anthropic.com/v1/messages";
    r.headers = {
        {"x-api-key", ""},  // filled by complete(); builder keeps it pure/inspectable
        {"anthropic-version", "2023-06-01"},
        {"content-type", "application/json"},
    };
    // omp: output_config.effort requires the effort-2025-11-24 beta header;
    // without it some endpoints 400 the field even on adaptive-capable models.
    if (send_effort) {
        r.headers.push_back({"anthropic-beta", "effort-2025-11-24"});
    }
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
        return json{{"type", "text"}, {"text", pstr(part, "text")}};
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
            joined += pstr(p, "text");
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

// Reasoning-effort wire mapping for the OpenAI-compatible family. The GUI sends ONE effort
// level (off/low/medium/high); each provider/model accepts a DIFFERENT set and sending an
// unsupported value 400s. Returns a JSON object to MERGE into the request body (empty = omit).
// Verified against live provider docs (2026-07):
//   OpenAI gpt-5*/o1/o3/o4    reasoning_effort: low|medium|high   (non-reasoning gpt-4*: omit)
//   xAI grok-4.5/4.3/-fast/3-mini reasoning_effort: low|medium|high (original grok-4: omit)
//   Moonshot kimi-k3          reasoning_effort: "max" ONLY         (kimi-k2.x: omit -> thinking)
//   DeepSeek reasoner/v4      reasoning_effort: high|max           -> "high" (deepseek-chat: omit)
//   Alibaba Qwen (DashScope)  enable_thinking:true (top-level bool; no low/med/high level)
//   OpenRouter                reasoning:{effort: low|medium|high}
//   local / unknown           omit (never 400 on an unsupported param)
json openai_reasoning_patch(const std::string& provider, const std::string& model,
                            const std::string& base_url, const std::string& effort) {
    auto low = [](std::string s) {
        for (auto& c : s) c = static_cast<char>(std::tolower((unsigned char)c));
        return s;
    };
    std::string lvl = low(effort);
    if (lvl.empty() || lvl == "off" || lvl == "none") return json::object();
    const std::string m = low(model), b = low(base_url), p = low(provider);
    auto has = [](const std::string& h, const char* n) { return h.find(n) != std::string::npos; };
    // GUI level -> the low|medium|high set most reasoning models accept.
    auto lmh = [&]() -> std::string {
        if (lvl == "low" || lvl == "medium" || lvl == "high") return lvl;
        if (lvl == "minimal") return "low";
        return "high";  // xhigh / max / any other on-value -> high
    };
    // Local endpoints have no reasoning_effort knob.
    if (has(b, "localhost") || has(b, "127.0.0.1") || has(b, "::1") || has(b, "0.0.0.0"))
        return json::object();
    // --- ENDPOINT-identified services (base_url or explicit provider name) win over model-name
    // heuristics, so a vendor-prefixed OpenRouter slug (deepseek/*, qwen/*, moonshotai/*) routes
    // by its ENDPOINT, not the slug prefix. ---
    if (has(b, "openrouter"))  // OpenRouter unified reasoning object (ignored per non-reasoning model)
        return json{{"reasoning", json{{"effort", lmh()}}}};
    if (p == "moonshot" || has(b, "moonshot.ai")) {
        if (has(m, "kimi-k3")) return json{{"reasoning_effort", "max"}};  // K3: only "max"
        return json::object();  // kimi-k2.x uses the `thinking` field, not reasoning_effort
    }
    if (p == "xai" || p == "xai-sub" || has(b, "x.ai")) {
        if (m == "grok-4" || m == "grok-4-0709") return json::object();  // original grok-4: no knob
        return json{{"reasoning_effort", lmh()}};
    }
    if (has(b, "deepseek.com")) {  // DeepSeek official API: only high|max valid
        if (has(m, "reasoner") || has(m, "v4-pro") || has(m, "v4-flash") || has(m, "deepseek-v4"))
            return json{{"reasoning_effort", "high"}};
        return json::object();  // deepseek-chat: non-thinking
    }
    if (has(b, "aliyuncs") || has(b, "dashscope"))  // Qwen (DashScope): hybrid thinking is a
        return json{{"enable_thinking", true}};      // top-level bool (no low/med/high level)
    if (has(b, "openai.com")) {  // OpenAI proper: only gpt-5+/o-series accept reasoning_effort
        if (m.rfind("gpt-5", 0) == 0 || m.rfind("o1", 0) == 0 || m.rfind("o3", 0) == 0 ||
            m.rfind("o4", 0) == 0)
            return json{{"reasoning_effort", lmh()}};
        return json::object();
    }
    // --- No endpoint match: fall back to MODEL-NAME heuristics (bare provider name / empty base). ---
    if (m.rfind("kimi", 0) == 0) {
        if (has(m, "kimi-k3")) return json{{"reasoning_effort", "max"}};
        return json::object();
    }
    if (m.rfind("grok", 0) == 0) {
        if (m == "grok-4" || m == "grok-4-0709") return json::object();
        return json{{"reasoning_effort", lmh()}};
    }
    if (m.rfind("deepseek", 0) == 0) {
        if (has(m, "reasoner") || has(m, "v4-pro") || has(m, "v4-flash"))
            return json{{"reasoning_effort", "high"}};
        return json::object();
    }
    if (m.rfind("qwen", 0) == 0)
        return json{{"enable_thinking", true}};
    if (p == "openai" && b.empty() &&
        (m.rfind("gpt-5", 0) == 0 || m.rfind("o1", 0) == 0 || m.rfind("o3", 0) == 0 ||
         m.rfind("o4", 0) == 0))
        return json{{"reasoning_effort", lmh()}};
    return json::object();  // unknown OpenAI-compatible endpoint: omit (safe)
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
    //
    // Reasoning models served over the OpenAI-compatible API (LM Studio, llama.cpp, vLLM,
    // DeepSeek-R1, Qwen-think, gemma-think, …) emit their chain-of-thought in a separate
    // `reasoning_content` field (some use `reasoning`). Capture it as thinking — but never
    // alias it into `text`: extract_json_obj must parse the model's ACTIONS, not its
    // scratchpad. A content-empty reply stays empty (the caller retries/fails honestly).
    for (const char* k : {"reasoning_content", "reasoning"}) {
        if (msg.contains(k) && msg[k].is_string() && !msg[k].get<std::string>().empty()) {
            out.thinking = msg[k].get<std::string>();
            break;
        }
    }
    return out;
}

// --------------------------------------------------------------------------- gemini
// Google Gemini (generativelanguage REST: models/{model}:generateContent).
const char* kGeminiDefaultModel = "gemini-2.5-flash";

// One content part -> a Gemini part: text -> {text}; image -> {inline_data:{mime_type,data}}.
json gemini_part(const json& part) {
    if (pstr(part, "type") == "text") {
        return json{{"text", pstr(part, "text")}};
    }
    std::string data = base64_encode(read_file_bytes(pstr(part, "path")));
    return json{{"inline_data", {{"mime_type", "image/png"}, {"data", data}}}};
}

// effort -> thinkingConfig.thinkingBudget. Empty/"off" -> omit (model default).
// 2.5-pro cannot disable thinking (budget 0 is rejected), so we never send 0 by default.
static bool gemini_thinking_budget(const std::string& effort, int* out) {
    std::string e;
    for (char c : effort) e.push_back(static_cast<char>(std::tolower((unsigned char)c)));
    if (e == "low") { *out = 1024; return true; }
    if (e == "medium" || e == "med") { *out = 8192; return true; }
    if (e == "high" || e == "max") { *out = 24576; return true; }
    if (e == "off" || e == "none") { *out = 0; return true; }
    return false;  // unknown/empty -> let the model decide (dynamic)
}

BuiltRequest gemini_build_request(const std::string& model, const std::string& system,
                                  const json& messages, int max_tokens,
                                  const std::string& reasoning_effort) {
    // contents: one entry per message. role "user" stays "user"; anything else -> "model".
    json contents = json::array();
    for (const auto& m : messages) {
        json parts = json::array();
        for (const auto& p : m.at("content")) parts.push_back(gemini_part(p));
        std::string role = m.value("role", std::string("user"));
        if (role != "user" && role != "model") role = (role == "assistant") ? "model" : "user";
        contents.push_back(json{{"role", role}, {"parts", parts}});
    }
    json gen = json{{"maxOutputTokens", max_tokens}};
    int budget = 0;
    if (gemini_thinking_budget(reasoning_effort, &budget)) {
        gen["thinkingConfig"] = json{{"thinkingBudget", budget}};
        // Gemini 2.5 counts thinking tokens against maxOutputTokens. If the thinking budget
        // meets or exceeds the output cap, the model spends the whole cap thinking and returns
        // EMPTY content (finishReason MAX_TOKENS) -> the caller sees "no JSON ops in reply" and
        // every stage fails. At "medium" (8192) the budget equalled the cap and at "high" (24576)
        // it dwarfed it. Give the actual answer room ON TOP of the thinking budget: max_tokens is
        // the intended answer size, so the real cap is thinking + answer.
        if (budget > 0) gen["maxOutputTokens"] = budget + max_tokens;
    }
    json body = {{"contents", contents}, {"generationConfig", gen}};
    if (!system.empty()) {
        body["system_instruction"] = json{{"parts", json::array({json{{"text", system}}})}};
    }
    BuiltRequest r;
    r.url = "https://generativelanguage.googleapis.com/v1beta/models/" + model +
            ":generateContent";
    r.headers = {
        {"x-goog-api-key", ""},  // filled by complete()
        {"content-type", "application/json"},
    };
    r.body_json = body;
    r.body = body.dump();
    return r;
}

ParsedReply gemini_parse_response(const json& resp) {
    ParsedReply out;
    auto cit = resp.find("candidates");
    if (cit == resp.end() || !cit->is_array() || cit->empty()) return out;
    const json& cand = (*cit)[0];
    const json content = cand.value("content", json::object());
    auto pit = content.find("parts");
    if (pit == content.end() || !pit->is_array()) return out;
    for (const auto& p : *pit) {
        if (p.contains("text") && p["text"].is_string()) {
            // A "thought" part is reasoning, not the answer; keep it out of the reply text.
            if (p.value("thought", false))
                out.thinking += p["text"].get<std::string>();
            else
                out.text += p["text"].get<std::string>();
        }
    }
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

// POST with transparent retry on rate-limit / transient overload (HTTP 429/503). Cloud free
// tiers (esp. Gemini flash) rate-limit, and an agent run fires ~10 calls — without this a single
// transient 429 mid-run fails the whole build. Exponential backoff: 1s, 2s, 4s, 8s (4 retries).
namespace {
// A HARD daily-quota 429 (vs a transient per-minute rate-limit) won't clear for hours — the quota
// resets at midnight, not in seconds — so backing off 15s per call just wastes the user's time
// across the ~10 calls of a run. Detect the per-day / free-tier quota markers and fail fast.
bool is_daily_quota_exhausted(const HttpResponse& hr) {
    if (hr.status != 429) return false;
    const std::string& b = hr.body;
    return b.find("PerDay") != std::string::npos ||
           b.find("free_tier") != std::string::npos ||
           b.find("FreeTier") != std::string::npos;
}
// Returns the response plus 1-based attempt count (1 = first try, >1 after 429/503 retries).
struct RetryResult {
    HttpResponse hr;
    int attempt = 1;
};
RetryResult http_post_retry(const std::string& url,
                            const std::vector<std::pair<std::string, std::string>>& headers,
                            const std::string& body) {
    RetryResult out;
    if (http_cancel_requested()) {
        out.hr.error = "cancelled";
        return out;
    }
    out.hr = http_post(url, headers, body);
    int delay_ms = 1000;
    for (int attempt = 0; attempt < 4 && out.hr.error.empty() &&
                          (out.hr.status == 429 || out.hr.status == 503); ++attempt) {
        if (http_cancel_requested()) {
            out.hr.error = "cancelled";
            out.hr.status = 0;
            out.hr.body.clear();
            break;
        }
        if (is_daily_quota_exhausted(out.hr)) break;  // retrying won't help today — surface it now
        std::this_thread::sleep_for(std::chrono::milliseconds(delay_ms));
        delay_ms *= 2;
        out.hr = http_post(url, headers, body);
        out.attempt = attempt + 2;  // 1-based: first retry => 2
    }
    return out;
}
}  // namespace

// Turn a verbose provider HTTP-error body into one concise, actionable line for the run timeline
// (which only shows the LAST line — a raw JSON body shows up as a useless "}"). A 429 in particular
// should tell the user WHAT to do (enable billing / switch to a local model / wait for reset).
std::string concise_http_error(const std::string& provider, int status, const std::string& body) {
    // Best-effort: pull human message from {"error":{"message":"..."}} (Anthropic/OpenAI) or
    // {"error":{"message":"..."}} Gemini shape. Prefer this over a raw JSON body — the run
    // timeline only shows the last line, so a raw body surfaces as a useless "}".
    auto extract_msg = [&]() -> std::string {
        json j = json::parse(body, nullptr, false);
        if (j.is_discarded() || !j.is_object()) return "";
        if (j.contains("error") && j["error"].is_object() && j["error"].contains("message") &&
            j["error"]["message"].is_string())
            return j["error"]["message"].get<std::string>();
        if (j.contains("message") && j["message"].is_string())
            return j["message"].get<std::string>();
        return "";
    };

    if (status == 429) {
        std::string msg = extract_msg();
        bool daily = body.find("PerDay") != std::string::npos ||
                     body.find("free_tier") != std::string::npos ||
                     body.find("FreeTier") != std::string::npos;
        std::string head = provider + (daily ? " daily quota exhausted (HTTP 429, free tier)"
                                              : " rate limit hit (HTTP 429)");
        std::string tail = daily ? " — enable billing on your API key, switch to a local model, or "
                                   "try again after the daily quota resets."
                                 : " — wait a moment and retry, or switch to a local model.";
        if (!msg.empty()) {
            // Keep only the first sentence of the provider message; drop the URLs/retry noise.
            size_t dot = msg.find(". ");
            if (dot != std::string::npos) msg = msg.substr(0, dot + 1);
            return head + ": " + msg + tail;
        }
        return head + "." + tail;
    }
    std::string msg = extract_msg();
    if (!msg.empty()) {
        // One clean line: "anthropic HTTP 400: This model does not support the effort parameter"
        size_t nl = msg.find('\n');
        if (nl != std::string::npos) msg = msg.substr(0, nl);
        return provider + " HTTP " + std::to_string(status) + ": " + msg;
    }
    return provider + " HTTP " + std::to_string(status) + ": " + body;
}

std::string humanize_provider_error(const std::string& raw) {
    if (raw.empty()) return "The model provider failed.";
    std::string s = raw;
    // Strip leading "provider error: " so we classify the payload.
    const char* pref = "provider error: ";
    if (s.size() >= 16 && s.compare(0, 16, pref) == 0) s = s.substr(16);
    std::string low = s;
    for (char& c : low) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));

    auto has = [&](const char* n) { return low.find(n) != std::string::npos; };

    // Transport / connectivity
    if (has("could not connect") || has("connection refused") || has("failed to connect") ||
        has("couldn't connect") || has("name or service not known") || has("resolve host") ||
        has("network is unreachable") || has("no route to host") || has("curl error") ||
        (has("ssl") && has("error")) || has("tls")) {
        if (has("refused") || has("127.0.0.1") || has("localhost"))
            return "Could not reach the model server (connection refused).";
        return "Could not reach the model provider (network error).";
    }
    if (has("timeout") || has("timed out") || has("deadline exceeded"))
        return "The model provider timed out. Try again or lower reasoning effort.";

    // Auth
    if (has("http 401") || has("unauthorized") || has("invalid_api_key") ||
        has("invalid x-api-key") || has("authentication"))
        return "Authentication failed — sign in again or check the API key in Settings.";
    if (has("http 403") || has("forbidden") || has("tier not") || has("not api-enabled") ||
        has("permission"))
        return "Access denied by the provider (forbidden / tier). Check subscription status.";

    // Rate / quota
    if (has("daily quota") || has("free tier") || has("freetier") || has("perday"))
        return "Daily free-tier quota exhausted. Enable billing or switch providers.";
    if (has("http 429") || has("rate limit") || has("rate_limit") || has("too many requests") ||
        has("spending-limit") || has("spending limit") || has("out of credits") ||
        has("run out of credits"))
        return "Rate limit or spending limit hit. Wait and retry, or switch providers.";

    // Model / request shape
    if (has("http 404") && has("model"))
        return "That model id is not available on this account. Pick another model in Settings.";
    if (has("http 400") || has("invalid_request") || has("does not support"))
        return "The provider rejected the request (bad parameters or unsupported model feature).";
    if (has("http 5") || has("internal server") || has("bad gateway") || has("service unavailable") ||
        has("http 502") || has("http 503") || has("http 504"))
        return "The model provider had a server error. Retry in a moment.";

    // Cursor / missing transport
    if ((has("no c++") && has("transport")) || has("connect-rpc"))
        return "This provider has no in-app chat transport. Pick xAI, Claude, or OpenAI.";

    // Generic HTTP
    if (has("http 4") || has("http 5"))
        return "The model provider returned an error. See details below.";

    // Resolution / config
    if (has("not signed in") || has("not configured") || has("missing api_key") ||
        has("missing access_token"))
        return "Provider not signed in. Open Settings → AI → Accounts.";
    if (has("unknown provider") || has("offline mode"))
        return s;  // already human

    // Fallback: first line of the raw message, capped.
    size_t nl = s.find('\n');
    if (nl != std::string::npos) s = s.substr(0, nl);
    if (s.size() > 160) s = s.substr(0, 157) + "...";
    return s.empty() ? "The model provider failed." : s;
}

namespace {
// --------------------------------------------------------------------------- omp bridge
// Build the bridge's messages[] wire shape from CAD's existing base.py-mirrored part
// representation (text_part/image_part/user_message above). CAD's parts already carry
// {type:"text",text} / {type:"image",path}, which is the exact CadPart shape
// vendor/omp/bridge/src/bridge.ts's readPart/convertPart expects — this only re-validates and
// filters to the two part kinds the bridge accepts, mirroring anthropic_block/openai_content/
// gemini_part's same-shaped per-provider builders above (each is also a pure part -> wire-part
// function).
json omp_bridge_part(const json& part) {
    if (pstr(part, "type") == "image") {
        json p = json{{"type", "image"}, {"path", pstr(part, "path")}};
        // Pass inline base64 data straight through when the caller supplied it instead of a
        // path (image_part() above only ever builds path-based parts today; kept so a future
        // caller can hand the bridge already-loaded bytes without a round-trip through disk).
        if (part.contains("data") && part["data"].is_string()) p["data"] = part["data"];
        if (part.contains("mime") && part["mime"].is_string()) p["mime"] = part["mime"];
        return p;
    }
    return json{{"type", "text"}, {"text", pstr(part, "text")}};
}

json omp_bridge_messages(const json& messages) {
    json out = json::array();
    for (const auto& m : messages) {
        std::string role = m.value("role", std::string("user"));
        if (role != "assistant") role = "user";  // bridge.ts normalizes the same way
        json content = json::array();
        if (m.contains("content") && m["content"].is_array())
            for (const auto& p : m["content"]) content.push_back(omp_bridge_part(p));
        out.push_back(json{{"role", role}, {"content", content}});
    }
    return out;
}

}  // namespace

namespace {
// Provider ids routed through the OMP bun bridge (core/agent/omp_bridge_client.h,
// vendor/omp/bridge/src/bridge.ts) instead of a native libcurl/WinHTTP branch below.
//
// TODO(omp-bridge-wiring): empty until the orchestrator populates this with the real catalog
// provider ids once the provider picker + sign-in UI land (docs/omp-integration/
// 01-DECISIONS.md "Providers"; ids must match what OMP's pi-catalog getBundledProviders()
// returns, since providers.cpp forwards `provider` verbatim as the bridge's {provider,model}
// params below). Until populated, is_omp_bridge_provider() always returns false and every
// provider keeps resolving through its existing native branch.
const std::unordered_set<std::string>& omp_bridge_provider_ids() {
    static const std::unordered_set<std::string> kOmpBridgeProviders = {};
    return kOmpBridgeProviders;
}
}  // namespace

bool is_omp_bridge_provider(const std::string& provider) {
    return omp_bridge_provider_ids().count(provider) != 0;
}

// --------------------------------------------------------------------------- complete
CompleteResult complete(const std::string& provider, const json& cfg, const std::string& system,
                        const json& messages, int max_tokens) {
    CompleteResult res;
    auto t0 = std::chrono::steady_clock::now();
    auto finish = [&](CompleteResult r) -> CompleteResult {
        r.latency_ms = static_cast<int>(
            std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - t0)
                .count());
        if (r.attempt <= 0) r.attempt = 1;
        if (!r.error.empty() && r.error_excerpt.empty())
            r.error_excerpt = truncate_excerpt(r.error);
        emit_provider_call(r, provider);
        return r;
    };

    // Cooperative cancel — checked before any network work and between retries.
    if (http_cancel_requested() ||
        (g_provider_cancel && g_provider_cancel->load(std::memory_order_acquire))) {
        res.error = "cancelled";
        return finish(std::move(res));
    }

    if (provider == "mock") {
        std::vector<std::string> replies;
        if (cfg.contains("replies") && cfg["replies"].is_array())
            for (const auto& r : cfg["replies"]) replies.push_back(r.get<std::string>());
        MockProvider mp(std::move(replies));
        res.text = mp.complete();
        res.model = "mock";
        res.endpoint = "mock";
        return finish(std::move(res));
    }

    std::string api_key = str_or(cfg, "api_key");
    std::string base_url = str_or(cfg, "base_url");
    std::string auth_mode = str_or(cfg, "auth_mode");
    auto is_oauth_key = [](const std::string& k) {
        return k.rfind("sk-ant-oat", 0) == 0;
    };

    // OMP bridge — providers routed to the bun sidecar (core/agent/omp_bridge_client.h)
    // instead of a native branch below. See is_omp_bridge_provider()'s TODO for how the
    // provider set gets populated; this branch is additive and never shadows a native
    // provider id. Contract keys threaded through: name (== provider), model, max_tokens,
    // reasoning_effort — no temperature (docs/omp-integration/01-DECISIONS.md).
    if (is_omp_bridge_provider(provider)) {
        std::string model = str_or(cfg, "model");
        std::string effort = str_or(cfg, "reasoning_effort");
        json params = {
            {"provider", provider},
            {"model", model},
            {"system", system},
            {"messages", omp_bridge_messages(messages)},
            {"maxTokens", max_tokens},
        };
        if (!api_key.empty()) params["apiKey"] = api_key;
        if (!base_url.empty()) params["baseUrl"] = base_url;
        if (!effort.empty()) params["reasoningEffort"] = effort;

        json result;
        std::string bridge_err;
        // 300 s: handleComplete() in bridge.ts awaits the FULL (non-streamed-to-us)
        // completion before responding, so this has to tolerate a slow reasoning-model turn,
        // not just RPC round-trip latency.
        const bool ok = OmpBridgeClient::instance().call("complete", params, result, bridge_err,
                                                          /*timeoutMs=*/300000);
        res.model = model;
        res.endpoint = "omp-bridge:" + provider;
        if (!ok) {
            res.error = "omp-bridge " + provider + ": " +
                        (bridge_err.empty() ? std::string("transport error") : bridge_err);
            res.error_excerpt = truncate_excerpt(res.error);
            return finish(std::move(res));
        }
        res.text = result.value("text", std::string());
        res.thinking = result.value("thinking", std::string());
        if (result.value("stopReason", std::string()) == "error") {
            std::string em = result.value("errorMessage", std::string("provider error"));
            if (result.contains("errorStatus") && result["errorStatus"].is_number())
                res.http_status = result["errorStatus"].get<int>();
            res.error = "omp-bridge " + provider + ": " + em;
            res.error_excerpt = truncate_excerpt(em);
            return finish(std::move(res));
        }
        if (res.text.empty() && res.thinking.empty()) {
            res.error = "omp-bridge " + provider + ": endpoint returned no completion";
            res.error_excerpt = truncate_excerpt(res.error);
            return finish(std::move(res));
        }
        // NOTE: result["usage"] (bridge.ts collapse()) has no CompleteResult field yet — cost
        // accounting (01-DECISIONS.md "Cost + cache") is a later phase; not dropped silently,
        // just not threaded through until that struct grows a usage member.
        return finish(std::move(res));
    }

    // Anthropic Messages API — classic x-api-key OR Claude Pro/Max OAuth bearer.
    // OAuth requires the Claude-Code-shaped system array + CCH billing hash + betas.
    // OAuth tokens arrive already refreshed (auth::ensure_fresh_for_run, T-RF4).
    if (provider == "anthropic" || provider == "anthropic-sub") {
        std::string model = str_or(cfg, "model", std::string(kAnthropicDefaultModel));
        std::string effort = str_or(cfg, "reasoning_effort");
        const bool oauth = (provider == "anthropic-sub") || auth_mode == "oauth" ||
                           is_oauth_key(api_key);
        if (api_key.empty()) {
            res.error = oauth ? "anthropic-sub: no access_token (sign in via Settings → Accounts)"
                              : "anthropic: missing api_key";
            res.model = model;
            res.endpoint = "https://api.anthropic.com/v1/messages";
            return finish(std::move(res));
        }

        BuiltRequest req =
            anthropic_build_request(model, system, messages, max_tokens, effort);

        if (oauth) {
            // OAuth: Authorization Bearer + Claude Code betas (not x-api-key).
            // System must be the 3-block array with billing header + CCH integrity.
            std::string first_user;
            for (const auto& m : messages) {
                if (m.value("role", std::string()) != "user") continue;
                if (!m.contains("content") || !m["content"].is_array()) break;
                for (const auto& p : m["content"]) {
                    if (pstr(p, "type") == "text") first_user += pstr(p, "text");
                }
                break;
            }
            // billing suffix + CCH (mirrors anthropic_sub_provider.py)
            auto ch = [&](size_t i) -> char {
                return i < first_user.size() ? first_user[i] : '0';
            };
            const char* kClaudeCodeVer = "2.1.165";
            std::string k;
            k.push_back(ch(4));
            k.push_back(ch(7));
            k.push_back(ch(20));
            // SHA-256 truncated — use a simple FNV-ish fallback if we lack OpenSSL;
            // match Python: sha256("59cf53e54c78"+k+ver).hexdigest()[:3]
            std::string suffix_src = std::string("59cf53e54c78") + k + kClaudeCodeVer;
            // Portable SHA-256 via openssl command is too heavy; use the same
            // xxh-independent approach as a compact FNV64 then hex[:3] only if
            // needed. Prefer real sha256 when available via system crypto.
            // Inline SHA-256 (public domain compact).
            auto sha256_hex = [](const std::string& s) -> std::string {
                // Minimal SHA-256 implementation.
                static const uint32_t K[64] = {
                    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
                    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
                    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
                    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
                    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
                    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
                    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
                    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
                auto rotr = [](uint32_t x, uint32_t n) { return (x >> n) | (x << (32 - n)); };
                uint32_t h0=0x6a09e667,h1=0xbb67ae85,h2=0x3c6ef372,h3=0xa54ff53a,
                         h4=0x510e527f,h5=0x9b05688c,h6=0x1f83d9ab,h7=0x5be0cd19;
                std::vector<uint8_t> data(s.begin(), s.end());
                uint64_t bitlen = static_cast<uint64_t>(data.size()) * 8;
                data.push_back(0x80);
                while ((data.size() % 64) != 56) data.push_back(0);
                for (int i = 7; i >= 0; --i) data.push_back(static_cast<uint8_t>((bitlen >> (i * 8)) & 0xff));
                for (size_t chunk = 0; chunk < data.size(); chunk += 64) {
                    uint32_t w[64];
                    for (int i = 0; i < 16; ++i)
                        w[i] = (data[chunk+i*4]<<24)|(data[chunk+i*4+1]<<16)|(data[chunk+i*4+2]<<8)|data[chunk+i*4+3];
                    for (int i = 16; i < 64; ++i) {
                        uint32_t s0 = rotr(w[i-15],7)^rotr(w[i-15],18)^(w[i-15]>>3);
                        uint32_t s1 = rotr(w[i-2],17)^rotr(w[i-2],19)^(w[i-2]>>10);
                        w[i] = w[i-16] + s0 + w[i-7] + s1;
                    }
                    uint32_t a=h0,b=h1,c=h2,d=h3,e=h4,f=h5,g=h6,h=h7;
                    for (int i = 0; i < 64; ++i) {
                        uint32_t S1 = rotr(e,6)^rotr(e,11)^rotr(e,25);
                        uint32_t chv = (e&f)^((~e)&g);
                        uint32_t t1 = h + S1 + chv + K[i] + w[i];
                        uint32_t S0 = rotr(a,2)^rotr(a,13)^rotr(a,22);
                        uint32_t maj = (a&b)^(a&c)^(b&c);
                        uint32_t t2 = S0 + maj;
                        h=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
                    }
                    h0+=a; h1+=b; h2+=c; h3+=d; h4+=e; h5+=f; h6+=g; h7+=h;
                }
                char buf[65];
                std::snprintf(buf, sizeof(buf), "%08x%08x%08x%08x%08x%08x%08x%08x",
                              h0,h1,h2,h3,h4,h5,h6,h7);
                return std::string(buf);
            };
            std::string suffix = sha256_hex(suffix_src).substr(0, 3);

            // xxhash64 CCH with seed 0x4D659218E32A3268 (Anthropic subscription integrity).
            auto xxh64 = [](const std::string& input, uint64_t seed) -> uint64_t {
                const uint64_t PRIME1 = 11400714785074694791ULL;
                const uint64_t PRIME2 = 14029467366897019727ULL;
                const uint64_t PRIME3 = 1609587929392839161ULL;
                const uint64_t PRIME4 = 9650029242287828579ULL;
                const uint64_t PRIME5 = 2870177450012600261ULL;
                auto rotl = [](uint64_t x, int r) { return (x << r) | (x >> (64 - r)); };
                const uint8_t* p = reinterpret_cast<const uint8_t*>(input.data());
                const uint8_t* const end = p + input.size();
                uint64_t h;
                if (input.size() >= 32) {
                    uint64_t v1 = seed + PRIME1 + PRIME2;
                    uint64_t v2 = seed + PRIME2;
                    uint64_t v3 = seed + 0;
                    uint64_t v4 = seed - PRIME1;
                    const uint8_t* const limit = end - 32;
                    do {
                        auto get64 = [](const uint8_t* q) {
                            uint64_t r;
                            std::memcpy(&r, q, 8);
                            return r;
                        };
                        v1 = rotl(v1 + get64(p) * PRIME2, 31) * PRIME1; p += 8;
                        v2 = rotl(v2 + get64(p) * PRIME2, 31) * PRIME1; p += 8;
                        v3 = rotl(v3 + get64(p) * PRIME2, 31) * PRIME1; p += 8;
                        v4 = rotl(v4 + get64(p) * PRIME2, 31) * PRIME1; p += 8;
                    } while (p <= limit);
                    h = rotl(v1,1)+rotl(v2,7)+rotl(v3,12)+rotl(v4,18);
                    auto round_merge = [&](uint64_t acc, uint64_t v) {
                        v = rotl(v * PRIME2, 31) * PRIME1;
                        acc ^= v;
                        return acc * PRIME1 + PRIME4;
                    };
                    h = round_merge(h, v1);
                    h = round_merge(h, v2);
                    h = round_merge(h, v3);
                    h = round_merge(h, v4);
                } else {
                    h = seed + PRIME5;
                }
                h += static_cast<uint64_t>(input.size());
                auto get64 = [](const uint8_t* q) {
                    uint64_t r; std::memcpy(&r, q, 8); return r;
                };
                auto get32 = [](const uint8_t* q) {
                    uint32_t r; std::memcpy(&r, q, 4); return r;
                };
                while (p + 8 <= end) {
                    uint64_t k1 = rotl(get64(p) * PRIME2, 31) * PRIME1;
                    h = rotl(h ^ k1, 27) * PRIME1 + PRIME4;
                    p += 8;
                }
                if (p + 4 <= end) {
                    h = rotl(h ^ (static_cast<uint64_t>(get32(p)) * PRIME1), 23) * PRIME2 + PRIME3;
                    p += 4;
                }
                while (p < end) {
                    h = rotl(h ^ (*p * PRIME5), 11) * PRIME1;
                    ++p;
                }
                h ^= h >> 33; h *= PRIME2; h ^= h >> 29; h *= PRIME3; h ^= h >> 32;
                return h;
            };

            // Rebuild body: system as array with billing placeholder, adaptive thinking.
            json body = req.body_json;
            body["system"] = json::array({
                {{"type", "text"},
                 {"text", std::string("x-anthropic-billing-header: cc_version=") + kClaudeCodeVer +
                              "." + suffix + "; cc_entrypoint=local-agent; cch=00000;"}},
                {{"type", "text"},
                 {"text", "You are a Claude agent, built on Anthropic's Claude Agent SDK."}},
                {{"type", "text"}, {"text", system}},
            });
            // OAuth path: adaptive thinking (budget_tokens 400s on opus 4.8).
            // Object, not array — see anthropic_build_request note.
            body["thinking"] = json::object({{"type", "adaptive"}});
            body.erase("output_config");  // optional; adaptive alone is valid

            std::string body_str = body.dump();  // compact? nlohmann default is compact
            // nlohmann dump() is compact by default (no spaces) — matches separators=(',',':')
            uint64_t cch_val = xxh64(body_str, 0x4D659218E32A3268ULL);
            char cch_buf[8];
            std::snprintf(cch_buf, sizeof(cch_buf), "%05x",
                          static_cast<unsigned>((cch_val & 0xFFFFFULL)));
            std::string patched = body_str;
            auto pos = patched.find("cch=00000;");
            if (pos != std::string::npos)
                patched.replace(pos, 10, std::string("cch=") + cch_buf + ";");
            req.body = patched;
            req.body_json = json::parse(patched, nullptr, false);
            req.headers = {
                {"Authorization", "Bearer " + api_key},
                {"anthropic-version", "2023-06-01"},
                {"content-type", "application/json"},
                {"anthropic-beta", "interleaved-thinking-2025-05-14,oauth-2025-04-20"},
                {"anthropic-dangerous-direct-browser-access", "true"},
                {"x-app", "cli"},
                {"User-Agent",
                 "claude-cli/2.1.165 (external, local-agent, agent-sdk/0.3.165)"},
                {"X-Stainless-Retry-Count", "0"},
                {"X-Stainless-Timeout", "900"},
            };
        } else {
            for (auto& h : req.headers)
                if (h.first == "x-api-key") h.second = api_key;
        }

        RetryResult rr = http_post_retry(req.url, req.headers, req.body);
        res.endpoint = req.url;
        res.model = model;
        res.attempt = rr.attempt;
        res.http_status = static_cast<int>(rr.hr.status);
        res.request_id = request_id_from_body(rr.hr.body);
        if (!rr.hr.error.empty()) {
            res.error = rr.hr.error;
            res.error_excerpt = truncate_excerpt(rr.hr.error);
            return finish(std::move(res));
        }
        if (rr.hr.status < 200 || rr.hr.status >= 300) {
            res.error = concise_http_error(oauth ? "anthropic-sub" : "anthropic", rr.hr.status,
                                           rr.hr.body);
            res.error_excerpt = truncate_excerpt(rr.hr.body.empty() ? res.error : rr.hr.body);
            return finish(std::move(res));
        }
        ParsedReply pr = anthropic_parse_response(json::parse(rr.hr.body, nullptr, false));
        res.text = pr.text;
        res.thinking = pr.thinking;
        return finish(std::move(res));
    }

    // OpenAI-compatible family: openai / llamacpp / xai / xai-sub / moonshot.
    // billet's Python XAISubProvider posts to /v1/chat/completions with Bearer
    // (OAuth access_token or API key). OAuth tokens arrive already refreshed
    // (auth::ensure_fresh_for_run at resolve time, T-RF4).
    if (provider == "openai" || provider == "llamacpp" || provider == "xai" ||
        provider == "xai-sub" || provider == "moonshot") {
        std::string def = (provider == "llamacpp") ? std::string("local-gguf")
                          : (provider == "xai" || provider == "xai-sub")
                              ? std::string("grok-4.5")
                              : (provider == "moonshot") ? std::string("kimi-k3")
                                                         : std::string(kOpenAIDefaultModel);
        std::string model = str_or(cfg, "model", def);
        if (model == "grok-4-5") model = "grok-4.5";  // normalize hyphen alias
        // Moonshot (Kimi K3): the API accepts only `kimi-k3`. `kimi-k3-256k` is our
        // context-capped profile (see moonshot_provider.py) — normalize to the wire id.
        if (provider == "moonshot" &&
            (model == "kimi-k3-256k" || model == "kimi-k3-1m" || model == "kimi-latest" ||
             model == "moonshotai/kimi-k3"))
            model = "kimi-k3";
        if ((provider == "xai" || provider == "xai-sub") && base_url.empty())
            base_url = "https://api.x.ai/v1";
        if (provider == "moonshot" && base_url.empty())
            base_url = "https://api.moonshot.ai/v1";
        if (api_key.empty() && (provider == "xai" || provider == "xai-sub")) {
            res.error = "xai: missing access_token/api_key (sign in via Settings → Accounts)";
            res.model = model;
            res.endpoint = base_url.empty() ? "https://api.x.ai/v1" : base_url;
            return finish(std::move(res));
        }
        if (api_key.empty() && provider == "moonshot") {
            res.error = "moonshot: missing api_key (config keys.moonshot / MOONSHOT_API_KEY)";
            res.model = model;
            res.endpoint = base_url.empty() ? "https://api.moonshot.ai/v1" : base_url;
            return finish(std::move(res));
        }
        BuiltRequest req = openai_build_request(model, system, messages, max_tokens, base_url);
        const char* err_name =
            (provider == "xai" || provider == "xai-sub")
                ? (provider == "xai-sub" ? "xai-sub" : "xai")
                : (provider == "moonshot") ? "moonshot"
                                           : "openai";
        // STREAM the completion: token deltas flow continuously so the idle timeout
        // never kills a long-but-active generation (the old 120s hard-cap death), and
        // each delta is forwarded live to the run's sink. openai_build_request stays
        // pure (parity-tested); stream:true is added to the built body only here.
        json sbody = req.body_json;
        // Per-model reasoning-effort: merge the capability-gated field(s) into the streamed
        // body ONLY when the target model accepts them (see openai_reasoning_patch).
        {
            json rpatch = openai_reasoning_patch(provider, model, base_url,
                                                 str_or(cfg, "reasoning_effort"));
            for (auto it = rpatch.begin(); it != rpatch.end(); ++it)
                sbody[it.key()] = it.value();
        }
        sbody["stream"] = true;
        const std::string stream_body = sbody.dump();
        // Keys to try: the resolved api_key first, then any signed-in fallback accounts
        // (xai-sub multi-account rotation on a 403 Heavy-only tier gate). Read-only.
        std::vector<std::string> keys = {api_key.empty() ? std::string("not-needed") : api_key};
        if (cfg.contains("fallback_api_keys") && cfg["fallback_api_keys"].is_array())
            for (const auto& k : cfg["fallback_api_keys"])
                if (k.is_string() && !k.get<std::string>().empty())
                    keys.push_back(k.get<std::string>());
        SseAccum acc;
        auto& delta_emit = g_provider_delta_emitter;
        HttpChunkCb on_chunk = [&](const char* data, size_t len) {
            acc.feed(data, len, delta_emit);
        };
        HttpResponse hr;
        int attempt = 1;
        for (size_t ki = 0; ki < keys.size(); ++ki) {
            std::vector<std::pair<std::string, std::string>> sheaders = req.headers;
            for (auto& h : sheaders)
                if (h.first == "Authorization") h.second = "Bearer " + keys[ki];
            sheaders.push_back({"Accept", "text/event-stream"});
            int delay_ms = 1000;
            for (;;) {  // transient 429/503 backoff (only while nothing has streamed)
                if (http_cancel_requested()) {
                    hr.error = "cancelled";
                    hr.status = 0;
                    break;
                }
                acc.reset();
                hr = http_post_stream(req.url, sheaders, stream_body, on_chunk);
                const bool streamed_now = !acc.text.empty() || !acc.thinking.empty();
                const bool retryable = hr.error.empty() && !streamed_now &&
                                       (hr.status == 429 || hr.status == 503);
                if (!retryable || attempt >= 5 || is_daily_quota_exhausted(hr)) break;
                std::this_thread::sleep_for(std::chrono::milliseconds(delay_ms));
                delay_ms *= 2;
                ++attempt;
            }
            const bool streamed_now = !acc.text.empty() || !acc.thinking.empty();
            const bool tier_gate = !streamed_now && hr.status == 403 &&
                                   hr.body.find("denied") != std::string::npos;
            if (tier_gate && ki + 1 < keys.size()) continue;  // rotate to next account
            break;
        }
        const bool streamed = !acc.text.empty() || !acc.thinking.empty();
        res.endpoint = req.url;
        res.model = model;
        res.attempt = attempt;
        res.http_status = static_cast<int>(hr.status);
        res.request_id = request_id_from_body(hr.body);
        if (!hr.error.empty() && !streamed) {
            res.error = hr.error;
            res.error_excerpt = truncate_excerpt(hr.error);
            return finish(std::move(res));
        }
        if ((hr.status < 200 || hr.status >= 300) && !streamed) {
            res.error = concise_http_error(err_name, hr.status, hr.body);
            res.error_excerpt = truncate_excerpt(hr.body.empty() ? res.error : hr.body);
            return finish(std::move(res));
        }
        std::string text = acc.text;
        std::string thinking = acc.thinking;
        if (text.empty() && thinking.empty()) {
            // Endpoint ignored stream:true and returned one JSON object — whole-body parse.
            json parsed = json::parse(hr.body, nullptr, false);
            ParsedReply pr = openai_parse_response(parsed);
            text = pr.text;
            thinking = pr.thinking;
        }
        // No CoT->text fallback: extraction downstream (extract_json_obj) must
        // see the model's ACTIONS only. Aliasing reasoning into text lets a
        // thinking model's scratchpad (which often contains JSON-ish arrays of
        // constraints it is mulling over) be parsed as if it were the answer.
        // An action-empty reply is an honest "no completion" instead.
        if (text.empty()) {
            res.error = std::string(err_name) + " endpoint returned no completion";
            res.error_excerpt = truncate_excerpt(hr.body.empty() ? res.error : hr.body);
            return finish(std::move(res));
        }
        res.text = text;
        res.thinking = thinking;
        return finish(std::move(res));
    }

    if (provider == "gemini") {
        std::string model = str_or(cfg, "model", std::string(kGeminiDefaultModel));
        std::string effort = str_or(cfg, "reasoning_effort");
        BuiltRequest req = gemini_build_request(model, system, messages, max_tokens, effort);
        for (auto& h : req.headers)
            if (h.first == "x-goog-api-key") h.second = api_key;
        RetryResult rr = http_post_retry(req.url, req.headers, req.body);
        res.endpoint = req.url;
        res.model = model;
        res.attempt = rr.attempt;
        res.http_status = static_cast<int>(rr.hr.status);
        res.request_id = request_id_from_body(rr.hr.body);
        if (!rr.hr.error.empty()) {
            res.error = rr.hr.error;
            res.error_excerpt = truncate_excerpt(rr.hr.error);
            return finish(std::move(res));
        }
        if (rr.hr.status < 200 || rr.hr.status >= 300) {
            res.error = concise_http_error("gemini", rr.hr.status, rr.hr.body);
            res.error_excerpt = truncate_excerpt(rr.hr.body.empty() ? res.error : rr.hr.body);
            return finish(std::move(res));
        }
        json parsed = json::parse(rr.hr.body, nullptr, false);
        if (parsed.is_discarded()) {
            res.error = "gemini: unparseable response body";
            res.error_excerpt = truncate_excerpt(rr.hr.body);
            return finish(std::move(res));
        }
        ParsedReply pr = gemini_parse_response(parsed);
        if (pr.text.empty()) {
            if (!parsed.contains("candidates") || !parsed["candidates"].is_array() ||
                parsed["candidates"].empty()) {
                std::string why;
                if (parsed.contains("promptFeedback")) why = parsed["promptFeedback"].dump();
                res.error = "gemini endpoint returned no completion" +
                            (why.empty() ? std::string() : (": " + why));
                res.error_excerpt = truncate_excerpt(res.error);
                return finish(std::move(res));
            }
            std::string fr = parsed["candidates"][0].value("finishReason", std::string());
            res.error = "gemini: empty completion (finishReason " +
                        (fr.empty() ? std::string("unknown") : fr) + ")" +
                        (fr == "MAX_TOKENS"
                             ? " — thinking consumed the output budget; lower reasoning effort or"
                               " raise max_output_tokens"
                             : "");
            res.error_excerpt = truncate_excerpt(res.error);
            return finish(std::move(res));
        }
        res.text = pr.text;
        res.thinking = pr.thinking;
        return finish(std::move(res));
    }

    if (provider == "cursor" || provider == "cursor-sub") {
        res.error =
            "cursor: no C++ chat completions transport (Connect-RPC/protobuf only). "
            "Pick xAI, Claude, or OpenAI.";
        res.model = str_or(cfg, "model");
        res.endpoint = "cursor";
        return finish(std::move(res));
    }

    res.error = "unknown provider: " + provider;
    return finish(std::move(res));
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
