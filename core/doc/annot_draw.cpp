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
constexpr double TEXT_H = 3.5;     // nominal text height
constexpr double ARROW_LEN = 3.2;  // arrowhead length
constexpr double ARROW_W = 1.1;    // arrowhead half-width
constexpr double EXT_GAP = 1.0;    // gap between geometry and start of an extension line
constexpr double EXT_OVER = 1.5;   // extension-line overshoot past the dimension line
constexpr double CHAR_W = 0.62;    // mean glyph advance as a fraction of text height

using P2 = std::array<double, 2>;

// ----- primitive accumulators ------------------------------------------------
struct Draw {
    json segments = json::array();
    json dashed = json::array();
    json arcs = json::array();
    json tris = json::array();
    json texts = json::array();
    json pick;

    void seg(P2 a, P2 b) {
        segments.push_back(json::array({json::array({a[0], a[1]}), json::array({b[0], b[1]})}));
    }
    void poly(const std::vector<P2>& pts, bool dash = false) {
        json pl = json::array();
        for (auto& p : pts) pl.push_back(json::array({p[0], p[1]}));
        (dash ? dashed : segments).push_back(pl);
    }
    void dash_seg(P2 a, P2 b) {
        dashed.push_back(json::array({json::array({a[0], a[1]}), json::array({b[0], b[1]})}));
    }
    void arc(P2 c, double r, double a0, double a1) {
        arcs.push_back(json{{"center", json::array({c[0], c[1]})}, {"radius", r},
                            {"a0", a0}, {"a1", a1}});
    }
    void circle(P2 c, double r) { arc(c, r, 0.0, 2.0 * CHUDWARE_PI); }
    void tri(P2 a, P2 b, P2 c) {
        tris.push_back(json::array({json::array({a[0], a[1]}), json::array({b[0], b[1]}),
                                    json::array({c[0], c[1]})}));
    }
    void text(P2 pos, const std::string& t, double h = TEXT_H, const char* align = "left",
              const char* valign = "base") {
        if (t.empty()) return;
        texts.push_back(json{{"pos", json::array({pos[0], pos[1]})}, {"h", h}, {"text", t},
                             {"align", align}, {"valign", valign}});
    }
    json finish() {
        return json{{"segments", segments}, {"dashed", dashed}, {"arcs", arcs},
                    {"tris", tris}, {"texts", texts},
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

double text_width(const std::string& t, double h) {
    // UTF-8 aware-enough advance estimate: count code points, not bytes.
    int cp = 0;
    for (size_t i = 0; i < t.size();) {
        unsigned char c = (unsigned char)t[i];
        i += (c < 0x80) ? 1 : (c >> 5) == 0x6 ? 2 : (c >> 4) == 0xE ? 3 : 4;
        ++cp;
    }
    return cp * h * CHAR_W;
}

const std::string kDia = "\xE2\x8C\x80";  // U+2300 ⌀

// Strip a leading ⌀ from a composed dim string (we stroke it instead of relying on a font).
bool strip_dia(std::string& s) {
    if (s.size() >= 3 && (unsigned char)s[0] == 0xE2 && (unsigned char)s[1] == 0x8C &&
        (unsigned char)s[2] == 0x80) {
        s = s.substr(3);
        return true;
    }
    return false;
}

// A ⌀ diameter glyph: a circle with a slash, drawn left-aligned at baseline `pos`, height h.
void glyph_dia(Draw& d, P2 pos, double h) {
    double r = h * 0.36;
    P2 c = {pos[0] + r, pos[1] + h * 0.5};
    d.circle(c, r);
    double k = r * 1.25;
    d.seg({c[0] - k * 0.7071, c[1] - k * 0.7071}, {c[0] + k * 0.7071, c[1] + k * 0.7071});
}

// An inward arrowhead: filled triangle with tip at `tip`, pointing toward `toward`.
void arrowhead(Draw& d, P2 tip, P2 toward) {
    P2 u = unit(sub(toward, tip));
    if (len(u) < 1e-9) return;
    P2 n = perp(u);
    P2 base = add(tip, mul(u, ARROW_LEN));
    d.tri(tip, add(base, mul(n, ARROW_W)), sub(base, mul(n, ARROW_W)));
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

// ----- dimension kinds (parity with the legacy host renderer) ----------------
void compile_linear(Draw& d, const json& ann, P2 a, P2 b, P2 p, const std::string& txt) {
    P2 dir = sub(b, a);
    if (len(dir) < 1e-6) return;
    P2 u = unit(dir);
    P2 pa = sub(p, a);
    double along = pa[0] * u[0] + pa[1] * u[1];
    P2 off = sub(pa, mul(u, along));  // perpendicular A -> dimension line
    P2 A2 = add(a, off), B2 = add(b, off);
    P2 n = unit(off);
    // Extension lines: small gap off the geometry, slight overshoot past the dim line.
    auto ext = [&](P2 from, P2 to) {
        P2 e = unit(sub(to, from));
        if (len(e) < 1e-9) return;
        d.seg(add(from, mul(e, EXT_GAP)), add(to, mul(e, EXT_OVER)));
    };
    ext(a, A2);
    ext(b, B2);
    d.seg(A2, B2);
    arrowhead(d, A2, B2);
    arrowhead(d, B2, A2);
    P2 mid = mul(add(A2, B2), 0.5);
    P2 tp = add(mid, mul(n, TEXT_H * 0.5 + 0.8));
    std::string t = txt;
    if (strip_dia(t)) glyph_dia(d, sub(tp, P2{text_width(t, TEXT_H) * 0.5 + TEXT_H * 0.5, 0}), TEXT_H);
    d.text(tp, t, TEXT_H, "center", "mid");
    d.pick = json::array({p[0], p[1]});
}

void compile_leader_dim(Draw& d, const json& ann, P2 c, P2 p, std::string txt) {
    d.seg(c, p);
    d.tri({c[0] - 0.6, c[1] - 0.6}, {c[0] + 0.6, c[1] - 0.6}, {c[0], c[1] + 0.8});  // dot
    P2 tp = {p[0] + 1.0, p[1]};
    if (strip_dia(txt)) {
        glyph_dia(d, {tp[0], tp[1] - TEXT_H * 0.5}, TEXT_H);
        tp[0] += TEXT_H * 0.85;
    }
    d.text(tp, txt, TEXT_H, "left", "mid");
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

    double radius = std::max(len(sub(pos, vertex)), ARROW_LEN * 2.0);
    d.seg(add(vertex, mul(u0, EXT_GAP)), add(vertex, mul(u0, radius + EXT_OVER)));
    d.seg(add(vertex, mul(u1, EXT_GAP)), add(vertex, mul(u1, radius + EXT_OVER)));
    d.arc(vertex, radius, a0, a0 + sweep);

    P2 arc0 = add(vertex, mul(u0, radius));
    P2 arc1 = add(vertex, mul(u1, radius));
    double inward_step = sweep * 0.08;
    P2 in0 = add(vertex, P2{radius * std::cos(a0 + inward_step),
                            radius * std::sin(a0 + inward_step)});
    P2 in1 = add(vertex, P2{radius * std::cos(a0 + sweep - inward_step),
                            radius * std::sin(a0 + sweep - inward_step)});
    arrowhead(d, arc0, in0);
    arrowhead(d, arc1, in1);
    d.text(pos, txt.empty() ? "?" : txt, TEXT_H, "center", "mid");
    d.pick = json::array({pos[0], pos[1]});
}

void compile_note(Draw& d, const json& ann, P2 anchor, bool has_anchor, P2 pos,
                  const std::string& txt) {
    std::string label = txt.empty() ? "note" : txt;
    if (has_anchor) {
        P2 elbow{};
        bool has_elbow = rd2(ann, "b", elbow) && len(sub(elbow, anchor)) > 1e-6;
        if (!has_elbow) elbow = {pos[0] - 2.0, pos[1] - 1.0};
        double landing_y = elbow[1];
        P2 landing_end = {std::max(pos[0] + text_width(label, TEXT_H), elbow[0] + 4.0),
                          landing_y};
        d.poly({anchor, elbow, landing_end});
        arrowhead(d, anchor, elbow);
    }
    d.text(pos, label, TEXT_H, "left", "base");
    d.pick = json::array({pos[0], pos[1]});
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
    P2 elbow = {pos[0] - 2.0, pos[1] - 1.0};
    P2 landing_end = {pos[0] + std::max(4.0, text_width(label, TEXT_H)), elbow[1]};
    d.poly({anchor, elbow, landing_end});
    arrowhead(d, anchor, elbow);
    d.text(pos, label, TEXT_H, "left", "base");
    d.pick = json::array({pos[0], pos[1]});
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
    double tolW = pad * 2 + (dia ? h * 0.9 : 0) + text_width(tol.empty() ? "0" : tol, h) +
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
        glyph_dia(d, {tx, y0 + (H - h) * 0.5}, h);
        tx += h * 0.9;
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
        // Filled datum triangle at the feature, connected by a leader to the box.
        d.seg(anchor, {pos[0], by + box});  // leader to box top (simplified)
        P2 u = unit(sub(pos, anchor));
        P2 n = perp(u);
        P2 baseC = add(anchor, mul(u, h * 1.0));
        d.tri(anchor, add(baseC, mul(n, h * 0.5)), sub(baseC, mul(n, h * 0.5)));
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

}  // namespace

bool annot_is_known(const std::string& kind) {
    static const char* k[] = {
        "linear_dim",     "aligned_dim",    "radial_dim",    "diameter_dim",
        "hole_callout",   "angular_dim",    "note",          "center_mark",
        "center_line",    "balloon",        "chamfer",       "chamfer_dim",
        "chamfer_callout", "ordinate",       "ordinate_dim",  "gdt_frame",
        "datum_feature",  "datum_target",   "surface_finish", "weld_symbol"};
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
        P2 cc = hc ? c : (ha ? a : p);
        double r = 2.5;
        d.seg({cc[0] - r, cc[1]}, {cc[0] + r, cc[1]});
        d.seg({cc[0], cc[1] - r}, {cc[0], cc[1] + r});
        d.pick = json::array({cc[0], cc[1]});
        return d.finish();
    }
    if (kind == "center_line") {
        if (ha && hb) {
            P2 u = unit(sub(b, a));
            P2 e = mul(u, 2.5);
            d.dash_seg(sub(a, e), add(b, e));
        }
        d.pick = hp ? json::array({p[0], p[1]}) : (ha ? json::array({a[0], a[1]}) : json(nullptr));
        return d.finish();
    }
    if (kind == "balloon") {
        if (!hp) return d.finish();
        double r = 4.0;
        if (ha) { d.seg(a, p); }
        d.circle(p, r);
        if (txt.empty() && ann.contains("item") && ann["item"].is_number())
            txt = std::to_string(ann["item"].get<int>());
        d.text({p[0], p[1] - TEXT_H * 0.5}, txt, TEXT_H, "center", "base");
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
