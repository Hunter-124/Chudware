// Vector PDF writer for drawing sheets.
//
// Hand-writes a spec-valid PDF 1.4 file (Catalog, Pages, one Page + content stream per
// sheet, a shared Helvetica/WinAnsiEncoding font, xref + trailer). Each page renders an
// InkSheet in sheet millimetres (a `cm` maps mm → pt; origin bottom-left, +y up) with
// ISO 128 line classes: thin 0.25 mm, thick 0.5 mm, dashed [3 1.5], long-dash-dot
// [6 1 0.5 1], round caps/joins, filled arrowheads, and black Helvetica text aligned
// from the real AFM advance widths. Text stays as literal `(...)` strings.
#pragma once

#include <string>
#include <vector>

#include "doc/sheet_ink.h"

namespace chudware::doc {

// Build the full PDF bytes, one page per sheet. A sheet with fixed_page uses its
// page_w/page_h; otherwise the page is the ink bbox + a 10 mm margin. Throws
// std::runtime_error when there are no pages or a page has no ink.
std::string build_pdf(const std::vector<const InkSheet*>& pages);

// Write the PDF at `path` (parent dirs created). Returns the absolute path.
std::string write_pdf(const std::string& path, const std::vector<const InkSheet*>& pages);
std::string write_pdf(const std::string& path, const InkSheet& sheet);

// Helvetica advance width (mm) of UTF-8 `text` set at cap height `cap_h` mm, using the
// same WinAnsi mapping the writer applies.
double pdf_text_width(const std::string& text, double cap_h);

}  // namespace chudware::doc
