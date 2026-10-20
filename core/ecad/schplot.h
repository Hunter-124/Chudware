#pragma once

#include <string>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

namespace chudware::ecad::plot {

struct PlotOpts {
    std::string sheet;
    bool frame = true;
    bool title_block = true;
    bool pin_numbers = true;
    bool pin_names = true;
    std::string paper = "A4";
    double margin_mm = 10.0;
};

// SVG entrypoints refuse unsafe filename-leaf sheet IDs/options with ecad::OpError.
std::vector<std::pair<std::string, std::string>> schematic_svg_pages(
    const nlohmann::json& sch, const PlotOpts& o);
std::string schematic_svg(const nlohmann::json& sch, const PlotOpts& o);
std::string schematic_pdf(const nlohmann::json& sch, const PlotOpts& o);
std::pair<double, double> paper_size_mm(const std::string& paper);

}  // namespace chudware::ecad::plot
