// Parts Library workspace body — full-viewport standard parts catalog browser.
//
// Layout (phase-2 implementation):
//   +----------------------------------------------+
//   | [Parts Library title]  [/ search shortcut]   |
//   | [Fasteners] [Bearings] [Seals] … nav rail     |
//   +------+---------------------------------------+
//   | LEFT | Category / filter breadcrumb          |
//   | nav  | search bar + domain chips             |
//   | rail | result rows                           |
//   |      +---------------------------------------+
//   |      | detail card + Insert button           |
//   +------+---------------------------------------+
//   | Vendor Sources pane (collapsible)            |
//   +----------------------------------------------+
//
// Wiring: viewport.cpp calls drawPartsWorkspace() when workspace == PartsLibrary
// (full-viewport content-swap — no 3D scene beneath).

#include "ui/workspace_parts.h"
#include "ui/parts_panel.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/tokens.h"
#include "imgui.h"

#include <array>
#include <cstring>

namespace ui {

namespace {

// Category nav rail entries.  "All" maps to domain="" (same as clicking "All" chip).
struct NavEntry { const char* icon; const char* label; const char* domain; };
static const std::array<NavEntry, 9> kNavEntries = {{
    { "P", "All",        ""           },
    { "F", "Fasteners",  "fastener"   },
    { "B", "Bearings",   "bearing"    },
    { "M", "Mechanical", "mech_family"},
    { "S", "Sections",   "section"    },
    { "T", "Fittings",   "fitting"    },
    { "H", "Hydraulic",  "hydraulic"  },
    { "E", "ECAD",       "ecad"       },
    { "C", "Catalog",    "catalog"    },
}};

static int gNavSel = 0;   // index into kNavEntries

} // namespace

void drawPartsWorkspace(app::AppState& s) {
    const float S = theme::S();
    const float NAV_W = 100.f * S;   // left nav rail width

    // ---- Workspace title bar ------------------------------------------------
    ImGui::PushFont(theme::font(theme::Font::H2));
    ImGui::TextUnformatted("Parts Library");
    ImGui::PopFont();
    ImGui::SameLine(0, 12.f * S);
    ImGui::PushFont(theme::font(theme::Font::Body));
    ImGui::PushStyleColor(ImGuiCol_Text, TCOL(txt2));
    ImGui::TextUnformatted("Press / to search");
    ImGui::PopStyleColor();
    ImGui::PopFont();
    ImGui::Separator();

    // ---- Two-column layout: nav rail | main panel ---------------------------
    const ImVec2 avail = ImGui::GetContentRegionAvail();

    // Left column: nav rail
    ImGui::BeginChild("##partsNav", ImVec2(NAV_W, avail.y), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar);
    {
        const float btnW = NAV_W - 8.f * S;
        ImGui::Dummy(ImVec2(0, 4.f * S));
        for (int i = 0; i < (int)kNavEntries.size(); ++i) {
            const auto& e = kNavEntries[i];
            const bool active = (gNavSel == i);
            if (active) {
                ImGui::PushStyleColor(ImGuiCol_Button,        TVEC(accent));
                ImGui::PushStyleColor(ImGuiCol_ButtonHovered, TVEC(accent));
                ImGui::PushStyleColor(ImGuiCol_ButtonActive,  TVEC(accent));
            }
            ImGui::PushID(i);
            if (ImGui::Button(e.label, ImVec2(btnW, 0))) {
                if (!active) {
                    gNavSel = i;
                    // Propagate category selection into the parts panel domain filter.
                    // The panel picks this up next frame via partsSetNavDomain().
                    partsSetNavDomain(e.domain);
                }
            }
            ImGui::PopID();
            if (active) ImGui::PopStyleColor(3);
            ImGui::Spacing();
        }
    }
    ImGui::EndChild();

    ImGui::SameLine(0, 4.f * S);

    // Right column: the actual parts browser panel (search + results + detail card)
    ImGui::BeginChild("##partsMain", ImVec2(0, avail.y), ImGuiChildFlags_None);
    ui::drawPartsPanel(s);
    ImGui::EndChild();
}

} // namespace ui
