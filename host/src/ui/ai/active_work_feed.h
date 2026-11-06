#pragma once

#include <algorithm>
#include <chrono>
#include <deque>
#include <string>

#include <nlohmann/json.hpp>

namespace ui {

using ActiveWorkClock = std::chrono::steady_clock;

// A tool operation retained for the project activity ticker.
struct AwOp {
    std::string tool;
    std::string target;
    std::string conversationId;
    std::string runId;
    bool ok = true;
    int latencyMs = -1;
    long long seq = 0;
    ActiveWorkClock::time_point at{};
};

inline std::string awStr(const nlohmann::json& d, const char* k) {
    auto it = d.find(k);
    return (it != d.end() && it->is_string()) ? it->get<std::string>() : std::string();
}

struct AwStore {
    std::deque<AwOp> ops;
    long long seqCtr = 0;
    void feed(const std::string& kind, const nlohmann::json& data,
              const std::string& conversationId) {
        if (!data.is_object() || conversationId.empty()) return;
        if (kind != "run_started" && kind != "dag.run.started" &&
            kind != "agent.tool_call") return;
        const std::string runId = awStr(data, "run_id");
        if (runId.empty()) return;
        if (kind == "run_started" || kind == "dag.run.started") {
            // A new run replaces this conversation's previous ticker, never another's.
            ops.erase(std::remove_if(ops.begin(), ops.end(), [&](const AwOp& op) {
                return op.conversationId == conversationId && op.runId != runId;
            }), ops.end());
            return;
        }
        AwOp op;
        op.conversationId = conversationId;
        op.runId = runId;
        op.tool = awStr(data, "tool_name");
        if (op.tool.empty()) op.tool = "tool";
        op.target = awStr(data, "target");
        op.ok = data.value("ok", true);
        auto lat = data.find("latency_ms");
        op.latencyMs = (lat != data.end() && lat->is_number()) ? lat->get<int>() : -1;
        op.seq = seqCtr++;
        op.at = ActiveWorkClock::now();
        ops.push_back(std::move(op));
        constexpr size_t kMaxOps = 96;
        while (ops.size() > kMaxOps) ops.pop_front();
    }
};

}  // namespace ui
