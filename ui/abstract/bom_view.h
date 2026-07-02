#pragma once
// Framework-agnostic view-model for the T-U4 BOM & sourcing panel.
// Mirrors ui/abstract/parts_view.h (T-U2 exemplar): pure structs with no ImGui/GL/GLFW
// includes — the ui/imgui/ backend renders from these; a future ui/qt/ backend can reuse them.
// Consumed by ui/imgui/src/ui/bom_panel.cpp.
//
// Price contract (aicad.sourcing/1 §0):
//   unit_cost_str / ext_cost_str are EITHER a formatted currency string (e.g. "$1.23")
//   or the em-dash sentinel "—" (BomRow::UNPRICED). A blank/"unknown" price is always the
//   sentinel — never a fabricated number, never an empty string in the rendered cell.

#include <string>
#include <vector>

namespace aicad::ui {

// Em-dash sentinel displayed in the price columns when cost is unknown/unresolved.
// Contract: if unit_cost_str == UNPRICED the tooltip says "price not available".
inline constexpr const char* BOM_UNPRICED = "\xe2\x80\x94";  // UTF-8 "—"

// One line in the BOM rollup (maps to aicad.sourcing/1 §3 BomLine).
struct BomRow {
    std::string line_id;       // stable opaque id from the engine
    std::string kind;          // "made" | "bought"
    int         quantity = 0;
    std::string unit;          // "ea" | "mm" | …
    std::string description;   // human-readable part description
    std::string mpn;           // manufacturer part number, or empty
    std::string supplier;      // resolved supplier name, or empty
    std::string unit_cost_str; // "$1.23" or BOM_UNPRICED
    std::string ext_cost_str;  // "$4.92" or BOM_UNPRICED

    // Match-review fields (populated after match_part_number RPC).
    bool        matchPending  = false;  // match_part_number call in-flight
    bool        matchReady    = false;  // match result available for review
    bool        matchAccepted = false;  // user accepted; bom_panel calls applyOps to write
    std::string matchMpn;     // suggested MPN from the matcher
    std::string matchSupplier;
    std::string matchDetail;  // free-text detail for the review tooltip
};

// Inline filter: returns rows where description or mpn contains `query` (case-insensitive
// simple substring).  Called every frame from bom_panel.cpp; stays in this header so a
// future Qt backend can reuse it without touching the ImGui .cpp.
inline std::vector<BomRow> filterBomRows(const std::vector<BomRow>& rows,
                                          const std::string& query) {
    if (query.empty()) return rows;
    std::string lo = query;
    for (char& c : lo) c = (char)std::tolower((unsigned char)c);
    std::vector<BomRow> out;
    for (const auto& r : rows) {
        auto contains = [&](const std::string& s) {
            std::string ls = s;
            for (char& c : ls) c = (char)std::tolower((unsigned char)c);
            return ls.find(lo) != std::string::npos;
        };
        if (contains(r.description) || contains(r.mpn) || contains(r.supplier))
            out.push_back(r);
    }
    return out;
}

// Panel loading state axis — drives typed placeholder vocab (pattern library §4).
enum class BomPanelState {
    Idle,          // No doc loaded yet
    Loading,       // bom_price RPC in-flight
    Ready,         // Rows in hand (may be empty)
    Error,         // RPC or engine error
};

// Quote request status (per-panel; one request at a time).
enum class QuoteStatus {
    Idle,
    Pending,       // request_quote RPC in-flight
    Ok,            // quote received
    Error,         // vendor error / keyless
};

// The per-panel view-model.
struct BomPanelModel {
    BomPanelState         state     = BomPanelState::Idle;
    std::vector<BomRow>   rows;
    std::string           errorMsg;

    // Footer totals (pre-formatted; empty when all prices are unknown).
    std::string           totalStr;  // "$12.34" or BOM_UNPRICED or empty

    // Quote seam.
    QuoteStatus           quoteStatus = QuoteStatus::Idle;
    std::string           quoteMsg;     // status line shown in panel footer on quote request
    std::string           quoteVendor; // last vendor requested

    // Cache invalidation keys (bom_panel.cpp uses these to detect doc change).
    std::string           cacheDocId;
    int                   cacheRevision = -1;
};

}  // namespace aicad::ui
