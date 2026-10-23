// core/cam/op_milling.cpp — 2.5D milling generators: face, contour/profile,
// pocket (with islands), slot, 2D chamfer, helical bore.
#define _USE_MATH_DEFINES  // MSVC needs this before <cmath> for M_PI (mirrors geom/plane.cpp)
#include <algorithm>
#include <cmath>

#include "cam/op_common.h"

namespace chudware {
namespace cam {

namespace {
// Orient a loop for the requested cut direction. We treat the geometric CCW
// orientation as "climb" for an outer boundary; conventional reverses it.
Path2 orient(const Path2& loop, CutDirection dir) {
    bool ccw = geo::is_ccw(loop);
    bool want_ccw = (dir == CutDirection::Climb);
    if (ccw != want_ccw) { Path2 r(loop.rbegin(), loop.rend()); return r; }
    return loop;
}
PathBuilder make_builder(const Operation& op) {
    return PathBuilder(op.heights, op.feed, op.plunge, 5000.0);
}
}  // namespace

// ───────────────────────────────────────────────────────────────── facing ──
GenResult gen_face(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);
    // region to face: explicit regions, else the stock top rectangle
    std::vector<Region> regs = op_regions(op);
    if (regs.empty()) {
        Region r;
        const Stock& s = ctx.stock;
        r.outer = {{s.min.x, s.min.y}, {s.max.x, s.min.y}, {s.max.x, s.max.y}, {s.min.x, s.max.y}};
        if (r.outer[0].x == r.outer[2].x) { res.ok = false; res.error = "face: no region and empty stock bbox"; return res; }
        regs.push_back(r);
    }
    double r = ctx.tool.effective_radius();
    // grow region by tool radius so passes clear the edges of the stock
    std::vector<Region> area = geo::offset_regions(regs, r * 0.9);
    if (area.empty()) area = regs;
    double stepover = op.stepover > 0 ? op.stepover : r;
    PathBuilder pb = make_builder(op);
    pb.set_spindle((int)std::lround(op.rpm));
    if (!valid_depth_levels(op.heights.top, op.heights.bottom, op.stock_to_leave_axial)) {
        res.ok = false; res.error = "depth_levels: final depth must be below top"; return res;
    }
    auto levels = depth_levels(op.heights.top, op.heights.bottom, op.stepdown, op.stock_to_leave_axial);
    for (double z : levels) {
        auto strokes = geo::raster_fill(area, stepover, op.angle, op.pattern != CutPattern::OneWay);
        if (strokes.empty()) { res.warnings.push_back("face: no strokes at a level"); continue; }
        // link strokes: rapid to first, then feed across each, feed-link to the next
        pb.rapid_xy(strokes.front().front());
        pb.plunge_to(z);
        Vec2 last = strokes.front().front();
        for (auto& st : strokes) {
            if (std::hypot(st.front().x - last.x, st.front().y - last.y) > stepover * 2.5) {
                pb.rapid_to_clearance();
                pb.rapid_xy(st.front());
                pb.plunge_to(z);
            } else {
                pb.feed_to(st.front(), z);  // stepover link at depth
            }
            pb.feed_to(st.back(), z);
            last = st.back();
        }
        pb.rapid_to_clearance();
    }
    res.path = pb.take(ctx.tool.number, ctx.machine);
    res.path.op_id = op.id;
    return res;
}

// ─────────────────────────────────────────────────────────── contour/profile ──
GenResult gen_contour2d(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);
    std::vector<Path2> loops = op_contours(op);
    std::vector<Region> regs = op_regions(op);
    for (auto& rg : regs) {
        loops.push_back(rg.outer);
        for (auto& h : rg.holes) loops.push_back(h);
    }
    if (loops.empty()) { res.ok = false; res.error = "contour: no contours/regions"; return res; }

    double r = ctx.tool.effective_radius();
    double off = r + op.stock_to_leave;
    PathBuilder pb = make_builder(op);
    pb.set_spindle((int)std::lround(op.rpm));
    if (!valid_depth_levels(op.heights.top, op.heights.bottom, op.stock_to_leave_axial)) {
        res.ok = false; res.error = "depth_levels: final depth must be below top"; return res;
    }
    auto levels = depth_levels(op.heights.top, op.heights.bottom, op.stepdown, op.stock_to_leave_axial);

    for (const Path2& loop : loops) {
        if (loop.size() < 2) continue;
        // compute the actual cut path per comp side
        std::vector<Path2> cut_loops;
        if (op.comp_side == CompSide::On || op.comp_in_control) {
            cut_loops.push_back(loop);
        } else {
            double d = (op.comp_side == CompSide::Left) ? off : -off;
            Region reg;
            reg.outer = geo::ensure_ccw(loop);
            auto offs = geo::offset_region(reg, d);
            for (auto& o : offs) {
                cut_loops.push_back(o.outer);
                for (auto& h : o.holes) cut_loops.push_back(h);
            }
            if (cut_loops.empty()) { res.warnings.push_back("contour: offset collapsed a loop"); continue; }
        }
        for (Path2& cl : cut_loops) {
            Path2 oriented = orient(cl, op.direction);
            size_t nlev = levels.size();
            for (size_t li = 0; li < nlev; ++li) {
                double z = levels[li];
                bool last = (li + 1 == nlev);
                pb.cut_loop(oriented, z,
                            op.lead_in, op.lead_out, op.comp_side,
                            op.direction == CutDirection::Climb,
                            last ? op.tab_count : 0, op.tab_width, op.tab_height,
                            op.heights.top);
            }
            // spring/finish passes at final depth, no stock-to-leave
            for (int fp = 0; fp < op.finish_passes; ++fp)
                pb.cut_loop(oriented, levels.back(), op.lead_in, op.lead_out, op.comp_side,
                            op.direction == CutDirection::Climb, 0, 0, 0, op.heights.top);
        }
    }
    res.path = pb.take(ctx.tool.number, ctx.machine);
    res.path.op_id = op.id;
    return res;
}

// ─────────────────────────────────────────────────── pocket clearing (islands) ──
GenResult gen_pocket2d(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);
    std::vector<Region> regs = op_regions(op);
    if (regs.empty()) { res.ok = false; res.error = "pocket: no regions"; return res; }
    double r = ctx.tool.effective_radius();
    double stepover = op.stepover > 0 ? op.stepover : r * 0.8;
    PathBuilder pb = make_builder(op);
    pb.set_spindle((int)std::lround(op.rpm));
    if (!valid_depth_levels(op.heights.top, op.heights.bottom, op.stock_to_leave_axial)) {
        res.ok = false; res.error = "depth_levels: final depth must be below top"; return res;
    }
    auto levels = depth_levels(op.heights.top, op.heights.bottom, op.stepdown, op.stock_to_leave_axial);

    for (const Region& reg : regs) {
        // concentric clearing loops (outer wall inward)
        auto pocket_levels = geo::pocket_offsets(reg, r, stepover, op.stock_to_leave);
        if (pocket_levels.empty()) { res.warnings.push_back("pocket: tool too large for region"); continue; }
        Vec2 entry = region_inside_point(reg);
        // for the deepest inner level use it as the helix center if inside
        for (double z : levels) {
            // entry: helix ramp at the region inside point
            double helix_r = std::max(0.5, std::min(r * 0.9, stepover));
            if (op.ramp.type == RampType::Helix)
                pb.helix_entry(entry, helix_r, op.heights.top, z, op.ramp_feed, std::max(0.2, helix_r * std::tan(op.ramp.angle * M_PI / 180.0) * 2));
            else {
                // straight-plunge entry: rapid over the entry point at clearance FIRST,
                // then plunge — else the tool plunges from wherever the previous loop/level
                // ended, air-cutting or diving into a wall. rapid_xy retracts to
                // the clearance plane internally before the XY traverse.
                pb.rapid_xy(entry);
                pb.plunge_to(z);
            }
            // cut concentric loops inner->outer (climb-friendly) at this Z
            for (auto it = pocket_levels.rbegin(); it != pocket_levels.rend(); ++it) {
                for (Path2& loop : *it) {
                    if (loop.size() < 3) continue;
                    Path2 oriented = orient(loop, op.direction);
                    // approach: feed to the loop start at depth
                    pb.feed_to(oriented.front(), z);
                    for (size_t i = 1; i <= oriented.size(); ++i)
                        pb.feed_to(oriented[i % oriented.size()], z);
                }
            }
            pb.rapid_to_clearance();
        }
    }
    res.path = pb.take(ctx.tool.number, ctx.machine);
    res.path.op_id = op.id;
    return res;
}

// ───────────────────────────────────────────────────────────────────── slot ──
GenResult gen_slot(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);
    std::vector<Path2> center = op_contours(op);
    if (center.empty()) { res.ok = false; res.error = "slot: no centerline contour"; return res; }
    double dia = ctx.tool.diameter;
    double width = op_num(op, "width", dia);
    PathBuilder pb = make_builder(op);
    pb.set_spindle((int)std::lround(op.rpm));
    if (!valid_depth_levels(op.heights.top, op.heights.bottom, op.stock_to_leave_axial)) {
        res.ok = false; res.error = "depth_levels: final depth must be below top"; return res;
    }
    auto levels = depth_levels(op.heights.top, op.heights.bottom, op.stepdown, op.stock_to_leave_axial);

    if (width <= dia + 1e-6) {
        // single pass along the centerline at each depth (ramped entry)
        for (const Path2& c : center) {
            if (c.size() < 2) continue;
            for (double z : levels) {
                pb.rapid_xy(c.front());
                if (op.ramp.type != RampType::Plunge && c.size() >= 2)
                    pb.zigzag_entry(c[0], c[1], op.heights.top, z, op.ramp_feed);
                else
                    pb.plunge_to(z);
                for (size_t i = 1; i < c.size(); ++i) pb.feed_to(c[i], z);
                pb.rapid_to_clearance();
            }
        }
    } else {
        // wide slot -> pocket the stadium region(s)
        auto ribbons = geo::offset_open(center, width * 0.5, geo::JoinType::Round, true);
        Operation pk = op;
        pk.geometry = json::object();
        json regs = json::array();
        for (auto& rb : ribbons) regs.push_back(to_json(Region{rb, {}}));
        pk.geometry["regions"] = regs;
        pk.type = OpType::Pocket2D;
        return gen_pocket2d(pk, ctx);
    }
    res.path = pb.take(ctx.tool.number, ctx.machine);
    res.path.op_id = op.id;
    return res;
}

// ────────────────────────────────────────────────────────────── 2D chamfer ──
GenResult gen_chamfer2d(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);
    std::vector<Path2> loops = op_contours(op);
    for (auto& rg : op_regions(op)) { loops.push_back(rg.outer); for (auto& h : rg.holes) loops.push_back(h); }
    if (loops.empty()) { res.ok = false; res.error = "chamfer: no edges"; return res; }
    double width = op_num(op, "width", 1.0);              // chamfer width (mm)
    double tip_off = op_num(op, "tip_offset", ctx.tool.effective_radius() * 0.0);
    double z = op.heights.top - width;                    // 45deg chamfer depth ~= width
    PathBuilder pb = make_builder(op);
    pb.set_spindle((int)std::lround(op.rpm));
    for (const Path2& loop : loops) {
        if (loop.size() < 2) continue;
        Path2 path = loop;
        if (tip_off != 0) {
            Region reg{geo::ensure_ccw(loop), {}};
            auto offs = geo::offset_region(reg, (op.comp_side == CompSide::Right ? -tip_off : tip_off));
            if (!offs.empty()) path = offs.front().outer;
        }
        pb.cut_loop(orient(path, op.direction), z, op.lead_in, op.lead_out, op.comp_side,
                    op.direction == CutDirection::Climb, 0, 0, 0, op.heights.top);
    }
    res.path = pb.take(ctx.tool.number, ctx.machine);
    res.path.op_id = op.id;
    return res;
}

// ─────────────────────────────────────────────────────────── helical bore ──
GenResult gen_bore2d(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);
    std::vector<Vec3> centers = op_points(op);
    if (centers.empty()) { res.ok = false; res.error = "bore: no center points"; return res; }
    double bore_dia = op_num(op, "diameter", ctx.tool.diameter * 2);
    double r = ctx.tool.effective_radius();
    double helix_r = bore_dia * 0.5 - r;
    if (helix_r <= 0) { res.ok = false; res.error = "bore: tool >= bore diameter"; return res; }
    double pitch = std::max(0.2, helix_r * std::tan(std::max(1.0, op.ramp.angle) * M_PI / 180.0) * 2);
    PathBuilder pb = make_builder(op);
    pb.set_spindle((int)std::lround(op.rpm));
    for (const Vec3& c : centers) {
        Vec2 cc{c.x, c.y};
        pb.helix_entry(cc, helix_r, op.heights.top, op.heights.bottom + op.stock_to_leave_axial,
                       op.ramp_feed, pitch);
        // finishing full circle at depth handled inside helix_entry
        pb.rapid_to_clearance();
    }
    res.path = pb.take(ctx.tool.number, ctx.machine);
    res.path.op_id = op.id;
    return res;
}

}  // namespace cam
}  // namespace chudware
