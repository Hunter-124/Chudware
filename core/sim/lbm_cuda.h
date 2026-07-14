// Optional CUDA 13 backend for D3Q19 LBM (collide/stream/BC/force reduction).
// Built only when AICAD_WITH_CUDA=ON. CPU path remains the reference + fallback.
#pragma once

#include "sim/lbm.h"

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace aicad::sim {

// True when this TU was compiled with CUDA and at least one device is present.
bool lbm_cuda_device_available();

// Run the lattice on CUDA. On any failure sets R.ok=false and R.error; caller falls back.
// Preserves float32 reduction-order parity goals of the CPU path (pairwise 19-sum).
LbmResult lbm_run_cuda(const std::vector<uint8_t>& solid, int nx, int ny, int nz,
                       const LbmParams& p, int n_steps,
                       const std::function<void(int, int)>& on_progress,
                       std::string& fallback_reason);

}  // namespace aicad::sim
