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

// Draw a dashed line segment a->b onto dl.  dash/gap are in screen pixels; scale them by
// ui::uiScale before passing.  SolidWorks default: dash=6, gap=4 at scale 1.
void drawDashedLine(ImDrawList* dl, ImVec2 a, ImVec2 b, ImU32 col_,
                    float thickness, float dash, float gap) {
    float dx = b.x - a.x, dy = b.y - a.y;
    float L = std::hypot(dx, dy);
    if (L < 1.0f) return;
    float step = dash + gap;
    for (float t = 0.0f; t < L; t += step) {
        float e = std::fmin(t + dash, L);
        dl->AddLine(ImVec2(a.x + dx * (t / L), a.y + dy * (t / L)),
                    ImVec2(a.x + dx * (e / L), a.y + dy * (e / L)), col_, thickness);
    }
}

// Draw a dashed polyline through `pts` (screen-space) with continuous phase across segments.
// Used for construction circles/arcs/ellipses/splines so dashes don't restart at each tesselation
// vertex (which would look like a dotted line).
void drawDashedPolyline(ImDrawList* dl, const std::vector<ImVec2>& pts, ImU32 col_,
                        float thickness, float dash, float gap) {
    if (pts.size() < 2) return;
    float phase = 0.0f;   // distance walked in the current ON or OFF span
    bool on = true;
    for (size_t i = 1; i < pts.size(); ++i) {
        ImVec2 a = pts[i - 1], b = pts[i];
        float dx = b.x - a.x, dy = b.y - a.y;
        float segLen = std::hypot(dx, dy);
        if (segLen < 1e-4f) continue;
        float walked = 0.0f;   // distance walked along THIS segment
        while (walked < segLen) {
            float span = on ? dash : gap;
            float rem = span - phase;          // remaining in this dash/gap slot
            float avail = segLen - walked;
            float step = std::fmin(rem, avail);
            if (on) {
                float f0 = walked / segLen, f1 = (walked + step) / segLen;
                dl->AddLine(ImVec2(a.x + dx * f0, a.y + dy * f0),
                            ImVec2(a.x + dx * f1, a.y + dy * f1), col_, thickness);
            }
            walked += step;
            phase += step;
            if (phase >= span - 1e-4f) { on = !on; phase = 0.0f; }
        }
    }
}

// C51: fill a simple (possibly concave) screen-space polygon via ear clipping. ImGui's
// AddConvexPolyFilled fans from vertex 0 and bleeds across concavities, so closed profiles like
// L-brackets need real triangulation. Robust enough for the well-formed loops region detection
// produces (no self-intersections); falls back to the convex fill for triangles/degenerate input.
void fillPolygon(ImDrawList* dl, const std::vector<ImVec2>& poly, ImU32 c) {
    int n = (int)poly.size();
    if (n < 3) return;
    if (n == 3) { dl->AddTriangleFilled(poly[0], poly[1], poly[2], c); return; }
    auto cross = [](const ImVec2& a, const ImVec2& b, const ImVec2& cc) {
        return (b.x - a.x) * (cc.y - a.y) - (b.y - a.y) * (cc.x - a.x);
    };
    // polygon winding (signed area)
    float area = 0.0f;
    for (int i = 0, j = n - 1; i < n; j = i++) area += poly[j].x * poly[i].y - poly[i].x * poly[j].y;
    bool ccw = area > 0.0f;
    std::vector<int> idx(n);
    for (int i = 0; i < n; ++i) idx[i] = i;
    auto inTri = [&](const ImVec2& a, const ImVec2& b, const ImVec2& cc, const ImVec2& p) {
        float d1 = cross(a, b, p), d2 = cross(b, cc, p), d3 = cross(cc, a, p);
        bool neg = (d1 < 0) || (d2 < 0) || (d3 < 0);
        bool pos = (d1 > 0) || (d2 > 0) || (d3 > 0);
        return !(neg && pos);
    };
    int guard = 0;
    while ((int)idx.size() > 3 && guard++ < 4 * n) {
        bool clipped = false;
        int m = (int)idx.size();
        for (int i = 0; i < m; ++i) {
            const ImVec2& a = poly[idx[(i + m - 1) % m]];
            const ImVec2& b = poly[idx[i]];
            const ImVec2& cc = poly[idx[(i + 1) % m]];
            float conv = cross(a, b, cc);
            bool convex = ccw ? (conv > 0) : (conv < 0);
            if (!convex) continue;
            bool ear = true;
            for (int k = 0; k < m; ++k) {
                int vi = idx[k];
                if (vi == idx[(i + m - 1) % m] || vi == idx[i] || vi == idx[(i + 1) % m]) continue;
                if (inTri(a, b, cc, poly[vi])) { ear = false; break; }
            }
            if (!ear) continue;
            dl->AddTriangleFilled(a, b, cc, c);
            idx.erase(idx.begin() + i);
            clipped = true;
            break;
        }
        if (!clipped) break;   // non-simple polygon — bail rather than spin
    }
    if (idx.size() == 3)
        dl->AddTriangleFilled(poly[idx[0]], poly[idx[1]], poly[idx[2]], c);
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

// C50f: read-only over-define pre-flight. Wraps the sketch_probe RPC. Returns true and fills the
// out-params on success; false on transport / RPC error (callSketch already logged it) so the
// caller can fall back to committing normally — a probe failure must never block the user.
bool SketchMode::sketchProbe(AppState& st, const nlohmann::json& action, bool& wouldOverDefine,
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
// The base entity id behind a selection handle ("e3.p2" -> "e3"; a bare "e3" -> "e3").
static std::string skBaseId(const std::string& handle) {
    return handle.substr(0, handle.find('.'));
}

bool SketchMode::sketchHasSelection() const {
    return active_ && !selIds_.empty();
}

// The active sketch feature's raw params object in the live doc tree (entities + constraints with
// EVERY field intact — richer than the parsed SketchEntity). nullptr if the sketch isn't found.
static const nlohmann::json* sketchParamsInTree(AppState& st, const std::string& fid) {
    const nlohmann::json& tree = st.docMirror.tree();
    if (!tree.is_object()) return nullptr;
    auto fa = tree.find("features");
    if (fa == tree.end() || !fa->is_array()) return nullptr;
    for (const auto& f : *fa) {
        if (!f.is_object()) continue;
        if (f.value("id", std::string()) != fid) continue;
        auto p = f.find("params");
        return (p != f.end() && p->is_object()) ? &(*p) : nullptr;
    }
    return nullptr;
}

// Copy the selected entities (+ their internal constraints) into st.clipboard as an entities bundle.
// Entities are pulled RAW from the doc tree so all type-specific fields survive the round-trip; the
// constraint set keeps only relations whose every handle references a SELECTED entity (external /
// origin relations are dropped — the engine drops them at paste anyway). copyOrigin = the min-corner
// of the selected geometry in sketch (u,v), so paste can offset relative to a stable anchor.
bool SketchMode::sketchCopySelection(AppState& st) {
    if (!active_ || selIds_.empty()) return false;
    const nlohmann::json* params = sketchParamsInTree(st, fid_);
    if (!params) { st.toast(ui::ToastLevel::Warn, "Copy: sketch not found"); return false; }

    // Selected base entity ids (deduped, preserving the raw-entity order below).
    std::unordered_map<std::string, bool> wanted;
    for (const auto& h : selIds_) wanted[skBaseId(h)] = true;

    nlohmann::json entities = nlohmann::json::array();
    auto ents = params->find("entities");
    if (ents != params->end() && ents->is_array()) {
        for (const auto& e : *ents) {
            if (!e.is_object()) continue;
            std::string id = e.value("id", std::string());
            if (!id.empty() && wanted.count(id)) entities.push_back(e);
        }
    }
    if (entities.empty()) { st.toast(ui::ToastLevel::Warn, "Nothing to copy"); return false; }

    // Internal constraints only: every handle base must be a copied entity.
    nlohmann::json constraints = nlohmann::json::array();
    auto cons = params->find("constraints");
    if (cons != params->end() && cons->is_array()) {
        for (const auto& c : *cons) {
            if (!c.is_object()) continue;
            auto refs = c.find("refs");
            if (refs == c.end() || !refs->is_array() || refs->empty()) continue;
            bool allInternal = true;
            for (const auto& r : *refs) {
                if (!r.is_string() || !wanted.count(skBaseId(r.get<std::string>()))) {
                    allInternal = false; break;
                }
            }
            if (allInternal) constraints.push_back(c);
        }
    }

    // Copy-origin = min (u,v) corner of the selected entities (from the solved coords we already
    // hold). Gives a stable, intuitive anchor so the paste preview tracks the cursor naturally.
    bool any = false;
    float mnU = 0, mnV = 0;
    auto acc = [&](float u, float v) {
        if (!any) { mnU = u; mnV = v; any = true; }
        else { if (u < mnU) mnU = u; if (v < mnV) mnV = v; }
    };
    for (const auto& e : ents_) {
        if (!wanted.count(e.id)) continue;
        acc(e.p1[0], e.p1[1]); acc(e.p2[0], e.p2[1]);
        acc(e.center[0], e.center[1]);
        for (const auto& q : e.pts) acc(q.x, q.y);
    }

    st.clipboard.clear();
    st.clipboard.kind = AppState::Clipboard::Kind::SketchEntities;
    st.clipboard.srcDocId = st.docMirror.docId();
    st.clipboard.payload = nlohmann::json{{"entities", entities}, {"constraints", constraints}};
    st.clipboard.copyOriginU = any ? mnU : 0.0f;
    st.clipboard.copyOriginV = any ? mnV : 0.0f;
    st.addLog("[sketch] copied " + std::to_string(entities.size()) + " entit(ies)");
    st.toast(ui::ToastLevel::Info,
             "Copied " + std::to_string(entities.size()) + " sketch entit(ies)");
    return true;
}

// Arm the paste-placement mode from a SketchEntities clipboard. The pasted outlines then follow the
// cursor (drawPastePreview) until the next viewport click commits, or Esc cancels. Snapshots the
// bundle + anchor so a later clipboard change can't disturb the in-progress placement.
bool SketchMode::sketchPasteBegin(AppState& st) {
    if (!active_) return false;
    if (st.clipboard.kind != AppState::Clipboard::Kind::SketchEntities) return false;
    const nlohmann::json& pl = st.clipboard.payload;
    if (!pl.is_object() || !pl.contains("entities") || !pl["entities"].is_array() ||
        pl["entities"].empty())
        return false;
    // Cancel any armed drawing tool / Smart-Dim pick so placement owns the next click cleanly.
    resetTool();
    pasteBundle_ = pl;
    pasteAnchorUV_ = ImVec2(st.clipboard.copyOriginU, st.clipboard.copyOriginV);
    pastePlacing_ = true;
    st.toast(ui::ToastLevel::Info, "Click to place pasted entities (Esc to cancel)");
    return true;
}

// Commit the pasted bundle shifted by (offsetX, offsetY) via the sketch_paste_entities op. Routed
// through the normal ops path (revision bump + undo) like sketch_add_entity, then re-solve so the
// new geometry shows. Op errors are toasted and leave the sketch untouched.
void SketchMode::commitPasteEntities(AppState& st, float offsetX, float offsetY) {
    if (!pasteBundle_.is_object()) { pastePlacing_ = false; return; }
    nlohmann::json op = {
        {"op", "sketch_paste_entities"},
        {"sketch", fid_},
        {"entities_bundle", pasteBundle_},
        {"offset_x", offsetX},
        {"offset_y", offsetY},
    };
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    pastePlacing_ = false;
    pasteBundle_ = nlohmann::json();
    if (!r.ok) { st.toast(ui::ToastLevel::Error, "Paste: " + r.error_message); return; }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);
    st.addLog("[sketch] pasted entities (dx=" + std::to_string(offsetX) +
              ", dy=" + std::to_string(offsetY) + ")");
}

// Follow-cursor preview of the paste bundle: parse its raw entities, translate them by
// (cursorUV - anchor), and stroke each outline in the accent color. The cursor delta is computed in
// sketch (u,v) so the preview lands exactly where the commit op will place the geometry.
void SketchMode::drawPastePreview(AppState& st, ImDrawList* dl, const ImVec2& cursor) {
    if (!pasteBundle_.is_object() || !basisOk_) return;
    (void)st;
    float cu = 0, cv = 0; screenToUV(cursor, cu, cv);
    float du = cu - pasteAnchorUV_.x, dv = cv - pasteAnchorUV_.y;
    // Parse the bundle entities into temporary SketchEntity records, then shift each in (u,v).
    std::vector<SketchEntity> preview;
    nlohmann::json wrap = nlohmann::json{{"entities", pasteBundle_["entities"]}};
    parseEntities(wrap, preview);
    const ImU32 c = col(ui::palette.accent, 0.9f);
    for (auto& e : preview) {
        e.p1[0] += du; e.p1[1] += dv;
        e.p2[0] += du; e.p2[1] += dv;
        e.center[0] += du; e.center[1] += dv;
        for (auto& q : e.pts) { q.x += du; q.y += dv; }
        strokeEntityOutline(dl, e, c, std::max(1.5f, 2.0f * ui::uiScale));
    }
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

void SketchMode::applyRelationOps(AppState& st, const nlohmann::json& ops,
                                  const std::string& label) {
    auto r = st.docMirror.applyOps(st.worker, ops);
    if (!r.ok) {
        st.toast(ui::ToastLevel::Warn, label + ": " + r.error_message);
        return;
    }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);
}

void SketchMode::addRelationOps(AppState& st, const nlohmann::json& ops,
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

// Project external geometry (model edge / planar face / datum plane) into the sketch as a FIXED
// construction entity and chain it into the Smart Dimension pick buffer. The worker does the 3D
// math (sketch_project_ref); the entity goes through the normal ops path so it undoes cleanly.
std::string SketchMode::addProjectedDimRef(AppState& st, const nlohmann::json& ref,
                                           const std::string& srcKey) {
    std::string eid;
    auto it = projRefs_.find(srcKey);
    if (it != projRefs_.end() && entById(it->second)) {
        eid = it->second;   // same source picked before — reuse its reference entity
    } else {
        nlohmann::json res;
        if (!callSketch(st, "sketch_project_ref", {{"ref", ref}}, res)) {
            st.toast(ui::ToastLevel::Warn,
                     "Can't dimension to that reference (projection not supported)");
            return std::string();
        }
        if (!res.contains("entity") || !res["entity"].is_object()) return std::string();
        nlohmann::json ent = res["entity"];
        ent["construction"] = true;
        nlohmann::json op = {{"op", "sketch_add_entity"}, {"sketch", fid_}, {"entity", ent},
            {"constraints", nlohmann::json::array(
                {{{"type", "fix"}, {"refs", nlohmann::json::array({"$self"})}}})}};
        auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
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
            return eid;   // already in the buffer
    dimPicks_.push_back(p);
    return eid;
}

void SketchMode::addPlaneDimRef(AppState& st, int planeIdx) {
    static const char* kDatum[3] = {"front", "top", "right"};
    if (planeIdx < 0 || planeIdx > 2 || !smartDimPicking()) return;
    addProjectedDimRef(st, nlohmann::json{{"datum", kDatum[planeIdx]}},
                       std::string("datum:") + kDatum[planeIdx]);
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
        // C50: one undo step for the whole rectangle — only the first batch pushes an undo snapshot.
        auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}), /*coalesce=*/i != 0);
        if (!r.ok) { st.addLog("[sketch] quad line failed: " + r.error_message); break; }
        std::string nid = (r.id_map.is_object() && r.id_map.contains("$0"))
                        ? r.id_map["$0"].get<std::string>() : std::string();
        ids[i] = nid;
        if (i == 0) firstId = nid;
        prevId = nid;
        if (i == 3 && !firstId.empty() && !nid.empty()) {
            nlohmann::json clop = {{"op", "sketch_add_constraint"}, {"sketch", fid_},
                {"constraint", {{"type", "coincident"}, {"refs", {nid + ".p2", firstId + ".p1"}}}}};
            st.docMirror.applyOps(st.worker, nlohmann::json::array({clop}), /*coalesce=*/true);
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
        st.docMirror.applyOps(st.worker, nlohmann::json::array({op}), /*coalesce=*/true);
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
        // C50: one undo step for the whole polygon — only the first batch pushes an undo snapshot.
        auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}), /*coalesce=*/i != 0);
        if (!r.ok) { st.addLog("[sketch] poly line failed: " + r.error_message); break; }
        std::string nid = (r.id_map.is_object() && r.id_map.contains("$0"))
                        ? r.id_map["$0"].get<std::string>() : std::string();
        ids[i] = nid;
        if (i == 0) firstId = nid;
        prevId = nid;
        if (i == n - 1 && !firstId.empty() && !nid.empty()) {
            nlohmann::json clop = {{"op", "sketch_add_constraint"}, {"sketch", fid_},
                {"constraint", {{"type", "coincident"}, {"refs", {nid + ".p2", firstId + ".p1"}}}}};
            st.docMirror.applyOps(st.worker, nlohmann::json::array({clop}), /*coalesce=*/true);
        }
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    }
    if (equalEdges) {
        for (size_t i = 0; i + 1 < n; ++i) {
            if (ids[i].empty() || ids[i + 1].empty()) continue;
            nlohmann::json op = {{"op", "sketch_add_constraint"}, {"sketch", fid_},
                {"constraint", {{"type", "equal"}, {"refs", {ids[i], ids[i + 1]}}}}};
            st.docMirror.applyOps(st.worker, nlohmann::json::array({op}), /*coalesce=*/true);
        }
    }
    resolve(st);
}

// C51: straight slot as real geometry (see header). Builds the capsule from the spine endpoints
// p1/p2 (the arc centers) and the full width: 2 side lines tangent-offset by half-width, 2 cap
// arcs (the outer semicircles), and a construction centerline. Endpoints are chained by coincident
// closure; the centerline ends pin to the arc centers; the cap radii are equal. All five batches
// coalesce into ONE undo step.
void SketchMode::addDecomposedSlot(AppState& st, const ImVec2& p1, const ImVec2& p2, float width) {
    const float R2D = 57.29577951f, D2R = 1.0f / 57.29577951f;
    float hw = width * 0.5f;
    float axisDeg = std::atan2(p2.y - p1.y, p2.x - p1.x) * R2D;
    float nx = -std::sin(axisDeg * D2R), ny = std::cos(axisDeg * D2R);   // unit +normal
    ImVec2 A1(p1.x + nx * hw, p1.y + ny * hw), B1(p2.x + nx * hw, p2.y + ny * hw);   // l1: A1->B1
    ImVec2 A2(p1.x - nx * hw, p1.y - ny * hw), B2(p2.x - nx * hw, p2.y - ny * hw);   // l2: A2->B2

    bool first = true;
    auto add = [&](const nlohmann::json& entity) -> std::string {
        nlohmann::json op = {{"op", "sketch_add_entity"}, {"sketch", fid_}, {"entity", entity}};
        auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}), /*coalesce=*/!first);
        first = false;
        if (!r.ok) { st.addLog("[sketch] slot part failed: " + r.error_message); return std::string(); }
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
        return (r.id_map.is_object() && r.id_map.contains("$0"))
             ? r.id_map["$0"].get<std::string>() : std::string();
    };
    std::string l1 = add({{"type", "line"}, {"p1", {A1.x, A1.y}}, {"p2", {B1.x, B1.y}}});
    std::string l2 = add({{"type", "line"}, {"p1", {A2.x, A2.y}}, {"p2", {B2.x, B2.y}}});
    // c1 at p1: start=axis+90 (=A1) -> end=axis-90 (=A2), CCW (outer bulge away from p2).
    std::string c1 = add({{"type", "arc"}, {"center", {p1.x, p1.y}}, {"radius", hw},
                          {"start", axisDeg + 90.0f}, {"end", axisDeg - 90.0f}, {"ccw", true}});
    // c2 at p2: start=axis-90 (=B2) -> end=axis+90 (=B1), CCW (outer bulge away from p1).
    std::string c2 = add({{"type", "arc"}, {"center", {p2.x, p2.y}}, {"radius", hw},
                          {"start", axisDeg - 90.0f}, {"end", axisDeg + 90.0f}, {"ccw", true}});
    std::string sp = add({{"type", "line"}, {"construction", true},
                          {"p1", {p1.x, p1.y}}, {"p2", {p2.x, p2.y}}});
    if (l1.empty() || l2.empty() || c1.empty() || c2.empty() || sp.empty()) { resolve(st); return; }

    auto con = [&](const nlohmann::json& c) {
        nlohmann::json op = {{"op", "sketch_add_constraint"}, {"sketch", fid_}, {"constraint", c}};
        st.docMirror.applyOps(st.worker, nlohmann::json::array({op}), /*coalesce=*/true);
    };
    con({{"type", "coincident"}, {"refs", {l1 + ".p1", c1 + ".start"}}});   // A1
    con({{"type", "coincident"}, {"refs", {l1 + ".p2", c2 + ".end"}}});     // B1
    con({{"type", "coincident"}, {"refs", {l2 + ".p1", c1 + ".end"}}});     // A2
    con({{"type", "coincident"}, {"refs", {l2 + ".p2", c2 + ".start"}}});   // B2
    con({{"type", "coincident"}, {"refs", {sp + ".p1", c1 + ".center"}}});  // centerline -> centers
    con({{"type", "coincident"}, {"refs", {sp + ".p2", c2 + ".center"}}});
    con({{"type", "equal"}, {"refs", {c1, c2}}});                           // uniform cap radius
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
        if (selIds_.empty()) {
            st.toast(ui::ToastLevel::Info, "Add Relation: select sketch entities or points first");
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
            st.toast(ui::ToastLevel::Info, "Fix: select sketch entities or points first");
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
        default:                          return "";
    }
}

// ---------------------------------------------------------------------------
// tool helpers
// ---------------------------------------------------------------------------

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
                               pastePlacing_ = false; pasteBundle_ = nlohmann::json(); }  // Wave 4C paste-placement
void SketchMode::cancelTool() { resetTool(); }

// ---------------------------------------------------------------------------
// Sketch Text + Equation-Driven Curve parameter popups (engine entities text / eq_curve)
// ---------------------------------------------------------------------------

// Sketch Text: a small borderless popup (mirrors the Add-Relation popup) collecting the string +
// cap height for a "text" entity placed at the click-captured lower-left anchor (textAnchorUV_).
// OK emits {"type":"text","construction":false,"p":[u,v],"text":...,"height":...} (font omitted ->
// engine default, safer cross-machine). Empty text disables OK. Esc/Cancel closes; the tool stays
// armed for another placement.
void SketchMode::drawTextPopup(AppState& st, bool& escHandled) {
    if (!textPopupOpen_) return;
    const auto& pal = ui::palette;
    ImGui::SetNextWindowPos(textPopupPos_, ImGuiCond_Appearing);
    ImGui::SetNextWindowBgAlpha(0.98f);
    if (ImGui::Begin("##SketchText", nullptr,
                     ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                     ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
        ImGui::TextUnformatted("Sketch Text");
        ImGui::PopStyleColor();
        ImGui::Separator();
        ImGui::TextUnformatted("Text");
        ImGui::SetNextItemWidth(220);
        if (textFocus_) { ImGui::SetKeyboardFocusHere(); textFocus_ = false; }
        bool enter = ImGui::InputText("##textstr", textBuf_, sizeof(textBuf_),
                                      ImGuiInputTextFlags_EnterReturnsTrue);
        ImGui::SetNextItemWidth(120);
        ImGui::InputFloat("Height (mm)", &textHeight_, 1.0f, 5.0f, "%.3f");
        if (textHeight_ < 0.001f) textHeight_ = 0.001f;   // engine requires height > 0
        ImGui::Separator();
        const bool empty = (textBuf_[0] == '\0');
        ImGui::BeginDisabled(empty);
        bool ok = ImGui::Button("OK") || (enter && !empty);
        ImGui::EndDisabled();
        ImGui::SameLine();
        bool escPressed = ImGui::IsKeyPressed(ImGuiKey_Escape);
        bool cancel = ImGui::Button("Cancel") || escPressed;
        if (escPressed) escHandled = true;   // suppress the catch-all Esc (it would resetTool)
        if (empty && enter)
            st.toast(ui::ToastLevel::Info, "Text: enter a non-empty string");
        if (ok && !empty) {
            nlohmann::json ent = {{"type", "text"}, {"construction", false},
                {"p", nlohmann::json::array({textAnchorUV_.x, textAnchorUV_.y})},
                {"text", std::string(textBuf_)},
                {"height", (double)textHeight_}};
            addEntity(st, ent, nlohmann::json::array());
            textPopupOpen_ = false;
            resetTool(); tool_ = SketchTool::Text;   // stay armed for another placement (one-shot)
        } else if (cancel) {
            textPopupOpen_ = false;
            // keep the tool armed so the next click can place again
        }
    }
    ImGui::End();
}

// Equation-Driven Curve: a borderless popup collecting x(t)/y(t) expression strings + the t range
// for an "eq_curve" entity (origin-relative; no viewport pick). OK emits
// {"type":"eq_curve","construction":false,"xt":...,"yt":...,"t1":...,"t2":...}. Preset buttons fill
// the fields with a circle / sine wave. A closed curve (cos/sin over 0..2pi) extrudes as a region.
void SketchMode::drawEqCurvePopup(AppState& st, bool& escHandled) {
    if (!eqPopupOpen_) return;
    const auto& pal = ui::palette;
    ImGui::SetNextWindowPos(eqPopupPos_, ImGuiCond_Appearing);
    ImGui::SetNextWindowBgAlpha(0.98f);
    if (ImGui::Begin("##SketchEqCurve", nullptr,
                     ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                     ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
        ImGui::TextUnformatted("Equation Driven Curve");
        ImGui::PopStyleColor();
        ImGui::Separator();
        // Presets fill the expression fields (parameter t in radians).
        if (ImGui::SmallButton("Circle")) {
            std::snprintf(eqXt_, sizeof(eqXt_), "5*cos(t)");
            std::snprintf(eqYt_, sizeof(eqYt_), "5*sin(t)");
            eqT1_ = 0.0f; eqT2_ = 6.2831853f;
        }
        ImGui::SameLine();
        if (ImGui::SmallButton("Sine wave")) {
            std::snprintf(eqXt_, sizeof(eqXt_), "t");
            std::snprintf(eqYt_, sizeof(eqYt_), "5*sin(t)");
            eqT1_ = 0.0f; eqT2_ = 6.2831853f;
        }
        ImGui::Separator();
        ImGui::SetNextItemWidth(220);
        if (eqFocus_) { ImGui::SetKeyboardFocusHere(); eqFocus_ = false; }
        ImGui::InputText("x(t)", eqXt_, sizeof(eqXt_));
        ImGui::SetNextItemWidth(220);
        ImGui::InputText("y(t)", eqYt_, sizeof(eqYt_));
        ImGui::SetNextItemWidth(120);
        ImGui::InputFloat("t start", &eqT1_, 0.0f, 0.0f, "%.6f");
        ImGui::SetNextItemWidth(120);
        ImGui::InputFloat("t end (rad)", &eqT2_, 0.0f, 0.0f, "%.6f");
        ImGui::Separator();
        const bool empty = (eqXt_[0] == '\0' || eqYt_[0] == '\0');
        const bool badRange = (eqT2_ == eqT1_);
        ImGui::BeginDisabled(empty || badRange);
        bool ok = ImGui::Button("OK");
        ImGui::EndDisabled();
        ImGui::SameLine();
        bool escPressed = ImGui::IsKeyPressed(ImGuiKey_Escape);
        bool cancel = ImGui::Button("Cancel") || escPressed;
        if (escPressed) escHandled = true;   // suppress the catch-all Esc (it would also resetTool)
        if (ok && !empty && !badRange) {
            nlohmann::json ent = {{"type", "eq_curve"}, {"construction", false},
                {"xt", std::string(eqXt_)}, {"yt", std::string(eqYt_)},
                {"t1", (double)eqT1_}, {"t2", (double)eqT2_}};
            addEntity(st, ent, nlohmann::json::array());
            eqPopupOpen_ = false;
            resetTool();   // one entity per invocation, like Polygon/Ellipse
        } else if (cancel) {
            eqPopupOpen_ = false;
            resetTool();
        }
    }
    ImGui::End();
}

// C52: apply one sketch_fillet / sketch_chamfer op for the held corner (e1=corner1Id_, e2=line2).
// The engine round-trips like every other sketch-editing op (applyOps -> loadDocMesh -> resolve).
// On an OpError (unsupported_entity / fillet_failed / chamfer_failed / invalid_op) the sketch is
// NOT mutated; we toast the engine message and reset the first pick so the user starts a fresh
// pair. On success the new arc/line appears and the sketch re-solves; we stay armed for the next
// corner (SolidWorks keeps filleting until Esc).
void SketchMode::applyCornerOp(AppState& st, const std::string& line2) {
    const bool fillet = (tool_ == SketchTool::SkFillet);
    nlohmann::json op = {{"op", fillet ? "sketch_fillet" : "sketch_chamfer"},
                         {"sketch", fid_}, {"e1", corner1Id_}, {"e2", line2}};
    if (fillet) op["radius"]   = skFilletRadius_;
    else        op["distance"] = skChamferDist_;
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    if (!r.ok) {
        // Engine refused (bad corner / unsupported entity): surface the message, keep tool armed.
        std::string msg = r.error_message.empty()
            ? std::string(fillet ? "Sketch fillet failed" : "Sketch chamfer failed")
            : r.error_message;
        st.addLog(std::string("[sketch] ") + (fillet ? "fillet" : "chamfer") + " failed: " + msg);
        st.toast(ui::ToastLevel::Warn, msg);
        corner1Id_.clear();   // start a fresh pair (match Offset's reset-on-error UX)
        return;
    }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);              // refresh entities/statuses (same path as trim/extend/offset)
    corner1Id_.clear();       // ready for the next corner; stay in the tool
}

// C53: apply sketch_mirror_entities (entities=mpSel_, line=lineId) and refresh. On engine OpError
// the message is toasted and the selection is reset (SolidWorks lets you start a fresh mirror).
void SketchMode::applyMirrorOp(AppState& st, const std::string& lineId) {
    nlohmann::json eids = nlohmann::json::array();
    for (const auto& id : mpSel_) eids.push_back(id);
    nlohmann::json op = {{"op", "sketch_mirror_entities"}, {"sketch", fid_},
                         {"entities", eids}, {"line", lineId}};
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    if (!r.ok) {
        std::string msg = r.error_message.empty() ? std::string("Mirror failed") : r.error_message;
        st.addLog("[sketch] mirror failed: " + msg);
        st.toast(ui::ToastLevel::Warn, msg);
    } else {
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
        resolve(st);
    }
    mpStage_ = 0; mpSel_.clear();   // stay in the tool, ready for another mirror
}

// C53: apply sketch_pattern_entities (linear or circular) for the current selection + dims.
void SketchMode::applyPatternOp(AppState& st, bool circular) {
    nlohmann::json eids = nlohmann::json::array();
    for (const auto& id : mpSel_) eids.push_back(id);
    nlohmann::json op = {{"op", "sketch_pattern_entities"}, {"sketch", fid_},
                         {"mode", circular ? "circular" : "linear"},
                         {"entities", eids}, {"count", patternCount_}};
    if (circular) {
        op["center"] = nlohmann::json::array({patternCenterU_, patternCenterV_});
        op["angle"] = patternAngle_;
        op["equal_spacing"] = patternEqualSpacing_;
    } else {
        op["dir"] = nlohmann::json::array({patternDirU_, patternDirV_});
        op["spacing"] = patternSpacing_;
    }
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    if (!r.ok) {
        std::string msg = r.error_message.empty() ? std::string("Pattern failed") : r.error_message;
        st.addLog("[sketch] pattern failed: " + msg);
        st.toast(ui::ToastLevel::Warn, msg);
    } else {
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
        resolve(st);
    }
    mpStage_ = 0; mpSel_.clear();   // stay in the tool, ready for another pattern
}

// C53: apply one sketch_convert op for a model edge/face ref (a {kind, selector} or {datum} object).
void SketchMode::applyConvertRef(AppState& st, const nlohmann::json& ref) {
    nlohmann::json op = {{"op", "sketch_convert"}, {"sketch", fid_},
                         {"refs", nlohmann::json::array({ref})}};
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    if (!r.ok) {
        std::string msg = r.error_message.empty() ? std::string("Convert failed") : r.error_message;
        st.addLog("[sketch] convert failed: " + msg);
        st.toast(ui::ToastLevel::Warn, msg);
        return;
    }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);   // the projected entity appears; stay armed for more converts
}

// C54: apply sketch_move (translate or copy) for the current selection + delta.
void SketchMode::applyMoveOp(AppState& st, float dx, float dy, bool copy) {
    nlohmann::json eids = nlohmann::json::array();
    for (const auto& id : mpSel_) eids.push_back(id);
    nlohmann::json op = {{"op", "sketch_move"}, {"sketch", fid_},
                         {"entities", eids}, {"dx", dx}, {"dy", dy}, {"copy", copy}};
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    if (!r.ok) {
        std::string msg = r.error_message.empty() ? std::string("Move failed") : r.error_message;
        st.addLog("[sketch] move failed: " + msg);
        st.toast(ui::ToastLevel::Warn, msg);
    } else {
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
        resolve(st);
    }
    mpStage_ = 0; mpSel_.clear();   // stay in the tool, ready for another move
}

// C54: resolve the tangent-arc source — the line/circle/arc nearest the cursor, the endpoint of it
// nearest the cursor (the arc start), and the unit tangent direction there (pointing into the arc).
// Prefers the last selected entity, then the entity under the cursor, then the last drawn entity.
bool SketchMode::tangentArcSource(const ImVec2& cursorScreen, TangentArcSrc& out) const {
    const SketchEntity* src = nullptr;
    // 1) a held selection wins (its last-picked entity is the source).
    for (auto it = selIds_.rbegin(); it != selIds_.rend() && !src; ++it) {
        const SketchEntity* e = entById(*it);
        if (e && (e->type == "line" || e->type == "arc" || e->type == "circle")) src = e;
    }
    // 2) else the entity under the cursor.
    if (!src) {
        int h = hitTestEntity(cursorScreen);
        if (h >= 0 && (ents_[h].type == "line" || ents_[h].type == "arc" ||
                       ents_[h].type == "circle"))
            src = &ents_[h];
    }
    // 3) else the most-recently-drawn supported entity (last in the list).
    if (!src) {
        for (auto it = ents_.rbegin(); it != ents_.rend() && !src; ++it)
            if (it->type == "line" || it->type == "arc" || it->type == "circle") src = &*it;
    }
    if (!src) return false;

    const float D2R = 3.14159265f / 180.0f;
    if (src->type == "line") {
        ImVec2 p1(src->p1[0], src->p1[1]), p2(src->p2[0], src->p2[1]);
        ImVec2 s1 = uvToScreen(p1.x, p1.y), s2 = uvToScreen(p2.x, p2.y);
        bool useP1 = std::hypot(s1.x - cursorScreen.x, s1.y - cursorScreen.y) <=
                     std::hypot(s2.x - cursorScreen.x, s2.y - cursorScreen.y);
        ImVec2 endpt = useP1 ? p1 : p2;
        ImVec2 other = useP1 ? p2 : p1;
        // Tangent continues the segment outward (away from the line body).
        float dx = endpt.x - other.x, dy = endpt.y - other.y;
        float L = std::hypot(dx, dy);
        if (L < 1e-6f) return false;
        out.id = src->id;
        out.handle = src->id + (useP1 ? ".p1" : ".p2");
        out.start = endpt;
        out.tdir = ImVec2(dx / L, dy / L);
        return true;
    }
    // arc / circle: tangent is perpendicular to the radius at the endpoint.
    // A full circle has no free endpoint — fall back to the point on it nearest the cursor.
    float a0 = src->start, a1 = src->end;
    ImVec2 c(src->center[0], src->center[1]);
    float rad = src->radius;
    if (src->type == "arc") {
        ImVec2 pa(c.x + rad * std::cos(a0 * D2R), c.y + rad * std::sin(a0 * D2R));
        ImVec2 pb(c.x + rad * std::cos(a1 * D2R), c.y + rad * std::sin(a1 * D2R));
        ImVec2 sa = uvToScreen(pa.x, pa.y), sb = uvToScreen(pb.x, pb.y);
        bool useStart = std::hypot(sa.x - cursorScreen.x, sa.y - cursorScreen.y) <=
                        std::hypot(sb.x - cursorScreen.x, sb.y - cursorScreen.y);
        float ang = (useStart ? a0 : a1) * D2R;
        ImVec2 endpt(c.x + rad * std::cos(ang), c.y + rad * std::sin(ang));
        // Radial unit, then rotate +90 for the tangent; orient it along the arc's sweep so the new
        // arc continues smoothly outward (ccw => +tangent at end / -tangent at start).
        float rx = std::cos(ang), ry = std::sin(ang);
        float tx = -ry, ty = rx;             // +90 rotation of the radial
        bool ccw = src->ccw;
        float sgn = (useStart ? -1.0f : 1.0f) * (ccw ? 1.0f : -1.0f);
        out.id = src->id;
        out.handle = src->id + (useStart ? ".start" : ".end");
        out.start = endpt;
        out.tdir = ImVec2(tx * sgn, ty * sgn);
        return true;
    }
    // circle: nearest rim point to the cursor; tangent perpendicular to the radius (ccw sense).
    float cu, cv; screenToUV(cursorScreen, cu, cv);
    float ang = std::atan2(cv - c.y, cu - c.x);
    ImVec2 endpt(c.x + rad * std::cos(ang), c.y + rad * std::sin(ang));
    out.id = src->id;
    out.handle = src->id;                    // whole-circle ref (no endpoint sub-handle)
    out.start = endpt;
    out.tdir = ImVec2(-std::sin(ang), std::cos(ang));   // ccw tangent
    return true;
}

// C54: solve the arc tangent to `tdir` at `start`, passing through `end`. The center lies on the
// line perpendicular to tdir at start (start + r*N). Requiring |C-end| == |C-start| == |r| gives
// r = |end-start|^2 / (2 * (end-start)·N), with N the left-normal of tdir. Returns false when the
// end is on the tangent line through start (denominator ~0 -> infinite radius) or coincident.
bool SketchMode::tangentArcThrough(const ImVec2& start, const ImVec2& tdir, const ImVec2& end,
                                   float& cu, float& cv, float& rad,
                                   float& a0deg, float& a1deg, bool& ccw) {
    float tx = tdir.x, ty = tdir.y;
    float tl = std::hypot(tx, ty);
    if (tl < 1e-9f) return false;
    tx /= tl; ty /= tl;
    float nx = -ty, ny = tx;                 // left-normal of the tangent
    float ex = end.x - start.x, ey = end.y - start.y;
    float d2 = ex * ex + ey * ey;
    if (d2 < 1e-12f) return false;           // start == end
    float denom = 2.0f * (ex * nx + ey * ny);
    if (std::fabs(denom) < 1e-9f) return false;   // end on the tangent line -> straight, no arc
    float r = d2 / denom;                    // signed radius (sign picks the side)
    cu = start.x + r * nx;
    cv = start.y + r * ny;
    rad = std::fabs(r);
    const float R2D = 57.29577951f;
    a0deg = std::atan2(start.y - cv, start.x - cu) * R2D;
    a1deg = std::atan2(end.y - cv, end.x - cu) * R2D;
    // The sweep direction is set by the tangent: cross(radial_at_start, tangent) sign. radial at
    // start is (start-center) = -r*N; tangent is T. ccw when cross(radial, T) > 0.
    float radx = start.x - cu, rady = start.y - cv;
    ccw = (radx * ty - rady * tx) > 0.0f;
    return true;
}

void SketchMode::snapPoint(float& u, float& v, std::string& snapHandle, SnapInfer* infer) const {
    snapHandle.clear();
    const ImVec2 cur = uvToScreen(u, v);   // raw cursor (snap distances measure from here)
    // Visit every snappable POINT (handle, u, v) — point snap + alignment share this set.
    auto forEachPoint = [&](const std::function<void(const std::string&, float, float)>& fn) {
        fn("origin", 0.0f, 0.0f);   // the sketch origin is a first-class snap/anchor target
        for (const auto& e : ents_) {
            if (e.type == "line" || e.type == "slot") {
                fn(e.id + ".p1", e.p1[0], e.p1[1]);
                fn(e.id + ".p2", e.p2[0], e.p2[1]);
            } else if (e.type == "circle" || e.type == "arc" || e.type == "arc_slot" ||
                       e.type == "ellipse") {
                fn(e.id + ".center", e.center[0], e.center[1]);
            } else if (e.type == "point") {
                fn(e.id + ".p", e.p1[0], e.p1[1]);
            } else if (e.type == "spline" || e.type == "style_spline") {  // C42: sub-handles p0..pN (sketch_solver convention; style_spline = control poles)
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
        } else if (e.type == "spline" || e.type == "style_spline") {   // C42: draggable through-points / control poles p0..pN
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
        if (e.type == "line") {
            ImVec2 a = uvToScreen(e.p1[0], e.p1[1]), b = uvToScreen(e.p2[0], e.p2[1]);
            float vx = b.x - a.x, vy = b.y - a.y;
            float wx = s.x - a.x, wy = s.y - a.y;
            float t = (vx * wx + vy * wy) / std::fmax(1e-6f, vx * vx + vy * vy);
            t = std::fmax(0.0f, std::fmin(1.0f, t));
            float px = a.x + t * vx, py = a.y + t * vy;
            d2 = (px - s.x) * (px - s.x) + (py - s.y) * (py - s.y);
        } else if (e.type == "slot" || e.type == "arc_slot") {
            // Distance to the visible capsule OUTLINE (its body) AND the centerline spine, so the
            // slot selects whether you click its edge OR its centerline (the spine is real, pickable
            // geometry — SolidWorks lets you dimension it). Segment-distance over both.
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
            if (e.type == "slot")   // straight centerline spine
                d2 = std::fmin(d2, segD2(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1])));
        } else if (e.type == "circle" || e.type == "arc") {
            ImVec2 cc = uvToScreen(e.center[0], e.center[1]);
            ImVec2 rim = uvToScreen(e.center[0] + e.radius, e.center[1]);
            float rpx = std::hypot(rim.x - cc.x, rim.y - cc.y);
            float dm = std::hypot(s.x - cc.x, s.y - cc.y);
            d2 = (dm - rpx) * (dm - rpx);
        } else if (e.type == "point") {                       // C42: distance to the point itself
            ImVec2 p = uvToScreen(e.p1[0], e.p1[1]);
            d2 = (p.x - s.x) * (p.x - s.x) + (p.y - s.y) * (p.y - s.y);
        } else if (e.type == "ellipse" || e.type == "spline" || e.type == "style_spline") {
            // C42: min distance to a sampled polyline (32-seg ellipse / spline through-points /
            // style_spline control polygon — the displayed editable polyline, SW-style).
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
// C51: closed-profile regions + box-select geometry
// ---------------------------------------------------------------------------
namespace {
// Even-odd point-in-polygon test on a (u,v) polygon (implicitly closed last->first).
bool pointInPolyUV(const std::vector<ImVec2>& poly, float x, float y) {
    bool in = false;
    size_t n = poly.size();
    if (n < 3) return false;
    for (size_t i = 0, j = n - 1; i < n; j = i++) {
        bool cross = (poly[i].y > y) != (poly[j].y > y);
        if (cross && x < (poly[j].x - poly[i].x) * (y - poly[i].y) /
                             (poly[j].y - poly[i].y) + poly[i].x)
            in = !in;
    }
    return in;
}
// |signed area| of a (u,v) polygon (shoelace).
float polyAreaUV(const std::vector<ImVec2>& p) {
    float a = 0.0f;
    for (size_t i = 0, j = p.size() - 1; i < p.size(); j = i++)
        a += (p[j].x + p[i].x) * (p[j].y - p[i].y);
    return std::fabs(a) * 0.5f;
}
bool ptInRect(const ImVec2& p, const ImVec2& lo, const ImVec2& hi) {
    return p.x >= lo.x && p.x <= hi.x && p.y >= lo.y && p.y <= hi.y;
}
// Do segments p1p2 and p3p4 (screen space) properly cross?
bool segSegCross(const ImVec2& p1, const ImVec2& p2, const ImVec2& p3, const ImVec2& p4) {
    auto cr = [](const ImVec2& a, const ImVec2& b, const ImVec2& c) {
        return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
    };
    float d1 = cr(p3, p4, p1), d2 = cr(p3, p4, p2);
    float d3 = cr(p1, p2, p3), d4 = cr(p1, p2, p4);
    return ((d1 > 0) != (d2 > 0)) && ((d3 > 0) != (d4 > 0));
}
// Does segment a-b cross any edge of the axis-aligned rect [lo,hi]?
bool segCrossesRect(const ImVec2& a, const ImVec2& b, const ImVec2& lo, const ImVec2& hi) {
    ImVec2 c00(lo.x, lo.y), c10(hi.x, lo.y), c11(hi.x, hi.y), c01(lo.x, hi.y);
    return segSegCross(a, b, c00, c10) || segSegCross(a, b, c10, c11) ||
           segSegCross(a, b, c11, c01) || segSegCross(a, b, c01, c00);
}
}  // namespace

// Sample one entity's boundary into sketch-(u,v) points (no duplicated closing vertex).
void SketchMode::entityBoundaryUV(const SketchEntity& e, std::vector<ImVec2>& out,
                                  bool& closed) const {
    out.clear();
    closed = false;
    const float D2R = 3.14159265f / 180.0f;
    if (e.type == "line") {
        out.push_back(ImVec2(e.p1[0], e.p1[1]));
        out.push_back(ImVec2(e.p2[0], e.p2[1]));
    } else if (e.type == "point") {
        out.push_back(ImVec2(e.p1[0], e.p1[1]));
    } else if (e.type == "arc") {
        float a0 = e.start, a1 = e.end;
        if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
        if (a1 < a0) a1 += 360.0f;
        int seg = 32;
        for (int i = 0; i <= seg; ++i) {
            float a = (a0 + (a1 - a0) * (i / (float)seg)) * D2R;
            out.push_back(ImVec2(e.center[0] + e.radius * std::cos(a),
                                 e.center[1] + e.radius * std::sin(a)));
        }
    } else if (e.type == "circle") {
        int seg = 48; closed = true;
        for (int i = 0; i < seg; ++i) {
            float a = i / (float)seg * 6.2831853f;
            out.push_back(ImVec2(e.center[0] + e.radius * std::cos(a),
                                 e.center[1] + e.radius * std::sin(a)));
        }
    } else if (e.type == "ellipse") {
        int seg = 48; closed = true;
        float ca = std::cos(e.rot * D2R), sa = std::sin(e.rot * D2R);
        for (int i = 0; i < seg; ++i) {
            float a = i / (float)seg * 6.2831853f;
            float lx = e.rx * std::cos(a), ly = e.ry * std::sin(a);
            out.push_back(ImVec2(e.center[0] + lx * ca - ly * sa,
                                 e.center[1] + lx * sa + ly * ca));
        }
    } else if (e.type == "slot") {
        // Capsule perimeter walked in order: side line (+normal) p1->p2, round cap bulging past p2,
        // side line (-normal) p2->p1, round cap bulging past p1. The cap interiors (i=1..11) skip
        // the side-line endpoints so no vertex is duplicated. axis = spine direction p1->p2; the
        // +normal endpoint sits at angle axis+90, the -normal endpoint at axis-90.
        closed = true;
        const float HALF_PI = 1.5707963f, PI = 3.14159265f;
        float dx = e.p2[0] - e.p1[0], dy = e.p2[1] - e.p1[1];
        float L = std::hypot(dx, dy), hw = e.width * 0.5f;
        float axis = std::atan2(dy, dx);
        float nx = -std::sin(axis), ny = std::cos(axis);   // unit +normal
        out.push_back(ImVec2(e.p1[0] + nx * hw, e.p1[1] + ny * hw));
        out.push_back(ImVec2(e.p2[0] + nx * hw, e.p2[1] + ny * hw));
        for (int i = 1; i < 12; ++i) {      // cap at p2: axis+90 -> axis-90 (bulge toward +axis)
            float t = axis + HALF_PI - PI * (i / 12.0f);
            out.push_back(ImVec2(e.p2[0] + hw * std::cos(t), e.p2[1] + hw * std::sin(t)));
        }
        out.push_back(ImVec2(e.p2[0] - nx * hw, e.p2[1] - ny * hw));
        out.push_back(ImVec2(e.p1[0] - nx * hw, e.p1[1] - ny * hw));
        for (int i = 1; i < 12; ++i) {      // cap at p1: axis-90 -> axis-270 (bulge toward -axis)
            float t = axis - HALF_PI - PI * (i / 12.0f);
            out.push_back(ImVec2(e.p1[0] + hw * std::cos(t), e.p1[1] + hw * std::sin(t)));
        }
        (void)L;
    } else if (e.type == "arc_slot") {
        closed = true;
        float hw = e.width * 0.5f;
        float a0 = e.start, a1 = e.end; if (a1 < a0) a1 += 360.0f;
        for (int i = 0; i <= 32; ++i) {     // outer arc
            float a = (a0 + (a1 - a0) * (i / 32.0f)) * D2R;
            out.push_back(ImVec2(e.center[0] + (e.radius + hw) * std::cos(a),
                                 e.center[1] + (e.radius + hw) * std::sin(a)));
        }
        for (int i = 32; i >= 0; --i) {     // inner arc, back
            float a = (a0 + (a1 - a0) * (i / 32.0f)) * D2R;
            out.push_back(ImVec2(e.center[0] + (e.radius - hw) * std::cos(a),
                                 e.center[1] + (e.radius - hw) * std::sin(a)));
        }
    } else if (e.type == "spline" || e.type == "style_spline") {
        for (const auto& q : e.pts) out.push_back(q);
    }
}

// Detect closed loops among the live non-construction entities. Mirrors the engine connectivity
// walk (features/sketch.py _detect_loops): self-closed shapes are one-entity regions; line/arc
// edges are chained by shared endpoints until they return to the loop head. Greedy and single-use
// per edge (an edge consumed by a failed open chain won't re-seed another), matching the engine.
void SketchMode::rebuildRegions() {
    regions_.clear();
    const float EPS = 1e-3f;   // uv (mm) endpoint coincidence tolerance
    auto near2 = [&](const ImVec2& a, const ImVec2& b) {
        return std::fabs(a.x - b.x) < EPS && std::fabs(a.y - b.y) < EPS;
    };
    auto finish = [&](SketchRegion&& r) {
        // Strip consecutive near-duplicate vertices and a trailing vertex equal to the head. The
        // connectivity walk re-appends the loop head when it closes (line A..D then D->A leaves
        // [A,B,C,D,A]); that repeated corner makes the ear-clip fill collapse, so drop it here.
        std::vector<ImVec2> clean;
        for (const auto& p : r.poly) {
            if (!clean.empty() && std::fabs(clean.back().x - p.x) < EPS &&
                std::fabs(clean.back().y - p.y) < EPS) continue;
            clean.push_back(p);
        }
        while (clean.size() >= 2 &&
               std::fabs(clean.front().x - clean.back().x) < EPS &&
               std::fabs(clean.front().y - clean.back().y) < EPS)
            clean.pop_back();
        r.poly.swap(clean);
        if (r.poly.size() < 3) return;
        r.area = polyAreaUV(r.poly);
        if (r.area < 1e-4f) return;   // degenerate
        regions_.push_back(std::move(r));
    };

    // 1) self-closed entities -> single-entity regions.
    for (const auto& e : ents_) {
        if (e.construction) continue;
        if (e.type == "circle" || e.type == "ellipse" || e.type == "slot" || e.type == "arc_slot") {
            SketchRegion r; r.ids.push_back(e.id);
            bool cl; entityBoundaryUV(e, r.poly, cl);
            finish(std::move(r));
        }
    }

    // 2) chainable line/arc edges -> connectivity walk.
    struct Edge { int idx; std::vector<ImVec2> pts; };   // pts: boundary, pts.front()/back() = ends
    std::vector<Edge> edges;
    for (size_t i = 0; i < ents_.size(); ++i) {
        const auto& e = ents_[i];
        if (e.construction) continue;
        if (e.type != "line" && e.type != "arc") continue;
        Edge ed; ed.idx = (int)i; bool cl;
        entityBoundaryUV(e, ed.pts, cl);
        if (ed.pts.size() >= 2) edges.push_back(std::move(ed));
    }
    std::vector<char> used(edges.size(), 0);
    for (size_t s = 0; s < edges.size(); ++s) {
        if (used[s]) continue;
        used[s] = 1;
        SketchRegion r;
        r.ids.push_back(ents_[edges[s].idx].id);
        r.poly = edges[s].pts;                       // a..b in natural order
        ImVec2 head = edges[s].pts.front();
        ImVec2 tip = edges[s].pts.back();
        bool closedLoop = false;
        while (true) {
            int found = -1; bool rev = false;
            for (size_t j = 0; j < edges.size(); ++j) {
                if (used[j]) continue;
                if (near2(edges[j].pts.front(), tip)) { found = (int)j; rev = false; break; }
                if (near2(edges[j].pts.back(), tip))  { found = (int)j; rev = true;  break; }
            }
            if (found < 0) break;
            used[found] = 1;
            r.ids.push_back(ents_[edges[found].idx].id);
            const auto& fp = edges[found].pts;
            // append the new edge's points (skip the shared junction vertex), oriented to flow
            // away from the current tip.
            if (rev) { for (size_t k = fp.size(); k-- > 0; ) if (k != fp.size() - 1) r.poly.push_back(fp[k]); tip = fp.front(); }
            else     { for (size_t k = 1; k < fp.size(); ++k) r.poly.push_back(fp[k]); tip = fp.back(); }
            if (near2(tip, head)) { closedLoop = true; break; }
        }
        if (closedLoop) finish(std::move(r));
    }
}

// Smallest-area region whose polygon contains the screen point (innermost wins), or -1.
int SketchMode::hitTestRegion(const ImVec2& s) const {
    float u, v; screenToUV(s, u, v);
    int hit = -1; float best = 1e30f;
    for (size_t i = 0; i < regions_.size(); ++i) {
        if (pointInPolyUV(regions_[i].poly, u, v) && regions_[i].area < best) {
            best = regions_[i].area; hit = (int)i;
        }
    }
    return hit;
}

// The MULTI-entity loop (rectangle/polygon/custom chain) that entId is part of, or -1. Single-entity
// regions (a lone circle/slot) don't count — there's no "loop" to preview beyond the entity itself.
int SketchMode::regionContaining(const std::string& entId) const {
    for (size_t i = 0; i < regions_.size(); ++i) {
        if (regions_[i].ids.size() < 2) continue;
        if (std::find(regions_[i].ids.begin(), regions_[i].ids.end(), entId) != regions_[i].ids.end())
            return (int)i;
    }
    return -1;
}

// Stroke an entity's outline on top of the geometry (hover pre-highlight / loop preview).
void SketchMode::strokeEntityOutline(ImDrawList* dl, const SketchEntity& e, ImU32 c, float th) const {
    std::vector<ImVec2> uv; bool closed; entityBoundaryUV(e, uv, closed);
    if (uv.empty()) return;
    if (uv.size() == 1) { dl->AddCircle(uvToScreen(uv[0].x, uv[0].y), 5.0f, c, 12, th); return; }
    std::vector<ImVec2> sp; sp.reserve(uv.size());
    for (const auto& p : uv) sp.push_back(uvToScreen(p.x, p.y));
    for (size_t i = 1; i < sp.size(); ++i) dl->AddLine(sp[i - 1], sp[i], c, th);
    if (closed) dl->AddLine(sp.back(), sp.front(), c, th);
}

bool SketchMode::isRelSelected(const std::string& cid) const {
    return std::find(selRelIds_.begin(), selRelIds_.end(), cid) != selRelIds_.end();
}

// Topmost relation badge (last drawn = on top) whose screen rect contains s, or -1.
int SketchMode::hitTestRelationBadge(const ImVec2& s) const {
    for (int i = (int)relHits_.size() - 1; i >= 0; --i) {
        const RelHit& r = relHits_[i];
        if (s.x >= r.lo.x && s.x <= r.hi.x && s.y >= r.lo.y && s.y <= r.hi.y) return i;
    }
    return -1;
}

// Select every entity the screen rect (a,b) encloses (crossing=false: fully inside) or touches
// (crossing=true: any vertex inside or any boundary segment crossing the rect). additive keeps
// the current selection; otherwise it is replaced.
void SketchMode::boxSelect(const ImVec2& a, const ImVec2& b, bool crossing, bool additive) {
    ImVec2 lo(std::fmin(a.x, b.x), std::fmin(a.y, b.y));
    ImVec2 hi(std::fmax(a.x, b.x), std::fmax(a.y, b.y));
    if (!additive) selIds_.clear();
    std::vector<ImVec2> uv; bool closed;
    for (const auto& e : ents_) {
        entityBoundaryUV(e, uv, closed);
        if (uv.empty()) continue;
        std::vector<ImVec2> sp; sp.reserve(uv.size());
        for (const auto& p : uv) sp.push_back(uvToScreen(p.x, p.y));
        bool hit = false;
        if (crossing) {
            for (const auto& p : sp) if (ptInRect(p, lo, hi)) { hit = true; break; }
            if (!hit) {
                size_t n = sp.size();
                for (size_t i = 1; i < n && !hit; ++i)
                    if (segCrossesRect(sp[i - 1], sp[i], lo, hi)) hit = true;
                if (!hit && closed && n >= 2 && segCrossesRect(sp[n - 1], sp[0], lo, hi)) hit = true;
            }
        } else {
            hit = true;
            for (const auto& p : sp) if (!ptInRect(p, lo, hi)) { hit = false; break; }
        }
        if (hit && !isSelected(e.id)) selIds_.push_back(e.id);
    }
}

// The point handles that rigidly translate an entity, with their current (u,v). Lines/slots move
// by both endpoints; centered shapes (circle/arc/arc_slot/ellipse) by their center; splines by
// every through-point; a lone point by itself. Targeting all of them at orig+delta translates the
// entity without reshaping it (the engine keeps constraints exact).
void SketchMode::entityDragHandles(const SketchEntity& e,
                                   std::vector<std::pair<std::string, ImVec2>>& out) const {
    if (e.type == "line" || e.type == "slot") {
        out.emplace_back(e.id + ".p1", ImVec2(e.p1[0], e.p1[1]));
        out.emplace_back(e.id + ".p2", ImVec2(e.p2[0], e.p2[1]));
    } else if (e.type == "circle" || e.type == "arc" || e.type == "arc_slot" || e.type == "ellipse") {
        out.emplace_back(e.id + ".center", ImVec2(e.center[0], e.center[1]));
    } else if (e.type == "point") {
        out.emplace_back(e.id + ".p", ImVec2(e.p1[0], e.p1[1]));
    } else if (e.type == "spline" || e.type == "style_spline") {
        for (size_t i = 0; i < e.pts.size(); ++i)
            out.emplace_back(e.id + ".p" + std::to_string(i), e.pts[i]);
    }
}

// Capture the translation handles (+ origin coords) of every entity in `ids` for a group drag.
void SketchMode::beginGroupDrag(const std::vector<std::string>& ids, float startU, float startV) {
    groupDragTargets_.clear();
    for (const auto& id : ids) {
        const SketchEntity* e = entById(id);
        if (e) entityDragHandles(*e, groupDragTargets_);
    }
    groupDragStartUV_ = ImVec2(startU, startV);
    groupDragging_ = true;
}

// Push one transient frame of the group translation: every captured handle soft-targeted at its
// original (u,v) plus the cursor delta. Throttled to once per frame by the caller (dragFrame_).
void SketchMode::updateGroupDrag(AppState& st, float curU, float curV) {
    if (groupDragTargets_.empty()) return;
    float dx = curU - groupDragStartUV_.x, dy = curV - groupDragStartUV_.y;
    nlohmann::json targets = nlohmann::json::array();
    for (const auto& t : groupDragTargets_)
        targets.push_back({{"handle", t.first},
                           {"xy", nlohmann::json::array({t.second.x + dx, t.second.y + dy})}});
    nlohmann::json res;
    if (callSketch(st, "sketch_drag", {{"targets", targets}}, res)) applyEnterResult(res);
}

// ---------------------------------------------------------------------------
// Trim doomed-span preview (client-side mirror of engine sketch_geom.split_entity)
// ---------------------------------------------------------------------------
// The engine splits the target at every in-range crossing with the OTHER lines/circles/arcs and
// deletes the span nearest the click. Replicating that math here lets the Trim tool highlight
// exactly what a click will remove (SolidWorks power-trim red segment) instead of the whole
// entity. Geometry only — the engine stays authoritative on the actual edit.
namespace {
struct TrimHit { float ta; float tb; float px, py; };  // params on target/other + point

// All crossings of two entities among line/circle/arc. ta/tb are the natural parameters
// (line: 0..1; circle/arc: angle in degrees, [0,360)). Mirrors sketch_geom.intersect.
void trimIntersect(const SketchEntity& A, const SketchEntity& B, std::vector<TrimHit>& out) {
    auto angOf = [](float cx, float cy, float x, float y) {
        float a = std::atan2(y - cy, x - cx) * 57.29577951f;
        return a < 0 ? a + 360.0f : a;
    };
    bool aLine = (A.type == "line"), bLine = (B.type == "line");
    if (aLine && bLine) {
        float d1x = A.p2[0] - A.p1[0], d1y = A.p2[1] - A.p1[1];
        float d2x = B.p2[0] - B.p1[0], d2y = B.p2[1] - B.p1[1];
        float den = d1x * d2y - d1y * d2x;
        if (std::fabs(den) < 1e-9f) return;
        float fx = B.p1[0] - A.p1[0], fy = B.p1[1] - A.p1[1];
        float t = (fx * d2y - fy * d2x) / den;
        float u = (fx * d1y - fy * d1x) / den;
        out.push_back({t, u, A.p1[0] + d1x * t, A.p1[1] + d1y * t});
        return;
    }
    if (aLine != bLine) {
        const SketchEntity& L = aLine ? A : B;
        const SketchEntity& C = aLine ? B : A;
        float dx = L.p2[0] - L.p1[0], dy = L.p2[1] - L.p1[1];
        float L2 = dx * dx + dy * dy;
        if (L2 < 1e-14f) return;
        float fx = L.p1[0] - C.center[0], fy = L.p1[1] - C.center[1];
        float b = 2.0f * (fx * dx + fy * dy);
        float c = fx * fx + fy * fy - C.radius * C.radius;
        float disc = b * b - 4.0f * L2 * c;
        if (disc < 0) return;
        float sq = std::sqrt(std::fmax(disc, 0.0f));
        float prevX = 1e30f, prevY = 1e30f;
        for (float t : {(-b - sq) / (2.0f * L2), (-b + sq) / (2.0f * L2)}) {
            float x = L.p1[0] + dx * t, y = L.p1[1] + dy * t;
            if (std::fabs(x - prevX) < 1e-7f && std::fabs(y - prevY) < 1e-7f) continue;  // tangent
            prevX = x; prevY = y;
            float ang = angOf(C.center[0], C.center[1], x, y);
            if (aLine) out.push_back({t, ang, x, y});
            else       out.push_back({ang, t, x, y});
        }
        return;
    }
    // both curved (circle/arc): circle-circle intersection on the underlying circles
    float dxc = B.center[0] - A.center[0], dyc = B.center[1] - A.center[1];
    float d = std::hypot(dxc, dyc);
    if (d < 1e-9f) return;  // concentric
    if (d > A.radius + B.radius + 1e-7f || d < std::fabs(A.radius - B.radius) - 1e-7f) return;
    float a = (A.radius * A.radius - B.radius * B.radius + d * d) / (2.0f * d);
    float h2 = A.radius * A.radius - a * a;
    float h = std::sqrt(std::fmax(h2, 0.0f));
    float ux = dxc / d, uy = dyc / d;
    float mx = A.center[0] + ux * a, my = A.center[1] + uy * a;
    float prevX = 1e30f, prevY = 1e30f;
    for (float sgn : {1.0f, -1.0f}) {
        float x = mx + sgn * h * (-uy), y = my + sgn * h * ux;
        if (std::fabs(x - prevX) < 1e-7f && std::fabs(y - prevY) < 1e-7f) continue;
        prevX = x; prevY = y;
        out.push_back({angOf(A.center[0], A.center[1], x, y),
                       angOf(B.center[0], B.center[1], x, y), x, y});
    }
}

// Is angle (deg) within the arc's drawn sweep? Mirrors sketch_geom._arc_sweep_contains.
bool arcSweepContains(const SketchEntity& arc, float ang) {
    float a0 = std::fmod(arc.start, 360.0f); if (a0 < 0) a0 += 360.0f;
    float a1 = std::fmod(arc.end, 360.0f);   if (a1 < 0) a1 += 360.0f;
    float a = std::fmod(ang, 360.0f);        if (a < 0)  a += 360.0f;
    float span, rel;
    if (arc.ccw) { span = std::fmod(a1 - a0 + 360.0f, 360.0f); rel = std::fmod(a - a0 + 360.0f, 360.0f); }
    else         { span = std::fmod(a0 - a1 + 360.0f, 360.0f); rel = std::fmod(a0 - a + 360.0f, 360.0f); }
    if (span < 1e-7f) span = 360.0f;
    return rel <= span + 1e-5f;
}

bool trimParamInRange(const SketchEntity& e, float t) {
    if (e.type == "line")   return t >= -1e-7f && t <= 1.0f + 1e-7f;
    if (e.type == "circle") return true;
    if (e.type == "arc")    return arcSweepContains(e, t);
    return false;
}
}  // namespace

bool SketchMode::trimDoomedSpan(int idx, float mu, float mv,
                                std::vector<ImVec2>& outUV, bool& wholeEntity) const {
    outUV.clear(); wholeEntity = false;
    if (idx < 0 || idx >= (int)ents_.size()) return false;
    const SketchEntity& T = ents_[idx];
    if (T.type != "line" && T.type != "circle" && T.type != "arc") return false;

    // Cut parameters on the target from every other line/circle/arc (in-range on BOTH).
    std::vector<float> cuts;
    for (size_t i = 0; i < ents_.size(); ++i) {
        if ((int)i == idx) continue;
        const SketchEntity& O = ents_[i];
        if (O.type != "line" && O.type != "circle" && O.type != "arc") continue;
        std::vector<TrimHit> hits;
        trimIntersect(T, O, hits);
        for (const auto& h : hits)
            if (trimParamInRange(O, h.tb) && trimParamInRange(T, h.ta)) cuts.push_back(h.ta);
    }

    auto sampleArc = [&](float cx, float cy, float r, float aa0, float aa1) {
        int seg = 48;
        const float D2R = 3.14159265f / 180.0f;
        for (int i = 0; i <= seg; ++i) {
            float a = (aa0 + (aa1 - aa0) * (i / (float)seg)) * D2R;
            outUV.push_back(ImVec2(cx + r * std::cos(a), cy + r * std::sin(a)));
        }
    };

    if (T.type == "line") {
        // interior cuts sorted, endpoints excluded (engine _dedupe_sorted)
        std::sort(cuts.begin(), cuts.end());
        std::vector<float> inner;
        for (float t : cuts) {
            if (t <= 1e-7f || t >= 1.0f - 1e-7f) continue;
            if (!inner.empty() && std::fabs(t - inner.back()) <= 1e-6f) continue;
            inner.push_back(t);
        }
        if (inner.empty()) { wholeEntity = true; return true; }
        std::vector<float> bounds; bounds.push_back(0.0f);
        for (float t : inner) bounds.push_back(t);
        bounds.push_back(1.0f);
        float dx = T.p2[0] - T.p1[0], dy = T.p2[1] - T.p1[1];
        int best = 0; float bestD = 1e30f;
        for (size_t i = 0; i + 1 < bounds.size(); ++i) {
            float tm = (bounds[i] + bounds[i + 1]) * 0.5f;
            float x = T.p1[0] + dx * tm, y = T.p1[1] + dy * tm;
            float dd = std::hypot(x - mu, y - mv);
            if (dd < bestD) { bestD = dd; best = (int)i; }
        }
        float t0 = bounds[best], t1 = bounds[best + 1];
        outUV.push_back(ImVec2(T.p1[0] + dx * t0, T.p1[1] + dy * t0));
        outUV.push_back(ImVec2(T.p1[0] + dx * t1, T.p1[1] + dy * t1));
        return true;
    }

    if (T.type == "circle") {
        // distinct cut angles; need >= 2 to form removable arcs (engine _split_circle)
        std::vector<float> angs;
        for (float a : cuts) { a = std::fmod(a, 360.0f); if (a < 0) a += 360.0f; angs.push_back(a); }
        std::sort(angs.begin(), angs.end());
        std::vector<float> ded;
        for (float a : angs) {
            if (!ded.empty() && std::fabs(a - ded.back()) <= 1e-6f) continue;
            ded.push_back(a);
        }
        if (ded.size() >= 2 && std::fabs((ded.front() + 360.0f) - ded.back()) <= 1e-6f) ded.pop_back();
        if (ded.size() < 2) { wholeEntity = true; return true; }
        const float D2R = 3.14159265f / 180.0f;
        int n = (int)ded.size(); int best = 0; float bestD = 1e30f;
        for (int i = 0; i < n; ++i) {
            float a0 = ded[i], a1 = ded[(i + 1) % n];
            float span = std::fmod(a1 - a0 + 360.0f, 360.0f); if (span < 1e-7f) span = 360.0f;
            float am = (a0 + span * 0.5f) * D2R;
            float x = T.center[0] + T.radius * std::cos(am), y = T.center[1] + T.radius * std::sin(am);
            float dd = std::hypot(x - mu, y - mv);
            if (dd < bestD) { bestD = dd; best = i; }
        }
        float a0 = ded[best], a1 = ded[(best + 1) % n];
        if (a1 <= a0) a1 += 360.0f;
        sampleArc(T.center[0], T.center[1], T.radius, a0, a1);
        return true;
    }

    // arc: relative sweep offsets within (0, full), spans in the drawn direction (engine _split_arc)
    float a0 = std::fmod(T.start, 360.0f); if (a0 < 0) a0 += 360.0f;
    float a1 = std::fmod(T.end, 360.0f);   if (a1 < 0) a1 += 360.0f;
    float full = T.ccw ? std::fmod(a1 - a0 + 360.0f, 360.0f) : std::fmod(a0 - a1 + 360.0f, 360.0f);
    if (full < 1e-7f) full = 360.0f;
    std::vector<float> rels;
    for (float a : cuts) {
        float rel = T.ccw ? std::fmod(a - a0 + 360.0f, 360.0f) : std::fmod(a0 - a + 360.0f, 360.0f);
        if (rel > 1e-7f && rel < full - 1e-7f) rels.push_back(rel);
    }
    std::sort(rels.begin(), rels.end());
    std::vector<float> ded;
    for (float r2 : rels) {
        if (!ded.empty() && std::fabs(r2 - ded.back()) <= 1e-6f) continue;
        ded.push_back(r2);
    }
    if (ded.empty()) { wholeEntity = true; return true; }
    std::vector<float> bounds; bounds.push_back(0.0f);
    for (float r2 : ded) bounds.push_back(r2);
    bounds.push_back(full);
    const float D2R = 3.14159265f / 180.0f;
    int best = 0; float bestD = 1e30f;
    for (size_t i = 0; i + 1 < bounds.size(); ++i) {
        float rm = (bounds[i] + bounds[i + 1]) * 0.5f;
        float am = (T.ccw ? a0 + rm : a0 - rm) * D2R;
        float x = T.center[0] + T.radius * std::cos(am), y = T.center[1] + T.radius * std::sin(am);
        float dd = std::hypot(x - mu, y - mv);
        if (dd < bestD) { bestD = dd; best = (int)i; }
    }
    float s0 = T.ccw ? a0 + bounds[best] : a0 - bounds[best];
    float s1 = T.ccw ? a0 + bounds[best + 1] : a0 - bounds[best + 1];
    sampleArc(T.center[0], T.center[1], T.radius, s0, s1);
    return true;
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
    char buf[96];
    // C50f: surface the conflict/redundancy count when over-defined (SolidWorks-style).
    if (status_ == "over" && !conflicting_.empty())
        std::snprintf(buf, sizeof(buf), "%s \xE2\x80\x94 %d conflicting   DOF: %d",
                      s, (int)conflicting_.size(), dof_);
    else if (status_ == "over" && !redundant_.empty())
        std::snprintf(buf, sizeof(buf), "%s \xE2\x80\x94 %d redundant   DOF: %d",
                      s, (int)redundant_.size(), dof_);
    else
        std::snprintf(buf, sizeof(buf), "%s   DOF: %d", s, dof_);
    return std::string(buf);
}

// ---------------------------------------------------------------------------
// C37-D: Smart Dimension state machine + dimension annotation rendering
// ---------------------------------------------------------------------------

namespace {
// Active document-unit display for the sketcher's on-canvas dimensions + Modify box. Set once per
// frame from AppState.ips() at the top of drawOverlay so the free-function formatters below (which
// have no AppState) follow the MMGS<->IPS toggle. Sketch measured values are canonical mm; angles
// stay in degrees in both systems.
bool g_skIps = false;
const char* skLenUnit() { return g_skIps ? "in" : "mm"; }

// Format a length (mm) for a dimension label in the active document length unit (mm / inch),
// trimming trailing zeros. The unit suffix is added by the caller.
std::string fmtLen(double mm) {
    double v = g_skIps ? mm / 25.4 : mm;
    char b[48]; std::snprintf(b, sizeof(b), g_skIps ? "%.4g" : "%.2f", v);
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
        if (baseId(h) == "origin") { u = 0; v = 0; return true; }
        const SketchEntity* e = entById(baseId(h));
        if (!e) return false;
        if (h.find(".mid") != std::string::npos)      { u = (e->p1[0]+e->p2[0])*0.5f; v = (e->p1[1]+e->p2[1])*0.5f; }
        else if (h.find(".p1") != std::string::npos)  { u = e->p1[0]; v = e->p1[1]; }
        else if (h.find(".p2") != std::string::npos)  { u = e->p2[0]; v = e->p2[1]; }
        else if (h.find(".center") != std::string::npos) { u = e->center[0]; v = e->center[1]; }
        else if (e->type == "circle" || e->type == "arc") { u = e->center[0]; v = e->center[1]; }
        else { u = e->p1[0]; v = e->p1[1]; }
        return true;
    };
    if (c.type == "radius" || c.type == "diameter") {
        const SketchEntity* e = c.refs.empty() ? nullptr : entById(baseId(c.refs[0]));
        if (!e) return false;
        if (e->type == "slot") {   // C51: width dim sits off the spine midpoint, past the side
            float mx = (e->p1[0]+e->p2[0])*0.5f, my = (e->p1[1]+e->p2[1])*0.5f;
            float sx = e->p2[0]-e->p1[0], sy = e->p2[1]-e->p1[1]; float SL = std::hypot(sx, sy);
            float nnx = SL>1e-6f? -sy/SL : 0.0f, nny = SL>1e-6f? sx/SL : 1.0f;
            pu = mx + nnx * (e->width * 0.5f + 12.0f); pv = my + nny * (e->width * 0.5f + 12.0f);
            return true;
        }
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
                               ImU32 textCol, bool hovered, float pu, float pv, DimHit* hitOut) const {
    // C50g: helper to record the rendered text box (padded) + a dim-line segment for picking.
    auto recText = [&](ImVec2 pos, ImVec2 size) {
        if (!hitOut) return;
        const float pad = 4.0f * ui::uiScale;
        hitOut->textMin = ImVec2(pos.x - pad, pos.y - pad);
        hitOut->textMax = ImVec2(pos.x + size.x + pad, pos.y + size.y + pad);
    };
    auto recSeg = [&](ImVec2 a, ImVec2 b) {
        if (!hitOut || hitOut->nSeg >= 2) return;
        hitOut->seg[hitOut->nSeg][0] = a; hitOut->seg[hitOut->nSeg][1] = b; hitOut->nSeg++;
    };
    auto baseId = [](const std::string& h) { return h.substr(0, h.find('.')); };
    auto handlePt = [&](const std::string& h, ImVec2& s) -> bool {
        if (baseId(h) == "origin") { s = uvToScreen(0, 0); return true; }
        const SketchEntity* e = entById(baseId(h));
        if (!e) return false;
        if (h.find(".mid") != std::string::npos)      s = uvToScreen((e->p1[0]+e->p2[0])*0.5f, (e->p1[1]+e->p2[1])*0.5f);
        else if (h.find(".p1") != std::string::npos)  s = uvToScreen(e->p1[0], e->p1[1]);
        else if (h.find(".p2") != std::string::npos)  s = uvToScreen(e->p2[0], e->p2[1]);
        else if (h.find(".center") != std::string::npos) s = uvToScreen(e->center[0], e->center[1]);
        else if (e->type == "circle" || e->type == "arc") s = uvToScreen(e->center[0], e->center[1]);
        else s = uvToScreen(e->p1[0], e->p1[1]);
        return true;
    };
    ImVec2 place = uvToScreen(pu, pv);
    std::string lbl = dimLabel(c);
    ImVec2 ts = ImGui::CalcTextSize(lbl.c_str());
    float th = hovered ? 2.2f : 1.4f;

    // C51: a slot WIDTH dim (diameter/radius constraint on a slot) renders as a LINEAR dimension
    // across the slot at its spine midpoint — not a circle leader — with a plain (un-⌀'d) value.
    if ((c.type == "diameter" || c.type == "radius") && !c.refs.empty()) {
        const SketchEntity* se = entById(baseId(c.refs[0]));
        if (se && se->type == "slot") {
            float mx = (se->p1[0] + se->p2[0]) * 0.5f, my = (se->p1[1] + se->p2[1]) * 0.5f;
            float sx = se->p2[0] - se->p1[0], sy = se->p2[1] - se->p1[1];
            float SL = std::hypot(sx, sy);
            float nnx = SL > 1e-6f ? -sy / SL : 0.0f, nny = SL > 1e-6f ? sx / SL : 1.0f;
            float hw = se->width * 0.5f;
            ImVec2 A = uvToScreen(mx + nnx * hw, my + nny * hw);
            ImVec2 B = uvToScreen(mx - nnx * hw, my - nny * hw);
            float dx = B.x - A.x, dy = B.y - A.y; float L = std::hypot(dx, dy);
            float ux = L > 1e-6f ? dx / L : 1, uy = L > 1e-6f ? dy / L : 0;
            float nx = -uy, ny = ux;
            float off = (place.x - A.x) * nx + (place.y - A.y) * ny;
            ImVec2 dA(A.x + nx * off, A.y + ny * off), dB(B.x + nx * off, B.y + ny * off);
            const float gap = 4.0f, ext = 6.0f;
            auto extLine = [&](ImVec2 from, ImVec2 to) {
                float ex = to.x - from.x, ey = to.y - from.y; float el = std::hypot(ex, ey);
                if (el < 1e-3f) return; float gx = ex / el, gy = ey / el;
                dl->AddLine(ImVec2(from.x + gx * gap, from.y + gy * gap),
                            ImVec2(to.x + gx * ext, to.y + gy * ext), lineCol, th);
            };
            extLine(A, dA); extLine(B, dB);
            dl->AddLine(dA, dB, lineCol, th);
            arrowHead(dl, dA, -ux, -uy, 8.0f, lineCol);
            arrowHead(dl, dB, ux, uy, 8.0f, lineCol);
            recSeg(dA, dB);
            double v = c.hasMeasured ? c.measured : (c.hasValue ? c.value : (double)se->width);
            std::string wl = fmtLen(c.type == "radius" ? v : v);
            if (c.driven) wl = "(" + wl + ")";
            ImVec2 wts = ImGui::CalcTextSize(wl.c_str());
            ImVec2 tp(place.x + 4, place.y - wts.y * 0.5f);
            dl->AddText(tp, textCol, wl.c_str());
            recText(tp, wts);
            return;
        }
    }

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
            recSeg(rim2, place);
        } else {
            dl->AddLine(cc, place, lineCol, th);
            recSeg(cc, place);
        }
        dl->AddLine(rim, place, lineCol, th);
        arrowHead(dl, rim, rimDir.x, rimDir.y, 8.0f, lineCol);
        recSeg(rim, place);
        ImVec2 tp(place.x + 4, place.y - ts.y * 0.5f);
        dl->AddText(tp, textCol, lbl.c_str());
        recText(tp, ts);
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
        ImVec2 tp(place.x + 4, place.y - ts.y * 0.5f);
        dl->AddText(tp, textCol, lbl.c_str());
        recText(tp, ts);
        // Approximate the arc's pickable span by its chord (start->end rim points).
        recSeg(ImVec2(vtx.x + std::cos(ang0) * r, vtx.y + std::sin(ang0) * r),
               ImVec2(vtx.x + std::cos(ang1) * r, vtx.y + std::sin(ang1) * r));
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

    // Arc conditions: a "min"/"max" circle ref visually attaches at its near/far RIM point along
    // the measurement direction instead of the center (matches the engine's ∓r adjustment).
    {
        auto condShift = [&](size_t i, ImVec2& P, const ImVec2& other) {
            if (c.cond.size() <= i || c.refs.size() <= i) return;
            const std::string& cd = c.cond[i];
            if (cd != "min" && cd != "max") return;
            const SketchEntity* e = entById(baseId(c.refs[i]));
            if (!e || (e->type != "circle" && e->type != "arc")) return;
            ImVec2 cc = uvToScreen(e->center[0], e->center[1]);
            ImVec2 rimRef = uvToScreen(e->center[0] + e->radius, e->center[1]);
            float rpx = std::hypot(rimRef.x - cc.x, rimRef.y - cc.y);
            float ddx = other.x - P.x, ddy = other.y - P.y;
            float LL = std::hypot(ddx, ddy);
            if (LL < 1e-3f) return;
            float sgn = (cd == "min") ? 1.0f : -1.0f;
            P.x += ddx / LL * rpx * sgn; P.y += ddy / LL * rpx * sgn;
        };
        ImVec2 A0 = A, B0 = B;
        condShift(0, A, B0);
        condShift(1, B, A0);
    }

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
    recSeg(dA, dB);
    // text centered on the dim line, nudged to the placement side
    ImVec2 mid((dA.x + dB.x) * 0.5f, (dA.y + dB.y) * 0.5f);
    ImVec2 tp(mid.x - ts.x * 0.5f + nx * 8, mid.y - ts.y * 0.5f + ny * 8);
    dl->AddText(tp, textCol, lbl.c_str());
    recText(tp, ts);
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
    consider("origin", 0.0f, 0.0f);   // sketch origin: dimension FROM the origin marker
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
    if (ei >= 0) {
        const SketchEntity& e = ents_[ei];
        // C50: SolidWorks line-MIDPOINT target. The midpoint is only offered while the line body
        // is hovered (we're inside this whole-entity branch), and only when the cursor is within a
        // comfortable radius of the midpoint — otherwise the click selects the whole line. The
        // midpoint marker itself is drawn by the hover code when a line is hovered.
        if (e.type == "line") {
            float mu = (e.p1[0] + e.p2[0]) * 0.5f, mv = (e.p1[1] + e.p2[1]) * 0.5f;
            ImVec2 mp = uvToScreen(mu, mv);
            if (std::hypot(mp.x - s.x, mp.y - s.y) < 11.0f) {
                out.handle = e.id + ".mid"; out.isPoint = true; out.entIdx = -1;
                out.entId.clear(); out.u = mu; out.v = mv; return true;
            }
        }
        out.handle.clear(); out.isPoint = false; out.entIdx = ei;
        out.entId = e.id; out.cond.clear();
        // C51: on a slot, tag whether the cursor hit a rounded CAP (→ width dim) vs the body/
        // centerline (→ length dim). Cap = the cursor's projection onto the spine falls beyond
        // the endpoints.
        if (e.type == "slot") {
            ImVec2 a = uvToScreen(e.p1[0], e.p1[1]), b = uvToScreen(e.p2[0], e.p2[1]);
            float vx = b.x - a.x, vy = b.y - a.y;
            float t = (vx * (s.x - a.x) + vy * (s.y - a.y)) / std::fmax(1e-6f, vx * vx + vy * vy);
            out.cond = (t < 0.0f || t > 1.0f) ? "wid" : "len";
        }
        return true;
    }
    return false;
}

// C50: the midpoint of the line currently hovered in Smart Dimension, in (u,v), or false. Used by
// the hover overlay to draw the SolidWorks midpoint marker whenever a line is hovered (so the user
// knows the target is available before moving onto it).
bool SketchMode::hoveredLineMidpoint(const ImVec2& s, float& mu, float& mv) const {
    int ei = hitTestEntity(s, 12.0f);
    if (ei < 0 || ents_[ei].type != "line") return false;
    const SketchEntity& e = ents_[ei];
    mu = (e.p1[0] + e.p2[0]) * 0.5f; mv = (e.p1[1] + e.p2[1]) * 0.5f;
    return true;
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
    // Circle/arc entity picks act as their CENTER in distance combos, with an optional per-pick
    // arc condition: "min" (near rim / tangent) or "max" (far rim) adds -r/+r to the measured
    // and enforced distance (engine `cond` field). Default is the center (SolidWorks default).
    auto isCirc = [&](const DimPick& p) {
        if (p.isPoint) return false;
        const SketchEntity* e = entOf(p);
        return e && (e->type == "circle" || e->type == "arc");
    };
    auto condOf = [&](const DimPick& p) -> std::string {
        return p.cond.empty() ? std::string("center") : p.cond;
    };
    auto condAdj = [&](const DimPick& p) -> double {
        if (!isCirc(p)) return 0.0;
        std::string c = condOf(p);
        if (c == "min") return -(double)entOf(p)->radius;
        if (c == "max") return  (double)entOf(p)->radius;
        return 0.0;
    };
    auto refOf = [&](const DimPick& p) -> std::string {
        return p.isPoint ? p.handle : p.entId;
    };
    // Attach the cond array when any ref is a circle/arc with a non-center condition.
    auto attachCond = [&](const DimPick& a, const DimPick& b) {
        if ((isCirc(a) && condOf(a) != "center") || (isCirc(b) && condOf(b) != "center"))
            dimPending_["cond"] = nlohmann::json::array(
                {isCirc(a) ? condOf(a) : "center", isCirc(b) ? condOf(b) : "center"});
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
            } else if (e.type == "slot") {
                // C51: a whole-slot pick dimensions either its centerline LENGTH (center-to-center,
                // the default) or its WIDTH (when the cap end was picked — cond "wid", set by
                // dimHitTest). Width is a diameter constraint on the slot's width variable.
                if (a.cond == "wid") {
                    dimType_ = "diameter"; dimMeasured_ = e.width;
                    dimPending_ = {{"type", "diameter"}, {"refs", nlohmann::json::array({e.id})}};
                } else {
                    dimType_ = "distance";
                    dimMeasured_ = std::hypot(e.p2[0] - e.p1[0], e.p2[1] - e.p1[1]);
                    dimPending_ = {{"type", "distance"},
                        {"refs", nlohmann::json::array({e.id + ".p1", e.id + ".p2"})}};
                }
                dimHasPending_ = true; return true;
            }
        }
        return false;   // a single point alone is not a dimension yet
    }
    if (n == 2) {
        const DimPick& a = dimPicks_[0]; const DimPick& b = dimPicks_[1];
        bool aLine = !a.isPoint && entOf(a)->type == "line";
        bool bLine = !b.isPoint && entOf(b)->type == "line";
        // A pick is "point-like" when it's a point handle OR a circle/arc (anchored at center).
        bool aPt = a.isPoint || isCirc(a);
        bool bPt = b.isPoint || isCirc(b);
        // Same-entity guard (e.g. a circle's center handle + the same circle entity): no combo.
        auto base = [](const std::string& h) { return h.substr(0, h.find('.')); };
        if (base(refOf(a)) == base(refOf(b))) return false;
        if (aPt && bPt) {        // point/circle <-> point/circle distance (variant chosen at place)
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
            m += condAdj(a) + condAdj(b);          // min/max arc conditions shift by the radii
            dimType_ = "distance"; dimMeasured_ = m;
            dimPending_ = {{"type", "distance"},
                           {"refs", nlohmann::json::array({refOf(a), refOf(b)})}};
            attachCond(a, b);
            if (!dimAxis_.empty()) dimPending_["axis"] = dimAxis_;
            dimHasPending_ = true; return true;
        }
        if ((aPt && bLine) || (aLine && bPt)) {   // point/circle <-> line perpendicular distance
            const DimPick& pp = aPt ? a : b;
            const DimPick& ll = aPt ? b : a;
            const SketchEntity& le = *entOf(ll);
            float pu, pv; handleUV(pp, pu, pv);
            // perpendicular foot distance (center-based), plus the arc condition
            float dx = le.p2[0] - le.p1[0], dy = le.p2[1] - le.p1[1]; float L = std::hypot(dx, dy);
            float nx = L > 1e-6f ? -dy / L : 0, ny = L > 1e-6f ? dx / L : 1;
            double m = std::fabs((pu - le.p1[0]) * nx + (pv - le.p1[1]) * ny) + condAdj(pp);
            dimType_ = "distance"; dimMeasured_ = m;
            dimPending_ = {{"type", "distance"},
                           {"refs", nlohmann::json::array({refOf(pp), le.id})}};
            attachCond(pp, ll);
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

// C50f: apply a sketch_set_dim op (the shared EDIT-commit body), reload the mesh, re-solve, and
// tear down the Modify box. Used by the dimCommit edit path and by "Apply anyway" on the edit
// over-define modal.
void SketchMode::applyDimEditOp(AppState& st, const nlohmann::json& op) {
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    if (!r.ok) { st.toast(ui::ToastLevel::Error, "Dimension: " + r.error_message); dimError_ = r.error_message; return; }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);
    // C38 (S3): full pick-buffer cleanup. A double-click edit leaves stale picks otherwise, which
    // dimBuildPending consumes on the next frame.
    dimPopupOpen_ = false; dimEditCid_.clear();
    dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr;
    dimAxis_.clear(); dimPopupDriven_ = false;
}

// Commit the Modify box: parse value/expr, apply, and (on success) keep the tool armed.
void SketchMode::dimCommit(AppState& st) {
    std::string txt = dimBuf_.data();
    nlohmann::json con = dimPending_;
    bool isExpr = (!txt.empty() && txt[0] == '=');
    double mm = dimMeasured_;
    if (!isExpr) {
        std::string perr;
        // A bare value in the Modify box follows the document length unit (mm / inch); angles
        // stay degrees. An explicit suffix the user types still wins via pmParseDim.
        const double bareScale = (g_skIps && !dimIsAngle_) ? 25.4 : 1.0;
        if (!pmParseDim(txt, mm, perr, dimIsAngle_, bareScale)) { dimError_ = perr.empty() ? "bad value" : perr; return; }
    }
    if (!dimEditCid_.empty()) {
        // Editing an EXISTING dim: sketch_set_dim. C50f: pre-flight the new value with sketch_probe
        // so a value that contradicts other relations is caught BEFORE it over-defines the sketch
        // (the user's main bug — the edit path had zero over-define handling). Driven dims can't
        // over-define (they're references), so they skip the probe.
        nlohmann::json op = {{"op", "sketch_set_dim"}, {"sketch", fid_}, {"id", dimEditCid_}};
        if (isExpr) op["expr"] = txt.substr(1); else op["value"] = mm;
        bool editDriven = false;
        for (const auto& c : cons_) if (c.id == dimEditCid_) { editDriven = c.driven; break; }
        if (!editDriven) {
            nlohmann::json setAction = {{"id", dimEditCid_}};
            if (isExpr) { setAction["expr"] = txt.substr(1); setAction["value"] = mm; }
            else        setAction["value"] = mm;
            bool wouldOver = false; std::vector<std::string> pc, pr;
            if (sketchProbe(st, nlohmann::json{{"set_dim", setAction}}, wouldOver, pc, pr) && wouldOver) {
                // Stash the op; the modal's "Apply anyway" applies it, "Cancel" keeps the old value.
                editOverDefOp_ = op;
                editOverDefOpen_ = true;
                editOverDefPos_ = dimPopupPos_;
                dimPopupOpen_ = false;   // hide the Modify box behind the modal
                return;
            }
            // probe failure (RPC/transport) falls through and commits normally — never block.
        }
        applyDimEditOp(st, op);
        return;
    }
    // New dim: attach value/expr + placement, then (after the over-define pre-flight) driven.
    con["value"] = isExpr ? nlohmann::json{{"value", dimMeasured_}, {"expr", txt.substr(1)}}
                          : nlohmann::json{{"value", mm}, {"expr", nullptr}};
    con["placement"] = nlohmann::json::array({dimPlaceU_, dimPlaceV_});
    // C50f: SolidWorks over-define pre-flight. Probe the add BEFORE committing it. If the user
    // already ticked "Driven" in the Modify box, skip the probe/dialog and just commit driven
    // (a reference dim can't over-define). Otherwise probe; if would_over_define, open the
    // three-choice dialog. A probe failure falls through to a normal driving commit (never block).
    if (!dimPopupDriven_) {
        bool wouldOver = false; std::vector<std::string> pc, pr;
        if (sketchProbe(st, nlohmann::json{{"add_constraint", con}}, wouldOver, pc, pr) && wouldOver) {
            overDefPendingCon_ = con;      // commit happens on the dialog button (driving|driven)
            overDefOpen_ = true;
            overDefPos_ = dimPopupPos_;
            dimPopupOpen_ = false;         // hide the Modify box behind the dialog
            return;
        }
    }
    if (dimPopupDriven_) con["driven"] = true;
    // probe said fine (or the Driven box was ticked) -> commit con as-is. con already carries
    // "driven" when ticked, so pass driven=false (overDefCommit only ADDS driven, never removes it).
    overDefPendingCon_ = con;
    overDefCommit(st, /*driven=*/false);
}

// C50f: commit the pending new-dimension constraint (overDefPendingCon_ — already carries
// value/expr/placement). driven=true converts it to a driven (reference) dim before adding. Shared
// by dimCommit's "fine" path and the over-define dialog's Make Driven / Keep Driving buttons. After
// applying it re-solves and tears down the Smart Dimension pick buffer (tool stays armed).
void SketchMode::overDefCommit(AppState& st, bool driven) {
    nlohmann::json con = overDefPendingCon_;
    if (driven) con["driven"] = true;
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
    resolve(st);   // refreshes conflicting_ + redundant_ + status_ + dof_ + solveRejected_
    // Stay in Smart Dimension; clear the pick buffer for the next dimension.
    overDefPendingCon_ = nullptr;
    dimPopupOpen_ = false; dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr;
    dimAxis_.clear(); dimPopupDriven_ = false;
}

// C50f: the SolidWorks over-define pre-flight dialog for a NEW dimension. sketch_probe flagged the
// pending add (overDefPendingCon_) as over-defining BEFORE anything was committed. Three choices,
// matching SolidWorks' "Make Dimension Driven?" prompt: Make Driven (default/highlighted) commits
// the dim as a reference dim; Leave Driving commits it as a driving dim (sketch goes over-defined);
// Cancel commits nothing. The chosen button drives overDefCommit; nothing has touched the doc yet.
void SketchMode::drawOverDefinePrompt(AppState& st) {
    if (!overDefOpen_) return;
    const auto& pal = ui::palette;
    ImGui::SetNextWindowPos(overDefPos_, ImGuiCond_Appearing);
    ImGui::SetNextWindowBgAlpha(0.98f);
    if (ImGui::Begin("##OverDefine", nullptr,
                     ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                     ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1));
        ImGui::TextUnformatted("Make Dimension Driven?");
        ImGui::PopStyleColor();
        ImGui::TextUnformatted("Adding this dimension makes the sketch over defined.");
        ImGui::Spacing();
        auto finish = [&]() { overDefOpen_ = false; overDefPendingCon_ = nullptr;
                              dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr;
                              dimAxis_.clear(); dimPopupDriven_ = false; };
        // Default action (Make this dimension driven) — commits a reference dimension. Highlight it
        // in the accent color so it reads as the recommended choice.
        ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.85f));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.70f));
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.windowBg[0], pal.windowBg[1], pal.windowBg[2], 1));
        bool mkDriven = ImGui::Button("Make this dimension driven");
        ImGui::PopStyleColor(4);
        if (mkDriven) { overDefCommit(st, /*driven=*/true); overDefOpen_ = false; }
        ImGui::SameLine();
        if (ImGui::Button("Leave this dimension driving")) {
            // Commit as a driving dim; the sketch becomes over defined (reads red, frozen geometry).
            overDefCommit(st, /*driven=*/false);
            overDefOpen_ = false;
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel")) {
            // Commit nothing — the dimension input is discarded entirely.
            finish();
        }
    }
    ImGui::End();
}

// C50f: over-define pre-flight dialog for a dimension EDIT. sketch_probe found the new value
// contradicts other relations. Cancel (default) keeps the old value and sends nothing; Apply anyway
// pushes the stashed set_dim op (the sketch goes over-defined with frozen geometry).
void SketchMode::drawEditOverDefinePrompt(AppState& st) {
    if (!editOverDefOpen_) return;
    const auto& pal = ui::palette;
    ImGui::SetNextWindowPos(editOverDefPos_, ImGuiCond_Appearing);
    ImGui::SetNextWindowBgAlpha(0.98f);
    if (ImGui::Begin("##EditOverDefine", nullptr,
                     ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                     ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1));
        ImGui::TextUnformatted("Over Defined");
        ImGui::PopStyleColor();
        ImGui::TextUnformatted("This value over-defines the sketch \xE2\x80\x94 the geometry");
        ImGui::TextUnformatted("cannot satisfy all relations.");
        ImGui::Spacing();
        auto finish = [&]() { editOverDefOpen_ = false; editOverDefOp_ = nullptr;
                              dimEditCid_.clear();
                              dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr;
                              dimAxis_.clear(); dimPopupDriven_ = false; };
        // Default action (Cancel) — keep the old value, send nothing. Highlighted as recommended.
        ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.85f));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.70f));
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.windowBg[0], pal.windowBg[1], pal.windowBg[2], 1));
        bool cancel = ImGui::Button("Cancel");
        ImGui::PopStyleColor(4);
        if (cancel) finish();
        ImGui::SameLine();
        if (ImGui::Button("Apply anyway")) {
            nlohmann::json op = editOverDefOp_;
            editOverDefOpen_ = false; editOverDefOp_ = nullptr;
            applyDimEditOp(st, op);   // sends it; sketch goes over-defined with frozen geometry
        }
    }
    ImGui::End();
}

void SketchMode::drawRelationOverDefinePrompt(AppState& st) {
    if (!relOverDefOpen_) return;
    const auto& pal = ui::palette;
    ImGui::SetNextWindowPos(relOverDefPos_, ImGuiCond_Appearing);
    ImGui::SetNextWindowBgAlpha(0.98f);
    if (ImGui::Begin("##RelationOverDefine", nullptr,
                     ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                     ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1));
        ImGui::TextUnformatted("Over Defined");
        ImGui::PopStyleColor();
        ImGui::TextUnformatted("Adding this relation makes the sketch over defined.");
        ImGui::Spacing();
        auto finish = [&]() {
            relOverDefOpen_ = false;
            relOverDefOps_ = nullptr;
            relOverDefLabel_.clear();
        };
        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
            ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.85f));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive,
            ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.70f));
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.windowBg[0], pal.windowBg[1], pal.windowBg[2], 1));
        bool cancel = ImGui::Button("Cancel");
        ImGui::PopStyleColor(4);
        if (cancel) finish();
        ImGui::SameLine();
        if (ImGui::Button("Add anyway")) {
            nlohmann::json ops = relOverDefOps_;
            std::string label = relOverDefLabel_.empty() ? "Relation" : relOverDefLabel_;
            finish();
            applyRelationOps(st, ops, label);
        }
    }
    ImGui::End();
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
    afterSketchHistory(st);
}

void SketchMode::inSketchRedo(AppState& st) {
    st.docRedo();
    afterSketchHistory(st);
}

// Shared post-undo/redo refresh: resync the mirror, re-solve the active sketch, or close cleanly
// if the sketch feature itself was undone/redone away.
void SketchMode::afterSketchHistory(AppState& st) {
    resyncMirror(st);
    // Does the sketch still exist? Re-solving an absent sketch returns an error envelope.
    nlohmann::json res;
    if (!callSketch(st, "sketch_solve", nlohmann::json::object(), res)) {
        st.toast(ui::ToastLevel::Info, "Sketch removed by undo — closing");
        // The sketch feature is gone; drop out cleanly without re-exiting the worker sketch.
        active_ = false; isNewEmpty_ = false; fid_.clear();
        ents_.clear(); cons_.clear(); autoPlace_.clear(); projRefs_.clear(); cancelCheckpoint_.clear();
        status_.clear(); dof_ = 0;
        conflicting_.clear(); redundant_.clear(); solveRejected_ = false;   // C50f
        relOverDefOpen_ = false; relOverDefOps_ = nullptr; relOverDefLabel_.clear();   // C50g
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
            if (e.type == "line") {
                poly([&](float t, float& u, float& v) {
                    u = e.p1[0] + (e.p2[0] - e.p1[0]) * t;
                    v = e.p1[1] + (e.p2[1] - e.p1[1]) * t;
                }, 1);
            } else if (e.type == "slot") {
                // Closed capsule: two side lines (tangent, offset by half-width) + two end-cap
                // half-circles — the same profile the engine extrudes.
                float dx = e.p2[0] - e.p1[0], dy = e.p2[1] - e.p1[1];
                float L = std::hypot(dx, dy);
                float hw = e.width * 0.5f;
                float nx = L > 1e-6f ? -dy / L : 0.0f, ny = L > 1e-6f ? dx / L : 0.0f;
                for (float s : {1.0f, -1.0f})
                    poly([&](float t, float& u, float& v) {
                        u = (e.p1[0] + nx * hw * s) + (e.p2[0] - e.p1[0]) * t;
                        v = (e.p1[1] + ny * hw * s) + (e.p2[1] - e.p1[1]) * t;
                    }, 1);
                if (hw > 0.0f) {
                    float ax1 = std::atan2(-dy, -dx), ax2 = std::atan2(dy, dx);
                    for (auto pr : {std::make_pair(e.p1, ax1), std::make_pair(e.p2, ax2)}) {
                        const float* c = pr.first; float axis = pr.second;
                        poly([&](float t, float& u, float& v) {
                            float a = axis - 1.5707963f + 3.14159265f * t;
                            u = c[0] + hw * std::cos(a);
                            v = c[1] + hw * std::sin(a);
                        }, 24);
                    }
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
            } else if ((e.type == "spline" || e.type == "style_spline") && e.pts.size() >= 2) {
                // C42: polyline through the solved through-points (style_spline: control polygon).
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
// C51: feature-PM region picker — hover-highlight + click-to-toggle profile regions
// ---------------------------------------------------------------------------

void SketchMode::refreshRegionPicker(AppState& st) {
    const std::string docId = st.docMirror.docId();
    const int rev = st.docMirror.revision();
    auto* pm = st.propertyManager;
    std::vector<std::string> cands = pm ? pm->regionCandidateSketches(st)
                                        : std::vector<std::string>();
    std::string key;
    for (const auto& c : cands) { key += c; key += ','; }
    if (docId == pickDocId_ && rev == pickRevision_ && key == pickCandidatesKey_) return;
    pickDocId_ = docId; pickRevision_ = rev; pickCandidatesKey_ = key;
    ++pickGen_;
    // Carry stale snapshots across the refresh (avoid flicker during PM preview revision churn).
    std::vector<RegionSketch> old; old.swap(pickRegions_);
    for (const auto& fid : cands) {
        pickRegions_.push_back(RegionSketch{});
        pickRegions_.back().fid = fid;
        for (auto& o : old) {
            if (o.fid != fid || !o.loaded) continue;
            std::memcpy(pickRegions_.back().frame, o.frame, sizeof(o.frame));
            pickRegions_.back().regions = std::move(o.regions);
            pickRegions_.back().loaded = true;
            break;
        }
        const int gen = pickGen_;
        st.rpc.call("doc_sketch_regions", {{"doc_id", docId}, {"sketch", fid}},
            [this, gen, fid](const core::RpcResult& r) {
                if (gen != pickGen_) return;
                if (!r.ok || !r.result.is_object() || r.result.contains("code")) return;
                for (auto& rs : pickRegions_) {
                    if (rs.fid != fid) continue;
                    if (r.result.contains("plane_frame") && r.result["plane_frame"].is_array() &&
                        r.result["plane_frame"].size() == 16)
                        for (int i = 0; i < 16; ++i)
                            rs.frame[i] = r.result["plane_frame"][i].get<float>();
                    rs.regions.clear();
                    if (r.result.contains("regions") && r.result["regions"].is_array()) {
                        for (const auto& jr : r.result["regions"]) {
                            PickRegion pr;
                            pr.index = jr.value("index", 0);
                            pr.isMaterial = jr.value("is_material", true);
                            if (jr.contains("centroid") && jr["centroid"].is_array() &&
                                jr["centroid"].size() == 2) {
                                pr.centroid[0] = jr["centroid"][0].get<float>();
                                pr.centroid[1] = jr["centroid"][1].get<float>();
                            }
                            auto flat2 = [](const nlohmann::json& arr, std::vector<float>& out) {
                                if (!arr.is_array()) return;
                                for (const auto& p : arr)
                                    if (p.is_array() && p.size() == 2) {
                                        out.push_back(p[0].get<float>());
                                        out.push_back(p[1].get<float>());
                                    }
                            };
                            if (jr.contains("outer")) flat2(jr["outer"], pr.outer);
                            if (jr.contains("holes") && jr["holes"].is_array())
                                for (const auto& h : jr["holes"]) {
                                    pr.holes.emplace_back();
                                    flat2(h, pr.holes.back());
                                }
                            if (jr.contains("tris") && jr["tris"].is_array())
                                for (const auto& t : jr["tris"])
                                    if (t.is_array() && t.size() == 3)
                                        for (const auto& p : t)
                                            if (p.is_array() && p.size() == 2) {
                                                pr.tris.push_back(p[0].get<float>());
                                                pr.tris.push_back(p[1].get<float>());
                                            }
                            rs.regions.push_back(std::move(pr));
                        }
                    }
                    rs.loaded = true;
                }
            });
    }
}

// Screen-space point-in-polygon over a flat u,v list already projected to screen pts.
static bool pmPtInScreenPoly(const std::vector<ImVec2>& poly, const ImVec2& p) {
    bool in = false;
    size_t n = poly.size();
    if (n < 3) return false;
    for (size_t i = 0, j = n - 1; i < n; j = i++) {
        const ImVec2& a = poly[i];
        const ImVec2& b = poly[j];
        if (((a.y > p.y) != (b.y > p.y)) &&
            (p.x < (b.x - a.x) * (p.y - a.y) / (b.y - a.y + 1e-9f) + a.x))
            in = !in;
    }
    return in;
}

bool SketchMode::drawRegionPicker(AppState& st, ImGuiIO& io, const ImVec2& imgMin,
                                  const ImVec2& imgMax, int vw, int vh, bool imgHovered) {
    if (active_) return false;                 // editing a sketch: the overlay owns the canvas
    auto* pm = st.propertyManager;
    if (!st.renderer || !st.workerStarted || !st.docMirror.hasDoc() ||
        !pm || !pm->active() || !pm->wantsRegionPicker()) {
        if (!pickRegions_.empty()) { pickRegions_.clear(); pickCandidatesKey_.clear(); }
        return false;
    }
    refreshRegionPicker(st);
    if (pickRegions_.empty()) return false;

    const auto& cam = st.renderer->camera();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->PushClipRect(imgMin, imgMax, true);
    const auto& pal = ui::palette;
    const bool ordered = pm->sectionsAreOrdered();

    // Parse the current profile/sections selection.
    std::vector<nlohmann::json> picks = pm->profilePicks();
    auto isWhole = [&](const std::string& fid) -> bool {
        for (const auto& s : picks)
            if (s.value("kind", std::string()) == "region" &&
                s.value("sketch", std::string()) == fid && !s.contains("index"))
                return true;
        return false;
    };
    auto explicitHas = [&](const std::string& fid, int idx) -> bool {
        for (const auto& s : picks)
            if (s.value("kind", std::string()) == "region" &&
                s.value("sketch", std::string()) == fid && s.contains("index") &&
                s["index"].is_number() && (int)s["index"].get<double>() == idx)
                return true;
        return false;
    };
    auto isSelected = [&](const std::string& fid, const PickRegion& pr) -> bool {
        if (explicitHas(fid, pr.index)) return true;
        return isWhole(fid) && pr.isMaterial;   // whole sketch = all material regions
    };

    // Project a region's outer + hole polylines for the given sketch frame.
    auto projectPoly = [&](const float frame[16], const std::vector<float>& uv,
                           std::vector<ImVec2>& out) {
        out.clear();
        for (size_t i = 0; i + 1 < uv.size(); i += 2) {
            float u = uv[i], v = uv[i + 1];
            float wx = frame[0] * u + frame[1] * v + frame[3];
            float wy = frame[4] * u + frame[5] * v + frame[7];
            float wz = frame[8] * u + frame[9] * v + frame[11];
            ImVec2 p;
            if (worldToScreen(cam, imgMin, vw, vh, wx, wy, wz, p)) out.push_back(p);
        }
    };

    // ---- hover detection (screen space, even-odd hole aware) ----
    int hovS = -1, hovR = -1;
    float hovArea = 1e30f;
    const ImVec2 mouse = io.MousePos;
    const bool canHover = imgHovered && mouse.x >= imgMin.x && mouse.x <= imgMax.x &&
                          mouse.y >= imgMin.y && mouse.y <= imgMax.y;
    std::vector<ImVec2> outerS, holeS;
    if (canHover) {
        for (size_t si = 0; si < pickRegions_.size(); ++si) {
            const auto& rs = pickRegions_[si];
            if (!rs.loaded) continue;
            for (size_t ri = 0; ri < rs.regions.size(); ++ri) {
                const auto& pr = rs.regions[ri];
                projectPoly(rs.frame, pr.outer, outerS);
                if (!pmPtInScreenPoly(outerS, mouse)) continue;
                bool inHole = false;
                for (const auto& h : pr.holes) {
                    projectPoly(rs.frame, h, holeS);
                    if (pmPtInScreenPoly(holeS, mouse)) { inHole = true; break; }
                }
                if (inHole) continue;
                // Screen-area tie-break: prefer the smallest region under the cursor.
                float a = 0.0f;
                for (size_t k = 0, j = outerS.size() - 1; k < outerS.size(); j = k++)
                    a += (outerS[j].x + outerS[k].x) * (outerS[j].y - outerS[k].y);
                a = std::fabs(a) * 0.5f;
                if (a < hovArea) { hovArea = a; hovS = (int)si; hovR = (int)ri; }
            }
        }
    }

    // ---- draw: fills (selected/hover) + outlines ----
    std::vector<ImVec2> tri(3);
    for (size_t si = 0; si < pickRegions_.size(); ++si) {
        const auto& rs = pickRegions_[si];
        if (!rs.loaded) continue;
        for (size_t ri = 0; ri < rs.regions.size(); ++ri) {
            const auto& pr = rs.regions[ri];
            const bool sel = isSelected(rs.fid, pr);
            const bool hov = ((int)si == hovS && (int)ri == hovR);
            // Hole-aware fill from engine triangles when selected or hovered.
            if ((sel || hov) && pr.tris.size() >= 6) {
                ImU32 fillc = sel ? col(pal.accent, hov ? 0.42f : 0.28f)
                                  : col(pal.hoverHighlight, 0.5f);
                for (size_t t = 0; t + 5 < pr.tris.size(); t += 6) {
                    bool ok = true;
                    for (int k = 0; k < 3; ++k) {
                        float u = pr.tris[t + k * 2], v = pr.tris[t + k * 2 + 1];
                        float wx = rs.frame[0] * u + rs.frame[1] * v + rs.frame[3];
                        float wy = rs.frame[4] * u + rs.frame[5] * v + rs.frame[7];
                        float wz = rs.frame[8] * u + rs.frame[9] * v + rs.frame[11];
                        if (!worldToScreen(cam, imgMin, vw, vh, wx, wy, wz, tri[k])) { ok = false; break; }
                    }
                    if (ok) dl->AddTriangleFilled(tri[0], tri[1], tri[2], fillc);
                }
            }
            // Outline: outer + holes. Selected = accent bold, else faint dim line.
            ImU32 lc = sel ? col(pal.accent, 0.95f)
                           : (hov ? col(pal.accent, 0.6f) : col(pal.sketchDimLine, 0.5f));
            float th = sel ? 2.2f : (hov ? 1.8f : 1.2f);
            projectPoly(rs.frame, pr.outer, outerS);
            if (outerS.size() >= 2) dl->AddPolyline(outerS.data(), (int)outerS.size(), lc, ImDrawFlags_Closed, th);
            for (const auto& h : pr.holes) {
                projectPoly(rs.frame, h, holeS);
                if (holeS.size() >= 2) dl->AddPolyline(holeS.data(), (int)holeS.size(), lc, ImDrawFlags_Closed, th);
            }
        }
    }
    dl->PopClipRect();

    // ---- click: toggle the hovered region into/out of the profile ----
    if (hovS >= 0 && canHover && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
        st.requestCursor(ui::CursorKind::Hand);
        const std::string fid = pickRegions_[hovS].fid;
        const int idx = pickRegions_[hovS].regions[hovR].index;

        std::vector<nlohmann::json> out;
        if (ordered) {
            // Loft sections: clicks APPEND in order; re-clicking removes.
            bool removed = false;
            for (const auto& s : picks) {
                if (s.value("kind", std::string()) == "region" &&
                    s.value("sketch", std::string()) == fid && s.contains("index") &&
                    s["index"].is_number() && (int)s["index"].get<double>() == idx) {
                    removed = true; continue;  // drop it
                }
                out.push_back(s);
            }
            if (!removed)
                out.push_back({{"kind", "region"}, {"sketch", fid}, {"index", idx}});
        } else {
            // Profile: toggle within an effective per-sketch set (whole = all material).
            // Build effective sets for every candidate sketch, toggle the clicked one, re-emit.
            for (const auto& rs : pickRegions_) {
                // effective selected indices for this sketch
                std::vector<int> eff;
                std::vector<int> material;
                for (const auto& pr : rs.regions) {
                    if (pr.isMaterial) material.push_back(pr.index);
                    bool on = explicitHas(rs.fid, pr.index) || (isWhole(rs.fid) && pr.isMaterial);
                    if (on) eff.push_back(pr.index);
                }
                if (rs.fid == fid) {
                    auto it = std::find(eff.begin(), eff.end(), idx);
                    if (it != eff.end()) eff.erase(it); else eff.push_back(idx);
                }
                std::sort(eff.begin(), eff.end());
                // Compact to a whole-sketch pick when eff == the material set (non-empty).
                std::vector<int> mat = material; std::sort(mat.begin(), mat.end());
                if (!eff.empty() && eff == mat) {
                    out.push_back({{"kind", "region"}, {"sketch", rs.fid}});
                } else {
                    for (int i : eff)
                        out.push_back({{"kind", "region"}, {"sketch", rs.fid}, {"index", i}});
                }
            }
            // Never leave a profile empty (would dangle) — fall back to whole clicked sketch.
            if (out.empty())
                out.push_back({{"kind", "region"}, {"sketch", fid}});
        }
        pm->setProfilePicks(st, out);
    } else if (hovS >= 0 && canHover) {
        st.requestCursor(ui::CursorKind::Hand);
    }
    return hovS >= 0;   // consumed the click target this frame
}

// ---------------------------------------------------------------------------
// overlay draw + interaction
// ---------------------------------------------------------------------------

void SketchMode::drawOverlay(AppState& st, ImGuiIO& io, const ImVec2& imgMin, const ImVec2& imgMax,
                             int vw, int vh, bool imgHovered) {
    if (!st.renderer) return;
    // Sketch dimensions + Modify box follow the document unit system (mm / inch); refreshed each
    // frame so a status-bar units toggle reformats on-canvas dims and the Modify box live.
    g_skIps = st.ips();
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

        // Project each origin-plane quad (front=0/top=1/right=2). The corners come straight from
        // Renderer::planeQuad — the SAME geometry drawReferencePlanes renders — so the hit-test
        // polygon and the visible plane always coincide (the old mirrored copy used cam.center,
        // which includes the pan offset, and drifted away from the drawn planes after any pan).
        const char* planeSel[3] = {"plane.front", "plane.top", "plane.right"};
        int hovered = -1;
        float hoveredDepth = 1e30f;   // C52: pick the FRONTMOST plane under the cursor (SW behavior)
        ImVec2 quadS[3][4];
        bool quadVis[3] = {false, false, false};
        Vec3 quadW[3][4];
        for (int p = 0; p < 3; ++p) {
            Vec3 c4[4];
            if (!r.planeQuad(p, c4)) continue;
            for (int i = 0; i < 4; ++i) quadW[p][i] = c4[i];
            bool ok = true;
            for (int i = 0; i < 4; ++i) {
                if (!worldToScreen(cam, imgMin, vw, vh, c4[i].x, c4[i].y, c4[i].z, quadS[p][i])) { ok = false; break; }
            }
            quadVis[p] = ok;
            if (!ok || !imgHovered) continue;
            // C52 plane-pick fix: the three origin planes all pass through the origin and overlap
            // heavily on screen, so a simple point-in-quad test that keeps the LAST matching index
            // always resolved the overlap region to Right (YZ, index 2) — the user's "wrong plane"
            // bug. Instead, point-in-triangle with BARYCENTRIC weights so we can interpolate the
            // world-space hit point and compare DEPTHS, choosing the plane whose surface is closest
            // to the camera at this pixel (frontmost) — exactly how SolidWorks highlights the plane
            // under the cursor.
            auto hitDepth = [&](ImVec2 P, int a, int b, int cc, float& depthOut) -> bool {
                ImVec2 sa = quadS[p][a], sb = quadS[p][b], sc = quadS[p][cc];
                auto edge = [](ImVec2 pp, ImVec2 qq, ImVec2 rr) {
                    return (pp.x - rr.x) * (qq.y - rr.y) - (qq.x - rr.x) * (pp.y - rr.y);
                };
                float area = edge(sa, sb, sc);
                if (area == 0.0f) return false;
                float w0 = edge(P, sb, sc) / area;   // weight of vertex a
                float w1 = edge(P, sc, sa) / area;   // weight of vertex b
                float w2 = edge(P, sa, sb) / area;   // weight of vertex c
                // Inside-test is winding-agnostic: dividing by the signed area normalises the sign,
                // so an interior point has all three weights >= 0 for both CW and CCW screen winding.
                if (w0 < 0.0f || w1 < 0.0f || w2 < 0.0f) return false;
                Vec3 Wa = quadW[p][a], Wb = quadW[p][b], Wc = quadW[p][cc];
                Vec3 wp(w0 * Wa.x + w1 * Wb.x + w2 * Wc.x,
                        w0 * Wa.y + w1 * Wb.y + w2 * Wc.y,
                        w0 * Wa.z + w1 * Wb.z + w2 * Wc.z);
                float dx = wp.x - cam.eye.x, dy = wp.y - cam.eye.y, dz = wp.z - cam.eye.z;
                depthOut = dx * dx + dy * dy + dz * dz;
                return true;
            };
            float d;
            if ((hitDepth(io.MousePos, 0, 1, 2, d) || hitDepth(io.MousePos, 0, 2, 3, d)) &&
                d < hoveredDepth) {
                hoveredDepth = d;
                hovered = p;
            }
        }
        planeHover_ = hovered;
        // The renderer already draws all three datum planes (fill + outline, with emphasis
        // brightening the hovered one — viewport_panel passes planeHover() as the emphasis).
        // Painting a second translucent fill per plane on top made the overlap muddy and
        // unreadable (user bug report), so the overlay adds ONLY a hover tint on the one
        // plane under the cursor.
        if (hovered >= 0 && quadVis[hovered]) {
            wdl->AddQuadFilled(quadS[hovered][0], quadS[hovered][1], quadS[hovered][2],
                               quadS[hovered][3], col(pal.accent, 0.16f));
            wdl->AddQuad(quadS[hovered][0], quadS[hovered][1], quadS[hovered][2],
                         quadS[hovered][3], col(pal.accent, 0.90f), 2.0f);
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

    // C37-D / C50: in-sketch undo + redo (the global app handler skips both while a sketch is
    // active). Ctrl+Z undoes (mid-stroke pops the last point); Ctrl+Y or Ctrl+Shift+Z redoes.
    // Suppressed while a text field (the Modify box) owns the keyboard. Returns early if the
    // sketch vanished from the undo/redo.
    if (!io.WantTextInput && io.KeyCtrl) {
        if (io.KeyShift && ImGui::IsKeyPressed(ImGuiKey_Z, false)) {
            inSketchRedo(st);
            if (!active_) { dl->PopClipRect(); return; }
        } else if (ImGui::IsKeyPressed(ImGuiKey_Z, false)) {
            inSketchUndo(st);
            if (!active_) { dl->PopClipRect(); return; }
        } else if (ImGui::IsKeyPressed(ImGuiKey_Y, false)) {
            inSketchRedo(st);
            if (!active_) { dl->PopClipRect(); return; }
        }
    }

    // ---- Wave 4C/4D: sketch entity copy / paste keyboard routing ----
    // Ctrl+C copies the current selection; Ctrl+V arms paste-placement from a SketchEntities
    // clipboard. Suppressed while a text field owns input, while placement is already in progress
    // (Ctrl+V then is a no-op), and (Ctrl+C) only when there's a selection. The Ctrl+X/cut path is
    // intentionally NOT bound here — deleting sketch entities already has Delete; copy+delete on
    // Ctrl+X would surprise. The global app router only forwards Ctrl+C/V to sketch mode.
    if (!io.WantTextInput && io.KeyCtrl && !pastePlacing_) {
        if (ImGui::IsKeyPressed(ImGuiKey_C, false) && !selIds_.empty())
            sketchCopySelection(st);
        else if (ImGui::IsKeyPressed(ImGuiKey_V, false) &&
                 st.clipboard.kind == AppState::Clipboard::Kind::SketchEntities)
            sketchPasteBegin(st);
    }

    // ---- Wave 4C/4D: paste-placement mode (preview drawn at the END so the existing geometry still
    // renders underneath). While placing we suppress all tool/selection INTERACTION by treating the
    // canvas as not-hovered, so a placement click never also selects/places — but grid + entities +
    // dimensions still draw normally below. The follow-cursor preview + commit/cancel run after the
    // geometry pass (search "paste-placement preview").
    if (pastePlacing_) imgHovered = false;

    // C41 (T1): Esc consumption flag. The specific Esc behaviors that KEEP the tool armed
    // (Smart Dim pick-buffer clear, Offset stage back-out, Modify box close) set this so the
    // hover-independent catch-all at the end of drawOverlay doesn't also disarm the tool.
    bool escHandled = false;

    // ---- faint grid (lines along sketch u/v through the origin neighbourhood) ----
    // Off by default (SolidWorks ships with the sketch grid off); right-click > Grid toggles it.
    const ImU32 gridCol = col(pal.sketchGrid);
    const ImU32 axisCol = col(pal.sketchGrid, 2.2f);
    if (showGrid_) {
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

    // ---- sketch origin marker (always visible, SolidWorks red L-arrows at (0,0)) ----
    // The origin is real geometry: snapPoint and the Smart Dimension hit-test both expose it as
    // the "origin" handle, so entities can be anchored / dimensioned to it.
    {
        const ImU32 oc = col(pal.statusErr, 0.95f);
        ImVec2 o = uvToScreen(0, 0);
        // unit u/v directions in screen space (normalized axis deltas)
        auto unit = [](ImVec2 d) {
            float L = std::hypot(d.x, d.y);
            return L > 1e-6f ? ImVec2(d.x / L, d.y / L) : ImVec2(1, 0);
        };
        ImVec2 du = unit(uAxisS_), dv = unit(vAxisS_);
        const float len = 22.0f * ui::uiScale;
        ImVec2 tu(o.x + du.x * len, o.y + du.y * len);
        ImVec2 tv(o.x + dv.x * len, o.y + dv.y * len);
        dl->AddLine(o, tu, oc, 1.8f);
        dl->AddLine(o, tv, oc, 1.8f);
        arrowHead(dl, tu, du.x, du.y, 7.0f, oc);
        arrowHead(dl, tv, dv.x, dv.y, 7.0f, oc);
        dl->AddCircle(o, 3.5f, oc, 12, 1.8f);
    }

    // ---- C51: closed-profile region shading (under the entities) ----
    // Loops detected from the live geometry are filled faintly so a closed profile reads as a
    // selectable face; the one the cursor has dwelled in (hotRegion_) brightens + gets an outline,
    // signalling that a click will grab its whole loop. Drawn BEFORE the entities so the boundary
    // lines stay crisp on top. The fill is ear-clipped so concave profiles (L-brackets etc.) shade
    // correctly rather than fanning across the notch.
    rebuildRegions();
    if (hotRegion_ >= (int)regions_.size()) hotRegion_ = -1;
    {
        const ImU32 fillCol = col(pal.accent, 0.13f);
        const ImU32 hotFill = col(pal.accent, 0.28f);
        const ImU32 hotEdge = col(pal.accent, 0.65f);
        for (size_t ri = 0; ri < regions_.size(); ++ri) {
            const auto& rg = regions_[ri];
            bool hot = (tool_ == SketchTool::None && (int)ri == hotRegion_);
            std::vector<ImVec2> sp; sp.reserve(rg.poly.size());
            for (const auto& p : rg.poly) sp.push_back(uvToScreen(p.x, p.y));
            fillPolygon(dl, sp, hot ? hotFill : fillCol);
            if (hot) dl->AddPolyline(sp.data(), (int)sp.size(), hotEdge, ImDrawFlags_Closed, 1.5f);
        }
    }

    // ---- entities (color by solver status via theme roles) ----
    auto statusCol = [&](const SketchEntity& e) -> ImU32 {
        if (e.status == "over") return col(pal.statusErr);
        if (e.status == "full") return col(pal.sketchInk);
        return col(pal.sketchUnder);   // under-defined (blue-ish)
    };
    // Construction dashes: SolidWorks centerline style, dash=6px gap=4px at scale 1.
    const float kConDash = 6.0f * ui::uiScale;
    const float kConGap  = 4.0f * ui::uiScale;
    // Stroke one segment — solid for normal geometry, dashed for construction.
    // addSeg accumulates points into a local vector so the polyline dash phase is continuous.
    for (const auto& e : ents_) {
        // C41 (T2): selected entities render in the accent color, slightly thicker.
        bool sel = !selIds_.empty() && isSelected(e.id);
        ImU32 cc = sel ? col(pal.accent) : statusCol(e);
        float th = (e.construction ? 1.0f : 2.0f) + (sel ? 1.0f : 0.0f);
        // Helper: stroke a polyline; uses dashed rendering for construction entities.
        auto strokePoly = [&](const std::vector<ImVec2>& pts) {
            if (e.construction) {
                drawDashedPolyline(dl, pts, cc, th, kConDash, kConGap);
            } else {
                for (size_t i = 1; i < pts.size(); ++i)
                    dl->AddLine(pts[i-1], pts[i], cc, th);
            }
        };
        if (e.type == "line") {
            ImVec2 a = uvToScreen(e.p1[0], e.p1[1]), b = uvToScreen(e.p2[0], e.p2[1]);
            if (e.construction) {
                drawDashedLine(dl, a, b, cc, th, kConDash, kConGap);
            } else {
                dl->AddLine(a, b, cc, th);
            }
            dl->AddCircleFilled(a, 3.0f, cc);
            dl->AddCircleFilled(b, 3.0f, cc);
        } else if (e.type == "circle") {
            // Sample the circle in (u,v) so it follows the plane mapping exactly.
            int seg = 64;
            std::vector<ImVec2> pts; pts.reserve(seg + 1);
            for (int i = 0; i <= seg; ++i) {
                float a = i / (float)seg * 6.2831853f;
                pts.push_back(uvToScreen(e.center[0] + e.radius * std::cos(a),
                                         e.center[1] + e.radius * std::sin(a)));
            }
            strokePoly(pts);
            dl->AddCircleFilled(uvToScreen(e.center[0], e.center[1]), 2.5f, cc);
        } else if (e.type == "arc") {
            float a0 = e.start, a1 = e.end;
            if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
            if (a1 < a0) a1 += 360.0f;
            int seg = 48;
            std::vector<ImVec2> pts; pts.reserve(seg + 1);
            for (int i = 0; i <= seg; ++i) {
                float a = (a0 + (a1 - a0) * (i / (float)seg)) * 3.14159265f / 180.0f;
                pts.push_back(uvToScreen(e.center[0] + e.radius * std::cos(a),
                                         e.center[1] + e.radius * std::sin(a)));
            }
            strokePoly(pts);
        } else if (e.type == "slot") {
            // Closed capsule profile: two side lines tangent to the caps (parallel to the spine,
            // offset by the half-width = end radius) plus a trimmed semicircle arc at each end.
            // The spine itself renders as a construction centerline, and the trimmed-away inner
            // halves of each end circle render as construction too (as if trimmed by hand).
            float dx = e.p2[0] - e.p1[0], dy = e.p2[1] - e.p1[1];
            float L = std::hypot(dx, dy);
            float hw = e.width * 0.5f;
            float nx = L > 1e-6f ? -dy / L : 0.0f, ny = L > 1e-6f ? dx / L : 0.0f;
            ImVec2 s1a = uvToScreen(e.p1[0] + nx * hw, e.p1[1] + ny * hw);
            ImVec2 s1b = uvToScreen(e.p2[0] + nx * hw, e.p2[1] + ny * hw);
            ImVec2 s2a = uvToScreen(e.p1[0] - nx * hw, e.p1[1] - ny * hw);
            ImVec2 s2b = uvToScreen(e.p2[0] - nx * hw, e.p2[1] - ny * hw);
            float axis1 = std::atan2(e.p1[1] - e.p2[1], e.p1[0] - e.p2[0]);  // outward at p1
            float axis2 = std::atan2(e.p2[1] - e.p1[1], e.p2[0] - e.p1[0]);  // outward at p2
            // Sample a half-circle arc (180 deg centered on `axis`) around end `c`.
            auto halfArc = [&](const float* c, float axis) {
                std::vector<ImVec2> pts; pts.reserve(25);
                for (int i = 0; i <= 24; ++i) {
                    float ang = axis - 1.5707963f + 3.14159265f * (i / 24.0f);
                    pts.push_back(uvToScreen(c[0] + hw * std::cos(ang), c[1] + hw * std::sin(ang)));
                }
                return pts;
            };
            ImVec2 a = uvToScreen(e.p1[0], e.p1[1]), b = uvToScreen(e.p2[0], e.p2[1]);
            if (e.construction) {
                drawDashedLine(dl, s1a, s1b, cc, th, kConDash, kConGap);
                drawDashedLine(dl, s2a, s2b, cc, th, kConDash, kConGap);
                drawDashedPolyline(dl, halfArc(e.p1, axis1), cc, th, kConDash, kConGap);
                drawDashedPolyline(dl, halfArc(e.p2, axis2), cc, th, kConDash, kConGap);
            } else {
                dl->AddLine(s1a, s1b, cc, th);
                dl->AddLine(s2a, s2b, cc, th);
                strokePoly(halfArc(e.p1, axis1));   // outer (kept) cap halves
                strokePoly(halfArc(e.p2, axis2));
                // construction: centerline spine + the trimmed-away inner half of each end circle
                const ImU32 conCol = sel ? cc : col(pal.sketchUnder, 0.85f);
                drawDashedLine(dl, a, b, conCol, 1.0f, kConDash, kConGap);
                drawDashedPolyline(dl, halfArc(e.p1, axis1 + 3.14159265f), conCol, 1.0f,
                                   kConDash, kConGap);
                drawDashedPolyline(dl, halfArc(e.p2, axis2 + 3.14159265f), conCol, 1.0f,
                                   kConDash, kConGap);
            }
        } else if (e.type == "arc_slot") {
            // Capsule outline around a circular spine (center, radius; CCW a0->a1 in start/end).
            // Outer arc (r+hw), inner arc (r-hw), and a half-circle cap at each spine endpoint.
            const float D2R = 3.14159265f / 180.0f;
            float hw = e.width * 0.5f;
            float a0 = e.start, a1 = e.end; if (a1 < a0) a1 += 360.0f;
            auto arcPts = [&](float rad, int seg) {
                std::vector<ImVec2> pts; pts.reserve(seg + 1);
                for (int i = 0; i <= seg; ++i) {
                    float a = (a0 + (a1 - a0) * (i / (float)seg)) * D2R;
                    pts.push_back(uvToScreen(e.center[0] + rad * std::cos(a),
                                             e.center[1] + rad * std::sin(a)));
                }
                return pts;
            };
            auto capPts = [&](float aDeg) {
                float sa = aDeg * D2R;
                float sx = e.center[0] + e.radius * std::cos(sa);
                float sy = e.center[1] + e.radius * std::sin(sa);
                int seg = 24;
                std::vector<ImVec2> pts; pts.reserve(seg + 1);
                for (int i = 0; i <= seg; ++i) {
                    float t = (aDeg - 90.0f + 180.0f * (i / (float)seg)) * D2R;
                    pts.push_back(uvToScreen(sx + hw * std::cos(t), sy + hw * std::sin(t)));
                }
                return pts;
            };
            strokePoly(arcPts(e.radius + hw, 64));
            strokePoly(arcPts(e.radius - hw, 64));
            strokePoly(capPts(e.start));
            strokePoly(capPts(e.end));
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
            int seg = 64;
            std::vector<ImVec2> pts; pts.reserve(seg + 1);
            for (int i = 0; i <= seg; ++i) {
                float a = i / (float)seg * 6.2831853f;
                float lx = e.rx * std::cos(a), ly = e.ry * std::sin(a);
                pts.push_back(uvToScreen(e.center[0] + lx * ca - ly * sa,
                                         e.center[1] + lx * sa + ly * ca));
            }
            strokePoly(pts);
            dl->AddCircleFilled(uvToScreen(e.center[0], e.center[1]), 2.5f, cc);
        } else if (e.type == "spline" || e.type == "style_spline") {
            // C42: polyline through the through-points + point markers. For style_spline this is the
            // control polygon (the editable poles, exactly as SW shows a Style Spline); the smooth
            // by-poles curve is realized by the engine at use/extrude time.
            std::vector<ImVec2> pts; pts.reserve(e.pts.size());
            for (const auto& q : e.pts) pts.push_back(uvToScreen(q.x, q.y));
            strokePoly(pts);
            for (const auto& q : e.pts) dl->AddCircleFilled(uvToScreen(q.x, q.y), 2.5f, cc);
        }
    }

    // C50: selected POINT handles (vertices/centers picked for relations) — filled accent squares.
    // Entity-level selection already thickens the whole entity above; this marks sub-handle picks.
    for (const auto& selh : selIds_) {
        auto dot = selh.find('.');
        if (dot == std::string::npos) continue;   // bare entity id, handled in the entity loop
        const SketchEntity* e = entById(selh.substr(0, dot));
        if (!e) continue;
        std::string sub = selh.substr(dot + 1);
        ImVec2 p;
        if (sub == "p1")          p = uvToScreen(e->p1[0], e->p1[1]);
        else if (sub == "p2")     p = uvToScreen(e->p2[0], e->p2[1]);
        else if (sub == "center") p = uvToScreen(e->center[0], e->center[1]);
        else if (sub == "p")      p = uvToScreen(e->p1[0], e->p1[1]);
        else continue;
        dl->AddRectFilled(ImVec2(p.x - 4, p.y - 4), ImVec2(p.x + 4, p.y + 4), col(pal.accent), 1.5f);
    }

    // ---- C37-D: dimension annotations + relation glyphs (always rendered) ----
    {
        const ImU32 dimLineCol = col(pal.sketchDimLine, 0.85f);
        const ImU32 dimTextCol = col(pal.sketchInk);
        const ImU32 dimDrivenCol = col(pal.consoleDim);     // dimmer for driven (reference) dims
        const ImU32 dimHoverCol = col(pal.accent);
        const ImU32 dimErrCol = col(pal.statusErr);         // C50f: conflicting dims/badges in red
        // C50f: is this constraint id one the solver flagged as conflicting? Conflicting dims/badges
        // render in the error color (SolidWorks paints fighting relations red), matching the entity
        // coloring already done by statusCol.
        auto isConflict = [&](const std::string& cid) {
            // Redundant driving dims count too: a consistent-but-redundant dim still over-defines
            // (SolidWorks paints it red the moment you keep it driving), and the redundant tag is
            // the only hint which dim of the dependent set to delete or make driven.
            return std::find(conflicting_.begin(), conflicting_.end(), cid) != conflicting_.end()
                || std::find(redundant_.begin(),  redundant_.end(),  cid) != redundant_.end();
        };
        // 1) relation badges for geometric (non-dimensional) constraints.
        auto baseId = [](const std::string& h) { return h.substr(0, h.find('.')); };
        auto refScreen = [&](const std::string& h, ImVec2& s) -> bool {
            if (baseId(h) == "origin") { s = uvToScreen(0, 0); return true; }
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
        // C50f: conflict flag flows in so a fighting relation's chip border + glyph go red.
        // C51: accent tint for a selected/hovered relation badge (relations are click-selectable).
        const ImU32 relSelBg     = col(pal.accent, 0.85f);
        const ImU32 relSelBorder = col(pal.accent);
        const ImU32 relSelInk    = col(pal.statusBg);   // dark glyph reads on the accent fill
        auto drawBadge = [&](const std::string& cid, const std::string& h, const char* glyph,
                             const char* name, bool conflict) {
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
            bool hot = isRelSelected(cid) || hoverRelCid_ == cid;
            dl->AddRectFilled(c0, c1, hot ? relSelBg : relBg, 3.5f * ui::uiScale);
            dl->AddRect(c0, c1, conflict ? dimErrCol : (hot ? relSelBorder : relBorder),
                        3.5f * ui::uiScale, 0, (conflict || hot) ? 1.8f : 1.2f);
            ImVec2 gs = ImGui::CalcTextSize(glyph);
            dl->AddText(ImVec2(gp.x - gs.x * 0.5f, gp.y - gs.y * 0.5f),
                        conflict ? dimErrCol : (hot ? relSelInk : relInk), glyph);
            // Record the badge rect (constraint id) for click hit-testing this frame.
            relHits_.push_back({cid, c0, c1});
            // Hover tooltip naming the relation type.
            if (imgHovered && io.MousePos.x >= c0.x && io.MousePos.x <= c1.x &&
                io.MousePos.y >= c0.y && io.MousePos.y <= c1.y)
                ImGui::SetTooltip("%s", name);
        };
        relHits_.clear();
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
                          : c.type == "collinear"     ? "col"
                          : c.type == "coradial"      ? "cr"
                          : c.type == "midpoint"      ? "m"
                          : c.type == "symmetric"     ? "s"
                          : c.type == "fix"           ? "x" : nullptr;
            if (!g) continue;
            bool conflict = isConflict(c.id);
            // Entity-pair relations get a badge on EACH ref (SolidWorks shows both sides);
            // single-entity / point relations get one badge at the first ref.
            bool pairRel = c.type == "parallel" || c.type == "perpendicular" ||
                           c.type == "equal" || c.type == "tangent" || c.type == "concentric" ||
                           c.type == "collinear" || c.type == "coradial" || c.type == "symmetric";
            if (pairRel) { for (const auto& rh : c.refs) drawBadge(c.id, rh, g, c.type.c_str(), conflict); }
            else         drawBadge(c.id, c.refs[0], g, c.type.c_str(), conflict);
        }
        // 2) dimension annotations. C50g: SolidWorks-style picking — a dim is hovered when the
        // mouse is inside its TEXT box (padded ~4px*uiScale) or within ~6px*uiScale of its
        // DIMENSION LINE (not the extension lines). We draw every dim non-hovered first while
        // collecting per-dim hit geometry, resolve the hover (text beats line, then nearest),
        // then re-draw just the winner on top in the hover color. Drawing the geometry is
        // independent of the hover flag (only thickness/color differ), so the overlap is exact
        // and the placement math lives in one place (drawDimension) — no drift.
        struct DimDrawn { const SketchConstraint* c; float pu, pv; bool conflict; DimHit hit; };
        std::vector<DimDrawn> drawn;
        drawn.reserve(cons_.size());
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
            bool conflict = isConflict(c.id);
            ImU32 lc = conflict ? dimErrCol : dimLineCol;
            ImU32 tc = conflict ? dimErrCol : (c.driven ? dimDrivenCol : dimTextCol);
            DimHit hit; hit.cid = c.id;
            drawDimension(dl, c, lc, tc, false, pu, pv, &hit);
            drawn.push_back({&c, pu, pv, conflict, hit});
        }
        // Resolve hover from the collected geometry. Prefer a TEXT-box hit over a line hit; among
        // equals, nearest wins (text: distance to box center; line: perpendicular distance).
        std::string newHover;
        if (imgHovered) {
            const ImVec2 m = io.MousePos;
            const float lineThresh = 6.0f * ui::uiScale;
            const float lineThresh2 = lineThresh * lineThresh;
            auto segDist2 = [](const ImVec2& p, const ImVec2& a, const ImVec2& b) {
                float vx = b.x - a.x, vy = b.y - a.y; float L2 = vx * vx + vy * vy;
                float t = L2 > 1e-6f ? ((p.x - a.x) * vx + (p.y - a.y) * vy) / L2 : 0.0f;
                if (t < 0) t = 0; else if (t > 1) t = 1;
                float cx = a.x + vx * t, cy = a.y + vy * t;
                return (p.x - cx) * (p.x - cx) + (p.y - cy) * (p.y - cy);
            };
            bool haveText = false; float bestText2 = 0, bestLine2 = 0;
            for (const auto& d : drawn) {
                const DimHit& h = d.hit;
                bool inText = m.x >= h.textMin.x && m.x <= h.textMax.x &&
                              m.y >= h.textMin.y && m.y <= h.textMax.y;
                if (inText) {
                    float cx = (h.textMin.x + h.textMax.x) * 0.5f, cy = (h.textMin.y + h.textMax.y) * 0.5f;
                    float d2 = (m.x - cx) * (m.x - cx) + (m.y - cy) * (m.y - cy);
                    if (!haveText || d2 < bestText2) { haveText = true; bestText2 = d2; newHover = h.cid; }
                    continue;
                }
                if (haveText) continue;   // a text hit already beats any line hit
                float ld2 = lineThresh2;
                for (int s = 0; s < h.nSeg; ++s)
                    ld2 = std::min(ld2, segDist2(m, h.seg[s][0], h.seg[s][1]));
                if (ld2 < lineThresh2 && (newHover.empty() || ld2 < bestLine2)) {
                    bestLine2 = ld2; newHover = h.cid;
                }
            }
        }
        // Re-draw the hovered dim on top in the hover color (exact overlap with its first pass).
        if (!newHover.empty()) {
            for (const auto& d : drawn) {
                if (d.c->id != newHover) continue;
                drawDimension(dl, *d.c, dimHoverCol, dimHoverCol, true, d.pu, d.pv);
                break;
            }
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
        if ((!selIds_.empty() || !selRelIds_.empty()) && dimHoverCid_.empty() && !io.WantTextInput &&
            (ImGui::IsKeyPressed(ImGuiKey_Delete) || ImGui::IsKeyPressed(ImGuiKey_Backspace))) {
            // C50: selection may hold point handles ("e3.p2") — delete the OWNING entity, deduped.
            // C51: selected relations are deleted in the same batch via sketch_delete_constraint.
            nlohmann::json ops = nlohmann::json::array();
            std::vector<std::string> done;
            for (const auto& sel : selIds_) {
                std::string base = sel.substr(0, sel.find('.'));
                if (std::find(done.begin(), done.end(), base) != done.end()) continue;
                done.push_back(base);
                ops.push_back({{"op", "sketch_delete_entity"}, {"sketch", fid_}, {"id", base}});
            }
            for (const auto& cid : selRelIds_)
                ops.push_back({{"op", "sketch_delete_constraint"}, {"sketch", fid_}, {"id", cid}});
            auto r = st.docMirror.applyOps(st.worker, ops);
            if (!r.ok) st.toast(ui::ToastLevel::Error, "Delete: " + r.error_message);
            else {
                if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                resolve(st);
            }
            selIds_.clear(); selRelIds_.clear();
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
        auto toggleSel = [&](const std::string& id) {
            auto it = std::find(selIds_.begin(), selIds_.end(), id);
            if (io.KeyCtrl) { if (it != selIds_.end()) selIds_.erase(it); else selIds_.push_back(id); }
            else { selIds_.clear(); selRelIds_.clear(); selIds_.push_back(id); }   // C51: drop relations too
        };
        // C51: also toggle a geometric RELATION into the relation selection (separate from the
        // entity selIds_ buffer). A non-Ctrl pick replaces both selections.
        auto toggleRel = [&](const std::string& cid) {
            auto it = std::find(selRelIds_.begin(), selRelIds_.end(), cid);
            if (io.KeyCtrl) { if (it != selRelIds_.end()) selRelIds_.erase(it); else selRelIds_.push_back(cid); }
            else { selIds_.clear(); selRelIds_.clear(); selRelIds_.push_back(cid); }
        };

        // ---- C51: hover target (what a click will grab). Priority handle > relation badge >
        // entity (or its dwelled loop) > region interior. Computed once and reused for the
        // pre-highlight AND the press, so the cursor's affordance and its action always agree. ----
        bool idleHover = imgHovered && !dragging_ && !boxSelecting_ && !groupDragging_ &&
                         !dragArmed_ && !groupDragArmed_ && !dimDragging_ && dimHoverCid_.empty();
        std::string hovHandle; float hovHu = 0, hovHv = 0;
        std::string hovRel, hovEnt;
        int hovRegion = -1;
        if (idleHover) {
            if (hitTestHandle(io.MousePos, hovHandle, hovHu, hovHv) > 0) {
                // point handle wins
            } else {
                int rb = hitTestRelationBadge(io.MousePos);
                if (rb >= 0) hovRel = relHits_[rb].cid;
                else {
                    int ei = hitTestEntity(io.MousePos);
                    if (ei >= 0) hovEnt = ents_[ei].id;
                    else { int r = hitTestRegion(io.MousePos); if (regionValid(r)) hovRegion = r; }
                }
            }
        }
        hoverRelCid_ = hovRel;   // feeds the badge tint on the next frame's render pass

        // C51: entity-edge dwell -> loop preview. Resting on the same loop-member edge for kLoopDwell
        // grows the pick from that lone edge to its whole closed loop (SolidWorks-ish loop pick).
        // Clicking the INSIDE of a profile selects the loop with no delay (handled via hovRegion).
        constexpr float kLoopDwell = 0.7f;   // ~"a second" of rest to expand an edge to its loop
        bool slow = std::hypot(io.MouseDelta.x, io.MouseDelta.y) < 2.5f;
        int loopOfHov = hovEnt.empty() ? -1 : regionContaining(hovEnt);
        if (!hovEnt.empty() && hovEnt == dwellEnt_ && slow && loopOfHov >= 0) dwellAccum_ += io.DeltaTime;
        else { dwellEnt_ = hovEnt; dwellAccum_ = 0.0f; }
        entityLoopRegion_ = (loopOfHov >= 0 && dwellAccum_ >= kLoopDwell) ? loopOfHov : -1;

        // The loop a click would grab: a hovered interior immediately, else a dwelled edge's loop.
        int effRegion = hovRegion >= 0 ? hovRegion : entityLoopRegion_;

        // ---- C51: pre-highlight — outline exactly what a click will select, drawn on top. ----
        const ImU32 hiCol = col(pal.accent);
        if (!hovHandle.empty()) {
            dl->AddCircle(uvToScreen(hovHu, hovHv), 6.0f, col(pal.hoverHighlight), 16, 2.0f);
            st.requestCursor(ui::CursorKind::Hand);
        } else if (effRegion >= 0) {
            for (const auto& id : regions_[effRegion].ids)
                if (const SketchEntity* e = entById(id)) strokeEntityOutline(dl, *e, hiCol, 2.6f);
            hotRegion_ = effRegion;          // brighten the region fill (next frame)
            st.requestCursor(ui::CursorKind::Hand);
        } else if (!hovEnt.empty()) {
            if (const SketchEntity* e = entById(hovEnt)) strokeEntityOutline(dl, *e, hiCol, 2.6f);
            hotRegion_ = -1;
            st.requestCursor(ui::CursorKind::Hand);
        } else {
            if (hovRel.empty()) hotRegion_ = -1;   // badge hover is tinted in the render pass
            else st.requestCursor(ui::CursorKind::Hand);
        }

        if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !dragging_ &&
            !dragArmed_ && !groupDragArmed_ && !dimDragging_ && dimHoverCid_.empty()) {
            if (!hovHandle.empty()) {
                // C50: ARM a potential drag on this point handle — release without moving selects
                // the handle; a move past threshold reshapes (single point).
                dragArmed_ = true; pendingHandle_ = hovHandle; dragDownPos_ = io.MousePos;
            } else if (!hovRel.empty()) {
                toggleRel(hovRel);                    // relations select on click (no drag)
            } else if (effRegion >= 0) {
                // C51: profile interior, or a dwelled edge's loop -> arm a group drag of the loop;
                // release without moving selects the whole loop.
                groupDragArmed_ = true; boxStart_ = io.MousePos; boxCtrl_ = io.KeyCtrl;
                groupClickKind_ = 2; groupClickRegion_ = effRegion; groupClickEnt_.clear();
            } else if (!hovEnt.empty()) {
                // C51: entity body -> arm a group drag (the whole selection if this entity is in
                // it, else just this entity); release without moving selects it.
                groupDragArmed_ = true; boxStart_ = io.MousePos; boxCtrl_ = io.KeyCtrl;
                groupClickKind_ = 1; groupClickEnt_ = hovEnt; groupClickRegion_ = -1;
            } else {
                boxArmed_ = true; boxStart_ = io.MousePos; boxCtrl_ = io.KeyCtrl;   // empty canvas
            }
        }
        // Armed handle: promote to a real (single-point reshape) drag past the threshold.
        if (dragArmed_ && !dragging_ && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
            if (std::hypot(io.MousePos.x - dragDownPos_.x, io.MousePos.y - dragDownPos_.y) > 4.0f) {
                dragging_ = true; dragHandle_ = pendingHandle_; dragFrame_ = -1; dragArmed_ = false;
            }
        }
        // C51: armed group -> promote to a live profile/selection translate past the threshold.
        if (groupDragArmed_ && !groupDragging_ && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
            if (std::hypot(io.MousePos.x - boxStart_.x, io.MousePos.y - boxStart_.y) > 4.0f) {
                std::vector<std::string> set;
                if (groupClickKind_ == 2 && groupClickRegion_ >= 0 &&
                    groupClickRegion_ < (int)regions_.size()) {
                    set = regions_[groupClickRegion_].ids;
                } else if (!groupClickEnt_.empty() && isSelected(groupClickEnt_)) {
                    for (const auto& s : selIds_) set.push_back(s.substr(0, s.find('.')));
                } else if (!groupClickEnt_.empty()) {
                    set.push_back(groupClickEnt_);
                }
                float su, sv; screenToUV(boxStart_, su, sv);
                beginGroupDrag(set, su, sv);
                groupDragArmed_ = false; dragFrame_ = -1;
            }
        }
        // C51: armed box -> promote to a live rubber band past the threshold.
        if (boxArmed_ && !boxSelecting_ && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
            if (std::hypot(io.MousePos.x - boxStart_.x, io.MousePos.y - boxStart_.y) > 4.0f)
                boxSelecting_ = true;
        }
        // C51: feed the live group translate (throttled once/frame, like the point drag). Compute
        // uv straight from the cursor so the drag tracks even when it strays past the image edge.
        if (groupDragging_ && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
            int frame = ImGui::GetFrameCount();
            if (frame != dragFrame_) {
                dragFrame_ = frame;
                float gu, gv; screenToUV(io.MousePos, gu, gv);
                updateGroupDrag(st, gu, gv);
            }
            st.requestCursor(ui::CursorKind::Hand);
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
        if (ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
            if (dragging_) { dragging_ = false; commitDrag(st); }
            else if (groupDragging_) {
                // C51: profile/selection translate finished -> persist it as one undoable batch.
                groupDragging_ = false; commitDrag(st);
            }
            else if (dragArmed_) {
                // Released without dragging -> select the point handle (vertex/center pick).
                toggleSel(pendingHandle_);
            }
            else if (groupDragArmed_) {
                // C51: press without a drag -> plain click. Select the pressed entity, or the
                // whole loop if the press was inside a hot profile.
                if (groupClickKind_ == 2 && groupClickRegion_ >= 0 &&
                    groupClickRegion_ < (int)regions_.size()) {
                    if (!boxCtrl_) { selIds_.clear(); selRelIds_.clear(); }
                    for (const auto& id : regions_[groupClickRegion_].ids)
                        if (!isSelected(id)) selIds_.push_back(id);
                } else if (!groupClickEnt_.empty()) {
                    toggleSel(groupClickEnt_);
                }
            }
            // C51: finish a box drag, or a plain empty-canvas click.
            else if (boxSelecting_) {
                bool crossing = io.MousePos.x < boxStart_.x;   // right->left = crossing
                boxSelect(boxStart_, io.MousePos, crossing, boxCtrl_);
            } else if (boxArmed_ && !boxCtrl_) {
                selIds_.clear(); selRelIds_.clear();   // C41 (T2): empty-canvas click clears selection
            }
            dragArmed_ = false;
            boxArmed_ = boxSelecting_ = false;
            groupDragArmed_ = false; groupClickKind_ = 0; groupClickEnt_.clear(); groupClickRegion_ = -1;
        }
        // C51: live rubber band. Solid accent = window (L->R, fully-enclosed); dashed amber =
        // crossing (R->L, touch), mirroring SolidWorks' two box-select modes.
        if (boxSelecting_) {
            bool crossing = io.MousePos.x < boxStart_.x;
            ImVec2 lo(std::fmin(boxStart_.x, io.MousePos.x), std::fmin(boxStart_.y, io.MousePos.y));
            ImVec2 hi(std::fmax(boxStart_.x, io.MousePos.x), std::fmax(boxStart_.y, io.MousePos.y));
            ImU32 edge = crossing ? col(pal.sketchInfer) : col(pal.accent, 0.9f);
            dl->AddRectFilled(lo, hi, crossing ? col(pal.sketchInfer, 0.10f) : col(pal.accent, 0.10f));
            if (crossing) {
                drawDashedLine(dl, lo, ImVec2(hi.x, lo.y), edge, 1.4f, 6.0f, 4.0f);
                drawDashedLine(dl, ImVec2(hi.x, lo.y), hi, edge, 1.4f, 6.0f, 4.0f);
                drawDashedLine(dl, hi, ImVec2(lo.x, hi.y), edge, 1.4f, 6.0f, 4.0f);
                drawDashedLine(dl, ImVec2(lo.x, hi.y), lo, edge, 1.4f, 6.0f, 4.0f);
            } else {
                dl->AddRect(lo, hi, edge, 0.0f, 0, 1.4f);
            }
        }
        // (Handle/entity/loop/relation hover highlight is drawn by the C51 pre-highlight above.)

        // C37-D: right-click with no tool active -> small context menu (gestures are off in-sketch,
        // so the RMB is free; consume it before anything else can).
        if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Right) && !dimPopupOpen_)
            ImGui::OpenPopup("##SketchCtx");
    }
    if (ImGui::BeginPopup("##SketchCtx")) {
        if (!selIds_.empty()) {
            if (ImGui::MenuItem("Add Relation...")) {
                relIds_ = selIds_; relPopupOpen_ = true; relPopupPos_ = ImGui::GetMousePos();
            }
            ImGui::Separator();
        }
        if (ImGui::MenuItem("Select")) resetTool();
        if (ImGui::MenuItem("Smart Dimension")) { resetTool(); tool_ = SketchTool::SmartDim; }
        if (ImGui::MenuItem("Grid", nullptr, showGrid_)) showGrid_ = !showGrid_;
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
                     tool_ == SketchTool::Offset ||
                     tool_ == SketchTool::SkFillet || tool_ == SketchTool::SkChamfer ||
                     tool_ == SketchTool::Mirror || tool_ == SketchTool::LinPattern ||
                     tool_ == SketchTool::CircPattern || tool_ == SketchTool::Move);
    if (editTool) {
        int hovered = imgHovered ? hitTestEntity(io.MousePos) : -1;

        // ----- TRIM (power trim): highlight the exact doomed SPAN in red; click trims it; -----
        // ----- holding LMB and sweeping across entities trims each one crossed (SW style). -----
        if (tool_ == SketchTool::Trim) {
            const ImU32 rc = col(pal.statusErr, 0.95f);
            // Preview: the span a click would remove (client-side mirror of the engine split).
            // Spans of unsupported types (slot etc.) draw nothing — the click logs the error.
            if (hovered >= 0) {
                std::vector<ImVec2> span; bool whole = false;
                if (trimDoomedSpan(hovered, mu, mv, span, whole)) {
                    if (whole) {
                        // No interior crossing: the whole entity goes (engine deletes it).
                        const SketchEntity& e = ents_[hovered];
                        if (e.type == "line") {
                            dl->AddLine(uvToScreen(e.p1[0], e.p1[1]),
                                        uvToScreen(e.p2[0], e.p2[1]), rc, 3.5f);
                        } else {
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
                    } else {
                        for (size_t i = 1; i < span.size(); ++i)
                            dl->AddLine(uvToScreen(span[i - 1].x, span[i - 1].y),
                                        uvToScreen(span[i].x, span[i].y), rc, 3.5f);
                    }
                }
            }
            // One trim op at the current cursor (shared by click + sweep).
            auto fireTrim = [&](int idx) -> bool {
                const SketchEntity& e = ents_[idx];
                if (e.type != "line" && e.type != "circle" && e.type != "arc") {
                    st.addLog("[sketch] Trim: unsupported entity type '" + e.type + "'");
                    st.toast(ui::ToastLevel::Info,
                             "Trim works on lines, circles, and arcs — not " + e.type);
                    return false;
                }
                nlohmann::json op = {{"op", "sketch_trim"}, {"sketch", fid_},
                    {"entity", e.id}, {"point", nlohmann::json::array({mu, mv})}};
                auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
                if (!r.ok) { st.addLog("[sketch] trim failed: " + r.error_message); return false; }
                if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                resolve(st);   // refresh entities/statuses (same path as add_entity)
                return true;
            };
            if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                if (fireTrim(hovered)) { trimSweepLast_ = io.MousePos; trimSweeping_ = true; }
            } else if (trimSweeping_ && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                // Power-trim sweep: each entity the cursor crosses while LMB is held gets trimmed.
                // A small pixel gate between ops avoids re-firing on the same spot while the
                // entity list refills (ids change across every trim).
                float moved = std::hypot(io.MousePos.x - trimSweepLast_.x,
                                         io.MousePos.y - trimSweepLast_.y);
                if (imgHovered && moved > 10.0f && hovered >= 0) {
                    const SketchEntity& e = ents_[hovered];
                    if (e.type == "line" || e.type == "circle" || e.type == "arc")
                        if (fireTrim(hovered)) trimSweepLast_ = io.MousePos;
                }
            }
            if (!ImGui::IsMouseDown(ImGuiMouseButton_Left)) trimSweeping_ = false;
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
                    st.toast(ui::ToastLevel::Info,
                             "Extend works on lines and arcs — not " + e.type);
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

        // ----- SKETCH FILLET / CHAMFER (C52): pick two lines; second pick builds the op. -----
        // Engine handles LINE-LINE corners only (sketch_fillet radius / sketch_chamfer distance).
        // The radius/distance numeric field lives in a small always-on overlay pinned top-left
        // (drawn after the interaction pass) and persists across corners. On the second pick the op
        // is applied; success appends the new arc/line + re-solves and we stay armed for another
        // corner. A non-line pick toasts and is ignored. Esc clears a held first pick (or exits the
        // tool if none is held).
        else if (tool_ == SketchTool::SkFillet || tool_ == SketchTool::SkChamfer) {
            const ImU32 ac = col(pal.accent, 0.95f);
            const ImU32 hc = col(pal.hoverHighlight, 0.9f);
            // Tint the already-picked first line in accent.
            const SketchEntity* first = corner1Id_.empty() ? nullptr : entById(corner1Id_);
            if (first && first->type == "line")
                dl->AddLine(uvToScreen(first->p1[0], first->p1[1]),
                            uvToScreen(first->p2[0], first->p2[1]), ac, 3.5f);
            // Hover preview (faint) over the would-be-picked line (skip the already-picked one).
            if (hovered >= 0 && ents_[hovered].type == "line" && ents_[hovered].id != corner1Id_) {
                const SketchEntity& e = ents_[hovered];
                dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), hc, 3.0f);
            }
            if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                const SketchEntity& e = ents_[hovered];
                if (e.type != "line") {
                    const char* what = tool_ == SketchTool::SkFillet ? "Sketch Fillet" : "Sketch Chamfer";
                    st.addLog(std::string("[sketch] ") + what + ": pick a line, not " + e.type);
                    st.toast(ui::ToastLevel::Info,
                             std::string(what) + " works on two lines — not " + e.type);
                } else if (corner1Id_.empty()) {
                    corner1Id_ = e.id;                 // first line picked; wait for the second
                } else if (e.id == corner1Id_) {
                    // Clicking the same line again deselects it (pick a different first line).
                    corner1Id_.clear();
                } else {
                    applyCornerOp(st, e.id);           // second line: build + apply, then stay armed
                }
            }
            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
                escHandled = true;   // C41 (T1): clear a held first pick, else exit the tool
                if (!corner1Id_.empty()) corner1Id_.clear();
                else cancelTool();
            }
        }

        // ----- C53 MIRROR / LINEAR PATTERN / CIRCULAR PATTERN: collect an entity selection -----
        // Stage 0 collects line/circle/arc entities (click toggles, like the Offset chain). Enter
        // advances: Mirror -> stage 1 pick a construction line (the mirror axis); Circular -> stage 1
        // pick the center point; Linear -> stage 1 the numeric panel. The op is applied from the
        // numeric panel / line pick (drawn after the interaction pass). Esc backs out a stage.
        else if (tool_ == SketchTool::Mirror || tool_ == SketchTool::LinPattern ||
                 tool_ == SketchTool::CircPattern) {
            const ImU32 ac = col(pal.accent, 0.95f);
            const ImU32 hc = col(pal.hoverHighlight, 0.9f);
            auto inSel = [&](const std::string& id) {
                return std::find(mpSel_.begin(), mpSel_.end(), id) != mpSel_.end();
            };
            auto strokeEnt = [&](const SketchEntity& e, ImU32 c, float th) {
                if (e.type == "line")
                    dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), c, th);
                else if (e.type == "circle" || e.type == "arc") {
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
                        if (i) dl->AddLine(prev, p, c, th); prev = p;
                    }
                }
            };
            // Tint the current selection.
            for (const auto& e : ents_) if (inSel(e.id)) strokeEnt(e, ac, 3.5f);

            if (mpStage_ == 0) {
                if (hovered >= 0 && !inSel(ents_[hovered].id)) strokeEnt(ents_[hovered], hc, 3.0f);
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                    const SketchEntity& e = ents_[hovered];
                    if (e.type != "line" && e.type != "circle" && e.type != "arc") {
                        st.toast(ui::ToastLevel::Info,
                                 "Mirror/Pattern works on lines, circles, and arcs — not " + e.type);
                    } else {
                        auto it = std::find(mpSel_.begin(), mpSel_.end(), e.id);
                        if (it != mpSel_.end()) mpSel_.erase(it); else mpSel_.push_back(e.id);
                    }
                }
                if (!mpSel_.empty() && ImGui::IsKeyPressed(ImGuiKey_Enter)) mpStage_ = 1;
            } else if (tool_ == SketchTool::Mirror && mpStage_ == 1) {
                // Pick the mirror line (must be a line entity; a construction line is the norm).
                if (hovered >= 0 && ents_[hovered].type == "line" && !inSel(ents_[hovered].id))
                    strokeEnt(ents_[hovered], hc, 3.0f);
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                    const SketchEntity& e = ents_[hovered];
                    if (e.type != "line")
                        st.toast(ui::ToastLevel::Info, "Pick a line as the mirror axis, not " + e.type);
                    else if (inSel(e.id))
                        st.toast(ui::ToastLevel::Info, "The mirror line can't be one of the mirrored entities");
                    else
                        applyMirrorOp(st, e.id);   // build + apply, reset selection, stay armed
                }
            } else if (tool_ == SketchTool::CircPattern && mpStage_ == 1) {
                // Pick the center point: snap to a vertex/center if near one, else the raw cursor.
                std::string snapH; float su = mu, sv = mv;
                snapPoint(su, sv, snapH);
                dl->AddCircle(uvToScreen(su, sv), 5.0f, col(pal.accent, 0.95f), 16, 2.0f);
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                    patternCenterU_ = su; patternCenterV_ = sv;
                    mpStage_ = 2;   // advance to the numeric panel
                }
            }
            // Visualize the chosen circular center while in the panel stage.
            if (tool_ == SketchTool::CircPattern && mpStage_ == 2)
                dl->AddCircle(uvToScreen(patternCenterU_, patternCenterV_), 5.0f,
                              col(pal.accent, 0.95f), 16, 2.0f);

            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
                escHandled = true;
                if (mpStage_ > 0) { mpStage_ = (tool_ == SketchTool::CircPattern) ? mpStage_ - 1 : 0; }
                else cancelTool();
            }
        }

        // ----- C54 MOVE/COPY: stage 0 collect entities (Enter advances) -> stage 1 pick a -----
        // ----- FROM point -> stage 2 live drag to a TO point; the second click applies         -----
        // ----- sketch_move {entities, dx, dy, copy} with delta = to - from. Ctrl => copy.       -----
        else if (tool_ == SketchTool::Move) {
            const ImU32 ac = col(pal.accent, 0.95f);
            const ImU32 hc = col(pal.hoverHighlight, 0.9f);
            auto inSel = [&](const std::string& id) {
                return std::find(mpSel_.begin(), mpSel_.end(), id) != mpSel_.end();
            };
            auto strokeEnt = [&](const SketchEntity& e, ImU32 c, float th) {
                if (e.type == "line")
                    dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), c, th);
                else if (e.type == "circle" || e.type == "arc") {
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
                        if (i) dl->AddLine(prev, p, c, th); prev = p;
                    }
                }
            };
            for (const auto& e : ents_) if (inSel(e.id)) strokeEnt(e, ac, 3.5f);

            if (mpStage_ == 0) {
                if (hovered >= 0 && !inSel(ents_[hovered].id)) strokeEnt(ents_[hovered], hc, 3.0f);
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                    const SketchEntity& e = ents_[hovered];
                    if (e.type != "line" && e.type != "circle" && e.type != "arc") {
                        st.toast(ui::ToastLevel::Info,
                                 "Move works on lines, circles, and arcs — not " + e.type);
                    } else {
                        auto it = std::find(mpSel_.begin(), mpSel_.end(), e.id);
                        if (it != mpSel_.end()) mpSel_.erase(it); else mpSel_.push_back(e.id);
                    }
                }
                if (!mpSel_.empty() && ImGui::IsKeyPressed(ImGuiKey_Enter)) mpStage_ = 1;
            } else if (mpStage_ == 1) {
                // Pick the FROM anchor (snap to a vertex/center if near one).
                std::string snapH; float su = mu, sv = mv;
                snapPoint(su, sv, snapH);
                dl->AddCircle(uvToScreen(su, sv), 5.0f, ac, 16, 2.0f);
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                    moveFromUV_ = ImVec2(su, sv);
                    mpStage_ = 2;
                }
            } else if (mpStage_ == 2) {
                // Live drag to the TO point; second click applies the move (Ctrl => copy).
                std::string snapH; float su = mu, sv = mv;
                snapPoint(su, sv, snapH);
                bool copy = io.KeyCtrl;
                dl->AddLine(uvToScreen(moveFromUV_.x, moveFromUV_.y), uvToScreen(su, sv),
                            ac, 1.6f);
                dl->AddCircle(uvToScreen(moveFromUV_.x, moveFromUV_.y), 4.0f, ac, 16, 1.6f);
                // Ghost-preview the selection translated by the live delta.
                float pdx = su - moveFromUV_.x, pdy = sv - moveFromUV_.y;
                const ImU32 gc = col(pal.sketchUnder, 0.8f);
                for (const auto& e : ents_) {
                    if (!inSel(e.id)) continue;
                    if (e.type == "line") {
                        dl->AddLine(uvToScreen(e.p1[0] + pdx, e.p1[1] + pdy),
                                    uvToScreen(e.p2[0] + pdx, e.p2[1] + pdy), gc, 2.0f);
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
                            ImVec2 p = uvToScreen(e.center[0] + pdx + e.radius * std::cos(a),
                                                  e.center[1] + pdy + e.radius * std::sin(a));
                            if (i) dl->AddLine(prev, p, gc, 2.0f); prev = p;
                        }
                    }
                }
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                    applyMoveOp(st, su - moveFromUV_.x, sv - moveFromUV_.y, copy);
                    // applyMoveOp resets the selection + stage 0 for another move.
                }
            }

            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
                escHandled = true;
                if (mpStage_ > 0) mpStage_ = mpStage_ - 1;   // back out from to-pick -> from-pick -> select
                else cancelTool();
            }
        }
    }

    // ---- C53 CONVERT ENTITIES: pick MODEL edges/faces in the viewport, project each into ----
    // ---- the sketch (sketch_convert). Like Smart Dim's external-ref pick, but it applies the ----
    // ---- op immediately and stays armed for more converts. Esc exits the tool. ----
    if (tool_ == SketchTool::Convert) {
        render::PickHit extHit{};
        if (imgHovered && st.hasDocMesh && st.idPass) {
            int px = (int)(io.MousePos.x - imgMin.x), py = (int)(io.MousePos.y - imgMin.y);
            render::PickHit h = st.idPass->pickFinal(px, py, vw, vh);
            if (h.valid() && (h.kind == render::PickKind::Edge || h.kind == render::PickKind::Face))
                extHit = h;
        }
        st.hover = extHit;   // standard 3D hover highlight on the pickable edge/face
        if (extHit.valid() && imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
            std::string perr;
            std::string selStr = st.docMirror.resolvePick(
                st.worker, extHit.body_id,
                extHit.kind == render::PickKind::Edge ? "edge" : "face", extHit.topo_id, perr);
            nlohmann::json selJs;
            if (!selStr.empty()) { try { selJs = nlohmann::json::parse(selStr); } catch (...) {} }
            if (selJs.is_object()) {
                applyConvertRef(st, nlohmann::json{
                    {"kind", extHit.kind == render::PickKind::Edge ? "edge" : "face"},
                    {"selector", selJs}});
            } else {
                st.toast(ui::ToastLevel::Warn, "Can't convert that geometry" +
                         (perr.empty() ? std::string() : ": " + perr));
            }
        }
        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) { escHandled = true; cancelTool(); }
    }

    // ---- drawing tools: rubber-band preview + click handling ----
    if (!editTool && tool_ != SketchTool::None && tool_ != SketchTool::SmartDim &&
        tool_ != SketchTool::Convert) {
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
            // Slot: only rubber-band the spine while the length is still being set (1 click
            // placed). Once both spine endpoints exist, the width-stage capsule preview below
            // owns the drawing — otherwise this would draw a stray line to the cursor.
            if (tool_ == SketchTool::Line ||
                (tool_ == SketchTool::Slot && toolUV_.size() == 1)) {
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
            // Straight-slot capsule preview: two side lines tangent to the end caps (parallel to
            // the spine, offset by the half-width) plus a 180-degree cap arc at each spine end.
            auto drawStraightSlot = [&](ImVec2 p1, ImVec2 p2, float hw) {
                float dx = p2.x - p1.x, dy = p2.y - p1.y; float L = std::hypot(dx, dy);
                if (L < 1e-6f) return;
                float nx = -dy / L, ny = dx / L;          // unit perpendicular to the spine
                dl->AddLine(uvToScreen(p1.x + nx * hw, p1.y + ny * hw),
                            uvToScreen(p2.x + nx * hw, p2.y + ny * hw), uCol, 1.6f);
                dl->AddLine(uvToScreen(p1.x - nx * hw, p1.y - ny * hw),
                            uvToScreen(p2.x - nx * hw, p2.y - ny * hw), uCol, 1.6f);
                auto cap = [&](ImVec2 c, float axis) {       // outward-facing 180-deg arc
                    ImVec2 prev;
                    for (int i = 0; i <= 24; ++i) {
                        float a = axis - 1.5707963f + 3.14159265f * (i / 24.0f);
                        ImVec2 q = uvToScreen(c.x + hw * std::cos(a), c.y + hw * std::sin(a));
                        if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                    }
                };
                cap(p1, std::atan2(p1.y - p2.y, p1.x - p2.x));
                cap(p2, std::atan2(p2.y - p1.y, p2.x - p1.x));
                // centerline previewed as the (dashed) construction spine
                drawDashedLine(dl, uvToScreen(p1.x, p1.y), uvToScreen(p2.x, p2.y),
                               col(pal.sketchUnder, 0.7f), 1.0f, 6.0f, 4.0f);
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
            } else if (tool_ == SketchTool::Slot && n >= 2) {
                // spine fixed by clicks 1 & 2; cursor sets the width (perp distance from spine).
                ImVec2 p1 = toolUV_[0], p2 = toolUV_[1];
                float dx = p2.x - p1.x, dy = p2.y - p1.y; float L = std::hypot(dx, dy);
                float hw = L > 1e-6f ? std::fabs((su - p1.x) * (-dy) + (sv - p1.y) * dx) / L : 5.0f;
                if (hw < 1e-4f) hw = 1e-4f;
                drawStraightSlot(p1, p2, hw);
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
                    if (hw < 1e-4f) hw = 1e-4f;
                    drawStraightSlot(e1, e2, hw);
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
            } else if (tool_ == SketchTool::ArcTangent) {
                // C54: derive the source + free endpoint each frame; the arc starts there tangent to
                // the source and sweeps to the live cursor. Highlight the snapped start; if no source
                // exists we fall back to a plain 2-point chord (treated as a near-straight arc).
                TangentArcSrc src;
                if (tangentArcSource(io.MousePos, src)) {
                    // bright dot on the tangent start + faint tangent direction tick
                    ImVec2 sS = uvToScreen(src.start.x, src.start.y);
                    dl->AddCircleFilled(sS, 4.0f, col(pal.accent, 0.95f));
                    ImVec2 tEnd = uvToScreen(src.start.x + src.tdir.x * 6.0f,
                                             src.start.y + src.tdir.y * 6.0f);
                    dl->AddLine(sS, tEnd, col(pal.accent, 0.6f), 1.2f);
                    float cu, cv, rad, a0, a1; bool ccw;
                    if (tangentArcThrough(src.start, src.tdir, ImVec2(su, sv),
                                          cu, cv, rad, a0, a1, ccw)) {
                        if (a1 < a0) a1 += 360.0f;
                        float lo = ccw ? a0 : a1, hi = ccw ? a1 : a0;
                        if (hi < lo) hi += 360.0f;
                        const float D2R = 3.14159265f / 180.0f;
                        ImVec2 prev;
                        for (int i = 0; i <= 48; ++i) {
                            float a = (lo + (hi - lo) * (i / 48.0f)) * D2R;
                            ImVec2 q = uvToScreen(cu + rad * std::cos(a), cv + rad * std::sin(a));
                            if (i) dl->AddLine(prev, q, uCol, 1.6f); prev = q;
                        }
                    } else {
                        // Degenerate (cursor on the tangent line): preview a straight chord.
                        dl->AddLine(sS, sCur, uCol, 1.6f);
                    }
                } else {
                    // No source -> behave like a 3-pt arc's first leg (rubber chord from cursor).
                    dl->AddCircleFilled(sCur, 3.0f, uCol);
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
            } else if ((tool_ == SketchTool::Spline || tool_ == SketchTool::StyleSpline) && n >= 1) {
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

        // Click placement. EqCurve takes no viewport pick (origin-relative; its popup opens on the
        // ribbon command). Text takes exactly one placement click and then defers to its popup, so
        // suppress further captures while that popup is open.
        if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
            tool_ != SketchTool::EqCurve && !(tool_ == SketchTool::Text && textPopupOpen_)) {
            toolUV_.push_back(ImVec2(su, sv));
            toolSnaps_.push_back(snapH);
            // Sketch Text — the single click captures the lower-left anchor; open the popup that
            // collects the string + cap height. The entity is emitted from the popup's OK.
            if (tool_ == SketchTool::Text && toolUV_.size() == 1) {
                textAnchorUV_ = toolUV_[0];
                textPopupPos_ = io.MousePos;
                textPopupOpen_ = true;
                textFocus_ = true;
                toolUV_.clear(); toolSnaps_.clear();   // anchor stashed; stop click capture
            } else
            // Tools that complete on a fixed number of clicks:
            // C54: Tangent Arc — ONE click places the end. The start + tangent come from the source
            // entity (resolved at click time). We commit an arc tangent to the source there, joined
            // by a coincident at the shared endpoint and a tangent relation (arc <-> source) so it
            // stays tangent under drag. No source / degenerate geometry -> toast a hint and reset.
            if (tool_ == SketchTool::ArcTangent && toolUV_.size() == 1) {
                ImVec2 endPt = toolUV_[0];
                TangentArcSrc src;
                float cu, cv, rad, a0, a1; bool ccw;
                if (!tangentArcSource(io.MousePos, src)) {
                    st.toast(ui::ToastLevel::Info,
                             "Tangent Arc: draw or select a line/arc first, then click the arc end");
                    resetTool(); tool_ = SketchTool::ArcTangent;
                } else if (!tangentArcThrough(src.start, src.tdir, endPt,
                                              cu, cv, rad, a0, a1, ccw)) {
                    st.toast(ui::ToastLevel::Info,
                             "Tangent Arc: the end is on the tangent line (no arc) — pick a point off it");
                    resetTool(); tool_ = SketchTool::ArcTangent;
                } else {
                    // The solved arc always sweeps start->end; store CCW with the matching angle order
                    // (solver drops the ccw flag, so a false flag would re-render mirrored). When the
                    // geometric sweep is CW, store the CCW arc end->start (same curve) and join the
                    // source endpoint to whichever arc handle landed on the start.
                    float sStart = a0, sEnd = a1;
                    const char* startHandle = "$self.start";
                    if (!ccw) { float t = sStart; sStart = sEnd; sEnd = t; startHandle = "$self.end"; }
                    nlohmann::json ent = {{"type", "arc"},
                        {"center", nlohmann::json::array({cu, cv})}, {"radius", rad},
                        {"start", sStart}, {"end", sEnd}, {"ccw", true}};
                    // Relations: coincident joining the source endpoint to the arc's start handle +
                    // tangent(arc, source). For a whole-circle source there's no endpoint sub-handle
                    // (src.handle == src.id) so we skip the coincident and keep only the tangent.
                    nlohmann::json cs = nlohmann::json::array();
                    if (src.handle != src.id)
                        cs.push_back({{"type", "coincident"}, {"refs", {src.handle, startHandle}}});
                    cs.push_back({{"type", "tangent"}, {"refs", {"$self", src.id}}});
                    addEntity(st, ent, cs);
                    resetTool(); tool_ = SketchTool::ArcTangent;   // stay armed for another tangent arc
                }
            } else if (tool_ == SketchTool::Line && toolUV_.size() == 2) {
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
                // Axis-aligned rect: 4 coincident-chained lines with per-edge h/v relations.
                // C50: route through addQuadLoop so the 4 line batches COALESCE into one undo step
                // (was an inline loop with 4 separate undo steps -> "vanishes one corner at a time").
                ImVec2 quad[4] = {{a.x, a.y}, {b.x, a.y}, {b.x, b.y}, {a.x, b.y}};
                const char* rel[4] = {"horizontal", "vertical", "horizontal", "vertical"};
                addQuadLoop(st, quad, rel);
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
                // C51: emit a decomposed slot (2 lines + 2 arcs + construction centerline) so every
                // piece is individually selectable / dimensionable.
                addDecomposedSlot(st, p1, p2, w * 2.0f);
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
                // C51: decomposed slot (real lines + arcs + construction centerline).
                addDecomposedSlot(st, e1, e2, w * 2.0f);
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
        // Style Spline shares the exact same collect/finish UX (>= 2 poles) and only differs in the
        // emitted entity type ("style_spline" — control poles, not interpolated through-points).
        if (tool_ == SketchTool::Spline || tool_ == SketchTool::StyleSpline) {
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
                const char* entType = (tool_ == SketchTool::StyleSpline) ? "style_spline" : "spline";
                nlohmann::json ent = {{"type", entType}, {"points", pts}};
                addEntity(st, ent, nlohmann::json::array());
                resetTool();
            }
        }
        // C37-D: right-click ends the current chain/segment but keeps the tool armed (SW muscle
        // memory: the Line tool drops its in-progress points and waits for a fresh first point).
        // mouse_gestures is disabled in-sketch (viewport_panel), so the RMB is free here; consume it.
        // C42: Spline / Style Spline own their RMB (finish/drop, handled above).
        if (tool_ != SketchTool::Spline && tool_ != SketchTool::StyleSpline && imgHovered &&
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
        // A pick can still be chained while fewer than 2 targets are collected — even when a
        // single-pick combo (diameter / line length) is already previewing: clicking ANOTHER
        // target replaces it with the two-pick combo (SolidWorks behavior; this also makes the
        // line-line angle and all circle-distance combos actually reachable).
        const bool canAddPick = !dimPopupOpen_ && !overDefOpen_ && dimPicks_.size() < 2;
        DimPick hoverPick; bool hoverHit = false;
        if (canAddPick && imgHovered) hoverHit = dimHitTest(io.MousePos, hoverPick);
        if (hoverHit) {
            // A midpoint pick (handle ".mid") draws as a small filled diamond — the SolidWorks
            // midpoint glyph — to distinguish it from an endpoint circle.
            bool isMid = hoverPick.isPoint && hoverPick.handle.size() > 4 &&
                         hoverPick.handle.compare(hoverPick.handle.size() - 4, 4, ".mid") == 0;
            if (isMid) {
                ImVec2 m = uvToScreen(hoverPick.u, hoverPick.v); float r = 5.0f;
                dl->AddQuadFilled(ImVec2(m.x, m.y - r), ImVec2(m.x + r, m.y),
                                  ImVec2(m.x, m.y + r), ImVec2(m.x - r, m.y), hiCol);
            } else if (hoverPick.isPoint) {
                dl->AddCircle(uvToScreen(hoverPick.u, hoverPick.v), 6.0f, hiCol, 12, 2.0f);
            } else if (hoverPick.entIdx >= 0) {
                const SketchEntity& e = ents_[hoverPick.entIdx];
                if (e.type == "line") {
                    dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), hiCol, 2.5f);
                    // SolidWorks shows the midpoint marker whenever a line is hovered, so the user
                    // knows they can snap to it — a hollow diamond at the center.
                    ImVec2 m = uvToScreen((e.p1[0]+e.p2[0])*0.5f, (e.p1[1]+e.p2[1])*0.5f); float r = 5.0f;
                    dl->AddQuad(ImVec2(m.x, m.y - r), ImVec2(m.x + r, m.y),
                                ImVec2(m.x, m.y + r), ImVec2(m.x - r, m.y), hiCol, 1.6f);
                } else if (e.type == "circle" || e.type == "arc")
                    dl->AddCircle(uvToScreen(e.center[0], e.center[1]),
                                  std::hypot(uvToScreen(e.center[0] + e.radius, e.center[1]).x -
                                             uvToScreen(e.center[0], e.center[1]).x, 0.0f), hiCol, 48, 2.5f);
            }
        }

        // External (model) geometry: when nothing sketch-side is under the cursor, probe the
        // id-buffer for a model edge/face. st.hover drives the standard 3D hover highlight
        // (rendered next frame); a click projects the reference into the sketch as a FIXED
        // construction entity (worker sketch_project_ref) and chains it as a pick.
        render::PickHit extHit{};
        if (canAddPick && imgHovered && !hoverHit && st.hasDocMesh && st.idPass) {
            int px = (int)(io.MousePos.x - imgMin.x), py = (int)(io.MousePos.y - imgMin.y);
            render::PickHit h = st.idPass->pickFinal(px, py, vw, vh);
            if (h.valid() && (h.kind == render::PickKind::Edge || h.kind == render::PickKind::Face))
                extHit = h;
        }
        st.hover = extHit;   // empty hit = no highlight; cleared when SmartDim disarms (below)

        // Phase A: collecting picks (no full combo yet). A click adds the hovered target.
        if (!dimPopupOpen_ && !overDefOpen_) {
            // Rebuild the pending dim from the current picks + live cursor (drives the preview AND
            // the live h/v/aligned variant choice for point-point).
            dimBuildPending(io.MousePos, mu, mv);

            bool clicked = imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left);
            bool pickAdded = false;
            if (clicked && canAddPick && dimHoverCid_.empty()) {
                // C47: dimHoverCid_ guard — a click on an existing dim's text belongs to the
                // drag / double-click-edit handler above, not a new pick.
                if (hoverHit) {
                    // Avoid duplicate picks of the same target. C38 (S1): compare entity picks by
                    // id (stable) rather than ents_ index (per-frame, may have shifted).
                    bool dup = false;
                    for (const auto& q : dimPicks_)
                        if (q.isPoint == hoverPick.isPoint &&
                            (hoverPick.isPoint ? q.handle == hoverPick.handle
                                               : q.entId == hoverPick.entId)) dup = true;
                    if (!dup) {
                        // Shift+click a circle/arc = tangent (near-rim "min") anchor; plain click
                        // anchors at the center. Either way the Modify box can switch it after.
                        if (!hoverPick.isPoint) {
                            const SketchEntity* ce = entById(hoverPick.entId);
                            if (ce && (ce->type == "circle" || ce->type == "arc"))
                                hoverPick.cond = io.KeyShift ? "min" : "center";
                        }
                        dimPicks_.push_back(hoverPick);
                        pickAdded = true;
                        dimBuildPending(io.MousePos, mu, mv);   // refresh the combo immediately
                    }
                } else if (extHit.valid()) {
                    std::string perr;
                    std::string selStr = st.docMirror.resolvePick(
                        st.worker, extHit.body_id,
                        extHit.kind == render::PickKind::Edge ? "edge" : "face",
                        extHit.topo_id, perr);
                    nlohmann::json selJs;
                    if (!selStr.empty()) { try { selJs = nlohmann::json::parse(selStr); } catch (...) {} }
                    if (selJs.is_object()) {
                        nlohmann::json ref = {
                            {"kind", extHit.kind == render::PickKind::Edge ? "edge" : "face"},
                            {"selector", selJs}};
                        if (!addProjectedDimRef(st, ref, selStr).empty()) {
                            pickAdded = true;
                            dimBuildPending(io.MousePos, mu, mv);
                        }
                    } else {
                        st.toast(ui::ToastLevel::Warn, "Can't reference that geometry" +
                                 (perr.empty() ? std::string() : ": " + perr));
                    }
                }
            }
            if (dimHasPending_) {
                // LIVE PREVIEW: a full annotation following the cursor until the placement click.
                SketchConstraint pc;
                pc.type = dimType_; pc.axis = dimAxis_;
                pc.hasMeasured = true; pc.measured = dimMeasured_;
                if (dimPending_.contains("refs"))
                    for (const auto& rr : dimPending_["refs"]) pc.refs.push_back(rr.get<std::string>());
                if (dimPending_.contains("cond"))
                    for (const auto& q : dimPending_["cond"]) pc.cond.push_back(q.get<std::string>());
                drawDimension(dl, pc, hiCol, hiCol, true, mu, mv);
                // Placement click (a click that didn't chain a pick) stores the placement and
                // opens the Modify box.
                if (clicked && !pickAdded) {
                    dimPlaceU_ = mu; dimPlaceV_ = mv;
                    dimOpenModify(io.MousePos);
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
    } else if (st.hover.valid()) {
        // Smart Dimension was the only in-sketch writer of st.hover (model-ref picking); clear a
        // stale highlight once the tool disarms (the viewport's own picking is off in-sketch).
        st.hover = render::PickHit{};
    }

    // ---- Wave 4C/4D: paste-placement preview (drawn LAST, on top of the existing geometry) ----
    // imgHovered was forced false above so no tool/selection interaction consumed the placement
    // click. The follow-cursor outline preview is shifted by (cursor - copyOrigin); a left-click over
    // the canvas commits the paste at that offset, Esc cancels. We re-derive canvas-hover from the
    // image rect (the suppressed imgHovered can't be reused for the commit click).
    if (pastePlacing_) {
        st.requestCursor(ui::CursorKind::Crosshair);
        drawPastePreview(st, dl, io.MousePos);
        const char* banner = "Click to place \xE2\x80\x94 Esc to cancel";
        ImVec2 bts = ImGui::CalcTextSize(banner);
        float bcx = (imgMin.x + imgMax.x) * 0.5f;
        ImVec2 b0(bcx - bts.x * 0.5f - 14, imgMin.y + 52.0f * ui::uiScale);
        ImVec2 b1(bcx + bts.x * 0.5f + 14, b0.y + bts.y + 12);
        dl->AddRectFilled(b0, b1, col(pal.overlayBg), 6.0f);
        dl->AddRect(b0, b1, col(pal.overlayBorder), 6.0f);
        dl->AddText(ImVec2(b0.x + 14, b0.y + 6), col(pal.overlayText), banner);
        bool overCanvas = ImGui::IsMouseHoveringRect(imgMin, imgMax);
        // Exclude the ✓/✗ confirmation corner (top-right) so a click there doesn't also commit the
        // paste — same geometry the top-of-function corner block uses to steal hover from tools.
        {
            const float sz = 30.0f, pad = 12.0f;
            ImVec2 okMin(imgMax.x - pad - sz, imgMin.y + pad), okMax(okMin.x + sz, okMin.y + sz);
            ImVec2 cxMin(okMin.x, okMax.y + 6), cxMax(cxMin.x + sz, cxMin.y + sz);
            if (ImGui::IsMouseHoveringRect(okMin, okMax) || ImGui::IsMouseHoveringRect(cxMin, cxMax))
                overCanvas = false;
        }
        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
            pastePlacing_ = false; pasteBundle_ = nlohmann::json();
            st.toast(ui::ToastLevel::Info, "Paste cancelled");
        } else if (overCanvas && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
            float cu = 0, cv = 0; screenToUV(io.MousePos, cu, cv);
            commitPasteEntities(st, cu - pasteAnchorUV_.x, cv - pasteAnchorUV_.y);
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
                         tool_ == SketchTool::SkFillet ? "Sketch Fillet" :
                         tool_ == SketchTool::SkChamfer ? "Sketch Chamfer" :
                         tool_ == SketchTool::Mirror ? "Mirror Entities" :
                         tool_ == SketchTool::LinPattern ? "Linear Pattern" :
                         tool_ == SketchTool::CircPattern ? "Circular Pattern" :
                         tool_ == SketchTool::Convert ? "Convert Entities" :
                         tool_ == SketchTool::Move ? "Move Entities" :
                         tool_ == SketchTool::ArcTangent ? "Tangent Arc" :
                         tool_ == SketchTool::Arc3pt ? "3 Point Arc" :
                         tool_ == SketchTool::Polygon ? "Polygon" :
                         tool_ == SketchTool::RectCenter ? "Center Rectangle" :
                         tool_ == SketchTool::Point ? "Point" :
                         tool_ == SketchTool::Ellipse ? "Ellipse" :
                         tool_ == SketchTool::Spline ? "Spline" :
                         tool_ == SketchTool::StyleSpline ? "Style Spline" :
                         tool_ == SketchTool::Text ? "Text" :
                         tool_ == SketchTool::EqCurve ? "Equation Driven Curve" : "";
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
                step = dimPicks_.empty()
                     ? "click an entity, point, model edge/face, or a tree plane (Shift+circle = tangent)"
                     : dimPicks_.size() < 2
                     ? "click another entity/point/model ref, or click empty space to place"
                     : "click to place the dimension"; break;
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
            case SketchTool::SkFillet: {
                static char fb[64];
                std::snprintf(fb, sizeof(fb), corner1Id_.empty()
                                  ? "pick two lines (radius %.3g mm)"
                                  : "pick the second line (radius %.3g mm)", skFilletRadius_);
                step = fb; break;
            }
            case SketchTool::SkChamfer: {
                static char cb[64];
                std::snprintf(cb, sizeof(cb), corner1Id_.empty()
                                  ? "pick two lines (distance %.3g mm)"
                                  : "pick the second line (distance %.3g mm)", skChamferDist_);
                step = cb; break;
            }
            case SketchTool::Mirror:
                step = mpStage_ == 0 ? "click entities to mirror, then Enter"
                                     : "click the mirror line (construction line)"; break;
            case SketchTool::LinPattern:
                step = mpStage_ == 0 ? "click entities to pattern, then Enter"
                                     : "set count / spacing / direction, then Apply"; break;
            case SketchTool::CircPattern:
                step = mpStage_ == 0 ? "click entities to pattern, then Enter"
                     : mpStage_ == 1 ? "click the pattern center point"
                                     : "set count / angle, then Apply"; break;
            case SketchTool::Convert:
                step = "click a model edge or face to project into the sketch"; break;
            case SketchTool::Move:
                step = mpStage_ == 0 ? "click entities to move, then Enter"
                     : mpStage_ == 1 ? "click the from point (anchor)"
                                     : "click the to point \xC2\xB7 hold Ctrl to copy"; break;
            case SketchTool::ArcTangent:
                step = "click the arc end (it starts tangent to the prior/selected entity)"; break;
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
            case SketchTool::StyleSpline:
                step = n < 2 ? "click control poles; Enter / right-click / double-click to finish"
                             : "click more poles \xC2\xB7 Enter / right-click / double-click finishes"; break;
            case SketchTool::Text:
                step = textPopupOpen_ ? "type the text + height, then OK"
                                      : "click to place the text"; break;
            case SketchTool::EqCurve:
                step = "enter x(t) / y(t) and the t range, then OK"; break;
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

        // C50f: conflict-freeze warning bar. When the last solve came back solve_rejected:true the
        // engine froze geometry at the last valid coordinates (no least-squares mangling); tell the
        // user the sketch couldn't be solved and point at the red (conflicting) relations to edit.
        // Sits just below the status pill, error-tinted. Cleared automatically when a later solve
        // succeeds (applyEnterResult sets solveRejected_ = false).
        if (solveRejected_) {
            const char* warn = "Sketch could not be solved \xE2\x80\x94 geometry frozen at last valid "
                               "state. Delete or edit a conflicting (red) relation.";
            ImVec2 wts = ImGui::CalcTextSize(warn);
            ImVec2 w0(imgMin.x + 12, p1b.y + 8);
            ImVec2 w1(w0.x + wts.x + 20, w0.y + wts.y + 10);
            ImU32 ec = col(pal.statusErr);
            wdl->AddRectFilled(w0, w1, col(pal.overlayBg), 6.0f);
            wdl->AddRect(w0, w1, ec, 6.0f, 0, 1.5f);
            wdl->AddText(ImVec2(w0.x + 10, w0.y + 5), ec, warn);
        }
    }

    // ---- Smart Dimension Modify box (C50 redesign) ----
    // SolidWorks-style compact panel: titled header with the dim type + live readout, a prominent
    // value field flanked by steppers with the unit suffix, Driven + Arc-condition controls, an
    // inline error line, and a primary OK / Cancel footer. Auto-focused + text pre-selected; Enter
    // commits, Esc closes without committing (pick buffer cleared) but stays in the tool. Expression
    // input ("=...") and full unit math ("100mm+2in/0.5m") keep working.
    if (dimPopupOpen_) {
        const float sc = ui::uiScale;
        ImGui::SetNextWindowPos(dimPopupPos_, ImGuiCond_Appearing);
        ImGui::SetNextWindowBgAlpha(0.99f);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(12 * sc, 10 * sc));
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(7 * sc, 7 * sc));
        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 4.0f);
        if (ImGui::Begin("##SketchDim", nullptr,
                         ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                         ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
            const bool isAngle = dimIsAngle_;
            const char* suffix = isAngle ? "\xC2\xB0" : skLenUnit();
            // Friendly title + symbol per dim type.
            const char* title = dimType_ == "diameter" ? "\xE2\x8C\x80 Diameter"
                              : dimType_ == "radius"   ? "R Radius"
                              : dimType_ == "angle"    ? "\xE2\x88\xA0 Angle"
                              : "Distance";
            // ---- header row: title (accent) + live measured readout on the right ----
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
            ImGui::TextUnformatted(dimEditCid_.empty() ? title : (std::string("Edit ") + title).c_str());
            ImGui::PopStyleColor();
            {
                std::string meas = isAngle ? fmtAng(dimMeasured_)
                                           : (fmtLen(dimMeasured_) + " " + skLenUnit());
                std::string lab = "now " + meas;
                float w = ImGui::CalcTextSize(lab.c_str()).x;
                ImGui::SameLine(0, 0);
                ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                    std::max(14.0f * sc, ImGui::GetContentRegionAvail().x - w));
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.consoleDim[0], pal.consoleDim[1], pal.consoleDim[2], 1));
                ImGui::TextUnformatted(lab.c_str());
                ImGui::PopStyleColor();
            }
            ImGui::Separator();

            // ---- value row: [-] [ value ] unit [+] ----
            auto stepVal = [&](double delta) {
                std::string txt = dimBuf_.data();
                if (!txt.empty() && txt[0] == '=') return;   // don't mutate expressions
                // Steps are one DISPLAY unit: in IPS a length step is 1 inch (= 25.4 mm canonical).
                const double bareScale = (g_skIps && !isAngle) ? 25.4 : 1.0;
                double cur = dimMeasured_; std::string perr;
                if (pmParseDim(txt, cur, perr, isAngle, bareScale)) {}  // parse current text if valid
                cur += delta * ((isAngle) ? 1.0 : bareScale);
                std::snprintf(dimBuf_.data(), dimBuf_.size(), "%s", fmtLen(cur).c_str());
            };
            const float btn = ImGui::GetFrameHeight();
            if (ImGui::Button("-", ImVec2(btn, btn))) stepVal(-dimPopupStep_);
            ImGui::SameLine();
            ImGui::SetNextItemWidth(150 * sc);
            if (dimPopupFocus_) ImGui::SetKeyboardFocusHere();
            bool enter = ImGui::InputText("##dimval", dimBuf_.data(), dimBuf_.size(),
                                          ImGuiInputTextFlags_EnterReturnsTrue |
                                          (dimPopupFocus_ ? ImGuiInputTextFlags_AutoSelectAll : 0));
            dimPopupFocus_ = false;
            ImGui::SameLine(0, 4 * sc);
            ImGui::TextUnformatted(suffix);
            ImGui::SameLine();
            if (ImGui::Button("+", ImVec2(btn, btn))) stepVal(dimPopupStep_);

            // ---- options row: Driven + Arc condition ----
            ImGui::Checkbox("Driven (reference)", &dimPopupDriven_);
            if (dimEditCid_.empty() && dimType_ == "distance") {
                static const char* kConds[3] = {"Center", "Min (near rim)", "Max (far rim)"};
                bool condChanged = false;
                int shown = 0;
                for (auto& p : dimPicks_) {
                    if (p.isPoint) continue;
                    const SketchEntity* ce = entById(p.entId);
                    if (!ce || (ce->type != "circle" && ce->type != "arc")) continue;
                    int cur = p.cond == "min" ? 1 : p.cond == "max" ? 2 : 0;
                    double adjOld = cur == 1 ? -(double)ce->radius : cur == 2 ? (double)ce->radius : 0.0;
                    ImGui::SetNextItemWidth(150 * sc);
                    std::string lbl = std::string(shown == 0 ? "Arc condition" : "Arc condition 2")
                                      + "##cond" + p.entId;
                    if (ImGui::Combo(lbl.c_str(), &cur, kConds, 3)) {
                        double adjNew = cur == 1 ? -(double)ce->radius : cur == 2 ? (double)ce->radius : 0.0;
                        p.cond = cur == 1 ? "min" : cur == 2 ? "max" : "center";
                        dimMeasured_ += adjNew - adjOld;
                        condChanged = true;
                    }
                    ++shown;
                }
                if (condChanged && dimPending_.contains("refs")) {
                    nlohmann::json cond = nlohmann::json::array();
                    for (const auto& rr : dimPending_["refs"]) {
                        std::string rs = rr.get<std::string>();
                        std::string cv = "center";
                        for (const auto& p : dimPicks_)
                            if (!p.isPoint && p.entId == rs && !p.cond.empty()) cv = p.cond;
                        cond.push_back(cv);
                    }
                    dimPending_["cond"] = cond;
                    std::snprintf(dimBuf_.data(), dimBuf_.size(), "%s", fmtLen(dimMeasured_).c_str());
                }
            }
            if (!dimError_.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1));
                ImGui::Text("! %s", dimError_.c_str());
                ImGui::PopStyleColor();
            }

            // ---- footer: primary OK (accent) + Cancel, right-aligned ----
            ImGui::Separator();
            const float okW = 64 * sc, cancelW = 64 * sc;
            ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                std::max(0.0f, ImGui::GetContentRegionAvail().x - okW - cancelW - 7 * sc));
            ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
            ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.85f));
            ImGui::PushStyleColor(ImGuiCol_ButtonActive, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.70f));
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.windowBg[0], pal.windowBg[1], pal.windowBg[2], 1));
            bool ok = ImGui::Button("OK", ImVec2(okW, 0)) || enter;
            ImGui::PopStyleColor(4);
            ImGui::SameLine();
            bool cancel = ImGui::Button("Cancel", ImVec2(cancelW, 0));
            if (ok) dimCommit(st);
            if (cancel || (ImGui::IsKeyPressed(ImGuiKey_Escape) && !ImGui::IsKeyDown(ImGuiKey_LeftCtrl))) {
                escHandled = true;   // C41 (T1): box close keeps Smart Dimension armed
                dimPopupOpen_ = false; dimError_.clear();
                if (dimEditCid_.empty()) { dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr; }
                dimEditCid_.clear();
            }
        }
        ImGui::End();
        ImGui::PopStyleVar(3);
    }

    // C50f: SolidWorks over-define pre-flight dialogs (add path: "Make Dimension Driven?";
    // edit path: "This value over-defines the sketch"). Both are opened by sketch_probe before
    // anything is committed.
    drawOverDefinePrompt(st);
    drawEditOverDefinePrompt(st);
    drawRelationOverDefinePrompt(st);

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

    // ---- C52: Sketch Fillet / Chamfer numeric input (always shown while the tool is armed) ----
    // A small always-on overlay near the status chip holds the radius (fillet) / distance (chamfer);
    // the value persists across corners and invocations. Editing it never disturbs the pick state —
    // the two lines are still picked by clicking in the viewport. Pinned top-left so it doesn't fight
    // the cursor while the user clicks corners (unlike Offset's one-shot box near the cursor).
    if (tool_ == SketchTool::SkFillet || tool_ == SketchTool::SkChamfer) {
        const bool fillet = (tool_ == SketchTool::SkFillet);
        ImGui::SetNextWindowPos(ImVec2(imgMin.x + 12, imgMin.y + 52), ImGuiCond_Appearing);
        ImGui::SetNextWindowBgAlpha(0.98f);
        if (ImGui::Begin(fillet ? "##SketchFilletParam" : "##SketchChamferParam", nullptr,
                         ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                         ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
            ImGui::TextUnformatted(fillet ? "Fillet radius (mm)" : "Chamfer distance (mm)");
            ImGui::SetNextItemWidth(120);
            float* val = fillet ? &skFilletRadius_ : &skChamferDist_;
            ImGui::InputFloat(fillet ? "##skfilletr" : "##skchamferd", val, 1.0f, 5.0f, "%.3f");
            if (*val < 0.001f) *val = 0.001f;   // engine requires a strictly positive value
        }
        ImGui::End();
    }

    // ---- C53: Linear / Circular Pattern numeric panel (count + spacing/angle, then Apply) ----
    // Shown once the entity selection is locked (Enter): Linear at stage 1, Circular at stage 2
    // (after the center pick). The dims persist across invocations. Apply builds the op; Cancel
    // backs out to re-pick. The viewport selection is frozen while the panel is up.
    bool linPanel  = (tool_ == SketchTool::LinPattern  && mpStage_ == 1);
    bool circPanel = (tool_ == SketchTool::CircPattern && mpStage_ == 2);
    if (linPanel || circPanel) {
        ImGui::SetNextWindowPos(ImVec2(imgMin.x + 12, imgMin.y + 52), ImGuiCond_Appearing);
        ImGui::SetNextWindowBgAlpha(0.98f);
        if (ImGui::Begin(linPanel ? "##SketchLinPattern" : "##SketchCircPattern", nullptr,
                         ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                         ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
            ImGui::Text(linPanel ? "Linear Pattern (%d entities)" : "Circular Pattern (%d entities)",
                        (int)mpSel_.size());
            ImGui::Separator();
            ImGui::SetNextItemWidth(120);
            ImGui::InputInt("Count", &patternCount_);
            if (patternCount_ < 2) patternCount_ = 2;   // engine requires count >= 2
            if (linPanel) {
                ImGui::SetNextItemWidth(120);
                ImGui::InputFloat("Spacing (mm)", &patternSpacing_, 1.0f, 10.0f, "%.3f");
                ImGui::SetNextItemWidth(180);
                ImGui::InputFloat2("Direction (u,v)", &patternDirU_, "%.2f");
            } else {
                ImGui::SetNextItemWidth(120);
                ImGui::InputFloat(patternEqualSpacing_ ? "Total angle (deg)" : "Step angle (deg)",
                                  &patternAngle_, 1.0f, 15.0f, "%.2f");
                ImGui::Checkbox("Equal spacing (full span)", &patternEqualSpacing_);
            }
            ImGui::Separator();
            if (ImGui::Button("Apply")) applyPatternOp(st, circPanel);
            ImGui::SameLine();
            if (ImGui::Button("Cancel")) { mpStage_ = circPanel ? 1 : 0; }
        }
        ImGui::End();
    }

    // ---- Sketch Text + Equation-Driven Curve parameter popups (engine entities text / eq_curve) ----
    drawTextPopup(st, escHandled);
    drawEqCurvePopup(st, escHandled);

    // ---- C50: Add Relation popup over an N-element selection (entities and/or point handles) ----
    // Classifies the snapshotted selection and offers the SolidWorks relations valid for it:
    // points -> coincident / horizontal (same Y) / vertical (same X); lines -> horizontal /
    // vertical / collinear / perpendicular(2) / parallel / equal / symmetric(3); circles/arcs ->
    // coradial / equal / concentric / tangent(2); line+circle -> tangent; point+line -> point_on /
    // midpoint; 2 same-kind subjects + 1 line -> symmetric (axis last). Pair relations chain to the
    // first ref (e0~e1, e0~e2, …); symmetric uses an explicit ordered [a,b,axis] ref list.
    if (relPopupOpen_) {
        for (const auto& h : relIds_)
            if (!entById(h.substr(0, h.find('.')))) { relPopupOpen_ = false; relIds_.clear(); break; }
    }
    if (relPopupOpen_) {
        // Classify: a handle with a '.' (or a bare point entity) is a POINT; bare line/circle ids
        // are entities. (Centers select as "eN.center" = point.)
        auto isPointHandle = [&](const std::string& h) {
            if (h.find('.') != std::string::npos) return true;
            const SketchEntity* e = entById(h);
            return e && e->type == "point";
        };
        int nPts = 0, nLines = 0, nCircles = 0, nOther = 0;
        for (const auto& h : relIds_) {
            if (isPointHandle(h)) { ++nPts; continue; }
            const SketchEntity* e = entById(h);
            if (!e) { ++nOther; continue; }
            if (e->type == "line") ++nLines;
            else if (e->type == "circle" || e->type == "arc") ++nCircles;
            else ++nOther;
        }
        const int n = (int)relIds_.size();

        ImGui::SetNextWindowPos(relPopupPos_, ImGuiCond_Appearing);
        ImGui::SetNextWindowBgAlpha(0.98f);
        if (ImGui::Begin("##SketchRel", nullptr,
                         ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                         ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
            ImGui::Text("Add Relation  (%d selected)", n);
            ImGui::PopStyleColor();
            ImGui::Separator();
            bool any = false;
            // Emit a relation. mode: 'each' = one constraint per ref ($self-style single ref);
            // 'chain' = relate refs[0] to every other ref; 'pair' = exactly refs[0],refs[1].
            auto emit = [&](const char* label, const char* type, char mode) {
                any = true;
                if (!ImGui::MenuItem(label)) return;
                nlohmann::json ops = nlohmann::json::array();
                if (mode == 'e') {
                    for (const auto& h : relIds_)
                        ops.push_back({{"op", "sketch_add_constraint"}, {"sketch", fid_},
                            {"constraint", {{"type", type}, {"refs", nlohmann::json::array({h})}}}});
                } else if (mode == 'p') {
                    ops.push_back({{"op", "sketch_add_constraint"}, {"sketch", fid_},
                        {"constraint", {{"type", type}, {"refs", nlohmann::json::array({relIds_[0], relIds_[1]})}}}});
                } else {  // chain
                    for (size_t i = 1; i < relIds_.size(); ++i)
                        ops.push_back({{"op", "sketch_add_constraint"}, {"sketch", fid_},
                            {"constraint", {{"type", type}, {"refs", nlohmann::json::array({relIds_[0], relIds_[i]})}}}});
                }
                addRelationOps(st, ops, label);
            };

            // Emit a single multi-ref constraint with an explicit ordered ref list (used by
            // symmetric, whose engine refs are [subjectA, subjectB, axisLine] — axis LAST).
            auto emitOrdered = [&](const char* label, const char* type,
                                   const std::vector<std::string>& refs) {
                any = true;
                if (!ImGui::MenuItem(label)) return;
                nlohmann::json arr = nlohmann::json::array();
                for (const auto& r : refs) arr.push_back(r);
                nlohmann::json ops = nlohmann::json::array();
                ops.push_back({{"op", "sketch_add_constraint"}, {"sketch", fid_},
                    {"constraint", {{"type", type}, {"refs", arr}}}});
                addRelationOps(st, ops, label);
            };

            const bool onlyPts    = nPts == n;
            const bool onlyLines  = nLines == n;
            const bool onlyCircs  = nCircles == n;

            if (onlyPts && n >= 2) {
                emit("Coincident / Merge", "coincident", n == 2 ? 'p' : 'c');
                emit("Horizontal (align Y)", "horizontal", 'c');
                emit("Vertical (align X)",   "vertical",   'c');
            } else if (onlyLines && n == 1) {
                emit("Horizontal", "horizontal", 'e');
                emit("Vertical",   "vertical",   'e');
                emit("Fix",        "fix",        'e');
            } else if (onlyLines && n >= 2) {
                // Order mirrors SolidWorks' line context menu: H, V, Collinear, Perp, Parallel, Equal.
                emit("Horizontal (each)", "horizontal", 'e');
                emit("Vertical (each)",   "vertical",   'e');
                emit("Collinear", "collinear", 'c');
                if (n == 2) emit("Perpendicular", "perpendicular", 'p');
                emit("Parallel", "parallel", 'c');
                emit("Equal", "equal", 'c');
                // 3 lines selected: offer Symmetric with the LAST-picked line as the mirror axis
                // (relIds_ preserves pick order). subjects = the two earlier lines.
                if (n == 3)
                    emitOrdered("Symmetric (last line = axis)", "symmetric",
                                {relIds_[0], relIds_[1], relIds_[2]});
            } else if (onlyCircs && n >= 2) {
                emit("Collinear / Coradial", "coradial", 'c');  // same center + same radius
                emit("Equal", "equal", 'c');
                emit("Concentric", "concentric", 'c');
                if (n == 2) emit("Tangent", "tangent", 'p');    // curve <-> curve tangency
            } else if (n == 2 && nLines == 1 && nCircles == 1) {
                emit("Tangent", "tangent", 'p');
            } else if (n == 2 && nPts == 1 && nLines == 1) {
                // point + line. Two readings: point ON the line, or point at the line MIDPOINT.
                // Both expect refs [point, line]; reorder so the point handle is first.
                if (!isPointHandle(relIds_[0])) std::swap(relIds_[0], relIds_[1]);
                emit("Coincident (point on line)", "point_on", 'p');
                emit("Midpoint (point at line center)", "midpoint", 'p');
            } else if (n == 3 && nLines == 1 && (nPts == 2 || nCircles == 2)) {
                // Two same-kind subjects + one line: mirror the subjects across that line.
                // refs = [subjectA, subjectB, axisLine]; pull the lone line out to the end.
                std::vector<std::string> subs; std::string axis;
                for (const auto& h : relIds_) {
                    const SketchEntity* e = isPointHandle(h) ? nullptr : entById(h);
                    if (e && e->type == "line") axis = h; else subs.push_back(h);
                }
                if (subs.size() == 2 && !axis.empty())
                    emitOrdered("Symmetric (across line)", "symmetric", {subs[0], subs[1], axis});
            } else if (n == 1) {
                emit("Fix", "fix", 'e');
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
