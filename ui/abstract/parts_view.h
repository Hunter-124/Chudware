#pragma once
// Framework-agnostic view-model for the T-U2 parts browser panel (Pillar 17).
// Mirrors ui/abstract/scaffold_demo_view.h (T-U1 exemplar): pure structs with no ImGui/GL/GLFW
// includes — the ui/imgui/ backend renders from these; a future ui/qt/ backend can do the same.
// Consumed by ui/imgui/src/ui/parts_panel.cpp.  Async population via s.rpc.call (AsyncRpc).

#include <string>
#include <vector>

namespace aicad::ui {

// Domain badge strings — the eight reserved domain identifiers from contracts/parts.md §0.
// Keep in sync with the contract; new domains are additive.
inline const char* partsDomainLabel(const std::string& domain) {
    if (domain == "fastener")    return "FAST";
    if (domain == "bearing")     return "BRG";
    if (domain == "mech_family") return "MECH";
    if (domain == "section")     return "SECT";
    if (domain == "fitting")     return "FIT";
    if (domain == "hydraulic")   return "HYD";
    if (domain == "ecad")        return "ECAD";
    return "PART";
}

// One sourcing link (from contracts/sourcing_schema.md / PartRecord.sourcing[]).
struct PartsSourcingRef {
    std::string supplier;     // e.g. "DigiKey"
    std::string url;          // may be empty if local catalog only
    std::string price;        // formatted string e.g. "$0.12 / 10" or empty
};

// One result row returned by part_search (maps to the PartRecord envelope, contracts/parts.md §1).
struct PartsResultRow {
    std::string part_id;      // "fastener:ISO4762:M8"
    std::string domain;       // "fastener", "bearing", …
    std::string title;        // "ISO 4762 M8 Socket Head Cap Screw"
    std::string standard;     // "ISO 4762" or empty
    std::string designation;  // "M8" or empty
    bool        source_live = false;   // true = live vendor result (marked with price annotation)
    std::string price_hint;            // set when source_live && sourcing price available
};

// Detail card data (populated when user selects a result row, via part_get RPC).
struct PartsDetailCard {
    std::string part_id;
    std::string domain;
    std::string title;
    std::string standard;
    std::string designation;
    std::string provenance;
    std::string license;                      // "clean-room" | "public-domain" | "user-supplied"
    std::vector<std::pair<std::string,std::string>> params;  // {key, display_value}
    std::vector<PartsSourcingRef> sourcing;
    bool has_geometry = false;  // geometry_ref non-null → Insert button enabled
};

// Panel loading/state axis — drives typed placeholder vocab (pattern library §4 / docs/UI_PATTERN_LIBRARY.md).
enum class PartsPanelState {
    Idle,           // No query yet (initial state)
    Loading,        // Waiting for part_search RPC result
    Ready,          // Results in hand (may be empty)
    Error,          // RPC call failed or worker not started
    DetailLoading,  // Waiting for part_get RPC result (row selected)
    DetailReady,    // Detail card populated
    DetailError,    // part_get call failed
    Inserting,      // Waiting for part_place RPC result
    InsertOk,       // Insert succeeded — status line shows confirmation
    InsertError,    // Insert failed
};

// The per-panel view-model.  Kept in AppState (not file-static) so the async RPC callbacks
// (fired inside AsyncRpc::drain() in Application::frame()) can populate it safely.
struct PartsPanelModel {
    // --- search ---
    PartsPanelState           state     = PartsPanelState::Idle;
    std::vector<PartsResultRow> results;
    int                       total     = 0;
    bool                      has_live  = false;   // any live vendor rows in results
    std::string               errorMsg;

    // --- filters ---
    // Empty = all domains; filled = one domain filter chip is active.
    std::string               domainFilter;   // "fastener" | "bearing" | … | ""

    // --- selected row ---
    int                       selectedIdx = -1;    // index into results

    // --- detail ---
    PartsPanelState           detailState = PartsPanelState::Idle;
    PartsDetailCard           detail;
    std::string               detailErrorMsg;

    // --- insert ---
    PartsPanelState           insertState = PartsPanelState::Idle;
    std::string               insertedDocId;   // set on InsertOk
    std::string               insertErrorMsg;
};

}  // namespace aicad::ui
