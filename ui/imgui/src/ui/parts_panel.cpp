// T-U2 Parts Browser panel — see parts_panel.h.
//
// Architecture (pattern library §0/§1):
//   - View-model: aicad::ui::PartsPanelModel (ui/abstract/parts_view.h, no ImGui/GL includes)
//   - Scaffold: ui::drawPanelScaffold / ui::drawPanelEmptyState (T-U1 kit)
//   - Worker RPC: s.rpc.call("part_search" / "part_get" / "part_place") via AsyncRpc
//   - State machine: PartsPanelState enum drives typed placeholder vocab (pattern §4)
//   - Row pills: theme::glassRowBackground (pattern §2)
//   - Domain filter chips: compact toggleable SmallButtons in the search row area

#include "parts_panel.h"
#include "panel_scaffold.h"
#include "parts_view.h"   // ui/abstract/ (added to include path in CMakeLists — no ImGui includes)

#include "app/app_state.h"

#include "theme/theme.h"
#include "theme/tokens.h"

#include "imgui.h"
#include <nlohmann/json.hpp>

#include <string>
#include <vector>

namespace ui {

namespace {

// ---- Panel state -----------------------------------------------------------------
bool gOpen = false;
ui::PanelScaffoldState gScaffoldState;
aicad::ui::PartsPanelModel gModel;

// Debounce: track the last query string we fired so we don't re-fire on every
// frame (searchChanged fires once per keystroke; this guard ensures idempotence).
std::string gLastQuery;
std::string gLastDomain;

// Ordered domain filter chip list (blank = All).
struct DomainChip { const char* label; const char* domain; };
static const DomainChip kDomainChips[] = {
    {"All",   ""},
    {"Fastener", "fastener"},
    {"Bearing",  "bearing"},
    {"Mech",     "mech_family"},
    {"Section",  "section"},
    {"Fitting",  "fitting"},
    {"Hydraulic","hydraulic"},
    {"ECAD",     "ecad"},
};

// ---- RPC helpers -----------------------------------------------------------------

void fireSearch(app::AppState& s, const std::string& query, const std::string& domain) {
    gLastQuery  = query;
    gLastDomain = domain;
    gModel.state    = aicad::ui::PartsPanelState::Loading;
    gModel.results.clear();
    gModel.selectedIdx  = -1;
    gModel.detailState  = aicad::ui::PartsPanelState::Idle;
    gModel.insertState  = aicad::ui::PartsPanelState::Idle;

    nlohmann::json params = {{"query", query}, {"limit", 40}};
    if (!domain.empty()) params["domain"] = domain;

    s.rpc.call("part_search", params, [](const core::RpcResult& r) {
        if (!r.ok) {
            gModel.state    = aicad::ui::PartsPanelState::Error;
            gModel.errorMsg = r.err;
            gScaffoldState.statusText    = "Search failed";
            gScaffoldState.statusIsError = true;
            return;
        }
        const auto& res = r.result;
        if (!res.value("ok", false)) {
            gModel.state    = aicad::ui::PartsPanelState::Error;
            gModel.errorMsg = res.value("reason", std::string("search error"));
            gScaffoldState.statusText    = "Search failed";
            gScaffoldState.statusIsError = true;
            return;
        }
        gModel.state    = aicad::ui::PartsPanelState::Ready;
        gModel.total    = res.value("count", 0);
        gModel.has_live = res.value("has_live", false);
        gModel.results.clear();
        if (res.contains("results") && res["results"].is_array()) {
            for (const auto& rec : res["results"]) {
                aicad::ui::PartsResultRow row;
                row.part_id     = rec.value("part_id",     std::string());
                row.domain      = rec.value("domain",      std::string());
                row.title       = rec.value("title",       std::string());
                row.standard    = rec.value("standard",    std::string());
                row.designation = rec.value("designation", std::string());
                row.source_live = (rec.value("source", std::string()) == "live");
                // Price hint from first sourcing entry with a price.
                if (rec.contains("sourcing") && rec["sourcing"].is_array()) {
                    for (const auto& src : rec["sourcing"]) {
                        std::string price = src.value("price", std::string());
                        if (!price.empty()) { row.price_hint = price; break; }
                    }
                }
                gModel.results.push_back(std::move(row));
            }
        }
        gScaffoldState.statusIsError = false;
        if (gModel.results.empty()) {
            gScaffoldState.statusText = "No results";
        } else {
            gScaffoldState.statusText = std::to_string(gModel.total) + " result" +
                                        (gModel.total == 1 ? "" : "s") +
                                        (gModel.has_live ? " (incl. live)" : "");
        }
    });
}

void firePartGet(app::AppState& s, const std::string& part_id) {
    gModel.detailState   = aicad::ui::PartsPanelState::DetailLoading;
    gModel.detail        = aicad::ui::PartsDetailCard{};
    gModel.insertState   = aicad::ui::PartsPanelState::Idle;

    s.rpc.call("part_get", {{"part_id", part_id}}, [](const core::RpcResult& r) {
        if (!r.ok) {
            gModel.detailState    = aicad::ui::PartsPanelState::DetailError;
            gModel.detailErrorMsg = r.err;
            return;
        }
        const auto& res = r.result;
        if (!res.value("ok", false)) {
            gModel.detailState    = aicad::ui::PartsPanelState::DetailError;
            gModel.detailErrorMsg = res.value("reason", std::string("not found"));
            return;
        }
        const auto& p = res["part"];
        auto& d = gModel.detail;
        d.part_id     = p.value("part_id",     std::string());
        d.domain      = p.value("domain",      std::string());
        d.title       = p.value("title",       std::string());
        d.standard    = p.value("standard",    std::string());
        d.designation = p.value("designation", std::string());
        d.provenance  = p.value("provenance",  std::string());
        d.license     = p.value("license",     std::string());
        d.has_geometry = p.contains("geometry_ref") && !p["geometry_ref"].is_null();

        d.params.clear();
        if (p.contains("params") && p["params"].is_object()) {
            for (auto it = p["params"].begin(); it != p["params"].end(); ++it) {
                std::string val = it->is_string() ? it->get<std::string>() : it->dump();
                d.params.emplace_back(it.key(), val);
            }
        }
        d.sourcing.clear();
        if (p.contains("sourcing") && p["sourcing"].is_array()) {
            for (const auto& src : p["sourcing"]) {
                aicad::ui::PartsSourcingRef ref;
                ref.supplier = src.value("supplier", std::string());
                ref.url      = src.value("url",      std::string());
                ref.price    = src.value("price",    std::string());
                d.sourcing.push_back(std::move(ref));
            }
        }
        gModel.detailState = aicad::ui::PartsPanelState::DetailReady;
    });
}

void firePartPlace(app::AppState& s, const std::string& part_id) {
    gModel.insertState    = aicad::ui::PartsPanelState::Inserting;
    gModel.insertedDocId  = {};
    gModel.insertErrorMsg = {};
    gScaffoldState.statusText    = "Inserting…";
    gScaffoldState.statusIsError = false;

    s.rpc.call("part_place", {{"part_id", part_id}}, [](const core::RpcResult& r) {
        if (!r.ok) {
            gModel.insertState    = aicad::ui::PartsPanelState::InsertError;
            gModel.insertErrorMsg = r.err;
            gScaffoldState.statusText    = "Insert failed";
            gScaffoldState.statusIsError = true;
            return;
        }
        const auto& res = r.result;
        if (!res.value("ok", false)) {
            gModel.insertState    = aicad::ui::PartsPanelState::InsertError;
            gModel.insertErrorMsg = res.value("reason", std::string("insert failed"));
            gScaffoldState.statusText    = "Insert failed";
            gScaffoldState.statusIsError = true;
            return;
        }
        gModel.insertState   = aicad::ui::PartsPanelState::InsertOk;
        gModel.insertedDocId = res.value("doc_id", std::string());
        gScaffoldState.statusText    = "Inserted — editable part doc created";
        gScaffoldState.statusIsError = false;
    });
}

// ---- Draw helpers ----------------------------------------------------------------

// Domain filter chip row.
void drawDomainChips(app::AppState& s, const std::string& currentQuery) {
    const float S = theme::S();
    ImGui::Dummy(ImVec2(0, 2.f * S));
    for (int i = 0; i < (int)(sizeof(kDomainChips) / sizeof(kDomainChips[0])); ++i) {
        const auto& chip = kDomainChips[i];
        const bool active = (gModel.domainFilter == chip.domain);
        if (active) {
            ImGui::PushStyleColor(ImGuiCol_Button,        TVEC(accent));
            ImGui::PushStyleColor(ImGuiCol_ButtonHovered, TVEC(accent));
            ImGui::PushStyleColor(ImGuiCol_ButtonActive,  TVEC(accent));
        }
        if (ImGui::SmallButton(chip.label)) {
            if (!active) {
                gModel.domainFilter = chip.domain;
                // Re-fire search with new domain filter.
                fireSearch(s, currentQuery, gModel.domainFilter);
            }
        }
        if (active) ImGui::PopStyleColor(3);
        if (i < (int)(sizeof(kDomainChips) / sizeof(kDomainChips[0])) - 1)
            ImGui::SameLine(0, 4.f * S);
    }
    ImGui::Dummy(ImVec2(0, 2.f * S));
}

// Result row list.
void drawResultRows(app::AppState& s) {
    const float S = theme::S();

    if (gModel.results.empty()) {
        if (gModel.state == aicad::ui::PartsPanelState::Loading) {
            drawPanelEmptyState("Searching…");
        } else {
            drawPanelEmptyState("No results", "try a different search term or domain");
        }
        return;
    }

    for (int i = 0; i < (int)gModel.results.size(); ++i) {
        const auto& row = gModel.results[i];
        const bool selected = (gModel.selectedIdx == i);

        ImGui::PushID(i);

        // Row pill background (pattern library §2 — background before content).
        const ImVec2 rowMn = ImGui::GetCursorScreenPos();
        const float  rowH  = ImGui::GetTextLineHeightWithSpacing() * 2.f + 6.f * S;
        const ImVec2 rowMx(rowMn.x + ImGui::GetContentRegionAvail().x, rowMn.y + rowH);
        theme::glassRowBackground(ImGui::GetWindowDrawList(), rowMn, rowMx,
                                   4.f * S, i, selected);

        // Invisible button over the row for selection.
        ImGui::SetCursorScreenPos(rowMn);
        if (ImGui::InvisibleButton("##row", ImVec2(rowMx.x - rowMn.x, rowH))) {
            if (gModel.selectedIdx != i) {
                gModel.selectedIdx = i;
                firePartGet(s, row.part_id);
            }
        }
        ImGui::SetCursorScreenPos(ImVec2(rowMn.x + 8.f * S, rowMn.y + 3.f * S));

        // Domain badge (colored accent label).
        ImGui::TextColored(TVEC(accent), "[%s]", aicad::ui::partsDomainLabel(row.domain));
        ImGui::SameLine();

        // Title.
        ImGui::TextUnformatted(row.title.c_str());

        // Second line: standard + designation + live marker + price.
        ImGui::SetCursorScreenPos(ImVec2(rowMn.x + 8.f * S, rowMn.y + ImGui::GetTextLineHeightWithSpacing() + 3.f * S));
        std::string detail;
        if (!row.standard.empty())    detail += row.standard;
        if (!row.designation.empty()) { if (!detail.empty()) detail += " · "; detail += row.designation; }
        if (row.source_live)          { if (!detail.empty()) detail += " · "; detail += "LIVE"; }
        if (!row.price_hint.empty())  detail += " " + row.price_hint;
        if (detail.empty())           detail = row.part_id;
        ImGui::TextColored(TVEC(txt2), "%s", detail.c_str());

        // Advance cursor past the row rect.
        ImGui::SetCursorScreenPos(ImVec2(rowMn.x, rowMx.y + 2.f * S));
        ImGui::PopID();
    }
}

// Detail card (right column or lower area when a result is selected).
void drawDetailCard(app::AppState& s) {
    const float S = theme::S();

    ImGui::Dummy(ImVec2(0, 4.f * S));
    ImGui::Separator();
    ImGui::Dummy(ImVec2(0, 4.f * S));

    if (gModel.detailState == aicad::ui::PartsPanelState::Idle) {
        drawPanelEmptyState("Select a part", "to see details");
        return;
    }
    if (gModel.detailState == aicad::ui::PartsPanelState::DetailLoading) {
        drawPanelEmptyState("Loading…");
        return;
    }
    if (gModel.detailState == aicad::ui::PartsPanelState::DetailError) {
        drawPanelEmptyState("Detail unavailable", gModel.detailErrorMsg.c_str());
        return;
    }

    const auto& d = gModel.detail;

    // Title + domain badge.
    ImGui::PushFont(theme::font(theme::Font::H2));
    ImGui::TextUnformatted(d.title.c_str());
    ImGui::PopFont();
    ImGui::SameLine();
    ImGui::TextColored(TVEC(accent), " [%s]", aicad::ui::partsDomainLabel(d.domain));

    // Standard/designation.
    if (!d.standard.empty() || !d.designation.empty()) {
        std::string sd;
        if (!d.standard.empty())    sd += d.standard;
        if (!d.designation.empty()) { if (!sd.empty()) sd += " / "; sd += d.designation; }
        ImGui::TextColored(TVEC(txt2), "%s", sd.c_str());
    }

    ImGui::Dummy(ImVec2(0, 4.f * S));

    // Params table.
    if (!d.params.empty() &&
        ImGui::BeginTable("##params", 2,
            ImGuiTableFlags_SizingStretchSame | ImGuiTableFlags_BordersInnerH)) {
        ImGui::TableSetupColumn("Parameter", ImGuiTableColumnFlags_WidthStretch, 0.45f);
        ImGui::TableSetupColumn("Value",     ImGuiTableColumnFlags_WidthStretch, 0.55f);
        ImGui::TableHeadersRow();
        for (const auto& [k, v] : d.params) {
            ImGui::TableNextRow();
            ImGui::TableSetColumnIndex(0);
            ImGui::TextUnformatted(k.c_str());
            ImGui::TableSetColumnIndex(1);
            ImGui::TextUnformatted(v.c_str());
        }
        ImGui::EndTable();
    }

    ImGui::Dummy(ImVec2(0, 4.f * S));

    // Sourcing links.
    if (!d.sourcing.empty()) {
        ImGui::TextColored(TVEC(txt1), "Sourcing:");
        for (const auto& src : d.sourcing) {
            std::string line = src.supplier;
            if (!src.price.empty())  line += " — " + src.price;
            ImGui::TextColored(TVEC(txt2), "  %s", line.c_str());
        }
        ImGui::Dummy(ImVec2(0, 2.f * S));
    }

    // Provenance + license.
    if (!d.provenance.empty()) {
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x);
        ImGui::TextColored(TVEC(txt2), "Source: %s", d.provenance.c_str());
        ImGui::PopTextWrapPos();
    }
    if (!d.license.empty()) {
        ImGui::TextColored(TVEC(txt2), "License: %s", d.license.c_str());
    }

    ImGui::Dummy(ImVec2(0, 6.f * S));

    // Insert button — only when geometry is available and not already in flight.
    const bool canInsert = d.has_geometry &&
                           gModel.insertState != aicad::ui::PartsPanelState::Inserting;
    if (!canInsert) ImGui::BeginDisabled();
    if (ImGui::Button("Insert into document", ImVec2(-1.f, 0.f))) {
        firePartPlace(s, d.part_id);
    }
    if (!canInsert) ImGui::EndDisabled();

    if (!d.has_geometry) {
        ImGui::TextColored(TVEC(txt2), "(No geometry — insert not available for this domain)");
    }
    if (gModel.insertState == aicad::ui::PartsPanelState::InsertError) {
        ImGui::TextColored(TVEC(bad), "Insert failed: %s", gModel.insertErrorMsg.c_str());
    }
    if (gModel.insertState == aicad::ui::PartsPanelState::InsertOk && !gModel.insertedDocId.empty()) {
        ImGui::TextColored(TVEC(txt1), "Created doc: %s", gModel.insertedDocId.c_str());
    }
}

// ---- Top-level draw --------------------------------------------------------------

void drawContent(app::AppState& s) {
    // Top section: result list.
    const float avail = ImGui::GetContentRegionAvail().y;
    const float listH = avail * 0.55f;

    switch (gModel.state) {
    case aicad::ui::PartsPanelState::Idle:
        drawPanelEmptyState("Search for a part", "type to begin");
        break;
    case aicad::ui::PartsPanelState::Loading:
        drawPanelEmptyState("Searching…");
        break;
    case aicad::ui::PartsPanelState::Error:
        drawPanelEmptyState("Search error", gModel.errorMsg.c_str());
        break;
    case aicad::ui::PartsPanelState::Ready:
        ImGui::BeginChild("##resultList", ImVec2(0, listH), ImGuiChildFlags_None);
        drawResultRows(s);
        ImGui::EndChild();
        // Detail card below the list.
        drawDetailCard(s);
        break;
    default:
        break;
    }
}

}  // namespace

// ---- Public API ------------------------------------------------------------------

void drawPartsPanel(app::AppState& s) {
    ui::PanelScaffoldSpec spec;
    spec.id         = "parts_browser";
    spec.title      = "Parts Browser";
    spec.icon       = "part";
    spec.hasSearch  = true;
    spec.searchHint = "Search parts…";

    // Warm up status line on first draw.
    if (gScaffoldState.statusText.empty()) {
        gScaffoldState.statusText = "Start typing to search all domains";
    }

    const bool searchChanged = ui::drawPanelScaffold(spec, gScaffoldState, [&]() {
        drawDomainChips(s, gScaffoldState.searchText);
        drawContent(s);
    });

    // Fire search on keystroke (debounced by query+domain comparison).
    if (searchChanged || (gScaffoldState.searchText != gLastQuery && !gScaffoldState.searchText.empty())) {
        if (s.workerStarted && (gScaffoldState.searchText != gLastQuery ||
                                gModel.domainFilter != gLastDomain)) {
            fireSearch(s, gScaffoldState.searchText, gModel.domainFilter);
        }
    }
}

void openPartsPanel() { gOpen = true; }

bool* partsPanelOpen() { return &gOpen; }

void seedPartsPanelFromJson(const std::string& json_result, const std::string& query) {
    // Called from scenarios.cpp tickSimpleShots to pre-populate the model for headless shots.
    // Mirrors the logic in the RPC callback in fireSearch — same parse, direct model write.
    try {
        const auto res = nlohmann::json::parse(json_result);
        if (!res.value("ok", false)) return;
        gLastQuery  = query;
        gLastDomain = "";
        gScaffoldState.searchText = query;
        gModel.state    = aicad::ui::PartsPanelState::Ready;
        gModel.total    = res.value("count", 0);
        gModel.has_live = res.value("has_live", false);
        gModel.results.clear();
        if (res.contains("results") && res["results"].is_array()) {
            for (const auto& rec : res["results"]) {
                aicad::ui::PartsResultRow row;
                row.part_id     = rec.value("part_id",     std::string());
                row.domain      = rec.value("domain",      std::string());
                row.title       = rec.value("title",       std::string());
                row.standard    = rec.value("standard",    std::string());
                row.designation = rec.value("designation", std::string());
                row.source_live = (rec.value("source", std::string()) == "live");
                if (rec.contains("sourcing") && rec["sourcing"].is_array()) {
                    for (const auto& src : rec["sourcing"]) {
                        std::string price = src.value("price", std::string());
                        if (!price.empty()) { row.price_hint = price; break; }
                    }
                }
                gModel.results.push_back(std::move(row));
            }
        }
        gScaffoldState.statusIsError = false;
        gScaffoldState.statusText = std::to_string(gModel.total) + " result" +
                                    (gModel.total == 1 ? "" : "s");
    } catch (...) {}  // malformed JSON → leave model as-is
}

}  // namespace ui
