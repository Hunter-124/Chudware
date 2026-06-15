// AI-CAD title bar — menu bar + window caption buttons + chrome::HitLayout publishing.
#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include "imgui.h"

#include "title_bar.h"

#include "../core/app_state.h"
#include "../renderer.h"
#include "../ui_theme.h"   // ui::uiScale

// ---------------- caption buttons (shared small ui helper) ----------------
bool windowButton(const char* id, WinBtn kind, float h, bool maximized) {
    // C25-W3G: button width tracks uiScale so buttons stay clickable at 1.5x/2.0x.
    const float btnW = h * (1.35f + 0.15f * (ui::uiScale - 1.0f));
    const ImVec2 size(btnW, h);
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    bool clicked = ImGui::InvisibleButton(id, size);
    bool hovered = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    if (hovered) {
        ImU32 bg = kind == BTN_CLOSE ? IM_COL32(232, 17, 35, 255) : IM_COL32(255, 255, 255, 26);
        dl->AddRectFilled(p0, ImVec2(p0.x + size.x, p0.y + size.y), bg);
    }
    // Caption glyph color from palette so it adapts to light/dark themes.
    const unsigned char fgv = (unsigned char)(ui::palette.captionFg * 255.0f + 0.5f);
    const ImU32 fg = (hovered && kind == BTN_CLOSE) ? IM_COL32(255, 255, 255, 255)
                                                    : IM_COL32(fgv, fgv, fgv, 235);
    const ImVec2 c(p0.x + size.x * 0.5f, p0.y + size.y * 0.5f);
    const float s = h * 0.16f;  // glyph half-size
    const float th = 1.4f;
    if (kind == BTN_MIN) {
        dl->AddLine(ImVec2(c.x - s, c.y), ImVec2(c.x + s, c.y), fg, th);
    } else if (kind == BTN_MAX) {
        if (maximized) {  // restore: two stacked squares
            dl->AddRect(ImVec2(c.x - s + 2, c.y - s), ImVec2(c.x + s, c.y + s - 2), fg, 0, 0, th);
            dl->AddRect(ImVec2(c.x - s, c.y - s + 2), ImVec2(c.x + s - 2, c.y + s), fg, 0, 0, th);
        } else {
            dl->AddRect(ImVec2(c.x - s, c.y - s), ImVec2(c.x + s, c.y + s), fg, 0, 0, th);
        }
    } else {  // BTN_CLOSE: an X
        dl->AddLine(ImVec2(c.x - s, c.y - s), ImVec2(c.x + s, c.y + s), fg, th);
        dl->AddLine(ImVec2(c.x - s, c.y + s), ImVec2(c.x + s, c.y - s), fg, th);
    }
    return clicked;
}

// Wave 1C: buildDefaultLayout() moved to ui/layout.cpp — the default dock arrangement (slot names +
// split ratios that "Reset layout" rebuilds) was never a title-bar concern. The "Reset layout" menu
// items below just set st.resetLayout; app.cpp calls buildDefaultLayout() (global, declared in
// app_state.h) on the next frame.

namespace ui {

void drawTitleBar(AppState& st, bool maximized, float& barHOut) {
    GLFWwindow* win = st.win;
    chrome::HitLayout& hitLayout = *st.hitLayout;

    // C25-W3G: scale the frame padding so the title bar grows with DPI.
    float s = ui::uiScale;
    // ---------- custom title bar (our own; the OS title bar is stripped) ----------
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(14.0f * s, 12.0f * s));
    const float barH = ImGui::GetFrameHeight();
    hitLayout.titleBarHeight = (int)barH;
    ImGui::PushStyleColor(ImGuiCol_MenuBarBg,
        ImVec4(ui::palette.headerBg[0], ui::palette.headerBg[1],
               ui::palette.headerBg[2], ui::palette.headerBg[3]));
    if (ImGui::BeginMainMenuBar()) {
        // C25-W3G: compute caption button width early so we can clamp the brand/menu region.
        const float bw = barH * (1.35f + 0.15f * (s - 1.0f));
        const float captionZoneW = bw * 3.0f;
        const float winW = ImGui::GetWindowWidth();
        // Available horizontal space for brand + menus (leave caption buttons on the right).
        const float menuZoneR = winW - captionZoneW - 4.0f;

        // brand mark on the left
        const float iconH = barH * 0.52f;
        if (st.logoIcon.id) {
            ImGui::SetCursorPosX(12.0f * s);
            ImGui::SetCursorPosY((barH - iconH) * 0.5f);
            ImGui::Image((ImTextureID)(intptr_t)st.logoIcon.id, ImVec2(iconH, iconH));
            ImGui::SameLine(0, 9.0f * s);
        } else {
            ImGui::SetCursorPosX(14.0f * s);
        }
        const Tex& wm = (ui::activeTheme == ui::Theme::Light && st.wordmarkDark.id)
                            ? st.wordmarkDark : st.wordmark;
        if (wm.id) {
            const float wmH = barH * 0.40f;
            float wmW = wmH * (float)wm.w / wm.h;
            // C25-W3G: only show wordmark if it fits in the available zone.
            if (ImGui::GetCursorPosX() + wmW + 18.0f * s < menuZoneR - 120.0f) {
                ImGui::SetCursorPosY((barH - wmH) * 0.5f);
                ImGui::Image((ImTextureID)(intptr_t)wm.id, ImVec2(wmW, wmH));
                ImGui::SameLine(0, 18.0f * s);
            }
        }

        // C25-W3G: collapse to single "Menu" dropdown below a width threshold.
        const float collapseThreshold = 700.0f * s;
        hitLayout.menuLeft = (int)ImGui::GetCursorScreenPos().x;

        auto drawAiCadMenu = [&]() {
            if (ImGui::MenuItem("Settings...")) st.openSettings = true;
            if (ImGui::MenuItem("Repair / install runtime", nullptr, false,
                                !st.setupRunning.load() && !st.job.running.load()))
                st.startSetup(true);
            if (ImGui::MenuItem("Reset layout")) st.resetLayout = true;
            ImGui::Separator();
            if (ImGui::MenuItem("Quit", "Alt+F4")) glfwSetWindowShouldClose(win, 1);
        };
        auto drawViewMenu = [&]() {
            ImGui::MenuItem("Ribbon", nullptr, &st.showRibbon);
            ImGui::MenuItem("FeatureManager", nullptr, &st.showFeatureTree);
            ImGui::MenuItem("Configurations", nullptr, &st.showConfigPanel);
            ImGui::MenuItem("Design Table", nullptr, &st.showDesignTablePanel);
            ImGui::MenuItem("Cut List", nullptr, &st.showCutListPanel);
            ImGui::MenuItem("AI Panel", nullptr, &st.showAIPanel);
            ImGui::MenuItem("Viewport", nullptr, &st.showViewport);
            ImGui::Separator();
            bool isLight = (ui::activeTheme == ui::Theme::Light);
            if (ImGui::MenuItem("Light theme (SolidWorks-style)", nullptr, isLight)) {
                ui::Theme next = isLight ? ui::Theme::Dark : ui::Theme::Light;
                ui::applyTheme(next, st.scale);
                st.settings.theme = (next == ui::Theme::Light) ? "light" : "dark";
                saveSettings(st.settings);
                if (st.renderer)
                    st.renderer->setBackground(
                        ui::palette.viewportTop[0], ui::palette.viewportTop[1],
                        ui::palette.viewportTop[2], ui::palette.viewportBottom[0],
                        ui::palette.viewportBottom[1], ui::palette.viewportBottom[2]);
            }
            ImGui::Separator();
            if (ImGui::MenuItem("Reset layout")) st.resetLayout = true;
        };
        auto drawHelpMenu = [&]() {
            if (ImGui::MenuItem("About AI-CAD")) st.showAbout = true;
        };

        if (winW < collapseThreshold) {
            // Collapsed: single "Menu" top-level item with all submenus
            if (ImGui::BeginMenu("Menu")) {
                if (ImGui::BeginMenu("AI-CAD")) { drawAiCadMenu(); ImGui::EndMenu(); }
                if (ImGui::BeginMenu("View"))   { drawViewMenu();  ImGui::EndMenu(); }
                if (ImGui::BeginMenu("Help"))   { drawHelpMenu();  ImGui::EndMenu(); }
                ImGui::EndMenu();
            }
        } else {
            if (ImGui::BeginMenu("AI-CAD")) { drawAiCadMenu(); ImGui::EndMenu(); }
            if (ImGui::BeginMenu("View"))   { drawViewMenu();  ImGui::EndMenu(); }
            if (ImGui::BeginMenu("Help"))   { drawHelpMenu();  ImGui::EndMenu(); }
        }
        hitLayout.menuRight = (int)ImGui::GetItemRectMax().x;

        // window controls, right-aligned.
        const float bx = winW - captionZoneW;
        hitLayout.controlsLeft = (int)bx;
        hitLayout.controlsRight = (int)(bx + captionZoneW);
        ImGui::SetCursorPos(ImVec2(bx, 0.0f));
        if (windowButton("##min", BTN_MIN, barH, false)) glfwIconifyWindow(win);
        ImGui::SetCursorPos(ImVec2(bx + bw, 0.0f));
        if (windowButton("##max", BTN_MAX, barH, maximized)) {
            if (maximized) glfwRestoreWindow(win);
            else glfwMaximizeWindow(win);
        }
        ImGui::SetCursorPos(ImVec2(bx + bw * 2.0f, 0.0f));
        if (windowButton("##close", BTN_CLOSE, barH, false)) glfwSetWindowShouldClose(win, 1);
        ImGui::EndMainMenuBar();
    }
    ImGui::PopStyleColor();
    ImGui::PopStyleVar();  // FramePadding

    barHOut = barH;
}

}  // namespace ui
