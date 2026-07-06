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
#include <utility>
#include <vector>

#include <chrono>
#include <cstdint>
#include <nlohmann/json.hpp>

#include "worker_client.h"

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
    nlohmann::json result;
    std::string error;
    std::string errorCode;                // structured-error code (worker envelope) if any

    std::mutex evMtx;
    std::vector<SimProgress> events;      // streamed progress, drained on the main thread
};

class SimClient {
public:
    // Spawn the sim worker if it isn't running (lazy). Returns true if running afterwards.
    // `consoleNote` receives a human line on (re)spawn / failure for the app log.
    bool ensureStarted(std::string& consoleNote);

    bool started() const { return worker_.started(); }
    void stop() { worker_.stop(); }

    // True while a study is in flight (one job at a time).
    bool busy() const { return job_.running.load(); }

    // Thread-safe progress readouts consumed by the status bar while busy() == true.
    // stage()       — current solver phase ("mesh", "assemble", "solve", "post", …)
    // pct()         — 0..100 completion of the CURRENT stage (last streamed pct)
    // recentLog()   — most recent human-readable event text from the worker
    // elapsedSeconds() — wall seconds since the job was launched (0 if idle)
    // stageTimes()  — accumulated wall seconds per completed stage, in first-seen order
    //                 (e.g. {{"mesh",2.3f},{"solve",5.1f}}); lets the UI show which
    //                 phase dominated. The in-flight stage is included once it has run.
    std::string stage() const;
    int         pct() const;
    std::string recentLog() const;
    float       elapsedSeconds() const;
    std::vector<std::pair<std::string, float>> stageTimes() const;

    // Launch a sim_* method on a background thread. `params` is the full RPC params object
    // (e.g. {"study": {...}} or {"stl_path":..., "direction":..., ...}). The job streams
    // sim_progress into job().events and latches job().ready when done. No-op (returns false)
    // if a job is already running or the worker can't be started.
    // `ownerDocId` tags the job so the originating document's SimManager (and only it) consumes the
    // completion, even if the user switched tabs mid-run.
    bool launch(const std::string& method, nlohmann::json params, std::string& consoleNote,
                const std::string& ownerDocId = "");

    // Best-effort cancel of the in-flight job (sends sim_cancel on a detached thread).
    void cancel();

    // Which simulation engines are available, e.g. {lbm:true, openfoam:true,
    // openfoam_kind:"wsl", openfoam_version:"..."}. Queried once (lazily, only when idle) and
    // cached; returns {} until the first successful probe. Used to gate the Flow solver
    // selector so OpenFOAM is offered only when it's actually provisioned.
    nlohmann::json caps();

    SimJobResult& job() { return job_; }

private:
    WorkerClient worker_;
    SimJobResult job_;
    std::string curJobId_;
    std::mutex startMtx_;

    std::mutex capsMtx_;
    nlohmann::json capsCache_;
    std::atomic<bool> capsQueried_{false};

    // Live progress readouts — written by the background solve thread, read from the main
    // thread via the accessors above.  progMtx_ is mutable so the const accessors can lock it.
    mutable std::mutex     progMtx_;
    std::string            progStage_;        // current solver phase
    int                    progPct_ = 0;      // 0..100 completion of the current stage
    std::string            progLog_;          // last human-readable event text
    std::atomic<long long> launchNs_{0LL};   // steady_clock ns at launch; 0 when idle

    // Per-stage timing (transparency): first-seen order + accumulated seconds. Stamped
    // O(1) on each stage change and once more at completion so the final (usually longest)
    // stage is recorded. Guarded by progMtx_.
    std::vector<std::pair<std::string, float>> stageDur_;  // completed/running stage -> seconds
    std::string            stageStartName_;   // stage currently being timed
    long long              stageStartNs_ = 0; // steady_clock ns when stageStartName_ began
    void stampStageLocked(const std::string& nextStage);   // requires progMtx_ held
};

}  // namespace core
