// DocMirror implementation (C16).
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <string>

#include "doc_mirror.h"
#include "async_rpc.h"
#include "../worker_client.h"
#include "cpp_core.h"  // strangler diagnostic: parity probe against libchudware.dll

namespace core {

// ---- helpers ----

static inline std::string jstr(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_string()) ? it->get<std::string>() : std::string();
}
static inline int jint(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_number_integer()) ? it->get<int>() : 0;
}

// ---- OpsResponse parser ----

OpsResponse DocMirror::parseOpsResponse(const nlohmann::json& result) {
    OpsResponse r;
    r.revision = jint(result, "revision");
    if (result.contains("tree") && result["tree"].is_object())
        r.tree = result["tree"];
    if (result.contains("rebuild") && result["rebuild"].is_object()) {
        const auto& rb = result["rebuild"];
        if (rb.contains("feature_status") && rb["feature_status"].is_array())
            r.feature_status = rb["feature_status"];
    }
    if (result.contains("tess") && result["tess"].is_object())
        r.aicm_path = jstr(result["tess"], "aicm_path");
    if (result.contains("id_map") && result["id_map"].is_object())
        r.id_map = result["id_map"];   // C17: add-feature flows find their new id here
    if (result.contains("owner_map") && result["owner_map"].is_object())
        r.owner_map = result["owner_map"];  // W1-B (item 6): feature -> owned topology (optional)
    // Datum reference geometry to draw (axes/planes/points/csys). Stored on the mirror here — the
    // single chokepoint every ops-response flows through — so all mutation paths (apply/undo/redo/
    // revert/push-pull/config) refresh it without each re-implementing the store. Absent (e.g.
    // assembly responses / older engine) leaves the last value; reset() / doc switch clears it.
    if (result.contains("datums") && result["datums"].is_array())
        refDatums_ = result["datums"];
    r.ok = true;
    return r;
}

// ---- W1-B (item 6): owner-map storage + reverse-index build ----
// Tolerant of absence: a missing/null/non-object owner_map clears the maps (lookups then no-op).
void DocMirror::storeOwnerMap(const nlohmann::json& ownerMapJson) {
    ownerMap_.clear();
    faceOwner_.clear();
    edgeOwner_.clear();
    bodyOwners_.clear();
    if (!ownerMapJson.is_object()) return;  // engine hasn't shipped it yet — fine.
    auto pairList = [](const nlohmann::json& arr,
                       std::vector<std::pair<uint32_t, uint32_t>>& out) {
        if (!arr.is_array()) return;
        for (const auto& e : arr) {
            if (e.is_array() && e.size() == 2 && e[0].is_number_integer() &&
                e[1].is_number_integer())
                out.emplace_back((uint32_t)e[0].get<long long>(), (uint32_t)e[1].get<long long>());
        }
    };
    for (auto it = ownerMapJson.begin(); it != ownerMapJson.end(); ++it) {
        const std::string& fid = it.key();
        const nlohmann::json& o = it.value();
        if (!o.is_object()) continue;
        Owned owned;
        if (auto b = o.find("bodies"); b != o.end() && b->is_array())
            for (const auto& e : *b)
                if (e.is_number_integer()) owned.bodies.push_back((uint32_t)e.get<long long>());
        if (auto f = o.find("faces"); f != o.end()) pairList(*f, owned.faces);
        if (auto e = o.find("edges"); e != o.end()) pairList(*e, owned.edges);
        // reverse indices
        for (uint32_t body : owned.bodies) bodyOwners_[body].push_back(fid);
        for (const auto& bf : owned.faces)
            faceOwner_[((uint64_t)bf.first << 32) | (uint64_t)bf.second] = fid;
        for (const auto& be : owned.edges)
            edgeOwner_[((uint64_t)be.first << 32) | (uint64_t)be.second] = fid;
        ownerMap_.emplace(fid, std::move(owned));
    }
}

DocMirrorSnapshot DocMirror::snapshot() const {
    DocMirrorSnapshot s;
    s.docId = docId_;
    s.revision = revision_;
    s.tree = tree_;
    s.featureStatus = featureStatus_;
    s.refDatums = refDatums_;
    s.ownerMap = ownerMap_;
    s.faceOwner = faceOwner_;
    s.edgeOwner = edgeOwner_;
    s.bodyOwners = bodyOwners_;
    return s;
}

bool DocMirror::restoreSnapshot(const DocMirrorSnapshot& s) {
    if (!s.valid()) return false;
    docId_ = s.docId;
    revision_ = s.revision;
    tree_ = s.tree;
    featureStatus_ = s.featureStatus;
    refDatums_ = s.refDatums;
    ownerMap_ = s.ownerMap;
    faceOwner_ = s.faceOwner;
    edgeOwner_ = s.edgeOwner;
    bodyOwners_ = s.bodyOwners;
    ++mutationGen_;  // any completion submitted for the leaving mirror is now superseded
    notify();
    return true;
}

// ---- resync on stale_revision ----

bool DocMirror::resyncTree(WorkerClient& w) {
    if (docId_.empty()) return false;
    nlohmann::json res;
    std::string err;
    if (!w.call("doc_tree", {{"doc_id", docId_}}, res, err)) return false;
    revision_ = jint(res, "revision");
    if (res.contains("tree") && res["tree"].is_object()) tree_ = res["tree"];
    if (res.contains("rebuild") && res["rebuild"].is_object()) {
        const auto& rb = res["rebuild"];
        if (rb.contains("feature_status") && rb["feature_status"].is_array())
            featureStatus_ = rb["feature_status"];
    }
    // W1-B (item 6): doc_tree may carry owner_map (top-level or under rebuild); refresh if present.
    if (res.contains("owner_map") && res["owner_map"].is_object())
        storeOwnerMap(res["owner_map"]);
    else if (res.contains("rebuild") && res["rebuild"].is_object() &&
             res["rebuild"].contains("owner_map") && res["rebuild"]["owner_map"].is_object())
        storeOwnerMap(res["rebuild"]["owner_map"]);
    // Refresh datum overlays when doc_tree carries them; leave the last value when absent (a cheap
    // tree pull may omit datums that are still valid from the prior full rebuild).
    if (res.contains("datums") && res["datums"].is_array())
        refDatums_ = res["datums"];
    else if (res.contains("rebuild") && res["rebuild"].is_object() &&
             res["rebuild"].contains("datums") && res["rebuild"]["datums"].is_array())
        refDatums_ = res["rebuild"]["datums"];
    return true;
}

// C24: public convenience wrapper — pull tree/revision and notify so the tree repaints.
bool DocMirror::resync(WorkerClient& w) {
    if (!resyncTree(w)) return false;
    notify();
    return true;
}

// ---- public API ----

bool DocMirror::newPart(WorkerClient& w, std::string& err) {
    nlohmann::json res;
    if (!w.call("doc_new", {{"kind", "part"}}, res, err)) return false;
    if (res.contains("code")) { err = jstr(res, "message"); return false; }
    docId_ = jstr(res, "doc_id");
    revision_ = jint(res, "revision");
    tree_ = {};
    featureStatus_ = {};
    refDatums_ = nlohmann::json::array();
    storeOwnerMap(nlohmann::json());
    notify();
    return true;
}

// --- C20b: assembly support ---

bool DocMirror::newAssembly(WorkerClient& w, std::string& err) {
    nlohmann::json res;
    if (!w.call("doc_new", {{"kind", "assembly"}}, res, err)) return false;
    if (res.contains("code")) { err = jstr(res, "message"); return false; }
    docId_ = jstr(res, "doc_id");
    revision_ = jint(res, "revision");
    if (res.contains("tree") && res["tree"].is_object()) tree_ = res["tree"];
    else tree_ = {};
    featureStatus_ = {};
    refDatums_ = nlohmann::json::array();
    storeOwnerMap(nlohmann::json());
    notify();
    return true;
}

std::string DocMirror::resolvePickAsm(WorkerClient& w, uint32_t body_id,
                                      const std::string& kind, uint32_t topo_id,
                                      std::string& component, std::string& err) {
    component.clear();
    err.clear();
    if (docId_.empty()) { err = "no document open"; return {}; }
    nlohmann::json res;
    nlohmann::json params = {
        {"doc_id", docId_},
        {"revision", revision_},
        {"body_id", body_id},
        {"kind", kind},
        {"topo_id", (int)topo_id}
    };
    if (!w.call("doc_resolve_pick", params, res, err)) return {};
    if (res.contains("code")) {
        err = jstr(res, "message");
        if (jstr(res, "code") == "stale_revision") resyncTree(w);
        return {};
    }
    component = jstr(res, "component");
    if (res.contains("selector") && res["selector"].is_object())
        return res["selector"].dump();
    err = "The selected topology no longer exists. Select it again.";
    return {};
}

OpsResponse DocMirror::applyOps(WorkerClient& w, const nlohmann::json& ops, bool coalesce) {
    OpsResponse r;
    nlohmann::json res;
    std::string err;
    nlohmann::json params = {
        {"doc_id", docId_},
        {"base_revision", revision_},
        {"ops", ops},
        {"coalesce", coalesce}
    };
    bool ok = w.call("doc_apply_ops", params, res, err);
    if (!ok) {
        r.error_code = "transport";
        r.error_message = err;
        return r;
    }
    // Check for error envelope {code, message, detail}
    if (res.contains("code")) {
        r.error_code = jstr(res, "code");
        r.error_message = jstr(res, "message");
        if (r.error_code == "stale_revision") {
            // Auto-resync and surface a note.
            std::printf("[doc] stale_revision — resyncing tree (now at rev %d)\n",
                        jint(res.value("detail", nlohmann::json::object()), "current_revision"));
            std::fflush(stdout);
            resyncTree(w);
            if (staleResyncCb_) staleResyncCb_();  // W1-B: surface a toast — action was discarded
            notify();                              // repaint the tree at the resynced revision
        }
        return r;
    }
    r = parseOpsResponse(res);
    if (r.ok) {
        revision_ = r.revision;
        if (!r.tree.is_null()) tree_ = r.tree;
        if (!r.feature_status.is_null()) featureStatus_ = r.feature_status;
        storeOwnerMap(r.owner_map);  // W1-B: refresh owner map (tolerates absence)
        notify();
    }
    return r;
}

void DocMirror::applyOpsAsync(AsyncRpc& rpc, WorkerClient& w, const nlohmann::json& ops,
                              ApplyAsyncDone done, bool coalesce, int timeoutMs) {
    OpsResponse early;
    if (docId_.empty()) {
        early.error_code = "no_doc";
        early.error_message = "no document open";
        if (done) done(early, false);
        return;
    }
    const uint64_t gen = ++mutationGen_;
    const std::string docId = docId_;
    const int baseRev = revision_;
    nlohmann::json params = {
        {"doc_id", docId},
        {"base_revision", baseRev},
        {"ops", ops},
        {"coalesce", coalesce}
    };
    // Capture `this` carefully: DocMirror is owned by AppState for the process lifetime of the
    // host, so it outlives AsyncRpc callbacks. Gen + docId guard supersession / tab switches.
    rpc.call("doc_apply_ops", std::move(params),
        [this, &w, gen, docId, done = std::move(done)](const RpcResult& rr) {
            OpsResponse r;
            const bool stillCurrent = (gen == mutationGen_ && docId_ == docId);
            if (!rr.ok) {
                r.error_code = "transport";
                r.error_message = rr.err;
                if (done) done(r, false);
                return;
            }
            if (rr.result.contains("code")) {
                r.error_code = jstr(rr.result, "code");
                r.error_message = jstr(rr.result, "message");
                if (stillCurrent && r.error_code == "stale_revision") {
                    std::printf("[doc] stale_revision (async) — resyncing tree\n");
                    std::fflush(stdout);
                    resyncTree(w);
                    if (staleResyncCb_) staleResyncCb_();
                    notify();
                }
                if (done) done(r, false);
                return;
            }
            r = parseOpsResponse(rr.result);
            if (!stillCurrent) {
                // Superseded by a newer applyOpsAsync or the user switched docs — do NOT
                // mutate the mirror / load mesh for this result.
                if (done) done(r, false);
                return;
            }
            if (r.ok) {
                revision_ = r.revision;
                if (!r.tree.is_null()) tree_ = r.tree;
                if (!r.feature_status.is_null()) featureStatus_ = r.feature_status;
                storeOwnerMap(r.owner_map);
                notify();
            }
            if (done) done(r, r.ok);
        }, timeoutMs);
}

OpsResponse DocMirror::undo(WorkerClient& w) {
    OpsResponse r;
    if (docId_.empty()) { r.error_code = "no_doc"; r.error_message = "no document open"; return r; }
    nlohmann::json res;
    std::string err;
    if (!w.call("doc_undo", {{"doc_id", docId_}}, res, err)) {
        r.error_code = "transport"; r.error_message = err; return r;
    }
    if (res.contains("code")) {
        r.error_code = jstr(res, "code"); r.error_message = jstr(res, "message"); return r;
    }
    r = parseOpsResponse(res);
    if (r.ok) {
        revision_ = r.revision;
        if (!r.tree.is_null()) tree_ = r.tree;
        if (!r.feature_status.is_null()) featureStatus_ = r.feature_status;
        storeOwnerMap(r.owner_map);  // W1-B
        notify();
    }
    return r;
}

OpsResponse DocMirror::redo(WorkerClient& w) {
    OpsResponse r;
    if (docId_.empty()) { r.error_code = "no_doc"; r.error_message = "no document open"; return r; }
    nlohmann::json res;
    std::string err;
    if (!w.call("doc_redo", {{"doc_id", docId_}}, res, err)) {
        r.error_code = "transport"; r.error_message = err; return r;
    }
    if (res.contains("code")) {
        r.error_code = jstr(res, "code"); r.error_message = jstr(res, "message"); return r;
    }
    r = parseOpsResponse(res);
    if (r.ok) {
        revision_ = r.revision;
        if (!r.tree.is_null()) tree_ = r.tree;
        if (!r.feature_status.is_null()) featureStatus_ = r.feature_status;
        storeOwnerMap(r.owner_map);  // W1-B
        notify();
    }
    return r;
}

// Instant3D push-pull (doc_push_pull). See the header for the live-drag envelope. -------------
PushPullResult DocMirror::ingestPushPullResponse(const nlohmann::json& res) {
    PushPullResult r;
    if (docId_.empty()) { r.error_code = "no_doc"; r.error_message = "no document open"; return r; }
    if (res.contains("code")) {
        r.error_code = jstr(res, "code"); r.error_message = jstr(res, "message");
        return r;
    }
    r.ok = true;
    r.draggable = res.value("draggable", false);
    r.reason        = jstr(res, "reason");
    r.feature_id    = jstr(res, "feature_id");
    r.feature_type  = jstr(res, "feature_type");
    if (!r.draggable) return r;   // no doc mutation — nothing to ingest

    r.committed = res.value("committed", false);
    r.clamped   = res.value("clamped", false);
    r.param     = jstr(res, "param");
    r.axis      = jstr(res, "axis");
    if (auto it = res.find("old_value"); it != res.end() && it->is_number())
        r.old_value = it->get<double>();
    if (auto it = res.find("new_value"); it != res.end() && it->is_number())
        r.new_value = it->get<double>();
    if (auto it = res.find("distance"); it != res.end() && it->is_number())
        r.distance = it->get<double>();

    r.ops = parseOpsResponse(res);
    if (r.ops.ok) {
        revision_ = r.ops.revision;
        if (!r.ops.tree.is_null()) tree_ = r.ops.tree;
        if (!r.ops.feature_status.is_null()) featureStatus_ = r.ops.feature_status;
        storeOwnerMap(r.ops.owner_map);
        notify();
    }
    return r;
}

OpsResponse DocMirror::ingestOpsResponse(const nlohmann::json& res) {
    OpsResponse r;
    if (docId_.empty()) { r.error_code = "no_doc"; r.error_message = "no document open"; return r; }
    if (res.contains("code")) {
        r.error_code = jstr(res, "code"); r.error_message = jstr(res, "message"); return r;
    }
    r = parseOpsResponse(res);
    if (r.ok) {
        revision_ = r.revision;
        if (!r.tree.is_null()) tree_ = r.tree;
        if (!r.feature_status.is_null()) featureStatus_ = r.feature_status;
        storeOwnerMap(r.owner_map);
        notify();
    }
    return r;
}

PushPullResult DocMirror::pushPull(WorkerClient& w, uint32_t body_id, uint32_t topo_id,
                                   double distance, bool commit) {
    PushPullResult r;
    if (docId_.empty()) { r.error_code = "no_doc"; r.error_message = "no document open"; return r; }
    nlohmann::json res;
    std::string err;
    nlohmann::json params = {
        {"doc_id", docId_},
        {"body_id", (int)body_id},
        {"topo_id", (int)topo_id},
        {"distance", distance},
        {"commit", commit}
    };
    // A drag sample triggers a full rebuild; give it more headroom than the doc_* default.
    if (!w.call("doc_push_pull", params, res, err, 60000)) {
        r.error_code = "transport"; r.error_message = err; return r;
    }
    if (res.contains("code")) {
        r.error_code = jstr(res, "code"); r.error_message = jstr(res, "message");
        if (r.error_code == "stale_revision") resyncTree(w);
        return r;
    }
    return ingestPushPullResponse(res);
}

std::string DocMirror::resolvePick(WorkerClient& w, uint32_t body_id,
                                   const std::string& kind, uint32_t topo_id,
                                   std::string& err) {
    err.clear();
    if (docId_.empty()) { err = "no document open"; return {}; }
    nlohmann::json res;
    nlohmann::json params = {
        {"doc_id", docId_},
        {"revision", revision_},
        {"body_id", body_id},
        {"kind", kind},
        {"topo_id", (int)topo_id}
    };
    if (!w.call("doc_resolve_pick", params, res, err)) return {};
    if (res.contains("code")) {
        err = jstr(res, "message");
        if (jstr(res, "code") == "stale_revision") resyncTree(w);
        return {};
    }
    // selector is returned as a JSON object; serialize to string for storage in SelItem
    if (res.contains("selector") && res["selector"].is_object())
        return res["selector"].dump();
    err = "The selected topology no longer exists. Select it again.";
    return {};
}

std::vector<uint32_t> DocMirror::faceEdges(WorkerClient& w, uint32_t body_id, uint32_t face_topo_id) {
    std::vector<uint32_t> out;
    if (docId_.empty()) return out;
    nlohmann::json res; std::string err;
    nlohmann::json params = {
        {"doc_id", docId_}, {"revision", revision_},
        {"body_id", body_id}, {"topo_id", (int)face_topo_id}
    };
    if (!w.call("doc_face_edges", params, res, err)) return out;
    if (res.contains("code")) {
        if (jstr(res, "code") == "stale_revision") resyncTree(w);
        return out;
    }
    if (res.contains("edges") && res["edges"].is_array())
        for (const auto& e : res["edges"]) if (e.is_number_integer()) out.push_back(e.get<uint32_t>());
    return out;
}

// C37-F: Checkpoint / revert helpers -----------------------------------------

std::string DocMirror::checkpoint(WorkerClient& w) {
    if (docId_.empty()) return {};
    nlohmann::json res;
    std::string err;
    if (!w.call("doc_checkpoint", {{"doc_id", docId_}}, res, err)) return {};
    if (res.contains("code")) return {};   // engine error — leave empty so PM falls back to undo loop
    return jstr(res, "checkpoint");
}

OpsResponse DocMirror::revert(WorkerClient& w, const std::string& checkpointId) {
    OpsResponse r;
    if (docId_.empty() || checkpointId.empty()) {
        r.error_code = "no_doc"; r.error_message = "no document or checkpoint"; return r;
    }
    nlohmann::json res;
    std::string err;
    if (!w.call("doc_revert", {{"doc_id", docId_}, {"checkpoint", checkpointId}}, res, err)) {
        r.error_code = "transport"; r.error_message = err; return r;
    }
    return ingestOpsResponse(res);
}

bool DocMirror::save(WorkerClient& w, const std::string& path, std::string& err) {
    if (docId_.empty()) { err = "no document open"; return false; }
    nlohmann::json res;
    if (!w.call("doc_save", {{"doc_id", docId_}, {"path", path}}, res, err)) return false;
    if (res.contains("code")) { err = jstr(res, "message"); return false; }
    return true;
}

bool DocMirror::adoptOpenResponse(const nlohmann::json& res, std::string& err) {
    if (res.contains("code")) { err = jstr(res, "message"); return false; }
    const std::string id = jstr(res, "doc_id");
    if (id.empty()) { err = "doc_open response missing doc_id"; return false; }
    docId_ = id;
    revision_ = jint(res, "revision");
    if (res.contains("tree") && res["tree"].is_object()) tree_ = res["tree"];
    // The doc_open response is a full ops-response: adopt the feature status / owner map /
    // datums / mesh it already carries instead of discarding them — the caller then has no
    // reason to issue the historical follow-up doc_rebuild (a FULL recompile whenever the
    // open itself was served from the embedded cache).
    featureStatus_ = {};
    if (res.contains("rebuild") && res["rebuild"].is_object()) {
        const auto& rb = res["rebuild"];
        if (rb.contains("feature_status") && rb["feature_status"].is_array())
            featureStatus_ = rb["feature_status"];
    }
    if (res.contains("owner_map") && res["owner_map"].is_object())
        storeOwnerMap(res["owner_map"]);
    else
        storeOwnerMap(nlohmann::json());
    if (res.contains("datums") && res["datums"].is_array())
        refDatums_ = res["datums"];
    else
        refDatums_ = nlohmann::json::array();
    lastOpenedAicm_.clear();
    if (res.contains("tess") && res["tess"].is_object())
        lastOpenedAicm_ = jstr(res["tess"], "aicm_path");
    lastOpenedVolume_ = (res.contains("volume") && res["volume"].is_number())
                            ? res["volume"].get<double>() : 1.0;
    // How the engine served the open: saved build cache vs. full rebuild, and whether its
    // session is already warm (applyOpenedDoc then skips any follow-up activation).
    {
        const auto hit = res.find("cache_hit");
        lastOpenedCacheReported_ = hit != res.end() && hit->is_boolean();
        lastOpenedCacheHit_ = lastOpenedCacheReported_ && hit->get<bool>();
        const auto warm = res.find("warm");
        lastOpenedWarm_ = warm != res.end() && warm->is_boolean() && warm->get<bool>();
        lastOpenedCacheMissReason_ = jstr(res, "cache_miss_reason");
        // cache.writeback: `entries` is present only when the engine attempted the write.
        lastOpenedWriteback_ = 0;
        lastOpenedWritebackReason_.clear();
        const auto cache = res.find("cache");
        if (cache != res.end() && cache->is_object()) {
            const auto wb = cache->find("writeback");
            if (wb != cache->end() && wb->is_object() && wb->contains("entries")) {
                const auto written = wb->find("written");
                lastOpenedWriteback_ =
                    written != wb->end() && written->is_boolean() && written->get<bool>() ? 1 : -1;
                lastOpenedWritebackReason_ = jstr(*wb, "reason");
            }
        }
    }
    // W12+ — stash any restored drawings so the open caller can re-create their Drawing tabs.
    if (res.contains("drawings") && res["drawings"].is_array())
        lastOpenedDrawings_ = res["drawings"];
    else
        lastOpenedDrawings_ = nlohmann::json::array();
    notify();
    return true;
}

bool DocMirror::open(WorkerClient& w, const std::string& path, std::string& err, bool asSolid) {
    nlohmann::json res;
    // Default (asSolid==false) keeps the request byte-identical to the historic {"path": path} shape;
    // only when asSolid is set do we add the optional engine flag that sews a mesh import into a solid.
    nlohmann::json params = {{"path", path}};
    if (asSolid) params["as_solid"] = true;
    // doc_open runs in hostd's long tier: a cache-miss rebuild may legitimately take minutes.
    if (!w.call("doc_open", params, res, err, kLongEngineCallTimeoutMs)) return false;
    return adoptOpenResponse(res, err);
}

bool DocMirror::activate(WorkerClient& w, const std::string& docId, std::string& aicmPath,
                         std::string& err) {
    aicmPath.clear();
    if (docId.empty()) { err = "empty doc id"; return false; }
    nlohmann::json res;
    // doc_activate returns the full ops-response (tree / revision / rebuild.feature_status /
    // tess.aicm_path / owner_map) WITHOUT recompiling when the engine's read-cache compile is
    // warm — it is the one cheap call that hands us a fresh mesh path for an arbitrary
    // already-open doc, so the viewport can refresh on switch. (doc_tree has no tess block;
    // doc_rebuild is the forced-recompile variant this replaced.) Never bumps the revision.
    if (!w.call("doc_activate", {{"doc_id", docId}}, res, err, kLongEngineCallTimeoutMs))
        return false;
    if (res.contains("code")) { err = jstr(res, "message"); return false; }
    docId_ = docId;
    revision_ = jint(res, "revision");
    if (res.contains("tree") && res["tree"].is_object()) tree_ = res["tree"];
    else tree_ = {};
    featureStatus_ = {};
    if (res.contains("rebuild") && res["rebuild"].is_object()) {
        const auto& rb = res["rebuild"];
        if (rb.contains("feature_status") && rb["feature_status"].is_array())
            featureStatus_ = rb["feature_status"];
    }
    if (res.contains("owner_map") && res["owner_map"].is_object())
        storeOwnerMap(res["owner_map"]);
    else
        storeOwnerMap(nlohmann::json());  // clear stale owner map from the previous doc
    if (res.contains("datums") && res["datums"].is_array())
        refDatums_ = res["datums"];
    else
        refDatums_ = nlohmann::json::array();
    if (res.contains("tess") && res["tess"].is_object())
        aicmPath = jstr(res["tess"], "aicm_path");
    notify();
    return true;
}


// Adopt a finished doc_rebuild response (AsyncRpc) as activate() would.
bool DocMirror::adoptActivateResponse(const std::string& docId, const nlohmann::json& res,
                                      std::string& aicmPath, std::string& err) {
    aicmPath.clear();
    if (docId.empty()) { err = "empty doc id"; return false; }
    if (res.contains("code")) { err = jstr(res, "message"); return false; }
    docId_ = docId;
    revision_ = jint(res, "revision");
    if (res.contains("tree") && res["tree"].is_object()) tree_ = res["tree"];
    else tree_ = {};
    featureStatus_ = {};
    if (res.contains("rebuild") && res["rebuild"].is_object()) {
        const auto& rb = res["rebuild"];
        if (rb.contains("feature_status") && rb["feature_status"].is_array())
            featureStatus_ = rb["feature_status"];
    }
    if (res.contains("owner_map") && res["owner_map"].is_object())
        storeOwnerMap(res["owner_map"]);
    else
        storeOwnerMap(nlohmann::json());
    if (res.contains("datums") && res["datums"].is_array())
        refDatums_ = res["datums"];
    else
        refDatums_ = nlohmann::json::array();
    if (res.contains("tess") && res["tess"].is_object())
        aicmPath = jstr(res["tess"], "aicm_path");
    notify();
    return true;
}

nlohmann::json DocMirror::list(WorkerClient& w, std::string& err) {
    nlohmann::json res;
    if (!w.call("doc_list", {}, res, err)) return nlohmann::json::array();
    if (res.contains("code")) { err = jstr(res, "message"); return nlohmann::json::array(); }
    if (res.contains("docs") && res["docs"].is_array()) return res["docs"];
    return nlohmann::json::array();
}

// ---- Configurations -------------------------------------------------------

OpsResponse DocMirror::callConfig(WorkerClient& w, const std::string& method,
                                  const nlohmann::json& extraParams) {
    OpsResponse r;
    if (docId_.empty()) { r.error_code = "no_doc"; r.error_message = "no document open"; return r; }
    nlohmann::json params = extraParams.is_object() ? extraParams : nlohmann::json::object();
    params["doc_id"] = docId_;
    nlohmann::json res;
    std::string err;
    // A config_activate/add/delete can trigger a full rebuild — give it the same headroom the
    // tab-switch doc_rebuild uses rather than the doc_* default.
    if (!w.call(method, params, res, err, 60000)) {
        r.error_code = "transport"; r.error_message = err; return r;
    }
    if (res.contains("code")) {
        r.error_code = jstr(res, "code"); r.error_message = jstr(res, "message");
        if (r.error_code == "stale_revision") resyncTree(w);
        return r;
    }
    // Same ops-response shape apply_ops returns — ingest it so the mirror (revision/tree/owner_map)
    // stays in step with the now-active config and the listener repaints the tree.
    r = parseOpsResponse(res);
    if (r.ok) {
        revision_ = r.revision;
        if (!r.tree.is_null()) tree_ = r.tree;
        if (!r.feature_status.is_null()) featureStatus_ = r.feature_status;
        storeOwnerMap(r.owner_map);
        notify();
    }
    return r;
}

nlohmann::json DocMirror::configList(WorkerClient& w, std::string& err) {
    if (docId_.empty()) { err = "no document open"; return nlohmann::json(); }
    nlohmann::json res;
    if (!w.call("config_list", {{"doc_id", docId_}}, res, err)) return nlohmann::json();
    if (res.contains("code")) { err = jstr(res, "message"); return nlohmann::json(); }
    return res;
}

// ---- Weldments cut list ----------------------------------------------------

nlohmann::json DocMirror::weldCutList(WorkerClient& w, std::string& err) {
    if (docId_.empty()) { err = "no document open"; return nlohmann::json(); }
    nlohmann::json res;
    if (!w.call("weld_cut_list", {{"doc_id", docId_}}, res, err)) return nlohmann::json();
    if (res.contains("code")) { err = jstr(res, "message"); return nlohmann::json(); }
    return res;
}

// ---- 2D export (DXF / PDF) -------------------------------------------------

std::string DocMirror::exportFlat(WorkerClient& w, const std::string& path,
                                  const std::string& format, std::string& err) {
    if (docId_.empty()) { err = "no document open"; return {}; }
    nlohmann::json res;
    if (!w.call("export_flat_dxf",
                {{"doc_id", docId_}, {"path", path}, {"format", format}}, res, err, 60000))
        return {};
    // Structured-error methods return {code, message, detail} on failure.
    if (res.contains("code")) { err = jstr(res, "message"); return {}; }
    return jstr(res, "path");
}

std::string DocMirror::exportDrawing(WorkerClient& w, const std::string& drawingDocId,
                                     const std::string& path, const std::string& format,
                                     std::string& err) {
    if (drawingDocId.empty()) { err = "no drawing open"; return {}; }
    nlohmann::json res;
    if (!w.call("export_drawing",
                {{"doc_id", drawingDocId}, {"path", path}, {"format", format}}, res, err, 60000))
        return {};
    if (res.contains("code")) { err = jstr(res, "message"); return {}; }
    return jstr(res, "path");
}

// ---- General 3D export (export_file) --------------------------------------
// Mirrors exportFlat/exportDrawing: a read-only worker passthrough that returns the written path.
// A fresh compile + B-rep/mesh write can be slow, so it gets the same 60s headroom they use.
std::string DocMirror::exportFile(WorkerClient& w, const std::string& docId,
                                  const std::string& path, const std::string& format,
                                  std::string& err, double linDeflection, double angDeflection,
                                  bool relative, bool ascii) {
    if (docId.empty()) { err = "no document open"; return {}; }
    nlohmann::json res;
    if (!w.call("export_file",
                {{"doc_id", docId}, {"path", path}, {"format", format},
                 {"lin_deflection", linDeflection}, {"ang_deflection", angDeflection},
                 {"relative", relative}, {"ascii", ascii}},
                res, err, 60000))
        return {};
    if (res.contains("code")) { err = jstr(res, "message"); return {}; }
    return jstr(res, "path");
}

bool DocMirror::exportEstimate(WorkerClient& w, const std::string& docId, double linDeflection,
                               double angDeflection, bool relative, long long& tris,
                               long long& verts, std::string& err) {
    if (docId.empty()) { err = "no document open"; return false; }
    nlohmann::json res;
    if (!w.call("export_tess_estimate",
                {{"doc_id", docId}, {"lin_deflection", linDeflection},
                 {"ang_deflection", angDeflection}, {"relative", relative}},
                res, err, 60000))
        return false;
    if (res.contains("code")) { err = jstr(res, "message"); return false; }
    tris = res.value("triangles", 0LL);
    verts = res.value("vertices", 0LL);
    return true;
}

}  // namespace core
