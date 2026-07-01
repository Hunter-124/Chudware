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
using ui::sketch::col;
using ui::sketch::kSnapPx;
using ui::sketch::kHandlePx;
using ui::sketch::parseEntities;
using ui::sketch::skBaseId;
using ui::sketch::sketchParamsInTree;
using ui::sketch::polyAreaUV;
using ui::sketch::pointInPolyUV;
using ui::sketch::ptInRect;
using ui::sketch::segCrossesRect;
using ui::sketch::resyncMirror;
using ui::sketch::catmullRomUV;
#define skPal ui::sketch::sketchPalette()

namespace ui {
bool SketchMode::sketchCopySelection(SketchHost& st) {
    if (!active_ || selIds_.empty()) return false;
    const nlohmann::json* params = sketchParamsInTree(st, fid_);
    if (!params) { st.toast(ui::sketch::ToastLevel::Warn, "Copy: sketch not found"); return false; }

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
    if (entities.empty()) { st.toast(ui::sketch::ToastLevel::Warn, "Nothing to copy"); return false; }

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
    st.clipboard.kind = ui::sketch::SketchClipboard::Kind::SketchEntities;
    st.clipboard.srcDocId = st.docMirror().docId();
    st.clipboard.payload = nlohmann::json{{"entities", entities}, {"constraints", constraints}};
    st.clipboard.copyOriginU = any ? mnU : 0.0f;
    st.clipboard.copyOriginV = any ? mnV : 0.0f;
    st.addLog("[sketch] copied " + std::to_string(entities.size()) + " entit(ies)");
    st.toast(ui::sketch::ToastLevel::Info,
             "Copied " + std::to_string(entities.size()) + " sketch entit(ies)");
    return true;
}

// Arm the paste-placement mode from a SketchEntities clipboard. The pasted outlines then follow the
// cursor (drawPastePreview) until the next viewport click commits, or Esc cancels. Snapshots the
// bundle + anchor so a later clipboard change can't disturb the in-progress placement.
bool SketchMode::sketchPasteBegin(SketchHost& st) {
    if (!active_) return false;
    if (st.clipboard.kind != ui::sketch::SketchClipboard::Kind::SketchEntities) return false;
    const nlohmann::json& pl = st.clipboard.payload;
    if (!pl.is_object() || !pl.contains("entities") || !pl["entities"].is_array() ||
        pl["entities"].empty())
        return false;
    // Cancel any armed drawing tool / Smart-Dim pick so placement owns the next click cleanly.
    resetTool();
    pasteBundle_ = pl;
    pasteAnchorUV_ = ImVec2(st.clipboard.copyOriginU, st.clipboard.copyOriginV);
    pastePlacing_ = true;
    st.toast(ui::sketch::ToastLevel::Info, "Click to place pasted entities (Esc to cancel)");
    return true;
}

// Commit the pasted bundle shifted by (offsetX, offsetY) via the sketch_paste_entities op. Routed
// through the normal ops path (revision bump + undo) like sketch_add_entity, then re-solve so the
// new geometry shows. Op errors are toasted and leave the sketch untouched.
void SketchMode::commitPasteEntities(SketchHost& st, float offsetX, float offsetY) {
    if (!pasteBundle_.is_object()) { pastePlacing_ = false; return; }
    nlohmann::json op = {
        {"op", "sketch_paste_entities"},
        {"sketch", fid_},
        {"entities_bundle", pasteBundle_},
        {"offset_x", offsetX},
        {"offset_y", offsetY},
    };
    auto r = st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}));
    pastePlacing_ = false;
    pasteBundle_ = nlohmann::json();
    if (!r.ok) { st.toast(ui::sketch::ToastLevel::Error, "Paste: " + r.error_message); return; }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);
    st.addLog("[sketch] pasted entities (dx=" + std::to_string(offsetX) +
              ", dy=" + std::to_string(offsetY) + ")");
}

// Follow-cursor preview of the paste bundle: parse its raw entities, translate them by
// (cursorUV - anchor), and stroke each outline in the accent color. The cursor delta is computed in
// sketch (u,v) so the preview lands exactly where the commit op will place the geometry.
void SketchMode::drawPastePreview(SketchHost& st, ImDrawList* dl, const ImVec2& cursor) {
    if (!pasteBundle_.is_object() || !basisOk_) return;
    (void)st;
    float cu = 0, cv = 0; screenToUV(cursor, cu, cv);
    float du = cu - pasteAnchorUV_.x, dv = cv - pasteAnchorUV_.y;
    // Parse the bundle entities into temporary SketchEntity records, then shift each in (u,v).
    std::vector<SketchEntity> preview;
    nlohmann::json wrap = nlohmann::json{{"entities", pasteBundle_["entities"]}};
    parseEntities(wrap, preview);
    const ImU32 c = col(skPal.accent, 0.9f);
    for (auto& e : preview) {
        e.p1[0] += du; e.p1[1] += dv;
        e.p2[0] += du; e.p2[1] += dv;
        e.center[0] += du; e.center[1] += dv;
        for (auto& q : e.pts) { q.x += du; q.y += dv; }
        strokeEntityOutline(dl, e, c, std::max(1.5f, 2.0f * ui::sketch::uiScale()));
    }
}

// C38 (S1): resolve a stored entity id to its CURRENT index in ents_ (-1 if gone). DimPicks store
// the id (stable) rather than the index (refilled by applyEnterResult/resolve mid-pick).
void SketchMode::applyCornerOp(SketchHost& st, const std::string& line2) {
    const bool fillet = (tool_ == SketchTool::SkFillet);
    nlohmann::json op = {{"op", fillet ? "sketch_fillet" : "sketch_chamfer"},
                         {"sketch", fid_}, {"e1", corner1Id_}, {"e2", line2}};
    if (fillet) op["radius"]   = skFilletRadius_;
    else        op["distance"] = skChamferDist_;
    auto r = st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}));
    if (!r.ok) {
        // Engine refused (bad corner / unsupported entity): surface the message, keep tool armed.
        std::string msg = r.error_message.empty()
            ? std::string(fillet ? "Sketch fillet failed" : "Sketch chamfer failed")
            : r.error_message;
        st.addLog(std::string("[sketch] ") + (fillet ? "fillet" : "chamfer") + " failed: " + msg);
        st.toast(ui::sketch::ToastLevel::Warn, msg);
        corner1Id_.clear();   // start a fresh pair (match Offset's reset-on-error UX)
        return;
    }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);              // refresh entities/statuses (same path as trim/extend/offset)
    corner1Id_.clear();       // ready for the next corner; stay in the tool
}

// C53: apply sketch_mirror_entities (entities=mpSel_, line=lineId) and refresh. On engine OpError
// the message is toasted and the selection is reset (SolidWorks lets you start a fresh mirror).
void SketchMode::applyMirrorOp(SketchHost& st, const std::string& lineId) {
    nlohmann::json eids = nlohmann::json::array();
    for (const auto& id : mpSel_) eids.push_back(id);
    nlohmann::json op = {{"op", "sketch_mirror_entities"}, {"sketch", fid_},
                         {"entities", eids}, {"line", lineId}};
    auto r = st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}));
    if (!r.ok) {
        std::string msg = r.error_message.empty() ? std::string("Mirror failed") : r.error_message;
        st.addLog("[sketch] mirror failed: " + msg);
        st.toast(ui::sketch::ToastLevel::Warn, msg);
    } else {
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
        resolve(st);
    }
    mpStage_ = 0; mpSel_.clear();   // stay in the tool, ready for another mirror
}

// C53: apply sketch_pattern_entities (linear or circular) for the current selection + dims.
void SketchMode::applyPatternOp(SketchHost& st, bool circular) {
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
    auto r = st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}));
    if (!r.ok) {
        std::string msg = r.error_message.empty() ? std::string("Pattern failed") : r.error_message;
        st.addLog("[sketch] pattern failed: " + msg);
        st.toast(ui::sketch::ToastLevel::Warn, msg);
    } else {
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
        resolve(st);
    }
    mpStage_ = 0; mpSel_.clear();   // stay in the tool, ready for another pattern
}

// C53: apply one sketch_convert op for a model edge/face ref (a {kind, selector} or {datum} object).
void SketchMode::applyConvertRef(SketchHost& st, const nlohmann::json& ref) {
    nlohmann::json op = {{"op", "sketch_convert"}, {"sketch", fid_},
                         {"refs", nlohmann::json::array({ref})}};
    auto r = st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}));
    if (!r.ok) {
        std::string msg = r.error_message.empty() ? std::string("Convert failed") : r.error_message;
        st.addLog("[sketch] convert failed: " + msg);
        st.toast(ui::sketch::ToastLevel::Warn, msg);
        return;
    }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);   // the projected entity appears; stay armed for more converts
}

// C54: apply sketch_move (translate or copy) for the current selection + delta.
void SketchMode::applyMoveOp(SketchHost& st, float dx, float dy, bool copy) {
    nlohmann::json eids = nlohmann::json::array();
    for (const auto& id : mpSel_) eids.push_back(id);
    nlohmann::json op = {{"op", "sketch_move"}, {"sketch", fid_},
                         {"entities", eids}, {"dx", dx}, {"dy", dy}, {"copy", copy}};
    auto r = st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}));
    if (!r.ok) {
        std::string msg = r.error_message.empty() ? std::string("Move failed") : r.error_message;
        st.addLog("[sketch] move failed: " + msg);
        st.toast(ui::sketch::ToastLevel::Warn, msg);
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
void SketchMode::updateGroupDrag(SketchHost& st, float curU, float curV) {
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


}  // namespace ui
