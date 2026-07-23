// SimClient (C19) — see sim_client.h. Mirrors jobs.cpp: background thread per job, sim_progress
// events drained on the main thread; lazy spawn + restart-on-crash of the second worker process.
#include "sim_client.h"

#include <cstdio>
#include <thread>

#include "app_state.h"  // jstr/jint/jnum/jbool safe JSON accessors
#include "cpp_core.h"   // in-process C++ core (libchudware) — the sim backend when no Python worker
#include "engine_backend.h"  // daemonMode — WASM/desktop hostd path (no child sim worker)

namespace core {

bool SimClient::ensureStarted(std::string& consoleNote) {
    std::lock_guard<std::mutex> lk(startMtx_);
    if (worker_.started()) return true;

    // Daemon / WASM: there is no Python sim child to spawn. FEA methods
    // (sim_mesh / sim_run_static / sim_run_modal / sim_run_fatigue / sim_run_thermal)
    // route via routeSimToCpp → engine::call BEFORE WorkerClient's running_ check, so a
    // synthetic started flag is enough for launch()/caps() gates. Mirrors
    // AppState::tryStartWorker's web branch.
    if (engine::daemonMode()) {
        worker_.markStarted();
        consoleNote = "[sim] daemon mode: FEA routes via hostd (no local sim worker)";
        return true;
    }

    std::string err;
    // No Python sim worker exists anymore (T-RF19: FEA + flow + cancel all route
    // in-process to libchudware via routeSimToCpp; engine.sim.worker is deleted). Treat
    // the backend as ready if the core loads. sim_caps / multiphysics ride the
    // services-side worker (services.worker_main).
    std::string lerr;
    if (core::cpp_core().ensureLoaded(lerr)) {
        // Mark started so launch()'s worker_.call path can reach routeSimToCpp (and so
        // started() reports ready). running_ stays meaningless for a real pipe — call()
        // still routes FEA in-process before the pipe write.
        worker_.markStarted();
        consoleNote = "[sim] using in-process C++ core (libchudware) for simulation";
        return true;
    }
    consoleNote = "[sim] could not load the simulation backend: " + lerr;
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

nlohmann::json SimClient::caps() {
    if (capsQueried_.load()) {
        std::lock_guard<std::mutex> lk(capsMtx_);
        return capsCache_;
    }
    // Don't poke the worker mid-job (the WorkerClient serializes calls; a long solve would block
    // the UI thread). Leave capsQueried_ false so a later idle frame retries.
    if (job_.running.load()) return nlohmann::json::object();
    std::string note;
    if (!ensureStarted(note)) return nlohmann::json::object();
    nlohmann::json r;
    std::string e;
    bool ok = worker_.call("sim_caps", nlohmann::json::object(), r, e, 15000);
    std::lock_guard<std::mutex> lk(capsMtx_);
    if (ok && r.is_object() && !r.contains("code")) capsCache_ = r;
    capsQueried_ = true;
    return capsCache_;
}

std::string SimClient::stage() const {
    std::lock_guard<std::mutex> lk(progMtx_);
    return progStage_;
}

std::string SimClient::recentLog() const {
    std::lock_guard<std::mutex> lk(progMtx_);
    return progLog_;
}

float SimClient::elapsedSeconds() const {
    long long launch = launchNs_.load();
    if (launch == 0LL) return 0.0f;
    long long nowNs = static_cast<long long>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count());
    return static_cast<float>(static_cast<double>(nowNs - launch) * 1e-9);
}

bool SimClient::launch(const std::string& method, nlohmann::json params,
                       std::string& consoleNote, const std::string& ownerDocId) {
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
        job_.ownerDocId = ownerDocId;
        job_.ok = false;
        job_.result = nlohmann::json::object();
        job_.error.clear();
        job_.errorCode.clear();
    }
    { std::lock_guard<std::mutex> lk(job_.evMtx); job_.events.clear(); }
    job_.ready = false;

    // Reset live progress for the incoming job so the status bar shows "Starting" immediately.
    {
        std::lock_guard<std::mutex> lk(progMtx_);
        progStage_ = "Starting";
        progLog_.clear();
    }
    launchNs_.store(static_cast<long long>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count()));

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
            // Update the live progress display (read before ev is moved into the queue).
            {
                std::lock_guard<std::mutex> lk(progMtx_);
                if (!ev.stage.empty()) progStage_ = ev.stage;
                if (!ev.text.empty())  progLog_   = ev.text;
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
        launchNs_.store(0LL);
        { std::lock_guard<std::mutex> lk(progMtx_); progStage_.clear(); progLog_.clear(); }
    }).detach();

    consoleNote = "[sim] launched " + method;
    return true;
}

}  // namespace core
