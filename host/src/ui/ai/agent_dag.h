// Per-conversation agent execution DAG. All calls are on the GUI/drain thread.
// Event insertion joins parent_id to id; a new run resets only its conversation.
// Serialization includes the canvas camera so switching conversations preserves pan/zoom.

#pragma once
#include <string>
#include <nlohmann/json.hpp>
struct AppState;
namespace ui {
struct AgentDagSummary {
    std::string runId;
    int toolCount = 0, thoughtCount = 0, ctxCount = 0;
    bool empty = true;
};
// Called by the streamed JobState event drain after stripping the "orch_" prefix.
void feedAgentDagEvent(const std::string& convId, const std::string& kind,
                       const nlohmann::json& data);
// Returns null for an unseen conversation; restoration ignores invalid node records.
nlohmann::json agentDagToJson(const std::string& convId);
void agentDagFromJson(const std::string& convId, const nlohmann::json& j);
AgentDagSummary agentDagSummary(const std::string& convId);
void drawAgentDagCanvas(AppState& st, const std::string& convId, int verbosity,
                        void (*selectAgent)(AppState&, const std::string&, const std::string&));
}  // namespace ui
