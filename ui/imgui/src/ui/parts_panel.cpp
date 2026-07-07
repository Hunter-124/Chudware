// T-U2 Parts Browser panel — see parts_panel.h.
//
// Architecture (pattern library §0/§1):
//   - View-model: aicad::ui::PartsPanelModel (ui/abstract/parts_view.h, no ImGui/GL includes)
//   - Scaffold: ui::drawPanelScaffold / ui::drawPanelEmptyState (T-U1 kit)
//   - Worker RPC: s.rpc.call("part_search" / "part_get" / "part_place" /
//                            "vendor_status" / "vendor_login" /
//                            "scrape_enqueue" / "scrape_queue_status") via AsyncRpc
//   - State machine: PartsPanelState enum drives typed placeholder vocab (pattern §4)
//   - Row pills: theme::glassRowBackground (pattern §2)
//   - Domain filter chips: compact toggleable SmallButtons in the search row area
//   - Vendor sources pane: collapsible tree with per-source status + "Log in…" button
//   - Queue status pane: collapsible, shows background scrape/download progress

#include "parts_panel.h"
#include "workspace_parts.h"   // partsSetNavDomain declaration
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

// Nav rail domain pending set (from workspace_parts.cpp nav click).
// Cleared to "" after it is consumed in drawPartsPanel().
static std::string gPendingNavDomain;
static bool        gNavDomainPending = false;

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
    {"Catalog",  "catalog"},
};

// ---- Vendor sources model --------------------------------------------------------
struct VendorSourceStatus {
    std::string key;           // adapter key, e.g. "mcmaster"
    std::string label;         // display label
    bool        logged_in = false;
    std::string last_login;    // ISO timestamp or empty
    bool        login_in_flight = false;
    std::string login_error;
};
static std::vector<VendorSourceStatus> gVendorSources;
static bool gVendorLoaded = false;      // have we fetched status at least once?
static bool gVendorLoading = false;     // vendor_status RPC in-flight

// ---- Queue model -----------------------------------------------------------------
struct QueueEntry {
    std::string id;
    std::string site;
    std::string query;
    std::string status;  // "pending"|"running"|"done"|"error"
    std::string artifact_path;
    std::string error_msg;
    // Spec/vendor metadata from the acquire() provenance envelope.
    // Non-empty only when status=="done" and acquire returned a full envelope.
    // Keys commonly present: title, sku, price, source_url, fetched_ts.
    nlohmann::json spec_data;  // object or null
    // Per-row import state (Gap b: import done queue artifacts into the active doc).
    bool import_in_flight = false;
    std::string import_error;
    std::string import_doc_id;
};
static std::vector<QueueEntry> gQueue;
static bool gQueueLoading = false;
static std::string gEnqueueSite;
static std::string gEnqueueQuery;
static char        gEnqueueSiteBuf[64]  = {};
static char        gEnqueueQueryBuf[128] = {};
static std::string gEnqueueError;
static bool        gEnqueueRunning = false;

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

void fireVendorStatus(app::AppState& s) {
    gVendorLoading = true;
    s.rpc.call("vendor_status", {}, [](const core::RpcResult& r) {
        gVendorLoading = false;
        gVendorLoaded  = true;
        gVendorSources.clear();
        if (!r.ok) return;
        const auto& res = r.result;
        if (!res.value("ok", false)) return;
        if (!res.contains("sources") || !res["sources"].is_array()) return;
        for (const auto& src : res["sources"]) {
            VendorSourceStatus v;
            v.key        = src.value("key",        std::string());
            v.label      = src.value("label",      v.key);
            v.logged_in  = src.value("logged_in",  false);
            v.last_login = src.value("last_login",  std::string());
            gVendorSources.push_back(std::move(v));
        }
    });
}

void fireVendorLogin(app::AppState& s, int idx) {
    if (idx < 0 || idx >= (int)gVendorSources.size()) return;
    auto& vs = gVendorSources[idx];
    vs.login_in_flight = true;
    vs.login_error.clear();
    const std::string site = vs.key;
    s.rpc.call("vendor_login", {{"site", site}}, [idx](const core::RpcResult& r) {
        if (idx >= (int)gVendorSources.size()) return;
        auto& vs2 = gVendorSources[idx];
        vs2.login_in_flight = false;
        if (!r.ok) { vs2.login_error = r.err; return; }
        const auto& res = r.result;
        if (!res.value("ok", false)) {
            vs2.login_error = res.value("reason", std::string("login failed"));
            return;
        }
        vs2.logged_in  = true;
        vs2.last_login = res.value("last_login_ts", std::string());
        vs2.login_error.clear();
    });
}

void fireQueueStatus(app::AppState& s) {
    gQueueLoading = true;
    s.rpc.call("scrape_queue_status", {}, [](const core::RpcResult& r) {
        gQueueLoading = false;
        gQueue.clear();
        if (!r.ok) return;
        const auto& res = r.result;
        if (!res.value("ok", false)) return;
        if (!res.contains("jobs") || !res["jobs"].is_array()) return;
        for (const auto& job : res["jobs"]) {
            QueueEntry e;
            e.id            = job.value("id",            std::string());
            e.site          = job.value("site",           std::string());
            e.query         = job.value("query",          std::string());
            e.status        = job.value("status",         std::string("pending"));
            e.artifact_path = job.value("artifact_path",  std::string());
            e.error_msg     = job.value("error",          std::string());
            if (job.contains("spec_data") && job["spec_data"].is_object()) {
                e.spec_data = job["spec_data"];
            }
            gQueue.push_back(std::move(e));
        }
    });
}

void fireEnqueue(app::AppState& s, const std::string& site, const std::string& query) {
    gEnqueueRunning = true;
    gEnqueueError.clear();
    s.rpc.call("scrape_enqueue", {{"site", site}, {"query", query}},
               [](const core::RpcResult& r) {
        gEnqueueRunning = false;
        if (!r.ok) { gEnqueueError = r.err; return; }
        const auto& res = r.result;
        if (!res.value("ok", false)) {
            gEnqueueError = res.value("reason", std::string("enqueue failed"));
            return;
        }
        gEnqueueError.clear();
        // Refresh the queue display.
    });
}

// (b) Import a downloaded artifact (artifact_path from a done queue job) into a new part doc.
// Routes through import_artifact MCP tool → _place_import_file → import_file core RPC.
void fireImportArtifact(app::AppState& s, int jobIdx) {
    if (jobIdx < 0 || jobIdx >= (int)gQueue.size()) return;
    auto& job = gQueue[jobIdx];
    if (job.artifact_path.empty()) return;
    job.import_in_flight = true;
    job.import_error.clear();
    job.import_doc_id.clear();
    const std::string path = job.artifact_path;
    s.rpc.call("import_artifact", {{"artifact_path", path}},
               [jobIdx](const core::RpcResult& r) {
        if (jobIdx >= (int)gQueue.size()) return;
        auto& j = gQueue[jobIdx];
        j.import_in_flight = false;
        if (!r.ok) { j.import_error = r.err; return; }
        const auto& res = r.result;
        if (!res.value("ok", false)) {
            j.import_error = res.value("reason",
                                       std::string("import failed"));
            return;
        }
        j.import_doc_id = res.value("doc_id", std::string());
        j.import_error.clear();
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
                if (!currentQuery.empty())
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

// ---- Vendor sources pane ---------------------------------------------------------
// (c) Collapsible section: per-source status dot + "Log in…" button that triggers
//     browser_login (headed Chromium window) via the vendor_login RPC.
void drawVendorSources(app::AppState& s) {
    const float S = theme::S();

    ImGui::Dummy(ImVec2(0, 4.f * S));
    ImGui::Separator();
    if (!ImGui::CollapsingHeader("Vendor Sources")) return;
    ImGui::Dummy(ImVec2(0, 2.f * S));

    // Load on first open.
    if (!gVendorLoaded && !gVendorLoading && s.workerStarted) {
        fireVendorStatus(s);
    }

    if (gVendorLoading) {
        ImGui::TextColored(TVEC(txt2), "Loading sources…");
        return;
    }
    if (gVendorSources.empty()) {
        ImGui::TextColored(TVEC(txt2), "No vendor adapters loaded.");
        // Refresh button.
        ImGui::SameLine();
        if (ImGui::SmallButton("Refresh") && s.workerStarted) {
            gVendorLoaded = false;
            fireVendorStatus(s);
        }
        return;
    }

    // Table: source name | status | action
    if (ImGui::BeginTable("##vendorSrc", 3,
            ImGuiTableFlags_SizingStretchProp | ImGuiTableFlags_BordersInnerH)) {
        ImGui::TableSetupColumn("Source", ImGuiTableColumnFlags_WidthStretch, 0.40f);
        ImGui::TableSetupColumn("Status", ImGuiTableColumnFlags_WidthStretch, 0.35f);
        ImGui::TableSetupColumn("",       ImGuiTableColumnFlags_WidthStretch, 0.25f);
        ImGui::TableHeadersRow();

        for (int i = 0; i < (int)gVendorSources.size(); ++i) {
            auto& vs = gVendorSources[i];
            ImGui::TableNextRow();
            ImGui::TableSetColumnIndex(0);
            ImGui::TextUnformatted(vs.label.c_str());

            ImGui::TableSetColumnIndex(1);
            if (vs.login_in_flight) {
                ImGui::TextColored(TVEC(txt2), "Logging in…");
            } else if (!vs.login_error.empty()) {
                ImGui::TextColored(TVEC(bad), "Error");
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip("%s", vs.login_error.c_str());
            } else if (vs.logged_in) {
                ImGui::TextColored(ImVec4(0.3f, 0.9f, 0.3f, 1.f), "Logged in");
                if (!vs.last_login.empty() && ImGui::IsItemHovered())
                    ImGui::SetTooltip("Last login: %s", vs.last_login.c_str());
            } else {
                ImGui::TextColored(TVEC(txt2), "Not logged in");
            }

            ImGui::TableSetColumnIndex(2);
            ImGui::PushID(i);
            const bool busy = vs.login_in_flight;
            if (busy) ImGui::BeginDisabled();
            if (ImGui::SmallButton(vs.logged_in ? "Re-login" : "Log in…")) {
                if (s.workerStarted) fireVendorLogin(s, i);
            }
            if (busy) ImGui::EndDisabled();
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Opens a browser window for you to sign into %s.\n"
                                  "Close the window when done — your session is saved locally.",
                                  vs.label.c_str());
            ImGui::PopID();
        }
        ImGui::EndTable();
    }

    // Refresh button.
    ImGui::Dummy(ImVec2(0, 2.f * S));
    if (ImGui::SmallButton("Refresh status") && s.workerStarted && !gVendorLoading) {
        gVendorLoaded = false;
        fireVendorStatus(s);
    }
}

// ---- Queue status pane -----------------------------------------------------------
// (d) Collapsible section showing enqueue form + job list with progress.
void drawQueueStatus(app::AppState& s) {
    const float S = theme::S();

    ImGui::Dummy(ImVec2(0, 4.f * S));
    if (!ImGui::CollapsingHeader("Download Queue")) return;
    ImGui::Dummy(ImVec2(0, 2.f * S));

    // Enqueue form.
    ImGui::PushFont(theme::font(theme::Font::Body));
    ImGui::TextColored(TVEC(txt1), "Enqueue a vendor search:");
    ImGui::PopFont();
    ImGui::Dummy(ImVec2(0, 2.f * S));

    ImGui::PushItemWidth(100.f * S);
    ImGui::InputText("Site##eq_site", gEnqueueSiteBuf, sizeof(gEnqueueSiteBuf));
    ImGui::SameLine(0, 4.f * S);
    ImGui::SetNextItemWidth(180.f * S);
    ImGui::InputText("Query##eq_query", gEnqueueQueryBuf, sizeof(gEnqueueQueryBuf));
    ImGui::PopItemWidth();
    ImGui::SameLine(0, 4.f * S);

    const bool canEnqueue = gEnqueueSiteBuf[0] && gEnqueueQueryBuf[0] &&
                            !gEnqueueRunning && s.workerStarted;
    if (!canEnqueue) ImGui::BeginDisabled();
    if (ImGui::SmallButton("Enqueue")) {
        fireEnqueue(s, gEnqueueSiteBuf, gEnqueueQueryBuf);
    }
    if (!canEnqueue) ImGui::EndDisabled();

    if (!gEnqueueError.empty()) {
        ImGui::TextColored(TVEC(bad), "Enqueue error: %s", gEnqueueError.c_str());
    }

    ImGui::Dummy(ImVec2(0, 4.f * S));

    // Job list — always refresh on first open; button to force refresh.
    if (gQueue.empty() && !gQueueLoading && s.workerStarted) {
        fireQueueStatus(s);
    }

    bool refreshBtn = ImGui::SmallButton("Refresh##q");
    if (refreshBtn && s.workerStarted && !gQueueLoading) fireQueueStatus(s);

    ImGui::Dummy(ImVec2(0, 2.f * S));
    if (gQueueLoading) {
        ImGui::TextColored(TVEC(txt2), "Loading…");
        return;
    }
    if (gQueue.empty()) {
        ImGui::TextColored(TVEC(txt2), "Queue is empty.");
        return;
    }

    // Queue job table: 5 columns — Site | Query | Status | Spec/Result | Import
    if (ImGui::BeginTable("##queueJobs", 5,
            ImGuiTableFlags_SizingStretchProp | ImGuiTableFlags_BordersInnerH |
            ImGuiTableFlags_ScrollY, ImVec2(0, 180.f * S))) {
        ImGui::TableSetupColumn("Site",    ImGuiTableColumnFlags_WidthStretch, 0.14f);
        ImGui::TableSetupColumn("Query",   ImGuiTableColumnFlags_WidthStretch, 0.30f);
        ImGui::TableSetupColumn("Status",  ImGuiTableColumnFlags_WidthStretch, 0.18f);
        ImGui::TableSetupColumn("Spec / Result", ImGuiTableColumnFlags_WidthStretch, 0.26f);
        ImGui::TableSetupColumn("Action",  ImGuiTableColumnFlags_WidthStretch, 0.12f);
        ImGui::TableHeadersRow();

        for (int i = 0; i < (int)gQueue.size(); ++i) {
            auto& job = gQueue[i];
            ImGui::PushID(i);
            ImGui::TableNextRow();

            // Col 0: site
            ImGui::TableSetColumnIndex(0);
            ImGui::TextUnformatted(job.site.c_str());

            // Col 1: query
            ImGui::TableSetColumnIndex(1);
            ImGui::TextUnformatted(job.query.c_str());

            // Col 2: status badge
            ImGui::TableSetColumnIndex(2);
            ImVec4 col = TVEC(txt2);
            if (job.status == "done")    col = ImVec4(0.3f, 0.9f, 0.3f, 1.f);
            if (job.status == "error")   col = TVEC(bad);
            if (job.status == "running") col = TVEC(accent);
            ImGui::TextColored(col, "%s", job.status.c_str());
            if (job.status == "error" && !job.error_msg.empty() && ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", job.error_msg.c_str());

            // Col 3: (d) spec-extraction results view — show vendor metadata when available.
            // For done jobs with spec_data, display title+sku+price on one line.
            // Hover expands to source_url + fetched_ts.
            // For done jobs with only artifact_path, show the filename.
            ImGui::TableSetColumnIndex(3);
            if (job.status == "done") {
                if (!job.spec_data.is_null() && job.spec_data.is_object() &&
                    !job.spec_data.empty()) {
                    // Build a compact spec summary line.
                    std::string specLine;
                    const auto& sd = job.spec_data;
                    // Title (may be long — truncate for table display).
                    if (sd.contains("title") && sd["title"].is_string()) {
                        std::string t = sd["title"].get<std::string>();
                        if (t.size() > 28) t = t.substr(0, 25) + "…";
                        specLine += t;
                    }
                    if (sd.contains("sku") && sd["sku"].is_string()) {
                        if (!specLine.empty()) specLine += " · ";
                        specLine += sd["sku"].get<std::string>();
                    }
                    if (sd.contains("price") && sd["price"].is_string()) {
                        if (!specLine.empty()) specLine += " ";
                        specLine += sd["price"].get<std::string>();
                    }
                    if (specLine.empty()) {
                        // Fallback: show filename if no useful vendor metadata.
                        const auto slash = job.artifact_path.rfind('/');
                        specLine = (slash != std::string::npos)
                            ? job.artifact_path.substr(slash + 1) : job.artifact_path;
                    }
                    ImGui::TextColored(TVEC(txt2), "%s", specLine.c_str());
                    // Hover tooltip: full spec detail.
                    if (ImGui::IsItemHovered()) {
                        std::string tip;
                        if (sd.contains("title") && sd["title"].is_string())
                            tip += "Title: " + sd["title"].get<std::string>() + "\n";
                        if (sd.contains("sku") && sd["sku"].is_string())
                            tip += "SKU: " + sd["sku"].get<std::string>() + "\n";
                        if (sd.contains("price") && sd["price"].is_string())
                            tip += "Price: " + sd["price"].get<std::string>() + "\n";
                        if (sd.contains("source_url") && sd["source_url"].is_string())
                            tip += "URL: " + sd["source_url"].get<std::string>() + "\n";
                        if (sd.contains("fetched_ts") && sd["fetched_ts"].is_string())
                            tip += "Fetched: " + sd["fetched_ts"].get<std::string>() + "\n";
                        if (!job.artifact_path.empty())
                            tip += "File: " + job.artifact_path;
                        if (!tip.empty()) {
                            // Trim trailing newline.
                            if (tip.back() == '\n') tip.pop_back();
                            ImGui::SetTooltip("%s", tip.c_str());
                        }
                    }
                } else if (!job.artifact_path.empty()) {
                    // No spec_data — show filename only.
                    const auto slash = job.artifact_path.rfind('/');
                    const std::string fname = (slash != std::string::npos)
                        ? job.artifact_path.substr(slash + 1) : job.artifact_path;
                    ImGui::TextColored(TVEC(txt2), "%s", fname.c_str());
                    if (ImGui::IsItemHovered())
                        ImGui::SetTooltip("%s", job.artifact_path.c_str());
                }
                // Import result feedback (shown in same cell when import was attempted).
                if (!job.import_doc_id.empty()) {
                    ImGui::TextColored(ImVec4(0.3f, 0.9f, 0.3f, 1.f),
                                       "Imported: %s", job.import_doc_id.c_str());
                }
                if (!job.import_error.empty()) {
                    ImGui::TextColored(TVEC(bad), "Import failed: %s",
                                       job.import_error.c_str());
                }
            }

            // Col 4: (b) Import button — only on done jobs with an artifact file.
            // Triggers import_artifact RPC → import_file core RPC → new editable doc.
            ImGui::TableSetColumnIndex(4);
            if (job.status == "done" && !job.artifact_path.empty()) {
                const bool canImport = !job.import_in_flight &&
                                       job.import_doc_id.empty() &&
                                       s.workerStarted;
                if (!canImport) ImGui::BeginDisabled();
                if (ImGui::SmallButton(job.import_in_flight ? "…" : "Import")) {
                    fireImportArtifact(s, i);
                }
                if (!canImport) ImGui::EndDisabled();
                if (ImGui::IsItemHovered() && !job.import_in_flight)
                    ImGui::SetTooltip("Import this downloaded CAD file into a new editable part doc.\n"
                                      "%s", job.artifact_path.c_str());
            }

            ImGui::PopID();
        }
        ImGui::EndTable();
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

    // Vendor sources + download queue panes (collapsible, at the bottom).
    drawVendorSources(s);
    drawQueueStatus(s);
}

}  // namespace

// ---- Public API ------------------------------------------------------------------

void drawPartsPanel(app::AppState& s) {
    // Consume any pending nav domain set from workspace_parts.cpp nav rail click.
    if (gNavDomainPending) {
        gNavDomainPending = false;
        if (gModel.domainFilter != gPendingNavDomain) {
            gModel.domainFilter = gPendingNavDomain;
            if (!gScaffoldState.searchText.empty() && s.workerStarted) {
                fireSearch(s, gScaffoldState.searchText, gModel.domainFilter);
            }
        }
    }

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

void partsSetNavDomain(const std::string& domain) {
    gPendingNavDomain = domain;
    gNavDomainPending = true;
}

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
