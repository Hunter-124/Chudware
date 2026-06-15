// AI-CAD desktop GUI — branded, themed, docked, with AI chat + provider settings.
//
// run_gui(): GLFW/ImGui init, the dockspace host, the per-frame panel calls (title bar, Design,
// Viewport, Console, status bar, Settings/About modals), the worker-event drain + job-completion
// handling, the headless screenshot modes, and teardown. The data model, job launcher, and each
// panel's draw code live in core/* and ui/* (see core/app_state.h). Behavior-preserving split of
// the former gui_app.cpp monolith.
#include <glad/gl.h>
//
#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include "imgui.h"
#include "imgui_internal.h"
#include "imgui_impl_glfw.h"
#include "imgui_impl_opengl3.h"
#include "stb_image.h"
#include "stb_image_write.h"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <string>
#include <vector>

#include "bootstrap.h"
#include "mesh.h"
#include "renderer.h"
#include "ui_theme.h"
#include "win_chrome.h"
#include "worker_client.h"

#include "render/id_pass.h"
#include "render/overlays.h"

#include "core/app_state.h"
#include "core/jobs.h"
#include "ui/console_panel.h"
#include "ui/doc_tabs.h"
#include "ui/feature_tree.h"
#include "ui/file_explorer.h"  // Wave 3: File Explorer + workspace/recent helpers
#include "ui/layout.h"        // Wave 1C/1D: buildDefaultLayout + tabbed left pane (drawLeftPane)
#include "ui/legacy_chat.h"
#include "ui/settings_modal.h"
#include "ui/status_bar.h"
#include "ui/title_bar.h"
#include "ui/viewport_panel.h"
#include "ui/ecad/ecad_panel.h"   // Wave N: AI-ECAD 2D editor
#include "ui/ecad/ecad_mirror.h"  // Wave N: complete type for EcadMirror::setDocId
#include "ui/file_dialog.h"       // Wave Y: native Save As / Open dialogs for .ecad+
#include "ui/icons.h"
#include "ui/widgets/ribbon.h"
#include "ui/ribbon_def.h"
#include "ui/property_manager.h"
#include "ui/sketch_mode.h"   // C18
#include "ui/config_panel.h"  // Configurations (SolidWorks ConfigurationManager)
#include "ui/design_table_panel.h"  // Design Table (SolidWorks editable config grid)
#include "ui/cutlist_panel.h"  // Weldment Cut List (SolidWorks Weldment Cut List)
#include "ui/file_dialog.h"   // W14: native Save-As for 2D export (flat-pattern DXF)
#include "ui/toast.h"         // W1-B: toast notifications
#include "ui/cursors.h"       // W1-B: cursor manager
// --- C19: simulation + flow + evaluate UI ---
#include "core/sim_client.h"
#include "ui/sim_panel.h"
#include "ui/evaluate_dialogs.h"
// --- end C19 ---
// --- C22b: Activity panel + prompt editor ---
#include "ui/activity_panel.h"
// --- end C22b ---
// --- C20b: assembly UI ---
#include "ui/asm_panels.h"
// --- end C20b ---
#include "ui/equations_dialog.h"  // Equations / Global Variables editor
#include "ui/scenarios.h"  // scripted screenshot/test scenarios (m1/sketch/asm/fea/evalmass/activity)

#include <filesystem>      // Wave N: ecad export output dir + W14: flat-pattern DXF export

// =====================================================================================
// Wave N: AI-ECAD GUI command wiring. Creates/routes ECAD documents through the worker
// ecad_* RPCs (engine/ecad/rpc.py) and feeds the {revision,kind,tree,compile} responses into
// the editor mirror — the same discipline core/doc_mirror.cpp uses for the CAD doc_* methods.
// See docs/ECAD_UI_INTEGRATION.md §(e). New docs + doc-scoped analysis/exports are live;
// geometry-placing ops (sch_add_symbol, pcb_add_track, ...) still need canvas-click placement.
// =====================================================================================
namespace {

// Feed a worker ECAD ops-response into the editor mirror; track doc id + revision.
void ecadAdopt(AppState& st, const nlohmann::json& res) {
    const std::string id = jstr(res, "doc_id");
    if (!id.empty()) st.ecadDocId = id;
    st.ecadRevision = jint(res, "revision");
    if (!st.ecadDocId.empty()) ui::ecadEditorMirror().setDocId(st.ecadDocId);
    ui::ecadEditorLoadResponse(res);
}

// Blocking ECAD RPC; toasts on transport or {code,message} error. Returns false on failure.
bool ecadCall(AppState& st, const char* method, const nlohmann::json& params, nlohmann::json& res) {
    std::string err;
    if (!st.worker.call(method, params, res, err)) {
        st.toast(ui::ToastLevel::Error, std::string("ECAD ") + method + ": " + err);
        return false;
    }
    if (res.is_object() && res.contains("code")) {
        st.toast(ui::ToastLevel::Error, "ECAD: " + jstr(res, "message"));
        return false;
    }
    return true;
}

void ecadNewDoc(AppState& st, const char* kind) {
    nlohmann::json res;
    if (!ecadCall(st, "ecad_new", {{"kind", kind}}, res)) return;
    // Wave 2 (GUI rework): open a first-class Schematic/PCB tab for the new doc and make it active
    // BEFORE adopting, so ecadAdopt's mirror load (ecadEditorLoadResponse) lands in THIS tab's
    // per-document EcadEditorState (the ecad_panel state() resolver targets the active ECAD tab).
    const std::string id = jstr(res, "doc_id");
    const int rev = jint(res, "revision");
    st.addEcadTab(kind, id, rev, /*title=*/std::string());
    ecadAdopt(st, res);
    st.addLog(std::string("[ecad] new ") + kind + " doc " + st.ecadDocId);
    st.toast(ui::ToastLevel::Info,
             std::string("New ECAD ") + (std::string(kind) == "sch" ? "schematic" : "board"));
    ImGui::SetWindowFocus("ECAD");   // bring the editor tab forward
}

// A doc-scoped RPC that returns an ops-response (rebuild / autoroute / fill_zones) -> refresh.
void ecadDocRpc(AppState& st, const char* method, const char* okMsg) {
    if (st.ecadDocId.empty()) { st.toast(ui::ToastLevel::Warn, "Open an ECAD document first"); return; }
    nlohmann::json res;
    if (!ecadCall(st, method, {{"doc_id", st.ecadDocId}}, res)) return;
    ecadAdopt(st, res);
    if (okMsg) st.toast(ui::ToastLevel::Info, okMsg);
}

// Apply an op batch to the current ECAD doc (canvas place/route edits) with the optimistic-
// concurrency + stale-revision-resync discipline core/doc_mirror.cpp uses for the CAD doc.
bool ecadApplyOps(AppState& st, const nlohmann::json& ops) {
    if (st.ecadDocId.empty() || !ops.is_array() || ops.empty()) return false;
    nlohmann::json res; std::string err;
    nlohmann::json params = {{"doc_id", st.ecadDocId}, {"base_revision", st.ecadRevision}, {"ops", ops}};
    if (!st.worker.call("ecad_apply_ops", params, res, err)) {
        st.toast(ui::ToastLevel::Error, "ECAD edit failed: " + err);
        return false;
    }
    if (res.is_object() && res.contains("code")) {
        if (jstr(res, "code") == "stale_revision") {
            nlohmann::json tr; std::string e2;
            if (st.worker.call("ecad_tree", {{"doc_id", st.ecadDocId}}, tr, e2) &&
                !(tr.is_object() && tr.contains("code"))) ecadAdopt(st, tr);
            st.toast(ui::ToastLevel::Warn, "ECAD edit dropped (stale) - resynced");
        } else {
            st.toast(ui::ToastLevel::Error, "ECAD: " + jstr(res, "message"));
        }
        return false;
    }
    ecadAdopt(st, res);
    return true;
}

// Undo / Redo the current ECAD doc via the worker's history (ecad_undo / ecad_redo). Both return an
// ops-response ({revision,kind,tree,compile}); we adopt it into the editor mirror like any other
// mutation so the canvas + revision badge reflect the new history position. No-op without a doc.
void ecadUndo(AppState& st) {
    if (st.ecadDocId.empty()) return;
    nlohmann::json res; std::string err;
    if (!st.worker.call("ecad_undo", {{"doc_id", st.ecadDocId}}, res, err)) {
        st.toast(ui::ToastLevel::Error, "ECAD undo: " + err);
        return;
    }
    if (res.is_object() && res.contains("code")) {
        st.toast(ui::ToastLevel::Warn, "ECAD undo: " + jstr(res, "message"));
        return;
    }
    ecadAdopt(st, res);
    st.toast(ui::ToastLevel::Info, "Undo");
}

void ecadRedo(AppState& st) {
    if (st.ecadDocId.empty()) return;
    nlohmann::json res; std::string err;
    if (!st.worker.call("ecad_redo", {{"doc_id", st.ecadDocId}}, res, err)) {
        st.toast(ui::ToastLevel::Error, "ECAD redo: " + err);
        return;
    }
    if (res.is_object() && res.contains("code")) {
        st.toast(ui::ToastLevel::Warn, "ECAD redo: " + jstr(res, "message"));
        return;
    }
    ecadAdopt(st, res);
    st.toast(ui::ToastLevel::Info, "Redo");
}

void ecadExport(AppState& st, const char* kind) {
    if (st.ecadDocId.empty()) { st.toast(ui::ToastLevel::Warn, "Open an ECAD document first"); return; }
    std::error_code ec;
    const std::string dir = appdataDir() + "\\AI-CAD\\ecad_out";
    std::filesystem::create_directories(dir, ec);
    nlohmann::json res;
    if (!ecadCall(st, "ecad_export", {{"doc_id", st.ecadDocId}, {"kind", kind}, {"path", dir}}, res))
        return;
    st.toast(ui::ToastLevel::Info, std::string("Exported ") + kind + " -> ecad_out");
    st.addLog("[ecad] export " + std::string(kind) + " -> " + dir);
}

// Save the open ECAD document to a native `.ecad+` container (ZIP) via a Save As dialog; the worker's
// ecad_save writes through engine.ecad.formats. Reuses the doc's current name as the suggested file.
void ecadSaveDoc(AppState& st) {
    if (st.ecadDocId.empty()) { st.toast(ui::ToastLevel::Warn, "Design or open an ECAD document to save"); return; }
    const std::string name = ui::ecadEditorMirror().name();
    const std::string path = ui::saveFileDialog(
        "AI-ECAD design (*.ecad+)\0*.ecad+\0All files (*.*)\0*.*\0", "ecad+",
        name.empty() ? "design" : name.c_str());
    if (path.empty()) return;  // cancelled
    nlohmann::json res;
    if (!ecadCall(st, "ecad_save", {{"doc_id", st.ecadDocId}, {"path", path}}, res)) return;
    const std::string saved = jstr(res, "path");
    st.toast(ui::ToastLevel::Info, "Saved " + (saved.empty() ? path : saved));
    st.addLog("[ecad] saved -> " + (saved.empty() ? path : saved));
}

// Open an ECAD document by absolute path (Wave 3: the File Explorer / Recent route here for
// .ecad+/.kicad_sch/.kicad_pcb files). The loaded doc adopts into the editor mirror and surfaces.
void ecadOpenPath(AppState& st, const std::string& path) {
    if (path.empty()) return;
    nlohmann::json res;
    if (!ecadCall(st, "ecad_open", {{"path", path}}, res)) return;
    ecadAdopt(st, res);          // sets ecadDocId/revision + loads the editor mirror
    st.wantFocusEcad = 6;        // surface the editor with the freshly opened doc
    st.toast(ui::ToastLevel::Info, "Opened " + path);
    st.addLog("[ecad] opened " + path);
    st.pushRecentFile(path);     // Wave 3: record in Recent (persists to config.json)
}

// Open an ECAD document (native .ecad+ / .ecad / .json, or a KiCad .kicad_sch/.kicad_pcb which the
// worker imports) via an Open dialog. The loaded doc adopts into the editor mirror and surfaces.
void ecadOpenDoc(AppState& st) {
    const std::string path = ui::openFileDialog(
        "ECAD / KiCad files\0*.ecad+;*.ecad;*.json;*.kicad_sch;*.kicad_pcb\0"
        "AI-ECAD design (*.ecad+)\0*.ecad+\0KiCad schematic (*.kicad_sch)\0*.kicad_sch\0"
        "KiCad board (*.kicad_pcb)\0*.kicad_pcb\0All files (*.*)\0*.*\0");
    if (path.empty()) return;  // cancelled
    ecadOpenPath(st, path);
}

// The headline integration: export the current board to STEP, then IMPORT it into a live CAD part
// document shown in the 3D viewport — the starting point for designing an enclosure/housing around
// the board with the existing modeling + interference tools.
void ecadTo3d(AppState& st) {
    if (st.ecadDocId.empty()) { st.toast(ui::ToastLevel::Warn, "Open an ECAD board first"); return; }
    std::error_code ec;
    const std::string dir = appdataDir() + "\\AI-CAD\\ecad_out";
    std::filesystem::create_directories(dir, ec);
    const std::string step = dir + "\\board.step";
    nlohmann::json res;
    if (!ecadCall(st, "ecad_to_3d", {{"doc_id", st.ecadDocId}, {"path", step}}, res)) return;
    std::string stepPath = jstr(res, "step_path");
    if (stepPath.empty()) stepPath = step;

    // Bring the board into the CAD viewport as an `import` feature on a fresh part doc.
    std::string err;
    if (!st.docMirror.newPart(st.worker, err)) {
        st.toast(ui::ToastLevel::Error, "ECAD 3D: new CAD part failed: " + err);
        return;
    }
    st.clearViewportForNewDoc();
    nlohmann::json ops = nlohmann::json::array();
    ops.push_back({{"op", "add_feature"}, {"feature", {
        {"type", "import"}, {"name", "PCB"},
        {"params", {{"source_path", stepPath}, {"format", "step"}}}}}});
    auto r = st.docMirror.applyOps(st.worker, ops);
    if (r.ok && !r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    st.wantFocusViewport = 6;            // surface the 3D view (force for a few frames so it sticks)
    st.toast(ui::ToastLevel::Info, r.ok ? "Board imported into 3D - design the housing around it"
                                        : "Board exported to STEP (CAD import failed)");
    st.addLog("[ecad] board -> 3D part: " + stepPath);
}

// AI-design an ECAD schematic from a natural-language request (the launchpad / "AI Design" command):
// run the orchestrator (ecad_design) on the worker, then load the produced schematic into the editor.
void ecadAiDesign(AppState& st, const std::string& request) {
    if (request.empty()) return;
    st.toast(ui::ToastLevel::Info, "AI: designing \"" + request + "\" ...");
    st.addLog("[ecad] AI design: " + request);
    nlohmann::json res;
    if (!ecadCall(st, "ecad_design", {{"request", request}}, res)) return;
    const std::string id = jstr(res, "doc_id");
    if (id.empty()) { st.toast(ui::ToastLevel::Warn, "AI design produced no document"); return; }
    nlohmann::json tree;
    if (ecadCall(st, "ecad_tree", {{"doc_id", id}}, tree)) ecadAdopt(st, tree);
    const std::string recipe = jstr(res, "recipe_used");
    st.toast(ui::ToastLevel::Info,
             recipe.empty() ? "AI design ready" : ("AI design ready (recipe: " + recipe + ")"));
}

// One-click "AI Board": NL request -> AI designs a schematic -> a board is created, populated from
// the schematic's netlist (real footprints + pads + bound nets), given an auto-sized rectangular
// outline, and autorouted. The finished board is loaded into the ECAD editor. Each step is a blocking
// worker RPC mirroring ecadAiDesign's shape; every ops-response is adopted into the editor mirror so
// st.ecadDocId/revision track the board and the canvas reflects each stage.
void ecadAiBoard(AppState& st, const std::string& request) {
    if (request.empty()) return;
    st.toast(ui::ToastLevel::Info, "AI Board: designing \"" + request + "\" ...");
    st.addLog("[ecad] AI board: " + request);

    // a. Design the schematic.
    nlohmann::json res;
    if (!ecadCall(st, "ecad_design", {{"request", request}}, res)) return;
    const std::string schId = jstr(res, "doc_id");
    if (schId.empty()) { st.toast(ui::ToastLevel::Warn, "AI board: design produced no schematic"); return; }
    st.addLog("[ecad] AI board: schematic " + schId);

    // b. New empty PCB doc (ecadAdopt tracks st.ecadDocId = pcbId + revision).
    nlohmann::json pcbRes;
    if (!ecadCall(st, "ecad_new", {{"kind", "pcb"}}, pcbRes)) return;
    ecadAdopt(st, pcbRes);
    const std::string pcbId = st.ecadDocId;
    if (pcbId.empty()) { st.toast(ui::ToastLevel::Warn, "AI board: new board failed"); return; }

    // c. Populate the board from the schematic netlist (places footprints with bound nets). The
    //    response carries the populated board tree -> adopt so the mirror has the footprints.
    nlohmann::json upd;
    if (!ecadCall(st, "ecad_update_from_sch", {{"board_doc_id", pcbId}, {"sch_doc_id", schId}}, upd))
        return;
    ecadAdopt(st, upd);

    // d. Auto-outline: axis-aligned bbox over every footprint pad's world position (inflated by the
    //    pad half-extent so pads sit inside the edge), padded by ~5mm, applied as a 4-line rectangle.
    {
        const auto& fps = ui::ecadEditorMirror().footprints();
        bool any = false;
        float minX = 1e30f, minY = 1e30f, maxX = -1e30f, maxY = -1e30f;
        for (const auto& fp : fps) {
            for (const auto& pad : fp.pads) {
                const float hx = 0.5f * pad.w, hy = 0.5f * pad.h;
                minX = std::min(minX, pad.pos.x - hx); maxX = std::max(maxX, pad.pos.x + hx);
                minY = std::min(minY, pad.pos.y - hy); maxY = std::max(maxY, pad.pos.y + hy);
                any = true;
            }
            if (fp.pads.empty()) {  // footprint with no parsed pads -> fall back to its origin
                minX = std::min(minX, fp.pos.x); maxX = std::max(maxX, fp.pos.x);
                minY = std::min(minY, fp.pos.y); maxY = std::max(maxY, fp.pos.y);
                any = true;
            }
        }
        if (any) {
            const float m = 5.0f;  // edge margin (mm)
            minX -= m; minY -= m; maxX += m; maxY += m;
            nlohmann::json shapes = nlohmann::json::array({
                {{"type","line"},{"p1",{minX,minY}},{"p2",{maxX,minY}}},
                {{"type","line"},{"p1",{maxX,minY}},{"p2",{maxX,maxY}}},
                {{"type","line"},{"p1",{maxX,maxY}},{"p2",{minX,maxY}}},
                {{"type","line"},{"p1",{minX,maxY}},{"p2",{minX,minY}}}});
            nlohmann::json ops = nlohmann::json::array();
            ops.push_back({{"op","pcb_set_outline"},{"closed",true},{"shapes",shapes}});
            ecadApplyOps(st, ops);  // adopts the result (tracks revision)
            st.addLog("[ecad] AI board: outline " +
                      std::to_string((int)(maxX - minX)) + "x" + std::to_string((int)(maxY - minY)) + "mm");
        } else {
            st.addLog("[ecad] AI board: no footprints - skipping outline");
        }
    }

    // e. Autoroute the ratsnest.
    nlohmann::json route;
    if (ecadCall(st, "ecad_autoroute", {{"doc_id", pcbId}}, route)) ecadAdopt(st, route);

    // f. Final tree refresh -> ensure the editor shows the finished board.
    nlohmann::json tree;
    if (ecadCall(st, "ecad_tree", {{"doc_id", pcbId}}, tree)) ecadAdopt(st, tree);

    st.wantFocusEcad = 6;  // surface the ECAD tab so the routed board is visible
    st.toast(ui::ToastLevel::Info, "AI board ready - Board to 3D builds the enclosure");
    st.addLog("[ecad] AI board ready: " + pcbId);
}

// Native circuit simulation (ecad_simulate): build an analysis study from st.ecadSimAnalysis
// (0=Transient, 1=AC), run it on the worker, and stash the results payload so the ECAD plot panel
// renders the waveforms. Only schematic docs simulate.
void ecadSimulate(AppState& st) {
    if (st.ecadDocId.empty()) { st.toast(ui::ToastLevel::Warn, "Open a schematic to simulate"); return; }
    nlohmann::json study;
    if (st.ecadSimAnalysis == 1) {
        study = {{"analyses", {{ {"type", "ac"}, {"sweep", "dec"}, {"points", 20},
                                 {"fstart", 10.0}, {"fstop", 1e6} }} }};
    } else {
        study = {{"analyses", {{ {"type", "tran"}, {"tstep", 5e-5}, {"tstop", 5e-3} }} }};
    }
    nlohmann::json res; std::string err;
    if (!st.worker.call("ecad_simulate", {{"doc_id", st.ecadDocId}, {"study", study}}, res, err)) {
        st.toast(ui::ToastLevel::Error, std::string("Simulate failed: ") + err);
        return;
    }
    if (!jbool(res, "ok")) {
        st.toast(ui::ToastLevel::Error, "Simulate: " + jstr(res, "error"));
        return;
    }
    st.ecadSimResults = res.contains("results") ? res["results"] : nlohmann::json::object();
    const bool converged = jbool(res, "converged");
    if (converged) {
        st.toast(ui::ToastLevel::Info, st.ecadSimAnalysis == 1 ? "Simulated (AC)" : "Simulated (transient)");
    } else {
        st.toast(ui::ToastLevel::Warn, "Simulation ran, but convergence warnings need review");
    }
    std::string line = "[ecad] simulate " + std::string(st.ecadSimAnalysis == 1 ? "ac" : "tran") +
                       (converged ? " converged" : " not converged");
    if (res.contains("convergence")) line += " " + res["convergence"].dump();
    if (res.contains("ms") && !res["ms"].is_null()) line += " " + res["ms"].dump() + "ms";
    st.addLog(line);
    if (!converged && res.contains("log") && res["log"].is_array() && !res["log"].empty()) {
        st.addLog("[ecad] sim log: " + res["log"].back().dump());
    }
}

// Route a PCB-tab ribbon command. New docs + doc-scoped analysis/exports run now; geometry-placing
// ops need canvas-click placement / a PM form (logged so they stay observable).
void ecadDispatch(AppState& st, const std::string& cmd) {
    if (cmd == "ecad_ai_design")     { st.wantFocusEcad = 6; return; }  // surface the AI launchpad
    if (cmd == "ecad_new_sch")       { ecadNewDoc(st, "sch"); return; }
    if (cmd == "ecad_new_pcb")       { ecadNewDoc(st, "pcb"); return; }
    if (cmd == "ecad_open")          { ecadOpenDoc(st); return; }   // Wave Y: open .ecad+/KiCad
    if (cmd == "ecad_save")          { ecadSaveDoc(st); return; }   // Wave Y: save .ecad+
    if (cmd == "ecad_run_erc")       { ecadDocRpc(st, "ecad_rebuild", "ERC updated"); return; }
    if (cmd == "ecad_run_drc")       { ecadDocRpc(st, "ecad_rebuild", "DRC updated"); return; }
    if (cmd == "ecad_simulate")      { st.ecadSimGo = true; st.wantFocusEcad = 4; return; }
    if (cmd == "ecad_autoroute")     { ecadDocRpc(st, "ecad_autoroute", "Autoroute complete"); return; }
    if (cmd == "ecad_fill_zones")    { ecadDocRpc(st, "ecad_fill_zones", "Copper zones filled"); return; }
    if (cmd == "ecad_to_3d")         { ecadTo3d(st); return; }
    if (cmd == "ecad_export_gerber") { ecadExport(st, "gerber"); return; }
    if (cmd == "ecad_export_drill")  { ecadExport(st, "drill"); return; }
    if (cmd == "ecad_export_bom")    { ecadExport(st, "bom"); return; }
    if (cmd == "sch_annotate") {
        if (st.ecadDocId.empty()) {
            st.toast(ui::ToastLevel::Warn, "Open a schematic to annotate");
            return;
        }
        bool ok = ecadApplyOps(st, nlohmann::json::array({
            {{"op", "sch_annotate"}, {"scope", "all"}, {"mode", "reset"}}
        }));
        if (ok) {
            st.toast(ui::ToastLevel::Info, "Schematic annotated");
            st.wantFocusEcad = 4;
        }
        return;
    }
    // Canvas tool modes (Wave Q): place a part / route on click.
    if (cmd == "sch_add_symbol" || cmd == "pcb_add_footprint") {
        st.ecadTool = 1; st.wantFocusEcad = 4;
        st.toast(ui::ToastLevel::Info, "Place tool: click the canvas to drop the part");
        return;
    }
    if (cmd == "sch_add_wire" || cmd == "pcb_add_track") {
        st.ecadTool = 2; st.wantFocusEcad = 4;
        st.toast(ui::ToastLevel::Info, "Route tool: click points; Esc or right-click to finish");
        return;
    }
    st.addLog("[ecad] " + cmd + " (interactive placement pending)");
    st.toast(ui::ToastLevel::Info, "ECAD: place/edit on the canvas (interactive tools coming)");
}

}  // namespace

// W16: apply the param-less sm_flat_pattern feature (Sheet Metal "Flatten") + reload the mesh.
// One-click feature (no PM form) — add_feature then loadDocMesh from the ops-response, like the
// feature-tree op helper. The DocMirror listener refreshes the FeatureManager automatically.
static void applyFlatPattern(AppState& st) {
    if (!st.docMirror.hasDoc()) {
        st.toast(ui::ToastLevel::Info, "Open a sheet metal part first");
        return;
    }
    nlohmann::json ops = nlohmann::json::array({
        {{"op", "add_feature"}, {"feature", {{"type", "sm_flat_pattern"}, {"name", "Flat-Pattern"},
                                             {"params", nlohmann::json::object()}}}}});
    auto r = st.docMirror.applyOps(st.worker, ops);
    if (r.ok && !r.aicm_path.empty()) {
        std::string le;
        st.loadDocMesh(r.aicm_path, le);
        st.toast(ui::ToastLevel::Info, "Flat pattern created");
    } else if (!r.ok) {
        st.toast(ui::ToastLevel::Warn,
                 r.error_message.empty() ? std::string("Flatten failed") : r.error_message);
    }
}

// W14: export the ACTIVE sheet-metal part's flat pattern to DXF (outline on layer OUTLINE, bend
// lines on layer BEND). One-click Sheet Metal command (no PM form) — picks a path (native Save-As,
// or cwd fallback when dialogs are disabled) then calls export_flat_dxf via DocMirror. The engine
// computes the flat (fresh compile) and errors cleanly if the part has no flat pattern.
static void exportFlatDxf(AppState& st) {
    if (!st.docMirror.hasDoc()) {
        st.toast(ui::ToastLevel::Info, "Open a sheet metal part first");
        return;
    }
    // Suggested file name from the doc title.
    std::string title = st.docMirror.tree().is_object()
                            ? st.docMirror.tree().value("name", std::string("part"))
                            : std::string("part");
    std::string base;
    for (char c : title) {
        if (c == ' ') base += '_';
        else if (std::isalnum((unsigned char)c) || c == '_' || c == '-') base += c;
    }
    if (base.empty()) base = "part";
    base += "_flat";

    const bool dialogsDisabled = std::getenv("AI_CAD_NO_FILE_DIALOGS") != nullptr;
    std::string path;
    if (dialogsDisabled) {
        path = (std::filesystem::current_path() / (base + ".dxf")).string();
    } else {
        const char* filter = "DXF (*.dxf)\0*.dxf\0PDF (*.pdf)\0*.pdf\0All files (*.*)\0*.*\0";
        path = ui::saveFileDialog(filter, "dxf", (base + ".dxf").c_str());
        if (path.empty()) return;  // canceled
    }
    // Format follows the chosen extension (export_flat_dxf accepts dxf|pdf; defaults to the ext).
    std::string fmt = "dxf";
    if (path.size() >= 4) {
        std::string tail = path.substr(path.size() - 4);
        for (auto& ch : tail) ch = (char)std::tolower((unsigned char)ch);
        if (tail == ".pdf") fmt = "pdf";
    }

    std::string err;
    std::string written = st.docMirror.exportFlat(st.worker, path, fmt, err);
    if (!written.empty()) {
        st.addLog("[sm] exported flat " + fmt + ": " + written);
        st.toast(ui::ToastLevel::Info, "Exported " + written);
    } else {
        st.addLog("[sm] export flat failed: " + err);
        st.toast(ui::ToastLevel::Warn,
                 err.empty() ? std::string("Export Flat DXF failed") : err);
    }
}

// Wave-5-fill: File > Export <fmt> — export the ACTIVE part/assembly's bodies to a 3D format
// engine.doc.formats.export_document accepts ("step"|"iges"|"stl"|"3mf"|"brep"). Picks a path
// (native Save-As, or a cwd fallback when dialogs are disabled) then calls export_file via
// DocMirror. Mirrors exportFlatDxf's shape. Called only from the GL-thread request pump (the menu
// sets st.requestExportFormat) so the native dialog never re-enters an ImGui menu walk.
static void exportFile3d(AppState& st, const std::string& fmt) {
    if (!st.docMirror.hasDoc()) {
        st.toast(ui::ToastLevel::Info, "Open a part or assembly first");
        return;
    }
    // Per-format extension + native-dialog filter.
    const char* ext = "step";
    const char* filter = "STEP (*.step)\0*.step;*.stp\0All files (*.*)\0*.*\0";
    if (fmt == "iges")      { ext = "iges"; filter = "IGES (*.iges)\0*.iges;*.igs\0All files (*.*)\0*.*\0"; }
    else if (fmt == "stl")  { ext = "stl";  filter = "STL (*.stl)\0*.stl\0All files (*.*)\0*.*\0"; }
    else if (fmt == "3mf")  { ext = "3mf";  filter = "3MF (*.3mf)\0*.3mf\0All files (*.*)\0*.*\0"; }
    else if (fmt == "brep") { ext = "brep"; filter = "BREP (*.brep)\0*.brep\0All files (*.*)\0*.*\0"; }

    // Suggested base name from the doc title (alnum/_/-, spaces -> _).
    std::string title = st.docMirror.tree().is_object()
                            ? st.docMirror.tree().value("name", std::string("part"))
                            : std::string("part");
    std::string base;
    for (char c : title) {
        if (c == ' ') base += '_';
        else if (std::isalnum((unsigned char)c) || c == '_' || c == '-') base += c;
    }
    if (base.empty()) base = "part";

    const bool dialogsDisabled = std::getenv("AI_CAD_NO_FILE_DIALOGS") != nullptr;
    std::string path;
    if (dialogsDisabled) {
        path = (std::filesystem::current_path() / (base + "." + ext)).string();
    } else {
        path = ui::saveFileDialog(filter, ext, (base + "." + ext).c_str());
        if (path.empty()) return;  // canceled
    }

    std::string err;
    std::string written = st.docMirror.exportFile(st.worker, st.docMirror.docId(), path, fmt, err);
    if (!written.empty()) {
        st.addLog("[export] " + fmt + ": " + written);
        st.toast(ui::ToastLevel::Info, "Exported " + written);
    } else {
        st.addLog("[export] " + fmt + " failed: " + err);
        st.toast(ui::ToastLevel::Warn,
                 err.empty() ? ("Export " + fmt + " failed") : err);
    }
}

// ---- Wave 3A item 5: OS file drop (Windows Explorer -> our window) --------------------------
// GLFW delivers OS file drops to a static callback. We reach AppState via the window user pointer
// (set once in run_gui after AppState is constructed) and route the FIRST dropped path with a known
// CAD/ECAD extension to the GL-thread request pump (st.requestOpenPath), exactly like the File
// Explorer's double-click / drag-to-viewport. Routing CAD vs ECAD by extension is done by the pump.
//
// Behavior on a MULTI-file drop: open the first valid file and toast that the rest were ignored
// (the pump is single-shot per frame; opening a doc swaps the active tab, so silently opening N
// docs would be surprising). Unknown extensions are skipped; if none are openable we toast.
//
// We CHAIN the ImGui-GLFW backend's drop callback (captured at install time) so ImGui still sees the
// drop too (it does nothing harmful with it, but chaining keeps the backend contract intact).
static GLFWdropfun g_prevDropCallback = nullptr;

static bool dropPathHasKnownExt(const std::string& path) {
    std::string ext = std::filesystem::path(path).extension().string();
    std::transform(ext.begin(), ext.end(), ext.begin(),
                   [](unsigned char c) { return (char)std::tolower(c); });
    return ext == ".step+" || ext == ".step" || ext == ".stp" || ext == ".iges" || ext == ".igs" ||
           ext == ".stl"   || ext == ".3mf"  || ext == ".ecad+" || ext == ".ecad" ||
           ext == ".kicad_sch" || ext == ".kicad_pcb";
}

static void onFileDrop(GLFWwindow* win, int count, const char** paths) {
    if (g_prevDropCallback) g_prevDropCallback(win, count, paths);   // keep ImGui's backend in the loop
    AppState* stp = static_cast<AppState*>(glfwGetWindowUserPointer(win));
    if (!stp || count <= 0 || !paths) return;
    AppState& st = *stp;
    std::string first;
    int known = 0;
    for (int i = 0; i < count; ++i) {
        if (!paths[i]) continue;
        if (dropPathHasKnownExt(paths[i])) {
            ++known;
            if (first.empty()) first = paths[i];
        }
    }
    if (first.empty()) {
        st.toast(ui::ToastLevel::Warn, "Drop a CAD or ECAD file to open it");
        return;
    }
    // Route the first valid file through the same deferred pump the menu/Explorer use.
    st.requestOpenPath = first;
    st.addLog("[dnd] OS drop -> " + first);
    if (known > 1)
        st.toast(ui::ToastLevel::Info,
                 "Opening " + std::filesystem::path(first).filename().string() +
                 " (dropped " + std::to_string(known) + " files; opening the first)");
}

int run_gui(const char* screenshotPath) {
    const bool shot = screenshotPath != nullptr;
    if (!glfwInit()) return 1;
    glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 3);
    glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 3);
    glfwWindowHint(GLFW_OPENGL_PROFILE, GLFW_OPENGL_CORE_PROFILE);
    if (!shot) glfwWindowHint(GLFW_MAXIMIZED, GLFW_TRUE);
    GLFWwindow* win = glfwCreateWindow(shot ? 1340 : 1440, shot ? 838 : 900, "AI-CAD", nullptr, nullptr);
    if (!win) { glfwTerminate(); return 1; }
    glfwMakeContextCurrent(win);
    glfwSwapInterval(1);
    if (!gladLoadGL((GLADloadfunc)glfwGetProcAddress)) return 1;

    const std::string assets = exeDir() + "\\assets";
    setWindowIcon(win, assets + "\\logo_window.png");

    IMGUI_CHECKVERSION();
    ImGui::CreateContext();
    ImGuiIO& io = ImGui::GetIO();
    io.ConfigFlags |= ImGuiConfigFlags_DockingEnable;
    static std::string iniPath = appdataDir() + "\\AI-CAD\\imgui.ini";
    io.IniFilename = shot ? nullptr : iniPath.c_str();

    float xs = 1.0f, ys = 1.0f;
    glfwGetWindowContentScale(win, &xs, &ys);
    float scale = shot ? 1.0f : (xs > ys ? xs : ys);
    // AI_CAD_SCALE overrides the content scale (incl. in screenshot mode) so HiDPI layout can be
    // reproduced/verified headlessly. Clamped to a sane range.
    if (const char* envScale = std::getenv("AI_CAD_SCALE")) {
        float v = (float)std::atof(envScale);
        if (v >= 0.5f && v <= 4.0f) scale = v;
    }

    // Theme: honour AI_CAD_THEME env override, then persisted setting, then dark default.
    // Applied once here; runtime toggle in the View menu calls applyTheme() again.
    {
        const char* envTheme = std::getenv("AI_CAD_THEME");
        ui::Theme initTheme = ui::Theme::Dark;
        if (envTheme && std::strcmp(envTheme, "light") == 0) initTheme = ui::Theme::Light;
        ui::applyTheme(initTheme, scale);
    }
    ui::Fonts fonts = ui::loadFonts(assets, scale);
    ImGui_ImplGlfw_InitForOpenGL(win, true);
    ImGui_ImplOpenGL3_Init("#version 330");

    // Borderless window: strip the OS title bar/frame and drive our own (drag/resize/snap
    // stay native). Must be installed after the ImGui GLFW backend so our WndProc is on top.
    static chrome::HitLayout hitLayout;
    hitLayout.resizeBorder = (int)(6 * scale);
    chrome::install(win, &hitLayout);

    Tex wordmark = loadTexture(assets + "\\logo_wordmark.png");
    Tex wordmarkDark = loadTexture(assets + "\\logo_wordmark_dark.png");
    Tex logoIcon = loadTexture(assets + "\\logo_icon.png");

    std::string rerr;
    Renderer renderer;
    renderer.init(960, 720, rerr);
    // Viewport visuals driven by palette roles so they switch with the theme. These are cheap
    // uniform/state sets re-pushed every frame in the main loop (see below) so a runtime theme
    // toggle takes effect immediately; this initial push covers the first headless frame.
    {
        float top3[3] = {ui::palette.viewportTop[0], ui::palette.viewportTop[1],
                         ui::palette.viewportTop[2]};
        float mid3[3] = {ui::palette.viewportMid[0], ui::palette.viewportMid[1],
                         ui::palette.viewportMid[2]};
        float bot3[3] = {ui::palette.viewportBottom[0], ui::palette.viewportBottom[1],
                         ui::palette.viewportBottom[2]};
        renderer.setBackground3(top3, mid3, bot3);
        renderer.setEdgeStyle(ui::palette.viewportEdge[0], ui::palette.viewportEdge[1],
                              ui::palette.viewportEdge[2], 1.6f * ui::uiScale);
        renderer.setDefaultPartColor(ui::palette.partDefault[0], ui::palette.partDefault[1],
                                     ui::palette.partDefault[2]);
        renderer.setGroundShadow(ui::palette.groundShadow[0], ui::palette.groundShadow[1],
                                 ui::palette.groundShadow[2], ui::palette.groundShadow[3]);
        renderer.setPlaneColors(ui::palette.planeFill, ui::palette.planeOutline);
    }

    // C14: load icon atlases (stb_image; must be after GL context is live).
    ui::loadIcons(assets);

    // W1-B: create the GLFW standard cursors once (consumed per-frame via st.requestCursor).
    ui::initCursors(win);

    // C17: the ribbon callback (PM dispatcher) is registered after AppState is created so it can
    // capture &st and the PropertyManager (see below). C14's default printf handler is replaced.

    // Topology picking (C15): id pass + overlays live for the GUI's lifetime; AppState borrows
    // them. They stay inert until a doc mesh (AICM) is loaded. Highlight colors track theme roles.
    render::IdPass idPass;
    render::Overlays overlays;
    overlays.setColors(ui::palette.hoverHighlight, ui::palette.selectionHighlight);

    // ---------- application state (bundles what were run_gui locals) ----------
    AppState st;
    st.win = win;
    st.shot = shot;
    st.scale = scale;
    st.assets = assets;
    st.fonts = fonts;
    st.wordmark = wordmark;
    st.wordmarkDark = wordmarkDark;
    st.logoIcon = logoIcon;
    st.renderer = &renderer;
    st.hitLayout = &hitLayout;
    st.idPass = &idPass;
    st.overlays = &overlays;

    // Wave 3A item 5: OS file drop. Expose AppState to the static drop callback via the GLFW window
    // user pointer (unused elsewhere), then install onFileDrop. glfwSetDropCallback returns the
    // previously-installed callback (ImGui's GLFW backend registered one during InitForOpenGL); we
    // chain to it from onFileDrop so the backend contract is preserved.
    glfwSetWindowUserPointer(win, &st);
    g_prevDropCallback = glfwSetDropCallback(win, onFileDrop);

    // C17: PropertyManager — owned here, borrowed via st.propertyManager (opaque void*).
    ui::PropertyManager propertyManager;
    st.propertyManager = &propertyManager;

    // C19: SimClient (worker #2) + Simulation panel + Evaluate dialogs — owned here.
    core::SimClient simClient;
    st.simClient = &simClient;
    ui::SimPanel simPanel;
    st.simPanel = &simPanel;
    ui::EvaluateDialogs evaluateDialogs;

    // C18: SketchMode — owned here, borrowed via st.sketchMode (opaque void*).
    ui::SketchMode sketchMode;
    st.sketchMode = &sketchMode;

    // Configurations panel (SolidWorks ConfigurationManager) — owned here. The Configurations
    // left-pane tab renders it (ConfigPanel::drawEmbedded) via st.configPanel; there is no
    // standalone dock window for it anymore.
    ui::ConfigPanel configPanel;
    st.configPanel = &configPanel;

    // Design Table panel (SolidWorks editable config grid) — owned here, drawn each frame.
    ui::DesignTablePanel designTablePanel;

    // Weldment Cut List panel (SolidWorks Weldment Cut List) — owned here, drawn each frame.
    ui::CutListPanel cutListPanel;

    // --- C22b: ActivityPanel — owned here, borrowed via st.activityPanel (opaque void*). ---
    ui::ActivityPanel activityPanel;
    st.activityPanel = &activityPanel;
    // --- end C22b ---

    // --- C20b: AsmPanels — owned here, borrowed via st.asmPanels (opaque void*). ---
    ui::AsmPanels asmPanels;
    st.asmPanels = &asmPanels;
    // --- end C20b ---

    // C17/C18: SINGLE ribbon-command dispatcher. The ribbon callback and st.ribbonDispatch (used by
    // the shortcut bar / gesture wheel) route through the SAME lambda so the two paths can never
    // drift apart — they were previously hand-duplicated (Wave 1B unification).
    //   - sketch-tab commands (sketch_new / sk_* / smart_dim / sketch_exit) go to SketchMode first
    //     so the sketch experience takes priority over any PM form for those ids;
    //   - commands without a form log a stub line so they stay observable until later cards wire them.
    auto dispatchCmd = [&st, &propertyManager, &sketchMode, &simPanel,
                        &evaluateDialogs, &asmPanels](const char* cmd_id) {
        // C43: starting a sketch while a PM form is open cancels the form first (revert
        // preview + close). Without this, plane-pick and the PM coexisted: the PM hid the
        // FeatureManager, both fought over the viewport mouse, and Esc couldn't untangle it.
        if (propertyManager.active() && std::strcmp(cmd_id, "sketch_new") == 0)
            propertyManager.cancelEdits(st);
        if (sketchMode.onRibbonCommand(st, cmd_id)) return;       // C18 sketch tool handled
        // Wave 4C/4D: Edit-menu copy / cut / paste. The menu bar fires these ids; they route exactly
        // like the global Ctrl+C/X/V keys (sketch first when active, else the FeatureManager). Copy
        // routed AFTER onRibbonCommand so an armed sketch tool doesn't swallow "edit_copy" — these
        // ids are never sketch-tool ids, so onRibbonCommand returns false for them.
        if (std::strcmp(cmd_id, "edit_copy") == 0) {
            if (sketchMode.active()) { if (sketchMode.sketchHasSelection()) sketchMode.sketchCopySelection(st); }
            else ui::featureCopySelection(st);
            return;
        }
        if (std::strcmp(cmd_id, "edit_cut") == 0) {
            // Sketch mode has no cut (Delete removes entities); cut applies to the FeatureManager.
            if (!sketchMode.active()) ui::featureCutSelection(st);
            return;
        }
        if (std::strcmp(cmd_id, "edit_paste") == 0) {
            if (sketchMode.active() &&
                st.clipboard.kind == AppState::Clipboard::Kind::SketchEntities)
                sketchMode.sketchPasteBegin(st);
            else if (st.clipboard.kind == AppState::Clipboard::Kind::Feature)
                ui::featurePasteSelection(st);
            return;
        }
        // Wave-5-fill: Edit > Delete / Select All. Like cut/copy/paste these route through the SAME
        // lambda the menu shares. featureDeleteSelection applies a DocMirror op immediately; that is
        // safe here because the menu bar draws BEFORE the FeatureManager tree walk this frame, so no
        // tree-json iteration is live. Both stand down while a sketch is active (SketchMode owns the
        // sketch-entity Del / marquee); with no sketch they act on the FeatureManager selection.
        if (std::strcmp(cmd_id, "edit_delete") == 0) {
            if (!sketchMode.active()) ui::featureDeleteSelection(st);
            return;
        }
        if (std::strcmp(cmd_id, "edit_select_all") == 0) {
            if (!sketchMode.active()) ui::featureSelectAll(st);
            return;
        }
        // DRAWINGS: "Make Drawing" creates a drawing + standard views from the active part and
        // opens it as a Drawing tab (guarded; toasts if no part is open).
        if (std::strcmp(cmd_id, "make_drawing") == 0) { st.makeDrawing(); return; }
        // C20b: Assembly tab + New Assembly route to the assembly panels first.
        {
            std::string cmd(cmd_id);
            if ((cmd.rfind("asm_", 0) == 0 || cmd == "new_assembly") &&
                asmPanels.handleCommand(st, cmd)) return;
        }
        // Wave N: AI-ECAD commands (ecad_/sch_/pcb_) route to the ECAD editor.
        {
            std::string ec(cmd_id);
            if (ec.rfind("ecad_", 0) == 0 || ec.rfind("sch_", 0) == 0 || ec.rfind("pcb_", 0) == 0) {
                st.showPcbTools = true;
                ecadDispatch(st, ec); return;
            }
        }
        if (propertyManager.openForCommand(st, cmd_id)) return;  // form opened
        // C19: Simulation tab -> sim panel; Evaluate tab -> evaluate dialogs.
        std::string cmd(cmd_id);
        if (cmd == "sm_flat_pattern") { applyFlatPattern(st); return; }  // W16: one-click Flatten
        if (cmd == "sm_export_flat_dxf") { exportFlatDxf(st); return; }  // W14: flat-pattern DXF
        if (cmd.rfind("sim_", 0) == 0) {
            st.showSimulationTools = true;
            if (simPanel.handleCommand(st, cmd)) return;
        }
        if (cmd.rfind("eval_", 0) == 0 && evaluateDialogs.handleCommand(st, cmd)) return;
        std::printf("[ribbon] %s (no PM form)\n", cmd_id);
        std::fflush(stdout);
        st.addLog(std::string("[ribbon] ") + cmd_id + " (no PM form yet)");
    };
    ui::setRibbonCallback(dispatchCmd);
    // C25: expose the same dispatcher to UI widgets (shortcut bar, gesture wheel).
    st.ribbonDispatch = dispatchCmd;

    loadSettings(st.settings);
    // editor buffer for the system-prompt override (Settings > Agent)
    std::strncpy(st.spBuf.data(), st.settings.systemPrompt.c_str(), st.spBuf.size() - 1);

    // Wave 3 (GUI rework): seed the File Explorer's live state from persisted settings and ensure the
    // workspace directory exists (default <Documents>/AI-CAD). recentFiles/workspaceDir are the live
    // copies the Explorer + File menu read; pushRecentFile() mirrors changes back into settings.
    st.recentFiles  = st.settings.recentFiles;
    st.workspaceDir = st.settings.workspaceDir;
    ui::ensureWorkspaceDir(st);

    // Re-apply theme from persisted settings (env override already applied above; if a
    // saved setting differs, the persisted value wins unless env was set).
    {
        const char* envTheme = std::getenv("AI_CAD_THEME");
        if (!envTheme) {
            // No env override — use the saved setting.
            ui::Theme t = (st.settings.theme == "light") ? ui::Theme::Light : ui::Theme::Dark;
            if (t != ui::activeTheme) ui::applyTheme(t, scale);
        }
    }

    // C16: Selection observer — on every selection change, resolve picks through the worker
    // (doc_resolve_pick) and fill the selector slot in each pending SelItem.
    // Called on the GL thread (selection changes happen there); worker call is synchronous.
    st.selection.setObserver([&st]() {
        if (!st.docMirror.hasDoc() || !st.workerStarted) return;
        for (auto& item : st.selection.mutableItems()) {
            if (!item.pending_resolve || !item.hit.valid()) continue;
            const char* kindStr =
                item.hit.kind == render::PickKind::Face   ? "face"   :
                item.hit.kind == render::PickKind::Edge   ? "edge"   :
                item.hit.kind == render::PickKind::Vertex ? "vertex" : nullptr;
            if (!kindStr) continue;
            std::string err;
            // C20b: while the Mate form is open, assembly picks resolve via the asm path
            // (doc_resolve_pick returns {selector, component}) and route into the active well.
            if (st.asmPanels) {
                auto* ap = st.asmPanels;
                if (ap->mateFormActive()) {
                    std::string comp;
                    std::string asel = st.docMirror.resolvePickAsm(
                        st.worker, item.hit.body_id, kindStr, item.hit.topo_id, comp, err);
                    if (!asel.empty()) {
                        item.selector = asel;
                        item.pending_resolve = false;
                        try {
                            nlohmann::json selObj = nlohmann::json::parse(asel);
                            ap->routeAsmPick(selObj, comp, (int)item.hit.kind);
                        } catch (...) {}
                    }
                    continue;  // handled (or failed) — skip the part-doc resolve path
                }
                // Component pattern form: a pick selects the seed COMPONENT (not a face/edge).
                if (ap->patternFormActive()) {
                    std::string comp;
                    st.docMirror.resolvePickAsm(
                        st.worker, item.hit.body_id, kindStr, item.hit.topo_id, comp, err);
                    if (!comp.empty()) ap->routeAsmPatternPick(comp);
                    continue;  // handled — skip the part-doc resolve path
                }
            }
            // Body wells (boolean targets/tools): resolve the clicked element's OWNING body
            // (doc_resolve_pick kind="body" -> {kind:"body", owner_feature:...}) instead of the
            // face/edge/vertex it landed on, so picking anywhere on a body selects that body.
            const char* resolveKind = kindStr;
            if (st.propertyManager && st.propertyManager->active() &&
                st.propertyManager->activeWellWantsBody())
                resolveKind = "body";
            std::string sel = st.docMirror.resolvePick(
                st.worker, item.hit.body_id, resolveKind, item.hit.topo_id, err);
            if (!sel.empty()) {
                item.selector = sel;
                item.pending_resolve = false;
                // W1-B (item 7): viewport->tree sync. Resolve the pick's owning feature via the
                // DocMirror owner map and select it in the FeatureManager so its row highlights and
                // (Wave-2) scrolls into view. Faces/edges use ownerOfFace/ownerOfEdge; a body pick
                // (or a face whose owner is unknown) falls back to the body's first owner. TOLERANT
                // of an empty owner map (engine hasn't shipped it) — then we leave the tree alone.
                {
                    std::string owner;
                    if (item.hit.kind == render::PickKind::Face)
                        owner = st.docMirror.ownerOfFace(item.hit.body_id, item.hit.topo_id);
                    else if (item.hit.kind == render::PickKind::Edge)
                        owner = st.docMirror.ownerOfEdge(item.hit.body_id, item.hit.topo_id);
                    if (owner.empty()) {
                        auto owners = st.docMirror.ownerOfBody(item.hit.body_id);
                        if (!owners.empty()) owner = owners.back();  // most-recent feature on the body
                    }
                    if (!owner.empty()) {
                        st.selectedFeatureId = owner;
                        st.treeScrollToSelection = true;
                    }
                }
                // C17: route the freshly resolved pick into the active PropertyManager well
                // (filtered by the well's allowed kinds). Consumed picks do not affect the
                // viewport highlight set beyond their normal selection role.
                if (st.propertyManager) {
                    auto* pm = st.propertyManager;
                    if (pm->active()) {
                        try {
                            nlohmann::json selObj = nlohmann::json::parse(sel);
                            // Feedback when a pick is rejected for the wrong topology (the active
                            // well wants a different kind) — otherwise the click is silently lost.
                            if (!pm->routePick(selObj, (int)item.hit.kind)) {
                                std::string hint = pm->activeWellKindHint();
                                if (!hint.empty())
                                    st.toast(ui::ToastLevel::Info,
                                             "This field takes " + hint + " — that pick wasn't added");
                            }
                        } catch (...) {}
                    }
                }
            } else if (!err.empty()) {
                // Log once (avoid flooding; resolvePick logs stale_revision internally).
            }
        }
    });

    // C16: Doc mirror listener — re-draws the tree whenever a mutation lands.
    // (Tree reads st.docMirror.tree() directly; listener fires on the calling thread.)
    st.docMirror.setListener([&st]() {
        // Nothing to do beyond triggering a redraw; ImGui redraws every frame so
        // the tree panel auto-updates from docMirror.tree() on the next frame.
        // We log the new revision for observability.
        st.addLog("[doc] tree updated  rev=" + std::to_string(st.docMirror.revision()));
        // C38 (T1): this is the single central choke point — DocMirror::notify() fires here after
        // EVERY mutation (applyOps, resync, undo, redo, newPart) once the rebuilt owner map is
        // stored. Re-derive the feature highlight from the fresh owner map so the tinted faces never
        // go stale after a topology rebuild retags them. (docUndo/docRedo also refresh explicitly,
        // which is redundant-but-harmless; this listener covers the ops/resync paths.)
        ui::refreshFeatureHighlight(st);
        // Document tabs: a real edit (applyOps/undo/redo/pushPull/revert) lights the active tab's
        // unsaved-changes marker. Tab switches / fresh-doc activation / resyncs set suppressTabDirty
        // so they don't falsely dirty a tab. Keep the cached title in step with renames, too.
        if (!st.suppressTabDirty) {
            st.markActiveTabDirty();
            st.syncActiveTabTitle();
        }
    });

    // W1-B (item 2/11): route otherwise-silent failures into the toast system.
    //  - WorkerClient transport errors (timeout / closed pipe) from ANY call site (77 of them),
    //    incl. ones owned by other modules — they all share this one client.
    //  - DocMirror stale_revision auto-resync — the user's action was discarded; tell them.
    st.worker.setErrorSink([&st](const std::string& method, const std::string& err) {
        st.toast(ui::ToastLevel::Error, "Worker error (" + method + "): " + err);
    });
    st.docMirror.setStaleResyncCallback([&st]() {
        st.toast(ui::ToastLevel::Warn, "Model changed — action was discarded, try again");
    });

    // ---------- runtime + worker ----------
    st.rtReady = runtimeReady();

    if (st.rtReady) {
        st.tryStartWorker();
    } else {
        st.status = "Runtime not installed";
        st.statusKind = 2;
        st.addLog("[!] runtime not found at %APPDATA%\\AI-CAD\\runtime");
        st.autoSetupTried = true;
        st.startSetup(false);  // self-provision from the payload next to the exe / source tree
    }

    // NOTE: the old "welcome model" (mounting_plate.stl into the bare renderer) is gone — it
    // contradicted the empty Part1 document (a mesh with no tree features), occluded the
    // plane-pick quads, and inflated the empty-scene camera radius. Startup now shows the
    // clean empty-doc state: origin planes + triad, like a fresh SolidWorks part.

    std::strncpy(st.code.data(), kStarterCode, st.code.size() - 1);
    std::strncpy(st.prompt.data(), "a knurled hex standoff, 22mm tall, M3 clearance bore", st.prompt.size() - 1);
    {
        ChatMsg w;
        w.text = "Hi! Describe a part and I'll model it in build123d, render it, and check the "
                 "result - then keep refining it with you. Set your AI provider in Settings first.";
        st.chat.push_back(w);
    }

    const char* shotMode = std::getenv("AI_CAD_SHOT");  // screenshot-only: settings|chat|assembly|plan|ribbon|pmform
    st.wantSettings = shot && shotMode && std::strcmp(shotMode, "settings") == 0;
    st.wantChat = shot && shotMode && std::strcmp(shotMode, "chat") == 0;
    st.wantAssembly = shot && shotMode && std::strcmp(shotMode, "assembly") == 0;
    st.wantPlan = shot && shotMode && std::strcmp(shotMode, "plan") == 0;
    st.wantPick = shot && shotMode && std::strcmp(shotMode, "pick") == 0;
    st.wantRibbon = shot && shotMode && std::strcmp(shotMode, "ribbon") == 0;
    st.wantTree = shot && shotMode && std::strcmp(shotMode, "tree") == 0;
    // Wave 3: explorer shot — force the tabbed left pane to the Explorer (File Explorer) tab so the
    // headless screenshot captures the workspace file tree (otherwise it defaults to FeatureManager).
    if (shot && shotMode && std::strcmp(shotMode, "explorer") == 0)
        st.leftPaneTab = AppState::LP_Explorer;
    // Equations editor shot — seed two globals + open the dialog once the worker is ready.
    st.wantEquations = shot && shotMode && std::strcmp(shotMode, "equations") == 0;
    if (st.wantEquations) { st.showFeatureTree = true; }  // keep the FeatureManager (Equations node) visible
    st.wantM1 = shot && shotMode && std::strcmp(shotMode, "m1") == 0;  // C17 integration scenario
    st.wantEcad = shot && shotMode && std::strcmp(shotMode, "ecad") == 0;  // Wave N: ECAD GUI shot
    if (st.wantEcad) { st.showPcbTools = true; st.showRibbon = true; st.ribbonTab = AppState::RT_PCB; }  // PCB tab forward
    st.wantSketch = shot && shotMode && std::strcmp(shotMode, "sketch") == 0;  // C18 sketch flow
    if (st.wantSketch) { st.showRibbon = true; st.ribbonTab = AppState::RT_Sketch; }  // Sketch tab forward
    st.wantFea = shot && shotMode && std::strcmp(shotMode, "fea") == 0;       // C19 static study
    st.wantEvalMass = shot && shotMode && std::strcmp(shotMode, "evalmass") == 0;  // C19 mass props
    if (st.wantFea) {
        st.showSimulationTools = true;
        st.showRibbon = true;
        st.ribbonTab = AppState::RT_Simulation;
        st.showSimPanel = true;
    }
    // C22b: activity shot — new part doc -> agent_run_v2 -> Activity panel -> Settings Agents tab
    st.wantActivity = shot && shotMode && std::strcmp(shotMode, "activity") == 0;
    if (st.wantActivity) { st.showActivityPanel = true; }
    // C20b: assembly shot — author base/peg parts, new assembly, insert + mate + interference.
    st.wantAsm = shot && shotMode && std::strcmp(shotMode, "asm") == 0;
    // AI_CAD_MATEFORM: a lightweight Mate-form-only variant of the asm shot for visual verification
    // of the mate PM (Standard + Mechanical/Advanced sections + the type-aware param block). It
    // skips the heavy author/insert/mate state machine and just opens the Mate form so the generic
    // frame-6 capture grabs it (mirrors the pmform scenario). AI_CAD_MATETYPE=<type> preselects a
    // mechanical/advanced type (e.g. "gear") so its parameter field renders.
    const bool wantMateForm = st.wantAsm && std::getenv("AI_CAD_MATEFORM") != nullptr;
    if (st.wantAsm) {
        st.showRibbon = true; st.ribbonTab = AppState::RT_Assembly;  // Assembly tab forward
        st.showActivityPanel = false;            // keep the FeatureManager (Mates folder) unobscured
        if (wantMateForm) {
            st.wantAsm = false;                  // suppress the scripted scenario; just show the form
            asmPanels.openMateForm();
            if (const char* mt = std::getenv("AI_CAD_MATETYPE"))
                if (*mt) asmPanels.presetMateTypeForShot(mt);
        }
    }
    // ribbon shot: show Features tab by default; AI_CAD_TAB=<0..8> selects which tab to capture
    // (0=Features 1=Sketch 2=Evaluate 3=Simulation 4=Assembly 5=Sheet Metal 6=Weldments 7=PCB
    // 8=Surfaces) for headless per-tab verification. Upper bound tracks kRibbonTabCount-1.
    if (st.wantRibbon) {
        st.showRibbon = true; st.ribbonTab = AppState::RT_Features;
        if (const char* tabEnv = std::getenv("AI_CAD_TAB")) {
            int t = std::atoi(tabEnv);
            if (t >= 0 && t <= 8) st.ribbonTab = t;
        }
    }
    // pmform shot: open a PropertyManager form headlessly for visual verification of its field
    // layout/sizing. AI_CAD_PMCMD=<cmd_id> (e.g. "extrude", "surf_loft") selects which form; the
    // left pane auto-switches to the PropertyManager tab when a form is active (see left_pane.cpp).
    // openForCommand is safe pre-worker: with no doc the checkpoint is skipped and field init is pure.
    if (shot && shotMode && std::strcmp(shotMode, "pmform") == 0) {
        st.showRibbon = true;
        st.showFeatureTree = true;  // ensure the left pane (which hosts the PM tab) is visible
        if (const char* cmd = std::getenv("AI_CAD_PMCMD"))
            if (*cmd && !propertyManager.openForCommand(st, cmd))
                std::fprintf(stderr, "pmform: no form for command '%s'\n", cmd);
        // AI_CAD_PMSET="key=value;key2=value2" presets fields so type-gated rows render
        // (e.g. "type=face" to show a fillet's face-set wells, "end=up_to_surface", "thin=true").
        if (const char* setEnv = std::getenv("AI_CAD_PMSET")) {
            std::string spec = setEnv;
            size_t pos = 0;
            while (pos < spec.size()) {
                size_t semi = spec.find(';', pos);
                std::string tok = spec.substr(pos, semi - pos);
                size_t eq = tok.find('=');
                if (eq != std::string::npos)
                    propertyManager.presetFieldForShot(tok.substr(0, eq), tok.substr(eq + 1));
                if (semi == std::string::npos) break;
                pos = semi + 1;
            }
        }
    }
    // wantTree doc smoke runs in the frame loop once the worker is ready (see below).
    if (st.wantPick) {
        // C15 picking demo/verification: load the golden box AICM, assert its topology counts,
        // and (in the frame loop) simulate a center-screen pick + highlight. The AICM path can be
        // overridden via AI_CAD_AICM; otherwise we probe the repo fixture relative to the exe.
        std::string aicm;
        if (const char* env = std::getenv("AI_CAD_AICM")) aicm = env;
        if (aicm.empty()) {
            const char* candidates[] = {
                "docs\\contracts\\fixtures\\box.aicm",
                "..\\docs\\contracts\\fixtures\\box.aicm",
                "..\\..\\docs\\contracts\\fixtures\\box.aicm",
                "..\\..\\..\\docs\\contracts\\fixtures\\box.aicm",
                "..\\..\\..\\..\\docs\\contracts\\fixtures\\box.aicm",
            };
            for (const char* c : candidates) {
                FILE* f = std::fopen(c, "rb");
                if (f) { std::fclose(f); aicm = c; break; }
            }
        }
        std::string lerr;
        if (aicm.empty() || !st.loadDocMesh(aicm, lerr)) {
            std::fprintf(stderr, "pick: failed to load box.aicm (%s): %s\n",
                         aicm.empty() ? "not found" : aicm.c_str(), lerr.c_str());
        } else {
            // Assert + print golden counts (mesh_format.md §4): 6 faces / 12 edges / 8 verts.
            render::DocMesh probe;
            std::string perr;
            render::loadAicmFile(aicm, probe, perr);
            const auto& b = probe.bodies.front();
            std::printf("aicm: bodies=%zu name=%s verts=%u tris=%u faces=%zu edges=%zu brep_verts=%zu\n",
                        probe.bodies.size(), b.name.c_str(), b.n_verts, b.n_tris,
                        b.faces.size(), b.edges.size(), b.brep_verts.size());
            if (probe.bodies.size() != 1 || b.faces.size() != 6 || b.edges.size() != 12 ||
                b.brep_verts.size() != 8 || b.n_verts != 24 || b.n_tris != 12) {
                std::fprintf(stderr, "pick: golden box.aicm counts mismatch!\n");
            }
            st.yaw = 35; st.pitch = 28; st.zoom = 1.0f;  // iso so a face faces the camera
        }
    }
    if (st.wantPlan) {
        // The moment plan mode shines: the plan card is drafted and visible while the
        // first code pass is still being written.
        ChatMsg u;
        u.fromUser = true;
        u.text = "an M3 hex standoff, 30mm tall";
        st.chat.push_back(u);
        ChatMsg a;
        a.running = true;
        a.liveStatus = "Build plan ready - writing the CAD code...";
        a.plan.present = true;
        a.plan.name = "hex standoff";
        a.plan.summary = "extruded hex prism with a through bore, chamfered top rim";
        PlanPart pp;
        pp.name = "standoff";
        pp.purpose = "spaces a PCB off its mounting plate";
        pp.dims = "across_flats 10, height 30, bore_radius 1.7";
        pp.features = {"M3 clearance bore r=1.7 through - cut a Cylinder",
                       "1mm chamfer on the top rim - chamfer(top_edges, length=1)"};
        a.plan.parts.push_back(pp);
        st.chat.push_back(a);
    }
    if (st.wantAssembly) {
        // Demo a 3-part fit-test: base plate, lid above it, peg through both (clash on the
        // peg). Populates the viewport parts panel + a chat card so --ui-screenshot shows the
        // assembly UI. A real color-coded render is shown if its cache PNG exists.
        const std::string asmDir = appdataDir() + "\\AI-CAD\\cache\\screenshot_asm\\";
        st.asmStl = asmDir + "screenshot_asm_assembly.stl";
        std::string sheet = asmDir + "screenshot_asm_grid.png";
        // Per-part STLs (generated once for the demo) so the viewport shows the bodies
        // color-coded; missing files just fall back to the combined mesh.
        st.asmParts = {{"base", asmDir + "screenshot_asm_base.stl", "40.0 x 30.0 x 8.0 mm", 9600.0},
                    {"lid", asmDir + "screenshot_asm_lid.stl", "40.0 x 30.0 x 8.0 mm", 9600.0},
                    {"peg", asmDir + "screenshot_asm_peg.stl", "6.0 x 6.0 x 30.0 mm", 848.2}};
        st.asmFit.present = true;
        st.asmFit.ok = false;
        st.asmFit.summary = "2 interfering pair(s); largest base<->peg = 226.2 mm^3";
        st.asmFit.interferences = {{"base", "peg", 226.2}, {"lid", "peg", 226.2}};
        st.asmFit.clearances = {{"base", "lid", 10.0}};
        st.showPartMesh(-1);  // show the combined assembly mesh if its STL was pre-generated
        ChatMsg u; u.fromUser = true;
        u.text = "a base plate, a lid 10mm above it, and a peg through both";
        st.chat.push_back(u);
        ChatMsg a;
        a.text = "Built a 3-part assembly. The peg passes through both plates - tell me if that "
                 "press-fit is intended, or I'll trim it to clear.";
        AgentStep s;
        s.index = 0; s.ok = true; s.done = false; s.isAssembly = true;
        s.dims = "40.0 x 30.0 x 38.0 mm";
        s.parts = st.asmParts;
        s.fit = st.asmFit;
        s.contactSheet = sheet;   // color-coded render (loaded if present)
        s.notes = "base and lid sit 10mm apart; the peg interferes with both - intended as a "
                  "press-fit, or should I add radial clearance?";
        a.steps.push_back(s);
        st.chat.push_back(a);
    }
    if (st.wantChat) {
        ChatMsg u; u.fromUser = true; u.text = "make it 30mm tall with a chamfered top edge";
        st.chat.push_back(u);
        ChatMsg a;
        a.text = "Raised the standoff to 30mm and chamfered the top rim. Ask for another tweak "
                 "and I'll revise this same part.";
        // The plan-mode card the agent drafts before writing code.
        a.plan.present = true;
        a.plan.name = "hex standoff";
        a.plan.summary = "extruded hex prism with a through bore, chamfered top rim";
        {
            PlanPart pp;
            pp.name = "standoff";
            pp.purpose = "spaces a PCB off its mounting plate";
            pp.dims = "across_flats 10, height 30, bore_radius 1.7";
            pp.features = {"M3 clearance bore r=1.7 through - cut a Cylinder",
                           "1mm chamfer on the top rim - chamfer(top_edges, length=1)"};
            a.plan.parts.push_back(pp);
        }
        // A self-corrected build issue first, so the calm amber styling is visible.
        AgentStep f;
        f.index = 0; f.ok = false; f.done = false;
        f.error = "Traceback (most recent call last):\n"
                  "  File \"engine\\kernel.py\", line 361, in _exec_model\n"
                  "    exec(compile(code, \"<model>\", \"exec\"), g)\n"
                  "  File \"<model>\", line 9, in <module>\n"
                  "TypeError: chamfer() got an unexpected keyword argument 'radius'";
        a.steps.push_back(f);
        AgentStep s;
        s.index = 1; s.ok = true; s.done = true;
        s.dims = "22.0 x 19.1 x 30.0 mm  |  8243 mm^3";
        s.notes = "The previous part was 22mm tall. I raised total_h to 30 and added a 1mm "
                  "chamfer on the top edge ring; the bore and across-flats are unchanged.";
        s.thinking = "Looking at the prior render the proportions were right but short. I only "
                     "need to change the height variable and add a chamfer on the top face's "
                     "perimeter edges - everything else should stay parametric.";
        s.code = "af = 11.0\ntotal_h = 30.0   # was 22\nbore_r = 1.7\n"
                 "hex_r = af / (3 ** 0.5)\nface = RegularPolygon(radius=hex_r, side_count=6)\n"
                 "result = extrude(face, amount=total_h)\n"
                 "result -= Cylinder(radius=bore_r, height=total_h)\n"
                 "top = result.edges().filter_by(Plane.XY).group_by(Axis.Z)[-1]\n"
                 "result = chamfer(top, length=1.0)";
        s.diff = "--- previous\n+++ current\n@@ -1,4 +1,6 @@\n af = 11.0\n-total_h = 22.0\n"
                 "+total_h = 30.0   # was 22\n bore_r = 1.7\n+top = result.edges()."
                 "filter_by(Plane.XY).group_by(Axis.Z)[-1]\n+result = chamfer(top, length=1.0)";
        a.steps.push_back(s);
        st.chat.push_back(a);
    }

    // Scripted screenshot/test scenarios (AI_CAD_SHOT=m1/sketch/asm/fea/evalmass/activity): the
    // six step machines + their per-step backbuffer captures, extracted to ui::ScenarioRunner
    // (ui/scenarios.cpp). tick() advances the active scenario before each frame; endFrame()
    // handles the captures and the scenario-driven swap+continue / swap+break at loop bottom.
    ui::ScenarioRunner scenarios(propertyManager, simPanel, evaluateDialogs, activityPanel);

    auto desiredThemeForFrame = [&st]() {
        if (const char* envTheme = std::getenv("AI_CAD_THEME")) {
            if (std::strcmp(envTheme, "light") == 0) return ui::Theme::Light;
            if (std::strcmp(envTheme, "dark") == 0) return ui::Theme::Dark;
        }
        return (st.settings.theme == "light") ? ui::Theme::Light : ui::Theme::Dark;
    };
    auto styleColorMatchesRole = [](const ImVec4& c, const float* role) {
        return std::fabs(c.x - role[0]) < 0.006f &&
               std::fabs(c.y - role[1]) < 0.006f &&
               std::fabs(c.z - role[2]) < 0.006f &&
               std::fabs(c.w - role[3]) < 0.006f;
    };
    auto ensureFrameTheme = [&]() {
        const ui::Theme desired = desiredThemeForFrame();
        const ImVec4* colors = ImGui::GetStyle().Colors;
        const bool staleColors =
            !styleColorMatchesRole(colors[ImGuiCol_WindowBg], ui::palette.windowBg) ||
            !styleColorMatchesRole(colors[ImGuiCol_ChildBg],  ui::palette.panelBg) ||
            !styleColorMatchesRole(colors[ImGuiCol_FrameBg],  ui::palette.frameBg);
        if (desired != ui::activeTheme || staleColors)
            ui::applyTheme(desired, scale);
    };

    while (!glfwWindowShouldClose(win)) {
        glfwPollEvents();

        if (st.setupDone.exchange(false)) {
            bool ok;
            std::string msg;
            {
                std::lock_guard<std::mutex> lk(st.setupMtx);
                ok = st.setupOk;
                msg = st.setupMsg;
            }
            if (ok) {
                st.addLog("[ok] " + msg);
                st.tryStartWorker();
            } else {
                st.addLog("[err] " + msg);
                st.status = "Setup needed";
                st.statusKind = 2;
            }
        }

        // C39: deliver finished async worker calls — their callbacks run HERE, on the GL
        // thread, before this frame's panels draw, so results land like any other state.
        st.rpc.drain();

        // C48: click telemetry for interaction debugging — only when AI_CAD_DEBUG_INPUT is
        // set (pairs with AI_CAD_LOG_FILE). Logs what ImGui thinks is under every click.
        {
            static const bool dbgInput = std::getenv("AI_CAD_DEBUG_INPUT") != nullptr;
            if (dbgInput && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                ImGuiContext* g = ImGui::GetCurrentContext();
                char b[256];
                std::snprintf(b, sizeof(b), "[dbg] click (%.0f,%.0f) win=%s item=0x%08X",
                              ImGui::GetIO().MousePos.x, ImGui::GetIO().MousePos.y,
                              g->HoveredWindow ? g->HoveredWindow->Name : "(none)",
                              g->HoveredIdPreviousFrame);
                st.addLog(b);
            }
        }

        // Drain streamed agent progress into the active (running) assistant message so the
        // conversation updates live, iteration by iteration (texture loads stay on this thread).
        {
            std::vector<AgentEvent> evs;
            {
                std::lock_guard<std::mutex> lk(st.job.evMtx);
                if (!st.job.events.empty()) evs.swap(st.job.events);
            }
            if (!evs.empty()) {
                ChatMsg* active = nullptr;
                for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
                    if (!it->fromUser && it->running) { active = &(*it); break; }
                for (auto& e : evs) {
                    // --- C22b: route orchestrator events to the ActivityPanel ---
                    if (e.kind.rfind("orch_", 0) == 0) {
                        std::string orchKind = e.kind.substr(5);  // strip "orch_"
                        try {
                            nlohmann::json data = nlohmann::json::parse(e.text);
                            if (st.activityPanel) {
                                auto* ap = st.activityPanel;
                                ap->onEvent(orchKind, data);
                            }
                        } catch (...) {}
                        // Also update the compact status line in the chat
                        if (!e.notes.empty()) {
                            st.agentV2Status = e.notes;
                            if (active) active->liveStatus = e.notes;
                        }
                        continue;  // don't process as legacy event
                    }
                    // --- end C22b ---
                    if (e.kind == "status") {
                        st.livePhase = e.text;
                        if (active) active->liveStatus = e.text;
                    } else if (e.kind == "plan") {
                        if (active) active->plan = e.plan;
                        st.livePhase = "Build plan ready - writing the CAD code...";
                    } else if (e.kind == "iteration") {
                        if (active) {
                            AgentStep* st2 = nullptr;
                            for (auto& s : active->steps)
                                if (s.index == e.index) { st2 = &s; break; }
                            if (!st2) { active->steps.emplace_back(); st2 = &active->steps.back(); }
                            st2->index = e.index; st2->ok = e.ok; st2->done = e.done;
                            st2->notes = e.notes; st2->thinking = e.thinking; st2->code = e.code;
                            st2->diff = e.diff; st2->dims = e.dims; st2->error = e.error;
                            st2->contactSheet = e.contactSheet; st2->stdoutText = e.stdoutText;
                            st2->isAssembly = e.isAssembly; st2->parts = e.parts; st2->fit = e.fit;
                            active->liveStatus.clear();
                        }
                        // Live preview: show each successful build in the viewport the moment
                        // it lands, color-coded per part for assemblies (we're on the GL thread).
                        if (e.ok && !e.stlPath.empty()) {
                            st.asmStl = e.stlPath;
                            st.asmParts = e.isAssembly ? e.parts : std::vector<PartInfo>{};
                            st.asmFit = e.isAssembly ? e.fit : FitReport{};
                            st.selectedPart = -1;
                            st.showPartMesh(-1);
                            if (!e.dims.empty()) st.modelInfo = e.dims;
                            st.livePhase = "Iteration " + std::to_string(e.index + 1) +
                                        " built - evaluating the render...";
                        } else if (!e.ok) {
                            st.livePhase = "Iteration " + std::to_string(e.index + 1) +
                                        " - build issue, revising the code...";
                        }
                    }
                }
            }
        }

        if (st.job.ready.exchange(false)) {
            std::lock_guard<std::mutex> lk(st.job.mtx);
            st.livePhase.clear();
            st.agentV2Status.clear();
            // --- C22b: if this was a v2 run, resync the DocMirror tree + reload mesh ---
            // The worker already rebuilt the doc; call doc_rebuild to refresh the tree and
            // get the latest aicm_path, then load it into the viewport.
            const bool wasV2 = st.agentV2Running;
            if (st.agentV2Running) {
                st.agentV2Running = false;
                if (st.docMirror.hasDoc() && st.job.error.empty()) {
                    // C39: doc_rebuild can take tens of seconds on a heavy doc — the old sync
                    // call froze the window right after the agent finished. Queue it async;
                    // the callback (a later frame's drain, GL thread) resyncs and reloads.
                    st.livePhase = "Refreshing model...";
                    st.rpc.call("doc_rebuild", {{"doc_id", st.docMirror.docId()}},
                        [&st](const core::RpcResult& r) {
                            st.livePhase.clear();
                            if (r.ok) {
                                // W1-B (item 6): STALENESS FIX. Reloading the mesh alone left the
                                // FeatureManager showing PRE-run features. Resync (doc_tree — a
                                // fast call) pulls the new tree + revision + owner map and fires
                                // the listener so the tree repaints with what the agent built.
                                st.docMirror.resync(st.worker);
                                // C40: doc_rebuild is an ops-response — the mesh path lives at
                                // tess.aicm_path (the old top-level read was always empty, so
                                // the viewport NEVER refreshed after a v2 agent run).
                                std::string aicmPath;
                                if (r.result.contains("tess") && r.result["tess"].is_object())
                                    aicmPath = r.result["tess"].value("aicm_path", "");
                                if (aicmPath.empty())
                                    aicmPath = r.result.value("aicm_path", "");
                                if (!aicmPath.empty()) {
                                    std::string le;
                                    st.loadDocMesh(aicmPath, le);
                                }
                                st.addLog("[v2] doc_rebuild + resync after run: rev=" +
                                          std::to_string(st.docMirror.revision()));
                            } else {
                                st.addLog("[err] doc_rebuild after v2 run: " + r.err);
                                st.toast(ui::ToastLevel::Error,
                                         "Agent run finished but the model couldn't refresh");
                            }
                        }, 60000);
                }
            }
            // --- end C22b ---
            std::string assistant;
            int assistantErr = ERR_NONE;
            if (!st.job.error.empty() && st.job.errorIsCad) {
                // The CAD script didn't build. That's normal territory for an AI design loop
                // (it usually self-corrects mid-run) — report it calmly, details on demand,
                // and don't dress a modeling problem up as an app crash.
                std::string cause = errSummary(st.job.error);
                st.status = "Build didn't converge";
                st.statusKind = 1;
                st.addLog("[cad] " + cause);
                assistantErr = ERR_CAD;
                if (st.job.kind == JOB_BUILD) {
                    st.buildErrSummary = cause;
                    st.buildErrFull = st.job.error;
                } else {
                    assistant = "I couldn't get this design to build cleanly - the CAD code kept "
                                "failing.\nLast error: " + cause + "\nThis is a modeling problem, "
                                "not an app problem. Try rephrasing or simplifying the request "
                                "(each iteration's Error details has the full trace).";
                }
            } else if (!st.job.error.empty()) {
                st.status = "System error";
                st.statusKind = 2;
                std::string e = st.job.error.substr(0, 300);
                st.addLog("[err] " + e);
                st.toast(ui::ToastLevel::Error, "System error: " + errSummary(st.job.error));  // W1-B
                assistantErr = ERR_SYSTEM;
                // A missing Python module means the venv is incomplete — auto-repair once.
                bool missingModule = st.job.error.find("ModuleNotFoundError") != std::string::npos ||
                                     st.job.error.find("No module named") != std::string::npos;
                if (missingModule && !st.importRepairTried && !st.setupRunning.load()) {
                    st.importRepairTried = true;
                    st.addLog("[..] runtime module missing — auto-repairing (use AI-CAD > Repair runtime if this persists)...");
                    st.startSetup(true);
                    assistant = "A required module is missing from the runtime — repairing now (may take a minute). Try again once the console shows setup complete.";
                } else {
                    assistant = "Something went wrong on the app side (your design is fine):\n" + e;
                    // Point at Settings only for credential-shaped failures; other errors
                    // (connection refused, model missing) carry their own guidance.
                    std::string low = e;
                    for (auto& c : low) c = (char)std::tolower((unsigned char)c);
                    static const char* kAuthMarks[] = {
                        "401", "403", "unauthorized", "forbidden", "api key", "api_key",
                        "apikey", "authentication", "invalid key", "credential"};
                    for (const char* mark : kAuthMarks) {
                        if (low.find(mark) != std::string::npos) {
                            assistant += "\n(This looks like an authentication problem - "
                                         "set your provider/API key in Settings.)";
                            break;
                        }
                    }
                }
            } else if (!st.job.stl.empty()) {
                Mesh m;
                std::string e;
                if (loadBinarySTL(st.job.stl, m, e)) {
                    renderer.setMesh(m);
                    st.status = "Done";
                    st.statusKind = 0;
                    st.modelInfo = st.job.info;
                    st.buildErrSummary.clear();
                    st.addLog("[ok] built  " + st.job.info);
                    // Refresh the parts panel for the new model (cleared for single parts so
                    // the panel disappears), then re-load assemblies color-coded per part.
                    st.asmStl = st.job.stl;
                    st.asmParts = st.job.parts;
                    st.asmFit = st.job.fit;
                    st.selectedPart = -1;
                    if (st.job.isAssembly) st.showPartMesh(-1);
                    if (st.job.isAssembly && st.job.fit.present) {
                        assistant = "Done - " + st.job.info + ".\n" +
                                    (st.job.fit.ok ? "Fit OK - " : "Fit issue - ") + st.job.fit.summary +
                                    "\nSee the Viewport (parts list + fit readout). Ask for a tweak and I'll revise.";
                        st.addLog((st.job.fit.ok ? "[ok] fit: " : "[err] fit: ") + st.job.fit.summary);
                    } else {
                        assistant = "Done - " + st.job.info + ".\nSee the Viewport. Ask for a tweak and I'll revise.";
                    }
                    if ((st.job.kind == JOB_DESIGN || st.job.kind == JOB_CHAT) && !st.job.converged)
                        assistant += "\n(Stopped at the iteration cap - say \"keep going\" "
                                     "and I'll keep refining this same design.)";
                } else {
                    st.status = "STL load failed";
                    st.statusKind = 2;
                    st.addLog("[err] " + e);
                    assistant = "Built it, but couldn't load the mesh: " + e;
                    assistantErr = ERR_SYSTEM;
                }
            } else if (wasV2 && st.job.error.empty()) {
                // C40: a v2 orchestrator run has no job.stl — the doc itself was built. Without
                // this branch the assistant bubble finished EMPTY (the run looked dead even when
                // it succeeded). Summarize from the run result; the tree/viewport refresh lands
                // via the async doc_rebuild above.
                if (st.job.converged) {
                    st.status = "Done";
                    st.statusKind = 0;
                    assistant = "Done - I built this as parametric features; see the FeatureManager "
                                "tree and the viewport. Switch to \"Edit part\" to ask for changes, "
                                "or edit any feature directly (double-click / right-click it).";
                } else {
                    st.status = "Stopped early";
                    st.statusKind = 1;
                    assistantErr = ERR_CAD;
                    std::string why = st.job.stopReason.empty() ? "unknown reason" : st.job.stopReason;
                    assistant = "I stopped before fully finishing (" + why + "). Whatever was built "
                                "so far is in the FeatureManager tree - check the Activity panel "
                                "for the step-by-step log, or rephrase and try again.";
                }
            }
            if (st.job.kind == JOB_DESIGN || st.job.kind == JOB_CHAT) {
                ChatMsg* active = nullptr;
                for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
                    if (!it->fromUser && it->running) { active = &(*it); break; }
                if (active) {
                    active->running = false;
                    active->liveStatus.clear();
                    active->errKind = assistantErr;
                    if (!assistant.empty()) active->text = assistant;
                } else if (!assistant.empty()) {
                    ChatMsg m; m.text = assistant; m.errKind = assistantErr;
                    st.chat.push_back(m);
                }
            }
        }

        // C16 tree shot: run the doc smoke once the worker is ready (fires the first time
        // workerStarted is true after a wantTree shot). Synchronous calls are OK here since
        // we defer the screenshot to frame >= 10 (giving extra frames for the tree to repaint).
        if (st.wantTree && !st.treeShotDone && st.workerStarted) {
            st.treeShotDone = true;
            std::string err;
            if (st.docMirror.newPart(st.worker, err)) {
                std::printf("doc_new: doc_id=%s rev=%d\n",
                            st.docMirror.docId().c_str(), st.docMirror.revision());
                std::fflush(stdout);
                nlohmann::json boxOps = nlohmann::json::array();
                boxOps.push_back({{"op","add_feature"},{"feature",{
                    {"type","box"},{"name","Box1"},{"params",{
                        {"length",{{"value",50.0},{"expr",nullptr}}},
                        {"width", {{"value",40.0},{"expr",nullptr}}},
                        {"height",{{"value",30.0},{"expr",nullptr}}}
                    }}}}});
                auto r1 = st.docMirror.applyOps(st.worker, boxOps);
                std::printf("add_box: ok=%d rev=%d aicm=%s\n",
                            (int)r1.ok, r1.revision, r1.aicm_path.c_str());
                std::fflush(stdout);
                if (r1.ok && !r1.aicm_path.empty()) { std::string le; st.loadDocMesh(r1.aicm_path, le); }
                std::string selStr, serr;
                selStr = st.docMirror.resolvePick(st.worker, 1, "face", 1, serr);
                std::printf("resolve_pick: selector=%s err=%s\n", selStr.c_str(), serr.c_str());
                std::fflush(stdout);
                nlohmann::json edgeRef;
                try { edgeRef = nlohmann::json::parse(
                    R"({"kind":"edge","surf":"line","centroid":[0,0,15],"axis":[0,0,1],"area_or_len":30,"owner_feature":null,"hint_index":0})");
                } catch (...) {}
                if (!edgeRef.is_null()) {
                    nlohmann::json fo = nlohmann::json::array();
                    fo.push_back({{"op","add_feature"},{"feature",{
                        {"type","fillet"},{"name","Fillet1"},
                        {"params",{{"radius",{{"value",3.0},{"expr",nullptr}}}}},
                        {"refs",{{"edges",nlohmann::json::array({edgeRef})}}}}}});
                    auto r2 = st.docMirror.applyOps(st.worker, fo);
                    std::printf("add_fillet: ok=%d rev=%d err=%s\n",
                                (int)r2.ok, r2.revision, r2.error_message.c_str());
                    std::fflush(stdout);
                    if (r2.ok && !r2.aicm_path.empty()) { std::string le; st.loadDocMesh(r2.aicm_path, le); }
                }
                auto ru = st.docMirror.undo(st.worker);
                std::printf("undo: ok=%d rev=%d features=%d\n", (int)ru.ok, ru.revision,
                    (int)(ru.tree.is_object()&&ru.tree.contains("features")&&ru.tree["features"].is_array()
                          ? ru.tree["features"].size() : 0));
                std::fflush(stdout);
                if (ru.ok && !ru.aicm_path.empty()) { std::string le; st.loadDocMesh(ru.aicm_path, le); }
                auto rr = st.docMirror.redo(st.worker);
                std::printf("redo: ok=%d rev=%d features=%d\n", (int)rr.ok, rr.revision,
                    (int)(rr.tree.is_object()&&rr.tree.contains("features")&&rr.tree["features"].is_array()
                          ? rr.tree["features"].size() : 0));
                std::fflush(stdout);
                if (rr.ok && !rr.aicm_path.empty()) { std::string le; st.loadDocMesh(rr.aicm_path, le); }
                st.wantFocusViewport = 6;
            } else {
                std::printf("tree-shot: doc_new failed: %s\n", err.c_str());
                std::fflush(stdout);
            }
        }

        // Equations shot: create a part, seed two globals via eq_set (width=20, height=width*1.5),
        // then open the Equations editor so the dialog renders with rows (mirrors the wantTree flow).
        // Runs once the worker is ready; the deferred screenshot (frame >= 10) gives the dialog
        // a few frames to repaint with the seeded rows.
        if (st.wantEquations && !st.equationsShotDone && st.workerStarted) {
            st.equationsShotDone = true;
            std::string err;
            if (st.docMirror.newPart(st.worker, err)) {
                auto r1 = st.docMirror.applyOps(st.worker, nlohmann::json::array({
                    {{"op", "eq_set"}, {"name", "width"}, {"expr", "20"}}}));
                std::printf("eq_set width: ok=%d rev=%d err=%s\n",
                            (int)r1.ok, r1.revision, r1.error_message.c_str());
                auto r2 = st.docMirror.applyOps(st.worker, nlohmann::json::array({
                    {{"op", "eq_set"}, {"name", "height"}, {"expr", "width*1.5"}}}));
                std::printf("eq_set height: ok=%d rev=%d err=%s\n",
                            (int)r2.ok, r2.revision, r2.error_message.c_str());
                std::fflush(stdout);
                if (r2.ok && !r2.aicm_path.empty()) { std::string le; st.loadDocMesh(r2.aicm_path, le); }
                st.showEquations = true;
            } else {
                std::printf("equations-shot: doc_new failed: %s\n", err.c_str());
                std::fflush(stdout);
            }
        }

        // Wave N: ECAD GUI shot — build a verified design via the worker and load it into the
        // editor mirror so the screenshot shows a live schematic/board (mirrors the wantTree flow).
        // Default shows the schematic (always populated via the orchestrator's recipe fallback);
        // AI_CAD_ECAD_KIND=pcb shows the laid-out + autorouted board instead.
        if (st.wantEcad && !st.ecadShotDone && st.workerStarted) {
            st.ecadShotDone = true;
            auto ecall = [&](const char* m, const nlohmann::json& p, nlohmann::json& out) -> bool {
                std::string e;
                bool ok = st.worker.call(m, p, out, e, 60000);
                std::printf("[ecad-shot] %s ok=%d%s\n", m, (int)ok,
                            (out.is_object() && out.contains("code")) ? " (code-err)" : "");
                std::fflush(stdout);
                return ok && !(out.is_object() && out.contains("code"));
            };
            const char* kindEnv = std::getenv("AI_CAD_ECAD_KIND");
            const bool wantPcb = kindEnv && std::strcmp(kindEnv, "pcb") == 0;
            const bool want3d  = kindEnv && std::strcmp(kindEnv, "3d") == 0;
            const bool wantAi  = kindEnv && std::strcmp(kindEnv, "ai") == 0;
            const bool wantEdit = kindEnv && std::strcmp(kindEnv, "editsch") == 0;
            const bool wantSelect = kindEnv && std::strcmp(kindEnv, "select") == 0;  // selection highlight
            const bool wantDel    = kindEnv && std::strcmp(kindEnv, "del") == 0;     // delete a symbol
            const bool wantSim    = kindEnv && std::strcmp(kindEnv, "sim") == 0;     // simulate + plot
            const bool wantUndo   = kindEnv && std::strcmp(kindEnv, "undo") == 0;    // history undo
            const bool wantAiBoard = kindEnv && std::strcmp(kindEnv, "aiboard") == 0; // Wave U one-click
            const bool wantAiBoard3d = kindEnv && std::strcmp(kindEnv, "aiboard3d") == 0; // Wave X chain
            const bool wantRotate  = kindEnv && std::strcmp(kindEnv, "rotate") == 0;  // Wave W rotate
            const bool wantSaveOpen = kindEnv && std::strcmp(kindEnv, "saveopen") == 0; // Wave Y roundtrip
            const bool wantViolZoom = kindEnv && std::strcmp(kindEnv, "violzoom") == 0; // Wave Z zoom-to-ERC
            nlohmann::json r;
            std::string showId;
            if (wantAiBoard || wantAiBoard3d) {
                // Wave U capstone: drive the full one-click flow (design -> populate -> outline ->
                // autoroute). ecadAiBoard adopts the finished board into the editor mirror itself, so
                // there is no showId to tree-load below; the screenshot captures the routed PCB.
                ecadAiBoard(st, "5V resistive divider with 2.5V output");
                std::printf("[ecad-shot] aiboard: doc=%s footprints=%d tracks(adopted)\n",
                            st.ecadDocId.c_str(), (int)ui::ecadEditorMirror().footprints().size());
                if (wantAiBoard3d) {
                    // Wave X: continue the seamless chain into the CAD 3D viewport -- export the
                    // AI-designed board to STEP and import it as a solid part the user can build a
                    // housing around. ecadTo3d surfaces the Viewport (wantFocusViewport). ecadAiBoard
                    // just requested ECAD focus; cancel it so only the Viewport wins this frame (in
                    // real use these are separate clicks, so the focus never contends).
                    ecadTo3d(st);
                    st.wantFocusEcad = 0;
                    std::printf("[ecad-shot] aiboard3d: exported board -> CAD 3D part\n");
                }
                std::fflush(stdout);
            } else if (wantAi) {
                st.wantFocusEcad = 6;   // surface the empty AI launchpad for the screenshot
            } else if (wantEdit || wantSelect || wantDel || wantRotate || wantViolZoom) {
                // Exercise the Place/Route tools by applying the ops a user's clicks would produce.
                // select/del/rotate/violzoom reuse this schematic batch (it has unconnected pins ->
                // ERC violations with locations) then drive selection/delete/rotate/zoom-to-violation.
                if (ecall("ecad_new", {{"kind", "sch"}}, r)) {
                    const std::string id = jstr(r, "doc_id");
                    const int rev = jint(r, "revision");
                    nlohmann::json ops = nlohmann::json::array();
                    ops.push_back({{"op","sch_add_symbol"},{"libid","Simulation_SPICE:V_source"},{"pos",{10,42}},{"value","5"}});
                    ops.push_back({{"op","sch_add_symbol"},{"libid","Device:R"},{"pos",{32,42}},{"value","10k"}});
                    ops.push_back({{"op","sch_add_symbol"},{"libid","Device:R"},{"pos",{32,22}},{"value","10k"}});
                    ops.push_back({{"op","sch_add_symbol"},{"libid","Device:C"},{"pos",{54,22}},{"value","100n"}});
                    ops.push_back({{"op","sch_add_symbol"},{"libid","power:GND"},{"pos",{32,6}}});
                    ops.push_back({{"op","sch_add_wire"},{"pts",{{10,32},{32,32}}}});
                    ops.push_back({{"op","sch_add_wire"},{"pts",{{32,32},{54,32},{54,26}}}});
                    ecall("ecad_apply_ops", {{"doc_id",id},{"base_revision",rev},{"ops",ops}}, r);
                    st.ecadTool = wantEdit ? 1 : 0;   // editsch shows Place; select/del/rotate use Select
                    showId = id;
                    if (wantDel) {
                        // Delete one symbol directly (what the Delete key emits) so the shot shows
                        // FEWER symbols than the editsch baseline. Re-read the new revision first.
                        const int rev2 = jint(r, "revision");
                        nlohmann::json delOps = nlohmann::json::array();
                        delOps.push_back({{"op","sch_delete_symbol"},{"id","s2"}});  // the 2nd symbol (a 10k R)
                        ecall("ecad_apply_ops", {{"doc_id",id},{"base_revision",rev2},{"ops",delOps}}, r);
                    }
                    if (wantRotate) {
                        // Rotate s2 (a 10k R) by 90 deg — exactly the op the Rotate button / R key
                        // emits — so the shot shows the resistor turned on its side. Re-read revision.
                        const int rev2 = jint(r, "revision");
                        nlohmann::json rotOps = nlohmann::json::array();
                        rotOps.push_back({{"op","sch_edit_symbol"},{"id","s2"},{"angle",90}});
                        ecall("ecad_apply_ops", {{"doc_id",id},{"base_revision",rev2},{"ops",rotOps}}, r);
                    }
                }
            } else if (wantPcb || want3d) {
                // Build a small demo board explicitly: outline + two 0603 footprints (pads
                // resolved from the library) + a routed track + a GND copper pour, then fill it.
                if (ecall("ecad_new", {{"kind", "pcb"}}, r)) {
                    const std::string pcbId = jstr(r, "doc_id");
                    const int rev = jint(r, "revision");
                    nlohmann::json ops = nlohmann::json::array();
                    ops.push_back({{"op","pcb_set_outline"},{"closed",true},{"shapes",
                        nlohmann::json::array({
                            {{"type","line"},{"p1",{0,0}},  {"p2",{40,0}}},
                            {{"type","line"},{"p1",{40,0}}, {"p2",{40,30}}},
                            {{"type","line"},{"p1",{40,30}},{"p2",{0,30}}},
                            {{"type","line"},{"p1",{0,30}}, {"p2",{0,0}}}})}});
                    ops.push_back({{"op","pcb_add_footprint"},{"fpid","Resistor_SMD:R_0603"},
                                   {"pos",{14,15}},{"ref","R1"},{"value","10k"}});
                    ops.push_back({{"op","pcb_add_footprint"},{"fpid","Resistor_SMD:R_0603"},
                                   {"pos",{26,15}},{"ref","R2"},{"value","10k"}});
                    ops.push_back({{"op","pcb_add_track"},{"layer","F.Cu"},{"width",0.3},
                                   {"p1",{15.6,15}},{"p2",{24.4,15}}});
                    ops.push_back({{"op","pcb_add_zone"},{"net","GND"},{"layer","B.Cu"},{"polygon",
                        nlohmann::json::array({ {1,1},{39,1},{39,29},{1,29} })}});
                    ecall("ecad_apply_ops", {{"doc_id",pcbId},{"base_revision",rev},{"ops",ops}}, r);
                    ecall("ecad_fill_zones", {{"doc_id", pcbId}}, r);
                    if (want3d) {
                        st.ecadDocId = pcbId;   // ecadTo3d exports the current ecad doc
                        ecadTo3d(st);           // STEP -> CAD part -> 3D viewport (leave ECAD empty)
                    } else {
                        showId = pcbId;         // show the board in the ECAD canvas
                    }
                }
            } else if (wantUndo) {
                // Undo demo: build a schematic in TWO separate op batches (so the worker records two
                // undoable history steps), then below we load it and ecadUndo() once -> the second
                // batch (the capacitor) is rolled back and the symbol count drops by one.
                if (ecall("ecad_new", {{"kind", "sch"}}, r)) {
                    const std::string id = jstr(r, "doc_id");
                    int rev = jint(r, "revision");
                    // Batch 1: place a resistor.
                    nlohmann::json b1 = nlohmann::json::array();
                    b1.push_back({{"op","sch_add_symbol"},{"libid","Device:R"},{"pos",{20,30}},{"value","10k"}});
                    if (ecall("ecad_apply_ops", {{"doc_id",id},{"base_revision",rev},{"ops",b1}}, r))
                        rev = jint(r, "revision");
                    // Batch 2: place a capacitor (a distinct undoable step).
                    nlohmann::json b2 = nlohmann::json::array();
                    b2.push_back({{"op","sch_add_symbol"},{"libid","Device:C"},{"pos",{40,30}},{"value","100n"}});
                    ecall("ecad_apply_ops", {{"doc_id",id},{"base_revision",rev},{"ops",b2}}, r);
                    showId = id;
                }
            } else if (wantSaveOpen) {
                // Wave Y: exercise the GUI's exact Save -> Open RPC path (minus the modal dialog,
                // which can't run headless). Design a divider schematic, ecad_save it to a temp
                // .ecad+, ecad_open it back into a fresh session, and show the reopened doc. The
                // before/after symbol counts must match for a clean round-trip.
                if (ecall("ecad_design", {{"request", "5V resistive divider with 2.5V output"}}, r)) {
                    const std::string srcId = jstr(r, "doc_id");
                    nlohmann::json before;
                    ecall("ecad_tree", {{"doc_id", srcId}}, before);
                    const int nBefore = before.contains("tree") && before["tree"].contains("symbols")
                                        ? (int)before["tree"]["symbols"].size() : -1;
                    std::error_code ec;
                    const std::string dir = appdataDir() + "\\AI-CAD\\ecad_out";
                    std::filesystem::create_directories(dir, ec);
                    const std::string path = dir + "\\roundtrip.ecad+";
                    nlohmann::json sres;
                    ecall("ecad_save", {{"doc_id", srcId}, {"path", path}}, sres);
                    nlohmann::json ores;
                    if (ecall("ecad_open", {{"path", path}}, ores)) {
                        const std::string openedId = jstr(ores, "doc_id");
                        const int nAfter = ores.contains("tree") && ores["tree"].contains("symbols")
                                           ? (int)ores["tree"]["symbols"].size() : -1;
                        std::printf("[ecad-shot] saveopen: symbols before=%d after=%d (%s)\n",
                                    nBefore, nAfter, nBefore == nAfter ? "ROUND-TRIP OK" : "MISMATCH");
                        showId = openedId;  // show the REOPENED doc
                    }
                    std::fflush(stdout);
                }
            } else if (wantSim) {
                // Native simulator demo: design a verified RC low-pass and run an AC sweep so the shot
                // plots its frequency-response rolloff in the Simulation tab. (ecad_simulate injects a
                // unit AC stimulus on the input source when the study is AC, so |H(f)| is meaningful.)
                if (ecall("ecad_design", {{"request", "RC low-pass filter at 1kHz"}}, r))
                    showId = jstr(r, "doc_id");
                st.ecadSimAnalysis = 1;  // AC sweep (frequency response)
            } else {
                // AI design -> a verified divider schematic (recipe fallback; no model needed).
                if (ecall("ecad_design", {{"request", "5V resistive divider with 2.5V output"}}, r))
                    showId = jstr(r, "doc_id");
            }
            if (!showId.empty() && ecall("ecad_tree", {{"doc_id", showId}}, r)) {
                st.ecadDocId = showId;
                st.ecadRevision = jint(r, "revision");
                ui::ecadEditorMirror().setDocId(showId);
                ui::ecadEditorLoadResponse(r);
                std::printf("[ecad-shot] showing %s rev=%d\n", showId.c_str(), st.ecadRevision);
                if (wantSelect || wantRotate) {
                    // Drive selection without a mouse so the shot shows the highlight halo (+ the
                    // property inspector). Pick a real symbol id from the loaded mirror (the 2nd
                    // symbol when present); for rotate this is s2 — the resistor we turned 90 deg.
                    const auto& syms = ui::ecadEditorMirror().symbols();
                    const std::string sel = "s:" + (syms.size() >= 2 ? syms[1].id
                                                    : (syms.empty() ? std::string("s1") : syms[0].id));
                    ui::ecadEditorSelect(sel);
                    std::printf("[ecad-shot] selected %s (%d symbols)\n", sel.c_str(), (int)syms.size());
                }
                if (wantDel)
                    std::printf("[ecad-shot] after delete: %d symbols\n",
                                (int)ui::ecadEditorMirror().symbols().size());
                if (wantViolZoom) {
                    // Zoom-to-violation: pan/zoom the canvas to the first located ERC violation and
                    // flash the marker — exactly what clicking a violation row does.
                    const bool ok = ui::ecadEditorFocusFirstViolation();
                    st.wantFocusEcad = 6;
                    std::printf("[ecad-shot] violzoom: focused first located violation = %d\n", (int)ok);
                }
                if (wantUndo) {
                    // Undo the capacitor batch (the 2nd, most-recent history step). Confirm via the
                    // editor mirror that the symbol count dropped by one (2 -> 1).
                    const int before = (int)ui::ecadEditorMirror().symbols().size();
                    ecadUndo(st);
                    const int after = (int)ui::ecadEditorMirror().symbols().size();
                    std::printf("[ecad-shot] undo: symbols %d -> %d (rev %d)\n",
                                before, after, st.ecadRevision);
                }
                if (wantSim) {
                    // Run the sim now (blocking) so the screenshot captures the plotted curve. The
                    // panel pops the Simulation tab forward the frame ecadSimResults first appears.
                    ecadSimulate(st);
                    std::printf("[ecad-shot] sim results present=%d\n",
                                (int)(st.ecadSimResults.is_object() && !st.ecadSimResults.empty()));
                }
                std::fflush(stdout);
            }
        }

        // ---------- scripted scenarios (AI_CAD_SHOT=m1/sketch/asm/fea/evalmass/activity) ----------
        // Frame-driven state machines that issue scripted ops through the REAL worker, drive the
        // panels, and request per-step screenshots; extracted to ui::ScenarioRunner (ui/scenarios.cpp).
        scenarios.tick(st);

        // Keep theme state coherent across runtime toggles, screenshot scenarios, and any accidental
        // style leak. This runs before NewFrame so docked panes cannot inherit a stale light surface
        // while the saved/active theme says dark.
        ensureFrameTheme();

        bool busy = st.job.running.load();

        ImGui_ImplOpenGL3_NewFrame();
        ImGui_ImplGlfw_NewFrame();
        ImGui::NewFrame();

        // ---------- W1-B (item 4): global Undo / Redo keys ----------
        // Ctrl+Z = undo, Ctrl+Y / Ctrl+Shift+Z = redo. Routed through the SAME AppState helper the
        // FeatureManager toolbar buttons use (mesh refresh + owner map + confirmation toast).
        // Guards:
        //   - skip while a text field has focus (the field owns Ctrl+Z for its own edit history),
        //   - skip while a sketch is active (sketch mode owns in-sketch undo in a later wave),
        //   - skip while a background job / v2 agent run is in flight (same canDoc gate the toolbar
        //     uses), so we never mutate the doc under a running worker call,
        //   - skip while the ECAD editor window is focused — that window owns Ctrl+Z for its own
        //     history (ecadUndo/ecadRedo, gated inside drawEcadEditor) and must not also drive the
        //     CAD doc's undo.
        {
            const bool canDoc = st.workerStarted && !st.job.running.load() && !st.agentV2Running;
            const bool textFocus = io.WantTextInput;
            const bool inSketchNow = sketchMode.active();
            bool ecadFocused = false;
            if (ImGuiContext* g = ImGui::GetCurrentContext()) {
                for (ImGuiWindow* w = g->NavWindow; w; w = w->ParentWindow)
                    if (std::strcmp(w->Name, "ECAD") == 0) { ecadFocused = true; break; }
            }
            if (canDoc && !textFocus && !inSketchNow && !ecadFocused && st.docMirror.hasDoc() &&
                (io.KeyCtrl || io.KeySuper)) {
                if (io.KeyShift && ImGui::IsKeyPressed(ImGuiKey_Z, false)) {
                    st.docRedo();
                } else if (ImGui::IsKeyPressed(ImGuiKey_Z, false)) {
                    st.docUndo();
                } else if (ImGui::IsKeyPressed(ImGuiKey_Y, false)) {
                    st.docRedo();
                }
            }
            // Wave 2 (GUI rework): Ctrl+N opens the "+" New-Document chooser (Part/Assembly/Drawing/
            // Schematic/PCB). Skip while a text field owns input. The chooser flag is consumed by the
            // doc-tab strip's draw; it shows only when at least one document is already open.
            if (!textFocus && (io.KeyCtrl || io.KeySuper) && !io.KeyShift &&
                ImGui::IsKeyPressed(ImGuiKey_N, false))
                ui::openNewDocChooser();

            // ---- Wave 3: Ctrl+O / Ctrl+S / Ctrl+Shift+S file shortcuts ----
            // Set request flags consumed by the GL-thread file pump below (after the input block) so
            // the native dialog opens with the doc swap off the ImGui input path. Skip while a text
            // field or the ECAD window owns input (the ECAD editor has its own save). Ctrl+S/Shift+S
            // require a live doc; Ctrl+O is always allowed.
            if (!textFocus && !ecadFocused && (io.KeyCtrl || io.KeySuper)) {
                if (!io.KeyShift && ImGui::IsKeyPressed(ImGuiKey_O, false))
                    st.requestOpenDialog = true;
                else if (io.KeyShift && ImGui::IsKeyPressed(ImGuiKey_S, false))
                    st.requestSaveAs = true;
                else if (!io.KeyShift && ImGui::IsKeyPressed(ImGuiKey_S, false))
                    st.requestSave = true;
            }

            // ---- Wave 4C/4D + Wave 3: global Ctrl+C / Ctrl+X / Ctrl+V copy/paste routing ----
            // Priority: Explorer tab active -> filesystem file copy/paste (Wave 3). Otherwise, when a
            // sketch is active SketchMode::drawOverlay owns Ctrl+C/V (it has the (u,v) context), so the
            // global router stands down; with no sketch active it routes to the FeatureManager
            // selection (Wave 4C/4D). Guards mirror the undo block: skip while a text field or the
            // ECAD window owns input. Ctrl+Z/Y are handled above; we never touch them.
            if (!textFocus && !ecadFocused && (io.KeyCtrl || io.KeySuper) && !io.KeyShift) {
                if (ui::fileExplorerActive(st)) {
                    // Explorer-tab FILE copy/paste — no worker needed, so not gated on canDoc.
                    if (ImGui::IsKeyPressed(ImGuiKey_C, false))      ui::fileExplorerCopySelection(st);
                    else if (ImGui::IsKeyPressed(ImGuiKey_V, false)) ui::fileExplorerPaste(st);
                    // Ctrl+X on files intentionally unsupported (no filesystem "cut" semantics).
                } else if (canDoc && !inSketchNow) {
                    // FeatureManager / left-pane selection copy/cut/paste.
                    if (ImGui::IsKeyPressed(ImGuiKey_C, false))      ui::featureCopySelection(st);
                    else if (ImGui::IsKeyPressed(ImGuiKey_X, false)) ui::featureCutSelection(st);
                    else if (ImGui::IsKeyPressed(ImGuiKey_V, false)) ui::featurePasteSelection(st);
                }
                // When inSketchNow (and not Explorer): no-op — SketchMode::drawOverlay handles Ctrl+C/V.
            }
        }

        // ---- Wave 3: file request pump (open/save/recent) ----
        // Drains the one-shot flags the File menu (title_bar.cpp) and the Ctrl+O/S shortcuts set.
        // Runs once per frame on the GL thread, OUTSIDE the ImGui input/menu walk, so the native
        // dialog + DocMirror swap never re-enters a draw. Open paths are dispatched by extension:
        // CAD -> featureTreeOpenPath, ECAD/KiCad -> ecadOpenPath. Each open/save records Recent.
        // Gated on a live, idle worker (mirrors the FeatureManager toolbar's canDoc gate) so we never
        // swap the doc under a running call; a queued request waits one frame rather than firing early.
        if (st.requestOpenDialog || !st.requestOpenPath.empty() || st.requestSave || st.requestSaveAs ||
            st.requestSaveAll || !st.requestExportFormat.empty()) {
            const bool fileReady = st.workerStarted && !st.job.running.load() && !st.agentV2Running;
            if (!fileReady) {
                // Drop save/export/recent requests (need a doc); keep open requests pending until ready.
                if (st.requestSave || st.requestSaveAs || st.requestSaveAll ||
                    !st.requestExportFormat.empty())
                    st.toast(ui::ToastLevel::Info, "Busy — try again in a moment");
                st.requestSave = st.requestSaveAs = st.requestSaveAll = false;
                st.requestExportFormat.clear();
            } else {
            if (st.requestOpenDialog) {
                st.requestOpenDialog = false;
                std::string opened = ui::featureTreeOpenDialog(st);
                if (!opened.empty()) st.pushRecentFile(opened);
            }
            if (!st.requestOpenPath.empty()) {
                std::string path = st.requestOpenPath;
                st.requestOpenPath.clear();
                std::string ext = std::filesystem::path(path).extension().string();
                std::transform(ext.begin(), ext.end(), ext.begin(),
                               [](unsigned char c) { return (char)std::tolower(c); });
                const bool isEcad = (ext == ".ecad+" || ext == ".ecad" ||
                                     ext == ".kicad_sch" || ext == ".kicad_pcb");
                if (isEcad) {
                    ecadOpenPath(st, path);   // records Recent itself
                } else if (ui::featureTreeOpenPath(st, path)) {
                    st.pushRecentFile(path);
                }
            }
            if (st.requestSave) {
                st.requestSave = false;
                std::string saved = ui::featureTreeSaveDoc(st, /*saveAs=*/false);
                if (!saved.empty()) st.pushRecentFile(saved);
            }
            if (st.requestSaveAs) {
                st.requestSaveAs = false;
                std::string saved = ui::featureTreeSaveDoc(st, /*saveAs=*/true);
                if (!saved.empty()) st.pushRecentFile(saved);
            }
            // Wave-5-fill: Save All — save every part/assembly tab. Tabs with a known path re-save
            // silently to it (DocMirror::save needs the mirror pointed at the doc, so we switch to
            // each first); the never-saved ACTIVE doc falls back to a Save-As prompt (a batch must
            // not pop one dialog per untitled doc). Drawing/ECAD tabs are skipped (not DocMirror
            // docs — they have their own save). The original active tab is restored afterward.
            if (st.requestSaveAll) {
                st.requestSaveAll = false;
                const int origActive = st.activeTab;
                int savedCount = 0, failCount = 0;
                for (int i = 0; i < (int)st.docTabs.size(); ++i) {
                    AppState::DocTab& tab = st.docTabs[i];
                    if (tab.kind == AppState::DocTab::TabKind::Drawing ||
                        tab.kind == AppState::DocTab::TabKind::Schematic ||
                        tab.kind == AppState::DocTab::TabKind::PCB)
                        continue;  // not a DocMirror-backed doc
                    if (!tab.path.empty()) {
                        st.switchToTab(i);   // re-point the mirror at this doc (no-op if already active)
                        std::string err;
                        if (st.docMirror.save(st.worker, tab.path, err)) {
                            tab.dirty = false;
                            st.pushRecentFile(tab.path);
                            ++savedCount;
                        } else {
                            st.addLog("[doc] save-all failed for " + tab.path + ": " + err);
                            ++failCount;
                        }
                    } else if (i == origActive) {
                        // Untitled active doc: a Save-As prompt (sets tab.path + s_docPath on success).
                        // Re-point the mirror at it first (a prior iteration may have switched away).
                        st.switchToTab(i);
                        std::string saved = ui::featureTreeSaveDoc(st, /*saveAs=*/false);
                        if (!saved.empty()) { st.pushRecentFile(saved); ++savedCount; }
                    }
                    // Other untitled tabs are left for an explicit per-doc Save (no batch dialogs).
                }
                if (origActive >= 0 && origActive < (int)st.docTabs.size())
                    st.switchToTab(origActive);   // restore the originally-active document
                if (savedCount || failCount)
                    st.toast(failCount ? ui::ToastLevel::Warn : ui::ToastLevel::Info,
                             "Save All: " + std::to_string(savedCount) + " saved" +
                             (failCount ? (", " + std::to_string(failCount) + " failed") : ""));
                else
                    st.toast(ui::ToastLevel::Info, "Save All: nothing to save");
            }
            // Wave-5-fill: Export <fmt> — native Save-As + DocMirror::exportFile for the active doc.
            if (!st.requestExportFormat.empty()) {
                std::string fmt = st.requestExportFormat;
                st.requestExportFormat.clear();
                exportFile3d(st, fmt);
            }
            }  // fileReady
        }

        // Re-push palette-derived viewport visuals every frame so a runtime theme toggle (from the
        // title bar) takes effect immediately. Cheap uniform/state sets; no GPU allocation.
        {
            float top3[3] = {ui::palette.viewportTop[0], ui::palette.viewportTop[1],
                             ui::palette.viewportTop[2]};
            float mid3[3] = {ui::palette.viewportMid[0], ui::palette.viewportMid[1],
                             ui::palette.viewportMid[2]};
            float bot3[3] = {ui::palette.viewportBottom[0], ui::palette.viewportBottom[1],
                             ui::palette.viewportBottom[2]};
            renderer.setBackground3(top3, mid3, bot3);
            renderer.setEdgeStyle(ui::palette.viewportEdge[0], ui::palette.viewportEdge[1],
                                  ui::palette.viewportEdge[2], 1.6f * ui::uiScale);
            renderer.setDefaultPartColor(ui::palette.partDefault[0], ui::palette.partDefault[1],
                                         ui::palette.partDefault[2]);
            // W4-K: gate on showGroundShadow so the HUD hide/show toggle actually kills the shadow.
            // (Previously this unconditional full-alpha push every frame clobbered viewport_panel's
            // gated value before renderOrbit ran, so the toggle had no visible effect.)
            float gsA = st.showGroundShadow ? ui::palette.groundShadow[3] : 0.0f;
            renderer.setGroundShadow(ui::palette.groundShadow[0], ui::palette.groundShadow[1],
                                     ui::palette.groundShadow[2], gsA);
            renderer.setRealView(st.showRealView);  // C30-C: RealView environment reflections
            renderer.setPlaneColors(ui::palette.planeFill, ui::palette.planeOutline);
        }

        if (shot && st.frame == 1) {
            if (st.wantSettings) st.openSettings = true;
            if (st.wantChat || st.wantAssembly || st.wantPlan)
                st.designStarted = true;  // show the conversation takeover

        }
        // Screenshot verification captures after a few frames; keep the settings popup request
        // alive long enough that ImGui opens it even if the first frame is consumed by layout init.
        if (shot && st.wantSettings && st.frame <= 5)
            st.openSettings = true;

        // ---------- custom title bar (our own; the OS title bar is stripped) ----------
        ImGuiViewport* vp = ImGui::GetMainViewport();
        const bool maximized = glfwGetWindowAttrib(win, GLFW_MAXIMIZED) != 0;

        float barH = 0.0f;
        ui::drawTitleBar(st, maximized, barH);

        // ---------- C14: ribbon bar (drawn between title bar and dockspace) ----------
        // syncIconTheme keeps texture selection in sync with the active theme.
        ui::syncIconTheme();
        // C34: SolidWorks behavior — entering a sketch flips the ribbon to the Sketch tab,
        // exiting returns to Features. Edge-triggered so the user can still switch tabs freely
        // while sketching.
        {
            static bool s_wasInSketch = false;
            const bool nowInSketch = sketchMode.active();
            if (nowInSketch && !s_wasInSketch)      st.ribbonTab = AppState::RT_Sketch;
            else if (!nowInSketch && s_wasInSketch) st.ribbonTab = AppState::RT_Features;
            s_wasInSketch = nowInSketch;
        }
        // C40: accent-highlight the armed sketch tool's ribbon button.
        ui::setRibbonActiveCmd(sketchMode.activeToolCmd());
        float ribbonH = ui::drawRibbon(st.ribbonTab, st.showRibbon,
                                       st.showSimulationTools, st.showPcbTools);

        // ---------- document tab strip (browser/SolidWorks-style; under the ribbon) ----------
        // Reconcile the strip with the mirror (adopt a doc created by a path that doesn't open its
        // own tab — sketch auto-create, chat new-part, agent v2 run, assembly panel), then draw it.
        // Shown whenever at least one document is open; zero-height in the empty start state so the
        // single-document / no-document layout is unchanged.
        st.ensureActiveTab();
        float docTabsH = ui::drawDocTabs(st, barH + ribbonH);

        // ---------- dockspace host (between the title bar+ribbon and the status bar) ----------
        const float statusH = ImGui::GetTextLineHeight() + 12.0f * scale;
        const float dockTopY = barH + ribbonH + docTabsH;
        ImGui::SetNextWindowPos(ImVec2(vp->Pos.x, vp->Pos.y + dockTopY));
        ImGui::SetNextWindowSize(ImVec2(vp->Size.x, vp->Size.y - dockTopY - statusH));
        ImGui::SetNextWindowViewport(vp->ID);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, 0);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
        ImGuiWindowFlags hostFlags = ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoCollapse |
            ImGuiWindowFlags_NoResize | ImGuiWindowFlags_NoMove |
            ImGuiWindowFlags_NoBringToFrontOnFocus | ImGuiWindowFlags_NoNavFocus |
            ImGuiWindowFlags_NoDocking;
        ImGui::Begin("##DockHost", nullptr, hostFlags);
        ImGui::PopStyleVar(3);
        ImGuiID dockId = ImGui::GetID("AICADDock");
        if ((!st.layoutInit && ImGui::DockBuilderGetNode(dockId) == nullptr) || st.resetLayout) {
            buildDefaultLayout(dockId, ImVec2(vp->Size.x, vp->Size.y - dockTopY - statusH));
            st.layoutInit = true;
            st.resetLayout = false;
            // C24: make Design the active tab in the right task pane (not Agent Activity), matching
            // SolidWorks's task pane defaulting to the first tab. Deferred one frame via a flag so
            // the docked windows exist before we focus.
            st.focusDesignTab = true;
            if (!st.wantEcad) st.wantFocusViewport = 6;
        }
        ImGui::DockSpace(dockId, ImVec2(0, 0), ImGuiDockNodeFlags_None);
        ImGui::End();

        // ---------- status bar ----------
        ui::drawStatusBar(st, vp, statusH, busy, hostFlags);

        bool canRun = st.workerStarted && !busy;

        // ---------- panels ----------
        // Wave 1D: the SolidWorks-faithful tabbed LEFT pane (Explorer / FeatureManager /
        // PropertyManager / Configurations). drawLeftPane routes the active tab body and auto-
        // switches to the PropertyManager tab while a command form is active. We still tick the PM
        // so the debounced live preview fires; the container draws the PM form body when active.
        // A live PropertyManager forces the left pane open so its form is visible, but the preview
        // tick is independent of that pane state to avoid invisible active commands.
        if (propertyManager.active()) {
            propertyManager.tick(st);
            st.showFeatureTree = true;
        }
        if (st.showFeatureTree) {
            ui::drawLeftPane(st, [&st, &propertyManager](const std::string& fid) {
                propertyManager.openForEdit(st, fid);
            });
        }
        // W3-H: unified AI Copilot pane (input-at-top + conversation; embeds the run tree
        // inline while a v2 run is active). st.activityPanel is still fed events via onEvent.
        ui::drawAIPanel(st, busy, canRun);
        // W3-H: standalone "Agent Activity" window (View > Activity / the pane's Activity
        // button / the activity shot). No-op unless st.showActivityPanel; was previously
        // unreachable because only the in-pane tab hosted drawBody.
        activityPanel.draw(st);
        if (st.wantFocusViewport > 0) { ImGui::SetNextWindowFocus(); st.wantFocusViewport--; }
        ui::drawViewportPanel(st, io, busy);
        if (st.wantFocusEcad > 0) {
            st.showPcbTools = true;
            ImGui::SetNextWindowFocus();
            st.wantFocusEcad--;
        }
        if (st.showPcbTools || !st.ecadDocId.empty())
            ui::drawEcadEditor(st);   // Wave N: AI-ECAD 2D editor
        if (st.ecadAiGo) { st.ecadAiGo = false; ecadAiDesign(st, st.ecadAiRequest); }  // Wave P
        if (st.ecadAiBoardGo) { st.ecadAiBoardGo = false; ecadAiBoard(st, st.ecadAiRequest); }  // Wave U
        if (st.ecadOpsGo) { st.ecadOpsGo = false; ecadApplyOps(st, st.ecadPendingOps); }  // Wave Q
        if (st.ecadSimGo) { st.ecadSimGo = false; ecadSimulate(st); }  // native simulator
        if (st.ecadUndoGo) { st.ecadUndoGo = false; ecadUndo(st); }  // ECAD history undo
        if (st.ecadRedoGo) { st.ecadRedoGo = false; ecadRedo(st); }  // ECAD history redo

        // ---------- C19: simulation panel + evaluate dialogs ----------
        simPanel.tick(st);          // drain SimClient progress + completion (loads overlays)
        simPanel.draw(st);
        evaluateDialogs.draw(st);

        // ---------- C20b: assembly panels (Insert / Mate / Explode / Interference / BOM) ----------
        asmPanels.draw(st);

        // ---------- Equations / Global Variables editor (modeless; gated by st.showEquations) ----------
        // Opened from the FeatureManager "Equations" node (double-click) or View > Equations. Lists
        // doc.equations.globals; add/edit -> eq_set, delete -> eq_delete, then reloads the mesh.
        ui::drawEquationsDialog(st);

        // Configurations now lives inside the left manager's ConfigurationManager tab, matching
        // the commercial CAD mental model instead of floating as a separate task-pane window.

        // ---------- Design Table panel (SolidWorks editable config grid) ----------
        // Editable spreadsheet over the active doc's configurations: rows=configs, columns=driven
        // params + per-feature suppression. Cell edits write per-config overrides via the same
        // config_* RPCs; editing the active row updates the viewport. No-op unless the flag is set.
        designTablePanel.draw(st);

        // ---------- Weldment Cut List panel (SolidWorks Weldment Cut List) ----------
        // Read-only table of the active part's weld_member structural members grouped by
        // profile + length into rows with quantities. No-op unless st.showCutListPanel.
        cutListPanel.draw(st);

        // ---------- C15 pick-shot driver: simulate a center-screen pick ----------
        // The viewport panel rendered the id buffer for this frame's camera; pick its center,
        // apply the selection (highlight shows from the next frame), and print the PickHit once
        // so verification is scriptable. Runs every frame so the camera has settled by capture.
        if (st.wantPick && st.pickReady) {
            const Renderer::CameraXform& cam = renderer.camera();
            int fw = cam.ssWidth / renderer.supersample();
            int fh = cam.ssHeight / renderer.supersample();
            render::PickHit center = idPass.pickFinal(fw / 2, fh / 2, fw, fh);
            st.hover = render::PickHit{};
            st.selection.selectOne(center);
            st.pickStatus = st.pickLabel(center);
            if (!st.pickPrinted) {
                const char* kindName =
                    center.kind == render::PickKind::Face ? "face" :
                    center.kind == render::PickKind::Edge ? "edge" :
                    center.kind == render::PickKind::Vertex ? "vertex" : "none";
                std::printf("pick: body=%u kind=%s id=%u\n", center.body_id, kindName,
                            center.topo_id);
                std::fflush(stdout);
                st.pickPrinted = true;
            }
        }

        // ---------- modals ----------
        ui::drawSettingsModal(st, busy);

        // ---------- W1-B: toast notifications (top-right, over everything) ----------
        ui::drawToasts(st);

        // ---------- W1-B: consume this frame's cursor request (item 3) ----------
        // After all panels have had a chance to call st.requestCursor(); suspends ImGui cursor
        // management and sets the GLFW cursor when a non-default cursor was requested, else restores.
        ui::applyCursorRequest(st, win);

        ImGui::Render();
        int dw, dh;
        glfwGetFramebufferSize(win, &dw, &dh);
        glViewport(0, 0, dw, dh);
        glClearColor(ui::palette.windowBg[0], ui::palette.windowBg[1],
                     ui::palette.windowBg[2], 1.0f);
        glClear(GL_COLOR_BUFFER_BIT);
        ImGui_ImplOpenGL3_RenderDrawData(ImGui::GetDrawData());

        // Scripted-scenario per-step captures + frame-loop control (m1/sketch/asm/fea/evalmass/
        // activity): endFrame() writes any pending step screenshot from the backbuffer, mirrors
        // the final step to screenshotPath, swaps buffers, and reports whether the scenario cut
        // this loop iteration short (was the inline swap+continue) or ended the loop (swap+break).
        {
            ui::ScenarioRunner::FrameAction act = scenarios.endFrame(st, dw, dh, screenshotPath);
            if (act == ui::ScenarioRunner::FrameAction::Break) break;
            if (act == ui::ScenarioRunner::FrameAction::Continue) continue;
        }

        // tree shot needs extra frames for the tree to repaint after async doc load
        // wantTree but !treeShotDone: keep waiting (up to 30 frames) for the worker to start
        // wantTree and treeShotDone: take the screenshot at frame 10 (tree has repainted)
        int shotFrame = st.wantTree ? (st.treeShotDone ? 10 : 30) : 6;
        // equations shot: wait (up to 30 frames) for the worker, then capture at frame 10 once the
        // two globals are seeded and the dialog has repainted with its rows.
        if (st.wantEquations) shotFrame = st.equationsShotDone ? 10 : 30;
        if (st.wantEcad) {
            if (!st.ecadShotDone)                                       shotFrame = 150;   // worker + design
            else if (st.wantFocusViewport > 0 || st.wantFocusEcad > 0)  shotFrame = 99999; // let focus settle
            else                                                        shotFrame = 10;    // then capture
        }
        if (shot && ++st.frame >= shotFrame) {
            std::vector<unsigned char> px(dw * dh * 4);
            glReadBuffer(GL_BACK);
            glReadPixels(0, 0, dw, dh, GL_RGBA, GL_UNSIGNED_BYTE, px.data());
            std::vector<unsigned char> flip(dw * dh * 4);
            for (int y = 0; y < dh; ++y)
                std::memcpy(&flip[(dh - 1 - y) * dw * 4], &px[y * dw * 4], dw * 4);
            stbi_write_png(screenshotPath, dw, dh, 4, flip.data(), dw * 4);
            std::printf("ui screenshot -> %s (%dx%d)\n", screenshotPath, dw, dh);
            break;
        }
        glfwSwapBuffers(win);
    }

    freeChatTextures(st.chat);  // release iteration thumbnails while the GL context is still live
    ui::freeIcons();            // C14: release atlas textures while GL context is still live
    ui::destroyCursors();       // W1-B: free the GLFW standard cursors
    st.rpc.shutdown();          // C39: drop pending async calls + abort the in-flight one
    st.worker.stop();
    simClient.stop();  // C19: shut down the second worker process
    ImGui_ImplOpenGL3_Shutdown();
    ImGui_ImplGlfw_Shutdown();
    ImGui::DestroyContext();
    glfwDestroyWindow(win);
    glfwTerminate();
    return 0;
}
