// core/cam/op_adaptive.cpp — adaptive / high-speed clearing: constant low radial
// engagement (the tool never exceeds the requested stepover engagement), large
// axial depth-of-cut, helical entry, and smooth concentric offset linking.
// Clean-room: an engagement-limited concentric-offset clearer with a trochoidal
// helical entry, the standard public approach (no VoluMill/Adaptive source used).
#define _USE_MATH_DEFINES  // MSVC needs this before <cmath> for M_PI (mirrors geom/plane.cpp)
#include <algorithm>
#include <cmath>

#include "cam/op_common.h"

namespace aicad {
namespace cam {

GenResult gen_adaptive2d(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);
    std::vector<Region> regs = op_regions(op);
    if (regs.empty()) { res.ok = false; res.error = "adaptive: no regions"; return res; }

    double r = ctx.tool.effective_radius();
    // engagement-limited radial stepover: adaptive keeps this small (~10-40% D),
    // trading it for a deep axial cut. Honor the user's stepover, else 10% dia.
    double engage = op.stepover > 0 ? op.stepover : ctx.tool.diameter * 0.1;
    // adaptive uses a large axial DOC; default the full depth in one or few steps.
    double stepdown = op.stepdown > 0 ? op.stepdown : (op.heights.top - op.heights.bottom);
    PathBuilder pb = PathBuilder(op.heights, op.feed, op.plunge, std::max(ctx.machine.rapid_xy, 1000.0));
    pb.set_spindle((int)std::lround(op.rpm));
    auto levels = depth_levels(op.heights.top, op.heights.bottom, stepdown, op.stock_to_leave_axial);

    // Largest ring-to-ring link distance seen across the whole op, for the
    // engagement-exceeded check below (see op_common.h link_ring/rotate_ring_to_nearest).
    double max_link_dist = 0.0;

    for (const Region& reg : regs) {
        auto offs = geo::pocket_offsets(reg, r, engage, op.stock_to_leave);
        if (offs.empty()) { res.warnings.push_back("adaptive: tool too large"); continue; }
        Vec2 entry = region_inside_point(reg);
        double helix_r = std::max(0.5, std::min(r * 0.9, engage * 2));
        for (double z : levels) {
            // trochoidal helical entry to full depth (no straight plunge)
            double pitch = std::max(0.2, helix_r * std::tan(std::max(1.5, op.ramp.angle) * M_PI / 180.0) * 2);
            pb.helix_entry(entry, helix_r, op.heights.top, z, op.ramp_feed, pitch);
            // Clear inner -> outer. This is concentric-offset ring clearing; with
            // arc_blend_linking (the default) each ring-to-ring transition is a
            // tangent arc-blend link (rotate the next ring to start nearest the
            // tool, then a semicircular arc) instead of a direct feed_to, which is
            // what keeps this "adaptive" (bounded engagement) rather than plain
            // concentric-offset clearing. The very first ring of a level is entered
            // from the helix bore, not from another ring, so it is not tracked as a
            // ring-transition.
            bool have_prev = false;
            Vec2 prev{0, 0};
            for (auto it = offs.rbegin(); it != offs.rend(); ++it) {
                for (Path2& loop : *it) {
                    if (loop.size() < 3) continue;
                    bool ccw = geo::is_ccw(loop);
                    bool want = (op.direction == CutDirection::Climb);
                    Path2 cl = (ccw == want) ? loop : Path2(loop.rbegin(), loop.rend());
                    if (have_prev && op.arc_blend_linking) cl = rotate_ring_to_nearest(cl, prev);
                    if (have_prev) {
                        double link_dist = link_ring(pb, prev, cl.front(), z, op.arc_blend_linking, entry);
                        max_link_dist = std::max(max_link_dist, link_dist);
                    } else {
                        pb.feed_to(cl.front(), z);
                    }
                    for (size_t i = 1; i <= cl.size(); ++i) pb.feed_to(cl[i % cl.size()], z);
                    prev = cl.front();
                    have_prev = true;
                }
            }
            pb.rapid_to_clearance();
        }
    }
    if (max_link_dist > op.stepover * 1.5) {
        res.warnings.push_back("adaptive: ring-transition engagement exceeds 1.5x stepover — "
                               "arc-blend linking recommended");
    }
    res.path = pb.take(ctx.tool.number, ctx.machine);
    res.path.op_id = op.id;
    return res;
}

}  // namespace cam
}  // namespace aicad
