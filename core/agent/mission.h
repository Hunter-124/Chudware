// core/agent/mission.h — top-level "mission" orchestrator.
//
// run_mission() sits ONE level above run_design / run_assembly_design. Where those
// build a single part (or one assembly) from a request, the mission layer decomposes a
// request into an ORDERED list of high-level capability tasks
//   {design | assembly | simulate_flow | simulate_fea | drawing | evaluate | report}
// and runs them in sequence, threading the working document forward (so a wind-tunnel
// task simulates the geometry an earlier design/assembly task produced). A final report
// task turns measured results into a plain-language verdict ("is this a good design?").
//
// This is what lets one chat message express a whole workflow — e.g. "model an airplane,
// then run CFD and prove it's a good design" — instead of the old one-capability-per-
// message routing. It is domain-general: "design a bracket and FEA it at 200 N" works the
// same way.
//
// The body lives in run.cpp so it can reuse that TU's file-local helpers (make_provider,
// agent_call, extract_json_obj, call_budget_for). The simulate_flow capability is supplied
// as a CALLBACK (SimFn) wired by the libaicad layer, so the agent library does not take a
// link dependency on the sim library.
#pragma once

#include <functional>
#include <string>

#include <nlohmann/json.hpp>

#include "agent/run.h"  // EmitFn

namespace aicad {
namespace agent {

using nlohmann::json;

// A flow-sim backend: (doc_id, natural-language request, params) -> the CFD result object
// {ok, answer, drag_n, lift_n, cd, frontal_area_mm2, reynolds, velocity_ms, aoa_deg} (or
// {ok:false, answer} on no-geometry / failure). The libaicad layer passes a lambda over
// run_flow_sim (the same path agent_simulate_v1 uses). May be empty: a simulate_flow task
// then resolves to status "skipped".
using SimFn = std::function<json(const std::string& doc_id, const std::string& request,
                                 const json& params)>;

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
}  // namespace aicad
