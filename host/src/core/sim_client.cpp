// SimClient (C19) — see sim_client.h. Mirrors jobs.cpp: background thread per job, sim_progress
// events drained on the main thread; lazy spawn + restart-on-crash of the second worker process.
#include "sim_client.h"

#include <cstdio>
#include <thread>
#include <exception>
#include <utility>

#include "app_state.h"  // jstr/jint/jnum/jbool safe JSON accessors
#include "cpp_core.h"   // in-process C++ core (libchudware) — the sim backend when no Python worker
#include "engine_backend.h"  // daemonMode — WASM/desktop hostd path (no child sim worker)

namespace core {

SimClient::~SimClient() {
    stop();
}

bool SimClient::started() const {
    std::lock_guard<std::mutex> lk(startMtx_);
    return worker_.started();
}

void SimClient::stop() {
    if (stopping_.exchange(true)) return;

    std::lock_guard<std::mutex> op(operationMtx_);
    reservationToken_.store(0);
    reservationOwnerDocId_.clear();

    if (launchThread_.joinable()) {
        // A shared app worker may already have aborted the global daemon run connection during
        // teardown, making our local call return before hostd has unwound the solver. Preserve
        // the job id on transport failure and still send sim_cancel over the independent
        // short engine connection before joining.
        const bool solveMayStillRun = job_.running.load() || workerFailed_.load();
        if (solveMayStillRun) {
            startCancelLocked(true);
            if (cancelThread_.joinable()) cancelThread_.join();
            // Now close any daemon call still blocked in transport before joining its owner.
            worker_.abortPending();
        } else if (cancelThread_.joinable()) {
            cancelThread_.join();
        }

        // The transport abort wakes a daemon call immediately. The in-process core cannot be
        // force-interrupted, but the cooperative sim_cancel above remains observable at its
        // registered checkpoints. No member teardown starts until the owned operation returns.
        launchThread_.join();
    } else if (cancelThread_.joinable()) {
        // Defensive: launch normally owns the full lifetime window, but never leave a completed
        // cancellation thread joinable if a future call path changes that invariant.
        cancelThread_.join();
    }

    std::lock_guard<std::mutex> start(startMtx_);
    worker_.stop();
}

bool SimClient::tryReserve(const std::string& ownerDocId, std::uint64_t& token) {
    token = 0;
    if (ownerDocId.empty() || stopping_.load()) return false;

    std::lock_guard<std::mutex> op(operationMtx_);
    if (stopping_.load() || reservationToken_.load() != 0 ||
        cancelInFlight_.load() || job_.running.load() || job_.ready.load())
        return false;

    std::uint64_t next = nextReservationToken_++;
    if (next == 0) next = nextReservationToken_++;
    reservationOwnerDocId_ = ownerDocId;
    reservationToken_.store(next);
    token = next;
    return true;
}

void SimClient::releaseReservation(std::uint64_t token) {
    if (token == 0) return;
    std::lock_guard<std::mutex> op(operationMtx_);
    if (reservationToken_.load() != token) return;
    reservationToken_.store(0);
    reservationOwnerDocId_.clear();
}

bool SimClient::ensureStarted(std::string& consoleNote) {
    std::lock_guard<std::mutex> lk(startMtx_);
    if (stopping_.load()) {
        consoleNote = "[sim] the simulation backend is stopping";
        return false;
    }
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

void SimClient::startCancelLocked(bool allowSettled) {
    if (cancelThread_.joinable() && !cancelRequested_.load())
        cancelThread_.join();

    bool workerStarted = false;
    {
        std::lock_guard<std::mutex> start(startMtx_);
        workerStarted = worker_.started();
    }
    if ((!allowSettled && !job_.running.load()) || !workerStarted ||
        curJobId_.empty() || cancelRequested_.exchange(true))
        return;

    cancelInFlight_ = true;
    const std::string jobId = curJobId_;
    try {
        cancelThread_ = std::thread([this, jobId]() {
            bool accepted = false;
            try {
                nlohmann::json r;
                std::string e;
                // Simulation runs use engine_backend's dedicated long-run connection. The
                // shared short connection can therefore deliver cancellation without waiting
                // on WorkerClient::callMtx_, and reconnects even if another owner already
                // aborted the long transport during app teardown.
                const bool called =
                    engine::call("sim_cancel", {{"job_id", jobId}}, r, e, 10000);
                accepted =
                    called && r.is_object() && r.value("ok", false) &&
                    r.contains("cancelled") && r["cancelled"].is_boolean() &&
                    r["cancelled"].get<bool>();
            } catch (...) {
                accepted = false;
            }
            if (!accepted) cancelRequested_ = false;
            cancelInFlight_ = false;
        });
    } catch (const std::exception&) {
        cancelInFlight_ = false;
        cancelRequested_ = false;
    }
}

void SimClient::cancel() {
    if (stopping_.load()) return;
    std::lock_guard<std::mutex> op(operationMtx_);
    if (stopping_.load()) return;
    startCancelLocked();
}

nlohmann::json SimClient::caps() {
    if (capsQueried_.load()) {
        std::lock_guard<std::mutex> lk(capsMtx_);
        return capsCache_;
    }
    // The Python simulation child was retired. Every method launched by this client now routes
    // through routeSimToCpp, whose STL-shaped flow implementation is explicitly LBM-only.
    // Calling the old Python sim_caps here cannot work: ensureStarted() marks the synthetic C++
    // backend ready but deliberately creates no stdio pipe for non-routed methods. Report the
    // capability of the route we actually own instead of caching a failed probe forever.
    if (job_.running.load()) return nlohmann::json::object();
    std::string note;
    if (!ensureStarted(note)) return nlohmann::json::object();
    std::lock_guard<std::mutex> lk(capsMtx_);
    capsCache_ = {
        {"lbm", true},
        {"openfoam", false},
        {"openfoam_kind", ""},
        {"openfoam_version", ""},
        {"note", "The desktop C++ flow backend supports LBM; OpenFOAM is not routed by SimClient."},
    };
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
                       std::string& consoleNote, const std::string& ownerDocId,
                       std::uint64_t reservationToken) {
    std::lock_guard<std::mutex> op(operationMtx_);
    if (stopping_.load()) {
        consoleNote = "[sim] the simulation backend is stopping";
        return false;
    }

    const std::uint64_t activeReservation = reservationToken_.load();
    const bool usingReservation = reservationToken != 0;
    if (usingReservation) {
        if (activeReservation != reservationToken ||
            reservationOwnerDocId_ != ownerDocId) {
            consoleNote = "[sim] the simulation reservation is no longer valid";
            return false;
        }
        if (cancelInFlight_.load()) {
            reservationToken_.store(0);
            reservationOwnerDocId_.clear();
            consoleNote = "[sim] cancellation is still settling";
            return false;
        }

        // A valid token is single-use even if an unexpected state change makes launch fail.
        // busy() remains true through either state, so consuming it cannot expose an idle gap.
        if (job_.running.load()) {
            reservationToken_.store(0);
            reservationOwnerDocId_.clear();
            consoleNote = "[sim] a study is already running";
            return false;
        }
        if (job_.ready.load()) {
            reservationToken_.store(0);
            reservationOwnerDocId_.clear();
            consoleNote = "[sim] the previous study result is still pending";
            return false;
        }
    } else if (activeReservation != 0) {
        consoleNote = "[sim] a study is being prepared";
        return false;
    }

    if (!usingReservation && cancelInFlight_.load()) {
        consoleNote = "[sim] cancellation is still settling";
        return false;
    }

    if (!usingReservation && job_.running.load()) {
        consoleNote = "[sim] a study is already running";
        return false;
    }
    if (!usingReservation && job_.ready.load()) {
        consoleNote = "[sim] the previous study result is still pending";
        return false;
    }

    // Publish running before clearing the matching reservation so busy() never observes an
    // idle gap between preparation and solve. From here on, every failure consumes the token.
    job_.running.store(true);
    if (usingReservation) {
        reservationToken_.store(0);
        reservationOwnerDocId_.clear();
    }

    // Completed owned operations remain joinable until the UI consumes their result. Reap them
    // before reusing worker/job state; running=false guarantees the solve thread is at its tail.
    if (cancelThread_.joinable()) cancelThread_.join();
    if (launchThread_.joinable()) launchThread_.join();
    if (workerFailed_.exchange(false)) {
        std::lock_guard<std::mutex> start(startMtx_);
        worker_.stop();
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
        job_.cancelled = false;
        job_.result = nlohmann::json::object();
        job_.error.clear();
        job_.errorCode.clear();
    }
    {
        std::lock_guard<std::mutex> lk(job_.evMtx);
        job_.events.clear();
    }
    job_.ready = false;
    cancelRequested_ = false;

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
    static std::atomic<std::uint64_t> counter{0};
    curJobId_ = "job" + std::to_string(counter.fetch_add(1) + 1);
    if (params.contains("study") && params["study"].is_object())
        params["study"]["job_id"] = curJobId_;
    else
        params["job_id"] = curJobId_;

    try {
        launchThread_ = std::thread(
            [this, method, params = std::move(params)]() mutable {
                WorkerClient::EventFn onEvent = [this](const nlohmann::json& d) {
                    // Only sim_progress carries the fields we care about; tolerate unknown kinds.
                    SimProgress ev;
                    ev.stage = jstr(d, "stage");
                    ev.pct = jint(d, "pct");
                    ev.text = jstr(d, "text");
                    if (auto it = d.find("trace_point");
                        it != d.end() && it->is_object()) {
                        ev.hasTrace = true;
                        ev.traceStep = jint(*it, "step");
                        ev.traceDrag = jnum(*it, "drag");
                    }
                    // Update the live progress display (read before ev is moved into the queue).
                    {
                        std::lock_guard<std::mutex> lk(progMtx_);
                        if (!ev.stage.empty()) progStage_ = ev.stage;
                        if (!ev.text.empty()) progLog_ = ev.text;
                    }
                    std::lock_guard<std::mutex> lk(job_.evMtx);
                    job_.events.push_back(std::move(ev));
                };

                nlohmann::json result;
                std::string err;
                bool ok = false;
                try {
                    // No host-side timeout for sim (cancel is explicit) — use a very large cap
                    // (worker_rpc §4). stop() aborts the daemon transport before joining.
                    ok = worker_.call(method, params, result, err, 3'600'000, onEvent);
                } catch (const std::exception& ex) {
                    err = ex.what();
                } catch (...) {
                    err = "simulation backend threw an unknown exception";
                }

                {
                    std::lock_guard<std::mutex> lk(job_.mtx);
                    if (!ok) {
                        // Defer WorkerClient teardown until this owned operation is joined.
                        // Calling stop() here would race an in-flight cancellation side call.
                        job_.ok = false;
                        job_.error = err.empty() ? "simulation worker failed" : err;
                        workerFailed_ = true;
                    } else if (!result.is_object()) {
                        job_.ok = false;
                        job_.error = "simulation backend returned a non-object result";
                    } else {
                        auto stringField = [&](const char* key) {
                            auto it = result.find(key);
                            return it != result.end() && it->is_string()
                                       ? it->get<std::string>()
                                       : std::string();
                        };
                        const std::string errorText = stringField("error");
                        const std::string messageText = stringField("message");
                        const std::string& cancelText =
                            !errorText.empty() ? errorText : messageText;
                        const bool cancelled =
                            (result.contains("cancelled") &&
                             result["cancelled"].is_boolean() &&
                             result["cancelled"].get<bool>()) ||
                            stringField("code") == "cancelled" ||
                            cancelText.find("cancelled") != std::string::npos ||
                            cancelText.find("canceled") != std::string::npos;
                        if (cancelled) {
                            job_.ok = false;
                            job_.cancelled = true;
                            job_.errorCode = "cancelled";
                            job_.error = messageText;
                            if (job_.error.empty()) job_.error = errorText;
                            if (job_.error.empty()) job_.error = "cancelled";
                        } else if (result.contains("code") ||
                                   !result.value("ok", true)) {
                            // Both engine-style {code,message} and worker-style
                            // {ok:false,error|reason|hint} are method failures.
                            job_.ok = false;
                            job_.errorCode = stringField("code");
                            for (const char* key : {"message", "error", "reason", "hint"}) {
                                job_.error = stringField(key);
                                if (!job_.error.empty()) break;
                            }
                            if (job_.error.empty()) job_.error = "simulation error";
                        } else {
                            job_.ok = true;
                            job_.result = std::move(result);
                        }
                    }
                }

                launchNs_.store(0LL);
                cancelRequested_ = false;
                {
                    std::lock_guard<std::mutex> lk(progMtx_);
                    progStage_.clear();
                    progLog_.clear();
                }
                job_.ready = true;
                job_.running = false;
            });
    } catch (const std::exception& ex) {
        launchNs_ = 0LL;
        job_.running = false;
        curJobId_.clear();
        {
            std::lock_guard<std::mutex> lk(progMtx_);
            progStage_.clear();
            progLog_.clear();
        }
        consoleNote = std::string("[sim] could not start solver thread: ") + ex.what();
        return false;
    }

    consoleNote = "[sim] launched " + method;
    return true;
}

}  // namespace core
