// T-U3 CAM workspace panel — job tree (setups/ops), tool table, sim-results strip,
// post-processor + setup-sheet actions. Built on panel_scaffold.h + theme:: tokens.
// View-model: ui/abstract/cam_view.h (no ImGui includes there).
// RPC seam: app::AppState::worker (WorkerClient -> C++ cam_* core).
// Style conformance: no literal colors; theme::/TCOL()/TVEC() only.
//
// M1/M2/M4/M5/M6 backend fields surfaced:
//   arc_blend_linking (Operation, default true) — shown per op in detail pane
//   engagement warning text in cam_generate warnings[] — displayed per op
//   holder_collisions (-1 = not checked, T-M3 unmerged) — degrade gracefully

#include "cam_panel.h"
#include "cam_view.h"         // ui/abstract — pure view-model, no ImGui
#include "panel_scaffold.h"
#include "theme/theme.h"

#include "imgui.h"

#include "app/app_state.h"
#include "worker_client.h"
#include "theme/icons.h"

#include <nlohmann/json.hpp>

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

namespace ui {

namespace {

// ---- singleton state --------------------------------------------------------

bool gOpen = false;
aicad::ui::CamViewModel gModel;
ui::PanelScaffoldState  gScaffoldState;
int gFocusFrames = 0;

// Deferred action flags — set in sub-draw functions, drained at end of frame.
bool gWantSimulate   = false;
bool gWantPost       = false;
bool gWantSetupSheet = false;

// ---- RPC helpers ------------------------------------------------------------

using nlohmann::json;

static bool camCall(app::AppState& s, const char* method, const json& params, json& out) {
    std::string err;
    bool ok = s.worker.call(method, params, out, err);
    if (!ok) {
        gModel.status          = std::string(method) + ": " + (err.empty() ? "failed" : err);
        gModel.status_is_error = true;
    }
    return ok;
}

static void refreshDialects(app::AppState& s) {
    if (!gModel.post.dialects.empty()) return;
    json r;
    if (camCall(s, "cam_post_dialects", json::object(), r) && r.contains("dialects"))
        for (auto& d : r["dialects"]) gModel.post.dialects.push_back(d.value("id", std::string()));
}

// Parse cam_tree / cam_apply_ops response ({revision, tree:{name,post,tools,setups:[…]}}).
static void loadTree(const json& resp) {
    if (!resp.contains("tree")) return;
    const auto& t = resp["tree"];
    gModel.job_name = t.value("name", std::string());
    gModel.job_post = t.value("post", std::string());
    gModel.revision = resp.value("revision", -1);

    gModel.setups.clear();
    if (t.contains("setups")) {
        for (const auto& su : t["setups"]) {
            aicad::ui::CamSetupView sv;
            sv.id      = su.value("id",      std::string());
            sv.name    = su.value("name",    std::string());
            sv.machine = su.value("machine", std::string());
            if (su.contains("operations")) {
                for (const auto& op : su["operations"]) {
                    aicad::ui::CamOpView ov;
                    ov.id                = op.value("id",                std::string());
                    ov.name              = op.value("name",              std::string());
                    ov.type              = op.value("type",              std::string());
                    ov.tool_number       = op.value("tool_number",       0);
                    ov.suppressed        = op.value("suppressed",        false);
                    ov.arc_blend_linking = op.value("arc_blend_linking", true);
                    sv.ops.push_back(std::move(ov));
                }
            }
            gModel.setups.push_back(std::move(sv));
        }
    }

    gModel.tools.clear();
    if (t.contains("tools")) {
        for (const auto& tl : t["tools"]) {
            aicad::ui::CamToolView tv;
            tv.number   = tl.value("number",   0);
            tv.name     = tl.value("name",     std::string());
            tv.type     = tl.value("type",     std::string());
            tv.diameter = tl.value("diameter", 0.0);
            gModel.tools.push_back(std::move(tv));
        }
    }

    if (gModel.job_state == aicad::ui::CamJobState::Empty)
        gModel.job_state = aicad::ui::CamJobState::Loaded;
}

// Parse cam_generate warnings[] (array of {op, warning|error} objects).
static void loadGenWarnings(const json& resp) {
    gModel.gen_warnings.clear();
    if (!resp.contains("warnings")) return;
    for (const auto& w : resp["warnings"]) {
        aicad::ui::CamGenWarning gw;
        gw.op_id    = w.value("op",      std::string());
        gw.is_error = w.contains("error");
        gw.text     = w.value(gw.is_error ? "error" : "warning", std::string());
        if (!gw.text.empty()) gModel.gen_warnings.push_back(std::move(gw));
    }
}

static json applyOps(app::AppState& s, const json& ops) {
    json r;
    if (camCall(s, "cam_apply_ops",
                {{"doc_id", gModel.job_doc_id}, {"base_revision", -1}, {"ops", ops}}, r))
        loadTree(r);
    return r;
}

static bool ensureJob(app::AppState& s) {
    if (!gModel.job_doc_id.empty()) return true;
    json r;
    if (!camCall(s, "cam_new", {{"name", "CAM Job"}, {"post", "grbl"}}, r)) return false;
    gModel.job_doc_id      = r.value("doc_id", std::string());
    gModel.status          = "new CAM job";
    gModel.status_is_error = false;
    loadTree(r);
    return !gModel.job_doc_id.empty();
}

static const char* setupId() {
    if (gModel.sel_setup >= 0 && gModel.sel_setup < (int)gModel.setups.size())
        return gModel.setups[gModel.sel_setup].id.c_str();
    return "";
}

static const aicad::ui::CamOpView* selectedOp() {
    if (gModel.sel_setup < 0 || gModel.sel_setup >= (int)gModel.setups.size()) return nullptr;
    const auto& su = gModel.setups[gModel.sel_setup];
    if (gModel.sel_op < 0 || gModel.sel_op >= (int)su.ops.size()) return nullptr;
    return &su.ops[gModel.sel_op];
}

static void addOp(app::AppState& s, const char* type) {
    if (!ensureJob(s) || gModel.setups.empty()) {
        if (gModel.setups.empty()) { gModel.status = "add a setup first"; gModel.status_is_error = true; }
        return;
    }
    int tool = gModel.tools.empty() ? 1 : gModel.tools.front().number;
    applyOps(s, json::array({json{
        {"op", "add_operation"}, {"setup_id", setupId()},
        {"operation", {{"type", type}, {"tool_number", tool},
                       {"heights", {{"top",0},{"bottom",-5},{"clearance",15},{"retract",5}}}}}
    }}));
    gModel.status          = std::string("added ") + type;
    gModel.status_is_error = false;
    if (!gModel.setups.empty())
        gModel.sel_op = (int)gModel.setups[gModel.sel_setup].ops.size() - 1;
}

static void doGenerate(app::AppState& s) {
    if (gModel.job_doc_id.empty()) return;
    json r;
    if (camCall(s, "cam_generate", {{"doc_id", gModel.job_doc_id}}, r)) {
        loadGenWarnings(r);
        int n = r.contains("toolpaths") ? (int)r["toolpaths"].size() : 0;
        gModel.job_state       = aicad::ui::CamJobState::Generated;
        gModel.status          = "generated " + std::to_string(n) + " toolpath(s)";
        gModel.status_is_error = false;
        if (!gModel.gen_warnings.empty())
            gModel.status += " (" + std::to_string(gModel.gen_warnings.size()) + " warning(s))";
    }
}

static void doSimulate(app::AppState& s) {
    if (gModel.job_doc_id.empty()) return;
    auto& sim = gModel.sim;
    sim = aicad::ui::CamSimView{};
    sim.state = aicad::ui::CamSimState::Running;
    json r;
    if (camCall(s, "cam_simulate", {{"doc_id", gModel.job_doc_id}}, r)) {
        sim.state = aicad::ui::CamSimState::Ok;
        // cam_simulate returns {results:[{...}]} (one per setup)
        const json* resPtr = nullptr;
        if (r.contains("results") && r["results"].is_array() && !r["results"].empty())
            resPtr = &r["results"][0];
        else if (r.contains("ok"))  // flat result (single-setup path)
            resPtr = &r;
        if (resPtr) {
            const auto& res = *resPtr;
            sim.stock_volume_mm3     = res.value("stock_volume_mm3",    0.0);
            sim.removed_volume_mm3   = res.value("removed_volume_mm3",  0.0);
            sim.remaining_volume_mm3 = res.value("remaining_volume_mm3",0.0);
            sim.machined_fraction    = res.value("machined_fraction",   0.0);
            sim.cycle_time_s         = res.value("cycle_time_s",        0.0);
            sim.cut_time_s           = res.value("cut_time_s",          0.0);
            sim.rapid_time_s         = res.value("rapid_time_s",        0.0);
            sim.gouges               = res.value("gouges",              0);
            sim.max_gouge_mm         = res.value("max_gouge_mm",        0.0);
            sim.rapid_collisions     = res.value("rapid_collisions",    0);
            // holder_collisions: absent when T-M3 not merged (check_holder=false default).
            // -1 means "not checked"; 0 = checked + clean; >0 = collisions. Degrade gracefully.
            sim.holder_collisions    = res.value("holder_collisions",  -1);
            if (res.contains("warnings"))
                for (auto& w : res["warnings"]) sim.warnings.push_back(w.get<std::string>());
        }
        gModel.job_state       = aicad::ui::CamJobState::Simulated;
        gModel.status          = "simulate complete";
        gModel.status_is_error = false;
    } else {
        sim.state      = aicad::ui::CamSimState::Error;
        sim.error_text = gModel.status;
    }
}

static void doPost(app::AppState& s) {
    if (gModel.job_doc_id.empty()) return;
    auto& p = gModel.post;
    std::string dialect = (!p.dialects.empty() && p.selected_idx < (int)p.dialects.size())
                          ? p.dialects[p.selected_idx] : "grbl";
    json r;
    if (camCall(s, "cam_post", {{"doc_id", gModel.job_doc_id}, {"dialect", dialect}}, r)) {
        p.gcode      = r.value("gcode", std::string());
        p.line_count = r.value("line_count", 0);
        std::filesystem::path np = std::filesystem::temp_directory_path()
                                   / ("aicad_cam." + dialect + ".nc");
        std::ofstream f(np);
        if (f) { f << p.gcode; p.gcode_path = np.string(); }
        gModel.status          = "posted " + dialect + " (" + std::to_string(p.line_count) + " lines)";
        gModel.status_is_error = false;
    }
}

static void doSetupSheet(app::AppState& s) {
    if (gModel.job_doc_id.empty()) return;
    json r;
    if (camCall(s, "cam_setup_sheet", {{"doc_id", gModel.job_doc_id}}, r)) {
        std::string path = r.value("path", std::string());
        if (path.empty()) path = r.value("html_path", std::string());
        gModel.post.setup_sheet_path = path;
        gModel.status          = "setup sheet: " + (path.empty() ? "(no path)" : path);
        gModel.status_is_error = false;
    }
}

// ============================================================================
// Sub-draw functions (no AppState& needed; deferred flags for actions)
// ============================================================================

static void drawJobTree() {
    const float S = theme::S();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    char buf[128];

    // Job name + revision
    std::snprintf(buf, sizeof(buf), "%s  r%d",
                  gModel.job_name.empty() ? "(unnamed)" : gModel.job_name.c_str(),
                  gModel.revision);
    ImGui::PushStyleColor(ImGuiCol_Text, TCOL(txt2));
    ImGui::TextUnformatted(buf);
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, 3.f * S));

    if (gModel.job_doc_id.empty()) {
        drawPanelEmptyState("No CAM job", "Press 'New Job' to start");
        return;
    }
    if (gModel.setups.empty()) {
        drawPanelEmptyState("No setups yet", "Press 'Add Setup'");
    }

    for (int si = 0; si < (int)gModel.setups.size(); ++si) {
        const auto& su = gModel.setups[si];
        ImGui::PushID(si);

        ImGuiTreeNodeFlags nf = ImGuiTreeNodeFlags_OpenOnArrow |
                                ImGuiTreeNodeFlags_SpanAvailWidth;
        if (si == gModel.sel_setup) nf |= ImGuiTreeNodeFlags_DefaultOpen;

        const ImVec2 rowMn = ImGui::GetCursorScreenPos();
        const float  rowH  = ImGui::GetTextLineHeightWithSpacing() + 4.f * S;
        const ImVec2 rowMx(rowMn.x + ImGui::GetContentRegionAvail().x, rowMn.y + rowH);
        theme::glassRowBackground(dl, rowMn, rowMx, 4.f * S,
                                  si % 2 == 1, si == gModel.sel_setup);

        bool open = ImGui::TreeNodeEx(su.name.c_str(), nf);
        if (ImGui::IsItemClicked()) { gModel.sel_setup = si; gModel.sel_op = -1; }

        if (open) {
            ImGui::PushStyleColor(ImGuiCol_Text, TCOL(txt2));
            ImGui::TextUnformatted(su.machine.c_str());
            ImGui::PopStyleColor();

            for (int oi = 0; oi < (int)su.ops.size(); ++oi) {
                const auto& op = su.ops[oi];
                ImGui::PushID(oi);
                bool opSel = (si == gModel.sel_setup && oi == gModel.sel_op);

                const ImVec2 opMn = ImGui::GetCursorScreenPos();
                const float  opH  = ImGui::GetTextLineHeightWithSpacing() + 2.f * S;
                const ImVec2 opMx(opMn.x + ImGui::GetContentRegionAvail().x, opMn.y + opH);
                theme::glassRowBackground(dl, opMn, opMx, 3.f * S, oi % 2 == 1, opSel);

                std::string lbl = op.name.empty() ? op.type : (op.type + "  " + op.name);
                lbl += "  T" + std::to_string(op.tool_number);
                if (op.suppressed) lbl += " [off]";

                if (ImGui::Selectable(lbl.c_str(), opSel,
                                      ImGuiSelectableFlags_None, ImVec2(0, opH - 1.f * S))) {
                    gModel.sel_setup = si;
                    gModel.sel_op    = oi;
                }

                // Engagement-warning dot from gen_warnings
                for (const auto& gw : gModel.gen_warnings) {
                    if (gw.op_id == op.id && !gw.text.empty()) {
                        ImGui::SameLine();
                        ImGui::PushStyleColor(ImGuiCol_Text, gw.is_error ? TCOL(bad) : TCOL(warn));
                        ImGui::TextUnformatted(gw.is_error ? " E" : " W");
                        ImGui::PopStyleColor();
                        break;
                    }
                }
                ImGui::PopID();
            }
            ImGui::TreePop();
        }
        ImGui::PopID();
    }

    // Tool list
    ImGui::Dummy(ImVec2(0, 6.f * S));
    ImGui::PushStyleColor(ImGuiCol_Text, TCOL(txt2));
    ImGui::TextUnformatted("Tools");
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, 2.f * S));

    if (gModel.tools.empty()) {
        drawPanelEmptyState("No tools", "Press 'Add Tool'");
    } else {
        for (const auto& t : gModel.tools) {
            ImGui::PushID(t.number);
            const ImVec2 tMn = ImGui::GetCursorScreenPos();
            const float  tH  = ImGui::GetTextLineHeightWithSpacing() * 2.f + 4.f * S;
            const ImVec2 tMx(tMn.x + ImGui::GetContentRegionAvail().x, tMn.y + tH);
            theme::glassRowBackground(dl, tMn, tMx, 4.f * S);
            ImGui::Indent(6.f * S);
            std::snprintf(buf, sizeof(buf), "T%d  %s",
                          t.number, t.name.empty() ? t.type.c_str() : t.name.c_str());
            ImGui::TextUnformatted(buf);
            ImGui::PushStyleColor(ImGuiCol_Text, TCOL(txt2));
            std::snprintf(buf, sizeof(buf), "\xC3\x98 %.2f mm  %s", t.diameter, t.type.c_str());
            ImGui::TextUnformatted(buf);
            ImGui::PopStyleColor();
            ImGui::Unindent(6.f * S);
            ImGui::Dummy(ImVec2(0, 2.f * S));
            ImGui::PopID();
        }
    }
}

static void drawOpDetail() {
    const aicad::ui::CamOpView* op = selectedOp();
    if (!op) {
        drawPanelEmptyState("No operation selected", "Click an operation in the job tree");
        return;
    }
    const float S = theme::S();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    char buf[128];

    // Type + name header
    ImGui::PushStyleColor(ImGuiCol_Text, TCOL(accent));
    ImGui::TextUnformatted(op->type.c_str());
    ImGui::PopStyleColor();
    if (!op->name.empty()) {
        ImGui::SameLine(0, 6.f * S);
        ImGui::PushStyleColor(ImGuiCol_Text, TCOL(txt2));
        ImGui::TextUnformatted(op->name.c_str());
        ImGui::PopStyleColor();
    }
    ImGui::Dummy(ImVec2(0, 4.f * S));

    auto infoRow = [&](const char* label, const char* value, bool highlight = false) {
        const ImVec2 mn = ImGui::GetCursorScreenPos();
        const float  rH = ImGui::GetTextLineHeightWithSpacing() + 4.f * S;
        const ImVec2 mx(mn.x + ImGui::GetContentRegionAvail().x, mn.y + rH);
        theme::glassRowBackground(dl, mn, mx, 3.f * S);
        ImGui::Indent(4.f * S);
        ImGui::PushStyleColor(ImGuiCol_Text, TCOL(txt2));
        ImGui::TextUnformatted(label);
        ImGui::PopStyleColor();
        ImGui::SameLine(theme::U(12.f));
        if (highlight)
            ImGui::PushStyleColor(ImGuiCol_Text, TCOL(accent));
        ImGui::TextUnformatted(value);
        if (highlight) ImGui::PopStyleColor();
        ImGui::Unindent(4.f * S);
        ImGui::Dummy(ImVec2(0, 1.f * S));
    };

    std::snprintf(buf, sizeof(buf), "T%d", op->tool_number);
    infoRow("Tool", buf);

    // arc_blend_linking — M2/M4 backend field; shown with semantic color
    {
        const ImVec2 mn = ImGui::GetCursorScreenPos();
        const float  rH = ImGui::GetTextLineHeightWithSpacing() + 4.f * S;
        const ImVec2 mx(mn.x + ImGui::GetContentRegionAvail().x, mn.y + rH);
        theme::glassRowBackground(dl, mn, mx, 3.f * S);
        ImGui::Indent(4.f * S);
        ImGui::PushStyleColor(ImGuiCol_Text, TCOL(txt2));
        ImGui::TextUnformatted("Arc-blend linking");
        ImGui::PopStyleColor();
        ImGui::SameLine(theme::U(14.f));
        if (op->arc_blend_linking) {
            ImGui::PushStyleColor(ImGuiCol_Text, TCOL(good));
            ImGui::TextUnformatted("ON — engagement-bounded");
        } else {
            ImGui::PushStyleColor(ImGuiCol_Text, TCOL(warn));
            ImGui::TextUnformatted("OFF — plain concentric");
        }
        ImGui::PopStyleColor();
        ImGui::Unindent(4.f * S);
        ImGui::Dummy(ImVec2(0, 1.f * S));
    }

    if (op->suppressed) {
        ImGui::Dummy(ImVec2(0, 2.f * S));
        ImGui::PushStyleColor(ImGuiCol_Text, TCOL(txt2));
        ImGui::TextUnformatted("[suppressed — excluded from generate]");
        ImGui::PopStyleColor();
    }

    // Engagement / generate warnings for this op
    bool hasWarn = false;
    for (const auto& gw : gModel.gen_warnings) {
        if (gw.op_id != op->id) continue;
        if (!hasWarn) {
            ImGui::Dummy(ImVec2(0, 4.f * S));
            ImGui::Separator();
            ImGui::Dummy(ImVec2(0, 2.f * S));
            ImGui::PushStyleColor(ImGuiCol_Text, TCOL(txt2));
            ImGui::TextUnformatted("Generate warnings:");
            ImGui::PopStyleColor();
            ImGui::Dummy(ImVec2(0, 2.f * S));
            hasWarn = true;
        }
        ImGui::PushStyleColor(ImGuiCol_Text, gw.is_error ? TCOL(bad) : TCOL(warn));
        ImGui::TextWrapped("  %s  %s",
                           gw.is_error ? "[error]" : "[warn]", gw.text.c_str());
        ImGui::PopStyleColor();
    }
}

static void drawSimResults() {
    const float S = theme::S();

    if (ImGui::Button("Run Simulation")) gWantSimulate = true;
    ImGui::Dummy(ImVec2(0, 4.f * S));

    const auto& sim = gModel.sim;
    switch (sim.state) {
        case aicad::ui::CamSimState::None:
            drawPanelEmptyState("No simulation yet", "Press 'Run Simulation'");
            return;
        case aicad::ui::CamSimState::Running:
            drawPanelEmptyState("Simulating\xe2\x80\xa6");
            return;
        case aicad::ui::CamSimState::Error:
            ImGui::PushStyleColor(ImGuiCol_Text, TCOL(bad));
            ImGui::TextWrapped("%s", sim.error_text.c_str());
            ImGui::PopStyleColor();
            return;
        default: break;
    }

    ImDrawList* dl = ImGui::GetWindowDrawList();
    char buf[160];

    auto statRow = [&](const char* label, const char* value) {
        const ImVec2 mn = ImGui::GetCursorScreenPos();
        const float  rH = ImGui::GetTextLineHeightWithSpacing() + 3.f * S;
        const ImVec2 mx(mn.x + ImGui::GetContentRegionAvail().x, mn.y + rH);
        theme::glassRowBackground(dl, mn, mx, 3.f * S);
        ImGui::Indent(4.f * S);
        ImGui::PushStyleColor(ImGuiCol_Text, TCOL(txt2));
        ImGui::TextUnformatted(label);
        ImGui::PopStyleColor();
        ImGui::SameLine(theme::U(14.f));
        ImGui::TextUnformatted(value);
        ImGui::Unindent(4.f * S);
        ImGui::Dummy(ImVec2(0, 1.f * S));
    };

    std::snprintf(buf, sizeof(buf), "%.0f mm\xC2\xB3", sim.stock_volume_mm3);
    statRow("Stock", buf);
    std::snprintf(buf, sizeof(buf), "%.0f mm\xC2\xB3  (%.0f%%)",
                  sim.removed_volume_mm3, sim.machined_fraction * 100.0);
    statRow("Removed", buf);
    std::snprintf(buf, sizeof(buf), "%.0f mm\xC2\xB3", sim.remaining_volume_mm3);
    statRow("Remaining", buf);
    int mm = (int)(sim.cycle_time_s / 60), ss = (int)sim.cycle_time_s % 60;
    std::snprintf(buf, sizeof(buf), "%dm %02ds", mm, ss);
    statRow("Cycle time", buf);
    std::snprintf(buf, sizeof(buf), "%dm %02ds", (int)(sim.cut_time_s/60), (int)sim.cut_time_s%60);
    statRow("  cut", buf);
    std::snprintf(buf, sizeof(buf), "%dm %02ds", (int)(sim.rapid_time_s/60), (int)sim.rapid_time_s%60);
    statRow("  rapid", buf);

    ImGui::Dummy(ImVec2(0, 4.f * S));
    ImGui::Separator();
    ImGui::Dummy(ImVec2(0, 3.f * S));

    // Collision / gouge summary
    bool any = false;
    if (sim.gouges > 0) {
        ImGui::PushStyleColor(ImGuiCol_Text, TCOL(bad));
        std::snprintf(buf, sizeof(buf), "GOUGES: %d  (max %.3f mm)", sim.gouges, sim.max_gouge_mm);
        ImGui::TextUnformatted(buf);
        ImGui::PopStyleColor();
        any = true;
    }
    if (sim.rapid_collisions > 0) {
        ImGui::PushStyleColor(ImGuiCol_Text, TCOL(warn));
        std::snprintf(buf, sizeof(buf), "RAPID THROUGH STOCK: %d", sim.rapid_collisions);
        ImGui::TextUnformatted(buf);
        ImGui::PopStyleColor();
        any = true;
    }
    // holder_collisions: -1 = not checked (T-M3 not yet merged); 0 = checked + clean; >0 = collision
    if (sim.holder_collisions > 0) {
        ImGui::PushStyleColor(ImGuiCol_Text, TCOL(bad));
        std::snprintf(buf, sizeof(buf), "HOLDER COLLISIONS: %d", sim.holder_collisions);
        ImGui::TextUnformatted(buf);
        ImGui::PopStyleColor();
        any = true;
    } else if (sim.holder_collisions == 0) {
        ImGui::PushStyleColor(ImGuiCol_Text, TCOL(txt2));
        ImGui::TextUnformatted("Holder: checked, no collisions");
        ImGui::PopStyleColor();
    }
    // holder_collisions == -1: silently omit (check_holder not enabled yet)

    for (const auto& w : sim.warnings) {
        ImGui::PushStyleColor(ImGuiCol_Text, TCOL(warn));
        ImGui::TextWrapped("\xe2\x9a\xa0 %s", w.c_str());
        ImGui::PopStyleColor();
        any = true;
    }

    if (!any) {
        ImGui::PushStyleColor(ImGuiCol_Text, TCOL(good));
        ImGui::TextUnformatted("No gouges, rapids, or holder collisions.");
        ImGui::PopStyleColor();
    }
}

static void drawGcodeTab() {
    const float S = theme::S();
    auto& p = gModel.post;

    // Dialect picker + post/setup-sheet buttons
    if (!p.dialects.empty()) {
        ImGui::SetNextItemWidth(theme::U(9.f));
        std::vector<const char*> items;
        for (auto& d : p.dialects) items.push_back(d.c_str());
        ImGui::Combo("##cam_dialect", &p.selected_idx, items.data(), (int)items.size());
        ImGui::SameLine();
    }
    if (ImGui::Button("Post"))        gWantPost = true;
    ImGui::SameLine();
    if (ImGui::Button("Setup Sheet")) gWantSetupSheet = true;

    if (!p.gcode_path.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, TCOL(txt2));
        ImGui::TextWrapped("\xe2\x86\x92 %s", p.gcode_path.c_str());
        ImGui::PopStyleColor();
    }
    if (!p.setup_sheet_path.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, TCOL(txt2));
        ImGui::TextWrapped("Setup sheet: %s", p.setup_sheet_path.c_str());
        ImGui::PopStyleColor();
    }

    ImGui::Dummy(ImVec2(0, 4.f * S));
    ImGui::Separator();
    ImGui::Dummy(ImVec2(0, 2.f * S));

    if (p.gcode.empty()) {
        drawPanelEmptyState("No G-code yet", "Choose a dialect and press Post");
    } else {
        if (p.line_count > 0) {
            ImGui::PushStyleColor(ImGuiCol_Text, TCOL(txt2));
            char lc[32]; std::snprintf(lc, sizeof(lc), "%d lines", p.line_count);
            ImGui::TextUnformatted(lc);
            ImGui::PopStyleColor();
        }
        ImGui::InputTextMultiline("##cam_gcode",
                                  p.gcode.data(), p.gcode.size() + 1,
                                  ImVec2(-1.f, -1.f),
                                  ImGuiInputTextFlags_ReadOnly);
    }
}

}  // anonymous namespace

// ============================================================================
// Public API
// ============================================================================

void openCamWorkspacePanel() {
    gOpen = true;
    gFocusFrames = 3;
}

bool* camWorkspacePanelOpen() { return &gOpen; }

void camWorkspaceLoadJob(app::AppState& s, const std::string& docId) {
    if (docId.empty()) return;
    gModel = aicad::ui::CamViewModel{};
    gModel.job_doc_id = docId;
    json r;
    if (camCall(s, "cam_tree", {{"doc_id", docId}}, r)) loadTree(r);
    // Pull cached toolpaths — cam_program pre-generates them; cam_generate returns the cache.
    json gp;
    if (camCall(s, "cam_generate", {{"doc_id", docId}}, gp)) loadGenWarnings(gp);
    gModel.status          = "loaded CAM job";
    gModel.status_is_error = false;
    gOpen        = true;
    gFocusFrames = 6;
}

void drawCamWorkspacePanel(app::AppState& s) {
    refreshDialects(s);
    if (gFocusFrames > 0) { ImGui::SetWindowFocus(); --gFocusFrames; }

    const float S = theme::S();

    // ---- Toolbar: job management + op add buttons ---------------------------
    if (ImGui::Button("New Job")) {
        gModel = aicad::ui::CamViewModel{};
        ensureJob(s);
    }
    ImGui::SameLine();
    if (ImGui::Button("Add Setup")) {
        if (ensureJob(s)) {
            applyOps(s, json::array({json{
                {"op", "add_setup"},
                {"setup", {{"name", "Setup" + std::to_string(gModel.setups.size() + 1)},
                           {"machine", {{"kind", "mill3"}}},
                           {"stock",   {{"type","box_offset"},{"off_ztop",1.0},
                                        {"material","aluminum"}}}}}}}));
            gModel.sel_setup = (int)gModel.setups.size() - 1;
        }
    }
    ImGui::SameLine();
    if (ImGui::Button("Add Tool")) {
        if (ensureJob(s)) {
            int n = (int)gModel.tools.size() + 1;
            applyOps(s, json::array({json{
                {"op","add_tool"},
                {"tool",{{"number",n},{"type","endmill_flat"},{"diameter",6},
                         {"flutes",3},{"name","6mm flat endmill"}}}}}));
            gModel.status          = "added tool T" + std::to_string(n);
            gModel.status_is_error = false;
        }
    }

    ImGui::Dummy(ImVec2(0, 2.f * S));

    // Op-add compact button row
    // Each op button carries its CAM toolpath tile (host/assets/icons atlas, "cam_"+type).
    // The tile is a multi-color raster (opted out of SDF via atlas.json "raster_only"), drawn in
    // the label's leading pad so it reads as icon + text.
    auto opBtn = [&](const char* lbl, const char* type, const char* icon) {
        const ImVec2 p = ImGui::GetCursorScreenPos();
        const float lh = ImGui::GetTextLineHeight();
        const bool clicked = ImGui::SmallButton(lbl);
        icons::draw(ImGui::GetWindowDrawList(), icon,
                    ImVec2(p.x + lh * 0.62f, p.y + lh * 0.5f), lh * 0.95f,
                    IM_COL32(255, 255, 255, 255));
        if (clicked) addOp(s, type);
        ImGui::SameLine();
    };
    opBtn("   Face",     "face",       "cam_face");
    opBtn("   Contour",  "contour2d",  "cam_contour2d");
    opBtn("   Pocket",   "pocket2d",   "cam_pocket2d");
    opBtn("   Adaptive", "adaptive2d", "cam_adaptive2d");
    opBtn("   Drill",    "drill",      "cam_drill");
    opBtn("   3D Para",  "parallel3d", "cam_parallel3d");
    opBtn("   Turn",     "turn_rough", "cam_turn_rough");
    opBtn("   Laser",    "cut2d",      "cam_cut2d");

    ImGui::Dummy(ImVec2(0, 3.f * S));

    // Generate / Simulate / Post row
    if (ImGui::Button("Generate")) doGenerate(s);
    ImGui::SameLine();
    if (ImGui::Button("Simulate")) doSimulate(s);
    if (!gModel.post.dialects.empty()) {
        ImGui::SameLine();
        ImGui::SetNextItemWidth(theme::U(9.f));
        std::vector<const char*> items;
        for (auto& d : gModel.post.dialects) items.push_back(d.c_str());
        ImGui::Combo("##post_hdr", &gModel.post.selected_idx, items.data(), (int)items.size());
        ImGui::SameLine();
        if (ImGui::Button("Post"))        doPost(s);
        ImGui::SameLine();
        if (ImGui::Button("Setup Sheet")) doSetupSheet(s);
    }

    ImGui::Dummy(ImVec2(0, 3.f * S));
    ImGui::Separator();
    ImGui::Dummy(ImVec2(0, 3.f * S));

    // ---- Two-column layout: tree (35%) | detail tabs (65%) -----------------
    const float avail  = ImGui::GetContentRegionAvail().x;
    const float leftW  = avail * 0.35f;
    const float gap    = 6.f * S;
    const float rightW = avail - leftW - gap;
    const float colH   = ImGui::GetContentRegionAvail().y;

    // LEFT: scrollable tree via scaffold (no search bar)
    ImGui::BeginChild("##cam_tree_child", ImVec2(leftW, colH), false);
    {
        PanelScaffoldSpec spec;
        spec.id        = "cam_workspace";
        spec.title     = "Job / Tools";
        spec.hasSearch = false;

        drawPanelScaffold(spec, gScaffoldState, []() { drawJobTree(); });
    }
    ImGui::EndChild();

    ImGui::SameLine(0, gap);

    // RIGHT: tabbed detail view
    ImGui::BeginChild("##cam_detail_child", ImVec2(rightW, colH), false);
    if (ImGui::BeginTabBar("##cam_detail_tabs")) {
        if (ImGui::BeginTabItem("Operation")) {
            ImGui::Dummy(ImVec2(0, 3.f * S));
            drawOpDetail();
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Simulate")) {
            ImGui::Dummy(ImVec2(0, 3.f * S));
            drawSimResults();
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("G-code")) {
            ImGui::Dummy(ImVec2(0, 3.f * S));
            drawGcodeTab();
            ImGui::EndTabItem();
        }
        ImGui::EndTabBar();
    }
    ImGui::EndChild();

    // ---- Footer status (drives scaffold state line) -------------------------
    gScaffoldState.statusText    = gModel.status;
    gScaffoldState.statusIsError = gModel.status_is_error;

    // Drain deferred action flags (set by sub-draws that lack AppState&)
    if (gWantSimulate)   { doSimulate(s);   gWantSimulate   = false; }
    if (gWantPost)       { doPost(s);       gWantPost       = false; }
    if (gWantSetupSheet) { doSetupSheet(s); gWantSetupSheet = false; }
}

}  // namespace ui
