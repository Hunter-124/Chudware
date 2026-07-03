// T-U5 Requirements & verification panel — pure view-model (no ImGui/GL includes).
// Rendered by ui/imgui/src/ui/requirements_panel.cpp.
//
// Status vocabulary mirrors engine_oracle/requirements/requirements.py:VALID_STATUSES exactly:
//   proposed, user_confirmed, verified, failed, waived
// NEVER invent new values; the backend rejects unknown statuses.
//
// Waive contract: the backend requires who="user" and the panel may not set
// user_confirmed or waived on behalf of an agent (requirements.py set_status L375).
// The UI gates the Waive button on a non-empty waive_reason string and
// calls req_status with who="user" in the RPC payload.
#pragma once

#include <string>
#include <vector>

namespace aicad { namespace ui {

// ---- Requirement row -------------------------------------------------------

// One requirement as displayed in the panel (flat struct, no engine types).
struct ReqRow {
    std::string id;           // e.g. "req_001"
    std::string kind;         // one of VALID_KINDS: dimensional/functional/material/...
    std::string status;       // one of VALID_STATUSES: proposed/user_confirmed/verified/failed/waived
    std::string text;         // human-readable requirement text
    std::string quantity_str; // formatted "op value unit" or empty when absent
    std::vector<std::string> verify_by; // verifier ref ids (display only)
};

// ---- Battery check row -----------------------------------------------------

// One entry from the verify_battery / req_report battery results.
struct BatteryCheckRow {
    std::string check_name;   // e.g. "interference", "erc", "budget"
    bool        passed = false;
    std::string evidence_str; // short summary / evidence text (may be empty)
    int         elapsed_ms = 0;
};

// ---- Panel state machine ---------------------------------------------------

enum class ReqPanelState {
    Idle,      // no doc / worker not started
    Loading,   // RPC in flight
    Ready,     // req_rows + battery_rows populated
    Error,     // error msg set
};

// ---- Panel model -----------------------------------------------------------

struct ReqPanelModel {
    ReqPanelState state = ReqPanelState::Idle;

    std::vector<ReqRow>         req_rows;
    std::vector<BatteryCheckRow> battery_rows;

    std::string errorMsg;

    // Waive-action form state (cleared when panel loads new data).
    // Only shown when a failed/proposed req is selected; Waive button
    // is disabled until this is non-empty (contract from set_status).
    std::string waive_reason;
    std::string selected_req_id;  // req row being acted on

    // Doc-change cache invalidation (mirrors BomPanelModel pattern).
    std::string cacheDocId;
    int         cacheRevision = -1;
};

// ---- Filter helper ---------------------------------------------------------

inline std::vector<ReqRow> filterReqRows(const std::vector<ReqRow>& rows,
                                          const std::string& search) {
    if (search.empty()) return rows;
    std::vector<ReqRow> out;
    out.reserve(rows.size());
    for (const auto& r : rows) {
        // Case-insensitive substring match on id, kind, status, or text.
        auto contains = [&](const std::string& haystack) -> bool {
            if (haystack.size() < search.size()) return false;
            // simple linear scan (panel is small)
            for (size_t i = 0; i + search.size() <= haystack.size(); ++i) {
                bool match = true;
                for (size_t j = 0; j < search.size(); ++j) {
                    char a = haystack[i + j], b = search[j];
                    if (a >= 'A' && a <= 'Z') a = (char)(a + 32);
                    if (b >= 'A' && b <= 'Z') b = (char)(b + 32);
                    if (a != b) { match = false; break; }
                }
                if (match) return true;
            }
            return false;
        };
        if (contains(r.id) || contains(r.kind) || contains(r.status) || contains(r.text))
            out.push_back(r);
    }
    return out;
}

}} // namespace aicad::ui
