// AI-CAD title bar — menu bar + window caption buttons + chrome::HitLayout publishing.
#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include "imgui.h"
#include "imgui_internal.h"

#include "title_bar.h"

#include "../core/app_state.h"
#include "../renderer.h"

// ---------------- caption buttons (shared small ui helper) ----------------
bool windowButton(const char* id, WinBtn kind, float h, bool maximized) {
    const ImVec2 size(h * 1.35f, h);
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

void buildDefaultLayout(ImGuiID root, const ImVec2& size) {
    ImGui::DockBuilderRemoveNode(root);
    ImGui::DockBuilderAddNode(root, ImGuiDockNodeFlags_DockSpace);
    ImGui::DockBuilderSetNodeSize(root, size);
    ImGuiID main = root, left, bottom;
    left = ImGui::DockBuilderSplitNode(main, ImGuiDir_Left, 0.31f, nullptr, &main);
    bottom = ImGui::DockBuilderSplitNode(main, ImGuiDir_Down, 0.24f, nullptr, &main);
    ImGui::DockBuilderDockWindow("Design", left);
    ImGui::DockBuilderDockWindow("Viewport", main);
    ImGui::DockBuilderDockWindow("Console", bottom);
    ImGui::DockBuilderFinish(root);
}

namespace ui {

void drawTitleBar(AppState& st, bool maximized, float& barHOut) {
    GLFWwindow* win = st.win;
    chrome::HitLayout& hitLayout = *st.hitLayout;

    // ---------- custom title bar (our own; the OS title bar is stripped) ----------
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(14.0f, 12.0f));
    const float barH = ImGui::GetFrameHeight();
    hitLayout.titleBarHeight = (int)barH;
    ImGui::PushStyleColor(ImGuiCol_MenuBarBg,
        ImVec4(ui::palette.headerBg[0], ui::palette.headerBg[1],
               ui::palette.headerBg[2], ui::palette.headerBg[3]));
    if (ImGui::BeginMainMenuBar()) {
        // brand mark on the left (draggable caption around it)
        const float iconH = barH * 0.52f;
        if (st.logoIcon.id) {
            ImGui::SetCursorPosX(12.0f);
            ImGui::SetCursorPosY((barH - iconH) * 0.5f);
            ImGui::Image((ImTextureID)(intptr_t)st.logoIcon.id, ImVec2(iconH, iconH));
            ImGui::SameLine(0, 9);
        } else {
            ImGui::SetCursorPosX(14.0f);
        }
        if (st.wordmark.id) {
            const float wmH = barH * 0.40f;
            ImGui::SetCursorPosY((barH - wmH) * 0.5f);
            ImGui::Image((ImTextureID)(intptr_t)st.wordmark.id,
                         ImVec2(wmH * (float)st.wordmark.w / st.wordmark.h, wmH));
            ImGui::SameLine(0, 18);
        }

        // menus — record their horizontal span so the native hit-test keeps them clickable
        hitLayout.menuLeft = (int)ImGui::GetCursorScreenPos().x;
        if (ImGui::BeginMenu("AI-CAD")) {
            if (ImGui::MenuItem("Settings...")) st.openSettings = true;
            if (ImGui::MenuItem("Repair / install runtime", nullptr, false,
                                !st.setupRunning.load() && !st.job.running.load()))
                st.startSetup(true);
            if (ImGui::MenuItem("Reset layout")) st.resetLayout = true;
            ImGui::Separator();
            if (ImGui::MenuItem("Quit", "Alt+F4")) glfwSetWindowShouldClose(win, 1);
            ImGui::EndMenu();
        }
        if (ImGui::BeginMenu("View")) {
            ImGui::MenuItem("Ribbon", nullptr, &st.showRibbon);
            ImGui::MenuItem("Design / Chat", nullptr, &st.showDesign);
            ImGui::MenuItem("Viewport", nullptr, &st.showViewport);
            ImGui::MenuItem("Console", nullptr, &st.showConsole);
            ImGui::Separator();
            // Theme toggle — switches between dark and light palettes at runtime.
            bool isLight = (ui::activeTheme == ui::Theme::Light);
            if (ImGui::MenuItem("Light theme (SolidWorks-style)", nullptr, isLight)) {
                ui::Theme next = isLight ? ui::Theme::Dark : ui::Theme::Light;
                ui::applyTheme(next, st.scale);
                st.settings.theme = (next == ui::Theme::Light) ? "light" : "dark";
                saveSettings(st.settings);
                // Sync viewport gradient to the new palette.
                if (st.renderer)
                    st.renderer->setBackground(
                        ui::palette.viewportTop[0], ui::palette.viewportTop[1],
                        ui::palette.viewportTop[2], ui::palette.viewportBottom[0],
                        ui::palette.viewportBottom[1], ui::palette.viewportBottom[2]);
            }
            ImGui::Separator();
            if (ImGui::MenuItem("Reset layout")) st.resetLayout = true;
            ImGui::EndMenu();
        }
        if (ImGui::BeginMenu("Help")) {
            if (ImGui::MenuItem("About AI-CAD")) st.showAbout = true;
            ImGui::EndMenu();
        }
        hitLayout.menuRight = (int)ImGui::GetItemRectMax().x;

        // window controls, right-aligned (minimize / maximize-restore / close).
        // Position each one absolutely so they share one baseline (the menu bar's
        // horizontal flow otherwise drifts arbitrary widgets vertically).
        const float bw = barH * 1.35f;
        const float bx = ImGui::GetWindowWidth() - bw * 3.0f;
        hitLayout.controlsLeft = (int)bx;
        hitLayout.controlsRight = (int)(bx + bw * 3.0f);
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
