#include "ui/title_bar.h"
#include "ui/doc_tab_strip.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/icons.h"
#include "imgui.h"
#include <GLFW/glfw3.h>
#include <string>

namespace ui {

using app::AppState;

namespace {

enum WinBtn { BTN_MIN, BTN_MAX, BTN_CLOSE };

bool windowButton(const char* id, WinBtn kind, float h, bool maximized) {
    const float S = theme::S();
    const float btnW = h * (1.35f + 0.15f * (S - 1.f));
    const ImVec2 size(btnW, h);
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const bool clicked = ImGui::InvisibleButton(id, size);
    const bool hovered = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();

    if (hovered) {
        const ImU32 bg = (kind == BTN_CLOSE) ? TCOL(bad) : TCOL(hover);
        dl->AddRectFilled(p0, ImVec2(p0.x + size.x, p0.y + size.y), bg);
    }

    const ImU32 fg = (hovered && kind == BTN_CLOSE) ? TCOL(txt0) : TCOL(txt1);
    const ImVec2 c(p0.x + size.x * 0.5f, p0.y + size.y * 0.5f);
    const float s = h * 0.16f;
    const float th = 1.4f * S;

    if (kind == BTN_MIN) {
        dl->AddLine(ImVec2(c.x - s, c.y), ImVec2(c.x + s, c.y), fg, th);
    } else if (kind == BTN_MAX) {
        if (maximized) {
            dl->AddRect(ImVec2(c.x - s + 2 * S, c.y - s), ImVec2(c.x + s, c.y + s - 2 * S), fg, 0, 0, th);
            dl->AddRect(ImVec2(c.x - s, c.y - s + 2 * S), ImVec2(c.x + s - 2 * S, c.y + s), fg, 0, 0, th);
        } else {
            dl->AddRect(ImVec2(c.x - s, c.y - s), ImVec2(c.x + s, c.y + s), fg, 0, 0, th);
        }
    } else {
        dl->AddLine(ImVec2(c.x - s, c.y - s), ImVec2(c.x + s, c.y + s), fg, th);
        dl->AddLine(ImVec2(c.x - s, c.y + s), ImVec2(c.x + s, c.y - s), fg, th);
    }
    return clicked;
}

}  // namespace

void drawTitleBar(AppState& st, float height) {
    const float S = theme::S();
    GLFWwindow* win = st.window;

    ImGui::PushStyleColor(ImGuiCol_ChildBg, TVEC(bg1));
    ImGui::BeginChild("##title", ImVec2(0, height), false,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 wp = ImGui::GetWindowPos();
    const ImVec2 wsz = ImGui::GetWindowSize();
    const bool maximized = win && glfwGetWindowAttrib(win, GLFW_MAXIMIZED) != 0;

    // -----------------------------------------------------------------------
    // Part 1: Workspace chip cluster — drawn DIRECTLY in the child window
    // (not inside BeginMenuBar) so it renders correctly in both runtime and
    // headless screenshot mode.  We measure the free width between the menu
    // zone and the caption-button zone and center the chips there.
    // -----------------------------------------------------------------------
    {
        const float S2 = S;
        // Caption button geometry (same formula as Part 2 below so the zones match).
        // We need a provisional barH.  Since FramePadding hasn't been pushed yet,
        // use the current frame height as an approximation; it equals what Part 2
        // will see after the style push (FramePadding only affects GetFrameHeight
        // if a new PushStyleVar is applied first, which Part 2 does — but the
        // difference is small: 12*S on each side, so barH_approx ≈ FontSize+24*S).
        // Use the title bar pixel height directly for vertical centering since that
        // is authoritative.
        const float chipH    = 24*S2;
        const float chipY    = (height - chipH) * 0.5f;   // child-local Y

        // Rough menu zone right edge: ShopCraft2 title (~80px) + 5 menus (~300px)
        // The real edge is determined at runtime inside BeginMenuBar, but for centering
        // we use a conservative estimate.  The chips will be clamped at that edge + pad.
        const float menuEstimateW  = 380.f * S2;    // app title + File/Edit/View/Insert/Help/Search
        const float captionBtnW    = height * (1.35f + 0.15f * (S2 - 1.f)); // single btn width
        const float captionZoneW   = captionBtnW * 3.f;
        const float winW           = wsz.x;
        const float menuEdgeEst    = menuEstimateW;
        const float captionStartX  = winW - captionZoneW;
        const float freeW          = captionStartX - menuEdgeEst;

        const float chipsW = ui::workspaceChipsWidth(st);
        const float pad    = 8.f * S2;
        float chipLocalX   = menuEdgeEst + (freeW - chipsW) * 0.5f;
        if (chipLocalX < menuEdgeEst + pad) chipLocalX = menuEdgeEst + pad;
        // Don't let chips overlap caption buttons
        if (chipLocalX + chipsW > captionStartX - pad) chipLocalX = captionStartX - pad - chipsW;
        if (chipLocalX < pad) chipLocalX = pad;

        // Place chips: SetCursorPos is child-window-local.
        ImGui::SetCursorPos(ImVec2(chipLocalX, chipY));
        const float chipScreenX = wp.x + chipLocalX;
        ui::drawWorkspaceChips(st, chipScreenX, height);
    }

    // -----------------------------------------------------------------------
    // Part 2: App title/menus + caption buttons — drawn inside BeginMenuBar.
    // These are interactive at runtime.  The style push makes the bar height
    // match the title bar band so menu items align vertically.
    // -----------------------------------------------------------------------
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(14.f * S, 12.f * S));
    ImGui::PushStyleColor(ImGuiCol_MenuBarBg, TVEC(bg1));

    const float barH = ImGui::GetFrameHeight();
    const float bw = barH * (1.35f + 0.15f * (S - 1.f));
    const float captionZoneW = bw * 3.f;
    const float winW = wsz.x;
    const float menuZoneR = winW - captionZoneW - 4.f * S;

    if (ImGui::BeginMenuBar()) {
        ImGui::SetCursorPosX(12.f * S);

        ImGui::PushFont(theme::font(theme::Font::H2));
        if (ImGui::GetCursorPosX() + 120.f * S < menuZoneR - 80.f * S) {
            ImGui::SetCursorPosY((barH - ImGui::GetFontSize()) * 0.5f);
            ImGui::TextColored(TVEC(txt0), "ShopCraft");
            ImGui::SameLine(0, 4.f * S);
            ImGui::TextColored(TVEC(accent), "2");
        }
        ImGui::PopFont();

        const float collapseThreshold = 700.f * S;
        const bool hasDoc = st.activeTab >= 0 || !st.docs.empty();

        auto drawFileMenu = [&]() {
            if (ImGui::BeginMenu("New")) {
                if (ImGui::MenuItem("Part", "Ctrl+N")) ui::createNewDoc(st, 0);
                if (ImGui::MenuItem("Assembly")) ui::createNewDoc(st, 1);
                if (ImGui::MenuItem("Drawing", nullptr, false, hasDoc)) ui::createNewDoc(st, 2);
                ImGui::Separator();
                if (ImGui::MenuItem("Schematic")) ui::createNewDoc(st, 3);
                if (ImGui::MenuItem("PCB")) ui::createNewDoc(st, 4);
                ImGui::EndMenu();
            }
            if (ImGui::MenuItem("Open...", "Ctrl+O")) st.doOpen();
            ImGui::Separator();
            if (ImGui::MenuItem("Save", "Ctrl+S", false, hasDoc)) st.doSave(false);
            if (ImGui::MenuItem("Save As...", "Ctrl+Shift+S", false, hasDoc)) st.doSave(true);
            ImGui::Separator();
            if (ImGui::MenuItem("Quit", "Alt+F4")) glfwSetWindowShouldClose(win, 1);
        };

        auto drawEditMenu = [&]() {
            if (ImGui::MenuItem("Undo", "Ctrl+Z", false, hasDoc)) {
                st.dispatch("edit_undo");
                st.onDocMutation();
            }
            if (ImGui::MenuItem("Redo", "Ctrl+Y", false, hasDoc)) {
                st.dispatch("edit_redo");
                st.onDocMutation();
            }
            ImGui::Separator();
            if (ImGui::MenuItem("Cut", "Ctrl+X", false, hasDoc)) st.dispatch("edit_cut");
            if (ImGui::MenuItem("Copy", "Ctrl+C", false, hasDoc)) st.dispatch("edit_copy");
            if (ImGui::MenuItem("Paste", "Ctrl+V", false, hasDoc)) st.dispatch("edit_paste");
        };

        auto drawViewMenu = [&]() {
            if (ImGui::MenuItem(st.dark ? "Light theme" : "Dark theme")) {
                st.dark = !st.dark;
                theme::setDark(st.dark);
                icons::setAtlasTheme(st.dark);
            }
            ImGui::Separator();
            if (ImGui::BeginMenu("Display Style")) {
                if (ImGui::MenuItem("Shaded", nullptr, st.viewStyle == 0)) st.dispatch("view_style_shaded");
                if (ImGui::MenuItem("Shaded + Edges", nullptr, st.viewStyle == 1)) st.dispatch("view_style_edges");
                if (ImGui::MenuItem("Wireframe", nullptr, st.viewStyle == 2)) st.dispatch("view_style_wire");
                ImGui::EndMenu();
            }
        };

        auto drawInsertMenu = [&]() {
            if (ImGui::MenuItem("Sketch", nullptr, false, hasDoc)) st.dispatch("sketch_new");
            if (ImGui::MenuItem("Extrude Boss/Base", nullptr, false, hasDoc)) st.dispatch("extrude_boss");
            if (ImGui::MenuItem("Fillet", nullptr, false, hasDoc)) st.dispatch("fillet");
        };

        auto drawHelpMenu = [&]() {
            ImGui::MenuItem("Documentation", nullptr, false, false);
            ImGui::MenuItem("About ShopCraft", nullptr, false, false);
        };

        if (winW < collapseThreshold) {
            if (ImGui::BeginMenu("Menu")) {
                if (ImGui::BeginMenu("File")) { drawFileMenu(); ImGui::EndMenu(); }
                if (ImGui::BeginMenu("Edit")) { drawEditMenu(); ImGui::EndMenu(); }
                if (ImGui::BeginMenu("View")) { drawViewMenu(); ImGui::EndMenu(); }
                if (ImGui::BeginMenu("Insert")) { drawInsertMenu(); ImGui::EndMenu(); }
                if (ImGui::BeginMenu("Help")) { drawHelpMenu(); ImGui::EndMenu(); }
                if (ImGui::MenuItem("Search")) st.paletteOpen = true;
                ImGui::EndMenu();
            }
        } else {
            if (ImGui::BeginMenu("File")) { drawFileMenu(); ImGui::EndMenu(); }
            if (ImGui::BeginMenu("Edit")) { drawEditMenu(); ImGui::EndMenu(); }
            if (ImGui::BeginMenu("View")) { drawViewMenu(); ImGui::EndMenu(); }
            if (ImGui::BeginMenu("Insert")) { drawInsertMenu(); ImGui::EndMenu(); }
            if (ImGui::BeginMenu("Help")) { drawHelpMenu(); ImGui::EndMenu(); }
            if (ImGui::MenuItem("Search")) st.paletteOpen = true;
        }

        // Caption buttons — right-aligned.
        const float bx = winW - captionZoneW;
        ImGui::SetCursorPos(ImVec2(bx, 0.f));
        if (windowButton("##min", BTN_MIN, barH, false)) glfwIconifyWindow(win);
        ImGui::SetCursorPos(ImVec2(bx + bw, 0.f));
        if (windowButton("##max", BTN_MAX, barH, maximized)) {
            if (maximized) glfwRestoreWindow(win);
            else glfwMaximizeWindow(win);
        }
        ImGui::SetCursorPos(ImVec2(bx + bw * 2.f, 0.f));
        if (windowButton("##close", BTN_CLOSE, barH, false)) glfwSetWindowShouldClose(win, 1);

        ImGui::EndMenuBar();
    }

    ImGui::PopStyleColor();
    ImGui::PopStyleVar();

    // Bottom separator line.
    dl->AddLine(ImVec2(wp.x, wp.y + wsz.y - 0.5f), ImVec2(wp.x + wsz.x, wp.y + wsz.y - 0.5f),
                theme::roleU32A(theme::pal().line, 0.35f), 1.f);

    ImGui::EndChild();
    ImGui::PopStyleColor();
}

}  // namespace ui
