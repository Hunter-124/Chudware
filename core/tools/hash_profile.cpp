// Standalone SHA-256 throughput harness, with no engine or crypto-library linkage.
//
// Current header: c++ -O3 -DNDEBUG -std=c++17 -Icore core/tools/hash_profile.cpp -o hash_profile
// Original header: put its core/ include root FIRST instead of -Icore above.
// No new Sha256 API is required by the baseline build. Backend detection below is
// conditional on the accelerated header's feature macro; older headers are scalar.
// Usage: hash_profile [size_bytes [iterations]] (defaults: 64 MiB, 16 iterations).
// Input byte i is ((i % 256) * 131 + 17) % 256; the digest is for ONE such message,
// not concatenated repetitions. Allocation/fill/warmup are outside the measured loop.
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <ctime>
#include <exception>
#include <limits>
#include <string>
#include <string_view>
#include <system_error>
#include <vector>

#include "util/sha256.h"

namespace {
using Digest = std::array<uint8_t, 32>;

bool parse_size(const char* text, size_t& result) {
    const std::string_view value(text);
    const auto parsed = std::from_chars(value.data(), value.data() + value.size(), result);
    return !value.empty() && parsed.ec == std::errc{} &&
           parsed.ptr == value.data() + value.size();
}

Digest hash_payload(const uint8_t* bytes, size_t size) {
    chudware::util::Sha256 hash;
    hash.update(bytes, size);
    return hash.finish();
}

const char* backend() {
#ifdef CHUDWARE_UTIL_SHA256_HAS_SHA_NI
    return chudware::util::detail::sha256_sha_ni_available() ? "sha-ni" : "scalar";
#else
    return "scalar";
#endif
}

std::string hex(const Digest& digest) {
    constexpr char alphabet[] = "0123456789abcdef";
    std::string result(64, '0');
    for (size_t i = 0; i < digest.size(); ++i) {
        result[i * 2] = alphabet[digest[i] >> 4];
        result[i * 2 + 1] = alphabet[digest[i] & 15];
    }
    return result;
}
}  // namespace

int main(int argc, char** argv) {
    size_t size = 64u * 1024u * 1024u;
    size_t iterations = 16;
    if (argc > 3 || (argc > 1 && !parse_size(argv[1], size)) ||
        (argc > 2 && !parse_size(argv[2], iterations)) || iterations == 0 ||
        size == std::numeric_limits<size_t>::max()) {
        std::fprintf(stderr, "Usage: %s [size_bytes [iterations>0]]\n", argv[0]);
        return 2;
    }

    try {
        // Offset by one so both current and baseline builds hash unaligned input.
        std::vector<uint8_t> storage(size + 1);
        uint8_t* const bytes = storage.data() + 1;
        for (size_t i = 0; i < size; ++i)
            bytes[i] = static_cast<uint8_t>((i % 256) * 131 + 17);
        const char* const selected_backend = backend();
        Digest digest = hash_payload(bytes, size);  // Warm pages and runtime dispatch.
        volatile uint8_t digest_fold = digest[0];

        const std::clock_t cpu_start = std::clock();
        const auto wall_start = std::chrono::steady_clock::now();
        for (size_t iteration = 0; iteration < iterations; ++iteration) {
            // A compiler memory barrier and observable digest consumption prevent
            // repeated identical input from being folded into one hash under LTO.
            std::atomic_signal_fence(std::memory_order_seq_cst);
            digest = hash_payload(bytes, size);
            digest_fold = static_cast<uint8_t>(digest_fold ^ digest[iteration % digest.size()]);
        }
        const auto wall_end = std::chrono::steady_clock::now();
        const std::clock_t cpu_end = std::clock();
        const double wall_seconds = std::chrono::duration<double>(wall_end - wall_start).count();
        const double mib = (static_cast<double>(size) / (1024.0 * 1024.0)) *
                           static_cast<double>(iterations);
        const double throughput = wall_seconds > 0 ? mib / wall_seconds : 0;

        std::printf("backend=%s size_bytes=%zu iterations=%zu\n", selected_backend, size, iterations);
        std::printf("sha256=%s\n", hex(digest).c_str());
        std::printf("wall_seconds=%.9f throughput_mib_per_second=%.3f\n", wall_seconds, throughput);
        if (cpu_start != std::clock_t(-1) && cpu_end != std::clock_t(-1) && cpu_end >= cpu_start) {
            const double cpu_seconds = static_cast<double>(cpu_end - cpu_start) / CLOCKS_PER_SEC;
            std::printf("cpu_seconds=%.9f\n", cpu_seconds);
        } else {
            std::printf("cpu_seconds=unavailable\n");
        }
        std::printf("digest_fold=%u\n", static_cast<unsigned>(digest_fold));
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "hash_profile: %s\n", error.what());
        return 1;
    }
}
