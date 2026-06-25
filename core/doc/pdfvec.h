// Minimal vector PDF writer — C++ port of engine/doc/pdfvec.py (W14).
//
// Hand-writes a spec-valid PDF 1.4 file (Catalog, Pages, Page, content stream, xref
// + trailer) with line-drawing operators at 1:1 mm scale. The PDF origin is
// bottom-left with +y up, matching the engine's sheet convention (no Y flip).
//
// The byte layout (objects, content-stream operators, %.3f float formatting, xref
// offsets) is identical to pdfvec.py::write_pdf for the same input, so the parity
// harness can gate page count + path count + bbox + (here) exact byte equality.
#pragma once

#include <array>
#include <string>
#include <vector>

namespace aicad::doc {

// One drawn polyline + its dashed flag (hidden lines are dashed). polyline is a
// list of [x,y] mm points; polylines with <2 points are skipped.
struct PdfLine {
    std::vector<std::array<double, 2>> pts;
    bool dashed = false;
};

// One TEXT label at an insertion point (mm), `height` mm tall. Used for drawing
// annotations on PDF export (Bug N-7). When the texts list is empty the output is
// byte-identical to the geometry-only writer (no Font object is emitted).
struct PdfText {
    std::array<double, 2> pos;
    double height = 3.0;
    std::string text;
};

// Build the full PDF bytes for `lines`. `page_w_mm`/`page_h_mm` set the page size;
// when `auto_page` is true the page is sized to the drawing bbox + a 10mm margin
// (and `page_w_mm`/`page_h_mm` are ignored). `path_count` (out) gets the number of
// stroked polylines. Throws std::runtime_error when there is no geometry.
std::string build_pdf(const std::vector<PdfLine>& lines, bool auto_page,
                      double page_w_mm, double page_h_mm, int* path_count);
std::string build_pdf(const std::vector<PdfLine>& lines, const std::vector<PdfText>& texts,
                      bool auto_page, double page_w_mm, double page_h_mm, int* path_count);

// Write `lines` to a single-page vector PDF at `path` (parent dirs created). When
// `auto_page` is false the caller-fixed page size is used. Throws std::runtime_error
// on no geometry / a write failure. Returns the resolved absolute path.
std::string write_pdf(const std::string& path, const std::vector<PdfLine>& lines,
                      bool auto_page = true, double page_w_mm = 0.0,
                      double page_h_mm = 0.0);
std::string write_pdf(const std::string& path, const std::vector<PdfLine>& lines,
                      const std::vector<PdfText>& texts, bool auto_page = true,
                      double page_w_mm = 0.0, double page_h_mm = 0.0);

}  // namespace aicad::doc
