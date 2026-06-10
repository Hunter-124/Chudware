// SimClient (C19) — see sim_client.h. Mirrors jobs.cpp: background thread per job, sim_progress
// events drained on the main thread; lazy spawn + restart-on-crash of the second worker process.
#include "sim_client.h"

#include <cstdio>
#include <thread>

#include "app_state.h"  // jstr/jint/jnum/jbool safe JSON accessors

namespace core {

bool SimClient::ensureStarted(std::string& consoleNote) {
    std::lock_guard<std::mutex> lk(startMtx_);
    if (worker_.started()) return true;
    std::string err;
    if (worker_.start(err, "engine.sim.worker")) {
        consoleNote = "[sim] simulation worker started";
        return true;
    }
    consoleNote = "[sim] could not start simulation worker: " + err;
    return false;
}

void SimClient::cancel() {
    if (!worker_.started() || curJobId_.empty()) return;
    std::string jobId = curJobId_;
    // Note: the doc/sim WorkerClient serializes calls on callMtx_, so sim_cancel will not be
    // delivered until the in-flight call returns. The Python solver also polls a cancel flag set
    // by sim_cancel; for long CFD runs the worker checks it between steps. Best-effort.
    std::thread([this, jobId]() {
        nlohmann::json r;
        std::string e;
        worker_.call("sim_cancel", {{"job_id", jobId}}, r, e, 10000);
    }).detach();
}

bool SimClient::launch(const std::string& method, nlohmann::json params,
                       std::string& consoleNote) {
    if (job_.running.exchange(true)) {
        consoleNote = "[sim] a study is already running";
        return false;
    }
    if (!ensureStarted(consoleNote)) {
        job_.running = false;
        return false;
    }

    // Reset job slots.
    {
        std::lock_guard<std::mutex> lk(job_.mtx);
        job_.method = method;
        job_.ok = false;
        job_.result = nlohmann::json::object();
        job_.error.clear();
        job_.errorCode.clear();
    }
    { std::lock_guard<std::mutex> lk(job_.evMtx); job_.events.clear(); }
    job_.ready = false;

    // Stamp a job_id so sim_progress / sim_cancel correlate. Reuse the same id everywhere the
    // worker reads it (study.job_id for FEA, top-level job_id for flow/mesh).
    static int counter = 0;
    curJobId_ = "job" + std::to_string(++counter);
    if (params.contains("study") && params["study"].is_object())
        params["study"]["job_id"] = curJobId_;
    else
        params["job_id"] = curJobId_;

    std::thread([this, method, params]() {
        WorkerClient::EventFn onEvent = [this](const nlohmann::json& d) {
            // Only sim_progress carries the fields we care about; tolerate unknown kinds.
            SimProgress ev;
            ev.stage = jstr(d, "stage");
            ev.pct = jint(d, "pct");
            ev.text = jstr(d, "text");
            if (auto it = d.find("trace_point"); it != d.end() && it->is_object()) {
                ev.hasTrace = true;
                ev.traceStep = jint(*it, "step");
                ev.traceDrag = jnum(*it, "drag");
            }
            std::lock_guard<std::mutex> lk(job_.evMtx);
            job_.events.push_back(std::move(ev));
        };

        nlohmann::json result;
        std::string err;
        // No host-side timeout for sim (cancel is explicit) — use a very large cap (worker_rpc §4).
        bool ok = worker_.call(method, params, result, err, 3'600'000, onEvent);

        std::lock_guard<std::mutex> lk(job_.mtx);
        if (!ok) {
            // Transport / worker death. Mark not-started so the next launch respawns it.
            job_.ok = false;
            job_.error = err.empty() ? "simulation worker failed" : err;
            worker_.stop();
        } else if (result.is_object() && result.contains("code")) {
            // Structured error envelope delivered in `result` (worker_client.cpp contract).
            job_.ok = false;
            job_.errorCode = result.value("code", std::string());
            job_.error = result.value("message", std::string("simulation error"));
        } else {
            job_.ok = true;
            job_.result = std::move(result);
        }
        job_.ready = true;
        job_.running = false;
    }).detach();

    consoleNote = "[sim] launched " + method;
    return true;
}

}  // namespace core
