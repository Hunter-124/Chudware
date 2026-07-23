// Sketch 2D geometry — see sketch_geom.h. Function-by-function port of
// engine/doc/sketch_geom.py (the oracle). Entity dicts are nlohmann::json.

#define _USE_MATH_DEFINES
#include "doc/sketch_geom.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <limits>
#include <set>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

namespace chudware::doc::sketchgeom {

using nlohmann::json;

namespace {

constexpr double TOL = 1e-7;
constexpr double DEG = M_PI / 180.0;

using V2 = std::array<double, 2>;

V2 sub(const V2& a, const V2& b) { return {a[0] - b[0], a[1] - b[1]}; }
V2 add(const V2& a, const V2& b) { return {a[0] + b[0], a[1] + b[1]}; }
V2 scale(const V2& a, double s) { return {a[0] * s, a[1] * s}; }
double dot(const V2& a, const V2& b) { return a[0] * b[0] + a[1] * b[1]; }
double cross(const V2& a, const V2& b) { return a[0] * b[1] - a[1] * b[0]; }
double vlen(const V2& a) { return std::hypot(a[0], a[1]); }
V2 vnorm(const V2& a) {
    double L = vlen(a);
    if (L < TOL) return {0.0, 0.0};
    return {a[0] / L, a[1] / L};
}
V2 perp(const V2& a) { return {-a[1], a[0]}; }
bool vnear(const V2& a, const V2& b, double tol = TOL) {
    return std::abs(a[0] - b[0]) <= tol && std::abs(a[1] - b[1]) <= tol;
}
bool is_zero(const V2& a) { return a[0] == 0.0 && a[1] == 0.0; }

// Python-style modulo (result has sign of the divisor; here divisor>0 so >= 0).
double pymod(double a, double m) {
    double r = std::fmod(a, m);
    if (r < 0) r += m;
    return r;
}

// JSON entity field accessors.
std::string etype(const json& e) { return e.value("type", std::string()); }
V2 pt(const json& e, const char* k) { return {e[k][0].get<double>(), e[k][1].get<double>()}; }
double num(const json& e, const char* k) { return e[k].get<double>(); }
bool arc_ccw(const json& e) { return e.value("ccw", true); }

std::string require_supported(const json& e) {
    std::string t = etype(e);
    if (t != "line" && t != "circle" && t != "arc") throw UnsupportedEntity(t);
    return t;
}

double angle_of(const V2& c, const V2& p) {
    double a = std::atan2(p[1] - c[1], p[0] - c[0]) * (180.0 / M_PI);
    return pymod(a, 360.0);
}

V2 pt_on_circle(const V2& c, double r, double ang_deg) {
    double a = ang_deg * DEG;
    return {c[0] + r * std::cos(a), c[1] + r * std::sin(a)};
}

std::pair<V2, V2> arc_endpoints(const json& arc) {
    V2 c = pt(arc, "center");
    double r = num(arc, "radius");
    return {pt_on_circle(c, r, num(arc, "start")), pt_on_circle(c, r, num(arc, "end"))};
}

bool arc_sweep_contains(const json& arc, double ang_deg) {
    double a0 = pymod(num(arc, "start"), 360.0);
    double a1 = pymod(num(arc, "end"), 360.0);
    bool ccw = arc_ccw(arc);
    double a = pymod(ang_deg, 360.0);
    double span, rel;
    if (ccw) {
        span = pymod(a1 - a0, 360.0);
        rel = pymod(a - a0, 360.0);
    } else {
        span = pymod(a0 - a1, 360.0);
        rel = pymod(a0 - a, 360.0);
    }
    if (span < TOL) span = 360.0;
    return rel <= span + 1e-6;
}

std::pair<V2, double> circle_params(const json& e) { return {pt(e, "center"), num(e, "radius")}; }

// Intersection result: point + parameter on a + parameter on b.
struct Hit { V2 p; double ta; double tb; };

std::vector<Hit> inter_line_line(const json& la, const json& lb) {
    V2 a1 = pt(la, "p1"), a2 = pt(la, "p2");
    V2 b1 = pt(lb, "p1"), b2 = pt(lb, "p2");
    V2 d1 = sub(a2, a1), d2 = sub(b2, b1);
    double den = cross(d1, d2);
    if (std::abs(den) < TOL) return {};
    V2 diff = sub(b1, a1);
    double t = cross(diff, d2) / den;
    double u = cross(diff, d1) / den;
    V2 p = add(a1, scale(d1, t));
    return {{p, t, u}};
}

std::vector<Hit> inter_line_circle(const json& la, const json& cb) {
    V2 a1 = pt(la, "p1"), a2 = pt(la, "p2");
    auto [c, r] = circle_params(cb);
    V2 d = sub(a2, a1);
    double L2 = dot(d, d);
    if (L2 < TOL * TOL) return {};
    V2 f = sub(a1, c);
    double a = L2;
    double b = 2.0 * dot(f, d);
    double cc = dot(f, f) - r * r;
    double disc = b * b - 4 * a * cc;
    if (disc < -TOL) return {};
    disc = std::max(disc, 0.0);
    double sq = std::sqrt(disc);
    std::vector<Hit> out;
    for (double t : {(-b - sq) / (2 * a), (-b + sq) / (2 * a)}) {
        V2 p = add(a1, scale(d, t));
        out.push_back({p, t, angle_of(c, p)});
    }
    if (out.size() == 2 && vnear(out[0].p, out[1].p)) out.resize(1);
    return out;
}

std::vector<Hit> inter_circle_circle(const json& ca, const json& cb) {
    auto [c0, r0] = circle_params(ca);
    auto [c1, r1] = circle_params(cb);
    double d = vlen(sub(c1, c0));
    if (d < TOL) return {};
    if (d > r0 + r1 + TOL || d < std::abs(r0 - r1) - TOL) return {};
    double a = (r0 * r0 - r1 * r1 + d * d) / (2 * d);
    double h2 = r0 * r0 - a * a;
    double h = std::sqrt(std::max(h2, 0.0));
    V2 u = scale(sub(c1, c0), 1.0 / d);
    V2 mid = add(c0, scale(u, a));
    std::vector<Hit> out;
    for (double sgn : {1.0, -1.0}) {
        V2 p = {mid[0] + sgn * h * (-u[1]), mid[1] + sgn * h * u[0]};
        out.push_back({p, angle_of(c0, p), angle_of(c1, p)});
    }
    if (out.size() == 2 && vnear(out[0].p, out[1].p)) out.resize(1);
    return out;
}

std::vector<Hit> intersect(const json& a, const json& b) {
    std::string ta = require_supported(a), tb = require_supported(b);
    bool a_line = ta == "line", b_line = tb == "line";
    if (a_line && b_line) return inter_line_line(a, b);
    if (a_line && !b_line) return inter_line_circle(a, b);
    if (!a_line && b_line) {
        // swap: line vs circle, report (pt, ang_on_a, t_on_b)
        std::vector<Hit> out;
        for (auto& h : inter_line_circle(b, a)) out.push_back({h.p, h.tb, h.ta});
        return out;
    }
    return inter_circle_circle(a, b);
}

bool param_on_target_in_range(const json& target, double t) {
    std::string et = etype(target);
    if (et == "line") return -TOL <= t && t <= 1.0 + TOL;
    if (et == "circle") return true;
    if (et == "arc") return arc_sweep_contains(target, t);
    return false;
}

// ---- split helpers ----
std::vector<double> dedupe_sorted(std::vector<double> vals, double lo, double hi) {
    std::sort(vals.begin(), vals.end());
    std::vector<double> out;
    for (double v : vals) {
        if (v <= lo + TOL || v >= hi - TOL) continue;
        if (!out.empty() && std::abs(v - out.back()) <= 1e-6) continue;
        out.push_back(v);
    }
    return out;
}

std::vector<double> dedupe_angles(std::vector<double> angs) {
    std::vector<double> out;
    for (double a : angs) {
        if (!out.empty() && std::abs(a - out.back()) <= 1e-6) continue;
        out.push_back(a);
    }
    if (out.size() >= 2 && std::abs((out[0] + 360.0) - out.back()) <= 1e-6) out.pop_back();
    return out;
}

double arc_full_sweep(const json& arc) {
    double a0 = pymod(num(arc, "start"), 360.0);
    double a1 = pymod(num(arc, "end"), 360.0);
    bool ccw = arc_ccw(arc);
    double span = ccw ? pymod(a1 - a0, 360.0) : pymod(a0 - a1, 360.0);
    if (span < TOL) span = 360.0;
    return span;
}

V2 arc_midpoint(const json& arc) {
    auto [c, r] = circle_params(arc);
    double a0 = pymod(num(arc, "start"), 360.0);
    bool ccw = arc_ccw(arc);
    double full = arc_full_sweep(arc);
    double mid = ccw ? (a0 + full / 2.0) : (a0 - full / 2.0);
    return pt_on_circle(c, r, mid);
}

int nearest_span_line(const std::vector<json>& spans, const V2& point) {
    int best = 0;
    double bestd = std::numeric_limits<double>::infinity();
    for (int i = 0; i < (int)spans.size(); ++i) {
        V2 mid = scale(add(pt(spans[i], "p1"), pt(spans[i], "p2")), 0.5);
        double dd = vlen(sub(mid, point));
        if (dd < bestd) { bestd = dd; best = i; }
    }
    return best;
}

int nearest_span_arc(const std::vector<json>& spans, const V2& point) {
    int best = 0;
    double bestd = std::numeric_limits<double>::infinity();
    for (int i = 0; i < (int)spans.size(); ++i) {
        V2 mid = arc_midpoint(spans[i]);
        double dd = vlen(sub(mid, point));
        if (dd < bestd) { bestd = dd; best = i; }
    }
    return best;
}

std::vector<json> drop_index(const std::vector<json>& spans, int drop) {
    std::vector<json> out;
    for (int i = 0; i < (int)spans.size(); ++i)
        if (i != drop) out.push_back(spans[i]);
    return out;
}

// The full partition of `line` at every interior cut, in parameter order. Empty =>
// no interior cut (caller treats the whole entity as doomed). No span is dropped here.
std::vector<json> partition_line(const json& line, const std::vector<double>& cuts) {
    V2 p1 = pt(line, "p1"), p2 = pt(line, "p2");
    std::vector<double> inner = dedupe_sorted(cuts, 0.0, 1.0);
    if (inner.empty()) return {};
    std::vector<double> bounds; bounds.push_back(0.0);
    for (double v : inner) bounds.push_back(v);
    bounds.push_back(1.0);
    V2 d = sub(p2, p1);
    std::vector<json> spans;
    for (std::size_t i = 0; i + 1 < bounds.size(); ++i) {
        V2 a = add(p1, scale(d, bounds[i]));
        V2 b = add(p1, scale(d, bounds[i + 1]));
        spans.push_back({{"type", "line"}, {"p1", {a[0], a[1]}}, {"p2", {b[0], b[1]}}});
    }
    return spans;
}

std::vector<json> partition_circle(const json& circle, const std::vector<double>& cuts) {
    auto [c, r] = circle_params(circle);
    std::set<double> uniq;
    for (double a : cuts) uniq.insert(pymod(a, 360.0));
    std::vector<double> angs(uniq.begin(), uniq.end());
    angs = dedupe_angles(angs);
    if (angs.size() < 2) return {};
    std::vector<json> spans;
    int n = (int)angs.size();
    for (int i = 0; i < n; ++i) {
        double a0 = angs[i], a1 = angs[(i + 1) % n];
        spans.push_back({{"type", "arc"}, {"center", {c[0], c[1]}}, {"radius", r},
                         {"start", a0}, {"end", a1}, {"ccw", true}});
    }
    return spans;
}

std::vector<json> partition_arc(const json& arc, const std::vector<double>& cuts) {
    auto [c, r] = circle_params(arc);
    double a0 = pymod(num(arc, "start"), 360.0);
    bool ccw = arc_ccw(arc);
    double full = arc_full_sweep(arc);
    std::vector<double> rels;
    for (double a : cuts) {
        double rel = ccw ? pymod(a - a0, 360.0) : pymod(a0 - a, 360.0);
        if (TOL < rel && rel < full - TOL) rels.push_back(rel);
    }
    rels = dedupe_sorted(rels, 0.0, full);
    if (rels.empty()) return {};
    std::vector<double> bounds; bounds.push_back(0.0);
    for (double v : rels) bounds.push_back(v);
    bounds.push_back(full);
    std::vector<json> spans;
    for (std::size_t i = 0; i + 1 < bounds.size(); ++i) {
        double r0 = bounds[i], r1 = bounds[i + 1];
        double s = ccw ? pymod(a0 + r0, 360.0) : pymod(a0 - r0, 360.0);
        double e = ccw ? pymod(a0 + r1, 360.0) : pymod(a0 - r1, 360.0);
        spans.push_back({{"type", "arc"}, {"center", {c[0], c[1]}}, {"radius", r},
                         {"start", s}, {"end", e}, {"ccw", ccw}});
    }
    return spans;
}

// Index of the span nearest (px,py) — the one a power-trim click removes.
int doomed_index(const std::string& et, const std::vector<json>& spans, const V2& point) {
    return et == "line" ? nearest_span_line(spans, point) : nearest_span_arc(spans, point);
}

// ---- offset helpers ----
std::pair<V2, V2> entity_ends(const json& e) {
    std::string t = etype(e);
    if (t == "line") return {pt(e, "p1"), pt(e, "p2")};
    if (t == "arc") return arc_endpoints(e);
    auto [c, r] = circle_params(e);
    return {c, c};
}

std::pair<V2, V2> nearest_and_tangent(const json& e, const V2& p) {
    if (etype(e) == "line") {
        V2 p1 = pt(e, "p1"), p2 = pt(e, "p2");
        V2 d = sub(p2, p1);
        double L2 = dot(d, d);
        double t = (L2 < TOL * TOL) ? 0.0 : std::max(0.0, std::min(1.0, dot(sub(p, p1), d) / L2));
        return {add(p1, scale(d, t)), vnorm(d)};
    }
    auto [c, r] = circle_params(e);
    V2 radial = vnorm(sub(p, c));
    if (is_zero(radial)) radial = {1.0, 0.0};
    V2 nearp = add(c, scale(radial, r));
    return {nearp, perp(radial)};
}

double chain_side_sign(const std::vector<json>& entities, const V2& s) {
    double best_d = std::numeric_limits<double>::infinity();
    double best_sign = 1.0;
    for (const json& e : entities) {
        auto [nearest, tangent] = nearest_and_tangent(e, s);
        double d = vlen(sub(s, nearest));
        if (d < best_d) {
            best_d = d;
            V2 left = perp(tangent);
            best_sign = dot(sub(s, nearest), left) >= 0 ? 1.0 : -1.0;
        }
    }
    return best_sign;
}

json offset_one(const json& e, double distance, double side) {
    if (etype(e) == "line") {
        V2 p1 = pt(e, "p1"), p2 = pt(e, "p2");
        V2 nrm = vnorm(perp(sub(p2, p1)));
        V2 off = scale(nrm, side * distance);
        return {{"type", "line"}, {"p1", {p1[0] + off[0], p1[1] + off[1]}},
                {"p2", {p2[0] + off[0], p2[1] + off[1]}}};
    }
    auto [c, r] = circle_params(e);
    double ccw_sign = arc_ccw(e) ? 1.0 : -1.0;
    double new_r = std::max(r - side * ccw_sign * distance, TOL);
    json out = e;
    out["radius"] = new_r;
    out["center"] = {c[0], c[1]};
    return out;
}

void set_end(json& e, const std::string& which, const V2& p) {
    if (etype(e) == "line") {
        e[which == "start" ? "p1" : "p2"] = {p[0], p[1]};
    } else if (etype(e) == "arc") {
        auto [c, r] = circle_params(e);
        double ang = angle_of(c, p);
        e[which == "start" ? "start" : "end"] = pymod(ang, 360.0);
    }
}

void join_at_intersection(json& a, json& b) {
    std::vector<Hit> inters = intersect(a, b);
    V2 a_end = entity_ends(a).second;
    V2 b_start = entity_ends(b).first;
    V2 joint = scale(add(a_end, b_start), 0.5);
    V2 ptv;
    if (!inters.empty()) {
        std::sort(inters.begin(), inters.end(), [&](const Hit& x, const Hit& y) {
            return vlen(sub(x.p, joint)) < vlen(sub(y.p, joint));
        });
        ptv = inters[0].p;
    } else {
        ptv = joint;
    }
    set_end(a, "end", ptv);
    set_end(b, "start", ptv);
}

// ---- fillet/chamfer helpers ----
V2 far_end(const json& line, const std::string& corner_end) {
    return corner_end == "start" ? pt(line, "p2") : pt(line, "p1");
}

struct Corner { V2 corner; std::string end_a; V2 dir_a; std::string end_b; V2 dir_b; };

Corner line_corner(const json& la, const json& lb) {
    if (etype(la) != "line" || etype(lb) != "line")
        throw GeomError("fillet/chamfer needs two line segments");
    V2 a1 = pt(la, "p1"), a2 = pt(la, "p2");
    V2 b1 = pt(lb, "p1"), b2 = pt(lb, "p2");
    V2 da = sub(a2, a1), db = sub(b2, b1);
    if (vlen(da) < TOL || vlen(db) < TOL) throw GeomError("degenerate (zero-length) segment");
    if (std::abs(cross(vnorm(da), vnorm(db))) < 1e-9)
        throw GeomError("segments are parallel — no corner to round");
    std::vector<Hit> inter = inter_line_line(la, lb);
    if (inter.empty()) throw GeomError("segments do not meet at a corner");
    V2 corner = inter[0].p;

    auto pick = [&](const V2& p_start, const V2& p_end) -> std::pair<std::string, V2> {
        double ds = vlen(sub(p_start, corner));
        double de = vlen(sub(p_end, corner));
        if (ds <= de) return {"start", vnorm(sub(p_end, corner))};
        return {"end", vnorm(sub(p_start, corner))};
    };
    auto [end_a, dir_a] = pick(a1, a2);
    auto [end_b, dir_b] = pick(b1, b2);
    if (is_zero(dir_a) || is_zero(dir_b)) throw GeomError("segments do not meet at a corner");
    return {corner, end_a, dir_a, end_b, dir_b};
}

}  // namespace

// ----------------------------------------------------------------- public API
SplitResult split_entity_ex(const json& target, const std::vector<json>& others,
                            double px, double py) {
    std::string et = require_supported(target);
    for (const json& o : others) require_supported(o);
    std::vector<double> cuts;
    for (const json& o : others) {
        for (const Hit& h : intersect(target, o)) {
            if (!param_on_target_in_range(o, h.tb)) continue;
            if (!param_on_target_in_range(target, h.ta)) continue;
            cuts.push_back(h.ta);
        }
    }
    V2 point = {px, py};
    std::vector<json> spans = et == "line"   ? partition_line(target, cuts)
                            : et == "circle" ? partition_circle(target, cuts)
                                             : partition_arc(target, cuts);
    SplitResult res;
    if (spans.empty()) {           // no interior cut => the whole entity is doomed
        res.whole = true;
        res.removed = target;
        return res;
    }
    int doomed = doomed_index(et, spans, point);
    for (int i = 0; i < (int)spans.size(); ++i) {
        if (i == doomed) res.removed = spans[i];
        else res.survivors.push_back(spans[i]);
    }
    return res;
}

std::vector<json> split_entity(const json& target, const std::vector<json>& others,
                               double px, double py) {
    return split_entity_ex(target, others, px, py).survivors;
}

std::optional<json> extend_entity(const json& target, const std::string& end,
                                  const std::vector<json>& others) {
    std::string et = require_supported(target);
    if (et == "circle") throw UnsupportedEntity("circle");
    if (et == "line") {
        V2 p1 = pt(target, "p1"), p2 = pt(target, "p2");
        V2 anchor_moving, d;
        V2 moving;
        if (end == "start") { moving = p1; d = vnorm(sub(p1, p2)); }
        else { moving = p2; d = vnorm(sub(p2, p1)); }
        if (is_zero(d)) return std::nullopt;
        std::optional<double> best_s;
        std::optional<V2> best_pt;
        for (const json& o : others) {
            require_supported(o);
            json ray = {{"type", "line"}, {"p1", {moving[0], moving[1]}},
                        {"p2", {moving[0] + d[0], moving[1] + d[1]}}};
            for (const Hit& h : intersect(ray, o)) {
                if (h.ta <= TOL) continue;
                if (!param_on_target_in_range(o, h.tb)) continue;
                if (!best_s || h.ta < *best_s) { best_s = h.ta; best_pt = h.p; }
            }
        }
        if (!best_pt) return std::nullopt;
        json out = target;
        if (end == "start") out["p1"] = {(*best_pt)[0], (*best_pt)[1]};
        else out["p2"] = {(*best_pt)[0], (*best_pt)[1]};
        return out;
    }
    // arc
    auto [c, r] = circle_params(target);
    bool ccw = arc_ccw(target);
    double base_ang;
    bool grow_ccw;
    if (end == "start") { base_ang = pymod(num(target, "start"), 360.0); grow_ccw = !ccw; }
    else { base_ang = pymod(num(target, "end"), 360.0); grow_ccw = ccw; }
    json circle = {{"type", "circle"}, {"center", {c[0], c[1]}}, {"radius", r}};
    std::optional<double> best_rel;
    std::optional<double> best_ang;
    for (const json& o : others) {
        require_supported(o);
        for (const Hit& h : intersect(circle, o)) {
            if (!param_on_target_in_range(o, h.tb)) continue;
            double ang = angle_of(c, h.p);
            double rel = grow_ccw ? pymod(ang - base_ang, 360.0) : pymod(base_ang - ang, 360.0);
            if (rel <= TOL) continue;
            if (!best_rel || rel < *best_rel) { best_rel = rel; best_ang = ang; }
        }
    }
    if (!best_ang) return std::nullopt;
    json out = target;
    if (end == "start") out["start"] = pymod(*best_ang, 360.0);
    else out["end"] = pymod(*best_ang, 360.0);
    return out;
}

bool chain_is_closed(const std::vector<json>& entities) {
    if (entities.size() < 2) return false;
    V2 first_s = entity_ends(entities.front()).first;
    V2 last_e = entity_ends(entities.back()).second;
    return vnear(first_s, last_e, 1e-5);
}

std::vector<json> offset_chain(const std::vector<json>& entities, double distance,
                               double sx, double sy) {
    for (const json& e : entities) require_supported(e);
    if (entities.empty()) return {};
    bool closed = chain_is_closed(entities);
    double side = chain_side_sign(entities, {sx, sy});
    std::vector<json> raws;
    for (const json& e : entities) raws.push_back(offset_one(e, distance, side));
    int n = (int)raws.size();
    if (n == 1) return {raws[0]};
    int pairs = closed ? n : n - 1;
    for (int i = 0; i < pairs; ++i) join_at_intersection(raws[i], raws[(i + 1) % n]);
    return raws;
}

json fillet_corner(const json& la, const json& lb, double radius) {
    if (radius <= TOL) throw GeomError("fillet radius must be positive");
    Corner co = line_corner(la, lb);
    double cos_t = std::max(-1.0, std::min(1.0, dot(co.dir_a, co.dir_b)));
    double theta = std::acos(cos_t);
    double half = theta / 2.0;
    if (half < 1e-6 || half > M_PI / 2 - 1e-9)
        throw GeomError("corner angle too shallow to fillet");
    double setback = radius / std::tan(half);
    double far_a = vlen(sub(co.corner, far_end(la, co.end_a)));
    double far_b = vlen(sub(co.corner, far_end(lb, co.end_b)));
    if (setback > far_a - TOL || setback > far_b - TOL)
        throw GeomError("radius too large for this corner");
    V2 ta = add(co.corner, scale(co.dir_a, setback));
    V2 tb = add(co.corner, scale(co.dir_b, setback));
    V2 bis = vnorm(add(co.dir_a, co.dir_b));
    if (is_zero(bis)) throw GeomError("corner angle too shallow to fillet");
    V2 center = add(co.corner, scale(bis, radius / std::sin(half)));
    double start_ang = angle_of(center, ta);
    double end_ang = angle_of(center, tb);
    double ccw_sweep = pymod(end_ang - start_ang, 360.0);
    bool ccw = ccw_sweep <= 180.0;
    json arc = {{"type", "arc"}, {"center", {center[0], center[1]}}, {"radius", radius},
                {"start", pymod(start_ang, 360.0)}, {"end", pymod(end_ang, 360.0)}, {"ccw", ccw}};
    return {{"ta", {ta[0], ta[1]}}, {"tb", {tb[0], tb[1]}}, {"arc", arc},
            {"end_a", co.end_a}, {"end_b", co.end_b}};
}

json chamfer_corner(const json& la, const json& lb, double dist_a, double dist_b) {
    if (dist_a <= TOL || dist_b <= TOL) throw GeomError("chamfer setback must be positive");
    Corner co = line_corner(la, lb);
    double far_a = vlen(sub(co.corner, far_end(la, co.end_a)));
    double far_b = vlen(sub(co.corner, far_end(lb, co.end_b)));
    if (dist_a > far_a - TOL || dist_b > far_b - TOL)
        throw GeomError("setback too large for this corner");
    V2 ta = add(co.corner, scale(co.dir_a, dist_a));
    V2 tb = add(co.corner, scale(co.dir_b, dist_b));
    return {{"ta", {ta[0], ta[1]}}, {"tb", {tb[0], tb[1]}},
            {"end_a", co.end_a}, {"end_b", co.end_b}};
}

}  // namespace chudware::doc::sketchgeom
