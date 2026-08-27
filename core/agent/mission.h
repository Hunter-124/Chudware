// core/agent/mission.h — top-level "mission" orchestrator.
//
// run_mission() sits ONE level above run_design / run_assembly_design. Where those
// build a single part (or one assembly) from a request, the mission layer decomposes a
// request into an ORDERED list of high-level capability tasks. The planner
// vocabulary remains
//   {design | assembly | simulate_flow | simulate_fea | drawing | evaluate | report}
// for compatibility with stored/approved plans, but only capabilities with a
// native executor may cross the execution boundary. Plans containing
// simulate_fea, drawing, or evaluate currently terminate before execution with
// stop_reason="capability_unavailable"; they are never counted as skipped work.
// Executable tasks thread the working document forward (so a wind-tunnel task
// simulates the geometry an earlier design/assembly task produced). A final
// report task turns measured results into a plain-language verdict.
//
// The body lives in run.cpp so it can reuse that TU's file-local helpers (make_provider,
// agent_call, extract_json_obj, call_budget_for). The simulate_flow capability is supplied
// as a CALLBACK (SimFn) wired by the libchudware layer, so the agent library does not take a
// link dependency on the sim library.
#pragma once

#include <functional>
#include <string>
#include <string_view>

#include <nlohmann/json.hpp>

#include "agent/run.h"  // EmitFn

namespace chudware {
namespace agent {

using nlohmann::json;

// A flow-sim backend: (doc_id, natural-language request, params) -> the CFD result object
// {ok, answer, drag_n, lift_n, cd, frontal_area_mm2, reynolds, velocity_ms, aoa_deg} (or
// {ok:false, answer} on no-geometry / failure). The libchudware layer passes a lambda over
// run_flow_sim (the same path agent_simulate_v1 uses). May be empty: a simulate_flow task
// then resolves to status "skipped".
using SimFn = std::function<json(const std::string& doc_id, const std::string& request,
                                 const json& params)>;

// Keep the accepted planner vocabulary separate from executable truth. Stored
// plans may still contain the unavailable values; run_mission uses this helper
// at plan acceptance, before emitting mission_started or spending task budget.
inline bool mission_capability_has_executor(std::string_view capability) {
    return capability == "design" || capability == "assembly" ||
           capability == "simulate_flow" || capability == "report" ||
           capability == "reverse_engineer";
}

struct MissionCapabilityDecision {
    bool executable;
    std::string stop_reason;
};

inline MissionCapabilityDecision classify_mission_capability(std::string_view capability) {
    if (mission_capability_has_executor(capability)) return {true, {}};
    return {false, "capability_unavailable"};
}

// run_mission — decompose `request` into an ordered task list (one mission_planner LLM
// call), execute each task threading the working doc forward, and synthesize a verdict.
//
// request       the natural-language mission (may span design + analysis + report).
// doc_id        the OPEN doc the mission starts from (the "active" target). Design tasks
//               mutate it in place; an assembly task creates a NEW doc and the executor
//               threads that forward to subsequent tasks.
// provider_cfg  resolved provider descriptor (same shape run_design takes).
// emit          optional event callback — receives mission_* events plus every sub-run's
//               existing stage events.
// sim           optional flow-sim backend (see SimFn).
// options       {run_id?, prompts_dir?, budgets?} — forwarded to sub-runs.
//
// Returns {run_id, status, stop_reason, doc_id, revision, created_doc?, asm_path?,
// components?, verdict, tasks:[{capability,title,status,doc_id,summary}],
// tasks_completed?, tasks_failed?, tasks_abandoned?, first_fail_reason?}.
// status: "done" (all ok) | "partial" (some tasks done + some failed; geometry kept;
// stop_reason = first unit failure) | "failed" | "plan_ready".
json run_mission(const std::string& request, const std::string& doc_id, const json& provider_cfg,
                 EmitFn emit = nullptr, SimFn sim = nullptr, const json& options = json::object());

}  // namespace agent
}  // namespace chudware
