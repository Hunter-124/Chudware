// SimClient (C19): the host's interface to the *second* worker process — `python -m
// engine.sim.worker` — which runs FEA / CFD studies. The sim worker is spawned LAZILY on the
// first study (FEA wheels are heavy; no reason to pay startup until a study runs) and restarted
// on crash with a console note. One job runs at a time on a background thread; sim_progress
// events are drained to the main/GL thread exactly like jobs.cpp does for design jobs.
//
// The sim worker never touches live document state — it receives FILES (STEP/STL) + study JSON,
// so a crash cannot corrupt a session. The host owns its lifecycle (worker_rpc.md §3/§4).
#pragma once

#include <atomic>
#include <functional>
#include <mutex>
#include <string>
#include <vector>
#include <thread>

#include <chrono>
#include <cstdint>
#include <nlohmann/json.hpp>

#include "../worker_client.h"

namespace core {

// One streamed sim_progress event (events.md): stage + pct + text, plus an optional CFD drag
// trace point. Drained on the main thread for the progress bar + the live drag sparkline.
struct SimProgress {
    std::string stage;   // voxelize|mesh|assemble|solve|stream|post
    int pct = 0;         // 0..100 (omit -> last)
    std::string text;
    bool hasTrace = false;
    int traceStep = 0;
    double traceDrag = 0.0;
};

// A finished sim job's outcome. `result` is the worker's result object (overlay_aicm, summary,
// freqs_hz, slice_pngs, lines_aicm, …) verbatim; ok=false leaves error/errorCode set.
struct SimJobResult {
    std::atomic<bool> running{false};
    std::atomic<bool> ready{false};       // a result is waiting to be consumed on the main thread
    std::mutex mtx;
    std::string method;                   // the sim_* method that ran
    std::string ownerDocId;               // doc id that launched this job (so the right tab consumes it)
    bool ok = false;
    bool cancelled = false;
    nlohmann::json result;
    std::string error;
    std::string errorCode;                // structured-error code (worker envelope) if any

    std::mutex evMtx;
    std::vector<SimProgress> events;      // streamed progress, drained on the main thread
};

class SimClient {
public:
    SimClient() = default;
    ~SimClient();
    SimClient(const SimClient&) = delete;
    SimClient& operator=(const SimClient&) = delete;

    // Spawn the sim worker if it isn't running (lazy). Returns true if running afterwards.
    // `consoleNote` receives a human line on (re)spawn / failure for the app log.
    bool ensureStarted(std::string& consoleNote);

    bool started() const;
    // Idempotent terminal shutdown: consume any reservation, request cancellation, abort
    // daemon transport, and join all owned operations before stopping the worker.
    void stop();

    // True while a study is reserved, in flight, or its completion is waiting for the owning
    // SimManager. A pending completion must block a new launch, otherwise launch()
    // clears job_.ready and silently discards the first study's result. A reservation closes
    // the prep-to-launch TOCTOU across document managers without pretending a solve is running.
    bool busy() const {
        return reservationToken_.load() != 0 || cancelInFlight_.load() ||
               job_.running.load() || job_.ready.load();
    }

    // Thread-safe progress readouts consumed by the status bar while busy() == true.
    // stage()       — current solver phase ("mesh", "assemble", "solve", "post", …)
    // recentLog()   — most recent human-readable event text from the worker
    // elapsedSeconds() — wall seconds since the job was launched (0 if idle)
    std::string stage() const;
    std::string recentLog() const;
    float       elapsedSeconds() const;

    // Atomically reserve the one global solver slot for asynchronous study preparation.
    // Tokens are scoped to this client and must be presented by the same owner at launch.
    // A matching launch consumes its reservation even when backend startup or thread creation
    // fails; releaseReservation is for preparation failures that never reach launch.
    bool tryReserve(const std::string& ownerDocId, std::uint64_t& token);
    void releaseReservation(std::uint64_t token);

    // Launch a sim_* method on a background thread. `params` is the full RPC params object
    // (e.g. {"study": {...}} or {"stl_path":..., "direction":..., ...}). The job streams
    // sim_progress into job().events and latches job().ready when done. No-op (returns false)
    // if a job is already running, a completion is still pending, or the backend can't start.
    // A nonzero reservation token must match both the active token and owner; tokenless launches
    // cannot bypass another manager's preparation reservation. `ownerDocId` tags the job so the
    // originating document's SimManager (and only it) consumes the completion, even if the user
    // switched tabs mid-run.
    bool launch(const std::string& method, nlohmann::json params, std::string& consoleNote,
                const std::string& ownerDocId = "", std::uint64_t reservationToken = 0);

    // Request cooperative cancellation of the in-flight C++ route. Delivery
    // bypasses the long-run connection; the solver observes it at its next
    // registered checkpoint (not necessarily inside a factorization).
    void cancel();
    bool cancelRequested() const { return cancelRequested_.load(); }

    // Which flow engines this client can actually route. The retired Python sim child is no
    // longer available to this WorkerClient; the in-process/hostd C++ flow path is LBM-only.
    // Cached after the C++ backend is proven loadable, so the UI never offers an unreachable
    // OpenFOAM path or turns a failed capability probe into a permanent ambiguous {}.
    nlohmann::json caps();

    SimJobResult& job() { return job_; }

private:
    // operationMtx_ serializes reservation transitions and owned thread handles. Operation
    // threads never acquire it, so stop() can abort their transport and join without deadlock.
    mutable std::mutex operationMtx_;
    std::thread launchThread_;
    std::thread cancelThread_;
    std::atomic<bool> stopping_{false};

    WorkerClient worker_;
    SimJobResult job_;
    std::string curJobId_;                    // guarded by operationMtx_
    std::atomic<bool> cancelRequested_{false};
    std::atomic<bool> cancelInFlight_{false};
    std::atomic<bool> workerFailed_{false};

    std::atomic<std::uint64_t> reservationToken_{0};
    std::string reservationOwnerDocId_;       // guarded by operationMtx_
    std::uint64_t nextReservationToken_ = 1;  // guarded by operationMtx_

    void startCancelLocked(bool allowSettled = false);

    mutable std::mutex startMtx_;

    std::mutex capsMtx_;
    nlohmann::json capsCache_;
    std::atomic<bool> capsQueried_{false};

    // Live progress readouts — written by the background solve thread, read from the main
    // thread via the accessors above.  progMtx_ is mutable so the const accessors can lock it.
    mutable std::mutex     progMtx_;
    std::string            progStage_;        // current solver phase
    std::string            progLog_;          // last human-readable event text
    std::atomic<long long> launchNs_{0LL};   // steady_clock ns at launch; 0 when idle
};

}  // namespace core
