// core/agent/pm_assist.h — C++ port of agent/pm_assist.py (P54b).
//
// PropertyManager AI assist: ask the active provider for a strict JSON action
// plan, then validate it against the visible PropertyManager schema. The worker
// never mutates the document and never invents topology refs. All the pure
// logic (field map, json extraction, value coercion, plan validation, the
// heuristic fallback, and prompt assembly) is ported byte-for-byte and gateable
// without the worker.
#pragma once

#include <functional>
#include <string>

#include "agent/state.h"  // ojson

namespace chudware {
namespace agent {

extern const char* kPmAssistRole;    // "pm_assist"
extern const char* kPmAssistSystem;  // the verbatim _SYSTEM string

// _field_map(form_schema, visible_only): {key: field} for fields with a
// non-empty key (skip non-dict); visible_only skips visible-falsy (default true).
ojson pm_field_map(const ojson& form_schema, bool visible_only);

// _extract_json(reply) -> a dict (ojson object) or {} on any miss.
ojson pm_extract_json(const std::string& reply);

// _coerce_patch_value(field, value) -> the coerced value, or null when rejected.
// (A JSON null is the sentinel; callers test against null.)
ojson pm_coerce_patch_value(const ojson& field, const ojson& value);

// _validate_plan(plan, form_schema, current) -> the validated action dict.
ojson pm_validate_plan(const ojson& plan, const ojson& form_schema, const ojson& current);

// _heuristic_plan(form_schema, current, intent) -> a raw plan dict.
ojson pm_heuristic_plan(const ojson& form_schema, const ojson& current, const std::string& intent);

struct PmAssistPrompt {
    std::string system;
    std::string body;
};

// build_pm_assist_prompt(params, <doc ctx inputs>) -> (system, body). params
// carries {form_schema, current, intent, selected_refs}. The doc context inputs
// (doc | null, doc_id, revision, feature_status) come from the host session.
PmAssistPrompt build_pm_assist_prompt(const ojson& params, const ojson& doc,
                                      const std::string& doc_id, long revision,
                                      const ojson& feature_status);

// assist_property_manager: if a completion fn is supplied, extract a plan from
// its reply (on a thrown error, capture provider_error); else / on empty plan,
// fall back to the heuristic (appending " Provider fallback: {err}" when set);
// return _validate_plan. The completion fn returns the raw reply text; it may
// signal a provider error by setting `*err` (so we can replicate the fallback).
using PmCompleteFn = std::function<std::string(const std::string& system, const std::string& body,
                                               std::string* err)>;
ojson assist_property_manager(const ojson& params, const ojson& doc, const std::string& doc_id,
                              long revision, const ojson& feature_status,
                              const PmCompleteFn& complete);

}  // namespace agent
}  // namespace chudware
