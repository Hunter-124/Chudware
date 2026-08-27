// Editable SVG 1.1 vector writer — see svgvec.h.

#include "doc/svgvec.h"

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <limits>
#include <stdexcept>

namespace chudware::doc {
namespace {

constexpr double kMarginMm = 10.0;

std::string f3(double value) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.3f", value);
    return buf;
}

std::string xml_escape(const std::string& text) {
    std::string out;
    out.reserve(text.size());
    for (char c : text) {
        switch (c) {
            case '&': out += "&amp;"; break;
            case '<': out += "&lt;"; break;
            case '>': out += "&gt;"; break;
            case '"': out += "&quot;"; break;
            case '\'': out += "&apos;"; break;
            default: out.push_back(c); break;
        }
    }
    return out;
}

struct Bbox {
    bool ok = false;
    double minx = 0.0;
    double miny = 0.0;
    double maxx = 0.0;
    double maxy = 0.0;
};

Bbox bbox_of(const SvgSheet& sheet) {
    Bbox box;
    double minx = std::numeric_limits<double>::infinity();
    double miny = std::numeric_limits<double>::infinity();
    double maxx = -std::numeric_limits<double>::infinity();
    double maxy = -std::numeric_limits<double>::infinity();
    auto add = [&](double x, double y) {
        box.ok = true;
        if (x < minx) minx = x;
        if (y < miny) miny = y;
        if (x > maxx) maxx = x;
        if (y > maxy) maxy = y;
    };
    auto add_lines = [&](const std::vector<SvgLine>& lines) {
        for (const SvgLine& line : lines)
            for (const auto& point : line.pts) add(point[0], point[1]);
    };
    add_lines(sheet.visible);
    add_lines(sheet.hidden);
    add_lines(sheet.annotations);
    add_lines(sheet.tables);
    for (const SvgText& text : sheet.annotation_text) add(text.pos[0], text.pos[1]);
    for (const SvgText& text : sheet.table_text) add(text.pos[0], text.pos[1]);
    if (box.ok) {
        box.minx = minx;
        box.miny = miny;
        box.maxx = maxx;
        box.maxy = maxy;
    }
    return box;
}

void append_path(std::string& out, const SvgLine& line, double dx, double dy,
                 double page_h) {
    if (line.pts.size() < 2) return;
    out += "    <path d=\"M " + f3(line.pts[0][0] + dx) + " " +
           f3(page_h - (line.pts[0][1] + dy));
    for (std::size_t i = 1; i < line.pts.size(); ++i)
        out += " L " + f3(line.pts[i][0] + dx) + " " +
               f3(page_h - (line.pts[i][1] + dy));
    out += "\"";
    if (line.dashed) out += " stroke-dasharray=\"2.5,2.5\"";
    out += "/>\n";
}

void append_text(std::string& out, const SvgText& text, double dx, double dy,
                 double page_h, const char* fill) {
    if (text.text.empty()) return;
    out += "    <text x=\"" + f3(text.pos[0] + dx) + "\" y=\"" +
           f3(page_h - (text.pos[1] + dy)) +
           "\" font-family=\"Helvetica, Arial, sans-serif\" font-size=\"" +
           f3(text.height) + "\" fill=\"" + fill +
           "\" stroke=\"none\" xml:space=\"preserve\">" + xml_escape(text.text) +
           "</text>\n";
}

void append_layer(std::string& out, const char* id, const std::vector<SvgLine>& lines,
                  const std::vector<SvgText>& texts, double dx, double dy,
                  double page_h, const char* stroke, const char* fill) {
    out += "  <g id=\"";
    out += id;
    out += "\" inkscape:groupmode=\"layer\" inkscape:label=\"";
    out += id;
    out += "\" fill=\"none\" stroke=\"";
    out += stroke;
    out += "\" stroke-width=\"0.35\" stroke-linecap=\"round\" "
           "stroke-linejoin=\"round\">\n";
    for (const SvgLine& line : lines) append_path(out, line, dx, dy, page_h);
    for (const SvgText& text : texts) append_text(out, text, dx, dy, page_h, fill);
    out += "  </g>\n";
}

}  // namespace

std::string build_svg(const SvgSheet& sheet, bool auto_page, double page_w_mm,
                      double page_h_mm, int* path_count) {
    const Bbox box = bbox_of(sheet);
    if (!box.ok) throw std::runtime_error("write_svg: no drawing primitives to export");

    double dx = 0.0;
    double dy = 0.0;
    if (auto_page) {
        dx = kMarginMm - box.minx;
        dy = kMarginMm - box.miny;
        page_w_mm = box.maxx - box.minx + 2.0 * kMarginMm;
        page_h_mm = box.maxy - box.miny + 2.0 * kMarginMm;
    } else if (page_w_mm <= 0.0 || page_h_mm <= 0.0) {
        throw std::runtime_error("write_svg: fixed page dimensions must be positive");
    }

    int paths = 0;
    auto count_paths = [&](const std::vector<SvgLine>& lines) {
        for (const SvgLine& line : lines)
            if (line.pts.size() >= 2) ++paths;
    };
    count_paths(sheet.visible);
    count_paths(sheet.hidden);
    count_paths(sheet.annotations);
    count_paths(sheet.tables);
    if (path_count) *path_count = paths;

    std::string out;
    out += "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n";
    out += "<!DOCTYPE svg PUBLIC \"-//W3C//DTD SVG 1.1//EN\" "
           "\"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd\">\n";
    out += "<svg xmlns=\"http://www.w3.org/2000/svg\" "
           "xmlns:inkscape=\"http://www.inkscape.org/namespaces/inkscape\" "
           "version=\"1.1\" width=\"" + f3(page_w_mm) + "mm\" height=\"" +
           f3(page_h_mm) + "mm\" viewBox=\"0 0 " + f3(page_w_mm) + " " +
           f3(page_h_mm) + "\" shape-rendering=\"geometricPrecision\">\n";
    append_layer(out, "visible", sheet.visible, {}, dx, dy, page_h_mm, "#000000", "#000000");
    append_layer(out, "hidden", sheet.hidden, {}, dx, dy, page_h_mm, "#666666", "#666666");
    append_layer(out, "annotations", sheet.annotations, sheet.annotation_text, dx, dy,
                 page_h_mm, "#000099", "#000099");
    append_layer(out, "tables", sheet.tables, sheet.table_text, dx, dy, page_h_mm,
                 "#000000", "#000000");
    out += "</svg>\n";
    return out;
}

std::string write_svg(const std::string& path, const SvgSheet& sheet, bool auto_page,
                      double page_w_mm, double page_h_mm) {
    std::filesystem::path target(path);
    if (target.has_parent_path()) std::filesystem::create_directories(target.parent_path());
    const std::string bytes = build_svg(sheet, auto_page, page_w_mm, page_h_mm, nullptr);
    std::ofstream stream(target, std::ios::binary | std::ios::trunc);
    if (!stream) throw std::runtime_error("write_svg: cannot open output: " + path);
    stream.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
    stream.close();
    if (!stream) throw std::runtime_error("write_svg: failed writing output: " + path);
    return std::filesystem::absolute(target).string();
}

}  // namespace chudware::doc
