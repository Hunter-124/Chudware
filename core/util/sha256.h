// Portable streaming SHA-256 (FIPS 180-4), spelled once for core digest callers.
//
// Header-only and C++17 so every core library can use it without a crypto dependency.
// update() streams bytes through one 64-byte block buffer, so callers hashing files or
// large payloads never build a whole-message copy. security/integrity.cpp keeps its own
// independent implementation on purpose: the integrity boundary must not share code
// with the paths it verifies.
#pragma once

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string>
#include <string_view>

namespace chudware::util {

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

    void update(const uint8_t* bytes, size_t length) {
        total += length;
        while (length > 0) {
            const size_t take = std::min(length, sizeof(buffer) - buffered);
            std::memcpy(buffer + buffered, bytes, take);
            buffered += take;
            bytes += take;
            length -= take;
            if (buffered == sizeof(buffer)) {
                block(buffer);
                buffered = 0;
            }
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
