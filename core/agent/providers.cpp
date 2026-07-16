// core/agent/providers.cpp — C++ port of agent/providers/*.py (see providers.h).
#include "agent/providers.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <fstream>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
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
        body["thinking"] = json{{"type", "adaptive"}};
        body["output_config"] = json{{"effort", wire_effort}};
        send_effort = true;
    } else if (mode == "budget" || mode == "budget_effort") {
        int budget = budget_tokens_for(effort, max_tokens);
        // If the caller's max_tokens is too tight for a real budget, lift it so the
        // request is legal (budget_tokens must be < max_tokens).
        if (budget >= effective_max) effective_max = budget + 1024;
        body["max_tokens"] = effective_max;
        body["thinking"] = json{{"type", "enabled"}, {"budget_tokens", budget}};
        if (mode == "budget_effort") {
            // Opus 4.5: effort alongside budget_tokens. xhigh → max on the wire.
            if (wire_effort == "xhigh") wire_effort = "max";
            body["output_config"] = json{{"effort", wire_effort}};
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
    // `reasoning_content` field (some use `reasoning`). Capture it as thinking; and when the
    // model returned ONLY reasoning with an empty `content` — it spent its output budget
    // thinking, or never split the two — fall back to it so extract_json_obj can still pull
    // any ops JSON the model wrote inline, instead of the run dying with an empty reply.
    for (const char* k : {"reasoning_content", "reasoning"}) {
        if (msg.contains(k) && msg[k].is_string() && !msg[k].get<std::string>().empty()) {
            out.thinking = msg[k].get<std::string>();
            if (out.text.empty()) out.text = out.thinking;
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
HttpResponse http_post_retry(const std::string& url,
                             const std::vector<std::pair<std::string, std::string>>& headers,
                             const std::string& body) {
    HttpResponse hr = http_post(url, headers, body);
    int delay_ms = 1000;
    for (int attempt = 0; attempt < 4 && hr.error.empty() &&
                          (hr.status == 429 || hr.status == 503); ++attempt) {
        if (is_daily_quota_exhausted(hr)) break;  // retrying won't help today — surface it now
        std::this_thread::sleep_for(std::chrono::milliseconds(delay_ms));
        delay_ms *= 2;
        hr = http_post(url, headers, body);
    }
    return hr;
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

    std::string api_key = str_or(cfg, "api_key");
    std::string base_url = str_or(cfg, "base_url");

    if (provider == "anthropic") {
        std::string model = str_or(cfg, "model", std::string(kAnthropicDefaultModel));
        // Honor config/role reasoning_effort (low|medium|high|max|xhigh); empty → high.
        std::string effort = str_or(cfg, "reasoning_effort");
        BuiltRequest req =
            anthropic_build_request(model, system, messages, max_tokens, effort);
        for (auto& h : req.headers)
            if (h.first == "x-api-key") h.second = api_key;
        HttpResponse hr = http_post_retry(req.url, req.headers, req.body);
        if (!hr.error.empty()) {
            res.error = hr.error;
            return res;
        }
        if (hr.status < 200 || hr.status >= 300) {
            res.error = concise_http_error("anthropic", hr.status, hr.body);
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
        std::string model = str_or(cfg, "model", def);
        BuiltRequest req = openai_build_request(model, system, messages, max_tokens, base_url);
        for (auto& h : req.headers)
            if (h.first == "Authorization")
                h.second = "Bearer " + (api_key.empty() ? std::string("not-needed") : api_key);
        HttpResponse hr = http_post_retry(req.url, req.headers, req.body);
        if (!hr.error.empty()) {
            res.error = hr.error;
            return res;
        }
        if (hr.status < 200 || hr.status >= 300) {
            res.error = concise_http_error("openai", hr.status, hr.body);
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
        res.thinking = pr.thinking;
        return res;
    }

    if (provider == "gemini") {
        std::string model = str_or(cfg, "model", std::string(kGeminiDefaultModel));
        std::string effort = str_or(cfg, "reasoning_effort");
        BuiltRequest req = gemini_build_request(model, system, messages, max_tokens, effort);
        for (auto& h : req.headers)
            if (h.first == "x-goog-api-key") h.second = api_key;
        HttpResponse hr = http_post_retry(req.url, req.headers, req.body);
        if (!hr.error.empty()) {
            res.error = hr.error;
            return res;
        }
        if (hr.status < 200 || hr.status >= 300) {
            res.error = concise_http_error("gemini", hr.status, hr.body);
            return res;
        }
        json parsed = json::parse(hr.body, nullptr, false);
        if (parsed.is_discarded()) {
            res.error = "gemini: unparseable response body";
            return res;
        }
        ParsedReply pr = gemini_parse_response(parsed);
        if (pr.text.empty()) {
            // Surface a blocked/empty completion so the caller can retry/report.
            if (!parsed.contains("candidates") || !parsed["candidates"].is_array() ||
                parsed["candidates"].empty()) {
                std::string why;
                if (parsed.contains("promptFeedback")) why = parsed["promptFeedback"].dump();
                res.error = "gemini endpoint returned no completion" +
                            (why.empty() ? std::string() : (": " + why));
                return res;
            }
            // Candidates exist but carry no answer text — almost always finishReason MAX_TOKENS
            // with the whole output budget consumed by thinking. Report it as a real error so the
            // run shows the cause instead of a silent "no JSON ops in reply".
            std::string fr = parsed["candidates"][0].value("finishReason", std::string());
            res.error = "gemini: empty completion (finishReason " +
                        (fr.empty() ? std::string("unknown") : fr) + ")" +
                        (fr == "MAX_TOKENS"
                             ? " — thinking consumed the output budget; lower reasoning effort or"
                               " raise max_output_tokens"
                             : "");
            return res;
        }
        res.text = pr.text;
        res.thinking = pr.thinking;
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
