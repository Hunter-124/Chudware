// AI-CAD unified command + settings search widget (W2-Search).
// Floating glass window, centered, opened when AppState::showCommandSearch is true.
// Results: (a) ribbon modeling commands from getRibbonCmds(); (b) settings entries from
// kSettingsEntries[]. Activates on click or Enter (first match); ESC closes.
#include "command_search.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstring>
#include <vector>

#include "imgui.h"

#include "../core/app_state.h"
#include "../ui_theme.h"
#include "icons.h"
#include "ribbon_def.h"
#include "settings_modal.h"

namespace ui {

namespace {

static char s_buf[256]      = {};
static bool s_wantFocus     = false;

// Case-insensitive substring search. Returns true when needle is empty (matches everything).
static bool icontains(const char* hay, const char* needle) {
    if (!needle || !needle[0]) return true;
    if (!hay)                  return false;
    const char* h = hay;
    while (*h) {
        const char* ph = h, *pn = needle;
        while (*ph && *pn &&
               std::tolower((unsigned char)*ph) == std::tolower((unsigned char)*pn))
        { ++ph; ++pn; }
        if (!*pn) return true;
        ++h;
    }
    return false;
}

}  // anonymous namespace

void drawCommandSearch(AppState& st) {
    // Detect the open edge: clear the buffer and schedule focus.
    static bool s_wasShown = false;
    if (st.showCommandSearch && !s_wasShown) {
        s_buf[0]    = '\0';
        s_wantFocus = true;
    }
    s_wasShown = st.showCommandSearch;
    if (!st.showCommandSearch) return;

    // ---- window geometry ----
    ImGuiViewport* vp   = ImGui::GetMainViewport();
    const float    sc   = ui::uiScale;
    const float    popW = 560.0f * sc;
    const float    popH = 460.0f * sc;
    ImGui::SetNextWindowSize(ImVec2(popW, popH), ImGuiCond_Always);
    ImGui::SetNextWindowPos(vp->GetCenter(), ImGuiCond_Always, ImVec2(0.5f, 0.5f));

    bool shouldClose = false;

    ui::pushGlassWindowStyle();
    if (ImGui::Begin("##CommandSearch", nullptr,
                     ImGuiWindowFlags_NoTitleBar  | ImGuiWindowFlags_NoResize  |
                     ImGuiWindowFlags_NoMove       | ImGuiWindowFlags_NoDocking |
                     ImGuiWindowFlags_NoScrollbar  | ImGuiWindowFlags_NoScrollWithMouse |
                     ImGuiWindowFlags_NoCollapse)) {

        // ---- filter input ----
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(8.0f * sc, 6.0f * sc));
        if (s_wantFocus) {
            ImGui::SetKeyboardFocusHere();
            s_wantFocus = false;
        }
        ImGui::SetNextItemWidth(-1.0f);
        const bool entered = ImGui::InputText("##csq", s_buf, sizeof(s_buf),
                                               ImGuiInputTextFlags_EnterReturnsTrue);
        ImGui::PopStyleVar();

        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) shouldClose = true;

        ImGui::Separator();

        // ---- collect matches ----
        int              nCmds = 0;
        const RibbonCmd* cmds  = ui::getRibbonCmds(nCmds);

        std::vector<int> ribIdx, setIdx;
        ribIdx.reserve(32);
        setIdx.reserve(16);

        for (int i = 0; i < nCmds; ++i) {
            const RibbonCmd& c = cmds[i];
            if (c.stretch) continue;
            if (icontains(c.label, s_buf) || icontains(c.tab, s_buf) ||
                icontains(c.group, s_buf))
                ribIdx.push_back(i);
        }
        for (int i = 0; i < kSettingsEntryCount; ++i) {
            const SettingsEntry& e = kSettingsEntries[i];
            if (icontains(e.label, s_buf) || icontains(e.tab, s_buf))
                setIdx.push_back(i);
        }

        // ---- Enter => activate first match ----
        if (entered && !shouldClose) {
            if (!ribIdx.empty()) {
                if (st.ribbonDispatch) st.ribbonDispatch(cmds[ribIdx[0]].cmd_id);
                shouldClose = true;
            } else if (!setIdx.empty()) {
                const SettingsEntry& e = kSettingsEntries[setIdx[0]];
                st.openSettings        = true;
                st.openSettingsTab     = e.tabIndex;
                shouldClose            = true;
            }
        }

        // ---- results scroll area ----
        ImGui::BeginChild("##csqlist", ImVec2(0.0f, 0.0f), ImGuiChildFlags_None);

        const float iconSz  = 20.0f * sc;
        const float rowH    = iconSz + 6.0f * sc;   // icon + vertical padding
        const float iconOff = 4.0f * sc;             // left margin to icon
        const float lblOff  = iconOff + iconSz + 6.0f * sc;  // left edge of label text
        const ImU32 colText = ui::palette.toU32(ui::palette.text);
        const ImU32 colDim  = ui::palette.toU32(ui::palette.textDim);

        auto secLabel = [&](const char* name) {
            ImGui::Dummy(ImVec2(0.0f, 2.0f * sc));
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                       ui::palette.textDim[2], 1.0f));
            ImGui::TextUnformatted(name);
            ImGui::PopStyleColor();
        };

        // -- (a) Modeling tools --
        if (!ribIdx.empty()) {
            secLabel("Modeling Tools");
            for (int ri : ribIdx) {
                const RibbonCmd& c  = cmds[ri];
                IconUV           ico = ui::getIcon(c.icon, 20);
                ImGui::PushID(ri);

                ImVec2       pos   = ImGui::GetCursorScreenPos();
                const float  avail = ImGui::GetContentRegionAvail().x;
                const bool   hit   = ImGui::Selectable("##row", false, 0,
                                                        ImVec2(avail, rowH));

                ImDrawList*  dl = ImGui::GetWindowDrawList();
                const float  ty = pos.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f;

                if (ico.valid) {
                    const float iy = pos.y + (rowH - iconSz) * 0.5f;
                    dl->AddImage(ico.texId,
                                 ImVec2(pos.x + iconOff, iy),
                                 ImVec2(pos.x + iconOff + iconSz, iy + iconSz),
                                 ico.uv0, ico.uv1);
                }
                dl->AddText(ImVec2(pos.x + lblOff, ty), colText, c.label);

                char  dimstr[128];
                std::snprintf(dimstr, sizeof(dimstr), "%s / %s", c.tab, c.group);
                float dw = ImGui::CalcTextSize(dimstr).x;
                dl->AddText(ImVec2(pos.x + avail - dw - 8.0f * sc, ty), colDim, dimstr);

                if (hit) {
                    if (st.ribbonDispatch) st.ribbonDispatch(c.cmd_id);
                    shouldClose = true;
                }
                ImGui::PopID();
            }
        }

        // -- (b) Settings entries --
        if (!setIdx.empty()) {
            if (!ribIdx.empty()) ImGui::Dummy(ImVec2(0.0f, 4.0f * sc));
            secLabel("Settings");
            for (int si : setIdx) {
                const SettingsEntry& e = kSettingsEntries[si];
                ImGui::PushID(1000 + si);

                ImVec2      pos   = ImGui::GetCursorScreenPos();
                const float avail = ImGui::GetContentRegionAvail().x;
                const bool  hit   = ImGui::Selectable("##row", false, 0,
                                                       ImVec2(avail, rowH));

                ImDrawList* dl = ImGui::GetWindowDrawList();
                const float ty = pos.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f;

                // Settings rows: no icon; indent to match the label column of ribbon rows
                dl->AddText(ImVec2(pos.x + lblOff, ty), colText, e.label);

                float dw = ImGui::CalcTextSize(e.tab).x;
                dl->AddText(ImVec2(pos.x + avail - dw - 8.0f * sc, ty), colDim, e.tab);

                if (hit) {
                    st.openSettings    = true;
                    st.openSettingsTab = e.tabIndex;
                    shouldClose        = true;
                }
                ImGui::PopID();
            }
        }

        // Empty-state message
        if (ribIdx.empty() && setIdx.empty() && s_buf[0]) {
            ImGui::Dummy(ImVec2(0.0f, 8.0f * sc));
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                       ui::palette.textDim[2], 1.0f));
            char msg[320];
            std::snprintf(msg, sizeof(msg), "No results for \"%s\"", s_buf);
            ImGui::TextUnformatted(msg);
            ImGui::PopStyleColor();
        }

        ImGui::EndChild();
    }
    ImGui::End();
    ui::popGlassWindowStyle();

    if (shouldClose) {
        st.showCommandSearch = false;
        s_buf[0]             = '\0';
    }
}

}  // namespace ui
