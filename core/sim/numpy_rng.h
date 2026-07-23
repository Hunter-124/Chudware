// numpy_rng.h — bit-exact replication of numpy's default_rng(seed).standard_normal().
//
// engine/sim/analysis_ext.py::run_monte_carlo seeds a Monte-Carlo tolerance study with
// numpy's Generator(PCG64(SeedSequence(seed))) and draws one standard_normal() per eligible
// device per run. To reproduce that stream in the C++ core (core/sim/spice.cpp "mc" branch)
// we must replicate, bit-for-bit, three pieces of numpy's BSD-licensed RNG (numpy 2.x):
//
//   1. SeedSequence(int seed).generate_state(4, uint64)  — numpy/random/bit_generator.pyx
//      (mix_entropy / generate_state: INIT_A/INIT_B, MULT_A/MULT_B, MIX_MULT_L/R, XSHIFT).
//   2. PCG64 (XSL-RR 128/64): 128-bit LCG state*PCG_DEFAULT_MULTIPLIER_128 + inc, output
//      rotr64(hi^lo, hi>>58) — numpy/random/src/pcg64/pcg64.{c,h}.
//   3. standard_normal (Generator): the 256-box ziggurat random_standard_normal with numpy's
//      exact ki/wi/fi tables (next_double = (next_uint64>>11) * 2^-53) —
//      numpy/random/src/distributions/distributions.c + ziggurat_constants.h.
//
// NumpyGenerator(seed).standard_normal() then matches default_rng(seed).standard_normal()
// exactly (validated by tools/cpp_spice_parity.py via the sim_numpy_normal diagnostic).
#pragma once

#include <cstdint>

namespace chudware::sim {

// A 128-bit unsigned integer with just the wrap-around mul/add PCG64 needs. We do NOT rely
// on a compiler __int128 (MSVC lacks it) — this portable struct gives identical results.
struct U128 {
    uint64_t hi = 0;
    uint64_t lo = 0;
};

// Generator(PCG64(SeedSequence(seed))) — bit-exact numpy clone for an integer seed.
class NumpyGenerator {
public:
    explicit NumpyGenerator(uint64_t seed);

    // numpy Generator.standard_normal() — one draw per call, same stream as numpy.
    double standard_normal();

    // Raw 64-bit PCG64 output (numpy bit_generator next_uint64). Exposed for testing.
    uint64_t next_uint64();

    // numpy next_double: (next_uint64() >> 11) * (1.0 / 9007199254740992.0).
    double next_double();

private:
    U128 state_;  // PCG64 LCG state
    U128 inc_;    // PCG64 increment (odd)
    void pcg_step();
};

}  // namespace chudware::sim
