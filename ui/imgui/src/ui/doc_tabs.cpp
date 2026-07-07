#include "ui/doc_tab_strip.h"
#include "app/app_state.h"
#include "ui/command_registry.h"
#include "ui/cam/cam_panel.h"   // camActivateForPart / camHasJobForPart (section switcher)
#include "theme/theme.h"
#include "theme/icons.h"
#include "theme/tokens.h"
#include "imgui.h"
#include "imgui_internal.h"
#include <cmath>
#include <cstring>
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

    // T-DP13: tick per-tab agent spinner animation.
    const float dt = ImGui::GetIO().DeltaTime;
    if (t.agentRunning) t.agentSpinT += dt;

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float padX = theme::U(1.5f);
    const float closeW = 18.f * S;
    const float iconSz = 16.f * S;
    const float iconGap = theme::U(0.75f);
    const float textW = ImGui::CalcTextSize(label.c_str()).x;
    // T-DP13: reserve space for the live activity dot when an agent is running.
    const float agentDotDiam = 7.f * S;
    const float agentDotGap  = 4.f * S;
    const float agentExtraW  = t.agentRunning ? (agentDotGap + agentDotDiam) : 0.f;
    const float tabW = padX + iconSz + iconGap + textW + agentExtraW + 6.f * S + closeW + padX * 0.5f;
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

    // T-DP13: live agent activity indicator — pulsing dot in the tab when an agent
    // holds a write lease on this doc.  Uses the same accent-dot vocabulary as the
    // Copilot header (agent_panel.cpp drawCopilotHeader).
    if (t.agentRunning) {
        const float dotR   = agentDotDiam * 0.5f;
        const float dotCx  = p0.x + padX + iconSz + iconGap + textW + agentDotGap + dotR;
        const float dotCy  = p0.y + tabH * 0.5f;
        // Pulse: brightness oscillates on a ~1.2 s cycle.
        const float pulse  = 0.55f + 0.45f * std::sin(t.agentSpinT * 5.2f);
        const ImU32 dotCol = theme::roleU32(theme::pal().accent, pulse);
        dl->AddCircleFilled(ImVec2(dotCx, dotCy), dotR, dotCol);
    }

    if (hovered && t.agentRunning && !t.agentOwner.empty()) {
        // Attribution tooltip: agent role + model when available.
        if (!t.agentModel.empty())
            ImGui::SetTooltip("Agent: %s\nModel: %s", t.agentOwner.c_str(), t.agentModel.c_str());
        else
            ImGui::SetTooltip("Agent: %s", t.agentOwner.c_str());
    } else if (hovered && !t.path.empty())
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
    // Doc-less global full-viewport workspaces (AiAgent, PLM) have no document, so the
    // doc-tab strip would render as an empty band above the shell — suppress it there.
    // (ECAD Schematic/Board + Sketch3D are doc-scoped and keep their tabs.)
    // st.workspace (not effectiveWorkspace()) — the latter is non-const; AiAgent/PLM are
    // never the inSketch case so the raw workspace is equivalent here.
    const cmd::Workspace ws = st.workspace;
    if (ws == cmd::Workspace::AiAgent || ws == cmd::Workspace::PlmManagement) return 0.f;
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

    // Section-view switcher: right-aligned chip row for linked views of the active part.
    drawSectionSwitcher(st, bandR, y0, barH);

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

// ---- Section-view switcher ---------------------------------------------------
// Compact right-aligned chip row in the doc tab strip showing linked views of the
// active part: Model | CAM Job | Sim Study | Drawing. Switching clicks change both
// the active tab and the workspace together so the user lands in the right context.
//
// Link sources (what IS enumerable today):
//   - Drawing: DocTab::drawingDocId — a Drawing tab whose drawingDocId == active part docId
//   - CAM Job: st.camActive — the CAM manager is live for this doc
//   - Sim Study: workspace==Simulate means a study exists/is active for this doc
//   - Model: the Part/Assembly tab itself (always present when a part is open)
//
// When a true linked-doc graph exists (core/doc), expand from tabIndexForDocId to that graph.
// Returns the width consumed (placed right-of-tabs, right-aligned to bandR).
float drawSectionSwitcher(AppState& st, float bandR, float y0, float barH) {
    if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) return 0.f;
    const DocTab& active = st.docTabs[st.activeTab];
    // Only show for Part and Assembly docs (drawings/ECAD/etc. have no linked views yet).
    if (active.kind != DocTabKind::Part && active.kind != DocTabKind::Assembly) return 0.f;

    const float S = theme::S();
    ImDrawList* dl = ImGui::GetWindowDrawList();

    // Gather link entries: {label, icon, tabIdx (-1=ws-only), targetWorkspace}
    struct Entry {
        const char* label;
        const char* icon;
        int   tabIdx;         // -1 = no tab switch, just workspace switch
        cmd::Workspace ws;
        bool  isCurrent;      // true = this view is already active
    };
    Entry entries[5];   // Model | CAM Job | Sim Study | Drawing | Board
    int nEntries = 0;

    // 1. Model (always — the Part/Assembly tab itself)
    {
        const bool cur = (st.workspace != cmd::Workspace::Simulate &&
                          !st.camActive &&
                          st.workspace != cmd::Workspace::Drafting);
        entries[nEntries++] = { active.kind == DocTabKind::Assembly ? "Asm" : "Model",
                                active.kind == DocTabKind::Assembly ? "assembly" : "ws_model",
                                st.activeTab, cmd::Workspace::Model, cur };
    }

    // 2. CAM Job — always show for Part/Assembly (creates on demand via camActivateForPart).
    // Prior behaviour gated on camActive — circular: the user can't see the chip to activate.
    // camHasJobForPart() returns true always (any part can have a CAM job created on demand);
    // a future pass can check the dep registry for an existing job and show a filled vs. outline icon.
    if (isCadTab(active)) {
        const bool camCurrent = st.camActive;
        entries[nEntries++] = { "CAM Job", "ws_model",
                                st.activeTab, cmd::Workspace::Manufacture, camCurrent };
        if (camCurrent) {
            // Mark Model entry as non-current since CAM takeover is active
            entries[0].isCurrent = false;
        }
    }

    // 3. Sim Study (present when workspace is Simulate for this doc)
    if (st.workspace == cmd::Workspace::Simulate) {
        // Mark Model (entry[0]) and CAM Job (entry[1]) as non-current — Sim Study is active.
        entries[0].isCurrent = false;
        if (nEntries > 1) entries[1].isCurrent = false;
        entries[nEntries++] = { "Sim Study", "sim_static",
                                st.activeTab, cmd::Workspace::Simulate, true };
    }

    // 4. Drawing (find co-open Drawing tab whose drawingDocId matches active docId)
    {
        int dwgTab = -1;
        if (!active.docId.empty()) {
            for (int i = 0; i < (int)st.docTabs.size(); ++i) {
                if (st.docTabs[i].kind == DocTabKind::Drawing &&
                    st.docTabs[i].drawingDocId == active.docId) {
                    dwgTab = i;
                    break;
                }
            }
        }
        if (dwgTab >= 0) {
            const bool cur = (st.activeTab == dwgTab && st.workspace == cmd::Workspace::Drafting);
            entries[nEntries++] = { "Drawing", "section_props", dwgTab, cmd::Workspace::Drafting, cur };
        }
    }

    // 5. Board (find co-open PCB tab that was generated FROM this Part via ecadHandleTo3d).
    // The Part tab's ecadDocId stores the Board docId; we find the PCB tab by docId match.
    // Mirrors the Drawing/drawingDocId pattern. Shared-file edit #3 from workspace_ecad.cpp.
    {
        int boardTab = -1;
        if (!active.ecadDocId.empty()) {
            for (int i = 0; i < (int)st.docTabs.size(); ++i) {
                if (st.docTabs[i].kind == DocTabKind::Pcb &&
                    st.docTabs[i].docId == active.ecadDocId) {
                    boardTab = i;
                    break;
                }
            }
        }
        if (boardTab >= 0) {
            const bool cur = (st.activeTab == boardTab &&
                              (st.workspace == cmd::Workspace::Board ||
                               st.workspace == cmd::Workspace::Schematic));
            entries[nEntries++] = { "Board", "ws_board", boardTab, cmd::Workspace::Board, cur };
        }
    }

    if (nEntries <= 1) return 0.f;  // only "Model" itself — nothing to switch to

    // Measure total width of the switcher block to right-align it.
    ImFont* f = theme::font(theme::Font::Label);
    const float chipH = barH * 0.76f;
    const float icSz  = 12.f * S;
    const float padX  = 8.f * S;
    const float gap   = 3.f * S;
    float totalW = padX;  // leading margin
    for (int i = 0; i < nEntries; ++i) {
        ImVec2 ts = f->CalcTextSizeA(f->FontSize, FLT_MAX, 0, entries[i].label);
        totalW += icSz + 5.f * S + ts.x + padX * 1.6f + gap;
    }
    totalW += padX * 0.5f;  // trailing margin

    float startX = bandR - totalW;
    const float chipY  = y0 + (barH - chipH) * 0.5f;

    // Separator line left of the switcher block
    dl->AddLine(ImVec2(startX - padX * 0.5f, y0 + 4.f * S),
                ImVec2(startX - padX * 0.5f, y0 + barH - 4.f * S),
                TCOL(line), ImMax(1.f, S));

    // Resolve glass surface (same adaptive surface as the band, already computed this frame)
    const ImRect bandRect(ImVec2(startX, y0), ImVec2(bandR, y0 + barH));
    const theme::AdaptiveSurface& surf =
        theme::adaptivePanel(theme::panelId("doctabs_glass"), bandRect);

    float x = startX + padX * 0.5f;
    for (int i = 0; i < nEntries; ++i) {
        const Entry& e = entries[i];
        ImVec2 ts = f->CalcTextSizeA(f->FontSize, FLT_MAX, 0, e.label);
        const float chipW = icSz + 5.f * S + ts.x + padX * 1.6f;
        const ImVec2 p0(x, chipY);
        const ImVec2 p1(x + chipW, chipY + chipH);

        ImGui::SetCursorScreenPos(p0);
        char bid[32]; std::snprintf(bid, sizeof(bid), "##svsw%d", i);
        const bool clicked = ImGui::InvisibleButton(bid, ImVec2(chipW, chipH));
        const bool hov     = ImGui::IsItemHovered();

        // Background pill
        const float r = chipH * 0.45f;
        if (e.isCurrent) {
            dl->AddRectFilled(p0, p1, TCOL(accent), r);
        } else if (hov) {
            theme::glassPanel(dl, ImRect(p0, p1), theme::MaterialLevel::UltraThin, r / S, surf);
        }

        // Icon + label
        const ImU32 fg = e.isCurrent
            ? TCOL(onAccent)
            : ImGui::ColorConvertFloat4ToU32(hov ? surf.textPrimary : surf.textSecondary);
        icons::draw(dl, e.icon,
                    ImVec2(x + padX * 0.8f + icSz * 0.5f, chipY + chipH * 0.5f),
                    icSz, fg);
        dl->AddText(f, f->FontSize,
                    ImVec2(x + padX * 0.8f + icSz + 5.f * S, chipY + (chipH - f->FontSize) * 0.5f),
                    fg, e.label);

        if (hov) ImGui::SetTooltip("%s", e.label);

        if (clicked && !e.isCurrent) {
            st.activeTool.clear();
            if (std::strcmp(e.label, "CAM Job") == 0) {
                // CAM Job: use camActivateForPart() to find/create the job and set workspace.
                // This handles the full setup (job creation + sourcePartDocId + workspace switch).
                ui::camActivateForPart(st, active.docId);
            } else {
                // All other entries: switch tab (if needed) + workspace.
                if (e.tabIdx >= 0 && e.tabIdx != st.activeTab)
                    s_switchTo = e.tabIdx;
                st.workspace = e.ws;
                st.camActive = false;
            }
        }

        x += chipW + gap;
    }

    return totalW;
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
