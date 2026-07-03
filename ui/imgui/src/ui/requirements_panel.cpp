// T-U5 Requirements & verification panel — see requirements_panel.h.
//
// Architecture (pattern library §0/§1):
//   - View-model: aicad::ui::ReqPanelModel (ui/abstract/req_view.h, no ImGui/GL includes)
//   - Scaffold: ui::drawPanelScaffold / ui::drawPanelEmptyState (T-U1 kit)
//   - Worker RPC: s.rpc.call("req_report" / "req_status") via AsyncRpc
//   - State machine: ReqPanelState enum drives typed placeholder vocab (pattern §4)
//   - Req table: status pill + kind badge + quantity string per row
//   - Battery results: per-check pass/fail rows with evidence string
//
// Status pill colours follow theme tokens only — no literal ImVec4 / IM_COL32.
// Pill map (engine_oracle/requirements/requirements.py:VALID_STATUSES):
//   proposed       → txt2  (neutral / unreviewed)
//   user_confirmed → accent (user explicitly marked OK)
//   verified       → ok    (agent verified)
//   failed         → bad   (verification failed)
//   waived         → txt2  (intentionally skipped)
//
// Waive path: UI gates Waive button on non-empty waive_reason and passes
//   who="user" in the req_status RPC payload (set_status contract L375).
#include "requirements_panel.h"
#include "panel_scaffold.h"
#include "req_view.h"    // ui/abstract/ (no ImGui includes there)

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

// ---- Panel state ------------------------------------------------------------
bool gReqOpen = false;
ui::PanelScaffoldState gReqScaffoldState;
aicad::ui::ReqPanelModel gReqModel;

// ---- JSON parsing helpers ---------------------------------------------------

// Format a Quantity dict as "op value unit" or empty string.
static std::string fmtQuantity(const nlohmann::json& qty) {
    if (!qty.is_object()) return {};
    const std::string op    = qty.value("op",    std::string());
    const std::string unit  = qty.value("unit",  std::string());
    // value may be number or string
    std::string val;
    if (qty.contains("value")) {
        const auto& v = qty["value"];
        if (v.is_number()) {
            char buf[32];
            std::snprintf(buf, sizeof(buf), "%g", v.get<double>());
            val = buf;
        } else if (v.is_string()) {
            val = v.get<std::string>();
        }
    }
    if (val.empty()) return {};
    // "range" ops also carry value_max
    if (op == "range" && qty.contains("value_max")) {
        const auto& vm = qty["value_max"];
        std::string valMax;
        if (vm.is_number()) {
            char buf[32];
            std::snprintf(buf, sizeof(buf), "%g", vm.get<double>());
            valMax = buf;
        }
        if (!valMax.empty())
            return val + " – " + valMax + (unit.empty() ? "" : " " + unit);
    }
    // Render op symbol
    std::string opSym;
    if (op == "==")       opSym = "=";
    else if (op == ">=")  opSym = "\xe2\x89\xa5";   // UTF-8 ≥
    else if (op == "<=")  opSym = "\xe2\x89\xa4";   // UTF-8 ≤
    else                  opSym = op;

    return opSym + " " + val + (unit.empty() ? "" : " " + unit);
}

// Parse a req_report JSON result into gReqModel.
static void parseReqReport(const nlohmann::json& res) {
    gReqModel.req_rows.clear();
    gReqModel.battery_rows.clear();

    // Requirements list
    const auto& reqs = res.contains("requirements") ? res["requirements"]
                                                     : nlohmann::json::array();
    if (reqs.is_array()) {
        for (const auto& r : reqs) {
            aicad::ui::ReqRow row;
            row.id     = r.value("id",     std::string());
            row.kind   = r.value("kind",   std::string());
            row.status = r.value("status", std::string("proposed"));
            row.text   = r.value("text",   std::string());
            // quantity
            if (r.contains("quantity") && r["quantity"].is_object())
                row.quantity_str = fmtQuantity(r["quantity"]);
            // verify_by list of ref ids
            if (r.contains("verify_by") && r["verify_by"].is_array()) {
                for (const auto& vr : r["verify_by"]) {
                    if (vr.is_object())
                        row.verify_by.push_back(vr.value("id", std::string()));
                    else if (vr.is_string())
                        row.verify_by.push_back(vr.get<std::string>());
                }
            }
            gReqModel.req_rows.push_back(std::move(row));
        }
    }

    // Battery results (nested under "battery" or at top level as "checks")
    const auto* checksPtr = res.contains("battery") && res["battery"].is_object()
                          ? &res["battery"]
                          : &res;
    const auto& checks = checksPtr->contains("checks") ? (*checksPtr)["checks"]
                                                        : nlohmann::json::array();
    if (checks.is_array()) {
        for (const auto& c : checks) {
            aicad::ui::BatteryCheckRow brow;
            // Accept both flat and nested "output_state" shapes (verify.py _make_output)
            if (c.contains("output_state") && c["output_state"].is_object()) {
                const auto& os = c["output_state"];
                brow.check_name  = c.value("check",   c.value("name", std::string("check")));
                brow.passed      = os.value("passed",  false);
                brow.evidence_str = os.value("summary", os.value("evidence", std::string()));
                brow.elapsed_ms   = os.value("elapsed_ms", 0);
            } else {
                brow.check_name   = c.value("check",   c.value("name", std::string("check")));
                brow.passed       = c.value("passed",  false);
                brow.evidence_str = c.value("summary", c.value("evidence", std::string()));
                brow.elapsed_ms   = c.value("elapsed_ms", 0);
            }
            gReqModel.battery_rows.push_back(std::move(brow));
        }
    }
}

// ---- RPC helpers ------------------------------------------------------------

static void fireReqReport(app::AppState& s) {
    gReqModel.state = aicad::ui::ReqPanelState::Loading;
    gReqModel.req_rows.clear();
    gReqModel.battery_rows.clear();
    gReqModel.errorMsg.clear();
    gReqModel.selected_req_id.clear();
    gReqModel.waive_reason.clear();
    gReqScaffoldState.statusText    = "Loading requirements\xe2\x80\xa6";
    gReqScaffoldState.statusIsError = false;

    gReqModel.cacheDocId    = s.docMirror.docId();
    gReqModel.cacheRevision = s.docMirror.revision();

    const std::string docPath = s.savePath;

    s.rpc.call("req_report", {{"doc_path", docPath}}, [](const core::RpcResult& r) {
        if (!r.ok) {
            gReqModel.state             = aicad::ui::ReqPanelState::Error;
            gReqModel.errorMsg          = r.err;
            gReqScaffoldState.statusText    = "Requirements load failed";
            gReqScaffoldState.statusIsError = true;
            return;
        }
        const auto& res = r.result;
        if (!res.value("ok", false)) {
            gReqModel.state    = aicad::ui::ReqPanelState::Error;
            gReqModel.errorMsg = res.value("error", std::string("req_report error"));
            gReqScaffoldState.statusText    = "Requirements error";
            gReqScaffoldState.statusIsError = true;
            return;
        }
        parseReqReport(res);
        gReqModel.state = aicad::ui::ReqPanelState::Ready;
        const int nr = (int)gReqModel.req_rows.size();
        const int nb = (int)gReqModel.battery_rows.size();
        gReqScaffoldState.statusIsError = false;
        gReqScaffoldState.statusText    =
            std::to_string(nr) + " req" + (nr == 1 ? "" : "s") +
            (nb > 0 ? "  \xc2\xb7  " + std::to_string(nb) + " battery check" +
                      (nb == 1 ? "" : "s") : "");
    });
}

// Fire req_status with who="user" to waive a requirement.
// Backend contract: waived requires who="user" (set_status L375).
// The evidence list carries the reason string.
static void fireWaive(app::AppState& s, const std::string& reqId, const std::string& reason) {
    if (reqId.empty() || reason.empty()) return;
    const nlohmann::json evidence = nlohmann::json::array({
        {{"source", "user"}, {"note", reason}}
    });
    s.rpc.call("req_status",
               {{"req_id", reqId},
                {"new_status", "waived"},
                {"who", "user"},
                {"evidence", evidence}},
               [reqId](const core::RpcResult& r) {
        if (!r.ok || !r.result.value("ok", false)) {
            gReqScaffoldState.statusText    = "Waive failed: " +
                (r.ok ? r.result.value("error", std::string("unknown")) : r.err);
            gReqScaffoldState.statusIsError = true;
            return;
        }
        // Update local row immediately so the UI reflects the change.
        for (auto& row : gReqModel.req_rows) {
            if (row.id == reqId) { row.status = "waived"; break; }
        }
        gReqModel.selected_req_id.clear();
        gReqModel.waive_reason.clear();
        gReqScaffoldState.statusText    = reqId + " waived";
        gReqScaffoldState.statusIsError = false;
    });
}

// ---- Status pill colour -----------------------------------------------------

// Returns the theme color token name for a status value.
// Uses TVEC() in draw code; this helper just picks the token name.
static ImVec4 statusColor(const std::string& status) {
    if (status == "verified")       return TVEC(good);
    if (status == "failed")         return TVEC(bad);
    if (status == "user_confirmed") return TVEC(accent);
    // proposed, waived, unknown → muted
    return TVEC(txt2);
}

// ---- Draw: requirements table -----------------------------------------------

static void drawReqTable(app::AppState& s, const std::vector<aicad::ui::ReqRow>& rows) {
    const float S = theme::S();

    constexpr ImGuiTableFlags tflags =
        ImGuiTableFlags_Borders    |
        ImGuiTableFlags_RowBg      |
        ImGuiTableFlags_SizingStretchProp |
        ImGuiTableFlags_ScrollY    |
        ImGuiTableFlags_Resizable;

    // Leave space for battery section below.
    const int  nbat     = (int)gReqModel.battery_rows.size();
    const float batH    = nbat > 0 ? (ImGui::GetTextLineHeightWithSpacing() * (nbat + 2.5f)) : 0.f;
    const float formH   = gReqModel.selected_req_id.empty() ? 0.f
                        : (ImGui::GetTextLineHeightWithSpacing() * 3.5f + 8.f * S);
    const float tableH  = ImGui::GetContentRegionAvail().y - batH - formH - 4.f * S;

    if (!ImGui::BeginTable("##reqs", 4, tflags, ImVec2(0, tableH > 40.f ? tableH : 40.f)))
        return;

    ImGui::TableSetupScrollFreeze(0, 1);
    ImGui::TableSetupColumn("ID",         ImGuiTableColumnFlags_WidthFixed,   80.f * S);
    ImGui::TableSetupColumn("Status",     ImGuiTableColumnFlags_WidthFixed,   96.f * S);
    ImGui::TableSetupColumn("Kind",       ImGuiTableColumnFlags_WidthFixed,   80.f * S);
    ImGui::TableSetupColumn("Requirement",ImGuiTableColumnFlags_WidthStretch, 0.f);
    ImGui::TableHeadersRow();

    for (int i = 0; i < (int)rows.size(); ++i) {
        const auto& row = rows[(size_t)i];
        ImGui::PushID(i);
        ImGui::TableNextRow();

        // ID — selectable for waive form
        ImGui::TableSetColumnIndex(0);
        const bool selected = (gReqModel.selected_req_id == row.id);
        if (ImGui::Selectable(row.id.c_str(), selected,
                              ImGuiSelectableFlags_SpanAllColumns, ImVec2(0,0))) {
            if (selected) {
                gReqModel.selected_req_id.clear();
                gReqModel.waive_reason.clear();
            } else {
                gReqModel.selected_req_id = row.id;
                gReqModel.waive_reason.clear();
            }
        }

        // Status pill
        ImGui::TableSetColumnIndex(1);
        ImGui::TextColored(statusColor(row.status), "%s", row.status.c_str());

        // Kind
        ImGui::TableSetColumnIndex(2);
        ImGui::TextColored(TVEC(txt2), "%s", row.kind.c_str());

        // Requirement text + optional quantity string
        ImGui::TableSetColumnIndex(3);
        ImGui::TextUnformatted(row.text.c_str());
        if (!row.quantity_str.empty()) {
            ImGui::SameLine(0, 6.f * S);
            ImGui::TextColored(TVEC(accent), "[%s]", row.quantity_str.c_str());
        }
        // Verify-by tooltip
        if (!row.verify_by.empty() && ImGui::IsItemHovered()) {
            std::string tip = "Verify by: ";
            for (size_t j = 0; j < row.verify_by.size(); ++j) {
                if (j) tip += ", ";
                tip += row.verify_by[j];
            }
            ImGui::SetTooltip("%s", tip.c_str());
        }

        ImGui::PopID();
    }

    ImGui::EndTable();

    // ---- Waive form (shown when a row is selected and status allows it) -----
    if (!gReqModel.selected_req_id.empty()) {
        // Find the selected row's status.
        std::string selStatus;
        for (const auto& row : gReqModel.req_rows)
            if (row.id == gReqModel.selected_req_id) { selStatus = row.status; break; }

        // Only offer Waive for proposed / failed / user_confirmed rows;
        // verified and waived are terminal for the waive path.
        const bool canWaive = (selStatus == "proposed" || selStatus == "failed"
                             || selStatus == "user_confirmed");
        if (canWaive) {
            ImGui::Dummy(ImVec2(0, 4.f * S));
            ImGui::Separator();
            ImGui::TextColored(TVEC(txt2), "Waive  %s:", gReqModel.selected_req_id.c_str());
            ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x - 110.f * S);
            char buf[256] = {};
            std::snprintf(buf, sizeof(buf), "%s", gReqModel.waive_reason.c_str());
            if (ImGui::InputText("##waive_reason", buf, sizeof(buf),
                                 ImGuiInputTextFlags_EnterReturnsTrue)) {
                gReqModel.waive_reason = buf;
            } else {
                gReqModel.waive_reason = buf;
            }
            ImGui::SameLine(0, 6.f * S);
            const bool canSubmit = !gReqModel.waive_reason.empty();
            if (!canSubmit) ImGui::BeginDisabled();
            if (ImGui::Button("Waive", ImVec2(96.f * S, 0.f)))
                fireWaive(s, gReqModel.selected_req_id, gReqModel.waive_reason);
            if (!canSubmit) {
                ImGui::EndDisabled();
                if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                    ImGui::SetTooltip("Enter a reason to enable the Waive action");
            }
        }
    }

    // ---- Battery results section --------------------------------------------
    if (nbat > 0) {
        ImGui::Dummy(ImVec2(0, 4.f * S));
        ImGui::Separator();
        ImGui::TextColored(TVEC(txt1), "Verification battery");
        ImGui::Dummy(ImVec2(0, 2.f * S));

        for (const auto& brow : gReqModel.battery_rows) {
            // Pass/fail indicator using theme colour only.
            if (brow.passed) {
                ImGui::TextColored(TVEC(good), "\xe2\x9c\x93");  // UTF-8 checkmark ✓
            } else {
                ImGui::TextColored(TVEC(bad), "\xe2\x9c\x97"); // UTF-8 cross ✗
            }
            ImGui::SameLine(0, 6.f * S);
            ImGui::TextUnformatted(brow.check_name.c_str());
            if (!brow.evidence_str.empty()) {
                ImGui::SameLine(0, 8.f * S);
                ImGui::TextColored(TVEC(txt2), "%s", brow.evidence_str.c_str());
            }
            if (brow.elapsed_ms > 0 && ImGui::IsItemHovered()) {
                ImGui::SetTooltip("%d ms", brow.elapsed_ms);
            }
        }
    }
}

// ---- Top-level draw ---------------------------------------------------------

static void drawContent(app::AppState& s) {
    switch (gReqModel.state) {
    case aicad::ui::ReqPanelState::Idle:
        drawPanelEmptyState("No document open", "open a part or assembly to view its requirements");
        break;
    case aicad::ui::ReqPanelState::Loading:
        drawPanelEmptyState("Loading requirements\xe2\x80\xa6");
        break;
    case aicad::ui::ReqPanelState::Error:
        drawPanelEmptyState("Requirements unavailable", gReqModel.errorMsg.c_str());
        break;
    case aicad::ui::ReqPanelState::Ready: {
        const auto filtered = aicad::ui::filterReqRows(gReqModel.req_rows,
                                                        gReqScaffoldState.searchText);
        if (filtered.empty() && gReqModel.req_rows.empty()) {
            drawPanelEmptyState("No requirements", "use req_create to add requirements");
        } else if (filtered.empty()) {
            drawPanelEmptyState("No matching requirements", "try a different search term");
        } else {
            drawReqTable(s, filtered);
        }
        break;
    }
    }
}

} // namespace

// ---- Public API -------------------------------------------------------------

void drawRequirementsPanel(app::AppState& s) {
    // Cache-invalidate when doc changes.
    const std::string curDocId = s.docMirror.docId();
    const int         curRev   = s.docMirror.revision();
    const bool cacheStale =
        (gReqModel.state != aicad::ui::ReqPanelState::Idle &&
         gReqModel.state != aicad::ui::ReqPanelState::Loading) &&
        (curDocId != gReqModel.cacheDocId || curRev != gReqModel.cacheRevision);

    // Auto-load on first draw (if worker ready) or on doc change.
    const bool shouldLoad =
        s.workerStarted &&
        (gReqModel.state == aicad::ui::ReqPanelState::Idle || cacheStale);

    if (shouldLoad) fireReqReport(s);

    ui::PanelScaffoldSpec spec;
    spec.id         = "requirements";
    spec.title      = "Requirements";
    spec.icon       = "requirements";
    spec.hasSearch  = true;
    spec.searchHint = "Filter requirements\xe2\x80\xa6";

    if (gReqScaffoldState.statusText.empty())
        gReqScaffoldState.statusText = "Open a document to view requirements";

    drawPanelScaffold(spec, gReqScaffoldState, [&]() {
        drawContent(s);
    });
}

void openRequirementsPanel() { gReqOpen = true; }

bool* requirementsPanelOpen() { return &gReqOpen; }

void seedRequirementsPanelFromJson(const std::string& json_result) {
    // Called from scenarios.cpp tickSimpleShots to pre-populate the model for headless shots.
    try {
        const auto res = nlohmann::json::parse(json_result);
        if (!res.value("ok", false)) return;
        parseReqReport(res);
        gReqModel.state = aicad::ui::ReqPanelState::Ready;
        const int nr = (int)gReqModel.req_rows.size();
        const int nb = (int)gReqModel.battery_rows.size();
        gReqScaffoldState.statusIsError = false;
        gReqScaffoldState.statusText    =
            std::to_string(nr) + " req" + (nr == 1 ? "" : "s") +
            (nb > 0 ? "  \xc2\xb7  " + std::to_string(nb) + " battery check" +
                      (nb == 1 ? "" : "s") : "");
    } catch (...) {}  // malformed JSON → leave model as-is
}

} // namespace ui
