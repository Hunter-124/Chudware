#include "app/doc_engine.h"

#include "doc_mirror.h"
#include "cpp_core.h"
#include "worker_client.h"

using nlohmann::json;

namespace eng {

Engine::Engine(WorkerClient& worker, core::DocMirror& mirror)
    : worker_(worker), mirror_(mirror) {}

bool Engine::available() {
    std::string err;
    if (core::cpp_core().ensureLoaded(err)) return true;
    if (!worker_.started()) {
        if (!worker_.start(err)) {
            lastErr_ = err.empty() ? "worker not available" : err;
            return false;
        }
    }
    return worker_.started();
}

bool Engine::hasDoc() const { return mirror_.hasDoc(); }
const std::string& Engine::docId() const { return mirror_.docId(); }
int Engine::revision() const { return mirror_.revision(); }
const json& Engine::tree() const { return mirror_.tree(); }
const json& Engine::featureStatus() const { return mirror_.featureStatus(); }
const std::string& Engine::aicmPath() const { return aicmPath_; }

OpsResult Engine::fromMirror(const core::OpsResponse& o) {
    OpsResult r;
    r.ok = o.ok;
    r.revision = o.revision;
    r.tree = o.tree;
    r.featureStatus = o.feature_status;
    r.aicmPath = o.aicm_path;
    r.errCode = o.error_code;
    r.errMsg = o.error_message;
    return r;
}

OpsResult Engine::parse(const json& res) {
    OpsResult r;
    if (res.is_object() && res.contains("code")) {
        r.ok = false;
        r.errCode = res.value("code", std::string());
        r.errMsg = res.value("message", std::string("engine error"));
        lastErr_ = r.errMsg;
        return r;
    }
    r.ok = true;
    r.revision = res.value("revision", revision());
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
    if (!r.aicmPath.empty()) aicmPath_ = r.aicmPath;
}

OpsResult Engine::call(const std::string& method, const json& params) {
    json result;
    std::string err;
    if (!worker_.call(method, params, result, err)) {
        OpsResult r;
        r.ok = false;
        r.errMsg = err.empty() ? ("call failed: " + method) : err;
        lastErr_ = r.errMsg;
        return r;
    }
    if (result.is_object() && result.contains("code")) {
        auto r = parse(result);
        r.ok = false;
        return r;
    }
    return parse(result);
}

bool Engine::newPart(const std::string& name) {
    (void)name;
    std::string err;
    if (!mirror_.newPart(worker_, err)) {
        lastErr_ = err.empty() ? "doc_new failed" : err;
        return false;
    }
    aicmPath_.clear();
    auto rb = rebuild();
    if (!rb.ok) lastErr_ = rb.errMsg.empty() ? "rebuild after new failed" : rb.errMsg;
    return rb.ok;
}

bool Engine::save(const std::string& path) {
    if (!mirror_.hasDoc()) { lastErr_ = "no document"; return false; }
    std::string err;
    if (!mirror_.save(worker_, path, err)) {
        lastErr_ = err.empty() ? "save failed" : err;
        return false;
    }
    return true;
}

bool Engine::open(const std::string& path) {
    std::string err;
    if (!mirror_.open(worker_, path, err)) {
        lastErr_ = err.empty() ? "open failed" : err;
        return false;
    }
    OpsResult rt = refreshTree();
    if (!rt.ok) { lastErr_ = rt.errMsg; return false; }
    OpsResult rb = rebuild();
    if (!rb.ok) { lastErr_ = rb.errMsg; return false; }
    return true;
}

OpsResult Engine::apply(const json& ops, bool coalesce) {
    if (!mirror_.hasDoc()) { OpsResult r; r.errMsg = "no document"; return r; }
    auto o = mirror_.applyOps(worker_, ops, coalesce);
    OpsResult r = fromMirror(o);
    if (!r.ok && r.errCode == "stale_revision") refreshTree();
    ingest(r);
    return r;
}

OpsResult Engine::addFeature(const json& feature) {
    return apply(json::array({json{{"op", "add_feature"}, {"feature", feature}, {"before", nullptr}}}));
}

OpsResult Engine::refreshTree() {
    if (!mirror_.hasDoc()) { OpsResult r; r.errMsg = "no document"; return r; }
    if (!mirror_.resync(worker_)) {
        OpsResult r;
        r.errMsg = "doc_tree failed";
        lastErr_ = r.errMsg;
        return r;
    }
    OpsResult r;
    r.ok = true;
    r.revision = mirror_.revision();
    r.tree = mirror_.tree();
    r.featureStatus = mirror_.featureStatus();
    return r;
}

OpsResult Engine::rebuild() {
    if (!mirror_.hasDoc()) { OpsResult r; r.errMsg = "no document"; return r; }
    OpsResult r = call("doc_rebuild", json{{"doc_id", mirror_.docId()}});
    ingest(r);
    return r;
}

OpsResult Engine::undo() {
    if (!mirror_.hasDoc()) { OpsResult r; r.errMsg = "no document"; lastErr_ = r.errMsg; return r; }
    OpsResult r = fromMirror(mirror_.undo(worker_));
    ingest(r);
    return r;
}

OpsResult Engine::redo() {
    if (!mirror_.hasDoc()) { OpsResult r; r.errMsg = "no document"; lastErr_ = r.errMsg; return r; }
    OpsResult r = fromMirror(mirror_.redo(worker_));
    ingest(r);
    return r;
}

OpsResult Engine::addBox(double l, double w, double h, const std::string& name) {
    return addFeature(json{{"type", "box"}, {"name", name},
                           {"params", {{"length", dim(l)}, {"width", dim(w)}, {"height", dim(h)}}}});
}
OpsResult Engine::addCylinder(double r, double h, const std::string& name) {
    return addFeature(json{{"type", "cylinder"}, {"name", name},
                           {"params", {{"radius", dim(r)}, {"height", dim(h)}}}});
}
OpsResult Engine::addSphere(double r, const std::string& name) {
    return addFeature(json{{"type", "sphere"}, {"name", name}, {"params", {{"radius", dim(r)}}}});
}
OpsResult Engine::addCone(double r1, double r2, double h, const std::string& name) {
    return addFeature(json{{"type", "cone"}, {"name", name},
                           {"params", {{"r1", dim(r1)}, {"r2", dim(r2)}, {"height", dim(h)}}}});
}
OpsResult Engine::addFilletAll(double radius, const std::string& name) {
    return addFeature(json{{"type", "fillet"}, {"name", name},
                           {"params", {{"radius", dim(radius)}}},
                           {"refs", {{"edges", {{"of", "edge"}}}}}});
}
OpsResult Engine::addChamferAll(double length, const std::string& name) {
    return addFeature(json{{"type", "chamfer"}, {"name", name},
                           {"params", {{"length", dim(length)}}},
                           {"refs", {{"edges", {{"of", "edge"}}}}}});
}
OpsResult Engine::addShell(double thickness, const std::string& name) {
    return addFeature(json{{"type", "shell"}, {"name", name},
                           {"params", {{"thickness", dim(thickness)}}},
                           {"refs", {{"remove", {{"of", "face"}, {"side", "top"}}}}}});
}
OpsResult Engine::addDraftSides(double angleDeg, const std::string& name) {
    return addFeature(json{{"type", "draft"}, {"name", name},
                           {"params", {{"angle", dim(angleDeg)}}},
                           {"refs", {{"neutral", {{"of", "face"}, {"side", "bottom"}}},
                                     {"faces", {{"of", "face"}, {"surf", "flat"}}}}}});
}
OpsResult Engine::addHoleCenter(double diameter, const std::string& name) {
    return addFeature(json{{"type", "hole"}, {"name", name},
                           {"params", {{"style", "simple"}, {"standard", "custom"},
                                       {"end", "through_all"}, {"diameter", dim(diameter)}}},
                           {"refs", {{"face", {{"of", "face"}, {"side", "top"}}}}}});
}

json Engine::resolvePick(uint32_t body_id, const std::string& kind, uint32_t topo_id) {
    if (!mirror_.hasDoc()) { lastErr_ = "no document"; return json(); }
    std::string err;
    std::string sel = mirror_.resolvePick(worker_, body_id, kind, topo_id, err);
    if (sel.empty()) { lastErr_ = err.empty() ? "resolve failed" : err; return json(); }
    try { return json::parse(sel); } catch (...) { lastErr_ = "bad selector json"; return json(); }
}

OpsResult Engine::addShellFaces(double thickness, const json& removeRefs) {
    return addFeature(json{{"type", "shell"}, {"name", "Shell"},
                           {"params", {{"thickness", dim(thickness)}}},
                           {"refs", {{"remove", removeRefs}}}});
}
OpsResult Engine::addDraftFaces(double angleDeg, const json& faceRefs) {
    return addFeature(json{{"type", "draft"}, {"name", "Draft"},
                           {"params", {{"angle", dim(angleDeg)}}},
                           {"refs", {{"neutral", {{"of", "face"}, {"side", "bottom"}}},
                                     {"faces", faceRefs}}}});
}
OpsResult Engine::addFilletEdges(double radius, const json& edgeRefs) {
    return addFeature(json{{"type", "fillet"}, {"name", "Fillet"},
                           {"params", {{"radius", dim(radius)}}}, {"refs", {{"edges", edgeRefs}}}});
}
OpsResult Engine::addChamferEdges(double length, const json& edgeRefs) {
    return addFeature(json{{"type", "chamfer"}, {"name", "Chamfer"},
                           {"params", {{"length", dim(length)}}}, {"refs", {{"edges", edgeRefs}}}});
}

OpsResult Engine::sketchRectExtrude(const std::string& plane, double w, double h, double depth,
                                    const std::string& mode) {
    double x = w * 0.5, y = h * 0.5;
    json ops = json::array();
    ops.push_back({{"op", "add_feature"},
                   {"feature", {{"type", "sketch"}, {"name", "Sketch"},
                                {"params", {{"entities", json::array()}, {"constraints", json::array()}}},
                                {"refs", {{"plane", plane}}}}}});
    auto line = [&](double x1, double y1, double x2, double y2) {
        ops.push_back({{"op", "sketch_add_entity"}, {"sketch", "$0"},
                       {"entity", {{"type", "line"}, {"p1", {x1, y1}}, {"p2", {x2, y2}}}}});
    };
    line(-x, -y, x, -y);
    line(x, -y, x, y);
    line(x, y, -x, y);
    line(-x, y, -x, -y);
    ops.push_back({{"op", "add_feature"},
                   {"feature", {{"type", "extrude"}, {"name", "Extrude"},
                                {"params", {{"mode", mode}, {"end", "blind"}, {"depth", dim(depth)}}},
                                {"refs", {{"profile", json::array({json{{"kind", "region"},
                                                                    {"sketch", "$0"},
                                                                    {"centroid", {0, 0}},
                                                                    {"hint_index", 0}}})}}}}}});
    return apply(ops);
}

OpsResult Engine::sketchCircleExtrude(const std::string& plane, double r, double depth,
                                      const std::string& mode) {
    json ops = json::array();
    ops.push_back({{"op", "add_feature"},
                   {"feature", {{"type", "sketch"}, {"name", "Sketch"},
                                {"params", {{"entities", json::array()}, {"constraints", json::array()}}},
                                {"refs", {{"plane", plane}}}}}});
    ops.push_back({{"op", "sketch_add_entity"}, {"sketch", "$0"},
                   {"entity", {{"type", "circle"}, {"center", {0, 0}}, {"radius", r}}}});
    ops.push_back({{"op", "add_feature"},
                   {"feature", {{"type", "extrude"}, {"name", "Extrude"},
                                {"params", {{"mode", mode}, {"end", "blind"}, {"depth", dim(depth)}}},
                                {"refs", {{"profile", json::array({json{{"kind", "region"},
                                                                    {"sketch", "$0"},
                                                                    {"centroid", {0, 0}},
                                                                    {"hint_index", 0}}})}}}}}});
    return apply(ops);
}

void Engine::startAgent(const std::string& request) {
    if (agentRunning_.load() || !mirror_.hasDoc()) return;
    agentRunning_ = true;
    agentDone_ = false;
    {
        std::lock_guard<std::mutex> lk(agentMtx_);
        agentResult_ = json::object();
    }
    std::string req = request;
    std::string dId = mirror_.docId();
    if (agentThread_.joinable()) agentThread_.join();
    agentThread_ = std::thread([this, req, dId]() {
        json result;
        std::string err;
        bool ok = worker_.call("agent_run_v2",
                               json{{"request", req}, {"doc_id", dId}}, result, err);
        if (!ok && !(result.is_object() && result.contains("code")))
            result = json{{"ok", false}, {"error", err.empty() ? "agent_run_v2 failed" : err}};
        {
            std::lock_guard<std::mutex> lk(agentMtx_);
            agentResult_ = std::move(result);
        }
        agentRunning_ = false;
        agentDone_ = true;
    });
    agentThread_.detach();
}

json Engine::takeAgentResult() {
    std::lock_guard<std::mutex> lk(agentMtx_);
    agentDone_ = false;
    json r = std::move(agentResult_);
    agentResult_ = json::object();
    return r;
}

}  // namespace eng
