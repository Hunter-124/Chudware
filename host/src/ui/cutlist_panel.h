// Weldment Cut List panel (SolidWorks "Weldment Cut List").
//
// A dockable window that shows the active PART document's weldment cut list: the structural members
// produced by `weld_member` features, grouped by profile + dimensions + length into rows with a
// quantity and total length (Item / Profile / Description / Length / Qty / Total Length).
//
// Backed by the engine `weld_cut_list` RPC via DocMirror::weldCutList (read-only). The result is
// cached and re-pulled on a doc/revision change or the Refresh button. A part with no weldment
// members shows a "No weldment members" hint; drawing tabs / no document hide the table.
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

struct AppState;

namespace ui {

class CutListPanel {
public:
    // Draw the "Cut List" window (no-op unless st.showCutListPanel). Call every frame from the main
    // loop. Pulls weld_cut_list lazily and caches it; gated to part documents (a "no part" hint for
    // drawing tabs / no document; a "no weldment members" hint for parts without weld_member).
    void draw(AppState& st);

private:
    // (Re)pull weld_cut_list into the cache for the active doc. Guarded; logs + leaves the old cache
    // on failure. Records the doc id + revision the cache reflects so staleness is auto-detected.
    void refresh(AppState& st);

    // ---- cached weld_cut_list result ----
    bool           cacheValid_ = false;
    std::string    cacheDocId_;          // doc id the cache reflects
    int            cacheRevision_ = -1;  // revision the cache reflects (re-pull when it advances)
    nlohmann::json rows_;                // [{item, profile, description, length_mm, quantity, total_length_mm}]
    int            totalMembers_ = 0;
    double         totalLengthMm_ = 0.0;
    std::vector<std::string> profiles_;  // distinct profile names present
};

}  // namespace ui
