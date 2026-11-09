// core/harness/path_check.cpp — discrete bend and tessellated-solid clearance checks.
#include "harness/path_check.h"

#include <algorithm>
#include <cmath>
#include <string>
#include <utility>
#include <vector>

#include "harness/obstacles.h"

namespace chudware::harness {
namespace {
using json = nlohmann::json;
constexpr size_t kMaxPoints = 4096;
constexpr size_t kMaxFindings = 256;
// Limits keep the distance and cross-product calculations well within double range.
constexpr double kMaxCoordinate = 1e9;
constexpr double kMinSegment = 1e-9;
constexpr double kContactTolerance = 1e-9;
// A measured bend satisfies the requirement when radius >= min_bend * (1 - kBendTolerance);
// this absorbs floating-point noise of the circumradius of exact arc samples.
constexpr double kBendTolerance = 1e-6;
// Interior vertices whose adjacent unit directions have |cross| <= this (same direction)
// are ignored when estimating bends, so points inserted on a chord change nothing.
constexpr double kCollinearSine = 1e-9;

json error(const std::string& message) {
    return {{"ok", false}, {"code", "bad_args"}, {"error", message}};
}

bool number(const json& value, double& out) {
    if (!value.is_number()) return false;
    out = value.get<double>();
    return std::isfinite(out);
}

bool vector3(const json& value, V3& out) {
    if (!value.is_array() || value.size() != 3) return false;
    for (int i = 0; i < 3; ++i) {
        if (!number(value[static_cast<size_t>(i)], out[i]) ||
            std::abs(out[i]) > kMaxCoordinate) return false;
    }
    return true;
}

json point(const V3& p) { return json::array({p.x, p.y, p.z}); }

json owner_id(const ObstacleSet& obstacles, int owner) {
    if (owner < 0 || static_cast<size_t>(owner) >= obstacles.owners.size()) return nullptr;
    const auto& o = obstacles.owners[static_cast<size_t>(owner)];
    return o.keepout ? json(o.name) : json(o.body_id);
}
}  // namespace

nlohmann::json harness_path_check(const nlohmann::json& params, const Scene& scene) {
    if (!params.is_object()) return error("parameters must be an object");
    if (!params.contains("path") || !params["path"].is_array() ||
        params["path"].size() < 2 || params["path"].size() > kMaxPoints)
        return error("path must contain 2 to 4096 [x,y,z] points");
    double od = 0, min_bend = 0, clearance = 2;
    if (!params.contains("od_mm") || !number(params["od_mm"], od) || od <= 0 ||
        od > kMaxCoordinate)
        return error("od_mm must be finite, positive, and at most 1e9");
    if (!params.contains("min_bend_radius_mm") ||
        !number(params["min_bend_radius_mm"], min_bend) || min_bend < 0 ||
        min_bend > kMaxCoordinate)
        return error("min_bend_radius_mm must be finite and between 0 and 1e9");
    if (params.contains("clearance_mm") &&
        (!number(params["clearance_mm"], clearance) || clearance < 0 ||
         clearance > kMaxCoordinate))
        return error("clearance_mm must be finite and between 0 and 1e9");

    std::vector<V3> path;
    path.reserve(params["path"].size());
    double length = 0;
    for (const auto& p : params["path"]) {
        V3 v;
        if (!vector3(p, v)) return error("path points must be finite [x,y,z] within +/-1e9 mm");
        if (!path.empty()) {
            const double d = dist(path.back(), v);
            if (d <= kMinSegment) return error("consecutive path points must be distinct");
            length += d;
        }
        path.push_back(v);
    }
    // Optional `junctions`: strictly interior path indices where the wire leaves a bundle.
    std::vector<char> is_junction(path.size(), 0);
    if (params.contains("junctions")) {
        const auto& list = params["junctions"];
        if (!list.is_array() || list.size() > kMaxPoints)
            return error("junctions must be an array of at most 4096 integer path indices");
        for (const auto& item : list) {
            if (!item.is_number_integer() ||
                (!item.is_number_unsigned() && item.get<long long>() < 0))
                return error("junctions entries must be integer path indices");
            const auto index = item.get<unsigned long long>();
            if (index < 1 || index + 2 > path.size())
                return error("junctions entries must be strictly interior path indices (1 to path.size()-2)");
            is_junction[static_cast<size_t>(index)] = 1;
        }
    }

    // Optional `report_range: [first, last]`: estimation runs on the whole path (arcs cut by a
    // split point keep their context); findings and minima cover only that range.
    size_t range_first = 0, range_last = path.size() - 1;
    if (params.contains("report_range")) {
        const auto& range = params["report_range"];
        const char* const range_error =
            "report_range must be [first, last] integer vertex indices with 0 <= first <= last <= len(path)-1";
        if (!range.is_array() || range.size() != 2) return error(range_error);
        unsigned long long ends[2];
        for (int k = 0; k < 2; ++k) {
            if (!range[k].is_number_integer() ||
                (!range[k].is_number_unsigned() && range[k].get<long long>() < 0))
                return error(range_error);
            ends[k] = range[k].get<unsigned long long>();
        }
        if (ends[0] > ends[1] || ends[1] >= path.size()) return error(range_error);
        range_first = static_cast<size_t>(ends[0]);
        range_last = static_cast<size_t>(ends[1]);
    }
    double report_length = 0;
    for (size_t i = range_first; i < range_last; ++i) report_length += dist(path[i], path[i + 1]);
    ObstacleSet obstacles;
    const std::string obstacle_error = build_obstacles(params, scene, od, clearance, obstacles);
    if (!obstacle_error.empty()) return error(obstacle_error);

    // Bends are estimated on the path with collinear interior vertices removed;
    // `orig` maps each kept vertex back to the caller's original index.
    std::vector<size_t> orig;
    orig.push_back(0);
    for (size_t i = 1; i + 1 < path.size(); ++i) {
        const V3 in = unit(path[i] - path[i - 1]);
        const V3 out = unit(path[i + 1] - path[i]);
        if (norm(cross(in, out)) <= kCollinearSine && dot(in, out) > 0) continue;
        orig.push_back(i);
    }
    orig.push_back(path.size() - 1);
    std::vector<V3> rp;
    rp.reserve(orig.size());
    for (size_t i : orig) rp.push_back(path[i]);
    const size_t m = rp.size();

    struct Turn {
        bool present = false;
        bool junction = false;
        bool arc_sample = false;
        double radius = 0;
        double sine = 0;
        double cosine = 1;
        double angle = 0;
        V3 axis;
    };
    std::vector<Turn> turns(m);
    for (size_t i = 1; i + 1 < m; ++i) {
        const V3 in = unit(rp[i] - rp[i - 1]);
        const V3 out = unit(rp[i + 1] - rp[i]);
        const V3 normal = cross(in, out);
        Turn& t = turns[i];
        t.sine = norm(normal);
        t.cosine = std::clamp(dot(in, out), -1.0, 1.0);
        if (t.sine <= 1e-12 && t.cosine >= 0) continue;
        t.present = true;
        t.angle = std::atan2(t.sine, t.cosine);
        if (t.sine > 1e-12) {
            t.axis = normal * (1.0 / t.sine);
            t.radius = dist(rp[i - 1], rp[i + 1]) / (2 * t.sine);
        }
    }
    for (size_t i = 1; i + 1 < m; ++i) turns[i].junction = is_junction[orig[i]] != 0;
    // Two adjacent triples with the same oriented plane and circumradius are
    // evidence of a sampled circular arc. A single triple cannot distinguish a
    // sharp polyline kink from a sampled curve, so it is treated conservatively.
    for (size_t i = 1; i + 2 < m; ++i) {
        Turn& a = turns[i];
        Turn& b = turns[i + 1];
        if (a.present && b.present && !a.junction && !b.junction && a.radius > 0 && b.radius > 0 &&
            dot(a.axis, b.axis) > 0.995 &&
            std::abs(a.radius - b.radius) <= 0.02 * std::max(a.radius, b.radius)) {
            a.arc_sample = b.arc_sample = true;
        }
    }
    std::vector<char> sampled(m, 0);
    std::vector<double> estimate(m, 0.0);
    // Router-style bends: a circular arc tangent to its straight legs, sampled by n >= 2 turning
    // vertices (n - 1 >= 1 equal chords c), appears as n consecutive same-axis turning vertices
    // whose end turns are both theta (half a chord step) and whose interior turns are 2 theta.
    // Then R = c / (2 sin theta) exactly. Shallow bends sampled by one or two chords (n = 2, 3)
    // have no two adjacent equal-radius triples, so this pattern is their only evidence;
    // for longer arcs it also avoids the long-leg bias of the boundary triples.
    auto eligible = [&](size_t k) {
        return k + 1 < m && turns[k].present && !turns[k].junction && turns[k].radius > 0 &&
               turns[k].angle <= kPi / 2;
    };
    auto similar = [](double a, double b, double rel) {
        return std::abs(a - b) <= rel * std::max(a, b);
    };
    for (size_t i = 1; i + 1 < m;) {
        if (!eligible(i)) { ++i; continue; }
        const double theta = turns[i].angle;
        const double chord = dist(rp[i], rp[i + 1]);
        size_t best = 0;
        for (size_t n = 2; i + n < m; ++n) {
            const size_t last = i + n - 1;
            // The previous last vertex becomes an interior one: it must turn by 2 theta.
            if (n > 2 && !similar(turns[last - 1].angle, 2 * theta, 0.02)) break;
            if (!eligible(last) || dot(turns[i].axis, turns[last].axis) < 0.995) break;
            if (!similar(dist(rp[last - 1], rp[last]), chord, 0.02)) break;
            if (similar(turns[last].angle, theta, 0.02)) best = n;
        }
        // A pair whose adjacent chords equal its own chord is indistinguishable from
        // consecutive samples of a longer uniform arc (all turns equal); leave those to the
        // adjacent-triple rule below, which measures the true circumradius.
        if (best == 2 && (similar(dist(rp[i - 1], rp[i]), chord, 0.02) ||
                          similar(dist(rp[i + 1], rp[i + 2]), chord, 0.02)))
            best = 0;
        if (best == 0) { ++i; continue; }
        for (size_t k = i; k < i + best; ++k) {
            sampled[k] = 1;
            estimate[k] = chord / (2 * std::sin(theta));
        }
        i += best;
    }
    for (size_t i = 1; i + 1 < m; ++i) {
        const Turn& t = turns[i];
        if (!t.present || t.junction || sampled[i]) continue;
        // The tangent-to-arc boundary triple can include a long straight leg
        // and consequently has a different circumradius. It is arc-supported
        // only when its smaller turn continues an adjacent established arc.
        auto tangent_to = [&](const Turn& neighbor) {
            return neighbor.arc_sample && t.radius > 0 &&
                   t.cosine >= neighbor.cosine && dot(t.axis, neighbor.axis) > 0.995;
        };
        if (t.arc_sample || tangent_to(turns[i - 1]) || tangent_to(turns[i + 1])) {
            sampled[i] = 1;
            estimate[i] = t.radius;
        }
    }
    json bends = json::array();
    size_t bend_count = 0;
    double min_found = kInf;
    for (size_t i = 1; i + 1 < m; ++i) {
        const Turn& t = turns[i];
        if (!t.present) continue;
        if (orig[i] < range_first || orig[i] > range_last) continue;
        double radius = sampled[i] ? estimate[i] : 0.0;
        // Geometric upper bound for one isolated tangent fillet. Adjacent
        // bends and obstacles can reduce the actually available radius.
        double fillet = t.sine > 1e-12
            ? std::min(dist(rp[i - 1], rp[i]), dist(rp[i], rp[i + 1])) *
                  (1 + t.cosine) / t.sine
            : 0.0;
        if (t.junction) {
            // Junction: the wire leaves the bundle with its own bend, so the corner is
            // judged by the tangent fillet the adjacent straight runs can hold. A run
            // shared with another junction vertex is split between the two corners.
            const double run_in = dist(rp[i - 1], rp[i]) * (turns[i - 1].junction ? 0.5 : 1.0);
            const double run_out = dist(rp[i], rp[i + 1]) * (turns[i + 1].junction ? 0.5 : 1.0);
            fillet = t.sine > 1e-12 ? std::min(run_in, run_out) / std::tan(t.angle / 2) : 0.0;
            radius = fillet;
        }
        min_found = std::min(min_found, radius);
        if (radius < min_bend * (1 - kBendTolerance)) {
            ++bend_count;
            if (bends.size() < kMaxFindings) {
                bends.push_back({{"index", orig[i]}, {"at", point(rp[i])}, {"radius_mm", radius},
                                 {"kind", t.junction ? "junction_fillet"
                                                     : sampled[i] ? "sampled_arc_estimate" : "isolated_kink"},
                                 {"circumradius_estimate_mm", t.radius},
                                 {"available_fillet_radius_mm", fillet}});
            }
        }
    }

    json clearances = json::array();
    size_t clearance_count = 0;
    double min_gap = kInf;
    for (size_t i = 0; i + 1 < path.size(); ++i) {
        if (!obstacles.has_geometry) break;
        if (i < range_first || i + 1 > range_last) continue;
        const auto nearest = obstacles.bvh.nearest_segment(path[i], path[i + 1], kInf);
        const int inside_a = obstacles.bvh.first_containing_owner(path[i], obstacles.owner_count());
        const int inside_b = obstacles.bvh.first_containing_owner(path[i + 1], obstacles.owner_count());
        const int inside = inside_a >= 0 ? inside_a : inside_b;
        const double gap = nearest.owner >= 0 ? std::max(0.0, nearest.dist - od / 2) : kInf;
        if (inside >= 0) min_gap = 0;
        else min_gap = std::min(min_gap, gap);

        const bool intersects = nearest.owner >= 0 && nearest.dist <= kContactTolerance;
        const auto violation = obstacles.bvh.first_within_segment(path[i], path[i + 1],
                                                                  obstacles.thr, obstacles.thr_max);
        if (inside < 0 && !intersects && violation.owner < 0) continue;
        ++clearance_count;
        if (clearances.size() >= kMaxFindings) continue;
        if (inside >= 0) {
            clearances.push_back({{"index", i}, {"at", point(inside_a >= 0 ? path[i] : path[i + 1])},
                                  {"body", owner_id(obstacles, inside)}, {"distance_mm", 0.0},
                                  {"reason", "centerline_inside_solid"}});
        } else {
            const auto& hit = intersects ? nearest : violation;
            clearances.push_back({{"index", i}, {"at", point(hit.on_query)},
                                  {"body", owner_id(obstacles, hit.owner)},
                                  {"distance_mm", std::max(0.0, hit.dist - od / 2)},
                                  {"reason", intersects ? "centerline_surface_intersection" : "clearance"}});
        }
    }

    json warnings = json::array();
    for (size_t i = 0; i < std::min(obstacles.warnings.size(), kMaxFindings); ++i)
        warnings.push_back(obstacles.warnings[i]);
    return {{"ok", true}, {"length_mm", length}, {"report_length_mm", report_length},
            {"min_bend_radius_found_mm", std::isfinite(min_found) ? json(min_found) : json(nullptr)},
            {"bend_violations", std::move(bends)}, {"bend_violation_count", bend_count},
            {"min_clearance_mm", std::isfinite(min_gap) ? json(min_gap) : json(nullptr)},
            {"clearance_violations", std::move(clearances)},
            {"clearance_violation_count", clearance_count},
            {"findings_truncated", bend_count > kMaxFindings || clearance_count > kMaxFindings},
            {"bend_basis", "heuristic: collinear vertices ignored; isolated turns are zero-radius kinks; consistent sampled arcs use circumradius; `junctions` vertices use the tangent fillet of the adjacent straight runs"},
            {"bend_tolerance_rel", kBendTolerance},
            {"clearance_basis", "minimum cable-surface to tessellated triangle distance, clamped to zero for contact or centerline inside a solid"},
            {"assumptions", json::array({"Sampled-arc classification and circumradius are heuristics; a polyline does not establish a smooth physical bend radius. Fillet availability is an isolated geometric upper bound, not an obstacle or adjacent-bend check.",
                "Solid containment uses ray parity and assumes closed, correctly tessellated meshes; clearance is to tessellated surfaces, not exact CAD surfaces.",
                "Cable is a constant-diameter circular sweep; distances and coordinates are millimeters."})},
            {"measurement_notes", json::array({"A null minimum bend radius means no turn was measurable from the points.",
                "A null minimum clearance means there was no obstacle geometry to measure; it is not a measured clearance."})},
            {"clearance_mm", clearance}, {"clearance_defaulted", !params.contains("clearance_mm")},
            {"warnings", std::move(warnings)},
            {"warnings_truncated", obstacles.warnings.size() > kMaxFindings}};
}

}  // namespace chudware::harness
