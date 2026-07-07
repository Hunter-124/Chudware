// CamPanel — the CAM workspace UI (tree + toolbar + backplot + sim + post), driving the
// C++ core's cam_* RPCs. File-static singleton state; standalone dock window.
#include "imgui.h"

#include "cam_panel.h"
#include "cam_mirror.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#include "../../core/app_state.h"
#include "../../ui_theme.h"    // ui::palette (opaque title-bar / border override for this window)
#include "../feature_tree.h"   // fmRowPill / fmTreeIcon / fmSectionDivider — the ratified FM row
                               // styling, reused so the Manufacture tree matches the FeatureManager

namespace ui {

namespace {

struct CamUI {
    CamMirror mirror;
    std::string docId;                 // cam job doc id ("" = no job)
    bool show = false;
    int  focusFrames = 0;              // >0: keep popping the CAM tab forward (set by camLoadJob)
    bool floatForShot = false;         // shot-only: float the panel so a headless capture sees it
    bool backplot = true;              // draw toolpaths in the 3D viewport
    int selSetup = 0;
    int selOp = -1;
    nlohmann::json lastExtract;        // last cam_extract result (geometry source)
    std::string partStep;              // temp STEP path of the exported current part
    std::string sourceModel;           // display name of the part doc the job extracts from
    std::vector<std::string> dialects;
    bool   dialectsInflight = false;   // async cam_post_dialects in flight (never re-fire)
    double dialectsNextTry  = 0.0;     // failure cooldown: no retry before this ImGui::GetTime()
    int postIdx = 0;
    // Workspace-tree state (drawCamTreeBody — the Manufacture left column):
    int  generatedRev = -1;            // mirror revision at the last successful cam_generate
    int  genWarnings  = 0;             // warning count from that generate (op status)
    bool flavorPopup  = false;         // ribbon "Flavor" clicked: open the dialect popup
    bool floatUndock  = false;         // one-shot: float (undock) the detail panel on next open
    int  selKind = 0;                  // visual tree selection: 0=none 1=job 2=jobsetup 3=stock
                                       // 4=setup 5=op 6=tool 7=models
    int  selTool = -1;                 // selected Tools row (selKind==6); separate from selOp
    std::string gcode;
    std::string gcodePath;
    nlohmann::json sim;                // last cam_simulate result
    std::string status;                // last action status / error
    // operation form buffers (selected op)
    char opName[64] = "";
    float fStepover = 0, fStepdown = 0, fStockLeave = 0, fFeed = 0, fRpm = 0;
    float hTop = 0, hBottom = -5, hClear = 15, hRetract = 5;
    int loadedOpFor = -1;
};
CamUI g;

// ---- RPC helpers (synchronous; cam_* routes to the C++ core via WorkerClient) ----
bool camCall(AppState& st, const char* method, const nlohmann::json& params, nlohmann::json& out) {
    std::string err;
    bool ok = st.worker.call(method, params, out, err);
    if (!ok) {
        g.status = std::string(method) + ": " + (err.empty() ? std::string("failed") : err);
    }
    return ok;
}

// Post-dialect list, fetched ONCE via the async rpc queue. This runs on per-frame draw paths
// (drawCamManager / the Manufacture tree), so it must never issue a synchronous worker.call:
// a failing worker would then be re-hit every frame from the GL thread (that pattern hard-froze
// the app once — see commit 50a54308). Guards: in-flight flag + a 10s failure cooldown.
void refreshDialects(AppState& st) {
    if (!g.dialects.empty() || g.dialectsInflight) return;
    if (ImGui::GetTime() < g.dialectsNextTry) return;
    g.dialectsInflight = true;
    st.rpc.call("cam_post_dialects", nlohmann::json::object(), [](const core::RpcResult& r) {
        g.dialectsInflight = false;
        if (r.ok && r.result.contains("dialects"))
            for (auto& d : r.result["dialects"]) g.dialects.push_back(d.value("id", std::string()));
        if (g.dialects.empty()) g.dialectsNextTry = ImGui::GetTime() + 10.0;  // failed: cool down
    });
}

void loadTree(const nlohmann::json& r) { g.mirror.loadTree(r); }

bool ensureJob(AppState& st) {
    if (!g.docId.empty()) return true;
    nlohmann::json r;
    if (!camCall(st, "cam_new", {{"name", "CAM Job"}, {"post", "grbl"}}, r)) return false;
    g.docId = r.value("doc_id", std::string());
    loadTree(r);
    return !g.docId.empty();
}

nlohmann::json applyOps(AppState& st, const nlohmann::json& ops) {
    nlohmann::json r;
    if (camCall(st, "cam_apply_ops", {{"doc_id", g.docId}, {"base_revision", -1}, {"ops", ops}}, r))
        loadTree(r);
    return r;
}

// Export the active PART tab to a temp STEP (cached), for cam_extract.
bool exportPartStep(AppState& st) {
    if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) { g.status = "no active part"; return false; }
    const auto& tab = st.docTabs[st.activeTab];
    if (tab.docId.empty()) { g.status = "active tab is not a part"; return false; }
    std::filesystem::path p = std::filesystem::temp_directory_path() / "aicad_cam_part.step";
    nlohmann::json r;
    if (!camCall(st, "export_file", {{"doc_id", tab.docId}, {"path", p.string()}, {"format", "step"}}, r))
        return false;
    g.partStep = p.string();
    g.sourceModel = tab.title.empty() ? tab.docId : tab.title;  // Models node in the CAM tree
    return true;
}

// cam_extract on the current part (exports STEP first). kind: all|stock|holes|section|mesh|profile.
nlohmann::json extract(AppState& st, const std::string& kind, double z = 0, bool useZ = false) {
    nlohmann::json out;
    if (g.partStep.empty() && !exportPartStep(st)) return out;
    nlohmann::json params = {{"path", g.partStep}, {"kind", kind}, {"deflection", 0.1}};
    if (useZ) params["z"] = z;
    camCall(st, "cam_extract", params, out);
    return out;
}

const char* setupId() {
    if (g.selSetup >= 0 && g.selSetup < (int)g.mirror.setups().size())
        return g.mirror.setups()[g.selSetup].id.c_str();
    return "";
}

// Add an operation of `type`, pulling appropriate geometry from the current part.
void addOperation(AppState& st, const std::string& type, int toolNumber) {
    if (!ensureJob(st)) return;
    if (g.mirror.setups().empty()) { g.status = "add a setup first"; return; }
    std::string sid = setupId();

    nlohmann::json geom = nlohmann::json::object();
    nlohmann::json heights = {{"top", 0}, {"bottom", -5}, {"clearance", 15}, {"retract", 5}};
    bool is3d = type.rfind("parallel", 0) == 0 || type.rfind("contour3", 0) == 0 ||
                type.rfind("scallop", 0) == 0 || type.rfind("pencil", 0) == 0 ||
                type.rfind("pocket3", 0) == 0 || type.rfind("adaptive_clear", 0) == 0 ||
                type.rfind("radial", 0) == 0 || type.rfind("spiral", 0) == 0;
    bool isTurn = type.rfind("turn", 0) == 0;
    bool isDrill = (type == "drill" || type == "thread_mill");
    bool isCut = (type == "cut2d");

    if (is3d) {
        nlohmann::json m = extract(st, "mesh");
        if (m.contains("mesh")) geom["mesh"] = m["mesh"];
    } else if (isTurn) {
        nlohmann::json pr = extract(st, "profile");
        if (pr.contains("profile")) geom["profile"] = pr["profile"];
    } else if (isDrill) {
        nlohmann::json h = extract(st, "holes");
        if (h.contains("points")) geom["points"] = h["points"];
    } else if (type == "face") {
        // face uses the stock top automatically; also set heights bottom slightly below top
        heights["bottom"] = -1.0;
    } else {  // 2.5D milling + cut2d: planar section regions at the top
        nlohmann::json s = extract(st, "section", 0.0, true);
        if (s.contains("regions")) {
            if (isCut) {
                nlohmann::json contours = nlohmann::json::array();
                for (auto& reg : s["regions"]) if (reg.contains("outer")) contours.push_back(reg["outer"]);
                geom["contours"] = contours;
            } else {
                geom["regions"] = s["regions"];
            }
        }
    }
    // pull stock bottom for a sensible cut depth
    if (g.lastExtract.empty()) g.lastExtract = extract(st, "stock");
    if (g.lastExtract.contains("bbox")) {
        auto& mn = g.lastExtract["bbox"]["min"];
        if (mn.is_array() && mn.size() >= 3) heights["bottom"] = mn[2].get<double>();
    }

    nlohmann::json op = {{"type", type}, {"tool_number", toolNumber}, {"heights", heights}};
    if (!geom.empty()) op["geometry"] = geom;
    nlohmann::json r = applyOps(st, nlohmann::json::array({nlohmann::json{
        {"op", "add_operation"}, {"setup_id", sid}, {"operation", op}}}));
    if (r.contains("revision")) {
        g.status = "added " + type;
        // select the new last op
        if (!g.mirror.setups().empty()) {
            g.selOp = (int)g.mirror.setups()[g.selSetup].ops.size() - 1;
            g.loadedOpFor = -1;
        }
    }
}

void doGenerate(AppState& st) {
    if (g.docId.empty()) { g.status = "no job"; return; }
    nlohmann::json r;
    if (camCall(st, "cam_generate", {{"doc_id", g.docId}}, r)) {
        g.mirror.loadToolpaths(r);
        int n = r.contains("toolpaths") ? (int)r["toolpaths"].size() : 0;
        g.generatedRev = g.mirror.revision();  // ops now read "generated" in the workspace tree
        g.genWarnings = (r.contains("warnings") && r["warnings"].is_array())
                            ? (int)r["warnings"].size() : 0;
        g.status = "generated " + std::to_string(n) + " toolpath(s)";
        if (g.genWarnings > 0) g.status += " (" + std::to_string(g.genWarnings) + " warning)";
    }
}

void doSimulate(AppState& st) {
    if (g.docId.empty()) return;
    nlohmann::json r;
    if (camCall(st, "cam_simulate", {{"doc_id", g.docId}}, r)) {
        g.sim = r;
        g.status = "simulated";
    }
}

void doPost(AppState& st) {
    if (g.docId.empty()) return;
    std::string dialect = (g.postIdx >= 0 && g.postIdx < (int)g.dialects.size()) ? g.dialects[g.postIdx] : "grbl";
    nlohmann::json r;
    if (camCall(st, "cam_post", {{"doc_id", g.docId}, {"dialect", dialect}}, r)) {
        g.gcode = r.value("gcode", std::string());
        // also write to a temp .nc so the operator can grab it
        std::filesystem::path p = std::filesystem::temp_directory_path() / ("aicad_cam." + dialect + ".nc");
        std::ofstream f(p);
        if (f) { f << g.gcode; g.gcodePath = p.string(); }
        g.status = "posted " + dialect + " (" + std::to_string(r.value("line_count", 0)) + " lines)";
    }
}

// ---- 2D backplot canvas (top view) ----
void drawBackplot(AppState& /*st*/) {
    const CamBackplot& bp = g.mirror.backplot();
    ImVec2 avail = ImGui::GetContentRegionAvail();
    if (avail.y < 80) avail.y = 200;
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->AddRectFilled(p0, ImVec2(p0.x + avail.x, p0.y + avail.y), IM_COL32(24, 26, 30, 255));
    if (bp.empty()) {
        dl->AddText(ImVec2(p0.x + 10, p0.y + 10), IM_COL32(160, 160, 160, 255),
                    "No toolpaths. Add operations, then Generate.");
        ImGui::Dummy(avail);
        return;
    }
    float w = bp.maxx - bp.minx, h = bp.maxy - bp.miny;
    if (w < 1e-3f) w = 1; if (h < 1e-3f) h = 1;
    float pad = 12.0f;
    float sx = (avail.x - 2 * pad) / w, sy = (avail.y - 2 * pad) / h;
    float s = std::min(sx, sy);
    float ox = p0.x + pad - bp.minx * s;
    float oy = p0.y + avail.y - pad + bp.miny * s;  // flip Y for screen
    auto line = [&](const std::vector<float>& v, ImU32 col, float th) {
        for (size_t i = 0; i + 5 < v.size(); i += 6) {
            ImVec2 a(ox + v[i] * s, oy - v[i + 1] * s);
            ImVec2 b(ox + v[i + 3] * s, oy - v[i + 4] * s);
            dl->AddLine(a, b, col, th);
        }
    };
    line(bp.rapid,  IM_COL32(200, 60, 60, 120), 1.0f);   // rapid = faint red
    line(bp.lead,   IM_COL32(80, 200, 120, 200), 1.5f);  // lead = green
    line(bp.plunge, IM_COL32(80, 180, 220, 220), 1.5f);  // plunge = cyan
    line(bp.cut,    IM_COL32(90, 150, 240, 255), 1.6f);  // cut = blue
    line(bp.arc,    IM_COL32(150, 130, 240, 255), 1.6f); // arc = violet
    char info[96];
    std::snprintf(info, sizeof(info), "XY %.1f x %.1f mm   Z %.1f..%.1f", w, h, bp.minz, bp.maxz);
    dl->AddText(ImVec2(p0.x + 8, p0.y + 8), IM_COL32(150, 160, 170, 255), info);
    ImGui::Dummy(avail);
}

void drawSimSummary() {
    if (g.sim.empty() || !g.sim.contains("results")) { ImGui::TextDisabled("Run Simulate to see material removal + time."); return; }
    for (auto& r : g.sim["results"]) {
        ImGui::Separator();
        ImGui::Text("Stock:      %.0f mm^3", r.value("stock_volume_mm3", 0.0));
        ImGui::Text("Removed:    %.0f mm^3 (%.0f%%)", r.value("removed_volume_mm3", 0.0),
                    100.0 * r.value("machined_fraction", 0.0));
        ImGui::Text("Remaining:  %.0f mm^3", r.value("remaining_volume_mm3", 0.0));
        double t = r.value("cycle_time_s", 0.0);
        ImGui::Text("Cycle time: %dm %02ds", (int)(t / 60), (int)t % 60);
        int gouges = r.value("gouges", 0), rapids = r.value("rapid_collisions", 0);
        if (gouges > 0) { ImGui::PushStyleColor(ImGuiCol_Text, IM_COL32(230, 120, 60, 255));
            ImGui::Text("GOUGES: %d (max %.3f mm)", gouges, r.value("max_gouge_mm", 0.0)); ImGui::PopStyleColor(); }
        if (rapids > 0) { ImGui::PushStyleColor(ImGuiCol_Text, IM_COL32(230, 120, 60, 255));
            ImGui::Text("RAPID THROUGH STOCK: %d", rapids); ImGui::PopStyleColor(); }
        if (gouges == 0 && rapids == 0) { ImGui::PushStyleColor(ImGuiCol_Text, IM_COL32(90, 200, 120, 255));
            ImGui::TextUnformatted("No gouges or rapid collisions."); ImGui::PopStyleColor(); }
    }
}

}  // namespace

// ---- ribbon command handler ----
void camHandleCommand(AppState& st, const std::string& cmd) {
    // In the Manufacture WORKSPACE the CAM job tree (left column) + ribbon are the primary UI:
    // commands fire silently against the tree selection and must NOT pop the floating detail
    // panel. Everywhere else (e.g. the Manufacture ribbon tab from the Model workspace) keep the
    // legacy behavior of revealing the panel.
    const bool camWs = (st.activeWorkspace == AppState::Workspace::Manufacture);
    if (!camWs) g.show = true;
    refreshDialects(st);
    if (cmd == "cam_show3d") {          // ribbon Output toggle: viewport backplot overlay
        g.backplot = !g.backplot;
        g.status = g.backplot ? "toolpaths shown in 3D" : "toolpaths hidden";
        return;
    }
    if (cmd == "cam_flavor") {          // ribbon Output dropdown: pick the post dialect
        if (camWs) g.flavorPopup = true;    // popup rendered by drawCamTreeBody (left column)
        else       g.show = true;           // elsewhere: the panel's combo has the picker
        return;
    }
    if (cmd == "cam_set_stock") {
        // Re-extract the current part's stock box and apply it to the selected setup.
        if (!ensureJob(st)) return;
        if (g.mirror.setups().empty()) { g.status = "add a setup first"; return; }
        nlohmann::json s = extract(st, "stock");
        if (!s.contains("stock")) { g.status = "stock extraction failed"; return; }
        nlohmann::json box = s["stock"]["box"];
        box["type"] = "box"; box["off_ztop"] = 1.0; box["material"] = "aluminum";
        applyOps(st, nlohmann::json::array({nlohmann::json{
            {"op", "set_stock"}, {"setup_id", setupId()}, {"stock", box}}}));
        g.status = "stock set from part";
        return;
    }
    if (cmd == "cam_new_job") { g.docId.clear(); g.mirror.clear(); ensureJob(st); g.status = "new CAM job"; return; }
    if (cmd == "cam_extract") {
        if (!ensureJob(st)) return;
        g.lastExtract = extract(st, "all");
        // seed a setup from the extracted stock if none exists
        if (g.mirror.setups().empty() && g.lastExtract.contains("stock")) {
            nlohmann::json box = g.lastExtract["stock"]["box"];
            box["type"] = "box"; box["off_ztop"] = 1.0; box["material"] = "aluminum";
            applyOps(st, nlohmann::json::array({nlohmann::json{
                {"op", "add_setup"}, {"setup", {{"name", "Setup1"}, {"machine", {{"kind", "mill3"}}}, {"stock", box}}}}}));
        }
        int nh = g.lastExtract.contains("holes") ? (int)g.lastExtract["holes"].size() : 0;
        g.status = "extracted stock + " + std::to_string(nh) + " hole(s)";
        return;
    }
    if (cmd == "cam_add_setup") {
        if (!ensureJob(st)) return;
        applyOps(st, nlohmann::json::array({nlohmann::json{
            {"op", "add_setup"},
            {"setup", {{"name", "Setup" + std::to_string(g.mirror.setups().size() + 1)},
                       {"machine", {{"kind", "mill3"}}},
                       {"stock", {{"type", "box_offset"}, {"off_ztop", 1.0}, {"material", "aluminum"}}}}}}}));
        g.selSetup = (int)g.mirror.setups().size() - 1;
        return;
    }
    if (cmd == "cam_add_tool") {
        if (!ensureJob(st)) return;
        int n = (int)g.mirror.tools().size() + 1;
        applyOps(st, nlohmann::json::array({nlohmann::json{
            {"op", "add_tool"}, {"tool", {{"number", n}, {"type", "endmill_flat"}, {"diameter", 6}, {"flutes", 3},
                                          {"name", "6mm flat endmill"}}}}}));
        g.status = "added tool T" + std::to_string(n);
        return;
    }
    if (cmd == "cam_generate") { doGenerate(st); return; }
    if (cmd == "cam_simulate") { doSimulate(st); return; }
    if (cmd == "cam_post")     { doPost(st); return; }
    // operation commands: cam_op_<type>
    static const std::pair<const char*, const char*> kOps[] = {
        {"cam_op_face", "face"}, {"cam_op_contour", "contour2d"}, {"cam_op_pocket", "pocket2d"},
        {"cam_op_adaptive", "adaptive2d"}, {"cam_op_slot", "slot"}, {"cam_op_chamfer", "chamfer2d"},
        {"cam_op_engrave", "engrave"}, {"cam_op_drill", "drill"}, {"cam_op_thread", "thread_mill"},
        {"cam_op_parallel3d", "parallel3d"}, {"cam_op_waterline3d", "contour3d"},
        {"cam_op_scallop3d", "scallop3d"}, {"cam_op_pocket3d", "pocket3d"},
        {"cam_op_turn_rough", "turn_rough"}, {"cam_op_turn_finish", "turn_finish"},
        {"cam_op_turn_face", "turn_face"}, {"cam_op_turn_part", "turn_part"},
        {"cam_op_laser", "cut2d"}, {"cam_op_probe", "probe"},
    };
    for (auto& kv : kOps)
        if (cmd == kv.first) {
            int tool = g.mirror.tools().empty() ? 1 : g.mirror.tools().front().number;
            addOperation(st, kv.second, tool);
            return;
        }
}

const CamBackplot* camActiveBackplot() {
    // No g.show gate anymore: in the Manufacture workspace the 3D viewport is the central body
    // and the floating panel stays closed — the ribbon "Show in 3D" toggle (g.backplot) is the
    // one switch for the overlay.
    if (!g.backplot || g.mirror.backplot().empty()) return nullptr;
    return &g.mirror.backplot();
}

void camEnsureVisible() { g.show = true; }

void camRequestFocus() {
    g.show = true;
    // Keep focusFrames non-zero every frame the Manufacture workspace is active so the CAM window
    // wins the docked-tab race against the Viewport window in the shared central dock node.
    // Set to 3 each frame so it's always non-zero (drawCamManager decrements by 1 per frame).
    g.focusFrames = 3;
}

// Shot-only hook: make the CAM panel float (undock + centre) so a headless screenshot of the
// AI_CAD_SHOT=cam scenario can actually show the populated workspace. Never called in normal use.
void camFloatForShot() { g.floatForShot = true; g.show = true; }

// Adopt an existing CAM job doc (built core-side by the copilot's CAM fast-path) into the
// panel so it shows up populated instead of empty. Loads the job tree (setups/ops/tools).
void camLoadJob(AppState& st, const std::string& docId) {
    if (docId.empty()) return;
    g.docId = docId;
    g.selSetup = 0;
    g.selOp = -1;
    g.loadedOpFor = -1;
    nlohmann::json r;
    if (camCall(st, "cam_tree", {{"doc_id", docId}}, r)) {
        loadTree(r);
        g.status = "loaded CAM job from copilot";
    }
    // Also pull the generated toolpaths so the backplot / "Show in 3D" works immediately (the
    // job was already generated core-side by cam_program; cam_generate returns the cached paths).
    nlohmann::json gp;
    if (camCall(st, "cam_generate", {{"doc_id", docId}}, gp)) {
        g.mirror.loadToolpaths(gp);
        g.generatedRev = g.mirror.revision();  // copilot jobs arrive pre-generated
        g.genWarnings = (gp.contains("warnings") && gp["warnings"].is_array())
                            ? (int)gp["warnings"].size() : 0;
    }
    g.show = true;
    g.focusFrames = 10;  // surface the CAM tab for a few frames even if the panel was already
                         // open in the background (out-competes a one-off viewport focus grab)
}

// ---- the panel ----
void drawCamManager(AppState& st) {
    if (!g.show) return;
    refreshDialects(st);
    // Pop the CAM tab forward the frame the workspace first becomes visible (the user switched to
    // the Manufacture ribbon), so the docked CAM editor surfaces instead of staying behind the
    // Viewport tab. Mirrors the ECAD panel. Docked into the central node by buildDefaultLayout.
    // Shot-only (set via camFloatForShot() from the AI_CAD_SHOT=cam scenario): float the panel
    // (undock + centre) so a headless capture sees the populated workspace — a central-node-docked
    // tab won't surface over the full-bleed 3D in an offscreen render. No-op in normal use.
    if (g.floatForShot) {
        ImGui::SetNextWindowDockID(0, ImGuiCond_Always);
        ImGui::SetNextWindowPos(ImVec2(330, 210), ImGuiCond_Always);
        ImGui::SetNextWindowSize(ImVec2(660, 470), ImGuiCond_Always);
    } else if (g.floatUndock) {
        // Opened as the DETAIL editor from the Manufacture workspace tree: float it (undock)
        // so it never steals the central dock node from the 3D viewport in that workspace.
        ImGui::SetNextWindowDockID(0, ImGuiCond_Always);
        ImGui::SetNextWindowPos(ImVec2(360, 220), ImGuiCond_Appearing);
        ImGui::SetNextWindowSize(ImVec2(720, 480), ImGuiCond_Appearing);
        g.floatUndock = false;
    } else {
        ImGui::SetNextWindowSize(ImVec2(720, 480), ImGuiCond_FirstUseEver);
    }
    static bool prevShown = false;
    if (g.focusFrames > 0 || (g.show && !prevShown)) ImGui::SetNextWindowFocus();  // applies to the Begin below
    prevShown = g.show;
    if (g.focusFrames > 0) g.focusFrames--;
    // The global theme makes window title bars fully transparent (a clean look for docked panels),
    // but a FLOATING CAM window then reads as see-through at the top. Give this window an opaque
    // title bar and a solid (fully opaque) border so it stands on its own whether floating or
    // docked. Pushed before Begin (window decoration colours are read there) and popped right after.
    const ui::Palette& pal = ui::palette;
    const ImVec4 titleOpaque(pal.headerBg[0], pal.headerBg[1], pal.headerBg[2], 1.0f);
    float camBorder[4]; ui::Palette::withAlpha(pal.border, camBorder, 1.0f);
    ImGui::PushStyleColor(ImGuiCol_TitleBg,       titleOpaque);
    ImGui::PushStyleColor(ImGuiCol_TitleBgActive, titleOpaque);
    ImGui::PushStyleColor(ImGuiCol_Border, ImVec4(camBorder[0], camBorder[1], camBorder[2], camBorder[3]));
    const bool camOpen = ImGui::Begin("CAM", &g.show);
    ImGui::PopStyleColor(3);
    if (!camOpen) { ImGui::End(); return; }

    // toolbar
    if (ImGui::Button("New Job")) camHandleCommand(st, "cam_new_job");
    ImGui::SameLine(); if (ImGui::Button("Extract Part")) camHandleCommand(st, "cam_extract");
    ImGui::SameLine(); if (ImGui::Button("Add Setup")) camHandleCommand(st, "cam_add_setup");
    ImGui::SameLine(); if (ImGui::Button("Add Tool")) camHandleCommand(st, "cam_add_tool");
    ImGui::Separator();
    // operation buttons
    auto opBtn = [&](const char* label, const char* cmd) { if (ImGui::Button(label)) camHandleCommand(st, cmd); ImGui::SameLine(); };
    opBtn("Face", "cam_op_face"); opBtn("Contour", "cam_op_contour"); opBtn("Pocket", "cam_op_pocket");
    opBtn("Adaptive", "cam_op_adaptive"); opBtn("Drill", "cam_op_drill"); opBtn("3D Parallel", "cam_op_parallel3d");
    opBtn("Waterline", "cam_op_waterline3d"); opBtn("Turn", "cam_op_turn_rough"); if (ImGui::Button("Laser")) camHandleCommand(st, "cam_op_laser");
    ImGui::Separator();
    if (ImGui::Button("Generate")) doGenerate(st);
    ImGui::SameLine(); if (ImGui::Button("Simulate")) doSimulate(st);
    ImGui::SameLine(); ImGui::Checkbox("Show in 3D", &g.backplot);
    ImGui::SameLine(); ImGui::TextDisabled("|");
    ImGui::SameLine(); ImGui::SetNextItemWidth(120);
    if (!g.dialects.empty()) {
        std::vector<const char*> items;
        for (auto& d : g.dialects) items.push_back(d.c_str());
        ImGui::Combo("##post", &g.postIdx, items.data(), (int)items.size());
        ImGui::SameLine(); if (ImGui::Button("Post")) doPost(st);
    }
    if (!g.status.empty()) { ImGui::SameLine(); ImGui::TextDisabled("%s", g.status.c_str()); }

    ImGui::Separator();
    ImGui::Columns(2, "cam_cols");
    ImGui::SetColumnWidth(0, 240);

    // ---- LEFT: tree ----
    ImGui::BeginChild("cam_tree");
    ImGui::TextDisabled("Job: %s  (rev %d)", g.mirror.name().c_str(), g.mirror.revision());
    const auto& setups = g.mirror.setups();
    for (int si = 0; si < (int)setups.size(); ++si) {
        const auto& s = setups[si];
        ImGui::PushID(si);
        bool open = ImGui::TreeNodeEx(s.name.c_str(), (si == g.selSetup ? ImGuiTreeNodeFlags_DefaultOpen : 0) |
                                                          ImGuiTreeNodeFlags_OpenOnArrow);
        if (ImGui::IsItemClicked()) g.selSetup = si;
        if (open) {
            ImGui::TextDisabled("machine: %s", s.machine.c_str());
            for (int oi = 0; oi < (int)s.ops.size(); ++oi) {
                const auto& o = s.ops[oi];
                std::string lbl = o.name.empty() ? o.type : o.name;
                lbl += "  [T" + std::to_string(o.tool_number) + "]";
                if (o.suppressed) lbl += " (off)";
                if (ImGui::Selectable(lbl.c_str(), si == g.selSetup && oi == g.selOp)) {
                    g.selSetup = si; g.selOp = oi; g.loadedOpFor = -1;
                }
            }
            ImGui::TreePop();
        }
        ImGui::PopID();
    }
    ImGui::Separator();
    ImGui::TextDisabled("Tools");
    for (const auto& t : g.mirror.tools())
        ImGui::BulletText("T%d %s  \xC3\x98%.2f", t.number, t.type.c_str(), t.diameter);
    ImGui::EndChild();

    // ---- RIGHT: tabs ----
    ImGui::NextColumn();
    if (ImGui::BeginTabBar("cam_tabs")) {
        if (ImGui::BeginTabItem("Toolpaths")) { drawBackplot(st); ImGui::EndTabItem(); }
        if (ImGui::BeginTabItem("Simulate")) {
            if (ImGui::Button("Run Material-Removal Sim")) doSimulate(st);
            drawSimSummary();
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("G-code")) {
            if (ImGui::Button("Post")) doPost(st);
            if (!g.gcodePath.empty()) { ImGui::SameLine(); ImGui::TextDisabled("-> %s", g.gcodePath.c_str()); }
            ImGui::Separator();
            ImGui::InputTextMultiline("##gcode", g.gcode.data(), g.gcode.size() + 1,
                                      ImVec2(-1, -1), ImGuiInputTextFlags_ReadOnly);
            ImGui::EndTabItem();
        }
        ImGui::EndTabBar();
    }
    ImGui::Columns(1);
    ImGui::End();
}

// ---- the Manufacture workspace LEFT TREE ------------------------------------------------------
// Drawn INSIDE drawLeftPane's pinned glass column (layout.cpp routes the Manufacture workspace
// here, the same way Simulate routes to SimManager::drawTreeBody). Shows the real job structure —
// Job / Job Setup / Stock Setup / Machining Setups (ops with status) / Tools / Models — styled
// with the exported FeatureManager row helpers. Selecting a node sets the context the ribbon
// commands act on (g.selSetup / g.selOp); double-click opens the floating panel as the detail
// editor. NO RPCs on this per-frame path (refreshDialects is async + cooldown-guarded).
namespace {

void camTreeOpenDetail() { g.show = true; g.focusFrames = 3; g.floatUndock = true; }

}  // namespace

void drawCamTreeBody(AppState& st) {
    const float S = ui::uiScale;
    refreshDialects(st);   // async + guarded; feeds the flavor popup + Job Setup row
    fmRowStripeReset();

    // Ribbon "Flavor" dropdown: opened here (this body draws every frame in Manufacture) at the
    // mouse position, which is still on the ribbon button the frame after the click.
    if (g.flavorPopup) {
        ImGui::OpenPopup("##cam_flavor_popup");
        ImGui::SetNextWindowPos(ImGui::GetMousePos(), ImGuiCond_Appearing);
        g.flavorPopup = false;
    }
    if (ImGui::BeginPopup("##cam_flavor_popup")) {
        ImGui::TextDisabled("G-code flavor");
        ImGui::Separator();
        if (g.dialects.empty())
            ImGui::TextDisabled(g.dialectsInflight ? "loading..." : "unavailable");
        for (int i = 0; i < (int)g.dialects.size(); ++i)
            if (ImGui::Selectable(g.dialects[i].c_str(), i == g.postIdx)) {
                g.postIdx = i;
                g.status = "post flavor: " + g.dialects[i];
            }
        ImGui::EndPopup();
    }

    if (g.docId.empty()) {
        ImGui::Spacing();
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x);
        ImGui::TextDisabled("No CAM job.\n\nUse New Job or Extract Part on the Manufacture "
                            "ribbon to start one from the active part.");
        ImGui::PopTextWrapPos();
        if (!g.status.empty()) { ImGui::Spacing(); ImGui::TextDisabled("%s", g.status.c_str()); }
        return;
    }

    const auto& setups = g.mirror.setups();
    if (g.selSetup >= (int)setups.size()) g.selSetup = setups.empty() ? 0 : (int)setups.size() - 1;
    const bool generated = (g.generatedRev >= 0 && g.generatedRev == g.mirror.revision());

    // Deferred mutations: context-menu actions apply ops AFTER the tree walk (applyOps reloads
    // the mirror, which would invalidate the rows we're iterating).
    nlohmann::json pendingOps = nlohmann::json::array();

    // Leaf row helper: FM pill + 16px icon + selectable label. Returns clicked; double-click
    // opens the detail editor.
    auto leafRow = [&](const char* id, const char* icon, const char* fb,
                       const std::string& label, bool selected) -> bool {
        ImGui::PushID(id);
        FmRowRect rr = fmRowPill(selected);
        fmTreeIcon(icon, fb);
        ImGui::SameLine(0, 4.0f * S);
        bool clicked = ImGui::Selectable(label.c_str(), selected,
                                         ImGuiSelectableFlags_AllowDoubleClick);
        const bool dbl = ImGui::IsItemHovered() && ImGui::IsMouseDoubleClicked(0);
        fmRowPillEnd(rr, selected);
        ImGui::PopID();
        if (dbl) camTreeOpenDetail();
        return clicked;
    };

    // ---- Job root ----
    const bool jobSel = (g.selKind == 1);
    {
        FmRowRect rr = fmRowPill(jobSel);
        fmTreeIcon("new_doc", "J");
        ImGui::SameLine(0, 4.0f * S);
        std::string jobLbl = (g.mirror.name().empty() ? std::string("CAM Job") : g.mirror.name())
                             + "  (rev " + std::to_string(g.mirror.revision()) + ")";
        const bool jobOpen = ImGui::TreeNodeEx("##cam_job",
            ImGuiTreeNodeFlags_DefaultOpen | ImGuiTreeNodeFlags_OpenOnArrow |
            ImGuiTreeNodeFlags_SpanAvailWidth, "%s", jobLbl.c_str());
        if (ImGui::IsItemClicked() && !ImGui::IsItemToggledOpen()) g.selKind = 1;
        if (ImGui::IsItemHovered() && ImGui::IsMouseDoubleClicked(0)) camTreeOpenDetail();
        fmRowPillEnd(rr, jobSel);
        if (!jobOpen) {
            if (!g.status.empty()) { ImGui::Spacing(); ImGui::TextDisabled("%s", g.status.c_str()); }
            return;
        }
    }

    // ---- Job Setup (machine family + post flavor) ----
    {
        std::string flavor = (g.postIdx >= 0 && g.postIdx < (int)g.dialects.size())
                                 ? g.dialects[g.postIdx] : g.mirror.post();
        if (flavor.empty()) flavor = "grbl";
        if (leafRow("cam_jobsetup", "settings", "S", "Job Setup — post: " + flavor,
                    g.selKind == 2))
            g.selKind = 2;
    }

    // ---- Stock Setup (selected setup's stock summary) ----
    {
        std::string lbl = "Stock Setup";
        if (!setups.empty() && setups[g.selSetup].stock.is_object() &&
            !setups[g.selSetup].stock.empty()) {
            const auto& sk = setups[g.selSetup].stock;
            lbl += " — " + sk.value("type", std::string("box"));
            const std::string mat = sk.value("material", std::string());
            if (!mat.empty()) lbl += ", " + mat;
        } else {
            lbl += " — (none)";
        }
        if (leafRow("cam_stock", "extrude", "K", lbl, g.selKind == 3)) g.selKind = 3;
    }

    // ---- Machining Setups (WCS/machine) + their operations -------------------------------------
    for (int si = 0; si < (int)setups.size(); ++si) {
        const auto& s = setups[si];
        const bool setupSel = (g.selKind == 4 && g.selSetup == si);
        ImGui::PushID(si);
        FmRowRect rr = fmRowPill(setupSel);
        fmTreeIcon("ref_csys", "W");
        ImGui::SameLine(0, 4.0f * S);
        std::string lbl = s.name + "  —  " + (s.machine.empty() ? "mill" : s.machine);
        const bool open = ImGui::TreeNodeEx("##cam_setup",
            (si == g.selSetup ? ImGuiTreeNodeFlags_DefaultOpen : 0) |
            ImGuiTreeNodeFlags_OpenOnArrow | ImGuiTreeNodeFlags_SpanAvailWidth,
            "%s", lbl.c_str());
        if (ImGui::IsItemClicked() && !ImGui::IsItemToggledOpen()) {
            g.selKind = 4; g.selSetup = si; g.selOp = -1;
        }
        if (ImGui::IsItemHovered() && ImGui::IsMouseDoubleClicked(0)) camTreeOpenDetail();
        fmRowPillEnd(rr, setupSel);
        if (open) {
            for (int oi = 0; oi < (int)s.ops.size(); ++oi) {
                const auto& o = s.ops[oi];
                const bool opSel = (g.selKind == 5 && g.selSetup == si && g.selOp == oi);
                std::string ol = (o.name.empty() ? o.type : o.name)
                                 + "  [T" + std::to_string(o.tool_number) + "]";
                if (o.suppressed)      ol += "  — off";
                else if (generated)    ol += (g.genWarnings > 0) ? "  — generated (!)"
                                                                 : "  — generated";
                else                   ol += "  — needs generate";
                ImGui::PushID(oi);
                FmRowRect orr = fmRowPill(opSel);
                fmTreeIcon(("cam_" + o.type).c_str(), "o", 16.0f, o.suppressed ? 0.45f : 1.0f);
                ImGui::SameLine(0, 4.0f * S);
                if (ImGui::Selectable(ol.c_str(), opSel,
                                      ImGuiSelectableFlags_AllowDoubleClick)) {
                    g.selKind = 5; g.selSetup = si; g.selOp = oi; g.loadedOpFor = -1;
                }
                const bool dbl = ImGui::IsItemHovered() && ImGui::IsMouseDoubleClicked(0);
                if (ImGui::BeginPopupContextItem("##cam_op_ctx")) {
                    if (ImGui::MenuItem(o.suppressed ? "Unsuppress" : "Suppress"))
                        pendingOps.push_back(nlohmann::json{
                            {"op", "update_operation"}, {"op_id", o.id},
                            {"operation", {{"suppressed", !o.suppressed}}}});
                    if (ImGui::MenuItem("Delete"))
                        pendingOps.push_back(nlohmann::json{
                            {"op", "remove_operation"}, {"op_id", o.id}});
                    if (ImGui::MenuItem("Open in editor")) camTreeOpenDetail();
                    ImGui::EndPopup();
                }
                fmRowPillEnd(orr, opSel);
                ImGui::PopID();
                if (dbl) camTreeOpenDetail();
            }
            if (s.ops.empty()) {
                FmRowRect err = fmRowPill(false);
                ImGui::TextDisabled("   (no operations — add from the ribbon)");
                fmRowPillEnd(err, false);
            }
            ImGui::TreePop();
        }
        ImGui::PopID();
    }
    if (setups.empty()) {
        FmRowRect err = fmRowPill(false);
        ImGui::TextDisabled("   (no machining setup — Add Setup on the ribbon)");
        fmRowPillEnd(err, false);
    }

    // ---- Tools (job tool library) ----
    fmSectionDivider("Tools");
    const auto& tools = g.mirror.tools();
    for (int ti = 0; ti < (int)tools.size(); ++ti) {
        const auto& t = tools[ti];
        char lbl[128];
        std::snprintf(lbl, sizeof(lbl), "T%d  %s  \xC3\x98%.2f mm",
                      t.number, (t.name.empty() ? t.type.c_str() : t.name.c_str()), t.diameter);
        char id[24];
        std::snprintf(id, sizeof(id), "cam_tool_%d", ti);
        if (leafRow(id, "curve_helix", "T", lbl, g.selKind == 6 && g.selTool == ti)) {
            g.selKind = 6; g.selTool = ti;
        }
    }
    if (tools.empty()) {
        FmRowRect err = fmRowPill(false);
        ImGui::TextDisabled("   (no tools — Add Tool on the ribbon)");
        fmRowPillEnd(err, false);
    }

    // ---- Models (the part docs the toolpaths are computed from) ----
    fmSectionDivider("Models");
    if (!g.sourceModel.empty()) {
        if (leafRow("cam_model", "part", "M", g.sourceModel, g.selKind == 7)) g.selKind = 7;
    } else {
        FmRowRect err = fmRowPill(false);
        ImGui::TextDisabled("   (none — Extract Part pulls the active part)");
        fmRowPillEnd(err, false);
    }

    // ---- status footer ----
    if (!g.status.empty()) {
        ImGui::Spacing();
        ImGui::Separator();
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x);
        ImGui::TextDisabled("%s", g.status.c_str());
        ImGui::PopTextWrapPos();
    }

    if (!pendingOps.empty()) applyOps(st, pendingOps);   // after the walk — mirror reload safe
}

}  // namespace ui
