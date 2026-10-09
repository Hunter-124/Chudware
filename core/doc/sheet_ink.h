// Pooled drawing-sheet ink — the v2 draw-primitive contract (see drawing_session /
// annot_draw) bucketed by ISO 128 line class and by semantic layer. export2d pools a
// drawing_get sheet into an InkSheet once; the PDF / SVG / DXF writers and the PNG
// rasterizer (session.cpp drawing_render_png) all render from it.
//
// Coordinates are sheet millimetres, origin bottom-left, +y up. Ink is black.
#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <string>
#include <vector>

namespace chudware::doc {

using InkPoint = std::array<double, 2>;
using InkPolyline = std::vector<InkPoint>;

// ISO 128 line weights + dash patterns (mm). Shared so every back-end agrees.
inline constexpr double kInkThin = 0.25;   // type 01.1 / 02.1 / 04.1
inline constexpr double kInkThick = 0.50;  // type 01.2 / 04.2
inline constexpr double kInkDash[2] = {3.0, 1.5};                // dashed (hidden)
inline constexpr double kInkChain[4] = {6.0, 1.0, 0.5, 1.0};     // long-dash-dot (centre)

enum class InkAlign { left, center, right };
enum class InkVAlign { base, mid, top };

// Thin continuous arc, CCW from a0 to a1 (radians). |a1-a0| >= 2π is a full circle.
struct InkArc {
    InkPoint center{0.0, 0.0};
    double radius = 0.0;
    double a0 = 0.0;
    double a1 = 0.0;
    bool full() const { return std::fabs(a1 - a0) >= 2.0 * 3.14159265358979323846 - 1e-9; }
    // CCW sweep in (0, 2π]: a1 < a0 wraps once around.
    double sweep() const {
        constexpr double tau = 2.0 * 3.14159265358979323846;
        if (full()) return tau;
        double s = a1 - a0;
        while (s <= 0.0) s += tau;
        return s;
    }
};

// One text run. `h` is the cap height (ISO 3098); pos is interpreted via align/valign:
// valign base = baseline, mid = mid cap height, top = top of capitals.
struct InkText {
    InkPoint pos{0.0, 0.0};
    double h = 3.5;
    std::string text;  // UTF-8
    InkAlign align = InkAlign::left;
    InkVAlign valign = InkVAlign::base;
    double baseline() const {
        return valign == InkVAlign::mid ? pos[1] - 0.5 * h
             : valign == InkVAlign::top ? pos[1] - h
                                        : pos[1];
    }
};

// Every line class of one semantic layer.
struct InkLayer {
    std::vector<InkPolyline> thin;
    std::vector<InkPolyline> thick;
    std::vector<InkPolyline> dashed;
    std::vector<InkPolyline> chain;
    std::vector<InkPolyline> chain_thick;
    std::vector<InkArc> arcs;
    std::vector<std::array<InkPoint, 3>> tris;  // filled
    std::vector<InkText> texts;

    bool empty() const {
        return thin.empty() && thick.empty() && dashed.empty() && chain.empty() &&
               chain_thick.empty() && arcs.empty() && tris.empty() && texts.empty();
    }
    void shift(double dx, double dy) {
        auto mv = [&](std::vector<InkPolyline>& v) {
            for (auto& pl : v)
                for (auto& p : pl) { p[0] += dx; p[1] += dy; }
        };
        mv(thin); mv(thick); mv(dashed); mv(chain); mv(chain_thick);
        for (auto& a : arcs) { a.center[0] += dx; a.center[1] += dy; }
        for (auto& t : tris)
            for (auto& p : t) { p[0] += dx; p[1] += dy; }
        for (auto& t : texts) { t.pos[0] += dx; t.pos[1] += dy; }
    }
    void append(const InkLayer& o) {
        auto cat = [](std::vector<InkPolyline>& d, const std::vector<InkPolyline>& s) {
            d.insert(d.end(), s.begin(), s.end());
        };
        cat(thin, o.thin); cat(thick, o.thick); cat(dashed, o.dashed);
        cat(chain, o.chain); cat(chain_thick, o.chain_thick);
        arcs.insert(arcs.end(), o.arcs.begin(), o.arcs.end());
        tris.insert(tris.end(), o.tris.begin(), o.tris.end());
        texts.insert(texts.end(), o.texts.begin(), o.texts.end());
    }
};

// One sheet's ink by semantic layer (the DXF layer names; SVG groups).
//   visible  — view visible edges (thick)       hidden — view hidden edges (dashed)
//   hatch    — section hatch (thin)             annotations — dims/notes/GD&T/view decorations
//   tables   — table overlays                   frame — border, zones, title block, projection
struct InkSheet {
    InkLayer frame;
    InkLayer hatch;
    InkLayer hidden;
    InkLayer visible;
    InkLayer annotations;
    InkLayer tables;
    double page_w = 0.0;
    double page_h = 0.0;
    bool fixed_page = false;  // false → writers size the page to the ink bbox + margin
    int annotation_count = 0;

    template <class F>
    void for_each_layer(F&& f) const {
        f(frame); f(hatch); f(hidden); f(visible); f(annotations); f(tables);
    }
    void shift(double dx, double dy) {
        frame.shift(dx, dy); hatch.shift(dx, dy); hidden.shift(dx, dy);
        visible.shift(dx, dy); annotations.shift(dx, dy); tables.shift(dx, dy);
    }
    bool empty() const {
        bool e = true;
        for_each_layer([&](const InkLayer& l) { e = e && l.empty(); });
        return e;
    }
};

// Axis-aligned ink extents (text approximated by 0.7·h per char). ok=false when empty.
struct InkBounds {
    bool ok = false;
    double minx = 0.0, miny = 0.0, maxx = 0.0, maxy = 0.0;
    void add(double x, double y) {
        if (!ok) { minx = maxx = x; miny = maxy = y; ok = true; return; }
        minx = std::min(minx, x); maxx = std::max(maxx, x);
        miny = std::min(miny, y); maxy = std::max(maxy, y);
    }
};

inline void ink_bounds_add(InkBounds& b, const InkLayer& l) {
    auto pls = [&](const std::vector<InkPolyline>& v) {
        for (const auto& pl : v)
            for (const auto& p : pl) b.add(p[0], p[1]);
    };
    pls(l.thin); pls(l.thick); pls(l.dashed); pls(l.chain); pls(l.chain_thick);
    for (const auto& a : l.arcs) {
        b.add(a.center[0] - a.radius, a.center[1] - a.radius);
        b.add(a.center[0] + a.radius, a.center[1] + a.radius);
    }
    for (const auto& t : l.tris)
        for (const auto& p : t) b.add(p[0], p[1]);
    for (const auto& t : l.texts) {
        double w = 0.0;
        for (unsigned char c : t.text)
            if ((c & 0xC0) != 0x80) w += 0.7 * t.h;
        const double x0 = t.align == InkAlign::center ? t.pos[0] - 0.5 * w
                        : t.align == InkAlign::right  ? t.pos[0] - w
                                                      : t.pos[0];
        const double base = t.baseline();
        b.add(x0, base - 0.3 * t.h);
        b.add(x0 + w, base + t.h);
    }
}

inline InkBounds ink_bounds(const InkSheet& s) {
    InkBounds b;
    s.for_each_layer([&](const InkLayer& l) { ink_bounds_add(b, l); });
    return b;
}

// Decode one UTF-8 code point at `i` (advancing it). Malformed bytes yield U+FFFD.
inline char32_t ink_utf8_next(const std::string& s, std::size_t& i) {
    const unsigned char c = static_cast<unsigned char>(s[i]);
    const int n = c < 0x80 ? 0 : (c >> 5) == 0x6 ? 1 : (c >> 4) == 0xE ? 2
                : (c >> 3) == 0x1E ? 3 : -1;
    if (n == 0) { ++i; return c; }
    if (n < 0 || i + static_cast<std::size_t>(n) >= s.size()) { ++i; return 0xFFFD; }
    char32_t cp = n == 1 ? (c & 0x1F) : n == 2 ? (c & 0x0F) : (c & 0x07);
    for (int k = 1; k <= n; ++k) {
        const unsigned char d = static_cast<unsigned char>(s[i + static_cast<std::size_t>(k)]);
        if ((d & 0xC0) != 0x80) { ++i; return 0xFFFD; }
        cp = (cp << 6) | (d & 0x3F);
    }
    i += static_cast<std::size_t>(n) + 1;
    return cp;
}

// Hole-callout glyphs that no target font carries are spelled as ASCII words
// (⌴ CBORE, ⌵ CSK, ↧ DEPTH). Returns nullptr for any other code point.
inline const char* ink_callout_word(char32_t cp) {
    switch (cp) {
        case 0x2334: return "CBORE";
        case 0x2335: return "CSK";
        case 0x21A7: return "DEPTH";
        default: return nullptr;
    }
}

// UTF-8 → UTF-8 with every ⌴/⌵/↧ replaced by its word, space-separated from neighbours
// ("⌴⌀11 ↧6.4" → "CBORE ⌀11 DEPTH 6.4"). Back-ends lacking those glyphs call this first.
inline std::string ink_expand_callouts(const std::string& in) {
    std::string out;
    out.reserve(in.size() + 8);
    for (std::size_t i = 0; i < in.size();) {
        const std::size_t start = i;
        const char32_t cp = ink_utf8_next(in, i);
        const char* word = ink_callout_word(cp);
        if (!word) { out.append(in, start, i - start); continue; }
        if (!out.empty() && out.back() != ' ') out.push_back(' ');
        out += word;
        if (i < in.size() && in[i] != ' ') out.push_back(' ');
    }
    return out;
}

}  // namespace chudware::doc
