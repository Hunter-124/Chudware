// core/sim/flow_run.cpp — see flow_run.h. Voxelize triangle soup -> wind-tunnel LBM.
// Domain layout + lattice-param derivation mirror engine/sim/lbm.py::run_wind_tunnel; the
// trimesh voxelize+fill is replaced by a self-contained X-scanline parity fill (per body).
#include "sim/flow_run.h"
#include "sim/sim_cache.h"
#include "sim/sim_cancel.h"
#include "sim/progress.h"
#include "sim/overlay.h"
#include "util/thread_pool.h"

#include <nlohmann/json.hpp>

#include "util/resource_governor.h"
#include "prof/phases.h"  // per-phase timers (StudyScope "flow_run")

#include <algorithm>
#include <chrono>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <exception>
#include <functional>
#include <iterator>
#include <map>
#include <limits>
#include <new>
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

}  // namespace

// X-scanline parity fill one body's triangles into `mask` (OR) — see flow_run.h.
void voxelize_body(const std::vector<float>& pos, const std::vector<uint32_t>& idx,
                   std::vector<uint8_t>& mask, int Nx, int Ny, int Nz, double voxel,
                   bool center_sample) {
    // crossings[(iy*Nz)+iz] = sorted x-intersections for that column.
    std::vector<std::vector<double>> col((size_t)Ny * Nz);
    const size_t ntri = idx.size() / 3;
    const auto plan = chudware::governor::plan_for(
        static_cast<uint64_t>(mask.size()) + static_cast<uint64_t>(col.size()) * 24,
        "flow voxelization");
    const int slabs = std::max(1, std::min({Nz, plan.workers, 8}));
    chudware::pool::shared().parallel_for(slabs, [&](int slab) {
    const int zfirst = Nz * slab / slabs, zlast = Nz * (slab + 1) / slabs;
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
        int iz0 = std::max(zfirst, (int)std::floor(zmin / voxel));
        int iz1 = std::min(zlast - 1, (int)std::ceil(zmax / voxel));
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
        for (int iz = zfirst; iz < zlast; ++iz) {
            auto& xs = col[(size_t)iy * Nz + iz];
            if (xs.size() < 2) continue;
            std::sort(xs.begin(), xs.end());
            // A scanline exactly on the shared diagonal of a triangulated face intersects both
            // coplanar triangles at the same X. Count that geometric crossing once; otherwise
            // [entry,entry,exit,exit] pairs into two zero-thickness fills and opens a leak.
            const double merge_eps = std::max(1e-9, voxel * 1e-7);
            size_t out = 0;
            for (double x : xs) {
                if (out == 0 || std::abs(x - xs[out - 1]) > merge_eps)
                    xs[out++] = x;
            }
            xs.resize(out);
            // Fill between consecutive entry/exit pairs (parity). The default fill is
            // conservative along X (any cell the interior span touches); center_sample keeps
            // only cells whose CENTER lies inside, matching the Y/Z center sampling.
            for (size_t k = 0; k + 1 < xs.size(); k += 2) {
                int ix0, ix1;
                if (center_sample) {
                    ix0 = std::max(0, (int)std::ceil(xs[k] / voxel - 0.5));
                    ix1 = std::min(Nx - 1, (int)std::floor(xs[k + 1] / voxel - 0.5));
                } else {
                    ix0 = std::max(0, (int)std::floor(xs[k] / voxel));
                    ix1 = std::min(Nx - 1, (int)std::floor(xs[k + 1] / voxel));
                }
                for (int ix = ix0; ix <= ix1; ++ix)
                    mask[((size_t)ix * Ny + iy) * Nz + iz] = 1;
            }
        }
    });
}

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
        {"side_n", R.side_n},
        {"frontal_area_mm2", R.frontal_area_mm2},
        {"steps_run", R.steps_run},
        {"diverged", R.diverged},
        {"residual", R.residual},
        {"mass_imbalance", R.mass_imbalance},
        {"converged", R.converged},
        {"voxel_mm", R.voxel_mm},
        {"grid_origin_mm", R.grid_origin_mm},
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
    R.side_n = j.value("side_n", 0.0);
    R.frontal_area_mm2 = j.value("frontal_area_mm2", 0.0);
    R.steps_run = j.value("steps_run", 0);
    R.diverged = j.value("diverged", false);
    R.residual = j.value("residual", -1.0);
    R.mass_imbalance = j.value("mass_imbalance", -1.0);
    R.converged = j.value("converged", false);
    R.voxel_mm = j.value("voxel_mm", 0.0);
    if (j.contains("grid_origin_mm"))
        R.grid_origin_mm = j["grid_origin_mm"].get<std::array<double, 3>>();
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
        {"solver_revision", 6},
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
    try {
    if (body_pos.empty()) { R.error = "no geometry"; return R; }
    if (body_pos.size() != body_idx.size()) {
        R.error = "geometry body/triangle-list count mismatch";
        return R;
    }
    if (!std::isfinite(fp.velocity_ms) || fp.velocity_ms <= 0.0 ||
        !std::isfinite(fp.nu_fluid) || fp.nu_fluid <= 0.0 ||
        !std::isfinite(fp.rho_fluid) || fp.rho_fluid <= 0.0 ||
        !std::isfinite(fp.aoa_deg) || fp.cells < 16 || fp.n_steps <= 0) {
        R.error = "invalid flow parameters: speed, density, viscosity and steps must be "
                  "positive and finite; cells must be >= 16";
        return R;
    }
    std::vector<std::vector<uint32_t>> valid_body_idx(body_idx.size());
    for (size_t b = 0; b < body_pos.size(); ++b) {
        const auto& p = body_pos[b];
        const auto& input_idx = body_idx[b];
        auto& idx = valid_body_idx[b];
        if (p.size() < 9 || p.size() % 3 != 0 ||
            input_idx.size() < 3 || input_idx.size() % 3 != 0) {
            R.error = "malformed triangle geometry";
            return R;
        }
        if (!std::all_of(p.begin(), p.end(), [](float v) { return std::isfinite(v); })) {
            R.error = "geometry contains non-finite coordinates";
            return R;
        }
        const size_t nverts = p.size() / 3;
        for (uint32_t i : input_idx) {
            if (static_cast<size_t>(i) >= nverts) {
                R.error = "geometry triangle index out of range";
                return R;
            }
        }
        idx.reserve(input_idx.size());
        for (size_t t = 0; t < input_idx.size(); t += 3) {
            const size_t ia = static_cast<size_t>(input_idx[t]) * 3;
            const size_t ib = static_cast<size_t>(input_idx[t + 1]) * 3;
            const size_t ic = static_cast<size_t>(input_idx[t + 2]) * 3;
            const double abx = p[ib] - p[ia], aby = p[ib + 1] - p[ia + 1];
            const double abz = p[ib + 2] - p[ia + 2];
            const double acx = p[ic] - p[ia], acy = p[ic + 1] - p[ia + 1];
            const double acz = p[ic + 2] - p[ia + 2];
            const double cx = aby * acz - abz * acy;
            const double cy = abz * acx - abx * acz;
            const double cz = abx * acy - aby * acx;
            // OCCT tessellation can emit zero-area padding facets around seams. They
            // carry no surface area and are safe to discard; the closed-manifold check
            // below still rejects any resulting hole or otherwise incomplete shell.
            if (cx * cx + cy * cy + cz * cz <= 1e-24) continue;
            idx.push_back(input_idx[t]);
            idx.push_back(input_idx[t + 1]);
            idx.push_back(input_idx[t + 2]);
        }
        if (idx.empty()) {
            R.error = "geometry contains no non-degenerate triangles";
            return R;
        }
        using VertexKey = std::array<uint32_t, 3>;
        auto vertex_key = [&](uint32_t vi) {
            VertexKey key{};
            for (size_t axis = 0; axis < 3; ++axis) {
                float value = p[static_cast<size_t>(vi) * 3 + axis];
                if (value == 0.0f) value = 0.0f;  // canonicalize negative zero
                std::memcpy(&key[axis], &value, sizeof(value));
            }
            return key;
        };
        std::map<std::pair<VertexKey, VertexKey>, int> edge_counts;
        for (size_t t = 0; t < idx.size(); t += 3) {
            for (int e = 0; e < 3; ++e) {
                VertexKey a = vertex_key(idx[t + e]);
                VertexKey c = vertex_key(idx[t + (e + 1) % 3]);
                if (c < a) std::swap(a, c);
                ++edge_counts[{a, c}];
            }
        }
        if (std::any_of(edge_counts.begin(), edge_counts.end(),
                        [](const auto& edge) { return edge.second != 2; })) {
            R.error = "geometry is not a closed two-manifold triangle surface";
            return R;
        }
    }

    // Cancel registration (sim_cancel): an empty job_id is an anonymous run and registers
    // nothing. The guard unregisters on every return path (cache hit, error, cancel, done).
    const SimCancelToken cancel_tok = sim_job_register(fp.job_id);
    struct JobGuard {
        const std::string& id;
        const SimCancelToken& token;
        ~JobGuard() { sim_job_unregister(id, token); }
    } _job_guard{fp.job_id, cancel_tok};
    std::function<bool()> should_cancel;
    if (cancel_tok) should_cancel = [cancel_tok] { return cancel_tok->load(); };

    auto on_progress = [&](int step, int total) {
        const int pct = total > 0 ? (int)std::lround(100.0 * step / total) : 0;
        if (progress_enabled()) {
            auto event = progress_detail::event("lbm_solve", std::nullopt);
            event["pct"] = pct;
            event["step"] = step;
            event["n_steps"] = total;
            progress_detail::emit(event);
        }
        if (!fp.progress_path.empty()) {
            // Windows rename cannot replace an existing file. Readers ignore a torn
            // JSON snapshot and retain the preceding progress value.
            std::ofstream file(fp.progress_path, std::ios::binary | std::ios::trunc);
            if (file) file << "{\"pct\":" << pct << ",\"step\":" << step
                           << ",\"n_steps\":" << total << ",\"stage\":\"lbm_solve\"}";
        }
    };
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
                *reynolds_out =
                    (fp.nu_fluid > 0)
                        ? fp.velocity_ms * (longest * 1e-3) / fp.nu_fluid
                        : 0.0;
            }
            on_progress(fp.n_steps, fp.n_steps);
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

    // Keep anonymous/direct calls safe even when the adaptive governor is disabled.
    // The mask is allocated before lbm_run can apply its own size checks, so validate
    // multiplication and an absolute 8 GiB lattice ceiling here.
    constexpr size_t kHardMaxCells = (size_t{8} << 30) / 256;
    if (static_cast<size_t>(Nx) >
            std::numeric_limits<size_t>::max() / static_cast<size_t>(Ny) ||
        static_cast<size_t>(Nx) * static_cast<size_t>(Ny) >
            std::numeric_limits<size_t>::max() / static_cast<size_t>(Nz)) {
        R.error = "CFD grid dimensions overflow addressable memory";
        return R;
    }
    const size_t ncell = static_cast<size_t>(Nx) * static_cast<size_t>(Ny) *
                         static_cast<size_t>(Nz);
    if (ncell > kHardMaxCells) {
        R.error = "CFD grid exceeds the absolute 8 GiB lattice safety limit";
        return R;
    }
    // Offset geometry into the domain corner with the pads, then voxelize each body (OR).
    double off[3] = {up - lo[0], lat - lo[1], lat - lo[2]};
    std::vector<uint8_t> mask(ncell, 0);
    for (size_t b = 0; b < pos.size(); ++b) {
        auto& shifted = pos[b];  // working copy is no longer needed in the unshifted frame
        for (size_t i = 0; i + 2 < shifted.size(); i += 3) {
            shifted[i] += (float)off[0]; shifted[i + 1] += (float)off[1]; shifted[i + 2] += (float)off[2];
        }
        voxelize_body(shifted, valid_body_idx[b], mask, Nx, Ny, Nz, voxel);
    }
    size_t solid_cells = 0;
    for (uint8_t m : mask) solid_cells += m;
    if (solid_cells == 0) { R.ok = false; R.error = "voxelization produced no solid cells"; return R; }

    // Lattice params from the physical inlet speed (mirror lbm.py).
    const double speed = fp.velocity_ms;
    const double dx_m = voxel * 1e-3;
    constexpr double u_lat = 0.05;
    const double dt_s = u_lat * dx_m / speed;
    const double L_char_m = longest * 1e-3;
    const double reynolds = speed * L_char_m / fp.nu_fluid;
    const double requested_nu_lat = fp.nu_fluid * dt_s / (dx_m * dx_m);
    // BGK becomes numerically fragile as tau approaches 0.5.  The fast LBM route is
    // intentionally a qualitative reduced-Re model at high physical Reynolds numbers:
    // keep at least the validated tau floor below, and disclose the resulting
    // effective Reynolds number.
    // tau=0.55 is the validated stability floor for the single-relaxation-time
    // kernel; lower values keep too little numerical viscosity for a bounded
    // coarse lattice and do not settle within the advertised presets.
    constexpr double kMinTau = 0.55;
    const double nu_lat = std::max(requested_nu_lat, kCs2 * (kMinTau - 0.5));
    const bool viscosity_limited = nu_lat > requested_nu_lat * (1.0 + 1e-12);
    const double tau0 = nu_lat / kCs2 + 0.5;
    if (!std::isfinite(reynolds) || !std::isfinite(tau0) ||
        tau0 <= 0.500001 || tau0 > 2.0) {
        R.error = "flow setup is outside the BGK stability range: tau=" +
                  std::to_string(tau0) +
                  " (refine the grid, reduce speed, or increase viscosity)";
        return R;
    }
    if (reynolds_out) *reynolds_out = reynolds;

    LbmParams lp;
    lp.u_lat = u_lat;
    lp.tau0 = tau0;
    lp.voxel_mm = voxel;
    lp.dx_m = dx_m;
    lp.dt_s = dt_s;
    lp.rho_air = fp.rho_fluid;

    // The kernel schedules wall-clock heartbeats independently of convergence.
    const auto t_solve0 = std::chrono::steady_clock::now();
    chudware::prof::phases::add("voxelize", std::chrono::duration<double, std::milli>(
                                             t_solve0 - t_setup0).count());
    R = lbm_run(mask, Nx, Ny, Nz, lp, fp.n_steps, on_progress, should_cancel);
    chudware::prof::phases::add("solve", std::chrono::duration<double, std::milli>(
                                          std::chrono::steady_clock::now() - t_solve0).count());
    R.grid_origin_mm = {-off[0], -off[1], -off[2]};

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
    if (viscosity_limited) {
        const double effective_re =
            u_lat * (L_char_m / dx_m) / nu_lat;
        char buf[256];
        std::snprintf(
            buf, sizeof buf,
            "LBM stability viscosity raised tau to %.3f: effective lattice Re %.0f "
            "versus physical Re %.0f. Forces are qualitative; use OpenFOAM for "
            "engineering high-Reynolds predictions.",
            tau0, effective_re, reynolds);
        R.warning = R.warning.empty() ? std::string(buf) : (R.warning + "; " + buf);
    }
    if (!governor_note.empty())
        R.warning = R.warning.empty() ? governor_note : (R.warning + "; " + governor_note);
    if (R.ok && !R.converged) {
        R.ok = false;
        R.error = "flow solve did not converge: drag residual=" +
                  std::to_string(R.residual) + ", mass imbalance=" +
                  std::to_string(R.mass_imbalance);
    }
    if (R.ok) chudware::sim::cache::store(chudware::sim::cache::Kind::Cfd, input_hash, lbm_to_json(R));
    return R;
    } catch (const std::bad_alloc&) {
        R.ok = false;
        R.error = "CFD allocation failed for the requested grid";
        return R;
    } catch (const std::exception& e) {
        R.ok = false;
        R.error = std::string("CFD setup/solve failed: ") + e.what();
        return R;
    }
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

}  // namespace

#include "sim/stl_read.inc"

namespace {

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

// Package the solved field without repeating voxelization. Zero density marks
// the solid nodes (lbm_run clears their populations before its final macros).
std::string flow_overlay(const std::vector<float>& model_pos,
                         const std::vector<float>& rotated_pos,
                         const std::vector<uint32_t>& idx, const LbmResult& R,
                         const Vec3& drag, const Vec3& side, const Vec3& lift,
                         double speed_ms) {
    if (idx.size() / 3 > kMaxOverlayTris || R.nx < 2 || R.ny < 2 ||
        R.nz < 2 || R.voxel_mm <= 0.0) return {};
    const size_t cells = (size_t)R.nx * R.ny * R.nz;
    if (R.rho.size() != cells || R.ux.size() != cells ||
        R.uy.size() != cells || R.uz.size() != cells) return {};
    auto cell = [&](int x, int y, int z) {
        return ((size_t)x * R.ny + y) * R.nz + z;
    };
    auto world = [&](const Vec3& p) {
        const double x = R.grid_origin_mm[0] + (p.x + 0.5) * R.voxel_mm;
        const double y = R.grid_origin_mm[1] + (p.y + 0.5) * R.voxel_mm;
        const double z = R.grid_origin_mm[2] + (p.z + 0.5) * R.voxel_mm;
        return Vec3{drag.x * x + lift.x * y + side.x * z,
                    drag.y * x + lift.y * y + side.y * z,
                    drag.z * x + lift.z * y + side.z * z};
    };
    auto inside = [&](const Vec3& p) {
        return p.x >= 0.0 && p.x < R.nx - 1 && p.y >= 0.0 &&
               p.y < R.ny - 1 && p.z >= 0.0 && p.z < R.nz - 1;
    };
    auto velocity = [&](const Vec3& p, Vec3& u) {
        if (!inside(p)) return false;
        const int ix = (int)std::lround(p.x), iy = (int)std::lround(p.y),
                  iz = (int)std::lround(p.z);
        if (R.rho[cell(ix, iy, iz)] <= 0.0f) return false;
        const int x = (int)std::floor(p.x), y = (int)std::floor(p.y),
                  z = (int)std::floor(p.z);
        const double tx = p.x - x, ty = p.y - y, tz = p.z - z;
        u = {0.0, 0.0, 0.0};
        double weight = 0.0;
        for (int a = 0; a < 2; ++a)
            for (int b = 0; b < 2; ++b)
                for (int c = 0; c < 2; ++c) {
                    const size_t i = cell(x + a, y + b, z + c);
                    if (R.rho[i] <= 0.0f) continue;
                    const double w = (a ? tx : 1.0 - tx) *
                                     (b ? ty : 1.0 - ty) * (c ? tz : 1.0 - tz);
                    u.x += w * R.ux[i]; u.y += w * R.uy[i]; u.z += w * R.uz[i];
                    weight += w;
                }
        if (weight <= 0.0) return false;
        u = vscale(u, 1.0 / weight);
        return vnorm(u) > 1e-9;
    };
    aicm::AicmBody body;
    body.body_id = 1;
    body.name = "Flow surface";
    body.pos.insert_end(model_pos.begin(), model_pos.end());
    body.idx.insert_end(idx.begin(), idx.end());
    body.nrm.assign(model_pos.size(), 0.0f);
    body.faces.push_back({1, 0, (uint32_t)(idx.size() / 3), 6});
    for (size_t t = 0; t + 2 < idx.size(); t += 3) {
        const size_t a = (size_t)idx[t] * 3, b = (size_t)idx[t + 1] * 3,
                     c = (size_t)idx[t + 2] * 3;
        const Vec3 n = vcross({model_pos[b] - model_pos[a], model_pos[b+1] - model_pos[a+1],
                              model_pos[b+2] - model_pos[a+2]},
                             {model_pos[c] - model_pos[a], model_pos[c+1] - model_pos[a+1],
                              model_pos[c+2] - model_pos[a+2]});
        for (size_t v : {a, b, c}) {
            body.nrm[v] += (float)n.x;
            body.nrm[v + 1] += (float)n.y;
            body.nrm[v + 2] += (float)n.z;
        }
    }
    std::vector<float> cp(model_pos.size() / 3), speed(cp.size());
    Vec3 lo{1e30, 1e30, 1e30}, hi{-1e30, -1e30, -1e30};
    double speed_max = speed_ms;
    for (size_t v = 0; v < cp.size(); ++v) {
        const size_t k = v * 3;
        const Vec3 p{(rotated_pos[k] - R.grid_origin_mm[0]) / R.voxel_mm - 0.5,
                     (rotated_pos[k + 2] - R.grid_origin_mm[1]) / R.voxel_mm - 0.5,
                     (rotated_pos[k + 1] - R.grid_origin_mm[2]) / R.voxel_mm - 0.5};
        lo.x = std::min(lo.x, p.x); lo.y = std::min(lo.y, p.y); lo.z = std::min(lo.z, p.z);
        hi.x = std::max(hi.x, p.x); hi.y = std::max(hi.y, p.y); hi.z = std::max(hi.z, p.z);
        const int x = (int)std::lround(p.x), y = (int)std::lround(p.y), z = (int)std::lround(p.z);
        size_t nearest = cells;
        double best = std::numeric_limits<double>::infinity();
        for (int radius = 0; radius <= 4; ++radius) {
            for (int a = std::max(0, x-radius); a <= std::min(R.nx-1, x+radius); ++a)
                for (int b = std::max(0, y-radius); b <= std::min(R.ny-1, y+radius); ++b)
                    for (int c = std::max(0, z-radius); c <= std::min(R.nz-1, z+radius); ++c) {
                        const size_t i = cell(a,b,c);
                        if (R.rho[i] <= 0.0f) continue;
                        const double d = (a-p.x)*(a-p.x) + (b-p.y)*(b-p.y) + (c-p.z)*(c-p.z);
                        if (d < best) { best = d; nearest = i; }
                    }
            if (best < (radius + 0.5) * (radius + 0.5)) break;
        }
        if (nearest == cells) return {};  // no fabricated wall sample
        cp[v] = (float)std::clamp((2.0 / 3.0) * (R.rho[nearest] - 1.0) / (0.05 * 0.05), -3.0, 1.5);
        speed[v] = (float)(vnorm({R.ux[nearest], R.uy[nearest], R.uz[nearest]}) * speed_ms / 0.05);
        speed_max = std::max(speed_max, (double)speed[v]);
        const double n = vnorm({body.nrm[k], body.nrm[k+1], body.nrm[k+2]});
        if (n > 0.0) for (size_t a = 0; a < 3; ++a) body.nrm[k+a] /= (float)n;
    }
    std::vector<std::vector<float>> streams, stream_speed, slices, slice_speed;
    streams.reserve(180); stream_speed.reserve(180);
    auto append = [&](std::vector<float>& line, std::vector<float>& values, const Vec3& p, double s) {
        const Vec3 w = world(p);
        line.insert(line.end(), {(float)w.x, (float)w.y, (float)w.z});
        values.push_back((float)(s * speed_ms / 0.05));
    };
    // 180 seeds, at most 450 points each: <81k segments even on a very fine grid.
    for (int a = 0; a < 15; ++a) for (int b = 0; b < 12; ++b) {
        Vec3 p{std::max(1.0, lo.x - 3.0),
               std::clamp(lo.y + (hi.y-lo.y) * (-0.25 + 1.5 * (a+0.5)/15), 1.0, (double)R.ny-2),
               std::clamp(lo.z + (hi.z-lo.z) * (-0.25 + 1.5 * (b+0.5)/12), 1.0, (double)R.nz-2)};
        std::vector<float> line, values;
        line.reserve(450*3); values.reserve(450);
        for (int j = 0; j < 450; ++j) {
            Vec3 u;
            if (!velocity(p,u)) break;
            const double s = vnorm(u), dt = 0.65 / s;
            append(line,values,p,s);
            const Vec3 mid{p.x+0.5*dt*u.x, p.y+0.5*dt*u.y, p.z+0.5*dt*u.z};
            Vec3 um;
            if (!velocity(mid,um)) break;
            const double h = std::min(dt, 0.65 / vnorm(um));
            p = {p.x+h*um.x, p.y+h*um.y, p.z+h*um.z};
        }
        if (values.size() >= 2) {
            streams.push_back(std::move(line)); stream_speed.push_back(std::move(values));
        }
    }
    // Mid-span velocity vectors: sampled field data, speed in m/s on both endpoints.
    for (int a = 0; a < 40; ++a) for (int b = 0; b < 24; ++b) {
        const Vec3 p{1.0+(R.nx-3.0)*(a+0.5)/40,
                     1.0+(R.ny-3.0)*(b+0.5)/24, 0.5*(lo.z+hi.z)};
        Vec3 u;
        if (!velocity(p,u)) continue;
        const double s = vnorm(u);
        const Vec3 end{p.x+u.x/s, p.y+u.y/s, p.z+u.z/s};
        Vec3 unused;
        if (!velocity(end,unused)) continue;
        std::vector<float> line, values;
        append(line,values,p,s); append(line,values,end,s);
        slices.push_back(std::move(line)); slice_speed.push_back(std::move(values));
    }
    std::vector<std::vector<uint8_t>> chunks;
    chunks.push_back(aicm::scalar_chunk(1,"pressure_cp",cp,-3.0f,1.5f));
    chunks.push_back(aicm::scalar_chunk(1,"velocity",speed,0.0f,(float)speed_max));
    chunks.push_back(aicm::lines_chunk("streamlines",streams,&stream_speed));
    chunks.push_back(aicm::lines_chunk("slices",slices,&slice_speed));
    return publish_overlay_aicm(body,chunks);
}


}  // namespace

nlohmann::json run_flow_stl(const nlohmann::json& params) {
    try {
    // The OpenFOAM bridge was a Python-worker feature (engine.sim.openfoam); the C++ core is
    // LBM-only. Fail loud rather than silently running the wrong solver.
    const std::string solver = params.value("solver", std::string("lbm"));
    if (solver != "lbm") {
        if (solver == "openfoam")
            return flow_error("unsupported_solver",
                              "OpenFOAM is not available in the C++ flow core (LBM only).");
        return flow_error("invalid_params", "solver must be 'lbm'");
    }

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
    if (!std::isfinite(speed_mps) || speed_mps <= 0.0 ||
        !std::isfinite(aoa_deg) || !std::isfinite(sideslip_deg) ||
        !std::isfinite(roughness_um) || roughness_um < 0.0 ||
        !std::isfinite(ref_area_mm2) || ref_area_mm2 < 0.0) {
        return flow_error("invalid_params",
                          "speed must be positive; angles/roughness/reference area must be finite");
    }

    bool preset_known = true;
    const FlowPreset preset = flow_preset(params.value("preset", std::string("coarse")), preset_known);
    if (!preset_known)
        return flow_error("invalid_params", "unknown preset (coarse|medium|fine|xfine)");

    // Fluid (default air). Explicit invalid values are rejected rather than silently replaced.
    std::string fluid_name = "air";
    double rho_fluid = 1.225, nu_fluid = 1.5e-5;
    if (params.contains("fluid")) {
        if (!params["fluid"].is_object())
            return flow_error("invalid_params", "fluid must be an object");
        const auto& fl = params["fluid"];
        fluid_name = fl.value("name", fluid_name);
        rho_fluid = fl.value("density_kgm3", rho_fluid);
        nu_fluid = fl.value("kinematic_visc_m2s", nu_fluid);
    }
    if (!std::isfinite(rho_fluid) || rho_fluid <= 0.0 ||
        !std::isfinite(nu_fluid) || nu_fluid <= 0.0)
        return flow_error("invalid_params", "fluid density and kinematic viscosity must be positive");

    // Align the composed flow direction to the tunnel's +X. run_wind_tunnel assumes flow on
    // CAD-X with lift on CAD-Z (it axis-swaps Z->LBM-Y), so rotate the soup into an
    // orthonormal (drag, side, lift) basis: drag=flow_dir -> +X, side -> +Y, lift -> +Z.
    // AoA/sideslip are baked into flow_dir, so FlowParams.aoa_deg stays 0.
    Vec3 base{1.0, 0.0, 0.0};
    if (params.contains("direction")) {
        if (!params["direction"].is_array() || params["direction"].size() != 3)
            return flow_error("invalid_params", "direction must be a 3-vector");
        base = {params["direction"][0].get<double>(), params["direction"][1].get<double>(),
                params["direction"][2].get<double>()};
    }
    if (!std::isfinite(base.x) || !std::isfinite(base.y) || !std::isfinite(base.z) ||
        vnorm(base) <= 1e-12)
        return flow_error("invalid_params", "direction must be finite and non-zero");
    const Vec3 drag_dir = compose_flow_dir(base, aoa_deg, sideslip_deg);
    Vec3 side_raw = vcross({0.0, 0.0, 1.0}, drag_dir);
    if (vnorm(side_raw) < 1e-9) side_raw = vcross({1.0, 0.0, 0.0}, drag_dir);
    const Vec3 side_dir = vscale(side_raw, 1.0 / (vnorm(side_raw) + 1e-12));
    const Vec3 lift_dir = vscale(vcross(drag_dir, side_dir),
                                 1.0 / (vnorm(vcross(drag_dir, side_dir)) + 1e-12));
    // p_rot = M p with M's rows = (drag_dir, side_dir, lift_dir).
    const bool want_overlay = params.value("overlay", true);
    const std::vector<float> model_pos = want_overlay ? pos : std::vector<float>{};
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
    if (fp.cells < 16 || fp.n_steps <= 0)
        return flow_error("invalid_params", "cells must be >= 16 and n_steps must be positive");
    fp.progress_path = params.value("progress_path", std::string());
    fp.job_id = params.value("job_id", std::string());

    double reynolds = 0.0;
    LbmResult R = run_wind_tunnel({pos}, {idx}, fp, &reynolds);
    if (!R.ok) {
        const std::string code = R.diverged ? "solver_diverged" :
                                 (!R.converged && R.residual >= 0.0 ? "not_converged" :
                                  "sim_flow_error");
        nlohmann::json err = flow_error(code, R.error.empty() ? "flow solve failed" : R.error);
        err["diverged"] = R.diverged;
        err["converged"] = R.converged;
        err["steps_run"] = R.steps_run;
        err["residual"] = R.residual;
        err["mass_imbalance"] = R.mass_imbalance;
        return err;
    }

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
    const double requested_nu_lat = nu_fluid * dt_s / (dx_m * dx_m);
    constexpr double kMinTau = 0.55;
    const double nu_lat = std::max(requested_nu_lat, cs2 * (kMinTau - 0.5));
    const double tau0 = nu_lat / cs2 + 0.5;
    const double effective_re =
        (nu_lat > 0.0 && dx_m > 0.0) ? u_lat * (L_char_m / dx_m) / nu_lat : 0.0;
    std::snprintf(nbuf, sizeof nbuf,
                  "Domain %dx%dx%d, voxel %.2f mm; fluid %s (rho=%.1f kg/m^3, nu=%.2e m^2/s); "
                  "physical Re=%.0f, effective LBM Re=%.0f; u_lat=%.3f, tau=%.3f; "
                  "AoA=%.1f deg, sideslip=%.1f deg; ref area=%.1f mm^2 (%s).",
                  R.nx, R.ny, R.nz, R.voxel_mm, fluid_name.c_str(), rho_fluid, nu_fluid,
                  reynolds, effective_re, u_lat, tau0, aoa_deg, sideslip_deg,
                  ref_area_used_mm2, ref_area_src);
    notes.insert(notes.begin(), nbuf);
    notes.push_back(
        "QUALITATIVE LBM (Smagorinsky LES, half-way bounce-back) - not a validated CFD result.");
    std::string overlay_path;
    if (want_overlay) {
        try {
            overlay_path = flow_overlay(model_pos, pos, idx, R, drag_dir, side_dir, lift_dir, speed_mps);
        } catch (const std::exception& e) {
            notes.push_back(std::string("Flow overlay could not be packaged: ") + e.what());
        }
        if (overlay_path.empty())
            notes.push_back("Flow overlay unavailable: surface exceeded the display budget, "
                            "wall samples were unavailable, or publishing failed.");
    }
    if (!R.warning.empty()) notes.push_back(R.warning);

    std::string reynolds_note;
    {
        char rbuf[384];
        std::snprintf(rbuf, sizeof rbuf,
                      "%s physical Re ~%.0f; effective LBM Re ~%.0f "
                      "(u_lat=%.3f, tau=%.3f, Nu_lat=%.4f, dx=%.2f mm, dt=%.3f us, "
                      "rho=%.1f kg/m^3, nu=%.2e m^2/s) - QUALITATIVE result; use a full CFD "
                      "solver for engineering decisions.",
                      fluid_name.c_str(), reynolds, effective_re, u_lat, tau0, nu_lat,
                      dx_m * 1e3, dt_s * 1e6, rho_fluid, nu_fluid);
        reynolds_note = rbuf;
    }

    nlohmann::json out{
        {"ok", true},
        {"drag_n", drag_n},
        {"lift_n", lift_n},
        {"side_n", R.side_n},
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
        {"scalar_fields", overlay_path.empty() ? nlohmann::json::array()
                                             : nlohmann::json{"pressure_cp", "velocity"}},
        {"overlay_aicm", overlay_path},
        {"steps_run", R.steps_run},
        {"converged", R.converged},
        {"residual", R.residual},
        {"mass_imbalance", R.mass_imbalance},
        {"voxel_mm", R.voxel_mm},
        {"min_feature_mm", R.min_feature_mm},
        {"min_feature_voxels", R.min_feature_voxels},
        {"grid", {R.nx, R.ny, R.nz}},
    };
    if (!R.warning.empty()) out["warning"] = R.warning;
    return out;
    } catch (const nlohmann::json::exception& e) {
        return flow_error("invalid_params", std::string("invalid flow parameter type: ") + e.what());
    } catch (const std::exception& e) {
        return flow_error("sim_flow_error", e.what());
    }
}

}  // namespace sim
}  // namespace chudware
