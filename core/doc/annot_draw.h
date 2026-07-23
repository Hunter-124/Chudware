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

#include <string>

#include <nlohmann/json.hpp>

namespace chudware::doc {

// Compile one annotation into draw primitives. `units` is the drawing's unit system
// ("MMGS"|"IPS"), used only to format a fallback display string when the annotation has
// no composed `text`. Returns:
//   {
//     "segments": [ [[x,y],[x,y],...], ... ],  // stroked thin SOLID polylines (sheet mm)
//     "dashed":   [ [[x,y],[x,y],...], ... ],  // stroked thin DASHED polylines (centerlines)
//     "arcs":     [ {"center":[x,y],"radius":r,"a0":rad,"a1":rad}, ... ],
//     "tris":     [ [[x,y],[x,y],[x,y]], ... ], // FILLED triangles (arrowheads)
//     "texts":    [ {"pos":[x,y],"h":mm,"text":"...","align":"left|center|right",
//                    "valign":"base|mid|top"}, ... ],
//     "pick":     [x,y]                          // primary label/leader anchor (hit-test)
//   }
// Never throws; an unrecognized / malformed annotation yields empty primitive arrays.
nlohmann::json compile_annotation(const nlohmann::json& ann, const std::string& units);

// Compile the 45° section hatch for a closed polygon (already sheet mm). `spacing` and
// `angle_deg` default to 3 mm / 45°. Returns {"segments":[...]} of clipped hatch lines.
// Used to move section hatching off the host so exports/PNG hatch too.
nlohmann::json compile_hatch(const nlohmann::json& polygon, double spacing, double angle_deg);

// True for annotation kinds the compiler knows how to draw (so callers can detect
// pass-through / legacy kinds). Cheap string check, no allocation of primitives.
bool annot_is_known(const std::string& kind);

}  // namespace chudware::doc
