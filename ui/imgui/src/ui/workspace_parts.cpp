// Parts Library workspace body — full-viewport standard parts catalog browser.
//
// Backend: drawPartsPanel(s) (parts_panel.h — T-U2) provides federated search,
// detail cards, and insert-via-part_place. In the old layout this lived as a
// side panel; this workspace gives it a full-screen home so users can browse the
// catalog without the 3D viewport competing for space.
//
// Phase-2 agents: add a workspace header BEFORE the parts panel content:
//   - "Parts Library" title on the left
//   - Category nav rail (Fasteners / Bearings / Seals / Custom / Favorites)
//   - Keyboard shortcut hints (/ to focus search, Enter to insert)
//   - Insert-into-model button (wired to "parts_insert" dispatch, or openPartsPanel()
//     followed by part_place RPC once a part is selected)
// The drawPartsPanel body already handles search + results + detail card; the
// header just adds navigation context.
//
// Note: openPartsPanel() / partsPanelOpen() are for the old side-pane mode;
// in this workspace the panel body renders directly without the Begin/End pane wrapper.
#include "ui/workspace_parts.h"
#include "ui/parts_panel.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/tokens.h"
#include "imgui.h"

namespace ui {

void drawPartsWorkspace(app::AppState& s) {
    const float S = theme::S();

    // Minimal header bar — "Parts Library" title.
    // TODO(phase-2 parts): replace with full category nav rail + filter chips.
    ImGui::PushFont(theme::font(theme::Font::H2));
    ImGui::TextUnformatted("Parts Library");
    ImGui::PopFont();
    ImGui::PushFont(theme::font(theme::Font::Body));
    ImGui::PushStyleColor(ImGuiCol_Text, TCOL(txt1));
    ImGui::TextUnformatted("Standard parts, fasteners, and custom catalog");
    ImGui::PopStyleColor();
    ImGui::PopFont();
    ImGui::Dummy(ImVec2(0, 6.f * S));
    ImGui::Separator();
    ImGui::Dummy(ImVec2(0, 4.f * S));

    // Delegate to the existing Parts Panel (live backend — do not replace).
    // drawPartsPanel renders its own PanelScaffold header ("Parts") + search bar;
    // it runs headlessly via AI_CAD_SHOT=parts_browser fixture seeding.
    ui::drawPartsPanel(s);
}

} // namespace ui
