// Chudware worker job launcher: spawns design/mission jobs on a background thread and streams
// agent progress events back to the main thread.
#pragma once

#include <memory>
#include <string>

#include <nlohmann/json.hpp>

class WorkerClient;
struct JobState;

// Online launches call the long-lived services worker's `agent_admin_turn`. Explicit offline
// launches alone retain the native core methods. Both paths stream orchestrator events
// (run_started / stage_started / agent_call / agent_output / stage_result / run_finished)
// through job->events with kind prefixed "orch_" so the main-thread drain can route them to
// the Agent Workflow DAG store + the conversation's inline activity timeline.
//
// `job` is the run slot BOUND to `convId` (see AppState::runFor/activeRun) — JobState is
// non-copyable, so it is always passed/held by shared_ptr; the detached worker thread captures
// it by value so the JobState outlives the run even if AppState::runs rehashes. Refuses (no-op)
// only if THIS job is already running — never a global lock, so N conversations can each have
// their own run in flight. `convId`/`docKey` are stamped onto `job` at launch so every streamed
// event and the final result route back to the owning conversation, never by scanning whichever
// conversation happens to be on screen.
// `cam` (set when the copilot is launched from the Manufacture workspace) routes generic
// "make a program"-style requests to the current part's CAM tools.
// `extra` carries the model picker's provider/model/reasoning_effort override, attachments,
// actor fields, and an `options` object. Online routing preserves that object and flattens its
// worker-facing fields (history/session/workspace/mode/context) into `agent_admin_turn`.
struct AgentJobRpcRoute {
    std::string method;
    nlohmann::json params = nlohmann::json::object();
    bool online = true;
    bool cell = false;
    std::string error;
};

#ifdef __EMSCRIPTEN__
constexpr bool kBrowserCellClient = true;
#else
constexpr bool kBrowserCellClient = false;
#endif
AgentJobRpcRoute buildAgentJobRpcRoute(const std::string& request, const std::string& docId,
                                      const std::string& convId, bool cam,
                                      const nlohmann::json& extra, bool mission,
                                      bool cell = kBrowserCellClient);
// Local conversation IDs never identify server sessions. These bindings contain
// only opaque cell-issued handles and are persisted beside local chat metadata.
std::string cellAgentSessionHandle(const std::string& convId);
std::string cellAgentSessionProvider(const std::string& convId);
void bindCellAgentSessionHandle(const std::string& convId, const std::string& handle,
                                const std::string& provider = {});
void clearCellAgentSessionHandles();
bool applyCellAgentHandles(AgentJobRpcRoute& route, const nlohmann::json& reply,
                           std::string& err);
// Web Stop is remembered even before mint completes. Publishing a handle
// returns that exact handle when its own job has a pending cancellation.
std::string requestCellAgentStop(JobState& job);
std::string publishCellAgentRunHandle(JobState& job, const std::string& handle);
// Client-side wait for a launched route (WorkerClient::call timeoutMs). An online turn with
// an explicit positive `timeout` budget waits that budget plus slack; one without a budget
// waits for its response (-1: ends on the terminal reply, Stop, worker/daemon loss or app
// teardown, never on a wall clock). Explicit offline core runs keep their fixed bound.
int agentJobWaitMs(const AgentJobRpcRoute& route);
nlohmann::json normalizeAgentAdminTurnResult(const nlohmann::json& result, bool mission);
void consumeAgentJobEvent(const std::shared_ptr<JobState>& job, const nlohmann::json& data,
                          bool mission);
void launchAgentRunV2(WorkerClient& w, std::shared_ptr<JobState> job,
                      const std::string& convId, const std::string& docKey,
                      const std::string& request, const std::string& docId, bool cam = false,
                      const nlohmann::json& extra = nlohmann::json::object());

// Mission launcher. Online missions use the same `agent_admin_turn` entry as normal runs, with
// mode=swarm and corporate_force=true; explicit offline missions retain the native
// `agent_mission_v1` path. Mission-specific verdict/task fields are still stashed onto
// JobState for the existing main-thread drain.
void launchMissionRun(WorkerClient& w, std::shared_ptr<JobState> job,
                      const std::string& convId, const std::string& docKey,
                      const std::string& request, const std::string& docId,
                      const nlohmann::json& extra = nlohmann::json::object());
