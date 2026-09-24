// core/cam/op_common.h — inline helpers shared by the operation generators:
// reading resolved geometry off an Operation, resolving feeds, and small math.
// Header-only so each op_*.cpp (compiled independently / in parallel) can include it.
#pragma once

#include <algorithm>
#include <cmath>
#include <vector>

#include "cam/feeds.h"
#include "cam/geometry.h"
#include "cam/model.h"
#include "cam/operations.h"
#include "cam/toolpath.h"

namespace chudware {
namespace cam {

// ── geometry readers (Operation::geometry is the resolved input) ─────────────
inline std::vector<Region> op_regions(const Operation& op) {
    std::vector<Region> out;
    if (op.geometry.contains("regions"))
        for (auto& r : op.geometry["regions"]) out.push_back(region_from_json(r));
    return out;
}
inline std::vector<Path2> op_contours(const Operation& op) {
    std::vector<Path2> out;
    if (op.geometry.contains("contours"))
        for (auto& c : op.geometry["contours"]) out.push_back(path2_from_json(c));
    return out;
}
inline std::vector<Vec3> op_points(const Operation& op) {
    std::vector<Vec3> out;
    if (op.geometry.contains("points"))
        for (auto& p : op.geometry["points"]) out.push_back(vec3_from_json(p));
    return out;
}
inline std::vector<Vec3> op_axes(const Operation& op) {
    std::vector<Vec3> out;
    if (op.geometry.contains("axes"))
        for (auto& p : op.geometry["axes"]) out.push_back(vec3_from_json(p));
    return out;
}
inline TriMesh op_mesh(const Operation& op) {
    if (op.geometry.contains("mesh")) return trimesh_from_json(op.geometry["mesh"]);
    return {};
}
inline double op_num(const Operation& op, const char* key, double dflt) {
    if (op.extra.contains(key) && op.extra[key].is_number()) return op.extra[key].get<double>();
    if (op.geometry.contains(key) && op.geometry[key].is_number()) return op.geometry[key].get<double>();
    return dflt;
}

// ── feed resolution: fill unset op fields from the cutting DB ─────────────────
inline Operation resolved_op(const Operation& op, const OpContext& ctx) {
    Operation r = op;
    Feeds f = compute_feeds(ctx.machine, ctx.tool, r, ctx.material_id);
    apply_feeds(r, f);
    // Toolpaths and simulation always use mm/min; an explicit turning G95 feed
    // is entered in mm/rev and converted after the resolved spindle speed is known.
    if (ctx.machine.kind == MachineKind::Lathe && op.feed > 0 &&
        op.extra.value("feed_mode", std::string("per_min")) == "per_rev" &&
        r.rpm > 0)
        r.feed = op.feed * r.rpm;
    return r;
}

// ── centroid of a region (a point guaranteed inside is not trivial; this is the
// area centroid, adequate for convex/most pockets; ops verify containment) ────
inline Vec2 region_centroid(const Region& reg) {
    const Path2& p = reg.outer;
    double a = 0, cx = 0, cy = 0;
    size_t n = p.size();
    for (size_t i = 0; i < n; ++i) {
        const Vec2& u = p[i];
        const Vec2& v = p[(i + 1) % n];
        double cross = u.x * v.y - v.x * u.y;
        a += cross;
        cx += (u.x + v.x) * cross;
        cy += (u.y + v.y) * cross;
    }
    if (std::fabs(a) < 1e-9) {
        Vec2 lo, hi;
        geo::bbox(p, lo, hi);
        return {(lo.x + hi.x) * 0.5, (lo.y + hi.y) * 0.5};
    }
    a *= 0.5;
    return {cx / (6 * a), cy / (6 * a)};
}

// A point guaranteed inside the region: centroid if inside, else the midpoint of
// the widest interior scan span.
inline Vec2 region_inside_point(const Region& reg) {
    Vec2 c = region_centroid(reg);
    if (geo::point_in_region(c, reg)) return c;
    Vec2 lo, hi;
    geo::bbox(reg.outer, lo, hi);
    double y = (lo.y + hi.y) * 0.5;
    double bestw = -1;
    Vec2 best = c;
    // sample a few rows for the widest inside span
    for (int row = 1; row < 16; ++row) {
        double yy = lo.y + (hi.y - lo.y) * row / 16.0;
        bool inside = false;
        double xs = lo.x;
        for (double x = lo.x; x <= hi.x; x += (hi.x - lo.x) / 64.0) {
            bool in = geo::point_in_region({x, yy}, reg);
            if (in && !inside) { xs = x; inside = true; }
            else if (!in && inside) {
                double w = x - xs;
                if (w > bestw) { bestw = w; best = {(xs + x) * 0.5, yy}; }
                inside = false;
            }
        }
    }
    (void)y;
    return best;
}

// ── ring-to-ring linking for concentric area clearing ────────────────────────
// Plain concentric-offset clearing (gen_pocket2d-style pocketing, and the plain
// "waterline" flavor of clear_waterline_region) links consecutive offset rings
// with a direct feed_to at whatever vertex each ring happens to start at. That's
// fine for round/simple pockets, but on corner-heavy geometry the two rings'
// start vertices can end up far apart, so the "link" move re-engages a long
// stretch of fresh wall in one feed — a tool-engagement spike defeating the
// point of adaptive/HSM clearing.
//
// "Adaptive" arc-blend linking fixes this in two steps: (1) rotate the next
// ring so it starts at the vertex nearest the current tool position (this is
// the main fix — it minimizes the transition distance to roughly the radial
// stepover, even at corners), then (2) bridge the (now short) gap with a
// tangent semicircular arc — radius = gap/2 — instead of a straight line, so
// the tool sweeps into the next ring rather than plunging straight at it.
// The arc is built to bulge toward `toward` (the pocket interior, already
// cleared), never toward uncut material.
//
// Distinguishing the two by name/flag (Operation::arc_blend_linking) keeps
// "adaptive" honest: without this, ring linking is plain concentric-offset
// clearing wearing an adaptive label.

// Rotate a closed loop so index 0 is the vertex nearest `from` (does not
// change orientation/winding, only the start index).
inline Path2 rotate_ring_to_nearest(const Path2& loop, const Vec2& from) {
    if (loop.size() < 2) return loop;
    size_t best = 0;
    double bestd2 = 1e300;
    for (size_t i = 0; i < loop.size(); ++i) {
        double dx = loop[i].x - from.x, dy = loop[i].y - from.y;
        double d2 = dx * dx + dy * dy;
        if (d2 < bestd2) { bestd2 = d2; best = i; }
    }
    Path2 out;
    out.reserve(loop.size());
    for (size_t i = 0; i < loop.size(); ++i) out.push_back(loop[(best + i) % loop.size()]);
    return out;
}

// Emit the link move from `from` to `to` at depth z: a direct feed (arc_blend
// false — plain concentric-offset behavior) or a tangent semicircular arc
// (radius = |to-from|/2) bulging toward `toward` (arc_blend true — "adaptive"
// behavior). Returns the XY link distance, for the caller's engagement check.
inline double link_ring(PathBuilder& pb, const Vec2& from, const Vec2& to, double z,
                        bool arc_blend, const Vec2& toward) {
    double vx = to.x - from.x, vy = to.y - from.y;
    double dist = std::hypot(vx, vy);
    if (!arc_blend || dist < 1e-6) {
        pb.feed_to(to, z);
        return dist;
    }
    Vec2 mid{(from.x + to.x) * 0.5, (from.y + to.y) * 0.5};
    double dxc = toward.x - mid.x, dyc = toward.y - mid.y;
    bool ccw = (dxc * vy - dyc * vx) > 0;  // bulge the semicircle toward `toward`
    pb.arc_to(to, mid, ccw, z);
    return dist;
}

}  // namespace cam
}  // namespace chudware
