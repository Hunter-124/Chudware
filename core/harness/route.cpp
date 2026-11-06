#include "harness/route.h"
#include "harness/obstacles.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <limits>
#include <queue>
#include <string>
#include <unordered_map>
#include <vector>

namespace chudware::harness {
namespace {
using json = nlohmann::json;
json fail(const char* code, const std::string& error) {
    return {{"ok", false}, {"code", code}, {"error", error}};
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
bool number(const json& p, const char* key, double def, double lo, double hi, double& out) {
    if (!p.contains(key)) { out = def; return true; }
    if (!p[key].is_number()) return false;
    out = p[key].get<double>();
    return std::isfinite(out) && out >= lo && out <= hi;
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
    bool point_inside(const V3& p) const {
        return obs.bvh.first_containing_owner(p, obs.owner_count()) >= 0;
    }
    bool clear(const V3& a, const V3& b, double extra = 0) const {
        if (obs.bvh.empty()) return true;
        // Route starts outside solids; every continuous edge is tested against
        // the surface, so it cannot enter one without a detected crossing.
        if (relax == 0 && extra == 0)
            return !obs.bvh.any_within_segment(a, b, strict, std::max(1e-8, obs.thr_max));
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
            if (obs.bvh.any_within_segment(lerp(a, b, cuts[c - 1]), lerp(a, b, cuts[c]),
                                           workspace, max_thr)) return false;
        }
        return true;
    }
};

double point_segment_distance(const V3& p, const V3& a, const V3& b) {
    const V3 d = b - a;
    const double n = norm2(d);
    const double t = n > 0 ? std::clamp(dot(p - a, d) / n, 0.0, 1.0) : 0;
    return dist(p, a + d * t);
}

struct Bend { V3 at; double angle, radius; };
// Replace each polyline corner with an exact circular fillet, sampling it at
// <= 10 degrees. The sagitta is added to the obstacle threshold during checks.
bool fillet(const std::vector<V3>& ctrl, double r, const Collision& collision,
            std::vector<V3>& path, std::vector<Bend>& bends, double& length) {
    const size_t n = ctrl.size();
    if (n < 2 || n > 256) return false;
    std::vector<double> trim(n, 0), angle(n, 0);
    for (size_t i = 1; i + 1 < n; ++i) {
        V3 u = unit(ctrl[i] - ctrl[i - 1]), v = unit(ctrl[i + 1] - ctrl[i]);
        const double cosine = std::clamp(dot(u, v), -1.0, 1.0);
        angle[i] = std::acos(cosine);
        if (angle[i] < 1e-8) continue;
        if (r <= 0 || cosine < -0.999999) return false;
        trim[i] = r * std::tan(angle[i] / 2);
        if (!std::isfinite(trim[i])) return false;
    }
    for (size_t i = 0; i + 1 < n; ++i)
        if (trim[i] + trim[i + 1] > dist(ctrl[i], ctrl[i + 1]) - 1e-8) return false;
    path.clear(); bends.clear(); length = 0; path.push_back(ctrl.front());
    auto add = [&](const V3& p, double extra) {
        if (dist(path.back(), p) < 1e-10) return true;
        if (path.size() >= 4096) return false;
        if (!collision.clear(path.back(), p, extra)) return false;
        path.push_back(p); return true;
    };
    for (size_t i = 1; i + 1 < n; ++i) {
        if (trim[i] == 0) continue;
        const V3 u = unit(ctrl[i] - ctrl[i - 1]), v = unit(ctrl[i + 1] - ctrl[i]);
        const V3 entry = ctrl[i] - u * trim[i], exit = ctrl[i] + v * trim[i];
        const V3 axis = unit(cross(u, v));
        const V3 center = entry + cross(axis, u) * r;
        if (!add(entry, 0)) return false;
        const V3 radial = entry - center;
        const int steps = std::max(1, static_cast<int>(std::ceil(angle[i] / (kPi / 18))));
        const double sagitta = r * (1 - std::cos(angle[i] / (2 * steps)));
        for (int j = 1; j <= steps; ++j) {
            const double a = angle[i] * j / steps;
            const V3 p = j == steps ? exit : center + radial * std::cos(a) + cross(axis, radial) * std::sin(a);
            if (!add(p, sagitta)) return false;
        }
        bends.push_back({ctrl[i], angle[i], r});
    }
    if (!add(ctrl.back(), 0)) return false;
    // Length of the intended tangent-line / circular-arc route, not the chord sum.
    for (size_t i = 0; i + 1 < n; ++i) length += dist(ctrl[i], ctrl[i + 1]) - trim[i] - trim[i + 1];
    for (size_t i = 1; i + 1 < n; ++i) length += r * angle[i];
    return true;
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
    if (!p.contains("od_mm") || !p.contains("min_bend_radius_mm") ||
        !number(p, "od_mm", 0, 1e-6, 1e6, od) ||
        !number(p, "min_bend_radius_mm", 0, 1e-6, 1e7, radius) ||
        !number(p, "clearance_mm", 2, 0, 1e6, clearance) ||
        !number(p, "endpoint_relax_mm", 0, 0, 1e6, relax) ||
        !number(p, "grid_mm", std::max({1.0, od, radius / 2, dist(start, end) / 40}), 1e-4, 1e7, grid) ||
        !number(p, "max_nodes", 50000, 1, 200000, max_nodes_d) || std::floor(max_nodes_d) != max_nodes_d)
        return fail("bad_args", "invalid od_mm, min_bend_radius_mm, clearance_mm, endpoint_relax_mm, grid_mm or max_nodes");
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
    auto blocked_endpoint = [&](const V3& pt, bool first) {
        if (collision.point_inside(pt)) return true;
        std::vector<double> threshold = obstacles.thr;
        const auto& exempt = first ? collision.start_owner : collision.end_owner;
        for (size_t i = 0; i < threshold.size(); ++i)
            threshold[i] = relax > 0 && exempt[i] ? 1e-8 : std::max(1e-8, threshold[i]);
        return obstacles.bvh.any_within_point(pt, threshold, std::max(1e-8, obstacles.thr_max));
    };
    if (blocked_endpoint(start, true)) return fail("start_blocked", "start is inside an obstacle or its clearance envelope");
    if (blocked_endpoint(end, false)) return fail("end_blocked", "end is inside an obstacle or its clearance envelope");
    const double stub = std::max(2 * radius, 2 * grid);
    const V3 sp = has_sd ? start + sd * stub : start;
    const V3 ep = has_ed ? end + ed * stub : end;
    if ((has_sd && !collision.clear(start, sp)) || (has_ed && !collision.clear(ep, end)))
        return fail("no_path", "endpoint direction stub intersects an obstacle or clearance envelope");

    std::vector<V3> controls, path;
    std::vector<Bend> bends;
    double length = 0;
    size_t expanded = 0;
    auto finish = [&](const std::vector<V3>& middle) {
        controls.clear(); controls.push_back(start);
        for (const V3& point : middle) if (dist(controls.back(), point) > 1e-9) controls.push_back(point);
        if (dist(controls.back(), end) > 1e-9) controls.push_back(end);
        return fillet(controls, radius, collision, path, bends, length);
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
    if (!found) {
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
                return fail("budget", "search domain exceeds grid coordinate limit; increase grid_mm");
            }
            low[i] = static_cast<int>(a); high[i] = static_cast<int>(b);
        }
        if (!found) {
        auto point = [&](Key k) { return sp + V3{k.x * grid, k.y * grid, k.z * grid}; };
        std::priority_queue<Entry, std::vector<Entry>, Greater> open;
        std::unordered_map<Key, Record, Hash> records;
        const Key origin{0, 0, 0}; records[origin].g = 0;
        uint64_t order = 0;
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
                std::vector<V3> simplified{reverse.front()};
                for (size_t i = 0; i + 1 < reverse.size();) {
                    size_t j = reverse.size() - 1;
                    while (j > i + 1) {
                        bool preserves_preference = true;
                        if (!preferred.empty() && preferred_prefix[j] > preferred_prefix[i + 1]) {
                            preserves_preference = false;
                            const V3 midpoint = (reverse[i] + reverse[j]) * 0.5;
                            for (const auto& segment : preferred)
                                if (point_segment_distance(midpoint, segment.first, segment.second) <= 1.5 * grid) {
                                    preserves_preference = true; break;
                                }
                        }
                        if (preserves_preference && collision.clear(reverse[i], reverse[j], radius)) break;
                        --j;
                    }
                    simplified.push_back(reverse[j]); i = j;
                    if (simplified.size() > 254) { exhausted = true; break; }
                }
                if (exhausted) break;
                if (finish(simplified)) { found = true; break; }
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
    }
    if (!found && direct_found) {
        found = true;
        controls = direct_controls; path = direct_path; bends = direct_bends; length = direct_length;
    }
    if (!found) return fail(exhausted ? "budget" : "no_path", exhausted ? "routing search node budget exhausted" : "no feasible rounded path found in the bounded search domain");
    json result = {{"ok", true}, {"path", json::array()}, {"control_points", json::array()},
                   {"bends", json::array()}, {"length_mm", length}, {"grid_mm", grid},
                   {"expanded_nodes", expanded}, {"warnings", obstacles.warnings},
                   {"clearance_basis", "triangle-mesh segment distances; sampled arcs conservatively checked with sagitta; not exact B-rep clearance"}};
    result["assumptions"] = json::array({
        p.contains("grid_mm") ? "Caller-specified grid resolution." : "Grid resolution automatically chosen from diameter, bend radius and endpoint distance.",
        "Bounded grid search penalizes turns by 0.2 times bend radius times (1 - cosine); search and rounding are heuristic and may miss feasible routes.",
        "Circular bends are sampled at no more than 10 degrees; clearance is against tessellated triangle surfaces, not exact CAD solids.",
        "Endpoint directions point outward from both endpoints."
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
