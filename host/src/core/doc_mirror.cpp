// DocMirror implementation (C16).
#include <cstdio>
#include <string>

#include "doc_mirror.h"
#include "../worker_client.h"

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
    notify();
    return true;
}

std::string DocMirror::resolvePickAsm(WorkerClient& w, uint32_t body_id,
                                      const std::string& kind, uint32_t topo_id,
                                      std::string& component, std::string& err) {
    component.clear();
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
    if (res.contains("selector")) return res["selector"].dump();
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

    // The draggable response embeds the same ops block apply_ops returns — ingest it so the mirror
    // (revision/tree/owner_map) stays in step and the listener repaints the tree.
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

std::string DocMirror::resolvePick(WorkerClient& w, uint32_t body_id,
                                   const std::string& kind, uint32_t topo_id,
                                   std::string& err) {
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
    if (res.contains("selector")) return res["selector"].dump();
    return {};
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

bool DocMirror::save(WorkerClient& w, const std::string& path, std::string& err) {
    if (docId_.empty()) { err = "no document open"; return false; }
    nlohmann::json res;
    if (!w.call("doc_save", {{"doc_id", docId_}, {"path", path}}, res, err)) return false;
    if (res.contains("code")) { err = jstr(res, "message"); return false; }
    return true;
}

bool DocMirror::open(WorkerClient& w, const std::string& path, std::string& err) {
    nlohmann::json res;
    if (!w.call("doc_open", {{"path", path}}, res, err)) return false;
    if (res.contains("code")) { err = jstr(res, "message"); return false; }
    docId_ = jstr(res, "doc_id");
    revision_ = jint(res, "revision");
    if (res.contains("tree") && res["tree"].is_object()) tree_ = res["tree"];
    featureStatus_ = {};
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

}  // namespace core
