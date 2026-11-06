// Doc access registry + event bus for chudware-hostd (Phase 3 of the shared-engine
// plan). Everything here is daemon-layer policy — the engine stays completely
// client-ignorant; the registry only inspects requests/responses passing through
// Server::dispatch.
//
// Responsibilities:
//   * DocAccessTable: per doc_id -> {mode per connection (rw|ro), last_seen_rev
//     per writer (a non-empty hello.writer_group shares one), edit-hold, subscribers}.
//   * Daemon methods: attach_doc / detach_doc / edit_hold / edit_release /
//     subscribe (dispatched via handleMethod).
//   * Access enforcement (preCall): mutating call from an ro attach -> hard
//     {"code":"read_only"}; desktop unattached clients implicitly attach rw.
//     Cell viewers are pinned ro by trusted principal, including mint-time and
//     manual attaches; mutations fail forbidden_right. Holds yield busy_preview.
//   * Writers serialize per document. Explicit caller-supplied revision guards
//     remain engine-enforced; the registry does not invent stale CAS guards.
//   * Revision sniff + event fanout (postCall): mutating responses already
//     carry top-level "revision" (ops_response, core/doc/session.cpp) plus
//     tess.aicm_path / rebuild.feature_status; when a doc's revision advances
//     the registry pushes {"event":"doc.changed",...} to every OTHER
//     subscribed client (cell subscribers must also be attached). Cell writes
//     include trusted actor_key in snapshots, changes and lastWriter attribution.
//     doc.opened / doc.closed / doc.lease_change follow the same visibility gate.
//     EXCEPTION — internal docs: a doc minted with {"internal":true} (the agent
//     preflight advisor's shadow clone) is registered like any other but NEVER
//     produces a single event, so no mirror ever opens a tab for it. See
//     DocState::internal.
//   * _foreign_edits piggyback (postCall): when a client's call touches a doc
//     another client advanced since this client's last touch, the registry
//     appends "_foreign_edits":[...] into the RESULT object.
//
// MUTATING-METHOD CLASSIFICATION: desktop retains its document-only allowlist,
// including geometry-only sim/agent families. Cell authorization uses an exact
// fail-closed native read list plus the server's trusted cell_methods read policy;
// broad agent_/sim_ prefixes never confer cell read privileges.
#pragma once

#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

namespace hostd {

class DocRegistry {
public:
    // Sends one event line to client `clientId` (Server locks that conn's
    // writeMtx). Installed once by Server before the accept loop starts.
    using EventSender = std::function<void(int clientId, const std::string& line)>;

    void setEventSender(EventSender s) { send_ = std::move(s); }

    // Register hello's optional process-wide writer identity. Attachments and event
    // subscriptions remain connection-local; edit/CAS ownership is group-wide.
    void setWriterGroup(int clientId, const std::string& writerGroup);

    // Trusted gateway/listener identity, never client-declared. Viewers stay ro
    // across explicit attaches, implicit reads and mint-time registration.
    void setCellPrincipal(int clientId, const std::string& actorKey, bool readOnly);

    // Daemon-level methods (attach_doc/detach_doc/edit_hold/edit_release/
    // subscribe). Returns true if `method` was one of ours; result/isError as
    // in Server::dispatch.
    bool handleMethod(int clientId, const std::string& kind, const std::string& method,
                      const nlohmann::json& params, nlohmann::json& result, bool& isError);

    // Policy gate before an engine passthrough. Returns false with err filled
    // ({"code","message",...}) when the call must be rejected.
    // `userId`/`userDisplay` are the connection's optional multi-user identity
    // (from hello params.user); empty when unset. `warning` (when set) is appended
    // to the successful result by the caller — the implicit-attach "doc has
    // another writer" path. cellPolicyRead is a trusted server policy decision,
    // never taken from client params; ignored for desktop connections.
    bool preCall(int clientId, const std::string& kind, const std::string& userId,
                 const std::string& userDisplay, const std::string& method,
                 nlohmann::json& params, nlohmann::json& err, std::string& warning,
                 bool cellPolicyRead = false);

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
    // other_writer reports rw attachments outside the caller's writer group.
    void annotateDocList(nlohmann::json& result, int clientId);

    bool humanDirty(const std::string& docId) const;
    int savedRevision(const std::string& docId) const;
    bool closeBlockedBy(const std::string& docId, int requester) const;
    void restoreRecoveryState(const std::string& docId, const std::string& originKind,
                              bool humanDirty, long revision, long savedRevision);

    // Serialize document writes and guarded saves through their postCall bookkeeping.
    struct MutationGuard {
        std::shared_ptr<std::recursive_mutex> mutex;
        std::unique_lock<std::recursive_mutex> lock;
    };
    MutationGuard lockMutation(const std::string& docId);
    static bool requiresMutationLock(const std::string& method);
    void notifyDiskConflict(const std::string& docId, const std::string& path,
                            const nlohmann::json& fileStamp);

    // A turn owns every non-internal document it opens or touches, independently
    // of the lifetime or access mode of any individual tool connection.
    void startAgentRun(const std::string& scope, int clientId,
                       const std::string& actorKey, const std::string& docId);
    void joinAgentRun(int clientId, const std::string& scope, const std::string& actorKey);
    void endAgentRun(const std::string& scope);

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

    // Cache verification refreshes the snapshot without changing the revision.
    // Derived assembly revisions are published as system writes.
    void publishCacheRefresh(const std::string& docId, const nlohmann::json& response);

    // Disconnect drops connection-local attachments, edit-holds and subscriptions.
    // Run-owned documents remain held until their owning turns end; only then
    // does their ordinary (or agent-origin) orphan grace countdown start.
    void onDisconnect(int clientId, const std::string& kind);

    // Periodic housekeeping: expire stale edit-holds and return documents whose
    // GC grace expired with neither an attached client nor a live agent run.
    std::vector<std::string> tick();

    // Reap only an unchanged abandonment generation. closeFn executes under
    // the attachment lock and must not call back into the registry.
    bool reapToken(const std::string& docId, std::uint64_t& token) const;
    bool closeAbandoned(const std::string& docId, std::uint64_t token,
                        const std::string& reason, const std::function<bool()>& closeFn);

    // True while `clientId` still holds at least one attachment. A connection with no
    // attachments, no in-flight call and no recent traffic is a PARKED connection, not
    // work: Server::idleExpired refuses to let one pin the daemon forever.
    bool hasAttachments(int clientId);
    // Fail-closed read classification for cell (tenant) authorization. Methods not
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
        std::set<std::string> agentRuns;  // live turn scopes, independent of sockets
        // A tool socket can leave while its turn is still editing this document.
        std::string deferredLastWriterKind;
        Hold hold;
        long revision = -1;  // last revision the daemon observed for this doc
        long publishedRevision = -1;
        long savedRevision = -1;
        long lastHumanRevision = -1;
        long metadataRevision = -1;
        long publishedMetadataRevision = -1;
        long savedMetadataRevision = -1;
        long lastHumanMetadataRevision = -1;
        bool humanDirty = false;
        void refreshHumanDirty() {
            humanDirty = lastHumanRevision > savedRevision ||
                         lastHumanMetadataRevision > savedMetadataRevision;
        }
        std::uint64_t attachmentGeneration = 0;
        // A write can finish before the minting call returns. Its event waits until
        // origin/internal status is known, so a private mint cannot leak.
        std::vector<std::pair<nlohmann::json, int>> pendingChanges;
        int lastWriterClient = -1;
        // Preserve group attribution even after the source connection disconnects.
        std::string lastWriterGroup;
        std::map<std::string, long> groupLastSeenRev;
        std::string lastWriterKind;
        std::string lastWriterMethod;
        // Multi-user identity of the last writer (from hello params.user). Empty
        // strings when the writer did not declare a user (local single-seat).
        std::string lastWriterUserId;
        std::string lastWriterUserDisplay;
        std::string lastWriterActorKey;
        // Kind of the client that minted this doc (gui|mcp|agent|test). Immutable
        // once set — the origin of ownership (user vs AI). Empty = unknown.
        std::string originKind;
        // Explicit presentation is independent of editing, attachment and persistence.
        bool presented = false;
        std::string name;
        // Internal doc: minted with {"internal":true} and deliberately invisible on
        // the wire — the registry NEVER fans out doc.opened/doc.changed/doc.closed
        // (nor lease_change/history_cleared) for it. WHY: the agent preflight advisor
        // mints a shadow clone of the live part inside a single add_features call and
        // closes it a few RPCs later; mirroring that clone made the GUI open a
        // throwaway part tab for every operation the AI performed. Internal
        // clones do not take a run hold, so their normal cleanup still works.
        bool internal = false;
        // Doc-GC deadline, armed only when no client or live run holds the doc.
        long long orphanDeadlineMs = 0;
        // The last doc.changed payload fanned out for this doc (tree + aicm_path +
        // owner_map + datums + feature_status). Served verbatim by doc_snapshot.
        // WHY it is kept: a GUI mirror tab that has not yet received a push had no way
        // to populate itself except doc_activate, which takes the doc's per-doc engine
        // mutex (DocLock) and therefore QUEUES BEHIND THE AGENT'S WRITE BURST.
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
    // Start the GC countdown only after all attachments and run holds leave.
    // Agent/mcp-origin docs get a longer grace; a disabled GC is a no-op.
    static void updateOrphanDeadlineLocked(DocState& d);
    void rememberAgentRunLocked(DocState& d, int clientId);
    int rwCountLocked(const DocState& d) const;
    const std::string& writerGroupLocked(int clientId) const;
    bool sameWriterLocked(int a, int b) const;
    long& lastSeenRevLocked(DocState& d, int clientId);
    void forgetDetachedGroupLocked(DocState& d, int clientId);
    // Queue an event for every subscriber of `docId` except `exceptClient`. Drops the
    // event outright when the doc is internal (see DocState::internal).
    void fanoutLocked(const std::string& docId, const nlohmann::json& evt, int exceptClient,
                      std::vector<PendingEvent>& out);
    void flush(std::vector<PendingEvent>& pending);
    // Expire the hold on `d` if past deadline; queues the lease_change event.
    void expireHoldLocked(const std::string& docId, DocState& d,
                          std::vector<PendingEvent>& out);

    mutable std::mutex mtx_;
    std::mutex mutationMtx_;
    std::map<std::string, std::weak_ptr<std::recursive_mutex>> mutationLocks_;
    std::map<std::string, DocState> docs_;
    // Late postCall responses must not recreate a document closed in the meantime.
    std::set<std::string> closedIds_;
    std::map<int, std::string> writerGroups_;
    std::map<std::string, std::string> agentRuns_;  // scope -> authenticated actor
    std::map<int, std::vector<std::string>> clientAgentRuns_;  // socket -> nested scopes
    struct CellPrincipal {
        std::string actorKey;
        bool readOnly = false;
    };
    std::map<int, CellPrincipal> cellPrincipals_;
    // client_id -> subscribed doc ids; the "*" entry means everything.
    std::map<int, std::set<std::string>> subs_;
    EventSender send_;
};

}  // namespace hostd
