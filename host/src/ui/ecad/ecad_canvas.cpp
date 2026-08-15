// AI-ECAD UI scaffold (Wave D) — NOT yet build-verified; see docs/ECAD_UI_INTEGRATION.md
#include "ecad_canvas.h"

#include <algorithm>
#include <cmath>
#include <vector>

#include "../../ui_theme.h"
#include "../layout.h"   // ui::overlayColumnEdge — fit frames the board clear of the Copilot column

namespace ui {

namespace {

constexpr float kPi = 3.14159265358979323846f;

// Build an ImU32 from a palette role with an optional alpha multiply — same helper sketch_mode.cpp
// uses (host pattern: never hardcode colors; always route through ui::palette + Palette::toU32).
ImU32 col(const float role[4], float a = 1.0f) {
    float c[4] = {role[0], role[1], role[2], role[3] * a};
    return ui::Palette::toU32(c);
}

// Dashed line a->b in screen px (port of sketch_mode.cpp::drawDashedLine). dash/gap in px.
void drawDashedLine(ImDrawList* dl, ImVec2 a, ImVec2 b, ImU32 c, float th, float dash, float gap) {
    float dx = b.x - a.x, dy = b.y - a.y;
    float L = std::hypot(dx, dy);
    if (L < 1.0f) return;
    float step = dash + gap;
    for (float t = 0.0f; t < L; t += step) {
        float e = std::fmin(t + dash, L);
        dl->AddLine(ImVec2(a.x + dx * (t / L), a.y + dy * (t / L)),
                    ImVec2(a.x + dx * (e / L), a.y + dy * (e / L)), c, th);
    }
}

// Small arrowhead at `tip` pointing along (dx,dy) (port of sketch_mode.cpp::arrowHead).
void arrowHead(ImDrawList* dl, ImVec2 tip, float dx, float dy, float sz, ImU32 c) {
    float L = std::hypot(dx, dy);
    if (L < 1e-4f) return;
    dx /= L; dy /= L;
    const float nx = -dy, ny = dx;
    ImVec2 b1(tip.x - dx * sz + nx * sz * 0.5f, tip.y - dy * sz + ny * sz * 0.5f);
    ImVec2 b2(tip.x - dx * sz - nx * sz * 0.5f, tip.y - dy * sz - ny * sz * 0.5f);
    dl->AddTriangleFilled(tip, b1, b2, c);
}

// Distinct copper-layer colors keyed by copperIndex. Reuses palette axis/accent/status roles so the
// scheme tracks the active theme (F.Cu red-ish, B.Cu blue-ish — the KiCad muscle-memory mapping).
// New named layer roles could be appended to the Palette later (see ECAD_UI_INTEGRATION.md §(f)).
ImU32 copperColor(int copperIndex, float a) {
    const ui::Palette& p = ui::palette;
    switch (((copperIndex % 6) + 6) % 6) {
        case 0:  return col(p.axisX, a);       // F.Cu  — red
        case 1:  return col(p.axisZ, a);       // B.Cu / In1 — blue
        case 2:  return col(p.statusOk, a);    // teal
        case 3:  return col(p.statusWarn, a);  // amber
        case 4:  return col(p.axisY, a);       // green
        default: return col(p.statusErr, a);   // orange
    }
}

// Effective copper color for a specific layer: a per-layer override (picked in the PCB Manager's
// layer swatch) wins over the copperIndex-derived default, so a recolored layer repaints on the board.
ImU32 copperColorFor(const EcadMirror& doc, const std::string& layerId, int copperIndex, float a) {
    float ov[3];
    if (!layerId.empty() && doc.layerColor(layerId, ov)) {
        float c[4] = {ov[0], ov[1], ov[2], a};
        return ui::Palette::toU32(c);
    }
    return copperColor(copperIndex, a);
}

// Split a "<kind>:<id>" selection token into its parts. Returns false on an empty/malformed token.
bool splitSel(const std::string& sel, std::string& kind, std::string& id) {
    const auto c = sel.find(':');
    if (sel.empty() || c == std::string::npos) return false;
    kind = sel.substr(0, c);
    id   = sel.substr(c + 1);
    return !id.empty();
}

// Point-to-segment distance in world mm (used for wire/track hit-test + halo decisions).
float ptSegDist(const EcadPt& p, const EcadPt& a, const EcadPt& b) {
    const float vx = b.x - a.x, vy = b.y - a.y;
    const float wx = p.x - a.x, wy = p.y - a.y;
    const float vv = vx * vx + vy * vy;
    float t = (vv > 1e-9f) ? (wx * vx + wy * vy) / vv : 0.0f;
    t = std::max(0.0f, std::min(1.0f, t));
    const float cx = a.x + t * vx, cy = a.y + t * vy;
    return std::hypot(p.x - cx, p.y - cy);
}

}  // namespace

// ---- camera --------------------------------------------------------------------------------

ImVec2 EcadCanvas::toScreen(const EcadPt& w) const {
    const float cx = imgMin_.x + imgSize_.x * 0.5f;
    const float cy = imgMin_.y + imgSize_.y * 0.5f;
    return ImVec2(cx + (w.x - pan_.x) * pxPerMm_,
                  cy - (w.y - pan_.y) * pxPerMm_);  // +y up -> screen y down
}

EcadPt EcadCanvas::toWorld(const ImVec2& s) const {
    const float cx = imgMin_.x + imgSize_.x * 0.5f;
    const float cy = imgMin_.y + imgSize_.y * 0.5f;
    return EcadPt{pan_.x + (s.x - cx) / pxPerMm_,
                  pan_.y - (s.y - cy) / pxPerMm_};
}

void EcadCanvas::zoomToFit(const EcadMirror& doc, const ImVec2& avail) {
    EcadPt mn, mx;
    if (!doc.worldBounds(mn, mx)) {
        // Empty doc: center origin at a sane default zoom.
        pan_ = {0, 0};
        pxPerMm_ = 8.0f;
        inited_ = true;
        return;
    }
    const float w = std::max(1.0f, mx.x - mn.x);
    const float h = std::max(1.0f, mx.y - mn.y);
    pan_ = {(mn.x + mx.x) * 0.5f, (mn.y + mx.y) * 0.5f};
    const float pad = 1.10f;  // ~5% margin each side (fill the canvas)
    const float zx = avail.x / (w * pad);
    const float zy = avail.y / (h * pad);
    pxPerMm_ = std::max(0.25f, std::min(zx, zy));
    inited_ = true;
}

void EcadCanvas::focusOn(const EcadPt& w) {
    pan_ = w;                                  // pan so the point sits at the canvas centre
    if (pxPerMm_ < 14.0f) pxPerMm_ = 14.0f;    // zoom in to a readable level if currently far out
    wantFit_ = false;                          // don't let a pending fit clobber the pan
    inited_ = true;
    focusLoc_ = w;
    focusFrames_ = 180;                         // flash the marker for ~3s of frames
}

void EcadCanvas::handleNavigation(const EcadCanvasIO& io, const ImVec2& avail) {
    if (!io.hovered) return;
    ImGuiIO& gio = ImGui::GetIO();

    // Zoom about the cursor (sketch_mode keeps the world point under the cursor fixed across zoom).
    const float wheel = gio.MouseWheel;
    if (wheel != 0.0f && io.hasWorldMouse) {
        const ImVec2 sBefore = toScreen(io.worldMouse);
        const float factor = std::pow(1.15f, wheel);
        pxPerMm_ = std::max(0.1f, std::min(400.0f, pxPerMm_ * factor));
        const EcadPt wAfter = toWorld(sBefore);
        pan_.x += io.worldMouse.x - wAfter.x;
        pan_.y += io.worldMouse.y - wAfter.y;
    }

    // Middle-drag or space+left to pan (sketch_mode-style). Right-drag also pans.
    const bool panDrag = ImGui::IsMouseDragging(ImGuiMouseButton_Middle) ||
                         ImGui::IsMouseDragging(ImGuiMouseButton_Right);
    if (panDrag) {
        const ImVec2 d = ImGui::GetIO().MouseDelta;
        pan_.x -= d.x / pxPerMm_;
        pan_.y += d.y / pxPerMm_;  // screen down -> world down
    }
    (void)avail;
}

// ---- grid ----------------------------------------------------------------------------------

void EcadCanvas::drawGrid(const ImVec2& avail) {
    const ui::Palette& p = ui::palette;
    const ImU32 gridCol = col(p.sketchGrid);
    const ImU32 axisCol = col(p.sketchGrid, 0.55f);

    // Choose a grid spacing (mm) that stays readable at the current zoom (~14..60 px between lines).
    float stepMm = 1.0f;
    const float minPx = 14.0f;
    while (stepMm * pxPerMm_ < minPx) stepMm *= 10.0f;
    while (stepMm * pxPerMm_ > 120.0f) stepMm /= 10.0f;

    const EcadPt wTL = toWorld(imgMin_);
    const EcadPt wBR = toWorld(ImVec2(imgMin_.x + avail.x, imgMin_.y + avail.y));
    const float x0 = std::floor(wTL.x / stepMm) * stepMm;
    const float x1 = std::ceil(wBR.x / stepMm) * stepMm;
    const float y0 = std::floor(wBR.y / stepMm) * stepMm;
    const float y1 = std::ceil(wTL.y / stepMm) * stepMm;

    for (float x = x0; x <= x1; x += stepMm) {
        ImVec2 a = toScreen({x, wTL.y}), b = toScreen({x, wBR.y});
        const bool isAxis = std::fabs(x) < stepMm * 0.5f;
        dl_->AddLine(a, b, isAxis ? axisCol : gridCol, isAxis ? 1.4f : 1.0f);
    }
    for (float y = y0; y <= y1; y += stepMm) {
        ImVec2 a = toScreen({wTL.x, y}), b = toScreen({wBR.x, y});
        const bool isAxis = std::fabs(y) < stepMm * 0.5f;
        if (isAxis)
            drawDashedLine(dl_, a, b, axisCol, 1.0f, 5.0f, 5.0f);
        else
            dl_->AddLine(a, b, gridCol, 1.0f);
    }
}

// ---- schematic -----------------------------------------------------------------------------

void EcadCanvas::drawSch(const EcadMirror& doc) {
    const ui::Palette& p = ui::palette;
    // The canvas surface is the (light) viewport look in both themes — like the sketch canvas — so we
    // ink with sketchInk (dark), and use the under-defined blue / accent / status roles for accents.
    const ImU32 inkCol   = col(p.sketchInk);
    const ImU32 wireCol  = col(p.sketchInk, 0.85f);
    const ImU32 busCol   = col(p.sketchUnder);
    const ImU32 pinCol   = col(p.sketchUnder);
    const ImU32 netCol   = col(p.accentDim);
    const ImU32 pwrCol   = col(p.statusWarn);
    const ImU32 refCol   = col(p.accentDim);

    // Wires (orthogonal polylines). Bus wires drawn thicker in the under-defined blue.
    for (const auto& w : doc.wires()) {
        const ImU32 c = w.bus ? busCol : wireCol;
        const float th = w.bus ? 3.0f : 1.8f;
        for (size_t i = 1; i < w.pts.size(); ++i)
            dl_->AddLine(toScreen(w.pts[i - 1]), toScreen(w.pts[i]), c, th);
    }

    // Junction dots.
    for (const auto& j : doc.junctions())
        dl_->AddCircleFilled(toScreen(j.pos), 3.5f * ui::uiScale, wireCol, 12);

    // Symbols: body graphics (local-frame, transformed) + pin stubs + ref/value text.
    for (const auto& s : doc.symbols()) {
        const ImU32 bodyCol = s.power ? pwrCol : inkCol;
        for (const auto& g : s.body) {
            switch (g.kind) {
                case EcadSymGraphic::Rect: {
                    if (g.pts.size() < 2) break;
                    // local rect -> transform both corners; AABB-in-world fine for axis symbols, but
                    // rotated symbols need all 4 corners — draw as a 4-point polyline for correctness.
                    EcadPt c0 = g.pts[0], c1 = g.pts[1];
                    EcadPt corners[4] = {{c0.x, c0.y}, {c1.x, c0.y}, {c1.x, c1.y}, {c0.x, c1.y}};
                    ImVec2 sp[4];
                    for (int i = 0; i < 4; ++i)
                        sp[i] = toScreen(EcadMirror::localToWorld(corners[i], s.pos, s.angle, s.mirror));
                    for (int i = 0; i < 4; ++i)
                        dl_->AddLine(sp[i], sp[(i + 1) % 4], bodyCol, 1.8f);
                    break;
                }
                case EcadSymGraphic::Line: {
                    if (g.pts.size() < 2) break;
                    dl_->AddLine(toScreen(EcadMirror::localToWorld(g.pts[0], s.pos, s.angle, s.mirror)),
                                 toScreen(EcadMirror::localToWorld(g.pts[1], s.pos, s.angle, s.mirror)),
                                 bodyCol, 1.8f);
                    break;
                }
                case EcadSymGraphic::Circle:
                case EcadSymGraphic::Arc: {
                    if (g.pts.empty()) break;
                    const EcadPt cw = EcadMirror::localToWorld(g.pts[0], s.pos, s.angle, s.mirror);
                    const float rpx = g.radius * pxPerMm_;
                    if (g.kind == EcadSymGraphic::Circle) {
                        dl_->AddCircle(toScreen(cw), rpx, bodyCol, 32, 1.8f);
                    } else {
                        // Arc: tessellate (angles are doc-CCW; screen y is flipped so negate).
                        const int seg = 24;
                        ImVec2 prev;
                        for (int i = 0; i <= seg; ++i) {
                            const float a = (g.a0 + (g.a1 - g.a0) * (float)i / seg + s.angle) * kPi / 180.0f;
                            EcadPt wp{cw.x + g.radius * std::cos(a), cw.y + g.radius * std::sin(a)};
                            ImVec2 q = toScreen(wp);
                            if (i > 0) dl_->AddLine(prev, q, bodyCol, 1.8f);
                            prev = q;
                        }
                    }
                    break;
                }
                case EcadSymGraphic::Polyline: {
                    for (size_t i = 1; i < g.pts.size(); ++i)
                        dl_->AddLine(
                            toScreen(EcadMirror::localToWorld(g.pts[i - 1], s.pos, s.angle, s.mirror)),
                            toScreen(EcadMirror::localToWorld(g.pts[i],     s.pos, s.angle, s.mirror)),
                            bodyCol, 1.8f);
                    break;
                }
            }
        }

        // Pin stubs (root -> tip) + a small endpoint dot at the connection tip.
        for (const auto& pin : s.pins) {
            ImVec2 r = toScreen(pin.root), t = toScreen(pin.tip);
            dl_->AddLine(r, t, pinCol, 1.6f);
            dl_->AddCircleFilled(t, 2.5f * ui::uiScale, pinCol, 8);
        }

        // ref / value text near the symbol origin.
        const ImVec2 sp = toScreen(s.pos);
        if (!s.ref.empty())
            dl_->AddText(ImVec2(sp.x + 8, sp.y - 18), refCol, s.ref.c_str());
        if (!s.value.empty())
            dl_->AddText(ImVec2(sp.x + 8, sp.y + 6), inkCol, s.value.c_str());
    }

    // Net labels (text + a small flag tick).
    for (const auto& l : doc.labels()) {
        const ImVec2 sp = toScreen(l.pos);
        dl_->AddCircleFilled(sp, 2.5f * ui::uiScale, netCol, 8);
        dl_->AddText(ImVec2(sp.x + 6, sp.y - 8), netCol, l.text.c_str());
    }

    // Power ports: a stem + a bar glyph + the net name.
    for (const auto& pw : doc.powers()) {
        const ImVec2 sp = toScreen(pw.pos);
        const float r = 7.0f * ui::uiScale;
        dl_->AddLine(sp, ImVec2(sp.x, sp.y - r * 1.6f), pwrCol, 1.8f);          // stem (up)
        dl_->AddLine(ImVec2(sp.x - r, sp.y - r * 1.6f),
                     ImVec2(sp.x + r, sp.y - r * 1.6f), pwrCol, 1.8f);          // bar
        if (!pw.net.empty())
            dl_->AddText(ImVec2(sp.x + r + 2, sp.y - r * 1.6f - 8), pwrCol, pw.net.c_str());
    }

    // Selection highlight (drawn last so it sits on top). A symbol gets a translucent halo disk +
    // a thicker ring around its origin; a wire gets a thick translucent over-stroke per segment.
    std::string skind, sid;
    if (splitSel(selected_, skind, sid)) {
        const ImU32 halo = col(p.selectionHighlight);
        const ImU32 ring = col(p.hoverHighlight, 2.2f);
        if (skind == "s") {
            for (const auto& s : doc.symbols()) {
                if (s.id != sid) continue;
                const EcadPt eff{s.pos.x + dragDx_, s.pos.y + dragDy_};
                const ImVec2 sp = toScreen(eff);
                const float rad = 16.0f * ui::uiScale;
                dl_->AddCircleFilled(sp, rad, halo, 24);
                dl_->AddCircle(sp, rad, ring, 24, 2.4f);
                break;
            }
        } else if (skind == "w") {
            for (const auto& w : doc.wires()) {
                if (w.id != sid) continue;
                for (size_t i = 1; i < w.pts.size(); ++i)
                    dl_->AddLine(toScreen(w.pts[i - 1]), toScreen(w.pts[i]), halo, 6.0f);
                break;
            }
        }
    }
}

// ---- board ---------------------------------------------------------------------------------

void EcadCanvas::drawPcb(const EcadMirror& doc) {
    const ui::Palette& p = ui::palette;
    const ImU32 outlineCol = col(p.statusWarn);          // Edge.Cuts — amber/yellow (KiCad-ish)
    const ImU32 courtCol   = col(p.textDim, 0.7f);       // courtyard — faint
    const ImU32 ratsCol    = col(p.sketchInfer);         // ratsnest — amber dashed
    const ImU32 drillCol   = col(p.windowBg);            // drill hole — dark plug
    const ImU32 viaRing    = col(p.text, 0.9f);
    const ImU32 refCol     = col(p.text, 0.85f);

    // Board outline (Edge.Cuts).
    for (const auto& o : doc.outline())
        dl_->AddLine(toScreen(o.p1), toScreen(o.p2), outlineCol, 1.8f);

    // Zones (filled translucent + outline), per copper layer color.
    for (const auto& z : doc.zones()) {
        if (!doc.layerVisible(z.layer)) continue;
        int ci = 0;
        for (const auto& ly : doc.layers()) if (ly.id == z.layer) { ci = ly.copperIndex; break; }
        const ImU32 fill = copperColorFor(doc, z.layer, ci, 0.14f);
        const ImU32 edge = copperColorFor(doc, z.layer, ci, 0.6f);
        std::vector<ImVec2> poly;
        poly.reserve(z.polygon.size());
        for (const auto& pt : z.polygon) poly.push_back(toScreen(pt));
        if (poly.size() >= 3) {
            dl_->AddConvexPolyFilled(poly.data(), (int)poly.size(), fill);
            for (size_t i = 0; i < poly.size(); ++i)
                dl_->AddLine(poly[i], poly[(i + 1) % poly.size()], edge, 1.2f);
        }
    }

    // Copper tracks, drawn back-to-front so the highest copperIndex (B.Cu) paints first and F.Cu
    // ends up on top — iterate the layer list in reverse copper order.
    auto layersCopy = doc.layers();
    std::sort(layersCopy.begin(), layersCopy.end(),
              [](const EcadLayer& a, const EcadLayer& b) { return a.copperIndex > b.copperIndex; });
    for (const auto& ly : layersCopy) {
        if (!doc.layerVisible(ly.id)) continue;
        const bool dimLayer = !highlightedNet_.empty();
        const ImU32 c = copperColorFor(doc, ly.id, ly.copperIndex,
                                       dimLayer ? 0.24f : 0.92f);
        for (const EcadTrack* t : doc.tracksOnLayer(ly.id)) {
            if (!doc.netVisible(t->net)) continue;
            const float wpx = std::max(1.0f, t->width * pxPerMm_);
            if (t->kind == "arc") {
                const int seg = 24;
                const float r = std::hypot(t->p1.x - t->center.x, t->p1.y - t->center.y);
                ImVec2 prev;
                for (int i = 0; i <= seg; ++i) {
                    const float a = (t->a0 + (t->a1 - t->a0) * (float)i / seg) * kPi / 180.0f;
                    EcadPt wp{t->center.x + r * std::cos(a), t->center.y + r * std::sin(a)};
                    ImVec2 q = toScreen(wp);
                    if (i > 0) dl_->AddLine(prev, q, c, wpx);
                    prev = q;
                }
            } else {
                dl_->AddLine(toScreen(t->p1), toScreen(t->p2), c, wpx);
            }
        }
    }

    // Footprints: courtyard + pads (per-layer color) + drill holes + ref text.
    for (const auto& fp : doc.footprints()) {
        if (fp.hasCourtyard) {
            drawDashedLine(dl_, toScreen({fp.courtyardMin.x, fp.courtyardMax.y}),
                           toScreen({fp.courtyardMax.x, fp.courtyardMax.y}), courtCol, 1.0f, 4, 3);
            drawDashedLine(dl_, toScreen({fp.courtyardMax.x, fp.courtyardMax.y}),
                           toScreen({fp.courtyardMax.x, fp.courtyardMin.y}), courtCol, 1.0f, 4, 3);
            drawDashedLine(dl_, toScreen({fp.courtyardMax.x, fp.courtyardMin.y}),
                           toScreen({fp.courtyardMin.x, fp.courtyardMin.y}), courtCol, 1.0f, 4, 3);
            drawDashedLine(dl_, toScreen({fp.courtyardMin.x, fp.courtyardMin.y}),
                           toScreen({fp.courtyardMin.x, fp.courtyardMax.y}), courtCol, 1.0f, 4, 3);
        }
        for (const auto& pad : fp.pads) {
            // Pad color: the topmost copper layer it sits on (F.Cu if present, else first layer).
            int ci = 0;
            std::string cLid;
            bool found = false;
            for (const auto& lid : pad.layers) {
                for (const auto& ly : doc.layers()) {
                    if (ly.id == lid && doc.layerVisible(lid)) { ci = ly.copperIndex; cLid = lid; found = true; break; }
                }
                if (found) break;
            }
            if (!found && !pad.layers.empty() && !doc.layerVisible(pad.layers.front())) continue;
            const bool padHighlighted =
                !highlightedNet_.empty() && pad.net == highlightedNet_;
            const float padAlpha =
                highlightedNet_.empty() ? 0.95f : (padHighlighted ? 1.0f : 0.22f);
            const ImU32 padCol =
                padHighlighted ? col(p.selectionHighlight)
                               : copperColorFor(doc, cLid, ci, padAlpha);
            const ImVec2 sp = toScreen(pad.pos);
            const float hw = std::max(2.0f, pad.w * 0.5f * pxPerMm_);
            const float hh = std::max(2.0f, pad.h * 0.5f * pxPerMm_);
            if (pad.shape == "circle" || pad.shape == "oval") {
                dl_->AddCircleFilled(sp, std::max(hw, hh), padCol, 20);
            } else {
                const float rr = (pad.shape == "roundrect") ? std::min(hw, hh) * pad.roundrectRatio * 2.0f : 0.0f;
                dl_->AddRectFilled(ImVec2(sp.x - hw, sp.y - hh), ImVec2(sp.x + hw, sp.y + hh), padCol, rr);
            }
            // Drill hole (through-hole pads): a dark plug.
            if (pad.hasDrill && pad.drillDia > 0.0f)
                dl_->AddCircleFilled(sp, std::max(1.5f, pad.drillDia * 0.5f * pxPerMm_), drillCol, 16);
        }
        const ImVec2 fps = toScreen(fp.pos);
        if (!fp.ref.empty())
            dl_->AddText(ImVec2(fps.x + 4, fps.y - 16), refCol, fp.ref.c_str());
    }

    // Vias: annular ring + drill plug. Net selection uses the same selection
    // tone as entity selection and dims unrelated copper.
    for (const auto& v : doc.vias()) {
        const ImVec2 sp = toScreen(v.at);
        const bool hi = !highlightedNet_.empty() && v.net == highlightedNet_;
        const ImU32 ring = hi ? col(p.selectionHighlight)
                              : (highlightedNet_.empty() ? viaRing
                                                        : col(p.text, 0.22f));
        dl_->AddCircleFilled(sp, std::max(2.0f, v.diameter * 0.5f * pxPerMm_), ring, 20);
        dl_->AddCircleFilled(sp, std::max(1.0f, v.drill * 0.5f * pxPerMm_), drillCol, 16);
    }

    // Ratsnest airwires: thin dashed lines between unrouted pads.
    for (const auto& aw : doc.ratsnest()) {
        if (!doc.netVisible(aw.net)) continue;
        drawDashedLine(dl_, toScreen(aw.p1), toScreen(aw.p2), ratsCol, 1.2f, 5, 4);
    }

    if (!highlightedNet_.empty()) {
        const ImU32 hi = col(p.selectionHighlight);
        for (const auto& t : doc.tracks()) {
            if (t.net != highlightedNet_ || !doc.layerVisible(t.layer) ||
                !doc.netVisible(t.net))
                continue;
            const float wpx = std::max(3.0f, t.width * pxPerMm_ + 2.0f);
            dl_->AddLine(toScreen(t.p1), toScreen(t.p2), hi, wpx);
        }
    }

    // Selection highlight (drawn last, on top). A footprint gets a translucent halo over its
    // courtyard (or origin) + a ring; a track gets a thick translucent over-stroke.
    std::string skind, sid;
    if (splitSel(selected_, skind, sid)) {
        const ImU32 halo = col(ui::palette.selectionHighlight);
        const ImU32 ring = col(ui::palette.hoverHighlight, 2.2f);
        if (skind == "fp") {
            for (const auto& fp : doc.footprints()) {
                if (fp.id != sid) continue;
                const EcadPt eff{fp.pos.x + dragDx_, fp.pos.y + dragDy_};
                const ImVec2 sp = toScreen(eff);
                // Halo radius from the courtyard half-diagonal when present, else a default body.
                float radMm = 1.5f;
                if (fp.hasCourtyard) {
                    radMm = 0.5f * std::hypot(fp.courtyardMax.x - fp.courtyardMin.x,
                                              fp.courtyardMax.y - fp.courtyardMin.y);
                }
                const float rad = std::max(14.0f * ui::uiScale, radMm * pxPerMm_);
                dl_->AddCircleFilled(sp, rad, halo, 28);
                dl_->AddCircle(sp, rad, ring, 28, 2.4f);
                break;
            }
        } else if (skind == "t") {
            for (const auto& t : doc.tracks()) {
                if (t.id != sid) continue;
                const float wpx = std::max(6.0f, t.width * pxPerMm_ + 4.0f);
                dl_->AddLine(toScreen(t.p1), toScreen(t.p2), halo, wpx);
                break;
            }
        }
    }
}

// ---- main draw -----------------------------------------------------------------------------

EcadCanvasIO EcadCanvas::draw(const EcadMirror& doc, const ImVec2& avail) {
    EcadCanvasIO io;
    dl_ = ImGui::GetWindowDrawList();
    imgMin_ = ImGui::GetCursorScreenPos();
    imgSize_ = avail;

    // First-frame / requested fit. Fit to the VISIBLE canvas, not the full region: the pinned
    // Copilot column floats over this canvas's right side, so fitting to `avail` framed the
    // board partly underneath it (its right Edge.Cuts edge vanished in the ecad capture).
    if (!inited_ || wantFit_) {
        ImVec2 visible = avail;
        float hiddenPx = 0.0f;
        const float copilotEdge = ui::overlayColumnEdge(1);
        if (copilotEdge > imgMin_.x) {
            visible.x = std::max(64.0f, std::min(visible.x, copilotEdge - imgMin_.x));
            hiddenPx = avail.x - visible.x;
        }
        zoomToFit(doc, visible);
        // toWorld/toScreen centre on the FULL region, so shrinking the zoom alone still parks the
        // board's middle under the column. Push the pan so the board's centre lands on the centre
        // of the visible strip instead.
        if (hiddenPx > 0.0f && pxPerMm_ > 0.0f) pan_.x += (hiddenPx * 0.5f) / pxPerMm_;
        wantFit_ = false;
    }

    // Background fill (the light "studio" canvas, same idea as the 3D viewport in both themes).
    dl_->AddRectFilled(imgMin_, ImVec2(imgMin_.x + avail.x, imgMin_.y + avail.y),
                       col(ui::palette.viewportBottom));
    dl_->PushClipRect(imgMin_, ImVec2(imgMin_.x + avail.x, imgMin_.y + avail.y), true);

    // An invisible button captures hover + drag without stealing the draw list.
    ImGui::InvisibleButton("##ecadCanvas", avail,
                           ImGuiButtonFlags_MouseButtonLeft | ImGuiButtonFlags_MouseButtonRight |
                           ImGuiButtonFlags_MouseButtonMiddle);
    io.hovered = ImGui::IsItemHovered();
    const ImVec2 m = ImGui::GetIO().MousePos;
    if (io.hovered) {
        io.worldMouse = toWorld(m);
        io.hasWorldMouse = true;
        io.clicked = ImGui::IsItemDeactivated() && !ImGui::IsMouseDragging(ImGuiMouseButton_Left);
    }

    handleNavigation(io, avail);

    drawGrid(avail);
    if (doc.isSch())      drawSch(doc);
    else if (doc.isPcb()) drawPcb(doc);

    // "Zoom to violation" marker: a pulsing crosshair-ring at focusLoc_ for a few seconds after a
    // violation row is clicked, so the eye lands on the exact reported location.
    if (focusFrames_ > 0) {
        const ImVec2 s = toScreen(focusLoc_);
        const float phase = static_cast<float>(180 - focusFrames_);
        const float r = 11.0f + 5.0f * std::sin(phase * 0.18f);  // px, gentle pulse
        const ImU32 c = col(ui::palette.statusErr, 0.95f);
        dl_->AddCircle(s, r, c, 28, 2.5f);
        dl_->AddLine(ImVec2(s.x - r - 5, s.y), ImVec2(s.x + r + 5, s.y), c, 1.5f);
        dl_->AddLine(ImVec2(s.x, s.y - r - 5), ImVec2(s.x, s.y + r + 5), c, 1.5f);
        focusFrames_--;
    }

    dl_->PopClipRect();
    dl_ = nullptr;
    return io;
}

}  // namespace ui
