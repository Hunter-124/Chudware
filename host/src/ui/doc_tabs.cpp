// Document tab strip — see doc_tabs.h. Renders the open documents as browser-style tabs and routes
// click→switch, X/middle-click→close (with an unsaved-changes prompt), and "+"→New Part. All
// mutations to AppState::docTabs are DEFERRED until after the strip is fully drawn (we iterate the
// vector while drawing), mirroring the feature tree's s_pendingTreeOps discipline so a switch/close
// never invalidates the loop it fires from.
#include <cstdio>
#include <string>

#include "imgui.h"

#include "doc_tabs.h"

#include "../core/app_state.h"
#include "../core/doc_mirror.h"
#include "../ui_theme.h"   // ui::uiScale, ui::palette

namespace ui {

// Deferred actions, applied after the strip finishes drawing (see header note).
static int  s_switchTo   = -1;   // tab index to activate, or -1
static int  s_closeIdx   = -1;   // tab index to close, or -1
static bool s_newPart    = false;
// Unsaved-changes prompt: the tab the user asked to close while it had unsaved edits.
static int  s_confirmCloseIdx = -1;

// A single tab, placed explicitly at screen position (x, y0). Returns the tab WIDTH so the caller
// can advance to the next tab. Records a deferred switch/close into the file statics. Layout:
// [ title (dirty *) × ]. The whole pill switches; the × hot-zone (submitted on top) closes.
static float drawOneTab(AppState& st, int idx, float x, float y0, float barH) {
    const auto& pal = ui::palette;
    AppState::DocTab& t = st.docTabs[idx];
    const bool active = (idx == st.activeTab);
    float s = ui::uiScale;

    ImGui::PushID(idx);

    std::string label = t.title.empty() ? std::string("Untitled") : t.title;
    if (t.dirty) label += " *";

    ImDrawList* dl = ImGui::GetWindowDrawList();
    float padX = 12.0f * s;
    float closeW = 18.0f * s;
    float textW = ImGui::CalcTextSize(label.c_str()).x;
    float tabW = padX + textW + 6.0f * s + closeW + padX * 0.5f;
    float tabH = barH;

    ImVec2 p0(x, y0);
    ImVec2 p1(p0.x + tabW, p0.y + tabH);

    // Whole-pill click target (switch). The × button sits on top and consumes its own clicks.
    ImGui::SetCursorScreenPos(p0);
    bool switchClicked = ImGui::InvisibleButton("##tab", ImVec2(tabW, tabH));
    bool hovered = ImGui::IsItemHovered();
    bool middleClose = hovered && ImGui::IsMouseClicked(ImGuiMouseButton_Middle);

    // Pill background: active = panel bg with an accent top edge; inactive = darker, hover lifts it.
    ImU32 bg = active ? ui::Palette::toU32(pal.panelBg)
                      : (hovered ? ui::Palette::toU32(pal.buttonHovered)
                                 : ui::Palette::toU32(pal.headerBg));
    dl->AddRectFilled(p0, p1, bg, 4.0f * s, ImDrawFlags_RoundCornersTop);
    if (active)
        dl->AddLine(ImVec2(p0.x + 2, p0.y + 1), ImVec2(p1.x - 2, p0.y + 1),
                    ui::Palette::toU32(pal.accent), 2.0f * s);
    else  // thin separator on the right edge of inactive tabs
        dl->AddLine(ImVec2(p1.x - 0.5f, p0.y + 4 * s), ImVec2(p1.x - 0.5f, p1.y - 4 * s),
                    ui::Palette::toU32(pal.border), 1.0f);

    // Title text.
    const float* textCol = active ? pal.text : pal.textDim;
    dl->AddText(ImVec2(p0.x + padX, p0.y + (tabH - ImGui::GetFontSize()) * 0.5f),
                ImGui::ColorConvertFloat4ToU32(ImVec4(textCol[0], textCol[1], textCol[2], 1)),
                label.c_str());

    if (hovered && !t.path.empty())
        ImGui::SetTooltip("%s%s", t.path.c_str(), t.dirty ? "  (unsaved changes)" : "");
    else if (hovered && t.dirty)
        ImGui::SetTooltip("Unsaved changes");

    // × close button — submitted AFTER (on top of) the pill so ImGui routes clicks in this zone to
    // it, not to the switch button underneath.
    float cx = p1.x - closeW - padX * 0.4f;
    ImGui::SetCursorScreenPos(ImVec2(cx, p0.y + (tabH - closeW) * 0.5f));
    bool xClicked = ImGui::InvisibleButton("##x", ImVec2(closeW, closeW));
    bool xHover = ImGui::IsItemHovered();
    ImVec2 xc(cx + closeW * 0.5f, p0.y + tabH * 0.5f);
    if (xHover)
        dl->AddCircleFilled(xc, closeW * 0.5f, IM_COL32(232, 17, 35, 200));
    float g = closeW * 0.22f;
    ImU32 xCol = xHover ? IM_COL32(255, 255, 255, 255)
                        : ImGui::ColorConvertFloat4ToU32(
                              ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1));
    dl->AddLine(ImVec2(xc.x - g, xc.y - g), ImVec2(xc.x + g, xc.y + g), xCol, 1.6f * s);
    dl->AddLine(ImVec2(xc.x - g, xc.y + g), ImVec2(xc.x + g, xc.y - g), xCol, 1.6f * s);

    // Route interactions (deferred).
    if (xClicked || middleClose) {
        if (t.dirty) s_confirmCloseIdx = idx;   // ask first
        else         s_closeIdx = idx;
    } else if (switchClicked && !active) {
        s_switchTo = idx;
    }

    ImGui::PopID();
    return tabW;
}

float drawDocTabs(AppState& st, float topY) {
    // Nothing to show in the empty start state (no document open).
    if (st.docTabs.empty()) return 0.0f;

    const auto& pal = ui::palette;
    float s = ui::uiScale;
    float barH = 26.0f * s;

    ImGuiViewport* vp = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(ImVec2(vp->Pos.x, vp->Pos.y + topY));
    ImGui::SetNextWindowSize(ImVec2(vp->Size.x, barH));
    ImGui::SetNextWindowViewport(vp->ID);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, 0);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(6.0f * s, 0));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(0, 0));
    ImGui::PushStyleColor(ImGuiCol_WindowBg,
        ImVec4(pal.windowBg[0], pal.windowBg[1], pal.windowBg[2], 1));
    ImGuiWindowFlags flags = ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
        ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse |
        ImGuiWindowFlags_NoDocking | ImGuiWindowFlags_NoSavedSettings |
        ImGuiWindowFlags_NoBringToFrontOnFocus | ImGuiWindowFlags_NoNavFocus |
        ImGuiWindowFlags_NoFocusOnAppearing;
    ImGui::Begin("##DocTabs", nullptr, flags);

    s_switchTo = -1;
    s_closeIdx = -1;
    s_newPart = false;

    // Explicit x-cursor placement (no SameLine/Dummy bookkeeping): each tab is drawn at the running
    // x and returns its width. The strip never wraps; very many tabs just clip at the window edge.
    ImVec2 origin = ImGui::GetCursorScreenPos();
    float x = origin.x;
    float y0 = origin.y;
    float gap = 2.0f * s;
    for (int i = 0; i < (int)st.docTabs.size(); ++i) {
        x += drawOneTab(st, i, x, y0, barH) + gap;
    }

    // "+" New Part button at the end of the strip.
    {
        float btn = barH;
        ImVec2 p0(x, y0);
        ImGui::SetCursorScreenPos(p0);
        bool clicked = ImGui::InvisibleButton("##newtab", ImVec2(btn, btn));
        bool hov = ImGui::IsItemHovered();
        ImDrawList* dl = ImGui::GetWindowDrawList();
        if (hov)
            dl->AddRectFilled(p0, ImVec2(p0.x + btn, p0.y + btn),
                              ui::Palette::toU32(pal.buttonHovered), 4.0f * s);
        ImVec2 c(p0.x + btn * 0.5f, p0.y + btn * 0.5f);
        float g = btn * 0.2f;
        ImU32 col = ImGui::ColorConvertFloat4ToU32(
            ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1));
        dl->AddLine(ImVec2(c.x - g, c.y), ImVec2(c.x + g, c.y), col, 1.6f * s);
        dl->AddLine(ImVec2(c.x, c.y - g), ImVec2(c.x, c.y + g), col, 1.6f * s);
        if (hov) ImGui::SetTooltip("New Part");
        if (clicked) s_newPart = true;
    }

    // ---- unsaved-changes confirmation prompt ----
    if (s_confirmCloseIdx >= 0) {
        ImGui::OpenPopup("Close document?##doctab");
    }
    bool open = true;
    // Center the modal.
    ImGui::SetNextWindowPos(ImVec2(vp->Pos.x + vp->Size.x * 0.5f, vp->Pos.y + vp->Size.y * 0.5f),
                            ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
    if (ImGui::BeginPopupModal("Close document?##doctab", &open,
                               ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoSavedSettings)) {
        int idx = s_confirmCloseIdx;
        std::string name = (idx >= 0 && idx < (int)st.docTabs.size())
                               ? st.docTabs[idx].title : std::string("this document");
        ImGui::Text("\"%s\" has unsaved changes.", name.c_str());
        ImGui::TextUnformatted("Close it anyway? Your edits will be lost.");
        ImGui::Spacing();
        if (ImGui::Button("Close without saving", ImVec2(180 * s, 0))) {
            s_closeIdx = s_confirmCloseIdx;
            s_confirmCloseIdx = -1;
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel", ImVec2(90 * s, 0))) {
            s_confirmCloseIdx = -1;
            ImGui::CloseCurrentPopup();
        }
        ImGui::EndPopup();
    }
    if (!open) s_confirmCloseIdx = -1;   // modal dismissed via the title-bar X

    ImGui::End();
    ImGui::PopStyleColor();
    ImGui::PopStyleVar(4);

    // ---- apply deferred actions (after all tabs were drawn) ----
    // A close that targets the active tab leaves the mirror on a neighbor, so handle close before
    // switch; only one of these fires per frame in practice.
    if (s_closeIdx >= 0) {
        st.closeDocTab(s_closeIdx);
        s_closeIdx = -1;
    } else if (s_switchTo >= 0) {
        st.switchToTab(s_switchTo);
        s_switchTo = -1;
    }
    if (s_newPart) {
        s_newPart = false;
        std::string err;
        // newPart fires the mirror listener (a doc change, not an edit); suppress the dirty marker
        // so the new tab starts clean.
        st.suppressTabDirty = true;
        bool ok = st.docMirror.newPart(st.worker, err);
        st.suppressTabDirty = false;
        if (ok) {
            st.addLog("[tabs] new part  rev=" + std::to_string(st.docMirror.revision()));
            st.clearViewportForNewDoc();
            st.addDocTab(/*isAssembly=*/false);
        } else {
            st.addLog("[err] new part: " + err);
            st.toast(ui::ToastLevel::Error, "New part failed: " + err);
        }
    }

    return barH;
}

}  // namespace ui
