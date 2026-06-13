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
#include <unordered_map>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

class WorkerClient;

namespace core {

// W1-B (item 6): the topology a single feature owns, parsed from a response's "owner_map".
// Shape on the wire: {"<fid>": {"bodies":[id...], "faces":[[body,face]...], "edges":[[body,edge]...]}}.
struct Owned {
    std::vector<uint32_t> bodies;                       // body ids this feature created/owns
    std::vector<std::pair<uint32_t, uint32_t>> faces;   // (body_id, face_id)
    std::vector<std::pair<uint32_t, uint32_t>> edges;   // (body_id, edge_id)
};

// Represents an ops-response (doc_apply_ops / doc_undo / doc_redo).
// The caller should feed tess.aicm_path into AppState::loadDocMesh().
struct OpsResponse {
    bool ok = false;
    int  revision = -1;
    nlohmann::json tree;              // full doc JSON (aicad.part/1 | aicad.asm/1)
    nlohmann::json feature_status;   // [{id,ok,ms,error,dangling_refs}]
    nlohmann::json id_map;           // {"$0":"f9", …} batch placeholder -> assigned id (C17)
    std::string aicm_path;           // tess.aicm_path (empty if no tess block)
    std::string error_code;          // set on RPC error (stale_revision, invalid_op, …)
    std::string error_message;       // human-readable error
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
    // Listener called after every mutation that succeeds (on the calling thread).
    using Listener = std::function<void()>;
    void setListener(Listener l) { listener_ = std::move(l); }

    // W1-B (item 2/6): notified when an op was discarded because the mirror was stale and we had to
    // auto-resync (the caller's action did NOT apply). app.cpp routes this to a toast so the user
    // learns their action was dropped instead of silently failing. Fired on the calling thread.
    using StaleCb = std::function<void()>;
    void setStaleResyncCallback(StaleCb cb) { staleResyncCb_ = std::move(cb); }

    // ---- accessors ----
    bool        hasDoc()   const { return !docId_.empty(); }
    std::string docId()    const { return docId_; }
    int         revision() const { return revision_; }

    // Full document JSON (last known tree from the worker). Empty object until a successful
    // new/open or the first ops-response. Use this to drive the feature tree.
    const nlohmann::json& tree() const { return tree_; }

    // Last rebuild feature_status array from the worker response.
    const nlohmann::json& featureStatus() const { return featureStatus_; }

    // ---- W1-B (item 6): owner-map lookups ----
    // The parsed feature->topology map from the latest response that carried one. TOLERANT of the
    // engine not (yet) sending "owner_map": the map is simply empty and the lookups return nothing.

    // Which feature (if any) owns this face. Empty string when unknown.
    std::string ownerOfFace(uint32_t body_id, uint32_t face_id) const {
        auto it = faceOwner_.find(((uint64_t)body_id << 32) | (uint64_t)face_id);
        return it == faceOwner_.end() ? std::string() : it->second;
    }
    // Which feature (if any) owns this edge. Empty string when unknown.
    std::string ownerOfEdge(uint32_t body_id, uint32_t edge_id) const {
        auto it = edgeOwner_.find(((uint64_t)body_id << 32) | (uint64_t)edge_id);
        return it == edgeOwner_.end() ? std::string() : it->second;
    }
    // Every feature that owns this body (most bodies have exactly one creator; patterns/booleans can
    // touch several). Empty vector when unknown.
    std::vector<std::string> ownerOfBody(uint32_t body_id) const {
        auto it = bodyOwners_.find(body_id);
        return it == bodyOwners_.end() ? std::vector<std::string>() : it->second;
    }
    // True when the worker has supplied an owner map at least once (lets callers fall back gracefully
    // before the engine lands the feature).
    bool hasOwnerMap() const { return !faceOwner_.empty() || !bodyOwners_.empty(); }

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

    // C37-F: Checkpoint / revert helpers for the PropertyManager cancel path.
    // checkpoint() issues doc_checkpoint and returns the opaque checkpoint id string (empty on
    // failure). revert() issues doc_revert, parses the full ops-style response (tree / revision /
    // feature_status / owner_map), updates the mirror, fires the listener, and returns the response
    // so the PM can refresh the mesh from aicm_path. Both are no-ops when no doc is open.
    std::string checkpoint(WorkerClient& w);
    OpsResponse revert(WorkerClient& w, const std::string& checkpointId);

    // Resolve a viewport pick to a typed selector string.
    // Returns the selector JSON string, or empty on error (err filled).
    std::string resolvePick(WorkerClient& w, uint32_t body_id,
                            const std::string& kind, uint32_t topo_id,
                            std::string& err);

    // Save / open (.step+ path). On open: updates docId/revision/tree.
    bool save(WorkerClient& w, const std::string& path, std::string& err);
    bool open(WorkerClient& w, const std::string& path, std::string& err);

    // List open docs (returns array or empty on error).
    nlohmann::json list(WorkerClient& w, std::string& err);

    // Re-pull the document tree + revision from the worker (doc_tree) and fire the listener.
    // C24: the public face of the formerly-private resyncTree — callers that mutated the doc
    // out-of-band (sketch drag/commit/exit, a v2 agent run) use this to bring the mirror back in
    // step instead of the old throwaway resolvePick(1,"body",1) trick. Returns false if no doc is
    // open or the call fails; on success the listener fires so the tree repaints.
    bool resync(WorkerClient& w);

    // Reset local state (e.g. on worker restart).
    void reset() { docId_.clear(); revision_ = -1; tree_ = {}; featureStatus_ = {}; }

    // Owned topology for each feature (raw, as last parsed). Exposed for completeness / debugging.
    const std::unordered_map<std::string, Owned>& ownerMap() const { return ownerMap_; }

private:
    OpsResponse parseOpsResponse(const nlohmann::json& result);
    bool resyncTree(WorkerClient& w);  // called on stale_revision
    void notify() { if (listener_) listener_(); }

    // W1-B (item 6): (re)build ownerMap_ + the reverse lookups from a response's "owner_map" object.
    // TOLERATES a missing/null/empty owner_map: in that case the maps are cleared and lookups
    // return nothing, so the host can build and run before the engine ships the field.
    void storeOwnerMap(const nlohmann::json& ownerMapJson);

    std::string    docId_;
    int            revision_ = -1;
    nlohmann::json tree_;
    nlohmann::json featureStatus_;
    Listener       listener_;
    StaleCb        staleResyncCb_;  // W1-B: fired on stale_revision auto-resync

    // W1-B: parsed owner map + reverse indices (key = (body<<32)|topo for faces/edges).
    std::unordered_map<std::string, Owned> ownerMap_;          // fid -> owned topology
    std::unordered_map<uint64_t, std::string> faceOwner_;      // (body<<32)|face -> fid
    std::unordered_map<uint64_t, std::string> edgeOwner_;      // (body<<32)|edge -> fid
    std::unordered_map<uint32_t, std::vector<std::string>> bodyOwners_;  // body -> [fid...]
};

}  // namespace core
