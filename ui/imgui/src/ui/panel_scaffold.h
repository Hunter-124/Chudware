// Reusable glass-panel scaffold (T-U1) — the ONE header/search/content/footer skeleton every new
// feature panel (T-U2..T-U8: parts browser, CAM workspace, BOM, requirements, sim studies, new-doc
// viewers, model/telemetry transparency) instantiates, so eight agents produce one coherent
// liquid-glass surface instead of eight styles. See docs/UI_PATTERN_LIBRARY.md §1 for the anatomy
// diagram and docs/GLASS_DESIGN_SPEC.md for the material/token rules this draws from.
//
// Convention (checked by scripts/ui_style_check.py): a panel built on this scaffold keeps its
// PanelScaffoldSpec + any per-frame view-model struct in a header under ui/abstract/ (no ImGui/GL
// includes there — see ui/abstract/artifact_view.h), and its .cpp includes that header. The .cpp
// itself renders from the view-model via drawPanelScaffold(); it never talks to the worker RPC or
// engine directly (that seam lives in the AppState-consuming caller, same as preview_pane.cpp).
#pragma once

#include <functional>
#include <string>

namespace ui {

// Compile-time-ish metadata for one panel instance. `id` is the stable key both
// scripts/ui_inventory.py (element inventory) and scripts/ui_style_check.py (conformance) key off
// of — pick something short and grep-friendly, e.g. "parts_browser".
struct PanelScaffoldSpec {
    const char* id;                    // stable id, e.g. "parts_browser" (T-G12 inventory key)
    const char* title;                 // header title text, e.g. "Parts Browser"
    const char* icon = nullptr;        // optional icons::draw() name shown left of the title
    bool        hasSearch = false;     // draw the search row under the header
    const char* searchHint = "Search...";
};

// Per-frame content the caller owns (search text, status line). Kept separate from
// PanelScaffoldSpec because the spec is fixed per panel instance while this mutates every frame.
struct PanelScaffoldState {
    std::string searchText;
    std::string statusText;     // footer line; empty hides the footer entirely (zero cost when idle)
    bool        statusIsError = false;  // true => footer text uses palette.bad instead of txt1
};

// Draws the glass header (icon + title [+ search row]), then `drawContent` inside a scrollable,
// transparent content region (row pills / cards / tables are the caller's job — see the pattern
// library), then the optional status footer. No ImGui::Begin/End — call this inside an existing
// window or child, same convention as ui::drawPreviewPane() / ui::drawArtifactCard().
//
// Returns true the frame the search text changes (callers debounce/re-query on that edge; compare
// against your own cached copy of state.searchText before this call if you need the previous value).
bool drawPanelScaffold(const PanelScaffoldSpec& spec, PanelScaffoldState& state,
                       const std::function<void()>& drawContent);

// Typed empty-state / placeholder row for scaffold content (pattern library §4 — mirrors
// ui/abstract/artifact_view.h's previewStateFromReason/previewStateLabel precedent: a panel with
// nothing to show renders a centered label, never a blank region). `hint` is optional secondary
// text (e.g. "try a different search term").
void drawPanelEmptyState(const char* label, const char* hint = nullptr);

}  // namespace ui
