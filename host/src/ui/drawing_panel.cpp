// AI-CAD Drawing panel — 2D sheet renderer. See drawing_panel.h.
//
// The drawing engine hands us, per sheet: size_mm:[w,h] (the paper) and a list of ViewGeometry, each
// with `visible` (solid) and `hidden` (dashed) polylines already scaled + translated to sheet-mm at
// the view's `position`. So all this panel does is paper -> screen: a single affine (uniform scale
// pxPerMm + screen pan), then stroke the polylines with ImDrawList. We add a paper border, a faint
// drop shadow, per-view bounding boxes + labels, and a small "Rebuild" / fit affordance.
#include <algorithm>
#include <cmath>
#include <string>
#include <vector>

#include "imgui.h"

#include "drawing_panel.h"

#include "../core/app_state.h"
#include "../ui_theme.h"

namespace ui {

// --- safe JSON readers (tolerate missing keys / null) ---------------------------------------
static double djnum(const nlohmann::json& o, const char* k, double def = 0.0) {
    auto it = o.find(k);
    return (it != o.end() && it->is_number()) ? it->get<double>() : def;
}
static std::string djstr(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_string()) ? it->get<std::string>() : std::string();
}

// A polyline as a dashed stroke (for hidden lines). dashPx / gapPx are in SCREEN pixels so the
// dash pattern stays constant regardless of zoom.
static void addDashedPolyline(ImDrawList* dl, const std::vector<ImVec2>& pts, ImU32 col,
                              float thick, float dashPx, float gapPx) {
    if (pts.size() < 2) return;
    float phase = 0.0f;  // distance into the current dash/gap cycle
    const float period = dashPx + gapPx;
    for (size_t i = 1; i < pts.size(); ++i) {
        ImVec2 a = pts[i - 1], b = pts[i];
        float segLen = std::sqrt((b.x - a.x) * (b.x - a.x) + (b.y - a.y) * (b.y - a.y));
        if (segLen < 1e-4f) continue;
        float dx = (b.x - a.x) / segLen, dy = (b.y - a.y) / segLen;
        float d = 0.0f;
        while (d < segLen) {
            // position within the period: [0,dashPx) draws, [dashPx,period) is the gap.
            float local = std::fmod(phase, period);
            bool inDash = local < dashPx;
            float remain = inDash ? (dashPx - local) : (period - local);
            float step = std::min(remain, segLen - d);
            if (inDash) {
                ImVec2 p0(a.x + dx * d, a.y + dy * d);
                ImVec2 p1(a.x + dx * (d + step), a.y + dy * (d + step));
                dl->AddLine(p0, p1, col, thick);
            }
            d += step;
            phase += step;
        }
    }
}

// Compute the combined sheet-mm bounds: the paper rect plus every view's bbox (so even views that
// overshoot the paper are framed by "fit"). Returns false if nothing measurable.
static bool sheetBounds(const nlohmann::json& sheet, float& minx, float& miny,
                        float& maxx, float& maxy) {
    bool any = false;
    auto grow = [&](float x, float y) {
        if (!any) { minx = maxx = x; miny = maxy = y; any = true; }
        else { minx = std::min(minx, x); miny = std::min(miny, y);
               maxx = std::max(maxx, x); maxy = std::max(maxy, y); }
    };
    // Paper rectangle.
    if (sheet.contains("size_mm") && sheet["size_mm"].is_array() && sheet["size_mm"].size() == 2) {
        double w = sheet["size_mm"][0].is_number() ? sheet["size_mm"][0].get<double>() : 0.0;
        double h = sheet["size_mm"][1].is_number() ? sheet["size_mm"][1].get<double>() : 0.0;
        if (w > 0 && h > 0) { grow(0, 0); grow((float)w, (float)h); }
    }
    // View bounds.
    if (sheet.contains("views") && sheet["views"].is_array()) {
        for (const auto& v : sheet["views"]) {
            if (v.contains("bbox") && v["bbox"].is_array() && v["bbox"].size() == 4) {
                const auto& b = v["bbox"];
                if (b[0].is_number() && b[1].is_number() && b[2].is_number() && b[3].is_number())
                    { grow((float)b[0].get<double>(), (float)b[1].get<double>());
                      grow((float)b[2].get<double>(), (float)b[3].get<double>()); }
            }
        }
    }
    return any;
}

void drawDrawingSheet(AppState& st) {
    const auto& pal = ui::palette;
    const float s = ui::uiScale;

    if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) return;
    AppState::DocTab& tab = st.docTabs[st.activeTab];

    // ---- header strip: title + Rebuild + Fit ----
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1));
    ImGui::TextUnformatted("Drawing");
    ImGui::PopStyleColor();
    ImGui::SameLine();
    if (ImGui::SmallButton("Rebuild")) {
        // Re-fetch from the engine (it reprojects from the live source part).
        st.refreshDrawingTab(st.activeTab);
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Re-project from the source part (picks up edits)");
    ImGui::SameLine();
    if (ImGui::SmallButton("Fit")) tab.drawNeedFit = true;
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1));
    ImGui::Text("  wheel: zoom   MMB / Space+drag: pan");
    ImGui::PopStyleColor();

    // ---- canvas child ----
    ImVec2 canvasMin = ImGui::GetCursorScreenPos();
    ImVec2 avail = ImGui::GetContentRegionAvail();
    if (avail.x < 32) avail.x = 32;
    if (avail.y < 32) avail.y = 32;
    ImGui::InvisibleButton("##drawcanvas", avail,
                           ImGuiButtonFlags_MouseButtonLeft | ImGuiButtonFlags_MouseButtonMiddle);
    bool canvasHovered = ImGui::IsItemHovered();
    ImVec2 canvasMax(canvasMin.x + avail.x, canvasMin.y + avail.y);

    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->PushClipRect(canvasMin, canvasMax, true);
    // Canvas backdrop (a touch darker than the panel so the white paper reads as paper).
    dl->AddRectFilled(canvasMin, canvasMax, ui::Palette::toU32(pal.headerBg));

    // The active sheet: first sheet of the cached drawing_get (the engine makes one A3 sheet "s1").
    const nlohmann::json* sheet = nullptr;
    if (tab.drawingDoc.is_object() && tab.drawingDoc.contains("sheets") &&
        tab.drawingDoc["sheets"].is_array() && !tab.drawingDoc["sheets"].empty() &&
        tab.drawingDoc["sheets"][0].is_object())
        sheet = &tab.drawingDoc["sheets"][0];

    if (!sheet) {
        const char* msg = "No sheet to display - try Rebuild.";
        ImVec2 ts = ImGui::CalcTextSize(msg);
        dl->AddText(ImVec2((canvasMin.x + canvasMax.x) * 0.5f - ts.x * 0.5f,
                           (canvasMin.y + canvasMax.y) * 0.5f - ts.y * 0.5f),
                    ui::Palette::toU32(pal.textDim), msg);
        dl->PopClipRect();
        return;
    }

    double paperW = 0, paperH = 0;
    if (sheet->contains("size_mm") && (*sheet)["size_mm"].is_array() &&
        (*sheet)["size_mm"].size() == 2) {
        paperW = (*sheet)["size_mm"][0].is_number() ? (*sheet)["size_mm"][0].get<double>() : 0;
        paperH = (*sheet)["size_mm"][1].is_number() ? (*sheet)["size_mm"][1].get<double>() : 0;
    }
    if (paperW <= 0 || paperH <= 0) { paperW = 420.0; paperH = 297.0; }  // A3 fallback

    // ---- one-shot fit: frame the combined bounds with a margin ----
    if (tab.drawNeedFit || tab.drawZoom <= 0.0f) {
        float bx0, by0, bx1, by1;
        if (!sheetBounds(*sheet, bx0, by0, bx1, by1)) {
            bx0 = 0; by0 = 0; bx1 = (float)paperW; by1 = (float)paperH;
        }
        float bw = std::max(1.0f, bx1 - bx0), bh = std::max(1.0f, by1 - by0);
        const float margin = 0.92f;  // leave a little air around the paper
        float zx = (avail.x * margin) / bw;
        float zy = (avail.y * margin) / bh;
        tab.drawZoom = std::min(zx, zy);
        if (tab.drawZoom <= 0.0f || !std::isfinite(tab.drawZoom)) tab.drawZoom = 1.0f;
        // Center the bounds in the canvas. screen = canvasMin + pan + mm*zoom*(x, -y inverted).
        // We flip Y so the engine's +Y-up sheet maps to screen +Y-down.
        float cxMm = (bx0 + bx1) * 0.5f, cyMm = (by0 + by1) * 0.5f;
        tab.drawPanX = avail.x * 0.5f - cxMm * tab.drawZoom;
        tab.drawPanY = avail.y * 0.5f + cyMm * tab.drawZoom;  // +cy because of the Y flip
        tab.drawNeedFit = false;
    }

    // ---- wheel zoom to cursor ----
    if (canvasHovered) {
        float wheel = ImGui::GetIO().MouseWheel;
        if (wheel != 0.0f) {
            float old = tab.drawZoom;
            float factor = std::pow(1.15f, wheel);
            float nz = old * factor;
            nz = std::clamp(nz, 0.02f, 200.0f);
            // Keep the sheet-mm point under the cursor fixed: solve pan so that mapping is stable.
            ImVec2 mouse = ImGui::GetIO().MousePos;
            float lx = mouse.x - canvasMin.x;   // cursor in canvas-local px
            float ly = mouse.y - canvasMin.y;
            // mm under cursor (inverse of the forward map below).
            float mmx = (lx - tab.drawPanX) / old;
            float mmy = -(ly - tab.drawPanY) / old;
            tab.drawZoom = nz;
            tab.drawPanX = lx - mmx * nz;
            tab.drawPanY = ly + mmy * nz;
        }
    }

    // ---- pan: MMB drag, or Space + LMB drag ----
    bool spaceDown = ImGui::IsKeyDown(ImGuiKey_Space);
    bool panning = ImGui::IsMouseDragging(ImGuiMouseButton_Middle) ||
                   (spaceDown && ImGui::IsMouseDragging(ImGuiMouseButton_Left));
    if (panning && (canvasHovered || ImGui::IsItemActive())) {
        ImVec2 d = ImGui::GetIO().MouseDelta;
        tab.drawPanX += d.x;
        tab.drawPanY += d.y;
    }

    const float z = tab.drawZoom;
    const float panX = tab.drawPanX, panY = tab.drawPanY;
    // Forward map: sheet-mm (x,y) -> screen px. Y flipped (engine +Y up, screen +Y down).
    auto toScreen = [&](double mx, double my) -> ImVec2 {
        return ImVec2(canvasMin.x + panX + (float)mx * z,
                      canvasMin.y + panY - (float)my * z);
    };

    // ---- paper: drop shadow + white fill + border ----
    {
        ImVec2 p0 = toScreen(0, paperH);          // top-left of paper in screen (maxY -> top)
        ImVec2 p1 = toScreen(paperW, 0);          // bottom-right
        ImVec2 sh0(p0.x + 4 * s, p0.y + 4 * s), sh1(p1.x + 4 * s, p1.y + 4 * s);
        dl->AddRectFilled(sh0, sh1, IM_COL32(0, 0, 0, 60), 0.0f);
        dl->AddRectFilled(p0, p1, IM_COL32(252, 252, 250, 255), 0.0f);  // paper is always white
        dl->AddRect(p0, p1, IM_COL32(60, 64, 74, 255), 0.0f, 0, 1.4f * s);
        // Inner trim border (a faint margin line, SolidWorks-style).
        float m = 8.0f * z;  // 8mm margin scaled
        if (m > 4)
            dl->AddRect(ImVec2(p0.x + m, p0.y + m), ImVec2(p1.x - m, p1.y - m),
                        IM_COL32(150, 154, 164, 200), 0.0f, 0, 1.0f * s);
    }

    // Line colors: drawings are ink-on-paper, so use fixed dark inks (independent of theme — the
    // paper is always white) for legibility. Hidden lines are a lighter grey + dashed.
    const ImU32 inkVisible = IM_COL32(28, 32, 40, 255);
    const ImU32 inkHidden  = IM_COL32(120, 126, 138, 230);
    const float visThick = std::max(1.0f, 1.1f * s);
    const float hidThick = std::max(1.0f, 1.0f * s);

    // ---- views ----
    int viewIdx = 0;
    if (sheet->contains("views") && (*sheet)["views"].is_array()) {
        for (const auto& v : (*sheet)["views"]) {
            ++viewIdx;
            if (!v.is_object()) continue;

            std::string err = djstr(v, "error");
            std::string orient = djstr(v, "orientation");
            double scale = djnum(v, "scale", 1.0);

            // bbox (sheet-mm) for the label + failure note + view frame.
            bool haveBox = v.contains("bbox") && v["bbox"].is_array() && v["bbox"].size() == 4 &&
                           v["bbox"][0].is_number();
            ImVec2 boxTL, boxBR;  // screen
            double bMinX = 0, bMinY = 0, bMaxX = 0, bMaxY = 0;
            if (haveBox) {
                bMinX = v["bbox"][0].get<double>(); bMinY = v["bbox"][1].get<double>();
                bMaxX = v["bbox"][2].get<double>(); bMaxY = v["bbox"][3].get<double>();
                boxTL = toScreen(bMinX, bMaxY);  // maxY -> top
                boxBR = toScreen(bMaxX, bMinY);
            }

            // A failed / empty view: render nothing but a small note anchored at its position/bbox.
            auto visEmpty = [&]() {
                if (!v.contains("visible") || !v["visible"].is_array()) return true;
                for (const auto& pl : v["visible"])
                    if (pl.is_array() && pl.size() >= 2) return false;
                return true;
            };
            if (!err.empty() || visEmpty()) {
                ImVec2 anchor;
                if (haveBox) anchor = ImVec2((boxTL.x + boxBR.x) * 0.5f, (boxTL.y + boxBR.y) * 0.5f);
                else if (v.contains("position") && v["position"].is_array() &&
                         v["position"].size() == 2 && v["position"][0].is_number())
                    anchor = toScreen(v["position"][0].get<double>(),
                                      v["position"][1].get<double>());
                else
                    anchor = ImVec2((canvasMin.x + canvasMax.x) * 0.5f,
                                    (canvasMin.y + canvasMax.y) * 0.5f);
                std::string note = (orient.empty() ? std::string("view") : orient) +
                                   (err.empty() ? ": (empty)" : ": view failed");
                ImVec2 ts = ImGui::CalcTextSize(note.c_str());
                ImVec2 t0(anchor.x - ts.x * 0.5f - 6, anchor.y - ts.y * 0.5f - 3);
                ImVec2 t1(t0.x + ts.x + 12, t0.y + ts.y + 6);
                dl->AddRectFilled(t0, t1, IM_COL32(250, 235, 235, 235), 3.0f);
                dl->AddRect(t0, t1, IM_COL32(200, 120, 110, 255), 3.0f);
                dl->AddText(ImVec2(t0.x + 6, t0.y + 3), IM_COL32(150, 60, 50, 255), note.c_str());
                continue;
            }

            // Stroke hidden polylines first (under the solid lines).
            if (v.contains("hidden") && v["hidden"].is_array()) {
                for (const auto& pl : v["hidden"]) {
                    if (!pl.is_array() || pl.size() < 2) continue;
                    std::vector<ImVec2> pts;
                    pts.reserve(pl.size());
                    for (const auto& pt : pl)
                        if (pt.is_array() && pt.size() == 2 && pt[0].is_number() && pt[1].is_number())
                            pts.push_back(toScreen(pt[0].get<double>(), pt[1].get<double>()));
                    addDashedPolyline(dl, pts, inkHidden, hidThick, 5.0f * s, 4.0f * s);
                }
            }
            // Then the solid visible polylines.
            if (v.contains("visible") && v["visible"].is_array()) {
                for (const auto& pl : v["visible"]) {
                    if (!pl.is_array() || pl.size() < 2) continue;
                    std::vector<ImVec2> pts;
                    pts.reserve(pl.size());
                    for (const auto& pt : pl)
                        if (pt.is_array() && pt.size() == 2 && pt[0].is_number() && pt[1].is_number())
                            pts.push_back(toScreen(pt[0].get<double>(), pt[1].get<double>()));
                    if (pts.size() >= 2)
                        dl->AddPolyline(pts.data(), (int)pts.size(), inkVisible, 0, visThick);
                }
            }

            // Per-view label: orientation + scale, just under the view bbox.
            if (haveBox) {
                char lbl[64];
                std::snprintf(lbl, sizeof(lbl), "%s  (%.2g:1)",
                              orient.empty() ? "view" : orient.c_str(), scale);
                ImVec2 lp(boxTL.x, boxBR.y + 4 * s);
                ImVec2 ts = ImGui::CalcTextSize(lbl);
                dl->AddRectFilled(ImVec2(lp.x - 2, lp.y - 1),
                                  ImVec2(lp.x + ts.x + 4, lp.y + ts.y + 2),
                                  IM_COL32(255, 255, 255, 210), 2.0f);
                dl->AddText(lp, IM_COL32(70, 76, 88, 255), lbl);
            }
        }
    }

    // ---- bottom-left HUD: zoom % + view count ----
    {
        int nViews = (sheet->contains("views") && (*sheet)["views"].is_array())
                         ? (int)(*sheet)["views"].size() : 0;
        char hud[96];
        std::snprintf(hud, sizeof(hud), "%s   %d view%s   %.0f%%",
                      djstr(*sheet, "id").c_str(), nViews, nViews == 1 ? "" : "s", z * 100.0f);
        ImVec2 ts = ImGui::CalcTextSize(hud);
        ImVec2 p0(canvasMin.x + 8 * s, canvasMax.y - ts.y - 8 * s);
        ImVec2 p1(p0.x + ts.x + 12, p0.y + ts.y + 6);
        dl->AddRectFilled(p0, p1, ui::Palette::toU32(pal.overlayBg), 4.0f);
        dl->AddText(ImVec2(p0.x + 6, p0.y + 3), ui::Palette::toU32(pal.overlayText), hud);
    }

    dl->PopClipRect();
}

}  // namespace ui
