// SimClient (C19) — see sim_client.h. Mirrors jobs.cpp: background thread per job, sim_progress
// events drained on the main thread; lazy spawn + restart-on-crash of the second worker process.
#include "sim_client.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <limits>
#include <thread>
#include <exception>
#include <utility>

#include "app_state.h"  // jstr/jint/jnum/jbool safe JSON accessors
#include "cpp_core.h"   // in-process C++ core (libchudware) — the sim backend when no Python worker
#include "engine_backend.h"  // daemonMode — WASM/desktop hostd path (no child sim worker)

namespace core {

namespace {

constexpr double kUnmeasured = std::numeric_limits<double>::quiet_NaN();

// A native metric: finite number, else NaN (the wire sends null for "not measured").
double measured(const nlohmann::json& obj, const char* key) {
    auto it = obj.find(key);
    if (it == obj.end() || !it->is_number()) return kUnmeasured;
    const double v = it->get<double>();
    return std::isfinite(v) ? v : kUnmeasured;
}

void readNamedValues(const nlohmann::json& obj, const char* key,
                     std::vector<std::pair<std::string, double>>& out) {
    auto it = obj.find(key);
    if (it == obj.end() || !it->is_object()) return;
    out.reserve(it->size());
    for (const auto& [name, value] : it->items()) {
        double v = kUnmeasured;
        if (value.is_number()) {
            v = value.get<double>();
            if (!std::isfinite(v)) v = kUnmeasured;
        }
        out.emplace_back(name, v);
    }
}

// "mesh.begin" -> "Mesh begin", "setup" -> "Setup".
std::string phaseLabel(const std::string& phase) {
    std::string out = phase;
    for (char& ch : out)
        if (ch == '.' || ch == '_') ch = ' ';
    if (!out.empty() && out[0] >= 'a' && out[0] <= 'z') out[0] = static_cast<char>(out[0] - 32);
    return out;
}

void appendMetric(std::string& text, const char* label, double v) {
    if (!std::isfinite(v)) return;
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%s%s %.4g", text.empty() ? "" : ", ", label, v);
    text += buf;
}

// Native core/sim/progress.h event: {type:"sim.progress", phase, elapsed_s, elements,
// probe?, iteration?}. Always relevant: it names a phase even before anything is measured.
void parseNativeProgress(const nlohmann::json& d, SimProgress& ev) {
    const std::string phase = jstr(d, "phase");
    ev.stage = phase;
    const double elapsed = measured(d, "elapsed_s");
    if (std::isfinite(elapsed) && elapsed >= 0.0) ev.elapsedS = elapsed;
    if (auto el = d.find("elements"); el != d.end() && el->is_number_integer()) {
        const long long n = el->get<long long>();
        if (n >= 0) ev.elements = n;
    }

    auto itIt = d.find("iteration");
    if (itIt != d.end() && itIt->is_object()) {
        const auto& j = *itIt;
        SimIteration& it = ev.iteration;
        it.index = jint(j, "index");
        it.maxIterations = std::max(0, jint(j, "max"));
        it.objective = measured(j, "objective");
        it.objectiveChange = measured(j, "objective_change");
        it.volumeFraction = measured(j, "volume_fraction");
        it.designChange = measured(j, "design_change");
        it.maxConstraint = measured(j, "max_constraint");
        readNamedValues(j, "constraints", it.constraints);
        readNamedValues(j, "metrics", it.metrics);
        ev.hasIteration = true;

        if (it.maxIterations > 0) {
            const long long pct = static_cast<long long>(it.index) * 100 / it.maxIterations;
            ev.pct = static_cast<int>(std::clamp<long long>(pct, 0, 100));
        }
        char head[64];
        if (it.maxIterations > 0)
            std::snprintf(head, sizeof(head), "Iteration %d/%d", it.index, it.maxIterations);
        else
            std::snprintf(head, sizeof(head), "Iteration %d", it.index);
        std::string metrics;
        appendMetric(metrics, "objective", it.objective);
        appendMetric(metrics, "rel. change", it.objectiveChange);
        appendMetric(metrics, "volume", it.volumeFraction);
        appendMetric(metrics, "design change", it.designChange);
        appendMetric(metrics, "max constraint", it.maxConstraint);
        ev.text = metrics.empty() ? std::string(head) : std::string(head) + ": " + metrics;
        return;
    }

    ev.text = phase.empty() ? std::string("Working") : phaseLabel(phase);
    if (auto probe = d.find("probe"); probe != d.end() && probe->is_object()) {
        const std::string sub = jstr(*probe, "sub_phase");
        if (!sub.empty()) ev.text += " (" + sub + ")";
        if (auto done = probe->find("cells_done"), total = probe->find("cells_total");
            done != probe->end() && total != probe->end() && done->is_number_integer() &&
            total->is_number_integer() && total->get<long long>() > 0) {
            ev.text += " " + std::to_string(done->get<long long>()) + "/" +
                       std::to_string(total->get<long long>()) + " cells";
        }
    }
    if (ev.elements >= 0) ev.text += ", " + std::to_string(ev.elements) + " elements";
}

// Maps one streamed event onto SimProgress. False = not progress (other stream kinds the same
// sink may carry); the caller then queues nothing.
bool parseSimEvent(const nlohmann::json& d, SimProgress& ev) {
    if (!d.is_object()) return false;
    if (jstr(d, "type") == "sim.progress") {
        parseNativeProgress(d, ev);
        return true;
    }
    // Legacy sim_progress (events.md): {job_id, stage, pct, text, trace_point?}.
    ev.stage = jstr(d, "stage");
    ev.pct = jint(d, "pct");
    ev.text = jstr(d, "text");
    if (auto it = d.find("trace_point"); it != d.end() && it->is_object()) {
        ev.hasTrace = true;
        ev.traceStep = jint(*it, "step");
        ev.traceDrag = jnum(*it, "drag");
    }
    return !ev.stage.empty() || !ev.text.empty() || ev.pct != 0 || ev.hasTrace;
}

}  // namespace

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
    // No Python sim worker exists anymore (FEA + flow + cancel all route
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

std::vector<SimIteration> SimClient::iterationHistory() const {
    std::lock_guard<std::mutex> lk(progMtx_);
    return iterations_;
}

void SimClient::recordIterationLocked(SimIteration it) {
    const std::size_t n = iterations_.size();
    if (n >= kMaxIterationHistory) {
        // Thin 2:1: keep even positions, plus the newest entry when its position is odd.
        std::size_t w = 0;
        for (std::size_t r = 0; r < n; r += 2, ++w)
            if (w != r) iterations_[w] = std::move(iterations_[r]);
        if ((n - 1) % 2 != 0) iterations_[w++] = std::move(iterations_[n - 1]);
        iterations_.resize(w);
    }
    iterations_.push_back(std::move(it));
    iterationRevision_.fetch_add(1);
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
        iterations_.clear();
        iterationRevision_.fetch_add(1);
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
                    // Legacy sim_progress and native sim.progress both map onto SimProgress;
                    // anything else the stream carries is not progress and is not queued.
                    SimProgress ev;
                    if (!parseSimEvent(d, ev)) return;
                    // Update the live progress display (read before ev is moved into the queue).
                    {
                        std::lock_guard<std::mutex> lk(progMtx_);
                        if (!ev.stage.empty()) progStage_ = ev.stage;
                        if (!ev.text.empty()) progLog_ = ev.text;
                        if (ev.hasIteration) recordIterationLocked(ev.iteration);
                    }
                    std::lock_guard<std::mutex> lk(job_.evMtx);
                    // An owner that is not drawing never drains: drop the oldest quarter rather
                    // than grow for the whole solve (iterations survive in iterations_).
                    if (job_.events.size() >= SimJobResult::kMaxQueuedEvents)
                        job_.events.erase(
                            job_.events.begin(),
                            job_.events.begin() + static_cast<std::ptrdiff_t>(
                                                      SimJobResult::kMaxQueuedEvents / 4));
                    job_.events.push_back(std::move(ev));
                };

                nlohmann::json result;
                std::string err;
                bool ok = false;
                try {
                    // No host-side timeout for sim (cancel is explicit) — use a very large cap
                    // (worker_rpc §4). stop() aborts the daemon transport before joining.
                    // A topology optimization may legitimately outlast that cap; its liveness
                    // is the streamed per-iteration progress (hostd reclaims a solve that falls
                    // silent), so it waits without a transport deadline instead of abandoning
                    // a still-running solve at the hour mark.
                    const int timeoutMs = method == "sim_run_topology" ? -1 : 3'600'000;
                    ok = worker_.call(method, params, result, err, timeoutMs, onEvent);
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
                        // A native topology run stopped by sim_cancel answers with its partial
                        // design and termination "cancelled", which is never a success.
                        const bool cancelled =
                            (result.contains("cancelled") &&
                             result["cancelled"].is_boolean() &&
                             result["cancelled"].get<bool>()) ||
                            stringField("code") == "cancelled" ||
                            stringField("termination") == "cancelled" ||
                            cancelText.find("cancelled") != std::string::npos ||
                            cancelText.find("canceled") != std::string::npos;
                        if (cancelled) {
                            job_.ok = false;
                            job_.cancelled = true;
                            job_.errorCode = "cancelled";
                            job_.error = messageText;
                            if (job_.error.empty()) job_.error = errorText;
                            if (job_.error.empty()) job_.error = stringField("termination_detail");
                            if (job_.error.empty()) job_.error = "cancelled";
                            // Keep the partial reply for its owner (job_.ok stays false).
                            job_.result = std::move(result);
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
                            // Keep the refusal/diagnostic reply for its owner.
                            job_.result = std::move(result);
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
