// core/agent/events.h — C++ port of agent/orchestrator/events.py.
//
// P52: hierarchical run-event emission + persistence to
//   %APPDATA%/AI-CAD/cache/runs/<run_id>/events.jsonl
// Raw model outputs capped at 16 KB in the event (full text -> sidecar files).
// The live-emit callback is a host concern; here we persist + return the event.
#pragma once

#include <filesystem>
#include <string>

#include <nlohmann/json.hpp>

namespace aicad {
namespace agent {

using nlohmann::json;

constexpr int kRawCap = 16 * 1024;  // 16 KB.

// "" if falsy; text if len<=cap; else text[:cap] + truncation marker.
std::string cap_raw(const std::string& text);

// CACHE/"runs"/run_id (mkdir -p).
std::filesystem::path run_dir(const std::string& run_id);

// RunEvents: truncate events.jsonl on construct; emit() appends one JSON object
// per line; write_sidecar() writes the uncapped raw output.
class RunEvents {
public:
    explicit RunEvents(const std::string& run_id);
    // ev = {"kind":kind, "run_id":run_id, **data}; append json+"\n" to events.jsonl.
    json emit(const std::string& kind, const json& data);
    std::string write_sidecar(const std::string& stage, int attempt, const std::string& raw);
    const std::filesystem::path& dir() const { return dir_; }

private:
    std::string run_id_;
    std::filesystem::path dir_;
    std::filesystem::path jsonl_;
};

}  // namespace agent
}  // namespace aicad
