// T-U4 BOM & sourcing panel — see bom_panel.h.
//
// Architecture (pattern library §0/§1):
//   - View-model: aicad::ui::BomPanelModel (ui/abstract/bom_view.h, no ImGui/GL includes)
//   - Scaffold: ui::drawPanelScaffold / ui::drawPanelEmptyState (T-U1 kit)
//   - Worker RPC: s.rpc.call("bom_price" / "request_quote" / "match_part_number") via AsyncRpc
//   - State machine: BomPanelState enum drives typed placeholder vocab (pattern §4)
//   - Table rows: ImGui table with provenance-safe blanks for unpriced cells (BOM_UNPRICED "—")
//   - Quote-out: fires request_quote; status lands in the panel footer; keyless mode degrades
//   - Match review: accept writes match to doc via applyOps (R1 editable path)
//
// Price contract (aicad.sourcing/1 §0):
//   A cell with an unknown price renders the em-dash sentinel "—" and a tooltip "price not
//   available".  A fabricated price is NEVER shown under any circumstance.
#include "bom_panel.h"
#include "panel_scaffold.h"
#include "bom_view.h"   // ui/abstract/ (no ImGui includes there)

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

// ---- Panel state ----------------------------------------------------------------
bool gBomOpen = false;
ui::PanelScaffoldState gBomScaffoldState;
aicad::ui::BomPanelModel gBomModel;

// ---- Formatting helpers ---------------------------------------------------------

// Format a nullable float from JSON as a dollar-string or the UNPRICED sentinel.
// The JSON value is either a number or null (contract aicad.sourcing/1 §3).
static std::string fmtCost(const nlohmann::json& j) {
    if (j.is_null() || j.is_discarded()) return aicad::ui::BOM_UNPRICED;
    if (j.is_number()) {
        char buf[32];
        std::snprintf(buf, sizeof(buf), "$%.2f", j.get<double>());
        return buf;
    }
    return aicad::ui::BOM_UNPRICED;
}

// Recompute the footer total (sum of ext_costs that are NOT the sentinel).
static void recomputeTotal() {
    double total = 0.0;
    bool   anyPriced = false;
    for (const auto& r : gBomModel.rows) {
        if (r.ext_cost_str != aicad::ui::BOM_UNPRICED && !r.ext_cost_str.empty() &&
            r.ext_cost_str[0] == '$') {
            try { total += std::stod(r.ext_cost_str.substr(1)); anyPriced = true; } catch (...) {}
        }
    }
    if (anyPriced) {
        char buf[32];
        std::snprintf(buf, sizeof(buf), "$%.2f", total);
        gBomModel.totalStr = buf;
    } else {
        gBomModel.totalStr = aicad::ui::BOM_UNPRICED;
    }
}

// Parse a JSON BomLine array into gBomModel.rows.
static void parseBomLines(const nlohmann::json& bom) {
    gBomModel.rows.clear();
    if (!bom.is_array()) return;
    for (const auto& line : bom) {
        aicad::ui::BomRow row;
        row.line_id     = line.value("line_id",     std::string());
        row.kind        = line.value("kind",        std::string("bought"));
        row.quantity    = line.value("quantity",    0);
        row.unit        = line.value("unit",        std::string("ea"));
        row.description = line.value("description", std::string());
        row.mpn         = line.value("mpn",         std::string());
        // Resolve supplier from the vendor_part sub-object if present.
        if (line.contains("vendor_part") && line["vendor_part"].is_object()) {
            row.supplier = line["vendor_part"].value("supplier", std::string());
        }
        row.unit_cost_str = fmtCost(line.contains("unit_cost") ? line["unit_cost"] : nlohmann::json{});
        row.ext_cost_str  = fmtCost(line.contains("ext_cost")  ? line["ext_cost"]  : nlohmann::json{});
        gBomModel.rows.push_back(std::move(row));
    }
    recomputeTotal();
}

// ---- RPC helpers ----------------------------------------------------------------

static void fireBomPrice(app::AppState& s) {
    gBomModel.state    = aicad::ui::BomPanelState::Loading;
    gBomModel.rows.clear();
    gBomModel.totalStr.clear();
    gBomModel.quoteStatus = aicad::ui::QuoteStatus::Idle;
    gBomModel.quoteMsg.clear();
    gBomScaffoldState.statusText    = "Loading BOM…";
    gBomScaffoldState.statusIsError = false;

    // Cache the doc id + revision so we can detect staleness.
    gBomModel.cacheDocId      = s.docMirror.docId();
    gBomModel.cacheRevision   = s.docMirror.revision();

    // Use the active doc save path; fall back to empty string (engine errors gracefully).
    std::string docPath = s.savePath;

    s.rpc.call("bom_price", {{"doc_path", docPath}}, [](const core::RpcResult& r) {
        if (!r.ok) {
            gBomModel.state             = aicad::ui::BomPanelState::Error;
            gBomModel.errorMsg          = r.err;
            gBomScaffoldState.statusText    = "BOM load failed";
            gBomScaffoldState.statusIsError = true;
            return;
        }
        const auto& res = r.result;
        if (!res.value("ok", false)) {
            gBomModel.state             = aicad::ui::BomPanelState::Error;
            gBomModel.errorMsg          = res.value("error", std::string("bom_price error"));
            gBomScaffoldState.statusText    = "BOM error";
            gBomScaffoldState.statusIsError = true;
            return;
        }
        parseBomLines(res.contains("bom") ? res["bom"] : nlohmann::json::array());
        gBomModel.state = aicad::ui::BomPanelState::Ready;
        const int n = (int)gBomModel.rows.size();
        gBomScaffoldState.statusIsError = false;
        gBomScaffoldState.statusText    = std::to_string(n) + " line" + (n == 1 ? "" : "s") +
                                          (gBomModel.totalStr == aicad::ui::BOM_UNPRICED
                                               ? " (prices unavailable)"
                                               : "  total " + gBomModel.totalStr);
    });
}

static void fireRequestQuote(app::AppState& s, const std::string& vendor) {
    gBomModel.quoteStatus  = aicad::ui::QuoteStatus::Pending;
    gBomModel.quoteVendor  = vendor;
    gBomModel.quoteMsg     = "Requesting quote from " + vendor + "…";
    gBomScaffoldState.statusText    = gBomModel.quoteMsg;
    gBomScaffoldState.statusIsError = false;

    std::string docPath = s.savePath;

    s.rpc.call("request_quote",
               {{"doc_path", docPath}, {"vendor", vendor}, {"quantity", 1}},
               [vendor](const core::RpcResult& r) {
        if (!r.ok) {
            gBomModel.quoteStatus = aicad::ui::QuoteStatus::Error;
            gBomModel.quoteMsg    = "Quote request failed: " + r.err;
            gBomScaffoldState.statusText    = gBomModel.quoteMsg;
            gBomScaffoldState.statusIsError = true;
            return;
        }
        const auto& res = r.result;
        if (!res.value("ok", false)) {
            // Keyless or vendor down — degrade gracefully (no fabricated price).
            gBomModel.quoteStatus = aicad::ui::QuoteStatus::Error;
            gBomModel.quoteMsg    = res.value("reason",
                                    res.value("hint",
                                    std::string("Quote unavailable — configure vendor API key")));
            gBomScaffoldState.statusText    = gBomModel.quoteMsg;
            gBomScaffoldState.statusIsError = true;
            return;
        }
        gBomModel.quoteStatus = aicad::ui::QuoteStatus::Ok;
        // Quote arrived — show a concise summary if the response carries a total.
        const auto& q = res.contains("quote") ? res["quote"] : res;
        std::string totalStr;
        if (q.contains("total") && !q["total"].is_null()) {
            char buf[32];
            std::snprintf(buf, sizeof(buf), "$%.2f", q["total"].get<double>());
            totalStr = std::string(buf);
        }
        gBomModel.quoteMsg = vendor + " quote" + (totalStr.empty() ? " received" : ": " + totalStr);
        gBomScaffoldState.statusText    = gBomModel.quoteMsg;
        gBomScaffoldState.statusIsError = false;
    });
}

static void fireMatchPartNumber(app::AppState& s, int rowIdx) {
    if (rowIdx < 0 || rowIdx >= (int)gBomModel.rows.size()) return;
    auto& row = gBomModel.rows[(size_t)rowIdx];
    if (row.mpn.empty()) return;
    row.matchPending  = true;
    row.matchReady    = false;
    row.matchAccepted = false;
    row.matchMpn.clear();
    row.matchSupplier.clear();
    row.matchDetail.clear();

    const std::string mpn = row.mpn;
    const size_t idx = (size_t)rowIdx;

    s.rpc.call("match_part_number", {{"mpn", mpn}, {"generate", true}},
               [idx](const core::RpcResult& r) {
        if (idx >= gBomModel.rows.size()) return;
        auto& row = gBomModel.rows[idx];
        row.matchPending = false;
        if (!r.ok || !r.result.value("ok", false)) {
            row.matchDetail = r.ok ? r.result.value("error", std::string("match failed")) : r.err;
            return;
        }
        const auto& m = r.result.contains("match") ? r.result["match"] : r.result;
        row.matchMpn      = m.value("mpn",      std::string());
        row.matchSupplier = m.value("supplier",  std::string());
        row.matchDetail   = m.value("detail",    m.value("description", std::string()));
        row.matchReady    = true;
    });
}

// Accept a match: write the matched MPN + supplier back to the doc via applyOps (R1 editable).
static void acceptMatch(app::AppState& s, int rowIdx) {
    if (rowIdx < 0 || rowIdx >= (int)gBomModel.rows.size()) return;
    auto& row = gBomModel.rows[(size_t)rowIdx];
    if (!row.matchReady) return;

    // applyOps: set_part_attr on the line's part_ref with the matched MPN + supplier.
    // This is the R1 editable path — the engine writes it to the doc so it round-trips.
    nlohmann::json ops = nlohmann::json::array({
        {{"op", "set_part_attr"},
         {"line_id", row.line_id},
         {"mpn",      row.matchMpn},
         {"supplier", row.matchSupplier}}
    });
    (void)s.docMirror.applyOps(s.worker, ops);

    // Update the local row immediately so the UI reflects the accepted match.
    row.mpn           = row.matchMpn;
    row.supplier      = row.matchSupplier;
    row.matchAccepted = true;
    row.matchReady    = false;
    gBomScaffoldState.statusText    = "Match accepted — doc updated";
    gBomScaffoldState.statusIsError = false;
}

// ---- Draw: table ----------------------------------------------------------------

static void drawBomTable(app::AppState& s, const std::vector<aicad::ui::BomRow>& rows) {
    const float S = theme::S();

    // Six columns: Qty | Part / Description | MPN | Supplier | Unit | Ext
    constexpr ImGuiTableFlags tflags =
        ImGuiTableFlags_Borders    |
        ImGuiTableFlags_RowBg      |
        ImGuiTableFlags_SizingStretchProp |
        ImGuiTableFlags_ScrollY    |
        ImGuiTableFlags_Resizable;

    const float footerH = ImGui::GetTextLineHeightWithSpacing() * 2.5f;
    const float tableH  = ImGui::GetContentRegionAvail().y - footerH;

    if (!ImGui::BeginTable("##bom", 6, tflags, ImVec2(0, tableH))) return;

    ImGui::TableSetupScrollFreeze(0, 1);
    ImGui::TableSetupColumn("Qty",         ImGuiTableColumnFlags_WidthFixed,        44.f * S);
    ImGui::TableSetupColumn("Description", ImGuiTableColumnFlags_WidthStretch,      0.40f);
    ImGui::TableSetupColumn("MPN",         ImGuiTableColumnFlags_WidthStretch,      0.20f);
    ImGui::TableSetupColumn("Supplier",    ImGuiTableColumnFlags_WidthStretch,      0.15f);
    ImGui::TableSetupColumn("Unit",        ImGuiTableColumnFlags_WidthFixed,        68.f * S);
    ImGui::TableSetupColumn("Ext",         ImGuiTableColumnFlags_WidthFixed,        72.f * S);
    ImGui::TableHeadersRow();

    for (int i = 0; i < (int)rows.size(); ++i) {
        const auto& row = rows[(size_t)i];
        ImGui::PushID(i);
        ImGui::TableNextRow();

        // Qty
        ImGui::TableSetColumnIndex(0);
        ImGui::Text("%d", row.quantity);

        // Description (+ kind badge for made parts)
        ImGui::TableSetColumnIndex(1);
        if (row.kind == "made") {
            ImGui::TextColored(TVEC(accent), "[MADE] ");
            ImGui::SameLine(0, 2.f * S);
        }
        ImGui::TextUnformatted(row.description.c_str());

        // MPN + optional match-review button
        ImGui::TableSetColumnIndex(2);
        if (!row.mpn.empty()) {
            ImGui::TextUnformatted(row.mpn.c_str());

            // Show "Match" button for bought lines with an MPN but no accepted match yet.
            if (row.kind == "bought" && !row.matchAccepted && !row.matchPending) {
                ImGui::SameLine(0, 4.f * S);
                if (ImGui::SmallButton("Match")) fireMatchPartNumber(s, i);
            }
            if (row.matchPending) {
                ImGui::SameLine(0, 4.f * S);
                ImGui::TextColored(TVEC(txt2), "(…)");
            }

            // Match review row: accept / reject
            if (row.matchReady && !row.matchAccepted) {
                ImGui::TableNextRow();
                ImGui::TableSetColumnIndex(1);
                ImGui::TextColored(TVEC(accent), "  Suggest:");
                ImGui::SameLine();
                ImGui::TextUnformatted(row.matchMpn.c_str());
                if (!row.matchSupplier.empty()) {
                    ImGui::SameLine(0, 4.f * S);
                    ImGui::TextColored(TVEC(txt2), "@ %s", row.matchSupplier.c_str());
                }
                ImGui::SameLine(0, 8.f * S);
                if (ImGui::SmallButton("Accept")) acceptMatch(s, i);
                ImGui::SameLine(0, 4.f * S);
                if (ImGui::SmallButton("Reject")) {
                    gBomModel.rows[(size_t)i].matchReady = false;
                }
                if (!row.matchDetail.empty()) {
                    if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", row.matchDetail.c_str());
                }
            }
        } else {
            ImGui::TextColored(TVEC(txt2), "\xe2\x80\x94");  // em-dash: no MPN
        }

        // Supplier
        ImGui::TableSetColumnIndex(3);
        if (!row.supplier.empty())
            ImGui::TextUnformatted(row.supplier.c_str());
        else
            ImGui::TextColored(TVEC(txt2), "\xe2\x80\x94");

        // Unit price — sentinel if unpriced
        ImGui::TableSetColumnIndex(4);
        if (row.unit_cost_str == aicad::ui::BOM_UNPRICED) {
            ImGui::TextColored(TVEC(txt2), "%s", aicad::ui::BOM_UNPRICED);
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Price not available — configure sourcing provider");
        } else {
            ImGui::TextUnformatted(row.unit_cost_str.c_str());
        }

        // Extended price — sentinel if unpriced
        ImGui::TableSetColumnIndex(5);
        if (row.ext_cost_str == aicad::ui::BOM_UNPRICED) {
            ImGui::TextColored(TVEC(txt2), "%s", aicad::ui::BOM_UNPRICED);
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Price not available — configure sourcing provider");
        } else {
            ImGui::TextUnformatted(row.ext_cost_str.c_str());
        }

        ImGui::PopID();
    }

    ImGui::EndTable();

    // Footer: total + Quote-out button
    ImGui::Dummy(ImVec2(0, 2.f * S));
    ImGui::Separator();
    ImGui::Dummy(ImVec2(0, 2.f * S));

    // Total line
    ImGui::TextColored(TVEC(txt1), "Total:");
    ImGui::SameLine(0, 6.f * S);
    if (gBomModel.totalStr == aicad::ui::BOM_UNPRICED || gBomModel.totalStr.empty()) {
        ImGui::TextColored(TVEC(txt2), "%s", aicad::ui::BOM_UNPRICED);
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Total unavailable — one or more lines are unpriced");
    } else {
        ImGui::TextUnformatted(gBomModel.totalStr.c_str());
    }

    // Quote-out button (right-aligned; fires request_quote for Xometry as default).
    const bool quoteInFlight = (gBomModel.quoteStatus == aicad::ui::QuoteStatus::Pending);
    ImGui::SameLine();
    if (quoteInFlight) ImGui::BeginDisabled();
    // Align button to the right edge.
    const float btnW = 110.f * S;
    float rightX = ImGui::GetContentRegionAvail().x - btnW;
    if (rightX > 0.f) ImGui::SetCursorPosX(ImGui::GetCursorPosX() + rightX);
    if (ImGui::Button("Request Quote…", ImVec2(btnW, 0.f))) {
        // Fire for the first available vendor; a follow-up pass could show a vendor picker.
        fireRequestQuote(s, "xometry");
    }
    if (quoteInFlight) ImGui::EndDisabled();
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Submit this BOM to Xometry for a fabrication quote (requires API key)");
}

// ---- Top-level draw -------------------------------------------------------------

static void drawContent(app::AppState& s) {
    switch (gBomModel.state) {
    case aicad::ui::BomPanelState::Idle:
        drawPanelEmptyState("No document open", "open a part or assembly to view its BOM");
        break;
    case aicad::ui::BomPanelState::Loading:
        drawPanelEmptyState("Loading BOM…");
        break;
    case aicad::ui::BomPanelState::Error:
        drawPanelEmptyState("BOM unavailable", gBomModel.errorMsg.c_str());
        break;
    case aicad::ui::BomPanelState::Ready: {
        const auto filtered = aicad::ui::filterBomRows(gBomModel.rows,
                                                        gBomScaffoldState.searchText);
        if (filtered.empty()) {
            drawPanelEmptyState("No matching lines", "try a different search term");
        } else {
            drawBomTable(s, filtered);
        }
        break;
    }
    }
}

}  // namespace

// ---- Public API -----------------------------------------------------------------

void drawBomPanel(app::AppState& s) {
    // Cache-invalidate when doc changes.
    const std::string curDocId  = s.docMirror.docId();
    const int         curRev    = s.docMirror.revision();
    const bool cacheStale =
        (gBomModel.state != aicad::ui::BomPanelState::Idle &&
         gBomModel.state != aicad::ui::BomPanelState::Loading) &&
        (curDocId != gBomModel.cacheDocId || curRev != gBomModel.cacheRevision);

    // Auto-load on first draw (if worker ready) or on doc change.
    const bool shouldLoad =
        s.workerStarted &&
        (gBomModel.state == aicad::ui::BomPanelState::Idle || cacheStale);

    if (shouldLoad) fireBomPrice(s);

    ui::PanelScaffoldSpec spec;
    spec.id         = "bom";
    spec.title      = "BOM & Sourcing";
    spec.icon       = "bom";
    spec.hasSearch  = true;
    spec.searchHint = "Filter parts…";

    if (gBomScaffoldState.statusText.empty())
        gBomScaffoldState.statusText = "Open a document to load its BOM";

    drawPanelScaffold(spec, gBomScaffoldState, [&]() {
        drawContent(s);
    });
}

void openBomPanel() { gBomOpen = true; }

bool* bomPanelOpen() { return &gBomOpen; }

void seedBomPanelFromJson(const std::string& json_result) {
    // Called from scenarios.cpp tickSimpleShots to pre-populate the model for headless shots.
    try {
        const auto res = nlohmann::json::parse(json_result);
        if (!res.value("ok", false)) return;
        parseBomLines(res.contains("bom") ? res["bom"] : nlohmann::json::array());
        gBomModel.state = aicad::ui::BomPanelState::Ready;
        const int n = (int)gBomModel.rows.size();
        gBomScaffoldState.statusIsError = false;
        gBomScaffoldState.statusText    = std::to_string(n) + " line" + (n == 1 ? "" : "s") +
                                          (gBomModel.totalStr == aicad::ui::BOM_UNPRICED
                                               ? " (prices unavailable)"
                                               : "  total " + gBomModel.totalStr);
    } catch (...) {}  // malformed JSON → leave model as-is
}

}  // namespace ui
