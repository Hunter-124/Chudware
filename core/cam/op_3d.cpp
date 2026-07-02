// core/cam/op_3d.cpp — 3D milling generators: parallel raster, Z-level
// contour, waterline pocket roughing, adaptive clearing, scallop, pencil,
// radial, and spiral finish.  All are built on the HeightField / waterline
// substrate from heightfield.{h,cpp}; OCCT-free.
#include <algorithm>
#include <cmath>
#include <vector>

#include "cam/heightfield.h"
#include "cam/op_common.h"

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

namespace aicad {
namespace cam {

namespace {

// ── shared local helpers ──────────────────────────────────────────────────────

PathBuilder make_builder(const Operation& op) {
    return PathBuilder(op.heights, op.feed, op.plunge, 5000.0);
}

// Choose a heightfield resolution appropriate for the op quality requirements.
double hf_res_for(double stepover, double tol) {
    double r = std::min(stepover * 0.25, tol * 4.0);
    return std::max(0.1, std::min(r, 1.0));
}

// Build a rectangular Region from the mesh XY bounding box.
Region mesh_bbox_region(const TriMesh& mesh) {
    if (mesh.verts.empty()) return {};
    double xmin = mesh.verts[0].x, xmax = xmin;
    double ymin = mesh.verts[0].y, ymax = ymin;
    for (auto& v : mesh.verts) {
        if (v.x < xmin) xmin = v.x;  if (v.x > xmax) xmax = v.x;
        if (v.y < ymin) ymin = v.y;  if (v.y > ymax) ymax = v.y;
    }
    Region r;
    r.outer = {{xmin, ymin}, {xmax, ymin}, {xmax, ymax}, {xmin, ymax}};
    return r;
}

// Distance squared between two heightfield corners and a centre point — used
// to find the maximum radius for radial / spiral ops.
double max_corner_dist(const HeightField& hf, const Vec2& cen) {
    Vec2 lo = hf.lo(), hi = hf.hi();
    auto d2 = [&](double cx, double cy) {
        double dx = cx - cen.x, dy = cy - cen.y;
        return dx * dx + dy * dy;
    };
    return std::sqrt(std::max({d2(lo.x, lo.y), d2(hi.x, lo.y),
                               d2(hi.x, hi.y), d2(lo.x, hi.y)}));
}

// Feed from path[0] through path[n-1], resampling each segment at sample_step
// intervals and querying the heightfield for Z at each sample.
// The caller is responsible for rapid+plunge positioning to path[0] beforehand.
// When close_loop is true, an extra segment back to path[0] is appended.
void drape_path(PathBuilder& pb, const Path2& path,
                const HeightField& hf,
                double tool_r, double corner_r,
                double sample_step, bool close_loop = false) {
    if (path.empty()) return;
    Vec2 prev = path[0];
    size_t n = path.size();
    size_t limit = close_loop ? n + 1 : n;
    for (size_t si = 1; si < limit; ++si) {
        Vec2 cur = (si < n) ? path[si] : path[0];
        double seg = std::hypot(cur.x - prev.x, cur.y - prev.y);
        int steps = std::max(1, (int)std::ceil(seg / sample_step));
        for (int k = 1; k <= steps; ++k) {
            double t = (double)k / steps;
            double x = prev.x + t * (cur.x - prev.x);
            double y = prev.y + t * (cur.y - prev.y);
            double z = hf.drop_tool(x, y, tool_r, corner_r);
            pb.feed_to({x, y, z});
        }
        prev = cur;
    }
}

// Rapid to clearance, position XY over the first path point, plunge to the
// drop-tool Z, drape the path, and retract.  close_loop closes back to path[0].
void cut_draped(PathBuilder& pb, const Path2& path,
                const HeightField& hf,
                double tool_r, double corner_r,
                double sample_step, bool close_loop = false) {
    if (path.empty()) return;
    pb.rapid_to_clearance();
    pb.rapid_xy(path.front());
    pb.plunge_to(hf.drop_tool(path.front().x, path.front().y, tool_r, corner_r));
    drape_path(pb, path, hf, tool_r, corner_r, sample_step, close_loop);
    pb.rapid_to_clearance();
}

// ── pocket entry helper (helix or plunge) ────────────────────────────────────
void pocket_entry(PathBuilder& pb, const Operation& op, const Region& reg,
                  double tool_r, double stepover, double z) {
    Vec2 entry = region_inside_point(reg);
    double helix_r = std::max(0.5, std::min(tool_r * 0.9, stepover * 0.5));
    double ramp_f = op.ramp_feed > 0.0 ? op.ramp_feed : op.plunge;
    double angle  = std::max(0.5, op.ramp.angle);
    double pitch  = std::max(0.2, helix_r * std::tan(angle * M_PI / 180.0) * 2.0);
    if (op.ramp.type == RampType::Helix) {
        pb.helix_entry(entry, helix_r, op.heights.top, z, ramp_f, pitch);
    } else {
        pb.rapid_to_clearance();
        pb.rapid_xy(entry);
        pb.plunge_to(z);
    }
}

// ── waterline pocket clearing for one region at one Z level ──────────────────
// Cuts inner->outer concentric offset rings. Shared by gen_pocket3d (plain
// waterline roughing) and gen_adaptive_clear3d (engagement-limited adaptive
// clearing). With op.arc_blend_linking (default true), ring-to-ring transitions
// use the same tangent arc-blend link as gen_adaptive2d (rotate the next ring
// to start nearest the tool, then a semicircular arc) instead of a direct
// feed_to, bounding tool engagement at corners; with it off, linking is plain
// concentric-offset (direct feed_to). Returns the largest ring-transition XY
// link distance seen (0 if there were <=1 rings), so an adaptive caller can run
// the same engagement-exceeded check as gen_adaptive2d.
double clear_waterline_region(PathBuilder& pb, const Operation& op,
                              const Path2& loop, double tool_r,
                              double stepover, double z) {
    if (loop.size() < 3) return 0.0;
    Region reg;
    reg.outer = geo::ensure_ccw(loop);
    auto pocket_lv = geo::pocket_offsets(reg, tool_r, stepover, op.stock_to_leave);
    if (pocket_lv.empty()) return 0.0;

    pocket_entry(pb, op, reg, tool_r, stepover, z);
    Vec2 centroid = region_inside_point(reg);

    bool climb = op.direction == CutDirection::Climb;
    double max_link = 0.0;
    bool have_prev = false;
    Vec2 prev{0, 0};
    // Cut from innermost to outermost (climb from inside out).
    for (auto it = pocket_lv.rbegin(); it != pocket_lv.rend(); ++it) {
        for (const Path2& lp : *it) {
            if (lp.size() < 3) continue;
            Path2 oriented = climb ? geo::ensure_ccw(lp) : geo::ensure_cw(lp);
            if (have_prev && op.arc_blend_linking) oriented = rotate_ring_to_nearest(oriented, prev);
            if (have_prev) {
                double link_dist = link_ring(pb, prev, oriented.front(), z, op.arc_blend_linking, centroid);
                max_link = std::max(max_link, link_dist);
            } else {
                pb.feed_to(oriented.front(), z);
            }
            for (size_t k = 1; k <= oriented.size(); ++k)
                pb.feed_to(oriented[k % oriented.size()], z);
            prev = oriented.front();
            have_prev = true;
        }
    }
    pb.rapid_to_clearance();
    return max_link;
}

}  // namespace

// ─────────────────────────────────────── parallel (raster) 3D finish ──
GenResult gen_parallel3d(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);

    TriMesh mesh = op_mesh(op);
    if (mesh.verts.empty()) {
        res.ok = false; res.error = "parallel3d: no mesh"; return res;
    }

    double tool_r   = ctx.tool.effective_radius();
    double corner_r = ctx.tool.corner_radius;
    double stepover = op.stepover > 0.0 ? op.stepover : tool_r * 0.7;
    double tol      = op.tolerance > 0.0 ? op.tolerance : 0.01;
    double hfres    = hf_res_for(stepover, tol);
    double samp     = std::max(hfres, tol);

    HeightField hf = HeightField::from_mesh(mesh, hfres, tool_r, op.heights.bottom);

    std::vector<Region> regs = op_regions(op);
    if (regs.empty()) regs.push_back(mesh_bbox_region(mesh));

    auto strokes = geo::raster_fill(regs, stepover, op.angle,
                                    op.pattern != CutPattern::OneWay);
    if (strokes.empty()) {
        res.ok = false; res.error = "parallel3d: no raster strokes"; return res;
    }

    PathBuilder pb = make_builder(op);
    pb.set_spindle((int)std::lround(op.rpm));

    Vec2 last_pt = strokes.front().front();
    bool first = true;
    for (const Path2& st : strokes) {
        if (st.empty()) continue;
        Vec2 start = st.front();
        double gap = std::hypot(start.x - last_pt.x, start.y - last_pt.y);
        if (first || gap > stepover * 2.5) {
            // Full retract + reposition between non-adjacent strokes.
            pb.rapid_to_clearance();
            pb.rapid_xy(start);
            pb.plunge_to(hf.drop_tool(start.x, start.y, tool_r, corner_r));
            first = false;
        } else {
            // Zigzag link: 3D feed at surface height across the stepover gap.
            double zl = hf.drop_tool(start.x, start.y, tool_r, corner_r);
            pb.feed_to({start.x, start.y, zl});
        }
        drape_path(pb, st, hf, tool_r, corner_r, samp);
        last_pt = st.back();
    }
    pb.rapid_to_clearance();

    res.path = pb.take(ctx.tool.number, ctx.machine);
    res.path.op_id = op.id;
    return res;
}

// ──────────────────────────────── Z-level (waterline) contour finish ──
GenResult gen_contour3d(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);

    TriMesh mesh = op_mesh(op);
    if (mesh.verts.empty()) {
        res.ok = false; res.error = "contour3d: no mesh"; return res;
    }

    double tool_r   = ctx.tool.effective_radius();
    double stepdown = op.stepdown > 0.0 ? op.stepdown
                                        : std::fabs(op.heights.top - op.heights.bottom);
    if (stepdown <= 0.0) stepdown = 1.0;

    auto levels   = depth_levels(op.heights.top, op.heights.bottom, stepdown,
                                 op.stock_to_leave_axial);
    auto sections = waterline_sections(mesh, levels, op.tolerance);

    PathBuilder pb = make_builder(op);
    pb.set_spindle((int)std::lround(op.rpm));

    bool climb = op.direction == CutDirection::Climb;
    for (size_t li = 0; li < levels.size(); ++li) {
        double z = levels[li];
        for (const Path2& loop : sections[li]) {
            if (loop.size() < 3) continue;
            // Offset the waterline boundary outward by tool_r so the tool edge
            // just grazes the material silhouette at this Z level.
            Region reg;
            reg.outer = geo::ensure_ccw(loop);
            auto offs = geo::offset_region(reg, tool_r + op.stock_to_leave);
            for (auto& o : offs) {
                if (o.outer.size() < 3) continue;
                pb.cut_loop(o.outer, z,
                            op.lead_in, op.lead_out,
                            op.comp_side, climb,
                            0, 0.0, 0.0, op.heights.top);
            }
        }
    }

    res.path = pb.take(ctx.tool.number, ctx.machine);
    res.path.op_id = op.id;
    return res;
}

// ──────────────────────────────────── waterline pocket roughing ──
GenResult gen_pocket3d(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);

    TriMesh mesh = op_mesh(op);
    if (mesh.verts.empty()) {
        res.ok = false; res.error = "pocket3d: no mesh"; return res;
    }

    double tool_r   = ctx.tool.effective_radius();
    double stepover = op.stepover > 0.0 ? op.stepover : tool_r * 0.8;
    double stepdown = op.stepdown > 0.0 ? op.stepdown : tool_r;
    if (stepdown <= 0.0) stepdown = 1.0;

    auto levels   = depth_levels(op.heights.top, op.heights.bottom, stepdown,
                                 op.stock_to_leave_axial);
    auto sections = waterline_sections(mesh, levels, op.tolerance);

    PathBuilder pb = make_builder(op);
    pb.set_spindle((int)std::lround(op.rpm));

    for (size_t li = 0; li < levels.size(); ++li) {
        double z = levels[li];
        for (const Path2& loop : sections[li]) {
            clear_waterline_region(pb, op, loop, tool_r, stepover, z);
        }
    }

    res.path = pb.take(ctx.tool.number, ctx.machine);
    res.path.op_id = op.id;
    return res;
}

// ─────────────────────────────── adaptive-clearing (engagement-limited) ──
GenResult gen_adaptive_clear3d(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);

    TriMesh mesh = op_mesh(op);
    if (mesh.verts.empty()) {
        res.ok = false; res.error = "adaptive_clear3d: no mesh"; return res;
    }

    double tool_r   = ctx.tool.effective_radius();
    // Engagement-limited: use a small fraction of the tool radius for stepover
    // (e.g. 30 % vs. the 80 % default of gen_pocket3d).
    double stepover = op.stepover > 0.0 ? op.stepover : tool_r * 0.3;
    double stepdown = op.stepdown > 0.0 ? op.stepdown : tool_r * 0.5;
    if (stepdown <= 0.0) stepdown = 0.5;

    auto levels   = depth_levels(op.heights.top, op.heights.bottom, stepdown,
                                 op.stock_to_leave_axial);
    auto sections = waterline_sections(mesh, levels, op.tolerance);

    PathBuilder pb = make_builder(op);
    pb.set_spindle((int)std::lround(op.rpm));

    // Force helical entry for every level (adaptive entry strategy).
    Operation op_helix = op;
    op_helix.ramp.type = RampType::Helix;

    // Largest ring-to-ring link distance seen across the whole op (see
    // clear_waterline_region / op_common.h link_ring); mirrors gen_adaptive2d's
    // engagement-exceeded check since this is the true "adaptive" 3D path (as
    // opposed to gen_pocket3d's plain waterline roughing, which shares the same
    // clearing routine but is not held to the adaptive engagement bound).
    double max_link_dist = 0.0;
    for (size_t li = 0; li < levels.size(); ++li) {
        double z = levels[li];
        for (const Path2& loop : sections[li]) {
            max_link_dist = std::max(max_link_dist,
                                     clear_waterline_region(pb, op_helix, loop, tool_r, stepover, z));
        }
    }
    if (max_link_dist > stepover * 1.5) {
        res.warnings.push_back("adaptive_clear3d: ring-transition engagement exceeds 1.5x stepover — "
                               "arc-blend linking recommended");
    }

    res.path = pb.take(ctx.tool.number, ctx.machine);
    res.path.op_id = op.id;
    return res;
}

// ──────────────────────────────────── constant-stepover scallop finish ──
GenResult gen_scallop3d(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);

    TriMesh mesh = op_mesh(op);
    if (mesh.verts.empty()) {
        res.ok = false; res.error = "scallop3d: no mesh"; return res;
    }

    double tool_r   = ctx.tool.effective_radius();
    double corner_r = ctx.tool.corner_radius;
    double stepover = op.stepover > 0.0 ? op.stepover : tool_r * 0.5;
    double tol      = op.tolerance > 0.0 ? op.tolerance : 0.01;
    double hfres    = hf_res_for(stepover, tol);
    double samp     = std::max(hfres, tol);

    HeightField hf = HeightField::from_mesh(mesh, hfres, tool_r, op.heights.bottom);

    std::vector<Region> regs = op_regions(op);
    if (regs.empty()) regs.push_back(mesh_bbox_region(mesh));

    PathBuilder pb = make_builder(op);
    pb.set_spindle((int)std::lround(op.rpm));

    // Offset the region boundary inward by stepover repeatedly until it
    // collapses, draping each closed loop onto the heightfield.
    std::vector<Region> current = regs;
    while (!current.empty()) {
        for (auto& reg : current) {
            if (reg.outer.size() < 3) continue;
            // Resample to samp density so the draped output is smooth.
            Path2 loop = geo::resample_max_seg(geo::ensure_ccw(reg.outer), samp);
            if (loop.size() < 3) continue;
            cut_draped(pb, loop, hf, tool_r, corner_r, samp, /*close_loop=*/true);
        }
        current = geo::offset_regions(current, -stepover);
    }

    res.path = pb.take(ctx.tool.number, ctx.machine);
    res.path.op_id = op.id;
    return res;
}

// ─────────────────────────────────────── pencil (corner/edge) pass ──
//
// Strategy: scan the heightfield for nodes where the slope is high (≥ ~30°).
// Those nodes identify steep walls and silhouette edges — exactly the
// surfaces the pencil pass is meant to clean up.  Emit scanline-ordered
// 3D feed segments draped at the drop-tool height.
GenResult gen_pencil3d(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);

    TriMesh mesh = op_mesh(op);
    if (mesh.verts.empty()) {
        res.ok = false; res.error = "pencil3d: no mesh"; return res;
    }

    double tool_r   = ctx.tool.effective_radius();
    double corner_r = ctx.tool.corner_radius;
    double tol      = op.tolerance > 0.0 ? op.tolerance : 0.01;
    double hfres    = std::max(0.1, std::min(tool_r * 0.4, tol * 4.0));

    HeightField hf = HeightField::from_mesh(mesh, hfres, tool_r, op.heights.bottom);

    PathBuilder pb = make_builder(op);
    pb.set_spindle((int)std::lround(op.rpm));

    double floor_z = hf.floor();
    // Slope threshold ~30° (tangent ≈ 0.58) — catches both box vertical edges
    // and dome perimeter regions where the pencil tool contacts two faces.
    const double slope_thr = 0.5;

    for (int j = 1; j < hf.ny() - 1; ++j) {
        bool in_pass = false;
        for (int i = 1; i < hf.nx() - 1; ++i) {
            double h00 = hf.raw(i,     j    );
            double hp  = hf.raw(i + 1, j    );
            double hm  = hf.raw(i - 1, j    );
            double hup = hf.raw(i,     j + 1);
            double hdn = hf.raw(i,     j - 1);

            bool on_surf = h00 > floor_z + 1e-6;
            // Central-difference slope magnitude in X and Y.
            double sx = std::fabs(hp - hm) / (2.0 * hfres);
            double sy = std::fabs(hup - hdn) / (2.0 * hfres);
            bool steep = on_surf && (sx > slope_thr || sy > slope_thr);

            if (steep) {
                double x = hf.lo().x + i * hfres;
                double y = hf.lo().y + j * hfres;
                double z = hf.drop_tool(x, y, tool_r, corner_r);
                if (!in_pass) {
                    pb.rapid_to_clearance();
                    pb.rapid_xy({x, y});
                    pb.plunge_to(z);
                    in_pass = true;
                } else {
                    pb.feed_to({x, y, z});
                }
            } else if (in_pass) {
                pb.rapid_to_clearance();
                in_pass = false;
            }
        }
        if (in_pass) pb.rapid_to_clearance();
    }

    res.path = pb.take(ctx.tool.number, ctx.machine);
    res.path.op_id = op.id;
    return res;
}

// ──────────────────────────── radial finish (lines from region centroid) ──
GenResult gen_radial3d(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);

    TriMesh mesh = op_mesh(op);
    if (mesh.verts.empty()) {
        res.ok = false; res.error = "radial3d: no mesh"; return res;
    }

    double tool_r   = ctx.tool.effective_radius();
    double corner_r = ctx.tool.corner_radius;
    double stepover = op.stepover > 0.0 ? op.stepover : tool_r * 0.5;
    double tol      = op.tolerance > 0.0 ? op.tolerance : 0.01;
    double hfres    = hf_res_for(stepover, tol);
    double samp     = std::max(hfres, tol);

    HeightField hf = HeightField::from_mesh(mesh, hfres, tool_r, op.heights.bottom);

    std::vector<Region> regs = op_regions(op);
    Region region = regs.empty() ? mesh_bbox_region(mesh) : regs[0];
    Vec2 center = region_centroid(region);

    double max_r = max_corner_dist(hf, center);
    if (max_r < 1e-6) {
        res.ok = false; res.error = "radial3d: degenerate region"; return res;
    }

    // Angular step: arc length at the mean radius equals one stepover.
    double mean_r = max_r * 0.5;
    double da = (mean_r > 1e-6) ? stepover / mean_r : M_PI / 8.0;
    da = std::min(da, M_PI / 4.0);  // at most 8 spokes per quadrant

    PathBuilder pb = make_builder(op);
    pb.set_spindle((int)std::lround(op.rpm));

    Vec2 lo = hf.lo(), hi = hf.hi();
    int steps_per_line = std::max(1, (int)std::ceil(max_r / samp));

    for (double theta = 0.0; theta < 2.0 * M_PI; theta += da) {
        double dx = std::cos(theta), dy = std::sin(theta);
        bool in_cut = false;
        for (int k = 0; k <= steps_per_line; ++k) {
            double rk = k * samp;
            double x  = center.x + rk * dx;
            double y  = center.y + rk * dy;
            // Skip nodes outside the heightfield bounds.
            if (x < lo.x || x > hi.x || y < lo.y || y > hi.y) {
                if (in_cut) { pb.rapid_to_clearance(); in_cut = false; }
                continue;
            }
            double z = hf.drop_tool(x, y, tool_r, corner_r);
            // Skip nodes that landed at floor (no material).
            if (z <= hf.floor() + 1e-6 && k > 0) {
                if (in_cut) { pb.rapid_to_clearance(); in_cut = false; }
                continue;
            }
            if (!in_cut) {
                pb.rapid_to_clearance();
                pb.rapid_xy({x, y});
                pb.plunge_to(z);
                in_cut = true;
            } else {
                pb.feed_to({x, y, z});
            }
        }
        if (in_cut) pb.rapid_to_clearance();
    }

    res.path = pb.take(ctx.tool.number, ctx.machine);
    res.path.op_id = op.id;
    return res;
}

// ──────────────────────────────── Archimedean spiral finish ──
//
// r(θ) = stepover · θ / (2π), so radial advance equals stepover per full turn.
// Arc-length step ≈ samp: dθ = samp / max(r, samp).
GenResult gen_spiral_finish3d(const Operation& opIn, const OpContext& ctx) {
    GenResult res;
    Operation op = resolved_op(opIn, ctx);

    TriMesh mesh = op_mesh(op);
    if (mesh.verts.empty()) {
        res.ok = false; res.error = "spiral_finish3d: no mesh"; return res;
    }

    double tool_r   = ctx.tool.effective_radius();
    double corner_r = ctx.tool.corner_radius;
    double stepover = op.stepover > 0.0 ? op.stepover : tool_r * 0.5;
    double tol      = op.tolerance > 0.0 ? op.tolerance : 0.01;
    double hfres    = hf_res_for(stepover, tol);
    double samp     = std::max(hfres, tol);

    HeightField hf = HeightField::from_mesh(mesh, hfres, tool_r, op.heights.bottom);

    std::vector<Region> regs = op_regions(op);
    Region region = regs.empty() ? mesh_bbox_region(mesh) : regs[0];
    Vec2 center = region_centroid(region);

    double max_r = max_corner_dist(hf, center);
    if (max_r < 1e-6) {
        res.ok = false; res.error = "spiral_finish3d: degenerate region"; return res;
    }

    PathBuilder pb = make_builder(op);
    pb.set_spindle((int)std::lround(op.rpm));

    // Begin at the centre.
    double z0 = hf.drop_tool(center.x, center.y, tool_r, corner_r);
    pb.rapid_to_clearance();
    pb.rapid_xy(center);
    pb.plunge_to(z0);

    Vec2 lo = hf.lo(), hi = hf.hi();
    double theta    = 0.0;
    double spiral_r = 0.0;
    while (spiral_r < max_r) {
        // Variable angular step so arc length ≈ samp regardless of radius.
        double dt = samp / std::max(spiral_r, samp);
        theta    += dt;
        spiral_r  = stepover * theta / (2.0 * M_PI);
        double x  = center.x + spiral_r * std::cos(theta);
        double y  = center.y + spiral_r * std::sin(theta);
        // Points outside the heightfield are silently skipped (no gouge risk).
        if (x < lo.x || x > hi.x || y < lo.y || y > hi.y) continue;
        double z = hf.drop_tool(x, y, tool_r, corner_r);
        pb.feed_to({x, y, z});
    }
    pb.rapid_to_clearance();

    res.path = pb.take(ctx.tool.number, ctx.machine);
    res.path.op_id = op.id;
    return res;
}

}  // namespace cam
}  // namespace aicad
