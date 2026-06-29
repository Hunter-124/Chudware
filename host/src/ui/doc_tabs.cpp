// Document tab strip — see doc_tabs.h. Renders the open documents as browser-style tabs and routes
// click→switch, X/middle-click→close (with an unsaved-changes prompt), and "+"→New chooser. All
// mutations to AppState::docTabs are DEFERRED until after the strip is fully drawn (we iterate the
// vector while drawing), mirroring the feature tree's s_pendingTreeOps discipline so a switch/close
// never invalidates the loop it fires from.
#include <cstdio>
#include <string>

#include "imgui.h"
#include "imgui_internal.h"   // ImGuiDockNode + GetWindowDockNode — the doc-tab band spans the node

#include "doc_tabs.h"
#include "feature_tree.h"   // featureTreeSaveDoc — save-then-close from the unsaved-changes prompt
#include "file_dialog.h"    // saveFileDialog — ECAD Save-As from the unsaved-changes prompt
#include "icons.h"
#include "metrics.h"

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
// Save-then-close: the tab to save and then close (the "Save" choice on the unsaved-changes prompt).
// Applied after the strip is drawn (like s_closeIdx), so it never mutates docTabs mid-iteration.
static int  s_saveCloseIdx = -1;
// Wave 2: set true to request the New-Document chooser popup open this frame (the "+" button click,
// or a Ctrl+N hook from app.cpp via openNewDocChooser()). Consumed inside drawDocTabs.
static bool s_openChooser = false;

// Forward decls (definitions below drawDocTabs, which calls them).
void createNewDoc(AppState& st, int kindOrd);
void openNewDocChooser();

static const char* docIconName(const AppState::DocTab& t) {
    using K = AppState::DocTab::TabKind;
    switch (t.kind) {
        case K::Assembly: return "assembly";
        case K::Drawing: return "section_props";
        case K::Schematic: return "ai_panel";
        case K::PCB: return "ai_panel";
        case K::Part:
        default: return "part";
    }
}

// A single tab, placed explicitly at screen position (x, y0). Returns the tab WIDTH so the caller
// can advance to the next tab. Records a deferred switch/close into the file statics. Layout:
// [ title (dirty *) × ]. The whole pill switches; the × hot-zone (submitted on top) closes.
static float drawOneTab(AppState& st, int idx, float x, float y0, float barH) {
    const auto& pal = ui::palette;
    AppState::DocTab& t = st.docTabs[idx];
    const bool active = (idx == st.activeTab);
    float s = ui::uiScale;

    ImGui::PushID(idx);

    std::string label = t.displayTitle();   // filename-first (real file name, not internal "Part1")
    if (t.dirty) label += " *";

    ImDrawList* dl = ImGui::GetWindowDrawList();
    float padX = 12.0f * s;
    float closeW = 18.0f * s;
    float iconSz = 16.0f * s;
    float iconGap = 6.0f * s;
    float textW = ImGui::CalcTextSize(label.c_str()).x;
    float tabW = padX + iconSz + iconGap + textW + 6.0f * s + closeW + padX * 0.5f;
    float tabH = barH;

    ImVec2 p0(x, y0);
    ImVec2 p1(p0.x + tabW, p0.y + tabH);

    // Whole-pill click target (switch). The × button sits on top and consumes its own clicks — but
    // ImGui occludes a later item by an earlier overlapping one unless the underlying item opts in,
    // so allow the pill to be overlapped or the × (submitted below) never receives its clicks.
    ImGui::SetCursorScreenPos(p0);
    ImGui::SetNextItemAllowOverlap();
    bool switchClicked = ImGui::InvisibleButton("##tab", ImVec2(tabW, tabH));
    bool hovered = ImGui::IsItemHovered();
    bool middleClose = hovered && ImGui::IsMouseClicked(ImGuiMouseButton_Middle);

    // Pill background: active = panel bg with an accent top edge; inactive = darker, hover lifts it.
    ImU32 bg = active ? ui::Palette::toU32(pal.panelBg)
                      : (hovered ? ui::Palette::toU32(pal.buttonHovered)
                                 : ui::Palette::toU32(pal.headerBg));
    dl->AddRectFilled(p0, p1, bg, 4.0f * s, ImDrawFlags_RoundCornersTop);
    if (active)
        dl->AddLine(ImVec2(p0.x + ui::metrics::px(2), p0.y + ui::metrics::px(1)),
                    ImVec2(p1.x - ui::metrics::px(2), p0.y + ui::metrics::px(1)),
                    ui::Palette::toU32(pal.accent), 2.0f * s);
    else  // thin separator on the right edge of inactive tabs
        dl->AddLine(ImVec2(p1.x - 0.5f, p0.y + 4 * s), ImVec2(p1.x - 0.5f, p1.y - 4 * s),
                    ui::Palette::toU32(pal.border), 1.0f);

    // Title text.
    const float* textCol = active ? pal.text : pal.textDim;
    IconUV ic = getIcon(docIconName(t), 20);
    if (ic.valid) {
        ImVec4 tint(textCol[0], textCol[1], textCol[2], active ? 1.0f : 0.78f);
        float iy = p0.y + (tabH - iconSz) * 0.5f;
        dl->AddImage(ic.texId, ImVec2(p0.x + padX, iy),
                     ImVec2(p0.x + padX + iconSz, iy + iconSz),
                     ic.uv0, ic.uv1, ImGui::ColorConvertFloat4ToU32(tint));
    }
    dl->AddText(ImVec2(p0.x + padX + iconSz + iconGap,
                       p0.y + (tabH - ImGui::GetFontSize()) * 0.5f),
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

float docTabStripHeight(const AppState& st) {
    // The strip persists even with no document open (empty start state / all tabs closed) so its
    // "+" New-Document button stays reachable — closing the last tab must not strand the user with
    // no way to start a new file. Only suppressed before the worker is up (nothing to host yet).
    if (!st.docTabs.empty()) return 26.0f * ui::uiScale;
    return st.workerStarted ? 26.0f * ui::uiScale : 0.0f;
}

// Index of the Part/Assembly tab that owns worker doc `docId`, or -1. Used to resolve a Drawing's
// source part (a drawing is persisted by saving that part's .step+).
static int partTabIndexForDoc(AppState& st, const std::string& docId) {
    using K = AppState::DocTab::TabKind;
    if (docId.empty()) return -1;
    for (int i = 0; i < (int)st.docTabs.size(); ++i)
        if ((st.docTabs[i].kind == K::Part || st.docTabs[i].kind == K::Assembly) &&
            st.docTabs[i].docId == docId)
            return i;
    return -1;
}

// See doc_tabs.h. One place to save any tab kind so the close / quit / Save / Save-All flows behave
// identically across the whole suite (CAD parts, ECAD boards, drawings).
TabSaveResult saveDocTab(AppState& st, int idx, bool forceDialog) {
    if (idx < 0 || idx >= (int)st.docTabs.size()) return TabSaveResult::Nothing;
    using K = AppState::DocTab::TabKind;
    AppState::DocTab& t = st.docTabs[idx];

    // ---- Part / Assembly → DocMirror .step+ ----
    if (t.kind == K::Part || t.kind == K::Assembly) {
        st.switchToTab(idx);   // DocMirror::save writes whatever doc the mirror points at
        if (!forceDialog && !t.path.empty()) {
            std::string err;
            if (st.docMirror.save(st.worker, t.path, err)) {
                t.dirty = false;
                st.clearDrawingDirtyForSource(t.docId);   // the save embedded this part's drawings
                st.pushRecentFile(t.path);
                return TabSaveResult::Saved;
            }
            st.addLog("[tabs] save failed for " + t.path + ": " + err);
            st.toast(ui::ToastLevel::Error, "Save failed: " + err);
            return TabSaveResult::Failed;
        }
        // Never saved (or forced) → Save-As (featureTreeSaveDoc records path + clears active dirty +
        // doSaveDoc clears this part's drawing siblings).
        std::string saved = ui::featureTreeSaveDoc(st, /*saveAs=*/forceDialog);
        if (!saved.empty()) { st.pushRecentFile(saved); return TabSaveResult::Saved; }
        return TabSaveResult::Cancelled;
    }

    // ---- Schematic / PCB → ecad_save .ecad+ ----
    if (t.kind == K::Schematic || t.kind == K::PCB) {
        if (t.ecadDocId.empty()) return TabSaveResult::Nothing;
        std::string path = t.path;
        if (forceDialog || path.empty()) {
            std::string suggest = t.displayTitle();
            path = ui::saveFileDialog(
                "AI-ECAD design (*.ecad+)\0*.ecad+\0All files (*.*)\0*.*\0", "ecad+",
                suggest.empty() ? "design" : suggest.c_str());
            if (path.empty()) return TabSaveResult::Cancelled;
        }
        nlohmann::json res;
        std::string err;
        if (!st.worker.call("ecad_save", {{"doc_id", t.ecadDocId}, {"path", path}}, res, err) ||
            (res.is_object() && res.contains("code"))) {
            const std::string msg = err.empty() && res.is_object()
                                        ? res.value("message", std::string("engine error")) : err;
            st.addLog("[tabs] ecad_save failed for " + t.ecadDocId + ": " + msg);
            st.toast(ui::ToastLevel::Error, "Save failed: " + msg);
            return TabSaveResult::Failed;
        }
        const std::string saved = res.is_object() ? res.value("path", path) : path;
        t.path = saved;
        t.dirty = false;
        st.pushRecentFile(saved);
        st.addLog("[tabs] ecad saved -> " + saved);
        st.toast(ui::ToastLevel::Info, "Saved " + t.displayTitle());
        return TabSaveResult::Saved;
    }

    // ---- Drawing → save its SOURCE part (the drawing embeds in that part's .step+) ----
    if (t.kind == K::Drawing) {
        const int src = partTabIndexForDoc(st, t.sourceDocId);
        if (src < 0) {
            // Source part is no longer open — it can't be embed-saved. Don't block the close.
            st.addLog("[tabs] drawing '" + t.displayTitle() +
                      "' has no open source part — cannot persist independently");
            return TabSaveResult::Nothing;
        }
        TabSaveResult r = saveDocTab(st, src, forceDialog);   // saving the part clears this drawing
        return r;
    }

    return TabSaveResult::Nothing;
}

// Drawn INLINE at the top of the central dock window's content (the Viewport 3D view, the drawing
// sheet, or the ECAD editor — whichever owns the central node this frame), NOT as a separate
// overlay window. An overlay floated behind the focused central window and got covered by its
// opaque background; drawing into the SAME window's draw list keeps the tabs on top with no z-order
// fight. `availW` is the content width to span. The strip reserves its own height via a trailing
// Dummy so the 3D image / sheet flows below it (picking is unaffected — the image rect is read from
// the image item itself). Returns the strip height (0 when no document is open).
float drawDocTabs(AppState& st, float availW) {
    // The strip still draws with zero tabs (empty start state / all tabs closed) — just the band and
    // the "+" New-Document button, so the user can always start a new file. Suppressed only before
    // the worker is up (mirrors docTabStripHeight).
    if (st.docTabs.empty() && !st.workerStarted) return 0.0f;

    const auto& pal = ui::palette;
    float s = ui::uiScale;
    float barH = 26.0f * s;

    ImGuiViewport* vp = ImGui::GetMainViewport();

    s_switchTo = -1;
    s_closeIdx = -1;
    s_saveCloseIdx = -1;
    s_newDoc = NewDocKind::None;

    // ONE opaque windowBg bar for the ENTIRE tab row — full dockspace WIDTH, behind all three dock
    // nodes (FeatureManager tabs | doc tabs | Copilot/Settings tabs). Spanning the whole width (not just
    // this central node) guarantees the row reads as one continuous dark bar: it backfills the dock
    // splitter gaps and any translucent tab-bar region on the side nodes with the same windowBg the
    // dock tabs (ImGuiCol_Tab) and the side panes' opaque base already use — so nothing two-tone shows
    // between the ribbon and the content. Drawn on the background draw list (behind every window): the
    // tabs paint on top via their window draw lists; the side panes' opaque base paints on top in their
    // x-range; the full-bleed 3D is composited lower (from cr0, below this bar). The bar reaches DOWN
    // past the tab height to the viewport content top so its bottom divider sits flush above the 3D
    // instead of floating in a dark strip. Replaces the old glassBg@0.75 strip + the W2b band.
    ImVec2 origin = ImGui::GetCursorScreenPos();
    {
        const ImGuiStyle& style = ImGui::GetStyle();
        float bandT = origin.y, bandL = origin.x, bandR = origin.x + availW;
        if (ImGuiDockNode* node = ImGui::GetWindowDockNode()) bandT = node->Pos.y;
        // Full dockspace width = the whole main viewport row, not just the central node.
        if (ImGuiViewport* mv = ImGui::GetMainViewport()) {
            bandL = mv->Pos.x;
            bandR = mv->Pos.x + mv->Size.x;
        }
        // Bottom = where the viewport 3D content begins (the strip + the window-padding/spacing gap
        // that the caller leaves before cr0), so the divider hugs the viewport top.
        const float bandB = origin.y + barH + style.ItemSpacing.y;
        ImDrawList* bdl = ImGui::GetBackgroundDrawList();
        bdl->AddRectFilled(ImVec2(bandL, bandT), ImVec2(bandR, bandB),
                           ui::Palette::toU32(pal.windowBg));
        bdl->AddLine(ImVec2(bandL, bandB - 0.5f), ImVec2(bandR, bandB - 0.5f),
                     ui::Palette::toU32(pal.border), 1.0f);
    }

    // Explicit x-cursor placement (no SameLine/Dummy bookkeeping): each tab is drawn at the running
    // x and returns its width. The strip never wraps; very many tabs just clip at the content edge.
    float runX = origin.x;
    float y0 = origin.y;
    float gap = 2.0f * s;
    for (int i = 0; i < (int)st.docTabs.size(); ++i) {
        runX += drawOneTab(st, i, runX, y0, barH) + gap;
    }

    // "+" New-Document button at the end of the strip — opens a chooser popup (Wave 2). Clicking it
    // (or Ctrl+N via openNewDocChooser()) opens "##newdocmenu"; each item records a deferred
    // NewDocKind applied after the strip is drawn (so docTabs isn't mutated mid-iteration).
    {
        float btn = barH;
        ImVec2 p0(runX, y0);
        ImGui::SetCursorScreenPos(p0);
        bool clicked = ImGui::InvisibleButton("##newtab", ImVec2(btn, btn));
        bool hov = ImGui::IsItemHovered();
        ImDrawList* dl = ImGui::GetWindowDrawList();
        if (hov)
            dl->AddRectFilled(p0, ImVec2(p0.x + btn, p0.y + btn),
                              ui::Palette::toU32(pal.buttonHovered), 4.0f * s);
        ImVec2 c(p0.x + btn * 0.5f, p0.y + btn * 0.5f);
        IconUV plus = getIcon("new_doc", 20);
        if (plus.valid) {
            float iz = 18.0f * s;
            ImVec4 tint(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1);
            dl->AddImage(plus.texId, ImVec2(c.x - iz * 0.5f, c.y - iz * 0.5f),
                         ImVec2(c.x + iz * 0.5f, c.y + iz * 0.5f),
                         plus.uv0, plus.uv1, ImGui::ColorConvertFloat4ToU32(tint));
        } else {
            float g = btn * 0.2f;
            ImU32 col = ImGui::ColorConvertFloat4ToU32(
                ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1));
            dl->AddLine(ImVec2(c.x - g, c.y), ImVec2(c.x + g, c.y), col, 1.6f * s);
            dl->AddLine(ImVec2(c.x, c.y - g), ImVec2(c.x, c.y + g), col, 1.6f * s);
        }
        if (hov) ImGui::SetTooltip("New document");
        // Open the tiled "Create New" chooser (Part / Assembly / Drawing / Simulation / PCB / Open
        // existing — with previews), hosted at app scope (ui::drawNewDocChooser). Ctrl+N raises it
        // the same way via openNewDocChooser().
        if (clicked || s_openChooser) { st.wantNewDocChooser = true; s_openChooser = false; }
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
                               ? st.docTabs[idx].displayTitle() : std::string("this document");
        ImGui::Text("\"%s\" has unsaved changes.", name.c_str());
        ImGui::TextUnformatted("Save your edits before closing?");
        ImGui::Spacing();
        if (ImGui::Button("Save", ImVec2(ui::metrics::px(120), 0))) {
            s_saveCloseIdx = s_confirmCloseIdx;
            s_confirmCloseIdx = -1;
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Don't Save", ImVec2(ui::metrics::px(120), 0))) {
            s_closeIdx = s_confirmCloseIdx;
            s_confirmCloseIdx = -1;
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel", ImVec2(ui::metrics::px(90), 0))) {
            s_confirmCloseIdx = -1;
            ImGui::CloseCurrentPopup();
        }
        ImGui::EndPopup();
    }
    if (!open) s_confirmCloseIdx = -1;   // modal dismissed via the title-bar X

    // Reserve the strip's height in the host window's layout so the 3D image / sheet flows BELOW it
    // (the tabs above were placed absolutely; this is what actually advances the content cursor).
    ImGui::SetCursorScreenPos(origin);
    ImGui::Dummy(ImVec2(availW, barH));

    // ---- apply deferred actions (after all tabs were drawn) ----
    // A close that targets the active tab leaves the mirror on a neighbor, so handle close before
    // switch; only one of these fires per frame in practice.
    if (s_saveCloseIdx >= 0) {
        // Save the tab, then close it — unless the user cancels the Save-As or it fails (keep it open
        // so nothing is lost). Saved/Nothing both proceed to close.
        const int idx = s_saveCloseIdx;
        s_saveCloseIdx = -1;
        TabSaveResult r = saveDocTab(st, idx, /*forceDialog=*/false);
        if (r != TabSaveResult::Cancelled && r != TabSaveResult::Failed) st.closeDocTab(idx);
    } else if (s_closeIdx >= 0) {
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
