// Streaming SHA-256 (FIPS 180-4), spelled once for core digest callers.
//
// Header-only and C++17, without a crypto dependency or whole-message copy.
// Complete input blocks are consumed directly, including unaligned input. GCC/Clang
// x86 builds select SHA-NI only after checking SHA, SSSE3 and SSE4.1 at runtime;
// all other CPUs/toolchains retain the portable scalar compressor. Target attributes
// keep these instructions out of baseline code (no library-wide -march requirement).
// security/integrity.cpp intentionally retains its independent implementation.
#pragma once

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string>
#include <string_view>

#if (defined(__i386__) || defined(__x86_64__)) && \
    (defined(__GNUC__) || defined(__clang__)) && !defined(_MSC_VER)
#define CHUDWARE_UTIL_SHA256_HAS_SHA_NI 1
#include <cpuid.h>
#include <immintrin.h>
#endif

namespace chudware::util {

namespace detail {

inline bool sha256_sha_ni_available() {
#ifdef CHUDWARE_UTIL_SHA256_HAS_SHA_NI
    static const bool available = [] {
        unsigned int eax, ebx, ecx, edx;
        if (!__get_cpuid(1, &eax, &ebx, &ecx, &edx) ||
            (ecx & ((1u << 9) | (1u << 19))) != ((1u << 9) | (1u << 19)))
            return false;  // SSSE3 and SSE4.1.
        return __get_cpuid_count(7, 0, &eax, &ebx, &ecx, &edx) &&
               (ebx & (1u << 29)) != 0;  // SHA.
    }();
    return available;
#else
    return false;
#endif
}

#ifdef CHUDWARE_UTIL_SHA256_HAS_SHA_NI
// Adapted from Jeffrey Walton's public-domain sha256-x86.c:
// https://github.com/noloader/SHA-Intrinsics/blob/master/sha256-x86.c
// Original attribution: "Written and place in public domain by Jeffrey Walton.
// Based on code from Intel, and by Sean Gulley for the miTLS project."
// Keep its explicit four-round groups and message schedule for straightforward
// comparison with that source. Only dispatch, length units and naming differ.
__attribute__((target("sha,ssse3,sse4.1")))
inline void sha256_sha_ni_blocks(uint32_t state[8], const uint8_t* data, size_t blocks) {
    const __m128i mask = _mm_set_epi64x(0x0c0d0e0f08090a0bULL, 0x0405060700010203ULL);
    __m128i tmp = _mm_loadu_si128(reinterpret_cast<const __m128i*>(state));
    __m128i state1 = _mm_loadu_si128(reinterpret_cast<const __m128i*>(state + 4));
    tmp = _mm_shuffle_epi32(tmp, 0xB1);            // CDAB
    state1 = _mm_shuffle_epi32(state1, 0x1B);     // EFGH
    __m128i state0 = _mm_alignr_epi8(tmp, state1, 8);  // ABEF
    state1 = _mm_blend_epi16(state1, tmp, 0xF0);   // CDGH

    while (blocks != 0) {
        const __m128i abef_save = state0;
        const __m128i cdgh_save = state1;

        // Rounds 0-3.
        __m128i msg0 = _mm_shuffle_epi8(
            _mm_loadu_si128(reinterpret_cast<const __m128i*>(data)), mask);
        __m128i msg = _mm_add_epi32(msg0,
            _mm_set_epi64x(0xE9B5DBA5B5C0FBCFULL, 0x71374491428A2F98ULL));
        state1 = _mm_sha256rnds2_epu32(state1, state0, msg);
        msg = _mm_shuffle_epi32(msg, 0x0E);
        state0 = _mm_sha256rnds2_epu32(state0, state1, msg);

        // Rounds 4-7.
        __m128i msg1 = _mm_shuffle_epi8(
            _mm_loadu_si128(reinterpret_cast<const __m128i*>(data + 16)), mask);
        msg = _mm_add_epi32(msg1,
            _mm_set_epi64x(0xAB1C5ED5923F82A4ULL, 0x59F111F13956C25BULL));
        state1 = _mm_sha256rnds2_epu32(state1, state0, msg);
        msg = _mm_shuffle_epi32(msg, 0x0E);
        state0 = _mm_sha256rnds2_epu32(state0, state1, msg);
        msg0 = _mm_sha256msg1_epu32(msg0, msg1);

        // Rounds 8-11.
        __m128i msg2 = _mm_shuffle_epi8(
            _mm_loadu_si128(reinterpret_cast<const __m128i*>(data + 32)), mask);
        msg = _mm_add_epi32(msg2,
            _mm_set_epi64x(0x550C7DC3243185BEULL, 0x12835B01D807AA98ULL));
        state1 = _mm_sha256rnds2_epu32(state1, state0, msg);
        msg = _mm_shuffle_epi32(msg, 0x0E);
        state0 = _mm_sha256rnds2_epu32(state0, state1, msg);
        msg1 = _mm_sha256msg1_epu32(msg1, msg2);

        // Rounds 12-15.
        __m128i msg3 = _mm_shuffle_epi8(
            _mm_loadu_si128(reinterpret_cast<const __m128i*>(data + 48)), mask);
        msg = _mm_add_epi32(msg3,
            _mm_set_epi64x(0xC19BF1749BDC06A7ULL, 0x80DEB1FE72BE5D74ULL));
        state1 = _mm_sha256rnds2_epu32(state1, state0, msg);
        tmp = _mm_alignr_epi8(msg3, msg2, 4);
        msg0 = _mm_add_epi32(msg0, tmp);
        msg0 = _mm_sha256msg2_epu32(msg0, msg3);
        msg = _mm_shuffle_epi32(msg, 0x0E);
        state0 = _mm_sha256rnds2_epu32(state0, state1, msg);
        msg2 = _mm_sha256msg1_epu32(msg2, msg3);

        // Rounds 16-19.
        msg = _mm_add_epi32(msg0,
            _mm_set_epi64x(0x240CA1CC0FC19DC6ULL, 0xEFBE4786E49B69C1ULL));
        state1 = _mm_sha256rnds2_epu32(state1, state0, msg);
        tmp = _mm_alignr_epi8(msg0, msg3, 4);
        msg1 = _mm_add_epi32(msg1, tmp);
        msg1 = _mm_sha256msg2_epu32(msg1, msg0);
        msg = _mm_shuffle_epi32(msg, 0x0E);
        state0 = _mm_sha256rnds2_epu32(state0, state1, msg);
        msg3 = _mm_sha256msg1_epu32(msg3, msg0);

        // Rounds 20-23.
        msg = _mm_add_epi32(msg1,
            _mm_set_epi64x(0x76F988DA5CB0A9DCULL, 0x4A7484AA2DE92C6FULL));
        state1 = _mm_sha256rnds2_epu32(state1, state0, msg);
        tmp = _mm_alignr_epi8(msg1, msg0, 4);
        msg2 = _mm_add_epi32(msg2, tmp);
        msg2 = _mm_sha256msg2_epu32(msg2, msg1);
        msg = _mm_shuffle_epi32(msg, 0x0E);
        state0 = _mm_sha256rnds2_epu32(state0, state1, msg);
        msg0 = _mm_sha256msg1_epu32(msg0, msg1);

        // Rounds 24-27.
        msg = _mm_add_epi32(msg2,
            _mm_set_epi64x(0xBF597FC7B00327C8ULL, 0xA831C66D983E5152ULL));
        state1 = _mm_sha256rnds2_epu32(state1, state0, msg);
        tmp = _mm_alignr_epi8(msg2, msg1, 4);
        msg3 = _mm_add_epi32(msg3, tmp);
        msg3 = _mm_sha256msg2_epu32(msg3, msg2);
        msg = _mm_shuffle_epi32(msg, 0x0E);
        state0 = _mm_sha256rnds2_epu32(state0, state1, msg);
        msg1 = _mm_sha256msg1_epu32(msg1, msg2);

        // Rounds 28-31.
        msg = _mm_add_epi32(msg3,
            _mm_set_epi64x(0x1429296706CA6351ULL, 0xD5A79147C6E00BF3ULL));
        state1 = _mm_sha256rnds2_epu32(state1, state0, msg);
        tmp = _mm_alignr_epi8(msg3, msg2, 4);
        msg0 = _mm_add_epi32(msg0, tmp);
        msg0 = _mm_sha256msg2_epu32(msg0, msg3);
        msg = _mm_shuffle_epi32(msg, 0x0E);
        state0 = _mm_sha256rnds2_epu32(state0, state1, msg);
        msg2 = _mm_sha256msg1_epu32(msg2, msg3);

        // Rounds 32-35.
        msg = _mm_add_epi32(msg0,
            _mm_set_epi64x(0x53380D134D2C6DFCULL, 0x2E1B213827B70A85ULL));
        state1 = _mm_sha256rnds2_epu32(state1, state0, msg);
        tmp = _mm_alignr_epi8(msg0, msg3, 4);
        msg1 = _mm_add_epi32(msg1, tmp);
        msg1 = _mm_sha256msg2_epu32(msg1, msg0);
        msg = _mm_shuffle_epi32(msg, 0x0E);
        state0 = _mm_sha256rnds2_epu32(state0, state1, msg);
        msg3 = _mm_sha256msg1_epu32(msg3, msg0);

        // Rounds 36-39.
        msg = _mm_add_epi32(msg1,
            _mm_set_epi64x(0x92722C8581C2C92EULL, 0x766A0ABB650A7354ULL));
        state1 = _mm_sha256rnds2_epu32(state1, state0, msg);
        tmp = _mm_alignr_epi8(msg1, msg0, 4);
        msg2 = _mm_add_epi32(msg2, tmp);
        msg2 = _mm_sha256msg2_epu32(msg2, msg1);
        msg = _mm_shuffle_epi32(msg, 0x0E);
        state0 = _mm_sha256rnds2_epu32(state0, state1, msg);
        msg0 = _mm_sha256msg1_epu32(msg0, msg1);

        // Rounds 40-43.
        msg = _mm_add_epi32(msg2,
            _mm_set_epi64x(0xC76C51A3C24B8B70ULL, 0xA81A664BA2BFE8A1ULL));
        state1 = _mm_sha256rnds2_epu32(state1, state0, msg);
        tmp = _mm_alignr_epi8(msg2, msg1, 4);
        msg3 = _mm_add_epi32(msg3, tmp);
        msg3 = _mm_sha256msg2_epu32(msg3, msg2);
        msg = _mm_shuffle_epi32(msg, 0x0E);
        state0 = _mm_sha256rnds2_epu32(state0, state1, msg);
        msg1 = _mm_sha256msg1_epu32(msg1, msg2);

        // Rounds 44-47.
        msg = _mm_add_epi32(msg3,
            _mm_set_epi64x(0x106AA070F40E3585ULL, 0xD6990624D192E819ULL));
        state1 = _mm_sha256rnds2_epu32(state1, state0, msg);
        tmp = _mm_alignr_epi8(msg3, msg2, 4);
        msg0 = _mm_add_epi32(msg0, tmp);
        msg0 = _mm_sha256msg2_epu32(msg0, msg3);
        msg = _mm_shuffle_epi32(msg, 0x0E);
        state0 = _mm_sha256rnds2_epu32(state0, state1, msg);
        msg2 = _mm_sha256msg1_epu32(msg2, msg3);

        // Rounds 48-51.
        msg = _mm_add_epi32(msg0,
            _mm_set_epi64x(0x34B0BCB52748774CULL, 0x1E376C0819A4C116ULL));
        state1 = _mm_sha256rnds2_epu32(state1, state0, msg);
        tmp = _mm_alignr_epi8(msg0, msg3, 4);
        msg1 = _mm_add_epi32(msg1, tmp);
        msg1 = _mm_sha256msg2_epu32(msg1, msg0);
        msg = _mm_shuffle_epi32(msg, 0x0E);
        state0 = _mm_sha256rnds2_epu32(state0, state1, msg);
        msg3 = _mm_sha256msg1_epu32(msg3, msg0);

        // Rounds 52-55.
        msg = _mm_add_epi32(msg1,
            _mm_set_epi64x(0x682E6FF35B9CCA4FULL, 0x4ED8AA4A391C0CB3ULL));
        state1 = _mm_sha256rnds2_epu32(state1, state0, msg);
        tmp = _mm_alignr_epi8(msg1, msg0, 4);
        msg2 = _mm_add_epi32(msg2, tmp);
        msg2 = _mm_sha256msg2_epu32(msg2, msg1);
        msg = _mm_shuffle_epi32(msg, 0x0E);
        state0 = _mm_sha256rnds2_epu32(state0, state1, msg);

        // Rounds 56-59.
        msg = _mm_add_epi32(msg2,
            _mm_set_epi64x(0x8CC7020884C87814ULL, 0x78A5636F748F82EEULL));
        state1 = _mm_sha256rnds2_epu32(state1, state0, msg);
        tmp = _mm_alignr_epi8(msg2, msg1, 4);
        msg3 = _mm_add_epi32(msg3, tmp);
        msg3 = _mm_sha256msg2_epu32(msg3, msg2);
        msg = _mm_shuffle_epi32(msg, 0x0E);
        state0 = _mm_sha256rnds2_epu32(state0, state1, msg);

        // Rounds 60-63.
        msg = _mm_add_epi32(msg3,
            _mm_set_epi64x(0xC67178F2BEF9A3F7ULL, 0xA4506CEB90BEFFFAULL));
        state1 = _mm_sha256rnds2_epu32(state1, state0, msg);
        msg = _mm_shuffle_epi32(msg, 0x0E);
        state0 = _mm_sha256rnds2_epu32(state0, state1, msg);

        state0 = _mm_add_epi32(state0, abef_save);
        state1 = _mm_add_epi32(state1, cdgh_save);
        data += 64;
        --blocks;
    }

    tmp = _mm_shuffle_epi32(state0, 0x1B);        // FEBA
    state1 = _mm_shuffle_epi32(state1, 0xB1);     // DCHG
    state0 = _mm_blend_epi16(tmp, state1, 0xF0);  // DCBA
    state1 = _mm_alignr_epi8(state1, tmp, 8);     // HGFE
    _mm_storeu_si128(reinterpret_cast<__m128i*>(state), state0);
    _mm_storeu_si128(reinterpret_cast<__m128i*>(state + 4), state1);
}
#endif

}  // namespace detail

struct Sha256 {
    uint32_t h[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                     0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    uint8_t buffer[64] = {};
    size_t buffered = 0;
    uint64_t total = 0;

    static uint32_t rotate_right(uint32_t value, int count) {
        return (value >> count) | (value << (32 - count));
    }

    void block(const uint8_t* bytes) {
        static constexpr uint32_t constants[64] = {
            0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
            0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
            0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
            0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
            0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
            0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
            0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
            0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
            0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
            0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
            0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};
        uint32_t words[64];
        for (int i = 0; i < 16; ++i) {
            words[i] = (uint32_t(bytes[i * 4]) << 24) |
                       (uint32_t(bytes[i * 4 + 1]) << 16) |
                       (uint32_t(bytes[i * 4 + 2]) << 8) |
                       uint32_t(bytes[i * 4 + 3]);
        }
        for (int i = 16; i < 64; ++i) {
            const uint32_t s0 = rotate_right(words[i - 15], 7) ^
                                rotate_right(words[i - 15], 18) ^ (words[i - 15] >> 3);
            const uint32_t s1 = rotate_right(words[i - 2], 17) ^
                                rotate_right(words[i - 2], 19) ^ (words[i - 2] >> 10);
            words[i] = words[i - 16] + s0 + words[i - 7] + s1;
        }
        uint32_t a = h[0], b = h[1], c = h[2], d = h[3];
        uint32_t e = h[4], f = h[5], g = h[6], hh = h[7];
        for (int i = 0; i < 64; ++i) {
            const uint32_t s1 = rotate_right(e, 6) ^ rotate_right(e, 11) ^ rotate_right(e, 25);
            const uint32_t choice = (e & f) ^ (~e & g);
            const uint32_t t1 = hh + s1 + choice + constants[i] + words[i];
            const uint32_t s0 = rotate_right(a, 2) ^ rotate_right(a, 13) ^ rotate_right(a, 22);
            const uint32_t majority = (a & b) ^ (a & c) ^ (b & c);
            const uint32_t t2 = s0 + majority;
            hh = g; g = f; f = e; e = d + t1;
            d = c; c = b; b = a; a = t1 + t2;
        }
        h[0] += a; h[1] += b; h[2] += c; h[3] += d;
        h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
    }

    // Keep block() as the portable compressor; both it and the state layout are
    // unchanged. Dispatch once per contiguous run, not once per input block.
    void blocks(const uint8_t* bytes, size_t count) {
#ifdef CHUDWARE_UTIL_SHA256_HAS_SHA_NI
        if (detail::sha256_sha_ni_available()) {
            detail::sha256_sha_ni_blocks(h, bytes, count);
            return;
        }
#endif
        for (size_t i = 0; i < count; ++i) block(bytes + i * sizeof(buffer));
    }

    void update(const uint8_t* bytes, size_t length) {
        total += length;
        if (length == 0) return;
        if (buffered != 0) {
            const size_t take = std::min(length, sizeof(buffer) - buffered);
            std::memcpy(buffer + buffered, bytes, take);
            buffered += take;
            bytes += take;
            length -= take;
            if (buffered != sizeof(buffer)) return;
            blocks(buffer, 1);
            buffered = 0;
        }
        const size_t count = length / sizeof(buffer);
        if (count != 0) {
            blocks(bytes, count);
            const size_t consumed = count * sizeof(buffer);
            bytes += consumed;
            length -= consumed;
        }
        if (length != 0) {
            std::memcpy(buffer, bytes, length);
            buffered = length;
        }
    }

    std::array<uint8_t, 32> finish() {
        const uint64_t bit_length = total * 8;
        uint8_t byte = 0x80;
        update(&byte, 1);
        byte = 0;
        while (buffered != 56) update(&byte, 1);
        uint8_t length_bytes[8];
        for (int i = 0; i < 8; ++i)
            length_bytes[i] = static_cast<uint8_t>(bit_length >> (56 - i * 8));
        update(length_bytes, sizeof(length_bytes));
        std::array<uint8_t, 32> digest{};
        for (int i = 0; i < 8; ++i) {
            digest[i * 4] = static_cast<uint8_t>(h[i] >> 24);
            digest[i * 4 + 1] = static_cast<uint8_t>(h[i] >> 16);
            digest[i * 4 + 2] = static_cast<uint8_t>(h[i] >> 8);
            digest[i * 4 + 3] = static_cast<uint8_t>(h[i]);
        }
        return digest;
    }

    // finish() rendered as 64 lowercase hex characters.
    std::string finish_hex() {
        const auto digest = finish();
        static constexpr char hex[] = "0123456789abcdef";
        std::string result(64, '0');
        for (size_t i = 0; i < digest.size(); ++i) {
            result[i * 2] = hex[digest[i] >> 4];
            result[i * 2 + 1] = hex[digest[i] & 0x0f];
        }
        return result;
    }
};

inline std::string sha256_hex(std::string_view bytes) {
    Sha256 hash;
    hash.update(reinterpret_cast<const uint8_t*>(bytes.data()), bytes.size());
    return hash.finish_hex();
}

}  // namespace chudware::util
