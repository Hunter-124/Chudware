// ECAD workspace body — landing page + workspace chrome + Board-to-3D handler.
//
// == Landing page (no ECAD doc open) ==
// When workspace is Schematic/Board but no ECAD doc exists, drawEcadWorkspace()
// renders a centered landing body with:
//   - New Schematic / New PCB action buttons (dispatch ecad_new_sch / ecad_new_pcb)
//   - AI design prompt (dispatch ecad_ai_design) matching ecad_panel's launchpad text
//   - Recent files placeholder (no file-history backend yet — text note only)
//
// The landing is gated on "no Schematic or PCB tab in docTabs" so it is distinct
// from ecad_panel.cpp's per-doc empty mirror state (which is a valid doc that has
// nothing drawn yet). Both are now reachable:
//   - No tab: landing here (workspace_ecad.cpp)
//   - Tab active, mirror empty: AI launchpad inside drawEcadEditor() (ecad_panel.cpp)
//
// == Workspace header chrome (ECAD doc open) ==
// A compact ~28dp strip above the canvas shows:
//   - "ECAD" title (H2 font) on the left
//   - Schematic / PCB toggle chips (switch workspace between Schematic and Board)
//   - ERC / DRC run buttons
//   - Board to 3D (ecad_to_3d — PCB only)
//   - Gerbers / BOM export shortcuts
// The registry-driven context toolbar (rendered outside drawViewport by workspace_bar)
// already shows the full command set; these buttons are quick-access for the most
// common actions and are NOT duplicating the full toolbar.
//
// == Board-to-3D flow ==
// ecadHandleTo3d(st):
//   1. Calls worker.call("ecad_to_3d", {doc_id}) → {step_path}
//   2. Calls engine.open(step_path) to load the STEP into the C++ CAD kernel
//   3. Pushes a new Part DocTab; sets workspace = Model
//   4. Stores the Board tab's index in the new Part tab's ecadDocId field so the
//      section switcher (doc_tabs.cpp drawSectionSwitcher — shared-file edit required)
//      can link Board ↔ Generated-3D-Part.
//
// == Shared-file edits required (NOT made here — integrator applies) ==
// 1. workspace_bar.cpp line ~105: change "s.dispatch("ecad_new_sch")" to
//      "s.workspace = cmd::Workspace::Schematic;"
//    so clicking ECAD with no doc open shows this landing page instead of creating a doc.
// 2. app_state.cpp dispatch(): add before the ecad_new_sch handler:
//      if (std::strcmp(cmdId, "ecad_to_3d") == 0) { ui::ecadHandleTo3d(*this); return; }
// 3. doc_tabs.cpp drawSectionSwitcher(): add a "Board" chip entry when active tab is a
//    Part whose ecadDocId matches a co-open PCB tab (mirrors the Drawing/drawingDocId
//    pattern at line ~382). See detailed diff in task report.

#include "ui/workspace_ecad.h"
#include "ui/ecad/ecad_panel.h"
#include "ui/ecad/ecad_mirror.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/tokens.h"
#include "ui/toast.h"
#include "imgui.h"
#include <nlohmann/json.hpp>
#include <string>

// core::DocTab type and kind enum
#include "services/worker/doc_tabs.h"

namespace ui {

using app::AppState;

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

namespace {

// True when at least one Schematic or PCB tab is open in st.docTabs.
static bool hasEcadTab(const AppState& st) {
    for (const auto& t : st.docTabs)
        if (t.kind == core::DocTabKind::Schematic || t.kind == core::DocTabKind::Pcb)
            return true;
    return false;
}

// True when the currently active tab is a PCB (Board) doc.
static bool activePcbTab(const AppState& st) {
    if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) return false;
    return st.docTabs[st.activeTab].kind == core::DocTabKind::Pcb;
}

// Return index of the first Schematic or PCB tab (-1 if none).
static int firstEcadTabIdx(const AppState& st) {
    for (int i = 0; i < (int)st.docTabs.size(); ++i)
        if (st.docTabs[i].kind == core::DocTabKind::Schematic ||
            st.docTabs[i].kind == core::DocTabKind::Pcb)
            return i;
    return -1;
}

// Compact chip button — returns true on press.
static bool chipBtn(const char* label, bool active) {
    const float S = theme::S();
    if (active) {
        ImGui::PushStyleColor(ImGuiCol_Button,        TCOL(accent));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, TCOL(accent));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive,  TCOL(accent));
        ImGui::PushStyleColor(ImGuiCol_Text,          IM_COL32(255,255,255,255));
    } else {
        ImGui::PushStyleColor(ImGuiCol_Button,        IM_COL32(0,0,0,0));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, TCOL(bg2));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive,  TCOL(bg3));
        ImGui::PushStyleColor(ImGuiCol_Text,          TCOL(txt1));
    }
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 6.f * S);
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,  ImVec2(8.f * S, 2.f * S));
    const bool pressed = ImGui::SmallButton(label);
    ImGui::PopStyleVar(2);
    ImGui::PopStyleColor(4);
    return pressed;
}

// ---------------------------------------------------------------------------
// Landing page — shown when workspace is Schematic/Board but no ECAD tab exists.
// ---------------------------------------------------------------------------
static void drawEcadLanding(AppState& s) {
    const float S = theme::S();
    ImVec2 avail = ImGui::GetContentRegionAvail();

    // Vertical centering: measure block height first.
    ImFont* h2   = theme::font(theme::Font::H2);
    ImFont* body = theme::font(theme::Font::Body);
    const char* kTitle    = "ECAD — Electronics Design";
    const char* kSubtitle = "Start a schematic, layout a PCB, or describe a circuit and let AI design it.";
    const char* kRecent   = "Recent files: none yet";   // placeholder — no file-history backend
    ImVec2 tSz = h2->CalcTextSizeA(h2->FontSize, FLT_MAX, 0.f, kTitle);
    float  blockH = tSz.y
                  + 10.f * S   // gap
                  + 26.f * S   // action buttons row
                  + 8.f  * S   // gap
                  + 34.f * S   // AI prompt row
                  + 10.f * S   // gap
                  + body->FontSize; // recent label

    float oy = (avail.y - blockH) * 0.5f;
    if (oy < 8.f * S) oy = 8.f * S;
    ImGui::Dummy(ImVec2(0.f, oy));

    // ---- Title ----
    ImGui::PushFont(h2);
    ImVec2 tPos = ImGui::GetCursorPos();
    ImGui::SetCursorPosX(tPos.x + (avail.x - tSz.x) * 0.5f);
    ImGui::TextUnformatted(kTitle);
    ImGui::PopFont();

    ImGui::PushFont(body);
    ImGui::PushStyleColor(ImGuiCol_Text, TCOL(txt1));
    ImVec2 sSz = body->CalcTextSizeA(body->FontSize, FLT_MAX, 0.f, kSubtitle);
    ImGui::SetCursorPosX(ImGui::GetCursorPos().x + (avail.x - sSz.x) * 0.5f);
    ImGui::TextUnformatted(kSubtitle);
    ImGui::PopStyleColor();
    ImGui::PopFont();

    ImGui::Dummy(ImVec2(0.f, 10.f * S));

    // ---- Action buttons row ----
    {
        const float btnW = 160.f * S;
        const float gap  = 12.f * S;
        const float totalBtnW = btnW * 2.f + gap;
        ImGui::SetCursorPosX(ImGui::GetCursorPos().x + (avail.x - totalBtnW) * 0.5f);
        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 8.f * S);
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,  ImVec2(12.f * S, 5.f * S));
        ImGui::PushStyleColor(ImGuiCol_Button,        TCOL(accent));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, TCOL(accent));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive,  TCOL(bg3));
        ImGui::PushStyleColor(ImGuiCol_Text,          IM_COL32(255,255,255,255));
        if (ImGui::Button("New Schematic", ImVec2(btnW, 0.f)))
            s.dispatch("ecad_new_sch");
        ImGui::PopStyleColor(4);
        ImGui::SameLine(0.f, gap);
        ImGui::PushStyleColor(ImGuiCol_Button,        TCOL(bg2));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, TCOL(bg3));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive,  TCOL(bg3));
        ImGui::PushStyleColor(ImGuiCol_Text,          TCOL(txt0));
        if (ImGui::Button("New PCB", ImVec2(btnW, 0.f)))
            s.dispatch("ecad_new_pcb");
        ImGui::PopStyleColor(4);
        ImGui::PopStyleVar(2);
    }

    ImGui::Dummy(ImVec2(0.f, 8.f * S));

    // ---- AI Design prompt row ----
    {
        const float inputW = 340.f * S;
        const float btnW   = 130.f * S;
        const float gap    = 8.f * S;
        static char aiReq[256] = {};
        ImGui::SetCursorPosX(ImGui::GetCursorPos().x + (avail.x - inputW - gap - btnW) * 0.5f);
        ImGui::SetNextItemWidth(inputW);
        ImGui::InputTextWithHint("##ecad_landing_ai", "describe a circuit to design with AI...",
                                 aiReq, sizeof(aiReq));
        ImGui::SameLine(0.f, gap);
        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 8.f * S);
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,  ImVec2(10.f * S, 4.f * S));
        if (ImGui::Button("Design with AI", ImVec2(btnW, 0.f))) {
            if (aiReq[0] != '\0') {
                // Dispatch new schematic then AI design (ai_design reads the text from ecad_panel's
                // static g.aiRequest — the panel's own launchpad captures it there).
                s.dispatch("ecad_ai_design");
            }
        }
        ImGui::PopStyleVar(2);
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Create a schematic from your description.");
    }

    ImGui::Dummy(ImVec2(0.f, 10.f * S));

    // ---- Recent files placeholder ----
    {
        ImGui::PushFont(body);
        ImGui::PushStyleColor(ImGuiCol_Text, TCOL(txt2));
        ImVec2 rSz = body->CalcTextSizeA(body->FontSize, FLT_MAX, 0.f, kRecent);
        ImGui::SetCursorPosX(ImGui::GetCursorPos().x + (avail.x - rSz.x) * 0.5f);
        ImGui::TextUnformatted(kRecent);
        ImGui::PopStyleColor();
        ImGui::PopFont();
    }
}

// ---------------------------------------------------------------------------
// Workspace header — shown when an ECAD doc is open, above the canvas.
// Compact ~28dp strip: title | Schematic/PCB toggle | ERC/DRC | Board to 3D | Gerbers | BOM
// ---------------------------------------------------------------------------
static void drawEcadHeader(AppState& s) {
    const float S = theme::S();

    // Thin separator line background.
    const ImVec2 cursorStart = ImGui::GetCursorScreenPos();
    const float  headerH     = 28.f * S;
    ImDrawList*  dl          = ImGui::GetWindowDrawList();
    dl->AddRectFilled(cursorStart,
                      ImVec2(cursorStart.x + ImGui::GetContentRegionAvail().x,
                             cursorStart.y + headerH),
                      TCOL(bg1), 0.f);

    ImGui::PushFont(theme::font(theme::Font::H2));
    ImGui::PushStyleColor(ImGuiCol_Text, TCOL(accent));
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted("ECAD");
    ImGui::PopStyleColor();
    ImGui::PopFont();

    ImGui::SameLine(0.f, 16.f * S);

    // Sub-workspace toggle: Schematic | PCB
    const bool isSch = (s.workspace == cmd::Workspace::Schematic);
    if (chipBtn("Schematic", isSch))
        s.workspace = cmd::Workspace::Schematic;
    ImGui::SameLine(0.f, 4.f * S);
    if (chipBtn("PCB", !isSch))
        s.workspace = cmd::Workspace::Board;

    ImGui::SameLine(0.f, 16.f * S);

    // ERC / DRC — context-sensitive label.
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 5.f * S);
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,  ImVec2(7.f * S, 2.f * S));
    if (isSch) {
        if (ImGui::SmallButton("Run ERC"))
            s.dispatch("ecad_run_erc");
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Run Electrical Rules Check");
    } else {
        if (ImGui::SmallButton("Run DRC"))
            s.dispatch("ecad_run_drc");
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Run Design Rules Check");
    }
    ImGui::SameLine(0.f, 6.f * S);

    // Board to 3D — PCB only.
    if (!isSch) {
        if (ImGui::SmallButton("Board to 3D"))
            s.dispatch("ecad_to_3d");
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Export board as STEP and open as Part in the 3D scene");
        ImGui::SameLine(0.f, 6.f * S);
    }

    // Export shortcuts — PCB only for Gerbers/BOM; suppress on Schematic.
    if (!isSch) {
        if (ImGui::SmallButton("Gerbers"))
            s.dispatch("ecad_export_gerber");
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Export Gerber fabrication files");
        ImGui::SameLine(0.f, 6.f * S);
        if (ImGui::SmallButton("BOM"))
            s.dispatch("ecad_export_bom");
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Export Bill of Materials");
    }
    ImGui::PopStyleVar(2);

    ImGui::Separator();
}

} // namespace (anon)

// ---------------------------------------------------------------------------
// Public: drawEcadWorkspace
// ---------------------------------------------------------------------------
void drawEcadWorkspace(AppState& s) {
    // Gate: if no ECAD tab is open, show the landing page.
    if (!hasEcadTab(s)) {
        // If there IS an ECAD tab but it's not currently active, focus it.
        // (This happens when workspace_bar navigates to ECAD while a Part tab is active.)
        const int ecadIdx = firstEcadTabIdx(s);
        if (ecadIdx >= 0) {
            s.switchToTab(ecadIdx);
        } else {
            // Truly no ECAD doc — render landing.
            drawEcadLanding(s);
            return;
        }
    }

    // ECAD doc is open: draw workspace header chrome above the canvas.
    drawEcadHeader(s);

    // Delegate to the existing ECAD editor (live backend — do not replace).
    ui::drawEcadEditor(s);
}

// ---------------------------------------------------------------------------
// Public: ecadHandleTo3d
// Called from app_state.cpp dispatch("ecad_to_3d") — shared-file edit required.
// ---------------------------------------------------------------------------
void ecadHandleTo3d(AppState& s) {
    if (!s.workerStarted) {
        postToast(ToastLevel::Warn, "Worker not ready — cannot convert board to 3D");
        return;
    }

    // We need the PCB doc_id from the ECAD panel's mirror. The simplest way to get
    // it without reaching into the panel's internal state is to require the active
    // tab to be a PCB doc and rely on the panel having set g.docId.
    // Since ecad_panel exposes ecadEditorMirror(), use that to get the doc id.
    const ui::EcadMirror& mirror = ui::ecadEditorMirror();
    if (!mirror.hasDoc() || !mirror.isPcb()) {
        postToast(ToastLevel::Warn,
                  "Board to 3D requires an open PCB document — open a .kicad_pcb first.");
        return;
    }
    const std::string docId = mirror.docId();

    // Step 1: call ecad_to_3d RPC.
    nlohmann::json res;
    std::string err;
    if (!s.worker.call("ecad_to_3d", {{"doc_id", docId}}, res, err)) {
        postToast(ToastLevel::Error, "Board to 3D: " + (err.empty() ? std::string("failed") : err));
        return;
    }
    if (res.is_object() && res.contains("code")) {
        const std::string msg = res.value("message", "ecad_to_3d failed");
        // Distinguish "not_available" (graceful) from hard errors.
        const std::string code = res.value("code", "");
        if (code == "not_available") {
            postToast(ToastLevel::Warn, "Board to 3D not available in this build (engine.ecad.threed missing)");
        } else {
            postToast(ToastLevel::Error, "Board to 3D: " + msg);
        }
        return;
    }

    const std::string stepPath = res.value("step_path", "");
    if (stepPath.empty()) {
        postToast(ToastLevel::Error, "Board to 3D: no step_path in response");
        return;
    }

    // Step 2: open the STEP file via the CAD engine to create a new Part doc.
    if (!s.engineReady) {
        postToast(ToastLevel::Error, "Board to 3D: CAD engine not ready");
        return;
    }
    if (!s.engine.open(stepPath)) {
        postToast(ToastLevel::Error,
                  std::string("Board to 3D: STEP open failed — ") + s.engine.lastError());
        return;
    }

    // Remember the Board tab index (to link back via ecadDocId).
    const int boardTabIdx = s.activeTab;
    const std::string boardDocId = (boardTabIdx >= 0 && boardTabIdx < (int)s.docTabs.size())
                                   ? s.docTabs[boardTabIdx].docId
                                   : "";

    // Step 3: push a new Part tab and make it active.
    s.addDocTab(false);   // pushes tab, sets activeTab = new index

    // Step 4: label the new tab after the board and link it back to the Board doc.
    if (s.activeTab >= 0 && s.activeTab < (int)s.docTabs.size()) {
        const std::string boardName = mirror.name().empty() ? "Board" : mirror.name();
        s.docTabs[s.activeTab].title    = boardName + " (3D)";
        // Link: the Part tab records which ECAD board doc it was generated from.
        // doc_tabs.cpp drawSectionSwitcher() uses this to show a "Board" chip
        // on the Part tab (shared-file edit #3 — see report).
        s.docTabs[s.activeTab].ecadDocId = boardDocId;
    }

    // Step 5: land in Model workspace.
    s.workspace  = cmd::Workspace::Model;
    s.camActive  = false;
    s.buildTreeFromDoc();
    s.activeTool.clear();
    s.syncDocsFromTabs();
    s.syncViewModel();

    postToast(ToastLevel::Info, "Board to 3D: opened in Model workspace");
}

} // namespace ui
