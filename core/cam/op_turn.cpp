// core/cam/op_turn.cpp - turning operation generators: face, rough, finish,
// groove, thread, part, drill. All toolpath moves live in the XZ plane:
//   Move.to.x = radius (>= 0), Move.to.z = axial position, Move.to.y = 0.
#include <algorithm>
#include <cmath>
#include <vector>

#include "cam/op_common.h"

namespace chudware {
namespace cam {

namespace {

// ── profile types and readers ─────────────────────────────────────────────────
struct TurnPt { double z, r; }; // z = axial, r = radius

std::vector<TurnPt> read_profile(const Operation& op, const char* key = "profile") {
    std::vector<TurnPt> out;
    if (op.geometry.contains(key))
        for (auto& p : op.geometry[key])
            out.push_back({p[0].get<double>(), p[1].get<double>()});
    return out;
}

double prof_max_r(const std::vector<TurnPt>& p) {
    double m = 0;
    for (auto& t : p) m = std::max(m, t.r);
    return m;
}
double prof_min_r(const std::vector<TurnPt>& p) {
    if (p.empty()) return 0;
    double m = p[0].r;
    for (auto& t : p) m = std::min(m, t.r);
    return m;
}
double prof_z_hi(const std::vector<TurnPt>& p) {
    if (p.empty()) return 0;
    double m = p[0].z;
    for (auto& t : p) m = std::max(m, t.z);
    return m;
}
double prof_z_lo(const std::vector<TurnPt>& p) {
    if (p.empty()) return 0;
    double m = p[0].z;
    for (auto& t : p) m = std::min(m, t.z);
    return m;
}

// Z range [lo,hi] where (profile_r + stl < pass_r): material exists above pass_r.
// Returns {1e18, -1e18} when empty.
std::pair<double,double> od_cut_z_range(
        const std::vector<TurnPt>& prof, double pass_r, double stl) {
    double lo = 1e18, hi = -1e18;
    for (auto& p : prof) {
        if (p.r + stl < pass_r - 1e-6) {
            lo = std::min(lo, p.z);
            hi = std::max(hi, p.z);
        }
    }
    // Segment crossings
    for (size_t i = 0; i + 1 < prof.size(); ++i) {
        double r0 = prof[i].r + stl, r1 = prof[i+1].r + stl;
        if ((r0 < pass_r) != (r1 < pass_r) && std::fabs(r1 - r0) > 1e-9) {
            double t  = (pass_r - r0) / (r1 - r0);
            double zc = prof[i].z + t * (prof[i+1].z - prof[i].z);
            lo = std::min(lo, zc);
            hi = std::max(hi, zc);
        }
    }
    return {lo, hi};
}

// Stock outer radius: cylindrical stock radius first, box max.x, then fallback.
double stock_od(const OpContext& ctx, const std::vector<TurnPt>& prof, double stl) {
    if (ctx.stock.radius > 0) return ctx.stock.radius;
    if (ctx.stock.max.x > 0) return ctx.stock.max.x;
    return prof_max_r(prof) + stl + 2.0;
}

// ── move emitters (turning XZ plane, y always 0) ──────────────────────────────
void t_rapid(Toolpath& tp, double r, double z) {
    Move m;
    m.kind = MoveKind::Rapid;
    m.to   = Vec3(r, 0.0, z);
    tp.moves.push_back(m);
}

void t_feed(Toolpath& tp, double r, double z, double feed) {
    Move m;
    m.kind = MoveKind::Cut;
    m.to   = Vec3(r, 0.0, z);
    m.feed = feed;
    tp.moves.push_back(m);
}

void t_dwell(Toolpath& tp, double s) {
    if (s <= 0) return;
    Move m;
    m.kind    = MoveKind::Dwell;
    m.dwell_s = s;
    tp.moves.push_back(m);
}

// Attach spindle RPM to the first non-dwell move, compute stats, hand off.
void finalize(GenResult& res, Toolpath& tp, const Operation& op,
              const OpContext& ctx, int rpm) {
    for (auto& m : tp.moves)
        if (m.kind != MoveKind::Dwell && m.kind != MoveKind::ToolChange) {
            m.spindle_rpm = rpm;
            break;
        }
    tp.op_id       = op.id;
    tp.tool_number = ctx.tool.number;
    tp.update_stats(ctx.machine);
    res.path = std::move(tp);
}

} // namespace

// ─────────────────────────────────────────────────────────── gen_turn_face ──
// Radial passes (OD→center) at each Z step from heights.top down to heights.bottom.
GenResult gen_turn_face(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);

    auto prof   = read_profile(op);
    double od   = prof_max_r(prof);
    if (od <= 0) od = stock_od(ctx, prof, 0);

    double clr   = op.turn_clearance > 0 ? op.turn_clearance : 1.0;
    double stepz = op.stepdown       > 0 ? op.stepdown       : 0.5;
    double feed  = op.feed           > 0 ? op.feed           : 100.0;
    double z_top = op.heights.top;
    double z_end = op.heights.bottom;
    if (z_top < z_end) std::swap(z_top, z_end); // ensure top > end

    Toolpath tp;
    t_rapid(tp, od + clr, z_top + clr);

    for (double z = z_top; ; z = std::max(z_end, z - stepz)) {
        t_rapid(tp, od + clr, z);
        t_feed(tp,  od,       z, feed);
        t_feed(tp,  0.0,      z, feed); // radial sweep to center
        t_rapid(tp, od + clr, z);
        if (z <= z_end + 1e-6) break;
    }

    t_rapid(tp, od + clr, z_end + clr);
    finalize(res, tp, op, ctx, (int)std::lround(op.rpm));
    return res;
}

// ──────────────────────────────────────────────────────────── gen_turn_rough ──
// Successive Z-sweep passes stepping radially inward (OD) or outward (ID),
// leaving stock_to_leave on the profile. Each pass clips Z to material extent.
GenResult gen_turn_rough(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);

    auto prof = read_profile(op);
    if (prof.empty()) { res.ok = false; res.error = "turn_rough: no profile"; return res; }
    auto stk  = read_profile(op, "stock_profile");

    double stl  = op.stock_to_leave > 0 ? op.stock_to_leave : 0.0;
    double clr  = op.turn_clearance > 0 ? op.turn_clearance : 1.0;
    double doc  = op.stepdown       > 0 ? op.stepdown       : 0.5;
    double feed = op.feed           > 0 ? op.feed           : 100.0;

    Toolpath tp;

    if (!op.id_op) {
        // ── OD roughing: step from stock OD inward ────────────────────────────
        double stock_r = prof_max_r(stk) > 0 ? prof_max_r(stk)
                                              : stock_od(ctx, prof, stl);
        t_rapid(tp, stock_r + clr, prof_z_hi(prof) + clr);

        for (double cur_r = stock_r - doc; cur_r >= -1e-9; cur_r -= doc) {
            double pass_r = std::max(0.0, cur_r);
            auto [zlo, zhi] = od_cut_z_range(prof, pass_r, stl);
            if (zlo > zhi - 1e-6) {
                if (pass_r <= 0.0) break;
                continue; // no material at this radius
            }
            zhi += 0.1; // extend slightly past profile edge for clean exit
            t_rapid(tp, pass_r + clr, zhi + clr);
            t_rapid(tp, pass_r + clr, zhi);
            t_feed(tp,  pass_r,       zhi, feed);
            t_feed(tp,  pass_r,       zlo, feed); // Z sweep toward chuck
            t_rapid(tp, pass_r + clr, zlo);
            if (pass_r <= 0.0) break;
        }

        t_rapid(tp, stock_r + clr, prof_z_hi(prof) + clr);

    } else {
        // ── ID roughing: step from existing bore outward ──────────────────────
        double stock_id = prof_max_r(stk); // existing hole radius (0 = solid)
        double target_r = prof_min_r(prof) - stl;

        if (stock_id < clr) stock_id = 0;
        t_rapid(tp, std::max(0.0, stock_id - clr), prof_z_hi(prof) + clr);

        for (double cur_r = stock_id + doc; cur_r <= target_r + 1e-6; cur_r += doc) {
            double pass_r = std::min(target_r, cur_r);
            // ID: material exists where profile_r - stl > pass_r
            double zlo = 1e18, zhi = -1e18;
            for (auto& p : prof) {
                if (p.r - stl > pass_r + 1e-6) {
                    zlo = std::min(zlo, p.z);
                    zhi = std::max(zhi, p.z);
                }
            }
            for (size_t i = 0; i + 1 < prof.size(); ++i) {
                double r0 = prof[i].r - stl, r1 = prof[i+1].r - stl;
                if ((r0 > pass_r) != (r1 > pass_r) && std::fabs(r1 - r0) > 1e-9) {
                    double t  = (pass_r - r0) / (r1 - r0);
                    double zc = prof[i].z + t * (prof[i+1].z - prof[i].z);
                    zlo = std::min(zlo, zc);
                    zhi = std::max(zhi, zc);
                }
            }
            if (zlo > zhi - 1e-6) {
                if (pass_r >= target_r - 1e-6) break;
                continue;
            }
            t_rapid(tp, pass_r - clr, zhi + clr);
            t_rapid(tp, pass_r - clr, zhi);
            t_feed(tp,  pass_r,       zhi, feed);
            t_feed(tp,  pass_r,       zlo, feed);
            t_rapid(tp, pass_r - clr, zlo);
            if (pass_r >= target_r - 1e-6) break;
        }
    }

    finalize(res, tp, op, ctx, (int)std::lround(op.rpm));
    return res;
}

// ─────────────────────────────────────────────────────────── gen_turn_finish ──
// Single pass tracing the profile exactly at finish feed.
GenResult gen_turn_finish(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);

    auto prof = read_profile(op);
    if (prof.empty()) { res.ok = false; res.error = "turn_finish: no profile"; return res; }

    double clr   = op.turn_clearance > 0 ? op.turn_clearance : 1.0;
    double feed  = op.feed           > 0 ? op.feed           : 100.0;
    // ID boring: approach from below the bore radius
    double clr_x = op.id_op ? -clr : clr;

    Toolpath tp;
    t_rapid(tp, prof[0].r + clr_x, prof[0].z + clr);
    t_rapid(tp, prof[0].r + clr_x, prof[0].z);
    t_feed(tp,  prof[0].r,         prof[0].z, feed);
    for (size_t i = 1; i < prof.size(); ++i)
        t_feed(tp, prof[i].r, prof[i].z, feed);
    t_rapid(tp, prof.back().r + clr_x, prof.back().z + clr);

    finalize(res, tp, op, ctx, (int)std::lround(op.rpm));
    return res;
}

// ─────────────────────────────────────────────────────────── gen_turn_groove ──
// Plunge grooves with infeed pecks and dwell at depth.
// geometry["points"] = [[z, target_radius], ...]; falls back to heights.top / center.
GenResult gen_turn_groove(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);

    std::vector<std::pair<double,double>> gpts; // {z_axial, target_radius}
    if (op.geometry.contains("points"))
        for (auto& p : op.geometry["points"])
            gpts.push_back({p[0].get<double>(), p[1].get<double>()});

    auto prof = read_profile(op);
    double od = prof_max_r(prof);
    if (od <= 0) od = stock_od(ctx, prof, 0);

    if (gpts.empty())
        gpts.push_back({op.heights.top, 0.0}); // default: face to center

    double clr    = op.turn_clearance > 0 ? op.turn_clearance : 1.0;
    double infeed = op.infeed         > 0 ? op.infeed         : 0.5;
    double feed   = op.feed           > 0 ? op.feed           : 50.0;
    double plunge = op.plunge         > 0 ? op.plunge         : feed * 0.4;
    double dwell  = op.dwell;

    Toolpath tp;
    for (auto& [gz, gr] : gpts) {
        double approach = od + clr;
        t_rapid(tp, approach, gz);
        // Peck infeed loop: retract to stock OD between pecks
        for (double cur = od; cur > gr + 1e-6; ) {
            cur = std::max(gr, cur - infeed);
            t_feed(tp, cur, gz, plunge);
            if (cur <= gr + 1e-6)
                t_dwell(tp, dwell);
            t_rapid(tp, approach, gz);
        }
    }

    finalize(res, tp, op, ctx, (int)std::lround(op.rpm));
    return res;
}

// ─────────────────────────────────────────────────────────── gen_turn_thread ──
// Long-hand G76-style threading: degressive-infeed passes for sim/backplot.
// The post maps op metadata to G76; these moves drive simulation.
GenResult gen_turn_thread(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);

    double pitch  = op.thread_pitch > 0 ? op.thread_pitch  : 1.5;
    // 60° thread form depth ≈ 0.6495 * pitch
    double depth  = op.thread_depth > 0 ? op.thread_depth  : 0.6495 * pitch;
    double infeed = op.infeed       > 0 ? op.infeed        : 0.08;
    double clr    = op.turn_clearance > 0 ? op.turn_clearance : 1.0;

    auto prof  = read_profile(op);
    double od  = prof_max_r(prof);
    if (od <= 0) od = stock_od(ctx, prof, 0);

    // Thread Z extents from profile Z range or heights
    double z_hi = prof.empty() ? op.heights.top    : prof_z_hi(prof);
    double z_lo = prof.empty() ? op.heights.bottom : prof_z_lo(prof);
    if (z_hi < z_lo) std::swap(z_hi, z_lo);

    // Thread feed (mm/min): pitch * rpm represents the synchronised leadscrew rate
    double tf = op.lead_feed > 0 ? op.lead_feed
              : (op.rpm > 0 ? pitch * op.rpm : op.feed);
    if (tf <= 0) tf = 200.0;

    // Auto pass count: degressive infeed → cumulative depth = infeed * sqrt(n)
    int n = op.thread_passes;
    if (n <= 0) {
        n = 1;
        while (infeed * std::sqrt((double)n) < depth - 1e-6 && n < 200) ++n;
    }
    // One extra spring pass at full depth
    int total = n + 1;

    Toolpath tp;
    t_rapid(tp, od + clr, z_hi + pitch); // start above thread free end

    for (int pass = 1; pass <= total; ++pass) {
        // Degressive cumulative depth; final and spring passes at full depth
        double x_depth = (pass < n) ? infeed * std::sqrt((double)pass) : depth;
        x_depth = std::min(x_depth, depth);
        double thread_r = std::max(0.0, od - x_depth);

        // Approach: rapid to safe axial start at this X depth (angled infeed axis)
        t_rapid(tp, od + clr,  z_hi + pitch);  // safe retract
        t_rapid(tp, thread_r,  z_hi + pitch);  // infeed to thread depth
        t_feed(tp,  thread_r,  z_lo - pitch, tf); // synchronised Z sweep
        t_rapid(tp, od + clr,  z_lo - pitch);  // X retract
    }

    t_rapid(tp, od + clr, z_hi + clr);
    finalize(res, tp, op, ctx, (int)std::lround(op.rpm));
    return res;
}

// ──────────────────────────────────────────────────────────── gen_turn_part ──
// Parting/cutoff: radial plunge from OD to part_off radius with peck retracts.
// geometry["points"][0] = [z_axial, final_radius] or falls back to op.heights/op_num.
GenResult gen_turn_part(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);

    double part_z    = op_num(op, "part_x", op.heights.top);
    double part_off_r = 0.0;
    if (op.geometry.contains("points") && !op.geometry["points"].empty()) {
        auto& pt  = op.geometry["points"][0];
        part_z    = pt[0].get<double>();
        part_off_r = pt.size() > 1 ? pt[1].get<double>() : 0.0;
    }

    auto prof = read_profile(op);
    double od = prof_max_r(prof);
    if (od <= 0) od = stock_od(ctx, prof, 0);

    double clr    = op.turn_clearance > 0 ? op.turn_clearance : 1.0;
    double peck   = op.peck           > 0 ? op.peck           : od * 0.3;
    double feed   = op.feed           > 0 ? op.feed           : 30.0;
    double plunge = op.plunge         > 0 ? op.plunge         : feed * 0.5;
    double dwell  = op.dwell;

    Toolpath tp;
    t_rapid(tp, od + clr, part_z);

    for (double cur = od; cur > part_off_r + 1e-6; ) {
        cur = std::max(part_off_r, cur - peck);
        t_feed(tp, cur, part_z, plunge);
        if (cur <= part_off_r + 1e-6) {
            t_dwell(tp, dwell);
        } else {
            t_rapid(tp, od + clr, part_z); // chip-break retract
        }
    }

    t_rapid(tp, od + clr, part_z);
    finalize(res, tp, op, ctx, (int)std::lround(op.rpm));
    return res;
}

// ──────────────────────────────────────────────────────────── gen_turn_drill ──
// Center drilling on the spindle axis (r = 0): pecked cycle along -Z.
GenResult gen_turn_drill(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);

    double z_top   = op.heights.top;
    double z_final = op.heights.bottom - op.tip_through;
    if (z_top < z_final) std::swap(z_top, z_final); // drilling goes in -Z direction

    double clr    = op.turn_clearance > 0 ? op.turn_clearance : 1.0;
    double peck   = op.peck > 0 ? op.peck
                  : (ctx.tool.diameter > 0 ? ctx.tool.diameter : 3.0);
    double plunge = op.plunge > 0 ? op.plunge : (op.feed > 0 ? op.feed * 0.5 : 60.0);
    double dwell  = op.dwell;
    double ret_z  = z_top + clr;

    Toolpath tp;
    t_rapid(tp, 0.0, ret_z);

    double z = z_top;
    while (z > z_final + 1e-6) {
        double next_z = std::max(z_final, z - peck);
        t_feed(tp, 0.0, next_z, plunge);
        if (next_z <= z_final + 1e-6)
            t_dwell(tp, dwell);
        t_rapid(tp, 0.0, ret_z);           // full retract (clears chips)
        if (next_z > z_final + 1e-6)
            t_rapid(tp, 0.0, next_z + 0.5); // re-approach to near last depth
        z = next_z;
    }

    t_rapid(tp, 0.0, ret_z);
    finalize(res, tp, op, ctx, (int)std::lround(op.rpm));
    return res;
}

} // namespace cam
} // namespace chudware
