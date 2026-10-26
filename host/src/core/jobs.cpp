// Chudware worker job launcher (extracted from gui_app.cpp).
#include <atomic>
#include <chrono>
#include <cstdio>
#include <memory>
#include <mutex>
#include <cmath>
#include <string>
#include <thread>

#include <nlohmann/json.hpp>

#include "jobs.h"
#include "fs_backend.h"
#include <map>

#include "app_state.h"


#include "../worker_client.h"

// Crash guard for the detached agent-job threads below: an exception escaping a
// detached std::thread calls std::terminate -> SIGABRT and kills the whole app
// (the classic case is an nlohmann type_error on a malformed run result, e.g.
// result.value("error", ...) when the field is null/an object). Land the job in
// a normal error state instead so the chat drain paints a failure card.
static void failJobFromEscapedException(const std::shared_ptr<JobState>& job, const std::string& what) {
    std::lock_guard<std::mutex> lk(job->mtx);
    job->error = "agent job error: " + what;
    job->errorDetail = job->error;
    job->errorIsCad = false;
    job->converged = false;
    if (job->stopReason.empty()) job->stopReason = "provider_error";
    job->ready = true;
    job->running = false;
}

namespace {
std::mutex cellHandlesMutex;
struct CellSessionBinding { std::string handle, provider; };
std::map<std::string, CellSessionBinding> cellSessions;

std::string jsonString(const nlohmann::json& value, const char* key) {
    if (!value.is_object() || !value.contains(key) || !value[key].is_string()) return {};
    return value[key].get<std::string>();
}
bool opaqueHandle(const nlohmann::json& value) {
    return value.is_string() && !value.get_ref<const std::string&>().empty() &&
           value.get_ref<const std::string&>().size() <= 1024 &&
           value.get_ref<const std::string&>().find('\0') == std::string::npos;
}

bool mintCellAgentHandles(WorkerClient& worker, const std::shared_ptr<JobState>& job,
                          AgentJobRpcRoute& route, std::string& err) {
    if (!route.cell || !route.online) return true;
    nlohmann::json request = nlohmann::json::object(), reply;
    const auto session = route.params.find("session_id");
    if (session != route.params.end()) request["session_id"] = *session;
    if (!worker.call("agent_handles_new", request, reply, err, 30000) ||
        !applyCellAgentHandles(route, reply, err)) return false;
    bindCellAgentSessionHandle(job->convId, route.params["session_id"].get<std::string>(),
                              jsonString(route.params, "provider"));
    const auto pendingCancel = publishCellAgentRunHandle(
        *job, route.params["run_id"].get<std::string>());
    if (!pendingCancel.empty()) {
        nlohmann::json cancelled;
        if (!worker.callAgentCancel(pendingCancel, cancelled, err, jsonString(route.params, "provider"))) {
            if (err.empty()) err = "Cell refused pending run cancellation";
            return false;
        }
        std::lock_guard<std::mutex> lock(job->mtx);
        job->stopReason = "cancelled";
        job->converged = false;
        job->ready = true;
        job->running = false;
        err.clear();
        return false;
    }
    return true;
}

std::string nextAgentRunId() {
    static std::atomic<unsigned long long> sequence{0};
    const auto micros = std::chrono::duration_cast<std::chrono::microseconds>(
                            std::chrono::system_clock::now().time_since_epoch())
                            .count();
    return "gui-" + std::to_string(micros) + "-" +
           std::to_string(sequence.fetch_add(1, std::memory_order_relaxed));
}


// The online turn budget is the caller's: an explicit positive `timeout` (seconds) rides to
// agent_admin_turn and bounds chudcode_host's wait (abort + stop_reason "turn_budget" when it
// expires); the client wait is that budget plus slack so the worker's structured reply arrives
// first. An ordinary GUI turn sends NO budget and has no wall-clock stop: the old fixed
// 900 000 ms client deadline (2026-09-24), then the 7200 s default, both ended healthy turns
// mid-build. Stop, worker/daemon loss and app teardown still end an unbudgeted wait.
constexpr int kOnlineTurnSlackS = 120;
constexpr int kOfflineRunWaitMs = 900000;
// WorkerClient/engine_backend: a negative timeout waits for the response itself.
constexpr int kWaitForResponseMs = -1;

bool positiveTurnBudget(const nlohmann::json& value) {
    if (!value.is_number()) return false;
    const double seconds = value.get<double>();
    return std::isfinite(seconds) && seconds > 0;
}

// A client-side wait expiring is not a provider failure: name it for what it is.
std::string transportStopReason(const std::string& err) {
    return err.rfind("engine call timed out", 0) == 0 ? "client_timeout" : "provider_error";
}

}  // namespace
bool agentJobExplicitOffline(const nlohmann::json& extra, const nlohmann::json& options) {
    auto enabled = [](const nlohmann::json& value, const char* key) {
        return value.is_object() && value.contains(key) && value[key].is_boolean() &&
               value[key].get<bool>();
    };
    if (enabled(extra, "offline") || enabled(extra, "offline_mode") ||
        enabled(options, "offline") || enabled(options, "offline_mode"))
        return true;
    const auto modeIsOffline = [](const nlohmann::json& value) {
        return value.is_object() && value.contains("mode") && value["mode"].is_string() &&
               value["mode"].get<std::string>() == "offline";
    };
    return modeIsOffline(extra) || modeIsOffline(options);
}

std::string cellAgentSessionHandle(const std::string& convId) {
    std::lock_guard<std::mutex> lock(cellHandlesMutex);
    const auto it = cellSessions.find(convId);
    return it == cellSessions.end() ? std::string() : it->second.handle;
}

std::string cellAgentSessionProvider(const std::string& convId) {
    std::lock_guard<std::mutex> lock(cellHandlesMutex);
    const auto it = cellSessions.find(convId);
    return it == cellSessions.end() ? std::string() : it->second.provider;
}

void bindCellAgentSessionHandle(const std::string& convId, const std::string& handle,
                                const std::string& provider) {
    std::lock_guard<std::mutex> lock(cellHandlesMutex);
    if (handle.empty()) cellSessions.erase(convId);
    else if (handle.size() <= 1024 && handle.find('\0') == std::string::npos) {
        auto& binding = cellSessions[convId];
        binding.handle = handle;
        if (!provider.empty()) binding.provider = provider;
    }
}

void clearCellAgentSessionHandles() {
    std::lock_guard<std::mutex> lock(cellHandlesMutex);
    cellSessions.clear();
}

bool applyCellAgentHandles(AgentJobRpcRoute& route, const nlohmann::json& reply,
                           std::string& err) {
    err.clear();
    if (!reply.is_object() || !reply.contains("ok") || reply["ok"] != true ||
        !reply.contains("run_id") || !opaqueHandle(reply["run_id"]) ||
        !reply.contains("session_id") || !opaqueHandle(reply["session_id"])) {
        err = "Cell did not issue agent handles";
        return false;
    }
    route.params["run_id"] = reply["run_id"];
    route.params["session_id"] = reply["session_id"];
    route.params["options"]["session_id"] = reply["session_id"];
    return true;
}
std::string requestCellAgentStop(JobState& job) {
    std::lock_guard<std::mutex> lock(job.mtx);
    if (job.stopRequested.exchange(true)) return {};
    return job.runId;
}

std::string publishCellAgentRunHandle(JobState& job, const std::string& handle) {
    std::lock_guard<std::mutex> lock(job.mtx);
    job.runId = handle;
    return job.stopRequested.load() ? handle : std::string();
}

int agentJobWaitMs(const AgentJobRpcRoute& route) {
    if (!route.online) return kOfflineRunWaitMs;
    const auto it = route.params.find("timeout");
    if (it == route.params.end() || !positiveTurnBudget(*it)) return kWaitForResponseMs;
    const double ms = (it->get<double>() + kOnlineTurnSlackS) * 1000.0;
    return ms >= 2147483647.0 ? 2147483647 : (int)ms;
}

AgentJobRpcRoute buildAgentJobRpcRoute(const std::string& request, const std::string& docId,
                                      const std::string& convId, bool cam,
                                      const nlohmann::json& extra, bool mission, bool cell) {
    nlohmann::json options = nlohmann::json::object();
    options["advisor"] = true;
    if (!mission) options["cam"] = cam;
    if (extra.is_object() && extra.contains("options") && extra["options"].is_object()) {
        for (auto it = extra["options"].begin(); it != extra["options"].end(); ++it)
            options[it.key()] = it.value();
    }

    AgentJobRpcRoute route;
    route.cell = cell;
    route.online = !agentJobExplicitOffline(extra, options);
    route.method = route.online ? "agent_admin_turn"
                                : (mission ? "agent_mission_v1" : "agent_run_v2");
    route.params = {{"request", request}, {"doc_id", docId}, {"options", options}};
    if (extra.is_object()) {
        for (auto it = extra.begin(); it != extra.end(); ++it) {
            if (it.key() != "options" && !route.params.contains(it.key()))
                route.params[it.key()] = it.value();
        }
    }
    if (cell && !route.online) {
        route.error = "Offline agent execution is unavailable in browser cells";
        return route;
    }
    if (!route.online) {
        // The native core agent reads only the request text: the picked context rides inline.
        // (Online, the worker resolves context_refs against the engine and renders them itself.)
        if (extra.is_object() && extra.contains("context_refs")) {
            const std::string ctx =
                core::contextRefsPromptText(core::contextRefsFromJson(extra["context_refs"]));
            if (!ctx.empty()) route.params["request"] = request + "\n\n" + ctx;
        }
        return route;
    }

    // agent_admin_turn consumes these values at the top level. Keep the options object too:
    // host-tool context and the explicit offline core fallback still use its full shape.
    for (auto it = options.begin(); it != options.end(); ++it)
        if (!route.params.contains(it.key())) route.params[it.key()] = it.value();
    // The composer's user-picked mode (options.mode: "agent"|"plan"|"ask") is authoritative —
    // never re-derived from the request text. A mission run still forces the swarm lane.
    const std::string optionMode = jsonString(options, "mode");
    route.params["mode"] = mission ? std::string("swarm")
                                   : (optionMode.empty() ? std::string("agent") : optionMode);
    route.params["options"]["mode"] = route.params["mode"];
    if (mission) {
        route.params["corporate_force"] = true;
        route.params["options"]["corporate_force"] = true;
    }

    if (cell) {
        // Discard local conversation/run IDs and IDBFS/runtime paths. A resumed
        // session can only come from the binding created by a cell reply.
        for (const char* key : {"run_id", "session_id", "sessions_dir", "chudcode_session_file"}) {
            route.params.erase(key);
            route.params["options"].erase(key);
        }
        const std::string session = cellAgentSessionHandle(convId);
        if (!session.empty()) route.params["session_id"] = session;
        for (auto* object : {&route.params, &route.params["options"]}) {
            auto workspace = object->find("workspace_dir");
            if (workspace != object->end()) {
                std::string relative;
                if (!workspace->is_string() ||
                    !chudware::workspaceRelativePath(workspace->get<std::string>(),
                                                     relative, route.error)) {
                    if (route.error.empty()) route.error = "Invalid workspace-relative path";
                    return route;
                }
                *workspace = relative.empty() ? "." : relative;
            }
        }
        for (auto* object : {&route.params, &route.params["options"]}) {
            auto attachments = object->find("attachments");
            if (attachments == object->end() || !attachments->is_array()) continue;
            for (auto& attachment : *attachments) {
                if (!attachment.is_object()) {
                    route.error = "Invalid attachment";
                    return route;
                }
                auto path = attachment.find("path");
                if (path == attachment.end()) continue;
                std::string relative;
                if (!path->is_string() ||
                    !chudware::workspaceRelativePath(path->get<std::string>(),
                                                     relative, route.error)) {
                    if (route.error.empty()) route.error = "Invalid workspace-relative path";
                    return route;
                }
                *path = relative;
            }
        }
    } else {
        std::string runId = jsonString(route.params, "run_id");
        if (runId.empty()) runId = nextAgentRunId();
        route.params["run_id"] = runId;
        std::string sessionId = jsonString(route.params, "session_id");
        if (sessionId.empty()) sessionId = convId;
        if (sessionId.empty()) sessionId = runId;
        route.params["session_id"] = sessionId;
    }
    // No default budget: an absent/zero/invalid `timeout` means the turn has no wall-clock
    // stop, so never forward a non-budget value the worker would have to second-guess.
    const auto timeout = route.params.find("timeout");
    if (timeout != route.params.end() && !positiveTurnBudget(*timeout))
        route.params.erase(timeout);
    return route;
}

nlohmann::json normalizeAgentAdminTurnResult(const nlohmann::json& result, bool mission) {
    if (!result.is_object()) return result;
    nlohmann::json mapped = result;
    const bool accepted = result.contains("ok") && result["ok"].is_boolean() &&
                          result["ok"].get<bool>();
    if (!accepted) {
        const std::string reason = result.value("reason", std::string());
        if (!reason.empty()) {
            mapped["error"] = reason;
            mapped["error_detail"] = reason;
        }
        if (!mapped.contains("stop_reason")) mapped["stop_reason"] = "provider_error";
        return mapped;
    }
    if (!result.contains("turn") || !result["turn"].is_object()) return mapped;

    const nlohmann::json& turn = result["turn"];
    const bool cancelled = turn.value("cancelled", false) ||
                           turn.value("stop_reason", std::string()) == "cancelled";
    const bool success = turn.value("success", !cancelled);
    mapped["status"] = cancelled ? "cancelled" : (success ? "done" : "failed");
    mapped["stop_reason"] =
        turn.value("stop_reason", std::string(success ? "completed" : "provider_error"));
    const std::string runId = turn.value("run_id", std::string());
    if (!runId.empty()) mapped["run_id"] = runId;
    const std::string text = turn.value("result", std::string());
    if (mission) {
        mapped["verdict"] = text;
    } else {
        mapped["lane"] = "general";
        mapped["summary"] = text;
    }
    return mapped;
}

void consumeAgentJobEvent(const std::shared_ptr<JobState>& job, const nlohmann::json& data,
                          bool mission) {
    AgentEvent event;
    const std::string kind = jstr(data, "kind");
    event.kind = "orch_" + kind;
    event.text = data.dump();
    {
        std::string runId = jstr(data, "run_id");
        if (!runId.empty()) {
            std::lock_guard<std::mutex> lock(job->mtx);
            if (job->runId.empty()) job->runId = std::move(runId);
        }
    }

    const std::string stage = jstr(data, "stage");
    const std::string role = jstr(data, "role");
    if (kind == "stage_started") {
        event.notes = stage + " > " + role + " attempt " +
                      std::to_string(jint(data, "attempt") + 1) + "...";
    } else if (mission && kind == "mission_task_started") {
        event.notes = jstr(data, "title");
    } else if (kind == "run_finished" || (mission && kind == "mission_finished")) {
        event.notes = "run " + jstr(data, "status");
        std::string stopReason = jstr(data, "stop_reason");
        std::string detail = jstr(data, "error_detail");
        if (detail.empty()) detail = jstr(data, "error");
        const std::string human = jstr(data, "error_human");
        if (detail.empty()) detail = human;
        if (!stopReason.empty() || !detail.empty() || !human.empty()) {
            std::lock_guard<std::mutex> lock(job->mtx);
            if (!stopReason.empty() && job->stopReason.empty())
                job->stopReason = std::move(stopReason);
            if (!detail.empty() && job->errorDetail.empty()) job->errorDetail = std::move(detail);
            if (!human.empty() && job->errorHuman.empty()) job->errorHuman = human;
        }
    } else if (!mission && kind == "provider_call") {
        const std::string provider = jstr(data, "provider");
        const std::string model = jstr(data, "model");
        const int http = jint(data, "http_status");
        const int latency = jint(data, "latency_ms");
        char buffer[160];
        if (http > 0)
            std::snprintf(buffer, sizeof(buffer), "%s · %s HTTP %d %dms", provider.c_str(),
                          model.c_str(), http, latency);
        else
            std::snprintf(buffer, sizeof(buffer), "%s · %s transport %dms", provider.c_str(),
                          model.c_str(), latency);
        event.notes = buffer;
    } else if (mission && kind == "swarm_wave") {
        const int wave = jint(data, "wave");
        const int total = jint(data, "total_waves");
        char buffer[64];
        if (total > 0)
            std::snprintf(buffer, sizeof(buffer), "Wave %d/%d", wave, total);
        else
            std::snprintf(buffer, sizeof(buffer), "Wave %d", wave);
        event.notes = buffer;
    } else if (mission && kind == "firm_bids") {
        event.notes = "Collecting firm bids...";
    } else if (mission && kind == "firm_award") {
        const std::string firm = jstr(data, "firm");
        event.notes = firm.empty() ? "Firm awarded" : ("Awarded " + firm);
    } else if (mission && kind == "firm_team") {
        event.notes = "Assembling firm team...";
    } else if (mission && kind == "advisor") {
        event.notes = stage.empty() ? "Advisor" : ("Advisor \xc2\xb7 " + stage);
    } else if (mission && kind == "sub_run") {
        const std::string purpose = jstr(data, "purpose");
        const std::string status = jstr(data, "status");
        if (!purpose.empty() && !status.empty())
            event.notes = purpose + " (" + status + ")";
        else if (!purpose.empty())
            event.notes = purpose;
        else
            event.notes = "Sub-run";
    }

    std::lock_guard<std::mutex> lock(job->evMtx);
    job->events.push_back(std::move(event));
}

// Online agent launcher. `buildAgentJobRpcRoute` selects the long-lived worker entry or the
// explicit offline core fallback; both keep the established streamed-event and JobState drain.
void launchAgentRunV2(WorkerClient& w, std::shared_ptr<JobState> job,
                      const std::string& convId, const std::string& docKey,
                      const std::string& request, const std::string& docId, bool cam,
                      const nlohmann::json& extra) {
    if (job->running.exchange(true)) return;
    job->ready = false;
    job->kind = JOB_DESIGN;
    {
        std::lock_guard<std::mutex> lk(job->mtx);
        job->error.clear(); job->stl.clear(); job->info.clear();
        job->errorIsCad = false;
        job->converged = true; job->stopReason.clear(); job->errorDetail.clear();
        job->errorHuman.clear();
        job->isMission = false; job->missionVerdict.clear();
        job->isCompaction = extra.is_object() && extra.contains("compact") &&
                            extra["compact"].is_object();
        job->compactionTurn = nullptr;
        job->generalLane = false; job->agentSummary.clear();
        job->missionPartial = false;
        job->missionTasksCompleted = 0;
        job->missionTasksFailed = 0;
        job->missionTasksTotal = 0;
        job->planReady = false; job->planReadyJson.clear();
        job->createdDocPath.clear();
        job->drawingDocId.clear(); job->drawingName.clear(); job->drawingSourceId.clear();
        job->camDocId.clear();
        job->partName.clear();
        job->savedPath.clear();
        job->finalRevision = 0;
        job->runId.clear();
        job->convId = convId;
        job->docKey = docKey;
    }
    { std::lock_guard<std::mutex> lk(job->evMtx); job->events.clear(); }
    AgentJobRpcRoute route =
        buildAgentJobRpcRoute(request, docId, convId, cam, extra, /*mission=*/false);
    if (!route.error.empty()) {
        failJobFromEscapedException(job, route.error);
        return;
    }
    if (route.online) {
        std::lock_guard<std::mutex> lock(job->mtx);
        job->runId = route.params.value("run_id", std::string());
#ifdef __EMSCRIPTEN__
        job->provider = jsonString(route.params, "provider");
#endif
    }

    // Capture the shared_ptr BY VALUE (not &job) so the JobState outlives this run even if
    // AppState::runs rehashes the map from another launch on the GL thread meanwhile.
    std::thread([&w, job, route = std::move(route)]() mutable {
        nlohmann::json result;
        std::string err;
        try {
        if (!mintCellAgentHandles(w, job, route, err)) {
            if (!err.empty()) failJobFromEscapedException(job, err);
            return;
        }

        WorkerClient::EventFn onEvent = [&job](const nlohmann::json& data) {
            consumeAgentJobEvent(job, data, /*mission=*/false);
        };
        const bool ok =
            w.call(route.method, route.params, result, err, agentJobWaitMs(route), onEvent);
        if (ok && route.online) result = normalizeAgentAdminTurnResult(result, /*mission=*/false);
        // Transport loss (timeout/closed) orphans the daemon-side run: hostd keeps
        // executing — and billing — with no client attached. A user resubmit then
        // burns quota twice. Best-effort auto-cancel the orphan BEFORE taking job->mtx:
        // the side-socket connect can block seconds against a sick daemon, and the GL
        // thread takes job->mtx every frame.
        bool orphanCancelled = false;
        if (!ok) {
            std::string orphanId;
            { std::lock_guard<std::mutex> lk(job->mtx); orphanId = job->runId; }
            if (!orphanId.empty()) {
                nlohmann::json cr;
                std::string ce;
                if (w.callAgentCancel(orphanId, cr, ce, route.cell ? jsonString(route.params, "provider") : "") &&
                    cr.value("cancelled", false))
                    orphanCancelled = true;
            }
        }
        std::lock_guard<std::mutex> lk(job->mtx);
        if (!ok) {
            // Hard transport/DLL failure — no result payload.
            job->error = err;
            job->errorIsCad = false;
            job->converged = false;
            if (job->stopReason.empty()) job->stopReason = transportStopReason(err);
            if (job->errorDetail.empty()) job->errorDetail = err;
            if (orphanCancelled)
                job->errorDetail += " (orphaned run auto-cancelled after transport loss)";
        } else if (job->isCompaction) {
            // Maintenance has no answer prose, plan gate, or geometry result to interpret.
            if (result.contains("turn") && result["turn"].is_object())
                job->compactionTurn = std::move(result["turn"]);
            job->converged = jstr(result, "status") == "done";
            job->stopReason = jstr(result, "stop_reason");
            if (!job->converged && job->stopReason != "cancelled") {
                job->errorDetail = jstr(result, "error_detail");
                if (job->errorDetail.empty()) job->errorDetail = jstr(result, "error");
                if (job->errorDetail.empty()) job->errorDetail = jstr(result, "reason");
                if (job->errorDetail.empty())
                    job->errorDetail = job->stopReason.empty()
                                           ? "Compaction failed" : job->stopReason;
                job->error = job->errorDetail;
            }
        } else {
            // Online agent_admin_turn results are normalized above to the legacy JobState shape;
            // explicit offline core results already use it. Both may return {ok:false,error}.
            const bool preRunFail = result.contains("ok") && result["ok"].is_boolean() &&
                                    !result["ok"].get<bool>();
            if (preRunFail) {
                std::string detail = result.value("error_detail", std::string());
                if (detail.empty()) detail = result.value("error", std::string());
                if (detail.empty()) detail = err.empty() ? (route.method + " failed") : err;
                job->error = detail;
                job->errorDetail = detail;
                job->errorIsCad = false;
                job->converged = false;
                job->stopReason = result.value("stop_reason", std::string("provider_error"));
            } else {
                // Always remember run_id from the result (backup if events lagged).
                {
                    std::string rid = result.value("run_id", std::string());
                    if (!rid.empty() && job->runId.empty()) job->runId = std::move(rid);
                }
                const std::string status = result.value("status", std::string());
                std::string sr = result.value("stop_reason", std::string());
                // User cancelled — soft success for drain discard path (stopRequested).
                if (status == "cancelled" || sr == "cancelled") {
                    job->converged = false;
                    job->stopReason = "cancelled";
                    // Leave error empty so the drain doesn't paint a failure card if
                    // stopRequested was cleared race-y; requestStopRun already finalized UI.
                } else {
                // User-facing gate ONLY for manual Plan / advisor escalate — not agent-decided
                // plans (those execute after in-core advisor approval and return status done).
                const bool isPlanReady =
                    (status == "plan_ready") ||
                    (sr == "awaiting_plan_approval") ||
                    (sr == "needs_user_decision") ||
                    (sr == "plan_only");
                if (isPlanReady) {
                    job->planReady = true;
                    job->converged = true;
                    if (sr.empty()) sr = "awaiting_plan_approval";
                    job->stopReason = sr;
                    nlohmann::json stash = nlohmann::json::object();
                    if (result.contains("plan")) stash["plan"] = result["plan"];
                    if (result.contains("tasks")) stash["tasks"] = result["tasks"];
                    if (result.contains("summary") && result["summary"].is_string())
                        stash["summary"] = result["summary"];
                    if (result.contains("execution_style") && result["execution_style"].is_string())
                        stash["execution_style"] = result["execution_style"];
                    else if (result.contains("structure") && result["structure"].is_string())
                        stash["execution_style"] = result["structure"];
                    std::string reason = result.value("reason", std::string());
                    if (reason.empty()) reason = result.value("verdict", std::string());
                    if (reason.empty()) reason = result.value("advisor_reason", std::string());
                    if (!reason.empty()) stash["reason"] = reason;
                    if (!stash.empty()) job->planReadyJson = stash.dump();
                } else {
                    job->converged = (status == "done");
                    if (!sr.empty()) job->stopReason = sr;
                    // General lane (no geometry pipeline): keep the turn's own
                    // handoff prose — the drain must not substitute a built-
                    // geometry claim for a run that touched no geometry.
                    if (result.value("lane", std::string()) == "general") {
                        job->generalLane = true;
                        job->agentSummary = result.value("summary", std::string());
                    }
                    std::string ed = result.value("error_detail", std::string());
                    if (ed.empty()) ed = result.value("error", std::string());
                    // error_human is the actionable one-liner, NOT a stand-in for the raw
                    // detail — keep both (headline + supporting body on the chat card).
                    const std::string eh = result.value("error_human", std::string());
                    if (ed.empty()) ed = eh;
                    if (!ed.empty()) job->errorDetail = ed;
                    if (!eh.empty()) job->errorHuman = eh;
                    // Failed run with detail but no hard error string — still surface it so
                    // the chat card prefers errorDetail over the generic early-stop prose.
                    if (!job->converged && job->error.empty() && !job->errorDetail.empty()) {
                        job->error = job->errorDetail;
                        job->errorIsCad = (job->stopReason == "failed" ||
                                          job->stopReason == "no_code" ||
                                          job->stopReason == "max_iters" ||
                                          job->stopReason == "model_failed" ||
                                          job->stopReason == "decompose_failed");
                    }
                    // WS3 true-assembly: the run created a SEPARATE assembly document saved to its own
                    // .step+ (it did not mutate the active doc). Carry the absolute path so the GL-thread
                    // drain opens it as a new tab (via requestOpenPath). Only on a clean assembly.
                    if (result.value("created_doc", false) && status == "done")
                        job->createdDocPath = result.value("asm_path", std::string());
                    // W17: the run answered a drawing request by building a 2D drawing (its own dwg-cpp
                    // id). The drain adopts it as a Drawing tab; the active part doc was untouched.
                    if (result.value("created_drawing", false) && status == "done") {
                        job->drawingDocId = result.value("drawing_doc_id", std::string());
                        job->drawingName = result.value("drawing_name", std::string());
                        job->drawingSourceId = result.value("source_doc_id", std::string());
                    }
                    // CAM fast-path: the run built a machining job from the current part (its own cam
                    // doc id). The drain loads it into the Manufacture workspace; the part was untouched.
                    if (result.value("created_cam", false) && status == "done")
                        job->camDocId = result.value("cam_doc_id", std::string());
                    // Part-naming stage: AI-chosen display name for the built part.
                    {
                        std::string pn = result.value("part_name", std::string());
                        if (!pn.empty()) job->partName = std::move(pn);
                    }
                    // File-first persistence: the core agent already saved the part itself
                    // (at modeling start + milestone re-saves + memory-stage rename). Carry
                    // its path so the GL drain saves onto it instead of writing a duplicate
                    // <name>_2.step+ next to the agent's file.
                    {
                        std::string sp = result.value("saved_path", std::string());
                        if (!sp.empty()) job->savedPath = std::move(sp);
                    }
                    // Final doc revision (0 when the run produced no geometry / never advanced).
                    // Host autosave gates on this so empty/no_geometry runs never write a file.
                    if (result.contains("revision") && result["revision"].is_number_integer())
                        job->finalRevision = result["revision"].get<int>();
                    else
                        job->finalRevision = result.value("revision", 0);
                }
                }  // end !cancelled
            }
        }
        job->ready = true;
        job->running = false;
        } catch (const std::exception& e) {
            failJobFromEscapedException(job, e.what());
        } catch (...) {
            failJobFromEscapedException(job, "unknown exception");
        }
    }).detach();
}

// Mission launcher: same online `agent_admin_turn` entry and event drain as a normal run, with
// swarm/corporate_force routing. Explicit offline missions retain the native mission method.
void launchMissionRun(WorkerClient& w, std::shared_ptr<JobState> job,
                      const std::string& convId, const std::string& docKey,
                      const std::string& request, const std::string& docId,
                      const nlohmann::json& extra) {
    if (job->running.exchange(true)) return;
    job->ready = false;
    job->kind = JOB_DESIGN;
    {
        std::lock_guard<std::mutex> lk(job->mtx);
        job->error.clear(); job->stl.clear(); job->info.clear();
        job->errorIsCad = false;
        job->converged = true; job->stopReason.clear(); job->errorDetail.clear();
        job->errorHuman.clear();
        job->createdDocPath.clear();
        job->isMission = true; job->missionVerdict.clear();
        job->isCompaction = false; job->compactionTurn = nullptr;
        job->generalLane = false; job->agentSummary.clear();
        job->missionPartial = false;
        job->missionTasksCompleted = 0;
        job->missionTasksFailed = 0;
        job->missionTasksTotal = 0;
        job->planReady = false; job->planReadyJson.clear();
        job->partName.clear();
        job->savedPath.clear();
        job->finalRevision = 0;
        job->runId.clear();
        job->convId = convId;
        job->docKey = docKey;
    }
    { std::lock_guard<std::mutex> lk(job->evMtx); job->events.clear(); }
    AgentJobRpcRoute route =
        buildAgentJobRpcRoute(request, docId, convId, /*cam=*/false, extra, /*mission=*/true);
    if (!route.error.empty()) {
        failJobFromEscapedException(job, route.error);
        return;
    }
    if (route.online) {
        std::lock_guard<std::mutex> lock(job->mtx);
        job->runId = route.params.value("run_id", std::string());
#ifdef __EMSCRIPTEN__
        job->provider = jsonString(route.params, "provider");
#endif
    }

    // Capture the shared_ptr BY VALUE (not &job) — see launchAgentRunV2 above.
    std::thread([&w, job, route = std::move(route)]() mutable {
        nlohmann::json result;
        std::string err;
        try {
        if (!mintCellAgentHandles(w, job, route, err)) {
            if (!err.empty()) failJobFromEscapedException(job, err);
            return;
        }

        WorkerClient::EventFn onEvent = [&job](const nlohmann::json& data) {
            consumeAgentJobEvent(job, data, /*mission=*/true);
        };
        const bool ok =
            w.call(route.method, route.params, result, err, agentJobWaitMs(route), onEvent);
        if (ok && route.online) result = normalizeAgentAdminTurnResult(result, /*mission=*/true);
        std::lock_guard<std::mutex> lk(job->mtx);
        if (!ok) {
            job->error = err;
            job->errorIsCad = false;
            job->converged = false;
            if (job->stopReason.empty()) job->stopReason = transportStopReason(err);
            if (job->errorDetail.empty()) job->errorDetail = err;
        } else {
            // Online agent_admin_turn results are normalized above to the legacy mission shape;
            // explicit offline core results already use it. Both may return {ok:false,error}.
            const bool preRunFail = result.contains("ok") && result["ok"].is_boolean() &&
                                    !result["ok"].get<bool>();
            if (preRunFail) {
                std::string detail = result.value("error_detail", std::string());
                if (detail.empty()) detail = result.value("error", std::string());
                if (detail.empty()) detail = err.empty() ? (route.method + " failed") : err;
                job->error = detail;
                job->errorDetail = detail;
                job->errorIsCad = false;
                job->converged = false;
                job->stopReason = result.value("stop_reason", std::string("provider_error"));
            } else {
                const std::string returnedRunId = result.value("run_id", std::string());
                if (!returnedRunId.empty()) job->runId = returnedRunId;
                const std::string status = result.value("status", std::string());
                std::string sr = result.value("stop_reason", std::string());
                const bool isPlanReady =
                    (status == "plan_ready") ||
                    (sr == "awaiting_plan_approval") ||
                    (sr == "needs_user_decision") ||
                    (sr == "plan_only");
                if (isPlanReady) {
                    job->planReady = true;
                    job->converged = true;  // gate, not a failure
                    if (sr.empty()) sr = "awaiting_plan_approval";
                    job->stopReason = sr;
                    nlohmann::json stash = nlohmann::json::object();
                    if (result.contains("plan")) stash["plan"] = result["plan"];
                    if (result.contains("tasks")) stash["tasks"] = result["tasks"];
                    if (result.contains("summary") && result["summary"].is_string())
                        stash["summary"] = result["summary"];
                    if (result.contains("execution_style") && result["execution_style"].is_string())
                        stash["execution_style"] = result["execution_style"];
                    else if (result.contains("structure") && result["structure"].is_string())
                        stash["execution_style"] = result["structure"];
                    std::string reason = result.value("reason", std::string());
                    if (reason.empty()) reason = result.value("verdict", std::string());
                    if (reason.empty()) reason = result.value("advisor_reason", std::string());
                    if (!reason.empty()) stash["reason"] = reason;
                    if (!stash.empty()) job->planReadyJson = stash.dump();
                    // Soft assistant text only; plan section + approve card own the UI.
                    std::string v = result.value("verdict", std::string());
                    if (v.empty()) v = result.value("summary", std::string());
                    if (v.empty()) v = reason;
                    job->missionVerdict = std::move(v);
                } else if (status == "partial") {
                    // Mixed outcomes: some tasks built, at least one failed. Keep geometry;
                    // do NOT set job->error (not a hard fail). Drain shows warning card.
                    job->missionPartial = true;
                    job->converged = false;
                    if (!sr.empty()) job->stopReason = sr;
                    // Prefer first_fail_reason; fall back to stop_reason / error fields.
                    std::string failReason = result.value("first_fail_reason", std::string());
                    if (failReason.empty()) failReason = sr;
                    if (failReason.empty()) failReason = result.value("error_detail", std::string());
                    if (failReason.empty()) failReason = result.value("error", std::string());
                    if (!failReason.empty()) job->errorDetail = failReason;
                    // Task counts — accept several field names defensively.
                    auto countField = [&](const char* a, const char* b) -> int {
                        if (result.contains(a) && result[a].is_number_integer())
                            return result[a].get<int>();
                        if (result.contains(a) && result[a].is_number())
                            return (int)result[a].get<double>();
                        if (b && result.contains(b) && result[b].is_number_integer())
                            return result[b].get<int>();
                        if (b && result.contains(b) && result[b].is_number())
                            return (int)result[b].get<double>();
                        return 0;
                    };
                    job->missionTasksCompleted = countField("tasks_completed", "completed");
                    job->missionTasksFailed = countField("tasks_failed", "failed");
                    int abandoned = countField("tasks_abandoned", "abandoned");
                    job->missionTasksTotal = job->missionTasksCompleted + job->missionTasksFailed +
                                           abandoned;
                    // Tally from tasks[] when counts missing.
                    if (job->missionTasksTotal == 0 && result.contains("tasks") &&
                        result["tasks"].is_array()) {
                        int done = 0, fail = 0, tot = 0;
                        for (const auto& t : result["tasks"]) {
                            if (!t.is_object()) continue;
                            ++tot;
                            std::string ts = t.value("status", std::string());
                            if (ts == "done" || ts == "ok" || ts == "pass") ++done;
                            else if (ts != "pending" && ts != "skipped" && ts != "abandoned")
                                ++fail;
                        }
                        job->missionTasksCompleted = done;
                        job->missionTasksFailed = fail;
                        job->missionTasksTotal = tot;
                    }
                    // Verdict line surfaces the failure reason (user-visible).
                    std::string v = result.value("verdict", std::string());
                    if (v.empty()) {
                        char buf[256];
                        int n = job->missionTasksCompleted;
                        int m = job->missionTasksTotal > 0
                                    ? job->missionTasksTotal
                                    : (n + job->missionTasksFailed);
                        if (m > 0 && !failReason.empty())
                            std::snprintf(buf, sizeof(buf),
                                          "Partially complete — %d/%d tasks built "
                                          "(first failure: %s)",
                                          n, m, failReason.c_str());
                        else if (m > 0)
                            std::snprintf(buf, sizeof(buf),
                                          "Partially complete — %d/%d tasks built", n, m);
                        else if (!failReason.empty())
                            std::snprintf(buf, sizeof(buf),
                                          "Partially complete (first failure: %s)",
                                          failReason.c_str());
                        else
                            std::snprintf(buf, sizeof(buf), "Partially complete");
                        v = buf;
                    } else if (!failReason.empty() &&
                               v.find(failReason) == std::string::npos) {
                        v += "\nFirst failure: ";
                        v += failReason;
                    }
                    job->missionVerdict = std::move(v);
                    // Keep any assembly/doc that was built.
                    if (result.value("created_doc", false))
                        job->createdDocPath = result.value("asm_path", std::string());
                } else {
                    job->converged = (status == "done");
                    if (!sr.empty()) job->stopReason = sr;
                    std::string ed = result.value("error_detail", std::string());
                    if (ed.empty()) ed = result.value("error", std::string());
                    const std::string eh = result.value("error_human", std::string());
                    if (ed.empty()) ed = eh;
                    if (!ed.empty()) job->errorDetail = ed;
                    if (!eh.empty()) job->errorHuman = eh;
                    // Full mission failure (status failed) — surface as error so the drain
                    // doesn't treat empty verdict as a soft early-stop.
                    if (status == "failed") {
                        job->converged = false;
                        if (job->error.empty()) {
                            std::string why = !ed.empty() ? ed : sr;
                            if (why.empty()) why = "mission_failed";
                            job->error = why;
                            job->errorDetail = why;
                            // Unit failures are CAD-shaped, not app crashes.
                            job->errorIsCad = (sr == "failed" || sr == "model_failed" ||
                                              sr == "no_geometry" || sr == "no_code" ||
                                              sr == "max_iters" || sr == "decompose_failed" ||
                                              sr.empty());
                        }
                    }
                    job->missionVerdict = result.value("verdict", std::string());
                    if (result.value("created_doc", false))
                        job->createdDocPath = result.value("asm_path", std::string());
                }
            }
        }
        job->ready = true;
        job->running = false;
        } catch (const std::exception& e) {
            failJobFromEscapedException(job, e.what());
        } catch (...) {
            failJobFromEscapedException(job, "unknown exception");
        }
    }).detach();
}
