// core/agent/clarify.h — pre-flight CLARIFY gate for the in-app Copilot.
//
// Before a Design run, one cheap provider call decides whether the user's request already
// carries the build-critical dimensions/fits, and if not surfaces the FEWEST targeted
// questions to get them. Structured like ask.{h,cpp} (assist one-shot, pure + gateable):
//   build_clarify_prompt — (system, body) for the single provider call
//   clarify              — ONE provider completion + JSON parse, with a never-block fallback
//
// Like build_ask_prompt, the doc context is passed as explicit INPUTS (doc | null, doc_id,
// revision, feature_status) so this is trivially gateable without a live session. For a
// Design-new request `doc` is null and only the request matters; for Edit it adds context.
#pragma once

#include <functional>
#include <string>

#include "agent/state.h"  // ojson

namespace aicad {
namespace agent {

extern const char* kClarifyRole;    // "clarify"
extern const char* kClarifySystem;  // the verbatim system string

struct ClarifyPrompt {
    std::string system;
    std::string body;  // the single user message's text
};

// (system, body) for the single clarify provider call. Reuses build_doc_context (ask.h)
// for the optional document digest, then appends the request.
ClarifyPrompt build_clarify_prompt(const std::string& request, const ojson& doc,
                                   const std::string& doc_id, long revision,
                                   const ojson& feature_status);

using ClarifyCompleteFn =
    std::function<std::string(const std::string& system, const std::string& body)>;

// ONE provider completion + JSON parse. Returns a normalized object:
//   {"ready": <bool>, "questions": [{key, ask, unit, suggested}], "assumptions": [str,...]}
// Questions are capped at 4 and each is guaranteed key/ask/unit/suggested string fields.
// On ANY parse/shape failure -> {"ready": true, "questions": [], "assumptions": []} so a
// weak local model can never block the build.
ojson clarify(const std::string& request, const ojson& doc, const std::string& doc_id,
              long revision, const ojson& feature_status, const ClarifyCompleteFn& complete);

}  // namespace agent
}  // namespace aicad
