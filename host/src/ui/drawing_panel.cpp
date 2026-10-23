// Chudware Drawing panel — 2D sheet renderer. See drawing_panel.h.
//
// The drawing engine hands us, per sheet, everything already compiled to sheet millimetres
// (contract: draw-primitive v2): the sheet frame (border, ISO 5457 zones, ISO 7200 title block,
// projection symbol) in sheet["frame"], each view's visible/hidden/hatch polylines, per-view
// decorations (cutting planes, detail circles, captions) in view["draw"], and every annotation's
// compiled draw block. This panel maps sheet-mm -> screen with one affine (pxPerMm zoom + pan) and
// strokes them with ISO 128 paper-space weights (SheetPen), so the canvas matches the PDF/SVG/DXF
// deliverables. It adds only UI: the paper shadow, hover/selection accents, tool previews, HUD.
#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <functional>
#include <map>
#include <string>
#include <vector>

#include "imgui.h"

#include "drawing_panel.h"

#include "../core/app_state.h"
#include "../ui_theme.h"
#include "file_dialog.h"
#include "layout.h"             // ui::overlayColumnEdge / docTabBandBottom — the sheet's clear rect
#include "metrics.h"            // ui::metrics::U — 4 px grid
#include "widgets/kit.h"        // ui::kit — HUD chips, empty states, sheet-properties form

namespace ui {

// Sheet ink. The drawing sheet is white paper in BOTH themes (exactly like the 3D viewport), so
// everything painted on it comes from the on-canvas sketch-ink family and the light-canvas
// viewport roles — never the panel text/chrome roles, which are near-white in the dark theme.
static inline ImU32 sheetCol(const float role[4], float a = 1.0f) {
    float c[4] = {role[0], role[1], role[2], role[3] * a};
    return Palette::toU32(c);
}

// --- 2D export (PDF / SVG / DXF) for the active Drawing tab -----------------------------------
// Path choice: a native Save-As dialog when available (remembers the Documents folder); when file
// dialogs are disabled (headless / CHUDWARE_NO_FILE_DIALOGS) we fall back to writing next to nothing
// in the cwd as "<title>_drawing.<ext>" and toast the path. A future nicety: per-sheet export +
// remembering the last export dir. The engine writes the file via export_drawing.
static bool s_drawDialogsDisabled() {
    static const bool disabled = std::getenv("CHUDWARE_NO_FILE_DIALOGS") != nullptr;
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
    const bool isDwg = std::strcmp(fmt, "dwg") == 0;
    const bool isSvg = std::strcmp(fmt, "svg") == 0;
    const std::string ext = isDxf ? "dxf" : isDwg ? "dwg" : isSvg ? "svg" : "pdf";
    const std::string base = s_sanitize(tab.title.empty() ? "drawing" : tab.title) + "_drawing";

    std::string path;
    if (s_drawDialogsDisabled()) {
        // Headless fallback: write into the current working directory.
        path = base + "." + ext;
    } else {
        const char* filterDxf = "DXF (*.dxf)\0*.dxf\0All files (*.*)\0*.*\0";
        const char* filterDwg = "AutoCAD DWG (*.dwg)\0*.dwg\0All files (*.*)\0*.*\0";
        const char* filterPdf = "PDF (*.pdf)\0*.pdf\0All files (*.*)\0*.*\0";
        const char* filterSvg = "SVG (*.svg)\0*.svg\0All files (*.*)\0*.*\0";
        std::string suggest = base + "." + ext;
        path = ui::saveFileDialog(isDxf   ? filterDxf
                                  : isDwg ? filterDwg
                                  : isSvg ? filterSvg
                                          : filterPdf,
                                  ext.c_str(), suggest.c_str());
        if (path.empty()) return;  // canceled
    }

    std::string err;
    std::string written = st.docMirror.exportDrawing(st.worker, tab.drawingDocId, path, ext, err);
    if (!written.empty()) {
        st.addLog("[draw] exported " + ext + ": " + written);
        st.toast(ui::ToastLevel::Info, "Exported " + written);
        ui::afterFileWritten(written);
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
        st.markDrawingTabDirty(st.activeTab);     // unsaved until the source part is saved
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
        st.markDrawingTabDirty(st.activeTab);
        st.addLog("[draw] removed annotation " + annId);
    } else {
        st.addLog("[draw] drawing_remove_annotation failed: " + err);
    }
    tab.selAnnViewId.clear();
    tab.selAnnId.clear();
}

// W15: push a finished cutting line to the engine (drawing_add_section{cut_line}) + refresh.
// `pts` is the (possibly jogged/offset) cut-line polyline in sheet mm; mode/reverse carry the
// custom-section controls (full|half + which side is removed).
static void commitSection(AppState& st, AppState::DocTab& tab, const std::string& parentViewId,
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
        st.refreshDrawingTab(st.activeTab);
        st.markDrawingTabDirty(st.activeTab);
        st.addLog("[draw] added " + mode + " section on " + parentViewId);
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
        st.markDrawingTabDirty(st.activeTab);
        st.addLog("[draw] added detail on " + parentViewId);
        tab.drawNeedFit = true;
    } else {
        std::string msg = res.is_object() && res.contains("message") ? djstr(res, "message") : err;
        st.addLog("[draw] drawing_add_detail failed: " + msg);
        st.toast(ui::ToastLevel::Error, "Detail failed: " + (msg.empty() ? "engine error" : msg));
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

// --- W17 detailing: shared RPC plumbing + tool commits ---------------------------------------

// Update the cached drawing from an RPC result (a {drawing:...} envelope, a full {sheets:...} doc,
// or neither -> re-fetch), and mark the tab unsaved.
static void absorbDrawingResult(AppState& st, AppState::DocTab& tab,
                                const nlohmann::json& res, int tabIdx = -1) {
    const int target = tabIdx >= 0 ? tabIdx : st.activeTab;
    if (res.is_object() && res.contains("drawing") && res["drawing"].is_object())
        tab.drawingDoc = res["drawing"];
    else if (res.is_object() && res.contains("sheets") && res["sheets"].is_array())
        tab.drawingDoc = res;
    else
        st.refreshDrawingTab(target);
    st.markDrawingTabDirty(target);
}

// Call a drawing RPC; on failure log + toast and return false. `label` is the user-facing verb.
static bool drawCall(AppState& st, const char* method, const nlohmann::json& params,
                     nlohmann::json& res, const char* label) {
    std::string err;
    if (st.worker.call(method, params, res, err) && res.is_object() && !res.contains("code"))
        return true;
    std::string msg = res.is_object() && res.contains("message") ? djstr(res, "message") : err;
    st.addLog(std::string("[draw] ") + method + " failed: " + msg);
    st.toast(ui::ToastLevel::Error,
             std::string(label) + " failed: " + (msg.empty() ? "engine error" : msg));
    return false;
}

// Async drawing RPC for HLR-heavy ops (add/scale view, arrange, sheet size, title block).
// Keeps the UI live; absorbs the returned drawing on drain. `then` runs after a successful absorb
// (e.g. drawing_set_sheet -> drawing_arrange so the views refit the new paper).
using DrawThen = std::function<void(AppState&, int tabIdx)>;
static void drawCallAsyncOn(AppState& st, int tabIdx, const char* method, nlohmann::json params,
                            const char* label, bool needFit = false, DrawThen then = {}) {
    if (!st.workerStarted) {
        st.toast(ui::ToastLevel::Error, std::string(label) + " failed: worker not started");
        return;
    }
    if (tabIdx < 0 || tabIdx >= (int)st.docTabs.size()) return;
    const std::string methodS = method;
    const std::string labelS = label;
    const std::string drawingId = st.docTabs[tabIdx].drawingDocId;
    st.toast(ui::ToastLevel::Info, labelS + "\xE2\x80\xA6");
    ++st.rebuildsInFlight;
    st.rpc.call(methodS, std::move(params),
                [&st, methodS, labelS, tabIdx, drawingId, needFit,
                 then = std::move(then)](const core::RpcResult& r) {
                    --st.rebuildsInFlight;
                    if (tabIdx < 0 || tabIdx >= (int)st.docTabs.size() ||
                        st.docTabs[tabIdx].drawingDocId != drawingId) {
                        st.addLog("[draw] " + methodS + " ignored — drawing tab changed");
                        return;
                    }
                    auto& tab = st.docTabs[tabIdx];
                    if (!r.ok) {
                        st.addLog("[draw] " + methodS + " failed: " + r.err);
                        st.toast(ui::ToastLevel::Error, labelS + " failed: " + r.err);
                        return;
                    }
                    if (!r.result.is_object() || r.result.contains("code")) {
                        std::string msg = r.result.is_object()
                                              ? djstr(r.result, "message")
                                              : std::string("engine error");
                        st.addLog("[draw] " + methodS + " failed: " + msg);
                        st.toast(ui::ToastLevel::Error,
                                 labelS + " failed: " + (msg.empty() ? "engine error" : msg));
                        return;
                    }
                    absorbDrawingResult(st, tab, r.result, tabIdx);
                    if (needFit) tab.drawNeedFit = true;
                    if (methodS == "drawing_auto_balloon") {
                        const int added = r.result.value("added", 0);
                        st.toast(ui::ToastLevel::Info,
                                 "Auto Balloon: " + std::to_string(added) + " BOM items");
                        st.addLog("[draw] auto balloon placed " + std::to_string(added) +
                                  " BOM items");
                        return;
                    }
                    st.addLog("[draw] " + methodS + " ok");
                    if (then) then(st, tabIdx);
                },
                120000);
}
static void drawCallAsync(AppState& st, const char* method, nlohmann::json params,
                          const char* label, bool needFit = false) {
    drawCallAsyncOn(st, st.activeTab, method, std::move(params), label, needFit);
}

// Clear every modal drawing tool + its in-progress stage (called when arming a new tool).
static void disarmDrawTools(AppState::DocTab& tab) {
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
static bool anyDrawToolActive(const AppState::DocTab& tab) {
    return tab.dimToolActive || tab.sectionToolActive || tab.detailToolActive ||
           tab.modelItemsActive || tab.holeCalloutActive || tab.centerMarkActive ||
           tab.noteActive || tab.balloonActive || tab.centerLineActive || tab.gdtToolActive;
}

// Commit a GD&T element (drawing_add_gdt/datum/datum_target/surface_finish/weld) + absorb.
static void commitGdt(AppState& st, AppState::DocTab& tab, const std::string& viewId,
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
        st.addLog("[draw] added " + tab.gdtKind + " on " + viewId);
    }
}

// Moving a parent also repositions derived-view cuts and detail crops; refresh the
// assembled sheet so their markers and geometry move together with its annotations.
static void commitViewMove(AppState& st, AppState::DocTab& tab, const std::string& viewId,
                           double px, double py) {
    nlohmann::json res;
    nlohmann::json params = {{"doc_id", tab.drawingDocId}, {"view_id", viewId},
                             {"position", nlohmann::json::array({px, py})}};
    if (!drawCall(st, "drawing_set_view", params, res, "Move view")) return;
    absorbDrawingResult(st, tab, res);
    st.addLog("[draw] moved view " + viewId);
}

// Remove a view (drawing_remove_view) + refresh.
static void removeView(AppState& st, AppState::DocTab& tab, const std::string& viewId) {
    nlohmann::json res;
    nlohmann::json params = {{"doc_id", tab.drawingDocId}, {"view_id", viewId}};
    if (drawCall(st, "drawing_remove_view", params, res, "Delete view")) {
        st.refreshDrawingTab(st.activeTab);
        st.markDrawingTabDirty(st.activeTab);
        tab.selViewId.clear();
        st.addLog("[draw] removed view " + viewId);
    }
}

// Set a view's scale (drawing_set_view {scale}) — re-projects this view at the new scale.
static void commitViewScale(AppState& st, AppState::DocTab& tab, const std::string& viewId,
                            double scale) {
    nlohmann::json params = {{"doc_id", tab.drawingDocId}, {"view_id", viewId}, {"scale", scale}};
    // HLR re-project can be multi-second — async.
    drawCallAsync(st, "drawing_set_view", std::move(params), "Scale view");
}

static void addStdView(AppState& st, AppState::DocTab& tab, const char* type, const char* orient,
                       double px, double py) {
    nlohmann::json params = {{"doc_id", tab.drawingDocId}, {"type", type}, {"orientation", orient},
                             {"position", nlohmann::json::array({px, py})}};
    // HLR for a new view is the expensive path — async.
    drawCallAsync(st, "drawing_add_view", std::move(params), "Add view", /*needFit=*/true);
}

// Auto-dimension the clicked view (overall envelope + per-circle diameter callouts).
static void commitModelItems(AppState& st, AppState::DocTab& tab, const std::string& viewId) {
    nlohmann::json res;
    if (drawCall(st, "drawing_model_items",
                 {{"doc_id", tab.drawingDocId}, {"view_id", viewId}}, res, "Model Items")) {
        absorbDrawingResult(st, tab, res);
        int added = res.is_object() ? (int)djnum(res, "added", 0) : 0;
        st.addLog("[draw] model items added " + std::to_string(added) + " dims on " + viewId);
        st.toast(ui::ToastLevel::Info, "Auto-dimensioned (" + std::to_string(added) + ")");
    }
}

// Smart hole callout: snaps the nearest circle near (ax,ay) -> "n× ⌀d".
static void commitHoleCallout(AppState& st, AppState::DocTab& tab, const std::string& viewId,
                              float ax, float ay) {
    nlohmann::json res;
    nlohmann::json params = {{"doc_id", tab.drawingDocId}, {"view_id", viewId}, {"kind", "hole"},
                             {"a", nlohmann::json::array({ax, ay})}};
    if (drawCall(st, "drawing_add_callout", params, res, "Hole callout")) {
        absorbDrawingResult(st, tab, res);
        st.addLog("[draw] hole callout on " + viewId);
    }
}

// Item-number balloon at the click point (auto-incrementing).
static void commitBalloon(AppState& st, AppState::DocTab& tab, const std::string& viewId,
                          float ax, float ay) {
    nlohmann::json res;
    nlohmann::json params = {{"doc_id", tab.drawingDocId}, {"view_id", viewId}, {"kind", "balloon"},
                             {"a", nlohmann::json::array({ax, ay})}, {"item", tab.balloonNext}};
    if (drawCall(st, "drawing_add_callout", params, res, "Balloon")) {
        absorbDrawingResult(st, tab, res);
        ++tab.balloonNext;
        st.addLog("[draw] balloon on " + viewId);
    }
}

// Center mark / center line / note via drawing_add_dim (type carries the kind).
static void commitExtDim(AppState& st, AppState::DocTab& tab, const std::string& viewId,
                         const char* type, const nlohmann::json& a, const nlohmann::json& b,
                         const std::string& text) {
    nlohmann::json res;
    nlohmann::json params = {{"doc_id", tab.drawingDocId}, {"view_id", viewId}, {"type", type},
                             {"a", a}};
    if (b.is_array()) params["b"] = b;
    if (!text.empty()) params["text"] = text;
    if (drawCall(st, "drawing_add_dim", params, res, type)) {
        absorbDrawingResult(st, tab, res);
        st.addLog(std::string("[draw] added ") + type + " on " + viewId);
    }
}

// Patch detailing fields on an existing annotation (tolerance / critical / prefix / pos / ...).
static void applyAnnPatch(AppState& st, AppState::DocTab& tab, const std::string& viewId,
                          const std::string& annId, const nlohmann::json& patch) {
    nlohmann::json res;
    nlohmann::json params = patch;
    params["doc_id"] = tab.drawingDocId;
    params["view_id"] = viewId;
    params["ann_id"] = annId;
    if (drawCall(st, "drawing_set_annotation", params, res, "Edit dimension")) {
        absorbDrawingResult(st, tab, res);
        st.addLog("[draw] edited annotation " + annId);
    }
}

bool drawingHandleCommand(AppState& st, const std::string& cmd) {
    if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) return true;
    AppState::DocTab& tab = st.docTabs[st.activeTab];
    if (tab.kind != AppState::DocTab::TabKind::Drawing || tab.drawingDocId.empty()) {
        st.toast(ui::ToastLevel::Warn, "Open a drawing first");
        return true;
    }

    // --- View Layout tab ---
    if (cmd == "dwg_rebuild")     { disarmDrawTools(tab); st.refreshDrawingTab(st.activeTab); return true; }
    if (cmd == "dwg_fit")         { tab.drawNeedFit = true; return true; }
    if (cmd == "dwg_export_dxf")  { exportActiveDrawing(st, "dxf"); return true; }
    if (cmd == "dwg_export_pdf")  { exportActiveDrawing(st, "pdf"); return true; }
    if (cmd == "dwg_export_svg")  { exportActiveDrawing(st, "svg"); return true; }
    if (cmd == "dwg_export_dwg")  { exportActiveDrawing(st, "dwg"); return true; }
    if (cmd == "dwg_arrange") {
        // Re-run the engine's professional layout (projection + scale kept from the title block).
        disarmDrawTools(tab);
        nlohmann::json p = {{"doc_id", tab.drawingDocId}};
        if (tab.drawingDoc.contains("sheets") && tab.drawingDoc["sheets"].is_array() &&
            !tab.drawingDoc["sheets"].empty() && tab.drawingDoc["sheets"][0].is_object())
            p["sheet_id"] = djstr(tab.drawingDoc["sheets"][0], "id");
        drawCallAsync(st, "drawing_arrange", std::move(p), "Arrange views", /*needFit=*/true);
        return true;
    }
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
    if (cmd == "dwg_balloon") { disarmDrawTools(tab); tab.balloonActive = true; return true; }
    if (cmd == "dwg_auto_balloon") {
        disarmDrawTools(tab);
        drawCallAsync(st, "drawing_auto_balloon", {{"doc_id", tab.drawingDocId}},
                      "Auto Balloon");
        return true;
    }
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
        st.toast(ui::ToastLevel::Info, "Click the feature, then click to place");
    };
    if (cmd == "dwg_gdt")            { armGdt("gdt_frame"); return true; }
    if (cmd == "dwg_datum")          { armGdt("datum_feature"); return true; }
    if (cmd == "dwg_datum_target")   { armGdt("datum_target"); return true; }
    if (cmd == "dwg_surface_finish") { armGdt("surface_finish"); return true; }
    if (cmd == "dwg_weld")           { armGdt("weld_symbol"); return true; }

    // --- Custom-section controls (View Layout tab) ---
    if (cmd == "dwg_section_half") {
        tab.sectionMode = (tab.sectionMode == "half") ? "full" : "half";
        st.toast(ui::ToastLevel::Info, std::string("Section mode: ") + tab.sectionMode);
        return true;
    }
    if (cmd == "dwg_section_reverse") {
        tab.sectionReverse = !tab.sectionReverse;
        st.toast(ui::ToastLevel::Info, tab.sectionReverse ? "Section: reversed side"
                                                          : "Section: normal side");
        return true;
    }

    // --- Scale the selected view (View Layout tab) ---
    auto setScale = [&](double sc) {
        if (tab.selViewId.empty()) { st.toast(ui::ToastLevel::Warn, "Select a view first"); return; }
        commitViewScale(st, tab, tab.selViewId, sc);
    };
    if (cmd == "dwg_scale_1_1") { setScale(1.0); return true; }
    if (cmd == "dwg_scale_1_2") { setScale(0.5); return true; }
    if (cmd == "dwg_scale_2_1") { setScale(2.0); return true; }
    return false;
}

// ---- sheet ink: the v2 draw-primitive contract ----------------------------------------------------
// Every line on the sheet — frame, title block, view geometry, core view decorations (cutting
// planes, detail circles, captions) and every annotation — arrives as compiled primitives in
// sheet millimetres. This painter maps them to screen with ISO 128 PAPER-SPACE weights and dash
// patterns scaled by the live zoom, so the canvas matches the PDF / SVG / DXF deliverables.
static constexpr float kThinMm = 0.25f;    // ISO 128 type 01.1 (dims, hatch, hidden, centre)
static constexpr float kThickMm = 0.50f;   // outlines, frame, title-block border, cutting-plane ends
static constexpr float kHiddenPat[] = {3.0f, 1.5f};             // dash, gap (mm)
static constexpr float kChainPat[] = {6.0f, 1.0f, 0.5f, 1.0f};  // long dash, gap, dot, gap (mm)

// Stroke an open polyline with an on/off pattern in screen px (even entries draw). The phase runs
// continuously across vertices, so dashes turn corners instead of restarting per segment.
static void strokePatternPx(ImDrawList* dl, const ImVec2* pts, int n, ImU32 col, float w,
                            const float* pat, int np) {
    if (n < 2 || np < 2) return;
    int idx = 0;
    float left = pat[0];
    for (int i = 1; i < n; ++i) {
        const ImVec2 a = pts[i - 1], b = pts[i];
        const float len = std::sqrt((b.x - a.x) * (b.x - a.x) + (b.y - a.y) * (b.y - a.y));
        if (len < 1e-4f) continue;
        const float ux = (b.x - a.x) / len, uy = (b.y - a.y) / len;
        float d = 0.0f;
        while (d < len) {
            const float step = std::min(left, len - d);
            if ((idx & 1) == 0)
                dl->AddLine(ImVec2(a.x + ux * d, a.y + uy * d),
                            ImVec2(a.x + ux * (d + step), a.y + uy * (d + step)), col, w);
            d += step;
            left -= step;
            if (left <= 1e-4f) { idx = (idx + 1) % np; left = pat[idx]; }
        }
    }
}

// The atlas has no cell for the counterbore / countersink / depth symbols; spell them as the
// ASCII words the PDF and DXF exports use (the diameter sign is in the atlas and stays).
static void mapCadGlyphs(std::string& s) {
    static const char* const kSym[] = {"\xE2\x8C\xB4", "\xE2\x8C\xB5", "\xE2\x86\xA7"};
    static const char* const kWord[] = {"CBORE", "CSK", "DEPTH"};
    for (int k = 0; k < 3; ++k) {
        size_t at;
        while ((at = s.find(kSym[k])) != std::string::npos) {
            std::string rep = kWord[k];
            if (at + 3 < s.size() && s[at + 3] != ' ') rep += ' ';
            s.replace(at, 3, rep);
        }
    }
}

struct SheetPen {
    ImDrawList* dl;
    ImVec2 origin;              // screen position of sheet (0,0)
    float z;                    // px per sheet mm
    float thin = 1.0f, thick = 2.0f;
    ImFont* font = nullptr;
    float capRatio = 0.7f;      // cap height / font size   (measured from the 'H' glyph)
    float baseRatio = 0.8f;     // baseline below the line top / font size
    std::vector<ImVec2> buf;    // reused per polyline (no per-line allocation)
    std::string textBuf;

    SheetPen(ImDrawList* d, ImVec2 o, float zoom) : dl(d), origin(o), z(zoom) {
        // Paper-space weights, floored at a hairline; thick always reads heavier than thin.
        thin = std::max(ui::metrics::stroke(1.0f), kThinMm * z);
        thick = std::max(kThickMm * z, thin * 1.8f);
        font = ImGui::GetFont();
        const ImFontGlyph* g = font->FindGlyph((ImWchar)'H');
        if (g && font->FontSize > 0.0f && g->Y1 > g->Y0) {
            capRatio = (g->Y1 - g->Y0) / font->FontSize;
            baseRatio = g->Y1 / font->FontSize;
        }
    }
    ImVec2 at(double x, double y) const {
        return ImVec2(origin.x + (float)x * z, origin.y - (float)y * z);
    }
    static bool num2(const nlohmann::json& p) {
        return p.is_array() && p.size() == 2 && p[0].is_number() && p[1].is_number();
    }
    bool load(const nlohmann::json& pl) {
        buf.clear();
        if (!pl.is_array()) return false;
        for (const auto& p : pl)
            if (num2(p)) buf.push_back(at(p[0].get<double>(), p[1].get<double>()));
        return buf.size() >= 2;
    }
    void solid(const nlohmann::json& list, ImU32 col, float w) {
        if (!list.is_array()) return;
        for (const auto& pl : list) {
            if (!load(pl)) continue;
            ImDrawFlags flags = 0;
            const ImVec2 f = buf.front(), l = buf.back();
            if (buf.size() >= 4 && std::fabs(f.x - l.x) < 0.01f && std::fabs(f.y - l.y) < 0.01f) {
                buf.pop_back();   // closed loop: a mitred joint instead of a seam
                flags = ImDrawFlags_Closed;
            }
            dl->AddPolyline(buf.data(), (int)buf.size(), col, flags, w);
        }
    }
    void patterned(const nlohmann::json& list, ImU32 col, float w, const float* patMm, int n) {
        if (!list.is_array()) return;
        // Never fewer than one logical px per pattern mm, so a zoomed-out sheet keeps legible dashes.
        const float mmPx = std::max(z, ui::metrics::px(1.0f));
        float pat[4];
        for (int i = 0; i < n && i < 4; ++i) pat[i] = patMm[i] * mmPx;
        for (const auto& pl : list)
            if (load(pl)) strokePatternPx(dl, buf.data(), (int)buf.size(), col, w, pat, std::min(n, 4));
    }
    void arcs(const nlohmann::json& list, ImU32 col, float w) {
        if (!list.is_array()) return;
        constexpr double kTwoPi = 6.283185307179586;
        for (const auto& a : list) {
            if (!a.is_object() || !a.contains("center") || !num2(a["center"])) continue;
            const double r = djnum(a, "radius", 0.0);
            if (!(r > 0.0)) continue;
            const double cx = a["center"][0].get<double>(), cy = a["center"][1].get<double>();
            const double a0 = djnum(a, "a0", 0.0);
            double da = djnum(a, "a1", kTwoPi) - a0;
            const bool full = std::fabs(da) >= kTwoPi - 1e-9;
            if (full) da = kTwoPi;
            const int segs = std::clamp((int)(std::fabs(da) * r * z / 3.0), 8, 512);
            buf.clear();
            const int count = full ? segs : segs + 1;
            for (int i = 0; i < count; ++i) {
                const double t = a0 + da * (double)i / (double)segs;
                buf.push_back(at(cx + r * std::cos(t), cy + r * std::sin(t)));
            }
            dl->AddPolyline(buf.data(), (int)buf.size(), col, full ? ImDrawFlags_Closed : 0, w);
        }
    }
    void tris(const nlohmann::json& list, ImU32 col) {
        if (!list.is_array()) return;
        for (const auto& t : list) {
            if (!t.is_array() || t.size() != 3 || !num2(t[0]) || !num2(t[1]) || !num2(t[2]))
                continue;
            dl->AddTriangleFilled(at(t[0][0].get<double>(), t[0][1].get<double>()),
                                  at(t[1][0].get<double>(), t[1][1].get<double>()),
                                  at(t[2][0].get<double>(), t[2][1].get<double>()), col);
        }
    }
    // `h` is the cap height in mm (ISO 3098); align/valign per the contract, measured with the
    // real rendered width.
    void texts(const nlohmann::json& list, ImU32 col) {
        if (!list.is_array()) return;
        for (const auto& t : list) {
            if (!t.is_object() || !t.contains("pos") || !num2(t["pos"])) continue;
            auto it = t.find("text");
            if (it == t.end() || !it->is_string()) continue;
            const std::string* raw = it->get_ptr<const std::string*>();
            if (raw->empty()) continue;
            const std::string* txt = raw;
            if (raw->find('\xE2') != std::string::npos) {
                textBuf = *raw;
                mapCadGlyphs(textBuf);
                txt = &textBuf;
            }
            const float capPx = (float)djnum(t, "h", 3.5) * z;
            const float fpx = capPx / capRatio;
            if (fpx < 1.0f) continue;
            const char* b = txt->c_str();
            const char* e = b + txt->size();
            const ImVec2 a = at(t["pos"][0].get<double>(), t["pos"][1].get<double>());
            const std::string align = djstr(t, "align"), valign = djstr(t, "valign");
            float x = a.x;
            if (align == "center" || align == "right") {
                const float w = font->CalcTextSizeA(fpx, FLT_MAX, 0.0f, b, e).x;
                x -= align == "center" ? w * 0.5f : w;
            }
            float baseY = a.y;                          // "base": pos is the baseline
            if (valign == "mid") baseY += capPx * 0.5f; // pos is mid cap-height
            else if (valign == "top") baseY += capPx;   // pos is the top of capitals
            dl->AddText(font, fpx, ImVec2(x, baseY - baseRatio * fpx), col, b, e);
        }
    }
    // One draw block. Missing keys are empty; unknown keys are ignored.
    void block(const nlohmann::json& d, ImU32 col) {
        if (!d.is_object()) return;
        auto arr = [&](const char* k) -> const nlohmann::json* {
            auto it = d.find(k);
            return (it != d.end() && it->is_array() && !it->empty()) ? &*it : nullptr;
        };
        if (const auto* a = arr("segments")) solid(*a, col, thin);
        if (const auto* a = arr("thick")) solid(*a, col, thick);
        if (const auto* a = arr("dashed")) patterned(*a, col, thin, kHiddenPat, 2);
        if (const auto* a = arr("chain")) patterned(*a, col, thin, kChainPat, 4);
        if (const auto* a = arr("chain_thick")) patterned(*a, col, thick, kChainPat, 4);
        if (const auto* a = arr("arcs")) arcs(*a, col, thin);
        if (const auto* a = arr("tris")) tris(*a, col);
        if (const auto* a = arr("texts")) texts(*a, col);
    }
};

static std::string viewIdOf(const nlohmann::json& v) {
    std::string id = djstr(v, "view_id");
    return id.empty() ? djstr(v, "id") : id;
}

// A view's sheet-mm bbox (false for table overlays / views without geometry).
static bool viewBox(const nlohmann::json& v, double& x0, double& y0, double& x1, double& y1) {
    auto it = v.find("bbox");
    if (it == v.end() || !it->is_array() || it->size() != 4) return false;
    const auto& b = *it;
    if (!(b[0].is_number() && b[1].is_number() && b[2].is_number() && b[3].is_number()))
        return false;
    x0 = b[0].get<double>(); y0 = b[1].get<double>();
    x1 = b[2].get<double>(); y1 = b[3].get<double>();
    return true;
}

// The point an annotation is grabbed by: its compiled draw block's pick, else its stored pos.
static bool annAnchor(const nlohmann::json& ann, double& x, double& y) {
    auto dr = ann.find("draw");
    if (dr != ann.end() && dr->is_object()) {
        auto pk = dr->find("pick");
        if (pk != dr->end() && SheetPen::num2(*pk)) {
            x = (*pk)[0].get<double>(); y = (*pk)[1].get<double>();
            return true;
        }
    }
    auto it = ann.find("pos");
    if (it != ann.end() && SheetPen::num2(*it)) {
        x = (*it)[0].get<double>(); y = (*it)[1].get<double>();
        return true;
    }
    return false;
}

// Nearest editable annotation anchor within `radiusMm` (table overlays are not annotations the
// dimension editor can patch, so they never pick).
static void pickAnnotation(const nlohmann::json& sheet, double qx, double qy, double radiusMm,
                           std::string& hv, std::string& ha, double& hx, double& hy) {
    double best = radiusMm * radiusMm;
    if (!(sheet.contains("views") && sheet["views"].is_array())) return;
    for (const auto& v : sheet["views"]) {
        if (!v.is_object() || djstr(v, "type") == "table_overlay" || !v.contains("annotations") ||
            !v["annotations"].is_array())
            continue;
        for (const auto& ann : v["annotations"]) {
            double px, py;
            if (!ann.is_object() || !annAnchor(ann, px, py)) continue;
            const double d2 = (px - qx) * (px - qx) + (py - qy) * (py - qy);
            if (d2 < best) {
                best = d2; hv = viewIdOf(v); ha = djstr(ann, "id"); hx = px; hy = py;
            }
        }
    }
}

// --- W17: editor / drag interaction state (only the active Drawing tab interacts) -------------
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

// The dimension/annotation editor popup: double-click a dim to open it. Edits tolerance, prefix/
// suffix, precision, "Critical", and a verbatim text override -> drawing_set_annotation.
// Glass-v2 popup frosting. Call once immediately after a BeginPopup()/BeginPopupModal() succeeds:
// it splits the window draw list (content lands on channel 1) and returns a handle to finish().
// finish() paints the frosted adaptivePanel/glassPanel behind the content on channel 0 and merges,
// so drawing-mode dialogs float as part of the app-wide glass family (matching section_tool + the
// evaluate/equations dialogs) instead of the flat legacy PopupBg fill.
struct GlassPopupScope {
    ImDrawList* dl = nullptr;
    const char* id = nullptr;
    void finish() {
        const float s = ui::uiScale;
        ImVec2 p0 = ImGui::GetWindowPos();
        ImVec2 p1(p0.x + ImGui::GetWindowSize().x, p0.y + ImGui::GetWindowSize().y);
        dl->ChannelsSetCurrent(0);
        const ui::AdaptiveSurface& surf =
            ui::adaptivePanel(ui::panelId(id), ImRect(p0, p1), ui::GlassGroup::Hud);
        ui::glassPanel(dl, ImRect(p0, p1), ui::GlassLevel::Regular,
                       ImGui::GetStyle().PopupRounding / s, surf);
        dl->ChannelsMerge();
    }
};
// Call AFTER BeginPopup() returns true. (Pair with ImGui::SetNextWindowBgAlpha(0.0f) BEFORE the
// BeginPopup so the flat legacy PopupBg fill is suppressed and the glass replaces it.)
static GlassPopupScope beginGlassPopup(const char* panelId) {
    GlassPopupScope sc;
    sc.dl = ImGui::GetWindowDrawList();
    sc.id = panelId;
    sc.dl->ChannelsSplit(2);
    sc.dl->ChannelsSetCurrent(1);
    return sc;
}

static void renderDimEditorPopup(AppState& st, AppState::DocTab& tab, const nlohmann::json& sheet) {
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

    ImGui::SetNextWindowSize(ImVec2(320 * ui::uiScale, 0), ImGuiCond_Appearing);
    ImGui::SetNextWindowBgAlpha(0.0f);
    if (ImGui::BeginPopup("Edit Dimension##dimedit")) {
        GlassPopupScope glass = beginGlassPopup("drw_dimedit_popup");
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
        const float etFieldW = 150.0f * ui::uiScale;
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
        if (ImGui::Button("Apply", ImVec2(120 * ui::uiScale, 0))) {
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
        if (ImGui::Button("Delete", ImVec2(90 * ui::uiScale, 0))) {
            deleteAnnotation(st, tab, tab.selAnnViewId, tab.selAnnId);
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel", ImVec2(90 * ui::uiScale, 0))) ImGui::CloseCurrentPopup();
        glass.finish();
        ImGui::EndPopup();
    } else {
        s_annEditLoaded = false;
    }
}

// The title-block / sheet-properties editor popup (opened from the View Layout ribbon).
// GD&T editor popup — opened after the user places a feature control frame / datum target /
// surface finish / weld symbol. Adapts its fields to tab.gdtKind, then commits the full spec
// (with the placement's a/pos) via the matching drawing_add_* RPC.
static void renderGdtEditorPopup(AppState& st, AppState::DocTab& tab) {
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
    ImGui::SetNextWindowSize(ImVec2(340 * ui::uiScale, 0), ImGuiCond_Appearing);
    ImGui::SetNextWindowBgAlpha(0.0f);
    if (!ImGui::BeginPopup("GD&T##gdtedit")) return;
    GlassPopupScope glass = beginGlassPopup("drw_gdtedit_popup");

    const std::string& k = tab.gdtKind;
    bool commit = false;
    nlohmann::json spec = tab.gdtSpec.is_object() ? tab.gdtSpec : nlohmann::json::object();

    // One shared field-column width for every combo/input so labels form a clean right-edge
    // column; only the datum A/B/C trio stays narrow, on a single shared row.
    const float gdtFieldW = 160.0f * ui::uiScale;

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
        const float datW = 46.0f * ui::uiScale;
        ImGui::SetNextItemWidth(datW); ImGui::InputText("##datA", datA, sizeof(datA));
        ImGui::SameLine();
        ImGui::SetNextItemWidth(datW); ImGui::InputText("##datB", datB, sizeof(datB));
        ImGui::SameLine();
        ImGui::SetNextItemWidth(datW); ImGui::InputText("##datC", datC, sizeof(datC));
        ImGui::SameLine();
        ImGui::TextUnformatted("Datums (1st / 2nd / 3rd)");
        if (ImGui::Button("Place", ImVec2(120 * ui::uiScale, 0))) {
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
        if (ImGui::Button("Place", ImVec2(120 * ui::uiScale, 0))) {
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
        if (ImGui::Button("Place", ImVec2(120 * ui::uiScale, 0))) {
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
        if (ImGui::Button("Place", ImVec2(120 * ui::uiScale, 0))) {
            spec["label"] = std::string(dtLabel);
            spec["target_type"] = kT[dtType];
            if (dtSize[0]) spec["size"] = std::string(dtSize);
            commit = true;
        }
    }
    ImGui::SameLine();
    if (ImGui::Button("Cancel", ImVec2(100 * ui::uiScale, 0))) {
        tab.gdtSpec = nlohmann::json::object();
        ImGui::CloseCurrentPopup();
    }
    if (commit) {
        commitGdt(st, tab, tab.gdtViewId, spec);
        tab.gdtSpec = nlohmann::json::object();
        ImGui::CloseCurrentPopup();
    }
    glass.finish();
    ImGui::EndPopup();
}

// ---- Sheet / title-block properties ----------------------------------------------------------------
// Opened by dwg_sheet_props / dwg_title_block. Sheet controls (paper, orientation, projection,
// scale, units) apply immediately through the engine's professional layout pass; the title-block
// fields are edited together and applied with one drawing_set_titleblock.
static const char* const kSheetSizeKeys[] = {"A4", "A3", "A2", "A1", "A0", "A", "B", "C", "D", "E"};
static const char* const kSheetSizeLabels[] = {
    "ISO A4   297 \xC3\x97 210 mm",   "ISO A3   420 \xC3\x97 297 mm",
    "ISO A2   594 \xC3\x97 420 mm",   "ISO A1   841 \xC3\x97 594 mm",
    "ISO A0   1189 \xC3\x97 841 mm",  "ANSI A   11 \xC3\x97 8.5 in",
    "ANSI B   17 \xC3\x97 11 in",     "ANSI C   22 \xC3\x97 17 in",
    "ANSI D   34 \xC3\x97 22 in",     "ANSI E   44 \xC3\x97 34 in"};
static const char* const kProjLabels[] = {"Third angle", "First angle"};
static const char* const kProjTips[] = {"ASME Y14.3 / North America: top view above the front.",
                                        "ISO 128 / Europe: top view below the front."};
static const char* const kScaleLabels[] = {"Auto", "10:1", "5:1", "2:1", "1:1", "1:2",
                                           "1:5",  "1:10", "1:20", "1:50", "1:100"};
static const double kScaleValues[] = {0.0, 10.0, 5.0, 2.0, 1.0, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01};
static const char* const kUnitLabels[] = {"Millimetres", "Inches"};

struct TitleField {
    const char* key;
    const char* label;
    const char* hint;
    const char* legacy;   // older alias still read by the engine (nullptr = none)
};
static const TitleField kTitleFields[] = {
    {"title", "Title", "PART NAME", nullptr},
    {"part_number", "Part number", "CW-0001", "part_no"},
    {"rev", "Revision", "A", "revision"},
    {"drawing_standard", "Standard", "ISO 128", nullptr},
    {"material", "Material", "AISI 304", nullptr},
    {"finish", "Finish", "As machined", nullptr},
    {"general_tolerance", "General tolerance", "ISO 2768-m", nullptr},
    {"mass", "Mass", "0.25 kg", nullptr},
    {"drawn_by", "Drawn by", "Initials", "drawn"},
    {"date", "Date", "YYYY-MM-DD", nullptr},
    {"checked_by", "Checked by", "", nullptr},
    {"approved_by", "Approved by", "", nullptr},
    {"company", "Company", "", nullptr},
};
static constexpr int kTitleFieldCount = (int)(sizeof(kTitleFields) / sizeof(kTitleFields[0]));

// drawing_arrange on the sheet; projection "" keeps the title block's, scale <= 0 = auto-fit.
static void arrangeSheet(AppState& st, int tabIdx, const std::string& sheetId,
                         const std::string& projection, double scale) {
    if (tabIdx < 0 || tabIdx >= (int)st.docTabs.size()) return;
    nlohmann::json params = {{"doc_id", st.docTabs[tabIdx].drawingDocId}, {"sheet_id", sheetId}};
    if (!projection.empty()) params["projection"] = projection;
    if (scale > 0.0) params["scale"] = scale;
    drawCallAsyncOn(st, tabIdx, "drawing_arrange", std::move(params), "Arrange views",
                    /*needFit=*/true);
}

// Paper size (e.g. "A4P") then a re-arrange so the views refit the new paper.
static void applySheetSize(AppState& st, int tabIdx, const std::string& sheetId,
                           const std::string& size, const std::string& projection, double scale) {
    if (tabIdx < 0 || tabIdx >= (int)st.docTabs.size()) return;
    drawCallAsyncOn(st, tabIdx, "drawing_set_sheet",
                    {{"doc_id", st.docTabs[tabIdx].drawingDocId}, {"sheet_id", sheetId},
                     {"size", size}},
                    "Sheet size", /*needFit=*/true,
                    [sheetId, projection, scale](AppState& s2, int idx) {
                        arrangeSheet(s2, idx, sheetId, projection, scale);
                    });
}

static void renderTitleBlockPopup(AppState& st, AppState::DocTab& tab, const nlohmann::json& sheet) {
    static int sizeIdx = 1, scaleIdx = 0, projIdx = 0, unitsIdx = 0;
    static bool portrait = false;
    static bool loaded = false;
    static double loadedRev = -1.0;
    static char fieldBuf[kTitleFieldCount][96] = {};

    const double rev = djnum(tab.drawingDoc, "revision", 0.0);
    const nlohmann::json* tb = (sheet.contains("title_block") && sheet["title_block"].is_object())
                                   ? &sheet["title_block"] : nullptr;
    // Sheet controls mirror the engine's state; reloaded on open and whenever a finished RPC
    // changed the drawing (never mid-flight, so a pending change does not snap back).
    auto loadSheetState = [&]() {
        std::string sz = djstr(sheet, "size");
        portrait = sz.size() > 1 && sz.back() == 'P';
        if (portrait) sz.pop_back();
        sizeIdx = 1;
        for (int i = 0; i < IM_ARRAYSIZE(kSheetSizeKeys); ++i)
            if (sz == kSheetSizeKeys[i]) { sizeIdx = i; break; }
        projIdx = (tb && djstr(*tb, "projection") == "first") ? 1 : 0;
        const std::string sc = tb ? djstr(*tb, "scale") : std::string();
        scaleIdx = 0;
        for (int i = 1; i < IM_ARRAYSIZE(kScaleLabels); ++i)
            if (sc == kScaleLabels[i]) { scaleIdx = i; break; }
        const nlohmann::json& tree = tab.drawingDoc.value("tree", nlohmann::json::object());
        unitsIdx = tree.contains("units") && tree["units"].is_object() &&
                           djstr(tree["units"], "system") == "IPS" ? 1 : 0;
        loadedRev = rev;
    };

    if (tab.titleBlockOpen) {
        ImGui::OpenPopup("Sheet Properties##sheetprops");
        tab.titleBlockOpen = false;
        loaded = false;
    }
    if (!loaded) {
        loadSheetState();
        for (int i = 0; i < kTitleFieldCount; ++i) {
            const TitleField& f = kTitleFields[i];
            std::string v;
            if (tb) {
                for (const char* k : {f.key, f.legacy}) {
                    if (!k || !v.empty()) continue;
                    auto it = tb->find(k);
                    if (it == tb->end()) continue;
                    if (it->is_string()) v = it->get<std::string>();
                    else if (it->is_number()) {
                        char b[32];
                        std::snprintf(b, sizeof(b), "%g", it->get<double>());
                        v = b;
                    }
                }
            }
            if (v.empty() && std::strcmp(f.key, "date") == 0) {   // default: today (ISO 8601)
                const std::time_t now = std::time(nullptr);
                char b[16] = {0};
                if (const std::tm* lt = std::localtime(&now)) std::strftime(b, sizeof(b), "%Y-%m-%d", lt);
                v = b;
            }
            std::snprintf(fieldBuf[i], sizeof(fieldBuf[i]), "%s", v.c_str());
        }
        loaded = true;
    } else if (rev != loadedRev && st.rebuildsInFlight == 0) {
        loadSheetState();
    }

    ImGui::SetNextWindowSize(ImVec2(ui::metrics::px(440.0f), 0), ImGuiCond_Appearing);
    ImGui::SetNextWindowBgAlpha(0.0f);
    if (!ImGui::BeginPopup("Sheet Properties##sheetprops")) {
        loaded = false;
        return;
    }
    GlassPopupScope glass = beginGlassPopup("drw_sheetprops_popup");
    const std::string sheetId = djstr(sheet, "id");
    const int tabIdx = st.activeTab;
    // Every re-layout below auto-fits the scale unless the user picked one; an empty projection
    // keeps the title block's.

    ui::kit::sectionHeader("Sheet", nullptr, nullptr, -1, /*first=*/true);
    {
        ui::kit::FieldGroup g("drw_sheet", "Projection");
        bool sizeChanged = false;
        if (ui::kit::fieldEnum("size", "Paper", &sizeIdx, kSheetSizeLabels,
                               IM_ARRAYSIZE(kSheetSizeLabels),
                               ui::kit::FieldOpts().withTip(
                                   "ISO 5457 / ASME Y14.1 sheet. Views re-arrange to fit.")))
            sizeChanged = true;
        if (ui::kit::fieldCheckbox("portrait", "Portrait orientation", &portrait,
                                   ui::kit::FieldOpts().withTip(
                                       "Portrait paper (A4 portrait suits a single small part).")))
            sizeChanged = true;
        if (sizeChanged)
            applySheetSize(st, tabIdx, sheetId,
                           std::string(kSheetSizeKeys[sizeIdx]) + (portrait ? "P" : ""), "", 0.0);
        if (ui::kit::fieldEnum("proj", "Projection", &projIdx, kProjLabels, 2,
                               ui::kit::FieldOpts().withItemTips(kProjTips)))
            arrangeSheet(st, tabIdx, sheetId, projIdx == 1 ? "first" : "third", 0.0);
        if (ui::kit::fieldEnum("scale", "Scale", &scaleIdx, kScaleLabels,
                               IM_ARRAYSIZE(kScaleLabels),
                               ui::kit::FieldOpts().withTip(
                                   "ISO 5455 standard scale for every primary view. Auto picks the "
                                   "largest scale that fits the drawing area.")))
            arrangeSheet(st, tabIdx, sheetId, "", kScaleValues[scaleIdx]);
        if (ui::kit::fieldEnum("units", "Units", &unitsIdx, kUnitLabels, 2,
                               ui::kit::FieldOpts().withTip(
                                   "Drawing unit system: dimension values and the frame's unit note.")))
            drawCallAsyncOn(st, tabIdx, "drawing_set_units",
                            {{"doc_id", tab.drawingDocId}, {"system", unitsIdx == 1 ? "IPS" : "MMGS"}},
                            "Drawing units");
    }
    ui::kit::gapRow();
    if (ui::kit::secondaryButton("Auto-arrange views##drw_arrange", true, nullptr,
                                 ImVec2(ImGui::GetContentRegionAvail().x, 0.0f)))
        arrangeSheet(st, tabIdx, sheetId, "", 0.0);
    ui::kit::itemTooltip("Re-run the professional layout: standard views on the projection grid, "
                         "aligned, centred in the free drawing area.");

    auto fieldsRange = [&](int from, int to) {
        for (int i = from; i < to; ++i) {
            const TitleField& f = kTitleFields[i];
            ui::kit::fieldText(f.key, f.label, fieldBuf[i], sizeof(fieldBuf[i]),
                               ui::kit::FieldOpts().withHint(f.hint));
        }
    };
    ui::kit::sectionHeader("Title block");
    { ui::kit::FieldGroup g("drw_tb_id", "General tolerance"); fieldsRange(0, 4); }
    ui::kit::sectionHeader("Manufacturing");
    { ui::kit::FieldGroup g("drw_tb_mfg", "General tolerance"); fieldsRange(4, 8); }
    ui::kit::sectionHeader("Approvals");
    { ui::kit::FieldGroup g("drw_tb_appr", "General tolerance"); fieldsRange(8, kTitleFieldCount); }

    ui::kit::gapSection();
    if (st.rebuildsInFlight > 0) {
        ui::kit::spinner("Updating sheet\xE2\x80\xA6");
        ui::kit::gapRow();
    }
    const float half = (ImGui::GetContentRegionAvail().x - ImGui::GetStyle().ItemSpacing.x) * 0.5f;
    if (ui::kit::primaryButton("Apply title block##drw_tb_apply", true, nullptr, ImVec2(half, 0.0f))) {
        nlohmann::json fields = nlohmann::json::object();
        for (int i = 0; i < kTitleFieldCount; ++i) fields[kTitleFields[i].key] = std::string(fieldBuf[i]);
        drawCallAsyncOn(st, tabIdx, "drawing_set_titleblock",
                        {{"doc_id", tab.drawingDocId}, {"sheet_id", sheetId}, {"fields", fields}},
                        "Title block");
        ImGui::CloseCurrentPopup();
    }
    ImGui::SameLine();
    if (ui::kit::secondaryButton("Close##drw_tb_close", true, nullptr, ImVec2(half, 0.0f)))
        ImGui::CloseCurrentPopup();

    ui::kit::sectionHeader("Export");
    const char* exportFmt = nullptr;
    const float quarter = (ImGui::GetContentRegionAvail().x - ImGui::GetStyle().ItemSpacing.x * 3.0f) / 4.0f;
    if (ui::kit::secondaryButton("PDF##drw_exp_pdf", true, nullptr, ImVec2(quarter, 0.0f))) exportFmt = "pdf";
    ui::kit::itemTooltip("Vector PDF at true paper size");
    ImGui::SameLine();
    if (ui::kit::secondaryButton("SVG##drw_exp_svg", true, nullptr, ImVec2(quarter, 0.0f))) exportFmt = "svg";
    ui::kit::itemTooltip("Scalable vector SVG at true paper size");
    ImGui::SameLine();
    if (ui::kit::secondaryButton("DXF##drw_exp_dxf", true, nullptr, ImVec2(quarter, 0.0f))) exportFmt = "dxf";
    ui::kit::itemTooltip("AutoCAD R12 DXF, millimetre units");
    ImGui::SameLine();
    if (ui::kit::secondaryButton("DWG##drw_exp_dwg", true, nullptr, ImVec2(quarter, 0.0f))) exportFmt = "dwg";
    ui::kit::itemTooltip("AutoCAD DWG (R2000), layers, linetypes and text kept");
    if (exportFmt) ImGui::CloseCurrentPopup();

    glass.finish();
    ImGui::EndPopup();
    if (exportFmt) exportActiveDrawing(st, exportFmt);
}

// ---- canvas chrome --------------------------------------------------------------------------------

// The part of the canvas no pinned glass column covers: FeatureManager (left), Copilot (right)
// and the doc-tab band (top). The sheet is fitted into and clipped to this rect so no paper ink
// shows through the translucent columns, and every HUD element stays inside it.
static ImRect drawingClearRect(const ImVec2& mn, const ImVec2& mx) {
    ImRect r(mn, mx);
    const float gap = ui::metrics::U(2.0f);
    // A column rolled up to its header strip (published height under half the canvas) leaves the
    // sheet the full width; only a full-height column occludes a side.
    auto occludes = [&](int slot) {
        const float h = ui::overlayColumnHeight(slot);
        return !(h > 0.0f && h < (mx.y - mn.y) * 0.5f);
    };
    const float left = ui::overlayColumnEdge(0);
    if (left > r.Min.x && left < r.Max.x && occludes(0)) r.Min.x = left + gap;
    const float right = ui::overlayColumnEdge(1);
    if (right > r.Min.x && right < r.Max.x && occludes(1)) r.Max.x = right - gap;
    const float top = ui::docTabBandBottom();
    if (top > r.Min.y && top < r.Max.y) r.Min.y = top + gap;
    // Columns wider than the window (tiny window): fall back to the full canvas on that axis.
    if (r.GetWidth() < ui::metrics::U(24.0f)) { r.Min.x = mn.x; r.Max.x = mx.x; }
    if (r.GetHeight() < ui::metrics::U(24.0f)) { r.Min.y = mn.y; r.Max.y = mx.y; }
    return r;
}

// Canvas backdrop behind the paper: a soft theme gradient so the white sheet reads as paper.
static void paintCanvasBackdrop(ImDrawList* dl, const ImVec2& mn, const ImVec2& mx) {
    const ImU32 top = ui::Palette::toU32(ui::palette.panelBg);
    const ImU32 bot = ui::Palette::toU32(ui::palette.windowBg);
    dl->AddRectFilledMultiColor(mn, mx, top, top, bot, bot);
}

// A kit empty state centred in `r` at a readable measure (vertically centred on last frame's
// measured height). `extra` draws optional rows beneath the action. Returns true on the action.
static bool sheetPlaceholder(const ImRect& r, const char* id, const char* title, const char* body,
                             const char* action, bool actionEnabled,
                             const std::function<void()>& extra = {}) {
    bool clicked = false;
    ImGui::PushID(id);
    ImGuiStorage* store = ImGui::GetStateStorage();
    const ImGuiID hKey = ImGui::GetID("##placeholder_h");
    const float blockH = store->GetFloat(hKey, 0.0f);
    const float w = std::min(ui::metrics::px(440.0f), r.GetWidth());
    ImGui::SetCursorScreenPos(ImVec2(r.Min.x + (r.GetWidth() - w) * 0.5f,
                                     r.Min.y + std::max(0.0f, (r.GetHeight() - blockH) * 0.5f)));
    if (ImGui::BeginChild("##placeholder", ImVec2(w, 0.0f),
                          ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysAutoResize,
                          ImGuiWindowFlags_NoBackground | ImGuiWindowFlags_NoScrollbar |
                              ImGuiWindowFlags_NoScrollWithMouse)) {
        clicked = ui::kit::emptyState("ws_drawing", title, body, action, actionEnabled);
        if (extra) extra();
        store->SetFloat(hKey, ImGui::GetCursorPosY());
    }
    ImGui::EndChild();
    ImGui::PopID();
    return clicked;
}

// One frosted HUD row whose top-left is `origin`. `body` lays its widgets out left to right,
// advancing `x` past each (hudNext); the glass is painted behind their union afterwards.
static float hudChipH() { return ImGui::GetFontSize() + ui::metrics::U(1.0f); }  // kit chip height
static void hudNext(float& x) { x = ImGui::GetItemRectMax().x + ui::metrics::U(1.0f); }
static ImRect hudRow(const ImVec2& origin,
                     const std::function<void(float& x, float y, float rowH)>& body) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float pad = ui::metrics::U(1.5f);
    const float rowH = ImGui::GetFrameHeight();
    dl->ChannelsSplit(2);
    dl->ChannelsSetCurrent(1);
    float x = origin.x + pad;
    const float y = origin.y + pad;
    body(x, y, rowH);
    dl->ChannelsSetCurrent(0);
    const float right = std::max(origin.x + pad, x - ui::metrics::U(1.0f)) + pad;
    const ImRect r(origin, ImVec2(right, y + rowH + pad));
    ui::glassBackground(dl, r.Min, r.Max);
    dl->ChannelsMerge();
    return r;
}
static float hudRowHeight() { return ImGui::GetFrameHeight() + ui::metrics::U(3.0f); }

// "1:2" / "2:1" from a numeric view scale.
static std::string fmtScaleRatio(double sc) {
    char b[32];
    if (!(sc > 0.0)) return "1:1";
    if (sc >= 1.0) std::snprintf(b, sizeof(b), "%g:1", std::round(sc * 100.0) / 100.0);
    else std::snprintf(b, sizeof(b), "1:%g", std::round(100.0 / sc) / 100.0);
    return b;
}

// UI caption for a view (hover/selection chip only — never sheet ink).
static std::string viewCaption(const nlohmann::json& v) {
    const std::string type = djstr(v, "type"), label = djstr(v, "label");
    std::string cap;
    if (type == "section" && !label.empty()) cap = "Section " + label + "-" + label;
    else if (type == "detail" && !label.empty()) cap = "Detail " + label;
    else {
        cap = djstr(v, "orientation");
        if (cap.empty()) cap = "view";
        if (cap[0] >= 'a' && cap[0] <= 'z') cap[0] = (char)(cap[0] - 'a' + 'A');
        cap += " view";
    }
    return cap + "  \xC2\xB7  " + fmtScaleRatio(djnum(v, "scale", 1.0));
}

// Sheet summary for the HUD: "A3 · 1:2 · Third angle" (title-block scale/projection).
static std::string sheetSummary(const nlohmann::json& sheet) {
    std::string size = djstr(sheet, "size");
    bool portrait = false;
    if (size.size() > 1 && size.back() == 'P') { size.pop_back(); portrait = true; }
    if (size.empty()) size = "Sheet";
    if (portrait) size += " portrait";
    std::string scale, proj = "third";
    if (sheet.contains("title_block") && sheet["title_block"].is_object()) {
        scale = djstr(sheet["title_block"], "scale");
        const std::string p = djstr(sheet["title_block"], "projection");
        if (!p.empty()) proj = p;
    }
    std::string out = size;
    if (!scale.empty()) out += "  \xC2\xB7  " + scale;
    out += proj == "first" ? "  \xC2\xB7  First angle" : "  \xC2\xB7  Third angle";
    return out;
}

void drawDrawingEmptyState(AppState& st) {  // gui-lint: allow (forwards to kit::emptyState via sheetPlaceholder)
    const ImVec2 mn = ImGui::GetCursorScreenPos();
    ImVec2 avail = ImGui::GetContentRegionAvail();
    avail.x = std::max(avail.x, ui::metrics::U(8.0f));
    avail.y = std::max(avail.y, ui::metrics::U(8.0f));
    const ImVec2 mx(mn.x + avail.x, mn.y + avail.y);
    paintCanvasBackdrop(ImGui::GetWindowDrawList(), mn, mx);
    const ImRect clear = drawingClearRect(mn, mx);
    const bool canMake = st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
                         st.docTabs[st.activeTab].kind != AppState::DocTab::TabKind::Drawing &&
                         st.docMirror.hasDoc();
    std::vector<int> drawings;
    for (int i = 0; i < (int)st.docTabs.size(); ++i)
        if (st.docTabs[i].kind == AppState::DocTab::TabKind::Drawing) drawings.push_back(i);
    int openIdx = -1;
    const std::string body =
        canMake ? "Make Drawing projects " + st.docTabs[st.activeTab].title +
                      " onto an A3 sheet: third-angle views, auto-dimensions, hole callouts "
                      "and a complete title block."
                : std::string("Open or create a part in the Model workspace, then make its "
                              "drawing here.");
    const bool make = sheetPlaceholder(
        clear, "drw_empty", "No drawing open", body.c_str(), "Make Drawing", canMake, [&]() {
            if (drawings.empty()) return;
            ui::kit::sectionHeader("Open drawings", nullptr, nullptr, (int)drawings.size());
            for (int i : drawings) {
                ImGui::PushID(i);
                ui::kit::RowStyle rs;
                rs.icon = "ws_drawing";
                rs.secondary = "Switch to this drawing";
                if (ui::kit::row("drw_open", st.docTabs[i].displayTitle().c_str(), rs)) openIdx = i;
                ImGui::PopID();
            }
        });
    if (make) st.makeDrawing();
    else if (openIdx >= 0) st.switchToTab(openIdx);
}

void drawDrawingSheet(AppState& st) {
    const auto& pal = ui::palette;
    const float s = ui::uiScale;

    if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) return;
    AppState::DocTab& tab = st.docTabs[st.activeTab];

    // ---- canvas: the whole window body is one hit item; the sheet lives in the clear rect ----
    const ImVec2 canvasMin = ImGui::GetCursorScreenPos();
    ImVec2 avail = ImGui::GetContentRegionAvail();
    avail.x = std::max(avail.x, ui::metrics::U(8.0f));
    avail.y = std::max(avail.y, ui::metrics::U(8.0f));
    ImGui::SetNextItemAllowOverlap();   // HUD widgets drawn over the canvas take hover + clicks
    ImGui::InvisibleButton("##drawcanvas", avail,
                           ImGuiButtonFlags_MouseButtonLeft | ImGuiButtonFlags_MouseButtonMiddle);
    const bool canvasHovered = ImGui::IsItemHovered();
    const bool canvasActive = ImGui::IsItemActive();
    const ImVec2 canvasMax(canvasMin.x + avail.x, canvasMin.y + avail.y);
    const ImRect clear = drawingClearRect(canvasMin, canvasMax);

    ImDrawList* dl = ImGui::GetWindowDrawList();
    paintCanvasBackdrop(dl, canvasMin, canvasMax);

    // The active sheet: first sheet of the cached drawing_get.
    const nlohmann::json* sheet = nullptr;
    if (tab.drawingDoc.is_object() && tab.drawingDoc.contains("sheets") &&
        tab.drawingDoc["sheets"].is_array() && !tab.drawingDoc["sheets"].empty() &&
        tab.drawingDoc["sheets"][0].is_object())
        sheet = &tab.drawingDoc["sheets"][0];
    if (!sheet) {
        if (sheetPlaceholder(clear, "drw_nosheet", "This drawing has no sheet",
                             "The engine returned no sheet for this drawing. Rebuild re-fetches it "
                             "from the source part.",
                             "Rebuild", st.workerStarted))
            st.refreshDrawingTab(st.activeTab);
        return;
    }

    double paperW = 0, paperH = 0;
    if (sheet->contains("size_mm") && (*sheet)["size_mm"].is_array() &&
        (*sheet)["size_mm"].size() == 2) {
        paperW = (*sheet)["size_mm"][0].is_number() ? (*sheet)["size_mm"][0].get<double>() : 0;
        paperH = (*sheet)["size_mm"][1].is_number() ? (*sheet)["size_mm"][1].get<double>() : 0;
    }
    if (paperW <= 0 || paperH <= 0) { paperW = 420.0; paperH = 297.0; }  // A3 fallback

    // ---- one-shot fit: frame the sheet in the clear rect, above the bottom HUD row ----
    if (tab.drawNeedFit || tab.drawZoom <= 0.0f) {
        float bx0, by0, bx1, by1;
        if (!sheetBounds(*sheet, bx0, by0, bx1, by1)) {
            bx0 = 0; by0 = 0; bx1 = (float)paperW; by1 = (float)paperH;
        }
        const ImRect fitR(clear.Min, ImVec2(clear.Max.x, clear.Max.y - hudRowHeight()));
        const float bw = std::max(1.0f, bx1 - bx0), bh = std::max(1.0f, by1 - by0);
        const float margin = 0.92f;  // leave a little air around the paper
        tab.drawZoom = std::min(fitR.GetWidth() * margin / bw,
                                std::max(1.0f, fitR.GetHeight()) * margin / bh);
        if (tab.drawZoom <= 0.0f || !std::isfinite(tab.drawZoom)) tab.drawZoom = 1.0f;
        // screen = canvasMin + pan + (x, -y) * zoom   (engine +Y up, screen +Y down)
        const ImVec2 c = fitR.GetCenter();
        const float cxMm = (bx0 + bx1) * 0.5f, cyMm = (by0 + by1) * 0.5f;
        tab.drawPanX = (c.x - canvasMin.x) - cxMm * tab.drawZoom;
        tab.drawPanY = (c.y - canvasMin.y) + cyMm * tab.drawZoom;
        tab.drawNeedFit = false;
    }

    const ImVec2 mouse = ImGui::GetIO().MousePos;
    const bool overSheet = canvasHovered && clear.Contains(mouse);

    // ---- wheel zoom to cursor ----
    if (overSheet) {
        const float wheel = ImGui::GetIO().MouseWheel;
        if (wheel != 0.0f) {
            const float old = tab.drawZoom;
            const float nz = std::clamp(old * std::pow(1.15f, wheel), 0.02f, 200.0f);
            const float lx = mouse.x - canvasMin.x, ly = mouse.y - canvasMin.y;
            const float mmx = (lx - tab.drawPanX) / old;
            const float mmy = -(ly - tab.drawPanY) / old;
            tab.drawZoom = nz;
            tab.drawPanX = lx - mmx * nz;
            tab.drawPanY = ly + mmy * nz;
        }
    }

    // ---- pan: MMB drag, or Space + LMB drag ----
    const bool spaceDown = ImGui::IsKeyDown(ImGuiKey_Space) && !ImGui::GetIO().WantTextInput;
    const bool panning = ImGui::IsMouseDragging(ImGuiMouseButton_Middle) ||
                         (spaceDown && ImGui::IsMouseDragging(ImGuiMouseButton_Left));
    if (panning && (overSheet || canvasActive)) {
        const ImVec2 d = ImGui::GetIO().MouseDelta;
        tab.drawPanX += d.x;
        tab.drawPanY += d.y;
    }

    const float z = tab.drawZoom;
    const ImVec2 origin(canvasMin.x + tab.drawPanX, canvasMin.y + tab.drawPanY);
    // Forward map: sheet-mm (x,y) -> screen px. Y flipped (engine +Y up, screen +Y down).
    auto toScreen = [&](double mx, double my) -> ImVec2 {
        return ImVec2(origin.x + (float)mx * z, origin.y - (float)my * z);
    };
    const double curMx = (mouse.x - origin.x) / z;
    const double curMy = -(mouse.y - origin.y) / z;
    const double snapRadiusMm = ui::metrics::px(9.0f) / std::max(z, 1e-4f);
    const double pickRadiusMm = ui::metrics::px(14.0f) / std::max(z, 1e-4f);
    const bool hasViews = sheet->contains("views") && (*sheet)["views"].is_array();

    // ---- hover (no tool armed): the annotation under the cursor, else the view ----
    std::string hovAnnView, hovAnnId, hovViewId;
    if (!anyDrawToolActive(tab) && overSheet && !tab.viewDragActive && !s_annDragActive &&
        !panning) {
        double hx = 0, hy = 0;
        pickAnnotation(*sheet, curMx, curMy, pickRadiusMm, hovAnnView, hovAnnId, hx, hy);
        if (hovAnnId.empty()) hovViewId = viewAtPoint(*sheet, curMx, curMy, 2.0);
    }

    dl->PushClipRect(clear.Min, clear.Max, true);

    // ---- paper: soft drop shadow + paper fill (the frame block draws every sheet line) ----
    const ImVec2 paper0 = toScreen(0, paperH);   // top-left (maxY -> top)
    const ImVec2 paper1 = toScreen(paperW, 0);   // bottom-right
    for (int i = 4; i >= 1; --i) {
        const float grow = ui::metrics::px(1.5f * (float)i);
        const float drop = ui::metrics::px(2.0f);
        dl->AddRectFilled(ImVec2(paper0.x - grow, paper0.y - grow + drop),
                          ImVec2(paper1.x + grow, paper1.y + grow + drop),
                          sheetCol(pal.glassShadow, 0.22f), grow);
    }
    dl->AddRectFilled(paper0, paper1, sheetCol(pal.viewportTop));
    dl->AddRect(paper0, paper1, sheetCol(pal.sketchInk, 0.14f), 0.0f, 0, ui::metrics::stroke(1.0f));

    // One sheet ink for every line class; selection/hover stays the theme accent.
    const ImU32 ink = sheetCol(pal.sketchInk);
    const ImU32 accent = ui::Palette::toU32(pal.accent);
    SheetPen pen(dl, origin, z);
    if (sheet->contains("frame")) pen.block((*sheet)["frame"], ink);

    // ---- views: hatch (thin), hidden (thin dashed), visible (thick), core decorations ----
    if (hasViews) {
        for (const auto& v : (*sheet)["views"]) {
            if (!v.is_object() || djstr(v, "type") == "table_overlay") continue;
            const std::string err = djstr(v, "error");
            bool visEmpty = true;
            if (v.contains("visible") && v["visible"].is_array())
                for (const auto& pl : v["visible"])
                    if (pl.is_array() && pl.size() >= 2) { visEmpty = false; break; }
            if (!err.empty() || visEmpty) {
                // A failed / empty view: a small note at its bbox / position instead of geometry.
                ImVec2 anchor((clear.Min.x + clear.Max.x) * 0.5f, (clear.Min.y + clear.Max.y) * 0.5f);
                double bx0, by0, bx1, by1;
                if (viewBox(v, bx0, by0, bx1, by1))
                    anchor = toScreen((bx0 + bx1) * 0.5, (by0 + by1) * 0.5);
                else if (v.contains("position") && v["position"].is_array() &&
                         v["position"].size() == 2 && v["position"][0].is_number() &&
                         v["position"][1].is_number())
                    anchor = toScreen(v["position"][0].get<double>(), v["position"][1].get<double>());
                const std::string orient = djstr(v, "orientation");
                const std::string note = (orient.empty() ? std::string("view") : orient) +
                                         (err.empty() ? ": (empty)" : ": view failed");
                const ImVec2 ts = ImGui::CalcTextSize(note.c_str());
                const float px = ui::metrics::U(1.5f), py = ui::metrics::U(0.75f);
                const ImVec2 t0(anchor.x - ts.x * 0.5f - px, anchor.y - ts.y * 0.5f - py);
                const ImVec2 t1(t0.x + ts.x + px * 2.0f, t0.y + ts.y + py * 2.0f);
                dl->AddRectFilled(t0, t1, sheetCol(pal.viewportTop, 0.92f), ui::metrics::rounding(3.0f));
                dl->AddRect(t0, t1, sheetCol(pal.statusErr, 0.85f), ui::metrics::rounding(3.0f), 0,
                            ui::metrics::stroke(1.0f));
                dl->AddText(ImVec2(t0.x + px, t0.y + py), sheetCol(pal.statusErr), note.c_str());
                continue;
            }
            if (v.contains("hatch")) pen.solid(v["hatch"], ink, pen.thin);
            if (v.contains("hidden")) pen.patterned(v["hidden"], ink, pen.thin, kHiddenPat, 2);
            if (v.contains("visible")) pen.solid(v["visible"], ink, pen.thick);
            if (v.contains("draw")) pen.block(v["draw"], ink);
        }
    }

    // ---- annotations (dims, callouts, GD&T, notes, centre marks, tables): compiled draw blocks ----
    if (hasViews) {
        for (const auto& v : (*sheet)["views"]) {
            if (!v.is_object() || !v.contains("annotations") || !v["annotations"].is_array())
                continue;
            const std::string vid = viewIdOf(v);
            for (const auto& ann : v["annotations"]) {
                if (!ann.is_object() || !ann.contains("draw")) continue;
                const std::string aid = djstr(ann, "id");
                const bool sel = tab.selAnnViewId == vid && tab.selAnnId == aid && !aid.empty();
                const bool hov = hovAnnView == vid && hovAnnId == aid && !aid.empty();
                pen.block(ann["draw"], sel ? accent : hov ? sheetCol(pal.accent, 0.75f) : ink);
            }
        }
    }

    // ---- view hover / selection: accent frame + caption chip above the view (UI, not ink) ----
    if (hasViews && !tab.viewDragActive) {
        for (const auto& v : (*sheet)["views"]) {
            if (!v.is_object() || djstr(v, "type") == "table_overlay") continue;
            const std::string vid = viewIdOf(v);
            const bool sel = !tab.selViewId.empty() && tab.selViewId == vid && tab.selAnnId.empty();
            const bool hov = !hovViewId.empty() && hovViewId == vid;
            double bx0, by0, bx1, by1;
            if ((!sel && !hov) || !viewBox(v, bx0, by0, bx1, by1)) continue;
            const float g = ui::metrics::U(0.75f);
            const ImVec2 h0 = toScreen(bx0, by1), h1 = toScreen(bx1, by0);
            dl->AddRect(ImVec2(h0.x - g, h0.y - g), ImVec2(h1.x + g, h1.y + g),
                        sheetCol(pal.accent, sel ? 0.90f : 0.45f), ui::metrics::rounding(2.0f), 0,
                        ui::metrics::stroke(sel ? 1.5f : 1.0f));
            const std::string cap = viewCaption(v);
            const float chipW = ImGui::CalcTextSize(cap.c_str()).x + ui::metrics::U(3.0f);
            const ImVec2 cp(h0.x - g, h0.y - g - hudChipH() - ui::metrics::U(1.0f));
            if (cp.x >= clear.Min.x && cp.y >= clear.Min.y && cp.x + chipW <= clear.Max.x) {
                ImGui::PushID(vid.c_str());
                ImGui::SetCursorScreenPos(cp);
                ui::kit::chip(cap.c_str(), ui::kit::Tone::Accent);
                ImGui::PopID();
            }
        }
    }

    // ---- tool interaction (snap + place) + annotation/view select, drag, delete ----
    {
        const bool typing = ImGui::GetIO().WantTextInput;
        // Esc cancels the in-progress action and disarms whatever tool is armed.
        if (!typing && ImGui::IsKeyPressed(ImGuiKey_Escape) && anyDrawToolActive(tab))
            disarmDrawTools(tab);
        const bool overCanvas = overSheet;

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
                addDashedPolyline(dl, chain, sheetCol(pal.statusErr, 0.86f), 1.4f * s, 10.0f * s,
                                  4.0f * s);
            }
            bool dbl = ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left);
            bool finish = dbl || (!typing && ImGui::IsKeyPressed(ImGuiKey_Enter));
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
                        st.toast(ui::ToastLevel::Warn, "Start the cutting line on a view");
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
                dl->AddCircle(c, (float)rmm * z, sheetCol(pal.statusErr, 0.86f), 48, 1.4f * s);
                dl->AddCircleFilled(c, 2.5f * s, sheetCol(pal.statusErr, 0.86f));
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
                dl->AddCircle(sp, 5.0f * s, sheetCol(pal.statusWarn), 12, 1.6f * s);
                dl->AddCircleFilled(sp, 2.0f * s, sheetCol(pal.statusWarn));
            }
            // Rubber-band preview of the in-progress dimension.
            if (tab.dimStage == 1) {
                ImVec2 a = toScreen(tab.dimAx, tab.dimAy);
                ImVec2 b = snapped ? toScreen(sx, sy) : mouse;
                dl->AddLine(a, b, sheetCol(pal.statusWarn, 0.63f), 1.2f * s);
            } else if (tab.dimStage == 2) {
                ImVec2 a = toScreen(tab.dimAx, tab.dimAy);
                ImVec2 b = toScreen(tab.dimBx, tab.dimBy);
                dl->AddLine(a, b, sheetCol(pal.statusWarn, 0.63f), 1.2f * s);
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
                else st.toast(ui::ToastLevel::Warn, "Click a view to auto-dimension");
            }
        }
        // ---- W17: Hole Callout / Center Mark — snap a circle, drop the callout ----
        else if ((tab.holeCalloutActive || tab.centerMarkActive) && overCanvas) {
            double sx, sy; std::string svid;
            bool snapped = snapToVertex(*sheet, curMx, curMy, snapRadiusMm * 2.0, sx, sy, svid);
            if (snapped) {
                ImVec2 sp = toScreen(sx, sy);
                dl->AddCircle(sp, 5.0f * s, sheetCol(pal.statusWarn), 12, 1.6f * s);
            }
            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !spaceDown) {
                std::string overView = !svid.empty() ? svid : viewAtPoint(*sheet, curMx, curMy, 8.0);
                if (overView.empty()) {
                    st.toast(ui::ToastLevel::Warn, "Click on a circle in a view");
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
                dl->AddCircle(sp, 5.0f * s, sheetCol(pal.statusWarn), 12, 1.6f * s);
            }
            if (tab.centerLineStage == 1) {
                ImVec2 a = toScreen(tab.centerLineAx, tab.centerLineAy);
                ImVec2 b = snapped ? toScreen(sx, sy) : mouse;
                addDashedPolyline(dl, {a, b}, sheetCol(pal.statusWarn, 0.63f), 1.2f * s, 9.0f * s,
                                  4.0f * s);
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
                if (overView.empty() && hasViews && !(*sheet)["views"].empty())
                    overView = viewIdOf((*sheet)["views"][0]);
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
                dl->AddCircle(sp, 5.0f * s, sheetCol(pal.simGlyph), 12, 1.6f * s);
            }
            if (tab.gdtStage == 1) {
                ImVec2 a = toScreen(tab.gdtAx, tab.gdtAy);
                dl->AddLine(a, mouse, sheetCol(pal.simGlyph, 0.71f), 1.2f * s);
            }
            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !spaceDown) {
                if (tab.gdtStage == 0) {
                    double ax = snapped ? sx : curMx, ay = snapped ? sy : curMy;
                    std::string vv = !svid.empty() ? svid : viewAtPoint(*sheet, curMx, curMy, 12.0);
                    if (vv.empty()) {
                        st.toast(ui::ToastLevel::Warn, "Click a feature on a view");
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
            // Double-click opens the dimension editor for the annotation under the cursor.
            if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left) && !spaceDown) {
                if (!hovAnnId.empty()) {
                    tab.selAnnViewId = hovAnnView; tab.selAnnId = hovAnnId;
                    s_annEditOpen = true; s_annEditLoaded = false;
                }
            } else if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !spaceDown && !panning) {
                std::string hv, ha; double hx = 0, hy = 0;
                pickAnnotation(*sheet, curMx, curMy, pickRadiusMm, hv, ha, hx, hy);
                tab.selAnnViewId = hv; tab.selAnnId = ha;   // empty clears the selection
                // No annotation under the cursor -> select the VIEW for move/scale/delete.
                if (ha.empty()) tab.selViewId = viewAtPoint(*sheet, curMx, curMy, 4.0);
            }
            // ---- view move: drag the selected view; ghost-rect preview, commit on release --
            if (!tab.selViewId.empty() && tab.selAnnId.empty() && !s_annDragActive && !spaceDown &&
                hasViews) {
                const nlohmann::json* sv = nullptr;
                for (const auto& v : (*sheet)["views"])
                    if (v.is_object() && viewIdOf(v) == tab.selViewId) { sv = &v; break; }
                double bx0, by0, bx1, by1;
                if (sv && viewBox(*sv, bx0, by0, bx1, by1)) {
                    const ImVec2 hl0 = toScreen(bx0, by1), hl1 = toScreen(bx1, by0);
                    if (ImGui::IsMouseDragging(ImGuiMouseButton_Left)) {
                        const ImVec2 dd = ImGui::GetMouseDragDelta(ImGuiMouseButton_Left);
                        if (!tab.viewDragActive) {
                            const ImVec2 press(mouse.x - dd.x, mouse.y - dd.y);
                            const float g = ui::metrics::U(1.0f);
                            if (press.x >= hl0.x - g && press.x <= hl1.x + g &&
                                press.y >= hl0.y - g && press.y <= hl1.y + g)
                                tab.viewDragActive = true;
                        }
                        if (tab.viewDragActive) {
                            // ghost frame at the dragged offset
                            dl->AddRect(ImVec2(hl0.x + dd.x, hl0.y + dd.y),
                                        ImVec2(hl1.x + dd.x, hl1.y + dd.y), accent,
                                        ui::metrics::rounding(2.0f), 0, ui::metrics::stroke(1.6f));
                            tab.viewDragDX = dd.x / z;       // sheet-mm offset
                            tab.viewDragDY = -dd.y / z;
                        }
                    }
                    if (tab.viewDragActive && ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
                        double px = 0, py = 0;
                        if (sv->contains("position") && (*sv)["position"].is_array() &&
                            (*sv)["position"].size() == 2 && (*sv)["position"][0].is_number() &&
                            (*sv)["position"][1].is_number()) {
                            px = (*sv)["position"][0].get<double>();
                            py = (*sv)["position"][1].get<double>();
                        }
                        commitViewMove(st, tab, tab.selViewId, px + tab.viewDragDX,
                                       py + tab.viewDragDY);
                        tab.viewDragActive = false; tab.viewDragDX = tab.viewDragDY = 0;
                    }
                }
            }
            // Drag the selected annotation's text to reposition it (commit on release).
            if (!tab.selAnnId.empty() && !spaceDown) {
                const nlohmann::json* selAnn = findSelAnn(*sheet, tab.selAnnViewId, tab.selAnnId);
                double selx = 0, sely = 0;
                const bool haveSel = selAnn && annAnchor(*selAnn, selx, sely);
                if (haveSel && ImGui::IsMouseDragging(ImGuiMouseButton_Left)) {
                    const ImVec2 selS = toScreen(selx, sely);
                    if (!s_annDragActive) {
                        // arm only if the drag began near the selected annotation
                        const ImVec2 dd = ImGui::GetMouseDragDelta(ImGuiMouseButton_Left);
                        const ImVec2 press(mouse.x - dd.x, mouse.y - dd.y);
                        const float pr = ui::metrics::px(16.0f);
                        if (std::fabs(press.x - selS.x) < pr && std::fabs(press.y - selS.y) < pr)
                            s_annDragActive = true;
                    }
                    if (s_annDragActive) {
                        dl->AddLine(selS, mouse, sheetCol(pal.accent, 0.78f), ui::metrics::stroke(1.2f));
                        dl->AddCircleFilled(mouse, ui::metrics::px(3.0f), sheetCol(pal.accent, 0.86f));
                    }
                }
                if (s_annDragActive && ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
                    applyAnnPatch(st, tab, tab.selAnnViewId, tab.selAnnId,
                                  {{"pos", nlohmann::json::array({curMx, curMy})}});
                    s_annDragActive = false;
                }
            }
        }

        // Delete the selected annotation, else the selected view (never while typing in a field).
        if (!typing && !anyDrawToolActive(tab) &&
            (ImGui::IsKeyPressed(ImGuiKey_Delete) || ImGui::IsKeyPressed(ImGuiKey_Backspace))) {
            if (!tab.selAnnId.empty())
                deleteAnnotation(st, tab, tab.selAnnViewId, tab.selAnnId);
            else if (!tab.selViewId.empty())
                removeView(st, tab, tab.selViewId);
        }
    }

    // ---- W13: BOM table overlay (top-right of the clear rect) ----
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
        ImVec2 t0(clear.Max.x - tableW - 10 * s, clear.Min.y + 10 * s);
        ImVec2 t1(t0.x + tableW, t0.y + tableH);
        dl->AddRectFilled(t0, t1, sheetCol(pal.viewportTop, 0.94f), 3.0f);
        dl->AddRect(t0, t1, sheetCol(pal.sketchDimLine), 3.0f, 0, 1.2f * s);
        float yy = t0.y + pad;
        dl->AddText(ImVec2(t0.x + pad, yy), ink, "Bill of Materials");
        yy += rowH;
        if (!isAsm || nRows == 0) {
            const char* msg = bom.is_object() && bom.contains("note") && bom["note"].is_string()
                                  ? bom["note"].get_ptr<const std::string*>()->c_str()
                                  : (isAsm ? "no components" : "part drawing - no BOM");
            dl->AddText(ImVec2(t0.x + pad, yy), sheetCol(pal.sketchInk, 0.55f), msg);
        } else {
            // Header row.
            dl->AddText(ImVec2(t0.x + pad, yy), ink, "#");
            dl->AddText(ImVec2(t0.x + pad + wItem, yy), ink, "Part");
            dl->AddText(ImVec2(t0.x + pad + wItem + wName, yy), ink, "Qty");
            dl->AddLine(ImVec2(t0.x + pad, yy + rowH - 2), ImVec2(t1.x - pad, yy + rowH - 2),
                        sheetCol(pal.sketchInk, 0.35f), 1.0f * s);
            yy += rowH;
            for (const auto& r : *rows) {
                if (!r.is_object()) continue;
                char itemBuf[16];
                std::snprintf(itemBuf, sizeof(itemBuf), "%d", (int)djnum(r, "item", 0));
                const std::string name = ui::kit::elide(djstr(r, "name").c_str(), wName - 4 * s);
                char qtyBuf[16];
                std::snprintf(qtyBuf, sizeof(qtyBuf), "%d", (int)djnum(r, "quantity", 0));
                dl->AddText(ImVec2(t0.x + pad, yy), ink, itemBuf);
                dl->AddText(ImVec2(t0.x + pad + wItem, yy), ink, name.c_str());
                dl->AddText(ImVec2(t0.x + pad + wItem + wName, yy), ink, qtyBuf);
                yy += rowH;
            }
        }
    }

    dl->PopClipRect();

    // ---- HUD (inside the clear rect, never under the pinned columns) ----
    ImGui::PushClipRect(clear.Min, clear.Max, true);
    {
        const float m = ui::metrics::U(2.0f);
        const float chipH = hudChipH();
        // Top: the armed tool's current step + Done, or a stale-source warning.
        const char* stageMsg = nullptr;
        if (tab.dimToolActive)
            stageMsg = tab.dimStage == 0 ? "Smart Dimension: click vertex A"
                     : tab.dimStage == 1 ? "Smart Dimension: click vertex B"
                                         : "Smart Dimension: click to place text";
        else if (tab.sectionToolActive)
            stageMsg = tab.sectionStage == 0 ? "Section: click the cutting-line start across a view"
                                             : "Section: click more points, double-click to finish";
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
        else if (tab.gdtToolActive)
            stageMsg = tab.gdtStage == 0 ? "GD&T: click the feature" : "GD&T: click to place";
        const bool stale = tab.drawingDoc.value("views_stale", false);
        if (stageMsg || stale) {
            hudRow(ImVec2(clear.Min.x + m, clear.Min.y + m), [&](float& x, float y, float rowH) {
                if (stale) {
                    const std::string w = djstr(tab.drawingDoc, "warning");
                    ImGui::SetCursorScreenPos(ImVec2(x, y + (rowH - chipH) * 0.5f));
                    ui::kit::chip(w.empty() ? "Source unavailable \xE2\x80\x94 views are stale"
                                            : w.c_str(),
                                  ui::kit::Tone::Warn, true);
                    hudNext(x);
                }
                if (stageMsg) {
                    ImGui::SetCursorScreenPos(ImVec2(x, y + (rowH - chipH) * 0.5f));
                    ui::kit::chip(stageMsg, ui::kit::Tone::Accent, true);
                    hudNext(x);
                    ImGui::SetCursorScreenPos(ImVec2(x, y));
                    if (ui::kit::secondaryButton("Done##drw_tool_done")) disarmDrawTools(tab);
                    ui::kit::itemTooltip("Finish the tool (Esc)");
                    hudNext(x);
                }
            });
        }

        // Bottom-left: sheet · scale · projection, view count, zoom, and the canvas hint.
        int nViews = 0;
        if (hasViews)
            for (const auto& v : (*sheet)["views"])
                if (v.is_object() && djstr(v, "type") != "table_overlay") ++nViews;
        const std::string summary = sheetSummary(*sheet);
        char viewsBuf[32], zoomBuf[32];
        std::snprintf(viewsBuf, sizeof(viewsBuf), "%d view%s", nViews, nViews == 1 ? "" : "s");
        std::snprintf(zoomBuf, sizeof(zoomBuf), "%.0f%%", z / std::max(1e-4f, ui::metrics::px(3.78f)) * 100.0f);
        const ImVec2 hudOrigin(clear.Min.x + m, clear.Max.y - m - hudRowHeight());
        hudRow(hudOrigin, [&](float& x, float y, float rowH) {
            ImGui::SetCursorScreenPos(ImVec2(x, y + (rowH - chipH) * 0.5f));
            ui::kit::chip(summary.c_str(), ui::kit::Tone::Neutral, false,
                          "Paper size, drawing scale and projection method (Sheet Props to change)");
            hudNext(x);
            ImGui::SetCursorScreenPos(ImVec2(x, y + (rowH - chipH) * 0.5f));
            ui::kit::chip(viewsBuf);
            hudNext(x);
            ImGui::SetCursorScreenPos(ImVec2(x, y + (rowH - chipH) * 0.5f));
            ui::kit::chip(zoomBuf, ui::kit::Tone::Neutral, false,
                          "Zoom relative to true paper size at 96 dpi");
            hudNext(x);
            const char* hint = "Double-click a dimension to edit  \xC2\xB7  drag to move  \xC2\xB7  "
                               "wheel zoom  \xC2\xB7  middle-drag pan  \xC2\xB7  Del removes";
            const float hintW = clear.Max.x - m - ui::metrics::U(3.0f) - x;
            if (hintW > ui::metrics::U(16.0f)) {
                const std::string h = ui::kit::elide(hint, hintW);
                ImGui::SetCursorScreenPos(ImVec2(x, y + (rowH - ImGui::GetTextLineHeight()) * 0.5f));
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.textDim[0], pal.textDim[1],
                                                            pal.textDim[2], pal.textDim[3]));
                ImGui::TextUnformatted(h.c_str());
                ImGui::PopStyleColor();
                hudNext(x);
            }
        });
    }
    ImGui::PopClipRect();

    // ---- editor popups (rendered last; they are separate ImGui windows) ----
    renderDimEditorPopup(st, tab, *sheet);
    renderGdtEditorPopup(st, tab);
    renderTitleBlockPopup(st, tab, *sheet);
}

}  // namespace ui
