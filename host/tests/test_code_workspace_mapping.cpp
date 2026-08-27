// GL-free mapper coverage for the Chudcode ACP adapter. The narrow test seam
// mutates the maintained Conversation / ChatMsg / ActivityItem / PlanState model;
// there is deliberately no test-only transcript representation.
#include "core/app_state.h"
#include "ui/code/code_mapping.h"

#include <cstdio>
#include <string>
#include <vector>


static int g_failures = 0;

#define CHECK(cond)                                                               \
    do {                                                                          \
        if (!(cond)) {                                                            \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);          \
            ++g_failures;                                                         \
        }                                                                         \
    } while (0)

using ui::code::testing::applyPromptResponse;
using ui::code::testing::applyServerRequest;
using ui::code::testing::applyUpdate;
using ui::code::testing::resetMapping;
using ui::code::testing::setHistoryReplay;

static nlohmann::json textUpdate(const char* kind, const char* id, const char* text) {
    return {
        {"sessionUpdate", kind},
        {"messageId", id},
        {"content", {{"type", "text"}, {"text", text}}},
    };
}

static void test_ordered_text_thinking_tool_interleave() {
    resetMapping();
    Conversation conversation;
    conversation.id = "s1";
    std::vector<ChatMsg> messages;

    applyUpdate(conversation, messages, textUpdate("agent_message_chunk", "a1", "before "));
    applyUpdate(conversation, messages, textUpdate("agent_thought_chunk", "a1", "reasoning"));
    applyUpdate(conversation, messages,
                {{"sessionUpdate", "tool_call"},
                 {"toolCallId", "t1"},
                 {"title", "Read file"},
                 {"kind", "read"},
                 {"status", "pending"},
                 {"rawInput", {{"path", "src/a.cpp"}}}});
    applyUpdate(conversation, messages,
                {{"sessionUpdate", "tool_call_update"},
                 {"toolCallId", "t1"},
                 {"status", "completed"},
                 {"content", nlohmann::json::array({
                     {{"type", "content"}, {"content", {{"type", "text"}, {"text", "file body"}}}}
                 })}});
    applyUpdate(conversation, messages, textUpdate("agent_message_chunk", "a1", "after"));

    CHECK(messages.size() == 1);
    CHECK(messages[0].text == "before after");
    CHECK(messages[0].activity.size() == 4);
    CHECK(messages[0].activity[0].stage == "stream:out");
    CHECK(messages[0].activity[1].stage == "thought:stream");
    CHECK(messages[0].activity[2].stage == "tool:t1");
    CHECK(messages[0].activity[2].status == "done");
    CHECK(messages[0].activity[3].stage == "stream:out");
}

static void test_history_replay_uses_real_message_rows() {
    resetMapping();
    setHistoryReplay(true);
    Conversation conversation;
    conversation.id = "history";
    std::vector<ChatMsg> messages;

    applyUpdate(conversation, messages, textUpdate("user_message_chunk", "u1", "hello "));
    applyUpdate(conversation, messages, textUpdate("user_message_chunk", "u1", "world"));
    applyUpdate(conversation, messages, textUpdate("agent_message_chunk", "a1", "answer"));
    applyUpdate(conversation, messages,
                {{"sessionUpdate", "tool_call"},
                 {"toolCallId", "hist-tool"},
                 {"title", "Search"},
                 {"status", "pending"}});
    applyUpdate(conversation, messages,
                {{"sessionUpdate", "tool_call_update"},
                 {"toolCallId", "hist-tool"},
                 {"status", "failed"},
                 {"rawOutput", "not found"}});
    applyUpdate(conversation, messages, textUpdate("user_message_chunk", "u2", "next"));
    applyUpdate(conversation, messages, textUpdate("agent_thought_chunk", "a2", "consider"));

    CHECK(messages.size() == 4);
    CHECK(messages[0].fromUser && messages[0].text == "hello world");
    CHECK(!messages[1].fromUser && messages[1].text == "answer");
    CHECK(messages[1].activity.size() == 2);
    CHECK(messages[1].activity[1].stage == "tool:hist-tool");
    CHECK(messages[1].activity[1].status == "fail");
    CHECK(messages[2].fromUser && messages[2].text == "next");
    CHECK(!messages[3].fromUser && !messages[3].running);
    CHECK(messages[3].activity.size() == 1);
    CHECK(messages[3].activity[0].stage == "thought:stream");
}

static void test_todo_plan_snapshot_and_clear() {
    resetMapping();
    Conversation conversation;
    std::vector<ChatMsg> messages;

    applyUpdate(conversation, messages,
                {{"sessionUpdate", "plan"},
                 {"entries", nlohmann::json::array({
                     {{"content", "Inspect"}, {"priority", "medium"}, {"status", "completed"}},
                     {{"content", "Edit"}, {"priority", "medium"}, {"status", "in_progress"}},
                     {{"content", "Verify"}, {"priority", "medium"}, {"status", "pending"}},
                 })}});

    CHECK(messages.size() == 1);
    CHECK(messages[0].planState.present);
    CHECK(messages[0].planState.shape == "checklist");
    CHECK(messages[0].planState.items.size() == 3);
    CHECK(messages[0].planState.items[0].status == "done");
    CHECK(messages[0].planState.items[1].status == "running");
    CHECK(messages[0].planState.items[2].status == "pending");
    CHECK(!messages[0].activity.empty());
    CHECK(messages[0].activity[0].stage == "__plan__");

    applyUpdate(conversation, messages,
                {{"sessionUpdate", "plan"}, {"entries", nlohmann::json::array()}});
    CHECK(messages[0].planState.present);
    CHECK(messages[0].planState.items.empty());
}

static void test_unknown_usage_and_pricing_stay_unknown() {
    resetMapping();
    Conversation conversation;
    std::vector<ChatMsg> messages;
    applyUpdate(conversation, messages, textUpdate("agent_message_chunk", "a", "done"));

    const size_t rowsBefore = messages[0].activity.size();
    applyPromptResponse(conversation, messages, {{"stopReason", "end_turn"}}, false);
    CHECK(messages[0].activity.size() == rowsBefore);
    CHECK(messages[0].cacheReadTok == 0);
    CHECK(messages[0].cacheReadTotTok == 0);
    CHECK(messages[0].cacheReadCalls == 0);
    CHECK(conversation.lastContextTokens == 0);

    applyUpdate(conversation, messages,
                {{"sessionUpdate", "usage_update"}, {"size", 200000}, {"used", 12345}});
    CHECK(conversation.lastContextTokens == 12345);
    bool inventedCost = false;
    for (const auto& row : messages[0].activity)
        if (row.stage == "acp:cost") inventedCost = true;
    CHECK(!inventedCost);
}

static void test_permission_is_typed_rejection_and_actionable_row() {
    resetMapping();
    Conversation conversation;
    std::vector<ChatMsg> messages;
    bool isError = true;
    int errorCode = 99;
    std::string errorMessage = "stale";

    const nlohmann::json result = applyServerRequest(
        conversation, messages, "session/request_permission",
        {{"sessionId", "s"},
         {"toolCall", {{"toolCallId", "danger"}, {"title", "Run command"}, {"status", "pending"}}},
         {"options", nlohmann::json::array({
             {{"optionId", "yes"}, {"name", "Allow"}, {"kind", "allow_once"}}
         })}},
        isError, errorCode, errorMessage);

    CHECK(!isError);
    CHECK(errorCode == 0);
    CHECK(errorMessage.empty());
    CHECK(result["outcome"]["outcome"] == "cancelled");
    CHECK(messages.size() == 1);
    CHECK(messages[0].activity.size() == 1);
    CHECK(messages[0].activity[0].stage == "tool:danger");
    CHECK(messages[0].activity[0].status == "fail");
    CHECK(messages[0].activity[0].detail.find("no permission prompt UI") != std::string::npos);
    CHECK(messages[0].liveStatus.find("Permission rejected") != std::string::npos);
}

static void test_unknown_server_request_is_method_not_found() {
    resetMapping();
    Conversation conversation;
    std::vector<ChatMsg> messages;
    bool isError = false;
    int errorCode = 0;
    std::string errorMessage;

    const nlohmann::json result = applyServerRequest(
        conversation, messages, "workspace/delete_everything", nlohmann::json::object(),
        isError, errorCode, errorMessage);

    CHECK(isError);
    CHECK(errorCode == -32601);
    CHECK(errorMessage.find("Method not found") != std::string::npos);
    CHECK(result.is_object() && result.empty());
    CHECK(messages.empty());
}

static void test_arrival_order_is_never_resorted() {
    resetMapping();
    Conversation conversation;
    std::vector<ChatMsg> messages;

    applyUpdate(conversation, messages,
                {{"sessionUpdate", "tool_call"}, {"toolCallId", "z-last-lexically"},
                 {"title", "First arrival"}, {"status", "pending"}});
    applyUpdate(conversation, messages,
                {{"sessionUpdate", "tool_call"}, {"toolCallId", "a-first-lexically"},
                 {"title", "Second arrival"}, {"status", "pending"}});
    applyUpdate(conversation, messages,
                {{"sessionUpdate", "tool_call"}, {"toolCallId", "m-middle-lexically"},
                 {"title", "Third arrival"}, {"status", "pending"}});

    CHECK(messages.size() == 1);
    if (messages.size() != 1) return;
    CHECK(messages[0].activity.size() == 3);
    if (messages[0].activity.size() != 3) return;
    CHECK(messages[0].activity[0].stage == "tool:z-last-lexically");
    CHECK(messages[0].activity[1].stage == "tool:a-first-lexically");
    CHECK(messages[0].activity[2].stage == "tool:m-middle-lexically");
}

int main() {
    test_ordered_text_thinking_tool_interleave();
    test_history_replay_uses_real_message_rows();
    test_todo_plan_snapshot_and_clear();
    test_unknown_usage_and_pricing_stay_unknown();
    test_permission_is_typed_rejection_and_actionable_row();
    test_unknown_server_request_is_method_not_found();
    test_arrival_order_is_never_resorted();
    if (g_failures == 0) std::printf("All Chudcode mapper tests passed.\n");
    return g_failures == 0 ? 0 : 1;
}
