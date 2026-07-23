// core/cam/op_cut.cpp — 2D cutting-head generators (laser / plasma / waterjet)
// and metrology (touch-probe).
//
// Both generators are OCCT-free: geometry arrives as Path2 / Vec3 lists through
// op_common helpers; arithmetic is plain <cmath> / Clipper2-backed geo::.
#include <algorithm>
#include <cmath>

#include "cam/op_common.h"

namespace chudware {
namespace cam {

// ──────────────────────────────────────────────── 2D cutting head ──

// Classify a raw Path2 as closed (part outline / hole) or open (cut line).
// A closed contour has non-trivial signed area; an open path does not.
static bool path_is_closed(const Path2& p) {
    return p.size() >= 3 && std::fabs(geo::signed_area(p)) > 1e-6;
}

// Normalise a 2-D delta into a unit vector; returns {0,1} if degenerate.
static Vec2 unit2(double dx, double dy) {
    const double len = std::hypot(dx, dy);
    return (len > 1e-9) ? Vec2{dx / len, dy / len} : Vec2{0.0, 1.0};
}

// ─────────────────────────────────────────────────────────────────────────────
// gen_cut2d — laser / plasma / waterjet
//
// Algorithm per contour:
//   1. Apply kerf offset (Clipper2-backed geo::offset_region) for closed paths.
//   2. Compute lead-in geometry from geo::lead_in_path so the pierce lands at
//      the approach entry point (off the part edge).
//   3. Emit PIERCE sequence: rapid to lead-in start, plunge to pierce_z,
//      optional dwell, settle to cut_z.
//   4. Feed through lead-in → closed loop (or open path) → lead-out.
//   5. Retract. Repeat for op.passes.
//
// Z heights: cut_z = heights.top + cut_height (head standoff during cutting).
//            pierce_z = heights.top + pierce_height (standoff for arc strike).
// ─────────────────────────────────────────────────────────────────────────────
GenResult gen_cut2d(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);

    std::vector<Path2> contours = op_contours(op);
    if (contours.empty()) {
        res.ok = false;
        res.error = "cut2d: no contours in geometry";
        return res;
    }

    // Kerf: per-op override takes priority over the machine default.
    const double kerf     = op.kerf_override > 0.0 ? op.kerf_override : ctx.machine.kerf;
    // Feed: op.feed_rate is the 2D-head-specific field; fall back to generic feed.
    const double cut_feed = op.feed_rate > 0.0 ? op.feed_rate : op.feed;
    const double lf       = op.lead_feed  > 0.0 ? op.lead_feed  : cut_feed;
    // Plunge rate for head height changes; default to half the cut feed.
    const double plunge_f = op.plunge     > 0.0 ? op.plunge     : cut_feed * 0.5;

    // Absolute Z of the head standoffs (material top + standoff mm).
    // pierce_height must be >= cut_height — clamp silently if not.
    const double cut_z    = op.heights.top + op.cut_height;
    const double pierce_z = op.heights.top +
                            std::max(op.pierce_height, op.cut_height);

    PathBuilder pb(op.heights, cut_feed, plunge_f, 5000.0);
    pb.set_spindle(0);   // laser / plasma / waterjet: no spindle

    const bool climb = (op.direction == CutDirection::Climb);

    for (const Path2& raw : contours) {
        if (raw.size() < 2) continue;

        const bool is_closed = path_is_closed(raw);

        // ── Kerf compensation (closed contours, comp_side != On) ─────────────
        //
        // CompSide::Left  → outside cut; head centre moved OUT by kerf/2
        //   so the kerf-width slot leaves the part at the programmed line.
        // CompSide::Right → inside cut / hole; head centre moved IN by kerf/2.
        Path2 cut_path;
        if (is_closed && kerf > 0.0 && op.comp_side != CompSide::On) {
            const double delta =
                (op.comp_side == CompSide::Left) ? +kerf * 0.5 : -kerf * 0.5;
            Region reg;
            reg.outer = geo::ensure_ccw(raw);
            auto offs = geo::offset_region(reg, delta);
            if (offs.empty()) {
                res.warnings.push_back(
                    "cut2d: kerf offset collapsed a contour – using original");
                cut_path = raw;
            } else {
                cut_path = offs.front().outer;
            }
        } else {
            cut_path = raw;
        }
        if (cut_path.size() < 2) continue;

        const size_t n          = cut_path.size();
        const int    pass_count = std::max(1, op.passes);

        for (int pass = 0; pass < pass_count; ++pass) {

            if (is_closed) {
                // ── Tangent direction at loop start p0 → p1 ─────────────────
                const Vec2& p0  = cut_path.front();
                const Vec2  dir = unit2(cut_path[1].x - p0.x,
                                        cut_path[1].y - p0.y);

                // Lead-in path: points from approach entry up to (not including) p0.
                // Placing the pierce at the lead entry keeps the arc-strike scar
                // off the finished part edge.
                Path2 lin;
                if (op.lead_in.type != LeadType::None)
                    lin = geo::lead_in_path(p0, dir, op.lead_in, op.comp_side, climb);

                const Vec2 pierce_pt = lin.empty() ? p0 : lin.front();

                // ── PIERCE ───────────────────────────────────────────────────
                pb.rapid_to_clearance();
                pb.rapid_xy(pierce_pt);
                pb.plunge_to(pierce_z);                  // head descends to pierce standoff
                if (op.pierce_delay > 0.0)
                    pb.dwell(op.pierce_delay);
                if (pierce_z - cut_z > 1e-6)
                    pb.plunge_to(cut_z);                 // settle to cutting standoff

                // ── LEAD-IN → CONTOUR START ──────────────────────────────────
                if (!lin.empty()) {
                    pb.lead(lin, cut_z, lf);
                    pb.feed_to(p0, cut_z);   // geo::lead_in_path excludes p0
                }

                // ── CLOSED LOOP ──────────────────────────────────────────────
                // Feed [1]..[n-1] then close back to [0]=p0.
                for (size_t i = 1; i <= n; ++i)
                    pb.feed_to(cut_path[i % n], cut_z);

                // ── LEAD-OUT ─────────────────────────────────────────────────
                // Loop closed at p0; closing segment direction: [n-1] → [0].
                if (op.lead_out.type != LeadType::None) {
                    const Vec2& prev = cut_path[n - 1];
                    const Vec2  dout = unit2(p0.x - prev.x, p0.y - prev.y);
                    Path2 lout = geo::lead_out_path(
                        p0, dout, op.lead_out, op.comp_side, climb);
                    if (!lout.empty()) pb.lead(lout, cut_z, lf);
                }

            } else {
                // ── OPEN PATH ────────────────────────────────────────────────
                // Pierce at the path start; no lead-in/out for open cuts.
                const Vec2& start = cut_path.front();
                pb.rapid_to_clearance();
                pb.rapid_xy(start);
                pb.plunge_to(pierce_z);
                if (op.pierce_delay > 0.0)
                    pb.dwell(op.pierce_delay);
                if (pierce_z - cut_z > 1e-6)
                    pb.plunge_to(cut_z);
                // Feed [1]..[n-1] (already at [0] after the pierce).
                for (size_t i = 1; i < n; ++i)
                    pb.feed_to(cut_path[i], cut_z);
            }

            pb.rapid_to_clearance();
        }
    }

    res.path = pb.take(ctx.tool.number, ctx.machine);
    res.path.op_id = op.id;
    return res;
}

// ──────────────────────────────────────────────────── probing ──

// gen_probe — touch-probe surface measurement
//
// Algorithm per point:
//   1. Rapid to clearance, position XY.
//   2. Plunge (Plunge-kind move) toward the expected surface at heights.bottom
//      (or the per-point Z when the Vec3 carries one).  The POST converts this
//      Plunge move to G38.2 for probe operations.
//   3. Retract to clearance (Retract move).
//   4. Optional XY edge probe when op.extra["direction"] is present:
//      back up in the opposite direction, descend to the surface level, feed
//      laterally across the nominal edge position.  Retract.
// ─────────────────────────────────────────────────────────────────────────────
GenResult gen_probe(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);

    std::vector<Vec3> pts = op_points(op);
    if (pts.empty()) {
        res.ok = false;
        res.error = "probe: no probe points";
        return res;
    }

    // Optional XY edge-probe direction: op.extra["direction"] = [dx, dy].
    bool   has_dir    = false;
    Vec2   xy_dir     {1.0, 0.0};
    double probe_dist = op_num(op, "probe_dist", 5.0);  // lateral travel each side (mm)

    if (op.extra.contains("direction")) {
        const auto& d = op.extra["direction"];
        if (d.is_array() && d.size() >= 2) {
            const double dx  = d[0].get<double>();
            const double dy  = d[1].get<double>();
            const double len = std::hypot(dx, dy);
            if (len > 1e-9) {
                xy_dir  = {dx / len, dy / len};
                has_dir = true;
            }
        }
    }

    PathBuilder pb(op.heights, op.feed, op.plunge, 5000.0);
    pb.set_spindle(0);   // probe: no spindle

    for (const Vec3& p : pts) {
        const Vec2 xy{p.x, p.y};
        // Per-point Z takes priority; fall back to heights.bottom (part surface).
        const double probe_z = (std::fabs(p.z) > 1e-9) ? p.z : op.heights.bottom;

        // ── Z surface probe ─────────────────────────────────────────────────
        pb.rapid_to_clearance();       // safe height above point
        pb.rapid_xy(xy);               // position XY at clearance
        pb.plunge_to(probe_z);         // Plunge → POST: G38.2 Z<probe_z>
        pb.rapid_to_clearance();       // Retract after touch

        // ── optional XY edge probe ──────────────────────────────────────────
        // Approach from the opposite side of the expected edge at surface level,
        // feed through the nominal edge position in the probe direction.
        if (has_dir) {
            const Vec2 start{xy.x - xy_dir.x * probe_dist,
                             xy.y - xy_dir.y * probe_dist};
            pb.rapid_xy(start);                // move to approach position at clearance
            pb.plunge_to(op.heights.top);      // descend to surface level for lateral probe
            pb.feed_to({xy.x + xy_dir.x * probe_dist,
                        xy.y + xy_dir.y * probe_dist},
                       op.heights.top);        // lateral feed — POST emits G38.2 in XY
            pb.rapid_to_clearance();
        }
    }

    pb.rapid_to_clearance();
    res.path = pb.take(ctx.tool.number, ctx.machine);
    res.path.op_id = op.id;
    return res;
}

}  // namespace cam
}  // namespace chudware
