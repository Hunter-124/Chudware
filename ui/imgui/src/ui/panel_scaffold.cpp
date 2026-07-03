// See panel_scaffold.h.
#include "panel_scaffold.h"

#include "theme/icons.h"
#include "theme/theme.h"
#include "theme/tokens.h"

#include "imgui.h"

namespace ui {

namespace {

// Centers a (possibly wrapped) line of text within the current content region — same helper
// shape as preview_pane.cpp's centeredText(), duplicated here to keep panel_scaffold.cpp
// dependency-free of ui/preview_pane.h (T-F16 is a sibling consumer, not a dependency).
void centeredText(const char* text) {
    if (!text || !*text) return;
    const float avail = ImGui::GetContentRegionAvail().x;
    const ImVec2 ts = ImGui::CalcTextSize(text, nullptr, false, avail);
    const float x = ImGui::GetCursorPosX() + (avail - ts.x) * 0.5f;
    if (x > ImGui::GetCursorPosX()) ImGui::SetCursorPosX(x);
    ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + avail);
    ImGui::TextUnformatted(text);
    ImGui::PopTextWrapPos();
}

}  // namespace

bool drawPanelScaffold(const PanelScaffoldSpec& spec, PanelScaffoldState& state,
                       const std::function<void()>& drawContent) {
    const float S = theme::S();
    bool searchChanged = false;

    // ---- header band: thin glass wash (GLASS_DESIGN_SPEC §6 doc-tab-band precedent) ------------
    {
        const ImVec2 mn = ImGui::GetCursorScreenPos();
        const float headerH = ImGui::GetFrameHeightWithSpacing() + theme::U(0.5f);
        const ImVec2 mx(mn.x + ImGui::GetContentRegionAvail().x, mn.y + headerH);
        ImDrawList* dl = ImGui::GetWindowDrawList();
        theme::glassPanel(dl, ImRect(mn, mx), theme::MaterialLevel::Thin, 0.f);

        ImGui::Dummy(ImVec2(0, theme::U(0.25f)));
        ImGui::Indent(theme::U(0.5f));
        if (spec.icon && *spec.icon) {
            icons::inlineIcon(spec.icon, ImGui::GetTextLineHeight(), theme::roleU32(theme::pal().txt0));
            ImGui::SameLine();
        }
        ImGui::PushFont(theme::font(theme::Font::H2));
        ImGui::TextUnformatted(spec.title ? spec.title : "");
        ImGui::PopFont();
        ImGui::Unindent(theme::U(0.5f));
        ImGui::Dummy(ImVec2(0, theme::U(0.25f)));
    }

    // ---- search row (optional) -------------------------------------------------------------------
    if (spec.hasSearch) {
        ImGui::SetNextItemWidth(-1.f);
        char buf[256];
        const size_t n = state.searchText.copy(buf, sizeof(buf) - 1);
        buf[n] = '\0';
        ImGui::PushID("panelScaffoldSearch");
        if (ImGui::InputTextWithHint("##search", spec.searchHint, buf, sizeof(buf))) {
            state.searchText = buf;
            searchChanged = true;
        }
        ImGui::PopID();
        ImGui::Dummy(ImVec2(0, theme::U(0.25f)));
    }

    // ---- content region (transparent; caller draws row pills/cards/tables) ------------------------
    const bool hasFooter = !state.statusText.empty();
    const float footerH = hasFooter ? ImGui::GetFrameHeightWithSpacing() : 0.f;
    ImGui::BeginChild("##panelScaffoldContent", ImVec2(0, -footerH), ImGuiChildFlags_None);
    if (drawContent) drawContent();
    ImGui::EndChild();

    // ---- status footer (optional; zero cost while idle) --------------------------------------------
    if (hasFooter) {
        ImGui::Separator();
        const ImVec4 col = state.statusIsError ? TVEC(bad) : TVEC(txt1);
        ImGui::TextColored(col, "%s", state.statusText.c_str());
    }

    (void)S;
    return searchChanged;
}

void drawPanelEmptyState(const char* label, const char* hint) {
    ImGui::Dummy(ImVec2(0, ImGui::GetContentRegionAvail().y * 0.4f));
    centeredText(label);
    if (hint && *hint) {
        ImGui::Dummy(ImVec2(0, theme::U(0.5f)));
        ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
        centeredText(hint);
        ImGui::PopStyleColor();
    }
}

}  // namespace ui
