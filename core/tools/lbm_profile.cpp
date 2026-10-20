// Deterministic CPU/CUDA LBM timing and parity probe. CUDA is called directly so
// the same driver can exercise a pre-fix lbm.cpp compiled without CUDA dispatch.
#include "sim/lbm.h"
#ifdef CHUDWARE_WITH_CUDA
#include "sim/lbm_cuda.h"
#endif
#include "prof/phases.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>
#include <nlohmann/json.hpp>

namespace {
using chudware::sim::LbmParams;
using chudware::sim::LbmResult;
using Json = nlohmann::json;
constexpr int eq[19][3] = {
    {0,0,0}, {1,0,0}, {-1,0,0}, {0,1,0}, {0,-1,0}, {0,0,1}, {0,0,-1},
    {1,1,0}, {-1,-1,0}, {1,-1,0}, {-1,1,0}, {1,0,1}, {-1,0,-1},
    {1,0,-1}, {-1,0,1}, {0,1,1}, {0,-1,-1}, {0,1,-1}, {0,-1,1}};
struct Options {
    int nx = 128, ny = 64, nz = 64, steps = 200, iterations = 3;
    std::string backend = "both", mask = "asymmetric";
    double tau = 0.6, u_lat = 0.05;
    bool allow_baseline_mismatch = false;
};
int integer(const std::string& s) {
    size_t used = 0;
    const long value = std::stol(s, &used);
    if (used != s.size() || value < 1 || value > std::numeric_limits<int>::max())
        throw std::invalid_argument("expected a positive int: " + s);
    return static_cast<int>(value);
}
double real(const std::string& s) {
    size_t used = 0;
    const double value = std::stod(s, &used);
    if (used != s.size() || !std::isfinite(value))
        throw std::invalid_argument("expected a finite number: " + s);
    return value;
}
Options parse(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--allow-baseline-mismatch") { o.allow_baseline_mismatch = true; continue; }
        if (arg == "--help") {
            std::cout << "lbm_profile [--nx N --ny N --nz N --steps N --iterations N] "
                         "[--backend cpu|cuda|both] [--mask box|asymmetric|empty] "
                         "[--tau T --u-lat U] [--allow-baseline-mismatch]\n"
                         "Default parity is strict and failures exit 1. The explicit baseline "
                         "option reports parity_pass=false but permits a known old mismatch. "
                         "Missing CUDA and solver errors always fail. Each solve is capped "
                         "by a conservative 4 GiB estimate; CPU threads are capped at four.\n";
            std::exit(0);
        }
        if (i + 1 >= argc) throw std::invalid_argument("missing value for " + arg);
        const std::string value = argv[++i];
        if (arg == "--nx") o.nx = integer(value);
        else if (arg == "--ny") o.ny = integer(value);
        else if (arg == "--nz") o.nz = integer(value);
        else if (arg == "--steps") o.steps = integer(value);
        else if (arg == "--iterations") o.iterations = integer(value);
        else if (arg == "--backend") o.backend = value;
        else if (arg == "--mask") o.mask = value;
        else if (arg == "--tau") o.tau = real(value);
        else if (arg == "--u-lat") o.u_lat = real(value);
        else throw std::invalid_argument("unknown argument: " + arg);
    }
    if (o.nx < 3 || o.ny < 3 || o.nz < 3) throw std::invalid_argument("dimensions must be >= 3");
    if (o.backend != "cpu" && o.backend != "cuda" && o.backend != "both")
        throw std::invalid_argument("backend must be cpu, cuda, or both");
    if (o.mask != "box" && o.mask != "asymmetric" && o.mask != "empty")
        throw std::invalid_argument("mask must be box, asymmetric, or empty");
    if (o.tau <= 0.5 || o.tau > 2.0 || o.u_lat <= 0.0 || o.u_lat > 0.1)
        throw std::invalid_argument("tau must be (0.5,2], u-lat must be (0,0.1]");
    return o;
}
uint64_t hash_bytes(uint64_t h, const void* data, size_t bytes) {
    const auto* p = static_cast<const unsigned char*>(data);
    for (size_t i = 0; i < bytes; ++i) { h ^= p[i]; h *= UINT64_C(1099511628211); }
    return h;
}
uint64_t fingerprint(const LbmResult& r) {
    uint64_t h = UINT64_C(14695981039346656037);
    for (const auto* field : {&r.rho, &r.ux, &r.uy, &r.uz})
        h = hash_bytes(h, field->data(), field->size() * sizeof(float));
    for (double v : {r.drag_n, r.lift_n, r.side_n, r.cd, r.frontal_area_mm2,
                     r.residual, r.mass_imbalance}) h = hash_bytes(h, &v, sizeof(v));
    h = hash_bytes(h, &r.steps_run, sizeof(r.steps_run));
    h = hash_bytes(h, &r.converged, sizeof(r.converged));
    return h;
}
Json field_error(const std::vector<float>& a, const std::vector<float>& b) {
    if (a.size() != b.size()) return {{"same_size", false}, {"finite", false}};
    double maximum = 0.0, squares = 0.0;
    bool finite = true;
    for (size_t i = 0; i < a.size(); ++i) {
        if (!std::isfinite(a[i]) || !std::isfinite(b[i])) { finite = false; continue; }
        const double d = std::abs(static_cast<double>(a[i]) - b[i]);
        maximum = std::max(maximum, d);
        squares += d * d;
    }
    return {{"same_size", true}, {"finite", finite}, {"max_abs", maximum},
            {"rms", a.empty() ? 0.0 : std::sqrt(squares / a.size())}};
}
Json parity(const LbmResult& cpu, const LbmResult& gpu, double u_lat) {
    Json fields;
    bool fields_pass = true;
    for (const auto& entry : {std::make_pair("rho", std::make_pair(&cpu.rho, &gpu.rho)),
                              std::make_pair("ux", std::make_pair(&cpu.ux, &gpu.ux)),
                              std::make_pair("uy", std::make_pair(&cpu.uy, &gpu.uy)),
                              std::make_pair("uz", std::make_pair(&cpu.uz, &gpu.uz))}) {
        Json e = field_error(*entry.second.first, *entry.second.second);
        const double limit = std::string(entry.first) == "rho" ? 1e-5 : 1e-4 * u_lat;
        e["limit_max_abs"] = limit;
        e["pass"] = e.value("finite", false) && e.value("same_size", false) &&
                    e.value("max_abs", std::numeric_limits<double>::infinity()) <= limit;
        fields_pass = fields_pass && e["pass"].get<bool>();
        fields[entry.first] = std::move(e);
    }
    const double denom = std::max(1e-12, std::abs(cpu.drag_n));
    const double drag_rel = std::abs(gpu.drag_n - cpu.drag_n) / denom;
    const double lift_rel = std::abs(gpu.lift_n - cpu.lift_n) / denom;
    const double side_rel = std::abs(gpu.side_n - cpu.side_n) / denom;
    const double residual_abs = std::abs(gpu.residual - cpu.residual);
    const double mass_abs = std::abs(gpu.mass_imbalance - cpu.mass_imbalance);
    const bool forces_pass = std::isfinite(drag_rel) && std::isfinite(lift_rel) &&
        std::isfinite(side_rel) && drag_rel < 1e-3 && lift_rel < 1e-3 && side_rel < 1e-3 &&
        std::abs(gpu.cd - cpu.cd) <= 1e-3 * std::max(1e-12, std::abs(cpu.cd)) &&
        gpu.frontal_area_mm2 == cpu.frontal_area_mm2;
    const bool convergence_pass = gpu.steps_run == cpu.steps_run &&
        gpu.converged == cpu.converged && gpu.diverged == cpu.diverged &&
        std::isfinite(residual_abs) && residual_abs < 1e-3 &&
        std::isfinite(mass_abs) && mass_abs < 1e-6;
    return {{"kind", "parity"}, {"cpu_backend", cpu.backend}, {"cuda_backend", gpu.backend},
            {"cpu_steps", cpu.steps_run}, {"cuda_steps", gpu.steps_run}, {"fields", fields},
            {"drag_relative_to_cpu_drag", drag_rel}, {"lift_relative_to_cpu_drag", lift_rel},
            {"side_relative_to_cpu_drag", side_rel}, {"residual_abs", residual_abs},
            {"mass_imbalance_abs", mass_abs}, {"forces_pass", forces_pass},
            {"convergence_pass", convergence_pass},
            {"parity_pass", cpu.ok && gpu.ok && cpu.backend == "cpu" && gpu.backend == "cuda" &&
                             fields_pass && forces_pass && convergence_pass}};
}
LbmResult run(const Options& o, const std::vector<uint8_t>& mask, const char* backend,
              int iteration, uint64_t& hash, bool& deterministic, bool& have_hash) {
    LbmParams p;
    p.backend = backend;
    p.tau0 = o.tau;
    p.u_lat = o.u_lat;
    p.dx_m = 1e-3;
    p.voxel_mm = 1.0;
    p.dt_s = o.u_lat * p.dx_m / 10.0;
    chudware::prof::phases::reset();
    const auto start = std::chrono::steady_clock::now();
    LbmResult result;
    std::string reason;
    if (p.backend == "cpu") result = chudware::sim::lbm_run(mask, o.nx, o.ny, o.nz, p, o.steps);
    else {
#ifdef CHUDWARE_WITH_CUDA
        result = chudware::sim::lbm_run_cuda(mask, o.nx, o.ny, o.nz, p, o.steps, {}, {}, reason);
#else
        result.error = "benchmark compiled without CUDA; no fallback credited";
#endif
    }
    const double seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
    const uint64_t current = fingerprint(result);
    if (have_hash && current != hash) deterministic = false;
    if (!have_hash) { hash = current; have_hash = true; }
    const bool actual = result.backend == backend;
    Json row = {{"kind", "run"}, {"iteration", iteration}, {"requested_backend", backend},
                {"actual_backend", result.backend}, {"backend_matches_request", actual},
                {"ok", result.ok}, {"error", result.error}, {"fallback_reason", reason},
                {"seconds", seconds},
                {"mlups", result.ok && actual && seconds > 0.0
                    ? Json(mask.size() * static_cast<double>(result.steps_run) / seconds / 1e6)
                    : Json(nullptr)},
                {"requested_steps", o.steps}, {"steps_run", result.steps_run},
                {"fingerprint", std::to_string(current)}, {"deterministic_so_far", deterministic},
                {"drag_n", result.drag_n}, {"lift_n", result.lift_n}, {"side_n", result.side_n},
                {"cd", result.cd}, {"frontal_area_mm2", result.frontal_area_mm2},
                {"converged", result.converged}, {"diverged", result.diverged},
                {"residual", result.residual}, {"mass_imbalance", result.mass_imbalance},
                {"phases", chudware::prof::phases::report()}};
    std::cout << row.dump() << '\n';
    if (!result.ok || !actual) throw std::runtime_error("requested backend did not complete successfully");
    return result;
}
}  // namespace

int main(int argc, char** argv) {
    try {
        const Options o = parse(argc, argv);
        // Do not raise an existing tighter user cap.
        int threads = 4;
        if (const char* e = std::getenv("CHUDWARE_SIM_THREADS")) {
            const int n = std::atoi(e);
            if (n > 0) threads = std::min(threads, n);
        }
#ifdef _WIN32
        _putenv_s("CHUDWARE_SIM_THREADS", std::to_string(threads).c_str());
#else
        if (setenv("CHUDWARE_SIM_THREADS", std::to_string(threads).c_str(), 1) != 0)
            throw std::runtime_error("cannot set solver thread cap");
#endif
        const uint64_t plane = static_cast<uint64_t>(o.ny) * o.nz;
        if (plane > static_cast<uint64_t>(std::numeric_limits<int>::max()) / o.nx)
            throw std::invalid_argument("grid exceeds CUDA int indexing");
        const uint64_t cells = plane * o.nx;
        constexpr uint64_t cap = UINT64_C(4) << 30;
        // Refuse before allocating even the mask; 256 bytes/cell covers the baseline
        // lattice plus retained CPU reference fields for these compact body shapes.
        if (cells > cap / 256) throw std::invalid_argument("grid exceeds 4 GiB solve estimate");
        std::vector<uint8_t> mask(static_cast<size_t>(cells), 0);
        auto idx = [&](int x, int y, int z) { return (static_cast<size_t>(x) * o.ny + y) * o.nz + z; };
        uint64_t solids = 0;
        if (o.mask != "empty") {
            for (int x = 1; x + 1 < o.nx; ++x)
                for (int y = 1; y + 1 < o.ny; ++y)
                    for (int z = 1; z + 1 < o.nz; ++z) {
                        const bool box = x >= o.nx / 3 && x < std::max(o.nx / 3 + 1, o.nx / 2) &&
                            y >= o.ny / 3 && y < std::max(o.ny / 3 + 1, 2 * o.ny / 3) &&
                            z >= o.nz / 3 && z < std::max(o.nz / 3 + 1, 2 * o.nz / 3);
                        const bool wing = o.mask == "asymmetric" &&
                            x >= o.nx / 3 && x < std::max(o.nx / 3 + 1, 2 * o.nx / 3) &&
                            y >= o.ny / 2 && y < std::max(o.ny / 2 + 1, 3 * o.ny / 4) &&
                            z >= o.nz / 2 && z < std::max(o.nz / 2 + 1, 3 * o.nz / 4);
                        if (box || wing) { mask[idx(x, y, z)] = 1; ++solids; }
                    }
        }
        uint64_t links = 0, max_q = 0;
        for (int q = 1; q < 19; ++q) {
            uint64_t count = 0;
            for (int x = 0; x < o.nx; ++x)
                for (int y = 0; y < o.ny; ++y)
                    for (int z = 0; z < o.nz; ++z) {
                        const int xx = (x + eq[q][0] + o.nx) % o.nx;
                        const int yy = (y + eq[q][1] + o.ny) % o.ny;
                        const int zz = (z + eq[q][2] + o.nz) % o.nz;
                        if (!mask[idx(x,y,z)] && mask[idx(xx,yy,zz)]) ++count;
                    }
            links += count;
            max_q = std::max(max_q, count);
        }
        const uint64_t history = static_cast<uint64_t>(std::max(5, (std::max(50, o.steps / 5) + 9) / 10) + 1) * 24;
        const uint64_t reference = o.backend == "both" ? 16 * cells : 0;
        const uint64_t cpu_host = 169 * cells + 32 * links + 16 * solids + 4 * max_q + history;
        const uint64_t cuda_host = 17 * cells + 20 * links + 16 * plane + history + reference;
        const uint64_t old_cuda_host = 93 * cells + 4 * max_q + reference +
            (static_cast<uint64_t>(o.steps) / std::max(1, o.steps / 50) + 1) * sizeof(double);
        const uint64_t cuda_device = 169 * cells + 12 * links + 80;
        const uint64_t cuda_combined = cuda_host + cuda_device;
        const uint64_t baseline_combined = old_cuda_host + 169 * cells + 80;
        if ((o.backend != "cuda" && cpu_host > cap) ||
            (o.backend != "cpu" && (cuda_combined > cap || baseline_combined > cap)))
            throw std::invalid_argument("boundary-inclusive estimate exceeds 4 GiB per solve");
        const uint64_t mask_hash = hash_bytes(UINT64_C(14695981039346656037), mask.data(), mask.size());
        std::cout << Json({{"kind", "configuration"}, {"nx", o.nx}, {"ny", o.ny}, {"nz", o.nz},
            {"steps", o.steps}, {"iterations", o.iterations}, {"backend", o.backend}, {"mask", o.mask},
            {"u_lat", o.u_lat}, {"tau0", o.tau}, {"cpu_thread_cap", threads}, {"cells", cells},
            {"solid_cells", solids}, {"boundary_links", links}, {"mask_fingerprint", std::to_string(mask_hash)},
            {"estimated_cpu_host_peak_bytes", cpu_host}, {"estimated_cuda_host_peak_bytes", cuda_host},
            {"estimated_baseline_cuda_host_peak_bytes", old_cuda_host},
            {"estimated_cuda_device_peak_bytes", cuda_device}, {"memory_cap_bytes", cap},
            {"estimated_cuda_combined_peak_bytes", cuda_combined},
            {"estimated_baseline_cuda_combined_peak_bytes", baseline_combined},
            {"allow_baseline_mismatch", o.allow_baseline_mismatch}}).dump() << '\n';
        std::array<uint64_t, 2> hashes{};
        std::array<bool, 2> deterministic{true, true}, have_hash{false, false};
        bool parity_pass = true;
        for (int i = 0; i < o.iterations; ++i) {
            LbmResult cpu, gpu;
            if (o.backend != "cuda") cpu = run(o, mask, "cpu", i, hashes[0], deterministic[0], have_hash[0]);
            if (o.backend != "cpu") gpu = run(o, mask, "cuda", i, hashes[1], deterministic[1], have_hash[1]);
            if (o.backend == "both") {
                Json comparison = parity(cpu, gpu, o.u_lat);
                comparison["iteration"] = i;
                parity_pass = parity_pass && comparison["parity_pass"].get<bool>();
                comparison["baseline_mismatch_allowed"] = o.allow_baseline_mismatch;
                std::cout << comparison.dump() << '\n';
            }
        }
        const bool deterministic_pass = deterministic[0] && deterministic[1];
        const bool success = deterministic_pass && (parity_pass || o.allow_baseline_mismatch);
        std::cout << Json({{"kind", "summary"}, {"parity_checked", o.backend == "both"},
            {"parity_pass", o.backend == "both" ? Json(parity_pass) : Json(nullptr)},
            {"deterministic_pass", deterministic_pass},
            {"status", !deterministic_pass ? "nondeterministic" :
                !parity_pass ? (o.allow_baseline_mismatch ? "baseline_mismatch_allowed" : "parity_failed") :
                (o.backend == "both" ? "passed" : "timing_only")}, {"exit_success", success}}).dump() << '\n';
        return success ? 0 : 1;
    } catch (const std::exception& e) {
        std::cerr << Json({{"kind", "error"}, {"error", e.what()}}).dump() << '\n';
        return 1;
    }
}
