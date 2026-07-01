#include "theme/theme.h"
#include <algorithm>
#include <cstdio>

namespace theme {

static Ctx g;
static ViewportBackdrop gBackdrop;

Ctx& ctx() { return g; }
ImFont* font(Font f) { return g.fonts[(int)f]; }

void setViewportBackdrop(const ViewportBackdrop& backdrop) { gBackdrop = backdrop; }
const ViewportBackdrop& viewportBackdrop() { return gBackdrop; }

static ImFont* loadFont(const std::string& dir, const char* file, float px,
                        const ImWchar* ranges = nullptr) {
    ImGuiIO& io = ImGui::GetIO();
    std::string path = dir + "/fonts/" + file;
    ImFontConfig cfg;
    cfg.OversampleH = 2;
    cfg.OversampleV = 1;
    cfg.PixelSnapH = true;
    ImFont* f = io.Fonts->AddFontFromFileTTF(path.c_str(), px, &cfg, ranges);
    if (!f) {
        std::fprintf(stderr, "[theme] missing font %s\n", path.c_str());
        f = io.Fonts->AddFontDefault();
    }
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
    const TypeScale& t = g.m.type;
    g.fonts[(int)Font::Body]  = loadFont(resourcesDir, "Rubik-Regular.ttf",    t.body     * s, glyphs);
    g.fonts[(int)Font::Label] = loadFont(resourcesDir, "Rubik-Medium.ttf",     t.headline * s, glyphs);
    g.fonts[(int)Font::H2]    = loadFont(resourcesDir, "Rubik-SemiBold.ttf",   t.title    * s, glyphs);
    g.fonts[(int)Font::H1]    = loadFont(resourcesDir, "Rubik-Bold.ttf",       t.largeTitle * s, glyphs);
    g.fonts[(int)Font::Mono]  = loadFont(resourcesDir, "JetBrainsMono-Regular.ttf", t.mono * s, glyphs);
    g.fonts[(int)Font::Icon]  = g.fonts[(int)Font::Label];
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
    s = ImGuiStyle{};

    const Metrics& m = g.m;
    const float sp = m.space.sm;
    s.WindowPadding     = ImVec2(m.space.md - 1.f, m.space.md - 3.f);
    s.FramePadding      = ImVec2(sp + 2.f, sp - 1.f);
    s.ItemSpacing       = ImVec2(sp, sp - 1.f);
    s.ItemInnerSpacing  = ImVec2(sp, sp - 2.f);
    s.CellPadding       = ImVec2(sp, sp - 2.f);
    s.IndentSpacing     = 18.f;
    s.ScrollbarSize     = 12.f;
    s.GrabMinSize       = 11.f;
    s.WindowBorderSize  = 1.f;
    s.ChildBorderSize   = 1.f;
    s.PopupBorderSize   = 1.f;
    s.FrameBorderSize   = g.dark ? 0.f : 1.f;
    s.WindowRounding    = m.roundingLg;
    s.ChildRounding     = m.roundingLg;
    s.FrameRounding     = m.rounding;
    s.PopupRounding     = 8.f;
    s.GrabRounding      = m.rounding;
    s.TabRounding       = 8.f;
    s.ScrollbarRounding = 10.f;
    s.WindowTitleAlign  = ImVec2(0.02f, 0.5f);
    s.WindowMenuButtonPosition = ImGuiDir_None;
    s.DockingSeparatorSize = 1.f;
    s.AntiAliasedLines  = true;
    s.AntiAliasedFill   = true;
    s.DisabledAlpha     = 0.45f;

    auto C = [](unsigned int hex) { return vec(hex); };
    const Palette& p = g.pal;
    ImVec4* c = s.Colors;
    c[ImGuiCol_Text]                  = C(p.txt0);
    c[ImGuiCol_TextDisabled]          = C(p.txt2);
    c[ImGuiCol_WindowBg]              = C(p.bg1);
    c[ImGuiCol_ChildBg]               = ImVec4(0, 0, 0, 0);
    c[ImGuiCol_PopupBg]               = C(p.glassBg);
    c[ImGuiCol_Border]                = C(p.line);
    c[ImGuiCol_BorderShadow]          = ImVec4(0, 0, 0, 0);
    c[ImGuiCol_FrameBg]               = C(p.bg2);
    c[ImGuiCol_FrameBgHovered]        = C(p.hover);
    c[ImGuiCol_FrameBgActive]         = C(p.active);
    c[ImGuiCol_TitleBg]               = ImVec4(0, 0, 0, 0);
    c[ImGuiCol_TitleBgActive]         = ImVec4(0, 0, 0, 0);
    c[ImGuiCol_TitleBgCollapsed]      = ImVec4(0, 0, 0, 0);
    c[ImGuiCol_MenuBarBg]             = C(p.bg1);
    c[ImGuiCol_ScrollbarBg]           = ImVec4(0, 0, 0, 0);
    c[ImGuiCol_ScrollbarGrab]         = C(p.bg3);
    c[ImGuiCol_ScrollbarGrabHovered]  = C(p.hover);
    c[ImGuiCol_ScrollbarGrabActive]  = C(p.accentMid);
    c[ImGuiCol_CheckMark]             = C(p.accent);
    c[ImGuiCol_SliderGrab]            = C(p.accent);
    c[ImGuiCol_SliderGrabActive]      = C(p.accentDim);
    c[ImGuiCol_Button]                = C(p.bg2);
    c[ImGuiCol_ButtonHovered]         = C(p.hover);
    c[ImGuiCol_ButtonActive]          = C(p.active);
    c[ImGuiCol_Header]                = C(p.accentSoft);
    c[ImGuiCol_HeaderHovered]         = C(p.accentMid);
    c[ImGuiCol_HeaderActive]          = C(p.accentMid);
    c[ImGuiCol_Separator]             = C(p.line);
    c[ImGuiCol_SeparatorHovered]      = C(p.accentMid);
    c[ImGuiCol_SeparatorActive]       = C(p.accent);
    c[ImGuiCol_ResizeGrip]            = C(p.accentSoft);
    c[ImGuiCol_ResizeGripHovered]     = C(p.accentMid);
    c[ImGuiCol_ResizeGripActive]      = C(p.accent);
    c[ImGuiCol_Tab]                   = C(p.bg1);
    c[ImGuiCol_TabHovered]            = C(p.accentMid);
    c[ImGuiCol_TabActive]             = C(p.bg2);
    c[ImGuiCol_TabUnfocused]          = C(p.bg1);
    c[ImGuiCol_TabUnfocusedActive]    = C(p.bg2);
    c[ImGuiCol_DockingPreview]        = C(p.accentMid);
    c[ImGuiCol_DockingEmptyBg]        = ImVec4(0, 0, 0, 0);
    c[ImGuiCol_PlotLines]             = C(p.accent);
    c[ImGuiCol_PlotHistogram]         = C(p.accent);
    c[ImGuiCol_TableHeaderBg]         = C(p.bg2);
    c[ImGuiCol_TableBorderStrong]     = C(p.line);
    c[ImGuiCol_TableBorderLight]      = C(p.line);
    c[ImGuiCol_TableRowBg]            = ImVec4(0, 0, 0, 0);
    c[ImGuiCol_TableRowBgAlt]         = ImVec4(1, 1, 1, 0.02f);
    c[ImGuiCol_TextSelectedBg]        = C(p.accentSoft);
    c[ImGuiCol_NavHighlight]          = C(p.accent);
    c[ImGuiCol_DragDropTarget]        = C(p.accent);
    c[ImGuiCol_ModalWindowDimBg]      = g.dark ? ImVec4(0.02f, 0.027f, 0.039f, 0.65f)
                                               : ImVec4(0.1f, 0.1f, 0.1f, 0.45f);

    s.ScaleAllSizes(g.scale);
}

// ---- adaptive contrast --------------------------------------------------------
AdaptiveSurface adaptiveSurface(float avgLuminance) {
    const Palette& p = g.pal;
    AdaptiveSurface out{};
    out.tint = vec(p.glassBg);

    if (avgLuminance < 0.35f) {
        out.textPrimary   = vec(p.vibrancyLightPrimary);
        out.textSecondary = vec(p.vibrancyLightSecondary);
        out.tint.w *= 1.05f;
    } else if (avgLuminance > 0.65f) {
        out.textPrimary   = vec(p.vibrancyDarkPrimary);
        out.textSecondary = vec(p.vibrancyDarkSecondary);
        if (avgLuminance > 0.55f)
            out.textSecondary.w = std::min(out.textSecondary.w, 0.85f);
    } else {
        out.textPrimary   = vec(p.txt0);
        out.textSecondary = vec(p.txt1);
    }
    return out;
}

// ---- glassBackground (5-layer, host/ui_theme.cpp) -----------------------------
void glassBackground(ImDrawList* dl, ImVec2 mn, ImVec2 mx, float rounding, float alpha) {
    const Palette& p = g.pal;
    const float s = S();
    const float r = rounding * s;
    auto A = [&](unsigned int role, float mul) -> ImU32 {
        return roleU32(role, mul * alpha);
    };

    {
        const float off  = 4.f * s;
        const float off2 = 2.5f * s;
        dl->AddRectFilled(
            ImVec2(mn.x + off2 * 0.4f, mn.y + off2),
            ImVec2(mx.x + off, mx.y + off),
            A(p.glassShadow, 0.35f), r + off * 0.5f);
        dl->AddRectFilled(
            ImVec2(mn.x + 1.f * s, mn.y + 2.f * s),
            ImVec2(mx.x + off2, mx.y + off2),
            A(p.glassShadow, 1.f), r + 0.5f * s);
    }

    dl->AddRectFilled(mn, mx, A(p.glassBg, 1.f), r);

    {
        const float shineH = (mx.y - mn.y) * 0.35f;
        const ImVec2 shineMx(mx.x, mn.y + shineH);
        const ImU32 top = A(p.glassHighlight, 1.f);
        const ImU32 btm = top & 0x00FFFFFFu;
        dl->AddRectFilledMultiColor(mn, shineMx, top, top, btm, btm);
    }

    dl->AddRect(mn, mx, A(p.glassBorder, 1.f), r, 0, 1.5f * s);

    {
        const float lineY = mn.y + 1.5f * s;
        const float inset = r * 0.6f;
        dl->AddLine(
            ImVec2(mn.x + inset, lineY),
            ImVec2(mx.x - inset, lineY),
            A(p.glassHighlight, 2.2f), 1.5f * s);
    }
}

void glassBackground(ImDrawList* dl, ImVec2 mn, ImVec2 mx, float rounding) {
    glassBackground(dl, mn, mx, rounding, 1.f);
}

void pushGlassTooltipStyle() {
    const float s = S();
    ImGui::PushStyleColor(ImGuiCol_PopupBg, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10.f * s, 8.f * s));
    ImGui::PushStyleVar(ImGuiStyleVar_PopupRounding, 8.f * s);
}

void popGlassTooltipStyle() {
    ImGui::PopStyleVar(2);
    ImGui::PopStyleColor(1);
}

void pushGlassWindowStyle() {
    const Palette& p = g.pal;
    ImGui::PushStyleColor(ImGuiCol_WindowBg, vec(p.glassBg));
}

void popGlassWindowStyle() { ImGui::PopStyleColor(1); }

void pushGlassChildStyle(float rounding) {
    const Palette& p = g.pal;
    ImGui::PushStyleColor(ImGuiCol_ChildBg, vec(p.glassBg));
    ImGui::PushStyleColor(ImGuiCol_Border, vec(p.glassBorder));
    ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding, rounding * S());
}

void popGlassChildStyle() {
    ImGui::PopStyleVar(1);
    ImGui::PopStyleColor(2);
}

void glassRowBackground(ImDrawList* dl, ImVec2 mn, ImVec2 mx, float rounding, bool stripe,
                        bool selected, float washAlpha) {
    const Palette& p = g.pal;
    const float s = S();
    const float r = rounding * s;
    const float inset = 2.f * s;
    ImVec2 pillMin(mn.x + inset, mn.y + inset);
    ImVec2 pillMax(mx.x - inset, mx.y - inset);

    dl->AddRectFilled(pillMin, pillMax, roleU32A(p.glassBg, washAlpha), r);

    if (washAlpha > 0.08f) {
        dl->AddRect(pillMin, pillMax, roleU32A(p.glassBorder, washAlpha * 0.55f), r, 0,
                    std::max(0.5f, 0.75f * s));
    }
    if (stripe) {
        dl->AddRectFilled(pillMin, pillMax, roleU32(p.glassHighlight, 0.35f), r);
    }
    if (selected) {
        ImVec4 fill = vec(p.accentSoft);
        fill.w = 0.48f;
        dl->AddRectFilled(pillMin, pillMax, ImGui::ColorConvertFloat4ToU32(fill), r);
        dl->AddRect(pillMin, pillMax, roleU32(p.accentMid), r, 0, std::max(1.f, 1.f * s));
        dl->AddRectFilled(ImVec2(pillMin.x, pillMin.y + 3.f * s),
                          ImVec2(pillMin.x + 4.f * s, pillMax.y - 3.f * s),
                          TCOL(accent), 2.f * s);
    }
}

void glassRowAtCursor(float rounding, bool stripe, bool selected, float rowPadV) {
    const float s = S();
    ImVec2 rowMin = ImGui::GetCursorScreenPos();
    float availW = ImGui::GetContentRegionAvail().x;
    const float rowH = ImGui::GetTextLineHeight() + rowPadV * s;
    ImVec2 rowMax(rowMin.x + availW, rowMin.y + rowH);
    float wash = selected ? rowWash::selected : rowWash::idle;
    glassRowBackground(ImGui::GetWindowDrawList(), rowMin, rowMax, rounding, stripe, selected, wash);
}

void glassPanel(ImDrawList* dl, const ImRect& rect, MaterialLevel level,
                float rounding, float avgLuminance, float alpha) {
    const MaterialToken mat = materialToken(level);
    const Metrics& m = g.m;
    const float r = (rounding < 0.f ? m.roundingLg : rounding) * S();
    const ImVec2 mn = rect.Min;
    const ImVec2 mx = rect.Max;

    AdaptiveSurface surf = avgLuminance >= 0.f ? adaptiveSurface(avgLuminance)
                                             : AdaptiveSurface{vec(g.pal.glassBg), vec(g.pal.txt0), vec(g.pal.txt1)};

    const ViewportBackdrop& bd = viewportBackdrop();
    if (bd.valid()) {
        ImU32 imgCol = IM_COL32(255, 255, 255, (int)(255.f * alpha));
        dl->AddImageRounded((ImTextureID)(intptr_t)bd.textureId, mn, mx,
                            bd.uvMin, bd.uvMax, imgCol, r);
        ImVec4 tint = surf.tint;
        tint.w = mat.tintAlpha * alpha;
        dl->AddRectFilled(mn, mx, ImGui::ColorConvertFloat4ToU32(tint), r);
    } else {
        glassBackground(dl, mn, mx, r / S(), alpha);
        ImVec4 tint = surf.tint;
        tint.w = mat.tintAlpha * 0.5f * alpha;
        if (tint.w > 0.001f)
            dl->AddRectFilled(mn, mx, ImGui::ColorConvertFloat4ToU32(tint), r);
    }

    const float hiA = g.dark ? 0.11f : 0.14f;
    const float lineY = mn.y + 1.5f * S();
    const float inset = r * 0.6f;
    dl->AddLine(ImVec2(mn.x + inset, lineY), ImVec2(mx.x - inset, lineY),
                roleU32A(g.pal.glassHighlight, hiA * alpha), 1.5f * S());
    dl->AddRect(mn, mx, roleU32(g.pal.glassBorder, alpha), r, 0, 1.5f * S());
}

} // namespace theme
