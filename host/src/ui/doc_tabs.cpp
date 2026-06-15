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
#include "ecad/ecad_panel.h"  // Wave 2: New Schematic/PCB feed the editor mirror (ecadEditorMirror)
#include "ecad/ecad_mirror.h" // Wave 2: complete EcadMirror type for setDocId in the chooser path

namespace ui {

// Deferred actions, applied after the strip finishes drawing (see header note).
static int  s_switchTo   = -1;   // tab index to activate, or -1
static int  s_closeIdx   = -1;   // tab index to close, or -1
// Wave 2: the "+" New-Document chooser. The clicked menu item records which kind of doc to create;
// the actual create+addTab runs after the strip is drawn (deferred, like switch/close), so it never
// mutates docTabs mid-iteration. -1 = nothing requested this frame.
enum class NewDocKind { None = -1, Part = 0, Assembly, Drawing, Schematic, PCB };
static NewDocKind s_newDoc = NewDocKind::None;
// Unsaved-changes prompt: the tab the user asked to close while it had unsaved edits.
static int  s_confirmCloseIdx = -1;
// Wave 2: set true to request the New-Document chooser popup open this frame (the "+" button click,
// or a Ctrl+N hook from app.cpp via openNewDocChooser()). Consumed inside drawDocTabs.
static bool s_openChooser = false;

// Forward decls (definitions below drawDocTabs, which calls them).
void createNewDoc(AppState& st, int kindOrd);
void openNewDocChooser();

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
    s_newDoc = NewDocKind::None;

    // Explicit x-cursor placement (no SameLine/Dummy bookkeeping): each tab is drawn at the running
    // x and returns its width. The strip never wraps; very many tabs just clip at the window edge.
    ImVec2 origin = ImGui::GetCursorScreenPos();
    float x = origin.x;
    float y0 = origin.y;
    float gap = 2.0f * s;
    for (int i = 0; i < (int)st.docTabs.size(); ++i) {
        x += drawOneTab(st, i, x, y0, barH) + gap;
    }

    // "+" New-Document button at the end of the strip — opens a chooser popup (Wave 2). Clicking it
    // (or Ctrl+N via openNewDocChooser()) opens "##newdocmenu"; each item records a deferred
    // NewDocKind applied after the strip is drawn (so docTabs isn't mutated mid-iteration).
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
        if (hov) ImGui::SetTooltip("New document");
        if (clicked || s_openChooser) { ImGui::OpenPopup("##newdocmenu"); s_openChooser = false; }
        // Anchor the popup just under the "+" button.
        ImGui::SetNextWindowPos(ImVec2(p0.x, p0.y + btn));
        if (ImGui::BeginPopup("##newdocmenu")) {
            if (ImGui::MenuItem("New Part"))      s_newDoc = NewDocKind::Part;
            if (ImGui::MenuItem("New Assembly"))  s_newDoc = NewDocKind::Assembly;
            // Make Drawing needs an active PART/ASM tab to project from; gray it out otherwise.
            const bool canDraw = st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
                                 (st.docTabs[st.activeTab].kind == AppState::DocTab::TabKind::Part ||
                                  st.docTabs[st.activeTab].kind == AppState::DocTab::TabKind::Assembly);
            if (ImGui::MenuItem("New Drawing", nullptr, false, canDraw)) s_newDoc = NewDocKind::Drawing;
            ImGui::Separator();
            if (ImGui::MenuItem("New Schematic")) s_newDoc = NewDocKind::Schematic;
            if (ImGui::MenuItem("New PCB"))       s_newDoc = NewDocKind::PCB;
            ImGui::EndPopup();
        }
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
    if (s_newDoc != NewDocKind::None) {
        const NewDocKind kind = s_newDoc;
        s_newDoc = NewDocKind::None;
        createNewDoc(st, (int)kind);
    }

    return barH;
}

// Wave 2: New-Document chooser action — create the worker doc + open a matching tab. Part/Assembly
// go through DocMirror (with the dirty-suppress discipline) + addDocTab; Drawing reuses makeDrawing;
// Schematic/PCB call ecad_new directly and addEcadTab, feeding the editor mirror via the public ECAD
// API. `kind` is a NewDocKind ordinal (0=Part,1=Assembly,2=Drawing,3=Schematic,4=PCB). Kept as a free
// function (not a static) so app.cpp's Ctrl+N hook can request it via openNewDocChooser() instead.
void createNewDoc(AppState& st, int kindOrd) {
    const NewDocKind kind = (NewDocKind)kindOrd;
    if (kind == NewDocKind::Part || kind == NewDocKind::Assembly) {
        const bool asm_ = (kind == NewDocKind::Assembly);
        std::string err;
        // new{Part,Assembly} fires the mirror listener (a doc change, not an edit); suppress the
        // dirty marker so the new tab starts clean.
        st.suppressTabDirty = true;
        bool ok = asm_ ? st.docMirror.newAssembly(st.worker, err)
                       : st.docMirror.newPart(st.worker, err);
        st.suppressTabDirty = false;
        if (ok) {
            st.addLog(std::string("[tabs] new ") + (asm_ ? "assembly" : "part") +
                      "  rev=" + std::to_string(st.docMirror.revision()));
            st.clearViewportForNewDoc();
            st.addDocTab(/*isAssembly=*/asm_);
            if (asm_) { st.showRibbon = true; st.ribbonTab = AppState::RT_Assembly; }
        } else {
            st.addLog(std::string("[err] new ") + (asm_ ? "assembly" : "part") + ": " + err);
            st.toast(ui::ToastLevel::Error,
                     std::string("New ") + (asm_ ? "assembly" : "part") + " failed: " + err);
        }
        return;
    }
    if (kind == NewDocKind::Drawing) {
        st.makeDrawing();   // guards on an active part/asm tab itself + toasts if none
        return;
    }
    if (kind == NewDocKind::Schematic || kind == NewDocKind::PCB) {
        const char* ek = (kind == NewDocKind::PCB) ? "pcb" : "sch";
        nlohmann::json res;
        std::string err;
        if (!st.worker.call("ecad_new", {{"kind", ek}}, res, err) ||
            (res.is_object() && res.contains("code"))) {
            const std::string msg = err.empty() && res.is_object()
                                        ? res.value("message", std::string("engine error")) : err;
            st.addLog(std::string("[err] new ecad ") + ek + ": " + msg);
            st.toast(ui::ToastLevel::Error, std::string("New ") +
                     (kind == NewDocKind::PCB ? "PCB" : "schematic") + " failed: " + msg);
            return;
        }
        const std::string id = res.value("doc_id", std::string());
        const int rev = (res.contains("revision") && res["revision"].is_number_integer())
                            ? res["revision"].get<int>() : 0;
        // Open the tab FIRST so it becomes active — the ECAD editor's state() resolver then targets
        // this tab's per-document EcadEditorState before we load the worker response into the mirror.
        st.addEcadTab(ek, id, rev, /*title=*/std::string());
        if (!id.empty()) ui::ecadEditorMirror().setDocId(id);
        ui::ecadEditorLoadResponse(res);   // {revision,kind,tree,compile} -> active tab's mirror
        st.toast(ui::ToastLevel::Info,
                 std::string("New ECAD ") + (kind == NewDocKind::PCB ? "board" : "schematic"));
        return;
    }
}

// Wave 2: request the New-Document chooser popup open on the next strip draw (e.g. a Ctrl+N hook in
// app.cpp). Safe to call anytime; the flag is consumed inside drawDocTabs.
void openNewDocChooser() { s_openChooser = true; }

}  // namespace ui
