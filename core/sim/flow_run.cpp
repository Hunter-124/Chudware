// core/sim/flow_run.cpp — see flow_run.h. Voxelize triangle soup -> wind-tunnel LBM.
// Domain layout + lattice-param derivation mirror engine/sim/lbm.py::run_wind_tunnel; the
// trimesh voxelize+fill is replaced by a self-contained X-scanline parity fill (per body).
#include "sim/flow_run.h"
#include "sim/sim_cache.h"
#include "sim/sim_cancel.h"

#include <nlohmann/json.hpp>

#include "util/resource_governor.h"
#include "prof/phases.h"  // per-phase timers (StudyScope "flow_run")

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <functional>
#include <iterator>
#include <limits>
#include <string>
#include <vector>

namespace chudware {
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

namespace {

nlohmann::json lbm_to_json(const LbmResult& R) {
    return nlohmann::json{
        {"ok", R.ok},
        {"error", R.error},
        {"nx", R.nx},
        {"ny", R.ny},
        {"nz", R.nz},
        {"ux", R.ux},
        {"uy", R.uy},
        {"uz", R.uz},
        {"rho", R.rho},
        {"cd", R.cd},
        {"drag_n", R.drag_n},
        {"lift_n", R.lift_n},
        {"frontal_area_mm2", R.frontal_area_mm2},
        {"steps_run", R.steps_run},
        {"diverged", R.diverged},
        {"residual", R.residual},
        {"converged", R.converged},
        {"voxel_mm", R.voxel_mm},
        {"ref_length_mm", R.ref_length_mm},
        {"min_feature_mm", R.min_feature_mm},
        {"min_feature_voxels", R.min_feature_voxels},
        {"warning", R.warning},
        {"backend", R.backend},
        {"backend_fallback", R.backend_fallback},
    };
}

LbmResult lbm_from_json(const nlohmann::json& j) {
    LbmResult R;
    R.ok = j.value("ok", false);
    R.error = j.value("error", std::string());
    R.nx = j.value("nx", 0);
    R.ny = j.value("ny", 0);
    R.nz = j.value("nz", 0);
    if (j.contains("ux") && j["ux"].is_array()) R.ux = j["ux"].get<std::vector<float>>();
    if (j.contains("uy") && j["uy"].is_array()) R.uy = j["uy"].get<std::vector<float>>();
    if (j.contains("uz") && j["uz"].is_array()) R.uz = j["uz"].get<std::vector<float>>();
    if (j.contains("rho") && j["rho"].is_array()) R.rho = j["rho"].get<std::vector<float>>();
    R.cd = j.value("cd", 0.0);
    R.drag_n = j.value("drag_n", 0.0);
    R.lift_n = j.value("lift_n", 0.0);
    R.frontal_area_mm2 = j.value("frontal_area_mm2", 0.0);
    R.steps_run = j.value("steps_run", 0);
    R.diverged = j.value("diverged", false);
    R.residual = j.value("residual", -1.0);
    R.converged = j.value("converged", false);
    R.voxel_mm = j.value("voxel_mm", 0.0);
    R.ref_length_mm = j.value("ref_length_mm", 0.0);
    R.min_feature_mm = j.value("min_feature_mm", 0.0);
    R.min_feature_voxels = j.value("min_feature_voxels", 0);
    R.warning = j.value("warning", std::string());
    R.backend = j.value("backend", std::string());
    R.backend_fallback = j.value("backend_fallback", std::string());
    return R;
}

// Content-addressed key for a wind-tunnel call: geometry + every FlowParams field.
// progress_path and job_id are intentionally excluded (side-channel / run-control only).
std::string cfd_input_hash(const std::vector<std::vector<float>>& body_pos,
                           const std::vector<std::vector<uint32_t>>& body_idx,
                           const FlowParams& fp) {
    nlohmann::json bodies = nlohmann::json::array();
    for (size_t b = 0; b < body_pos.size(); ++b) {
        nlohmann::json body;
        body["pos"] = body_pos[b];
        if (b < body_idx.size()) body["idx"] = body_idx[b];
        bodies.push_back(std::move(body));
    }
    nlohmann::json inputs = {
        {"bodies", bodies},
        {"velocity_ms", fp.velocity_ms},
        {"aoa_deg", fp.aoa_deg},
        {"nu_fluid", fp.nu_fluid},
        {"rho_fluid", fp.rho_fluid},
        {"cells", fp.cells},
        {"n_steps", fp.n_steps},
    };
    return chudware::sim::cache::hash_cfd(inputs);
}

}  // namespace

LbmResult run_wind_tunnel(const std::vector<std::vector<float>>& body_pos,
                          const std::vector<std::vector<uint32_t>>& body_idx, const FlowParams& fp,
                          double* reynolds_out) {
    LbmResult R;
    if (body_pos.empty()) { R.ok = false; R.error = "no geometry"; return R; }

    // Cancel registration (sim_cancel): an empty job_id is an anonymous run and registers
    // nothing. The guard unregisters on every return path (cache hit, error, cancel, done).
    const SimCancelToken cancel_tok = sim_job_register(fp.job_id);
    struct JobGuard {
        const std::string& id;
        ~JobGuard() { sim_job_unregister(id); }
    } _job_guard{fp.job_id};
    std::function<bool()> should_cancel;
    if (cancel_tok) should_cancel = [cancel_tok] { return cancel_tok->load(); };

    const std::string input_hash = cfd_input_hash(body_pos, body_idx, fp);
    {
        auto hit = chudware::sim::cache::lookup(chudware::sim::cache::Kind::Cfd, input_hash);
        if (hit.hit) {
            R = lbm_from_json(hit.result);
            // Reynolds is a pure function of inputs; recompute cheaply for the out-param.
            if (reynolds_out) {
                double lo[3] = {1e30, 1e30, 1e30}, hi[3] = {-1e30, -1e30, -1e30};
                for (const auto& p : body_pos)
                    for (size_t i = 0; i + 2 < p.size(); i += 3) {
                        lo[0] = std::min(lo[0], (double)p[i]);
                        hi[0] = std::max(hi[0], (double)p[i]);
                        lo[1] = std::min(lo[1], (double)p[i + 1]);
                        hi[1] = std::max(hi[1], (double)p[i + 1]);
                        lo[2] = std::min(lo[2], (double)p[i + 2]);
                        hi[2] = std::max(hi[2], (double)p[i + 2]);
                    }
                const double longest = std::max({hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]});
                const double speed = std::max(fp.velocity_ms, 1e-6);
                *reynolds_out = (fp.nu_fluid > 0) ? speed * (longest * 1e-3) / fp.nu_fluid : 0.0;
            }
            return R;
        }
    }

    // Per-phase record (prof/phases.h): setup+voxelize / solve (the nested lbm_run also
    // opens its own "lbm" study with the init/loop/output split) / post.
    chudware::prof::phases::StudyScope _study("flow_run");
    const auto t_setup0 = std::chrono::steady_clock::now();

    // Axis convention: the LBM kernel measures DRAG along its X and LIFT along its Y. Chudware is
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

    // Governor: the lattice must fit the HOST's memory budget (was a hard-coded 2 GB —
    // too big for an 8 GB laptop, needlessly small on a big box). ~256 B/cell covers the
    // two f-field copies (19 dirs × f32 × 2 = 152 B), macros (16 B), mask (1 B) and the
    // collision scratch. Degrade gracefully: shrink the resolution (cells ~ cbrt(mem))
    // and note it in R.warning, refusing only at the floor.
    std::string governor_note;
    {
        constexpr double kBytesPerCell = 256.0;
        const uint64_t mem_budget = chudware::governor::budget_bytes();
        for (int tries = 0; tries < 4; ++tries) {
            const double need = (double)Nx * Ny * Nz * kBytesPerCell;
            if (mem_budget == UINT64_MAX || need <= (double)mem_budget) break;
            const int cells_new = std::max(
                16, (int)std::floor(cells * std::cbrt((double)mem_budget / need)));
            if (cells_new >= cells) {  // at the floor and still over budget: refuse clearly
                std::string why;
                chudware::governor::admit((uint64_t)need, "CFD flow lattice", why);
                R.ok = false;
                R.error = why;
                return R;
            }
            if (governor_note.empty()) {
                char buf[192];
                std::snprintf(buf, sizeof buf,
                              "governor: grid reduced %d->%d cells to fit this machine's "
                              "memory budget (raise CHUDWARE_MEM_FRAC for more resolution)",
                              cells, cells_new);
                governor_note = buf;
            }
            cells = cells_new;
            voxel = dom_longest / cells;
            Nx = std::max(4, (int)std::ceil(dom[0] / voxel));
            Ny = std::max(4, (int)std::ceil(dom[1] / voxel));
            Nz = std::max(4, (int)std::ceil(dom[2] / voxel));
        }
    }

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
    const auto t_solve0 = std::chrono::steady_clock::now();
    chudware::prof::phases::add("voxelize", std::chrono::duration<double, std::milli>(
                                             t_solve0 - t_setup0).count());
    R = lbm_run(mask, Nx, Ny, Nz, lp, std::max(1, fp.n_steps), on_progress, should_cancel);
    chudware::prof::phases::add("solve", std::chrono::duration<double, std::milli>(
                                          std::chrono::steady_clock::now() - t_solve0).count());

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
    if (!governor_note.empty())
        R.warning = R.warning.empty() ? governor_note : (R.warning + "; " + governor_note);
    if (R.ok) chudware::sim::cache::store(chudware::sim::cache::Kind::Cfd, input_hash, lbm_to_json(R));
    return R;
}

// ────────────────────────────────────────────────────────────────────────────────────────────
// run_flow_stl — see flow_run.h. STL-shaped front-end mirroring the retired Python sim
// worker's sim_run_flow (engine_oracle/sim/worker.py + engine_oracle/sim/lbm.py).
// ────────────────────────────────────────────────────────────────────────────────────────────

namespace {

// Grid presets — mirror engine_oracle/sim/lbm.py::_PRESETS exactly.
struct FlowPreset { int cells; int steps; };
FlowPreset flow_preset(const std::string& name, bool& known) {
    known = true;
    if (name == "coarse") return {48, 1500};
    if (name == "medium") return {64, 3000};
    if (name == "fine")   return {96, 6000};
    if (name == "xfine")  return {128, 10000};
    known = false;
    return {48, 1500};
}

// Load an STL (binary or ASCII, units = mm) into a triangle soup: pos = flat 3*nverts, idx =
// flat 3*ntris sequential (no vertex dedup — the X-scanline voxelizer indexes per-triangle,
// so sharing vertices would not save work). Returns false with `err` on any malformed input.
bool load_stl_soup(const std::string& path, std::vector<float>& pos,
                   std::vector<uint32_t>& idx, std::string& err) {
    std::ifstream f(path, std::ios::binary);
    if (!f) { err = "cannot open STL: " + path; return false; }
    std::string data((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    if (data.size() < 15) { err = "STL too small: " + path; return false; }

    // ASCII detection: starts with "solid" and carries a "facet" keyword (a binary header may
    // also start with the bytes "solid", so the facet check is what disambiguates).
    bool ascii = false;
    {
        size_t i = data.find_first_not_of(" \t\r\n");
        if (i != std::string::npos && data.compare(i, 5, "solid") == 0 &&
            data.find("facet", i) != std::string::npos)
            ascii = true;
    }

    if (ascii) {
        // Every "vertex x y z" line contributes one vertex; each consecutive triple one tri.
        const char* p = data.c_str();
        std::vector<float> v;
        while ((p = std::strstr(p, "vertex")) != nullptr) {
            p += 6;
            char* end = nullptr;
            float x = std::strtof(p, &end); if (end == p) { ++p; continue; } p = end;
            float y = std::strtof(p, &end); if (end == p) { err = "bad ASCII STL vertex"; return false; } p = end;
            float z = std::strtof(p, &end); if (end == p) { err = "bad ASCII STL vertex"; return false; } p = end;
            v.push_back(x); v.push_back(y); v.push_back(z);
        }
        if (v.size() < 9 || v.size() % 9 != 0) {
            err = "ASCII STL vertex count not a multiple of 3 per triangle";
            return false;
        }
        pos = std::move(v);
    } else {
        // Binary: 80-byte header, uint32 tri count, 50 bytes per tri (normal + 3 verts + attr).
        if (data.size() < 84) { err = "binary STL shorter than header"; return false; }
        uint32_t ntri = 0;
        std::memcpy(&ntri, data.data() + 80, 4);
        const uint64_t need = 84ull + 50ull * ntri;
        if (need != data.size()) { err = "binary STL size mismatch (not an STL?)"; return false; }
        pos.resize((size_t)ntri * 9);
        for (uint32_t t = 0; t < ntri; ++t) {
            const char* rec = data.data() + 84 + 50ull * t;
            std::memcpy(pos.data() + (size_t)t * 9, rec + 12, 9 * sizeof(float));
        }
    }
    const size_t ntri = pos.size() / 9;
    idx.resize(ntri * 3);
    for (uint32_t i = 0; i < (uint32_t)(ntri * 3); ++i) idx[i] = i;
    return true;
}

struct Vec3 { double x, y, z; };
Vec3 vcross(const Vec3& a, const Vec3& b) {
    return {a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x};
}
double vnorm(const Vec3& a) { return std::sqrt(a.x * a.x + a.y * a.y + a.z * a.z); }
Vec3 vscale(const Vec3& a, double s) { return {a.x * s, a.y * s, a.z * s}; }

// Compose the incoming flow unit vector from a base axis + AoA + sideslip.
// Mirrors engine_oracle/sim/lbm.py::_compose_flow_dir (world is Z-up).
Vec3 compose_flow_dir(const Vec3& base, double aoa_deg, double sideslip_deg) {
    const double pi = 3.14159265358979323846;
    Vec3 f0 = vscale(base, 1.0 / (vnorm(base) + 1e-12));
    Vec3 z{0.0, 0.0, 1.0};
    Vec3 side0 = vcross(z, f0);
    if (vnorm(side0) < 1e-9) side0 = {0.0, 1.0, 0.0};
    side0 = vscale(side0, 1.0 / vnorm(side0));
    Vec3 up0 = vcross(f0, side0);
    up0 = vscale(up0, 1.0 / (vnorm(up0) + 1e-12));
    const double a = aoa_deg * pi / 180.0, b = sideslip_deg * pi / 180.0;
    Vec3 flow{
        std::cos(a) * std::cos(b) * f0.x + std::sin(a) * up0.x + std::cos(a) * std::sin(b) * side0.x,
        std::cos(a) * std::cos(b) * f0.y + std::sin(a) * up0.y + std::cos(a) * std::sin(b) * side0.y,
        std::cos(a) * std::cos(b) * f0.z + std::sin(a) * up0.z + std::cos(a) * std::sin(b) * side0.z};
    return vscale(flow, 1.0 / (vnorm(flow) + 1e-12));
}

nlohmann::json flow_error(const std::string& code, const std::string& msg) {
    return nlohmann::json{{"ok", false}, {"error", msg}, {"code", code}, {"message", msg}};
}

}  // namespace

nlohmann::json run_flow_stl(const nlohmann::json& params) {
    // The OpenFOAM bridge was a Python-worker feature (engine.sim.openfoam); the C++ core is
    // LBM-only. Fail loud rather than silently running the wrong solver.
    const std::string solver = params.value("solver", std::string("lbm"));
    if (solver == "openfoam")
        return flow_error("unsupported_solver",
                          "OpenFOAM is not available in the C++ flow core (LBM only).");

    const std::string stl_path = params.value("stl_path", std::string());
    if (stl_path.empty()) return flow_error("invalid_params", "sim_run_flow_stl: missing stl_path");

    std::vector<float> pos;
    std::vector<uint32_t> idx;
    std::string lerr;
    if (!load_stl_soup(stl_path, pos, idx, lerr)) return flow_error("invalid_stl", lerr);

    // Params (defaults mirror the Python worker).
    const double speed_mps = params.value("speed_mps", 10.0);
    const double aoa_deg = params.value("aoa_deg", 0.0);
    const double sideslip_deg = params.value("sideslip_deg", 0.0);
    const double roughness_um = params.value("roughness_um", 0.0);
    const double ref_area_mm2 = params.value("ref_area_mm2", 0.0);

    bool preset_known = true;
    const FlowPreset preset = flow_preset(params.value("preset", std::string("coarse")), preset_known);
    if (!preset_known)
        return flow_error("invalid_params", "unknown preset (coarse|medium|fine|xfine)");

    // Fluid (default air; non-positive values fall back like the oracle).
    std::string fluid_name = "air";
    double rho_fluid = 1.225, nu_fluid = 1.5e-5;
    if (params.contains("fluid") && params["fluid"].is_object()) {
        const auto& fl = params["fluid"];
        fluid_name = fl.value("name", fluid_name);
        rho_fluid = fl.value("density_kgm3", rho_fluid);
        nu_fluid = fl.value("kinematic_visc_m2s", nu_fluid);
    }
    if (rho_fluid <= 0.0) rho_fluid = 1.225;
    if (nu_fluid <= 0.0) nu_fluid = 1.5e-5;

    // Align the composed flow direction to the tunnel's +X. run_wind_tunnel assumes flow on
    // CAD-X with lift on CAD-Z (it axis-swaps Z->LBM-Y), so rotate the soup into an
    // orthonormal (drag, side, lift) basis: drag=flow_dir -> +X, side -> +Y, lift -> +Z.
    // AoA/sideslip are baked into flow_dir, so FlowParams.aoa_deg stays 0.
    Vec3 base{1.0, 0.0, 0.0};
    if (params.contains("direction") && params["direction"].is_array() &&
        params["direction"].size() == 3)
        base = {params["direction"][0].get<double>(), params["direction"][1].get<double>(),
                params["direction"][2].get<double>()};
    const Vec3 drag_dir = compose_flow_dir(base, aoa_deg, sideslip_deg);
    Vec3 side_raw = vcross({0.0, 0.0, 1.0}, drag_dir);
    if (vnorm(side_raw) < 1e-9) side_raw = vcross({1.0, 0.0, 0.0}, drag_dir);
    const Vec3 side_dir = vscale(side_raw, 1.0 / (vnorm(side_raw) + 1e-12));
    const Vec3 lift_dir = vscale(vcross(drag_dir, side_dir),
                                 1.0 / (vnorm(vcross(drag_dir, side_dir)) + 1e-12));
    // p_rot = M p with M's rows = (drag_dir, side_dir, lift_dir).
    for (size_t i = 0; i + 2 < pos.size(); i += 3) {
        const double x = pos[i], y = pos[i + 1], z = pos[i + 2];
        pos[i]     = (float)(drag_dir.x * x + drag_dir.y * y + drag_dir.z * z);
        pos[i + 1] = (float)(side_dir.x * x + side_dir.y * y + side_dir.z * z);
        pos[i + 2] = (float)(lift_dir.x * x + lift_dir.y * y + lift_dir.z * z);
    }

    FlowParams fp;
    fp.velocity_ms = speed_mps;
    fp.aoa_deg = 0.0;  // baked into the rotation above
    fp.nu_fluid = nu_fluid;
    fp.rho_fluid = rho_fluid;
    fp.cells = params.value("cells", preset.cells);
    fp.n_steps = params.value("n_steps", preset.steps);
    fp.progress_path = params.value("progress_path", std::string());
    fp.job_id = params.value("job_id", std::string());

    double reynolds = 0.0;
    LbmResult R = run_wind_tunnel({pos}, {idx}, fp, &reynolds);
    if (!R.ok) return flow_error("sim_flow_error", R.error.empty() ? "flow solve failed" : R.error);

    // ── Roughness skin-friction multiplier (qualitative) — mirror lbm.py ──
    std::vector<std::string> notes;
    double drag_n = R.drag_n;
    const double lift_n = R.lift_n;
    const double L_char_m = R.ref_length_mm * 1e-3;
    const double friction_frac = 0.5;  // assumed friction fraction of total drag (oracle)
    double roughness_factor = 1.0;
    char nbuf[256];
    if (roughness_um > 0.0 && L_char_m > 0.0) {
        const double ks_m = roughness_um * 1e-6;
        const double re_clamp = std::min(std::max(reynolds, 1e3), 1e9);
        const double cf_smooth = 0.074 * std::pow(re_clamp, -0.2);
        const double l_over_ks = std::max(L_char_m / ks_m, 1.0 + 1e-6);
        const double cf_rough = std::pow(1.89 + 1.62 * std::log10(l_over_ks), -2.5);
        roughness_factor = (cf_smooth > 0.0) ? std::max(1.0, cf_rough / cf_smooth) : 1.0;
        std::snprintf(nbuf, sizeof nbuf,
                      "Roughness Ks=%.1f um, Re=%.0f: Cf_smooth=%.4f, Cf_rough=%.4f, "
                      "drag x%.3f on the friction fraction (~%.0f%%) - QUALITATIVE.",
                      roughness_um, reynolds, cf_smooth, cf_rough,
                      1.0 + friction_frac * (roughness_factor - 1.0), friction_frac * 100.0);
        notes.push_back(nbuf);
    } else {
        notes.push_back("Roughness: smooth wall (Ks=0).");
    }
    if (roughness_factor > 1.0) drag_n *= (1.0 + friction_frac * (roughness_factor - 1.0));

    // ── Coefficients (mirror lbm.py: user ref area override, else auto frontal) ──
    const double ref_area_used_mm2 = (ref_area_mm2 > 0.0) ? ref_area_mm2 : R.frontal_area_mm2;
    const char* ref_area_src = (ref_area_mm2 > 0.0) ? "user" : "auto-frontal";
    const double ref_area_m2 = ref_area_used_mm2 * 1e-6;
    const double dyn_pressure = 0.5 * rho_fluid * speed_mps * speed_mps;
    double cd = 0.0, cl = 0.0;
    if (ref_area_m2 > 0.0 && dyn_pressure > 0.0) {
        cd = drag_n / (dyn_pressure * ref_area_m2);
        cl = lift_n / (dyn_pressure * ref_area_m2);
    }
    const double ld = (std::abs(drag_n) > 1e-9) ? lift_n / drag_n : 0.0;

    // vmax from the returned lattice velocity field (u_lat = 0.05, fixed in run_wind_tunnel).
    double vmax_lat = 0.0;
    for (size_t i = 0; i < R.ux.size(); ++i) {
        const double m = std::sqrt((double)R.ux[i] * R.ux[i] + (double)R.uy[i] * R.uy[i] +
                                   (double)R.uz[i] * R.uz[i]);
        if (m > vmax_lat) vmax_lat = m;
    }
    const double vmax_mps = vmax_lat * (speed_mps / 0.05);

    // ── Notes / reynolds_note (mirror the oracle's formats) ──
    const double u_lat = 0.05, cs2 = 1.0 / 3.0;
    const double dx_m = R.voxel_mm * 1e-3;
    const double dt_s = (speed_mps > 0.0) ? u_lat * dx_m / speed_mps : 0.0;
    double nu_lat = (reynolds > 0.0) ? u_lat * L_char_m / dx_m / reynolds : cs2 * 0.5;
    nu_lat = std::max(nu_lat, cs2 * 0.01);
    const double tau0 = std::min(1.99, std::max(0.51, nu_lat / cs2 + 0.5));
    std::snprintf(nbuf, sizeof nbuf,
                  "Domain %dx%dx%d, voxel %.2f mm; fluid %s (rho=%.1f kg/m^3, nu=%.2e m^2/s); "
                  "Re=%.0f; u_lat=%.3f, tau=%.3f; AoA=%.1f deg, sideslip=%.1f deg; "
                  "ref area=%.1f mm^2 (%s).",
                  R.nx, R.ny, R.nz, R.voxel_mm, fluid_name.c_str(), rho_fluid, nu_fluid,
                  reynolds, u_lat, tau0, aoa_deg, sideslip_deg, ref_area_used_mm2, ref_area_src);
    notes.insert(notes.begin(), nbuf);
    notes.push_back(
        "QUALITATIVE LBM (Smagorinsky LES, half-way bounce-back) - not a validated CFD result.");
    // side_n: the C++ kernel's momentum exchange reports drag/lift only (no span force yet).
    notes.push_back("Side force not reported by the C++ LBM kernel (side_n=0).");
    notes.push_back("Flow overlay (Cp colormap / streamlines / slice PNGs) not yet ported to the "
                    "C++ core — forces only this run.");
    if (!R.warning.empty()) notes.push_back(R.warning);

    std::string reynolds_note;
    {
        char rbuf[384];
        std::snprintf(rbuf, sizeof rbuf,
                      "%s Re ~%.0f (u_lat=%.3f, tau=%.3f, Nu_lat=%.4f, dx=%.2f mm, dt=%.3f us, "
                      "rho=%.1f kg/m^3, nu=%.2e m^2/s) - QUALITATIVE result; use a full CFD "
                      "solver for engineering decisions.",
                      fluid_name.c_str(), reynolds, u_lat, tau0, nu_lat, dx_m * 1e3, dt_s * 1e6,
                      rho_fluid, nu_fluid);
        reynolds_note = rbuf;
    }

    nlohmann::json out{
        {"ok", true},
        {"drag_n", drag_n},
        {"lift_n", lift_n},
        {"side_n", 0.0},
        {"cd", cd},
        {"cl", cl},
        {"ld", ld},
        {"reynolds", reynolds},
        {"frontal_area_mm2", R.frontal_area_mm2},
        {"ref_area_mm2", ref_area_used_mm2},
        {"reynolds_note", reynolds_note},
        {"diverged", R.diverged},
        // The C++ kernel keeps no drag history (the oracle's trace list); the panel's live
        // sparkline is driven by the progress_path side-channel instead.
        {"trace", nlohmann::json::array()},
        {"cp_min", -3.0},  // legend clamp, same constants as the oracle
        {"cp_max", 1.5},
        {"vmax_mps", vmax_mps},
        {"solver_notes", notes},
        {"scalar_fields", nlohmann::json::array()},
        {"overlay_pending", true},  // honest marker: no overlay_aicm/lines_aicm/slice_pngs yet
        {"steps_run", R.steps_run},
        {"converged", R.converged},
        {"residual", R.residual},
        {"voxel_mm", R.voxel_mm},
        {"min_feature_mm", R.min_feature_mm},
        {"min_feature_voxels", R.min_feature_voxels},
        {"grid", {R.nx, R.ny, R.nz}},
    };
    if (!R.warning.empty()) out["warning"] = R.warning;
    return out;
}

}  // namespace sim
}  // namespace chudware
