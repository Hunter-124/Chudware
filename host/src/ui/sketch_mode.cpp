// Sketch mode UI (C18) implementation. See sketch_mode.h for the design overview.
#include "sketch_mode.h"

#include <cmath>
#include <cstdio>
#include <cstring>

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

void SketchMode::applyEnterResult(const nlohmann::json& res) {
    ents_.clear();
    dof_ = res.value("dof", 0);
    status_ = res.value("status", std::string("under"));
    if (res.contains("plane_frame") && res["plane_frame"].is_array() &&
        res["plane_frame"].size() == 16) {
        for (int i = 0; i < 16; ++i) frame_[i] = res["plane_frame"][i].get<float>();
    }
    const nlohmann::json* stats = res.contains("statuses") ? &res["statuses"] : nullptr;
    if (res.contains("entities") && res["entities"].is_array()) {
        for (const auto& e : res["entities"]) {
            SketchEntity se;
            se.id = e.value("id", std::string());
            se.type = e.value("type", std::string());
            se.construction = e.value("construction", false);
            if (stats && stats->contains(se.id)) se.status = (*stats)[se.id].get<std::string>();
            else se.status = "under";
            auto v2 = [&](const char* k, float out[2]) {
                if (e.contains(k) && e[k].is_array() && e[k].size() == 2) {
                    out[0] = e[k][0].get<float>(); out[1] = e[k][1].get<float>();
                }
            };
            v2("p1", se.p1); v2("p2", se.p2); v2("center", se.center);
            se.radius = e.value("radius", 0.0f);
            se.start = e.value("start", 0.0f);
            se.end = e.value("end", 0.0f);
            se.ccw = e.value("ccw", true);
            se.width = e.value("width", 0.0f);
            ents_.push_back(se);
        }
    }
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
    // Lock the camera looking down the plane normal (frame_ column 2 = z_dir in world).
    float nz0 = frame_[2], nz1 = frame_[6], nz2 = frame_[10];
    // yaw/pitch from the normal: pitch = asin(nz2), yaw = atan2(nz1, nz0).
    camPitch_ = std::asin(std::fmax(-1.0f, std::fmin(1.0f, nz2))) * 57.2957795f;
    camYaw_ = std::atan2(nz1, nz0) * 57.2957795f;
    if (std::fabs(camPitch_) > 88.9f) camPitch_ = camPitch_ > 0 ? 89.0f : -89.0f;
    st.yaw = camYaw_; st.pitch = camPitch_;
    st.addLog("[sketch] entered " + fid_ + "  (dof=" + std::to_string(dof_) + ")");
    return true;
}

bool SketchMode::enterNewOnSelection(AppState& st) {
    if (!st.docMirror.hasDoc()) {
        std::string err;
        if (!st.docMirror.newPart(st.worker, err)) { st.addLog("[sketch] newPart failed: " + err); return false; }
    }
    // Plane ref: a resolved planar face/datum selector from the current selection, else plane.top.
    nlohmann::json planeRef = "plane.top";
    for (const auto& it : st.selection.items()) {
        if (!it.selector.empty()) {
            try {
                nlohmann::json sel = nlohmann::json::parse(it.selector);
                // Faces (and datum selectors) are valid sketch planes; edges/vertices are not.
                std::string k = sel.value("kind", std::string());
                if (k == "face" || k == "datum") { planeRef = sel; break; }
            } catch (...) {}
        }
    }
    nlohmann::json ops = nlohmann::json::array({
        {{"op", "add_feature"}, {"feature", {
            {"type", "sketch"}, {"name", "Sketch"},
            {"refs", {{"plane", planeRef}}},
            {"params", {{"entities", nlohmann::json::array()},
                        {"constraints", nlohmann::json::array()}}}}}}});
    auto r = st.docMirror.applyOps(st.worker, ops);
    if (!r.ok) { st.addLog("[sketch] create sketch failed: " + r.error_message); return false; }
    std::string newFid = (r.id_map.is_object() && r.id_map.contains("$0"))
                       ? r.id_map["$0"].get<std::string>() : std::string();
    if (newFid.empty()) { st.addLog("[sketch] create sketch: no id"); return false; }
    if (!enterForFeature(st, newFid)) return false;
    isNewEmpty_ = true;
    return true;
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
    status_.clear();
    dof_ = 0;
    resetTool();
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
    if (cmdId == "sk_ellipse_partial") { resetTool(); tool_ = SketchTool::EllipsePartial; return true; }
    if (cmdId == "sk_parabola")        { resetTool(); tool_ = SketchTool::Parabola;       return true; }
    if (cmdId == "sk_conic")           { resetTool(); tool_ = SketchTool::Conic;          return true; }
    // sk_slot_arc_3pt / sk_slot_arc_ctr intentionally NOT handled: the slot entity schema is
    // straight-only (p1,p2,width — no arc spine), so they stay greyed (see ribbon_def.cpp).
    return false;
}

// ---------------------------------------------------------------------------
// tool helpers
// ---------------------------------------------------------------------------

void SketchMode::resetTool() { tool_ = SketchTool::None; toolUV_.clear(); toolSnaps_.clear();
                               conicRho_ = 0.5f;
                               dimPopupOpen_ = false; dimPending_ = nullptr; }
void SketchMode::cancelTool() { resetTool(); }

void SketchMode::snapPoint(float& u, float& v, std::string& snapHandle) const {
    snapHandle.clear();
    // Endpoint coincident snap: search entity endpoints within kSnapPx in SCREEN space.
    ImVec2 cur = uvToScreen(u, v);
    float best = kSnapPx * kSnapPx;
    auto consider = [&](const std::string& handle, float eu, float ev) {
        ImVec2 s = uvToScreen(eu, ev);
        float dx = s.x - cur.x, dy = s.y - cur.y;
        float d2 = dx * dx + dy * dy;
        if (d2 < best) { best = d2; snapHandle = handle; u = eu; v = ev; }
    };
    for (const auto& e : ents_) {
        if (e.type == "line" || e.type == "slot") {
            consider(e.id + ".p1", e.p1[0], e.p1[1]);
            consider(e.id + ".p2", e.p2[0], e.p2[1]);
        } else if (e.type == "circle" || e.type == "arc") {
            consider(e.id + ".center", e.center[0], e.center[1]);
        } else if (e.type == "point") {
            consider(e.id + ".p", e.p1[0], e.p1[1]);
        }
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
        } else if (e.type == "circle" || e.type == "arc") {
            consider(e.id + ".center", e.center[0], e.center[1]);
        } else if (e.type == "point") {
            consider(e.id + ".p", e.p1[0], e.p1[1]);
        }
    }
    return hit;
}

// ---------------------------------------------------------------------------
// status
// ---------------------------------------------------------------------------

std::string SketchMode::statusLine() const {
    if (!active_) return {};
    const char* s = status_ == "full" ? "Fully Defined"
                  : status_ == "over" ? "Over Defined" : "Under Defined";
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%s   DOF: %d", s, dof_);
    return std::string(buf);
}

// ---------------------------------------------------------------------------
// overlay draw + interaction
// ---------------------------------------------------------------------------

void SketchMode::drawOverlay(AppState& st, ImGuiIO& io, const ImVec2& imgMin, const ImVec2& imgMax,
                             int vw, int vh, bool imgHovered) {
    if (!active_ || !st.renderer) return;
    buildBasis(*st.renderer, imgMin, vw, vh);
    if (!basisOk_) return;

    const auto& pal = ui::palette;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->PushClipRect(imgMin, imgMax, true);

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
        if (e.status == "full") return col(pal.text);
        return col(pal.sketchUnder);   // under-defined (blue-ish)
    };
    for (const auto& e : ents_) {
        ImU32 cc = statusCol(e);
        float th = e.construction ? 1.0f : 2.0f;
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
        } else if (e.type == "point") {
            dl->AddCircleFilled(uvToScreen(e.p1[0], e.p1[1]), 3.5f, cc);
        }
    }

    // ---- selection / drag handles + interaction ----
    float mu = 0, mv = 0;
    if (imgHovered) screenToUV(io.MousePos, mu, mv);

    // Drag (only when no drawing tool is active): hit-test handles, drag with sketch_drag, release
    // commits. At most one sketch_drag per frame (throttled by dragFrame_).
    if (tool_ == SketchTool::None) {
        if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !dragging_) {
            std::string h; float hu, hv;
            if (hitTestHandle(io.MousePos, h, hu, hv) > 0) {
                dragging_ = true; dragHandle_ = h; dragFrame_ = -1;
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
    }

    // ---- drawing tools: rubber-band preview + click handling ----
    if (tool_ != SketchTool::None && tool_ != SketchTool::SmartDim) {
        // Snap the live cursor for preview + inference glyphs.
        float su = mu, sv = mv; std::string snapH;
        if (imgHovered) snapPoint(su, sv, snapH);
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
            }
        }

        // Cursor marker + inference glyph.
        dl->AddCircle(sCur, 4.0f, prevCol, 12, 1.5f);
        if (!snapH.empty()) {  // coincident inference glyph (small square)
            dl->AddRect(ImVec2(sCur.x - 6, sCur.y - 6), ImVec2(sCur.x + 6, sCur.y + 6),
                        col(pal.accent), 0, 0, 1.6f);
        }

        // Click placement.
        if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
            toolUV_.push_back(ImVec2(su, sv));
            toolSnaps_.push_back(snapH);
            // Tools that complete on a fixed number of clicks:
            if (tool_ == SketchTool::Line && toolUV_.size() == 2) {
                ImVec2 p1 = toolUV_[0], p2 = toolUV_[1];
                nlohmann::json cs = nlohmann::json::array();
                // Auto h/v relation if within tolerance.
                float du = p2.x - p1.x, dv = p2.y - p1.y;
                float ang = std::atan2(std::fabs(dv), std::fabs(du)) * 57.2957795f;
                if (ang < kHVDeg) cs.push_back({{"type", "horizontal"}, {"refs", {"$self"}}});
                else if (ang > 90.0f - kHVDeg) cs.push_back({{"type", "vertical"}, {"refs", {"$self"}}});
                // Coincident on the start endpoint if it snapped.
                if (!toolSnaps_[0].empty())
                    cs.push_back({{"type", "coincident"}, {"refs", {toolSnaps_[0], "$self.p1"}}});
                nlohmann::json ent = {{"type", "line"},
                    {"p1", nlohmann::json::array({p1.x, p1.y})},
                    {"p2", nlohmann::json::array({p2.x, p2.y})}};
                addEntity(st, ent, cs);
                // Chain: keep drawing from the new endpoint.
                ImVec2 chainStart = p2;
                resetTool(); tool_ = SketchTool::Line;
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
            }
        }
        // ESC ends the tool.
        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) cancelTool();
    }

    // ---- Smart Dimension ----
    if (tool_ == SketchTool::SmartDim) {
        // Click an entity (line -> distance/length, circle/arc -> radius). For simplicity we
        // dimension the nearest entity's defining measure and pop the value editor.
        if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !dimPopupOpen_) {
            // find nearest entity by handle hit OR by line proximity
            std::string h; float hu, hv;
            const SketchEntity* picked = nullptr;
            float bestD = 14.0f * 14.0f;
            for (const auto& e : ents_) {
                if (e.type == "line") {
                    ImVec2 a = uvToScreen(e.p1[0], e.p1[1]), b = uvToScreen(e.p2[0], e.p2[1]);
                    // distance from mouse to segment
                    float vx = b.x - a.x, vy = b.y - a.y;
                    float wx = io.MousePos.x - a.x, wy = io.MousePos.y - a.y;
                    float t = (vx * wx + vy * wy) / std::fmax(1e-6f, vx * vx + vy * vy);
                    t = std::fmax(0.0f, std::fmin(1.0f, t));
                    float px = a.x + t * vx, py = a.y + t * vy;
                    float d2 = (px - io.MousePos.x) * (px - io.MousePos.x) +
                               (py - io.MousePos.y) * (py - io.MousePos.y);
                    if (d2 < bestD) { bestD = d2; picked = &e; }
                } else if (e.type == "circle" || e.type == "arc") {
                    ImVec2 cc = uvToScreen(e.center[0], e.center[1]);
                    float rpx = std::hypot(uvToScreen(e.center[0] + e.radius, e.center[1]).x - cc.x,
                                           uvToScreen(e.center[0] + e.radius, e.center[1]).y - cc.y);
                    float dm = std::hypot(io.MousePos.x - cc.x, io.MousePos.y - cc.y);
                    float d2 = (dm - rpx) * (dm - rpx);
                    if (d2 < bestD) { bestD = d2; picked = &e; }
                }
            }
            if (picked) {
                if (picked->type == "line") {
                    double L = std::hypot(picked->p2[0] - picked->p1[0], picked->p2[1] - picked->p1[1]);
                    dimPending_ = {{"type", "distance"},
                        {"refs", nlohmann::json::array({picked->id + ".p1", picked->id + ".p2"})},
                        {"value", {{"value", L}, {"expr", nullptr}}}};
                    std::snprintf(dimBuf_.data(), dimBuf_.size(), "%.3g", L);
                } else {
                    dimPending_ = {{"type", "radius"},
                        {"refs", nlohmann::json::array({picked->id})},
                        {"value", {{"value", picked->radius}, {"expr", nullptr}}}};
                    std::snprintf(dimBuf_.data(), dimBuf_.size(), "%.3g", picked->radius);
                }
                dimError_.clear();
                dimPopupOpen_ = true;
                dimPopupPos_ = io.MousePos;
            }
        }
        if (ImGui::IsKeyPressed(ImGuiKey_Escape) && !dimPopupOpen_) cancelTool();
    }

    dl->PopClipRect();

    // ---- Exit Sketch chip (top-left of the viewport) ----
    {
        const char* lbl = "Exit Sketch";
        ImVec2 ts = ImGui::CalcTextSize(lbl);
        ImVec2 p0(imgMin.x + 12, imgMin.y + 12);
        ImVec2 p1(p0.x + ts.x + 24, p0.y + ts.y + 12);
        bool over = ImGui::IsMouseHoveringRect(p0, p1);
        ImDrawList* wdl = ImGui::GetWindowDrawList();
        wdl->AddRectFilled(p0, p1, col(pal.accent, over ? 0.95f : 0.80f), (p1.y - p0.y) * 0.5f);
        wdl->AddText(ImVec2(p0.x + 12, p0.y + 6), col(pal.windowBg), lbl);
        if (over && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) exitSketch(st, /*keep=*/true);
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
                         tool_ == SketchTool::EllipsePartial ? "Partial Ellipse" :
                         tool_ == SketchTool::Parabola ? "Parabola" :
                         tool_ == SketchTool::Conic ? "Conic" : "";
        // Per-stage step prompt (status-bar style) for the multi-click variants.
        const size_t n = toolUV_.size();
        const char* step = nullptr;
        switch (tool_) {
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
            case SketchTool::EllipsePartial:
                step = n == 0 ? "click center" : n == 1 ? "click major (rx/rot)" :
                       n == 2 ? "click minor (ry)" : n == 3 ? "click start angle" : "click end angle"; break;
            case SketchTool::Parabola:
                step = n == 0 ? "click focus" : n == 1 ? "click vertex" :
                       n == 2 ? "click start" : "click end"; break;
            case SketchTool::Conic:
                step = n == 0 ? "click endpoint 1" : n == 1 ? "click endpoint 2" :
                       n == 2 ? "click apex" : "click to set rho (fullness)"; break;
            default: break;
        }
        std::string hint = std::string(tn) + (step ? std::string(" \xE2\x80\x94 ") + step : std::string()) +
                           "  (ESC to end)";
        ImVec2 ts = ImGui::CalcTextSize(hint.c_str());
        float cx = (imgMin.x + imgMax.x) * 0.5f;
        ImVec2 p0(cx - ts.x * 0.5f - 12, imgMin.y + 12);
        ImVec2 p1(cx + ts.x * 0.5f + 12, p0.y + ts.y + 10);
        ImDrawList* wdl = ImGui::GetWindowDrawList();
        wdl->AddRectFilled(p0, p1, col(pal.overlayBg), 6.0f);
        wdl->AddRect(p0, p1, col(pal.overlayBorder), 6.0f);
        wdl->AddText(ImVec2(p0.x + 12, p0.y + 5), col(pal.overlayText), hint.c_str());
    }

    // ---- status chip (top-right): Under/Fully/Over Defined + DOF ----
    {
        std::string sl = statusLine();
        ImU32 sc = status_ == "over" ? col(pal.statusErr)
                 : status_ == "full" ? col(pal.statusOk) : col(pal.sketchUnder);
        ImVec2 ts = ImGui::CalcTextSize(sl.c_str());
        ImVec2 p1(imgMax.x - 12, imgMin.y + 12);
        ImVec2 p0(p1.x - ts.x - 20, p1.y);
        ImVec2 p1b(p1.x, p1.y + ts.y + 10);
        ImDrawList* wdl = ImGui::GetWindowDrawList();
        wdl->AddRectFilled(p0, p1b, col(pal.overlayBg), 6.0f);
        wdl->AddRect(p0, p1b, sc, 6.0f, 0, 1.5f);
        wdl->AddText(ImVec2(p0.x + 10, p0.y + 5), sc, sl.c_str());
    }

    // ---- Smart Dimension value popup ----
    if (dimPopupOpen_) {
        ImGui::SetNextWindowPos(dimPopupPos_, ImGuiCond_Appearing);
        ImGui::SetNextWindowBgAlpha(0.98f);
        if (ImGui::Begin("##SketchDim", nullptr,
                         ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                         ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
            ImGui::TextUnformatted("Dimension");
            ImGui::SetNextItemWidth(140);
            bool enter = ImGui::InputText("##dimval", dimBuf_.data(), dimBuf_.size(),
                                          ImGuiInputTextFlags_EnterReturnsTrue);
            if (!dimError_.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1));
                ImGui::TextUnformatted(dimError_.c_str());
                ImGui::PopStyleColor();
            }
            bool ok = ImGui::Button("OK") || enter;
            ImGui::SameLine();
            bool cancel = ImGui::Button("Cancel");
            if (ok) {
                // Parse: leading '=' = expression binding; else mm/in via pmParseDim.
                std::string txt = dimBuf_.data();
                nlohmann::json con = dimPending_;
                bool good = true;
                if (!txt.empty() && txt[0] == '=') {
                    con["value"] = {{"value", con["value"].value("value", 0.0)},
                                    {"expr", txt.substr(1)}};
                } else {
                    double mm = 0; std::string perr;
                    if (pmParseDim(txt, mm, perr)) {
                        con["value"] = {{"value", mm}, {"expr", nullptr}};
                    } else { dimError_ = perr.empty() ? "bad value" : perr; good = false; }
                }
                if (good) {
                    addConstraint(st, con);
                    dimPopupOpen_ = false; dimPending_ = nullptr;
                }
            }
            if (cancel) { dimPopupOpen_ = false; dimPending_ = nullptr; dimError_.clear(); }
        }
        ImGui::End();
    }
}

}  // namespace ui
