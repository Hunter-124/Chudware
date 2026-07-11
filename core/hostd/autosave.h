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
// RECOVERY IS CRASH-ONLY (doc-lifecycle overhaul). The rule: a CLEAN exit
// discards, a CRASH is recoverable — and the app signals which by whether it
// explicitly discarded before going away.
//
//  * Explicit discard — an app closing a tab, or quitting cleanly (it discards
//    every unsaved doc it leaves behind), calls doc_discard. That closes the doc
//    AND purges its autosave artifacts (onDocClosed) — a deliberately-dropped
//    part never resurfaces.
//
//  * doc-GC RETIRE — a doc whose every client vanished WITHOUT discarding it
//    (an app/agent crash) is doc_save'd into the ORPHAN tier (<baseDir>/orphaned/,
//    {stem}.step+ + {stem}.json meta) and then closed to free memory. A clean doc
//    already safe in a real user file is just closed. This is the long-lived
//    disk recovery store (AICAD_ORPHAN_KEEP entries).
//
//  * CRASH tier — a prior session dir that still exists means that daemon itself
//    died hard (clean shutdown removes its session dir). Its autosaves are NOT
//    auto-opened (that silently resurrected zombie parts) — each is registered
//    into the same orphan tier.
//
//  Everything in the orphan tier waits for a DELIBERATE recovery_list ->
//  recovery_open (the GUI "Recover unsaved parts?" prompt, or the recover_parts
//  MCP tool); nothing auto-loads. recovery_open mints a fresh id and records
//  {old_id: new_id} (noteRecoveredRemap) so hello can rebind a reconnecting
//  client. This keeps closed / cleanly-quit parts from flooding back while still
//  catching genuine crashes.
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

    // Stop the loop (join) and DISCARD: remove this session's dir so the next
    // start does NOT crash-recover a cleanly shut down session. Clean shutdown is
    // not a crash — unsaved work is dropped (the GUI's quit warning is the guard),
    // recovery is reserved for crashes. finalSave is retained for ABI/callers but
    // no longer preserves anything.
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
    // The stored meta for orphan entry `id` ({doc_id,name,retired_ms,from_crash});
    // empty object when unknown. Lets recovery_open recover the ORIGINAL doc_id.
    nlohmann::json orphanMeta(const std::string& id);
    // Delete orphan `id` (or all when id==""). Returns how many were removed.
    int discardOrphans(const std::string& id);
    // Record a crash-recovery rebind so hello can hand a reconnecting client the
    // {old_id: new_id} map (recovery_open mints a fresh id for each restored doc).
    void noteRecoveredRemap(const std::string& oldId, const std::string& newId);

    // Explicit doc_close: delete the doc's session autosave, and when the doc
    // was crash-recovered also its source file + id_remap entry.
    void onDocClosed(const std::string& docId);

    // doc-GC of an ABANDONED doc (an app/agent vanished without discarding it —
    // i.e. a crash): doc_save `docId` into the orphan tier + drop its session
    // autosave, so the work is recoverable long-term while the live doc is closed
    // to free memory. `name` is stored for the recovery prompt. False on save fail.
    bool retireDoc(const std::string& docId, const std::string& name);

    // True when `path` is one of OUR artifacts (session autosave / orphan file):
    // a doc whose path points in here is NOT "safe in a user file", so GC still
    // retires it rather than trusting the on-disk copy.
    bool isAutosavePath(const std::string& path) const {
        return !baseDir_.empty() && path.rfind(baseDir_, 0) == 0;
    }

private:
    void loop();
    void recoverPriorSession();
    void saveDirtyDocs();
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
    // (onDocClosed) — guarded by revMtx_ against concurrent map ops.
    std::mutex revMtx_;
    std::map<std::string, long> lastSavedRev_;
    std::mutex orphanMtx_;  // serializes orphan dir mutations
};

}  // namespace hostd
