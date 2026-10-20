// D3Q19 BGK Lattice-Boltzmann wind-tunnel core — see lbm.h.
//
// Faithful port of engine/sim/lbm.py's run_wind_tunnel loop + kernel helpers
// (_feq_fast, _stream, _smag_tau, _macros, _inlet_bc, _lateral_slip,
// _bounce_back, _build_boundary_links, _momentum_exchange).
//
// FLOAT32. Per-cell state + arithmetic is 32-bit float to mirror numpy's float32
// arrays. Reductions over the 19-direction axis are SEQUENTIAL float32 adds: numpy
// switches to pairwise summation only for axes >= 128 elements, so for the q=19
// axis `f.sum(axis=-1)` is exactly f[0]+f[1]+...+f[18] left-to-right in float32.
// We replicate that order. The drag accumulator is float64 (np.float64 in oracle).

#include "sim/lbm.h"

#ifdef CHUDWARE_WITH_CUDA
#include "sim/lbm_cuda.h"
#endif

#include "util/resource_governor.h"
#include "util/thread_pool.h"
#include "prof/phases.h"  // per-phase timers (StudyScope "lbm")

#include <array>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <new>
#include <cstdlib>
#include <thread>
#ifdef CHUDWARE_HAVE_OPENMP
#include <omp.h>
#endif

namespace chudware::sim {

namespace {

constexpr int NQ = 19;

// D3Q19 velocity set — EXACT order of engine/sim/lbm.py:_EQ (load-bearing).
constexpr int EQ[NQ][3] = {
    {0, 0, 0},    // 0  rest
    {1, 0, 0},    // 1
    {-1, 0, 0},   // 2
    {0, 1, 0},    // 3
    {0, -1, 0},   // 4
    {0, 0, 1},    // 5
    {0, 0, -1},   // 6
    {1, 1, 0},    // 7
    {-1, -1, 0},  // 8
    {1, -1, 0},   // 9
    {-1, 1, 0},   // 10
    {1, 0, 1},    // 11
    {-1, 0, -1},  // 12
    {1, 0, -1},   // 13
    {-1, 0, 1},   // 14
    {0, 1, 1},    // 15
    {0, -1, -1},  // 16
    {0, 1, -1},   // 17
    {0, -1, 1},   // 18
};

// Weights — EXACT _W (float32). 1/3 rest, 1/18 face (1..6), 1/36 edge (7..18).
const float W[NQ] = {
    1.0f / 3.0f,
    1.0f / 18.0f, 1.0f / 18.0f, 1.0f / 18.0f, 1.0f / 18.0f,
    1.0f / 18.0f, 1.0f / 18.0f,
    1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f,
    1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f,
    1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f,
};

// Opposite-direction index for bounce-back — EXACT _OPP.
constexpr int OPP[NQ] = {
    0, 2, 1, 4, 3, 6, 5, 8, 7, 10, 9, 12, 11, 14, 13, 16, 15, 18, 17};

// cs^2 = 1/3 as float32 (the oracle's _CS2).
const float CS2 = 1.0f / 3.0f;

// numpy float32 pairwise_sum over EXACTLY 19 contiguous elements. Replicates
// numpy's pairwise_sum (loops_utils.h.src): n in [8,128] uses 8 accumulators
// seeded with v[0..7], blocks of 8 added lane-wise, then the 8 lanes combined as
// ((r0+r1)+(r2+r3)) + ((r4+r5)+(r6+r7)), then the remainder added sequentially.
// For n=19: one block (v[8..15]) then remainder v[16],v[17],v[18]. Matching this
// exact float32 reduction order is what makes _macros / _smag_tau bit-identical to
// numpy's f.sum(axis=-1).
inline float pairwise_sum19(const float v[NQ]) {
    float r0 = v[0], r1 = v[1], r2 = v[2], r3 = v[3];
    float r4 = v[4], r5 = v[5], r6 = v[6], r7 = v[7];
    // one full block of 8 (i = 8..15)
    r0 += v[8];
    r1 += v[9];
    r2 += v[10];
    r3 += v[11];
    r4 += v[12];
    r5 += v[13];
    r6 += v[14];
    r7 += v[15];
    float res = ((r0 + r1) + (r2 + r3)) + ((r4 + r5) + (r6 + r7));
    // remainder i = 16,17,18 (n - n%8 = 16; tail added sequentially)
    res += v[16];
    res += v[17];
    res += v[18];
    return res;
}

// General numpy float32 pairwise_sum over an arbitrary-length contiguous range.
// Faithful to numpy's pairwise_sum (loops_utils.h.src) so np.sum() of a float32
// array is reproduced bit-for-bit: n<8 sequential; 8<=n<=128 the 8-lane unrolled
// block scheme (same as pairwise_sum19 but with a loop + arbitrary remainder); n>128
// split at the largest multiple-of-8 <= n/2. Used by _momentum_exchange's np.sum.
float pairwise_sum_f32(const float* a, std::size_t n) {
    if (n == 0) return 0.0f;
    if (n < 8) {
        float r = a[0];
        for (std::size_t i = 1; i < n; ++i) r += a[i];
        return r;
    }
    if (n <= 128) {
        float r0 = a[0], r1 = a[1], r2 = a[2], r3 = a[3];
        float r4 = a[4], r5 = a[5], r6 = a[6], r7 = a[7];
        std::size_t i = 8;
        for (; i + 8 <= n; i += 8) {
            r0 += a[i + 0];
            r1 += a[i + 1];
            r2 += a[i + 2];
            r3 += a[i + 3];
            r4 += a[i + 4];
            r5 += a[i + 5];
            r6 += a[i + 6];
            r7 += a[i + 7];
        }
        float res = ((r0 + r1) + (r2 + r3)) + ((r4 + r5) + (r6 + r7));
        for (; i < n; ++i) res += a[i];
        return res;
    }
    // n > 128: split into halves; first half rounded down to a multiple of 8.
    std::size_t n2 = n / 2;
    n2 -= n2 % 8;
    return pairwise_sum_f32(a, n2) + pairwise_sum_f32(a + n2, n - n2);
}

// Static, disjoint slabs preserve each cell's arithmetic and reduction order. OpenMP
// builds avoid queue allocations; portable builds use the existing bounded shared pool.
template <class Body>
void lattice_for(std::size_t count, int threads, const Body& body) {
    const int slabs = static_cast<int>(std::min<std::size_t>(count, threads));
    auto slab = [&](int s) {
        const std::size_t first = count * s / slabs;
        const std::size_t last = count * (s + 1) / slabs;
        for (std::size_t i = first; i < last; ++i) body(i);
    };
    if (slabs <= 1) {
        for (std::size_t i = 0; i < count; ++i) body(i);
        return;
    }
#ifdef CHUDWARE_HAVE_OPENMP
#pragma omp parallel for schedule(static) num_threads(slabs)
    for (int s = 0; s < slabs; ++s) slab(s);
#else
    chudware::pool::shared().parallel_for(slabs, slab);
#endif
}

}  // namespace

LbmResult lbm_run(const std::vector<uint8_t>& solid, int nx, int ny, int nz,
                  const LbmParams& p, int n_steps,
                  const std::function<void(int, int)>& on_progress,
                  const std::function<bool()>& should_cancel) {
    LbmResult R;
    R.nx = nx;
    R.ny = ny;
    R.nz = nz;
    try {
    if (nx < 3 || ny < 3 || nz < 3) {
        R.error = "lbm_run: grid dims must each be >= 3";
        return R;
    }
    const std::size_t max_cells = std::numeric_limits<std::size_t>::max() / NQ;
    if (static_cast<std::size_t>(nx) > max_cells / static_cast<std::size_t>(ny) ||
        static_cast<std::size_t>(nx) * static_cast<std::size_t>(ny) >
            max_cells / static_cast<std::size_t>(nz)) {
        R.error = "lbm_run: grid is too large";
        return R;
    }
    const std::size_t ncell = static_cast<std::size_t>(nx) *
                              static_cast<std::size_t>(ny) *
                              static_cast<std::size_t>(nz);
    if (solid.size() != ncell) {
        R.error = "lbm_run: solid mask size mismatch";
        return R;
    }
    if (n_steps <= 0) {
        R.error = "lbm_run: n_steps must be positive";
        return R;
    }
    if (!std::isfinite(p.u_lat) || p.u_lat <= 0.0 || p.u_lat > 0.1) {
        R.error = "lbm_run: u_lat must be finite and in (0, 0.1]";
        return R;
    }
    if (!std::isfinite(p.tau0) || p.tau0 <= 0.5 || p.tau0 > 2.0) {
        R.error = "lbm_run: tau0 must be finite and in (0.5, 2.0]";
        return R;
    }
    if (!std::isfinite(p.voxel_mm) || p.voxel_mm <= 0.0 ||
        !std::isfinite(p.dx_m) || p.dx_m <= 0.0 ||
        !std::isfinite(p.dt_s) || p.dt_s <= 0.0 ||
        !std::isfinite(p.rho_air) || p.rho_air <= 0.0) {
        R.error = "lbm_run: voxel_mm, dx_m, dt_s, and rho_air must be finite and positive";
        return R;
    }
    if (std::all_of(solid.begin(), solid.end(), [](uint8_t v) { return v != 0; })) {
        R.error = "lbm_run: solid mask contains no fluid cells";
        return R;
    }

    // Backend select: params.backend, else CHUDWARE_LBM_BACKEND, else "auto".
    // "cuda" / "auto" may pick CUDA when built+available; always fall back to CPU.
    std::string want = p.backend;
    if (want.empty()) {
        if (const char* e = std::getenv("CHUDWARE_LBM_BACKEND")) want = e;
        else want = "auto";
    }
    for (char& c : want) if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
    if (want != "auto" && want != "cpu" && want != "cuda") {
        R.error = "lbm_run: backend must be 'auto', 'cpu', or 'cuda'";
        return R;
    }
    R.backend = "cpu";
#ifdef CHUDWARE_WITH_CUDA
    const bool try_cuda = (want == "cuda" || want == "auto");
    if (try_cuda) {
        if (!lbm_cuda_device_available()) {
            R.backend_fallback = (want == "cuda")
                ? "CUDA requested but no device available"
                : "auto: no CUDA device, using CPU";
        } else {
            std::string why;
            LbmResult cr = lbm_run_cuda(solid, nx, ny, nz, p, n_steps, on_progress,
                                        should_cancel, why);
            if (cr.ok) {
                cr.backend = "cuda";
                if (!why.empty()) cr.backend_fallback = why;
                return cr;
            }
            // Cooperative stops are terminal, even if a one-shot cancel hook has
            // already reset; never restart a cancelled or memory-starved solve.
            if (cr.error.rfind("cancelled by sim_cancel", 0) == 0 ||
                cr.error.find("host memory critically low (governor)") != std::string::npos)
                return cr;
            if (should_cancel && should_cancel()) {
                cr.error = "cancelled by sim_cancel";
                return cr;
            }
            R.backend_fallback = why.empty()
                ? (cr.error.empty() ? "CUDA path failed; using CPU" : cr.error)
                : why;
            // fall through to CPU reference
        }
    }
#else
    if (want == "cuda") {
        R.backend_fallback = "CUDA not built into this libchudware (CHUDWARE_WITH_CUDA=OFF)";
    }
#endif

    // Per-phase record (prof/phases.h): init / loop (+ accumulated convergence sampling)
    // / output. Opened after arg validation so trivially-rejected calls don't pollute it.
    chudware::prof::phases::StudyScope _study("lbm");
    const auto t_init0 = std::chrono::steady_clock::now();

    // Stride along q for the (Nx,Ny,Nz,19) C-order layout.
    const std::size_t sz_q = static_cast<std::size_t>(NQ);
    // cell index helper
    auto cidx = [ny, nz](int x, int y, int z) -> std::size_t {
        return (static_cast<std::size_t>(x) * ny + y) * nz + z;
    };
    auto fidx = [&](int x, int y, int z, int q) -> std::size_t {
        return cidx(x, y, z) * sz_q + q;
    };

    const float u_lat = static_cast<float>(p.u_lat);

    // -- Distribution buffers (float32). f + scratch for streaming. (P3: the old feq
    // buffer is gone — collision computes equilibrium per cell in registers, saving a
    // full ncell*19 float write+read round-trip to memory every step.) --
    std::vector<float> f(ncell * NQ, 0.0f);
    std::vector<float> fstream(ncell * NQ, 0.0f);

    // The benchmark preference may lower the governor budget, never bypass the
    // CHUDWARE_SIM_THREADS cap. Nested shared-pool solves stay on their worker.
    const int lbm_threads = [&]() -> int {
        if (ncell < 64 * 1024 || chudware::pool::shared().in_worker()) return 1;
        const uint64_t est = static_cast<uint64_t>(ncell) * 256ull;
        const auto plan = chudware::governor::plan_for(est, "lbm", 0);
        int n = std::max(1, std::min(plan.workers, 8));
        if (const char* e = std::getenv("CHUDWARE_LBM_THREADS")) {
            const int requested = std::atoi(e);
            if (requested >= 1) n = std::min(n, requested);
        }
#ifndef CHUDWARE_HAVE_OPENMP
        n = std::min(n, chudware::pool::shared().max_workers());
#endif
        return n;
    }();
    // macro fields
    std::vector<float> rho(ncell, 1.0f), ux(ncell, u_lat), uy(ncell, 0.0f), uz(ncell, 0.0f);

    // ---- _feq_fast(rho,ux,uy,uz) -> out (float32) -------------------------------
    // f_i^eq = w_i * rho * (1 + e.u/cs2 + (e.u)^2/(2*cs2^2) - u^2/(2*cs2))
    // Matches the oracle's float32 expression ordering element-wise.
    auto feq_compute = [&](const std::vector<float>& Rho, const std::vector<float>& Ux,
                           const std::vector<float>& Uy, const std::vector<float>& Uz,
                           std::vector<float>& out) {
        for (std::size_t c = 0; c < ncell; ++c) {
            const float rx = Ux[c], ry = Uy[c], rz = Uz[c];
            const float u2 = rx * rx + ry * ry + rz * rz;  // (ux**2+uy**2+uz**2)
            const float rh = Rho[c];
            const std::size_t base = c * sz_q;
            for (int q = 0; q < NQ; ++q) {
                // eu2 = ux*ex + uy*ey + uz*ez (float32 accumulation, x then y then z)
                float eu = rx * static_cast<float>(EQ[q][0]);
                eu += ry * static_cast<float>(EQ[q][1]);
                eu += rz * static_cast<float>(EQ[q][2]);
                // 1 + eu/cs2 + eu^2/(2*cs2*cs2) - u2/(2*cs2)
                float term = 1.0f + eu / CS2 + (eu * eu) / (2.0f * CS2 * CS2) -
                             u2 / (2.0f * CS2);
                out[base + q] = W[q] * rh * term;
            }
        }
    };

    // ---- _macros(f) -> rho,ux,uy,uz ---------------------------------------------
    // rho = sum_q f_q ; rho_safe = rho>1e-10 ? rho : 1 ; u = sum_q f_q*e_q / rho_safe.
    // Sequential float32 reduction over q (numpy naive sum for 19<128).
    auto macros = [&](const std::vector<float>& F) {
        lattice_for(ncell, lbm_threads, [&](std::size_t c) {
            float prod[NQ];
            const std::size_t base = c * sz_q;
            // rho = f.sum(axis=-1) — numpy pairwise_sum over the 19 contiguous f.
            const float sr = pairwise_sum19(&F[base]);
            // ux/uy/uz: numpy forms (f*_EQ[:,k]) (a 19-vector) THEN .sum(axis=-1),
            // so the reduction is the SAME pairwise_sum over the product vector.
            for (int q = 0; q < NQ; ++q) prod[q] = F[base + q] * static_cast<float>(EQ[q][0]);
            const float sx = pairwise_sum19(prod);
            for (int q = 0; q < NQ; ++q) prod[q] = F[base + q] * static_cast<float>(EQ[q][1]);
            const float sy = pairwise_sum19(prod);
            for (int q = 0; q < NQ; ++q) prod[q] = F[base + q] * static_cast<float>(EQ[q][2]);
            const float sz = pairwise_sum19(prod);
            const float rho_safe = (sr > 1e-10f) ? sr : 1.0f;
            rho[c] = sr;
            ux[c] = sx / rho_safe;
            uy[c] = sy / rho_safe;
            uz[c] = sz / rho_safe;
        });
    };

    // ---- _stream(f): g[...,q] = np.roll(f[...,q], shift=(ex,ey,ez), axis=(0,1,2))
    // np.roll positive shift moves data toward higher index with wrap:
    //   g[x,y,z,q] = f[(x-ex) mod Nx, (y-ey) mod Ny, (z-ez) mod Nz, q].
    // Destination-cell gather writes a contiguous 19-population row, rather than
    // striding the entire AoS lattice nineteen times. Reads and wrap semantics are
    // unchanged; X slabs keep the innermost Z rows contiguous.
    auto stream = [&](const std::vector<float>& F, std::vector<float>& G) {
        lattice_for(static_cast<std::size_t>(nx), lbm_threads, [&](std::size_t xx) {
            const int x = static_cast<int>(xx);
            int sx[NQ];
            for (int q = 0; q < NQ; ++q) {
                sx[q] = x - EQ[q][0];
                if (sx[q] < 0) sx[q] += nx;
                else if (sx[q] >= nx) sx[q] -= nx;
            }
            for (int y = 0; y < ny; ++y) {
                int sy[NQ];
                for (int q = 0; q < NQ; ++q) {
                    sy[q] = y - EQ[q][1];
                    if (sy[q] < 0) sy[q] += ny;
                    else if (sy[q] >= ny) sy[q] -= ny;
                }
                for (int z = 0; z < nz; ++z) {
                    const std::size_t base = fidx(x, y, z, 0);
                    for (int q = 0; q < NQ; ++q) {
                        int sz = z - EQ[q][2];
                        if (sz < 0) sz += nz;
                        else if (sz >= nz) sz -= nz;
                        G[base + q] = F[fidx(sx[q], sy[q], sz, q)];
                    }
                }
            }
        });
    };

    // ---- initialise: f = feq(1, u_lat, 0, 0); zero solid cells --------------------
    feq_compute(rho, ux, uy, uz, f);
    for (std::size_t c = 0; c < ncell; ++c) {
        if (solid[c]) {
            const std::size_t base = c * sz_q;
            for (int q = 0; q < NQ; ++q) f[base + q] = 0.0f;
        }
    }

    // ---- _build_boundary_links: fluid cells with a solid neighbour in dir q -------
    // Per q, fluid_boundary[c] = solid(shift -e) & ~solid(c)  (matches the np.roll).
    // np.roll(solid, shift=(-ex,-ey,-ez)) at (x,y,z) reads solid[(x+ex)%Nx, ...].
    // We store, per direction q, the list of fluid cell indices on the boundary.
    struct Link {
        int q;
        std::vector<std::size_t> cells;  // fluid cell indices
        std::vector<std::size_t> nbs;    // matching solid neighbour indices (P3 bounce-back)
    };
    std::vector<Link> links;
    for (int q = 1; q < NQ; ++q) {
        const int ex = EQ[q][0], ey = EQ[q][1], ez = EQ[q][2];
        Link lk;
        lk.q = q;
        // numpy np.where order is C-order over (x,y,z); preserve it.
        for (int x = 0; x < nx; ++x) {
            int nbx = (x + ex) % nx;
            if (nbx < 0) nbx += nx;
            for (int y = 0; y < ny; ++y) {
                int nby = (y + ey) % ny;
                if (nby < 0) nby += ny;
                for (int z = 0; z < nz; ++z) {
                    int nbz = (z + ez) % nz;
                    if (nbz < 0) nbz += nz;
                    const std::size_t c = cidx(x, y, z);
                    const std::size_t nb = cidx(nbx, nby, nbz);
                    if (solid[nb] && !solid[c]) {
                        lk.cells.push_back(c);
                        lk.nbs.push_back(nb);
                    }
                }
            }
        }
        if (!lk.cells.empty()) links.push_back(std::move(lk));
    }

    // P3: solid-cell list, computed once — the per-step "zero solid cells" pass walks
    // just these instead of scanning the whole lattice.
    std::vector<std::size_t> solid_cells;
    for (std::size_t c = 0; c < ncell; ++c)
        if (solid[c]) solid_cells.push_back(c);

    // For each boundary link: contrib = sum_over_cells(f[solid_destination,q]) * 2.0
    // (float64 sum of float32 f values per the oracle's np.sum -> np.float64). Resolve
    // all three lattice axes so public flow results do not fabricate a zero side force.
    std::vector<float> gather;  // scratch for the gathered f_q values per link
    std::size_t max_link_cells = 0;
    for (const Link& lk : links) max_link_cells = std::max(max_link_cells, lk.cells.size());
    gather.reserve(max_link_cells);
    auto momentum_exchange = [&](const std::vector<float>& F, double& drag_lat,
                                 double& lift_lat, double& side_lat) {
        // The oracle accumulates into np.float64 force components. For each link:
        //   contrib = float(np.sum(f_q)) * 2.0  where f_q is the gathered float32
        // array f[solid_destinations, q]; np.sum of a float32 array returns float32
        // (numpy's pairwise_sum) which is THEN promoted to python float64 by float(...).
        drag_lat = 0.0;
        lift_lat = 0.0;
        side_lat = 0.0;
        for (const Link& lk : links) {
            const int q = lk.q;
            gather.resize(lk.cells.size());
            for (std::size_t i = 0; i < lk.cells.size(); ++i)
                gather[i] = F[lk.nbs[i] * sz_q + q];
            const float s32 = pairwise_sum_f32(gather.data(), gather.size());
            const double contrib = static_cast<double>(s32) * 2.0;
            drag_lat += contrib * static_cast<double>(EQ[q][0]);
            lift_lat += contrib * static_cast<double>(EQ[q][1]);
            side_lat += contrib * static_cast<double>(EQ[q][2]);
        }
    };

    // tau handling — the oracle keeps a python-float tau_current (no watchdog here
    // because the gate runs a fixed small N below divergence). nu0 uses python float
    // (double) tau0; smag uses float32.
    const double tau_current = p.tau0;
    const bool use_smag = true;  // LES is needed most when molecular tau is close to 0.5.
    const float omega_s = static_cast<float>(1.0 / tau_current);  // float32(1.0/tau)
    // _smag_tau internals are ALL float32 (oracle: _CS2 is float32, so nu0 is float32;
    // smag = (_CS_SMAG**2 * sqrt(float32(2.0))) * pi_mag is float32). nu0 = _CS2 *
    // (tau0-0.5): the python (tau0-0.5) is folded to float32 by the float32 multiply.
    const float nu0 = CS2 * static_cast<float>(tau_current - 0.5);
    // (_CS_SMAG**2) * np.sqrt(np.float32(2.0)) constant-folds to this float32 value.
    const float SMAG_C = 0.04087077f;

    // Inlet equilibrium row (P3): feq(rho=1, ux=u_lat, uy=uz=0) — identical arithmetic
    // to the old per-step inner loop, evaluated once since rho/u never change.
    float inlet_feq[NQ];
    {
        const float u2 = u_lat * u_lat;  // uy=uz=0
        for (int q = 0; q < NQ; ++q) {
            float eu = u_lat * static_cast<float>(EQ[q][0]);  // uy=uz=0 -> only x term
            float term = 1.0f + eu / CS2 + (eu * eu) / (2.0f * CS2 * CS2) -
                         u2 / (2.0f * CS2);
            inlet_feq[q] = W[q] * 1.0f * term;
        }
    }

    // Match the production Python solver's steady-force estimator: sample every ten
    // steps before bounce-back, retain the final fifth of the run (at least 50 steps),
    // and measure drift between the two half-window means. Raw coefficient of variation
    // incorrectly rejects a physically settled periodic wake.
    std::vector<std::array<double, 3>> force_hist;
    const int convergence_window = std::max(50, n_steps / 5);
    const int force_window_samples = std::max(5, (convergence_window + 9) / 10);
    force_hist.reserve(static_cast<std::size_t>(force_window_samples) + 1);
    std::array<double, 3> mean_force_lat{0.0, 0.0, 0.0};
    double force_drift = std::numeric_limits<double>::infinity();

    bool converged_in_loop = false;
    const auto t_loop0 = std::chrono::steady_clock::now();
    chudware::prof::phases::add("init", std::chrono::duration<double, std::milli>(
                                         t_loop0 - t_init0).count());
    double convergence_ms = 0.0;  // accumulated checkpoint-sampling time inside the loop
    auto last_progress = std::chrono::steady_clock::now();
    if (on_progress) on_progress(0, n_steps);

    int step = 0;
    for (step = 1; step <= n_steps; ++step) {
        // --- Collision (BGK + Smagorinsky), FUSED (P3) ---
        // macros -> feq -> collide used to be three full-lattice passes through memory
        // (with a persistent ncell*19 feq buffer in the middle). Each cell's update is
        // independent and uses only its own 19 distributions, so the whole thing fuses
        // into ONE pass with the equilibrium held in registers. Per cell the arithmetic
        // (pairwise sums, float32 expression order) is exactly the old sequence, so the
        // lattice walk stays bit-identical to the oracle; cells write disjoint slots, so
        // the OpenMP split changes nothing.
        lattice_for(ncell, lbm_threads, [&](std::size_t c) {
            const std::size_t base = c * sz_q;
            // _macros for this cell (same pairwise reductions as the macros() lambda).
            float prod[NQ];
            const float sr = pairwise_sum19(&f[base]);
            for (int q = 0; q < NQ; ++q) prod[q] = f[base + q] * static_cast<float>(EQ[q][0]);
            const float sx = pairwise_sum19(prod);
            for (int q = 0; q < NQ; ++q) prod[q] = f[base + q] * static_cast<float>(EQ[q][1]);
            const float sy = pairwise_sum19(prod);
            for (int q = 0; q < NQ; ++q) prod[q] = f[base + q] * static_cast<float>(EQ[q][2]);
            const float szv = pairwise_sum19(prod);
            const float rho_safe = (sr > 1e-10f) ? sr : 1.0f;
            const float cux = sx / rho_safe, cuy = sy / rho_safe, cuz = szv / rho_safe;
            // _feq_fast for this cell, in registers.
            float feq_c[NQ];
            const float u2 = cux * cux + cuy * cuy + cuz * cuz;
            for (int q = 0; q < NQ; ++q) {
                float eu = cux * static_cast<float>(EQ[q][0]);
                eu += cuy * static_cast<float>(EQ[q][1]);
                eu += cuz * static_cast<float>(EQ[q][2]);
                float term = 1.0f + eu / CS2 + (eu * eu) / (2.0f * CS2 * CS2) -
                             u2 / (2.0f * CS2);
                feq_c[q] = W[q] * sr * term;
            }
            if (use_smag) {
                // _smag_tau: pi_mag = sqrt(sum_q (f-feq)^2); nu_eff = nu0 + Cs^2*sqrt2*pi;
                // tau_eff retains the physical molecular floor tau0; only the upper stability
                // bound is clipped. Raising the floor to 0.51 silently changed Reynolds number.
                float sq[NQ];
                for (int q = 0; q < NQ; ++q) {
                    const float fn = f[base + q] - feq_c[q];
                    sq[q] = fn * fn;
                }
                const float pi_mag = std::sqrt(pairwise_sum19(sq));
                const float smag = SMAG_C * pi_mag;
                const float nu_eff = nu0 + smag;  // float32
                float tau_eff = nu_eff / CS2 + 0.5f;
                if (tau_eff < static_cast<float>(tau_current))
                    tau_eff = static_cast<float>(tau_current);
                if (tau_eff > 2.0f) tau_eff = 2.0f;
                const float omega = 1.0f / tau_eff;  // float32(1.0)/tau_arr
                for (int q = 0; q < NQ; ++q)
                    f[base + q] = f[base + q] - omega * (f[base + q] - feq_c[q]);
            } else {
                for (int q = 0; q < NQ; ++q)
                    f[base + q] = f[base + q] - omega_s * (f[base + q] - feq_c[q]);
            }
        });
        // f.astype(np.float32) — already float32; no-op.

        // --- Streaming ---
        stream(f, fstream);
        f.swap(fstream);

        // --- Boundary conditions ---
        // Inlet (x=0): equilibrium injection feq(rho=1, ux=u_lat, uy=uz=0). The 19
        // values are CONSTANT for the whole run (fixed rho/u), so they're computed once
        // outside the loop (inlet_feq below) and just copied here.
        {
            for (int y = 0; y < ny; ++y) {
                for (int z = 0; z < nz; ++z) {
                    const std::size_t base = fidx(0, y, z, 0);
                    for (int q = 0; q < NQ; ++q) f[base + q] = inlet_feq[q];
                }
            }
        }

        // Outlet (x=Nx-1): zero-gradient, f[-1] = f[-2].
        if (nx >= 2) {
            for (int y = 0; y < ny; ++y) {
                for (int z = 0; z < nz; ++z) {
                    const std::size_t dst = fidx(nx - 1, y, z, 0);
                    const std::size_t src = fidx(nx - 2, y, z, 0);
                    for (int q = 0; q < NQ; ++q) f[dst + q] = f[src + q];
                }
            }
        }

        // Lateral slip: specularly reflect only populations whose normal velocity
        // points into the domain. A zero-gradient plane copy is not a slip wall: it
        // imports the adjacent plane's normal momentum and permits lateral mass flux.
        // Order matters at edges (Y faces, then Z faces).
        static constexpr int REFLECT_Y[NQ] = {
            0, 1, 2, 4, 3, 5, 6, 9, 10, 7, 8, 11, 12, 13, 14, 18, 17, 16, 15};
        static constexpr int REFLECT_Z[NQ] = {
            0, 1, 2, 3, 4, 6, 5, 7, 8, 9, 10, 13, 14, 11, 12, 17, 18, 15, 16};
        for (int x = 0; x < nx; ++x) {
            for (int z = 0; z < nz; ++z) {
                const std::size_t low = fidx(x, 0, z, 0);
                const std::size_t high = fidx(x, ny - 1, z, 0);
                for (int q = 0; q < NQ; ++q) {
                    if (EQ[q][1] > 0) f[low + q] = f[low + REFLECT_Y[q]];
                    if (EQ[q][1] < 0) f[high + q] = f[high + REFLECT_Y[q]];
                }
            }
        }
        for (int x = 0; x < nx; ++x) {
            for (int y = 0; y < ny; ++y) {
                const std::size_t low = fidx(x, y, 0, 0);
                const std::size_t high = fidx(x, y, nz - 1, 0);
                for (int q = 0; q < NQ; ++q) {
                    if (EQ[q][2] > 0) f[low + q] = f[low + REFLECT_Z[q]];
                    if (EQ[q][2] < 0) f[high + q] = f[high + REFLECT_Z[q]];
                }
            }
        }

        // Sample momentum exchange BEFORE bounce-back mutates the fluid-side population.
        // The post-bounce f[fluid,q] belongs to an unrelated upstream population and adds
        // a free-stream bias to both the force and its convergence history.
        const bool sampled_force = step % 10 == 0 || step == n_steps;
        if (sampled_force) {
            const auto t_cv0 = std::chrono::steady_clock::now();
            double d = 0.0, l = 0.0, s = 0.0;
            momentum_exchange(f, d, l, s);
            force_hist.push_back({d, l, s});
            if (static_cast<int>(force_hist.size()) > force_window_samples)
                force_hist.erase(force_hist.begin());
            mean_force_lat = {0.0, 0.0, 0.0};
            for (const auto& sample : force_hist)
                for (int c = 0; c < 3; ++c) mean_force_lat[c] += sample[c];
            for (double& component : mean_force_lat)
                component /= static_cast<double>(force_hist.size());
            const std::size_t half = force_hist.size() / 2;
            if (half >= 2) {
                double early_drag = 0.0, late_drag = 0.0;
                for (std::size_t i = 0; i < half; ++i) {
                    early_drag += force_hist[i][0];
                    late_drag += force_hist[force_hist.size() - half + i][0];
                }
                early_drag /= static_cast<double>(half);
                late_drag /= static_cast<double>(half);
                force_drift = std::abs(late_drag - early_drag) /
                              std::max(std::abs(mean_force_lat[0]), 1e-30);
            }
            convergence_ms += std::chrono::duration<double, std::milli>(
                                  std::chrono::steady_clock::now() - t_cv0).count();
            // Progress is independent of force sampling, below.
            if (should_cancel && should_cancel()) {
                R.ok = false;
                R.steps_run = step;
                R.error = "cancelled by sim_cancel at step " + std::to_string(step) +
                          " of " + std::to_string(n_steps);
                return R;
            }
            if (chudware::governor::low_memory_now()) {
                R.ok = false;
                R.steps_run = step;
                R.error = "CFD run stopped at step " + std::to_string(step) +
                          ": host memory critically low (governor) — close other "
                          "applications and re-run";
                return R;
            }
        }

        // Solid half-way bounce-back over the PRECOMPUTED boundary links (P3).
        // The old per-q full-lattice scan (18 sweeps of every cell, every step) touched
        // O(18·ncell) cells to find an O(boundary) set that never changes — the links
        // list (built once above) walks exactly the boundary pairs instead.
        //
        // Ordering note: every WRITE lands on a FLUID cell's opp(q) slot and every READ
        // comes from a SOLID neighbour's q slot. The write set (fluid cells) and read
        // set (solid cells) are disjoint, so the 18 q-passes never observe each other's
        // writes at all — the oracle's in-place sequential semantics and this link walk
        // (and its parallel split) are identical by construction. Within one q the
        // (c, opp(q)) targets are unique, so threads write disjoint slots.
        for (const Link& lk : links) {
            const int q = lk.q;
            const int oq = OPP[q];
            const std::int64_t n_lk = static_cast<std::int64_t>(lk.cells.size());
#if defined(CHUDWARE_HAVE_OPENMP)
#pragma omp parallel for schedule(static) num_threads(lbm_threads) \
    if (lbm_threads > 1 && n_lk > 4096)
#endif
            for (std::int64_t i = 0; i < n_lk; ++i)
                f[lk.cells[i] * sz_q + oq] = f[lk.nbs[i] * sz_q + q];
        }

        // Zero solid cells (precomputed list — O(solid), not O(ncell)).
        {
            const std::int64_t n_sc = static_cast<std::int64_t>(solid_cells.size());
#if defined(CHUDWARE_HAVE_OPENMP)
#pragma omp parallel for schedule(static) num_threads(lbm_threads) \
    if (lbm_threads > 1 && n_sc > 4096)
#endif
            for (std::int64_t i = 0; i < n_sc; ++i) {
                const std::size_t base = solid_cells[i] * sz_q;
                for (int q = 0; q < NQ; ++q) f[base + q] = 0.0f;
            }
        }

        const auto now = std::chrono::steady_clock::now();
        if (on_progress && (step == n_steps ||
            std::chrono::duration<double>(now - last_progress).count() >= 1.0)) {
            on_progress(step, n_steps);
            last_progress = now;
        }
        // Match the production solver's early-stop contract.  Once the force-window
        // drift settles, verify continuity on the current post-bounce field; continuing
        // far past this point can move a periodic wake into a different lattice phase
        // and falsely report a previously converged run as unconverged.
        if (sampled_force &&
            static_cast<int>(force_hist.size()) >= force_window_samples &&
            step > n_steps / 5 && force_drift < 0.01 &&
            (links.empty() || mean_force_lat[0] > 0.0)) {
            macros(f);
            double inlet = 0.0;
            double outlet = 0.0;
            for (int y = 0; y < ny; ++y) {
                for (int z = 0; z < nz; ++z) {
                    const std::size_t ci = cidx(0, y, z);
                    const std::size_t co = cidx(nx - 1, y, z);
                    if (!solid[ci]) inlet += static_cast<double>(rho[ci]) * ux[ci];
                    if (!solid[co]) outlet += static_cast<double>(rho[co]) * ux[co];
                }
            }
            const double scale =
                std::max({std::abs(inlet), std::abs(outlet), 1e-30});
            const double continuity = std::abs(inlet - outlet) / scale;
            if (std::isfinite(continuity) && continuity < 0.02) {
                converged_in_loop = true;
                break;
            }
        }

    }
    if (step > n_steps) step = n_steps;
    chudware::prof::phases::add("loop", std::chrono::duration<double, std::milli>(
                                         std::chrono::steady_clock::now() - t_loop0).count(),
                             static_cast<unsigned long long>(n_steps > 0 ? n_steps : 0));
    chudware::prof::phases::add("convergence", convergence_ms,
                             static_cast<unsigned long long>(force_hist.size()));
    if (!std::all_of(f.begin(), f.end(), [](float v) {
            return std::isfinite(v) && std::abs(v) <= 1.0e6f;
        })) {
        R.ok = false;
        R.diverged = true;
        R.residual = 1.0;
        R.error = "lbm_run: non-finite or unbounded lattice population";
        return R;
    }
    // Post-processing to function exit ("output"): final macros + drag/Cd + residual.
    chudware::prof::phases::PhaseScope _p_output("output");

    // ---- Post-processing: final macros + drag/Cd --------------------------------
    macros(f);
    // Keep fields local until the final diagnostics finish, then transfer ownership.
    bool finite_fields = true;
    for (std::size_t c = 0; c < ncell; ++c) {
        if (!std::isfinite(rho[c]) || !std::isfinite(ux[c]) ||
            !std::isfinite(uy[c]) || !std::isfinite(uz[c]) ||
            (!solid[c] && rho[c] <= 0.0f)) {
            finite_fields = false;
            break;
        }
    }
    double inlet_flux = 0.0;
    double outlet_flux = 0.0;
    for (int y = 0; y < ny; ++y) {
        for (int z = 0; z < nz; ++z) {
            const std::size_t ci = cidx(0, y, z);
            const std::size_t co = cidx(nx - 1, y, z);
            if (!solid[ci]) inlet_flux += static_cast<double>(rho[ci]) * ux[ci];
            if (!solid[co]) outlet_flux += static_cast<double>(rho[co]) * ux[co];
        }
    }
    const double flux_scale = std::max({std::abs(inlet_flux), std::abs(outlet_flux), 1e-30});
    R.mass_imbalance = std::abs(inlet_flux - outlet_flux) / flux_scale;

    const double drag_lat = mean_force_lat[0];
    const double lift_lat = mean_force_lat[1];
    const double side_lat = mean_force_lat[2];
    // Lattice force -> SI force. The momentum-exchange sum is a force in lattice units (momentum
    // per lattice timestep, dt_lat=1), so the conversion is the LBM force factor
    //   C_F = rho * dx^4 / dt^2     (mass=rho*dx^3, accel=dx/dt^2).
    // The old code used rho*dx^3/dt, short by dx/dt = speed/u_lat — which turned the physical V^2
    // drag into the observed V^1 (and Cd ~ 1/V). With dt=u_lat*dx/speed this now yields the
    // correct drag = Cd*0.5*rho*V^2*A scaling. (lbm.py force_factor matches this.)
    const double force_factor = p.rho_air * std::pow(p.dx_m, 4.0) / (p.dt_s * p.dt_s);
    R.drag_n = drag_lat * force_factor;
    R.lift_n = lift_lat * force_factor;
    R.side_n = side_lat * force_factor;

    // Frontal area: solid.any(axis=0) over (Ny,Nz) * voxel_mm^2.
    double frontal_cells = 0.0;
    for (int y = 0; y < ny; ++y) {
        for (int z = 0; z < nz; ++z) {
            bool any = false;
            for (int x = 0; x < nx && !any; ++x) {
                if (solid[cidx(x, y, z)]) any = true;
            }
            if (any) frontal_cells += 1.0;
        }
    }
    R.frontal_area_mm2 = frontal_cells * (p.voxel_mm * p.voxel_mm);

    // Cd = drag_n / (0.5 * rho_air * speed^2 * frontal_area_m2). The oracle uses the
    // PHYSICAL speed; here vel_scale = speed/u_lat, but Cd cancels speed via the force
    // factor only through dt. The gate passes the physical pieces needed, so we compute
    // Cd with the same dynamic-pressure denominator the oracle uses. speed_mps is
    // recoverable as dx_m * u_lat / dt_s (since dt = u_lat*dx/speed -> speed=u_lat*dx/dt).
    const double speed_mps = (p.dt_s > 0.0) ? (u_lat * p.dx_m / p.dt_s) : 0.0;
    const double frontal_area_m2 = R.frontal_area_mm2 * 1e-6;
    const double dyn_pressure = 0.5 * p.rho_air * speed_mps * speed_mps;
    R.cd = (frontal_area_m2 > 0.0 && dyn_pressure > 0.0)
               ? R.drag_n / (dyn_pressure * frontal_area_m2)
               : 0.0;

    R.steps_run = step;
    R.diverged = false;

    // A voxel boundary produces a small lattice-phase oscillation. Treat the force as
    // settled when consecutive half-window MEANS stop drifting, rather than requiring
    // every instantaneous sample in a periodic wake to be nearly identical.
    const bool force_convergence_available =
        static_cast<int>(force_hist.size()) >= force_window_samples &&
        std::isfinite(force_drift);
    if (!finite_fields || !std::isfinite(R.drag_n) || !std::isfinite(R.lift_n) ||
        !std::isfinite(R.side_n) || !std::isfinite(R.cd) ||
        !std::isfinite(R.mass_imbalance)) {
        R.diverged = true;
        R.residual = 1.0;
        R.converged = false;
        R.error = "lbm_run: non-finite/invalid final flow field";
    } else {
        // A short diagnostic run can finish before it has enough samples to judge
        // force convergence. That is a valid, finite result, just not a converged one.
        R.residual = force_convergence_available ? force_drift : 1.0;
        R.converged =
            converged_in_loop ||
            (force_convergence_available && R.residual < 0.01 &&
             R.mass_imbalance < 0.02);
    }

    R.ok = !R.diverged;
    R.rho = std::move(rho);
    R.ux = std::move(ux);
    R.uy = std::move(uy);
    R.uz = std::move(uz);
    if (R.ok && on_progress && step < n_steps) on_progress(n_steps, n_steps);
    return R;
    } catch (const std::bad_alloc&) {
        R.ok = false;
        R.error = "lbm_run: allocation failed for requested lattice";
        return R;
    } catch (const std::exception& e) {
        R.ok = false;
        R.error = std::string("lbm_run: solver failed: ") + e.what();
        return R;
    }
}

bool lbm_cuda_available() {
#ifdef CHUDWARE_WITH_CUDA
    return lbm_cuda_device_available();
#else
    return false;
#endif
}

}  // namespace chudware::sim
