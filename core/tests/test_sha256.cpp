// SHA-256 known answers, padding boundaries, streaming and backend parity.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string>
#include <string_view>
#include <vector>

#include "util/sha256.h"

namespace {
using chudware::util::Sha256;
using Digest = std::array<uint8_t, 32>;

std::string hex(const Digest& digest) {
    constexpr char alphabet[] = "0123456789abcdef";
    std::string result;
    result.reserve(64);
    for (uint8_t byte : digest) {
        result.push_back(alphabet[byte >> 4]);
        result.push_back(alphabet[byte & 15]);
    }
    return result;
}

// Test-only streaming oracle retains the original copy-each-block append path.
// It always uses the unchanged scalar block(), including both padding blocks.
// Keeping this separate tests the new buffering/dispatch rather than reusing it.
struct ScalarStream {
    Sha256 state;

    void update(const uint8_t* bytes, size_t length) {
        state.total += length;
        while (length != 0) {
            const size_t take = std::min(length, sizeof(state.buffer) - state.buffered);
            std::memcpy(state.buffer + state.buffered, bytes, take);
            state.buffered += take;
            bytes += take;
            length -= take;
            if (state.buffered == sizeof(state.buffer)) {
                state.block(state.buffer);
                state.buffered = 0;
            }
        }
    }

    Digest finish() {
        const uint64_t bit_length = state.total * 8;
        uint8_t byte = 0x80;
        update(&byte, 1);
        byte = 0;
        while (state.buffered != 56) update(&byte, 1);
        uint8_t length_bytes[8];
        for (int i = 0; i < 8; ++i)
            length_bytes[i] = static_cast<uint8_t>(bit_length >> (56 - i * 8));
        update(length_bytes, sizeof(length_bytes));
        Digest result{};
        for (int i = 0; i < 8; ++i)
            for (int j = 0; j < 4; ++j)
                result[i * 4 + j] = static_cast<uint8_t>(state.h[i] >> (24 - j * 8));
        return result;
    }
};

uint32_t random_word(uint32_t& state) {
    // Fixed xorshift32 stream: independent of standard-library distribution choices.
    state ^= state << 13;
    state ^= state >> 17;
    state ^= state << 5;
    return state;
}

void check_answer(std::string_view message, const char* expected) {
    const auto* bytes = reinterpret_cast<const uint8_t*>(message.data());
    CAPTURE(message.size());
    CHECK(chudware::util::sha256_hex(message) == expected);
    ScalarStream reference;
    reference.update(bytes, message.size());
    CHECK(hex(reference.finish()) == expected);
    for (size_t chunk : {size_t(1), size_t(7), size_t(55), size_t(64), size_t(65),
                         size_t(127), size_t(4093)}) {
        CAPTURE(chunk);
        Sha256 hash;
        for (size_t offset = 0; offset < message.size();) {
            const size_t take = std::min(chunk, message.size() - offset);
            hash.update(nullptr, 0);
            hash.update(bytes + offset, take);
            offset += take;
        }
        CHECK(hex(hash.finish()) == expected);
    }
}
}  // namespace

TEST_CASE("SHA-256 standard known answers include many blocks") {
    check_answer("", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    check_answer("abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    check_answer("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
                 "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1");
    check_answer("abcdefghbcdefghicdefghijdefghijkefghijklfghijklmghijklmn"
                 "hijklmnoijklmnopjklmnopqklmnopqrlmnopqrsmnopqrstnopqrstu",
                 "cf5b16a778af8380036ce59e7b0492370b249b11e8f07a51afac45037afee9d1");
    check_answer(std::string(1000000, 'a'),
                 "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0");
}

TEST_CASE("SHA-256 padding boundaries have full-width known answers") {
    // NIST CAVS 11.0 SHA256ShortMsg.rsp, Len=440/448/504/512 bits:
    // https://github.com/pyca/cryptography/blob/main/vectors/cryptography_vectors/hashes/SHA2/SHA256ShortMsg.rsp
    struct Vector { const char* message_hex; const char* digest; size_t length; };
    const Vector vectors[] = {
        {"3ebfb06db8c38d5ba037f1363e118550aad94606e26835a01af05078533cc25f2f"
         "39573c04b632f62f68c294ab31f2a3e2a1a0d8c2be51",
         "6595a2ef537a69ba8583dfbf7f5bec0ab1f93ce4c8ee1916eff44a93af5749c4", 55},
        {"2d52447d1244d2ebc28650e7b05654bad35b3a68eedc7f8515306b496d75f3e7"
         "3385dd1b002625024b81a02f2fd6dffb6e6d561cb7d0bd7a",
         "cfb88d6faf2de3a69d36195acec2e255e2af2b7d933997f348e09f6ce5758360", 56},
        {"e2f76e97606a872e317439f1a03fcd92e632e5bd4e7cbc4e97f1afc19a16fde9"
         "2d77cbe546416b51640cddb92af996534dfd81edb17c4424cf1ac4d75aceeb",
         "18041bd4665083001fba8c5411d2d748e8abbfdcdfd9218cb02b68a78e7d4c23", 63},
        {"5a86b737eaea8ee976a0a24da63e7ed7eefad18a101c1211e2b3650c5187c2a8"
         "a650547208251f6d4237e661c7bf4c77f335390394c37fa1a9f9be836ac28509",
         "42e61e174fbb3897d6dd6cef3dd2802fe67b331953b06114a65c772859dfc1aa", 64},
    };
    const auto nibble = [](char c) { return c <= '9' ? c - '0' : c - 'a' + 10; };
    for (const auto& vector : vectors) {
        const std::string_view encoded(vector.message_hex);
        REQUIRE(encoded.size() == vector.length * 2);
        std::string message(vector.length, '\0');
        for (size_t i = 0; i < message.size(); ++i)
            message[i] = static_cast<char>((nibble(encoded[i * 2]) << 4) |
                                          nibble(encoded[i * 2 + 1]));
        check_answer(message, vector.digest);
    }
    check_answer(std::string(65, 'a'),
                 "635361c48bb9eab14198e76ea8ab7f1a41685d6ad62aa9146d301d4f17eb0ae0");
}

TEST_CASE("SHA-256 every two-way split preserves boundary digests") {
    uint32_t random = 0x3a5f789du;
    std::array<uint8_t, 321> data{};
    for (auto& byte : data) byte = static_cast<uint8_t>(random_word(random));
    for (size_t length : {size_t(0), size_t(1), size_t(55), size_t(56), size_t(63),
                          size_t(64), size_t(65), size_t(119), size_t(120),
                          size_t(127), size_t(128), size_t(129), size_t(320)}) {
        ScalarStream reference;
        reference.update(data.data() + 1, length);
        const Digest expected = reference.finish();
        for (size_t split = 0; split <= length; ++split) {
            CAPTURE(length);
            CAPTURE(split);
            Sha256 hash;
            hash.update(data.data() + 1, split);
            hash.update(nullptr, 0);
            hash.update(data.data() + 1 + split, length - split);
            CHECK(hash.finish() == expected);
        }
    }
}

TEST_CASE("SHA-256 randomized unaligned chunking matches original scalar stream") {
    uint32_t random = 0x6c8e9cf5u;
    for (size_t trial = 0; trial < 256; ++trial) {
        const size_t length = random_word(random) % 32769;
        const size_t alignment = trial % 32;
        std::vector<uint8_t> storage(length + alignment + 1);
        uint8_t* const bytes = storage.data() + alignment;
        for (size_t i = 0; i < length; ++i)
            bytes[i] = static_cast<uint8_t>(random_word(random));
        ScalarStream reference;
        reference.update(bytes, length);
        const Digest expected = reference.finish();
        Sha256 contiguous;
        contiguous.update(bytes, length);
        CAPTURE(trial);
        CAPTURE(length);
        CAPTURE(alignment);
        CHECK(contiguous.finish() == expected);
        for (size_t segmentation = 0; segmentation < 4; ++segmentation) {
            Sha256 hash;
            ScalarStream chunked_reference;
            for (size_t offset = 0; offset < length;) {
                const size_t take = std::min<size_t>(1 + random_word(random) % 1024,
                                                     length - offset);
                hash.update(nullptr, 0);
                chunked_reference.update(nullptr, 0);
                hash.update(bytes + offset, take);
                chunked_reference.update(bytes + offset, take);
                offset += take;
            }
            CHECK(hash.finish() == expected);
            CHECK(chunked_reference.finish() == expected);
        }
    }
}

TEST_CASE("SHA-NI multiblock compression matches scalar for arbitrary initial states") {
#ifdef CHUDWARE_UTIL_SHA256_HAS_SHA_NI
    if (!chudware::util::detail::sha256_sha_ni_available()) {
        MESSAGE("SHA/SSSE3/SSE4.1 unavailable: scalar tests run; SHA-NI parity not exercised");
        return;
    }
    uint32_t random = 0x1234abcd;
    for (size_t trial = 0; trial < 128; ++trial) {
        const size_t count = trial == 0 ? 0 : 1 + random_word(random) % 128;
        const size_t alignment = trial % 32;
        std::vector<uint8_t> storage(count * 64 + alignment + 1);
        uint8_t* const bytes = storage.data() + alignment;
        for (size_t i = 0; i < count * 64; ++i)
            bytes[i] = static_cast<uint8_t>(random_word(random));
        Sha256 scalar;
        for (auto& word : scalar.h) word = random_word(random);
        Sha256 hardware = scalar;
        Sha256 segmented = scalar;
        for (size_t i = 0; i < count; ++i) scalar.block(bytes + i * 64);
        chudware::util::detail::sha256_sha_ni_blocks(hardware.h, bytes, count);
        for (size_t offset = 0; offset < count;) {
            const size_t take = std::min<size_t>(1 + random_word(random) % 7, count - offset);
            chudware::util::detail::sha256_sha_ni_blocks(segmented.h, bytes + offset * 64, take);
            offset += take;
        }
        CAPTURE(trial);
        CAPTURE(count);
        CAPTURE(alignment);
        for (size_t i = 0; i < 8; ++i) {
            CHECK(hardware.h[i] == scalar.h[i]);
            CHECK(segmented.h[i] == scalar.h[i]);
        }
    }
#else
    MESSAGE("SHA-NI not compiled on this toolchain: portable scalar coverage only");
#endif
}
