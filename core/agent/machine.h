// core/agent/machine.h — C++ port of agent/orchestrator/machine.py (P53a).
//
// The deterministic orchestrator state machine: CODE decides every transition;
// an Action only fills each stage's bounded prompt. run_machine drives
//   INTAKE -> PLAN -> MODEL(loop) -> REVIEW -> FIX(loop) -> MEMORY -> DONE
// with per-stage attempt budgets, the ported err_sig identical-failure detector,
// a stagnation guard, and a global iteration cap. Decoupled from the live
// session: StageActions bundles the callables; run.py LIVE wiring is P53b.
#pragma once

#include <functional>
#include <map>
#include <string>

#include <nlohmann/json.hpp>

#include "agent/events.h"
#include "agent/state.h"

namespace aicad {
namespace agent {

using nlohmann::json;

extern const std::map<std::string, int> kDefaultBudgets;
// Global provider-call cap across the whole run. Raised from 40 to absorb per-feature step
// decomposition (a part with several features now spans several modeler steps).
constexpr int kGlobalIterCap = 60;
constexpr int kSameSigLimit = 3;
constexpr int kStagnationLimit = 3;

// A stable signature for an error string: the exception line if present, else
// the last nonempty line. Hand-rolled regex check (no std::regex).
std::string err_sig(const std::string& err);

// What an Action reports after one attempt of a stage.
struct StageOutcome {
    std::string verdict;  // "pass" | "retry" | "fail" | "skip" | "pause"
    // "pause" (VERDICT_PAUSE) — used by the DAG gate contract (contracts/dag_schema.md §6)
    // to suspend execution at a human-approval checkpoint.  The LangGraph interrupt()
    // mechanism in services/dag/gate.py owns the actual suspension; this value is the
    // C++ sentinel that signals an in-progress stage machine should yield to human input.
    std::string reason;
    bool has_error = false;  // Python error=None vs a string
    std::string error;
    bool done = false;
    std::string raw;
    bool parsed_ok = true;
    bool has_parse_error = false;
    std::string parse_error;
    bool has_ops_count = false;
    int ops_count = 0;
    int duration_ms = 0;
    std::string prompt_file;
    std::string prompt_hash;
    int context_chars = 0;
    int context_tokens_est = 0;
    std::string inputs_digest;
    bool has_tokens = false;
    json tokens = nullptr;
    bool no_progress = false;
};

// Action takes (state, attempt) -> StageOutcome (mutating state as a side effect).
using Action = std::function<StageOutcome(DesignState&, int)>;

struct StageActions {
    Action intake;
    Action plan;
    Action model;
    Action review;
    Action fix;
    Action memory;
    std::function<std::vector<std::string>(DesignState&)> plan_steps;
};

// Cooperative-cancel flag, checked at every stage boundary.
struct RunControl {
    std::function<bool()> cancelled = [] { return false; };
};

// Drive the state machine to completion. Returns {status, stop_reason}.
json run_machine(DesignState& state, const StageActions& actions, RunEvents& events,
                 const json& budgets = json::object(), RunControl control = RunControl());

}  // namespace agent
}  // namespace aicad
