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

#include "util/resource_governor.h"

#include <cmath>
#include <cstddef>

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
                  const std::function<void(int, int)>& on_progress) {
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

    // -- Distribution buffers (float32). f, plus scratch for streaming + macros. --
    std::vector<float> f(ncell * NQ, 0.0f);
    std::vector<float> feq(ncell * NQ, 0.0f);
    std::vector<float> fstream(ncell * NQ, 0.0f);
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
    auto stream = [&](const std::vector<float>& F, std::vector<float>& G) {
        for (int q = 0; q < NQ; ++q) {
            const int ex = EQ[q][0], ey = EQ[q][1], ez = EQ[q][2];
            for (int x = 0; x < nx; ++x) {
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
                    if (solid[nb] && !solid[c]) lk.cells.push_back(c);
                }
            }
        }
        if (!lk.cells.empty()) links.push_back(std::move(lk));
    }

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

    // Convergence sampling: record the drag at ~50 checkpoints through the run so we can report a
    // residual (how settled the force is) WITHOUT an early exit (the full n_steps still run, so the
    // lattice walk stays bit-identical to the oracle for the parity gate).
    std::vector<double> drag_hist;
    const int sample_every = std::max(1, n_steps / 50);

    int step = 0;
    for (step = 1; step <= n_steps; ++step) {
        // --- Collision (BGK + Smagorinsky) ---
        macros(f);
        feq_compute(rho, ux, uy, uz, feq);

        if (use_smag) {
            // _smag_tau: pi_mag = sqrt(sum_q (f-feq)^2); nu_eff = nu0 + Cs^2*sqrt2*pi;
            // tau_eff = float32(nu_eff/cs2 + 0.5) clipped [0.51,2.0]; omega=1/tau_eff.
            for (std::size_t c = 0; c < ncell; ++c) {
                const std::size_t base = c * sz_q;
                // pi_mag = sqrt((fneq**2).sum(axis=-1)) — numpy forms the 19-vector
                // fneq**2 then pairwise_sum-reduces it.
                float sq[NQ];
                for (int q = 0; q < NQ; ++q) {
                    const float fn = f[base + q] - feq[base + q];
                    sq[q] = fn * fn;
                }
                const float pi_mag = std::sqrt(pairwise_sum19(sq));
                // smag = SMAG_C * pi_mag (all float32, see SMAG_C above).
                const float smag = SMAG_C * pi_mag;
                const float nu_eff = nu0 + smag;  // float32
                // tau_eff = (nu_eff/_CS2 + float32(0.5)).astype(float32) — all float32.
                float tau_eff = nu_eff / CS2 + 0.5f;
                // np.clip(tau_eff, float32(0.51), float32(2.0))
                if (tau_eff < 0.51f) tau_eff = 0.51f;
                if (tau_eff > 2.0f) tau_eff = 2.0f;
                const float omega = 1.0f / tau_eff;  // float32(1.0)/tau_arr
                for (int q = 0; q < NQ; ++q) {
                    // f = f - omega*(f-feq) ; result .astype(float32) (already float32)
                    f[base + q] = f[base + q] - omega * (f[base + q] - feq[base + q]);
                }
            }
        } else {
            for (std::size_t i = 0; i < f.size(); ++i) {
                f[i] = f[i] - omega_s * (f[i] - feq[i]);
            }
        }
        // f.astype(np.float32) — already float32; no-op.

        // --- Streaming ---
        stream(f, fstream);
        f.swap(fstream);

        // --- Boundary conditions ---
        // Inlet (x=0): equilibrium injection feq(rho=1, ux=u_lat, uy=uz=0).
        // _inlet_bc builds feq over the (Ny,Nz) face and assigns f[0,:,:,:].
        {
            for (int y = 0; y < ny; ++y) {
                for (int z = 0; z < nz; ++z) {
                    const float u2 = u_lat * u_lat;  // uy=uz=0
                    const std::size_t base = fidx(0, y, z, 0);
                    for (int q = 0; q < NQ; ++q) {
                        float eu = u_lat * static_cast<float>(EQ[q][0]);
                        // uy=uz=0 -> only x term
                        float term = 1.0f + eu / CS2 + (eu * eu) / (2.0f * CS2 * CS2) -
                                     u2 / (2.0f * CS2);
                        f[base + q] = W[q] * 1.0f * term;
                    }
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

        // Solid half-way bounce-back.
        // For each q (1..18): solid_shifted[c]=solid[(c+e)] (np.roll shift -e); fluid
        // at boundary = solid_shifted & ~solid. f_solid_q[c]=f[(c+e),q] (roll -e). Set
        // f[c, opp(q)] = f_solid_q[c] = f[(c+e), q] for boundary fluid cells.
        // CRITICAL: the oracle uses the SAME f for reads (roll of current f) and writes;
        // np.roll snapshots f[...,q] BEFORE the per-q writes, and writes go into opp(q)
        // which is a DIFFERENT slice than q, so within one q there is no read/write
        // aliasing — but a later q' could read a slice opp(q'') written earlier. To match
        // numpy EXACTLY we must read from the f as it stands at the start of each q
        // iteration (the roll reads current f[...,q], writes f[...,opp]). Since each q
        // reads slice q and writes slice opp(q), and opp(q) for q in 1..18 maps onto the
        // same set 1..18, a write at q can be read by a later q' iff opp(q)==q'. We thus
        // replicate the in-place sequential semantics directly on f.
        for (int q = 1; q < NQ; ++q) {
            const int ex = EQ[q][0], ey = EQ[q][1], ez = EQ[q][2];
            const int oq = OPP[q];
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
                            f[c * sz_q + oq] = f[nb * sz_q + q];
                        }
                    }
                }
            }
        }

        // Zero solid cells.
        for (std::size_t c = 0; c < ncell; ++c) {
            if (solid[c]) {
                const std::size_t base = c * sz_q;
                for (int q = 0; q < NQ; ++q) f[base + q] = 0.0f;
            }
        }

        // Sample the (lattice) drag at checkpoints for the convergence residual. Read-only — it
        // gathers from f and never writes it, so the solve is unchanged.
        if (step % sample_every == 0 || step == n_steps) {
            double d = 0.0, l = 0.0;
            momentum_exchange(f, d, l);
            drag_hist.push_back(d);
            if (on_progress) on_progress(step, n_steps);  // live progress (read-only)
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

}  // namespace aicad::sim
