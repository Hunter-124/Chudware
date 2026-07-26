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

// Section header: optional accent glyph + h2 label + optional right-aligned dim `trailing`
// text (counts/state), closed by an accent-to-transparent rule spanning the pane.
// firstInPane=true sits flush at the pane top (no leading gap); subsequent headers add
// breathing room above.
void sectionHeader(const char* label, bool firstInPane = false, const char* icon = nullptr,
                   const char* trailing = nullptr);

// Scoped horizontal content inset (ui::metrics::partsPadX — the ONE inset of this workspace).
// Everything drawn inside the scope lands on the pane's TEXT line: the line sectionHeader puts
// its label on, borderRow puts its icon/label on, a grid card puts its title on and ImGui puts
// the text inside a framed widget on (partsPadX == style.FramePadding.x). Wrap the plain text
// and hand-drawn content that would otherwise sit one padding to the left of everything around
// it. Open it AFTER a full-bleed surface (borderRowSurface) so the surface keeps the full width.
struct ContentInset {
    ContentInset();
    ~ContentInset();
    ContentInset(const ContentInset&)            = delete;
    ContentInset& operator=(const ContentInset&) = delete;
};

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
//   Text vertically centered exactly; horizontally centered when centerText, else inset by
//   ui::metrics::partsPadX() (this workspace's one content inset) and placed after the icon.
//   size.x<=0 → content width; size.y<=0 → frame height. Returns clicked.
bool borderRow(const char* strId, const char* label, const char* icon, bool selected,
               ImVec2 size, bool centerText, float rounding = 6.f);

// Pill toggle chip (facet values, quick filters). Unlike borderRow it paints a RESTING
// surface, so a wrapped block of them reads as a set of options rather than loose words,
// and it is sized from its own text so nothing is ever clipped. Caller handles PushID.
bool  chipToggle(const char* strId, const char* label, bool active);
float chipWidth(const char* label);   // exact footprint — use this for wrap arithmetic
float chipPadX();
float chipHeight();

// ---- colour-coded liquid glass (Parts Library V3) -------------------------------------
// The Parts Library used to be the one workspace with no colour of its own: every category,
// every filter field and every card was the same slate-on-slate frame, so the eye had nothing
// to navigate by. These three helpers give it the app's shared liquid-glass language
// (ui::glassCardWash / ui::coloredRowWash) with a per-category hue.

// Stable accent colour (RGB 0..1, never null) for a taxonomy id. The 19 taxonomy roots get
// hand-picked hues; a child inherits its root's hue via the id prefix before the first '.'
// ("fasteners.bolts_screws" -> the Fasteners hue), and an unmapped id falls back to a
// deterministic hash hue so vendor/synthetic nodes still differ from each other.
const float* categoryTint(const std::string& id);

// Liquid-glass row. Same interaction contract as borderRow (returns clicked, selection =
// accent border) but with a RESTING colour-coded surface: the shared glassCardWash gradient
// in `tint` plus a 3dp left rail in the same hue. `emphasis` scales the wash alpha —
// 1.0 for a taxonomy root, ~0.5 for a child row — so depth reads as weight, not just indent.
bool glassRow(const char* strId, const char* label, const char* icon, bool selected,
              ImVec2 size, const float tint[3], float emphasis = 1.0f,
              float rounding = 7.f);

// Liquid-glass background for a FRAMED WIDGET (InputText / InputFloat / Combo). ImGui paints
// ImGuiCol_FrameBg before the widget's own text, so a gradient can only go behind it by
// splitting the draw list: glassFieldBegin() makes the stock frame transparent and opens
// channel 1 for the widget, glassFieldEnd() paints the gradient into channel 0 from the
// widget's REAL item rect and hover/active state, then merges. Always pair them.
//   glassFieldBegin();
//   changed = ImGui::InputTextWithHint(...);
//   glassFieldEnd(tint);
void glassFieldBegin();
void glassFieldEnd(const float tint[3], float rounding = 6.f);

// Dim field label on the pane's content line (partsPadX). Plain TextDisabled would sit a
// frame padding left of the widget it labels — the "label and body don't line up" defect.
void fieldLabel(const char* text);

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

// Ask for a part thumbnail / vendor logo. These do NOT dispatch an RPC: they push onto a
// coalescing queue that flushThumbRequests() drains as batched `parts_thumbnails` calls
// (16 images per call, 3 calls in flight). Cheap enough to call for every row every frame —
// an id already resident, in flight, suppressed or queued is dropped on the spot.
void requestThumb(AppState& st, const PartsItem& item);
void requestVendorLogo(AppState& st, const std::string& site, const std::string& domain);

// Send whatever is queued right now, up to the in-flight budget. GUI thread only.
// OPTIONAL: requestThumb / requestVendorLogo already self-flush on a full batch and after an
// 80 ms coalescing window, so the pipeline is correct with no caller cooperation at all.
// Calling this once at the end of a pane that just enqueued a page of rows only makes the
// images land sooner.
void flushThumbRequests(AppState& st);

// Move a queued id to the FRONT of the pending queue so an on-screen row is served before a
// backlog of scrolled-past ones. No-op when the id is not queued (already resident, in
// flight, or never asked for).
void prioritizeThumb(const std::string& id);

}  // namespace parts
}  // namespace ui
