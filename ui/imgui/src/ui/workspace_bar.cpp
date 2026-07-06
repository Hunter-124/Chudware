#include "ui/panels.h"
#include "ui/command_registry.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/icons.h"
#include "imgui.h"

namespace ui {

using app::AppState;

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

void drawWorkspaceBar(AppState& s, float height) {
    const float S = theme::S();
    // T-G23: the SW-style workspace tab strip shares the ribbon's T-G16 liquid-glass band treatment
    // (adaptive surface + Thin material) so the tab row + ribbon read as one continuous glass chrome
    // band over the full-width viewport, instead of an opaque bg1 strip.
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("##wsbar", ImVec2(0, height), false,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    {
        ImVec2 bwp = ImGui::GetWindowPos(), bws = ImGui::GetWindowSize();
        ImRect bandRect(bwp, ImVec2(bwp.x + bws.x, bwp.y + bws.y));
        const theme::AdaptiveSurface& bandSurf =
            theme::adaptivePanel(theme::panelId("wsbar_glass"), bandRect);
        theme::glassPanel(dl, bandRect, theme::MaterialLevel::Thin, 0.f, bandSurf);
    }
    ImGui::SetCursorPos(ImVec2(12*S, (height-24*S)*0.5f));

    if (s.inSketch) {
        chip("sk", "ws_sketch", "Sketch1", true, true);
        if (chip("skx", "ok_check", "Exit Sketch", false, true)) s.dispatch("sketch_exit");
    } else {
        // --- doc-scoped workspaces (depend on current doc type) ---
        auto wss = cmd::workspacesFor(s.docType());
        for (size_t i = 0; i < wss.size(); ++i) {
            cmd::Workspace w = wss[i];
            bool active = (s.workspace == w);
            bool primary = (i == 0);   // first workspace is the primary one
            if (chip(cmd::workspaceLabel(w), cmd::workspaceIcon(w), cmd::workspaceLabel(w), active, primary))
                { s.workspace = w; s.activeTool.clear(); s.camActive = false; }
            if (i == 0 && wss.size() > 1) {
                // a faint divider between the primary workspace and the specialized workbenches
                ImVec2 sp = ImGui::GetCursorScreenPos();
                dl->AddLine(ImVec2(sp.x, sp.y+4*S), ImVec2(sp.x, sp.y+height-8*S), TCOL(line), ImMax(1.f,S));
                ImGui::SameLine(0, 10*S);
            }
        }

        // --- Global workspaces (always visible, doc-type independent) ---
        // Strip order: AI | ECAD | Simulate | Manufacture | PLM | Parts
        // Dedup: skip any global workspace already shown in the doc-scoped cluster above so a
        // Part doc (which already shows Simulate) doesn't show it twice.
        // Open-or-focus: ECAD/Simulate clicks navigate to an appropriate doc context; they do
        // not dead-end when the active doc type doesn't support the workspace directly.
        {
            auto gws = cmd::globalWorkspaces();
            if (!gws.empty()) {
                ImVec2 sp = ImGui::GetCursorScreenPos();
                dl->AddLine(ImVec2(sp.x, sp.y+4*S), ImVec2(sp.x, sp.y+height-8*S), TCOL(line), ImMax(1.f,S));
                ImGui::SameLine(0, 10*S);
                for (cmd::Workspace w : gws) {
                    // Dedup: skip if this workspace is already shown in the doc-scoped cluster.
                    bool inDocScoped = false;
                    for (auto dw : wss) if (dw == w) { inDocScoped = true; break; }
                    if (inDocScoped) continue;

                    bool active = (s.workspace == w);
                    // Label override: Schematic tab reads as "ECAD" at the global level.
                    const char* label = (w == cmd::Workspace::Schematic) ? "ECAD"
                                      : cmd::workspaceLabel(w);
                    const char* icon  = cmd::workspaceIcon(w);
                    if (chip(cmd::workspaceLabel(w), icon, label, active, false)) {
                        // Open-or-focus: ECAD/Simulate on an incompatible doc opens new context.
                        cmd::DocType dt = s.docType();
                        if (w == cmd::Workspace::Schematic || w == cmd::Workspace::Board) {
                            // ECAD: if doc is already a Schematic or PCB just switch workspace,
                            // otherwise request a new Schematic doc (dispatch to app handler).
                            if (dt == cmd::DocType::Schematic) {
                                s.workspace = cmd::Workspace::Schematic;
                            } else if (dt == cmd::DocType::Pcb) {
                                s.workspace = cmd::Workspace::Board;
                            } else {
                                // No ECAD doc open — dispatch new-schematic intent.
                                s.dispatch("ecad_new_sch");
                            }
                        } else if (w == cmd::Workspace::Simulate) {
                            // Simulate: valid on Part and Assembly. On ECAD/Drawing docs, open
                            // a new Part doc first (or just set workspace if already compatible).
                            if (dt == cmd::DocType::Part || dt == cmd::DocType::Assembly) {
                                s.workspace = cmd::Workspace::Simulate;
                            } else {
                                // Focus first Part/Assembly tab that has a sim study, or fall
                                // through to just switching workspace optimistically.
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
    }

    ImVec2 wp = ImGui::GetWindowPos(), wsz = ImGui::GetWindowSize();
    dl->AddLine(ImVec2(wp.x, wp.y+wsz.y-0.5f), ImVec2(wp.x+wsz.x, wp.y+wsz.y-0.5f),
                theme::roleU32A(theme::pal().line, 0.35f), 1.f);
    ImGui::EndChild();
    ImGui::PopStyleColor();
}

} // namespace ui
