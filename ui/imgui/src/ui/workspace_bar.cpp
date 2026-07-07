#include "ui/panels.h"
#include "ui/title_bar.h"
#include "ui/command_registry.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/icons.h"
#include "imgui.h"

namespace ui {

using app::AppState;

// ---------------------------------------------------------------------------
// chip() — single workspace mode pill.  Returns true when clicked.
// Caller must be inside an ImGui window (InvisibleButton advances cursor).
// ---------------------------------------------------------------------------
static bool chip(const char* id, const char* icon, const char* label, bool active, bool primary) {
    const float S = theme::S();
    ImGui::PushID(id);
    ImFont* f = theme::font(theme::Font::Label);
    ImVec2 ts = f->CalcTextSizeA(f->FontSize, FLT_MAX, 0, label);
    float h = 24*S, icSz = 15*S;
    float w = icSz + 8*S + ts.x + 20*S;
    ImVec2 p = ImGui::GetCursorScreenPos();
    bool clicked = ImGui::InvisibleButton("c", ImVec2(w, h));
    bool hov = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImU32 bg = active ? TCOL(accent) : hov ? TCOL(hover) : 0u;
    if (bg) dl->AddRectFilled(p, ImVec2(p.x+w, p.y+h), bg, h*0.5f);
    ImU32 fg = active ? TCOL(onAccent) : primary ? TCOL(txt0) : TCOL(txt1);
    icons::draw(dl, icon, ImVec2(p.x+10*S+icSz*0.5f, p.y+h*0.5f), icSz, fg);
    dl->AddText(f, f->FontSize, ImVec2(p.x+10*S+icSz+6*S, p.y+h*0.5f-ts.y*0.5f), fg, label);
    ImGui::PopID();
    ImGui::SameLine(0, 6*S);
    return clicked;
}

// ---------------------------------------------------------------------------
// chipWidth() — measure a single chip without drawing it.
// ---------------------------------------------------------------------------
static float chipWidth(const char* label) {
    const float S = theme::S();
    ImFont* f = theme::font(theme::Font::Label);
    ImVec2 ts = f->CalcTextSizeA(f->FontSize, FLT_MAX, 0, label);
    float icSz = 15*S;
    return icSz + 8*S + ts.x + 20*S;
}

// ---------------------------------------------------------------------------
// workspaceChipsWidth() — total pixel width of the chip cluster for the
// current doc type / inSketch state.  Call this before drawWorkspaceChips()
// to compute the centered X origin in the title bar.
// ---------------------------------------------------------------------------
float workspaceChipsWidth(AppState& s) {
    const float S = theme::S();
    const float chipGap = 6*S;
    const float divW    = 10*S + ImMax(1.f, S); // spacing around the divider line

    if (s.inSketch) {
        // "Sketch1" chip + "Exit Sketch" chip
        return chipWidth("Sketch1") + chipGap + chipWidth("Exit Sketch") + chipGap;
    }

    float total = 0.f;
    auto wss = cmd::workspacesFor(s.docType());
    for (size_t i = 0; i < wss.size(); ++i) {
        total += chipWidth(cmd::workspaceLabel(wss[i])) + chipGap;
        if (i == 0 && wss.size() > 1) total += divW;
    }

    auto gws = cmd::globalWorkspaces();
    bool anyGlobal = false;
    for (cmd::Workspace w : gws) {
        bool inDocScoped = false;
        for (auto dw : wss) if (dw == w) { inDocScoped = true; break; }
        if (inDocScoped) continue;
        if (!anyGlobal) { total += divW; anyGlobal = true; }
        const char* label = (w == cmd::Workspace::Schematic) ? "ECAD"
                            : cmd::workspaceLabel(w);
        total += chipWidth(label) + chipGap;
    }
    return total;
}

// ---------------------------------------------------------------------------
// drawWorkspaceChips() — render the chip cluster.
// originX: screen-space X where the cluster should begin (pre-computed by
//           the caller from workspaceChipsWidth + centering math).
// height:  title-bar pixel height, used for divider line extents and
//          vertical centering.  Caller sets cursor Y before calling this.
//
// Must be called INSIDE an active ImGui window (child window context).
// The cursor Y is set by the caller; this function overrides cursor X
// to match originX and draws chips horizontally from there.
// ---------------------------------------------------------------------------
void drawWorkspaceChips(AppState& s, float originX, float height) {
    const float S = theme::S();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 wp = ImGui::GetWindowPos();

    // Set cursor X to the computed centered position; keep caller's Y.
    float localX = originX - wp.x;
    float currentY = ImGui::GetCursorPosY();
    ImGui::SetCursorPos(ImVec2(localX, currentY));

    if (s.inSketch) {
        chip("sk",  "ws_sketch",  "Sketch1",     true,  true);
        if (chip("skx", "ok_check", "Exit Sketch", false, true)) s.dispatch("sketch_exit");
        return;
    }

    // --- doc-scoped workspaces ---
    auto wss = cmd::workspacesFor(s.docType());
    for (size_t i = 0; i < wss.size(); ++i) {
        cmd::Workspace w = wss[i];
        bool active  = (s.workspace == w);
        bool primary = (i == 0);
        if (chip(cmd::workspaceLabel(w), cmd::workspaceIcon(w), cmd::workspaceLabel(w), active, primary))
            { s.workspace = w; s.activeTool.clear(); s.camActive = false; }
        if (i == 0 && wss.size() > 1) {
            ImVec2 sp = ImGui::GetCursorScreenPos();
            dl->AddLine(ImVec2(sp.x, sp.y+4*S), ImVec2(sp.x, sp.y+height-8*S), TCOL(line), ImMax(1.f,S));
            ImGui::SameLine(0, 10*S);
        }
    }

    // --- Global workspaces (always visible, deduped against doc-scoped) ---
    {
        auto gws = cmd::globalWorkspaces();
        bool first = true;
        for (cmd::Workspace w : gws) {
            bool inDocScoped = false;
            for (auto dw : wss) if (dw == w) { inDocScoped = true; break; }
            if (inDocScoped) continue;

            if (first) {
                ImVec2 sp = ImGui::GetCursorScreenPos();
                dl->AddLine(ImVec2(sp.x, sp.y+4*S), ImVec2(sp.x, sp.y+height-8*S), TCOL(line), ImMax(1.f,S));
                ImGui::SameLine(0, 10*S);
                first = false;
            }

            bool active = (s.workspace == w);
            const char* label = (w == cmd::Workspace::Schematic) ? "ECAD"
                                : cmd::workspaceLabel(w);
            const char* icon  = cmd::workspaceIcon(w);
            if (chip(cmd::workspaceLabel(w), icon, label, active, false)) {
                cmd::DocType dt = s.docType();
                if (w == cmd::Workspace::Schematic || w == cmd::Workspace::Board) {
                    if (dt == cmd::DocType::Schematic) {
                        s.workspace = cmd::Workspace::Schematic;
                    } else if (dt == cmd::DocType::Pcb) {
                        s.workspace = cmd::Workspace::Board;
                    } else {
                        s.dispatch("ecad_new_sch");
                    }
                } else if (w == cmd::Workspace::Simulate) {
                    if (dt == cmd::DocType::Part || dt == cmd::DocType::Assembly) {
                        s.workspace = cmd::Workspace::Simulate;
                    } else {
                        for (int i = 0; i < (int)s.docTabs.size(); ++i) {
                            auto& tab = s.docTabs[i];
                            if (tab.kind == core::DocTabKind::Part ||
                                tab.kind == core::DocTabKind::Assembly) {
                                s.switchToTab(i);
                                break;
                            }
                        }
                        s.workspace = cmd::Workspace::Simulate;
                    }
                } else {
                    s.workspace = w;
                }
                s.activeTool.clear();
                s.camActive = false;
            }
        }
    }
}

// ---------------------------------------------------------------------------
// drawWorkspaceBar() — legacy band call.  Now a no-op: the workspace chips
// moved into the title bar (drawTitleBar / drawWorkspaceChips).  The band
// call in application.cpp has been removed, so this function should never be
// reached, but the symbol must remain so panels.h's declaration links cleanly
// without touching the shared header.
// ---------------------------------------------------------------------------
void drawWorkspaceBar(AppState& /*s*/, float /*height*/) {
    // No-op: chips now live inside drawTitleBar (centered in the title bar band).
}

} // namespace ui
