// core/agent/state.h — C++ port of agent/orchestrator/state.py (P53a).
//
// DesignState is the shared, compact source every agent call assembles its
// FRESH bounded context from. build_context(state, role) returns
//   (system_text, user_body, context_chars, context_tokens_est)
// keeping the estimate under CONTEXT_TOKEN_CAP by trimming the lowest-priority
// blocks. Digests (tree/params), the edit-bias heuristic, and the per-stage
// AttemptLedger are ported byte-for-byte. The json.dumps(...,separators=(",",
// ": "),indent=0) spec/plan formatting is replicated by py_json_dumps_indent0.
#pragma once

#include <functional>
#include <map>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace aicad {
namespace agent {

// Use ordered_json so object key iteration preserves INSERTION order, matching
// Python dict order — essential for digest_params/digest_tree and the
// json.dumps(...,indent=0) spec/plan blocks to be byte-identical to the oracle.
// (Named ojson, NOT a namespace-level `json` alias, to avoid clashing with the
// `using nlohmann::json` in events.h/roles.h.)
using ojson = nlohmann::ordered_json;

// Per-call context budget (<=6k est tokens). Estimate = chars / 4 (ceil).
constexpr int kContextTokenCap = 6000;
constexpr int kCharsPerToken = 4;

// est_tokens(t) = (len + 3) // 4.
int est_tokens(const std::string& text);

// Replicates Python json.dumps(obj, separators=(",", ": "), indent=0):
// newline between every container item, no indentation, ": " key separator,
// "," item separator, empty {}/[] stay on one line, ensure_ascii \uXXXX escapes.
std::string py_json_dumps_indent0(const ojson& obj);

// --------------------------------------------------------------------------- ledger

// Per-stage attempt counts + the running error signatures (identical-failure stop).
struct AttemptLedger {
    std::map<std::string, int> attempts;
    std::map<std::string, std::string> last_sigs;
    std::map<std::string, int> sig_streak;

    int bump(const std::string& stage);
    int count(const std::string& stage) const;
    // Record sig for stage; return the consecutive-identical streak.
    int note_sig(const std::string& stage, const std::string& sig);
};

// --------------------------------------------------------------------------- state

struct DesignState {
    std::string request;
    std::string doc_id;
    int revision = 0;
    ojson spec = nullptr;            // dict | null
    ojson plan = nullptr;            // dict | null
    std::string tree_digest;
    ojson last_render = nullptr;     // str | null
    std::vector<std::string> last_errors;
    std::vector<std::string> last_issues;
    ojson memory = ojson::object();   // dict
    AttemptLedger ledger;
    std::string current_step;

    // Dynamic attr the model action sets (stagnation signal); see machine.py.
    bool last_no_progress = false;

    void update_tree(const ojson& tree);
    void update_revision(int rev) { revision = rev; }
};

// --------------------------------------------------------------------------- digests

// k=v summary of a feature's key params (first `limit`; dim-objects + scalars).
std::string digest_params(const ojson& params, int limit = 6);

// One compact line per feature (first `max_lines`) + "[supp]" marker + (dims).
std::string digest_tree(const ojson& tree, int max_lines = 40);

// True when *request* reads like a modification of existing geometry.
bool looks_like_edit(const std::string& request);

// Errors from a rebuild's feature_status list ("{id}: {error}").
std::vector<std::string> feature_status_errors(const ojson& feature_status);

extern const char* kEditBiasText;

// --------------------------------------------------------------------------- context

struct BuiltContext {
    std::string system;
    std::string body;  // the single user message's joined text
    int context_chars = 0;
    int context_tokens_est = 0;
};

// Assemble the FRESH bounded context for *role*. prompts_dir overrides the env
// builtin dir (so the gate can point C++ at the same prompt files Python reads).
BuiltContext build_context(const DesignState& state, const std::string& role,
                           const std::string& prompts_dir = "");

}  // namespace agent
}  // namespace aicad
