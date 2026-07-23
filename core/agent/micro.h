// core/agent/micro.h — C++ port of agent/orchestrator/micro.py (P54a).
//
// The MICRO-ORCHESTRATION profile: a tiny-context path for ultra-quantized local
// models. Instead of one big modeler call, it runs a chain of MINUSCULE,
// self-contained calls:
//   DECOMPOSER (one call)  -> ordered JSON list of single-feature steps (cap 12)
//   EXECUTOR   (one/step)  -> ONE doc-op JSON array per step, applied immediately;
//                              on a failure, ONE retry with the error appended
//   VERIFIER   (pure C++)  -> resolved params vs the step intent (dims + volume dir)
//
// run_micro_design() parallels run_design() (run.h) and reuses the same already-
// ported primitives (providers / ops_emit / events / state / roles / config) and
// the live doc session (doc/session.h). The libchudware boundary exposes:
//   agent_run_micro          — the live end-to-end run (returns {result + events}).
//   agent_should_engage      — the pure engagement decision (truth table).
//   agent_micro_normalize_steps / agent_micro_summarize_doc /
//   agent_micro_verify_step  — pure unit-level gating probes.
#pragma once

#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "agent/state.h"  // ojson

namespace chudware {
namespace agent {

using nlohmann::json;
using EmitFnMicro = std::function<void(const json&)>;

// Per-call budgets (token estimate = chars // 4, via state::est_tokens).
constexpr int kDecomposeSystemBudgetTokens = 400;
constexpr int kExecPromptBudgetTokens = 2000;
constexpr int kDocSummaryBudgetTokens = 300;
constexpr int kMaxSteps = 12;

// --------------------------------------------------------------------------- engagement

// should_engage(provider_cfg, cfg). provider_cfg carries {name, base_url?,
// context_tokens?}; cfg carries {micro_mode?, context_tokens?}. Pure decision.
bool should_engage(const json& provider_cfg, const json& cfg);

// --------------------------------------------------------------------------- JSON array extract

// extract_json_array(reply): fenced ```json block -> list (or its "steps" list),
// else a bare first-balanced [..] list. Returns the parsed array, or null on miss.
ojson extract_json_array(const std::string& reply);

// --------------------------------------------------------------------------- step model

struct Step {
    std::string type;
    std::string desc;
    ojson params = ojson::object();
};

// Coerce the decomposer's raw list into Steps, hard-capped at kMaxSteps.
std::vector<Step> normalize_steps(const ojson& raw);

// --------------------------------------------------------------------------- doc summary

// total_volume(body_count, volume): None when no bodies, else the total solid vol.
std::optional<double> total_volume(int body_count, double volume);

// A <=budget compact tree summary. `vol` present => a "volume=N mm^3" head line.
std::string summarize_doc(const ojson& tree, const std::optional<double>& vol,
                          int budget_tokens = kDocSummaryBudgetTokens);

// --------------------------------------------------------------------------- prompt assembly

// (system, body). build_decompose_prompt() returns the decomposer system (the body
// is the caller's REQUEST text). build_executor_prompt() returns (system, body) for
// one step with the budget-trim remediation applied.
std::string build_decompose_prompt(const std::string& prompts_dir = "");

struct ExecutorPrompt {
    std::string system;
    std::string body;
};
ExecutorPrompt build_executor_prompt(const Step& step, const std::string& doc_summary,
                                     const std::string& request,
                                     const std::string& error_feedback = "",
                                     const std::string& prompts_dir = "");

// --------------------------------------------------------------------------- verify

struct VerifyResult {
    bool verified = false;
    std::string notes;
};
VerifyResult verify_step(const Step& step, const ojson& tree, const ojson& feature_status,
                         const std::optional<double>& vol_before,
                         const std::optional<double>& vol_after);

// --------------------------------------------------------------------------- runner

// run_micro_design — drive the MICRO profile against a LIVE doc session.
// Returns {run_id, status, stop_reason, revision, doc_id}.
json run_micro_design(const std::string& request, const std::string& doc_id,
                      const json& provider_cfg, EmitFnMicro emit = nullptr,
                      const json& options = json::object());

}  // namespace agent
}  // namespace chudware
