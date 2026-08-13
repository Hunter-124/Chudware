#pragma once

#include <array>
#include <cstddef>
#include <string_view>

namespace chudware::security::release {

inline constexpr std::size_t kPublicKeyBytes = 32;

struct Key {
    std::string_view id;
    std::array<unsigned char, kPublicKeyBytes> publicKey;
};

// Release keys are intentionally owned by this trust ring rather than the
// entitlement verifier. Rotation adds the new key before publishers switch,
// then removes the old key only after older clients are no longer supported.
inline constexpr std::array kKeys = {
    Key{
        "release-v1",
        {
            0xbd, 0xb8, 0x86, 0x90, 0x38, 0xe8, 0x27, 0x4a,
            0x32, 0xa6, 0xf1, 0x4b, 0x86, 0xf0, 0xfb, 0xdb,
            0xe5, 0x99, 0xc6, 0xc7, 0x8e, 0x49, 0xdc, 0x49,
            0x5c, 0xfa, 0x12, 0x75, 0xb0, 0x14, 0x90, 0xf3,
        },
    },
#ifdef CHUDWARE_DEVELOPER_BUILD
    // Deterministic developer-only key (seed 00..1f) for updater regression
    // vectors. It is never trusted by packaged builds.
    Key{
        "dev-test-v1",
        {
            0x03, 0xa1, 0x07, 0xbf, 0xf3, 0xce, 0x10, 0xbe,
            0x1d, 0x70, 0xdd, 0x18, 0xe7, 0x4b, 0xc0, 0x99,
            0x67, 0xe4, 0xd6, 0x30, 0x9b, 0xa5, 0x0d, 0x5f,
            0x1d, 0xdc, 0x86, 0x64, 0x12, 0x55, 0x31, 0xb8,
        },
    },
#endif
};

constexpr const Key* findKey(std::string_view id) noexcept {
    for (const auto& key : kKeys) {
        if (key.id == id) return &key;
    }
    return nullptr;
}

}  // namespace chudware::security::release
