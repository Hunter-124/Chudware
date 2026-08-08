// lbm_cuda.cu — optional CUDA 13 D3Q19 LBM payload (collide/stream/bounce-back/inlet/forces).
// Mirrors the CPU float32 arithmetic in lbm.cpp as closely as practical on GPU.
// Voxelization stays on the host; this TU only owns the lattice loop.
#include "sim/lbm_cuda.h"

#include <cuda_runtime.h>
#include <algorithm>

#include <cmath>
#include <cstdio>
#include <limits>
#include <cstring>
#include <string>
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

__global__ void collide_kernel(float* f, const uint8_t* solid, int nx, int ny, int nz,
                               float omega_s, float nu0, int use_smag) {
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
        if (tau_eff < (1.0f / omega_s)) tau_eff = 1.0f / omega_s;
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
                              int ncell) {
    int c = blockIdx.x * blockDim.x + threadIdx.x;
    if (c >= ncell) return;
    const size_t base = static_cast<size_t>(c) * NQ;
    float loc[NQ];
    for (int q = 0; q < NQ; ++q) loc[q] = f[base + q];
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

#define CUDA_OK(call, reason)                                                          \
    do {                                                                               \
        cudaError_t _e = (call);                                                       \
        if (_e != cudaSuccess) {                                                       \
            reason = std::string("CUDA error: ") + cudaGetErrorString(_e);             \
            return false;                                                              \
        }                                                                              \
    } while (0)

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

    float *d_f = nullptr, *d_g = nullptr, *d_inlet = nullptr;
    float *d_rho = nullptr, *d_ux = nullptr, *d_uy = nullptr, *d_uz = nullptr;
    uint8_t* d_solid = nullptr;
    auto cleanup = [&]() {
        if (d_f) cudaFree(d_f);
        if (d_g) cudaFree(d_g);
        if (d_inlet) cudaFree(d_inlet);
        if (d_rho) cudaFree(d_rho);
        if (d_ux) cudaFree(d_ux);
        if (d_uy) cudaFree(d_uy);
        if (d_uz) cudaFree(d_uz);
        if (d_solid) cudaFree(d_solid);
    };

    std::string why;
    auto fail = [&](const std::string& msg) {
        fallback_reason = msg;
        R.error = msg;
        cleanup();
        return R;
    };

    if (cudaMalloc(&d_f, ncell * NQ * sizeof(float)) != cudaSuccess)
        return fail("cudaMalloc f failed");
    if (cudaMalloc(&d_g, ncell * NQ * sizeof(float)) != cudaSuccess)
        return fail("cudaMalloc g failed");
    if (cudaMalloc(&d_solid, ncell * sizeof(uint8_t)) != cudaSuccess)
        return fail("cudaMalloc solid failed");
    if (cudaMalloc(&d_rho, ncell * sizeof(float)) != cudaSuccess)
        return fail("cudaMalloc rho failed");
    if (cudaMalloc(&d_ux, ncell * sizeof(float)) != cudaSuccess)
        return fail("cudaMalloc ux failed");
    if (cudaMalloc(&d_uy, ncell * sizeof(float)) != cudaSuccess)
        return fail("cudaMalloc uy failed");
    if (cudaMalloc(&d_uz, ncell * sizeof(float)) != cudaSuccess)
        return fail("cudaMalloc uz failed");
    if (cudaMalloc(&d_inlet, NQ * sizeof(float)) != cudaSuccess)
        return fail("cudaMalloc inlet failed");

    if (cudaMemcpy(d_solid, solid.data(), ncell, cudaMemcpyHostToDevice) != cudaSuccess)
        return fail("H2D solid failed");

    // Initialise f = feq(1, u_lat, 0, 0); zero solid cells — host-side then upload.
    const float u_lat = static_cast<float>(p.u_lat);
    std::vector<float> h_f(ncell * NQ, 0.0f);
    float inlet_feq[NQ];
    {
        const float u2 = u_lat * u_lat;
        const float CS2 = 1.0f / 3.0f;
        const float W[NQ] = {
            1.0f / 3.0f, 1.0f / 18.0f, 1.0f / 18.0f, 1.0f / 18.0f, 1.0f / 18.0f, 1.0f / 18.0f,
            1.0f / 18.0f, 1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f,
            1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f, 1.0f / 36.0f,
            1.0f / 36.0f};
        const int EQ[NQ][3] = {
            {0, 0, 0}, {1, 0, 0}, {-1, 0, 0}, {0, 1, 0}, {0, -1, 0}, {0, 0, 1}, {0, 0, -1},
            {1, 1, 0}, {-1, -1, 0}, {1, -1, 0}, {-1, 1, 0}, {1, 0, 1}, {-1, 0, -1},
            {1, 0, -1}, {-1, 0, 1}, {0, 1, 1}, {0, -1, -1}, {0, 1, -1}, {0, -1, 1}};
        for (int q = 0; q < NQ; ++q) {
            float eu = u_lat * static_cast<float>(EQ[q][0]);
            float term = 1.0f + eu / CS2 + (eu * eu) / (2.0f * CS2 * CS2) - u2 / (2.0f * CS2);
            inlet_feq[q] = W[q] * 1.0f * term;
        }
        for (size_t c = 0; c < ncell; ++c) {
            if (solid[c]) continue;
            for (int q = 0; q < NQ; ++q) h_f[c * NQ + q] = inlet_feq[q];
        }
    }
    if (cudaMemcpy(d_f, h_f.data(), h_f.size() * sizeof(float), cudaMemcpyHostToDevice) !=
        cudaSuccess)
        return fail("H2D f failed");
    if (cudaMemcpy(d_inlet, inlet_feq, NQ * sizeof(float), cudaMemcpyHostToDevice) != cudaSuccess)
        return fail("H2D inlet failed");

    const double tau_current = p.tau0;
    const bool use_smag = true;  // Match CPU: preserve tau0 and add LES viscosity at high Re.
    const float omega_s = static_cast<float>(1.0 / tau_current);
    const float CS2 = 1.0f / 3.0f;
    const float nu0 = CS2 * static_cast<float>(tau_current - 0.5);

    const int EQ[NQ][3] = {
        {0, 0, 0}, {1, 0, 0}, {-1, 0, 0}, {0, 1, 0}, {0, -1, 0}, {0, 0, 1}, {0, 0, -1},
        {1, 1, 0}, {-1, -1, 0}, {1, -1, 0}, {-1, 1, 0}, {1, 0, 1}, {-1, 0, -1},
        {1, 0, -1}, {-1, 0, 1}, {0, 1, 1}, {0, -1, -1}, {0, 1, -1}, {0, -1, 1}};
    auto cidx_h = [ny, nz](int x, int y, int z) {
        return (static_cast<size_t>(x) * ny + y) * nz + z;
    };
    auto momentum_exchange_host = [&](const std::vector<float>& hf, double& drag_lat,
                                      double& lift_lat, double& side_lat) {
        drag_lat = 0.0;
        lift_lat = 0.0;
        side_lat = 0.0;
        std::vector<float> gather;
        for (int q = 1; q < NQ; ++q) {
            gather.clear();
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
                        if (solid[nb] && !solid[c]) gather.push_back(hf[c * NQ + q]);
                    }
                }
            }
            const float s32 = pairwise_sum_f32_host(gather.data(), gather.size());
            const double contrib = static_cast<double>(s32) * 2.0;
            drag_lat += contrib * static_cast<double>(EQ[q][0]);
            lift_lat += contrib * static_cast<double>(EQ[q][1]);
            side_lat += contrib * static_cast<double>(EQ[q][2]);
        }
    };
    const int threads = 256;
    const int blocks = static_cast<int>((ncell + threads - 1) / threads);
    dim3 thr2(16, 16);
    dim3 blk_yz((ny + 15) / 16, (nz + 15) / 16);
    dim3 blk_xz((nx + 15) / 16, (nz + 15) / 16);
    dim3 blk_xy((nx + 15) / 16, (ny + 15) / 16);

    const int sample_every = std::max(1, n_steps / 50);
    std::vector<double> drag_hist;

    for (int step = 1; step <= n_steps; ++step) {
        collide_kernel<<<blocks, threads>>>(d_f, d_solid, nx, ny, nz, omega_s, nu0,
                                            use_smag ? 1 : 0);
        stream_kernel<<<blocks, threads>>>(d_f, d_g, nx, ny, nz);
        std::swap(d_f, d_g);
        inlet_kernel<<<blk_yz, thr2>>>(d_f, nx, ny, nz, d_inlet);
        outlet_kernel<<<blk_yz, thr2>>>(d_f, nx, ny, nz);
        slip_y_kernel<<<blk_xz, thr2>>>(d_f, nx, ny, nz);
        slip_z_kernel<<<blk_xy, thr2>>>(d_f, nx, ny, nz);
        bounce_back_kernel<<<blocks, threads>>>(d_f, d_solid, nx, ny, nz);
        zero_solid_kernel<<<blocks, threads>>>(d_f, d_solid, static_cast<int>(ncell));
        if (const cudaError_t launch_error = cudaGetLastError();
            launch_error != cudaSuccess) {
            return fail(std::string("kernel launch failed: ") +
                        cudaGetErrorString(launch_error));
        }
        if (step % sample_every == 0 || step == n_steps) {
            if (on_progress) on_progress(step, n_steps);
            // Cooperative cancel (sim_cancel): drain the queued kernels, free, and report a
            // terminal cancel (NOT a fallback — the caller checks should_cancel before the
            // CPU fallback). Takes effect at the next checkpoint, like the CPU path.
            if (should_cancel && should_cancel()) {
                cudaDeviceSynchronize();
                cleanup();
                R.ok = false;
                R.steps_run = step;
                R.error = "cancelled by sim_cancel at step " + std::to_string(step) +
                          " of " + std::to_string(n_steps);
                return R;
            }
            if (cudaMemcpy(h_f.data(), d_f, h_f.size() * sizeof(float),
                           cudaMemcpyDeviceToHost) != cudaSuccess)
                return fail("D2H convergence sample failed");
            double drag_sample = 0.0, lift_sample = 0.0, side_sample = 0.0;
            momentum_exchange_host(h_f, drag_sample, lift_sample, side_sample);
            drag_hist.push_back(drag_sample);
        }
    }
    if (cudaDeviceSynchronize() != cudaSuccess)
        return fail("cudaDeviceSynchronize failed");

    macros_kernel<<<blocks, threads>>>(d_f, d_rho, d_ux, d_uy, d_uz, static_cast<int>(ncell));
    if (cudaDeviceSynchronize() != cudaSuccess)
        return fail("macros sync failed");

    R.rho.resize(ncell);
    R.ux.resize(ncell);
    R.uy.resize(ncell);
    R.uz.resize(ncell);
    if (cudaMemcpy(R.rho.data(), d_rho, ncell * sizeof(float), cudaMemcpyDeviceToHost) !=
            cudaSuccess ||
        cudaMemcpy(R.ux.data(), d_ux, ncell * sizeof(float), cudaMemcpyDeviceToHost) !=
            cudaSuccess ||
        cudaMemcpy(R.uy.data(), d_uy, ncell * sizeof(float), cudaMemcpyDeviceToHost) !=
            cudaSuccess ||
        cudaMemcpy(R.uz.data(), d_uz, ncell * sizeof(float), cudaMemcpyDeviceToHost) !=
            cudaSuccess)
        return fail("D2H macros failed");

    // Download f for host-side momentum exchange (float64 accum, float32 pairwise sum
    // order matches CPU path).
    if (cudaMemcpy(h_f.data(), d_f, h_f.size() * sizeof(float), cudaMemcpyDeviceToHost) !=
        cudaSuccess)
        return fail("D2H f failed");

    // Host-side momentum exchange preserves the CPU path's deterministic direction order.
    double drag_lat = 0.0, lift_lat = 0.0, side_lat = 0.0;
    momentum_exchange_host(h_f, drag_lat, lift_lat, side_lat);

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
    R.steps_run = n_steps;
    R.diverged = !finite_fields || !std::isfinite(R.drag_n) || !std::isfinite(R.lift_n) ||
                 !std::isfinite(R.side_n) || !std::isfinite(R.cd) ||
                 !std::isfinite(R.mass_imbalance);
    R.residual = -1.0;
    R.converged = false;
    if (!R.diverged && drag_hist.size() >= 4) {
        const size_t tail = std::max<size_t>(3, drag_hist.size() / 5);
        const size_t start = drag_hist.size() - tail;
        double mean = 0.0;
        for (size_t i = start; i < drag_hist.size(); ++i) mean += drag_hist[i];
        mean /= static_cast<double>(tail);
        double var = 0.0;
        for (size_t i = start; i < drag_hist.size(); ++i)
            var += (drag_hist[i] - mean) * (drag_hist[i] - mean);
        var /= static_cast<double>(tail);
        R.residual = std::sqrt(var) / std::max(std::abs(mean), 1e-30);
        R.converged = R.residual < 0.01 && R.mass_imbalance < 0.02;
    }
    R.ok = !R.diverged;
    if (R.diverged) R.error = "lbm_run_cuda: non-finite/invalid final flow field";
    cleanup();
    return R;
}

}  // namespace chudware::sim
