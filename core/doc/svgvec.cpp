// Editable SVG 1.1 vector writer — see svgvec.h.

#include "doc/svgvec.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <stdexcept>

namespace chudware::doc {
namespace {

constexpr double kMarginMm = 10.0;
constexpr double kPi = 3.14159265358979323846;
constexpr double kCapHeightEm = 0.718;  // Helvetica / Arial cap height per em

std::string num(double v) {
    char buf[48];
    std::snprintf(buf, sizeof(buf), "%.3f", v);
    std::size_t n = std::strlen(buf);
    while (n > 0 && buf[n - 1] == '0') --n;
    if (n > 0 && buf[n - 1] == '.') --n;
    std::string s(buf, n);
    if (s.empty() || s == "-0") return "0";
    return s;
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
            default:
                if (static_cast<unsigned char>(c) >= 0x20 || c == '\t') out.push_back(c);
                break;
        }
    }
    return out;
}

struct Xf {
    double dx, dy, page_h;
    std::string x(double v) const { return num(v + dx); }
    std::string y(double v) const { return num(page_h - (v + dy)); }
};

void path_data(std::string& d, const InkPolyline& pl, const Xf& xf) {
    d += "M" + xf.x(pl[0][0]) + " " + xf.y(pl[0][1]);
    for (std::size_t i = 1; i < pl.size(); ++i)
        d += " L" + xf.x(pl[i][0]) + " " + xf.y(pl[i][1]);
}

// One stroke class inside a layer: a <g> carrying width/dash, one <path> per polyline.
void stroke_group(std::string& out, const std::vector<InkPolyline>& pls,
                  const std::vector<InkArc>* arcs, const char* cls, double width,
                  const char* dash, const Xf& xf, int& paths) {
    bool any = false;
    for (const auto& pl : pls) any = any || pl.size() >= 2;
    if (arcs) any = any || !arcs->empty();
    if (!any) return;
    out += "    <g class=\"";
    out += cls;
    out += "\" stroke-width=\"" + num(width) + "\"";
    if (dash) {
        out += " stroke-dasharray=\"";
        out += dash;
        out += "\"";
    }
    out += ">\n";
    for (const auto& pl : pls) {
        if (pl.size() < 2) continue;
        std::string d;
        path_data(d, pl, xf);
        out += "      <path d=\"" + d + "\"/>\n";
        ++paths;
    }
    if (arcs)
        for (const InkArc& a : *arcs) {
            if (a.radius <= 0.0) continue;
            if (a.full()) {
                out += "      <circle cx=\"" + xf.x(a.center[0]) + "\" cy=\"" +
                       xf.y(a.center[1]) + "\" r=\"" + num(a.radius) + "\"/>\n";
            } else {
                const double sw = a.sweep();
                const double e = a.a0 + sw;
                // Sheet-CCW stays visually CCW after the Y flip: SVG sweep-flag 0.
                out += "      <path d=\"M" + xf.x(a.center[0] + a.radius * std::cos(a.a0)) +
                       " " + xf.y(a.center[1] + a.radius * std::sin(a.a0)) + " A" +
                       num(a.radius) + " " + num(a.radius) + " 0 " +
                       (sw > kPi ? "1" : "0") + " 0 " +
                       xf.x(a.center[0] + a.radius * std::cos(e)) + " " +
                       xf.y(a.center[1] + a.radius * std::sin(e)) + "\"/>\n";
            }
            ++paths;
        }
    out += "    </g>\n";
}

void append_layer(std::string& out, const char* id, const InkLayer& l, const Xf& xf,
                  int& paths) {
    out += "  <g id=\"";
    out += id;
    out += "\" inkscape:groupmode=\"layer\" inkscape:label=\"";
    out += id;
    out += "\" fill=\"none\" stroke=\"#000000\" stroke-linecap=\"round\" "
           "stroke-linejoin=\"round\">\n";
    const std::string dash = num(kInkDash[0]) + " " + num(kInkDash[1]);
    std::string chain;
    for (int i = 0; i < 4; ++i) chain += (i ? " " : "") + num(kInkChain[i]);
    stroke_group(out, l.thin, &l.arcs, "thin", kInkThin, nullptr, xf, paths);
    stroke_group(out, l.dashed, nullptr, "dashed", kInkThin, dash.c_str(), xf, paths);
    stroke_group(out, l.chain, nullptr, "chain", kInkThin, chain.c_str(), xf, paths);
    stroke_group(out, l.thick, nullptr, "thick", kInkThick, nullptr, xf, paths);
    stroke_group(out, l.chain_thick, nullptr, "chain-thick", kInkThick, chain.c_str(), xf,
                 paths);
    if (!l.tris.empty()) {
        out += "    <g class=\"arrowheads\" fill=\"#000000\" stroke=\"none\">\n";
        for (const auto& t : l.tris)
            out += "      <polygon points=\"" + xf.x(t[0][0]) + "," + xf.y(t[0][1]) + " " +
                   xf.x(t[1][0]) + "," + xf.y(t[1][1]) + " " + xf.x(t[2][0]) + "," +
                   xf.y(t[2][1]) + "\"/>\n";
        out += "    </g>\n";
    }
    for (const InkText& t : l.texts) {
        if (t.text.empty() || t.h <= 0.0) continue;
        const char* anchor = t.align == InkAlign::center ? "middle"
                           : t.align == InkAlign::right  ? "end"
                                                         : "start";
        out += "    <text x=\"" + xf.x(t.pos[0]) + "\" y=\"" + xf.y(t.baseline()) +
               "\" font-family=\"Helvetica, Arial, sans-serif\" font-size=\"" +
               num(t.h / kCapHeightEm) + "\" text-anchor=\"" + anchor +
               "\" fill=\"#000000\" stroke=\"none\" xml:space=\"preserve\">" +
               xml_escape(t.text) + "</text>\n";
    }
    out += "  </g>\n";
}

}  // namespace

std::string build_svg(const InkSheet& sheet, int* path_count) {
    double page_w = sheet.page_w, page_h = sheet.page_h;
    double dx = 0.0, dy = 0.0;
    if (!sheet.fixed_page || page_w <= 0.0 || page_h <= 0.0) {
        const InkBounds b = ink_bounds(sheet);
        if (!b.ok) throw std::runtime_error("write_svg: no drawing primitives to export");
        dx = kMarginMm - b.minx;
        dy = kMarginMm - b.miny;
        page_w = b.maxx - b.minx + 2.0 * kMarginMm;
        page_h = b.maxy - b.miny + 2.0 * kMarginMm;
    } else if (sheet.empty()) {
        throw std::runtime_error("write_svg: no drawing primitives to export");
    }
    const Xf xf{dx, dy, page_h};

    std::string out;
    out.reserve(1 << 16);
    out += "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n";
    out += "<!DOCTYPE svg PUBLIC \"-//W3C//DTD SVG 1.1//EN\" "
           "\"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd\">\n";
    out += "<svg xmlns=\"http://www.w3.org/2000/svg\" "
           "xmlns:inkscape=\"http://www.inkscape.org/namespaces/inkscape\" "
           "version=\"1.1\" width=\"" + num(page_w) + "mm\" height=\"" + num(page_h) +
           "mm\" viewBox=\"0 0 " + num(page_w) + " " + num(page_h) +
           "\" shape-rendering=\"geometricPrecision\">\n";
    out += "  <rect id=\"paper\" x=\"0\" y=\"0\" width=\"" + num(page_w) + "\" height=\"" +
           num(page_h) + "\" fill=\"#ffffff\" stroke=\"none\"/>\n";
    int paths = 0;
    append_layer(out, "frame", sheet.frame, xf, paths);
    append_layer(out, "hatch", sheet.hatch, xf, paths);
    append_layer(out, "hidden", sheet.hidden, xf, paths);
    append_layer(out, "visible", sheet.visible, xf, paths);
    append_layer(out, "annotations", sheet.annotations, xf, paths);
    append_layer(out, "tables", sheet.tables, xf, paths);
    out += "</svg>\n";
    if (path_count) *path_count = paths;
    return out;
}

std::string write_svg(const std::string& path, const InkSheet& sheet) {
    std::filesystem::path target(path);
    if (target.has_parent_path()) std::filesystem::create_directories(target.parent_path());
    const std::string bytes = build_svg(sheet, nullptr);
    std::ofstream stream(target, std::ios::binary | std::ios::trunc);
    if (!stream) throw std::runtime_error("write_svg: cannot open output: " + path);
    stream.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
    stream.close();
    if (!stream) throw std::runtime_error("write_svg: failed writing output: " + path);
    return std::filesystem::absolute(target).string();
}

}  // namespace chudware::doc
