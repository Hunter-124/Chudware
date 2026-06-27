// Theme runtime: holds the active palette/metrics/scale + loaded fonts, maps the
// palette onto ImGuiStyle, and rasterizes the Rubik/JetBrains fonts at DPI scale.
#pragma once
#include <string>
#include "imgui.h"
#include "theme/tokens.h"

namespace theme {

enum class Font { Body, Label, H2, H1, Mono, Icon, _count };

struct Ctx {
    Palette pal;
    Metrics m;          // logical metrics (multiply by scale at use, like the old style guide §1)
    float   scale = 1.f;
    bool    dark  = true;
    bool    reduceMotion = false;
    ImFont* fonts[(int)Font::_count] = {};
};

Ctx&      ctx();
inline const Palette& pal() { return ctx().pal; }
inline float S() { return ctx().scale; }                 // the one scale value
inline float U(float n=1.f) { return n * ctx().m.grid * ctx().scale; }   // 4px grid unit
ImFont*   font(Font f);

// Load fonts from <resourcesDir>/fonts at basePx*scale, then apply the theme. Call once after
// ImGui context creation, before the first frame. resourcesDir is the staged resources/ folder.
void init(const std::string& resourcesDir, float scale, bool dark);

// Re-map the active palette onto ImGui::GetStyle() (colors) + ScaleAllSizes once. Call on theme/scale change.
void apply();

// Swap dark/light at runtime (re-applies colors; does NOT reload fonts).
void setDark(bool dark);

} // namespace theme

// Shorthand: theme color as ImU32 / ImVec4 by palette field name.
#define TCOL(field)  theme::rgba(theme::pal().field)
#define TVEC(field)  theme::vec (theme::pal().field)
