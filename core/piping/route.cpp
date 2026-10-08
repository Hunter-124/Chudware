// core/piping/route.cpp — piping_route: bend-penalised orthogonal A* pipe
// routing around AABB obstacles (see route.h).
#include "piping/route.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <queue>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "piping/catalog.h"
#include "piping/schema.h"

namespace chudware::piping {
namespace {

using pyutil::jerr;
using pyutil::pyround;
using V3 = std::array<double, 3>;

constexpr double kTol = 1e-6;          // mm: coordinate identity / strictness
constexpr size_t kMaxNodes = 1000000;  // grid-node cap; coarser grid beyond
constexpr int kMaxCoarsen = 12;

struct Box {
    V3 lo{};
    V3 hi{};
    std::string name;
};

// Axis directions: 0 +x, 1 -x, 2 +y, 3 -y, 4 +z, 5 -z.
constexpr V3 kDirs[6] = {{1, 0, 0}, {-1, 0, 0}, {0, 1, 0}, {0, -1, 0}, {0, 0, 1}, {0, 0, -1}};
constexpr const char* kDirNames[6] = {"+X", "-X", "+Y", "-Y", "+Z", "-Z"};
int opposite(int d) { return d ^ 1; }

V3 add(const V3& a, const V3& b) { return {a[0] + b[0], a[1] + b[1], a[2] + b[2]}; }
V3 sub(const V3& a, const V3& b) { return {a[0] - b[0], a[1] - b[1], a[2] - b[2]}; }
V3 scale(const V3& a, double s) { return {a[0] * s, a[1] * s, a[2] * s}; }
double dot(const V3& a, const V3& b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
double norm(const V3& a) { return std::sqrt(dot(a, a)); }
double dist(const V3& a, const V3& b) { return norm(sub(a, b)); }
V3 unit(const V3& a) {
    const double n = norm(a);
    return n > 0 ? scale(a, 1.0 / n) : a;
}
V3 cross(const V3& a, const V3& b) {
    return {a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]};
}

bool read_v3(const ojson& v, V3& out) {
    if (!v.is_array() || v.size() != 3) return false;
    for (size_t i = 0; i < 3; ++i) {
        if (!v[i].is_number() || v[i].is_boolean()) return false;
        out[i] = v[i].get<double>();
        if (!std::isfinite(out[i])) return false;
    }
    return true;
}

ojson v3_json(const V3& p) {
    ojson a = ojson::array();
    for (double c : p) {
        const double r = pyround(c, 3);
        a.push_back(r == 0.0 ? 0.0 : r);  // no "-0.0" in documents
    }
    return a;
}

bool strictly_inside(const Box& b, const V3& p) {
    for (int a = 0; a < 3; ++a)
        if (!(p[a] > b.lo[a] + kTol && p[a] < b.hi[a] - kTol)) return false;
    return true;
}

// Does the closed segment p-q meet the open interior of b? Exact for
// axis-aligned segments (all route segments are).
bool seg_hits_open(const V3& p, const V3& q, const Box& b) {
    for (int a = 0; a < 3; ++a) {
        const double lo = std::min(p[a], q[a]);
        const double hi = std::max(p[a], q[a]);
        if (hi - lo < kTol) {
            if (!(lo > b.lo[a] + kTol && lo < b.hi[a] - kTol)) return false;
        } else if (std::min(hi, b.hi[a]) - std::max(lo, b.lo[a]) <= kTol) {
            return false;
        }
    }
    return true;
}

// Euclidean distance between segment p-q and box b (exact for axis-aligned
// segments: only one axis interval is non-degenerate).
double seg_box_dist(const V3& p, const V3& q, const Box& b) {
    double s = 0.0;
    for (int a = 0; a < 3; ++a) {
        const double lo = std::min(p[a], q[a]);
        const double hi = std::max(p[a], q[a]);
        const double gap = std::max({0.0, b.lo[a] - hi, lo - b.hi[a]});
        s += gap * gap;
    }
    return std::sqrt(s);
}

bool boxes_overlap(const Box& b, const V3& lo, const V3& hi) {
    for (int a = 0; a < 3; ++a)
        if (b.hi[a] < lo[a] || b.lo[a] > hi[a]) return false;
    return true;
}

// A nozzle stub: from p along axis direction d for at least min_len, extended
// until the tip clears every inflated box. The stub may cross only the
// inflated envelopes that already contain p (the nozzle's own clearance
// zone) and never the inside of a solid.
struct StubTry {
    bool ok = false;
    double len = 0.0;
    std::vector<size_t> escaped;
    std::string blocker;
};

StubTry stub_along(const V3& p, int d, double min_len, const std::vector<Box>& raw,
                   const std::vector<Box>& infl) {
    StubTry r;
    for (size_t i = 0; i < infl.size(); ++i)
        if (strictly_inside(infl[i], p)) r.escaped.push_back(i);
    const int a = d / 2;
    const bool pos = (d % 2) == 0;
    double t = min_len;
    for (size_t iter = 0; iter <= infl.size(); ++iter) {
        const V3 q = add(p, scale(kDirs[d], t));
        bool moved = false;
        for (const Box& b : infl) {
            if (!strictly_inside(b, q)) continue;
            const double exit = pos ? b.hi[a] - p[a] : p[a] - b.lo[a];
            if (exit > t) {
                t = exit;
                moved = true;
            }
        }
        if (!moved) break;
    }
    r.len = t;
    const V3 tip = add(p, scale(kDirs[d], t));
    for (const Box& b : raw) {
        if (seg_hits_open(p, tip, b)) {
            r.blocker = b.name;
            return r;
        }
    }
    for (size_t i = 0; i < infl.size(); ++i) {
        if (std::find(r.escaped.begin(), r.escaped.end(), i) != r.escaped.end()) continue;
        if (seg_hits_open(p, tip, infl[i]) || strictly_inside(infl[i], tip)) {
            r.blocker = infl[i].name;
            return r;
        }
    }
    r.ok = true;
    return r;
}

struct Stub {
    int dir = -1;  // axis direction index, -1 = free endpoint (no stub)
    V3 tip{};
    std::vector<size_t> escaped;
};

// Sorted unique axis coordinates; values within kTol merge, keeping a
// "special" (endpoint / obstacle face) value over a uniform grid line.
std::vector<double> merge_coords(std::vector<std::pair<double, bool>> v) {
    std::sort(v.begin(), v.end());
    std::vector<double> out;
    bool last_special = false;
    for (const auto& [c, special] : v) {
        if (!out.empty() && c - out.back() <= kTol) {
            if (special && !last_special) {
                out.back() = c;
                last_special = true;
            }
            continue;
        }
        out.push_back(c);
        last_special = special;
    }
    return out;
}

long find_coord(const std::vector<double>& cs, double v) {
    auto it = std::lower_bound(cs.begin(), cs.end(), v - kTol);
    if (it == cs.end() || std::abs(*it - v) > kTol) return -1;
    return static_cast<long>(it - cs.begin());
}

}  // namespace

std::array<double, 3> elbow_orient(const std::array<double, 3>& d_in,
                                   const std::array<double, 3>& d_out) {
    const V3 c0 = unit(d_in);
    const V3 c1 = unit(d_out);
    const V3 c2 = cross(c0, c1);
    // R columns are the images of +X, +Y, +Z; R = Rx(a)·Ry(b)·Rz(c).
    const double R[3][3] = {{c0[0], c1[0], c2[0]}, {c0[1], c1[1], c2[1]}, {c0[2], c1[2], c2[2]}};
    const double sb = std::clamp(R[0][2], -1.0, 1.0);
    const double b = std::asin(sb);
    double a = 0.0;
    double c = 0.0;
    if (std::cos(b) > 1e-9) {
        a = std::atan2(-R[1][2], R[2][2]);
        c = std::atan2(-R[0][1], R[0][0]);
    } else {
        c = std::atan2(R[1][0], R[1][1]);
    }
    constexpr double kDeg = 180.0 / 3.14159265358979323846;
    std::array<double, 3> out = {a * kDeg, b * kDeg, c * kDeg};
    for (double& v : out) {
        v = pyround(v, 6);
        if (v == 0.0) v = 0.0;
    }
    return out;
}

ojson piping_route(const ojson& params) {
    if (!params.is_object()) {
        return jerr("invalid_params", "pass {start:{point}, end:{point}, nps, obstacles, ...}");
    }
    std::vector<std::string> warnings;

    // ── endpoints + pipe size ───────────────────────────────────────────────
    const ojson start_j = params.value("start", ojson(nullptr));
    const ojson end_j = params.value("end", ojson(nullptr));
    V3 ps{};
    V3 pe{};
    if (!start_j.is_object() || !read_v3(start_j.value("point", ojson(nullptr)), ps)) {
        return jerr("invalid_params", "start.point must be [x, y, z] in mm");
    }
    if (!end_j.is_object() || !read_v3(end_j.value("point", ojson(nullptr)), pe)) {
        return jerr("invalid_params", "end.point must be [x, y, z] in mm");
    }
    if (dist(ps, pe) < kTol) return jerr("invalid_params", "start and end points coincide");

    const ojson nps_j = params.value("nps", ojson(nullptr));
    if (!nps_j.is_number() || nps_j.is_boolean()) {
        return jerr("invalid_params", "nps (nominal pipe size, inches) is required");
    }
    const double nps = nps_j.get<double>();
    std::string schedule = "40";
    if (params.contains("schedule") && !params["schedule"].is_null()) {
        schedule = pyutil::py_str(params["schedule"]);
    }
    const ojson pipe = nearest_pipe(nps_j, schedule);
    if (!pipe.value("ok", false)) {
        return jerr("unknown_pipe_size", pipe.value("reason", std::string()) + "; " +
                                             pipe.value("hint", std::string()));
    }
    const double od = pipe["outer_dia"].get<double>();

    // B16.9 90° LR elbow centre-to-end (A = 1.5·NPS where uncatalogued).
    double cte = 1.5 * nps * 25.4;
    const ojson ed = fitting_dims("elbow", nps);
    if (ed.value("center_to_end", ojson(nullptr)).is_number()) {
        cte = ed["center_to_end"].get<double>();
    }

    auto opt_num = [&](const char* key, double dflt, double& out) -> bool {
        if (!params.contains(key) || params[key].is_null()) {
            out = dflt;
            return true;
        }
        if (!params[key].is_number() || params[key].is_boolean()) return false;
        out = params[key].get<double>();
        return std::isfinite(out);
    };
    double clearance = 0.0;
    if (!opt_num("clearance_mm", 50.0, clearance) || clearance < 0) {
        return jerr("invalid_params", "clearance_mm must be a number >= 0");
    }
    double grid = 0.0;
    if (!opt_num("grid_mm", std::max(od, 25.0), grid) || grid <= 0) {
        return jerr("invalid_params", "grid_mm must be a positive number");
    }
    double bend = 0.0;
    if (!opt_num("bend_penalty", std::max(3.0 * grid, 2.0 * cte), bend) || bend < 0) {
        return jerr("invalid_params", "bend_penalty must be a number >= 0 (mm of equivalent pipe)");
    }

    std::string material;
    if (params.contains("material") && params["material"].is_string()) {
        material = params["material"].get<std::string>();
    }
    const std::string run_name = params.value("name", ojson(nullptr)).is_string()
                                     ? params["name"].get<std::string>()
                                     : std::string("Routed run");

    std::set<std::string> ignore;
    if (params.contains("ignore_bodies") && params["ignore_bodies"].is_array()) {
        for (const auto& n : params["ignore_bodies"])
            if (n.is_string()) ignore.insert(n.get<std::string>());
    }

    // ── obstacles: explicit boxes + existing runs of the target document ────
    std::vector<Box> raw;
    const ojson obs = params.value("obstacles", ojson::array());
    if (!obs.is_array()) {
        return jerr("invalid_params", "obstacles must be a list of {min:[x,y,z], max:[x,y,z], name?}");
    }
    for (size_t i = 0; i < obs.size(); ++i) {
        const ojson& o = obs[i];
        Box b;
        if (!o.is_object() || !read_v3(o.value("min", ojson(nullptr)), b.lo) ||
            !read_v3(o.value("max", ojson(nullptr)), b.hi)) {
            return jerr("invalid_params",
                        "obstacles[" + std::to_string(i) + "] needs min:[x,y,z] and max:[x,y,z]");
        }
        for (int a = 0; a < 3; ++a) {
            if (b.lo[a] > b.hi[a]) {
                return jerr("invalid_params", "obstacles[" + std::to_string(i) +
                                                  "]: min must be <= max on every axis");
            }
        }
        b.name = o.value("name", ojson(nullptr)).is_string() ? o["name"].get<std::string>()
                                                             : "obstacle[" + std::to_string(i) + "]";
        if (ignore.count(b.name)) continue;
        raw.push_back(std::move(b));
    }

    ojson doc;
    const ojson given = params.value("piping", ojson(nullptr));
    if (!given.is_null()) {
        const std::vector<std::string> errs = validate_piping(given);
        if (!errs.empty()) {
            ojson out = jerr("invalid_piping",
                             "the piping document to append to failed schema validation "
                             "(contracts/piping_schema.md)");
            out["errors"] = errs;
            return out;
        }
        doc = given;
        const double reach = clearance + od / 2.0;
        for (const auto& run : doc["runs"]) {
            if (run.value("suppressed", false)) continue;
            const ojson rp = nearest_pipe(run["nps"], pyutil::py_str(run.value("schedule", ojson("40"))));
            const double half = rp.value("ok", false) ? rp["outer_dia"].get<double>() / 2.0 : 0.0;
            const ojson& path = run["path"];
            for (size_t j = 0; j + 1 < path.size(); ++j) {
                V3 a{};
                V3 c{};
                read_v3(path[j], a);
                read_v3(path[j + 1], c);
                Box line;
                for (int k = 0; k < 3; ++k) {
                    line.lo[k] = std::min(a[k], c[k]);
                    line.hi[k] = std::max(a[k], c[k]);
                }
                // The route connects to this segment: not an obstacle there.
                if (seg_box_dist(ps, ps, line) < half + reach ||
                    seg_box_dist(pe, pe, line) < half + reach) {
                    continue;
                }
                Box b;
                for (int k = 0; k < 3; ++k) {
                    b.lo[k] = line.lo[k] - half;
                    b.hi[k] = line.hi[k] + half;
                }
                b.name = "run " + pyutil::py_str(run["id"]) + " segment " + std::to_string(j);
                raw.push_back(std::move(b));
            }
        }
    } else {
        doc = new_piping("Piping");
    }

    const double inflate = clearance + od / 2.0;
    std::vector<Box> infl = raw;
    for (Box& b : infl) {
        for (int a = 0; a < 3; ++a) {
            b.lo[a] -= inflate;
            b.hi[a] += inflate;
        }
    }

    for (const Box& b : raw) {
        if (strictly_inside(b, ps)) {
            return jerr("start_inside_obstacle",
                        "start point lies inside '" + b.name +
                            "'; place it on the nozzle face or pass that body in ignore_bodies");
        }
        if (strictly_inside(b, pe)) {
            return jerr("end_inside_obstacle",
                        "end point lies inside '" + b.name +
                            "'; place it on the nozzle face or pass that body in ignore_bodies");
        }
    }

    // ── nozzle stubs ────────────────────────────────────────────────────────
    auto make_stub = [&](const V3& p, const ojson& ep, const std::string& which,
                         Stub& st) -> ojson {
        const ojson dj = ep.value("dir", ojson(nullptr));
        bool in_env = false;
        for (const Box& b : infl) in_env = in_env || strictly_inside(b, p);
        StubTry best;
        int best_dir = -1;
        if (!dj.is_null()) {
            V3 d{};
            if (!read_v3(dj, d) || norm(d) < 1e-12) {
                return jerr("invalid_params", which + ".dir must be a non-zero [dx, dy, dz]");
            }
            int a = 0;
            for (int k = 1; k < 3; ++k)
                if (std::abs(d[k]) > std::abs(d[a])) a = k;
            if (std::abs(d[a]) / norm(d) < 0.9999) {
                warnings.push_back(which + ".dir snapped to the nearest axis (routes are orthogonal)");
            }
            best_dir = 2 * a + (d[a] < 0 ? 1 : 0);
            // end.dir is the travel direction arriving at the end point, so
            // its stub runs the other way out of the end point.
            if (which == "end") best_dir = opposite(best_dir);
            best = stub_along(p, best_dir, cte, raw, infl);
            if (!best.ok) {
                return jerr("no_route", which + " stub (" + kDirNames[best_dir] +
                                            " away from the point) runs into '" + best.blocker +
                                            "' within the elbow take-out; flip " + which +
                                            ".dir or pass that body in ignore_bodies");
            }
        } else if (in_env) {
            for (int d = 0; d < 6; ++d) {
                StubTry t = stub_along(p, d, cte, raw, infl);
                if (t.ok && (best_dir < 0 || t.len < best.len)) {
                    best = std::move(t);
                    best_dir = d;
                }
            }
            if (best_dir < 0) {
                return jerr("no_route", which +
                                            " point is boxed in by obstacle clearance envelopes; "
                                            "pass " + which + ".dir or reduce clearance_mm");
            }
            warnings.push_back(which + " is within clearance_mm of an obstacle; its stub runs " +
                               kDirNames[best_dir] + " away from the point (pass " + which +
                               ".dir to choose)");
        } else {
            st.dir = -1;
            st.tip = p;
            return nullptr;
        }
        st.dir = best_dir;
        st.tip = add(p, scale(kDirs[best_dir], best.len));
        st.escaped = best.escaped;
        if (!st.escaped.empty()) {
            std::string names;
            for (size_t i : st.escaped) names += (names.empty() ? "'" : ", '") + infl[i].name + "'";
            warnings.push_back(which + " lies within clearance_mm of " + names + "; the " + which +
                               " stub is exempt from the clearance check against it");
        }
        return nullptr;
    };
    Stub s_start;
    Stub s_end;
    if (ojson e = make_stub(ps, start_j, "start", s_start); !e.is_null()) return e;
    if (ojson e = make_stub(pe, end_j, "end", s_end); !e.is_null()) return e;
    const V3 sp = s_start.tip;
    const V3 ep = s_end.tip;

    // ── search domain ───────────────────────────────────────────────────────
    V3 lo{};
    V3 hi{};
    for (int a = 0; a < 3; ++a) {
        lo[a] = std::min({ps[a], pe[a], sp[a], ep[a]}) - (2.0 * grid + inflate);
        hi[a] = std::max({ps[a], pe[a], sp[a], ep[a]}) + (2.0 * grid + inflate);
    }
    std::vector<char> used(infl.size(), 0);
    for (bool grew = true; grew;) {
        grew = false;
        for (size_t i = 0; i < infl.size(); ++i) {
            if (used[i] || !boxes_overlap(infl[i], lo, hi)) continue;
            used[i] = 1;
            grew = true;
            for (int a = 0; a < 3; ++a) {
                lo[a] = std::min(lo[a], infl[i].lo[a] - 2.0 * grid);
                hi[a] = std::max(hi[a], infl[i].hi[a] + 2.0 * grid);
            }
        }
    }

    // ── rectilinear grid (uniform lines + exact special coordinates) ────────
    std::array<std::vector<double>, 3> cs;
    double step = grid;
    for (int attempt = 0;; ++attempt) {
        for (int a = 0; a < 3; ++a) {
            std::vector<std::pair<double, bool>> v;
            const double anchor = sp[a];
            const long i0 = static_cast<long>(std::ceil((lo[a] - anchor) / step - 1e-9));
            const long i1 = static_cast<long>(std::floor((hi[a] - anchor) / step + 1e-9));
            for (long i = i0; i <= i1; ++i) v.emplace_back(anchor + static_cast<double>(i) * step, false);
            v.emplace_back(lo[a], false);
            v.emplace_back(hi[a], false);
            for (double c : {ps[a], pe[a], sp[a], ep[a]}) v.emplace_back(c, true);
            for (size_t i = 0; i < infl.size(); ++i) {
                if (!used[i]) continue;
                if (infl[i].lo[a] >= lo[a] && infl[i].lo[a] <= hi[a]) v.emplace_back(infl[i].lo[a], true);
                if (infl[i].hi[a] >= lo[a] && infl[i].hi[a] <= hi[a]) v.emplace_back(infl[i].hi[a], true);
            }
            cs[a] = merge_coords(std::move(v));
        }
        const double n = static_cast<double>(cs[0].size()) * static_cast<double>(cs[1].size()) *
                         static_cast<double>(cs[2].size());
        if (n <= static_cast<double>(kMaxNodes)) break;
        if (attempt >= kMaxCoarsen) {
            return jerr("route_domain_too_large",
                        "the obstacle set needs more than 1e6 grid nodes even at grid_mm=" +
                            pyutil::fmt_g(step, 6) +
                            "; pass fewer obstacles (ignore_bodies) or route in shorter legs");
        }
        step *= std::cbrt(n / static_cast<double>(kMaxNodes)) * 1.05;
    }
    if (step > grid * (1.0 + 1e-9)) {
        warnings.push_back("grid coarsened from " + pyutil::fmt_g(grid, 6) + " to " +
                           pyutil::fmt_g(pyround(step, 1), 6) +
                           " mm to keep the search under 1e6 nodes (obstacle faces stay exact)");
    }
    const size_t nx = cs[0].size();
    const size_t ny = cs[1].size();
    const size_t nz = cs[2].size();
    const size_t N = nx * ny * nz;
    const size_t stride[3] = {1, nx, nx * ny};
    auto node_of = [&](const V3& p) -> long {
        const long i = find_coord(cs[0], p[0]);
        const long j = find_coord(cs[1], p[1]);
        const long k = find_coord(cs[2], p[2]);
        if (i < 0 || j < 0 || k < 0) return -1;
        return static_cast<long>((static_cast<size_t>(k) * ny + static_cast<size_t>(j)) * nx +
                                 static_cast<size_t>(i));
    };
    auto pos_of = [&](size_t n) -> V3 {
        const size_t i = n % nx;
        const size_t j = (n / nx) % ny;
        const size_t k = n / (nx * ny);
        return {cs[0][i], cs[1][j], cs[2][k]};
    };

    // bit0: node inside an inflated box; bit1/2/3: +x/+y/+z edge from node
    // crosses an inflated box interior (faces are grid lines, so an edge is
    // either wholly inside or wholly outside each box's open interior).
    std::vector<uint8_t> flags(N, 0);
    for (size_t bi = 0; bi < infl.size(); ++bi) {
        if (!used[bi]) continue;
        const Box& b = infl[bi];
        size_t in0[3];
        size_t in1[3];  // exclusive
        size_t ed0[3];
        size_t ed1[3];  // exclusive
        for (int a = 0; a < 3; ++a) {
            const auto& c = cs[a];
            in0[a] = static_cast<size_t>(std::upper_bound(c.begin(), c.end(), b.lo[a] + kTol) - c.begin());
            in1[a] = static_cast<size_t>(std::lower_bound(c.begin(), c.end(), b.hi[a] - kTol) - c.begin());
            ed0[a] = static_cast<size_t>(std::lower_bound(c.begin(), c.end(), b.lo[a] - kTol) - c.begin());
            const size_t ub = static_cast<size_t>(std::upper_bound(c.begin(), c.end(), b.hi[a] + kTol) - c.begin());
            ed1[a] = ub >= 1 ? ub - 1 : 0;  // edges i with c[i+1] <= hi: i < ub-1
        }
        for (size_t k = in0[2]; k < in1[2]; ++k)
            for (size_t j = in0[1]; j < in1[1]; ++j)
                for (size_t i = in0[0]; i < in1[0]; ++i) flags[(k * ny + j) * nx + i] |= 1;
        for (int a = 0; a < 3; ++a) {
            size_t r0[3] = {in0[0], in0[1], in0[2]};
            size_t r1[3] = {in1[0], in1[1], in1[2]};
            r0[a] = ed0[a];
            r1[a] = ed1[a];
            const uint8_t bit = static_cast<uint8_t>(2u << a);
            for (size_t k = r0[2]; k < r1[2]; ++k)
                for (size_t j = r0[1]; j < r1[1]; ++j)
                    for (size_t i = r0[0]; i < r1[0]; ++i) flags[(k * ny + j) * nx + i] |= bit;
        }
    }

    const long sn_l = node_of(sp);
    const long gn_l = node_of(ep);
    if (sn_l < 0 || gn_l < 0) {
        return jerr("no_route", "internal: route endpoints are not on the search grid");
    }
    const size_t sn = static_cast<size_t>(sn_l);
    const size_t gn = static_cast<size_t>(gn_l);

    // ── A* over (node, travel direction) ────────────────────────────────────
    const size_t S = N * 6;  // goal sentinel state
    constexpr float kInf = std::numeric_limits<float>::infinity();
    std::vector<float> g(S + 1, kInf);
    std::vector<int32_t> parent(S + 1, -1);
    std::vector<uint8_t> closed(S + 1, 0);
    struct QE {
        float f;
        float g;
        int32_t s;
    };
    auto worse = [](const QE& a, const QE& b) { return a.f > b.f || (a.f == b.f && a.g < b.g); };
    std::priority_queue<QE, std::vector<QE>, decltype(worse)> pq(worse);
    const V3 gpos = pos_of(gn);
    auto h = [&](size_t n) -> float {
        const V3 p = pos_of(n);
        return static_cast<float>(std::abs(p[0] - gpos[0]) + std::abs(p[1] - gpos[1]) +
                                  std::abs(p[2] - gpos[2]));
    };
    const float fbend = static_cast<float>(bend);
    if (s_start.dir >= 0) {
        const size_t s0 = sn * 6 + static_cast<size_t>(s_start.dir);
        g[s0] = 0.0f;
        pq.push({h(sn), 0.0f, static_cast<int32_t>(s0)});
    } else {
        for (size_t d = 0; d < 6; ++d) {
            g[sn * 6 + d] = 0.0f;
            pq.push({h(sn), 0.0f, static_cast<int32_t>(sn * 6 + d)});
        }
    }
    // Required final travel direction into the end point (= end.dir).
    const int goal_in = s_end.dir >= 0 ? opposite(s_end.dir) : -1;
    size_t expanded = 0;
    while (!pq.empty()) {
        const QE e = pq.top();
        pq.pop();
        const size_t s = static_cast<size_t>(e.s);
        if (closed[s] || e.g > g[s]) continue;
        closed[s] = 1;
        if (s == S) break;
        ++expanded;
        const size_t n = s / 6;
        const int d = static_cast<int>(s % 6);
        if (n == gn) {
            if (goal_in < 0 || d != opposite(goal_in)) {
                const float cand = e.g + ((goal_in < 0 || d == goal_in) ? 0.0f : fbend);
                if (cand < g[S]) {
                    g[S] = cand;
                    parent[S] = e.s;
                    pq.push({cand, cand, static_cast<int32_t>(S)});
                }
            }
            if (n != sn) continue;  // never route through the end point
        }
        const size_t idx[3] = {n % nx, (n / nx) % ny, n / (nx * ny)};
        const size_t dims[3] = {nx, ny, nz};
        for (int nd = 0; nd < 6; ++nd) {
            if (nd == opposite(d)) continue;  // no 180° fold-backs
            const int a = nd / 2;
            const bool pos = (nd % 2) == 0;
            const uint8_t bit = static_cast<uint8_t>(2u << a);
            size_t m = 0;
            if (pos) {
                if (idx[a] + 1 >= dims[a] || (flags[n] & bit)) continue;
                m = n + stride[a];
            } else {
                if (idx[a] == 0) continue;
                m = n - stride[a];
                if (flags[m] & bit) continue;
            }
            if (flags[m] & 1) continue;
            const double len = std::abs(cs[a][pos ? idx[a] + 1 : idx[a] - 1] - cs[a][idx[a]]);
            const float cost = e.g + static_cast<float>(len) + (nd != d ? fbend : 0.0f);
            const size_t ms = m * 6 + static_cast<size_t>(nd);
            if (cost < g[ms]) {
                g[ms] = cost;
                parent[ms] = e.s;
                pq.push({cost + h(m), cost, static_cast<int32_t>(ms)});
            }
        }
    }
    if (!closed[S]) {
        ojson out = jerr("no_route",
                         "no collision-free orthogonal path at clearance_mm=" +
                             pyutil::fmt_g(clearance, 6) +
                             " inside the search domain; reduce clearance_mm, drop obstacles the "
                             "pipe may pass (ignore_bodies), or move the endpoints/nozzle dirs");
        out["grid_mm"] = pyround(step, 3);
        out["expanded_states"] = expanded;
        return out;
    }

    // ── reconstruct + simplify ──────────────────────────────────────────────
    std::vector<V3> nodes;
    for (int32_t s = parent[S]; s >= 0; s = parent[static_cast<size_t>(s)])
        nodes.push_back(pos_of(static_cast<size_t>(s) / 6));
    std::reverse(nodes.begin(), nodes.end());
    std::vector<V3> full;
    full.push_back(ps);
    full.insert(full.end(), nodes.begin(), nodes.end());
    full.push_back(pe);
    std::vector<V3> path;
    for (const V3& p : full) {
        if (!path.empty() && dist(path.back(), p) < kTol) continue;
        if (path.size() >= 2) {
            const V3 u = unit(sub(path.back(), path[path.size() - 2]));
            const V3 w = unit(sub(p, path.back()));
            if (dot(u, w) > 1.0 - 1e-9) {
                path.back() = p;
                continue;
            }
        }
        path.push_back(p);
    }

    double length = 0.0;
    for (size_t i = 0; i + 1 < path.size(); ++i) length += dist(path[i], path[i + 1]);
    const size_t bends = path.size() - 2;

    // Straight between consecutive elbows must fit both take-outs.
    for (size_t i = 0; i + 1 < path.size(); ++i) {
        const double need = (i > 0 ? cte : 0.0) + (i + 2 < path.size() ? cte : 0.0);
        const double seg = dist(path[i], path[i + 1]);
        if (seg + kTol < need) {
            warnings.push_back("segment " + std::to_string(i) + " is " + pyutil::fmt_f(seg, 1) +
                               " mm but its elbows need " + pyutil::fmt_f(need, 1) +
                               " mm of take-out; raise bend_penalty or grid_mm");
        }
    }

    // Surface-to-obstacle clearance of the routed pipe (stub exemptions).
    ojson min_clear = nullptr;
    ojson nearest = nullptr;
    double best = std::numeric_limits<double>::infinity();
    for (size_t i = 0; i + 1 < path.size(); ++i) {
        for (size_t bi = 0; bi < raw.size(); ++bi) {
            const bool exempt_start =
                i == 0 && std::find(s_start.escaped.begin(), s_start.escaped.end(), bi) !=
                              s_start.escaped.end();
            const bool exempt_end = i + 2 == path.size() &&
                                    std::find(s_end.escaped.begin(), s_end.escaped.end(), bi) !=
                                        s_end.escaped.end();
            if (exempt_start || exempt_end) continue;
            const double c = seg_box_dist(path[i], path[i + 1], raw[bi]) - od / 2.0;
            if (c < best) {
                best = c;
                nearest = raw[bi].name;
            }
        }
    }
    if (std::isfinite(best)) min_clear = pyround(best, 2);

    // ── append run + elbows to the document ─────────────────────────────────
    const std::string run_id = next_id(doc, "r");
    ojson path_j = ojson::array();
    for (const V3& p : path) path_j.push_back(v3_json(p));
    ojson run = new_pipe_run(run_id, path_j, nps_j, schedule, run_name);
    if (!material.empty()) run["material"] = material;
    run["bend_radius"] = pyround(cte, 3);  // matches the LR elbow fittings
    doc["runs"].push_back(run);
    ojson elbow_ids = ojson::array();
    for (size_t i = 1; i + 1 < path.size(); ++i) {
        const V3 din = unit(sub(path[i], path[i - 1]));
        const V3 dout = unit(sub(path[i + 1], path[i]));
        const std::array<double, 3> o = elbow_orient(din, dout);
        ojson prm = ojson::object();
        prm["angle"] = 90;
        prm["radius"] = "LR";
        const std::string fid = next_id(doc, "ft");
        ojson ft = new_fitting(fid, "elbow", nps_j, v3_json(path[i]), schedule,
                               "Elbow " + run_id + "." + std::to_string(i), prm);
        ft["orient"] = ojson::array({o[0], o[1], o[2]});
        if (!material.empty()) ft["material"] = material;
        doc["fittings"].push_back(ft);
        elbow_ids.push_back(fid);
    }
    const std::vector<std::string> verrs = validate_piping(doc);
    if (!verrs.empty()) {
        ojson out = jerr("invalid_piping",
                         "the routed document failed schema validation; check material/schedule "
                         "against contracts/piping_schema.md §2");
        out["errors"] = verrs;
        return out;
    }

    ojson out = ojson::object();
    out["ok"] = true;
    out["piping"] = std::move(doc);
    out["run_id"] = run_id;
    out["path"] = path_j;
    out["length_mm"] = pyround(length, 2);
    out["bends"] = bends;
    out["elbow_ids"] = std::move(elbow_ids);
    out["min_clearance_mm"] = min_clear;
    out["nearest_obstacle"] = nearest;
    out["clearance_mm"] = clearance;
    out["grid_mm"] = pyround(step, 3);
    out["bend_penalty_mm"] = pyround(bend, 3);
    out["search"] = {{"nodes", N}, {"expanded_states", expanded}, {"grid", {nx, ny, nz}}};
    out["warnings"] = warnings;
    out["hint"] = "routed " + pyutil::fmt_f(length, 0) + " mm of NPS " + pyutil::py_str(nps_j) +
                  " Sch " + schedule + " with " + std::to_string(bends) +
                  " LR elbow(s); check pressure design with piping_spec_check and quantities with "
                  "piping_bom";
    out["next_actions"] = ojson::array({"piping_spec_check", "piping_bom", "piping_solve"});
    return out;
}

}  // namespace chudware::piping
