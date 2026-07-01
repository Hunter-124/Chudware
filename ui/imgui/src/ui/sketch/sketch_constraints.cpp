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
using ui::sketch::resyncMirror;
#define skPal ui::sketch::sketchPalette()

namespace ui {
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

// Render one dimension annotation. (pu,pv) is the placement in (u,v).
void SketchMode::drawDimension(ImDrawList* dl, const SketchConstraint& c, ImU32 lineCol,
                               ImU32 textCol, bool hovered, float pu, float pv, DimHit* hitOut) const {
    // C50g: helper to record the rendered text box (padded) + a dim-line segment for picking.
    auto recText = [&](ImVec2 pos, ImVec2 size) {
        if (!hitOut) return;
        const float pad = 4.0f * ui::sketch::uiScale();
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

    // Arc conditions: a "min"/"max" circle ref attaches at its rim instead of the centre. A
    // DIRECTIONAL (h/v) dimension attaches at the TANGENT LINE — centre±r along the axis (matches
    // the engine's coordinate-based tangent-line residual; works for concentric/nested circles
    // where the old "toward/away the other centre" shift collapsed to nothing). An ALIGNED
    // dimension uses the near/far rim along the A->B centre line (engine adds ±r to the scalar gap).
    if (!c.cond.empty()) {
        const bool directional = (c.axis == "h" || c.axis == "v");
        if (directional) {
            auto tangent = [&](size_t i, ImVec2& P) {
                if (c.cond.size() <= i || c.refs.size() <= i) return;
                const std::string& cd = c.cond[i];
                if (cd != "min" && cd != "max") return;
                const SketchEntity* e = entById(baseId(c.refs[i]));
                if (!e || (e->type != "circle" && e->type != "arc")) return;
                float s = (cd == "min") ? -1.0f : 1.0f;
                float ru = e->center[0] + (c.axis == "h" ? s * e->radius : 0.0f);
                float rv = e->center[1] + (c.axis == "v" ? s * e->radius : 0.0f);
                P = uvToScreen(ru, rv);
            };
            tangent(0, A); tangent(1, B);
            if (c.axis == "h")      B.y = A.y;   // re-collapse onto the axis after the rim shift
            else if (c.axis == "v") B.x = A.x;
        } else {
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

// Build the pending dimension from the current pick buffer + the live cursor. Returns true when the
// picks form a valid combo (and dimPending_/dimType_/dimMeasured_ are filled). cu,cv = cursor uv (for
// the h/v/aligned variant choice on point-point).
bool SketchMode::dimBuildPending(const ImVec2& cursor, float cu, float cv, bool keepAxis) {
    dimHasPending_ = false;
    if (!keepAxis) dimAxis_.clear();
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
            // horizontal span -> vertical; inside both -> aligned. keepAxis re-runs (Modify-box arc
            // condition change) leave the already-placed axis untouched.
            if (!keepAxis) {
                float ymin = std::fmin(av, bv), ymax = std::fmax(av, bv);
                float xmin = std::fmin(au, bu), xmax = std::fmax(au, bu);
                if (cv < ymin || cv > ymax) dimAxis_ = "h";
                else if (cu < xmin || cu > xmax) dimAxis_ = "v";
                else dimAxis_.clear();
            }
            // h/v is a DIRECTIONAL tangent-line distance: each circle's rim offset attaches to its
            // own centre coordinate (engine _add_distance h/v branch), so a concentric/nested
            // wall thickness is |(au+adjA) - (bu+adjB)| rather than |Δ| - rA - rB (which would
            // shove the circles apart). Aligned keeps the centre-line sum.
            double m;
            if (dimAxis_ == "h")      m = std::fabs((au + condAdj(a)) - (bu + condAdj(b)));
            else if (dimAxis_ == "v") m = std::fabs((av + condAdj(a)) - (bv + condAdj(b)));
            else m = std::hypot(bu - au, bv - av) + condAdj(a) + condAdj(b);
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
    // silently commit a fake "0" — the user must type a value (ui::sketch::parseSketchDim rejects empty). When there
    // IS a value, seed the plain number; for angles dimMeasured_ is already in DEGREES (set by
    // dimBuildPending), so fmtLen yields the correct degree number — the "°" unit is shown as the box
    // suffix label, NOT baked into the buffer (a trailing "°" would make ui::sketch::parseSketchDim reject the value).
    if (std::isfinite(dimMeasured_) && std::fabs(dimMeasured_) > 1e-9)
        std::snprintf(dimBuf_.data(), dimBuf_.size(), "%s", fmtLen(dimMeasured_).c_str());
    else
        dimBuf_[0] = '\0';
}

// C50f: apply a sketch_set_dim op (the shared EDIT-commit body), reload the mesh, re-solve, and
// tear down the Modify box. Used by the dimCommit edit path and by "Apply anyway" on the edit
// over-define modal.
void SketchMode::applyDimEditOp(SketchHost& st, const nlohmann::json& op) {
    auto r = st.docMirror().applyOps(st.worker(), nlohmann::json::array({op}));
    if (!r.ok) { st.toast(ui::sketch::ToastLevel::Error, "Dimension: " + r.error_message); dimError_ = r.error_message; return; }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);
    // C38 (S3): full pick-buffer cleanup. A double-click edit leaves stale picks otherwise, which
    // dimBuildPending consumes on the next frame.
    dimPopupOpen_ = false; dimEditCid_.clear();
    dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr;
    dimAxis_.clear(); dimPopupDriven_ = false;
}

// Commit the Modify box: parse value/expr, apply, and (on success) keep the tool armed.
void SketchMode::dimCommit(SketchHost& st) {
    std::string txt = dimBuf_.data();
    nlohmann::json con = dimPending_;
    bool isExpr = (!txt.empty() && txt[0] == '=');
    double mm = dimMeasured_;
    if (!isExpr) {
        std::string perr;
        // A bare value in the Modify box follows the document length unit (mm / inch); angles
        // stay degrees. An explicit suffix the user types still wins via ui::sketch::parseSketchDim.
        const double bareScale = (g_skIps && !dimIsAngle_) ? 25.4 : 1.0;
        if (!ui::sketch::parseSketchDim(txt, mm, perr, dimIsAngle_, bareScale)) { dimError_ = perr.empty() ? "bad value" : perr; return; }
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
void SketchMode::overDefCommit(SketchHost& st, bool driven) {
    nlohmann::json con = overDefPendingCon_;
    if (driven) con["driven"] = true;
    nlohmann::json ops = nlohmann::json::array({
        {{"op", "sketch_add_constraint"}, {"sketch", fid_}, {"constraint", con}}});
    auto r = st.docMirror().applyOps(st.worker(), ops);
    if (!r.ok) {
        // Over-define protection: surface the engine error, never silently drop.
        st.toast(ui::sketch::ToastLevel::Error, "Dimension: " + r.error_message);
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
void SketchMode::drawOverDefinePrompt(SketchHost& st) {
    if (!overDefOpen_) return;
    const auto& pal = skPal;
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
void SketchMode::drawEditOverDefinePrompt(SketchHost& st) {
    if (!editOverDefOpen_) return;
    const auto& pal = skPal;
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

void SketchMode::drawRelationOverDefinePrompt(SketchHost& st) {
    if (!relOverDefOpen_) return;
    const auto& pal = skPal;
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
void SketchMode::inSketchUndo(SketchHost& st) {
    if (tool_ != SketchTool::None && tool_ != SketchTool::SmartDim && !toolUV_.empty()) {
        toolUV_.pop_back();
        if (!toolSnaps_.empty()) toolSnaps_.pop_back();
        return;
    }
    st.docUndo();
    afterSketchHistory(st);
}

void SketchMode::inSketchRedo(SketchHost& st) {
    st.docRedo();
    afterSketchHistory(st);
}

// Shared post-undo/redo refresh: resync the mirror, re-solve the active sketch, or close cleanly
// if the sketch feature itself was undone/redone away.
void SketchMode::afterSketchHistory(SketchHost& st) {
    resyncMirror(st);
    // Does the sketch still exist? Re-solving an absent sketch returns an error envelope.
    nlohmann::json res;
    if (!callSketch(st, "sketch_solve", nlohmann::json::object(), res)) {
        st.toast(ui::sketch::ToastLevel::Info, "Sketch removed by undo — closing");
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


}  // namespace ui
