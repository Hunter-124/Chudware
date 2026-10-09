// Dependency-free SVG 1.1 writer for editable 2D drawing sheets.
//
// Renders an InkSheet as Inkscape layers (frame, hatch, hidden, visible, annotations,
// tables — always emitted, even when empty) on white paper. Strokes carry the ISO 128
// weights + dash arrays per class, arrowheads are filled polygons, arcs are native SVG
// arcs/circles, and text stays editable <text> in Helvetica with text-anchor from the
// contract alignment and an explicit baseline. Sheet Y is flipped into SVG's y-down.
#pragma once

#include <string>

#include "doc/sheet_ink.h"

namespace chudware::doc {

// Build the SVG document. A sheet without fixed_page is sized to its ink bbox + 10 mm.
// `path_count` (out) receives the number of stroked paths. Throws std::runtime_error
// when there is no ink to draw.
std::string build_svg(const InkSheet& sheet, int* path_count = nullptr);

// Write one SVG sheet at `path` (parent directories created) and return its absolute path.
std::string write_svg(const std::string& path, const InkSheet& sheet);

}  // namespace chudware::doc
