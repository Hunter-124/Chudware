// core/cam/stock_sim.cpp — material-removal simulation, cycle-time estimation,
// and the classified backplot. A top-down Z-dexel grid (HeightField) of the stock
// is lowered by each cutting move; removed volume = initial minus remaining.
#include "cam/stock_sim.h"

#include <algorithm>
#include <cmath>

namespace aicad {
namespace cam {

namespace {
// A flat plane mesh over [lo..hi] (expanded by `grow`) at height z, for seeding a
// stock HeightField at the stock top.
TriMesh flat_plane(const Vec2& lo, const Vec2& hi, double z, double grow) {
    TriMesh m;
    m.verts = {{lo.x - grow, lo.y - grow, z}, {hi.x + grow, lo.y - grow, z},
               {hi.x + grow, hi.y + grow, z}, {lo.x - grow, hi.y + grow, z}};
    m.tris = {{0, 1, 2}, {0, 2, 3}};
    return m;
}
double dist_point_seg(double px, double py, double ax, double ay, double bx, double by, double& t) {
    double dx = bx - ax, dy = by - ay;
    double L2 = dx * dx + dy * dy;
    if (L2 < 1e-12) { t = 0; return std::hypot(px - ax, py - ay); }
    t = ((px - ax) * dx + (py - ay) * dy) / L2;
    t = std::max(0.0, std::min(1.0, t));
    double cx = ax + dx * t, cy = ay + dy * t;
    return std::hypot(px - cx, py - cy);
}
}  // namespace

double estimate_time(const Toolpath& tp, const Machine& m) {
    double rapid_xy = m.rapid_xy > 0 ? m.rapid_xy : 5000;
    double rapid_z = m.rapid_z > 0 ? m.rapid_z : 3000;
    double accel = m.accel;  // mm/s^2; 0 = feed-limited only
    double t = 0;
    Vec3 prev{0, 0, 0};
    bool have = false;
    for (const Move& mv : tp.moves) {
        if (mv.kind == MoveKind::Dwell) { t += mv.dwell_s; continue; }
        if (mv.kind == MoveKind::ToolChange) { t += m.tool_change_s; continue; }
        if (!have) { prev = mv.to; have = true; continue; }
        double len;
        if (mv.kind == MoveKind::Arc) {
            double r = std::hypot(prev.x - mv.center.x, prev.y - mv.center.y);
            double a0 = std::atan2(prev.y - mv.center.y, prev.x - mv.center.x);
            double a1 = std::atan2(mv.to.y - mv.center.y, mv.to.x - mv.center.x);
            double sweep = a1 - a0;
            if (mv.ccw) { while (sweep <= 0) sweep += 2 * M_PI; }
            else { while (sweep >= 0) sweep -= 2 * M_PI; sweep = -sweep; }
            len = r * sweep;
        } else {
            len = std::sqrt((mv.to.x - prev.x) * (mv.to.x - prev.x) +
                            (mv.to.y - prev.y) * (mv.to.y - prev.y) +
                            (mv.to.z - prev.z) * (mv.to.z - prev.z));
        }
        double feed_mm_min;
        if (mv.kind == MoveKind::Rapid || mv.kind == MoveKind::Retract) {
            double dxy = std::hypot(mv.to.x - prev.x, mv.to.y - prev.y);
            double dz = std::fabs(mv.to.z - prev.z);
            t += 60.0 * std::max(dxy / rapid_xy, dz / rapid_z);
            prev = mv.to;
            continue;
        } else {
            feed_mm_min = mv.feed > 0 ? mv.feed : 600;
        }
        double v = feed_mm_min / 60.0;  // mm/s
        if (accel > 0 && v > 0) {
            // trapezoidal: time to reach v, distance to accel+decel
            double d_acc = v * v / accel;  // accel + decel distance
            if (len >= d_acc) t += (len - d_acc) / v + 2.0 * v / accel;
            else t += 2.0 * std::sqrt(len / accel);
        } else {
            t += 60.0 * len / feed_mm_min;
        }
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

    // ── turning: axisymmetric radial-profile sim ──────────────────────────────
    // A top-down Z-dexel can't represent diameter reduction (removal is radial, the
    // dexel's height axis is the spindle axis). For a lathe + cylindrical stock, model
    // the stock as a radius profile R(z) and lower it as each turning pass cuts; removed
    // volume integrates π(R0²−R(z)²)dz along the axis (audit G4).
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
    for (const Toolpath& tp : toolpaths) {
        const Tool* tl = tools.find(tp.tool_number);
        double r = tl ? tl->effective_radius() : 1.0;
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
        for (const Move& m : tp.moves) {
            if (m.kind == MoveKind::Dwell || m.kind == MoveKind::ToolChange) continue;
            Vec3 to = m.to;
            if (!have) { prev = to; have = true; continue; }
            bool cutting = (m.kind == MoveKind::Cut || m.kind == MoveKind::Plunge ||
                            m.kind == MoveKind::Lead || m.kind == MoveKind::Arc);
            bool rapid = (m.kind == MoveKind::Rapid || m.kind == MoveKind::Retract);
            if (m.kind == MoveKind::Arc) {
                // sweep the TRUE arc (center+radius carried on the Move), not the chord:
                // a chord bows inside a convex arc and under-removes near the midpoint,
                // which false-negatives the gouge check on curved passes (audit D8).
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
                    a = b;
                }
            } else {
                sweep_seg(prev, to, cutting, rapid);
            }
            prev = to;
        }
    }
    double final_sum = field_sum();
    double removed = std::max(0.0, initial_sum - final_sum);

    res.stock_volume_mm3 = stock_vol;
    res.removed_volume_mm3 = removed;
    res.remaining_volume_mm3 = std::max(0.0, stock_vol - removed);
    res.rapid_collisions = rapid_hits;
    if (rapid_hits > 0) res.warnings.push_back("rapid move(s) pass through remaining stock");

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
           {"warnings", warnings}};
    if (include_field && remaining.nx() > 0) {
        json zs = json::array();
        for (int jj = 0; jj < remaining.ny(); ++jj)
            for (int ii = 0; ii < remaining.nx(); ++ii) zs.push_back(remaining.raw(ii, jj));
        j["field"] = json{{"nx", remaining.nx()}, {"ny", remaining.ny()}, {"res", remaining.res()},
                          {"lo", json::array({remaining.lo().x, remaining.lo().y})}, {"z", zs}};
    }
    return j;
}

}  // namespace cam
}  // namespace aicad
