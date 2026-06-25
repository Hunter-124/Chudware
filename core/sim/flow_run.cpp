// core/sim/flow_run.cpp — see flow_run.h. Voxelize triangle soup -> wind-tunnel LBM.
// Domain layout + lattice-param derivation mirror engine/sim/lbm.py::run_wind_tunnel; the
// trimesh voxelize+fill is replaced by a self-contained X-scanline parity fill (per body).
#include "sim/flow_run.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <functional>
#include <limits>
#include <string>

namespace aicad {
namespace sim {

namespace {

constexpr double kCs2 = 1.0 / 3.0;  // lattice speed of sound squared

struct V3 { double x, y, z; };

// Barycentric inside-test in the YZ plane; on success interpolate the triangle's X at (py,pz).
inline bool tri_x_at(double py, double pz, const V3& A, const V3& B, const V3& C, double& xout) {
    double v0y = B.y - A.y, v0z = B.z - A.z;
    double v1y = C.y - A.y, v1z = C.z - A.z;
    double v2y = py - A.y, v2z = pz - A.z;
    double d00 = v0y * v0y + v0z * v0z;
    double d01 = v0y * v1y + v0z * v1z;
    double d11 = v1y * v1y + v1z * v1z;
    double d20 = v2y * v0y + v2z * v0z;
    double d21 = v2y * v1y + v2z * v1z;
    double denom = d00 * d11 - d01 * d01;
    if (std::abs(denom) < 1e-12) return false;  // triangle edge-on to X (no YZ area)
    double v = (d11 * d20 - d01 * d21) / denom;
    double w = (d00 * d21 - d01 * d20) / denom;
    double u = 1.0 - v - w;
    const double eps = -1e-9;
    if (u < eps || v < eps || w < eps) return false;
    xout = u * A.x + v * B.x + w * C.x;
    return true;
}

// X-scanline parity fill one body's triangles into `mask` (OR), domain grid (Nx,Ny,Nz), cells of
// `voxel` mm with origin at 0. pos = flat 3*nverts, idx = flat 3*ntris.
void voxelize_body(const std::vector<float>& pos, const std::vector<uint32_t>& idx,
                   std::vector<uint8_t>& mask, int Nx, int Ny, int Nz, double voxel) {
    // crossings[(iy*Nz)+iz] = sorted x-intersections for that column.
    std::vector<std::vector<double>> col((size_t)Ny * Nz);
    const size_t ntri = idx.size() / 3;
    for (size_t t = 0; t < ntri; ++t) {
        uint32_t i0 = idx[3 * t], i1 = idx[3 * t + 1], i2 = idx[3 * t + 2];
        if (3u * i0 + 2 >= pos.size() || 3u * i1 + 2 >= pos.size() || 3u * i2 + 2 >= pos.size())
            continue;
        V3 A{pos[3 * i0], pos[3 * i0 + 1], pos[3 * i0 + 2]};
        V3 B{pos[3 * i1], pos[3 * i1 + 1], pos[3 * i1 + 2]};
        V3 C{pos[3 * i2], pos[3 * i2 + 1], pos[3 * i2 + 2]};
        double ymin = std::min({A.y, B.y, C.y}), ymax = std::max({A.y, B.y, C.y});
        double zmin = std::min({A.z, B.z, C.z}), zmax = std::max({A.z, B.z, C.z});
        int iy0 = std::max(0, (int)std::floor(ymin / voxel));
        int iy1 = std::min(Ny - 1, (int)std::ceil(ymax / voxel));
        int iz0 = std::max(0, (int)std::floor(zmin / voxel));
        int iz1 = std::min(Nz - 1, (int)std::ceil(zmax / voxel));
        for (int iy = iy0; iy <= iy1; ++iy) {
            double yc = (iy + 0.5) * voxel;
            for (int iz = iz0; iz <= iz1; ++iz) {
                double zc = (iz + 0.5) * voxel;
                double x;
                if (tri_x_at(yc, zc, A, B, C, x)) col[(size_t)iy * Nz + iz].push_back(x);
            }
        }
    }
    for (int iy = 0; iy < Ny; ++iy)
        for (int iz = 0; iz < Nz; ++iz) {
            auto& xs = col[(size_t)iy * Nz + iz];
            if (xs.size() < 2) continue;
            std::sort(xs.begin(), xs.end());
            // Fill between consecutive entry/exit pairs (parity).
            for (size_t k = 0; k + 1 < xs.size(); k += 2) {
                int ix0 = std::max(0, (int)std::floor(xs[k] / voxel));
                int ix1 = std::min(Nx - 1, (int)std::floor(xs[k + 1] / voxel));
                for (int ix = ix0; ix <= ix1; ++ix)
                    mask[((size_t)ix * Ny + iy) * Nz + iz] = 1;
            }
        }
}

}  // namespace

LbmResult run_wind_tunnel(const std::vector<std::vector<float>>& body_pos,
                          const std::vector<std::vector<uint32_t>>& body_idx, const FlowParams& fp,
                          double* reynolds_out) {
    LbmResult R;
    if (body_pos.empty()) { R.ok = false; R.error = "no geometry"; return R; }

    // Axis convention: the LBM kernel measures DRAG along its X and LIFT along its Y. AI-CAD is
    // Z-up, so we want lift to come out along CAD +Z. Map CAD (x,y,z) -> LBM (x, z, y): flow on X,
    // CAD-up(Z) -> LBM-Y(lift), CAD-Y(span) -> LBM-Z. Angle of attack is a pitch about the span
    // axis (CAD-Y) applied in the CAD X-Z plane BEFORE the swap, so +AoA tilts the nose up and
    // grows lift on LBM-Y.
    const double a = -fp.aoa_deg * 3.14159265358979323846 / 180.0;
    const double ca = std::cos(a), sa = std::sin(a);

    // ── Step 1: compute the PART bounding box in the LBM (rotated) frame.
    // This pass over the ORIGINAL body_pos vertices is kept strictly separate from the
    // in-place mutation below so that the domain-offset shift applied during voxelization
    // can never contaminate part[] / lo[] / hi[].  ref_length_mm and the Reynolds
    // characteristic length must reflect the PART geometry, not the padded domain.
    double lo[3] = {1e30, 1e30, 1e30}, hi[3] = {-1e30, -1e30, -1e30};
    bool any = false;
    for (const auto& p : body_pos)
        for (size_t i = 0; i + 2 < p.size(); i += 3) {
            const double x = p[i], y = p[i + 1], z = p[i + 2];
            const double xr = x * ca + z * sa, zr = -x * sa + z * ca;  // pitch about CAD-Y
            // LBM frame: axis-0=xr (flow), axis-1=zr (lift=CAD-Z), axis-2=y (span=CAD-Y)
            lo[0] = std::min(lo[0], xr); hi[0] = std::max(hi[0], xr);
            lo[1] = std::min(lo[1], zr); hi[1] = std::max(hi[1], zr);
            lo[2] = std::min(lo[2],   y); hi[2] = std::max(hi[2],   y);
            any = true;
        }
    if (!any) { R.ok = false; R.error = "empty geometry"; return R; }

    // ── Step 2: build the rotated+axis-swapped working copy used for voxelization.
    // This mutates the copy in-place; lo[]/hi[] are already final from Step 1.
    std::vector<std::vector<float>> pos = body_pos;
    for (auto& p : pos)
        for (size_t i = 0; i + 2 < p.size(); i += 3) {
            const double x = p[i], y = p[i + 1], z = p[i + 2];
            const double xr = x * ca + z * sa, zr = -x * sa + z * ca;
            p[i]     = (float)xr;  // LBM X (flow)
            p[i + 1] = (float)zr;  // LBM Y (lift) = CAD-Z after pitch
            p[i + 2] = (float)y;   // LBM Z (span) = CAD-Y
        }

    double part[3] = {hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]};
    double longest = std::max({part[0], part[1], part[2]});
    if (longest <= 1e-6) { R.ok = false; R.error = "degenerate bounding box"; return R; }

    // Wind-tunnel domain (mm): up 1x, part, down 3x; lateral 1x each side (matches lbm.py).
    double lat = std::max(longest, 10.0), up = std::max(longest, 10.0), dn = std::max(3.0 * longest, 30.0);
    double dom[3] = {up + part[0] + dn, lat + part[1] + lat, lat + part[2] + lat};
    double dom_longest = std::max({dom[0], dom[1], dom[2]});
    int cells = std::max(16, fp.cells);
    double voxel = dom_longest / cells;
    int Nx = std::max(4, (int)std::ceil(dom[0] / voxel));
    int Ny = std::max(4, (int)std::ceil(dom[1] / voxel));
    int Nz = std::max(4, (int)std::ceil(dom[2] / voxel));

    double mem_gb = (double)Nx * Ny * Nz * 19.0 * 4.0 * 2.0 / (1024.0 * 1024.0 * 1024.0);
    if (mem_gb > 2.0) { R.ok = false; R.error = "domain too large (" + std::to_string(mem_gb) + " GB) — lower cells"; return R; }

    // Offset geometry into the domain corner with the pads, then voxelize each body (OR).
    double off[3] = {up - lo[0], lat - lo[1], lat - lo[2]};
    std::vector<uint8_t> mask((size_t)Nx * Ny * Nz, 0);
    for (size_t b = 0; b < pos.size(); ++b) {
        std::vector<float> shifted = pos[b];
        for (size_t i = 0; i + 2 < shifted.size(); i += 3) {
            shifted[i] += (float)off[0]; shifted[i + 1] += (float)off[1]; shifted[i + 2] += (float)off[2];
        }
        voxelize_body(shifted, body_idx[b], mask, Nx, Ny, Nz, voxel);
    }
    size_t solid_cells = 0;
    for (uint8_t m : mask) solid_cells += m;
    if (solid_cells == 0) { R.ok = false; R.error = "voxelization produced no solid cells"; return R; }

    // Lattice params from the physical inlet speed (mirror lbm.py).
    double speed = std::max(fp.velocity_ms, 1e-6);
    double dx_m = voxel * 1e-3;
    double u_lat = 0.05;
    double dt_s = u_lat * dx_m / speed;
    double L_char_m = longest * 1e-3;
    double reynolds = (fp.nu_fluid > 0) ? speed * L_char_m / fp.nu_fluid : 0.0;
    double nu_lat = (reynolds > 0) ? u_lat * L_char_m / dx_m / reynolds : kCs2 * 0.5;
    nu_lat = std::max(nu_lat, kCs2 * 0.01);
    double tau0 = std::min(1.99, std::max(0.51, nu_lat / kCs2 + 0.5));
    if (reynolds_out) *reynolds_out = reynolds;

    LbmParams lp;
    lp.u_lat = u_lat;
    lp.tau0 = tau0;
    lp.voxel_mm = voxel;
    lp.dx_m = dx_m;
    lp.dt_s = dt_s;
    lp.rho_air = fp.rho_fluid;

    // Optional live-progress side-channel: write a tiny JSON file at each LBM checkpoint so a
    // background run can be polled (get_job) like the OpenFOAM path. Atomic (tmp + rename) so a
    // reader never sees a half-written file. No-op when no progress_path was supplied.
    std::function<void(int, int)> on_progress;
    if (!fp.progress_path.empty()) {
        const std::string path = fp.progress_path;
        on_progress = [path](int step, int total) {
            const int pct = (total > 0) ? (int)std::lround(100.0 * step / total) : 0;
            char buf[160];
            std::snprintf(buf, sizeof(buf),
                          "{\"pct\":%d,\"step\":%d,\"n_steps\":%d,\"stage\":\"lbm_solve\"}",
                          pct, step, total);
            // Overwrite in place each checkpoint. (NOT tmp+rename: std::rename does not replace an
            // existing file on Windows, which would freeze the file at the first write.) A torn
            // read is harmless — the poller ignores an unparseable file and keeps the last value.
            std::ofstream f(path, std::ios::binary | std::ios::trunc);
            if (f) f << buf;
        };
    }
    R = lbm_run(mask, Nx, Ny, Nz, lp, std::max(1, fp.n_steps), on_progress);

    // Resolution diagnostics. The grid pitch is set from the DOMAIN's longest span, so a thin
    // feature (a 4 mm wing/tail edge) can fall below one voxel and vanish — the real cause of
    // "lift ~0" and the frontal-area collapse seen at low `cells`. Surface the pitch, the smallest
    // part dimension, and how many voxels span it so the caller can warn rather than trust a
    // silently-unresolved solve. (Note `part[]` here is in LBM axes: [0]=flow, [1]=lift, [2]=span.)
    const double min_part = std::min({part[0], part[1], part[2]});
    R.voxel_mm = voxel;
    R.ref_length_mm = longest;
    R.min_feature_mm = min_part;
    R.min_feature_voxels = (voxel > 0.0) ? (int)std::floor(min_part / voxel) : 0;
    if (R.ok && R.min_feature_voxels < 4) {
        R.warning = "coarse grid: the smallest part dimension (" +
                    std::to_string((int)std::round(min_part)) + " mm) spans only " +
                    std::to_string(R.min_feature_voxels) + " voxel(s) at " +
                    std::to_string((int)std::round(voxel)) + " mm pitch — thin features are "
                    "under-resolved (lift/forces unreliable). Raise `cells` so the thinnest "
                    "feature spans >= 8 voxels, or simulate the wing/section alone.";
    }
    return R;
}

}  // namespace sim
}  // namespace aicad
