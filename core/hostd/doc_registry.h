// Doc access registry + event bus for chudware-hostd (Phase 3 of the shared-engine
// plan). Everything here is daemon-layer policy — the engine stays completely
// client-ignorant; the registry only inspects requests/responses passing through
// Server::dispatch.
//
// Responsibilities:
//   * DocAccessTable: per doc_id -> {mode per client (rw|ro), last_seen_rev per
//     client, one optional edit-hold, subscribers}.
//   * Daemon methods: attach_doc / detach_doc / edit_hold / edit_release /
//     subscribe (dispatched via handleMethod).
//   * Access enforcement (preCall): mutating call from an ro attach -> hard
//     {"code":"read_only"}; from an unattached client -> implicit rw attach
//     (compat with existing single-client flows); mutating call while another
//     client holds an edit-hold -> retriable {"code":"busy_preview"}.
//   * Overlap CAS (preCall): when a doc has >1 rw attachment, inject the
//     caller's last_seen_rev as base_revision into doc_apply_ops/doc_undo/
//     doc_redo so the engine's existing stale_revision CAS fires on races.
//     Single-writer docs are byte-identical passthrough.
//   * Revision sniff + event fanout (postCall): mutating responses already
//     carry top-level "revision" (ops_response, core/doc/session.cpp) plus
//     tess.aicm_path / rebuild.feature_status; when a doc's revision advances
//     the registry pushes {"event":"doc.changed",...} to every OTHER
//     subscribed client, doc.opened on mint, doc.closed on doc_close/last-rw
//     detach, doc.lease_change on hold grant/release/expiry.
//     EXCEPTION — internal docs: a doc minted with {"internal":true} (the agent
//     preflight advisor's shadow clone) is registered like any other but NEVER
//     produces a single event, so no mirror ever opens a tab for it. See
//     DocState::internal.
//   * _foreign_edits piggyback (postCall): when a client's call touches a doc
//     another client advanced since this client's last touch, the registry
//     appends "_foreign_edits":[...] into the RESULT object.
//
// MUTATING-METHOD CLASSIFICATION (the documented choice): enforcement applies
// to any engine method whose params carry a "doc_id" and whose name is NOT in
// the read-only allowlist below (exact names + prefixes). The allowlist covers
// every pure-read family in core/api/libchudware.cpp dispatch: doc_tree/doc_query/
// doc_bbox/doc_get_meta/render/describe/eval_*/sim_* solves (read geometry,
// write no doc state)/doc_export/prof_*/sketch probes. Anything new therefore
// FAILS SAFE as mutating — a false positive costs an ro client an error, never
// a silent shared-doc write.
#pragma once

#include <functional>
#include <map>
#include <mutex>
#include <set>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace hostd {

class DocRegistry {
public:
    // Sends one event line to client `clientId` (Server locks that conn's
    // writeMtx). Installed once by Server before the accept loop starts.
    using EventSender = std::function<void(int clientId, const std::string& line)>;

    void setEventSender(EventSender s) { send_ = std::move(s); }

    // Daemon-level methods (attach_doc/detach_doc/edit_hold/edit_release/
    // subscribe). Returns true if `method` was one of ours; result/isError as
    // in Server::dispatch.
    bool handleMethod(int clientId, const std::string& kind, const std::string& method,
                      const nlohmann::json& params, nlohmann::json& result, bool& isError);

    // Policy gate before an engine passthrough. May MUTATE params (base_revision
    // injection). Returns false with err filled ({"code","message",...}) when the
    // call must be rejected (read_only / busy_preview / undo_denied).
    // `userId`/`userDisplay` are the connection's optional multi-user identity
    // (from hello params.user); empty when unset. `warning` (when set) is appended
    // to the successful result by the caller — the implicit-attach "doc has
    // another writer" path.
    bool preCall(int clientId, const std::string& kind, const std::string& userId,
                 const std::string& userDisplay, const std::string& method,
                 nlohmann::json& params, nlohmann::json& err, std::string& warning);

    // Response post-processing after a SUCCESSFUL engine call: revision sniff,
    // implicit attach of minted docs, event fanout, _foreign_edits injection.
    // Records lastWriterUser{Id,Display} on writes and includes them on doc.changed.
    void postCall(int clientId, const std::string& kind, const std::string& userId,
                  const std::string& userDisplay, const std::string& method,
                  const nlohmann::json& params, nlohmann::json& result,
                  bool fanoutToSource = false);

    // Fan out doc.history_cleared to ALL subscribers of `docId` (including the
    // saver — exceptClient is -1 so every GUI greys undo/redo).
    void notifyHistoryCleared(const std::string& docId, int sourceClientId);

    // Stamp each doc_list entry with "origin_kind" (the kind of the client that
    // MINTED the doc: gui|mcp|agent|test, or "unknown"). Lets a GUI that connected
    // AFTER a doc was created recover its user-vs-AI ownership on bootstrap.
    void annotateDocList(nlohmann::json& result);

    // True if a client OTHER than `clientId` currently holds an rw attach on
    // `docId`. Server uses this to degrade a doc_close into a plain detach so one
    // client can't destroy a doc another client is still editing.
    bool hasOtherRwWriter(const std::string& docId, int clientId);

    // True when this doc was MINTED by an agent/mcp client. Ownership, not
    // attachment: an agent's per-turn worker process comes and goes, so between
    // turns nobody is attached even though a run still owns the work. The server
    // uses this (with agentWorkInFlight) to refuse to purge a doc the user closed
    // from their viewport while the agent is still building it.
    bool isAgentOrigin(const std::string& docId);

    // True when this doc was minted with {"internal":true} (see DocState::internal) —
    // the agent preflight advisor's throwaway shadow clone. Beyond suppressing fanout,
    // the daemon must never treat one as user work: no autosave artifact, and doc-GC
    // PURGES it instead of retiring it to the recovery tier. Without that, a clone the
    // agent could not close (worker died mid-run) resurfaces in the "Recover?" prompt
    // and the user accepts a tab strip of `AI: <part>_pf2/3/4` shadow copies.
    bool isInternal(const std::string& docId);

    // The last doc.changed payload pushed for `docId` (see DocState::lastPush), or a
    // null json when nothing has been pushed yet. Reads only the registry mutex —
    // NEVER the engine or the doc's per-doc mutex — so a GUI can populate a mirror
    // tab for a doc another client is mid-write on without queueing behind it.
    nlohmann::json lastPushFor(const std::string& docId);

    // Client disconnected: drop its attachments/holds/subscriptions and notify
    // remaining subscribers (doc.lease_change). Docs left with NO attached
    // client start the doc-GC grace countdown (CHUDWARE_DOC_GC_GRACE_S, default
    // 90 s; CHUDWARE_DOC_GC=0 disables) — swept by tick(). Agent/mcp-origin docs
    // use CHUDWARE_DOC_GC_AGENT_GRACE_S (default 1800 s) so a just-finished
    // mission's parts stay openable from the GUI after the agent detaches.
    void onDisconnect(int clientId, const std::string& kind);

    // Periodic housekeeping from the accept loop (~every 500 ms): expire stale
    // edit-holds (doc.lease_change) and return doc ids whose GC grace expired
    // with still no attached client. The Server retires + closes those.
    std::vector<std::string> tick();

    // A doc the daemon itself closed (GC): drop registry state + fan out
    // doc.closed with the given reason so mirrors drop their tabs.
    void onDaemonClosedDoc(const std::string& docId, const std::string& reason);

    // Re-check, under the registry lock, that a doc tick() flagged as expired is
    // STILL abandoned (no client attached) right before the Server retires+closes
    // it. tick() releases mtx_ before the Server acts, so a client can re-attach
    // (re-list -> attach_doc / implicit rw) in that window — closing it out from
    // under the client would be silent data loss. Returns false if the doc gained
    // an attachment or already vanished; the Server then aborts the GC close.
    bool isReapable(const std::string& docId);

    // Re-check reapability AND dispatch `closeFn` atomically under mtx_ — the SAME lock
    // attach_doc/detach_doc/preCall's implicit-attach all take. WHY: isReapable() alone only
    // proves the doc was abandoned at the instant it ran; the retire-tier I/O between that
    // check and the actual engine doc_close call (doc_list + retireDoc, both unlocked) gives
    // a racing client's attach a window to land right before the doc is torn down. Holding
    // mtx_ across the recheck AND closeFn closes that window: a concurrent attach either
    // lands first (recheck sees it, closeFn never runs) or blocks until closeFn returns.
    // Returns false (closeFn NOT invoked) if the doc gained an attachment or already vanished.
    // closeFn MUST NOT call back into DocRegistry — mtx_ is non-recursive.
    bool reapUnderLock(const std::string& docId, const std::function<void()>& closeFn);

    // How many docs are still counting DOWN their GC grace (orphaned, deadline not yet
    // reached). Read-only; Server::idleExpired refuses the idle-exit path while this is
    // non-zero — see the invariant spelled out there. Docs already PAST their deadline are
    // deliberately excluded: tick() retires + closes those, and a retire that keeps failing
    // must never pin the daemon forever.
    int pendingGcCount();
    // Shared fail-closed classification used by daemon containment. Methods not
    // listed here are treated as protected even when the engine does not know them.
    static bool isReadOnlyMethod(const std::string& method);


private:
    struct Attach {
        std::string mode;       // "rw" | "ro"
        long lastSeenRev = -1;  // engine revision at this client's last touch
    };
    struct Hold {
        bool active = false;
        int clientId = 0;
        std::string kind;
        long long deadlineMs = 0;
    };
    struct DocState {
        std::map<int, Attach> attach;  // client_id -> mode/last-seen
        Hold hold;
        long revision = -1;  // last revision the daemon observed for this doc
        int lastWriterClient = -1;
        std::string lastWriterKind;
        std::string lastWriterMethod;
        // Multi-user identity of the last writer (from hello params.user). Empty
        // strings when the writer did not declare a user (local single-seat).
        std::string lastWriterUserId;
        std::string lastWriterUserDisplay;
        // Kind of the client that minted this doc (gui|mcp|agent|test). Immutable
        // once set — the origin of ownership (user vs AI). Empty = unknown.
        std::string originKind;
        // Internal doc: minted with {"internal":true} and deliberately invisible on
        // the wire — the registry NEVER fans out doc.opened/doc.changed/doc.closed
        // (nor lease_change/history_cleared) for it. WHY: the agent preflight advisor
        // mints a shadow clone of the live part inside a single add_features call and
        // closes it a few RPCs later; mirroring that clone made the GUI open a
        // throwaway part tab for every operation the AI performed. Registration,
        // attach/rw counting and doc-GC are unaffected — only the fanout is skipped.
        bool internal = false;
        // Doc-GC: when > 0, no client is attached and the doc closes once this
        // deadline passes without a new attach (zombie-doc prevention).
        long long orphanDeadlineMs = 0;
        // The last doc.changed payload fanned out for this doc (tree + aicm_path +
        // owner_map + datums + feature_status). Served verbatim by doc_snapshot.
        // WHY it is kept: a GUI mirror tab that has not yet received a push had no way
        // to populate itself except doc_activate, which takes the doc's per-doc engine
        // mutex (T-PERF5 DocLock) and therefore QUEUES BEHIND THE AGENT'S WRITE BURST.
        // Measured on a live run: 142.9 s for one tab switch onto the part the agent was
        // building. This copy costs one tree per doc and is served without touching the
        // engine at all, so spectating a doc can never wait on the client writing it.
        // SEEDED AT MINT TIME too (postCall's `minted` branch, same snapshotPayload
        // builder): a doc_new response already carries the full ops_response, so the very
        // first doc_snapshot after doc.opened serves a real empty-tree snapshot instead of
        // has_snapshot=false. Without it the freshly revealed mirror tab still had to
        // doc_activate, which is exactly the lock queue described above — the user saw the
        // tab appear and then waited for the agent's first geometry before it opened.
        // Only ever overwritten by a STRICTLY HIGHER revision, so the seed cannot shadow
        // geometry.
        nlohmann::json lastPush;
    };
    struct PendingEvent {
        int target;
        std::string line;
    };

    static long sniffRevision(const nlohmann::json& result);
    // Display name of the doc a minting call just created, read out of the SAME
    // place doc_list reports it from: the document's own "name" field. doc_new /
    // parts_make put it at the top level; every ops_response carries the whole
    // doc as "tree". Empty when the response carries neither (the GUI then falls
    // back to its placeholder + async doc_tree backfill).
    static std::string sniffName(const nlohmann::json& result);
    int holdSecs() const;
    // Start/refresh the GC countdown when `d` has no attachments; clear it when
    // it does. Agent/mcp-origin docs get a longer grace (CHUDWARE_DOC_GC_AGENT_GRACE_S).
    // No-op when doc-GC is disabled.
    static void updateOrphanDeadlineLocked(DocState& d);
    int rwCountLocked(const DocState& d) const;
    // Queue an event for every subscriber of `docId` except `exceptClient`. Drops the
    // event outright when the doc is internal (see DocState::internal).
    void fanoutLocked(const std::string& docId, const nlohmann::json& evt, int exceptClient,
                      std::vector<PendingEvent>& out);
    void flush(std::vector<PendingEvent>& pending);
    // Expire the hold on `d` if past deadline; queues the lease_change event.
    void expireHoldLocked(const std::string& docId, DocState& d,
                          std::vector<PendingEvent>& out);

    std::mutex mtx_;
    std::map<std::string, DocState> docs_;
    // client_id -> subscribed doc ids; the "*" entry means everything.
    std::map<int, std::set<std::string>> subs_;
    EventSender send_;
};

}  // namespace hostd
