// core/agent/machine.h — C++ port of agent/orchestrator/machine.py (P53a).
//
// The deterministic orchestrator state machine: CODE decides every transition;
// an Action only fills each stage's bounded prompt. run_machine drives
//   INTAKE -> PLAN -> MODEL(loop) -> REVIEW -> FIX(loop) -> MEMORY -> DONE
// with per-stage attempt budgets, the ported err_sig identical-failure detector,
// a stagnation guard, and a global iteration cap. Decoupled from the live
// session: StageActions bundles the callables; run.py LIVE wiring is P53b.
#pragma once

#include <atomic>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "agent/events.h"
#include "agent/state.h"

namespace chudware {
namespace agent {

using nlohmann::json;

extern const std::map<std::string, int> kDefaultBudgets;
// Global provider-call cap across the whole run. Raised from 40 to absorb per-feature step
// decomposition (a part with several features now spans several modeler steps).
constexpr int kGlobalIterCap = 60;
constexpr int kSameSigLimit = 3;
constexpr int kStagnationLimit = 3;

// Stage classification for strong-plan / cheap-implement routing. Planning
// stages keep the main provider; implementation stages may use an explicitly
// supplied provider descriptor.
bool is_planning_stage(const std::string& stage);       // intake|plan|decompose|review|memory
bool is_implementation_stage(const std::string& stage);  // model|fix|execute|build

// A stable signature for an error string: the exception line if present, else
// the last nonempty line. Hand-rolled regex check (no std::regex).
std::string err_sig(const std::string& err);

// What an Action reports after one attempt of a stage.
struct StageOutcome {
    std::string verdict;  // "pass" | "retry" | "fail" | "skip" | "pause"
    // "pause" (VERDICT_PAUSE) — RESERVED for the DAG gate contract (contracts/dag_schema.md §6).
    // NOT WIRED in C++: machine.cpp run_stage() treats only pass/fail/skip/cancelled as
    // terminal, so an Action returning "pause" today would be retried until the stage
    // budget is exhausted and land as "fail".  Human-approval suspension is Python-side
    // only (LangGraph interrupt() in services/dag/gate.py).  C++ wiring is explicitly
    // deferred.  Do not return
    // "pause" from a C++ Action until run_stage() handles it.
    std::string reason;
    bool has_error = false;  // Python error=None vs a string
    std::string error;
    bool done = false;
    std::string raw;
    std::string sent;  // exact system+user prompt sent for this call (capped)
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
    // True when this retry must not burn the stage attempt budget (e.g. tool-scope
    // escalation that re-prompts with a bigger cheatsheet on the same step).
    bool free_retry = false;
    // Provider reasoning (CompleteResult.thinking) — emitted as agent.thought.
    std::string thinking;
    std::string model;  // optional provider model id for the thought row
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

// Cooperative-cancel flag, checked at every stage boundary AND (via the process-wide
// run registry + thread-local HTTP cancel flag) inside in-flight provider HTTP calls.
// cancelled is an atomic shared_ptr so agent_cancel can flip it from another thread.
struct RunControl {
    std::shared_ptr<std::atomic<bool>> flag = std::make_shared<std::atomic<bool>>(false);
    std::function<bool()> cancelled = [f = flag] { return f && f->load(std::memory_order_acquire); };
    void request_cancel() {
        if (flag) flag->store(true, std::memory_order_release);
    }

    // Mid-run steering (agent_steer): any thread queues text; the run drains it at
    // stage/attempt boundaries and folds it into the request so the NEXT provider
    // call sees it. Never interrupts an in-flight HTTP call (same cooperative model
    // as cancel).
    struct SteerQueue {
        std::mutex mtx;
        std::vector<std::string> pending;
    };
    std::shared_ptr<SteerQueue> steers = std::make_shared<SteerQueue>();
    void queue_steer(const std::string& text) {
        if (!steers) return;
        std::lock_guard<std::mutex> lk(steers->mtx);
        steers->pending.push_back(text);
    }
    std::vector<std::string> drain_steers() {
        std::vector<std::string> out;
        if (!steers) return out;
        std::lock_guard<std::mutex> lk(steers->mtx);
        out.swap(steers->pending);
        return out;
    }
};

// Process-wide run-id → RunControl registry. Runs register on start, unregister on finish.
// Registering an id that is ALREADY live ADOPTS the existing control (control is
// overwritten with the registered one, sharing its flag) instead of replacing it —
// nested same-id runs (run_assembly_design's single-doc fallthrough tail-calls
// run_design with its own run_id) must never mint a fresh un-flipped flag, or a
// Stop that landed before the nested run registered is silently lost.
void register_run_control(const std::string& run_id, RunControl& control);
void unregister_run_control(const std::string& run_id);
// Returns {ok:true, cancelled:bool} — cancelled is true when a live run was flagged.
// Empty run_id is a no-op (cancelled:false); use cancel_all_runs to stop every live run.
json cancel_run(const std::string& run_id);
// Cancel every registered live run (Stop with unknown/missing run_id, client disconnect).
// Returns {ok:true, cancelled:bool, count:N}.
json cancel_all_runs();

// Queue a mid-run steering message for a live run. Returns {ok:true, steered:bool} —
// steered is false for unknown/finished run_id or empty text. The run consumes queued
// steers at its next stage/attempt boundary as `[User steering]` context.
json steer_run(const std::string& run_id, const std::string& text);

// Drive the state machine to completion. Returns {status, stop_reason}.
json run_machine(DesignState& state, const StageActions& actions, RunEvents& events,
                 const json& budgets = json::object(), RunControl control = RunControl());

}  // namespace agent
}  // namespace chudware
