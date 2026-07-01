// Sketch mode (H5 port from host/src/ui/sketch_mode.cpp). See sketch_mode.h.
#include "sketch_mode.h"
#include "sketch_internal.h"
#include "sketch_seam.h"
#include "sketch_palette.h"
#include "airfoil.h"
#include "platform/file_dialog.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <functional>
#include <set>

using ui::sketch::SketchHost;
using ui::sketch::worldToScreen;
using ui::sketch::screenRay;
using ui::sketch::arcSweepContains;
using ui::sketch::snapHasPrefix;
using ui::sketch::parseEntities;
using ui::sketch::resyncMirror;
#define skPal ui::sketch::sketchPalette()

namespace ui {
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

bool SketchMode::callSketch(SketchHost& st, const char* method, const nlohmann::json& extra,
                            nlohmann::json& result) {
    if (!st.docMirror().hasDoc() || !st.workerStarted()) return false;
    nlohmann::json params = extra;
    params["doc_id"] = st.docMirror().docId();
    params["sketch"] = fid_;
    std::string err;
    if (!st.worker().call(method, params, result, err)) {
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

// C50f: read-only over-define pre-flight. Wraps the sketch_probe RPC. Returns true and fills the
// out-params on success; false on transport / RPC error (callSketch already logged it) so the
// caller can fall back to committing normally — a probe failure must never block the user.
bool SketchMode::sketchProbe(SketchHost& st, const nlohmann::json& action, bool& wouldOverDefine,
                             std::vector<std::string>& conflicting,
                             std::vector<std::string>& redundant) {
    nlohmann::json res;
    nlohmann::json extra = {{"action", action}};
    if (!callSketch(st, "sketch_probe", extra, res)) return false;
    // sketch_probe reports failure as {"ok": false, "error": "..."} (NOT the structured envelope
    // callSketch already screens). Treat that as "could not probe" -> fall back.
    if (res.contains("ok") && res["ok"].is_boolean() && !res["ok"].get<bool>()) {
        st.addLog(std::string("[sketch] sketch_probe rejected: ") +
                  res.value("error", std::string("?")));
        return false;
    }
    wouldOverDefine = res.value("would_over_define", false);
    conflicting.clear();
    if (res.contains("conflicting") && res["conflicting"].is_array())
        for (const auto& c : res["conflicting"]) if (c.is_string()) conflicting.push_back(c.get<std::string>());
    redundant.clear();
    if (res.contains("redundant") && res["redundant"].is_array())
        for (const auto& c : res["redundant"]) if (c.is_string()) redundant.push_back(c.get<std::string>());
    return true;
}

void SketchMode::applyEnterResult(const nlohmann::json& res) {
    dof_ = res.value("dof", 0);
    status_ = res.value("status", std::string("under"));
    conflicting_.clear();
    if (res.contains("conflicting") && res["conflicting"].is_array())
        for (const auto& c : res["conflicting"]) if (c.is_string()) conflicting_.push_back(c.get<std::string>());
    redundant_.clear();
    if (res.contains("redundant") && res["redundant"].is_array())
        for (const auto& c : res["redundant"]) if (c.is_string()) redundant_.push_back(c.get<std::string>());
    // C50f: latch the conflict-freeze flag. solve_rejected:true means the system is contradictory
    // and `entities` carry last-valid (frozen) coords; cleared the moment a solve succeeds again.
    // Drag responses always report solve_rejected:false, so they release a prior freeze too.
    solveRejected_ = res.value("solve_rejected", false);
    if (res.contains("plane_frame") && res["plane_frame"].is_array() &&
        res["plane_frame"].size() == 16) {
        for (int i = 0; i < 16; ++i) frame_[i] = res["plane_frame"][i].get<float>();
    }
    parseEntities(res, ents_);
    applyConstraints(res);   // C37-D: store the constraints array for dim annotations
    // C41 (T2) / C50: drop selected handles whose OWNING entity no longer exists (deleted/undone).
    selIds_.erase(std::remove_if(selIds_.begin(), selIds_.end(),
                                 [&](const std::string& id) {
                                     return entById(id.substr(0, id.find('.'))) == nullptr;
                                 }),
                  selIds_.end());
    // C51: drop selected relations whose constraint vanished (deleted/undone/re-solved away).
    selRelIds_.erase(std::remove_if(selRelIds_.begin(), selRelIds_.end(),
                                    [&](const std::string& cid) {
                                        for (const auto& c : cons_) if (c.id == cid) return false;
                                        return true;
                                    }),
                     selRelIds_.end());
    // C38 (S1) / C50: ents_ was just refilled — prune Smart Dimension picks SURGICALLY instead of
    // clearing the buffer. A projection of external geometry (addProjectedDimRef) applies ops and
    // resolves MID-PICK, and the wholesale clear was throwing away the user's first pick. Picks
    // whose entity vanished are dropped; point picks get their (u,v) refreshed from the solved
    // entity so the preview math doesn't use stale coordinates. The Modify box (if open) already
    // captured its placement + refs, so it survives this refresh untouched.
    if (!dimPopupOpen_) {
        auto baseOf = [](const std::string& h) { return h.substr(0, h.find('.')); };
        dimPicks_.erase(std::remove_if(dimPicks_.begin(), dimPicks_.end(),
            [&](DimPick& p) {
                if (p.isPoint) {
                    std::string base = baseOf(p.handle);
                    if (base == "origin") { p.u = 0; p.v = 0; return false; }
                    const SketchEntity* e = entById(base);
                    if (!e) return true;
                    if (p.handle.find(".p1") != std::string::npos)      { p.u = e->p1[0]; p.v = e->p1[1]; }
                    else if (p.handle.find(".p2") != std::string::npos) { p.u = e->p2[0]; p.v = e->p2[1]; }
                    else if (p.handle.find(".center") != std::string::npos) { p.u = e->center[0]; p.v = e->center[1]; }
                    else if (p.handle.find(".p") != std::string::npos)  { p.u = e->p1[0]; p.v = e->p1[1]; }
                    return false;
                }
                p.entIdx = entIndexById(p.entId);
                return p.entIdx < 0;
            }), dimPicks_.end());
        dimHasPending_ = false;     // rebuilt next frame from the surviving picks
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
        if (c.contains("cond") && c["cond"].is_array())
            for (const auto& q : c["cond"]) sc.cond.push_back(q.is_string() ? q.get<std::string>() : "center");
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

// ---- Wave 4C/4D: sketch entity copy / paste -------------------------------------------------

bool SketchMode::sketchHasSelection() const {
    return active_ && !selIds_.empty();
}

// Copy the selected entities (+ their internal constraints) into st.clipboard as an entities bundle.
// Entities are pulled RAW from the doc tree so all type-specific fields survive the round-trip; the
// constraint set keeps only relations whose every handle references a SELECTED entity (external /
// origin relations are dropped — the engine drops them at paste anyway). copyOrigin = the min-corner
// of the selected geometry in sketch (u,v), so paste can offset relative to a stable anchor.
void SketchMode::resolve(SketchHost& st) {
    nlohmann::json res;
    if (callSketch(st, "sketch_solve", nlohmann::json::object(), res)) applyEnterResult(res);
}

void SketchMode::commitDrag(SketchHost& st) {
    nlohmann::json res;
    if (!callSketch(st, "sketch_drag_commit", nlohmann::json::object(), res)) return;
    if (res.contains("tess") && res["tess"].is_object()) {
        std::string aicm = res["tess"].value("aicm_path", std::string());
        if (!aicm.empty()) { std::string le; st.loadDocMesh(aicm, le); }
    }
    resyncMirror(st);
    resolve(st);   // refresh statuses against the committed doc
}

void SketchMode::addEntity(SketchHost& st, const nlohmann::json& entity,
                           const nlohmann::json& constraints) {
    // sketch_add_entity goes through the normal ops path (revision bump + undo) via DocMirror.
    nlohmann::json op = {{"op", "sketch_add_entity"}, {"sketch", fid_}, {"entity", entity}};
    if (!constraints.is_null() && constraints.is_array() && !constraints.empty())
        op["constraints"] = constraints;
    nlohmann::json ops = nlohmann::json::array({op});
    auto r = st.docMirror().applyOps(st.worker(), ops);
    if (!r.ok) { st.addLog("[sketch] add_entity failed: " + r.error_message); return; }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);
}

// Shared snap->constraint translation (see header). The Line tool keeps its own inline variant
// because it also threads tangent emission through a per-segment flag; every other tool routes its
// center/endpoint snaps through here so a snapped placement leaves a real relation behind.
void SketchMode::pushSnapConstraint(const std::string& snap, const std::string& pt,
                                    nlohmann::json& cs) const {
    if (snap.empty()) return;
    if (snapHasPrefix(snap, "align:")) {
        // "align:<uHandle>|<vHandle>" — vertical (same u/x) to uHandle, horizontal (same v/y) to
        // vHandle; either side may be empty. Two-point form: refs = {source, this point}.
        std::string body = snap.substr(6);
        auto bar = body.find('|');
        std::string uH = bar == std::string::npos ? body : body.substr(0, bar);
        std::string vH = bar == std::string::npos ? std::string() : body.substr(bar + 1);
        if (!uH.empty()) cs.push_back({{"type", "vertical"},   {"refs", {uH, pt}}});
        if (!vH.empty()) cs.push_back({{"type", "horizontal"}, {"refs", {vH, pt}}});
        return;
    }
    if (snapHasPrefix(snap, "mid:")) {
        cs.push_back({{"type", "midpoint"}, {"refs", {pt, snap.substr(4)}}});
    } else if (snapHasPrefix(snap, "rim:") || snapHasPrefix(snap, "tan:")) {
        // The new entity's point `pt` lands on the snapped circle/arc rim.
        cs.push_back({{"type", "point_on"}, {"refs", {pt, snap.substr(4)}}});
    } else {
        // Plain endpoint/center/origin handle -> coincident.
        cs.push_back({{"type", "coincident"}, {"refs", {snap, pt}}});
    }
}

std::string SketchMode::emitConstructionEntity(SketchHost& st, const nlohmann::json& entity,
                                               const nlohmann::json& constraints) {
    nlohmann::json op = {{"op", "sketch_add_entity"}, {"sketch", fid_}, {"entity", entity}};
    if (constraints.is_array() && !constraints.empty()) op["constraints"] = constraints;
    auto r = st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}), /*coalesce=*/true);
    if (!r.ok) { st.addLog("[sketch] construction entity failed: " + r.error_message); return {}; }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    return (r.id_map.is_object() && r.id_map.contains("$0"))
         ? r.id_map["$0"].get<std::string>() : std::string();
}

void SketchMode::emitConstructionConstraint(SketchHost& st, const nlohmann::json& constraint) {
    nlohmann::json op = {{"op", "sketch_add_constraint"}, {"sketch", fid_}, {"constraint", constraint}};
    auto r = st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}), /*coalesce=*/true);
    if (!r.ok) { st.addLog("[sketch] construction constraint failed: " + r.error_message); return; }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
}

void SketchMode::applyRelationOps(SketchHost& st, const nlohmann::json& ops,
                                  const std::string& label) {
    auto r = st.docMirror().applyOps(st.worker(), ops);
    if (!r.ok) {
        st.toast(ui::sketch::ToastLevel::Warn, label + ": " + r.error_message);
        return;
    }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);
}

void SketchMode::addRelationOps(SketchHost& st, const nlohmann::json& ops,
                                const std::string& label) {
    if (!ops.is_array() || ops.empty()) return;

    for (const auto& op : ops) {
        if (!op.is_object() || op.value("op", std::string()) != "sketch_add_constraint")
            continue;
        if (!op.contains("constraint") || !op["constraint"].is_object())
            continue;
        bool wouldOver = false;
        std::vector<std::string> pc, pr;
        if (sketchProbe(st, nlohmann::json{{"add_constraint", op["constraint"]}},
                        wouldOver, pc, pr) && wouldOver) {
            relOverDefOps_ = ops;
            relOverDefLabel_ = label;
            relOverDefPos_ = relPopupPos_;
            relOverDefOpen_ = true;
            relPopupOpen_ = false;
            relIds_.clear();
            return;
        }
    }

    applyRelationOps(st, ops, label);
    relPopupOpen_ = false;
    relIds_.clear();
}

bool SketchMode::smartDimPicking() const {
    return active_ && tool_ == SketchTool::SmartDim && !dimPopupOpen_ && dimPicks_.size() < 2;
}

bool SketchMode::cameraLock(float& yaw, float& pitch) const {
    if (!active_) return false;
    yaw = camYaw_; pitch = camPitch_;
    return true;
}

bool SketchMode::enterForFeature(SketchHost& st, const std::string& sketchFid) {
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
    // Recentre on the sketch: zero the pan so the orbit-centre override (the sketch geometry centre,
    // set each frame by the viewport) sits at screen centre — the sketch, not the origin, is framed.
    // prevPan_ is restored on exit, so the 3D view returns to where it was.
    st.pan[0] = st.pan[1] = st.pan[2] = 0.0f;
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
    if (st.docMirror().hasDoc() && st.workerStarted()) {
        nlohmann::json cres, cp = {{"doc_id", st.docMirror().docId()}};
        std::string cerr;
        if (st.worker().call("doc_checkpoint", cp, cres, cerr) && cres.contains("checkpoint"))
            cancelCheckpoint_ = cres.value("checkpoint", std::string());
    }
    st.addLog("[sketch] entered " + fid_ + "  (dof=" + std::to_string(dof_) + ")");
    return true;
}

// C37-D: create a sketch on an explicit plane ref and enter it. Returns the new feature id.
std::string SketchMode::createSketchOnPlane(SketchHost& st, const nlohmann::json& planeRef) {
    if (!st.docMirror().hasDoc()) {
        std::string err;
        if (!st.docMirror().newPart(st.worker(), err)) { st.addLog("[sketch] newPart failed: " + err); return {}; }
        st.clearViewportForNewDoc();
    }
    nlohmann::json ops = nlohmann::json::array({
        {{"op", "add_feature"}, {"feature", {
            {"type", "sketch"}, {"name", "Sketch"},
            {"refs", {{"plane", planeRef}}},
            {"params", {{"entities", nlohmann::json::array()},
                        {"constraints", nlohmann::json::array()}}}}}}});
    auto r = st.docMirror().applyOps(st.worker(), ops);
    if (!r.ok) { st.addLog("[sketch] create sketch failed: " + r.error_message); return {}; }
    std::string newFid = (r.id_map.is_object() && r.id_map.contains("$0"))
                       ? r.id_map["$0"].get<std::string>() : std::string();
    if (newFid.empty()) { st.addLog("[sketch] create sketch: no id"); return {}; }
    if (!enterForFeature(st, newFid)) return {};
    isNewEmpty_ = true;
    return newFid;
}

void SketchMode::beginPlaneEdit(SketchHost& st, const std::string& sketchFid) {
    if (sketchFid.empty()) return;
    // The plane-pick overlay can't share the screen with an open sketcher — leave any active sketch
    // first (keep its entities), then arm the reparent pick.
    if (active_) exitSketch(st, /*keep=*/true);
    planeEditFid_ = sketchFid;
    planePicking_ = true;
    planeHover_ = -1;
    st.sketchPlanePickActive = true;
    st.requestCursor(ui::sketch::CursorKind::Crosshair);
    st.toast(ui::sketch::ToastLevel::Info, "Pick the new sketch plane (or a planar face)");
}

std::string SketchMode::consumePlanePick(SketchHost& st, const nlohmann::json& planeRef) {
    // New-sketch path: defer to createSketchOnPlane and let the CALLER own the pick-flag lifecycle
    // (so a non-planar face leaves the user still in pick mode, as before).
    if (planeEditFid_.empty()) return createSketchOnPlane(st, planeRef);

    // Re-parent path: there's no sketch to enter afterwards, so clear the pick-arm flags here or the
    // overlay would stay up. Then patch refs.plane on the existing sketch and rebuild the document.
    // edit_feature merges refs, so the sketch (and every feature consuming it) recompiles on the new
    // plane.
    planePicking_ = false;
    st.sketchPlanePickActive = false;
    std::string fid = planeEditFid_;
    planeEditFid_.clear();
    nlohmann::json ops = nlohmann::json::array({
        {{"op", "edit_feature"}, {"id", fid}, {"refs", {{"plane", planeRef}}}}});
    auto r = st.docMirror().applyOps(st.worker(), ops);
    if (!r.ok) {
        st.addLog("[sketch] reparent plane failed: " + r.error_message);
        st.toast(ui::sketch::ToastLevel::Warn, "Couldn't move the sketch to that plane");
        return {};
    }
    if (!r.aicm_path.empty()) {
        std::string le;
        st.loadDocMesh(r.aicm_path, le);
        if (!le.empty()) st.addLog("[err] loadDocMesh (reparent): " + le);
    }
    resyncMirror(st);
    st.selectedFeatureId = fid;
    st.toast(ui::sketch::ToastLevel::Info, "Sketch plane updated");
    return fid;
}

bool SketchMode::enterNewOnSelection(SketchHost& st) {
    // C38 (S2): a double-fire of the ribbon "New Sketch" (sketch_new) while a sketch is already
    // entered would start a plane-pick over the live sketch and leave a zombie state. Refuse to
    // re-enter when already active — the user must exit the current sketch first.
    if (active_) return false;
    // C37-D: if the selection already names a usable plane/planar face, enter directly. Otherwise
    // start the SolidWorks plane-pick step (drawOverlay handles the ray-pick while the flag is set).
    nlohmann::json planeRef;
    bool haveRef = false;
    for (const auto& it : st.selectionItems()) {
        if (!it.selector.empty()) {
            try {
                nlohmann::json sel = nlohmann::json::parse(it.selector);
                std::string k = sel.value("kind", std::string());
                if (k == "face" || k == "datum") { planeRef = sel; haveRef = true; break; }
            } catch (...) {}
        }
    }
    // Pre-selected plane (SolidWorks "select a plane, then Sketch"): a plane held in
    // st.selectedFeatureId — either a default origin plane ("plane.front/top/right") or a created
    // datum-plane feature — starts the sketch directly on it. Routed as a BARE string so the engine
    // resolves it as a plane name / feature id (same form createSketchOnPlane gets from a tree click).
    if (!haveRef && !st.selectedFeatureId.empty()) {
        const std::string& sfid = st.selectedFeatureId;
        bool isPlane = (sfid == "plane.front" || sfid == "plane.top" || sfid == "plane.right");
        if (!isPlane) {
            const nlohmann::json& tree = st.docMirror().tree();
            if (tree.is_object() && tree.contains("features") && tree["features"].is_array())
                for (const auto& tf : tree["features"])
                    if (tf.is_object() && tf.value("id", std::string()) == sfid &&
                        tf.value("type", std::string()) == "plane") { isPlane = true; break; }
        }
        if (isPlane) { planeRef = sfid; haveRef = true; }
    }
    if (haveRef) return !createSketchOnPlane(st, planeRef).empty();
    // No usable selection: enter the interactive plane-pick (front/top/right or a planar face).
    planePicking_ = true;
    planeHover_ = -1;
    st.sketchPlanePickActive = true;
    st.requestCursor(ui::sketch::CursorKind::Crosshair);
    return false;   // not yet in a sketch — the pick click finishes the job
}

void SketchMode::exitSketch(SketchHost& st, bool keep) {
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
        st.docMirror().applyOps(st.worker(), ops);
    }
    st.addLog("[sketch] exited " + fid_);
    active_ = false;
    isNewEmpty_ = false;
    fid_.clear();
    ents_.clear();
    cons_.clear();              // C37-D
    autoPlace_.clear(); projRefs_.clear();         // C37-D
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
void SketchMode::cancelSketch(SketchHost& st) {
    if (!active_) return;
    cancelTool();
    if (cancelCheckpoint_.empty() || !st.docMirror().hasDoc() || !st.workerStarted()) {
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
    nlohmann::json res, params = {{"doc_id", st.docMirror().docId()}, {"checkpoint", cancelCheckpoint_}};
    std::string err;
    bool ok = st.worker().call("doc_revert", params, res, err) && !res.contains("code");
    if (!ok) {
        st.toast(ui::sketch::ToastLevel::Warn, "Sketch cancel: revert failed — exited without revert");
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
    st.toast(ui::sketch::ToastLevel::Info, "Sketch cancelled");
    // Teardown (mirror of exitSketch, minus the sketch_exit call — revert already rebuilt).
    active_ = false;
    isNewEmpty_ = false;
    fid_.clear();
    ents_.clear();
    cons_.clear();
    autoPlace_.clear(); projRefs_.clear();
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

bool SketchMode::onRibbonCommand(SketchHost& st, const std::string& cmdId) {
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
    // C52: line-line corner fillet / chamfer (radius/distance persist across invocations).
    if (cmdId == "sk_fillet")          { resetTool(); tool_ = SketchTool::SkFillet;  corner1Id_.clear(); return true; }
    if (cmdId == "sk_chamfer")         { resetTool(); tool_ = SketchTool::SkChamfer; corner1Id_.clear(); return true; }
    // C53: mirror / linear+circular pattern / convert (numeric dims persist across invocations).
    if (cmdId == "sk_mirror")          { resetTool(); tool_ = SketchTool::Mirror;
                                         mpStage_ = 0; mpSel_.clear();                     return true; }
    if (cmdId == "sk_lin_pattern")     { resetTool(); tool_ = SketchTool::LinPattern;
                                         mpStage_ = 0; mpSel_.clear();                     return true; }
    if (cmdId == "sk_circ_pattern")    { resetTool(); tool_ = SketchTool::CircPattern;
                                         mpStage_ = 0; mpSel_.clear();                     return true; }
    if (cmdId == "sk_convert_ent")     { resetTool(); tool_ = SketchTool::Convert;         return true; }
    // C54: Move/Copy entities (engine op sketch_move) — reuses the mirror/pattern selection stage.
    if (cmdId == "sk_move")            { resetTool(); tool_ = SketchTool::Move;
                                         mpStage_ = 0; mpSel_.clear(); moveCopy_ = false;   return true; }
    // C54: Tangent Arc — draws an arc starting tangent to a prior/selected entity's free endpoint.
    if (cmdId == "sk_arc_tangent")     { resetTool(); tool_ = SketchTool::ArcTangent;      return true; }
    // C42: remaining draw tools.
    if (cmdId == "sk_arc_3pt")         { resetTool(); tool_ = SketchTool::Arc3pt;         return true; }
    if (cmdId == "sk_polygon")         { resetTool(); tool_ = SketchTool::Polygon;        return true; }
    // Polygon flyout variants — set polySides_ then arm the Polygon tool.
    if (cmdId == "sk_poly_tri")  { resetTool(); tool_ = SketchTool::Polygon; polySides_ = 3; return true; }
    if (cmdId == "sk_poly_quad") { resetTool(); tool_ = SketchTool::Polygon; polySides_ = 4; return true; }
    if (cmdId == "sk_poly_pent") { resetTool(); tool_ = SketchTool::Polygon; polySides_ = 5; return true; }
    if (cmdId == "sk_poly_hex")  { resetTool(); tool_ = SketchTool::Polygon; polySides_ = 6; return true; }
    if (cmdId == "sk_poly_oct")  { resetTool(); tool_ = SketchTool::Polygon; polySides_ = 8; return true; }
    if (cmdId == "sk_poly_star") { resetTool(); tool_ = SketchTool::Star;                  return true; }
    if (cmdId == "sk_rect_center")     { resetTool(); tool_ = SketchTool::RectCenter;     return true; }
    if (cmdId == "sk_point")           { resetTool(); tool_ = SketchTool::Point;          return true; }
    if (cmdId == "sk_ellipse" || cmdId == "sk_ellipse_big") {
        resetTool(); tool_ = SketchTool::Ellipse; return true;
    }
    if (cmdId == "sk_spline")          { resetTool(); tool_ = SketchTool::Spline;         return true; }
    // Style Spline — click-collect control poles (same UX as Spline); emits "style_spline".
    if (cmdId == "sk_spline_style")    { resetTool(); tool_ = SketchTool::StyleSpline;    return true; }
    // Sketch Text — arm a one-click placement; the click opens the text popup (string + height).
    if (cmdId == "sk_text")            { resetTool(); tool_ = SketchTool::Text;
                                         textPopupOpen_ = false;                           return true; }
    // Equation-Driven Curve — no viewport pick; open the x(t)/y(t)/range popup right away.
    if (cmdId == "sk_spline_equation") { resetTool(); tool_ = SketchTool::EqCurve;
                                         eqPopupOpen_ = true; eqPopupPos_ = ImGui::GetMousePos();
                                         eqFocus_ = true;                                  return true; }
    // Airfoil calculator — no viewport pick; open the NACA/.dat parameter popup right away.
    if (cmdId == "sk_airfoil")         { resetTool(); tool_ = SketchTool::Airfoil;
                                         afPopupOpen_ = true; afPopupPos_ = ImGui::GetMousePos();
                                         afFocus_ = true;                                  return true; }
    // C42: Construction — immediate toggle on the current selection (engine op
    // sketch_toggle_construction). Not a tool: the selection survives (entity ids persist).
    if (cmdId == "sk_construction") {
        if (selIds_.empty()) {
            st.toast(ui::sketch::ToastLevel::Info,
                     "Construction: select sketch entities first (click them with no tool armed)");
            return true;
        }
        nlohmann::json ops = nlohmann::json::array();
        for (const auto& id : selIds_) {
            if (id == "origin") continue;   // the virtual origin has no construction state
            ops.push_back({{"op", "sketch_toggle_construction"}, {"sketch", fid_}, {"id", id}});
        }
        auto r = st.docMirror().applyOps(st.worker(), ops);
        if (!r.ok) st.toast(ui::sketch::ToastLevel::Error, "Construction: " + r.error_message);
        else {
            if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
            resolve(st);
        }
        return true;
    }
    // C42: minimal Add Relation popup over the current 1-2 entity selection (drawOverlay renders
    // it; the offered relations depend on the selected entity types).
    if (cmdId == "sk_relations") {
        if (selIds_.empty()) {
            st.toast(ui::sketch::ToastLevel::Info, "Add Relation: select sketch entities or points first");
            return true;
        }
        relIds_ = selIds_;
        relPopupOpen_ = true;
        relPopupPos_ = ImGui::GetMousePos();
        return true;
    }
    if (cmdId == "sk_fix") {
        // SolidWorks "Fix" relation as a one-click ribbon button: pin every selected entity/point
        // (the engine `fix` constraint zeroes that entity's DOF — sketch_solver.py). This is the
        // direct shortcut for the Fix that already lives in the Add-Relation popup; it routes
        // through addRelationOps so an over-defining Fix surfaces the same "Over Defined" prompt.
        if (selIds_.empty()) {
            st.toast(ui::sketch::ToastLevel::Info, "Fix: select sketch entities or points first");
            return true;
        }
        relPopupPos_ = ImGui::GetMousePos();   // anchor the over-def prompt at the cursor
        nlohmann::json ops = nlohmann::json::array();
        for (const auto& h : selIds_)
            ops.push_back({{"op", "sketch_add_constraint"}, {"sketch", fid_},
                {"constraint", {{"type", "fix"}, {"refs", nlohmann::json::array({h})}}}});
        addRelationOps(st, ops, "Fix");
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
        case SketchTool::SkFillet:        return "sk_fillet";
        case SketchTool::SkChamfer:       return "sk_chamfer";
        case SketchTool::Mirror:          return "sk_mirror";
        case SketchTool::LinPattern:      return "sk_lin_pattern";
        case SketchTool::CircPattern:     return "sk_circ_pattern";
        case SketchTool::Convert:         return "sk_convert_ent";
        case SketchTool::Move:            return "sk_move";
        case SketchTool::ArcTangent:      return "sk_arc_tangent";
        case SketchTool::Arc3pt:          return "sk_arc_3pt";
        case SketchTool::Polygon:         return "sk_polygon";
        case SketchTool::RectCenter:      return "sk_rect_center";
        case SketchTool::Point:           return "sk_point";
        case SketchTool::Ellipse:         return "sk_ellipse_big";
        case SketchTool::Spline:          return "sk_spline";
        case SketchTool::StyleSpline:     return "sk_spline_style";
        case SketchTool::Text:            return "sk_text";
        case SketchTool::EqCurve:         return "sk_spline_equation";
        case SketchTool::Airfoil:         return "sk_airfoil";
        case SketchTool::Star:            return "sk_polygon";  // keeps parent button highlighted
        default:                          return "";
    }
}

// ---------------------------------------------------------------------------
// tool helpers
// ---------------------------------------------------------------------------

void SketchMode::clearEntitySelection() {
    selIds_.clear();
    selRelIds_.clear();
    hoverRelCid_.clear();
}

void SketchMode::resetTool() { tool_ = SketchTool::None; toolUV_.clear(); toolSnaps_.clear();
                               conicRho_ = 0.5f; chainPrevId_.clear();
                               selIds_.clear();   // C41 (T2): selection dies with any tool change/exit
                               boxArmed_ = boxSelecting_ = false;  // C51
                               groupDragArmed_ = groupDragging_ = false; groupDragTargets_.clear();
                               groupClickKind_ = 0; groupClickEnt_.clear(); groupClickRegion_ = -1;
                               hotRegion_ = entityLoopRegion_ = -1; dwellAccum_ = 0.0f; dwellEnt_.clear();
                               selRelIds_.clear(); hoverRelCid_.clear();
                               relPopupOpen_ = false; relIds_.clear();   // C42: relation popup too
                               offsetStage_ = 0; offsetChain_.clear(); offsetFocusInput_ = false;
                               corner1Id_.clear();   // C52: drop a held first corner pick (keep radius/distance)
                               mpStage_ = 0; mpSel_.clear();   // C53: drop mirror/pattern selection (keep dims)
                               // C37-D: clear the Smart Dimension state machine.
                               dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr;
                               dimType_.clear(); dimAxis_.clear(); dimEditCid_.clear();
                               dimPopupOpen_ = false; dimError_.clear(); dimPopupDriven_ = false;
                               overDefOpen_ = false; overDefPendingCon_ = nullptr;   // C50f: add-path over-define dialog
                               editOverDefOpen_ = false; editOverDefOp_ = nullptr;   // C50f: edit-path over-define dialog
                               relOverDefOpen_ = false; relOverDefOps_ = nullptr; relOverDefLabel_.clear();   // C50g
                               textPopupOpen_ = false; textFocus_ = false;          // Sketch Text popup
                               eqPopupOpen_ = false; eqFocus_ = false;              // Eq-Driven Curve popup
                               afPopupOpen_ = false; afFocus_ = false;              // Airfoil calculator popup
                               pastePlacing_ = false; pasteBundle_ = nlohmann::json(); }  // Wave 4C paste-placement
void SketchMode::cancelTool() { resetTool(); }

int SketchMode::entIndexById(const std::string& id) const {
    for (size_t i = 0; i < ents_.size(); ++i) if (ents_[i].id == id) return (int)i;
    return -1;
}

std::string SketchMode::addProjectedDimRef(SketchHost& st, const nlohmann::json& ref,
                                           const std::string& srcKey) {
    std::string eid;
    auto it = projRefs_.find(srcKey);
    if (it != projRefs_.end() && entById(it->second)) {
        eid = it->second;
    } else {
        nlohmann::json res;
        if (!callSketch(st, "sketch_project_ref", {{"ref", ref}}, res)) {
            st.toast(ui::sketch::ToastLevel::Warn,
                     "Can't dimension to that reference (projection not supported)");
            return std::string();
        }
        if (!res.contains("entity") || !res["entity"].is_object()) return std::string();
        nlohmann::json ent = res["entity"];
        ent["construction"] = true;
        nlohmann::json op = {{"op", "sketch_add_entity"}, {"sketch", fid_}, {"entity", ent},
            {"constraints", nlohmann::json::array(
                {{{"type", "fix"}, {"refs", nlohmann::json::array({"$self"})}}})}};
        auto r = st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}));
        if (!r.ok) { st.addLog("[sketch] project ref failed: " + r.error_message); return std::string(); }
        eid = (r.id_map.is_object() && r.id_map.contains("$0"))
            ? r.id_map["$0"].get<std::string>() : std::string();
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
        resolve(st);
        if (eid.empty()) return eid;
        projRefs_[srcKey] = eid;
    }
    const SketchEntity* e = entById(eid);
    if (!e) return std::string();
    DimPick p;
    if (e->type == "point") { p.isPoint = true; p.handle = eid + ".p"; p.u = e->p1[0]; p.v = e->p1[1]; }
    else { p.entId = eid; p.entIdx = entIndexById(eid); }
    for (const auto& q : dimPicks_)
        if (q.isPoint == p.isPoint && (p.isPoint ? q.handle == p.handle : q.entId == p.entId))
            return eid;
    dimPicks_.push_back(p);
    return eid;
}

int SketchMode::hitTestEntity(const ImVec2& s, float maxPx) const {
    float best = maxPx * maxPx;
    int hit = -1;
    for (size_t i = 0; i < ents_.size(); ++i) {
        const SketchEntity& e = ents_[i];
        float d2 = 1e30f;
        if (e.type == "line") {
            ImVec2 a = uvToScreen(e.p1[0], e.p1[1]), b = uvToScreen(e.p2[0], e.p2[1]);
            float vx = b.x - a.x, vy = b.y - a.y;
            float wx = s.x - a.x, wy = s.y - a.y;
            float t = (vx * wx + vy * wy) / std::fmax(1e-6f, vx * vx + vy * vy);
            t = std::fmax(0.0f, std::fmin(1.0f, t));
            float px = a.x + t * vx, py = a.y + t * vy;
            d2 = (px - s.x) * (px - s.x) + (py - s.y) * (py - s.y);
        } else if (e.type == "slot" || e.type == "arc_slot") {
            auto segD2 = [&](ImVec2 a, ImVec2 b) {
                float vx = b.x - a.x, vy = b.y - a.y;
                float t = std::fmax(0.0f, std::fmin(1.0f,
                    ((s.x-a.x)*vx + (s.y-a.y)*vy) / std::fmax(1e-6f, vx*vx + vy*vy)));
                float px = a.x + t * vx, py = a.y + t * vy;
                return (px - s.x) * (px - s.x) + (py - s.y) * (py - s.y);
            };
            std::vector<ImVec2> uv; bool cl; entityBoundaryUV(e, uv, cl);
            for (size_t k = 1; k < uv.size(); ++k)
                d2 = std::fmin(d2, segD2(uvToScreen(uv[k-1].x, uv[k-1].y), uvToScreen(uv[k].x, uv[k].y)));
            if (e.type == "slot")
                d2 = std::fmin(d2, segD2(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1])));
        } else if (e.type == "circle" || e.type == "arc") {
            ImVec2 cc = uvToScreen(e.center[0], e.center[1]);
            ImVec2 rim = uvToScreen(e.center[0] + e.radius, e.center[1]);
            float rpx = std::hypot(rim.x - cc.x, rim.y - cc.y);
            float dm = std::hypot(s.x - cc.x, s.y - cc.y);
            d2 = (dm - rpx) * (dm - rpx);
        } else if (e.type == "point") {
            ImVec2 p = uvToScreen(e.p1[0], e.p1[1]);
            d2 = (p.x - s.x) * (p.x - s.x) + (p.y - s.y) * (p.y - s.y);
        } else if (e.type == "ellipse" || e.type == "spline" || e.type == "style_spline") {
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

}  // namespace ui
