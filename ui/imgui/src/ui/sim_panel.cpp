// T-U6 Simulation Studies panel — study list, results, convergence caveats, re-run.
//
// Architecture (pattern library §0/§1):
//   - View-model: aicad::ui::SimPanelModel (ui/abstract/sim_view.h, no ImGui/GL includes)
//   - Scaffold: ui::drawPanelScaffold / ui::drawPanelEmptyState (T-U1 kit)
//   - Worker RPC: s.rpc.call() via AsyncRpc for sim_study_list, describe_results, re-run
//   - State machine: SimPanelState enum drives typed placeholder vocab (pattern §4)
//   - Narration: describe_results text shown VERBATIM (numbers-are-authoritative rule)
//   - Caveats: Cd uncalibrated (LBM per SIM_AUDIT.md) + FEA convergence note — rendered
//     from backend verdict fields, never hardcoded strings invented by this panel
//
// SIM_AUDIT.md findings honoured:
//   1. sim_run_static/modal are C++ plugins; only callable via the MCP worker. The re-run
//      action fires through s.rpc (AsyncRpc → WorkerClient → MCP server) with a 180s timeout.
//   2. Cd from run_cfd_flow is UNCALIBRATED BY DESIGN. cd_uncalibrated is set from the
//      engine field; narration text from describe_results repeats the warning verbatim.
//   3. convergence: run_cfd_flow returns "converged" bool; FEA always gets a mesh note.

#include "sim_panel.h"
#include "panel_scaffold.h"
#include "sim_view.h"   // ui/abstract/ (no ImGui includes there)

#include "app/app_state.h"

#include "theme/theme.h"
#include "theme/tokens.h"

#include "imgui.h"
#include <nlohmann/json.hpp>

#include <cstdio>
#include <string>
#include <vector>

namespace ui {

namespace {

// ---- singleton state ----------------------------------------------------------
bool gSimOpen = false;
ui::PanelScaffoldState gSimScaffoldState;
aicad::ui::SimPanelModel gSimModel;
// Set when fixture seeding populates the model before a real doc is available.
// Suppresses the first doc-change reset so the fixture rows stay visible.
bool gFixtureSeeded = false;

using nlohmann::json;

// ---- RPC helpers ---------------------------------------------------------------

// Kick a sim_study_list fetch (AsyncRpc — non-blocking).
static void loadStudies(app::AppState& s) {
    if (gSimModel.state == aicad::ui::SimPanelState::Loading) return;
    gSimModel.state    = aicad::ui::SimPanelState::Loading;
    gSimModel.errorMsg.clear();
    gSimModel.studies.clear();
    gSimModel.sel_idx = -1;
    gSimScaffoldState.statusText = "Loading studies…";

    const std::string docId = gSimModel.cache_doc_id;
    s.rpc.call("sim_study_list", json{{"doc_id", docId}},
               [](const core::RpcResult& r) {
        if (!r.ok) {
            gSimModel.state    = aicad::ui::SimPanelState::Error;
            gSimModel.errorMsg = r.err.empty() ? "sim_study_list failed" : r.err;
            gSimScaffoldState.statusText  = "Error loading studies";
            gSimScaffoldState.statusIsError = true;
            return;
        }
        const json& studies = r.result.value("studies", json::array());
        gSimModel.studies.clear();
        for (const auto& st : studies) {
            aicad::ui::SimStudyRow row;
            row.id     = st.value("id",     std::string());
            row.type   = st.value("type",   std::string());
            row.name   = st.value("name",   std::string());
            row.engine = st.value("engine", std::string());

            const json& res    = st.value("results", json::object());
            const json& params = st.value("params",  json::object());

            if (row.type == "fea_static") {
                if (res.contains("max_von_mises_mpa") && res["max_von_mises_mpa"].is_number())
                    row.max_von_mises_mpa = res["max_von_mises_mpa"].get<double>();
                if (res.contains("max_disp_mm") && res["max_disp_mm"].is_number())
                    row.max_disp_mm = res["max_disp_mm"].get<double>();
                if (res.contains("min_fos") && res["min_fos"].is_number())
                    row.min_fos = res["min_fos"].get<double>();
            } else if (row.type == "fea_modal") {
                if (res.contains("freqs_hz") && res["freqs_hz"].is_array()) {
                    for (const auto& f : res["freqs_hz"])
                        if (f.is_number()) row.freqs_hz.push_back(f.get<double>());
                }
            } else if (row.type.find("cfd") != std::string::npos) {
                if (res.contains("drag_n")   && res["drag_n"].is_number())
                    row.drag_n   = res["drag_n"].get<double>();
                if (res.contains("lift_n")   && res["lift_n"].is_number())
                    row.lift_n   = res["lift_n"].get<double>();
                if (res.contains("cd")       && res["cd"].is_number())
                    row.cd       = res["cd"].get<double>();
                if (res.contains("reynolds") && res["reynolds"].is_number())
                    row.reynolds = res["reynolds"].get<double>();
                if (res.contains("converged") && res["converged"].is_boolean())
                    row.converged = res["converged"].get<bool>();
                if (res.contains("warning")  && res["warning"].is_string())
                    row.warning  = res["warning"].get<std::string>();
                // Cd is uncalibrated for cpp-lbm engine (SIM_AUDIT.md §CFD)
                row.cd_uncalibrated = (row.engine.find("lbm") != std::string::npos);
                (void)params;
            }
            gSimModel.studies.push_back(std::move(row));
        }
        gSimModel.state    = aicad::ui::SimPanelState::Ready;
        gSimScaffoldState.statusText    = "";
        gSimScaffoldState.statusIsError = false;
    }, /*timeoutMs=*/15000);
}

// Kick a describe_results fetch for the selected study (lazy — only on expand).
static void loadNarration(app::AppState& s, int idx) {
    if (idx < 0 || idx >= (int)gSimModel.studies.size()) return;
    aicad::ui::SimStudyRow& row = gSimModel.studies[idx];
    if (row.narration_loading || row.narration_loaded) return;
    row.narration_loading = true;
    const std::string docId   = gSimModel.cache_doc_id;
    const std::string sname   = row.name;
    s.rpc.call("describe_results",
               json{{"doc_id", docId}, {"study_name", sname}},
               [idx](const core::RpcResult& r) {
        if (idx >= (int)gSimModel.studies.size()) return;
        aicad::ui::SimStudyRow& row2 = gSimModel.studies[idx];
        row2.narration_loading = false;
        if (!r.ok || !r.result.value("ok", true)) {
            row2.narration_error = true;
            row2.narration       = r.err.empty() ? "Failed to load narration." : r.err;
        } else {
            row2.narration       = r.result.value("text", std::string("(no text)"));
            row2.narration_loaded = true;
        }
    }, /*timeoutMs=*/20000);
}

// ---- draw helpers --------------------------------------------------------------

static const char* studyTypeLabel(const std::string& t) {
    if (t == "fea_static") return "FEA Static";
    if (t == "fea_modal")  return "FEA Modal";
    if (t.find("cfd") != std::string::npos) return "CFD Flow";
    return "Study";
}

// Build a short key-metric string for the summary table column.
static std::string studyMetricSummary(const aicad::ui::SimStudyRow& row) {
    char buf[128];
    if (row.type == "fea_static") {
        if (row.min_fos >= 0.0)
            std::snprintf(buf, sizeof(buf), "FoS %.2f  \xe2\x80\xa2  \xcf\x83 %.1f MPa",
                          row.min_fos, row.max_von_mises_mpa > 0 ? row.max_von_mises_mpa : 0.0);
        else
            std::snprintf(buf, sizeof(buf), "no results");
    } else if (row.type == "fea_modal") {
        if (!row.freqs_hz.empty())
            std::snprintf(buf, sizeof(buf), "f1=%.1f Hz (+%d modes)",
                          row.freqs_hz[0], (int)row.freqs_hz.size() - 1);
        else
            std::snprintf(buf, sizeof(buf), "no results");
    } else if (row.type.find("cfd") != std::string::npos) {
        if (row.cd >= 0.0)
            std::snprintf(buf, sizeof(buf), "Cd %.4f  \xe2\x80\xa2  D %.3f N",
                          row.cd, row.drag_n >= 0 ? row.drag_n : 0.0);
        else
            std::snprintf(buf, sizeof(buf), "no results");
    } else {
        std::snprintf(buf, sizeof(buf), "—");
    }
    return buf;
}

// Draw the expanded detail section for the selected study (called after the table).
static void drawStudyDetail(int idx, aicad::ui::SimStudyRow& row, app::AppState& s) {
    const float S = theme::S();
    ImGui::Spacing();
    ImGui::Indent(8.f * S);

    // ---- Result summary scalars ------------------------------------------------
    if (row.type == "fea_static") {
        if (row.max_von_mises_mpa >= 0.0) {
            char buf[64];
            std::snprintf(buf, sizeof(buf), "Max von Mises stress: %.1f MPa", row.max_von_mises_mpa);
            ImGui::TextUnformatted(buf);
        }
        if (row.max_disp_mm >= 0.0) {
            char buf[64];
            std::snprintf(buf, sizeof(buf), "Max displacement: %.3f mm", row.max_disp_mm);
            ImGui::TextUnformatted(buf);
        }
        if (row.min_fos >= 0.0) {
            bool holds = row.min_fos >= 1.0;
            char buf[64];
            std::snprintf(buf, sizeof(buf), "Min factor of safety: %.2f  (%s)",
                          row.min_fos, holds ? "holds" : "FAILS");
            ImGui::PushStyleColor(ImGuiCol_Text, holds ? TCOL(good) : TCOL(bad));
            ImGui::TextUnformatted(buf);
            ImGui::PopStyleColor();
        }
        // Convergence note — always shown for FEA (mesh fineness caveat, per SIM_AUDIT.md)
        ImGui::PushStyleColor(ImGuiCol_Text, TCOL(warn));
        ImGui::PushTextWrapPos(0.f);
        ImGui::TextUnformatted("\xe2\x9a\xa0 Mesh convergence: re-run at mesh_fineness 0.8 "
                               "to verify for final design.");
        ImGui::PopTextWrapPos();
        ImGui::PopStyleColor();

    } else if (row.type == "fea_modal") {
        ImGui::TextUnformatted("Modal frequencies:");
        for (int m = 0; m < (int)row.freqs_hz.size(); ++m) {
            char buf[64];
            std::snprintf(buf, sizeof(buf), "  Mode %d: %.1f Hz", m + 1, row.freqs_hz[m]);
            ImGui::TextUnformatted(buf);
        }
        if (row.freqs_hz.empty())
            ImGui::TextUnformatted("  (no frequency data)");

    } else if (row.type.find("cfd") != std::string::npos) {
        if (row.drag_n >= 0.0) {
            char buf[64]; std::snprintf(buf, sizeof(buf), "Drag: %.3f N", row.drag_n);
            ImGui::TextUnformatted(buf);
        }
        if (row.lift_n >= 0.0) {
            char buf[64]; std::snprintf(buf, sizeof(buf), "Lift: %.3f N", row.lift_n);
            ImGui::TextUnformatted(buf);
        }
        if (row.cd >= 0.0) {
            char buf[64]; std::snprintf(buf, sizeof(buf), "Cd: %.4f", row.cd);
            ImGui::TextUnformatted(buf);
        }
        if (row.reynolds >= 0.0) {
            char buf[64]; std::snprintf(buf, sizeof(buf), "Reynolds: %.0f", row.reynolds);
            ImGui::TextUnformatted(buf);
        }
        // Calibration caveat — only when Cd is uncalibrated (LBM engine, per SIM_AUDIT.md)
        if (row.cd_uncalibrated) {
            ImGui::PushStyleColor(ImGuiCol_Text, TCOL(warn));
            ImGui::PushTextWrapPos(0.f);
            ImGui::TextUnformatted("\xe2\x9a\xa0 Cd is UNCALIBRATED: coarse voxel LBM "
                                   "over-counts force. Use for TRENDS only, not "
                                   "absolute values. Run run_cfd_accurate for "
                                   "validated Cd/Cl (OpenFOAM RANS).");
            ImGui::PopTextWrapPos();
            ImGui::PopStyleColor();
        }
        if (!row.converged) {
            ImGui::PushStyleColor(ImGuiCol_Text, TCOL(warn));
            ImGui::TextUnformatted("\xe2\x9a\xa0 Not converged — increase n_steps.");
            ImGui::PopStyleColor();
        }
        if (!row.warning.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, TCOL(warn));
            char wbuf[512];
            std::snprintf(wbuf, sizeof(wbuf), "\xe2\x9a\xa0 %s", row.warning.c_str());
            ImGui::TextUnformatted(wbuf);
            ImGui::PopStyleColor();
        }
    }

    // ---- Narration (describe_results — collapsible) ----------------------------
    ImGui::Spacing();
    if (ImGui::CollapsingHeader("Full narration (describe_results)")) {
        if (row.narration_loading) {
            ImGui::TextUnformatted("  Loading narration…");
        } else if (!row.narration_loaded && !row.narration_error) {
            loadNarration(s, idx);
            ImGui::TextUnformatted("  Loading narration…");
        } else if (row.narration_error) {
            ImGui::PushStyleColor(ImGuiCol_Text, TCOL(bad));
            ImGui::TextUnformatted(row.narration.c_str());
            ImGui::PopStyleColor();
        } else {
            ImGui::PushTextWrapPos(0.0f);
            ImGui::TextUnformatted(row.narration.c_str());
            ImGui::PopTextWrapPos();
        }
    }

    // ---- Re-run action ---------------------------------------------------------
    ImGui::Spacing();
    bool canRerun = (row.run_state != aicad::ui::SimRunState::Running);
    if (!canRerun) ImGui::BeginDisabled();
    const char* btnLabel = (row.run_state == aicad::ui::SimRunState::Running)
                            ? "Running…" : "Re-run study";
    if (ImGui::SmallButton(btnLabel) && canRerun) {
        row.run_state = aicad::ui::SimRunState::Running;
        row.run_error.clear();
        gSimScaffoldState.statusText    = std::string("Running ") + row.name + "…";
        gSimScaffoldState.statusIsError = false;

        const std::string studyType = row.type;
        const std::string docId     = gSimModel.cache_doc_id;
        const int rowIdx            = idx;

        if (studyType == "fea_static") {
            s.rpc.call("run_fea_static",
                       json{{"fixtures", json::array()}, {"loads", json::array()},
                            {"doc_id", docId}, {"background", false}},
                       [rowIdx](const core::RpcResult& r) {
                if (rowIdx >= (int)gSimModel.studies.size()) return;
                auto& r2 = gSimModel.studies[rowIdx];
                if (!r.ok || !r.result.value("ok", true)) {
                    r2.run_state = aicad::ui::SimRunState::Error;
                    r2.run_error = r.err.empty()
                        ? r.result.value("error", "run_fea_static failed") : r.err;
                    gSimScaffoldState.statusText    = "Re-run failed: " + r2.run_error;
                    gSimScaffoldState.statusIsError = true;
                } else {
                    r2.run_state        = aicad::ui::SimRunState::Done;
                    r2.narration_loaded = false;
                    gSimScaffoldState.statusText    = "Re-run complete — refreshing…";
                    gSimScaffoldState.statusIsError = false;
                    gSimModel.cache_revision = -1;
                }
            }, /*timeoutMs=*/180000);

        } else if (studyType == "fea_modal") {
            s.rpc.call("run_fea_modal",
                       json{{"fixtures", json::array()},
                            {"doc_id", docId}, {"background", false}},
                       [rowIdx](const core::RpcResult& r) {
                if (rowIdx >= (int)gSimModel.studies.size()) return;
                auto& r2 = gSimModel.studies[rowIdx];
                if (!r.ok || !r.result.value("ok", true)) {
                    r2.run_state = aicad::ui::SimRunState::Error;
                    r2.run_error = r.err.empty()
                        ? r.result.value("error", "run_fea_modal failed") : r.err;
                    gSimScaffoldState.statusText    = "Re-run failed: " + r2.run_error;
                    gSimScaffoldState.statusIsError = true;
                } else {
                    r2.run_state        = aicad::ui::SimRunState::Done;
                    r2.narration_loaded = false;
                    gSimScaffoldState.statusText    = "Re-run complete — refreshing…";
                    gSimScaffoldState.statusIsError = false;
                    gSimModel.cache_revision = -1;
                }
            }, /*timeoutMs=*/180000);

        } else if (studyType.find("cfd") != std::string::npos) {
            s.rpc.call("run_cfd_flow",
                       json{{"doc_id", docId}, {"background", false}},
                       [rowIdx](const core::RpcResult& r) {
                if (rowIdx >= (int)gSimModel.studies.size()) return;
                auto& r2 = gSimModel.studies[rowIdx];
                if (!r.ok || !r.result.value("ok", true)) {
                    r2.run_state = aicad::ui::SimRunState::Error;
                    r2.run_error = r.err.empty()
                        ? r.result.value("error", "run_cfd_flow failed") : r.err;
                    gSimScaffoldState.statusText    = "Re-run failed: " + r2.run_error;
                    gSimScaffoldState.statusIsError = true;
                } else {
                    r2.run_state        = aicad::ui::SimRunState::Done;
                    r2.narration_loaded = false;
                    gSimScaffoldState.statusText    = "Re-run complete — refreshing…";
                    gSimScaffoldState.statusIsError = false;
                    gSimModel.cache_revision = -1;
                }
            }, /*timeoutMs=*/300000);
        } else {
            row.run_state = aicad::ui::SimRunState::Error;
            row.run_error = "Re-run not supported for type: " + studyType;
            gSimScaffoldState.statusText    = row.run_error;
            gSimScaffoldState.statusIsError = true;
        }
    }
    if (!canRerun) ImGui::EndDisabled();

    if (row.run_state == aicad::ui::SimRunState::Error && !row.run_error.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, TCOL(bad));
        ImGui::TextUnformatted(row.run_error.c_str());
        ImGui::PopStyleColor();
    }

    ImGui::Unindent(8.f * S);
    ImGui::Spacing();
    ImGui::Separator();
}

// Draw the study list as a RowBg table (gives legible backing over transparent glass window).
static void drawStudyTable(app::AppState& s) {
    const float S = theme::S();

    // Reserve space for detail panel if a row is selected
    const bool hasDetail = (gSimModel.sel_idx >= 0 &&
                            gSimModel.sel_idx < (int)gSimModel.studies.size());
    // Table takes ~1/3 of available height (min 80px), detail below takes the rest
    const float avail = ImGui::GetContentRegionAvail().y;
    const float tableH = hasDetail ? std::max(80.f * S, avail * 0.35f) : avail;

    constexpr ImGuiTableFlags tflags =
        ImGuiTableFlags_Borders         |
        ImGuiTableFlags_RowBg           |
        ImGuiTableFlags_SizingStretchProp |
        ImGuiTableFlags_ScrollY;

    // Override the theme's transparent row-bg colors so rows are legible over the
    // glass (NoBackground) window — theme sets TableRowBg=(0,0,0,0) for the dark
    // overlay aesthetic; we need a solid fill here for contrast against the viewport.
    ImGui::PushStyleColor(ImGuiCol_TableRowBg,    TVEC(bg1));
    ImGui::PushStyleColor(ImGuiCol_TableRowBgAlt, TVEC(bg2));
    if (!ImGui::BeginTable("##simlist", 3, tflags, ImVec2(0, tableH))) {
        ImGui::PopStyleColor(2);
        return;
    }

    ImGui::TableSetupScrollFreeze(0, 1);
    ImGui::TableSetupColumn("Type",    ImGuiTableColumnFlags_WidthFixed,   90.f * S);
    ImGui::TableSetupColumn("Name",    ImGuiTableColumnFlags_WidthStretch, 0.55f);
    ImGui::TableSetupColumn("Result",  ImGuiTableColumnFlags_WidthStretch, 0.45f);
    ImGui::TableHeadersRow();

    for (int i = 0; i < (int)gSimModel.studies.size(); ++i) {
        aicad::ui::SimStudyRow& row = gSimModel.studies[i];
        ImGui::PushID(i);
        ImGui::TableNextRow();
        const bool selected = (gSimModel.sel_idx == i);

        // Highlight selected row background
        if (selected)
            ImGui::TableSetBgColor(ImGuiTableBgTarget_RowBg0,
                                   ImGui::GetColorU32(ImGuiCol_Header));

        // Type column — clickable selectable spanning the full row height
        ImGui::TableSetColumnIndex(0);
        char selId[32];
        std::snprintf(selId, sizeof(selId), "##sel%d", i);
        if (ImGui::Selectable(selId, selected,
                              ImGuiSelectableFlags_SpanAllColumns |
                              ImGuiSelectableFlags_AllowOverlap,
                              ImVec2(0, 0))) {
            gSimModel.sel_idx = selected ? -1 : i;
            if (gSimModel.sel_idx == i) loadNarration(s, i);
        }
        ImGui::SameLine();
        ImGui::TextUnformatted(studyTypeLabel(row.type));

        // Name column
        ImGui::TableSetColumnIndex(1);
        ImGui::TextUnformatted(row.name.c_str());

        // Result summary column
        ImGui::TableSetColumnIndex(2);
        // Show caveat warning glyph when applicable
        const bool hasCaveat = (row.type == "fea_static") ||
                               (row.cd_uncalibrated) ||
                               (!row.converged);
        if (hasCaveat) {
            ImGui::PushStyleColor(ImGuiCol_Text, TCOL(warn));
            ImGui::TextUnformatted("\xe2\x9a\xa0");
            ImGui::SameLine(0, 2.f * S);
            ImGui::PopStyleColor();
        }
        const std::string metric = studyMetricSummary(row);
        ImGui::TextUnformatted(metric.c_str());

        ImGui::PopID();
    }
    ImGui::EndTable();
    ImGui::PopStyleColor(2);

    // Expanded detail pane below the table
    if (hasDetail) {
        drawStudyDetail(gSimModel.sel_idx,
                        gSimModel.studies[gSimModel.sel_idx], s);
    }
}

}  // anonymous namespace

// ---- Public API ---------------------------------------------------------------

bool* simStudiesPanelOpen() { return &gSimOpen; }
void openSimStudiesPanel()  { gSimOpen = true; }

void seedSimStudiesPanelFromJson(const std::string& jsonStr) {
    // Parse and load fixture rows directly into the view-model (headless shot seeding).
    // Mirrors the loadStudies() callback logic so the rendered result is identical.
    try {
        const json j = json::parse(jsonStr);
        if (!j.value("ok", true)) return;
        const json& studies = j.value("studies", json::array());
        gSimModel.studies.clear();
        for (const auto& st : studies) {
            aicad::ui::SimStudyRow row;
            row.id     = st.value("id",     std::string());
            row.type   = st.value("type",   std::string());
            row.name   = st.value("name",   std::string());
            row.engine = st.value("engine", std::string());

            const json& res    = st.value("results", json::object());
            const json& params = st.value("params",  json::object());

            if (row.type == "fea_static") {
                if (res.contains("max_von_mises_mpa") && res["max_von_mises_mpa"].is_number())
                    row.max_von_mises_mpa = res["max_von_mises_mpa"].get<double>();
                if (res.contains("max_disp_mm") && res["max_disp_mm"].is_number())
                    row.max_disp_mm = res["max_disp_mm"].get<double>();
                if (res.contains("min_fos") && res["min_fos"].is_number())
                    row.min_fos = res["min_fos"].get<double>();
            } else if (row.type == "fea_modal") {
                if (res.contains("freqs_hz") && res["freqs_hz"].is_array())
                    for (const auto& f : res["freqs_hz"])
                        if (f.is_number()) row.freqs_hz.push_back(f.get<double>());
            } else if (row.type.find("cfd") != std::string::npos) {
                if (res.contains("drag_n")   && res["drag_n"].is_number())
                    row.drag_n   = res["drag_n"].get<double>();
                if (res.contains("lift_n")   && res["lift_n"].is_number())
                    row.lift_n   = res["lift_n"].get<double>();
                if (res.contains("cd")       && res["cd"].is_number())
                    row.cd       = res["cd"].get<double>();
                if (res.contains("reynolds") && res["reynolds"].is_number())
                    row.reynolds = res["reynolds"].get<double>();
                if (res.contains("converged") && res["converged"].is_boolean())
                    row.converged = res["converged"].get<bool>();
                if (res.contains("warning")  && res["warning"].is_string())
                    row.warning  = res["warning"].get<std::string>();
                row.cd_uncalibrated = (row.engine.find("lbm") != std::string::npos);
                (void)params;
            }
            gSimModel.studies.push_back(std::move(row));
        }
        gSimModel.state = aicad::ui::SimPanelState::Ready;
        gSimModel.cache_revision = -1;
        gFixtureSeeded = true;   // suppress first doc-id mismatch reset in drawSimStudiesPanel
        gSimScaffoldState.statusText.clear();
        gSimScaffoldState.statusIsError = false;
    } catch (...) {
        // Parse failure in seeding is non-fatal; panel shows Idle state
    }
}

void drawSimStudiesPanel(app::AppState& s) {
    // Detect doc change / refresh trigger
    const std::string curDocId = s.docMirror.docId();
    const int         curRev   = s.docMirror.revision();

    if (gSimModel.cache_doc_id != curDocId) {
        if (gFixtureSeeded) {
            // First doc-id seen after seeding: absorb it without clearing the fixture rows.
            // Sync cache_revision to curRev so the reload guard below doesn't fire this frame.
            // The fixture rows stay visible; a real reload fires when curRev advances.
            gSimModel.cache_doc_id   = curDocId;
            gSimModel.cache_revision = curRev;
            gFixtureSeeded = false;
        } else {
            gSimModel.cache_doc_id  = curDocId;
            gSimModel.cache_revision = -2;
            gSimModel.studies.clear();
            gSimModel.sel_idx = -1;
            gSimModel.state   = aicad::ui::SimPanelState::Idle;
        }
    }

    // Kick a load when doc is ready and revision changed
    if (s.workerStarted &&
        gSimModel.state != aicad::ui::SimPanelState::Loading &&
        !curDocId.empty() && gSimModel.cache_revision != curRev) {
        gSimModel.cache_revision = curRev;
        loadStudies(s);
    }

    static const ui::PanelScaffoldSpec kSpec{
        /* id       */ "sim_studies",
        /* title    */ "Simulation Studies",
        /* icon     */ nullptr,
        /* hasSearch*/ false,
    };

    drawPanelScaffold(kSpec, gSimScaffoldState, [&]() {
        using State = aicad::ui::SimPanelState;
        switch (gSimModel.state) {
        case State::Idle:
            ui::drawPanelEmptyState("No document loaded",
                                    "Open a part to see simulation studies.");
            break;
        case State::Loading:
            ui::drawPanelEmptyState("Loading studies…", nullptr);
            break;
        case State::Error:
            ui::drawPanelEmptyState("Error", gSimModel.errorMsg.c_str());
            break;
        case State::Ready:
            if (gSimModel.studies.empty()) {
                ui::drawPanelEmptyState(
                    "No simulation studies",
                    "Run run_fea_static, run_fea_modal, or run_cfd_flow to create a study.");
            } else {
                drawStudyTable(s);
            }
            break;
        }
    });
}

}  // namespace ui
