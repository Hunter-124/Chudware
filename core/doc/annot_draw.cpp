// Shared annotation -> draw-primitives compiler — see annot_draw.h.

#include "doc/annot_draw.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

using nlohmann::json;

namespace chudware::doc {

namespace {

#ifndef CHUDWARE_PI
#define CHUDWARE_PI 3.14159265358979323846
#endif

// Paper-space sizing constants (mm) — annotations scale with the sheet like real CAD.
// ISO 129-1 / ISO 3098: 3.5 mm lettering, closed filled arrowheads ~3 mm long with a 1:3
// width ratio, a small visible gap between the object and an extension line, and an
// extension-line overshoot of ~2 mm past the dimension line.
constexpr double TEXT_H = 3.5;     // nominal text (cap) height
constexpr double ARROW_LEN = 3.0;  // arrowhead length
constexpr double ARROW_W = 0.55;   // arrowhead half-width
constexpr double EXT_GAP = 1.0;    // gap between geometry and start of an extension line
constexpr double EXT_OVER = 2.0;   // extension-line overshoot past the dimension line
constexpr double TEXT_GAP = 1.0;   // clearance between a dimension line and its value
constexpr double SHOULDER = 4.0;   // horizontal leader landing before a callout value
constexpr double CAP_EM = 0.718;   // Helvetica cap height as a fraction of the em

using P2 = std::array<double, 2>;

// ----- primitive accumulators ------------------------------------------------
json pts_json(const std::vector<P2>& pts) {
    json pl = json::array();
    for (auto& p : pts) pl.push_back(json::array({p[0], p[1]}));
    return pl;
}

struct Draw {
    json segments = json::array();
    json thick = json::array();
    json dashed = json::array();
    json chain = json::array();
    json chain_thick = json::array();
    json arcs = json::array();
    json tris = json::array();
    json texts = json::array();
    json pick;

    void seg(P2 a, P2 b) { segments.push_back(pts_json({a, b})); }
    void thick_seg(P2 a, P2 b) { thick.push_back(pts_json({a, b})); }
    void poly(const std::vector<P2>& pts, bool dash = false) {
        (dash ? dashed : segments).push_back(pts_json(pts));
    }
    void thick_poly(const std::vector<P2>& pts) { thick.push_back(pts_json(pts)); }
    void chain_poly(const std::vector<P2>& pts) { chain.push_back(pts_json(pts)); }
    void dash_seg(P2 a, P2 b) { dashed.push_back(pts_json({a, b})); }
    void chain_seg(P2 a, P2 b) { chain.push_back(pts_json({a, b})); }
    void rect(double x0, double y0, double x1, double y1, bool heavy = false) {
        std::vector<P2> r = {{x0, y0}, {x1, y0}, {x1, y1}, {x0, y1}, {x0, y0}};
        (heavy ? thick : segments).push_back(pts_json(r));
    }
    void arc(P2 c, double r, double a0, double a1) {
        arcs.push_back(json{{"center", json::array({c[0], c[1]})}, {"radius", r},
                            {"a0", a0}, {"a1", a1}});
    }
    void circle(P2 c, double r) { arc(c, r, 0.0, 2.0 * CHUDWARE_PI); }
    void tri(P2 a, P2 b, P2 c) { tris.push_back(pts_json({a, b, c})); }
    void text(P2 pos, const std::string& t, double h = TEXT_H, const char* align = "left",
              const char* valign = "base") {
        if (t.empty()) return;
        texts.push_back(json{{"pos", json::array({pos[0], pos[1]})}, {"h", h}, {"text", t},
                             {"align", align}, {"valign", valign}});
    }
    json finish() {
        return json{{"segments", segments}, {"thick", thick},   {"dashed", dashed},
                    {"chain", chain},       {"chain_thick", chain_thick},
                    {"arcs", arcs},         {"tris", tris},     {"texts", texts},
                    {"pick", pick.is_null() ? json(nullptr) : pick}};
    }
};

// ----- small vector helpers --------------------------------------------------
bool rd2(const json& ann, const char* k, P2& out) {
    auto it = ann.find(k);
    if (it == ann.end() || !it->is_array() || it->size() != 2 || !(*it)[0].is_number() ||
        !(*it)[1].is_number())
        return false;
    out = {(*it)[0].get<double>(), (*it)[1].get<double>()};
    return true;
}
P2 sub(P2 a, P2 b) { return {a[0] - b[0], a[1] - b[1]}; }
P2 add(P2 a, P2 b) { return {a[0] + b[0], a[1] + b[1]}; }
P2 mul(P2 a, double s) { return {a[0] * s, a[1] * s}; }
double len(P2 a) { return std::hypot(a[0], a[1]); }
P2 unit(P2 a) {
    double l = len(a);
    return l < 1e-9 ? P2{0, 0} : P2{a[0] / l, a[1] / l};
}
P2 perp(P2 a) { return {-a[1], a[0]}; }
double dot(P2 a, P2 b) { return a[0] * b[0] + a[1] * b[1]; }
double wrap_tau(double a) {
    constexpr double tau = 2.0 * CHUDWARE_PI;
    while (a < 0.0) a += tau;
    while (a >= tau) a -= tau;
    return a;
}

bool rd_line2(const json& ann, const char* key, P2& a, P2& b) {
    auto it = ann.find(key);
    if (it == ann.end() || !it->is_array() || it->size() != 2) return false;
    json pair = json{{"a", (*it)[0]}, {"b", (*it)[1]}};
    return rd2(pair, "a", a) && rd2(pair, "b", b);
}

std::string scalar_text(const json& value) {
    if (value.is_string()) return value.get<std::string>();
    if (!value.is_number()) return {};
    char buf[48];
    std::snprintf(buf, sizeof(buf), "%.6g", value.get<double>());
    return buf;
}

std::string field_text(const json& obj, const char* key) {
    auto it = obj.find(key);
    return it == obj.end() ? std::string() : scalar_text(*it);
}

// ----- text metrics + stroked dimension symbols ------------------------------
// Helvetica advance widths (1/1000 em) for ASCII 32..126 — the PDF base font, so the layout
// estimate matches the vector export; other back-ends use a similar plain sans.
constexpr int kHelvAdv[95] = {
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,  // ' '../
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,  // 0..?
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, // @..O
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,  // P.._
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,  // `..o
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584};      // p..~

// Stroked symbol glyphs inside dimension values (never left to a font).
enum Glyph { G_NONE = 0, G_DIA, G_CBORE, G_CSK, G_DEPTH };

// Decode one UTF-8 code point at s[i]; advances i.
unsigned decode_cp(const std::string& s, size_t& i) {
    unsigned char c = (unsigned char)s[i];
    if (c < 0x80) { ++i; return c; }
    int n = (c >> 5) == 0x6 ? 2 : (c >> 4) == 0xE ? 3 : (c >> 3) == 0x1E ? 4 : 1;
    unsigned cp = n == 2 ? (c & 0x1F) : n == 3 ? (c & 0x0F) : n == 4 ? (c & 0x07) : c;
    for (int k = 1; k < n && i + k < s.size(); ++k)
        cp = (cp << 6) | ((unsigned char)s[i + k] & 0x3F);
    i += n;
    return cp;
}

Glyph glyph_of(unsigned cp) {
    switch (cp) {
        case 0x2300: return G_DIA;    // ⌀
        case 0x2334: return G_CBORE;  // ⌴
        case 0x2335: return G_CSK;    // ⌵
        case 0x21A7: return G_DEPTH;  // ↧
        default: return G_NONE;
    }
}

double cp_em(unsigned cp) {
    if (cp >= 32 && cp <= 126) return kHelvAdv[cp - 32] / 1000.0;
    switch (cp) {
        case 0xB0: return 0.400;  // °
        case 0xB1: case 0xD7: return 0.584;  // ± ×
        case 0xD8: return 0.778;  // Ø
        default: return 0.556;
    }
}

double glyph_adv(Glyph g, double h) {
    switch (g) {
        case G_DIA: return h * 0.95;
        case G_DEPTH: return h * 0.85;
        default: return h * 1.05;
    }
}

// Plain run width (no stroked glyphs) at cap height h.
double plain_width(const std::string& t, double h) {
    double em = 0.0;
    for (size_t i = 0; i < t.size();) em += cp_em(decode_cp(t, i));
    return em * h / CAP_EM;
}

struct Run {
    Glyph glyph = G_NONE;
    std::string text;
};

std::vector<Run> split_runs(const std::string& t) {
    std::vector<Run> runs;
    for (size_t i = 0; i < t.size();) {
        size_t start = i;
        Glyph g = glyph_of(decode_cp(t, i));
        if (g != G_NONE) {
            runs.push_back(Run{g, {}});
        } else {
            if (runs.empty() || runs.back().glyph != G_NONE) runs.push_back(Run{});
            runs.back().text.append(t, start, i - start);
        }
    }
    return runs;
}

double text_width(const std::string& t, double h) {
    double w = 0.0;
    for (const Run& r : split_runs(t))
        w += r.glyph != G_NONE ? glyph_adv(r.glyph, h) : plain_width(r.text, h);
    return w;
}

// Draw one stroked symbol with its cell's baseline-left corner at `o`, cap height h.
void draw_glyph(Draw& d, Glyph g, P2 o, double h) {
    auto P = [&](double x, double y) { return P2{o[0] + x * h, o[1] + y * h}; };
    switch (g) {
        case G_DIA: {
            double r = 0.36 * h;
            P2 c = P(0.46, 0.5);
            d.circle(c, r);
            double k = r * 1.3 * 0.7071;
            d.seg({c[0] - k, c[1] - k}, {c[0] + k, c[1] + k});
            break;
        }
        case G_CBORE: d.poly({P(0.12, 0.95), P(0.12, 0.05), P(0.88, 0.05), P(0.88, 0.95)}); break;
        case G_CSK: d.poly({P(0.06, 0.95), P(0.5, 0.05), P(0.94, 0.95)}); break;
        case G_DEPTH:
            d.seg(P(0.12, 1.0), P(0.72, 1.0));
            d.seg(P(0.42, 1.0), P(0.42, 0.02));
            d.poly({P(0.17, 0.32), P(0.42, 0.02), P(0.67, 0.32)});
            break;
        default: break;
    }
}

// Place `t` like Draw::text, stroking the dimension symbols in-line. Plain strings pass
// straight through as one text run.
void rich_text(Draw& d, P2 pos, const std::string& t, double h, const char* align,
               const char* valign) {
    if (t.empty()) return;
    std::vector<Run> runs = split_runs(t);
    bool any_glyph = false;
    for (const Run& r : runs) any_glyph |= r.glyph != G_NONE;
    if (!any_glyph) { d.text(pos, t, h, align, valign); return; }
    const std::string a = align, v = valign;
    const double w = text_width(t, h);
    double x = pos[0] - (a == "center" ? w * 0.5 : a == "right" ? w : 0.0);
    const double base = pos[1] - (v == "mid" ? h * 0.5 : v == "top" ? h : 0.0);
    for (const Run& r : runs) {
        if (r.glyph != G_NONE) {
            draw_glyph(d, r.glyph, {x, base}, h);
            x += glyph_adv(r.glyph, h);
        } else {
            d.text({x, base}, r.text, h, "left", "base");
            x += plain_width(r.text, h);
        }
    }
}

// A filled arrowhead: tip at `tip`, body extending toward `toward`.
void arrowhead(Draw& d, P2 tip, P2 toward, double length = ARROW_LEN,
               double half_w = ARROW_W) {
    P2 u = unit(sub(toward, tip));
    if (len(u) < 1e-9) return;
    P2 n = perp(u);
    P2 base = add(tip, mul(u, length));
    d.tri(tip, add(base, mul(n, half_w)), sub(base, mul(n, half_w)));
}

// "[x]" basic-dimension text -> x plus a frame drawn around it.
bool strip_basic(std::string& t) {
    if (t.size() >= 2 && t.front() == '[' && t.back() == ']') {
        t = t.substr(1, t.size() - 2);
        return true;
    }
    return false;
}

void text_box(Draw& d, double cx, double cy, double w, double h) {
    d.rect(cx - w * 0.5 - 0.9, cy - h * 0.5 - 0.9, cx + w * 0.5 + 0.9, cy + h * 0.5 + 0.9);
}

// ----- ASME Y14.5 geometric-characteristic symbol stroke library -------------
// Each draws into the square cell with bottom-left (x,y) and side `s`.
void geom_symbol(Draw& d, const std::string& sym, double x, double y, double s) {
    auto L = [&](double ax, double ay, double bx, double by) {
        d.seg({x + ax * s, y + ay * s}, {x + bx * s, y + by * s});
    };
    auto C = [&](double cx, double cy, double r) { d.circle({x + cx * s, y + cy * s}, r * s); };
    auto A = [&](double cx, double cy, double r, double a0, double a1) {
        d.arc({x + cx * s, y + cy * s}, r * s, a0, a1);
    };
    if (sym == "straightness") {
        L(0.08, 0.5, 0.92, 0.5);
    } else if (sym == "flatness") {
        d.poly({{x + 0.30 * s, y + 0.22 * s}, {x + 0.92 * s, y + 0.22 * s},
                {x + 0.70 * s, y + 0.78 * s}, {x + 0.08 * s, y + 0.78 * s},
                {x + 0.30 * s, y + 0.22 * s}});
    } else if (sym == "circularity") {
        C(0.5, 0.5, 0.40);
    } else if (sym == "cylindricity") {
        C(0.5, 0.5, 0.28);
        L(0.06, 0.16, 0.34, 0.84);
        L(0.66, 0.16, 0.94, 0.84);
    } else if (sym == "profile_line") {
        A(0.5, 0.32, 0.42, 0.0, CHUDWARE_PI);  // upper half arc, open at bottom
    } else if (sym == "profile_surface") {
        A(0.5, 0.32, 0.42, 0.0, CHUDWARE_PI);
        L(0.08, 0.32, 0.92, 0.32);  // closed baseline
    } else if (sym == "perpendicularity") {
        L(0.5, 0.12, 0.5, 0.92);
        L(0.18, 0.12, 0.82, 0.12);
    } else if (sym == "angularity") {
        L(0.12, 0.14, 0.88, 0.14);
        L(0.12, 0.14, 0.82, 0.86);
    } else if (sym == "parallelism") {
        L(0.22, 0.12, 0.52, 0.88);
        L(0.50, 0.12, 0.80, 0.88);
    } else if (sym == "position") {
        C(0.5, 0.5, 0.30);
        L(0.5, 0.08, 0.5, 0.92);
        L(0.08, 0.5, 0.92, 0.5);
    } else if (sym == "concentricity") {
        C(0.5, 0.5, 0.42);
        C(0.5, 0.5, 0.20);
    } else if (sym == "symmetry") {
        L(0.5, 0.10, 0.5, 0.90);
        L(0.24, 0.66, 0.76, 0.66);
        L(0.24, 0.34, 0.76, 0.34);
    } else if (sym == "circular_runout" || sym == "total_runout") {
        int n = (sym == "total_runout") ? 2 : 1;
        for (int i = 0; i < n; ++i) {
            double off = i * 0.30;
            // slanted shaft with two barbs at the top (an arrow ↗)
            L(0.18 + off, 0.18, 0.62 + off, 0.82);
            L(0.62 + off, 0.82, 0.40 + off, 0.80);
            L(0.62 + off, 0.82, 0.58 + off, 0.58);
        }
    } else {
        // Unknown — leave the cell blank rather than throwing.
    }
}

// A material-condition modifier: a circled letter (M=MMC, L=LMC, P=projected, F=free).
// Returns the advance width used. The letter is a text run; the circle is a stroke.
double modifier_symbol(Draw& d, const std::string& mod, double x, double y, double h) {
    std::string letter;
    if (mod == "mmc") letter = "M";
    else if (mod == "lmc") letter = "L";
    else if (mod == "projected") letter = "P";
    else if (mod == "free") letter = "F";
    else if (mod == "tangent") letter = "T";
    else return 0.0;
    double r = h * 0.5;
    P2 c = {x + r, y + h * 0.5};
    d.circle(c, r);
    d.text({c[0], c[1] - h * 0.32}, letter, h * 0.62, "center", "base");
    return 2.0 * r + h * 0.15;
}

// ----- dimension kinds ---------------------------------------------------------
// Linear / aligned dimension (ISO 129-1 / ASME Y14.5): extension lines start a visible gap off
// the feature and overshoot the dimension line; filled arrowheads sit inside the extension
// lines, flipping outside (with a short tail) when the span is too tight; the value reads
// horizontally (unidirectional) — above a horizontal dimension line, beside a vertical one.
// A `linear_dim` measures along the dominant sheet axis of a->b (horizontal or vertical);
// an `aligned_dim` measures along a->b itself.
void compile_linear(Draw& d, const json& ann, P2 a, P2 b, P2 p, const std::string& txt_in) {
    const std::string type = ann.value("type", std::string());
    P2 u;
    if (type == "linear_dim") {
        const P2 ab = sub(b, a);
        u = std::fabs(ab[0]) >= std::fabs(ab[1]) ? P2{1.0, 0.0} : P2{0.0, 1.0};
    } else {
        u = unit(sub(b, a));
    }
    if (len(u) < 1e-9) return;
    const P2 nrm = perp(u);
    // Dimension line: the line through p along u. Feet of a and b on it.
    const double off_a = dot(sub(p, a), nrm), off_b = dot(sub(p, b), nrm);
    const P2 A2 = add(a, mul(nrm, off_a)), B2 = add(b, mul(nrm, off_b));
    const double span = len(sub(B2, A2));
    if (span < 1e-6) return;
    auto ext = [&](P2 from, P2 to) {
        const P2 e = sub(to, from);
        const double l = len(e);
        if (l < EXT_GAP + 1e-6) return;
        const P2 eu = mul(e, 1.0 / l);
        d.seg(add(from, mul(eu, EXT_GAP)), add(to, mul(eu, EXT_OVER)));
    };
    ext(a, A2);
    ext(b, B2);
    const P2 du = unit(sub(B2, A2));
    if (span >= 2.0 * ARROW_LEN + 2.0) {
        d.seg(A2, B2);
        arrowhead(d, A2, B2);
        arrowhead(d, B2, A2);
    } else {
        // Tight span: arrows outside pointing in, dimension line carried through.
        const double tail = ARROW_LEN + 3.0;
        d.seg(sub(A2, mul(du, tail)), add(B2, mul(du, tail)));
        arrowhead(d, A2, sub(A2, du));
        arrowhead(d, B2, add(B2, du));
    }
    std::string txt = txt_in;
    const bool basic = strip_basic(txt);
    const double tw = text_width(txt, TEXT_H);
    const P2 mid = mul(add(A2, B2), 0.5);
    const bool horizontal_line = std::fabs(du[1]) < 0.2;
    const bool vertical_line = std::fabs(du[0]) < 0.2;
    P2 tp;
    double box_cx, box_cy;
    if (horizontal_line) {
        tp = {mid[0], mid[1] + TEXT_GAP};
        rich_text(d, tp, txt, TEXT_H, "center", "base");
        box_cx = mid[0];
        box_cy = tp[1] + TEXT_H * 0.5;
    } else if (vertical_line) {
        // The value sits on the side of the dimension line facing away from the feature.
        const double side = (dot(sub(p, a), nrm) * nrm[0]) < 0.0 ? -1.0 : 1.0;
        tp = {mid[0] + side * TEXT_GAP * 1.5, mid[1]};
        rich_text(d, tp, txt, TEXT_H, side < 0.0 ? "right" : "left", "mid");
        box_cx = tp[0] + side * tw * 0.5;
        box_cy = mid[1];
    } else {
        P2 away = nrm;
        if (dot(sub(p, a), away) < 0.0) away = mul(away, -1.0);
        tp = add(mid, mul(away, TEXT_GAP + TEXT_H * 0.5 + 0.5));
        rich_text(d, tp, txt, TEXT_H, "center", "mid");
        box_cx = tp[0];
        box_cy = tp[1];
    }
    if (basic) text_box(d, box_cx, box_cy, tw, TEXT_H);
    d.pick = json::array({p[0], p[1]});
}

// Diameter / radius / hole callout leader (ISO 129-1 / ASME Y14.5): the arrow touches the
// circle on the line to its centre, the leader runs out to the elbow `p`, then a short
// horizontal shoulder carries the value. Without a known circle the leader ends on a dot.
void compile_leader_dim(Draw& d, const json& ann, P2 c, P2 p, std::string txt) {
    const double r = ann.value("radius", 0.0);
    const P2 out = sub(p, c);
    const double out_len = len(out);
    const bool right = p[0] >= c[0];
    const double sx = right ? 1.0 : -1.0;
    const P2 knee = p;
    const P2 landing = {knee[0] + sx * SHOULDER, knee[1]};
    if (r > 1e-6 && out_len > r + 1e-6) {
        const P2 dir = mul(out, 1.0 / out_len);
        const P2 tip = add(c, mul(dir, r));
        d.poly({tip, knee, landing});
        arrowhead(d, tip, knee);
    } else if (r > 1e-6) {
        // Elbow inside the circle: arrow from the centre side onto the rim.
        const P2 dir = out_len > 1e-9 ? mul(out, 1.0 / out_len) : P2{0.7071, 0.7071};
        const P2 tip = add(c, mul(dir, r));
        d.poly({c, tip});
        d.poly({tip, add(tip, mul(dir, ARROW_LEN + 4.0)),
                {tip[0] + dir[0] * (ARROW_LEN + 4.0) + sx * SHOULDER,
                 tip[1] + dir[1] * (ARROW_LEN + 4.0)}});
        arrowhead(d, tip, c);
    } else {
        d.poly({c, knee, landing});
        d.circle(c, 0.5);
    }
    const P2 tp = {landing[0] + sx * 1.0, landing[1]};
    rich_text(d, tp, txt, TEXT_H, right ? "left" : "right", "mid");
    d.pick = json::array({p[0], p[1]});
}

void compile_angular(Draw& d, const json& ann, P2 a, P2 b, P2 pos,
                     const std::string& txt) {
    P2 c{}, e{}, vertex{};
    if (!rd_line2(ann, "line2", c, e)) return;
    if (!rd2(ann, "vertex", vertex)) {
        P2 r = sub(b, a), s = sub(e, c);
        double den = r[0] * s[1] - r[1] * s[0];
        if (std::fabs(den) < 1e-9) return;
        P2 ca = sub(c, a);
        double t = (ca[0] * s[1] - ca[1] * s[0]) / den;
        vertex = add(a, mul(r, t));
    }

    P2 label_ray = unit(sub(pos, vertex));
    P2 u0 = unit(sub(b, a)), u1 = unit(sub(e, c));
    if (len(label_ray) < 1e-9 || len(u0) < 1e-9 || len(u1) < 1e-9) return;
    // Each picked line is infinite. Select the two ray directions facing the label so the
    // dimension occupies the intended quadrant rather than an arbitrary supplementary angle.
    if (dot(u0, label_ray) < 0.0) u0 = mul(u0, -1.0);
    if (dot(u1, label_ray) < 0.0) u1 = mul(u1, -1.0);

    double a0 = std::atan2(u0[1], u0[0]);
    double a1 = std::atan2(u1[1], u1[0]);
    double ap = std::atan2(label_ray[1], label_ray[0]);
    double ccw = wrap_tau(a1 - a0);
    double sweep = wrap_tau(ap - a0) <= ccw + 1e-9 ? ccw : ccw - 2.0 * CHUDWARE_PI;
    if (std::fabs(sweep) < 1e-6) return;

    double radius = std::max(len(sub(pos, vertex)), ARROW_LEN * 3.0);
    d.seg(add(vertex, mul(u0, EXT_GAP)), add(vertex, mul(u0, radius + EXT_OVER)));
    d.seg(add(vertex, mul(u1, EXT_GAP)), add(vertex, mul(u1, radius + EXT_OVER)));
    d.arc(vertex, radius, std::min(a0, a0 + sweep), std::max(a0, a0 + sweep));

    P2 arc0 = add(vertex, mul(u0, radius));
    P2 arc1 = add(vertex, mul(u1, radius));
    double inward_step = (sweep > 0 ? 1.0 : -1.0) * std::min(std::fabs(sweep) * 0.3,
                                                             ARROW_LEN / radius);
    P2 in0 = add(vertex, P2{radius * std::cos(a0 + inward_step),
                            radius * std::sin(a0 + inward_step)});
    P2 in1 = add(vertex, P2{radius * std::cos(a0 + sweep - inward_step),
                            radius * std::sin(a0 + sweep - inward_step)});
    arrowhead(d, arc0, in0);
    arrowhead(d, arc1, in1);
    const double amid = a0 + sweep * 0.5;
    const P2 tp = add(vertex, P2{(radius + TEXT_GAP + TEXT_H * 0.6) * std::cos(amid),
                                 (radius + TEXT_GAP + TEXT_H * 0.6) * std::sin(amid)});
    rich_text(d, tp, txt.empty() ? "?" : txt, TEXT_H, "center", "mid");
    d.pick = json::array({pos[0], pos[1]});
}

// Leadered note: arrow on the feature, leader to an elbow, a horizontal landing under the text.
void leadered_text(Draw& d, P2 anchor, bool has_anchor, P2 pos, const std::string& label,
                   const json* elbow_hint = nullptr) {
    const double tw = text_width(label, TEXT_H);
    if (has_anchor) {
        const bool right = pos[0] + tw * 0.5 >= anchor[0];
        P2 elbow = right ? P2{pos[0] - 1.0, pos[1] - 1.2} : P2{pos[0] + tw + 1.0, pos[1] - 1.2};
        if (elbow_hint) {
            P2 hint{};
            json holder = json{{"b", *elbow_hint}};
            if (rd2(holder, "b", hint) && len(sub(hint, anchor)) > 1e-6) elbow = hint;
        }
        const P2 landing_end = right ? P2{pos[0] + tw, elbow[1]} : P2{pos[0], elbow[1]};
        d.poly({anchor, elbow, landing_end});
        arrowhead(d, anchor, elbow);
    }
    // Free text keeps its characters: each back-end maps symbols it lacks (DXF/PDF words).
    d.text(pos, label, TEXT_H, "left", "base");
    d.pick = json::array({pos[0], pos[1]});
}

void compile_note(Draw& d, const json& ann, P2 anchor, bool has_anchor, P2 pos,
                  const std::string& txt) {
    std::string label = txt.empty() ? "note" : txt;
    // A note whose anchor IS its text position is free text (no leader).
    if (has_anchor && len(sub(anchor, pos)) < 1e-6) has_anchor = false;
    const json* hint = ann.contains("b") && len(sub(anchor, pos)) > 1e-6 ? &ann["b"] : nullptr;
    // Multi-line notes: split on '\n', stacking lines at 1.6 h pitch.
    if (label.find('\n') != std::string::npos) {
        std::vector<std::string> lines;
        size_t start = 0;
        for (size_t i = 0; i <= label.size(); ++i)
            if (i == label.size() || label[i] == '\n') {
                lines.push_back(label.substr(start, i - start));
                start = i + 1;
            }
        leadered_text(d, anchor, has_anchor, pos, lines.front(), hint);
        for (size_t i = 1; i < lines.size(); ++i)
            d.text({pos[0], pos[1] - TEXT_H * 1.6 * (double)i}, lines[i], TEXT_H, "left",
                   "base");
        return;
    }
    leadered_text(d, anchor, has_anchor, pos, label, hint);
}

void compile_chamfer(Draw& d, const json& ann) {
    P2 anchor{}, pos{};
    if (!rd2(ann, "a", anchor) || !rd2(ann, "pos", pos)) return;
    std::string label = ann.value("text", std::string());
    if (label.empty()) {
        std::string distance = field_text(ann, "length");
        if (distance.empty()) distance = field_text(ann, "distance");
        std::string angle = field_text(ann, "angle");
        if (angle.empty()) angle = field_text(ann, "angle_deg");
        if (angle.empty()) angle = "45";
        int count = ann.value("count", 1);
        if (count > 1) label = std::to_string(count) + "X ";
        label += (distance.empty() ? "?" : distance) + " X " + angle + "\xC2\xB0";
    }
    leadered_text(d, anchor, true, pos, label);
}

void compile_ordinate(Draw& d, const json& ann) {
    P2 origin{}, pos{};
    if (!rd2(ann, "origin", origin)) return;
    bool has_pos = rd2(ann, "pos", pos);
    std::string axis = ann.value("axis", std::string("x"));
    if (axis != "x" && axis != "y") axis = "x";

    std::vector<P2> points;
    if (ann.contains("points") && ann["points"].is_array()) {
        for (const json& point : ann["points"]) {
            json holder = json{{"point", point}};
            P2 p{};
            if (rd2(holder, "point", p)) points.push_back(p);
        }
    } else {
        P2 target{};
        if (rd2(ann, "a", target)) points.push_back(target);
    }
    if (points.empty()) return;

    // Datum-origin mark: a small target circle and cross, shared by the ordinate set.
    d.circle(origin, 1.2);
    d.seg({origin[0] - 1.8, origin[1]}, {origin[0] + 1.8, origin[1]});
    d.seg({origin[0], origin[1] - 1.8}, {origin[0], origin[1] + 1.8});

    double baseline = axis == "x"
                          ? (has_pos ? pos[1] : origin[1] + 12.0)
                          : (has_pos ? pos[0] : origin[0] + 12.0);
    for (size_t i = 0; i < points.size(); ++i) {
        P2 target = points[i];
        std::string value;
        if (ann.contains("values") && ann["values"].is_array() && i < ann["values"].size())
            value = scalar_text(ann["values"][i]);
        else if (points.size() == 1 && ann.contains("value"))
            value = scalar_text(ann["value"]);
        if (value.empty()) {
            double delta = axis == "x" ? target[0] - origin[0] : target[1] - origin[1];
            value = scalar_text(json(delta));
        }
        if (points.size() == 1 && ann.contains("text") && ann["text"].is_string() &&
            !ann["text"].get<std::string>().empty())
            value = ann["text"].get<std::string>();

        if (axis == "x") {
            P2 end = {target[0], baseline};
            d.poly({target, end, {end[0] + 1.5, end[1]}});
            double side = baseline >= target[1] ? 1.0 : -1.0;
            d.text({end[0], end[1] + side * 0.8}, value, TEXT_H, "center",
                   side > 0.0 ? "base" : "top");
        } else {
            P2 end = {baseline, target[1]};
            d.poly({target, end, {end[0], end[1] + 1.5}});
            double side = baseline >= target[0] ? 1.0 : -1.0;
            d.text({end[0] + side * 0.8, end[1]}, value, TEXT_H,
                   side > 0.0 ? "left" : "right", "mid");
        }
    }
    d.pick = json::array({has_pos ? pos[0] : origin[0], has_pos ? pos[1] : origin[1]});
}

// ----- GD&T: feature control frame -------------------------------------------
void compile_gdt_frame(Draw& d, const json& ann) {
    P2 anchor{}, pos{};
    bool ha = rd2(ann, "a", anchor);
    if (!rd2(ann, "pos", pos)) {
        if (ha) pos = add(anchor, P2{14.0, 10.0});
        else return;
    }
    const double h = TEXT_H;
    const double H = h * 1.7;           // cell height
    const double pad = h * 0.45;
    std::string sym = ann.value("symbol", std::string("position"));
    bool dia = ann.value("tol_diameter", false);
    std::string tol;
    if (ann.contains("tol_value")) {
        if (ann["tol_value"].is_string()) tol = ann["tol_value"].get<std::string>();
        else if (ann["tol_value"].is_number()) {
            char buf[48];
            std::snprintf(buf, sizeof(buf), "%.4g", ann["tol_value"].get<double>());
            tol = buf;
        }
    }
    std::string tmod = ann.value("tol_modifier", std::string("none"));

    struct Cell { double w; };
    // Cell widths.
    double symW = H;
    double tolW = pad * 2 + (dia ? glyph_adv(G_DIA, h) : 0) + text_width(tol.empty() ? "0" : tol, h) +
                  (tmod != "none" && !tmod.empty() ? H * 0.7 : 0);
    std::vector<std::string> datRefs, datMods;
    if (ann.contains("datums") && ann["datums"].is_array())
        for (const auto& dd : ann["datums"]) {
            if (!dd.is_object()) continue;
            std::string ref = dd.value("ref", std::string());
            if (ref.empty()) continue;
            datRefs.push_back(ref);
            datMods.push_back(dd.value("modifier", std::string("none")));
        }
    std::vector<double> datW;
    for (size_t i = 0; i < datRefs.size(); ++i)
        datW.push_back(pad * 2 + text_width(datRefs[i], h) +
                       (datMods[i] != "none" && !datMods[i].empty() ? H * 0.7 : 0));

    double totalW = symW + tolW;
    for (double w : datW) totalW += w;
    // pos = bottom-left of the frame.
    double x0 = pos[0], y0 = pos[1];
    // Outer rectangle + vertical separators.
    d.poly({{x0, y0}, {x0 + totalW, y0}, {x0 + totalW, y0 + H}, {x0, y0 + H}, {x0, y0}});
    double cx = x0;
    auto vline = [&](double xx) { d.seg({xx, y0}, {xx, y0 + H}); };
    // symbol cell
    geom_symbol(d, sym, cx + (symW - H) * 0.5 + H * 0.15, y0 + H * 0.15, H * 0.7);
    cx += symW;
    vline(cx);
    // tolerance cell
    double tx = cx + pad;
    if (dia) {
        draw_glyph(d, G_DIA, {tx, y0 + (H - h) * 0.5}, h);
        tx += glyph_adv(G_DIA, h);
    }
    d.text({tx, y0 + H * 0.5 - h * 0.5}, tol, h, "left", "base");
    tx += text_width(tol, h) + h * 0.25;
    if (tmod != "none" && !tmod.empty()) modifier_symbol(d, tmod, tx, y0 + (H - h) * 0.5, h);
    cx += tolW;
    // datum cells
    for (size_t i = 0; i < datRefs.size(); ++i) {
        vline(cx);
        double dx = cx + pad;
        d.text({dx, y0 + H * 0.5 - h * 0.5}, datRefs[i], h, "left", "base");
        dx += text_width(datRefs[i], h) + h * 0.2;
        if (datMods[i] != "none" && !datMods[i].empty())
            modifier_symbol(d, datMods[i], dx, y0 + (H - h) * 0.5, h);
        cx += datW[i];
    }
    // Leader from the anchor feature to the nearest frame corner.
    if (ha) {
        P2 target = {x0, y0 + H * 0.5};
        d.seg(anchor, target);
        arrowhead(d, anchor, target);
    }
    d.pick = json::array({pos[0], pos[1]});
}

// ----- GD&T: datum feature symbol --------------------------------------------
void compile_datum_feature(Draw& d, const json& ann) {
    P2 anchor{}, pos{};
    bool ha = rd2(ann, "a", anchor);
    if (!rd2(ann, "pos", pos)) {
        if (ha) pos = add(anchor, P2{0.0, -12.0});
        else return;
    }
    std::string label = ann.value("label", std::string("A"));
    const double h = TEXT_H;
    const double box = h * 1.7;
    // Boxed letter at pos (pos = box center).
    double bx = pos[0] - box * 0.5, by = pos[1] - box * 0.5;
    d.poly({{bx, by}, {bx + box, by}, {bx + box, by + box}, {bx, by + box}, {bx, by}});
    d.text({pos[0], pos[1] - h * 0.5}, label, h, "center", "base");
    if (ha) {
        // ASME Y14.5 datum feature: a filled triangle whose BASE sits on the feature, joined by
        // a leader from its apex to the nearest edge of the boxed letter.
        P2 u = unit(sub(pos, anchor));
        if (len(u) < 1e-9) u = {0.0, -1.0};
        P2 n = perp(u);
        P2 apex = add(anchor, mul(u, h * 0.9));
        d.tri(add(anchor, mul(n, h * 0.55)), sub(anchor, mul(n, h * 0.55)), apex);
        const bool vertical = std::fabs(u[1]) >= std::fabs(u[0]);
        P2 joint = vertical ? P2{pos[0], u[1] > 0 ? by : by + box}
                            : P2{u[0] > 0 ? bx : bx + box, pos[1]};
        d.seg(apex, joint);
    }
    d.pick = json::array({pos[0], pos[1]});
}

// ----- GD&T: datum target ----------------------------------------------------
void compile_datum_target(Draw& d, const json& ann) {
    P2 anchor{}, pos{};
    bool ha = rd2(ann, "a", anchor);
    if (!rd2(ann, "pos", pos)) {
        if (ha) pos = add(anchor, P2{12.0, 12.0});
        else return;
    }
    std::string label = ann.value("label", std::string("A1"));
    std::string ttype = ann.value("target_type", std::string("point"));
    double r = TEXT_H * 1.4;
    d.circle(pos, r);
    d.seg({pos[0] - r, pos[1]}, {pos[0] + r, pos[1]});  // horizontal divider
    // bottom half = label
    d.text({pos[0], pos[1] - TEXT_H * 0.85}, label, TEXT_H * 0.8, "center", "base");
    // top half = target size / type
    std::string top = ann.value("size", std::string());
    if (top.empty()) top = (ttype == "area") ? "A" : (ttype == "line") ? "L" : "";
    d.text({pos[0], pos[1] + TEXT_H * 0.15}, top, TEXT_H * 0.7, "center", "base");
    if (ha) {
        d.seg(anchor, {pos[0] - r * 0.7071, pos[1] - r * 0.7071});
        if (ttype == "point") {
            // target point: an X marker at the feature
            double k = TEXT_H * 0.5;
            d.seg({anchor[0] - k, anchor[1] - k}, {anchor[0] + k, anchor[1] + k});
            d.seg({anchor[0] - k, anchor[1] + k}, {anchor[0] + k, anchor[1] - k});
        }
    }
    d.pick = json::array({pos[0], pos[1]});
}

// ----- surface finish symbol -------------------------------------------------
void compile_surface_finish(Draw& d, const json& ann) {
    P2 anchor{}, pos{};
    bool ha = rd2(ann, "a", anchor);
    if (!rd2(ann, "pos", pos)) {
        if (ha) pos = add(anchor, P2{8.0, 8.0});
        else return;
    }
    const double h = TEXT_H;
    std::string state = ann.value("machining", std::string("any"));  // any|required|prohibited
    // The check-mark tick: short left leg up to a valley, long right leg up.
    P2 v = {pos[0], pos[1]};            // valley (bottom vertex)
    P2 l = {pos[0] - h * 0.7, pos[1] + h * 0.7};
    P2 rtop = {pos[0] + h * 1.0, pos[1] + h * 1.7};
    d.seg(l, v);
    d.seg(v, rtop);
    if (state == "required") {
        // a horizontal bar across the V (material removal required)
        d.seg({pos[0] - h * 0.35, pos[1] + h * 0.85}, {pos[0] + h * 0.55, pos[1] + h * 0.85});
    } else if (state == "prohibited") {
        d.circle({pos[0] + h * 0.15, pos[1] + h * 0.9}, h * 0.5);  // circle = removal prohibited
    }
    // Extension line on top for the value, plus all-around circle at the vertex.
    P2 top = rtop;
    d.seg(top, {top[0] + h * 1.6, top[1]});
    if (ann.value("all_around", false)) d.circle(v, h * 0.3);
    std::string val = ann.value("value", std::string());
    if (val.empty() && ann.contains("ra") && ann["ra"].is_number()) {
        char buf[32];
        std::snprintf(buf, sizeof(buf), "%.3g", ann["ra"].get<double>());
        val = buf;
    }
    if (!val.empty()) d.text({pos[0] - h * 0.2, top[1] + 0.4}, val, h * 0.85, "left", "base");
    if (ha) {
        d.seg(anchor, l);
        arrowhead(d, anchor, l);
    }
    d.pick = json::array({pos[0], pos[1]});
}

// ----- weld symbol (AWS A2.4 layout) -----------------------------------------
void compile_weld(Draw& d, const json& ann) {
    P2 anchor{}, pos{};
    bool ha = rd2(ann, "a", anchor);
    if (!rd2(ann, "pos", pos)) {
        if (ha) pos = add(anchor, P2{16.0, 0.0});
        else return;
    }
    const double h = TEXT_H;
    std::string length = field_text(ann, "length");
    if (length.empty()) length = field_text(ann, "intermittent_length");
    std::string pitch = field_text(ann, "pitch");
    std::string tail = ann.value("tail_text", ann.value("tail", std::string()));
    if (tail.empty()) {
        std::string process = ann.value("process", std::string());
        std::string spec = ann.value("spec", std::string());
        tail = process + (!process.empty() && !spec.empty() ? " / " : "") + spec;
    }
    bool full_penetration =
        ann.value("full_penetration", ann.value("complete_joint_penetration", false));
    bool has_tail = !tail.empty() || full_penetration;
    double refLen = h * (has_tail ? 6.0 : 4.5);
    P2 refA = pos;
    P2 refB = {pos[0] + refLen, pos[1]};
    d.seg(refA, refB);

    std::string wtype = ann.value("weld_type", std::string("fillet"));
    std::string size = field_text(ann, "size");
    bool other = ann.value("other_side", false);
    double sgn = other ? 1.0 : -1.0;
    P2 sp = {refA[0] + h * 1.6, refA[1]};
    if (wtype == "fillet") {
        d.poly({{sp[0], sp[1]}, {sp[0], sp[1] + sgn * h},
                {sp[0] + h, sp[1]}, {sp[0], sp[1]}});
    } else if (wtype == "square") {
        d.seg({sp[0], sp[1]}, {sp[0], sp[1] + sgn * h});
    } else if (wtype == "vee" || wtype == "bevel") {
        d.seg({sp[0], sp[1]}, {sp[0] - h * 0.4, sp[1] + sgn * h});
        if (wtype == "vee")
            d.seg({sp[0], sp[1]}, {sp[0] + h * 0.4, sp[1] + sgn * h});
        else
            d.seg({sp[0] + h * 0.4, sp[1]}, {sp[0] + h * 0.4, sp[1] + sgn * h});
    } else if (wtype == "plug") {
        d.poly({{sp[0] - h * 0.4, sp[1]}, {sp[0] - h * 0.4, sp[1] + sgn * h},
                {sp[0] + h * 0.4, sp[1] + sgn * h}, {sp[0] + h * 0.4, sp[1]}});
    }

    if (!size.empty())
        d.text({sp[0] - h * 0.35, refA[1] + sgn * h * 0.45}, size, h * 0.8, "right", "mid");
    if (!length.empty() || !pitch.empty()) {
        std::string intermittent = length;
        if (!pitch.empty()) intermittent += (intermittent.empty() ? "" : "-") + pitch;
        d.text({sp[0] + h * 1.25, refA[1] + sgn * h * 0.45}, intermittent,
               h * 0.8, "left", "mid");
    }

    std::string contour = ann.value("contour", std::string());
    double cy = refA[1] + sgn * h * 1.18;
    P2 contour_c = {sp[0] + h * 0.5, cy};
    if (contour == "flush" || contour == "flat") {
        d.seg({sp[0], cy}, {sp[0] + h, cy});
    } else if (contour == "convex") {
        d.arc(contour_c, h * 0.5, sgn > 0.0 ? 0.0 : CHUDWARE_PI,
              sgn > 0.0 ? CHUDWARE_PI : 2.0 * CHUDWARE_PI);
    } else if (contour == "concave") {
        d.arc(contour_c, h * 0.5, sgn > 0.0 ? CHUDWARE_PI : 0.0,
              sgn > 0.0 ? 2.0 * CHUDWARE_PI : CHUDWARE_PI);
    }
    std::string finish = ann.value("finish", std::string());
    if (!finish.empty())
        d.text({sp[0] + h * 1.15, cy}, finish, h * 0.7, "left", "mid");

    if (full_penetration) {
        double opp = -sgn;
        P2 pc = {sp[0] + h * 0.5, refA[1]};
        d.arc(pc, h * 0.38, opp > 0.0 ? 0.0 : CHUDWARE_PI,
              opp > 0.0 ? CHUDWARE_PI : 2.0 * CHUDWARE_PI);
        d.circle({pc[0], pc[1] + opp * h * 0.38}, h * 0.10);
    }

    if (ann.value("all_around", false)) d.circle(refA, h * 0.45);
    if (ann.value("field_weld", ann.value("field", false))) {
        P2 mast = {refA[0], refA[1] + h * 1.7};
        d.seg(refA, mast);
        d.poly({mast, {mast[0] + h * 1.05, mast[1] - h * 0.42},
                {mast[0], mast[1] - h * 0.84}, mast});
    }
    if (has_tail) {
        P2 upper = {refB[0] + h * 1.3, refB[1] + h * 0.75};
        P2 lower = {refB[0] + h * 1.3, refB[1] - h * 0.75};
        d.seg(refB, upper);
        d.seg(refB, lower);
        if (!tail.empty())
            d.text({upper[0] + h * 0.25, refB[1] + h * 0.15}, tail, h * 0.7, "left", "base");
        if (full_penetration)
            d.text({lower[0] + h * 0.25, refB[1] - h * 0.15}, "CJP", h * 0.7,
                   "left", "top");
    }
    if (ha) {
        d.seg(refA, anchor);
        arrowhead(d, anchor, refA);
    }
    d.pick = json::array({pos[0], pos[1]});
}

// ----- sheet frame: ISO 5457 border + zones, ISO 7200 title block ------------
constexpr double FRAME_LEFT = 20.0;   // filing margin
constexpr double FRAME_OTHER = 10.0;  // top / right / bottom margins
constexpr double TB_W = 180.0;        // ISO 7200 title block width
constexpr double TB_H = 50.0;

std::string ratio_text(double scale) {
    char buf[48];
    if (!(scale > 0.0)) return "1:1";
    if (scale >= 1.0) {
        if (std::fabs(scale - std::round(scale)) < 1e-6)
            std::snprintf(buf, sizeof(buf), "%d:1", (int)std::round(scale));
        else
            std::snprintf(buf, sizeof(buf), "%.3g:1", scale);
    } else {
        const double inv = 1.0 / scale;
        if (std::fabs(inv - std::round(inv)) < 1e-6)
            std::snprintf(buf, sizeof(buf), "1:%d", (int)std::round(inv));
        else
            std::snprintf(buf, sizeof(buf), "1:%.3g", inv);
    }
    return buf;
}

std::string upper(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return static_cast<char>(std::toupper(c)); });
    return s;
}

std::string tb_field(const json& tb, std::initializer_list<const char*> keys) {
    for (const char* k : keys) {
        auto it = tb.find(k);
        if (it == tb.end()) continue;
        std::string v = scalar_text(*it);
        if (it->is_boolean()) v = it->get<bool>() ? "YES" : "NO";
        if (!v.empty()) return v;
    }
    return {};
}

// Fit `value` into a cell `w` wide: shrink the text height down to `min_h`, then ellipsize.
void cell_value(Draw& d, double x, double y, double w, std::string value, double h,
                double min_h, const char* align = "left") {
    if (value.empty()) return;
    const double room = w - 3.0;
    double tw = text_width(value, h);
    if (tw > room) h = std::max(min_h, h * room / tw);
    while (text_width(value, h) > room && value.size() > 2) {
        size_t cut = value.size() - 1;
        while (cut > 0 && ((unsigned char)value[cut] & 0xC0) == 0x80) --cut;
        value = value.substr(0, cut);
        while (!value.empty() && value.back() == '.') value.pop_back();
        value += "..";
        if (text_width(value, h) <= room) break;
        value = value.substr(0, value.size() - 2);
    }
    const std::string a = align;
    const double px = a == "center" ? x + w * 0.5 : x + 1.5;
    d.text({px, y}, value, h, align, "base");
}

// ISO 5456-2 projection-method symbol in a box whose lower-left is (x, y), size w x h.
void projection_symbol(Draw& d, double x, double y, double w, double h, bool first_angle) {
    const double D = std::min(8.0, h * 0.62), dd = D * 0.5;  // cone large / small diameters
    const double L = D;                                       // cone length
    const double gap = D * 0.55;
    const double total = L + gap + D;
    const double x0 = x + (w - total) * 0.5;
    const double cy = y + h * 0.5;
    const double cone_x = first_angle ? x0 + D + gap : x0;  // cone's large (left) end
    const double circ_cx = first_angle ? x0 + D * 0.5 : x0 + L + gap + D * 0.5;
    d.thick.push_back(pts_json({{cone_x, cy - D * 0.5}, {cone_x + L, cy - dd * 0.5},
                                {cone_x + L, cy + dd * 0.5}, {cone_x, cy + D * 0.5},
                                {cone_x, cy - D * 0.5}}));
    d.arc({circ_cx, cy}, D * 0.5, 0.0, 2.0 * CHUDWARE_PI);
    d.arc({circ_cx, cy}, dd * 0.5, 0.0, 2.0 * CHUDWARE_PI);
    d.chain_seg({cone_x - 1.5, cy}, {cone_x + L + 1.5, cy});
    d.chain_seg({circ_cx - D * 0.5 - 1.5, cy}, {circ_cx + D * 0.5 + 1.5, cy});
    d.chain_seg({circ_cx, cy - D * 0.5 - 1.5}, {circ_cx, cy + D * 0.5 + 1.5});
}

json build_sheet_frame(const json& sheet, const std::string& units, int sheet_index,
                       int sheet_count) {
    Draw d;
    const json& mm = sheet.value("size_mm", json(nullptr));
    if (!mm.is_array() || mm.size() != 2 || !mm[0].is_number() || !mm[1].is_number())
        return d.finish();
    const double W = mm[0].get<double>(), H = mm[1].get<double>();
    if (W < 100.0 || H < 100.0) return d.finish();
    const auto in = sheet_frame_inner(W, H);
    const double fx0 = in[0], fy0 = in[1], fx1 = in[2], fy1 = in[3];

    // Drawing frame (thick) + zone grid in the margin (thin) + centring marks (thick).
    d.rect(fx0, fy0, fx1, fy1, true);
    const double zone_band = 5.0;
    d.rect(fx0 - zone_band, fy0 - zone_band, fx1 + zone_band, fy1 + zone_band);
    const int nx = std::max(2, (int)std::lround((fx1 - fx0) / 50.0));
    const int ny = std::max(2, (int)std::lround((fy1 - fy0) / 50.0));
    const double zw = (fx1 - fx0) / nx, zh = (fy1 - fy0) / ny;
    const double zt = 2.5;  // zone label height
    for (int i = 0; i < nx; ++i) {
        const double xa = fx0 + i * zw;
        if (i > 0) {
            d.seg({xa, fy1}, {xa, fy1 + zone_band});
            d.seg({xa, fy0 - zone_band}, {xa, fy0});
        }
        const std::string num = std::to_string(i + 1);
        d.text({xa + zw * 0.5, fy1 + zone_band * 0.5}, num, zt, "center", "mid");
        d.text({xa + zw * 0.5, fy0 - zone_band * 0.5}, num, zt, "center", "mid");
    }
    for (int j = 0; j < ny; ++j) {
        const double ya = fy1 - j * zh;  // letters run top-down
        if (j > 0) {
            d.seg({fx0 - zone_band, ya}, {fx0, ya});
            d.seg({fx1, ya}, {fx1 + zone_band, ya});
        }
        std::string letter(1, static_cast<char>('A' + (j % 26)));
        if (letter == "I") letter = "J";  // ISO 5457 skips I and O
        if (letter == "O") letter = "P";
        d.text({fx0 - zone_band * 0.5, ya - zh * 0.5}, letter, zt, "center", "mid");
        d.text({fx1 + zone_band * 0.5, ya - zh * 0.5}, letter, zt, "center", "mid");
    }
    const double cxm = (fx0 + fx1) * 0.5, cym = (fy0 + fy1) * 0.5;
    d.thick_seg({cxm, fy1 + zone_band}, {cxm, fy1 - 5.0});
    d.thick_seg({cxm, fy0 - zone_band}, {cxm, fy0 + 5.0});
    d.thick_seg({fx0 - zone_band, cym}, {fx0 + 5.0, cym});
    d.thick_seg({fx1 + zone_band, cym}, {fx1 - 5.0, cym});

    // Title block (ISO 7200 field set), lower-right corner inside the frame.
    const json& tb = sheet.value("title_block", json::object());
    const auto r = sheet_title_block_rect(W, H);
    const double bx = r[0], by = r[1], bw = r[2] - r[0];
    const double k = bw / TB_W;  // narrow sheets compress the block horizontally
    d.rect(r[0], r[1], r[2], r[3], true);
    const double LBL = 1.8;
    struct Cell {
        double x0, x1, y0, y1;
        const char* label;
        std::string value;
        double h;
    };
    std::string projection = upper(tb_field(tb, {"projection"}));
    const bool first_angle = projection.find("FIRST") != std::string::npos ||
                             projection.find("1ST") != std::string::npos;
    // UNITS always reflects the drawing's unit system, so it can never contradict the dims.
    const std::string units_v = units == "IPS" ? "inch" : "mm";
    std::string sheet_v = tb_field(tb, {"sheet"});
    if (sheet_v.empty())
        sheet_v = std::to_string(std::max(1, sheet_index)) + " OF " +
                  std::to_string(std::max(1, sheet_count));
    std::string size_v = scalar_text(sheet.value("size", json(nullptr)));
    if (!size_v.empty() && size_v.back() == 'P') size_v = size_v.substr(0, size_v.size() - 1);
    std::string tol_v = tb_field(tb, {"general_tolerance", "tolerance"});
    std::string std_v = tb_field(tb, {"drawing_standard", "standard"});
    const std::vector<Cell> cells = {
        // Row 1 (bottom, 16 mm): title, drawing number, revision, sheet.
        {0, 100, 0, 16, "TITLE", upper(tb_field(tb, {"title"})), 5.0},
        {100, 145, 0, 16, "DRAWING / PART NO.", tb_field(tb, {"part_number", "part_no", "drawing_number"}), 3.5},
        {145, 160, 0, 16, "REV", tb_field(tb, {"rev", "revision"}), 5.0},
        {160, 180, 0, 16, "SHEET", sheet_v, 3.0},
        // Row 2 (10 mm): material / finish / mass / scale / size.
        {0, 62, 16, 26, "MATERIAL", tb_field(tb, {"material"}), 3.0},
        {62, 110, 16, 26, "FINISH", tb_field(tb, {"finish"}), 3.0},
        {110, 135, 16, 26, "MASS", tb_field(tb, {"mass"}), 3.0},
        {135, 160, 16, 26, "SCALE", tb_field(tb, {"scale"}), 3.0},
        {160, 180, 16, 26, "SIZE", size_v, 3.0},
        // Row 3 (10 mm): document control.
        {0, 32, 26, 36, "DRAWN", tb_field(tb, {"drawn_by", "drawn"}), 2.5},
        {32, 57, 26, 36, "DATE", tb_field(tb, {"date"}), 2.5},
        {57, 85, 26, 36, "CHECKED", tb_field(tb, {"checked_by", "checked"}), 2.5},
        {85, 113, 26, 36, "APPROVED", tb_field(tb, {"approved_by", "approved"}), 2.5},
        {113, 180, 26, 36, "COMPANY", tb_field(tb, {"company"}), 3.0},
        // Row 4 (14 mm): projection method, units, general tolerances / standard.
        {0, 40, 36, 50, first_angle ? "FIRST ANGLE PROJECTION" : "THIRD ANGLE PROJECTION", "", 0.0},
        {40, 62, 36, 50, "UNITS", units_v, 3.0},
        {62, 180, 36, 50, "GENERAL TOLERANCES / STANDARD", "", 0.0},
    };
    for (const Cell& c : cells) {
        const double x0 = bx + c.x0 * k, x1 = bx + c.x1 * k;
        const double y0 = by + c.y0, y1 = by + c.y1;
        d.rect(x0, y0, x1, y1);
        cell_value(d, x0, y1 - 1.0 - LBL, x1 - x0, c.label, LBL, 1.4);
        if (!c.value.empty()) cell_value(d, x0, y0 + 2.0, x1 - x0, c.value, c.h, 1.8);
    }
    // Projection symbol beneath its label.
    projection_symbol(d, bx, by + 36.0, 40.0 * k, 14.0 - LBL - 1.5, first_angle);
    // General tolerance + standard: two stacked lines.
    {
        const double x0 = bx + 62.0 * k, w = (180.0 - 62.0) * k;
        std::string line1 = tol_v.empty() ? "UNLESS OTHERWISE SPECIFIED: DIMENSIONS IN " +
                                                upper(units_v)
                                          : "GENERAL TOLERANCES: " + tol_v;
        std::string line2 = std_v.empty() ? "DRAWN TO ISO 128 / ISO 129-1" : std_v;
        cell_value(d, x0, by + 36.0 + 6.2, w, line1, 2.5, 1.6);
        cell_value(d, x0, by + 36.0 + 2.0, w, line2, 2.5, 1.6);
    }
    return d.finish();
}

// Section cutting planes / detail circles on a parent view + captions under derived views.
json build_view_decorations(const json& view) {
    Draw d;
    const double LH = 5.0;  // view-identification letter height (1.4 x 3.5)
    for (const json& sl : view.value("section_lines", json::array())) {
        if (!sl.is_object() || !sl.contains("line") || !sl["line"].is_array()) continue;
        std::vector<P2> pts;
        for (const json& q : sl["line"]) {
            json holder = json{{"p", q}};
            P2 p{};
            if (rd2(holder, "p", p)) pts.push_back(p);
        }
        if (pts.size() < 2) continue;
        const P2 a = pts.front(), b = pts.back();
        const P2 u = unit(sub(b, a));
        if (len(u) < 1e-9) continue;
        // ISO 128-40: thin chain cutting plane, thick at its ends; arrows show the viewing
        // direction (sheet-left of a->b, flipped by `reverse`), letters beside the arrows.
        d.chain_poly(pts);
        const double end_len = 6.0;
        d.thick_seg(a, add(a, mul(u, end_len)));
        d.thick_seg(b, sub(b, mul(u, end_len)));
        P2 look = perp(u);
        if (sl.value("reverse", false)) look = mul(look, -1.0);
        std::string label = scalar_text(sl.value("label", json(nullptr)));
        if (label.empty()) label = "A";
        for (P2 end : {add(a, mul(u, end_len * 0.5)), sub(b, mul(u, end_len * 0.5))}) {
            const P2 tip = add(end, mul(look, 7.0));
            d.seg(end, tip);
            arrowhead(d, tip, end, 3.5, 0.75);
            const P2 lp = add(tip, mul(look, LH * 0.6 + 1.0));
            d.text(lp, label, LH, "center", "mid");
        }
    }
    for (const json& dc : view.value("detail_circles", json::array())) {
        P2 c{};
        if (!dc.is_object() || !rd2(dc, "center", c)) continue;
        const double r = dc.value("radius", 0.0);
        if (r <= 0.0) continue;
        d.circle(c, r);
        std::string label = scalar_text(dc.value("label", json(nullptr)));
        if (label.empty()) label = "A";
        const P2 at = add(c, P2{r * 0.7071, r * 0.7071});
        const P2 lp = add(at, P2{3.0, 3.0});
        d.seg(at, lp);
        d.text({lp[0] + 0.8, lp[1]}, label, LH, "left", "base");
    }
    const std::string type = view.value("type", std::string());
    const json& bb = view.value("bbox", json(nullptr));
    const std::string own_caption = scalar_text(view.value("caption", json(nullptr)));
    if (!own_caption.empty() && type != "section" && type != "detail" && bb.is_array() &&
        bb.size() == 4 && bb[0].is_number()) {
        const double cx = (bb[0].get<double>() + bb[2].get<double>()) * 0.5;
        d.text({cx, bb[1].get<double>() - 4.0}, own_caption, TEXT_H, "center", "top");
    }
    if ((type == "section" || type == "detail") && bb.is_array() && bb.size() == 4 &&
        bb[0].is_number()) {
        std::string label = scalar_text(view.value("label", json(nullptr)));
        if (label.empty()) label = "A";
        std::string caption = type == "section" ? "SECTION " + label + "-" + label
                                                : "DETAIL " + label;
        if (type == "detail") caption += "  SCALE " + ratio_text(view.value("scale", 1.0));
        const double cx = (bb[0].get<double>() + bb[2].get<double>()) * 0.5;
        d.text({cx, bb[1].get<double>() - 6.0}, caption, LH, "center", "top");
    }
    return d.finish();
}

}  // namespace

bool annot_is_known(const std::string& kind) {
    static const char* k[] = {
        "linear_dim",     "aligned_dim",    "radial_dim",    "diameter_dim",
        "hole_callout",   "angular_dim",    "note",          "center_mark",
        "center_line",    "balloon",        "chamfer",       "chamfer_dim",
        "chamfer_callout", "ordinate",       "ordinate_dim",  "gdt_frame",
        "datum_feature",  "datum_target",   "surface_finish", "weld_symbol",
        "pitch_circle"};
    for (const char* s : k)
        if (kind == s) return true;
    return false;
}

json compile_annotation(const json& ann, const std::string& units) {
    Draw d;
    if (!ann.is_object()) return d.finish();
    std::string kind = ann.value("kind", std::string());
    if (kind.empty()) kind = ann.value("type", std::string());
    std::string txt = ann.value("text", std::string());

    P2 a{}, b{}, p{}, c{};
    bool ha = rd2(ann, "a", a), hb = rd2(ann, "b", b), hp = rd2(ann, "pos", p);
    bool hc = rd2(ann, "center", c);

    if (kind == "gdt_frame") { compile_gdt_frame(d, ann); return d.finish(); }
    if (kind == "datum_feature") { compile_datum_feature(d, ann); return d.finish(); }
    if (kind == "datum_target") { compile_datum_target(d, ann); return d.finish(); }
    if (kind == "surface_finish") { compile_surface_finish(d, ann); return d.finish(); }
    if (kind == "weld_symbol") { compile_weld(d, ann); return d.finish(); }
    if (kind == "chamfer" || kind == "chamfer_dim" || kind == "chamfer_callout") {
        compile_chamfer(d, ann);
        return d.finish();
    }
    if (kind == "ordinate" || kind == "ordinate_dim") {
        compile_ordinate(d, ann);
        return d.finish();
    }

    if (kind == "center_mark") {
        // ISO 128 / ASME Y14.2 centre mark: a short solid cross at the centre, a gap, then
        // centre lines running a little past the circle in both axes.
        P2 cc = hc ? c : (ha ? a : p);
        const double r = ann.value("radius", 0.0);
        const double cross = 1.5, gap = 1.0;
        d.seg({cc[0] - cross, cc[1]}, {cc[0] + cross, cc[1]});
        d.seg({cc[0], cc[1] - cross}, {cc[0], cc[1] + cross});
        const double reach = r > 0.0 ? r + 2.5 : 4.0;
        if (reach > cross + gap + 1.0) {
            for (P2 u : {P2{1, 0}, P2{-1, 0}, P2{0, 1}, P2{0, -1}})
                d.seg(add(cc, mul(u, cross + gap)), add(cc, mul(u, reach)));
        }
        d.pick = json::array({cc[0], cc[1]});
        return d.finish();
    }
    if (kind == "center_line") {
        if (ha && hb) {
            P2 u = unit(sub(b, a));
            const double over = ann.value("overshoot", 3.0);
            d.chain_seg(sub(a, mul(u, over)), add(b, mul(u, over)));
        }
        d.pick = hp ? json::array({p[0], p[1]}) : (ha ? json::array({a[0], a[1]}) : json(nullptr));
        return d.finish();
    }
    if (kind == "pitch_circle") {
        const double r = ann.value("radius", 0.0);
        if (hc && r > 0.0) {
            d.chain_poly([&] {
                std::vector<P2> pts;
                const int n = std::max(48, (int)std::ceil(2.0 * CHUDWARE_PI * r / 1.5));
                for (int i = 0; i <= n; ++i) {
                    const double t = 2.0 * CHUDWARE_PI * i / n;
                    pts.push_back({c[0] + r * std::cos(t), c[1] + r * std::sin(t)});
                }
                return pts;
            }());
            d.pick = json::array({c[0] + r, c[1]});
        }
        return d.finish();
    }
    if (kind == "balloon") {
        if (!hp) return d.finish();
        const double r = 4.5;
        if (ha && len(sub(a, p)) > r + 0.5) {
            const P2 u = unit(sub(a, p));
            d.seg(add(p, mul(u, r)), a);
            d.circle(a, 0.6);  // leader ends on a dot inside the part outline
        }
        d.circle(p, r);
        if (txt.empty() && ann.contains("item") && ann["item"].is_number())
            txt = std::to_string(ann["item"].get<int>());
        d.text(p, txt, TEXT_H, "center", "mid");
        d.pick = json::array({p[0], p[1]});
        return d.finish();
    }
    if (kind == "hole_callout" || kind == "radial_dim" || kind == "diameter_dim") {
        P2 cc = hc ? c : a;
        P2 pp = hp ? p : add(cc, P2{8.0, 8.0});
        compile_leader_dim(d, ann, cc, pp, txt);
        return d.finish();
    }
    if (kind == "angular_dim") {
        if (ha && hb && hp) compile_angular(d, ann, a, b, p, txt);
        return d.finish();
    }
    if (kind == "note") {
        if (hp) compile_note(d, ann, a, ha, p, txt);
        return d.finish();
    }
    if (!ha || !hb) return d.finish();
    // linear / aligned dimension
    if (!hp) p = mul(add(a, b), 0.5);
    compile_linear(d, ann, a, b, p, txt);
    return d.finish();
}

std::array<double, 4> sheet_frame_inner(double w, double h) {
    return {FRAME_LEFT, FRAME_OTHER, w - FRAME_OTHER, h - FRAME_OTHER};
}

std::array<double, 4> sheet_title_block_rect(double w, double h) {
    const auto in = sheet_frame_inner(w, h);
    const double bw = std::min(TB_W, in[2] - in[0]);
    return {in[2] - bw, in[1], in[2], in[1] + std::min(TB_H, (in[3] - in[1]) * 0.4)};
}

json compile_sheet_frame(const json& sheet, const std::string& units, int sheet_index,
                         int sheet_count) {
    try {
        return build_sheet_frame(sheet, units, sheet_index, sheet_count);
    } catch (const std::exception&) {
        return Draw{}.finish();
    }
}

json compile_view_decorations(const json& view) {
    try {
        return build_view_decorations(view);
    } catch (const std::exception&) {
        return Draw{}.finish();
    }
}

double annot_text_width(const std::string& text, double h) { return text_width(text, h); }

json compile_hatch(const json& polygon, const json& style) {
    Draw d;
    if (!polygon.is_array() || polygon.size() < 3) return json{{"segments", json::array()}};

    double spacing = 3.0;
    double angle_deg = 45.0;
    std::string pattern = "single";
    if (style.is_object()) {
        auto spacing_it = style.find("spacing_mm");
        if (spacing_it == style.end()) spacing_it = style.find("spacing");
        if (spacing_it != style.end() && spacing_it->is_number() &&
            spacing_it->get<double>() > 0.0)
            spacing = spacing_it->get<double>();
        auto angle_it = style.find("angle_deg");
        if (angle_it == style.end()) angle_it = style.find("angle");
        if (angle_it != style.end() && angle_it->is_number())
            angle_deg = angle_it->get<double>();
        if (style.contains("pattern") && style["pattern"].is_string())
            pattern = style["pattern"].get<std::string>();
    }
    std::transform(pattern.begin(), pattern.end(), pattern.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });

    std::vector<P2> poly;
    poly.reserve(polygon.size());
    for (const auto& pt : polygon) {
        if (!pt.is_array() || pt.size() < 2 || !pt[0].is_number() || !pt[1].is_number())
            continue;
        poly.push_back({pt[0].get<double>(), pt[1].get<double>()});
    }
    if (poly.size() < 3) return json{{"segments", json::array()}};

    auto family = [&](double family_angle_deg, double phase) {
        double ang = family_angle_deg * CHUDWARE_PI / 180.0;
        P2 dir = {std::cos(ang), std::sin(ang)};
        P2 nrm = perp(dir);
        double cmin = 1e30, cmax = -1e30;
        for (P2 q : poly) {
            double cc = dot(q, nrm);
            cmin = std::min(cmin, cc);
            cmax = std::max(cmax, cc);
        }
        for (double off = cmin + spacing * phase; off < cmax; off += spacing) {
            P2 base = mul(nrm, off);
            std::vector<double> ts;
            ts.reserve(poly.size());
            for (size_t i = 0; i < poly.size(); ++i) {
                P2 q0 = poly[i], q1 = poly[(i + 1) % poly.size()];
                P2 edge = sub(q1, q0);
                double den = dir[0] * (-edge[1]) - dir[1] * (-edge[0]);
                if (std::fabs(den) < 1e-12) continue;
                P2 rhs = sub(q0, base);
                double t = (rhs[0] * (-edge[1]) - rhs[1] * (-edge[0])) / den;
                double edge_t = (dir[0] * rhs[1] - dir[1] * rhs[0]) / den;
                if (edge_t >= -1e-9 && edge_t <= 1.0 + 1e-9) ts.push_back(t);
            }
            std::sort(ts.begin(), ts.end());
            for (size_t i = 0; i + 1 < ts.size(); i += 2)
                if (std::fabs(ts[i + 1] - ts[i]) > 1e-9)
                    d.seg(add(base, mul(dir, ts[i])), add(base, mul(dir, ts[i + 1])));
        }
    };

    if (pattern == "cross" || pattern == "crosshatch" || pattern == "ansi32") {
        family(angle_deg, 0.5);
        family(angle_deg + 90.0, 0.5);
    } else if (pattern == "double" || pattern == "paired" || pattern == "aluminum" ||
               pattern == "ansi33") {
        family(angle_deg, 0.28);
        family(angle_deg, 0.62);
    } else {
        pattern = "single";
        family(angle_deg, 0.5);
    }
    return json{{"segments", d.segments},
                {"style", json{{"pattern", pattern},
                               {"spacing_mm", spacing},
                               {"angle_deg", angle_deg}}}};
}

json compile_hatch(const json& polygon, double spacing, double angle_deg) {
    return compile_hatch(polygon, json{{"spacing_mm", spacing > 0.0 ? spacing : 3.0},
                                       {"angle_deg", angle_deg},
                                       {"pattern", "single"}});
}

}  // namespace chudware::doc
