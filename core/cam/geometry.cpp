// core/cam/geometry.cpp — 2D machining geometry over Clipper2 (Boost license).
#define _USE_MATH_DEFINES  // MSVC needs this before <cmath> for M_PI (mirrors geom/plane.cpp)
#include "cam/geometry.h"

#include <algorithm>
#include <cmath>

#include "clipper2/clipper.h"

namespace chudware {
namespace cam {
namespace geo {

namespace cl = Clipper2Lib;

// ── scaling conversions ──────────────────────────────────────────────────────
static cl::Path64 to64(const Path2& p) {
    cl::Path64 o;
    o.reserve(p.size());
    for (auto& v : p) o.push_back(cl::Point64((int64_t)std::llround(v.x * kScale),
                                              (int64_t)std::llround(v.y * kScale)));
    return o;
}
static Path2 from64(const cl::Path64& p) {
    Path2 o;
    o.reserve(p.size());
    for (auto& pt : p) o.push_back({(double)pt.x / kScale, (double)pt.y / kScale});
    return o;
}
static cl::JoinType jt_cl(JoinType j) {
    switch (j) {
        case JoinType::Square: return cl::JoinType::Square;
        case JoinType::Miter: return cl::JoinType::Miter;
        default: return cl::JoinType::Round;
    }
}

// ── basic measures ───────────────────────────────────────────────────────────
double signed_area(const Path2& p) {
    double a = 0;
    size_t n = p.size();
    for (size_t i = 0; i < n; ++i) {
        const Vec2& u = p[i];
        const Vec2& v = p[(i + 1) % n];
        a += u.x * v.y - v.x * u.y;
    }
    return 0.5 * a;
}
double area(const Path2& p) { return std::fabs(signed_area(p)); }
double perimeter(const Path2& p, bool closed) {
    double L = 0;
    size_t n = p.size();
    if (n < 2) return 0;
    for (size_t i = 0; i + 1 < n; ++i) L += std::hypot(p[i + 1].x - p[i].x, p[i + 1].y - p[i].y);
    if (closed) L += std::hypot(p.front().x - p.back().x, p.front().y - p.back().y);
    return L;
}
bool is_ccw(const Path2& p) { return signed_area(p) > 0; }
Path2 ensure_ccw(const Path2& p) {
    if (is_ccw(p)) return p;
    Path2 r(p.rbegin(), p.rend());
    return r;
}
Path2 ensure_cw(const Path2& p) {
    if (!is_ccw(p)) return p;
    Path2 r(p.rbegin(), p.rend());
    return r;
}
bool point_in_polygon(const Vec2& q, const Path2& p) {
    bool in = false;
    size_t n = p.size();
    for (size_t i = 0, j = n - 1; i < n; j = i++) {
        const Vec2& a = p[i];
        const Vec2& b = p[j];
        if (((a.y > q.y) != (b.y > q.y)) &&
            (q.x < (b.x - a.x) * (q.y - a.y) / (b.y - a.y) + a.x))
            in = !in;
    }
    return in;
}
bool point_in_region(const Vec2& q, const Region& r) {
    if (!point_in_polygon(q, r.outer)) return false;
    for (auto& h : r.holes) if (point_in_polygon(q, h)) return false;
    return true;
}
void bbox(const Path2& p, Vec2& lo, Vec2& hi) {
    lo = {1e300, 1e300};
    hi = {-1e300, -1e300};
    for (auto& v : p) {
        lo.x = std::min(lo.x, v.x); lo.y = std::min(lo.y, v.y);
        hi.x = std::max(hi.x, v.x); hi.y = std::max(hi.y, v.y);
    }
    if (p.empty()) { lo = {0, 0}; hi = {0, 0}; }
}
void region_bbox(const std::vector<Region>& rs, Vec2& lo, Vec2& hi) {
    lo = {1e300, 1e300};
    hi = {-1e300, -1e300};
    bool any = false;
    for (auto& r : rs) {
        Vec2 l, h;
        bbox(r.outer, l, h);
        if (r.outer.empty()) continue;
        any = true;
        lo.x = std::min(lo.x, l.x); lo.y = std::min(lo.y, l.y);
        hi.x = std::max(hi.x, h.x); hi.y = std::max(hi.y, h.y);
    }
    if (!any) { lo = {0, 0}; hi = {0, 0}; }
}
double region_area(const Region& r) {
    double a = area(r.outer);
    for (auto& h : r.holes) a -= area(h);
    return std::max(0.0, a);
}

// ── resampling / cleanup ─────────────────────────────────────────────────────
Path2 resample_max_seg(const Path2& p, double max_seg) {
    if (p.size() < 2 || max_seg <= 0) return p;
    Path2 out;
    for (size_t i = 0; i + 1 < p.size(); ++i) {
        const Vec2& a = p[i];
        const Vec2& b = p[i + 1];
        out.push_back(a);
        double d = std::hypot(b.x - a.x, b.y - a.y);
        int n = (int)std::ceil(d / max_seg);
        for (int k = 1; k < n; ++k) {
            double t = (double)k / n;
            out.push_back({a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t});
        }
    }
    out.push_back(p.back());
    return out;
}
Path2 simplify(const Path2& p, double tol) {
    if (p.size() < 3) return p;
    Path2 out;
    for (size_t i = 0; i < p.size(); ++i) {
        if (!out.empty()) {
            double d = std::hypot(p[i].x - out.back().x, p[i].y - out.back().y);
            if (d < tol) continue;
        }
        out.push_back(p[i]);
    }
    // drop collinear interior points within tol
    if (out.size() >= 3) {
        Path2 r;
        for (size_t i = 0; i < out.size(); ++i) {
            const Vec2& a = out[(i + out.size() - 1) % out.size()];
            const Vec2& b = out[i];
            const Vec2& c = out[(i + 1) % out.size()];
            double cross = (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
            double base = std::hypot(c.x - a.x, c.y - a.y);
            if (base > 1e-9 && std::fabs(cross) / base < tol) continue;
            r.push_back(b);
        }
        if (r.size() >= 3) out.swap(r);
    }
    return out;
}
double polyline_length(const Path2& p, bool closed) { return perimeter(p, closed); }

// ── Region <-> Paths64 grouping ──────────────────────────────────────────────
static cl::Paths64 region_to_64(const Region& r) {
    cl::Paths64 ps;
    ps.push_back(to64(ensure_ccw(r.outer)));
    for (auto& h : r.holes) ps.push_back(to64(ensure_cw(h)));
    return ps;
}
static cl::Paths64 regions_to_64(const std::vector<Region>& rs) {
    cl::Paths64 ps;
    for (auto& r : rs) {
        auto a = region_to_64(r);
        ps.insert(ps.end(), a.begin(), a.end());
    }
    return ps;
}
static std::vector<Region> regions_from_64(const cl::Paths64& ps) {
    struct Loop { Path2 pts; double sa; };
    std::vector<Loop> outers, holes;
    for (auto& p : ps) {
        if (p.size() < 3) continue;
        Path2 pp = from64(p);
        double sa = signed_area(pp);
        if (std::fabs(sa) < 1e-9) continue;
        (sa > 0 ? outers : holes).push_back({pp, sa});
    }
    std::vector<Region> out;
    out.reserve(outers.size());
    for (auto& o : outers) out.push_back({o.pts, {}});
    for (auto& h : holes) {
        int best = -1;
        double best_area = 1e300;
        for (size_t i = 0; i < outers.size(); ++i) {
            if (point_in_polygon(h.pts[0], outers[i].pts)) {
                double a = std::fabs(outers[i].sa);
                if (a < best_area) { best_area = a; best = (int)i; }
            }
        }
        if (best >= 0) out[best].holes.push_back(h.pts);
    }
    return out;
}

// ── offset / boolean ─────────────────────────────────────────────────────────
std::vector<Region> offset_regions(const std::vector<Region>& in, double delta, JoinType jt,
                                   double miter, double arc_tol) {
    if (in.empty()) return {};
    cl::Paths64 src = regions_to_64(in);
    cl::Paths64 res = cl::InflatePaths(src, delta * kScale, jt_cl(jt), cl::EndType::Polygon,
                                       miter, arc_tol * kScale);
    // clean orientation/self-intersections via a union pass
    res = cl::Union(res, cl::FillRule::NonZero);
    return regions_from_64(res);
}
std::vector<Region> offset_region(const Region& r, double delta, JoinType jt, double miter,
                                  double arc_tol) {
    return offset_regions({r}, delta, jt, miter, arc_tol);
}
std::vector<Path2> offset_open(const std::vector<Path2>& paths, double delta, JoinType jt,
                               bool round_caps, double arc_tol) {
    cl::Paths64 src;
    for (auto& p : paths) if (p.size() >= 2) src.push_back(to64(p));
    if (src.empty()) return {};
    cl::EndType et = round_caps ? cl::EndType::Round : cl::EndType::Butt;
    cl::Paths64 res = cl::InflatePaths(src, delta * kScale, jt_cl(jt), et, 2.0, arc_tol * kScale);
    std::vector<Path2> out;
    for (auto& p : res) out.push_back(from64(p));
    return out;
}
std::vector<Region> boolean_op(ClipOp op, const std::vector<Region>& subjects,
                               const std::vector<Region>& clips, bool even_odd) {
    cl::Paths64 s = regions_to_64(subjects);
    cl::Paths64 c = regions_to_64(clips);
    cl::FillRule fr = even_odd ? cl::FillRule::EvenOdd : cl::FillRule::NonZero;
    cl::Paths64 res;
    switch (op) {
        case ClipOp::Union: res = c.empty() ? cl::Union(s, fr) : cl::Union(cl::Paths64(s.begin(), s.end()), c, fr); break;
        case ClipOp::Difference: res = cl::Difference(s, c, fr); break;
        case ClipOp::Intersection: res = cl::Intersect(s, c, fr); break;
        case ClipOp::Xor: res = cl::Xor(s, c, fr); break;
    }
    return regions_from_64(res);
}

// ── pocket / area clearing ───────────────────────────────────────────────────
std::vector<Region> machinable_region(const Region& r, double tool_radius, double stock_to_leave) {
    return offset_region(r, -(tool_radius + stock_to_leave));
}
std::vector<std::vector<Path2>> pocket_offsets(const Region& r, double tool_radius, double stepover,
                                               double stock_to_leave, JoinType jt) {
    std::vector<std::vector<Path2>> levels;
    if (stepover <= 0) stepover = tool_radius;  // safety
    double inset = tool_radius + stock_to_leave;
    int guard = 0;
    while (guard++ < 100000) {
        std::vector<Region> rs = offset_region(r, -inset, jt);
        if (rs.empty()) break;
        std::vector<Path2> loops;
        for (auto& rr : rs) {
            if (rr.outer.size() >= 3) loops.push_back(rr.outer);
            for (auto& h : rr.holes) if (h.size() >= 3) loops.push_back(h);
        }
        if (loops.empty()) break;
        levels.push_back(std::move(loops));
        inset += stepover;
    }
    return levels;
}

// ── raster fill ──────────────────────────────────────────────────────────────
static Vec2 rot(const Vec2& v, double c, double s) { return {v.x * c - v.y * s, v.x * s + v.y * c}; }

std::vector<Path2> raster_fill(const std::vector<Region>& regions, double spacing, double angle_deg,
                               bool bidirectional) {
    if (regions.empty() || spacing <= 0) return {};
    // union the regions so overlaps don't double-count crossings
    std::vector<Region> u = boolean_op(ClipOp::Union, regions, {}, false);
    if (u.empty()) u = regions;

    double th = angle_deg * M_PI / 180.0;
    double c = std::cos(-th), s = std::sin(-th);
    double cb = std::cos(th), sb = std::sin(th);

    // gather rotated edges + bbox
    struct Edge { Vec2 a, b; };
    std::vector<Edge> edges;
    Vec2 lo{1e300, 1e300}, hi{-1e300, -1e300};
    auto add_loop = [&](const Path2& loop) {
        size_t n = loop.size();
        for (size_t i = 0; i < n; ++i) {
            Vec2 a = rot(loop[i], c, s);
            Vec2 b = rot(loop[(i + 1) % n], c, s);
            edges.push_back({a, b});
            for (Vec2* p : {&a, &b}) {
                lo.x = std::min(lo.x, p->x); lo.y = std::min(lo.y, p->y);
                hi.x = std::max(hi.x, p->x); hi.y = std::max(hi.y, p->y);
            }
        }
    };
    for (auto& r : u) { add_loop(r.outer); for (auto& h : r.holes) add_loop(h); }
    if (edges.empty()) return {};

    std::vector<Path2> strokes;
    int row = 0;
    for (double y = lo.y + spacing * 0.5; y < hi.y; y += spacing, ++row) {
        std::vector<double> xs;
        for (auto& e : edges) {
            double y0 = e.a.y, y1 = e.b.y;
            if ((y0 <= y && y < y1) || (y1 <= y && y < y0)) {
                double t = (y - y0) / (y1 - y0);
                xs.push_back(e.a.x + (e.b.x - e.a.x) * t);
            }
        }
        if (xs.size() < 2) continue;
        std::sort(xs.begin(), xs.end());
        std::vector<std::pair<double, double>> spans;
        for (size_t i = 0; i + 1 < xs.size(); i += 2) spans.push_back({xs[i], xs[i + 1]});
        bool rev = bidirectional && (row & 1);
        if (rev) std::reverse(spans.begin(), spans.end());
        for (auto& sp : spans) {
            double x0 = sp.first, x1 = sp.second;
            if (x1 - x0 < 1e-6) continue;
            if (rev) std::swap(x0, x1);
            Path2 stroke = {rot({x0, y}, cb, sb), rot({x1, y}, cb, sb)};
            strokes.push_back(std::move(stroke));
        }
    }
    return strokes;
}

// ── lead geometry ────────────────────────────────────────────────────────────
static Vec2 normd(const Vec2& v) {
    double n = std::hypot(v.x, v.y);
    return n > 1e-12 ? Vec2{v.x / n, v.y / n} : Vec2{1, 0};
}
Path2 lead_in_path(const Vec2& p0, const Vec2& dir, const Lead& l, CompSide side, bool climb) {
    Path2 out;
    if (l.type == LeadType::None) return out;
    Vec2 d = normd(dir);
    // side normal: for climb on outside, lead approaches from the offset side
    int sgn = (side == CompSide::Right) ? -1 : 1;
    if (!climb) sgn = -sgn;
    Vec2 nrm{-d.y * sgn, d.x * sgn};
    if (l.type == LeadType::Line || l.type == LeadType::Tangent) {
        Vec2 start{p0.x - d.x * l.length, p0.y - d.y * l.length};
        out.push_back(start);
        return out;
    }
    // arc lead: centre offset along normal by radius; sweep `angle` ending tangent at p0
    double R = l.radius > 0 ? l.radius : 2.0;
    Vec2 center{p0.x + nrm.x * R, p0.y + nrm.y * R};
    double a_end = std::atan2(p0.y - center.y, p0.x - center.x);
    double sweep = l.angle * M_PI / 180.0;
    int steps = std::max(4, (int)std::ceil(l.angle / 10.0));
    // direction of sweep so the arc arrives tangent heading +d
    double dir_sign = (nrm.x * d.y - nrm.y * d.x) > 0 ? 1.0 : -1.0;
    for (int i = 0; i <= steps; ++i) {
        double a = a_end - dir_sign * sweep * (1.0 - (double)i / steps);
        out.push_back({center.x + R * std::cos(a), center.y + R * std::sin(a)});
    }
    if (!out.empty()) out.pop_back();  // exclude p0 itself
    return out;
}
Path2 lead_out_path(const Vec2& p1, const Vec2& dir, const Lead& l, CompSide side, bool climb) {
    // mirror of lead_in, departing tangent from p1 heading +dir
    Path2 in = lead_in_path(p1, {-dir.x, -dir.y}, l, side, climb);
    std::reverse(in.begin(), in.end());
    return in;
}

// ── arc fitting (conservative: emit lines) ───────────────────────────────────
std::vector<ArcSeg> fit_arcs(const Path2& p, double /*tol*/) {
    std::vector<ArcSeg> out;
    for (size_t i = 1; i < p.size(); ++i) out.push_back({false, p[i], {0, 0}, false});
    return out;
}

}  // namespace geo
}  // namespace cam
}  // namespace chudware
