// core/agent/run_stop.cpp — pure run helpers kept separate from document execution.
//
// These rules belong to run.cpp, but run.cpp pulls in the whole chudware::doc::* session
// API, and the agent tests (core/tests/test_agent.cpp) deliberately link only
// `chudware_agent` — no doc, no OCCT. Defining the helpers here lets that test LINK the
// shipped implementation instead of restating it in a mirrored lambda, which is how the
// older stop_reason cases in test_agent.cpp were written and exactly how a rule and its
// test drift.

#include "agent/run.h"

#include <algorithm>
#include <cctype>
#include <regex>

namespace chudware {
namespace agent {
// Sanitize a model-chosen part name into a short filename-safe display title.
// Illegal path chars stripped; whitespace collapsed; capped at 48 chars.
std::string sanitize_part_name(std::string name) {
    // Trim ends.
    auto not_ws = [](unsigned char c) { return !std::isspace(c); };
    name.erase(name.begin(), std::find_if(name.begin(), name.end(), not_ws));
    name.erase(std::find_if(name.rbegin(), name.rend(), not_ws).base(), name.end());

    // One design is one document: a version token in the name creates a second tab, a second
    // .step+, its own vault item, and a recovery row; revisions belong in PLM. Keep this in
    // lockstep with services/mcp/cad_common.py::_strip_version_suffix. Accepted false positive:
    // a genuine designator such as Intake_Manifold_V8 becomes Intake_Manifold; a second,
    // divergent definition of "version token" would be worse than that known cost.
    static const std::regex version_suffix(
        R"([\s._-]+(?:(?:v|ver|vers|version|rev|revision|mk|mark|gen|iter|iteration|attempt|try|pass|round)\s*\d{1,4}|(?:rev|revision)\s*[a-z]|final\s*\d{1,4})$)",
        std::regex_constants::icase);
    std::smatch version_match;
    while (std::regex_search(name, version_match, version_suffix)) {
        const size_t prefix_end = static_cast<size_t>(version_match.position());
        if (prefix_end == 0) break;
        const size_t last = name.find_last_not_of(" ._-", prefix_end - 1);
        if (last == std::string::npos) break;
        name.erase(last + 1);
    }

    // Strip illegal filename characters: / \ : * ? " < > |
    std::string cleaned;
    cleaned.reserve(name.size());
    for (unsigned char c : name) {
        if (c == '/' || c == '\\' || c == ':' || c == '*' || c == '?' || c == '"' || c == '<' ||
            c == '>' || c == '|')
            continue;
        cleaned.push_back(static_cast<char>(c));
    }
    // Collapse whitespace runs to a single space.
    std::string out;
    out.reserve(cleaned.size());
    bool prev_ws = false;
    for (unsigned char c : cleaned) {
        if (std::isspace(c)) {
            if (!prev_ws && !out.empty()) out.push_back(' ');
            prev_ws = true;
        } else {
            out.push_back(static_cast<char>(c));
            prev_ws = false;
        }
    }
    // Trailing space from collapse (shouldn't, but belt+suspenders).
    while (!out.empty() && out.back() == ' ') out.pop_back();
    if (out.size() > 48) {
        out.resize(48);
        while (!out.empty() && out.back() == ' ') out.pop_back();
    }
    return out;
}

WorkerStop classify_worker_stop(bool crashed, bool timed_out, bool reached_worker) {
    // A crash and a silence are BOTH Chudware failing, not the user asking for
    // something unrunnable, so both get status "failed" and their own stop_reason.
    // `crashed` wins when both are set: a worker killed on the timeout path still
    // reports a signal, and the crash is the more specific fact.
    if (crashed) return {"worker_crash", "failed", true};
    if (timed_out) return {"worker_timeout", "failed", true};
    // Only a pre-flight refusal that never reached the worker is genuinely a
    // non-modeling request. Any worker-side preflight/transport failure is a
    // harness failure; preserving that distinction keeps auth, actor, provider,
    // and serialization defects out of the user's request-classification bucket.
    if (!reached_worker) return {"non_modeling_request", "blocked", false};
    return {"worker_error", "failed", true};
}

std::string general_lane_status(bool turn_ok, const std::string& handoff_status,
                                const std::string& stop_reason) {
    if (turn_ok) return "done";
    // Order matters: `partial` means geometry WAS produced and some of it failed, which
    // the host renders differently from a stop that produced nothing.
    if (handoff_status == "partial") return "partial";
    if (stop_reason == "guardrail_blocked") return "blocked";
    // The model's own considered outcome. A decline it explained is not a fault, and
    // reporting it as one both mislabels the model and buries the explanation.
    if (handoff_status == "blocked" || handoff_status == "escalated") return "blocked";
    return "failed";
}

std::string general_lane_error_detail(const std::string& turn_error_detail,
                                      const std::string& status,
                                      const std::string& summary) {
    // An error_detail the turn built itself is already the contract shape — never
    // second-guess it.
    if (!turn_error_detail.empty()) return turn_error_detail;
    if (status == "done" || summary.empty()) return std::string();
    // events.jsonl is the forensics record and `error_detail: null` on a stopped run
    // destroys the only statement of why. Carry the turn's own words, clipped to the
    // ~300-char human sentence contracts/agent_events.md specifies. The clip is on a
    // UTF-8 boundary: cutting mid-sequence would emit an invalid byte into the event
    // stream and the chat card.
    if (summary.size() <= 300) return summary;
    size_t cut = 299;
    while (cut > 0 && (static_cast<unsigned char>(summary[cut]) & 0xC0) == 0x80) --cut;
    return summary.substr(0, cut) + "\xE2\x80\xA6";  // U+2026 HORIZONTAL ELLIPSIS
}

}  // namespace agent
}  // namespace chudware
