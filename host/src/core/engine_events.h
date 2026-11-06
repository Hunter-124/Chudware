// Phase 4 (shared engine host daemon): per-frame drain of hostd push events.
//
// Called from the app.cpp main loop right after st.rpc.drain(). In-process
// cache refreshes use the same document-change routing as daemon pushes.
// Only GUI-origin or explicitly presented documents are automatically mirrored.
//   doc.presented    -> reveal an admitted background mirror without taking
//                       focus from user content, attached read-only.
//   doc.opened       -> reveal only GUI-origin documents.
//   doc.changed      -> refresh the active mirror + mesh, or prewarm the
//                       inactive tab's snapshot and mesh without selecting it.
//   doc.closed       -> close the matching mirror tab, EXCEPT reason
//                       "last_writer_detached" (fires every turn when the agent's
//                       per-turn worker exits; the doc is still live) which keeps
//                       the tab and just clears aiPulse / sets agentDone.
//   doc.studies_changed -> another client added/edited/deleted a sim study
//                       (tree-less revision): the tab's SimManager re-pulls its
//                       study list on the next Simulation-tree draw.
//   doc.lease_change -> toasts; an expired hold on the doc our PropertyManager
//                       is previewing cancels the preview (revision-parentage
//                       checked before the undo — see engine_events.cpp).
#pragma once

#include <functional>
#include <string>
#include <vector>
#include <nlohmann/json.hpp>

struct AppState;

namespace core {

// Drain daemon events or in-process cache-refresh notifications.
void engineEventsTick(AppState& st);
// Unregister the process-wide in-process sink before GUI teardown.
void shutdownEngineEvents();
// Resolve a non-modal external-file conflict, discarding edits only on explicit reload.
void reloadDiskConflict(AppState& st, const std::string& docId);
void requireDocMesh(const std::string& docId, int revision);
void meshCatchupStarted(const std::string& docId, int revision);
void forgetDocMesh(const std::string& docId);
// Mesh loader completion keeps failed or superseded revisions retryable.
void meshCatchupFinished(const std::string& docId, int revision, bool success);

// Suppress automatic read-only attaches while a deliberate file open upgrades
// the same live document to the user's writable view.
void beginUserDocOpen(AppState& st, const std::string& docId);
void endUserDocOpen(AppState& st, const std::string& docId);

// Queue a primary-connection detach/discard after removing its GUI tab. The
// per-frame drain retries contention and completes closes before reattaching.
void queueMirrorClose(AppState& st, const std::string& docId,
                      const std::string& title, bool detachOnly);

// Follow a reveng mission's live progress (contracts/reveng.md §4).
//
// ui::reveng::kickReconstruct() calls this with the run_id reveng_reconstruct hands back
// (~200 ms, status "running", mission on a worker thread). engineEventsTick() then tails that
// run's EventJournal — <data>/Chudware/cache/runs/<run_id>/events.jsonl — and translates each
// aicad.reveng_events/1 record into ui::reveng::feedRevEngEvent(), so the Reconstruct stage's
// progress bar / sub-step track / elapsed clock advance DURING the run instead of only when the
// coarse stage-badge poll next fires. See engine_events.cpp for why the journal beats both push
// channels and a status poll.
//
// The tail ends itself on the journal's terminal record (dag.run.done other than "paused"); it
// deliberately stays armed across a gate pause, because reveng_resolve_gate resumes the same
// run_id onto the same journal. An empty runId stops it. Cheap when idle (one empty-string test
// per frame).
void watchRevEngReconstruct(const std::string& runId);

// Reveal read-only mirrors for a deliberate recovery or reopen. recovery_open's
// doc.opened fanout excludes its caller, so it passes the returned ids here.
// Existing user content retains focus. No-op off daemon mode.
void mirrorRecoveredDocs(AppState& st, const std::vector<std::string>& docIds,
                         bool humanDirty = false, int revision = 0,
                         const std::string& path = {}, bool diskConflict = false,
                         const nlohmann::json& observedStamp = nullptr);

// Populate a mirror tab's parked snapshot + mesh cache from the daemon's LOCK-FREE
// doc_snapshot (hostd serves DocState::lastPush straight out of its registry — no engine
// call, so it cannot queue behind the client that is writing the doc).
//
// WHY this exists: a mirror tab used to populate itself with doc_activate, which despite
// being read-only still takes the doc's per-doc engine mutex. Clicking the tab for the part
// an agent was actively building therefore blocked behind the agent's whole write burst —
// 142.9 s on a live run — and every further click coalesced behind that one in-flight call,
// so the tab only ever opened during a lull in the agent's tool calls.
//
// Async and best-effort: `then` runs after the snapshot has been applied (or immediately on
// the next drain if the daemon has nothing yet), with `true` when a snapshot was applied.
// No-op off daemon mode (then is still invoked, with false).
void primeMirrorFromSnapshot(AppState& st, const std::string& docId,
                             std::function<void(bool)> then = {});

}  // namespace core
