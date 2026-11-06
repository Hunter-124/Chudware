#include "ui/ai/active_work_feed.h"

#include <iostream>

int main() {
    ui::AwStore store;
    store.feed("run_started", {{"run_id", "run-a"}}, "conversation-a");
    store.feed("agent.tool_call", {{"run_id", "run-a"}, {"tool_name", "a-first"}}, "conversation-a");
    store.feed("run_started", {{"run_id", "run-b"}}, "conversation-b");
    store.feed("agent.tool_call", {{"run_id", "run-b"}, {"tool_name", "b-first"}}, "conversation-b");
    store.feed("agent.tool_call", {{"run_id", "run-a"}, {"tool_name", "a-second"}}, "conversation-a");
    if (store.ops.size() != 3 || store.ops[0].tool != "a-first" ||
        store.ops[1].tool != "b-first" || store.ops[2].tool != "a-second") {
        std::cerr << "FAIL: simultaneous conversations must retain each other's tool activity\n";
        return 1;
    }
    if (store.ops[0].conversationId != "conversation-a" ||
        store.ops[1].conversationId != "conversation-b" ||
        store.ops[2].conversationId != "conversation-a") {
        std::cerr << "FAIL: tool activity must retain conversation provenance\n";
        return 1;
    }
    store.feed("run_started", {{"run_id", "run-a"}}, "conversation-a");
    if (store.ops.size() != 3) {
        std::cerr << "FAIL: duplicate start of the same run must not clear activity\n";
        return 1;
    }
    store.feed("run_started", {{"run_id", "run-a-next"}}, "conversation-a");
    if (store.ops.size() != 1 || store.ops[0].tool != "b-first" ||
        store.ops[0].conversationId != "conversation-b") {
        std::cerr << "FAIL: starting a new run must clear only its own conversation\n";
        return 1;
    }
    store.feed("agent.tool_call", {{"run_id", "run-a-next"}, {"tool_name", "a-next"}}, "conversation-a");
    store.feed("agent.tool_call", {{"tool_name", "unowned"}}, "conversation-a");
    store.feed("agent.tool_call", {{"run_id", "run-a-next"}}, "");
    if (store.ops.size() != 2 || store.ops[1].tool != "a-next") {
        std::cerr << "FAIL: run and conversation identity are required\n";
        return 1;
    }
    std::cout << "active work feed: PASS\n";
    return 0;
}
