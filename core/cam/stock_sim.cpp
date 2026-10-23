// core/cam/stock_sim.cpp — material-removal simulation, cycle-time estimation,
// and the classified backplot. A top-down Z-dexel grid (HeightField) of the stock
// is lowered by each cutting move; removed volume = initial minus remaining.
#define _USE_MATH_DEFINES  // MSVC needs this before <cmath> for M_PI (mirrors geom/plane.cpp)
#include "cam/stock_sim.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <iomanip>
#include <sstream>

namespace chudware {
namespace cam {

namespace {
// A flat plane mesh over [lo..hi] (expanded by `grow`) at height z, for seeding a
// stock HeightField at the stock top.
TriMesh flat_plane(const Vec2& lo, const Vec2& hi, double z, double grow) {
    double x0 = lo.x - grow, x1 = hi.x + grow, y0 = lo.y - grow, y1 = hi.y + grow;
    TriMesh m;
    m.verts = {{x0, y0, z}, {x1, y0, z}, {x1, y1, z}, {x0, y1, z}};
    m.tris = {{0, 1, 2}, {0, 2, 3}};
    return m;
}

double dist_point_seg(double px, double py, double ax, double ay, double bx, double by, double& t) {
    double dx = bx - ax, dy = by - ay;
    double den = dx * dx + dy * dy;
    if (den < 1e-18) { t = 0; return std::hypot(px - ax, py - ay); }
    t = std::max(0.0, std::min(1.0, ((px - ax) * dx + (py - ay) * dy) / den));
    return std::hypot(px - (ax + t * dx), py - (ay + t * dy));
}

// Shared per-move time (seconds). Used by estimate_time and the sim sweep
// accumulator so frame times align with cycle_time_s. `from` is the previous
// position; for the first real move of a path the caller skips (have=false).
double move_time_s(const Move& mv, const Vec3& from, const Machine& m) {
    if (mv.kind == MoveKind::Dwell) return mv.dwell_s;
    if (mv.kind == MoveKind::ToolChange) return m.tool_change_s;
    double rapid_xy = m.rapid_xy > 0 ? m.rapid_xy : 5000;
    double rapid_z = m.rapid_z > 0 ? m.rapid_z : 3000;
    double accel = m.accel;  // mm/s^2; 0 = feed-limited only
    double len;
    if (mv.kind == MoveKind::Arc) {
        double r = std::hypot(from.x - mv.center.x, from.y - mv.center.y);
        double a0 = std::atan2(from.y - mv.center.y, from.x - mv.center.x);
        double a1 = std::atan2(mv.to.y - mv.center.y, mv.to.x - mv.center.x);
        double sweep = a1 - a0;
        if (mv.ccw) { while (sweep <= 0) sweep += 2 * M_PI; }
        else { while (sweep >= 0) sweep -= 2 * M_PI; sweep = -sweep; }
        len = r * sweep;
    } else {
        len = std::sqrt((mv.to.x - from.x) * (mv.to.x - from.x) +
                        (mv.to.y - from.y) * (mv.to.y - from.y) +
                        (mv.to.z - from.z) * (mv.to.z - from.z));
    }
    if (mv.kind == MoveKind::Rapid || mv.kind == MoveKind::Retract) {
        double dxy = std::hypot(mv.to.x - from.x, mv.to.y - from.y);
        double dz = std::fabs(mv.to.z - from.z);
        return 60.0 * std::max(dxy / rapid_xy, dz / rapid_z);
    }
    double feed_mm_min = mv.feed > 0 ? mv.feed : 600;
    double v = feed_mm_min / 60.0;  // mm/s
    if (accel > 0 && v > 0) {
        double d_acc = v * v / accel;  // accel + decel distance
        if (len >= d_acc) return (len - d_acc) / v + 2.0 * v / accel;
        return 2.0 * std::sqrt(len / accel);
    }
    return 60.0 * len / feed_mm_min;
}

// Downsample a heightfield to ≤max_axis per side by strided sampling (frames payload only).
HeightField downsample_field(const HeightField& src, int max_axis, bool& clamped) {
    int nx = src.nx(), ny = src.ny();
    if (nx <= 0 || ny <= 0) return src;
    if (nx <= max_axis && ny <= max_axis) return src;
    int step_x = std::max(1, (nx + max_axis - 1) / max_axis);
    int step_y = std::max(1, (ny + max_axis - 1) / max_axis);
    int step = std::max(step_x, step_y);
    int nnx = std::max(1, (nx + step - 1) / step);
    int nny = std::max(1, (ny + step - 1) / step);
    double nres = src.res() * step;
    HeightField out = HeightField::empty(nnx, nny, nres, src.lo(), src.floor());
    for (int j = 0; j < nny; ++j)
        for (int i = 0; i < nnx; ++i)
            out.set_raw(i, j, src.raw(i * step, j * step));
    clamped = true;
    return out;
}

bool field_z_equal(const HeightField& a, const HeightField& b) {
    if (a.nx() != b.nx() || a.ny() != b.ny()) return false;
    for (int j = 0; j < a.ny(); ++j)
        for (int i = 0; i < a.nx(); ++i)
            if (a.raw(i, j) != b.raw(i, j)) return false;
    return true;
}
std::string lineage_hash(const Stock& stock, const std::vector<Toolpath>& toolpaths) {
    std::string bytes = stock.lineage_id.empty() ? stock.to_json().dump() : stock.lineage_id;
    for (const auto& path : toolpaths) bytes += path.to_json().dump();
    uint64_t hash = 1469598103934665603ULL;
    for (const unsigned char byte : bytes) {
        hash ^= byte;
        hash *= 1099511628211ULL;
    }
    std::ostringstream out;
    out << "stock:" << std::hex << std::setfill('0') << std::setw(16) << hash;
    return out.str();
}
}  // namespace

double estimate_time(const Toolpath& tp, const Machine& m) {
    double t = 0;
    Vec3 prev{0, 0, 0};
    bool have = false;
    for (const Move& mv : tp.moves) {
        if (mv.kind == MoveKind::Dwell) { t += move_time_s(mv, prev, m); continue; }
        if (mv.kind == MoveKind::ToolChange) { t += move_time_s(mv, prev, m); continue; }
        if (!have) { prev = mv.to; have = true; continue; }
        t += move_time_s(mv, prev, m);
        prev = mv.to;
    }
    return t;
}


json backplot(const Toolpath& tp) {
    json rapid = json::array(), cut = json::array(), lead = json::array(),
         plunge = json::array(), arc = json::array();
    Vec3 prev{0, 0, 0};
    bool have = false;
    auto push3 = [](json& a, const Vec3& p) { a.push_back(p.x); a.push_back(p.y); a.push_back(p.z); };
    for (const Move& m : tp.moves) {
        if (m.kind == MoveKind::Dwell || m.kind == MoveKind::ToolChange) continue;
        if (!have) { prev = m.to; have = true; continue; }
        switch (m.kind) {
            case MoveKind::Rapid:
            case MoveKind::Retract: push3(rapid, prev); push3(rapid, m.to); break;
            case MoveKind::Lead:    push3(lead, prev);  push3(lead, m.to);  break;
            case MoveKind::Plunge:  push3(plunge, prev); push3(plunge, m.to); break;
            case MoveKind::Cut:     push3(cut, prev);   push3(cut, m.to);   break;
            case MoveKind::Arc: {
                // tessellate into ~5deg segments for display
                double r = std::hypot(prev.x - m.center.x, prev.y - m.center.y);
                double a0 = std::atan2(prev.y - m.center.y, prev.x - m.center.x);
                double a1 = std::atan2(m.to.y - m.center.y, m.to.x - m.center.x);
                double sweep = a1 - a0;
                if (m.ccw) { while (sweep <= 0) sweep += 2 * M_PI; }
                else { while (sweep >= 0) sweep -= 2 * M_PI; }
                int n = std::max(2, (int)std::ceil(std::fabs(sweep) / (5.0 * M_PI / 180.0)));
                Vec3 p = prev;
                for (int i = 1; i <= n; ++i) {
                    double a = a0 + sweep * i / n;
                    double z = prev.z + (m.to.z - prev.z) * i / n;
                    Vec3 q{m.center.x + r * std::cos(a), m.center.y + r * std::sin(a), z};
                    push3(arc, p); push3(arc, q);
                    p = q;
                }
                break;
            }
            default: break;
        }
        prev = m.to;
    }
    return json{{"rapid", rapid}, {"cut", cut}, {"lead", lead}, {"plunge", plunge}, {"arc", arc}};
}

SimResult simulate_setup(const Setup& setup, const ToolLibrary& tools,
                         const std::vector<Toolpath>& toolpaths, const TriMesh& part_mesh,
                         const SimOptions& opt) {
    SimResult res;
    const Stock& st = setup.stock;
    res.stock_lineage_in = st.lineage_id.empty() ? "unbound_source_stock" : st.lineage_id;
    double res_mm = opt.resolution > 0 ? opt.resolution : 0.5;

    // resolve stock bounds + bottom + analytic volume
    Vec2 lo, hi;
    double top, bottom, stock_vol;
    bool cylinder = (st.type == StockType::Cylinder || st.type == StockType::CylinderOffset);
    Vec2 cyl_c{0, 0};
    double cyl_r = 0;
    if (cylinder) {
        cyl_c = {st.base.x, st.base.y};
        cyl_r = st.radius + (st.type == StockType::CylinderOffset ? st.off_radial : 0);
        lo = {cyl_c.x - cyl_r, cyl_c.y - cyl_r};
        hi = {cyl_c.x + cyl_r, cyl_c.y + cyl_r};
        bottom = st.base.z;
        top = st.base.z + st.height + (st.type == StockType::CylinderOffset ? 2 * st.off_axial : 0);
        stock_vol = M_PI * cyl_r * cyl_r * (top - bottom);
    } else if (st.type == StockType::FromSolid && !st.solid.verts.empty()) {
        HeightField hf = HeightField::from_mesh(st.solid, res_mm, 0, 0);
        lo = hf.lo(); hi = hf.hi();
        top = 0; bottom = 0;
        for (auto& v : st.solid.verts) { top = std::max(top, v.z); bottom = std::min(bottom, v.z); }
        stock_vol = 0;  // approximated from field below
    } else {
        lo = {std::min(st.min.x, st.max.x), std::min(st.min.y, st.max.y)};
        hi = {std::max(st.min.x, st.max.x), std::max(st.min.y, st.max.y)};
        bottom = std::min(st.min.z, st.max.z);
        top = std::max(st.min.z, st.max.z);
        stock_vol = (hi.x - lo.x) * (hi.y - lo.y) * (top - bottom);
    }
    if (hi.x <= lo.x || hi.y <= lo.y || top <= bottom) {
        res.ok = false; res.error = "stock has zero extent"; return res;
    }
    if (setup.machine.kind == MachineKind::Laser ||
        setup.machine.kind == MachineKind::Plasma ||
        setup.machine.kind == MachineKind::Waterjet) {
        res.ok = false;
        res.error = "cut_process_physics_unmodelled_use_vector_fidelity_only";
        return res;
    }

    // ── turning: axisymmetric radial-profile sim ──────────────────────────────
    // A top-down Z-dexel can't represent diameter reduction (removal is radial, the
    // dexel's height axis is the spindle axis). For a lathe + cylindrical stock, model
    // the stock as a radius profile R(z) and lower it as each turning pass cuts; removed
    // volume integrates π(R0²−R(z)²)dz along the axis.
    if (setup.machine.kind == MachineKind::Lathe && cylinder) {
        int nz = std::max(2, (int)std::ceil((top - bottom) / res_mm));
        double dz = (top - bottom) / nz;
        std::vector<double> rad(nz, cyl_r);   // outer radius per axial slice
        for (const Toolpath& tp : toolpaths) {
            Vec3 prev{0, 0, 0};
            bool have = false;
            for (const Move& m : tp.moves) {
                if (m.kind == MoveKind::Dwell || m.kind == MoveKind::ToolChange) continue;
                Vec3 to = m.to;
                if (!have) { prev = to; have = true; continue; }
                bool cutting = (m.kind == MoveKind::Cut || m.kind == MoveKind::Plunge ||
                                m.kind == MoveKind::Lead || m.kind == MoveKind::Arc);
                if (cutting) {
                    double z0 = std::min(prev.z, to.z), z1 = std::max(prev.z, to.z);
                    bool radial = std::fabs(to.z - prev.z) < 1e-9;  // pure infeed at one Z
                    for (int k = 0; k < nz; ++k) {
                        double zc = bottom + (k + 0.5) * dz;
                        if (zc < z0 - dz || zc > z1 + dz) continue;
                        double xr;
                        if (radial) {
                            xr = std::min(std::fabs(prev.x), std::fabs(to.x));
                        } else {
                            double f = (zc - prev.z) / (to.z - prev.z);
                            f = std::max(0.0, std::min(1.0, f));
                            xr = std::fabs(prev.x + (to.x - prev.x) * f);
                        }
                        if (xr < rad[k]) rad[k] = xr;   // tool leaves the smallest radius it reached
                    }
                }
                prev = to;
            }
        }
        double remaining = 0;
        for (int k = 0; k < nz; ++k) remaining += M_PI * rad[k] * rad[k] * dz;
        double removed = std::max(0.0, stock_vol - remaining);
        res.stock_volume_mm3 = stock_vol;
        res.removed_volume_mm3 = removed;
        res.remaining_volume_mm3 = std::max(0.0, remaining);
        res.machined_fraction = removed / std::max(1e-9, stock_vol);
        double total = 0;
        int last_tool = -999;
        for (const Toolpath& tp : toolpaths) {
            if (tp.tool_number != last_tool) { total += setup.machine.tool_change_s; last_tool = tp.tool_number; }
            total += estimate_time(tp, setup.machine);
        }
        res.cycle_time_s = total;
        res.stock_lineage_out = lineage_hash(st, toolpaths);
        return res;
    }

    // seed the dexel field at the stock top
    HeightField field = HeightField::from_mesh(flat_plane(lo, hi, top, res_mm), res_mm, 0, bottom);
    int nx = field.nx(), ny = field.ny();
    double cell_area = res_mm * res_mm;
    Vec2 flo = field.lo();

    // mask cells outside a cylindrical stock (no material -> bottom)
    if (cylinder) {
        for (int j = 0; j < ny; ++j)
            for (int i = 0; i < nx; ++i) {
                double x = flo.x + i * res_mm, y = flo.y + j * res_mm;
                if (std::hypot(x - cyl_c.x, y - cyl_c.y) > cyl_r) field.set_raw(i, j, bottom);
            }
    }
    if (st.type == StockType::FromSolid && !st.solid.verts.empty()) {
        HeightField sf = HeightField::from_mesh(st.solid, res_mm, 0, bottom);
        for (int j = 0; j < ny; ++j)
            for (int i = 0; i < nx; ++i) {
                double x = flo.x + i * res_mm, y = flo.y + j * res_mm;
                field.set_raw(i, j, sf.height_at(x, y));
            }
    }

    auto field_sum = [&]() {
        double s = 0;
        for (int j = 0; j < ny; ++j)
            for (int i = 0; i < nx; ++i) s += std::max(0.0, field.raw(i, j) - bottom);
        return s * cell_area;
    };
    double initial_sum = field_sum();
    if (st.type == StockType::FromSolid) stock_vol = initial_sum;

    // part surface heightfield for gouge checks
    HeightField part;
    bool has_part = !part_mesh.verts.empty();
    if (has_part) part = HeightField::from_mesh(part_mesh, res_mm, 0, bottom - 1e6);

    // lower the field along every cutting move; flag rapids through stock
    int rapid_hits = 0;
    int holder_hits = 0;

    // ── frame snapshots (aicad.cam_sim_frames/1) ──────────────────────────
    // Total time up front with the same model the sweep accumulates, then
    // snapshot the field at equal time intervals as the sweep crosses them.
    int want_frames = opt.frames > 0 ? opt.frames : 0;
    if (want_frames > 64) { want_frames = 64; res.frames_clamped = true; }
    double frame_total = 0;
    if (want_frames > 0) {
        int lt = -999;
        for (const Toolpath& tp : toolpaths) {
            if (tp.tool_number != lt) { frame_total += setup.machine.tool_change_s; lt = tp.tool_number; }
            frame_total += estimate_time(tp, setup.machine);
        }
    }
    double elapsed = 0;
    int frames_taken = 0;
    auto push_frame = [&](double t) {
        bool dc = false;
        HeightField snap = downsample_field(field, 128, dc);
        if (dc) res.frames_clamped = true;
        res.frames.push_back(std::move(snap));
        res.frame_times_s.push_back(t);
    };
    auto cross_frames = [&]() {
        if (want_frames <= 0 || frame_total <= 0) return;
        while (frames_taken < want_frames &&
               elapsed >= (double)(frames_taken + 1) * frame_total / want_frames) {
            ++frames_taken;
            push_frame(elapsed);
        }
    };
    int last_tool_frame = -999;
    for (const Toolpath& tp : toolpaths) {
        if (want_frames > 0 && tp.tool_number != last_tool_frame) {
            elapsed += setup.machine.tool_change_s;
            last_tool_frame = tp.tool_number;
            cross_frames();
        }
        const Tool* tl = tools.find(tp.tool_number);
        double r = tl ? tl->effective_radius() : 1.0;
        // non-cutting tool body for holder/shank collision: the flutes cut
        // to tip_z..tip_z+flute_length at radius r; above that the (wider) shank runs to
        // the shoulder and the holder above that. Any remaining stock that rises into
        // those non-cutting zones is a crash the tip-only sim misses.
        double shank_r = tl ? tl->shank_diameter * 0.5 : 0;
        double holder_r = tl ? tl->holder_diameter * 0.5 : 0;
        double flute_len = tl ? tl->flute_length : 0;
        double shoulder_len = tl ? tl->shoulder_length : 0;
        Vec3 prev{0, 0, 1e9};
        bool have = false;
        // lower the dexel field under the tool disc along one straight sub-segment a→b
        // (cutting), or flag a rapid that passes through remaining stock.
        auto sweep_seg = [&](const Vec3& a, const Vec3& b, bool cutting, bool rapid) {
            double minx = std::min(a.x, b.x) - r, maxx = std::max(a.x, b.x) + r;
            double miny = std::min(a.y, b.y) - r, maxy = std::max(a.y, b.y) + r;
            int i0 = std::max(0, (int)std::floor((minx - flo.x) / res_mm));
            int i1 = std::min(nx - 1, (int)std::ceil((maxx - flo.x) / res_mm));
            int j0 = std::max(0, (int)std::floor((miny - flo.y) / res_mm));
            int j1 = std::min(ny - 1, (int)std::ceil((maxy - flo.y) / res_mm));
            for (int j = j0; j <= j1; ++j)
                for (int i = i0; i <= i1; ++i) {
                    double x = flo.x + i * res_mm, y = flo.y + j * res_mm;
                    double t;
                    double d = dist_point_seg(x, y, a.x, a.y, b.x, b.y, t);
                    if (d > r) continue;
                    double zt = a.z + (b.z - a.z) * t;  // tool tip Z at nearest pass
                    if (cutting) {
                        if (zt < field.raw(i, j)) field.set_raw(i, j, zt);
                    } else if (rapid && opt.check_rapid) {
                        if (field.raw(i, j) > zt + 0.01) { ++rapid_hits; }
                    }
                }
        };
        // does the non-cutting body sweep of a→b intersect any remaining stock? Within
        // the shank radius, stock above the flute end hits the shank; in the annulus out
        // to the holder radius, stock above the shoulder hits the holder. tip_z tracks the
        // tip along the segment so a plunging move raises the whole envelope with it.
        auto holder_hit_seg = [&](const Vec3& a, const Vec3& b) -> bool {
            double rr = std::max(shank_r, holder_r);
            if (rr <= 0) return false;
            double minx = std::min(a.x, b.x) - rr, maxx = std::max(a.x, b.x) + rr;
            double miny = std::min(a.y, b.y) - rr, maxy = std::max(a.y, b.y) + rr;
            int i0 = std::max(0, (int)std::floor((minx - flo.x) / res_mm));
            int i1 = std::min(nx - 1, (int)std::ceil((maxx - flo.x) / res_mm));
            int j0 = std::max(0, (int)std::floor((miny - flo.y) / res_mm));
            int j1 = std::min(ny - 1, (int)std::ceil((maxy - flo.y) / res_mm));
            for (int j = j0; j <= j1; ++j)
                for (int i = i0; i <= i1; ++i) {
                    double x = flo.x + i * res_mm, y = flo.y + j * res_mm;
                    double t;
                    double d = dist_point_seg(x, y, a.x, a.y, b.x, b.y, t);
                    if (d > rr) continue;
                    double tipz = a.z + (b.z - a.z) * t;
                    double h = field.raw(i, j);
                    if (d <= shank_r) {
                        if (h > tipz + flute_len + opt.gouge_tol) return true;
                    } else if (d <= holder_r) {
                        if (h > tipz + shoulder_len + opt.gouge_tol) return true;
                    }
                }
            return false;
        };
        for (const Move& m : tp.moves) {
            if (m.kind == MoveKind::Dwell || m.kind == MoveKind::ToolChange) {
                if (want_frames > 0) { elapsed += move_time_s(m, prev, setup.machine); cross_frames(); }
                continue;
            }
            Vec3 to = m.to;
            if (!have) { prev = to; have = true; continue; }
            double frame_from = elapsed;
            if (want_frames > 0) elapsed += move_time_s(m, prev, setup.machine);
            bool cutting = (m.kind == MoveKind::Cut || m.kind == MoveKind::Plunge ||
                            m.kind == MoveKind::Lead || m.kind == MoveKind::Arc);
            bool rapid = (m.kind == MoveKind::Rapid || m.kind == MoveKind::Retract);
            bool check_holder = cutting && opt.check_holder && (shank_r > 0 || holder_r > 0);
            bool holder_hit = false;
            if (m.kind == MoveKind::Arc) {
                // sweep the TRUE arc (center+radius carried on the Move), not the chord:
                // a chord bows inside a convex arc and under-removes near the midpoint,
                // which false-negatives the gouge check on curved passes.
                double cx = m.center.x, cy = m.center.y;
                double rad = std::hypot(prev.x - cx, prev.y - cy);
                double a0 = std::atan2(prev.y - cy, prev.x - cx);
                double a1 = std::atan2(to.y - cy, to.x - cx);
                double da = a1 - a0;
                if (m.ccw) { if (da <= 1e-9) da += 2 * M_PI; }
                else       { if (da >= -1e-9) da -= 2 * M_PI; }
                double arclen = std::fabs(da) * rad;
                // sample ~2 points per grid cell so the arc's mid-bulge is captured
                int segs = std::max(2, (int)std::ceil(arclen / std::max(1e-6, res_mm * 0.5)));
                segs = std::min(segs, 1440);
                Vec3 a = prev;
                for (int k = 1; k <= segs; ++k) {
                    double f = (double)k / segs;
                    Vec3 b = (k == segs) ? to
                             : Vec3{cx + rad * std::cos(a0 + da * f),
                                    cy + rad * std::sin(a0 + da * f),
                                    prev.z + (to.z - prev.z) * f};
                    sweep_seg(a, b, cutting, rapid);
                    if (check_holder && !holder_hit) holder_hit = holder_hit_seg(a, b);
                    a = b;
                }
            } else {
                sweep_seg(prev, to, cutting, rapid);
                if (check_holder) holder_hit = holder_hit_seg(prev, to);
            }
            if (holder_hit) ++holder_hits;
            prev = to;
            (void)frame_from;
            cross_frames();
        }
    }
    // guarantee the last frame is the final remnant field (never exceeding the 64 cap)
    if (want_frames > 0 && res.frames.size() < 64) {
        if (res.frames.empty() || !field_z_equal(res.frames.back(), field) ||
            res.frame_times_s.back() < frame_total) {
            push_frame(frame_total > 0 ? frame_total : elapsed);
        }
    }
    double final_sum = field_sum();
    double removed = std::max(0.0, initial_sum - final_sum);

    res.stock_volume_mm3 = stock_vol;
    res.removed_volume_mm3 = removed;
    res.remaining_volume_mm3 = std::max(0.0, stock_vol - removed);
    res.rapid_collisions = rapid_hits;
    if (rapid_hits > 0) res.warnings.push_back("rapid move(s) pass through remaining stock");
    res.holder_collisions = holder_hits;
    if (holder_hits > 0) res.warnings.push_back("tool holder/shank collides with remaining stock");

    // gouge: remaining material below the part surface
    if (has_part && opt.check_gouge) {
        int gouges = 0;
        double maxg = 0;
        for (int j = 0; j < ny; ++j)
            for (int i = 0; i < nx; ++i) {
                double x = flo.x + i * res_mm, y = flo.y + j * res_mm;
                double pz = part.height_at(x, y);
                if (pz < bottom - 1e5) continue;  // outside the part footprint
                double fz = field.raw(i, j);
                if (fz < pz - opt.gouge_tol) { ++gouges; maxg = std::max(maxg, pz - fz); }
            }
        res.gouges = gouges;
        res.max_gouge_mm = maxg;
        if (gouges > 0) res.warnings.push_back("toolpath gouges below the part surface");
    }

    double machinable = std::max(1e-9, stock_vol);
    if (has_part) {
        // ideal removable = stock - part volume (approx part vol from its field over footprint)
        // keep simple: machined_fraction = removed / stock
    }
    res.machined_fraction = removed / machinable;

    // cycle time
    double total = 0;
    int last_tool = -999;
    for (const Toolpath& tp : toolpaths) {
        if (tp.tool_number != last_tool) { total += setup.machine.tool_change_s; last_tool = tp.tool_number; }
        total += estimate_time(tp, setup.machine);
    }
    res.cycle_time_s = total;

    res.remaining = std::move(field);
    return res;
}

json SimResult::to_json(bool include_field) const {
    json j{{"ok", ok}, {"error", error}, {"removed_volume_mm3", removed_volume_mm3},
           {"remaining_volume_mm3", remaining_volume_mm3}, {"stock_volume_mm3", stock_volume_mm3},
           {"machined_fraction", machined_fraction}, {"cycle_time_s", cycle_time_s},
           {"gouges", gouges}, {"max_gouge_mm", max_gouge_mm}, {"rapid_collisions", rapid_collisions},
           {"holder_collisions", holder_collisions}, {"warnings", warnings},
           {"stock_lineage_in", stock_lineage_in}, {"stock_lineage_out", stock_lineage_out}};
    if (include_field && remaining.nx() > 0) {
        json zs = json::array();
        for (int jj = 0; jj < remaining.ny(); ++jj)
            for (int ii = 0; ii < remaining.nx(); ++ii) zs.push_back(remaining.raw(ii, jj));
        j["field"] = json{{"nx", remaining.nx()}, {"ny", remaining.ny()}, {"res", remaining.res()},
                          {"lo", json::array({remaining.lo().x, remaining.lo().y})}, {"z", zs}};
    }
    frames_to_json(j);
    return j;
}

void SimResult::frames_to_json(json& j) const {
    if (frames.empty()) return;
    json arr = json::array();
    for (size_t k = 0; k < frames.size(); ++k) {
        const HeightField& f = frames[k];
        json zs = json::array();
        for (int jj = 0; jj < f.ny(); ++jj)
            for (int ii = 0; ii < f.nx(); ++ii) zs.push_back(f.raw(ii, jj));
        arr.push_back(json{{"t_s", frame_times_s[k]},
                           {"field", json{{"nx", f.nx()}, {"ny", f.ny()}, {"res", f.res()},
                                          {"lo", json::array({f.lo().x, f.lo().y})}, {"z", zs}}}});
    }
    j["frames"] = arr;
    if (frames_clamped) j["frames_clamped"] = true;
}

}  // namespace cam
}  // namespace chudware
