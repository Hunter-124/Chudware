// core/agent/run.h — C++ port of agent/orchestrator/run.py (P53b).
//
// run_design() is the capstone of the agent port: it wires the deterministic
// orchestrator state machine (machine.{h,cpp}) + DesignState/context assembly
// (state.{h,cpp}) + the role prompts (roles) + the LLM provider layer
// (providers) + the LIVE document session (doc/session.h: doc_apply_ops,
// doc_dry_run_ops, doc_undo) into one end-to-end run against an OPEN doc.
//
// Per-stage StageActions each run a provider call over a FRESH bounded context,
// parse the reply (ops_emit / _extract_json_obj), validate via doc_dry_run_ops,
// and commit via doc_apply_ops. The MODEL stage carries the dry-run retry + the
// build123d script ESCAPE HATCH (after _ESCAPE_AFTER failures); REVIEW->FIX is a
// bounded re-review loop; the final MEMORY stage is repurposed as a lightweight
// PART-NAMING step (sets the doc name via doc_set_name + emits part_named; it no
// longer writes cache/runs/<run_id>/memory.json).
//
// REVIEW vision-rendering (_render_review in Python) is DISPLAY/host tooling
// (engine.render + pyvista + build123d export) and is DEFERRED to the host
// (P55): in C++ the review render_path is always "" (stats-only) and no image is
// ever attached. The parity gate neutralizes the Python side to match.
#pragma once

#include <filesystem>
#include <functional>
#include <string>
#include <system_error>

#include <nlohmann/json.hpp>

namespace chudware {
namespace agent {

using nlohmann::json;

// run.py emit callback: every run event (the machine's stage events + the run_*
// bookends) is delivered here. The libchudware wiring captures them into a vector.
using EmitFn = std::function<void(const json&)>;

// Synchronous notification for document calls performed inside an opaque agent RPC. The hostd
// registers this thread-locally before chudware_call, just like the live event sink, so direct
// doc_new/doc_apply_ops calls can enter its normal document registry at commit boundaries.
using DocPublishFn =
    std::function<void(const std::string& method, const json& params, const json& result)>;
void set_doc_publish_sink(DocPublishFn sink);

// Pure filename/display-title normalization used by the mission naming lane.
std::string sanitize_part_name(std::string name);

// Mission names are model-authored and therefore cross both a document-mint and
// filesystem boundary. This is the single boundary helper used by the assembly
// lane: normalize the model value first, then a deterministic caller-owned
// fallback, and never allow an empty title through to doc_new or a path.
inline std::string mission_mint_name(const std::string& model_name,
                                     const std::string& fallback) {
    std::string out = sanitize_part_name(model_name);
    if (out.empty()) out = sanitize_part_name(fallback);
    return out.empty() ? std::string("Part") : out;
}

inline bool mission_path_is_beneath(const std::filesystem::path& root,
                                    const std::filesystem::path& candidate) {
    std::error_code ec;
    const auto abs_root = std::filesystem::absolute(root, ec).lexically_normal();
    if (ec || abs_root.empty()) return false;
    const auto abs_candidate = std::filesystem::absolute(candidate, ec).lexically_normal();
    if (ec || abs_candidate.empty()) return false;
    const auto rel = abs_candidate.lexically_relative(abs_root);
    if (rel.empty() || rel.is_absolute()) return false;
    const auto first = rel.begin();
    return first != rel.end() && *first != "..";
}

inline std::filesystem::path mission_output_path(const std::filesystem::path& run_root,
                                                 const std::string& model_name,
                                                 const std::string& fallback = "Assembly") {
    std::error_code ec;
    const auto abs_root = std::filesystem::absolute(run_root, ec).lexically_normal();
    if (ec || abs_root.empty()) return {};
    auto candidate =
        (abs_root / (mission_mint_name(model_name, fallback) + ".step+")).lexically_normal();
    if (!mission_path_is_beneath(abs_root, candidate))
        candidate = (abs_root / (mission_mint_name("", fallback) + ".step+")).lexically_normal();
    return mission_path_is_beneath(abs_root, candidate) ? candidate : std::filesystem::path();
}

// Add workspace_dir to a worker RPC only when the exact directory is usable.
// Missing configuration and an invalid explicit path must remain distinguishable:
// both omit the key instead of forwarding a path Python will reject.
inline bool emit_existing_workspace_dir(json& params, const std::string& candidate) {
    if (candidate.empty()) return false;
    std::error_code ec;
    const auto absolute = std::filesystem::absolute(candidate, ec).lexically_normal();
    if (ec || !std::filesystem::is_directory(absolute, ec) || ec) return false;
    params["workspace_dir"] = absolute.string();
    return true;
}

struct AssemblyChildDecision {
    bool accept = false;
    bool cancelled = false;
    std::string failure_reason;
};

// A child is component-worthy only when its terminal result says it completed
// and the live document independently proves at least one body.
inline AssemblyChildDecision classify_assembly_child(const json& child_result,
                                                      bool geometry_proven) {
    if (!child_result.is_object())
        return {false, false, "child_result_invalid"};
    const std::string status = child_result.value("status", std::string());
    const std::string stop = child_result.value("stop_reason", std::string());
    if (status == "cancelled" || stop == "cancelled")
        return {false, true, "cancelled"};
    if (status == "done" && geometry_proven)
        return {true, false, {}};
    if (status == "done")
        return {false, false, "no_geometry"};
    if (!stop.empty())
        return {false, false, stop};
    if (!status.empty())
        return {false, false, status};
    return {false, false, "child_failed"};
}

struct AssemblyFinish {
    std::string status;
    std::string stop_reason;
};

inline AssemblyFinish classify_assembly_finish(int accepted_components, bool any_child_failed,
                                               const std::string& first_failure,
                                               bool cancelled = false) {
    if (cancelled) return {"cancelled", "cancelled"};
    if (accepted_components > 0 && any_child_failed)
        return {"partial", first_failure.empty() ? "child_failed" : first_failure};
    if (accepted_components > 0) return {"done", "assembled"};
    return {"failed", first_failure.empty() ? "no_parts_built" : first_failure};
}

// run_design — drive the orchestrator against a LIVE doc session.
//
// request       the natural-language design request.
// doc_id        an OPEN C++ doc session (doc_new/doc_open). Its .doc seeds the
//               initial tree; its .revision seeds state.revision. doc_apply_ops /
//               doc_dry_run_ops / doc_undo all route to this session.
// provider_cfg  the resolved provider descriptor:
//                 {name:"anthropic"|"openai"|"llamacpp", model, api_key, base_url,
//                  supports_vision, prewalk_provider?: <implementation-stage descriptor>}
//               OR, for the parity gate, the role-scripted mock:
//                 {name:"mock", replies:{<role>:[reply,...], ...}}
// emit          optional event callback (may be empty).
// options       {run_id?, budgets?, prompts_dir?, offline?,
//                capability?, cam?,
//                // Copilot mode overhaul (agent|plan|swarm|edit|ask|eng_firm):
//                mode?,            // "agent"|"plan"|"swarm"|"edit"|"ask"|"eng_firm"
//                plan_only?,       // bool: intake+plan then STOP (status plan_ready)
//                approved_plan?,   // string|object: skip re-plan; seed DesignState.plan
//                execute_as?,      // "agent"|"swarm"|"eng_firm" (host resume after plan approve)
//                corporate_force?, // bool: force swarm/corporate
//                scope?,           // {doc_id?, feature_ids?:[], selection?:[], section?}
//                advisor?,         // bool, default true — advisory inject + plan review
//                advisory_text?,   // optional pre-seeded advisory block
//                execution_style?, // "edit"|"swarm"|"eng_firm" (overrides recommend_structure)
//                run_depth?,       // nested sub-run depth (delegate / swarm unit)
//                is_sub_run?,      // true inside nested units (disables further delegate)
//                firm_role_seed?   // specialist prompt prefix for eng_firm sub-runs
//               }. run_id overrides the generated uuid hex[:12]. prompts_dir overrides
//               CHUDWARE_PROMPTS_DIR only in developer builds; packaged builds use compiled assets.
//
// Returns {run_id, status, stop_reason, revision, doc_id, execution_style?} — or on plan_only
// success {status:"plan_ready", plan, stop_reason:"plan_only"|"awaiting_plan_approval", ...}.
// Advisor escalate: {status:"plan_ready", stop_reason:"needs_user_decision", plan, reason}.
json run_design(const std::string& request, const std::string& doc_id, const json& provider_cfg,
                EmitFn emit = nullptr, const json& options = json::object());

// classify_worker_stop — the honest terminal classification when the Python
// general-agent worker does not return a usable turn.
//
// "non_modeling_request" is reserved for the offline pre-flight that never reached
// the worker. A worker that DIED, went SILENT, or rejected its request is Chudware
// failing, not the user asking for something unrunnable: run-42508173312214 built
// five real, auto-persisted features before its worker stopped responding. That is
// not a request-classification failure.
//
// reached_worker  false only for the offline pre-flight refusal.
struct WorkerStop {
    std::string stop_reason;
    std::string status;
    bool may_have_changed_the_model;
};
WorkerStop classify_worker_stop(bool crashed, bool timed_out, bool reached_worker);

// general_lane_status / general_lane_error_detail — how a general-lane turn's own
// account of itself becomes the host-facing status + error_detail. Extracted for the
// same reason as classify_worker_stop above: the mapping is pure, and it was wrong.
//
// The general lane runs no geometry pipeline, so the handoff envelope is the ONLY
// record of what happened. Only `partial` and the guardrail stop used to be mapped;
// every other non-success became "failed", so a model that DECIDED not to do the work
// and explained why through the harness's own `blocked`/`escalated` handoff reached the
// GUI indistinguishable from a provider fault, and `error_detail` came through null so
// events.jsonl kept no statement of the reason either (live 2026-08-27: a machine-shop
// tooling request returned status=failed carrying a paragraph of reasoning and three
// offered alternatives, none of which anything rendered).
//
// error_detail is the contract's ~300-char human sentence (contracts/agent_events.md),
// so a long summary is clipped rather than dropped; an error_detail the turn supplied
// itself always wins.
std::string general_lane_status(bool turn_ok, const std::string& handoff_status,
                                const std::string& stop_reason);
std::string general_lane_error_detail(const std::string& turn_error_detail,
                                      const std::string& status,
                                      const std::string& summary);

// run_assembly_design — multi-part TRUE assembly. Plans the request; if the plan
// is a single part (or assembly:false) it delegates to run_design on `doc_id`.
// Otherwise it builds each part in its own child doc, but accepts a component
// only after status=done and an independent live-body proof. It saves accepted
// parts under run_dir, creates an assembly doc, and inserts each part at the
// planner's transform. Mixed accepted and failed children return status=partial
// with the first child failure preserved. A created assembly returns
// {created_doc:true, assembly:true, doc_id:<asm>} so the host can open it.
json run_assembly_design(const std::string& request, const std::string& doc_id,
                         const json& provider_cfg, EmitFn emit = nullptr,
                         const json& options = json::object());

}  // namespace agent
}  // namespace chudware
