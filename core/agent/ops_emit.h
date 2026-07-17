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
#include <string>

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

// True if the reply carries a {"revert": true} verdict (fenced or bare {..}).
bool extract_revert(const std::string& reply);

// The escape-hatch op: add_feature{type:"script"} wrapping a build123d script.
json script_escape_op(const std::string& code, const std::string& name = "Script");

// First python-fenced (or any-fenced fallback) block whose body assigns
// result/parts/assembly. nullopt if none.
std::optional<std::string> extract_script(const std::string& reply);

}  // namespace agent
}  // namespace aicad
