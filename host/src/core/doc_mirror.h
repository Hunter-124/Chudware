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

#include <functional>
#include <string>

#include <nlohmann/json.hpp>

class WorkerClient;

namespace core {

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
};

class DocMirror {
public:
    // Listener called after every mutation that succeeds (on the calling thread).
    using Listener = std::function<void()>;
    void setListener(Listener l) { listener_ = std::move(l); }

    // ---- accessors ----
    bool        hasDoc()   const { return !docId_.empty(); }
    std::string docId()    const { return docId_; }
    int         revision() const { return revision_; }

    // Full document JSON (last known tree from the worker). Empty object until a successful
    // new/open or the first ops-response. Use this to drive the feature tree.
    const nlohmann::json& tree() const { return tree_; }

    // Last rebuild feature_status array from the worker response.
    const nlohmann::json& featureStatus() const { return featureStatus_; }

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
    OpsResponse applyOps(WorkerClient& w, const nlohmann::json& ops);

    // Undo / redo the most recent batch. Return the ops-response (same shape as applyOps).
    OpsResponse undo(WorkerClient& w);
    OpsResponse redo(WorkerClient& w);

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

private:
    OpsResponse parseOpsResponse(const nlohmann::json& result);
    bool resyncTree(WorkerClient& w);  // called on stale_revision
    void notify() { if (listener_) listener_(); }

    std::string    docId_;
    int            revision_ = -1;
    nlohmann::json tree_;
    nlohmann::json featureStatus_;
    Listener       listener_;
};

}  // namespace core
