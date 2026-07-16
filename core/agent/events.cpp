// core/agent/events.cpp — see events.h. Mirrors events.py.
#include "agent/events.h"

#include <atomic>
#include <cstdint>
#include <chrono>
#include <cstdio>
#include <fstream>

#include "agent/paths.h"
#include "agent/roles.h"  // prompt_hash → sha256 hex prefix for digests

namespace fs = std::filesystem;

namespace aicad {
namespace agent {

std::string cap_raw(const std::string& text) {
    if (text.empty()) return "";
    if (static_cast<int>(text.size()) <= kRawCap) return text;
    return text.substr(0, kRawCap) + "\n...[raw truncated; full output in sidecar]";
}

std::string cap_text(const std::string& text, int n) {
    if (n <= 0 || text.empty()) return "";
    if (static_cast<int>(text.size()) <= n) return text;
    if (n == 1) return "…";
    return text.substr(0, static_cast<size_t>(n - 1)) + "…";
}

std::string new_span_id() {
    static std::atomic<uint64_t> seq{0};
    const uint64_t n = seq.fetch_add(1, std::memory_order_relaxed);
    const uint64_t t = static_cast<uint64_t>(
        std::chrono::steady_clock::now().time_since_epoch().count());
    char buf[20];
    // 16 hex chars (opaque span id, mirrors Python secrets.token_hex(8)).
    std::snprintf(buf, sizeof(buf), "%08x%08x",
                  static_cast<unsigned>(t & 0xffffffffu),
                  static_cast<unsigned>((n ^ (t >> 32)) & 0xffffffffu));
    return std::string(buf);
}

namespace {

// First 16 hex chars of sha256 (via prompt_hash's 12 + second pass).
std::string digest16(const std::string& text) {
    std::string a = prompt_hash(text);
    std::string b = prompt_hash(a + ":" + text);
    std::string h = a + b;
    if (h.size() > 16) h.resize(16);
    return h;
}

std::string derive_ops_target(const json& args) {
    auto one = [](const json& op) -> std::string {
        if (!op.is_object()) return "";
        for (const char* k : {"target", "face", "faces", "edge", "edges", "feature",
                              "sketch", "part", "part_name", "name", "body", "component",
                              "plane", "doc_id", "path", "id"}) {
            if (!op.contains(k)) continue;
            const auto& v = op[k];
            if (v.is_string() && !v.get_ref<const std::string&>().empty())
                return cap_text(v.get<std::string>(), 120);
            if (v.is_number_integer()) return std::to_string(v.get<long long>());
            if (v.is_number_unsigned()) return std::to_string(v.get<unsigned long long>());
            if (v.is_number_float()) return std::to_string(v.get<double>());
            if (v.is_array() && !v.empty()) {
                std::string joined;
                int n = 0;
                for (const auto& el : v) {
                    if (n >= 4) break;
                    if (!joined.empty()) joined += ", ";
                    if (el.is_string()) joined += el.get<std::string>();
                    else if (el.is_number_integer()) joined += std::to_string(el.get<long long>());
                    else if (el.is_number()) joined += el.dump();
                    else continue;
                    ++n;
                }
                if (!joined.empty()) return cap_text(joined, 120);
            }
        }
        // type/op as last-resort label fragment
        if (op.contains("type") && op["type"].is_string())
            return cap_text(op["type"].get<std::string>(), 120);
        if (op.contains("op") && op["op"].is_string())
            return cap_text(op["op"].get<std::string>(), 120);
        return "";
    };
    if (args.is_array()) {
        if (args.empty()) return "";
        if (args.size() == 1) return one(args[0]);
        // Multi-op: first op type + count
        std::string t = one(args[0]);
        if (t.empty() && args[0].is_object()) {
            if (args[0].contains("op") && args[0]["op"].is_string())
                t = args[0]["op"].get<std::string>();
            else if (args[0].contains("type") && args[0]["type"].is_string())
                t = args[0]["type"].get<std::string>();
        }
        std::string s = t.empty() ? (std::to_string(args.size()) + " ops")
                                  : (t + " +" + std::to_string(args.size() - 1) + " more");
        return cap_text(s, 120);
    }
    if (args.is_object()) return one(args);
    return "";
}

std::string derive_tool_name(const json& args) {
    auto op_name = [](const json& op) -> std::string {
        if (!op.is_object()) return "";
        if (op.contains("op") && op["op"].is_string()) return op["op"].get<std::string>();
        if (op.contains("type") && op["type"].is_string()) return op["type"].get<std::string>();
        return "";
    };
    if (args.is_array()) {
        if (args.empty()) return "doc_apply_ops";
        std::string first = op_name(args[0]);
        if (first.empty()) return "doc_apply_ops";
        for (size_t i = 1; i < args.size(); ++i) {
            if (op_name(args[i]) != first) return "doc_apply_ops";
        }
        return first;
    }
    if (args.is_object()) {
        std::string n = op_name(args);
        return n.empty() ? "doc_apply_ops" : n;
    }
    return "doc_apply_ops";
}

}  // namespace

json emit_agent_thought(RunEvents& ev,
                        const std::string& text,
                        const std::string& model,
                        const std::string& stage,
                        int attempt,
                        const std::string& parent_id,
                        const std::string& actor) {
    if (text.empty()) return json(nullptr);
    json data = json::object();
    data["id"] = new_span_id();
    data["text"] = cap_text(text, kThoughtCap);
    data["node_id"] = stage.empty() ? "run" : stage;
    if (!parent_id.empty()) data["parent_id"] = parent_id;
    if (!actor.empty()) data["actor"] = actor;
    if (!model.empty()) data["model"] = model;
    if (!stage.empty()) data["stage"] = stage;
    if (attempt > 0) data["attempt"] = attempt;
    return ev.emit("agent.thought", data);
}

json emit_agent_tool_call(RunEvents& ev,
                          const std::string& tool_name,
                          const std::string& target,
                          const json& args,
                          bool ok,
                          int latency_ms,
                          const std::string& parent_id,
                          const std::string& actor) {
    std::string name = tool_name.empty() ? derive_tool_name(args) : tool_name;
    std::string tgt = target.empty() ? derive_ops_target(args) : target;
    std::string preview;
    try {
        preview = args.is_null() ? std::string() : args.dump();
    } catch (...) {
        preview = "";
    }
    preview = cap_text(preview, kArgsPreviewCap);
    json data = json::object();
    data["id"] = new_span_id();
    data["node_id"] = "apply";
    data["tool_name"] = name;
    data["target"] = tgt;
    data["args_digest"] = digest16(preview);
    data["result_digest"] = digest16(ok ? "ok" : "err");
    data["ok"] = ok;
    data["latency_ms"] = latency_ms;
    if (!preview.empty()) data["args_preview"] = preview;
    if (!parent_id.empty()) data["parent_id"] = parent_id;
    if (!actor.empty()) data["actor"] = actor;
    return ev.emit("agent.tool_call", data);
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
