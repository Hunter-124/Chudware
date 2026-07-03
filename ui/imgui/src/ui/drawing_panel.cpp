// AI-CAD Drawing panel — 2D sheet renderer. See drawing_panel.h.
//
// Ported from host/src/ui/drawing_panel.cpp (H3). The drawing engine hands us, per sheet:
// size_mm:[w,h] (the paper) and a list of ViewGeometry, each with `visible` (solid) and `hidden`
// (dashed) polylines already scaled + translated to sheet-mm at the view's `position`. So all
// this panel does is paper -> screen: a single affine (uniform scale pxPerMm + screen pan), then
// stroke the polylines with ImDrawList. We add a paper border, a faint drop shadow, per-view
// bounding boxes + labels, and a small "Rebuild" / fit affordance.
//
// Every drawing_* / export_drawing RPC call below is byte-identical to host (same method name,
// same param shape) so exported DXF/PDF artifacts are unchanged. What's adapted for the new tree:
//   - AppState is app::AppState (worker/docMirror access is identical — both share
//     services/worker/{worker_client,doc_mirror}.h).
//   - Host keeps drawing state per-DocTab; here it lives in a file-static singleton (DrawingUI),
//     the same "panel owns its state, no AppState fields" convention cam_panel.cpp / preview_pane
//     use. Only one active drawing is tracked at a time (see drawing_panel.h).
//   - host's st.toast()/st.addLog()/pmFormatDim(via st.ips())/ui::saveFileDialog/ui::uiScale/
//     ui::palette map to this tree's ui::postToast/ui::appendConsoleLog/pmFormatDim(with
//     st.units=="in")/platform::saveFileDialog/theme::S()/local ImU32 constants respectively.
//   - host's GlassPopupScope (ChannelsSplit/Merge) is replaced by this tree's simpler convention
//     (theme::adaptivePanel + theme::glassPanel painted immediately after BeginPopup, before
//     content — see section_tool.cpp) since content draws after in the same list either way.
//   - the new tree's "make_drawing" command dispatch (app/app_state.cpp) only flips workspace to
//     Drafting and pushes a placeholder doc-strip entry; it does not talk to the engine. This
//     panel self-creates the real drawing (drawing_new + the 4 standard views + drawing_get —
//     host's AppState::makeDrawing() sequence, verbatim) the first time it renders for a given
//     active source document.
#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <map>
#include <string>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

#include "imgui.h"

#include "drawing_panel.h"

#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/domain_tokens.h"
#include "ui/toast.h"
#include "ui/console_panel.h"
#include "ui/property_manager.h"   // pmFormatDim — dimension text follows MMGS/IPS like the rest of the UI
#include "platform/dialogs.h"

namespace ui {

// --- panel-owned state (file-static singleton — see drawing_panel.h) --------------------------
struct DrawingUI {
    std::string sourceDocId;    // part/asm doc this drawing was created from
    std::string attemptedFor;   // sourceDocId a create attempt was already made for (ok or not) —
                                 // avoids re-issuing drawing_new every frame after a failure
    std::string drawingDocId;   // drawing registry id (from drawing_new) — NOT a doc_* id
    nlohmann::json drawingDoc;  // cached drawing_get result {sheets:[{...views...}]}

    // 2D sheet pan/zoom. pxPerMm scales sheet-mm -> screen px; panPx is the screen-space offset
    // of the sheet origin. needFit=true requests a one-shot fit-to-window.
    float drawZoom = 0.0f;      // 0 = uninitialized -> triggers a fit on first show
    float drawPanX = 0.0f, drawPanY = 0.0f;
    bool  drawNeedFit = true;

    // ---- W13: dimension tool + BOM ----
    bool        dimToolActive = false;
    std::string dimType = "linear_dim";
    int         dimStage = 0;              // 0=pick a, 1=pick b, 2=pick text pos
    std::string dimViewId;
    float       dimAx = 0, dimAy = 0;
    float       dimBx = 0, dimBy = 0;
    std::string selAnnViewId;
    std::string selAnnId;
    bool        bomVisible = false;
    nlohmann::json bomRows;
    bool        bomFetched = false;

    // ---- W15: Section + Detail view tools ----
    bool        sectionToolActive = false;
    int         sectionStage = 0;
    std::string sectionViewId;
    bool        detailToolActive = false;
    int         detailStage = 0;
    std::string detailViewId;
    float       detailCx = 0, detailCy = 0;

    // ---- W17: detailing tools ----
    bool        modelItemsActive = false;
    bool        holeCalloutActive = false;
    bool        centerMarkActive = false;
    bool        noteActive = false;
    bool        balloonActive = false;
    int         balloonNext = 1;
    bool        centerLineActive = false;
    int         centerLineStage = 0;
    float       centerLineAx = 0, centerLineAy = 0;
    bool        titleBlockOpen = false;

    // ---- view drag/select + GD&T + custom sections ----
    std::string selViewId;
    bool        viewDragActive = false;
    float       viewDragDX = 0, viewDragDY = 0;
    bool        gdtToolActive = false;
    std::string gdtKind;
    std::string gdtViewId;
    int         gdtStage = 0;
    float       gdtAx = 0, gdtAy = 0;
    nlohmann::json gdtSpec;
    bool        gdtEditorOpen = false;
    std::string sectionMode = "full";       // full|half
    bool        sectionReverse = false;
    std::vector<std::array<float, 2>> sectionPts;
};
static DrawingUI g_dwg;

// --- 2D export (DXF / PDF) for the active drawing ----------------------------------------------
// Path choice: a native Save-As dialog when available; when file dialogs are disabled (headless /
// AI_CAD_NO_FILE_DIALOGS) we fall back to writing "<title>_drawing.<ext>" in the cwd and toast the
// path. The engine writes the file via export_drawing.
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

static void exportActiveDrawing(app::AppState& st, DrawingUI& tab, const char* fmt) {
    if (tab.drawingDocId.empty()) {
        ui::postToast(ui::ToastLevel::Warn, "Open a drawing first");
        return;
    }
    const bool isDxf = std::strcmp(fmt, "dxf") == 0;
    const std::string ext = isDxf ? "dxf" : "pdf";
    const std::string base = s_sanitize("drawing") + "_drawing";

    std::string path;
    if (s_drawDialogsDisabled()) {
        // Headless fallback: write into the current working directory.
        path = base + "." + ext;
    } else {
        const char* filterDxf = "DXF (*.dxf)\0*.dxf\0All files (*.*)\0*.*\0";
        const char* filterPdf = "PDF (*.pdf)\0*.pdf\0All files (*.*)\0*.*\0";
        std::string suggest = base + "." + ext;
        path = platform::saveFileDialog(isDxf ? filterDxf : filterPdf, ext.c_str(), suggest.c_str());
        if (path.empty()) return;  // canceled
    }

    std::string err;
    std::string written = st.docMirror.exportDrawing(st.worker, tab.drawingDocId, path, fmt, err);
    if (!written.empty()) {
        ui::appendConsoleLog("[draw] exported " + ext + ": " + written);
        ui::postToast(ui::ToastLevel::Info, "Exported " + written);
    } else {
        ui::appendConsoleLog("[draw] export " + ext + " failed: " + err);
        ui::postToast(ui::ToastLevel::Error,
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

// Re-fetch the cached drawing (drawing_get) — host's AppState::refreshDrawingTab, panel-local.
static void refreshDrawing(app::AppState& st, DrawingUI& tab) {
    if (tab.drawingDocId.empty()) return;
    nlohmann::json res;
    std::string err;
    if (st.worker.call("drawing_get", {{"doc_id", tab.drawingDocId}}, res, err) && res.is_object()) {
        tab.drawingDoc = std::move(res);
        tab.bomFetched = false;  // the source may have changed — invalidate the cached BOM
    } else {
        ui::appendConsoleLog("[draw] refresh drawing_get failed for " + tab.drawingDocId + ": " + err);
        // Keep the previously-cached drawing so the sheet doesn't blank on a transient failure.
    }
}

// Push a finished dimension to the engine (drawing_add_dim) and refresh the cached drawing_get.
static void commitDimension(app::AppState& st, DrawingUI& tab, const std::string& viewId,
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
            refreshDrawing(st, tab);
        ui::appendConsoleLog("[draw] added " + tab.dimType + " on " + viewId);
    } else {
        std::string msg = res.is_object() && res.contains("message") ? djstr(res, "message") : err;
        ui::appendConsoleLog("[draw] drawing_add_dim failed: " + msg);
        ui::postToast(ui::ToastLevel::Error, "Dimension failed: " + (msg.empty() ? "engine error" : msg));
    }
}

// Delete a stored annotation (drawing_remove_annotation) + refresh.
static void deleteAnnotation(app::AppState& st, DrawingUI& tab, const std::string& viewId,
                             const std::string& annId) {
    nlohmann::json res;
    std::string err;
    nlohmann::json params = {{"doc_id", tab.drawingDocId}, {"view_id", viewId}, {"ann_id", annId}};
    if (st.worker.call("drawing_remove_annotation", params, res, err) && res.is_object() &&
        !res.contains("code")) {
        refreshDrawing(st, tab);
        ui::appendConsoleLog("[draw] removed annotation " + annId);
    } else {
        ui::appendConsoleLog("[draw] drawing_remove_annotation failed: " + err);
    }
    tab.selAnnViewId.clear();
    tab.selAnnId.clear();
}

// W15: push a finished cutting line to the engine (drawing_add_section{cut_line}) + refresh.
// `pts` is the (possibly jogged/offset) cut-line polyline in sheet mm; mode/reverse carry the
// custom-section controls (full|half + which side is removed).
static void commitSection(app::AppState& st, DrawingUI& tab, const std::string& parentViewId,
                          const std::vector<std::array<float, 2>>& pts, const std::string& mode,
                          bool reverse) {
    if (pts.size() < 2) return;
    nlohmann::json cut = nlohmann::json::array();
    for (const auto& p : pts) cut.push_back(nlohmann::json::array({p[0], p[1]}));
    nlohmann::json res;
    std::string err;
    nlohmann::json params = {
        {"doc_id", tab.drawingDocId},
        {"parent_view_id", parentViewId},
        {"cut_line", cut},
        {"opts", nlohmann::json{{"mode", mode}, {"reverse", reverse}}},
    };
    if (st.worker.call("drawing_add_section", params, res, err) && res.is_object() &&
        !res.contains("code")) {
        refreshDrawing(st, tab);
        ui::appendConsoleLog("[draw] added " + mode + " section on " + parentViewId);
        tab.drawNeedFit = true;   // frame the new section view
    } else {
        std::string msg = res.is_object() && res.contains("message") ? djstr(res, "message") : err;
        ui::appendConsoleLog("[draw] drawing_add_section failed: " + msg);
        ui::postToast(ui::ToastLevel::Error, "Section failed: " + (msg.empty() ? "engine error" : msg));
    }
}

// W15: push a finished detail circle to the engine (drawing_add_detail) + refresh.
static void commitDetail(app::AppState& st, DrawingUI& tab, const std::string& parentViewId,
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
        refreshDrawing(st, tab);
        ui::appendConsoleLog("[draw] added detail on " + parentViewId);
        tab.drawNeedFit = true;
    } else {
        std::string msg = res.is_object() && res.contains("message") ? djstr(res, "message") : err;
        ui::appendConsoleLog("[draw] drawing_add_detail failed: " + msg);
        ui::postToast(ui::ToastLevel::Error, "Detail failed: " + (msg.empty() ? "engine error" : msg));
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
static void fetchBom(app::AppState& st, DrawingUI& tab) {
    nlohmann::json res;
    std::string err;
    if (st.worker.call("drawing_bom", {{"doc_id", tab.drawingDocId}}, res, err) && res.is_object() &&
        !res.contains("code")) {
        tab.bomRows = res;
    } else {
        ui::appendConsoleLog("[draw] drawing_bom failed: " + err);
        tab.bomRows = nlohmann::json::object();
    }
    tab.bomFetched = true;
}

// --- W17 detailing: shared RPC plumbing + tool commits ---------------------------------------

// Update the cached drawing from an RPC result (a {drawing:...} envelope, a full {sheets:...} doc,
// or neither -> re-fetch).
static void absorbDrawingResult(app::AppState& st, DrawingUI& tab, const nlohmann::json& res) {
    if (res.is_object() && res.contains("drawing") && res["drawing"].is_object())
        tab.drawingDoc = res["drawing"];
    else if (res.is_object() && res.contains("sheets") && res["sheets"].is_array())
        tab.drawingDoc = res;
    else
        refreshDrawing(st, tab);
}

// Call a drawing RPC; on failure log + toast and return false. `label` is the user-facing verb.
static bool drawCall(app::AppState& st, const char* method, const nlohmann::json& params,
                     nlohmann::json& res, const char* label) {
    std::string err;
    if (st.worker.call(method, params, res, err) && res.is_object() && !res.contains("code"))
        return true;
    std::string msg = res.is_object() && res.contains("message") ? djstr(res, "message") : err;
    ui::appendConsoleLog(std::string("[draw] ") + method + " failed: " + msg);
    ui::postToast(ui::ToastLevel::Error,
                 std::string(label) + " failed: " + (msg.empty() ? "engine error" : msg));
    return false;
}

// Clear every modal drawing tool + its in-progress stage (called when arming a new tool).
static void disarmDrawTools(DrawingUI& tab) {
    tab.dimToolActive = false; tab.dimStage = 0; tab.dimViewId.clear();
    tab.sectionToolActive = false; tab.sectionStage = 0; tab.sectionViewId.clear();
    tab.detailToolActive = false; tab.detailStage = 0; tab.detailViewId.clear();
    tab.modelItemsActive = false; tab.holeCalloutActive = false;
    tab.centerMarkActive = false; tab.noteActive = false; tab.balloonActive = false;
    tab.centerLineActive = false; tab.centerLineStage = 0;
    tab.gdtToolActive = false; tab.gdtStage = 0; tab.gdtKind.clear(); tab.sectionPts.clear();
    tab.selAnnViewId.clear(); tab.selAnnId.clear();
}

// True while ANY modal drawing tool is armed (used to gate plain selection / drag).
static bool anyDrawToolActive(const DrawingUI& tab) {
    return tab.dimToolActive || tab.sectionToolActive || tab.detailToolActive ||
           tab.modelItemsActive || tab.holeCalloutActive || tab.centerMarkActive ||
           tab.noteActive || tab.balloonActive || tab.centerLineActive || tab.gdtToolActive;
}

// Commit a GD&T element (drawing_add_gdt/datum/datum_target/surface_finish/weld) + absorb.
static void commitGdt(app::AppState& st, DrawingUI& tab, const std::string& viewId,
                      const nlohmann::json& spec) {
    static const std::map<std::string, std::string> method = {
        {"gdt_frame", "drawing_add_gdt"},          {"datum_feature", "drawing_add_datum"},
        {"datum_target", "drawing_add_datum_target"}, {"surface_finish", "drawing_add_surface_finish"},
        {"weld_symbol", "drawing_add_weld"}};
    auto it = method.find(tab.gdtKind);
    if (it == method.end()) return;
    nlohmann::json res;
    nlohmann::json params = {{"doc_id", tab.drawingDocId}, {"view_id", viewId}, {"spec", spec}};
    if (drawCall(st, it->second.c_str(), params, res, "GD&T")) {
        absorbDrawingResult(st, tab, res);
        ui::appendConsoleLog("[draw] added " + tab.gdtKind + " on " + viewId);
    }
}

// Move a view to a new sheet position (drawing_set_view {position}) — position-only, so the
// engine re-projects nothing; cheap + smooth. Absorbs the returned drawing.
static void commitViewMove(app::AppState& st, DrawingUI& tab, const std::string& viewId,
                           double px, double py) {
    nlohmann::json res;
    nlohmann::json params = {{"doc_id", tab.drawingDocId}, {"view_id", viewId},
                             {"position", nlohmann::json::array({px, py})}};
    if (!drawCall(st, "drawing_set_view", params, res, "Move view")) return;
    // Splice ONLY the moved view's returned geometry into the cache — no full drawing_get
    // (which would re-HLR every view). Parent-view markers (section/detail) the moved view
    // owns are recomputed on the next Rebuild; the move itself is instant.
    if (res.contains("view") && res["view"].is_object() && tab.drawingDoc.contains("sheets") &&
        tab.drawingDoc["sheets"].is_array()) {
        const std::string rid = res["view"].value("view_id", viewId);
        for (auto& sh : tab.drawingDoc["sheets"]) {
            if (!sh.contains("views") || !sh["views"].is_array()) continue;
            for (auto& vv : sh["views"]) {
                std::string vid = vv.value("view_id", vv.value("id", std::string()));
                if (vid == rid) { vv = res["view"]; break; }
            }
        }
    } else {
        refreshDrawing(st, tab);
    }
    ui::appendConsoleLog("[draw] moved view " + viewId);
}

// Remove a view (drawing_remove_view) + refresh.
static void removeView(app::AppState& st, DrawingUI& tab, const std::string& viewId) {
    nlohmann::json res;
    nlohmann::json params = {{"doc_id", tab.drawingDocId}, {"view_id", viewId}};
    if (drawCall(st, "drawing_remove_view", params, res, "Delete view")) {
        refreshDrawing(st, tab);
        tab.selViewId.clear();
        ui::appendConsoleLog("[draw] removed view " + viewId);
    }
}

// Set a view's scale (drawing_set_view {scale}) — re-projects this view at the new scale.
static void commitViewScale(app::AppState& st, DrawingUI& tab, const std::string& viewId,
                            double scale) {
    nlohmann::json res;
    nlohmann::json params = {{"doc_id", tab.drawingDocId}, {"view_id", viewId}, {"scale", scale}};
    if (drawCall(st, "drawing_set_view", params, res, "Scale view")) {
        refreshDrawing(st, tab);
        ui::appendConsoleLog("[draw] scaled view " + viewId);
    }
}

static void addStdView(app::AppState& st, DrawingUI& tab, const char* type, const char* orient,
                       double px, double py) {
    nlohmann::json res;
    nlohmann::json params = {{"doc_id", tab.drawingDocId}, {"type", type}, {"orientation", orient},
                             {"position", nlohmann::json::array({px, py})}};
    if (drawCall(st, "drawing_add_view", params, res, "Add view")) {
        refreshDrawing(st, tab);
        tab.drawNeedFit = true;
        ui::appendConsoleLog(std::string("[draw] added ") + type + " " + orient + " view");
    }
}

// Auto-dimension the clicked view (overall envelope + per-circle diameter callouts).
static void commitModelItems(app::AppState& st, DrawingUI& tab, const std::string& viewId) {
    nlohmann::json res;
    if (drawCall(st, "drawing_model_items",
                 {{"doc_id", tab.drawingDocId}, {"view_id", viewId}}, res, "Model Items")) {
        absorbDrawingResult(st, tab, res);
        int added = res.is_object() ? (int)djnum(res, "added", 0) : 0;
        ui::appendConsoleLog("[draw] model items added " + std::to_string(added) + " dims on " + viewId);
        ui::postToast(ui::ToastLevel::Info, "Auto-dimensioned (" + std::to_string(added) + ")");
    }
}

// Smart hole callout: snaps the nearest circle near (ax,ay) -> "n× ⌀d".
static void commitHoleCallout(app::AppState& st, DrawingUI& tab, const std::string& viewId,
                              float ax, float ay) {
    nlohmann::json res;
    nlohmann::json params = {{"doc_id", tab.drawingDocId}, {"view_id", viewId}, {"kind", "hole"},
                             {"a", nlohmann::json::array({ax, ay})}};
    if (drawCall(st, "drawing_add_callout", params, res, "Hole callout")) {
        absorbDrawingResult(st, tab, res);
        ui::appendConsoleLog("[draw] hole callout on " + viewId);
    }
}

// Item-number balloon at the click point (auto-incrementing).
static void commitBalloon(app::AppState& st, DrawingUI& tab, const std::string& viewId,
                          float ax, float ay) {
    nlohmann::json res;
    nlohmann::json params = {{"doc_id", tab.drawingDocId}, {"view_id", viewId}, {"kind", "balloon"},
                             {"a", nlohmann::json::array({ax, ay})}, {"item", tab.balloonNext}};
    if (drawCall(st, "drawing_add_callout", params, res, "Balloon")) {
        absorbDrawingResult(st, tab, res);
        ++tab.balloonNext;
        ui::appendConsoleLog("[draw] balloon on " + viewId);
    }
}

// Center mark / center line / note via drawing_add_dim (type carries the kind).
static void commitExtDim(app::AppState& st, DrawingUI& tab, const std::string& viewId,
                         const char* type, const nlohmann::json& a, const nlohmann::json& b,
                         const std::string& text) {
    nlohmann::json res;
    nlohmann::json params = {{"doc_id", tab.drawingDocId}, {"view_id", viewId}, {"type", type},
                             {"a", a}};
    if (b.is_array()) params["b"] = b;
    if (!text.empty()) params["text"] = text;
    if (drawCall(st, "drawing_add_dim", params, res, type)) {
        absorbDrawingResult(st, tab, res);
        ui::appendConsoleLog(std::string("[draw] added ") + type + " on " + viewId);
    }
}

// Patch detailing fields on an existing annotation (tolerance / critical / prefix / pos / ...).
static void applyAnnPatch(app::AppState& st, DrawingUI& tab, const std::string& viewId,
                          const std::string& annId, const nlohmann::json& patch) {
    nlohmann::json res;
    nlohmann::json params = patch;
    params["doc_id"] = tab.drawingDocId;
    params["view_id"] = viewId;
    params["ann_id"] = annId;
    if (drawCall(st, "drawing_set_annotation", params, res, "Edit dimension")) {
        absorbDrawingResult(st, tab, res);
        ui::appendConsoleLog("[draw] edited annotation " + annId);
    }
}

// --- self-create: host's AppState::makeDrawing(), byte-identical RPC sequence -----------------
namespace {
struct DrawingViewSpec {
    const char* type;         // "base" | "projected" | "iso"
    const char* orientation;  // "front" | "top" | "right" | "iso"
    double x, y;              // sheet-mm anchor
};
// The standard four-view layout placed on the blank A3 sheet (positions in sheet mm; the engine
// scales + translates each projected polyline to these anchors). Matches host exactly.
const DrawingViewSpec kStdViews[] = {
    {"base",      "front", 105.0, 180.0},
    {"projected", "top",   105.0,  80.0},
    {"projected", "right", 230.0, 180.0},
    {"iso",       "iso",   300.0,  80.0},
};
}  // namespace

static void resetPanelState(DrawingUI& tab) {
    disarmDrawTools(tab);
    tab.selViewId.clear();
    tab.bomVisible = false; tab.bomFetched = false; tab.bomRows = nlohmann::json::object();
    tab.balloonNext = 1;
    tab.drawZoom = 0.0f; tab.drawPanX = 0.0f; tab.drawPanY = 0.0f; tab.drawNeedFit = true;
}

// Create a drawing from the currently active part/asm doc (docMirror.docId()) — drawing_new, the
// 4 standard views (drawing_add_view), then drawing_get. One attempt per distinct source doc id
// (success or failure) so a persistently-erroring engine doesn't spam RPCs every frame.
static void createDrawingFromActive(app::AppState& st, DrawingUI& tab) {
    tab.attemptedFor = st.docMirror.docId();
    if (!st.docMirror.hasDoc()) return;
    const std::string sourceId = st.docMirror.docId();
    std::string sourceName = "Part";
    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size())
        sourceName = st.docTabs[st.activeTab].displayTitle();

    std::string drawingId, drawingName;
    {
        nlohmann::json res;
        std::string err;
        nlohmann::json params = {{"source_doc_id", sourceId},
                                 {"name", sourceName + " - Drawing"}};
        if (!st.worker.call("drawing_new", params, res, err) || !res.is_object()) {
            ui::appendConsoleLog("[draw] drawing_new failed: " + err);
            ui::postToast(ui::ToastLevel::Error,
                         "Make Drawing failed: " + (err.empty() ? std::string("engine error") : err));
            return;
        }
        drawingId = djstr(res, "doc_id");
        if (res.contains("drawing") && res["drawing"].is_object())
            drawingName = djstr(res["drawing"], "name");
        if (drawingId.empty()) {
            ui::appendConsoleLog("[draw] drawing_new returned no doc_id");
            ui::postToast(ui::ToastLevel::Error, "Make Drawing failed: no drawing id");
            return;
        }
    }
    if (drawingName.empty()) drawingName = sourceName + " - Drawing";

    int added = 0;
    for (const auto& v : kStdViews) {
        nlohmann::json res;
        std::string err;
        nlohmann::json params = {
            {"doc_id", drawingId},
            {"type", v.type},
            {"orientation", v.orientation},
            {"position", nlohmann::json::array({v.x, v.y})},
            {"scale", 1.0},
        };
        if (st.worker.call("drawing_add_view", params, res, err))
            ++added;
        else
            ui::appendConsoleLog(std::string("[draw] add_view ") + v.orientation + " failed: " + err);
    }

    nlohmann::json drawingDoc;
    {
        nlohmann::json res;
        std::string err;
        if (st.worker.call("drawing_get", {{"doc_id", drawingId}}, res, err) && res.is_object())
            drawingDoc = res;
        else
            ui::appendConsoleLog("[draw] drawing_get failed: " + err);
    }

    resetPanelState(tab);
    tab.sourceDocId = sourceId;
    tab.drawingDocId = drawingId;
    tab.drawingDoc = std::move(drawingDoc);
    tab.drawNeedFit = true;
    ui::appendConsoleLog("[draw] created drawing '" + drawingName + "' (" + drawingId + ", " +
                         std::to_string(added) + " views) from " + sourceId);
    ui::postToast(ui::ToastLevel::Info, "Drawing created with " + std::to_string(added) + " views");
}

// --- T-G9 scenario/parity hooks (see drawing_panel.h) -----------------------------------------
std::string drawingActiveDocId() { return g_dwg.drawingDocId; }
nlohmann::json drawingActiveDoc() { return g_dwg.drawingDoc; }
void drawingRefreshActive(app::AppState& st) { refreshDrawing(st, g_dwg); }

bool drawingHandleCommand(app::AppState& st, const std::string& cmd) {
    DrawingUI& tab = g_dwg;
    if (tab.drawingDocId.empty()) {
        ui::postToast(ui::ToastLevel::Warn, "Open a drawing first");
        return true;
    }

    // --- View Layout tab ---
    if (cmd == "dwg_rebuild")     { disarmDrawTools(tab); refreshDrawing(st, tab); return true; }
    if (cmd == "dwg_fit")         { tab.drawNeedFit = true; return true; }
    if (cmd == "dwg_export_dxf")  { exportActiveDrawing(st, tab, "dxf"); return true; }
    if (cmd == "dwg_export_pdf")  { exportActiveDrawing(st, tab, "pdf"); return true; }
    if (cmd == "dwg_view_model")  { disarmDrawTools(tab); addStdView(st, tab, "base", "front", 150, 180); return true; }
    if (cmd == "dwg_view_proj")   { disarmDrawTools(tab); addStdView(st, tab, "projected", "top", 150, 80); return true; }
    if (cmd == "dwg_view_std3") {
        disarmDrawTools(tab);
        addStdView(st, tab, "base", "front", 150, 180);
        addStdView(st, tab, "projected", "top", 150, 80);
        addStdView(st, tab, "projected", "right", 320, 180);
        tab.drawNeedFit = true;
        return true;
    }
    if (cmd == "dwg_view_section") { disarmDrawTools(tab); tab.sectionToolActive = true; tab.sectionStage = 0; return true; }
    if (cmd == "dwg_view_detail")  { disarmDrawTools(tab); tab.detailToolActive = true; tab.detailStage = 0; return true; }
    if (cmd == "dwg_sheet_props" || cmd == "dwg_title_block") { tab.titleBlockOpen = true; return true; }

    // --- Annotation tab ---
    if (cmd == "dwg_smart_dim")    { disarmDrawTools(tab); tab.dimToolActive = true; tab.dimStage = 0; return true; }
    if (cmd == "dwg_model_items")  { disarmDrawTools(tab); tab.modelItemsActive = true; return true; }
    if (cmd == "dwg_hole_callout") { disarmDrawTools(tab); tab.holeCalloutActive = true; return true; }
    if (cmd == "dwg_center_mark")  { disarmDrawTools(tab); tab.centerMarkActive = true; return true; }
    if (cmd == "dwg_center_line")  { disarmDrawTools(tab); tab.centerLineActive = true; tab.centerLineStage = 0; return true; }
    if (cmd == "dwg_note")         { disarmDrawTools(tab); tab.noteActive = true; return true; }
    if (cmd == "dwg_balloon" || cmd == "dwg_auto_balloon") { disarmDrawTools(tab); tab.balloonActive = true; return true; }
    if (cmd == "dwg_bom_table") {
        tab.bomVisible = !tab.bomVisible;
        if (tab.bomVisible && !tab.bomFetched) fetchBom(st, tab);
        return true;
    }

    // --- GD&T tools (Annotation tab) ---
    auto armGdt = [&](const char* kind) {
        disarmDrawTools(tab);
        tab.gdtToolActive = true; tab.gdtKind = kind; tab.gdtStage = 0;
        tab.gdtSpec = nlohmann::json::object();
        ui::postToast(ui::ToastLevel::Info, "Click the feature, then click to place");
    };
    if (cmd == "dwg_gdt")            { armGdt("gdt_frame"); return true; }
    if (cmd == "dwg_datum")          { armGdt("datum_feature"); return true; }
    if (cmd == "dwg_datum_target")   { armGdt("datum_target"); return true; }
    if (cmd == "dwg_surface_finish") { armGdt("surface_finish"); return true; }
    if (cmd == "dwg_weld")           { armGdt("weld_symbol"); return true; }

    // --- Custom-section controls (View Layout tab) ---
    if (cmd == "dwg_section_half") {
        tab.sectionMode = (tab.sectionMode == "half") ? "full" : "half";
        ui::postToast(ui::ToastLevel::Info, std::string("Section mode: ") + tab.sectionMode);
        return true;
    }
    if (cmd == "dwg_section_reverse") {
        tab.sectionReverse = !tab.sectionReverse;
        ui::postToast(ui::ToastLevel::Info, tab.sectionReverse ? "Section: reversed side"
                                                                : "Section: normal side");
        return true;
    }

    // --- Scale the selected view (View Layout tab) ---
    auto setScale = [&](double sc) {
        if (tab.selViewId.empty()) { ui::postToast(ui::ToastLevel::Warn, "Select a view first"); return; }
        commitViewScale(st, tab, tab.selViewId, sc);
    };
    if (cmd == "dwg_scale_1_1") { setScale(1.0); return true; }
    if (cmd == "dwg_scale_1_2") { setScale(0.5); return true; }
    if (cmd == "dwg_scale_2_1") { setScale(2.0); return true; }
    return false;
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
    const std::string kind = djstr(ann, "kind");
    const bool critical = ann.is_object() && ann.value("critical", false);
    double ax, ay, bx, by, px, py;
    bool haveA = rdvec("a", ax, ay), haveB = rdvec("b", bx, by), haveP = rdvec("pos", px, py);

    // Critical dims pop red + heavier; selected = orange; otherwise drawing-blue ink.
    const ImU32 dimCol = critical ? theme::draw2d::dimCritical()
                       : selected  ? theme::draw2d::dimSelected()
                                   : theme::draw2d::dimInk();
    const float thick = std::max(1.0f, (critical ? 1.6f : 1.0f) * s);

    std::string txt = djstr(ann, "text");
    auto drawLabel = [&](ImVec2 tp, const std::string& t) {
        if (t.empty()) return;
        ImVec2 ts = ImGui::CalcTextSize(t.c_str());
        dl->AddRectFilled(ImVec2(tp.x - 2, tp.y - 1), ImVec2(tp.x + ts.x + 2, tp.y + ts.y + 1),
                          theme::draw2d::labelPad(), 2.0f);
        dl->AddText(tp, dimCol, t.c_str());
    };
    auto criticalFlag = [&](ImVec2 at) {
        if (critical) dl->AddText(ImVec2(at.x - 10 * s, at.y - 1), theme::draw2d::dimCritical(), "!");
    };

    // --- center mark: a small cross at the circle center ---
    if (kind == "center_mark") {
        double cx = ax, cy = ay, t1, t2;
        if (rdvec("center", t1, t2)) { cx = t1; cy = t2; }
        ImVec2 c = toScreen(cx, cy);
        float r = 6.0f * s;
        dl->AddLine(ImVec2(c.x - r, c.y), ImVec2(c.x + r, c.y), dimCol, thick);
        dl->AddLine(ImVec2(c.x, c.y - r), ImVec2(c.x, c.y + r), dimCol, thick);
        return;
    }
    // --- center line: dashed construction line a->b (with a small overrun each end) ---
    if (kind == "center_line") {
        if (haveA && haveB) {
            ImVec2 A = toScreen(ax, ay), B = toScreen(bx, by);
            ImVec2 d(B.x - A.x, B.y - A.y);
            float l = std::sqrt(d.x * d.x + d.y * d.y);
            if (l > 1e-3f) {
                d.x /= l; d.y /= l;
                float e = 6.0f * s;
                A = ImVec2(A.x - d.x * e, A.y - d.y * e);
                B = ImVec2(B.x + d.x * e, B.y + d.y * e);
            }
            addDashedPolyline(dl, {A, B}, dimCol, thick, 9.0f * s, 4.0f * s);
        }
        return;
    }
    // --- balloon: leader from anchor a to a numbered bubble at pos ---
    if (kind == "balloon") {
        if (haveP) {
            ImVec2 P = toScreen(px, py);
            float r = 11.0f * s;
            if (haveA) {
                ImVec2 A = toScreen(ax, ay);
                dl->AddLine(A, P, dimCol, thick);
                dl->AddCircleFilled(A, 2.0f * s, dimCol);
            }
            dl->AddCircleFilled(P, r, theme::draw2d::fillWhite());
            dl->AddCircle(P, r, dimCol, 24, thick);
            if (txt.empty()) {
                int it = (int)djnum(ann, "item", 0);
                if (it > 0) txt = std::to_string(it);
            }
            if (!txt.empty()) {
                ImVec2 ts = ImGui::CalcTextSize(txt.c_str());
                dl->AddText(ImVec2(P.x - ts.x * 0.5f, P.y - ts.y * 0.5f), dimCol, txt.c_str());
            }
        }
        return;
    }
    // --- hole callout / radial / diameter: leader from the circle center to text at pos ---
    if (kind == "hole_callout" || kind == "radial_dim" || kind == "diameter_dim") {
        if (txt.empty()) {
            double val = djnum(ann, "value", 0.0);
            txt = pmFormatDim(val, ips);
            if (kind == "diameter_dim" || kind == "hole_callout") txt = "\xE2\x8C\x80" + txt;
            else if (kind == "radial_dim") txt = "R" + txt;
        }
        double cx = ax, cy = ay, t1, t2;
        if (rdvec("center", t1, t2)) { cx = t1; cy = t2; }
        ImVec2 C = toScreen(cx, cy);
        ImVec2 P = haveP ? toScreen(px, py) : ImVec2(C.x + 22 * s, C.y - 22 * s);
        dl->AddLine(C, P, dimCol, thick);
        dl->AddCircleFilled(C, 2.0f * s, dimCol);
        drawLabel(ImVec2(P.x + 3 * s, P.y - ImGui::GetTextLineHeight() * 0.5f), txt);
        criticalFlag(P);
        return;
    }
    // --- angular dim: value text at pos (witness geometry omitted for the MVP) ---
    if (kind == "angular_dim") {
        if (haveP) drawLabel(toScreen(px, py), txt.empty() ? "?" : txt);
        return;
    }

    // Note: just text at pos.
    if (atype == "note" || !haveA || !haveB) {
        if (haveP) {
            if (txt.empty()) txt = "note";
            drawLabel(toScreen(px, py), txt);
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

    // Value text at P (centered above the dimension line). `txt` is the engine-composed display
    // string (prefix / tolerance baked in); fall back to formatting the canonical mm value.
    if (txt.empty()) {
        double val = djnum(ann, "value", 0.0);
        txt = pmFormatDim(val, ips);   // engine value is canonical mm; pmFormatDim handles IPS
    }
    ImVec2 ts = ImGui::CalcTextSize(txt.c_str());
    ImVec2 mid((A2.x + B2.x) * 0.5f, (A2.y + B2.y) * 0.5f);
    ImVec2 tp(mid.x - ts.x * 0.5f, mid.y - ts.y - 2 * s);
    dl->AddRectFilled(ImVec2(tp.x - 2, tp.y - 1), ImVec2(tp.x + ts.x + 2, tp.y + ts.y + 1),
                      theme::draw2d::labelPad(), 2.0f);
    dl->AddText(tp, dimCol, txt.c_str());
    criticalFlag(ImVec2(tp.x, tp.y));
}

// Render an annotation from the engine-compiled `draw` primitive block (segments / dashed /
// arcs / tris / texts in sheet mm). This is the ONE rendering path for dimensions, callouts,
// AND every GD&T element — the same primitives the DXF/PDF export and PNG raster consume, so
// the on-screen sheet matches the deliverables exactly. `z` is px-per-mm (paper space).
static void renderDrawPrimitives(ImDrawList* dl, const nlohmann::json& ann,
                                 const std::function<ImVec2(double, double)>& toScreen, float z,
                                 float s, bool selected) {
    auto it = ann.find("draw");
    if (it == ann.end() || !it->is_object()) return;
    const nlohmann::json& d = *it;
    const bool critical = ann.value("critical", false);
    const ImU32 col = critical ? theme::draw2d::dimCritical()
                    : selected  ? theme::draw2d::dimSelected()
                                : theme::draw2d::dimInk();
    const float thick = std::max(1.0f, (critical ? 1.6f : 1.0f) * s);

    auto strokePoly = [&](const nlohmann::json& pl, bool dashed) {
        if (!pl.is_array() || pl.size() < 2) return;
        std::vector<ImVec2> pts;
        pts.reserve(pl.size());
        for (const auto& p : pl)
            if (p.is_array() && p.size() == 2 && p[0].is_number() && p[1].is_number())
                pts.push_back(toScreen(p[0].get<double>(), p[1].get<double>()));
        if (pts.size() < 2) return;
        if (dashed) addDashedPolyline(dl, pts, col, thick, 6.0f * s, 3.0f * s);
        else dl->AddPolyline(pts.data(), (int)pts.size(), col, 0, thick);
    };
    if (d.contains("segments") && d["segments"].is_array())
        for (const auto& pl : d["segments"]) strokePoly(pl, false);
    if (d.contains("dashed") && d["dashed"].is_array())
        for (const auto& pl : d["dashed"]) strokePoly(pl, true);
    // Arcs — sample to screen points (toScreen flips Y, so sampling is orientation-safe).
    if (d.contains("arcs") && d["arcs"].is_array())
        for (const auto& a : d["arcs"]) {
            if (!a.is_object() || !a.contains("center")) continue;
            double cx = a["center"][0].get<double>(), cy = a["center"][1].get<double>();
            double r = djnum(a, "radius", 0.0);
            double a0 = djnum(a, "a0", 0.0), a1 = djnum(a, "a1", 6.28318530718);
            if (r <= 0.0) continue;
            int n = std::max(10, (int)(std::fabs(a1 - a0) / 0.3));
            std::vector<ImVec2> pts;
            pts.reserve(n + 1);
            for (int i = 0; i <= n; ++i) {
                double t = a0 + (a1 - a0) * (double)i / (double)n;
                pts.push_back(toScreen(cx + r * std::cos(t), cy + r * std::sin(t)));
            }
            dl->AddPolyline(pts.data(), (int)pts.size(), col, 0, thick);
        }
    // Filled triangles (arrowheads / datum triangles).
    if (d.contains("tris") && d["tris"].is_array())
        for (const auto& t : d["tris"]) {
            if (!t.is_array() || t.size() != 3) continue;
            dl->AddTriangleFilled(toScreen(t[0][0].get<double>(), t[0][1].get<double>()),
                                  toScreen(t[1][0].get<double>(), t[1][1].get<double>()),
                                  toScreen(t[2][0].get<double>(), t[2][1].get<double>()), col);
        }
    // Text runs — paper-space height (mm) scaled to px by the live zoom.
    if (d.contains("texts") && d["texts"].is_array()) {
        ImFont* font = ImGui::GetFont();
        const float baseSz = ImGui::GetFontSize();
        for (const auto& t : d["texts"]) {
            if (!t.is_object() || !t.contains("pos")) continue;
            std::string txt = djstr(t, "text");
            if (txt.empty()) continue;
            double h = djnum(t, "h", 3.5);
            float fpx = std::max(8.0f, (float)h * z);
            ImVec2 anchor = toScreen(t["pos"][0].get<double>(), t["pos"][1].get<double>());
            ImVec2 ts = ImGui::CalcTextSize(txt.c_str());
            float scaleF = fpx / baseSz;
            float w = ts.x * scaleF;
            std::string align = djstr(t, "align"), valign = djstr(t, "valign");
            float tx = anchor.x, ty = anchor.y;
            if (align == "center") tx -= w * 0.5f;
            else if (align == "right") tx -= w;
            if (valign == "base") ty -= fpx;
            else if (valign == "mid") ty -= fpx * 0.5f;
            // a faint white pad behind the glyphs for legibility over geometry
            dl->AddRectFilled(ImVec2(tx - 1, ty), ImVec2(tx + w + 1, ty + fpx),
                              theme::draw2d::textPad(), 1.0f);
            dl->AddText(font, fpx, ImVec2(tx, ty), col, txt.c_str());
        }
    }
}

// --- W17: editor / drag interaction state (only the single active drawing interacts) ----------
static bool s_annEditOpen = false;     // request to open the dimension editor popup this frame
static bool s_annEditLoaded = false;   // editor scratch has been loaded from the selected ann
static bool s_annDragActive = false;   // a text-drag is in progress

// Locate the selected annotation in the cached sheet (read-only).
static const nlohmann::json* findSelAnn(const nlohmann::json& sheet, const std::string& viewId,
                                        const std::string& annId) {
    if (annId.empty() || !(sheet.contains("views") && sheet["views"].is_array())) return nullptr;
    for (const auto& v : sheet["views"]) {
        if (!v.is_object() || !v.contains("annotations") || !v["annotations"].is_array()) continue;
        const std::string vid = djstr(v, "view_id").empty() ? djstr(v, "id") : djstr(v, "view_id");
        if (vid != viewId) continue;
        for (const auto& a : v["annotations"])
            if (a.is_object() && djstr(a, "id") == annId) return &a;
    }
    return nullptr;
}

// Draw a standard SolidWorks-style title block in the bottom-right of the sheet (always drawn so
// the sheet reads as a real drawing; fields come from sheet["title_block"], else sensible blanks).
static void renderTitleBlock(ImDrawList* dl, const nlohmann::json& sheet,
                             const std::function<ImVec2(double, double)>& toScreen,
                             double paperW, double /*paperH*/, float s) {
    const nlohmann::json* tb =
        (sheet.contains("title_block") && sheet["title_block"].is_object()) ? &sheet["title_block"]
                                                                            : nullptr;
    auto field = [&](const char* key, const std::string& fb) -> std::string {
        if (tb) {
            auto it = tb->find(key);
            if (it != tb->end()) {
                if (it->is_string()) return it->get<std::string>();
                if (it->is_number()) { char b[32]; std::snprintf(b, sizeof(b), "%g",
                                       it->get<double>()); return b; }
            }
        }
        return fb;
    };
    const double margin = 8.0, bw = 176.0, bh = 48.0;
    const double rx = paperW - margin, by = margin;     // bottom-right corner, inside the trim
    const double lx = rx - bw, ty = by + bh;
    ImVec2 tl = toScreen(lx, ty), br = toScreen(rx, by);
    const ImU32 ink = theme::draw2d::inkTitle(), line = theme::draw2d::paperBorder();
    dl->AddRectFilled(tl, br, theme::draw2d::fillWhite());
    dl->AddRect(tl, br, line, 0.0f, 0, 1.4f * s);
    // Two horizontal dividers -> 3 rows: title, drawn/date, scale/rev/sheet + material.
    float h = br.y - tl.y, w = br.x - tl.x;
    float r1 = tl.y + h * 0.42f, r2 = tl.y + h * 0.72f;
    dl->AddLine(ImVec2(tl.x, r1), ImVec2(br.x, r1), line, 1.0f * s);
    dl->AddLine(ImVec2(tl.x, r2), ImVec2(br.x, r2), line, 1.0f * s);
    auto label = [&](float x, float y, const char* k, const std::string& val) {
        dl->AddText(ImVec2(x + 3 * s, y + 1 * s), theme::draw2d::mutedLabel(), k);
        dl->AddText(ImVec2(x + 3 * s, y + ImGui::GetTextLineHeight() * 0.6f + 2 * s), ink,
                    val.c_str());
    };
    std::string title = field("title", "TITLE");
    ImVec2 tts = ImGui::CalcTextSize(title.c_str());
    dl->AddText(ImVec2(tl.x + (w - tts.x) * 0.5f, tl.y + (r1 - tl.y - tts.y) * 0.5f), ink,
                title.c_str());
    label(tl.x, r1, "DRAWN", field("drawn_by", ""));
    label(tl.x + w * 0.5f, r1, "DATE", field("date", ""));
    label(tl.x, r2, "MATL", field("material", ""));
    label(tl.x + w * 0.34f, r2, "SCALE", field("scale", "1:1"));
    label(tl.x + w * 0.62f, r2, "REV", field("rev", "A"));
    label(tl.x + w * 0.80f, r2, "SHEET", field("sheet", "1/1"));
}

// Frost a just-opened popup: call right after BeginPopup()/BeginPopupModal() returns true, BEFORE
// any content — the glass paints first so the popup's own widgets draw on top in the same window
// draw list (this tree's convention; see section_tool.cpp, simpler than host's channel-split
// GlassPopupScope since content is always drawn after this call here).
static void frostPopup(const char* panelId) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 p0 = ImGui::GetWindowPos();
    ImVec2 p1(p0.x + ImGui::GetWindowSize().x, p0.y + ImGui::GetWindowSize().y);
    ImRect rect(p0, p1);
    const theme::AdaptiveSurface& surf = theme::adaptivePanel(theme::panelId(panelId), rect);
    theme::glassPanel(dl, rect, theme::MaterialLevel::Regular, -1.f, surf);
}

static void renderDimEditorPopup(app::AppState& st, DrawingUI& tab, const nlohmann::json& sheet) {
    static int  etTol = 0;              // 0 none,1 symmetric,2 deviation,3 limit,4 basic
    static double etUp = 0.0, etLo = 0.0;
    static int  etPrec = 2;
    static bool etCrit = false;
    static char etPrefix[32] = {0}, etSuffix[32] = {0}, etText[96] = {0};

    const nlohmann::json* ann = findSelAnn(sheet, tab.selAnnViewId, tab.selAnnId);
    if (s_annEditOpen) {
        if (!ann) { s_annEditOpen = false; }
        else {
            ImGui::OpenPopup("Edit Dimension##dimedit");
            s_annEditOpen = false;
            s_annEditLoaded = false;
        }
    }
    if (ann && !s_annEditLoaded) {
        std::string tt = djstr(*ann, "tol_type");
        etTol = tt == "symmetric" ? 1 : tt == "deviation" ? 2 : tt == "limit" ? 3
                                                                : tt == "basic" ? 4 : 0;
        etUp = djnum(*ann, "tol_upper", 0.0);
        etLo = djnum(*ann, "tol_lower", 0.0);
        etPrec = ann->contains("precision") && (*ann)["precision"].is_number_integer()
                     ? (*ann)["precision"].get<int>() : 2;
        etCrit = ann->is_object() && ann->value("critical", false);
        std::snprintf(etPrefix, sizeof(etPrefix), "%s", djstr(*ann, "prefix").c_str());
        std::snprintf(etSuffix, sizeof(etSuffix), "%s", djstr(*ann, "suffix").c_str());
        std::snprintf(etText, sizeof(etText), "%s", djstr(*ann, "text_override").c_str());
        s_annEditLoaded = true;
    }

    const float s = theme::S();
    ImGui::SetNextWindowSize(ImVec2(320 * s, 0), ImGuiCond_Appearing);
    ImGui::SetNextWindowBgAlpha(0.0f);
    if (ImGui::BeginPopup("Edit Dimension##dimedit")) {
        frostPopup("drw_dimedit_popup");
        std::string kind = ann ? djstr(*ann, "kind") : std::string();
        std::string type = ann ? djstr(*ann, "type") : std::string();
        bool numeric = kind == "linear_dim" || kind == "aligned_dim" || kind == "radial_dim" ||
                       kind == "diameter_dim" || kind == "angular_dim" || kind == "hole_callout" ||
                       type == "linear_dim" || type == "aligned_dim";
        if (ann) {
            double val = djnum(*ann, "value", 0.0);
            ImGui::TextDisabled("%s  (value %.4g mm)", kind.empty() ? type.c_str() : kind.c_str(),
                                val);
        }
        ImGui::Separator();
        // Shared field-column width so Tolerance/Upper/Lower/Precision/Prefix/Suffix align in one
        // column (labels form a clean right-edge column beside them).
        const float etFieldW = 150.0f * s;
        if (numeric) {
            const char* tols[] = {"None", "Symmetric  \xC2\xB1", "Deviation  +/-", "Limit",
                                  "Basic  [ ]"};
            ImGui::SetNextItemWidth(etFieldW);
            ImGui::Combo("Tol type", &etTol, tols, IM_ARRAYSIZE(tols));
            // Typical tolerances are +/-0.001..0.5 — the 0.01/0.1 steppers make keyboard-free
            // tweaks practical.
            if (etTol == 1) {
                ImGui::SetNextItemWidth(etFieldW);
                ImGui::InputDouble("Tolerance \xC2\xB1", &etUp, 0.01, 0.1, "%.4f");
            } else if (etTol == 2 || etTol == 3) {
                ImGui::SetNextItemWidth(etFieldW);
                ImGui::InputDouble("Upper", &etUp, 0.01, 0.1, "%.4f");
                ImGui::SetNextItemWidth(etFieldW);
                ImGui::InputDouble("Lower", &etLo, 0.01, 0.1, "%.4f");
            }
            ImGui::SetNextItemWidth(etFieldW);
            ImGui::SliderInt("Precision", &etPrec, 0, 4);
            ImGui::SetNextItemWidth(etFieldW);
            ImGui::InputText("Prefix", etPrefix, sizeof(etPrefix));
            ImGui::SetNextItemWidth(etFieldW);
            ImGui::InputText("Suffix", etSuffix, sizeof(etSuffix));
            ImGui::Checkbox("Critical dimension", &etCrit);
        }
        ImGui::SetNextItemWidth(etFieldW);
        ImGui::InputText(numeric ? "Override text" : "Note text", etText, sizeof(etText));
        ImGui::Separator();
        if (ImGui::Button("Apply", ImVec2(120 * s, 0))) {
            nlohmann::json patch;
            if (numeric) {
                const char* tname[] = {"none", "symmetric", "deviation", "limit", "basic"};
                patch["tol_type"] = tname[etTol];
                patch["tol_upper"] = etUp;
                patch["tol_lower"] = etLo;
                patch["precision"] = etPrec;
                patch["prefix"] = std::string(etPrefix);
                patch["suffix"] = std::string(etSuffix);
                patch["critical"] = etCrit;
            }
            patch["text"] = std::string(etText);
            applyAnnPatch(st, tab, tab.selAnnViewId, tab.selAnnId, patch);
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Delete", ImVec2(90 * s, 0))) {
            deleteAnnotation(st, tab, tab.selAnnViewId, tab.selAnnId);
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel", ImVec2(90 * s, 0))) ImGui::CloseCurrentPopup();
        ImGui::EndPopup();
    } else {
        s_annEditLoaded = false;
    }
}

// GD&T editor popup — opened after the user places a feature control frame / datum target /
// surface finish / weld symbol. Adapts its fields to tab.gdtKind, then commits the full spec
// (with the placement's a/pos) via the matching drawing_add_* RPC.
static void renderGdtEditorPopup(app::AppState& st, DrawingUI& tab) {
    static const char* kSyms[] = {"position",     "flatness",        "perpendicularity",
                                  "parallelism",  "angularity",      "concentricity",
                                  "symmetry",     "profile_surface", "profile_line",
                                  "circularity",  "cylindricity",    "straightness",
                                  "circular_runout", "total_runout"};
    static const char* kMods[] = {"none", "mmc", "lmc", "projected"};
    static int  symIdx = 0, modIdx = 0;
    static bool dia = true;
    static char tolBuf[32] = "0.1";
    static char datA[8] = "A", datB[8] = "", datC[8] = "";
    static char sfRa[16] = "1.6";
    static int  sfMach = 0;  // any|required|prohibited
    static bool sfAll = false;
    static int  weldType = 0;  // fillet|square|vee|bevel|plug
    static char weldSize[16] = "6";
    static bool weldOther = false, weldAll = false;
    static char dtLabel[8] = "A1", dtSize[16] = "";
    static int  dtType = 0;  // point|line|area

    if (tab.gdtEditorOpen) {
        ImGui::OpenPopup("GD&T##gdtedit");
        tab.gdtEditorOpen = false;
    }
    const float s = theme::S();
    ImGui::SetNextWindowSize(ImVec2(340 * s, 0), ImGuiCond_Appearing);
    ImGui::SetNextWindowBgAlpha(0.0f);
    if (!ImGui::BeginPopup("GD&T##gdtedit")) return;
    frostPopup("drw_gdtedit_popup");

    const std::string& k = tab.gdtKind;
    bool commit = false;
    nlohmann::json spec = tab.gdtSpec.is_object() ? tab.gdtSpec : nlohmann::json::object();

    // One shared field-column width for every combo/input so labels form a clean right-edge
    // column; only the datum A/B/C trio stays narrow, on a single shared row.
    const float gdtFieldW = 160.0f * s;

    if (k == "gdt_frame") {
        ImGui::TextUnformatted("Feature Control Frame");
        ImGui::SetNextItemWidth(gdtFieldW);
        ImGui::Combo("Characteristic", &symIdx, kSyms, IM_ARRAYSIZE(kSyms));
        ImGui::SetNextItemWidth(gdtFieldW);
        ImGui::InputText("Tolerance", tolBuf, sizeof(tolBuf));
        ImGui::SetNextItemWidth(gdtFieldW);
        ImGui::Combo("Modifier", &modIdx, kMods, IM_ARRAYSIZE(kMods));
        ImGui::Checkbox("Diameter zone", &dia);
        ImGui::Separator();
        // Datum trio on one row (primary / secondary / tertiary).
        const float datW = 46.0f * s;
        ImGui::SetNextItemWidth(datW); ImGui::InputText("##datA", datA, sizeof(datA));
        ImGui::SameLine();
        ImGui::SetNextItemWidth(datW); ImGui::InputText("##datB", datB, sizeof(datB));
        ImGui::SameLine();
        ImGui::SetNextItemWidth(datW); ImGui::InputText("##datC", datC, sizeof(datC));
        ImGui::SameLine();
        ImGui::TextUnformatted("Datums (1st / 2nd / 3rd)");
        if (ImGui::Button("Place", ImVec2(120 * s, 0))) {
            spec["symbol"] = kSyms[symIdx];
            spec["tol_value"] = std::string(tolBuf);
            spec["tol_diameter"] = dia;
            spec["tol_modifier"] = kMods[modIdx];
            nlohmann::json datums = nlohmann::json::array();
            for (const char* d : {datA, datB, datC})
                if (d[0]) datums.push_back(nlohmann::json{{"ref", std::string(d)}});
            spec["datums"] = datums;
            commit = true;
        }
    } else if (k == "surface_finish") {
        static const char* kMach[] = {"any", "required", "prohibited"};
        ImGui::TextUnformatted("Surface Finish");
        ImGui::SetNextItemWidth(gdtFieldW); ImGui::InputText("Ra", sfRa, sizeof(sfRa));
        ImGui::SetNextItemWidth(gdtFieldW);
        ImGui::Combo("Machining", &sfMach, kMach, IM_ARRAYSIZE(kMach));
        ImGui::Checkbox("All around", &sfAll);
        if (ImGui::Button("Place", ImVec2(120 * s, 0))) {
            spec["value"] = std::string(sfRa);
            spec["machining"] = kMach[sfMach];
            spec["all_around"] = sfAll;
            commit = true;
        }
    } else if (k == "weld_symbol") {
        static const char* kWeld[] = {"fillet", "square", "vee", "bevel", "plug"};
        ImGui::TextUnformatted("Weld Symbol");
        ImGui::SetNextItemWidth(gdtFieldW);
        ImGui::Combo("Type", &weldType, kWeld, IM_ARRAYSIZE(kWeld));
        ImGui::SetNextItemWidth(gdtFieldW); ImGui::InputText("Size", weldSize, sizeof(weldSize));
        ImGui::Checkbox("Other side", &weldOther);
        ImGui::SameLine();
        ImGui::Checkbox("All around", &weldAll);
        if (ImGui::Button("Place", ImVec2(120 * s, 0))) {
            spec["weld_type"] = kWeld[weldType];
            spec["size"] = std::string(weldSize);
            spec["other_side"] = weldOther;
            spec["all_around"] = weldAll;
            commit = true;
        }
    } else if (k == "datum_target") {
        static const char* kT[] = {"point", "line", "area"};
        ImGui::TextUnformatted("Datum Target");
        ImGui::SetNextItemWidth(gdtFieldW); ImGui::InputText("Label", dtLabel, sizeof(dtLabel));
        ImGui::SetNextItemWidth(gdtFieldW);
        ImGui::Combo("Type", &dtType, kT, IM_ARRAYSIZE(kT));
        ImGui::SetNextItemWidth(gdtFieldW); ImGui::InputText("Size", dtSize, sizeof(dtSize));
        if (ImGui::Button("Place", ImVec2(120 * s, 0))) {
            spec["label"] = std::string(dtLabel);
            spec["target_type"] = kT[dtType];
            if (dtSize[0]) spec["size"] = std::string(dtSize);
            commit = true;
        }
    }
    ImGui::SameLine();
    if (ImGui::Button("Cancel", ImVec2(100 * s, 0))) {
        tab.gdtSpec = nlohmann::json::object();
        ImGui::CloseCurrentPopup();
    }
    if (commit) {
        commitGdt(st, tab, tab.gdtViewId, spec);
        tab.gdtSpec = nlohmann::json::object();
        ImGui::CloseCurrentPopup();
    }
    ImGui::EndPopup();
}

static void renderTitleBlockPopup(app::AppState& st, DrawingUI& tab, const nlohmann::json& sheet) {
    static char tbTitle[96] = {0}, tbDrawn[48] = {0}, tbDate[32] = {0}, tbMatl[48] = {0},
                tbFinish[48] = {0}, tbRev[16] = {0}, tbScale[16] = {0}, tbSheetNo[16] = {0},
                tbCompany[64] = {0};
    static int  tbSizeIdx = 1;          // A3 default
    static bool tbLoaded = false;
    static const char* kSizes[] = {"A4", "A3", "A2", "A1", "A0", "A", "B", "C", "D", "E"};

    if (tab.titleBlockOpen) {
        ImGui::OpenPopup("Sheet Properties##sheetprops");
        tab.titleBlockOpen = false;
        tbLoaded = false;
    }
    if (!tbLoaded) {
        const nlohmann::json* tb =
            (sheet.contains("title_block") && sheet["title_block"].is_object())
                ? &sheet["title_block"] : nullptr;
        auto load = [&](const char* k, char* buf, size_t n, const char* fb) {
            std::string v = tb ? djstr(*tb, k) : std::string();
            std::snprintf(buf, n, "%s", v.empty() ? fb : v.c_str());
        };
        load("title", tbTitle, sizeof(tbTitle), "");
        load("drawn_by", tbDrawn, sizeof(tbDrawn), "");
        load("date", tbDate, sizeof(tbDate), "");
        load("material", tbMatl, sizeof(tbMatl), "");
        load("finish", tbFinish, sizeof(tbFinish), "");
        load("rev", tbRev, sizeof(tbRev), "A");
        load("scale", tbScale, sizeof(tbScale), "1:1");
        load("sheet", tbSheetNo, sizeof(tbSheetNo), "1/1");
        load("company", tbCompany, sizeof(tbCompany), "");
        std::string sz = djstr(sheet, "size");
        tbSizeIdx = 1;
        for (int i = 0; i < IM_ARRAYSIZE(kSizes); ++i)
            if (sz == kSizes[i]) { tbSizeIdx = i; break; }
        tbLoaded = true;
    }

    const float s = theme::S();
    ImGui::SetNextWindowSize(ImVec2(360 * s, 0), ImGuiCond_Appearing);
    ImGui::SetNextWindowBgAlpha(0.0f);
    if (ImGui::BeginPopup("Sheet Properties##sheetprops")) {
        frostPopup("drw_sheetprops_popup");
        ImGui::SetNextItemWidth(120 * s);
        ImGui::Combo("Sheet size", &tbSizeIdx, kSizes, IM_ARRAYSIZE(kSizes));
        ImGui::Separator();
        // Short fields share rows (kills the dead vertical space of nine full-width inputs in a
        // 360px popup); Title/Company stay full width.
        const float tbHalfW = 110.0f * s;
        const float tbThirdW = 60.0f * s;
        ImGui::SetNextItemWidth(-FLT_MIN);
        ImGui::InputTextWithHint("##tb_title", "Title", tbTitle, sizeof(tbTitle));
        const float tbCol2X = 190.0f * s;   // fixed 2nd column so rows align vertically
        ImGui::SetNextItemWidth(tbHalfW);
        ImGui::InputText("Drawn by", tbDrawn, sizeof(tbDrawn));
        ImGui::SameLine(tbCol2X);
        ImGui::SetNextItemWidth(tbHalfW);
        ImGui::InputText("Date", tbDate, sizeof(tbDate));
        ImGui::SetNextItemWidth(tbHalfW);
        ImGui::InputText("Material", tbMatl, sizeof(tbMatl));
        ImGui::SameLine(tbCol2X);
        ImGui::SetNextItemWidth(tbHalfW);
        ImGui::InputText("Finish", tbFinish, sizeof(tbFinish));
        ImGui::SetNextItemWidth(tbThirdW);
        ImGui::InputText("Scale", tbScale, sizeof(tbScale));
        ImGui::SameLine();
        ImGui::SetNextItemWidth(tbThirdW);
        ImGui::InputText("Rev", tbRev, sizeof(tbRev));
        ImGui::SameLine();
        ImGui::SetNextItemWidth(tbThirdW);
        ImGui::InputText("Sheet", tbSheetNo, sizeof(tbSheetNo));
        ImGui::SetNextItemWidth(-FLT_MIN);
        ImGui::InputTextWithHint("##tb_company", "Company", tbCompany, sizeof(tbCompany));
        ImGui::Separator();
        if (ImGui::Button("Apply", ImVec2(120 * s, 0))) {
            std::string sheetId = djstr(sheet, "id");
            // Sheet size (only if it changed) then the title-block fields.
            std::string curSize = djstr(sheet, "size");
            if (curSize != kSizes[tbSizeIdx]) {
                nlohmann::json res;
                drawCall(st, "drawing_set_sheet",
                         {{"doc_id", tab.drawingDocId}, {"sheet_id", sheetId},
                          {"size", kSizes[tbSizeIdx]}}, res, "Sheet size");
                absorbDrawingResult(st, tab, res);
                tab.drawNeedFit = true;
            }
            nlohmann::json fields = {
                {"title", tbTitle}, {"drawn_by", tbDrawn}, {"date", tbDate}, {"material", tbMatl},
                {"finish", tbFinish}, {"scale", tbScale}, {"rev", tbRev}, {"sheet", tbSheetNo},
                {"company", tbCompany}};
            nlohmann::json res;
            if (drawCall(st, "drawing_set_titleblock",
                         {{"doc_id", tab.drawingDocId}, {"sheet_id", sheetId}, {"fields", fields}},
                         res, "Title block")) {
                absorbDrawingResult(st, tab, res);
            }
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel", ImVec2(90 * s, 0))) ImGui::CloseCurrentPopup();
        ImGui::EndPopup();
    } else {
        tbLoaded = false;
    }
}

void drawDrawingSheet(app::AppState& st) {
    DrawingUI& tab = g_dwg;
    const float s = theme::S();
    const bool ips = (st.units == "in");

    // Self-create: on entering Drafting, project the active source part into a drawing via host's
    // AppState::makeDrawing() sequence (drawing_new + 4 standard views + drawing_get — driven from
    // here since the new tree's "make_drawing" command dispatch only flips workspace/doc-type).
    // Two guard rails make this robust to the active doc changing underneath the panel:
    //   - the source must actually have projectable geometry — an empty part projects to "no
    //     bodies" and every view fails, so we defer creation until the part has features;
    //   - we (re)create ONLY when the active source is a DIFFERENT doc than the one already drawn,
    //     so a momentary flip of the active doc to a fresh/empty part never CLOBBERS a good sheet.
    // createDrawingFromActive records attemptedFor = the source id (success or hard RPC failure) so
    // a persistently-erroring source can't respin drawing_new every frame.
    {
        const std::string cur = st.docMirror.docId();
        const nlohmann::json& tr = st.docMirror.tree();
        const bool srcHasGeom = tr.is_object() && tr.contains("features") &&
                                tr["features"].is_array() && !tr["features"].empty();
        if (cur != tab.attemptedFor && srcHasGeom)
            createDrawingFromActive(st, tab);
    }

    // ---- compact status strip (a future ribbon owns Rebuild/Fit/Export/Views/Dimensions once
    // dwg_* ribbon wiring lands — see drawing_panel.h; this strip echoes the active tool's current
    // click-stage + the canvas hotkeys in the meantime) ----
    ImGui::PushStyleColor(ImGuiCol_Text, theme::draw2d::statusIdle());
    ImGui::TextUnformatted("Drawing");
    ImGui::PopStyleColor();
    ImGui::SameLine();
    {
        const char* stageMsg = nullptr;
        if (tab.dimToolActive)
            stageMsg = tab.dimStage == 0 ? "Smart Dimension: click vertex A"
                     : tab.dimStage == 1 ? "Smart Dimension: click vertex B"
                                         : "Smart Dimension: click to place text";
        else if (tab.sectionToolActive)
            stageMsg = tab.sectionStage == 0 ? "Section: click the cutting-line start across a view"
                                             : "Section: click the cutting-line end";
        else if (tab.detailToolActive)
            stageMsg = tab.detailStage == 0 ? "Detail: click the circle center on a view"
                                            : "Detail: move to set the radius, click to finish";
        else if (tab.modelItemsActive)  stageMsg = "Model Items: click a view to auto-dimension it";
        else if (tab.holeCalloutActive) stageMsg = "Hole Callout: click a circle";
        else if (tab.centerMarkActive)  stageMsg = "Center Mark: click a circle";
        else if (tab.centerLineActive)
            stageMsg = tab.centerLineStage == 0 ? "Center Line: click the first point"
                                                : "Center Line: click the second point";
        else if (tab.noteActive)        stageMsg = "Note: click to place a note";
        else if (tab.balloonActive)     stageMsg = "Balloon: click to drop an item bubble";

        if (stageMsg) {
            ImGui::PushStyleColor(ImGuiCol_Text, theme::draw2d::statusArmed());
            ImGui::TextUnformatted(stageMsg);
            ImGui::PopStyleColor();
            ImGui::SameLine();
            if (ImGui::SmallButton("Done")) disarmDrawTools(tab);
        } else {
            ImGui::PushStyleColor(ImGuiCol_Text, theme::draw2d::statusIdle());
            ImGui::TextUnformatted("double-click a dim to edit  |  drag to reposition  |  "
                                   "wheel zoom  |  MMB / Space+drag pan  |  Del removes");
            ImGui::PopStyleColor();
        }
    }

    // ---- canvas child ----
    ImVec2 canvasMin = ImGui::GetCursorScreenPos();
    ImVec2 avail = ImGui::GetContentRegionAvail();
    // On the very first frame(s) after entering Drafting the docked window may not be laid out
    // yet, so GetContentRegionAvail returns a near-zero size. The one-shot fit MUST NOT lock in
    // against that (it would freeze a ~7% zoom = 32px floor / A3 width); track whether the canvas
    // is genuinely laid out and defer the fit until it is.
    const bool canvasLaidOut = avail.x > 64.0f && avail.y > 64.0f;
    if (avail.x < 32) avail.x = 32;
    if (avail.y < 32) avail.y = 32;
    ImGui::InvisibleButton("##drawcanvas", avail,
                           ImGuiButtonFlags_MouseButtonLeft | ImGuiButtonFlags_MouseButtonMiddle);
    bool canvasHovered = ImGui::IsItemHovered();
    ImVec2 canvasMax(canvasMin.x + avail.x, canvasMin.y + avail.y);

    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->PushClipRect(canvasMin, canvasMax, true);
    // Canvas backdrop (a touch darker than the panel so the white paper reads as paper).
    dl->AddRectFilled(canvasMin, canvasMax, theme::draw2d::canvasBackdrop());

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
                    theme::draw2d::emptyMsg(), msg);
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
        // Only consider the fit final once the canvas is really laid out; otherwise keep the
        // request armed so the next frame (with a real size) re-frames the sheet properly.
        tab.drawNeedFit = !canvasLaidOut;
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
        dl->AddRectFilled(sh0, sh1, theme::draw2d::paperShadow(), 0.0f);
        dl->AddRectFilled(p0, p1, theme::draw2d::paper(), 0.0f);  // paper is always white
        dl->AddRect(p0, p1, theme::draw2d::paperBorder(), 0.0f, 0, 1.4f * s);
        // Inner trim border (a faint margin line, SolidWorks-style).
        float m = 8.0f * z;  // 8mm margin scaled
        if (m > 4)
            dl->AddRect(ImVec2(p0.x + m, p0.y + m), ImVec2(p1.x - m, p1.y - m),
                        theme::draw2d::trimLine(), 0.0f, 0, 1.0f * s);
    }

    // Line colors: drawings are ink-on-paper, so use fixed dark inks (independent of theme — the
    // paper is always white) for legibility. Hidden lines are a lighter grey + dashed.
    const ImU32 inkVisible = theme::draw2d::inkVisible();
    const ImU32 inkHidden  = theme::draw2d::inkHidden();
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
                dl->AddRectFilled(t0, t1, theme::draw2d::errFill(), 3.0f);
                dl->AddRect(t0, t1, theme::draw2d::errBorder(), 3.0f);
                dl->AddText(ImVec2(t0.x + 6, t0.y + 3), theme::draw2d::errInk(), note.c_str());
                continue;
            }

            // W15: section hatching — fill each section_face polygon with 45° lines (under the
            // edges, so the cut outline draws crisply on top). The bore shows as a gap because the
            // engine returns the cut face split into pieces around the hole.
            if (v.contains("section_faces") && v["section_faces"].is_array()) {
                const ImU32 hatchCol = theme::draw2d::hatch();
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
            const ImU32 markCol = theme::draw2d::sectionMark();
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
                                  theme::draw2d::weldLabelPad(), 2.0f);
                dl->AddText(lp, theme::draw2d::weldLabelInk(), lbl);
            }
        }
    }

    // ---- W17: standard title block in the sheet's bottom-right corner ----
    renderTitleBlock(dl, *sheet, toScreen, paperW, paperH, s);

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
                // Prefer the engine-compiled primitive block (covers dims + all GD&T); fall
                // back to the legacy renderer if an older drawing lacks `draw`.
                if (ann.contains("draw") && ann["draw"].is_object())
                    renderDrawPrimitives(dl, ann, toScreen, z, s, sel);
                else
                    renderDimension(dl, ann, ips, toScreen, s, sel);
            }
        }
    }

    // ---- W13: dimension tool interaction (snap + place) + annotation select/delete ----
    {
        // Esc cancels the in-progress action and disarms whatever tool is armed.
        if (ImGui::IsKeyPressed(ImGuiKey_Escape) && anyDrawToolActive(tab)) {
            disarmDrawTools(tab);
        }
        ImVec2 mouse = ImGui::GetIO().MousePos;
        bool overCanvas = canvasHovered;
        // Convert the cursor to sheet-mm (inverse of toScreen).
        double curMx = (mouse.x - canvasMin.x - panX) / z;
        double curMy = -(mouse.y - canvasMin.y - panY) / z;
        double snapRadiusMm = 9.0 / std::max(z, 1e-4f);   // ~9 px snap radius -> sheet mm

        // ---- Section View tool: click a start on a parent view, click more points for a
        // jogged/offset cut line, then DOUBLE-CLICK (or Enter) to finish. Straight section =
        // just two clicks then finish. Mode (full/half) + reverse come from the ribbon. ----
        if (tab.sectionToolActive && overCanvas) {
            std::string overView = viewAtPoint(*sheet, curMx, curMy, 8.0);
            // Rubber-band preview of the cut-line so far + the segment to the cursor.
            if (!tab.sectionPts.empty()) {
                std::vector<ImVec2> chain;
                for (auto& p : tab.sectionPts) chain.push_back(toScreen(p[0], p[1]));
                chain.push_back(mouse);
                addDashedPolyline(dl, chain, theme::draw2d::sectionPreview(), 1.4f * s, 10.0f * s,
                                  4.0f * s);
            }
            bool dbl = ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left);
            bool finish = dbl || ImGui::IsKeyPressed(ImGuiKey_Enter);
            if (finish && tab.sectionPts.size() >= 2) {
                commitSection(st, tab, tab.sectionViewId, tab.sectionPts, tab.sectionMode,
                              tab.sectionReverse);
                tab.sectionPts.clear(); tab.sectionStage = 0; tab.sectionViewId.clear();
            } else if (!dbl && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !spaceDown) {
                if (tab.sectionPts.empty()) {
                    if (!overView.empty()) {
                        tab.sectionViewId = overView;
                        tab.sectionPts.push_back({(float)curMx, (float)curMy});
                        tab.sectionStage = 1;
                    } else {
                        ui::postToast(ui::ToastLevel::Warn, "Start the cutting line on a view");
                    }
                } else {
                    tab.sectionPts.push_back({(float)curMx, (float)curMy});
                }
            }
        }
        // ---- W15: Detail View tool (click center, drag radius) ----
        else if (tab.detailToolActive && overCanvas) {
            std::string overView = viewAtPoint(*sheet, curMx, curMy, 8.0);
            if (tab.detailStage == 1) {
                ImVec2 c = toScreen(tab.detailCx, tab.detailCy);
                double rmm = std::hypot(curMx - tab.detailCx, curMy - tab.detailCy);
                dl->AddCircle(c, (float)rmm * z, theme::draw2d::sectionPreview(), 48, 1.4f * s);
                dl->AddCircleFilled(c, 2.5f * s, theme::draw2d::sectionPreview());
            }
            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !spaceDown) {
                if (tab.detailStage == 0) {
                    if (!overView.empty()) {
                        tab.detailCx = (float)curMx; tab.detailCy = (float)curMy;
                        tab.detailViewId = overView; tab.detailStage = 1;
                    } else {
                        ui::postToast(ui::ToastLevel::Warn, "Click the detail center on a view");
                    }
                } else if (tab.detailStage == 1) {
                    double rmm = std::hypot(curMx - tab.detailCx, curMy - tab.detailCy);
                    if (rmm < 1.0) {
                        ui::postToast(ui::ToastLevel::Warn, "Drag out a larger detail radius");
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
                dl->AddCircle(sp, 5.0f * s, theme::draw2d::dimSelected(), 12, 1.6f * s);
                dl->AddCircleFilled(sp, 2.0f * s, theme::draw2d::dimSelected());
            }
            // Rubber-band preview of the in-progress dimension.
            if (tab.dimStage == 1) {
                ImVec2 a = toScreen(tab.dimAx, tab.dimAy);
                ImVec2 b = snapped ? toScreen(sx, sy) : mouse;
                dl->AddLine(a, b, theme::draw2d::toolPreview(), 1.2f * s);
            } else if (tab.dimStage == 2) {
                ImVec2 a = toScreen(tab.dimAx, tab.dimAy);
                ImVec2 b = toScreen(tab.dimBx, tab.dimBy);
                dl->AddLine(a, b, theme::draw2d::toolPreview(), 1.2f * s);
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
        }
        // ---- W17: Model Items — click a view to auto-dimension it ----
        else if (tab.modelItemsActive && overCanvas) {
            std::string overView = viewAtPoint(*sheet, curMx, curMy, 8.0);
            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !spaceDown) {
                if (!overView.empty()) { commitModelItems(st, tab, overView); tab.modelItemsActive = false; }
                else ui::postToast(ui::ToastLevel::Warn, "Click a view to auto-dimension");
            }
        }
        // ---- W17: Hole Callout / Center Mark — snap a circle, drop the callout ----
        else if ((tab.holeCalloutActive || tab.centerMarkActive) && overCanvas) {
            double sx, sy; std::string svid;
            bool snapped = snapToVertex(*sheet, curMx, curMy, snapRadiusMm * 2.0, sx, sy, svid);
            if (snapped) {
                ImVec2 sp = toScreen(sx, sy);
                dl->AddCircle(sp, 5.0f * s, theme::draw2d::dimSelected(), 12, 1.6f * s);
            }
            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !spaceDown) {
                std::string overView = !svid.empty() ? svid : viewAtPoint(*sheet, curMx, curMy, 8.0);
                if (overView.empty()) {
                    ui::postToast(ui::ToastLevel::Warn, "Click on a circle in a view");
                } else if (tab.holeCalloutActive) {
                    commitHoleCallout(st, tab, overView, (float)curMx, (float)curMy);
                    tab.holeCalloutActive = false;
                } else {
                    commitExtDim(st, tab, overView, "center_mark",
                                 nlohmann::json::array({curMx, curMy}), nlohmann::json(), "");
                    tab.centerMarkActive = false;
                }
            }
        }
        // ---- W17: Center Line — two clicks ----
        else if (tab.centerLineActive && overCanvas) {
            double sx, sy; std::string svid;
            bool snapped = snapToVertex(*sheet, curMx, curMy, snapRadiusMm, sx, sy, svid);
            if (snapped) {
                ImVec2 sp = toScreen(sx, sy);
                dl->AddCircle(sp, 5.0f * s, theme::draw2d::dimSelected(), 12, 1.6f * s);
            }
            if (tab.centerLineStage == 1) {
                ImVec2 a = toScreen(tab.centerLineAx, tab.centerLineAy);
                ImVec2 b = snapped ? toScreen(sx, sy) : mouse;
                addDashedPolyline(dl, {a, b}, theme::draw2d::toolPreview(), 1.2f * s, 9.0f * s, 4.0f * s);
            }
            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !spaceDown) {
                double mx = snapped ? sx : curMx, my = snapped ? sy : curMy;
                if (tab.centerLineStage == 0) {
                    tab.centerLineAx = (float)mx; tab.centerLineAy = (float)my;
                    tab.dimViewId = !svid.empty() ? svid : viewAtPoint(*sheet, curMx, curMy, 8.0);
                    tab.centerLineStage = 1;
                } else {
                    std::string overView = tab.dimViewId.empty()
                                               ? viewAtPoint(*sheet, curMx, curMy, 8.0)
                                               : tab.dimViewId;
                    if (!overView.empty())
                        commitExtDim(st, tab, overView, "center_line",
                                     nlohmann::json::array({tab.centerLineAx, tab.centerLineAy}),
                                     nlohmann::json::array({mx, my}), "");
                    tab.centerLineStage = 0; tab.dimViewId.clear();
                }
            }
        }
        // ---- W17: Note / Balloon — single click drops the annotation ----
        else if ((tab.noteActive || tab.balloonActive) && overCanvas) {
            double sx, sy; std::string svid;
            bool snapped = tab.balloonActive &&
                           snapToVertex(*sheet, curMx, curMy, snapRadiusMm, sx, sy, svid);
            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !spaceDown) {
                std::string overView = !svid.empty() ? svid : viewAtPoint(*sheet, curMx, curMy, 24.0);
                if (overView.empty() && sheet->contains("views") && (*sheet)["views"].is_array() &&
                    !(*sheet)["views"].empty()) {
                    const auto& v0 = (*sheet)["views"][0];
                    overView = djstr(v0, "view_id").empty() ? djstr(v0, "id") : djstr(v0, "view_id");
                }
                if (!overView.empty()) {
                    if (tab.noteActive)
                        commitExtDim(st, tab, overView, "note",
                                     nlohmann::json::array({curMx, curMy}),
                                     nlohmann::json::array({curMx, curMy}), "NOTE");
                    else
                        commitBalloon(st, tab, overView, snapped ? (float)sx : (float)curMx,
                                      snapped ? (float)sy : (float)curMy);
                }
                tab.noteActive = false; tab.balloonActive = false;
            }
        }
        // ---- GD&T tool — click the feature (anchor), click again to drop the frame/symbol ----
        else if (tab.gdtToolActive && overCanvas) {
            double sx, sy; std::string svid;
            bool snapped = snapToVertex(*sheet, curMx, curMy, snapRadiusMm, sx, sy, svid);
            if (snapped && tab.gdtStage == 0) {
                ImVec2 sp = toScreen(sx, sy);
                dl->AddCircle(sp, 5.0f * s, theme::draw2d::gdtSnap(), 12, 1.6f * s);
            }
            if (tab.gdtStage == 1) {
                ImVec2 a = toScreen(tab.gdtAx, tab.gdtAy);
                dl->AddLine(a, mouse, theme::draw2d::gdtPreview(), 1.2f * s);
            }
            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !spaceDown) {
                if (tab.gdtStage == 0) {
                    double ax = snapped ? sx : curMx, ay = snapped ? sy : curMy;
                    std::string vv = !svid.empty() ? svid : viewAtPoint(*sheet, curMx, curMy, 12.0);
                    if (vv.empty()) {
                        ui::postToast(ui::ToastLevel::Warn, "Click a feature on a view");
                    } else {
                        tab.gdtAx = (float)ax; tab.gdtAy = (float)ay;
                        tab.gdtViewId = vv; tab.gdtStage = 1;
                    }
                } else {
                    tab.gdtSpec = nlohmann::json{
                        {"a", nlohmann::json::array({tab.gdtAx, tab.gdtAy})},
                        {"pos", nlohmann::json::array({curMx, curMy})}};
                    tab.gdtToolActive = false; tab.gdtStage = 0;
                    if (tab.gdtKind == "datum_feature") {
                        // No extra fields needed — commit straight away (engine auto-labels).
                        commitGdt(st, tab, tab.gdtViewId, tab.gdtSpec);
                    } else {
                        tab.gdtEditorOpen = true;  // open the GD&T editor to fill the details
                    }
                }
            }
        }
        // ---- selection + double-click-to-edit + drag-to-reposition (no tool armed) ----
        else if (!anyDrawToolActive(tab) && overCanvas) {
            // Nearest stored annotation's text position to the cursor (sheet-mm pick radius).
            auto pickAnn = [&](double qx, double qy, std::string& hv, std::string& ha,
                               double& hx, double& hy) {
                double bestD2 = (14.0 / std::max(z, 1e-4f)) * (14.0 / std::max(z, 1e-4f));
                if (!(sheet->contains("views") && (*sheet)["views"].is_array())) return;
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
                        double d2 = (ppx - qx) * (ppx - qx) + (ppy - qy) * (ppy - qy);
                        if (d2 < bestD2) { bestD2 = d2; hv = vid; ha = djstr(ann, "id");
                                           hx = ppx; hy = ppy; }
                    }
                }
            };
            // Double-click opens the dimension editor for the annotation under the cursor.
            if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left) && !spaceDown) {
                std::string hv, ha; double hx = 0, hy = 0;
                pickAnn(curMx, curMy, hv, ha, hx, hy);
                if (!ha.empty()) {
                    tab.selAnnViewId = hv; tab.selAnnId = ha;
                    s_annEditOpen = true; s_annEditLoaded = false;
                }
            } else if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !spaceDown && !panning) {
                std::string hv, ha; double hx = 0, hy = 0;
                pickAnn(curMx, curMy, hv, ha, hx, hy);
                tab.selAnnViewId = hv; tab.selAnnId = ha;   // empty clears the selection
                // No annotation under the cursor -> select the VIEW for move/scale/delete.
                if (ha.empty()) tab.selViewId = viewAtPoint(*sheet, curMx, curMy, 4.0);
            }
            // ---- view move: drag the selected view; ghost-rect preview, commit on release --
            if (!tab.selViewId.empty() && tab.selAnnId.empty() && !s_annDragActive && !spaceDown) {
                // find the selected view's position + bbox in the cache
                const nlohmann::json* sv = nullptr;
                for (const auto& v : (*sheet)["views"]) {
                    if (!v.is_object()) continue;
                    std::string vid = djstr(v, "view_id").empty() ? djstr(v, "id")
                                                                   : djstr(v, "view_id");
                    if (vid == tab.selViewId) { sv = &v; break; }
                }
                if (sv && sv->contains("bbox") && (*sv)["bbox"].is_array() &&
                    (*sv)["bbox"].size() == 4 && (*sv)["bbox"][0].is_number()) {
                    double bx0 = (*sv)["bbox"][0].get<double>(), by0 = (*sv)["bbox"][1].get<double>();
                    double bx1 = (*sv)["bbox"][2].get<double>(), by1 = (*sv)["bbox"][3].get<double>();
                    // persistent selection highlight
                    ImVec2 hl0 = toScreen(bx0, by1), hl1 = toScreen(bx1, by0);
                    dl->AddRect(ImVec2(hl0.x - 3, hl0.y - 3), ImVec2(hl1.x + 3, hl1.y + 3),
                                IM_COL32(40, 120, 220, tab.viewDragActive ? 120 : 200), 2.0f, 0,
                                1.4f * s);
                    if (ImGui::IsMouseDragging(ImGuiMouseButton_Left)) {
                        if (!tab.viewDragActive) {
                            ImVec2 dd = ImGui::GetMouseDragDelta(ImGuiMouseButton_Left);
                            ImVec2 press(mouse.x - dd.x, mouse.y - dd.y);
                            if (press.x >= hl0.x - 4 && press.x <= hl1.x + 4 &&
                                press.y >= hl0.y - 4 && press.y <= hl1.y + 4) {
                                tab.viewDragActive = true;
                            }
                        }
                        if (tab.viewDragActive) {
                            ImVec2 dd = ImGui::GetMouseDragDelta(ImGuiMouseButton_Left);
                            // ghost rect at the dragged offset
                            ImVec2 g0(hl0.x + dd.x, hl0.y + dd.y), g1(hl1.x + dd.x, hl1.y + dd.y);
                            dl->AddRect(g0, g1, theme::draw2d::viewDragGhost(), 2.0f, 0, 1.6f * s);
                            tab.viewDragDX = dd.x / z;       // sheet-mm offset
                            tab.viewDragDY = -dd.y / z;
                        }
                    }
                    if (tab.viewDragActive && ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
                        double px = djnum(*sv, "position", 0.0);  // (overwritten below)
                        double cury = 0;
                        if (sv->contains("position") && (*sv)["position"].is_array() &&
                            (*sv)["position"].size() == 2) {
                            px = (*sv)["position"][0].get<double>();
                            cury = (*sv)["position"][1].get<double>();
                        }
                        commitViewMove(st, tab, tab.selViewId, px + tab.viewDragDX,
                                       cury + tab.viewDragDY);
                        tab.viewDragActive = false; tab.viewDragDX = tab.viewDragDY = 0;
                    }
                }
            }
            // Drag the selected annotation's text to reposition it (commit on release).
            if (!tab.selAnnId.empty() && !spaceDown) {
                // current pos of the selected annotation
                double selx = 0, sely = 0; bool haveSel = false;
                for (const auto& v : (*sheet)["views"]) {
                    if (!v.is_object() || !v.contains("annotations") ||
                        !v["annotations"].is_array()) continue;
                    const std::string vid = djstr(v, "view_id").empty() ? djstr(v, "id")
                                                                        : djstr(v, "view_id");
                    if (vid != tab.selAnnViewId) continue;
                    for (const auto& ann : v["annotations"])
                        if (ann.is_object() && djstr(ann, "id") == tab.selAnnId) {
                            auto it = ann.find("pos");
                            if (it != ann.end() && it->is_array() && it->size() == 2) {
                                selx = (*it)[0].get<double>(); sely = (*it)[1].get<double>();
                                haveSel = true;
                            }
                        }
                }
                if (haveSel && ImGui::IsMouseDragging(ImGuiMouseButton_Left)) {
                    if (!s_annDragActive) {
                        // arm only if the drag began near the selected annotation
                        ImVec2 dd = ImGui::GetMouseDragDelta(ImGuiMouseButton_Left);
                        ImVec2 press(mouse.x - dd.x, mouse.y - dd.y);
                        ImVec2 selS = toScreen(selx, sely);
                        float pr = 16.0f * s;
                        if (std::fabs(press.x - selS.x) < pr && std::fabs(press.y - selS.y) < pr)
                            s_annDragActive = true;
                    }
                    if (s_annDragActive) {
                        ImVec2 selS = toScreen(selx, sely);
                        dl->AddLine(selS, mouse, theme::draw2d::annDragLine(), 1.2f * s);
                        dl->AddCircleFilled(mouse, 3.0f * s, theme::draw2d::annDragDot());
                    }
                }
                if (s_annDragActive && ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
                    applyAnnPatch(st, tab, tab.selAnnViewId, tab.selAnnId,
                                  {{"pos", nlohmann::json::array({curMx, curMy})}});
                    s_annDragActive = false;
                }
            }
        }

        // Delete the selected annotation, else the selected view.
        if (!anyDrawToolActive(tab) &&
            (ImGui::IsKeyPressed(ImGuiKey_Delete) || ImGui::IsKeyPressed(ImGuiKey_Backspace))) {
            if (!tab.selAnnId.empty())
                deleteAnnotation(st, tab, tab.selAnnViewId, tab.selAnnId);
            else if (!tab.selViewId.empty())
                removeView(st, tab, tab.selViewId);
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
        dl->AddRectFilled(t0, t1, theme::draw2d::bomFill(), 3.0f);
        dl->AddRect(t0, t1, theme::draw2d::paperBorder(), 3.0f, 0, 1.2f * s);
        const ImU32 ink = theme::draw2d::inkTitle();
        float yy = t0.y + pad;
        dl->AddText(ImVec2(t0.x + pad, yy), ink, "Bill of Materials");
        yy += rowH;
        if (!isAsm || nRows == 0) {
            const char* msg = bom.is_object() && bom.contains("note") && bom["note"].is_string()
                                  ? bom["note"].get<std::string>().c_str()
                                  : (isAsm ? "no components" : "part drawing - no BOM");
            dl->AddText(ImVec2(t0.x + pad, yy), theme::draw2d::mutedLabel(), msg);
        } else {
            // Header row.
            dl->AddText(ImVec2(t0.x + pad, yy), ink, "#");
            dl->AddText(ImVec2(t0.x + pad + wItem, yy), ink, "Part");
            dl->AddText(ImVec2(t0.x + pad + wItem + wName, yy), ink, "Qty");
            dl->AddLine(ImVec2(t0.x + pad, yy + rowH - 2), ImVec2(t1.x - pad, yy + rowH - 2),
                        theme::draw2d::trimLine(), 1.0f * s);
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
        theme::glassBackground(dl, p0, p1, 4.0f);
        dl->AddText(ImVec2(p0.x + 6, p0.y + 3), theme::draw2d::hudText(), hud);
    }

    dl->PopClipRect();

    // ---- W17: editor popups (rendered last; they are separate ImGui windows) ----
    renderDimEditorPopup(st, tab, *sheet);
    renderGdtEditorPopup(st, tab);
    renderTitleBlockPopup(st, tab, *sheet);
}

}  // namespace ui
