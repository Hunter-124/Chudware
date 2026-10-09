// DXF R12 ASCII writer — see dxf.h.

#include "doc/dxf.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <stdexcept>

#include "doc/sheet_ink.h"  // ink_utf8_next, ink_expand_callouts

namespace chudware::doc {

namespace {

struct Out {
    std::string s;
    void tag(int code, const std::string& v) {
        s += std::to_string(code);
        s += "\r\n";
        s += v;
        s += "\r\n";
    }
    void tag(int code, long long v) { tag(code, std::to_string(v)); }
    void tag(int code, int v) { tag(code, std::to_string(v)); }
    void tag(int code, const char* v) { tag(code, std::string(v)); }
    void real(int code, double v) {
        if (!(v == v)) { tag(code, "0"); return; }
        char buf[64];
        std::snprintf(buf, sizeof(buf), "%.6f", v);
        std::string t(buf);
        while (!t.empty() && t.back() == '0') t.pop_back();
        if (!t.empty() && t.back() == '.') t.pop_back();
        if (t.empty() || t == "-0") t = "0";
        tag(code, t);
    }
    void xy(int code, const std::array<double, 2>& p, bool z = true) {
        real(code, p[0]);
        real(code + 10, p[1]);
        if (z) real(code + 20, 0.0);
    }
};

struct Ext {
    bool ok = false;
    double minx = 0, miny = 0, maxx = 0, maxy = 0;
    void add(double x, double y) {
        if (!ok) { minx = maxx = x; miny = maxy = y; ok = true; return; }
        minx = std::min(minx, x); maxx = std::max(maxx, x);
        miny = std::min(miny, y); maxy = std::max(maxy, y);
    }
};

Ext extents(const DxfDocument& d) {
    Ext e;
    for (const auto& pl : d.polylines)
        for (const auto& p : pl.pts) e.add(p[0], p[1]);
    for (const auto& a : d.arcs) {
        e.add(a.center[0] - a.radius, a.center[1] - a.radius);
        e.add(a.center[0] + a.radius, a.center[1] + a.radius);
    }
    for (const auto& s : d.solids)
        for (const auto& p : s.pts) e.add(p[0], p[1]);
    for (const auto& t : d.texts) e.add(t.pos[0], t.pos[1]);
    if (d.has_limits) {
        e.add(d.limits[0], d.limits[1]);
        e.add(d.limits[2], d.limits[3]);
    }
    return e;
}

void header(Out& o, const DxfDocument& d) {
    const Ext e = extents(d);
    std::array<double, 4> lim = d.has_limits ? d.limits
                                             : std::array<double, 4>{e.minx, e.miny, e.maxx, e.maxy};
    o.tag(0, "SECTION");
    o.tag(2, "HEADER");
    o.tag(9, "$ACADVER");
    o.tag(1, "AC1009");
    o.tag(9, "$DWGCODEPAGE");
    o.tag(3, "ANSI_1252");
    o.tag(9, "$INSBASE");
    o.xy(10, {0.0, 0.0});
    o.tag(9, "$EXTMIN");
    o.xy(10, {e.minx, e.miny});
    o.tag(9, "$EXTMAX");
    o.xy(10, {e.maxx, e.maxy});
    o.tag(9, "$LIMMIN");
    o.xy(10, {lim[0], lim[1]}, false);
    o.tag(9, "$LIMMAX");
    o.xy(10, {lim[2], lim[3]}, false);
    o.tag(9, "$LTSCALE");
    o.real(40, 1.0);
    o.tag(9, "$TEXTSTYLE");
    o.tag(7, "STANDARD");
    o.tag(0, "ENDSEC");
}

void ltype(Out& o, const char* name, const char* desc, const std::vector<double>& pattern) {
    double total = 0.0;
    for (double v : pattern) total += std::fabs(v);
    o.tag(0, "LTYPE");
    o.tag(2, name);
    o.tag(70, 0);
    o.tag(3, desc);
    o.tag(72, 65);
    o.tag(73, static_cast<int>(pattern.size()));
    o.real(40, total);
    for (double v : pattern) o.real(49, v);
}

void tables(Out& o, const DxfDocument& d) {
    o.tag(0, "SECTION");
    o.tag(2, "TABLES");

    o.tag(0, "TABLE");
    o.tag(2, "LTYPE");
    o.tag(70, 3);
    ltype(o, "CONTINUOUS", "Solid line", {});
    ltype(o, "DASHED", "Dashed __ __ __", {kInkDash[0], -kInkDash[1]});
    ltype(o, "CENTER", "Center ____ . ____ . ____",
          {kInkChain[0], -kInkChain[1], kInkChain[2], -kInkChain[3]});
    o.tag(0, "ENDTAB");

    std::vector<DxfLayerDef> layers;
    layers.push_back({"0", 7, "CONTINUOUS"});
    for (const auto& l : d.layers)
        if (l.name != "0") layers.push_back(l);
    o.tag(0, "TABLE");
    o.tag(2, "LAYER");
    o.tag(70, static_cast<int>(layers.size()));
    for (const auto& l : layers) {
        o.tag(0, "LAYER");
        o.tag(2, l.name);
        o.tag(70, 0);
        o.tag(62, l.color);
        o.tag(6, l.ltype);
    }
    o.tag(0, "ENDTAB");

    o.tag(0, "TABLE");
    o.tag(2, "STYLE");
    o.tag(70, 1);
    o.tag(0, "STYLE");
    o.tag(2, "STANDARD");
    o.tag(70, 0);
    o.real(40, 0.0);
    o.real(41, 1.0);
    o.real(50, 0.0);
    o.tag(71, 0);
    o.real(42, 3.5);
    o.tag(3, "isocp.shx");
    o.tag(4, "");
    o.tag(0, "ENDTAB");

    o.tag(0, "ENDSEC");
}

int entities(Out& o, const DxfDocument& d) {
    o.tag(0, "SECTION");
    o.tag(2, "ENTITIES");
    int n = 0;
    for (const auto& pl : d.polylines) {
        if (pl.pts.size() < 2) continue;
        o.tag(0, "POLYLINE");
        o.tag(8, pl.layer);
        if (!pl.ltype.empty()) o.tag(6, pl.ltype);
        o.tag(66, 1);
        o.xy(10, {0.0, 0.0});
        o.tag(70, 0);
        if (pl.width > 0.0) {
            o.real(40, pl.width);
            o.real(41, pl.width);
        }
        for (const auto& p : pl.pts) {
            o.tag(0, "VERTEX");
            o.tag(8, pl.layer);
            o.xy(10, p);
        }
        o.tag(0, "SEQEND");
        o.tag(8, pl.layer);
        ++n;
    }
    for (const auto& a : d.arcs) {
        if (a.radius <= 0.0) continue;
        o.tag(0, a.circle ? "CIRCLE" : "ARC");
        o.tag(8, a.layer);
        if (!a.ltype.empty()) o.tag(6, a.ltype);
        o.xy(10, a.center);
        o.real(40, a.radius);
        if (!a.circle) {
            o.real(50, a.start_deg);
            o.real(51, a.end_deg);
        }
        ++n;
    }
    for (const auto& s : d.solids) {
        o.tag(0, "SOLID");
        o.tag(8, s.layer);
        o.xy(10, s.pts[0]);
        o.xy(11, s.pts[1]);
        o.xy(12, s.pts[2]);
        o.xy(13, s.pts[2]);
        ++n;
    }
    for (const auto& t : d.texts) {
        const std::string str = dxf_text_string(t.text);
        if (str.empty() || t.height <= 0.0) continue;
        o.tag(0, "TEXT");
        o.tag(8, t.layer);
        o.xy(10, t.pos);
        o.real(40, t.height);
        o.tag(1, str);
        o.tag(7, "STANDARD");
        if (t.halign != 0) o.tag(72, t.halign);
        if (t.halign != 0 || t.valign != 0) o.xy(11, t.pos);
        if (t.valign != 0) o.tag(73, t.valign);
        ++n;
    }
    o.tag(0, "ENDSEC");
    return n;
}

}  // namespace

std::string dxf_text_string(const std::string& utf8) {
    const std::string in = ink_expand_callouts(utf8);
    std::string out;
    out.reserve(in.size() + 4);
    for (std::size_t i = 0; i < in.size();) {
        const char32_t cp = ink_utf8_next(in, i);
        switch (cp) {
            case '%': out += "%%%"; continue;
            case 0x00B0: out += "%%d"; continue;
            case 0x00B1: out += "%%p"; continue;
            case 0x00D8: case 0x00F8: case 0x2300: out += "%%c"; continue;
            case 0x00D7: out += "X"; continue;
            case 0x2212: case 0x2013: case 0x2014: out += "-"; continue;
            case '\t': out += " "; continue;
            default: break;
        }
        if (cp >= 0x20 && cp <= 0x7E) out.push_back(static_cast<char>(cp));
        else if (cp >= 0xA0 && cp <= 0xFF) out.push_back(static_cast<char>(cp));  // cp1252
        else if (cp >= 0x20) out.push_back('?');
    }
    return out;
}

std::string build_dxf(const DxfDocument& doc, int* entity_count) {
    Out o;
    o.s.reserve(1 << 16);
    header(o, doc);
    tables(o, doc);
    const int n = entities(o, doc);
    o.tag(0, "EOF");
    if (entity_count) *entity_count = n;
    return std::move(o.s);
}

std::string write_dxf(const std::string& path, const DxfDocument& doc) {
    int n = 0;
    const std::string text = build_dxf(doc, &n);
    if (n == 0)
        throw std::runtime_error("write_dxf: no geometry to export (all layers empty)");
    std::filesystem::path p(path);
    if (p.has_parent_path()) std::filesystem::create_directories(p.parent_path());
    std::ofstream f(p, std::ios::binary | std::ios::trunc);
    if (!f) throw std::runtime_error("write_dxf: cannot open output: " + path);
    f.write(text.data(), static_cast<std::streamsize>(text.size()));
    f.close();
    if (!f) throw std::runtime_error("write_dxf: failed writing output: " + path);
    return std::filesystem::absolute(p).string();
}

}  // namespace chudware::doc
