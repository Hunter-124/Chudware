// AI-CAD worker job launcher (extracted from gui_app.cpp).
#include <cstdio>
#include <mutex>
#include <string>
#include <thread>

#include <nlohmann/json.hpp>

#include "jobs.h"

#include "app_state.h"

#include "../worker_client.h"

// Spawn a worker job on a background thread. Design/chat jobs stream live iteration events
// into job.events (drained on the main thread); `reset` starts a fresh agent session and
// `plan` lets the agent draft a build plan before writing code (plan mode).
void launchJob(WorkerClient& w, JobState& job, int kind, std::string payload, bool reset,
               bool plan) {
    if (job.running.exchange(true)) return;
    job.ready = false;
    job.kind = kind;
    {
        std::lock_guard<std::mutex> lk(job.mtx);
        job.error.clear(); job.stl.clear(); job.info.clear();
        job.errorIsCad = false;
        job.isAssembly = false; job.parts.clear(); job.fit = FitReport{};
        job.converged = true; job.stopReason.clear();
        job.isMission = false; job.missionVerdict.clear();
    }
    { std::lock_guard<std::mutex> lk(job.evMtx); job.events.clear(); }
    std::thread([&w, &job, kind, payload, reset, plan]() {
        nlohmann::json result;
        std::string err;
        bool design = (kind != JOB_BUILD);

        WorkerClient::EventFn onEvent = [&job](const nlohmann::json& d) {
            AgentEvent e;
            e.kind = jstr(d, "kind");
            e.index = jint(d, "index");
            e.ok = jbool(d, "ok");
            e.done = jbool(d, "done");
            e.phase = jstr(d, "phase");
            e.text = jstr(d, "text");
            e.notes = jstr(d, "notes");
            e.thinking = jstr(d, "thinking");
            e.code = jstr(d, "code");
            e.diff = jstr(d, "diff");
            e.dims = jstr(d, "dims");
            e.error = jstr(d, "error");
            e.contactSheet = jstr(d, "contact_sheet");
            e.stlPath = jstr(d, "stl_path");
            e.stdoutText = jstr(d, "stdout");
            e.isAssembly = jbool(d, "is_assembly");
            if (auto it = d.find("parts"); it != d.end()) e.parts = parseParts(*it);
            if (auto it = d.find("fit"); it != d.end()) e.fit = parseFit(*it);
            if (auto it = d.find("plan"); it != d.end()) e.plan = parsePlan(*it);
            std::lock_guard<std::mutex> lk(job.evMtx);
            job.events.push_back(std::move(e));
        };

        bool ok = design
            ? w.call("agent_design",
                     {{"request", payload}, {"name", "gui_design"},
                      {"session_id", "gui_design"}, {"reset", reset}, {"plan", plan}},
                     result, err, 900000, onEvent)
            : w.call("run_model", {{"code", payload}, {"name", "gui_build"}},
                     result, err, 120000);
        std::lock_guard<std::mutex> lk(job.mtx);
        if (!ok) {
            job.error = err;            // transport / worker / provider failure -> system error
            job.errorIsCad = false;
        } else {
            nlohmann::json mr = design
                ? (result.contains("result") && result["result"].is_object()
                       ? result["result"] : nlohmann::json::object())
                : result;
            if (!mr.value("ok", false)) {
                // The worker ran fine but the CAD script didn't build — a modeling
                // problem (model- or user-authored code), not an app failure.
                job.error = mr.value("error", "The CAD code didn't produce a solid.");
                job.errorIsCad = true;
            } else {
                job.stl = mr.value("stl_path", "");
                char buf[256] = "";
                auto bb = mr.value("bbox", nlohmann::json::object());
                if (bb.contains("size")) {
                    auto sz = bb["size"];
                    std::snprintf(buf, sizeof(buf), "%.1f x %.1f x %.1f mm  |  %.0f mm\xc2\xb3",
                                  sz[0].get<double>(), sz[1].get<double>(), sz[2].get<double>(),
                                  mr.value("volume", 0.0));
                }
                job.info = buf;
                if (design) {
                    job.info += "  |  " + std::to_string(result.value("iterations", 0)) + " iters";
                    job.converged = result.value("converged", true);
                    job.stopReason = result.value("stop_reason", "");
                }
                // Multi-part assembly: capture per-part list + fit report for the parts
                // panel and the fit readout (single parts leave these empty).
                job.isAssembly = mr.value("is_assembly", false);
                if (job.isAssembly) {
                    job.parts = parseParts(mr.value("parts", nlohmann::json::array()));
                    job.fit = parseFit(mr.value("fit", nlohmann::json::object()));
                }
            }
        }
        job.ready = true;
        job.running = false;
    }).detach();
}

// --- C22b: agent_run_v2 launcher ---
// Runs agent_run_v2 on a background thread. Orchestrator events arrive via the EventFn
// callback with kind = "run_started" / "stage_started" / etc.  We push them into
// job.events with an "orch_" prefix (kind field) so the main thread drain can distinguish
// them from legacy iteration events and route them to the ActivityPanel.
void launchAgentRunV2(WorkerClient& w, JobState& job,
                      const std::string& request, const std::string& docId) {
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

    std::thread([&w, &job, request, docId]() {
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

        bool ok = w.call("agent_run_v2",
                         {{"request", request}, {"doc_id", docId}},
                         result, err, 900000, onEvent);
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
                      const std::string& request, const std::string& docId) {
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

    std::thread([&w, &job, request, docId]() {
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

        bool ok = w.call("agent_mission_v1",
                         {{"request", request}, {"doc_id", docId}},
                         result, err, 900000, onEvent);
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
