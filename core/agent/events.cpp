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

}  // namespace agent
}  // namespace aicad
