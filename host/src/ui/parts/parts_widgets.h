// Parts Library shared widgets (GUI V2 phase A): border-highlight rows, section headers,
// empty states, spinners, domain glyphs/icons, thumbnail helpers, base64.
#pragma once

#include <string>
#include <vector>

#include "imgui.h"

struct AppState;

namespace ui {
namespace parts {

struct PartsItem;

// Minimal base64 decode for parts_thumbnail image_b64 payloads.
std::vector<unsigned char> b64decode(const std::string& in);

// Section header: h2 font label + accent underline. firstInPane=true sits flush at the pane
// top (no leading gap); subsequent headers add breathing room above.
void sectionHeader(const char* label, bool firstInPane = false);

// Centered empty-state: 40px atlas icon (drawIconGlass, textDisabled) + message + optional sub.
void emptyState(const char* iconName, const char* msg, const char* sub = nullptr);

void spinner(const char* prefix);

// Flat vector glyph per part domain — the offline preview fallback.
void domainGlyph(ImDrawList* dl, ImVec2 c, float r, const std::string& domain);

// Taxonomy/domain id → atlas icon name ("cat_fasteners", …); nullptr when unmapped
// (caller falls back to domainGlyph vector art).
const char* partIcon(const std::string& domainOrTaxonomyId);

// Draw the part icon for a domain/taxonomy id at `center`, `sizePx` square: atlas icon via
// drawIconGlass when partIcon() maps it, else the domainGlyph vector fallback.
void partIconDraw(ImDrawList* dl, ImVec2 center, float sizePx, const std::string& id,
                  bool selected = false);

// Border-highlight row (V2 selection language: selection = BORDER, not wash).
//   hover/active → treeHover fill; selected → accent 1.5*S border; hover-not-selected →
//   accentMid 1.0*S border (both inset 0.5px so the stroke never bleeds outside).
//   Optional 18px icon (atlas name; skipped when null/unknown), tinted accent when selected.
//   Text vertically centered exactly; horizontally centered when centerText, else left after
//   the icon. size.x<=0 → content width; size.y<=0 → frame height. Returns clicked.
bool borderRow(const char* strId, const char* label, const char* icon, bool selected,
               ImVec2 size, bool centerText, float rounding = 6.f);

// Border treatment for MULTI-WIDGET rows (vendor cards, queue rows, API providers) where an
// InvisibleButton would eat the inner buttons: paints resting glass surface + hover/selected
// border at the cursor for `rows` frame-heights. Call immediately BEFORE the row's widgets.
void borderRowSurface(bool selected = false, float rows = 1.0f, float rounding = 6.f);

// Card border treatment over an explicit rect (grid cards).
void borderCardRect(ImDrawList* dl, ImVec2 p0, ImVec2 p1, bool selected, bool hovered,
                    float rounding = 8.f);

// ---- thumbnails --------------------------------------------------------------------
// Runs inside rpc.drain() on the GL thread — safe to create GL textures here.
void storeThumb(const std::string& id, const std::vector<unsigned char>& bytes);
void requestThumb(AppState& st, const PartsItem& item);
void requestVendorLogo(AppState& st, const std::string& site, const std::string& domain);

}  // namespace parts
}  // namespace ui
