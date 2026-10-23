// core/agent/providers.cpp — C++ port of services/agent/providers/*.py (see providers.h).
#include "agent/providers.h"

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cctype>
#include <chrono>
#include <fstream>
#include <memory>
#include <regex>
#include <set>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "agent/config.h"  // compliance_mode / provider_offline_safe / host_is_loopback (WS3)
#include "agent/http.h"

namespace chudware {
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


const char* kAnthropicDefaultModel = "claude-opus-5-5";
const char* kOpenAIDefaultModel = "gpt-4o";  // API-key endpoint; GPT-6 Sol is Codex-only

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
    if (effort == "off" || effort == "none" || effort == "minimal")
        effort = "low";
    else if (effort == "auto" || effort.empty())
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
    // Anthropic requires the effort-2025-11-24 beta header with output_config.effort;
    // without it some endpoints reject the field even on adaptive-capable models.
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

std::string moonshot_normalize_model(const std::string& model) {
    // `kimi-k3-256k` is our context-capped profile (see moonshot_provider.py); the API only
    // accepts the bare `kimi-k3` wire id. Normalize those aliases to it.
    if (model == "kimi-k3-256k" || model == "kimi-k3-1m" || model == "kimi-latest" ||
        model == "moonshotai/kimi-k3")
        return "kimi-k3";
    // The official wire id for Kimi K2.7 is `kimi-k2.7-code`; the bare `kimi-k2.7` 404s
    // ("Not found the model kimi-k2.7").
    if (model == "kimi-k2.7") return "kimi-k2.7-code";
    return model;
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
//   Moonshot kimi-k3          reasoning_effort: low|high|max  (NO medium rung, so a
//                             medium selection maps to high; kimi-k2.x: omit -> thinking)
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
    // kimi-k3 accepts low|high|max and has NO medium rung, so a "medium" selection maps
    // to high (the nearest real rung above low) and the above-high spellings map to the
    // model's genuine "max". Nothing is clamped: the ladder the GUI offers for this model
    // is exactly what the endpoint accepts.
    auto kimi_k3 = [&]() -> std::string {
        if (lvl == "low" || lvl == "minimal") return "low";
        if (lvl == "max" || lvl == "maximum" || lvl == "xhigh" || lvl == "x-high" ||
            lvl == "extra_high" || lvl == "extra-high" || lvl == "ultra" ||
            lvl == "highest")
            return "max";
        return "high";  // medium / high / any other on-value
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
        if (has(m, "kimi-k3")) return json{{"reasoning_effort", kimi_k3()}};
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
    if (has(b, "openai.com")) {
        if (m.rfind("gpt-5", 0) == 0 || m.rfind("gpt-6", 0) == 0 ||
            m.rfind("o1", 0) == 0 || m.rfind("o3", 0) == 0 || m.rfind("o4", 0) == 0) {
            bool modern = m.rfind("gpt-6", 0) == 0 || m.rfind("gpt-5.6", 0) == 0;
            return json{{"reasoning_effort", modern && (lvl == "xhigh" || lvl == "max")
                                                ? lvl : lmh()}};
        }
        return json::object();
    }
    // --- No endpoint match: fall back to MODEL-NAME heuristics (bare provider name / empty base). ---
    if (m.rfind("kimi", 0) == 0) {
        if (has(m, "kimi-k3")) return json{{"reasoning_effort", kimi_k3()}};
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
        (m.rfind("gpt-5", 0) == 0 || m.rfind("gpt-6", 0) == 0 ||
         m.rfind("o1", 0) == 0 || m.rfind("o3", 0) == 0 || m.rfind("o4", 0) == 0)) {
        bool modern = m.rfind("gpt-6", 0) == 0 || m.rfind("gpt-5.6", 0) == 0;
        return json{{"reasoning_effort", modern && (lvl == "xhigh" || lvl == "max")
                                            ? lvl : lmh()}};
    }
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
const char* kGeminiDefaultModel = "gemini-3.5-flash";

// One content part -> a Gemini part: text -> {text}; image -> {inline_data:{mime_type,data}}.
json gemini_part(const json& part) {
    if (pstr(part, "type") == "text") {
        return json{{"text", pstr(part, "text")}};
    }
    std::string data = base64_encode(read_file_bytes(pstr(part, "path")));
    return json{{"inline_data", {{"mime_type", "image/png"}, {"data", data}}}};
}

// Budget-mode models support minimal thinking, not zero on Gemini 2.5 Pro.
// Empty leaves the model default; a saved "off" selects the lowest valid rung.
static bool gemini_thinking_budget(const std::string& effort, int* out) {
    std::string e;
    for (char c : effort) e.push_back(static_cast<char>(std::tolower((unsigned char)c)));
    if (e == "minimal" || e == "off" || e == "none") { *out = 128; return true; }
    if (e == "low") { *out = 1024; return true; }
    if (e == "medium" || e == "med") { *out = 8192; return true; }
    if (e == "high" || e == "max" || e == "xhigh") { *out = 24576; return true; }
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
    if (model.rfind("gemini-3", 0) == 0) {
        std::string level = lower_copy(reasoning_effort);
        if (level == "off" || level == "none" || level == "minimal") level = "minimal";
        else if (level == "max" || level == "xhigh" || level == "med") level = "high";
        if (model.find("pro") != std::string::npos && level == "medium") level = "high";
        if (level == "minimal" || level == "low" || level == "medium" || level == "high") {
            for (char& c : level) c = static_cast<char>(std::toupper((unsigned char)c));
            gen["thinkingConfig"] = json{{"thinkingLevel", level}};
        }
    } else {
        int budget = 0;
        if (gemini_thinking_budget(reasoning_effort, &budget)) {
            gen["thinkingConfig"] = json{{"thinkingBudget", budget}};
            // Budget thinking counts against the output limit on Gemini 2.5.
            if (budget > 0) gen["maxOutputTokens"] = budget + max_tokens;
        }
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

bool is_provider_refusal(const std::string& raw) {
    return raw.find(kProviderRefusedMarker) != std::string::npos;
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

    // Content refusal FIRST, and on OUR marker, not on prose: the transport branch below
    // matches the substring "refused", so a vendor refusal reaching it would be reported as
    // a connection failure and send the user to check their network.
    if (is_provider_refusal(s))
        return "That provider declined this request on content policy. Its credential and "
               "connection are fine — pick a different provider in Settings → AI to run it.";

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
        has("no api key") || has("missing access_token"))
        return "Provider not signed in. Open Settings → AI → Accounts.";
    if (has("unknown provider") || has("offline mode"))
        return s;  // already human

    // Fallback: first line of the raw message, capped.
    size_t nl = s.find('\n');
    if (nl != std::string::npos) s = s.substr(0, nl);
    if (s.size() > 160) s = s.substr(0, 157) + "...";
    return s.empty() ? "The model provider failed." : s;
}



// --------------------------------------------------------------------------- redaction (WS3)
// Native C++ port of services/agent/redaction.py. compliance_mode=redact wraps cloud-bound
// traffic: deterministic scrub -> optional loopback-LLM residual pass -> rehydrate inbound;
// images blocked outbound. Loopback/local providers are never wrapped; local_only /
// offline_mode refuse non-loopback at resolve_provider (config.cpp) as before.

// Placeholder families -> category prefix used in tokens (DIM_1, MAT_1, PN_3, PROJ_1, SPAN_4).
static const char* kCatDim = "DIM";
static const char* kCatMat = "MAT";
static const char* kCatPn = "PN";
static const char* kCatProj = "PROJ";
static const char* kCatSpan = "SPAN";  // residual spans from the optional LLM pass

std::string RedactionMap::placeholder_for(const std::string& original,
                                          const std::string& category) {
    auto it = forward_.find(original);
    if (it != forward_.end()) return it->second;
    int n = ++counters_[category];
    std::string token = category + "_" + std::to_string(n);
    forward_[original] = token;
    reverse_[token] = original;
    return token;
}

namespace {

// Placeholder token form: DIM_1, MAT_12, PN_3, PROJ_1, SPAN_4
const std::regex& placeholder_re() {
    static const std::regex re(R"(\b(?:DIM|MAT|PN|PROJ|SPAN)_\d+\b)");
    return re;
}

// Common aerospace/mechanical alloy & polymer designations. Longest first so multi-token
// specs win ("6061-T6" over bare "6061"); case-insensitive, original case preserved in the
// mapping table for rehydrate fidelity. Mirrors redaction.py _MATERIAL_SPECS.
const char* kMaterialSpecs[] = {
    // Aluminum alloys + temper
    R"(7075-T6)", R"(7075-T73)", R"(7075-T7351)", R"(6061-T6)", R"(6061-T651)", R"(6061-T4)",
    R"(2024-T3)", R"(2024-T4)", R"(2024-T351)", R"(7050-T7451)", R"(5083-H116)", R"(5052-H32)",
    R"(AL\s*7075)", R"(AL\s*6061)", R"(AL\s*2024)",
    R"(Aluminium\s+6061-T6)", R"(Aluminum\s+6061-T6)",
    R"(Aluminium\s+6061)", R"(Aluminum\s+6061)",
    R"(Aluminium\s+7075)", R"(Aluminum\s+7075)",
    // Steels
    R"(AISI\s*4140)", R"(AISI\s*4340)", R"(AISI\s*1018)", R"(AISI\s*1020)", R"(AISI\s*1045)",
    R"(SAE\s*4140)", R"(SAE\s*4340)", R"(SAE\s*1018)",
    R"(A36\s+steel)", R"(A36)",
    R"(4140\s+steel)", R"(4340\s+steel)", R"(1018\s+steel)", R"(mild\s+steel)",
    R"(SS\s*316L)", R"(SS\s*316)", R"(SS\s*304L)", R"(SS\s*304)",
    R"(304\s+stainless)", R"(316\s+stainless)", R"(316L\s+stainless)",
    R"(17-4\s*PH)", R"(15-5\s*PH)",
    // Titanium / nickel / copper alloys
    R"(Ti-6Al-4V)", R"(Ti6Al4V)", R"(Grade\s*5\s+titanium)", R"(titanium\s+Grade\s*5)",
    R"(Inconel\s*718)", R"(Inconel\s*625)", R"(Hastelloy\s*C-?276)",
    R"(brass\s*C360)", R"(C360\s+brass)", R"(C36000)",
    // Polymers / composites commonly called out as material specs
    R"(PEEK)", R"(ULTEM\s*9085)", R"(ULTEM\s*1010)", R"(Delrin)", R"(POM-C)",
    R"(carbon\s+fiber)", R"(GFRP)", R"(CFRP)",
};

// Project / program identifiers often leak controlled context.
const char* kProjectPatterns[] = {
    R"(\b(?:ITAR|EAR|CUI|FOUO)[-_\s]?(?:restricted|controlled)?\b)",
    R"(\b(?:program|project|mission|contract)\s*(?:id|no\.?|number|#)?\s*[:=]?\s*[A-Z]{2,6}[-_]?\d{2,8}(?:[-_][A-Z0-9]{1,6})?\b)",
    R"(\b(?:PRJ|PROG|MSN|CTR)[-_][A-Z0-9]{2,12}\b)",
};

// Part / drawing numbers: DWG-1234, P/N ABC-12, drawing 100-200-A, PN: X12345
const char* kPartNumberPatterns[] = {
    R"(\b(?:P/?N|P/N|PN|part\s*(?:no\.?|number|#)|drawing\s*(?:no\.?|number|#)?|DWG|dwg)\s*[:=]?\s*[A-Z0-9][A-Z0-9._/-]{2,24}\b)",
    R"(\b(?:DWG|PN|P/N)[-_][A-Z0-9][A-Z0-9._/-]{1,20}\b)",
    R"(\b[A-Z]{1,3}\d{2,4}[-_]\d{2,6}(?:[-_][A-Z0-9]{1,4})?\b)",  // AB12-3456-A style
};

// Join patterns into one alternation; longest-first when `longest_first` (materials) so
// multi-token specs win. Case-insensitive mirrors redaction.py _compile_list/_compile_material_re.
std::string join_patterns(const char* const* specs, size_t n, bool longest_first) {
    std::vector<std::string> ordered(specs, specs + n);
    if (longest_first)
        std::stable_sort(ordered.begin(), ordered.end(),
                         [](const std::string& a, const std::string& b) {
                             return a.size() > b.size();
                         });
    std::string pat = "(?:";
    bool first = true;
    for (const auto& s : ordered) {
        if (!first) pat += "|";
        pat += "(?:" + s + ")";
        first = false;
    }
    pat += ")";
    return pat;
}

const std::regex& material_re() {
    static const std::regex re(
        join_patterns(kMaterialSpecs, sizeof(kMaterialSpecs) / sizeof(kMaterialSpecs[0]), true),
        std::regex_constants::icase);
    return re;
}
const std::regex& part_number_re() {
    static const std::regex re(
        join_patterns(kPartNumberPatterns,
                      sizeof(kPartNumberPatterns) / sizeof(kPartNumberPatterns[0]), false),
        std::regex_constants::icase);
    return re;
}
const std::regex& project_re() {
    static const std::regex re(
        join_patterns(kProjectPatterns, sizeof(kProjectPatterns) / sizeof(kProjectPatterns[0]),
                      false),
        std::regex_constants::icase);
    return re;
}

// Bilateral / unilateral tolerance: ±0.02, +/-0.05 mm, +0.02/-0.01
const std::regex& tol_re() {
    static const std::regex re(
        R"((?:±|\+/-|\+/?-)\s*(\d+(?:[.,]\d+)?)\s*(mm|in|mil|µm|um|μm)?)"
        R"(|\+(\d+(?:[.,]\d+)?)\s*/\s*-(\d+(?:[.,]\d+)?)\s*(mm|in|mil|µm|um|μm)?)");
    return re;
}

// Numeric dimensions with units. Order: compound first, then simple.
// Captures: 12.5 mm, 0.25 in, 45 deg, 30°, 1/4", 3/8 in, Ø20 mm, dia 10mm, R3.0
const std::regex& dim_unit_re() {
    static const std::regex re(
        // diameter prefixes (Ø, ⌀, dia, diam) — radius handled separately (word-bounded)
        R"((?:[Ø⌀]|[Dd]ia(?:m(?:eter)?)?\.?)\s*([+-]?\d+(?:[.,]\d+)?(?:\s*/\s*\d+(?:[.,]\d+)?)?)\s*(mm|cm|m|in|inch|inches|"|''|mil|µm|um|μm|deg|°|rad|degrees?)?)"
        // radius: R3 / R 3.0 / R3.0mm — whole-word R so "chamfer" is safe
        R"(|\b[Rr]\s*([+-]?\d+(?:[.,]\d+)?)\s*(mm|cm|m|in|inch|inches|"|''|mil|µm|um|μm)?\b)"
        // plain number + unit (incl. fraction 1/4 in)
        R"(|([+-]?\d+(?:[.,]\d+)?(?:\s*/\s*\d+(?:[.,]\d+)?)?)\s*(mm|cm|m|in|inch|inches|"|''|mil|µm|um|μm|deg|°|rad|degrees?)\b)"
        // bare degree symbol glued: 45°
        R"(|([+-]?\d+(?:[.,]\d+)?)\s*(°))");
    return re;
}

// ISO hole/shaft fits & unilateral tolerances: H7, g6, H7/g6, js6, F8, …
const std::regex& iso_fit_strict_re() {
    static const std::regex re(
        R"(\b(?:[A-Z]{1,2}|[a-z]{1,2})\d{1,2}(?:\s*/\s*(?:[A-Z]{1,2}|[a-z]{1,2})\d{1,2})?\b)");
    return re;
}
const std::regex& iso_fit_known_re() {
    static const std::regex re(
        R"(^(?:[A-Z]{1,2}|[a-z]{1,2})\d{1,2}$|^(?:[A-Z]{1,2}|[a-z]{1,2})\d{1,2}\s*/\s*(?:[A-Z]{1,2}|[a-z]{1,2})\d{1,2}$)");
    return re;
}
const std::regex& iso_fit_part_re() {
    static const std::regex re(R"(^([A-Za-z]{1,2})(\d{1,2})$)");
    return re;
}
const std::unordered_set<std::string>& iso_fit_letters() {
    static const std::unordered_set<std::string> s = {
        "A", "B", "C", "CD", "D", "E", "EF", "F", "FG", "G", "H", "JS", "J", "K", "M",
        "N", "P", "R", "S", "T", "U", "V", "X", "Y", "Z", "ZA", "ZB", "ZC",
    };
    return s;
}

// Only treat a letter+grade token as an ISO fit when the letters are a known fit class and
// the grade is 1..18 (mirrors redaction.py _is_iso_fit).
bool is_iso_fit(std::string token) {
    // trim
    while (!token.empty() && std::isspace(static_cast<unsigned char>(token.front())))
        token.erase(token.begin());
    while (!token.empty() && std::isspace(static_cast<unsigned char>(token.back())))
        token.pop_back();
    if (!std::regex_search(token, iso_fit_known_re())) return false;
    // Split possible hole/shaft pair.
    size_t slash = token.find('/');
    std::string parts[2];
    size_t nparts = 0;
    if (slash == std::string::npos) {
        parts[nparts++] = token;
    } else {
        parts[nparts++] = token.substr(0, slash);
        parts[nparts++] = token.substr(slash + 1);
    }
    for (size_t i = 0; i < nparts; ++i) {
        std::string p = parts[i];
        while (!p.empty() && std::isspace(static_cast<unsigned char>(p.front())))
            p.erase(p.begin());
        while (!p.empty() && std::isspace(static_cast<unsigned char>(p.back()))) p.pop_back();
        std::smatch m;
        if (!std::regex_match(p, m, iso_fit_part_re())) return false;
        std::string letters = m[1].str();
        for (char& c : letters) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
        if (!iso_fit_letters().count(letters)) return false;
        int grade = std::atoi(m[2].str().c_str());
        if (grade < 1 || grade > 18) return false;
    }
    return true;
}

struct RedactSpan {
    size_t start;
    size_t end;
    std::string original;
    std::string category;
};

void collect_spans(const std::string& text, const std::regex& re, const char* category,
                   std::vector<RedactSpan>& spans, bool iso_fit_predicate = false) {
    for (std::sregex_iterator it(text.begin(), text.end(), re), end; it != end; ++it) {
        std::string orig = it->str();
        if (iso_fit_predicate && !is_iso_fit(orig)) continue;
        size_t pos = static_cast<size_t>(it->position());
        spans.push_back({pos, pos + static_cast<size_t>(it->length()), std::move(orig),
                         category});
    }
}

// Deep-scrub arbitrary JSON (tool-call args / inputs): every string value is scrubbed.
json scrub_json_value(const json& v, RedactionMap& map);

json scrub_arguments(const json& args, RedactionMap& map) {
    if (args.is_string()) {
        // JSON string args — scrub then try to keep valid JSON (mirrors Python).
        std::string s = args.get<std::string>();
        json parsed = json::parse(s, nullptr, false);
        if (!parsed.is_discarded()) return scrub_json_value(parsed, map).dump();
        return redaction_scrub_text(s, map);
    }
    if (args.is_null()) return args;
    return scrub_json_value(args, map);
}

json scrub_json_value(const json& v, RedactionMap& map) {
    if (v.is_string()) return redaction_scrub_text(v.get<std::string>(), map);
    if (v.is_object()) {
        json o = json::object();
        for (auto it = v.begin(); it != v.end(); ++it) o[it.key()] = scrub_json_value(it.value(), map);
        return o;
    }
    if (v.is_array()) {
        json a = json::array();
        for (const auto& e : v) a.push_back(scrub_json_value(e, map));
        return a;
    }
    return v;
}

// --- optional local-LLM residual pass (redaction.py llm_flag_residual) -------------------
const char* kRedactionLlmFlagSystem =
    "You are a compliance redaction assistant. Given TEXT already partially redacted "
    "with placeholders like DIM_1/MAT_1/PN_1, list any REMAINING sensitive spans that "
    "look like exact dimensions, tolerances, material grades, part/drawing numbers, "
    "or project identifiers. Reply ONLY with a JSON array of exact substrings to "
    "redact, e.g. [\"secret-bit\", \"12.345\"]. Empty array if none. No prose.";

// Resolve the redaction-role assignment to a LOOPBACK-only OpenAI-compatible descriptor.
// Hard rule (mirrors redaction.py _build_loopback_from_assignment): known cloud names are
// refused up-front; openai is allowed only with an explicit loopback base_url. Returns null
// when absent/unusable — the caller degrades to deterministic-only and NEVER sends raw.
json resolve_redaction_role_cfg(const json& cfg) {
    json role = json(nullptr);
    if (cfg.contains("redaction_role") && cfg["redaction_role"].is_object())
        role = cfg["redaction_role"];  // threaded by resolve_provider (config.cpp)
    else if (cfg.contains("ai_roles") && cfg["ai_roles"].is_object() &&
             cfg["ai_roles"].contains("redaction"))
        role = cfg["ai_roles"]["redaction"];
    else if (cfg.contains("redaction"))
        role = cfg["redaction"];
    if (role.is_null()) return json(nullptr);
    std::string name, model, base_url;
    if (role.is_string()) {
        std::string s = role.get<std::string>();
        if (s.empty() || s == "auto") return json(nullptr);
        size_t colon = s.find(':');
        if (colon != std::string::npos) {
            name = s.substr(0, colon);
            model = s.substr(colon + 1);
        } else {
            name = s;
        }
    } else if (role.is_object()) {
        name = str_or(role, "provider");
        model = str_or(role, "model");
        base_url = str_or(role, "base_url");
    } else {
        return json(nullptr);
    }
    name = lower_copy(name);
    if (name.empty()) return json(nullptr);
    // Refuse known cloud names up-front (hard rule for the redaction role).
    if (name == "anthropic" || name == "gemini" || name == "openrouter" || name == "xai" ||
        name == "anthropic-sub" || name == "codex-sub" || name == "cursor-sub" ||
        name == "xai-sub" || name == "claude-cli" || name == "codex-cli" ||
        name == "moonshot" || name == "cursor")
        return json(nullptr);
    if (name == "llamacpp") {
        if (base_url.empty()) base_url = kDefaultLocalBaseUrl;
        if (!host_is_loopback(base_url)) return json(nullptr);
        return json{{"name", "llamacpp"}, {"model", model}, {"base_url", base_url},
                    {"api_key", "lm-studio"}};
    }
    if (name == "openai") {
        if (base_url.empty() || !host_is_loopback(base_url)) return json(nullptr);
        return json{{"name", "openai"}, {"model", model}, {"base_url", base_url},
                    {"api_key", "lm-studio"}};
    }
    return json(nullptr);  // unknown name — refuse (never construct non-loopback)
}

// Extract the first JSON array from an LLM reply (tolerant of prose/fences), mirroring
// redaction.py _parse_span_list.
std::vector<std::string> parse_span_list(const std::string& raw) {
    std::vector<std::string> out;
    if (raw.empty()) return out;
    size_t l = raw.find('[');
    size_t r = raw.rfind(']');
    if (l == std::string::npos || r == std::string::npos || r < l) return out;
    json data = json::parse(raw.substr(l, r - l + 1), nullptr, false);
    if (!data.is_array()) return out;
    for (const auto& x : data) {
        std::string s;
        if (x.is_string())
            s = x.get<std::string>();
        else if (x.is_number())
            s = x.dump();
        if (!s.empty()) out.push_back(s);
    }
    return out;
}

void replace_all(std::string& text, const std::string& from, const std::string& to) {
    if (from.empty()) return;
    size_t pos = 0;
    while ((pos = text.find(from, pos)) != std::string::npos) {
        text.replace(pos, from.size(), to);
        pos += to.size();
    }
}

// Best-effort residual pass over ONE text blob. Any failure leaves the text unchanged
// (deterministic-only); the scrubbed text is what would go out, never the raw.
void redaction_llm_residual_pass(std::string& text, RedactionMap& map, const json& role_cfg) {
    if (text.empty() || text.find_first_not_of(" \t\r\n") == std::string::npos) return;
    std::string name = role_cfg.value("name", std::string());
    if (name.empty()) return;
    std::string snippet = text.size() > 8000 ? text.substr(0, 8000) : text;
    json msgs = json::array({user_message(json::array({text_part(snippet)}))});
    CompleteResult cr = complete(name, role_cfg, kRedactionLlmFlagSystem, msgs, 512);
    if (!cr.error.empty()) return;  // degrade to deterministic-only
    std::vector<std::string> spans = parse_span_list(cr.text);
    if (spans.empty()) return;
    // Replace longer first to reduce partial-eat issues; dedupe identical spans.
    std::set<std::string> seen(spans.begin(), spans.end());
    std::vector<std::string> uniq(seen.begin(), seen.end());
    std::stable_sort(uniq.begin(), uniq.end(), [](const std::string& a, const std::string& b) {
        return a.size() > b.size();
    });
    for (const auto& span : uniq) {
        if (span.empty() || text.find(span) == std::string::npos) continue;
        // Skip if it already looks like a placeholder.
        if (std::regex_match(span, placeholder_re())) continue;
        std::string token = map.placeholder_for(span, kCatSpan);
        replace_all(text, span, token);
    }
}

// Apply the optional residual pass to the scrubbed system prompt + every message text part
// (mirrors RedactingProvider.complete_ex). No-op when no loopback redaction role resolves.
void apply_redaction_llm_residual(std::string& system, json& messages, RedactionMap& map,
                                  const json& cfg) {
    json role_cfg = resolve_redaction_role_cfg(cfg);
    if (role_cfg.is_null()) return;
    redaction_llm_residual_pass(system, map, role_cfg);
    if (!messages.is_array()) return;
    for (auto& m : messages) {
        if (!m.is_object() || !m.contains("content")) continue;
        json& content = m["content"];
        if (content.is_string()) {
            std::string t = content.get<std::string>();
            redaction_llm_residual_pass(t, map, role_cfg);
            content = t;
        } else if (content.is_array()) {
            for (auto& p : content) {
                if (!p.is_object() || pstr(p, "type") != "text" || !p.contains("text") ||
                    !p["text"].is_string())
                    continue;
                std::string t = p["text"].get<std::string>();
                redaction_llm_residual_pass(t, map, role_cfg);
                p["text"] = t;
            }
        }
    }
}

}  // namespace

bool redaction_active(const std::string& provider, const json& cfg) {
    if (compliance_mode(cfg) != "redact") return false;
    // Build a minimal descriptor for the loopback check: the provider NAME discriminates
    // cloud (anthropic/gemini/xai/moonshot -> never offline-safe) from local; openai needs
    // its base_url; an explicit offline_safe flag (loopback mock/test) wins.
    json desc = json::object();
    desc["name"] = provider;
    std::string base = str_or(cfg, "base_url");
    if (!base.empty()) desc["base_url"] = base;
    if (cfg.contains("offline_safe") && cfg["offline_safe"].is_boolean())
        desc["offline_safe"] = cfg["offline_safe"];
    return !provider_offline_safe(desc);
}

std::string redaction_scrub_text(const std::string& text, RedactionMap& map) {
    if (text.empty()) return text;
    // Collect spans from every class, then apply right-to-left so indices stay valid
    // (mirrors redaction.py scrub_text).
    std::vector<RedactSpan> spans;
    collect_spans(text, material_re(), kCatMat, spans);
    collect_spans(text, part_number_re(), kCatPn, spans);
    collect_spans(text, project_re(), kCatProj, spans);
    collect_spans(text, tol_re(), kCatDim, spans);
    collect_spans(text, dim_unit_re(), kCatDim, spans);
    collect_spans(text, iso_fit_strict_re(), kCatDim, spans, /*iso_fit_predicate=*/true);
    if (spans.empty()) return text;

    // Drop overlaps: keep earliest-starting, then longer; skip any that overlap kept.
    std::sort(spans.begin(), spans.end(), [](const RedactSpan& a, const RedactSpan& b) {
        if (a.start != b.start) return a.start < b.start;
        return (a.end - a.start) > (b.end - b.start);
    });
    std::vector<RedactSpan> kept;
    size_t occupied_end = 0;
    bool any = false;
    for (const auto& s : spans) {
        if (any && s.start < occupied_end) continue;  // overlap with a previously kept span
        kept.push_back(s);
        occupied_end = s.end;
        any = true;
    }

    // Apply right-to-left.
    std::sort(kept.begin(), kept.end(),
              [](const RedactSpan& a, const RedactSpan& b) { return a.start > b.start; });
    std::string out = text;
    for (const auto& s : kept) {
        std::string token = map.placeholder_for(s.original, s.category);
        out.replace(s.start, s.end - s.start, token);
    }
    return out;
}

std::string RedactionMap::rehydrate(const std::string& text) const {
    if (text.empty() || reverse_.empty()) return text;
    std::string out;
    out.reserve(text.size());
    size_t last = 0;
    for (std::sregex_iterator it(text.begin(), text.end(), placeholder_re()), end; it != end;
         ++it) {
        size_t pos = static_cast<size_t>(it->position());
        out.append(text, last, pos - last);
        auto found = reverse_.find(it->str());
        out += found != reverse_.end() ? found->second : it->str();
        last = pos + static_cast<size_t>(it->length());
    }
    out.append(text, last, std::string::npos);
    return out;
}

std::string redaction_rehydrate_text(const std::string& text, const RedactionMap& map) {
    return map.rehydrate(text);
}

json redaction_scrub_messages(const json& messages, RedactionMap& map) {
    // Deep-scrub message content (text + tool-call args); image parts are dropped and
    // replaced with a text marker (mirrors redaction.py scrub_messages, block_images=True).
    if (!messages.is_array()) return messages;
    json out = json::array();
    for (const auto& msg : messages) {
        if (!msg.is_object()) {
            out.push_back(msg);
            continue;
        }
        json m = msg;
        if (m.contains("content")) {
            const json& content = m["content"];
            if (content.is_string()) {
                m["content"] = redaction_scrub_text(content.get<std::string>(), map);
            } else if (content.is_array()) {
                json parts = json::array();
                for (const auto& part : content) {
                    if (!part.is_object()) {
                        parts.push_back(part);
                        continue;
                    }
                    json p = part;
                    std::string ptype = pstr(p, "type");
                    if (ptype == "image" || ptype == "image_url" || ptype == "input_image") {
                        // Drop the image part entirely; leave a text marker so the model
                        // knows vision was withheld under compliance policy.
                        parts.push_back(
                            {{"type", "text"},
                             {"text",
                              "[image redacted — compliance_mode=redact blocks outbound "
                              "images]"}});
                        continue;
                    }
                    if (p.contains("text") && p["text"].is_string())
                        p["text"] = redaction_scrub_text(p["text"].get<std::string>(), map);
                    if (p.contains("input")) p["input"] = scrub_json_value(p["input"], map);
                    if (p.contains("arguments"))
                        p["arguments"] = scrub_arguments(p["arguments"], map);
                    // tool names are not redacted
                    parts.push_back(p);
                }
                m["content"] = parts;
            }
        }
        // Top-level tool_calls (OpenAI chat format)
        if (m.contains("tool_calls") && m["tool_calls"].is_array()) {
            json tcs = json::array();
            for (const auto& tc : m["tool_calls"]) {
                if (!tc.is_object()) {
                    tcs.push_back(tc);
                    continue;
                }
                json t = tc;
                if (t.contains("function") && t["function"].is_object()) {
                    json fn = t["function"];
                    if (fn.contains("arguments"))
                        fn["arguments"] = scrub_arguments(fn["arguments"], map);
                    t["function"] = fn;
                }
                tcs.push_back(t);
            }
            m["tool_calls"] = tcs;
        }
        out.push_back(m);
    }
    return out;
}

// --------------------------------------------------------------------------- complete
CompleteResult complete(const std::string& provider, const json& cfg, const std::string& system,
                        const json& messages, int max_tokens) {
    CompleteResult res;
    auto t0 = std::chrono::steady_clock::now();
    // WS3 compliance_mode=redact: gate + per-request placeholder map. Loopback providers are
    // never wrapped (redaction_active), so mock/local take the zero-cost path and mode=off
    // is byte-identical to the pre-redaction flow.
    RedactionMap redact_map;
    const bool redact = redaction_active(provider, cfg);
    std::string scrubbed_system;
    json scrubbed_messages;
    if (redact) {
        scrubbed_system = redaction_scrub_text(system, redact_map);
        scrubbed_messages = redaction_scrub_messages(messages, redact_map);
        // Best-effort loopback-LLM residual pass (the deterministic scrub above is the
        // mandatory layer; the LLM pass only ever ADDS placeholders, never removes them).
        apply_redaction_llm_residual(scrubbed_system, scrubbed_messages, redact_map, cfg);
    }
    const std::string& eff_system = redact ? scrubbed_system : system;
    const json& eff_messages = redact ? scrubbed_messages : messages;
    auto finish = [&](CompleteResult r) -> CompleteResult {
        r.latency_ms = static_cast<int>(
            std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - t0)
                .count());
        if (r.attempt <= 0) r.attempt = 1;
        if (!r.error.empty() && r.error_excerpt.empty())
            r.error_excerpt = truncate_excerpt(r.error);
        if (redact) {
            // Inbound rehydrate: the cloud only ever saw placeholders; restore originals
            // before the agent loop parses tool calls or the GUI shows the reply.
            r.text = redaction_rehydrate_text(r.text, redact_map);
            r.thinking = redaction_rehydrate_text(r.thinking, redact_map);
        }
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


    // OpenAI-compatible family: openai / llamacpp / xai / xai-sub / moonshot / freellmapi.
    // chudware's Python XAISubProvider posts to /v1/chat/completions with Bearer
    // (OAuth access_token or API key). OAuth tokens arrive already refreshed
    // (auth::ensure_fresh_for_run at resolve time).
    if (provider == "openai" || provider == "llamacpp" || provider == "xai" ||
        provider == "xai-sub" || provider == "moonshot" || provider == "freellmapi") {
        std::string def = (provider == "llamacpp") ? std::string("local-gguf")
                          : (provider == "xai" || provider == "xai-sub")
                              ? std::string("grok-4.5")
                              : (provider == "moonshot") ? std::string("kimi-k3")
                              : (provider == "freellmapi") ? std::string("auto")
                                                         : std::string(kOpenAIDefaultModel);
        std::string model = str_or(cfg, "model", def);
        if (model == "grok-4-5") model = "grok-4.5";  // normalize hyphen alias
        // Moonshot (Kimi): map catalog/GUI aliases to the ids the moonshot.ai API accepts.
        if (provider == "moonshot") model = moonshot_normalize_model(model);
        if ((provider == "xai" || provider == "xai-sub") && base_url.empty())
            base_url = "https://api.x.ai/v1";
        if (provider == "moonshot" && base_url.empty())
            base_url = "https://api.moonshot.ai/v1";
        if (provider == "freellmapi" && base_url.empty())
            base_url = "http://localhost:3001/v1";
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
        if (api_key.empty() && provider == "freellmapi") {
            res.error = "freellmapi: missing unified API key (Settings → Providers → Free AI — "
                        "grab it from your FreeLLMAPI dashboard's Keys page)";
            res.model = model;
            res.endpoint = base_url.empty() ? "http://localhost:3001/v1" : base_url;
            return finish(std::move(res));
        }
        BuiltRequest req = openai_build_request(model, eff_system, eff_messages, max_tokens, base_url);
        const char* err_name =
            (provider == "xai" || provider == "xai-sub")
                ? (provider == "xai-sub" ? "xai-sub" : "xai")
                : (provider == "moonshot") ? "moonshot"
                : (provider == "freellmapi") ? "freellmapi"
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
        // Moonshot kimi-k2.5+ / *-thinking stream their chain-of-thought as reasoning
        // deltas BEFORE any content, and the wire max_tokens caps the SUM — at the default
        // stage budget (~8k) the model burns it all thinking and returns content-empty
        // ("endpoint returned no completion", modeler retry-death). Mirror the
        // anthropic/gemini branches: give the ANSWER room on top of the thinking spend.
        if (provider == "moonshot" &&
            (model.find("thinking") != std::string::npos ||
             model.find("k2.") != std::string::npos)) {
            int mt = sbody.value("max_tokens", max_tokens);
            sbody["max_tokens"] = mt + 24576;
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
        BuiltRequest req = gemini_build_request(model, eff_system, eff_messages, max_tokens, effort);
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
            "Cursor is not available for in-app agent runs (no REST transport; its API is "
            "Connect-RPC/protobuf). Pick a different provider in Settings -> Providers.";
        res.error_excerpt = truncate_excerpt(res.error);
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
}  // namespace chudware
