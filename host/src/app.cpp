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
#include <unordered_set>
#ifndef _WIN32
#include <csignal>
#include <unistd.h>
#endif

#include "bootstrap.h"
#include "mesh.h"
#include "renderer.h"
#include "ui_theme.h"
#include "win_chrome.h"
#include "worker_client.h"

#include "render/id_pass.h"
#include "render/overlays.h"
#include "render/splat_renderer.h"  // RE-27: splat GL draw in reveng reconstruct stage

#include "core/app_state.h"
#include "core/engine_events.h"  // Phase 4: per-frame hostd event drain (daemon mode)
#include "engine_backend.h"      // Phase 4: engine::daemonMode() (services/worker)
#include "ipc_client.h"          // services/ipc EngineClient (orphan recovery RPCs)
#include "ipc_discovery.h"       // H2 shot: spawnDetached/resolveHostdBin for hostd bounce

#ifdef AI_CAD_ENABLE_CEF
#include "ui/widgets/cef/cef_app.h"  // CEF process lifecycle (init/pump/shutdown)
#endif
#include "core/cpp_core.h"   // sampling-profiler RPCs (prof_start/prof_stop) via libaicad
#include "core/jobs.h"
#include "ui/console_panel.h"
#include "ui/doc_tabs.h"
#include "ui/feature_tree.h"
#include "ui/file_explorer.h"  // Wave 3: File Explorer + workspace/recent helpers
#include "ui/layout.h"        // Wave 1C/1D: buildDefaultLayout + tabbed left pane (drawLeftPane)
#include "ui/legacy_chat.h"
#include "ui/ai/ai_workspace.h"   // AI workspace full-body hub (sessions + DAG + chat + settings)
#include "ui/reveng/reveng_workspace.h"  // Reverse Engineering workspace full-body shell (RE-03)
#include "ui/firmware/fw_workspace.h"    // T-FIN5 Firmware workspace (drawFirmwareWorkspace + shot fixture)
#include "ui/reveng/reveng_internal.h"   // RE-22: S() + Stage enum for shot seeding
#include "ui/reveng/dim_review.h"        // RE-22 dimension-confirm review + deviation panels
#include "ui/settings_modal.h"
#include "ui/status_bar.h"
#include "ui/title_bar.h"
#include "ui/viewport_panel.h"
#include "ui/drawing_panel.h"     // W17: drawing ribbon command dispatch (drawingHandleCommand)
#include "ui/ecad/ecad_panel.h"   // Wave N: AI-ECAD 2D editor
#include "ui/ecad/ecad_mirror.h"  // Wave N: complete type for EcadMirror::setDocId
#include "ui/cam/cam_panel.h"     // CAM: manufacturing workspace (toolpaths / post / sim)
#include "ui/file_dialog.h"       // Wave Y: native Save As / Open dialogs for .ecad+
#include "ui/icons.h"
#include "ui/appearance_common.h"  // Appearance tab: MaterialPreset table (kMaterials) for presets
#include "ui/widgets/ribbon.h"
#include "ui/widgets/interwebz.h"  // InterwebzDemoDraw (View > Interwebz demo)
#include "ui/ribbon_def.h"
#include "ui/property_manager.h"
#include "ui/sketch_mode.h"   // C18
#include "ui/sketch3d_mode.h" // 3D free-space sketcher (sketch3d features)
#include "ui/config_panel.h"  // Configurations (SolidWorks ConfigurationManager)
#include "ui/design_table_panel.h"  // Design Table (SolidWorks editable config grid)
#include "ui/cutlist_panel.h"  // Weldment Cut List (SolidWorks Weldment Cut List)
#include "ui/file_dialog.h"   // W14: native Save-As for 2D export (flat-pattern DXF)
#include "ui/toast.h"         // W1-B: toast notifications
#include "ui/cursors.h"       // W1-B: cursor manager
// --- C19: simulation + flow + evaluate UI ---
#include "core/sim_client.h"
#include "ui/sim_manager.h"
#include "ui/evaluate_dialogs.h"
// --- end C19 ---
// --- C22b: Activity panel + prompt editor ---
#include "ui/activity_panel.h"
// --- end C22b ---
// --- C20b: assembly UI ---
#include "ui/asm_panels.h"
// --- end C20b ---
#include "ui/equations_dialog.h"  // Equations / Global Variables editor
#include "ui/profiler_window.h"   // in-app sampling-profiler window (text report + flamegraph)
#include "ui/command_search.h"    // unified settings + modeling-tools search popup
#include "ui/web_panel.h"          // "Web" title-bar chip: full-window in-app CEF browser
#include "ui/scenarios.h"  // scripted screenshot/test scenarios (m1/sketch/asm/fea/evalmass/activity)
// [workspace-tabs seam] full-body workspace renderers for PLM and PartsLibrary.
#include "ui/plm/plm_workspace.h"
#include "ui/parts/parts_workspace.h"

#include <filesystem>      // Wave N: ecad export output dir + W14: flat-pattern DXF export

// =====================================================================================
// Wave N: AI-ECAD GUI command wiring. Creates/routes ECAD documents through the worker
// ecad_* RPCs (engine/ecad/rpc.py) and feeds the {revision,kind,tree,compile} responses into
// the editor mirror — the same discipline core/doc_mirror.cpp uses for the CAD doc_* methods.
// See docs/ECAD_UI_INTEGRATION.md §(e). New docs + doc-scoped analysis/exports are live;
// geometry-placing ops (sch_add_symbol, pcb_add_track, ...) still need canvas-click placement.
// =====================================================================================
namespace {

// Appearance ribbon: apply a set_appearance op to the whole model (body "b1") and reload the mesh
// so the new color/material shows immediately. color==nullptr clears back to the default grey.
// A direct apply is safe from the ribbon dispatcher (it runs before the FeatureManager tree walk
// this frame, like edit_delete / Flatten). "b1" matches the feature-tree root convention; per-body
// edits stay on the tree / viewport context menu.
void applyModelAppearance(AppState& st, const float* color, float metallic, float roughness) {
    if (!st.docMirror.hasDoc() || !st.workerStarted) {
        st.toast(ui::ToastLevel::Info, "Open a part first to change its appearance");
        return;
    }
    nlohmann::json op = {{"op", "set_appearance"}, {"id", "b1"}};
    if (color) { op["color"] = {color[0], color[1], color[2]};
                 op["metallic"] = metallic; op["roughness"] = roughness; }
    else op["color"] = nullptr;
    ++st.rebuildsInFlight;
    st.docMirror.applyOpsAsync(st.rpc, st.worker, nlohmann::json::array({op}),
        [&st](const core::OpsResponse& resp, bool applied) {
            --st.rebuildsInFlight;
            if (!applied) return;
            if (resp.ok && !resp.aicm_path.empty()) {
                st.loadDocMeshAsync(resp.aicm_path);  // T-PERF3
            } else if (!resp.ok) {
                st.addLog("[err] set_appearance: " + resp.error_message);
            }
        });
}

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

// Auto-init: when the Electronics workspace is entered with no ECAD document open, create a default
// "PCB1" board so the user always lands in a usable, editable layout instead of the empty launchpad.
// Runs from the app loop (workspace-entry edge), NEVER from a draw path — an ecad_new call in
// drawEcadEditor's every-frame empty branch would spawn infinite boards + risk the GL-thread-RPC
// freeze. On worker failure it is a no-op: the launchpad stays as the graceful fallback (item 4:
// flag/degrade, never render a broken viewport).
void ecadAutoInitBoard(AppState& st) {
    if (!st.ecadDocId.empty()) return;   // a board/schematic is already open — nothing to do
    nlohmann::json res;
    if (!ecadCall(st, "ecad_new", {{"kind", "pcb"}}, res)) {
        // Backend hook missing/unavailable: degrade gracefully, keep the AI launchpad reachable.
        st.addLog("[ecad] auto-init PCB1 skipped (worker ecad_new unavailable) — launchpad shown");
        return;
    }
    const std::string id = jstr(res, "doc_id");
    const int rev = jint(res, "revision");
    st.addEcadTab("pcb", id, rev, /*title=*/"PCB1");
    // Disposable starter: if the user leaves Electronics without adding anything, this blank PCB1 is
    // discarded (AppState::discardWorkspaceStarter). Any edit sets the tab dirty and it survives.
    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size())
        st.docTabs[st.activeTab].isStarter = true;
    ecadAdopt(st, res);
    st.addLog("[ecad] auto-init default board PCB1 (" + st.ecadDocId + ")");
    st.toast(ui::ToastLevel::Info, "Started a new board: PCB1");
    ImGui::SetWindowFocus("ECAD");
}

// A doc-scoped RPC that returns an ops-response (rebuild / autoroute / fill_zones) -> refresh.
void ecadDocRpc(AppState& st, const char* method, const char* okMsg) {
    if (st.ecadDocId.empty()) { st.toast(ui::ToastLevel::Warn, "Open an ECAD document first"); return; }
    nlohmann::json res;
    if (!ecadCall(st, method, {{"doc_id", st.ecadDocId}}, res)) return;
    ecadAdopt(st, res);
    st.markEcadTabDirty(st.ecadDocId);   // rebuild / autoroute / fill_zones mutate the board
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
    st.markEcadTabDirty(st.ecadDocId);   // canvas place/route/move/delete/edit → unsaved
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
    st.markEcadTabDirty(st.ecadDocId);
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
    st.markEcadTabDirty(st.ecadDocId);
    st.toast(ui::ToastLevel::Info, "Redo");
}

void ecadExport(AppState& st, const char* kind) {
    if (st.ecadDocId.empty()) { st.toast(ui::ToastLevel::Warn, "Open an ECAD document first"); return; }
    std::error_code ec;
    const std::string dir = appdataAppDir() + "/ecad_out";
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
    // Prefer the unified per-tab saver when this ECAD doc has a tab — it re-saves silently to the
    // tab's known path (Save-As only on first save) and clears the tab's unsaved marker.
    for (int i = 0; i < (int)st.docTabs.size(); ++i) {
        const auto& t = st.docTabs[i];
        if ((t.kind == AppState::DocTab::TabKind::Schematic ||
             t.kind == AppState::DocTab::TabKind::PCB) && t.ecadDocId == st.ecadDocId) {
            ui::saveDocTab(st, i, /*forceDialog=*/false);
            return;
        }
    }
    // Fallback: a tab-less ECAD doc (e.g. an AI-designed schematic that never got a tab).
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
    const std::string dir = appdataAppDir() + "/ecad_out";
    std::filesystem::create_directories(dir, ec);
    const std::string step = dir + "/board.step";
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
    ++st.rebuildsInFlight;
    st.docMirror.applyOpsAsync(st.rpc, st.worker, ops,
        [&st](const core::OpsResponse& r, bool applied) {
            --st.rebuildsInFlight;
            if (!applied) return;
            if (r.ok && !r.aicm_path.empty()) {
                st.loadDocMeshAsync(r.aicm_path);  // T-PERF3
                st.toast(ui::ToastLevel::Info, "Flat pattern created");
            } else if (!r.ok) {
                st.toast(ui::ToastLevel::Warn,
                         r.error_message.empty() ? std::string("Flatten failed")
                                                 : r.error_message);
            }
        });
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

// Wave-5-fill / F4: File > Export <fmt> — export the ACTIVE part/assembly's bodies to a 3D format
// engine.doc.formats.export_document accepts ("step"|"iges"|"stl"|"3mf"|"brep"|"obj"|"ply"|"gltf"|
// "glb"; see engine/doc/formats.py FORMAT_REGISTRY / doc_formats RPC). Picks a path (native Save-As,
// or a cwd fallback when dialogs are disabled) then calls export_file via DocMirror. Mirrors
// exportFlatDxf's shape. Called only from the GL-thread request pump (the menu sets
// st.requestExportFormat) so the native dialog never re-enters an ImGui menu walk.
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
    else if (fmt == "obj")  { ext = "obj";  filter = "OBJ (*.obj)\0*.obj\0All files (*.*)\0*.*\0"; }
    else if (fmt == "ply")  { ext = "ply";  filter = "PLY (*.ply)\0*.ply\0All files (*.*)\0*.*\0"; }
    else if (fmt == "gltf") { ext = "gltf"; filter = "glTF (*.gltf)\0*.gltf\0All files (*.*)\0*.*\0"; }
    else if (fmt == "glb")  { ext = "glb";  filter = "glTF binary (*.glb)\0*.glb\0All files (*.*)\0*.*\0"; }

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

    // Mesh formats carry the dialog's tessellation choice; B-rep formats ignore it.
    const bool meshFmt = (fmt == "stl" || fmt == "obj" || fmt == "ply" || fmt == "3mf" ||
                          fmt == "gltf" || fmt == "glb");
    const double linDef = meshFmt ? (double)st.exportTess.linDeflection : 0.05;
    const double angDef = meshFmt ? (double)st.exportTess.angDeflectionDeg * 3.14159265358979 / 180.0
                                  : 0.35;
    const bool relDef = meshFmt && st.exportTess.relative;
    const bool asciiStl = meshFmt && st.exportTess.ascii;

    std::string err;
    std::string written = st.docMirror.exportFile(st.worker, st.docMirror.docId(), path, fmt, err,
                                                  linDef, angDef, relDef, asciiStl);
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
    // F4: every importable 3D format (see engine/doc/formats.py FORMAT_REGISTRY / doc_formats RPC)
    // plus the ECAD/KiCad family and the 2D DXF/DWG lane. The GL-thread pump branches CAD vs
    // ECAD by extension.
    return ext == ".step+" || ext == ".step" || ext == ".stp" || ext == ".iges" || ext == ".igs" ||
           ext == ".brep"  || ext == ".stl"  || ext == ".3mf" || ext == ".obj"  || ext == ".ply"  ||
           ext == ".gltf"  || ext == ".glb"  || ext == ".3dm" ||
           ext == ".dxf" || ext == ".dwg" ||
           ext == ".sldprt" || ext == ".sldasm" || ext == ".x_t" || ext == ".x_b" ||  // experimental (SW COM)
           ext == ".ecad+" || ext == ".ecad" ||
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

// Default to a fresh part on launch — on a non-shot run, once the worker is ready and nothing is
// open, auto-create a disposable STARTER part (DocTab::isStarter) so the app opens straight into a
// usable document instead of an empty shell. If the user then opens an existing file, the untouched
// starter is silently discarded (AppState::dropBlankStarterTabs, called from the open path); if they
// edit it, it sticks. Fires at most once (st.starterTried). No-op in shot/scenario mode (scenarios
// create their own docs) and once a doc/design already exists.
static void ensureStarterDoc(AppState& st, bool shot) {
    if (shot || st.starterTried) return;
    if (!st.workerStarted || st.docMirror.hasDoc() || !st.docTabs.empty() || st.designStarted) return;
    st.starterTried = true;
    ui::createNewDoc(st, 0);   // New Part — same path as the chooser (incl. per-doc session setup)
    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size())
        st.docTabs[st.activeTab].isStarter = true;
    st.wantFocusViewport = 6;
    st.addLog("[startup] opened default starter part");
}

// Save every dirty tab for the exit flow ("Save All & Quit"), dispatching by kind via the unified
// saver (CAD .step+, ECAD .ecad+, Drawing → its source part). Titled docs re-save silently; untitled
// ones raise a Save-As dialog. Restores the originally-active tab afterward (CAD/Drawing saves
// re-point the mirror). Returns true only if every dirty doc is now clean — a cancelled Save-As or a
// save failure returns false → abort the quit so nothing is lost.
static bool saveAllDirtyForQuit(AppState& st) {
    const int origActive = st.activeTab;
    bool allSaved = true;
    for (int i = 0; i < (int)st.docTabs.size(); ++i) {
        if (!st.docTabs[i].dirty) continue;
        // Only save user-owned docs — a pure AI mirror the user never adopted isn't theirs to
        // save (its agent owns it). In practice such a tab is never dirty, but guard anyway.
        if (st.docTabs[i].aiHandoff && !st.docTabs[i].userAdopted) continue;
        ui::TabSaveResult r = ui::saveDocTab(st, i, /*forceDialog=*/false);
        if (r == ui::TabSaveResult::Cancelled || r == ui::TabSaveResult::Failed) allSaved = false;
    }
    if (origActive >= 0 && origActive < (int)st.docTabs.size())
        st.switchToTab(origActive);   // restore the originally-active document
    return allSaved;
}

// TEMP DIAGNOSTIC: forward decl for the vendor-wizard headless repro in run_gui.
namespace ui { namespace parts { void startLoginWizard(AppState&, const std::string&); } }

int run_gui(const char* screenshotPath, const char* openPath) {
    const bool shot = screenshotPath != nullptr;
#ifndef _WIN32
    // The borderless custom chrome (chrome::tick drag/resize) needs a backend where a client
    // can position itself; Wayland forbids that, so prefer X11 (XWayland). Fall back to the
    // default platform if X11 isn't available (the window stays borderless but can't self-move).
    glfwInitHint(GLFW_PLATFORM, GLFW_PLATFORM_X11);
    if (!glfwInit()) {
        glfwInitHint(GLFW_PLATFORM, GLFW_ANY_PLATFORM);
        if (!glfwInit()) return 1;
    }
#else
    if (!glfwInit()) return 1;
#endif
    glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 3);
    glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 3);
    glfwWindowHint(GLFW_OPENGL_PROFILE, GLFW_OPENGL_CORE_PROFILE);
    if (!shot) glfwWindowHint(GLFW_MAXIMIZED, GLFW_TRUE);
#ifndef _WIN32
    // Borderless: the app draws its own title bar; chrome::tick handles move/resize. (Windows
    // creates a normal window and strips the frame later via the WndProc in chrome::install.)
    glfwWindowHint(GLFW_DECORATED, GLFW_FALSE);
#endif
    // T-G15 dev/test hook: AI_CAD_HIDDEN=1 creates the window unmapped so headless shot runs never
    // enter the compositor's present path (on an unattended desktop the compositor can starve a new
    // window's swaps indefinitely — observed as multi-minute drmSyncobjTimelineWait stalls). GL
    // rendering + glReadPixels work as normal on the hidden window's framebuffer. Shot-mode only.
    if (shot && std::getenv("AI_CAD_HIDDEN")) glfwWindowHint(GLFW_VISIBLE, GLFW_FALSE);
    // Stable window class / app_id so the desktop shell (KDE/GNOME) matches the window to the
    // installed .desktop entry (StartupWMClass=Billet) and shows the app icon in the taskbar
    // instead of a generic placeholder. Without this, GLFW's WM_CLASS falls back to the window
    // title, which may match no .desktop entry. Harmless on Windows (no-op hints).
    glfwWindowHintString(GLFW_X11_CLASS_NAME, "Billet");
    glfwWindowHintString(GLFW_X11_INSTANCE_NAME, "Billet");
    glfwWindowHintString(GLFW_WAYLAND_APP_ID, "Billet");
    // AI_CAD_WIN_SIZE=WxH: explicit window geometry for managed/headless sessions
    // (billet-web streams a fixed-size Xvfb display with no WM, so maximize hints
    // are no-ops there — the window must be created at display size and at 0,0).
    int winW = shot ? 1340 : 1440, winH = shot ? 838 : 900;
    bool winSized = false;
    if (const char* ws = std::getenv("AI_CAD_WIN_SIZE")) {
        int w = 0, h = 0;
        if (std::sscanf(ws, "%dx%d", &w, &h) == 2 && w >= 640 && h >= 480 && w <= 16384 && h <= 16384) {
            winW = w; winH = h; winSized = true;
        }
    }
    GLFWwindow* win = glfwCreateWindow(winW, winH, "Billet", nullptr, nullptr);
    if (!win) { glfwTerminate(); return 1; }
    if (winSized) glfwSetWindowPos(win, 0, 0);
    glfwMakeContextCurrent(win);
    glfwSwapInterval(1);
    if (!gladLoadGL((GLADloadfunc)glfwGetProcAddress)) return 1;

    // CEF (when the cef backend is selected) is initialised early in main(),
    // before GLFW — see main.cpp. Nothing to do here.

    const std::string assets = exeDir() + "/assets";  // '/' is valid on Windows + POSIX
    setWindowIcon(win, assets + "/logo_icon.png");

    IMGUI_CHECKVERSION();
    ImGui::CreateContext();
    ImGuiIO& io = ImGui::GetIO();
    io.ConfigFlags |= ImGuiConfigFlags_DockingEnable;
    // GUI rework layout schema. The old imgui.ini can contain dock IDs/window placements from
    // pre-rework names, which restores the FeatureManager into the center and leaves Viewport as a
    // floating orphan. Use a versioned ini so stale dock layouts cannot override buildDefaultLayout.
    // v4: overlay layout — stale v3 entries carry docked flags/placements for the now-floating
    // FeatureManager/Copilot columns. The ini is ALSO the columns' width persistence (ImGui saves
    // floating windows' Pos/Size by name), so column width survives restarts with zero new code.
    static std::string iniPath = appdataAppDir() + "/imgui_gui_v4.ini";
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

    // Theme: honour AI_CAD_THEME env override, then persisted setting, then LIGHT default
    // (owner default 2026-07-14). Applied once here; the settings load at first frame
    // re-applies the persisted choice, and the View-menu toggle calls applyTheme() again.
    {
        const char* envTheme = std::getenv("AI_CAD_THEME");
        ui::Theme initTheme = ui::Theme::Light;
        if (envTheme && std::strcmp(envTheme, "dark") == 0) initTheme = ui::Theme::Dark;
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

    Tex wordmark = loadTexture(assets + "/logo_wordmark.png");
    Tex wordmarkDark = loadTexture(assets + "/logo_wordmark_dark.png");
    Tex logoIcon = loadTexture(assets + "/logo_icon.png");

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
    {
        // C37-C feature-highlight wash follows the theme (dimmer selection tint) instead of the
        // baked default; re-pushed every frame below so theme switches track automatically.
        float feat[4];
        ui::Palette::withAlpha(ui::palette.selectionHighlight, feat, 0.30f);
        overlays.setColors(ui::palette.hoverHighlight, ui::palette.selectionHighlight, feat);
    }

    // ---------- application state (bundles what were run_gui locals) ----------
    AppState st;
    st.win = win;
    st.shot = shot;
    st.scale = scale;
    // "Open with" / file association (task 05): a document handed in on the command line is queued
    // on the same deferred open pump the menu / OS file-drop use. The pump holds an open request
    // pending until the worker is up (it only drops save/export when busy), so this opens cleanly
    // even though the worker hasn't started yet at this point.
    if (openPath && openPath[0]) {
        st.requestOpenPath = openPath;
        st.addLog(std::string("[open-with] launch arg -> ") + openPath);
    }
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

    // Multi-document FALLBACK singletons. PropertyManager / SketchMode / EvaluateDialogs are now
    // owned PER-DOCUMENT on each DocSession; st.propertyManager / sketchMode / evaluateDialogs are
    // re-seated at the active document's instances on every tab switch (reseatActiveSubsystems).
    // These run_gui-owned objects are the FALLBACK used at the empty start state and on a Drawing /
    // ECAD tab (no CAD document active). st.* default to them so every borrow site is valid before
    // the first document opens. NOTE: the dispatcher + frame loop below go through st.propertyManager
    // / st.sketchMode / st.evaluateDialogs (NOT these locals) so they act on the ACTIVE document.
    ui::PropertyManager propertyManager;
    st.propertyManager = &propertyManager;
    st.fallbackPropertyManager = &propertyManager;

    // C19: SimClient (worker #2, single shared solver) + Evaluate dialogs — owned here. The
    // per-document SimManagers live on each DocSession; the fallback below covers the empty start
    // state / non-CAD tabs.
    core::SimClient simClient;
    st.simClient = &simClient;
    ui::SimManager fallbackSimManager;
    st.fallbackSimManager = &fallbackSimManager;
    st.simManager = &fallbackSimManager;
    ui::EvaluateDialogs evaluateDialogs;        // fallback Measure/Mass/Section dialogs (per-doc otherwise)
    st.evaluateDialogs = &evaluateDialogs;
    st.fallbackEvaluate = &evaluateDialogs;

    // C18: SketchMode — owned here, borrowed via st.sketchMode (opaque void*).
    ui::SketchMode sketchMode;
    st.sketchMode = &sketchMode;
    st.fallbackSketchMode = &sketchMode;

    // 3D sketcher (sketch3d features) — app-wide singleton (guards its doc id itself).
    ui::Sketch3DMode sketch3dMode;
    st.sketch3dMode = &sketch3dMode;

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
    auto dispatchCmd = [&st, &asmPanels](const char* cmd_id) {
        // Multi-document: resolve the PropertyManager / SketchMode / EvaluateDialogs of the ACTIVE
        // document (st.* are re-seated on every tab switch). Shadowing the names keeps the dispatch
        // body below byte-identical to the single-document version.
        ui::PropertyManager& propertyManager = *st.propertyManager;
        ui::SketchMode&      sketchMode      = *st.sketchMode;
        ui::EvaluateDialogs& evaluateDialogs = *st.evaluateDialogs;
        // Evaluate > Profiler: global, document-independent — handle before any lockout so it can be
        // toggled at any time (including mid-edit, which is exactly when you want to profile).
        if (std::strcmp(cmd_id, "prof_toggle") == 0) { st.showProfilerWindow = true; return; }
        // Simulation lockout: while the Simulation workspace is active the model is frozen — a study's
        // fixtures/loads/results are tied to the current geometry, so changing the part's shape from
        // under them is disallowed (SolidWorks freezes the model inside a study the same way). We block
        // the feature/sketch/assembly/edit verbs but leave simulation (sim_*), inspection (eval_*) and
        // view/camera commands free so the user can still measure, orient and set up the study. The
        // toast tells them the one move that unlocks editing: leave the Simulation tab.
        if (st.inSimulation()) {
            std::string c(cmd_id);
            auto pre = [&](const char* p) { return c.rfind(p, 0) == 0; };
            const bool allowed =
                pre("sim_") || pre("eval_") || pre("view_") || pre("zoom") || pre("cam_") ||
                pre("display_") || pre("section") || pre("rollback") || pre("config_") ||
                c == "toggle_units" || c == "make_drawing";
            const bool modeling =
                pre("sketch") || pre("sk_") || pre("smart_dim") || pre("extrude") || pre("revolve") ||
                pre("fillet") || pre("chamfer") || pre("shell") || pre("draft") || pre("hole") ||
                pre("sweep") || pre("loft") || pre("boss") || pre("cut") || pre("pattern") ||
                pre("mirror") || pre("rib") || pre("dome") || pre("wrap") || pre("split") ||
                pre("combine") || pre("scale") || pre("move") || pre("boolean") || pre("helix") ||
                pre("ref_") || pre("datum") || pre("plane") || pre("axis") || pre("weld") ||
                pre("sm_") || pre("asm_") || pre("edit_") || c == "new_assembly";
            if (modeling && !allowed) {
                st.toast(ui::ToastLevel::Info,
                         "Model is locked in Simulation — leave the Simulation tab to edit it");
                return;
            }
        }
        // C43: starting a sketch while a PM form is open cancels the form first (revert
        // preview + close). Without this, plane-pick and the PM coexisted: the PM hid the
        // FeatureManager, both fought over the viewport mouse, and Esc couldn't untangle it.
        if (propertyManager.active() && std::strcmp(cmd_id, "sketch_new") == 0)
            propertyManager.cancelEdits(st);
        if (sketchMode.onRibbonCommand(st, cmd_id)) return;       // C18 sketch tool handled
        // 3D sketch entry point + exit (all in-mode tools live on the viewport overlay toolbar).
        if (st.sketch3dMode && st.sketch3dMode->onRibbonCommand(st, cmd_id)) return;
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
        // W17 drawing overhaul: View Layout / Annotation ribbon commands route to the 2D panel.
        if (std::strncmp(cmd_id, "dwg_", 4) == 0 && ui::drawingHandleCommand(st, std::string(cmd_id)))
            return;
        // ---- Appearance tab: Display group (cheap — viewport applies st.displayStyle each frame) ----
        {
            int ds = -1;
            if      (std::strcmp(cmd_id, "display_shaded")       == 0) ds = 0;
            else if (std::strcmp(cmd_id, "display_shaded_edges") == 0) ds = 1;
            else if (std::strcmp(cmd_id, "display_hlr")          == 0) ds = 2;
            else if (std::strcmp(cmd_id, "display_hlv")          == 0) ds = 3;
            else if (std::strcmp(cmd_id, "display_wireframe")    == 0) ds = 4;
            if (ds >= 0) { st.displayStyle = ds; return; }
        }
        // ---- Appearance tab: Material group (set_appearance on body "b1") ----
        if (std::strcmp(cmd_id, "appr_color") == 0) { st.requestAppearanceEditor = true; return; }
        if (std::strcmp(cmd_id, "appr_clear") == 0) { applyModelAppearance(st, nullptr, 0.0f, 0.5f); return; }
        {
            int mi = -1;
            if      (std::strcmp(cmd_id, "appr_mat_plastic")  == 0) mi = 0;
            else if (std::strcmp(cmd_id, "appr_mat_aluminum") == 0) mi = 1;
            else if (std::strcmp(cmd_id, "appr_mat_steel")    == 0) mi = 2;
            else if (std::strcmp(cmd_id, "appr_mat_brass")    == 0) mi = 3;
            else if (std::strcmp(cmd_id, "appr_mat_dark")     == 0) mi = 4;
            if (mi >= 0) {
                const ui::MaterialPreset& m = ui::kMaterials[mi];
                applyModelAppearance(st, m.rgb, m.metallic, m.roughness);
                return;
            }
        }
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
        // CAM (Manufacture workspace): cam_* commands route to the CAM manager panel.
        {
            std::string cc(cmd_id);
            if (cc.rfind("cam_", 0) == 0) { ui::camHandleCommand(st, cc); return; }
        }
        if (propertyManager.openForCommand(st, cmd_id)) return;  // form opened
        // C19: Simulation tab -> sim panel; Evaluate tab -> evaluate dialogs.
        std::string cmd(cmd_id);
        if (cmd == "sm_flat_pattern") { applyFlatPattern(st); return; }  // W16: one-click Flatten
        if (cmd == "sm_export_flat_dxf") { exportFlatDxf(st); return; }  // W14: flat-pattern DXF
        if (cmd.rfind("sim_", 0) == 0) {
            st.showSimulationTools = true;
            if (st.simManager && st.simManager->handleCommand(st, cmd)) return;
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
    st.orthographic = st.settings.orthographic;  // live projection mode mirrors the persisted setting
    // Liquid glass v2.1: install + resolve the persisted appearance config before the first frame
    // (idempotent; re-pushed every frame in the loop below so runtime mutations self-heal).
    ui::setGlassConfig(st.settings.glassCfg);
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
        bool simPickConsumed = false;  // a sim face pick was routed into a fixture/load well this pass
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
            // SolidWorks "fillet a whole face": a FACE pick into an active fillet/chamfer edge well
            // expands into ALL of that face's edges. Resolve each edge to a selector and route it,
            // then we're done with this pick (skip the normal single-element resolve below).
            if (item.hit.kind == render::PickKind::Face && st.propertyManager &&
                st.propertyManager->active() &&
                st.propertyManager->activeWellExpandsFaceToEdges()) {
                auto* pm = st.propertyManager;
                std::vector<uint32_t> eids =
                    st.docMirror.faceEdges(st.worker, item.hit.body_id, item.hit.topo_id);
                int added = 0;
                for (uint32_t eid : eids) {
                    std::string eerr;
                    std::string esel = st.docMirror.resolvePick(
                        st.worker, item.hit.body_id, "edge", eid, eerr);
                    if (esel.empty()) continue;
                    try {
                        nlohmann::json eobj = nlohmann::json::parse(esel);
                        if (pm->routePick(eobj, (int)render::PickKind::Edge, item.hit.body_id, eid))
                            ++added;
                    } catch (...) {}
                }
                st.addLog("[pm] face fillet: expanded face " + std::to_string(item.hit.topo_id) +
                          " -> " + std::to_string(added) + " edge(s)");
                item.pending_resolve = false;
                continue;
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
                            const char* kn = item.hit.kind == render::PickKind::Edge ? "edge" :
                                             item.hit.kind == render::PickKind::Face ? "face" :
                                             item.hit.kind == render::PickKind::Vertex ? "vertex" : "?";
                            bool routed = pm->routePick(selObj, (int)item.hit.kind,
                                                        item.hit.body_id, item.hit.topo_id);
                            st.addLog(std::string("[pm] click kind=") + kn +
                                      " body=" + std::to_string(item.hit.body_id) +
                                      " topo=" + std::to_string(item.hit.topo_id) +
                                      " routed=" + (routed ? "1" : "0") +
                                      " sel=" + sel.substr(0, 120));
                            if (!routed) {
                                std::string hint = pm->activeWellKindHint();
                                if (!hint.empty())
                                    st.toast(ui::ToastLevel::Info,
                                             "This field takes " + hint + " — that pick wasn't added");
                            }
                        } catch (...) {}
                    }
                }
                // Simulation studies: route a FACE pick into the armed fixture/load well of the
                // active study (only when the Simulation tab is up and no PM form owns picks — the
                // mutual-exclusion guard lives in SimManager::wantsViewportFacePick).
                if (st.simManager && st.simManager->wantsViewportFacePick(st)) {
                    if (item.hit.kind == render::PickKind::Face) {
                        try {
                            nlohmann::json selObj = nlohmann::json::parse(sel);
                            if (st.simManager->routeFacePick(st, selObj, item.hit.body_id,
                                                             item.hit.topo_id))
                                simPickConsumed = true;
                        } catch (...) {}
                    } else {
                        // The click resolved to an edge/vertex while a fixture/load well is armed —
                        // tell the user instead of silently dropping it.
                        st.toast(ui::ToastLevel::Info,
                                 "Fixtures and loads take faces — click a model face");
                    }
                }
            } else if (!err.empty()) {
                // Log once (avoid flooding; resolvePick logs stale_revision internally).
            }
        }
        // A routed sim face pick belongs to the fixture/load WELL (shown as the green/red face wash),
        // not the global selection. Clear st.selection so sim picks never ACCUMULATE here and get
        // scooped into the next well's capture (the fixture→force carryover). Safe here: we're past
        // the iteration, and clear()'s re-entrant notify just re-runs this observer over an empty list.
        if (simPickConsumed) st.selection.clear();
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
        // Push feature datum geometry (axes/planes/points/csys) to the renderer so CREATED reference
        // geometry is visible — datums build no body, so the engine ships them out-of-band in the
        // ops-response. Tolerant of missing fields / an engine that doesn't send "datums" (the array
        // is then empty and nothing is drawn).
        if (st.renderer) {
            auto v3 = [](const nlohmann::json& a, Vec3 dflt) -> Vec3 {
                if (a.is_array() && a.size() == 3 && a[0].is_number())
                    return Vec3((float)a[0].get<double>(), (float)a[1].get<double>(),
                                (float)a[2].get<double>());
                return dflt;
            };
            std::vector<Renderer::RefDatum> rds;
            for (const auto& d : st.docMirror.refDatums()) {
                if (!d.is_object()) continue;
                Renderer::RefDatum rd;
                const std::string k = d.value("kind", std::string());
                if (k == "axis") {
                    rd.kind = Renderer::RefDatum::Kind::Axis;
                    rd.origin = v3(d.value("point", nlohmann::json()), Vec3(0, 0, 0));
                    rd.dir    = v3(d.value("dir", nlohmann::json()), Vec3(0, 0, 1));
                } else if (k == "plane" || k == "csys") {
                    rd.kind = (k == "csys") ? Renderer::RefDatum::Kind::Csys
                                            : Renderer::RefDatum::Kind::Plane;
                    rd.origin = v3(d.value("origin", nlohmann::json()), Vec3(0, 0, 0));
                    rd.dir    = v3(d.value("normal", nlohmann::json()), Vec3(0, 0, 1));
                    rd.xdir   = v3(d.value("x_dir", nlohmann::json()), Vec3(1, 0, 0));
                    rd.ydir   = v3(d.value("y_dir", nlohmann::json()), Vec3(0, 1, 0));
                } else if (k == "point") {
                    rd.kind = Renderer::RefDatum::Kind::Point;
                    rd.origin = v3(d.value("point", nlohmann::json()), Vec3(0, 0, 0));
                } else if (k == "curve") {
                    // A helix / spiral / spline path: a sampled world polyline that the
                    // renderer draws and the path picker hit-tests as a sweep path.
                    rd.kind = Renderer::RefDatum::Kind::Curve;
                    if (d.contains("points") && d["points"].is_array()) {
                        rd.points.reserve(d["points"].size());
                        for (const auto& p : d["points"])
                            rd.points.push_back(v3(p, Vec3(0, 0, 0)));
                    }
                    if (rd.points.size() < 2) continue;  // nothing to draw / pick
                } else {
                    continue;
                }
                rd.id = d.value("id", std::string());
                rd.name = d.value("name", std::string());
                rds.push_back(rd);
            }
            // T-BL37: inject the synthetic world-origin point datum so the renderer draws
            // a point glyph at (0,0,0). Eye-toggle in the FeatureManager hides/shows it
            // via hiddenFeatures — same mechanism as every user-created datum feature.
            {
                Renderer::RefDatum originPt;
                originPt.kind   = Renderer::RefDatum::Kind::Point;
                originPt.origin = Vec3(0.0f, 0.0f, 0.0f);
                originPt.id     = "point.origin";
                originPt.name   = "Origin";
                rds.push_back(originPt);
            }
            // W2-ref: inject default X/Y/Z axes + auto plane-intersection axes (GUI-synthetic,
            // like point.origin). User axis features already present in refDatums win on id.
            // Default axis.x/y/z are PREDEFINED constants — never "solved". Reserved plane-pair
            // intersections ARE those three axes; never emit axis.auto for reserved-only pairs.
            // User-plane auto axes are cached by plane-set fingerprint (skip O(P²) when unchanged).
            {
                std::unordered_set<std::string> occupied;
                for (const auto& rd : rds)
                    if (!rd.id.empty()) occupied.insert(rd.id);

                auto pushAxis = [&](const std::string& id, const std::string& name,
                                    const Vec3& origin, const Vec3& dir) {
                    if (occupied.count(id)) return;
                    Renderer::RefDatum ax;
                    ax.kind   = Renderer::RefDatum::Kind::Axis;
                    ax.origin = origin;
                    ax.dir    = dir;
                    ax.id     = id;
                    ax.name   = name;
                    rds.push_back(ax);
                    occupied.insert(id);
                };

                // Reserved world axes through origin (contracts/doc_schema.md §5) — constants.
                pushAxis("axis.x", "X Axis", Vec3(0, 0, 0), Vec3(1, 0, 0));
                pushAxis("axis.y", "Y Axis", Vec3(0, 0, 0), Vec3(0, 1, 0));
                pushAxis("axis.z", "Z Axis", Vec3(0, 0, 0), Vec3(0, 0, 1));

                // User planes only for the auto-axis fingerprint. Reserved Front/Top/Right are
                // pair partners for user planes but never pair with each other as axis.auto
                // (those three intersections are axis.x/y/z).
                struct PlaneSrc { std::string id; std::string name; Vec3 origin; Vec3 normal; bool reserved = false; };
                std::vector<PlaneSrc> userPlanes;
                for (const auto& d : st.docMirror.refDatums()) {
                    if (!d.is_object() || d.value("kind", std::string()) != "plane") continue;
                    const std::string pid = d.value("id", std::string());
                    if (pid.empty() || pid == "plane.front" || pid == "plane.top" ||
                        pid == "plane.right")
                        continue;
                    PlaneSrc ps;
                    ps.id     = pid;
                    ps.name   = d.value("name", pid);
                    ps.origin = v3(d.value("origin", nlohmann::json()), Vec3(0, 0, 0));
                    ps.normal = v3(d.value("normal", nlohmann::json()), Vec3(0, 0, 1));
                    if (length(ps.normal) < 1e-9f) continue;
                    ps.normal = normalize(ps.normal);
                    userPlanes.push_back(std::move(ps));
                }

                // Fingerprint = sorted "id@ox,oy,oz,nx,ny,nz" joined with ';' for user planes.
                std::vector<std::string> fpParts;
                fpParts.reserve(userPlanes.size());
                for (const auto& p : userPlanes) {
                    char buf[192];
                    std::snprintf(buf, sizeof(buf),
                                  "%s@%.6g,%.6g,%.6g,%.6g,%.6g,%.6g",
                                  p.id.c_str(),
                                  (double)p.origin.x, (double)p.origin.y, (double)p.origin.z,
                                  (double)p.normal.x, (double)p.normal.y, (double)p.normal.z);
                    fpParts.emplace_back(buf);
                }
                std::sort(fpParts.begin(), fpParts.end());
                std::string fp;
                for (size_t i = 0; i < fpParts.size(); ++i) {
                    if (i) fp += ';';
                    fp += fpParts[i];
                }

                if (fp != st.axisInjectPlaneFp) {
                    // Recompute: reserved + user, skip reserved-only pairs and parallel pairs.
                    st.axisAutoCache.clear();
                    std::vector<PlaneSrc> planes;
                    planes.reserve(3 + userPlanes.size());
                    planes.push_back({"plane.front", "Front", Vec3(0, 0, 0), Vec3(0, -1, 0), true});
                    planes.push_back({"plane.top",   "Top",   Vec3(0, 0, 0), Vec3(0,  0, 1), true});
                    planes.push_back({"plane.right", "Right", Vec3(0, 0, 0), Vec3(1,  0, 0), true});
                    for (const auto& up : userPlanes)
                        planes.push_back(up);

                    const float kParEps = 1e-6f;
                    for (size_t i = 0; i < planes.size(); ++i) {
                        for (size_t j = i + 1; j < planes.size(); ++j) {
                            const PlaneSrc& a = planes[i];
                            const PlaneSrc& b = planes[j];
                            // Reserved∩reserved → axis.x/y/z; never emit axis.auto.
                            if (a.reserved && b.reserved) continue;
                            Vec3 dir = cross(a.normal, b.normal);
                            const float dirLen = length(dir);
                            if (dirLen < kParEps) continue;  // parallel — no unique line
                            dir = dir * (1.0f / dirLen);
                            const float d1 = dot(a.normal, a.origin);
                            const float d2 = dot(b.normal, b.origin);
                            const float det2 = dirLen * dirLen;
                            Vec3 pt = (cross(b.normal, dir * dirLen) * d1 +
                                       cross(dir * dirLen, a.normal) * d2) * (1.0f / det2);
                            std::string idA = a.id, idB = b.id;
                            if (idB < idA) std::swap(idA, idB);
                            AppState::AutoAxisCache ent;
                            ent.id = "axis.auto:" + idA + "|" + idB;
                            // Name uses original a/b display names (not id-sorted).
                            ent.name = "Axis " + a.name + " \u2229 " + b.name;
                            ent.origin[0] = pt.x; ent.origin[1] = pt.y; ent.origin[2] = pt.z;
                            ent.dir[0] = dir.x;   ent.dir[1] = dir.y;   ent.dir[2] = dir.z;
                            st.axisAutoCache.push_back(std::move(ent));
                        }
                    }
                    st.axisInjectPlaneFp = std::move(fp);
                }

                for (const auto& ent : st.axisAutoCache) {
                    pushAxis(ent.id, ent.name,
                             Vec3(ent.origin[0], ent.origin[1], ent.origin[2]),
                             Vec3(ent.dir[0], ent.dir[1], ent.dir[2]));
                }

                // Rebuild axisDatumIds; auto-hide any newly-seen synthetic axis (default + auto).
                // User-created axis features are left alone (visible unless the user hid them).
                // Membership in the previous axisDatumIds set is the per-doc "ever seen" marker —
                // once the user unhides an id it stays unhidden across rebuilds.
                std::unordered_set<std::string> prevAxisIds = std::move(st.axisDatumIds);
                st.axisDatumIds.clear();
                for (const auto& rd : rds) {
                    if (rd.kind != Renderer::RefDatum::Kind::Axis || rd.id.empty()) continue;
                    st.axisDatumIds.insert(rd.id);
                    const bool isSynthetic =
                        rd.id == "axis.x" || rd.id == "axis.y" || rd.id == "axis.z" ||
                        rd.id.rfind("axis.auto:", 0) == 0;
                    if (isSynthetic && !prevAxisIds.count(rd.id))
                        st.hiddenFeatures.insert(rd.id);
                }
                // Rebuild display names when axis set membership changes (or first fill).
                bool membershipChanged =
                    st.axisDisplayNames.empty() ||
                    st.axisDatumIds.size() != prevAxisIds.size();
                if (!membershipChanged) {
                    for (const auto& id : st.axisDatumIds) {
                        if (!prevAxisIds.count(id)) { membershipChanged = true; break; }
                    }
                }
                if (membershipChanged) {
                    st.axisDisplayNames.clear();
                    for (const auto& rd : rds) {
                        if (rd.kind != Renderer::RefDatum::Kind::Axis || rd.id.empty()) continue;
                        st.axisDisplayNames[rd.id] = rd.name.empty() ? rd.id : rd.name;
                    }
                }
            }
            st.renderer->setRefDatums(std::move(rds));
            st.applyDatumVisibility();   // re-apply the FeatureManager eye hides to the fresh datums
        }
        // Document tabs: a real edit (applyOps/undo/redo/pushPull/revert) lights the active tab's
        // unsaved-changes marker. Tab switches / fresh-doc activation / resyncs set suppressTabDirty
        // so they don't falsely dirty a tab. Keep the cached title in step with renames, too.
        // Also skip when the mirror has already re-pointed at a different doc than the active tab
        // (open-second-file race: adoptOpenResponse notifies before applyOpenedDoc adds the tab).
        // markActiveTabDirty / syncActiveTabTitle each guard on docId as well — belt and braces.
        if (!st.suppressTabDirty) {
            const bool activeMatchesMirror =
                st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
                st.docTabs[st.activeTab].docId == st.docMirror.docId();
            if (activeMatchesMirror) {
                st.markActiveTabDirty();
                st.syncActiveTabTitle();
            }
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

#ifndef _WIN32
    // On Linux/macOS the backend is the in-process C++ core (libaicad), not a managed Python
    // venv — route through tryStartWorker, whose libaicad guard reports readiness and only
    // falls back to Python provisioning if libaicad is genuinely unavailable.
    st.tryStartWorker();
#else
    if (st.rtReady) {
        st.tryStartWorker();
    } else {
        st.status = "Runtime not installed";
        st.statusKind = 2;
        st.addLog("[!] runtime not found at %APPDATA%\\AI-CAD\\runtime");
        st.autoSetupTried = true;
        st.startSetup(false);  // self-provision from the payload next to the exe / source tree
    }
#endif

    // NOTE: the old "welcome model" (mounting_plate.stl into the bare renderer) is gone — it
    // contradicted the empty Part1 document (a mesh with no tree features), occluded the
    // plane-pick quads, and inflated the empty-scene camera radius. Startup now shows the
    // clean empty-doc state: origin planes + triad, like a fresh SolidWorks part.

    std::strncpy(st.code.data(), kStarterCode, st.code.size() - 1);
    std::strncpy(st.prompt.data(), "a knurled hex standoff, 22mm tall, M3 clearance bore", st.prompt.size() - 1);
    {
        ChatMsg w;
        w.text = "Hi! Describe a part and I'll model it, show my work as I go, and check the "
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
    // H2: daemon reconnect resilience — kill hostd, capture unreachable chip, restart, capture tabs.
    st.wantDaemonH2 = shot && shotMode && std::strcmp(shotMode, "daemon_h2") == 0;
    if (st.wantDaemonH2) { st.showRibbon = true; st.showFeatureTree = true; }
    st.wantM1 = shot && shotMode && std::strcmp(shotMode, "m1") == 0;  // C17 integration scenario
    // Multi-document mid-edit scenario: open a PM form on Part A, switch to Part B and measure,
    // switch back — assert A's form/selection/hidden-body survive and B is independent (DocSession).
    st.wantMultidoc = shot && shotMode && std::strcmp(shotMode, "multidoc") == 0;
    if (st.wantMultidoc) { st.showRibbon = true; st.showFeatureTree = true; }
    // Tab-switch perf harness: 2 real parts + many A<->B switches; logTabPerf prints to stderr.
    st.wantTabPerf = shot && shotMode && std::strcmp(shotMode, "tabperf") == 0;
    if (st.wantTabPerf) { st.showRibbon = true; st.showFeatureTree = true; }
    st.wantEcad = shot && shotMode && std::strcmp(shotMode, "ecad") == 0;  // Wave N: ECAD GUI shot
    if (st.wantEcad) { st.showPcbTools = true; st.showRibbon = true; st.ribbonTab = AppState::RT_PCB; }  // PCB tab forward
    st.wantSketch = shot && shotMode && std::strcmp(shotMode, "sketch") == 0;  // C18 sketch flow
    if (st.wantSketch) { st.showRibbon = true; st.ribbonTab = AppState::RT_Sketch; }  // Sketch tab forward
    st.wantSketch3d = shot && shotMode && std::strcmp(shotMode, "sketch3d") == 0;  // 3D sketcher flow
    if (st.wantSketch3d) { st.showRibbon = true; st.showFeatureTree = true; }
    st.wantFea = shot && shotMode && std::strcmp(shotMode, "fea") == 0;       // C19 static study
    // T-FIN24 disclosure-matrix shots (Simulate workspace study picker).
    st.wantSimDisc = shot && shotMode && (std::strcmp(shotMode, "simdisc_mech") == 0 ||
                                          std::strcmp(shotMode, "simdisc_ecad") == 0);
    if (st.wantSimDisc) {
        st.simDiscKind = std::strcmp(shotMode, "simdisc_ecad") == 0 ? 1 : 0;
        st.showSimulationTools = true;
        st.showRibbon = true;
        st.ribbonTab = AppState::RT_Simulation;
        st.leftPaneTab = AppState::LP_Simulation;
    }
    st.wantCam = shot && shotMode && std::strcmp(shotMode, "cam") == 0;       // CAM copilot fast-path
    if (st.wantCam) { st.showRibbon = true; st.ribbonTab = AppState::RT_Manufacture; }
    st.wantFlow = shot && shotMode && std::strcmp(shotMode, "flow") == 0;     // external-flow study
    // W17: engineering-drawing scenario — part w/ a hole -> Make Drawing -> Model Items + hole
    // callout + a critical toleranced dim + title block, with the contextual drawing ribbon shown.
    st.wantDrawing = shot && shotMode && std::strcmp(shotMode, "drawing") == 0;
    if (st.wantDrawing) { st.showRibbon = true; st.showFeatureTree = true; }
    st.wantEvalMass = shot && shotMode && std::strcmp(shotMode, "evalmass") == 0;  // C19 mass props
    st.wantIconPipe = shot && shotMode && std::strcmp(shotMode, "iconpipe") == 0;  // T-G15 icon render
    if (st.wantFea || st.wantFlow) {
        st.showSimulationTools = true;
        st.showRibbon = true;
        st.ribbonTab = AppState::RT_Simulation;
        st.leftPaneTab = AppState::LP_Simulation;
    }
    // C22b: activity shot — new part doc -> agent_run_v2 -> Activity panel -> Settings Agents tab
    st.wantActivity = shot && shotMode && std::strcmp(shotMode, "activity") == 0;
    if (st.wantActivity) { st.showActivityPanel = true; }
    // [workspace-tabs seam] workspaces shot — cycle all 8 workspace tabs, one screenshot each.
    st.wantWorkspace = shot && shotMode && std::strcmp(shotMode, "workspaces") == 0;
    if (st.wantWorkspace) { st.showRibbon = true; }
    // AI_CAD_SHOT=motion: kinematic motion study end-to-end (2-body asm + angle mate + playback).
    st.wantMotion = shot && shotMode && std::strcmp(shotMode, "motion") == 0;
    if (st.wantMotion) { st.showRibbon = true; st.activeWorkspace = AppState::Workspace::Simulate; }
    // AI_CAD_SHOT=aiws: open the AI workspace full-body hub for headless visual verification.
    if (shot && shotMode && std::strcmp(shotMode, "aiws") == 0) {
        st.showRibbon = true; st.activeWorkspace = AppState::Workspace::AI;
    }
    // AI_CAD_SHOT=firmware: open the Firmware workspace with the canned project + build
    // console + emulation UART log + GPIO trace fixture — headless visual verification of
    // T-FIN5 (project visible, build console output, emulation trace rendered). fw_status /
    // fw_projects still hit the live worker, so the backend rail shows the REAL doctor state.
    if (shot && shotMode && std::strcmp(shotMode, "firmware") == 0) {
        st.showRibbon = true; st.activeWorkspace = AppState::Workspace::Firmware;
        ui::firmware::seedFirmwareShotFixture();
    }
    // AI_CAD_SHOT=reveng: open the Reverse Engineering full-body workspace shell (RE-03) for
    // headless visual verification of the new tab. Same inline flag pattern as aiws — the
    // generic per-frame shot capture grabs it (no scenario state machine needed).
    if (shot && shotMode && std::strcmp(shotMode, "reveng") == 0) {
        st.showRibbon = true; st.activeWorkspace = AppState::Workspace::ReverseEng;
    }
    // AI_CAD_SHOT=reveng_review: open the RE workspace on the Review stage with a canned
    // confirm_dimensions payload + deviation result — headless visual verification of RE-22.
    if (shot && shotMode && std::strcmp(shotMode, "reveng_review") == 0) {
        st.showRibbon = true; st.activeWorkspace = AppState::Workspace::ReverseEng;
        ui::reveng::seedCannedDimPayload();
        // Force the Review stage so the center panel shows dim-review + deviation.
        ui::reveng::S().stage = ui::reveng::Stage::Review;
    }
    // AI_CAD_SHOT=reveng_capture: open RE workspace on the Source stage (RE-19 capture suite
    // + webcam widget + image gallery) for headless visual verification of RE-19 panels.
    if (shot && shotMode && std::strcmp(shotMode, "reveng_capture") == 0) {
        st.showRibbon = true; st.activeWorkspace = AppState::Workspace::ReverseEng;
        ui::reveng::S().stage = ui::reveng::Stage::Source;
    }
    // AI_CAD_SHOT=reveng_recon: open RE workspace on the Reconstruct stage with the canned
    // sparse fixture + recon.done seeded; shows sub-stage stepper + splat indicator (RE-21/RE-27).
    if (shot && shotMode && std::strcmp(shotMode, "reveng_recon") == 0) {
        st.showRibbon = true; st.activeWorkspace = AppState::Workspace::ReverseEng;
        {
            ui::reveng::RevEngState& rs = ui::reveng::S();
            rs.stage      = ui::reveng::Stage::Reconstruct;
            rs.recon.done = true;
            rs.recon.pct  = 100;
        }
        ui::reveng::seedSplatFixture();
    }
    // AI_CAD_SHOT=parts: open the Parts Library workspace with a synthetic no-model
    // result row so the "Generate 3D model" action (RE-32) is visible in the shot.
    if (shot && shotMode && std::strcmp(shotMode, "parts") == 0) {
        st.showRibbon = true; st.activeWorkspace = AppState::Workspace::PartsLibrary;
        st.wantPartsShot = true;
    }
    // AI_CAD_SHOT=reveng_capture: RE-19 capture suite — webcam widget + coverage ring +
    // image gallery rendered with stub camera (no device required).
    if (shot && shotMode && std::strcmp(shotMode, "reveng_capture") == 0) {
        st.showRibbon = true; st.activeWorkspace = AppState::Workspace::ReverseEng;
    }
    // AI_CAD_SHOT=reveng_recon: open the RE workspace on the Reconstruct stage with canned
    // sparse fixture + progress state — headless visual verification of RE-21.
    if (shot && shotMode && std::strcmp(shotMode, "reveng_recon") == 0) {
        st.showRibbon = true; st.activeWorkspace = AppState::Workspace::ReverseEng;
        ui::reveng::S().stage = ui::reveng::Stage::Reconstruct;
        // Seed canned session so the rail + center show a real-looking entry.
        ui::reveng::SessionSummary ss;
        ss.session_id = "re_canned_recon";
        ss.name       = "Bracket scan (canned)";
        ss.status     = "reconstructing";
        ss.images     = 12;
        ui::reveng::S().sessions.push_back(ss);
        ui::reveng::S().selected = 0;
        ui::reveng::S().listedOnce = true;
        // Seed reconstruct progress at 45% (mid-dense phase).
        ui::reveng::RevEngEvent ev;
        ev.kind = "reconstruct_progress";
        ev.pct  = 45;
        ev.msg  = "dense";
        ui::reveng::feedRevEngEvent(ev);
    }
    // AI_CAD_SHOT=git: open the PLM hub on the new Git page for headless verification.
    if (shot && shotMode && std::strcmp(shotMode, "git") == 0) {
        st.showRibbon = true; st.activeWorkspace = AppState::Workspace::PLM;
        const char* pg = std::getenv("AI_CAD_PLM_PAGE");
        if (pg && std::strcmp(pg, "planning") == 0) ui::plmSelectPlanningPage();
        else if (pg && std::strcmp(pg, "overview") == 0) ui::plmSelectOverviewPage();
        else if (pg && std::strcmp(pg, "files") == 0) ui::plmSelectFilesPage();
        else ui::plmSelectGitPage();
        if (st.plmProjectDir.empty()) {
            std::error_code ec;
            auto cwd = std::filesystem::current_path(ec);
            st.plmProjectDir = ec ? std::string(".") : cwd.string();
        }
    }
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
    // ribbon shot: show Features tab by default; AI_CAD_TAB=<0..12> selects which tab to capture
    // (0=Features 1=Sketch 2=Evaluate 3=Simulation 4=Assembly 5=Sheet Metal 6=Weldments 7=PCB
    // 8=Surfaces .. 11=Appearance 12=Manufacture) for headless per-tab verification.
    if (st.wantRibbon) {
        st.showRibbon = true; st.ribbonTab = AppState::RT_Features;
        // T-BL31 NEW-3: the Billet on-load toast pins over the ribbon's top-left and occluded
        // icon verification (weld_gusset, appr_mat_aluminum/steel in the T-G12 round-2 walkthrough).
        // Suppress toasts for per-tab ribbon captures by default; AI_CAD_SHOT_KEEPTOAST=1 re-enables
        // them (for verifying the toast's own position/theming against the ribbon).
        if (!(std::getenv("AI_CAD_SHOT_KEEPTOAST") && *std::getenv("AI_CAD_SHOT_KEEPTOAST")))
            st.suppressToasts = true;
        else
            st.toast(ui::ToastLevel::Info,
                     "Billet: live view of the AI's part — it updates as it builds");
        if (const char* tabEnv = std::getenv("AI_CAD_TAB")) {
            int t = std::atoi(tabEnv);
            if (t >= 0 && t <= AppState::RT_Manufacture) st.ribbonTab = t;
        }
        // T-G15: named alias for AI_CAD_TAB so in-context icon verification reads self-documenting
        // (e.g. AI_CAD_RIBBON_TAB=appearance). Also unblocks T-G16's per-tab ribbon-glass captures.
        if (const char* tn = std::getenv("AI_CAD_RIBBON_TAB")) {
            const std::string t = tn;
            int idx = -1;
            if      (t == "features")   idx = AppState::RT_Features;
            else if (t == "sketch")     idx = AppState::RT_Sketch;
            else if (t == "evaluate")   idx = AppState::RT_Evaluate;
            else if (t == "simulation") idx = AppState::RT_Simulation;
            else if (t == "assembly")   idx = AppState::RT_Assembly;
            else if (t == "sheetmetal") idx = AppState::RT_SheetMetal;
            else if (t == "weldments")  idx = AppState::RT_Weldments;
            else if (t == "pcb")        idx = AppState::RT_PCB;
            else if (t == "surfaces")   idx = AppState::RT_Surfaces;
            else if (t == "appearance") idx = AppState::RT_Appearance;
            else if (t == "manufacture")idx = AppState::RT_Manufacture;
            if (idx >= 0) st.ribbonTab = idx;
            else { std::fprintf(stderr, "AI_CAD_RIBBON_TAB: unknown tab '%s'\n", tn); }
        }
    }
    // pmform shot: open a PropertyManager form headlessly for visual verification of its field
    // layout/sizing. AI_CAD_PMCMD=<cmd_id> (e.g. "extrude", "surf_loft") selects which form; the
    // left pane auto-switches to the PropertyManager tab when a form is active (see left_pane.cpp).
    // openForCommand is safe pre-worker: with no doc the checkpoint is skipped and field init is pure.
    if (shot && shotMode && std::strcmp(shotMode, "pmform") == 0) {
        st.showRibbon = true;
        st.showFeatureTree = true;  // ensure the left pane (which hosts the PM tab) is visible
        // T-G14 verification harness: optionally load a part into the viewport so the docked
        // panels sample a LIVE (light) backdrop — that regime (adaptive DARK text over the
        // light viewport) is where Erik's dark-mode black-on-black actually manifests; without
        // a loaded doc the panes fall back to the theme palette (light text) and hide the bug.
        // Gated on AI_CAD_PMBOX=1 so the default pmform shot is unchanged.
        if (const char* box = std::getenv("AI_CAD_PMBOX"); box && *box && std::strcmp(box, "0") != 0) {
            std::string aicm;
            const char* candidates[] = {
                "contracts/fixtures/box.aicm",
                "../contracts/fixtures/box.aicm",
                "../../contracts/fixtures/box.aicm",
            };
            for (const char* c : candidates) {
                FILE* f = std::fopen(c, "rb");
                if (f) { std::fclose(f); aicm = c; break; }
            }
            std::string lerr;
            if (!aicm.empty() && st.loadDocMesh(aicm, lerr)) {
                st.yaw = 35; st.pitch = 28; st.zoom = 1.0f;  // iso so the box fills the viewport
            } else {
                std::fprintf(stderr, "pmform: AI_CAD_PMBOX load failed (%s): %s\n",
                             aicm.empty() ? "not found" : aicm.c_str(), lerr.c_str());
            }
        }
        if (const char* cmd = std::getenv("AI_CAD_PMCMD"))
            if (*cmd && !st.propertyManager->openForCommand(st, cmd))
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
                    st.propertyManager->presetFieldForShot(tok.substr(0, eq), tok.substr(eq + 1));
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
        const std::string asmDir = appdataAppDir() + "/cache/screenshot_asm/";
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
        // Transparent activity timeline — what the AI did, expandable. Mirrors the live
        // orchestrator stages so the screenshot exercises the new inline activity surface.
        {
            ActivityItem i0; i0.stage = "intake"; i0.title = "Understanding the request";
            i0.status = "pass"; i0.reason = "Edit the open hex standoff: 30mm tall + chamfered top.";
            i0.detail = "User wants the existing part taller with a chamfered top rim; this is an "
                        "edit to the open document, not a new part.";
            i0.durationMs = 900; a.activity.push_back(i0);
            ActivityItem i1; i1.stage = "plan"; i1.title = "Planning the build";
            i1.status = "pass"; i1.reason = "Change total_h 22->30, add 1mm chamfer on top edges.";
            i1.durationMs = 1400; a.activity.push_back(i1);
            ActivityItem i2; i2.stage = "edit"; i2.title = "Editing the model";
            i2.status = "pass"; i2.opsCount = 3; i2.durationMs = 2600;
            i2.detail = "Set height=30; selected top perimeter edges; applied chamfer length=1.0.";
            a.activity.push_back(i2);
            ActivityItem i3; i3.stage = "review"; i3.title = "Reviewing the result";
            i3.status = "pass"; i3.reason = "Rebuilds clean; height and chamfer match.";
            i3.durationMs = 1100; a.activity.push_back(i3);
        }
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
    ui::ScenarioRunner scenarios(activityPanel);

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
        // T-PERF10: idle render throttle. When nothing needs continuous redraw (no animation,
        // no async RPCs, no camera tween, no headless shot), wait up to 50ms for an event
        // instead of spinning at full vsync and burning a core. Input still wakes immediately.
        // CEF's pump_if_due below still runs every frame after wait; when browsers are live the
        // 20 Hz floor is enough for its deadline path, and note_browser_* still forces work.
        {
            const bool busy =
                st.animActive || st.rebuildsInFlight > 0 || st.rpc.busy() ||
                (st.simClient && st.simClient->busy());
            if (busy || shot) {
                glfwPollEvents();
            } else {
                glfwWaitEventsTimeout(0.05);  // 20 Hz idle floor
            }
        }
#ifdef AI_CAD_ENABLE_CEF
        ui::cef::pump_if_due();   // drive CEF's message loop when its deadline is due
        {
            static const char* dbgVendor = std::getenv("AI_CAD_DEBUG_CEF_VENDOR");
            static long dbgFrame = 0;
            if (dbgVendor && dbgVendor[0]) {
                ++dbgFrame;
                if (dbgFrame == 120) {
                    st.activeWorkspace = AppState::Workspace::PartsLibrary;
                    ui::parts::startLoginWizard(st, dbgVendor);
                }
                if (dbgFrame == 6000) break;
            }
        }
#endif

        // Borderless-window chrome: drag / edge-resize / double-click-maximize off the same
        // title-bar HitLayout the GUI publishes. No-op on Windows (the WndProc handles it).
        chrome::tick(win, &hitLayout);

        // Unsaved-changes guard on exit. A close request (title-bar X / Alt+F4 / File > Quit all set
        // glfwSetWindowShouldClose) is intercepted here: if any document has unsaved edits and the
        // user hasn't already confirmed, cancel the close and raise the exit confirmation modal. A
        // clean session (or an already-confirmed quit) falls straight through and the loop exits.
        if (!shot && glfwWindowShouldClose(win) && !st.quitConfirmed) {
            bool anyDirty = false;
            for (const auto& t : st.docTabs)
                // Only warn about USER-owned docs: a doc the user created/opened, or an AI mirror
                // they adopted by editing. Pure AI mirror tabs are excluded — the agent owns their
                // save discipline, and any unsaved AI work is preserved by the daemon's recovery
                // tier, so nagging the user about them would be a false positive.
                if (t.dirty && (!t.aiHandoff || t.userAdopted)) { anyDirty = true; break; }
            if (anyDirty) {
                glfwSetWindowShouldClose(win, GLFW_FALSE);
                st.wantQuitConfirm = true;
            }
        }

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
        // T-PERF3: commit mesh loads that finished disk-parse on the loader thread (GL
        // upload only here; stale gens dropped so keep-last-good stays on screen).
        st.drainMeshLoads();
        // Phase 4 (shared engine daemon): drain hostd push events — foreign-edit
        // mirror refresh, mirror tabs from doc.opened, lease toasts. No-op in
        // legacy (in-process) mode.
        core::engineEventsTick(st);
        // Multi-document: release closed-tab sessions whose async work (PM preview / sketch / eval
        // callbacks that captured `this`) has now fully drained — safe to destroy once rpc is idle.
        st.sweepRetiredSessions();

        // Live MCP handoff: if the external AI-CAD MCP server (the engine Claude drives) published
        // a new part, open/refresh it in the viewport so the user watches the AI build. Self-guards
        // and no-ops the vast majority of frames (a single mtime stat).
        // In daemon mode the live preview arrives as doc.changed events with the
        // mesh path (engineEventsTick above) — the .step+ file handoff is legacy
        // only, so skip the poller entirely (no second source of truth).
        if (!engine::daemonMode()) ui::pollAiCadHandoff(st);

        // If a chat-triggered accurate (OpenFOAM) CFD finished on the SimClient channel, post its
        // Cd/Cl onto the running chat bubble. No-op unless a chat OpenFOAM run is pending.
        ui::pollChatOpenFoam(st);

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
            // The user stopped this run — drop its streamed events so the (finalized) chat message
            // and status line don't flicker with live progress while the worker winds down.
            if (st.stopRequested) evs.clear();
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
                            // Phase 2 (M9/M10): feed the live Agent Workflow DAG store. Ungated —
                            // it must capture agent.* span events even when no chat bubble is
                            // active (a subagent branch can outlive the trunk turn). GL-thread
                            // only; the store is a plain static in ai_workspace.cpp.
                            ui::feedAgentDagEvent(orchKind, data);
                            // Fold the event into the running turn's inline activity timeline so
                            // the conversation transparently shows what the AI is doing/thinking.
                            if (active) feedTurnActivity(*active, orchKind, data);
                            // Context meter: reset at run start; track the latest call's input-token
                            // estimate (how full the model's context is right now).
                            if (orchKind == "run_started") st.lastContextTokens = 0;
                            else if (orchKind == "agent_output" && data.contains("tokens") &&
                                     data["tokens"].is_object()) {
                                int in = data["tokens"].value("in", 0);
                                if (in > 0) st.lastContextTokens = in;
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
          if (st.stopRequested) {
            // The user stopped this run. The worker finished in the background, but discard the
            // result: leave the chat message as the Stop handler finalized it, and skip the doc
            // rebuild/resync and any new-tab adoption.
            st.stopRequested = false;
            st.agentV2Running = false;
            st.status = "Stopped.";  st.statusKind = 1;
            st.addLog("[..] run result discarded (stopped by user)");
          } else {
            // --- C22b: if this was a v2 run, resync the DocMirror tree + reload mesh ---
            // The worker already rebuilt the doc; call doc_rebuild to refresh the tree and
            // get the latest aicm_path, then load it into the viewport.
            const bool wasV2 = st.agentV2Running;
            // WS3: a true-assembly run produced a NEW assembly document (its own .step+) rather
            // than mutating the active doc. Open that file as a fresh tab via the GL-thread open
            // pump — featureTreeOpenPath detects the aicad.asm schema and routes the open to the
            // Python worker (the C++ core rejects opening assemblies by design). The active part
            // doc was untouched, so we skip the doc_rebuild-on-current-doc below for this case.
            const bool openedNewDoc = wasV2 && !st.job.createdDocPath.empty() && st.job.error.empty();
            if (openedNewDoc) {
                st.requestOpenPath = st.job.createdDocPath;
                st.addLog("[v2] assembly built -> opening as new tab: " + st.job.createdDocPath);
            }
            // W17: the run built a 2D drawing — adopt it as a Drawing tab (the active part was
            // NOT mutated, so we skip the doc_rebuild below).
            const bool madeDrawing = wasV2 && !st.job.drawingDocId.empty() && st.job.error.empty();
            if (madeDrawing) {
                st.adoptDrawingTab(st.job.drawingDocId, st.job.drawingName, st.job.drawingSourceId);
                st.addLog("[v2] drawing built -> opened drawing tab " + st.job.drawingDocId);
                st.job.drawingDocId.clear();
            }
            // CAM fast-path: the run built a machining job from the current part. Load it into the
            // Manufacture workspace panel and switch to it (the active part doc was NOT mutated, so
            // the doc_rebuild below is skipped — same as drawing).
            const bool madeCam = wasV2 && !st.job.camDocId.empty() && st.job.error.empty();
            if (madeCam) {
                ui::camLoadJob(st, st.job.camDocId);
                st.showRibbon = true;
                st.ribbonTab = AppState::RT_Manufacture;
                st.addLog("[v2] CAM job built -> opened Manufacture workspace " + st.job.camDocId);
                st.job.camDocId.clear();
            }
            if (st.agentV2Running) {
                st.agentV2Running = false;
                if (!openedNewDoc && !madeDrawing && !madeCam && st.docMirror.hasDoc() &&
                    st.job.error.empty()) {
                    // C39: a forced rebuild can take tens of seconds on a heavy doc — the old
                    // sync call froze the window right after the agent finished. Queue async;
                    // the callback (a later frame's drain, GL thread) resyncs and reloads.
                    // doc_activate: the agent's final apply_ops left the engine's read-cache
                    // compile warm, so this serves the tree + mesh path without recompiling.
                    st.livePhase = "Refreshing model...";
                    // Multi-document: capture the doc this rebuild targets. If the user switches to
                    // another tab before the async result drains, the callback must NOT resync /
                    // reload the FOREGROUND doc with this (background) doc's result — the foreground
                    // mirror + mesh would be corrupted. The backgrounded doc picks up the fresh tree
                    // + mesh when its tab is reactivated (switchToTab re-activates + reloads).
                    const std::string rebuildDocId = st.docMirror.docId();
                    ++st.rebuildsInFlight;
                    st.rpc.call("doc_activate", {{"doc_id", rebuildDocId}},
                        [&st, rebuildDocId](const core::RpcResult& r) {
                            --st.rebuildsInFlight;
                            st.livePhase.clear();
                            if (st.docMirror.docId() != rebuildDocId) {
                                st.addLog("[v2] doc_rebuild result for " + rebuildDocId +
                                          " ignored — active doc changed");
                                return;
                            }
                            if (r.ok) {
                                // W1-B (item 6): STALENESS FIX. Reloading the mesh alone left the
                                // FeatureManager showing PRE-run features. Resync (doc_tree — a
                                // fast call) pulls the new tree + revision + owner map and fires
                                // the listener so the tree repaints with what the agent built.
                                st.docMirror.resync(st.worker);
                                // C40: doc_activate is an ops-response — the mesh path lives at
                                // tess.aicm_path (the old top-level read was always empty, so
                                // the viewport NEVER refreshed after a v2 agent run).
                                std::string aicmPath;
                                if (r.result.contains("tess") && r.result["tess"].is_object())
                                    aicmPath = r.result["tess"].value("aicm_path", "");
                                if (aicmPath.empty())
                                    aicmPath = r.result.value("aicm_path", "");
                                if (!aicmPath.empty())
                                    st.loadDocMeshAsync(aicmPath);  // T-PERF3
                                st.addLog("[v2] doc_activate + resync after run: rev=" +
                                          std::to_string(st.docMirror.revision()));
                            } else {
                                st.addLog("[err] doc_activate after v2 run: " + r.err);
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
                    st.addLog("[..] runtime module missing — auto-repairing (use Billet > Repair runtime if this persists)...");
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
            } else if (wasV2 && st.job.isMission && !st.job.missionVerdict.empty() &&
                       st.job.error.empty()) {
                // Mission run: the report/verdict prose IS the assistant's reply (it already
                // narrates what was built + the analysis conclusion). The assembly tab / doc
                // rebuild were handled above; we just surface the verdict as the bubble text.
                st.status = "Done";
                st.statusKind = 0;
                assistant = st.job.missionVerdict;
            } else if (wasV2 && st.job.error.empty()) {
                // C40: a v2 orchestrator run has no job.stl — the doc itself was built. Without
                // this branch the assistant bubble finished EMPTY (the run looked dead even when
                // it succeeded). Summarize from the run result; the tree/viewport refresh lands
                // via the async doc_rebuild above.
                if (st.job.converged) {
                    st.status = "Done";
                    st.statusKind = 0;
                    if (openedNewDoc)
                        assistant = "Done - I built each part as its own editable document and "
                                    "assembled them with positioned components. The assembly is "
                                    "opening in a new tab; each component is a separate part you "
                                    "can edit. Ask for a tweak and I'll revise.";
                    else
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
          }  // end else (run not stopped by the user)
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
                // Headless appearance smoke (AI_CAD_APPR): apply a vivid colour so a screenshot
                // confirms the GL renderer reflects a set_appearance colour (not the default grey).
                if (std::getenv("AI_CAD_APPR")) {
                    float orange[3] = {0.95f, 0.45f, 0.10f};
                    applyModelAppearance(st, orange, 0.6f, 0.35f);
                    std::printf("appr: applied orange\n"); std::fflush(stdout);
                }
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

        // H2: daemon_h2 shot — seed a part, kill hostd, capture the sticky unreachable
        // chip, respawn hostd, re-bootstrap + open a part so tabs repopulate, capture.
        // Wall-clock phases (not frame counts): hostd kill/spawn takes real seconds.
        if (st.wantDaemonH2 && st.workerStarted) {
            const double now = ImGui::GetTime();
            auto killHostd = []() {
#ifndef _WIN32
                // Instance-scoped hard kill. --stop drain leaves client sockets open
                // until the process fully exits (and may refuse while busy), so the
                // GUI never sees HUP. Kill the daemon_pid from hello for a real
                // mid-session reset — still scoped to THIS instance (never pkill -x).
                long pid = 0;
                if (auto* c = engine::client()) {
                    pid = c->serverInfo().value("daemon_pid", 0L);
                }
                if (pid > 0) {
                    std::printf("[daemon_h2] SIGKILL hostd pid=%ld\n", pid);
                    std::fflush(stdout);
                    ::kill((pid_t)pid, SIGKILL);
                } else {
                    // Fallback: instance-keyed --stop --force.
                    std::string bin = aicad_ipc::resolveHostdBin("");
                    if (bin.empty()) bin = "core/build-occt-linux/bin/aicad-hostd";
                    std::string cmd = "\"" + bin + "\" --stop --force 2>/dev/null";
                    int rc = std::system(cmd.c_str());
                    (void)rc;
                }
                // Drop the instance sock so reconnect doesn't race a half-dead peer.
                std::string bin = aicad_ipc::resolveHostdBin("");
                if (bin.empty()) bin = "core/build-occt-linux/bin/aicad-hostd";
                const std::string sock = aicad_ipc::socketPath(aicad_ipc::instanceKey(bin));
                if (!sock.empty()) ::unlink(sock.c_str());
#endif
            };
            auto spawnHostd = []() -> bool {
                std::string err;
                // Prefer the same resolve path EngineClient uses so we hit the right binary.
                std::string bin = aicad_ipc::resolveHostdBin("");
                if (bin.empty()) {
                    // Dev-tree fallback next to the known layout.
                    bin = "core/build-occt-linux/bin/aicad-hostd";
                }
                return aicad_ipc::spawnDetached(bin, "", err);
            };
            if (st.daemonH2Phase == 0) {
                // Seed a live part so the pre-kill session has a real doc tab.
                std::string err;
                if (st.docMirror.newPart(st.worker, err)) {
                    nlohmann::json boxOps = nlohmann::json::array();
                    boxOps.push_back({{"op","add_feature"},{"feature",{
                        {"type","box"},{"name","Box1"},{"params",{
                            {"length",{{"value",40.0},{"expr",nullptr}}},
                            {"width", {{"value",30.0},{"expr",nullptr}}},
                            {"height",{{"value",20.0},{"expr",nullptr}}}
                        }}}}});
                    auto r1 = st.docMirror.applyOps(st.worker, boxOps);
                    if (r1.ok && !r1.aicm_path.empty()) {
                        std::string le;
                        st.loadDocMesh(r1.aicm_path, le);
                    }
                    std::printf("[daemon_h2] seeded doc=%s tabs=%zu\n",
                                st.docMirror.docId().c_str(), st.docTabs.size());
                } else {
                    std::printf("[daemon_h2] seed newPart failed: %s\n", err.c_str());
                }
                std::fflush(stdout);
                killHostd();
                st.daemonH2Phase = 1;
                st.daemonH2PhaseT = now;
                std::printf("[daemon_h2] killed hostd; waiting for unreachable notice\n");
                std::fflush(stdout);
            } else if (st.daemonH2Phase == 1) {
                // Wait until the sticky flag latches, or time out after ~8s and force-capture.
                if (st.daemonUnreachable || (now - st.daemonH2PhaseT) > 8.0) {
                    std::printf("[daemon_h2] unreachable=%d (%.1fs) — capturing notice\n",
                                (int)st.daemonUnreachable, now - st.daemonH2PhaseT);
                    std::fflush(stdout);
                    st.daemonH2Phase = 2;
                    st.daemonH2PhaseT = now;
                }
            } else if (st.daemonH2Phase == 2) {
                // Hold the unreachable chip on screen, then mark a mid-shot capture
                // (taken after ImGui::Render where dw/dh exist) and respawn hostd.
                if ((now - st.daemonH2PhaseT) > 1.2) {
                    // Latch notice capture for the end of this frame (backbuffer is
                    // fully drawn only after ImGui::Render below). -1 = write notice PNG.
                    st.daemonH2CaptureFrame = -1;
                    bool ok = spawnHostd();
                    std::printf("[daemon_h2] respawn hostd ok=%d (notice capture pending)\n",
                                (int)ok);
                    std::fflush(stdout);
                    st.daemonH2Phase = 3;
                    st.daemonH2PhaseT = now;
                }
            } else if (st.daemonH2Phase == 3) {
                // Wait for reconnect + bootstrap, then open a fresh part so the tab
                // strip is visibly populated in the post-reconnect capture.
                if (!st.daemonUnreachable && (now - st.daemonH2PhaseT) > 1.5) {
                    std::string err;
                    if (st.docMirror.newPart(st.worker, err)) {
                        std::printf("[daemon_h2] post-reconnect doc=%s tabs=%zu\n",
                                    st.docMirror.docId().c_str(), st.docTabs.size());
                    } else {
                        std::printf("[daemon_h2] post-reconnect newPart failed: %s\n",
                                    err.c_str());
                    }
                    std::fflush(stdout);
                    st.daemonH2Phase = 4;
                    st.daemonH2PhaseT = now;
                    if (st.daemonH2CaptureFrame <= 0)
                        st.daemonH2CaptureFrame = st.frame + 8;
                } else if ((now - st.daemonH2PhaseT) > 20.0) {
                    std::printf("[daemon_h2] reconnect timeout unreachable=%d\n",
                                (int)st.daemonUnreachable);
                    std::fflush(stdout);
                    st.daemonH2Phase = 4;
                    st.daemonH2PhaseT = now;
                    if (st.daemonH2CaptureFrame <= 0)
                        st.daemonH2CaptureFrame = st.frame + 8;
                }
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
                    const std::string dir = appdataAppDir() + "/ecad_out";
                    std::filesystem::create_directories(dir, ec);
                    const std::string path = dir + "/roundtrip.ecad+";
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

        // Liquid glass v2.1: per-frame Settings -> resolved-glass push. Idempotent (exact-match
        // early-out in setGlassConfig => free), so any code path that mutates st.settings.glassCfg
        // (the Appearance editor, MCP, tests) is live-applied without extra plumbing.
        ui::setGlassConfig(st.settings.glassCfg);

        // Viewport highlight colors track the live palette (theme switches included) — setColors
        // just stores three float[4]s, so the per-frame push is free.
        if (st.overlays) {
            float featHl[4];
            ui::Palette::withAlpha(ui::palette.selectionHighlight, featHl, 0.30f);
            st.overlays->setColors(ui::palette.hoverHighlight, ui::palette.selectionHighlight,
                                   featHl);
        }

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
            const bool inSketchNow = st.sketchMode->active();
            bool ecadFocused = false;
            if (ImGuiContext* g = ImGui::GetCurrentContext()) {
                for (ImGuiWindow* w = g->NavWindow; w; w = w->ParentWindow)
                    if (std::strcmp(w->Name, "ECAD") == 0) { ecadFocused = true; break; }
            }
            if (canDoc && !textFocus && !st.showWeb && !inSketchNow && !ecadFocused &&
                st.docMirror.hasDoc() && (io.KeyCtrl || io.KeySuper)) {
                if (io.KeyShift && ImGui::IsKeyPressed(ImGuiKey_Z, false)) {
                    st.docRedo();
                } else if (ImGui::IsKeyPressed(ImGuiKey_Z, false)) {
                    st.docUndo();
                } else if (ImGui::IsKeyPressed(ImGuiKey_Y, false)) {
                    st.docRedo();
                }
            }
            // While the in-app browser overlay owns the screen it is the "text field": its
            // page/forms swallow typing, but a CEF OSR view does not set io.WantTextInput, so
            // guard the app's Ctrl-shortcuts on !st.showWeb too — otherwise Ctrl+N/P/O/S get
            // hijacked out from under the browser (where they mean new-tab/print/open/save).
            const bool appShortcutsOk = !textFocus && !st.showWeb;

            // Ctrl+N creates a new part directly. The old "+" chooser lived in the removed document
            // strip; full document-type creation is still available from File > New.
            if (appShortcutsOk && (io.KeyCtrl || io.KeySuper) && !io.KeyShift &&
                ImGui::IsKeyPressed(ImGuiKey_N, false))
                ui::createNewDoc(st, 0);

            // Ctrl+P: open the unified command + settings search.
            if (appShortcutsOk && (io.KeyCtrl || io.KeySuper) && !io.KeyShift &&
                ImGui::IsKeyPressed(ImGuiKey_P, false))
                st.showCommandSearch = true;

            // [workspace-tabs seam] Ctrl+1..Ctrl+8 switch the title-bar workspace (matches tab
            // order). The shortcut bar advertises these. Guards: text input owns digits while
            // typing. Deliberately NOT gated on ecadFocused: the ECAD window is focused for the
            // whole Electronics stay, and its accelerator space has no Ctrl+digit bindings —
            // gating here made the workspace keys dead exactly where users need an exit.
            if (!textFocus && (io.KeyCtrl || io.KeySuper) && !io.KeyShift && !io.KeyAlt) {
                using WS = AppState::Workspace;
                static constexpr struct { ImGuiKey key; WS ws; } kWsKeys[] = {
                    {ImGuiKey_1, WS::Model},        {ImGuiKey_2, WS::Drawing},
                    {ImGuiKey_3, WS::Simulate},     {ImGuiKey_4, WS::Electronics},
                    {ImGuiKey_5, WS::Manufacture},  {ImGuiKey_6, WS::PLM},
                    {ImGuiKey_7, WS::PartsLibrary}, {ImGuiKey_8, WS::AI},
                    {ImGuiKey_9, WS::ReverseEng},
                };
                // Ctrl+digit stays live while the Web overlay is up so it doubles as an exit
                // (switch workspace AND drop the browser), mirroring a workspace-tab click.
                for (const auto& k : kWsKeys)
                    if (ImGui::IsKeyPressed(k.key, false)) {
                        st.activeWorkspace = k.ws; st.showWeb = false; break;
                    }
            }

            // Esc closes the Web overlay (back to modeling), unless a text field owns the key.
            if (st.showWeb && !textFocus && ImGui::IsKeyPressed(ImGuiKey_Escape, false))
                st.showWeb = false;

            // ---- Wave 3: Ctrl+O / Ctrl+S / Ctrl+Shift+S file shortcuts ----
            // Set request flags consumed by the GL-thread file pump below (after the input block) so
            // the native dialog opens with the doc swap off the ImGui input path. Skip while a text
            // field or the ECAD window owns input (the ECAD editor has its own save). Ctrl+S/Shift+S
            // require a live doc; Ctrl+O is always allowed.
            if (appShortcutsOk && !ecadFocused && (io.KeyCtrl || io.KeySuper)) {
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
            if (appShortcutsOk && !ecadFocused && (io.KeyCtrl || io.KeySuper) && !io.KeyShift) {
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
        if (st.requestOpenDialog || !st.requestOpenPath.empty() ||
            st.requestSave || st.requestSaveAs ||
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
            // Save / Save As route through the unified per-tab saver so the active tab saves by its
            // kind (CAD .step+, ECAD .ecad+, or a Drawing via its source part). saveDocTab records
            // Recent + clears the unsaved marker itself. Saving a Drawing switches the mirror to its
            // source part, so restore the active tab afterward (a no-op for CAD/ECAD saves).
            if (st.requestSave || st.requestSaveAs) {
                const bool asSave = st.requestSaveAs;
                st.requestSave = st.requestSaveAs = false;
                const int origActive = st.activeTab;
                ui::saveDocTab(st, origActive, /*forceDialog=*/asSave);
                if (origActive >= 0 && origActive < (int)st.docTabs.size() &&
                    st.activeTab != origActive)
                    st.switchToTab(origActive);
            }
            // Wave-5-fill: Save All — save every part/assembly + ECAD tab via the unified saver.
            // Titled docs re-save silently; the never-saved ACTIVE doc falls back to a Save-As prompt
            // (a batch must not pop one dialog per untitled doc, so untitled BACKGROUND tabs are left
            // for an explicit per-doc Save). Drawing tabs are skipped — they persist when their source
            // part is saved (which this loop does, clearing their markers). The original active tab is
            // restored afterward (CAD saves re-point the mirror as a side effect).
            if (st.requestSaveAll) {
                st.requestSaveAll = false;
                const int origActive = st.activeTab;
                int savedCount = 0, failCount = 0;
                for (int i = 0; i < (int)st.docTabs.size(); ++i) {
                    const AppState::DocTab& tab = st.docTabs[i];
                    if (tab.kind == AppState::DocTab::TabKind::Drawing) continue;  // via source part
                    if (tab.path.empty() && i != origActive) continue;            // untitled background
                    ui::TabSaveResult r = ui::saveDocTab(st, i, /*forceDialog=*/false);
                    if (r == ui::TabSaveResult::Saved) ++savedCount;
                    else if (r == ui::TabSaveResult::Failed) ++failCount;
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

        // Window title mirrors the active document + dirty marker (SolidWorks-style). Cheap
        // change-detection so glfwSetWindowTitle only fires when the string actually changes.
        {
            std::string title;
            if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()) {
                const auto& tab = st.docTabs[st.activeTab];
                title = tab.title.empty() ? "Untitled" : tab.title;
                if (tab.dirty) title += "*";
                title += " \xe2\x80\x94 Billet";                 // " — Billet"
            } else {
                title = "Billet";
            }
            static std::string lastTitle;
            if (title != lastTitle) { lastTitle = title; glfwSetWindowTitle(win, title.c_str()); }
        }

        // ---------- C14: ribbon bar (drawn between title bar and dockspace) ----------
        // syncIconTheme keeps texture selection in sync with the active theme.
        ui::syncIconTheme();
        // C34: SolidWorks behavior — entering a sketch flips the ribbon to the Sketch tab,
        // exiting returns to Features. Edge-triggered so the user can still switch tabs freely
        // while sketching.
        {
            static bool s_wasInSketch = false;
            static bool s_wasInSketch3d = false;
            const bool nowInSketch = st.sketchMode->active();
            const bool nowInSketch3d = st.sketch3dMode && st.sketch3dMode->active();
            if ((nowInSketch && !s_wasInSketch) || (nowInSketch3d && !s_wasInSketch3d))
                st.ribbonTab = AppState::RT_Sketch;
            else if (!nowInSketch && s_wasInSketch && !nowInSketch3d)
                st.ribbonTab = AppState::RT_Features;
            else if (!nowInSketch3d && s_wasInSketch3d && !nowInSketch)
                st.ribbonTab = AppState::RT_Features;
            s_wasInSketch = nowInSketch;
            s_wasInSketch3d = nowInSketch3d;
        }
        // C40: accent-highlight the armed sketch tool's ribbon button. When the profiler is
        // running (and no sketch tool is armed) highlight the Profiler button instead, so its
        // ON state is visible at a glance.
        const char* activeCmd = st.sketchMode->activeToolCmd();
        // Armed-but-awaiting-pick: "New Sketch" was clicked and we're waiting on a plane/face pick
        // (no sketch tool active yet, so activeToolCmd() above returns ""). Highlight the New Sketch
        // button itself so the armed state is visible, matching every other armed-tool convention.
        if ((!activeCmd || !*activeCmd) && st.sketchPlanePickActive) activeCmd = "sketch_new";
        if (st.profilerRunning && (!activeCmd || !*activeCmd)) activeCmd = "prof_toggle";
        ui::setRibbonActiveCmd(activeCmd);
        // Sketch tab swaps Exit Sketch <-> New Sketch depending on whether a sketch is being edited.
        ui::setRibbonSketchActive(st.sketchMode->active());
        // 3D sketch: swaps the Sketch tab's command source to the 3D tool set (see ribbon.cpp).
        ui::setRibbonSketch3DActive(st.sketch3dMode && st.sketch3dMode->active());
        // [workspace-tabs seam] Drive the per-workspace ribbon filter from the active title-bar
        // workspace so Model shows modeling tools, Simulate shows sim tools, etc. (WIRE_REQUESTS).
        ui::setRibbonWorkspace(static_cast<int>(st.activeWorkspace));
        float ribbonH = ui::drawRibbon(st.ribbonTab, st.showRibbon,
                                       st.showSimulationTools, st.showPcbTools, barH,
                                       st.activeTabIsDrawing());

        // Electronics: surface the PCB DRC/unrouted summary in the ribbon band's trailing dead space
        // (the board canvas gave up its bottom results strip to fill the full height — see
        // ecad_panel.cpp). No-op for schematic docs / when no board is open.
        if (st.activeWorkspace == AppState::Workspace::Electronics && ribbonH > 0.0f)
            ui::drawEcadRibbonStatus(st, vp->Pos.y + barH, ribbonH);

        // ---------- default starter document ----------
        // Open straight into a fresh part on launch (non-shot) so the app is immediately usable.
        ensureStarterDoc(st, shot);

        // ---------- active document reconciliation ----------
        // The browser/SolidWorks-style document tabs are rendered by the central Viewport/ECAD
        // window that owns the view area, so the strip stays contained in that view window.
        st.ensureActiveTab();

        // ---------- dockspace host (between the title bar+ribbon and the status bar) ----------
        const float statusH = ImGui::GetTextLineHeight() + 8.0f * scale;
        const float dockTopY = barH + ribbonH;
        // Publish the overlay band (the full-width viewport region the pinned glass columns
        // anchor to) BEFORE any panel draws — drawLeftPane (layout.cpp) and the Copilot column
        // (legacy_chat.cpp) read it via ui::overlayBand().
        ui::setOverlayBand(vp->Pos.x, vp->Pos.y + dockTopY,
                           vp->Pos.x + vp->Size.x, vp->Pos.y + vp->Size.y - statusH);
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
        ImGui::Begin("##DockHost", nullptr, hostFlags | ImGuiWindowFlags_NoBackground);
        ImGui::PopStyleVar(3);
        // Versioned dockspace ID pairs with the versioned ini. Changing this intentionally forces
        // one clean DockBuilder layout after major window/dock identity changes.
        ImGuiID dockId = ImGui::GetID("AICADDockV6");  // V6: overlay layout — splitless central node
        if ((!st.layoutInit && ImGui::DockBuilderGetNode(dockId) == nullptr) || st.resetLayout) {
            buildDefaultLayout(dockId, ImVec2(vp->Size.x, vp->Size.y - dockTopY - statusH));
            st.layoutInit = true;
            st.resetLayout = false;
            // "Reset layout" also restores the pinned glass columns' default width/collapse state
            // (one-shot flags consumed by drawLeftPane / the Copilot column on their next draw).
            ui::resetOverlayColumns();
            // Don't yank focus to the Viewport when the app opens straight into a non-3D workspace
            // (ECAD board editor or the CAM Manufacture tab) — let that workspace's docked tab show.
            if (!st.wantEcad && st.ribbonTab != AppState::RT_Manufacture) st.wantFocusViewport = 6;
        }
        // W2 full-bleed: with PassthruCentralNode AND a window docked in the central node, ImGui
        // paints an OPAQUE ImGuiCol_WindowBg rect over the WHOLE dockspace (imgui.cpp DockNodeUpdate:
        // the transparent "central node hole" is punched only when the central node is EMPTY, but the
        // Viewport window is docked there). That backdrop sits above the global background draw list
        // and hides the full-bleed 3D image composited there. Push a transparent WindowBg just for
        // the DockSpace() call so the dockspace backdrop is see-through and the 3D shows; every docked
        // window still paints its own background (the glass FeatureManager stays translucent) on top.
        ImGui::PushStyleColor(ImGuiCol_WindowBg, ImVec4(0, 0, 0, 0));
        ImGui::PushStyleColor(ImGuiCol_DockingEmptyBg, ImVec4(0, 0, 0, 0));
        // Overlay layout: the default layout has NO dock splits, so ImGui draws no splitter bars.
        // Belt-and-braces for user-created splits (a window dragged into a dockspace edge): make
        // any splitter invisible around ONLY this DockSpace call (DockingSeparatorSize keeps its
        // default so the invisible hit zone still resizes). ui_theme's Separator roles stay live
        // for normal ImGui::Separator() everywhere else.
        ImGui::PushStyleColor(ImGuiCol_Separator, ImVec4(0, 0, 0, 0));
        ImGui::PushStyleColor(ImGuiCol_SeparatorHovered, ImVec4(0, 0, 0, 0));
        ImGui::PushStyleColor(ImGuiCol_SeparatorActive, ImVec4(0, 0, 0, 0));
        ImGui::DockSpace(dockId, ImVec2(0, 0), ImGuiDockNodeFlags_PassthruCentralNode);
        ImGui::PopStyleColor(5);
        ImGui::End();

        // ---------- status bar ----------
        ui::drawStatusBar(st, vp, statusH, busy, hostFlags);

        bool canRun = st.workerStarted && !busy;

        // ---------- panels (pinned glass columns + floating dialogs over the full-width 3D) ----
        // The SolidWorks-faithful tabbed LEFT column (Explorer / FeatureManager / PropertyManager /
        // Configurations). drawLeftPane routes the active tab body and auto-
        // switches to the PropertyManager tab while a command form is active. We still tick the PM
        // so the debounced live preview fires; the container draws the PM form body when active.
        // A live PropertyManager forces the left pane open so its form is visible, but the preview
        // tick is independent of that pane state to avoid invisible active commands.
        // Multi-document: the ACTIVE document's PropertyManager (st.propertyManager is re-seated on
        // every tab switch). A backgrounded doc's PM is NOT ticked here, so its debounced preview
        // pauses while parked and resumes when its tab is reactivated.

        // [workspace-tabs seam] Derive per-frame display flags from st.activeWorkspace. These are
        // LOCAL to this frame — they never write persistent state (no st.leftPaneTab mutation,
        // no clamping that traps the user). Full-body workspaces (PLM, PartsLibrary, AI) suppress
        // the left pane and viewport in favour of their own full-screen layout; the docked Copilot
        // column is always drawn so it stays available in non-AI workspaces exactly as today.
        using WS = AppState::Workspace;
        const WS ws = st.activeWorkspace;
        // The "Web" chip's full-window in-app browser overlay (title_bar.cpp toggles st.showWeb).
        // It is NOT a workspace — activeWorkspace stays put underneath — so it composes with any
        // workspace and toggles straight back to modeling. While up it owns the whole central band.
        const bool webActive = st.showWeb;
        // Workspaces (or the Web overlay) that own the ENTIRE central region — no left pane, no
        // viewport, no cam/ecad. Folding webActive in suppresses all of those in one place; the
        // full-body workspace renderers below are gated on !webActive separately.
        const bool wsFullBody = webActive || (ws == WS::PLM || ws == WS::PartsLibrary || ws == WS::AI
                                 || ws == WS::ReverseEng || ws == WS::Firmware);
        // Effective left-pane tab override (derived this frame; never written to st).
        // Simulate forces the Simulation tab so the sim tree shows.
        // Non-Simulate workspaces must NOT show the sim tree: clamp LP_Simulation back to
        // FeatureManager so the sim tab never leaks into Model/Drawing/etc.
        const int effectiveLeftTab = (ws == WS::Simulate)
            ? ui::kLeftPaneSimulation
            : (st.leftPaneTab == ui::kLeftPaneSimulation ? ui::kLeftPaneFeatureManager
                                                         : st.leftPaneTab);
        // (cam-workspace overhaul) Manufacture no longer force-shows the floating CAM manager —
        // the workspace's primary UI is the left CAM tree + the Manufacture ribbon; the panel is
        // the double-click detail editor (see drawCamManager call below).
        // Electronics workspace shows the ECAD editor as the primary body.
        // In other workspaces, only show ECAD when an ECAD doc is actually open (ecadDocId set)
        // or showPcbTools was explicitly triggered by a ribbon command. Do NOT let showPcbTools
        // bleed from the Electronics workspace into Manufacture/Model/etc — clear it on exit.
        if (ws != WS::Electronics && st.showPcbTools && st.ecadDocId.empty()) st.showPcbTools = false;
        // The ECAD editor canvas + PCB Manager belong to the Electronics workspace ONLY — an open
        // board no longer bleeds its 2D canvas over the 3D viewport in Model/etc (Hunter round). The
        // doc-tab strip drives the coupling the other way: activating a PCB/Schematic tab switches the
        // workspace to Electronics (AppState::switchToTab), so a board is always reachable.
        const bool wsShowEcad = (ws == WS::Electronics);

        // ---- Electronics workspace-entry auto-init (hoisted ABOVE drawLeftPane) ----
        // Must run before drawLeftPane (below) so its ECAD "PCB Manager" body (layout.cpp's
        // Electronics branch -> ecad_panel.cpp::drawEcadManagerBody) sees the freshly-created board
        // on the SAME frame the workspace is entered, instead of one frame of "No PCB/schematic
        // document open" while the mirror is still empty. wantFocusEcad's actual SetNextWindowFocus
        // stays below, right before drawEcadEditor draws — window-focus calls that fire before their
        // window's Begin() are a no-op, so only the doc-creation edge needs to move.
        //
        // ecadPrimeActiveApp is needed because ecadAutoInitBoard's ecad_new -> ecadAdopt can be the
        // FIRST-EVER ECAD call this session (a brand new Electronics entry, nothing loaded yet) —
        // before either drawEcadEditor or drawEcadManagerBody has run once to set the state()
        // resolver's active-AppState pointer. Without priming it first, ecadAdopt's mirror load lands
        // in the process-wide fallback state instead of the just-created PCB1 tab's own state, so the
        // manager body / canvas the user actually sees still reads "no document" this frame.
        ui::ecadPrimeActiveApp(st);
        static WS ecadPrevWsForInit = WS::Model;
        if (ws != ecadPrevWsForInit) {
            // Leaving a workspace: discard its untouched auto-created starter doc (blank PCB1, an
            // untouched starter Part, etc.) so a blank initial document doesn't linger. Runs BEFORE
            // the Electronics auto-init below so entering Electronics from a blank Model drops that
            // starter part first, then creates PCB1. Never closes the last tab.
            st.discardWorkspaceStarter(ecadPrevWsForInit);
            // discardWorkspaceStarter -> closeDocTab -> neighbor switchToTab can flip the workspace
            // (a Part neighbor pulls to Model); re-assert the workspace the user actually picked.
            st.activeWorkspace = ws;
            if (ws == WS::Electronics) {
                st.wantFocusEcad = 2;      // pop the ECAD tab forward for a couple of frames
                // Auto-init a default PCB1 board on first entry with nothing open, so the user
                // lands in a usable layout (item 1). Edge-triggered here (once), never in the
                // draw path. No-op when a doc is already open; graceful fallback on worker error.
                ecadAutoInitBoard(st);
            } else if (ecadPrevWsForInit == WS::Electronics) {
                st.wantFocusEcad = 0;      // cancel any pending grab from the stay
                st.wantFocusViewport = 2;  // bring the 3D view back to the central node
            }
            ecadPrevWsForInit = ws;
        }

        if (!wsFullBody) {
            // ---- normal viewport workspaces (Model, Drawing, Simulate, Electronics, Manufacture) ----
            // Electronics used to suppress this column (the ECAD panel drew its own embedded left
            // sub-panel instead) — that's what left the doc-tab strip anchored at the band's far-left
            // edge instead of matching the Model tab (drawLeftPane never ran, so it never published
            // this frame's overlayColumnEdge(0)). drawLeftPane now hosts the ECAD "PCB/Schematic
            // Manager" body itself (layout.cpp's Electronics branch, mirroring the Simulate/
            // Manufacture single-tree pattern) so the SAME pinned column serves every workspace.
            const bool showLeftPane = true;
            if (st.propertyManager->active()) {
                st.propertyManager->tick(st);
                if (showLeftPane) st.showFeatureTree = true;
            }
            if (st.showFeatureTree && showLeftPane) {
                // Pass the effective tab as a temporary override via the AppState field; restore after.
                // drawLeftPane reads st.leftPaneTab directly, so we temporarily set it per-frame.
                const int savedTab = st.leftPaneTab;
                if (effectiveLeftTab != savedTab) st.leftPaneTab = effectiveLeftTab;
                ui::drawLeftPane(st, [&st](const std::string& fid) {
                    st.propertyManager->openForEdit(st, fid);
                });
                if (effectiveLeftTab != savedTab) st.leftPaneTab = savedTab;
            }
        }

        // W3-H: unified AI Copilot pane (input-at-top + conversation; embeds the run tree
        // inline while a v2 run is active). Drawn as the docked Copilot column on the right
        // edge in every workspace EXCEPT the AI workspace, where the full-body AI hub
        // (drawAiWorkspace, below) hosts its own expanded chat on the same st.chat state —
        // drawing both would double the conversation panel.
        if (ws != WS::AI && !webActive) ui::drawAIPanel(st, busy, canRun);
        // W3-H: standalone "Agent Activity" window (View > Activity / the pane's Activity
        // button / the activity shot). No-op unless st.showActivityPanel; was previously
        // unreachable because only the in-pane tab hosted drawBody.
        activityPanel.draw(st);

        if (!wsFullBody) {
            // ---- 3D/2D viewport (Model, Drawing, Simulate, Manufacture) ----
            // Electronics uses the ECAD editor as the full central body — skip the 3D viewport
            // so the ECAD dock window wins the central node without fighting the Viewport tab.
            // Manufacture (cam-workspace overhaul): the 3D viewport IS the central body — the
            // CAM job tree lives in the left column (drawLeftPane -> drawCamTreeBody), the CAM
            // actions live on the Manufacture ribbon, and generated toolpaths draw over the part
            // via the camActiveBackplot viewport overlay ("Show in 3D").
            const bool skipViewport = (ws == WS::Electronics);
            if (!skipViewport) {
                if (st.wantFocusViewport > 0) { ImGui::SetNextWindowFocus(); st.wantFocusViewport--; }
                ui::drawViewportPanel(st, io, busy);
            }
        }

        if (!wsFullBody) {
            // ---- ECAD editor (Electronics workspace or when an ECAD doc is open) ----
            // The workspace-entry edge trigger (auto-init PCB1 + arming wantFocusEcad/
            // wantFocusViewport) now runs ABOVE drawLeftPane — see the hoisted block above. This must
            // stay edge-triggered: SetNextWindowFocus -> FocusWindow steals ImGui's ActiveId from
            // widgets in every other window, so a continuous per-frame grab would cancel any click
            // outside the ECAD window before its release could land — title-bar workspace tabs
            // included. That was the "stuck on the Electronics tab" input lockup.
            if (st.wantFocusEcad > 0) {
                // Surfaced by an explicit ecad command from another workspace: the editor now lives
                // ONLY in the Electronics workspace (wsShowEcad above), so switch there to reveal it
                // instead of the old showPcbTools overlay (which no longer un-hides the canvas).
                if (ws != WS::Electronics) st.activeWorkspace = WS::Electronics;
                ImGui::SetNextWindowFocus();
                st.wantFocusEcad--;
            }
            if (wsShowEcad) ui::drawEcadEditor(st);   // Wave N: AI-ECAD 2D editor
        }

        // ---- CAM detail panel ----
        if (!wsFullBody) {
            // The floating CAM panel is no longer the Manufacture workspace's primary UI (the
            // left CAM tree + Manufacture ribbon are) — it is now the double-click DETAIL editor
            // opened from the tree, so no per-frame camRequestFocus grab. Draw every frame
            // (no-ops until opened).
            ui::drawCamManager(st);
        }

        // ---- full-body workspace renderers (PLM / PartsLibrary / AI) ----
        // These take the whole central region. They use drawViewportPanel's dock slot (the
        // ImGui window ID "Viewport" is NOT opened by them — they draw directly into the
        // overlay band area as floating non-docked glass panels, same pattern as drawLeftPane).
        if (!webActive && (ws == WS::PLM || ws == WS::PartsLibrary)) {
            // PLM / Parts Library: full-body workspaces. The body functions emit widgets directly
            // (no Begin of their own), so host them in ONE borderless window spanning the whole
            // central band — otherwise ImGui dumps them into its fallback "Debug" window (a small
            // box top-left, which is what Hunter saw). The band already stops left of the docked
            // Copilot column, so the body fills the central region and Copilot stays visible.
            const ui::OverlayBand& band = ui::overlayBand();
            if (band.valid) {
                const float m = 12.0f;
                // Stop at the docked Copilot column's LEFT edge (published each frame, slot 1) so
                // the 2D workspace body never renders under the Copilot. Falls back to the band's
                // right edge when the Copilot isn't up.
                float rightX = ui::overlayColumnEdge(1);
                if (rightX <= band.minX + m) rightX = band.maxX;
                // ImGuiCond_Always: override any stale collapsed/size state from imgui.ini
                // so the workspace always fills the full band on first open.
                ImGui::SetNextWindowPos(ImVec2(band.minX + m, band.minY + m),
                                        ImGuiCond_Always);
                ImGui::SetNextWindowSize(ImVec2((rightX - band.minX) - 2.0f * m,
                                                (band.maxY - band.minY) - 2.0f * m),
                                         ImGuiCond_Always);
                ImGui::Begin(ws == WS::PLM ? "##PLMWorkspace" : "##PartsWorkspace", nullptr,
                             ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                             ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse |
                             ImGuiWindowFlags_NoBringToFrontOnFocus | ImGuiWindowFlags_NoNav |
                             ImGuiWindowFlags_NoSavedSettings);
                if (ws == WS::PLM) ui::drawPlmWorkspace(st);
                else               ui::drawPartsLibraryWorkspace(st);
                ImGui::End();
            }
        } else if (!webActive && ws == WS::AI) {
            // AI workspace: full-body AI hub spanning the whole central band —
            // [sessions + sub-agents rail] | [expanded Copilot chat / project DAG] | [AI settings].
            // Unlike PLM/Parts this fills to band.maxX (drawAIPanel's docked column is suppressed
            // for WS::AI above, so there is no right column to reserve space for). Same borderless
            // full-region window pattern as drawPlmWorkspace / drawPartsLibraryWorkspace.
            const ui::OverlayBand& band = ui::overlayBand();
            if (band.valid) {
                const float m = 12.0f;
                ImGui::SetNextWindowPos(ImVec2(band.minX + m, band.minY + m), ImGuiCond_Always);
                ImGui::SetNextWindowSize(ImVec2((band.maxX - band.minX) - 2.0f * m,
                                                (band.maxY - band.minY) - 2.0f * m),
                                         ImGuiCond_Always);
                ImGui::Begin("##AIWorkspace", nullptr,
                             ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                             ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse |
                             ImGuiWindowFlags_NoBringToFrontOnFocus | ImGuiWindowFlags_NoNav |
                             ImGuiWindowFlags_NoSavedSettings);
                ui::drawAiWorkspace(st);
                ImGui::End();
            }
        } else if (!webActive && ws == WS::ReverseEng) {
            // RE-ANCHOR(reveng): full-body Reverse Engineering workspace (RE-03/RE-19/RE-21).
            const ui::OverlayBand& band = ui::overlayBand();
            if (band.valid) {
                const float m = 12.0f;
                float rightX = ui::overlayColumnEdge(1);
                if (rightX <= band.minX + m) rightX = band.maxX;
                ImGui::SetNextWindowPos(ImVec2(band.minX + m, band.minY + m), ImGuiCond_Always);
                ImGui::SetNextWindowSize(ImVec2((rightX - band.minX) - 2.0f * m,
                                                (band.maxY - band.minY) - 2.0f * m),
                                         ImGuiCond_Always);
                ImGui::Begin("##RevEngWorkspace", nullptr,
                             ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                             ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse |
                             ImGuiWindowFlags_NoBringToFrontOnFocus | ImGuiWindowFlags_NoNav |
                             ImGuiWindowFlags_NoSavedSettings);
                ui::drawRevEngWorkspace(st);
                ImGui::End();
            }
        } else if (!webActive && ws == WS::Firmware) {
            // T-FIN5: full-body Firmware workspace (project rail | build/emulation/trace |
            // fw_status-gated backend rail). Same borderless central-band host as PLM/RevEng.
            const ui::OverlayBand& band = ui::overlayBand();
            if (band.valid) {
                const float m = 12.0f;
                float rightX = ui::overlayColumnEdge(1);
                if (rightX <= band.minX + m) rightX = band.maxX;
                ImGui::SetNextWindowPos(ImVec2(band.minX + m, band.minY + m), ImGuiCond_Always);
                ImGui::SetNextWindowSize(ImVec2((rightX - band.minX) - 2.0f * m,
                                                (band.maxY - band.minY) - 2.0f * m),
                                         ImGuiCond_Always);
                ImGui::Begin("##FirmwareWorkspace", nullptr,
                             ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                             ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse |
                             ImGuiWindowFlags_NoBringToFrontOnFocus | ImGuiWindowFlags_NoNav |
                             ImGuiWindowFlags_NoSavedSettings);
                ui::drawFirmwareWorkspace(st);
                ImGui::End();
            }
        }

        // ---- "Web" chip overlay: full-window in-app browser over the central band ----
        // Drawn LAST in the central-body section and only when active, so it's the single
        // central renderer for the frame (viewport / left pane / full-body workspaces above
        // are all suppressed via wsFullBody|webActive) — one body owner = no focus/z fights.
        if (webActive) ui::drawWebBrowserPanel(st);

        if (st.ecadAiGo) { st.ecadAiGo = false; ecadAiDesign(st, st.ecadAiRequest); }  // Wave P
        if (st.ecadAiBoardGo) { st.ecadAiBoardGo = false; ecadAiBoard(st, st.ecadAiRequest); }  // Wave U
        if (st.ecadOpsGo) { st.ecadOpsGo = false; ecadApplyOps(st, st.ecadPendingOps); }  // Wave Q
        if (st.ecadSimGo) { st.ecadSimGo = false; ecadSimulate(st); }  // native simulator
        if (st.ecadUndoGo) { st.ecadUndoGo = false; ecadUndo(st); }  // ECAD history undo
        if (st.ecadRedoGo) { st.ecadRedoGo = false; ecadRedo(st); }  // ECAD history redo
        // Panel-owned toolbar action (item 3 check buttons): route through the shared ribbon handler
        // so Annotate / Update Board behave identically whether fired from the ribbon or the panel.
        if (!st.ecadPanelCmd.empty()) { const std::string c = st.ecadPanelCmd; st.ecadPanelCmd.clear(); ecadDispatch(st, c); }

        // ---------- C19: simulation (active doc) + evaluate dialogs ----------
        // The active document's SimManager drains its own SimClient job (owner-gated) and loads result
        // overlays. Its tree + setup forms render in the left pane's Simulation tab (drawLeftPane).
        if (st.simManager) st.simManager->tick(st);
        st.evaluateDialogs->draw(st);   // active doc's Measure/Mass/Section windows (per-doc; re-seated)

        // ---------- C20b: assembly panels (Insert / Mate / Explode / Interference / BOM) ----------
        asmPanels.draw(st);

        // ---------- "Create New" document chooser (tiled previews + Open existing) ----------
        // Hosted here at app scope (not inside the Viewport window) so the doc-strip "+" / Ctrl+N
        // raise it regardless of which central document — part, drawing, or ECAD — is forward.
        ui::drawNewDocChooser(st);

        // ---------- unsaved-changes confirmation on app exit ----------
        // Raised from the main-loop close guard (st.wantQuitConfirm) when the user tries to quit with
        // dirty documents. Save All & Quit saves every dirty doc then exits (cancelled Save-As keeps
        // the app open); Quit Without Saving discards; Cancel returns. quitConfirmed latches the
        // approved choices so the re-issued close request falls through the guard to the real exit.
        {
            static bool s_quitModalOpen = false;
            if (st.wantQuitConfirm) { st.wantQuitConfirm = false; s_quitModalOpen = true; }
            if (s_quitModalOpen) ImGui::OpenPopup("Quit Billet?##appquit");
            ImGui::SetNextWindowPos(ImVec2(vp->Pos.x + vp->Size.x * 0.5f,
                                           vp->Pos.y + vp->Size.y * 0.5f),
                                    ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
            bool open = true;
            if (ImGui::BeginPopupModal("Quit Billet?##appquit", &open,
                                       ImGuiWindowFlags_AlwaysAutoResize |
                                           ImGuiWindowFlags_NoSavedSettings)) {
                // Only USER-owned docs (own tabs + adopted AI mirrors) are the user's to save —
                // pure AI mirror tabs are the agent's responsibility and are covered by the daemon
                // recovery tier, so they're excluded from this prompt (no false positives).
                auto userOwnedDirty = [](const AppState::DocTab& t) {
                    return t.dirty && (!t.aiHandoff || t.userAdopted);
                };
                int n = 0;
                for (const auto& t : st.docTabs) if (userOwnedDirty(t)) ++n;
                ImGui::Text("%d document%s with unsaved changes:", n, n == 1 ? "" : "s");
                ImGui::Spacing();
                for (const auto& t : st.docTabs)
                    if (userOwnedDirty(t)) ImGui::BulletText("%s", t.displayTitle().c_str());
                ImGui::Spacing();
                ImGui::TextUnformatted("Save before quitting?");
                ImGui::Spacing();
                const float bw = 150.0f * scale;
                if (ImGui::Button("Save All & Quit", ImVec2(bw, 0))) {
                    if (saveAllDirtyForQuit(st)) {
                        st.quitConfirmed = true;
                        glfwSetWindowShouldClose(win, GLFW_TRUE);
                        s_quitModalOpen = false;
                        ImGui::CloseCurrentPopup();
                    }
                    // else: a Save-As was cancelled — keep the modal up so nothing is lost.
                }
                ImGui::SameLine();
                if (ImGui::Button("Quit Without Saving", ImVec2(bw + 30.0f * scale, 0))) {
                    st.quitConfirmed = true;
                    glfwSetWindowShouldClose(win, GLFW_TRUE);
                    s_quitModalOpen = false;
                    ImGui::CloseCurrentPopup();
                }
                ImGui::SameLine();
                if (ImGui::Button("Cancel", ImVec2(90.0f * scale, 0))) {
                    s_quitModalOpen = false;
                    ImGui::CloseCurrentPopup();
                }
                ImGui::EndPopup();
            }
            if (!open) s_quitModalOpen = false;   // dismissed via the title-bar X = Cancel
        }

        // ---------- orphan recovery prompt (daemon mode) ----------
        // Raised from the daemon bootstrap (st.recoveryPromptCount) when the daemon reports retired
        // docs from a previous session awaiting explicit recovery. Recover opens them all (tabs then
        // appear via the normal doc.opened mirror path); Discard drops them daemon-side; Later just
        // dismisses — the daemon keeps the entries, so the prompt returns on the next app start.
        {
            static bool s_recoveryModalOpen = false;
            static int  s_recoveryCount = 0;
            if (st.recoveryPromptCount > 0) {
                s_recoveryCount = st.recoveryPromptCount;
                st.recoveryPromptCount = 0;
                s_recoveryModalOpen = true;
            }
            if (s_recoveryModalOpen) ImGui::OpenPopup("Recover unsaved parts?##recovery");
            ImGui::SetNextWindowPos(ImVec2(vp->Pos.x + vp->Size.x * 0.5f,
                                           vp->Pos.y + vp->Size.y * 0.5f),
                                    ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
            bool ropen = true;
            if (ImGui::BeginPopupModal("Recover unsaved parts?##recovery", &ropen,
                                       ImGuiWindowFlags_AlwaysAutoResize |
                                           ImGuiWindowFlags_NoSavedSettings)) {
                ImGui::Text("%d unsaved part%s from a previous session.", s_recoveryCount,
                            s_recoveryCount == 1 ? "" : "s");
                ImGui::Spacing();
                ImGui::TextUnformatted("Recover them now, or discard them for good?");
                ImGui::Spacing();
                const float bw = 110.0f * scale;
                if (ImGui::Button("Recover", ImVec2(bw, 0))) {
                    if (auto* c = engine::client()) {
                        nlohmann::json r;
                        std::string e;
                        if (c->call("recovery_open", nlohmann::json{{"all", true}}, r, e)) {
                            // The daemon minted a fresh doc for each recovered
                            // orphan and returned their doc_ids in `opened`. Its
                            // doc.opened fanout EXCLUDES us (the caller), so mirror
                            // the tabs here from the return value — otherwise the
                            // recovered parts only surface on the next app launch.
                            std::vector<std::string> ids;
                            if (r.is_object() && r.contains("opened") &&
                                r["opened"].is_array()) {
                                for (const auto& o : r["opened"])
                                    if (o.is_object() && o.contains("doc_id") &&
                                        o["doc_id"].is_string())
                                        ids.push_back(o["doc_id"].get<std::string>());
                            }
                            core::mirrorRecoveredDocs(st, ids);
                            const int n = ids.empty() ? s_recoveryCount : (int)ids.size();
                            st.toast(ui::ToastLevel::Info,
                                     "Recovered " + std::to_string(n) + " part" +
                                         (n == 1 ? "" : "s"));
                        } else {
                            st.toast(ui::ToastLevel::Error, "Recovery failed: " + e);
                        }
                    }
                    s_recoveryModalOpen = false;
                    ImGui::CloseCurrentPopup();
                }
                ImGui::SameLine();
                if (ImGui::Button("Discard", ImVec2(bw, 0))) {
                    if (auto* c = engine::client()) {
                        nlohmann::json r;
                        std::string e;
                        if (!c->call("recovery_discard", nlohmann::json{{"all", true}}, r, e))
                            st.toast(ui::ToastLevel::Error, "Discard failed: " + e);
                    }
                    s_recoveryModalOpen = false;
                    ImGui::CloseCurrentPopup();
                }
                ImGui::SameLine();
                if (ImGui::Button("Later", ImVec2(90.0f * scale, 0))) {
                    s_recoveryModalOpen = false;
                    ImGui::CloseCurrentPopup();
                }
                ImGui::EndPopup();
            }
            if (!ropen) s_recoveryModalOpen = false;   // title-bar X = Later
        }

        // ---------- mesh-export tessellation options ----------
        // Raised by File > Export of a mesh format. Lets the user pick a quality preset or
        // fine-tune the chord/normal deflection, shows a live triangle estimate, then sets
        // requestExportFormat so the GL-thread pump runs the actual Save-As + export.
        {
            auto& d = st.exportTess;
            // Preset -> (linear mm, angular deg). Custom keeps whatever is in the fields.
            auto applyPreset = [&](int p) {
                if (p == 0)      { d.linDeflection = 0.2f;  d.angDeflectionDeg = 30.0f; }
                else if (p == 1) { d.linDeflection = 0.05f; d.angDeflectionDeg = 20.0f; }
                else if (p == 2) { d.linDeflection = 0.01f; d.angDeflectionDeg = 12.0f; }
                d.estimateDirty = true;
            };
            if (d.open) ImGui::OpenPopup("Export mesh options##exporttess");
            ImGui::SetNextWindowPos(ImVec2(vp->Pos.x + vp->Size.x * 0.5f,
                                           vp->Pos.y + vp->Size.y * 0.5f),
                                    ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
            ImGui::SetNextWindowSize(ImVec2(420.0f * scale, 0.0f), ImGuiCond_Appearing);
            bool eopen = true;
            if (ImGui::BeginPopupModal("Export mesh options##exporttess", &eopen,
                                       ImGuiWindowFlags_AlwaysAutoResize |
                                           ImGuiWindowFlags_NoSavedSettings)) {
                std::string fmtUpper = d.fmt;
                for (char& c : fmtUpper) c = (char)std::toupper((unsigned char)c);
                ImGui::Text("Tessellation quality for %s", fmtUpper.c_str());
                ImGui::Separator();
                ImGui::Spacing();

                ImGui::TextUnformatted("Quality");
                ImGui::SetNextItemWidth(-FLT_MIN);
                static const char* kPreset[4] = {"Coarse (fast, small file)", "Standard",
                                                 "Fine (smooth, large file)", "Custom"};
                if (ImGui::BeginCombo("##tesspreset", kPreset[d.preset < 0 || d.preset > 3 ? 1 : d.preset])) {
                    for (int i = 0; i < 4; ++i)
                        if (ImGui::Selectable(kPreset[i], d.preset == i)) {
                            d.preset = i;
                            if (i != 3) applyPreset(i);
                        }
                    ImGui::EndCombo();
                }

                ImGui::Spacing();
                if (ImGui::Checkbox("Advanced", &d.advanced)) {}
                if (d.advanced) {
                    ImGui::Indent();
                    float lin = d.linDeflection, angDeg = d.angDeflectionDeg;
                    ImGui::TextUnformatted("Linear deflection (max chord deviation)");
                    ImGui::SetNextItemWidth(-FLT_MIN);
                    if (ImGui::InputFloat("##lindef", &lin, 0.0f, 0.0f,
                                          d.relative ? "%.4g (fraction)" : "%.4g mm")) {}
                    if (ImGui::IsItemDeactivatedAfterEdit() && lin > 0.0f) {
                        d.linDeflection = lin; d.preset = 3; d.estimateDirty = true;
                    }
                    ImGui::TextUnformatted("Angular deflection (max normal deviation)");
                    ImGui::SetNextItemWidth(-FLT_MIN);
                    if (ImGui::SliderFloat("##angdef", &angDeg, 5.0f, 45.0f, "%.0f deg")) {}
                    if (ImGui::IsItemDeactivatedAfterEdit()) {
                        d.angDeflectionDeg = angDeg; d.preset = 3; d.estimateDirty = true;
                    }
                    if (ImGui::Checkbox("Relative deflection (scale with feature size)", &d.relative))
                        d.estimateDirty = true;
                    if (d.fmt == "stl")
                        ImGui::Checkbox("ASCII STL (default binary)", &d.ascii);
                    ImGui::Unindent();
                }

                // Live triangle/vertex estimate.
                ImGui::Spacing();
                if (d.estimateDirty) {
                    d.estimateDirty = false;
                    std::string eerr;
                    long long tris = 0, verts = 0;
                    const double angRad = (double)d.angDeflectionDeg * 3.14159265358979 / 180.0;
                    d.estimateValid = st.docMirror.exportEstimate(
                        st.worker, st.docMirror.docId(), (double)d.linDeflection, angRad,
                        d.relative, tris, verts, eerr);
                    d.estTris = tris; d.estVerts = verts;
                }
                if (d.estimateValid)
                    ImGui::TextDisabled("Estimated: %lld triangles, %lld vertices", d.estTris,
                                        d.estVerts);
                else
                    ImGui::TextDisabled("Estimate unavailable");

                ImGui::Spacing();
                ImGui::Separator();
                const float bw = 110.0f * scale;
                if (ImGui::Button("Export...", ImVec2(bw, 0))) {
                    d.open = false;
                    st.requestExportFormat = d.fmt;   // GL pump runs Save-As + export with d.*
                    ImGui::CloseCurrentPopup();
                }
                ImGui::SameLine();
                if (ImGui::Button("Cancel", ImVec2(bw, 0))) {
                    d.open = false;
                    ImGui::CloseCurrentPopup();
                }
                ImGui::EndPopup();
            }
            if (!eopen) d.open = false;  // dismissed via the title-bar X
        }

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

        // ---------- in-app profiler window + unified command/settings search ----------
        ui::drawProfilerWindow(st);
        ui::drawCommandSearch(st);

        // ---------- click empty space (any panel whitespace) to deselect everything ----------
        // The viewport's own full-window input surface handles empty-3D clicks; this covers the
        // panels: a left click with nothing hovered/active and no open popup clears the selection.
        if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
            !ImGui::IsAnyItemHovered() && !ImGui::IsAnyItemActive() &&
            !ImGui::IsPopupOpen(nullptr, ImGuiPopupFlags_AnyPopupId))
            st.clearSelection();

        // ---------- splash notifications (top-left glass; click to copy + clear) ----------
        ui::drawToasts(st);

        // Interwebz widget-family demo window (View > Interwebz demo).
        ui::InterwebzDemoDraw();

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

        // RE-27: Splat GL overlay — draw 3DGS splat over the RE Reconstruct panel when
        // splatLoaded. Runs after ImGui to composite over the workspace (same overlay pattern
        // as re_points). Guard: ReverseEng workspace + Reconstruct stage + splat ready.
        {
            ui::reveng::RevEngState& rs = ui::reveng::S();
            if (st.activeWorkspace == AppState::Workspace::ReverseEng &&
                rs.stage == ui::reveng::Stage::Reconstruct &&
                rs.splatLoaded && rs.splat.hasSplats()) {
                // Lazily build a neutral CameraXform (no live camera in 2D workspace mode).
                // We just want the billboard draw to run and composite into the overlay area.
                Renderer::CameraXform splatCam;
                splatCam.ssWidth  = dw;
                splatCam.ssHeight = dh;
                // Use a fixed look-at that frames the fixture: eye behind/above, looking at origin.
                Vec3 eye{22.0f, 18.0f, 38.0f};
                Vec3 center{0.0f, 0.0f, 7.5f};
                Vec3 up{0.0f, 0.0f, 1.0f};
                float aspect = dh > 0 ? (float)dw / (float)dh : 1.0f;
                Mat4 proj = perspective(35.0f * 3.14159265f / 180.0f, aspect, 1.0f, 500.0f);
                Mat4 view = lookAt(eye, center, up);
                splatCam.mvp    = mul(proj, view);
                splatCam.eye    = eye;
                splatCam.center = center;
                splatCam.radius = 20.0f;
                rs.splat.draw(splatCam);
            }
        }

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
        // cam shot: wait for the worker + the build/load scenario, then give the CAM tab a few
        // frames to come forward (camLoadJob re-requests focus for ~10 frames) before capturing.
        if (st.wantCam) shotFrame = st.camShotDone ? 60 : 99999;
        // simdisc: keep the loop alive until the scenario forced the study picker open, then
        // give the popup a few frames to draw before capturing.
        if (st.wantSimDisc) shotFrame = st.simDiscShotDone ? 30 : 99999;
        // T-G15 iconpipe: keep the loop alive (offscreen icon renders happen in tick()) until every
        // recipe is captured, then exit promptly — the --shot backbuffer capture is a throwaway.
        if (st.wantIconPipe) shotFrame = st.iconPipeShotDone ? 3 : 99999;
        // H2 daemon_h2: wall-clock scenario. captureFrame==-1 => write the notice PNG
        // this frame (hostd just respawned, chip still sticky). captureFrame>0 => final
        // post-reconnect shot at that absolute frame.
        if (st.wantDaemonH2) {
            if (st.daemonH2CaptureFrame == -1) {
                const char* slash = std::strrchr(screenshotPath, '/');
                std::string midPath = slash ? std::string(screenshotPath, slash + 1) +
                                                  "daemon_h2_notice.png"
                                            : std::string("daemon_h2_notice.png");
                std::vector<unsigned char> px(dw * dh * 4);
                glReadBuffer(GL_BACK);
                glReadPixels(0, 0, dw, dh, GL_RGBA, GL_UNSIGNED_BYTE, px.data());
                std::vector<unsigned char> flip(dw * dh * 4);
                for (int y = 0; y < dh; ++y)
                    std::memcpy(&flip[(dh - 1 - y) * dw * 4], &px[y * dw * 4], dw * 4);
                stbi_write_png(midPath.c_str(), dw, dh, 4, flip.data(), dw * 4);
                std::printf("[daemon_h2] notice screenshot -> %s (unreachable=%d)\n",
                            midPath.c_str(), (int)st.daemonUnreachable);
                std::fflush(stdout);
                st.daemonH2CaptureFrame = 0;  // clear sentinel; wait for phase 4 latch
            }
            shotFrame = (st.daemonH2CaptureFrame > 0) ? st.daemonH2CaptureFrame : 99999;
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

    // Clean-exit signal (daemon mode): closing the app normally is a DELIBERATE exit.
    //  * USER-owned docs (own tabs + adopted AI mirrors): doc_discard — drop the parts we're
    //    leaving unsaved so, once abandoned, the daemon doesn't retire them and flood the
    //    "Recover unsaved parts?" prompt next launch. This is what makes recovery CRASH-ONLY
    //    for user docs (a crash never reaches this teardown, so its docs are retired + offered).
    //    The user was already warned about these by the quit modal.
    //  * PURE AI mirror tabs (un-adopted): only detach_doc — never discard. The agent may still
    //    be building the doc (discard would degrade to detach anyway), but if the agent already
    //    finished a DIRTY doc, discarding here would purge its autosave and lose the work. A
    //    plain detach leaves it for the daemon's GC/retire tier so it's recoverable — closing
    //    the real data-loss hole. Screenshot/headless runs skip it.
    if (!shot && engine::daemonMode()) {
        if (auto* c = engine::client()) {
            for (const auto& t : st.docTabs) {
                if (t.docId.empty() || (t.kind != AppState::DocTab::TabKind::Part &&
                                        t.kind != AppState::DocTab::TabKind::Assembly))
                    continue;
                const bool userOwned = !t.aiHandoff || t.userAdopted;
                nlohmann::json res;
                std::string err;
                c->call(userOwned ? "doc_discard" : "detach_doc",
                        {{"doc_id", t.docId}}, res, err);
            }
        }
    }

    ui::persistChatHistory(st);  // save the active conversation transcript before teardown
    freeChatTextures(st.chat);  // release iteration thumbnails while the GL context is still live
    ui::freeIcons();            // C14: release atlas textures while GL context is still live
    ui::destroyCursors();       // W1-B: free the GLFW standard cursors
    st.rpc.shutdown();          // C39: drop pending async calls + abort the in-flight one
    st.stopMeshLoader();        // T-PERF3: join mesh thread before static dtors (avoid terminate)
    st.worker.stop();
    simClient.stop();  // C19: shut down the second worker process
#ifdef AI_CAD_ENABLE_CEF
    ui::cef::shutdown();  // no-op unless CEF was initialised (browsers close in their dtors)
#endif
    ImGui_ImplOpenGL3_Shutdown();
    ImGui_ImplGlfw_Shutdown();
    ImGui::DestroyContext();
    glfwDestroyWindow(win);
    glfwTerminate();
    return 0;
}
