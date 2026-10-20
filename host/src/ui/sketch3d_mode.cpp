// 3D Sketch mode — implementation. See sketch3d_mode.h and contracts/sketch3d_gui_contract.md.
#include "sketch3d_mode.h"

#include <algorithm>
#include <cmath>
#include <cstdio>

#include "../core/app_state.h"
#include "../renderer.h"
#include "../ui_theme.h"
#include "toast.h"
#include "viewport_panel.h"
#include "metrics.h"
#include "layout.h"

namespace ui {

namespace {

// Projection helpers — local copies of sketch_mode.cpp's anonymous-namespace versions (same
// math; that TU's helpers have internal linkage so they cannot be shared without a refactor).
bool worldToScreen(const Renderer::CameraXform& cam, const ImVec2& imgMin, int vw, int vh,
                   float wx, float wy, float wz, ImVec2& out) {
    const float* m = cam.mvp.m;   // column-major: clip[r] = sum_c m[c*4+r]*p[c]
    float p[4] = {wx, wy, wz, 1.0f};
    float c[4];
    for (int r = 0; r < 4; ++r)
        c[r] = m[0 * 4 + r] * p[0] + m[1 * 4 + r] * p[1] + m[2 * 4 + r] * p[2] + m[3 * 4 + r] * p[3];
    if (c[3] <= 1e-6f) return false;
    float ndcX = c[0] / c[3];
    float ndcY = c[1] / c[3];
    out.x = imgMin.x + (ndcX * 0.5f + 0.5f) * (float)vw;
    out.y = imgMin.y + (1.0f - (ndcY * 0.5f + 0.5f)) * (float)vh;
    return true;
}

bool invert4x4(const float* m, float* inv) {
    inv[0]  =  m[5]*m[10]*m[15] - m[5]*m[11]*m[14] - m[9]*m[6]*m[15] + m[9]*m[7]*m[14] + m[13]*m[6]*m[11] - m[13]*m[7]*m[10];
    inv[4]  = -m[4]*m[10]*m[15] + m[4]*m[11]*m[14] + m[8]*m[6]*m[15] - m[8]*m[7]*m[14] - m[12]*m[6]*m[11] + m[12]*m[7]*m[10];
    inv[8]  =  m[4]*m[9]*m[15]  - m[4]*m[11]*m[13] - m[8]*m[5]*m[15] + m[8]*m[7]*m[13] + m[12]*m[5]*m[11] - m[12]*m[7]*m[9];
    inv[12] = -m[4]*m[9]*m[14]  + m[4]*m[10]*m[13] + m[8]*m[5]*m[14] - m[8]*m[6]*m[13] - m[12]*m[5]*m[10] + m[12]*m[6]*m[9];
    inv[1]  = -m[1]*m[10]*m[15] + m[1]*m[11]*m[14] + m[9]*m[2]*m[15] - m[9]*m[3]*m[14] - m[13]*m[2]*m[11] + m[13]*m[3]*m[10];
    inv[5]  =  m[0]*m[10]*m[15] - m[0]*m[11]*m[14] - m[8]*m[2]*m[15] + m[8]*m[3]*m[14] + m[12]*m[2]*m[11] - m[12]*m[3]*m[10];
    inv[9]  = -m[0]*m[9]*m[15]  + m[0]*m[11]*m[13] + m[8]*m[1]*m[15] - m[8]*m[3]*m[13] - m[12]*m[1]*m[11] + m[12]*m[3]*m[9];
    inv[13] =  m[0]*m[9]*m[14]  - m[0]*m[10]*m[13] - m[8]*m[1]*m[14] + m[8]*m[2]*m[13] + m[12]*m[1]*m[10] - m[12]*m[2]*m[9];
    inv[2]  =  m[1]*m[6]*m[15]  - m[1]*m[7]*m[14]  - m[5]*m[2]*m[15] + m[5]*m[3]*m[14] + m[13]*m[2]*m[7]  - m[13]*m[3]*m[6];
    inv[6]  = -m[0]*m[6]*m[15]  + m[0]*m[7]*m[14]  + m[4]*m[2]*m[15] - m[4]*m[3]*m[14] - m[12]*m[2]*m[7]  + m[12]*m[3]*m[6];
    inv[10] =  m[0]*m[5]*m[15]  - m[0]*m[7]*m[13]  - m[4]*m[1]*m[15] + m[4]*m[3]*m[13] + m[12]*m[1]*m[7]  - m[12]*m[3]*m[5];
    inv[14] = -m[0]*m[5]*m[14]  + m[0]*m[6]*m[13]  + m[4]*m[1]*m[14] - m[4]*m[2]*m[13] - m[12]*m[1]*m[6]  + m[12]*m[2]*m[5];
    inv[3]  = -m[1]*m[6]*m[11]  + m[1]*m[7]*m[10]  + m[5]*m[2]*m[11] - m[5]*m[3]*m[10] - m[9]*m[2]*m[7]   + m[9]*m[3]*m[6];
    inv[7]  =  m[0]*m[6]*m[11]  - m[0]*m[7]*m[10]  - m[4]*m[2]*m[11] + m[4]*m[3]*m[10] + m[8]*m[2]*m[7]   - m[8]*m[3]*m[6];
    inv[11] = -m[0]*m[5]*m[11]  + m[0]*m[7]*m[9]   + m[4]*m[1]*m[11] - m[4]*m[3]*m[9]  - m[8]*m[1]*m[7]   + m[8]*m[3]*m[5];
    inv[15] =  m[0]*m[5]*m[10]  - m[0]*m[6]*m[9]   - m[4]*m[1]*m[10] + m[4]*m[2]*m[9]  + m[8]*m[1]*m[6]   - m[8]*m[2]*m[5];
    float det = m[0]*inv[0] + m[1]*inv[4] + m[2]*inv[8] + m[3]*inv[12];
    if (std::fabs(det) < 1e-20f) return false;
    det = 1.0f / det;
    for (int i = 0; i < 16; ++i) inv[i] *= det;
    return true;
}

bool screenRay(const Renderer::CameraXform& cam, const ImVec2& imgMin, int vw, int vh,
               ImVec2 mouse, Vec3& outOrig, Vec3& outDir) {
    float inv[16];
    if (!invert4x4(cam.mvp.m, inv)) return false;
    float ndcX = ((mouse.x - imgMin.x) / (float)vw) * 2.0f - 1.0f;
    float ndcY = 1.0f - ((mouse.y - imgMin.y) / (float)vh) * 2.0f;
    auto unproj = [&](float ndcZ, Vec3& o) -> bool {
        float cl[4] = {ndcX, ndcY, ndcZ, 1.0f}, w[4];
        for (int r = 0; r < 4; ++r)
            w[r] = inv[0*4+r]*cl[0] + inv[1*4+r]*cl[1] + inv[2*4+r]*cl[2] + inv[3*4+r]*cl[3];
        if (std::fabs(w[3]) < 1e-12f) return false;
        o = Vec3(w[0]/w[3], w[1]/w[3], w[2]/w[3]);
        return true;
    };
    Vec3 pNear, pFar;
    if (!unproj(-1.0f, pNear) || !unproj(1.0f, pFar)) return false;
    Vec3 d = pFar - pNear;
    float len = length(d);
    if (len < 1e-9f) return false;
    outOrig = pNear;
    outDir = d * (1.0f / len);
    return true;
}

constexpr float kSnapPx = 10.0f;       // point-handle snap radius (screen px, pre-DPI)
constexpr float kAxisSnapPx = 6.0f;    // axis-inference pull radius (screen px, pre-DPI)
constexpr float kHandlePx = 4.0f;      // handle square half-size
constexpr float kEntHitPx = 9.0f;      // whole-entity pick distance

ImU32 col(const float c[4], float aMul = 1.0f) {
    return ImGui::ColorConvertFloat4ToU32(ImVec4(c[0], c[1], c[2], c[3] * aMul));
}
ImU32 col3(const float c[4], float a) {
    return ImGui::ColorConvertFloat4ToU32(ImVec4(c[0], c[1], c[2], a));
}

float distPtSeg(const ImVec2& p, const ImVec2& a, const ImVec2& b) {
    float dx = b.x - a.x, dy = b.y - a.y;
    float l2 = dx * dx + dy * dy;
    float t = l2 > 1e-9f ? ((p.x - a.x) * dx + (p.y - a.y) * dy) / l2 : 0.0f;
    t = t < 0 ? 0 : (t > 1 ? 1 : t);
    return std::hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
}

// Circumcenter of 3 points in 3D (the arc's display center). False when near-collinear.
bool circumcenter3(const Vec3& a, const Vec3& b, const Vec3& c, Vec3& out) {
    Vec3 ab = b - a, ac = c - a;
    Vec3 n = cross(ab, ac);
    float n2 = dot(n, n);
    if (n2 < 1e-10f) return false;
    // center = a + (|ac|^2 (n x ab) + |ab|^2 (ac x n)) / (2 |n|^2)
    Vec3 v = (cross(n, ab) * dot(ac, ac) + cross(ac, n) * dot(ab, ab)) * (1.0f / (2.0f * n2));
    out = a + v;
    return true;
}

// json [x,y,z] -> Vec3
Vec3 jv(const nlohmann::json& a) {
    if (!a.is_array() || a.size() < 3) return {0, 0, 0};
    return {(float)a[0].get<double>(), (float)a[1].get<double>(), (float)a[2].get<double>()};
}
nlohmann::json vj(const Vec3& v) {
    return nlohmann::json::array({(double)v.x, (double)v.y, (double)v.z});
}

}  // namespace

// ---------------------------------------------------------------------------
// worker round-trips
// ---------------------------------------------------------------------------

bool Sketch3DMode::callSk3(AppState& st, const char* method, const nlohmann::json& extra,
                           nlohmann::json& result) {
    if (!st.docMirror.hasDoc() || !st.workerStarted) return false;
    nlohmann::json params = extra;
    params["doc_id"] = st.docMirror.docId();
    params["sketch"] = fid_;
    std::string err;
    if (!st.worker.call(method, params, result, err)) {
        st.addLog(std::string("[sketch3d] ") + method + " transport error: " + err);
        return false;
    }
    if (result.contains("code")) {   // structured OpError envelope
        st.addLog(std::string("[sketch3d] ") + method + " error: " +
                  result.value("message", std::string("?")));
        return false;
    }
    return true;
}

void Sketch3DMode::applyEntities(const nlohmann::json& res) {
    if (!res.contains("entities") || !res["entities"].is_array()) return;
    const nlohmann::json* stats =
        res.contains("statuses") && res["statuses"].is_object() ? &res["statuses"] : nullptr;
    ents_.clear();
    for (const auto& e : res["entities"]) {
        Sk3Entity se;
        se.id = e.value("id", std::string());
        se.type = e.value("type", std::string());
        se.construction = e.value("construction", false);
        if (stats && stats->contains(se.id)) se.status = (*stats)[se.id].get<std::string>();
        if (se.type == "point") {
            se.pts.push_back(jv(e.value("p", nlohmann::json())));
        } else if (se.type == "line") {
            se.pts.push_back(jv(e.value("p1", nlohmann::json())));
            se.pts.push_back(jv(e.value("p2", nlohmann::json())));
        } else if (se.type == "arc") {
            se.pts.push_back(jv(e.value("p1", nlohmann::json())));
            se.pts.push_back(jv(e.value("pm", nlohmann::json())));
            se.pts.push_back(jv(e.value("p2", nlohmann::json())));
        } else if (se.type == "spline" && e.contains("points") && e["points"].is_array()) {
            for (const auto& p : e["points"]) se.pts.push_back(jv(p));
            if (e.contains("samples") && e["samples"].is_array())
                for (const auto& p : e["samples"]) se.samples.push_back(jv(p));
        }
        ents_.push_back(std::move(se));
    }
}

void Sketch3DMode::refreshHealth(AppState& st) {
    nlohmann::json res;
    if (!callSk3(st, "sketch3d_health", nlohmann::json::object(), res)) return;
    applyEntities(res);
    dof_ = res.value("dof", 0);
    status_ = res.value("status", std::string("under"));
    converged_ = res.value("converged", true);
    nWires_ = res.contains("wires") && res["wires"].is_array() ? (int)res["wires"].size() : 0;
    warning_.clear();
    for (const auto& ref : externalRefs_)
        if (ref.value("stale", false)) {
            warning_ = "Model reference " + ref.value("id", std::string()) +
                       " is stale; last known position retained.";
            break;
        }
    if (res.contains("skipped_constraints") && !res["skipped_constraints"].empty())
        warning_ += (warning_.empty() ? "" : "  ") +
                    std::to_string(res["skipped_constraints"].size()) + " relation(s) not applied.";
}

void Sketch3DMode::reloadParamsFromTree(AppState& st) {
    entJson_ = nlohmann::json::array();
    conJson_ = nlohmann::json::array();
    externalRefs_ = nlohmann::json::array();
    const auto& tree = st.docMirror.tree();
    if (!tree.is_object() || !tree.contains("features")) return;
    for (const auto& f : tree["features"]) {
        if (f.value("id", std::string()) != fid_) continue;
        const auto& params = f.value("params", nlohmann::json::object());
        if (params.contains("entities") && params["entities"].is_array())
            entJson_ = params["entities"];
        if (params.contains("constraints") && params["constraints"].is_array())
            conJson_ = params["constraints"];
        if (params.contains("external_refs") && params["external_refs"].is_array())
            externalRefs_ = params["external_refs"];
        return;
    }
}

bool Sketch3DMode::commitParams(AppState& st, const nlohmann::json* entities,
                                const nlohmann::json* constraints, const char* label) {
    nlohmann::json params = nlohmann::json::object();
    if (entities) params["entities"] = *entities;
    if (constraints) params["constraints"] = *constraints;
    nlohmann::json op = {{"op", "edit_feature"}, {"id", fid_}, {"params", params}};
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    if (!r.ok) {
        st.toast(ui::ToastLevel::Warn, std::string(label) + ": " + r.error_message);
        reloadParamsFromTree(st);   // drop the rejected local edit
        return false;
    }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    if (entities) entJson_ = *entities;
    if (constraints) conJson_ = *constraints;
    reloadParamsFromTree(st);
    refreshHealth(st);
    return true;
}

std::string Sketch3DMode::nextId(const char* prefix) const {
    const nlohmann::json& arr = prefix[0] == 'e' ? entJson_ : conJson_;
    int mx = 0;
    for (const auto& e : arr) {
        std::string id = e.value("id", std::string());
        if (id.size() > 1 && id[0] == prefix[0]) mx = std::max(mx, std::atoi(id.c_str() + 1));
    }
    return std::string(prefix) + std::to_string(mx + 1);
}

std::string Sketch3DMode::addEntity(AppState& st, nlohmann::json entity,
                                    nlohmann::json constraints) {
    std::string id = nextId("e");
    entity["id"] = id;
    entity["construction"] = constructionOn_;
    nlohmann::json ents = entJson_; ents.push_back(entity);
    nlohmann::json cons = conJson_;
    if (constraints.is_array())
        for (auto c : constraints) {
            // resolve "$self" placeholder to the assigned entity id
            if (c.contains("refs"))
                for (auto& r : c["refs"]) {
                    std::string s = r.get<std::string>();
                    auto p = s.find("$self");
                    if (p != std::string::npos) r = s.substr(0, p) + id + s.substr(p + 5);
                }
            c["id"] = nextId("c");
            cons.push_back(c);
        }
    if (!commitParams(st, &ents, &cons, "Add entity")) return {};
    return id;
}

void Sketch3DMode::addConstraint(AppState& st, nlohmann::json con, const char* label) {
    con["id"] = nextId("c");
    nlohmann::json cons = conJson_; cons.push_back(con);
    commitParams(st, nullptr, &cons, label);
}

bool Sketch3DMode::isExternalHandle(const std::string& handle) const {
    return !handle.empty() && handle[0] == 'x';
}

void Sketch3DMode::setModelVertexPicking(AppState& st, bool enabled) {
    if (enabled == modelVertexPicking_) return;
    if (enabled) {
        resetTool();
        savedPickFaces_ = st.selection.allowFaces;
        savedPickEdges_ = st.selection.allowEdges;
        savedPickVertices_ = st.selection.allowVertices;
        st.selection.allowFaces = st.selection.allowEdges = false;
        st.selection.allowVertices = true;
        st.selection.clear();
    } else {
        st.selection.allowFaces = savedPickFaces_;
        st.selection.allowEdges = savedPickEdges_;
        st.selection.allowVertices = savedPickVertices_;
    }
    modelVertexPicking_ = enabled;
}

void Sketch3DMode::consumeModelVertexPick(AppState& st) {
    if (!modelVertexPicking_ || st.selection.empty()) return;
    // app.cpp resolves ordinary viewport selections through its asynchronous
    // doc_resolve_pick queue, including document/revision guards. Never block
    // the overlay on another resolve call or reuse an unresolved id-pass hit.
    const auto& item = st.selection.items().back();
    if (item.hit.kind != render::PickKind::Vertex || item.pending_resolve) return;
    if (!item.resolution_error.empty()) {
        st.toast(ui::ToastLevel::Warn, item.resolution_error);
        st.selection.clear();
        return;
    }
    auto selector = nlohmann::json::parse(item.selector, nullptr, false);
    if (!selector.is_object()) return;
    reloadParamsFromTree(st);
    for (const auto& ref : externalRefs_) {
        if (ref.contains("source") && ref["source"].value("selector", nlohmann::json()) == selector) {
            const std::string id = ref.value("id", std::string());
            if (std::find(selHandles_.begin(), selHandles_.end(), id) == selHandles_.end())
                selHandles_.push_back(id);
            setModelVertexPicking(st, false);
            st.selection.clear();
            return;
        }
    }
    int next = 1;
    const auto& tree = st.docMirror.tree();
    if (tree.contains("features")) for (const auto& f : tree["features"]) {
        const auto params = f.value("params", nlohmann::json::object());
        if (!params.contains("external_refs") || !params["external_refs"].is_array()) continue;
        for (const auto& ref : params["external_refs"]) {
            const std::string id = ref.value("id", std::string());
            if (!id.empty() && id[0] == 'x') next = std::max(next, std::atoi(id.c_str() + 1) + 1);
        }
    }
    const std::string id = "x" + std::to_string(next);
    auto refs = externalRefs_;
    refs.push_back({{"id", id}, {"source", {{"kind", "vertex"}, {"selector", selector}}}, {"stale", true}});
    nlohmann::json op{{"op", "edit_feature"}, {"id", fid_}, {"params", {{"external_refs", refs}}}};
    auto result = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    setModelVertexPicking(st, false);
    st.selection.clear();
    if (!result.ok) {
        st.toast(ui::ToastLevel::Warn, "Reference Vertex: " + result.error_message);
        return;
    }
    if (!result.aicm_path.empty()) { std::string error; st.loadDocMesh(result.aicm_path, error); }
    reloadParamsFromTree(st);
    refreshHealth(st);
    selHandles_.push_back(id);
}

// ---------------------------------------------------------------------------
// enter / exit
// ---------------------------------------------------------------------------

bool Sketch3DMode::enterForFeature(AppState& st, const std::string& fid) {
    if (st.activeDocLockedByAgent()) {
        st.toast(ui::ToastLevel::Warn, st.agentLockReason(st.docMirror.docId()));
        return false;
    }
    if (active_) exitSketch(st, true);
    if (!st.docMirror.hasDoc() || !st.workerStarted) return false;
    fid_ = fid;
    docId_ = st.docMirror.docId();
    reloadParamsFromTree(st);
    refreshHealth(st);
    active_ = true;
    isNewEmpty_ = false;
    tool_ = Sk3Tool::None;
    toolPts_.clear(); toolSnaps_.clear(); toolAxes_.clear(); chainPrevHandle_.clear();
    clearSel();
    setPlanePreset(st, 0);
    wpO_ = {0, 0, 0};
    // Cancel checkpoint so the confirmation-corner ✗ can revert the whole session.
    nlohmann::json cres, cp = {{"doc_id", docId_}};
    std::string cerr;
    cancelCheckpoint_.clear();
    if (st.worker.call("doc_checkpoint", cp, cres, cerr) && cres.contains("checkpoint"))
        cancelCheckpoint_ = cres["checkpoint"].get<std::string>();
    st.addLog("[sketch3d] editing " + fid_);
    return true;
}

bool Sketch3DMode::enterNew(AppState& st) {
    if (st.activeDocLockedByAgent()) {
        st.toast(ui::ToastLevel::Warn, st.agentLockReason(st.docMirror.docId()));
        return false;
    }
    if (!st.docMirror.hasDoc() || !st.workerStarted) return false;
    nlohmann::json op = {{"op", "add_feature"},
                         {"feature", {{"type", "sketch3d"}, {"name", "3DSketch"},
                                      {"params", {{"entities", nlohmann::json::array()},
                                                  {"constraints", nlohmann::json::array()}}}}}};
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    if (!r.ok) { st.toast(ui::ToastLevel::Error, "3D Sketch: " + r.error_message); return false; }
    std::string fid = (r.id_map.is_object() && r.id_map.contains("$0"))
                          ? r.id_map["$0"].get<std::string>() : std::string();
    if (fid.empty()) return false;
    bool ok = enterForFeature(st, fid);
    isNewEmpty_ = ok;
    return ok;
}

void Sketch3DMode::exitSketch(AppState& st, bool keep) {
    if (!active_) return;
    setModelVertexPicking(st, false);
    const bool locked = st.docLockedByAgent(docId_);
    if (locked) keep = true;
    if (!keep && !cancelCheckpoint_.empty() && st.docMirror.docId() == docId_) {
        nlohmann::json res, params = {{"doc_id", docId_}, {"checkpoint", cancelCheckpoint_}};
        std::string err;
        if (st.worker.call("doc_revert", params, res, err) && !res.contains("code")) {
            if (res.contains("tess") && res["tess"].is_object()) {
                std::string aicm = res["tess"].value("aicm_path", std::string());
                if (!aicm.empty()) { std::string le; st.loadDocMesh(aicm, le); }
            }
            st.docMirror.resync(st.worker);
        }
    }
    // An EMPTY sketch is noise — delete it on exit rather than leaving a contentless feature in the
    // tree. Fires for a never-populated NEW sketch AND for an existing sketch the user emptied out.
    // Note: the cancel checkpoint above was taken AFTER the empty feature was created (enterNew
    // adds the feature, then enterForFeature snapshots it), so a discard-revert alone leaves that
    // empty feature sitting in the tree; this delete cleans it up on both the keep and discard
    // paths.
    // keep=true (Finish/exit): delete any empty sketch, new or existing — requirement 3.
    // keep=false (Cancel): only delete a NEW empty sketch. An EXISTING sketch the user emptied was
    // just restored by the revert block above; entJson_ (the in-memory mirror) is stale-empty and
    // must NOT trigger a delete of the restored feature — the isNewEmpty_ gate protects it.
    if (!locked && entJson_.empty() && (keep || isNewEmpty_) && st.docMirror.docId() == docId_) {
        nlohmann::json op = {{"op", "delete_feature"}, {"id", fid_}};
        st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    }
    active_ = false;
    fid_.clear(); docId_.clear(); cancelCheckpoint_.clear();
    ents_.clear(); entJson_ = nlohmann::json::array(); conJson_ = nlohmann::json::array();
    resetTool();
    clearSel();
    dragArmed_ = dragging_ = false;
    dimPopupOpen_ = false;
    planePopupOpen_ = false;
}

bool Sketch3DMode::onRibbonCommand(AppState& st, const std::string& cmdId) {
    if (cmdId == "sketch3d_new") {
        if (active_) exitSketch(st, true);
        else enterNew(st);
        return true;
    }
    if (!active_) return false;
    if (cmdId == "sketch_exit" || cmdId == "sketch3d_exit") { exitSketch(st, true); return true; }
    if (cmdId == "sketch3d_cancel") { exitSketch(st, false); return true; }   // ribbon commit cluster ✗
    if (cmdId.rfind("sk3d_", 0) == 0 && cmdId != "sk3d_relations")
        setModelVertexPicking(st, false);
    // Ribbon tool buttons (Sketch tab, swapped to the "Sketch3D" row set while editing — see
    // ribbon.cpp/ribbon_def.cpp). Mirror the old floating overlay toolbar's tool-arming behavior.
    if (cmdId == "sk3d_line")   { resetTool(); tool_ = Sk3Tool::Line;   clearSel(); return true; }
    if (cmdId == "sk3d_arc")    { resetTool(); tool_ = Sk3Tool::Arc3;   clearSel(); return true; }
    if (cmdId == "sk3d_spline") { resetTool(); tool_ = Sk3Tool::Spline; clearSel(); return true; }
    if (cmdId == "sk3d_point")  { resetTool(); tool_ = Sk3Tool::Point;  clearSel(); return true; }
    if (cmdId == "sk3d_plane_xy")   { setPlanePreset(st, 0); return true; }
    if (cmdId == "sk3d_plane_xz")   { setPlanePreset(st, 1); return true; }
    if (cmdId == "sk3d_plane_yz")   { setPlanePreset(st, 2); return true; }
    if (cmdId == "sk3d_plane_view") { setPlanePreset(st, 3); return true; }
    if (cmdId == "sk3d_construction") { constructionOn_ = !constructionOn_; return true; }
    if (cmdId == "sk3d_delete") { deleteSelection(st); return true; }
    if (cmdId == "sk3d_fix") {
        for (const auto& h : selHandles_)
            if (h != "origin" && !isExternalHandle(h))
                addConstraint(st, {{"type", "fix"}, {"refs", {h}}}, "Fix");
        for (const auto& id : selEnts_) addConstraint(st, {{"type", "fix"}, {"refs", {id}}}, "Fix");
        clearSel();
        return true;
    }
    // Relation kinds depend on the selection; the viewport status strip offers contextual buttons.
    if (cmdId == "sk3d_relations") {
        st.toast(ui::ToastLevel::Info, "Add Relation: use the contextual relation buttons in the viewport status strip");
        return true;
    }
    return false;
}

std::string Sketch3DMode::statusLine() const {
    if (!active_) return {};
    if (status_ == "over" || !converged_) return "Over Defined";
    if (status_ == "full") return "Fully Defined";
    return "Under Defined   DOF: " + std::to_string(dof_);
}

// ---------------------------------------------------------------------------
// working plane
// ---------------------------------------------------------------------------

void Sketch3DMode::setPlanePreset(AppState& st, int preset) {
    wpPreset_ = preset;
    switch (preset) {
        case 0: wpU_ = {1, 0, 0}; wpV_ = {0, 1, 0}; break;   // XY (top)
        case 1: wpU_ = {1, 0, 0}; wpV_ = {0, 0, 1}; break;   // XZ (front)
        case 2: wpU_ = {0, 1, 0}; wpV_ = {0, 0, 1}; break;   // YZ (right)
        default: {                                            // view-aligned
            const float deg = 3.14159265f / 180.0f;
            float yr = st.yaw * deg, pr = st.pitch * deg;
            Vec3 vdir(std::cos(pr) * std::sin(yr), -std::cos(pr) * std::cos(yr), std::sin(pr));
            Vec3 wUp = normalize(Vec3(-std::sin(pr) * std::sin(yr),
                                       std::sin(pr) * std::cos(yr), std::cos(pr)));
            Vec3 V = normalize(vdir);
            wpU_ = normalize(cross(wUp, V));
            wpV_ = normalize(cross(V, wpU_));
            break;
        }
    }
}

void Sketch3DMode::drawPlaneGrid(ImDrawList* dl, AppState& st, const ImVec2& imgMin,
                                 int vw, int vh) const {
    if (!st.renderer) return;
    const auto& cam = st.renderer->camera();
    const float kStep = 10.0f, kHalf = 60.0f;   // 120mm grid, 10mm pitch
    const ImU32 gc = col(ui::palette.sketchGrid);
    for (float t = -kHalf; t <= kHalf + 0.1f; t += kStep) {
        ImVec2 a, b, c, d;
        Vec3 p0 = wpO_ + wpU_ * t - wpV_ * kHalf, p1 = wpO_ + wpU_ * t + wpV_ * kHalf;
        Vec3 q0 = wpO_ + wpV_ * t - wpU_ * kHalf, q1 = wpO_ + wpV_ * t + wpU_ * kHalf;
        if (worldToScreen(cam, imgMin, vw, vh, p0.x, p0.y, p0.z, a) &&
            worldToScreen(cam, imgMin, vw, vh, p1.x, p1.y, p1.z, b))
            dl->AddLine(a, b, gc, 1.0f);
        if (worldToScreen(cam, imgMin, vw, vh, q0.x, q0.y, q0.z, c) &&
            worldToScreen(cam, imgMin, vw, vh, q1.x, q1.y, q1.z, d))
            dl->AddLine(c, d, gc, 1.0f);
    }
    // origin triad glyph (world axes at the world origin) — the same X/Y/Z roles the orientation
    // triad and the manipulators use, so every axis cue in the app is one colour.
    ImVec2 o, axp;
    if (worldToScreen(cam, imgMin, vw, vh, 0, 0, 0, o)) {
        const float axLen = 12.0f;
        const Vec3 axes[3] = {{axLen, 0, 0}, {0, axLen, 0}, {0, 0, axLen}};
        const ImU32 axc[3] = {col(ui::palette.axisX), col(ui::palette.axisY),
                              col(ui::palette.axisZ)};
        for (int i = 0; i < 3; ++i)
            if (worldToScreen(cam, imgMin, vw, vh, axes[i].x, axes[i].y, axes[i].z, axp))
                dl->AddLine(o, axp, axc[i], 1.6f);
        dl->AddCircleFilled(o, 3.0f * ui::uiScale, col(ui::palette.sketchInk));
    }
}

// ---------------------------------------------------------------------------
// picking / snapping
// ---------------------------------------------------------------------------

const Sk3Entity* Sketch3DMode::entById(const std::string& id) const {
    for (const auto& e : ents_) if (e.id == id) return &e;
    return nullptr;
}

// handle name for point k of an entity ("e3.p1" style, per the contract grammar)
static std::string handleName(const Sk3Entity& e, int k) {
    if (!e.id.empty() && e.id[0] == 'x') return e.id;
    if (e.type == "point") return e.id + ".p";
    if (e.type == "line") return e.id + (k == 0 ? ".p1" : ".p2");
    if (e.type == "arc") return e.id + (k == 0 ? ".p1" : (k == 1 ? ".pm" : ".p2"));
    return e.id + ".p" + std::to_string(k);   // spline
}

Vec3 Sketch3DMode::handleWorld(const std::string& handle) const {
    if (handle == "origin") return {0, 0, 0};
    auto dot = handle.find('.');
    const Sk3Entity* e = entById(handle.substr(0, dot));
    if (!e) return {0, 0, 0};
    if (dot == std::string::npos) return e->pts.empty() ? Vec3{0, 0, 0} : e->pts[0];
    std::string suf = handle.substr(dot + 1);
    int k = 0;
    if (suf == "p" || suf == "p1" || suf == "start") k = 0;
    else if (suf == "pm" || suf == "mid") k = 1;
    else if (suf == "p2" || suf == "end") k = (int)e->pts.size() - 1;
    else if (suf.size() > 1 && suf[0] == 'p') k = std::atoi(suf.c_str() + 1);
    if (k < 0 || k >= (int)e->pts.size()) k = 0;
    return e->pts[k];
}

bool Sketch3DMode::hitTestHandle(AppState& st, const ImVec2& s, const ImVec2& imgMin, int vw,
                                 int vh, std::string& handleOut, Vec3& worldOut) const {
    if (!st.renderer) return false;
    const auto& cam = st.renderer->camera();
    float best = kSnapPx * ui::uiScale;
    bool found = false;
    ImVec2 sp;
    for (const auto& e : ents_) {
        for (int k = 0; k < (int)e.pts.size(); ++k) {
            if (!worldToScreen(cam, imgMin, vw, vh, e.pts[k].x, e.pts[k].y, e.pts[k].z, sp))
                continue;
            float d = std::hypot(s.x - sp.x, s.y - sp.y);
            if (d < best) { best = d; handleOut = handleName(e, k); worldOut = e.pts[k]; found = true; }
        }
    }
    if (worldToScreen(cam, imgMin, vw, vh, 0, 0, 0, sp)) {
        float d = std::hypot(s.x - sp.x, s.y - sp.y);
        if (d < best) { handleOut = "origin"; worldOut = {0, 0, 0}; found = true; }
    }
    return found;
}

void Sketch3DMode::samplePolyline(const Sk3Entity& e, std::vector<Vec3>& out) {
    out.clear();
    if (e.type == "point") { if (!e.pts.empty()) out.push_back(e.pts[0]); return; }
    if (e.type == "line") { out = e.pts; return; }
    if (e.type == "arc" && e.pts.size() == 3) {
        Vec3 c;
        if (!circumcenter3(e.pts[0], e.pts[1], e.pts[2], c)) { out = e.pts; return; }
        Vec3 r0 = e.pts[0] - c, r2 = e.pts[2] - c;
        float rad = length(r0);
        Vec3 n = normalize(cross(e.pts[1] - e.pts[0], e.pts[2] - e.pts[0]));
        Vec3 u = normalize(r0), v = normalize(cross(n, u));
        float a2 = std::atan2(dot(r2, v), dot(r2, u));
        // sweep from 0 to a2 keeping pm inside: check pm's angle sits between
        Vec3 rm = e.pts[1] - c;
        float am = std::atan2(dot(rm, v), dot(rm, u));
        if (a2 < 0) a2 += 2 * 3.14159265f;
        if (am < 0) am += 2 * 3.14159265f;
        bool fwd = am <= a2;   // pm within the CCW 0..a2 sweep? else go the other way
        float sweep = fwd ? a2 : a2 - 2 * 3.14159265f;
        const int N = 24;
        for (int i = 0; i <= N; ++i) {
            float a = sweep * (float)i / (float)N;
            out.push_back(c + (u * std::cos(a) + v * std::sin(a)) * rad);
        }
        return;
    }
    // Use the solver's OCCT-compatible interpolation, not a different local spline.
    out = e.samples;
}

std::string Sketch3DMode::hitTestEntity(AppState& st, const ImVec2& s, const ImVec2& imgMin,
                                        int vw, int vh) const {
    if (!st.renderer) return {};
    const auto& cam = st.renderer->camera();
    float best = kEntHitPx * ui::uiScale;
    std::string bestId;
    std::vector<Vec3> poly;
    std::vector<ImVec2> sp;
    for (const auto& e : ents_) {
        samplePolyline(e, poly);
        sp.clear();
        for (const auto& p : poly) {
            ImVec2 q;
            if (worldToScreen(cam, imgMin, vw, vh, p.x, p.y, p.z, q)) sp.push_back(q);
        }
        if (e.type == "point" && sp.size() == 1) {
            float d = std::hypot(s.x - sp[0].x, s.y - sp[0].y);
            if (d < best) { best = d; bestId = e.id; }
            continue;
        }
        for (size_t i = 0; i + 1 < sp.size(); ++i) {
            float d = distPtSeg(s, sp[i], sp[i + 1]);
            if (d < best) { best = d; bestId = e.id; }
        }
    }
    return bestId;
}

bool Sketch3DMode::cursorWorld(AppState& st, const ImVec2& mouse, const ImVec2& imgMin, int vw,
                               int vh, const Vec3* prev, Vec3& out, std::string& snapHandle,
                               std::string& axisSnap) const {
    snapHandle.clear();
    axisSnap.clear();
    if (!st.renderer) return false;
    const auto& cam = st.renderer->camera();
    // 1) exact point snap (existing handles + origin) — wins outright.
    Vec3 hw;
    std::string h;
    if (hitTestHandle(st, mouse, imgMin, vw, vh, h, hw)) {
        out = hw; snapHandle = h; return true;
    }
    // 2) ray ∩ working plane.
    Vec3 ro, rd;
    if (!screenRay(cam, imgMin, vw, vh, mouse, ro, rd)) return false;
    Vec3 n = normalize(cross(wpU_, wpV_));
    float denom = dot(n, rd);
    if (std::fabs(denom) < 1e-6f) return false;
    float t = dot(n, wpO_ - ro) / denom;
    if (t < 0) return false;
    out = ro + rd * t;
    // 3) axis inference off the previous point: pull onto the X/Y/Z line through prev when the
    //    projection lands within kAxisSnapPx on screen (SolidWorks-style h/v inference, in 3D).
    if (prev) {
        const Vec3 axes[3] = {{1, 0, 0}, {0, 1, 0}, {0, 0, 1}};
        const char* names[3] = {"x", "y", "z"};
        float best = kAxisSnapPx * ui::uiScale;
        for (int i = 0; i < 3; ++i) {
            Vec3 d = out - *prev;
            Vec3 q = *prev + axes[i] * dot(d, axes[i]);
            ImVec2 sq, sc;
            if (!worldToScreen(cam, imgMin, vw, vh, q.x, q.y, q.z, sq) ||
                !worldToScreen(cam, imgMin, vw, vh, out.x, out.y, out.z, sc))
                continue;
            float dist = std::hypot(sq.x - sc.x, sq.y - sc.y);
            if (dist < best) { best = dist; out = q; axisSnap = names[i]; }
        }
    }
    return true;
}

// ---------------------------------------------------------------------------
// tools
// ---------------------------------------------------------------------------

void Sketch3DMode::resetTool() {
    tool_ = Sk3Tool::None;
    toolPts_.clear(); toolSnaps_.clear(); toolAxes_.clear();
    chainPrevHandle_.clear();
}

void Sketch3DMode::commitLine(AppState& st) {
    if (toolPts_.size() < 2) return;
    nlohmann::json cs = nlohmann::json::array();
    if (!chainPrevHandle_.empty())
        cs.push_back({{"type", "coincident"}, {"refs", {chainPrevHandle_, "$self.p1"}}});
    else if (!toolSnaps_[0].empty())
        cs.push_back({{"type", "coincident"}, {"refs", {toolSnaps_[0], "$self.p1"}}});
    if (!toolSnaps_[1].empty())
        cs.push_back({{"type", "coincident"}, {"refs", {toolSnaps_[1], "$self.p2"}}});
    if (!toolAxes_.empty() && !toolAxes_[0].empty())
        cs.push_back({{"type", "along_axis"}, {"refs", {"$self"}}, {"axis", toolAxes_[0]}});
    nlohmann::json e = {{"type", "line"}, {"p1", vj(toolPts_[0])}, {"p2", vj(toolPts_[1])}};
    std::string id = addEntity(st, e, cs);
    // chain: next segment starts here
    Vec3 last = toolPts_[1];
    std::string lastSnap = toolSnaps_[1];
    toolPts_.clear(); toolSnaps_.clear(); toolAxes_.clear();
    if (!id.empty()) {
        chainPrevHandle_ = id + ".p2";
        toolPts_.push_back(last);
        toolSnaps_.push_back(lastSnap);
    }
}

void Sketch3DMode::commitArc(AppState& st) {
    if (toolPts_.size() < 3) return;
    nlohmann::json cs = nlohmann::json::array();
    const char* selfH[3] = {"$self.p1", "$self.pm", "$self.p2"};
    for (int i = 0; i < 3; ++i)
        if (!toolSnaps_[i].empty())
            cs.push_back({{"type", "coincident"}, {"refs", {toolSnaps_[i], selfH[i]}}});
    nlohmann::json e = {{"type", "arc"}, {"p1", vj(toolPts_[0])}, {"pm", vj(toolPts_[1])},
                        {"p2", vj(toolPts_[2])}};
    addEntity(st, e, cs);
    toolPts_.clear(); toolSnaps_.clear(); toolAxes_.clear();
}

void Sketch3DMode::commitSpline(AppState& st) {
    if (toolPts_.size() < 2) { toolPts_.clear(); toolSnaps_.clear(); return; }
    nlohmann::json pts = nlohmann::json::array();
    for (const auto& p : toolPts_) pts.push_back(vj(p));
    nlohmann::json cs = nlohmann::json::array();
    for (size_t i = 0; i < toolSnaps_.size() && i < toolPts_.size(); ++i)
        if (!toolSnaps_[i].empty())
            cs.push_back({{"type", "coincident"},
                          {"refs", {toolSnaps_[i], std::string("$self.p") + std::to_string(i)}}});
    nlohmann::json e = {{"type", "spline"}, {"points", pts}};
    addEntity(st, e, cs);
    toolPts_.clear(); toolSnaps_.clear(); toolAxes_.clear();
}

void Sketch3DMode::commitPoint(AppState& st) {
    if (toolPts_.empty()) return;
    nlohmann::json cs = nlohmann::json::array();
    if (!toolSnaps_[0].empty())
        cs.push_back({{"type", "coincident"}, {"refs", {toolSnaps_[0], "$self.p"}}});
    nlohmann::json e = {{"type", "point"}, {"p", vj(toolPts_[0])}};
    addEntity(st, e, cs);
    toolPts_.clear(); toolSnaps_.clear(); toolAxes_.clear();
}

// ---------------------------------------------------------------------------
// selection / constraints
// ---------------------------------------------------------------------------

void Sketch3DMode::toggleSel(const std::string& idOrHandle) {
    bool isHandle = idOrHandle.find('.') != std::string::npos || idOrHandle == "origin" ||
                    isExternalHandle(idOrHandle);
    auto& vec = isHandle ? selHandles_ : selEnts_;
    auto it = std::find(vec.begin(), vec.end(), idOrHandle);
    if (it != vec.end()) vec.erase(it);
    else vec.push_back(idOrHandle);
}

void Sketch3DMode::deleteSelection(AppState& st) {
    if (selEnts_.empty()) return;
    nlohmann::json ents = nlohmann::json::array(), cons = nlohmann::json::array();
    auto gone = [&](const std::string& id) {
        return std::find(selEnts_.begin(), selEnts_.end(), id) != selEnts_.end();
    };
    for (const auto& e : entJson_)
        if (!gone(e.value("id", std::string()))) ents.push_back(e);
    for (const auto& c : conJson_) {
        bool refsGone = false;
        if (c.contains("refs"))
            for (const auto& r : c["refs"]) {
                std::string s = r.get<std::string>();
                if (gone(s.substr(0, s.find('.')))) { refsGone = true; break; }
            }
        if (!refsGone) cons.push_back(c);
    }
    if (commitParams(st, &ents, &cons, "Delete")) clearSel();
}

void Sketch3DMode::drawConstraintButtons(AppState& st) {
    const int nP = static_cast<int>(selHandles_.size()), nE = static_cast<int>(selEnts_.size());
    std::vector<std::string> lines, arcs, splines;
    for (const auto& id : selEnts_) {
        const Sk3Entity* e = entById(id);
        if (!e) continue;
        if (e->type == "line") lines.push_back(id);
        else if (e->type == "arc") arcs.push_back(id);
        else if (e->type == "spline") splines.push_back(id);
    }
    const int nL = static_cast<int>(lines.size()), nA = static_cast<int>(arcs.size());
    const int nS = static_cast<int>(splines.size());
    const bool validPoints = std::none_of(selHandles_.begin(), selHandles_.end(),
        [](const std::string& p) { return p == "origin"; });
    nlohmann::json pending;
    const char* pendingLabel = "Relation";
    bool any = false, sameLine = false;
    auto button = [&](const char* label) {
        if (sameLine) ImGui::SameLine();
        sameLine = true;
        any = true;
        return ImGui::SmallButton(label);
    };
    auto relation = [&](const char* label, const char* type, const nlohmann::json& refs) {
        if (button(label)) {
            pending = {{"type", type}, {"refs", refs}};
            pendingLabel = label;
        }
    };
    auto planeRelation = [&](const char* label, const char* type, const nlohmann::json& refs) {
        if (button(label)) {
            planePendingCon_ = {{"type", type}, {"refs", refs}};
            planePopupOpen_ = true;
        }
    };
    auto dimension = [&](const char* label, const char* type, const nlohmann::json& refs, double value) {
        if (button(label)) {
            dimPendingCon_ = {{"type", type}, {"refs", refs}};
            dimValue_ = value;
            dimPopupOpen_ = dimFocus_ = true;
        }
    };
    if (validPoints && nP == 2 && nE == 0) {
        relation("Coincident", "coincident", selHandles_);
        dimension("Distance", "distance", selHandles_,
                  length(handleWorld(selHandles_[1]) - handleWorld(selHandles_[0])));
        planeRelation("Symmetric", "symmetric", selHandles_);
    }
    if (validPoints && nP == 1 && nE == 1 && nL + nA + nS == 1) {
        relation("Point On", "point_on", {selHandles_[0], selEnts_[0]});
        if (nL + nA == 1) relation("Midpoint", "midpoint", {selHandles_[0], selEnts_[0]});
        if (nL == 1) {
            const auto* line = entById(lines[0]);
            Vec3 p = handleWorld(selHandles_[0]), d = line->pts[1] - line->pts[0];
            double distance = length(d) > 1e-9f ? length(cross(p - line->pts[0], d)) / length(d) : 0;
            dimension("Distance", "distance", {selHandles_[0], lines[0]}, distance);
        }
    }
    if (nL == 1 && nP == 0 && nE == 1) {
        const auto* e = entById(lines[0]);
        dimension("Length", "length", lines, length(e->pts[1] - e->pts[0]));
        for (const char* axis : {"x", "y", "z"}) {
            std::string label = std::string("Along ") + static_cast<char>(axis[0] - 'a' + 'A');
            if (button(label.c_str())) {
                pending = {{"type", "along_axis"}, {"refs", lines}, {"axis", axis}};
                pendingLabel = "Along Axis";
            }
        }
        sameLine = false;
        planeRelation("Parallel to Plane", "parallel_plane", lines);
        planeRelation("Normal to Plane", "normal_plane", lines);
    }
    if (nL == 2 && nP == 0 && nE == 2) {
        relation("Parallel", "parallel", lines);
        relation("Perpendicular", "perpendicular", lines);
        dimension("Angle", "angle", lines, 90);
        relation("Collinear", "collinear", lines);
        relation("Equal", "equal", lines);
    }
    if (nA == 2 && nP == 0 && nE == 2) {
        relation("Equal", "equal", arcs);
        relation("Concentric", "concentric", arcs);
        relation("Coradial", "coradial", arcs);
    }
    // Tangency belongs to a shared end, never an interior spline point or an
    // arbitrary pair of unconnected curves. Endpoint selections retain the branch.
    if (nP + nE == 2) {
        std::vector<std::string> refs = selEnts_;
        refs.insert(refs.end(), selHandles_.begin(), selHandles_.end());
        const auto* a = entById(refs[0].substr(0, refs[0].find('.')));
        const auto* b = entById(refs[1].substr(0, refs[1].find('.')));
        const bool pair = a && b && a != b &&
            ((a->type == "arc" && b->type == "arc") ||
             (a->type == "line" && (b->type == "arc" || b->type == "spline")) ||
             (b->type == "line" && (a->type == "arc" || a->type == "spline")));
        bool shared = false;
        if (pair && !a->pts.empty() && !b->pts.empty()) {
            for (int ia : {0, static_cast<int>(a->pts.size()) - 1})
                for (int ib : {0, static_cast<int>(b->pts.size()) - 1}) {
                    std::string ha = handleName(*a, ia), hb = handleName(*b, ib);
                    if (refs[0].find('.') != std::string::npos && refs[0] != ha) continue;
                    if (refs[1].find('.') != std::string::npos && refs[1] != hb) continue;
                    if (length(a->pts[ia] - b->pts[ib]) < 1e-4f) shared = true;
                    for (const auto& con : conJson_)
                        if (con.value("type", std::string()) == "coincident" && con.contains("refs") &&
                            con["refs"].is_array() && con["refs"].size() == 2 &&
                            ((con["refs"][0] == ha && con["refs"][1] == hb) ||
                             (con["refs"][1] == ha && con["refs"][0] == hb))) shared = true;
                }
        }
        if (shared) relation("Tangent", "tangent", refs);
    }
    sameLine = false;
    if (validPoints && nP + nE == 1) {
        const std::string ref = nP == 1 ? selHandles_[0] : selEnts_[0];
        const auto* e = entById(ref.substr(0, ref.find('.')));
        if (nP == 1 || (e && (e->type == "point" || e->type == "line" || e->type == "arc")))
            planeRelation("On Plane", "on_plane", nlohmann::json::array({ref}));
        if (!isExternalHandle(ref)) relation("Fix", "fix", nlohmann::json::array({ref}));
    }
    if (nE >= 1) {
        if (button("Delete")) deleteSelection(st);
        if (button("Construction")) {
            auto entities = entJson_;
            for (auto& e : entities)
                if (std::find(selEnts_.begin(), selEnts_.end(), e.value("id", std::string())) != selEnts_.end())
                    e["construction"] = !e.value("construction", false);
            commitParams(st, &entities, nullptr, "Construction");
        }
    }
    if (!pending.is_null()) {
        addConstraint(st, pending, pendingLabel);
        clearSel();
    }
    if (!any && nP + nE > 0) ImGui::TextDisabled("(no relation for this selection)");
}

void Sketch3DMode::drawDimPopup(AppState& st) {
    if (!dimPopupOpen_) return;
    ImGui::OpenPopup("sk3_dim");
    if (ImGui::BeginPopup("sk3_dim")) {
        const std::string t = dimPendingCon_.value("type", std::string());
        ImGui::TextUnformatted(t == "angle" ? "Angle (deg)" : (t + " (mm)").c_str());
        if (dimFocus_) { ImGui::SetKeyboardFocusHere(); dimFocus_ = false; }
        bool commit = ImGui::InputDouble("##v", &dimValue_, 0, 0, "%.3f",
                                         ImGuiInputTextFlags_EnterReturnsTrue);
        ImGui::SameLine();
        commit = ImGui::Button("OK") || commit;
        ImGui::SameLine();
        if (ImGui::Button("Cancel")) { dimPopupOpen_ = false; ImGui::CloseCurrentPopup(); }
        if (commit) {
            nlohmann::json con = dimPendingCon_;
            con["value"] = {{"value", dimValue_}, {"expr", nullptr}};
            addConstraint(st, con, "Dimension");
            clearSel();
            dimPopupOpen_ = false;
            ImGui::CloseCurrentPopup();
        }
        ImGui::EndPopup();
    } else {
        dimPopupOpen_ = false;
    }
}

void Sketch3DMode::drawPlanePopup(AppState& st) {
    if (!planePopupOpen_) return;
    ImGui::OpenPopup("sk3_relation_plane");
    if (ImGui::BeginPopup("sk3_relation_plane")) {
        ImGui::TextUnformatted("Relation plane");
        const char* labels[] = {"Front (XZ)", "Top (XY)", "Right (YZ)", "Active Working Plane"};
        const char* datums[] = {"front", "top", "right"};
        for (int i = 0; i < 4; ++i) {
            if (!ImGui::Selectable(labels[i])) continue;
            nlohmann::json con = planePendingCon_;
            con["plane"] = i < 3 ? nlohmann::json(datums[i]) :
                nlohmann::json{{"origin", vj(wpO_)}, {"normal", vj(normalize(cross(wpU_, wpV_)))}};
            addConstraint(st, con, "Plane Relation");
            clearSel();
            planePopupOpen_ = false;
            ImGui::CloseCurrentPopup();
        }
        if (ImGui::Selectable("Cancel")) {
            planePopupOpen_ = false;
            ImGui::CloseCurrentPopup();
        }
        ImGui::EndPopup();
    } else planePopupOpen_ = false;
}

// ---------------------------------------------------------------------------
// drag
// ---------------------------------------------------------------------------

void Sketch3DMode::queueSketch3dDrag(AppState& st, nlohmann::json extra) {
    dragPendingExtra_ = std::move(extra);
    dragPending_ = true;
    kickSketch3dDragAsync(st);
}

void Sketch3DMode::kickSketch3dDragAsync(AppState& st) {
    if (dragInFlight_ || !dragPending_) return;
    if (!st.docMirror.hasDoc() || !st.workerStarted || fid_.empty()) return;
    dragPending_ = false;
    dragInFlight_ = true;
    const int gen = ++dragGen_;
    nlohmann::json params = dragPendingExtra_;
    params["doc_id"] = st.docMirror.docId();
    params["sketch"] = fid_;
    st.rpc.call("sketch3d_drag", std::move(params),
                [this, &st, gen](const core::RpcResult& r) {
                    dragInFlight_ = false;
                    if (gen != dragGen_) {
                        if (dragPending_ && dragging_) kickSketch3dDragAsync(st);
                        return;
                    }
                    if (r.ok && r.result.is_object() && !r.result.contains("code") &&
                        r.result.value("ok", false)) {
                        applyEntities(r.result);
                        if (r.result.contains("entities")) dragEntities_ = r.result["entities"];
                    } else if (!r.ok) {
                        st.addLog(std::string("[sketch3d] sketch3d_drag transport error: ") + r.err);
                    }
                    if (dragPending_ && dragging_) kickSketch3dDragAsync(st);
                }, /*timeoutMs=*/30000);
}

void Sketch3DMode::updateDrag(AppState& st, const ImVec2& mouse, const ImVec2& imgMin, int vw,
                              int vh) {
    if (!st.renderer) return;
    int frame = ImGui::GetFrameCount();
    if (frame == dragFrame_) return;   // once per frame UI sample rate
    dragFrame_ = frame;
    // Target = cursor ray ∩ the screen-parallel plane through the handle's press position, so
    // the point tracks the cursor at constant depth (standard 3D handle drag).
    const auto& cam = st.renderer->camera();
    Vec3 ro, rd;
    if (!screenRay(cam, imgMin, vw, vh, mouse, ro, rd)) return;
    Vec3 n = normalize(cam.eye - dragOrig_);
    float denom = dot(n, rd);
    if (std::fabs(denom) < 1e-6f) return;
    float t = dot(n, dragOrig_ - ro) / denom;
    if (t < 0) return;
    Vec3 target = ro + rd * t;
    // T-PERF2: latest-wins async — viewport keeps last solved entities while RPCs catch up.
    queueSketch3dDrag(st, {{"handle", dragHandle_}, {"xyz", vj(target)}});
}

void Sketch3DMode::commitDrag(AppState& st) {
    ++dragGen_;
    dragPending_ = false;
    dragInFlight_ = false;
    if (dragEntities_.is_array() && !dragEntities_.empty()) {
        // Contract §4.3: commit = plain edit_feature with the last drag response's entities.
        nlohmann::json entities = nlohmann::json::array();
        for (auto entity : dragEntities_) {
            if (isExternalHandle(entity.value("id", std::string()))) continue;
            if (entity.value("type", std::string()) == "spline") entity.erase("samples");
            entities.push_back(std::move(entity));
        }
        commitParams(st, &entities, nullptr, "Drag");
    }
    dragEntities_ = nlohmann::json();
    dragHandle_.clear();
}

// ---------------------------------------------------------------------------
// overlay (draw + interaction)
// ---------------------------------------------------------------------------

void Sketch3DMode::drawOverlay(AppState& st, ImGuiIO& io, const ImVec2& imgMin,
                               const ImVec2& imgMax, int vw, int vh, bool imgHovered) {
    if (!active_) return;
    if (st.docLockedByAgent(docId_)) {
        std::string part = "part";
        for (const auto& tab : st.docTabs)
            if (tab.docId == docId_) { part = tab.title; break; }
        exitSketch(st, true);
        st.toast(ui::ToastLevel::Info,
                 "Agent started editing " + part + " — your sketch was kept and closed");
        return;
    }
    // Doc switched underneath us (tab change / close): drop out cleanly.
    if (!st.docMirror.hasDoc() || st.docMirror.docId() != docId_) { exitSketch(st, true); return; }
    if (!st.renderer) return;
    consumeModelVertexPick(st);
    const auto& cam = st.renderer->camera();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->PushClipRect(imgMin, imgMax, true);

    drawPlaneGrid(dl, st, imgMin, vw, vh);

    // ---- hover ----
    hovHandle_.clear(); hovEnt_.clear();
    if (imgHovered && !dragging_ && !modelVertexPicking_) {
        Vec3 hw;
        if (!hitTestHandle(st, io.MousePos, imgMin, vw, vh, hovHandle_, hw))
            hovEnt_ = hitTestEntity(st, io.MousePos, imgMin, vw, vh);
    }
    // GUI-picking overhaul: "hot" whenever a sketch entity/handle claims the cursor, a drawing
    // tool owns it, or a drag is live — the viewport's fallback model-pick must not fire then.
    // See Sketch3DMode::inSketchEntityHot().
    entityHoverHot_ = !modelVertexPicking_ &&
        (tool_ != Sk3Tool::None || dragging_ || !hovHandle_.empty() || !hovEnt_.empty());

    // ---- entities ----
    const auto& pal = ui::palette;
    std::vector<Vec3> poly;
    std::vector<ImVec2> sp;
    for (const auto& e : ents_) {
        if (isExternalHandle(e.id)) {
            if (e.pts.empty()) continue;
            ImVec2 q;
            if (!worldToScreen(cam, imgMin, vw, vh, e.pts[0].x, e.pts[0].y, e.pts[0].z, q)) continue;
            const bool selected = std::find(selHandles_.begin(), selHandles_.end(), e.id) != selHandles_.end();
            bool stale = false;
            for (const auto& ref : externalRefs_)
                if (ref.value("id", std::string()) == e.id) stale = ref.value("stale", false);
            const ImU32 ink = col(stale ? pal.statusWarn : selected || hovHandle_ == e.id ? pal.accent : pal.sketchInfer);
            const float r = ui::metrics::U(selected || hovHandle_ == e.id ? 6.0f : 5.0f);
            dl->AddQuad(ImVec2(q.x, q.y - r), ImVec2(q.x + r, q.y),
                        ImVec2(q.x, q.y + r), ImVec2(q.x - r, q.y), ink, ui::metrics::U(1.5f));
            dl->AddText(ImVec2(q.x + r + ui::metrics::U(3.0f), q.y - r), ink, e.id.c_str());
            continue;
        }
        bool sel = std::find(selEnts_.begin(), selEnts_.end(), e.id) != selEnts_.end();
        bool hov = hovEnt_ == e.id;
        const float* role = e.status == "over" ? pal.statusErr
                          : e.status == "full" ? pal.sketchInk : pal.sketchUnder;
        ImU32 c = sel ? col(pal.accent) : hov ? col3(pal.accent, 0.85f) : col(role);
        float th = (sel || hov ? 3.0f : 2.0f) * ui::uiScale;
        samplePolyline(e, poly);
        sp.clear();
        for (const auto& p : poly) {
            ImVec2 q;
            if (worldToScreen(cam, imgMin, vw, vh, p.x, p.y, p.z, q)) sp.push_back(q);
        }
        if (e.type == "point") {
            if (!sp.empty()) dl->AddCircleFilled(sp[0], 3.5f * ui::uiScale, c);
        } else if (e.construction) {
            // dashed construction stroke
            for (size_t i = 0; i + 1 < sp.size(); ++i) {
                ImVec2 a = sp[i], b = sp[i + 1];
                float len = std::hypot(b.x - a.x, b.y - a.y);
                int nd = (int)(len / (8.0f * ui::uiScale)) + 1;
                for (int k = 0; k < nd; k += 2) {
                    float t0 = (float)k / nd, t1 = std::min(1.0f, (float)(k + 1) / nd);
                    dl->AddLine(ImVec2(a.x + (b.x - a.x) * t0, a.y + (b.y - a.y) * t0),
                                ImVec2(a.x + (b.x - a.x) * t1, a.y + (b.y - a.y) * t1), c,
                                th * 0.75f);
                }
            }
        } else {
            for (size_t i = 0; i + 1 < sp.size(); ++i) dl->AddLine(sp[i], sp[i + 1], c, th);
        }
        // point handles (defining points)
        for (int k = 0; k < (int)e.pts.size(); ++k) {
            ImVec2 q;
            if (!worldToScreen(cam, imgMin, vw, vh, e.pts[k].x, e.pts[k].y, e.pts[k].z, q))
                continue;
            std::string h = handleName(e, k);
            bool hh = hovHandle_ == h || (dragging_ && dragHandle_ == h);
            bool hs = std::find(selHandles_.begin(), selHandles_.end(), h) != selHandles_.end();
            float r = kHandlePx * ui::uiScale * (hh ? 1.4f : 1.0f);
            ImU32 hc = hs || hh ? col(pal.accent) : col(pal.sketchInk);
            dl->AddRectFilled(ImVec2(q.x - r, q.y - r), ImVec2(q.x + r, q.y + r), hc);
        }
    }

    // ---- in-progress placed points (spline needs this every frame — it collects 2+ points
    // before committing, so without a live preview of the chain-so-far nothing is visible until
    // Enter/double-click; line/arc commit at 2/3 points so the rubber band below already covers
    // their single pending segment, but draw the chain uniformly for consistency). ----
    if (tool_ != Sk3Tool::None && toolPts_.size() >= 2) {
        ImU32 pc = col3(pal.accent, 0.55f);
        ImVec2 pa, pb;
        for (size_t i = 0; i + 1 < toolPts_.size(); ++i) {
            if (worldToScreen(cam, imgMin, vw, vh, toolPts_[i].x, toolPts_[i].y, toolPts_[i].z, pa) &&
                worldToScreen(cam, imgMin, vw, vh, toolPts_[i + 1].x, toolPts_[i + 1].y,
                              toolPts_[i + 1].z, pb))
                dl->AddLine(pa, pb, pc, 2.0f * ui::uiScale);
        }
    }
    if (tool_ != Sk3Tool::None) {
        for (const auto& p : toolPts_) {
            ImVec2 pp;
            if (worldToScreen(cam, imgMin, vw, vh, p.x, p.y, p.z, pp))
                dl->AddCircleFilled(pp, 3.0f * ui::uiScale, col3(pal.accent, 0.8f));
        }
    }

    // ---- tool rubber band ----
    if (tool_ != Sk3Tool::None && imgHovered) {
        const Vec3* prev = toolPts_.empty() ? nullptr : &toolPts_.back();
        Vec3 cur;
        std::string snapH, axisS;
        bool haveCur = cursorWorld(st, io.MousePos, imgMin, vw, vh, prev, cur, snapH, axisS);
        if (haveCur) {
            ImVec2 sc;
            if (worldToScreen(cam, imgMin, vw, vh, cur.x, cur.y, cur.z, sc)) {
                dl->AddCircle(sc, 4.0f * ui::uiScale, col(pal.accent), 0, 1.5f);
                if (!snapH.empty())
                    dl->AddCircle(sc, 8.0f * ui::uiScale, col(pal.sketchInfer), 0, 1.5f);
            }
            if (prev) {
                ImVec2 a, b;
                if (worldToScreen(cam, imgMin, vw, vh, prev->x, prev->y, prev->z, a) &&
                    worldToScreen(cam, imgMin, vw, vh, cur.x, cur.y, cur.z, b)) {
                    ImU32 rb = axisS.empty() ? col3(pal.accent, 0.7f) : col(pal.sketchInfer);
                    dl->AddLine(a, b, rb, 1.5f * ui::uiScale);
                    if (!axisS.empty()) {
                        char lab[16];
                        std::snprintf(lab, sizeof lab, "along %s", axisS.c_str());
                        dl->AddText(ImVec2(b.x + 10, b.y - 14), col(pal.sketchInfer), lab);
                    }
                }
            }
            // click = place a point
            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                toolPts_.push_back(cur);
                toolSnaps_.push_back(snapH);
                if (tool_ == Sk3Tool::Line) {
                    toolAxes_.assign(1, axisS);
                    if (toolPts_.size() == 2) commitLine(st);
                } else if (tool_ == Sk3Tool::Arc3) {
                    if (toolPts_.size() == 3) commitArc(st);
                } else if (tool_ == Sk3Tool::Point) {
                    commitPoint(st);
                }
                // spline collects until Enter / double-click
            }
            if (tool_ == Sk3Tool::Spline &&
                (ImGui::IsKeyPressed(ImGuiKey_Enter) ||
                 ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)))
                commitSpline(st);
        }
        // Esc: end the chain / drop the tool.
        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
            if (tool_ == Sk3Tool::Spline && toolPts_.size() >= 2) commitSpline(st);
            if (!toolPts_.empty() || !chainPrevHandle_.empty()) {
                toolPts_.clear(); toolSnaps_.clear(); toolAxes_.clear();
                chainPrevHandle_.clear();
            } else {
                resetTool();
            }
        }
        // Right-click ends a line chain without dropping the tool.
        if (ImGui::IsMouseClicked(ImGuiMouseButton_Right)) {
            toolPts_.clear(); toolSnaps_.clear(); toolAxes_.clear();
            chainPrevHandle_.clear();
        }
        st.requestCursor(ui::CursorKind::Crosshair);
    }

    // ---- selection / drag (tool None) ----
    if (tool_ == Sk3Tool::None && !modelVertexPicking_) {
        const float kDragArmPx = 5.0f * ui::uiScale;   // matches the 2D sketcher's threshold
        if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !dragging_ &&
            !dragArmed_) {
            if (!hovHandle_.empty() && isExternalHandle(hovHandle_)) {
                toggleSel(hovHandle_);
            } else if (!hovHandle_.empty()) {
                dragArmed_ = true;
                dragHandle_ = hovHandle_;
                dragOrig_ = handleWorld(hovHandle_);
                dragDownPos_ = io.MousePos;
            } else if (!hovEnt_.empty()) {
                if (!io.KeyCtrl) clearSel();
                toggleSel(hovEnt_);
            } else if (!io.KeyCtrl) {
                clearSel();
            }
        }
        if (dragArmed_ && !dragging_ && ImGui::IsMouseDown(ImGuiMouseButton_Left) &&
            dragHandle_ != "origin") {
            if (std::hypot(io.MousePos.x - dragDownPos_.x, io.MousePos.y - dragDownPos_.y) >
                kDragArmPx) {
                dragging_ = true;
                dragArmed_ = false;
                dragFrame_ = -1;
                dragEntities_ = nlohmann::json();
            }
        }
        if (dragging_ && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
            updateDrag(st, io.MousePos, imgMin, vw, vh);
            st.requestCursor(ui::CursorKind::Hand);
            // Edge auto-pan (Hunter UX): camera dead-still except a quadratic ramp inside the
            // edge band — mirrors the 2D sketcher's tuning.
            const float kBandPx = 48.0f * ui::uiScale;
            const float kMaxPx = 540.0f * ui::uiScale;
            auto ramp = [](float t) { t = t < 0 ? 0 : (t > 1 ? 1 : t); return t * t; };
            float ox = ramp((imgMin.x + kBandPx - io.MousePos.x) / kBandPx) * -1.f +
                       ramp((io.MousePos.x - (imgMax.x - kBandPx)) / kBandPx);
            float oy = ramp((imgMin.y + kBandPx - io.MousePos.y) / kBandPx) * -1.f +
                       ramp((io.MousePos.y - (imgMax.y - kBandPx)) / kBandPx);
            if (ox != 0.0f || oy != 0.0f) {
                float dt = io.DeltaTime < 0.1f ? io.DeltaTime : 0.1f;
                float gx = -ox * kMaxPx * dt, gy = -oy * kMaxPx * dt;
                const float deg = 3.14159265f / 180.0f;
                // Route through the renderer's camera seam so ortho/persp both pan correctly.
                float worldPerPx = st.renderer->worldPerPixel(vh);
                float yr = st.yaw * deg, pr = st.pitch * deg;
                Vec3 vdir(std::cos(pr) * std::sin(yr), -std::cos(pr) * std::cos(yr), std::sin(pr));
                Vec3 wUp = normalize(Vec3(-std::sin(pr) * std::sin(yr),
                                          std::sin(pr) * std::cos(yr), std::cos(pr)));
                Vec3 V = normalize(vdir);
                Vec3 R = normalize(cross(wUp, V));
                Vec3 U = normalize(cross(V, R));
                st.pan[0] += (-R.x * gx + U.x * gy) * worldPerPx;
                st.pan[1] += (-R.y * gx + U.y * gy) * worldPerPx;
                st.pan[2] += (-R.z * gx + U.z * gy) * worldPerPx;
            }
        }
        if (ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
            if (dragging_) {
                dragging_ = false;
                commitDrag(st);
            } else if (dragArmed_) {
                // click-without-move = select the point handle
                if (!io.KeyCtrl && hovEnt_.empty()) { /* keep multi via ctrl only */ }
                toggleSel(dragHandle_);
                dragArmed_ = false;
                dragHandle_.clear();
            }
        }
        if (!ImGui::IsMouseDown(ImGuiMouseButton_Left)) dragArmed_ = false;
        if ((ImGui::IsKeyPressed(ImGuiKey_Delete) || ImGui::IsKeyPressed(ImGuiKey_Backspace)) &&
            !io.WantTextInput)
            deleteSelection(st);
        if (ImGui::IsKeyPressed(ImGuiKey_Escape) && !dimPopupOpen_ && !planePopupOpen_) clearSel();
    }
    if (modelVertexPicking_ && ImGui::IsKeyPressed(ImGuiKey_Escape))
        setModelVertexPicking(st, false);

    // Tab cycles the working plane preset (documented in the contract appendix).
    if (!io.WantTextInput && ImGui::IsKeyPressed(ImGuiKey_Tab))
        setPlanePreset(st, (wpPreset_ + 1) % 4);
    // Working plane follows the drawing chain so a polyline can turn in space: anchor at the
    // last placed point (or the origin when idle).
    wpO_ = toolPts_.empty() ? Vec3{0, 0, 0} : toolPts_.back();
    if (wpPreset_ == 3) setPlanePreset(st, 3);   // view-aligned tracks the live camera

    dl->PopClipRect();

    // ---- status/constraint strip (glass strip, top-center of the viewport) ----
    // The draw-tool set, working-plane presets, construction toggle and Finish/Cancel now live in
    // the ribbon's Sketch tab (see ribbon.cpp/ribbon_def.cpp — swapped in via
    // setRibbonSketch3DActive while a sketch3d feature is being edited). What's left here is
    // genuinely viewport-local: the DOF/status readout, and the selection-contextual constraint
    // buttons + dimension popup, which need to sit right next to the current selection/cursor.
    {
        ImVec2 clearMin = imgMin, clearMax = imgMax;
        ui::viewportClearRect(clearMin, clearMax);
        const float margin = ui::metrics::U(12.0f);
        clearMin.y = std::max(clearMin.y, ui::docTabBandBottom());
        ImGui::SetNextWindowPos(
            ImVec2((clearMin.x + clearMax.x) * 0.5f, clearMin.y + margin),
            ImGuiCond_Always, ImVec2(0.5f, 0.0f));
        ImGui::SetNextWindowSizeConstraints(ImVec2(0, 0),
            ImVec2(std::max(1.0f, clearMax.x - clearMin.x - margin * 2),
                   std::max(1.0f, clearMax.y - clearMin.y - margin * 2)));
        ImGui::SetNextWindowBgAlpha(0.85f);
        if (ImGui::Begin("##sk3status", nullptr,
                         ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoDecoration |
                             ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoFocusOnAppearing |
                             ImGuiWindowFlags_NoNav)) {
            ImGui::TextDisabled("3D Sketch");
            ImGui::SameLine();
            ImGui::TextDisabled("|");
            ImGui::SameLine();
            std::string sl = statusLine();
            if (nWires_ > 0) sl += "   wires: " + std::to_string(nWires_);
            const float* role = status_ == "over" || !converged_ ? ui::palette.statusErr
                              : status_ == "full" ? ui::palette.statusOk : ui::palette.sketchUnder;
            ImGui::TextColored(ImVec4(role[0], role[1], role[2], 1), "%s", sl.c_str());
            if (!warning_.empty())
                ImGui::TextColored(ImVec4(ui::palette.statusWarn[0], ui::palette.statusWarn[1],
                                         ui::palette.statusWarn[2], ui::palette.statusWarn[3]),
                                   "%s", warning_.c_str());
            if (ImGui::SmallButton(modelVertexPicking_ ? "Cancel Vertex Pick" : "Reference Model Vertex"))
                setModelVertexPicking(st, !modelVertexPicking_);
            if (modelVertexPicking_)
                ImGui::TextDisabled("Pick a model vertex (Esc to cancel). Existing sketch selection is retained.");

            // contextual constraints for the current selection (needs the live selection, so it
            // stays viewport-local rather than a static ribbon row set)
            if (!selEnts_.empty() || !selHandles_.empty()) drawConstraintButtons(st);
            drawDimPopup(st);
            drawPlanePopup(st);
        }
        ImGui::End();
    }
}

}  // namespace ui
