// DocMirror implementation (C16).
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <mutex>
#include <string>

#include "doc_mirror.h"
#include "async_rpc.h"
#include "../worker_client.h"
#include "cpp_core.h"  // strangler diagnostic: parity probe against libchudware.dll

namespace core {

namespace {

using OwnerIndex = std::vector<std::pair<uint64_t, uint32_t>>;
enum class SolidPresence { Unknown, Absent, Present };

// Revision-bound lazy caches are released with their last mirror/snapshot reference.
struct OwnerData {
    explicit OwnerData(const nlohmann::json& value) : raw(value) {}

    const nlohmann::json raw;
    mutable std::once_flag parsedOnce;
    mutable std::once_flag indexedOnce;
    mutable std::unordered_map<std::string, Owned> owned;
    mutable std::vector<std::string> features;
    mutable OwnerIndex faces, edges, bodies;

    void parse() const {
        std::call_once(parsedOnce, [this] {
            if (!raw.is_object()) return;
            auto pairList = [](const nlohmann::json& arr,
                               std::vector<std::pair<uint32_t, uint32_t>>& out) {
                if (!arr.is_array()) return;
                out.reserve(arr.size());
                for (const auto& e : arr) {
                    if (e.is_array() && e.size() == 2 && e[0].is_number_integer() &&
                        e[1].is_number_integer())
                        out.emplace_back((uint32_t)e[0].get<long long>(),
                                         (uint32_t)e[1].get<long long>());
                }
            };
            owned.reserve(raw.size());
            features.reserve(raw.size());
            for (auto it = raw.begin(); it != raw.end(); ++it) {
                const auto& o = it.value();
                if (!o.is_object()) continue;
                Owned topology;
                if (auto b = o.find("bodies"); b != o.end() && b->is_array()) {
                    topology.bodies.reserve(b->size());
                    for (const auto& e : *b)
                        if (e.is_number_integer())
                            topology.bodies.push_back((uint32_t)e.get<long long>());
                }
                if (auto f = o.find("faces"); f != o.end()) pairList(*f, topology.faces);
                if (auto e = o.find("edges"); e != o.end()) pairList(*e, topology.edges);
                features.push_back(it.key());
                owned.emplace(it.key(), std::move(topology));
            }
        });
    }

    void index() const {
        parse();
        std::call_once(indexedOnce, [this] {
            size_t faceCount = 0, edgeCount = 0, bodyCount = 0;
            for (const auto& item : owned) {
                faceCount += item.second.faces.size();
                edgeCount += item.second.edges.size();
                bodyCount += item.second.bodies.size();
            }
            faces.reserve(faceCount);
            edges.reserve(edgeCount);
            bodies.reserve(bodyCount);
            for (size_t i = 0; i < features.size(); ++i) {
                const auto& topology = owned.at(features[i]);
                const auto feature = static_cast<uint32_t>(i);
                for (uint32_t body : topology.bodies) bodies.emplace_back(body, feature);
                for (const auto& bf : topology.faces)
                    faces.emplace_back((uint64_t(bf.first) << 32) | bf.second, feature);
                for (const auto& be : topology.edges)
                    edges.emplace_back((uint64_t(be.first) << 32) | be.second, feature);
            }
            auto lastOwnerWins = [](OwnerIndex& entries) {
                // Match the old map assignment: the last wire-order feature owns a shared key.
                std::sort(entries.begin(), entries.end(), [](const auto& a, const auto& b) {
                    return a.first < b.first || (a.first == b.first && a.second > b.second);
                });
                entries.erase(std::unique(entries.begin(), entries.end(),
                    [](const auto& a, const auto& b) { return a.first == b.first; }), entries.end());
            };
            lastOwnerWins(faces);
            lastOwnerWins(edges);
            std::sort(bodies.begin(), bodies.end());
        });
    }

    std::string owner(const OwnerIndex& entries, uint64_t key) const {
        const auto it = std::lower_bound(entries.begin(), entries.end(), key,
            [](const auto& entry, uint64_t value) { return entry.first < value; });
        return it != entries.end() && it->first == key ? features[it->second] : std::string();
    }
};

}  // namespace

struct DocMirrorPayload {
    std::shared_ptr<const nlohmann::json> tree = std::make_shared<const nlohmann::json>();
    std::shared_ptr<const nlohmann::json> featureStatus = std::make_shared<const nlohmann::json>();
    int featureStatusRevision = -1;
    std::shared_ptr<const nlohmann::json> refDatums =
        std::make_shared<const nlohmann::json>(nlohmann::json::array());
    std::shared_ptr<const OwnerData> owners = std::make_shared<const OwnerData>(nlohmann::json());
    SolidPresence solidPresence = SolidPresence::Unknown;
};

static const std::shared_ptr<const DocMirrorPayload>& emptyMirrorPayload() {
    static const auto payload = std::make_shared<const DocMirrorPayload>();
    return payload;
}

const nlohmann::json& DocMirrorSnapshot::tree() const {
    static const nlohmann::json empty;
    return payload ? *payload->tree : empty;
}

DocMirror::DocMirror() : payload_(emptyMirrorPayload()) {}

const nlohmann::json& DocMirror::tree() const { return *payload_->tree; }
const nlohmann::json& DocMirror::featureStatus() const { return *payload_->featureStatus; }
bool DocMirror::featureStatusCurrent() const {
    return revision_ >= 0 && payload_->featureStatusRevision == revision_;
}
const nlohmann::json& DocMirror::refDatums() const { return *payload_->refDatums; }
bool DocMirror::solidPresenceKnown() const {
    return payload_->solidPresence != SolidPresence::Unknown;
}
bool DocMirror::hasSolidBody() const {
    return payload_->solidPresence == SolidPresence::Present;
}

void DocMirror::storeTree(const nlohmann::json& tree) {
    auto next = std::make_shared<DocMirrorPayload>(*payload_);
    next->tree = std::make_shared<const nlohmann::json>(tree);
    payload_ = std::move(next);
}

void DocMirror::storeFeatureStatus(const nlohmann::json& status) {
    auto next = std::make_shared<DocMirrorPayload>(*payload_);
    next->featureStatus = std::make_shared<const nlohmann::json>(status);
    next->featureStatusRevision = status.is_array() ? revision_ : -1;
    payload_ = std::move(next);
}

void DocMirror::storeRefDatums(const nlohmann::json& datums) {
    auto next = std::make_shared<DocMirrorPayload>(*payload_);
    next->refDatums = std::make_shared<const nlohmann::json>(datums);
    payload_ = std::move(next);
}

void DocMirror::reset() {
    docId_.clear();
    revision_ = -1;
    notifiedDocId_.clear();
    notifiedRevision_ = -1;
    payload_ = emptyMirrorPayload();
    lastTreeOnlyPayload_.reset();
    lastOpenedDrawings_ = nlohmann::json::array();
    lastOpenedAicm_.clear();
    lastOpenedMeshUsable_ = false;
    lastOpenedCacheReported_ = lastOpenedCacheHit_ = lastOpenedWarm_ = false;
    lastOpenedCacheMissReason_.clear();
    lastOpenedWriteback_ = 0;
    lastOpenedWritebackReason_.clear();
    ++mutationGen_;
    ++generation_;
}

const std::unordered_map<std::string, Owned>& DocMirror::ownerMap() const {
    payload_->owners->parse();
    return payload_->owners->owned;
}

std::string DocMirror::ownerOfFace(uint32_t body_id, uint32_t face_id) const {
    const auto& owners = *payload_->owners;
    owners.index();
    return owners.owner(owners.faces, (uint64_t(body_id) << 32) | face_id);
}

std::string DocMirror::ownerOfEdge(uint32_t body_id, uint32_t edge_id) const {
    const auto& owners = *payload_->owners;
    owners.index();
    return owners.owner(owners.edges, (uint64_t(body_id) << 32) | edge_id);
}

std::vector<std::string> DocMirror::ownerOfBody(uint32_t body_id) const {
    const auto& owners = *payload_->owners;
    owners.index();
    auto it = std::lower_bound(owners.bodies.begin(), owners.bodies.end(), uint64_t(body_id),
        [](const auto& entry, uint64_t value) { return entry.first < value; });
    std::vector<std::string> result;
    for (; it != owners.bodies.end() && it->first == body_id; ++it)
        result.push_back(owners.features[it->second]);
    return result;
}

// ---- helpers ----

static inline std::string jstr(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_string()) ? it->get<std::string>() : std::string();
}
static inline int jint(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_number_integer()) ? it->get<int>() : 0;
}

void DocMirror::storeSolidPresence(const nlohmann::json& result, bool sameDocument) {
    const auto rev = result.find("revision");
    const bool sameRevision = sameDocument && rev != result.end() && rev->is_number_integer() &&
                              *rev == revision_;
    const bool treeOnly = !result.contains("tess") && !result.contains("volume") &&
                          !result.contains("rebuild");
    SolidPresence presence =
        sameRevision && treeOnly ? payload_->solidPresence : SolidPresence::Unknown;
    const auto responseTree = result.find("tree");
    const bool hasResponseTree = responseTree != result.end() && responseTree->is_object();
    const nlohmann::json& evidenceTree = hasResponseTree ? *responseTree : tree();
    bool positivePartVolume = false;
    const auto schema = evidenceTree.find("schema");
    if ((hasResponseTree || sameDocument) && schema != evidenceTree.end() &&
        schema->is_string() && *schema == "aicad.part/1") {
        const auto volume = result.find("volume");
        if (volume != result.end() && volume->is_number()) {
            const double value = volume->get<double>();
            positivePartVolume = std::isfinite(value) && value > 0.0;
        }
    }
    const auto tess = result.find("tess");
    const nlohmann::json* bodies = nullptr;
    if (tess != result.end() && tess->is_object()) {
        const auto it = tess->find("bodies");
        if (it != tess->end() && it->is_array()) bodies = &*it;
    }
    if (bodies) {
        bool solid = false, unknown = false;
        for (const auto& body : *bodies) {
            const auto kind = body.find("kind");
            if (kind != body.end() && kind->is_string() && *kind == "solid") {
                solid = true;
                break;
            }
            if (kind == body.end() || !kind->is_string() || *kind != "surface") unknown = true;
        }
        if (solid || (unknown && positivePartVolume)) presence = SolidPresence::Present;
        else if (!unknown)
            presence = positivePartVolume ? SolidPresence::Unknown : SolidPresence::Absent;
    } else if (positivePartVolume) {
        presence = SolidPresence::Present;
    }
    if (presence != payload_->solidPresence) {
        auto next = std::make_shared<DocMirrorPayload>(*payload_);
        next->solidPresence = presence;
        payload_ = std::move(next);
    }
}

// ---- OpsResponse parser ----

static OpsResponse parseOpsError(const nlohmann::json& result) {
    OpsResponse r;
    r.error_code = jstr(result, "code");
    r.error_message = jstr(result, "message");
    if (auto detail = result.find("detail"); detail != result.end())
        r.error_detail = *detail;
    return r;
}

static bool hasRejectedTree(const nlohmann::json& result) {
    const auto it = result.find("rejected_tree");
    return it != result.end() && !it->is_null();
}

static bool isPartTree(const nlohmann::json& tree) {
    const auto schema = tree.find("schema");
    return schema != tree.end() && schema->is_string() && *schema == "aicad.part/1";
}

// Matches doc_apply_ops' read-only batch contract: only copy_feature leaves the
// document revision and history untouched.
static bool isReadOnlyOps(const nlohmann::json& ops) {
    if (!ops.is_array() || ops.empty()) return false;
    for (const auto& op : ops) {
        if (!op.is_object()) return false;
        const auto kind = op.find("op");
        if (kind == op.end() || !kind->is_string() || *kind != "copy_feature") return false;
    }
    return true;
}

// On parts doc_apply_ops checks base_revision under the lock, then validates a
// working copy. This structured envelope proves that the batch never committed.
static bool isAtomicPartValidation(const OpsResponse& r, const nlohmann::json& result,
                                   int baseRev, bool part) {
    if (r.error_code != "invalid_op" || !part || baseRev < 0 || !r.error_detail.is_object())
        return false;
    auto agrees = [baseRev](const nlohmann::json& obj, const char* key) {
        auto it = obj.find(key);
        return it == obj.end() || (it->is_number_integer() && *it == baseRev);
    };
    return agrees(result, "revision") && agrees(r.error_detail, "current_revision");
}

static void classifyApplyError(OpsResponse& r, const nlohmann::json& result,
                               int baseRev, bool part, bool current) {
    if (r.error_code != "invalid_op") return;
    r.rejected = true;
    if (current && isAtomicPartValidation(r, result, baseRev, part)) {
        r.unchanged = true;
        r.revision = baseRev;
    }
}

OpsResponse DocMirror::parseOpsResponse(const nlohmann::json& result) {
    OpsResponse r;
    if (auto rev = result.find("revision"); rev != result.end() && rev->is_number_integer())
        r.revision = rev->get<int>();
    if (result.contains("tree") && result["tree"].is_object())
        r.tree = result["tree"];
    if (result.contains("rebuild") && result["rebuild"].is_object()) {
        const auto& rb = result["rebuild"];
        if (rb.contains("feature_status") && rb["feature_status"].is_array())
            r.feature_status = rb["feature_status"];
    }
    if (result.contains("tess") && result["tess"].is_object()) {
        const auto& tess = result["tess"];
        const auto path = tess.find("aicm_path");
        if (path != tess.end() && path->is_string()) {
            r.aicm_path = path->get<std::string>();
            const auto bodies = tess.find("bodies");
            r.has_tess = !r.aicm_path.empty() ||
                         (bodies != tess.end() && bodies->is_array() && bodies->empty());
        }
    }
    if (result.contains("id_map") && result["id_map"].is_object())
        r.id_map = result["id_map"];   // C17: add-feature flows find their new id here
    if (result.contains("owner_map") && result["owner_map"].is_object())
        r.owner_map = result["owner_map"];  // W1-B (item 6): feature -> owned topology (optional)
    if (result.contains("auto_rollback") && result["auto_rollback"].is_object())
        r.auto_rollback = result["auto_rollback"];
    if (result.contains("warning") && result["warning"].is_string())
        r.warning = result["warning"].get<std::string>();

    r.rejected = hasRejectedTree(result);
    if (r.rejected && r.feature_status.is_array()) {
        for (const auto& status : r.feature_status) {
            if (!status.is_object()) continue;
            auto ok = status.find("ok");
            if (ok == status.end() || !ok->is_boolean() || ok->get<bool>()) continue;
            const std::string id = jstr(status, "id");
            const std::string error = jstr(status, "error");
            if (!r.error_message.empty()) r.error_message += "\n";
            if (!id.empty()) r.error_message += id + ": ";
            r.error_message += error.empty() ? "Feature rebuild failed." : error;
        }
    }
    if (r.rejected) {
        r.error_code = "rebuild_rejected";
        if (r.error_message.empty()) r.error_message = "The candidate rebuild was rejected.";
        // Even a malformed or older response must not hand callers ghost feature ids.
        r.id_map = nlohmann::json::object();
    } else {
        r.ok = true;
    }
    return r;
}

void DocMirror::storeResponseDatums(const nlohmann::json& result) {
    // Missing datums leave the existing state untouched. Apply only after the
    // caller has accepted this response for the current document.
    if (result.contains("datums") && result["datums"].is_array())
        storeRefDatums(result["datums"]);
}

// Retain the wire payload without constructing per-face strings or reverse indices.
void DocMirror::storeOwnerMap(const nlohmann::json& ownerMapJson) {
    auto next = std::make_shared<DocMirrorPayload>(*payload_);
    next->owners = std::make_shared<const OwnerData>(ownerMapJson);
    payload_ = std::move(next);
}

DocMirrorSnapshot DocMirror::snapshot() const {
    return {docId_, revision_, payload_};
}

bool DocMirror::restoreSnapshot(const DocMirrorSnapshot& s) {
    if (!s.valid()) return false;
    docId_ = s.docId;
    revision_ = s.revision;
    payload_ = s.payload;
    lastTreeOnlyPayload_.reset();
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
    return storeTreeResponse(res);
}

bool DocMirror::storeTreeResponse(const nlohmann::json& res) {
    if (docId_.empty() || res.contains("code")) return false;
    storeSolidPresence(res, true);
    revision_ = jint(res, "revision");
    if (res.contains("tree") && res["tree"].is_object()) storeTree(res["tree"]);
    if (res.contains("rebuild") && res["rebuild"].is_object()) {
        const auto& rb = res["rebuild"];
        if (rb.contains("feature_status") && rb["feature_status"].is_array())
            storeFeatureStatus(rb["feature_status"]);
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
        storeRefDatums(res["datums"]);
    else if (res.contains("rebuild") && res["rebuild"].is_object() &&
             res["rebuild"].contains("datums") && res["rebuild"]["datums"].is_array())
        storeRefDatums(res["rebuild"]["datums"]);
    // Only an exact tree-only snapshot can prove that a later N+1 response is the
    // same committed revision. Subsequent payload changes invalidate this weak marker.
    const auto rev = res.find("revision");
    if (rev != res.end() && rev->is_number_integer() && res.contains("tree") &&
        res["tree"].is_object() && !res.contains("tess") && !res.contains("rebuild") &&
        !hasRejectedTree(res))
        lastTreeOnlyPayload_ = payload_;
    else
        lastTreeOnlyPayload_.reset();
    return true;
}

bool DocMirror::ingestTreeResponse(const nlohmann::json& res) {
    if (!storeTreeResponse(res)) return false;
    notify();
    return true;
}

// C24: public convenience wrapper — pull tree/revision and notify so the tree repaints.
bool DocMirror::resync(WorkerClient& w) {
    if (docId_.empty()) return false;
    nlohmann::json res;
    std::string err;
    if (!w.call("doc_tree", {{"doc_id", docId_}}, res, err)) return false;
    return ingestTreeResponse(res);
}

// ---- public API ----

bool DocMirror::newPart(WorkerClient& w, std::string& err) {
    nlohmann::json res;
    if (!w.call("doc_new", {{"kind", "part"}}, res, err)) return false;
    if (res.contains("code")) { err = jstr(res, "message"); return false; }
    lastTreeOnlyPayload_.reset();
    storeSolidPresence(res, false);
    ++mutationGen_;
    docId_ = jstr(res, "doc_id");
    revision_ = jint(res, "revision");
    if (res.contains("tree") && res["tree"].is_object()) storeTree(res["tree"]);
    else storeTree({});
    storeFeatureStatus({});
    if (res.contains("rebuild") && res["rebuild"].is_object()) {
        const auto& rb = res["rebuild"];
        if (rb.contains("feature_status") && rb["feature_status"].is_array())
            storeFeatureStatus(rb["feature_status"]);
    }
    if (res.contains("datums") && res["datums"].is_array()) storeRefDatums(res["datums"]);
    else storeRefDatums(nlohmann::json::array());
    if (res.contains("owner_map") && res["owner_map"].is_object()) storeOwnerMap(res["owner_map"]);
    else storeOwnerMap(nlohmann::json());
    notify();
    return true;
}

// --- C20b: assembly support ---

bool DocMirror::newAssembly(WorkerClient& w, std::string& err) {
    nlohmann::json res;
    if (!w.call("doc_new", {{"kind", "assembly"}}, res, err)) return false;
    if (res.contains("code")) { err = jstr(res, "message"); return false; }
    lastTreeOnlyPayload_.reset();
    storeSolidPresence(res, false);
    ++mutationGen_;
    docId_ = jstr(res, "doc_id");
    revision_ = jint(res, "revision");
    if (res.contains("tree") && res["tree"].is_object()) storeTree(res["tree"]);
    else storeTree({});
    storeFeatureStatus({});
    storeRefDatums(nlohmann::json::array());
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
    const std::string docId = docId_;
    const int baseRev = revision_;
    const uint64_t gen = mutationGen_;
    const bool part = isPartTree(tree());
    nlohmann::json params = {
        {"doc_id", docId},
        {"base_revision", baseRev},
        {"ops", ops},
        {"coalesce", coalesce},
        {"_mutation_origin", "gui"}
    };
    bool ok = w.call("doc_apply_ops", params, res, err);
    if (!ok) {
        r.error_code = "transport";
        r.error_message = err;
        return r;
    }
    const bool stillCurrent = (gen == mutationGen_ && docId_ == docId && revision_ == baseRev);
    // Check for error envelope {code, message, detail}
    if (res.contains("code")) {
        r = parseOpsError(res);
        classifyApplyError(r, res, baseRev, part, stillCurrent);
        if (stillCurrent && r.error_code == "stale_revision") {
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
    r.unchanged = stillCurrent && baseRev >= 0 && r.rejected &&
                  r.revision == baseRev;
    if (!stillCurrent && r.ok) {
        r.ok = false;
        r.error_code = "superseded";
        r.error_message = "The document changed before the operation response arrived.";
        return r;
    }
    if (r.ok) {
        storeSolidPresence(res, true);
        revision_ = r.revision;
        if (!r.tree.is_null()) storeTree(r.tree);
        if (!r.feature_status.is_null()) storeFeatureStatus(r.feature_status);
        storeResponseDatums(res);
        storeOwnerMap(r.owner_map);  // W1-B: refresh owner map (tolerates absence)
        if (!isReadOnlyOps(ops) && acceptedOpsObserver_)
            acceptedOpsObserver_(r, r.revision != baseRev);
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
        if (done) done(early, true);
        return;
    }
    const uint64_t gen = ++mutationGen_;
    const std::string docId = docId_;
    const int baseRev = revision_;
    const bool part = isPartTree(tree());
    const bool readOnly = isReadOnlyOps(ops);
    nlohmann::json params = {
        {"doc_id", docId},
        {"base_revision", baseRev},
        {"ops", ops},
        {"coalesce", coalesce},
        {"_mutation_origin", "gui"}
    };
    const auto pendingObserver = readOnly ? PendingOpsObserver{} : pendingOpsObserver_;
    if (pendingObserver) pendingObserver(docId, true);
    // Generation, document and base revision guard normal admission. A later
    // tree-only pull may already hold this accepted N+1; reconcile only its exact
    // still-live payload and tree, never an unrelated or newer snapshot.
    rpc.call("doc_apply_ops", std::move(params),
        [this, &w, gen, docId, baseRev, part, readOnly, pendingObserver,
         done = std::move(done)](const RpcResult& rr) {
            struct SettlePending {
                PendingOpsObserver observer;
                const std::string& id;
                ~SettlePending() { if (observer) observer(id, false); }
            } settle{pendingObserver, docId};
            OpsResponse r;
            const bool sameOwner = (gen == mutationGen_ && docId_ == docId);
            const bool atBase = sameOwner && revision_ == baseRev;
            if (!rr.ok) {
                r.error_code = "transport";
                r.error_message = rr.err;
                if (!atBase && supersededOpsObserver_) supersededOpsObserver_(docId, baseRev);
                if (done) done(r, atBase);
                return;
            }
            if (rr.result.contains("code")) {
                r = parseOpsError(rr.result);
                classifyApplyError(r, rr.result, baseRev, part, atBase);
                if (atBase && r.error_code == "stale_revision") {
                    std::printf("[doc] stale_revision (async) — resyncing tree\n");
                    std::fflush(stdout);
                    resyncTree(w);
                    if (staleResyncCb_) staleResyncCb_();
                    notify();
                }
                const bool current = atBase && gen == mutationGen_ && docId_ == docId;
                if (!current && supersededOpsObserver_ &&
                    !isAtomicPartValidation(r, rr.result, baseRev, part))
                    supersededOpsObserver_(docId, baseRev);
                if (done) done(r, current);
                return;
            }
            r = parseOpsResponse(rr.result);
            bool reconciled = false;
            if (!atBase && sameOwner && r.ok && baseRev >= 0 &&
                static_cast<int64_t>(r.revision) == static_cast<int64_t>(baseRev) + 1 &&
                revision_ == r.revision && r.tree.is_object() && !r.tree.empty()) {
                const auto treeOnly = lastTreeOnlyPayload_.lock();
                reconciled = treeOnly && treeOnly == payload_ && r.tree == tree();
            }
            const bool current = atBase || reconciled;
            r.unchanged = atBase && baseRev >= 0 && r.rejected && r.revision == baseRev;
            if (!current) {
                const bool rolledBack = r.rejected && baseRev >= 0 && r.revision == baseRev;
                const bool readOnlyUnchanged = r.ok && readOnly && baseRev >= 0 &&
                                               r.revision == baseRev;
                if (!readOnly && r.ok && r.revision > baseRev && humanMutationObserver_)
                    humanMutationObserver_(docId, r.revision);
                if (!rolledBack && !readOnlyUnchanged && supersededOpsObserver_)
                    supersededOpsObserver_(docId, baseRev);
                if (done) done(r, false);
                return;
            }
            if (r.ok) {
                storeSolidPresence(rr.result, true);
                revision_ = r.revision;
                if (!r.tree.is_null()) storeTree(r.tree);
                if (!r.feature_status.is_null()) storeFeatureStatus(r.feature_status);
                storeResponseDatums(rr.result);
                storeOwnerMap(r.owner_map);
                if (!readOnly && acceptedOpsObserver_)
                    acceptedOpsObserver_(r, r.revision != baseRev);
                notify();
            }
            if (done) done(r, true);
        }, timeoutMs);
}

OpsResponse DocMirror::undo(WorkerClient& w) {
    OpsResponse r;
    if (docId_.empty()) { r.error_code = "no_doc"; r.error_message = "no document open"; return r; }
    nlohmann::json res;
    std::string err;
    const int baseRev = revision_;
    if (!w.call("doc_undo", {{"doc_id", docId_}}, res, err)) {
        r.error_code = "transport"; r.error_message = err; return r;
    }
    if (res.contains("code")) {
        return parseOpsError(res);
    }
    r = parseOpsResponse(res);  // undo restores an already-committed historical tree
    if (r.ok) {
        storeSolidPresence(res, true);
        revision_ = r.revision;
        if (!r.tree.is_null()) storeTree(r.tree);
        if (!r.feature_status.is_null()) storeFeatureStatus(r.feature_status);
        storeResponseDatums(res);
        storeOwnerMap(r.owner_map);  // W1-B
        if (acceptedOpsObserver_) acceptedOpsObserver_(r, r.revision != baseRev);
        notify();
    }
    return r;
}

OpsResponse DocMirror::redo(WorkerClient& w) {
    OpsResponse r;
    if (docId_.empty()) { r.error_code = "no_doc"; r.error_message = "no document open"; return r; }
    nlohmann::json res;
    const int baseRev = revision_;
    std::string err;
    if (!w.call("doc_redo", {{"doc_id", docId_}}, res, err)) {
        r.error_code = "transport"; r.error_message = err; return r;
    }
    if (res.contains("code")) {
        return parseOpsError(res);
    }
    r = parseOpsResponse(res);  // redo likewise restores committed history
    if (r.ok) {
        storeSolidPresence(res, true);
        revision_ = r.revision;
        if (!r.tree.is_null()) storeTree(r.tree);
        if (!r.feature_status.is_null()) storeFeatureStatus(r.feature_status);
        storeResponseDatums(res);
        storeOwnerMap(r.owner_map);  // W1-B
        if (acceptedOpsObserver_) acceptedOpsObserver_(r, r.revision != baseRev);
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

    // Push-pull returns the same committed ops envelope; use the canonical ingress
    // so its snapshot and mesh reach the accepted observer exactly once.
    r.ops = ingestOpsResponse(res);
    return r;
}

OpsResponse DocMirror::ingestOpsResponse(const nlohmann::json& res) {
    OpsResponse r;
    if (docId_.empty()) { r.error_code = "no_doc"; r.error_message = "no document open"; return r; }
    if (res.contains("code")) {
        return parseOpsError(res);
    }
    r = parseOpsResponse(res);
    if (r.ok) {
        const int oldRevision = revision_;
        storeSolidPresence(res, true);
        revision_ = r.revision;
        if (!r.tree.is_null()) storeTree(r.tree);
        if (!r.feature_status.is_null()) storeFeatureStatus(r.feature_status);
        storeResponseDatums(res);
        storeOwnerMap(r.owner_map);
        if (acceptedOpsObserver_) acceptedOpsObserver_(r, r.revision != oldRevision);
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

bool DocMirror::save(WorkerClient& w, const std::string& path, std::string& err) {
    if (docId_.empty()) { err = "no document open"; return false; }
    nlohmann::json res;
    if (!w.call("doc_save", {{"doc_id", docId_}, {"path", path}}, res, err)) return false;
    if (res.contains("code")) { err = jstr(res, "message"); return false; }
    return true;
}

bool DocMirror::adoptOpenResponse(const nlohmann::json& res, std::string& err) {
    if (res.contains("code")) { err = jstr(res, "message"); return false; }
    if (hasRejectedTree(res) ||
        (res.contains("ok") && res["ok"].is_boolean() && !res["ok"].get<bool>())) {
        err = jstr(res, "message");
        if (err.empty() && res.contains("rebuild") && res["rebuild"].is_object()) {
            const auto& statuses = res["rebuild"].value("feature_status", nlohmann::json::array());
            if (statuses.is_array()) for (const auto& status : statuses) {
                if (!status.is_object() || status.value("ok", true)) continue;
                const std::string detail = jstr(status, "error");
                if (!detail.empty()) {
                    if (!err.empty()) err += "\n";
                    err += detail;
                }
            }
        }
        if (err.empty()) err = "The document could not be opened because its build was rejected.";
        return false;
    }
    const std::string id = jstr(res, "doc_id");
    if (id.empty()) { err = "doc_open response missing doc_id"; return false; }
    lastTreeOnlyPayload_.reset();
    storeSolidPresence(res, false);
    ++mutationGen_;
    docId_ = id;
    revision_ = jint(res, "revision");
    if (res.contains("tree") && res["tree"].is_object()) storeTree(res["tree"]);
    // The doc_open response is a full ops-response: adopt the feature status / owner map /
    // datums / mesh it already carries instead of discarding them — the caller then has no
    // reason to issue the historical follow-up doc_rebuild (a FULL recompile whenever the
    // open itself was served from the embedded cache).
    storeFeatureStatus({});
    if (res.contains("rebuild") && res["rebuild"].is_object()) {
        const auto& rb = res["rebuild"];
        if (rb.contains("feature_status") && rb["feature_status"].is_array())
            storeFeatureStatus(rb["feature_status"]);
    }
    if (res.contains("owner_map") && res["owner_map"].is_object())
        storeOwnerMap(res["owner_map"]);
    else
        storeOwnerMap(nlohmann::json());
    if (res.contains("datums") && res["datums"].is_array())
        storeRefDatums(res["datums"]);
    else
        storeRefDatums(nlohmann::json::array());
    lastOpenedAicm_.clear();
    lastOpenedMeshUsable_ = res.contains("tess") && res["tess"].is_object() &&
        !res["tess"].contains("mesh_error") && !res.contains("mesh_error") &&
        res["tess"].contains("aicm_path") && res["tess"]["aicm_path"].is_string();
    if (lastOpenedMeshUsable_) {
        lastOpenedAicm_ = jstr(res["tess"], "aicm_path");
        if (lastOpenedAicm_.empty()) {
            const auto bodies = res["tess"].find("bodies");
            lastOpenedMeshUsable_ = bodies != res["tess"].end() &&
                                    bodies->is_array() && bodies->empty();
        }
    }
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
    if (!w.call("doc_activate", {{"doc_id", docId}, {"origin", "system"}}, res, err, kLongEngineCallTimeoutMs))
        return false;
    if (res.contains("code")) { err = jstr(res, "message"); return false; }
    lastTreeOnlyPayload_.reset();
    storeSolidPresence(res, docId_ == docId);
    ++mutationGen_;
    docId_ = docId;
    revision_ = jint(res, "revision");
    if (res.contains("tree") && res["tree"].is_object()) storeTree(res["tree"]);
    else storeTree({});
    storeFeatureStatus({});
    if (res.contains("rebuild") && res["rebuild"].is_object()) {
        const auto& rb = res["rebuild"];
        if (rb.contains("feature_status") && rb["feature_status"].is_array())
            storeFeatureStatus(rb["feature_status"]);
    }
    if (res.contains("owner_map") && res["owner_map"].is_object())
        storeOwnerMap(res["owner_map"]);
    else
        storeOwnerMap(nlohmann::json());  // clear stale owner map from the previous doc
    if (res.contains("datums") && res["datums"].is_array())
        storeRefDatums(res["datums"]);
    else
        storeRefDatums(nlohmann::json::array());
    if (res.contains("tess") && res["tess"].is_object())
        aicmPath = jstr(res["tess"], "aicm_path");
    notify();
    return true;
}


// Adopt a finished doc_activate response (AsyncRpc) as activate() would.
bool DocMirror::adoptActivateResponse(const std::string& docId, const nlohmann::json& res,
                                      std::string& aicmPath, std::string& err) {
    aicmPath.clear();
    if (docId.empty()) { err = "empty doc id"; return false; }
    if (res.contains("code")) { err = jstr(res, "message"); return false; }
    lastTreeOnlyPayload_.reset();
    storeSolidPresence(res, docId_ == docId);
    ++mutationGen_;
    docId_ = docId;
    revision_ = jint(res, "revision");
    if (res.contains("tree") && res["tree"].is_object()) storeTree(res["tree"]);
    else storeTree({});
    storeFeatureStatus({});
    if (res.contains("rebuild") && res["rebuild"].is_object()) {
        const auto& rb = res["rebuild"];
        if (rb.contains("feature_status") && rb["feature_status"].is_array())
            storeFeatureStatus(rb["feature_status"]);
    }
    if (res.contains("owner_map") && res["owner_map"].is_object())
        storeOwnerMap(res["owner_map"]);
    else
        storeOwnerMap(nlohmann::json());
    if (res.contains("datums") && res["datums"].is_array())
        storeRefDatums(res["datums"]);
    else
        storeRefDatums(nlohmann::json::array());
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
        r = parseOpsError(res);
        if (r.error_code == "stale_revision") resyncTree(w);
        return r;
    }
    // Same ops-response shape apply_ops returns — ingest it so the mirror (revision/tree/owner_map)
    // stays in step with the now-active config and the listener repaints the tree.
    r = parseOpsResponse(res);  // configurations commit before reporting their build status
    if (r.ok) {
        storeSolidPresence(res, true);
        revision_ = r.revision;
        if (!r.tree.is_null()) storeTree(r.tree);
        if (!r.feature_status.is_null()) storeFeatureStatus(r.feature_status);
        storeResponseDatums(res);
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
                                  bool relative, bool ascii, nlohmann::json* result) {
    if (docId.empty()) { err = "no document open"; return {}; }
    nlohmann::json res;
    if (!w.call("export_file",
                {{"doc_id", docId}, {"path", path}, {"format", format},
                 {"lin_deflection", linDeflection}, {"ang_deflection", angDeflection},
                 {"relative", relative}, {"ascii", ascii}},
                res, err, 60000))
        return {};
    if (res.contains("code")) { err = jstr(res, "message"); return {}; }
    auto written = jstr(res, "path");
    if (result) *result = std::move(res);
    return written;
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
