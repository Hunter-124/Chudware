// EcadPanel — the ECAD workspace UI (canvas + layers/nets + toolbar + ERC/DRC + sim plot), driving
// the C++ core's ecad_* RPCs. File-static singleton state; body drawn inside the "ECAD" dock window
// (application.cpp owns Begin/End — see ecad_panel.h).
//
// Ported from host/src/ui/ecad/ecad_panel.cpp (H2). All ecad_* RPC calls are byte-for-byte identical
// to the host (same method names + param shapes — see host/src/app.cpp's ecadAdopt/ecadCall/
// ecadApplyOps/ecadUndo/ecadRedo/ecadDocRpc/ecadAiDesign/ecadSimulate, folded in here as file-static
// helpers instead of AppState-staged one-shot flags consumed by an app loop — same synchronous
// st.worker.call() pattern cam_panel.cpp uses (camCall), since the new tree forbids AppState fields
// for panel state). Host's Wave-2 per-DocTab EcadEditorState is collapsed to a single active
// document (file-static, like CamUI) — matching CAM's single-active-job model; there is no ECAD
// doc-tab infra (dirty-tracking / save-as-you-switch) in the new tree yet (see task report).
//
// Palette seam: host's `ui::palette` / `ui::uiScale` map to `ui::ecad::ecadPalette()` (ecad_palette.h).
// Toast/log seam: host's `st.toast()` / `st.addLog()` map to `ui::postToast()` / `ui::c5::addLog()`.
#include "imgui.h"
#include "imgui_internal.h"

#include "ecad_panel.h"
#include "ecad_canvas.h"
#include "ecad_mirror.h"
#include "ecad_palette.h"

#include "app/app_state.h"
#include "theme/theme.h"
#include "ui/toast.h"
#include "ui/appearance_common.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace ui {

namespace {

// ---- tolerant JSON readers (local to this TU; same discipline as ecad_mirror.cpp) ----
std::string jstr(const nlohmann::json& o, const char* k, const char* dflt = "") {
    auto it = o.find(k);
    return (it != o.end() && it->is_string()) ? it->get<std::string>() : std::string(dflt);
}
int jint(const nlohmann::json& o, const char* k, int dflt = 0) {
    auto it = o.find(k);
    return (it != o.end() && it->is_number()) ? it->get<int>() : dflt;
}
bool jbool(const nlohmann::json& o, const char* k, bool dflt = false) {
    auto it = o.find(k);
    return (it != o.end() && it->is_boolean()) ? it->get<bool>() : dflt;
}

// ---- panel state (file-static singleton — see header note; CAM's CamUI is the precedent) ----
struct EcadUI {
    EcadMirror mirror;
    EcadCanvas canvas;
    EcadMirror::Kind lastKind = EcadMirror::Kind::None;
    std::string docId;        // worker ecad doc id ("" = none open)
    int  focusFrames = 0;     // >0: keep requesting focus (ecadEnsureVisible/FloatForShot/adopt)
    float leftW = 200.0f;     // left sub-panel width (px, pre-scale)
    bool  showLeft = true;

    // canvas editing tool state
    int   tool = 0;               // 0=Select, 1=Place, 2=Route
    std::string placeLib = "Device:R";             // symbol to place (Place tool, schematic)
    std::string placeFp  = "Resistor_SMD:R_0603";  // footprint to place (Place tool, board)
    bool  routing = false;        // a Route chain is in progress
    float lastX = 0, lastY = 0;   // last routed point (world mm)

    // Select-tool state: highlighted entity + live drag-move (symbols/footprints only).
    std::string selected;         // "<kind>:<id>" ("" = nothing), kind in {s,fp,w,t}
    bool  dragging = false;
    float dragDx = 0, dragDy = 0;

    // Property inspector buffers (selected symbol/footprint's editable Ref + Value + extras).
    char  editRef[64] = "";
    char  editValue[96] = "";
    char  editFootprint[128] = "";
    bool  editDnp = false;
    char  editSimParams[512] = "";
    std::string editLoadedFor;

    // AI launchpad request buffer.
    char  aiRequest[512] = "5V divider with 2.5V output";

    // Native simulator (ecad_simulate): last results + which analysis produced them + the plotted
    // snapshot (so the plot axes match the run, not the live combo) + a "pop the Sim tab" tracker.
    int   simAnalysis = 0;        // 0=Transient, 1=AC
    nlohmann::json simResults;
    nlohmann::json lastSimResults;
    int   lastAnalysis = 0;

    std::string status;
};
EcadUI g;

// ---- RPC helpers (synchronous; ecad_* routes to the C++ core via WorkerClient — byte-identical to
// host's app.cpp glue, just folded into the panel instead of AppState-staged flags) ----

// Feed a worker ECAD ops-response into the editor mirror; track doc id.
void ecadAdopt(const nlohmann::json& res) {
    const std::string id = jstr(res, "doc_id");
    if (!id.empty()) g.docId = id;
    if (!g.docId.empty()) g.mirror.setDocId(g.docId);
    g.mirror.loadFromTree(res);
    if (g.mirror.kind() != g.lastKind) {
        g.canvas.requestFit();
        g.lastKind = g.mirror.kind();
    }
}

// Blocking ECAD RPC; toasts on transport or {code,message} error. Returns false on failure.
bool ecadCall(app::AppState& st, const char* method, const nlohmann::json& params,
             nlohmann::json& res) {
    std::string err;
    if (!st.worker.call(method, params, res, err)) {
        ui::postToast(ui::ToastLevel::Error, std::string("ECAD ") + method + ": " + err);
        g.status = std::string(method) + ": " + (err.empty() ? std::string("failed") : err);
        return false;
    }
    if (res.is_object() && res.contains("code")) {
        ui::postToast(ui::ToastLevel::Error, "ECAD: " + jstr(res, "message"));
        g.status = jstr(res, "message");
        return false;
    }
    return true;
}

// A doc-scoped RPC that returns an ops-response (rebuild / autoroute / fill_zones) -> refresh.
void ecadDocRpc(app::AppState& st, const char* method, const char* okMsg) {
    if (g.docId.empty()) { ui::postToast(ui::ToastLevel::Warn, "Open an ECAD document first"); return; }
    nlohmann::json res;
    if (!ecadCall(st, method, {{"doc_id", g.docId}}, res)) return;
    ecadAdopt(res);
    if (okMsg) ui::postToast(ui::ToastLevel::Info, okMsg);
}

// Apply an op batch to the current ECAD doc (canvas place/route edits) with the optimistic-
// concurrency + stale-revision-resync discipline core/doc_mirror.cpp uses for the CAD doc.
bool ecadApplyOps(app::AppState& st, const nlohmann::json& ops) {
    if (g.docId.empty() || !ops.is_array() || ops.empty()) return false;
    nlohmann::json res; std::string err;
    nlohmann::json params = {{"doc_id", g.docId}, {"base_revision", g.mirror.revision()}, {"ops", ops}};
    if (!st.worker.call("ecad_apply_ops", params, res, err)) {
        ui::postToast(ui::ToastLevel::Error, "ECAD edit failed: " + err);
        return false;
    }
    if (res.is_object() && res.contains("code")) {
        if (jstr(res, "code") == "stale_revision") {
            nlohmann::json tr; std::string e2;
            if (st.worker.call("ecad_tree", {{"doc_id", g.docId}}, tr, e2) &&
                !(tr.is_object() && tr.contains("code"))) ecadAdopt(tr);
            ui::postToast(ui::ToastLevel::Warn, "ECAD edit dropped (stale) - resynced");
        } else {
            ui::postToast(ui::ToastLevel::Error, "ECAD: " + jstr(res, "message"));
        }
        return false;
    }
    ecadAdopt(res);
    return true;
}

// Undo / Redo the current ECAD doc via the worker's history (ecad_undo / ecad_redo).
void ecadUndoRun(app::AppState& st) {
    if (g.docId.empty()) return;
    nlohmann::json res; std::string err;
    if (!st.worker.call("ecad_undo", {{"doc_id", g.docId}}, res, err)) {
        ui::postToast(ui::ToastLevel::Error, "ECAD undo: " + err);
        return;
    }
    if (res.is_object() && res.contains("code")) {
        ui::postToast(ui::ToastLevel::Warn, "ECAD undo: " + jstr(res, "message"));
        return;
    }
    ecadAdopt(res);
    ui::postToast(ui::ToastLevel::Info, "Undo");
}

void ecadRedoRun(app::AppState& st) {
    if (g.docId.empty()) return;
    nlohmann::json res; std::string err;
    if (!st.worker.call("ecad_redo", {{"doc_id", g.docId}}, res, err)) {
        ui::postToast(ui::ToastLevel::Error, "ECAD redo: " + err);
        return;
    }
    if (res.is_object() && res.contains("code")) {
        ui::postToast(ui::ToastLevel::Warn, "ECAD redo: " + jstr(res, "message"));
        return;
    }
    ecadAdopt(res);
    ui::postToast(ui::ToastLevel::Info, "Redo");
}

// AI-design an ECAD schematic from a natural-language request (the launchpad "Design with AI"
// command): run the orchestrator (ecad_design) on the worker, then load the produced schematic.
void ecadAiDesignRun(app::AppState& st, const std::string& request) {
    if (request.empty()) return;
    ui::postToast(ui::ToastLevel::Info, "AI: designing \"" + request + "\" ...");
    c5::addLog(st, "[ecad] AI design: " + request);
    nlohmann::json res;
    if (!ecadCall(st, "ecad_design", {{"request", request}}, res)) return;
    const std::string id = jstr(res, "doc_id");
    if (id.empty()) { ui::postToast(ui::ToastLevel::Warn, "AI design produced no document"); return; }
    nlohmann::json tree;
    if (ecadCall(st, "ecad_tree", {{"doc_id", id}}, tree)) ecadAdopt(tree);
    const std::string recipe = jstr(res, "recipe_used");
    ui::postToast(ui::ToastLevel::Info,
                 recipe.empty() ? "AI design ready" : ("AI design ready (recipe: " + recipe + ")"));
}

// Native circuit simulation (ecad_simulate): build an analysis study from g.simAnalysis
// (0=Transient, 1=AC), run it on the worker, and stash the results payload so the sim plot renders.
void ecadSimulateRun(app::AppState& st) {
    if (g.docId.empty()) { ui::postToast(ui::ToastLevel::Warn, "Open a schematic to simulate"); return; }
    nlohmann::json study;
    if (g.simAnalysis == 1) {
        study = {{"analyses", {{ {"type", "ac"}, {"sweep", "dec"}, {"points", 20},
                                 {"fstart", 10.0}, {"fstop", 1e6} }} }};
    } else {
        study = {{"analyses", {{ {"type", "tran"}, {"tstep", 5e-5}, {"tstop", 5e-3} }} }};
    }
    nlohmann::json res; std::string err;
    if (!st.worker.call("ecad_simulate", {{"doc_id", g.docId}, {"study", study}}, res, err)) {
        ui::postToast(ui::ToastLevel::Error, std::string("Simulate failed: ") + err);
        return;
    }
    if (!jbool(res, "ok")) {
        ui::postToast(ui::ToastLevel::Error, "Simulate: " + jstr(res, "error"));
        return;
    }
    g.simResults = res.contains("results") ? res["results"] : nlohmann::json::object();
    const bool converged = jbool(res, "converged");
    ui::postToast(converged ? ui::ToastLevel::Info : ui::ToastLevel::Warn,
                 converged ? (g.simAnalysis == 1 ? "Simulated (AC)" : "Simulated (transient)")
                           : "Simulation ran, but convergence warnings need review");
    std::string line = "[ecad] simulate " + std::string(g.simAnalysis == 1 ? "ac" : "tran") +
                       (converged ? " converged" : " not converged");
    if (res.contains("convergence")) line += " " + res["convergence"].dump();
    if (res.contains("ms") && !res["ms"].is_null()) line += " " + res["ms"].dump() + "ms";
    c5::addLog(st, line);
    if (!converged && res.contains("log") && res["log"].is_array() && !res["log"].empty())
        c5::addLog(st, "[ecad] sim log: " + res["log"].back().dump());
}

ImVec4 v4(const float c[4], float a = 1.0f) { return ImVec4(c[0], c[1], c[2], c[3] * a); }

// Copper-layer swatch color, kept in sync with ecad_canvas.cpp::copperColor.
ImVec4 layerSwatch(int copperIndex) {
    const ui::ecad::EcadPalette& p = ui::ecad::ecadPalette();
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
float snapTo(float v, float gsz) { return std::round(v / gsz) * gsz; }

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
// the nearest entity, or "" for empty space. Symbols/footprints win over wires/tracks.
std::string hitTest(const EcadMirror& m, const EcadPt& w, float worldTol) {
    std::string best;
    float bestD = 1e30f;
    if (m.isSch()) {
        const float bodyR = std::max(worldTol, 2.6f);
        for (const auto& s : m.symbols()) {
            const float d = std::hypot(w.x - s.pos.x, w.y - s.pos.y);
            if (d <= bodyR && d < bestD) { bestD = d; best = "s:" + s.id; }
        }
        if (!best.empty()) return best;
        for (const auto& wire : m.wires()) {
            for (size_t i = 1; i < wire.pts.size(); ++i) {
                const float d = ptSegDistP(w, wire.pts[i - 1], wire.pts[i]);
                if (d <= worldTol && d < bestD) { bestD = d; best = "w:" + wire.id; }
            }
        }
    } else if (m.isPcb()) {
        for (const auto& fp : m.footprints()) {
            float bodyR = std::max(worldTol, 1.5f);
            if (fp.hasCourtyard) {
                bodyR = std::max(bodyR, 0.5f * std::hypot(fp.courtyardMax.x - fp.courtyardMin.x,
                                                          fp.courtyardMax.y - fp.courtyardMin.y));
            }
            const float d = std::hypot(w.x - fp.pos.x, w.y - fp.pos.y);
            if (d <= bodyR && d < bestD) { bestD = d; best = "fp:" + fp.id; }
        }
        if (!best.empty()) return best;
        for (const auto& t : m.tracks()) {
            const float tol = std::max(worldTol, t.width * 0.5f);
            const float d = ptSegDistP(w, t.p1, t.p2);
            if (d <= tol && d < bestD) { bestD = d; best = "t:" + t.id; }
        }
    }
    return best;
}

// Look up the world pos of the selected symbol/footprint (the only movable kinds).
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

// Rotate (+90 deg CCW) or flip (mirror/side toggle) the selected symbol/footprint.
void rotateOrFlipSelected(app::AppState& st, bool rotate) {
    const auto c = g.selected.find(':');
    if (c == std::string::npos) return;
    const std::string kind = g.selected.substr(0, c), id = g.selected.substr(c + 1);
    nlohmann::json op;
    if (kind == "s") {
        const EcadSymbol* sym = nullptr;
        for (const auto& s : g.mirror.symbols()) if (s.id == id) { sym = &s; break; }
        if (!sym) return;
        op = {{"op", "sch_edit_symbol"}, {"id", id}};
        if (rotate) op["angle"]  = std::fmod(sym->angle + 90.0f, 360.0f);
        else        op["mirror"] = (sym->mirror == "none" ? "x" : "none");
    } else if (kind == "fp") {
        const EcadFootprint* fp = nullptr;
        for (const auto& f : g.mirror.footprints()) if (f.id == id) { fp = &f; break; }
        if (!fp) return;
        op = {{"op", "pcb_edit_footprint"}, {"id", id}};
        if (rotate) op["angle"] = std::fmod(fp->angle + 90.0f, 360.0f);
        else        op["side"]  = (fp->side == "top" ? "bottom" : "top");
    } else {
        return;
    }
    nlohmann::json ops = nlohmann::json::array();
    ops.push_back(op);
    ecadApplyOps(st, ops);
}

// Property inspector (Select tool): when a symbol/footprint is selected, expose its Ref + Value as
// inline editable fields (+ schematic-only footprint/DNP/sim-params extras).
bool drawPropInspector(app::AppState& st, float sc) {
    const auto c = g.selected.find(':');
    if (c == std::string::npos) return false;
    const std::string kind = g.selected.substr(0, c), id = g.selected.substr(c + 1);
    const bool sch = (kind == "s"), pcb = (kind == "fp");
    if (!sch && !pcb) return false;

    const std::string* ref = nullptr; const std::string* value = nullptr; std::string libLabel;
    const EcadSymbol* sym = nullptr;
    if (sch) {
        for (const auto& s : g.mirror.symbols())
            if (s.id == id) { sym = &s; ref = &s.ref; value = &s.value; libLabel = s.libid; break; }
    } else {
        for (const auto& fp : g.mirror.footprints())
            if (fp.id == id) { ref = &fp.ref; value = &fp.value; libLabel = fp.fpid; break; }
    }
    if (!ref) return false;

    if (g.editLoadedFor != g.selected) {
        std::snprintf(g.editRef, sizeof(g.editRef), "%s", ref->c_str());
        std::snprintf(g.editValue, sizeof(g.editValue), "%s", value->c_str());
        if (sym) {
            std::snprintf(g.editFootprint, sizeof(g.editFootprint), "%s", sym->footprint.c_str());
            g.editDnp = sym->dnp;
            const std::string spText =
                (sym->simParams.is_object() && !sym->simParams.empty()) ? sym->simParams.dump() : "";
            std::snprintf(g.editSimParams, sizeof(g.editSimParams), "%s", spText.c_str());
        } else {
            g.editFootprint[0] = '\0';
            g.editDnp = false;
            g.editSimParams[0] = '\0';
        }
        g.editLoadedFor = g.selected;
    }

    const ui::ecad::EcadPalette& pal = ui::ecad::ecadPalette();
    ImGui::TextColored(v4(pal.accent), "%s", libLabel.empty() ? "Properties" : libLabel.c_str());
    ImGui::SameLine(0, 10 * sc);
    ImGui::SetNextItemWidth(80 * sc);
    bool commit = ImGui::InputText("Ref", g.editRef, sizeof(g.editRef),
                                   ImGuiInputTextFlags_EnterReturnsTrue);
    ImGui::SameLine();
    ImGui::SetNextItemWidth(120 * sc);
    commit |= ImGui::InputText("Value", g.editValue, sizeof(g.editValue),
                               ImGuiInputTextFlags_EnterReturnsTrue);
    ImGui::SameLine();
    if (ImGui::Button("Apply")) commit = true;
    if (commit) {
        nlohmann::json ops = nlohmann::json::array();
        ops.push_back({{"op", sch ? "sch_edit_symbol" : "pcb_edit_footprint"},
                       {"id", id}, {"ref", g.editRef}, {"value", g.editValue}});
        if (ecadApplyOps(st, ops)) g.editLoadedFor.clear();
    }
    ImGui::SameLine(0, 14 * sc);
    if (ImGui::Button("Rotate (R)")) rotateOrFlipSelected(st, /*rotate=*/true);
    ImGui::SameLine();
    if (ImGui::Button(sch ? "Flip (F)" : "Flip side (F)")) rotateOrFlipSelected(st, false);

    if (sch) {
        ImGui::SetNextItemWidth(210 * sc);
        bool fpCommit = ImGui::InputText("Footprint", g.editFootprint, sizeof(g.editFootprint),
                                         ImGuiInputTextFlags_EnterReturnsTrue);
        ImGui::SameLine();
        if (ImGui::BeginCombo("##fpPick", "Pick", ImGuiComboFlags_NoPreview)) {
            static const char* kFps[] = {"Resistor_SMD:R_0603", "Capacitor_SMD:C_0603",
                "Package_SO:SOIC-8", "Package_TO_SOT_SMD:SOT-23"};
            for (auto* f : kFps)
                if (ImGui::Selectable(f, std::string(g.editFootprint) == f)) {
                    std::snprintf(g.editFootprint, sizeof(g.editFootprint), "%s", f);
                    fpCommit = true;
                }
            ImGui::EndCombo();
        }
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Pick a footprint from the library");
        ImGui::SameLine();
        if (ImGui::Button("Set##fp")) fpCommit = true;
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Assign the footprint (blank clears it)");
        if (fpCommit) {
            nlohmann::json ops = nlohmann::json::array();
            nlohmann::json op = {{"op", "sch_edit_symbol"}, {"id", id}};
            op["footprint"] = (g.editFootprint[0] == '\0') ? nlohmann::json(nullptr)
                                                            : nlohmann::json(std::string(g.editFootprint));
            ops.push_back(op);
            if (ecadApplyOps(st, ops)) g.editLoadedFor.clear();
        }

        if (ImGui::Checkbox("DNP (do-not-populate)", &g.editDnp)) {
            nlohmann::json ops = nlohmann::json::array();
            ops.push_back({{"op", "sch_edit_symbol"}, {"id", id}, {"dnp", g.editDnp}});
            if (ecadApplyOps(st, ops)) g.editLoadedFor.clear();
        }

        ImGui::TextColored(v4(pal.textDim), "Sim params (JSON)");
        ImGui::InputTextMultiline("##simParams", g.editSimParams, sizeof(g.editSimParams),
                                  ImVec2(280 * sc, 48 * sc));
        ImGui::SameLine();
        if (ImGui::Button("Apply##sim")) {
            const std::string txt(g.editSimParams);
            const auto first = txt.find_first_not_of(" \t\r\n");
            const bool blank = (first == std::string::npos);
            nlohmann::json parsed;
            bool ok = true;
            if (!blank) {
                parsed = nlohmann::json::parse(txt, nullptr, /*allow_exceptions=*/false);
                if (parsed.is_discarded() || !parsed.is_object()) ok = false;
            }
            if (!ok) {
                c5::addLog(st, "[ecad] sim params: invalid JSON object — not applied");
            } else {
                nlohmann::json ops = nlohmann::json::array();
                nlohmann::json op = {{"op", "sch_edit_symbol"}, {"id", id}};
                op["sim_params"] = blank ? nlohmann::json(nullptr) : parsed;
                ops.push_back(op);
                if (ecadApplyOps(st, ops)) g.editLoadedFor.clear();
            }
        }
    }
    return true;
}

// Select-tool interaction: pick/highlight on click, Delete to remove, left-drag to move a
// symbol/footprint (emits sch_edit_symbol / pcb_edit_footprint on release, grid-snapped).
void handleSelectEdit(app::AppState& st, const EcadCanvasIO& io) {
    const bool sch = g.mirror.isSch();
    const float gsz = sch ? 1.27f : 0.5f;
    const float pxTol = 9.0f;
    const float worldTol = pxTol / std::max(0.01f, g.canvas.zoom());

    if (!g.dragging && io.hovered && ImGui::IsMouseDragging(ImGuiMouseButton_Left)) {
        EcadPt selPos;
        if (selectedPos(g.mirror, g.selected, selPos)) {
            const ImVec2 d0 = ImGui::GetMouseDragDelta(ImGuiMouseButton_Left);
            const EcadPt startWorld{io.worldMouse.x - d0.x / g.canvas.zoom(),
                                    io.worldMouse.y + d0.y / g.canvas.zoom()};
            float bodyR = std::max(worldTol, sch ? 2.6f : 1.5f);
            if (std::hypot(startWorld.x - selPos.x, startWorld.y - selPos.y) <= bodyR * 1.5f)
                g.dragging = true;
        }
    }
    if (g.dragging) {
        const ImVec2 d = ImGui::GetMouseDragDelta(ImGuiMouseButton_Left);
        g.dragDx = d.x / g.canvas.zoom();
        g.dragDy = -d.y / g.canvas.zoom();
        if (ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
            EcadPt selPos;
            if (selectedPos(g.mirror, g.selected, selPos) &&
                (std::fabs(g.dragDx) > 1e-4f || std::fabs(g.dragDy) > 1e-4f)) {
                const float nx = snapTo(selPos.x + g.dragDx, gsz);
                const float ny = snapTo(selPos.y + g.dragDy, gsz);
                const std::string id = g.selected.substr(g.selected.find(':') + 1);
                nlohmann::json ops = nlohmann::json::array();
                if (sch) ops.push_back({{"op", "sch_edit_symbol"}, {"id", id}, {"pos", {nx, ny}}});
                else     ops.push_back({{"op", "pcb_edit_footprint"}, {"id", id}, {"pos", {nx, ny}}});
                ecadApplyOps(st, ops);
            }
            g.dragging = false;
            g.dragDx = g.dragDy = 0.0f;
        }
        return;
    }
    g.dragDx = g.dragDy = 0.0f;

    if (!g.selected.empty() && io.hovered && !ImGui::GetIO().WantTextInput) {
        if (ImGui::IsKeyPressed(ImGuiKey_R)) { rotateOrFlipSelected(st, /*rotate=*/true);  return; }
        if (ImGui::IsKeyPressed(ImGuiKey_F)) { rotateOrFlipSelected(st, /*rotate=*/false); return; }
    }

    if (!g.selected.empty() && io.hovered && ImGui::IsKeyPressed(ImGuiKey_Delete)) {
        const auto c = g.selected.find(':');
        const std::string kind = g.selected.substr(0, c), id = g.selected.substr(c + 1);
        nlohmann::json ops = nlohmann::json::array();
        if (kind == "s")       ops.push_back({{"op", "sch_delete_symbol"}, {"id", id}});
        else if (kind == "w")  ops.push_back({{"op", "sch_delete_wire"}, {"id", id}});
        else if (kind == "fp") ops.push_back({{"op", "pcb_delete_footprint"}, {"id", id}});
        else if (kind == "t")  ops.push_back({{"op", "pcb_delete_track"}, {"id", id}});
        if (!ops.empty()) ecadApplyOps(st, ops);
        g.selected.clear();
        return;
    }

    if (io.clicked && io.hasWorldMouse)
        g.selected = hitTest(g.mirror, io.worldMouse, worldTol);
}

// Translate a canvas click into a place/route op for the active tool.
void handleCanvasEdit(app::AppState& st, const EcadCanvasIO& io) {
    if (g.routing && (ImGui::IsKeyPressed(ImGuiKey_Escape) ||
                      ImGui::IsMouseClicked(ImGuiMouseButton_Right)))
        g.routing = false;
    if (g.tool == 0) { handleSelectEdit(st, io); return; }
    if (!io.clicked || !io.hasWorldMouse) return;

    const bool sch = g.mirror.isSch();
    const float gsz = sch ? 1.27f : 0.5f;
    const float gx = snapTo(io.worldMouse.x, gsz), gy = snapTo(io.worldMouse.y, gsz);
    nlohmann::json ops = nlohmann::json::array();

    if (g.tool == 1) {              // Place
        if (sch) ops.push_back({{"op", "sch_add_symbol"}, {"libid", g.placeLib}, {"pos", {gx, gy}}});
        else     ops.push_back({{"op", "pcb_add_footprint"}, {"fpid", g.placeFp}, {"pos", {gx, gy}}});
    } else if (g.tool == 2) {       // Route (click-to-click polyline)
        if (g.routing) {
            if (sch) ops.push_back({{"op", "sch_add_wire"}, {"pts", {{g.lastX, g.lastY}, {gx, gy}}}});
            else     ops.push_back({{"op", "pcb_add_track"}, {"layer", "F.Cu"}, {"width", 0.25},
                                    {"p1", {g.lastX, g.lastY}}, {"p2", {gx, gy}}});
        }
        g.routing = true; g.lastX = gx; g.lastY = gy;
    }
    if (!ops.empty()) ecadApplyOps(st, ops);
}

// ---- left sub-panel: PCB layers / sch nets ----
void drawLeftPanel() {
    const ui::ecad::EcadPalette& pal = ui::ecad::ecadPalette();
    EcadMirror& m = g.mirror;

    if (m.isPcb()) {
        ImGui::TextColored(v4(pal.textDim), "Layers");
        ImGui::Separator();
        for (const auto& ly : m.layers()) {
            ImGui::PushID(ly.id.c_str());
            bool vis = m.layerVisible(ly.id);
            if (ImGui::Checkbox("##v", &vis)) m.setLayerVisible(ly.id, vis);
            ImGui::SameLine();
            const ImVec4 sw = layerSwatch(ly.copperIndex);
            ImVec2 p0 = ImGui::GetCursorScreenPos();
            const float s = ImGui::GetTextLineHeight();
            ImGui::GetWindowDrawList()->AddRectFilled(
                p0, ImVec2(p0.x + s, p0.y + s), pal.toU32(&sw.x), 2.0f);
            ImGui::Dummy(ImVec2(s + 6 * pal.scale, s));
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

// A distinct color per trace index (palette roles, like ecad_canvas::copperColor).
ImU32 traceColor(int i) {
    const ui::ecad::EcadPalette& p = ui::ecad::ecadPalette();
    switch (((i % 6) + 6) % 6) {
        case 0:  return p.toU32(p.accent);
        case 1:  return p.toU32(p.axisX);
        case 2:  return p.toU32(p.axisY);
        case 3:  return p.toU32(p.axisZ);
        case 4:  return p.toU32(p.statusWarn);
        default: return p.toU32(p.statusErr);
    }
}

struct PlotTrace {
    std::string name;
    std::vector<float> x, y;
};

// Gather voltage traces from the sim results for the active analysis.
bool collectTraces(const nlohmann::json& results, int analysis,
                   std::vector<PlotTrace>& out, std::string& xLabel, std::string& yLabel) {
    out.clear();
    auto wantTrace = [](const std::string& key) {
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
                if (m <= 1e-12) m = 1e-12;
                double db = 20.0 * std::log10(m);
                if (db < -120.0) db = -120.0;
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

void drawSimMetadata(const nlohmann::json& results) {
    const ui::ecad::EcadPalette& pal = ui::ecad::ecadPalette();
    if (!results.is_object()) return;

    if (results.contains("convergence") && results["convergence"].is_object()) {
        std::string summary;
        bool allOk = true;
        for (auto it = results["convergence"].begin(); it != results["convergence"].end(); ++it) {
            const bool ok = it.value().is_boolean() ? it.value().get<bool>() : false;
            allOk = allOk && ok;
            if (!summary.empty()) summary += "  ";
            summary += it.key() + std::string(ok ? ": ok" : ": review");
        }
        ImGui::TextColored(v4(allOk ? pal.statusOk : pal.statusWarn), "%s", summary.c_str());
    }
    if (results.contains("ms") && results["ms"].is_number()) {
        ImGui::SameLine();
        ImGui::TextColored(v4(pal.textDim), "%.2f ms", results["ms"].get<double>());
    }
    if (results.contains("log") && results["log"].is_array() && !results["log"].empty()) {
        const auto& last = results["log"].back();
        std::string msg = last.is_string() ? last.get<std::string>() : last.dump();
        ImGui::TextWrapped("%s", msg.c_str());
    }
}

bool drawScalarResults(const nlohmann::json& results) {
    const ui::ecad::EcadPalette& pal = ui::ecad::ecadPalette();
    const char* keys[] = {"op", "dc", "tf", "noise", "four"};
    for (const char* key : keys) {
        if (!results.contains(key) || !results[key].is_object()) continue;
        ImGui::TextColored(v4(pal.accent), "%s", key);
        if (ImGui::BeginTable("##simScalarResults", 2,
                              ImGuiTableFlags_BordersInnerH | ImGuiTableFlags_RowBg)) {
            ImGui::TableSetupColumn("Signal");
            ImGui::TableSetupColumn("Value");
            ImGui::TableHeadersRow();
            int shown = 0;
            for (auto it = results[key].begin(); it != results[key].end() && shown < 16; ++it, ++shown) {
                ImGui::TableNextRow();
                ImGui::TableSetColumnIndex(0);
                ImGui::TextUnformatted(it.key().c_str());
                ImGui::TableSetColumnIndex(1);
                std::string val = it.value().is_number() ? std::to_string(it.value().get<double>())
                                                         : it.value().dump();
                ImGui::TextUnformatted(val.c_str());
            }
            ImGui::EndTable();
        }
        return true;
    }
    return false;
}

// ---- bottom area: simulation waveform plot ----
void drawSimPlot(const nlohmann::json& results, int analysis) {
    const ui::ecad::EcadPalette& pal = ui::ecad::ecadPalette();
    const float sc = pal.scale;

    drawSimMetadata(results);

    std::vector<PlotTrace> traces;
    std::string xLabel, yLabel;
    if (!collectTraces(results, analysis, traces, xLabel, yLabel)) {
        if (drawScalarResults(results)) return;
        ImGui::TextColored(v4(pal.textDim),
                           "Simulated, but no voltage traces to plot for this analysis.");
        return;
    }

    ImGui::TextColored(v4(pal.accent), "%s",
                       analysis == 1 ? "AC Sweep (Bode)" : "Transient Response");

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

    const float lpad = 52 * sc, rpad = 12 * sc, tpad = 6 * sc, bpad = 22 * sc;
    const ImVec2 avail = ImGui::GetContentRegionAvail();
    const ImVec2 org = ImGui::GetCursorScreenPos();
    const float plotW = std::max(40.0f, avail.x - lpad - rpad);
    const float plotH = std::max(30.0f, avail.y - tpad - bpad);
    const ImVec2 p0(org.x + lpad, org.y + tpad);
    const ImVec2 p1(p0.x + plotW, p0.y + plotH);

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImU32 borderCol = pal.toU32(pal.textDim);
    const ImU32 gridCol   = pal.toU32(pal.panelBg);
    const ImU32 axisLblCol = pal.toU32(pal.textDim);

    dl->AddRectFilled(p0, p1, pal.toU32(pal.windowBg));
    for (int gStep = 1; gStep < 4; ++gStep) {
        const float fx = p0.x + plotW * (gStep / 4.0f);
        const float fy = p0.y + plotH * (gStep / 4.0f);
        dl->AddLine(ImVec2(fx, p0.y), ImVec2(fx, p1.y), gridCol, 1.0f);
        dl->AddLine(ImVec2(p0.x, fy), ImVec2(p1.x, fy), gridCol, 1.0f);
    }
    dl->AddRect(p0, p1, borderCol);

    auto toScreen = [&](float x, float y) {
        const float sx = p0.x + (x - xMin) / (xMax - xMin) * plotW;
        float sy = p1.y - (y - yMin) / (yMax - yMin) * plotH;
        sy = std::max(p0.y, std::min(p1.y, sy));
        return ImVec2(sx, sy);
    };

    if (analysis != 1 && yMin < 0.0f && yMax > 0.0f) {
        const ImVec2 z0 = toScreen(xMin, 0.0f), z1 = toScreen(xMax, 0.0f);
        dl->AddLine(ImVec2(p0.x, z0.y), ImVec2(p1.x, z1.y), borderCol, 1.0f);
    }

    for (size_t ti = 0; ti < traces.size(); ++ti) {
        const PlotTrace& tr = traces[ti];
        const ImU32 c = traceColor((int)ti);
        std::vector<ImVec2> pts;
        pts.reserve(tr.x.size());
        for (size_t i = 0; i < tr.x.size(); ++i) pts.push_back(toScreen(tr.x[i], tr.y[i]));
        if (pts.size() >= 2)
            dl->AddPolyline(pts.data(), (int)pts.size(), c, ImDrawFlags_None, 1.8f);
    }

    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.3g", yMax);
    dl->AddText(ImVec2(org.x + 4 * sc, p0.y - 2 * sc), axisLblCol, buf);
    std::snprintf(buf, sizeof(buf), "%.3g", yMin);
    dl->AddText(ImVec2(org.x + 4 * sc, p1.y - 14 * sc), axisLblCol, buf);
    std::snprintf(buf, sizeof(buf), "%.3g", xMin);
    dl->AddText(ImVec2(p0.x, p1.y + 4 * sc), axisLblCol, buf);
    std::snprintf(buf, sizeof(buf), "%.3g", xMax);
    dl->AddText(ImVec2(p1.x - 40 * sc, p1.y + 4 * sc), axisLblCol, buf);
    const float xLblW = ImGui::CalcTextSize(xLabel.c_str()).x;
    dl->AddText(ImVec2(p0.x + (plotW - xLblW) * 0.5f, p1.y + 4 * sc), axisLblCol, xLabel.c_str());
    dl->AddText(ImVec2(org.x + 2 * sc, p0.y + plotH * 0.5f), axisLblCol, yLabel.c_str());

    float ly = p0.y + 4 * sc;
    for (size_t ti = 0; ti < traces.size(); ++ti) {
        const ImU32 c = traceColor((int)ti);
        const float sw = ImGui::GetTextLineHeight();
        const ImVec2 lp(p1.x - 110 * sc, ly);
        dl->AddRectFilled(lp, ImVec2(lp.x + sw, lp.y + sw), c, 2.0f * sc);
        dl->AddText(ImVec2(lp.x + sw + 4 * sc, lp.y), pal.toU32(pal.text), traces[ti].name.c_str());
        ly += sw + 3 * sc;
    }

    ImGui::Dummy(ImVec2(avail.x, avail.y));
}

// ---- bottom strip: ERC / DRC results ----
void drawResultsStrip(app::AppState& st) {
    const ui::ecad::EcadPalette& pal = ui::ecad::ecadPalette();
    EcadMirror& m = g.mirror;

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
        char label[256];
        std::snprintf(label, sizeof(label), "[%s] %s%s%s##viol%zu",
                      v.severity.c_str(), v.rule.c_str(),
                      v.message.empty() ? "" : " — ", v.message.c_str(), i);
        ImGui::PushStyleColor(ImGuiCol_Text, c);
        const bool clicked = ImGui::Selectable(label);
        ImGui::PopStyleColor();
        if (v.hasLoc && ImGui::IsItemHovered())
            ImGui::SetTooltip("Click to zoom to (%.2f, %.2f) mm", v.loc.x, v.loc.y);
        if (clicked) {
            if (v.hasLoc) {
                g.canvas.focusOn(v.loc);
                c5::addLog(st, "[ecad] zoom to " + v.rule + " @ (" +
                          std::to_string(v.loc.x) + ", " + std::to_string(v.loc.y) + ")");
            } else {
                c5::addLog(st, "[ecad] " + v.rule + " (no location)");
            }
        }
    }
    if (list.empty())
        ImGui::TextColored(v4(pal.textDim), "No %s violations.", kindLbl);
    ImGui::EndChild();
}

}  // namespace

// ---- public API ----

EcadMirror& ecadEditorMirror() { return g.mirror; }

void ecadEnsureVisible() { g.focusFrames = 10; }

void ecadFloatForShot() { g.focusFrames = 10; }

bool ecadTakeFocusRequest() {
    // Shot-env seam: application.cpp polls this every frame BEFORE Begin("ECAD"...), mirroring
    // ui::camTakeFocusRequest() — the one place a one-time env check can bring the ECAD tab forward
    // for a headless AI_CAD_SHOT=ecad capture.
    static bool shotChecked = false;
    if (!shotChecked) {
        shotChecked = true;
        const char* shot = std::getenv("AI_CAD_SHOT");
        if (shot && std::string(shot) == "ecad") ecadFloatForShot();
    }
    if (g.focusFrames > 0) { g.focusFrames--; return true; }
    return false;
}

void ecadHandleNewDoc(app::AppState& st, const char* kind) {
    nlohmann::json res;
    if (!ecadCall(st, "ecad_new", {{"kind", kind}}, res)) return;
    ecadAdopt(res);
    c5::addLog(st, std::string("[ecad] new ") + kind + " doc " + g.docId);
    ui::postToast(ui::ToastLevel::Info,
                 std::string("New ECAD ") + (std::string(kind) == "sch" ? "schematic" : "board"));
    ecadEnsureVisible();
}

void drawEcadEditor(app::AppState& st) {
    ui::ecad::ecadPalette().scale = theme::S();
    const ui::ecad::EcadPalette& pal = ui::ecad::ecadPalette();
    const float sc = pal.scale;

    if (!g.mirror.hasDoc()) {
        // Launchpad: describe a circuit -> the AI designs a verified schematic. (Blank docs come
        // from ecadHandleNewDoc, wired to the doc-tab chooser's New Schematic / New PCB.)
        ImGui::Dummy(ImVec2(0, 18 * sc));
        ImGui::Indent(24 * sc);
        ImGui::TextColored(v4(pal.accent), "AI-ECAD  -  electronics design");
        ImGui::Spacing();
        ImGui::TextColored(v4(pal.textDim),
            "Describe a circuit and the AI designs a verified schematic, or start a blank");
        ImGui::TextColored(v4(pal.textDim),
            "document from the doc-tab chooser (New Schematic / New PCB).");
        ImGui::Dummy(ImVec2(0, 10 * sc));
        ImGui::SetNextItemWidth(440 * sc);
        ImGui::InputTextWithHint("##ecad_ai", "describe a circuit...", g.aiRequest, sizeof(g.aiRequest));
        ImGui::SameLine();
        if (ImGui::Button("Design with AI", ImVec2(150 * sc, 0))) {
            ecadAiDesignRun(st, g.aiRequest);
            ecadEnsureVisible();
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Generate a verified schematic from the description.");
        ImGui::Dummy(ImVec2(0, 6 * sc));
        ImGui::TextColored(v4(pal.textDisabled),
            "examples:  inverting amp gain 10    RC low-pass 1kHz    555 astable");
        ImGui::Unindent(24 * sc);
        return;
    }

    // ---- header toolbar: doc badge + name + revision (left); view controls (right) ----
    ImGui::AlignTextToFramePadding();
    ImGui::TextColored(v4(pal.accent), "%s", g.mirror.isSch() ? "SCHEMATIC" : "PCB LAYOUT");
    ImGui::SameLine(0, 12 * sc);
    ImGui::TextColored(v4(pal.text), "%s",
                       g.mirror.name().empty() ? "(untitled)" : g.mirror.name().c_str());
    ImGui::SameLine(0, 8 * sc);
    ImGui::TextColored(v4(pal.textDim), "rev %d", g.mirror.revision());
    ImGui::SameLine(ImGui::GetWindowContentRegionMax().x - 270 * sc);
    if (ImGui::SmallButton("Undo")) ecadUndoRun(st);
    ImGui::SameLine();
    if (ImGui::SmallButton("Redo")) ecadRedoRun(st);
    ImGui::SameLine(0, 12 * sc);
    if (ImGui::SmallButton("Zoom Fit")) g.canvas.requestFit();
    ImGui::SameLine();
    ImGui::Checkbox(g.mirror.isPcb() ? "Layers" : "Nets", &g.showLeft);

    ImGui::Separator();

    // ---- keyboard history: Ctrl+Z = undo, Ctrl+Y / Ctrl+Shift+Z = redo ----
    // Gated on the ECAD window (incl. its child windows) being focused so it never collides with the
    // CAD doc's global Ctrl+Z. Skip while a text field has focus.
    if (ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows) &&
        !ImGui::GetIO().WantTextInput) {
        if (ImGui::IsKeyChordPressed(ImGuiMod_Ctrl | ImGuiMod_Shift | ImGuiKey_Z) ||
            ImGui::IsKeyChordPressed(ImGuiMod_Ctrl | ImGuiKey_Y)) {
            ecadRedoRun(st);
        } else if (ImGui::IsKeyChordPressed(ImGuiMod_Ctrl | ImGuiKey_Z)) {
            ecadUndoRun(st);
        }
    }

    // ---- canvas tool toolbar: Select / Place / Route + the part to place ----
    {
        auto toolBtn = [&](const char* label, int tool) {
            const bool on = (g.tool == tool);
            if (on) ImGui::PushStyleColor(ImGuiCol_Button, v4(pal.accentDim));
            if (ImGui::Button(label)) { g.tool = tool; g.routing = false; }
            if (on) ImGui::PopStyleColor();
            ImGui::SameLine();
        };
        toolBtn("Select", 0);
        toolBtn("Place", 1);
        toolBtn("Route", 2);
        ImGui::TextColored(v4(pal.textDim), "|");
        ImGui::SameLine();
        if (g.tool == 1 && g.mirror.isSch()) {
            static const char* kLibs[] = {"Device:R", "Device:C", "Device:L", "Device:D",
                "Simulation_SPICE:V_source", "power:GND", "power:VCC"};
            ImGui::SetNextItemWidth(210 * sc);
            if (ImGui::BeginCombo("Part", g.placeLib.c_str())) {
                for (auto* l : kLibs)
                    if (ImGui::Selectable(l, g.placeLib == l)) g.placeLib = l;
                ImGui::EndCombo();
            }
        } else if (g.tool == 1) {
            static const char* kFps[] = {"Resistor_SMD:R_0603", "Capacitor_SMD:C_0603",
                "Package_SO:SOIC-8", "Package_TO_SOT_SMD:SOT-23"};
            ImGui::SetNextItemWidth(250 * sc);
            if (ImGui::BeginCombo("Footprint", g.placeFp.c_str())) {
                for (auto* f : kFps)
                    if (ImGui::Selectable(f, g.placeFp == f)) g.placeFp = f;
                ImGui::EndCombo();
            }
        } else {
            ImGui::TextColored(v4(pal.textDim), g.tool == 2
                ? "Route: click points; Esc / right-click to finish"
                : "Select: click a part to edit; middle/right-drag to pan, wheel to zoom");
        }
        // Native simulator controls (schematic only).
        if (g.mirror.isSch()) {
            ImGui::SameLine(ImGui::GetWindowContentRegionMax().x - 240 * sc);
            ImGui::SetNextItemWidth(110 * sc);
            const char* kAnalyses[] = {"Transient", "AC"};
            ImGui::Combo("##ecadAnalysis", &g.simAnalysis, kAnalyses, 2);
            ImGui::SameLine();
            if (ImGui::Button("Simulate", ImVec2(100 * sc, 0))) ecadSimulateRun(st);
        }
    }
    if (g.tool == 0) drawPropInspector(st, sc);
    ImGui::Separator();

    const bool hasSimNow = g.mirror.isSch() && g.simResults.is_object() && !g.simResults.empty();
    const float bottomH = (hasSimNow ? 260.0f : 140.0f) * sc;
    const ImVec2 avail = ImGui::GetContentRegionAvail();
    const float mainH = avail.y - bottomH;

    if (g.showLeft) {
        ImGui::BeginChild("##ecadLeft", ImVec2(g.leftW * sc, mainH), ImGuiChildFlags_Border);
        drawLeftPanel();
        ImGui::EndChild();
        ImGui::SameLine();
    }

    ImGui::BeginChild("##ecadCanvasHost",
                      ImVec2(0, mainH), ImGuiChildFlags_Border,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    const ImVec2 canvasAvail = ImGui::GetContentRegionAvail();
    g.canvas.setSelection(g.tool == 0 ? g.selected : std::string());
    g.canvas.setDragOffset(g.dragDx, g.dragDy);
    EcadCanvasIO io = g.canvas.draw(g.mirror, canvasAvail);
    handleCanvasEdit(st, io);
    if (io.hasWorldMouse) {
        char buf[64];
        std::snprintf(buf, sizeof(buf), "%.2f, %.2f mm", io.worldMouse.x, io.worldMouse.y);
        ImVec2 p = ImGui::GetWindowPos();
        ImGui::GetWindowDrawList()->AddText(
            ImVec2(p.x + 8, p.y + ImGui::GetWindowHeight() - 20), pal.toU32(pal.textDim), buf);
    }
    ImGui::EndChild();

    ImGui::BeginChild("##ecadStrip", ImVec2(0, 0), ImGuiChildFlags_Border);
    const bool haveSim = g.mirror.isSch() && g.simResults.is_object() && !g.simResults.empty();
    if (haveSim && ImGui::BeginTabBar("##ecadBottomTabs")) {
        if (ImGui::BeginTabItem(g.mirror.isSch() ? "ERC" : "DRC")) {
            drawResultsStrip(st);
            ImGui::EndTabItem();
        }
        ImGuiTabItemFlags simFlags = ImGuiTabItemFlags_None;
        if (g.lastSimResults != g.simResults) {
            simFlags = ImGuiTabItemFlags_SetSelected;
            g.lastSimResults = g.simResults;
            g.lastAnalysis = g.simAnalysis;
        }
        if (ImGui::BeginTabItem("Simulation", nullptr, simFlags)) {
            drawSimPlot(g.simResults, g.lastAnalysis);
            ImGui::EndTabItem();
        }
        ImGui::EndTabBar();
    } else {
        drawResultsStrip(st);
    }
    ImGui::EndChild();
}

}  // namespace ui
