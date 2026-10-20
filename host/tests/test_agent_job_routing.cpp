#include "core/app_state.h"
#include "core/jobs.h"
#include "core/fs_backend.h"
#include "worker_client.h"
#include "engine_backend.h"

#include <iostream>
#include <memory>
#include <string>

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
    const nlohmann::json actor = {{"kind", "user"}, {"id", "acct-7"}};
    const nlohmann::json extra = {
        {"provider", "xai"},
        {"model", "grok-4.5"},
        {"reasoning_effort", "high"},
        {"attachments", attachments},
        {"context_refs", nlohmann::json::array({{{"kind", "face"}, {"id", "Face12"}}})},
        {"_chudware_actor", actor},
        {"options",
         {{"session_id", "session-17"},
          {"sessions_dir", "/tmp/sessions"},
          {"workspace_dir", "/tmp/project"},
          {"history", history},
          {"advisor", false},
          {"corporate_mode", true},
          {"scope", {{"feature_ids", nlohmann::json::array({"Pad"})}}}}}};

    const AgentJobRpcRoute route =
        buildAgentJobRpcRoute("make the bracket", "doc-4", "conv-fallback", true, extra, false);
    check(route.online, "normal route is online");
    check(route.method == "agent_admin_turn", "normal online route uses agent_admin_turn");
    check(route.params.value("request", std::string()) == "make the bracket", "request preserved");
    check(route.params.value("doc_id", std::string()) == "doc-4", "doc id preserved");
    check(route.params.value("provider", std::string()) == "xai", "provider flattened");
    check(route.params.value("model", std::string()) == "grok-4.5", "model flattened");
    check(route.params.value("reasoning_effort", std::string()) == "high", "effort flattened");
    check(route.params.value("mode", std::string()) == "agent", "normal mode is agent");
    check(route.params.value("cam", false), "CAM option flattened");
    check(route.params.value("session_id", std::string()) == "session-17", "session id flattened");
    check(route.params.value("sessions_dir", std::string()) == "/tmp/sessions",
          "sessions directory flattened");
    check(route.params.value("workspace_dir", std::string()) == "/tmp/project",
          "workspace directory flattened");
    check(route.params["history"] == history, "history flattened without request injection");
    check(route.params["attachments"] == attachments, "attachments preserved");
    check(route.params["_chudware_actor"] == actor, "actor preserved");
    check(route.params.contains("context_refs"), "context refs preserved");
    check(route.params["options"].value("corporate_mode", false),
          "normal corporate preference preserved");
    check(!route.params.value("corporate_force", false), "normal route does not force company");
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

    const AgentJobRpcRoute offline = buildAgentJobRpcRoute(
        "local build", "doc", "conv", false, {{"options", {{"offline", true}}}}, false);
    check(!offline.params.contains("timeout"), "offline native route gets no worker budget");
    check(agentJobWaitMs(offline) > 0, "offline native route keeps its bounded wait");
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
         {{"mode", "eng_firm"},
          {"org", "eng_firm"},
          {"corporate_force", false},
          {"company_budget", 9000},
          {"history", nlohmann::json::array({{{"role", "user"}, {"text", "design it"}}})},
          {"session_id", "mission-session"}}}};
    const AgentJobRpcRoute route =
        buildAgentJobRpcRoute("build and verify", "doc-m", "conv-m", false, extra, true);
    check(route.online, "mission route is online");
    check(route.method == "agent_admin_turn", "mission online route uses agent_admin_turn");
    check(route.params.value("mode", std::string()) == "swarm", "mission mode forced to swarm");
    check(route.params.value("corporate_force", false), "mission corporate force enabled");
    check(route.params["options"].value("mode", std::string()) == "swarm",
          "mission options mode matches top level");
    check(route.params["options"].value("corporate_force", false),
          "mission options corporate force matches top level");
    check(route.params.value("org", std::string()) == "eng_firm", "firm organization preserved");
    check(route.params.value("company_budget", 0) == 9000, "firm budget preserved");
    check(route.params.value("provider", std::string()) == "anthropic",
          "mission provider preserved");
    check(route.params.contains("history") && route.params.contains("attachments"),
          "mission history and attachments preserved");
}

void testExplicitOfflineFallbacks() {
    const AgentJobRpcRoute normal = buildAgentJobRpcRoute(
        "local build", "doc", "conv", false, {{"options", {{"offline", true}}}}, false);
    check(!normal.online, "explicit offline normal is not online");
    check(normal.method == "agent_run_v2", "explicit offline normal retains native method");

    const AgentJobRpcRoute mission = buildAgentJobRpcRoute(
        "local mission", "doc", "conv", false, {{"offline_mode", true}}, true);
    check(!mission.online, "explicit offline mission is not online");
    check(mission.method == "agent_mission_v1", "explicit offline mission retains native method");

    const AgentJobRpcRoute localProvider = buildAgentJobRpcRoute(
        "local endpoint", "doc", "conv", false, {{"provider", "llamacpp"}}, false);
    check(localProvider.online && localProvider.method == "agent_admin_turn",
          "local provider alone is not an explicit offline request");
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

    const nlohmann::json failed =
        normalizeAgentAdminTurnResult({{"ok", false}, {"reason", "authentication failed"}}, false);
    check(failed.value("error", std::string()) == "authentication failed",
          "admin reason maps to legacy error");
    check(failed.value("error_detail", std::string()) == "authentication failed",
          "admin reason maps to error detail");
    check(failed.value("stop_reason", std::string()) == "provider_error",
          "admin failure maps to provider error stop");
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
        check(nlohmann::json::parse(job->events.front().text).value("run_id", std::string()) ==
                  "event-run",
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
                     {"chudcode_session_file", "/srv/actor/session.jsonl"},
                     {"workspace_dir", "./parts"}}}};
    auto route = buildAgentJobRpcRoute("go", "doc", "local-conversation", false, extra, false, true);
    check(route.cell && route.error.empty() && !route.params.contains("run_id") &&
              !route.params.contains("session_id"), "cell route never mints client identifiers");
    check(route.params["workspace_dir"] == "parts" &&
              route.params["options"]["workspace_dir"] == "parts",
          "cell workspace selection remains relative in both envelopes");
    check(!route.params.contains("sessions_dir") &&
              !route.params["options"].contains("chudcode_session_file"),
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
          "rewind handle replacement retains its credential provider");
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
    const auto offline = buildAgentJobRpcRoute("go", "doc", "chat", false,
                                               {{"offline", true}}, false, true);
    check(!offline.error.empty(), "cell has no native offline fallback");
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
             "chudcode_models_list", "agent_admin_turn"})}};
    check(engine::webCellProtocolReady(current), "version two cell enables browser capabilities");
    check(engine::webCellMethodAllowed(current, "doc_open") &&
              engine::webCellMethodAllowed(current, "agent_handles_new"),
          "only advertised CAD and handle methods are admitted");
    check(!engine::webCellMethodAllowed(current, "doc_rebuild"),
          "unadvertised method is refused without legacy fallback");
    check(engine::webCellMethodAllowed(current, "subscribe") &&
              engine::webCellMethodAllowed(current, "doc_list") &&
              engine::webCellMethodAllowed(current, "chudcode_models_list") &&
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
    testExplicitOfflineFallbacks();
    testAdminResultMapping();
    testEventConsumption();
    testWorkspaceRelativePaths();
    testCellHandlesAndPaths();
    testCellPendingStop();
    testCellProtocolAdmission();
    if (failures) return 1;
    std::cout << "agent job routing: PASS\n";
    return 0;
}
