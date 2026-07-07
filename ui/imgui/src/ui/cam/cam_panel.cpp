// CamPanel — the CAM workspace UI (toolbar + tree + backplot + sim + post), driving the
// C++ core's cam_* RPCs. File-static singleton state; body drawn inside the "CAM" dock window
// (application.cpp owns Begin/End — see cam_panel.h).
//
// Ported from host/src/ui/cam/cam_panel.cpp (H1). All cam_* / export_file RPC calls are
// byte-for-byte identical to the host — only the AppState type, include paths, and the
// panel-visibility mechanics (focus-request instead of a floating window) were adapted for
// the new tree's docked-tab convention (see cam_panel.h header comment).
#include "imgui.h"

#include "cam_panel.h"
#include "cam_mirror.h"

#include "app/app_state.h"
#include "theme/domain_tokens.h"
#include "theme/icons.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

namespace ui {

namespace {

struct CamUI {
    CamMirror mirror;
    std::string docId;                 // cam job doc id ("" = no job)
    int  focusFrames = 0;              // >0: keep requesting focus (set by camEnsureVisible/Load/Float)
    bool backplot = true;              // draw toolpaths in the 3D viewport
    int selSetup = 0;
    int selOp = -1;
    nlohmann::json lastExtract;        // last cam_extract result (geometry source)
    std::string partStep;              // temp STEP path of the exported current part
    std::vector<std::string> dialects;
    int postIdx = 0;
    std::string gcode;
    std::string gcodePath;
    nlohmann::json sim;                // last cam_simulate result
    std::string status;                // last action status / error
    // operation form buffers (selected op)
    char opName[64] = "";
    float fStepover = 0, fStepdown = 0, fStockLeave = 0, fFeed = 0, fRpm = 0;
    float hTop = 0, hBottom = -5, hClear = 15, hRetract = 4;
    int loadedOpFor = -1;

    // --- cross-doc dependency tracking (b) ---
    // The part docId this job was extracted from. Set when cam_extract succeeds.
    std::string sourcePartDocId;
    // The part revision at the time of the last cam_extract (geometry snapshot).
    long sourcePartRevision = -1;
    // Whether the part has changed since the last extract (stale flag — pull model).
    bool partStale = false;
    // Whether the stale toast has been shown this stale cycle (suppress re-trigger).
    bool staleToastShown = false;
    // Part revision as of last poll (avoid a worker RPC every single frame — poll
    // every ~60 frames when the Manufacture workspace is visible).
    long lastPolledPartRev = -1;
    int pollCountdown = 0;
};
CamUI g;

// ---- RPC helpers (synchronous; cam_* routes to the C++ core via WorkerClient) ----
bool camCall(app::AppState& st, const char* method, const nlohmann::json& params, nlohmann::json& out) {
    std::string err;
    bool ok = st.worker.call(method, params, out, err);
    if (!ok) {
        g.status = std::string(method) + ": " + (err.empty() ? std::string("failed") : err);
    }
    return ok;
}

void refreshDialects(app::AppState& st) {
    if (!g.dialects.empty()) return;
    nlohmann::json r;
    if (camCall(st, "cam_post_dialects", nlohmann::json::object(), r) && r.contains("dialects"))
        for (auto& d : r["dialects"]) g.dialects.push_back(d.value("id", std::string()));
}

void loadTree(const nlohmann::json& r) { g.mirror.loadTree(r); }

bool ensureJob(app::AppState& st) {
    if (!g.docId.empty()) return true;
    nlohmann::json r;
    if (!camCall(st, "cam_new", {{"name", "CAM Job"}, {"post", "grbl"}}, r)) return false;
    g.docId = r.value("doc_id", std::string());
    loadTree(r);
    return !g.docId.empty();
}

nlohmann::json applyOps(app::AppState& st, const nlohmann::json& ops) {
    nlohmann::json r;
    if (camCall(st, "cam_apply_ops", {{"doc_id", g.docId}, {"base_revision", -1}, {"ops", ops}}, r))
        loadTree(r);
    return r;
}

// Export the active PART tab to a temp STEP (cached), for cam_extract.
// Also records the source part docId and the revision at the time of export.
bool exportPartStep(app::AppState& st) {
    if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) { g.status = "no active part"; return false; }
    const auto& tab = st.docTabs[st.activeTab];
    if (tab.docId.empty()) { g.status = "active tab is not a part"; return false; }
    std::filesystem::path p = std::filesystem::temp_directory_path() / "aicad_cam_part.step";
    nlohmann::json r;
    if (!camCall(st, "export_file", {{"doc_id", tab.docId}, {"path", p.string()}, {"format", "step"}}, r))
        return false;
    g.partStep = p.string();
    // Snapshot: record which part and which revision this geometry came from.
    g.sourcePartDocId = tab.docId;
    // Read current revision from doc_tree (lightweight call, returns {tree,env,revision}).
    nlohmann::json tr;
    if (camCall(st, "doc_tree", {{"doc_id", tab.docId}}, tr) && tr.contains("revision"))
        g.sourcePartRevision = tr["revision"].get<long>();
    else
        g.sourcePartRevision = -1;
    g.partStale = false;
    g.staleToastShown = false;
    g.lastPolledPartRev = g.sourcePartRevision;
    // Register the cross-doc dependency: CAM job depends on this part at this revision.
    // This persists the link in core/doc's dep registry so doc_dep_notify fires whenever
    // the part rebuilds (feat(xdoc) in-process stale notification path).
    if (!g.docId.empty() && g.sourcePartRevision >= 0) {
        nlohmann::json dr;
        camCall(st, "doc_dep_register", {
            {"source_doc_id",   g.sourcePartDocId},
            {"dep_doc_id",      g.docId},
            {"kind",            "cam"},
            {"linked_revision", g.sourcePartRevision},
        }, dr);
    }
    return true;
}

// cam_extract on the current part (exports STEP first). kind: all|stock|holes|section|mesh|profile.
nlohmann::json extract(app::AppState& st, const std::string& kind, double z = 0, bool useZ = false) {
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
void addOperation(app::AppState& st, const std::string& type, int toolNumber) {
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

void doGenerate(app::AppState& st) {
    if (g.docId.empty()) { g.status = "no job"; return; }
    nlohmann::json r;
    if (camCall(st, "cam_generate", {{"doc_id", g.docId}}, r)) {
        g.mirror.loadToolpaths(r);
        int n = r.contains("toolpaths") ? (int)r["toolpaths"].size() : 0;
        g.status = "generated " + std::to_string(n) + " toolpath(s)";
        if (r.contains("warnings") && r["warnings"].is_array() && !r["warnings"].empty())
            g.status += " (" + std::to_string(r["warnings"].size()) + " warning)";
    }
}

void doSimulate(app::AppState& st) {
    if (g.docId.empty()) return;
    nlohmann::json r;
    if (camCall(st, "cam_simulate", {{"doc_id", g.docId}}, r)) {
        g.sim = r;
        g.status = "simulated";
    }
}

void doPost(app::AppState& st) {
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
void drawBackplot(app::AppState& /*st*/) {
    const CamBackplot& bp = g.mirror.backplot();
    ImVec2 avail = ImGui::GetContentRegionAvail();
    if (avail.y < 80) avail.y = 200;
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->AddRectFilled(p0, ImVec2(p0.x + avail.x, p0.y + avail.y), theme::camview::backdrop());
    if (bp.empty()) {
        dl->AddText(ImVec2(p0.x + 10, p0.y + 10), theme::camview::emptyMsg(),
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
    line(bp.rapid,  theme::camview::rapid(), 1.0f);   // rapid = faint red
    line(bp.lead,   theme::camview::lead(), 1.5f);  // lead = green
    line(bp.plunge, theme::camview::plunge(), 1.5f);  // plunge = cyan
    line(bp.cut,    theme::camview::cut(), 1.6f);  // cut = blue
    line(bp.arc,    theme::camview::arc(), 1.6f); // arc = violet
    char info[96];
    std::snprintf(info, sizeof(info), "XY %.1f x %.1f mm   Z %.1f..%.1f", w, h, bp.minz, bp.maxz);
    dl->AddText(ImVec2(p0.x + 8, p0.y + 8), theme::camview::dimText(), info);
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
        if (gouges > 0) { ImGui::PushStyleColor(ImGuiCol_Text, theme::camview::simWarn());
            ImGui::Text("GOUGES: %d (max %.3f mm)", gouges, r.value("max_gouge_mm", 0.0)); ImGui::PopStyleColor(); }
        if (rapids > 0) { ImGui::PushStyleColor(ImGuiCol_Text, theme::camview::simWarn());
            ImGui::Text("RAPID THROUGH STOCK: %d", rapids); ImGui::PopStyleColor(); }
        if (gouges == 0 && rapids == 0) { ImGui::PushStyleColor(ImGuiCol_Text, theme::camview::simOk());
            ImGui::TextUnformatted("No gouges or rapid collisions."); ImGui::PopStyleColor(); }
    }
}

// ---- op-parameter editor form -----------------------------------------------
// Reads back the selected op's parameters from the mirror and lets the user edit
// them. On "Apply" rebuilds the op via applyOps with an update_operation op.
static void drawOpEditorImpl(app::AppState& st) {
    if (g.mirror.setups().empty() || g.selSetup < 0 || g.selSetup >= (int)g.mirror.setups().size()) {
        ImGui::TextDisabled("No setup selected.");
        return;
    }
    const auto& setup = g.mirror.setups()[g.selSetup];
    if (g.selOp < 0 || g.selOp >= (int)setup.ops.size()) {
        ImGui::TextDisabled("Select an operation in the tree to edit its parameters.");
        return;
    }
    const auto& op = setup.ops[g.selOp];

    // Seed form buffers once when the selected op changes.
    if (g.loadedOpFor != g.selOp) {
        g.loadedOpFor = g.selOp;
        std::snprintf(g.opName, sizeof(g.opName), "%s", op.name.empty() ? op.type.c_str() : op.name.c_str());
        // Conservative defaults — proper values come from the live op JSON via cam_tree
        // (which is already in the mirror). For now seed from struct defaults; a future
        // pass can deep-copy the op JSON into the form when the mirror exposes it.
        g.fStepover    = 3.0f;
        g.fStepdown    = 2.0f;
        g.fStockLeave  = 0.0f;
        g.fFeed        = 600.0f;
        g.fRpm         = 8000.0f;
        g.hTop         = 0.0f;
        g.hBottom      = -5.0f;
        g.hClear       = 15.0f;
        g.hRetract     = 4.0f;
    }

    ImGui::Text("Operation: %s  [T%d]", op.type.c_str(), op.tool_number);
    ImGui::Separator();

    ImGui::SetNextItemWidth(180);
    ImGui::InputText("Name", g.opName, sizeof(g.opName));

    bool changed = false;
    ImGui::Text("Cut Parameters");
    ImGui::SetNextItemWidth(120); changed |= ImGui::InputFloat("Stepover (mm)", &g.fStepover, 0.1f, 0.5f, "%.2f");
    ImGui::SetNextItemWidth(120); changed |= ImGui::InputFloat("Stepdown (mm)", &g.fStepdown, 0.1f, 0.5f, "%.2f");
    ImGui::SetNextItemWidth(120); changed |= ImGui::InputFloat("Stock Leave (mm)", &g.fStockLeave, 0.05f, 0.2f, "%.3f");
    ImGui::Spacing();
    ImGui::Text("Feeds & Speeds");
    ImGui::SetNextItemWidth(120); changed |= ImGui::InputFloat("Feed (mm/min)", &g.fFeed, 10.f, 50.f, "%.0f");
    ImGui::SetNextItemWidth(120); changed |= ImGui::InputFloat("RPM", &g.fRpm, 100.f, 500.f, "%.0f");
    ImGui::Spacing();
    ImGui::Text("Heights (mm, WCS Z)");
    ImGui::SetNextItemWidth(120); changed |= ImGui::InputFloat("Top", &g.hTop, 0.1f, 1.f, "%.2f");
    ImGui::SetNextItemWidth(120); changed |= ImGui::InputFloat("Bottom", &g.hBottom, 0.1f, 1.f, "%.2f");
    ImGui::SetNextItemWidth(120); changed |= ImGui::InputFloat("Clearance", &g.hClear, 0.5f, 2.f, "%.2f");
    ImGui::SetNextItemWidth(120); changed |= ImGui::InputFloat("Retract", &g.hRetract, 0.1f, 0.5f, "%.2f");

    ImGui::Spacing();
    if (ImGui::Button("Apply") && !g.docId.empty()) {
        nlohmann::json params = {
            {"stepover",    (double)g.fStepover},
            {"stepdown",    (double)g.fStepdown},
            {"stock_leave", (double)g.fStockLeave},
            {"feed",        (double)g.fFeed},
            {"spindle_rpm", (double)g.fRpm},
        };
        nlohmann::json heights = {
            {"top",      (double)g.hTop},
            {"bottom",   (double)g.hBottom},
            {"clearance",(double)g.hClear},
            {"retract",  (double)g.hRetract},
        };
        nlohmann::json upd = {
            {"op", "update_operation"},
            {"setup_id",     setup.id},
            {"operation_id", op.id},
            {"params",       params},
            {"heights",      heights},
        };
        if (std::string(g.opName) != (op.name.empty() ? op.type : op.name))
            upd["name"] = std::string(g.opName);
        applyOps(st, nlohmann::json::array({upd}));
        g.status = "updated operation parameters";
    }
    (void)changed;  // used only to indicate form is dirty — future: dirty-flag indicator
}

// ---- setup-sheet summary tab ------------------------------------------------
static void drawSetupSheetImpl(app::AppState& st) {
    if (g.mirror.setups().empty()) {
        ImGui::TextDisabled("No setups. Add a setup and run Generate.");
        return;
    }

    ImGui::Text("Job: %s", g.mirror.name().c_str());
    ImGui::Separator();

    const auto& setups = g.mirror.setups();
    for (int si = 0; si < (int)setups.size(); ++si) {
        const auto& s = setups[si];
        ImGui::PushID(si);
        if (ImGui::CollapsingHeader(s.name.c_str(), ImGuiTreeNodeFlags_DefaultOpen)) {
            ImGui::Text("Machine: %s", s.machine.c_str());
            // Stock dimensions from last extract (if available)
            if (!g.lastExtract.empty() && g.lastExtract.contains("bbox")) {
                const auto& bbox = g.lastExtract["bbox"];
                if (bbox.contains("min") && bbox.contains("max")) {
                    const auto& mn = bbox["min"];
                    const auto& mx = bbox["max"];
                    if (mn.is_array() && mx.is_array() && mn.size() >= 3 && mx.size() >= 3) {
                        double dx = mx[0].get<double>() - mn[0].get<double>();
                        double dy = mx[1].get<double>() - mn[1].get<double>();
                        double dz = mx[2].get<double>() - mn[2].get<double>();
                        ImGui::Text("Stock: %.1f x %.1f x %.1f mm", dx, dy, dz);
                    }
                }
            } else {
                ImGui::TextDisabled("Stock: run Extract Part");
            }
            // Operations count
            ImGui::Text("Operations: %d", (int)s.ops.size());
            // Sim data (cycle time) per setup if available
            if (!g.sim.empty() && g.sim.contains("results") && si < (int)g.sim["results"].size()) {
                const auto& r = g.sim["results"][si];
                double t = r.value("cycle_time_s", 0.0);
                ImGui::Text("Cycle time: %dm %02ds", (int)(t / 60), (int)t % 60);
            }
            // G-code output path
            if (!g.gcodePath.empty()) {
                ImGui::TextDisabled("NC: %s", g.gcodePath.c_str());
                if (ImGui::Button("Open in explorer")) {
                    std::string cmd = "xdg-open \"" + std::filesystem::path(g.gcodePath).parent_path().string() + "\"";
                    (void)std::system(cmd.c_str());
                }
            } else {
                ImGui::TextDisabled("NC: not yet posted");
            }
        }
        ImGui::PopID();
    }

    ImGui::Separator();
    ImGui::Text("Tools:");
    for (const auto& t : g.mirror.tools())
        ImGui::BulletText("T%d  %s  \xC3\x98%.2f mm  (%d fl)", t.number, t.type.c_str(),
                          t.diameter, 0);
}

}  // namespace

// ---- ribbon command handler ----
void camHandleCommand(app::AppState& st, const std::string& cmd) {
    st.camActive = true;   // surface CAM panel via viewport content-swap (H1 fix)
    camEnsureVisible();
    refreshDialects(st);
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
    if (!g.backplot || g.mirror.backplot().empty()) return nullptr;
    return &g.mirror.backplot();
}

// Focus-request seam (see cam_panel.h): the central dock node hides its tab bar, so bringing
// the CAM tab forward needs a few consecutive SetNextWindowFocus() calls from application.cpp,
// which polls camTakeFocusRequest() each frame before Begin("CAM"...).
void camEnsureVisible() { g.focusFrames = 10; }

// Shot-only hook: request sustained focus so a headless AI_CAD_SHOT=cam capture can actually
// show the populated workspace. Never called in normal use.
void camFloatForShot() { g.focusFrames = 10; }

bool camTakeFocusRequest() {
    // Shot-env seam: application.cpp polls this every frame BEFORE Begin("CAM"...), even while
    // the tab is still hidden behind Viewport — unlike drawCamManager (which only runs once the
    // tab is already forward), so this is the one place a one-time env check can actually bring
    // the CAM tab forward for a headless AI_CAD_SHOT=cam capture. Mirrors pmApplyShotEnv's
    // shot-only self-trigger (left_dock.cpp), adapted for a tab-bar-hidden dock window.
    static bool shotChecked = false;
    if (!shotChecked) {
        shotChecked = true;
        const char* shot = std::getenv("AI_CAD_SHOT");
        if (shot && std::string(shot) == "cam") camFloatForShot();
    }
    if (g.focusFrames > 0) { g.focusFrames--; return true; }
    return false;
}

// Adopt an existing CAM job doc (built core-side by the copilot's CAM fast-path) into the
// panel so it shows up populated instead of empty. Loads the job tree (setups/ops/tools).
void camLoadJob(app::AppState& st, const std::string& docId) {
    if (docId.empty()) return;
    st.camActive = true;   // surface CAM panel via viewport content-swap (H1 fix)
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
    if (camCall(st, "cam_generate", {{"doc_id", docId}}, gp)) g.mirror.loadToolpaths(gp);
    camEnsureVisible();
}

// ---- section-switcher helpers (c) -------------------------------------------

bool camHasJobForPart(const std::string& /*partDocId*/) {
    // Currently always returns true for any Part tab — a CAM job can always be
    // created on demand. A future enhancement could query the dep registry to check
    // whether a job was already created, and show a filled vs. empty icon.
    return true;
}

void camActivateForPart(app::AppState& st, const std::string& partDocId) {
    // Resolve (find existing or create new) the CAM job for this part, then switch
    // workspace to Manufacture so the Manufacture overlay becomes active.
    camFindOrCreateJobForPart(st, partDocId);
    st.workspace = cmd::Workspace::Manufacture;
}

// ---- workspace header strip (drawn above drawCamManager by workspace_manufacture.cpp) ----
void drawCamWorkspaceHeader(app::AppState& st) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    const float S = ImGui::GetIO().DisplaySize.y > 0 ? 1.f : 1.f;  // placeholder; theme::S() used in panels
    const float hdr = 28.f * S;
    const float avail = ImGui::GetContentRegionAvail().x;
    // Glass header bar background
    dl->AddRectFilled(p0, ImVec2(p0.x + avail, p0.y + hdr),
                      IM_COL32(40, 40, 44, 180), 4.f);
    ImGui::Dummy(ImVec2(avail, hdr));

    ImGui::SameLine(0, 0);
    ImGui::SetCursorScreenPos(ImVec2(p0.x + 8, p0.y + (hdr - ImGui::GetTextLineHeight()) * 0.5f));
    // Breadcrumb: Job > Setup > Op
    const std::string jobName  = g.mirror.name().empty() ? "No Job" : g.mirror.name();
    const std::string setupName = (!g.mirror.setups().empty() && g.selSetup >= 0 && g.selSetup < (int)g.mirror.setups().size())
                                  ? g.mirror.setups()[g.selSetup].name : "";
    const std::string opName = (!setupName.empty() && g.selOp >= 0 && g.selOp < (int)g.mirror.setups()[g.selSetup].ops.size())
                                ? g.mirror.setups()[g.selSetup].ops[g.selOp].type : "";

    ImGui::TextDisabled("Manufacture");
    if (!jobName.empty()) { ImGui::SameLine(); ImGui::TextDisabled(">"); ImGui::SameLine(); ImGui::Text("%s", jobName.c_str()); }
    if (!setupName.empty()) { ImGui::SameLine(); ImGui::TextDisabled(">"); ImGui::SameLine(); ImGui::TextDisabled("%s", setupName.c_str()); }
    if (!opName.empty()) { ImGui::SameLine(); ImGui::TextDisabled(">"); ImGui::SameLine(); ImGui::TextDisabled("%s", opName.c_str()); }

    // Right-aligned shortcut buttons
    const float btnW = 90.f * S;
    const float gap  = 4.f * S;
    float rx = p0.x + avail - 3 * (btnW + gap) - gap;
    ImGui::SetCursorScreenPos(ImVec2(rx, p0.y + (hdr - 20.f) * 0.5f));
    if (ImGui::Button("Generate", ImVec2(btnW, 0))) doGenerate(st);
    ImGui::SetCursorScreenPos(ImVec2(rx + btnW + gap, p0.y + (hdr - 20.f) * 0.5f));
    ImGui::SameLine();
    if (ImGui::Button("Simulate", ImVec2(btnW, 0))) doSimulate(st);
    ImGui::SetCursorScreenPos(ImVec2(rx + 2 * (btnW + gap), p0.y + (hdr - 20.f) * 0.5f));
    ImGui::SameLine();
    if (ImGui::Button("Post", ImVec2(btnW, 0))) doPost(st);

    // Stale banner (b): part changed since last extract
    if (g.partStale) {
        ImGui::PushStyleColor(ImGuiCol_Text, IM_COL32(255, 200, 60, 255));
        ImGui::SameLine(0, 16.f);
        ImGui::TextUnformatted("Part changed.");
        ImGui::PopStyleColor();
        ImGui::SameLine();
        if (ImGui::SmallButton("Regen from Part")) camRegenFromPart(st);
    }
}

// ---- public wrappers for the sub-draws used by workspace_manufacture ----
void drawCamOpEditor(app::AppState& st)     { drawOpEditorImpl(st); }
void drawCamSetupSheet(app::AppState& st)   { drawSetupSheetImpl(st); }

// ---- cross-doc stale check (b) ----------------------------------------------
bool camCheckPartStale(app::AppState& st) {
    // Only meaningful when we have a job linked to a source part.
    if (g.sourcePartDocId.empty() || g.docId.empty()) return false;

    // Throttle: check roughly every 60 frames (~1 s at 60 fps).
    if (--g.pollCountdown > 0) return g.partStale;
    g.pollCountdown = 60;

    // Fast path: use doc_dep_query which reads the stale flag set by dep_notify_unlocked
    // inside warm_and_respond on every part rebuild. Sub-frame latency for in-process use.
    nlohmann::json dq;
    if (camCall(st, "doc_dep_query", {{"source_doc_id", g.sourcePartDocId}, {"kind", "cam"}}, dq)
        && dq.contains("dependents") && dq["dependents"].is_array()) {
        for (const auto& d : dq["dependents"]) {
            if (d.value("dep_doc_id", std::string()) == g.docId) {
                bool stale = d.value("stale", false);
                if (stale && !g.partStale) {
                    g.partStale = true;
                    g.staleToastShown = false;
                }
                long srcRev = dq.value("source_revision", (long)-1);
                if (srcRev >= 0) g.lastPolledPartRev = srcRev;
                return g.partStale;
            }
        }
    }

    // Fallback: poll doc_list_open when the dep registry entry is absent (e.g. the
    // job was loaded via camLoadJob without a fresh extract).
    nlohmann::json r;
    if (!camCall(st, "doc_list_open", nlohmann::json::object(), r)) return g.partStale;
    if (!r.contains("docs") || !r["docs"].is_array()) return g.partStale;
    for (const auto& d : r["docs"]) {
        if (d.value("doc_id", std::string()) == g.sourcePartDocId) {
            long rev = d.value("revision", (long)-1);
            if (rev > g.sourcePartRevision && g.sourcePartRevision >= 0) {
                if (!g.partStale) {
                    g.partStale = true;
                    g.staleToastShown = false;
                }
            }
            g.lastPolledPartRev = rev;
            break;
        }
    }
    return g.partStale;
}

void camRegenFromPart(app::AppState& st) {
    // Re-export the part step then re-extract stock + re-generate toolpaths.
    g.partStep.clear();  // force re-export
    if (!exportPartStep(st)) return;
    g.lastExtract = extract(st, "all");
    if (!ensureJob(st)) return;
    // Seed a setup if none exists (fresh re-link)
    if (g.mirror.setups().empty() && g.lastExtract.contains("stock")) {
        nlohmann::json box = g.lastExtract["stock"]["box"];
        box["type"] = "box"; box["off_ztop"] = 1.0; box["material"] = "aluminum";
        applyOps(st, nlohmann::json::array({nlohmann::json{
            {"op", "add_setup"},
            {"setup", {{"name", "Setup1"}, {"machine", {{"kind", "mill3"}}}, {"stock", box}}}}}));
    }
    doGenerate(st);
    g.partStale = false;
    g.staleToastShown = true;
    g.status = "re-generated from updated part (rev " + std::to_string(g.sourcePartRevision) + ")";
}

// ---- part↔CAM resolver (c) --------------------------------------------------
std::string camFindOrCreateJobForPart(app::AppState& st, const std::string& partDocId) {
    if (partDocId.empty()) return {};

    // Case 1: the currently loaded job was extracted from this part — reuse it.
    if (!g.docId.empty() && g.sourcePartDocId == partDocId) {
        st.camActive = true;
        camEnsureVisible();
        return g.docId;
    }

    // Case 2: create a new CAM job, extract geometry from the part, add a default setup.
    // We need the active tab to match partDocId so exportPartStep picks the right tab.
    // Find the tab index for partDocId.
    int targetTab = -1;
    for (int i = 0; i < (int)st.docTabs.size(); ++i) {
        if (st.docTabs[i].docId == partDocId) { targetTab = i; break; }
    }
    const int prevTab = st.activeTab;
    if (targetTab >= 0) st.activeTab = targetTab;

    g.docId.clear();
    g.mirror.clear();
    g.partStep.clear();
    g.lastExtract = nlohmann::json();

    if (!ensureJob(st)) {
        st.activeTab = prevTab;
        return {};
    }
    g.sourcePartDocId = partDocId;

    // Extract geometry from the part
    nlohmann::json extr = extract(st, "all");
    if (!extr.empty()) {
        g.lastExtract = extr;
        // Add a default setup seeded from stock bounding box
        if (g.mirror.setups().empty() && extr.contains("stock")) {
            nlohmann::json box = extr["stock"]["box"];
            box["type"] = "box"; box["off_ztop"] = 1.0; box["material"] = "aluminum";
            applyOps(st, nlohmann::json::array({nlohmann::json{
                {"op", "add_setup"},
                {"setup", {{"name", "Setup1"}, {"machine", {{"kind", "mill3"}}}, {"stock", box}}}}}));
        }
    }
    // Read the part revision snapshot
    nlohmann::json tr;
    if (camCall(st, "doc_tree", {{"doc_id", partDocId}}, tr) && tr.contains("revision"))
        g.sourcePartRevision = tr["revision"].get<long>();
    g.partStale = false;
    g.lastPolledPartRev = g.sourcePartRevision;

    st.activeTab = prevTab;
    st.camActive = true;
    camEnsureVisible();
    g.status = "CAM job created for part";
    return g.docId;
}

// ---- the panel body (Begin("CAM")/End() live in application.cpp; see cam_panel.h) ----
void drawCamManager(app::AppState& st) {
    refreshDialects(st);

    // toolbar
    if (ImGui::Button("New Job")) camHandleCommand(st, "cam_new_job");
    ImGui::SameLine(); if (ImGui::Button("Extract Part")) camHandleCommand(st, "cam_extract");
    ImGui::SameLine(); if (ImGui::Button("Add Setup")) camHandleCommand(st, "cam_add_setup");
    ImGui::SameLine(); if (ImGui::Button("Add Tool")) camHandleCommand(st, "cam_add_tool");
    ImGui::Separator();
    // operation buttons
    // Each op button carries its CAM toolpath tile ("cam_"+atlas name); the tile is a multi-color
    // raster (opted out of SDF via atlas.json "raster_only"), drawn in the label's leading pad.
    auto opBtn = [&](const char* label, const char* cmd, const char* icon) {
        const ImVec2 p = ImGui::GetCursorScreenPos();
        const float lh = ImGui::GetTextLineHeight();
        const bool clicked = ImGui::Button(label);
        icons::draw(ImGui::GetWindowDrawList(), icon,
                    ImVec2(p.x + ImGui::GetStyle().FramePadding.x + lh * 0.5f, p.y + ImGui::GetFrameHeight() * 0.5f),
                    lh * 1.05f, IM_COL32(255, 255, 255, 255));
        if (clicked) camHandleCommand(st, cmd);
        ImGui::SameLine();
    };
    opBtn("   Face", "cam_op_face", "cam_face"); opBtn("   Contour", "cam_op_contour", "cam_contour2d"); opBtn("   Pocket", "cam_op_pocket", "cam_pocket2d");
    opBtn("   Adaptive", "cam_op_adaptive", "cam_adaptive2d"); opBtn("   Drill", "cam_op_drill", "cam_drill"); opBtn("   3D Parallel", "cam_op_parallel3d", "cam_parallel3d");
    opBtn("   Waterline", "cam_op_waterline3d", "cam_waterline3d"); opBtn("   Turn", "cam_op_turn_rough", "cam_turn_rough"); opBtn("   Laser", "cam_op_laser", "cam_cut2d");
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
    if (setups.empty()) ImGui::TextDisabled("No setups yet. Add Setup, then add operations.");
    ImGui::Separator();
    ImGui::TextDisabled("Tools");
    for (const auto& t : g.mirror.tools())
        ImGui::BulletText("T%d %s  \xC3\x98%.2f", t.number, t.type.c_str(), t.diameter);
    ImGui::EndChild();

    // ---- RIGHT: tabs ----
    ImGui::NextColumn();
    if (ImGui::BeginTabBar("cam_tabs")) {
        if (ImGui::BeginTabItem("Toolpaths")) { drawBackplot(st); ImGui::EndTabItem(); }
        if (ImGui::BeginTabItem("Op Editor")) {
            drawOpEditorImpl(st);
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Simulate")) {
            if (ImGui::Button("Run Material-Removal Sim")) doSimulate(st);
            drawSimSummary();
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Setup Sheet")) {
            drawSetupSheetImpl(st);
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
}

}  // namespace ui
