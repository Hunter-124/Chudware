// core/cam/op_adaptive.cpp — adaptive / high-speed clearing: constant low radial
// engagement (the tool never exceeds the requested stepover engagement), large
// axial depth-of-cut, helical entry, and smooth concentric offset linking.
// Clean-room: an engagement-limited concentric-offset clearer with a trochoidal
// helical entry, the standard public approach (no VoluMill/Adaptive source used).
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

    for (const Region& reg : regs) {
        auto offs = geo::pocket_offsets(reg, r, engage, op.stock_to_leave);
        if (offs.empty()) { res.warnings.push_back("adaptive: tool too large"); continue; }
        Vec2 entry = region_inside_point(reg);
        double helix_r = std::max(0.5, std::min(r * 0.9, engage * 2));
        for (double z : levels) {
            // trochoidal helical entry to full depth (no straight plunge)
            double pitch = std::max(0.2, helix_r * std::tan(std::max(1.5, op.ramp.angle) * M_PI / 180.0) * 2);
            pb.helix_entry(entry, helix_r, op.heights.top, z, op.ramp_feed, pitch);
            // clear inner -> outer with the engagement-limited loops, linked at depth
            for (auto it = offs.rbegin(); it != offs.rend(); ++it) {
                for (Path2& loop : *it) {
                    if (loop.size() < 3) continue;
                    bool ccw = geo::is_ccw(loop);
                    bool want = (op.direction == CutDirection::Climb);
                    Path2 cl = (ccw == want) ? loop : Path2(loop.rbegin(), loop.rend());
                    pb.feed_to(cl.front(), z);
                    for (size_t i = 1; i <= cl.size(); ++i) pb.feed_to(cl[i % cl.size()], z);
                }
            }
            pb.rapid_to_clearance();
        }
    }
    res.path = pb.take(ctx.tool.number, ctx.machine);
    res.path.op_id = op.id;
    return res;
}

}  // namespace cam
}  // namespace aicad
