// core/cam/toolpath.cpp — toolpath assembly helpers shared by the generators.
#define _USE_MATH_DEFINES  // MSVC needs this before <cmath> for M_PI (mirrors geom/plane.cpp)
#include "cam/toolpath.h"

#include "cam/geometry.h"

#include <algorithm>
#include <cmath>

namespace chudware {
namespace cam {

// ───────────────────────────────────────────────────────── depth stepping ──
std::vector<double> depth_levels(double top, double bottom, double stepdown,
                                 double stock_to_leave_axial) {
    std::vector<double> levels;
    double final_z = bottom + stock_to_leave_axial;
    if (final_z >= top - 1e-9) { levels.push_back(final_z); return levels; }
    if (stepdown <= 1e-9) { levels.push_back(final_z); return levels; }
    double z = top - stepdown;
    while (z > final_z + 1e-6) {
        levels.push_back(z);
        z -= stepdown;
    }
    levels.push_back(final_z);
    return levels;
}

// ───────────────────────────────────────────────────────────── PathBuilder ──
PathBuilder::PathBuilder(const Heights& h, double feed, double plunge, double rapid_feed)
    : h_(h), feed_(feed > 0 ? feed : 600), plunge_(plunge > 0 ? plunge : 200),
      rapid_(rapid_feed > 0 ? rapid_feed : 5000) {}

namespace {
double dist3(const Vec3& a, const Vec3& b) {
    return std::sqrt((a.x - b.x) * (a.x - b.x) + (a.y - b.y) * (a.y - b.y) + (a.z - b.z) * (a.z - b.z));
}
}  // namespace

void PathBuilder::set_spindle(int rpm) { rpm_ = rpm; }

void PathBuilder::rapid_to_clearance() {
    if (cur_.z >= h_.clearance - 1e-9 && cur_.z < 1e8) { at_clear_ = true; return; }
    Move m;
    m.kind = MoveKind::Rapid;
    m.to = {cur_.x < 1e8 ? cur_.x : 0, cur_.y < 1e8 ? cur_.y : 0, h_.clearance};
    if (rpm_ > 0) m.spindle_rpm = rpm_;
    tp_.moves.push_back(m);
    cur_ = m.to;
    at_clear_ = true;
}
void PathBuilder::rapid_xy(const Vec2& p) {
    rapid_to_clearance();
    Move m;
    m.kind = MoveKind::Rapid;
    m.to = {p.x, p.y, h_.clearance};
    if (rpm_ > 0) m.spindle_rpm = rpm_;
    tp_.moves.push_back(m);
    cur_ = m.to;
}
void PathBuilder::rapid_to(const Vec3& v) {
    Move m;
    m.kind = MoveKind::Rapid;
    m.to = v;
    if (rpm_ > 0) m.spindle_rpm = rpm_;
    tp_.moves.push_back(m);
    cur_ = v;
    at_clear_ = (v.z >= h_.clearance - 1e-9);
}
void PathBuilder::plunge_to(double z) {
    Move m;
    m.kind = MoveKind::Plunge;
    m.to = {cur_.x, cur_.y, z};
    m.feed = plunge_;
    tp_.moves.push_back(m);
    cur_ = m.to;
    at_clear_ = false;
}
void PathBuilder::feed_to(const Vec2& p, double z) {
    Move m;
    m.kind = MoveKind::Cut;
    m.to = {p.x, p.y, z};
    m.feed = feed_;
    tp_.moves.push_back(m);
    cur_ = m.to;
    at_clear_ = false;
}
void PathBuilder::feed_to(const Vec3& v) {
    Move m;
    m.kind = MoveKind::Cut;
    m.to = v;
    m.feed = feed_;
    tp_.moves.push_back(m);
    cur_ = v;
    at_clear_ = false;
}
void PathBuilder::arc_to(const Vec2& end, const Vec2& center, bool ccw, double z) {
    Move m;
    m.kind = MoveKind::Arc;
    m.to = {end.x, end.y, z};
    m.center = {center.x, center.y, z};
    m.ccw = ccw;
    m.feed = feed_;
    tp_.moves.push_back(m);
    cur_ = m.to;
    at_clear_ = false;
}
void PathBuilder::lead(const Path2& pts, double z, double feed) {
    for (auto& p : pts) {
        Move m;
        m.kind = MoveKind::Lead;
        m.to = {p.x, p.y, z};
        m.feed = feed > 0 ? feed : feed_;
        tp_.moves.push_back(m);
        cur_ = m.to;
    }
    at_clear_ = false;
}
void PathBuilder::dwell(double seconds) {
    Move m;
    m.kind = MoveKind::Dwell;
    m.to = cur_;
    m.dwell_s = seconds;
    tp_.moves.push_back(m);
}

void PathBuilder::cut_loop(const Path2& loop, double z, const Lead& in, const Lead& out,
                           CompSide side, bool climb, int tab_count, double tab_w, double tab_h,
                           double top_z) {
    if (loop.size() < 2) return;
    Vec2 p0 = loop.front();
    Vec2 p1 = loop[1];
    Vec2 dir{p1.x - p0.x, p1.y - p0.y};

    // lead-in
    Path2 lin = geo::lead_in_path(p0, dir, in, side, climb);
    Vec2 approach = lin.empty() ? p0 : lin.front();
    rapid_xy(approach);
    plunge_to(z);
    if (!lin.empty()) lead(lin, z, in.feed_scale * feed_);

    // walk the closed loop with optional tabs
    bool use_tabs = (tab_count > 0 && tab_h > 1e-6 && tab_w > 1e-6);
    double tab_z = std::min(top_z, z + tab_h);
    double perim = 0;
    size_t n = loop.size();
    std::vector<double> cum(n + 1, 0);
    for (size_t i = 0; i < n; ++i) {
        Vec2 a = loop[i], b = loop[(i + 1) % n];
        cum[i + 1] = cum[i] + std::hypot(b.x - a.x, b.y - a.y);
    }
    perim = cum[n];
    std::vector<double> tab_centers;
    if (use_tabs && perim > 1e-6)
        for (int t = 0; t < tab_count; ++t) tab_centers.push_back(perim * (t + 0.5) / tab_count);

    auto in_tab = [&](double s) -> bool {
        for (double tc : tab_centers) {
            double d = std::fabs(s - tc);
            d = std::min(d, perim - d);
            if (d <= tab_w * 0.5) return true;
        }
        return false;
    };

    feed_to(p0, z);
    for (size_t i = 0; i < n; ++i) {
        Vec2 a = loop[i], b = loop[(i + 1) % n];
        if (!use_tabs) { feed_to(b, z); continue; }
        // sample the segment so tab transitions land at the right place
        double seg = std::hypot(b.x - a.x, b.y - a.y);
        int steps = std::max(1, (int)std::ceil(seg / std::max(0.5, tab_w * 0.25)));
        for (int k = 1; k <= steps; ++k) {
            double t = (double)k / steps;
            Vec2 p{a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t};
            double s = cum[i] + seg * t;
            feed_to(p, in_tab(s) ? tab_z : z);
        }
    }

    // lead-out
    Vec2 lastDir{loop[1].x - loop[0].x, loop[1].y - loop[0].y};
    Path2 lout = geo::lead_out_path(p0, lastDir, out, side, climb);
    if (!lout.empty()) lead(lout, z, out.feed_scale * feed_);
    rapid_to_clearance();
}

void PathBuilder::cut_open(const Path2& path, double z, bool /*lead_link*/) {
    if (path.size() < 2) return;
    rapid_xy(path.front());
    plunge_to(z);
    for (size_t i = 1; i < path.size(); ++i) feed_to(path[i], z);
    rapid_to_clearance();
}

void PathBuilder::helix_entry(const Vec2& center, double radius, double top_z, double z,
                              double ramp_feed, double pitch) {
    if (pitch <= 1e-6) pitch = 1.0;
    double descent = top_z - z;
    if (descent <= 1e-6) { feed_to({center.x + radius, center.y}, z); return; }
    int turns = std::max(1, (int)std::ceil(descent / pitch));
    int seg_per_turn = 36;
    int total = turns * seg_per_turn;
    // start at angle 0 on the helix circle, top
    Vec2 start{center.x + radius, center.y};
    rapid_xy(start);
    plunge_to(top_z);
    double save = feed_;
    feed_ = ramp_feed > 0 ? ramp_feed : feed_;
    for (int i = 1; i <= total; ++i) {
        double frac = (double)i / total;
        double ang = 2 * M_PI * turns * frac;
        double zz = top_z - descent * frac;
        feed_to({center.x + radius * std::cos(ang), center.y + radius * std::sin(ang)}, zz);
    }
    // one flat finishing revolution at depth
    for (int i = 1; i <= seg_per_turn; ++i) {
        double ang = 2 * M_PI * i / seg_per_turn;
        feed_to({center.x + radius * std::cos(ang), center.y + radius * std::sin(ang)}, z);
    }
    feed_ = save;
}

void PathBuilder::zigzag_entry(const Vec2& a, const Vec2& b, double top_z, double z,
                               double ramp_feed) {
    double descent = top_z - z;
    if (descent <= 1e-6) { feed_to(b, z); return; }
    double seg = std::hypot(b.x - a.x, b.y - a.y);
    if (seg <= 1e-6) { plunge_to(z); return; }
    rapid_xy(a);
    plunge_to(top_z);
    double save = feed_;
    feed_ = ramp_feed > 0 ? ramp_feed : feed_;
    // ramp angle defaults handled by caller via z target; descend over passes
    int passes = std::max(2, (int)std::ceil(descent / std::max(0.2, seg * std::tan(2.0 * M_PI / 180))));
    double zz = top_z;
    double dz = descent / passes;
    for (int i = 0; i < passes; ++i) {
        zz -= dz;
        feed_to(((i & 1) ? a : b), zz);
    }
    feed_to(a, z);
    feed_to(b, z);
    feed_ = save;
}

// Toolpath statistics: cut/rapid lengths, bbox, and a feed-limited cycle-time
// estimate (rapid moves use the machine's rapid rates; accel is ignored here —
// stock_sim::estimate_time refines it when machine.accel > 0).
void Toolpath::update_stats(const Machine& m) {
    cut_length = 0;
    rapid_length = 0;
    cycle_time_s = 0;
    bbox_min = {1e300, 1e300, 1e300};
    bbox_max = {-1e300, -1e300, -1e300};
    Vec3 prev{0, 0, 0};
    bool have_prev = false;
    auto bump = [&](const Vec3& p) {
        bbox_min.x = std::min(bbox_min.x, p.x); bbox_min.y = std::min(bbox_min.y, p.y);
        bbox_min.z = std::min(bbox_min.z, p.z);
        bbox_max.x = std::max(bbox_max.x, p.x); bbox_max.y = std::max(bbox_max.y, p.y);
        bbox_max.z = std::max(bbox_max.z, p.z);
    };
    double rapid_xy = m.rapid_xy > 0 ? m.rapid_xy : 5000;
    double rapid_z = m.rapid_z > 0 ? m.rapid_z : 3000;
    for (const Move& mv : moves) {
        if (mv.kind == MoveKind::Dwell) { cycle_time_s += mv.dwell_s; continue; }
        if (mv.kind == MoveKind::ToolChange) { cycle_time_s += m.tool_change_s; continue; }
        bump(mv.to);
        if (!have_prev) { prev = mv.to; have_prev = true; bump(prev); continue; }
        double len;
        if (mv.kind == MoveKind::Arc) {
            double r = std::hypot(prev.x - mv.center.x, prev.y - mv.center.y);
            double a0 = std::atan2(prev.y - mv.center.y, prev.x - mv.center.x);
            double a1 = std::atan2(mv.to.y - mv.center.y, mv.to.x - mv.center.x);
            double sweep = a1 - a0;
            if (mv.ccw) { while (sweep <= 0) sweep += 2 * M_PI; }
            else { while (sweep >= 0) sweep -= 2 * M_PI; sweep = -sweep; }
            double dz = mv.to.z - prev.z;
            len = std::sqrt((r * sweep) * (r * sweep) + dz * dz);
        } else {
            len = dist3(prev, mv.to);
        }
        if (mv.kind == MoveKind::Rapid || mv.kind == MoveKind::Retract) {
            rapid_length += len;
            double dxy = std::hypot(mv.to.x - prev.x, mv.to.y - prev.y);
            double dz = std::fabs(mv.to.z - prev.z);
            cycle_time_s += 60.0 * std::max(dxy / rapid_xy, dz / rapid_z);
        } else {
            cut_length += len;
            double f = mv.feed > 0 ? mv.feed : 600;
            cycle_time_s += 60.0 * len / f;
        }
        prev = mv.to;
    }
    if (!have_prev) { bbox_min = {0, 0, 0}; bbox_max = {0, 0, 0}; }
}

Toolpath PathBuilder::take(int tool_number, const Machine& m) {
    tp_.tool_number = tool_number;
    tp_.update_stats(m);
    return std::move(tp_);
}

}  // namespace cam
}  // namespace chudware
