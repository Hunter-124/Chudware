#include "harness/route.h"
#include "harness/obstacles.h"

#include <algorithm>
#include <functional>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <limits>
#include <queue>
#include <string>
#include <unordered_map>
#include <vector>

namespace chudware::harness {
namespace {
using json = nlohmann::json;
json fail(const char* code, const std::string& error, json details = json::object()) {
    json reply = {{"ok", false}, {"code", code}, {"error", error}};
    for (auto it = details.begin(); it != details.end(); ++it) reply[it.key()] = std::move(it.value());
    return reply;
}
bool vec(const json& j, V3& v) {
    if (!j.is_array() || j.size() != 3) return false;
    for (int i = 0; i < 3; ++i) {
        if (!j[i].is_number()) return false;
        v[i] = j[i].get<double>();
    }
    return finite(v) && std::abs(v.x) <= 1e9 && std::abs(v.y) <= 1e9 && std::abs(v.z) <= 1e9;
}
json jvec(const V3& v) { return json::array({v.x, v.y, v.z}); }
// Reads optional `key` (default `def`) into `out`. Returns "" when valid, otherwise an error
// naming the key, the supplied value and the accepted range.
std::string number(const json& p, const char* key, double def, double lo, double hi, double& out,
                   bool integer = false) {
    if (!p.contains(key)) { out = def; return {}; }
    const json& value = p[key];
    if (value.is_number()) {
        out = value.get<double>();
        if (std::isfinite(out) && out >= lo && out <= hi && (!integer || std::floor(out) == out)) return {};
    }
    std::string supplied = value.dump();
    if (supplied.size() > 64) supplied = supplied.substr(0, 61) + "...";
    char range[96];
    std::snprintf(range, sizeof range, " in [%.6g, %.6g]", lo, hi);
    return std::string(key) + " = " + supplied + " is invalid: expected " +
           (integer ? "an integer" : "a finite number") + range;
}
struct Key {
    int x, y, z;
    bool operator==(const Key& b) const { return x == b.x && y == b.y && z == b.z; }
};
struct Hash {
    size_t operator()(const Key& k) const {
        size_t h = std::hash<int>{}(k.x);
        h ^= std::hash<int>{}(k.y) + 0x9e3779b9u + (h << 6) + (h >> 2);
        h ^= std::hash<int>{}(k.z) + 0x9e3779b9u + (h << 6) + (h >> 2);
        return h;
    }
};
struct Record { double g = kInf; Key parent{}; bool has_parent = false; bool closed = false; };
struct Entry { double f, g; Key k; uint64_t order; };
struct Greater {
    bool operator()(const Entry& a, const Entry& b) const {
        return a.f != b.f ? a.f > b.f : a.order > b.order;
    }
};

// A relaxed endpoint affects only owners whose clearance envelope contains it.
// Split at sphere boundaries so no part outside that radius gets an exemption.
struct Collision {
    const ObstacleSet& obs;
    V3 start, end;
    double relax;
    std::vector<bool> start_owner, end_owner;
    std::vector<double> strict;
    mutable std::vector<double> workspace;
    Collision(const ObstacleSet& o, V3 s, V3 e, double r)
        : obs(o), start(s), end(e), relax(r), start_owner(o.owners.size()), end_owner(o.owners.size()),
          strict(o.thr), workspace(o.thr.size()) {
        for (double& t : strict) t = std::max(1e-8, t);
        if (r > 0) {
            const auto a = o.bvh.nearest_point(s, o.thr_max);
            const auto b = o.bvh.nearest_point(e, o.thr_max);
            if (a.owner >= 0 && a.dist < o.thr[static_cast<size_t>(a.owner)])
                start_owner[static_cast<size_t>(a.owner)] = true;
            if (b.owner >= 0 && b.dist < o.thr[static_cast<size_t>(b.owner)])
                end_owner[static_cast<size_t>(b.owner)] = true;
        }
    }
    // True when segment ab, inflated by `extra`, clears every obstacle (see scan).
    bool clear(const V3& a, const V3& b, double extra = 0) const { return scan(a, b, extra, false) < 0; }
    // Owner index of an obstacle that makes clear(a, b, extra) false, or -1 when clear.
    int blocker(const V3& a, const V3& b, double extra = 0) const { return scan(a, b, extra, true); }
private:
    int scan(const V3& a, const V3& b, double extra, bool want_owner) const {
        if (obs.bvh.empty()) return -1;
        // Route starts outside solids; every continuous edge is tested against
        // the surface, so it cannot enter one without a detected crossing.
        auto hit = [&](const V3& p, const V3& q, const std::vector<double>& thr, double thr_max) {
            if (want_owner) return obs.bvh.first_within_segment(p, q, thr, thr_max).owner;
            return obs.bvh.any_within_segment(p, q, thr, thr_max) ? 0 : -1;
        };
        if (relax == 0 && extra == 0) return hit(a, b, strict, std::max(1e-8, obs.thr_max));
        const V3 d = b - a;
        std::array<double, 6> cuts{};
        cuts[0] = 0; cuts[1] = 1;
        size_t count = 2;
        const double effective_relax = std::max(0.0, relax - extra);
        if (effective_relax > 0) {
            for (const V3& center : {start, end}) {
                const V3 f = a - center;
                const double A = norm2(d), B = 2 * dot(f, d), C = norm2(f) - effective_relax * effective_relax;
                const double disc = B * B - 4 * A * C;
                if (A > 0 && disc > 0) {
                    const double root = std::sqrt(disc);
                    for (double t : {(-B - root) / (2 * A), (-B + root) / (2 * A)})
                        if (t > 0 && t < 1) cuts[count++] = t;
                }
            }
        }
        std::sort(cuts.begin(), cuts.begin() + count);
        for (size_t c = 1; c < count; ++c) {
            const V3 mid = lerp(a, b, (cuts[c - 1] + cuts[c]) * 0.5);
            const bool near_start = effective_relax > 0 && dist(mid, start) < effective_relax;
            const bool near_end = effective_relax > 0 && dist(mid, end) < effective_relax;
            double max_thr = 0;
            for (size_t i = 0; i < workspace.size(); ++i) {
                const bool exempt = (start_owner[i] && near_start) || (end_owner[i] && near_end);
                workspace[i] = exempt ? std::max(1e-8, extra) : std::max(1e-8, obs.thr[i] + extra);
                max_thr = std::max(max_thr, workspace[i]);
            }
            const int owner = hit(lerp(a, b, cuts[c - 1]), lerp(a, b, cuts[c]), workspace, max_thr);
            if (owner >= 0) return owner;
        }
        return -1;
    }
};

double point_segment_distance(const V3& p, const V3& a, const V3& b) {
    const V3 d = b - a;
    const double n = norm2(d);
    const double t = n > 0 ? std::clamp(dot(p - a, d) / n, 0.0, 1.0) : 0;
    return dist(p, a + d * t);
}

struct Bend { V3 at; double angle, radius; };
// Turns below this (radians) are straight: their fillet would be a micro-arc whose entry and exit
// lie ~r * angle apart, where coordinate rounding (~1e-13 mm at 500 mm) swamps the half-angle
// turns path checks measure, so a lattice corner off by float noise became two "isolated kinks".
// Skipping such a corner shifts the path by at most leg * angle (~1e-3 mm on a 100 mm leg).
constexpr double kStraightTurn = 1e-5;

// Trim length of the fillet at corner b of a-b-c; false when the turn cannot be filleted.
bool corner_trim(const V3& a, const V3& b, const V3& c, double r, double& trim) {
    trim = 0;
    const double cosine = std::clamp(dot(unit(b - a), unit(c - b)), -1.0, 1.0);
    const double angle = std::acos(cosine);
    if (angle < kStraightTurn) return true;
    if (r <= 0 || cosine < -0.999999) return false;
    trim = r * std::tan(angle / 2);
    return std::isfinite(trim);
}

// Walks the fillet arc at corner b of a-b-c (trim from corner_trim), sampled at <= 10 degrees:
// visit(point, extra) gets the entry first (extra 0: the straight leg before it), then each sample,
// the last being the exit. The chord ending at a sample is inflated by its sagitta (`extra`) in
// obstacle checks. Stops and returns false when visit does.
template <class Visit>
bool walk_arc(const V3& a, const V3& b, const V3& c, double r, double trim, Visit visit) {
    const V3 u = unit(b - a), v = unit(c - b);
    const double angle = std::acos(std::clamp(dot(u, v), -1.0, 1.0));
    const V3 entry = b - u * trim, exit = b + v * trim;
    const V3 axis = unit(cross(u, v));
    const V3 center = entry + cross(axis, u) * r;
    const V3 radial = entry - center;
    const int steps = std::max(1, static_cast<int>(std::ceil(angle / (kPi / 18))));
    const double sagitta = r * (1 - std::cos(angle / (2 * steps)));
    if (!visit(entry, 0.0)) return false;
    for (int j = 1; j <= steps; ++j) {
        const double t = angle * j / steps;
        if (!visit(j == steps ? exit : center + radial * std::cos(t) + cross(axis, radial) * std::sin(t), sagitta))
            return false;
    }
    return true;
}

// Replace each polyline corner with an exact circular fillet, sampling it at
// <= 10 degrees. The sagitta is added to the obstacle threshold during checks.
bool fillet(const std::vector<V3>& ctrl, double r, const Collision& collision,
            std::vector<V3>& path, std::vector<Bend>& bends, double& length,
            bool* geometry_failed = nullptr) {
    if (geometry_failed) *geometry_failed = false;
    auto geometry = [&]() { if (geometry_failed) *geometry_failed = true; return false; };
    const size_t n = ctrl.size();
    if (n < 2 || n > 256) return false;
    std::vector<double> trim(n, 0), angle(n, 0);
    for (size_t i = 1; i + 1 < n; ++i) {
        if (!corner_trim(ctrl[i - 1], ctrl[i], ctrl[i + 1], r, trim[i])) return geometry();
        if (trim[i] > 0)  // a corner below kStraightTurn is passed straight: no arc, no length
            angle[i] = std::acos(std::clamp(dot(unit(ctrl[i] - ctrl[i - 1]), unit(ctrl[i + 1] - ctrl[i])), -1.0, 1.0));
    }
    for (size_t i = 0; i + 1 < n; ++i)
        if (trim[i] + trim[i + 1] > dist(ctrl[i], ctrl[i + 1]) - 1e-8) return geometry();
    path.clear(); bends.clear(); length = 0; path.push_back(ctrl.front());
    auto add = [&](const V3& p, double extra) {
        if (dist(path.back(), p) < 1e-10) return true;
        if (path.size() >= 4096) return false;
        if (!collision.clear(path.back(), p, extra)) return false;
        path.push_back(p); return true;
    };
    for (size_t i = 1; i + 1 < n; ++i) {
        if (trim[i] == 0) continue;
        if (!walk_arc(ctrl[i - 1], ctrl[i], ctrl[i + 1], r, trim[i], add)) return false;
        bends.push_back({ctrl[i], angle[i], r});
    }
    if (!add(ctrl.back(), 0)) return false;
    // Length of the intended tangent-line / circular-arc route, not the chord sum.
    for (size_t i = 0; i + 1 < n; ++i) length += dist(ctrl[i], ctrl[i + 1]) - trim[i] - trim[i + 1];
    for (size_t i = 1; i + 1 < n; ++i) length += r * angle[i];
    return true;
}

// Collision check of the fillet arc at corner b of a-b-c (trim from corner_trim), exactly as
// fillet() samples it.
bool arc_clear(const Collision& collision, const V3& a, const V3& b, const V3& c, double r, double trim) {
    if (trim <= 0) return true;
    V3 last{};
    bool first = true;
    return walk_arc(a, b, c, r, trim, [&](const V3& p, double extra) {
        const bool ok = first || dist(last, p) < 1e-10 || collision.clear(last, p, extra);
        first = false;
        last = p;
        return ok;
    });
}

// A corner whose turn the adjacent legs cannot fillet (short stub, hairpin, end behind the
// start), or a polyline whose fillet collides, is repaired by replacing a corner with a detour:
// continue `a` past the corner along the incoming leg, make a quarter turn toward the target
// side, run `w` and turn onto the next leg. The detour legs are sampled on multiples of the bend
// radius and kept only when all their trims fit locally; the full polyline then passes the
// ordinary fillet/collision check (recursing if another corner is geometrically infeasible).
// The corner replaced is the first geometrically infeasible one or, when only collisions
// reject the polyline, each corner in turn. A detour keeps the leg that arrives at its corner,
// so the corner at the end of an exit stub is detoured travelling away from that endpoint (the
// polyline reversed): the stub of a fixed start/end is never replaced. On success `ctrl` is the
// repaired polyline and path/bends/length are set.
bool repair_controls(std::vector<V3>& ctrl, double r, const Collision& collision, std::vector<V3>& path,
                     std::vector<Bend>& bends, double& length, int depth, bool fixed_start, bool fixed_end) {
    const size_t n = ctrl.size();
    if (depth <= 0 || n < 3 || n + 1 > 256 || r <= 0) return false;
    const double slack = 0.02 * r;
    std::vector<double> trim(n, 0.0);
    size_t bad = 0;
    for (size_t i = 1; i + 1 < n; ++i)
        if (!corner_trim(ctrl[i - 1], ctrl[i], ctrl[i + 1], r, trim[i]) && bad == 0) bad = i;
    for (size_t i = 0; i + 1 < n && bad == 0; ++i) {
        if (trim[i] + trim[i + 1] <= dist(ctrl[i], ctrl[i + 1]) - 1e-8) continue;
        if (i == 0) bad = 1;
        else if (i + 2 == n) bad = i;
        else bad = trim[i] >= trim[i + 1] ? i : i + 1;
    }
    std::vector<size_t> corners;
    if (bad != 0) corners.push_back(bad);
    else for (size_t k = 1; k + 1 < n; ++k) corners.push_back(k);
    struct Detour { double a, w; };
    std::vector<Detour> detours;
    for (double a : {0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0})
        for (double w : {1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0}) detours.push_back({a * r, w * r});
    std::sort(detours.begin(), detours.end(), [](const Detour& x, const Detour& y) {
        return x.a + x.w != y.a + y.w ? x.a + x.w < y.a + y.w : x.a < y.a;
    });
    for (const size_t k : corners) {
        for (const bool backward : {false, true}) {
            if (backward ? fixed_start && k == 1 : fixed_end && k + 2 == n) continue;
            std::vector<V3> c = ctrl;
            std::vector<double> t = trim;
            if (backward) { std::reverse(c.begin(), c.end()); std::reverse(t.begin(), t.end()); }
            const size_t i = backward ? n - 1 - k : k;
            const V3 prev = c[i - 1], p = c[i], q = c[i + 1];
            const V3 u = unit(p - prev);
            const V3 toward = q - p;
            V3 s = toward - u * dot(toward, u);
            if (norm(s) < 1e-6 * norm(toward)) {
                const V3 axis = std::abs(u.z) < 0.9 ? V3{0, 0, 1} : V3{1, 0, 0};
                s = cross(axis, u);
            }
            s = unit(s);
            const bool q_interior = i + 2 < n;
            const double trim_after = i + 2 < n - 1 ? t[i + 2] : 0.0;
            int tried = 0;
            for (const Detour& d : detours) {
                const V3 p1 = p + u * d.a, p2 = p1 + s * d.w;
                double t1 = 0, t2 = 0, tq = 0;
                if (!corner_trim(prev, p1, p2, r, t1) || !corner_trim(p1, p2, q, r, t2)) continue;
                if (q_interior && !corner_trim(p2, q, c[i + 2], r, tq)) continue;
                if (t[i - 1] + t1 > dist(prev, p1) - slack) continue;
                if (t1 + t2 > d.w - slack) continue;
                if (t2 + tq > dist(p2, q) - slack) continue;
                if (q_interior && tq + trim_after > dist(q, c[i + 2]) - slack) continue;
                std::vector<V3> next(c.begin(), c.begin() + static_cast<std::ptrdiff_t>(i));
                next.push_back(p1);
                next.push_back(p2);
                next.insert(next.end(), c.begin() + static_cast<std::ptrdiff_t>(i) + 1, c.end());
                bool geometry = false;
                bool ok = fillet(next, r, collision, path, bends, length, &geometry) ||
                          (geometry && repair_controls(next, r, collision, path, bends, length, depth - 1,
                                                       backward ? fixed_end : fixed_start,
                                                       backward ? fixed_start : fixed_end));
                if (ok && backward) {
                    std::reverse(next.begin(), next.end());
                    ok = fillet(next, r, collision, path, bends, length);
                }
                if (ok) {
                    ctrl = std::move(next);
                    return true;
                }
                if (++tried >= 6) break;
            }
        }
    }
    return false;
}
} // namespace

nlohmann::json harness_route(const nlohmann::json& p, const Scene& scene) {
    if (!p.is_object()) return fail("bad_args", "parameters must be an object");
    V3 start, end, sd, ed;
    bool has_sd = false, has_ed = false;
    auto endpoint = [&](const char* key, V3& point, V3& direction, bool& has_dir) {
        if (!p.contains(key) || !p[key].is_object() || !p[key].contains("pos") || !vec(p[key]["pos"], point)) return false;
        if (p[key].contains("dir")) {
            if (!vec(p[key]["dir"], direction) || norm(direction) < 1e-12) return false;
            direction = unit(direction); has_dir = true;
        }
        return true;
    };
    if (!endpoint("start", start, sd, has_sd) || !endpoint("end", end, ed, has_ed) || dist(start, end) < 1e-9)
        return fail("bad_args", "start/end require distinct finite pos vectors and nonzero optional directions");
    double od, radius, clearance, grid, relax, max_nodes_d;
    if (!p.contains("od_mm") || !p.contains("min_bend_radius_mm"))
        return fail("bad_args", "od_mm and min_bend_radius_mm are required");
    for (const std::string& bad : {
             number(p, "od_mm", 0, 1e-6, 1e6, od),
             number(p, "min_bend_radius_mm", 0, 1e-6, 1e7, radius),
             number(p, "clearance_mm", 2, 0, 1e6, clearance),
             number(p, "endpoint_relax_mm", 0, 0, 1e6, relax),
             number(p, "max_nodes", 50000, 1, 200000, max_nodes_d, true)})
        if (!bad.empty()) return fail("bad_args", bad);
    if (const std::string bad = number(p, "grid_mm", std::max({1.0, od, radius / 2, dist(start, end) / 40}),
                                       1e-4, 1e7, grid);
        !bad.empty())
        return fail("bad_args", bad);
    const size_t budget = static_cast<size_t>(max_nodes_d);
    std::vector<std::pair<V3, V3>> preferred;
    if (p.contains("prefer_paths")) {
        if (!p["prefer_paths"].is_array() || p["prefer_paths"].size() > 128)
            return fail("bad_args", "prefer_paths must be an array of at most 128 polylines");
        for (const auto& line : p["prefer_paths"]) {
            if (!line.is_array() || line.size() < 2) return fail("bad_args", "preferred paths need at least two points");
            V3 prev, cur;
            if (!vec(line[0], prev)) return fail("bad_args", "invalid preferred path point");
            for (size_t i = 1; i < line.size(); ++i) {
                if (!vec(line[i], cur) || preferred.size() >= 256) return fail("bad_args", "invalid or oversized preferred paths (maximum 256 segments)");
                preferred.emplace_back(prev, cur); prev = cur;
            }
        }
    }
    ObstacleSet obstacles;
    const std::string error = build_obstacles(p, scene, od, clearance, obstacles);
    if (!error.empty()) return fail("bad_args", error);
    Collision collision(obstacles, start, end, relax);
    auto num = [](double v) {
        char buf[32];
        std::snprintf(buf, sizeof buf, "%.3f", v);
        return std::string(buf);
    };
    // Fills `detail` and returns true when the endpoint is inside an obstacle or its clearance envelope.
    auto endpoint_blocked = [&](const V3& pt, bool first, json& detail) {
        std::vector<double> threshold = obstacles.thr;
        const auto& exempt = first ? collision.start_owner : collision.end_owner;
        for (size_t i = 0; i < threshold.size(); ++i)
            threshold[i] = relax > 0 && exempt[i] ? 1e-8 : std::max(1e-8, threshold[i]);
        const double limit = std::max(1e-8, obstacles.thr_max);
        const int inside = obstacles.bvh.first_containing_owner(pt, obstacles.owner_count());
        TriBvh::Nearest hit;
        if (inside < 0) {
            if (!obstacles.bvh.any_within_point(pt, threshold, limit)) return false;
            // Prefer the nearest obstacle; when it is exempt or farther than its own
            // envelope, the violating owner is a different (farther) one.
            hit = obstacles.bvh.nearest_point(pt, limit);
            if (hit.owner < 0 || !(hit.dist < threshold[static_cast<size_t>(hit.owner)]))
                hit = obstacles.bvh.first_within_segment(pt, pt, threshold, limit);
        }
        const int owner = inside >= 0 ? inside : hit.owner;
        detail = {{"endpoint", first ? "start" : "end"}, {"pos", jvec(pt)},
                  {"obstacle", obstacles.label(owner)},
                  {"distance_mm", inside >= 0 ? 0.0 : hit.dist},
                  {"required_mm", owner >= 0 ? obstacles.thr[static_cast<size_t>(owner)] : 0.0},
                  {"inside_solid", inside >= 0}};
        return true;
    };
    auto blocked_message = [&](const json& d) {
        const std::string who = d["endpoint"].get<std::string>();
        if (d["inside_solid"].get<bool>())
            return who + " is inside solid " + d["obstacle"].get<std::string>();
        return who + " is " + num(d["distance_mm"].get<double>()) + " mm from " +
               d["obstacle"].get<std::string>() + " (centerline); needs at least " +
               num(d["required_mm"].get<double>()) + " mm (clearance + od/2)";
    };
    json detail;
    if (endpoint_blocked(start, true, detail)) return fail("start_blocked", blocked_message(detail), detail);
    if (endpoint_blocked(end, false, detail)) return fail("end_blocked", blocked_message(detail), detail);
    // Required straight length at an endpoint with a direction; independent of the grid and of
    // the far endpoint, so a connector's feasibility is a property of the connector alone.
    const double stub = 2 * radius;
    const V3 sp = has_sd ? start + sd * stub : start;
    const V3 ep = has_ed ? end + ed * stub : end;
    auto exit_blocked = [&](const V3& origin, const V3& dir, bool first) -> json {
        if (collision.clear(origin, origin + dir * stub)) return nullptr;
        // Largest clear straight length along dir (bisection, same semantics as the search).
        double lo = 0, hi = stub;
        while (hi - lo > 0.01) {
            const double mid = 0.5 * (lo + hi);
            (collision.clear(origin, origin + dir * mid) ? lo : hi) = mid;
        }
        const int owner = collision.blocker(origin, origin + dir * hi);
        const char* who = first ? "start" : "end";
        return fail("exit_blocked",
                    std::string(who) + " exit direction is blocked: " + num(lo) + " mm of the required " +
                        num(stub) + " mm straight stub (2 x min_bend_radius_mm) is clear, blocked by " +
                        obstacles.label(owner),
                    {{"endpoint", who}, {"dir", jvec(dir)}, {"stub_mm", stub}, {"free_mm", lo},
                     {"obstacle", obstacles.label(owner)}});
    };
    if (has_sd) { json r = exit_blocked(start, sd, true); if (!r.is_null()) return r; }
    if (has_ed) { json r = exit_blocked(end, ed, false); if (!r.is_null()) return r; }

    std::vector<V3> controls, path;
    std::vector<Bend> bends;
    double length = 0;
    size_t expanded = 0;
    auto budget_detail = [&]() -> json {
        return {{"expanded_nodes", expanded}, {"grid_mm", grid}, {"max_nodes", budget}};
    };
    const double slack = 0.02 * radius;  // keeps fillet arcs of adjacent corners apart
    std::vector<std::vector<V3>> turn_candidates;  // polylines rejected for turn geometry only
    bool turn_failed = false, other_failed = false;
    auto same_points = [](const std::vector<V3>& a, const std::vector<V3>& b) {
        if (a.size() != b.size()) return false;
        for (size_t i = 0; i < a.size(); ++i)
            if (a[i].x != b[i].x || a[i].y != b[i].y || a[i].z != b[i].z) return false;
        return true;
    };
    auto build_controls = [&](const std::vector<V3>& middle) {
        controls.clear(); controls.push_back(start);
        for (const V3& point : middle) if (dist(controls.back(), point) > 1e-9) controls.push_back(point);
        if (dist(controls.back(), end) > 1e-9) controls.push_back(end);
    };
    auto finish = [&](const std::vector<V3>& middle) {
        build_controls(middle);
        bool geometry = false;
        if (fillet(controls, radius, collision, path, bends, length, &geometry)) return true;
        if (!geometry) { other_failed = true; return false; }
        turn_failed = true;
        if (turn_candidates.size() < 8 &&
            std::none_of(turn_candidates.begin(), turn_candidates.end(),
                         [&](const std::vector<V3>& c) { return same_points(c, middle); }))
            turn_candidates.push_back(middle);
        return false;
    };
    const bool direct_found = collision.clear(sp, ep) && finish({sp, ep});
    std::vector<V3> direct_controls, direct_path;
    std::vector<Bend> direct_bends;
    if (direct_found && !preferred.empty()) {
        direct_controls = controls; direct_path = path; direct_bends = bends;
    }
    const double direct_length = length;
    bool found = direct_found && preferred.empty();
    bool exhausted = false;
    auto search = [&]() -> json {
        if (found) return nullptr;
        // Finite search domain includes scene bounds, with a bend/clearance margin.
        V3 lo{std::min(sp.x, ep.x), std::min(sp.y, ep.y), std::min(sp.z, ep.z)};
        V3 hi{std::max(sp.x, ep.x), std::max(sp.y, ep.y), std::max(sp.z, ep.z)};
        if (obstacles.has_geometry) for (int i = 0; i < 3; ++i) {
            lo[i] = std::min(lo[i], obstacles.lo[i]); hi[i] = std::max(hi[i], obstacles.hi[i]);
        }
        const double margin = std::max(4 * grid, 2 * radius + obstacles.thr_max + 2 * grid);
        for (int i = 0; i < 3; ++i) { lo[i] -= margin; hi[i] += margin; }
        std::array<int, 3> low{}, high{};
        for (int i = 0; i < 3; ++i) {
            const double a = std::floor((lo[i] - sp[i]) / grid), b = std::ceil((hi[i] - sp[i]) / grid);
            if (!std::isfinite(a) || !std::isfinite(b) || a < -1000000 || b > 1000000) {
                if (direct_found) {
                    found = true;
                    break;
                }
                return fail("budget", "search domain exceeds grid coordinate limit; increase grid_mm", budget_detail());
            }
            low[i] = static_cast<int>(a); high[i] = static_cast<int>(b);
        }
        if (!found) {
        auto point = [&](Key k) { return sp + V3{k.x * grid, k.y * grid, k.z * grid}; };
        std::priority_queue<Entry, std::vector<Entry>, Greater> open;
        std::unordered_map<Key, Record, Hash> records;
        const Key origin{0, 0, 0}; records[origin].g = 0;
        uint64_t order = 0;
        size_t dp_attempts = 0;
        open.push({0, 0, origin, order++});
        while (!open.empty() && expanded < budget) {
            Entry current = open.top(); open.pop();
            auto it = records.find(current.k);
            if (it == records.end() || it->second.closed || current.g > it->second.g) continue;
            it->second.closed = true; ++expanded;
            const V3 here = point(current.k);
            const bool has_incoming = it->second.has_parent;
            const V3 incoming = has_incoming ? unit(here - point(it->second.parent)) : V3{};
            if (dist(here, ep) <= 2 * grid && collision.clear(here, ep)) {
                std::vector<V3> reverse;
                Key k = current.k;
                for (;;) {
                    reverse.push_back(point(k));
                    if (reverse.size() > 4096) { exhausted = true; break; }
                    const Record& rec = records.find(k)->second;
                    if (!rec.has_parent) break;
                    k = rec.parent;
                }
                if (exhausted) break;
                std::reverse(reverse.begin(), reverse.end()); reverse.push_back(ep);
                std::vector<size_t> preferred_prefix(reverse.size() + 1, 0);
                if (!preferred.empty()) {
                    for (size_t q = 0; q < reverse.size(); ++q) {
                        bool near = false;
                        for (const auto& segment : preferred)
                            if (point_segment_distance(reverse[q], segment.first, segment.second) <= grid) { near = true; break; }
                        preferred_prefix[q + 1] = preferred_prefix[q] + (near ? 1 : 0);
                    }
                }
                // String-pull for few corners; test rounded geometry, not just grid edges.
                // Scanning from the far end cost O(corners x lattice points) clear() tests per
                // attempt (and an attempt runs for every node near the end). Try the final point
                // directly, else scan forward and stop after kPullLookahead consecutive blocked
                // candidates past the last visible one: O(lattice points) per attempt.
                // With prefer_feasible the next control is the furthest visible candidate whose
                // turn the fillet can realise (trim fits the incoming leg minus the previous
                // corner's trim, the candidate leg and, at the final point, the end stub); only
                // when none exists is it the furthest visible one.
                constexpr size_t kPullLookahead = 32;
                const size_t last = reverse.size() - 1;
                auto preference_ok = [&](size_t a, size_t b) {
                    if (preferred.empty() || preferred_prefix[b] <= preferred_prefix[a + 1]) return true;
                    const V3 midpoint = (reverse[a] + reverse[b]) * 0.5;
                    for (const auto& segment : preferred)
                        if (point_segment_distance(midpoint, segment.first, segment.second) <= 1.5 * grid) return true;
                    return false;
                };
                auto pull = [&](bool prefer_feasible, std::vector<V3>& simplified) {
                    simplified.assign(1, reverse.front());
                    V3 prev = start;
                    double prev_trim = 0;
                    for (size_t i = 0; i < last;) {
                        const V3 cur = reverse[i];
                        auto visible = [&](size_t j) {
                            return preference_ok(i, j) && collision.clear(reverse[i], reverse[j], radius);
                        };
                        auto feasible = [&](size_t j, double& trim_out) {
                            trim_out = 0;
                            const V3& q = reverse[j];
                            if (dist(prev, cur) > 1e-9) {
                                if (!corner_trim(prev, cur, q, radius, trim_out)) return false;
                                if (prev_trim + trim_out > dist(prev, cur) - slack) return false;
                            }
                            if (j == last && has_ed) {
                                double trim_q = 0;
                                if (!corner_trim(cur, q, end, radius, trim_q)) return false;
                                return trim_out + trim_q <= dist(cur, q) - slack && trim_q <= dist(q, end) - slack;
                            }
                            return trim_out <= dist(cur, q) - slack;
                        };
                        size_t visible_best = i + 1, feasible_best = 0;
                        double trim_cur = 0;
                        if (prefer_feasible && feasible(i + 1, trim_cur)) feasible_best = i + 1;
                        if (last > i + 1 && visible(last)) {
                            visible_best = last;
                            if (prefer_feasible && feasible(last, trim_cur)) feasible_best = last;
                        }
                        if (visible_best != last || (prefer_feasible && feasible_best != last)) {
                            size_t misses = 0;
                            for (size_t j = i + 2; j < last && misses < kPullLookahead; ++j) {
                                if (!visible(j)) { ++misses; continue; }
                                misses = 0;
                                visible_best = std::max(visible_best, j);
                                if (prefer_feasible && feasible(j, trim_cur)) feasible_best = j;
                            }
                        }
                        const size_t best = prefer_feasible && feasible_best != 0 ? feasible_best : visible_best;
                        feasible(best, trim_cur);
                        prev = cur; prev_trim = trim_cur;
                        simplified.push_back(reverse[best]); i = best;
                        if (simplified.size() > 254) return false;
                    }
                    return true;
                };
                // Principled selection: the shortest feasible control subsequence of the lattice
                // path, by Dijkstra over (previous control, current control) states. An edge is a
                // lattice step or a chord that clears the obstacles (cached); a corner needs its
                // trim to fit the incoming leg minus the previous corner's trim and the outgoing leg
                // (the end stub at the final point), and its fillet arc to clear the obstacles. A
                // label's trim is carried so labels with a larger trim must be cheaper to survive.
                constexpr size_t kDpWindow = kPullLookahead;
                auto dp_pull = [&](std::vector<V3>& out) {
                    std::unordered_map<size_t, char> edge_cache;
                    auto edge_ok = [&](size_t b, size_t c) {
                        if (c == b + 1) return true;
                        if (!preference_ok(b, c)) return false;
                        const size_t key = b * (last + 1) + c;
                        auto hit = edge_cache.find(key);
                        if (hit == edge_cache.end())
                            hit = edge_cache.emplace(key, collision.clear(reverse[b], reverse[c]) ? 1 : 0).first;
                        return hit->second != 0;
                    };
                    struct Node { long long a; size_t b; double cost, trim; int parent; };
                    std::vector<Node> nodes;
                    nodes.push_back({-1, 0, 0.0, 0.0, -1});
                    using Item = std::pair<double, int>;
                    std::priority_queue<Item, std::vector<Item>, std::greater<Item>> queue;
                    queue.push({0.0, 0});
                    std::unordered_map<size_t, std::vector<std::pair<double, double>>> settled;
                    size_t expansions = 0;
                    while (!queue.empty()) {
                        const double cost = queue.top().first;
                        const int id = queue.top().second;
                        queue.pop();
                        const Node cur = nodes[static_cast<size_t>(id)];
                        auto& done = settled[static_cast<size_t>(cur.a + 1) * (last + 1) + cur.b];
                        bool dominated = false;
                        for (const auto& d : done)
                            if (d.first <= cost + 1e-9 && d.second <= cur.trim + 1e-9) { dominated = true; break; }
                        if (dominated) continue;
                        done.push_back({cost, cur.trim});
                        if (cur.b == last) {
                            out.clear();
                            for (int at = id; at >= 0; at = nodes[static_cast<size_t>(at)].parent)
                                out.push_back(reverse[nodes[static_cast<size_t>(at)].b]);
                            std::reverse(out.begin(), out.end());
                            return out.size() <= 254;
                        }
                        if (++expansions > 4000) return false;
                        const V3 prev_point = cur.a < 0 ? start : reverse[static_cast<size_t>(cur.a)];
                        const V3 here_point = reverse[cur.b];
                        const bool corner = dist(prev_point, here_point) > 1e-9;
                        auto try_next = [&](size_t c) {
                            if (!edge_ok(cur.b, c)) return;
                            const V3& q = reverse[c];
                            double t_b = 0;
                            if (corner) {
                                if (!corner_trim(prev_point, here_point, q, radius, t_b)) return;
                                if (cur.trim + t_b > dist(prev_point, here_point) - slack) return;
                            }
                            const double leg = dist(here_point, q);
                            double t_c = 0;
                            if (c == last && has_ed) {
                                if (!corner_trim(here_point, q, end, radius, t_c)) return;
                                if (t_b + t_c > leg - slack || t_c > dist(q, end) - slack) return;
                            } else if (t_b > leg - slack) {
                                return;
                            }
                            if (corner && !arc_clear(collision, prev_point, here_point, q, radius, t_b)) return;
                            if (c == last && has_ed && !arc_clear(collision, here_point, q, end, radius, t_c)) return;
                            nodes.push_back({static_cast<long long>(cur.b), c, cost + leg + 1e-3 * grid, t_b, id});
                            queue.push({nodes.back().cost, static_cast<int>(nodes.size() - 1)});
                        };
                        for (size_t c = cur.b + 1; c <= last && c <= cur.b + kDpWindow; ++c) try_next(c);
                        if (cur.b + kDpWindow < last) try_next(last);
                    }
                    return false;
                };
                std::vector<V3> simplified, shortest, furthest;
                if (!pull(true, simplified)) { exhausted = true; break; }
                if (finish(simplified)) { found = true; break; }
                if (dp_attempts < 16) {
                    ++dp_attempts;
                    if (dp_pull(shortest) && !same_points(shortest, simplified) && finish(shortest)) { found = true; break; }
                }
                if (!pull(false, furthest)) { exhausted = true; break; }
                if (!same_points(furthest, simplified) && finish(furthest)) { found = true; break; }
            }
            for (int x = -1; x <= 1; ++x) for (int y = -1; y <= 1; ++y) for (int z = -1; z <= 1; ++z) {
                if (x == 0 && y == 0 && z == 0) continue;
                Key next{current.k.x + x, current.k.y + y, current.k.z + z};
                if (next.x < low[0] || next.x > high[0] || next.y < low[1] || next.y > high[1] || next.z < low[2] || next.z > high[2]) continue;
                V3 there = point(next);
                if (!collision.clear(here, there)) continue;
                double factor = 1;
                for (const auto& segment : preferred) if (point_segment_distance((here + there) * 0.5, segment.first, segment.second) <= grid) { factor = 0.8; break; }
                const double turn_cost = has_incoming ? 0.2 * radius * (1 - std::clamp(dot(incoming, unit(there - here)), -1.0, 1.0)) : 0;
                const double g = current.g + dist(here, there) * factor + turn_cost;
                auto ni = records.find(next);
                if (ni != records.end() && (ni->second.closed || ni->second.g <= g)) continue;
                if (ni == records.end()) {
                    // Bound memory independently of branching factor and stale queue entries.
                    if (records.size() >= budget) { exhausted = true; break; }
                    ni = records.emplace(next, Record{}).first;
                }
                ni->second.g = g; ni->second.parent = current.k; ni->second.has_parent = true;
                if (open.size() >= 500000) { exhausted = true; break; }
                open.push({g + 0.8 * dist(there, ep), g, next, order++});
            }
            if (exhausted) break;
        }
        exhausted = exhausted || (!open.empty() && expanded >= budget);
        }
        return nullptr;
    };
    if (!found) {
        json early = search();
        if (!early.is_null()) return early;
        // No lattice subsequence was feasible: an automatically chosen grid may simply be too
        // coarse for the bend radius, so retry once at half the grid within the remaining budget.
        if (!found && !direct_found && turn_failed && !p.contains("grid_mm") && expanded < budget &&
            grid * 0.5 >= 1e-4) {
            grid *= 0.5;
            exhausted = false;
            early = search();
            if (!early.is_null()) return early;
        }
    }
    if (!found && !direct_found) {
        // Last resort: repair the straight polyline and each polyline rejected for turn geometry
        // by replacing a corner with a detour (see repair_controls). The straight polyline is
        // included even when an obstacle blocks it: a detour can route around the obstacle.
        std::vector<std::vector<V3>> repair_list;
        repair_list.push_back({sp, ep});
        for (const auto& middle : turn_candidates)
            if (!same_points(middle, repair_list.front())) repair_list.push_back(middle);
        for (const auto& middle : repair_list) {
            build_controls(middle);
            if (repair_controls(controls, radius, collision, path, bends, length, 3, has_sd, has_ed)) { found = true; break; }
        }
    }
    if (!found && direct_found) {
        found = true;
        controls = direct_controls; path = direct_path; bends = direct_bends; length = direct_length;
    }
    if (!found) {
        if (exhausted) return fail("budget", "routing search node budget exhausted", budget_detail());
        if (turn_failed && !other_failed)
            return fail("no_path",
                        "no route satisfies min_bend_radius_mm: every candidate was rejected only because a turn could not be filleted (no obstacle blocked it)",
                        {{"reason", "turn_infeasible"}, {"min_bend_radius_mm", radius}, {"exit_stub_mm", stub},
                         {"hint", "an endpoint direction may point away from the target; the exit stub is 2 x min_bend_radius_mm and each bend needs tangent legs of R*tan(angle/2); give the endpoints more room, change the endpoint direction, or lower min_bend_radius_mm"}});
        return fail("no_path", "no feasible rounded path found in the bounded search domain");
    }
    json result = {{"ok", true}, {"path", json::array()}, {"control_points", json::array()},
                   {"bends", json::array()}, {"length_mm", length}, {"grid_mm", grid}, {"exit_stub_mm", stub},
                   {"expanded_nodes", expanded}, {"warnings", obstacles.warnings},
                   {"clearance_basis", "triangle-mesh segment distances; sampled arcs conservatively checked with sagitta; not exact B-rep clearance"}};
    result["assumptions"] = json::array({
        p.contains("grid_mm") ? "Caller-specified grid resolution." : "Grid resolution automatically chosen from diameter, bend radius and endpoint distance.",
        "Bounded grid search penalizes turns by 0.2 times bend radius times (1 - cosine); control points are chosen among lattice points whose bends the tangent legs can hold, and when every candidate polyline fails only on turn geometry a detour (overshoot along the incoming leg, quarter turn, run, turn onto the next leg) replaces the offending corner; search and rounding are heuristic and may miss feasible routes.",
        "Circular bends are sampled at no more than 10 degrees; clearance is against tessellated triangle surfaces, not exact CAD solids.",
        "Endpoints with a direction get a straight exit stub of 2 x min_bend_radius_mm (reported as exit_stub_mm), independent of grid and of the other endpoint; endpoint directions point outward from both endpoints."
    });
    result["confidence"] = "triangle-mesh approximation; bounded heuristic search";
    for (const V3& v : path) result["path"].push_back(jvec(v));
    for (const V3& v : controls) result["control_points"].push_back(jvec(v));
    for (const Bend& b : bends) result["bends"].push_back({{"at", jvec(b.at)}, {"angle_deg", b.angle * 180 / kPi}, {"radius_mm", b.radius}});
    double nearest = kInf;
    for (size_t i = 1; i < path.size(); ++i) nearest = std::min(nearest, obstacles.bvh.nearest_segment(path[i - 1], path[i], kInf).dist);
    result["min_clearance_mm"] = std::isfinite(nearest) ? json(nearest - od / 2) : json(nullptr);
    if (!std::isfinite(nearest)) result["warnings"].push_back("No obstacle triangles: clearance was not evaluated.");
    result["warnings"].push_back("Path points sample circular bends; length and bend radii describe the underlying tangent arcs, not the chord polyline.");
    if (relax > 0) result["warnings"].push_back("Endpoint relaxation waives clearance inflation only near each endpoint and only for its nearest owner when its envelope contains that endpoint; solid intersections remain forbidden.");
    if (!preferred.empty()) result["warnings"].push_back("Preferred paths discount nearby grid edges by 20%; smoothing heuristically preserves preference, and a clear direct path is used if preferred search fails.");
    return result;
}
} // namespace chudware::harness
