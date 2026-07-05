#include "ui/doc_tab_strip.h"
#include "app/app_state.h"
#include "ui/command_registry.h"
#include "theme/theme.h"
#include "theme/icons.h"
#include "theme/tokens.h"
#include "imgui.h"
#include "imgui_internal.h"
#include <string>

namespace ui {

using app::AppState;
using core::DocTab;
using core::DocTabKind;

namespace {

enum class NewDocKind { None = -1, Part = 0, Assembly, Drawing, Schematic, PCB };

static int  s_switchTo = -1;
static int  s_closeIdx = -1;
static NewDocKind s_newDoc = NewDocKind::None;
static int  s_confirmCloseIdx = -1;
static int  s_saveCloseIdx = -1;
static bool s_openChooser = false;

const char* docIconName(const DocTab& t) {
    switch (t.kind) {
        case DocTabKind::Assembly: return "assembly";
        case DocTabKind::Drawing: return "section_props";
        case DocTabKind::Schematic:
        case DocTabKind::Pcb: return "ai_panel";
        default: return "part";
    }
}

static void closeDocTabAt(AppState& st, int idx) {
    if (idx < 0 || idx >= (int)st.docTabs.size()) return;
    const bool wasActive = (idx == st.activeTab);
    st.docTabs.erase(st.docTabs.begin() + idx);
    if (st.docTabs.empty()) {
        st.activeTab = -1;
        st.docs.clear();
        st.syncDocsFromTabs();
        st.syncViewModel();
        return;
    }
    if (st.activeTab > idx) --st.activeTab;
    else if (st.activeTab >= (int)st.docTabs.size()) st.activeTab = (int)st.docTabs.size() - 1;
    if (wasActive) st.switchToTab(st.activeTab);
    else {
        st.syncDocsFromTabs();
        st.syncViewModel();
    }
}

static float drawOneTab(AppState& st, int idx, float x, float y0, float barH,
                        const theme::AdaptiveSurface& surf) {
    const float S = theme::S();
    DocTab& t = st.docTabs[idx];
    const bool active = (idx == st.activeTab);

    ImGui::PushID(idx);

    std::string label = t.displayTitle();
    if (t.dirty) label += " *";

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float padX = theme::U(1.5f);
    const float closeW = 18.f * S;
    const float iconSz = 16.f * S;
    const float iconGap = theme::U(0.75f);
    const float textW = ImGui::CalcTextSize(label.c_str()).x;
    const float tabW = padX + iconSz + iconGap + textW + 6.f * S + closeW + padX * 0.5f;
    const float tabH = barH;

    const ImVec2 p0(x, y0);
    const ImVec2 p1(p0.x + tabW, p0.y + tabH);
    const ImRect tabRect(p0, p1);

    ImGui::SetCursorScreenPos(p0);
    ImGui::SetNextItemAllowOverlap();
    const bool switchClicked = ImGui::InvisibleButton("##tab", ImVec2(tabW, tabH));
    const bool hovered = ImGui::IsItemHovered();
    const bool middleClose = hovered && ImGui::IsMouseClicked(ImGuiMouseButton_Middle);

    theme::MaterialLevel level = active ? theme::MaterialLevel::Regular : theme::MaterialLevel::UltraThin;
    if (hovered && !active) level = theme::MaterialLevel::Thin;
    theme::glassPanel(dl, tabRect, level, theme::ctx().m.rounding, surf);
    if (active) {
        dl->AddLine(ImVec2(p0.x + theme::U(0.25f), p0.y + theme::U(0.125f)),
                    ImVec2(p1.x - theme::U(0.25f), p0.y + theme::U(0.125f)),
                    TCOL(accent), 2.f * S);
    } else {
        dl->AddLine(ImVec2(p1.x - 0.5f, p0.y + 4.f * S), ImVec2(p1.x - 0.5f, p1.y - 4.f * S),
                    TCOL(line), 1.f);
    }

    const ImU32 textCol = ImGui::ColorConvertFloat4ToU32(active ? surf.textPrimary : surf.textSecondary);
    const float iy = p0.y + (tabH - iconSz) * 0.5f;
    icons::drawGlass(dl, docIconName(t), ImVec2(p0.x + padX + iconSz * 0.5f, iy + iconSz * 0.5f),
                     iconSz * 0.5f, textCol,
                     theme::roleU32A(surf.darkText ? theme::pal().vibrancyLightPrimary
                                                   : theme::pal().vibrancyDarkPrimary, 0.31f));
    dl->AddText(ImVec2(p0.x + padX + iconSz + iconGap,
                       p0.y + (tabH - ImGui::GetFontSize()) * 0.5f),
                textCol, label.c_str());

    if (hovered && !t.path.empty())
        ImGui::SetTooltip("%s%s", t.path.c_str(), t.dirty ? "  (unsaved changes)" : "");
    else if (hovered && t.dirty)
        ImGui::SetTooltip("Unsaved changes");

    const float cx = p1.x - closeW - padX * 0.4f;
    ImGui::SetCursorScreenPos(ImVec2(cx, p0.y + (tabH - closeW) * 0.5f));
    const bool xClicked = ImGui::InvisibleButton("##x", ImVec2(closeW, closeW));
    const bool xHover = ImGui::IsItemHovered();
    const ImVec2 xc(cx + closeW * 0.5f, p0.y + tabH * 0.5f);
    if (xHover)
        dl->AddCircleFilled(xc, closeW * 0.5f, TCOL(bad));
    const float g = closeW * 0.22f;
    const ImU32 xCol = ImGui::ColorConvertFloat4ToU32(xHover ? surf.textPrimary : surf.textTertiary);
    dl->AddLine(ImVec2(xc.x - g, xc.y - g), ImVec2(xc.x + g, xc.y + g), xCol, 1.6f * S);
    dl->AddLine(ImVec2(xc.x - g, xc.y + g), ImVec2(xc.x + g, xc.y - g), xCol, 1.6f * S);

    if (xClicked || middleClose) {
        if (t.dirty) s_confirmCloseIdx = idx;
        else s_closeIdx = idx;
    } else if (switchClicked && !active) {
        s_switchTo = idx;
    }

    ImGui::PopID();
    return tabW;
}

}  // namespace

float docTabStripHeight(const AppState& st) {
    (void)st;
    return 26.f * theme::S();
}

TabSaveResult saveDocTab(AppState& st, int idx, bool forceDialog) {
    if (idx < 0 || idx >= (int)st.docTabs.size()) return TabSaveResult::Nothing;
    DocTab& t = st.docTabs[idx];

    if (t.kind == DocTabKind::Part || t.kind == DocTabKind::Assembly) {
        const int prev = st.activeTab;
        st.switchToTab(idx);
        st.doSave(forceDialog || t.path.empty());
        if (st.activeTab != prev && prev >= 0) st.switchToTab(prev);
        if (!t.dirty && !t.path.empty()) return TabSaveResult::Saved;
        if (forceDialog && t.path.empty()) return TabSaveResult::Cancelled;
        return t.dirty ? TabSaveResult::Failed : TabSaveResult::Nothing;
    }

    if (t.kind == DocTabKind::Drawing || t.kind == DocTabKind::Schematic || t.kind == DocTabKind::Pcb)
        return TabSaveResult::Nothing;

    return TabSaveResult::Nothing;
}

float drawDocTabs(AppState& st, float availW) {
    const float S = theme::S();
    const float barH = 26.f * S;

    s_switchTo = -1;
    s_closeIdx = -1;
    s_saveCloseIdx = -1;
    s_newDoc = NewDocKind::None;

    const ImVec2 origin = ImGui::GetCursorScreenPos();
    const ImGuiStyle& style = ImGui::GetStyle();

    float bandT = origin.y;
    const float bandL = origin.x;
    const float bandR = origin.x + availW;
    if (ImGuiDockNode* node = ImGui::GetWindowDockNode()) bandT = node->Pos.y;
    const float bandB = origin.y + barH + style.ItemSpacing.y;

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImRect bandRect(ImVec2(bandL, bandT), ImVec2(bandR, bandB));
    const theme::AdaptiveSurface& surf =
        theme::adaptivePanel(theme::panelId("doctabs_glass"), bandRect);
    theme::glassPanel(dl, bandRect, theme::MaterialLevel::Thin, theme::ctx().m.roundingLg, surf);
    dl->AddLine(ImVec2(bandL, bandB - 0.5f), ImVec2(bandR, bandB - 0.5f), TCOL(line), 1.f);

    float runX = origin.x;
    const float y0 = origin.y;
    const float gap = 2.f * S;
    for (int i = 0; i < (int)st.docTabs.size(); ++i)
        runX += drawOneTab(st, i, runX, y0, barH, surf) + gap;

    {
        const float btn = barH;
        const ImVec2 p0(runX, y0);
        ImGui::SetCursorScreenPos(p0);
        const bool clicked = ImGui::InvisibleButton("##newtab", ImVec2(btn, btn));
        const bool hov = ImGui::IsItemHovered();
        if (hov) {
            const ImRect pill(p0, ImVec2(p0.x + btn, p0.y + btn));
            theme::glassPanel(dl, pill, theme::MaterialLevel::UltraThin, theme::ctx().m.rounding, surf);
        }
        icons::drawGlass(dl, "new_doc", ImVec2(p0.x + btn * 0.5f, p0.y + btn * 0.5f), 9.f * S,
                         ImGui::ColorConvertFloat4ToU32(surf.textSecondary),
                         theme::roleU32A(surf.darkText ? theme::pal().vibrancyLightPrimary
                                                       : theme::pal().vibrancyDarkPrimary, 0.31f));
        if (hov) ImGui::SetTooltip("New document");
        if (clicked || s_openChooser) {
            ImGui::OpenPopup("##newdocmenu");
            s_openChooser = false;
        }
    }

    if (ImGui::BeginPopup("##newdocmenu")) {
        if (ImGui::MenuItem("Part")) s_newDoc = NewDocKind::Part;
        if (ImGui::MenuItem("Assembly")) s_newDoc = NewDocKind::Assembly;
        if (ImGui::MenuItem("Drawing")) s_newDoc = NewDocKind::Drawing;
        ImGui::Separator();
        if (ImGui::MenuItem("Schematic")) s_newDoc = NewDocKind::Schematic;
        if (ImGui::MenuItem("PCB")) s_newDoc = NewDocKind::PCB;
        ImGui::EndPopup();
    }

    if (s_confirmCloseIdx >= 0) ImGui::OpenPopup("Close document?##doctab");

    ImGuiViewport* vp = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(ImVec2(vp->Pos.x + vp->Size.x * 0.5f, vp->Pos.y + vp->Size.y * 0.5f),
                            ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
    bool open = true;
    if (ImGui::BeginPopupModal("Close document?##doctab", &open,
                               ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoSavedSettings)) {
        const int idx = s_confirmCloseIdx;
        const std::string name = (idx >= 0 && idx < (int)st.docTabs.size())
                                     ? st.docTabs[idx].displayTitle()
                                     : std::string("this document");
        ImGui::Text("\"%s\" has unsaved changes.", name.c_str());
        ImGui::TextUnformatted("Save your edits before closing?");
        ImGui::Spacing();
        if (ImGui::Button("Save", ImVec2(theme::U(15.f), 0))) {
            s_saveCloseIdx = s_confirmCloseIdx;
            s_confirmCloseIdx = -1;
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Don't Save", ImVec2(theme::U(15.f), 0))) {
            s_closeIdx = s_confirmCloseIdx;
            s_confirmCloseIdx = -1;
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel", ImVec2(theme::U(11.f), 0))) {
            s_confirmCloseIdx = -1;
            ImGui::CloseCurrentPopup();
        }
        ImGui::EndPopup();
    }
    if (!open) s_confirmCloseIdx = -1;

    ImGui::SetCursorScreenPos(origin);
    ImGui::Dummy(ImVec2(availW, barH));

    if (s_saveCloseIdx >= 0) {
        const int idx = s_saveCloseIdx;
        s_saveCloseIdx = -1;
        const TabSaveResult r = saveDocTab(st, idx, false);
        if (r != TabSaveResult::Cancelled && r != TabSaveResult::Failed) closeDocTabAt(st, idx);
    } else if (s_closeIdx >= 0) {
        closeDocTabAt(st, s_closeIdx);
        s_closeIdx = -1;
    } else if (s_switchTo >= 0) {
        st.switchToTab(s_switchTo);
        s_switchTo = -1;
    }
    if (s_newDoc != NewDocKind::None) {
        const NewDocKind kind = s_newDoc;
        s_newDoc = NewDocKind::None;
        createNewDoc(st, (int)kind);
    }

    return barH;
}

void createNewDoc(AppState& st, int kindOrd) {
    const NewDocKind kind = (NewDocKind)kindOrd;

    if (kind == NewDocKind::Part) {
        if (!st.engineReady) { st.status = "Engine offline"; return; }
        st.suppressTabDirty = true;
        if (st.engine.newPart("Part")) {
            st.addDocTab(false);
            st.buildTreeFromDoc();
            st.status = "New part";
        } else {
            st.status = std::string("New failed: ") + st.engine.lastError();
        }
        st.suppressTabDirty = false;
        return;
    }

    if (kind == NewDocKind::Assembly) {
        DocTab t;
        t.kind = DocTabKind::Assembly;
        t.isAssembly = true;
        t.title = "Assembly" + std::to_string(st.docTabs.size() + 1);
        st.docTabs.push_back(t);
        st.activeTab = (int)st.docTabs.size() - 1;
        st.workspace = cmd::Workspace::Assemble;
        st.syncDocsFromTabs();
        st.syncViewModel();
        st.status = "New assembly tab";
        return;
    }

    if (kind == NewDocKind::Drawing) {
        st.dispatch("make_drawing");
        core::DocTab t;
        t.kind = DocTabKind::Drawing;
        t.title = "Drawing" + std::to_string(st.docTabs.size() + 1);
        st.docTabs.push_back(t);
        st.activeTab = (int)st.docTabs.size() - 1;
        st.syncDocsFromTabs();
        return;
    }

    if (kind == NewDocKind::Schematic) {
        st.dispatch("ecad_new_sch");
        core::DocTab t;
        t.kind = DocTabKind::Schematic;
        t.title = "Schematic" + std::to_string(st.docTabs.size() + 1);
        st.docTabs.push_back(t);
        st.activeTab = (int)st.docTabs.size() - 1;
        st.syncDocsFromTabs();
        return;
    }

    if (kind == NewDocKind::PCB) {
        st.dispatch("ecad_new_pcb");
        core::DocTab t;
        t.kind = DocTabKind::Pcb;
        t.title = "Board" + std::to_string(st.docTabs.size() + 1);
        st.docTabs.push_back(t);
        st.activeTab = (int)st.docTabs.size() - 1;
        st.syncDocsFromTabs();
    }
}

void openNewDocChooser() { s_openChooser = true; }

}  // namespace ui
