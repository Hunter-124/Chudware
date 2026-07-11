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
// TWO RECOVERY TIERS (doc-lifecycle overhaul):
//
//  * CRASH tier — a prior session dir that still exists means that daemon died
//    hard (clean shutdown removes its session dir). On start the MOST RECENT
//    prior session's autosaves are doc_open'd back in (the engine assigns fresh
//    ids); the {old_id: new_id} map is served in hello so reconnecting clients
//    rebind. Sessions older than that most recent one are swept.
//
//  * ORPHAN tier — <baseDir>/orphaned/. Docs retired here are NEVER auto-opened:
//    clean shutdown retires every open doc with unsaved state, and the doc-GC
//    (all clients gone + grace expired) retires before closing. Clients list
//    them with recovery_list and explicitly recovery_open / recovery_discard.
//    This is what stops closed viewports from resurrecting zombie parts on the
//    next startup while keeping the work recoverable.
//
// Explicit doc_close deletes the doc's autosave artifacts (session file, and
// its crash-tier source when the doc came from recovery) — closed means closed.
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

    // Stop the loop (join). finalSave: save every dirty doc, retire every open
    // doc with unsaved state to the orphan tier, then remove this session's dir
    // so the next start does NOT crash-recover a cleanly shut down session.
    void stop(bool finalSave);

    // {old_doc_id: new_doc_id} from crash recovery; empty until recoveryDone().
    nlohmann::json idRemap();
    bool recoveryDone() const { return recoveryDone_; }

    // ---- orphan tier -----------------------------------------------------
    // {"entries":[{"id","doc_id","name","path","mtime_ms"}]} newest first.
    nlohmann::json recoveryList();
    int orphanCount();
    // Look up the orphan file for entry `id`; "" when unknown. Does not delete —
    // the caller removes via discardOrphan() only after a successful doc_open.
    std::string orphanPath(const std::string& id);
    // Delete orphan `id` (or all when id==""). Returns how many were removed.
    int discardOrphans(const std::string& id);

    // Explicit doc_close: delete the doc's session autosave, and when the doc
    // was crash-recovered also its source file + id_remap entry.
    void onDocClosed(const std::string& docId);

    // True when `path` is one of OUR artifacts (session autosave / recovery /
    // orphan file). doc_save sets the doc's path + clears dirty, so a doc whose
    // path points in here is NOT "safe in a user file" — it must still be
    // retired before closing.
    bool isAutosavePath(const std::string& path) const {
        return !baseDir_.empty() && path.rfind(baseDir_, 0) == 0;
    }

    // Doc-GC / clean-shutdown path: doc_save `docId` into the orphan tier and
    // drop its session autosave. `name` is stored beside it for the recovery
    // prompt. False when the engine save failed (session file is kept).
    bool retireDoc(const std::string& docId, const std::string& name);

private:
    void loop();
    void recoverPriorSession();
    void saveDirtyDocs();
    void retireOpenDocsOnExit();
    void sweepOrphans();  // keep the newest AICAD_ORPHAN_KEEP (default 20)

    std::string baseDir_;
    std::string sessionDir_;
    std::string orphanDir_;
    std::string recoverDir_;  // crash-tier source session ("" when none)
    const std::atomic<int>* engineState_ = nullptr;
    double intervalS_ = 60.0;

    std::thread thread_;
    std::mutex mtx_;
    std::condition_variable cv_;
    std::atomic<bool> stopping_{false};
    std::atomic<bool> recoveryDone_{false};

    std::mutex remapMtx_;
    nlohmann::json idRemap_ = nlohmann::json::object();
    // doc_id -> revision at last save. Written by the autosave thread
    // (saveDirtyDocs) and erased from server-dispatch / accept-loop threads
    // (onDocClosed, retireDoc) — guarded by revMtx_ against concurrent map ops.
    std::mutex revMtx_;
    std::map<std::string, long> lastSavedRev_;
    std::mutex orphanMtx_;  // serializes orphan dir mutations
};

}  // namespace hostd
