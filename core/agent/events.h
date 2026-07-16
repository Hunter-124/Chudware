// core/agent/events.h — C++ port of agent/orchestrator/events.py.
//
// P52: hierarchical run-event emission + persistence to
//   %APPDATA%/AI-CAD/cache/runs/<run_id>/events.jsonl
// Raw model outputs capped at 16 KB in the event (full text -> sidecar files).
// The live-emit callback is a host concern; here we persist + return the event.
#pragma once

#include <filesystem>
#include <functional>
#include <string>

#include <nlohmann/json.hpp>

namespace aicad {
namespace agent {

using nlohmann::json;

constexpr int kRawCap = 16 * 1024;  // 16 KB.
// Caps for agent.thought / agent.tool_call stream fields (copilot Thinking/Doing).
constexpr int kThoughtCap = 4000;
constexpr int kArgsPreviewCap = 2048;

// "" if falsy; text if len<=cap; else text[:cap] + truncation marker.
std::string cap_raw(const std::string& text);
// Truncate to n chars with a single "…" when longer (agent-level event fields).
std::string cap_text(const std::string& text, int n);
// Opaque 16-hex span id for agent-level events (contract §2.1).
std::string new_span_id();

// CACHE/"runs"/run_id (mkdir -p).
std::filesystem::path run_dir(const std::string& run_id);

// RunEvents: truncate events.jsonl on construct; emit() appends one JSON object
// per line; write_sidecar() writes the uncapped raw output.
class RunEvents {
public:
    using Callback = std::function<void(const json&)>;
    explicit RunEvents(const std::string& run_id);
    // run.py RunEvents(run_id, emit): a live callback invoked with each emitted
    // event (after persistence). May be empty (no-op), matching emit=None.
    RunEvents(const std::string& run_id, Callback cb);
    // ev = {"kind":kind, "run_id":run_id, **data}; append json+"\n" to events.jsonl,
    // then invoke the callback (if set) — mirrors events.py emit order.
    json emit(const std::string& kind, const json& data);
    std::string write_sidecar(const std::string& stage, int attempt, const std::string& raw);
    const std::filesystem::path& dir() const { return dir_; }

private:
    std::string run_id_;
    std::filesystem::path dir_;
    std::filesystem::path jsonl_;
    Callback cb_;
};

// Emit agent.thought when text is non-empty (no-op if empty). Additive stream field
// for model reasoning that CompleteResult already parses but used to drop.
json emit_agent_thought(RunEvents& ev,
                        const std::string& text,
                        const std::string& model = "",
                        const std::string& stage = "",
                        int attempt = 0,
                        const std::string& parent_id = "",
                        const std::string& actor = "");

// Synthetic agent.tool_call after a successful op apply (Doing row with args_preview).
// args may be an ops array or a single-op object; tool_name/target may be empty to derive.
json emit_agent_tool_call(RunEvents& ev,
                          const std::string& tool_name,
                          const std::string& target,
                          const json& args,
                          bool ok,
                          int latency_ms,
                          const std::string& parent_id = "",
                          const std::string& actor = "");

// ---------------------------------------------------------------------------
// Telemetry helpers — emit the standard ai_artifact / ai_artifact_action /
// doc_semantic_diff event kinds.  Each stamps "consent" from the caller so
// the exporter (T-H5) can filter.  These are pure wrappers over
// RunEvents::emit(); they do NOT upload anything.
//
// Note on consent levels:
//   "off"  — still emit locally (persist-first contract); exporter skips upload.
//   "meta" — caller is responsible for passing empty strings for input_ref /
//            output_ref (and edit_ops_ref where applicable) before calling.
//   "full" — all fields included as-is.
// ---------------------------------------------------------------------------

json emit_ai_artifact(RunEvents& ev,
                      const std::string& artifact_id,
                      const std::string& insertion_point,
                      const json& model_info,
                      const std::string& prompt_hash,
                      const std::string& input_ref,
                      const std::string& output_ref,
                      int base_revision,
                      const json& verifier_scores,
                      int64_t latency_ms,
                      const std::string& consent);

json emit_ai_artifact_action(RunEvents& ev,
                             const std::string& artifact_id,
                             const std::string& action,
                             int revision,
                             const std::string& edit_ops_ref,
                             int undo_within_ops,
                             int64_t dwell_ms,
                             const std::string& note,
                             const std::string& consent);

json emit_doc_semantic_diff(RunEvents& ev,
                            const std::string& artifact_id,
                            int from_revision,
                            int to_revision,
                            const std::string& diff_ref,
                            const std::string& consent);

}  // namespace agent
}  // namespace aicad
