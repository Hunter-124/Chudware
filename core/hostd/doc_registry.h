// Doc access registry + event bus for aicad-hostd (Phase 3 of the shared-engine
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
//   * _foreign_edits piggyback (postCall): when a client's call touches a doc
//     another client advanced since this client's last touch, the registry
//     appends "_foreign_edits":[...] into the RESULT object.
//
// MUTATING-METHOD CLASSIFICATION (the documented choice): enforcement applies
// to any engine method whose params carry a "doc_id" and whose name is NOT in
// the read-only allowlist below (exact names + prefixes). The allowlist covers
// every pure-read family in core/api/libaicad.cpp dispatch: doc_tree/doc_query/
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
    // call must be rejected (read_only / busy_preview).
    // `warning` (when set) is appended to the successful result by the caller —
    // the implicit-attach "doc has another writer" path.
    bool preCall(int clientId, const std::string& kind, const std::string& method,
                 nlohmann::json& params, nlohmann::json& err, std::string& warning);

    // Response post-processing after a SUCCESSFUL engine call: revision sniff,
    // implicit attach of minted docs, event fanout, _foreign_edits injection.
    void postCall(int clientId, const std::string& kind, const std::string& method,
                  const nlohmann::json& params, nlohmann::json& result);

    // Client disconnected: drop its attachments/holds/subscriptions and notify
    // remaining subscribers (doc.lease_change). Docs left with NO attached
    // client start the doc-GC grace countdown (AICAD_DOC_GC_GRACE_S, default
    // 90 s; AICAD_DOC_GC=0 disables) — swept by tick().
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
        // Doc-GC: when > 0, no client is attached and the doc closes once this
        // deadline passes without a new attach (zombie-doc prevention).
        long long orphanDeadlineMs = 0;
    };
    struct PendingEvent {
        int target;
        std::string line;
    };

    static bool isReadOnlyMethod(const std::string& method);
    static long sniffRevision(const nlohmann::json& result);
    int holdSecs() const;
    // Start/refresh the GC countdown when `d` has no attachments; clear it when
    // it does. No-op when doc-GC is disabled.
    static void updateOrphanDeadlineLocked(DocState& d);
    int rwCountLocked(const DocState& d) const;
    // Queue an event for every subscriber of `docId` except `exceptClient`.
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
