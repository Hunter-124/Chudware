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

    // Launch a sim_* method on a background thread. `params` is the full RPC params object
    // (e.g. {"study": {...}} or {"stl_path":..., "direction":..., ...}). The job streams
    // sim_progress into job().events and latches job().ready when done. No-op (returns false)
    // if a job is already running or the worker can't be started.
    bool launch(const std::string& method, nlohmann::json params, std::string& consoleNote);

    // Best-effort cancel of the in-flight job (sends sim_cancel on a detached thread).
    void cancel();

    SimJobResult& job() { return job_; }

private:
    WorkerClient worker_;
    SimJobResult job_;
    std::string curJobId_;
    std::mutex startMtx_;
};

}  // namespace core
