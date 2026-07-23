// core/cam/op_drill.cpp — hole-making: drilling canned cycles (long-hand moves
// for sim/backplot; the post collapses them into G81/G83/G73/G84/G85/G86 from the
// operation metadata) and helical thread milling.
#define _USE_MATH_DEFINES  // MSVC needs this before <cmath> for M_PI (mirrors geom/plane.cpp)
#include <algorithm>
#include <cmath>

#include "cam/op_common.h"

namespace chudware {
namespace cam {

GenResult gen_drill(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);
    std::vector<Vec3> pts = op_points(op);
    if (pts.empty()) { res.ok = false; res.error = "drill: no hole points"; return res; }

    double Rp = op.heights.retract;
    double final_z = op.heights.bottom - op.tip_through;
    PathBuilder pb(op.heights, op.feed, op.plunge, std::max(ctx.machine.rapid_z, 1000.0));
    pb.set_spindle((int)std::lround(op.rpm));
    const std::string& cyc = op.cycle;

    for (const Vec3& p : pts) {
        Vec2 xy{p.x, p.y};
        double top = (p.z != 0.0) ? p.z : op.heights.top;  // per-hole top if supplied
        pb.rapid_xy(xy);
        pb.rapid_to({xy.x, xy.y, Rp});
        (void)top;
        if (cyc == "peck" || cyc == "deep") {
            double z = Rp;
            double inc = op.peck > 0 ? op.peck : ctx.tool.diameter;
            while (z > final_z + 1e-6) {
                z = std::max(final_z, z - inc);
                pb.plunge_to(z);
                pb.rapid_to({xy.x, xy.y, Rp});  // full retract (clears chips)
                if (z > final_z + 1e-6) pb.rapid_to({xy.x, xy.y, std::min(Rp, z + 0.5)});
            }
        } else if (cyc == "chipbreak") {
            double z = Rp;
            double inc = op.peck > 0 ? op.peck : ctx.tool.diameter;
            double br = op.break_chip > 0 ? op.break_chip : 0.5;
            while (z > final_z + 1e-6) {
                z = std::max(final_z, z - inc);
                pb.plunge_to(z);
                if (z > final_z + 1e-6) pb.feed_to({xy.x, xy.y, z + br});  // short chip-break retract
            }
            pb.rapid_to({xy.x, xy.y, Rp});
        } else if (cyc == "tap" || cyc == "rigidtap") {
            pb.plunge_to(final_z);
            if (op.dwell > 0) pb.dwell(op.dwell);
            pb.feed_to({xy.x, xy.y, Rp});       // retract at feed (synchronized)
        } else if (cyc == "ream") {
            pb.plunge_to(final_z);
            if (op.dwell > 0) pb.dwell(op.dwell);
            pb.feed_to({xy.x, xy.y, Rp});       // feed out for a clean bore
        } else if (cyc == "bore" || cyc == "backbore") {
            pb.plunge_to(final_z);
            if (op.dwell > 0) pb.dwell(op.dwell);
            pb.rapid_to({xy.x, xy.y, Rp});
        } else if (cyc == "spot") {
            double spot_z = op.heights.top - (op_num(op, "spot_depth", ctx.tool.diameter * 0.3));
            pb.plunge_to(spot_z);
            if (op.dwell > 0) pb.dwell(op.dwell);
            pb.rapid_to({xy.x, xy.y, Rp});
        } else {  // simple drill (G81)
            pb.plunge_to(final_z);
            if (op.dwell > 0) pb.dwell(op.dwell);
            pb.rapid_to({xy.x, xy.y, Rp});
        }
    }
    pb.rapid_to_clearance();
    res.path = pb.take(ctx.tool.number, ctx.machine);
    res.path.op_id = op.id;
    return res;
}

GenResult gen_threadmill(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);
    std::vector<Vec3> pts = op_points(op);
    if (pts.empty()) { res.ok = false; res.error = "thread mill: no center points"; return res; }
    double thread_dia = op_num(op, "diameter", ctx.tool.diameter * 1.5);
    double pitch = op.thread_pitch > 0 ? op.thread_pitch : (ctx.tool.thread_pitch > 0 ? ctx.tool.thread_pitch : 1.0);
    bool internal = !op.id_op ? true : op_num(op, "external", 0.0) < 0.5;
    double r = ctx.tool.effective_radius();
    double path_r = internal ? (thread_dia * 0.5 - r) : (thread_dia * 0.5 + r);
    if (path_r <= 0) { res.ok = false; res.error = "thread mill: tool too large"; return res; }
    double top = op.heights.top;
    double bottom = op.heights.bottom;
    double depth = top - bottom;
    int turns = std::max(1, (int)std::ceil(depth / pitch));
    int seg = 48;
    PathBuilder pb(op.heights, op.feed, op.plunge, std::max(ctx.machine.rapid_z, 1000.0));
    pb.set_spindle((int)std::lround(op.rpm));
    for (const Vec3& c : pts) {
        Vec2 cc{c.x, c.y};
        // rapid to center, plunge to the bottom, then helix UP cutting the thread
        pb.rapid_xy(cc);
        pb.plunge_to(bottom);
        // move out to the thread radius
        pb.feed_to({cc.x + path_r, cc.y}, bottom);
        bool ccw = internal == (op.direction == CutDirection::Climb);
        for (int i = 1; i <= turns * seg; ++i) {
            double frac = (double)i / (turns * seg);
            double ang = 2 * M_PI * turns * frac * (ccw ? 1 : -1);
            double zz = bottom + depth * frac;
            pb.feed_to({cc.x + path_r * std::cos(ang), cc.y + path_r * std::sin(ang)}, zz);
        }
        // retract to center then up
        pb.feed_to({cc.x, cc.y}, top);
        pb.rapid_to_clearance();
    }
    res.path = pb.take(ctx.tool.number, ctx.machine);
    res.path.op_id = op.id;
    return res;
}

}  // namespace cam
}  // namespace chudware
