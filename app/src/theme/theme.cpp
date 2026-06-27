#include "theme/theme.h"
#include <cstdio>

namespace theme {

static Ctx g;
Ctx& ctx() { return g; }
ImFont* font(Font f) { return g.fonts[(int)f]; }

static ImFont* loadFont(const std::string& dir, const char* file, float px,
                        const ImWchar* ranges = nullptr) {
    ImGuiIO& io = ImGui::GetIO();
    std::string path = dir + "/fonts/" + file;
    ImFontConfig cfg;
    cfg.OversampleH = 2; cfg.OversampleV = 1; cfg.PixelSnapH = true;
    ImFont* f = io.Fonts->AddFontFromFileTTF(path.c_str(), px, &cfg, ranges);
    if (!f) { std::fprintf(stderr, "[theme] missing font %s\n", path.c_str()); f = io.Fonts->AddFontDefault(); }
    return f;
}

void init(const std::string& resourcesDir, float scale, bool dark) {
    g.scale = scale;
    g.dark  = dark;
    g.pal   = dark ? darkPalette() : lightPalette();
    g.m     = Metrics{};

    ImGuiIO& io = ImGui::GetIO();
    io.Fonts->Clear();
    const ImWchar* glyphs = io.Fonts->GetGlyphRangesDefault();
    const float s = scale;
    g.fonts[(int)Font::Body]  = loadFont(resourcesDir, "Rubik-Regular.ttf",      15.0f * s, glyphs);
    g.fonts[(int)Font::Label] = loadFont(resourcesDir, "Rubik-Medium.ttf",       15.0f * s, glyphs);
    g.fonts[(int)Font::H2]    = loadFont(resourcesDir, "Rubik-SemiBold.ttf",     17.0f * s, glyphs);
    g.fonts[(int)Font::H1]    = loadFont(resourcesDir, "Rubik-Bold.ttf",         26.0f * s, glyphs);
    g.fonts[(int)Font::Mono]  = loadFont(resourcesDir, "JetBrainsMono-Regular.ttf", 13.5f * s, glyphs);
    g.fonts[(int)Font::Icon]  = g.fonts[(int)Font::Label];   // placeholder until the icon atlas ships
    io.Fonts->Build();
    io.FontDefault = g.fonts[(int)Font::Body];

    apply();
}

void setDark(bool dark) {
    g.dark = dark;
    g.pal  = dark ? darkPalette() : lightPalette();
    apply();
}

void apply() {
    ImGuiStyle& s = ImGui::GetStyle();
    s = ImGuiStyle{};   // reset to defaults, then theme + scale once

    const Metrics& m = g.m;
    s.WindowPadding     = ImVec2(14, 12);
    s.FramePadding      = ImVec2(10, 7);
    s.ItemSpacing       = ImVec2(8, 7);
    s.ItemInnerSpacing  = ImVec2(8, 6);
    s.CellPadding       = ImVec2(8, 6);
    s.IndentSpacing     = 18;
    s.ScrollbarSize     = 12;
    s.GrabMinSize       = 11;
    s.WindowBorderSize  = 1;
    s.ChildBorderSize   = 1;
    s.PopupBorderSize   = 1;
    s.FrameBorderSize   = 0;
    s.WindowRounding    = m.roundingLg;
    s.ChildRounding     = m.rounding;
    s.FrameRounding     = m.rounding;
    s.PopupRounding     = 8;
    s.GrabRounding      = m.rounding;
    s.TabRounding       = 7;
    s.ScrollbarRounding = 10;
    s.WindowTitleAlign  = ImVec2(0.0f, 0.5f);
    s.WindowMenuButtonPosition = ImGuiDir_None;
    s.DockingSeparatorSize = 1.0f;

    auto C = [](unsigned int hex){ return theme::vec(hex); };
    const Palette& p = g.pal;
    ImVec4* c = s.Colors;
    c[ImGuiCol_Text]                  = C(p.txt0);
    c[ImGuiCol_TextDisabled]          = C(p.txt2);
    c[ImGuiCol_WindowBg]              = C(p.bg1);
    c[ImGuiCol_ChildBg]               = ImVec4(0,0,0,0);
    c[ImGuiCol_PopupBg]               = C(p.bg2);
    c[ImGuiCol_Border]                = C(p.line);
    c[ImGuiCol_BorderShadow]          = ImVec4(0,0,0,0);
    c[ImGuiCol_FrameBg]               = C(p.bg2);
    c[ImGuiCol_FrameBgHovered]        = C(p.hover);
    c[ImGuiCol_FrameBgActive]         = C(p.active);
    c[ImGuiCol_TitleBg]               = C(p.bg0);
    c[ImGuiCol_TitleBgActive]         = C(p.bg0);
    c[ImGuiCol_TitleBgCollapsed]      = C(p.bg0);
    c[ImGuiCol_MenuBarBg]             = C(p.bg1);
    c[ImGuiCol_ScrollbarBg]           = ImVec4(0,0,0,0);
    c[ImGuiCol_ScrollbarGrab]         = C(p.bg3);
    c[ImGuiCol_ScrollbarGrabHovered]  = C(p.hover);
    c[ImGuiCol_ScrollbarGrabActive]   = C(p.active);
    c[ImGuiCol_CheckMark]             = C(p.accent);
    c[ImGuiCol_SliderGrab]            = C(p.accent);
    c[ImGuiCol_SliderGrabActive]      = C(p.accent);
    c[ImGuiCol_Button]                = C(p.bg2);
    c[ImGuiCol_ButtonHovered]         = C(p.hover);
    c[ImGuiCol_ButtonActive]          = C(p.active);
    c[ImGuiCol_Header]                = C(p.bg3);
    c[ImGuiCol_HeaderHovered]         = C(p.hover);
    c[ImGuiCol_HeaderActive]          = C(p.active);
    c[ImGuiCol_Separator]             = C(p.line);
    c[ImGuiCol_SeparatorHovered]      = C(p.accent);
    c[ImGuiCol_SeparatorActive]       = C(p.accent);
    c[ImGuiCol_ResizeGrip]            = ImVec4(0,0,0,0);
    c[ImGuiCol_ResizeGripHovered]     = C(p.accent);
    c[ImGuiCol_ResizeGripActive]      = C(p.accent);
    c[ImGuiCol_Tab]                   = C(p.bg1);
    c[ImGuiCol_TabHovered]            = C(p.hover);
    c[ImGuiCol_TabActive]             = C(p.bg2);
    c[ImGuiCol_TabUnfocused]          = C(p.bg1);
    c[ImGuiCol_TabUnfocusedActive]    = C(p.bg2);
    c[ImGuiCol_DockingPreview]        = C(p.accentDim);
    c[ImGuiCol_DockingEmptyBg]        = C(p.bg0);
    c[ImGuiCol_PlotLines]             = C(p.accent);
    c[ImGuiCol_PlotHistogram]         = C(p.accent);
    c[ImGuiCol_TableHeaderBg]         = C(p.bg2);
    c[ImGuiCol_TableBorderStrong]     = C(p.line);
    c[ImGuiCol_TableBorderLight]      = C(p.line);
    c[ImGuiCol_TableRowBg]            = ImVec4(0,0,0,0);
    c[ImGuiCol_TableRowBgAlt]         = ImVec4(1,1,1,0.02f);
    c[ImGuiCol_TextSelectedBg]        = C(p.accentDim);
    c[ImGuiCol_NavHighlight]          = C(p.accent);
    c[ImGuiCol_DragDropTarget]        = C(p.accent);
    c[ImGuiCol_ModalWindowDimBg]      = ImVec4(0,0,0,0.45f);

    s.ScaleAllSizes(g.scale);   // exactly once (style guide §1)
}

} // namespace theme
