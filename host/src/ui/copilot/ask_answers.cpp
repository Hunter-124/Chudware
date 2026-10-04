// ask_answers.cpp — GL-free half of the interactive question card (see ask_answers.h).
#include "ask_answers.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace {

std::string trim(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && std::isspace(static_cast<unsigned char>(s[a]))) ++a;
    while (b > a && std::isspace(static_cast<unsigned char>(s[b - 1]))) --b;
    return s.substr(a, b - a);
}

std::string jsonStr(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_string()) ? it->get<std::string>() : std::string();
}

bool jsonBool(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return it != o.end() && it->is_boolean() && it->get<bool>();
}

// A scalar suggested value as text: strings verbatim, numbers/bools in JSON spelling.
std::string scalarText(const nlohmann::json& v) {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_number() || v.is_boolean()) return v.dump();
    return std::string();
}

bool equalsFold(const std::string& a, const std::string& b) {
    if (a.size() != b.size()) return false;
    for (size_t i = 0; i < a.size(); ++i)
        if (std::tolower(static_cast<unsigned char>(a[i])) !=
            std::tolower(static_cast<unsigned char>(b[i])))
            return false;
    return true;
}

// Option index whose value (then label, then either case-folded) equals `s`; -1 when none.
int findOption(const AskQuestion& q, const std::string& s) {
    for (size_t i = 0; i < q.options.size(); ++i)
        if (q.options[i].value == s) return (int)i;
    for (size_t i = 0; i < q.options.size(); ++i)
        if (!q.options[i].label.empty() && q.options[i].label == s) return (int)i;
    for (size_t i = 0; i < q.options.size(); ++i)
        if (equalsFold(q.options[i].value, s) ||
            (!q.options[i].label.empty() && equalsFold(q.options[i].label, s)))
            return (int)i;
    return -1;
}

std::string fmtNumber(double v) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.10g", v);
    return buf;
}

// Strict number parse: the whole trimmed text must be one finite number.
bool parseNumber(const std::string& text, double& out) {
    const std::string t = trim(text);
    if (t.empty()) return false;
    char* end = nullptr;
    const double v = std::strtod(t.c_str(), &end);
    if (!end || *end != '\0' || !std::isfinite(v)) return false;
    out = v;
    return true;
}

std::vector<std::string> splitPaths(const std::string& text) {
    std::vector<std::string> out;
    std::string cur;
    auto flush = [&]() {
        std::string p = trim(cur);
        if (!p.empty()) out.push_back(std::move(p));
        cur.clear();
    };
    for (char c : text) {
        if (c == ';' || c == '\n' || c == '\r') flush();
        else cur += c;
    }
    flush();
    return out;
}

bool pickFromJson(const nlohmann::json& j, AskPick& out) {
    if (!j.is_object()) return false;
    out.id = jsonStr(j, "id");
    if (out.id.empty()) return false;
    out.kind = jsonStr(j, "kind");
    out.docId = jsonStr(j, "doc_id");
    out.label = jsonStr(j, "label");
    auto it = j.find("selector");
    if (it != j.end() && it->is_object()) out.selector = *it;
    return true;
}

std::string pickText(const nlohmann::json& p) {
    if (!p.is_object()) return scalarText(p);
    const std::string label = jsonStr(p, "label");
    if (!label.empty()) return label;
    const std::string kind = jsonStr(p, "kind");
    const std::string id = jsonStr(p, "id");
    return kind.empty() ? id : kind + " " + id;
}

std::string baseName(const std::string& path) {
    const size_t cut = path.find_last_of("/\\");
    return cut == std::string::npos ? path : path.substr(cut + 1);
}

void prefill(AskQuestion& q) {
    const nlohmann::json& s = q.suggested;
    if (q.kind == "choice") {
        q.checked.assign(q.options.size(), 0);
        if (q.multi) {
            std::vector<std::string> extra;
            auto take = [&](const nlohmann::json& v) {
                const std::string t = scalarText(v);
                if (t.empty()) return;
                const int idx = findOption(q, t);
                if (idx >= 0) q.checked[(size_t)idx] = 1;
                else extra.push_back(t);
            };
            if (s.is_array()) for (const auto& v : s) take(v);
            else take(s);
            if (!extra.empty()) {
                std::string joined;
                for (size_t i = 0; i < extra.size(); ++i) joined += (i ? ", " : "") + extra[i];
                q.otherOn = true;
                askBufSet(q.otherBuf, joined);
            }
        } else {
            const std::string t = scalarText(s);
            if (!t.empty()) {
                const int idx = findOption(q, t);
                if (idx >= 0) {
                    q.choice = idx;
                } else {
                    q.choice = kAskOther;
                    askBufSet(q.otherBuf, t);
                }
            }
        }
    } else if (q.kind == "dimension") {
        const auto& units = askDimUnits(q.family);
        q.unit = units.front();
        double v = 0.0;
        bool have = false;
        if (s.is_object()) {
            auto it = s.find("value");
            if (it != s.end() && it->is_number()) { v = it->get<double>(); have = true; }
            const std::string u = jsonStr(s, "unit");
            if (std::find(units.begin(), units.end(), u) != units.end()) q.unit = u;
        } else if (s.is_number()) {
            v = s.get<double>();
            have = true;
        }
        if (have) askBufSet(q.textBuf, fmtNumber(v));
    } else if (q.kind == "approval") {
        if (s.is_boolean()) {
            q.approval = s.get<bool>() ? 1 : 0;
        } else if (s.is_string()) {
            const std::string t = s.get<std::string>();
            if (equalsFold(t, "true") || equalsFold(t, "yes") || t == "1") q.approval = 1;
            else if (equalsFold(t, "false") || equalsFold(t, "no") || t == "0") q.approval = 0;
        }
    } else if (q.kind == "file_request") {
        std::vector<std::string> paths;
        if (s.is_object()) {
            auto it = s.find("paths");
            if (it != s.end() && it->is_array())
                for (const auto& p : *it)
                    if (p.is_string() && !p.get<std::string>().empty())
                        paths.push_back(p.get<std::string>());
        }
        std::string joined;
        for (size_t i = 0; i < paths.size(); ++i) joined += (i ? "; " : "") + paths[i];
        askBufSet(q.textBuf, joined);
    } else if (q.kind == "geometry_select") {
        AskPick p;
        if (s.is_array()) {
            for (const auto& e : s)
                if (pickFromJson(e, p)) q.picks.push_back(p);
        } else if (pickFromJson(s, p)) {
            q.picks.push_back(p);
        }
        if (!q.multi && q.picks.size() > 1) q.picks.resize(1);
    } else {
        // text and every unknown kind: a free-text answer.
        askBufSet(q.textBuf, s.is_string() ? s.get<std::string>()
                                           : (s.is_null() ? std::string() : s.dump()));
    }
}

}  // namespace

std::string askBufStr(const std::vector<char>& buf) {
    size_t n = 0;
    while (n < buf.size() && buf[n] != '\0') ++n;
    return std::string(buf.data(), n);
}

void askBufSet(std::vector<char>& buf, const std::string& s) {
    if (buf.size() < s.size() + 256) buf.resize(s.size() + 256, 0);
    std::fill(buf.begin(), buf.end(), '\0');
    std::memcpy(buf.data(), s.data(), s.size());
}

const std::vector<std::string>& askDimUnits(const std::string& family) {
    static const std::vector<std::string> kLength{"mm", "cm", "m", "in"};
    static const std::vector<std::string> kAngle{"deg", "rad"};
    static const std::vector<std::string> kCount{"count"};
    if (family == "angle") return kAngle;
    if (family == "count") return kCount;
    return kLength;
}

AskQuestion askQuestionFromJson(const nlohmann::json& j) {
    AskQuestion q;
    if (!j.is_object()) return q;
    q.src = j;
    q.id = jsonStr(j, "id");
    const std::string kind = jsonStr(j, "kind");
    if (!kind.empty()) q.kind = kind;
    q.prompt = jsonStr(j, "prompt");
    if (q.prompt.empty()) q.prompt = jsonStr(j, "ask");   // legacy field name
    q.why = jsonStr(j, "why");
    if (auto it = j.find("suggested"); it != j.end()) q.suggested = *it;
    q.required = jsonBool(j, "required");
    if (auto it = j.find("options"); it != j.end() && it->is_array()) {
        for (const auto& o : *it) {
            AskOption opt;
            if (o.is_object()) {
                opt.value = jsonStr(o, "value");
                opt.label = jsonStr(o, "label");
                if (opt.value.empty()) opt.value = opt.label;
            } else {
                opt.value = scalarText(o);
            }
            if (!opt.value.empty()) q.options.push_back(std::move(opt));
        }
    }
    q.multi = jsonBool(j, "multi");
    q.multiline = jsonBool(j, "multiline");
    q.family = jsonStr(j, "family");
    q.proposal = jsonStr(j, "proposal");
    q.media = jsonStr(j, "media");
    if (auto it = j.find("accept"); it != j.end() && it->is_array())
        for (const auto& a : *it)
            if (a.is_string()) q.accept.push_back(a.get<std::string>());
    q.multiple = jsonBool(j, "multiple");
    if (auto it = j.find("targets"); it != j.end() && it->is_array())
        for (const auto& t : *it)
            if (t.is_string()) q.targets.push_back(t.get<std::string>());
    q.docId = jsonStr(j, "doc_id");
    // A choice with no usable options can only be answered in words.
    if (q.kind == "choice" && q.options.empty()) q.kind = "text";
    prefill(q);
    return q;
}

AskBatch askBatchFromEvent(const nlohmann::json& data) {
    AskBatch b;
    if (!data.is_object()) return b;
    b.batchId = jsonStr(data, "batch_id");
    b.runId = jsonStr(data, "run_id");
    b.sessionId = jsonStr(data, "session_id");
    b.nodeId = jsonStr(data, "node_id");
    b.agentId = jsonStr(data, "agent_id");
    b.prompt = jsonStr(data, "prompt");
    if (auto it = data.find("questions"); it != data.end() && it->is_array())
        for (const auto& q : *it)
            if (q.is_object()) b.questions.push_back(askQuestionFromJson(q));
    return b;
}

nlohmann::json askAnswerValue(const AskQuestion& q) {
    if (q.kind == "choice") {
        const std::string other = trim(askBufStr(q.otherBuf));
        if (q.multi) {
            nlohmann::json arr = nlohmann::json::array();
            for (size_t i = 0; i < q.options.size() && i < q.checked.size(); ++i)
                if (q.checked[i]) arr.push_back(q.options[i].value);
            if (q.otherOn && !other.empty()) arr.push_back(other);
            // An empty list is a real answer ("none of these") only when not required.
            if (arr.empty() && q.required) return nullptr;
            return arr;
        }
        if (q.choice >= 0 && q.choice < (int)q.options.size())
            return q.options[(size_t)q.choice].value;
        if (q.choice == kAskOther && !other.empty()) return other;
        return nullptr;
    }
    if (q.kind == "dimension") {
        double v = 0.0;
        if (!parseNumber(askBufStr(q.textBuf), v)) return nullptr;
        const auto& units = askDimUnits(q.family);
        const std::string unit = q.unit.empty() ? units.front() : q.unit;
        nlohmann::json out = nlohmann::json::object();
        if (q.family == "count" && std::floor(v) == v && std::fabs(v) < 1e15)
            out["value"] = (long long)v;
        else
            out["value"] = v;
        out["unit"] = unit;
        return out;
    }
    if (q.kind == "approval") {
        if (q.approval < 0) return nullptr;
        return q.approval == 1;
    }
    if (q.kind == "file_request") {
        std::vector<std::string> paths = splitPaths(askBufStr(q.textBuf));
        if (paths.empty()) return nullptr;
        if (!q.multiple && paths.size() > 1) paths.resize(1);
        return nlohmann::json{{"paths", paths}};
    }
    if (q.kind == "geometry_select") {
        if (q.picks.empty()) return nullptr;
        if (!q.multi) return askPickToJson(q.picks.front());
        nlohmann::json arr = nlohmann::json::array();
        for (const auto& p : q.picks) arr.push_back(askPickToJson(p));
        return arr;
    }
    const std::string t = trim(askBufStr(q.textBuf));
    if (t.empty()) return nullptr;
    return t;
}

nlohmann::json buildAskAnswers(const AskBatch& b) {
    nlohmann::json out = nlohmann::json::object();
    for (const auto& q : b.questions) {
        if (q.id.empty()) continue;
        nlohmann::json v = askAnswerValue(q);
        if (!v.is_null()) out[q.id] = std::move(v);
    }
    return out;
}

bool askBatchReady(const AskBatch& b, std::string* why) {
    for (const auto& q : b.questions) {
        const std::string name = q.prompt.empty() ? q.id : q.prompt;
        if (q.kind == "dimension") {
            const std::string typed = trim(askBufStr(q.textBuf));
            double v = 0.0;
            if (!typed.empty() && !parseNumber(typed, v)) {
                if (why) *why = "Enter a number for \"" + name + "\".";
                return false;
            }
        }
        if (q.required && askAnswerValue(q).is_null()) {
            if (why) *why = "Answer the required question \"" + name + "\".";
            return false;
        }
    }
    if (why) why->clear();
    return true;
}

std::string askAnswerText(const AskQuestion& q, const nlohmann::json& raw) {
    if (raw.is_null()) return "(no answer)";
    if (q.kind == "choice") {
        auto one = [&](const nlohmann::json& v) {
            const std::string t = scalarText(v);
            for (const auto& o : q.options)
                if (o.value == t) return o.label.empty() ? o.value : o.label;
            return t;
        };
        if (raw.is_array()) {
            if (raw.empty()) return "(none)";
            std::string out;
            for (size_t i = 0; i < raw.size(); ++i) out += (i ? ", " : "") + one(raw[i]);
            return out;
        }
        return one(raw);
    }
    if (q.kind == "dimension" && raw.is_object()) {
        auto it = raw.find("value");
        const std::string v = (it != raw.end() && it->is_number())
                                  ? fmtNumber(it->get<double>()) : scalarText(raw.value("value", nlohmann::json()));
        const std::string unit = jsonStr(raw, "unit");
        return (unit.empty() || unit == "count") ? v : v + " " + unit;
    }
    if (raw.is_boolean()) return raw.get<bool>() ? "Approved" : "Declined";
    if (q.kind == "file_request" && raw.is_object()) {
        std::string out;
        auto it = raw.find("paths");
        if (it != raw.end() && it->is_array())
            for (const auto& p : *it)
                if (p.is_string()) out += (out.empty() ? "" : ", ") + baseName(p.get<std::string>());
        return out.empty() ? "(no files)" : out;
    }
    if (q.kind == "geometry_select") {
        if (raw.is_array()) {
            if (raw.empty()) return "(nothing picked)";
            std::string out;
            for (size_t i = 0; i < raw.size(); ++i) out += (i ? ", " : "") + pickText(raw[i]);
            return out;
        }
        return pickText(raw);
    }
    if (raw.is_string()) return raw.get<std::string>();
    return raw.dump();
}

const char* askStatusName(AskStatus s) {
    switch (s) {
        case AskStatus::Pending: return "pending";
        case AskStatus::Submitting: return "submitting";
        case AskStatus::Answered: return "answered";
        case AskStatus::Reply: return "reply";
        case AskStatus::Declined: return "declined";
        case AskStatus::Cancelled: return "cancelled";
        case AskStatus::Expired: return "expired";
    }
    return "pending";
}

AskStatus askStatusParse(const std::string& s) {
    if (s == "submitting") return AskStatus::Submitting;
    if (s == "answered" || s == "user") return AskStatus::Answered;
    if (s == "reply") return AskStatus::Reply;
    if (s == "declined") return AskStatus::Declined;
    if (s == "cancelled") return AskStatus::Cancelled;
    if (s == "expired") return AskStatus::Expired;
    return AskStatus::Pending;
}

bool askStatusOpen(AskStatus s) {
    return s == AskStatus::Pending || s == AskStatus::Submitting;
}

void applyAskAnswerEvent(AskBatch& b, const nlohmann::json& data) {
    if (!data.is_object()) return;
    const std::string source = jsonStr(data, "source");
    AskStatus next = AskStatus::Answered;
    if (source == "reply") next = AskStatus::Reply;
    else if (source == "declined") next = AskStatus::Declined;
    else if (source == "cancelled") next = AskStatus::Cancelled;
    const bool userResolved = b.status == AskStatus::Answered || b.status == AskStatus::Reply ||
                              b.status == AskStatus::Declined;
    if (next == AskStatus::Cancelled && userResolved) return;
    b.status = next;
    b.error.clear();
    if (auto it = data.find("answers"); it != data.end() && it->is_object()) b.answers = *it;
    const std::string reply = jsonStr(data, "reply");
    if (!reply.empty()) b.reply = reply;
}

nlohmann::json askBatchToJson(const AskBatch& b) {
    nlohmann::json qs = nlohmann::json::array();
    for (const auto& q : b.questions) qs.push_back(q.src);
    nlohmann::json j = {
        {"batch_id", b.batchId},
        {"run_id", b.runId},
        {"questions", std::move(qs)},
        {"status", askStatusName(b.status)},
    };
    if (!b.sessionId.empty()) j["session_id"] = b.sessionId;
    if (!b.nodeId.empty()) j["node_id"] = b.nodeId;
    if (!b.agentId.empty()) j["agent_id"] = b.agentId;
    if (!b.prompt.empty()) j["prompt"] = b.prompt;
    if (!b.error.empty()) j["error"] = b.error;
    if (b.answers.is_object() && !b.answers.empty()) j["answers"] = b.answers;
    if (!b.reply.empty()) j["reply"] = b.reply;
    return j;
}

AskBatch askBatchFromJson(const nlohmann::json& j) {
    AskBatch b = askBatchFromEvent(j);
    if (!j.is_object()) return b;
    b.status = askStatusParse(jsonStr(j, "status"));
    b.error = jsonStr(j, "error");
    if (auto it = j.find("answers"); it != j.end() && it->is_object()) b.answers = *it;
    b.reply = jsonStr(j, "reply");
    if (askStatusOpen(b.status)) {
        b.status = AskStatus::Expired;
        if (b.error.empty()) b.error = "The run that asked has ended.";
    }
    return b;
}

nlohmann::json askPickToJson(const AskPick& p) {
    nlohmann::json j = {{"kind", p.kind}, {"id", p.id}};
    if (!p.docId.empty()) j["doc_id"] = p.docId;
    if (!p.label.empty()) j["label"] = p.label;
    if (p.selector.is_object() && !p.selector.empty()) j["selector"] = p.selector;
    return j;
}
