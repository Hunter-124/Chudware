// core/agent/events.cpp — see events.h. Mirrors events.py.
#include "agent/events.h"

#include <fstream>

#include "agent/paths.h"

namespace fs = std::filesystem;

namespace aicad {
namespace agent {

std::string cap_raw(const std::string& text) {
    if (text.empty()) return "";
    if (static_cast<int>(text.size()) <= kRawCap) return text;
    return text.substr(0, kRawCap) + "\n...[raw truncated; full output in sidecar]";
}

fs::path run_dir(const std::string& run_id) {
    fs::path d = paths_cache() / "runs" / run_id;
    std::error_code ec;
    fs::create_directories(d, ec);  // best-effort, like Python mkdir(exist_ok=True).
    return d;
}

RunEvents::RunEvents(const std::string& run_id) : run_id_(run_id) {
    dir_ = run_dir(run_id);
    jsonl_ = dir_ / "events.jsonl";
    // Truncate any stale file for this run id.
    std::ofstream fh(jsonl_, std::ios::binary | std::ios::trunc);
    // (best-effort; ignore failure to match Python's try/except pass)
}

RunEvents::RunEvents(const std::string& run_id, Callback cb)
    : run_id_(run_id), cb_(std::move(cb)) {
    dir_ = run_dir(run_id);
    jsonl_ = dir_ / "events.jsonl";
    std::ofstream fh(jsonl_, std::ios::binary | std::ios::trunc);
}

json RunEvents::emit(const std::string& kind, const json& data) {
    // ev = {"kind":kind, "run_id":run_id, **data}. Python dict preserves
    // insertion order; the gate compares PARSED objects so key order is moot.
    json ev = json::object();
    ev["kind"] = kind;
    ev["run_id"] = run_id_;
    if (data.is_object())
        for (auto it = data.begin(); it != data.end(); ++it) ev[it.key()] = it.value();
    // Persist first (source of truth for replay), then surface live (best-effort).
    std::ofstream fh(jsonl_, std::ios::binary | std::ios::app);
    if (fh) fh << ev.dump() << "\n";
    if (cb_) {
        try {
            cb_(ev);
        } catch (...) {
            // match Python try/except pass — a sink failure never breaks the run.
        }
    }
    return ev;
}

std::string RunEvents::write_sidecar(const std::string& stage, int attempt,
                                     const std::string& raw) {
    fs::path p = dir_ / ("attempt_" + stage + "_" + std::to_string(attempt) + ".txt");
    std::ofstream fh(p, std::ios::binary | std::ios::trunc);
    if (fh) fh << raw;
    return p.string();
}

// ---------------------------------------------------------------------------
// Telemetry helper implementations.
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
                      const std::string& consent) {
    // For "meta" consent the caller is responsible for passing empty strings
    // for input_ref and output_ref so that path references are not persisted.
    json data;
    data["artifact_id"]     = artifact_id;
    data["insertion_point"] = insertion_point;
    data["model"]           = model_info;
    data["prompt_hash"]     = prompt_hash;
    data["input_ref"]       = input_ref;
    data["output_ref"]      = output_ref;
    data["base_revision"]   = base_revision;
    data["verifier_scores"] = verifier_scores;
    data["latency_ms"]      = latency_ms;
    data["consent"]         = consent;
    return ev.emit("ai_artifact", data);
}

json emit_ai_artifact_action(RunEvents& ev,
                             const std::string& artifact_id,
                             const std::string& action,
                             int revision,
                             const std::string& edit_ops_ref,
                             int undo_within_ops,
                             int64_t dwell_ms,
                             const std::string& note,
                             const std::string& consent) {
    // For "meta" consent the caller is responsible for passing an empty string
    // for edit_ops_ref so that path references are not persisted.
    json data;
    data["artifact_id"]    = artifact_id;
    data["action"]         = action;
    data["revision"]       = revision;
    data["edit_ops_ref"]   = edit_ops_ref;   // optional; pass "" when absent
    data["undo_within_ops"] = undo_within_ops;
    data["dwell_ms"]       = dwell_ms;
    data["note"]           = note;           // optional; pass "" when absent
    data["consent"]        = consent;
    return ev.emit("ai_artifact_action", data);
}

json emit_doc_semantic_diff(RunEvents& ev,
                            const std::string& artifact_id,
                            int from_revision,
                            int to_revision,
                            const std::string& diff_ref,
                            const std::string& consent) {
    json data;
    data["artifact_id"]   = artifact_id;
    data["from_revision"] = from_revision;
    data["to_revision"]   = to_revision;
    data["diff_ref"]      = diff_ref;
    data["consent"]       = consent;
    return ev.emit("doc_semantic_diff", data);
}

}  // namespace agent
}  // namespace aicad
