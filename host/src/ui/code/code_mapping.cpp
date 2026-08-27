#include "ui/code/code_mapping.h"

#include "core/app_state.h"

#include <algorithm>
#include <iomanip>
#include <sstream>
#include <utility>

namespace ui::code::mapping {
namespace {

std::string stringField(const nlohmann::json& value, const char* key) {
    const auto it = value.find(key);
    return it != value.end() && it->is_string() ? it->get<std::string>() : std::string();
}

bool visibleText(const std::string& text) {
    for (const unsigned char byte : text)
        if (byte > 0x20) return true;
    return false;
}

std::string blockText(const nlohmann::json& block) {
    if (!block.is_object() || block.value("type", std::string()) != "text") return {};
    return stringField(block, "text");
}

std::string toolContentSummary(const nlohmann::json& content) {
    if (!content.is_array()) return {};
    std::string result;
    for (const auto& item : content) {
        if (!item.is_object()) continue;
        const std::string type = stringField(item, "type");
        std::string text;
        if (type == "content" && item.contains("content")) {
            text = blockText(item["content"]);
        } else if (type == "diff") {
            const std::string path = stringField(item, "path");
            const std::string oldText = stringField(item, "oldText");
            const std::string newText = stringField(item, "newText");
            text = path.empty() ? std::string("diff") : path;
            if (!oldText.empty() || !newText.empty())
                text += "\n--- before\n" + oldText + "\n+++ after\n" + newText;
        } else if (type == "terminal") {
            const std::string id = stringField(item, "terminalId");
            text = id.empty() ? std::string("terminal") : "terminal " + id;
        }
        if (text.empty()) continue;
        if (!result.empty()) result += '\n';
        result += text;
    }
    return result;
}

void finalizeStreams(ChatMsg& message) {
    for (auto& row : message.activity) {
        if (row.rowKind != 2 || !row.streaming) continue;
        row.streaming = false;
        row.status = "done";
        if (row.stage == "thought:stream" && !row.userToggled) row.expanded = false;
    }
}

ChatMsg& appendAssistant(std::vector<ChatMsg>& messages, State& state) {
    ChatMsg message;
    message.fromUser = false;
    message.running = !state.replaying && state.pendingAssistant != kNoMessage;
    messages.push_back(std::move(message));
    state.activeAssistant = messages.size() - 1;
    return messages.back();
}

ChatMsg& assistantForUpdate(std::vector<ChatMsg>& messages, State& state,
                            const std::string& messageId) {
    if (!messageId.empty()) {
        const std::string key = "a:" + messageId;
        const auto found = state.messageRows.find(key);
        if (found != state.messageRows.end() && found->second < messages.size()) {
            state.activeAssistant = found->second;
            return messages[found->second];
        }
        if (state.pendingAssistant < messages.size()) {
            const size_t index = state.pendingAssistant;
            state.activeAssistant = index;
            state.messageRows.emplace(key, index);
            return messages[index];
        }
        ChatMsg& message = appendAssistant(messages, state);
        state.messageRows.emplace(key, messages.size() - 1);
        return message;
    }
    if (state.activeAssistant < messages.size() && !messages[state.activeAssistant].fromUser)
        return messages[state.activeAssistant];
    if (state.pendingAssistant < messages.size()) {
        state.activeAssistant = state.pendingAssistant;
        return messages[state.activeAssistant];
    }
    return appendAssistant(messages, state);
}

void appendStreamChunk(ChatMsg& message, bool thinking, const std::string& text) {
    const std::string stage = thinking ? "thought:stream" : "stream:out";
    ActivityItem* row = nullptr;
    if (!message.activity.empty()) {
        ActivityItem& tail = message.activity.back();
        if (tail.rowKind == 2 && tail.stage == stage && tail.streaming) row = &tail;
    }
    if (!row && !visibleText(text)) return;
    if (!row) {
        finalizeStreams(message);
        ActivityItem item;
        item.rowKind = 2;
        item.stage = stage;
        item.title = thinking ? "Thinking" : "Output";
        item.expanded = thinking;
        item.streaming = true;
        item.status = "running";
        message.activity.push_back(std::move(item));
        row = &message.activity.back();
    }
    if (thinking) row->detail += text;
    else {
        row->detail += text;
        message.text += text;
    }
    row->streaming = true;
    row->status = "running";
    message.uiCachedH = -1.0f;
}

void appendUnsupportedContent(ChatMsg& message, const nlohmann::json& content) {
    finalizeStreams(message);
    ActivityItem row;
    row.rowKind = 1;
    row.stage = "acp:unsupported-content:" + std::to_string(message.activity.size());
    row.title = "Unsupported ACP content";
    row.reason = stringField(content, "type");
    row.detail = "This ACP content type has no maintained CAD transcript renderer yet.";
    row.status = "fail";
    message.activity.push_back(std::move(row));
}

void applyAssistantContent(ChatMsg& message, const nlohmann::json& content, bool thinking) {
    const std::string text = blockText(content);
    if (!text.empty()) {
        appendStreamChunk(message, thinking, text);
        return;
    }
    if (content.is_object() && content.value("type", std::string()) == "image") {
        finalizeStreams(message);
        ActivityItem row;
        row.rowKind = 1;
        row.stage = "acp:image:" + std::to_string(message.activity.size());
        row.title = "Assistant image";
        row.reason = stringField(content, "mimeType");
        row.detail = "The image arrived over ACP, but ChatMsg has no inline image payload field.";
        row.status = "done";
        message.activity.push_back(std::move(row));
        return;
    }
    appendUnsupportedContent(message, content);
}

void applyUserContent(std::vector<ChatMsg>& messages, State& state,
                      const std::string& messageId, const nlohmann::json& content) {
    const std::string key = "u:" + messageId;
    size_t index = kNoMessage;
    if (!messageId.empty()) {
        const auto found = state.messageRows.find(key);
        if (found != state.messageRows.end() && found->second < messages.size()) index = found->second;
    }
    if (index == kNoMessage) {
        ChatMsg message;
        message.fromUser = true;
        message.running = false;
        messages.push_back(std::move(message));
        index = messages.size() - 1;
        if (!messageId.empty()) state.messageRows.emplace(key, index);
    }
    ChatMsg& message = messages[index];
    const std::string text = blockText(content);
    if (!text.empty()) {
        message.text += text;
    } else if (content.is_object() && content.value("type", std::string()) == "image") {
        const std::string mime = stringField(content, "mimeType");
        message.text += "[attached image: ACP " + (mime.empty() ? std::string("image") : mime) + "]";
    } else {
        const std::string type = stringField(content, "type");
        message.text += "[unsupported ACP content" + (type.empty() ? std::string() : ": " + type) + "]";
    }
    state.activeAssistant = kNoMessage;
}

ActivityItem* findToolRow(ChatMsg& message, const std::string& id) {
    const std::string stage = "tool:" + id;
    for (auto& row : message.activity)
        if (row.rowKind == 3 && row.stage == stage) return &row;
    return nullptr;
}

void applyToolUpdate(ChatMsg& message, const nlohmann::json& update) {
    finalizeStreams(message);
    const std::string id = stringField(update, "toolCallId");
    ActivityItem* row = findToolRow(message, id);
    if (!row) {
        ActivityItem item;
        item.rowKind = 3;
        item.stage = "tool:" + id;
        message.activity.push_back(std::move(item));
        row = &message.activity.back();
    }

    const std::string title = stringField(update, "title");
    if (!title.empty()) row->title = title;
    if (row->title.empty()) row->title = "tool";
    const auto locations = update.find("locations");
    if (locations != update.end() && locations->is_array() && !locations->empty() &&
        (*locations)[0].is_object()) {
        const std::string target = stringField((*locations)[0], "path");
        if (!target.empty()) row->reason = target;
    }

    std::string detail;
    if (update.contains("content")) detail = toolContentSummary(update["content"]);
    if (detail.empty() && update.contains("rawOutput") && !update["rawOutput"].is_null())
        detail = update["rawOutput"].is_string() ? update["rawOutput"].get<std::string>()
                                                  : update["rawOutput"].dump(2);
    if (detail.empty() && update.contains("rawInput") && !update["rawInput"].is_null())
        detail = update["rawInput"].is_string() ? update["rawInput"].get<std::string>()
                                                 : update["rawInput"].dump(2);
    if (!detail.empty()) row->detail = std::move(detail);

    const std::string status = stringField(update, "status");
    if (status == "completed") row->status = "done";
    else if (status == "failed") row->status = "fail";
    else row->status = "running";
}

void applyPlan(ChatMsg& message, const nlohmann::json& update) {
    finalizeStreams(message);
    bool hasMarker = false;
    for (const auto& row : message.activity)
        if (row.rowKind == 18 && row.stage == "__plan__") hasMarker = true;
    if (!hasMarker) {
        ActivityItem marker;
        marker.rowKind = 18;
        marker.stage = "__plan__";
        marker.status = "done";
        message.activity.push_back(std::move(marker));
    }

    PlanState plan;
    plan.present = true;
    plan.fromEvent = true;
    plan.shape = "checklist";
    plan.title = "Tasks";
    const auto entries = update.find("entries");
    if (entries != update.end() && entries->is_array()) {
        size_t index = 0;
        for (const auto& entry : *entries) {
            if (!entry.is_object()) continue;
            const std::string title = stringField(entry, "content");
            if (title.empty()) continue;
            PlanItem item;
            item.id = "acp:" + std::to_string(index++);
            item.title = title;
            item.kind = "task";
            const std::string status = stringField(entry, "status");
            item.status = status == "completed" ? "done"
                        : status == "in_progress" ? "running" : "pending";
            plan.items.push_back(std::move(item));
        }
    }
    message.planState = std::move(plan);
}

ActivityItem& usageRow(ChatMsg& message, const std::string& stage, const std::string& title) {
    for (auto& row : message.activity)
        if (row.rowKind == 0 && row.stage == stage) return row;
    ActivityItem row;
    row.rowKind = 0;
    row.stage = stage;
    row.title = title;
    row.status = "done";
    message.activity.push_back(std::move(row));
    return message.activity.back();
}

void applyUsage(Conversation& conversation, std::vector<ChatMsg>& messages,
                State& state, const nlohmann::json& update,
                int* activeTokens, int* activeWindow) {
    const auto used = update.find("used");
    if (used != update.end() && used->is_number_integer()) {
        const int value = std::max(0, used->get<int>());
        conversation.lastContextTokens = value;
        if (activeTokens) *activeTokens = value;
    }
    const auto size = update.find("size");
    if (activeWindow && size != update.end() && size->is_number_integer())
        *activeWindow = std::max(0, size->get<int>());

    const auto cost = update.find("cost");
    if (cost == update.end() || !cost->is_object()) return;
    const auto amount = cost->find("amount");
    if (amount == cost->end() || !amount->is_number()) return;
    ChatMsg& message = assistantForUpdate(messages, state, {});
    finalizeStreams(message);
    ActivityItem& row = usageRow(message, "acp:cost", "Session usage");
    std::ostringstream label;
    label << stringField(*cost, "currency") << ' ' << std::fixed << std::setprecision(4)
          << amount->get<double>();
    row.reason = label.str();
}

} // namespace

void reset(State& state) { state = {}; }
void setHistoryReplay(State& state, bool replaying) { state.replaying = replaying; }
void bindPendingAssistant(State& state, size_t index) {
    state.activeAssistant = index;
    state.pendingAssistant = index;
    state.replaying = false;
}

void applyUpdate(Conversation& conversation, std::vector<ChatMsg>& messages,
                 State& state, const nlohmann::json& update,
                 int* activeContextTokens, int* activeContextWindow) {
    if (!update.is_object()) return;
    const std::string kind = stringField(update, "sessionUpdate");
    const std::string messageId = stringField(update, "messageId");
    if (kind == "user_message_chunk") {
        if (update.contains("content")) applyUserContent(messages, state, messageId, update["content"]);
    } else if (kind == "agent_message_chunk" || kind == "agent_thought_chunk") {
        ChatMsg& message = assistantForUpdate(messages, state, messageId);
        if (update.contains("content"))
            applyAssistantContent(message, update["content"], kind == "agent_thought_chunk");
    } else if (kind == "tool_call" || kind == "tool_call_update") {
        applyToolUpdate(assistantForUpdate(messages, state, {}), update);
    } else if (kind == "plan") {
        applyPlan(assistantForUpdate(messages, state, {}), update);
    } else if (kind == "usage_update") {
        applyUsage(conversation, messages, state, update, activeContextTokens, activeContextWindow);
    } else if (kind == "session_info_update") {
        const auto title = update.find("title");
        if (title != update.end() && title->is_string() && !title->get_ref<const std::string&>().empty())
            conversation.title = title->get<std::string>();
    }
    // available_commands_update has no maintained composer field; current-mode
    // and config-option updates likewise have no existing CAD state consumer.
}

void applyPromptResponse(Conversation&, std::vector<ChatMsg>& messages,
                         State& state, const nlohmann::json& response,
                         bool cancelled) {
    size_t index = state.pendingAssistant;
    if (index >= messages.size()) index = state.activeAssistant;
    if (index >= messages.size()) {
        appendAssistant(messages, state);
        index = messages.size() - 1;
    }
    ChatMsg& message = messages[index];
    finalizeStreams(message);
    message.running = false;
    const std::string reason = stringField(response, "stopReason");
    if (reason == "cancelled" || cancelled) {
        message.liveStatus = "Cancelled";
    } else if (reason == "max_tokens" || reason == "max_turn_requests") {
        message.resultPartial = true;
        message.liveStatus = reason == "max_tokens" ? "Stopped at token limit" : "Stopped at turn limit";
    } else if (reason == "refusal") {
        message.errKind = ERR_SYSTEM;
        message.errorHuman = "The coding agent refused this request.";
        message.errorFull = message.errorHuman;
    } else {
        message.liveStatus.clear();
    }

    const auto usage = response.find("usage");
    if (usage != response.end() && usage->is_object()) {
        const int input = usage->value("inputTokens", 0);
        const int output = usage->value("outputTokens", 0);
        if (input > 0 || output > 0) {
            ActivityItem& row = usageRow(message, "acp:turn-usage", "Model response");
            row.tokensIn = std::max(0, input);
            row.tokensOut = std::max(0, output);
        }
        const int cached = usage->value("cachedReadTokens", 0);
        if (cached > 0) {
            message.cacheReadTok = std::max(message.cacheReadTok, cached);
            message.cacheReadTotTok += cached;
            ++message.cacheReadCalls;
        }
    }
    state.pendingAssistant = kNoMessage;
}

void markPromptFailure(std::vector<ChatMsg>& messages, State& state,
                       size_t preferred, const std::string& error) {
    size_t index = preferred;
    if (index >= messages.size()) index = state.pendingAssistant;
    if (index >= messages.size()) index = state.activeAssistant;
    if (index >= messages.size()) {
        appendAssistant(messages, state);
        index = messages.size() - 1;
    }
    ChatMsg& message = messages[index];
    finalizeStreams(message);
    message.running = false;
    message.errKind = ERR_SYSTEM;
    message.errorHuman = error.empty() ? "Chudcode request failed." : error;
    message.errorFull = error;
    message.liveStatus.clear();
    state.pendingAssistant = kNoMessage;
}

ServerRequestDecision applyServerRequest(Conversation&, std::vector<ChatMsg>& messages,
                                         State& state, const std::string& method,
                                         const nlohmann::json& params) {
    if (method != "session/request_permission") {
        ServerRequestDecision decision;
        decision.error = true;
        decision.errorCode = -32601;
        decision.errorMessage = "Method not found: " + method;
        return decision;
    }

    ChatMsg& message = assistantForUpdate(messages, state, {});
    nlohmann::json tool = params.is_object()
                              ? params.value("toolCall", nlohmann::json::object())
                              : nlohmann::json::object();
    if (!tool.is_object()) tool = nlohmann::json::object();
    if (!tool.contains("toolCallId")) tool["toolCallId"] = "permission";
    if (!tool.contains("title")) tool["title"] = "Permission required";
    tool["status"] = "failed";
    tool["rawOutput"] =
        "Permission was rejected because Chudcode v0.1 has no permission prompt UI. "
        "Re-run with a policy that does not require interactive approval.";
    applyToolUpdate(message, tool);
    message.liveStatus = "Permission rejected — change the agent policy and retry";

    ServerRequestDecision decision;
    decision.result = {{"outcome", {{"outcome", "cancelled"}}}};
    return decision;
}

} // namespace ui::code::mapping

namespace ui::code::testing {
namespace {
mapping::State g_state;
}

void resetMapping() { mapping::reset(g_state); }
void setHistoryReplay(bool replaying) { mapping::setHistoryReplay(g_state, replaying); }
void applyUpdate(Conversation& conversation, std::vector<ChatMsg>& messages,
                 const nlohmann::json& update) {
    mapping::applyUpdate(conversation, messages, g_state, update);
}
void applyPromptResponse(Conversation& conversation, std::vector<ChatMsg>& messages,
                         const nlohmann::json& response, bool cancelled) {
    mapping::applyPromptResponse(conversation, messages, g_state, response, cancelled);
}
nlohmann::json applyServerRequest(Conversation& conversation,
                                  std::vector<ChatMsg>& messages,
                                  const std::string& method,
                                  const nlohmann::json& params,
                                  bool& isError, int& errorCode,
                                  std::string& errorMessage) {
    mapping::ServerRequestDecision decision =
        mapping::applyServerRequest(conversation, messages, g_state, method, params);
    isError = decision.error;
    errorCode = decision.errorCode;
    errorMessage = decision.errorMessage;
    return decision.result;
}
} // namespace ui::code::testing
