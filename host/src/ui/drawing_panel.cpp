// AI-CAD Drawing panel — 2D sheet renderer. See drawing_panel.h.
//
// The drawing engine hands us, per sheet: size_mm:[w,h] (the paper) and a list of ViewGeometry, each
// with `visible` (solid) and `hidden` (dashed) polylines already scaled + translated to sheet-mm at
// the view's `position`. So all this panel does is paper -> screen: a single affine (uniform scale
// pxPerMm + screen pan), then stroke the polylines with ImDrawList. We add a paper border, a faint
// drop shadow, per-view bounding boxes + labels, and a small "Rebuild" / fit affordance.
#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <string>
#include <vector>

#include "imgui.h"

#include "drawing_panel.h"

#include "../core/app_state.h"
#include "../ui_theme.h"
#include "file_dialog.h"
#include "property_manager.h"   // pmFormatDim — dimension text follows MMGS/IPS like the rest of the UI

namespace ui {

// --- 2D export (DXF / PDF) for the active Drawing tab -----------------------------------------
// Path choice: a native Save-As dialog when available (remembers the Documents folder); when file
// dialogs are disabled (headless / AI_CAD_NO_FILE_DIALOGS) we fall back to writing next to nothing
// in the cwd as "<title>_drawing.<ext>" and toast the path. A future nicety: per-sheet export +
// remembering the last export dir. The engine writes the file via export_drawing.
static bool s_drawDialogsDisabled() {
    static const bool disabled = std::getenv("AI_CAD_NO_FILE_DIALOGS") != nullptr;
    return disabled;
}

static std::string s_sanitize(const std::string& in) {
    std::string out;
    for (char c : in) {
        if (c == ' ') out += '_';
        else if (std::isalnum((unsigned char)c) || c == '_' || c == '-' || c == '.') out += c;
    }
    if (out.empty()) out = "drawing";
    return out;
}

static void exportActiveDrawing(AppState& st, const char* fmt) {
    if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) return;
    AppState::DocTab& tab = st.docTabs[st.activeTab];
    if (tab.kind != AppState::DocTab::TabKind::Drawing || tab.drawingDocId.empty()) {
        st.toast(ui::ToastLevel::Warn, "Open a drawing first");
        return;
    }
    const bool isDxf = std::strcmp(fmt, "dxf") == 0;
    const std::string ext = isDxf ? "dxf" : "pdf";
    const std::string base = s_sanitize(tab.title.empty() ? "drawing" : tab.title) + "_drawing";

    std::string path;
    if (s_drawDialogsDisabled()) {
        // Headless fallback: write into the current working directory.
        path = base + "." + ext;
    } else {
        const char* filterDxf = "DXF (*.dxf)\0*.dxf\0All files (*.*)\0*.*\0";
        const char* filterPdf = "PDF (*.pdf)\0*.pdf\0All files (*.*)\0*.*\0";
        std::string suggest = base + "." + ext;
        path = ui::saveFileDialog(isDxf ? filterDxf : filterPdf, ext.c_str(), suggest.c_str());
        if (path.empty()) return;  // canceled
    }

    std::string err;
    std::string written = st.docMirror.exportDrawing(st.worker, tab.drawingDocId, path, fmt, err);
    if (!written.empty()) {
        st.addLog("[draw] exported " + ext + ": " + written);
        st.toast(ui::ToastLevel::Info, "Exported " + written);
    } else {
        st.addLog("[draw] export " + ext + " failed: " + err);
        st.toast(ui::ToastLevel::Error,
                 "Export failed: " + (err.empty() ? std::string("engine error") : err));
    }
}

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

// --- W13 dimension helpers --------------------------------------------------------------------

// Snap a sheet-mm query point to the NEAREST projected polyline VERTEX across all views (the line
// endpoints / arc sample points that the engine returns). `pickPx` is the screen-space snap radius;
// we convert it to sheet-mm via the live zoom. Returns true + fills (sx,sy) in sheet mm and the
// owning view id when a vertex is within range.
static bool snapToVertex(const nlohmann::json& sheet, double qx, double qy, double radiusMm,
                         double& sx, double& sy, std::string& viewId) {
    bool found = false;
    double best = radiusMm * radiusMm;
    if (!(sheet.contains("views") && sheet["views"].is_array())) return false;
    for (const auto& v : sheet["views"]) {
        if (!v.is_object()) continue;
        const std::string vid = djstr(v, "id").empty() ? djstr(v, "view_id") : djstr(v, "id");
        for (const char* layer : {"visible", "hidden"}) {
            if (!(v.contains(layer) && v[layer].is_array())) continue;
            for (const auto& pl : v[layer]) {
                if (!pl.is_array()) continue;
                for (const auto& pt : pl) {
                    if (!(pt.is_array() && pt.size() == 2 && pt[0].is_number() && pt[1].is_number()))
                        continue;
                    double px = pt[0].get<double>(), py = pt[1].get<double>();
                    double d2 = (px - qx) * (px - qx) + (py - qy) * (py - qy);
                    if (d2 < best) { best = d2; sx = px; sy = py; viewId = vid; found = true; }
                }
            }
        }
    }
    return found;
}

// W15: find which VIEW a sheet-mm query point falls inside (by bbox, with a small mm margin), so
// the section/detail tools can anchor a cutting line / detail circle to the parent view under the
// cursor. Returns the view id (empty when the point is over no view). A derived section/detail view
// can't itself be a section parent here, but the engine validates the parent anyway.
static std::string viewAtPoint(const nlohmann::json& sheet, double qx, double qy, double marginMm) {
    if (!(sheet.contains("views") && sheet["views"].is_array())) return {};
    std::string hit;
    double bestArea = 1e300;
    for (const auto& v : sheet["views"]) {
        if (!v.is_object() || !v.contains("bbox") || !v["bbox"].is_array() ||
            v["bbox"].size() != 4) continue;
        const auto& b = v["bbox"];
        if (!(b[0].is_number() && b[1].is_number() && b[2].is_number() && b[3].is_number())) continue;
        double x0 = b[0].get<double>() - marginMm, y0 = b[1].get<double>() - marginMm;
        double x1 = b[2].get<double>() + marginMm, y1 = b[3].get<double>() + marginMm;
        if (qx >= x0 && qx <= x1 && qy >= y0 && qy <= y1) {
            double area = (x1 - x0) * (y1 - y0);   // prefer the smallest enclosing view (detail over parent)
            if (area < bestArea) {
                bestArea = area;
                hit = djstr(v, "view_id").empty() ? djstr(v, "id") : djstr(v, "view_id");
            }
        }
    }
    return hit;
}

// Push a finished dimension to the engine (drawing_add_dim) and refresh the cached drawing_get.
static void commitDimension(AppState& st, AppState::DocTab& tab, const std::string& viewId,
                            float ax, float ay, float bx, float by, float px, float py) {
    nlohmann::json res;
    std::string err;
    nlohmann::json params = {
        {"doc_id", tab.drawingDocId},
        {"view_id", viewId},
        {"type", tab.dimType},
        {"a", nlohmann::json::array({ax, ay})},
        {"b", nlohmann::json::array({bx, by})},
        {"pos", nlohmann::json::array({px, py})},
    };
    if (st.worker.call("drawing_add_dim", params, res, err) && res.is_object() &&
        !res.contains("code")) {
        if (res.contains("drawing") && res["drawing"].is_object())
            tab.drawingDoc = res["drawing"];      // engine returned the full re-projected drawing
        else
            st.refreshDrawingTab(st.activeTab);
        st.addLog("[draw] added " + tab.dimType + " on " + viewId);
    } else {
        std::string msg = res.is_object() && res.contains("message") ? djstr(res, "message") : err;
        st.addLog("[draw] drawing_add_dim failed: " + msg);
        st.toast(ui::ToastLevel::Error, "Dimension failed: " + (msg.empty() ? "engine error" : msg));
    }
}

// Delete a stored annotation (drawing_remove_annotation) + refresh.
static void deleteAnnotation(AppState& st, AppState::DocTab& tab, const std::string& viewId,
                             const std::string& annId) {
    nlohmann::json res;
    std::string err;
    nlohmann::json params = {{"doc_id", tab.drawingDocId}, {"view_id", viewId}, {"ann_id", annId}};
    if (st.worker.call("drawing_remove_annotation", params, res, err) && res.is_object() &&
        !res.contains("code")) {
        st.refreshDrawingTab(st.activeTab);
        st.addLog("[draw] removed annotation " + annId);
    } else {
        st.addLog("[draw] drawing_remove_annotation failed: " + err);
    }
    tab.selAnnViewId.clear();
    tab.selAnnId.clear();
}

// W15: push a finished cutting line to the engine (drawing_add_section{cut_line}) + refresh.
static void commitSection(AppState& st, AppState::DocTab& tab, const std::string& parentViewId,
                          float ax, float ay, float bx, float by) {
    nlohmann::json res;
    std::string err;
    nlohmann::json params = {
        {"doc_id", tab.drawingDocId},
        {"parent_view_id", parentViewId},
        {"cut_line", nlohmann::json::array({nlohmann::json::array({ax, ay}),
                                            nlohmann::json::array({bx, by})})},
    };
    if (st.worker.call("drawing_add_section", params, res, err) && res.is_object() &&
        !res.contains("code")) {
        st.refreshDrawingTab(st.activeTab);
        st.addLog("[draw] added section on " + parentViewId);
        tab.drawNeedFit = true;   // frame the new section view
    } else {
        std::string msg = res.is_object() && res.contains("message") ? djstr(res, "message") : err;
        st.addLog("[draw] drawing_add_section failed: " + msg);
        st.toast(ui::ToastLevel::Error, "Section failed: " + (msg.empty() ? "engine error" : msg));
    }
}

// W15: push a finished detail circle to the engine (drawing_add_detail) + refresh.
static void commitDetail(AppState& st, AppState::DocTab& tab, const std::string& parentViewId,
                         float cx, float cy, float radius) {
    nlohmann::json res;
    std::string err;
    nlohmann::json params = {
        {"doc_id", tab.drawingDocId},
        {"parent_view_id", parentViewId},
        {"center", nlohmann::json::array({cx, cy})},
        {"radius", radius},
    };
    if (st.worker.call("drawing_add_detail", params, res, err) && res.is_object() &&
        !res.contains("code")) {
        st.refreshDrawingTab(st.activeTab);
        st.addLog("[draw] added detail on " + parentViewId);
        tab.drawNeedFit = true;
    } else {
        std::string msg = res.is_object() && res.contains("message") ? djstr(res, "message") : err;
        st.addLog("[draw] drawing_add_detail failed: " + msg);
        st.toast(ui::ToastLevel::Error, "Detail failed: " + (msg.empty() ? "engine error" : msg));
    }
}

// W15: hatch a closed section-face polygon with 45° lines (sheet-mm spacing), clipped to the
// polygon by even-odd scanline crossings. `toScreen` maps sheet-mm -> screen px. We sweep a family
// of lines x + y = c (i.e. 45°) across the polygon's mm bbox; for each we collect the polygon-edge
// crossings, sort along the line, and stroke inside/outside pairs.
static void hatchPolygon(ImDrawList* dl, const std::vector<std::array<float, 2>>& poly,
                         const std::function<ImVec2(double, double)>& toScreen, ImU32 col,
                         float thick, float spacingMm) {
    if (poly.size() < 3) return;
    float minx = poly[0][0], maxx = poly[0][0], miny = poly[0][1], maxy = poly[0][1];
    for (const auto& p : poly) {
        minx = std::min(minx, p[0]); maxx = std::max(maxx, p[0]);
        miny = std::min(miny, p[1]); maxy = std::max(maxy, p[1]);
    }
    // 45° hatch direction d=(1,1)/sqrt2; the family is parameterized by c = (y - x). For each line
    // y = x + c we intersect each polygon edge and pair up the crossings (even-odd fill).
    const float cMin = (miny - maxx), cMax = (maxy - minx);
    const float step = std::max(spacingMm, 0.5f) * 1.41421356f;  // perpendicular spacing -> c-step
    for (float c = cMin; c <= cMax; c += step) {
        // crossings: solve edge (p0->p1) against y = x + c.  param t where (p0.y + t*dy) = (p0.x + t*dx) + c
        std::vector<float> xs;  // x-coordinate of each crossing
        for (size_t i = 0; i < poly.size(); ++i) {
            const auto& p0 = poly[i];
            const auto& p1 = poly[(i + 1) % poly.size()];
            float dx = p1[0] - p0[0], dy = p1[1] - p0[1];
            float denom = dy - dx;                       // d/dt of (y - x)
            float num = c - (p0[1] - p0[0]);             // target (y-x)=c minus current at t=0
            if (std::fabs(denom) < 1e-6f) continue;      // edge parallel to the hatch line
            float t = num / denom;
            if (t < 0.0f || t > 1.0f) continue;
            xs.push_back(p0[0] + t * dx);
        }
        if (xs.size() < 2) continue;
        std::sort(xs.begin(), xs.end());
        for (size_t k = 0; k + 1 < xs.size(); k += 2) {
            float x0 = xs[k], x1 = xs[k + 1];
            // points on the line: y = x + c
            ImVec2 a = toScreen(x0, x0 + c);
            ImVec2 b = toScreen(x1, x1 + c);
            dl->AddLine(a, b, col, thick);
        }
    }
}

// Fetch the BOM for the active drawing (drawing_bom) into the tab cache.
static void fetchBom(AppState& st, AppState::DocTab& tab) {
    nlohmann::json res;
    std::string err;
    if (st.worker.call("drawing_bom", {{"doc_id", tab.drawingDocId}}, res, err) && res.is_object() &&
        !res.contains("code")) {
        tab.bomRows = res;
    } else {
        st.addLog("[draw] drawing_bom failed: " + err);
        tab.bomRows = nlohmann::json::object();
    }
    tab.bomFetched = true;
}

// Render one stored dimension annotation: extension lines from a & b, a dimension line through the
// text position, simple arrowheads, and the value text. `toScreen` maps sheet-mm to screen px.
static void renderDimension(ImDrawList* dl, const nlohmann::json& ann, bool ips,
                            const std::function<ImVec2(double, double)>& toScreen, float s,
                            bool selected) {
    auto rdvec = [&](const char* k, double& x, double& y) -> bool {
        auto it = ann.find(k);
        if (it == ann.end() || !it->is_array() || it->size() != 2 ||
            !(*it)[0].is_number() || !(*it)[1].is_number()) return false;
        x = (*it)[0].get<double>(); y = (*it)[1].get<double>(); return true;
    };
    const std::string atype = djstr(ann, "type");
    double ax, ay, bx, by, px, py;
    bool haveA = rdvec("a", ax, ay), haveB = rdvec("b", bx, by), haveP = rdvec("pos", px, py);

    const ImU32 dimCol = selected ? IM_COL32(220, 120, 30, 255) : IM_COL32(40, 90, 180, 255);
    const float thick = std::max(1.0f, 1.0f * s);

    // Note: just text at pos.
    if (atype == "note" || !haveA || !haveB) {
        if (haveP) {
            std::string txt = djstr(ann, "text");
            if (txt.empty()) txt = "note";
            ImVec2 tp = toScreen(px, py);
            dl->AddText(ImVec2(tp.x, tp.y), dimCol, txt.c_str());
        }
        return;
    }
    if (!haveP) { px = (ax + bx) * 0.5; py = (ay + by) * 0.5; }

    ImVec2 A = toScreen(ax, ay), B = toScreen(bx, by), P = toScreen(px, py);

    // The dimension line is parallel to A->B, passing through P. We project A and B onto that line so
    // the dimension line spans the measured extent at the text offset; extension lines connect the
    // real points to the dimension line ends.
    ImVec2 dir(B.x - A.x, B.y - A.y);
    float len = std::sqrt(dir.x * dir.x + dir.y * dir.y);
    if (len < 1e-3f) return;
    ImVec2 u(dir.x / len, dir.y / len);          // unit along A->B
    // Offset = component of (P - A) perpendicular to u, applied to both ends.
    ImVec2 pa(P.x - A.x, P.y - A.y);
    float along = pa.x * u.x + pa.y * u.y;
    ImVec2 perp(pa.x - along * u.x, pa.y - along * u.y);   // perpendicular vector A->dimline
    ImVec2 A2(A.x + perp.x, A.y + perp.y);
    ImVec2 B2(B.x + perp.x, B.y + perp.y);

    // Extension lines (with a small gap off the geometry).
    auto gapStart = [&](const ImVec2& from, const ImVec2& to) {
        ImVec2 d(to.x - from.x, to.y - from.y);
        float l = std::sqrt(d.x * d.x + d.y * d.y);
        if (l < 1e-3f) return from;
        float g = std::min(4.0f * s, l * 0.25f);
        return ImVec2(from.x + d.x / l * g, from.y + d.y / l * g);
    };
    dl->AddLine(gapStart(A, A2), A2, dimCol, thick);
    dl->AddLine(gapStart(B, B2), B2, dimCol, thick);
    // Dimension line.
    dl->AddLine(A2, B2, dimCol, thick);

    // Arrowheads at A2 and B2 pointing inward.
    auto arrow = [&](const ImVec2& tip, const ImVec2& other) {
        ImVec2 d(other.x - tip.x, other.y - tip.y);
        float l = std::sqrt(d.x * d.x + d.y * d.y);
        if (l < 1e-3f) return;
        d.x /= l; d.y /= l;
        ImVec2 n(-d.y, d.x);
        float ah = 7.0f * s, aw = 2.6f * s;
        ImVec2 base(tip.x + d.x * ah, tip.y + d.y * ah);
        dl->AddTriangleFilled(tip, ImVec2(base.x + n.x * aw, base.y + n.y * aw),
                              ImVec2(base.x - n.x * aw, base.y - n.y * aw), dimCol);
    };
    arrow(A2, B2);
    arrow(B2, A2);

    // Value text at P (centered above the dimension line).
    std::string txt = djstr(ann, "text");
    if (txt.empty()) {
        double val = djnum(ann, "value", 0.0);
        txt = pmFormatDim(val, ips);   // engine value is canonical mm; pmFormatDim handles IPS
    }
    ImVec2 ts = ImGui::CalcTextSize(txt.c_str());
    ImVec2 mid((A2.x + B2.x) * 0.5f, (A2.y + B2.y) * 0.5f);
    ImVec2 tp(mid.x - ts.x * 0.5f, mid.y - ts.y - 2 * s);
    dl->AddRectFilled(ImVec2(tp.x - 2, tp.y - 1), ImVec2(tp.x + ts.x + 2, tp.y + ts.y + 1),
                      IM_COL32(255, 255, 255, 220), 2.0f);
    dl->AddText(tp, dimCol, txt.c_str());
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
    // ---- 2D export: DXF (manufacturing) + PDF ----
    ImGui::SameLine();
    if (ImGui::SmallButton("Export DXF")) exportActiveDrawing(st, "dxf");
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Write this drawing's views to a DXF (layers VISIBLE + HIDDEN)");
    ImGui::SameLine();
    if (ImGui::SmallButton("Export PDF")) exportActiveDrawing(st, "pdf");
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Write this drawing's views to a vector PDF (1:1 mm)");

    // ---- W13: Dimension tool (arm/disarm) + type, and BOM toggle ----
    ImGui::SameLine();
    {
        bool armed = tab.dimToolActive;
        if (armed) ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.95f, 0.62f, 0.18f, 1.0f));
        if (ImGui::SmallButton(armed ? "Dimension*" : "Dimension")) {
            tab.dimToolActive = !tab.dimToolActive;
            tab.dimStage = 0;
            tab.dimViewId.clear();
            if (tab.dimToolActive) { tab.selAnnViewId.clear(); tab.selAnnId.clear(); }
        }
        if (armed) ImGui::PopStyleColor();
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Place a linear dimension: click two polyline vertices, then a 3rd "
                              "click sets the text position. Esc cancels.");
    }
    ImGui::SameLine();
    ImGui::SetNextItemWidth(86 * s);
    {
        const char* kinds[] = {"linear", "aligned"};
        int cur = (tab.dimType == "aligned_dim") ? 1 : 0;
        if (ImGui::Combo("##dimtype", &cur, kinds, 2))
            tab.dimType = (cur == 1) ? "aligned_dim" : "linear_dim";
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("linear = horizontal/vertical span; aligned = true point-to-point distance");

    // ---- W15: Section View tool ----
    ImGui::SameLine();
    {
        bool armed = tab.sectionToolActive;
        if (armed) ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.95f, 0.62f, 0.18f, 1.0f));
        if (ImGui::SmallButton(armed ? "Section*" : "Section")) {
            bool now = !tab.sectionToolActive;
            // Disarm the other modal tools (mutually exclusive).
            tab.dimToolActive = false; tab.detailToolActive = false;
            tab.sectionToolActive = now;
            tab.sectionStage = 0; tab.sectionViewId.clear();
            if (now) { tab.selAnnViewId.clear(); tab.selAnnId.clear(); }
        }
        if (armed) ImGui::PopStyleColor();
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Section View: click two points across a view to drop a cutting line; "
                              "the cut model is projected with the cut faces hatched. Esc cancels.");
    }
    // ---- W15: Detail View tool ----
    ImGui::SameLine();
    {
        bool armed = tab.detailToolActive;
        if (armed) ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.95f, 0.62f, 0.18f, 1.0f));
        if (ImGui::SmallButton(armed ? "Detail*" : "Detail")) {
            bool now = !tab.detailToolActive;
            tab.dimToolActive = false; tab.sectionToolActive = false;
            tab.detailToolActive = now;
            tab.detailStage = 0; tab.detailViewId.clear();
            if (now) { tab.selAnnViewId.clear(); tab.selAnnId.clear(); }
        }
        if (armed) ImGui::PopStyleColor();
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Detail View: click a center on a view, then drag to set the circle "
                              "radius; a magnified crop is created. Esc cancels.");
    }

    // BOM toggle — only meaningful for assembly source drawings; we fetch lazily and disable for
    // part drawings (the engine returns is_assembly=false + empty rows).
    ImGui::SameLine();
    if (ImGui::SmallButton(tab.bomVisible ? "BOM*" : "BOM")) {
        tab.bomVisible = !tab.bomVisible;
        if (tab.bomVisible && !tab.bomFetched) fetchBom(st, tab);
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Toggle the Bill of Materials table (assembly drawings list components)");

    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1));
    if (tab.dimToolActive || tab.sectionToolActive || tab.detailToolActive) {
        const char* stageMsg;
        if (tab.dimToolActive)
            stageMsg = tab.dimStage == 0 ? "  click vertex A"
                     : tab.dimStage == 1 ? "  click vertex B"
                                         : "  click to place text";
        else if (tab.sectionToolActive)
            stageMsg = tab.sectionStage == 0 ? "  click the cutting-line start across a view"
                                             : "  click the cutting-line end";
        else
            stageMsg = tab.detailStage == 0 ? "  click the detail-circle center on a view"
                                            : "  move to set the radius, click to finish";
        ImGui::PopStyleColor();
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.95f, 0.62f, 0.18f, 1.0f));
        ImGui::TextUnformatted(stageMsg);
    } else {
        ImGui::Text("  wheel: zoom   MMB / Space+drag: pan   Del: delete dim");
    }
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

            // W15: section hatching — fill each section_face polygon with 45° lines (under the
            // edges, so the cut outline draws crisply on top). The bore shows as a gap because the
            // engine returns the cut face split into pieces around the hole.
            if (v.contains("section_faces") && v["section_faces"].is_array()) {
                const ImU32 hatchCol = IM_COL32(70, 90, 130, 200);
                for (const auto& poly : v["section_faces"]) {
                    if (!poly.is_array() || poly.size() < 3) continue;
                    std::vector<std::array<float, 2>> mm;
                    mm.reserve(poly.size());
                    for (const auto& pt : poly)
                        if (pt.is_array() && pt.size() == 2 && pt[0].is_number() && pt[1].is_number())
                            mm.push_back({(float)pt[0].get<double>(), (float)pt[1].get<double>()});
                    if (mm.size() < 3) continue;
                    // 45° hatch at ~3 mm spacing (sheet mm); thin lines.
                    hatchPolygon(dl, mm, toScreen, hatchCol, std::max(1.0f, 0.8f * s), 3.0f);
                    // Outline the cut face boundary so the hatched region reads as a solid cut.
                    std::vector<ImVec2> bnd;
                    bnd.reserve(mm.size());
                    for (const auto& p : mm) bnd.push_back(toScreen(p[0], p[1]));
                    dl->AddPolyline(bnd.data(), (int)bnd.size(), inkVisible,
                                    ImDrawFlags_Closed, visThick);
                }
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

            // W15: parent-view markers — the cutting line(s) and detail circle(s) the engine
            // attaches to a parent view in drawing_get, drawn over the geometry.
            const ImU32 markCol = IM_COL32(190, 60, 50, 235);
            const float markThick = std::max(1.2f, 1.3f * s);
            if (v.contains("section_lines") && v["section_lines"].is_array()) {
                for (const auto& sl : v["section_lines"]) {
                    if (!sl.is_object() || !sl.contains("line") || !sl["line"].is_array() ||
                        sl["line"].size() != 2) continue;
                    const auto& L = sl["line"];
                    if (!(L[0].is_array() && L[1].is_array())) continue;
                    ImVec2 a = toScreen(L[0][0].get<double>(), L[0][1].get<double>());
                    ImVec2 b = toScreen(L[1][0].get<double>(), L[1][1].get<double>());
                    addDashedPolyline(dl, {a, b}, markCol, markThick, 10.0f * s, 4.0f * s);
                    // End arrows + the section letter at both ends ("A").
                    std::string lab = djstr(sl, "label");
                    ImVec2 dir(b.x - a.x, b.y - a.y);
                    float len = std::sqrt(dir.x * dir.x + dir.y * dir.y);
                    if (len > 1e-3f) {
                        ImVec2 n(-dir.y / len, dir.x / len);   // perpendicular (arrow body)
                        auto endArrow = [&](const ImVec2& tip, float sgn) {
                            ImVec2 bdir(n.x * sgn, n.y * sgn);  // arrow points along the look dir
                            float ah = 10.0f * s, aw = 4.0f * s;
                            ImVec2 base(tip.x + bdir.x * ah, tip.y + bdir.y * ah);
                            ImVec2 u(dir.x / len, dir.y / len);
                            dl->AddTriangleFilled(tip,
                                ImVec2(base.x + u.x * aw, base.y + u.y * aw),
                                ImVec2(base.x - u.x * aw, base.y - u.y * aw), markCol);
                        };
                        endArrow(a, 1.0f);
                        endArrow(b, 1.0f);
                        if (!lab.empty()) {
                            dl->AddText(ImVec2(a.x - 6 * s, a.y - 16 * s), markCol, lab.c_str());
                            dl->AddText(ImVec2(b.x - 6 * s, b.y + 6 * s), markCol, lab.c_str());
                        }
                    }
                }
            }
            if (v.contains("detail_circles") && v["detail_circles"].is_array()) {
                for (const auto& dc : v["detail_circles"]) {
                    if (!dc.is_object() || !dc.contains("center") || !dc["center"].is_array() ||
                        dc["center"].size() != 2) continue;
                    double ccx = dc["center"][0].get<double>(), ccy = dc["center"][1].get<double>();
                    double rad = djnum(dc, "radius", 0.0);
                    ImVec2 c = toScreen(ccx, ccy);
                    float rpx = (float)rad * z;
                    dl->AddCircle(c, rpx, markCol, 48, markThick);
                    std::string lab = djstr(dc, "label");
                    if (!lab.empty()) {
                        ImVec2 lp(c.x + rpx * 0.7f, c.y - rpx * 0.7f - 14 * s);
                        dl->AddText(lp, markCol, lab.c_str());
                    }
                }
            }

            // Per-view label: SolidWorks-style title. Section/detail get "SECTION A-A" / "DETAIL B"
            // (with the scale); ortho/iso show the orientation + scale.
            if (haveBox) {
                char lbl[80];
                std::string vtype = djstr(v, "type");
                std::string vlabel = djstr(v, "label");
                if (vtype == "section" && !vlabel.empty())
                    std::snprintf(lbl, sizeof(lbl), "SECTION %s-%s  (%.2g:1)",
                                  vlabel.c_str(), vlabel.c_str(), scale);
                else if (vtype == "detail" && !vlabel.empty())
                    std::snprintf(lbl, sizeof(lbl), "DETAIL %s  (%.2g:1)", vlabel.c_str(), scale);
                else
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

    // ---- W13: render stored dimensions/notes (over the geometry) ----
    if (sheet->contains("views") && (*sheet)["views"].is_array()) {
        for (const auto& v : (*sheet)["views"]) {
            if (!v.is_object() || !v.contains("annotations") || !v["annotations"].is_array())
                continue;
            const std::string vid = djstr(v, "view_id").empty() ? djstr(v, "id")
                                                                 : djstr(v, "view_id");
            for (const auto& ann : v["annotations"]) {
                if (!ann.is_object()) continue;
                bool sel = (tab.selAnnViewId == vid && tab.selAnnId == djstr(ann, "id"));
                renderDimension(dl, ann, st.ips(), toScreen, s, sel);
            }
        }
    }

    // ---- W13: dimension tool interaction (snap + place) + annotation select/delete ----
    {
        // Esc cancels the in-progress dimension/section/detail (or disarms a fresh tool).
        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
            if (tab.dimToolActive) { tab.dimStage = 0; tab.dimViewId.clear(); }
            if (tab.sectionToolActive) { tab.sectionStage = 0; tab.sectionViewId.clear(); }
            if (tab.detailToolActive) { tab.detailStage = 0; tab.detailViewId.clear(); }
        }
        ImVec2 mouse = ImGui::GetIO().MousePos;
        bool overCanvas = canvasHovered;
        // Convert the cursor to sheet-mm (inverse of toScreen).
        double curMx = (mouse.x - canvasMin.x - panX) / z;
        double curMy = -(mouse.y - canvasMin.y - panY) / z;
        double snapRadiusMm = 9.0 / std::max(z, 1e-4f);   // ~9 px snap radius -> sheet mm

        // ---- W15: Section View tool (drop a 2-click cutting line across a parent view) ----
        if (tab.sectionToolActive && overCanvas) {
            std::string overView = viewAtPoint(*sheet, curMx, curMy, 8.0);
            // Rubber-band preview of the cutting line.
            if (tab.sectionStage == 1) {
                ImVec2 a = toScreen(tab.sectionAx, tab.sectionAy);
                addDashedPolyline(dl, {a, mouse}, IM_COL32(190, 60, 50, 200), 1.4f * s,
                                  10.0f * s, 4.0f * s);
            }
            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !spaceDown) {
                if (tab.sectionStage == 0) {
                    if (!overView.empty()) {
                        tab.sectionAx = (float)curMx; tab.sectionAy = (float)curMy;
                        tab.sectionViewId = overView; tab.sectionStage = 1;
                    } else {
                        st.toast(ui::ToastLevel::Warn, "Start the cutting line on a view");
                    }
                } else if (tab.sectionStage == 1) {
                    commitSection(st, tab, tab.sectionViewId, tab.sectionAx, tab.sectionAy,
                                  (float)curMx, (float)curMy);
                    tab.sectionStage = 0; tab.sectionViewId.clear();
                }
            }
        }
        // ---- W15: Detail View tool (click center, drag radius) ----
        else if (tab.detailToolActive && overCanvas) {
            std::string overView = viewAtPoint(*sheet, curMx, curMy, 8.0);
            if (tab.detailStage == 1) {
                ImVec2 c = toScreen(tab.detailCx, tab.detailCy);
                double rmm = std::hypot(curMx - tab.detailCx, curMy - tab.detailCy);
                dl->AddCircle(c, (float)rmm * z, IM_COL32(190, 60, 50, 220), 48, 1.4f * s);
                dl->AddCircleFilled(c, 2.5f * s, IM_COL32(190, 60, 50, 220));
            }
            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !spaceDown) {
                if (tab.detailStage == 0) {
                    if (!overView.empty()) {
                        tab.detailCx = (float)curMx; tab.detailCy = (float)curMy;
                        tab.detailViewId = overView; tab.detailStage = 1;
                    } else {
                        st.toast(ui::ToastLevel::Warn, "Click the detail center on a view");
                    }
                } else if (tab.detailStage == 1) {
                    double rmm = std::hypot(curMx - tab.detailCx, curMy - tab.detailCy);
                    if (rmm < 1.0) {
                        st.toast(ui::ToastLevel::Warn, "Drag out a larger detail radius");
                    } else {
                        commitDetail(st, tab, tab.detailViewId, tab.detailCx, tab.detailCy,
                                     (float)rmm);
                        tab.detailStage = 0; tab.detailViewId.clear();
                    }
                }
            }
        }
        else if (tab.dimToolActive && overCanvas) {
            // Live snap highlight.
            double sx, sy; std::string svid;
            bool snapped = snapToVertex(*sheet, curMx, curMy, snapRadiusMm, sx, sy, svid);
            if (snapped) {
                ImVec2 sp = toScreen(sx, sy);
                dl->AddCircle(sp, 5.0f * s, IM_COL32(220, 120, 30, 255), 12, 1.6f * s);
                dl->AddCircleFilled(sp, 2.0f * s, IM_COL32(220, 120, 30, 255));
            }
            // Rubber-band preview of the in-progress dimension.
            if (tab.dimStage == 1) {
                ImVec2 a = toScreen(tab.dimAx, tab.dimAy);
                ImVec2 b = snapped ? toScreen(sx, sy) : mouse;
                dl->AddLine(a, b, IM_COL32(220, 120, 30, 160), 1.2f * s);
            } else if (tab.dimStage == 2) {
                ImVec2 a = toScreen(tab.dimAx, tab.dimAy);
                ImVec2 b = toScreen(tab.dimBx, tab.dimBy);
                dl->AddLine(a, b, IM_COL32(220, 120, 30, 160), 1.2f * s);
            }
            // A left click advances the placement (ignore Space+drag pans).
            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !spaceDown) {
                if (tab.dimStage == 0) {
                    if (snapped) { tab.dimAx = (float)sx; tab.dimAy = (float)sy;
                                   tab.dimViewId = svid; tab.dimStage = 1; }
                } else if (tab.dimStage == 1) {
                    if (snapped) { tab.dimBx = (float)sx; tab.dimBy = (float)sy; tab.dimStage = 2; }
                } else if (tab.dimStage == 2) {
                    // 3rd click sets the text/leader position (free, no snap).
                    commitDimension(st, tab, tab.dimViewId, tab.dimAx, tab.dimAy,
                                    tab.dimBx, tab.dimBy, (float)curMx, (float)curMy);
                    tab.dimStage = 0;
                    tab.dimViewId.clear();
                }
            }
        } else if (!tab.dimToolActive && !tab.sectionToolActive && !tab.detailToolActive &&
                   overCanvas && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !spaceDown &&
                   !panning) {
            // Selection: pick the nearest stored dimension's text position to the click.
            std::string hitView, hitAnn; double bestD2 = (14.0 / std::max(z, 1e-4f)) *
                                                          (14.0 / std::max(z, 1e-4f));
            if (sheet->contains("views") && (*sheet)["views"].is_array()) {
                for (const auto& v : (*sheet)["views"]) {
                    if (!v.is_object() || !v.contains("annotations") ||
                        !v["annotations"].is_array()) continue;
                    const std::string vid = djstr(v, "view_id").empty() ? djstr(v, "id")
                                                                        : djstr(v, "view_id");
                    for (const auto& ann : v["annotations"]) {
                        if (!ann.is_object()) continue;
                        auto it = ann.find("pos");
                        if (it == ann.end() || !it->is_array() || it->size() != 2) continue;
                        double ppx = (*it)[0].get<double>(), ppy = (*it)[1].get<double>();
                        double d2 = (ppx - curMx) * (ppx - curMx) + (ppy - curMy) * (ppy - curMy);
                        if (d2 < bestD2) { bestD2 = d2; hitView = vid; hitAnn = djstr(ann, "id"); }
                    }
                }
            }
            tab.selAnnViewId = hitView;   // empty clears the selection
            tab.selAnnId = hitAnn;
        }

        // Delete the selected dimension.
        if (!tab.dimToolActive && !tab.selAnnId.empty() &&
            (ImGui::IsKeyPressed(ImGuiKey_Delete) || ImGui::IsKeyPressed(ImGuiKey_Backspace))) {
            deleteAnnotation(st, tab, tab.selAnnViewId, tab.selAnnId);
        }
    }

    // ---- W13: BOM table overlay (top-right of the canvas) ----
    if (tab.bomVisible) {
        if (!tab.bomFetched) fetchBom(st, tab);   // (re)fetch after a Rebuild invalidated the cache
        const nlohmann::json& bom = tab.bomRows;
        bool isAsm = bom.is_object() && bom.value("is_assembly", false);
        const nlohmann::json* rows = (bom.is_object() && bom.contains("rows") &&
                                      bom["rows"].is_array()) ? &bom["rows"] : nullptr;
        const float pad = 6.0f * s, rowH = ImGui::GetTextLineHeight() + 4 * s;
        // Column layout (item / part / qty).
        const float wItem = 34 * s, wQty = 40 * s, wName = 150 * s;
        const float tableW = wItem + wName + wQty + pad * 2;
        int nRows = rows ? (int)rows->size() : 0;
        const float tableH = rowH * (nRows + 2) + pad * 2;   // header + title + rows
        ImVec2 t0(canvasMax.x - tableW - 10 * s, canvasMin.y + 10 * s);
        ImVec2 t1(t0.x + tableW, t0.y + tableH);
        dl->AddRectFilled(t0, t1, IM_COL32(252, 252, 250, 240), 3.0f);
        dl->AddRect(t0, t1, IM_COL32(60, 64, 74, 255), 3.0f, 0, 1.2f * s);
        const ImU32 ink = IM_COL32(40, 44, 52, 255);
        float yy = t0.y + pad;
        dl->AddText(ImVec2(t0.x + pad, yy), ink, "Bill of Materials");
        yy += rowH;
        if (!isAsm || nRows == 0) {
            const char* msg = bom.is_object() && bom.contains("note") && bom["note"].is_string()
                                  ? bom["note"].get<std::string>().c_str()
                                  : (isAsm ? "no components" : "part drawing - no BOM");
            dl->AddText(ImVec2(t0.x + pad, yy), IM_COL32(120, 126, 138, 255), msg);
        } else {
            // Header row.
            dl->AddText(ImVec2(t0.x + pad, yy), ink, "#");
            dl->AddText(ImVec2(t0.x + pad + wItem, yy), ink, "Part");
            dl->AddText(ImVec2(t0.x + pad + wItem + wName, yy), ink, "Qty");
            dl->AddLine(ImVec2(t0.x + pad, yy + rowH - 2), ImVec2(t1.x - pad, yy + rowH - 2),
                        IM_COL32(150, 154, 164, 200), 1.0f * s);
            yy += rowH;
            for (const auto& r : *rows) {
                if (!r.is_object()) continue;
                char itemBuf[16];
                std::snprintf(itemBuf, sizeof(itemBuf), "%d", (int)djnum(r, "item", 0));
                std::string name = djstr(r, "name");
                if ((float)ImGui::CalcTextSize(name.c_str()).x > wName - 4 * s) {
                    while (name.size() > 3 &&
                           (float)ImGui::CalcTextSize((name + "...").c_str()).x > wName - 4 * s)
                        name.pop_back();
                    name += "...";
                }
                char qtyBuf[16];
                std::snprintf(qtyBuf, sizeof(qtyBuf), "%d", (int)djnum(r, "quantity", 0));
                dl->AddText(ImVec2(t0.x + pad, yy), ink, itemBuf);
                dl->AddText(ImVec2(t0.x + pad + wItem, yy), ink, name.c_str());
                dl->AddText(ImVec2(t0.x + pad + wItem + wName, yy), ink, qtyBuf);
                yy += rowH;
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
