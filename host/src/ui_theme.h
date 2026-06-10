// AI-CAD UI theme: rounded modern dark style + custom Rubik / JetBrains Mono fonts.
#pragma once
#include <string>

struct ImFont;

namespace ui {

struct Fonts {
    ImFont* body = nullptr;    // Rubik Regular
    ImFont* medium = nullptr;  // Rubik Medium (labels/buttons)
    ImFont* h1 = nullptr;      // Rubik Bold (titles)
    ImFont* h2 = nullptr;      // Rubik SemiBold (section headers)
    ImFont* mono = nullptr;    // JetBrains Mono (code/console)
};

// Accent + key colors (RGB 0..1) so panels can match the theme.
struct Palette {
    float accent[3] = {0.176f, 0.831f, 0.749f};   // teal #2DD4BF
    float accentDim[3] = {0.118f, 0.553f, 0.498f};
    float textDim[3] = {0.52f, 0.56f, 0.62f};
    float panel[3] = {0.094f, 0.106f, 0.129f};
};

void applyTheme(float scale = 1.0f);
Fonts loadFonts(const std::string& assetsDir, float pxScale = 1.0f);
extern Palette palette;

}  // namespace ui
