// Chudware worker job launcher (extracted from gui_app.cpp).
#include <cstdio>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

#include <nlohmann/json.hpp>

#include "jobs.h"

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

// --- C22b: agent_run_v2 launcher ---
// Runs agent_run_v2 on a background thread. Orchestrator events arrive via the EventFn
// callback with kind = "run_started" / "stage_started" / etc.  We push them into
// job->events with an "orch_" prefix (kind field) so the main thread drain can distinguish
// them from legacy iteration events and route them to the Agent Workflow DAG store.
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

    // Capture the shared_ptr BY VALUE (not &job) so the JobState outlives this run even if
    // AppState::runs rehashes the map from another launch on the GL thread meanwhile.
    std::thread([&w, job, request, docId, cam, extra]() {
        nlohmann::json result;
        std::string err;
        try {

        // The onEvent callback is called on the worker thread for each streamed event.
        // We package it as an AgentEvent with kind = "orch_<original_kind>" so the GL thread
        // drain (in app.cpp) can identify and route it to the Agent Workflow DAG store.
        WorkerClient::EventFn onEvent = [&job](const nlohmann::json& d) {
            AgentEvent e;
            // The worker emits events.md §3 kinds; we prefix them "orch_" for routing.
            std::string origKind = jstr(d, "kind");
            e.kind = "orch_" + origKind;
            // Carry the full event data in the "text" field as JSON for the panel to parse.
            e.text = d.dump();
            // Capture run_id early so Stop can agent_cancel before the RPC returns.
            {
                std::string rid = jstr(d, "run_id");
                if (!rid.empty()) {
                    std::lock_guard<std::mutex> jlk(job->mtx);
                    if (job->runId.empty()) job->runId = std::move(rid);
                }
            }
            // Also expose a human-readable phase hint in notes.
            std::string stage = jstr(d, "stage");
            std::string role  = jstr(d, "role");
            if (origKind == "stage_started")
                e.notes = stage + " > " + role + " attempt " +
                          std::to_string(jint(d, "attempt") + 1) + "...";
            else if (origKind == "run_finished") {
                e.notes = "run " + jstr(d, "status");
                // Early-stop fields on the stream (provider_error etc.) — stash onto the
                // job so the GL drain can fill the chat card even if the RPC result shape
                // is thin ({ok:false,error} without stop_reason).
                std::string sr = jstr(d, "stop_reason");
                std::string ed = jstr(d, "error_detail");
                if (ed.empty()) ed = jstr(d, "error");
                // error_human is the actionable one-liner, NOT a stand-in for the raw detail —
                // keep both so the card can headline the guidance and still show the body.
                const std::string eh = jstr(d, "error_human");
                if (ed.empty()) ed = eh;
                if (!sr.empty() || !ed.empty() || !eh.empty()) {
                    std::lock_guard<std::mutex> jlk(job->mtx);
                    if (!sr.empty() && job->stopReason.empty()) job->stopReason = sr;
                    if (!ed.empty() && job->errorDetail.empty()) job->errorDetail = ed;
                    if (!eh.empty() && job->errorHuman.empty()) job->errorHuman = eh;
                }
            } else if (origKind == "provider_call") {
                // Compact live status: "xai · grok-4.5 HTTP 200 412ms"
                std::string prov = jstr(d, "provider");
                std::string model = jstr(d, "model");
                int http = jint(d, "http_status");
                int lat = jint(d, "latency_ms");
                char buf[160];
                if (http > 0)
                    std::snprintf(buf, sizeof(buf), "%s · %s HTTP %d %dms",
                                  prov.c_str(), model.c_str(), http, lat);
                else
                    std::snprintf(buf, sizeof(buf), "%s · %s transport %dms",
                                  prov.c_str(), model.c_str(), lat);
                e.notes = buf;
            }
            std::lock_guard<std::mutex> lk(job->evMtx);
            job->events.push_back(std::move(e));
        };

        // (T-RF4: subscription token refresh moved INTO the C++ core —
        // agent/auth.cpp refreshes at resolve_provider time; no Python RPC here.)

        // options.cam (true when launched from the Manufacture workspace) lets the core's
        // CAM fast-path treat a generic "make a program" as a CAM request on the current part.
        // Mode overhaul: merge extra["options"] (mode/plan_only/approved_plan/execute_as/
        // corporate_force/scope/advisor) under params["options"] so host routing stamps reach core.
        nlohmann::json opts = nlohmann::json::object();
        opts["cam"] = cam;
        opts["advisor"] = true;  // default-on; extra may override
        if (extra.is_object() && extra.contains("options") && extra["options"].is_object()) {
            for (auto it = extra["options"].begin(); it != extra["options"].end(); ++it)
                opts[it.key()] = it.value();
        }
        nlohmann::json params = {{"request", request}, {"doc_id", docId},
                                 {"options", std::move(opts)}};
        // O1/M23: merge the GUI's extra fields (provider/model/reasoning_effort override,
        // attachments) as top-level params. Core keys above always win. Skip "options" —
        // already folded into params["options"].
        if (extra.is_object())
            for (auto it = extra.begin(); it != extra.end(); ++it)
                if (it.key() != "options" && !params.contains(it.key()))
                    params[it.key()] = it.value();
        bool ok = w.call("agent_run_v2", params, result, err, 900000, onEvent);
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
                if (w.callAgentCancel(orphanId, cr, ce) && cr.value("cancelled", false))
                    orphanCancelled = true;
            }
        }
        std::lock_guard<std::mutex> lk(job->mtx);
        if (!ok) {
            // Hard transport/DLL failure — no result payload.
            job->error = err;
            job->errorIsCad = false;
            job->converged = false;
            if (job->stopReason.empty()) job->stopReason = "provider_error";
            if (job->errorDetail.empty()) job->errorDetail = err;
            if (orphanCancelled)
                job->errorDetail += " (orphaned run auto-cancelled after transport loss)";
        } else {
            // agent_run_v2 returns {status, stop_reason, run_id, revision[, error_detail]}
            // OR a pre-run refusal {ok:false, error} (provider resolution / offline).
            // Without mapping the latter, stopReason stays empty → app.cpp "unknown reason".
            const bool preRunFail = result.contains("ok") && result["ok"].is_boolean() &&
                                    !result["ok"].get<bool>();
            if (preRunFail) {
                std::string detail = result.value("error_detail", std::string());
                if (detail.empty()) detail = result.value("error", std::string());
                if (detail.empty()) detail = err.empty() ? "agent_run_v2 failed" : err;
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

// --- Mission orchestrator launcher (see jobs.h) ---
// A near-clone of launchAgentRunV2: same background thread + "orch_"-prefixed event streaming,
// but calls agent_mission_v1 and captures the verdict + any assembly tab to open. The mission
// emits mission_* events (in addition to each sub-run's stage events) — the drain renders them.
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

    // Capture the shared_ptr BY VALUE (not &job) — see launchAgentRunV2 above.
    std::thread([&w, job, request, docId, extra]() {
        nlohmann::json result;
        std::string err;
        try {

        // (T-RF4: subscription token refresh moved INTO the C++ core —
        // agent/auth.cpp refreshes at resolve_provider time; no Python RPC here.)
        WorkerClient::EventFn onEvent = [&job](const nlohmann::json& d) {
            AgentEvent e;
            std::string origKind = jstr(d, "kind");
            e.kind = "orch_" + origKind;
            e.text = d.dump();  // full event JSON for the panel/timeline to parse
            {
                std::string rid = jstr(d, "run_id");
                if (!rid.empty()) {
                    std::lock_guard<std::mutex> jlk(job->mtx);
                    if (job->runId.empty()) job->runId = std::move(rid);
                }
            }
            std::string stage = jstr(d, "stage");
            std::string role  = jstr(d, "role");
            if (origKind == "stage_started")
                e.notes = stage + " > " + role + " attempt " +
                          std::to_string(jint(d, "attempt") + 1) + "...";
            else if (origKind == "mission_task_started")
                e.notes = jstr(d, "title");
            else if (origKind == "run_finished" || origKind == "mission_finished") {
                e.notes = "run " + jstr(d, "status");
                std::string sr = jstr(d, "stop_reason");
                std::string ed = jstr(d, "error_detail");
                if (ed.empty()) ed = jstr(d, "error");
                if (!sr.empty() || !ed.empty()) {
                    std::lock_guard<std::mutex> jlk(job->mtx);
                    if (!sr.empty() && job->stopReason.empty()) job->stopReason = sr;
                    if (!ed.empty() && job->errorDetail.empty()) job->errorDetail = ed;
                }
            } else if (origKind == "swarm_wave") {
                int wave = jint(d, "wave");
                int total = jint(d, "total_waves");
                char buf[64];
                if (total > 0) std::snprintf(buf, sizeof(buf), "Wave %d/%d", wave, total);
                else std::snprintf(buf, sizeof(buf), "Wave %d", wave);
                e.notes = buf;
            } else if (origKind == "firm_bids") {
                e.notes = "Collecting firm bids...";
            } else if (origKind == "firm_award") {
                std::string firm = jstr(d, "firm");
                e.notes = firm.empty() ? "Firm awarded" : ("Awarded " + firm);
            } else if (origKind == "firm_team") {
                e.notes = "Assembling firm team...";
            } else if (origKind == "advisor") {
                std::string stg = jstr(d, "stage");
                e.notes = stg.empty() ? "Advisor" : ("Advisor \xc2\xb7 " + stg);
            } else if (origKind == "sub_run") {
                std::string purpose = jstr(d, "purpose");
                std::string stt = jstr(d, "status");
                if (!purpose.empty() && !stt.empty()) e.notes = purpose + " (" + stt + ")";
                else if (!purpose.empty()) e.notes = purpose;
                else e.notes = "Sub-run";
            }
            // firm_comm: frequent — leave notes empty (card aggregates).
            std::lock_guard<std::mutex> lk(job->evMtx);
            job->events.push_back(std::move(e));
        };

        nlohmann::json params = {{"request", request}, {"doc_id", docId}};
        // Mode overhaul: stamp mode/corporate_force/… under options (Swarm/mission path).
        nlohmann::json opts = nlohmann::json::object();
        opts["advisor"] = true;
        if (extra.is_object() && extra.contains("options") && extra["options"].is_object()) {
            for (auto it = extra["options"].begin(); it != extra["options"].end(); ++it)
                opts[it.key()] = it.value();
        }
        params["options"] = std::move(opts);
        if (extra.is_object())
            for (auto it = extra.begin(); it != extra.end(); ++it)
                if (it.key() != "options" && !params.contains(it.key()))
                    params[it.key()] = it.value();
        bool ok = w.call("agent_mission_v1", params, result, err, 900000, onEvent);
        std::lock_guard<std::mutex> lk(job->mtx);
        if (!ok) {
            job->error = err;
            job->errorIsCad = false;
            job->converged = false;
            if (job->stopReason.empty()) job->stopReason = "provider_error";
            if (job->errorDetail.empty()) job->errorDetail = err;
        } else {
            // agent_mission_v1 returns {status, stop_reason, doc_id, verdict, tasks, ...}
            // or {ok:false, error} on provider resolution failure.
            // User-facing plan gate ONLY for awaiting_plan_approval / needs_user_decision /
            // plan_only / status plan_ready — agent-decided plans advisor-approve in-core.
            const bool preRunFail = result.contains("ok") && result["ok"].is_boolean() &&
                                    !result["ok"].get<bool>();
            if (preRunFail) {
                std::string detail = result.value("error_detail", std::string());
                if (detail.empty()) detail = result.value("error", std::string());
                if (detail.empty()) detail = err.empty() ? "agent_mission_v1 failed" : err;
                job->error = detail;
                job->errorDetail = detail;
                job->errorIsCad = false;
                job->converged = false;
                job->stopReason = result.value("stop_reason", std::string("provider_error"));
            } else {
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
                                              sr == "budget_exceeded" || sr.empty());
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
