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

#ifdef AICAD_WITH_CUDA
#include "sim/lbm_cuda.h"
#endif

#include "util/resource_governor.h"
#include "util/thread_pool.h"
#include "prof/phases.h"  // per-phase timers (StudyScope "lbm")

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <thread>
#ifdef AICAD_HAVE_OPENMP
#include <omp.h>
#endif

namespace aicad::sim {

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

}  // namespace

LbmResult lbm_run(const std::vector<uint8_t>& solid, int nx, int ny, int nz,
                  const LbmParams& p, int n_steps,
                  const std::function<void(int, int)>& on_progress,
                  const std::function<bool()>& should_cancel) {
    LbmResult R;
    R.nx = nx;
    R.ny = ny;
    R.nz = nz;
    if (nx < 1 || ny < 1 || nz < 1) {
        R.error = "lbm_run: grid dims must be >= 1";
        return R;
    }
    const std::size_t ncell = static_cast<std::size_t>(nx) * ny * nz;
    if (solid.size() != ncell) {
        R.error = "lbm_run: solid mask size mismatch";
        return R;
    }

    // Backend select: params.backend, else AI_CAD_LBM_BACKEND, else "auto".
    // "cuda" / "auto" may pick CUDA when built+available; always fall back to CPU.
    std::string want = p.backend;
    if (want.empty()) {
        if (const char* e = std::getenv("AI_CAD_LBM_BACKEND")) want = e;
        else want = "auto";
    }
    for (char& c : want) if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
    R.backend = "cpu";
#ifdef AICAD_WITH_CUDA
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
            // A cancel on the CUDA path is terminal: report it instead of restarting the
            // whole lattice on the CPU fallback.
            if (should_cancel && should_cancel()) return cr;
            R.backend_fallback = why.empty()
                ? (cr.error.empty() ? "CUDA path failed; using CPU" : cr.error)
                : why;
            // fall through to CPU reference
        }
    } else if (want == "cpu") {
        // explicit CPU
    } else {
        R.backend_fallback = "unknown backend '" + want + "', using CPU";
    }
#else
    if (want == "cuda") {
        R.backend_fallback = "CUDA not built into this libaicad (AICAD_WITH_CUDA=OFF)";
    }
#endif

    // Per-phase record (prof/phases.h): init / loop (+ accumulated convergence sampling)
    // / output. Opened after arg validation so trivially-rejected calls don't pollute it.
    aicad::prof::phases::StudyScope _study("lbm");
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

    // P3/F3: worker threads for the per-step lattice loops. Budget comes from the
    // shared governor + pool so nested solves cannot oversubscribe; tiny lattices
    // stay serial (spawn overhead > work). AI_CAD_LBM_THREADS overrides for
    // benchmarking. Every parallel loop below writes DISJOINT indices and computes
    // each cell with the exact serial arithmetic, so results stay bit-identical to
    // the oracle parity gate.
    const int lbm_threads = [&]() -> int {
#ifndef AICAD_HAVE_OPENMP
        return 1;
#else
        if (ncell < 64 * 1024) return 1;
        if (const char* e = std::getenv("AI_CAD_LBM_THREADS")) {
            const int n = std::atoi(e);
            if (n >= 1) return n;
        }
        // Estimate lattice peak (~256 B/cell: f+scratch+macros) so plan_for can
        // grade under memory pressure; ample RAM keeps full allotted cores.
        const uint64_t est = static_cast<uint64_t>(ncell) * 256ull;
        const auto plan = aicad::governor::plan_for(est, "lbm", 0);
        const int n = std::max(1, std::min(plan.workers, 8));
        // Align shared pool with this op's budget (nested solves share one cap).
        aicad::pool::shared().set_max_workers(n);
        return n;
#endif
    }();
    (void)lbm_threads;
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
        float prod[NQ];
        for (std::size_t c = 0; c < ncell; ++c) {
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
        }
    };

    // ---- _stream(f): g[...,q] = np.roll(f[...,q], shift=(ex,ey,ez), axis=(0,1,2))
    // np.roll positive shift moves data toward higher index with wrap:
    //   g[x,y,z,q] = f[(x-ex) mod Nx, (y-ey) mod Ny, (z-ez) mod Nz, q].
    // P3: pure gather — every (q,x,y,z) writes its own slot in G from a snapshot F, so
    // the (q,x) collapse parallelizes with zero interaction (bit-identical copies).
    auto stream = [&](const std::vector<float>& F, std::vector<float>& G) {
#if defined(AICAD_HAVE_OPENMP)
#pragma omp parallel for collapse(2) schedule(static) num_threads(lbm_threads) \
    if (lbm_threads > 1)
#endif
        for (int q = 0; q < NQ; ++q) {
            for (int x = 0; x < nx; ++x) {
                const int ex = EQ[q][0], ey = EQ[q][1], ez = EQ[q][2];
                int sx = x - ex;
                sx %= nx;
                if (sx < 0) sx += nx;
                for (int y = 0; y < ny; ++y) {
                    int sy = y - ey;
                    sy %= ny;
                    if (sy < 0) sy += ny;
                    for (int z = 0; z < nz; ++z) {
                        int sz_ = z - ez;
                        sz_ %= nz;
                        if (sz_ < 0) sz_ += nz;
                        G[fidx(x, y, z, q)] = F[fidx(sx, sy, sz_, q)];
                    }
                }
            }
        }
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

    // ---- momentum-exchange drag/lift (lattice units) ----------------------------
    // For each boundary link: contrib = sum_over_cells(f[cell,q]) * 2.0 (float64 sum
    // of float32 f values per the oracle's np.sum -> np.float64). drag += contrib*ex,
    // lift += contrib*ey.
    std::vector<float> gather;  // scratch for the gathered f_q values per link
    auto momentum_exchange = [&](const std::vector<float>& F, double& drag_lat,
                                 double& lift_lat) {
        // The oracle accumulates into np.float64 drag_lat/lift_lat. For each link:
        //   contrib = float(np.sum(f_q)) * 2.0  where f_q is the gathered float32
        // array f[cells, q]; np.sum of a float32 array returns float32 (numpy's
        // pairwise_sum) which is THEN promoted to python float64 by float(...).
        drag_lat = 0.0;
        lift_lat = 0.0;
        for (const Link& lk : links) {
            const int q = lk.q;
            gather.resize(lk.cells.size());
            for (std::size_t i = 0; i < lk.cells.size(); ++i)
                gather[i] = F[lk.cells[i] * sz_q + q];
            const float s32 = pairwise_sum_f32(gather.data(), gather.size());
            const double contrib = static_cast<double>(s32) * 2.0;
            drag_lat += contrib * static_cast<double>(EQ[q][0]);
            lift_lat += contrib * static_cast<double>(EQ[q][1]);
        }
    };

    // tau handling — the oracle keeps a python-float tau_current (no watchdog here
    // because the gate runs a fixed small N below divergence). nu0 uses python float
    // (double) tau0; smag uses float32.
    const double tau_current = p.tau0;
    const bool use_smag = tau_current > 0.55;
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

    // Convergence sampling: record the drag at ~50 checkpoints through the run so we can report a
    // residual (how settled the force is) WITHOUT an early exit (the full n_steps still run, so the
    // lattice walk stays bit-identical to the oracle for the parity gate).
    std::vector<double> drag_hist;
    const int sample_every = std::max(1, n_steps / 50);

    const auto t_loop0 = std::chrono::steady_clock::now();
    aicad::prof::phases::add("init", std::chrono::duration<double, std::milli>(
                                         t_loop0 - t_init0).count());
    double convergence_ms = 0.0;  // accumulated checkpoint-sampling time inside the loop

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
#if defined(AICAD_HAVE_OPENMP)
#pragma omp parallel for schedule(static) num_threads(lbm_threads) if (lbm_threads > 1)
#endif
        for (std::int64_t ci = 0; ci < static_cast<std::int64_t>(ncell); ++ci) {
            const std::size_t c = static_cast<std::size_t>(ci);
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
                // tau_eff = float32(nu_eff/cs2 + 0.5) clipped [0.51,2.0]; omega=1/tau_eff.
                float sq[NQ];
                for (int q = 0; q < NQ; ++q) {
                    const float fn = f[base + q] - feq_c[q];
                    sq[q] = fn * fn;
                }
                const float pi_mag = std::sqrt(pairwise_sum19(sq));
                const float smag = SMAG_C * pi_mag;
                const float nu_eff = nu0 + smag;  // float32
                float tau_eff = nu_eff / CS2 + 0.5f;
                if (tau_eff < 0.51f) tau_eff = 0.51f;
                if (tau_eff > 2.0f) tau_eff = 2.0f;
                const float omega = 1.0f / tau_eff;  // float32(1.0)/tau_arr
                for (int q = 0; q < NQ; ++q)
                    f[base + q] = f[base + q] - omega * (f[base + q] - feq_c[q]);
            } else {
                for (int q = 0; q < NQ; ++q)
                    f[base + q] = f[base + q] - omega_s * (f[base + q] - feq_c[q]);
            }
        }
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

        // Lateral slip (mirror / zero-gradient copy): y=0<-y=1, y=Ny-1<-y=Ny-2,
        // z=0<-z=1, z=Nz-1<-z=Nz-2. Order matters (oracle does Y faces then Z faces).
        if (ny >= 2) {
            for (int x = 0; x < nx; ++x) {
                for (int z = 0; z < nz; ++z) {
                    const std::size_t d0 = fidx(x, 0, z, 0), s0 = fidx(x, 1, z, 0);
                    const std::size_t d1 = fidx(x, ny - 1, z, 0), s1 = fidx(x, ny - 2, z, 0);
                    for (int q = 0; q < NQ; ++q) {
                        f[d0 + q] = f[s0 + q];
                        f[d1 + q] = f[s1 + q];
                    }
                }
            }
        }
        if (nz >= 2) {
            for (int x = 0; x < nx; ++x) {
                for (int y = 0; y < ny; ++y) {
                    const std::size_t d0 = fidx(x, y, 0, 0), s0 = fidx(x, y, 1, 0);
                    const std::size_t d1 = fidx(x, y, nz - 1, 0), s1 = fidx(x, y, nz - 2, 0);
                    for (int q = 0; q < NQ; ++q) {
                        f[d0 + q] = f[s0 + q];
                        f[d1 + q] = f[s1 + q];
                    }
                }
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
#if defined(AICAD_HAVE_OPENMP)
#pragma omp parallel for schedule(static) num_threads(lbm_threads) \
    if (lbm_threads > 1 && n_lk > 4096)
#endif
            for (std::int64_t i = 0; i < n_lk; ++i)
                f[lk.cells[i] * sz_q + oq] = f[lk.nbs[i] * sz_q + q];
        }

        // Zero solid cells (precomputed list — O(solid), not O(ncell)).
        {
            const std::int64_t n_sc = static_cast<std::int64_t>(solid_cells.size());
#if defined(AICAD_HAVE_OPENMP)
#pragma omp parallel for schedule(static) num_threads(lbm_threads) \
    if (lbm_threads > 1 && n_sc > 4096)
#endif
            for (std::int64_t i = 0; i < n_sc; ++i) {
                const std::size_t base = solid_cells[i] * sz_q;
                for (int q = 0; q < NQ; ++q) f[base + q] = 0.0f;
            }
        }

        // Sample the (lattice) drag at checkpoints for the convergence residual. Read-only — it
        // gathers from f and never writes it, so the solve is unchanged. Timed into the
        // accumulated "convergence" phase (~50 checkpoints/run: two clock reads each = noise).
        if (step % sample_every == 0 || step == n_steps) {
            const auto t_cv0 = std::chrono::steady_clock::now();
            double d = 0.0, l = 0.0;
            momentum_exchange(f, d, l);
            drag_hist.push_back(d);
            convergence_ms += std::chrono::duration<double, std::milli>(
                                  std::chrono::steady_clock::now() - t_cv0).count();
            if (on_progress) on_progress(step, n_steps);  // live progress (read-only)
            // Cooperative cancel (sim_cancel): stop at the checkpoint, reporting the steps
            // actually run. Parity-safe — the gate never passes a cancel hook.
            if (should_cancel && should_cancel()) {
                R.ok = false;
                R.steps_run = step;
                R.error = "cancelled by sim_cancel at step " + std::to_string(step) +
                          " of " + std::to_string(n_steps);
                return R;
            }
            // Emergency stop (governor): the HOST is nearly out of memory — the lattice
            // itself was admitted up-front, so this means some other process is eating
            // RAM. Stop gracefully at this checkpoint instead of letting the OS swap or
            // OOM-kill the app. Never trips in the parity gate (fixed small grids, and
            // AICAD_GOVERNOR=0 disables outright).
            if (aicad::governor::low_memory_now()) {
                R.ok = false;
                R.error = "CFD run stopped at step " + std::to_string(step) +
                          ": host memory critically low (governor) — close other "
                          "applications and re-run";
                return R;
            }
        }
    }
    step = n_steps;  // loop exits with step == n_steps+1; oracle's `step` is last value
    aicad::prof::phases::add("loop", std::chrono::duration<double, std::milli>(
                                         std::chrono::steady_clock::now() - t_loop0).count(),
                             static_cast<unsigned long long>(n_steps > 0 ? n_steps : 0));
    aicad::prof::phases::add("convergence", convergence_ms,
                             static_cast<unsigned long long>(drag_hist.size()));
    // Post-processing to function exit ("output"): final macros + drag/Cd + residual.
    aicad::prof::phases::PhaseScope _p_output("output");

    // ---- Post-processing: final macros + drag/Cd --------------------------------
    macros(f);
    R.rho = rho;
    R.ux = ux;
    R.uy = uy;
    R.uz = uz;

    double drag_lat = 0.0, lift_lat = 0.0;
    momentum_exchange(f, drag_lat, lift_lat);
    // Lattice force -> SI force. The momentum-exchange sum is a force in lattice units (momentum
    // per lattice timestep, dt_lat=1), so the conversion is the LBM force factor
    //   C_F = rho * dx^4 / dt^2     (mass=rho*dx^3, accel=dx/dt^2).
    // The old code used rho*dx^3/dt, short by dx/dt = speed/u_lat — which turned the physical V^2
    // drag into the observed V^1 (and Cd ~ 1/V). With dt=u_lat*dx/speed this now yields the
    // correct drag = Cd*0.5*rho*V^2*A scaling. (lbm.py force_factor matches this.)
    const double force_factor = p.rho_air * std::pow(p.dx_m, 4.0) / (p.dt_s * p.dt_s);
    R.drag_n = drag_lat * force_factor;
    R.lift_n = lift_lat * force_factor;

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

    // Convergence residual: coefficient of variation of the drag over the last fifth of the
    // sampled history. Small => the force has settled; large => still developing (treat the numbers
    // as under-converged). NaN/Inf in the drag => diverged.
    if (!std::isfinite(R.drag_n) || !std::isfinite(R.lift_n)) {
        R.diverged = true;
        R.residual = 1.0;
        R.converged = false;
    } else if (drag_hist.size() >= 4) {
        const std::size_t tail = std::max<std::size_t>(3, drag_hist.size() / 5);
        const std::size_t start = drag_hist.size() - tail;
        double mean = 0.0;
        for (std::size_t i = start; i < drag_hist.size(); ++i) mean += drag_hist[i];
        mean /= static_cast<double>(tail);
        double var = 0.0;
        for (std::size_t i = start; i < drag_hist.size(); ++i)
            var += (drag_hist[i] - mean) * (drag_hist[i] - mean);
        var /= static_cast<double>(tail);
        const double denom = std::abs(mean) > 1e-30 ? std::abs(mean) : 1e-30;
        R.residual = std::sqrt(var) / denom;
        R.converged = R.residual < 0.01;  // <1% variation over the tail
    }

    R.ok = true;
    return R;
}

bool lbm_cuda_available() {
#ifdef AICAD_WITH_CUDA
    return lbm_cuda_device_available();
#else
    return false;
#endif
}

}  // namespace aicad::sim
