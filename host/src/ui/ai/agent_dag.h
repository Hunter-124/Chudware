// Per-conversation agent execution trace: the tree of the main agent and every sub-agent, plus a
// bounded, labeled step timeline per agent (thinking, tool calls, prose, steers, compaction,
// retries, run boundaries). Built from the drained orch_* events; a conversation with no
// recorded events falls back to its persisted activity rows. GUI/drain thread only.

#pragma once
#include <string>
#include <vector>
#include <nlohmann/json.hpp>
struct AppState;
namespace ui {
struct AgentDagSummary {
    std::string runId;
    int toolCount = 0, thoughtCount = 0, ctxCount = 0;
    bool empty = true;
};
// Called by the streamed JobState event drain after stripping the "orch_" prefix. Attribution:
// `subagent_id` names the owning sub-agent; events without it belong to the main agent.
void feedAgentDagEvent(const std::string& convId, const std::string& kind,
                       const nlohmann::json& data);
// Returns null for an unseen conversation; restoration ignores records of an older schema.
nlohmann::json agentDagToJson(const std::string& convId);
void agentDagFromJson(const std::string& convId, const nlohmann::json& j);
AgentDagSummary agentDagSummary(const std::string& convId);

// One timeline step, copied out for the docked inspector.
struct AgentTraceStep {
    unsigned long long uid = 0;   // unique within the conversation's trace; 0 = none
    unsigned long long rev = 0;   // bumps whenever the step changes (live tool completion)
    std::string convId;
    std::string agent;            // owning agent's display name
    std::string kind;             // "Tool call", "Thinking", "Reply", "Steer", ...
    std::string title, target, detail, error, status;
    int durationMs = -1;
    double ts = 0.0;              // epoch seconds
};
using AgentTraceSelectAgentFn = void (*)(AppState&, const std::string& agentId,
                                         const std::string& role);
using AgentTraceSelectStepFn = void (*)(AppState&, const AgentTraceStep&);

// Agent tree on top, the selected agent's step timeline below (fills the current region).
// Clicking a sub-agent node calls `onAgent`; clicking a step calls `onStep`.
void drawAgentTrace(AppState& st, const std::string& convId, AgentTraceSelectAgentFn onAgent,
                    AgentTraceSelectStepFn onStep, unsigned long long selectedStepUid);
// Re-copy `io` (matched by io.convId + io.uid) when the step changed since it was copied.
// False once the step no longer exists (dropped from the bounded timeline or run reset).
bool agentTraceRefreshStep(AppState& st, AgentTraceStep& io);

// One sub-agent of a conversation's trace (the main agent is never listed), copied out for the
// Current Run roster. Same source as the trace tree: live events, else the saved transcript.
struct AgentTraceAgent {
    std::string id, parent, name, brief, model, note;
    std::string status;           // running | done | failed | cancelled | stopped | ...
    double startTs = 0.0, endTs = 0.0;   // epoch seconds; endTs 0 while running
    int toolCount = 0, failCount = 0;
    std::string lastStep;         // "Tool call · add_features" — the newest step, one line
    double lastStepTs = 0.0;
};
// Tree order (parents before children). Empty when the conversation has no trace.
void agentTraceSubagents(AppState& st, const std::string& convId,
                         std::vector<AgentTraceAgent>& out);
// Show `agentId`'s steps in the trace tree/timeline. False when the trace has no such agent.
bool agentTraceSelectAgent(AppState& st, const std::string& convId, const std::string& agentId);
}  // namespace ui
