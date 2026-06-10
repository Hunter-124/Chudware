#include "ui_theme.h"

#include "imgui.h"

#include <cstdio>

namespace ui {

Palette palette;

static ImVec4 rgb(int r, int g, int b, float a = 1.0f) {
    return ImVec4(r / 255.0f, g / 255.0f, b / 255.0f, a);
}

void applyTheme(float scale) {
    ImGuiStyle& s = ImGui::GetStyle();

    // --- shape: rounded + roomy -------------------------------------------
    s.WindowRounding = 9.0f;
    s.ChildRounding = 9.0f;
    s.FrameRounding = 7.0f;
    s.PopupRounding = 8.0f;
    s.ScrollbarRounding = 10.0f;
    s.GrabRounding = 7.0f;
    s.TabRounding = 8.0f;

    s.WindowPadding = ImVec2(15, 13);
    s.FramePadding = ImVec2(12, 7);
    s.ItemSpacing = ImVec2(10, 9);
    s.ItemInnerSpacing = ImVec2(8, 6);
    s.CellPadding = ImVec2(8, 6);
    s.ScrollbarSize = 12.0f;
    s.GrabMinSize = 11.0f;

    s.WindowBorderSize = 1.0f;
    s.ChildBorderSize = 1.0f;
    s.PopupBorderSize = 1.0f;
    s.FrameBorderSize = 0.0f;
    s.TabBorderSize = 0.0f;
    s.WindowTitleAlign = ImVec2(0.02f, 0.5f);
    s.WindowMenuButtonPosition = ImGuiDir_None;
    s.AntiAliasedLines = true;
    s.AntiAliasedFill = true;

    // --- colors -----------------------------------------------------------
    ImVec4* c = s.Colors;
    const ImVec4 accent = rgb(45, 212, 191);
    const ImVec4 accentSoft = rgb(45, 212, 191, 0.22f);
    const ImVec4 accentMid = rgb(45, 212, 191, 0.40f);

    c[ImGuiCol_Text] = rgb(229, 234, 241);
    c[ImGuiCol_TextDisabled] = rgb(120, 128, 140);
    c[ImGuiCol_WindowBg] = rgb(21, 23, 28);
    c[ImGuiCol_ChildBg] = rgb(24, 27, 33);
    c[ImGuiCol_PopupBg] = rgb(27, 31, 38);
    c[ImGuiCol_Border] = rgb(42, 47, 58, 0.85f);
    c[ImGuiCol_BorderShadow] = rgb(0, 0, 0, 0.0f);
    c[ImGuiCol_FrameBg] = rgb(33, 38, 47);
    c[ImGuiCol_FrameBgHovered] = rgb(42, 49, 60);
    c[ImGuiCol_FrameBgActive] = rgb(49, 57, 69);
    c[ImGuiCol_TitleBg] = rgb(21, 23, 28);
    c[ImGuiCol_TitleBgActive] = rgb(27, 31, 38);
    c[ImGuiCol_TitleBgCollapsed] = rgb(21, 23, 28);
    c[ImGuiCol_MenuBarBg] = rgb(24, 27, 33);
    c[ImGuiCol_ScrollbarBg] = rgb(0, 0, 0, 0.0f);
    c[ImGuiCol_ScrollbarGrab] = rgb(47, 55, 66);
    c[ImGuiCol_ScrollbarGrabHovered] = rgb(60, 69, 82);
    c[ImGuiCol_ScrollbarGrabActive] = accentMid;
    c[ImGuiCol_CheckMark] = accent;
    c[ImGuiCol_SliderGrab] = accent;
    c[ImGuiCol_SliderGrabActive] = rgb(80, 235, 214);
    c[ImGuiCol_Button] = rgb(38, 45, 56);
    c[ImGuiCol_ButtonHovered] = rgb(47, 57, 70);
    c[ImGuiCol_ButtonActive] = rgb(55, 66, 81);
    c[ImGuiCol_Header] = accentSoft;
    c[ImGuiCol_HeaderHovered] = accentMid;
    c[ImGuiCol_HeaderActive] = accentMid;
    c[ImGuiCol_Separator] = rgb(42, 47, 58, 0.7f);
    c[ImGuiCol_SeparatorHovered] = accentMid;
    c[ImGuiCol_SeparatorActive] = accent;
    c[ImGuiCol_ResizeGrip] = rgb(45, 212, 191, 0.20f);
    c[ImGuiCol_ResizeGripHovered] = accentMid;
    c[ImGuiCol_ResizeGripActive] = accent;
    c[ImGuiCol_Tab] = rgb(27, 31, 38);
    c[ImGuiCol_TabHovered] = accentMid;
    c[ImGuiCol_TabActive] = rgb(38, 45, 56);
    c[ImGuiCol_TabUnfocused] = rgb(24, 27, 33);
    c[ImGuiCol_TabUnfocusedActive] = rgb(33, 38, 47);
    c[ImGuiCol_DockingPreview] = accentMid;
    c[ImGuiCol_DockingEmptyBg] = rgb(18, 20, 25);
    c[ImGuiCol_PlotLines] = accent;
    c[ImGuiCol_PlotHistogram] = accent;
    c[ImGuiCol_TextSelectedBg] = accentSoft;
    c[ImGuiCol_NavHighlight] = accent;
    c[ImGuiCol_ModalWindowDimBg] = rgb(5, 7, 10, 0.65f);  // settle focus on open dialogs
    s.DisabledAlpha = 0.45f;

    if (scale != 1.0f) s.ScaleAllSizes(scale);
}

static const ImWchar* uiGlyphRanges() {
    // Latin + common typographic punctuation/symbols (dashes, ellipsis, arrows, bullets,
    // geometric shapes, degree/superscripts) so nice glyphs don't render blank.
    static const ImWchar r[] = {
        0x0020, 0x00FF,  // Basic Latin + Latin-1 Supplement
        0x2010, 0x2027,  // hyphens, dashes, quotes, ellipsis
        0x2070, 0x209F,  // super/subscripts
        0x2190, 0x21FF,  // arrows
        0x2022, 0x2022,  // bullet
        0x25A0, 0x25FF,  // geometric shapes (●)
        0,
    };
    return r;
}

static ImFont* addFont(ImGuiIO& io, const std::string& path, float px, ImFont* fallback) {
    ImFont* f = io.Fonts->AddFontFromFileTTF(path.c_str(), px, nullptr, uiGlyphRanges());
    return f ? f : fallback;
}

Fonts loadFonts(const std::string& assetsDir, float pxScale) {
    ImGuiIO& io = ImGui::GetIO();
    const std::string fd = assetsDir + "\\fonts\\";
    Fonts f;
    ImFont* def = io.Fonts->AddFontDefault();  // fallback if files are missing
    f.body = addFont(io, fd + "Rubik-Regular.ttf", 17.5f * pxScale, def);
    f.medium = addFont(io, fd + "Rubik-Medium.ttf", 17.5f * pxScale, f.body);
    f.h1 = addFont(io, fd + "Rubik-Bold.ttf", 30.0f * pxScale, f.body);
    f.h2 = addFont(io, fd + "Rubik-SemiBold.ttf", 19.0f * pxScale, f.body);
    f.mono = addFont(io, fd + "JetBrainsMono-Regular.ttf", 15.5f * pxScale, f.body);
    io.Fonts->Build();
    io.FontDefault = f.body;
    return f;
}

}  // namespace ui
