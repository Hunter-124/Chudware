// Minimal dependency-free SVG 1.1 writer for editable 2D drawing sheets.
#pragma once

#include <array>
#include <string>
#include <vector>

namespace chudware::doc {

struct SvgLine {
    std::vector<std::array<double, 2>> pts;
    bool dashed = false;
};

struct SvgText {
    std::array<double, 2> pos;
    double height = 3.0;
    std::string text;
};

// The four stable drawing layers are emitted as real SVG/Inkscape groups even when empty.
// Geometry stays as paths and labels stay as text so downstream editors can modify both.
struct SvgSheet {
    std::vector<SvgLine> visible;
    std::vector<SvgLine> hidden;
    std::vector<SvgLine> annotations;
    std::vector<SvgLine> tables;
    std::vector<SvgText> annotation_text;
    std::vector<SvgText> table_text;
};

std::string build_svg(const SvgSheet& sheet, bool auto_page, double page_w_mm,
                      double page_h_mm, int* path_count = nullptr);

// Write one SVG sheet at `path` (parent directories created) and return its absolute path.
std::string write_svg(const std::string& path, const SvgSheet& sheet,
                      bool auto_page = true, double page_w_mm = 0.0,
                      double page_h_mm = 0.0);

}  // namespace chudware::doc
