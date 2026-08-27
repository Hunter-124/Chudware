#include "ecad_canvas.h"

#include <algorithm>
#include <cctype>
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

// One ERC/DRC violation glyph in screen px — shared by the schematic and board marker
// overlays so both modes read as one product. Error = stroked diamond with a centre dot;
// warning = stroked triangle. Constant SCREEN-space size (r in px) and stroke-only, so a
// marker stays legible at any zoom without occluding the geometry it flags.
void drawViolationGlyph(ImDrawList* dl, const ImVec2& q, bool isError, ImU32 c, float r) {
    if (isError) {
        const ImVec2 diamond[4] = {
            ImVec2(q.x, q.y - r), ImVec2(q.x + r, q.y),
            ImVec2(q.x, q.y + r), ImVec2(q.x - r, q.y)};
        dl->AddPolyline(diamond, 4, c, ImDrawFlags_Closed,
                        ui::metrics::stroke(2.0f));
        dl->AddCircleFilled(q, ui::metrics::px(1.0f), c, 8);
    } else {
        dl->AddTriangle(ImVec2(q.x, q.y - r),
                        ImVec2(q.x + r, q.y + r),
                        ImVec2(q.x - r, q.y + r), c,
                        ui::metrics::stroke(2.0f));
    }
}
void drawRotatedText(ImDrawList* dl, ImVec2 origin, ImU32 color, const char* text,
                     float fontSize, float worldAngleDeg) {
    const int firstVertex = dl->VtxBuffer.Size;
    dl->AddText(nullptr, fontSize, origin, color, text);
    const float angle = -worldAngleDeg * kPi / 180.0f;
    const float c = std::cos(angle), s = std::sin(angle);
    for (int i = firstVertex; i < dl->VtxBuffer.Size; ++i) {
        ImVec2& q = dl->VtxBuffer[i].pos;
        const float x = q.x - origin.x, y = q.y - origin.y;
        q.x = origin.x + x * c - y * s;
        q.y = origin.y + x * s + y * c;
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

bool equalNoCase(char a, char b) {
    return std::tolower(static_cast<unsigned char>(a)) ==
           std::tolower(static_cast<unsigned char>(b));
}

bool equalsNoCase(const std::string& value, const char* expected) {
    const size_t length = std::char_traits<char>::length(expected);
    return value.size() == length &&
           std::equal(value.begin(), value.end(), expected, equalNoCase);
}

bool containsNoCase(const std::string& value, const char* needle) {
    const size_t length = std::char_traits<char>::length(needle);
    return std::search(value.begin(), value.end(), needle, needle + length,
                       equalNoCase) != value.end();
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

bool EcadCanvas::sheetFrameGeometry(const EcadMirror& doc, const EcadSheet*& page,
                                    EcadPt& outMin, EcadPt& outMax,
                                    float& pageScale) const {
    page = nullptr;
    for (const auto& sheet : doc.sheets())
        if (sheet.id == doc.activeSheet()) {
            page = &sheet;
            break;
        }
    if (!page && !doc.sheets().empty()) page = &doc.sheets().front();
    if (!page) return false;

    EcadPt contentMin, contentMax;
    if (!doc.worldBounds(contentMin, contentMax)) {
        outMin = {0.0f, 0.0f};
        outMax = {page->w, page->h};
        pageScale = 1.0f;
        return true;
    }

    constexpr float margin = 5.0f;
    constexpr float pad = 3.0f;
    const float availableW = std::max(1.0f, page->w - 2.0f * (margin + pad));
    const float availableH = std::max(1.0f, page->h - 2.0f * (margin + pad));
    pageScale = 1.0f;
    const float contentW = contentMax.x - contentMin.x;
    const float contentH = contentMax.y - contentMin.y;
    if (contentW > 0.0f) pageScale = std::min(pageScale, availableW / contentW);
    if (contentH > 0.0f) pageScale = std::min(pageScale, availableH / contentH);
    pageScale = std::max(pageScale, 0.0001f);

    const EcadPt center{(contentMin.x + contentMax.x) * 0.5f,
                        (contentMin.y + contentMax.y) * 0.5f};
    const float halfW = page->w * 0.5f / pageScale;
    const float halfH = page->h * 0.5f / pageScale;
    outMin = {center.x - halfW, center.y - halfH};
    outMax = {center.x + halfW, center.y + halfH};
    return true;
}


void EcadCanvas::zoomToFit(const EcadMirror& doc, const ImVec2& avail) {
    EcadPt mn, mx;
    bool haveBounds = doc.worldBounds(mn, mx);
    // Fit actual design content by default. The page is a drafting overlay and may extend beyond
    // the viewport; using it as the camera authority makes a small circuit unreadably tiny.
    if (!haveBounds && showSheetFrame_ && doc.isSch()) {
        const EcadSheet* page = nullptr;
        float pageScale = 1.0f;
        haveBounds = sheetFrameGeometry(doc, page, mn, mx, pageScale);
    }
    if (!haveBounds) {
        pan_ = {0, 0};
        pxPerMm_ = 8.0f;
        inited_ = true;
        return;
    }
    const float w = std::max(1.0f, mx.x - mn.x);
    const float h = std::max(1.0f, mx.y - mn.y);
    pan_ = {(mn.x + mx.x) * 0.5f, (mn.y + mx.y) * 0.5f};
    const float margin = 1.15f;
    const float zx = avail.x / (w * margin);
    const float zy = avail.y / (h * margin);
    const float maxFitZoom = ui::metrics::U(16.0f);
    pxPerMm_ = std::clamp(std::min(zx, zy), 0.25f, maxFitZoom);
    inited_ = true;
}

void EcadCanvas::requestFit() { wantFit_ = true; }

float EcadCanvas::zoom() const { return pxPerMm_; }

void EcadCanvas::setSelection(const std::vector<std::string>& tokens) { selected_ = tokens; }
void EcadCanvas::setHighlightedNet(const std::string& net) { highlightedNet_ = net; }
void EcadCanvas::setMarquee(bool active, const EcadPt& a, const EcadPt& b) {
    marqueeActive_ = active;
    marqueeA_ = a;
    marqueeB_ = b;
}
void EcadCanvas::setRoutePreview(const std::vector<EcadPt>& pts, bool bus) {
    routePreview_ = pts;
    routePreviewBus_ = bus;
}
void EcadCanvas::setSnapTarget(bool active, const EcadPt& w, const std::string& label) {
    snapTargetActive_ = active;
    snapTarget_ = w;
    snapTargetLabel_ = label;
}
void EcadCanvas::setDragOffset(float dx, float dy) {
    dragDx_ = dx;
    dragDy_ = dy;
}
void EcadCanvas::setShowErcMarkers(bool on) { showErcMarkers_ = on; }
void EcadCanvas::setShowDrcMarkers(bool on) { showDrcMarkers_ = on; }
void EcadCanvas::setShowSheetFrame(bool on) {
    if (showSheetFrame_ != on) wantFit_ = true;
    showSheetFrame_ = on;
}

bool EcadCanvas::selected(const char* kind, const std::string& id) const {
    const size_t kindLength = std::char_traits<char>::length(kind);
    for (const std::string& token : selected_) {
        if (token.size() != kindLength + 1 + id.size()) continue;
        if (token.compare(0, kindLength, kind) != 0 || token[kindLength] != ':') continue;
        if (token.compare(kindLength + 1, id.size(), id) == 0) return true;
    }
    return false;
}

bool EcadCanvas::selected(const char* kind, const std::string& first,
                          const std::string& second) const {
    const size_t kindLength = std::char_traits<char>::length(kind);
    const size_t firstOffset = kindLength + 1;
    const size_t secondOffset = firstOffset + first.size() + 1;
    for (const std::string& token : selected_) {
        if (token.size() != secondOffset + second.size()) continue;
        if (token.compare(0, kindLength, kind) != 0 || token[kindLength] != ':') continue;
        if (token.compare(firstOffset, first.size(), first) != 0 ||
            token[firstOffset + first.size()] != '.')
            continue;
        if (token.compare(secondOffset, second.size(), second) == 0) return true;
    }
    return false;
}

void EcadCanvas::focusOn(const EcadPt& w) { focusOn(w, true); }

void EcadCanvas::focusOn(const EcadPt& w, bool isError) {
    pan_ = w;
    if (pxPerMm_ < 14.0f) pxPerMm_ = 14.0f;
    wantFit_ = false;
    inited_ = true;
    focusLoc_ = w;
    focusIsError_ = isError;
    focusFrames_ = 180;
}

void EcadCanvas::handleNavigation(const EcadCanvasIO& io, const ImVec2& avail) {
    if (!io.hovered) return;
    ImGuiIO& gio = ImGui::GetIO();
    const float wheel = gio.MouseWheel;
    if (wheel != 0.0f && io.hasWorldMouse) {
        const ImVec2 sBefore = toScreen(io.worldMouse);
        const float factor = std::pow(1.15f, wheel);
        pxPerMm_ = std::max(0.1f, std::min(400.0f, pxPerMm_ * factor));
        const EcadPt wAfter = toWorld(sBefore);
        pan_.x += io.worldMouse.x - wAfter.x;
        pan_.y += io.worldMouse.y - wAfter.y;
    }

    if (ImGui::IsMouseDragging(ImGuiMouseButton_Middle) ||
        ImGui::IsMouseDragging(ImGuiMouseButton_Right)) {
        const ImVec2 d = gio.MouseDelta;
        pan_.x -= d.x / pxPerMm_;
        pan_.y += d.y / pxPerMm_;
    }
    (void)avail;
}

// The document grid remains the source cadence. Camera zoom may skip whole grid points for
// legibility, but it never substitutes an unrelated 1/2/5 world spacing.
void EcadCanvas::drawGrid(const EcadMirror& doc, const ImVec2& avail) {
    const ui::Palette& p = ui::palette;
    const float baseMm = std::max(doc.grid(), 1e-4f);
    const float targetPx = ui::metrics::U(5.0f);
    const float rawSkip = targetPx / std::max(baseMm * pxPerMm_, 1e-6f);
    int skip = 1;
    if (rawSkip > 1.0f) {
        const float decade = std::pow(10.0f, std::floor(std::log10(rawSkip)));
        const float normalized = rawSkip / decade;
        const float multiple = normalized <= 1.0f ? 1.0f :
                               normalized <= 2.0f ? 2.0f :
                               normalized <= 5.0f ? 5.0f : 10.0f;
        skip = std::max(1, static_cast<int>(std::ceil(decade * multiple)));
    }
    float stepMm = baseMm * static_cast<float>(skip);

    const EcadPt wTL = toWorld(imgMin_);
    const EcadPt wBR = toWorld(ImVec2(imgMin_.x + avail.x, imgMin_.y + avail.y));
    const float minX = std::min(wTL.x, wBR.x);
    const float maxX = std::max(wTL.x, wBR.x);
    const float minY = std::min(wTL.y, wBR.y);
    const float maxY = std::max(wTL.y, wBR.y);
    int ix0 = static_cast<int>(std::floor(minX / stepMm));
    int ix1 = static_cast<int>(std::ceil(maxX / stepMm));
    int iy0 = static_cast<int>(std::floor(minY / stepMm));
    int iy1 = static_cast<int>(std::ceil(maxY / stepMm));

    constexpr long long kMaxDotCount = 32000;
    while (static_cast<long long>(ix1 - ix0 + 1) *
               static_cast<long long>(iy1 - iy0 + 1) > kMaxDotCount) {
        skip *= 2;
        stepMm = baseMm * static_cast<float>(skip);
        ix0 = static_cast<int>(std::floor(minX / stepMm));
        ix1 = static_cast<int>(std::ceil(maxX / stepMm));
        iy0 = static_cast<int>(std::floor(minY / stepMm));
        iy1 = static_cast<int>(std::ceil(maxY / stepMm));
    }

    const ImVec2 imgMax(imgMin_.x + avail.x, imgMin_.y + avail.y);
    float gridMinX = imgMin_.x;
    float gridMaxX = imgMax.x;
    const float managerEdge = ui::overlayColumnEdge(0);
    const float copilotEdge = ui::overlayColumnEdge(1);
    if (managerEdge >= 0.0f) gridMinX = std::clamp(managerEdge, imgMin_.x, imgMax.x);
    if (copilotEdge >= 0.0f) gridMaxX = std::clamp(copilotEdge, imgMin_.x, imgMax.x);
    if (gridMaxX <= gridMinX) return;

    dl_->PushClipRect(ImVec2(gridMinX, imgMin_.y), ImVec2(gridMaxX, imgMax.y), true);
    const float minorRadius = ui::metrics::px(0.65f);
    const float majorRadius = ui::metrics::px(1.05f);
    const float stepPx = stepMm * pxPerMm_;
    const float fadeSpan = std::max(stepPx * 2.0f, ui::metrics::U(2.0f));
    const ImVec2 first = toScreen(
        {static_cast<float>(ix0) * stepMm, static_cast<float>(iy0) * stepMm});
    for (int ix = ix0; ix <= ix1; ++ix) {
        const float sx = first.x + static_cast<float>(ix - ix0) * stepPx;
        if (sx <= gridMinX || sx >= gridMaxX) continue;
        const float edgeDistance = std::min(sx - gridMinX, gridMaxX - sx);
        const float edgeFade = std::clamp(edgeDistance / fadeSpan, 0.0f, 1.0f);
        const ImU32 minorCol = col(p.sketchGrid, 0.55f * edgeFade);
        const ImU32 majorCol = col(p.sketchGrid, edgeFade);
        const long long baseIx = static_cast<long long>(ix) * skip;
        for (int iy = iy0; iy <= iy1; ++iy) {
            const long long baseIy = static_cast<long long>(iy) * skip;
            if (baseIx == 0 && baseIy == 0) continue;
            const float sy = first.y - static_cast<float>(iy - iy0) * stepPx;
            const bool major = baseIx % 5 == 0 && baseIy % 5 == 0;
            dl_->AddCircleFilled(ImVec2(sx, sy), major ? majorRadius : minorRadius,
                                 major ? majorCol : minorCol, 6);
        }
    }

    if (minX <= 0.0f && maxX >= 0.0f && minY <= 0.0f && maxY >= 0.0f) {
        const ImVec2 origin = toScreen({0.0f, 0.0f});
        if (origin.x >= gridMinX && origin.x <= gridMaxX) {
            const ImU32 originCol = col(p.sketchInk, 0.20f);
            dl_->AddCircle(origin, ui::metrics::px(2.25f), originCol, 12,
                           ui::metrics::stroke(1.0f));
            dl_->AddCircleFilled(origin, ui::metrics::px(0.65f), originCol, 6);
        }
    }
    dl_->PopClipRect();
}

// ---- schematic -----------------------------------------------------------------------------

void EcadCanvas::drawSch(const EcadMirror& doc) {
    const ui::Palette& p = ui::palette;
    const float bodyStroke = zoomStroke(pxPerMm_, 0.12f, 1.35f, 2.6f);
    const float pinStroke = zoomStroke(pxPerMm_, 0.10f, 1.2f, 2.3f);
    const float wireStroke = zoomStroke(pxPerMm_, 0.10f, 1.25f, 2.5f);
    const float busStroke = zoomStroke(pxPerMm_, 0.45f, 2.8f, 5.5f);
    const ImU32 selectionOuter = col(p.selectionHighlight, 0.58f);
    const ImU32 selectionInner = col(p.selectionHighlight);

    auto netOf = [&doc](const std::string& id,
                        const std::string& direct) -> const std::string& {
        return direct.empty() ? doc.netOfObject(id) : direct;
    };
    auto netVisible = [&doc](const std::string& net) {
        return net.empty() || doc.netVisible(net);
    };
    auto netMatched = [this](const std::string& net) {
        return !highlightedNet_.empty() && net == highlightedNet_;
    };
    auto themedNetColor = [&](const float role[4], const std::string& net, float normalAlpha) {
        if (netMatched(net)) return selectionInner;
        return col(role, highlightedNet_.empty() ? normalAlpha : normalAlpha * 0.17f);
    };
    auto staticColor = [&](const float role[4], float normalAlpha = 1.0f) {
        return col(role, highlightedNet_.empty() ? normalAlpha : normalAlpha * 0.25f);
    };
    auto selectedPoint = [&](const char* kind, const std::string& id, const EcadPt& point) {
        if (!selected(kind, id)) return;
        const ImVec2 q = toScreen(point);
        dl_->AddCircle(q, ui::metrics::px(6.0f), selectionOuter, 20,
                       ui::metrics::stroke(4.0f));
        dl_->AddCircle(q, ui::metrics::px(6.0f), selectionInner, 20,
                       ui::metrics::stroke(1.5f));
    };

    auto drawPolyline = [&](const std::vector<EcadPt>& pts, ImU32 color, float thickness,
                            bool closed = false) {
        if (pts.size() < 2) return;
        for (size_t i = 1; i < pts.size(); ++i)
            dl_->AddLine(toScreen(pts[i - 1]), toScreen(pts[i]), color, thickness);
        if (closed && pts.size() > 2)
            dl_->AddLine(toScreen(pts.back()), toScreen(pts.front()), color, thickness);
    };

    auto drawArc = [&](const EcadPt& center, float radius, float a0, float a1, ImU32 color,
                       float thickness, bool filled) {
        const float sweep = a1 - a0;
        const int segments = std::clamp(
            static_cast<int>(std::ceil(std::fabs(sweep) * kPi / 180.0f *
                                       std::max(radius * pxPerMm_, 1.0f) /
                                       ui::metrics::U(1.0f))),
            12, 192);
        std::vector<ImVec2> points;
        points.reserve(static_cast<size_t>(segments) + (filled ? 2u : 1u));
        if (filled) points.push_back(toScreen(center));
        for (int i = 0; i <= segments; ++i) {
            const float a = (a0 + sweep * static_cast<float>(i) / segments) * kPi / 180.0f;
            points.push_back(toScreen(
                {center.x + radius * std::cos(a), center.y + radius * std::sin(a)}));
        }
        if (filled && points.size() >= 3)
            dl_->AddConvexPolyFilled(points.data(), static_cast<int>(points.size()), color);
        for (size_t i = filled ? 2u : 1u; i < points.size(); ++i)
            dl_->AddLine(points[i - 1], points[i], color, thickness);
    };

    auto drawGraphic = [&](const EcadGraphic& g, ImU32 color, float extraStroke, bool fill) {
        const float th = std::max(ui::metrics::stroke(1.0f), g.width * pxPerMm_) + extraStroke;
        switch (g.kind) {
            case EcadGraphic::Line:
                if (g.pts.size() >= 2)
                    dl_->AddLine(toScreen(g.pts[0]), toScreen(g.pts[1]), color, th);
                break;
            case EcadGraphic::Rect:
                if (g.pts.size() >= 2) {
                    ImVec2 a = toScreen(g.pts[0]), b = toScreen(g.pts[1]);
                    ImVec2 mn(std::min(a.x, b.x), std::min(a.y, b.y));
                    ImVec2 mx(std::max(a.x, b.x), std::max(a.y, b.y));
                    if (fill && g.filled) dl_->AddRectFilled(mn, mx, color);
                    dl_->AddRect(mn, mx, color, 0.0f, 0, th);
                }
                break;
            case EcadGraphic::Circle:
                if (!g.pts.empty()) {
                    const ImVec2 c = toScreen(g.pts[0]);
                    const float r = std::fabs(g.radius) * pxPerMm_;
                    if (fill && g.filled) dl_->AddCircleFilled(c, r, color, 48);
                    dl_->AddCircle(c, r, color, 48, th);
                }
                break;
            case EcadGraphic::Arc:
                if (!g.pts.empty())
                    drawArc(g.pts[0], std::fabs(g.radius), g.a0, g.a1, color, th,
                            fill && g.filled);
                break;
            case EcadGraphic::Polyline:
                if (fill && g.filled && g.pts.size() >= 3) {
                    std::vector<ImVec2> points;
                    points.reserve(g.pts.size());
                    for (const EcadPt& q : g.pts) points.push_back(toScreen(q));
                    dl_->AddConvexPolyFilled(points.data(), static_cast<int>(points.size()), color);
                }
                drawPolyline(g.pts, color, th, g.filled);
                break;
        }
    };

    if (showSheetFrame_) {
        const EcadSheet* page = nullptr;
        EcadPt pageMin, pageMax;
        float pageScale = 1.0f;
        if (sheetFrameGeometry(doc, page, pageMin, pageMax, pageScale)) {
            const ImU32 frame = staticColor(p.textDim, 0.75f);
            const ImVec2 frameTL = toScreen({pageMin.x, pageMax.y});
            const ImVec2 frameBR = toScreen({pageMax.x, pageMin.y});
            dl_->AddRect(frameTL, frameBR, frame, 0.0f, 0,
                         ui::metrics::stroke(1.5f));
            const float inset = 5.0f / pageScale;
            dl_->AddRect(toScreen({pageMin.x + inset, pageMax.y - inset}),
                         toScreen({pageMax.x - inset, pageMin.y + inset}), frame, 0.0f, 0,
                         ui::metrics::stroke(1.0f));

            const float paperBlockW =
                std::min(96.0f, std::max(55.0f, page->w - 10.0f));
            const float blockW = paperBlockW / pageScale;
            const float blockH = 31.0f / pageScale;
            const float blockBottom = pageMin.y + inset;
            const EcadPt blockTL{pageMax.x - inset - blockW, blockBottom + blockH};
            const EcadPt blockBR{pageMax.x - inset, blockBottom};
            const ImVec2 bt = toScreen(blockTL), bb = toScreen(blockBR);
            dl_->AddRectFilled(bt, bb, col(p.windowBg, 0.86f));
            dl_->AddRect(bt, bb, frame, 0.0f, 0, ui::metrics::stroke(1.0f));
            const EcadTitleBlock& tb = doc.titleBlock();
            const float x = bt.x + ui::metrics::U(1.0f);
            float y = bt.y + ui::metrics::U(0.6f);
            const float fontSize =
                std::clamp(1.8f / pageScale * pxPerMm_,
                           ui::metrics::px(7.0f), ImGui::GetFontSize());
            const float lineHeight = fontSize + ui::metrics::px(1.0f);
            auto blockText = [&](ImVec2 pos, ImU32 color, const std::string& value) {
                dl_->AddText(nullptr, fontSize, pos, color, value.c_str());
            };
            const std::string& title = tb.title.empty() ? doc.name() : tb.title;
            if (!title.empty()) {
                blockText(ImVec2(x, y), staticColor(p.text), title);
                y += lineHeight;
            }
            if (!tb.company.empty()) {
                blockText(ImVec2(x, y), staticColor(p.textDim), tb.company);
                y += lineHeight;
            }
            blockText(ImVec2(x, y), staticColor(p.textDim), tb.paper);
            blockText(ImVec2(x + blockW * pxPerMm_ * 0.28f, y),
                      staticColor(p.textDim), tb.rev);
            blockText(ImVec2(x + blockW * pxPerMm_ * 0.50f, y),
                      staticColor(p.textDim), tb.date);
            y += lineHeight;
            const std::string* comments[] = {
                &tb.comment1, &tb.comment2, &tb.comment3, &tb.comment4};
            for (const std::string* comment : comments) {
                if (comment->empty()) continue;
                blockText(ImVec2(x, y), staticColor(p.textDim), *comment);
                y += lineHeight;
            }
        }
    }

    for (const auto& g : doc.graphics()) {
        if (!doc.onActiveSheet(g.sheet)) continue;
        drawGraphic(g, staticColor(p.sketchUnder, 0.72f), 0.0f, true);
        if (selected("graphic", g.id)) {
            drawGraphic(g, selectionOuter, ui::metrics::px(4.0f), false);
            drawGraphic(g, selectionInner, ui::metrics::px(1.0f), false);
        }
    }

    for (const auto& text : doc.texts()) {
        if (!doc.onActiveSheet(text.sheet)) continue;
        const ImVec2 q = toScreen(text.pos);
        const ImU32 c = staticColor(p.text, 0.88f);
        const float fontSize =
            std::clamp(text.size * pxPerMm_, ImGui::GetFontSize() * 0.65f,
                       ImGui::GetFontSize() * 4.0f);
        drawRotatedText(dl_, q, c, text.text.c_str(), fontSize, text.angle);
        if (selected("text", text.id)) {
            ImVec2 size = ImGui::CalcTextSize(text.text.c_str());
            const float scale = fontSize / std::max(ImGui::GetFontSize(), 1.0f);
            size.x *= scale;
            size.y *= scale;
            const float halo = ui::metrics::px(2.0f);
            dl_->AddRect(ImVec2(q.x - halo, q.y - halo),
                         ImVec2(q.x + size.x + halo, q.y + size.y + halo),
                         selectionInner, 0.0f, 0, ui::metrics::stroke(1.5f));
        }
    }

    for (const auto& bus : doc.buses()) {
        if (!doc.onActiveSheet(bus.sheet)) continue;
        const std::string& resolved = doc.netOfObject(bus.id);
        const std::string& net = resolved.empty() ? bus.name : resolved;
        bool memberMatched = false;
        bool memberVisible = false;
        for (const std::string& member : bus.members) {
            memberMatched = memberMatched || netMatched(member);
            memberVisible = memberVisible || doc.netVisible(member);
        }
        if (!netVisible(net) && !memberVisible) continue;
        const ImU32 c = memberMatched ? selectionInner
                                      : themedNetColor(p.sketchUnder, net, 0.95f);
        drawPolyline(bus.pts, c, busStroke);
        if (!bus.name.empty() && !bus.pts.empty())
            dl_->AddText(toScreen({bus.pts.front().x + 1.0f, bus.pts.front().y + 2.0f}),
                         c, bus.name.c_str());
        if (selected("bus", bus.id)) {
            drawPolyline(bus.pts, selectionOuter, busStroke + ui::metrics::px(4.0f));
            drawPolyline(bus.pts, selectionInner, busStroke + ui::metrics::px(1.0f));
        }
    }

    for (const auto& entry : doc.busEntries()) {
        if (!doc.onActiveSheet(entry.sheet)) continue;
        const std::string& net = netOf(entry.id, entry.net);
        if (!netVisible(net)) continue;
        const EcadPt end{entry.pos.x + entry.size.x, entry.pos.y + entry.size.y};
        const ImU32 c = themedNetColor(p.sketchUnder, net, 0.95f);
        dl_->AddLine(toScreen(entry.pos), toScreen(end), c,
                     std::max(busStroke * 0.65f, ui::metrics::stroke(1.5f)));
        if (selected("busentry", entry.id)) {
            dl_->AddLine(toScreen(entry.pos), toScreen(end), selectionOuter,
                         busStroke + ui::metrics::px(4.0f));
            dl_->AddLine(toScreen(entry.pos), toScreen(end), selectionInner,
                         busStroke + ui::metrics::px(1.0f));
        }
    }

    for (const auto& wire : doc.wires()) {
        if (!doc.onActiveSheet(wire.sheet)) continue;
        const std::string& net = netOf(wire.id, wire.net);
        if (!netVisible(net)) continue;
        const float th = wire.bus ? busStroke : wireStroke;
        const ImU32 c = themedNetColor(wire.bus ? p.sketchUnder : p.sketchInk, net, 0.90f);
        drawPolyline(wire.pts, c, th);
        if (selected("wire", wire.id)) {
            drawPolyline(wire.pts, selectionOuter, th + ui::metrics::px(4.0f));
            drawPolyline(wire.pts, selectionInner, th + ui::metrics::px(1.0f));
        }
    }

    const float junctionRadius =
        std::clamp(0.22f * pxPerMm_, ui::metrics::px(2.5f), ui::metrics::px(4.4f));
    for (const auto& junction : doc.junctions()) {
        if (!doc.onActiveSheet(junction.sheet)) continue;
        const std::string& net = netOf(junction.id, junction.net);
        if (!netVisible(net)) continue;
        dl_->AddCircleFilled(toScreen(junction.pos), junctionRadius,
                             themedNetColor(p.sketchInk, net, 0.92f), 16);
        selectedPoint("junction", junction.id, junction.pos);
    }

    for (const auto& sheetSymbol : doc.sheetSymbols()) {
        if (!doc.onActiveSheet(sheetSymbol.sheet)) continue;
        const ImVec2 a = toScreen(sheetSymbol.pos);
        const ImVec2 b = toScreen({sheetSymbol.pos.x + sheetSymbol.size.x,
                                   sheetSymbol.pos.y - sheetSymbol.size.y});
        const ImVec2 mn(std::min(a.x, b.x), std::min(a.y, b.y));
        const ImVec2 mx(std::max(a.x, b.x), std::max(a.y, b.y));
        const ImU32 c = staticColor(p.accentDim, 0.88f);
        dl_->AddRectFilled(mn, mx, col(p.windowBg, highlightedNet_.empty() ? 0.45f : 0.15f));
        dl_->AddRect(mn, mx, c, 0.0f, 0, bodyStroke);
        dl_->AddText(ImVec2(mn.x + ui::metrics::U(1.0f), mn.y + ui::metrics::U(0.5f)),
                     c, sheetSymbol.name.c_str());
        for (const auto& pin : sheetSymbol.pins) {
            if (!netVisible(pin.net)) continue;
            const ImVec2 q = toScreen(pin.pos);
            const ImU32 pc = themedNetColor(p.accentDim, pin.net, 0.95f);
            dl_->AddCircleFilled(q, ui::metrics::px(2.0f), pc, 10);
            dl_->AddText(ImVec2(q.x + ui::metrics::U(0.7f),
                                q.y - ImGui::GetTextLineHeight() * 0.5f),
                         pc, pin.name.c_str());
        }
        if (selected("sheetsym", sheetSymbol.id)) {
            dl_->AddRect(mn, mx, selectionOuter, 0.0f, 0,
                         ui::metrics::stroke(5.0f));
            dl_->AddRect(mn, mx, selectionInner, 0.0f, 0,
                         ui::metrics::stroke(1.5f));
        }
    }

    auto drawSymbolBody = [&](const EcadSymbol& symbol, const EcadPt& offset, ImU32 color,
                              float extraStroke) {
        const EcadPt placed{symbol.pos.x + offset.x, symbol.pos.y + offset.y};
        const float th = bodyStroke + extraStroke;
        for (const auto& g : symbol.body) {
            auto world = [&](const EcadPt& local) {
                return EcadMirror::localToWorld(local, placed, symbol.angle, symbol.mirror);
            };
            switch (g.kind) {
                case EcadSymGraphic::Rect:
                    if (g.pts.size() >= 2) {
                        const EcadPt c0 = g.pts[0], c1 = g.pts[1];
                        const EcadPt corners[4] = {
                            {c0.x, c0.y}, {c1.x, c0.y}, {c1.x, c1.y}, {c0.x, c1.y}};
                        for (int i = 0; i < 4; ++i)
                            dl_->AddLine(toScreen(world(corners[i])),
                                         toScreen(world(corners[(i + 1) % 4])), color, th);
                    }
                    break;
                case EcadSymGraphic::Line:
                    if (g.pts.size() >= 2)
                        dl_->AddLine(toScreen(world(g.pts[0])), toScreen(world(g.pts[1])),
                                     color, th);
                    break;
                case EcadSymGraphic::Circle:
                    if (!g.pts.empty())
                        dl_->AddCircle(toScreen(world(g.pts[0])), std::fabs(g.radius) * pxPerMm_,
                                       color, 40, th);
                    break;
                case EcadSymGraphic::Arc:
                    if (!g.pts.empty()) {
                        const float sweep = g.a1 - g.a0;
                        const int count = std::clamp(
                            static_cast<int>(std::ceil(std::fabs(sweep) * kPi / 180.0f *
                                                       std::fabs(g.radius) * pxPerMm_ /
                                                       ui::metrics::U(1.0f))),
                            12, 96);
                        ImVec2 prev;
                        for (int i = 0; i <= count; ++i) {
                            const float a =
                                (g.a0 + sweep * static_cast<float>(i) / count) * kPi / 180.0f;
                            const ImVec2 q = toScreen(world(
                                {g.pts[0].x + g.radius * std::cos(a),
                                 g.pts[0].y + g.radius * std::sin(a)}));
                            if (i) dl_->AddLine(prev, q, color, th);
                            prev = q;
                        }
                    }
                    break;
                case EcadSymGraphic::Polyline:
                    for (size_t i = 1; i < g.pts.size(); ++i)
                        dl_->AddLine(toScreen(world(g.pts[i - 1])),
                                     toScreen(world(g.pts[i])), color, th);
                    break;
            }
        }
    };

    for (const auto& symbol : doc.symbols()) {
        if (!doc.onActiveSheet(symbol.sheet)) continue;
        const bool moving = selected("sym", symbol.id);
        const EcadPt offset = moving ? EcadPt{dragDx_, dragDy_} : EcadPt{0.0f, 0.0f};
        bool symbolMatched = false;
        for (const auto& pin : symbol.pins)
            symbolMatched = symbolMatched || (netVisible(pin.net) && netMatched(pin.net));
        ImU32 bodyColor = symbol.power ? col(p.statusWarn) : col(p.sketchInk);
        if (symbol.dnp) bodyColor = col(p.textDim, 0.62f);
        else if (!highlightedNet_.empty() && !symbolMatched) bodyColor = col(p.sketchInk, 0.22f);
        else if (symbolMatched) bodyColor = selectionInner;
        drawSymbolBody(symbol, offset, bodyColor, 0.0f);

        for (const auto& pin : symbol.pins) {
            if (!netVisible(pin.net)) continue;
            const EcadPt root{pin.root.x + offset.x, pin.root.y + offset.y};
            const EcadPt tip{pin.tip.x + offset.x, pin.tip.y + offset.y};
            const ImVec2 r = toScreen(root), t = toScreen(tip);
            float dx = t.x - r.x, dy = t.y - r.y;
            const float len = std::max(std::hypot(dx, dy), 1.0f);
            dx /= len;
            dy /= len;
            const float alpha = pin.hidden ? 0.28f : 0.92f;
            const ImU32 pc = netMatched(pin.net)
                                 ? col(p.selectionHighlight, pin.hidden ? 0.35f : 1.0f)
                                 : col(p.sketchUnder,
                                       highlightedNet_.empty() ? alpha : alpha * 0.17f);
            dl_->AddLine(r, t, pc, pinStroke);

            const bool inverted = containsNoCase(pin.shape, "inverted");
            if (inverted) {
                dl_->AddCircle(t, ui::metrics::px(2.6f), pc, 16, pinStroke);
            } else if (pin.net.empty()) {
                dl_->AddCircle(t, ui::metrics::px(2.2f), pc, 12,
                               ui::metrics::stroke(1.0f));
            } else {
                dl_->AddCircleFilled(t, ui::metrics::px(1.8f), pc, 10);
            }
            if (containsNoCase(pin.shape, "clock")) {
                const float h = ui::metrics::U(1.1f);
                const ImVec2 base(r.x - dx * h * 2.0f, r.y - dy * h * 2.0f);
                const ImVec2 normal(-dy * h, dx * h);
                dl_->AddTriangle(r, ImVec2(base.x + normal.x, base.y + normal.y),
                                 ImVec2(base.x - normal.x, base.y - normal.y), pc, pinStroke);
            }

            if (!pin.name.empty() && pin.name != "~") {
                const ImVec2 size = ImGui::CalcTextSize(pin.name.c_str());
                ImVec2 pos = r;
                pos.y -= size.y * 0.5f;
                pos.x += dx >= 0.0f ? -size.x - ui::metrics::U(0.8f)
                                    : ui::metrics::U(0.8f);
                dl_->AddText(pos, pc, pin.name.c_str());
            }
            if (!pin.number.empty()) {
                const ImVec2 size = ImGui::CalcTextSize(pin.number.c_str());
                ImVec2 pos(t.x + dx * ui::metrics::U(0.8f),
                           t.y + dy * ui::metrics::U(0.8f) - size.y * 0.5f);
                if (dx < 0.0f) pos.x -= size.x;
                dl_->AddText(pos, pc, pin.number.c_str());
            }

            if (selected("pin", symbol.id, pin.number) || selected("pin", symbol.id)) {
                dl_->AddLine(r, t, selectionOuter, pinStroke + ui::metrics::px(5.0f));
                dl_->AddLine(r, t, selectionInner, pinStroke + ui::metrics::px(1.5f));
                dl_->AddCircle(t, ui::metrics::px(5.0f), selectionInner, 16,
                               ui::metrics::stroke(1.5f));
            }
        }

        const ImVec2 origin = toScreen({symbol.pos.x + offset.x, symbol.pos.y + offset.y});
        const std::string* displayRef = &symbol.ref;
        std::string unitRef;
        if (symbol.unitCount > 1) {
            unitRef = symbol.ref;
            if (symbol.unit >= 1 && symbol.unit <= 26)
                unitRef.push_back(static_cast<char>('A' + symbol.unit - 1));
            else
                unitRef += "." + std::to_string(symbol.unit);
            displayRef = &unitRef;
        }
        if (!displayRef->empty())
            dl_->AddText(ImVec2(origin.x + ui::metrics::U(2.0f),
                                origin.y - ui::metrics::U(4.5f)),
                         symbol.dnp ? col(p.textDim) : staticColor(p.accentDim),
                         displayRef->c_str());
        if (!symbol.value.empty())
            dl_->AddText(ImVec2(origin.x + ui::metrics::U(2.0f),
                                origin.y + ui::metrics::U(1.5f)),
                         symbol.dnp ? col(p.textDim) : bodyColor, symbol.value.c_str());

        if (symbol.dnp) {
            const float r = ui::metrics::U(4.0f);
            dl_->AddLine(ImVec2(origin.x - r, origin.y - r),
                         ImVec2(origin.x + r, origin.y + r), col(p.textDim),
                         ui::metrics::stroke(2.0f));
            dl_->AddLine(ImVec2(origin.x - r, origin.y + r),
                         ImVec2(origin.x + r, origin.y - r), col(p.textDim),
                         ui::metrics::stroke(2.0f));
        }
        if (symbol.generated) {
            const float r = ui::metrics::U(2.2f);
            const ImVec2 badge(origin.x - ui::metrics::U(3.2f),
                               origin.y - ui::metrics::U(4.0f));
            dl_->AddCircleFilled(badge, r, col(p.statusWarn), 18);
            const ImVec2 bang = ImGui::CalcTextSize("!");
            dl_->AddText(ImVec2(badge.x - bang.x * 0.5f, badge.y - bang.y * 0.5f),
                         col(p.windowBg), "!");
        }
        if (moving) {
            drawSymbolBody(symbol, offset, selectionOuter, ui::metrics::px(4.0f));
            drawSymbolBody(symbol, offset, selectionInner, ui::metrics::px(1.0f));
        }
    }

    for (const auto& label : doc.labels()) {
        if (!doc.onActiveSheet(label.sheet)) continue;
        const std::string& net = netOf(label.id, label.net);
        if (!netVisible(net)) continue;
        const ImU32 c = themedNetColor(p.accentDim, net, 0.95f);
        const ImVec2 q = toScreen(label.pos);
        const float a = label.angle * kPi / 180.0f;
        const ImVec2 dir(std::cos(a), -std::sin(a));
        const ImVec2 normal(-dir.y, dir.x);
        const float leader = ui::metrics::U(1.6f);
        const ImVec2 end(q.x + dir.x * leader, q.y + dir.y * leader);
        dl_->AddLine(q, end, c, ui::metrics::stroke(1.5f));
        if (equalsNoCase(label.scope, "global")) {
            const float d = ui::metrics::U(1.4f);
            const ImVec2 points[4] = {
                end,
                ImVec2(end.x + dir.x * d + normal.x * d, end.y + dir.y * d + normal.y * d),
                ImVec2(end.x + dir.x * d * 2.6f, end.y + dir.y * d * 2.6f),
                ImVec2(end.x + dir.x * d - normal.x * d, end.y + dir.y * d - normal.y * d)};
            dl_->AddPolyline(points, 4, c, ImDrawFlags_Closed, ui::metrics::stroke(1.2f));
        } else if (equalsNoCase(label.scope, "hier") ||
                   equalsNoCase(label.scope, "hierarchical")) {
            const float d = ui::metrics::U(1.2f);
            const ImVec2 points[5] = {
                end,
                ImVec2(end.x + dir.x * d + normal.x * d, end.y + dir.y * d + normal.y * d),
                ImVec2(end.x + dir.x * d * 3.0f + normal.x * d,
                       end.y + dir.y * d * 3.0f + normal.y * d),
                ImVec2(end.x + dir.x * d * 3.0f - normal.x * d,
                       end.y + dir.y * d * 3.0f - normal.y * d),
                ImVec2(end.x + dir.x * d - normal.x * d, end.y + dir.y * d - normal.y * d)};
            dl_->AddPolyline(points, 5, c, ImDrawFlags_Closed, ui::metrics::stroke(1.2f));
        } else {
            dl_->AddCircleFilled(q, ui::metrics::px(1.6f), c, 8);
        }
        const ImVec2 textPos(end.x + dir.x * ui::metrics::U(0.8f),
                             end.y + dir.y * ui::metrics::U(0.8f) -
                                 ImGui::GetTextLineHeight() * 0.5f);
        dl_->AddText(textPos, c, label.text.c_str());
        if (selected("label", label.id)) {
            const ImVec2 size = ImGui::CalcTextSize(label.text.c_str());
            const float halo = ui::metrics::px(2.0f);
            dl_->AddRect(ImVec2(textPos.x - halo, textPos.y - halo),
                         ImVec2(textPos.x + size.x + halo, textPos.y + size.y + halo),
                         selectionInner, 0.0f, 0, ui::metrics::stroke(1.5f));
        }
    }

    for (const auto& power : doc.powers()) {
        if (!doc.onActiveSheet(power.sheet)) continue;
        const std::string& net = netOf(power.id, power.net);
        if (!netVisible(net)) continue;
        const ImVec2 q = toScreen(power.pos);
        const ImU32 c = themedNetColor(p.statusWarn, net, 0.95f);
        const float r = ui::metrics::U(1.8f);
        const bool ground = containsNoCase(power.symbol, "gnd") ||
                            containsNoCase(net, "gnd") ||
                            containsNoCase(power.symbol, "ground") ||
                            containsNoCase(net, "ground") ||
                            containsNoCase(power.symbol, "vss") || containsNoCase(net, "vss");
        const bool specialGround = containsNoCase(power.symbol, "agnd") ||
                                   containsNoCase(net, "agnd") ||
                                   containsNoCase(power.symbol, "dgnd") ||
                                   containsNoCase(net, "dgnd") ||
                                   containsNoCase(power.symbol, "analog") ||
                                   containsNoCase(net, "analog") ||
                                   containsNoCase(power.symbol, "digital") ||
                                   containsNoCase(net, "digital");
        dl_->AddLine(q, ImVec2(q.x, q.y + r), c, bodyStroke);
        if (ground && specialGround) {
            dl_->AddTriangle(ImVec2(q.x - r, q.y + r),
                             ImVec2(q.x + r, q.y + r),
                             ImVec2(q.x, q.y + r * 2.7f), c, bodyStroke);
        } else if (ground) {
            for (int i = 0; i < 3; ++i) {
                const float y = q.y + r * (1.0f + i * 0.55f);
                const float half = r * (1.0f - i * 0.30f);
                dl_->AddLine(ImVec2(q.x - half, y), ImVec2(q.x + half, y), c, bodyStroke);
            }
        } else {
            dl_->AddTriangle(ImVec2(q.x, q.y - r * 1.5f),
                             ImVec2(q.x - r, q.y + r * 0.2f),
                             ImVec2(q.x + r, q.y + r * 0.2f), c, bodyStroke);
        }
        if (!net.empty())
            dl_->AddText(ImVec2(q.x + r + ui::metrics::U(0.5f),
                                q.y - ImGui::GetTextLineHeight() * 0.5f),
                         c, net.c_str());
        selectedPoint("power", power.id, power.pos);
    }

    for (const auto& nc : doc.noConnects()) {
        if (!doc.onActiveSheet(nc.sheet)) continue;
        const ImVec2 q = toScreen(nc.pos);
        const float r = ui::metrics::U(1.0f);
        const ImU32 c = staticColor(p.statusWarn);
        dl_->AddLine(ImVec2(q.x - r, q.y - r), ImVec2(q.x + r, q.y + r), c,
                     ui::metrics::stroke(1.6f));
        dl_->AddLine(ImVec2(q.x - r, q.y + r), ImVec2(q.x + r, q.y - r), c,
                     ui::metrics::stroke(1.6f));
        selectedPoint("nc", nc.id, nc.pos);
    }
}

// ---- board ---------------------------------------------------------------------------------

void EcadCanvas::drawPcb(const EcadMirror& doc) {
    const ui::Palette& p = ui::palette;
    const ImU32 outlineCol = col(p.statusWarn);
    const ImU32 courtCol   = col(p.textDim, 0.70f);
    const ImU32 drillCol   = col(p.windowBg);
    const ImU32 refCol     = col(p.text, 0.85f);


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
        if (selected("fp", fp.id)) return EcadPt{dragDx_, dragDy_};
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
        if (selected("zone", zone.id)) {
            for (size_t i = 0; i < poly.size(); ++i) {
                dl_->AddLine(poly[i], poly[(i + 1) % poly.size()],
                             col(p.selectionHighlight), ui::metrics::stroke(2.5f));
            }
        }
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
            if (selected("track", track->id)) {
                drawTrack(*track, col(p.selectionHighlight, 0.58f),
                          trackStroke(*track, ui::metrics::px(5.0f)));
                drawTrack(*track, col(p.selectionHighlight),
                          trackStroke(*track, ui::metrics::px(2.0f)));
            }
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
            if (selected("pad", fp.id, pad.number) || selected("pad", fp.id))
                drawPadShape(pad, center, col(p.selectionHighlight, 0.40f),
                             col(p.selectionHighlight), ui::metrics::stroke(2.0f));

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
        if (selected("via", via.id)) {
            dl_->AddCircle(center,
                           std::max(ui::metrics::px(5.0f),
                                    via.diameter * 0.5f * pxPerMm_ + ui::metrics::px(2.0f)),
                           col(p.selectionHighlight), 28, ui::metrics::stroke(2.0f));
        }
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

    const ImU32 selectionOuter = col(p.selectionHighlight, 0.62f);
    const ImU32 selectionInner = col(p.selectionHighlight);
    for (const auto& fp : doc.footprints()) {
        if (!selected("fp", fp.id)) continue;
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
        if (!haveBounds) continue;
        ImVec2 a = toScreen({mn.x, mx.y});
        ImVec2 b = toScreen({mx.x, mn.y});
        const float pad = ui::metrics::px(2.0f);
        a.x -= pad;
        a.y -= pad;
        b.x += pad;
        b.y += pad;
        dl_->AddRect(a, b, selectionOuter, 0.0f, 0, ui::metrics::stroke(4.0f));
        dl_->AddRect(a, b, selectionInner, 0.0f, 0, ui::metrics::stroke(1.5f));
    }
}

void EcadCanvas::drawOverlays(const EcadMirror& doc) {
    const ui::Palette& p = ui::palette;
    if (showErcMarkers_ && doc.isSch()) {
        for (const auto& violation : doc.erc()) {
            if (!violation.hasLoc) continue;
            const bool isError = violation.severity == "error";
            const ImU32 c = isError ? col(p.statusErr, 0.95f)
                                    : col(p.statusWarn, 0.95f);
            drawViolationGlyph(dl_, toScreen(violation.loc), isError, c,
                               ui::metrics::U(2.2f));
        }
    }

    if (showDrcMarkers_ && doc.isPcb()) {
        // Persistent DRC markers — the board-side twin of the schematic ERC markers above:
        // same severity glyphs, same palette roles, same constant screen-space size, drawn
        // at the same overlay z (above copper, below the focus flash). Violations on a
        // hidden copper layer hide with it, exactly like the tracks they flag.
        //
        // A pair violation (clearance is BETWEEN two shapes) reports one location per
        // offender: a glyph marks EVERY location and a thin hairline joins the first two.
        // Marking only the midpoint would often land on bare substrate and point at
        // neither offending shape.
        //
        // Density: a real board can carry hundreds of violations. Errors paint first, and
        // a glyph landing within one glyph-radius of one already painted this frame is
        // skipped — stacked identical glyphs add overdraw, not information, and errors
        // must never be buried under warnings. The dedupe scratch is a reused member, so
        // the steady state allocates nothing; the quadratic scan only walks glyphs that
        // actually painted, which the dedupe itself keeps small when zoomed out.
        drcMarkerPts_.clear();
        const float r = ui::metrics::U(2.2f);
        const float minSepSq = r * r;
        const float cullMinX = imgMin_.x - r, cullMinY = imgMin_.y - r;
        const float cullMaxX = imgMin_.x + imgSize_.x + r;
        const float cullMaxY = imgMin_.y + imgSize_.y + r;
        for (int pass = 0; pass < 2; ++pass) {
            const bool wantError = pass == 0;
            for (const auto& violation : doc.drc()) {
                if (!violation.hasLoc) continue;
                const bool isError = violation.severity == "error";
                if (isError != wantError) continue;
                if (!violation.layer.empty() && !doc.layerVisible(violation.layer))
                    continue;
                const float* role = isError ? p.statusErr : p.statusWarn;
                if (violation.locs.size() >= 2) {
                    dl_->AddLine(toScreen(violation.locs[0]),
                                 toScreen(violation.locs[1]), col(role, 0.55f),
                                 ui::metrics::stroke(1.0f));
                }
                const ImU32 c = col(role, 0.95f);
                const EcadPt* pts = violation.locs.empty() ? &violation.loc
                                                           : violation.locs.data();
                const size_t n = violation.locs.empty() ? 1 : violation.locs.size();
                for (size_t i = 0; i < n; ++i) {
                    const ImVec2 q = toScreen(pts[i]);
                    if (q.x < cullMinX || q.x > cullMaxX ||
                        q.y < cullMinY || q.y > cullMaxY)
                        continue;
                    bool crowded = false;
                    for (const ImVec2& drawn : drcMarkerPts_) {
                        const float dx = q.x - drawn.x, dy = q.y - drawn.y;
                        if (dx * dx + dy * dy < minSepSq) { crowded = true; break; }
                    }
                    if (crowded) continue;
                    drcMarkerPts_.push_back(q);
                    drawViolationGlyph(dl_, q, isError, c, r);
                }
            }
        }
    }

    if (marqueeActive_) {
        const ImVec2 a = toScreen(marqueeA_), b = toScreen(marqueeB_);
        const ImVec2 mn(std::min(a.x, b.x), std::min(a.y, b.y));
        const ImVec2 mx(std::max(a.x, b.x), std::max(a.y, b.y));
        const ImU32 c = col(p.selectionHighlight, 0.95f);
        const float th = ui::metrics::stroke(1.5f);
        const float dash = ui::metrics::U(1.2f);
        const float gap = ui::metrics::U(0.8f);
        drawDashedLine(dl_, mn, ImVec2(mx.x, mn.y), c, th, dash, gap);
        drawDashedLine(dl_, ImVec2(mx.x, mn.y), mx, c, th, dash, gap);
        drawDashedLine(dl_, mx, ImVec2(mn.x, mx.y), c, th, dash, gap);
        drawDashedLine(dl_, ImVec2(mn.x, mx.y), mn, c, th, dash, gap);
    }

    if (routePreview_.size() >= 2) {
        const ImU32 c = col(routePreviewBus_ ? p.sketchUnder : p.selectionHighlight, 0.95f);
        const float th = routePreviewBus_ ? ui::metrics::stroke(3.2f)
                                          : ui::metrics::stroke(1.8f);
        const float dash = ui::metrics::U(1.4f);
        const float gap = ui::metrics::U(0.8f);
        for (size_t i = 1; i < routePreview_.size(); ++i) {
            const EcadPt a = routePreview_[i - 1];
            const EcadPt b = routePreview_[i];
            if (std::fabs(a.x - b.x) < 1e-5f || std::fabs(a.y - b.y) < 1e-5f) {
                drawDashedLine(dl_, toScreen(a), toScreen(b), c, th, dash, gap);
            } else {
                const EcadPt elbow{b.x, a.y};
                drawDashedLine(dl_, toScreen(a), toScreen(elbow), c, th, dash, gap);
                drawDashedLine(dl_, toScreen(elbow), toScreen(b), c, th, dash, gap);
            }
        }
    }

    if (snapTargetActive_) {
        const ImVec2 q = toScreen(snapTarget_);
        const ImU32 c = col(p.selectionHighlight);
        dl_->AddCircle(q, ui::metrics::px(6.0f), c, 24, ui::metrics::stroke(2.0f));
        dl_->AddCircleFilled(q, ui::metrics::px(1.8f), c, 10);
        if (!snapTargetLabel_.empty()) {
            const ImVec2 size = ImGui::CalcTextSize(snapTargetLabel_.c_str());
            const ImVec2 pos(q.x + ui::metrics::U(1.5f), q.y - size.y - ui::metrics::U(1.0f));
            const float xPad = ui::metrics::px(2.0f);
            const float yPad = ui::metrics::px(1.0f);
            dl_->AddRectFilled(ImVec2(pos.x - xPad, pos.y - yPad),
                               ImVec2(pos.x + size.x + xPad, pos.y + size.y + yPad),
                               col(p.windowBg, 0.88f), ui::metrics::px(2.0f));
            dl_->AddText(pos, c, snapTargetLabel_.c_str());
        }
    }

    if (focusFrames_ > 0) {
        const ImVec2 q = toScreen(focusLoc_);
        const float phase = static_cast<float>(180 - focusFrames_);
        const float r = ui::metrics::px(11.0f + 5.0f * std::sin(phase * 0.18f));
        const ImU32 c = focusIsError_ ? col(p.statusErr, 0.95f)
                                      : col(p.statusWarn, 0.95f);
        dl_->AddCircle(q, r, c, 28, ui::metrics::stroke(2.5f));
        dl_->AddLine(ImVec2(q.x - r - ui::metrics::U(1.0f), q.y),
                     ImVec2(q.x + r + ui::metrics::U(1.0f), q.y), c,
                     ui::metrics::stroke(1.5f));
        dl_->AddLine(ImVec2(q.x, q.y - r - ui::metrics::U(1.0f)),
                     ImVec2(q.x, q.y + r + ui::metrics::U(1.0f)), c,
                     ui::metrics::stroke(1.5f));
        --focusFrames_;
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

    ImGui::InvisibleButton("##ecadCanvas", avail,
                           ImGuiButtonFlags_MouseButtonLeft | ImGuiButtonFlags_MouseButtonRight |
                           ImGuiButtonFlags_MouseButtonMiddle);
    ImGuiIO& gio = ImGui::GetIO();
    io.hovered = ImGui::IsItemHovered();
    io.ctrl = gio.KeyCtrl;
    io.shift = gio.KeyShift;
    io.alt = gio.KeyAlt;
    if (io.hovered) {
        io.worldMouse = toWorld(gio.MousePos);
        io.hasWorldMouse = true;
        io.leftDoubleClicked = ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left);
        io.rightClicked = ImGui::IsMouseClicked(ImGuiMouseButton_Right);
        io.middleClicked = ImGui::IsMouseClicked(ImGuiMouseButton_Middle);
    }

    if (io.hovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
        leftGestureActive_ = true;
        leftGestureDragged_ = false;
        leftDragStartWorld_ = toWorld(gio.MousePos);
    }
    if (leftGestureActive_) {
        io.dragStartWorld = leftDragStartWorld_;
        if (ImGui::IsMouseDragging(ImGuiMouseButton_Left)) {
            if (!leftGestureDragged_) {
                leftGestureDragged_ = true;
                io.leftDragStarted = true;
            }
            io.leftDragging = true;
        }
        if (ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
            if (leftGestureDragged_) io.leftDragEnded = true;
            else if (io.hovered) io.leftClicked = true;
            leftGestureActive_ = false;
            leftGestureDragged_ = false;
        }
    }

    handleNavigation(io, avail);
    ImVec2 drawClipMin = imgMin_;
    ImVec2 drawClipMax(imgMin_.x + avail.x, imgMin_.y + avail.y);
    const float managerEdge = ui::overlayColumnEdge(0);
    const float copilotEdge = ui::overlayColumnEdge(1);
    if (managerEdge >= imgMin_.x && managerEdge <= drawClipMax.x)
        drawClipMin.x = managerEdge;
    if (copilotEdge >= drawClipMin.x && copilotEdge <= drawClipMax.x)
        drawClipMax.x = copilotEdge;
    dl_->PushClipRect(drawClipMin, drawClipMax, true);
    drawGrid(doc, avail);
    if (doc.isSch()) drawSch(doc);
    else if (doc.isPcb()) drawPcb(doc);
    drawOverlays(doc);

    dl_->PopClipRect();
    dl_ = nullptr;
    return io;
}

}  // namespace ui
