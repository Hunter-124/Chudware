// core/agent/ask.h — C++-only Ask mode (Python agent/ask.py mirror removed).
//
// Ask mode: answer QUESTIONS about the open parametric part, mutating nothing.
// Everything prompt-shaped lives here so it is gateable without the worker:
//   build_doc_context — the compact document digest
//   build_ask_prompt  — (system, body) for the single ask-mode provider call
//   answer_question   — ONE provider completion + the empty-reply fallback
//
// To stay PURE + trivially gateable, build_doc_context takes the doc CONTEXT
// INPUTS explicitly (doc | null, doc_id, revision, feature_status) rather than a
// live session object; the host passes these from its open C++ session.
#pragma once

#include <functional>
#include <string>

#include "agent/state.h"  // ojson

namespace aicad {
namespace agent {

extern const char* kAskRole;    // "ask"
extern const char* kAskSystem;  // the verbatim _SYSTEM string

// _param_summary(params, limit=8): first `limit` items; dict-with-"value" ->
// "k=value"; int/float/str/bool -> "k=v" (Python str()); join ", ".
std::string ask_param_summary(const ojson& params, int limit = 8);

// summarize_features(doc): first 60 features; "<id> <type> <name>[ [suppressed]]"
// rstripped, + " (dims)" when present; "... (+N more features)" past 60; join "\n".
std::string summarize_features(const ojson& doc);

// _status_line from a feature_status list ([{id,ok,error}, ...]).
std::string ask_status_line(const ojson& feature_status);

// The compact document context block. `doc` may be null (or non-object) -> the
// "NO DOCUMENT IS OPEN..." string. doc_id/revision/feature_status come from the
// host session.
std::string build_doc_context(const ojson& doc, const std::string& doc_id, long revision,
                              const ojson& feature_status);

struct AskPrompt {
    std::string system;
    std::string body;  // the single user message's text
};

// (system, body) for the single ask-mode provider call.
AskPrompt build_ask_prompt(const std::string& question, const ojson& doc,
                           const std::string& doc_id, long revision,
                           const ojson& feature_status);

// answer_question: ONE provider completion (caller supplies the completion fn);
// returns reply.strip() or the fallback string.
using AskCompleteFn =
    std::function<std::string(const std::string& system, const std::string& body)>;
std::string answer_question(const std::string& question, const ojson& doc,
                            const std::string& doc_id, long revision,
                            const ojson& feature_status, const AskCompleteFn& complete);

}  // namespace agent
}  // namespace aicad
