// Pure ACP frame-to-maintained-chat-state mapping.
//
// This module has no transport, controller, ImGui draw, or GL dependencies. It
// mutates the existing Conversation / ChatMsg / ActivityItem / PlanState model
// directly; State is only ephemeral message-id/request correlation bookkeeping.
#pragma once

#include <cstddef>
#include <limits>
#include <string>
#include <unordered_map>
#include <vector>

#include <nlohmann/json.hpp>

struct ChatMsg;
struct Conversation;

namespace ui::code::mapping {

inline constexpr size_t kNoMessage = std::numeric_limits<size_t>::max();

struct State {
    std::unordered_map<std::string, size_t> messageRows;
    size_t activeAssistant = kNoMessage;
    size_t pendingAssistant = kNoMessage;
    bool replaying = false;
};

struct ServerRequestDecision {
    bool error = false;
    int errorCode = 0;
    std::string errorMessage;
    nlohmann::json result = nlohmann::json::object();
};

void reset(State& state);
void setHistoryReplay(State& state, bool replaying);
void bindPendingAssistant(State& state, size_t index);

void applyUpdate(Conversation& conversation, std::vector<ChatMsg>& messages,
                 State& state, const nlohmann::json& update,
                 int* activeContextTokens = nullptr,
                 int* activeContextWindow = nullptr);
void applyPromptResponse(Conversation& conversation, std::vector<ChatMsg>& messages,
                         State& state, const nlohmann::json& response,
                         bool cancelled);
void markPromptFailure(std::vector<ChatMsg>& messages, State& state,
                       size_t preferred, const std::string& error);
ServerRequestDecision applyServerRequest(Conversation& conversation,
                                         std::vector<ChatMsg>& messages,
                                         State& state,
                                         const std::string& method,
                                         const nlohmann::json& params);

} // namespace ui::code::mapping

// Process-static convenience seam for the standalone mapper test. Production
// uses explicit mapping::State instances per ACP session.
namespace ui::code::testing {
void resetMapping();
void setHistoryReplay(bool replaying);
void applyUpdate(Conversation& conversation, std::vector<ChatMsg>& messages,
                 const nlohmann::json& update);
void applyPromptResponse(Conversation& conversation, std::vector<ChatMsg>& messages,
                         const nlohmann::json& response, bool cancelled);
nlohmann::json applyServerRequest(Conversation& conversation,
                                  std::vector<ChatMsg>& messages,
                                  const std::string& method,
                                  const nlohmann::json& params,
                                  bool& isError, int& errorCode,
                                  std::string& errorMessage);
} // namespace ui::code::testing
