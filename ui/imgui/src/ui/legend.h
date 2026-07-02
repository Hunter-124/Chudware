#pragma once
// Result colormap legend overlay (bottom-right of viewport).
namespace app { struct AppState; }
struct ImDrawList;
struct ImVec2;

namespace ui {

void drawLegend(app::AppState& st, ImDrawList* dl, const ImVec2& r0, const ImVec2& r1);

}  // namespace ui
