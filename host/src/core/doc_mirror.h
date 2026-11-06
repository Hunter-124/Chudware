// DocMirror (C16): in-process mirror of the worker-managed document.
//
// Holds the current document JSON + revision. All mutations go through the worker via the
// existing serialized WorkerClient (callMtx_ inside the client serializes concurrent calls).
// For M1, doc_* calls are fast (no agent loop) so they are made synchronously from the UI
// thread while a "busy" guard prevents concurrent job launching.
//
// After every successful ops-response (apply_ops / undo / redo) the listener callback is
// invoked on the same thread so the feature tree can refresh.
#pragma once

#include <cstdint>
#include <functional>
#include <string>
#include <memory>
#include <unordered_map>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

class WorkerClient;

namespace core {

class AsyncRpc;  // async_rpc.h — non-blocking worker round-trips (applyOpsAsync)

// W1-B (item 6): the topology a single feature owns, parsed from a response's "owner_map".
// Shape on the wire: {"<fid>": {"bodies":[id...], "faces":[[body,face]...], "edges":[[body,edge]...]}}.
struct Owned {
    std::vector<uint32_t> bodies;                       // body ids this feature created/owns
    std::vector<std::pair<uint32_t, uint32_t>> faces;   // (body_id, face_id)
    std::vector<std::pair<uint32_t, uint32_t>> edges;   // (body_id, edge_id)
};

struct DocMirrorPayload;

// Complete host-side snapshot of an already-open document mirror. Document-sized data is
// immutable and shared: copying/capturing/restoring never walks the tree or topology.
struct DocMirrorSnapshot {
    std::string docId;
    int revision = -1;
    std::shared_ptr<const DocMirrorPayload> payload;
    bool valid() const { return !docId.empty() && revision >= 0 && payload; }
    const nlohmann::json& tree() const;
};

// Represents an ops-response (doc_apply_ops / doc_undo / doc_redo).
// The caller should feed tess.aicm_path into AppState::loadDocMesh().
struct OpsResponse {
    bool ok = false;                 // only an accepted response, never a failed candidate
    bool rejected = false;           // a rebuild or known validation refusal
    bool unchanged = false;          // known atomic refusal at this request's current base revision
    bool has_tess = false;           // usable path, or explicit empty path with empty bodies
    int  revision = -1;
    nlohmann::json tree;             // response tree (restored live tree on atomic rollback)
    nlohmann::json feature_status;   // rejected candidate diagnostics when rejected, otherwise live
    nlohmann::json id_map;           // {"$0":"f9", …} batch placeholder -> assigned id (C17)
    nlohmann::json auto_rollback;     // committed downstream failures and hidden suffix
    std::string warning;              // non-fatal committed-state warning
    std::string aicm_path;           // tess.aicm_path (empty if no tess block)
    std::string error_code;          // RPC code, or rebuild_rejected for an in-band failure
    std::string error_message;       // human-readable error
    nlohmann::json error_detail;     // structured worker error detail, if supplied
    nlohmann::json owner_map;        // W1-B: raw "owner_map" object when present (else null)
};

// Instant3D push-pull result (doc_push_pull). Carries the push-pull verdict + readout fields on
// TOP of an embedded OpsResponse (the revision/tree/tess/owner_map block, present only when the
// face is draggable). When draggable==false NO doc mutation happened; `reason` explains why.
struct PushPullResult {
    bool        ok = false;          // transport/RPC succeeded (false => err in error_message)
    bool        draggable = false;   // the picked face mapped to a single drivable param
    bool        committed = false;   // this call pushed one undo entry (commit=true)
    bool        clamped = false;     // the new value was clamped to the engine's min dimension
    std::string reason;              // non-draggable reason (draggable==false)
    std::string feature_id;          // owning feature id (when known)
    std::string feature_type;        // box | extrude | cylinder | hole | plane | ...
    std::string param;               // driven param name (length/width/height/depth/radius/...)
    std::string axis;                // x|y|z|radial|axial|normal|n (informational)
    double      old_value = 0.0;
    double      new_value = 0.0;
    double      distance = 0.0;      // the signed mm distance applied (echoed back)
    std::string error_code;          // transport/rpc error code (empty on success)
    std::string error_message;       // human-readable error
    OpsResponse ops;                 // embedded ops-response (valid when draggable==true)
};

class DocMirror {
public:
    DocMirror();
    // Listener called whenever a tree/snapshot is adopted (on the calling thread).
    // revisionChanged is true only when the SAME document has a different native revision;
    // an unchanged refresh or a binding to another document is not a document mutation.
    using Listener = std::function<void(bool revisionChanged)>;
    void setListener(Listener l) { listener_ = std::move(l); }

    // W1-B (item 2/6): notified when an op was discarded because the mirror was stale and we had to
    // auto-resync (the caller's action did NOT apply). app.cpp routes this to a toast so the user
    // learns their action was dropped instead of silently failing. Fired on the calling thread.
    using StaleCb = std::function<void()>;
    void setStaleResyncCallback(StaleCb cb) { staleResyncCb_ = std::move(cb); }
    // Fired after accepted ops are ingested, before the normal listener and Done.
    // revisionChanged compares to the submitted base (or the previous mirror revision
    // for externally guarded ingestion), not to a prior tree-only resync.
    using AcceptedOpsObserver = std::function<void(const OpsResponse&, bool revisionChanged)>;
    void setAcceptedOpsObserver(AcceptedOpsObserver observer) {
        acceptedOpsObserver_ = std::move(observer);
    }
    // A superseded mutation may have committed or have an uncertain transport outcome.
    // This is an origin hint only; consumers must fetch current state rather than ingest
    // the superseded payload into another document.
    using SupersededOpsObserver = std::function<void(const std::string& docId, int baseRevision)>;
    void setSupersededOpsObserver(SupersededOpsObserver observer) {
        supersededOpsObserver_ = std::move(observer);
    }
    // Track pending human operations independently of the currently displayed document.
    using PendingOpsObserver = std::function<void(const std::string& docId, bool pending)>;
    void setPendingOpsObserver(PendingOpsObserver observer) {
        pendingOpsObserver_ = std::move(observer);
    }
    // A successful response can arrive after the originating document was switched away.
    using HumanMutationObserver = std::function<void(const std::string& docId, int revision)>;
    void setHumanMutationObserver(HumanMutationObserver observer) {
        humanMutationObserver_ = std::move(observer);
    }


    // ---- accessors ----
    bool        hasDoc()   const { return !docId_.empty(); }
    const std::string& docId() const { return docId_; }
    int         revision() const { return revision_; }
    uint64_t generation() const { return generation_; }
    // Cached from the accepted revision's body metadata or positive part volume.
    // Unknown is distinct from a known all-surface/empty document.
    bool solidPresenceKnown() const;
    bool hasSolidBody() const;

    // Full document JSON (last known tree from the worker). Empty object until a successful
    // new/open or the first ops-response. Use this to drive the feature tree.
    const nlohmann::json& tree() const;

    // Last rebuild feature_status array and whether it describes this revision.
    const nlohmann::json& featureStatus() const;
    bool featureStatusCurrent() const;

    // Displayable reference geometry (datum axes/planes/points/csys) from the latest response, as a
    // raw JSON array (see core/doc/session.cpp ops_response "datums" for the per-entry shape). The
    // renderer draws these as overlays — a datum produces no body, so without this it is invisible.
    // Empty for docs with no datum features (or an engine that doesn't ship the field).
    const nlohmann::json& refDatums() const;

    // Capture/restore an immutable local revision for instant already-open tab binds. Restore fires
    // the normal listener so every tree consumer observes the same state as a worker activation.
    DocMirrorSnapshot snapshot() const;
    bool restoreSnapshot(const DocMirrorSnapshot& snapshot);

    // W12+ — drawings restored by the most recent doc_open: an array of {doc_id, name,
    // source_doc_id}, one per embedded drawing the engine re-registered (their `source` already
    // re-pointed to THIS reopened doc). Empty for a file with no drawings. The open caller
    // (applyOpenedDoc) reads this once to re-create the Drawing tabs, then it is reset on the next
    // open/new. Additive + tolerant: stays empty if the engine omits the field.
    const nlohmann::json& lastOpenedDrawings() const { return lastOpenedDrawings_; }

    // Mesh path + body volume from the most recent doc_open response. The open caller can
    // load its mesh directly; without a path it uses the warm empty result when available,
    // otherwise requests doc_activate, which reads the warm compile cache when possible.
    // Reset per open.
    const std::string& lastOpenedAicm() const { return lastOpenedAicm_; }
    // True only when the engine explicitly returned a usable path, including
    // an empty path that represents a genuinely empty model.
    bool lastOpenedMeshUsable() const { return lastOpenedMeshUsable_; }
    double lastOpenedVolume() const { return lastOpenedVolume_; }

    // How the most recent doc_open was served, from the engine's response (reset per open):
    //   lastOpenedCacheReported — the engine sent `cache_hit` at all (older engines don't; the UI
    //                             then says nothing about the cache rather than guessing).
    //   lastOpenedCacheHit      — geometry/mesh came from the .step+'s saved build cache.
    //   lastOpenedWarm          — the engine session's compiled state is hot, so a follow-up
    //                             doc_activate/doc_rebuild would only repeat work already done.
    //   lastOpenedCacheMissReason — why the saved cache was not used (snake_case; empty on a hit).
    bool lastOpenedCacheReported() const { return lastOpenedCacheReported_; }
    bool lastOpenedCacheHit() const { return lastOpenedCacheHit_; }
    bool lastOpenedWarm() const { return lastOpenedWarm_; }
    const std::string& lastOpenedCacheMissReason() const { return lastOpenedCacheMissReason_; }
    //   lastOpenedWriteback — the open rebuilt and saved that build into the file (1), tried and
    //                         was refused (-1, reason in lastOpenedWritebackReason), or did not
    //                         try (0) — contracts/stepplus.md §17.
    int lastOpenedWriteback() const { return lastOpenedWriteback_; }
    const std::string& lastOpenedWritebackReason() const { return lastOpenedWritebackReason_; }

    // ---- W1-B (item 6): owner-map lookups ----
    // The parsed feature->topology map from the latest response that carried one. TOLERANT of the
    // engine not (yet) sending "owner_map": the map is simply empty and the lookups return nothing.

    // Which feature (if any) owns this face. Empty string when unknown.
    std::string ownerOfFace(uint32_t body_id, uint32_t face_id) const;
    // Which feature (if any) owns this edge. Empty string when unknown.
    std::string ownerOfEdge(uint32_t body_id, uint32_t edge_id) const;
    // Every feature that owns this body (most bodies have exactly one creator; patterns/booleans can
    // touch several). Empty vector when unknown.
    std::vector<std::string> ownerOfBody(uint32_t body_id) const;
    // ---- mutations (synchronous, blocking the calling thread) ----

    // Create a new empty part doc. Returns true + fills docId / revision on success.
    bool newPart(WorkerClient& w, std::string& err);

    // --- C20b: assembly support ---
    // Create a new empty assembly doc (doc_new {kind:"assembly"}). Like newPart but the worker
    // builds an aicad.asm/1 document.
    bool newAssembly(WorkerClient& w, std::string& err);

    // Resolve a viewport pick in an ASSEMBLY: doc_resolve_pick returns {selector, component} so the
    // host can build a mate ref {component, selector}. Returns the selector JSON string and fills
    // *component with the component id (k-id). Empty return on error (err filled).
    std::string resolvePickAsm(WorkerClient& w, uint32_t body_id,
                               const std::string& kind, uint32_t topo_id,
                               std::string& component, std::string& err);

    // Apply a batch of ops. base_revision must equal revision() or stale_revision is returned.
    // On stale_revision: auto-resyncs via doc_tree (revision & tree updated), logs a note to
    // the console string (caller should surface it), and returns the error so the caller can
    // decide to replay.
    // `coalesce` (undo grouping): when true the worker folds this batch into the previous batch's
    // undo step (no new undo snapshot). Set it on the 2nd..Nth applyOps of ONE user action that
    // can only be expressed as several batches (e.g. a rectangle's chained line ops), so a single
    // Ctrl+Z reverts the whole action.
    OpsResponse applyOps(WorkerClient& w, const nlohmann::json& ops, bool coalesce = false);

    // Non-blocking doc_apply_ops; Done runs on the GL thread in rpc.drain().
    // `current` is true for a still-owned response, including errors and rejections.
    // False means the completion is obsolete and must not be presented. Meshes and IDs
    // require BOTH current and r.ok. A matching tree-only N+1 resync may reconcile an
    // accepted response; foreign/newer or superseded document state is never overwritten.
    // stale_revision auto-resyncs through `w` and fires the stale callback.
    using ApplyAsyncDone = std::function<void(const OpsResponse& r, bool current)>;
    void applyOpsAsync(AsyncRpc& rpc, WorkerClient& w, const nlohmann::json& ops,
                       ApplyAsyncDone done, bool coalesce = false, int timeoutMs = 120000);

    // Monotonic counter bumped on every applyOpsAsync submission (and reset()). Callers that
    // need their own generation guard can snapshot this before submit.
    uint64_t mutationGen() const { return mutationGen_; }

    // Undo / redo the most recent batch. Return the ops-response (same shape as applyOps).
    OpsResponse undo(WorkerClient& w);
    OpsResponse redo(WorkerClient& w);

    // Instant3D push-pull: drag a face of `body_id` (1-based PickHit body) whose picked FACE is
    // `topo_id` (1-based TopExp face index) by `distance` mm SIGNED along the face outward normal.
    // `commit` false = transient preview (no undo entry); true = exactly one undo entry on release.
    // The host's live-drag envelope is: doc_checkpoint on press, then per sample doc_revert +
    // pushPull(commit=false, cumulative) so previews never compound, and on release doc_revert +
    // pushPull(commit=true, final). When the response is draggable, the embedded OpsResponse
    // (result.ops) updates the mirror EXACTLY like applyOps (revision/tree/owner_map refreshed,
    // listener fired) and result.ops.aicm_path drives loadDocMesh. When NOT draggable the document
    // is unchanged and result.reason explains why. A longer timeout is used because the first drag
    // sample triggers a full rebuild.
    PushPullResult pushPull(WorkerClient& w, uint32_t body_id, uint32_t topo_id,
                            double distance, bool commit);

    // GL-thread ingestion of externally guarded ops-shaped responses. A non-null
    // rejected_tree describes a rejected candidate; otherwise feature statuses describe
    // the committed tree, including historical failures.
    PushPullResult ingestPushPullResponse(const nlohmann::json& res);
    OpsResponse    ingestOpsResponse(const nlohmann::json& res);

    // Resolve a viewport pick to a typed selector string.
    // Returns the selector JSON string, or empty on error (err filled).
    std::string resolvePick(WorkerClient& w, uint32_t body_id,
                            const std::string& kind, uint32_t topo_id,
                            std::string& err);

    // The unique edge topo_ids bordering a face — for "fillet a whole face" (a face pick expands
    // into the face's edges). Empty on error / no doc.
    std::vector<uint32_t> faceEdges(WorkerClient& w, uint32_t body_id, uint32_t face_topo_id);

    // Save / open (.step+ path). On open: updates docId/revision/tree.
    // asSolid (optional): when true the doc_open request carries {"as_solid": true} so a mesh-format
    // source (.stl/.obj/.ply/.3mf/.gltf/.glb/.3dm) is sewn into a real B-rep solid by the engine
    // instead of imported as a graphics body (no-op / ignored for B-rep sources). Default false
    // keeps the request byte-identical for every existing caller.
    bool save(WorkerClient& w, const std::string& path, std::string& err);
    bool open(WorkerClient& w, const std::string& path, std::string& err, bool asSolid = false);

    // GL-thread adopt of a finished doc_open JSON (from AsyncRpc). Same post-conditions
    // as open() on success. Returns false if the envelope is an error or missing doc_id.
    bool adoptOpenResponse(const nlohmann::json& res, std::string& err);

    // Document tabs: re-point this mirror at an already-open worker document using
    // doc_activate. The engine serves its warm compiled snapshot when available and
    // rebuilds only on a cold cache. Returns tree/revision/owner_map and tess.aicm_path.
    bool activate(WorkerClient& w, const std::string& docId, std::string& aicmPath,
                  std::string& err);


    // Adopt a finished doc_activate JSON as activate() would (GL thread / drain).
    bool adoptActivateResponse(const std::string& docId, const nlohmann::json& res,
                               std::string& aicmPath, std::string& err);

    // List open docs (returns array or empty on error).
    nlohmann::json list(WorkerClient& w, std::string& err);

    // ---- Configurations (SolidWorks ConfigurationManager) ----
    // Generic config-op caller: issue `method` ({"config_add"|"config_delete"|"config_activate"|
    // "config_set_override"|"config_set_suppression"}) with {doc_id, ...extraParams}. These all
    // return the SAME ops-response shape applyOps does (revision/tree/rebuild/tess/owner_map), for
    // the now-active config, so this ingests it EXACTLY like applyOps: updates revision/tree/
    // feature_status/owner_map and fires the listener (so the FeatureManager tree repaints). The
    // caller feeds the returned OpsResponse::aicm_path into AppState::loadDocMesh so the viewport
    // updates. doc_id is injected automatically. No-op (no_doc) when no document is open.
    OpsResponse callConfig(WorkerClient& w, const std::string& method,
                           const nlohmann::json& extraParams);

    // Read-only config listing (config_list): returns the raw result
    // {configurations:[name...], active, detail:[{name, param_overrides, suppressions}]} or a null
    // json on error (err filled). Does NOT mutate the mirror.
    nlohmann::json configList(WorkerClient& w, std::string& err);

    // ---- Weldments cut list (SolidWorks Weldment Cut List) ----
    // Read-only passthrough of weld_cut_list: returns the raw result
    // {rows:[{item, profile, description, length_mm, quantity, total_length_mm}],
    //  total_members, total_length_mm, profiles:[...]} or a null json on error (err filled). A part
    // with no weld_member features (or an assembly) returns an empty rows list — NOT an error. Does
    // NOT mutate the mirror.
    nlohmann::json weldCutList(WorkerClient& w, std::string& err);

    // ---- 2D export (DXF / PDF) — drawing sheets + sheet-metal flat patterns (W14) ----
    // Both are read-only passthroughs that DON'T touch the mirror. They forward to the worker and
    // return the written absolute path (empty on error, err filled). `format` is "dxf" or "pdf".

    // export_flat_dxf: the ACTIVE part's flat-pattern outline (layer OUTLINE) + bend lines (layer
    // BEND) to `path`. Requires the open part to be sheet metal WITH a flat pattern (Flatten
    // feature); a clean error otherwise. Uses the mirror's current doc id (the open part).
    std::string exportFlat(WorkerClient& w, const std::string& path,
                           const std::string& format, std::string& err);

    // export_drawing: an open DRAWING's visible (layer VISIBLE) + hidden (layer HIDDEN) view
    // geometry to `path`. `drawingDocId` is a drawing registry id (NOT the part doc this mirror
    // tracks), so it's passed explicitly. Static-style: no mirror state is read or written.
    std::string exportDrawing(WorkerClient& w, const std::string& drawingDocId,
                              const std::string& path, const std::string& format, std::string& err);

    // ---- General 3D export (STEP / IGES / STL / 3MF / BREP) — File > Export ----
    // Read-only passthrough of export_file (engine/doc/formats.py:_handler_export_file): compiles
    // the document `docId`'s bodies and writes them to `path` in `format`. `format` is one of the
    // literal strings export_document accepts: "step" | "iges" | "stl" | "3mf" | "brep". Does NOT
    // touch the mirror. Returns the written absolute path, or "" on error
    // (err filled). When provided, result receives the complete worker response.
    // For mesh formats (stl/obj/ply/3mf/gltf/glb) the optional tessellation controls set
    // the export resolution: linDeflection (mm, or a fraction when `relative`),
    // angDeflection (radians), and `ascii` (STL container). The defaults reproduce the
    // historical hardcoded behaviour; B-rep formats ignore them.
    std::string exportFile(WorkerClient& w, const std::string& docId,
                           const std::string& path, const std::string& format, std::string& err,
                           double linDeflection = 0.05, double angDeflection = 0.35,
                           bool relative = false, bool ascii = false,
                           nlohmann::json* result = nullptr);

    // Live triangle/vertex estimate for the export dialog (export_tess_estimate RPC).
    // Returns true on success and fills tris/verts; false on error (err filled).
    bool exportEstimate(WorkerClient& w, const std::string& docId, double linDeflection,
                        double angDeflection, bool relative, long long& tris, long long& verts,
                        std::string& err);

    // Re-pull the document tree + revision from the worker (doc_tree) and fire the listener.
    // C24: the public face of the formerly-private resyncTree — callers that mutated the doc
    // out-of-band (sketch drag/commit/exit, a v2 agent run) use this to bring the mirror back in
    // step instead of the old throwaway resolvePick(1,"body",1) trick. Returns false if no doc is
    // open or the call fails; on success the listener fires so the tree repaints.
    bool resync(WorkerClient& w);

    // Adopt an async doc_tree response, preserving status/ownership/datums when omitted.
    // The caller guards the active document identity before delivering the response.
    bool ingestTreeResponse(const nlohmann::json& res);

    // Reset local state (e.g. on worker restart).
    void reset();

    // Parsed lazily from the latest response; shared by every snapshot of that revision.
    const std::unordered_map<std::string, Owned>& ownerMap() const;

private:
    static OpsResponse parseOpsResponse(const nlohmann::json& result);
    void storeResponseDatums(const nlohmann::json& result);
    void storeSolidPresence(const nlohmann::json& result, bool sameDocument);
    bool resyncTree(WorkerClient& w);  // called on stale_revision
    bool storeTreeResponse(const nlohmann::json& res);
    void notify() {
        ++generation_;
        const bool revisionChanged = !docId_.empty() && docId_ == notifiedDocId_ &&
                                     revision_ != notifiedRevision_;
        if (docId_ != notifiedDocId_) notifiedDocId_ = docId_;
        notifiedRevision_ = revision_;
        if (listener_) listener_(revisionChanged);
    }

    // Retain raw topology once; parsed ownership and compact reverse indices are built lazily.
    void storeOwnerMap(const nlohmann::json& ownerMapJson);
    void storeTree(const nlohmann::json& tree);
    void storeFeatureStatus(const nlohmann::json& status);
    void storeRefDatums(const nlohmann::json& datums);

    std::string    docId_;
    int            revision_ = -1;
    std::string    notifiedDocId_;
    int            notifiedRevision_ = -1;
    std::shared_ptr<const DocMirrorPayload> payload_;
    std::weak_ptr<const DocMirrorPayload> lastTreeOnlyPayload_;
    nlohmann::json lastOpenedDrawings_ = nlohmann::json::array();  // W12+ doc_open "drawings"
    std::string    lastOpenedAicm_;                                // doc_open tess.aicm_path
    bool           lastOpenedMeshUsable_ = false;
    double         lastOpenedVolume_ = 1.0;                        // doc_open "volume" (flat-2D probe)
    bool           lastOpenedCacheReported_ = false;               // doc_open carried "cache_hit"
    bool           lastOpenedCacheHit_ = false;                    // doc_open "cache_hit"
    bool           lastOpenedWarm_ = false;                        // doc_open "warm"
    std::string    lastOpenedCacheMissReason_;                     // doc_open "cache_miss_reason"
    int            lastOpenedWriteback_ = 0;                       // cache.writeback: 1 saved, -1 refused
    std::string    lastOpenedWritebackReason_;                     // cache.writeback.reason
    Listener       listener_;
    StaleCb        staleResyncCb_;  // W1-B: fired on stale_revision auto-resync
    AcceptedOpsObserver acceptedOpsObserver_;
    SupersededOpsObserver supersededOpsObserver_;
    PendingOpsObserver pendingOpsObserver_;
    HumanMutationObserver humanMutationObserver_;
    uint64_t       mutationGen_ = 0;  // latest-wins guard for applyOpsAsync

    uint64_t generation_ = 0;  // includes same-revision refreshes and local snapshot restores
};

}  // namespace core
