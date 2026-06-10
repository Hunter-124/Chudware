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
