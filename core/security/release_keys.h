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

// Release keys verify signed payload manifests and update manifests. Rotation
// adds the new key before publishers switch, then removes the old key only
// after older clients are no longer supported.
inline constexpr std::array kKeys = {
    Key{
        "release-v1",
        {
            0x29, 0xac, 0xba, 0xe1, 0x41, 0xbc, 0xca, 0xf0,
            0xb2, 0x2e, 0x1a, 0x94, 0xd3, 0x4d, 0x0b, 0xc7,
            0x36, 0x1e, 0x52, 0x6d, 0x0b, 0xfe, 0x12, 0xc8,
            0x97, 0x94, 0xbc, 0x93, 0x22, 0x96, 0x6d, 0xd7,
        },
    },
    // CI beta/signing key (`release-ci-v1`). Nightly CI signs every beta artifact
    // and the paired stable preimage with this key, so packaged builds must trust
    // it unconditionally — it is not a developer-only test key.
    Key{
        "release-ci-v1",
        {
            0xd7, 0x43, 0x4d, 0xc6, 0x7f, 0x4d, 0x6e, 0x11,
            0xcd, 0x4c, 0x8c, 0xed, 0x9b, 0x5e, 0xbb, 0xcc,
            0xf2, 0xe1, 0x47, 0x7b, 0x59, 0x49, 0xd5, 0x2d,
            0x76, 0x85, 0xb6, 0xca, 0x43, 0xa0, 0x10, 0xf6,
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
