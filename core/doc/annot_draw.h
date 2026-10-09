// Shared annotation -> draw-primitives compiler for 2D drawings.
//
// A single source of truth that turns one drawing annotation (a dimension, callout,
// note, or any GD&T element) into pure 2D draw primitives in SHEET-MILLIMETRE paper
// space: stroked polylines, arcs, filled triangles (arrowheads), and text runs. Every
// consumer renders the SAME primitives so the on-screen sheet (host ImGui), the DXF/PDF
// vector exports, and the PNG raster (for AI vision review) all agree byte-for-byte in
// layout:
//   - host/src/ui/drawing_panel.cpp draws them with ImGui draw-list calls,
//   - core/doc/export2d.cpp emits them as DXF polylines/TEXT + PDF strokes/text,
//   - core/doc/session.cpp drawing_render_png rasterizes them.
//
// This is also why GD&T (feature control frames, datum symbols, surface-finish and weld
// symbols, the 14 ASME Y14.5 geometric-characteristic symbols, the ⌀ diameter sign, and
// the Ⓜ/Ⓛ material-condition modifiers) is compiled to VECTOR STROKES here: the host's
// bitmap glyph atlas cannot draw those symbols, and strokes render identically across all
// three back-ends. Plain numerals, datum letters, and Ra values stay as `texts` runs that
// each back-end draws with its own font.
//
// All geometry is in sheet mm (the projected page); annotations already carry their
// anchor points (a / b / center / pos) in sheet mm, so the compiler needs no projection.
// Sizes (text height, arrowhead length, frame cell padding) are paper-space mm constants
// so annotations scale with the sheet exactly like a real CAD drawing.
#pragma once

#include <array>
#include <string>

#include <nlohmann/json.hpp>

namespace chudware::doc {

// Compile one annotation into draw primitives. `units` is the drawing's unit system
// ("MMGS"|"IPS"), used only to format a fallback display string when the annotation has
// no composed `text`. Returns the v2 draw block:
//   {
//     "segments":    [ [[x,y],...], ... ],  // THIN continuous polylines (0.25 mm)
//     "thick":       [ [[x,y],...], ... ],  // THICK continuous polylines (0.5 mm)
//     "dashed":      [ [[x,y],...], ... ],  // THIN dashed polylines (hidden-line pattern)
//     "chain":       [ [[x,y],...], ... ],  // THIN long-dash-dot polylines (centre lines)
//     "chain_thick": [ [[x,y],...], ... ],  // THICK long-dash-dot polylines
//     "arcs":        [ {"center":[x,y],"radius":r,"a0":rad,"a1":rad}, ... ],  // THIN arcs
//     "tris":        [ [[x,y],[x,y],[x,y]], ... ], // FILLED triangles (arrowheads)
//     "texts":       [ {"pos":[x,y],"h":mm,"text":"...","align":"left|center|right",
//                       "valign":"base|mid|top"}, ... ],   // h = cap height (ISO 3098)
//     "pick":        [x,y]                          // primary label/leader anchor (hit-test)
//   }
// Dimension values stroke the ⌀ / ⌴ / ⌵ / ↧ symbols as vector glyphs, so their text runs
// never depend on a font carrying them. Never throws; a malformed annotation yields empty
// primitive arrays.
nlohmann::json compile_annotation(const nlohmann::json& ann, const std::string& units);

// Compile a sheet's complete frame (ISO 5457 border, zone grid + centring marks, and the
// ISO 7200-style title block with its projection symbol) into one draw block. `sheet` is a
// drawing_get sheet ({size_mm, title_block}); `units` the drawing unit system; sheet_index
// is 1-based. Empty block for a sheet without a paper size.
nlohmann::json compile_sheet_frame(const nlohmann::json& sheet, const std::string& units,
                                   int sheet_index, int sheet_count);

// Per-view decorations compiled from a projected view: section cutting-plane lines with
// viewing arrows + letters and detail boundary circles + letters (from the view's
// section_lines / detail_circles markers), plus the "SECTION A-A" / "DETAIL B  SCALE 2:1"
// caption under a derived view. Empty block when the view has none.
nlohmann::json compile_view_decorations(const nlohmann::json& view);

// Paper-space advance width (mm) of `text` set at cap height `h` (Helvetica metrics, with the
// stroked dimension symbols at their glyph widths) — the same estimate the compiler lays out by.
double annot_text_width(const std::string& text, double h);

// Paper rectangles [x0,y0,x1,y1] (sheet mm) of a w x h sheet: the area inside the drawing
// frame, and the title block the frame draws in its lower-right corner.
std::array<double, 4> sheet_frame_inner(double w, double h);
std::array<double, 4> sheet_title_block_rect(double w, double h);

// Compile section hatch for a closed polygon (already sheet mm). The style overload accepts
// `pattern` ("single" default, "cross", or "double"), `spacing_mm`, and `angle_deg`; unknown
// or unset fields retain the backward-compatible generic 45 degree / 3 mm hatch. `material`
// and `body` may accompany the style as provenance for per-body material selection.
nlohmann::json compile_hatch(const nlohmann::json& polygon, const nlohmann::json& style);

// Compatibility overload for existing callers.
nlohmann::json compile_hatch(const nlohmann::json& polygon, double spacing, double angle_deg);

// True for annotation kinds the compiler knows how to draw (so callers can detect
// pass-through / legacy kinds). Cheap string check, no allocation of primitives.
bool annot_is_known(const std::string& kind);

}  // namespace chudware::doc
