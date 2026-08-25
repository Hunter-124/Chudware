#include "ecad_canvas.h"

#include <algorithm>
#include <cmath>
#include <vector>

#include "../../ui_theme.h"
#include "../layout.h"   // ui::overlayColumnEdge — fit frames the board clear of the Copilot column
#include "../metrics.h"

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


// Copper-layer colors reuse palette axis/accent/status roles so they follow the active theme.
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

float niceGridStep(float pxPerMm, float targetPx) {
    const float raw = targetPx / std::max(pxPerMm, 1e-6f);
    const float decade = std::pow(10.0f, std::floor(std::log10(raw)));
    const float normalized = raw / decade;
    const float multiple = normalized <= 1.0f ? 1.0f :
                           normalized <= 2.0f ? 2.0f :
                           normalized <= 5.0f ? 5.0f : 10.0f;
    return multiple * decade;
}

float zoomStroke(float pxPerMm, float worldMm, float minLogical, float maxLogical) {
    return std::clamp(worldMm * pxPerMm, ui::metrics::stroke(minLogical),
                      ui::metrics::px(maxLogical));
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
    const ImU32 dotCol   = col(p.sketchGrid, 0.72f);
    const ImU32 majorCol = col(p.sketchGrid, 1.9f);
    const ImU32 xAxisCol = col(p.axisX, 0.28f);
    const ImU32 yAxisCol = col(p.axisY, 0.28f);

    // A 1/2/5 adaptive cadence keeps dots legible without exploding the draw count at either
    // camera extreme. Major divisions remain lines so board scale is readable at a glance.
    float stepMm = niceGridStep(pxPerMm_, ui::metrics::U(5.0f));
    const EcadPt wTL = toWorld(imgMin_);
    const EcadPt wBR = toWorld(ImVec2(imgMin_.x + avail.x, imgMin_.y + avail.y));
    const float minX = std::min(wTL.x, wBR.x);
    const float maxX = std::max(wTL.x, wBR.x);
    const float minY = std::min(wTL.y, wBR.y);
    const float maxY = std::max(wTL.y, wBR.y);

    int ix0 = 0, ix1 = 0, iy0 = 0, iy1 = 0;
    for (;;) {
        ix0 = static_cast<int>(std::floor(minX / stepMm));
        ix1 = static_cast<int>(std::ceil(maxX / stepMm));
        iy0 = static_cast<int>(std::floor(minY / stepMm));
        iy1 = static_cast<int>(std::ceil(maxY / stepMm));
        const long long dotCount =
            static_cast<long long>(ix1 - ix0 + 1) * static_cast<long long>(iy1 - iy0 + 1);
        if (dotCount <= 32000) break;
        stepMm *= 2.0f;
    }

    constexpr int kMajorEvery = 5;
    constexpr int kDotSegments = 6;
    constexpr int kOriginSegments = 12;
    const float majorTh = ui::metrics::stroke(1.0f);
    for (int ix = ix0; ix <= ix1; ++ix) {
        if (ix == 0 || ix % kMajorEvery != 0) continue;
        const float x = static_cast<float>(ix) * stepMm;
        dl_->AddLine(toScreen({x, maxY}), toScreen({x, minY}), majorCol, majorTh);
    }
    for (int iy = iy0; iy <= iy1; ++iy) {
        if (iy == 0 || iy % kMajorEvery != 0) continue;
        const float y = static_cast<float>(iy) * stepMm;
        dl_->AddLine(toScreen({minX, y}), toScreen({maxX, y}), majorCol, majorTh);
    }

    const float dotRadius = ui::metrics::px(0.72f);
    for (int iy = iy0; iy <= iy1; ++iy) {
        const float y = static_cast<float>(iy) * stepMm;
        for (int ix = ix0; ix <= ix1; ++ix) {
            const float x = static_cast<float>(ix) * stepMm;
            dl_->AddCircleFilled(toScreen({x, y}), dotRadius, dotCol, kDotSegments);
        }
    }

    if (minY <= 0.0f && maxY >= 0.0f)
        dl_->AddLine(toScreen({minX, 0.0f}), toScreen({maxX, 0.0f}), xAxisCol,
                     ui::metrics::stroke(1.4f));
    if (minX <= 0.0f && maxX >= 0.0f)
        dl_->AddLine(toScreen({0.0f, maxY}), toScreen({0.0f, minY}), yAxisCol,
                     ui::metrics::stroke(1.4f));

    // Compact origin cue: it is clipped away when the origin is off-screen and never captures input.
    if (minX <= 0.0f && maxX >= 0.0f && minY <= 0.0f && maxY >= 0.0f) {
        const ImVec2 origin = toScreen({0.0f, 0.0f});
        const float arm = ui::metrics::U(1.5f);
        dl_->AddLine(origin, ImVec2(origin.x + arm, origin.y), xAxisCol,
                     ui::metrics::stroke(1.8f));
        dl_->AddLine(origin, ImVec2(origin.x, origin.y - arm), yAxisCol,
                     ui::metrics::stroke(1.8f));
        dl_->AddCircle(origin, ui::metrics::px(2.5f), col(p.sketchInk, 0.58f), kOriginSegments,
                       ui::metrics::stroke(1.0f));
    }
}

// ---- schematic -----------------------------------------------------------------------------

void EcadCanvas::drawSch(const EcadMirror& doc) {
    const ui::Palette& p = ui::palette;
    const ImU32 inkCol   = col(p.sketchInk);
    const ImU32 wireCol  = col(p.sketchInk, 0.86f);
    const ImU32 busCol   = col(p.sketchUnder);
    const ImU32 pinCol   = col(p.sketchUnder);
    const ImU32 netCol   = col(p.accentDim);
    const ImU32 pwrCol   = col(p.statusWarn);
    const ImU32 refCol   = col(p.accentDim);

    const float bodyStroke = zoomStroke(pxPerMm_, 0.12f, 1.35f, 2.6f);
    const float pinStroke  = zoomStroke(pxPerMm_, 0.10f, 1.2f, 2.3f);
    const float wireStroke = zoomStroke(pxPerMm_, 0.10f, 1.25f, 2.5f);
    const float busStroke  = zoomStroke(pxPerMm_, 0.22f, 2.4f, 4.4f);

    std::string selectedKind, selectedId;
    const bool hasSelection = splitSel(selected_, selectedKind, selectedId);

    auto drawSymbolGeometry =
        [this](const EcadSymbol& s, const EcadPt& offset, ImU32 bodyColor, ImU32 pinColor,
               float bodyTh, float pinTh, bool drawPinEnds) {
            constexpr int kCircleSegments = 40;
            constexpr int kPinSegments = 10;
            const EcadPt placed{s.pos.x + offset.x, s.pos.y + offset.y};
            for (const auto& g : s.body) {
                switch (g.kind) {
                    case EcadSymGraphic::Rect: {
                        if (g.pts.size() < 2) break;
                        const EcadPt c0 = g.pts[0], c1 = g.pts[1];
                        const EcadPt corners[4] = {
                            {c0.x, c0.y}, {c1.x, c0.y}, {c1.x, c1.y}, {c0.x, c1.y}};
                        ImVec2 screen[4];
                        for (int i = 0; i < 4; ++i)
                            screen[i] = toScreen(
                                EcadMirror::localToWorld(corners[i], placed, s.angle, s.mirror));
                        for (int i = 0; i < 4; ++i)
                            dl_->AddLine(screen[i], screen[(i + 1) % 4], bodyColor, bodyTh);
                        break;
                    }
                    case EcadSymGraphic::Line: {
                        if (g.pts.size() < 2) break;
                        dl_->AddLine(
                            toScreen(EcadMirror::localToWorld(g.pts[0], placed, s.angle, s.mirror)),
                            toScreen(EcadMirror::localToWorld(g.pts[1], placed, s.angle, s.mirror)),
                            bodyColor, bodyTh);
                        break;
                    }
                    case EcadSymGraphic::Circle: {
                        if (g.pts.empty()) break;
                        const EcadPt center =
                            EcadMirror::localToWorld(g.pts[0], placed, s.angle, s.mirror);
                        dl_->AddCircle(toScreen(center), g.radius * pxPerMm_, bodyColor,
                                       kCircleSegments, bodyTh);
                        break;
                    }
                    case EcadSymGraphic::Arc: {
                        if (g.pts.empty()) break;
                        const float sweep = g.a1 - g.a0;
                        const int segments = std::clamp(
                            static_cast<int>(std::ceil(std::fabs(sweep) * kPi / 180.0f *
                                                       g.radius * pxPerMm_ /
                                                       ui::metrics::U(1.0f))),
                            12, 96);
                        ImVec2 prev;
                        for (int i = 0; i <= segments; ++i) {
                            const float a =
                                (g.a0 + sweep * static_cast<float>(i) / segments) * kPi / 180.0f;
                            const EcadPt local{g.pts[0].x + g.radius * std::cos(a),
                                              g.pts[0].y + g.radius * std::sin(a)};
                            const ImVec2 q = toScreen(
                                EcadMirror::localToWorld(local, placed, s.angle, s.mirror));
                            if (i > 0) dl_->AddLine(prev, q, bodyColor, bodyTh);
                            prev = q;
                        }
                        break;
                    }
                    case EcadSymGraphic::Polyline: {
                        for (size_t i = 1; i < g.pts.size(); ++i)
                            dl_->AddLine(
                                toScreen(EcadMirror::localToWorld(g.pts[i - 1], placed, s.angle,
                                                                  s.mirror)),
                                toScreen(EcadMirror::localToWorld(g.pts[i], placed, s.angle,
                                                                  s.mirror)),
                                bodyColor, bodyTh);
                        break;
                    }
                }
            }

            for (const auto& pin : s.pins) {
                const EcadPt root{pin.root.x + offset.x, pin.root.y + offset.y};
                const EcadPt tip{pin.tip.x + offset.x, pin.tip.y + offset.y};
                const ImVec2 r = toScreen(root), t = toScreen(tip);
                dl_->AddLine(r, t, pinColor, pinTh);
                if (!drawPinEnds) continue;
                const float radius = ui::metrics::px(pin.net.empty() ? 2.4f : 2.0f);
                if (pin.net.empty())
                    dl_->AddCircle(t, radius, pinColor, kPinSegments,
                                   ui::metrics::stroke(1.0f));
                else
                    dl_->AddCircleFilled(t, radius, pinColor, kPinSegments);
            }
        };

    // Wires sit below symbols, matching schematic-editor convention.
    for (const auto& w : doc.wires()) {
        const ImU32 c = w.bus ? busCol : wireCol;
        const float th = w.bus ? busStroke : wireStroke;
        for (size_t i = 1; i < w.pts.size(); ++i)
            dl_->AddLine(toScreen(w.pts[i - 1]), toScreen(w.pts[i]), c, th);
    }

    // Junctions scale with the drawing until their screen-space cap, so they stay distinct from pins.
    const float junctionRadius =
        std::clamp(0.22f * pxPerMm_, ui::metrics::px(2.5f), ui::metrics::px(4.4f));
    for (const auto& j : doc.junctions())
        dl_->AddCircleFilled(toScreen(j.pos), junctionRadius, wireCol, 16);

    for (const auto& s : doc.symbols()) {
        const bool selectedSymbol =
            hasSelection && selectedKind == "s" && selectedId == s.id;
        const EcadPt offset =
            selectedSymbol ? EcadPt{dragDx_, dragDy_} : EcadPt{0.0f, 0.0f};
        drawSymbolGeometry(s, offset, s.power ? pwrCol : inkCol, pinCol, bodyStroke, pinStroke,
                           true);

        const ImVec2 sp = toScreen({s.pos.x + offset.x, s.pos.y + offset.y});
        if (!s.ref.empty())
            dl_->AddText(ImVec2(sp.x + ui::metrics::U(2.0f),
                                sp.y - ui::metrics::U(4.5f)),
                         refCol, s.ref.c_str());
        if (!s.value.empty())
            dl_->AddText(ImVec2(sp.x + ui::metrics::U(2.0f),
                                sp.y + ui::metrics::U(1.5f)),
                         inkCol, s.value.c_str());
    }

    // Net labels use an angle-aware flag leader instead of an ambiguous standalone dot.
    for (const auto& label : doc.labels()) {
        const ImVec2 sp = toScreen(label.pos);
        const float a = label.angle * kPi / 180.0f;
        const ImVec2 dir(std::cos(a), -std::sin(a));
        const ImVec2 normal(-dir.y, dir.x);
        const float flagLen = ui::metrics::U(1.5f);
        const ImVec2 end(sp.x + dir.x * flagLen, sp.y + dir.y * flagLen);
        dl_->AddLine(sp, end, netCol, ui::metrics::stroke(1.5f));
        dl_->AddLine(end, ImVec2(end.x + normal.x * ui::metrics::U(0.75f),
                                end.y + normal.y * ui::metrics::U(0.75f)),
                     netCol, ui::metrics::stroke(1.2f));
        dl_->AddCircleFilled(sp, ui::metrics::px(1.8f), netCol, 8);
        dl_->AddText(ImVec2(end.x + ui::metrics::U(0.75f),
                            end.y - ui::metrics::U(2.0f)),
                     netCol, label.text.c_str());
    }

    for (const auto& power : doc.powers()) {
        const ImVec2 sp = toScreen(power.pos);
        const float r = ui::metrics::U(1.75f);
        dl_->AddLine(sp, ImVec2(sp.x, sp.y - r * 1.6f), pwrCol, bodyStroke);
        dl_->AddLine(ImVec2(sp.x - r, sp.y - r * 1.6f),
                     ImVec2(sp.x + r, sp.y - r * 1.6f), pwrCol, bodyStroke);
        if (!power.net.empty())
            dl_->AddText(ImVec2(sp.x + r + ui::metrics::U(0.5f),
                                sp.y - r * 1.6f - ui::metrics::U(2.0f)),
                         pwrCol, power.net.c_str());
    }

    // The mirror does not expose a separate no-connect primitive, but ERC locations do. Mark only
    // reported no-connects, avoiding the false implication that every intentionally floating pin is
    // an error.
    for (const auto& violation : doc.erc()) {
        if (!violation.hasLoc ||
            (violation.rule.find("not_connected") == std::string::npos &&
             violation.rule.find("no_connect") == std::string::npos))
            continue;
        const ImVec2 q = toScreen(violation.loc);
        const float r = ui::metrics::U(0.9f);
        const ImU32 cue =
            violation.severity == "error" ? col(p.statusErr) : col(p.statusWarn);
        dl_->AddLine(ImVec2(q.x - r, q.y - r), ImVec2(q.x + r, q.y + r), cue,
                     ui::metrics::stroke(1.5f));
        dl_->AddLine(ImVec2(q.x - r, q.y + r), ImVec2(q.x + r, q.y - r), cue,
                     ui::metrics::stroke(1.5f));
    }

    if (!hasSelection) return;
    const ImU32 selectionOuter = col(p.selectionHighlight, 0.62f);
    const ImU32 selectionInner = col(p.selectionHighlight, 1.8f);
    if (selectedKind == "s") {
        for (const auto& s : doc.symbols()) {
            if (s.id != selectedId) continue;
            const EcadPt offset{dragDx_, dragDy_};
            drawSymbolGeometry(s, offset, selectionOuter, selectionOuter,
                               bodyStroke + ui::metrics::px(4.0f),
                               pinStroke + ui::metrics::px(4.0f), false);
            drawSymbolGeometry(s, offset, selectionInner, selectionInner,
                               bodyStroke + ui::metrics::px(1.0f),
                               pinStroke + ui::metrics::px(1.0f), false);
            break;
        }
    } else if (selectedKind == "w") {
        for (const auto& w : doc.wires()) {
            if (w.id != selectedId) continue;
            const float base = w.bus ? busStroke : wireStroke;
            for (size_t i = 1; i < w.pts.size(); ++i) {
                const ImVec2 a = toScreen(w.pts[i - 1]), b = toScreen(w.pts[i]);
                dl_->AddLine(a, b, selectionOuter, base + ui::metrics::px(4.0f));
                dl_->AddLine(a, b, selectionInner, base + ui::metrics::px(1.0f));
            }
            break;
        }
    }
}

// ---- board ---------------------------------------------------------------------------------

void EcadCanvas::drawPcb(const EcadMirror& doc) {
    const ui::Palette& p = ui::palette;
    const ImU32 outlineCol = col(p.statusWarn);
    const ImU32 courtCol   = col(p.textDim, 0.70f);
    const ImU32 drillCol   = col(p.windowBg);
    const ImU32 refCol     = col(p.text, 0.85f);

    std::string selectedKind, selectedId;
    const bool hasSelection = splitSel(selected_, selectedKind, selectedId);

    auto trackStroke = [this](const EcadTrack& track, float extra) {
        return std::max(ui::metrics::stroke(1.0f), track.width * pxPerMm_) + extra;
    };

    // Every track pass goes through this path, so arcs cannot regress to endpoint chords when
    // highlighted or selected.
    auto drawTrack = [this](const EcadTrack& track, ImU32 color, float widthPx) {
        if (track.kind != "arc") {
            dl_->AddLine(toScreen(track.p1), toScreen(track.p2), color, widthPx);
            return;
        }
        const float radius =
            std::hypot(track.p1.x - track.center.x, track.p1.y - track.center.y);
        const float sweep = track.a1 - track.a0;
        if (radius <= 1e-6f || std::fabs(sweep) <= 1e-5f) return;
        const int segments = std::clamp(
            static_cast<int>(std::ceil(std::fabs(sweep) * kPi / 180.0f * radius *
                                       pxPerMm_ / ui::metrics::U(1.0f))),
            12, 256);
        ImVec2 prev;
        for (int i = 0; i <= segments; ++i) {
            const float a =
                (track.a0 + sweep * static_cast<float>(i) / segments) * kPi / 180.0f;
            const ImVec2 q = toScreen({track.center.x + radius * std::cos(a),
                                       track.center.y + radius * std::sin(a)});
            if (i > 0) dl_->AddLine(prev, q, color, widthPx);
            prev = q;
        }
    };

    auto footprintOffset = [&](const EcadFootprint& fp) {
        if (hasSelection && selectedKind == "fp" && selectedId == fp.id)
            return EcadPt{dragDx_, dragDy_};
        return EcadPt{0.0f, 0.0f};
    };

    auto drawCourtyard = [this](const EcadFootprint& fp, const EcadPt& offset, ImU32 color) {
        const EcadPt mn{fp.courtyardMin.x + offset.x, fp.courtyardMin.y + offset.y};
        const EcadPt mx{fp.courtyardMax.x + offset.x, fp.courtyardMax.y + offset.y};
        const float dash = ui::metrics::U(1.0f);
        const float gap = ui::metrics::U(0.75f);
        const float th = ui::metrics::stroke(1.0f);
        drawDashedLine(dl_, toScreen({mn.x, mx.y}), toScreen({mx.x, mx.y}), color, th, dash, gap);
        drawDashedLine(dl_, toScreen({mx.x, mx.y}), toScreen({mx.x, mn.y}), color, th, dash, gap);
        drawDashedLine(dl_, toScreen({mx.x, mn.y}), toScreen({mn.x, mn.y}), color, th, dash, gap);
        drawDashedLine(dl_, toScreen({mn.x, mn.y}), toScreen({mn.x, mx.y}), color, th, dash, gap);
    };

    // Pads use a fixed-size convex path. Rectangles, rounded rectangles, circles, and ovals all
    // share the same rotation transform; no per-pad heap allocation is required.
    auto drawPadShape =
        [this](const EcadPad& pad, const EcadPt& center, ImU32 fill, ImU32 edge, float edgeTh) {
            float halfW =
                std::max(pad.w * 0.5f, ui::metrics::px(1.5f) / std::max(pxPerMm_, 1e-6f));
            float halfH =
                std::max(pad.h * 0.5f, ui::metrics::px(1.5f) / std::max(pxPerMm_, 1e-6f));
            if (pad.shape == "circle") {
                halfW = halfH = std::max(halfW, halfH);
            }

            float radius = 0.0f;
            if (pad.shape == "oval" || pad.shape == "circle")
                radius = std::min(halfW, halfH);
            else if (pad.shape == "roundrect")
                radius = std::min(halfW, halfH) *
                         std::clamp(pad.roundrectRatio * 2.0f, 0.0f, 1.0f);

            const float angle = pad.angle * kPi / 180.0f;
            const float ca = std::cos(angle), sa = std::sin(angle);
            auto transformed = [&](float x, float y) {
                return toScreen({center.x + x * ca - y * sa, center.y + x * sa + y * ca});
            };

            ImVec2 points[32];
            int pointCount = 0;
            if (radius <= 1e-6f) {
                points[pointCount++] = transformed(-halfW, -halfH);
                points[pointCount++] = transformed( halfW, -halfH);
                points[pointCount++] = transformed( halfW,  halfH);
                points[pointCount++] = transformed(-halfW,  halfH);
            } else {
                constexpr int kCornerSegments = 6;
                const float centers[4][2] = {
                    { halfW - radius, -halfH + radius},
                    { halfW - radius,  halfH - radius},
                    {-halfW + radius,  halfH - radius},
                    {-halfW + radius, -halfH + radius}};
                const float starts[4] = {-90.0f, 0.0f, 90.0f, 180.0f};
                for (int corner = 0; corner < 4; ++corner) {
                    for (int i = 0; i <= kCornerSegments; ++i) {
                        const float a =
                            (starts[corner] + 90.0f * static_cast<float>(i) /
                                                  kCornerSegments) *
                            kPi / 180.0f;
                        points[pointCount++] =
                            transformed(centers[corner][0] + radius * std::cos(a),
                                        centers[corner][1] + radius * std::sin(a));
                    }
                }
            }

            dl_->AddConvexPolyFilled(points, pointCount, fill);
            if (edgeTh <= 0.0f) return;
            for (int i = 0; i < pointCount; ++i)
                dl_->AddLine(points[i], points[(i + 1) % pointCount], edge, edgeTh);
        };

    for (const auto& outline : doc.outline())
        dl_->AddLine(toScreen(outline.p1), toScreen(outline.p2), outlineCol,
                     ui::metrics::stroke(1.8f));

    for (const auto& zone : doc.zones()) {
        if (!doc.layerVisible(zone.layer)) continue;
        int copperIndex = 0;
        for (const auto& layer : doc.layers())
            if (layer.id == zone.layer) {
                copperIndex = layer.copperIndex;
                break;
            }
        const bool matched =
            !highlightedNet_.empty() && zone.net == highlightedNet_;
        const float fillAlpha =
            highlightedNet_.empty() ? 0.14f : (matched ? 0.18f : 0.035f);
        const float edgeAlpha =
            highlightedNet_.empty() ? 0.60f : (matched ? 0.82f : 0.14f);
        std::vector<ImVec2> poly;
        poly.reserve(zone.polygon.size());
        for (const auto& point : zone.polygon) poly.push_back(toScreen(point));
        if (poly.size() < 3) continue;
        dl_->AddConvexPolyFilled(
            poly.data(), static_cast<int>(poly.size()),
            matched ? col(p.selectionHighlight, 0.45f)
                    : copperColorFor(doc, zone.layer, copperIndex, fillAlpha));
        const ImU32 edge =
            matched ? col(p.selectionHighlight, 1.2f)
                    : copperColorFor(doc, zone.layer, copperIndex, edgeAlpha);
        for (size_t i = 0; i < poly.size(); ++i)
            dl_->AddLine(poly[i], poly[(i + 1) % poly.size()], edge,
                         ui::metrics::stroke(1.2f));
    }

    auto layers = doc.layers();
    std::sort(layers.begin(), layers.end(),
              [](const EcadLayer& a, const EcadLayer& b) {
                  return a.copperIndex > b.copperIndex;
              });
    for (const auto& layer : layers) {
        if (!doc.layerVisible(layer.id)) continue;
        for (const EcadTrack* track : doc.tracksOnLayer(layer.id)) {
            if (!doc.netVisible(track->net)) continue;
            const bool matched =
                !highlightedNet_.empty() && track->net == highlightedNet_;
            const float alpha =
                highlightedNet_.empty() ? 0.92f : (matched ? 0.66f : 0.16f);
            drawTrack(*track,
                      copperColorFor(doc, layer.id, layer.copperIndex, alpha),
                      trackStroke(*track, 0.0f));
        }
    }

    for (const auto& fp : doc.footprints()) {
        const EcadPt offset = footprintOffset(fp);
        if (fp.hasCourtyard) drawCourtyard(fp, offset, courtCol);

        for (const auto& pad : fp.pads) {
            int copperIndex = 0;
            std::string layerId;
            bool foundLayer = false;
            for (const auto& candidate : pad.layers) {
                for (const auto& layer : doc.layers()) {
                    if (layer.id == candidate && doc.layerVisible(candidate)) {
                        copperIndex = layer.copperIndex;
                        layerId = candidate;
                        foundLayer = true;
                        break;
                    }
                }
                if (foundLayer) break;
            }
            if (!foundLayer && !pad.layers.empty() &&
                !doc.layerVisible(pad.layers.front()))
                continue;

            const bool matched =
                !highlightedNet_.empty() && pad.net == highlightedNet_;
            const float alpha =
                highlightedNet_.empty() ? 0.95f : (matched ? 1.0f : 0.16f);
            const ImU32 fill =
                matched ? col(p.selectionHighlight, 1.8f)
                        : copperColorFor(doc, layerId, copperIndex, alpha);
            const bool throughHole =
                pad.hasDrill || pad.type == "tht" || pad.type == "np_tht" ||
                pad.type == "connector";
            const ImU32 edge =
                col(p.text, highlightedNet_.empty() ? 0.58f : (matched ? 0.86f : 0.12f));
            const EcadPt center{pad.pos.x + offset.x, pad.pos.y + offset.y};
            drawPadShape(pad, center, fill, edge,
                         throughHole ? ui::metrics::stroke(1.0f) : 0.0f);

            if (pad.hasDrill && pad.drillDia > 0.0f) {
                const ImVec2 drill = toScreen(center);
                const float radius = std::max(
                    ui::metrics::px(1.25f), pad.drillDia * 0.5f * pxPerMm_);
                dl_->AddCircleFilled(
                    drill, radius,
                    highlightedNet_.empty() || matched ? drillCol
                                                       : col(p.windowBg, 0.38f),
                    24);
                dl_->AddCircle(drill, radius, edge, 24, ui::metrics::stroke(1.0f));
            }
        }

        if (!fp.ref.empty()) {
            const ImVec2 origin =
                toScreen({fp.pos.x + offset.x, fp.pos.y + offset.y});
            dl_->AddText(ImVec2(origin.x + ui::metrics::U(1.0f),
                                origin.y - ui::metrics::U(4.0f)),
                         refCol, fp.ref.c_str());
        }
    }

    for (const auto& via : doc.vias()) {
        const bool matched =
            !highlightedNet_.empty() && via.net == highlightedNet_;
        const ImU32 ring =
            matched ? col(p.selectionHighlight, 1.8f)
                    : col(p.text, highlightedNet_.empty() ? 0.90f : 0.14f);
        const ImVec2 center = toScreen(via.at);
        dl_->AddCircleFilled(
            center, std::max(ui::metrics::px(2.0f), via.diameter * 0.5f * pxPerMm_),
            ring, 28);
        dl_->AddCircleFilled(
            center, std::max(ui::metrics::px(1.0f), via.drill * 0.5f * pxPerMm_),
            highlightedNet_.empty() || matched ? drillCol : col(p.windowBg, 0.38f), 20);
    }

    for (const auto& airwire : doc.ratsnest()) {
        if (!doc.netVisible(airwire.net)) continue;
        const bool matched =
            !highlightedNet_.empty() && airwire.net == highlightedNet_;
        const ImU32 color =
            matched ? col(p.selectionHighlight, 1.8f)
                    : col(p.sketchInfer, highlightedNet_.empty() ? 1.0f : 0.14f);
        drawDashedLine(dl_, toScreen(airwire.p1), toScreen(airwire.p2), color,
                       ui::metrics::stroke(matched ? 1.7f : 1.2f),
                       ui::metrics::U(1.25f), ui::metrics::U(1.0f));
    }

    if (!highlightedNet_.empty()) {
        const ImU32 highlight = col(p.selectionHighlight, 1.8f);
        for (const auto& track : doc.tracks()) {
            if (track.net != highlightedNet_ || !doc.layerVisible(track.layer) ||
                !doc.netVisible(track.net))
                continue;
            drawTrack(track, highlight,
                      trackStroke(track, ui::metrics::px(2.0f)));
        }
    }

    if (!hasSelection) return;
    const ImU32 selectionOuter = col(p.selectionHighlight, 0.62f);
    const ImU32 selectionInner = col(p.selectionHighlight, 1.8f);
    if (selectedKind == "fp") {
        for (const auto& fp : doc.footprints()) {
            if (fp.id != selectedId) continue;
            const EcadPt offset{dragDx_, dragDy_};
            EcadPt mn, mx;
            bool haveBounds = false;
            if (fp.hasCourtyard) {
                mn = {fp.courtyardMin.x + offset.x, fp.courtyardMin.y + offset.y};
                mx = {fp.courtyardMax.x + offset.x, fp.courtyardMax.y + offset.y};
                haveBounds = true;
            } else {
                for (const auto& pad : fp.pads) {
                    const float angle = pad.angle * kPi / 180.0f;
                    const float halfW = pad.w * 0.5f, halfH = pad.h * 0.5f;
                    const float ex = std::fabs(std::cos(angle)) * halfW +
                                     std::fabs(std::sin(angle)) * halfH;
                    const float ey = std::fabs(std::sin(angle)) * halfW +
                                     std::fabs(std::cos(angle)) * halfH;
                    const EcadPt center{pad.pos.x + offset.x, pad.pos.y + offset.y};
                    if (!haveBounds) {
                        mn = {center.x - ex, center.y - ey};
                        mx = {center.x + ex, center.y + ey};
                        haveBounds = true;
                    } else {
                        mn.x = std::min(mn.x, center.x - ex);
                        mn.y = std::min(mn.y, center.y - ey);
                        mx.x = std::max(mx.x, center.x + ex);
                        mx.y = std::max(mx.y, center.y + ey);
                    }
                }
            }
            if (haveBounds) {
                ImVec2 a = toScreen({mn.x, mx.y});
                ImVec2 b = toScreen({mx.x, mn.y});
                const float pad = ui::metrics::px(2.0f);
                a.x -= pad;
                a.y -= pad;
                b.x += pad;
                b.y += pad;
                dl_->AddRect(a, b, selectionOuter, 0.0f, 0,
                             ui::metrics::stroke(4.0f));
                dl_->AddRect(a, b, selectionInner, 0.0f, 0,
                             ui::metrics::stroke(1.5f));
            }
            break;
        }
    } else if (selectedKind == "t") {
        for (const auto& track : doc.tracks()) {
            if (track.id != selectedId) continue;
            drawTrack(track, selectionOuter,
                      trackStroke(track, ui::metrics::px(5.0f)));
            drawTrack(track, selectionInner,
                      trackStroke(track, ui::metrics::px(2.0f)));
            break;
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
