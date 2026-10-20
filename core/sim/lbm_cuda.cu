// lbm_cuda.cu — optional CUDA 13 D3Q19 LBM payload (collide/stream/bounce-back/inlet/forces).
// Mirrors the CPU float32 arithmetic in lbm.cpp as closely as practical on GPU.
// Voxelization stays on the host; this TU only owns the lattice loop.
#include "sim/lbm_cuda.h"
#include "prof/phases.h"
#include "util/resource_governor.h"

#include <cuda_runtime.h>
#include <algorithm>
#include <array>

#include <chrono>
#include <cmath>
#include <cstdio>
#include <limits>
#include <cstring>
#include <new>
#include <string>
#include <utility>
#include <vector>

namespace chudware::sim {
namespace {

constexpr int NQ = 19;

__constant__ int d_EQ[NQ][3] = {
    {0, 0, 0}, {1, 0, 0}, {-1, 0, 0}, {0, 1, 0}, {0, -1, 0}, {0, 0, 1}, {0, 0, -1},
    {1, 1, 0}, {-1, -1, 0}, {1, -1, 0}, {-1, 1, 0}, {1, 0, 1}, {-1, 0, -1},
    {1, 0, -1}, {-1, 0, 1}, {0, 1, 1}, {0, -1, -1}, {0, 1, -1}, {0, -1, 1}};
__constant__ float d_W[NQ] = {
    1.0f / 3.0f, 1.0f / 18.0f, 1.0f / 18.0f, 1.0f / 18.0f, 1.0f / 18.0f, 1.0f / 18.0f,
    1.0f / 18.0f, 1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f,
    1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f,
    1.0f / 36.0f};
__constant__ int d_OPP[NQ] = {0, 2, 1, 4, 3, 6, 5, 8, 7, 10, 9, 12, 11, 14, 13, 16, 15, 18, 17};
__constant__ int d_REFLECT_Y[NQ] = {
    0, 1, 2, 4, 3, 5, 6, 9, 10, 7, 8, 11, 12, 13, 14, 18, 17, 16, 15};
__constant__ int d_REFLECT_Z[NQ] = {
    0, 1, 2, 3, 4, 6, 5, 7, 8, 9, 10, 13, 14, 11, 12, 17, 18, 15, 16};
__constant__ float d_CS2 = 1.0f / 3.0f;
__constant__ float d_SMAG_C = 0.04087077f;

__device__ inline float pairwise_sum19(const float v[NQ]) {
    float r0 = v[0], r1 = v[1], r2 = v[2], r3 = v[3];
    float r4 = v[4], r5 = v[5], r6 = v[6], r7 = v[7];
    r0 += v[8]; r1 += v[9]; r2 += v[10]; r3 += v[11];
    r4 += v[12]; r5 += v[13]; r6 += v[14]; r7 += v[15];
    float res = ((r0 + r1) + (r2 + r3)) + ((r4 + r5) + (r6 + r7));
    res += v[16]; res += v[17]; res += v[18];
    return res;
}
float pairwise_sum_f32_host(const float* a, size_t n) {
    if (n == 0) return 0.0f;
    if (n < 8) {
        float r = a[0];
        for (size_t i = 1; i < n; ++i) r += a[i];
        return r;
    }
    if (n <= 128) {
        float r0 = a[0], r1 = a[1], r2 = a[2], r3 = a[3];
        float r4 = a[4], r5 = a[5], r6 = a[6], r7 = a[7];
        size_t i = 8;
        for (; i + 8 <= n; i += 8) {
            r0 += a[i]; r1 += a[i + 1]; r2 += a[i + 2]; r3 += a[i + 3];
            r4 += a[i + 4]; r5 += a[i + 5]; r6 += a[i + 6]; r7 += a[i + 7];
        }
        float result = ((r0 + r1) + (r2 + r3)) + ((r4 + r5) + (r6 + r7));
        for (; i < n; ++i) result += a[i];
        return result;
    }
    size_t half = n / 2;
    half -= half % 8;
    return pairwise_sum_f32_host(a, half) + pairwise_sum_f32_host(a + half, n - half);
}

__device__ inline size_t cidx(int x, int y, int z, int ny, int nz) {
    return (static_cast<size_t>(x) * ny + y) * nz + z;
}

__global__ void initialize_kernel(float* f, const uint8_t* solid, const float* inlet,
                                  int ncell) {
    const size_t c = static_cast<size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
    if (c >= static_cast<size_t>(ncell)) return;
    for (int q = 0; q < NQ; ++q) f[c * NQ + q] = solid[c] ? 0.0f : inlet[q];
}

// Slots are grouped by q, then by the CPU's C-order fluid-boundary traversal.
// Keep the float32 pairwise reduction on the host, not an unordered device sum.
__global__ void gather_links_kernel(const float* f, const uint64_t* slots, float* values,
                                    size_t count) {
    const size_t i = static_cast<size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
    if (i < count) values[i] = f[slots[i]];
}

__global__ void collide_kernel(float* f, const uint8_t* solid, int nx, int ny, int nz,
                               float omega_s, float tau_floor, float nu0, int use_smag) {
    const int ncell = nx * ny * nz;
    int c = blockIdx.x * blockDim.x + threadIdx.x;
    if (c >= ncell) return;
    if (solid[c]) return;
    const size_t base = static_cast<size_t>(c) * NQ;
    float prod[NQ];
    float loc[NQ];
    for (int q = 0; q < NQ; ++q) loc[q] = f[base + q];
    const float sr = pairwise_sum19(loc);
    for (int q = 0; q < NQ; ++q) prod[q] = loc[q] * static_cast<float>(d_EQ[q][0]);
    const float sx = pairwise_sum19(prod);
    for (int q = 0; q < NQ; ++q) prod[q] = loc[q] * static_cast<float>(d_EQ[q][1]);
    const float sy = pairwise_sum19(prod);
    for (int q = 0; q < NQ; ++q) prod[q] = loc[q] * static_cast<float>(d_EQ[q][2]);
    const float szv = pairwise_sum19(prod);
    const float rho_safe = (sr > 1e-10f) ? sr : 1.0f;
    const float cux = sx / rho_safe, cuy = sy / rho_safe, cuz = szv / rho_safe;
    float feq_c[NQ];
    const float u2 = cux * cux + cuy * cuy + cuz * cuz;
    for (int q = 0; q < NQ; ++q) {
        float eu = cux * static_cast<float>(d_EQ[q][0]);
        eu += cuy * static_cast<float>(d_EQ[q][1]);
        eu += cuz * static_cast<float>(d_EQ[q][2]);
        float term = 1.0f + eu / d_CS2 + (eu * eu) / (2.0f * d_CS2 * d_CS2) - u2 / (2.0f * d_CS2);
        feq_c[q] = d_W[q] * sr * term;
    }
    float omega = omega_s;
    if (use_smag) {
        float sq[NQ];
        for (int q = 0; q < NQ; ++q) {
            const float fn = loc[q] - feq_c[q];
            sq[q] = fn * fn;
        }
        const float pi_mag = sqrtf(pairwise_sum19(sq));
        const float nu_eff = nu0 + d_SMAG_C * pi_mag;
        float tau_eff = nu_eff / d_CS2 + 0.5f;
        if (tau_eff < tau_floor) tau_eff = tau_floor;
        if (tau_eff > 2.0f) tau_eff = 2.0f;
        omega = 1.0f / tau_eff;
    }
    for (int q = 0; q < NQ; ++q)
        f[base + q] = loc[q] - omega * (loc[q] - feq_c[q]);
}

__global__ void stream_kernel(const float* f, float* g, int nx, int ny, int nz) {
    const int ncell = nx * ny * nz;
    int c = blockIdx.x * blockDim.x + threadIdx.x;
    if (c >= ncell) return;
    const int z = c % nz;
    const int y = (c / nz) % ny;
    const int x = c / (ny * nz);
    for (int q = 0; q < NQ; ++q) {
        int sx = x - d_EQ[q][0];
        int sy = y - d_EQ[q][1];
        int sz = z - d_EQ[q][2];
        if (sx < 0) sx += nx; else if (sx >= nx) sx -= nx;
        if (sy < 0) sy += ny; else if (sy >= ny) sy -= ny;
        if (sz < 0) sz += nz; else if (sz >= nz) sz -= nz;
        g[cidx(x, y, z, ny, nz) * NQ + q] = f[cidx(sx, sy, sz, ny, nz) * NQ + q];
    }
}

__global__ void inlet_kernel(float* f, int nx, int ny, int nz, const float* inlet_feq) {
    int y = blockIdx.x * blockDim.x + threadIdx.x;
    int z = blockIdx.y * blockDim.y + threadIdx.y;
    if (y >= ny || z >= nz) return;
    const size_t base = cidx(0, y, z, ny, nz) * NQ;
    for (int q = 0; q < NQ; ++q) f[base + q] = inlet_feq[q];
    (void)nx;
}

__global__ void outlet_kernel(float* f, int nx, int ny, int nz) {
    if (nx < 2) return;
    int y = blockIdx.x * blockDim.x + threadIdx.x;
    int z = blockIdx.y * blockDim.y + threadIdx.y;
    if (y >= ny || z >= nz) return;
    const size_t dst = cidx(nx - 1, y, z, ny, nz) * NQ;
    const size_t src = cidx(nx - 2, y, z, ny, nz) * NQ;
    for (int q = 0; q < NQ; ++q) f[dst + q] = f[src + q];
}

__global__ void slip_y_kernel(float* f, int nx, int ny, int nz) {
    int x = blockIdx.x * blockDim.x + threadIdx.x;
    int z = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= nx || z >= nz) return;
    const size_t low = cidx(x, 0, z, ny, nz) * NQ;
    const size_t high = cidx(x, ny - 1, z, ny, nz) * NQ;
    for (int q = 0; q < NQ; ++q) {
        if (d_EQ[q][1] > 0) f[low + q] = f[low + d_REFLECT_Y[q]];
        if (d_EQ[q][1] < 0) f[high + q] = f[high + d_REFLECT_Y[q]];
    }
}

__global__ void slip_z_kernel(float* f, int nx, int ny, int nz) {
    int x = blockIdx.x * blockDim.x + threadIdx.x;
    int y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= nx || y >= ny) return;
    const size_t low = cidx(x, y, 0, ny, nz) * NQ;
    const size_t high = cidx(x, y, nz - 1, ny, nz) * NQ;
    for (int q = 0; q < NQ; ++q) {
        if (d_EQ[q][2] > 0) f[low + q] = f[low + d_REFLECT_Z[q]];
        if (d_EQ[q][2] < 0) f[high + q] = f[high + d_REFLECT_Z[q]];
    }
}

__global__ void bounce_back_kernel(float* f, const uint8_t* solid, int nx, int ny, int nz) {
    const int ncell = nx * ny * nz;
    int c = blockIdx.x * blockDim.x + threadIdx.x;
    if (c >= ncell || solid[c]) return;
    const int z = c % nz;
    const int y = (c / nz) % ny;
    const int x = c / (ny * nz);
    for (int q = 1; q < NQ; ++q) {
        int nbx = x + d_EQ[q][0];
        int nby = y + d_EQ[q][1];
        int nbz = z + d_EQ[q][2];
        if (nbx < 0) nbx += nx; else if (nbx >= nx) nbx -= nx;
        if (nby < 0) nby += ny; else if (nby >= ny) nby -= ny;
        if (nbz < 0) nbz += nz; else if (nbz >= nz) nbz -= nz;
        const size_t nb = cidx(nbx, nby, nbz, ny, nz);
        if (solid[nb]) {
            f[static_cast<size_t>(c) * NQ + d_OPP[q]] = f[nb * NQ + q];
        }
    }
}

__global__ void zero_solid_kernel(float* f, const uint8_t* solid, int ncell) {
    int c = blockIdx.x * blockDim.x + threadIdx.x;
    if (c >= ncell || !solid[c]) return;
    const size_t base = static_cast<size_t>(c) * NQ;
    for (int q = 0; q < NQ; ++q) f[base + q] = 0.0f;
}

__global__ void macros_kernel(const float* f, float* rho, float* ux, float* uy, float* uz,
                              int ncell, int* invalid_populations = nullptr) {
    int c = blockIdx.x * blockDim.x + threadIdx.x;
    if (c >= ncell) return;
    const size_t base = static_cast<size_t>(c) * NQ;
    float loc[NQ];
    bool invalid = false;
    for (int q = 0; q < NQ; ++q) {
        loc[q] = f[base + q];
        if (invalid_populations && (!isfinite(loc[q]) || fabsf(loc[q]) > 1.0e6f))
            invalid = true;
    }
    if (invalid) atomicExch(invalid_populations, 1);
    float prod[NQ];
    const float sr = pairwise_sum19(loc);
    for (int q = 0; q < NQ; ++q) prod[q] = loc[q] * static_cast<float>(d_EQ[q][0]);
    const float sx = pairwise_sum19(prod);
    for (int q = 0; q < NQ; ++q) prod[q] = loc[q] * static_cast<float>(d_EQ[q][1]);
    const float sy = pairwise_sum19(prod);
    for (int q = 0; q < NQ; ++q) prod[q] = loc[q] * static_cast<float>(d_EQ[q][2]);
    const float sz = pairwise_sum19(prod);
    const float rho_safe = (sr > 1e-10f) ? sr : 1.0f;
    rho[c] = sr;
    ux[c] = sx / rho_safe;
    uy[c] = sy / rho_safe;
    uz[c] = sz / rho_safe;
}


}  // namespace

bool lbm_cuda_device_available() {
    int n = 0;
    if (cudaGetDeviceCount(&n) != cudaSuccess) return false;
    return n > 0;
}

LbmResult lbm_run_cuda(const std::vector<uint8_t>& solid, int nx, int ny, int nz,
                       const LbmParams& p, int n_steps,
                       const std::function<void(int, int)>& on_progress,
                       const std::function<bool()>& should_cancel,
                       std::string& fallback_reason) {
    LbmResult R;
    R.nx = nx;
    R.ny = ny;
    R.nz = nz;
    R.backend = "cuda";
    if (nx < 3 || ny < 3 || nz < 3) {
        R.error = "lbm_run_cuda: grid dims must each be >= 3";
        return R;
    }
    const size_t max_cells = std::numeric_limits<size_t>::max() / NQ;
    if (static_cast<size_t>(nx) > max_cells / static_cast<size_t>(ny) ||
        static_cast<size_t>(nx) * static_cast<size_t>(ny) >
            max_cells / static_cast<size_t>(nz)) {
        R.error = "lbm_run_cuda: grid is too large";
        return R;
    }
    const size_t ncell = static_cast<size_t>(nx) * static_cast<size_t>(ny) *
                         static_cast<size_t>(nz);
    if (ncell > static_cast<size_t>(std::numeric_limits<int>::max())) {
        R.error = "lbm_run_cuda: grid exceeds CUDA kernel index range";
        fallback_reason = R.error;
        return R;
    }
    if (solid.size() != ncell) {
        R.error = "lbm_run_cuda: solid mask size mismatch";
        return R;
    }
    if (n_steps <= 0 || !std::isfinite(p.u_lat) || p.u_lat <= 0.0 || p.u_lat > 0.1 ||
        !std::isfinite(p.tau0) || p.tau0 <= 0.5 || p.tau0 > 2.0 ||
        !std::isfinite(p.voxel_mm) || p.voxel_mm <= 0.0 ||
        !std::isfinite(p.dx_m) || p.dx_m <= 0.0 ||
        !std::isfinite(p.dt_s) || p.dt_s <= 0.0 ||
        !std::isfinite(p.rho_air) || p.rho_air <= 0.0) {
        R.error = "lbm_run_cuda: invalid lattice parameters";
        return R;
    }
    if (std::all_of(solid.begin(), solid.end(), [](uint8_t v) { return v != 0; })) {
        R.error = "lbm_run_cuda: solid mask contains no fluid cells";
        return R;
    }
    if (!lbm_cuda_device_available()) {
        fallback_reason = "no CUDA device";
        R.error = fallback_reason;
        return R;
    }

    // Every return (including allocation/callback exceptions) releases device storage.
    struct DeviceStorage {
        float *f = nullptr, *g = nullptr, *inlet = nullptr;
        float *rho = nullptr, *ux = nullptr, *uy = nullptr, *uz = nullptr;
        float* gather = nullptr;
        uint8_t* solid = nullptr;
        uint64_t* slots = nullptr;
        int* invalid = nullptr;
        ~DeviceStorage() {
            if (f) cudaFree(f);
            if (g) cudaFree(g);
            if (inlet) cudaFree(inlet);
            if (rho) cudaFree(rho);
            if (ux) cudaFree(ux);
            if (uy) cudaFree(uy);
            if (uz) cudaFree(uz);
            if (gather) cudaFree(gather);
            if (solid) cudaFree(solid);
            if (slots) cudaFree(slots);
            if (invalid) cudaFree(invalid);
        }
    } d;
    auto fail = [&](const std::string& msg) {
        fallback_reason = msg;
        R.ok = false;
        R.error = msg;
        return std::move(R);
    };
    try {
    chudware::prof::phases::StudyScope study("lbm_cuda");
    const auto t_init = std::chrono::steady_clock::now();
    const int EQ[NQ][3] = {
        {0, 0, 0}, {1, 0, 0}, {-1, 0, 0}, {0, 1, 0}, {0, -1, 0}, {0, 0, 1}, {0, 0, -1},
        {1, 1, 0}, {-1, -1, 0}, {1, -1, 0}, {-1, 1, 0}, {1, 0, 1}, {-1, 0, -1},
        {1, 0, -1}, {-1, 0, 1}, {0, 1, 1}, {0, -1, -1}, {0, 1, -1}, {0, -1, 1}};
    auto cidx_h = [ny, nz](int x, int y, int z) {
        return (static_cast<size_t>(x) * ny + y) * nz + z;
    };
    std::array<size_t, NQ + 1> offsets{};
    std::vector<uint64_t> slots;
    for (int q = 1; q < NQ; ++q) {
        offsets[q] = slots.size();
        // Exactly the CPU's q, x, y, z order, including periodic neighbours.
        for (int x = 0; x < nx; ++x) {
            int nbx = (x + EQ[q][0]) % nx;
            if (nbx < 0) nbx += nx;
            for (int y = 0; y < ny; ++y) {
                int nby = (y + EQ[q][1]) % ny;
                if (nby < 0) nby += ny;
                for (int z = 0; z < nz; ++z) {
                    int nbz = (z + EQ[q][2]) % nz;
                    if (nbz < 0) nbz += nz;
                    const size_t c = cidx_h(x, y, z);
                    const size_t nb = cidx_h(nbx, nby, nbz);
                    if (solid[nb] && !solid[c])
                        slots.push_back(static_cast<uint64_t>(nb) * NQ + q);
                }
            }
        }
    }
    offsets[NQ] = slots.size();
    std::vector<float> gathered(slots.size());
    const size_t plane = static_cast<size_t>(ny) * nz;
    std::vector<float> flux_planes(plane * 4);

    if (cudaMalloc(&d.f, ncell * NQ * sizeof(float)) != cudaSuccess ||
        cudaMalloc(&d.g, ncell * NQ * sizeof(float)) != cudaSuccess ||
        cudaMalloc(&d.solid, ncell) != cudaSuccess ||
        cudaMalloc(&d.rho, ncell * sizeof(float)) != cudaSuccess ||
        cudaMalloc(&d.ux, ncell * sizeof(float)) != cudaSuccess ||
        cudaMalloc(&d.uy, ncell * sizeof(float)) != cudaSuccess ||
        cudaMalloc(&d.uz, ncell * sizeof(float)) != cudaSuccess ||
        cudaMalloc(&d.inlet, NQ * sizeof(float)) != cudaSuccess ||
        cudaMalloc(&d.invalid, sizeof(int)) != cudaSuccess)
        return fail("CUDA lattice allocation failed");
    if (!slots.empty() &&
        (cudaMalloc(&d.slots, slots.size() * sizeof(uint64_t)) != cudaSuccess ||
         cudaMalloc(&d.gather, gathered.size() * sizeof(float)) != cudaSuccess))
        return fail("CUDA boundary-link allocation failed");

    const float u_lat = static_cast<float>(p.u_lat);
    const float CS2 = 1.0f / 3.0f;
    const float W[NQ] = {
        1.0f / 3.0f, 1.0f / 18.0f, 1.0f / 18.0f, 1.0f / 18.0f, 1.0f / 18.0f, 1.0f / 18.0f,
        1.0f / 18.0f, 1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f,
        1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f,
        1.0f / 36.0f};
    float inlet_feq[NQ];
    const float u2 = u_lat * u_lat;
    for (int q = 0; q < NQ; ++q) {
        float eu = u_lat * static_cast<float>(EQ[q][0]);
        float term = 1.0f + eu / CS2 + (eu * eu) / (2.0f * CS2 * CS2) - u2 / (2.0f * CS2);
        inlet_feq[q] = W[q] * 1.0f * term;
    }
    if (cudaMemcpy(d.solid, solid.data(), ncell, cudaMemcpyHostToDevice) != cudaSuccess ||
        cudaMemcpy(d.inlet, inlet_feq, sizeof(inlet_feq), cudaMemcpyHostToDevice) != cudaSuccess)
        return fail("H2D lattice inputs failed");
    if (!slots.empty() &&
        cudaMemcpy(d.slots, slots.data(), slots.size() * sizeof(uint64_t),
                   cudaMemcpyHostToDevice) != cudaSuccess)
        return fail("H2D boundary links failed");

    const float omega_s = static_cast<float>(1.0 / p.tau0);
    const float tau_floor = static_cast<float>(p.tau0);
    const float nu0 = CS2 * static_cast<float>(p.tau0 - 0.5);
    const int threads = 256;
    const int blocks = static_cast<int>((ncell + threads - 1) / threads);
    const unsigned int link_blocks =
        static_cast<unsigned int>((slots.size() + threads - 1) / threads);
    dim3 thr2(16, 16);
    dim3 blk_yz((ny + 15) / 16, (nz + 15) / 16);
    dim3 blk_xz((nx + 15) / 16, (nz + 15) / 16);
    dim3 blk_xy((nx + 15) / 16, (ny + 15) / 16);
    initialize_kernel<<<blocks, threads>>>(d.f, d.solid, d.inlet, static_cast<int>(ncell));
    if (const cudaError_t error = cudaGetLastError(); error != cudaSuccess)
        return fail(std::string("CUDA initialization launch failed: ") + cudaGetErrorString(error));

    const int convergence_window = std::max(50, n_steps / 5);
    const int force_window_samples = std::max(5, (convergence_window + 9) / 10);
    std::vector<std::array<double, 3>> force_hist;
    force_hist.reserve(static_cast<size_t>(force_window_samples) + 1);
    std::array<double, 3> mean_force_lat{0.0, 0.0, 0.0};
    double force_drift = std::numeric_limits<double>::infinity();
    bool converged_in_loop = false;
    double convergence_ms = 0.0;
    unsigned long long sample_count = 0;
    chudware::prof::phases::add("init", std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - t_init).count());
    auto last_progress = std::chrono::steady_clock::now();
    if (on_progress) on_progress(0, n_steps);
    int step = 0;
    {
    chudware::prof::phases::PhaseScope loop("loop");
    for (step = 1; step <= n_steps; ++step) {
        collide_kernel<<<blocks, threads>>>(d.f, d.solid, nx, ny, nz, omega_s, tau_floor,
                                            nu0, 1);
        stream_kernel<<<blocks, threads>>>(d.f, d.g, nx, ny, nz);
        std::swap(d.f, d.g);
        inlet_kernel<<<blk_yz, thr2>>>(d.f, nx, ny, nz, d.inlet);
        outlet_kernel<<<blk_yz, thr2>>>(d.f, nx, ny, nz);
        slip_y_kernel<<<blk_xz, thr2>>>(d.f, nx, ny, nz);
        slip_z_kernel<<<blk_xy, thr2>>>(d.f, nx, ny, nz);
        const bool sampled_force = step % 10 == 0 || step == n_steps;
        if (sampled_force) {
            const auto t_cv = std::chrono::steady_clock::now();
            if (!slots.empty()) {
                gather_links_kernel<<<link_blocks, threads>>>(d.f, d.slots, d.gather,
                                                              slots.size());
                if (cudaMemcpy(gathered.data(), d.gather, gathered.size() * sizeof(float),
                               cudaMemcpyDeviceToHost) != cudaSuccess)
                    return fail("D2H boundary force sample failed");
            }
            std::array<double, 3> sample{0.0, 0.0, 0.0};
            for (int q = 1; q < NQ; ++q) {
                const size_t count = offsets[q + 1] - offsets[q];
                if (!count) continue;
                const double contrib = 2.0 * static_cast<double>(
                    pairwise_sum_f32_host(gathered.data() + offsets[q], count));
                for (int c = 0; c < 3; ++c) sample[c] += contrib * static_cast<double>(EQ[q][c]);
            }
            force_hist.push_back(sample);
            if (static_cast<int>(force_hist.size()) > force_window_samples)
                force_hist.erase(force_hist.begin());
            mean_force_lat = {0.0, 0.0, 0.0};
            for (const auto& value : force_hist)
                for (int c = 0; c < 3; ++c) mean_force_lat[c] += value[c];
            for (double& component : mean_force_lat)
                component /= static_cast<double>(force_hist.size());
            const size_t half = force_hist.size() / 2;
            if (half >= 2) {
                double early_drag = 0.0, late_drag = 0.0;
                for (size_t i = 0; i < half; ++i) {
                    early_drag += force_hist[i][0];
                    late_drag += force_hist[force_hist.size() - half + i][0];
                }
                early_drag /= static_cast<double>(half);
                late_drag /= static_cast<double>(half);
                force_drift = std::abs(late_drag - early_drag) /
                              std::max(std::abs(mean_force_lat[0]), 1e-30);
            }
            ++sample_count;
            convergence_ms += std::chrono::duration<double, std::milli>(
                std::chrono::steady_clock::now() - t_cv).count();
            R.steps_run = step;
            if (should_cancel && should_cancel()) {
                // The empty-mask path has no blocking gather copy to drain work.
                const cudaError_t completion = cudaDeviceSynchronize();
                R.error = "cancelled by sim_cancel at step " + std::to_string(step) +
                          " of " + std::to_string(n_steps);
                if (completion != cudaSuccess)
                    R.error += std::string("; CUDA completion failed: ") +
                               cudaGetErrorString(completion);
                return R;
            }
            if (chudware::governor::low_memory_now()) {
                const cudaError_t completion = cudaDeviceSynchronize();
                R.error = "CFD run stopped at step " + std::to_string(step) +
                          ": host memory critically low (governor) — close other "
                          "applications and re-run";
                if (completion != cudaSuccess)
                    R.error += std::string("; CUDA completion failed: ") +
                               cudaGetErrorString(completion);
                return R;
            }
        }
        bounce_back_kernel<<<blocks, threads>>>(d.f, d.solid, nx, ny, nz);
        zero_solid_kernel<<<blocks, threads>>>(d.f, d.solid, static_cast<int>(ncell));
        if (const cudaError_t error = cudaGetLastError(); error != cudaSuccess)
            return fail(std::string("kernel launch failed: ") + cudaGetErrorString(error));
        if (on_progress && (step == n_steps ||
            std::chrono::duration<double>(std::chrono::steady_clock::now() -
                                           last_progress).count() >= 1.0)) {
            // Heartbeats describe completed work, never queued launches.
            if (cudaDeviceSynchronize() != cudaSuccess)
                return fail("progress heartbeat sync failed");
            on_progress(step, n_steps);
            last_progress = std::chrono::steady_clock::now();
        }
        if (sampled_force &&
            static_cast<int>(force_hist.size()) >= force_window_samples &&
            step > n_steps / 5 && force_drift < 0.01 &&
            (slots.empty() || mean_force_lat[0] > 0.0)) {
            const auto t_cv = std::chrono::steady_clock::now();
            macros_kernel<<<blocks, threads>>>(d.f, d.rho, d.ux, d.uy, d.uz,
                                               static_cast<int>(ncell));
            const size_t bytes = plane * sizeof(float);
            const size_t last = (static_cast<size_t>(nx) - 1) * plane;
            if (cudaMemcpy(flux_planes.data(), d.rho, bytes, cudaMemcpyDeviceToHost) != cudaSuccess ||
                cudaMemcpy(flux_planes.data() + plane, d.rho + last, bytes,
                           cudaMemcpyDeviceToHost) != cudaSuccess ||
                cudaMemcpy(flux_planes.data() + 2 * plane, d.ux, bytes,
                           cudaMemcpyDeviceToHost) != cudaSuccess ||
                cudaMemcpy(flux_planes.data() + 3 * plane, d.ux + last, bytes,
                           cudaMemcpyDeviceToHost) != cudaSuccess)
                return fail("D2H continuity planes failed");
            double inlet = 0.0, outlet = 0.0;
            for (size_t i = 0; i < plane; ++i) {
                if (!solid[i])
                    inlet += static_cast<double>(flux_planes[i]) * flux_planes[2 * plane + i];
                if (!solid[last + i])
                    outlet += static_cast<double>(flux_planes[plane + i]) * flux_planes[3 * plane + i];
            }
            const double scale = std::max({std::abs(inlet), std::abs(outlet), 1e-30});
            const double continuity = std::abs(inlet - outlet) / scale;
            convergence_ms += std::chrono::duration<double, std::milli>(
                std::chrono::steady_clock::now() - t_cv).count();
            if (std::isfinite(continuity) && continuity < 0.02) {
                converged_in_loop = true;
                break;
            }
        }
    }
    if (step > n_steps) step = n_steps;
    R.steps_run = step;
    // Include all necessary completions in loop wall time; no timer-only per-step sync.
    if (cudaDeviceSynchronize() != cudaSuccess)
        return fail("CUDA loop completion failed");
    }
    chudware::prof::phases::add("convergence", convergence_ms, sample_count);
    chudware::prof::phases::PhaseScope output("output");
    if (cudaMemset(d.invalid, 0, sizeof(int)) != cudaSuccess)
        return fail("CUDA validation initialization failed");
    macros_kernel<<<blocks, threads>>>(d.f, d.rho, d.ux, d.uy, d.uz,
                                       static_cast<int>(ncell), d.invalid);
    int invalid_populations = 0;
    if (cudaMemcpy(&invalid_populations, d.invalid, sizeof(int),
                   cudaMemcpyDeviceToHost) != cudaSuccess)
        return fail("D2H population validity failed");
    if (invalid_populations) {
        R.diverged = true;
        R.residual = 1.0;
        R.error = "lbm_run_cuda: non-finite or unbounded lattice population";
        return R;
    }
    R.rho.resize(ncell);
    R.ux.resize(ncell);
    R.uy.resize(ncell);
    R.uz.resize(ncell);
    if (cudaMemcpy(R.rho.data(), d.rho, ncell * sizeof(float), cudaMemcpyDeviceToHost) != cudaSuccess ||
        cudaMemcpy(R.ux.data(), d.ux, ncell * sizeof(float), cudaMemcpyDeviceToHost) != cudaSuccess ||
        cudaMemcpy(R.uy.data(), d.uy, ncell * sizeof(float), cudaMemcpyDeviceToHost) != cudaSuccess ||
        cudaMemcpy(R.uz.data(), d.uz, ncell * sizeof(float), cudaMemcpyDeviceToHost) != cudaSuccess)
        return fail("D2H macros failed");
    const double drag_lat = mean_force_lat[0];
    const double lift_lat = mean_force_lat[1];
    const double side_lat = mean_force_lat[2];

    const double force_factor = p.rho_air * std::pow(p.dx_m, 4.0) / (p.dt_s * p.dt_s);
    R.drag_n = drag_lat * force_factor;
    R.lift_n = lift_lat * force_factor;
    R.side_n = side_lat * force_factor;

    double frontal_cells = 0.0;
    for (int y = 0; y < ny; ++y) {
        for (int z = 0; z < nz; ++z) {
            bool any = false;
            for (int x = 0; x < nx && !any; ++x)
                if (solid[cidx_h(x, y, z)]) any = true;
            if (any) frontal_cells += 1.0;
        }
    }
    R.frontal_area_mm2 = frontal_cells * (p.voxel_mm * p.voxel_mm);
    const double speed_mps = (p.dt_s > 0.0) ? (u_lat * p.dx_m / p.dt_s) : 0.0;
    const double frontal_area_m2 = R.frontal_area_mm2 * 1e-6;
    const double dyn_pressure = 0.5 * p.rho_air * speed_mps * speed_mps;
    R.cd = (frontal_area_m2 > 0.0 && dyn_pressure > 0.0)
               ? R.drag_n / (dyn_pressure * frontal_area_m2)
               : 0.0;
    double inlet_flux = 0.0, outlet_flux = 0.0;
    bool finite_fields = true;
    for (size_t c = 0; c < ncell; ++c) {
        if (!std::isfinite(R.rho[c]) || !std::isfinite(R.ux[c]) ||
            !std::isfinite(R.uy[c]) || !std::isfinite(R.uz[c]) ||
            (!solid[c] && R.rho[c] <= 0.0f)) {
            finite_fields = false;
            break;
        }
    }
    for (int y = 0; y < ny; ++y) {
        for (int z = 0; z < nz; ++z) {
            const size_t ci = cidx_h(0, y, z);
            const size_t co = cidx_h(nx - 1, y, z);
            if (!solid[ci]) inlet_flux += static_cast<double>(R.rho[ci]) * R.ux[ci];
            if (!solid[co]) outlet_flux += static_cast<double>(R.rho[co]) * R.ux[co];
        }
    }
    const double flux_scale = std::max({std::abs(inlet_flux), std::abs(outlet_flux), 1e-30});
    R.mass_imbalance = std::abs(inlet_flux - outlet_flux) / flux_scale;
    R.steps_run = step;
    R.diverged = !finite_fields || !std::isfinite(R.drag_n) || !std::isfinite(R.lift_n) ||
                 !std::isfinite(R.side_n) || !std::isfinite(R.cd) ||
                 !std::isfinite(R.mass_imbalance);
    const bool force_convergence_available =
        static_cast<int>(force_hist.size()) >= force_window_samples &&
        std::isfinite(force_drift);
    R.residual = R.diverged ? 1.0 : (force_convergence_available ? force_drift : 1.0);
    R.converged = !R.diverged &&
        (converged_in_loop ||
         (force_convergence_available && R.residual < 0.01 && R.mass_imbalance < 0.02));
    R.ok = !R.diverged;
    if (R.diverged) R.error = "lbm_run_cuda: non-finite/invalid final flow field";
    if (R.ok && on_progress && step < n_steps) on_progress(n_steps, n_steps);
    return R;
    } catch (const std::bad_alloc&) {
        return fail("lbm_run_cuda: allocation failed for requested lattice");
    } catch (const std::exception& e) {
        return fail(std::string("lbm_run_cuda: solver failed: ") + e.what());
    }
}

}  // namespace chudware::sim
