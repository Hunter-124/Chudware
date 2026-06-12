// Sketch mode UI (C18) implementation. See sketch_mode.h for the design overview.
#include "sketch_mode.h"

#include "camera_anim.h"   // C34: restore the pre-sketch camera on exit

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <functional>

#include "../core/app_state.h"
#include "../renderer.h"
#include "../worker_client.h"
#include "../ui_theme.h"
#include "property_manager.h"   // pmParseDim (Smart Dimension value parsing)

namespace ui {

namespace {

constexpr float kSnapPx = 6.0f;        // endpoint coincident snap radius (screen px)
constexpr float kHandlePx = 8.0f;      // drag hit-test radius (screen px)
constexpr float kHVDeg = 5.0f;         // horizontal/vertical auto-relation tolerance (deg)
constexpr float kGridStep = 10.0f;     // sketch grid spacing (mm)
// C49 inference tolerances:
constexpr float kInferHVDeg = 4.0f;    // Line rubber-band h/v direction-snap tolerance (deg)
constexpr float kTangentDeg = 6.0f;    // Line-on-rim tangent direction-snap tolerance (deg)

// C49: toolSnaps_ entries are either a plain coincident point handle ("e3.p2" / "e4.center" /
// "e5.p"), or a prefixed inference snap: "mid:<lineId>" (line midpoint), "rim:<circleId>"
// (point on a circle/arc rim), "tan:<circleId>" (on-rim + tangent direction matched).
// coincRef() returns the handle only for the plain coincident form.
inline bool snapHasPrefix(const std::string& s, const char* pfx) { return s.rfind(pfx, 0) == 0; }
inline std::string coincRef(const std::string& s) {
    if (snapHasPrefix(s, "mid:") || snapHasPrefix(s, "rim:") || snapHasPrefix(s, "tan:"))
        return std::string();
    return s;
}

// Project a world point through the camera MVP to screen pixels inside [imgMin, imgMin+(vw,vh)].
// The viewport image is drawn flipped vertically (UV 0,1 -> 1,0), so screen-y uses (1 - ndcY).
// Returns false if the point is behind the camera.
bool worldToScreen(const Renderer::CameraXform& cam, const ImVec2& imgMin, int vw, int vh,
                   float wx, float wy, float wz, ImVec2& out) {
    const float* m = cam.mvp.m;  // column-major: clip[r] = sum_c m[c*4+r]*p[c]
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

ImU32 col(const float role[4], float a = 1.0f) {
    float c[4] = {role[0], role[1], role[2], role[3] * a};
    return ui::Palette::toU32(c);
}

// Circumcircle of three (u,v) points. Returns false when (near-)collinear.
bool circumcircle(const ImVec2& a, const ImVec2& b, const ImVec2& c,
                  float& cu, float& cv, float& r) {
    float d = 2.0f * (a.x * (b.y - c.y) + b.x * (c.y - a.y) + c.x * (a.y - b.y));
    if (std::fabs(d) < 1e-6f) return false;
    float a2 = a.x * a.x + a.y * a.y, b2 = b.x * b.x + b.y * b.y, c2 = c.x * c.x + c.y * c.y;
    cu = (a2 * (b.y - c.y) + b2 * (c.y - a.y) + c2 * (a.y - b.y)) / d;
    cv = (a2 * (c.x - b.x) + b2 * (a.x - c.x) + c2 * (b.x - a.x)) / d;
    r = std::hypot(a.x - cu, a.y - cv);
    return true;
}

}  // namespace

// ---------------------------------------------------------------------------
// projection basis (camera is locked fronto-parallel, so the plane maps affinely to screen)
// ---------------------------------------------------------------------------

void SketchMode::buildBasis(const Renderer& r, const ImVec2& imgMin, int vw, int vh) {
    basisOk_ = false;
    const auto& cam = r.camera();
    // World positions of the sketch-plane origin and the +1u / +1v points.
    // world = frame * [u,v,0,1]; columns of the 4x4 (row-major frame_): col j is frame_[k*4+j].
    auto uvWorld = [&](float u, float v, float& wx, float& wy, float& wz) {
        wx = frame_[0] * u + frame_[1] * v + frame_[3];
        wy = frame_[4] * u + frame_[5] * v + frame_[7];
        wz = frame_[8] * u + frame_[9] * v + frame_[11];
    };
    float ox, oy, oz, ux, uy, uz, vx, vy, vz;
    uvWorld(0, 0, ox, oy, oz);
    uvWorld(1, 0, ux, uy, uz);
    uvWorld(0, 1, vx, vy, vz);
    ImVec2 sO, sU, sV;
    if (!worldToScreen(cam, imgMin, vw, vh, ox, oy, oz, sO)) return;
    if (!worldToScreen(cam, imgMin, vw, vh, ux, uy, uz, sU)) return;
    if (!worldToScreen(cam, imgMin, vw, vh, vx, vy, vz, sV)) return;
    originS_ = sO;
    uAxisS_ = ImVec2(sU.x - sO.x, sU.y - sO.y);
    vAxisS_ = ImVec2(sV.x - sO.x, sV.y - sO.y);
    det_ = uAxisS_.x * vAxisS_.y - uAxisS_.y * vAxisS_.x;
    basisOk_ = std::fabs(det_) > 1e-6f;
}

ImVec2 SketchMode::uvToScreen(float u, float v) const {
    return ImVec2(originS_.x + uAxisS_.x * u + vAxisS_.x * v,
                  originS_.y + uAxisS_.y * u + vAxisS_.y * v);
}

void SketchMode::screenToUV(const ImVec2& s, float& u, float& v) const {
    // Solve [uAxis vAxis] * [u v]^T = (s - origin)  (2x2 inverse).
    float dx = s.x - originS_.x, dy = s.y - originS_.y;
    if (std::fabs(det_) < 1e-6f) { u = v = 0; return; }
    u = (vAxisS_.y * dx - vAxisS_.x * dy) / det_;
    v = (-uAxisS_.y * dx + uAxisS_.x * dy) / det_;
}

// ---------------------------------------------------------------------------
// worker round-trips
// ---------------------------------------------------------------------------

bool SketchMode::callSketch(AppState& st, const char* method, const nlohmann::json& extra,
                            nlohmann::json& result) {
    if (!st.docMirror.hasDoc() || !st.workerStarted) return false;
    nlohmann::json params = extra;
    params["doc_id"] = st.docMirror.docId();
    params["sketch"] = fid_;
    std::string err;
    if (!st.worker.call(method, params, result, err)) {
        st.addLog(std::string("[sketch] ") + method + " transport error: " + err);
        return false;
    }
    if (result.contains("code")) {  // structured error envelope
        st.addLog(std::string("[sketch] ") + method + " error: " +
                  result.value("message", std::string("?")));
        return false;
    }
    return true;
}

// Parse a sketch response's "entities" (+ optional "statuses") array into SketchEntity records.
// Shared by applyEnterResult (live sketch) and the C40 ghost-sketch cache.
static void parseEntities(const nlohmann::json& res, std::vector<SketchEntity>& out) {
    out.clear();
    const nlohmann::json* stats = res.contains("statuses") ? &res["statuses"] : nullptr;
    if (!res.contains("entities") || !res["entities"].is_array()) return;
    for (const auto& e : res["entities"]) {
        SketchEntity se;
        se.id = e.value("id", std::string());
        se.type = e.value("type", std::string());
        se.construction = e.value("construction", false);
        if (stats && stats->contains(se.id)) se.status = (*stats)[se.id].get<std::string>();
        else se.status = "under";
        auto v2 = [&](const char* k, float o[2]) {
            if (e.contains(k) && e[k].is_array() && e[k].size() == 2) {
                o[0] = e[k][0].get<float>(); o[1] = e[k][1].get<float>();
            }
        };
        v2("p1", se.p1); v2("p2", se.p2); v2("center", se.center);
        v2("p", se.p1);   // C42: point/text store their anchor as "p" — reuse p1
        se.radius = e.value("radius", 0.0f);
        se.start = e.value("start", 0.0f);
        se.end = e.value("end", 0.0f);
        se.ccw = e.value("ccw", true);
        se.width = e.value("width", 0.0f);
        se.rx = e.value("rx", 0.0f);          // C42: ellipse
        se.ry = e.value("ry", 0.0f);
        se.rot = e.value("rot", 0.0f);
        se.pts.clear();                       // C42: spline through-points
        if (e.contains("points") && e["points"].is_array())
            for (const auto& q : e["points"])
                if (q.is_array() && q.size() == 2)
                    se.pts.push_back(ImVec2(q[0].get<float>(), q[1].get<float>()));
        if (se.type == "arc_slot") {   // arc_slot stores spine angles as a0/a1 (CCW)
            se.start = e.value("a0", 0.0f);
            se.end = e.value("a1", 0.0f);
            se.ccw = true;
        }
        out.push_back(se);
    }
}

void SketchMode::applyEnterResult(const nlohmann::json& res) {
    dof_ = res.value("dof", 0);
    status_ = res.value("status", std::string("under"));
    if (res.contains("plane_frame") && res["plane_frame"].is_array() &&
        res["plane_frame"].size() == 16) {
        for (int i = 0; i < 16; ++i) frame_[i] = res["plane_frame"][i].get<float>();
    }
    parseEntities(res, ents_);
    applyConstraints(res);   // C37-D: store the constraints array for dim annotations
    // C41 (T2): drop selected ids that no longer exist (entity deleted / undone away).
    selIds_.erase(std::remove_if(selIds_.begin(), selIds_.end(),
                                 [&](const std::string& id) { return entById(id) == nullptr; }),
                  selIds_.end());
    // C38 (S1): ents_ was just refilled — any in-flight Smart Dimension picks now point at stale
    // entities (their by-id resolution may shift or vanish). Drop the pending pick buffer so we never
    // feed dangling picks into dimBuildPending/highlight on the next frame. The Modify box (if open)
    // already captured its placement + refs, so it survives this refresh untouched.
    if (!dimPopupOpen_) {
        dimPicks_.clear();
        dimHasPending_ = false;
        dimPending_ = nullptr;
    }
}

// C37-D: pull the constraints array (worker_rpc.md §"constraints") off any sketch response.
void SketchMode::applyConstraints(const nlohmann::json& res) {
    cons_.clear();
    if (!res.contains("constraints") || !res["constraints"].is_array()) return;
    for (const auto& c : res["constraints"]) {
        SketchConstraint sc;
        sc.id = c.value("id", std::string());
        sc.type = c.value("type", std::string());
        if (c.contains("refs") && c["refs"].is_array())
            for (const auto& r : c["refs"]) if (r.is_string()) sc.refs.push_back(r.get<std::string>());
        if (c.contains("value") && c["value"].is_number()) { sc.hasValue = true; sc.value = c["value"].get<double>(); }
        if (c.contains("expr") && c["expr"].is_string()) { sc.hasExpr = true; sc.expr = c["expr"].get<std::string>(); }
        sc.driven = c.value("driven", false);
        if (c.contains("placement") && c["placement"].is_array() && c["placement"].size() == 2) {
            sc.hasPlacement = true;
            sc.placement[0] = c["placement"][0].get<float>();
            sc.placement[1] = c["placement"][1].get<float>();
        }
        if (c.contains("measured") && c["measured"].is_number()) { sc.hasMeasured = true; sc.measured = c["measured"].get<double>(); }
        if (c.contains("axis") && c["axis"].is_string()) sc.axis = c["axis"].get<std::string>();
        cons_.push_back(sc);
    }
}

const SketchEntity* SketchMode::entById(const std::string& id) const {
    for (const auto& e : ents_) if (e.id == id) return &e;
    return nullptr;
}

// C41 (T2): is this entity id in the in-sketch selection?
bool SketchMode::isSelected(const std::string& id) const {
    return std::find(selIds_.begin(), selIds_.end(), id) != selIds_.end();
}

// C38 (S1): resolve a stored entity id to its CURRENT index in ents_ (-1 if gone). DimPicks store
// the id (stable) rather than the index (refilled by applyEnterResult/resolve mid-pick).
int SketchMode::entIndexById(const std::string& id) const {
    for (size_t i = 0; i < ents_.size(); ++i) if (ents_[i].id == id) return (int)i;
    return -1;
}

void SketchMode::resolve(AppState& st) {
    nlohmann::json res;
    if (callSketch(st, "sketch_solve", nlohmann::json::object(), res)) applyEnterResult(res);
}

// sketch_drag_commit / sketch_exit bump the worker's document revision out from under the mirror.
// C24: DocMirror::resync() pulls the fresh tree + revision (doc_tree) and fires the listener so
// the FeatureManager repaints — replaces the old throwaway resolvePick(1,"body",1) trick.
static void resyncMirror(AppState& st) {
    st.docMirror.resync(st.worker);
}

void SketchMode::commitDrag(AppState& st) {
    nlohmann::json res;
    if (!callSketch(st, "sketch_drag_commit", nlohmann::json::object(), res)) return;
    if (res.contains("tess") && res["tess"].is_object()) {
        std::string aicm = res["tess"].value("aicm_path", std::string());
        if (!aicm.empty()) { std::string le; st.loadDocMesh(aicm, le); }
    }
    resyncMirror(st);
    resolve(st);   // refresh statuses against the committed doc
}

void SketchMode::addEntity(AppState& st, const nlohmann::json& entity,
                           const nlohmann::json& constraints) {
    // sketch_add_entity goes through the normal ops path (revision bump + undo) via DocMirror.
    nlohmann::json op = {{"op", "sketch_add_entity"}, {"sketch", fid_}, {"entity", entity}};
    if (!constraints.is_null() && constraints.is_array() && !constraints.empty())
        op["constraints"] = constraints;
    nlohmann::json ops = nlohmann::json::array({op});
    auto r = st.docMirror.applyOps(st.worker, ops);
    if (!r.ok) { st.addLog("[sketch] add_entity failed: " + r.error_message); return; }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);
}

void SketchMode::addConstraint(AppState& st, const nlohmann::json& constraint) {
    nlohmann::json ops = nlohmann::json::array({
        {{"op", "sketch_add_constraint"}, {"sketch", fid_}, {"constraint", constraint}}});
    auto r = st.docMirror.applyOps(st.worker, ops);
    if (!r.ok) { st.addLog("[sketch] add_constraint failed: " + r.error_message); return; }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);
}

// Emit 4 lines as a closed loop (corners A,B,C,D). Each edge i (corners[i]->corners[(i+1)%4]) gets
// a chained coincident to the previous edge's p2, plus the per-edge relation in relations[i] (one
// of "horizontal"/"vertical"/"parallel"/"perpendicular"/"equal"/"", referencing $self, or for the
// two-ref relations referencing the opposite edge's id via the {prefix}). Mirrors the corner-rect
// op pattern (successive applyOps so coincidents reference real ids).
void SketchMode::addQuadLoop(AppState& st, const ImVec2 corners[4], const char* relations[4]) {
    std::string ids[4];
    std::string prevId, firstId;
    for (int i = 0; i < 4; ++i) {
        int j = (i + 1) % 4;
        nlohmann::json cs = nlohmann::json::array();
        const char* rel = relations[i];
        // Single-ref auto relations (h/v) reference $self; pair relations (parallel/equal/perp) are
        // applied in a second pass below once all four ids exist.
        if (rel && (std::strcmp(rel, "horizontal") == 0 || std::strcmp(rel, "vertical") == 0))
            cs.push_back({{"type", rel}, {"refs", {"$self"}}});
        if (!prevId.empty())
            cs.push_back({{"type", "coincident"}, {"refs", {prevId + ".p2", "$self.p1"}}});
        nlohmann::json ent = {{"type", "line"},
            {"p1", nlohmann::json::array({corners[i].x, corners[i].y})},
            {"p2", nlohmann::json::array({corners[j].x, corners[j].y})}};
        nlohmann::json op = {{"op", "sketch_add_entity"}, {"sketch", fid_},
                             {"entity", ent}, {"constraints", cs}};
        auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
        if (!r.ok) { st.addLog("[sketch] quad line failed: " + r.error_message); break; }
        std::string nid = (r.id_map.is_object() && r.id_map.contains("$0"))
                        ? r.id_map["$0"].get<std::string>() : std::string();
        ids[i] = nid;
        if (i == 0) firstId = nid;
        prevId = nid;
        if (i == 3 && !firstId.empty() && !nid.empty()) {
            nlohmann::json clop = {{"op", "sketch_add_constraint"}, {"sketch", fid_},
                {"constraint", {{"type", "coincident"}, {"refs", {nid + ".p2", firstId + ".p1"}}}}};
            st.docMirror.applyOps(st.worker, nlohmann::json::array({clop}));
        }
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    }
    // Second pass: pair relations on the two OPPOSITE-edge pairs (0<->2, 1<->3). The relation token
    // may combine "parallel"/"equal"/"perpendicular" (space-separated); we apply each once per pair
    // (only for i<2 so the 0/2 and 1/3 pairs aren't double-emitted). "perpendicular" relates the
    // pair's first edge to the adjacent edge i+1.
    auto pair = [&](const char* type, const std::string& a, const std::string& b) {
        if (a.empty() || b.empty()) return;
        nlohmann::json op = {{"op", "sketch_add_constraint"}, {"sketch", fid_},
            {"constraint", {{"type", type}, {"refs", {a, b}}}}};
        st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    };
    for (int i = 0; i < 2; ++i) {
        const char* rel = relations[i];
        if (!rel) continue;
        std::string tok(rel);
        if (tok.find("parallel") != std::string::npos) pair("parallel", ids[i], ids[i + 2]);
        if (tok.find("equal") != std::string::npos)    pair("equal",    ids[i], ids[i + 2]);
        if (tok.find("perpendicular") != std::string::npos) pair("perpendicular", ids[i], ids[i + 1]);
    }
    resolve(st);
}

// C42: N-line closed loop (generalized addQuadLoop without the per-edge h/v relations). Each edge
// gets a chained coincident to its predecessor's p2 (+ loop-closing coincident); equalEdges adds
// "equal" between consecutive edges so a Polygon stays regular-ish under drag.
void SketchMode::addPolyLoop(AppState& st, const std::vector<ImVec2>& corners, bool equalEdges) {
    const size_t n = corners.size();
    if (n < 3) return;
    std::vector<std::string> ids(n);
    std::string prevId, firstId;
    for (size_t i = 0; i < n; ++i) {
        size_t j = (i + 1) % n;
        nlohmann::json cs = nlohmann::json::array();
        if (!prevId.empty())
            cs.push_back({{"type", "coincident"}, {"refs", {prevId + ".p2", "$self.p1"}}});
        nlohmann::json ent = {{"type", "line"},
            {"p1", nlohmann::json::array({corners[i].x, corners[i].y})},
            {"p2", nlohmann::json::array({corners[j].x, corners[j].y})}};
        nlohmann::json op = {{"op", "sketch_add_entity"}, {"sketch", fid_},
                             {"entity", ent}, {"constraints", cs}};
        auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
        if (!r.ok) { st.addLog("[sketch] poly line failed: " + r.error_message); break; }
        std::string nid = (r.id_map.is_object() && r.id_map.contains("$0"))
                        ? r.id_map["$0"].get<std::string>() : std::string();
        ids[i] = nid;
        if (i == 0) firstId = nid;
        prevId = nid;
        if (i == n - 1 && !firstId.empty() && !nid.empty()) {
            nlohmann::json clop = {{"op", "sketch_add_constraint"}, {"sketch", fid_},
                {"constraint", {{"type", "coincident"}, {"refs", {nid + ".p2", firstId + ".p1"}}}}};
            st.docMirror.applyOps(st.worker, nlohmann::json::array({clop}));
        }
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    }
    if (equalEdges) {
        for (size_t i = 0; i + 1 < n; ++i) {
            if (ids[i].empty() || ids[i + 1].empty()) continue;
            nlohmann::json op = {{"op", "sketch_add_constraint"}, {"sketch", fid_},
                {"constraint", {{"type", "equal"}, {"refs", {ids[i], ids[i + 1]}}}}};
            st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
        }
    }
    resolve(st);
}

// ---------------------------------------------------------------------------
// enter / exit
// ---------------------------------------------------------------------------

bool SketchMode::cameraLock(float& yaw, float& pitch) const {
    if (!active_) return false;
    yaw = camYaw_; pitch = camPitch_;
    return true;
}

bool SketchMode::enterForFeature(AppState& st, const std::string& sketchFid) {
    if (sketchFid.empty()) return false;
    fid_ = sketchFid;
    nlohmann::json res;
    if (!callSketch(st, "sketch_enter", nlohmann::json::object(), res)) { fid_.clear(); return false; }
    applyEnterResult(res);
    active_ = true;
    isNewEmpty_ = false;
    resetTool();
    // C34: remember the pre-sketch camera so exit can restore the user's 3D view
    // (SolidWorks returns to the previous orientation when a sketch closes).
    prevYaw_ = st.yaw; prevPitch_ = st.pitch; prevZoom_ = st.zoom;
    prevPan_[0] = st.pan[0]; prevPan_[1] = st.pan[1]; prevPan_[2] = st.pan[2];
    havePrevCam_ = true;
    // Lock the camera looking down the plane normal (frame_ column 2 = z_dir in world).
    float nz0 = frame_[2], nz1 = frame_[6], nz2 = frame_[10];
    // The renderer's orbit convention (viewport_panel/renderer drawCurrent) is
    //   dir(yaw,pitch) = (cos(p)*sin(y), -cos(p)*cos(y), sin(p))   [target->eye]
    // For a fronto-parallel view we need dir == +normal:
    //   pitch = asin(nz), yaw = atan2(nx, -ny).
    // (The old atan2(ny, nx) put Front/Right plane sketches EDGE-ON: the u-axis projected to
    // ~0 screen px, buildBasis went degenerate, and every sketch tool silently died.)
    camPitch_ = std::asin(std::fmax(-1.0f, std::fmin(1.0f, nz2))) * 57.2957795f;
    camYaw_ = std::atan2(nz0, -nz1) * 57.2957795f;
    if (std::fabs(camPitch_) > 88.9f) camPitch_ = camPitch_ > 0 ? 89.0f : -89.0f;
    st.yaw = camYaw_; st.pitch = camPitch_;
    // C37-D: snapshot the doc so the confirmation-corner ✗ (cancel) can revert the whole session.
    // Best-effort: if the worker lacks doc_checkpoint the corner falls back to a plain exit.
    cancelCheckpoint_.clear();
    if (st.docMirror.hasDoc() && st.workerStarted) {
        nlohmann::json cres, cp = {{"doc_id", st.docMirror.docId()}};
        std::string cerr;
        if (st.worker.call("doc_checkpoint", cp, cres, cerr) && cres.contains("checkpoint"))
            cancelCheckpoint_ = cres.value("checkpoint", std::string());
    }
    st.addLog("[sketch] entered " + fid_ + "  (dof=" + std::to_string(dof_) + ")");
    return true;
}

// C37-D: create a sketch on an explicit plane ref and enter it. Returns the new feature id.
std::string SketchMode::createSketchOnPlane(AppState& st, const nlohmann::json& planeRef) {
    if (!st.docMirror.hasDoc()) {
        std::string err;
        if (!st.docMirror.newPart(st.worker, err)) { st.addLog("[sketch] newPart failed: " + err); return {}; }
        st.clearViewportForNewDoc();
    }
    nlohmann::json ops = nlohmann::json::array({
        {{"op", "add_feature"}, {"feature", {
            {"type", "sketch"}, {"name", "Sketch"},
            {"refs", {{"plane", planeRef}}},
            {"params", {{"entities", nlohmann::json::array()},
                        {"constraints", nlohmann::json::array()}}}}}}});
    auto r = st.docMirror.applyOps(st.worker, ops);
    if (!r.ok) { st.addLog("[sketch] create sketch failed: " + r.error_message); return {}; }
    std::string newFid = (r.id_map.is_object() && r.id_map.contains("$0"))
                       ? r.id_map["$0"].get<std::string>() : std::string();
    if (newFid.empty()) { st.addLog("[sketch] create sketch: no id"); return {}; }
    if (!enterForFeature(st, newFid)) return {};
    isNewEmpty_ = true;
    return newFid;
}

bool SketchMode::enterNewOnSelection(AppState& st) {
    // C38 (S2): a double-fire of the ribbon "New Sketch" (sketch_new) while a sketch is already
    // entered would start a plane-pick over the live sketch and leave a zombie state. Refuse to
    // re-enter when already active — the user must exit the current sketch first.
    if (active_) return false;
    // C37-D: if the selection already names a usable plane/planar face, enter directly. Otherwise
    // start the SolidWorks plane-pick step (drawOverlay handles the ray-pick while the flag is set).
    nlohmann::json planeRef;
    bool haveRef = false;
    for (const auto& it : st.selection.items()) {
        if (!it.selector.empty()) {
            try {
                nlohmann::json sel = nlohmann::json::parse(it.selector);
                std::string k = sel.value("kind", std::string());
                if (k == "face" || k == "datum") { planeRef = sel; haveRef = true; break; }
            } catch (...) {}
        }
    }
    if (haveRef) return !createSketchOnPlane(st, planeRef).empty();
    // No usable selection: enter the interactive plane-pick (front/top/right or a planar face).
    planePicking_ = true;
    planeHover_ = -1;
    st.sketchPlanePickActive = true;
    st.requestCursor(ui::CursorKind::Crosshair);
    return false;   // not yet in a sketch — the pick click finishes the job
}

void SketchMode::exitSketch(AppState& st, bool keep) {
    if (!active_) return;
    cancelTool();
    nlohmann::json res;
    callSketch(st, "sketch_exit", nlohmann::json::object(), res);
    if (res.contains("tess") && res["tess"].is_object()) {
        std::string aicm = res["tess"].value("aicm_path", std::string());
        if (!aicm.empty()) { std::string le; st.loadDocMesh(aicm, le); }
    }
    // Resync the mirror revision (sketch_exit bumps it server-side).
    resyncMirror(st);
    if (!keep && isNewEmpty_ && ents_.empty()) {
        nlohmann::json ops = nlohmann::json::array({
            {{"op", "delete_feature"}, {"id", fid_}, {"cascade", true}}});
        st.docMirror.applyOps(st.worker, ops);
    }
    st.addLog("[sketch] exited " + fid_);
    active_ = false;
    isNewEmpty_ = false;
    fid_.clear();
    ents_.clear();
    cons_.clear();              // C37-D
    autoPlace_.clear();         // C37-D
    cancelCheckpoint_.clear();  // C37-D
    status_.clear();
    dof_ = 0;
    // C38 (S2): unconditionally clear any plane-pick state so a stale pick flag can't survive an
    // exit (e.g. exit raced against an armed plane-pick) and trap the next session in pick mode.
    planePicking_ = false; planeHover_ = -1; st.sketchPlanePickActive = false;
    resetTool();
    // C34: glide back to the pre-sketch camera (the viewport resumes ticking the
    // animation now that sketch mode is off).
    if (havePrevCam_) {
        requestCameraAnimation(st, prevYaw_, prevPitch_, prevZoom_, prevPan_);
        havePrevCam_ = false;
    }
}

// C37-D: cancel the sketch session — revert to the entry checkpoint and leave sketch mode. The
// doc_revert response already carries the rebuilt mesh/tree (it bumps the revision), so we wire
// that directly instead of a second rebuild, then mirror the exit-path teardown.
void SketchMode::cancelSketch(AppState& st) {
    if (!active_) return;
    cancelTool();
    if (cancelCheckpoint_.empty() || !st.docMirror.hasDoc() || !st.workerStarted) {
        // No checkpoint available — fall back to a plain exit (deletes an empty new sketch).
        exitSketch(st, /*keep=*/false);
        return;
    }
    // C38 (S6): exit the worker's active sketch BEFORE reverting. doc_revert rewinds the document but
    // leaves the worker's active_sketch dangling, so the next sketch_enter errors with "already in
    // sketch". sketch_exit clears it; we ignore its tess payload because the revert below rebuilds.
    {
        nlohmann::json exitRes;
        callSketch(st, "sketch_exit", nlohmann::json::object(), exitRes);
    }
    nlohmann::json res, params = {{"doc_id", st.docMirror.docId()}, {"checkpoint", cancelCheckpoint_}};
    std::string err;
    bool ok = st.worker.call("doc_revert", params, res, err) && !res.contains("code");
    if (!ok) {
        st.toast(ui::ToastLevel::Warn, "Sketch cancel: revert failed — exited without revert");
        exitSketch(st, /*keep=*/true);
        return;
    }
    // The revert response is an ops-style envelope: load its mesh + resync the mirror revision.
    if (res.contains("tess") && res["tess"].is_object()) {
        std::string aicm = res["tess"].value("aicm_path", std::string());
        if (!aicm.empty()) { std::string le; st.loadDocMesh(aicm, le); }
    }
    resyncMirror(st);
    st.addLog("[sketch] cancelled " + fid_ + " (reverted to entry checkpoint)");
    st.toast(ui::ToastLevel::Info, "Sketch cancelled");
    // Teardown (mirror of exitSketch, minus the sketch_exit call — revert already rebuilt).
    active_ = false;
    isNewEmpty_ = false;
    fid_.clear();
    ents_.clear();
    cons_.clear();
    autoPlace_.clear();
    cancelCheckpoint_.clear();
    status_.clear();
    dof_ = 0;
    // C38 (S2): same unconditional plane-pick clear as exitSketch.
    planePicking_ = false; planeHover_ = -1; st.sketchPlanePickActive = false;
    resetTool();
    if (havePrevCam_) {
        requestCameraAnimation(st, prevYaw_, prevPitch_, prevZoom_, prevPan_);
        havePrevCam_ = false;
    }
}

// ---------------------------------------------------------------------------
// ribbon dispatch
// ---------------------------------------------------------------------------

bool SketchMode::onRibbonCommand(AppState& st, const std::string& cmdId) {
    if (cmdId == "sketch_new") { enterNewOnSelection(st); return true; }
    if (!active_) return false;  // the rest of the sketch tools require an active sketch
    if (cmdId == "sketch_exit") { exitSketch(st, /*keep=*/true); return true; }
    if (cmdId == "sk_line")        { resetTool(); tool_ = SketchTool::Line;       return true; }
    if (cmdId == "sk_rect_corner") { resetTool(); tool_ = SketchTool::RectCorner; return true; }
    if (cmdId == "sk_circle")      { resetTool(); tool_ = SketchTool::Circle;     return true; }
    if (cmdId == "sk_arc_center")  { resetTool(); tool_ = SketchTool::ArcCenter;  return true; }
    if (cmdId == "sk_slot")        { resetTool(); tool_ = SketchTool::Slot;       return true; }
    if (cmdId == "smart_dim")      { resetTool(); tool_ = SketchTool::SmartDim;   return true; }
    // C28-B flyout variants:
    if (cmdId == "sk_rect_3pt_corner") { resetTool(); tool_ = SketchTool::Rect3ptCorner;  return true; }
    if (cmdId == "sk_rect_3pt_center") { resetTool(); tool_ = SketchTool::Rect3ptCenter;  return true; }
    if (cmdId == "sk_parallelogram")   { resetTool(); tool_ = SketchTool::Parallelogram;  return true; }
    if (cmdId == "sk_circle_perimeter"){ resetTool(); tool_ = SketchTool::CirclePerimeter;return true; }
    if (cmdId == "sk_slot_ctr_str")    { resetTool(); tool_ = SketchTool::SlotCtrStr;     return true; }
    if (cmdId == "sk_slot_arc_ctr")    { resetTool(); tool_ = SketchTool::SlotArcCtr;     return true; }
    if (cmdId == "sk_slot_arc_3pt")    { resetTool(); tool_ = SketchTool::SlotArc3pt;     return true; }
    if (cmdId == "sk_ellipse_partial") { resetTool(); tool_ = SketchTool::EllipsePartial; return true; }
    if (cmdId == "sk_parabola")        { resetTool(); tool_ = SketchTool::Parabola;       return true; }
    if (cmdId == "sk_conic")           { resetTool(); tool_ = SketchTool::Conic;          return true; }
    // C29-B sketch-editing tools (engine ops sketch_trim / sketch_extend / sketch_offset):
    if (cmdId == "sk_trim")            { resetTool(); tool_ = SketchTool::Trim;           return true; }
    if (cmdId == "sk_extend")          { resetTool(); tool_ = SketchTool::Extend;         return true; }
    if (cmdId == "sk_offset_ent")      { resetTool(); tool_ = SketchTool::Offset;
                                         offsetStage_ = 0; offsetChain_.clear();           return true; }
    // C42: remaining draw tools.
    if (cmdId == "sk_arc_3pt")         { resetTool(); tool_ = SketchTool::Arc3pt;         return true; }
    if (cmdId == "sk_polygon")         { resetTool(); tool_ = SketchTool::Polygon;        return true; }
    if (cmdId == "sk_rect_center")     { resetTool(); tool_ = SketchTool::RectCenter;     return true; }
    if (cmdId == "sk_point")           { resetTool(); tool_ = SketchTool::Point;          return true; }
    if (cmdId == "sk_ellipse" || cmdId == "sk_ellipse_big") {
        resetTool(); tool_ = SketchTool::Ellipse; return true;
    }
    if (cmdId == "sk_spline")          { resetTool(); tool_ = SketchTool::Spline;         return true; }
    // C42: Construction — immediate toggle on the current selection (engine op
    // sketch_toggle_construction). Not a tool: the selection survives (entity ids persist).
    if (cmdId == "sk_construction") {
        if (selIds_.empty()) {
            st.toast(ui::ToastLevel::Info,
                     "Construction: select sketch entities first (click them with no tool armed)");
            return true;
        }
        nlohmann::json ops = nlohmann::json::array();
        for (const auto& id : selIds_)
            ops.push_back({{"op", "sketch_toggle_construction"}, {"sketch", fid_}, {"id", id}});
        auto r = st.docMirror.applyOps(st.worker, ops);
        if (!r.ok) st.toast(ui::ToastLevel::Error, "Construction: " + r.error_message);
        else {
            if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
            resolve(st);
        }
        return true;
    }
    // C42: minimal Add Relation popup over the current 1-2 entity selection (drawOverlay renders
    // it; the offered relations depend on the selected entity types).
    if (cmdId == "sk_relations") {
        if (selIds_.empty() || selIds_.size() > 2) {
            st.toast(ui::ToastLevel::Info, "Add Relation: select 1 or 2 sketch entities first");
            return true;
        }
        relIds_ = selIds_;
        relPopupOpen_ = true;
        relPopupPos_ = ImGui::GetMousePos();
        return true;
    }
    return false;
}

// C40: armed tool -> ribbon cmd id (inverse of onRibbonCommand). "" when no tool armed.
const char* SketchMode::activeToolCmd() const {
    if (!active_) return "";
    switch (tool_) {
        case SketchTool::Line:            return "sk_line";
        case SketchTool::RectCorner:      return "sk_rect_corner";
        case SketchTool::Circle:          return "sk_circle";
        case SketchTool::ArcCenter:       return "sk_arc_center";
        case SketchTool::Slot:            return "sk_slot";
        case SketchTool::SmartDim:        return "smart_dim";
        case SketchTool::Rect3ptCorner:   return "sk_rect_3pt_corner";
        case SketchTool::Rect3ptCenter:   return "sk_rect_3pt_center";
        case SketchTool::Parallelogram:   return "sk_parallelogram";
        case SketchTool::CirclePerimeter: return "sk_circle_perimeter";
        case SketchTool::SlotCtrStr:      return "sk_slot_ctr_str";
        case SketchTool::SlotArcCtr:      return "sk_slot_arc_ctr";
        case SketchTool::SlotArc3pt:      return "sk_slot_arc_3pt";
        case SketchTool::EllipsePartial:  return "sk_ellipse_partial";
        case SketchTool::Parabola:        return "sk_parabola";
        case SketchTool::Conic:           return "sk_conic";
        case SketchTool::Trim:            return "sk_trim";
        case SketchTool::Extend:          return "sk_extend";
        case SketchTool::Offset:          return "sk_offset_ent";
        case SketchTool::Arc3pt:          return "sk_arc_3pt";
        case SketchTool::Polygon:         return "sk_polygon";
        case SketchTool::RectCenter:      return "sk_rect_center";
        case SketchTool::Point:           return "sk_point";
        case SketchTool::Ellipse:         return "sk_ellipse_big";
        case SketchTool::Spline:          return "sk_spline";
        default:                          return "";
    }
}

// ---------------------------------------------------------------------------
// tool helpers
// ---------------------------------------------------------------------------

void SketchMode::resetTool() { tool_ = SketchTool::None; toolUV_.clear(); toolSnaps_.clear();
                               conicRho_ = 0.5f; chainPrevId_.clear();
                               selIds_.clear();   // C41 (T2): selection dies with any tool change/exit
                               relPopupOpen_ = false; relIds_.clear();   // C42: relation popup too
                               offsetStage_ = 0; offsetChain_.clear(); offsetFocusInput_ = false;
                               // C37-D: clear the Smart Dimension state machine.
                               dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr;
                               dimType_.clear(); dimAxis_.clear(); dimEditCid_.clear();
                               dimPopupOpen_ = false; dimError_.clear(); dimPopupDriven_ = false; }
void SketchMode::cancelTool() { resetTool(); }

void SketchMode::snapPoint(float& u, float& v, std::string& snapHandle, SnapInfer* infer) const {
    snapHandle.clear();
    const ImVec2 cur = uvToScreen(u, v);   // raw cursor (snap distances measure from here)
    // Visit every snappable POINT (handle, u, v) — point snap + alignment share this set.
    auto forEachPoint = [&](const std::function<void(const std::string&, float, float)>& fn) {
        for (const auto& e : ents_) {
            if (e.type == "line" || e.type == "slot") {
                fn(e.id + ".p1", e.p1[0], e.p1[1]);
                fn(e.id + ".p2", e.p2[0], e.p2[1]);
            } else if (e.type == "circle" || e.type == "arc" || e.type == "arc_slot" ||
                       e.type == "ellipse") {
                fn(e.id + ".center", e.center[0], e.center[1]);
            } else if (e.type == "point") {
                fn(e.id + ".p", e.p1[0], e.p1[1]);
            } else if (e.type == "spline") {  // C42: sub-handles p0..pN (sketch_solver convention)
                for (size_t i = 0; i < e.pts.size(); ++i)
                    fn(e.id + ".p" + std::to_string(i), e.pts[i].x, e.pts[i].y);
            }
        }
    };
    // 1) Endpoint/center/point coincident snap within kSnapPx (screen space) — highest priority.
    float best = kSnapPx * kSnapPx;
    forEachPoint([&](const std::string& handle, float eu, float ev) {
        ImVec2 s = uvToScreen(eu, ev);
        float dx = s.x - cur.x, dy = s.y - cur.y;
        float d2 = dx * dx + dy * dy;
        if (d2 < best) { best = d2; snapHandle = handle; u = eu; v = ev; }
    });
    if (!inferenceOn_) return;   // master toggle: everything below is C49 inference
    // 2) Line-midpoint snap ("mid:<id>") — only when no point snap fired.
    if (snapHandle.empty()) {
        best = kSnapPx * kSnapPx;
        for (const auto& e : ents_) {
            if (e.type != "line") continue;
            float mu2 = (e.p1[0] + e.p2[0]) * 0.5f, mv2 = (e.p1[1] + e.p2[1]) * 0.5f;
            ImVec2 s = uvToScreen(mu2, mv2);
            float dx = s.x - cur.x, dy = s.y - cur.y;
            float d2 = dx * dx + dy * dy;
            if (d2 < best) { best = d2; snapHandle = "mid:" + e.id; u = mu2; v = mv2; }
        }
    }
    // 3) Circle/arc RIM snap ("rim:<id>"): within kSnapPx of the rim (full-circle metric, like
    //    hitTestEntity), pulled to the exact rim along the center->cursor ray. Tiny circles
    //    (rim within 2*kSnapPx of the center) are skipped — the center snap owns those.
    if (snapHandle.empty()) {
        best = kSnapPx;          // compare |dist - r| in px (1D)
        for (const auto& e : ents_) {
            if (e.type != "circle" && e.type != "arc") continue;
            ImVec2 cc = uvToScreen(e.center[0], e.center[1]);
            ImVec2 rimRef = uvToScreen(e.center[0] + e.radius, e.center[1]);
            float rpx = std::hypot(rimRef.x - cc.x, rimRef.y - cc.y);
            if (rpx < 2.0f * kSnapPx) continue;
            float dm = std::hypot(cur.x - cc.x, cur.y - cc.y);
            float d = std::fabs(dm - rpx);
            if (d < best) {
                float a = std::atan2(v - e.center[1], u - e.center[0]);
                best = d; snapHandle = "rim:" + e.id;
                u = e.center[0] + e.radius * std::cos(a);
                v = e.center[1] + e.radius * std::sin(a);
            }
        }
    }
    // 4) Alignment inference (positional only — SolidWorks-style dashed lines, no constraint):
    //    cursor u (or v) within kSnapPx of a source point's u (or v), the point itself clearly
    //    OUTSIDE the coincident radius (else the dashed line degenerates / fights the point snap).
    if (infer && snapHandle.empty()) {
        const float pxPerU = std::hypot(uAxisS_.x, uAxisS_.y);
        const float pxPerV = std::hypot(vAxisS_.x, vAxisS_.y);
        float bestU = kSnapPx, bestV = kSnapPx;   // screen-px distance along each axis
        forEachPoint([&](const std::string&, float eu, float ev) {
            ImVec2 s = uvToScreen(eu, ev);
            float d2 = (s.x - cur.x) * (s.x - cur.x) + (s.y - cur.y) * (s.y - cur.y);
            if (d2 < (2.0f * kSnapPx) * (2.0f * kSnapPx)) return;  // too close to the point itself
            float dU = std::fabs(eu - u) * pxPerU;
            float dV = std::fabs(ev - v) * pxPerV;
            if (dU < bestU) { bestU = dU; infer->alignU = true; infer->srcU = ImVec2(eu, ev); }
            if (dV < bestV) { bestV = dV; infer->alignV = true; infer->srcV = ImVec2(eu, ev); }
        });
        if (infer->alignU) u = infer->srcU.x;
        if (infer->alignV) v = infer->srcV.y;
    }
}

int SketchMode::hitTestHandle(const ImVec2& s, std::string& handleOut, float& uOut, float& vOut) const {
    float best = kHandlePx * kHandlePx;
    int hit = -1;
    auto consider = [&](const std::string& handle, float eu, float ev) {
        ImVec2 p = uvToScreen(eu, ev);
        float dx = p.x - s.x, dy = p.y - s.y;
        float d2 = dx * dx + dy * dy;
        if (d2 < best) { best = d2; handleOut = handle; uOut = eu; vOut = ev; hit = 1; }
    };
    for (const auto& e : ents_) {
        if (e.type == "line" || e.type == "slot") {
            consider(e.id + ".p1", e.p1[0], e.p1[1]);
            consider(e.id + ".p2", e.p2[0], e.p2[1]);
        } else if (e.type == "circle" || e.type == "arc" || e.type == "arc_slot" ||
                   e.type == "ellipse") {
            consider(e.id + ".center", e.center[0], e.center[1]);
        } else if (e.type == "point") {
            consider(e.id + ".p", e.p1[0], e.p1[1]);
        } else if (e.type == "spline") {   // C42: draggable through-points p0..pN
            for (size_t i = 0; i < e.pts.size(); ++i)
                consider(e.id + ".p" + std::to_string(i), e.pts[i].x, e.pts[i].y);
        }
    }
    return hit;
}

// Nearest whole entity under the screen point. Distance is measured to the projected geometry:
// segment for lines/slots, |dist-to-center - radius_px| for circles/arcs, and the same rim metric
// (no angular clamp) for arcs — a deliberately simple approximation matching the Smart-Dimension
// picker. Returns the ents_ index or -1 when nothing is within maxPx.
int SketchMode::hitTestEntity(const ImVec2& s, float maxPx) const {
    float best = maxPx * maxPx;
    int hit = -1;
    for (size_t i = 0; i < ents_.size(); ++i) {
        const SketchEntity& e = ents_[i];
        float d2 = 1e30f;
        if (e.type == "line" || e.type == "slot") {
            ImVec2 a = uvToScreen(e.p1[0], e.p1[1]), b = uvToScreen(e.p2[0], e.p2[1]);
            float vx = b.x - a.x, vy = b.y - a.y;
            float wx = s.x - a.x, wy = s.y - a.y;
            float t = (vx * wx + vy * wy) / std::fmax(1e-6f, vx * vx + vy * vy);
            t = std::fmax(0.0f, std::fmin(1.0f, t));
            float px = a.x + t * vx, py = a.y + t * vy;
            d2 = (px - s.x) * (px - s.x) + (py - s.y) * (py - s.y);
        } else if (e.type == "circle" || e.type == "arc") {
            ImVec2 cc = uvToScreen(e.center[0], e.center[1]);
            ImVec2 rim = uvToScreen(e.center[0] + e.radius, e.center[1]);
            float rpx = std::hypot(rim.x - cc.x, rim.y - cc.y);
            float dm = std::hypot(s.x - cc.x, s.y - cc.y);
            d2 = (dm - rpx) * (dm - rpx);
        } else if (e.type == "point") {                       // C42: distance to the point itself
            ImVec2 p = uvToScreen(e.p1[0], e.p1[1]);
            d2 = (p.x - s.x) * (p.x - s.x) + (p.y - s.y) * (p.y - s.y);
        } else if (e.type == "ellipse" || e.type == "spline") {
            // C42: min distance to a sampled polyline (32-seg ellipse / spline through-points).
            auto seg2 = [&](ImVec2 a, ImVec2 b) {
                float vx = b.x - a.x, vy = b.y - a.y;
                float wx = s.x - a.x, wy = s.y - a.y;
                float t = (vx * wx + vy * wy) / std::fmax(1e-6f, vx * vx + vy * vy);
                t = std::fmax(0.0f, std::fmin(1.0f, t));
                float px = a.x + t * vx, py = a.y + t * vy;
                return (px - s.x) * (px - s.x) + (py - s.y) * (py - s.y);
            };
            if (e.type == "ellipse") {
                const float D2R = 3.14159265f / 180.0f;
                float ca = std::cos(e.rot * D2R), sa = std::sin(e.rot * D2R);
                ImVec2 prev;
                for (int k = 0; k <= 32; ++k) {
                    float a = k / 32.0f * 6.2831853f;
                    float lx = e.rx * std::cos(a), ly = e.ry * std::sin(a);
                    ImVec2 p = uvToScreen(e.center[0] + lx * ca - ly * sa,
                                          e.center[1] + lx * sa + ly * ca);
                    if (k) d2 = std::fmin(d2, seg2(prev, p));
                    prev = p;
                }
            } else {
                for (size_t k = 1; k < e.pts.size(); ++k)
                    d2 = std::fmin(d2, seg2(uvToScreen(e.pts[k - 1].x, e.pts[k - 1].y),
                                            uvToScreen(e.pts[k].x, e.pts[k].y)));
            }
        }
        if (d2 < best) { best = d2; hit = (int)i; }
    }
    return hit;
}

// ---------------------------------------------------------------------------
// status
// ---------------------------------------------------------------------------

std::string SketchMode::statusLine() const {
    if (!active_) return {};
    // C41 (T4): a sketch with no geometry isn't meaningfully "Fully Defined DOF: 0" — invite
    // the user to draw instead.
    if (ents_.empty()) return "Empty sketch \xE2\x80\x94 pick a tool to draw";
    const char* s = status_ == "full" ? "Fully Defined"
                  : status_ == "over" ? "Over Defined" : "Under Defined";
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%s   DOF: %d", s, dof_);
    return std::string(buf);
}

// ---------------------------------------------------------------------------
// C37-D: Smart Dimension state machine + dimension annotation rendering
// ---------------------------------------------------------------------------

namespace {
// Format a length (mm) for a dimension label, mirroring pmFormatDim's mm path without the unit
// suffix subtleties (we add the suffix ourselves). Trims trailing zeros.
std::string fmtLen(double mm) {
    char b[48]; std::snprintf(b, sizeof(b), "%.2f", mm);
    std::string s(b);
    if (s.find('.') != std::string::npos) { while (s.back() == '0') s.pop_back(); if (s.back() == '.') s.pop_back(); }
    return s;
}
std::string fmtAng(double deg) {
    char b[48]; std::snprintf(b, sizeof(b), "%.2f", deg);
    std::string s(b);
    if (s.find('.') != std::string::npos) { while (s.back() == '0') s.pop_back(); if (s.back() == '.') s.pop_back(); }
    return s + "\xC2\xB0";   // degree sign
}
// Filled triangle arrowhead at tip, pointing along (dirx,diry) (unit), size px.
void arrowHead(ImDrawList* dl, ImVec2 tip, float dx, float dy, float sz, ImU32 c) {
    float px = -dy, py = dx;   // perpendicular
    ImVec2 a(tip.x - dx * sz + px * sz * 0.35f, tip.y - dy * sz + py * sz * 0.35f);
    ImVec2 b(tip.x - dx * sz - px * sz * 0.35f, tip.y - dy * sz - py * sz * 0.35f);
    dl->AddTriangleFilled(tip, a, b, c);
}
}  // namespace

// Label text for a constraint (driven dims are parenthesized, SolidWorks-style).
static std::string dimLabel(const SketchConstraint& c) {
    double v = c.hasMeasured ? c.measured : (c.hasValue ? c.value : 0.0);
    std::string body;
    if (c.type == "angle")          body = fmtAng(v);
    else if (c.type == "radius")    body = "R" + fmtLen(v);
    else if (c.type == "diameter")  body = "\xE2\x8C\x80" + fmtLen(v);   // ⌀ U+2300 (CADSymbols)
    else                            body = fmtLen(v);
    if (c.hasExpr) body += "";   // expr-bound dims still show the measured value
    return c.driven ? "(" + body + ")" : body;
}

// A default annotation anchor in (u,v) for a constraint that has no stored placement: offset the
// geometry's natural midpoint a bit so the line/arrows clear the entity.
bool SketchMode::defaultPlacement(const SketchConstraint& c, float& pu, float& pv) const {
    auto baseId = [](const std::string& h) { return h.substr(0, h.find('.')); };
    auto pt = [&](const std::string& h, float& u, float& v) -> bool {
        const SketchEntity* e = entById(baseId(h));
        if (!e) return false;
        if (h.find(".p1") != std::string::npos)      { u = e->p1[0]; v = e->p1[1]; }
        else if (h.find(".p2") != std::string::npos) { u = e->p2[0]; v = e->p2[1]; }
        else if (h.find(".center") != std::string::npos) { u = e->center[0]; v = e->center[1]; }
        else { u = e->p1[0]; v = e->p1[1]; }
        return true;
    };
    if (c.type == "radius" || c.type == "diameter") {
        const SketchEntity* e = c.refs.empty() ? nullptr : entById(baseId(c.refs[0]));
        if (!e) return false;
        pu = e->center[0] + e->radius * 0.707f; pv = e->center[1] + e->radius * 0.707f;
        return true;
    }
    if (c.type == "angle") {
        if (c.refs.size() < 2) return false;
        const SketchEntity* a = entById(baseId(c.refs[0])); const SketchEntity* b = entById(baseId(c.refs[1]));
        if (!a || !b) return false;
        pu = (a->p1[0] + a->p2[0] + b->p1[0] + b->p2[0]) * 0.25f;
        pv = (a->p1[1] + a->p2[1] + b->p1[1] + b->p2[1]) * 0.25f;
        return true;
    }
    // distance: midpoint of the two refs, pushed perpendicular by a fixed sketch offset.
    if (c.refs.size() < 2) return false;
    float u0, v0, u1, v1;
    bool r0 = pt(c.refs[0], u0, v0);
    // point-line distance: second ref may be a bare entity id (line)
    const SketchEntity* le = entById(baseId(c.refs[1]));
    bool secondIsLine = le && le->type == "line" && c.refs[1].find('.') == std::string::npos;
    if (!r0) return false;
    if (secondIsLine) { u1 = (le->p1[0] + le->p2[0]) * 0.5f; v1 = (le->p1[1] + le->p2[1]) * 0.5f; }
    else if (!pt(c.refs[1], u1, v1)) return false;
    float mu = (u0 + u1) * 0.5f, mv = (v0 + v1) * 0.5f;
    float du = u1 - u0, dv = v1 - v0; float L = std::hypot(du, dv);
    float nx = L > 1e-6f ? -dv / L : 0, ny = L > 1e-6f ? du / L : 1;
    pu = mu + nx * 12.0f; pv = mv + ny * 12.0f;
    return true;
}

// Where a constraint's text sits on screen (for hover/drag/double-click hit-testing).
bool SketchMode::dimTextAnchor(const SketchConstraint& c, ImVec2& out) const {
    float pu, pv;
    auto it = autoPlace_.find(c.id);
    if (c.hasPlacement) { pu = c.placement[0]; pv = c.placement[1]; }
    else if (it != autoPlace_.end()) { pu = it->second.x; pv = it->second.y; }
    else if (!defaultPlacement(c, pu, pv)) return false;
    out = uvToScreen(pu, pv);
    return true;
}

// Render one dimension annotation. (pu,pv) is the placement in (u,v).
void SketchMode::drawDimension(ImDrawList* dl, const SketchConstraint& c, ImU32 lineCol,
                               ImU32 textCol, bool hovered, float pu, float pv) const {
    auto baseId = [](const std::string& h) { return h.substr(0, h.find('.')); };
    auto handlePt = [&](const std::string& h, ImVec2& s) -> bool {
        const SketchEntity* e = entById(baseId(h));
        if (!e) return false;
        if (h.find(".p1") != std::string::npos)      s = uvToScreen(e->p1[0], e->p1[1]);
        else if (h.find(".p2") != std::string::npos) s = uvToScreen(e->p2[0], e->p2[1]);
        else if (h.find(".center") != std::string::npos) s = uvToScreen(e->center[0], e->center[1]);
        else s = uvToScreen(e->p1[0], e->p1[1]);
        return true;
    };
    ImVec2 place = uvToScreen(pu, pv);
    std::string lbl = dimLabel(c);
    ImVec2 ts = ImGui::CalcTextSize(lbl.c_str());
    float th = hovered ? 2.2f : 1.4f;

    if (c.type == "radius" || c.type == "diameter") {
        const SketchEntity* e = c.refs.empty() ? nullptr : entById(baseId(c.refs[0]));
        if (!e) return;
        ImVec2 cc = uvToScreen(e->center[0], e->center[1]);
        // Leader from the rim toward the placement (radius) or across the circle (diameter).
        float ang = std::atan2(place.y - cc.y, place.x - cc.x);
        ImVec2 rimDir(std::cos(ang), std::sin(ang));
        float rpx = std::hypot(uvToScreen(e->center[0] + e->radius, e->center[1]).x - cc.x,
                               uvToScreen(e->center[0] + e->radius, e->center[1]).y - cc.y);
        ImVec2 rim(cc.x + rimDir.x * rpx, cc.y + rimDir.y * rpx);
        if (c.type == "diameter") {
            ImVec2 rim2(cc.x - rimDir.x * rpx, cc.y - rimDir.y * rpx);
            dl->AddLine(rim2, place, lineCol, th);
            arrowHead(dl, rim2, -rimDir.x, -rimDir.y, 8.0f, lineCol);
        } else {
            dl->AddLine(cc, place, lineCol, th);
        }
        dl->AddLine(rim, place, lineCol, th);
        arrowHead(dl, rim, rimDir.x, rimDir.y, 8.0f, lineCol);
        dl->AddText(ImVec2(place.x + 4, place.y - ts.y * 0.5f), textCol, lbl.c_str());
        return;
    }

    if (c.type == "angle") {
        if (c.refs.size() < 2) return;
        const SketchEntity* a = entById(baseId(c.refs[0])); const SketchEntity* b = entById(baseId(c.refs[1]));
        if (!a || !b) return;
        // Vertex ≈ intersection of the two lines (fallback: midpoint of all 4 endpoints).
        ImVec2 a1 = uvToScreen(a->p1[0], a->p1[1]), a2 = uvToScreen(a->p2[0], a->p2[1]);
        ImVec2 b1 = uvToScreen(b->p1[0], b->p1[1]), b2 = uvToScreen(b->p2[0], b->p2[1]);
        float dax = a2.x - a1.x, day = a2.y - a1.y, dbx = b2.x - b1.x, dby = b2.y - b1.y;
        float den = dax * dby - day * dbx;
        ImVec2 vtx;
        if (std::fabs(den) > 1e-4f) {
            float t = ((b1.x - a1.x) * dby - (b1.y - a1.y) * dbx) / den;
            vtx = ImVec2(a1.x + dax * t, a1.y + day * t);
        } else vtx = ImVec2((a1.x + a2.x + b1.x + b2.x) * 0.25f, (a1.y + a2.y + b1.y + b2.y) * 0.25f);
        float r = std::hypot(place.x - vtx.x, place.y - vtx.y); if (r < 14) r = 14;
        float ang0 = std::atan2(a2.y - a1.y, a2.x - a1.x);
        float ang1 = std::atan2(b2.y - b1.y, b2.x - b1.x);
        // small arc between the two line directions through the placement side
        dl->PathArcTo(vtx, r, ang0, ang1, 32);
        dl->PathStroke(lineCol, 0, th);
        dl->AddText(ImVec2(place.x + 4, place.y - ts.y * 0.5f), textCol, lbl.c_str());
        return;
    }

    // distance (point-point incl h/v, or point-line)
    ImVec2 P0, P1;
    bool p0ok = !c.refs.empty() && handlePt(c.refs[0], P0);
    const SketchEntity* le = c.refs.size() > 1 ? entById(baseId(c.refs[1])) : nullptr;
    bool secondIsLine = le && le->type == "line" && c.refs[1].find('.') == std::string::npos;
    if (!p0ok) return;
    if (secondIsLine) {
        // perpendicular foot of P0 onto the line
        ImVec2 l1 = uvToScreen(le->p1[0], le->p1[1]), l2 = uvToScreen(le->p2[0], le->p2[1]);
        float vx = l2.x - l1.x, vy = l2.y - l1.y; float L2 = vx * vx + vy * vy;
        float t = L2 > 1e-6f ? ((P0.x - l1.x) * vx + (P0.y - l1.y) * vy) / L2 : 0;
        P1 = ImVec2(l1.x + vx * t, l1.y + vy * t);
    } else if (c.refs.size() < 2 || !handlePt(c.refs[1], P1)) return;

    // For h/v variants, the dimension is measured along the axis: collapse one component.
    ImVec2 A = P0, B = P1;
    if (c.axis == "h")      B.y = A.y;   // horizontal: ignore vertical delta
    else if (c.axis == "v") B.x = A.x;   // vertical

    // Dimension line passes through the placement point, parallel to A->B; extension lines run from
    // A and B out to that offset line.
    float dx = B.x - A.x, dy = B.y - A.y; float L = std::hypot(dx, dy);
    float ux = L > 1e-6f ? dx / L : 1, uy = L > 1e-6f ? dy / L : 0;
    float nx = -uy, ny = ux;
    // signed offset of the placement from the A->B line
    float off = (place.x - A.x) * nx + (place.y - A.y) * ny;
    ImVec2 dA(A.x + nx * off, A.y + ny * off);
    ImVec2 dB(B.x + nx * off, B.y + ny * off);
    const float gap = 4.0f, ext = 6.0f;   // extension-line gap from geometry, overshoot past dimline
    auto extLine = [&](ImVec2 from, ImVec2 to) {
        // shorten near the geometry end by gap, extend past the dim line by ext
        float ex = to.x - from.x, ey = to.y - from.y; float el = std::hypot(ex, ey);
        if (el < 1e-3f) return;
        float gx = ex / el, gy = ey / el;
        dl->AddLine(ImVec2(from.x + gx * gap, from.y + gy * gap),
                    ImVec2(to.x + gx * ext, to.y + gy * ext), lineCol, th);
    };
    extLine(A, dA);
    extLine(B, dB);
    dl->AddLine(dA, dB, lineCol, th);
    arrowHead(dl, dA, -ux, -uy, 8.0f, lineCol);
    arrowHead(dl, dB, ux, uy, 8.0f, lineCol);
    // text centered on the dim line, nudged to the placement side
    ImVec2 mid((dA.x + dB.x) * 0.5f, (dA.y + dB.y) * 0.5f);
    dl->AddText(ImVec2(mid.x - ts.x * 0.5f + nx * 8, mid.y - ts.y * 0.5f + ny * 8), textCol, lbl.c_str());
}

// Smart Dimension pick: points (endpoints/centers) by priority within ~8px, then whole entities.
bool SketchMode::dimHitTest(const ImVec2& s, DimPick& out) const {
    // 1) points
    float best = 8.0f * 8.0f; bool got = false;
    auto consider = [&](const std::string& h, float u, float v) {
        ImVec2 p = uvToScreen(u, v);
        float d2 = (p.x - s.x) * (p.x - s.x) + (p.y - s.y) * (p.y - s.y);
        if (d2 < best) { best = d2; out.handle = h; out.isPoint = true; out.entIdx = -1; out.entId.clear(); out.u = u; out.v = v; got = true; }
    };
    for (const auto& e : ents_) {
        if (e.type == "line" || e.type == "slot") {
            consider(e.id + ".p1", e.p1[0], e.p1[1]);
            consider(e.id + ".p2", e.p2[0], e.p2[1]);
        } else if (e.type == "circle" || e.type == "arc" || e.type == "arc_slot") {
            consider(e.id + ".center", e.center[0], e.center[1]);
        } else if (e.type == "point") {
            consider(e.id + ".p", e.p1[0], e.p1[1]);
        }
    }
    if (got) return true;
    // 2) whole entity — C38 (S1): record the id (stable) alongside the per-frame index.
    int ei = hitTestEntity(s, 12.0f);
    if (ei >= 0) { out.handle.clear(); out.isPoint = false; out.entIdx = ei;
                   out.entId = ents_[ei].id; return true; }
    return false;
}

// Build the pending dimension from the current pick buffer + the live cursor. Returns true when the
// picks form a valid combo (and dimPending_/dimType_/dimMeasured_ are filled). cu,cv = cursor uv (for
// the h/v/aligned variant choice on point-point).
bool SketchMode::dimBuildPending(const ImVec2& cursor, float cu, float cv) {
    dimHasPending_ = false; dimAxis_.clear();
    // C38 (S1): resolve every pick's stored id to a CURRENT entity ptr. If any entity-pick has
    // vanished from ents_ (refilled mid-pick), bail — the combo is no longer valid.
    auto entOf = [&](const DimPick& p) -> const SketchEntity* {
        if (p.isPoint) return nullptr;
        return entById(p.entId);
    };
    for (const auto& p : dimPicks_)
        if (!p.isPoint && !entById(p.entId)) return false;
    auto handleUV = [&](const DimPick& p, float& u, float& v) {
        if (p.isPoint) { u = p.u; v = p.v; return; }
        const SketchEntity& e = *entOf(p);
        u = e.center[0]; v = e.center[1];
    };
    size_t n = dimPicks_.size();
    if (n == 1) {
        const DimPick& a = dimPicks_[0];
        if (!a.isPoint) {
            const SketchEntity& e = *entOf(a);
            if (e.type == "line") {                 // length: point-point on endpoints, aligned
                double Lm = std::hypot(e.p2[0] - e.p1[0], e.p2[1] - e.p1[1]);
                dimType_ = "distance"; dimMeasured_ = Lm;
                dimPending_ = {{"type", "distance"},
                    {"refs", nlohmann::json::array({e.id + ".p1", e.id + ".p2"})}};
                dimHasPending_ = true; return true;
            } else if (e.type == "circle") {
                dimType_ = "diameter"; dimMeasured_ = e.radius * 2.0;
                dimPending_ = {{"type", "diameter"}, {"refs", nlohmann::json::array({e.id})}};
                dimHasPending_ = true; return true;
            } else if (e.type == "arc") {
                dimType_ = "radius"; dimMeasured_ = e.radius;
                dimPending_ = {{"type", "radius"}, {"refs", nlohmann::json::array({e.id})}};
                dimHasPending_ = true; return true;
            }
        }
        return false;   // a single point alone is not a dimension yet
    }
    if (n == 2) {
        const DimPick& a = dimPicks_[0]; const DimPick& b = dimPicks_[1];
        bool aLine = !a.isPoint && entOf(a)->type == "line";
        bool bLine = !b.isPoint && entOf(b)->type == "line";
        if (a.isPoint && b.isPoint) {              // point-point distance (variant chosen at place)
            float au, av, bu, bv; handleUV(a, au, av); handleUV(b, bu, bv);
            // SolidWorks variant by cursor zone: beyond the vertical span -> horizontal; beyond the
            // horizontal span -> vertical; inside both -> aligned.
            float ymin = std::fmin(av, bv), ymax = std::fmax(av, bv);
            float xmin = std::fmin(au, bu), xmax = std::fmax(au, bu);
            if (cv < ymin || cv > ymax) dimAxis_ = "h";
            else if (cu < xmin || cu > xmax) dimAxis_ = "v";
            else dimAxis_.clear();
            double m;
            if (dimAxis_ == "h") m = std::fabs(bu - au);
            else if (dimAxis_ == "v") m = std::fabs(bv - av);
            else m = std::hypot(bu - au, bv - av);
            dimType_ = "distance"; dimMeasured_ = m;
            dimPending_ = {{"type", "distance"}, {"refs", nlohmann::json::array({a.handle, b.handle})}};
            if (!dimAxis_.empty()) dimPending_["axis"] = dimAxis_;
            dimHasPending_ = true; return true;
        }
        if ((a.isPoint && bLine) || (aLine && b.isPoint)) {   // point-line perpendicular distance
            const DimPick& pp = a.isPoint ? a : b;
            const DimPick& ll = a.isPoint ? b : a;
            const SketchEntity& le = *entOf(ll);
            float pu, pv; handleUV(pp, pu, pv);
            // perpendicular foot distance
            float dx = le.p2[0] - le.p1[0], dy = le.p2[1] - le.p1[1]; float L = std::hypot(dx, dy);
            float nx = L > 1e-6f ? -dy / L : 0, ny = L > 1e-6f ? dx / L : 1;
            double m = std::fabs((pu - le.p1[0]) * nx + (pv - le.p1[1]) * ny);
            dimType_ = "distance"; dimMeasured_ = m;
            dimPending_ = {{"type", "distance"}, {"refs", nlohmann::json::array({pp.handle, le.id})}};
            dimHasPending_ = true; return true;
        }
        if (aLine && bLine) {                      // two lines: parallel -> distance, else angle
            const SketchEntity& la = *entOf(a); const SketchEntity& lb = *entOf(b);
            float ax = la.p2[0] - la.p1[0], ay = la.p2[1] - la.p1[1];
            float bx = lb.p2[0] - lb.p1[0], by = lb.p2[1] - lb.p1[1];
            float cross = ax * by - ay * bx, dot = ax * bx + ay * by;
            float ang = std::atan2(std::fabs(cross), dot) * 57.2957795f;   // [0,180)
            float par = std::fmin(ang, 180.0f - ang);                       // angle from parallel
            if (par < 2.0f) {                        // nearly parallel -> point-line distance
                float L = std::hypot(bx, by);
                float nx = L > 1e-6f ? -by / L : 0, ny = L > 1e-6f ? bx / L : 1;
                double m = std::fabs((la.p1[0] - lb.p1[0]) * nx + (la.p1[1] - lb.p1[1]) * ny);
                dimType_ = "distance"; dimMeasured_ = m;
                dimPending_ = {{"type", "distance"}, {"refs", nlohmann::json::array({la.id + ".p1", lb.id})}};
                dimHasPending_ = true; return true;
            } else {                                 // angle between the two lines
                dimType_ = "angle"; dimMeasured_ = ang;
                dimPending_ = {{"type", "angle"}, {"refs", nlohmann::json::array({la.id, lb.id})}};
                dimHasPending_ = true; return true;
            }
        }
    }
    return false;
}

void SketchMode::dimOpenModify(const ImVec2& at) {
    dimError_.clear();
    dimPopupOpen_ = true;
    dimPopupFocus_ = true;
    dimPopupPos_ = at;
    dimIsAngle_ = (dimType_ == "angle");
    dimPopupStep_ = dimIsAngle_ ? 1.0 : 1.0;
    // C38 (S5): when there's no real measured value, leave the buffer EMPTY so a stray Enter can't
    // silently commit a fake "0" — the user must type a value (pmParseDim rejects empty). When there
    // IS a value, seed the plain number; for angles dimMeasured_ is already in DEGREES (set by
    // dimBuildPending), so fmtLen yields the correct degree number — the "°" unit is shown as the box
    // suffix label, NOT baked into the buffer (a trailing "°" would make pmParseDim reject the value).
    if (std::isfinite(dimMeasured_) && std::fabs(dimMeasured_) > 1e-9)
        std::snprintf(dimBuf_.data(), dimBuf_.size(), "%s", fmtLen(dimMeasured_).c_str());
    else
        dimBuf_[0] = '\0';
}

// Commit the Modify box: parse value/expr, apply, and (on success) keep the tool armed.
void SketchMode::dimCommit(AppState& st) {
    std::string txt = dimBuf_.data();
    nlohmann::json con = dimPending_;
    bool isExpr = (!txt.empty() && txt[0] == '=');
    double mm = dimMeasured_;
    if (!isExpr) {
        std::string perr;
        if (!pmParseDim(txt, mm, perr)) { dimError_ = perr.empty() ? "bad value" : perr; return; }
    }
    if (!dimEditCid_.empty()) {
        // Editing an EXISTING dim: sketch_set_dim.
        nlohmann::json op = {{"op", "sketch_set_dim"}, {"sketch", fid_}, {"id", dimEditCid_}};
        if (isExpr) op["expr"] = txt.substr(1); else op["value"] = mm;
        auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
        if (!r.ok) { st.toast(ui::ToastLevel::Error, "Dimension: " + r.error_message); dimError_ = r.error_message; return; }
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
        resolve(st);
        // C38 (S3): full pick-buffer cleanup, matching the new-dim path below. A double-click edit
        // leaves stale picks otherwise, which dimBuildPending consumes on the next frame.
        dimPopupOpen_ = false; dimEditCid_.clear();
        dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr;
        dimAxis_.clear(); dimPopupDriven_ = false;
        return;
    }
    // New dim: attach value/expr + placement + driven, then sketch_add_constraint.
    con["value"] = isExpr ? nlohmann::json{{"value", dimMeasured_}, {"expr", txt.substr(1)}}
                          : nlohmann::json{{"value", mm}, {"expr", nullptr}};
    con["placement"] = nlohmann::json::array({dimPlaceU_, dimPlaceV_});
    if (dimPopupDriven_) con["driven"] = true;
    nlohmann::json ops = nlohmann::json::array({
        {{"op", "sketch_add_constraint"}, {"sketch", fid_}, {"constraint", con}}});
    auto r = st.docMirror.applyOps(st.worker, ops);
    if (!r.ok) {
        // Over-define protection: surface the engine error, never silently drop.
        st.toast(ui::ToastLevel::Error, "Dimension: " + r.error_message);
        dimError_ = r.error_message;
        return;
    }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);
    // Stay in Smart Dimension; clear the pick buffer for the next dimension.
    dimPopupOpen_ = false; dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr;
    dimAxis_.clear(); dimPopupDriven_ = false;
}

// C37-D: in-sketch Ctrl+Z. Mid-stroke -> pop the last placed point. Else doc undo + re-solve; if the
// sketch vanished, exit gracefully.
void SketchMode::inSketchUndo(AppState& st) {
    if (tool_ != SketchTool::None && tool_ != SketchTool::SmartDim && !toolUV_.empty()) {
        toolUV_.pop_back();
        if (!toolSnaps_.empty()) toolSnaps_.pop_back();
        return;
    }
    st.docUndo();
    resyncMirror(st);
    // Does the sketch still exist? Re-solving an absent sketch returns an error envelope.
    nlohmann::json res;
    if (!callSketch(st, "sketch_solve", nlohmann::json::object(), res)) {
        st.toast(ui::ToastLevel::Info, "Sketch removed by undo — closing");
        // The sketch feature is gone; drop out cleanly without re-exiting the worker sketch.
        active_ = false; isNewEmpty_ = false; fid_.clear();
        ents_.clear(); cons_.clear(); autoPlace_.clear(); cancelCheckpoint_.clear();
        status_.clear(); dof_ = 0;
        // C38 (S2): clear plane-pick state in the emergency teardown too.
        planePicking_ = false; planeHover_ = -1; st.sketchPlanePickActive = false;
        resetTool();
        if (havePrevCam_) { requestCameraAnimation(st, prevYaw_, prevPitch_, prevZoom_, prevPan_); havePrevCam_ = false; }
        return;
    }
    applyEnterResult(res);
}

// ---------------------------------------------------------------------------
// C40: ghost rendering of inactive sketches
// ---------------------------------------------------------------------------

void SketchMode::refreshGhosts(AppState& st) {
    const std::string docId = st.docMirror.docId();
    const int rev = st.docMirror.revision();
    // C48: the in-flight create-mode PM preview (Extrude/Revolve...) consumes its profile
    // sketch in the doc, but the curves must STAY visible until commit (SolidWorks shows the
    // profile through the preview). A change of the exempt id also forces a refresh.
    std::string exemptFid;
    if (st.propertyManager) exemptFid = st.propertyManager->previewFeatureId();
    if (docId == ghostsDocId_ && rev == ghostsRevision_ && exemptFid == ghostsExemptFid_) return;
    ghostsDocId_ = docId;
    ghostsRevision_ = rev;
    ghostsExemptFid_ = exemptFid;
    ++ghostsGen_;
    // C48: keep already-loaded snapshots across the refresh — during PM preview churn the
    // revision bumps faster than the async overview fetch completes, and clearing here made
    // every unconsumed sketch flicker out (or vanish for the whole preview session). Stale
    // coords draw until the fresh fetch lands.
    std::vector<GhostSketch> old;
    old.swap(ghosts_);
    const nlohmann::json& tree = st.docMirror.tree();
    if (!tree.is_object() || !tree.contains("features") || !tree["features"].is_array()) return;
    const auto& feats = tree["features"];
    // Per-sketch consumer scan: a sketch is consumed when a DIFFERENT feature references it
    // ("sketch":"<fid>" inside a region selector) AND that consumer is actually built —
    // sketches referenced by an errored or rolled-back feature still ghost (the user needs to
    // see them to fix things), as does the PM's exempt preview feature.
    const nlohmann::json& statusArr = st.docMirror.featureStatus();
    auto consumerCounts = [&](const std::string& fid) -> bool {
        const std::string needle = "\"sketch\":\"" + fid + "\"";
        for (const auto& f : feats) {
            if (!f.is_object() || f.value("type", std::string()) == "sketch") continue;
            const std::string cid = f.value("id", std::string());
            if (cid.empty() || cid == exemptFid) continue;
            if (!f.contains("refs") || f["refs"].dump().find(needle) == std::string::npos)
                continue;
            // Consumer found — does it actually build? (missing status record = assume ok)
            bool ok = true, rolledBack = false;
            if (statusArr.is_array()) {
                for (const auto& s : statusArr) {
                    if (!s.is_object() || s.value("id", std::string()) != cid) continue;
                    ok = s.value("ok", true);
                    rolledBack = s.value("rolled_back", false);
                    break;
                }
            }
            if (ok && !rolledBack) return true;
        }
        return false;
    };
    for (const auto& f : feats) {
        if (!f.is_object() || f.value("type", std::string()) != "sketch") continue;
        if (f.value("suppressed", false)) continue;
        const std::string fid = f.value("id", std::string());
        if (fid.empty()) continue;
        if (!f.contains("params") || !f["params"].is_object()) continue;
        const auto& ents = f["params"].value("entities", nlohmann::json::array());
        if (!ents.is_array() || ents.empty()) continue;
        if (consumerCounts(fid)) continue;
        ghosts_.push_back(GhostSketch{});
        ghosts_.back().fid = fid;
        // Carry over the previous snapshot (stale-but-visible) while the refresh is in flight.
        for (auto& o : old) {
            if (o.fid != fid || !o.loaded) continue;
            std::memcpy(ghosts_.back().frame, o.frame, sizeof(o.frame));
            ghosts_.back().ents = std::move(o.ents);
            ghosts_.back().loaded = true;
            break;
        }
        // Fetch the solved coords + plane frame asynchronously; the callback runs on the GL
        // thread (AsyncRpc::drain) and is dropped if the doc moved on (gen guard) — harmless
        // now, since the carried-over snapshot keeps drawing until a fresh fetch lands.
        const int gen = ghostsGen_;
        st.rpc.call("doc_sketch_overview", {{"doc_id", docId}, {"sketch", fid}},
                    [this, gen, fid](const core::RpcResult& r) {
                        if (gen != ghostsGen_) return;
                        if (!r.ok || !r.result.is_object() || r.result.contains("code")) return;
                        for (auto& g : ghosts_) {
                            if (g.fid != fid) continue;
                            if (r.result.contains("plane_frame") &&
                                r.result["plane_frame"].is_array() &&
                                r.result["plane_frame"].size() == 16)
                                for (int i = 0; i < 16; ++i)
                                    g.frame[i] = r.result["plane_frame"][i].get<float>();
                            parseEntities(r.result, g.ents);
                            g.loaded = true;
                        }
                    });
    }
}

void SketchMode::drawGhosts(AppState& st, const ImVec2& imgMin, const ImVec2& imgMax,
                            int vw, int vh) {
    if (!st.renderer || !st.workerStarted || !st.docMirror.hasDoc()) {
        if (!ghosts_.empty()) { ghosts_.clear(); ghostsDocId_.clear(); ghostsRevision_ = -999; }
        return;
    }
    refreshGhosts(st);
    if (ghosts_.empty()) return;
    const auto& cam = st.renderer->camera();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->PushClipRect(imgMin, imgMax, true);
    const auto& pal = ui::palette;
    for (const auto& g : ghosts_) {
        if (!g.loaded) continue;
        if (active_ && g.fid == fid_) continue;   // the live overlay owns the active sketch
        // Project sketch (u,v) through this ghost's own plane frame (no shared basis).
        auto uv2s = [&](float u, float v, ImVec2& o) -> bool {
            float wx = g.frame[0] * u + g.frame[1] * v + g.frame[3];
            float wy = g.frame[4] * u + g.frame[5] * v + g.frame[7];
            float wz = g.frame[8] * u + g.frame[9] * v + g.frame[11];
            return worldToScreen(cam, imgMin, vw, vh, wx, wy, wz, o);
        };
        auto poly = [&](auto pointAt, int seg) {     // sample pointAt(t) for t in [0,1]
            ImVec2 prev; bool havePrev = false;
            for (int i = 0; i <= seg; ++i) {
                float u, v; pointAt(i / (float)seg, u, v);
                ImVec2 p;
                if (!uv2s(u, v, p)) { havePrev = false; continue; }
                if (havePrev)
                    dl->AddLine(prev, p, col(pal.sketchInk, g.fid == fid_ ? 0.9f : 0.55f), 1.5f);
                prev = p; havePrev = true;
            }
        };
        const float D2R = 3.14159265f / 180.0f;
        for (const auto& e : g.ents) {
            if (e.construction) continue;   // ghost view: skip construction geometry
            if (e.type == "line" || e.type == "slot") {
                poly([&](float t, float& u, float& v) {
                    u = e.p1[0] + (e.p2[0] - e.p1[0]) * t;
                    v = e.p1[1] + (e.p2[1] - e.p1[1]) * t;
                }, 1);
                if (e.type == "slot" && e.width > 0) {
                    for (const float* end : {e.p1, e.p2})
                        poly([&](float t, float& u, float& v) {
                            float a = t * 6.2831853f;
                            u = end[0] + e.width * 0.5f * std::cos(a);
                            v = end[1] + e.width * 0.5f * std::sin(a);
                        }, 24);
                }
            } else if (e.type == "circle") {
                poly([&](float t, float& u, float& v) {
                    float a = t * 6.2831853f;
                    u = e.center[0] + e.radius * std::cos(a);
                    v = e.center[1] + e.radius * std::sin(a);
                }, 48);
            } else if (e.type == "arc" || e.type == "arc_slot") {
                float a0 = e.start, a1 = e.end;
                if (e.type == "arc" && !e.ccw) { float t = a0; a0 = a1; a1 = t; }
                if (a1 < a0) a1 += 360.0f;
                auto arcAt = [&](float rad, float t, float& u, float& v) {
                    float a = (a0 + (a1 - a0) * t) * D2R;
                    u = e.center[0] + rad * std::cos(a);
                    v = e.center[1] + rad * std::sin(a);
                };
                if (e.type == "arc") {
                    poly([&](float t, float& u, float& v) { arcAt(e.radius, t, u, v); }, 40);
                } else {
                    float hw = e.width * 0.5f;
                    poly([&](float t, float& u, float& v) { arcAt(e.radius + hw, t, u, v); }, 40);
                    poly([&](float t, float& u, float& v) { arcAt(e.radius - hw, t, u, v); }, 40);
                    for (float aDeg : {e.start, e.end}) {
                        float sa = aDeg * D2R;
                        float cx = e.center[0] + e.radius * std::cos(sa);
                        float cy = e.center[1] + e.radius * std::sin(sa);
                        poly([&](float t, float& u, float& v) {
                            float a = t * 6.2831853f;
                            u = cx + hw * std::cos(a);
                            v = cy + hw * std::sin(a);
                        }, 24);
                    }
                }
            } else if (e.type == "ellipse") {
                // C42: rotated parametric sample (rot in degrees).
                float ca = std::cos(e.rot * D2R), sa = std::sin(e.rot * D2R);
                poly([&](float t, float& u, float& v) {
                    float a = t * 6.2831853f;
                    float lx = e.rx * std::cos(a), ly = e.ry * std::sin(a);
                    u = e.center[0] + lx * ca - ly * sa;
                    v = e.center[1] + lx * sa + ly * ca;
                }, 48);
            } else if (e.type == "spline" && e.pts.size() >= 2) {
                // C42: polyline through the solved through-points.
                for (size_t i = 1; i < e.pts.size(); ++i) {
                    ImVec2 a, b;
                    if (uv2s(e.pts[i - 1].x, e.pts[i - 1].y, a) && uv2s(e.pts[i].x, e.pts[i].y, b))
                        dl->AddLine(a, b, col(pal.sketchInk, 0.55f), 1.5f);
                }
            } else if (e.type == "point") {
                // C42: small ghost cross.
                ImVec2 p;
                if (uv2s(e.p1[0], e.p1[1], p)) {
                    const ImU32 gc = col(pal.sketchInk, 0.55f);
                    dl->AddLine(ImVec2(p.x - 4, p.y), ImVec2(p.x + 4, p.y), gc, 1.5f);
                    dl->AddLine(ImVec2(p.x, p.y - 4), ImVec2(p.x, p.y + 4), gc, 1.5f);
                }
            }
            // conic-family ghosts: not yet sampled (rare as standalone profiles).
        }
    }
    dl->PopClipRect();
}

// ---------------------------------------------------------------------------
// overlay draw + interaction
// ---------------------------------------------------------------------------

void SketchMode::drawOverlay(AppState& st, ImGuiIO& io, const ImVec2& imgMin, const ImVec2& imgMax,
                             int vw, int vh, bool imgHovered) {
    if (!st.renderer) return;
    const auto& pal = ui::palette;

    // ---- C37-D: SolidWorks plane-pick step (runs while !active_, before a sketch exists) ----
    // The viewport keeps calling drawOverlay (and force-shows the origin planes) while
    // st.sketchPlanePickActive is set; we ray-hit-test the three origin-plane quads + planar faces.
    if (planePicking_ && st.sketchPlanePickActive) {
        st.requestCursor(ui::CursorKind::Crosshair);
        const Renderer& r = *st.renderer;
        const auto& cam = r.camera();
        ImDrawList* wdl = ImGui::GetWindowDrawList();
        wdl->PushClipRect(imgMin, imgMax, true);

        // Project each origin-plane quad (front=0/top=1/right=2) — geometry mirrors renderer.cpp's
        // planeCorners (half-extent = sceneR*1.25). Hover-tint the quad whose projected polygon
        // contains the cursor; click -> enter that datum plane.
        const float e = (r.modelRadius() > 0.0f ? r.modelRadius() : 35.0f) * 1.25f;
        const char* planeSel[3] = {"plane.front", "plane.top", "plane.right"};
        // C37-D: mirror renderer.cpp planeCorners (private there) — center = bbox center (camera
        // .center), half-extent e, uv {-1,-1},{1,-1},{1,1},{-1,1}; plane 0=Front(XY) 1=Top(XZ)
        // 2=Right(YZ). DEVIATION: replicated geometry because Renderer::planeCorners is private.
        const Vec3 ctr = cam.center;
        auto planeCorner = [&](int plane, int i, float& wx, float& wy, float& wz) {
            static const float uv[4][2] = {{-1,-1}, {1,-1}, {1,1}, {-1,1}};
            float u = uv[i][0] * e, v = uv[i][1] * e;
            if (plane == 0)      { wx = ctr.x + u; wy = ctr.y + v; wz = ctr.z; }       // XY
            else if (plane == 1) { wx = ctr.x + u; wy = ctr.y;     wz = ctr.z + v; }   // XZ
            else                 { wx = ctr.x;     wy = ctr.y + u; wz = ctr.z + v; }   // YZ
        };
        int hovered = -1;
        ImVec2 quadS[3][4];
        bool quadVis[3] = {false, false, false};
        for (int p = 0; p < 3; ++p) {
            bool ok = true;
            for (int i = 0; i < 4; ++i) {
                float wx, wy, wz; planeCorner(p, i, wx, wy, wz);
                if (!worldToScreen(cam, imgMin, vw, vh, wx, wy, wz, quadS[p][i])) { ok = false; break; }
            }
            quadVis[p] = ok;
            if (!ok) continue;
            // point-in-quad (two triangles) for the cursor
            auto inTri = [](ImVec2 P, ImVec2 a, ImVec2 b, ImVec2 cc) {
                auto s = [](ImVec2 p, ImVec2 q, ImVec2 r2) {
                    return (p.x - r2.x) * (q.y - r2.y) - (q.x - r2.x) * (p.y - r2.y);
                };
                float d1 = s(P, a, b), d2 = s(P, b, cc), d3 = s(P, cc, a);
                bool neg = (d1 < 0) || (d2 < 0) || (d3 < 0);
                bool pos = (d1 > 0) || (d2 > 0) || (d3 > 0);
                return !(neg && pos);
            };
            if (imgHovered &&
                (inTri(io.MousePos, quadS[p][0], quadS[p][1], quadS[p][2]) ||
                 inTri(io.MousePos, quadS[p][0], quadS[p][2], quadS[p][3])))
                hovered = p;
        }
        planeHover_ = hovered;
        for (int p = 0; p < 3; ++p) {
            if (!quadVis[p]) continue;
            ImU32 fill = col(pal.accent, p == hovered ? 0.28f : 0.10f);
            ImU32 line = col(pal.accent, p == hovered ? 0.95f : 0.45f);
            wdl->AddQuadFilled(quadS[p][0], quadS[p][1], quadS[p][2], quadS[p][3], fill);
            wdl->AddQuad(quadS[p][0], quadS[p][1], quadS[p][2], quadS[p][3], line, 2.0f);
        }
        // Click: an origin plane wins if hovered; else attempt the hovered planar face.
        if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
            if (hovered >= 0) {
                planePicking_ = false; st.sketchPlanePickActive = false;
                createSketchOnPlane(st, planeSel[hovered]);
            } else if (st.hover.valid() && st.hover.kind == render::PickKind::Face) {
                // Planar-face pick: resolve via the same path enterNewOnSelection uses. We try the
                // hovered face's selector; the engine rejects non-planar faces (toast + stay).
                std::string perr;
                std::string sel = st.docMirror.resolvePick(st.worker, st.hover.body_id, "face",
                                                           st.hover.topo_id, perr);
                if (!sel.empty()) {
                    nlohmann::json js;
                    try { js = nlohmann::json::parse(sel); } catch (...) {}
                    if (js.is_object()) {
                        std::string newFid = createSketchOnPlane(st, js);
                        if (newFid.empty())
                            st.toast(ui::ToastLevel::Warn, "That face can't host a sketch — pick a planar face or plane");
                        else { planePicking_ = false; st.sketchPlanePickActive = false; }
                    }
                }
            }
        }
        // Top-center banner.
        const char* banner = "Select a plane or a planar face for the sketch \xE2\x80\x94 Esc to cancel";
        ImVec2 bts = ImGui::CalcTextSize(banner);
        float bcx = (imgMin.x + imgMax.x) * 0.5f;
        // Sit BELOW the heads-up view toolbar (centered pill at imgMin.y+8, ~34px tall) so the
        // two don't overlap (the HUD stays visible during plane-pick).
        ImVec2 b0(bcx - bts.x * 0.5f - 14, imgMin.y + 52.0f * ui::uiScale);
        ImVec2 b1(bcx + bts.x * 0.5f + 14, b0.y + bts.y + 12);
        wdl->AddRectFilled(b0, b1, col(pal.overlayBg), 6.0f);
        wdl->AddRect(b0, b1, col(pal.overlayBorder), 6.0f);
        wdl->AddText(ImVec2(b0.x + 14, b0.y + 6), col(pal.overlayText), banner);
        wdl->PopClipRect();
        // Esc cancels the whole thing.
        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
            planePicking_ = false; st.sketchPlanePickActive = false;
            st.toast(ui::ToastLevel::Info, "Sketch cancelled");
        }
        return;
    }

    if (!active_) return;
    buildBasis(*st.renderer, imgMin, vw, vh);
    if (!basisOk_) return;

    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->PushClipRect(imgMin, imgMax, true);

    // C37-D: crosshair cursor whenever a drawing tool or Smart Dimension is active.
    if (tool_ != SketchTool::None) st.requestCursor(ui::CursorKind::Crosshair);

    // C37-D: the confirmation corner (top-right) sits over the viewport image, so its rects steal
    // hover from the tools. Compute them here and treat clicks there as NOT hovering the canvas, so a
    // tool/dim click on ✓/✗ doesn't also place a point. (Geometry mirrors the corner block below.)
    {
        const float sz = 30.0f, pad = 12.0f;
        ImVec2 okMin(imgMax.x - pad - sz, imgMin.y + pad), okMax(okMin.x + sz, okMin.y + sz);
        ImVec2 cxMin(okMin.x, okMax.y + 6), cxMax(cxMin.x + sz, cxMin.y + sz);
        if (imgHovered && (ImGui::IsMouseHoveringRect(okMin, okMax) ||
                           ImGui::IsMouseHoveringRect(cxMin, cxMax)))
            imgHovered = false;
    }

    // C37-D: in-sketch Ctrl+Z (the global app handler skips Ctrl+Z while sketch is active). Mid-
    // stroke pops the last point; otherwise doc undo + re-solve. Suppressed while a text field
    // (the Modify box) owns the keyboard. Returns early if the sketch vanished from the undo.
    if (!io.WantTextInput && io.KeyCtrl && ImGui::IsKeyPressed(ImGuiKey_Z, false)) {
        inSketchUndo(st);
        if (!active_) { dl->PopClipRect(); return; }
    }

    // C41 (T1): Esc consumption flag. The specific Esc behaviors that KEEP the tool armed
    // (Smart Dim pick-buffer clear, Offset stage back-out, Modify box close) set this so the
    // hover-independent catch-all at the end of drawOverlay doesn't also disarm the tool.
    bool escHandled = false;

    // ---- faint grid (lines along sketch u/v through the origin neighbourhood) ----
    const ImU32 gridCol = col(pal.sketchGrid);
    const ImU32 axisCol = col(pal.sketchGrid, 2.2f);
    {
        // Estimate the visible (u,v) span from the four image corners.
        float umin = 1e9f, umax = -1e9f, vmin = 1e9f, vmax = -1e9f;
        ImVec2 corners[4] = {imgMin, ImVec2(imgMax.x, imgMin.y), ImVec2(imgMin.x, imgMax.y), imgMax};
        for (auto& c : corners) {
            float u, v; screenToUV(c, u, v);
            umin = std::fmin(umin, u); umax = std::fmax(umax, u);
            vmin = std::fmin(vmin, v); vmax = std::fmax(vmax, v);
        }
        // Clamp the line count so a zoomed-out view doesn't draw thousands of lines.
        int count = 0;
        float gu0 = std::floor(umin / kGridStep) * kGridStep;
        for (float u = gu0; u <= umax && count < 400; u += kGridStep, ++count) {
            ImVec2 a = uvToScreen(u, vmin), b = uvToScreen(u, vmax);
            dl->AddLine(a, b, std::fabs(u) < 1e-3f ? axisCol : gridCol, std::fabs(u) < 1e-3f ? 1.4f : 1.0f);
        }
        count = 0;
        float gv0 = std::floor(vmin / kGridStep) * kGridStep;
        for (float v = gv0; v <= vmax && count < 400; v += kGridStep, ++count) {
            ImVec2 a = uvToScreen(umin, v), b = uvToScreen(umax, v);
            dl->AddLine(a, b, std::fabs(v) < 1e-3f ? axisCol : gridCol, std::fabs(v) < 1e-3f ? 1.4f : 1.0f);
        }
    }

    // ---- entities (color by solver status via theme roles) ----
    auto statusCol = [&](const SketchEntity& e) -> ImU32 {
        if (e.status == "over") return col(pal.statusErr);
        if (e.status == "full") return col(pal.sketchInk);
        return col(pal.sketchUnder);   // under-defined (blue-ish)
    };
    for (const auto& e : ents_) {
        // C41 (T2): selected entities render in the accent color, slightly thicker.
        bool sel = !selIds_.empty() && isSelected(e.id);
        ImU32 cc = sel ? col(pal.accent) : statusCol(e);
        float th = (e.construction ? 1.0f : 2.0f) + (sel ? 1.0f : 0.0f);
        if (e.type == "line") {
            ImVec2 a = uvToScreen(e.p1[0], e.p1[1]), b = uvToScreen(e.p2[0], e.p2[1]);
            dl->AddLine(a, b, cc, th);
            dl->AddCircleFilled(a, 3.0f, cc);
            dl->AddCircleFilled(b, 3.0f, cc);
        } else if (e.type == "circle") {
            // Sample the circle in (u,v) so it follows the plane mapping exactly.
            ImVec2 prev; int seg = 64;
            for (int i = 0; i <= seg; ++i) {
                float a = i / (float)seg * 6.2831853f;
                ImVec2 p = uvToScreen(e.center[0] + e.radius * std::cos(a),
                                      e.center[1] + e.radius * std::sin(a));
                if (i) dl->AddLine(prev, p, cc, th);
                prev = p;
            }
            dl->AddCircleFilled(uvToScreen(e.center[0], e.center[1]), 2.5f, cc);
        } else if (e.type == "arc") {
            float a0 = e.start, a1 = e.end;
            if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
            if (a1 < a0) a1 += 360.0f;
            ImVec2 prev; int seg = 48;
            for (int i = 0; i <= seg; ++i) {
                float a = (a0 + (a1 - a0) * (i / (float)seg)) * 3.14159265f / 180.0f;
                ImVec2 p = uvToScreen(e.center[0] + e.radius * std::cos(a),
                                      e.center[1] + e.radius * std::sin(a));
                if (i) dl->AddLine(prev, p, cc, th);
                prev = p;
            }
        } else if (e.type == "slot") {
            ImVec2 a = uvToScreen(e.p1[0], e.p1[1]), b = uvToScreen(e.p2[0], e.p2[1]);
            dl->AddLine(a, b, cc, th);
            // approximate slot caps as circles at each end (radius = width/2 projected to px)
            ImVec2 wEnd = uvToScreen(e.p1[0] + e.width * 0.5f, e.p1[1]);
            float rpx = std::hypot(wEnd.x - a.x, wEnd.y - a.y);
            dl->AddCircle(a, rpx, cc, 24, th);
            dl->AddCircle(b, rpx, cc, 24, th);
        } else if (e.type == "arc_slot") {
            // Capsule outline around a circular spine (center, radius; CCW a0->a1 in start/end).
            // Outer arc (r+hw), inner arc (r-hw), and a half-circle cap at each spine endpoint.
            const float D2R = 3.14159265f / 180.0f;
            float hw = e.width * 0.5f;
            float a0 = e.start, a1 = e.end; if (a1 < a0) a1 += 360.0f;
            auto arcPoly = [&](float rad) {
                ImVec2 prev; int seg = 64;
                for (int i = 0; i <= seg; ++i) {
                    float a = (a0 + (a1 - a0) * (i / (float)seg)) * D2R;
                    ImVec2 p = uvToScreen(e.center[0] + rad * std::cos(a),
                                          e.center[1] + rad * std::sin(a));
                    if (i) dl->AddLine(prev, p, cc, th); prev = p;
                }
            };
            arcPoly(e.radius + hw);
            arcPoly(e.radius - hw);
            // End-cap half-circles centered on the spine endpoints, radius hw.
            auto cap = [&](float aDeg) {
                float sa = aDeg * D2R;
                float sx = e.center[0] + e.radius * std::cos(sa);
                float sy = e.center[1] + e.radius * std::sin(sa);
                ImVec2 prev; int seg = 24;
                for (int i = 0; i <= seg; ++i) {
                    float t = (aDeg - 90.0f + 180.0f * (i / (float)seg)) * D2R;
                    ImVec2 p = uvToScreen(sx + hw * std::cos(t), sy + hw * std::sin(t));
                    if (i) dl->AddLine(prev, p, cc, th); prev = p;
                }
            };
            cap(e.start); cap(e.end);
        } else if (e.type == "point") {
            // C42: dot + small cross so a lone point reads as geometry, not a stray handle.
            ImVec2 p = uvToScreen(e.p1[0], e.p1[1]);
            dl->AddCircleFilled(p, 3.0f, cc);
            dl->AddLine(ImVec2(p.x - 6, p.y), ImVec2(p.x + 6, p.y), cc, 1.2f);
            dl->AddLine(ImVec2(p.x, p.y - 6), ImVec2(p.x, p.y + 6), cc, 1.2f);
        } else if (e.type == "ellipse") {
            // C42: parametric sample with the rotation applied (rot stored in degrees).
            const float D2R = 3.14159265f / 180.0f;
            float ca = std::cos(e.rot * D2R), sa = std::sin(e.rot * D2R);
            ImVec2 prev; int seg = 64;
            for (int i = 0; i <= seg; ++i) {
                float a = i / (float)seg * 6.2831853f;
                float lx = e.rx * std::cos(a), ly = e.ry * std::sin(a);
                ImVec2 p = uvToScreen(e.center[0] + lx * ca - ly * sa,
                                      e.center[1] + lx * sa + ly * ca);
                if (i) dl->AddLine(prev, p, cc, th);
                prev = p;
            }
            dl->AddCircleFilled(uvToScreen(e.center[0], e.center[1]), 2.5f, cc);
        } else if (e.type == "spline") {
            // C42: polyline through the solved through-points + point markers.
            for (size_t i = 1; i < e.pts.size(); ++i)
                dl->AddLine(uvToScreen(e.pts[i - 1].x, e.pts[i - 1].y),
                            uvToScreen(e.pts[i].x, e.pts[i].y), cc, th);
            for (const auto& q : e.pts) dl->AddCircleFilled(uvToScreen(q.x, q.y), 2.5f, cc);
        }
    }

    // ---- C37-D: dimension annotations + relation glyphs (always rendered) ----
    {
        const ImU32 dimLineCol = col(pal.sketchDimLine, 0.85f);
        const ImU32 dimTextCol = col(pal.sketchInk);
        const ImU32 dimDrivenCol = col(pal.consoleDim);     // dimmer for driven (reference) dims
        const ImU32 dimHoverCol = col(pal.accent);
        // 1) relation badges for geometric (non-dimensional) constraints.
        auto baseId = [](const std::string& h) { return h.substr(0, h.find('.')); };
        auto refScreen = [&](const std::string& h, ImVec2& s) -> bool {
            const SketchEntity* e = entById(baseId(h));
            if (!e) return false;
            if (h.find(".p1") != std::string::npos)      s = uvToScreen(e->p1[0], e->p1[1]);
            else if (h.find(".p2") != std::string::npos) s = uvToScreen(e->p2[0], e->p2[1]);
            else if (h.find(".center") != std::string::npos) s = uvToScreen(e->center[0], e->center[1]);
            else if (e->type == "line")  s = uvToScreen((e->p1[0]+e->p2[0])*0.5f, (e->p1[1]+e->p2[1])*0.5f);
            else                         s = uvToScreen(e->center[0], e->center[1]);
            return true;
        };
        // C41 (T3): SolidWorks-like relation badges — ~16px (ui::uiScale-scaled) rounded chips
        // with a light-green fill (palette.sketchRelBg), darker green border, and a DARK ASCII
        // glyph (plain ASCII on purpose: no missing-glyph boxes). Entity-level relations sit at
        // the entity midpoint pushed ~10px perpendicular; point-level ones at the point + a
        // small diagonal offset. Badges whose refs can't be resolved are skipped. Multiple
        // badges on the same ref stack horizontally.
        const float bs = 16.0f * ui::uiScale;                 // chip side
        const ImU32 relBg     = col(pal.sketchRelBg);
        const ImU32 relBorder = col(pal.sketchRelBorder);
        const ImU32 relInk    = col(pal.sketchInk);
        std::unordered_map<std::string, int> badgeStack;      // per-anchor-cell stacking index
        // Chip-center anchor for a ref handle (entity id "e3" or point handle "e3.p2").
        auto badgeAnchor = [&](const std::string& h, ImVec2& out) -> bool {
            const SketchEntity* e = entById(baseId(h));
            if (!e) return false;
            const float off = 10.0f * ui::uiScale + bs * 0.5f;   // clear the geometry line
            if (h.find('.') != std::string::npos) {              // point-level: point + offset
                ImVec2 s;
                if (!refScreen(h, s)) return false;
                out = ImVec2(s.x + off * 0.8f, s.y - off * 0.8f);
                return true;
            }
            if (e->type == "line" || e->type == "slot") {        // midpoint + unit perpendicular
                ImVec2 a = uvToScreen(e->p1[0], e->p1[1]), b = uvToScreen(e->p2[0], e->p2[1]);
                ImVec2 m((a.x + b.x) * 0.5f, (a.y + b.y) * 0.5f);
                float dx = b.x - a.x, dy = b.y - a.y; float L = std::hypot(dx, dy);
                float nx = L > 1e-3f ? -dy / L : 0.0f, ny = L > 1e-3f ? dx / L : -1.0f;
                out = ImVec2(m.x + nx * off, m.y + ny * off);
                return true;
            }
            if (e->type == "circle" || e->type == "arc" || e->type == "arc_slot") {
                // rim point (45° for circles, mid-sweep for arcs), pushed radially outward
                float aMid = 45.0f;
                if (e->type != "circle") {
                    float a0 = e->start, a1 = e->end; if (a1 < a0) a1 += 360.0f;
                    aMid = (a0 + a1) * 0.5f;
                }
                float ar = aMid * 3.14159265f / 180.0f;
                ImVec2 cc = uvToScreen(e->center[0], e->center[1]);
                ImVec2 rim = uvToScreen(e->center[0] + e->radius * std::cos(ar),
                                        e->center[1] + e->radius * std::sin(ar));
                float dx = rim.x - cc.x, dy = rim.y - cc.y; float L = std::hypot(dx, dy);
                if (L > 1e-3f) out = ImVec2(rim.x + dx / L * off, rim.y + dy / L * off);
                else           out = ImVec2(cc.x + off, cc.y - off);
                return true;
            }
            ImVec2 s;                                            // point entity / fallback
            if (!refScreen(h, s)) return false;
            out = ImVec2(s.x + off * 0.8f, s.y - off * 0.8f);
            return true;
        };
        auto drawBadge = [&](const std::string& h, const char* glyph, const char* name) {
            ImVec2 gp;
            if (!badgeAnchor(h, gp)) return;
            // Stack badges landing in the same screen cell along +x (covers both multiple
            // relations on one ref AND distinct refs whose anchors coincide, e.g. a fix and a
            // coincident on the same corner point).
            char cell[40];
            std::snprintf(cell, sizeof(cell), "%d:%d",
                          (int)std::floor(gp.x / bs), (int)std::floor(gp.y / bs));
            gp.x += (float)badgeStack[cell]++ * (bs + 2.0f);
            ImVec2 c0(gp.x - bs * 0.5f, gp.y - bs * 0.5f), c1(gp.x + bs * 0.5f, gp.y + bs * 0.5f);
            dl->AddRectFilled(c0, c1, relBg, 3.5f * ui::uiScale);
            dl->AddRect(c0, c1, relBorder, 3.5f * ui::uiScale, 0, 1.2f);
            ImVec2 gs = ImGui::CalcTextSize(glyph);
            dl->AddText(ImVec2(gp.x - gs.x * 0.5f, gp.y - gs.y * 0.5f), relInk, glyph);
            // Hover tooltip naming the relation type.
            if (imgHovered && io.MousePos.x >= c0.x && io.MousePos.x <= c1.x &&
                io.MousePos.y >= c0.y && io.MousePos.y <= c1.y)
                ImGui::SetTooltip("%s", name);
        };
        for (const auto& c : cons_) {
            if (c.isDimensional() || c.refs.empty()) continue;
            const char* g = c.type == "horizontal"    ? "-"
                          : c.type == "vertical"      ? "|"
                          : c.type == "parallel"      ? "//"
                          : c.type == "perpendicular" ? "L"
                          : c.type == "equal"         ? "="
                          : c.type == "coincident"    ? "o"
                          : c.type == "concentric"    ? "c"
                          : c.type == "tangent"       ? "T"
                          : c.type == "fix"           ? "x" : nullptr;
            if (!g) continue;
            // Entity-pair relations get a badge on EACH ref (SolidWorks shows both sides);
            // single-entity / point relations get one badge at the first ref.
            bool pairRel = c.type == "parallel" || c.type == "perpendicular" ||
                           c.type == "equal" || c.type == "tangent" || c.type == "concentric";
            if (pairRel) { for (const auto& rh : c.refs) drawBadge(rh, g, c.type.c_str()); }
            else         drawBadge(c.refs[0], g, c.type.c_str());
        }
        // 2) dimension annotations. Hover-test the text anchor; track for drag / double-click / delete.
        std::string newHover;
        for (const auto& c : cons_) {
            if (!c.isDimensional()) continue;
            // placement: stored -> autoPlace_ cache -> default (persist into the cache lazily).
            float pu, pv;
            bool dragging = dimDragging_ && dimDragCid_ == c.id;
            if (dragging) { pu = dimDragPlace_.x; pv = dimDragPlace_.y; }
            else if (c.hasPlacement) { pu = c.placement[0]; pv = c.placement[1]; }
            else {
                auto it = autoPlace_.find(c.id);
                if (it != autoPlace_.end()) { pu = it->second.x; pv = it->second.y; }
                else if (defaultPlacement(c, pu, pv)) autoPlace_[c.id] = ImVec2(pu, pv);
                else continue;
            }
            ImVec2 anchor = uvToScreen(pu, pv);
            bool hov = imgHovered && std::hypot(io.MousePos.x - anchor.x, io.MousePos.y - anchor.y) < 22.0f;
            if (hov && newHover.empty()) newHover = c.id;
            ImU32 lc = hov ? dimHoverCol : dimLineCol;
            ImU32 tc = hov ? dimHoverCol : (c.driven ? dimDrivenCol : dimTextCol);
            drawDimension(dl, c, lc, tc, hov, pu, pv);
        }
        dimHoverCid_ = newHover;
    }

    // ---- selection / drag handles + interaction ----
    float mu = 0, mv = 0;
    if (imgHovered) screenToUV(io.MousePos, mu, mv);

    // ---- C37-D: dimension text drag + double-click-to-edit + Delete ----
    // C47: also active while Smart Dimension is armed (SolidWorks lets you drag/edit existing
    // dims mid-dimensioning); the SmartDim pick-click below skips text-hovered clicks.
    if ((tool_ == SketchTool::None || tool_ == SketchTool::SmartDim) && !dimPopupOpen_) {
        // Begin a drag on the hovered dim text. C38 (S4): the second click of a double-click also
        // fires IsMouseClicked; exclude it so a double-click-to-edit never spawns a phantom drag
        // (which would commit a stray sketch_set_dim placement on release).
        if (imgHovered && !dimDragging_ && !dragging_ && !dimHoverCid_.empty() &&
            ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
            !ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) {
            dimDragging_ = true; dimDragCid_ = dimHoverCid_; dimDragPlace_ = ImVec2(mu, mv);
        }
        if (dimDragging_) {
            dimDragPlace_ = ImVec2(mu, mv);
            if (ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
                // Persist the new placement via sketch_set_dim {placement}.
                nlohmann::json op = {{"op", "sketch_set_dim"}, {"sketch", fid_}, {"id", dimDragCid_},
                    {"placement", nlohmann::json::array({dimDragPlace_.x, dimDragPlace_.y})}};
                auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
                if (!r.ok) st.toast(ui::ToastLevel::Warn, "Move dimension: " + r.error_message);
                else { autoPlace_[dimDragCid_] = dimDragPlace_;
                       if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                       resolve(st); }
                dimDragging_ = false; dimDragCid_.clear();
            }
        }
        // Double-click a dim text -> reopen the Modify box prefilled.
        if (imgHovered && !dimHoverCid_.empty() && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) {
            // C38 (S4): if a drag somehow began on the first click of this double-click, cancel it so
            // its release-handler can't also fire a placement op behind the edit box.
            dimDragging_ = false; dimDragCid_.clear();
            for (const auto& c : cons_) {
                if (c.id != dimHoverCid_) continue;
                dimEditCid_ = c.id; dimType_ = c.type; dimIsAngle_ = (c.type == "angle");
                dimMeasured_ = c.hasMeasured ? c.measured : (c.hasValue ? c.value : 0.0);
                dimPopupDriven_ = c.driven;
                if (c.hasExpr) std::snprintf(dimBuf_.data(), dimBuf_.size(), "=%s", c.expr.c_str());
                else std::snprintf(dimBuf_.data(), dimBuf_.size(), "%s", fmtLen(dimMeasured_).c_str());
                dimError_.clear(); dimPopupOpen_ = true; dimPopupFocus_ = true; dimPopupPos_ = io.MousePos;
                break;
            }
        }
        // C41 (T2): Delete/Backspace removes the selected entities (one ops array for all of
        // them, then re-solve). A hovered dimension text takes priority (handler below) — keep
        // this gated on dimHoverCid_.empty() and BEFORE that handler (it clears the hover id).
        if (!selIds_.empty() && dimHoverCid_.empty() && !io.WantTextInput &&
            (ImGui::IsKeyPressed(ImGuiKey_Delete) || ImGui::IsKeyPressed(ImGuiKey_Backspace))) {
            nlohmann::json ops = nlohmann::json::array();
            for (const auto& id : selIds_)
                ops.push_back({{"op", "sketch_delete_entity"}, {"sketch", fid_}, {"id", id}});
            auto r = st.docMirror.applyOps(st.worker, ops);
            if (!r.ok) st.toast(ui::ToastLevel::Error, "Delete: " + r.error_message);
            else {
                if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                resolve(st);
            }
            selIds_.clear();
        }
        // Delete key removes the hovered dim.
        if (!dimHoverCid_.empty() && ImGui::IsKeyPressed(ImGuiKey_Delete)) {
            nlohmann::json op = {{"op", "sketch_delete_constraint"}, {"sketch", fid_}, {"id", dimHoverCid_}};
            auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
            if (!r.ok) st.toast(ui::ToastLevel::Error, "Delete dimension: " + r.error_message);
            else { if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                   autoPlace_.erase(dimHoverCid_); resolve(st); }
            dimHoverCid_.clear();
        }
    }

    // Drag (only when no drawing tool is active): hit-test handles, drag with sketch_drag, release
    // commits. At most one sketch_drag per frame (throttled by dragFrame_).
    if (tool_ == SketchTool::None) {
        // C37-D: a hovered/dragging dimension text claims the click (dim drag/edit above handles it).
        if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !dragging_ &&
            !dimDragging_ && dimHoverCid_.empty()) {
            std::string h; float hu, hv;
            if (hitTestHandle(io.MousePos, h, hu, hv) > 0) {
                dragging_ = true; dragHandle_ = h; dragFrame_ = -1;
            } else {
                // C41 (T2): no drag handle hit -> entity selection. Plain click selects the
                // entity under the cursor (or clears on empty space); Ctrl+click toggles it
                // in/out of the multi-selection (and leaves the rest alone on empty space).
                int ei = hitTestEntity(io.MousePos);
                if (ei >= 0) {
                    const std::string id = ents_[ei].id;
                    auto it = std::find(selIds_.begin(), selIds_.end(), id);
                    if (io.KeyCtrl) {
                        if (it != selIds_.end()) selIds_.erase(it);
                        else selIds_.push_back(id);
                    } else {
                        selIds_.clear();
                        selIds_.push_back(id);
                    }
                } else if (!io.KeyCtrl) {
                    selIds_.clear();
                }
            }
        }
        if (dragging_ && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
            int frame = ImGui::GetFrameCount();
            if (frame != dragFrame_ && imgHovered) {
                dragFrame_ = frame;
                nlohmann::json res;
                nlohmann::json extra = {{"handle", dragHandle_},
                                        {"xy", nlohmann::json::array({mu, mv})}};
                if (callSketch(st, "sketch_drag", extra, res)) applyEnterResult(res);
            }
        }
        if (dragging_ && ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
            dragging_ = false;
            commitDrag(st);
        }
        // Highlight handles on hover.
        std::string hh; float hu, hv;
        if (imgHovered && hitTestHandle(io.MousePos, hh, hu, hv) > 0)
            dl->AddCircle(uvToScreen(hu, hv), 6.0f, col(pal.hoverHighlight), 16, 2.0f);

        // C37-D: right-click with no tool active -> small context menu (gestures are off in-sketch,
        // so the RMB is free; consume it before anything else can).
        if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Right) && !dimPopupOpen_)
            ImGui::OpenPopup("##SketchCtx");
    }
    if (ImGui::BeginPopup("##SketchCtx")) {
        if (ImGui::MenuItem("Select")) resetTool();
        if (ImGui::MenuItem("Smart Dimension")) { resetTool(); tool_ = SketchTool::SmartDim; }
        ImGui::Separator();
        if (ImGui::MenuItem("Exit Sketch")) { ImGui::CloseCurrentPopup(); ImGui::EndPopup();
                                              exitSketch(st, /*keep=*/true); dl->PopClipRect(); return; }
        ImGui::EndPopup();
    }

    // ---- C29-B sketch-editing tools (Trim / Extend / Offset) ----
    // These have bespoke pick semantics (whole-entity hover + ops), so they run BEFORE the generic
    // drawing-tool block and short-circuit it. Each refreshes the entity set via the ops response
    // path (loadDocMesh + resolve), exactly like sketch_add_entity; op errors are logged and the
    // tool stays active. Esc exits.
    bool editTool = (tool_ == SketchTool::Trim || tool_ == SketchTool::Extend ||
                     tool_ == SketchTool::Offset);
    if (editTool) {
        int hovered = imgHovered ? hitTestEntity(io.MousePos) : -1;

        // ----- TRIM: highlight the hovered entity in red; click sends sketch_trim at the uv. -----
        if (tool_ == SketchTool::Trim) {
            if (hovered >= 0) {
                // Whole-entity emphasis in red (honest approximation: we do NOT compute the exact
                // trimmed span client-side — the engine splits at intersections and returns the
                // surviving spans; we just flag the picked entity).
                const SketchEntity& e = ents_[hovered];
                const ImU32 rc = col(pal.statusErr, 0.95f);
                if (e.type == "line" || e.type == "slot") {
                    dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), rc, 3.5f);
                } else if (e.type == "circle" || e.type == "arc") {
                    float a0 = 0, a1 = 6.2831853f;
                    if (e.type == "arc") {
                        a0 = e.start * 3.14159265f / 180.0f; a1 = e.end * 3.14159265f / 180.0f;
                        if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
                        if (a1 < a0) a1 += 6.2831853f;
                    }
                    ImVec2 prev; int seg = 64;
                    for (int i = 0; i <= seg; ++i) {
                        float a = a0 + (a1 - a0) * (i / (float)seg);
                        ImVec2 p = uvToScreen(e.center[0] + e.radius * std::cos(a),
                                              e.center[1] + e.radius * std::sin(a));
                        if (i) dl->AddLine(prev, p, rc, 3.5f); prev = p;
                    }
                }
            }
            if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                const SketchEntity& e = ents_[hovered];
                if (e.type != "line" && e.type != "circle" && e.type != "arc") {
                    st.addLog("[sketch] Trim: unsupported entity type '" + e.type + "'");
                } else {
                    nlohmann::json op = {{"op", "sketch_trim"}, {"sketch", fid_},
                        {"entity", e.id}, {"point", nlohmann::json::array({mu, mv})}};
                    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
                    if (!r.ok) st.addLog("[sketch] trim failed: " + r.error_message);
                    else {
                        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                        resolve(st);   // refresh entities/statuses (same path as add_entity)
                    }
                }
            }
            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) cancelTool();
        }

        // ----- EXTEND: highlight the nearer end; click sends sketch_extend with start|end. -----
        else if (tool_ == SketchTool::Extend) {
            const char* whichEnd = nullptr;     // "start"|"end" for line/arc, by nearer endpoint
            ImVec2 endPx;
            if (hovered >= 0) {
                const SketchEntity& e = ents_[hovered];
                const ImU32 ac = col(pal.accent, 0.95f);
                // Faint accent over the whole entity, bright dot on the nearer pickable end.
                if (e.type == "line") {
                    ImVec2 a = uvToScreen(e.p1[0], e.p1[1]), b = uvToScreen(e.p2[0], e.p2[1]);
                    dl->AddLine(a, b, ac, 2.5f);
                    float da = std::hypot(a.x - io.MousePos.x, a.y - io.MousePos.y);
                    float db = std::hypot(b.x - io.MousePos.x, b.y - io.MousePos.y);
                    whichEnd = da < db ? "start" : "end"; endPx = da < db ? a : b;
                } else if (e.type == "arc") {
                    ImVec2 a = uvToScreen(e.center[0] + e.radius * std::cos(e.start * 3.14159265f / 180.0f),
                                          e.center[1] + e.radius * std::sin(e.start * 3.14159265f / 180.0f));
                    ImVec2 b = uvToScreen(e.center[0] + e.radius * std::cos(e.end * 3.14159265f / 180.0f),
                                          e.center[1] + e.radius * std::sin(e.end * 3.14159265f / 180.0f));
                    float da = std::hypot(a.x - io.MousePos.x, a.y - io.MousePos.y);
                    float db = std::hypot(b.x - io.MousePos.x, b.y - io.MousePos.y);
                    whichEnd = da < db ? "start" : "end"; endPx = da < db ? a : b;
                }
                if (whichEnd) dl->AddCircleFilled(endPx, 6.0f, ac);
            }
            if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                const SketchEntity& e = ents_[hovered];
                if (!whichEnd) {
                    st.addLog("[sketch] Extend: unsupported entity type '" + e.type + "'");
                } else {
                    nlohmann::json op = {{"op", "sketch_extend"}, {"sketch", fid_},
                        {"entity", e.id}, {"end", whichEnd}};
                    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
                    if (!r.ok) st.addLog("[sketch] extend failed: " + r.error_message);
                    else {
                        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                        resolve(st);
                    }
                }
            }
            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) cancelTool();
        }

        // ----- OFFSET: pick chain -> Enter/[Offset] chip -> type distance -> click side. -----
        else if (tool_ == SketchTool::Offset) {
            auto inChain = [&](const std::string& id) {
                return std::find(offsetChain_.begin(), offsetChain_.end(), id) != offsetChain_.end();
            };
            // Tint already-selected chain entities in accent.
            for (const auto& e : ents_) {
                if (!inChain(e.id)) continue;
                const ImU32 ac = col(pal.accent, 0.95f);
                if (e.type == "line" || e.type == "slot") {
                    dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), ac, 3.5f);
                } else if (e.type == "circle" || e.type == "arc") {
                    float a0 = 0, a1 = 6.2831853f;
                    if (e.type == "arc") {
                        a0 = e.start * 3.14159265f / 180.0f; a1 = e.end * 3.14159265f / 180.0f;
                        if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
                        if (a1 < a0) a1 += 6.2831853f;
                    }
                    ImVec2 prev; int seg = 64;
                    for (int i = 0; i <= seg; ++i) {
                        float a = a0 + (a1 - a0) * (i / (float)seg);
                        ImVec2 p = uvToScreen(e.center[0] + e.radius * std::cos(a),
                                              e.center[1] + e.radius * std::sin(a));
                        if (i) dl->AddLine(prev, p, ac, 3.5f); prev = p;
                    }
                }
            }
            if (offsetStage_ == 0) {
                // Hover preview (faint) over the would-be-added entity.
                if (hovered >= 0 && !inChain(ents_[hovered].id)) {
                    const SketchEntity& e = ents_[hovered];
                    const ImU32 hc = col(pal.hoverHighlight, 0.9f);
                    if (e.type == "line" || e.type == "slot")
                        dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), hc, 3.0f);
                    else if (e.type == "circle" || e.type == "arc")
                        dl->AddCircle(uvToScreen(e.center[0], e.center[1]),
                                      std::hypot(uvToScreen(e.center[0] + e.radius, e.center[1]).x -
                                                 uvToScreen(e.center[0], e.center[1]).x, 0.0f),
                                      hc, 48, 3.0f);
                }
                // Click toggles an entity in/out of the chain.
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                    const SketchEntity& e = ents_[hovered];
                    if (e.type != "line" && e.type != "circle" && e.type != "arc") {
                        st.addLog("[sketch] Offset: unsupported entity type '" + e.type + "'");
                    } else {
                        auto it = std::find(offsetChain_.begin(), offsetChain_.end(), e.id);
                        if (it != offsetChain_.end()) offsetChain_.erase(it);
                        else offsetChain_.push_back(e.id);
                    }
                }
                // Enter advances to the distance stage when the chain is non-empty.
                if (!offsetChain_.empty() && ImGui::IsKeyPressed(ImGuiKey_Enter)) {
                    offsetStage_ = 1; offsetFocusInput_ = true; offsetOverlayPos_ = io.MousePos;
                }
            } else if (offsetStage_ == 2) {
                // Side pick: the next click's uv is the side hint.
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                    nlohmann::json eids = nlohmann::json::array();
                    for (const auto& id : offsetChain_) eids.push_back(id);
                    nlohmann::json op = {{"op", "sketch_offset"}, {"sketch", fid_},
                        {"entities", eids}, {"distance", offsetDist_},
                        {"side", nlohmann::json::array({mu, mv})}};
                    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
                    if (!r.ok) st.addLog("[sketch] offset failed: " + r.error_message);
                    else {
                        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                        resolve(st);
                    }
                    // Reset for another offset (stay in the tool), keep the distance default.
                    offsetStage_ = 0; offsetChain_.clear();
                }
            }
            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
                escHandled = true;   // C41 (T1): stage back-out keeps the tool armed
                // Esc backs out one stage; from the chain stage it exits the tool.
                if (offsetStage_ == 2) { offsetStage_ = 1; offsetFocusInput_ = true; }
                else if (offsetStage_ == 1) offsetStage_ = 0;
                else cancelTool();
            }
        }
    }

    // ---- drawing tools: rubber-band preview + click handling ----
    if (!editTool && tool_ != SketchTool::None && tool_ != SketchTool::SmartDim) {
        // Snap the live cursor for preview + inference glyphs (C49: point snap + midpoint/rim
        // snap + alignment inference, see snapPoint).
        float su = mu, sv = mv; std::string snapH;
        SnapInfer infer;
        if (imgHovered) snapPoint(su, sv, snapH, &infer);

        // C49: direction inference for the Line tool's SECOND point only (polygon/spline legs
        // and the 3-pt arc chord deliberately stay free). Priority: point snap > tangent > h/v.
        bool hvH = false, hvV = false;       // live h/v chip + exact-direction force
        bool tanSnap = false;                // live tangent chip
        bool tanStartUpgrade = false;        // placement click upgrades toolSnaps_[0] rim->tan
        if (inferenceOn_ && imgHovered && tool_ == SketchTool::Line && toolUV_.size() == 1) {
            const ImVec2 a = toolUV_[0];
            const float D2R = 3.14159265f / 180.0f;
            if (snapHasPrefix(snapH, "rim:")) {
                // (a) END point on a circle/arc rim: if the segment is nearly tangent there,
                // pull it to the EXACT tangent contact seen from the fixed start point.
                const SketchEntity* ce = entById(snapH.substr(4));
                if (ce && (ce->type == "circle" || ce->type == "arc") && ce->radius > 1e-5f) {
                    float cx = ce->center[0], cy = ce->center[1], r = ce->radius;
                    float dx = a.x - cx, dy = a.y - cy;
                    float d = std::hypot(dx, dy);
                    float segx = su - a.x, segy = sv - a.y;
                    float segL = std::hypot(segx, segy);
                    if (d > r + 1e-4f && segL > 1e-5f) {       // start outside; tangent exists
                        // tangent direction at the current rim point = perp(rim - center)
                        float tx = -(sv - cy), ty = su - cx;
                        float tL = std::hypot(tx, ty);
                        float cosang = std::fabs(segx * tx + segy * ty) / (segL * tL);
                        if (std::acos(std::fmin(1.0f, cosang)) < kTangentDeg * D2R) {
                            // exact contacts: center angle alpha +/- beta, cos(beta) = r/d
                            float alpha = std::atan2(dy, dx);
                            float beta = std::acos(std::fmax(-1.0f, std::fmin(1.0f, r / d)));
                            float u1 = cx + r * std::cos(alpha + beta), v1 = cy + r * std::sin(alpha + beta);
                            float u2 = cx + r * std::cos(alpha - beta), v2 = cy + r * std::sin(alpha - beta);
                            bool first = std::hypot(u1 - su, v1 - sv) <= std::hypot(u2 - su, v2 - sv);
                            su = first ? u1 : u2; sv = first ? v1 : v2;
                            snapH = "tan:" + ce->id;
                            tanSnap = true;
                        }
                    }
                }
            } else if (snapH.empty()) {
                // (b) START point sits on a rim: keep the segment tangent at that contact.
                if (!toolSnaps_.empty() && (snapHasPrefix(toolSnaps_[0], "rim:") ||
                                            snapHasPrefix(toolSnaps_[0], "tan:"))) {
                    const SketchEntity* ce = entById(toolSnaps_[0].substr(4));
                    if (ce && (ce->type == "circle" || ce->type == "arc")) {
                        float tx = -(a.y - ce->center[1]), ty = a.x - ce->center[0];
                        float tL = std::hypot(tx, ty);
                        float segx = su - a.x, segy = sv - a.y;
                        float segL = std::hypot(segx, segy);
                        if (tL > 1e-5f && segL > 1e-5f) {
                            float cosang = std::fabs(segx * tx + segy * ty) / (segL * tL);
                            if (std::acos(std::fmin(1.0f, cosang)) < kTangentDeg * D2R) {
                                float s = (segx * tx + segy * ty) / (tL * tL);  // project on tangent
                                su = a.x + tx * s; sv = a.y + ty * s;
                                tanSnap = true; tanStartUpgrade = true;
                                infer.alignU = infer.alignV = false;  // direction owns the point now
                            }
                        }
                    }
                }
                // (c) plain h/v direction snap (kInferHVDeg) — skipped when tangent fired.
                if (!tanSnap) {
                    float du = su - a.x, dv = sv - a.y;
                    float ang = std::atan2(std::fabs(dv), std::fabs(du)) * 57.2957795f;
                    if (ang < kInferHVDeg && (du != 0.0f || dv != 0.0f)) {
                        // drop a conflicting v-alignment (h/v force wins its own axis; an
                        // alignment on the OTHER axis combines — SW-style intersection).
                        if (infer.alignV && infer.srcV.y != a.y) infer.alignV = false;
                        sv = a.y; hvH = true;
                    } else if (ang > 90.0f - kInferHVDeg) {
                        if (infer.alignU && infer.srcU.x != a.x) infer.alignU = false;
                        su = a.x; hvV = true;
                    }
                }
            }
        }
        ImVec2 sCur = uvToScreen(su, sv);
        const ImU32 prevCol = col(pal.accent, 0.9f);

        // Rubber band from the last placed point.
        if (!toolUV_.empty()) {
            ImVec2 a = uvToScreen(toolUV_.back().x, toolUV_.back().y);
            if (tool_ == SketchTool::Line || tool_ == SketchTool::Slot) {
                dl->AddLine(a, sCur, prevCol, 1.6f);
            } else if (tool_ == SketchTool::RectCorner) {
                ImVec2 p0 = a, p1 = sCur;
                ImVec2 c1 = uvToScreen(su, toolUV_.back().y);
                ImVec2 c2 = uvToScreen(toolUV_.back().x, sv);
                dl->AddLine(p0, c1, prevCol, 1.6f); dl->AddLine(c1, p1, prevCol, 1.6f);
                dl->AddLine(p1, c2, prevCol, 1.6f); dl->AddLine(c2, p0, prevCol, 1.6f);
            } else if (tool_ == SketchTool::Circle) {
                float rpx = std::hypot(sCur.x - a.x, sCur.y - a.y);
                dl->AddCircle(a, rpx, prevCol, 48, 1.6f);
            } else if (tool_ == SketchTool::ArcCenter) {
                float rpx = std::hypot(sCur.x - a.x, sCur.y - a.y);
                dl->AddCircle(a, rpx, prevCol, 48, 1.2f);
            }
        }

        // C28-B variant previews (drawn in the sketch under-defined color, matching the placed
        // entities while still in-tool). Each branch builds the live geometry from the prior clicks
        // in toolUV_ + the snapped cursor (su,sv).
        {
            const ImU32 uCol = col(pal.sketchUnder, 0.95f);
            auto pl = [&](const std::vector<ImVec2>& uv) {  // draw a (u,v) polyline
                for (size_t i = 1; i < uv.size(); ++i)
                    dl->AddLine(uvToScreen(uv[i - 1].x, uv[i - 1].y),
                                uvToScreen(uv[i].x, uv[i].y), uCol, 1.6f);
            };
            // Arc-slot capsule preview: outer/inner arcs (r+/-hw, a0..a1 deg CCW) + two end caps.
            auto drawCapsule = [&](float cu, float cv, float rad, float a0d, float a1d, float hw) {
                const float D2R = 3.14159265f / 180.0f;
                if (a1d < a0d) a1d += 360.0f;
                auto arcPoly = [&](float rr) {
                    ImVec2 prev;
                    for (int i = 0; i <= 64; ++i) {
                        float a = (a0d + (a1d - a0d) * (i / 64.0f)) * D2R;
                        ImVec2 q = uvToScreen(cu + rr * std::cos(a), cv + rr * std::sin(a));
                        if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                    }
                };
                arcPoly(rad + hw); arcPoly(rad - hw);
                auto cap = [&](float aDeg) {
                    float sx = cu + rad * std::cos(aDeg * D2R), sy = cv + rad * std::sin(aDeg * D2R);
                    ImVec2 prev;
                    for (int i = 0; i <= 24; ++i) {
                        float t = (aDeg - 90.0f + 180.0f * (i / 24.0f)) * D2R;
                        ImVec2 q = uvToScreen(sx + hw * std::cos(t), sy + hw * std::sin(t));
                        if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                    }
                };
                cap(a0d); cap(a1d);
            };
            const size_t n = toolUV_.size();
            if (tool_ == SketchTool::Rect3ptCorner && n >= 1) {
                ImVec2 A = toolUV_[0];
                ImVec2 B = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                if (n < 2) { dl->AddLine(uvToScreen(A.x, A.y), sCur, uCol, 1.6f); }
                else {
                    // edge A-B fixed; third click sets width (perp offset of cursor from A-B).
                    float ex = B.x - A.x, ey = B.y - A.y; float L = std::hypot(ex, ey);
                    float nx = L > 1e-6f ? -ey / L : 0, ny = L > 1e-6f ? ex / L : 0;
                    float w = (su - A.x) * nx + (sv - A.y) * ny;
                    ImVec2 D(A.x + nx * w, A.y + ny * w), C(B.x + nx * w, B.y + ny * w);
                    pl({A, B, C, D, A});
                }
            } else if (tool_ == SketchTool::Rect3ptCenter && n >= 1) {
                ImVec2 ctr = toolUV_[0];
                ImVec2 mid = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                if (n < 2) { dl->AddLine(uvToScreen(ctr.x, ctr.y), sCur, uCol, 1.6f); }
                else {
                    // ctr->mid is a half-edge direction (half-height); cursor sets half-width.
                    float hx = mid.x - ctr.x, hy = mid.y - ctr.y; float L = std::hypot(hx, hy);
                    float px = L > 1e-6f ? -hy / L : 0, py = L > 1e-6f ? hx / L : 0;
                    float hw = (su - ctr.x) * px + (sv - ctr.y) * py;
                    ImVec2 wv(px * hw, py * hw);
                    ImVec2 P0(ctr.x + hx + wv.x, ctr.y + hy + wv.y);
                    ImVec2 P1(ctr.x + hx - wv.x, ctr.y + hy - wv.y);
                    ImVec2 P2(ctr.x - hx - wv.x, ctr.y - hy - wv.y);
                    ImVec2 P3(ctr.x - hx + wv.x, ctr.y - hy + wv.y);
                    pl({P0, P1, P2, P3, P0});
                }
            } else if (tool_ == SketchTool::Parallelogram && n >= 1) {
                ImVec2 A = toolUV_[0];
                ImVec2 B = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                if (n < 2) { dl->AddLine(uvToScreen(A.x, A.y), sCur, uCol, 1.6f); }
                else {
                    ImVec2 C(su, sv);                 // third corner sets the shear
                    ImVec2 D(A.x + (C.x - B.x), A.y + (C.y - B.y));
                    pl({A, B, C, D, A});
                }
            } else if (tool_ == SketchTool::CirclePerimeter) {
                std::vector<ImVec2> pts;
                for (size_t i = 0; i < n && i < 3; ++i) pts.push_back(toolUV_[i]);
                if (pts.size() < 3) pts.push_back(ImVec2(su, sv));
                for (auto& q : pts) dl->AddCircleFilled(uvToScreen(q.x, q.y), 3.0f, uCol);
                if (pts.size() == 3) {
                    float cu, cv, rad;
                    if (circumcircle(pts[0], pts[1], pts[2], cu, cv, rad)) {
                        ImVec2 prev;
                        for (int i = 0; i <= 48; ++i) {
                            float a = i / 48.0f * 6.2831853f;
                            ImVec2 q = uvToScreen(cu + rad * std::cos(a), cv + rad * std::sin(a));
                            if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                        }
                    }
                }
            } else if (tool_ == SketchTool::SlotCtrStr && n >= 1) {
                ImVec2 ctr = toolUV_[0];
                ImVec2 end = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                if (n < 2) { dl->AddLine(uvToScreen(ctr.x, ctr.y), sCur, uCol, 1.6f); }
                else {
                    // centerpoint slot: spine endpoints are ctr +/- (end-ctr); cursor sets width.
                    ImVec2 e1(ctr.x + (end.x - ctr.x), ctr.y + (end.y - ctr.y));
                    ImVec2 e2(ctr.x - (end.x - ctr.x), ctr.y - (end.y - ctr.y));
                    float dx = e1.x - e2.x, dy = e1.y - e2.y; float L = std::hypot(dx, dy);
                    float hw = L > 1e-6f ? std::fabs((su - e2.x) * (-dy) + (sv - e2.y) * dx) / L : 5.0f;
                    ImVec2 a = uvToScreen(e1.x, e1.y), b = uvToScreen(e2.x, e2.y);
                    dl->AddLine(a, b, uCol, 1.6f);
                    ImVec2 wEnd = uvToScreen(e1.x + hw, e1.y);
                    float rpx = std::hypot(wEnd.x - a.x, wEnd.y - a.y);
                    dl->AddCircle(a, rpx, uCol, 24, 1.6f);
                    dl->AddCircle(b, rpx, uCol, 24, 1.6f);
                }
            } else if (tool_ == SketchTool::SlotArcCtr && n >= 1) {
                // click 1 = arc center; 2 = spine start (sets radius+a0); 3 = spine end (a1);
                // 4 = width. Live preview at each stage with the snapped cursor.
                const float R2D = 57.29577951f;
                ImVec2 ctr = toolUV_[0];
                dl->AddCircleFilled(uvToScreen(ctr.x, ctr.y), 3.0f, uCol);
                if (n < 2) { dl->AddLine(uvToScreen(ctr.x, ctr.y), sCur, uCol, 1.6f); }
                else {
                    ImVec2 s0 = toolUV_[1];
                    float rad = std::hypot(s0.x - ctr.x, s0.y - ctr.y);
                    float a0 = std::atan2(s0.y - ctr.y, s0.x - ctr.x) * R2D;
                    ImVec2 endPt = (n >= 3) ? toolUV_[2] : ImVec2(su, sv);
                    float a1 = std::atan2(endPt.y - ctr.y, endPt.x - ctr.x) * R2D;
                    float hw = 0.0f;
                    if (n >= 3) {  // width stage: perp distance of cursor from the spine radius
                        hw = std::fabs(std::hypot(su - ctr.x, sv - ctr.y) - rad);
                        if (hw < 1e-3f) hw = rad * 0.15f;
                    } else { hw = rad * 0.15f; }
                    drawCapsule(ctr.x, ctr.y, rad, a0, a1, hw);
                }
            } else if (tool_ == SketchTool::SlotArc3pt && n >= 1) {
                // clicks 1,2,3 = points ON the spine arc (circumcircle); 4 = width.
                const float R2D = 57.29577951f;
                std::vector<ImVec2> pts;
                for (size_t i = 0; i < n && i < 3; ++i) pts.push_back(toolUV_[i]);
                if (pts.size() < 3) pts.push_back(ImVec2(su, sv));
                for (auto& q : pts) dl->AddCircleFilled(uvToScreen(q.x, q.y), 3.0f, uCol);
                float cu, cv, rad;
                if (circumcircle(pts[0], pts[1], pts[2], cu, cv, rad)) {
                    float a0 = std::atan2(pts[0].y - cv, pts[0].x - cu) * R2D;
                    float amid = std::atan2(pts[1].y - cv, pts[1].x - cu) * R2D;
                    float a1 = std::atan2(pts[2].y - cv, pts[2].x - cu) * R2D;
                    // Choose CCW a0->a1 that passes through the middle pick.
                    auto norm = [](float a) { while (a < 0) a += 360.0f; while (a >= 360.0f) a -= 360.0f; return a; };
                    float sweep = norm(a1 - a0), midSw = norm(amid - a0);
                    if (midSw > sweep) { float t = a0; a0 = a1; a1 = t; }  // go the other way
                    float hw = (n >= 3) ? std::fabs(std::hypot(su - cu, sv - cv) - rad) : rad * 0.15f;
                    if (hw < 1e-3f) hw = rad * 0.15f;
                    drawCapsule(cu, cv, rad, a0, a1, hw);
                }
            } else if (tool_ == SketchTool::EllipsePartial && n >= 1) {
                ImVec2 ctr = toolUV_[0];
                ImVec2 rxp = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                float rot = std::atan2(rxp.y - ctr.y, rxp.x - ctr.x);
                float rx = std::hypot(rxp.x - ctr.x, rxp.y - ctr.y);
                float ca = std::cos(rot), sa = std::sin(rot);
                if (n < 2) { dl->AddLine(uvToScreen(ctr.x, ctr.y), sCur, uCol, 1.6f); }
                else {
                    // ry from the third pick's perpendicular distance; live cursor while n==2.
                    ImVec2 ryp = (n >= 3) ? toolUV_[2] : ImVec2(su, sv);
                    float ry = std::fabs(-(ryp.x - ctr.x) * sa + (ryp.y - ctr.y) * ca);
                    if (ry < 1e-4f) ry = rx * 0.5f;
                    // angle sweep: a0 from the 4th pick, a1 from the 5th/live pick.
                    auto angOf = [&](ImVec2 q) {
                        float lx = (q.x - ctr.x) * ca + (q.y - ctr.y) * sa;
                        float ly = -(q.x - ctr.x) * sa + (q.y - ctr.y) * ca;
                        return std::atan2(ly / std::fmax(ry, 1e-6f), lx / std::fmax(rx, 1e-6f));
                    };
                    float a0 = (n >= 4) ? angOf(toolUV_[3]) : 0.0f;
                    float a1 = (n >= 4) ? angOf(ImVec2(su, sv)) : 6.2831853f;
                    if (a1 < a0) a1 += 6.2831853f;
                    ImVec2 prev;
                    for (int i = 0; i <= 64; ++i) {
                        float t = a0 + (a1 - a0) * (i / 64.0f);
                        float lx = rx * std::cos(t), ly = ry * std::sin(t);
                        ImVec2 q = uvToScreen(ctr.x + lx * ca - ly * sa, ctr.y + lx * sa + ly * ca);
                        if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                    }
                }
            } else if (tool_ == SketchTool::Parabola && n >= 1) {
                ImVec2 focus = toolUV_[0];
                ImVec2 vertex = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                dl->AddCircleFilled(uvToScreen(focus.x, focus.y), 3.0f, uCol);
                if (n < 2) { dl->AddLine(uvToScreen(focus.x, focus.y), sCur, uCol, 1.6f); }
                else {
                    // axis = vertex->focus; f = |vertex-focus|. Sample y = x^2/(4f) in axis frame
                    // over the [start..end] parametric range set by the 3rd/4th (or live) picks.
                    float ax = focus.x - vertex.x, ay = focus.y - vertex.y;
                    float f = std::hypot(ax, ay); if (f < 1e-4f) f = 1.0f;
                    float ux = ax / f, uy = ay / f;     // axis dir
                    float px = -uy, py = ux;            // perp (opening) dir
                    auto tOf = [&](ImVec2 q) { return (q.x - vertex.x) * px + (q.y - vertex.y) * py; };
                    float t0 = (n >= 3) ? tOf(toolUV_[2]) : -2.0f * f;
                    float t1 = (n >= 3) ? tOf(ImVec2(su, sv)) : 2.0f * f;
                    ImVec2 prev;
                    for (int i = 0; i <= 48; ++i) {
                        float t = t0 + (t1 - t0) * (i / 48.0f);
                        float d = t * t / (4.0f * f);    // distance along axis from vertex
                        ImVec2 q = uvToScreen(vertex.x + ux * d + px * t, vertex.y + uy * d + py * t);
                        if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                    }
                }
            } else if (tool_ == SketchTool::Conic && n >= 1) {
                ImVec2 P0 = toolUV_[0];
                ImVec2 P1 = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                dl->AddCircleFilled(uvToScreen(P0.x, P0.y), 3.0f, uCol);
                if (n < 2) { dl->AddLine(uvToScreen(P0.x, P0.y), sCur, uCol, 1.6f); }
                else {
                    ImVec2 apex = (n >= 3) ? toolUV_[2] : ImVec2(su, sv);
                    // rho: 0..1 weight of the apex (shoulder). Live-pick while n==3 (cursor sets it),
                    // committed in conicRho_ once the apex is placed.
                    float rho = conicRho_;
                    if (n == 3) {  // live rho preview: project cursor onto the mid->apex segment
                        ImVec2 mid((P0.x + P1.x) * 0.5f, (P0.y + P1.y) * 0.5f);
                        float dx = apex.x - mid.x, dy = apex.y - mid.y; float L2 = dx * dx + dy * dy;
                        rho = L2 > 1e-9f ? ((su - mid.x) * dx + (sv - mid.y) * dy) / L2 : 0.5f;
                        rho = std::fmax(0.01f, std::fmin(0.99f, rho));
                    }
                    // Rational Bezier conic: B(t)=((1-t)^2 P0 + 2(1-t)t w Apex + t^2 P1)/den,
                    // den=(1-t)^2 + 2(1-t)t w + t^2, with w = rho/(1-rho).
                    float w = rho / std::fmax(1e-3f, 1.0f - rho);
                    ImVec2 prev;
                    for (int i = 0; i <= 48; ++i) {
                        float t = i / 48.0f, mt = 1.0f - t;
                        float b0 = mt * mt, b1 = 2.0f * mt * t * w, b2 = t * t;
                        float den = b0 + b1 + b2;
                        float qx = (b0 * P0.x + b1 * apex.x + b2 * P1.x) / den;
                        float qy = (b0 * P0.y + b1 * apex.y + b2 * P1.y) / den;
                        ImVec2 q = uvToScreen(qx, qy);
                        if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                    }
                    dl->AddCircle(uvToScreen(apex.x, apex.y), 4.0f, uCol, 12, 1.2f);
                }

            // ---- C42 tool previews ----
            } else if (tool_ == SketchTool::Arc3pt && n >= 1) {
                // picks: arc start, arc end, point ON the arc (live cursor fills the missing one).
                std::vector<ImVec2> pts;
                for (size_t i = 0; i < n && i < 3; ++i) pts.push_back(toolUV_[i]);
                if (pts.size() < 3) pts.push_back(ImVec2(su, sv));
                for (auto& q : pts) dl->AddCircleFilled(uvToScreen(q.x, q.y), 3.0f, uCol);
                if (n == 1) { dl->AddLine(uvToScreen(pts[0].x, pts[0].y), sCur, uCol, 1.6f); }
                else {
                    float cu, cv, rad;
                    if (circumcircle(pts[0], pts[1], pts[2], cu, cv, rad)) {
                        const float R2D = 57.29577951f;
                        float aS = std::atan2(pts[0].y - cv, pts[0].x - cu) * R2D;
                        float aE = std::atan2(pts[1].y - cv, pts[1].x - cu) * R2D;
                        float aM = std::atan2(pts[2].y - cv, pts[2].x - cu) * R2D;
                        auto norm = [](float a) { while (a < 0) a += 360.0f; while (a >= 360.0f) a -= 360.0f; return a; };
                        bool fwd = norm(aM - aS) <= norm(aE - aS);   // CCW start->end passes thru mid?
                        float a0 = fwd ? aS : aE, a1 = fwd ? aE : aS;
                        if (a1 < a0) a1 += 360.0f;
                        const float D2R = 3.14159265f / 180.0f;
                        ImVec2 prev;
                        for (int i = 0; i <= 48; ++i) {
                            float a = (a0 + (a1 - a0) * (i / 48.0f)) * D2R;
                            ImVec2 q = uvToScreen(cu + rad * std::cos(a), cv + rad * std::sin(a));
                            if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                        }
                    }
                }
            } else if (tool_ == SketchTool::Polygon && n >= 1) {
                ImVec2 ctr = toolUV_[0];
                dl->AddCircleFilled(uvToScreen(ctr.x, ctr.y), 3.0f, uCol);
                float vx = su - ctr.x, vy = sv - ctr.y;
                float R = std::hypot(vx, vy);
                if (R < 1e-4f) { dl->AddLine(uvToScreen(ctr.x, ctr.y), sCur, uCol, 1.6f); }
                else {
                    float a0 = std::atan2(vy, vx);
                    ImVec2 prev;
                    for (int i = 0; i <= polySides_; ++i) {
                        float a = a0 + 6.2831853f * i / (float)polySides_;
                        ImVec2 q = uvToScreen(ctr.x + R * std::cos(a), ctr.y + R * std::sin(a));
                        if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                    }
                }
            } else if (tool_ == SketchTool::RectCenter && n >= 1) {
                ImVec2 ctr = toolUV_[0];
                dl->AddCircleFilled(uvToScreen(ctr.x, ctr.y), 3.0f, uCol);
                float dx = std::fabs(su - ctr.x), dy = std::fabs(sv - ctr.y);
                ImVec2 A(ctr.x - dx, ctr.y - dy), B(ctr.x + dx, ctr.y - dy);
                ImVec2 C(ctr.x + dx, ctr.y + dy), D(ctr.x - dx, ctr.y + dy);
                pl({A, B, C, D, A});
            } else if (tool_ == SketchTool::Ellipse && n >= 1) {
                ImVec2 ctr = toolUV_[0];
                ImVec2 rxp = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                float rot = std::atan2(rxp.y - ctr.y, rxp.x - ctr.x);
                float rx = std::hypot(rxp.x - ctr.x, rxp.y - ctr.y);
                float ca = std::cos(rot), sa = std::sin(rot);
                if (n < 2) { dl->AddLine(uvToScreen(ctr.x, ctr.y), sCur, uCol, 1.6f); }
                else {
                    float ry = std::fabs(-(su - ctr.x) * sa + (sv - ctr.y) * ca);
                    if (ry < 1e-4f) ry = rx * 0.5f;
                    ImVec2 prev;
                    for (int i = 0; i <= 64; ++i) {
                        float a = i / 64.0f * 6.2831853f;
                        float lx = rx * std::cos(a), ly = ry * std::sin(a);
                        ImVec2 q = uvToScreen(ctr.x + lx * ca - ly * sa, ctr.y + lx * sa + ly * ca);
                        if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                    }
                }
            } else if (tool_ == SketchTool::Spline && n >= 1) {
                std::vector<ImVec2> pts(toolUV_.begin(), toolUV_.end());
                pts.push_back(ImVec2(su, sv));
                pl(pts);
                for (auto& q : pts) dl->AddCircleFilled(uvToScreen(q.x, q.y), 3.0f, uCol);
            }
        }

        // ---- C49: inference feedback (dashed alignment lines, snap markers, relation chips) ----
        const ImU32 inferCol = col(pal.sketchInfer);
        if (infer.alignU || infer.alignV) {
            // Dashed line from the alignment source point to the cursor (positional only).
            auto dashed = [&](ImVec2 a, ImVec2 b) {
                float dx = b.x - a.x, dy = b.y - a.y;
                float L = std::hypot(dx, dy);
                if (L < 1.0f) return;
                const float dash = 6.0f * ui::uiScale, gap = 4.0f * ui::uiScale;
                for (float t = 0; t < L; t += dash + gap) {
                    float e = std::fmin(t + dash, L);
                    dl->AddLine(ImVec2(a.x + dx * (t / L), a.y + dy * (t / L)),
                                ImVec2(a.x + dx * (e / L), a.y + dy * (e / L)), inferCol, 1.2f);
                }
            };
            if (infer.alignU) dashed(uvToScreen(infer.srcU.x, infer.srcU.y), sCur);
            if (infer.alignV) dashed(uvToScreen(infer.srcV.x, infer.srcV.y), sCur);
        }
        // Cursor marker.
        dl->AddCircle(sCur, 4.0f * ui::uiScale, prevCol, 12, 1.5f);
        // Snap markers (unified, ui::uiScale-scaled): yellow-ish dot = endpoint/point snap,
        // concentric rings = center snap, diamond = midpoint snap, plain ring = rim contact.
        if (!snapH.empty()) {
            const float ms = 5.0f * ui::uiScale;
            if (snapHasPrefix(snapH, "mid:")) {
                ImVec2 d[4] = {ImVec2(sCur.x, sCur.y - ms * 1.5f), ImVec2(sCur.x + ms * 1.5f, sCur.y),
                               ImVec2(sCur.x, sCur.y + ms * 1.5f), ImVec2(sCur.x - ms * 1.5f, sCur.y)};
                dl->AddQuad(d[0], d[1], d[2], d[3], inferCol, 1.6f);
            } else if (snapHasPrefix(snapH, "rim:") || snapHasPrefix(snapH, "tan:")) {
                dl->AddCircle(sCur, ms * 1.4f, inferCol, 16, 1.6f);
            } else if (snapH.find(".center") != std::string::npos) {
                dl->AddCircle(sCur, ms, inferCol, 16, 1.6f);
                dl->AddCircle(sCur, ms * 1.9f, inferCol, 16, 1.2f);
            } else {  // endpoint / point / spline through-point
                dl->AddCircleFilled(sCur, ms * 0.7f, inferCol);
                dl->AddCircle(sCur, ms * 1.4f, inferCol, 16, 1.2f);
            }
        }
        // Live relation chips near the cursor (same style as the C41 relation badges).
        if (hvH || hvV || tanSnap) {
            const float bs2 = 16.0f * ui::uiScale;
            float chipX = sCur.x + bs2;                       // below-right of the cursor
            auto chip = [&](const char* glyph) {
                ImVec2 gp(chipX, sCur.y + bs2);
                ImVec2 c0(gp.x - bs2 * 0.5f, gp.y - bs2 * 0.5f), c1(gp.x + bs2 * 0.5f, gp.y + bs2 * 0.5f);
                dl->AddRectFilled(c0, c1, col(pal.sketchRelBg), 3.5f * ui::uiScale);
                dl->AddRect(c0, c1, col(pal.sketchRelBorder), 3.5f * ui::uiScale, 0, 1.2f);
                ImVec2 gs = ImGui::CalcTextSize(glyph);
                dl->AddText(ImVec2(gp.x - gs.x * 0.5f, gp.y - gs.y * 0.5f), col(pal.sketchInk), glyph);
                chipX += bs2 + 2.0f;
            };
            if (hvH) chip("-");
            if (hvV) chip("|");
            if (tanSnap) chip("T");
        }

        // Click placement.
        if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
            toolUV_.push_back(ImVec2(su, sv));
            toolSnaps_.push_back(snapH);
            // Tools that complete on a fixed number of clicks:
            if (tool_ == SketchTool::Line && toolUV_.size() == 2) {
                ImVec2 p1 = toolUV_[0], p2 = toolUV_[1];
                nlohmann::json cs = nlohmann::json::array();
                // C49: a rim-snapped START whose placement direction matched gets the tangent too.
                if (tanStartUpgrade && snapHasPrefix(toolSnaps_[0], "rim:"))
                    toolSnaps_[0] = "tan:" + toolSnaps_[0].substr(4);
                bool tangentSeg = snapHasPrefix(toolSnaps_[0], "tan:") ||
                                  snapHasPrefix(toolSnaps_[1], "tan:");
                // Auto h/v relation (inference only). When the END landed on a snapped point that
                // point wins: only add h/v if the result is EXACTLY axis-aligned. A tangent-snapped
                // direction never gets h/v stacked on top.
                float du = p2.x - p1.x, dv = p2.y - p1.y;
                if (inferenceOn_ && !tangentSeg) {
                    float ang = std::atan2(std::fabs(dv), std::fabs(du)) * 57.2957795f;
                    float tol = toolSnaps_[1].empty() ? kHVDeg : 0.01f;
                    if (ang < tol) cs.push_back({{"type", "horizontal"}, {"refs", {"$self"}}});
                    else if (ang > 90.0f - tol) cs.push_back({{"type", "vertical"}, {"refs", {"$self"}}});
                }
                // C49: snap-derived constraint for one endpoint (pt = "$self.p1"/"$self.p2"):
                // plain handle -> coincident; "mid:" -> midpoint; "rim:"/"tan:" -> point_on
                // (+ one tangent per segment).
                bool tangentEmitted = false;
                auto snapCons = [&](const std::string& snap, const char* pt) {
                    if (snap.empty()) return;
                    if (snapHasPrefix(snap, "mid:")) {
                        cs.push_back({{"type", "midpoint"}, {"refs", {pt, snap.substr(4)}}});
                    } else if (snapHasPrefix(snap, "rim:") || snapHasPrefix(snap, "tan:")) {
                        std::string eid = snap.substr(4);
                        cs.push_back({{"type", "point_on"}, {"refs", {pt, eid}}});
                        if (snapHasPrefix(snap, "tan:") && !tangentEmitted) {
                            cs.push_back({{"type", "tangent"}, {"refs", {"$self", eid}}});
                            tangentEmitted = true;
                        }
                    } else {
                        cs.push_back({{"type", "coincident"}, {"refs", {snap, pt}}});
                    }
                };
                // C37-D: start endpoint — from the chained predecessor's p2 (so a continued chain
                // stays constrained) OR from the start click's snap.
                if (!chainPrevId_.empty())
                    cs.push_back({{"type", "coincident"}, {"refs", {chainPrevId_ + ".p2", "$self.p1"}}});
                else
                    snapCons(toolSnaps_[0], "$self.p1");
                // C49: the END click's snap is now constrained too (was positional-only before).
                snapCons(toolSnaps_[1], "$self.p2");
                nlohmann::json ent = {{"type", "line"},
                    {"p1", nlohmann::json::array({p1.x, p1.y})},
                    {"p2", nlohmann::json::array({p2.x, p2.y})}};
                // Apply directly so we can capture the new line id for the next chain segment.
                nlohmann::json op = {{"op", "sketch_add_entity"}, {"sketch", fid_},
                                     {"entity", ent}, {"constraints", cs}};
                auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
                std::string newId;
                if (!r.ok) {
                    st.toast(ui::ToastLevel::Error, "Line: " + r.error_message);
                } else {
                    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                    newId = (r.id_map.is_object() && r.id_map.contains("$0"))
                          ? r.id_map["$0"].get<std::string>() : std::string();
                    resolve(st);
                }
                // Chain: keep drawing from the new endpoint, remembering this segment's id so the
                // next segment's p1 gets a coincident to its p2.
                ImVec2 chainStart = p2;
                resetTool(); tool_ = SketchTool::Line;
                chainPrevId_ = newId;
                toolUV_.push_back(chainStart);
                toolSnaps_.push_back("");
            } else if (tool_ == SketchTool::RectCorner && toolUV_.size() == 2) {
                ImVec2 a = toolUV_[0], b = toolUV_[1];
                // Four lines with coincident corners + h/v relations (inferred per line via $self).
                float xs[4] = {a.x, b.x, b.x, a.x};
                float ys[4] = {a.y, a.y, b.y, b.y};
                // Build as four separate add_entity ops via successive applyOps so coincidents can
                // reference real ids; for simplicity emit them with inferred h/v only and rely on
                // the solver + later dims. We chain coincidents using the running entity ids.
                std::string prevId;
                std::string firstId;
                for (int i = 0; i < 4; ++i) {
                    int j = (i + 1) % 4;
                    nlohmann::json cs = nlohmann::json::array();
                    bool horiz = (i == 0 || i == 2);
                    cs.push_back({{"type", horiz ? "horizontal" : "vertical"}, {"refs", {"$self"}}});
                    if (!prevId.empty())
                        cs.push_back({{"type", "coincident"}, {"refs", {prevId + ".p2", "$self.p1"}}});
                    nlohmann::json ent = {{"type", "line"},
                        {"p1", nlohmann::json::array({xs[i], ys[i]})},
                        {"p2", nlohmann::json::array({xs[j], ys[j]})}};
                    nlohmann::json op = {{"op", "sketch_add_entity"}, {"sketch", fid_},
                                         {"entity", ent}, {"constraints", cs}};
                    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
                    if (!r.ok) { st.addLog("[sketch] rect line failed: " + r.error_message); break; }
                    std::string nid = (r.id_map.is_object() && r.id_map.contains("$0"))
                                    ? r.id_map["$0"].get<std::string>() : std::string();
                    if (i == 0) firstId = nid;
                    prevId = nid;
                    if (i == 3 && !firstId.empty() && !nid.empty()) {
                        // close the loop: last.p2 coincident first.p1
                        nlohmann::json clop = {{"op", "sketch_add_constraint"}, {"sketch", fid_},
                            {"constraint", {{"type", "coincident"}, {"refs", {nid + ".p2", firstId + ".p1"}}}}};
                        st.docMirror.applyOps(st.worker, nlohmann::json::array({clop}));
                    }
                    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                }
                resolve(st);
                resetTool();
            } else if (tool_ == SketchTool::Circle && toolUV_.size() == 2) {
                ImVec2 c = toolUV_[0], rim = toolUV_[1];
                float rad = std::hypot(rim.x - c.x, rim.y - c.y);
                nlohmann::json ent = {{"type", "circle"},
                    {"center", nlohmann::json::array({c.x, c.y})}, {"radius", rad}};
                addEntity(st, ent, nlohmann::json::array());
                resetTool();
            } else if (tool_ == SketchTool::ArcCenter && toolUV_.size() == 3) {
                ImVec2 c = toolUV_[0], p1 = toolUV_[1], p2 = toolUV_[2];
                float rad = std::hypot(p1.x - c.x, p1.y - c.y);
                float a0 = std::atan2(p1.y - c.y, p1.x - c.x) * 57.2957795f;
                float a1 = std::atan2(p2.y - c.y, p2.x - c.x) * 57.2957795f;
                nlohmann::json ent = {{"type", "arc"},
                    {"center", nlohmann::json::array({c.x, c.y})}, {"radius", rad},
                    {"start", a0}, {"end", a1}, {"ccw", true}};
                addEntity(st, ent, nlohmann::json::array());
                resetTool();
            } else if (tool_ == SketchTool::Slot && toolUV_.size() == 3) {
                ImVec2 p1 = toolUV_[0], p2 = toolUV_[1], wpt = toolUV_[2];
                // width = perpendicular distance from wpt to the p1-p2 line.
                float dx = p2.x - p1.x, dy = p2.y - p1.y;
                float L = std::hypot(dx, dy);
                float w = L > 1e-6f ? std::fabs((wpt.x - p1.x) * (-dy) + (wpt.y - p1.y) * dx) / L : 5.0f;
                if (w < 1e-3f) w = 5.0f;
                nlohmann::json ent = {{"type", "slot"},
                    {"p1", nlohmann::json::array({p1.x, p1.y})},
                    {"p2", nlohmann::json::array({p2.x, p2.y})}, {"width", w * 2.0f}};
                addEntity(st, ent, nlohmann::json::array());
                resetTool();

            // ---- C28-B variants ----
            } else if (tool_ == SketchTool::Rect3ptCorner && toolUV_.size() == 3) {
                // A, B (one edge + angle), C (width by perp offset). Rotated rect: opposite sides
                // parallel+equal, adjacent perpendicular.
                ImVec2 A = toolUV_[0], B = toolUV_[1], C3 = toolUV_[2];
                float ex = B.x - A.x, ey = B.y - A.y; float L = std::hypot(ex, ey);
                float nx = L > 1e-6f ? -ey / L : 0, ny = L > 1e-6f ? ex / L : 0;
                float w = (C3.x - A.x) * nx + (C3.y - A.y) * ny;
                ImVec2 D(A.x + nx * w, A.y + ny * w), C(B.x + nx * w, B.y + ny * w);
                ImVec2 quad[4] = {A, B, C, D};
                const char* rel[4] = {"parallel perpendicular", "parallel", nullptr, nullptr};
                addQuadLoop(st, quad, rel);
                resetTool();
            } else if (tool_ == SketchTool::Rect3ptCenter && toolUV_.size() == 3) {
                // center, edge-midpoint dir (half-height), half-width pick.
                ImVec2 ctr = toolUV_[0], mid = toolUV_[1], wpk = toolUV_[2];
                float hx = mid.x - ctr.x, hy = mid.y - ctr.y; float L = std::hypot(hx, hy);
                float px = L > 1e-6f ? -hy / L : 0, py = L > 1e-6f ? hx / L : 0;
                float hw = (wpk.x - ctr.x) * px + (wpk.y - ctr.y) * py;
                ImVec2 wv(px * hw, py * hw);
                ImVec2 P0(ctr.x + hx + wv.x, ctr.y + hy + wv.y);
                ImVec2 P1(ctr.x + hx - wv.x, ctr.y + hy - wv.y);
                ImVec2 P2(ctr.x - hx - wv.x, ctr.y - hy - wv.y);
                ImVec2 P3(ctr.x - hx + wv.x, ctr.y - hy + wv.y);
                ImVec2 quad[4] = {P0, P1, P2, P3};
                const char* rel[4] = {"parallel perpendicular", "parallel", nullptr, nullptr};
                addQuadLoop(st, quad, rel);
                resetTool();
            } else if (tool_ == SketchTool::Parallelogram && toolUV_.size() == 3) {
                // 3 corners -> opposite sides parallel+equal, NO perpendicular.
                ImVec2 A = toolUV_[0], B = toolUV_[1], C = toolUV_[2];
                ImVec2 D(A.x + (C.x - B.x), A.y + (C.y - B.y));
                ImVec2 quad[4] = {A, B, C, D};
                const char* rel[4] = {"parallel equal", "parallel equal", nullptr, nullptr};
                addQuadLoop(st, quad, rel);
                resetTool();
            } else if (tool_ == SketchTool::CirclePerimeter && toolUV_.size() == 3) {
                float cu, cv, rad;
                if (!circumcircle(toolUV_[0], toolUV_[1], toolUV_[2], cu, cv, rad)) {
                    // collinear/degenerate guard: drop the 3rd pick, keep first two, hint via log.
                    st.addLog("[sketch] Perimeter Circle: 3 points are collinear — pick a 3rd off the line");
                    toolUV_.pop_back(); toolSnaps_.pop_back();
                } else {
                    nlohmann::json ent = {{"type", "circle"},
                        {"center", nlohmann::json::array({cu, cv})}, {"radius", rad}};
                    addEntity(st, ent, nlohmann::json::array());
                    resetTool();
                }
            } else if (tool_ == SketchTool::SlotCtrStr && toolUV_.size() == 3) {
                // center, end-direction/length, width. Spine = center +/- (end-center).
                ImVec2 ctr = toolUV_[0], end = toolUV_[1], wpk = toolUV_[2];
                ImVec2 e1(ctr.x + (end.x - ctr.x), ctr.y + (end.y - ctr.y));
                ImVec2 e2(ctr.x - (end.x - ctr.x), ctr.y - (end.y - ctr.y));
                float dx = e1.x - e2.x, dy = e1.y - e2.y; float L = std::hypot(dx, dy);
                float w = L > 1e-6f ? std::fabs((wpk.x - e2.x) * (-dy) + (wpk.y - e2.y) * dx) / L : 5.0f;
                if (w < 1e-3f) w = 5.0f;
                nlohmann::json ent = {{"type", "slot"},
                    {"p1", nlohmann::json::array({e1.x, e1.y})},
                    {"p2", nlohmann::json::array({e2.x, e2.y})}, {"width", w * 2.0f}};
                addEntity(st, ent, nlohmann::json::array());
                resetTool();
            } else if (tool_ == SketchTool::SlotArcCtr && toolUV_.size() == 4) {
                // center, spine start (radius+a0), spine end dir (a1), width.
                const float R2D = 57.29577951f;
                ImVec2 ctr = toolUV_[0], s0 = toolUV_[1], endPt = toolUV_[2], wpk = toolUV_[3];
                float rad = std::hypot(s0.x - ctr.x, s0.y - ctr.y);
                if (rad < 1e-4f) { st.addLog("[sketch] Arc Slot: center == start"); resetTool(); }
                else {
                    float a0 = std::atan2(s0.y - ctr.y, s0.x - ctr.x) * R2D;
                    float a1 = std::atan2(endPt.y - ctr.y, endPt.x - ctr.x) * R2D;
                    if (a1 <= a0) a1 += 360.0f;            // enforce CCW a0 -> a1
                    // width = total capsule width = 2 * |dist(wpk, center) - radius|.
                    float w = 2.0f * std::fabs(std::hypot(wpk.x - ctr.x, wpk.y - ctr.y) - rad);
                    if (w < 1e-3f || w >= 2.0f * rad) w = std::fmin(rad * 0.3f, rad * 0.9f);
                    nlohmann::json ent = {{"type", "arc_slot"},
                        {"center", nlohmann::json::array({ctr.x, ctr.y})},
                        {"radius", rad}, {"a0", a0}, {"a1", a1}, {"width", w}};
                    addEntity(st, ent, nlohmann::json::array());
                    resetTool();
                }
            } else if (tool_ == SketchTool::SlotArc3pt && toolUV_.size() == 4) {
                // 3 points on the spine arc (circumcircle), then width.
                const float R2D = 57.29577951f;
                float cu, cv, rad;
                if (!circumcircle(toolUV_[0], toolUV_[1], toolUV_[2], cu, cv, rad)) {
                    st.addLog("[sketch] 3Pt Arc Slot: 3 points are collinear — pick a 3rd off the line");
                    toolUV_.pop_back(); toolSnaps_.pop_back();   // drop the width pick, keep retrying
                } else {
                    ImVec2 p0 = toolUV_[0], pm = toolUV_[1], p2 = toolUV_[2], wpk = toolUV_[3];
                    float a0 = std::atan2(p0.y - cv, p0.x - cu) * R2D;
                    float amid = std::atan2(pm.y - cv, pm.x - cu) * R2D;
                    float a1 = std::atan2(p2.y - cv, p2.x - cu) * R2D;
                    auto norm = [](float a) { while (a < 0) a += 360.0f; while (a >= 360.0f) a -= 360.0f; return a; };
                    float sweep = norm(a1 - a0), midSw = norm(amid - a0);
                    if (midSw > sweep) { float t = a0; a0 = a1; a1 = t; }  // CCW must pass thru mid
                    if (a1 <= a0) a1 += 360.0f;
                    float w = 2.0f * std::fabs(std::hypot(wpk.x - cu, wpk.y - cv) - rad);
                    if (w < 1e-3f || w >= 2.0f * rad) w = std::fmin(rad * 0.3f, rad * 0.9f);
                    nlohmann::json ent = {{"type", "arc_slot"},
                        {"center", nlohmann::json::array({cu, cv})},
                        {"radius", rad}, {"a0", a0}, {"a1", a1}, {"width", w}};
                    addEntity(st, ent, nlohmann::json::array());
                    resetTool();
                }
            } else if (tool_ == SketchTool::EllipsePartial && toolUV_.size() == 5) {
                // center, rx point, ry/rot point, start-angle, end-angle.
                ImVec2 ctr = toolUV_[0], rxp = toolUV_[1], ryp = toolUV_[2];
                float rot = std::atan2(rxp.y - ctr.y, rxp.x - ctr.x);
                float rx = std::hypot(rxp.x - ctr.x, rxp.y - ctr.y);
                float ca = std::cos(rot), sa = std::sin(rot);
                float ry = std::fabs(-(ryp.x - ctr.x) * sa + (ryp.y - ctr.y) * ca);
                if (rx < 1e-4f || ry < 1e-4f) { st.addLog("[sketch] Partial Ellipse: degenerate radii"); resetTool(); }
                else {
                    auto angOf = [&](ImVec2 q) {
                        float lx = (q.x - ctr.x) * ca + (q.y - ctr.y) * sa;
                        float ly = -(q.x - ctr.x) * sa + (q.y - ctr.y) * ca;
                        return std::atan2(ly / ry, lx / rx);
                    };
                    // Contract (doc_schema.md §3.1): rot/a0/a1 are DEGREES, angles in the
                    // un-rotated ellipse frame, CCW a0 -> a1.
                    const float kR2D = 57.29577951f;
                    float a0 = angOf(toolUV_[3]) * kR2D, a1 = angOf(toolUV_[4]) * kR2D;
                    if (a1 <= a0) a1 += 360.0f;   // enforce CCW sweep
                    nlohmann::json ent = {{"type", "ellipse_arc"},
                        {"center", nlohmann::json::array({ctr.x, ctr.y})},
                        {"rx", rx}, {"ry", ry}, {"rot", rot * kR2D}, {"a0", a0}, {"a1", a1}};
                    addEntity(st, ent, nlohmann::json::array());
                    resetTool();
                }
            } else if (tool_ == SketchTool::Parabola && toolUV_.size() == 4) {
                // focus, vertex, start, end picks. Contract (doc_schema.md §3.1) stores the
                // range as t0/t1: signed extents along w = perp(unit(focus - vertex)), i.e.
                // t = (P - vertex)·w in the parabola's own frame.
                ImVec2 focus = toolUV_[0], vertex = toolUV_[1], s0 = toolUV_[2], s1 = toolUV_[3];
                float ux = focus.x - vertex.x, uy = focus.y - vertex.y;
                float ul = std::hypot(ux, uy);
                if (ul < 1e-4f) { st.addLog("[sketch] Parabola: focus == vertex"); resetTool(); }
                else {
                    ux /= ul; uy /= ul;
                    float wx = -uy, wy = ux;   // perp(u)
                    float t0 = (s0.x - vertex.x) * wx + (s0.y - vertex.y) * wy;
                    float t1 = (s1.x - vertex.x) * wx + (s1.y - vertex.y) * wy;
                    nlohmann::json ent = {{"type", "parabola"},
                        {"vertex", nlohmann::json::array({vertex.x, vertex.y})},
                        {"focus", nlohmann::json::array({focus.x, focus.y})},
                        {"t0", t0}, {"t1", t1}};
                    addEntity(st, ent, nlohmann::json::array());
                    resetTool();
                }
            } else if (tool_ == SketchTool::Conic && toolUV_.size() == 4) {
                // p0, p1, apex, then a 4th click sets rho (projection onto mid->apex).
                ImVec2 P0 = toolUV_[0], P1 = toolUV_[1], apex = toolUV_[2], rhoPk = toolUV_[3];
                ImVec2 mid((P0.x + P1.x) * 0.5f, (P0.y + P1.y) * 0.5f);
                float dx = apex.x - mid.x, dy = apex.y - mid.y; float L2 = dx * dx + dy * dy;
                float rho = L2 > 1e-9f ? ((rhoPk.x - mid.x) * dx + (rhoPk.y - mid.y) * dy) / L2 : 0.5f;
                rho = std::fmax(0.01f, std::fmin(0.99f, rho));
                nlohmann::json ent = {{"type", "conic"},
                    {"p0", nlohmann::json::array({P0.x, P0.y})},
                    {"p1", nlohmann::json::array({P1.x, P1.y})},
                    {"apex", nlohmann::json::array({apex.x, apex.y})},
                    {"rho", rho}};
                addEntity(st, ent, nlohmann::json::array());
                resetTool();

            // ---- C42 tool completions ----
            } else if (tool_ == SketchTool::Arc3pt && toolUV_.size() == 3) {
                // p1 = arc start, p2 = arc end, p3 = point ON the arc -> circumcircle; ccw chosen
                // so the sweep from start to end passes through p3.
                float cu, cv, rad;
                if (!circumcircle(toolUV_[0], toolUV_[1], toolUV_[2], cu, cv, rad)) {
                    st.addLog("[sketch] 3-Pt Arc: points are collinear \xE2\x80\x94 pick the on-arc point off the line");
                    toolUV_.pop_back(); toolSnaps_.pop_back();
                } else {
                    const float R2D = 57.29577951f;
                    float aS = std::atan2(toolUV_[0].y - cv, toolUV_[0].x - cu) * R2D;
                    float aE = std::atan2(toolUV_[1].y - cv, toolUV_[1].x - cu) * R2D;
                    float aM = std::atan2(toolUV_[2].y - cv, toolUV_[2].x - cu) * R2D;
                    auto norm = [](float a) { while (a < 0) a += 360.0f; while (a >= 360.0f) a -= 360.0f; return a; };
                    // Always emit a CCW arc (the solver's solved records drop "ccw", so a false
                    // flag would re-render wrong after the first solve): if the CCW sweep p1->p2
                    // misses p3, store the arc as CCW p2->p1 instead (same geometry).
                    bool fwd = norm(aM - aS) <= norm(aE - aS);
                    float a0 = fwd ? aS : aE, a1 = fwd ? aE : aS;
                    // Endpoint snaps -> coincidents onto the arc's start/end handles (like Line);
                    // when reversed, p1 sits on the "end" handle and p2 on "start".
                    // C49: only PLAIN point snaps become coincidents here — midpoint/rim snaps on
                    // arc picks stay positional (coincRef filters the prefixed records).
                    nlohmann::json cs = nlohmann::json::array();
                    std::string s0 = coincRef(toolSnaps_[0]), s1 = coincRef(toolSnaps_[1]);
                    if (!s0.empty())
                        cs.push_back({{"type", "coincident"},
                                      {"refs", {s0, fwd ? "$self.start" : "$self.end"}}});
                    if (!s1.empty())
                        cs.push_back({{"type", "coincident"},
                                      {"refs", {s1, fwd ? "$self.end" : "$self.start"}}});
                    nlohmann::json ent = {{"type", "arc"},
                        {"center", nlohmann::json::array({cu, cv})}, {"radius", rad},
                        {"start", a0}, {"end", a1}, {"ccw", true}};
                    addEntity(st, ent, cs);
                    resetTool();
                }
            } else if (tool_ == SketchTool::Polygon && toolUV_.size() == 2) {
                // center + one vertex -> inscribed N-gon (closed equal-edged line loop).
                ImVec2 ctr = toolUV_[0], vtx = toolUV_[1];
                float R = std::hypot(vtx.x - ctr.x, vtx.y - ctr.y);
                if (R < 1e-4f) {
                    st.addLog("[sketch] Polygon: vertex == center \xE2\x80\x94 pick a vertex away from the center");
                    toolUV_.pop_back(); toolSnaps_.pop_back();
                } else {
                    float a0 = std::atan2(vtx.y - ctr.y, vtx.x - ctr.x);
                    std::vector<ImVec2> corners;
                    for (int i = 0; i < polySides_; ++i) {
                        float a = a0 + 6.2831853f * i / (float)polySides_;
                        corners.push_back(ImVec2(ctr.x + R * std::cos(a), ctr.y + R * std::sin(a)));
                    }
                    addPolyLoop(st, corners, /*equalEdges=*/true);
                    resetTool();
                }
            } else if (tool_ == SketchTool::RectCenter && toolUV_.size() == 2) {
                // center + corner -> axis-aligned rect (same per-edge h/v relations as RectCorner).
                ImVec2 ctr = toolUV_[0], cor = toolUV_[1];
                float dx = std::fabs(cor.x - ctr.x), dy = std::fabs(cor.y - ctr.y);
                if (dx < 1e-4f || dy < 1e-4f) {
                    st.addLog("[sketch] Center Rectangle: zero width/height \xE2\x80\x94 pick a corner off the axes");
                    toolUV_.pop_back(); toolSnaps_.pop_back();
                } else {
                    ImVec2 quad[4] = {ImVec2(ctr.x - dx, ctr.y - dy), ImVec2(ctr.x + dx, ctr.y - dy),
                                      ImVec2(ctr.x + dx, ctr.y + dy), ImVec2(ctr.x - dx, ctr.y + dy)};
                    const char* rel[4] = {"horizontal", "vertical", "horizontal", "vertical"};
                    addQuadLoop(st, quad, rel);
                    resetTool();
                }
            } else if (tool_ == SketchTool::Point && toolUV_.size() == 1) {
                nlohmann::json ent = {{"type", "point"},
                    {"p", nlohmann::json::array({toolUV_[0].x, toolUV_[0].y})}};
                // C49: a snapped Point pick gets the matching relation (its handle is "$self.p").
                nlohmann::json cs = nlohmann::json::array();
                const std::string& sp = toolSnaps_[0];
                if (snapHasPrefix(sp, "mid:"))
                    cs.push_back({{"type", "midpoint"}, {"refs", {"$self.p", sp.substr(4)}}});
                else if (snapHasPrefix(sp, "rim:") || snapHasPrefix(sp, "tan:"))
                    cs.push_back({{"type", "point_on"}, {"refs", {"$self.p", sp.substr(4)}}});
                else if (!sp.empty())
                    cs.push_back({{"type", "coincident"}, {"refs", {sp, "$self.p"}}});
                addEntity(st, ent, cs);
                toolUV_.clear(); toolSnaps_.clear();   // stay armed (SW point tool repeats)
            } else if (tool_ == SketchTool::Ellipse && toolUV_.size() == 3) {
                // center, major-axis end (rx + rot), minor point (ry = perp distance).
                ImVec2 ctr = toolUV_[0], rxp = toolUV_[1], ryp = toolUV_[2];
                float rot = std::atan2(rxp.y - ctr.y, rxp.x - ctr.x);
                float rx = std::hypot(rxp.x - ctr.x, rxp.y - ctr.y);
                float ca = std::cos(rot), sa = std::sin(rot);
                float ry = std::fabs(-(ryp.x - ctr.x) * sa + (ryp.y - ctr.y) * ca);
                if (rx < 1e-4f || ry < 1e-4f) {
                    st.addLog("[sketch] Ellipse: degenerate radii \xE2\x80\x94 pick the minor point off the major axis");
                    toolUV_.pop_back(); toolSnaps_.pop_back();
                } else {
                    const float kR2D = 57.29577951f;
                    nlohmann::json ent = {{"type", "ellipse"},
                        {"center", nlohmann::json::array({ctr.x, ctr.y})},
                        {"rx", rx}, {"ry", ry}, {"rot", rot * kR2D}};   // rot in degrees (schema)
                    addEntity(st, ent, nlohmann::json::array());
                    resetTool();
                }
            }
        }

        // C42: Polygon side count — number keys 3-9 (top row or keypad) while the tool is armed.
        if (tool_ == SketchTool::Polygon && !io.WantTextInput) {
            for (int d = 3; d <= 9; ++d) {
                if (ImGui::IsKeyPressed((ImGuiKey)(ImGuiKey_0 + d)) ||
                    ImGui::IsKeyPressed((ImGuiKey)(ImGuiKey_Keypad0 + d)))
                    polySides_ = d;
            }
        }

        // C42: Spline finishing — Enter / right-click / double-click commits (>= 2 points); a
        // right-click with fewer just drops the in-progress points (mirrors the Line-chain RMB).
        if (tool_ == SketchTool::Spline) {
            bool finish = false;
            if (imgHovered && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left) &&
                toolUV_.size() >= 3) {
                // The double-click's 2nd click pushed a duplicate point this frame — drop it.
                toolUV_.pop_back(); toolSnaps_.pop_back();
                finish = true;
            }
            if (!io.WantTextInput && toolUV_.size() >= 2 &&
                (ImGui::IsKeyPressed(ImGuiKey_Enter) || ImGui::IsKeyPressed(ImGuiKey_KeypadEnter)))
                finish = true;
            if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Right)) {
                if (toolUV_.size() >= 2) finish = true;
                else { toolUV_.clear(); toolSnaps_.clear(); }
            }
            if (finish && toolUV_.size() >= 2) {
                nlohmann::json pts = nlohmann::json::array();
                for (const auto& q : toolUV_) pts.push_back(nlohmann::json::array({q.x, q.y}));
                nlohmann::json ent = {{"type", "spline"}, {"points", pts}};
                addEntity(st, ent, nlohmann::json::array());
                resetTool();
            }
        }
        // C37-D: right-click ends the current chain/segment but keeps the tool armed (SW muscle
        // memory: the Line tool drops its in-progress points and waits for a fresh first point).
        // mouse_gestures is disabled in-sketch (viewport_panel), so the RMB is free here; consume it.
        // C42: Spline owns its RMB (finish/drop, handled above).
        if (tool_ != SketchTool::Spline && imgHovered &&
            ImGui::IsMouseClicked(ImGuiMouseButton_Right)) {
            toolUV_.clear(); toolSnaps_.clear(); chainPrevId_.clear();
        }
        // ESC ends the tool.
        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) cancelTool();
    }

    // ---- C37-D: SolidWorks-grade Smart Dimension state machine ----
    if (tool_ == SketchTool::SmartDim) {
        st.requestCursor(ui::CursorKind::Crosshair);
        const ImU32 hiCol = col(pal.accent);
        const ImU32 pickCol = col(pal.accent, 0.95f);
        // Highlight already-picked targets. C38 (S1): resolve the picked entity by id (ents_ may have
        // been refilled since the pick) — skip the highlight when it's gone.
        for (const auto& p : dimPicks_) {
            if (p.isPoint) { dl->AddCircle(uvToScreen(p.u, p.v), 6.0f, pickCol, 12, 2.0f); continue; }
            const SketchEntity* ep = entById(p.entId);
            if (ep) {
                const SketchEntity& e = *ep;
                if (e.type == "line")
                    dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), pickCol, 3.0f);
                else if (e.type == "circle" || e.type == "arc")
                    dl->AddCircle(uvToScreen(e.center[0], e.center[1]),
                                  std::hypot(uvToScreen(e.center[0] + e.radius, e.center[1]).x -
                                             uvToScreen(e.center[0], e.center[1]).x, 0.0f), pickCol, 48, 3.0f);
            }
        }
        // Hover highlight for the next pickable target (only while collecting picks).
        if (!dimHasPending_ && imgHovered && !dimPopupOpen_) {
            DimPick hp;
            if (dimHitTest(io.MousePos, hp)) {
                if (hp.isPoint) dl->AddCircle(uvToScreen(hp.u, hp.v), 6.0f, hiCol, 12, 2.0f);
                else if (hp.entIdx >= 0) {
                    const SketchEntity& e = ents_[hp.entIdx];
                    if (e.type == "line")
                        dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), hiCol, 2.5f);
                    else if (e.type == "circle" || e.type == "arc")
                        dl->AddCircle(uvToScreen(e.center[0], e.center[1]),
                                      std::hypot(uvToScreen(e.center[0] + e.radius, e.center[1]).x -
                                                 uvToScreen(e.center[0], e.center[1]).x, 0.0f), hiCol, 48, 2.5f);
                }
            }
        }

        // Phase A: collecting picks (no full combo yet). A click adds the hovered target.
        if (!dimPopupOpen_) {
            // Rebuild the pending dim from the current picks + live cursor (drives the preview AND
            // the live h/v/aligned variant choice for point-point).
            dimBuildPending(io.MousePos, mu, mv);

            if (dimHasPending_) {
                // LIVE PREVIEW: a full annotation following the cursor until the placement click.
                SketchConstraint pc;
                pc.type = dimType_; pc.axis = dimAxis_;
                pc.hasMeasured = true; pc.measured = dimMeasured_;
                if (dimPending_.contains("refs"))
                    for (const auto& rr : dimPending_["refs"]) pc.refs.push_back(rr.get<std::string>());
                drawDimension(dl, pc, hiCol, hiCol, true, mu, mv);
                // Placement click stores the placement and opens the Modify box.
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                    dimPlaceU_ = mu; dimPlaceV_ = mv;
                    dimOpenModify(io.MousePos);
                }
            } else if (imgHovered && dimHoverCid_.empty() &&
                       ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                // C47: skip when an existing dim's text is hovered — that click belongs to the
                // drag / double-click-edit handler above, not a new pick.
                DimPick pick;
                if (dimHitTest(io.MousePos, pick)) {
                    // Avoid duplicate picks of the same target. C38 (S1): compare entity picks by id
                    // (stable) rather than ents_ index (per-frame, may have shifted).
                    bool dup = false;
                    for (const auto& q : dimPicks_)
                        if (q.isPoint == pick.isPoint &&
                            (pick.isPoint ? q.handle == pick.handle : q.entId == pick.entId)) dup = true;
                    if (!dup) dimPicks_.push_back(pick);
                }
            }
        }

        // Esc semantics: with the Modify box open, item-4 handler closes it (below). With picks in
        // the buffer, first Esc clears the buffer; second Esc exits the tool.
        if (!dimPopupOpen_ && ImGui::IsKeyPressed(ImGuiKey_Escape)) {
            escHandled = true;   // C41 (T1): pick-buffer clear keeps the tool armed
            if (!dimPicks_.empty()) { dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr; }
            else cancelTool();
        }
        // Right-click also clears the pick buffer (consume it; gestures off in-sketch).
        if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Right)) {
            dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr;
        }
    }

    dl->PopClipRect();

    // ---- C37-D: SolidWorks confirmation corner (top-right): ✓ exit & keep, ✗ cancel & revert ----
    {
        ImDrawList* wdl = ImGui::GetWindowDrawList();
        const float sz = 30.0f, pad = 12.0f;
        // Two stacked square buttons in the very top-right corner.
        ImVec2 okMin(imgMax.x - pad - sz, imgMin.y + pad);
        ImVec2 okMax(okMin.x + sz, okMin.y + sz);
        ImVec2 cxMin(okMin.x, okMax.y + 6);
        ImVec2 cxMax(cxMin.x + sz, cxMin.y + sz);
        bool okOver = ImGui::IsMouseHoveringRect(okMin, okMax);
        bool cxOver = ImGui::IsMouseHoveringRect(cxMin, cxMax);
        // ✓ keep
        wdl->AddRectFilled(okMin, okMax, col(pal.statusOk, okOver ? 0.95f : 0.80f), 5.0f);
        {
            ImVec2 c(okMin.x + sz * 0.5f, okMin.y + sz * 0.5f);
            wdl->AddLine(ImVec2(c.x - 7, c.y + 0), ImVec2(c.x - 2, c.y + 6), col(pal.windowBg), 2.6f);
            wdl->AddLine(ImVec2(c.x - 2, c.y + 6), ImVec2(c.x + 8, c.y - 6), col(pal.windowBg), 2.6f);
        }
        // ✗ cancel
        wdl->AddRectFilled(cxMin, cxMax, col(pal.statusErr, cxOver ? 0.95f : 0.80f), 5.0f);
        {
            ImVec2 c(cxMin.x + sz * 0.5f, cxMin.y + sz * 0.5f);
            wdl->AddLine(ImVec2(c.x - 6, c.y - 6), ImVec2(c.x + 6, c.y + 6), col(pal.windowBg), 2.6f);
            wdl->AddLine(ImVec2(c.x - 6, c.y + 6), ImVec2(c.x + 6, c.y - 6), col(pal.windowBg), 2.6f);
        }
        if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && (okOver || cxOver)) {
            // C38 (S8): if the Smart Dimension Modify box is open when ✓/✗ is clicked, the pending
            // dimension is INTENTIONALLY DISCARDED (the user chose to leave the sketch instead of
            // finishing the dim). Close the box and clear the pick buffer explicitly FIRST so
            // resetTool/exit teardown can't leave half-state behind, then run exit/cancel.
            if (dimPopupOpen_) {
                dimPopupOpen_ = false; dimError_.clear(); dimEditCid_.clear();
                dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr;
                dimAxis_.clear(); dimPopupDriven_ = false;
            }
            if (okOver)      exitSketch(st, /*keep=*/true);
            else if (cxOver) cancelSketch(st);
        }
        if (!active_) return;   // confirmation corner closed the sketch this frame (clip already popped)
    }

    // ---- tool hint chip (top-center) ----
    if (tool_ != SketchTool::None) {
        const char* tn = tool_ == SketchTool::Line ? "Line" :
                         tool_ == SketchTool::RectCorner ? "Rectangle" :
                         tool_ == SketchTool::Circle ? "Circle" :
                         tool_ == SketchTool::ArcCenter ? "Center Arc" :
                         tool_ == SketchTool::Slot ? "Slot" :
                         tool_ == SketchTool::SmartDim ? "Smart Dimension" :
                         tool_ == SketchTool::Rect3ptCorner ? "3 Point Corner Rectangle" :
                         tool_ == SketchTool::Rect3ptCenter ? "3 Point Center Rectangle" :
                         tool_ == SketchTool::Parallelogram ? "Parallelogram" :
                         tool_ == SketchTool::CirclePerimeter ? "Perimeter Circle" :
                         tool_ == SketchTool::SlotCtrStr ? "Centerpoint Slot" :
                         tool_ == SketchTool::SlotArcCtr ? "Centerpoint Arc Slot" :
                         tool_ == SketchTool::SlotArc3pt ? "3 Point Arc Slot" :
                         tool_ == SketchTool::EllipsePartial ? "Partial Ellipse" :
                         tool_ == SketchTool::Parabola ? "Parabola" :
                         tool_ == SketchTool::Conic ? "Conic" :
                         tool_ == SketchTool::Trim ? "Trim" :
                         tool_ == SketchTool::Extend ? "Extend" :
                         tool_ == SketchTool::Offset ? "Offset" :
                         tool_ == SketchTool::Arc3pt ? "3 Point Arc" :
                         tool_ == SketchTool::Polygon ? "Polygon" :
                         tool_ == SketchTool::RectCenter ? "Center Rectangle" :
                         tool_ == SketchTool::Point ? "Point" :
                         tool_ == SketchTool::Ellipse ? "Ellipse" :
                         tool_ == SketchTool::Spline ? "Spline" : "";
        // Per-stage step prompt (status-bar style) for the multi-click variants.
        const size_t n = toolUV_.size();
        const char* step = nullptr;
        switch (tool_) {
            case SketchTool::Line:   // C37-D: SW muscle-memory hint
                step = n == 0 ? "click start point" : "click next point \xC2\xB7 right-click to end chain"; break;
            case SketchTool::RectCorner:
                step = n == 0 ? "click first corner" : "click opposite corner"; break;
            case SketchTool::Circle:
                step = n == 0 ? "click center" : "click radius"; break;
            case SketchTool::ArcCenter:
                step = n == 0 ? "click center" : n == 1 ? "click start" : "click end"; break;
            case SketchTool::Slot:
                step = n == 0 ? "click start" : n == 1 ? "click end" : "click width"; break;
            case SketchTool::SmartDim:
                step = dimHasPending_ ? "click to place the dimension"
                     : dimPicks_.empty() ? "click an entity or point to dimension"
                     : "click another entity/point, or place"; break;
            case SketchTool::Rect3ptCorner:
                step = n == 0 ? "click corner" : n == 1 ? "click edge end" : "click width"; break;
            case SketchTool::Rect3ptCenter:
                step = n == 0 ? "click center" : n == 1 ? "click edge midpoint" : "click width"; break;
            case SketchTool::Parallelogram:
                step = n == 0 ? "click corner 1" : n == 1 ? "click corner 2" : "click corner 3"; break;
            case SketchTool::CirclePerimeter:
                step = n < 3 ? "click 3 points on the circumference" : nullptr; break;
            case SketchTool::SlotCtrStr:
                step = n == 0 ? "click center" : n == 1 ? "click end (length/dir)" : "click width"; break;
            case SketchTool::SlotArcCtr:
                step = n == 0 ? "click arc center" : n == 1 ? "click spine start" :
                       n == 2 ? "click spine end" : "click width"; break;
            case SketchTool::SlotArc3pt:
                step = n < 3 ? "click 3 points on the arc spine" : "click width"; break;
            case SketchTool::EllipsePartial:
                step = n == 0 ? "click center" : n == 1 ? "click major (rx/rot)" :
                       n == 2 ? "click minor (ry)" : n == 3 ? "click start angle" : "click end angle"; break;
            case SketchTool::Parabola:
                step = n == 0 ? "click focus" : n == 1 ? "click vertex" :
                       n == 2 ? "click start" : "click end"; break;
            case SketchTool::Conic:
                step = n == 0 ? "click endpoint 1" : n == 1 ? "click endpoint 2" :
                       n == 2 ? "click apex" : "click to set rho (fullness)"; break;
            case SketchTool::Trim:
                step = "hover a segment (red), click to trim"; break;
            case SketchTool::Extend:
                step = "hover near an end, click to extend"; break;
            case SketchTool::Offset:
                step = offsetStage_ == 0 ? "click entities for the chain, then Enter"
                     : offsetStage_ == 1 ? "type the offset distance, Enter"
                     : "click the side to offset toward"; break;
            case SketchTool::Arc3pt:
                step = n == 0 ? "click arc start" : n == 1 ? "click arc end"
                     : "click a point on the arc"; break;
            case SketchTool::Polygon:
                step = n == 0 ? "click center" : "click a vertex"; break;
            case SketchTool::RectCenter:
                step = n == 0 ? "click center" : "click a corner"; break;
            case SketchTool::Point:
                step = "click to place a point"; break;
            case SketchTool::Ellipse:
                step = n == 0 ? "click center" : n == 1 ? "click major-axis end" : "click minor (ry)"; break;
            case SketchTool::Spline:
                step = n < 2 ? "click points; Enter / right-click / double-click to finish"
                             : "click more points \xC2\xB7 Enter / right-click / double-click finishes"; break;
            default: break;
        }
        std::string hint = std::string(tn) + (step ? std::string(" \xE2\x80\x94 ") + step : std::string());
        if (tool_ == SketchTool::Polygon) {   // C42: live side count + how to change it
            char sb[48];
            std::snprintf(sb, sizeof(sb), "  [Sides: %d \xC2\xB7 press 3-9]", polySides_);
            hint += sb;
        }
        hint += "  (ESC to end)";
        ImVec2 ts = ImGui::CalcTextSize(hint.c_str());
        float cx = (imgMin.x + imgMax.x) * 0.5f;
        ImVec2 p0(cx - ts.x * 0.5f - 12, imgMin.y + 12);
        ImVec2 p1(cx + ts.x * 0.5f + 12, p0.y + ts.y + 10);
        ImDrawList* wdl = ImGui::GetWindowDrawList();
        wdl->AddRectFilled(p0, p1, col(pal.overlayBg), 6.0f);
        wdl->AddRect(p0, p1, col(pal.overlayBorder), 6.0f);
        wdl->AddText(ImVec2(p0.x + 12, p0.y + 5), col(pal.overlayText), hint.c_str());
    }

    // ---- status chip (top-LEFT now; confirmation corner owns the top-right) ----
    {
        std::string sl = statusLine();
        // C41 (T4): neutral pill while the sketch is empty (statusLine shows the draw hint).
        ImU32 sc = ents_.empty()  ? col(pal.overlayText, 0.9f)
                 : status_ == "over" ? col(pal.statusErr)
                 : status_ == "full" ? col(pal.statusOk) : col(pal.sketchUnder);
        ImVec2 ts = ImGui::CalcTextSize(sl.c_str());
        ImVec2 p0(imgMin.x + 12, imgMin.y + 12);
        ImVec2 p1b(p0.x + ts.x + 20, p0.y + ts.y + 10);
        ImDrawList* wdl = ImGui::GetWindowDrawList();
        wdl->AddRectFilled(p0, p1b, col(pal.overlayBg), 6.0f);
        wdl->AddRect(p0, p1b, sc, 6.0f, 0, 1.5f);
        wdl->AddText(ImVec2(p0.x + 10, p0.y + 5), sc, sl.c_str());
    }

    // ---- C37-D: Smart Dimension Modify box ----
    // Auto-focused + text pre-selected, +/- step buttons, unit suffix, Driven checkbox, Enter
    // commits, Esc closes WITHOUT committing but stays in the tool (pick buffer cleared). Expression
    // input ("=...") keeps working.
    if (dimPopupOpen_) {
        ImGui::SetNextWindowPos(dimPopupPos_, ImGuiCond_Appearing);
        ImGui::SetNextWindowBgAlpha(0.98f);
        if (ImGui::Begin("##SketchDim", nullptr,
                         ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                         ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
            ImGui::TextUnformatted(dimEditCid_.empty() ? "Dimension" : "Edit Dimension");
            // +/- step buttons adjust the numeric value (skipped for an expr input).
            const char* suffix = dimIsAngle_ ? "\xC2\xB0" : "mm";
            auto stepVal = [&](double delta) {
                std::string txt = dimBuf_.data();
                if (!txt.empty() && txt[0] == '=') return;   // don't mutate expressions
                double cur = dimMeasured_; std::string perr;
                if (pmParseDim(txt, cur, perr)) {}           // parse current text if valid
                cur += delta;
                std::snprintf(dimBuf_.data(), dimBuf_.size(), "%s", fmtLen(cur).c_str());
            };
            if (ImGui::Button("-")) stepVal(-dimPopupStep_);
            ImGui::SameLine();
            ImGui::SetNextItemWidth(120);
            // Auto-focus + select-all on first frame of the box.
            if (dimPopupFocus_) { ImGui::SetKeyboardFocusHere(); }
            bool enter = ImGui::InputText("##dimval", dimBuf_.data(), dimBuf_.size(),
                                          ImGuiInputTextFlags_EnterReturnsTrue |
                                          (dimPopupFocus_ ? ImGuiInputTextFlags_AutoSelectAll : 0));
            dimPopupFocus_ = false;
            ImGui::SameLine();
            ImGui::TextUnformatted(suffix);
            ImGui::SameLine();
            if (ImGui::Button("+")) stepVal(dimPopupStep_);
            ImGui::Checkbox("Driven", &dimPopupDriven_);
            if (!dimError_.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1));
                ImGui::TextUnformatted(dimError_.c_str());
                ImGui::PopStyleColor();
            }
            bool ok = ImGui::Button("OK") || enter;
            ImGui::SameLine();
            bool cancel = ImGui::Button("Cancel");
            if (ok) dimCommit(st);
            // Esc (or Cancel) closes the box WITHOUT committing; stay in Smart Dimension. Clearing
            // the pick buffer mirrors SolidWorks (the dimension is abandoned, tool stays armed).
            if (cancel || (ImGui::IsKeyPressed(ImGuiKey_Escape) && !ImGui::IsKeyDown(ImGuiKey_LeftCtrl))) {
                escHandled = true;   // C41 (T1): box close keeps Smart Dimension armed
                dimPopupOpen_ = false; dimError_.clear();
                if (dimEditCid_.empty()) { dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr; }
                dimEditCid_.clear();
            }
        }
        ImGui::End();
    }

    // ---- Offset distance overlay (small InputFloat near the cursor; Enter -> side-pick stage) ----
    if (tool_ == SketchTool::Offset && offsetStage_ == 1) {
        ImGui::SetNextWindowPos(offsetOverlayPos_, ImGuiCond_Appearing);
        ImGui::SetNextWindowBgAlpha(0.98f);
        if (ImGui::Begin("##SketchOffsetDist", nullptr,
                         ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                         ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
            ImGui::TextUnformatted("Offset distance (mm)");
            ImGui::SetNextItemWidth(120);
            if (offsetFocusInput_) { ImGui::SetKeyboardFocusHere(); offsetFocusInput_ = false; }
            bool enter = ImGui::InputFloat("##offdist", &offsetDist_, 0.0f, 0.0f, "%.3f",
                                           ImGuiInputTextFlags_EnterReturnsTrue);
            bool ok = ImGui::Button("Next") || enter;
            ImGui::SameLine();
            bool cancel = ImGui::Button("Cancel");
            if (ok) { offsetDist_ = std::fabs(offsetDist_); offsetStage_ = 2; }
            if (cancel) { offsetStage_ = 0; }
        }
        ImGui::End();
    }

    // ---- C42: minimal Add Relation popup (ribbon sk_relations over a 1-2 entity selection) ----
    // Lists the relations applicable to the snapshotted selection's entity types and emits ONE
    // sketch_add_constraint. Closes if any referenced entity vanished (undo/delete mid-popup).
    if (relPopupOpen_) {
        for (const auto& id : relIds_)
            if (!entById(id)) { relPopupOpen_ = false; relIds_.clear(); break; }
    }
    if (relPopupOpen_) {
        const SketchEntity* a = entById(relIds_[0]);
        const SketchEntity* b = relIds_.size() > 1 ? entById(relIds_[1]) : nullptr;
        auto isCirc = [](const SketchEntity* e) {
            return e && (e->type == "circle" || e->type == "arc");
        };
        ImGui::SetNextWindowPos(relPopupPos_, ImGuiCond_Appearing);
        ImGui::SetNextWindowBgAlpha(0.98f);
        if (ImGui::Begin("##SketchRel", nullptr,
                         ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                         ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
            ImGui::TextUnformatted("Add Relation");
            ImGui::Separator();
            bool any = false;
            auto rel = [&](const char* label, const char* type, bool pair) {
                any = true;
                if (!ImGui::MenuItem(label)) return;
                nlohmann::json refs = nlohmann::json::array({relIds_[0]});
                if (pair) refs.push_back(relIds_[1]);
                addConstraint(st, {{"type", type}, {"refs", refs}});   // errors logged + re-solve
                relPopupOpen_ = false; relIds_.clear();
            };
            if (!b) {
                if (a->type == "line") { rel("Horizontal", "horizontal", false);
                                         rel("Vertical",   "vertical",   false); }
                rel("Fix", "fix", false);
            } else {
                bool aLine = a->type == "line", bLine = b->type == "line";
                if (aLine && bLine) { rel("Parallel",      "parallel",      true);
                                      rel("Perpendicular", "perpendicular", true);
                                      rel("Equal",         "equal",         true); }
                else if (isCirc(a) && isCirc(b)) { rel("Equal",      "equal",      true);
                                                   rel("Concentric", "concentric", true); }
                else if ((aLine && isCirc(b)) || (isCirc(a) && bLine))
                    rel("Tangent", "tangent", true);
            }
            if (!any) ImGui::TextDisabled("No relations for this selection");
            ImGui::Separator();
            if (ImGui::MenuItem("Cancel") ||
                (relPopupOpen_ && ImGui::IsKeyPressed(ImGuiKey_Escape))) {
                escHandled = true;
                relPopupOpen_ = false; relIds_.clear();
            }
        }
        ImGui::End();
    }

    // ---- C41 (T1): catch-all Esc — disarm the armed tool / in-progress chain even when the
    // viewport image is NOT hovered. The more specific handlers above run first: the ones that
    // fully cancel leave tool_ == None (no-op here); the ones that keep the tool armed (Modify
    // box close, Smart Dim pick clear, Offset stage back-out) mark escHandled. Suppressed while
    // any text field owns the keyboard. Esc with no tool armed does nothing here.
    if (!escHandled && tool_ != SketchTool::None && !io.WantTextInput &&
        ImGui::IsKeyPressed(ImGuiKey_Escape))
        cancelTool();
}

}  // namespace ui
