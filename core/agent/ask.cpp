// core/agent/ask.cpp — see ask.h. Mirrors agent/ask.py byte-for-byte.
#include "agent/ask.h"

#include <vector>

namespace aicad {
namespace agent {

const char* kAskRole = "ask";
const char* kAskSystem =
    "You are the AI-CAD assistant in Ask mode. You answer QUESTIONS about the software, "
    "engineering practice, and the user's open parametric CAD parts. "
    "Be concise and concrete: quote the real feature names, ids and dimensions from the document "
    "context below your instructions (units are stated there; the default MMGS means millimeters, "
    "grams, seconds). You never modify the document in this mode — if the user asks for a change, "
    "tell them briefly to switch to Edit mode (scoped changes on the open part) or Agent mode "
    "(full design/build) so the change is applied as validated document ops. "
    "If no document is open, answer as a general CAD / AI-CAD usage assistant.";

namespace {

constexpr int kMaxFeatureLines = 60;
constexpr int kMaxContextChars = 8000;

// Python truthiness for a JSON value (used where Python does `if x`).
bool truthy(const ojson& v) {
    if (v.is_null()) return false;
    if (v.is_boolean()) return v.get<bool>();
    if (v.is_string()) return !v.get<std::string>().empty();
    if (v.is_number_integer()) return v.get<long long>() != 0;
    if (v.is_number_unsigned()) return v.get<unsigned long long>() != 0;
    if (v.is_number_float()) return v.get<double>() != 0.0;
    if (v.is_array() || v.is_object()) return !v.empty();
    return false;
}

// Python str() of a JSON scalar as used in f-strings ("{k}={v}").
std::string py_str(const ojson& v) {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_boolean()) return v.get<bool>() ? "True" : "False";
    if (v.is_null()) return "None";
    if (v.is_number_integer()) return std::to_string(v.get<long long>());
    if (v.is_number_unsigned()) return std::to_string(v.get<unsigned long long>());
    if (v.is_number_float()) return py_float_repr(v.get<double>());
    return v.dump();
}

// rstrip a string (Python str.rstrip default whitespace).
std::string rstrip(const std::string& s) {
    size_t b = s.size();
    while (b > 0) {
        unsigned char c = static_cast<unsigned char>(s[b - 1]);
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v') --b;
        else break;
    }
    return s.substr(0, b);
}

// strip a string (Python str.strip default whitespace).
std::string strip(const std::string& s) {
    size_t a = s.find_first_not_of(" \t\n\r\f\v");
    if (a == std::string::npos) return "";
    size_t b = s.find_last_not_of(" \t\n\r\f\v");
    return s.substr(a, b - a + 1);
}

// Python len(str) counts Unicode CODE POINTS (UTF-8 lead bytes).
int utf8_len(const std::string& s) {
    int n = 0;
    for (unsigned char c : s)
        if ((c & 0xC0) != 0x80) ++n;
    return n;
}

// Truncate a UTF-8 string to the first `cp` code points (Python s[:cp]).
std::string utf8_prefix(const std::string& s, int cp) {
    int seen = 0;
    size_t i = 0;
    while (i < s.size() && seen < cp) {
        unsigned char c = static_cast<unsigned char>(s[i]);
        size_t adv = 1;
        if ((c & 0x80) == 0) adv = 1;
        else if ((c & 0xE0) == 0xC0) adv = 2;
        else if ((c & 0xF0) == 0xE0) adv = 3;
        else if ((c & 0xF8) == 0xF0) adv = 4;
        i += adv;
        ++seen;
    }
    return s.substr(0, i);
}

// f-string of f.get(key, default): when the key is PRESENT (even null), str()
// the value (null -> "None"); when MISSING, the default text.
std::string get_str_field(const ojson& f, const char* key, const char* dflt) {
    if (f.is_object() && f.contains(key)) return py_str(f[key]);
    return dflt;
}

}  // namespace

std::string ask_param_summary(const ojson& params, int limit) {
    std::vector<std::string> out;
    if (params.is_object()) {
        int i = 0;
        for (auto it = params.begin(); it != params.end() && i < limit; ++it, ++i) {
            const ojson& v = it.value();
            if (v.is_object() && v.contains("value")) {
                out.push_back(it.key() + "=" + py_str(v["value"]));
            } else if (v.is_number_integer() || v.is_number_unsigned() || v.is_number_float() ||
                       v.is_string() || v.is_boolean()) {
                // Python isinstance(v,(int,float,str,bool)) — bool ok via py_str.
                out.push_back(it.key() + "=" + py_str(v));
            }
        }
    }
    std::string joined;
    for (size_t i = 0; i < out.size(); ++i) {
        if (i) joined += ", ";
        joined += out[i];
    }
    return joined;
}

std::string summarize_features(const ojson& doc) {
    ojson feats = ojson::array();
    if (doc.is_object() && doc.contains("features") && doc["features"].is_array())
        feats = doc["features"];
    std::vector<std::string> lines;
    int n = static_cast<int>(feats.size());
    int upto = n < kMaxFeatureLines ? n : kMaxFeatureLines;
    for (int i = 0; i < upto; ++i) {
        const ojson& f = feats[i];
        if (!f.is_object()) continue;
        std::string sup = (f.contains("suppressed") && truthy(f["suppressed"])) ? " [suppressed]" : "";
        std::string id = get_str_field(f, "id", "?");
        std::string type = get_str_field(f, "type", "?");
        std::string name = get_str_field(f, "name", "");
        std::string line = rstrip(id + " " + type + " " + name + sup);
        ojson params = (f.contains("params") && !f["params"].is_null()) ? f["params"]
                                                                        : ojson::object();
        std::string dims = ask_param_summary(params);
        if (!dims.empty()) line += " (" + dims + ")";
        lines.push_back(line);
    }
    if (n > kMaxFeatureLines)
        lines.push_back("... (+" + std::to_string(n - kMaxFeatureLines) + " more features)");
    std::string joined;
    for (size_t i = 0; i < lines.size(); ++i) {
        if (i) joined += "\n";
        joined += lines[i];
    }
    return joined;
}

std::string ask_status_line(const ojson& feature_status) {
    std::vector<const ojson*> fs;
    if (feature_status.is_array())
        for (const auto& s : feature_status) fs.push_back(&s);
    if (fs.empty()) return "STATUS: not built yet.";
    std::vector<const ojson*> bad;
    for (const ojson* s : fs) {
        bool ok = s->is_object() && s->contains("ok") && truthy((*s)["ok"]);
        if (!ok) bad.push_back(s);
    }
    if (bad.empty())
        return "STATUS: all " + std::to_string(fs.size()) + " feature(s) compile cleanly.";
    std::string errs;
    int upto = bad.size() < 4 ? static_cast<int>(bad.size()) : 4;
    for (int i = 0; i < upto; ++i) {
        if (i) errs += "; ";
        const ojson& s = *bad[i];
        std::string id = (s.is_object() && s.contains("id") && !s["id"].is_null())
                             ? py_str(s["id"]) : "?";
        // f"{s.get('error')}" — str() of error (None -> "None").
        std::string err = (s.is_object() && s.contains("error")) ? py_str(s["error"]) : "None";
        errs += id + ": " + err;
    }
    return "STATUS: " + std::to_string(bad.size()) + " of " + std::to_string(fs.size()) +
           " feature(s) have errors — " + errs;
}

std::string build_doc_context(const ojson& doc, const std::string& doc_id, long revision,
                              const ojson& feature_status) {
    if (doc.is_null() || !doc.is_object())
        return "NO DOCUMENT IS OPEN. Answer as a general CAD / AI-CAD usage assistant "
               "(suggest Design-new mode to start a part).";
    ojson feats = (doc.contains("features") && doc["features"].is_array()) ? doc["features"]
                                                                          : ojson::array();
    // units = ((doc.get("units") or {}).get("system")) or "MMGS"
    std::string units = "MMGS";
    if (doc.contains("units") && doc["units"].is_object()) {
        const ojson& u = doc["units"];
        if (u.contains("system") && truthy(u["system"])) units = py_str(u["system"]);
    }
    // doc.get("name", "Part") — present-and-non-null str()'d, else "Part".
    std::string name = get_str_field(doc, "name", "Part");
    std::string head = "DOCUMENT: " + name + "  (id " + doc_id + ", revision " +
                       std::to_string(revision) + ")\nUNITS: " + units;
    if (feats.empty()) return head + "\nFEATURES: none yet — the part is empty.";
    std::string body = head + "\nFEATURES (" + std::to_string(feats.size()) + "):\n" +
                       summarize_features(doc) + "\n" + ask_status_line(feature_status);
    if (utf8_len(body) > kMaxContextChars)
        body = utf8_prefix(body, kMaxContextChars) + "\n...[context truncated]";
    return body;
}

AskPrompt build_ask_prompt(const std::string& question, const ojson& doc,
                           const std::string& doc_id, long revision,
                           const ojson& feature_status) {
    AskPrompt out;
    out.system = std::string("ROLE: ") + kAskRole + "\n" + kAskSystem;
    out.body = build_doc_context(doc, doc_id, revision, feature_status) + "\n\nQUESTION:\n" +
               question;
    return out;
}

std::string answer_question(const std::string& question, const ojson& doc,
                            const std::string& doc_id, long revision,
                            const ojson& feature_status, const AskCompleteFn& complete) {
    AskPrompt p = build_ask_prompt(question, doc, doc_id, revision, feature_status);
    std::string reply = complete(p.system, p.body);
    std::string stripped = strip(reply);
    if (!stripped.empty()) return stripped;
    return "I couldn't produce an answer for that — try rephrasing the question.";
}

}  // namespace agent
}  // namespace aicad
