// core/agent/ops_emit.h — C++ port of agent/orchestrator/ops_emit.py.
//
// P52: pull + classify the modeler/fixer's doc-ops array (and the script
// escape-hatch / revert verdict) out of a model reply. PURE string functions,
// fully differential-gateable vs the Python `re`/`json` oracle.
//
// dry_run() (the engine.doc.ops validation path) is intentionally NOT ported
// here — it belongs with the wired doc session (deferred to P53).
#pragma once

#include <optional>
#include <set>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace aicad {
namespace agent {

using nlohmann::json;

// First depth-tracked balanced open_ch..close_ch span (string-aware: braces
// inside "..." with \ escapes are skipped). Returns the substring incl. both
// delimiters, or nullopt. Hand-written scan (mirrors _first_balanced).
std::optional<std::string> first_balanced(const std::string& text, char open_ch, char close_ch);

// extract_ops -> (ops, error). Exactly one of the two is set on return.
struct OpsResult {
    std::optional<json> ops;     // a JSON array of ops on success.
    std::optional<std::string> error;  // failure reason otherwise.
};
OpsResult extract_ops(const std::string& reply);

// Reasoning-channel fallback. grok-4.5 (and other CoT models) sometimes emit the
// FINAL ops array in their reasoning stream while the content channel is empty or a
// bare `[]`. This scans every fenced/bare JSON candidate and returns the LAST
// non-empty ops array (earlier arrays in a reasoning stream are drafts/fragments).
// Only call this when extract_ops(content) yielded no usable (non-empty) ops.
OpsResult extract_ops_last_nonempty(const std::string& reply);

// Peel leading/sole request_tools ops out of a model-emitted ops array.
// request_tools is a harness meta-op (never sent to doc_apply_ops):
//   {"op":"request_tools","classes":["full_ops"|"sheetmetal"|"weldments"|"surfacing"]}
// Normalizes aliases (weldment->weldments). Populates `requested` with unique
// recognized class names in first-seen order. `remaining` is the ops array with
// every request_tools entry stripped (may be empty).
struct ToolScopeRequest {
    std::vector<std::string> requested;
    json remaining = json::array();
    bool had_request = false;  // true if at least one request_tools op was present
};
ToolScopeRequest peel_request_tools(const json& ops);

// Peel leading/sole request_advice meta-ops (never sent to doc_apply_ops):
//   {"op":"request_advice","question":"...","context":"..."?}
// `remaining` strips every request_advice entry. First non-empty question wins.
struct AdviceRequest {
    std::string question;
    std::string context;   // optional free-form context from the model
    bool had_request = false;
    json remaining = json::array();
};
AdviceRequest peel_request_advice(const json& ops);

// Peel leading/sole delegate meta-ops (never sent to doc_apply_ops):
//   {"op":"delegate","task":"...","why":"..."?}
// First non-empty task wins. Cap/mode refusal is the caller's job.
struct DelegateRequest {
    std::string task;
    std::string why;
    bool had_request = false;
    json remaining = json::array();
};
DelegateRequest peel_delegate(const json& ops);

// Parse advisor role JSON from a model reply (fenced or bare object).
struct AdvisorAdvice {
    std::string advice;
    std::string confidence;  // "high"|"med"|"low" (empty if absent)
    bool ok = false;         // true when advice non-empty
};
AdvisorAdvice parse_advisor_advice(const std::string& reply);

struct AdvisorPlanVerdict {
    std::string verdict;     // "approve"|"revise"|"escalate" (default approve if unparseable)
    std::string note;
    std::string confidence;
    bool ok = false;         // true when a recognized verdict was present
};
AdvisorPlanVerdict parse_advisor_plan_verdict(const std::string& reply);

// Map complexity structure → copilot execution_style ("edit"|"swarm").
// eng_firm is never recommended — only set by explicit mode.
// Solo/Subagent → edit; Dag/Company → swarm.
const char* execution_style_from_structure(const char* structure_label);

// True if the reply carries a {"revert": true} verdict (fenced or bare {..}).
bool extract_revert(const std::string& reply);

// The escape-hatch op: add_feature{type:"script"} wrapping a build123d script.
json script_escape_op(const std::string& code, const std::string& name = "Script");

// First python-fenced (or any-fenced fallback) block whose body assigns
// result/parts/assembly. nullopt if none.
std::optional<std::string> extract_script(const std::string& reply);

}  // namespace agent
}  // namespace aicad
