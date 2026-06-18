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
// bounded re-review loop; MEMORY persists to cache/runs/<run_id>/memory.json.
//
// REVIEW vision-rendering (_render_review in Python) is DISPLAY/host tooling
// (engine.render + pyvista + build123d export) and is DEFERRED to the host
// (P55): in C++ the review render_path is always "" (stats-only) and no image is
// ever attached. The parity gate neutralizes the Python side to match.
#pragma once

#include <functional>
#include <string>

#include <nlohmann/json.hpp>

namespace aicad {
namespace agent {

using nlohmann::json;

// run.py emit callback: every run event (the machine's stage events + the run_*
// bookends) is delivered here. The libaicad wiring captures them into a vector.
using EmitFn = std::function<void(const json&)>;

// run_design — drive the orchestrator against a LIVE doc session.
//
// request       the natural-language design request.
// doc_id        an OPEN C++ doc session (doc_new/doc_open). Its .doc seeds the
//               initial tree; its .revision seeds state.revision. doc_apply_ops /
//               doc_dry_run_ops / doc_undo all route to this session.
// provider_cfg  the resolved provider descriptor:
//                 {name:"anthropic"|"openai"|"llamacpp", model, api_key, base_url,
//                  supports_vision}
//               OR, for the parity gate, the role-scripted mock:
//                 {name:"mock", replies:{<role>:[reply,...], ...}}
// emit          optional event callback (may be empty).
// options       {run_id?, budgets?, prompts_dir?}. run_id overrides the generated
//               uuid hex[:12]; prompts_dir overrides AI_CAD_PROMPTS_DIR (the gate
//               points both engines at the same agent/prompts).
//
// Returns {run_id, status, stop_reason, revision, doc_id}.
json run_design(const std::string& request, const std::string& doc_id, const json& provider_cfg,
                EmitFn emit = nullptr, const json& options = json::object());

}  // namespace agent
}  // namespace aicad
