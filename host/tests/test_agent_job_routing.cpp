#include "core/app_state.h"
#include "core/jobs.h"

#include <iostream>
#include <memory>
#include <string>

#include <nlohmann/json.hpp>

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

}  // namespace

int main() {
    testOnlineNormalRoute();
    testRunAndSessionIds();
    testOnlineMissionRoute();
    testExplicitOfflineFallbacks();
    testAdminResultMapping();
    testEventConsumption();
    if (failures) return 1;
    std::cout << "agent job routing: PASS\n";
    return 0;
}
