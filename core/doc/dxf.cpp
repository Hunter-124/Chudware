// DXF R12 ASCII writer — see dxf.h. Byte-faithful port of engine/doc/dxf.py.

#include "doc/dxf.h"

#include <cmath>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <stdexcept>

namespace aicad::doc {

namespace {

// --- _tag: append one DXF group-code / value pair (two physical lines) ---------
// Mirrors engine/doc/dxf.py::_tag exactly: an integer code line, then a value line.
void tag_int(std::vector<std::string>& out, int code, long long value) {
    out.push_back(std::to_string(code));
    out.push_back(std::to_string(value));
}

void tag_str(std::vector<std::string>& out, int code, const std::string& value) {
    out.push_back(std::to_string(code));
    out.push_back(value);
}

// Float formatting matches Python: f"{value:.6f}".rstrip("0").rstrip(".").
// NaN maps to "0" (the oracle's `if value == value else "0"` guard).
std::string fmt_float(double value) {
    if (!(value == value)) return "0";  // NaN
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.6f", value);
    std::string s(buf);
    // rstrip("0")
    std::size_t end = s.size();
    while (end > 0 && s[end - 1] == '0') --end;
    s.resize(end);
    // rstrip(".")
    end = s.size();
    while (end > 0 && s[end - 1] == '.') --end;
    s.resize(end);
    return s;
}

void tag_float(std::vector<std::string>& out, int code, double value) {
    out.push_back(std::to_string(code));
    out.push_back(fmt_float(value));
}

// Layer styling: name -> (aci_color, linetype). Mirrors _LAYER_STYLE / _DEFAULT_STYLE.
struct Style {
    int color;
    const char* ltype;
};
Style layer_style(const std::string& name) {
    if (name == "VISIBLE") return {7, "CONTINUOUS"};
    if (name == "HIDDEN") return {1, "DASHED"};
    if (name == "OUTLINE") return {7, "CONTINUOUS"};
    if (name == "BEND") return {3, "CONTINUOUS"};
    if (name == "DIMENSIONS" || name == "ANNOTATIONS") return {5, "CONTINUOUS"};  // blue
    return {7, "CONTINUOUS"};  // _DEFAULT_STYLE
}

void header(std::vector<std::string>& out) {
    tag_str(out, 0, "SECTION");
    tag_str(out, 2, "HEADER");
    tag_str(out, 9, "$ACADVER");
    tag_str(out, 1, "AC1009");  // R12
    tag_str(out, 0, "ENDSEC");
}

void tables(std::vector<std::string>& out, const std::vector<std::string>& names) {
    tag_str(out, 0, "SECTION");
    tag_str(out, 2, "TABLES");

    // LTYPE table: CONTINUOUS + DASHED.
    tag_str(out, 0, "TABLE");
    tag_str(out, 2, "LTYPE");
    tag_int(out, 70, 2);
    // CONTINUOUS
    tag_str(out, 0, "LTYPE");
    tag_str(out, 2, "CONTINUOUS");
    tag_int(out, 70, 0);
    tag_str(out, 3, "Solid line");
    tag_int(out, 72, 65);
    tag_int(out, 73, 0);
    tag_float(out, 40, 0.0);
    // DASHED: dash 5, gap 5.
    tag_str(out, 0, "LTYPE");
    tag_str(out, 2, "DASHED");
    tag_int(out, 70, 0);
    tag_str(out, 3, "Dashed __ __ __");
    tag_int(out, 72, 65);
    tag_int(out, 73, 2);
    tag_float(out, 40, 10.0);
    tag_float(out, 49, 5.0);
    tag_float(out, 49, -5.0);
    tag_str(out, 0, "ENDTAB");

    // LAYER table.
    tag_str(out, 0, "TABLE");
    tag_str(out, 2, "LAYER");
    tag_int(out, 70, static_cast<long long>(names.size()));
    for (const std::string& name : names) {
        Style st = layer_style(name);
        tag_str(out, 0, "LAYER");
        tag_str(out, 2, name);
        tag_int(out, 70, 0);
        tag_int(out, 62, st.color);
        tag_str(out, 6, st.ltype);
    }
    tag_str(out, 0, "ENDTAB");

    tag_str(out, 0, "ENDSEC");
}

void polyline_entity(std::vector<std::string>& out, const std::string& layer,
                     const Polyline& pts) {
    tag_str(out, 0, "POLYLINE");
    tag_str(out, 8, layer);
    tag_int(out, 66, 1);
    tag_int(out, 70, 0);
    tag_float(out, 10, 0.0);
    tag_float(out, 20, 0.0);
    tag_float(out, 30, 0.0);
    for (const auto& p : pts) {
        tag_str(out, 0, "VERTEX");
        tag_str(out, 8, layer);
        tag_float(out, 10, p[0]);
        tag_float(out, 20, p[1]);
        tag_float(out, 30, 0.0);
    }
    tag_str(out, 0, "SEQEND");
    tag_str(out, 8, layer);
}

void line_entity(std::vector<std::string>& out, const std::string& layer,
                 const std::array<double, 2>& a, const std::array<double, 2>& b) {
    tag_str(out, 0, "LINE");
    tag_str(out, 8, layer);
    tag_float(out, 10, a[0]);
    tag_float(out, 20, a[1]);
    tag_float(out, 11, b[0]);
    tag_float(out, 21, b[1]);
}

void text_entity(std::vector<std::string>& out, const DxfText& t) {
    tag_str(out, 0, "TEXT");
    tag_str(out, 8, t.layer);
    tag_float(out, 10, t.pos[0]);
    tag_float(out, 20, t.pos[1]);
    tag_float(out, 30, 0.0);
    tag_float(out, 40, t.height);
    tag_str(out, 1, t.text);
}

int entities(std::vector<std::string>& out, const DxfLayers& layers,
             const std::string& polyline_as) {
    tag_str(out, 0, "SECTION");
    tag_str(out, 2, "ENTITIES");
    int n = 0;
    for (const std::string& layer : layers.names) {
        auto it = layers.polylines.find(layer);
        if (it == layers.polylines.end()) continue;
        for (const Polyline& pl : it->second) {
            // Skip points with <2 coords is implicit (array<2>); skip pl with <2 pts.
            if (pl.size() < 2) continue;
            if (polyline_as == "line") {
                for (std::size_t i = 1; i < pl.size(); ++i) {
                    line_entity(out, layer, pl[i - 1], pl[i]);
                    ++n;
                }
            } else {
                polyline_entity(out, layer, pl);
                ++n;
            }
        }
    }
    // TEXT entities last (after the geometry layers), in insertion order. Empty in the
    // plain geometry-only path, so non-annotated drawings are byte-identical to before.
    for (const DxfText& t : layers.texts) {
        if (t.text.empty()) continue;
        text_entity(out, t);
        ++n;
    }
    tag_str(out, 0, "ENDSEC");
    return n;
}

}  // namespace

std::string build_dxf(const DxfLayers& layers, const std::string& polyline_as,
                      int* entity_count) {
    std::vector<std::string> out;
    header(out);
    tables(out, layers.names);
    int n = entities(out, layers, polyline_as);
    tag_str(out, 0, "EOF");
    if (entity_count) *entity_count = n;

    // CRLF join + trailing CRLF (matches the Python "\r\n".join(out) + "\r\n").
    std::string text;
    for (const std::string& line : out) {
        text += line;
        text += "\r\n";
    }
    return text;
}

std::string write_dxf(const std::string& path, const DxfLayers& layers,
                      const std::string& polyline_as) {
    std::filesystem::path p(path);
    if (p.has_parent_path()) std::filesystem::create_directories(p.parent_path());

    int n = 0;
    std::string text = build_dxf(layers, polyline_as, &n);
    if (n == 0)
        throw std::runtime_error(
            "write_dxf: no polyline geometry to export (all layers empty)");

    std::ofstream f(p, std::ios::binary | std::ios::trunc);
    if (!f) throw std::runtime_error("write_dxf: cannot open output: " + path);
    f.write(text.data(), static_cast<std::streamsize>(text.size()));
    f.close();
    return std::filesystem::absolute(p).string();
}

}  // namespace aicad::doc
