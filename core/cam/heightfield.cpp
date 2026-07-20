// core/cam/heightfield.cpp — HeightField construction (rasterized from a
// triangle mesh), bilinear sampling, 3-axis drop-cutter contact, and Z-level
// (waterline) cross-section extraction.  OCCT-free.
#include <algorithm>
#include <cmath>
#include <vector>

#include "cam/heightfield.h"

namespace aicad {
namespace cam {

namespace {

// ── barycentric test in XY ────────────────────────────────────────────────────
// Fills (u,v,w) for point p inside triangle (a,b,c) in the XY plane.
// Returns false for degenerate (zero-area) triangles.
bool bary2d(double px, double py,
            double ax, double ay,
            double bx, double by,
            double cx, double cy,
            double& u, double& v, double& w) noexcept {
    double denom = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy);
    if (std::fabs(denom) < 1e-14) return false;
    u = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / denom;
    v = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / denom;
    w = 1.0 - u - v;
    return true;
}

// ── waterline helpers ─────────────────────────────────────────────────────────
struct Seg2 { Vec2 a, b; };

// Intersect all triangles with the plane z = cut_z (strict crossings only so
// horizontal triangles and shared-vertex cases are skipped cleanly).
std::vector<Seg2> slice_at_z(const TriMesh& mesh, double cut_z) {
    std::vector<Seg2> out;
    out.reserve(mesh.tris.size() / 8);
    for (auto& tri : mesh.tris) {
        const Vec3& va = mesh.verts[tri[0]];
        const Vec3& vb = mesh.verts[tri[1]];
        const Vec3& vc = mesh.verts[tri[2]];
        const Vec3* vv[3] = {&va, &vb, &vc};
        double zz[3] = {va.z - cut_z, vb.z - cut_z, vc.z - cut_z};
        Vec2 pts[2];
        int np = 0;
        for (int e = 0; e < 3 && np < 2; ++e) {
            int i = e, j = (e + 1) % 3;
            if (zz[i] * zz[j] < 0.0) {   // strict crossing → one intersection
                double t = zz[i] / (zz[i] - zz[j]);
                pts[np++] = {vv[i]->x + t * (vv[j]->x - vv[i]->x),
                             vv[i]->y + t * (vv[j]->y - vv[i]->y)};
            }
        }
        if (np == 2) {
            double d2 = (pts[1].x - pts[0].x) * (pts[1].x - pts[0].x) +
                        (pts[1].y - pts[0].y) * (pts[1].y - pts[0].y);
            if (d2 > 1e-18) out.push_back({pts[0], pts[1]});
        }
    }
    return out;
}

// Chain unordered segments into closed contours using O(n²) endpoint matching.
// Adequate for typical cross-sections (< 20 k segments per level).
std::vector<Path2> chain_segments(std::vector<Seg2> segs, double tol) {
    if (segs.empty()) return {};
    double tol2 = tol * tol;
    std::vector<bool> used(segs.size(), false);
    std::vector<Path2> loops;

    auto d2 = [](const Vec2& p, const Vec2& q) noexcept -> double {
        double dx = p.x - q.x, dy = p.y - q.y;
        return dx * dx + dy * dy;
    };

    for (size_t si = 0; si < segs.size(); ++si) {
        if (used[si]) continue;
        used[si] = true;
        Path2 chain;
        chain.push_back(segs[si].a);
        chain.push_back(segs[si].b);
        bool closed = false;
        while (!closed) {
            const Vec2& tail = chain.back();
            bool found = false;
            for (size_t k = 0; k < segs.size(); ++k) {
                if (used[k]) continue;
                double da = d2(tail, segs[k].a);
                double db = d2(tail, segs[k].b);
                double dmin = std::min(da, db);
                if (dmin > tol2) continue;
                used[k] = true;
                // The closer endpoint is the connection; the other is the extension.
                Vec2 next = (da < db) ? segs[k].b : segs[k].a;
                if (d2(next, chain.front()) <= tol2) {
                    closed = true;
                } else {
                    chain.push_back(next);
                    found = true;
                }
                break;
            }
            if (!found) break;
        }
        if (chain.size() >= 3) loops.push_back(std::move(chain));
    }
    return loops;
}

}  // namespace

// ── HeightField ───────────────────────────────────────────────────────────────

HeightField HeightField::from_mesh(const TriMesh& mesh, double res, double pad,
                                   double floor_z) {
    HeightField hf;
    if (mesh.verts.empty() || res <= 0.0) return hf;

    // Compute XY bounding box and expand by pad.
    double xmin = mesh.verts[0].x, xmax = xmin;
    double ymin = mesh.verts[0].y, ymax = ymin;
    for (auto& v : mesh.verts) {
        if (v.x < xmin) xmin = v.x;  if (v.x > xmax) xmax = v.x;
        if (v.y < ymin) ymin = v.y;  if (v.y > ymax) ymax = v.y;
    }
    xmin -= pad;  xmax += pad;
    ymin -= pad;  ymax += pad;

    hf.lo_    = {xmin, ymin};
    hf.hi_    = {xmax, ymax};
    hf.res_   = res;
    hf.floor_ = floor_z;
    hf.nx_    = std::max(2, (int)std::ceil((xmax - xmin) / res) + 1);
    hf.ny_    = std::max(2, (int)std::ceil((ymax - ymin) / res) + 1);
    hf.z_.assign((size_t)hf.nx_ * hf.ny_, (float)floor_z);

    // Rasterize each triangle: for each grid node in the triangle's XY bbox,
    // test barycentric containment and keep the maximum Z found at that node.
    for (auto& tri : mesh.tris) {
        const Vec3& va = mesh.verts[tri[0]];
        const Vec3& vb = mesh.verts[tri[1]];
        const Vec3& vc = mesh.verts[tri[2]];

        double txmin = std::min({va.x, vb.x, vc.x});
        double txmax = std::max({va.x, vb.x, vc.x});
        double tymin = std::min({va.y, vb.y, vc.y});
        double tymax = std::max({va.y, vb.y, vc.y});

        int i0 = std::max(0, (int)std::floor((txmin - xmin) / res));
        int i1 = std::min(hf.nx_ - 1, (int)std::ceil((txmax - xmin) / res));
        int j0 = std::max(0, (int)std::floor((tymin - ymin) / res));
        int j1 = std::min(hf.ny_ - 1, (int)std::ceil((tymax - ymin) / res));

        for (int j = j0; j <= j1; ++j) {
            double py = ymin + j * res;
            for (int i = i0; i <= i1; ++i) {
                double px = xmin + i * res;
                double u, v, w;
                if (!bary2d(px, py,
                            va.x, va.y, vb.x, vb.y, vc.x, vc.y,
                            u, v, w)) continue;
                if (u < -1e-9 || v < -1e-9 || w < -1e-9) continue;
                float hz = (float)(u * va.z + v * vb.z + w * vc.z);
                size_t idx = (size_t)j * hf.nx_ + i;
                if (hz > hf.z_[idx]) hf.z_[idx] = hz;
            }
        }
    }
    return hf;
}

HeightField HeightField::empty(int nx, int ny, double res, Vec2 lo, double floor_z) {
    HeightField hf;
    if (nx < 1 || ny < 1 || res <= 0.0) return hf;
    hf.nx_ = nx;
    hf.ny_ = ny;
    hf.res_ = res;
    hf.lo_ = lo;
    hf.hi_ = {lo.x + (nx - 1) * res, lo.y + (ny - 1) * res};
    hf.floor_ = floor_z;
    hf.z_.assign((size_t)nx * (size_t)ny, (float)floor_z);
    return hf;
}


double HeightField::height_at(double x, double y) const {
    if (nx_ < 2 || ny_ < 2) return floor_;
    double fi = (x - lo_.x) / res_;
    double fj = (y - lo_.y) / res_;
    if (fi < 0.0 || fj < 0.0 || fi >= nx_ - 1 || fj >= ny_ - 1) return floor_;
    int i0 = (int)fi, j0 = (int)fj;
    double tx = fi - i0, ty = fj - j0;
    double z00 = z_[(size_t)j0       * nx_ + i0    ];
    double z10 = z_[(size_t)j0       * nx_ + (i0+1)];
    double z01 = z_[(size_t)(j0 + 1) * nx_ + i0    ];
    double z11 = z_[(size_t)(j0 + 1) * nx_ + (i0+1)];
    return z00*(1-tx)*(1-ty) + z10*tx*(1-ty)
         + z01*(1-tx)*ty     + z11*tx*ty;
}

double HeightField::raw(int i, int j) const {
    i = std::max(0, std::min(nx_ - 1, i));
    j = std::max(0, std::min(ny_ - 1, j));
    return z_[(size_t)j * nx_ + i];
}

void HeightField::set_raw(int i, int j, double z) {
    i = std::max(0, std::min(nx_ - 1, i));
    j = std::max(0, std::min(ny_ - 1, j));
    z_[(size_t)j * nx_ + i] = (float)z;
}

// 3-axis drop-cutter contact.
//
// For each grid node within the tool's XY footprint the required tip-Z
// (minimum Z that avoids gouging that node) is:
//
//   Flat zone (d ≤ flat_r):   z_req = h              (flat bottom contacts)
//   Corner zone (d > flat_r): z_req = h − r_c + √(r_c² − (d − flat_r)²)
//
// where flat_r = radius − corner_r.  Return the maximum over all nodes.
// corner_r == 0  → flat endmill   (entire disk is flat zone)
// corner_r == radius → ball endmill (no flat zone; pure corner zone)
double HeightField::drop_tool(double x, double y, double radius, double corner_r,
                              double /*sample_step*/) const {
    if (nx_ < 1 || ny_ < 1) return floor_;
    if (corner_r < 0.0) corner_r = 0.0;
    if (corner_r > radius) corner_r = radius;
    double flat_r = radius - corner_r;  // radius of flat disk (0 for ball)

    double tip_z = floor_;

    int i0 = std::max(0, (int)std::floor((x - radius - lo_.x) / res_));
    int i1 = std::min(nx_ - 1, (int)std::ceil ((x + radius - lo_.x) / res_));
    int j0 = std::max(0, (int)std::floor((y - radius - lo_.y) / res_));
    int j1 = std::min(ny_ - 1, (int)std::ceil ((y + radius - lo_.y) / res_));

    for (int j = j0; j <= j1; ++j) {
        double sy = lo_.y + j * res_;
        double dy = sy - y;
        for (int i = i0; i <= i1; ++i) {
            double sx = lo_.x + i * res_;
            double dx = sx - x;
            double d  = std::sqrt(dx * dx + dy * dy);
            if (d > radius + 1e-9) continue;

            double h = z_[(size_t)j * nx_ + i];
            double z_req;
            if (d <= flat_r) {
                z_req = h;               // flat zone: tip sits on the surface
            } else {
                double d2 = d - flat_r;  // radial distance from the torus ring
                double inside = corner_r * corner_r - d2 * d2;
                if (inside < 0.0) continue;
                z_req = h - corner_r + std::sqrt(inside);
            }
            if (z_req > tip_z) tip_z = z_req;
        }
    }
    return tip_z;
}

TriMesh heightfield_to_mesh(const HeightField& field) {
    TriMesh m;
    const int nx = field.nx(), ny = field.ny();
    if (nx < 2 || ny < 2) return m;
    const double res = field.res();
    const Vec2 lo = field.lo();
    m.verts.resize(static_cast<size_t>(nx) * static_cast<size_t>(ny));
    for (int j = 0; j < ny; ++j) {
        for (int i = 0; i < nx; ++i) {
            const size_t idx = static_cast<size_t>(j) * static_cast<size_t>(nx) + static_cast<size_t>(i);
            m.verts[idx] = {lo.x + i * res, lo.y + j * res, field.raw(i, j)};
        }
    }
    m.tris.reserve(static_cast<size_t>(nx - 1) * static_cast<size_t>(ny - 1) * 2);
    for (int j = 0; j < ny - 1; ++j) {
        for (int i = 0; i < nx - 1; ++i) {
            const int i00 = j * nx + i;
            const int i10 = i00 + 1;
            const int i01 = i00 + nx;
            const int i11 = i01 + 1;
            m.tris.push_back({i00, i10, i11});
            m.tris.push_back({i00, i11, i01});
        }
    }
    return m;
}

// ── waterline sections ────────────────────────────────────────────────────────

std::vector<std::vector<Path2>> waterline_sections(const TriMesh& mesh,
                                                   const std::vector<double>& z_levels,
                                                   double tolerance) {
    double tol = (tolerance > 0.0) ? tolerance : 0.01;
    std::vector<std::vector<Path2>> result;
    result.reserve(z_levels.size());
    for (double z : z_levels) {
        auto segs = slice_at_z(mesh, z);
        result.push_back(chain_segments(std::move(segs), tol));
    }
    return result;
}

}  // namespace cam
}  // namespace aicad
