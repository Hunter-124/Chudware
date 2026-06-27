#include "engine/engine.h"

#if defined(AICAD_HAVE_BRIDGE)
#  include "src/core/cpp_core.h"   // reused flat-ABI loader (needs -I ${HOST}); routes to libaicad.dll
#endif

using nlohmann::json;

namespace eng {

bool Engine::bridgeCompiled() const {
#if defined(AICAD_HAVE_BRIDGE)
    return true;
#else
    return false;
#endif
}

bool Engine::available() {
#if defined(AICAD_HAVE_BRIDGE)
    std::string err;
    bool ok = core::cpp_core().ensureLoaded(err);
    if (!ok) lastErr_ = err;
    return ok;
#else
    lastErr_ = "built without the C++ bridge (USE_BRIDGE=OFF)";
    return false;
#endif
}

OpsResult Engine::call(const std::string& method, const json& params) {
    OpsResult r;
#if defined(AICAD_HAVE_BRIDGE)
    json result; std::string err;
    bool ok = core::cpp_core().call(method, params, result, err);
    if (!ok && !(result.is_object() && result.contains("code"))) {
        r.ok = false;
        r.errMsg = err.empty() ? ("engine call failed: " + method) : err;
        lastErr_ = r.errMsg;
        return r;
    }
    return parse(result);
#else
    (void)method; (void)params;
    r.errMsg = "no bridge"; lastErr_ = r.errMsg; return r;
#endif
}

OpsResult Engine::parse(const json& res) {
    OpsResult r;
    if (res.is_object() && res.contains("code")) {
        r.ok = false;
        r.errCode = res.value("code", std::string());
        r.errMsg  = res.value("message", std::string("engine error"));
        lastErr_  = r.errMsg;
        return r;
    }
    r.ok = true;
    r.revision = res.value("revision", revision_);
    if (res.contains("tree") && res["tree"].is_object()) r.tree = res["tree"];
    if (res.contains("rebuild") && res["rebuild"].is_object()) {
        const auto& rb = res["rebuild"];
        if (rb.contains("feature_status") && rb["feature_status"].is_array())
            r.featureStatus = rb["feature_status"];
    } else if (res.contains("feature_status") && res["feature_status"].is_array()) {
        r.featureStatus = res["feature_status"];
    }
    if (res.contains("tess") && res["tess"].is_object())
        r.aicmPath = res["tess"].value("aicm_path", std::string());
    return r;
}

void Engine::ingest(const OpsResult& r) {
    if (!r.ok) return;
    if (r.revision >= 0) revision_ = r.revision;
    if (r.tree.is_object()) tree_ = r.tree;
    if (r.featureStatus.is_array()) featureStatus_ = r.featureStatus;
    if (!r.aicmPath.empty()) aicmPath_ = r.aicmPath;
}

bool Engine::newPart(const std::string& name) {
    (void)name;
#if defined(AICAD_HAVE_BRIDGE)
    json result; std::string err;
    bool ok = core::cpp_core().call("doc_new", json{{"kind", "part"}}, result, err);
    if (!ok && !(result.is_object() && result.contains("code"))) {
        lastErr_ = err.empty() ? "doc_new failed" : err; return false;
    }
    if (result.is_object() && result.contains("code")) {
        lastErr_ = result.value("message", std::string("doc_new error")); return false;
    }
    docId_ = result.value("doc_id", std::string());
    revision_ = result.value("revision", 0);
    tree_ = json{{"features", json::array()}};
    featureStatus_ = json::array();
    aicmPath_.clear();
    if (docId_.empty()) { lastErr_ = "doc_new returned no doc_id"; return false; }
    return true;
#else
    lastErr_ = "built without the C++ bridge"; return false;
#endif
}

OpsResult Engine::apply(const json& ops, bool coalesce) {
    if (docId_.empty()) { OpsResult r; r.errMsg = "no document"; return r; }
    OpsResult r = call("doc_apply_ops", json{
        {"doc_id", docId_}, {"base_revision", revision_}, {"ops", ops}, {"coalesce", coalesce}});
    if (!r.ok && r.errCode == "stale_revision") { refreshTree(); }
    ingest(r);
    return r;
}

OpsResult Engine::addFeature(const json& feature) {
    return apply(json::array({ json{{"op", "add_feature"}, {"feature", feature}, {"before", nullptr}} }));
}

OpsResult Engine::refreshTree() {
    if (docId_.empty()) { OpsResult r; r.errMsg = "no document"; return r; }
    OpsResult r = call("doc_tree", json{{"doc_id", docId_}});
    ingest(r);
    return r;
}
OpsResult Engine::rebuild() {
    if (docId_.empty()) { OpsResult r; r.errMsg = "no document"; return r; }
    OpsResult r = call("doc_rebuild", json{{"doc_id", docId_}});
    ingest(r);
    return r;
}
OpsResult Engine::undo() { OpsResult r = call("doc_undo", json{{"doc_id", docId_}}); ingest(r); return r; }
OpsResult Engine::redo() { OpsResult r = call("doc_redo", json{{"doc_id", docId_}}); ingest(r); return r; }

// ---- convenience builders ----
OpsResult Engine::addBox(double l, double w, double h, const std::string& name) {
    return addFeature(json{{"type","box"},{"name",name},
        {"params",{{"length",dim(l)},{"width",dim(w)},{"height",dim(h)}}}});
}
OpsResult Engine::addCylinder(double r, double h, const std::string& name) {
    return addFeature(json{{"type","cylinder"},{"name",name},
        {"params",{{"radius",dim(r)},{"height",dim(h)}}}});
}
OpsResult Engine::addFilletAll(double radius, const std::string& name) {
    return addFeature(json{{"type","fillet"},{"name",name},
        {"params",{{"radius",dim(radius)}}},{"refs",{{"edges",{{"of","edge"}}}}}});
}
OpsResult Engine::addChamferAll(double length, const std::string& name) {
    return addFeature(json{{"type","chamfer"},{"name",name},
        {"params",{{"length",dim(length)}}},{"refs",{{"edges",{{"of","edge"}}}}}});
}
OpsResult Engine::addShell(double thickness, const std::string& name) {
    return addFeature(json{{"type","shell"},{"name",name},
        {"params",{{"thickness",dim(thickness)}}},{"refs",{{"remove",{{"of","face"},{"side","top"}}}}}});
}
OpsResult Engine::addDraftSides(double angleDeg, const std::string& name) {
    return addFeature(json{{"type","draft"},{"name",name},
        {"params",{{"angle",dim(angleDeg)}}},
        {"refs",{{"neutral",{{"of","face"},{"side","bottom"}}},{"faces",{{"of","face"},{"surf","flat"}}}}}});
}
OpsResult Engine::addHoleCenter(double diameter, const std::string& name) {
    return addFeature(json{{"type","hole"},{"name",name},
        {"params",{{"style","simple"},{"end","through_all"},{"diameter",diameter}}},
        {"refs",{{"face",{{"of","face"},{"side","top"}}}}}});
}

} // namespace eng
