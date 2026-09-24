// core/cam/op_engrave.cpp — engraving / tracing along open or closed curves.
// For a V-bit the cut depth derives from the requested groove width and the tool
// included angle; otherwise it follows the curve at the operation depth.
#define _USE_MATH_DEFINES  // MSVC needs this before <cmath> for M_PI (mirrors geom/plane.cpp)
#include <algorithm>
#include <cmath>

#include "cam/op_common.h"

namespace chudware {
namespace cam {

GenResult gen_engrave(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);
    std::vector<Path2> curves = op_contours(op);
    for (auto& rg : op_regions(op)) {
        curves.push_back(rg.outer);
        for (auto& h : rg.holes) curves.push_back(h);
    }
    if (curves.empty()) { res.ok = false; res.error = "engrave: no curves"; return res; }

    // depth: explicit, else from V-bit width (depth = (width/2)/tan(half-angle))
    double z = op.heights.bottom;
    double width = op_num(op, "width", 0.0);
    if (width > 0 && ctx.tool.type == ToolType::VBit && ctx.tool.tip_angle > 0) {
        double half = ctx.tool.tip_angle * 0.5 * M_PI / 180.0;
        double d = (width * 0.5) / std::tan(half);
        z = op.heights.top - d;
    }
    if (!valid_depth_levels(op.heights.top, z, 0.0)) {
        res.ok = false; res.error = "depth_levels: final depth must be below top"; return res;
    }
    auto levels = depth_levels(op.heights.top, z, op.stepdown, 0.0);

    PathBuilder pb(op.heights, op.feed, op.plunge, std::max(ctx.machine.rapid_xy, 1000.0));
    pb.set_spindle((int)std::lround(op.rpm));
    for (const Path2& c : curves) {
        if (c.size() < 2) continue;
        bool closed = std::hypot(c.front().x - c.back().x, c.front().y - c.back().y) < 1e-6;
        for (double lz : levels) {
            pb.rapid_xy(c.front());
            pb.plunge_to(lz);
            for (size_t i = 1; i < c.size(); ++i) pb.feed_to(c[i], lz);
            if (closed && c.size() > 2) pb.feed_to(c.front(), lz);
            pb.rapid_to_clearance();
        }
    }
    res.path = pb.take(ctx.tool.number, ctx.machine);
    res.path.op_id = op.id;
    return res;
}

}  // namespace cam
}  // namespace chudware
