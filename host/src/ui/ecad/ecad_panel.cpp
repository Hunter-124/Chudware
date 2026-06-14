// AI-ECAD UI scaffold (Wave D) — NOT yet build-verified; see docs/ECAD_UI_INTEGRATION.md
#include "imgui.h"

#include "ecad_panel.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

#include "../../core/app_state.h"
#include "../../ui_theme.h"

#include "ecad_canvas.h"
#include "ecad_mirror.h"

namespace ui {

namespace {

// Scaffold-local editor state. In the permanent wiring these three move onto AppState (see
// ECAD_UI_INTEGRATION.md §(e)); kept here so the scaffold honours the "do not edit AppState" rule.
struct EcadEditorState {
    EcadMirror mirror;
    EcadCanvas canvas;
    EcadMirror::Kind lastKind = EcadMirror::Kind::None;
    float leftW = 200.0f;     // left sub-panel width (px, pre-scale)
    bool  showLeft = true;
    // Wave Q — canvas editing tool state.
    std::string placeLib = "Device:R";             // symbol to place (Place tool, schematic)
    std::string placeFp  = "Resistor_SMD:R_0603";  // footprint to place (Place tool, board)
    bool  routing = false;       // a Route chain is in progress
    float lastX = 0, lastY = 0;  // last routed point (world mm)
    // Select-tool state: highlighted entity + live drag-move (symbols/footprints only).
    std::string selected;        // "<kind>:<id>" ("" = nothing), kind in {s,fp,w,t}
    bool  dragging = false;      // a left-drag-move of the selected sym/fp is in progress
    float dragDx = 0, dragDy = 0;  // live drag offset (world mm) for visual feedback
    // Simulation plot: snapshot of the last results we plotted + the analysis that produced them,
    // so the plot axes match the run (not the live combo) and we can pop the Sim tab on new results.
    nlohmann::json lastSimResults;
    int lastAnalysis = 0;
};

EcadEditorState& state() {
    static EcadEditorState s;
    return s;
}

ImVec4 v4(const float c[4], float a = 1.0f) { return ImVec4(c[0], c[1], c[2], c[3] * a); }

// Copper-layer swatch color, kept in sync with ecad_canvas.cpp::copperColor (palette roles, theme
// aware). Returns an ImVec4 for ImGui widget tinting.
ImVec4 layerSwatch(int copperIndex) {
    const ui::Palette& p = ui::palette;
    switch (((copperIndex % 6) + 6) % 6) {
        case 0:  return v4(p.axisX);
        case 1:  return v4(p.axisZ);
        case 2:  return v4(p.statusOk);
        case 3:  return v4(p.statusWarn);
        case 4:  return v4(p.axisY);
        default: return v4(p.statusErr);
    }
}

// Snap a world coordinate to a sensible ECAD grid (schematic 1.27mm; board 0.5mm).
float snapTo(float v, float g) { return std::round(v / g) * g; }

// Distance (world mm) from point p to segment a-b.
float ptSegDistP(const EcadPt& p, const EcadPt& a, const EcadPt& b) {
    const float vx = b.x - a.x, vy = b.y - a.y;
    const float wx = p.x - a.x, wy = p.y - a.y;
    const float vv = vx * vx + vy * vy;
    float t = (vv > 1e-9f) ? (wx * vx + wy * vy) / vv : 0.0f;
    t = std::max(0.0f, std::min(1.0f, t));
    const float cx = a.x + t * vx, cy = a.y + t * vy;
    return std::hypot(p.x - cx, p.y - cy);
}

// Hit-test the mirror at world point w (tolerance worldTol mm). Returns a "<kind>:<id>" token for
// the nearest entity, or "" for empty space. Symbols/footprints (by distance to pos within a
// generous body radius) win over wires/tracks (by point-to-segment distance).
std::string hitTest(const EcadMirror& m, const EcadPt& w, float worldTol) {
    std::string best;
    float bestD = 1e30f;
    if (m.isSch()) {
        const float bodyR = std::max(worldTol, 2.6f);  // generous symbol body radius (mm)
        for (const auto& s : m.symbols()) {
            const float d = std::hypot(w.x - s.pos.x, w.y - s.pos.y);
            if (d <= bodyR && d < bestD) { bestD = d; best = "s:" + s.id; }
        }
        if (!best.empty()) return best;  // symbols take priority over wires
        for (const auto& wire : m.wires()) {
            for (size_t i = 1; i < wire.pts.size(); ++i) {
                const float d = ptSegDistP(w, wire.pts[i - 1], wire.pts[i]);
                if (d <= worldTol && d < bestD) { bestD = d; best = "w:" + wire.id; }
            }
        }
    } else if (m.isPcb()) {
        for (const auto& fp : m.footprints()) {
            // Body radius from the courtyard half-diagonal when present, else a default.
            float bodyR = std::max(worldTol, 1.5f);
            if (fp.hasCourtyard) {
                bodyR = std::max(bodyR, 0.5f * std::hypot(fp.courtyardMax.x - fp.courtyardMin.x,
                                                          fp.courtyardMax.y - fp.courtyardMin.y));
            }
            const float d = std::hypot(w.x - fp.pos.x, w.y - fp.pos.y);
            if (d <= bodyR && d < bestD) { bestD = d; best = "fp:" + fp.id; }
        }
        if (!best.empty()) return best;  // footprints take priority over tracks
        for (const auto& t : m.tracks()) {
            const float tol = std::max(worldTol, t.width * 0.5f);
            const float d = ptSegDistP(w, t.p1, t.p2);
            if (d <= tol && d < bestD) { bestD = d; best = "t:" + t.id; }
        }
    }
    return best;
}

// Look up the world pos of the selected symbol/footprint (the only movable kinds). Returns false if
// the selection is not a sym/fp or the id is gone.
bool selectedPos(const EcadMirror& m, const std::string& sel, EcadPt& out) {
    const auto c = sel.find(':');
    if (c == std::string::npos) return false;
    const std::string kind = sel.substr(0, c), id = sel.substr(c + 1);
    if (kind == "s") {
        for (const auto& s : m.symbols()) if (s.id == id) { out = s.pos; return true; }
    } else if (kind == "fp") {
        for (const auto& fp : m.footprints()) if (fp.id == id) { out = fp.pos; return true; }
    }
    return false;
}

// Select-tool interaction: pick/highlight on click, Delete to remove, left-drag to move a
// symbol/footprint (emits sch_edit_symbol / pcb_edit_footprint on release, grid-snapped). Sets
// es.dragDx/dragDy each frame for the canvas's live visual feedback. Emits ops via
// st.ecadPendingOps / st.ecadOpsGo, same mechanism as Place/Route.
void handleSelectEdit(AppState& st, EcadEditorState& es, const EcadCanvasIO& io) {
    const bool sch = es.mirror.isSch();
    const float g = sch ? 1.27f : 0.5f;
    const float pxTol = 9.0f;
    const float worldTol = pxTol / std::max(0.01f, es.canvas.zoom());

    // ---- drag-move of the selected symbol/footprint ----
    // Begin a drag when a left-drag starts over the (already) selected, movable entity.
    if (!es.dragging && io.hovered && ImGui::IsMouseDragging(ImGuiMouseButton_Left)) {
        EcadPt selPos;
        if (selectedPos(es.mirror, es.selected, selPos)) {
            // Anchor the drag only if it started on/near the selected body (avoid grabbing on a
            // far-away drag, which should pan-free in Select mode does nothing).
            const ImVec2 d0 = ImGui::GetMouseDragDelta(ImGuiMouseButton_Left);
            const EcadPt startWorld{io.worldMouse.x - d0.x / es.canvas.zoom(),
                                    io.worldMouse.y + d0.y / es.canvas.zoom()};
            float bodyR = std::max(worldTol, sch ? 2.6f : 1.5f);
            if (std::hypot(startWorld.x - selPos.x, startWorld.y - selPos.y) <= bodyR * 1.5f)
                es.dragging = true;
        }
    }
    if (es.dragging) {
        const ImVec2 d = ImGui::GetMouseDragDelta(ImGuiMouseButton_Left);
        es.dragDx = d.x / es.canvas.zoom();
        es.dragDy = -d.y / es.canvas.zoom();  // screen down -> world down
        if (ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
            EcadPt selPos;
            if (selectedPos(es.mirror, es.selected, selPos) &&
                (std::fabs(es.dragDx) > 1e-4f || std::fabs(es.dragDy) > 1e-4f)) {
                const float nx = snapTo(selPos.x + es.dragDx, g);
                const float ny = snapTo(selPos.y + es.dragDy, g);
                const std::string id = es.selected.substr(es.selected.find(':') + 1);
                nlohmann::json ops = nlohmann::json::array();
                if (sch) ops.push_back({{"op", "sch_edit_symbol"}, {"id", id}, {"pos", {nx, ny}}});
                else     ops.push_back({{"op", "pcb_edit_footprint"}, {"id", id}, {"pos", {nx, ny}}});
                st.ecadPendingOps = ops; st.ecadOpsGo = true;
            }
            es.dragging = false;
            es.dragDx = es.dragDy = 0.0f;
        }
        return;  // a drag in progress consumes the interaction (no select-on-click)
    }
    es.dragDx = es.dragDy = 0.0f;

    // ---- Delete the selected entity (only when the canvas is hovered, so the key doesn't fire
    // while typing elsewhere) ----
    if (!es.selected.empty() && io.hovered && ImGui::IsKeyPressed(ImGuiKey_Delete)) {
        const auto c = es.selected.find(':');
        const std::string kind = es.selected.substr(0, c), id = es.selected.substr(c + 1);
        nlohmann::json ops = nlohmann::json::array();
        if (kind == "s")       ops.push_back({{"op", "sch_delete_symbol"}, {"id", id}});
        else if (kind == "w")  ops.push_back({{"op", "sch_delete_wire"}, {"id", id}});
        else if (kind == "fp") ops.push_back({{"op", "pcb_delete_footprint"}, {"id", id}});
        else if (kind == "t")  ops.push_back({{"op", "pcb_delete_track"}, {"id", id}});
        if (!ops.empty()) { st.ecadPendingOps = ops; st.ecadOpsGo = true; }
        es.selected.clear();
        return;
    }

    // ---- pick on click (hit-test nearest entity; empty space clears) ----
    if (io.clicked && io.hasWorldMouse)
        es.selected = hitTest(es.mirror, io.worldMouse, worldTol);
}

// Wave Q — translate a canvas click into a place/route op for the active tool. Sets
// st.ecadPendingOps / st.ecadOpsGo; the app loop applies them via ecad_apply_ops + reloads.
void handleCanvasEdit(AppState& st, EcadEditorState& es, const EcadCanvasIO& io) {
    // Esc or right-click ends an in-progress route chain.
    if (es.routing && (ImGui::IsKeyPressed(ImGuiKey_Escape) ||
                       ImGui::IsMouseClicked(ImGuiMouseButton_Right)))
        es.routing = false;
    if (st.ecadTool == 0) { handleSelectEdit(st, es, io); return; }
    if (!io.clicked || !io.hasWorldMouse) return;

    const bool sch = es.mirror.isSch();
    const float g = sch ? 1.27f : 0.5f;
    const float gx = snapTo(io.worldMouse.x, g), gy = snapTo(io.worldMouse.y, g);
    nlohmann::json ops = nlohmann::json::array();

    if (st.ecadTool == 1) {              // Place
        if (sch) ops.push_back({{"op", "sch_add_symbol"}, {"libid", es.placeLib}, {"pos", {gx, gy}}});
        else     ops.push_back({{"op", "pcb_add_footprint"}, {"fpid", es.placeFp}, {"pos", {gx, gy}}});
    } else if (st.ecadTool == 2) {       // Route (click-to-click polyline)
        if (es.routing) {
            if (sch) ops.push_back({{"op", "sch_add_wire"}, {"pts", {{es.lastX, es.lastY}, {gx, gy}}}});
            else     ops.push_back({{"op", "pcb_add_track"}, {"layer", "F.Cu"}, {"width", 0.25},
                                    {"p1", {es.lastX, es.lastY}}, {"p2", {gx, gy}}});
        }
        es.routing = true; es.lastX = gx; es.lastY = gy;  // anchor for the next segment
    }
    if (!ops.empty()) { st.ecadPendingOps = ops; st.ecadOpsGo = true; }
}

// ---- left sub-panel: PCB layers / sch nets ----
void drawLeftPanel(EcadEditorState& es) {
    const ui::Palette& pal = ui::palette;
    EcadMirror& m = es.mirror;

    if (m.isPcb()) {
        ImGui::TextColored(v4(pal.textDim), "Layers");
        ImGui::Separator();
        for (const auto& ly : m.layers()) {
            ImGui::PushID(ly.id.c_str());
            bool vis = m.layerVisible(ly.id);
            if (ImGui::Checkbox("##v", &vis)) m.setLayerVisible(ly.id, vis);
            ImGui::SameLine();
            // color swatch
            const ImVec4 sw = layerSwatch(ly.copperIndex);
            ImVec2 p0 = ImGui::GetCursorScreenPos();
            const float s = ImGui::GetTextLineHeight();
            ImGui::GetWindowDrawList()->AddRectFilled(
                p0, ImVec2(p0.x + s, p0.y + s), ui::Palette::toU32(&sw.x), 2.0f);
            ImGui::Dummy(ImVec2(s + 6, s));
            ImGui::SameLine();
            ImGui::TextColored(vis ? v4(pal.text) : v4(pal.textDisabled), "%s", ly.id.c_str());
            ImGui::PopID();
        }
        ImGui::Spacing();
        ImGui::TextColored(v4(pal.textDim), "Nets (%d)", (int)m.nets().size());
        ImGui::Separator();
        for (const auto& n : m.nets())
            ImGui::TextColored(v4(pal.text), "%s", n.name.c_str());
    } else if (m.isSch()) {
        ImGui::TextColored(v4(pal.textDim), "Nets (%d)", (int)m.nets().size());
        ImGui::Separator();
        for (const auto& n : m.nets()) {
            ImGui::TextColored(v4(pal.text), "%s", n.name.c_str());
            ImGui::SameLine();
            ImGui::TextColored(v4(pal.textDim), "(%d pins)", n.pinCount);
        }
        ImGui::Spacing();
        ImGui::TextColored(v4(pal.textDim), "Symbols (%d)", (int)m.symbols().size());
        ImGui::Separator();
        for (const auto& s : m.symbols()) {
            ImGui::TextColored(v4(pal.text), "%s", s.ref.empty() ? s.libid.c_str() : s.ref.c_str());
            if (!s.value.empty()) {
                ImGui::SameLine();
                ImGui::TextColored(v4(pal.textDim), "%s", s.value.c_str());
            }
        }
    }
}

// A distinct, theme-aware trace color per index (palette roles, like ecad_canvas::copperColor).
ImU32 traceColor(int i) {
    const ui::Palette& p = ui::palette;
    switch (((i % 6) + 6) % 6) {
        case 0:  return ui::Palette::toU32(p.accent);    // teal
        case 1:  return ui::Palette::toU32(p.axisX);     // red
        case 2:  return ui::Palette::toU32(p.axisY);     // green
        case 3:  return ui::Palette::toU32(p.axisZ);     // blue
        case 4:  return ui::Palette::toU32(p.statusWarn);// amber
        default: return ui::Palette::toU32(p.statusErr); // orange
    }
}

// One plotted trace: a label + the points (in DATA space) it samples. Filled by collectTraces.
struct PlotTrace {
    std::string name;
    std::vector<float> x, y;
};

// Gather voltage traces from the sim results for the active analysis. Transient -> x=time(s),
// y=volts. AC -> x=log10(freq Hz), y=magnitude in dB (20*log10(mag), guarded). I(...) current
// traces are skipped (voltages are the priority for the waveform view). xLabel/yLabel describe
// the axes for the caller. Returns false when there is nothing to plot.
bool collectTraces(const nlohmann::json& results, int analysis,
                   std::vector<PlotTrace>& out, std::string& xLabel, std::string& yLabel) {
    out.clear();
    auto wantTrace = [](const std::string& key) {
        // Plot voltage nodes; skip current traces like "I(V1)".
        return key.rfind("I(", 0) != 0 && key.rfind("i(", 0) != 0;
    };
    if (analysis == 1) {  // AC: Bode magnitude
        xLabel = "log10(f / Hz)";
        yLabel = "Magnitude (dB)";
        if (!results.contains("ac") || !results["ac"].is_object()) return false;
        const auto& ac = results["ac"];
        if (!ac.contains("x_f") || !ac.contains("mag")) return false;
        const auto& xf = ac["x_f"];
        const auto& mag = ac["mag"];
        if (!xf.is_array() || !mag.is_object()) return false;
        for (auto it = mag.begin(); it != mag.end(); ++it) {
            if (!it.value().is_array() || !wantTrace(it.key())) continue;
            PlotTrace tr; tr.name = it.key();
            const size_t n = std::min(xf.size(), it.value().size());
            for (size_t i = 0; i < n; ++i) {
                const double f = xf[i].is_number() ? xf[i].get<double>() : 0.0;
                double m = it.value()[i].is_number() ? it.value()[i].get<double>() : 0.0;
                if (f <= 0.0) continue;
                if (m <= 1e-12) m = 1e-12;  // guard log of non-positive magnitude
                double db = 20.0 * std::log10(m);
                if (db < -120.0) db = -120.0;  // floor so a single near-zero sample can't flatten scale
                tr.x.push_back((float)std::log10(f));
                tr.y.push_back((float)db);
            }
            if (tr.x.size() >= 2) out.push_back(std::move(tr));
        }
    } else {  // Transient: voltage vs time
        xLabel = "Time (s)";
        yLabel = "Volts (V)";
        if (!results.contains("tran") || !results["tran"].is_object()) return false;
        const auto& tran = results["tran"];
        if (!tran.contains("x_t") || !tran.contains("traces")) return false;
        const auto& xt = tran["x_t"];
        const auto& traces = tran["traces"];
        if (!xt.is_array() || !traces.is_object()) return false;
        for (auto it = traces.begin(); it != traces.end(); ++it) {
            if (!it.value().is_array() || !wantTrace(it.key())) continue;
            PlotTrace tr; tr.name = it.key();
            const size_t n = std::min(xt.size(), it.value().size());
            for (size_t i = 0; i < n; ++i) {
                const double t = xt[i].is_number() ? xt[i].get<double>() : 0.0;
                const double v = it.value()[i].is_number() ? it.value()[i].get<double>() : 0.0;
                tr.x.push_back((float)t);
                tr.y.push_back((float)v);
            }
            if (tr.x.size() >= 2) out.push_back(std::move(tr));
        }
    }
    return !out.empty();
}

// ---- bottom area: simulation waveform plot ----
// A bordered plot rect with labeled axes, each voltage trace an auto-scaled colored polyline, and a
// legend (node name + color) in the top-right corner. Drawn into the current child window with the
// window draw list. Falls back to a hint line when the results carry no plottable voltage traces.
void drawSimPlot(const nlohmann::json& results, int analysis) {
    const ui::Palette& pal = ui::palette;
    const float sc = ui::uiScale;

    std::vector<PlotTrace> traces;
    std::string xLabel, yLabel;
    if (!collectTraces(results, analysis, traces, xLabel, yLabel)) {
        ImGui::TextColored(v4(pal.textDim),
                           "Simulated, but no voltage traces to plot for this analysis.");
        return;
    }

    ImGui::TextColored(v4(pal.accent), "%s",
                       analysis == 1 ? "AC Sweep (Bode)" : "Transient Response");

    // X range: full span across all traces. Y range: percentile-clipped so a few extreme outliers
    // (e.g. an AC node that dips to ~0 magnitude -> a huge negative dB) can't flatten the curve.
    float xMin = 1e30f, xMax = -1e30f;
    std::vector<float> allY;
    for (const auto& tr : traces) {
        for (size_t i = 0; i < tr.x.size(); ++i) {
            xMin = std::min(xMin, tr.x[i]); xMax = std::max(xMax, tr.x[i]);
            allY.push_back(tr.y[i]);
        }
    }
    if (xMax - xMin < 1e-12f) xMax = xMin + 1.0f;
    std::sort(allY.begin(), allY.end());
    auto pctl = [&](float q) {
        if (allY.empty()) return 0.0f;
        const float idx = q * (float)(allY.size() - 1);
        const size_t lo = (size_t)idx;
        const size_t hi = std::min(lo + 1, allY.size() - 1);
        return allY[lo] + (allY[hi] - allY[lo]) * (idx - (float)lo);
    };
    float yMin = pctl(0.02f), yMax = pctl(0.98f);
    if (yMax - yMin < 1e-6f) { yMax += 0.5f; yMin -= 0.5f; }
    const float yPad = (yMax - yMin) * 0.08f;
    yMin -= yPad; yMax += yPad;

    // Plot rect inside the child, leaving room for axis labels.
    const float lpad = 52 * sc, rpad = 12 * sc, tpad = 6 * sc, bpad = 22 * sc;
    const ImVec2 avail = ImGui::GetContentRegionAvail();
    const ImVec2 org = ImGui::GetCursorScreenPos();
    const float plotW = std::max(40.0f, avail.x - lpad - rpad);
    const float plotH = std::max(30.0f, avail.y - tpad - bpad);
    const ImVec2 p0(org.x + lpad, org.y + tpad);
    const ImVec2 p1(p0.x + plotW, p0.y + plotH);

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImU32 borderCol = ui::Palette::toU32(pal.textDim);
    const ImU32 gridCol   = ui::Palette::toU32(pal.panelBg);  // faint gridlines
    const ImU32 axisLblCol = ui::Palette::toU32(pal.textDim);

    dl->AddRectFilled(p0, p1, ui::Palette::toU32(pal.windowBg));
    // Light gridlines (4 vertical, 4 horizontal).
    for (int g = 1; g < 4; ++g) {
        const float fx = p0.x + plotW * (g / 4.0f);
        const float fy = p0.y + plotH * (g / 4.0f);
        dl->AddLine(ImVec2(fx, p0.y), ImVec2(fx, p1.y), gridCol, 1.0f);
        dl->AddLine(ImVec2(p0.x, fy), ImVec2(p1.x, fy), gridCol, 1.0f);
    }
    dl->AddRect(p0, p1, borderCol);

    auto toScreen = [&](float x, float y) {
        const float sx = p0.x + (x - xMin) / (xMax - xMin) * plotW;
        float sy = p1.y - (y - yMin) / (yMax - yMin) * plotH;
        sy = std::max(p0.y, std::min(p1.y, sy));  // clamp outliers to the plot rect
        return ImVec2(sx, sy);
    };

    // Zero line for transient (volts).
    if (analysis != 1 && yMin < 0.0f && yMax > 0.0f) {
        const ImVec2 z0 = toScreen(xMin, 0.0f), z1 = toScreen(xMax, 0.0f);
        dl->AddLine(ImVec2(p0.x, z0.y), ImVec2(p1.x, z1.y), borderCol, 1.0f);
    }

    // Trace polylines.
    for (size_t ti = 0; ti < traces.size(); ++ti) {
        const PlotTrace& tr = traces[ti];
        const ImU32 c = traceColor((int)ti);
        std::vector<ImVec2> pts;
        pts.reserve(tr.x.size());
        for (size_t i = 0; i < tr.x.size(); ++i) pts.push_back(toScreen(tr.x[i], tr.y[i]));
        if (pts.size() >= 2)
            dl->AddPolyline(pts.data(), (int)pts.size(), c, ImDrawFlags_None, 1.8f);
    }

    // Axis min/max labels.
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.3g", yMax);
    dl->AddText(ImVec2(org.x + 4 * sc, p0.y - 2 * sc), axisLblCol, buf);
    std::snprintf(buf, sizeof(buf), "%.3g", yMin);
    dl->AddText(ImVec2(org.x + 4 * sc, p1.y - 14 * sc), axisLblCol, buf);
    std::snprintf(buf, sizeof(buf), "%.3g", xMin);
    dl->AddText(ImVec2(p0.x, p1.y + 4 * sc), axisLblCol, buf);
    std::snprintf(buf, sizeof(buf), "%.3g", xMax);
    dl->AddText(ImVec2(p1.x - 40 * sc, p1.y + 4 * sc), axisLblCol, buf);
    // X-axis title centered under the plot (between the min/max ticks); Y-axis title at mid-height.
    const float xLblW = ImGui::CalcTextSize(xLabel.c_str()).x;
    dl->AddText(ImVec2(p0.x + (plotW - xLblW) * 0.5f, p1.y + 4 * sc), axisLblCol, xLabel.c_str());
    dl->AddText(ImVec2(org.x + 2 * sc, p0.y + plotH * 0.5f), axisLblCol, yLabel.c_str());

    // Legend (top-right corner inside the plot).
    float ly = p0.y + 4 * sc;
    for (size_t ti = 0; ti < traces.size(); ++ti) {
        const ImU32 c = traceColor((int)ti);
        const float sw = ImGui::GetTextLineHeight();
        const ImVec2 lp(p1.x - 110 * sc, ly);
        dl->AddRectFilled(lp, ImVec2(lp.x + sw, lp.y + sw), c, 2.0f);
        dl->AddText(ImVec2(lp.x + sw + 4 * sc, lp.y), ui::Palette::toU32(pal.text),
                    traces[ti].name.c_str());
        ly += sw + 3 * sc;
    }

    // Reserve the space we drew into so the child sizes correctly.
    ImGui::Dummy(ImVec2(avail.x, avail.y));
}

// ---- bottom strip: ERC / DRC results ----
void drawResultsStrip(AppState& st, EcadEditorState& es) {
    const ui::Palette& pal = ui::palette;
    EcadMirror& m = es.mirror;

    const auto& list = m.isSch() ? m.erc() : m.drc();
    int errs = 0, warns = 0;
    for (const auto& v : list) (v.severity == "error" ? errs : warns)++;

    const char* kindLbl = m.isSch() ? "ERC" : "DRC";
    ImGui::TextColored(errs ? v4(pal.statusErr) : v4(pal.statusOk),
                       "%s: %d error%s", kindLbl, errs, errs == 1 ? "" : "s");
    ImGui::SameLine();
    ImGui::TextColored(warns ? v4(pal.statusWarn) : v4(pal.textDim),
                       "  %d warning%s", warns, warns == 1 ? "" : "s");
    if (m.isPcb()) {
        ImGui::SameLine();
        const int un = m.unroutedCount();
        ImGui::TextColored(un ? v4(pal.statusWarn) : v4(pal.statusOk),
                           "  |  Unrouted: %d", un);
    }
    ImGui::Separator();

    ImGui::BeginChild("##ecadResults", ImVec2(0, 0), ImGuiChildFlags_None);
    for (size_t i = 0; i < list.size(); ++i) {
        const EcadViolation& v = list[i];
        const ImVec4 c = (v.severity == "error") ? v4(pal.statusErr) : v4(pal.statusWarn);
        // One full-width selectable carrying the whole row; tint the text by the severity. Clicking
        // logs (the scaffold). The permanent hook is a viewport "zoom to violation" using v.loc
        // (guide §(e)). A unique ##id keeps ImGui ids stable across frames.
        char label[256];
        std::snprintf(label, sizeof(label), "[%s] %s%s%s##viol%zu",
                      v.severity.c_str(), v.rule.c_str(),
                      v.message.empty() ? "" : " — ", v.message.c_str(), i);
        ImGui::PushStyleColor(ImGuiCol_Text, c);
        const bool clicked = ImGui::Selectable(label);
        ImGui::PopStyleColor();
        if (clicked) {
            if (v.hasLoc)
                st.addLog("[..] ECAD " + v.rule + " @ (" +
                          std::to_string(v.loc.x) + ", " + std::to_string(v.loc.y) + ")");
            else
                st.addLog("[..] ECAD " + v.rule);
        }
    }
    if (list.empty())
        ImGui::TextColored(v4(pal.textDim), "No %s violations.", kindLbl);
    ImGui::EndChild();
}

}  // namespace

// ---- public API ----

EcadMirror& ecadEditorMirror() { return state().mirror; }

void ecadEditorSelect(const std::string& sel) { state().selected = sel; }

void ecadEditorLoadResponse(const nlohmann::json& resp) {
    EcadEditorState& es = state();
    es.mirror.loadFromTree(resp);
    if (es.mirror.kind() != es.lastKind) {
        es.canvas.requestFit();
        es.lastKind = es.mirror.kind();
    }
}

void drawEcadEditor(AppState& st) {
    EcadEditorState& es = state();
    const ui::Palette& pal = ui::palette;
    const float sc = ui::uiScale;

    // Pop the ECAD tab forward the frame a document first appears (New Schematic/PCB, or a shot),
    // so the editor surfaces instead of staying hidden behind the Viewport tab.
    static bool prevHadDoc = false;
    const bool hasDocNow = es.mirror.hasDoc();
    if (hasDocNow && !prevHadDoc) ImGui::SetNextWindowFocus();  // applies to the Begin below
    prevHadDoc = hasDocNow;

    if (!ImGui::Begin("ECAD")) { ImGui::End(); return; }

    if (!es.mirror.hasDoc()) {
        // Launchpad: describe a circuit -> the AI designs a verified schematic. (Blank docs come
        // from the PCB ribbon's New Schematic / New PCB.) The button hands the request to the app
        // loop via st.ecadAiRequest/ecadAiGo, which runs ecad_design + loads the result here.
        ImGui::Dummy(ImVec2(0, 18 * sc));
        ImGui::Indent(24 * sc);
        ImGui::TextColored(v4(pal.accent), "AI-ECAD  -  electronics design");
        ImGui::Spacing();
        ImGui::TextColored(v4(pal.textDim),
            "Describe a circuit and the AI designs a verified schematic, or start a blank");
        ImGui::TextColored(v4(pal.textDim),
            "document from the PCB ribbon (New Schematic / New PCB).");
        ImGui::Dummy(ImVec2(0, 10 * sc));
        static char aibuf[512] = "5V divider with 2.5V output";
        ImGui::SetNextItemWidth(440 * sc);
        ImGui::InputTextWithHint("##ecad_ai", "describe a circuit...", aibuf, sizeof(aibuf));
        ImGui::SameLine();
        if (ImGui::Button("Design with AI", ImVec2(150 * sc, 0))) {
            st.ecadAiRequest = aibuf;
            st.ecadAiGo = true;
        }
        ImGui::Dummy(ImVec2(0, 6 * sc));
        ImGui::TextColored(v4(pal.textDisabled),
            "examples:  inverting amp gain 10    RC low-pass 1kHz    555 astable");
        ImGui::Unindent(24 * sc);
        ImGui::End();
        return;
    }

    // ---- header toolbar: doc badge + name + revision (left); view controls (right) ----
    ImGui::AlignTextToFramePadding();
    ImGui::TextColored(v4(pal.accent), "%s", es.mirror.isSch() ? "SCHEMATIC" : "PCB LAYOUT");
    ImGui::SameLine(0, 12 * sc);
    ImGui::TextColored(v4(pal.text), "%s",
                       es.mirror.name().empty() ? "(untitled)" : es.mirror.name().c_str());
    ImGui::SameLine(0, 8 * sc);
    ImGui::TextColored(v4(pal.textDim), "rev %d", es.mirror.revision());
    ImGui::SameLine(ImGui::GetWindowContentRegionMax().x - 160 * sc);
    if (ImGui::SmallButton("Zoom Fit")) es.canvas.requestFit();
    ImGui::SameLine();
    ImGui::Checkbox(es.mirror.isPcb() ? "Layers" : "Nets", &es.showLeft);

    ImGui::Separator();

    // ---- canvas tool toolbar (Wave Q): Select / Place / Route + the part to place ----
    {
        auto toolBtn = [&](const char* label, int tool) {
            const bool on = (st.ecadTool == tool);
            if (on) ImGui::PushStyleColor(ImGuiCol_Button, v4(pal.accentDim));
            if (ImGui::Button(label)) { st.ecadTool = tool; es.routing = false; }
            if (on) ImGui::PopStyleColor();
            ImGui::SameLine();
        };
        toolBtn("Select", 0);
        toolBtn("Place", 1);
        toolBtn("Route", 2);
        ImGui::TextColored(v4(pal.textDim), "|");
        ImGui::SameLine();
        if (st.ecadTool == 1 && es.mirror.isSch()) {
            static const char* kLibs[] = {"Device:R", "Device:C", "Device:L", "Device:D",
                "Simulation_SPICE:V_source", "power:GND", "power:VCC"};
            ImGui::SetNextItemWidth(210 * sc);
            if (ImGui::BeginCombo("Part", es.placeLib.c_str())) {
                for (auto* l : kLibs)
                    if (ImGui::Selectable(l, es.placeLib == l)) es.placeLib = l;
                ImGui::EndCombo();
            }
        } else if (st.ecadTool == 1) {
            static const char* kFps[] = {"Resistor_SMD:R_0603", "Capacitor_SMD:C_0603",
                "Package_SO:SOIC-8", "Package_TO_SOT_SMD:SOT-23"};
            ImGui::SetNextItemWidth(250 * sc);
            if (ImGui::BeginCombo("Footprint", es.placeFp.c_str())) {
                for (auto* f : kFps)
                    if (ImGui::Selectable(f, es.placeFp == f)) es.placeFp = f;
                ImGui::EndCombo();
            }
        } else {
            ImGui::TextColored(v4(pal.textDim), st.ecadTool == 2
                ? "Route: click points; Esc / right-click to finish"
                : "Select: middle/right-drag to pan, wheel to zoom");
        }
        // Native simulator controls (schematic only): an analysis selector + a Simulate button that
        // fires ecad_simulate via the app loop (st.ecadSimGo). Results plot in the bottom strip.
        if (es.mirror.isSch()) {
            ImGui::SameLine(ImGui::GetWindowContentRegionMax().x - 240 * sc);
            ImGui::SetNextItemWidth(110 * sc);
            const char* kAnalyses[] = {"Transient", "AC"};
            int analysis = st.ecadSimAnalysis;
            if (ImGui::Combo("##ecadAnalysis", &analysis, kAnalyses, 2)) st.ecadSimAnalysis = analysis;
            ImGui::SameLine();
            if (ImGui::Button("Simulate", ImVec2(100 * sc, 0))) st.ecadSimGo = true;
        }
    }
    ImGui::Separator();

    // Reserve the bottom results strip; the rest splits left sub-panel | canvas. Give the strip
    // more room when there are sim results to plot so the waveform is readable.
    const bool hasSimNow = es.mirror.isSch() && st.ecadSimResults.is_object() &&
                           !st.ecadSimResults.empty();
    const float bottomH = (hasSimNow ? 260.0f : 140.0f) * sc;
    const ImVec2 avail = ImGui::GetContentRegionAvail();
    const float mainH = avail.y - bottomH;

    // Left sub-panel.
    if (es.showLeft) {
        ImGui::BeginChild("##ecadLeft", ImVec2(es.leftW * sc, mainH), ImGuiChildFlags_Border);
        drawLeftPanel(es);
        ImGui::EndChild();
        ImGui::SameLine();
    }

    // Canvas.
    ImGui::BeginChild("##ecadCanvasHost",
                      ImVec2(0, mainH), ImGuiChildFlags_Border,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    const ImVec2 canvasAvail = ImGui::GetContentRegionAvail();
    // Push the current selection + live drag offset into the canvas so it haloes the picked entity
    // (and shows drag feedback). Only the Select tool shows a selection.
    es.canvas.setSelection(st.ecadTool == 0 ? es.selected : std::string());
    es.canvas.setDragOffset(es.dragDx, es.dragDy);
    EcadCanvasIO io = es.canvas.draw(es.mirror, canvasAvail);
    handleCanvasEdit(st, es, io);  // Wave Q: select/place/route + delete/drag on the canvas
    // Surface the world cursor in the corner.
    if (io.hasWorldMouse) {
        char buf[64];
        std::snprintf(buf, sizeof(buf), "%.2f, %.2f mm", io.worldMouse.x, io.worldMouse.y);
        ImVec2 p = ImGui::GetWindowPos();
        ImGui::GetWindowDrawList()->AddText(
            ImVec2(p.x + 8, p.y + ImGui::GetWindowHeight() - 20),
            ui::Palette::toU32(pal.textDim), buf);
    }
    ImGui::EndChild();

    // Bottom area: ERC/DRC results, plus a Simulation waveform tab when results are present.
    ImGui::BeginChild("##ecadStrip", ImVec2(0, 0), ImGuiChildFlags_Border);
    const bool haveSim = es.mirror.isSch() && st.ecadSimResults.is_object() &&
                         !st.ecadSimResults.empty();
    if (haveSim && ImGui::BeginTabBar("##ecadBottomTabs")) {
        if (ImGui::BeginTabItem(es.mirror.isSch() ? "ERC" : "DRC")) {
            drawResultsStrip(st, es);
            ImGui::EndTabItem();
        }
        // Force the Simulation tab forward the frame results first appear (or change).
        ImGuiTabItemFlags simFlags = ImGuiTabItemFlags_None;
        if (es.lastSimResults != st.ecadSimResults) {
            simFlags = ImGuiTabItemFlags_SetSelected;
            es.lastSimResults = st.ecadSimResults;
            es.lastAnalysis = st.ecadSimAnalysis;
        }
        if (ImGui::BeginTabItem("Simulation", nullptr, simFlags)) {
            drawSimPlot(st.ecadSimResults, es.lastAnalysis);
            ImGui::EndTabItem();
        }
        ImGui::EndTabBar();
    } else {
        drawResultsStrip(st, es);
    }
    ImGui::EndChild();

    ImGui::End();
}

}  // namespace ui
