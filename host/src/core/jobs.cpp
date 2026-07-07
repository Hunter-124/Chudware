// AI-CAD worker job launcher (extracted from gui_app.cpp).
#include <cstdio>
#include <mutex>
#include <string>
#include <thread>

#include <nlohmann/json.hpp>

#include "jobs.h"

#include "app_state.h"


#include "../worker_client.h"
// --- C22b: agent_run_v2 launcher ---
// Runs agent_run_v2 on a background thread. Orchestrator events arrive via the EventFn
// callback with kind = "run_started" / "stage_started" / etc.  We push them into
// job.events with an "orch_" prefix (kind field) so the main thread drain can distinguish
// them from legacy iteration events and route them to the ActivityPanel.
void launchAgentRunV2(WorkerClient& w, JobState& job,
                      const std::string& request, const std::string& docId, bool cam,
                      const nlohmann::json& extra) {
    if (job.running.exchange(true)) return;
    job.ready = false;
    job.kind = JOB_DESIGN;
    {
        std::lock_guard<std::mutex> lk(job.mtx);
        job.error.clear(); job.stl.clear(); job.info.clear();
        job.errorIsCad = false;
        job.converged = true; job.stopReason.clear();
        job.isMission = false; job.missionVerdict.clear();
    }
    { std::lock_guard<std::mutex> lk(job.evMtx); job.events.clear(); }

    std::thread([&w, &job, request, docId, cam, extra]() {
        nlohmann::json result;
        std::string err;

        // The onEvent callback is called on the worker thread for each streamed event.
        // We package it as an AgentEvent with kind = "orch_<original_kind>" so the GL thread
        // drain (in app.cpp) can identify and route it to the ActivityPanel.
        WorkerClient::EventFn onEvent = [&job](const nlohmann::json& d) {
            AgentEvent e;
            // The worker emits events.md §3 kinds; we prefix them "orch_" for routing.
            std::string origKind = jstr(d, "kind");
            e.kind = "orch_" + origKind;
            // Carry the full event data in the "text" field as JSON for the panel to parse.
            e.text = d.dump();
            // Also expose a human-readable phase hint in notes.
            std::string stage = jstr(d, "stage");
            std::string role  = jstr(d, "role");
            if (origKind == "stage_started")
                e.notes = stage + " > " + role + " attempt " +
                          std::to_string(jint(d, "attempt") + 1) + "...";
            else if (origKind == "run_finished")
                e.notes = "run " + jstr(d, "status");
            std::lock_guard<std::mutex> lk(job.evMtx);
            job.events.push_back(std::move(e));
        };

        // options.cam (true when launched from the Manufacture workspace) lets the core's
        // CAM fast-path treat a generic "make a program" as a CAM request on the current part.
        nlohmann::json params = {{"request", request}, {"doc_id", docId},
                                 {"options", {{"cam", cam}}}};
        // O1/M23: merge the GUI's extra fields (provider/model/reasoning_effort override,
        // attachments) as top-level params. Core keys above always win.
        if (extra.is_object())
            for (auto it = extra.begin(); it != extra.end(); ++it)
                if (!params.contains(it.key())) params[it.key()] = it.value();
        bool ok = w.call("agent_run_v2", params, result, err, 900000, onEvent);
        std::lock_guard<std::mutex> lk(job.mtx);
        if (!ok) {
            job.error = err;
            job.errorIsCad = false;
        } else {
            // agent_run_v2 returns {status, stop_reason, run_id, revision}.
            // The doc was rebuilt by the orchestrator; the DocMirror listener will be
            // called next time the GL thread calls doc_tree (or the existing listener path
            // fires via the worker's rebuild). The simplest correct path: on job ready,
            // the GL thread calls docMirror.open() or resync (C22b: see app.cpp drain).
            job.converged = (result.value("status", "") == "done");
            job.stopReason = result.value("stop_reason", "");
            // WS3 true-assembly: the run created a SEPARATE assembly document saved to its own
            // .step+ (it did not mutate the active doc). Carry the absolute path so the GL-thread
            // drain opens it as a new tab (via requestOpenPath). Only on a clean assembly.
            if (result.value("created_doc", false) && result.value("status", "") == "done")
                job.createdDocPath = result.value("asm_path", std::string());
            // W17: the run answered a drawing request by building a 2D drawing (its own dwg-cpp
            // id). The drain adopts it as a Drawing tab; the active part doc was untouched.
            if (result.value("created_drawing", false) && result.value("status", "") == "done") {
                job.drawingDocId = result.value("drawing_doc_id", std::string());
                job.drawingName = result.value("drawing_name", std::string());
                job.drawingSourceId = result.value("source_doc_id", std::string());
            }
            // CAM fast-path: the run built a machining job from the current part (its own cam
            // doc id). The drain loads it into the Manufacture workspace; the part was untouched.
            if (result.value("created_cam", false) && result.value("status", "") == "done")
                job.camDocId = result.value("cam_doc_id", std::string());
        }
        job.ready = true;
        job.running = false;
    }).detach();
}

// --- Mission orchestrator launcher (see jobs.h) ---
// A near-clone of launchAgentRunV2: same background thread + "orch_"-prefixed event streaming,
// but calls agent_mission_v1 and captures the verdict + any assembly tab to open. The mission
// emits mission_* events (in addition to each sub-run's stage events) — the drain renders them.
void launchMissionRun(WorkerClient& w, JobState& job,
                      const std::string& request, const std::string& docId,
                      const nlohmann::json& extra) {
    if (job.running.exchange(true)) return;
    job.ready = false;
    job.kind = JOB_DESIGN;
    {
        std::lock_guard<std::mutex> lk(job.mtx);
        job.error.clear(); job.stl.clear(); job.info.clear();
        job.errorIsCad = false;
        job.converged = true; job.stopReason.clear();
        job.createdDocPath.clear();
        job.isMission = true; job.missionVerdict.clear();
    }
    { std::lock_guard<std::mutex> lk(job.evMtx); job.events.clear(); }

    std::thread([&w, &job, request, docId, extra]() {
        nlohmann::json result;
        std::string err;

        WorkerClient::EventFn onEvent = [&job](const nlohmann::json& d) {
            AgentEvent e;
            std::string origKind = jstr(d, "kind");
            e.kind = "orch_" + origKind;
            e.text = d.dump();  // full event JSON for the panel/timeline to parse
            std::string stage = jstr(d, "stage");
            std::string role  = jstr(d, "role");
            if (origKind == "stage_started")
                e.notes = stage + " > " + role + " attempt " +
                          std::to_string(jint(d, "attempt") + 1) + "...";
            else if (origKind == "mission_task_started")
                e.notes = jstr(d, "title");
            else if (origKind == "run_finished" || origKind == "mission_finished")
                e.notes = "run " + jstr(d, "status");
            std::lock_guard<std::mutex> lk(job.evMtx);
            job.events.push_back(std::move(e));
        };

        nlohmann::json params = {{"request", request}, {"doc_id", docId}};
        if (extra.is_object())
            for (auto it = extra.begin(); it != extra.end(); ++it)
                if (!params.contains(it.key())) params[it.key()] = it.value();
        bool ok = w.call("agent_mission_v1", params, result, err, 900000, onEvent);
        std::lock_guard<std::mutex> lk(job.mtx);
        if (!ok) {
            job.error = err;
            job.errorIsCad = false;
        } else {
            // agent_mission_v1 returns {status, stop_reason, doc_id, verdict, tasks, created_doc?,
            // asm_path?}. The verdict is the assistant's final reply; createdDocPath opens a tab.
            job.converged = (result.value("status", "") == "done");
            job.stopReason = result.value("stop_reason", "");
            job.missionVerdict = result.value("verdict", std::string());
            if (result.value("created_doc", false))
                job.createdDocPath = result.value("asm_path", std::string());
        }
        job.ready = true;
        job.running = false;
    }).detach();
}
