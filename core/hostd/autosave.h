// Daemon-side autosave + crash recovery (Phase 3) — THE one autosave scheduler
// (the design ported from services/mcp/cad_tools.py, whose per-process
// threading.Timer chains this replaces in daemon mode).
//
// One std::thread + condition_variable loop. Each tick (AI_CAD_AUTOSAVE_INTERVAL_S,
// default 60 s, 0 disables): engine doc_list -> every DIRTY doc whose revision
// changed since its last autosave is doc_save'd (geometry cache embedded) into
//   <stateDir>/hostd_autosave/<instance-key>/<session-id>/<doc_id>.step+
// where session-id = zero-padded daemon start ms + pid (lexically sortable).
// The instance-key level is a deliberate addition over the plan's path: scratch
// test daemons must never recover (or sweep) the real daemon's sessions.
//
// On start (once the engine is ready) the MOST RECENT prior session's autosaves
// are doc_open'd back in (the engine assigns fresh ids); the resulting
// {old_id: new_id} map is served to every client in the hello response so
// reconnecting clients rebind their handles. Sessions older than that most
// recent one are swept (deleted); the recovered one is kept until this session
// produces its own saves.
#pragma once

#include <atomic>
#include <condition_variable>
#include <map>
#include <mutex>
#include <string>
#include <thread>

#include <nlohmann/json.hpp>

namespace hostd {

class Autosave {
public:
    // baseDir: <stateDir>/hostd_autosave/<instance-key>. engineState: the
    // server's loader flag (0=loading 1=ready 2=failed) — recovery and ticks
    // wait for ready.
    void start(const std::string& baseDir, const std::atomic<int>* engineState);

    // Stop the loop (join). finalSave: save every dirty doc first — the clean
    // shutdown / drain path.
    void stop(bool finalSave);

    // {old_doc_id: new_doc_id} from recovery; empty until recoveryDone().
    nlohmann::json idRemap();
    bool recoveryDone() const { return recoveryDone_; }

private:
    void loop();
    void recoverPriorSession();
    void saveDirtyDocs();

    std::string baseDir_;
    std::string sessionDir_;
    const std::atomic<int>* engineState_ = nullptr;
    double intervalS_ = 60.0;

    std::thread thread_;
    std::mutex mtx_;
    std::condition_variable cv_;
    std::atomic<bool> stopping_{false};
    std::atomic<bool> recoveryDone_{false};

    std::mutex remapMtx_;
    nlohmann::json idRemap_ = nlohmann::json::object();
    std::map<std::string, long> lastSavedRev_;  // doc_id -> revision at last save
};

}  // namespace hostd
