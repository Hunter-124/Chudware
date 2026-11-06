#include "core/app_state.h"
#include "core/jobs.h"
#include "core/fs_backend.h"
#include "ui/copilot/activity_settlement.h"
#include "worker_client.h"
#include "engine_backend.h"

#include <iostream>
#include <memory>
#include <string>
#include <utility>

#include <nlohmann/json.hpp>

// jobs.cpp's job launchers call into WorkerClient; this test only exercises route
// building and never launches a job. GNU ld / Apple ld drop those launchers
// (--gc-sections / -dead_strip), but MSVC resolves every referenced symbol before
// /OPT:REF and failed the Windows release build with LNK2019. Inert definitions keep
// the test self-contained on every linker without pulling the real IPC client in.
bool WorkerClient::call(const std::string&, const nlohmann::json&, nlohmann::json&,
                        std::string& err, int, const EventFn&) {
    err = "WorkerClient is not available in test_agent_job_routing";
    return false;
}
bool WorkerClient::callAgentCancel(const std::string&, nlohmann::json&, std::string& err,
                                 const std::string&) {
    err = "WorkerClient is not available in test_agent_job_routing";
    return false;
}

namespace {

int failures = 0;

void check(bool condition, const char* label) {
    if (!condition) {
        std::cerr << "FAIL: " << label << '\n';
        ++failures;
    }
}

void testOnlineNormalRoute() {
    const nlohmann::json history = nlohmann::json::array(
        {{{"role", "user"}, {"text", "first"}},
         {{"role", "assistant"}, {"text", "second"}}});
    const nlohmann::json attachments = nlohmann::json::array(
        {{{"path", "/tmp/example.png"}, {"name", "example.png"}, {"kind", "image"}}});
    const nlohmann::json extra = {
        {"provider", "xai"},
        {"model", "grok-4.5"},
        {"reasoning_effort", "high"},
        {"attachments", attachments},
        {"context_refs", nlohmann::json::array({{{"kind", "face"}, {"id", "Face12"}}})},
        {"options",
         {{"session_id", "session-17"},
          {"sessions_dir", "/tmp/sessions"},
          {"omp_session_file", "/original/runtime journal.jsonl"},
          {"workspace_dir", "/tmp/project"},
          {"history", history},
          {"advisor", false}}}};

    const AgentJobRpcRoute route =
        buildAgentJobRpcRoute("make the bracket", "doc-bracket", "conv-fallback", true, extra, false);
    check(route.method == "agent_admin_turn", "normal online route uses agent_admin_turn");
    check(route.params.value("request", std::string()) == "make the bracket", "request preserved");
    check(route.params.value("doc_id", std::string()) == "doc-bracket", "document id preserved");
    check(route.params.value("provider", std::string()) == "xai", "provider flattened");
    check(route.params.value("model", std::string()) == "grok-4.5", "model flattened");
    check(route.params.value("reasoning_effort", std::string()) == "high", "effort flattened");
    check(route.params.value("mode", std::string()) == "agent", "normal mode is agent");
    check(route.params.value("cam", false), "CAM option flattened");
    check(route.params.value("session_id", std::string()) == "session-17", "session id flattened");
    check(route.params.value("sessions_dir", std::string()) == "/tmp/sessions",
          "sessions directory flattened");
    check(route.params.value("omp_session_file", std::string()) ==
              "/original/runtime journal.jsonl", "original runtime journal path preserved");
    check(route.params.value("workspace_dir", std::string()) == "/tmp/project",
          "workspace directory flattened");
    check(route.params["history"] == history, "history flattened without request injection");
    check(route.params["attachments"] == attachments, "attachments preserved");
    check(route.params.contains("context_refs"), "context refs preserved");
    check(route.params.value("run_id", std::string()).rfind("gui-", 0) == 0,
          "normal route allocates run id before call");
    check(route.params.value("request", std::string()).find("CONVERSATION HISTORY") ==
              std::string::npos,
          "history is not duplicated into request");
}

void testRunAndSessionIds() {
    const nlohmann::json extra = {
        {"run_id", "run-explicit"}, {"options", {{"session_id", "session-explicit"}}}};
    const AgentJobRpcRoute route =
        buildAgentJobRpcRoute("continue", "doc", "conv", false, extra, false);
    check(route.params.value("run_id", std::string()) == "run-explicit",
          "explicit run id preserved");
    check(route.params.value("session_id", std::string()) == "session-explicit",
          "explicit session id preserved");

    const AgentJobRpcRoute fallback =
        buildAgentJobRpcRoute("continue", "doc", "conv-fallback", false,
                              nlohmann::json::object(), false);
    check(fallback.params.value("session_id", std::string()) == "conv-fallback",
          "conversation id supplies missing session id");
}

// An ordinary online turn has no wall-clock stop: no budget is stamped and the client waits for
// the worker's reply (Stop, worker/daemon loss and teardown still end it). The old 7200 s
// default stopped healthy turns at two hours. An explicit caller budget still bounds the turn
// and the client waits past it so the worker's structured "turn_budget" reply arrives first.
void testOnlineTurnBudget() {
    const AgentJobRpcRoute route =
        buildAgentJobRpcRoute("fix the brakes", "doc", "conv", false, nlohmann::json::object(), false);
    check(!route.params.contains("timeout"), "ordinary online turn carries no default budget");
    check(agentJobWaitMs(route) < 0, "ordinary online turn waits for its reply, not a clock");

    for (const nlohmann::json& bad : {nlohmann::json(0), nlohmann::json(-5),
                                      nlohmann::json("7200"), nlohmann::json(nullptr)}) {
        const AgentJobRpcRoute r =
            buildAgentJobRpcRoute("go", "doc", "conv", false, {{"timeout", bad}}, false);
        check(!r.params.contains("timeout"), "non-budget timeout is dropped, not forwarded");
        check(agentJobWaitMs(r) < 0, "non-budget timeout leaves the wait unbounded");
    }

    const AgentJobRpcRoute explicitBudget = buildAgentJobRpcRoute(
        "fix the brakes", "doc", "conv", false, {{"timeout", 14400}}, true);
    check(explicitBudget.params.value("timeout", 0.0) == 14400.0, "explicit budget preserved");
    check(agentJobWaitMs(explicitBudget) > 14400 * 1000,
          "explicit budget: client waits past it for the worker's turn_budget reply");
}

// The composer's user-picked mode is authoritative for a normal (non-mission) turn: the route
// must never re-derive it, and an absent/blank mode falls back to "agent".
void testComposerModePreserved() {
    for (const char* mode : {"plan", "ask", "agent"}) {
        const AgentJobRpcRoute route = buildAgentJobRpcRoute(
            "what is this", "doc", "conv", false, {{"options", {{"mode", mode}}}}, false);
        check(route.params.value("mode", std::string()) == mode,
              "composer mode preserved at top level");
        check(route.params["options"].value("mode", std::string()) == mode,
              "composer mode preserved in options");
    }
    const AgentJobRpcRoute blank = buildAgentJobRpcRoute(
        "hi", "doc", "conv", false, {{"options", {{"mode", ""}}}}, false);
    check(blank.params.value("mode", std::string()) == "agent", "blank mode defaults to agent");
    check(blank.params["options"].value("mode", std::string()) == "agent",
          "blank options mode defaults to agent");

    // A mission still forces the swarm lane even when the composer picked plan.
    const AgentJobRpcRoute mission = buildAgentJobRpcRoute(
        "build and verify", "doc", "conv", false, {{"options", {{"mode", "plan"}}}}, true);
    check(mission.params.value("mode", std::string()) == "swarm",
          "mission overrides the composer mode");
}

void testOnlineMissionRoute() {
    const nlohmann::json extra = {
        {"provider", "anthropic"},
        {"attachments", nlohmann::json::array({{{"path", "/tmp/spec.txt"}}})},
        {"options",
         {{"mode", "plan"},
          {"history", nlohmann::json::array({{{"role", "user"}, {"text", "design it"}}})},
          {"session_id", "mission-session"}}}};
    const AgentJobRpcRoute route =
        buildAgentJobRpcRoute("build and verify", "doc-m", "conv-m", false, extra, true);
    check(route.method == "agent_admin_turn", "mission online route uses agent_admin_turn");
    check(route.params.value("doc_id", std::string()) == "doc-m", "mission document id preserved");
    check(route.params.value("mode", std::string()) == "swarm", "mission mode forced to swarm");
    check(route.params["options"].value("mode", std::string()) == "swarm",
          "mission options mode matches top level");
    check(route.params.value("provider", std::string()) == "anthropic",
          "mission provider preserved");
    check(route.params.contains("history") && route.params.contains("attachments"),
          "mission history and attachments preserved");
}

void testAdminResultMapping() {
    const nlohmann::json success = normalizeAgentAdminTurnResult(
        {{"ok", true},
         {"turn",
          {{"success", true},
           {"stop_reason", "completed"},
           {"result", "Bracket saved."},
           {"run_id", "run-returned"}}}},
        false);
    check(success.value("status", std::string()) == "done", "online success maps to done");
    check(success.value("lane", std::string()) == "general", "normal result maps to general lane");
    check(success.value("summary", std::string()) == "Bracket saved.",
          "normal terminal text maps to summary");
    check(success.value("run_id", std::string()) == "run-returned", "returned run id maps");

    const nlohmann::json mission = normalizeAgentAdminTurnResult(
        {{"ok", true}, {"turn", {{"success", true}, {"result", "All checks pass."}}}}, true);
    check(mission.value("status", std::string()) == "done", "mission success maps to done");
    check(mission.value("verdict", std::string()) == "All checks pass.",
          "mission terminal text maps to verdict");

    const nlohmann::json cancelled = normalizeAgentAdminTurnResult(
        {{"ok", true},
         {"turn", {{"success", false}, {"cancelled", true}, {"stop_reason", "cancelled"}}}},
        false);
    check(cancelled.value("status", std::string()) == "cancelled", "cancel maps to cancelled");
    const nlohmann::json skipped = normalizeAgentAdminTurnResult(
        {{"ok", true}, {"turn", {{"success", false}, {"stop_reason", "skipped"},
                                  {"outcome", "skipped"}}}}, false);
    check(skipped.value("status", std::string()) != "done" &&
              skipped.value("stop_reason", std::string()) == "skipped",
          "skipped maintenance is not reported as committed");

    const nlohmann::json failed =
        normalizeAgentAdminTurnResult({{"ok", false}, {"reason", "authentication failed"}}, false);
    check(failed.value("error", std::string()) == "authentication failed",
          "admin reason maps to legacy error");
    check(failed.value("error_detail", std::string()) == "authentication failed",
          "admin reason maps to error detail");
    check(failed.value("stop_reason", std::string()) == "runtime_error",
          "unclassified admin failure maps to runtime error");
    for (const std::string reason : {"runtime_error", "provider_error", "turn_budget"}) {
        const auto nested = normalizeAgentAdminTurnResult(
            {{"ok", false}, {"reason", "failed"}, {"turn", {{"stop_reason", reason}}}}, false);
        check(nested.value("stop_reason", std::string()) == reason,
              "admin failure preserves nested stop reason");
    }
}

void testEventConsumption() {
    auto job = std::make_shared<JobState>();
    consumeAgentJobEvent(job,
                         {{"kind", "stage_started"},
                          {"run_id", "event-run"},
                          {"stage", "model"},
                          {"role", "designer"},
                          {"attempt", 0}},
                         false);
    {
        std::lock_guard<std::mutex> lock(job->mtx);
        check(job->runId == "event-run", "event consumption captures run id");
    }
    {
        std::lock_guard<std::mutex> lock(job->evMtx);
        check(job->events.size() == 1, "event consumption queues exactly once");
        check(job->events.front().kind == "orch_stage_started", "event kind keeps orch prefix");
        check(job->events.front().notes == "model > designer attempt 1...",
              "event phase note preserved");
        check(job->events.front().data &&
                  job->events.front().data->value("run_id", std::string()) == "event-run",
              "full event payload preserved");
    }

    consumeAgentJobEvent(job,
                         {{"kind", "run_finished"},
                          {"status", "failed"},
                          {"stop_reason", "provider_error"},
                          {"error_detail", "raw detail"},
                          {"error_human", "Sign in again"}},
                         false);
    {
        std::lock_guard<std::mutex> lock(job->mtx);
        check(job->stopReason == "provider_error", "terminal event maps stop reason");
        check(job->errorDetail == "raw detail", "terminal event maps raw detail");
        check(job->errorHuman == "Sign in again", "terminal event maps human detail");
    }
    {
        std::lock_guard<std::mutex> lock(job->evMtx);
        check(job->events.size() == 2, "terminal event queues exactly once");
    }
    AgentEvent ignored;
    while (takeAgentEvent(*job, ignored)) {}
    consumeAgentJobEvent(job, {{"kind", "assistant_delta"}, {"channel", "text"},
                              {"text", "first "}, {"run_id", "event-run"}}, false);
    consumeAgentJobEvent(job, {{"kind", "assistant_delta"}, {"channel", "text"},
                              {"text", "second"}, {"run_id", "event-run"}}, false);
    consumeAgentJobEvent(job, {{"kind", "assistant_delta"}, {"channel", "thinking"},
                              {"text", "next"}, {"run_id", "event-run"}}, false);
    consumeAgentJobEvent(job, {{"kind", "stage_started"}, {"stage", "after"},
                              {"run_id", "event-run"}}, false);
    AgentEvent joined, boundary, after;
    check(takeAgentEvent(*job, joined) && joined.data &&
              joined.data->value("text", std::string()) == "first second" &&
              takeAgentEvent(*job, boundary) && boundary.data &&
              boundary.data->value("channel", std::string()) == "thinking" &&
              boundary.data->value("text", std::string()) == "next" &&
              takeAgentEvent(*job, after) && after.kind == "orch_stage_started" &&
              !agentEventsPending(*job),
          "adjacent deltas concatenate without crossing channel or event boundaries");
}

void testConcurrentCellEventIsolation() {
    auto compact = std::make_shared<JobState>();
    auto conversation = std::make_shared<JobState>();
    compact->runId = "compact-a";
    conversation->runId = "turn-b";
    const nlohmann::json a = {{"kind", "sub_run"}, {"run_id", "compact-a"},
                               {"status", "running"}, {"purpose", "compaction"}};
    const nlohmann::json b = {{"kind", "assistant_delta"}, {"run_id", "turn-b"},
                               {"text", "reply from B"}};
    const nlohmann::json bFailure = {{"kind", "run_finished"}, {"run_id", "turn-b"},
                                     {"stop_reason", "provider_error"},
                                     {"error_detail", "B failed"}};
    // A cell broadcasts each event to both same-actor turn connections.
    for (const auto& event : {a, b, bFailure}) {
        consumeAgentJobEvent(compact, event, false, true);
        consumeAgentJobEvent(conversation, event, false, true);
    }
    consumeAgentJobEvent(compact, {{"kind", "assistant_delta"}, {"text", "unidentified"}}, false, true);
    auto unbound = std::make_shared<JobState>();
    consumeAgentJobEvent(unbound, a, false, true);
    check(unbound->runId.empty() && !agentEventsPending(*unbound),
          "unbound cell job never adopts another run's broadcast");
    AgentEvent event;
    check(takeAgentEvent(*compact, event) && event.data &&
              event.data->value("run_id", std::string()) == "compact-a" &&
              !agentEventsPending(*compact),
          "background compaction sees only its own streamed activity");
    check(compact->stopReason.empty() && compact->errorDetail.empty(),
          "another conversation's terminal failure cannot poison compaction");
    check(takeAgentEvent(*conversation, event) && event.data &&
              event.data->value("text", std::string()) == "reply from B" &&
              takeAgentEvent(*conversation, event) && event.kind == "orch_run_finished" &&
              !agentEventsPending(*conversation) &&
              conversation->stopReason == "provider_error",
          "conversation receives only its own reply and failure");
}
void testRetiredCorporateActivityRows() {
    ActivityItem row;
    for (int kind : {4, 5, 6, 7, 8}) {
        row.rowKind = kind;
        row.stage = "legacy";
        check(row.isRetiredCorporateRow(), "retired corporate row is not rendered as an action");
    }
    row.rowKind = 7;
    row.stage = "msg:irc:message-1";
    check(!row.isRetiredCorporateRow(), "inter-agent messages retain the shared row kind");
    row.stage = "msg";
    check(row.isRetiredCorporateRow(), "legacy row seven is not mistaken for a message");
    for (int kind : {0, 1, 2, 3, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20}) {
        row.rowKind = kind;
        row.stage = "legacy";
        check(!row.isRetiredCorporateRow(), "ordinary activity row remains available");
    }
}

void testCompactionTerminalSettlement() {
    for (const auto& item : {std::pair<const char*, const char*>{"failed", "fail"},
                            {"error", "fail"}, {"cancelled", "cancelled"},
                            {"interrupted", "stopped"}, {"done", "stopped"}}) {
        ActivityItem activity;
        activity.rowKind = 17;
        activity.stage = "subrun:compaction:run-1";
        activity.title = "Compacting context";
        activity.status = "running";
        check(ui::settleUnfinishedCompaction(activity, item.first), "open compaction settles");
        check(activity.status == item.second && activity.title != "Compacting context",
              "terminal outcome never invents a successful compaction");
        const auto title = activity.title;
        check(!ui::settleUnfinishedCompaction(activity, "done") && activity.title == title,
              "terminal compaction settlement is idempotent");
    }
    ActivityItem committed;
    committed.rowKind = 17;
    committed.stage = "subrun:compaction:run-2";
    committed.title = "Context compacted";
    committed.status = "done";
    check(!ui::settleUnfinishedCompaction(committed, "failed") && committed.status == "done",
          "confirmed compaction stays committed");
    ChatMsg message;
    message.running = true;
    message.compactionRun = true;
    message.liveStatus = "Compacting context";
    ActivityItem open;
    open.rowKind = 17;
    open.stage = "subrun:compaction:run-3";
    open.title = "Compacting context";
    open.status = "running";
    message.activity.push_back(open);
    ui::settleCompactionMessage(message, "error");
    check(!message.running && message.liveStatus.empty() &&
              message.activity[0].status == "fail",
          "terminal error clears owning compaction message and marks failure");
}

void testLateStopCompactionReceipt() {
    struct ConversationMeter { std::string id; int lastContextTokens; };
    const nlohmann::json envelope = {
        {"ok", true},
        {"turn", {{"success", true}, {"outcome", "committed"}, {"cancelled", false},
                  {"stop_reason", "completed"}, {"omp_session_id", "original-session"},
                  {"omp_session_file", "/original/journal.jsonl"},
                  {"compaction", {{"method", "soft"}, {"tokensBefore", 100},
                                  {"tokensAfter", 25}, {"tokensAfterEstimated", true}}}}}};
    const auto normalized = normalizeAgentAdminTurnResult(envelope, false);
    check(normalized.value("status", std::string()) == "done",
          "committed receipt stays done despite a late Stop");
    check(normalized["turn"]["omp_session_file"] == "/original/journal.jsonl" &&
              normalized["turn"]["compaction"]["method"] == "soft",
          "committed method and owning journal are retained");

    int visible = 800;
    std::vector<ConversationMeter> conversations = {{"viewed", 800}, {"owner", 100}};
    ChatMsg message;
    message.compactionRun = true;
    message.running = false;  // Stop already finalized the bubble.
    ActivityItem stopped;
    stopped.rowKind = 17;
    stopped.stage = "subrun:compaction:run";
    stopped.status = "cancelled";
    stopped.title = "Context compaction cancelled";
    stopped.reason = "cancelled";
    message.activity.push_back(stopped);
    check(ui::applyCommittedCompactionReceipt(normalized["turn"], visible, conversations,
                                               false, "owner", &message),
          "authoritative receipt reconciles a stopped maintenance message");
    check(visible == 800 && conversations[1].lastContextTokens == ui::storedContextEstimate(25),
          "estimated post-commit usage goes to the background owner only");
    check(message.activity[0].status == "done" &&
              message.activity[0].title == "Context compacted" &&
              message.activity[0].reason == "done",
          "committed receipt replaces cosmetic cancelled activity");

    auto noUsage = normalized["turn"];
    noUsage["compaction"].erase("tokensAfter");
    check(ui::applyCommittedCompactionReceipt(noUsage, visible, conversations, true, "viewed",
                                               &message) && visible == -1,
          "committed receipt with unknown occupancy invalidates the viewed meter");
    auto measured = normalized["turn"];
    measured["compaction"]["tokensAfterEstimated"] = false;
    check(ui::applyCommittedCompactionReceipt(measured, visible, conversations, true, "viewed",
                                               &message) && visible == 25,
          "provider-anchored occupancy remains an actual measurement");

    const auto cancelled = normalizeAgentAdminTurnResult(
        {{"ok", true}, {"turn", {{"success", false}, {"outcome", "cancelled"},
                                  {"cancelled", true}, {"stop_reason", "cancelled"}}}}, false);
    message.activity[0] = stopped;
    check(cancelled.value("status", std::string()) == "cancelled" &&
              !ui::applyCommittedCompactionReceipt(cancelled["turn"], visible, conversations,
                                                    false, "owner", &message) &&
              conversations[1].lastContextTokens == ui::storedContextEstimate(25) &&
              message.activity[0].status == "cancelled",
          "precommit cancellation leaves the old meter and cancelled activity unchanged");
}

void testWorkspaceRelativePaths() {
    std::string relative, err;
    for (const std::string& root : {std::string(), std::string("."), std::string("./")}) {
        check(chudware::workspaceRelativePath(root, relative, err) && relative.empty(),
              "workspace root stays relative");
    }
    check(chudware::workspaceRelativePath("./parts//bracket.step+", relative, err) &&
              relative == "parts/bracket.step+", "relative workspace path canonicalized");
    for (const std::string& bad : {
             std::string("/work/a.step+"), std::string("/srv/gateway/user/a.step+"),
             std::string("C:/a.step+"), std::string("\\\\server\\share"),
             std::string("../a.step+"), std::string("parts/../a.step+"),
             std::string("parts/.."), std::string("parts\\a.step+"),
             std::string("parts/a\0.step+", 14)}) {
        check(!chudware::workspaceRelativePath(bad, relative, err) &&
                  relative.empty() && !err.empty(),
              "absolute traversal and NUL paths refuse without reinterpretation");
    }
}

void testCellHandlesAndPaths() {
    clearCellAgentSessionHandles();
    const nlohmann::json extra = {
        {"run_id", "client-run"},
        {"options", {{"session_id", "local-conversation"}, {"sessions_dir", "/idbfs/sessions"},
                     {"omp_session_file", "/srv/actor/session.jsonl"},
                     {"omp_session_dir", "/srv/actor/private"},
                     {"workspace_dir", "./parts"}}}};
    auto route = buildAgentJobRpcRoute("go", "doc", "local-conversation", false, extra, false, true);
    check(route.cell && route.error.empty() && !route.params.contains("run_id") &&
              !route.params.contains("session_id"), "cell route never mints client identifiers");
    check(route.params["workspace_dir"] == "parts" &&
              route.params["options"]["workspace_dir"] == "parts",
          "cell workspace selection remains relative in both envelopes");
    check(!route.params.contains("sessions_dir") &&
              !route.params["options"].contains("omp_session_file") &&
              !route.params["options"].contains("omp_session_dir"),
          "browser-local and runtime paths are not sent");
    const std::string run = "opaque:run/not-a-path?raw=%2f";
    const std::string session = "opaque:session/../unchanged";
    std::string err;
    check(applyCellAgentHandles(route, {{"ok", true}, {"run_id", run}, {"session_id", session}}, err),
          "cell mint reply accepted");
    check(route.params["run_id"] == run && route.params["session_id"] == session &&
              route.params["options"]["session_id"] == session,
          "handles remain opaque and byte-exact");
    bindCellAgentSessionHandle("local-conversation", session, "anthropic");
    const auto resumed = buildAgentJobRpcRoute("continue", "doc", "local-conversation", false,
                                               extra, false, true);
    check(resumed.params["session_id"] == session && !resumed.params.contains("run_id"),
          "resume uses only the cell binding and mints a fresh run");
    check(cellAgentSessionHandle("other-conversation").empty(), "local binding is chat-scoped");
    check(cellAgentSessionProvider("local-conversation") == "anthropic",
          "session binding keeps the launched provider independently of the picker");
    bindCellAgentSessionHandle("local-conversation", "child-handle");
    check(cellAgentSessionProvider("local-conversation") == "anthropic",
          "branch handle replacement retains its credential provider");
    bindCellAgentSessionHandle("other-conversation", "other-handle", "xai");
    check(cellAgentSessionProvider("other-conversation") == "xai" &&
              cellAgentSessionProvider("local-conversation") == "anthropic",
          "simultaneous chat controls retain separate credential providers");
    for (const auto& invalid : {nlohmann::json::object(),
                               nlohmann::json{{"ok", false}, {"run_id", run}, {"session_id", session}},
                               nlohmann::json{{"ok", true}, {"run_id", ""}, {"session_id", session}},
                               nlohmann::json{{"ok", true}, {"run_id", run}, {"session_id", 12}}}) {
        const auto before = route.params;
        check(!applyCellAgentHandles(route, invalid, err) && route.params == before,
              "malformed mint reply never mutates the route");
    }
    for (const char* path : {"/srv/gateway", "../outside", "parts/../outside"}) {
        const auto bad = buildAgentJobRpcRoute("go", "doc", "chat", false,
            {{"options", {{"workspace_dir", path}}}}, false, true);
        check(!bad.error.empty(), "cell route rejects an unsafe workspace selection");
        const auto attachment = buildAgentJobRpcRoute("go", "doc", "chat", false,
            {{"attachments", nlohmann::json::array({{{"path", path}}})}}, false, true);
        check(!attachment.error.empty(), "cell route rejects unsafe attachment paths");
    }
    clearCellAgentSessionHandles();
}

void testCellPendingStop() {
    JobState pending, other;
    check(requestCellAgentStop(pending).empty() && pending.stopRequested.load(),
          "Stop before mint records only a per-job pending cancellation");
    check(publishCellAgentRunHandle(other, "other-run").empty() && !other.stopRequested.load(),
          "pending Stop does not affect another job");
    check(publishCellAgentRunHandle(pending, "minted-run") == "minted-run",
          "mint publication consumes pending Stop as an exact handle");
    check(requestCellAgentStop(pending).empty(), "repeated Stop does not dispatch again");
    JobState started;
    check(publishCellAgentRunHandle(started, "running-handle").empty(),
          "normal mint publication does not cancel");
    check(requestCellAgentStop(started) == "running-handle",
          "Stop after mint returns the unchanged exact handle");
}

void testCellProtocolAdmission() {
    const nlohmann::json current = {
        {"proto", 1}, {"abi", "1"}, {"state", "ready"},
        {"workspace_key", "user:00000000-0000-4000-8000-000000000001"},
        {"web_rpc_version", 2},
        {"web_rpc_methods", nlohmann::json::array(
            {"subscribe", "doc_list", "doc_open", "agent_handles_new",
             "omp_models_list", "agent_admin_turn"})}};
    check(engine::webCellProtocolReady(current), "version two cell enables browser capabilities");
    check(engine::webCellMethodAllowed(current, "doc_open") &&
              engine::webCellMethodAllowed(current, "agent_handles_new"),
          "only advertised CAD and handle methods are admitted");
    check(!engine::webCellMethodAllowed(current, "doc_rebuild"),
          "unadvertised method is refused without legacy fallback");
    check(engine::webCellMethodAllowed(current, "subscribe") &&
              engine::webCellMethodAllowed(current, "doc_list") &&
              engine::webCellMethodAllowed(current, "omp_models_list") &&
              engine::webCellMethodAllowed(current, "agent_admin_turn"),
          "actual ready hello admits document bootstrap and credential-free provider chat");
    check(!engine::webCellMethodAllowed(current, "recovery_list"),
          "desktop-only recovery is not implied by a ready cell hello");
    for (auto invalid : {nlohmann::json::object(),
                         nlohmann::json{{"web_rpc_version", 1}, {"web_rpc_methods", current["web_rpc_methods"]}},
                         nlohmann::json{{"web_rpc_version", 3}, {"web_rpc_methods", current["web_rpc_methods"]}},
                         nlohmann::json{{"web_rpc_version", "2"}, {"web_rpc_methods", current["web_rpc_methods"]}},
                         nlohmann::json{{"web_rpc_version", 2}, {"web_rpc_methods", "doc_open"}},
                         nlohmann::json{{"web_rpc_version", 2}, {"web_rpc_methods", nlohmann::json::array({12})}}}) {
        check(!engine::webCellProtocolReady(invalid) &&
                  !engine::webCellMethodAllowed(invalid, "doc_open"),
              "missing unknown older or malformed cell hello disables CAD and AI");
        check(engine::webCellMethodAllowed(invalid, "hello") &&
                  engine::webCellMethodAllowed(invalid, "ping"),
              "hello and ping remain available during protocol refusal");
    }
}

}  // namespace

int main() {
    testOnlineNormalRoute();
    testRunAndSessionIds();
    testOnlineTurnBudget();
    testComposerModePreserved();
    testOnlineMissionRoute();
    testAdminResultMapping();
    testEventConsumption();
    testConcurrentCellEventIsolation();
    testRetiredCorporateActivityRows();
    testCompactionTerminalSettlement();
    testLateStopCompactionReceipt();
    testWorkspaceRelativePaths();
    testCellHandlesAndPaths();
    testCellPendingStop();
    testCellProtocolAdmission();
    if (failures) return 1;
    std::cout << "agent job routing: PASS\n";
    return 0;
}
